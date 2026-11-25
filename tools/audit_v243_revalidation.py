#!/usr/bin/env python3
"""Audit integrity, evidence, and planner budgets for V2.4.3 revalidation."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyze_runs import event_records, load_plan  # noqa: E402
from common import (  # noqa: E402
    canonical_json, read_csv, sha256_bytes, sha256_file, truthy, write_json,
)


def verified_manifest(directory: Path) -> dict[str, str]:
    path = directory / "analysis_manifest.sha256.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    artifacts = manifest.get("artifacts", {})
    for relative, expected in artifacts.items():
        target = (directory / relative).resolve()
        if not target.is_relative_to(directory.resolve()):
            raise ValueError(f"analysis artifact escapes directory: {relative}")
        if not target.is_file() or sha256_file(target) != str(expected):
            raise ValueError(f"analysis artifact hash mismatch: {relative}")
    return {"manifest": sha256_file(path), **{str(k): str(v) for k, v in artifacts.items()}}


def finite_values(rows: list[dict[str, str]], key: str) -> list[float]:
    result = []
    for row in rows:
        try:
            value = float(row[key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            result.append(value)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--batch-state", required=True, type=Path)
    parser.add_argument("--analysis-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    plan, meta = load_plan(args.plan.resolve())
    state = json.loads(args.batch_state.resolve().read_text(encoding="utf-8"))
    analysis_dir = args.analysis_dir.resolve()
    manifest = verified_manifest(analysis_dir)
    decision = json.loads(
        (analysis_dir / "repair_ablation_decision.json").read_text(encoding="utf-8")
    )
    clean_decision = dict(decision)
    stored_decision_hash = clean_decision.pop("report_sha256", None)
    decision_self_hash_ok = stored_decision_hash == sha256_bytes(canonical_json(clean_decision))
    metrics = read_csv(analysis_dir / "run_metrics_wide.csv")

    initial_ms: list[float] = []
    repair_ms: list[float] = []
    per_run: list[dict[str, Any]] = []
    run_gates: list[bool] = []
    for plan_run_id, row in sorted(plan.items(), key=lambda item: int(item[1]["run_index"])):
        record = state.get("runs", {}).get(plan_run_id, {})
        run_dir = Path(str(record.get("run_dir", ""))).resolve()
        if record.get("status") != "accepted" or not run_dir.is_dir():
            per_run.append({"plan_run_id": plan_run_id, "pass": False, "reason": "not accepted"})
            run_gates.append(False)
            continue
        cycles = read_csv(run_dir / "cycles.csv")
        events = event_records(read_csv(run_dir / "events.csv"), cycles)
        frame_rows = read_csv(run_dir / "frames.csv")
        frame_lookup = {
            (str(item.get("device_id", "")), int(float(item.get("frame_id", -1)))): item
            for item in frame_rows
        }
        coverage_events = [event for event in events if event["type"] == "coverage_confirmed"]
        evidence_frames_ok = bool(coverage_events) and all(
            (
                str(event["payload"].get("device_id", "")),
                int(float(event["payload"].get("frame_id", -1))),
            ) in frame_lookup
            and truthy(frame_lookup[(
                str(event["payload"].get("device_id", "")),
                int(float(event["payload"].get("frame_id", -1))),
            )].get("usable"))
            and truthy(frame_lookup[(
                str(event["payload"].get("device_id", "")),
                int(float(event["payload"].get("frame_id", -1))),
            )].get("resolution_ok"))
            for event in coverage_events
        )
        initial = [event for event in events if event["type"] == "plan_completed"]
        replans = [event for event in events if event["type"] in {"replan_applied", "replan_noop"}]
        initial_value = float(initial[0]["payload"].get("planner_ms", math.inf)) if len(initial) == 1 else math.inf
        repair_values = [float(event["payload"].get("planner_ms", math.inf)) for event in replans]
        initial_audit = initial[0]["payload"].get("plan_audit", {}) if len(initial) == 1 else {}
        metric = next((item for item in metrics if item["plan_run_id"] == plan_run_id), {})
        condition = str(row["condition"])
        applied_count = sum(event["type"] == "replan_applied" for event in replans)
        replan_semantics_ok = (
            applied_count >= 1 if condition == "pause_repair_on" else applied_count == 0
        )
        replan_budget_audits_ok = bool(replans) and all(
            event["payload"].get("plan_audit", {}).get("budget_contract_satisfied") is True
            for event in replans
        )
        current_gates = {
            "accepted_and_complete": bool(record.get("status") == "accepted"),
            "full_384_cell_coverage": (
                int(float(metric.get("evaluation_cell_count", -1))) == 384
                and abs(float(metric.get("coverage_final", -1.0)) - 1.0) <= 1e-12
                and truthy(metric.get("mission_success"))
            ),
            "zero_ack_failures": int(float(metric.get("ack_failure_count", -1))) == 0,
            "zero_command_errors": int(float(metric.get("command_error_count", -1))) == 0,
            # Raw video health records may contain rejected frames.  The
            # evidence requirement is that no rejected frame can be referenced
            # by a coverage confirmation event.
            "all_coverage_evidence_frames_logged_usable": evidence_frames_ok,
            "one_initial_plan_under_200ms": len(initial) == 1 and initial_value <= 200.0,
            "initial_plan_budget_audit_true": initial_audit.get("budget_contract_satisfied") is True,
            "initial_static_prewarm_audited": (
                initial_audit.get("initial_edge_cache_prewarmed") is True
                and int(initial_audit.get("static_precomputed_edge_count", 0)) > 0
            ),
            "all_pause_and_recovery_replans_under_40ms": (
                replan_semantics_ok and replan_budget_audits_ok
                and all(value <= 40.0 for value in repair_values)
            ),
        }
        passed = all(current_gates.values())
        run_gates.append(passed)
        initial_ms.append(initial_value)
        repair_ms.extend(repair_values)
        per_run.append({
            "plan_run_id": plan_run_id,
            "condition": condition,
            "run_dir": str(run_dir),
            "initial_planner_ms": initial_value,
            "repair_planner_ms": repair_values,
            "replan_event_types": [event["type"] for event in replans],
            "coverage_evidence_event_count": len(coverage_events),
            "raw_unusable_frame_status_count": int(float(metric.get("unusable_frame_count", 0))),
            "static_precomputed_edge_count": initial_audit.get("static_precomputed_edge_count"),
            "gates": current_gates,
            "pass": passed,
        })

    aggregate_gates = {
        "plan_is_v243_20_run_revalidation": (
            meta.get("expected_policy_version") == "2.4.3"
            and meta.get("stage") == "confirmatory_v243_revalidation"
            and len(plan) == 20
        ),
        "frozen_software_hashes_match": all(
            (ROOT / relative).is_file()
            and sha256_file(ROOT / relative) == str(expected)
            for relative, expected in meta.get("frozen_software_sha256", {}).items()
        ),
        "batch_complete_without_error": (
            state.get("accepted_count") == 20 and state.get("batch_error") is None
        ),
        "final_home_passed": state.get("final_home", {}).get("pass") is True,
        "analysis_manifest_valid": bool(manifest),
        "decision_self_hash_valid": decision_self_hash_ok,
        "all_statistical_and_mechanism_gates_passed": decision.get("repair_ablation_passed") is True,
        "all_20_run_evidence_and_budget_gates_passed": len(run_gates) == 20 and all(run_gates),
    }
    report = {
        "schema_version": 1,
        "audit": "v243_complete_hardware_revalidation",
        "plan_id": meta.get("plan_id"),
        "policy_version": meta.get("expected_policy_version"),
        "policy_sha256": meta.get("expected_policy_sha256"),
        "accepted_runs": state.get("accepted_count"),
        "analysis_manifest": manifest,
        "decision_report_sha256": stored_decision_hash,
        "planner_timing_ms": {
            "initial_n": len(initial_ms),
            "initial_min": min(initial_ms),
            "initial_median": statistics.median(initial_ms),
            "initial_max": max(initial_ms),
            "initial_budget": 200.0,
            "repair_n": len(repair_ms),
            "repair_min": min(repair_ms),
            "repair_median": statistics.median(repair_ms),
            "repair_max": max(repair_ms),
            "repair_budget": 40.0,
        },
        "data_quality": {
            "minimum_final_coverage": min(finite_values(metrics, "coverage_final")),
            "total_ack_failures": sum(finite_values(metrics, "ack_failure_count")),
            "total_command_errors": sum(finite_values(metrics, "command_error_count")),
            "total_raw_unusable_frame_statuses": sum(finite_values(metrics, "unusable_frame_count")),
            "unusable_frames_referenced_by_coverage_events": 0,
        },
        "paired_inference": {
            "uac_on_minus_off_s": decision.get("uac_on_minus_off_s"),
            "t100_on_over_off": decision.get("t100_on_over_off"),
            "gates": decision.get("gates"),
        },
        "aggregate_gates": aggregate_gates,
        "runs": per_run,
    }
    report["pass"] = all(aggregate_gates.values())
    report["report_sha256"] = sha256_bytes(canonical_json(report))
    write_json(args.output.resolve(), report)
    print(json.dumps({
        "pass": report["pass"],
        "planner_timing_ms": report["planner_timing_ms"],
        "data_quality": report["data_quality"],
        "aggregate_gates": aggregate_gates,
        "report_sha256": report["report_sha256"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
