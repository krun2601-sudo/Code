#!/usr/bin/env python3
"""Analyze the preregistered paired V2 repair-on versus repair-off ablation."""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import (
    canonical_json, discover_run_dirs, median, percentile, read_csv,
    sha256_bytes, sha256_file, write_csv, write_json,
)
from analyze_runs import analyze_one, event_records, load_plan


ON = "pause_repair_on"
OFF = "pause_repair_off"


def bootstrap_ci(
    values: list[float], seed: int, samples: int,
    statistic: Callable[[list[float]], float] = median,
) -> tuple[float, float, float]:
    if not values:
        return math.nan, math.nan, math.nan
    rng = random.Random(seed)
    boot = [statistic([values[rng.randrange(len(values))] for _ in values])
            for _ in range(samples)]
    return statistic(values), percentile(boot, 0.025), percentile(boot, 0.975)


def one_sided_sign_p_less(values: list[float]) -> float:
    nonzero = [value for value in values if abs(value) > 1e-12]
    if not nonzero:
        return 1.0
    negative = sum(value < 0 for value in nonzero)
    n = len(nonzero)
    return sum(math.comb(n, k) for k in range(negative, n + 1)) / (2 ** n)


def self_hash_ok(document: dict[str, Any]) -> bool:
    stored = document.get("report_sha256")
    clean = dict(document)
    clean.pop("report_sha256", None)
    return stored == sha256_bytes(canonical_json(clean))


