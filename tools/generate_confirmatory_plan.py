"""Generate the frozen 42-run RA-L confirmatory hardware plan.

The design contains 30 nominal runs (five methods x six temporal blocks) and
12 logical-pause runs (Measured-CML-H and EA-CML-H x six blocks).  Generation
is file-only; it never connects to a device or authorizes motion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algorithms.user_policy import ALGORITHM_VERSION

from common import (
    config_sha256, deep_merge, hardware_config_sha256,
    load_yaml_with_inheritance, pair_frozen_config_sha256, sha256_file, write_csv,
    write_json,
)


PROTOCOL = "ral-hil-1"
DESIGN = "confirmatory-30-nominal-plus-12-pause-v1"
NOMINAL_METHODS = ("raster", "mag", "open_loop_cml", "measured_static", "full_eacml")
PAUSE_METHODS = ("measured_static", "full_eacml")
FIELDS = [
    "schema_version", "protocol_version", "design_id", "plan_id", "plan_run_id",
    "run_index", "block", "pair_id", "within_pair_order", "method", "condition",
    "instance_seed", "injection_seed", "solver_seed", "horizon_s",
    "runtime_duration_s",
    "condition_parameters_json", "expected_config_sha256",
    "expected_hardware_config_sha256", "expected_pair_frozen_sha256",
    "expected_evaluation_cell_count", "expected_policy_sha256",
    "expected_policy_version", "overlay_path", "cooldown_s", "status",
]


def seed_for(seed: int, *parts: str) -> int:
    digest = hashlib.sha256((str(seed) + "|" + "|".join(parts)).encode()).digest()
    return int.from_bytes(digest[:4], "big")


def nominal_order(seed: int, block: int) -> list[str]:
    base = list(NOMINAL_METHODS)
    random.Random(seed_for(seed, "nominal-base")).shuffle(base)
    shift = (block - 1) % len(base)
    order = base[shift:] + base[:shift]
    if block > len(base):
        order.reverse()
    return order


def add_pair(
    rows: list[dict[str, Any]], base: dict[str, Any], overlay_dir: Path,
    overlay_extends: str, plan_id: str, block: int, condition: str,
    methods: tuple[str, ...], order: list[str], seed: int, horizon_s: float,
    runtime_duration_s: float, cooldown_s: float,
) -> None:
    policy_sha256 = sha256_file(ROOT / "algorithms" / "user_policy.py")
    instance_seed = 0  # the same frozen physical task is repeated in all temporal blocks
    injection_seed = seed_for(seed, str(block), condition, "injection") if condition == "pause" else 0
    if condition == "pause":
        perturbation = {
            "kind": "logical_availability_pause",
            "enabled": True,
            "implementation": "policy_availability_mask",
            "device_slot": (block - 1) % 4,
            "start_after_mission_s": 15.0,
            "duration_s": 8.0,
            "last_command_expires_by_ttl": True,
            "no_sdk_fault_injection": True,
            "seed": injection_seed,
        }
    else:
        perturbation = {"kind": "none", "enabled": False}
    pair_id = f"B{block:02d}-{condition}"
    for within_order, method in enumerate(order, 1):
        if method not in methods:
            raise AssertionError(f"unexpected {condition} method {method}")
        run_index = len(rows) + 1
        plan_run_id = f"{plan_id}-R{run_index:03d}"
        experiment = {
            "protocol_version": PROTOCOL,
            "design_id": DESIGN,
            "plan_id": plan_id,
            "plan_run_id": plan_run_id,
            "run_index": run_index,
            "block": block,
            "pair_id": pair_id,
            "method": method,
            "condition": condition,
            "instance_seed": instance_seed,
            "injection_seed": injection_seed,
            "solver_seed": 0,
            "horizon_s": horizon_s,
        }
        override = {
            "system": {"duration_s": runtime_duration_s},
            "policy": {
                "method": method,
                "experiment": experiment,
                "fault_injection": perturbation,
            },
        }
        resolved = deep_merge(base, override)
        target = overlay_dir / f"{run_index:03d}_{method}_{condition}.yaml"
        import yaml
        target.write_text(yaml.safe_dump(
            {"extends": overlay_extends, **override}, allow_unicode=True, sort_keys=False,
        ), encoding="utf-8")
        rows.append({
            "schema_version": 1,
            "protocol_version": PROTOCOL,
            "design_id": DESIGN,
            "plan_id": plan_id,
            "plan_run_id": plan_run_id,
            "run_index": run_index,
            "block": block,
            "pair_id": pair_id,
            "within_pair_order": within_order,
            "method": method,
            "condition": condition,
            "instance_seed": instance_seed,
            "injection_seed": injection_seed,
            "solver_seed": 0,
            "horizon_s": horizon_s,
            "runtime_duration_s": runtime_duration_s,
            "condition_parameters_json": json.dumps(
                perturbation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "expected_config_sha256": config_sha256(resolved),
            "expected_hardware_config_sha256": hardware_config_sha256(resolved),
            "expected_pair_frozen_sha256": pair_frozen_config_sha256(resolved),
            "expected_evaluation_cell_count": 384,
            "expected_policy_sha256": policy_sha256,
            "expected_policy_version": ALGORITHM_VERSION,
            "overlay_path": str(target.resolve()),
            "cooldown_s": cooldown_s,
            "status": "planned",
        })


def validate(rows: list[dict[str, Any]], horizon_s: float) -> None:
    if len(rows) != 42:
        raise AssertionError(f"expected 42 rows, got {len(rows)}")
    for block in range(1, 7):
        nominal = [row for row in rows if row["block"] == block and row["condition"] == "nominal"]
        pause = [row for row in rows if row["block"] == block and row["condition"] == "pause"]
        if {row["method"] for row in nominal} != set(NOMINAL_METHODS) or len(nominal) != 5:
            raise AssertionError(f"block {block} nominal pair is incomplete")
        if {row["method"] for row in pause} != set(PAUSE_METHODS) or len(pause) != 2:
            raise AssertionError(f"block {block} pause pair is incomplete")
        for items in (nominal, pause):
            for key in (
                "instance_seed", "injection_seed", "condition_parameters_json",
                "expected_hardware_config_sha256", "expected_pair_frozen_sha256",
                "horizon_s", "runtime_duration_s", "expected_policy_sha256",
                "expected_policy_version",
            ):
                if len({str(row[key]) for row in items}) != 1:
                    raise AssertionError(f"block {block}: paired field differs: {key}")
    if {float(row["horizon_s"]) for row in rows} != {horizon_s}:
        raise AssertionError("horizon is not common and frozen")
    for method in NOMINAL_METHODS:
        positions = [int(row["within_pair_order"]) for row in rows
                     if row["condition"] == "nominal" and row["method"] == method]
        counts = [positions.count(position) for position in range(1, 6)]
        if max(counts) - min(counts) > 1:
            raise AssertionError(f"nominal order imbalance for {method}: {counts}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-prefix", required=True, type=Path)
    parser.add_argument("--overlay-dir", required=True, type=Path)
    parser.add_argument("--overlay-extends", required=True)
    parser.add_argument("--workload-audit", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=20261019)
    parser.add_argument("--cooldown-s", type=float, default=10.0)
    args = parser.parse_args()
    audit = json.loads(args.workload_audit.read_text(encoding="utf-8"))
    horizon_s = float(audit["frozen_horizon_s"])
    if horizon_s <= 0:
        parser.error("workload audit has no positive frozen horizon")
    base = load_yaml_with_inheritance(args.config.resolve())
    configured_h = float(base.get("policy", {}).get("experiment", {}).get("horizon_s", 0))
    system_h = float(base.get("system", {}).get("duration_s", 0))
    if configured_h != horizon_s or system_h < horizon_s:
        parser.error(
            f"base horizon/runtime ({configured_h}/{system_h}) is incompatible with audit H={horizon_s}"
        )
    plan_id = f"ralhil42-{args.seed}"
    overlay_dir = args.overlay_dir.resolve()
    overlay_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for block in range(1, 7):
        add_pair(
            rows, base, overlay_dir, args.overlay_extends, plan_id, block,
            "nominal", NOMINAL_METHODS, nominal_order(args.seed, block), args.seed,
            horizon_s, system_h, args.cooldown_s,
        )
        pause_order = list(PAUSE_METHODS if block % 2 else reversed(PAUSE_METHODS))
        add_pair(
            rows, base, overlay_dir, args.overlay_extends, plan_id, block,
            "pause", PAUSE_METHODS, pause_order, args.seed, horizon_s, system_h,
            args.cooldown_s,
        )
    validate(rows, horizon_s)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_prefix.with_suffix(".csv").resolve()
    json_path = args.output_prefix.with_suffix(".json").resolve()
    write_csv(csv_path, rows, FIELDS)
    payload = {
        "schema_version": 1,
        "protocol_version": PROTOCOL,
        "design_id": DESIGN,
        "plan_id": plan_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "plan_randomization_seed": args.seed,
        "workload_audit": str(args.workload_audit.resolve()),
        "frozen_horizon_s": horizon_s,
        "runtime_duration_s": system_h,
        "expected_policy_sha256": sha256_file(ROOT / "algorithms" / "user_policy.py"),
        "expected_policy_version": ALGORITHM_VERSION,
        "blocks": 6,
        "nominal_methods": list(NOMINAL_METHODS),
        "pause_methods": list(PAUSE_METHODS),
        "run_count": len(rows),
        "independent_unit": "one complete hardware run",
        "rows": rows,
    }
    write_json(json_path, payload)
    print(json.dumps({
        "plan_id": plan_id, "rows": len(rows), "horizon_s": horizon_s,
        "csv": str(csv_path), "json": str(json_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
