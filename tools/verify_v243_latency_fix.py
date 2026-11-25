#!/usr/bin/env python3
"""Verify V2.4.3 result equivalence and benchmark its initial planner.

The V2.4.2 policy is loaded from the preserved experiment-platform copy.  This
tool never opens a socket or issues an action.  It compares deterministic
planning outputs under the frozen repair-ablation configuration, then measures
fresh-policy construction and online planning separately across repeated runs.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import statistics
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algorithms import user_policy as v243  # noqa: E402
from common import canonical_json, sha256_bytes, sha256_file, write_json  # noqa: E402
from zr10lab.config import load_config  # noqa: E402


EXPECTED_V242_SHA256 = "7af5df1766dda38654e696703ff6e92226315fe5de28307b8e50a3ee279e92cb"
RUNTIME_AUDIT_FIELDS = {
    "observed_planner_ms",
    "budget_contract_satisfied",
    "initial_edge_cache_prewarmed",
    "static_precomputed_edge_count",
}


def load_module(path: Path) -> ModuleType:
    name = "preserved_user_policy_v242"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def percentile(values: list[float], probability: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def context_for(policy: Any) -> SimpleNamespace:
    return SimpleNamespace(
        t=100.0,
        dt=0.1,
        step=0,
        devices={},
        detections=(),
        rays=(),
        tracks=(),
        metrics={},
        frames={},
        command_results={},
    )


def candidate_signature(policy: Any) -> list[dict[str, Any]]:
    rows = []
    for candidate_id in sorted(policy.candidates):
        candidate = policy.candidates[candidate_id]
        rows.append({
            "candidate_id": candidate.candidate_id,
            "device_id": candidate.device_id,
            "yaw_deg": candidate.yaw_deg,
            "pitch_deg": candidate.pitch_deg,
            "coverage": sorted(candidate.coverage),
            "nominal_coverage": sorted(candidate.nominal_coverage),
        })
    return rows


def normalized_audit(audit: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(audit)
    for key in RUNTIME_AUDIT_FIELDS:
        result.pop(key, None)
    return result


def route_signature(routes: dict[str, list[str]]) -> str:
    normalized = {key: list(routes[key]) for key in sorted(routes)}
    return sha256_bytes(canonical_json(normalized))


def plan_once(policy: Any) -> tuple[dict[str, list[str]], dict[str, Any], float]:
    routes = policy._make_plan(context_for(policy), set(policy.reachable))
    return (
        {key: list(value) for key, value in routes.items()},
        copy.deepcopy(policy.last_plan_audit),
        float(policy.last_planner_ms),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--v242-policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=50)
    args = parser.parse_args()
    if args.repetitions < 20:
        parser.error("at least 20 repetitions are required")

    config_path = args.config.resolve()
    old_path = args.v242_policy.resolve()
    old_hash = sha256_file(old_path)
    if old_hash != EXPECTED_V242_SHA256:
        parser.error(
            "preserved V2.4.2 policy hash mismatch: "
            f"expected {EXPECTED_V242_SHA256}, got {old_hash}"
        )
    v242 = load_module(old_path)
    if str(v242.ALGORITHM_VERSION) != "2.4.2":
        parser.error(f"preserved policy reports version {v242.ALGORITHM_VERSION!r}")
    if str(v243.ALGORITHM_VERSION) != "2.4.3":
        parser.error(f"candidate policy reports version {v243.ALGORITHM_VERSION!r}")

    cfg = load_config(config_path)
    # Remove deadline sensitivity only for the exact deterministic equivalence
    # comparison.  The repeated benchmark below restores the formal 200 ms
    # budget from disk and audits that contract independently.
    old_cfg = copy.deepcopy(cfg)
    new_cfg = copy.deepcopy(cfg)
    old_cfg.policy["initial_planner_budget_ms"] = 5000.0
    new_cfg.policy["initial_planner_budget_ms"] = 5000.0
    old_policy = v242.MyPolicy(old_cfg)
    new_policy = v243.MyPolicy(new_cfg)
    old_routes, old_audit, _ = plan_once(old_policy)
    new_routes, new_audit, _ = plan_once(new_policy)
    equivalence_checks = {
        "candidate_geometry_identical": (
            candidate_signature(old_policy) == candidate_signature(new_policy)
        ),
        "reachable_cells_identical": (
            sorted(old_policy.reachable) == sorted(new_policy.reachable)
        ),
        "routes_identical": old_routes == new_routes,
        "route_signature_identical": (
            route_signature(old_routes) == route_signature(new_routes)
        ),
        "planning_audit_identical_excluding_runtime_fields": (
            normalized_audit(old_audit) == normalized_audit(new_audit)
        ),
    }

    construction_ms: list[float] = []
    precompute_ms: list[float] = []
    planner_ms: list[float] = []
    route_signatures: list[str] = []
    objectives: list[dict[str, Any]] = []
    budget_checks: list[bool] = []
    edge_counts: list[int] = []
    for _ in range(args.repetitions):
        fresh_cfg = load_config(config_path)
        started = time.perf_counter()
        policy = v243.MyPolicy(fresh_cfg)
        construction_ms.append((time.perf_counter() - started) * 1000.0)
        routes, audit, elapsed_ms = plan_once(policy)
        precompute_ms.append(float(policy.static_precompute_ms))
        planner_ms.append(elapsed_ms)
        route_signatures.append(route_signature(routes))
        objectives.append(copy.deepcopy(audit.get("final_objective", {})))
        budget_checks.append(bool(audit.get("budget_contract_satisfied")))
        edge_counts.append(int(policy.static_precomputed_edge_count))

    online_budget_ms = float(load_config(config_path).policy["initial_planner_budget_ms"])
    benchmark_checks = {
        "all_online_plans_within_budget": all(budget_checks),
        "all_routes_identical": len(set(route_signatures)) == 1,
        "all_final_objectives_identical": all(
            objective == objectives[0] for objective in objectives
        ),
        "all_static_edge_counts_positive_and_identical": (
            bool(edge_counts) and min(edge_counts) > 0 and len(set(edge_counts)) == 1
        ),
    }
    report = {
        "schema_version": 1,
        "verification": "v243_static_edge_prewarm",
        "inputs": {
            "config": str(config_path),
            "config_sha256": sha256_file(config_path),
            "preserved_v242_policy": str(old_path),
            "preserved_v242_policy_sha256": old_hash,
            "candidate_v243_policy": str(Path(v243.__file__).resolve()),
            "candidate_v243_policy_sha256": sha256_file(Path(v243.__file__).resolve()),
            "repetitions": args.repetitions,
        },
        "versions": {
            "reference": str(v242.ALGORITHM_VERSION),
            "candidate": str(v243.ALGORITHM_VERSION),
        },
        "equivalence": {
            "checks": equivalence_checks,
            "reference_route_signature": route_signature(old_routes),
            "candidate_route_signature": route_signature(new_routes),
            "reference_final_objective": old_audit.get("final_objective"),
            "candidate_final_objective": new_audit.get("final_objective"),
            "reference_audit": normalized_audit(old_audit),
            "candidate_audit": normalized_audit(new_audit),
        },
        "benchmark": {
            "online_budget_ms": online_budget_ms,
            "checks": benchmark_checks,
            "policy_construction_ms": {
                "min": min(construction_ms),
                "median": statistics.median(construction_ms),
                "p95": percentile(construction_ms, 0.95),
                "max": max(construction_ms),
            },
            "static_precompute_ms": {
                "min": min(precompute_ms),
                "median": statistics.median(precompute_ms),
                "p95": percentile(precompute_ms, 0.95),
                "max": max(precompute_ms),
            },
            "online_planner_ms": {
                "min": min(planner_ms),
                "median": statistics.median(planner_ms),
                "p95": percentile(planner_ms, 0.95),
                "max": max(planner_ms),
            },
            "online_planner_samples_ms": planner_ms,
            "route_signatures": sorted(set(route_signatures)),
            "static_precomputed_edge_counts": sorted(set(edge_counts)),
        },
    }
    report["pass"] = all(equivalence_checks.values()) and all(benchmark_checks.values())
    report["report_sha256"] = sha256_bytes(canonical_json(report))
    write_json(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
