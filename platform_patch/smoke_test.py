"""Dependency-light behavioral smoke test for an already patched overlay.

This test opens no network connection and never reads a platform configuration
file.  It is safe to run during installer dry-run.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path


def _fail(message: str) -> None:
    raise AssertionError(message)


class _OneFrameSource:
    def __init__(self, frame):
        self.frame = frame
        self.delivered = False
        self.last_error = ""

    def start(self) -> None:
        return None

    def poll(self):
        if self.delivered:
            return None
        self.delivered = True
        return self.frame

    def close(self) -> None:
        return None


class _Recorder:
    def __init__(self):
        self.rows = []
        self.events = []

    def write(self, table, t, **values):
        self.rows.append((table, t, values))

    def event(self, kind, payload, t=None):
        self.events.append((kind, payload, t))


class _Policy:
    def reset(self):
        self.context = None

    def decide(self, context):
        from zr10lab.models import Decision
        self.context = context
        return Decision({}, {"smoke": True})


def _vision_check(cfg, frame, VideoFrame, VisionPipeline):
    source = _OneFrameSource(frame)
    pipeline = VisionPipeline(cfg, sources={"unit-1": source}, detection_enabled=False)
    # Start the worker directly.  asyncio's Windows self-pipe opens a local
    # socket, which is deliberately unavailable in some installer sandboxes;
    # frame-health behavior itself does not require an event loop.
    source.start()
    pipeline._thread = threading.Thread(target=pipeline._run, name="smoke-vision", daemon=True)
    pipeline._thread.start()
    deadline = time.monotonic() + 1.0
    statuses = {}
    while time.monotonic() < deadline and not statuses:
        statuses = pipeline.collect_frame_statuses()
        time.sleep(0.005)
    pipeline._close_sync()
    if set(statuses) != {"unit-1"}:
        _fail("disabled detection did not publish frame health")
    status = statuses["unit-1"]
    if not status.usable or not status.resolution_ok or status.width != 12 or status.height != 8:
        _fail("matching frame was not assessed as usable")
    if pipeline.collect_frame_statuses():
        _fail("frame-status mailbox did not consume exactly once")

    now = time.monotonic()
    mismatch = VideoFrame("unit-1", 2, frame.image_bgr[:7], now, now, time.time())
    bad = pipeline._assess_frame(mismatch)
    if bad.usable or bad.resolution_ok or "resolution_mismatch" not in bad.reason:
        _fail("resolution mismatch was not rejected")
    stale = VideoFrame("unit-1", 3, frame.image_bgr, now - 2.0, now - 2.0, time.time())
    stale_status = pipeline._assess_frame(stale)
    if stale_status.usable or "stale_frame" not in stale_status.reason:
        _fail("stale decoded frame was not retained as an unusable status")
    return status


def run(platform_root: Path) -> dict:
    platform_root = platform_root.resolve()
    sys.path.insert(0, str(platform_root))
    for name in tuple(sys.modules):
        if name == "zr10lab" or name.startswith("zr10lab."):
            del sys.modules[name]

    import numpy as np
    from zr10lab import __version__
    from zr10lab.config import DeviceConfig, LabConfig
    from zr10lab.control_modes import mode_catalog
    from zr10lab.models import Action, CommandResult, FrameStatus, PolicyContext, Telemetry
    from zr10lab.recording import CSVRecorder, SCHEMAS
    from zr10lab.runtime import Experiment
    from zr10lab.vision import VideoFrame, VisionPipeline

    if __version__ != "4.0.0":
        _fail(f"unexpected platform version: {__version__}")
    device = DeviceConfig(
        id="unit-1",
        ip="127.0.0.1",
        camera={"image_size": [12, 8], "video_latency_s": 0.0},
        control={"mode": "velocity_p"},
        calibration_verified=True,
    )
    cfg = LabConfig(
        devices=[device],
        system={"max_frame_age_s": 1.0, "telemetry_stale_s": 1.0},
        policy={},
        fusion={},
        action_space={"enabled": ["yaw_deg", "pitch_deg"]},
        control_center={"modes": [{
            "id": "health_only", "policy": "placeholder.policy:HealthPolicy",
            "requires_detection": False, "requires_calibration": False,
            "requires_video": True,
        }]},
    )

    state = Telemetry("unit-1", 1.0, 0.0, 0.0)
    legacy = PolicyContext(1.0, 0.1, 0, {"unit-1": state})
    if legacy.frames or legacy.command_results:
        _fail("legacy positional PolicyContext constructor is not backward compatible")

    image = np.full((8, 12, 3), 128, dtype=np.uint8)
    now = time.monotonic()
    frame = VideoFrame("unit-1", 1, image, now, now, time.time())
    frame_status = _vision_check(cfg, frame, VideoFrame, VisionPipeline)

    action = Action("unit-1", issued_t=1.0, ttl_s=0.5)
    command = CommandResult("unit-1", 1.0, action, "acknowledged", True, True, 2.0)
    recorder = _Recorder()
    policy = _Policy()
    experiment = Experiment(cfg, recorder, policy=policy)
    live_state = Telemetry(
        "unit-1", now, 0.0, 0.0, source="hardware",
        raw={"zoom_known": True, "zoom_stable": True},
    )
    experiment.process(
        now, 0.1, {"unit-1": live_state}, [],
        frames={"unit-1": frame_status}, command_results={"unit-1": command},
    )
    if policy.context.frames.get("unit-1") != frame_status:
        _fail("Experiment did not pass frame status to the policy")
    if policy.context.command_results.get("unit-1") != command:
        _fail("Experiment did not pass command result to the policy")
    if not any(table == "frames" for table, _, _ in recorder.rows):
        _fail("Experiment did not write the frame record")

    entries = {row["id"]: row for row in mode_catalog(cfg)}
    if not entries["health_only"].get("requires_video"):
        _fail("requires_video was not preserved by the mode registry")
    if "frames" not in SCHEMAS or "usable" not in SCHEMAS["frames"]:
        _fail("frames.csv schema is absent")

    with tempfile.TemporaryDirectory(prefix="ral_hil_recording_", dir=platform_root.parent) as name:
        csv_recorder = CSVRecorder(name, cfg.to_dict(), "hardware", start_t=now)
        csv_recorder.close(completed=True)
        metadata = json.loads((csv_recorder.path / "metadata.json").read_text(encoding="utf-8"))
        if metadata.get("schema_version") != 1:
            _fail("global schema_version changed")
        if metadata.get("platform_patch_version") != "ral-hil-context-1.0.0":
            _fail("metadata does not identify the platform patch")
        if not (csv_recorder.path / "frames.csv").is_file():
            _fail("frames.csv was not created")

    # Type construction is intentionally explicit: it catches field drift.
    constructed = FrameStatus("unit-1", 3, now, now, 12, 8)
    if not constructed.usable:
        _fail("FrameStatus defaults are invalid")
    return {
        "ok": True,
        "platform_version": __version__,
        "policy_context_legacy_compatible": True,
        "frame_health_without_detection": True,
        "command_result_visible": True,
        "frames_csv": True,
        "schema_version": 1,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a patched ZR10 platform tree")
    parser.add_argument("--platform-root", required=True)
    args = parser.parse_args(argv)
    try:
        result = run(Path(args.platform_root))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

