#!/usr/bin/env python3
"""Generate a frozen paired real-hardware ablation of V2 online repair.

The two variants execute the same V2 policy and the same coverage-triggered
logical pause.  They differ only in ``switch_min_gain_s``: 0.05 s enables the
registered repair rule, while 60.000001 s makes acceptance impossible because
UAC_H is bounded to [0, 60] s.  The latter therefore executes the search and
audits the proposal but keeps the original queues.

This command only creates plan and overlay files.  It never opens a socket or
sends a hardware command.
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
    load_yaml_with_inheritance, pair_frozen_config_sha256, sha256_bytes,
    sha256_file, write_csv, write_json,
)
from generate_v2_confirmatory_plan import execution_model_path, pause_condition


PROTOCOL = "ral-hil-2"
DESIGN = "confirmatory-v2-paired-repair-ablation"
METHOD = "uac_robust_repair_v2"
CONDITIONS = ("pause_repair_on", "pause_repair_off")
HORIZON_S = 60.0
REPAIR_ON_GAIN_S = 0.05
REPAIR_OFF_GAIN_S = 60.000001
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
    names = (
        "algorithms/user_policy.py",
        "tools/analyze_runs.py",
        "tools/analyze_repair_ablation.py",
        "tools/preflight.py",
        "tools/prepare_run_context.py",
        "tools/run_confirmatory_batch.py",
        "tools/generate_repair_ablation_plan.py",
        "platform_patch/patchset.py",
    )
    return {name: sha256_file(ROOT / name) for name in names}


def verify_manifest(directory: Path) -> None:
    manifest_path = directory / "analysis_manifest.sha256.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, expected in manifest.get("artifacts", {}).items():
        target = directory / name
        if not target.is_file() or sha256_file(target) != str(expected):
            raise ValueError(f"formal analysis artifact failed hash audit: {name}")


def verify_formal_provenance(
    formal_analysis: Path, formal_plan: Path, formal_batch_state: Path,
    base_hash: str, policy_hash: str, model_hash: str,
) -> dict[str, str]:
    verify_manifest(formal_analysis)
    summary = json.loads((formal_analysis / "analysis_summary.json").read_text(encoding="utf-8"))
    gates = json.loads((formal_analysis / "v2_decision_gates.json").read_text(encoding="utf-8"))
    plan = json.loads(formal_plan.read_text(encoding="utf-8"))
    state = json.loads(formal_batch_state.read_text(encoding="utf-8"))
    if summary.get("accepted_runs") != 50 or summary.get("invalid_or_missing_active_attempts") != 0:
        raise ValueError("source confirmatory campaign is not a complete 50-run audited data set")
    if state.get("accepted_count") != 50 or state.get("batch_error") is not None:
        raise ValueError("source confirmatory batch state is incomplete or failed")
    if state.get("final_home", {}).get("pass") is not True:
        raise ValueError("source confirmatory campaign lacks a passing final-home record")
    expected_pattern = {
        "complete_10_blocks": True,
        "uac_superiority_vs_mag": True,
        "t100_noninferior_5pct_vs_mag": True,
        "pause_difference_in_differences_favorable": False,
        "reassigned_coverage_before_recovery_observed": True,
    }
    if gates.get("gates") != expected_pattern:
        raise ValueError("source gate pattern differs from the reviewed trigger for this ablation")
    if gates.get("paper_start_allowed") is not False:
        raise ValueError("repair ablation is only registered as follow-up to the failed pause gate")
    if plan.get("base_config_sha256") != base_hash:
        raise ValueError("base configuration differs from the 50-run source campaign")
    if plan.get("expected_policy_sha256") != policy_hash:
        raise ValueError("policy source differs from the 50-run source campaign")
    if plan.get("expected_execution_model_sha256") != model_hash:
        raise ValueError("execution model differs from the 50-run source campaign")
    if state.get("plan_id") != plan.get("plan_id"):
        raise ValueError("source plan and batch state do not match")
    return {
        "formal_analysis_manifest_sha256": sha256_file(
            formal_analysis / "analysis_manifest.sha256.json"
        ),
        "formal_decision_gates_sha256": sha256_file(
            formal_analysis / "v2_decision_gates.json"
        ),
        "formal_plan_sha256": sha256_file(formal_plan),
        "formal_batch_state_sha256": sha256_file(formal_batch_state),
    }


def seed_for(seed: int, *parts: str) -> int:
    digest = hashlib.sha256((str(seed) + "|" + "|".join(parts)).encode()).digest()
    return int.from_bytes(digest[:4], "big")


def validate_model(base: dict[str, Any], config_path: Path) -> tuple[Path, str]:
    path = execution_model_path(base, config_path)
    digest = sha256_file(path)
    options = base.get("policy", {}).get("execution_model", {})
    if options.get("status") != "calibrated" or str(options.get("sha256", "")).lower() != digest:
        raise ValueError("execution model is not calibrated and hash-frozen")
    model = json.loads(path.read_text(encoding="utf-8"))
    if int(model.get("schema_version", 0)) < 2:
        raise ValueError("angle-regime execution-model schema is required")
    if "disjoint completed source runs" not in str(model.get("split_rule", "")):
        raise ValueError("execution-model split is not run-disjoint")
    active = {str(row.get("id")) for row in base.get("devices", []) if row.get("enabled", True)}
    if set(model.get("models", {})) != active or len(active) != 4:
        raise ValueError("execution model and active four-device set differ")
    bounds = model.get("pose_error_bounds_deg", {})
    robust = base.get("policy", {}).get("robust_fov", {})
    if (float(robust.get("pose_error_yaw_deg", -1)) < float(bounds.get("yaw", math.inf)) or
            float(robust.get("pose_error_pitch_deg", -1)) < float(bounds.get("pitch", math.inf))):
        raise ValueError("robust FOV bounds are smaller than calibration evidence")
    return path, digest


def add_row(
    rows: list[dict[str, Any]], base: dict[str, Any], overlay_dir: Path,
    overlay_extends: str, plan_id: str, block: int, condition: str,
    perturbation: dict[str, Any], runtime_s: float, cooldown_s: float,
    model_hash: str,
) -> None:
    run_index = len(rows) + 1
    run_id = f"{plan_id}-R{run_index:03d}"
    enabled = condition == "pause_repair_on"
    gain = REPAIR_ON_GAIN_S if enabled else REPAIR_OFF_GAIN_S
    experiment = {
        "protocol_version": PROTOCOL,
        "design_id": DESIGN,
        "plan_id": plan_id,
        "plan_run_id": run_id,
        "run_index": run_index,
        "block": block,
        "pair_id": f"B{block:02d}-{condition}",
        "method": METHOD,
        "condition": condition,
        "instance_seed": 0,
        "injection_seed": int(perturbation["seed"]),
        "solver_seed": 0,
        "horizon_s": HORIZON_S,
    }
    override = {
        "system": {"duration_s": runtime_s},
        "policy": {
            "method": METHOD,
            "execution_confirmation": {"measured_pose_projection": True},
            "robust_fov": {"enforce_for_all_methods": True},
            "robust_objective": {
                "horizon_s": HORIZON_S,
                "complete_mag_reference": True,
                "switch_min_gain_s": gain,
            },
            "experiment": experiment,
            "fault_injection": perturbation,
        },
    }
    resolved = deep_merge(base, override)
    overlay = overlay_dir / f"{run_index:03d}_{condition}.yaml"
    import yaml
    overlay.write_text(yaml.safe_dump(
        {"extends": overlay_extends, **override}, allow_unicode=True, sort_keys=False,
    ), encoding="utf-8")
    rows.append({
        "schema_version": 2,
        "protocol_version": PROTOCOL,
        "design_id": DESIGN,
        "plan_id": plan_id,
        "plan_run_id": run_id,
        "run_index": run_index,
        "block": block,
        "pair_id": experiment["pair_id"],
        "within_pair_order": 1,
        "method": METHOD,
        "condition": condition,
        "instance_seed": 0,
        "injection_seed": experiment["injection_seed"],
        "solver_seed": 0,
        "horizon_s": HORIZON_S,
        "runtime_duration_s": runtime_s,
        "condition_parameters_json": json.dumps(
            perturbation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "expected_config_sha256": config_sha256(resolved),
        "expected_hardware_config_sha256": hardware_config_sha256(resolved),
        "expected_pair_frozen_sha256": pair_frozen_config_sha256(resolved),
        "expected_evaluation_cell_count": 384,
        "expected_policy_sha256": sha256_file(ROOT / "algorithms" / "user_policy.py"),
        "expected_policy_version": ALGORITHM_VERSION,
        "expected_execution_model_sha256": model_hash,
        "overlay_path": str(overlay.resolve()),
        "cooldown_s": cooldown_s,
        "status": "planned",
    })


def validate(rows: list[dict[str, Any]], blocks: int) -> None:
    if len(rows) != 2 * blocks:
        raise AssertionError(f"expected {2 * blocks} rows, got {len(rows)}")
    for block in range(1, blocks + 1):
        items = [row for row in rows if row["block"] == block]
        if {row["condition"] for row in items} != set(CONDITIONS):
            raise AssertionError(f"block {block} is not a complete repair pair")
        for key in (
            "instance_seed", "injection_seed", "solver_seed", "horizon_s",
            "runtime_duration_s", "condition_parameters_json",
            "expected_hardware_config_sha256", "expected_policy_sha256",
            "expected_policy_version", "expected_execution_model_sha256",
        ):
            if len({str(row[key]) for row in items}) != 1:
                raise AssertionError(f"block {block}: paired field differs: {key}")
        if len({row["expected_config_sha256"] for row in items}) != 2:
            raise AssertionError(f"block {block}: full config hashes must differ by repair threshold")
    positions = {
        condition: [index for index, row in enumerate(rows, 1) if row["condition"] == condition]
        for condition in CONDITIONS
    }
    first_counts = {
        condition: sum(rows[index]["condition"] == condition for index in range(0, len(rows), 2))
        for condition in CONDITIONS
    }
    if max(first_counts.values()) - min(first_counts.values()) > 1:
        raise AssertionError(f"variant order is imbalanced: {first_counts}; positions={positions}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-prefix", required=True, type=Path)
    parser.add_argument("--overlay-dir", required=True, type=Path)
    parser.add_argument("--overlay-extends", required=True)
    parser.add_argument("--formal-analysis", required=True, type=Path)
    parser.add_argument("--formal-plan", required=True, type=Path)
    parser.add_argument("--formal-batch-state", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=20261026)
    parser.add_argument("--blocks", type=int, default=10)
    parser.add_argument("--runtime-duration-s", type=float, default=85.0)
    parser.add_argument("--pause-duration-s", type=float, default=25.0)
    parser.add_argument("--cooldown-s", type=float, default=10.0)
    args = parser.parse_args()
    if args.blocks != 10:
        parser.error("confirmatory repair ablation is frozen at exactly 10 paired blocks")
    if args.runtime_duration_s < HORIZON_S:
        parser.error("runtime must cover the fixed 60 s horizon")
    if args.pause_duration_s <= 0 or args.cooldown_s < 10:
        parser.error("pause must be positive and cooldown must be at least 10 s")

    config_path = args.config.resolve()
    base = load_yaml_with_inheritance(config_path)
    model_path, model_hash = validate_model(base, config_path)
    policy_hash = sha256_file(ROOT / "algorithms" / "user_policy.py")
    base_hash = config_sha256(base)
    provenance = verify_formal_provenance(
        args.formal_analysis.resolve(), args.formal_plan.resolve(),
        args.formal_batch_state.resolve(), base_hash, policy_hash, model_hash,
    )

    plan_id = f"ralhil-v2-repair-ablation-20-{args.seed}"
    overlay_dir = args.overlay_dir.resolve()
    overlay_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    orders: dict[str, list[str]] = {}
    for block in range(1, args.blocks + 1):
        perturbation = pause_condition(args.seed, block, args.pause_duration_s)
        order = list(CONDITIONS)
        random.Random(seed_for(args.seed, str(block), "variant-order")).shuffle(order)
        # Reverse half the deterministic shuffles if necessary to guarantee
        # exact 5/5 first-position balance across ten blocks.
        desired_first = CONDITIONS[(block - 1) % 2]
        if order[0] != desired_first:
            order.reverse()
        orders[str(block)] = order
        for condition in order:
            add_row(
                rows, base, overlay_dir, args.overlay_extends, plan_id, block,
                condition, perturbation, args.runtime_duration_s, args.cooldown_s,
                model_hash,
            )
    validate(rows, args.blocks)

    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_prefix.with_suffix(".csv").resolve()
    json_path = args.output_prefix.with_suffix(".json").resolve()
    write_csv(csv_path, rows, FIELDS)
    payload = {
        "schema_version": 2,
        "protocol_version": PROTOCOL,
        "design_id": DESIGN,
        "plan_id": plan_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "plan_randomization_seed": args.seed,
        "primary_method": METHOD,
        "methods_by_condition": {condition: [METHOD] for condition in CONDITIONS},
        "repair_variants": {
            "pause_repair_on": {"switch_min_gain_s": REPAIR_ON_GAIN_S},
            "pause_repair_off": {
                "switch_min_gain_s": REPAIR_OFF_GAIN_S,
                "interpretation": "repair proposal audited but cannot be accepted because UAC_H gain <= 60 s",
            },
        },
        "variant_order_by_block": orders,
        "frozen_horizon_s": HORIZON_S,
        "runtime_duration_s": args.runtime_duration_s,
        "pause_duration_s": args.pause_duration_s,
        "cooldown_s": args.cooldown_s,
        "blocks": args.blocks,
        "stage": "confirmatory_repair_ablation",
        "run_count": len(rows),
        "expected_policy_sha256": policy_hash,
        "expected_policy_version": ALGORITHM_VERSION,
        "expected_analysis_sha256": sha256_file(ROOT / "tools" / "analyze_runs.py"),
        "expected_repair_ablation_analysis_sha256": sha256_file(
            ROOT / "tools" / "analyze_repair_ablation.py"
        ),
        "frozen_software_sha256": frozen_software_hashes(),
        "execution_model_path": str(model_path),
        "expected_execution_model_sha256": model_hash,
        "base_config_sha256": base_hash,
        "source_formal_provenance": provenance,
        "preregistered_gates": {
            "complete_pairs": "10 audited blocks with both variants",
            "same_fault_device": "same selected paused device within every block",
            "same_initial_plan_signature": "identical timing-independent initial-plan audit within every block",
            "repair_on_effect": "median(on-off UAC)<0, bootstrap 95% CI upper<0, one-sided exact sign p<0.05",
            "t100_noninferiority": "bootstrap 95% CI upper of paired on/off T100 ratio <=1.05",
            "repair_mechanism": "all on runs have effective repair and pre-recovery reassigned coverage; all off runs have neither",
        },
        "independent_unit": "one complete real-hardware run; inference is paired by block",
        "rows": rows,
    }
    write_json(json_path, payload)
    print(json.dumps({
        "plan_id": plan_id, "runs": len(rows), "blocks": args.blocks,
        "csv": str(csv_path), "json": str(json_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
