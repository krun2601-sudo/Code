#!/usr/bin/env python3
"""Audit timing and effects of every registered logical-pause run."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from analyze_runs import event_records, mission_start, mission_time, policy_diagnostics
from common import read_csv, write_csv, write_json


FIELDS = [
    "plan_run_id", "block", "pair_id", "method", "device_id",
    "configured_start_s", "observed_start_s", "configured_duration_s",
    "observed_duration_s", "coverage_at_start", "confirmed_at_start",
    "t100_s", "pause_began_before_t100", "fault_started_count",
    "fault_recovered_count", "replan_applied_count", "replan_reasons",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--output-prefix", required=True, type=Path)
    args = parser.parse_args()
    state = json.loads(args.state.read_text(encoding="utf-8"))
    metrics = {row["plan_run_id"]: row for row in read_csv(args.analysis / "run_metrics_wide.csv")}
    output: list[dict[str, Any]] = []
    for plan_run_id, record in state.get("runs", {}).items():
        metric = metrics.get(plan_run_id)
        if not metric or metric.get("condition") != "pause":
            continue
        run_dir = Path(record["run_dir"])
        cycles = read_csv(run_dir / "cycles.csv")
        events = event_records(read_csv(run_dir / "events.csv"), cycles)
        start_t = mission_start(events, cycles)
        starts = [event for event in events if event["type"] == "injection_started" and
                  event["payload"].get("injection_kind") == "logical_availability_pause"]
        ends = [event for event in events if event["type"] == "injection_ended" and
                event["payload"].get("injection_kind") == "logical_availability_pause"]
        if len(starts) != 1 or len(ends) != 1:
            raise ValueError(f"{plan_run_id}: expected one pause start/end, got {len(starts)}/{len(ends)}")
        begun = starts[0]
        ended = ends[0]
        observed_start = mission_time(begun, start_t)
        coverage = 0.0
        confirmed = 0
        for cycle in cycles:
            diag = policy_diagnostics(cycle)
            elapsed = float(diag.get("mission_elapsed_s", 0.0))
            if elapsed <= observed_start + 1e-9:
                coverage = float(diag.get("coverage_fraction", coverage))
                confirmed = int(diag.get("confirmed_count", confirmed))
            else:
                break
        relevant_faults = [event for event in events if event["payload"].get("reason") == "logical_pause"]
        replans = [event for event in events if event["type"] == "replan_applied"]
        t100 = float(metric["t100_s"])
        output.append({
            "plan_run_id": plan_run_id,
            "block": int(metric["block"]),
            "pair_id": metric["pair_id"],
            "method": metric["method"],
            "device_id": begun["payload"].get("device_id", ""),
            "configured_start_s": 15.0,
            "observed_start_s": observed_start,
            "configured_duration_s": float(begun["payload"]["configured_duration_s"]),
            "observed_duration_s": float(ended["payload"]["actual_duration_s"]),
            "coverage_at_start": coverage,
            "confirmed_at_start": confirmed,
            "t100_s": t100,
            "pause_began_before_t100": observed_start < t100,
            "fault_started_count": sum(event["type"] == "fault_started" for event in relevant_faults),
            "fault_recovered_count": sum(event["type"] == "fault_recovered" for event in relevant_faults),
            "replan_applied_count": len(replans),
            "replan_reasons": ";".join(str(event["payload"].get("reason", "")) for event in replans),
        })
    output.sort(key=lambda row: int(row["block"]) * 10 + (0 if row["method"] == "measured_static" else 1))
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_prefix.with_suffix(".csv"), output, FIELDS)
    durations = [float(row["observed_duration_s"]) for row in output]
    payload = {
        "schema": "zr10-logical-pause-audit-v1",
        "run_count": len(output),
        "all_have_one_start_and_recovery": all(
            row["fault_started_count"] == 1 and row["fault_recovered_count"] == 1
            for row in output
        ),
        "duration_s": {"min": min(durations), "max": max(durations)},
        "pause_before_t100_count": sum(bool(row["pause_began_before_t100"]) for row in output),
        "pause_after_t100_count": sum(not bool(row["pause_began_before_t100"]) for row in output),
        "ea_replan_counts": [row["replan_applied_count"] for row in output
                             if row["method"] == "full_eacml"],
        "measured_replan_counts": [row["replan_applied_count"] for row in output
                                   if row["method"] == "measured_static"],
        "rows": output,
    }
    write_json(args.output_prefix.with_suffix(".json"), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if len(output) == 12 and payload["all_have_one_start_and_recovery"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
