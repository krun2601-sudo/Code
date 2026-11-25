#!/usr/bin/env python3
"""Generate the frozen 50-run V2 real-hardware confirmatory plan.

This command only writes plan and YAML overlay files.  It never imports an SDK,
opens a network connection, or authorizes device motion.  Run it only after the
policy, execution model, base configuration, and analysis code are frozen.
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algorithms.user_policy import ALGORITHM_VERSION
from common import (
    canonical_json, config_sha256, deep_merge, hardware_config_sha256,
    load_yaml_with_inheritance, pair_frozen_config_sha256, sha256_file,
    sha256_bytes, write_csv, write_json,
)


PROTOCOL = "ral-hil-2"
DESIGN = "confirmatory-v2-30-nominal-plus-20-pause"
PRIMARY_METHOD = "uac_robust_repair_v2"
NOMINAL_METHODS = (PRIMARY_METHOD, "mag", "measured_static")
PAUSE_METHODS = (PRIMARY_METHOD, "measured_static")
BLOCKS = 10
HORIZON_S = 60.0
FIELDS = [
    "schema_version", "protocol_version", "design_id", "plan_id", "plan_run_id",
    "run_index", "block", "pair_id", "within_pair_order", "method", "condition",
    "instance_seed", "injection_seed", "solver_seed", "horizon_s",
    "runtime_duration_s", "condition_parameters_json", "expected_config_sha256",
    "expected_hardware_config_sha256", "expected_pair_frozen_sha256",
    "expected_evaluation_cell_count", "expected_policy_sha256",
    "expected_policy_version", "expected_execution_model_sha256",
    "overlay_path", "cooldown_s", "status",
]


def frozen_software_hashes() -> dict[str, str]:
    return {
        "algorithms/user_policy.py": sha256_file(ROOT / "algorithms" / "user_policy.py"),
        "tools/analyze_runs.py": sha256_file(ROOT / "tools" / "analyze_runs.py"),
        "tools/preflight.py": sha256_file(ROOT / "tools" / "preflight.py"),
        "tools/prepare_run_context.py": sha256_file(ROOT / "tools" / "prepare_run_context.py"),
        "tools/run_confirmatory_batch.py": sha256_file(ROOT / "tools" / "run_confirmatory_batch.py"),
        "tools/generate_v2_confirmatory_plan.py": sha256_file(
            ROOT / "tools" / "generate_v2_confirmatory_plan.py"
        ),
        "tools/audit_v2_readiness.py": sha256_file(ROOT / "tools" / "audit_v2_readiness.py"),
        "platform_patch/patchset.py": sha256_file(ROOT / "platform_patch" / "patchset.py"),
    }


def seed_for(seed: int, *parts: str) -> int:
    digest = hashlib.sha256((str(seed) + "|" + "|".join(parts)).encode()).digest()
    return int.from_bytes(digest[:4], "big")


def balanced_order(methods: tuple[str, ...], seed: int, condition: str,
                   block: int) -> list[str]:
    """Use a randomized Williams-style cyclic order with balanced positions."""
    base = list(methods)
    random.Random(seed_for(seed, condition, "base-order")).shuffle(base)
    shift = (block - 1) % len(base)
    order = base[shift:] + base[:shift]
    if ((block - 1) // len(base)) % 2:
        order.reverse()
    return order


def execution_model_path(base: dict[str, Any], config_path: Path) -> Path:
    raw = str(base.get("policy", {}).get("execution_model", {}).get("model_path", "")).strip()
    if not raw:
        raise ValueError("policy.execution_model.model_path must be frozen before plan generation")
    path = Path(raw)
    if not path.is_absolute():
        path = config_path.parent / path
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"execution model not found: {path}")
    return path


def pause_condition(seed: int, block: int, duration_s: float) -> dict[str, Any]:
    return {
        "kind": "logical_availability_pause",
        "enabled": True,
        "implementation": "policy_availability_mask",
        "trigger": "coverage_fraction",
        "trigger_coverage_fraction": 0.50,
        "device_selection": "max_unique_reserved_coverage",
        "duration_s": duration_s,
        "last_command_expires_by_ttl": True,
        "no_sdk_fault_injection": True,
        "seed": seed_for(seed, str(block), "pause", "injection"),
    }


def add_pair(
    rows: list[dict[str, Any]], base: dict[str, Any], overlay_dir: Path,
    overlay_extends: str, plan_id: str, block: int, condition: str,
    methods: tuple[str, ...], order: list[str], seed: int,
    runtime_duration_s: float, cooldown_s: float, model_sha256: str,
    pause_duration_s: float,
    protocol: str = PROTOCOL, design: str = DESIGN,
) -> None:
    policy_sha256 = sha256_file(ROOT / "algorithms" / "user_policy.py")
    perturbation = pause_condition(seed, block, pause_duration_s) if condition == "pause" else {
        "kind": "none", "enabled": False,
    }
    injection_seed = int(perturbation.get("seed", 0))
    pair_id = f"B{block:02d}-{condition}"
    for within_order, method in enumerate(order, 1):
        if method not in methods:
            raise AssertionError(f"unexpected {condition} method {method}")
        run_index = len(rows) + 1
        plan_run_id = f"{plan_id}-R{run_index:03d}"
        experiment = {
            "protocol_version": protocol,
            "design_id": design,
            "plan_id": plan_id,
            "plan_run_id": plan_run_id,
            "run_index": run_index,
            "block": block,
            "pair_id": pair_id,
            "method": method,
            "condition": condition,
            "instance_seed": 0,
            "injection_seed": injection_seed,
            "solver_seed": 0,
            "horizon_s": HORIZON_S,
        }
        override = {
            "system": {"duration_s": runtime_duration_s},
            "policy": {
                "method": method,
                "execution_confirmation": {"measured_pose_projection": True},
                "robust_fov": {"enforce_for_all_methods": True},
                "robust_objective": {
                    "horizon_s": HORIZON_S,
                    "complete_mag_reference": True,
                },
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
            "schema_version": 2,
            "protocol_version": protocol,
            "design_id": design,
            "plan_id": plan_id,
            "plan_run_id": plan_run_id,
            "run_index": run_index,
            "block": block,
            "pair_id": pair_id,
            "within_pair_order": within_order,
            "method": method,
            "condition": condition,
            "instance_seed": 0,
            "injection_seed": injection_seed,
            "solver_seed": 0,
            "horizon_s": HORIZON_S,
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
            "expected_execution_model_sha256": model_sha256,
            "overlay_path": str(target.resolve()),
            "cooldown_s": cooldown_s,
            "status": "planned",
        })


def validate(rows: list[dict[str, Any]]) -> None:
    if len(rows) != 50:
        raise AssertionError(f"expected 50 rows, got {len(rows)}")
    for block in range(1, BLOCKS + 1):
        for condition, methods in (("nominal", NOMINAL_METHODS), ("pause", PAUSE_METHODS)):
            items = [row for row in rows
                     if row["block"] == block and row["condition"] == condition]
            if len(items) != len(methods) or {row["method"] for row in items} != set(methods):
                raise AssertionError(f"block {block} {condition} pair is incomplete")
            for key in (
                "instance_seed", "injection_seed", "solver_seed",
                "condition_parameters_json", "expected_hardware_config_sha256",
                "expected_pair_frozen_sha256", "horizon_s", "runtime_duration_s",
                "expected_policy_sha256", "expected_policy_version",
                "expected_execution_model_sha256",
            ):
                if len({str(row[key]) for row in items}) != 1:
                    raise AssertionError(f"block {block} {condition}: paired field differs: {key}")
    for condition, methods in (("nominal", NOMINAL_METHODS), ("pause", PAUSE_METHODS)):
        for method in methods:
            positions = [int(row["within_pair_order"]) for row in rows
                         if row["condition"] == condition and row["method"] == method]
            counts = [positions.count(position) for position in range(1, len(methods) + 1)]
            if max(counts) - min(counts) > 1:
                raise AssertionError(f"order imbalance for {condition}/{method}: {counts}")


def validate_development(rows: list[dict[str, Any]]) -> None:
    expected = {
        "nominal": set(NOMINAL_METHODS),
        "pause": set(PAUSE_METHODS),
        "repeatability": {PRIMARY_METHOD},
    }
    if len(rows) != 6:
        raise AssertionError(f"expected 6 development rows, got {len(rows)}")
    for condition, methods in expected.items():
        items = [row for row in rows if row["condition"] == condition]
        if len(items) != len(methods) or {row["method"] for row in items} != methods:
            raise AssertionError(f"development condition {condition} is incomplete")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-prefix", required=True, type=Path)
    parser.add_argument("--overlay-dir", required=True, type=Path)
    parser.add_argument("--overlay-extends", required=True)
    parser.add_argument("--seed", type=int, default=20261020)
    parser.add_argument("--cooldown-s", type=float, default=10.0)
    parser.add_argument("--runtime-duration-s", type=float, default=63.0)
    parser.add_argument("--pause-duration-s", type=float, default=8.0)
    parser.add_argument("--stage", choices=("confirmatory", "development"),
                        default="confirmatory")
    parser.add_argument("--readiness-report", type=Path,
                        help="Passing audit_v2_readiness.py report; required for confirmatory stage")
    args = parser.parse_args()
    if args.cooldown_s < 10.0:
        parser.error("confirmatory cooldown must be at least 10 s")
    if args.runtime_duration_s < HORIZON_S:
        parser.error("runtime duration must not be shorter than the fixed 60 s horizon")
    if args.pause_duration_s <= 0:
        parser.error("pause duration must be positive")

    config_path = args.config.resolve()
    base = load_yaml_with_inheritance(config_path)
    base_config_hash = config_sha256(base)
    software_hashes = frozen_software_hashes()
    if len([row for row in base.get("devices", []) if row.get("enabled", True)]) != 4:
        parser.error("V2 confirmatory design requires exactly four enabled devices")
    model_path = execution_model_path(base, config_path)
    model_sha256 = sha256_file(model_path)
    execution_options = base.get("policy", {}).get("execution_model", {})
    if execution_options.get("status") != "calibrated":
        parser.error("policy.execution_model.status must be calibrated before formal-plan generation")
    configured_hash = str(base.get("policy", {}).get("execution_model", {}).get("sha256", ""))
    if not configured_hash:
        parser.error("policy.execution_model.sha256 must freeze the reviewed model artifact")
    if configured_hash.lower() != model_sha256.lower():
        parser.error("configured execution_model.sha256 does not match model file")
    model_document = json.loads(model_path.read_text(encoding="utf-8"))
    if int(model_document.get("schema_version", 0)) < 2:
        parser.error("formal V2 planning requires the angle-regime execution-model schema")
    if "disjoint completed source runs" not in str(model_document.get("split_rule", "")):
        parser.error("execution model does not attest run-disjoint train/calibration/test data")
    model_rows = model_document.get("models", {})
    active_ids = {str(row.get("id")) for row in base.get("devices", []) if row.get("enabled", True)}
    if not isinstance(model_rows, dict) or set(model_rows) != active_ids:
        parser.error("execution model must contain exactly the four enabled device IDs")
    for device_id, row in model_rows.items():
        coefficients = row.get("coefficients", {}) if isinstance(row, dict) else {}
        required_coefficients = {
            "intercept", "yaw_positive_deg", "yaw_negative_deg",
            "pitch_up_deg", "pitch_down_deg", "dual_axis_parallel",
        }
        if not isinstance(coefficients, dict) or not required_coefficients <= set(coefficients):
            parser.error(f"{device_id}: execution model has incomplete coefficients")
        banded = row.get("one_sided_padding_s_by_regime", {})
        if not isinstance(banded, dict) or set(banded) != {"small", "medium", "large"}:
            parser.error(f"{device_id}: execution model lacks small/medium/large padding")
        try:
            numeric_values = [float(coefficients[key]) for key in required_coefficients]
            numeric_values.extend(float(banded[key]) for key in ("small", "medium", "large"))
        except (TypeError, ValueError) as exc:
            parser.error(f"{device_id}: non-numeric execution-model parameter: {exc}")
        if any(not math.isfinite(value) or value < 0 for value in numeric_values):
            parser.error(f"{device_id}: execution-model parameters must be finite and non-negative")
    pose_bounds = model_document.get("pose_error_bounds_deg", {})
    robust_fov = base.get("policy", {}).get("robust_fov", {})
    try:
        configured_yaw = float(robust_fov["pose_error_yaw_deg"])
        configured_pitch = float(robust_fov["pose_error_pitch_deg"])
        measured_yaw = float(pose_bounds["yaw"])
        measured_pitch = float(pose_bounds["pitch"])
    except (KeyError, TypeError, ValueError) as exc:
        parser.error(f"frozen robust-FOV pose bounds are incomplete: {exc}")
    if configured_yaw + 1e-12 < measured_yaw or configured_pitch + 1e-12 < measured_pitch:
        parser.error("configured robust-FOV pose bounds are smaller than calibration evidence")
    readiness_report = None
    if args.stage == "confirmatory":
        if args.readiness_report is None:
            parser.error("confirmatory plan generation requires --readiness-report")
        readiness_report = json.loads(args.readiness_report.resolve().read_text(encoding="utf-8"))
        readiness_clean = dict(readiness_report)
        stored_readiness_hash = readiness_clean.pop("report_sha256", None)
        if stored_readiness_hash != sha256_bytes(canonical_json(readiness_clean)):
            parser.error("readiness report self-hash is invalid")
        if readiness_report.get("ready_to_freeze_and_generate_formal_plan") is not True:
            parser.error("readiness report did not pass every development gate")
        if readiness_report.get("inputs", {}).get("execution_model_sha256") != model_sha256:
            parser.error("readiness report refers to a different execution model")
        if readiness_report.get("development_frozen_software_sha256") != software_hashes:
            parser.error("runtime/analysis software changed after the six development runs")
        if readiness_report.get("development_base_config_sha256") != base_config_hash:
            parser.error("base configuration changed after the six development runs")

    protocol = PROTOCOL if args.stage == "confirmatory" else "ral-hil-2-development"
    design = DESIGN if args.stage == "confirmatory" else "v2-development-six-run-gate"
    plan_id = (f"ralhil-v2-50-{args.seed}" if args.stage == "confirmatory"
               else f"ralhil-v2-dev6-{args.seed}")
    overlay_dir = args.overlay_dir.resolve()
    overlay_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    block_count = BLOCKS if args.stage == "confirmatory" else 1
    for block in range(1, block_count + 1):
        add_pair(
            rows, base, overlay_dir, args.overlay_extends, plan_id, block, "nominal",
            NOMINAL_METHODS, balanced_order(NOMINAL_METHODS, args.seed, "nominal", block),
            args.seed, args.runtime_duration_s, args.cooldown_s, model_sha256,
            args.pause_duration_s,
            protocol, design,
        )
        add_pair(
            rows, base, overlay_dir, args.overlay_extends, plan_id, block, "pause",
            PAUSE_METHODS, balanced_order(PAUSE_METHODS, args.seed, "pause", block),
            args.seed, args.runtime_duration_s, args.cooldown_s, model_sha256,
            args.pause_duration_s,
            protocol, design,
        )
    if args.stage == "development":
        add_pair(
            rows, base, overlay_dir, args.overlay_extends, plan_id, 1, "repeatability",
            (PRIMARY_METHOD,), [PRIMARY_METHOD], args.seed,
            args.runtime_duration_s, args.cooldown_s, model_sha256,
            args.pause_duration_s, protocol, design,
        )
        validate_development(rows)
    else:
        validate(rows)

    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_prefix.with_suffix(".csv").resolve()
    json_path = args.output_prefix.with_suffix(".json").resolve()
    write_csv(csv_path, rows, FIELDS)
    payload = {
        "schema_version": 2,
        "protocol_version": protocol,
        "design_id": design,
        "plan_id": plan_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "plan_randomization_seed": args.seed,
        "primary_method": PRIMARY_METHOD,
        "nominal_methods": list(NOMINAL_METHODS),
        "pause_methods": list(PAUSE_METHODS),
        "methods_by_condition": {
            "nominal": list(NOMINAL_METHODS), "pause": list(PAUSE_METHODS),
            **({"repeatability": [PRIMARY_METHOD]} if args.stage == "development" else {}),
        },
        "frozen_horizon_s": HORIZON_S,
        "runtime_duration_s": args.runtime_duration_s,
        "cooldown_s": args.cooldown_s,
        "pause_duration_s": args.pause_duration_s,
        "blocks": block_count,
        "stage": args.stage,
        "run_count": len(rows),
        "expected_policy_sha256": sha256_file(ROOT / "algorithms" / "user_policy.py"),
        "expected_policy_version": ALGORITHM_VERSION,
        "expected_analysis_sha256": sha256_file(ROOT / "tools" / "analyze_runs.py"),
        "frozen_software_sha256": software_hashes,
        "execution_model_path": str(model_path),
        "expected_execution_model_sha256": model_sha256,
        "base_config_sha256": base_config_hash,
        "readiness_report_sha256": (
            sha256_file(args.readiness_report.resolve()) if args.readiness_report else None
        ),
        "paper_start_gates": {
            "nominal_uac": "median(V2-MAG)<0, bootstrap_95ci_upper<0, one-sided exact paired p<0.05",
            "nominal_t100": "bootstrap_95ci_upper of paired V2/MAG ratio <=1.05",
            "pause_uac": "bootstrap_95ci_upper of difference-in-differences <0",
            "repair_evidence": "at least one reassigned cell confirmed before recovery",
        },
        "independent_unit": "one complete real-hardware run",
        "rows": rows,
    }
    write_json(json_path, payload)
    print(json.dumps({
        "plan_id": plan_id, "rows": len(rows), "blocks": block_count,
        "stage": args.stage,
        "csv": str(csv_path), "json": str(json_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
