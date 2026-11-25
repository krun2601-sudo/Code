#!/usr/bin/env python3
"""Audit all freeze-before-formal-experiment gates for ZR10 V2.

Inputs are read-only artifacts from the RTSP stability test, run-disjoint
commissioning/model fit, and the six-run development campaign.  The output is
the machine-readable decision used before generating the 50-run formal plan.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import canonical_json, read_csv, sha256_bytes, sha256_file, write_json


PRIMARY = "uac_robust_repair_v2"


def finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def regime_model_ok(row: dict[str, Any]) -> bool:
    padding = row.get("one_sided_padding_s_by_regime", {})
    counts = row.get("regime_calibration_n", {})
    coefficients = row.get("coefficients", {})
    names = {"small", "medium", "large"}
    required_coefficients = {
        "intercept", "yaw_positive_deg", "yaw_negative_deg",
        "pitch_up_deg", "pitch_down_deg", "dual_axis_parallel",
    }
    try:
        return (
            isinstance(padding, dict) and set(padding) == names
            and isinstance(counts, dict) and set(counts) == names
            and isinstance(coefficients, dict)
            and required_coefficients <= set(coefficients)
            and all(math.isfinite(finite(padding[name])) and finite(padding[name]) >= 0
                    for name in names)
            and all(math.isfinite(finite(coefficients[name])) and finite(coefficients[name]) >= 0
                    for name in required_coefficients)
            and all(int(counts[name]) >= 12 for name in names)
        )
    except (KeyError, TypeError, ValueError):
        return False


def analysis_manifest_ok(analysis_dir: Path) -> bool:
    """Verify every development-analysis artifact before using its results."""
    try:
        manifest = json.loads(
            (analysis_dir / "analysis_manifest.sha256.json").read_text(encoding="utf-8")
        )
        artifacts = manifest.get("artifacts", {})
        required = {"analysis_summary.json", "run_metrics_wide.csv"}
        if not isinstance(artifacts, dict) or not required <= set(artifacts):
            return False
        for relative, expected in artifacts.items():
            target = (analysis_dir / str(relative)).resolve()
            if not target.is_relative_to(analysis_dir) or not target.is_file():
                return False
            if sha256_file(target) != str(expected):
                return False
        return True
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rtsp-report", required=True, type=Path)
    parser.add_argument("--execution-model", required=True, type=Path)
    parser.add_argument("--development-analysis-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    rtsp = json.loads(args.rtsp_report.read_text(encoding="utf-8"))
    rtsp_clean = dict(rtsp)
    stored_rtsp_hash = rtsp_clean.pop("report_sha256", None)
    rtsp_integrity = stored_rtsp_hash == sha256_bytes(canonical_json(rtsp_clean))
    rtsp_gate = bool(
        rtsp_integrity and rtsp.get("pass") is True and len(rtsp.get("devices", [])) == 4
        and all(float(row.get("decoded_span_s", 0)) >= 298.0
                and float(row.get("maximum_decoded_frame_gap_s", math.inf)) <= 2.0
                for row in rtsp.get("devices", []))
    )

    model = json.loads(args.execution_model.read_text(encoding="utf-8"))
    models = model.get("models", {})
    split_rule = str(model.get("split_rule", ""))
    pose_bounds = model.get("pose_error_bounds_deg", {})
    model_gate = bool(
        int(model.get("schema_version", 0)) >= 2 and len(models) == 4
        and "disjoint completed source runs" in split_rule
        and finite(pose_bounds.get("yaw")) >= 0 and finite(pose_bounds.get("pitch")) >= 0
        and all(regime_model_ok(row) for row in models.values())
    )

    analysis_dir = args.development_analysis_dir.resolve()
    summary = json.loads((analysis_dir / "analysis_summary.json").read_text(encoding="utf-8"))
    rows = read_csv(analysis_dir / "run_metrics_wide.csv")
    analysis_integrity = analysis_manifest_ok(analysis_dir)
    plan_metadata = summary.get("plan_metadata", {})
    model_sha256 = sha256_file(args.execution_model.resolve())
    development_model_matches = (
        str(plan_metadata.get("stage", "")) == "development"
        and str(plan_metadata.get("expected_execution_model_sha256", "")) == model_sha256
    )
    development_freeze_metadata_present = bool(
        plan_metadata.get("frozen_software_sha256")
        and plan_metadata.get("base_config_sha256")
    )
    by_key = {(str(row["condition"]), str(row["method"])): row for row in rows}
    required = {
        ("nominal", PRIMARY), ("nominal", "mag"), ("nominal", "measured_static"),
        ("pause", PRIMARY), ("pause", "measured_static"),
        ("repeatability", PRIMARY),
    }
    complete_development = (
        int(summary.get("accepted_runs", 0)) == 6
        and int(summary.get("invalid_or_missing_active_attempts", 1)) == 0
        and required <= set(by_key)
    )
    nominal_v2 = by_key.get(("nominal", PRIMARY), {})
    nominal_mag = by_key.get(("nominal", "mag"), {})
    nominal_uac_better = (
        finite(nominal_v2.get("uac_h_s")) < finite(nominal_mag.get("uac_h_s"))
    )
    nominal_t100_noninferior = (
        finite(nominal_v2.get("t100_s")) <= 1.05 * finite(nominal_mag.get("t100_s"))
    )
    pause_v2 = by_key.get(("pause", PRIMARY), {})
    repair_observed = (
        finite(pause_v2.get("effective_replan_count")) >= 1
        and finite(pause_v2.get("reassigned_coverage_before_recovery_count")) >= 1
    )
    gates = {
        "rtsp_four_stream_5min_max_gap_2s": rtsp_gate,
        "run_disjoint_banded_execution_model": model_gate,
        "development_analysis_manifest_integrity": analysis_integrity,
        "development_plan_used_same_execution_model": development_model_matches,
        "development_freeze_metadata_present": development_freeze_metadata_present,
        "six_development_runs_accepted": complete_development,
        "development_v2_uac_better_than_mag": nominal_uac_better,
        "development_v2_t100_within_5pct_of_mag": nominal_t100_noninferior,
        "development_effective_repair_observed": repair_observed,
    }
    report = {
        "schema_version": 1,
        "gate": "v2_freeze_before_confirmatory_campaign",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "rtsp_report": str(args.rtsp_report.resolve()),
            "rtsp_report_sha256": sha256_file(args.rtsp_report.resolve()),
            "execution_model": str(args.execution_model.resolve()),
            "execution_model_sha256": sha256_file(args.execution_model.resolve()),
            "development_analysis_dir": str(analysis_dir),
            "development_analysis_summary_sha256": sha256_file(analysis_dir / "analysis_summary.json"),
        },
        "pose_error_bounds_deg": pose_bounds,
        "development_frozen_software_sha256": summary.get("plan_metadata", {}).get(
            "frozen_software_sha256", {}
        ),
        "development_base_config_sha256": summary.get("plan_metadata", {}).get(
            "base_config_sha256"
        ),
        "development_observed": {
            "nominal_v2_uac_h_s": finite(nominal_v2.get("uac_h_s")),
            "nominal_mag_uac_h_s": finite(nominal_mag.get("uac_h_s")),
            "nominal_v2_t100_s": finite(nominal_v2.get("t100_s")),
            "nominal_mag_t100_s": finite(nominal_mag.get("t100_s")),
            "pause_v2_effective_replan_count": finite(pause_v2.get("effective_replan_count")),
            "pause_v2_reassigned_coverage_before_recovery_count": finite(
                pause_v2.get("reassigned_coverage_before_recovery_count")
            ),
        },
        "gates": gates,
        "ready_to_freeze_and_generate_formal_plan": all(gates.values()),
    }
    report["report_sha256"] = sha256_bytes(canonical_json(report))
    write_json(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready_to_freeze_and_generate_formal_plan"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
