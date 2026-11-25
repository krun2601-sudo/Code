"""Set and independently verify fixed optical zoom on configured ZR10 units.

The command is a dry run unless ``--apply`` is supplied.  It sends no gimbal
motion, mode, stop, or camera-encoding commands.  Some ZR10 firmware executes
the absolute-zoom request but replies using asynchronous function feedback,
so an SDK ACK timeout is recorded rather than treated as proof of failure;
the pass/fail decision uses a fresh connection and independent zoom readback.
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


async def _connect(device: Any) -> Any:
    from siyi_sdk import connect_udp

    return await asyncio.wait_for(
        connect_udp(device.ip, device.port, timeout=0.8, max_retries=0, auto_reconnect=False),
        1.5,
    )


async def _close(client: Any) -> str | None:
    if client is None:
        return None
    try:
        await asyncio.wait_for(client.close(), 1.0)
    except Exception as exc:  # retain cleanup diagnostics without hiding evidence
        return f"{type(exc).__name__}: {exc}"
    return None


async def _readback(device: Any) -> tuple[float, dict[str, Any]]:
    client = None
    try:
        client = await _connect(device)
        zoom = float(await asyncio.wait_for(client.get_current_zoom(), 1.0))
        attitude = await asyncio.wait_for(client.get_gimbal_attitude(), 1.0)
        return zoom, asdict(attitude)
    finally:
        await _close(client)


async def _set_one(device: Any, args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    client = None
    row: dict[str, Any] = {
        "device_id": device.id,
        "ip": device.ip,
        "apply": bool(args.apply),
        "target_zoom": args.zoom,
        "gimbal_motion_commands_sent": False,
        "camera_encoding_commands_sent": False,
        "lens_zoom_commands_sent": 0,
    }
    try:
        zoom_before, attitude_before = await _readback(device)
        row["before"] = {"zoom": zoom_before, "attitude_raw": attitude_before}
        already_at_target = math.isfinite(zoom_before) and abs(zoom_before - args.zoom) <= args.tolerance
        row["planned_change"] = not already_at_target

        if args.apply and not already_at_target:
            client = await _connect(device)
            row["lens_zoom_commands_sent"] = 1
            try:
                await asyncio.wait_for(client.absolute_zoom(args.zoom), args.ack_timeout_s)
                row["command_ack"] = "received"
            except Exception as exc:
                # The SDK exposes its own TimeoutError class.  This firmware
                # commonly applies 0x0F and reports completion as asynchronous
                # 0x05 feedback instead of the ACK awaited by the SDK.
                if "timeout" not in type(exc).__name__.lower() and "timeout" not in str(exc).lower():
                    raise
                row["command_ack"] = "timeout_pending_readback"
                row["command_ack_detail"] = f"{type(exc).__name__}: {exc}"
            finally:
                close_error = await _close(client)
                client = None
                if close_error:
                    row["command_close_error"] = close_error

        deadline = time.monotonic() + (args.verify_timeout_s if args.apply else 0.0)
        samples: list[dict[str, Any]] = []
        while True:
            await asyncio.sleep(args.poll_interval_s if args.apply else 0.0)
            zoom_after, attitude_after = await _readback(device)
            samples.append({"t_monotonic_s": time.monotonic(), "zoom": zoom_after})
            verified = math.isfinite(zoom_after) and abs(zoom_after - args.zoom) <= args.tolerance
            if verified or not args.apply or time.monotonic() >= deadline:
                break

        row["after"] = {"zoom": zoom_after, "attitude_raw": attitude_after}
        row["readback_samples"] = samples
        row["verified"] = verified if args.apply else already_at_target
        row["ok"] = True
        if not row["verified"]:
            row["error"] = f"independent readback did not reach {args.zoom}x; last={zoom_after}"
    except Exception as exc:
        row.update(ok=False, verified=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        close_error = await _close(client)
        if close_error:
            row["close_error"] = close_error
        row["elapsed_ms"] = (time.monotonic() - started) * 1000.0
    return row


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    report: dict[str, Any] = {
        "schema": "zr10-fixed-zoom-v1",
        "created_utc_s": time.time(),
        "config": str(Path(args.config).resolve()),
        "apply": bool(args.apply),
        "target_zoom": args.zoom,
        "gimbal_motion_commands_sent": False,
        "camera_encoding_commands_sent": False,
        "devices": [],
    }
    for device in cfg.active_devices:
        report["devices"].append(await _set_one(device, args))
    report["pass"] = all(item.get("ok") and item.get("verified") for item in report["devices"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--zoom", type=float, default=10.0)
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--ack-timeout-s", type=float, default=1.5)
    parser.add_argument("--verify-timeout-s", type=float, default=20.0)
    parser.add_argument("--poll-interval-s", type=float, default=0.5)
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
