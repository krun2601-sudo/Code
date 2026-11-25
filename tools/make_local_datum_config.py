"""Create a private runtime config with a read-only local angular datum.

For fixed bench mounts without an external survey reference, each station's
current FOLLOW-mode line of sight is defined as local yaw/pitch zero.  This is
not claimed as a world-frame station-pose calibration.  The output is a full
private config because inherited device endpoints must remain outside the
publishable overlay.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

import yaml

from zr10lab.config import load_config
from zr10lab.hardware import normalize_pitch


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


async def _sample(device: Any, args: argparse.Namespace) -> dict[str, Any]:
    from siyi_sdk import connect_udp

    client = None
    samples: list[dict[str, float]] = []
    try:
        client = await asyncio.wait_for(
            connect_udp(device.ip, device.port, timeout=0.8, max_retries=0, auto_reconnect=False),
            1.5,
        )
        mode = await asyncio.wait_for(client.get_gimbal_mode(), 1.0)
        zoom = float(await asyncio.wait_for(client.get_current_zoom(), 1.0))
        for index in range(args.samples):
            attitude = await asyncio.wait_for(client.get_gimbal_attitude(), 1.0)
            samples.append(
                {
                    "yaw_raw_deg": float(attitude.yaw_deg),
                    "pitch_raw_deg": float(attitude.pitch_deg),
                    "pitch_normalized_deg": normalize_pitch(
                        float(attitude.pitch_deg),
                        bool(device.control.get("normalize_pitch_feedback", True)),
                    ),
                    "yaw_rate_dps": float(attitude.yaw_rate_dps),
                    "pitch_rate_dps": float(attitude.pitch_rate_dps),
                }
            )
            if index + 1 < args.samples:
                await asyncio.sleep(args.interval_s)
        mode_name = getattr(mode, "name", str(mode))
        yaw_values = [item["yaw_raw_deg"] for item in samples]
        pitch_values = [item["pitch_normalized_deg"] for item in samples]
        yaw_offset = statistics.median(yaw_values)
        pitch_offset = statistics.median(pitch_values)
        verified = (
            mode_name == "FOLLOW"
            and math.isfinite(zoom)
            and abs(zoom - args.zoom) <= args.zoom_tolerance
            and max(yaw_values) - min(yaw_values) <= args.max_datum_range_deg
            and max(pitch_values) - min(pitch_values) <= args.max_datum_range_deg
        )
        return {
            "device_id": device.id,
            "ok": True,
            "verified": verified,
            "mode": mode_name,
            "zoom": zoom,
            "yaw_offset_deg": yaw_offset,
            "pitch_offset_deg": pitch_offset,
            "yaw_range_deg": max(yaw_values) - min(yaw_values),
            "pitch_range_deg": max(pitch_values) - min(pitch_values),
            "samples": samples,
        }
    except Exception as exc:
        return {"device_id": device.id, "ok": False, "verified": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), 1.0)
            except Exception:
                pass


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    evidence = await asyncio.gather(*(_sample(device, args) for device in cfg.active_devices))
    passed = all(item.get("ok") and item.get("verified") for item in evidence)
    report = {
        "schema": "zr10-local-datum-v1",
        "created_utc_s": time.time(),
        "source_config": str(Path(args.config).resolve()),
        "motion_commands_sent": False,
        "datum_definition": "current FOLLOW-mode line of sight is local yaw/pitch zero",
        "world_station_pose_verified": False,
        "devices": evidence,
        "pass": passed,
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not passed:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    data = _plain(cfg.to_dict())
    data.pop("source", None)
    by_id = {item["device_id"]: item for item in evidence}
    for device in data["devices"]:
        if not device.get("enabled", True):
            continue
        item = by_id[device["id"]]
        control = device.setdefault("control", {})
        control["startup_lock"] = False
        control["bench_mode"] = "FOLLOW"
        control["yaw_offset_deg"] = float(item["yaw_offset_deg"])
        control["pitch_offset_deg"] = float(item["pitch_offset_deg"])
        device["initial_yaw_deg"] = 0.0
        device["initial_pitch_deg"] = 0.0
        device["initial_zoom"] = float(args.zoom)
    policy = data.setdefault("policy", {})
    policy.setdefault("calibration_evidence", {})["fixed_zoom_verified"] = True
    policy["calibration_evidence"]["local_angular_datum_evidence"] = str(args.evidence.resolve())
    policy["calibration_evidence"]["world_station_pose_verified"] = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(json.dumps({
        "schema": report["schema"],
        "motion_commands_sent": False,
        "output": str(args.output.resolve()),
        "evidence": str(args.evidence.resolve()),
        "devices": [
            {key: item[key] for key in ("device_id", "mode", "zoom", "yaw_offset_deg", "pitch_offset_deg", "yaw_range_deg", "pitch_range_deg")}
            for item in evidence
        ],
        "pass": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--interval-s", type=float, default=0.25)
    parser.add_argument("--zoom", type=float, default=10.0)
    parser.add_argument("--zoom-tolerance", type=float, default=0.05)
    parser.add_argument("--max-datum-range-deg", type=float, default=0.5)
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
