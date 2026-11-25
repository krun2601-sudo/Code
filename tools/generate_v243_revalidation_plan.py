#!/usr/bin/env python3
"""Generate the frozen 20-run V2.4.3 hardware revalidation campaign.

The campaign repeats the already registered repair-on/off paired design with
the final V2.4.3 source.  Generation is allowed only when the V2.4.2 source
campaign, deterministic equivalence audit, and live four-RTSP stress audit are
all hash-bound and passing.  This command never opens a socket.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algorithms.user_policy import ALGORITHM_VERSION  # noqa: E402
from common import (  # noqa: E402
    canonical_json, config_sha256, load_yaml_with_inheritance, sha256_bytes,
    sha256_file, write_csv, write_json,
)
from generate_repair_ablation_plan import (  # noqa: E402
    DESIGN, FIELDS, METHOD, PROTOCOL, add_row, validate, validate_model,
)


EXPECTED_SOURCE_VERSION = "2.4.2"
EXPECTED_SOURCE_POLICY_SHA256 = "7af5df1766dda38654e696703ff6e92226315fe5de28307b8e50a3ee279e92cb"


def self_hash_ok(document: dict[str, Any]) -> bool:
    stored = document.get("report_sha256")
    clean = dict(document)
    clean.pop("report_sha256", None)
    return stored == sha256_bytes(canonical_json(clean))


def frozen_software_hashes() -> dict[str, str]:
    names = (
        "algorithms/user_policy.py",
        "tools/analyze_runs.py",
        "tools/analyze_repair_ablation.py",
        "tools/preflight.py",
        "tools/prepare_run_context.py",
        "tools/run_confirmatory_batch.py",
        "tools/generate_v243_revalidation_plan.py",
        "tools/verify_v243_latency_fix.py",
        "tools/live_v243_planner_stress.py",
        "platform_patch/patchset.py",
    )
    return {name: sha256_file(ROOT / name) for name in names}


def verified_provenance(
    source_path: Path, equivalence_path: Path, live_stress_path: Path,
    candidate_hash: str, base_hash: str, model_hash: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    equivalence = json.loads(equivalence_path.read_text(encoding="utf-8"))
    live = json.loads(live_stress_path.read_text(encoding="utf-8"))
    if source.get("design_id") != DESIGN or int(source.get("run_count", 0)) != 20:
        raise ValueError("source is not the complete registered 20-run repair design")
    if source.get("expected_policy_version") != EXPECTED_SOURCE_VERSION:
        raise ValueError("source algorithm version is not V2.4.2")
    if source.get("expected_policy_sha256") != EXPECTED_SOURCE_POLICY_SHA256:
        raise ValueError("source V2.4.2 policy hash differs from the preserved reference")
    if source.get("base_config_sha256") != base_hash:
        raise ValueError("V2.4.3 base configuration differs from the source campaign")
    if source.get("expected_execution_model_sha256") != model_hash:
        raise ValueError("V2.4.3 execution model differs from the source campaign")
    if equivalence.get("pass") is not True or not self_hash_ok(equivalence):
        raise ValueError("offline V2.4.2-to-V2.4.3 equivalence audit is not a valid PASS")
    inputs = equivalence.get("inputs", {})
    if inputs.get("preserved_v242_policy_sha256") != EXPECTED_SOURCE_POLICY_SHA256:
        raise ValueError("equivalence audit used another V2.4.2 source")
    if inputs.get("candidate_v243_policy_sha256") != candidate_hash:
        raise ValueError("equivalence audit used another V2.4.3 source")
    if live.get("pass") is not True or not self_hash_ok(live):
        raise ValueError("live four-RTSP stress audit is not a valid PASS")
    if live.get("algorithm_version") != ALGORITHM_VERSION:
        raise ValueError("live stress version differs from the candidate version")
    if live.get("policy_sha256") != candidate_hash:
        raise ValueError("live stress used another V2.4.3 source")
    if live.get("motion_control", {}).get("actions_submitted") != 0:
        raise ValueError("live stress provenance unexpectedly contains actions")
    return source, {
        "source_v242_plan_sha256": sha256_file(source_path),
        "offline_equivalence_report_sha256": sha256_file(equivalence_path),
        "offline_equivalence_self_hash": str(equivalence["report_sha256"]),
        "live_four_rtsp_stress_report_sha256": sha256_file(live_stress_path),
        "live_four_rtsp_stress_self_hash": str(live["report_sha256"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--source-plan", required=True, type=Path)
    parser.add_argument("--equivalence-report", required=True, type=Path)
    parser.add_argument("--live-stress-report", required=True, type=Path)
    parser.add_argument("--output-prefix", required=True, type=Path)
    parser.add_argument("--overlay-dir", required=True, type=Path)
    parser.add_argument("--overlay-extends", required=True)
    parser.add_argument("--plan-id", default="ralhil-v243-repair-revalidation-20-20261021")
    args = parser.parse_args()
    if ALGORITHM_VERSION != "2.4.3":
        parser.error(f"candidate policy reports {ALGORITHM_VERSION}, expected 2.4.3")

    config_path = args.config.resolve()
    base = load_yaml_with_inheritance(config_path)
    model_path, model_hash = validate_model(base, config_path)
    base_hash = config_sha256(base)
    policy_hash = sha256_file(ROOT / "algorithms" / "user_policy.py")
    source, provenance = verified_provenance(
        args.source_plan.resolve(), args.equivalence_report.resolve(),
        args.live_stress_report.resolve(), policy_hash, base_hash, model_hash,
    )

    source_rows = sorted(source["rows"], key=lambda row: int(row["run_index"]))
    overlay_dir = args.overlay_dir.resolve()
    overlay_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    orders: dict[str, list[str]] = {}
    for source_row in source_rows:
        block = int(source_row["block"])
        condition = str(source_row["condition"])
        perturbation = json.loads(str(source_row["condition_parameters_json"]))
        orders.setdefault(str(block), []).append(condition)
        add_row(
            rows, base, overlay_dir, args.overlay_extends, args.plan_id,
            block, condition, perturbation,
            float(source_row["runtime_duration_s"]),
            float(source_row.get("cooldown_s", 10.0)), model_hash,
        )
    validate(rows, 10)

    csv_path = args.output_prefix.with_suffix(".csv").resolve()
    json_path = args.output_prefix.with_suffix(".json").resolve()
    write_csv(csv_path, rows, FIELDS)
    payload = {
        "schema_version": 2,
        "protocol_version": PROTOCOL,
        "design_id": DESIGN,
        "plan_id": args.plan_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "plan_randomization_basis": "exact order and perturbations from hash-bound V2.4.2 source plan",
        "primary_method": METHOD,
        "methods_by_condition": {
            "pause_repair_on": [METHOD], "pause_repair_off": [METHOD],
        },
        "repair_variants": source["repair_variants"],
        "variant_order_by_block": orders,
        "frozen_horizon_s": 60.0,
        "runtime_duration_s": 85.0,
        "pause_duration_s": 25.0,
        "cooldown_s": 10.0,
        "blocks": 10,
        "stage": "confirmatory_v243_revalidation",
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
        "v243_change_scope": (
            "immutable initial-seed edge precomputation moved before RTSP/device workers; "
            "routes, objectives, tie breaks, evaluation order, and repair logic unchanged"
        ),
        "revalidation_provenance": provenance,
        "preregistered_gates": source["preregistered_gates"],
        "failure_rule": (
            "any planner-budget, coverage, ACK, command, frame-evidence, hash, or log-integrity "
            "failure stops the campaign and remains in the audit; it is not replaced as missing data"
        ),
        "independent_unit": "one complete real-hardware run; inference is paired by block",
        "rows": rows,
    }
    write_json(json_path, payload)
    print(json.dumps({
        "plan_id": args.plan_id,
        "algorithm_version": ALGORITHM_VERSION,
        "policy_sha256": policy_hash,
        "runs": len(rows),
        "csv": str(csv_path),
        "json": str(json_path),
        "provenance": provenance,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
