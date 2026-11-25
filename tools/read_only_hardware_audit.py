"""Read-only ZR10 telemetry, zoom, and RTSP audit.

This utility never subscribes to motion control, changes gimbal mode, or sends
stop/position/velocity/zoom commands.  It is intended to establish evidence
before any armed hardware session is allowed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from zr10lab.config import load_config
from zr10lab.hardware import normalize_pitch


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


async def _query_device(device: Any) -> dict[str, Any]:
    from siyi_sdk import connect_udp, models

    started = time.monotonic()
    client = None
    result: dict[str, Any] = {
        "device_id": device.id,
        "ip": device.ip,
        "port": device.port,
    }
    try:
        client = await asyncio.wait_for(
            connect_udp(
                device.ip,
                device.port,
                timeout=0.7,
                max_retries=0,
                auto_reconnect=False,
            ),
            1.2,
        )
        firmware = await asyncio.wait_for(client.get_firmware_version(), 0.9)
        attitude = await asyncio.wait_for(client.get_gimbal_attitude(), 0.9)
        zoom = await asyncio.wait_for(client.get_current_zoom(), 0.9)
        encoding = await asyncio.wait_for(client.get_encoding_params(models.StreamType.MAIN), 0.9)
        control = device.control
        raw_pitch = float(attitude.pitch_deg)
        normalized_pitch = normalize_pitch(
            raw_pitch, bool(control.get("normalize_pitch_feedback", True))
        )
        yaw = (float(attitude.yaw_deg) - float(control.get("yaw_offset_deg", 0.0))) / float(
            control.get("yaw_sign", 1.0)
        )
        pitch = (normalized_pitch - float(control.get("pitch_offset_deg", 0.0))) / float(
            control.get("pitch_sign", 1.0)
        )
        zoom_value = float(zoom)
        valid = all(math.isfinite(value) for value in (yaw, pitch, zoom_value)) and zoom_value > 0
        result.update(
            ok=valid,
            firmware=str(firmware),
            attitude_raw=asdict(attitude),
            attitude_calibrated={"yaw_deg": yaw, "pitch_deg": pitch},
            zoom=zoom_value,
            main_stream_encoding={
                "stream_type": getattr(encoding.stream_type, "name", str(encoding.stream_type)),
                "enc_type": getattr(encoding.enc_type, "name", str(encoding.enc_type)),
                "resolution_w": int(encoding.resolution_w),
                "resolution_h": int(encoding.resolution_h),
                "bitrate_kbps": int(encoding.bitrate_kbps),
                "frame_rate": int(encoding.frame_rate),
            },
        )
        if not valid:
            result["error"] = "non-finite attitude/zoom or non-positive zoom"
    except Exception as exc:  # evidence should retain per-device failures
        result.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), 1.0)
            except Exception as exc:
                result["close_error"] = f"{type(exc).__name__}: {exc}"
    result["latency_ms"] = (time.monotonic() - started) * 1000.0
    return result


def _read_frame(
    device: Any,
    frame_dir: Path,
    rtsp_path: str | None = None,
    frame_reads: int = 60,
) -> dict[str, Any]:
    started = time.monotonic()
    url = (
        f"rtsp://{device.ip}:8554/{rtsp_path.lstrip('/')}"
        if rtsp_path
        else device.rtsp_url or f"rtsp://{device.ip}:8554/main.264"
    )
    result: dict[str, Any] = {"device_id": device.id, "url": url}
    capture = None
    try:
        params = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
            4000,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC,
            2500,
        ]
        capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
        if not capture.isOpened():
            raise RuntimeError("RTSP stream could not be opened")
        image = None
        decoded_frames = 0
        for _ in range(max(1, frame_reads)):
            ok, candidate = capture.read()
            if ok and candidate is not None and candidate.size:
                image = candidate
                decoded_frames += 1
        if image is None:
            raise RuntimeError("RTSP stream opened but yielded no frame")
        height, width = image.shape[:2]
        expected_width, expected_height = (int(value) for value in device.camera.get("image_size", [1920, 1080]))
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        mean_luma = float(np.mean(gray))
        contrast = float(np.std(gray))
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        frame_dir.mkdir(parents=True, exist_ok=True)
        path = frame_dir / f"{device.id}.jpg"
        encoded_ok, encoded = cv2.imencode(".jpg", image)
        if not encoded_ok:
            raise RuntimeError("could not encode diagnostic frame")
        # cv2.imwrite on Windows does not reliably support non-ASCII paths.
        path.write_bytes(encoded.tobytes())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result.update(
            ok=True,
            width=width,
            height=height,
            resolution_ok=(width, height) == (expected_width, expected_height),
            mean_luma=mean_luma,
            contrast=contrast,
            laplacian_variance=sharpness,
            requested_frame_reads=max(1, frame_reads),
            decoded_frames=decoded_frames,
            frame_path=str(path.resolve()),
            frame_sha256=digest,
        )
    except Exception as exc:
        result.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if capture is not None:
            capture.release()
    result["elapsed_ms"] = (time.monotonic() - started) * 1000.0
    return result


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    frame_dir = output.parent / "frames"

    telemetry = await asyncio.gather(*(_query_device(device) for device in cfg.active_devices))
    video: list[dict[str, Any]] = []
    worker_count = 1 if args.sequential_video else len(cfg.active_devices)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                _read_frame, device, frame_dir, args.rtsp_path, args.frame_reads
            ): device.id
            for device in cfg.active_devices
        }
        for future in as_completed(future_map):
            video.append(future.result())
    video.sort(key=lambda item: item["device_id"])

    report = {
        "schema": "zr10-read-only-audit-v1",
        "created_utc_s": time.time(),
        "config": str(Path(args.config).resolve()),
        "motion_commands_sent": False,
        "telemetry": telemetry,
        "video": video,
    }
    report["pass"] = all(item.get("ok") for item in telemetry) and all(
        item.get("ok") and item.get("resolution_ok") for item in video
    )
    output.write_text(json.dumps(_jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(_jsonable(report), ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rtsp-path", help="Temporarily override only the RTSP path for read-only diagnosis")
    parser.add_argument("--frame-reads", type=int, default=60)
    parser.add_argument("--sequential-video", action="store_true")
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
