#!/usr/bin/env python3
"""Bind one planned run to a passing read-only preflight report.

The resulting JSON is provenance only; it neither arms nor starts hardware.
Copy it into the corresponding completed run directory as ``run_context.json``.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from common import canonical_json, sha256_bytes, sha256_file, write_json


def read_plan_row(path: Path, plan_run_id: str) -> dict[str, str]:
    if path.suffix.lower() == ".json":
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = document.get("rows", document if isinstance(document, list) else [])
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    matches = [row for row in rows if str(row.get("plan_run_id")) == plan_run_id]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one plan row for {plan_run_id!r}; found {len(matches)}")
    return dict(matches[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-run-id", required=True)
    parser.add_argument("--preflight", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--operator-arm-authorized", action="store_true",
                        help="Record that the human operator explicitly authorized real motion")
    args = parser.parse_args()
    row = read_plan_row(args.plan, args.plan_run_id)
    preflight = json.loads(args.preflight.read_text(encoding="utf-8"))
    if preflight.get("ready_to_move") is not True:
        raise SystemExit("preflight is not a full PASS (ready_to_move != true)")
    if preflight.get("phase") != "experiment":
        raise SystemExit("main-run context requires a --phase experiment preflight")
    expected_hardware = row.get("expected_hardware_config_sha256")
    if preflight.get("config_sha256") != row.get("expected_config_sha256"):
        raise SystemExit("preflight full configuration hash does not match the selected plan row")
    if preflight.get("hardware_config_sha256") != expected_hardware:
        raise SystemExit("preflight hardware hash does not match the selected plan row")
    coverage_task = preflight.get("coverage_task_observed")
    if not isinstance(coverage_task, dict):
        raise SystemExit("preflight lacks an observed coverage-task record")
    try:
        observed_m = int(coverage_task["evaluation_cell_count"])
        expected_m = int(row["expected_evaluation_cell_count"])
        reachable = int(coverage_task["reachable_count"])
        reachable_ratio = float(coverage_task["reachable_ratio"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"invalid coverage-task record in preflight: {exc}") from exc
    if observed_m != expected_m or observed_m != 384:
        raise SystemExit(f"preflight evaluation M={observed_m} does not match planned M={expected_m}")
    if reachable != observed_m or abs(reachable_ratio - 1.0) > 1e-6:
        raise SystemExit("all 384 evaluation cells must be reachable before a confirmatory run")
    if preflight.get("policy_source_sha256") != row.get("expected_policy_sha256"):
        raise SystemExit("preflight policy source hash does not match the frozen plan")
    if preflight.get("policy_algorithm_version") != row.get("expected_policy_version"):
        raise SystemExit("preflight policy version does not match the frozen plan")
    expected_model_hash = str(row.get("expected_execution_model_sha256", "")).strip()
    if expected_model_hash and preflight.get("execution_model_sha256") != expected_model_hash:
        raise SystemExit("preflight execution-model hash does not match the frozen plan")
    stored_report_hash = preflight.get("report_sha256")
    copy_for_hash = dict(preflight)
    copy_for_hash.pop("report_sha256", None)
    if stored_report_hash != sha256_bytes(canonical_json(copy_for_hash)):
        raise SystemExit("preflight report self-hash is invalid")
    context = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "motion_or_network_performed": False,
        "operator_arm_authorization": bool(args.operator_arm_authorized),
        "operator_arm_authorization_basis": (
            "explicit_user_request" if args.operator_arm_authorized else "not_recorded"
        ),
        "plan_file_sha256": sha256_file(args.plan.resolve()),
        "preflight_file_sha256": sha256_file(args.preflight.resolve()),
        "preflight_report_sha256": stored_report_hash,
        "preflight_summary": {
            "ready_to_move": preflight.get("ready_to_move"),
            "phase": preflight.get("phase"),
            "static_pass": preflight.get("static_pass"),
            "live_pass": preflight.get("live_pass"),
            "config_sha256": preflight.get("config_sha256"),
            "hardware_config_sha256": preflight.get("hardware_config_sha256"),
            "execution_model_sha256": preflight.get("execution_model_sha256"),
            "coverage_task_observed": coverage_task,
            "policy_source_sha256": preflight.get("policy_source_sha256"),
            "policy_algorithm_version": preflight.get("policy_algorithm_version"),
        },
        "plan_row": row,
    }
    context["context_sha256"] = sha256_bytes(canonical_json(context))
    write_json(args.output.resolve(), context)
    print(json.dumps({"output": str(args.output.resolve()), "plan_run_id": args.plan_run_id,
                      "context_sha256": context["context_sha256"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