def event_audit(run_dir: Path) -> dict[str, Any]:
    events = event_records(read_csv(run_dir / "events.csv"), read_csv(run_dir / "cycles.csv"))
    initial_plans = [event for event in events if event["type"] == "plan_completed"]
    if len(initial_plans) != 1:
        raise ValueError(f"{run_dir.name}: expected one initial plan_completed event")
    plan_payload = initial_plans[0]["payload"]
    plan_audit = dict(plan_payload.get("plan_audit", {}))
    # Wall-clock planner time can vary without changing a route.  Every field
    # that determines or fingerprints the deterministic committed solution is
    # retained, while runtime timing/accounting fields are removed.
    for key in (
        "observed_planner_ms", "configured_budget_ms", "deadline_guard_ms",
        "effective_search_budget_ms", "budget_contract_satisfied",
    ):
        plan_audit.pop(key, None)
    plan_signature = {
        "queued_task_count": plan_payload.get("queued_task_count"),
        "residual_count": plan_payload.get("residual_count"),
        "planner_variant": plan_payload.get("planner_variant"),
        "plan_audit": plan_audit,
    }
    injections = [event for event in events if event["type"] == "injection_started"]
    if len(injections) != 1:
        raise ValueError(f"{run_dir.name}: expected one injection_started event")
    injection = injections[0]
    injection_t = float(injection["payload"]["mission_elapsed_s"])
    device = str(injection["payload"].get("device_id", ""))
    prefault: dict[str, list[str]] = {}
    for event in events:
        if event["type"] != "transition_completed":
            continue
        payload = event["payload"]
        elapsed = float(payload.get("mission_elapsed_s", math.inf))
        if elapsed > injection_t + 1e-9:
            continue
        device_id = str(payload.get("device_id", ""))
        # transition_completed does not expose the private route array.  Its
        # stable task id plus planned endpoint is an execution-level route
        # fingerprint and avoids using frame arrival order as evidence.
        route_step = "|".join((
            str(payload.get("task_id", "")),
            str(payload.get("planned_to_yaw_deg", "")),
            str(payload.get("planned_to_pitch_deg", "")),
        ))
        prefault.setdefault(device_id, []).append(route_step)
    applied = [event for event in events
               if event["type"] == "replan_applied" and event["payload"].get("effective", True)]
    reassigned = [event for event in events
                  if event["type"] == "reassigned_coverage_confirmed"]
    before = [event for event in reassigned
              if event["payload"].get("before_original_device_recovery") is True]
    return {
        "paused_device_id": device,
        "injection_mission_elapsed_s": injection_t,
        "initial_plan_signature": plan_signature,
        "prefault_completed_candidates_by_device": prefault,
        "effective_replan_count_from_events": len(applied),
        "reassigned_event_count": len(reassigned),
        "reassigned_before_recovery_event_count": len(before),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--batch-state", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--bootstrap-samples", type=int, default=100000)
    parser.add_argument("--seed", type=int, default=20261027)
    args = parser.parse_args()
    if args.bootstrap_samples < 10000:
        parser.error("confirmatory analysis requires at least 10000 bootstrap samples")

    plan, meta = load_plan(args.plan.resolve())
    expected = str(meta.get("expected_repair_ablation_analysis_sha256", ""))
    if expected != sha256_file(Path(__file__).resolve()):
        parser.error("repair-ablation analysis source differs from frozen plan")
    if meta.get("design_id") != "confirmatory-v2-paired-repair-ablation":
        parser.error("plan is not the registered repair-ablation design")
    state = json.loads(args.batch_state.resolve().read_text(encoding="utf-8"))
    if state.get("plan_id") != meta.get("plan_id"):
        parser.error("batch state belongs to another plan")
    accepted_dirs = {
        Path(str(record["run_dir"])).resolve(): plan_id
        for plan_id, record in state.get("runs", {}).items()
        if record.get("status") == "accepted" and record.get("run_dir")
    }

    rows: list[dict[str, Any]] = []
    audits: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    rejected: list[dict[str, str]] = []
    for run_dir in discover_run_dirs(args.runs.resolve()):
        resolved = run_dir.resolve()
        if resolved not in accepted_dirs:
            rejected.append({"source_dir": str(resolved), "reason": "not active accepted attempt"})
            continue
        plan_id = accepted_dirs[resolved]
        try:
            metric = analyze_one(resolved, plan[plan_id])
            audit = event_audit(resolved)
        except Exception as exc:
            rejected.append({"source_dir": str(resolved), "reason": str(exc)})
            continue
        metric.update(audit)
        rows.append(metric)
        audits[plan_id] = audit
        seen.add(plan_id)
    for plan_id in sorted(set(plan) - seen):
        rejected.append({"source_dir": "", "reason": f"missing accepted plan row {plan_id}"})

    by_block: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_block.setdefault(int(row["block"]), {})[str(row["condition"])] = row
    pairs: list[dict[str, Any]] = []
    for block in range(1, 11):
        pair = by_block.get(block, {})
        if ON not in pair or OFF not in pair:
            continue
        on, off = pair[ON], pair[OFF]
        same_device = on["paused_device_id"] == off["paused_device_id"]
        same_prefault = (
            on["prefault_completed_candidates_by_device"] ==
            off["prefault_completed_candidates_by_device"]
        )
        same_initial_plan = on["initial_plan_signature"] == off["initial_plan_signature"]
        ratio = (float(on["t100_s"]) / float(off["t100_s"])
                 if float(off["t100_s"]) > 0 else math.nan)
        pairs.append({
            "block": block,
            "repair_on_run_id": on["run_id"],
            "repair_off_run_id": off["run_id"],
            "paused_device_on": on["paused_device_id"],
            "paused_device_off": off["paused_device_id"],
            "same_paused_device": same_device,
            "same_initial_plan_signature": same_initial_plan,
            "same_prefault_completed_sequence": same_prefault,
            "uac_on_s": on["uac_h_s"],
            "uac_off_s": off["uac_h_s"],
            "uac_on_minus_off_s": float(on["uac_h_s"]) - float(off["uac_h_s"]),
            "t100_on_s": on["t100_s"],
            "t100_off_s": off["t100_s"],
            "t100_on_over_off": ratio,
            "on_effective_replans": on["effective_replan_count"],
            "off_effective_replans": off["effective_replan_count"],
            "on_reassigned_before_recovery": on["reassigned_coverage_before_recovery_count"],
            "off_reassigned_before_recovery": off["reassigned_coverage_before_recovery_count"],
        })

    differences = [float(row["uac_on_minus_off_s"]) for row in pairs]
    ratios = [float(row["t100_on_over_off"]) for row in pairs
              if math.isfinite(float(row["t100_on_over_off"]))]
    uac_est, uac_low, uac_high = bootstrap_ci(
        differences, args.seed ^ 0xA611, args.bootstrap_samples
    )
    ratio_est, ratio_low, ratio_high = bootstrap_ci(
        ratios, args.seed ^ 0x7100, args.bootstrap_samples
    )
    sign_p = one_sided_sign_p_less(differences)

    complete = len(rows) == 20 and len(pairs) == 10 and not rejected
    same_device = len(pairs) == 10 and all(row["same_paused_device"] for row in pairs)
    same_initial_plan = len(pairs) == 10 and all(
        row["same_initial_plan_signature"] for row in pairs
    )
    mechanism = len(pairs) == 10 and all(
        int(row["on_effective_replans"]) >= 1 and
        int(row["on_reassigned_before_recovery"]) >= 1 and
        int(row["off_effective_replans"]) == 0 and
        int(row["off_reassigned_before_recovery"]) == 0
        for row in pairs
    )
    gates = {
        "complete_10_pairs": complete,
        "same_paused_device_within_every_pair": same_device,
        "same_initial_plan_signature_within_every_pair": same_initial_plan,
        "repair_on_uac_superior": (
            len(differences) == 10 and uac_est < 0 and uac_high < 0 and sign_p < 0.05
        ),
        "repair_on_t100_noninferior_5pct": len(ratios) == 10 and ratio_high <= 1.05,
        "repair_mechanism_observed_in_every_pair": mechanism,
    }
    result = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "plan_id": meta.get("plan_id"),
        "planned_runs": len(plan),
        "accepted_runs": len(rows),
        "rejected_or_missing": len(rejected),
        "bootstrap_samples": args.bootstrap_samples,
        "uac_on_minus_off_s": {
            "n": len(differences), "median": uac_est,
            "bootstrap_95ci": [uac_low, uac_high],
            "one_sided_exact_sign_p": sign_p,
        },
        "t100_on_over_off": {
            "n": len(ratios), "median": ratio_est,
            "bootstrap_95ci": [ratio_low, ratio_high],
            "noninferiority_margin": 1.05,
        },
        "gates": gates,
        "repair_ablation_passed": all(gates.values()),
        "inference_note": (
            "This independently registered follow-up isolates online repair; it does not replace "
            "or alter the original 50-run confirmatory results."
        ),
    }
    result["report_sha256"] = sha256_bytes(canonical_json(result))

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "run_metrics_wide.csv", rows, list(rows[0]) if rows else ["run_id"])
    pair_fields = list(pairs[0]) if pairs else ["block"]
    write_csv(output / "paired_repair_effects.csv", pairs, pair_fields)
    write_csv(output / "rejection_report.csv", rejected, ["source_dir", "reason"])
    write_json(output / "repair_ablation_decision.json", result)
    artifacts = (
        "run_metrics_wide.csv", "paired_repair_effects.csv",
        "rejection_report.csv", "repair_ablation_decision.json",
    )
    write_json(output / "analysis_manifest.sha256.json", {
        "schema_version": 1,
        "plan_sha256": sha256_file(args.plan.resolve()),
        "analysis_source_sha256": sha256_file(Path(__file__).resolve()),
        "artifacts": {name: sha256_file(output / name) for name in artifacts},
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["repair_ablation_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
