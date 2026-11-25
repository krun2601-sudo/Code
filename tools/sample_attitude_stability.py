"""Collect read-only ZR10 attitude stability evidence without motion commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

from zr10lab.config import load_config
from zr10lab.hardware import normalize_pitch


def _linear_slope(times: list[float], values: list[float]) -> float:
    if len(times) < 2:
        return math.nan
    mt = statistics.fmean(times)
    mv = statistics.fmean(values)
    denominator = sum((item - mt) ** 2 for item in times)
    if denominator <= 0:
        return math.nan
    return sum((t - mt) * (v - mv) for t, v in zip(times, values)) / denominator


async def _sample_one(device: Any, args: argparse.Namespace) -> dict[str, Any]:
    from siyi_sdk import connect_udp

    client = None
    started = time.monotonic()
    samples: list[dict[str, float]] = []
    row: dict[str, Any] = {"device_id": device.id, "ip": device.ip}
    try:
        client = await asyncio.wait_for(
            connect_udp(device.ip, device.port, timeout=0.8, max_retries=0, auto_reconnect=False),
            1.5,
        )
        mode = await asyncio.wait_for(client.get_gimbal_mode(), 1.0)
        row["gimbal_mode"] = getattr(mode, "name", str(mode))
        control = device.control
        yaw_offset = float(control.get("yaw_offset_deg", 0.0))
        yaw_sign = float(control.get("yaw_sign", 1.0))
        pitch_offset = float(control.get("pitch_offset_deg", 0.0))
        pitch_sign = float(control.get("pitch_sign", 1.0))
        normalize = bool(control.get("normalize_pitch_feedback", True))
        count = max(2, int(math.ceil(args.duration_s / args.interval_s)) + 1)
        for index in range(count):
            attitude = await asyncio.wait_for(client.get_gimbal_attitude(), 1.0)
            now = time.monotonic() - started
            raw_pitch = float(attitude.pitch_deg)
            samples.append(
                {
                    "t_s": now,
                    "yaw_raw_deg": float(attitude.yaw_deg),
                    "pitch_raw_deg": raw_pitch,
                    "yaw_deg": (float(attitude.yaw_deg) - yaw_offset) / yaw_sign,
                    "pitch_deg": (normalize_pitch(raw_pitch, normalize) - pitch_offset) / pitch_sign,
                    "yaw_rate_dps": float(attitude.yaw_rate_dps),
                    "pitch_rate_dps": float(attitude.pitch_rate_dps),
                }
            )
            if index + 1 < count:
                await asyncio.sleep(args.interval_s)
        times = [sample["t_s"] for sample in samples]
        yaws = [sample["yaw_deg"] for sample in samples]
        pitches = [sample["pitch_deg"] for sample in samples]
        tail_count = max(3, min(len(samples), int(round(args.tail_s / args.interval_s))))
        row.update(
            ok=True,
            samples=samples,
            summary={
                "sample_count": len(samples),
                "duration_s": times[-1] - times[0],
                "yaw_start_deg": yaws[0],
                "yaw_end_deg": yaws[-1],
                "yaw_range_deg": max(yaws) - min(yaws),
                "yaw_slope_dps_all": _linear_slope(times, yaws),
                "yaw_slope_dps_tail": _linear_slope(times[-tail_count:], yaws[-tail_count:]),
                "pitch_start_deg": pitches[0],
                "pitch_end_deg": pitches[-1],
                "pitch_range_deg": max(pitches) - min(pitches),
                "pitch_slope_dps_all": _linear_slope(times, pitches),
                "pitch_slope_dps_tail": _linear_slope(times[-tail_count:], pitches[-tail_count:]),
            },
        )
    except Exception as exc:
        row.update(ok=False, error=f"{type(exc).__name__}: {exc}", samples=samples)
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), 1.0)
            except Exception as exc:
                row["close_error"] = f"{type(exc).__name__}: {exc}"
    return row


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    devices = await asyncio.gather(*(_sample_one(device, args) for device in cfg.active_devices))
    report = {
        "schema": "zr10-attitude-stability-v1",
        "created_utc_s": time.time(),
        "config": str(Path(args.config).resolve()),
        "duration_s": args.duration_s,
        "interval_s": args.interval_s,
        "motion_commands_sent": False,
        "devices": devices,
        "pass": all(device.get("ok") for device in devices),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    compact = {
        "schema": report["schema"],
        "motion_commands_sent": False,
        "pass": report["pass"],
        "devices": [
            {"device_id": device["device_id"], "ok": device.get("ok"), "gimbal_mode": device.get("gimbal_mode"),
             "summary": device.get("summary"), "error": device.get("error")}
            for device in devices
        ],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--duration-s", type=float, default=30.0)
    parser.add_argument("--interval-s", type=float, default=0.5)
    parser.add_argument("--tail-s", type=float, default=10.0)
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
