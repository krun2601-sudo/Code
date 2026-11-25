from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import pytest


STAGE = Path(__file__).resolve().parents[1]
TOOLS = STAGE / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(STAGE))

from analyze_runs import event_metrics, load_plan, v2_decision_gates  # noqa: E402
from audit_v2_readiness import analysis_manifest_ok  # noqa: E402
from fit_execution_model import angle_regime, conformal_padding, feature_vector  # noqa: E402
from generate_commissioning_plan import (  # noqa: E402
    device_seed, generate_targets, stratified_split_labels,
)
from generate_v2_confirmatory_plan import main as generate_v2_plan  # noqa: E402


def event(kind: str, t: float, **payload):
    return {
        "type": kind, "session_t_s": t, "mission_elapsed_s": math.nan,
        "payload": {"type": kind, "t": t, **payload},
    }


def test_event_metrics_separates_fault_duration_response_and_planner_time():
    rows = [
        event("fault_started", 101.0, device_id="cam_1", fault_id="F1"),
        event("replan_applied", 101.2, planner_ms=4.2, effective=True),
        event("reassigned_coverage_confirmed", 101.5, response_latency_s=0.5,
              before_original_device_recovery=True),
        event("fault_recovered", 109.0, device_id="cam_1", fault_id="F1"),
    ]
    result = event_metrics(rows, start_t=100.0)
    assert result["fault_durations"] == pytest.approx([8.0])
    assert result["recovery_responses"] == pytest.approx([0.5])
    assert result["planner_values"] == pytest.approx([4.2])
    assert result["effective_replan_count"] == 1
    assert result["replan_noop_count"] == 0
    assert result["reassigned_coverage_before_recovery_count"] == 1


def test_banded_conformal_helpers_use_wrapped_yaw_and_finite_sample_rank():
    assert feature_vector({
        "from_yaw_deg": 179.0, "to_yaw_deg": -179.0,
        "from_pitch_deg": 0.0, "to_pitch_deg": 1.0,
    }) == pytest.approx([1.0, 2.0, 0.0, 1.0, 0.0, 1.0])
    assert angle_regime({
        "from_yaw_deg": 179.0, "to_yaw_deg": -179.0,
        "from_pitch_deg": 0.0, "to_pitch_deg": 0.0,
    }, (3.0, 12.0)) == "small"
    assert angle_regime({
        "from_yaw_deg": 0.0, "to_yaw_deg": 8.0,
        "from_pitch_deg": 0.0, "to_pitch_deg": 0.0,
    }, (3.0, 12.0)) == "medium"
    padding, rank = conformal_padding([0.1] * 9 + [0.5], 0.1, "test")
    assert rank == 10
    assert padding == pytest.approx(0.5)


def test_development_analysis_manifest_rejects_tampering(tmp_path: Path):
    summary = tmp_path / "analysis_summary.json"
    metrics = tmp_path / "run_metrics_wide.csv"
    summary.write_text('{"accepted_runs": 6}\n', encoding="utf-8")
    metrics.write_text("run_id,uac_h_s\nr1,1.0\n", encoding="utf-8")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / "analysis_manifest.sha256.json").write_text(json.dumps({
        "schema_version": 1,
        "artifacts": {
            summary.name: digest(summary),
            metrics.name: digest(metrics),
        },
    }), encoding="utf-8")
    assert analysis_manifest_ok(tmp_path) is True
    metrics.write_text("run_id,uac_h_s\nr1,9.0\n", encoding="utf-8")
    assert analysis_manifest_ok(tmp_path) is False


