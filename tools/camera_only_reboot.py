"""Audited, single-device camera-module reboot; never reset the gimbal."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zr10lab.config import load_config


def _encoding(value: Any) -> dict[str, Any]:
    return {
        "stream_type": getattr(value.stream_type, "name", str(value.stream_type)),
        "enc_type": getattr(value.enc_type, "name", str(value.enc_type)),
        "resolution_w": int(value.resolution_w),
        "resolution_h": int(value.resolution_h),
        "bitrate_kbps": int(value.bitrate_kbps),
        "frame_rate": int(value.frame_rate),
    }


async def _snapshot(device: Any) -> tuple[Any, dict[str, Any]]:
    from siyi_sdk import connect_udp, models

    client = await asyncio.wait_for(
        connect_udp(device.ip, device.port, timeout=0.8, max_retries=0, auto_reconnect=False),
        1.5,
    )
    attitude = await asyncio.wait_for(client.get_gimbal_attitude(), 1.0)
    zoom = float(await asyncio.wait_for(client.get_current_zoom(), 1.0))
    encoding = await asyncio.wait_for(client.get_encoding_params(models.StreamType.MAIN), 1.0)
    return client, {
        "attitude_raw": asdict(attitude),
        "zoom": zoom,
        "main_stream_encoding": _encoding(encoding),
    }


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    devices = {device.id: device for device in cfg.active_devices}
    if args.device not in devices:
        raise SystemExit(f"unknown or inactive device: {args.device}")
    device = devices[args.device]
    report: dict[str, Any] = {
        "schema": "zr10-camera-only-reboot-v1",
        "created_utc_s": time.time(),
        "device_id": device.id,
        "ip": device.ip,
        "apply": bool(args.apply),
        "camera_reboot_requested": False,
        "gimbal_reboot_requested": False,
        "gimbal_motion_commands_sent": False,
    }
    client = None
    try:
        client, report["before"] = await _snapshot(device)
        if not args.apply:
            report.update(ok=True, note="dry run; no reboot sent")
            report["pass"] = False
        else:
            camera_ack, gimbal_ack = await asyncio.wait_for(
                client.soft_reboot(camera=True, gimbal=False), 2.0
            )
            report["camera_reboot_requested"] = True
            report["reboot_ack"] = {"camera": bool(camera_ack), "gimbal": bool(gimbal_ack)}
            if gimbal_ack:
                raise RuntimeError("unexpected gimbal reboot acknowledgment")
            try:
                await asyncio.wait_for(client.close(), 1.0)
            finally:
                client = None

            deadline = time.monotonic() + args.recovery_timeout_s
            errors: list[str] = []
            while time.monotonic() < deadline:
                await asyncio.sleep(args.poll_s)
                try:
                    client, report["after"] = await _snapshot(device)
                    break
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
            else:
                raise TimeoutError("camera module did not recover before deadline")
            report["recovery_attempt_errors"] = errors
            report["ok"] = bool(camera_ack) and not bool(gimbal_ack)
            report["pass"] = report["ok"]
    except Exception as exc:
        report.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        report["pass"] = False
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), 1.0)
            except Exception as exc:
                report["close_error"] = f"{type(exc).__name__}: {exc}"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("pass") is True else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--recovery-timeout-s", type=float, default=30.0)
    parser.add_argument("--poll-s", type=float, default=2.0)
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
