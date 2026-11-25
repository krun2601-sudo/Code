"""Safely switch ZR10 gimbal mode with zero velocity before and after.

Dry-run by default.  The tool never sends a position target or non-zero speed.
It independently queries the reported mode after the fire-and-forget mode
request and preserves before/after attitude evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zr10lab.config import load_config


async def _one(device: Any, args: argparse.Namespace) -> dict[str, Any]:
    from siyi_sdk import connect_udp, models

    client = None
    row: dict[str, Any] = {
        "device_id": device.id,
        "ip": device.ip,
        "apply": bool(args.apply),
        "target_mode": args.mode.upper(),
        "nonzero_motion_commands_sent": False,
        "position_commands_sent": False,
        "zero_velocity_commands_sent": 0,
        "mode_commands_sent": 0,
    }
    try:
        client = await asyncio.wait_for(
            connect_udp(device.ip, device.port, timeout=0.8, max_retries=0, auto_reconnect=False),
            1.5,
        )
        mode_before = await asyncio.wait_for(client.get_gimbal_mode(), 1.0)
        attitude_before = await asyncio.wait_for(client.get_gimbal_attitude(), 1.0)
        before_name = getattr(mode_before, "name", str(mode_before))
        row["before"] = {"mode": before_name, "attitude_raw": asdict(attitude_before)}
        if args.apply and before_name != args.mode.upper():
            for _ in range(args.stop_repetitions):
                await asyncio.wait_for(client.rotate(yaw=0, pitch=0), 1.0)
                row["zero_velocity_commands_sent"] += 1
            constant = getattr(models.CaptureFuncType, f"{args.mode.upper()}_MODE")
            await asyncio.wait_for(client.capture(constant), 1.0)
            row["mode_commands_sent"] = 1
            await asyncio.sleep(args.settle_s)
            for _ in range(args.stop_repetitions):
                await asyncio.wait_for(client.rotate(yaw=0, pitch=0), 1.0)
                row["zero_velocity_commands_sent"] += 1
        mode_after = await asyncio.wait_for(client.get_gimbal_mode(), 1.0)
        attitude_after = await asyncio.wait_for(client.get_gimbal_attitude(), 1.0)
        after_name = getattr(mode_after, "name", str(mode_after))
        row["after"] = {"mode": after_name, "attitude_raw": asdict(attitude_after)}
        row["verified"] = after_name == args.mode.upper()
        row["ok"] = True
        if not row["verified"]:
            row["error"] = f"reported mode is {after_name}, expected {args.mode.upper()}"
    except Exception as exc:
        row.update(ok=False, verified=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), 1.0)
            except Exception as exc:
                row["close_error"] = f"{type(exc).__name__}: {exc}"
    return row


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    devices = []
    for device in cfg.active_devices:
        devices.append(await _one(device, args))
    report = {
        "schema": "zr10-gimbal-mode-v1",
        "created_utc_s": time.time(),
        "config": str(Path(args.config).resolve()),
        "apply": bool(args.apply),
        "target_mode": args.mode.upper(),
        "nonzero_motion_commands_sent": False,
        "position_commands_sent": False,
        "devices": devices,
        "pass": all(item.get("ok") and item.get("verified") for item in devices),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=("lock", "follow", "fpv"), required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--stop-repetitions", type=int, default=2)
    parser.add_argument("--settle-s", type=float, default=0.7)
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
