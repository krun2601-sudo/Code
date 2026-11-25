"""Establish and verify the camera-only baseline for the four-ZR10 trial.

The default is a dry run.  ``--apply`` may change only MAIN-stream encoding
and absolute optical zoom.  It never sends gimbal position/velocity, mode, or
stop commands.  The report records the original parameters for restoration.
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


def _encoding_dict(value: Any) -> dict[str, Any]:
    return {
        "stream_type": getattr(value.stream_type, "name", str(value.stream_type)),
        "enc_type": getattr(value.enc_type, "name", str(value.enc_type)),
        "resolution_w": int(value.resolution_w),
        "resolution_h": int(value.resolution_h),
        "bitrate_kbps": int(value.bitrate_kbps),
        "frame_rate": int(value.frame_rate),
    }


async def _configure(device: Any, args: argparse.Namespace) -> dict[str, Any]:
    from siyi_sdk import connect_udp, models

    client = None
    started = time.monotonic()
    row: dict[str, Any] = {
        "device_id": device.id,
        "ip": device.ip,
        "apply": bool(args.apply),
        "gimbal_motion_commands_sent": False,
        "camera_configuration_commands_sent": 0,
        "lens_zoom_commands_sent": 0,
    }
    try:
        client = await asyncio.wait_for(
            connect_udp(device.ip, device.port, timeout=0.8, max_retries=0, auto_reconnect=False),
            1.5,
        )
        attitude_before = await asyncio.wait_for(client.get_gimbal_attitude(), 1.0)
        encoding_before = await asyncio.wait_for(client.get_encoding_params(models.StreamType.MAIN), 1.0)
        zoom_before = float(await asyncio.wait_for(client.get_current_zoom(), 1.0))
        row["before"] = {
            "attitude_raw": asdict(attitude_before),
            "main_stream_encoding": _encoding_dict(encoding_before),
            "zoom": zoom_before,
        }

        target_encoding = {
            "stream_type": "MAIN",
            "enc_type": args.codec,
            "resolution_w": args.width,
            "resolution_h": args.height,
            "bitrate_kbps": args.bitrate_kbps,
        }
        row["target"] = {"main_stream_encoding": target_encoding, "zoom": args.zoom}
        encoding_matches = (
            getattr(encoding_before.stream_type, "name", "") == "MAIN"
            and getattr(encoding_before.enc_type, "name", "") == args.codec
            and int(encoding_before.resolution_w) == args.width
            and int(encoding_before.resolution_h) == args.height
            and int(encoding_before.bitrate_kbps) == args.bitrate_kbps
        )
        zoom_matches = math.isfinite(zoom_before) and abs(zoom_before - args.zoom) <= args.zoom_tolerance
        row["planned_changes"] = {
            "encoding": not encoding_matches,
            "zoom": not zoom_matches,
        }

        if args.apply and not encoding_matches:
            params = models.EncodingParams(
                stream_type=models.StreamType.MAIN,
                enc_type=models.VideoEncType[args.codec],
                resolution_w=args.width,
                resolution_h=args.height,
                bitrate_kbps=args.bitrate_kbps,
                frame_rate=0,
            )
            if not await asyncio.wait_for(client.set_encoding_params(params), 1.5):
                raise RuntimeError("camera rejected MAIN-stream encoding update")
            row["camera_configuration_commands_sent"] += 1
            await asyncio.sleep(1.0)

        if args.apply and not zoom_matches:
            row["lens_zoom_commands_sent"] += 1
            # Count the command as sent before waiting for the firmware's ACK.
            # Several ZR10 revisions execute 0x0F but answer with asynchronous
            # 0x05 function feedback, causing the SDK wait to time out.
            await asyncio.wait_for(client.absolute_zoom(args.zoom), 1.5)
            deadline = time.monotonic() + args.zoom_timeout_s
            while True:
                await asyncio.sleep(0.4)
                current = float(await asyncio.wait_for(client.get_current_zoom(), 1.0))
                if math.isfinite(current) and abs(current - args.zoom) <= args.zoom_tolerance:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"zoom did not reach {args.zoom}x; last readback={current}")

        encoding_after = await asyncio.wait_for(client.get_encoding_params(models.StreamType.MAIN), 1.0)
        zoom_after = float(await asyncio.wait_for(client.get_current_zoom(), 1.0))
        attitude_after = await asyncio.wait_for(client.get_gimbal_attitude(), 1.0)
        row["after"] = {
            "attitude_raw": asdict(attitude_after),
            "main_stream_encoding": _encoding_dict(encoding_after),
            "zoom": zoom_after,
        }
        after_dict = row["after"]["main_stream_encoding"]
        row["verified"] = (
            after_dict["stream_type"] == "MAIN"
            and after_dict["enc_type"] == args.codec
            and after_dict["resolution_w"] == args.width
            and after_dict["resolution_h"] == args.height
            and after_dict["bitrate_kbps"] == args.bitrate_kbps
            and abs(zoom_after - args.zoom) <= args.zoom_tolerance
        ) if args.apply else encoding_matches and zoom_matches
        row["ok"] = True
    except Exception as exc:
        row.update(ok=False, verified=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), 1.0)
            except Exception as exc:
                row["close_error"] = f"{type(exc).__name__}: {exc}"
        row["elapsed_ms"] = (time.monotonic() - started) * 1000.0
    return row


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    report = {
        "schema": "zr10-camera-baseline-v1",
        "created_utc_s": time.time(),
        "config": str(Path(args.config).resolve()),
        "apply": bool(args.apply),
        "gimbal_motion_commands_sent": False,
        "devices": [],
    }
    for device in cfg.active_devices:
        report["devices"].append(await _configure(device, args))
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
    parser.add_argument("--codec", choices=("H264", "H265"), default="H264")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--bitrate-kbps", type=int, default=3584)
    parser.add_argument("--zoom", type=float, default=10.0)
    parser.add_argument("--zoom-tolerance", type=float, default=0.05)
    parser.add_argument("--zoom-timeout-s", type=float, default=20.0)
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