def test_commissioning_design_covers_all_regimes_in_run_disjoint_splits():
    pooled_calibration = {"small": 0, "medium": 0, "large": 0}
    for device_id in ("cam_1", "cam_2", "cam_3", "cam_4"):
        targets = generate_targets(60, device_seed(17, device_id), (-35.0, 35.0), (-20.0, 20.0))
        previous = (0.0, 0.0)
        transitions = []
        regimes = []
        for target in targets:
            row = {
                "from_yaw": previous[0], "from_pitch": previous[1],
                "to_yaw": target[0], "to_pitch": target[1],
                "distance": math.hypot(target[0] - previous[0], target[1] - previous[1]),
            }
            transitions.append(row)
            regimes.append(angle_regime({
                "from_yaw_deg": previous[0], "from_pitch_deg": previous[1],
                "to_yaw_deg": target[0], "to_pitch_deg": target[1],
            }, (3.0, 12.0)))
            previous = target
        labels, _ = stratified_split_labels(transitions, device_seed(17, device_id) ^ 0xA5A5A5A5)
        assert {regime: regimes.count(regime) for regime in pooled_calibration} == {
            "small": 20, "medium": 20, "large": 20,
        }
        assert {label: labels.count(label) for label in set(labels)} == {
            "train": 36, "calibration": 12, "test": 12,
        }
        for label, regime in zip(labels, regimes):
            if label == "calibration":
                pooled_calibration[regime] += 1
    assert all(count >= 12 for count in pooled_calibration.values())


def test_v2_decision_gates_detects_clear_success_case():
    rows = []
    for block in range(1, 11):
        common = {
            "block": block, "pair_id": f"B{block:02d}",
            "t100_s": 20.0, "reassigned_coverage_before_recovery_count": 0,
        }
        rows.extend([
            {**common, "condition": "nominal", "method": "uac_robust_repair_v2",
             "uac_h_s": 8.0, "t100_s": 20.0},
            {**common, "condition": "nominal", "method": "mag",
             "uac_h_s": 10.0, "t100_s": 20.0},
            {**common, "condition": "nominal", "method": "measured_static",
             "uac_h_s": 9.5, "t100_s": 20.0},
            {**common, "condition": "pause", "method": "uac_robust_repair_v2",
             "uac_h_s": 8.5, "reassigned_coverage_before_recovery_count": 1},
            {**common, "condition": "pause", "method": "measured_static",
             "uac_h_s": 11.5},
        ])
    gates = v2_decision_gates(rows, seed=7, bootstrap_samples=500)
    assert gates["uac_paired_difference_s"]["n"] == 10
    assert gates["t100_ratio"]["bootstrap_95ci"][1] <= 1.05
    assert gates["paper_start_allowed"] is True
    assert all(math.isfinite(value) for value in gates["uac_paired_difference_s"]["bootstrap_95ci"])


def test_development_plan_generator_writes_six_hash_bound_runs(tmp_path: Path, monkeypatch):
    import yaml

    model_row = {
        "coefficients": {
            "intercept": 1.0, "yaw_positive_deg": 0.1, "yaw_negative_deg": 0.1,
            "pitch_up_deg": 0.1, "pitch_down_deg": 0.1, "dual_axis_parallel": 0.1,
        },
        "one_sided_padding_s_by_regime": {"small": 0.1, "medium": 0.2, "large": 0.3},
    }
    model = {
        "schema_version": 2,
        "split_rule": "preregistered transition_id and disjoint completed source runs",
        "pose_error_bounds_deg": {"yaw": 0.4, "pitch": 0.5},
        "models": {f"cam_{index}": model_row for index in range(4)},
    }
    model_path = tmp_path / "model.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
    config = {
        "devices": [{"id": f"cam_{index}", "enabled": True} for index in range(4)],
        "system": {"duration_s": 63.0},
        "policy": {
            "execution_model": {
                "status": "calibrated", "model_path": str(model_path), "sha256": model_hash,
            },
            "robust_fov": {"pose_error_yaw_deg": 0.4, "pose_error_pitch_deg": 0.5},
        },
        "action_space": {},
    }
    config_path = tmp_path / "base.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    output_prefix = tmp_path / "development_plan"
    overlay_dir = tmp_path / "overlays"
    monkeypatch.setattr(sys, "argv", [
        "generate_v2_confirmatory_plan.py", "--stage", "development",
        "--config", str(config_path), "--output-prefix", str(output_prefix),
        "--overlay-dir", str(overlay_dir), "--overlay-extends", str(config_path),
    ])
    assert generate_v2_plan() == 0
    plan, metadata = load_plan(output_prefix.with_suffix(".json"))
    assert len(plan) == 6
    assert metadata["stage"] == "development"
    assert set(metadata["methods_by_condition"]) == {"nominal", "pause", "repeatability"}
    assert len(list(overlay_dir.glob("*.yaml"))) == 6
