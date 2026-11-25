#!/usr/bin/env python3
"""Read-only concurrent RTSP stability gate for the four ZR10 streams.

The command never sends an SDK command.  It continuously decodes all enabled
streams for the requested interval and fails if any stream has a decoded-frame
gap above the frozen bound or an unexpected resolution.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import (
    canonical_json, hardware_config_sha256, load_yaml_with_inheritance,
    sha256_bytes, write_json,
)
from zr10lab.config import load_config


def stream_url(device: Any) -> str:
    return device.rtsp_url or f"rtsp://{device.ip}:8554/main.264"


def monitor(device: Any, duration_s: float, max_gap_s: float,
            startup_timeout_s: float) -> dict[str, Any]:
    import cv2

    started = time.monotonic()
    capture = None
    decoded = 0
    failed_reads = 0
    first_frame_t: float | None = None
    last_frame_t: float | None = None
    maximum_gap = 0.0
    resolutions: set[tuple[int, int]] = set()
    error = ""
    startup_to_first_frame_s: float | None = None
    try:
        parameters = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 4000,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(max(500, max_gap_s * 1000)),
        ]
        capture = cv2.VideoCapture(stream_url(device), cv2.CAP_FFMPEG, parameters)
        if not capture.isOpened():
            raise RuntimeError("RTSP stream could not be opened")
        # Connection/decoder startup is reported separately.  The stability
        # interval begins at the first decoded frame so every stream receives
        # the full requested observation duration; startup handshake latency
        # is not an in-stream outage.
        first_frame_deadline = started + startup_timeout_s
        deadline: float | None = None
        while deadline is None or time.monotonic() < deadline:
            ok, image = capture.read()
            now = time.monotonic()
            if not ok or image is None or not image.size:
                failed_reads += 1
                if first_frame_t is None and now >= first_frame_deadline:
                    raise RuntimeError(
                        f"no decoded frame within startup timeout {startup_timeout_s:.3f}s"
                    )
                continue
            decoded += 1
            height, width = image.shape[:2]
            resolutions.add((int(width), int(height)))
            if first_frame_t is None:
                first_frame_t = now
                startup_to_first_frame_s = now - started
                deadline = now + duration_s
            if last_frame_t is not None:
                maximum_gap = max(maximum_gap, now - last_frame_t)
            last_frame_t = now
        finished = time.monotonic()
        if last_frame_t is not None:
            maximum_gap = max(maximum_gap, finished - last_frame_t)
    except Exception as exc:
        finished = time.monotonic()
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if capture is not None:
            capture.release()
    expected = tuple(int(value) for value in device.camera.get("image_size", [1920, 1080]))
    span = (last_frame_t - first_frame_t
            if first_frame_t is not None and last_frame_t is not None else 0.0)
    passed = (
        not error and decoded > 0 and span >= max(0.0, duration_s - max_gap_s)
        and maximum_gap <= max_gap_s + 1e-9 and resolutions == {expected}
    )
    return {
        "device_id": device.id,
        "pass": passed,
        "requested_duration_s": duration_s,
        "observed_wall_duration_s": finished - started,
        "startup_to_first_frame_s": startup_to_first_frame_s,
        "decoded_span_s": span,
        "decoded_frame_count": decoded,
        "failed_read_count": failed_reads,
        "maximum_decoded_frame_gap_s": maximum_gap if math.isfinite(maximum_gap) else None,
        "maximum_allowed_gap_s": max_gap_s,
        "observed_resolutions": [list(value) for value in sorted(resolutions)],
        "expected_resolution": list(expected),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--duration-s", type=float, default=300.0)
    parser.add_argument("--max-gap-s", type=float, default=2.0)
    parser.add_argument("--startup-timeout-s", type=float, default=20.0)
    args = parser.parse_args()
    if args.duration_s < 300.0:
        parser.error("the V2 readiness gate requires at least 300 s")
    if args.max_gap_s <= 0 or args.max_gap_s > 2.0:
        parser.error("the V2 readiness gate requires 0 < max-gap-s <= 2")
    if args.startup_timeout_s <= 0:
        parser.error("--startup-timeout-s must be positive")
    config_path = args.config.resolve()
    raw_config = load_yaml_with_inheritance(config_path)
    cfg = load_config(config_path)
    devices = list(cfg.active_devices)
    if len(devices) != 4:
        parser.error("exactly four enabled devices are required")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(
            monitor, device, args.duration_s, args.max_gap_s, args.startup_timeout_s
        )
                   for device in devices]
        results = [future.result() for future in futures]
    report = {
        "schema_version": 1,
        "gate": "v2_four_stream_rtsp_stability",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "network_read_only": True,
        "motion_or_sdk_command_performed": False,
        "hardware_config_sha256": hardware_config_sha256(raw_config),
        "duration_s": args.duration_s,
        "max_gap_s": args.max_gap_s,
        "startup_timeout_s": args.startup_timeout_s,
        "devices": results,
        "pass": len(results) == 4 and all(row["pass"] for row in results),
    }
    report["report_sha256"] = sha256_bytes(canonical_json(report))
    write_json(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
