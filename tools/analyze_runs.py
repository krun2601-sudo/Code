#!/usr/bin/env python3
"""Validate and analyze independent real-hardware coverage runs.

Strict integrity gates are applied before a run enters any table: completed
session, intact CSV manifest, hardware mode, patched frame context, passing
preflight provenance, registered plan row, and exact configuration hashes.
Control-loop cycles are never treated as independent samples; one run produces
one row in the wide table and one observation per metric in the long table.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from common import (
    METHODS, PROTOCOL_VERSION, canonical_json, coefficient_of_variation,
    config_sha256, discover_run_dirs, hardware_config_sha256, median, parse_json,
    pair_frozen_config_sha256, percentile, read_csv, safe_mean, sha256_bytes, sha256_file, truthy, verify_manifest,
    wrap_delta_deg, write_csv, write_json,
)


RUN_FIELDS = [
    "run_id", "plan_run_id", "block", "pair_id", "method", "condition",
    "evaluation_cell_count", "reachable_count", "reachable_ratio",
    "evaluation_horizon_s", "mission_duration_s", "t95_s", "t95_reached", "t95_censored",
    "t100_s", "t100_reached", "t100_censored",
    "mean_first_coverage_s", "uac_h_s", "auc_uncovered_s", "coverage_final",
    "mission_success", "total_motion_deg", "load_cv", "load_cv_basis",
    "command_count", "ack_failure_count", "command_error_count",
    "unusable_frame_count", "deadline_miss_rate",
    "routing_prediction_mae_s", "routing_prediction_rmse_s", "routing_prediction_under_rate",
    "point_prediction_mae_s", "point_prediction_rmse_s", "point_prediction_under_rate",
    "upper_prediction_count", "upper_prediction_mae_s",
    "upper_prediction_rmse_s", "upper_prediction_miss_rate",
    "prediction_mae_s", "prediction_rmse_s", "prediction_under_rate",
    "fault_duration_median_s", "fault_duration_max_s",
    "recovery_response_median_s", "recovery_response_max_s",
    "reassigned_coverage_before_recovery_count", "planner_p95_ms", "frame_wait_median_s",
    "replan_count", "effective_replan_count", "replan_noop_count", "fault_count",
    "transition_count", "source_dir",
]

METRICS = (
    "t95_s", "t100_s", "mean_first_coverage_s", "uac_h_s",
    "coverage_final", "total_motion_deg", "load_cv",
    "ack_failure_count", "routing_prediction_mae_s", "routing_prediction_under_rate",
    "upper_prediction_miss_rate",
    "fault_duration_median_s", "recovery_response_median_s", "planner_p95_ms",
    "frame_wait_median_s",
    "replan_count", "effective_replan_count", "replan_noop_count", "fault_count",
)

BINARY_OUTCOMES = ("t95_reached", "t100_reached")
SECONDARY_HOLM_METRICS = {
    "t95_s", "total_motion_deg", "load_cv", "planner_p95_ms", "frame_wait_median_s",
}


def registered_methods(condition: str,
                       methods_by_condition: dict[str, tuple[str, ...]] | None = None) -> tuple[str, ...]:
    """Methods preregistered for each confirmatory condition."""
    if methods_by_condition is not None and condition in methods_by_condition:
        return methods_by_condition[condition]
    if condition == "nominal":
        return METHODS
    if condition == "pause":
        return ("measured_static", "full_eacml")
    raise ValueError(f"unsupported confirmatory condition: {condition!r}")


def methods_from_plan_metadata(meta: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    raw = meta.get("methods_by_condition")
    if isinstance(raw, dict):
        parsed = {
            str(condition): tuple(str(value) for value in values)
            for condition, values in raw.items()
            if isinstance(values, (list, tuple)) and values
        }
        if parsed:
            return parsed
    return {
        "nominal": tuple(str(value) for value in meta.get("nominal_methods", METHODS)),
        "pause": tuple(str(value) for value in meta.get(
            "pause_methods", ("measured_static", "full_eacml")
        )),
    }


class RejectRun(ValueError):
    pass


def load_plan(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if path.suffix.lower() == ".json":
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = document.get("rows", [])
        meta = {key: value for key, value in document.items() if key != "rows"}
    else:
        rows = read_csv(path)
        meta = {}
    expected_analysis_hash = str(meta.get("expected_analysis_sha256", "")).strip()
    if expected_analysis_hash and sha256_file(Path(__file__).resolve()) != expected_analysis_hash:
        raise ValueError("analysis source hash differs from the frozen plan")
    methods_by_condition = methods_from_plan_metadata(meta)
    mapping: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("plan_run_id", ""))
        if not key or key in mapping:
            raise ValueError(f"empty or duplicate plan_run_id: {key!r}")
        mapping[key] = dict(row)
    pairs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in mapping.values():
        pair_id = str(row.get("pair_id", ""))
        if not pair_id:
            raise ValueError("plan contains an empty pair_id")
        pairs[pair_id].append(row)
    shared_keys = (
        "block", "condition", "instance_seed", "injection_seed",
        "solver_seed",
        "runtime_duration_s",
        "condition_parameters_json", "expected_hardware_config_sha256",
        "expected_pair_frozen_sha256", "expected_evaluation_cell_count",
        "expected_policy_sha256", "expected_policy_version",
    )
    if any(str(row.get("expected_execution_model_sha256", "")).strip()
           for row in mapping.values()):
        shared_keys += ("expected_execution_model_sha256",)
    for pair_id, items in pairs.items():
        condition_values = {str(row.get("condition", "")) for row in items}
        if len(condition_values) != 1:
            raise ValueError(f"{pair_id}: condition is not frozen")
        condition = next(iter(condition_values))
        expected_methods = registered_methods(condition, methods_by_condition)
        if (len(items) != len(expected_methods) or
                {str(row.get("method")) for row in items} != set(expected_methods)):
            raise ValueError(
                f"{pair_id}: paired group must contain {expected_methods} exactly once"
            )
        for field in shared_keys:
            values = {str(row.get(field, "")) for row in items}
            if len(values) != 1 or "" in values:
                raise ValueError(f"{pair_id}: paired field {field} is missing or not frozen")
        if len({str(row.get("expected_config_sha256", "")) for row in items}) != len(expected_methods):
            raise ValueError(f"{pair_id}: method-specific full configuration hashes are not unique")
        try:
            positions = {int(row.get("within_pair_order", 0)) for row in items}
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{pair_id}: invalid within_pair_order") from exc
        if positions != set(range(1, len(expected_methods) + 1)):
            raise ValueError(
                f"{pair_id}: within_pair_order must be a permutation of 1..{len(expected_methods)}"
            )
    for condition in {str(row.get("condition")) for row in mapping.values()}:
        expected_methods = registered_methods(condition, methods_by_condition)
        for method in expected_methods:
            counts = [sum(str(row.get("condition")) == condition and str(row.get("method")) == method and
                          int(row.get("within_pair_order", 0)) == position
                          for row in mapping.values())
                      for position in range(1, len(expected_methods) + 1)]
            if max(counts) - min(counts) > 1:
                raise ValueError(f"plan method order is imbalanced for {condition}/{method}: {counts}")
    return mapping, meta


def finite_or_nan(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def first_value(mapping: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return default


def policy_diagnostics(cycle: dict[str, str]) -> dict[str, Any]:
    document = parse_json(cycle.get("diagnostics_json"), {})
    policy = document.get("policy", document)
    return policy if isinstance(policy, dict) else {}


def event_records(rows: list[dict[str, str]], cycles: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows:
        payload = parse_json(row.get("payload_json"), {})
        if not isinstance(payload, dict):
            payload = {"value": payload}
        event_type = str(payload.get("type", payload.get("event", row.get("kind", ""))))
        elapsed = finite_or_nan(first_value(payload, ("mission_elapsed_s", "elapsed_s")))
        events.append({
            "session_t_s": finite_or_nan(row.get("t_s")),
            "mission_elapsed_s": elapsed,
            "kind": row.get("kind", ""),
            "type": event_type,
            "payload": payload,
        })
    # The platform patch keeps policy events inside Decision.diagnostics so the
    # same event is not written twice by two logging paths.
    for cycle in cycles or []:
        diag = policy_diagnostics(cycle)
        embedded = diag.get("events", [])
        if isinstance(embedded, dict):
            embedded = [embedded]
        if not isinstance(embedded, list):
            raise RejectRun("diagnostics.policy.events must be a list")
        for payload in embedded:
            if not isinstance(payload, dict):
                raise RejectRun("diagnostics.policy.events item must be a mapping")
            event_type = str(payload.get("type", payload.get("event", "")))
            elapsed = finite_or_nan(first_value(payload, ("mission_elapsed_s", "elapsed_s")))
            events.append({
                "session_t_s": finite_or_nan(cycle.get("t_s")),
                "mission_elapsed_s": elapsed,
                "kind": "policy_diagnostic_event",
                "type": event_type,
                "payload": payload,
            })
    # Compatibility with early prototypes that duplicated an event into both
    # channels: exact semantic duplicates count only once.
    unique: list[dict[str, Any]] = []
    seen: set[bytes] = set()
    for event in events:
        signature = canonical_json({"type": event["type"], "payload": event["payload"]})
        if signature not in seen:
            seen.add(signature)
            unique.append(event)
    return unique


def mission_start(events: list[dict[str, Any]], cycles: list[dict[str, str]]) -> float:
    starts = [event["session_t_s"] for event in events if event["type"] == "mission_started" and math.isfinite(event["session_t_s"])]
    if starts:
        return min(starts)
    for row in cycles:
        diag = policy_diagnostics(row)
        elapsed = finite_or_nan(diag.get("mission_elapsed_s"))
        t_s = finite_or_nan(row.get("t_s"))
        if math.isfinite(elapsed) and math.isfinite(t_s):
            return t_s - elapsed
    raise RejectRun("mission start is not logged")


def mission_time(event: dict[str, Any], start_t: float) -> float:
    if math.isfinite(event["mission_elapsed_s"]):
        return max(0.0, float(event["mission_elapsed_s"]))
    return max(0.0, float(event["session_t_s"]) - start_t)


def configured_evaluation_cell_count(config: dict[str, Any]) -> int:
    grid = config.get("policy", {}).get("evaluation_grid", {})
    shape = grid.get("shape") if isinstance(grid, dict) else None
    declared = grid.get("cell_count") if isinstance(grid, dict) else None
    if not (isinstance(shape, list) and len(shape) == 3 and
            all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in shape)):
        raise RejectRun("policy.evaluation_grid.shape must contain three positive integers")
    computed = math.prod(shape)
    if not isinstance(declared, int) or isinstance(declared, bool) or declared != computed:
        raise RejectRun(f"evaluation_grid.cell_count={declared!r} does not match product(shape)={computed}")
    if computed != 384:
        raise RejectRun(f"confirmatory evaluation task must use preregistered M=384, got {computed}")
    alias = config.get("policy", {}).get("scan_grid")
    if alias is not None and alias != shape:
        raise RejectRun("deprecated scan_grid alias differs from evaluation_grid.shape")
    return computed


def coverage_curve(cycles: list[dict[str, str]], start_t: float) -> tuple[list[tuple[float, float]], list[float], dict[str, Any]]:
    points: list[tuple[float, float]] = [(0.0, 0.0)]
    planner_ms: list[float] = []
    previous_fraction = 0.0
    fixed_reachable: int | None = None
    fixed_evaluation: int | None = None
    fixed_reachable_ratio: float | None = None
    for row in cycles:
        diag = policy_diagnostics(row)
        elapsed = finite_or_nan(diag.get("mission_elapsed_s"))
        if not math.isfinite(elapsed):
            session_t = finite_or_nan(row.get("t_s"))
            if not math.isfinite(session_t) or session_t < start_t:
                continue
            elapsed = session_t - start_t
        planner = finite_or_nan(first_value(diag, ("planner_ms", "planning_ms")))
        if math.isfinite(planner) and planner >= 0:
            planner_ms.append(planner)
        fraction = finite_or_nan(first_value(diag, ("coverage_fraction", "confirmed_fraction", "execution_coverage")))
        confirmed = first_value(diag, ("confirmed_count", "confirmed_cell_count"))
        reachable = first_value(diag, ("reachable_count", "reachable_cell_count", "total_reachable_cells"))
        evaluation = first_value(diag, ("evaluation_cell_count", "total_cell_count", "M"))
        reachable_ratio = finite_or_nan(diag.get("reachable_ratio"))
        ids = diag.get("confirmed_cell_ids")
        if confirmed is None and isinstance(ids, list):
            confirmed = len(set(str(value) for value in ids))
        if evaluation is not None:
            evaluation_int = int(evaluation)
            if evaluation_int <= 0:
                raise RejectRun("nonpositive evaluation cell count")
            if fixed_evaluation is None:
                fixed_evaluation = evaluation_int
            elif fixed_evaluation != evaluation_int:
                raise RejectRun(f"evaluation cell count changed from {fixed_evaluation} to {evaluation_int}")
        if reachable is not None:
            reachable_int = int(reachable)
            if reachable_int <= 0:
                raise RejectRun("nonpositive reachable cell count")
            if fixed_reachable is None:
                fixed_reachable = reachable_int
            elif fixed_reachable != reachable_int:
                raise RejectRun(f"reachable cell count changed from {fixed_reachable} to {reachable_int}")
        if math.isfinite(reachable_ratio):
            if fixed_reachable_ratio is None:
                fixed_reachable_ratio = reachable_ratio
            elif abs(fixed_reachable_ratio - reachable_ratio) > 1e-9:
                raise RejectRun("reachable_ratio changed within a run")
        if fixed_evaluation is not None and fixed_reachable is not None:
            expected_ratio = fixed_reachable / fixed_evaluation
            if fixed_evaluation != 384 or fixed_reachable != fixed_evaluation:
                raise RejectRun("confirmatory run requires reachable_count == evaluation_cell_count == 384")
            if fixed_reachable_ratio is None or abs(fixed_reachable_ratio - expected_ratio) > 1e-6:
                raise RejectRun("reachable_ratio is missing or inconsistent with reachable_count/M")
        if confirmed is not None and fixed_evaluation is not None:
            confirmed_int = int(confirmed)
            if not 0 <= confirmed_int <= fixed_evaluation:
                raise RejectRun("confirmed_count is outside [0, evaluation_cell_count]")
        if not math.isfinite(fraction) and confirmed is not None and fixed_evaluation:
            fraction = float(confirmed) / fixed_evaluation
        if math.isfinite(fraction) and confirmed is not None and fixed_evaluation:
            expected_fraction = float(confirmed) / fixed_evaluation
            if abs(fraction - expected_fraction) > 1e-6:
                raise RejectRun("coverage_fraction is inconsistent with confirmed_count/evaluation_cell_count")
        if not math.isfinite(fraction):
            continue
        if not -1e-9 <= fraction <= 1 + 1e-9:
            raise RejectRun(f"coverage fraction outside [0,1]: {fraction}")
        fraction = min(1.0, max(0.0, fraction))
        if fraction + 1e-9 < previous_fraction:
            raise RejectRun(f"confirmed coverage regressed from {previous_fraction} to {fraction}")
        previous_fraction = max(previous_fraction, fraction)
        if elapsed + 1e-9 < points[-1][0]:
            raise RejectRun("mission elapsed time is not monotone")
        if abs(elapsed - points[-1][0]) <= 1e-12:
            points[-1] = (float(elapsed), previous_fraction)
        else:
            points.append((float(elapsed), previous_fraction))
    if len(points) == 1:
        raise RejectRun("no execution-confirmed coverage diagnostics")
    if fixed_evaluation is None or fixed_reachable is None or fixed_reachable_ratio is None:
        raise RejectRun("coverage diagnostics must record evaluation_cell_count, reachable_count, and reachable_ratio")
    if fixed_evaluation != 384 or fixed_reachable != 384 or abs(fixed_reachable_ratio - 1.0) > 1e-6:
        raise RejectRun("confirmatory run requires all 384/384 evaluation cells to be reachable")
    return points, planner_ms, {
        "evaluation_cell_count": fixed_evaluation,
        "reachable_count": fixed_reachable,
        "reachable_ratio": fixed_reachable_ratio,
    }


def first_threshold(points: list[tuple[float, float]], threshold: float) -> float:
    for elapsed, fraction in points:
        if fraction + 1e-12 >= threshold:
            return elapsed
    return math.nan


def uncovered_auc(points: list[tuple[float, float]], end_time: float) -> float:
    area = 0.0
    previous_t, previous_fraction = points[0]
    for elapsed, fraction in points[1:]:
        stop = min(elapsed, end_time)
        if stop > previous_t:
            area += (stop - previous_t) * (1.0 - previous_fraction)
        previous_t, previous_fraction = elapsed, fraction
        if elapsed >= end_time:
            return area
    if end_time > previous_t:
        area += (end_time - previous_t) * (1.0 - previous_fraction)
    return area


def actual_motion(rows: list[dict[str, str]], start_t: float, end_t: float) -> dict[str, float]:
    by_device: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for row in rows:
        t_s = finite_or_nan(row.get("t_s"))
        yaw = finite_or_nan(row.get("yaw_deg"))
        pitch = finite_or_nan(row.get("pitch_deg"))
        if math.isfinite(t_s) and math.isfinite(yaw) and math.isfinite(pitch) and start_t <= t_s <= end_t:
            by_device[str(row.get("device_id", ""))].append((t_s, yaw, pitch))
    result: dict[str, float] = {}
    for device_id, samples in by_device.items():
        samples.sort()
        travel = 0.0
        for (_, yaw0, pitch0), (_, yaw1, pitch1) in zip(samples, samples[1:]):
            delta_yaw = wrap_delta_deg(yaw1, yaw0) * math.cos(math.radians((pitch0 + pitch1) / 2))
            travel += math.hypot(delta_yaw, pitch1 - pitch0)
        result[device_id] = travel
    return result


def event_metrics(events: list[dict[str, Any]], start_t: float) -> dict[str, Any]:
    transitions = [event for event in events if event["type"] == "transition_completed"]
    routing_prediction_errors: list[float] = []
    point_prediction_errors: list[float] = []
    upper_prediction_errors: list[float] = []
    frame_wait_values: list[float] = []
    transition_counts: dict[str, int] = defaultdict(int)
    for event in transitions:
        payload = event["payload"]
        device_id = str(payload.get("device_id", ""))
        if device_id:
            transition_counts[device_id] += 1
        if payload.get("time_definition") != "command_issued_to_frame_confirm":
            raise RejectRun("transition_completed has a missing or unsupported time_definition")
        command_t = finite_or_nan(payload.get("command_issued_t"))
        frame_t = finite_or_nan(payload.get("frame_confirm_t"))
        actual = finite_or_nan(payload.get("actual_s"))
        if not (math.isfinite(command_t) and math.isfinite(frame_t) and frame_t > command_t and
                math.isfinite(actual) and actual > 0):
            raise RejectRun("transition_completed lacks valid command/frame timestamps or actual_s")
        if abs(actual - (frame_t - command_t)) > 1e-3:
            raise RejectRun("transition actual_s differs from frame_confirm_t-command_issued_t by more than 1 ms")
        prediction_kind = str(payload.get("prediction_kind", "conformal_upper"))
        if prediction_kind not in {"conformal_upper", "nominal_fallback_upper", "nominal_end_to_end"}:
            raise RejectRun(f"unsupported transition prediction_kind={prediction_kind!r}")
        predicted = finite_or_nan(first_value(payload, ("upper_prediction_s", "predicted_s", "predicted_time_s")))
        point_prediction = finite_or_nan(payload.get("point_prediction_s", predicted))
        padding = finite_or_nan(payload.get("conformal_padding_s", 0.0))
        if not (math.isfinite(predicted) and predicted >= 0 and math.isfinite(point_prediction) and
                point_prediction >= 0 and math.isfinite(padding) and padding >= 0):
            raise RejectRun("transition_completed lacks finite non-negative prediction fields")
        if prediction_kind == "conformal_upper" and abs(predicted - (point_prediction + padding)) > 1e-6:
            raise RejectRun("conformal upper prediction differs from point_prediction_s+conformal_padding_s")
        routing_prediction_errors.append(actual - predicted)
        point_prediction_errors.append(actual - point_prediction)
        if prediction_kind == "conformal_upper":
            upper_prediction_errors.append(actual - predicted)
        frame_wait = finite_or_nan(payload.get("frame_wait_s"))
        if math.isfinite(frame_wait) and frame_wait >= 0:
            frame_wait_values.append(frame_wait)
    starts: dict[str, list[float]] = defaultdict(list)
    fault_durations: list[float] = []
    recovery_responses: list[float] = []
    planner_values: list[float] = []
    reassigned_before_recovery = 0
    for event in sorted(events, key=lambda item: mission_time(item, start_t)):
        payload = event["payload"]
        key = str(payload.get("fault_id", payload.get("device_id", "global")))
        when = mission_time(event, start_t)
        if event["type"] == "fault_started":
            starts[key].append(when)
        elif event["type"] == "fault_recovered" and starts[key]:
            begun = starts[key].pop(0)
            if when >= begun:
                fault_durations.append(when - begun)
        elif event["type"] == "reassigned_coverage_confirmed":
            response = finite_or_nan(payload.get("response_latency_s"))
            if math.isfinite(response) and response >= 0:
                recovery_responses.append(response)
            if bool(payload.get("before_original_device_recovery", False)):
                reassigned_before_recovery += 1
        planner = finite_or_nan(payload.get("planner_ms"))
        if math.isfinite(planner) and planner >= 0:
            planner_values.append(planner)
    return {
        "routing_prediction_errors": routing_prediction_errors,
        "point_prediction_errors": point_prediction_errors,
        "upper_prediction_errors": upper_prediction_errors,
        "frame_wait_values": frame_wait_values,
        "fault_durations": fault_durations,
        "recovery_responses": recovery_responses,
        "planner_values": planner_values,
        "reassigned_coverage_before_recovery_count": reassigned_before_recovery,
        "transition_counts": dict(transition_counts),
        "transition_count": len(transitions),
        "replan_count": sum(event["type"] in {
            "replan", "replan_started", "residual_replan", "replan_applied", "replan_noop"
        } for event in events),
        "effective_replan_count": sum(
            event["type"] == "replan_applied" and bool(event["payload"].get("effective", True))
            for event in events
        ),
        "replan_noop_count": sum(event["type"] == "replan_noop" for event in events),
        "fault_count": sum(event["type"] == "fault_started" for event in events),
    }


def validate_context(context: dict[str, Any], planned: dict[str, Any]) -> dict[str, Any]:
    stored = context.get("context_sha256")
    clean = dict(context)
    clean.pop("context_sha256", None)
    if stored != sha256_bytes(canonical_json(clean)):
        raise RejectRun("run_context self-hash mismatch")
    context_row = context.get("plan_row", {})
    if context_row.get("plan_run_id") != planned.get("plan_run_id"):
        raise RejectRun("run_context plan_run_id mismatch")
    if context.get("operator_arm_authorization") is not True:
        raise RejectRun("run_context does not record explicit operator arm authorization")
    preflight = context.get("preflight_summary", {})
    if preflight.get("policy_source_sha256") != planned.get("expected_policy_sha256"):
        raise RejectRun("preflight policy source hash differs from registered plan")
    if preflight.get("policy_algorithm_version") != planned.get("expected_policy_version"):
        raise RejectRun("preflight policy version differs from registered plan")
    expected_model_hash = str(planned.get("expected_execution_model_sha256", "")).strip()
    if expected_model_hash and preflight.get("execution_model_sha256") != expected_model_hash:
        raise RejectRun("preflight execution-model hash differs from registered plan")
    for key in ("expected_config_sha256", "expected_hardware_config_sha256",
                "expected_pair_frozen_sha256", "instance_seed", "injection_seed",
                "solver_seed", "method", "condition", "pair_id"):
        if str(context_row.get(key)) != str(planned.get(key)):
            raise RejectRun(f"run_context differs from registered plan at {key}")
    if preflight.get("ready_to_move") is not True or preflight.get("static_pass") is not True or preflight.get("live_pass") is not True:
        raise RejectRun("preflight provenance is not a full pass")
    if preflight.get("phase") != "experiment":
        raise RejectRun("run context is not bound to an experiment-phase preflight")
    if preflight.get("config_sha256") != planned.get("expected_config_sha256"):
        raise RejectRun("preflight full configuration hash mismatch")
    if preflight.get("hardware_config_sha256") != planned.get("expected_hardware_config_sha256"):
        raise RejectRun("preflight hardware hash mismatch")
    task = preflight.get("coverage_task_observed")
    if not isinstance(task, dict):
        raise RejectRun("preflight summary lacks observed coverage-task evidence")
    try:
        observed_m = int(task["evaluation_cell_count"])
        expected_m = int(planned["expected_evaluation_cell_count"])
        reachable = int(task["reachable_count"])
        ratio = float(task["reachable_ratio"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RejectRun(f"invalid preflight coverage-task evidence: {exc}") from exc
    if observed_m != expected_m or observed_m != 384:
        raise RejectRun("preflight evaluation-cell count differs from plan")
    if reachable != observed_m or abs(ratio - 1.0) > 1e-6:
        raise RejectRun("preflight did not establish reachability of all 384 evaluation cells")
    return task


def analyze_one(run_dir: Path, planned: dict[str, Any]) -> dict[str, Any]:
    problems = verify_manifest(run_dir)
    if problems:
        raise RejectRun("; ".join(problems))
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("completed") is not True:
        raise RejectRun("metadata.completed != true")
    if metadata.get("mode") == "sim":
        raise RejectRun("simulation session is not hardware evidence")
    if not metadata.get("platform_patch_version"):
        raise RejectRun("missing platform_patch_version")
    config = metadata.get("config")
    if not isinstance(config, dict):
        raise RejectRun("metadata.config missing")
    if config_sha256(config) != planned.get("expected_config_sha256"):
        raise RejectRun("full configuration hash differs from plan")
    if hardware_config_sha256(config) != planned.get("expected_hardware_config_sha256"):
        raise RejectRun("hardware configuration hash differs from plan")
    if pair_frozen_config_sha256(config) != planned.get("expected_pair_frozen_sha256"):
        raise RejectRun("paired task/injection hash differs from plan")
    context_path = run_dir / "run_context.json"
    if not context_path.is_file():
        raise RejectRun("missing run_context.json")
    preflight_task = validate_context(json.loads(context_path.read_text(encoding="utf-8")), planned)
    required = ("cycles.csv", "events.csv", "telemetry.csv", "commands.csv", "frames.csv")
    for name in required:
        path = run_dir / name
        if not path.is_file() or path.stat().st_size <= 10:
            raise RejectRun(f"missing or empty {name}")
    cycles = read_csv(run_dir / "cycles.csv")
    observed_policy_versions = {
        str(policy_diagnostics(row).get("algorithm_version", "")) for row in cycles
    }
    if observed_policy_versions != {str(planned.get("expected_policy_version", ""))}:
        raise RejectRun(
            f"runtime policy versions {sorted(observed_policy_versions)} differ from plan"
        )
    events = event_records(read_csv(run_dir / "events.csv"), cycles)
    start_t = mission_start(events, cycles)
    points, planner_values, coverage_task = coverage_curve(cycles, start_t)
    configured_m = configured_evaluation_cell_count(config)
    if coverage_task["evaluation_cell_count"] != configured_m:
        raise RejectRun(
            f"logged evaluation M={coverage_task['evaluation_cell_count']} differs from configured M={configured_m}"
        )
    if (coverage_task["reachable_count"] != int(preflight_task["reachable_count"]) or
            abs(coverage_task["reachable_ratio"] - float(preflight_task["reachable_ratio"])) > 1e-6):
        raise RejectRun("logged reachable task set differs from the bound preflight record")
    completion_events = [mission_time(event, start_t) for event in events if event["type"] == "mission_completed"]
    session_end = max(finite_or_nan(row.get("t_s")) for row in cycles)
    duration = max(points[-1][0], max(completion_events, default=0.0), session_end - start_t)
    experiment_options = config.get("policy", {}).get("experiment", {})
    horizon = finite_or_nan(experiment_options.get("horizon_s", config.get("system", {}).get("duration_s")))
    if not math.isfinite(horizon) or horizon <= 0:
        raise RejectRun("positive fixed evaluation horizon is missing")
    raw_t95 = first_threshold(points, 0.95)
    raw_t100 = first_threshold(points, 1.0)
    t95 = raw_t95 if math.isfinite(raw_t95) and raw_t95 <= horizon + 1e-9 else math.nan
    t100 = raw_t100 if math.isfinite(raw_t100) and raw_t100 <= horizon + 1e-9 else math.nan
    t95_reached = math.isfinite(t95)
    t100_reached = math.isfinite(t100)
    dt_values = [finite_or_nan(row.get("dt_s")) for row in cycles]
    tolerance = max([value for value in dt_values if math.isfinite(value) and value > 0] or [0.1]) * 2
    if not t100_reached and duration + tolerance < horizon:
        raise RejectRun(f"censored mission has only {duration:.3f}s follow-up; fixed horizon is {horizon:.3f}s")
    coverage_at_horizon = 0.0
    for elapsed, fraction in points:
        if elapsed <= horizon + 1e-9:
            coverage_at_horizon = fraction
        else:
            break
    if t100_reached:
        if not any(abs(value - t100) <= tolerance and value <= horizon + tolerance for value in completion_events):
            raise RejectRun("T100 was reached but no time-consistent mission_completed event was logged")
    elif completion_events:
        raise RejectRun("mission_completed was logged without execution-confirmed T100")
    mission_success = t100_reached
    uac_h = uncovered_auc(points, horizon)
    mean_first = uac_h if t100_reached else math.nan

    telemetry = read_csv(run_dir / "telemetry.csv")
    motion = actual_motion(telemetry, start_t, start_t + duration)
    if len(motion) != 4:
        raise RejectRun(f"motion integration has {len(motion)} devices, expected 4")
    event_summary = event_metrics(events, start_t)
    planner_values.extend(event_summary["planner_values"])
    load_values = list(event_summary["transition_counts"].values())
    load_basis = "confirmed_transitions"
    if len(load_values) != 4 or sum(load_values) == 0:
        load_values = list(motion.values())
        load_basis = "actual_motion_fallback"

    commands = read_csv(run_dir / "commands.csv")
    ack_failures = sum(truthy(row.get("sent")) and not truthy(row.get("ack")) for row in commands)
    command_errors = sum(bool(str(row.get("error", "")).strip()) or str(row.get("status", "")).lower() in {"error", "failed", "rejected", "timeout"} for row in commands)
    frames = read_csv(run_dir / "frames.csv")
    unusable = sum(not truthy(row.get("usable")) or not truthy(row.get("resolution_ok")) for row in frames)
    deadline_rate = safe_mean(truthy(row.get("deadline_miss")) for row in cycles)
    routing_errors = event_summary["routing_prediction_errors"]
    point_errors = event_summary["point_prediction_errors"]
    upper_errors = event_summary["upper_prediction_errors"]
    frame_wait_values = event_summary["frame_wait_values"]
    fault_durations = event_summary["fault_durations"]
    recovery_responses = event_summary["recovery_responses"]
    if (planned["method"] == "uac_robust_repair_v2" or
            str(planned.get("protocol_version", "")).startswith("ral-hil-2")):
        coverage_events = [event for event in events if event["type"] == "coverage_confirmed"]
        if not coverage_events:
            raise RejectRun("V2 run has no coverage_confirmed evidence events")
        configured_gap = float(config.get("policy", {}).get("execution_confirmation", {}).get(
            "frame_pose_max_gap_s", config.get("system", {}).get("pose_max_gap_s", 0.2)))
        for event in coverage_events:
            payload = event["payload"]
            if payload.get("evidence_mode") != "measured_pose_fresh_frame_projection":
                raise RejectRun("V2 coverage event is not measured-pose fresh-frame evidence")
            if payload.get("frame_id") in (None, ""):
                raise RejectRun("V2 coverage event lacks frame_id")
            gap = finite_or_nan(payload.get("frame_pose_gap_s"))
            if not math.isfinite(gap) or gap < 0 or gap > configured_gap + 1e-9:
                raise RejectRun("V2 coverage event violates frame/pose synchronization bound")
    if planned["method"] == "uac_robust_repair_v2":
        initial_plans = [event for event in events if event["type"] == "plan_completed"]
        if not initial_plans:
            raise RejectRun("V2 run lacks an auditable initial plan_completed event")
        audit = initial_plans[0]["payload"].get("plan_audit", {})
        baseline = audit.get("baseline_objective", {}) if isinstance(audit, dict) else {}
        final = audit.get("final_objective", {}) if isinstance(audit, dict) else {}
        baseline_uac = finite_or_nan(baseline.get("uac_h_s"))
        baseline_t100 = finite_or_nan(baseline.get("t100_s"))
        final_uac = finite_or_nan(final.get("uac_h_s"))
        final_t100 = finite_or_nan(final.get("t100_s"))
        if (not all(math.isfinite(value) for value in (
                baseline_uac, baseline_t100, final_uac, final_t100)) or
                int(final.get("uncovered_count", -1)) != 0 or
                final_uac > baseline_uac + 1e-9 or final_t100 > baseline_t100 + 1e-9 or
                audit.get("uac_non_regression_verified") is not True or
                audit.get("t100_non_regression_verified") is not True or
                audit.get("evaluation_limit_respected") is not True or
                audit.get("budget_contract_satisfied") is not True or
                audit.get("search_stop_reason") == "wall_clock_deadline"):
            raise RejectRun("V2 initial plan does not establish logged MAG non-regression")
        for event in events:
            if event["type"] != "replan_applied":
                continue
            comparison = event["payload"].get("comparison", {})
            continuation = comparison.get("continuation_objective", {})
            proposal = comparison.get("proposal_objective", {})
            minimum_gain = finite_or_nan(comparison.get("minimum_uac_gain_s"))
            continuation_uac = finite_or_nan(continuation.get("uac_h_s"))
            continuation_t100 = finite_or_nan(continuation.get("t100_s"))
            proposal_uac = finite_or_nan(proposal.get("uac_h_s"))
            proposal_t100 = finite_or_nan(proposal.get("t100_s"))
            witness = comparison.get("pre_recovery_witness", {})
            repair_audit = event["payload"].get("plan_audit", {})
            if (not all(math.isfinite(value) for value in (
                    minimum_gain, continuation_uac, continuation_t100,
                    proposal_uac, proposal_t100)) or
                    continuation_uac - proposal_uac <= minimum_gain or
                    proposal_t100 > continuation_t100 + 1e-9 or
                    comparison.get("decision") !=
                    "accepted_strict_uac_gain_t100_nonincrease_with_pre_recovery_witness" or
                    not isinstance(witness, dict) or
                    witness.get("feasible_before_recovery") is not True or
                    not isinstance(repair_audit, dict) or
                    repair_audit.get("evaluation_limit_respected") is not True or
                    repair_audit.get("budget_contract_satisfied") is not True):
                raise RejectRun("V2 effective repair violates its logged non-regression rule")
    run_id = str(metadata.get("run_id", run_dir.name))
    return {
        "run_id": run_id,
        "plan_run_id": planned["plan_run_id"],
        "block": int(planned["block"]),
        "pair_id": planned["pair_id"],
        "method": planned["method"],
        "condition": planned["condition"],
        "evaluation_cell_count": coverage_task["evaluation_cell_count"],
        "reachable_count": coverage_task["reachable_count"],
        "reachable_ratio": coverage_task["reachable_ratio"],
        "evaluation_horizon_s": horizon,
        "mission_duration_s": duration,
        "t95_s": t95,
        "t95_reached": t95_reached,
        "t95_censored": not t95_reached,
        "t100_s": t100,
        "t100_reached": t100_reached,
        "t100_censored": not t100_reached,
        "mean_first_coverage_s": mean_first,
        "uac_h_s": uac_h,
        "auc_uncovered_s": uac_h,
        "coverage_final": coverage_at_horizon,
        "mission_success": mission_success,
        "total_motion_deg": sum(motion.values()),
        "load_cv": coefficient_of_variation(load_values),
        "load_cv_basis": load_basis,
        "command_count": len(commands),
        "ack_failure_count": ack_failures,
        "command_error_count": command_errors,
        "unusable_frame_count": unusable,
        "deadline_miss_rate": deadline_rate,
        "routing_prediction_mae_s": safe_mean(abs(error) for error in routing_errors),
        "routing_prediction_rmse_s": math.sqrt(safe_mean(error * error for error in routing_errors)) if routing_errors else math.nan,
        "routing_prediction_under_rate": safe_mean(error > 0 for error in routing_errors),
        "point_prediction_mae_s": safe_mean(abs(error) for error in point_errors),
        "point_prediction_rmse_s": math.sqrt(safe_mean(error * error for error in point_errors)) if point_errors else math.nan,
        "point_prediction_under_rate": safe_mean(error > 0 for error in point_errors),
        "upper_prediction_count": len(upper_errors),
        "upper_prediction_mae_s": safe_mean(abs(error) for error in upper_errors),
        "upper_prediction_rmse_s": math.sqrt(safe_mean(error * error for error in upper_errors)) if upper_errors else math.nan,
        "upper_prediction_miss_rate": safe_mean(error > 0 for error in upper_errors),
        # Deprecated generic aliases map to the prediction actually used by
        # the route planner, which may be conformal or nominal by method.
        "prediction_mae_s": safe_mean(abs(error) for error in routing_errors),
        "prediction_rmse_s": math.sqrt(safe_mean(error * error for error in routing_errors)) if routing_errors else math.nan,
        "prediction_under_rate": safe_mean(error > 0 for error in routing_errors),
        "fault_duration_median_s": median(fault_durations),
        "fault_duration_max_s": max(fault_durations) if fault_durations else math.nan,
        "recovery_response_median_s": median(recovery_responses),
        "recovery_response_max_s": max(recovery_responses) if recovery_responses else math.nan,
        "reassigned_coverage_before_recovery_count": event_summary["reassigned_coverage_before_recovery_count"],
        "planner_p95_ms": percentile(planner_values, 0.95),
        "frame_wait_median_s": median(frame_wait_values),
        "replan_count": event_summary["replan_count"],
        "effective_replan_count": event_summary["effective_replan_count"],
        "replan_noop_count": event_summary["replan_noop_count"],
        "fault_count": event_summary["fault_count"],
        "transition_count": event_summary["transition_count"],
        "source_dir": str(run_dir),
    }


def metric_values(rows: list[dict[str, Any]], metric: str) -> list[float]:
    values = [finite_or_nan(row.get(metric)) for row in rows]
    return [value for value in values if math.isfinite(value)]


def main_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["method"]), str(row["condition"]))].append(row)
    output: list[dict[str, Any]] = []
    for (method, condition), items in sorted(groups.items()):
        entry: dict[str, Any] = {
            "method": method,
            "condition": condition,
            "n_runs": len(items),
            "mission_success_rate": safe_mean(bool(row["mission_success"]) for row in items),
            "t95_reached_rate": safe_mean(bool(row["t95_reached"]) for row in items),
            "t95_censored_n": sum(bool(row["t95_censored"]) for row in items),
            "t100_reached_rate": safe_mean(bool(row["t100_reached"]) for row in items),
            "t100_censored_n": sum(bool(row["t100_censored"]) for row in items),
        }
        for metric in METRICS:
            values = metric_values(items, metric)
            entry[f"{metric}_n"] = len(values)
            entry[f"{metric}_median"] = median(values)
            entry[f"{metric}_q1"] = percentile(values, 0.25)
            entry[f"{metric}_q3"] = percentile(values, 0.75)
        output.append(entry)
    return output


def paired_differences(rows: list[dict[str, Any]], metric: str, condition: str,
                       baseline: str, primary_method: str = "full_eacml") -> list[float]:
    pairs: dict[str, dict[str, float]] = defaultdict(dict)
    for row in rows:
        if row["condition"] != condition:
            continue
        value = finite_or_nan(row.get(metric))
        if math.isfinite(value):
            pairs[str(row["pair_id"])][str(row["method"])] = value
    return [values[primary_method] - values[baseline] for values in pairs.values()
            if primary_method in values and baseline in values]


def bootstrap_ci(values: list[float], seed: int, samples: int,
                 estimator: Callable[[list[float]], float] = median) -> tuple[float, float, float]:
    if not values:
        return math.nan, math.nan, math.nan
    rng = random.Random(seed)
    estimates = []
    for _ in range(samples):
        draw = [values[rng.randrange(len(values))] for _ in values]
        estimates.append(estimator(draw))
    return estimator(values), percentile(estimates, 0.025), percentile(estimates, 0.975)


def holm_adjust(p_values: list[tuple[int, float]]) -> dict[int, float]:
    ordered = sorted(p_values, key=lambda item: item[1])
    adjusted: dict[int, float] = {}
    running = 0.0
    total = len(ordered)
    for rank, (index, p_value) in enumerate(ordered):
        running = max(running, min(1.0, (total - rank) * p_value))
        adjusted[index] = running
    return adjusted


def v2_decision_gates(rows: list[dict[str, Any]], seed: int,
                      bootstrap_samples: int) -> dict[str, Any]:
    """Predeclared paper-start gates for the 10-block V2 design."""
    primary = "uac_robust_repair_v2"
    uac_differences = paired_differences(rows, "uac_h_s", "nominal", "mag", primary)
    uac_estimate, uac_low, uac_high = bootstrap_ci(
        uac_differences, seed ^ 0x2A11, bootstrap_samples)
    try:
        from scipy.stats import binomtest
        nonzero_differences = [value for value in uac_differences if abs(value) > 1e-12]
        uac_p = (float(binomtest(
            sum(value < 0 for value in nonzero_differences), len(nonzero_differences),
            0.5, alternative="greater",
        ).pvalue) if nonzero_differences else 1.0)
    except (ImportError, ValueError):
        uac_p = math.nan

    nominal_by_block: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    pause_by_block: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        target = nominal_by_block if row["condition"] == "nominal" else pause_by_block
        target[int(row["block"])][str(row["method"])] = row
    t100_ratios: list[float] = []
    did_values: list[float] = []
    for block, values in nominal_by_block.items():
        if primary in values and "mag" in values:
            primary_t100 = finite_or_nan(values[primary].get("t100_s"))
            mag_t100 = finite_or_nan(values["mag"].get("t100_s"))
            if math.isfinite(primary_t100) and math.isfinite(mag_t100) and mag_t100 > 0:
                t100_ratios.append(primary_t100 / mag_t100)
        pause = pause_by_block.get(block, {})
        needed_nominal = primary in values and "measured_static" in values
        needed_pause = primary in pause and "measured_static" in pause
        if needed_nominal and needed_pause:
            numbers = [
                finite_or_nan(pause[primary].get("uac_h_s")),
                finite_or_nan(values[primary].get("uac_h_s")),
                finite_or_nan(pause["measured_static"].get("uac_h_s")),
                finite_or_nan(values["measured_static"].get("uac_h_s")),
            ]
            if all(math.isfinite(value) for value in numbers):
                did_values.append((numbers[0] - numbers[1]) - (numbers[2] - numbers[3]))
    ratio_estimate, ratio_low, ratio_high = bootstrap_ci(
        t100_ratios, seed ^ 0x100, bootstrap_samples)
    did_estimate, did_low, did_high = bootstrap_ci(
        did_values, seed ^ 0xD1D, bootstrap_samples)
    reassigned_before_recovery = sum(
        int(row.get("reassigned_coverage_before_recovery_count", 0))
        for row in rows if row["condition"] == "pause" and row["method"] == primary
    )
    complete_blocks = len({
        int(row["block"]) for row in rows if row["method"] == primary and row["condition"] == "nominal"
    })
    gates = {
        "complete_10_blocks": complete_blocks == 10,
        "uac_superiority_vs_mag": (
            len(uac_differences) == 10 and uac_estimate < 0 and uac_high < 0 and
            math.isfinite(uac_p) and uac_p < 0.05
        ),
        "t100_noninferior_5pct_vs_mag": len(t100_ratios) == 10 and ratio_high <= 1.05,
        "pause_difference_in_differences_favorable": len(did_values) == 10 and did_high < 0,
        "reassigned_coverage_before_recovery_observed": reassigned_before_recovery > 0,
    }
    return {
        "schema_version": 1,
        "primary_method": primary,
        "primary_baseline": "mag",
        "uac_paired_difference_s": {
            "n": len(uac_differences), "median": uac_estimate,
            "bootstrap_95ci": [uac_low, uac_high], "one_sided_exact_sign_p": uac_p,
        },
        "t100_ratio": {
            "n": len(t100_ratios), "median": ratio_estimate,
            "bootstrap_95ci": [ratio_low, ratio_high], "noninferiority_margin": 1.05,
        },
        "pause_uac_difference_in_differences_s": {
            "n": len(did_values), "median": did_estimate,
            "bootstrap_95ci": [did_low, did_high],
        },
        "reassigned_coverage_before_recovery_count": reassigned_before_recovery,
        "gates": gates,
        "paper_start_allowed": all(gates.values()),
    }


def inferential_tables(rows: list[dict[str, Any]], seed: int, bootstrap_samples: int,
                       *, primary_method: str = "full_eacml",
                       methods_by_condition: dict[str, tuple[str, ...]] | None = None
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    paired: list[dict[str, Any]] = []
    for condition in sorted({str(row["condition"]) for row in rows}):
        baselines = tuple(method for method in registered_methods(condition, methods_by_condition)
                          if method != primary_method)
        for metric in METRICS:
            for baseline in baselines:
                values = paired_differences(rows, metric, condition, baseline, primary_method)
                salt = int.from_bytes(hashlib.sha256(
                    f"{condition}|{metric}|{baseline}".encode("utf-8")).digest()[:4], "big")
                estimate, lower, upper = bootstrap_ci(values, seed ^ salt, bootstrap_samples)
                paired.append({
                    "condition": condition, "metric": metric,
                    "contrast": f"{primary_method}-minus-{baseline}", "n_pairs": len(values),
                    "inference_scope": ("completed_pairs_exploratory" if metric in {"t95_s", "t100_s", "mean_first_coverage_s"}
                                        else "available_complete_pairs"),
                    "estimand": "median_paired_difference",
                    "effect_estimate": estimate,
                    "bootstrap_95ci_low": lower, "bootstrap_95ci_high": upper,
                    "bootstrap_samples": bootstrap_samples,
                })
        for outcome in BINARY_OUTCOMES:
            for baseline in baselines:
                values = paired_differences(rows, outcome, condition, baseline, primary_method)
                salt = int.from_bytes(hashlib.sha256(
                    f"{condition}|{outcome}|{baseline}|risk-difference".encode("utf-8")).digest()[:4], "big")
                estimate, lower, upper = bootstrap_ci(values, seed ^ salt, bootstrap_samples, safe_mean)
                paired.append({
                    "condition": condition, "metric": outcome,
                    "contrast": f"{primary_method}-minus-{baseline}", "n_pairs": len(values),
                    "inference_scope": "fixed_horizon_attainment",
                    "estimand": "paired_risk_difference",
                    "effect_estimate": estimate,
                    "bootstrap_95ci_low": lower, "bootstrap_95ci_high": upper,
                    "bootstrap_samples": bootstrap_samples,
                })
    tests: list[dict[str, Any]] = []
    try:
        from scipy.stats import binomtest, chi2, friedmanchisquare, wilcoxon
    except ImportError:
        return paired, tests, ("scipy unavailable: Cochran Q, exact McNemar-Holm, Friedman, and "
                               "Wilcoxon-Holm were not computed; bootstrap effect sizes remain available")
    conditions = sorted({str(row["condition"]) for row in rows})
    for condition in conditions:
        expected_methods = registered_methods(condition, methods_by_condition)
        baselines = tuple(method for method in expected_methods if method != primary_method)
        for metric in METRICS:
            blocks: dict[str, dict[str, float]] = defaultdict(dict)
            for row in rows:
                if row["condition"] != condition:
                    continue
                value = finite_or_nan(row.get(metric))
                if math.isfinite(value):
                    blocks[str(row["pair_id"])][str(row["method"])] = value
            complete = [values for values in blocks.values()
                        if all(method in values for method in expected_methods)]
            if len(expected_methods) >= 3 and len(complete) >= 2:
                samples = [[block[method] for block in complete] for method in expected_methods]
                if all(max(block[method] for method in expected_methods) -
                       min(block[method] for method in expected_methods) <= 1e-12
                       for block in complete):
                    statistic, p_value = 0.0, 1.0
                else:
                    result = friedmanchisquare(*samples)
                    statistic, p_value = float(result.statistic), float(result.pvalue)
                    if not (math.isfinite(statistic) and math.isfinite(p_value)):
                        statistic, p_value = 0.0, 1.0
                tests.append({"condition": condition, "metric": metric, "test": "Friedman",
                              "contrast": "all_methods", "n_pairs": len(complete),
                              "inference_scope": ("completed_pairs_exploratory" if metric in {"t95_s", "t100_s", "mean_first_coverage_s"}
                                                  else "available_complete_pairs"),
                              "statistic": statistic, "p_raw": p_value, "p_holm": p_value,
                              "discordant_full_success": "", "discordant_baseline_success": ""})
            wilcoxon_rows: list[dict[str, Any]] = []
            raw_for_holm: list[tuple[int, float]] = []
            for baseline in baselines:
                diffs = paired_differences(rows, metric, condition, baseline, primary_method)
                if len(diffs) < 2:
                    continue
                if all(abs(value) <= 1e-12 for value in diffs):
                    statistic, p_value = 0.0, 1.0
                else:
                    try:
                        result = wilcoxon(diffs, alternative="two-sided", zero_method="wilcox")
                        statistic, p_value = float(result.statistic), float(result.pvalue)
                        if not (math.isfinite(statistic) and math.isfinite(p_value)):
                            statistic, p_value = 0.0, 1.0
                    except ValueError:
                        statistic, p_value = 0.0, 1.0
                raw_for_holm.append((len(wilcoxon_rows), p_value))
                wilcoxon_rows.append({"condition": condition, "metric": metric, "test": "Wilcoxon signed-rank",
                                      "contrast": f"{primary_method}-vs-{baseline}", "n_pairs": len(diffs),
                                      "inference_scope": ("completed_pairs_exploratory" if metric in {"t95_s", "t100_s", "mean_first_coverage_s"}
                                                          else "available_complete_pairs"),
                                      "statistic": statistic, "p_raw": p_value, "p_holm": math.nan,
                                      "discordant_full_success": "", "discordant_baseline_success": ""})
            adjusted = holm_adjust(raw_for_holm)
            for index, row in enumerate(wilcoxon_rows):
                row["p_holm"] = adjusted[index]
                tests.append(row)
    # Binary threshold attainment is the primary right-censoring-safe test.
    # Only blocks containing all five methods enter Cochran's Q.
    for condition in conditions:
        expected_methods = registered_methods(condition, methods_by_condition)
        baselines = tuple(method for method in expected_methods if method != primary_method)
        for outcome in BINARY_OUTCOMES:
            blocks: dict[str, dict[str, int]] = defaultdict(dict)
            for row in rows:
                if row["condition"] == condition:
                    blocks[str(row["pair_id"])][str(row["method"])] = int(bool(row[outcome]))
            complete = [values for values in blocks.values()
                        if all(method in values for method in expected_methods)]
            if len(expected_methods) >= 3 and len(complete) >= 2:
                k = len(expected_methods)
                columns = [sum(block[method] for block in complete) for method in expected_methods]
                row_totals = [sum(block[method] for method in expected_methods) for block in complete]
                total = sum(columns)
                denominator = k * total - sum(value * value for value in row_totals)
                q_value = ((k - 1) * (k * sum(value * value for value in columns) - total * total) / denominator
                           if denominator > 0 else 0.0)
                q_p = float(chi2.sf(q_value, k - 1)) if denominator > 0 else 1.0
                tests.append({"condition": condition, "metric": outcome, "test": "Cochran Q",
                              "contrast": "all_methods", "n_pairs": len(complete),
                              "inference_scope": "fixed_horizon_attainment",
                              "statistic": q_value, "p_raw": q_p, "p_holm": q_p,
                              "discordant_full_success": "", "discordant_baseline_success": ""})
            mcnemar_rows: list[dict[str, Any]] = []
            raw_for_holm: list[tuple[int, float]] = []
            for baseline in baselines:
                paired_binary = [(values[primary_method], values[baseline]) for values in blocks.values()
                                 if primary_method in values and baseline in values]
                if len(paired_binary) < 2:
                    continue
                full_only = sum(full == 1 and base == 0 for full, base in paired_binary)
                baseline_only = sum(full == 0 and base == 1 for full, base in paired_binary)
                discordant = full_only + baseline_only
                p_value = (float(binomtest(full_only, discordant, 0.5, alternative="two-sided").pvalue)
                           if discordant else 1.0)
                raw_for_holm.append((len(mcnemar_rows), p_value))
                mcnemar_rows.append({"condition": condition, "metric": outcome, "test": "McNemar exact",
                                     "contrast": f"{primary_method}-vs-{baseline}", "n_pairs": len(paired_binary),
                                     "inference_scope": "fixed_horizon_attainment",
                                     "statistic": discordant, "p_raw": p_value, "p_holm": math.nan,
                                     "discordant_full_success": full_only,
                                     "discordant_baseline_success": baseline_only})
            adjusted = holm_adjust(raw_for_holm)
            for index, row in enumerate(mcnemar_rows):
                row["p_holm"] = adjusted[index]
                tests.append(row)
    secondary_rows = [
        (index, float(row["p_raw"])) for index, row in enumerate(tests)
        if row.get("test") == "Wilcoxon signed-rank"
        and row.get("metric") in SECONDARY_HOLM_METRICS
        and math.isfinite(float(row.get("p_raw", math.nan)))
    ]
    secondary_adjusted = holm_adjust(secondary_rows)
    for index, adjusted in secondary_adjusted.items():
        tests[index]["p_holm"] = adjusted
        tests[index]["inference_scope"] += "; Holm family=all preregistered secondary metrics/contrasts"
    return paired, tests, ("scipy available; fixed-horizon attainment uses Cochran Q/McNemar-Holm; "
                           "continuous complete-pair tests computed where n>=2")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--batch-state", type=Path,
                        help="Required for ral-hil-2 confirmatory analysis; selects the audited active attempt per plan row")
    parser.add_argument("--bootstrap-samples", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261019)
    args = parser.parse_args()
    if args.bootstrap_samples < 100:
        parser.error("--bootstrap-samples must be at least 100")
    plan, plan_meta = load_plan(args.plan)
    if (str(plan_meta.get("protocol_version", "")) == "ral-hil-2" and
            str(plan_meta.get("stage", "confirmatory")) == "confirmatory" and
            args.batch_state is None):
        parser.error("ral-hil-2 confirmatory analysis requires --batch-state")
    active_run_dirs: set[Path] | None = None
    batch_status_by_dir: dict[Path, str] = {}
    if args.batch_state is not None:
        batch_state = json.loads(args.batch_state.resolve().read_text(encoding="utf-8"))
        if str(batch_state.get("plan_id", "")) != str(plan_meta.get("plan_id", "")):
            parser.error("batch state belongs to a different plan")
        active_run_dirs = set()
        for record in batch_state.get("runs", {}).values():
            raw_dir = str(record.get("run_dir", "")).strip()
            if not raw_dir:
                continue
            path = Path(raw_dir).resolve()
            batch_status_by_dir[path] = str(record.get("status", ""))
            if record.get("status") == "accepted":
                active_run_dirs.add(path)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_plan_ids: set[str] = set()
    for run_dir in discover_run_dirs(args.runs.resolve()):
        plan_run_id = ""
        if active_run_dirs is not None and run_dir.resolve() not in active_run_dirs:
            rejected.append({
                "source_dir": str(run_dir), "plan_run_id": "",
                "disposition": "excluded_attempt_retained_for_audit",
                "reason": ("run attempt is not the active accepted attempt in batch_state "
                           f"(status={batch_status_by_dir.get(run_dir.resolve(), 'excluded_or_unregistered')})"),
            })
            continue
        try:
            context_path = run_dir / "run_context.json"
            if not context_path.is_file():
                raise RejectRun("missing run_context.json")
            context = json.loads(context_path.read_text(encoding="utf-8"))
            plan_run_id = str(context.get("plan_row", {}).get("plan_run_id", ""))
            if plan_run_id not in plan:
                raise RejectRun(f"unregistered plan_run_id {plan_run_id!r}")
            if plan_run_id in seen_plan_ids:
                raise RejectRun(f"duplicate completed session for plan_run_id {plan_run_id}")
            row = analyze_one(run_dir, plan[plan_run_id])
            accepted.append(row)
            seen_plan_ids.add(plan_run_id)
        except (OSError, json.JSONDecodeError, ValueError, RejectRun) as exc:
            rejected.append({"source_dir": str(run_dir), "plan_run_id": plan_run_id,
                             "disposition": "invalid_attempt", "reason": str(exc)})
    for plan_run_id in sorted(set(plan) - seen_plan_ids):
        rejected.append({"source_dir": "", "plan_run_id": plan_run_id,
                         "disposition": "missing_plan_row",
                         "reason": "planned run not present or not accepted"})

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "run_metrics_wide.csv", accepted, RUN_FIELDS)
    long_metrics = METRICS + BINARY_OUTCOMES
    long_rows = [
        {"run_id": row["run_id"], "plan_run_id": row["plan_run_id"], "block": row["block"],
         "pair_id": row["pair_id"], "method": row["method"], "condition": row["condition"],
         "metric": metric, "value": row[metric], "independent_unit": "run"}
        for row in accepted for metric in long_metrics
    ]
    write_csv(output / "long_table.csv", long_rows,
              ["run_id", "plan_run_id", "block", "pair_id", "method", "condition", "metric", "value", "independent_unit"])
    summary = main_table(accepted)
    summary_fields = ["method", "condition", "n_runs", "mission_success_rate",
                      "t95_reached_rate", "t95_censored_n", "t100_reached_rate", "t100_censored_n"]
    for metric in METRICS:
        summary_fields.extend([f"{metric}_n", f"{metric}_median", f"{metric}_q1", f"{metric}_q3"])
    write_csv(output / "main_table.csv", summary, summary_fields)
    primary_method = str(plan_meta.get("primary_method", "full_eacml"))
    methods_by_condition = methods_from_plan_metadata(plan_meta)
    paired, tests, stats_status = inferential_tables(
        accepted, args.seed, args.bootstrap_samples,
        primary_method=primary_method, methods_by_condition=methods_by_condition,
    )
    write_csv(output / "paired_bootstrap.csv", paired,
              ["condition", "metric", "contrast", "n_pairs", "inference_scope", "estimand", "effect_estimate",
               "bootstrap_95ci_low", "bootstrap_95ci_high", "bootstrap_samples"])
    write_csv(output / "hypothesis_tests.csv", tests,
              ["condition", "metric", "test", "contrast", "n_pairs", "inference_scope", "statistic",
               "p_raw", "p_holm", "discordant_full_success", "discordant_baseline_success"])
    threshold_rows = []
    for row in accepted:
        for threshold, time_key, event_key in (("T95", "t95_s", "t95_reached"),
                                               ("T100", "t100_s", "t100_reached")):
            reached = bool(row[event_key])
            threshold_rows.append({"run_id": row["run_id"], "plan_run_id": row["plan_run_id"],
                                   "pair_id": row["pair_id"], "method": row["method"],
                                   "condition": row["condition"], "threshold": threshold,
                                   "time_s": row[time_key] if reached else row["evaluation_horizon_s"],
                                   "event_observed": reached, "right_censored": not reached,
                                   "horizon_s": row["evaluation_horizon_s"]})
    write_csv(output / "time_to_threshold.csv", threshold_rows,
              ["run_id", "plan_run_id", "pair_id", "method", "condition", "threshold",
               "time_s", "event_observed", "right_censored", "horizon_s"])
    write_csv(output / "rejection_report.csv", rejected,
              ["source_dir", "plan_run_id", "disposition", "reason"])
    excluded_attempts = sum(row["disposition"] == "excluded_attempt_retained_for_audit"
                            for row in rejected)
    invalid_or_missing = len(rejected) - excluded_attempts
    output_names = [
        "run_metrics_wide.csv", "long_table.csv", "main_table.csv",
        "paired_bootstrap.csv", "hypothesis_tests.csv", "time_to_threshold.csv",
        "rejection_report.csv",
    ]
    decision_gates: dict[str, Any] | None = None
    if primary_method == "uac_robust_repair_v2":
        decision_gates = v2_decision_gates(accepted, args.seed, args.bootstrap_samples)
        write_json(output / "v2_decision_gates.json", decision_gates)
        output_names.append("v2_decision_gates.json")
    result = {
        "schema_version": 1,
        "protocol_version": str(plan_meta.get("protocol_version", PROTOCOL_VERSION)),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "plan_metadata": plan_meta,
        "planned_runs": len(plan),
        "accepted_runs": len(accepted),
        "rejected_or_missing": len(rejected),
        "excluded_attempts_retained": excluded_attempts,
        "invalid_or_missing_active_attempts": invalid_or_missing,
        "statistics_status": stats_status,
        "primary_method": primary_method,
        "methods_by_condition": {key: list(value) for key, value in methods_by_condition.items()},
        "paper_start_allowed": (
            decision_gates["paper_start_allowed"] if decision_gates is not None else None
        ),
        "primary_inference": ("paired effect sizes/bootstrap confidence intervals, including paired fixed-H attainment "
                              "risk differences, and fixed-H UAC; no significance is guaranteed"),
        "independent_unit": "one completed real-hardware run",
        "cycle_rows_used_as_samples": False,
        "right_censoring_policy": "T95/T100 not reached by fixed H are recorded at H with event_observed=false only in time_to_threshold; completion time fields remain missing",
        "binary_tests": "Cochran Q and exact paired McNemar-Holm use fixed-H T95/T100 attainment",
        "completion_time_tests": "Friedman/Wilcoxon on T95/T100 use completed pairs only and are explicitly exploratory",
        "uac_policy": "uac_h_s integrates uncovered fraction to the preregistered fixed H for every accepted run",
        "mean_first_policy": "equals UAC_H only when T100 occurs by H; missing for censored runs",
        "outputs": output_names,
    }
    write_json(output / "analysis_summary.json", result)
    artifact_names = tuple(result["outputs"]) + ("analysis_summary.json",)
    write_json(output / "analysis_manifest.sha256.json", {
        "schema_version": 1,
        "plan_sha256": sha256_file(args.plan.resolve()),
        "artifacts": {name: sha256_file(output / name) for name in artifact_names},
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if accepted and invalid_or_missing == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
