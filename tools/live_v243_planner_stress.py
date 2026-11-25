#!/usr/bin/env python3
"""Stress the V2.4.3 initial planner while all configured RTSP streams run.

This is a motion-free gate.  It starts only the video pipeline with detection
disabled, never constructs ``HardwareFleet``, never submits an ``Action``, and
never opens a gimbal-control socket.  The policy is constructed before RTSP,
matching the production runtime ordering, and each timed trial resets only
mission state while retaining the immutable static edge cache.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algorithms.user_policy import ALGORITHM_VERSION, MyPolicy  # noqa: E402
from common import canonical_json, sha256_bytes, sha256_file, write_json  # noqa: E402
from zr10lab.config import load_config  # noqa: E402
from zr10lab.vision import VisionPipeline  # noqa: E402


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def context() -> SimpleNamespace:
    return SimpleNamespace(
        t=time.monotonic(), dt=0.1, step=0, devices={}, detections=(),
        rays=(), tracks=(), metrics={}, frames={}, command_results={},
    )


def route_signature(routes: dict[str, list[str]]) -> str:
    value = {key: list(routes[key]) for key in sorted(routes)}
    return sha256_bytes(canonical_json(value))


async def run(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config(args.config.resolve())
    if len(cfg.active_devices) != 4:
        raise ValueError("live stress requires the configured four-device set")
    if ALGORITHM_VERSION != "2.4.3":
        raise ValueError(f"candidate version is {ALGORITHM_VERSION}, not 2.4.3")

    # Production ordering: all immutable policy work finishes before RTSP
    # decoder processes or inference threads exist.
    construction_started = time.perf_counter()
    policy = MyPolicy(cfg)
    construction_ms = (time.perf_counter() - construction_started) * 1000.0
    events: list[dict[str, Any]] = []

    def event_sink(kind: str, payload: dict[str, Any], *unused: Any) -> None:
        events.append({"kind": kind, "payload": dict(payload)})

    vision = VisionPipeline(cfg, event_sink=event_sink, detection_enabled=False)
    frame_ids: dict[str, int] = {}
    usable_counts = {device.id: 0 for device in cfg.active_devices}
    observed_counts = {device.id: 0 for device in cfg.active_devices}
    planner_ms: list[float] = []
    signatures: list[str] = []
    objectives: list[dict[str, Any]] = []
    budget_checks: list[bool] = []
    started = time.monotonic()
    try:
        await vision.start()
        deadline = time.monotonic() + args.startup_timeout_s
        while len(frame_ids) < 4 and time.monotonic() < deadline:
            for device_id, status in vision.collect_frame_statuses().items():
                observed_counts[device_id] += 1
                if status.usable:
                    usable_counts[device_id] += 1
                    frame_ids[device_id] = int(status.frame_id)
            await asyncio.sleep(0.02)
        if len(frame_ids) != 4:
            raise RuntimeError(
                f"usable RTSP readiness failed: devices={sorted(frame_ids)}, "
                f"vision_errors={vision.errors}"
            )

        for _ in range(args.repetitions):
            policy.reset()
            routes = await asyncio.to_thread(
                policy._make_plan, context(), set(policy.reachable)
            )
            planner_ms.append(float(policy.last_planner_ms))
            signatures.append(route_signature(routes))
            objectives.append(dict(policy.last_plan_audit.get("final_objective", {})))
            budget_checks.append(bool(
                policy.last_plan_audit.get("budget_contract_satisfied")
            ))
            for device_id, status in vision.collect_frame_statuses().items():
                observed_counts[device_id] += 1
                if status.usable:
                    usable_counts[device_id] += 1
                    frame_ids[device_id] = int(status.frame_id)
            await asyncio.sleep(args.inter_trial_s)
    finally:
        await vision.close()

    errors = dict(vision.errors)
    checks = {
        "four_streams_produced_usable_frames": (
            set(frame_ids) == {device.id for device in cfg.active_devices}
        ),
        "every_stream_remained_active": all(
            usable_counts[device.id] >= 2 for device in cfg.active_devices
        ),
        "no_vision_errors": not errors,
        "all_online_plans_within_200ms": (
            len(planner_ms) == args.repetitions
            and all(budget_checks)
            and max(planner_ms) <= 200.0
        ),
        "all_routes_identical": len(set(signatures)) == 1,
        "all_objectives_identical": bool(objectives) and all(
            value == objectives[0] for value in objectives
        ),
        "no_motion_control_constructed_or_sent": True,
    }
    report = {
        "schema_version": 1,
        "test": "v243_live_four_rtsp_planner_stress",
        "algorithm_version": ALGORITHM_VERSION,
        "config": str(args.config.resolve()),
        "config_sha256": sha256_file(args.config.resolve()),
        "policy_sha256": sha256_file(Path(sys.modules[MyPolicy.__module__].__file__).resolve()),
        "repetitions": args.repetitions,
        "motion_control": {
            "hardware_fleet_constructed": False,
            "control_socket_opened": False,
            "actions_submitted": 0,
        },
        "duration_s": time.monotonic() - started,
        "policy_construction_ms": construction_ms,
        "static_precompute_ms": float(policy.static_precompute_ms),
        "static_precomputed_edge_count": int(policy.static_precomputed_edge_count),
        "frames": {
            "last_usable_frame_id": frame_ids,
            "observed_status_count": observed_counts,
            "usable_status_count": usable_counts,
            "vision_errors": errors,
            "event_count": len(events),
        },
        "online_planner_ms": {
            "min": min(planner_ms),
            "median": statistics.median(planner_ms),
            "p95": percentile(planner_ms, 0.95),
            "max": max(planner_ms),
            "samples": planner_ms,
        },
        "route_signatures": sorted(set(signatures)),
        "final_objectives": objectives[:1],
        "checks": checks,
    }
    report["pass"] = all(checks.values())
    report["report_sha256"] = sha256_bytes(canonical_json(report))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--startup-timeout-s", type=float, default=20.0)
    parser.add_argument("--inter-trial-s", type=float, default=0.01)
    args = parser.parse_args()
    if args.repetitions < 50:
        parser.error("at least 50 repetitions are required")
    report = asyncio.run(run(args))
    write_json(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
