#!/usr/bin/env python3
"""Generate a randomized complete-block, paired 5 x 3 HIL study plan.

The script writes configuration overlays only.  It never imports the hardware
SDK, opens a network connection, or launches an experiment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import (
    CONDITIONS, METHODS, PROTOCOL_VERSION, config_sha256, deep_merge,
    hardware_config_sha256, load_yaml_with_inheritance, pair_frozen_config_sha256,
    write_csv, write_json,
)


FIELDS = [
    "schema_version", "protocol_version", "plan_id", "plan_run_id", "run_index",
    "block", "pair_id", "within_pair_order", "method", "condition",
    "instance_seed", "injection_seed", "solver_seed",
    "condition_parameters_json", "expected_config_sha256",
    "expected_hardware_config_sha256", "expected_pair_frozen_sha256",
    "expected_evaluation_cell_count",
    "overlay_path", "cooldown_s", "status",
]


def seed_for(seed: int, *parts: str) -> int:
    digest = hashlib.sha256((str(seed) + "|" + "|".join(parts)).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def counterbalanced_order(items: tuple[str, ...], seed: int, block: int,
                          label: str) -> list[str]:
    """Randomised cyclic blocks with per-position counts differing by at most one."""
    cycle, shift = divmod(block - 1, len(items))
    base = list(items)
    random.Random(seed_for(seed, label, str(cycle))).shuffle(base)
    if cycle % 2:
        base.reverse()
    return base[shift:] + base[:shift]


def condition_parameters(condition: str, block: int, injection_seed: int) -> dict[str, Any]:
    slot = (block - 1) % 4
    if condition == "nominal":
        return {"kind": "none", "enabled": False}
    if condition == "heterogeneous_delay":
        # Policy-side confirmation gating only.  The low-level command loop and
        # watchdog are never slept, paused, or disconnected by this protocol.
        profiles = [0.0, 0.20, 0.45, 0.75]
        rotated = profiles[slot:] + profiles[:slot]
        return {
            "kind": "confirmation_delay",
            "enabled": True,
            "implementation": "policy_event_clock_only",
            "delay_s_by_device_slot": rotated,
            "never_block_watchdog": True,
            "seed": injection_seed,
        }
    if condition == "pause":
        return {
            "kind": "logical_availability_pause",
            "enabled": True,
            "implementation": "policy_availability_mask",
            "device_slot": slot,
            "start_after_mission_s": 15.0,
            "duration_s": 8.0,
            "last_command_expires_by_ttl": True,
            "no_sdk_fault_injection": True,
            "seed": injection_seed,
        }
    raise ValueError(condition)


def evaluation_cell_count(config: dict[str, Any]) -> int:
    grid = config.get("policy", {}).get("evaluation_grid", {})
    shape = grid.get("shape") if isinstance(grid, dict) else None
    declared = grid.get("cell_count") if isinstance(grid, dict) else None
    if not (isinstance(shape, list) and len(shape) == 3 and
            all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in shape)):
        raise ValueError("policy.evaluation_grid.shape must contain three positive integers")
    computed = math.prod(shape)
    if declared != computed:
        raise ValueError(f"evaluation_grid.cell_count={declared!r} does not match product(shape)={computed}")
    if computed != 384:
        raise ValueError(f"confirmatory evaluation grid is preregistered at M=384; got {computed}")
    alias = config.get("policy", {}).get("scan_grid")
    if alias is not None and alias != shape:
        raise ValueError("deprecated scan_grid alias must equal evaluation_grid.shape")
    return computed


def make_rows(base: dict[str, Any], blocks: int, seed: int, cooldown_s: float,
              overlay_dir: Path | None, overlay_extends: str | None) -> tuple[str, list[dict[str, Any]]]:
    plan_id = f"ralhil-{seed}-{blocks}b"
    hardware_hash = hardware_config_sha256(base)
    evaluation_m = evaluation_cell_count(base)
    rows: list[dict[str, Any]] = []
    run_index = 0
    for block in range(1, blocks + 1):
        conditions = counterbalanced_order(CONDITIONS, seed, block, "condition-order")
        for condition in conditions:
            pair_id = f"B{block:02d}-{condition}"
            instance_seed = seed_for(seed, str(block), condition, "instance")
            injection_seed = seed_for(seed, str(block), condition, "injection")
            perturbation = condition_parameters(condition, block, injection_seed)
            methods = counterbalanced_order(METHODS, seed, block, f"method-order|{condition}")
            for within_pair_order, method in enumerate(methods, 1):
                run_index += 1
                solver_seed = seed_for(seed, str(block), condition, method, "solver")
                plan_run_id = f"{plan_id}-R{run_index:03d}"
                experiment = {
                    "protocol_version": PROTOCOL_VERSION,
                    "plan_id": plan_id,
                    "plan_run_id": plan_run_id,
                    "run_index": run_index,
                    "block": block,
                    "pair_id": pair_id,
                    "method": method,
                    "condition": condition,
                    "instance_seed": instance_seed,
                    "injection_seed": injection_seed,
                    "solver_seed": solver_seed,
                }
                override = {
                    "policy": {
                        "method": method,
                        "experiment": experiment,
                        "fault_injection": perturbation,
                    }
                }
                resolved = deep_merge(base, override)
                expected_hash = config_sha256(resolved)
                pair_frozen_hash = pair_frozen_config_sha256(resolved)
                overlay_path = ""
                if overlay_dir is not None:
                    try:
                        import yaml
                    except ImportError as exc:  # pragma: no cover
                        raise RuntimeError("PyYAML is required to materialize run overlays") from exc
                    overlay_dir.mkdir(parents=True, exist_ok=True)
                    target = overlay_dir / f"{run_index:03d}_{method}_{condition}.yaml"
                    if not overlay_extends:
                        raise ValueError("--overlay-extends is required with --overlay-dir")
                    document = {"extends": overlay_extends, **override}
                    target.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
                    overlay_path = str(target.resolve())
                rows.append({
                    "schema_version": 1,
                    "protocol_version": PROTOCOL_VERSION,
                    "plan_id": plan_id,
                    "plan_run_id": plan_run_id,
                    "run_index": run_index,
                    "block": block,
                    "pair_id": pair_id,
                    "within_pair_order": within_pair_order,
                    "method": method,
                    "condition": condition,
                    "instance_seed": instance_seed,
                    "injection_seed": injection_seed,
                    "solver_seed": solver_seed,
                    "condition_parameters_json": json.dumps(perturbation, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    "expected_config_sha256": expected_hash,
                    "expected_hardware_config_sha256": hardware_hash,
                    "expected_pair_frozen_sha256": pair_frozen_hash,
                    "expected_evaluation_cell_count": evaluation_m,
                    "overlay_path": overlay_path,
                    "cooldown_s": cooldown_s,
                    "status": "planned",
                })
    return plan_id, rows


def validate_balance(rows: list[dict[str, Any]], blocks: int) -> None:
    expected = blocks * len(METHODS) * len(CONDITIONS)
    if len(rows) != expected:
        raise AssertionError(f"row count {len(rows)} != {expected}")
    keys = {(row["block"], row["method"], row["condition"]) for row in rows}
    if len(keys) != expected:
        raise AssertionError("design is not one observation per block x method x condition cell")
    for block in range(1, blocks + 1):
        for condition in CONDITIONS:
            subset = [row for row in rows if row["block"] == block and row["condition"] == condition]
            if {row["method"] for row in subset} != set(METHODS):
                raise AssertionError(f"incomplete pair block B{block:02d}-{condition}")
            for key in ("instance_seed", "injection_seed", "condition_parameters_json",
                        "expected_pair_frozen_sha256"):
                if len({str(row[key]) for row in subset}) != 1:
                    raise AssertionError(f"paired task/injection field {key} differs in B{block:02d}-{condition}")
            if len({str(row["solver_seed"]) for row in subset}) != len(METHODS):
                raise AssertionError(f"solver seeds are not method-specific in B{block:02d}-{condition}")
            if len({str(row["expected_config_sha256"]) for row in subset}) != len(METHODS):
                raise AssertionError(f"method-specific full config hashes are not unique in B{block:02d}-{condition}")
    for condition in CONDITIONS:
        for method in METHODS:
            counts = [sum(row["condition"] == condition and row["method"] == method and
                          int(row["within_pair_order"]) == position for row in rows)
                      for position in range(1, len(METHODS) + 1)]
            if max(counts) - min(counts) > 1:
                raise AssertionError(f"within-pair order is imbalanced for {condition}/{method}: {counts}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Resolved base overlay/template")
    parser.add_argument("--output-prefix", required=True, type=Path,
                        help="Path prefix; .csv and .json are appended")
    parser.add_argument("--blocks", type=int, default=8,
                        help="Complete repeats; 8 gives 120 independent runs (6/90 is pilot minimum)")
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--cooldown-s", type=float, default=15.0)
    parser.add_argument("--overlay-dir", type=Path,
                        help="Optional directory for per-run YAML overlays")
    parser.add_argument("--overlay-extends",
                        help="Literal extends value for generated overlays (normally private_site_overlay.yaml)")
    args = parser.parse_args()
    if args.blocks < 6:
        parser.error("--blocks must be at least 6 (90 runs); 8 blocks/120 runs is recommended")
    if args.cooldown_s < 0:
        parser.error("--cooldown-s cannot be negative")
    base = load_yaml_with_inheritance(args.config)
    plan_id, rows = make_rows(base, args.blocks, args.seed, args.cooldown_s,
                              args.overlay_dir.resolve() if args.overlay_dir else None,
                              args.overlay_extends)
    validate_balance(rows, args.blocks)
    csv_path = args.output_prefix.with_suffix(".csv").resolve()
    json_path = args.output_prefix.with_suffix(".json").resolve()
    write_csv(csv_path, rows, FIELDS)
    payload = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "plan_id": plan_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "plan_randomization_seed": args.seed,
        "seed_roles": {
            "instance_seed": "shared by all methods in one pair; freezes the task instance",
            "injection_seed": "shared by all methods in one pair; freezes policy-only perturbation",
            "solver_seed": "method-specific; affects solver tie-breaking only",
        },
        "blocks": args.blocks,
        "methods": list(METHODS),
        "conditions": list(CONDITIONS),
        "run_count": len(rows),
        "independent_unit": "one completed hardware run",
        "pairing_key": "pair_id (same block and condition, all five methods)",
        "primary_continuous_outcome": "uac_h_s at the preregistered fixed horizon",
        "primary_binary_outcomes": ["t95_reached", "t100_reached"],
        "censored_completion_times": ["t95_s", "t100_s"],
        "condition_safety": "policy-only injection; no SDK/network/process fault injection",
        "evaluation_cell_count": evaluation_cell_count(base),
        "inference_note": ("effect sizes and paired confidence intervals are primary; eight blocks permit, but do not guarantee, "
                           "small exact paired p-values after four-comparison Holm correction"),
        "rows": rows,
    }
    write_json(json_path, payload)
    print(json.dumps({"plan_id": plan_id, "rows": len(rows), "csv": str(csv_path),
                      "json": str(json_path), "seed": args.seed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

