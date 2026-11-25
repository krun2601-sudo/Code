"""Move every configured ZR10 to the frozen local home with audited stops.

This tool is intentionally separate from a measured run: homing is not part of
the method timing.  It uses the production HardwareFleet, action leases,
velocity-P controller, ACK/result path, telemetry normalisation, watchdogs, and
shutdown stops.  All selected devices move concurrently and the complete
telemetry/command trace is saved as JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zr10lab.config import load_config
from zr10lab.hardware import HardwareFleet
from zr10lab.models import Action


def state_row(value: Any) -> dict[str, Any]:
    row = asdict(value)
    row["sampled_utc_s"] = time.time()
    return row


async def main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    target = {
        device.id: (float(device.initial_yaw_deg), float(device.initial_pitch_deg))
        for device in cfg.active_devices
    }
    events: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    telemetry: list[dict[str, Any]] = []

    def event_sink(kind: str, payload: dict[str, Any]) -> None:
        if kind != "telemetry":
            events.append({"kind": kind, **payload})

    def result_sink(value: Any) -> None:
        results.append(asdict(value))

    def telemetry_sink(value: Any) -> None:
        telemetry.append(state_row(value))

    fleet = HardwareFleet(cfg, event_sink=event_sink, result_sink=result_sink,
                          telemetry_sink=telemetry_sink)
    error: str | None = None
    initial_states: dict[str, Any] = {}
    final_states: dict[str, Any] = {}
    settled_since: dict[str, float | None] = {key: None for key in target}
    started = time.monotonic()
    motion_started: float | None = None
    try:
        await fleet.start()
        ready_deadline = time.monotonic() + args.connect_timeout_s
        while time.monotonic() < ready_deadline:
            states = fleet.states()
            if all(
                state.connected
                and state.raw.get("zoom_known") is True
                and abs(float(state.zoom) - args.zoom) <= args.zoom_tolerance
                for state in states.values()
            ):
                break
            await asyncio.sleep(args.interval_s)
        states = fleet.states()
        if not all(state.connected for state in states.values()):
            raise TimeoutError("not all devices have fresh telemetry")
        bad_zoom = {
            key: state.zoom for key, state in states.items()
            if state.raw.get("zoom_known") is not True
            or abs(float(state.zoom) - args.zoom) > args.zoom_tolerance
        }
        if bad_zoom:
            raise RuntimeError(f"fixed zoom gate failed: {bad_zoom}")
        unsafe = {
            key: (state.yaw_deg, state.pitch_deg) for key, state in states.items()
            if abs(float(state.yaw_deg)) > args.initial_yaw_limit_deg
            or abs(float(state.pitch_deg)) > args.initial_pitch_limit_deg
        }
        if unsafe:
            raise RuntimeError(f"initial pose exceeds pre-homing safety gate: {unsafe}")
        initial_states = {key: state_row(value) for key, value in states.items()}

        motion_started = time.monotonic()
        while True:
            now = time.monotonic()
            if now - motion_started > args.timeout_s:
                last = {
                    key: (value.yaw_deg, value.pitch_deg)
                    for key, value in fleet.states().items()
                }
                raise TimeoutError(f"home timeout; last poses={last}")
            states = fleet.states()
            actions: dict[str, Action] = {}
            for key, state in states.items():
                if not state.connected:
                    raise RuntimeError(f"{key} telemetry became stale during homing")
                values = (state.yaw_deg, state.pitch_deg, state.yaw_rate_dps, state.pitch_rate_dps)
                if not all(math.isfinite(float(value)) for value in values):
                    raise RuntimeError(f"{key} produced non-finite telemetry")
                if (abs(float(state.yaw_deg)) > args.abort_yaw_deg or
                        abs(float(state.pitch_deg)) > args.abort_pitch_deg):
                    raise RuntimeError(
                        f"{key} crossed homing abort envelope: "
                        f"yaw={state.yaw_deg}, pitch={state.pitch_deg}"
                    )
                target_yaw, target_pitch = target[key]
                pose_ok = (
                    abs(float(state.yaw_deg) - target_yaw) <= args.pose_tolerance_deg
                    and abs(float(state.pitch_deg) - target_pitch) <= args.pose_tolerance_deg
                )
                rate_ok = (
                    abs(float(state.yaw_rate_dps)) <= args.max_rate_dps
                    and abs(float(state.pitch_rate_dps)) <= args.max_rate_dps
                )
                if pose_ok and rate_ok:
                    settled_since[key] = settled_since[key] or now
                else:
                    settled_since[key] = None
                actions[key] = Action(
                    key, yaw_deg=target_yaw, pitch_deg=target_pitch,
                    reason="confirmatory_batch:home", issued_t=now,
                    ttl_s=args.command_ttl_s,
                )
            fleet.submit(actions)
            if all(value is not None and now - value >= args.settle_hold_s
                   for value in settled_since.values()):
                break
            await asyncio.sleep(args.interval_s)

        await asyncio.gather(*(
            unit.cancel_pending("confirmatory_home_complete")
            for unit in fleet.devices.values()
        ))
        await asyncio.sleep(args.post_stop_s)
        final_states = {key: state_row(value) for key, value in fleet.states().items()}
        for key, state in fleet.states().items():
            target_yaw, target_pitch = target[key]
            if (abs(float(state.yaw_deg) - target_yaw) > args.final_tolerance_deg or
                    abs(float(state.pitch_deg) - target_pitch) > args.final_tolerance_deg):
                raise RuntimeError(
                    f"{key} post-stop home error exceeds {args.final_tolerance_deg} deg"
                )
        sent = [row for row in results if row.get("sent") is True]
        errors = [row for row in sent if row.get("ack") is not True or row.get("error")]
        if not sent or errors:
            raise RuntimeError(f"home command result gate failed: sent={len(sent)}, errors={len(errors)}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        close_errors = await asyncio.gather(fleet.close(), return_exceptions=True)
        failures = [repr(item) for item in close_errors if isinstance(item, BaseException)]
        if failures:
            error = f"{error}; close={failures}" if error else f"close={failures}"

    report = {
        "schema": "zr10-confirmatory-home-v1",
        "created_utc_s": time.time(),
        "config": str(Path(args.config).resolve()),
        "target_by_device": target,
        "initial_states": initial_states,
        "final_states": final_states,
        "motion_started_monotonic_s": motion_started,
        "elapsed_s": time.monotonic() - started,
        "events": events,
        "command_results": results,
        "telemetry": telemetry,
        "error": error,
        "pass": error is None and len(final_states) == len(target),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output.resolve()),
        "elapsed_s": report["elapsed_s"],
        "initial_pose": {
            key: [row["yaw_deg"], row["pitch_deg"]]
            for key, row in initial_states.items()
        },
        "final_pose": {
            key: [row["yaw_deg"], row["pitch_deg"]]
            for key, row in final_states.items()
        },
        "sent_commands": sum(row.get("sent") is True for row in results),
        "error": error,
        "pass": report["pass"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--connect-timeout-s", type=float, default=10.0)
    parser.add_argument("--timeout-s", type=float, default=45.0)
    parser.add_argument("--zoom", type=float, default=10.0)
    parser.add_argument("--zoom-tolerance", type=float, default=0.05)
    parser.add_argument("--initial-yaw-limit-deg", type=float, default=100.0)
    parser.add_argument("--initial-pitch-limit-deg", type=float, default=30.0)
    parser.add_argument("--abort-yaw-deg", type=float, default=105.0)
    parser.add_argument("--abort-pitch-deg", type=float, default=35.0)
    parser.add_argument("--pose-tolerance-deg", type=float, default=0.7)
    parser.add_argument("--final-tolerance-deg", type=float, default=1.0)
    parser.add_argument("--max-rate-dps", type=float, default=0.8)
    parser.add_argument("--settle-hold-s", type=float, default=0.5)
    parser.add_argument("--command-ttl-s", type=float, default=0.35)
    parser.add_argument("--interval-s", type=float, default=0.1)
    parser.add_argument("--post-stop-s", type=float, default=0.5)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
