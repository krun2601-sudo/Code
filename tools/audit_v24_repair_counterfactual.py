#!/usr/bin/env python3
"""Replay the V2.3 pause decision state through the V2.4 repair rule.

The script is read-only with respect to the recorded hardware run.  It rebuilds
the deterministic initial queues, restores the measured trigger pose and
confirmed-cell set, and evaluates several preregistration candidate pause
windows.  Its output is development evidence only, never formal evidence.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algorithms.user_policy import ActiveTask, FaultState, MyPolicy, PauseInjection
from zr10lab.config import load_config
from zr10lab.models import PolicyContext, Telemetry


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def policy_events(cycles: list[dict[str, str]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in cycles:
        document = json.loads(row["diagnostics_json"])
        policy = document.get("policy", {})
        for event in policy.get("events", []):
            output.append({"cycle_t_s": float(row["t_s"]), "event": event})
    return output


def trigger_snapshot(cycles: list[dict[str, str]]) -> tuple[float, dict[str, Any]]:
    for row in cycles:
        document = json.loads(row["diagnostics_json"])
        policy = document.get("policy", {})
        if any(event.get("type") == "fault_started" and
               event.get("reason") == "logical_pause"
               for event in policy.get("events", [])):
            return float(row["t_s"]), policy
    raise ValueError("recorded run has no logical-pause trigger")


def telemetry_at(rows: list[dict[str, str]], cycle_t_s: float) -> tuple[float, dict[str, Telemetry]]:
    selected: dict[str, dict[str, str]] = {}
    for row in rows:
        distance = abs(float(row["t_s"]) - cycle_t_s)
        device_id = row["device_id"]
        current = selected.get(device_id)
        if current is None or distance < abs(float(current["t_s"]) - cycle_t_s):
            selected[device_id] = row
    if not selected:
        raise ValueError("telemetry stream is empty")
    context_t = max(float(row["sample_t"]) + float(row["age_s"]) for row in selected.values())
    states = {
        device_id: Telemetry(
            device_id=device_id,
            t=float(row["sample_t"]),
            yaw_deg=float(row["yaw_deg"]),
            pitch_deg=float(row["pitch_deg"]),
            roll_deg=float(row["roll_deg"]),
            zoom=float(row["zoom"]),
            yaw_rate_dps=float(row["yaw_rate_dps"]),
            pitch_rate_dps=float(row["pitch_rate_dps"]),
            connected=str(row["connected"]).strip().lower() == "true",
            sequence=int(row["sequence"]),
            source=row["source"],
            raw=json.loads(row["raw_json"]),
        )
        for device_id, row in selected.items()
    }
    return context_t, states


def candidate_from_attempt(attempt: str) -> str:
    if not attempt:
        return ""
    return attempt.removeprefix("task:").split("/attempt:", 1)[0]


def transition_records(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for wrapped in events:
        event = wrapped["event"]
        if event.get("type") != "transition_completed":
            continue
        task_id = str(event.get("task_id", ""))
        candidate_id = task_id.removeprefix("task:")
        records.setdefault(candidate_id, event)
    return records


def replay_once(config: Path, run_dir: Path, pause_duration_s: float) -> dict[str, Any]:
    cycles = read_rows(run_dir / "cycles.csv")
    telemetry = read_rows(run_dir / "telemetry.csv")
    events = policy_events(cycles)
    trigger_cycle_t, snapshot = trigger_snapshot(cycles)
    context_t, states = telemetry_at(telemetry, trigger_cycle_t)
    context = PolicyContext(context_t, 0.1, 0, states)

    policy = MyPolicy(load_config(config))
    initial = policy._make_plan(context, set(policy.reachable))
    policy.routes = policy._copy_routes(initial)
    observed_attempts = {
        device_id: candidate_from_attempt(str(attempt))
        for device_id, attempt in snapshot.get("attempt_by_device", {}).items()
    }
    fault_event = next(
        wrapped["event"] for wrapped in events
        if wrapped["event"].get("type") == "fault_started" and
        wrapped["event"].get("reason") == "logical_pause"
    )
    faulted_id = str(fault_event["device_id"])
    interrupted = str(fault_event.get("task_id", "")).removeprefix("task:")
    observed_attempts[faulted_id] = interrupted
    initial_heads = {
        device_id: (route[0] if route else "")
        for device_id, route in initial.items()
    }
    route_contains_recorded = all(
        not observed_attempts.get(device_id) or
        observed_attempts[device_id] in initial[device_id]
        for device_id in policy.device_ids
    )

    records = transition_records(events)
    for device_id in policy.device_ids:
        candidate_id = observed_attempts.get(device_id, "")
        if not candidate_id:
            policy.active[device_id] = None
            continue
        if candidate_id in policy.routes[device_id]:
            completed_prefix = policy.routes[device_id].index(candidate_id)
            policy.routes[device_id] = policy.routes[device_id][completed_prefix + 1:]
        candidate = policy.candidates[candidate_id]
        stage = str(snapshot.get("stage_by_device", {}).get(device_id, "MOVE"))
        record = records.get(candidate_id, {})
        predicted_s = float(record.get("predicted_s", 0.0))
        command_started_t = record.get("command_issued_t")
        if predicted_s <= 0.0:
            predicted_s, _ = policy._edge(
                device_id,
                (states[device_id].yaw_deg, states[device_id].pitch_deg),
                (candidate.yaw_deg, candidate.pitch_deg),
                True,
            )
        policy.active[device_id] = ActiveTask(
            candidate_id=candidate_id,
            task_id=f"task:{candidate_id}",
            attempt_id=f"task:{candidate_id}/attempt:001",
            stage=stage,
            assigned_t=context_t - float(snapshot.get("mission_elapsed_s", 0.0)),
            from_yaw_deg=states[device_id].yaw_deg,
            from_pitch_deg=states[device_id].pitch_deg,
            predicted_s=predicted_s,
            command_started_t=(float(command_started_t)
                               if command_started_t is not None else None),
        )

    policy.active[faulted_id] = None
    policy.faults = {
        faulted_id: FaultState(
            "logical_pause", context_t, candidate_id=interrupted,
            task_id=f"task:{interrupted}",
            attempt_id=f"task:{interrupted}/attempt:001",
        )
    }
    policy.logical_pause = PauseInjection(
        mode="coverage_fraction",
        duration_s=pause_duration_s,
        trigger_coverage_fraction=0.5,
        selection_rule="max_unique_reserved_coverage",
        selected_device_id=faulted_id,
        started_t=context_t,
    )
    policy.confirmed = {
        int(str(cell_id).split("_")[-1])
        for cell_id in snapshot.get("confirmed_cell_ids", [])
    }
    mission_elapsed = float(snapshot.get("mission_elapsed_s", 0.0))
    policy.mission_started_t = context_t - mission_elapsed
    policy.pending_replan = True
    policy.pending_replan_since_t = context_t
    policy.pending_replan_reason = "counterfactual_recorded_pause"
    generated: list[dict[str, Any]] = []
    policy._replan(context, replace_queues=True, events=generated)
    result = next(event for event in generated
                  if event["type"] in {"replan_applied", "replan_noop"})
    comparison = result["comparison"]
    witness = comparison.get("pre_recovery_witness", {})
    selected = witness.get("selected") or {}
    return {
        "pause_duration_s": pause_duration_s,
        "initial_routes_contain_recorded_active_tasks": route_contains_recorded,
        "initial_route_heads": initial_heads,
        "recorded_attempts": observed_attempts,
        "faulted_device_id": faulted_id,
        "event_type": result["type"],
        "effective": result["effective"],
        "decision": comparison.get("decision"),
        "planner_ms": result["planner_ms"],
        "evaluation_count": result["plan_audit"].get("evaluations"),
        "evaluation_limit": result["plan_audit"].get("evaluation_limit"),
        "budget_contract_satisfied": result["plan_audit"].get(
            "budget_contract_satisfied"
        ),
        "reassigned_cell_count": result["reassigned_cell_count"],
        "predicted_first_reassigned_cover_s": selected.get(
            "predicted_first_reassigned_cover_s", math.inf
        ),
        "recovery_window_s": selected.get("recovery_window_s", pause_duration_s),
        "comparison": comparison,
        "plan_audit": result["plan_audit"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--durations", type=float, nargs="+", default=[20, 25, 30, 35, 40])
    args = parser.parse_args()
    rows = [replay_once(args.config.resolve(), args.run_dir.resolve(), value)
            for value in args.durations]
    payload = {
        "schema_version": 1,
        "purpose": "development_only_counterfactual_repair_gate",
        "source_run": str(args.run_dir.resolve()),
        "config": str(args.config.resolve()),
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
