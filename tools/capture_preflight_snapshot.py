#!/usr/bin/env python3
"""Capture a synchronized four-ZR10 commissioning preflight snapshot.

The only control packet issued by this tool is the production adapter's
zero-velocity stop.  It is used both as a safety action and as a fresh ACK
check.  No non-zero velocity or position target is submitted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from common import hardware_config_sha256, load_yaml_with_inheritance, sha256_file
from zr10lab import __version__ as platform_version
from zr10lab.config import load_config
from zr10lab.hardware import HardwareFleet
import zr10lab.recording as platform_recording
from zr10lab.vision import VisionPipeline


PATCH_VERSION = "ral-hil-context-1.0.0"


async def _wait_ready(fleet: HardwareFleet, timeout_s: float, zoom: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        states = fleet.states()
        if states and all(
            state.connected
            and state.raw.get("zoom_known") is True
            and state.raw.get("zoom_stable") is True
            and abs(state.zoom - zoom) <= 0.15
            for state in states.values()
        ):
            return states
        await asyncio.sleep(0.05)
    raise TimeoutError("four-device telemetry/zoom readiness gate timed out")


async def _wait_fresh_frames(
    vision: VisionPipeline,
    device_ids: list[str],
    timeout_s: float,
    max_age_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest.update(vision.collect_frame_statuses())
        now = time.monotonic()
        if all(
            key in latest
            and latest[key].usable
            and latest[key].resolution_ok
            and 0 <= now - latest[key].capture_t <= max_age_s
            for key in device_ids
        ):
            return latest
        await asyncio.sleep(0.02)
    detail = {
        key: (
            None
            if key not in latest
            else {
                "age_s": time.monotonic() - latest[key].capture_t,
                "usable": latest[key].usable,
                "resolution_ok": latest[key].resolution_ok,
                "reason": latest[key].reason,
            }
        )
        for key in device_ids
    }
    raise TimeoutError(f"four-device fresh-frame gate timed out: {detail}")


def _runtime_patch_version() -> str:
    source = Path(platform_recording.__file__).read_text(encoding="utf-8")
    return PATCH_VERSION if PATCH_VERSION in source else ""


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    expanded = load_yaml_with_inheritance(Path(args.config))
    if len(cfg.active_devices) != 4:
        raise ValueError(f"expected four active devices, found {len(cfg.active_devices)}")

    events: list[dict[str, Any]] = []

    def event_sink(kind: str, payload: dict[str, Any]) -> None:
        if kind != "telemetry":
            events.append({"kind": kind, **payload})

    fleet = HardwareFleet(cfg, event_sink=event_sink)
    vision = VisionPipeline(cfg, event_sink=event_sink, detection_enabled=False)
    frames: dict[str, Any] = {}
    snapshot: dict[str, Any] = {}
    try:
        await vision.start()
        await fleet.start()
        await _wait_ready(fleet, args.connect_timeout_s, args.zoom)
        max_frame_age_s = min(
            args.max_snapshot_frame_age_s,
            float(cfg.system.get("max_frame_age_s", 0.6)),
        )
        frames = await _wait_fresh_frames(
            vision,
            [device.id for device in cfg.active_devices],
            args.frame_timeout_s,
            max_frame_age_s,
        )

        stop_reason = "preflight_zero_velocity_ack"
        await asyncio.gather(*(
            unit.cancel_pending(stop_reason) for unit in fleet.devices.values()
        ))
        await asyncio.sleep(args.post_stop_s)
        captured_t = time.monotonic()
        states = fleet.states()
        stop_events = {
            item["device_id"]: item
            for item in events
            if item.get("kind") == "stop" and item.get("reason") == stop_reason
        }
        devices: dict[str, Any] = {}
        for device in cfg.active_devices:
            state = states[device.id]
            frame = frames[device.id]
            stop = stop_events.get(device.id, {})
            telemetry_age = captured_t - float(state.t)
            frame_age = captured_t - float(frame.capture_t)
            ack_t = float(stop.get("t", -math.inf))
            ack_age = captured_t - ack_t
            devices[device.id] = {
                "telemetry": {
                    "connected": state.connected,
                    "sample_t": state.t,
                    "age_s": telemetry_age,
                    "zoom": state.zoom,
                    "raw": dict(state.raw),
                    "state": asdict(state),
                },
                "frame": {
                    "frame_id": frame.frame_id,
                    "capture_t": frame.capture_t,
                    "age_s": frame_age,
                    "received_t": frame.received_t,
                    "width": frame.width,
                    "height": frame.height,
                    "usable": frame.usable,
                    "resolution_ok": frame.resolution_ok,
                    "mean_luma": frame.mean_luma,
                    "contrast": frame.contrast,
                    "sharpness": frame.sharpness,
                    "skipped_frames": frame.skipped_frames,
                    "reason": frame.reason,
                },
                "command_result": {
                    "status": "acknowledged" if stop.get("ack") is True else "error",
                    "sent": bool(stop),
                    "ack": stop.get("ack") is True,
                    "error": stop.get("error", "missing stop event"),
                    "age_s": ack_age,
                    "reason": stop_reason,
                },
            }

        # Build the exact policy once without issuing an action.  This records
        # the frozen task cardinality and geometric reachability that the
        # experiment preflight must bind to the selected overlay.
        from algorithms.user_policy import MyPolicy
        policy = MyPolicy(cfg)
        policy_module = sys.modules[MyPolicy.__module__]
        policy_source = Path(str(policy_module.__file__)).resolve()
        coverage_task = {
            "evaluation_cell_count": len(policy.cells),
            "reachable_count": len(policy.reachable),
            "reachable_ratio": (
                len(policy.reachable) / len(policy.cells) if policy.cells else 0.0
            ),
            "evaluation_grid": list(policy.evaluation_grid),
            "candidate_count": len(policy.candidates),
        }

        snapshot = {
            "schema_version": 1,
            "source": "synchronized_live_hardware_capture",
            "captured_monotonic_s": captured_t,
            "created_utc_s": time.time(),
            "hardware_config_sha256": hardware_config_sha256(expanded),
            "platform_version": platform_version,
            "platform_patch_version": _runtime_patch_version(),
            "policy_source_sha256": sha256_file(policy_source),
            "policy_algorithm_version": getattr(policy_module, "ALGORITHM_VERSION", ""),
            "coverage_task": coverage_task,
            "motion_commands_sent": False,
            "zero_velocity_stop_sent": True,
            "devices": devices,
        }
    finally:
        errors = await asyncio.gather(fleet.close(), vision.close(), return_exceptions=True)
        failures = [repr(error) for error in errors if isinstance(error, BaseException)]
        if failures:
            raise RuntimeError(f"preflight snapshot shutdown failed: {failures}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "output": str(args.output.resolve()),
        "platform_version": snapshot["platform_version"],
        "platform_patch_version": snapshot["platform_patch_version"],
        "motion_commands_sent": snapshot["motion_commands_sent"],
        "devices": {
            key: {
                "telemetry_age_s": value["telemetry"]["age_s"],
                "frame_age_s": value["frame"]["age_s"],
                "frame_id": value["frame"]["frame_id"],
                "ack": value["command_result"]["ack"],
                "ack_age_s": value["command_result"]["age_s"],
            }
            for key, value in snapshot["devices"].items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--connect-timeout-s", type=float, default=10.0)
    parser.add_argument("--frame-timeout-s", type=float, default=20.0)
    parser.add_argument("--max-snapshot-frame-age-s", type=float, default=0.35)
    parser.add_argument("--post-stop-s", type=float, default=0.05)
    parser.add_argument("--zoom", type=float, default=10.0)
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
