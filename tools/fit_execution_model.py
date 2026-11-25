#!/usr/bin/env python3
"""Fit device-specific transition-time models with split-conformal upper bounds.

For each device, a Huber-IRLS robust linear model with non-negative intercept
and direction-specific slopes is fitted on the preregistered training split
using NNLS and a declared ridge stabilizer.  A one-sided nonconformity score
``actual - prediction`` is then
calibrated on a disjoint calibration split using the finite-sample rank
``ceil((n_cal + 1) * (1 - alpha))``.  The test split is touched only once for the
reported final check and never affects coefficients or conformal padding.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import (
    PROTOCOL_VERSION, discover_run_dirs, parse_json, read_csv, safe_mean,
    sha256_file, truthy, verify_manifest, write_csv, write_json,
)


FEATURES = (
    "intercept", "yaw_positive_deg", "yaw_negative_deg", "pitch_up_deg",
    "pitch_down_deg", "dual_axis_parallel",
)


def feature_vector(row: dict[str, Any]) -> list[float]:
    delta_yaw = ((float(row["to_yaw_deg"]) - float(row["from_yaw_deg"]) + 180.0) % 360.0
                 - 180.0)
    delta_pitch = float(row["to_pitch_deg"]) - float(row["from_pitch_deg"])
    return [
        1.0,
        max(delta_yaw, 0.0),
        max(-delta_yaw, 0.0),
        max(delta_pitch, 0.0),
        max(-delta_pitch, 0.0),
        1.0 if abs(delta_yaw) > 1e-9 and abs(delta_pitch) > 1e-9 else 0.0,
    ]


def fit_robust_nonnegative(rows: list[dict[str, Any]], ridge: float, huber_delta: float,
                           max_iter: int, tolerance: float) -> tuple[list[float], dict[str, Any]]:
    """Huber IRLS where every weighted subproblem is an auditable NNLS fit."""
    try:
        import numpy as np
        from scipy.optimize import nnls
    except ImportError as exc:  # pragma: no cover - kit declares both packages
        raise RuntimeError("NumPy and SciPy (ZR10 kit requirements) are required for Huber/NNLS fitting") from exc
    matrix = np.asarray([feature_vector(row) for row in rows], dtype=float)
    target = np.asarray([float(row["actual_s"]) for row in rows], dtype=float)
    width = matrix.shape[1]

    def weighted_fit(weights):
        root = np.sqrt(weights)
        design = matrix * root[:, None]
        response = target * root
        if ridge > 0:
            penalty = np.zeros((width - 1, width), dtype=float)
            penalty[:, 1:] = np.eye(width - 1) * math.sqrt(ridge)
            design = np.vstack((design, penalty))
            response = np.concatenate((response, np.zeros(width - 1)))
        coefficients, _ = nnls(design, response)
        return coefficients

    weights = np.ones(len(rows), dtype=float)
    coefficients = weighted_fit(weights)
    converged = False
    scale = 0.0
    iterations = 0
    for iterations in range(1, max_iter + 1):
        residuals = target - matrix @ coefficients
        center = float(np.median(residuals))
        scale = max(1e-6, 1.4826 * float(np.median(np.abs(residuals - center))))
        cutoff = huber_delta * scale
        absolute = np.abs(residuals - center)
        weights = np.ones_like(absolute)
        mask = absolute > cutoff
        weights[mask] = cutoff / absolute[mask]
        updated = weighted_fit(weights)
        if float(np.max(np.abs(updated - coefficients))) <= tolerance * (1.0 + float(np.max(np.abs(coefficients)))):
            coefficients = updated
            converged = True
            break
        coefficients = updated
    if not converged:
        raise ValueError(f"Huber/NNLS did not converge within {max_iter} iterations")
    if np.any(coefficients < -1e-12):  # defensive check around the solver contract
        raise AssertionError("NNLS returned a negative coefficient")
    return coefficients.tolist(), {
        "iterations": iterations,
        "converged": converged,
        "robust_scale_s": scale,
        "huber_delta": huber_delta,
        "minimum_irls_weight": float(np.min(weights)),
    }


def predict(coefficients: list[float], row: dict[str, Any]) -> float:
    return max(0.0, sum(coefficient * feature for coefficient, feature in zip(coefficients, feature_vector(row))))


def yaw_error_deg(measured: float, planned: float) -> float:
    return abs((measured - planned + 180.0) % 360.0 - 180.0)


def load_plan(path: Path) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    if path.suffix.lower() == ".json":
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = document.get("rows", [])
        metadata = {key: value for key, value in document.items() if key != "rows"}
    else:
        rows = read_csv(path)
        metadata = {}
    mapping: dict[str, dict[str, str]] = {}
    for row in rows:
        transition_id = str(row.get("transition_id", ""))
        if not transition_id or transition_id in mapping:
            raise ValueError(f"empty or duplicate transition_id: {transition_id!r}")
        mapping[transition_id] = dict(row)
    return mapping, metadata


def normalized_transition(raw: dict[str, Any], source: str) -> dict[str, Any]:
    definition = str(raw.get("time_definition", raw.get("target_definition", "")))
    if definition != "command_issued_to_frame_confirm":
        raise ValueError("time_definition must be 'command_issued_to_frame_confirm'")
    command_t = raw.get("command_issued_t")
    frame_t = raw.get("frame_confirm_t")
    if command_t not in (None, "") and frame_t not in (None, ""):
        measured_from_timestamps = float(frame_t) - float(command_t)
        if measured_from_timestamps <= 0 or not math.isfinite(measured_from_timestamps):
            raise ValueError("frame_confirm_t must be finite and later than command_issued_t")
        if raw.get("actual_s") not in (None, "") and abs(float(raw["actual_s"]) - measured_from_timestamps) > 1e-3:
            raise ValueError("actual_s disagrees with frame_confirm_t-command_issued_t by more than 1 ms")
        raw = dict(raw)
        raw["actual_s"] = measured_from_timestamps
    required = ("transition_id", "device_id", "from_yaw_deg", "from_pitch_deg",
                "to_yaw_deg", "to_pitch_deg", "actual_s")
    missing = [key for key in required if raw.get(key) in (None, "")]
    if missing:
        raise ValueError(f"missing {missing}")
    result: dict[str, Any] = {key: raw[key] for key in required}
    for key in required[2:]:
        result[key] = float(result[key])
        if not math.isfinite(result[key]):
            raise ValueError(f"nonfinite {key}")
    if result["actual_s"] <= 0:
        raise ValueError("actual_s must be positive")
    result["source"] = source
    result["time_definition"] = definition
    result["success"] = truthy(raw.get("success", True))
    if not result["success"]:
        raise ValueError("transition was not successful")
    return result


def extract_payload(payload: dict[str, Any]) -> dict[str, Any]:
    start = payload.get("from_pose_deg", payload.get("from", {}))
    target = payload.get("to_pose_deg", payload.get("to", {}))
    result = dict(payload)
    if isinstance(start, (list, tuple)) and len(start) >= 2:
        result.setdefault("from_yaw_deg", start[0])
        result.setdefault("from_pitch_deg", start[1])
    elif isinstance(start, dict):
        result.setdefault("from_yaw_deg", start.get("yaw_deg", start.get("yaw")))
        result.setdefault("from_pitch_deg", start.get("pitch_deg", start.get("pitch")))
    if isinstance(target, (list, tuple)) and len(target) >= 2:
        result.setdefault("to_yaw_deg", target[0])
        result.setdefault("to_pitch_deg", target[1])
    elif isinstance(target, dict):
        result.setdefault("to_yaw_deg", target.get("yaw_deg", target.get("yaw")))
        result.setdefault("to_pitch_deg", target.get("pitch_deg", target.get("pitch")))
    for alias in ("transition_time_s", "elapsed_s", "duration_s", "move_settle_frame_s"):
        if result.get("actual_s") in (None, "") and result.get(alias) not in (None, ""):
            result["actual_s"] = result[alias]
    return result


def transitions_from_runs(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for run_dir in discover_run_dirs(root):
        try:
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            if metadata.get("completed") is not True:
                raise ValueError("session completed != true")
            problems = verify_manifest(run_dir)
            if problems:
                raise ValueError("; ".join(problems))
            payloads: list[dict[str, Any]] = []
            for row in read_csv(run_dir / "events.csv"):
                payload = parse_json(row.get("payload_json"), {})
                event_type = payload.get("type", payload.get("event", row.get("kind")))
                if event_type != "transition_completed":
                    continue
                payloads.append(payload)
            for cycle in read_csv(run_dir / "cycles.csv"):
                document = parse_json(cycle.get("diagnostics_json"), {})
                policy = document.get("policy", document) if isinstance(document, dict) else {}
                embedded = policy.get("events", []) if isinstance(policy, dict) else []
                if isinstance(embedded, dict):
                    embedded = [embedded]
                if not isinstance(embedded, list):
                    raise ValueError("diagnostics.policy.events must be a list")
                payloads.extend(payload for payload in embedded if isinstance(payload, dict)
                                and payload.get("type", payload.get("event")) == "transition_completed")
            unique_payloads: dict[str, dict[str, Any]] = {}
            for payload in payloads:
                transition_id = str(payload.get("transition_id", ""))
                signature = transition_id or json.dumps(payload, ensure_ascii=False, sort_keys=True)
                unique_payloads.setdefault(signature, payload)
            for payload in unique_payloads.values():
                try:
                    accepted.append(normalized_transition(extract_payload(payload), str(run_dir)))
                except ValueError as exc:
                    rejected.append({"source": str(run_dir), "transition_id": str(payload.get("transition_id", "")), "reason": str(exc)})
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            rejected.append({"source": str(run_dir), "transition_id": "", "reason": f"run rejected: {exc}"})
    return accepted, rejected


def transitions_from_csv(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for index, row in enumerate(read_csv(path), 2):
        try:
            accepted.append(normalized_transition(row, f"{path}:{index}"))
        except ValueError as exc:
            rejected.append({"source": f"{path}:{index}", "transition_id": row.get("transition_id", ""), "reason": str(exc)})
    return accepted, rejected


def angle_regime(row: dict[str, Any], thresholds: tuple[float, float]) -> str:
    magnitude = max(
        abs((float(row["to_yaw_deg"]) - float(row["from_yaw_deg"]) + 180.0) % 360.0 - 180.0),
        abs(float(row["to_pitch_deg"]) - float(row["from_pitch_deg"])),
    )
    return "small" if magnitude <= thresholds[0] else "medium" if magnitude <= thresholds[1] else "large"


def conformal_padding(residuals: list[float], alpha: float, label: str) -> tuple[float, int]:
    ordered = sorted(residuals)
    rank = math.ceil((len(ordered) + 1) * (1 - alpha))
    if rank > len(ordered):
        raise SystemExit(
            f"{label}: alpha={alpha} is too small for n_cal={len(ordered)}; "
            f"finite-sample rank k={rank} exceeds n_cal"
        )
    rank = max(rank, 1)
    return max(0.0, ordered[rank - 1]), rank


def metrics(rows: list[dict[str, Any]], coefficients: list[float],
            paddings: dict[str, float], thresholds: tuple[float, float]) -> dict[str, float | int]:
    errors = [float(row["actual_s"]) - predict(coefficients, row) for row in rows]
    row_paddings = [paddings[angle_regime(row, thresholds)] for row in rows]
    return {
        "n": len(rows),
        "mae_s": safe_mean(abs(error) for error in errors),
        "rmse_s": math.sqrt(safe_mean(error * error for error in errors)),
        "underprediction_rate": safe_mean(error > 0 for error in errors),
        "upper_empirical_coverage": safe_mean(
            error <= padding + 1e-12 for error, padding in zip(errors, row_paddings)
        ),
        "mean_upper_padding_s": safe_mean(row_paddings),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Measured transition CSV")
    source.add_argument("--runs", type=Path, help="Root containing completed commissioning run logs")
    parser.add_argument("--plan", required=True, type=Path,
                        help="Preregistered commissioning plan containing transition_id and split")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--alpha", type=float, default=0.10,
                        help="One-sided miscoverage target; default produces a 90%% upper bound")
    parser.add_argument("--ridge", type=float, default=1e-6)
    parser.add_argument("--huber-delta", type=float, default=1.345)
    parser.add_argument("--max-iter", type=int, default=100)
    parser.add_argument("--tol", type=float, default=1e-8)
    parser.add_argument("--angle-regime-thresholds-deg", type=float, nargs=2,
                        default=(3.0, 12.0), metavar=("SMALL_MAX", "MEDIUM_MAX"))
    parser.add_argument("--minimum-regime-calibration", type=int, default=12)
    args = parser.parse_args()
    if not 0 < args.alpha < 1:
        parser.error("--alpha must be in (0,1)")
    if args.ridge < 0:
        parser.error("--ridge cannot be negative")
    if args.huber_delta <= 0 or args.max_iter < 1 or args.tol <= 0:
        parser.error("Huber delta, max iterations, and tolerance must be positive")
    thresholds = tuple(float(value) for value in args.angle_regime_thresholds_deg)
    if not 0 < thresholds[0] < thresholds[1]:
        parser.error("angle-regime thresholds must be positive and increasing")
    if args.minimum_regime_calibration < 1:
        parser.error("--minimum-regime-calibration must be positive")
    plan, plan_meta = load_plan(args.plan)
    measured, rejected = transitions_from_csv(args.input) if args.input else transitions_from_runs(args.runs)
    if args.runs:
        planned_splits_by_source: dict[str, set[str]] = defaultdict(set)
        for row in measured:
            planned = plan.get(str(row.get("transition_id", "")))
            if planned is not None:
                planned_splits_by_source[str(row["source"])].add(str(planned.get("split", "")))
        contaminated_sources = {
            source: sorted(values) for source, values in planned_splits_by_source.items()
            if len(values) != 1
        }
        if contaminated_sources:
            raise SystemExit(
                "run-level split leakage detected before quality filtering; each completed source "
                f"run must contain exactly one split: {contaminated_sources}"
            )
    seen: set[str] = set()
    joined: list[dict[str, Any]] = []
    for row in measured:
        transition_id = str(row["transition_id"])
        if transition_id in seen:
            rejected.append({"source": str(row["source"]), "transition_id": transition_id, "reason": "duplicate measured transition_id"})
            continue
        seen.add(transition_id)
        planned = plan.get(transition_id)
        if planned is None:
            rejected.append({"source": str(row["source"]), "transition_id": transition_id, "reason": "not preregistered in plan"})
            continue
        if str(planned.get("device_id")) != str(row["device_id"]):
            rejected.append({"source": str(row["source"]), "transition_id": transition_id, "reason": "device_id differs from plan"})
            continue
        try:
            tolerance = float(planned["pose_plan_tolerance_deg"])
            deviations = {
                "from_yaw_deg": yaw_error_deg(float(row["from_yaw_deg"]), float(planned["planned_from_yaw_deg"])),
                "from_pitch_deg": abs(float(row["from_pitch_deg"]) - float(planned["planned_from_pitch_deg"])),
                "to_yaw_deg": yaw_error_deg(float(row["to_yaw_deg"]), float(planned["planned_to_yaw_deg"])),
                "to_pitch_deg": abs(float(row["to_pitch_deg"]) - float(planned["planned_to_pitch_deg"])),
            }
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append({"source": str(row["source"]), "transition_id": transition_id,
                             "reason": f"invalid planned-pose contract: {exc}"})
            continue
        if not math.isfinite(tolerance) or tolerance <= 0 or any(
                not math.isfinite(value) or value > tolerance + 1e-9 for value in deviations.values()):
            rejected.append({"source": str(row["source"]), "transition_id": transition_id,
                             "reason": f"measured from/to pose differs from preregistered transition beyond {tolerance} deg: {deviations}"})
            continue
        row["to_yaw_error_deg"] = deviations["to_yaw_deg"]
        row["to_pitch_error_deg"] = deviations["to_pitch_deg"]
        row["split"] = str(planned.get("split", ""))
        if row["split"] not in {"train", "calibration", "test"}:
            rejected.append({"source": str(row["source"]), "transition_id": transition_id, "reason": "invalid preregistered split"})
            continue
        joined.append(row)
    if args.runs:
        split_by_source: dict[str, set[str]] = defaultdict(set)
        for row in joined:
            split_by_source[str(row["source"])].add(str(row["split"]))
        contaminated = {source: sorted(values) for source, values in split_by_source.items()
                        if len(values) != 1}
        if contaminated:
            raise SystemExit(
                "run-level split leakage detected; each completed source run must contain exactly "
                f"one of train/calibration/test: {contaminated}"
            )
    by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in joined:
        by_device[str(row["device_id"])].append(row)
    if len(by_device) != 4:
        raise SystemExit(f"need four devices with valid transitions; found {sorted(by_device)}")

    models: dict[str, Any] = {}
    prediction_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    prepared: dict[str, tuple[dict[str, list[dict[str, Any]]], list[float], dict[str, Any]]] = {}
    for device_id, rows in sorted(by_device.items()):
        splits = {name: [row for row in rows if row["split"] == name]
                  for name in ("train", "calibration", "test")}
        if len(rows) < 48 or len(splits["train"]) < 24 or len(splits["calibration"]) < 12 or len(splits["test"]) < 12:
            raise SystemExit(f"{device_id}: need >=48 valid samples with train>=24, calibration>=12, test>=12; got "
                             + str({key: len(value) for key, value in splits.items()}))
        coefficients, fit_diagnostics = fit_robust_nonnegative(
            splits["train"], args.ridge, args.huber_delta, args.max_iter, args.tol)
        prepared[device_id] = (splits, coefficients, fit_diagnostics)

    pooled_residuals: dict[str, list[float]] = {name: [] for name in ("small", "medium", "large")}
    calibration_yaw_errors: list[float] = []
    calibration_pitch_errors: list[float] = []
    for splits, coefficients, _ in prepared.values():
        for row in splits["calibration"]:
            pooled_residuals[angle_regime(row, thresholds)].append(
                float(row["actual_s"]) - predict(coefficients, row)
            )
            calibration_yaw_errors.append(float(row["to_yaw_error_deg"]))
            calibration_pitch_errors.append(float(row["to_pitch_error_deg"]))
    for regime, values in pooled_residuals.items():
        if len(values) < args.minimum_regime_calibration:
            raise SystemExit(
                f"{regime}: need >= {args.minimum_regime_calibration} pooled calibration samples; got {len(values)}"
            )
    paddings: dict[str, float] = {}
    ranks: dict[str, int] = {}
    for regime, values in pooled_residuals.items():
        paddings[regime], ranks[regime] = conformal_padding(values, args.alpha, regime)

    for device_id, (splits, coefficients, fit_diagnostics) in sorted(prepared.items()):
        split_metrics = {
            name: metrics(items, coefficients, paddings, thresholds)
            for name, items in splits.items()
        }
        models[device_id] = {
            "feature_names": list(FEATURES),
            "coefficients": {name: value for name, value in zip(FEATURES, coefficients)},
            "ridge": args.ridge,
            "fit_diagnostics": fit_diagnostics,
            "alpha": args.alpha,
            "calibration_n": sum(len(values) for values in pooled_residuals.values()),
            "angle_regime_thresholds_deg": list(thresholds),
            "regime_calibration_n": {name: len(values) for name, values in pooled_residuals.items()},
            "finite_sample_rank_by_regime": ranks,
            "one_sided_padding_s_by_regime": paddings,
            "one_sided_padding_s": max(paddings.values()),
            "split_metrics": split_metrics,
        }
        for split, items in splits.items():
            for row in items:
                point = predict(coefficients, row)
                actual = float(row["actual_s"])
                regime = angle_regime(row, thresholds)
                padding = paddings[regime]
                prediction_rows.append({
                    "device_id": device_id,
                    "transition_id": row["transition_id"],
                    "split": split,
                    "angle_regime": regime,
                    "actual_s": actual,
                    "point_prediction_s": point,
                    "upper_prediction_s": point + padding,
                    "point_error_s": actual - point,
                    "upper_covers": actual <= point + padding + 1e-12,
                    "source": row["source"],
                })
        for split, values in split_metrics.items():
            summary_rows.append({"device_id": device_id, "split": split, **values,
                                 "alpha": args.alpha,
                                 "conformal_padding_small_s": paddings["small"],
                                 "conformal_padding_medium_s": paddings["medium"],
                                 "conformal_padding_large_s": paddings["large"]})

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    model_document = {
        "schema_version": 2,
        "protocol_version": PROTOCOL_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status": "fitted_pending_review",
        "method": "device-specific monotone Huber-NNLS point model plus pooled angle-regime split-conformal upper calibration",
        "target_definition": "first related command issued to execution-confirming FRAME_CONFIRM",
        "target_includes": ["motion", "settle_hold", "dwell", "distinct_fresh_frame_confirmation"],
        "routing_cost_rule": "use the calibrated upper prediction directly; do not add dwell again",
        "feature_definition": {
            "yaw_positive_deg": "max(to_yaw-from_yaw, 0)",
            "yaw_negative_deg": "max(from_yaw-to_yaw, 0)",
            "pitch_up_deg": "max(to_pitch-from_pitch, 0)",
            "pitch_down_deg": "max(from_pitch-to_pitch, 0)",
            "dual_axis_parallel": "1 when both yaw and pitch change, otherwise 0",
        },
        "coefficient_constraints": "intercept, direction-specific slopes, and dual-axis coordination term are non-negative",
        "robust_fit": {"loss": "Huber", "delta": args.huber_delta,
                       "solver": "scipy.optimize.nnls for each IRLS subproblem",
                       "max_iter": args.max_iter, "tolerance": args.tol,
                       "ridge_on_slopes_only": args.ridge},
        "split_rule": "preregistered transition_id and disjoint completed source runs; train fit, calibration conformal only, test final check only",
        "angle_regime_thresholds_deg": list(thresholds),
        "conformal_rule": "within each pooled small/medium/large angle regime: k=ceil((n_cal+1)*(1-alpha)); q=max(0,kth sorted(actual-point))",
        "pose_error_bounds_deg": {
            "yaw": max(calibration_yaw_errors),
            "pitch": max(calibration_pitch_errors),
            "source": "maximum absolute arrival error over disjoint calibration runs",
            "sample_count": len(calibration_yaw_errors),
        },
        "plan_sha256": sha256_file(args.plan.resolve()),
        "plan_metadata": plan_meta,
        "models": models,
    }
    write_json(output / "execution_model.json", model_document)
    write_csv(output / "predictions.csv", prediction_rows, [
        "device_id", "transition_id", "split", "angle_regime", "actual_s", "point_prediction_s",
        "upper_prediction_s", "point_error_s", "upper_covers", "source",
    ])
    write_csv(output / "model_summary.csv", summary_rows, [
        "device_id", "split", "n", "mae_s", "rmse_s", "underprediction_rate",
        "upper_empirical_coverage", "mean_upper_padding_s", "alpha",
        "conformal_padding_small_s", "conformal_padding_medium_s", "conformal_padding_large_s",
    ])
    write_csv(output / "rejected_transitions.csv", rejected, ["source", "transition_id", "reason"])
    artifact_names = ("execution_model.json", "predictions.csv", "model_summary.csv", "rejected_transitions.csv")
    write_json(output / "model_manifest.sha256.json", {
        "schema_version": 1,
        "plan_sha256": sha256_file(args.plan.resolve()),
        "measured_input_sha256": sha256_file(args.input.resolve()) if args.input else None,
        "artifacts": {name: sha256_file(output / name) for name in artifact_names},
    })
    print(json.dumps({"output_dir": str(output), "devices": sorted(models),
                      "accepted": len(joined), "rejected": len(rejected),
                      "status": model_document["status"],
                      "manifest": str(output / "model_manifest.sha256.json")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
