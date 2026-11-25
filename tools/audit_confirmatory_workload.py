"""Estimate the preregistered common deadline from the frozen policy/model.

No socket or SDK is imported.  Each strategy is planned from the common local
home.  Every selected route is evaluated with the reviewed device-specific
upper completion-time model, irrespective of the cost model used to construct
that route.  If the online planner intentionally emits a budget-limited chunk,
the audit advances the synthetic pose and repeats until all cells are assigned.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algorithms.user_policy import MyPolicy
from common import load_yaml_with_inheritance
from zr10lab.config import config_from_dict
from zr10lab.models import Telemetry


METHODS = ("raster", "mag", "open_loop_cml", "measured_static", "full_eacml")


def context_for(policy: MyPolicy, poses: dict[str, tuple[float, float]], t: float):
    states = {
        key: Telemetry(
            key, t, pose[0], pose[1], zoom=policy.fixed_zoom[key],
            connected=True, sequence=1, source="frozen_workload_audit",
            raw={"zoom_known": True, "zoom_stable": True},
        )
        for key, pose in poses.items()
    }
    frames = {
        key: SimpleNamespace(
            device_id=key, frame_id=1, capture_t=t, received_t=t,
            width=1920, height=1080, usable=True, resolution_ok=True,
            mean_luma=100.0, contrast=20.0, sharpness=10.0,
        )
        for key in poses
    }
    return SimpleNamespace(
        t=t, dt=0.1, step=0, devices=states, frames=frames,
        command_results={}, detections=(), rays=(), tracks=(), metrics={},
    )


def audit_method(config_path: Path, method: str,
                 roi: dict[str, list[float]] | None = None) -> dict:
    raw = load_yaml_with_inheritance(config_path)
    # Offline planning does not use the UI mode catalog.  Removing it also
    # keeps this audit compatible with the immutable pre-patch Kit snapshot.
    raw["control_center"] = {}
    cfg = config_from_dict(raw)
    if roi is not None:
        cfg.policy["roi"] = roi
    cfg.policy["method"] = method
    cfg.policy["fault_injection"] = {"kind": "none", "enabled": False}
    policy = MyPolicy(cfg)
    poses = {
        device.id: (float(device.initial_yaw_deg), float(device.initial_pitch_deg))
        for device in cfg.active_devices
    }
    residual = set(policy.reachable)
    routes = {key: [] for key in policy.device_ids}
    chunk_count = 0
    planning_ms = 0.0
    while residual:
        chunk_count += 1
        if chunk_count > 1000:
            raise RuntimeError(f"{method}: planner did not exhaust residual")
        context = context_for(policy, poses, 100.0 + chunk_count)
        started = time.perf_counter()
        chunk = policy._make_plan(context, residual)  # exact deployed planner
        planning_ms += (time.perf_counter() - started) * 1000.0
        selected = [candidate_id for route in chunk.values() for candidate_id in route]
        if not selected:
            raise RuntimeError(f"{method}: empty planner chunk with {len(residual)} residual cells")
        before = len(residual)
        for device_id in policy.device_ids:
            for candidate_id in chunk[device_id]:
                candidate = policy.candidates[candidate_id]
                routes[device_id].append(candidate_id)
                residual.difference_update(candidate.coverage)
                poses[device_id] = (candidate.yaw_deg, candidate.pitch_deg)
        if len(residual) >= before:
            raise RuntimeError(f"{method}: planner chunk made no coverage progress")

    loads: dict[str, float] = {}
    task_counts: dict[str, int] = {}
    for device_id in policy.device_ids:
        pose = (
            float(policy.devices[device_id].initial_yaw_deg),
            float(policy.devices[device_id].initial_pitch_deg),
        )
        total = 0.0
        for candidate_id in routes[device_id]:
            candidate = policy.candidates[candidate_id]
            edge, _ = policy._measured_edge(
                device_id, pose, (candidate.yaw_deg, candidate.pitch_deg)
            )
            total += edge
            pose = (candidate.yaw_deg, candidate.pitch_deg)
        loads[device_id] = total
        task_counts[device_id] = len(routes[device_id])
    return {
        "method": method,
        "evaluation_cell_count": len(policy.cells),
        "reachable_count": len(policy.reachable),
        "candidate_count": len(policy.candidates),
        "planner_chunks": chunk_count,
        "offline_planning_ms": planning_ms,
        "task_count_by_device": task_counts,
        "upper_workload_s_by_device": loads,
        "predicted_upper_makespan_s": max(loads.values(), default=0.0),
        # For V2 this is the exact, machine-readable MAG-incumbent audit from
        # the deployed planner: robust UAC/T95/T100 before and after local
        # search, accepted operations, rejected operations, and both
        # non-regression checks.  Other methods legitimately leave it empty.
        "plan_audit": policy.last_plan_audit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--safety-factor", type=float, default=1.5)
    parser.add_argument("--round-to-s", type=float, default=30.0)
    parser.add_argument("--methods", nargs="+", default=list(METHODS),
                        choices=(*METHODS, "uac_robust_repair_v2"))
    parser.add_argument(
        "--roi", type=float, nargs=6, metavar=("X0", "X1", "Y0", "Y1", "Z0", "Z1"),
        help="Optional workload-only ROI override",
    )
    args = parser.parse_args()
    if args.safety_factor < 1.0 or args.round_to_s <= 0:
        parser.error("invalid deadline rule")
    roi = None
    if args.roi:
        roi = {
            "x": [args.roi[0], args.roi[1]],
            "y": [args.roi[2], args.roi[3]],
            "z": [args.roi[4], args.roi[5]],
        }
    rows = [audit_method(args.config.resolve(), method, roi) for method in args.methods]
    maximum = max(row["predicted_upper_makespan_s"] for row in rows)
    deadline = math.ceil(maximum * args.safety_factor / args.round_to_s) * args.round_to_s
    payload = {
        "schema": "ral-hil-confirmatory-workload-audit-v1",
        "config": str(args.config.resolve()),
        "roi_override": roi,
        "generated_utc_s": time.time(),
        "deadline_rule": (
            "ceil(max_method_device_upper_workload * safety_factor / round_to_s) * round_to_s"
        ),
        "safety_factor": args.safety_factor,
        "round_to_s": args.round_to_s,
        "maximum_predicted_upper_makespan_s": maximum,
        "frozen_horizon_s": deadline,
        "methods": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
