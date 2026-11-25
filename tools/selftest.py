#!/usr/bin/env python3
"""Synthetic contract test; all fabricated rows stay in a temporary directory.

This test checks parser/integrity behavior only.  Its numbers must never be
copied into a manuscript or mixed with hardware runs.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from analyze_runs import RejectRun, analyze_one
from common import canonical_json, deep_merge, load_yaml_with_inheritance, sha256_bytes
from generate_plan import make_rows, validate_balance


def csv_file(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--work-dir", type=Path,
                        help="Optional existing writable parent for the temporary directory")
    args = parser.parse_args()
    base = load_yaml_with_inheritance(args.config)
    _, full_design = make_rows(base, blocks=8, seed=20261017, cooldown_s=0.0,
                               overlay_dir=None, overlay_extends=None)
    validate_balance(full_design, 8)
    pairs: dict[str, list[dict]] = defaultdict(list)
    for row in full_design:
        pairs[str(row["pair_id"])].append(row)
    for pair_id, rows in pairs.items():
        for key in ("instance_seed", "injection_seed", "condition_parameters_json",
                    "expected_hardware_config_sha256", "expected_pair_frozen_sha256"):
            assert len({str(row[key]) for row in rows}) == 1, (pair_id, key)
        assert len({str(row["solver_seed"]) for row in rows}) == 5
        assert len({str(row["expected_config_sha256"]) for row in rows}) == 5
    for condition in {str(row["condition"]) for row in full_design}:
        for method in {str(row["method"]) for row in full_design}:
            counts = Counter(int(row["within_pair_order"]) for row in full_design
                             if row["condition"] == condition and row["method"] == method)
            frequencies = [counts[position] for position in range(1, 6)]
            assert max(frequencies) - min(frequencies) <= 1, (condition, method, frequencies)
    _, plan = make_rows(base, blocks=1, seed=12345, cooldown_s=0.0,
                        overlay_dir=None, overlay_extends=None)
    planned = plan[0]
    perturbation = json.loads(planned["condition_parameters_json"])
    experiment = {
        "protocol_version": planned["protocol_version"],
        "plan_id": planned["plan_id"],
        "plan_run_id": planned["plan_run_id"],
        "run_index": planned["run_index"],
        "block": planned["block"],
        "pair_id": planned["pair_id"],
        "method": planned["method"],
        "condition": planned["condition"],
        "instance_seed": planned["instance_seed"],
        "injection_seed": planned["injection_seed"],
        "solver_seed": planned["solver_seed"],
    }
    config = deep_merge(base, {"policy": {"method": planned["method"],
                                           "experiment": experiment,
                                           "fault_injection": perturbation}})
    temporary_parent = str(args.work_dir.resolve()) if args.work_dir else None
    with tempfile.TemporaryDirectory(prefix="ral_hil_contract_", dir=temporary_parent) as temporary:
        run = Path(temporary) / "hardware_synthetic_contract"
        run.mkdir()
        metadata = {"schema_version": 1, "platform_version": "4.0.0",
                    "platform_patch_version": "ral-hil-context-1.0.0",
                    "mode": "hardware", "run_id": run.name, "config": config,
                    "completed": True, "error": ""}
        (run / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        context = {
            "schema_version": 1,
            "motion_or_network_performed": False,
            "operator_arm_authorization": False,
            "plan_file_sha256": "synthetic",
            "preflight_file_sha256": "synthetic",
            "preflight_report_sha256": "synthetic",
            "preflight_summary": {"ready_to_move": True, "static_pass": True,
                                  "live_pass": True, "phase": "experiment",
                                  "config_sha256": planned["expected_config_sha256"],
                                   "hardware_config_sha256": planned["expected_hardware_config_sha256"],
                                   "coverage_task_observed": {
                                       "evaluation_cell_count": 384,
                                       "reachable_count": 384,
                                       "reachable_ratio": 1.0,
                                       "task_set_hash": "synthetic-contract-only",
                                   }},
            "plan_row": planned,
        }
        context["context_sha256"] = sha256_bytes(canonical_json(context))
        (run / "run_context.json").write_text(json.dumps(context), encoding="utf-8")
        cycle_fields = ["schema_version", "run_id", "utc", "t_s", "step", "dt_s", "work_ms",
                        "deadline_miss", "detections", "rays", "localizations", "tracks",
                        "measured_tracks", "metrics_json", "diagnostics_json"]
        cycles = []
        for step, (elapsed, confirmed) in enumerate(((0, 0), (5, 192), (10, 384))):
            diagnostics = {"policy": {"mission_state": "running", "mission_elapsed_s": elapsed,
                                       "evaluation_cell_count": 384,
                                       "confirmed_count": confirmed, "reachable_count": 384,
                                       "reachable_ratio": 1.0,
                                       "coverage_fraction": confirmed / 384, "planner_ms": 3 + step}}
            cycles.append({"schema_version": 1, "run_id": run.name, "utc": "", "t_s": elapsed,
                           "step": step, "dt_s": 0.1, "work_ms": 4, "deadline_miss": False,
                           "detections": 0, "rays": 0, "localizations": 0, "tracks": 0,
                           "measured_tracks": 0, "metrics_json": "{}",
                           "diagnostics_json": json.dumps(diagnostics)})
        csv_file(run / "cycles.csv", cycle_fields, cycles)
        event_fields = ["schema_version", "run_id", "utc", "t_s", "kind", "payload_json"]
        events = [{"schema_version": 1, "run_id": run.name, "utc": "", "t_s": 0,
                   "kind": "policy_event", "payload_json": json.dumps({"type": "mission_started", "mission_elapsed_s": 0})}]
        for index, device in enumerate([item["id"] for item in config["devices"] if item.get("enabled", True)]):
            events.append({"schema_version": 1, "run_id": run.name, "utc": "", "t_s": 5 + index * 0.1,
                           "kind": "policy_event", "payload_json": json.dumps({
                               "type": "transition_completed", "mission_elapsed_s": 5 + index * 0.1,
                               "device_id": device, "predicted_s": 1.0, "actual_s": 1.1,
                               "prediction_kind": "conformal_upper",
                               "point_prediction_s": 0.9, "conformal_padding_s": 0.1,
                               "command_issued_t": 3.9 + index * 0.1,
                               "frame_confirm_t": 5.0 + index * 0.1,
                               "time_definition": "command_issued_to_frame_confirm"})})
        events.append({"schema_version": 1, "run_id": run.name, "utc": "", "t_s": 10,
                       "kind": "policy_event", "payload_json": json.dumps({"type": "mission_completed", "mission_elapsed_s": 10})})
        csv_file(run / "events.csv", event_fields, events)
        telemetry_fields = ["schema_version", "run_id", "utc", "t_s", "device_id", "sample_t", "yaw_deg",
                            "pitch_deg", "roll_deg", "zoom", "yaw_rate_dps", "pitch_rate_dps", "connected",
                            "sequence", "source", "age_s", "raw_json"]
        telemetry = []
        for device in [item["id"] for item in config["devices"] if item.get("enabled", True)]:
            for sequence, elapsed in enumerate((0, 10)):
                telemetry.append({"schema_version": 1, "run_id": run.name, "utc": "", "t_s": elapsed,
                                  "device_id": device, "sample_t": elapsed, "yaw_deg": 2 * sequence,
                                  "pitch_deg": sequence, "roll_deg": 0, "zoom": 10,
                                  "yaw_rate_dps": 0, "pitch_rate_dps": 0, "connected": True,
                                  "sequence": sequence, "source": "synthetic", "age_s": 0, "raw_json": "{}"})
        csv_file(run / "telemetry.csv", telemetry_fields, telemetry)
        command_fields = ["schema_version", "run_id", "utc", "t_s", "device_id", "command_t", "status",
                          "sent", "ack", "latency_ms", "applied_json", "error", "action_json"]
        commands = [{"schema_version": 1, "run_id": run.name, "utc": "", "t_s": 1, "device_id": device,
                     "command_t": 1, "status": "acknowledged", "sent": True, "ack": True,
                     "latency_ms": 5, "applied_json": "{}", "error": "", "action_json": "{}"}
                    for device in [item["id"] for item in config["devices"] if item.get("enabled", True)]]
        csv_file(run / "commands.csv", command_fields, commands)
        frame_fields = ["schema_version", "run_id", "utc", "t_s", "device_id", "frame_id", "capture_t",
                        "received_t", "width", "height", "age_s", "usable", "resolution_ok", "mean_luma",
                        "contrast", "sharpness", "skipped_frames", "reason"]
        frames = [{"schema_version": 1, "run_id": run.name, "utc": "", "t_s": 1, "device_id": device,
                   "frame_id": 1, "capture_t": 0.9, "received_t": 1, "width": 1920, "height": 1080,
                   "age_s": 0.1, "usable": True, "resolution_ok": True, "mean_luma": 100,
                   "contrast": 20, "sharpness": 5, "skipped_frames": 0, "reason": ""}
                  for device in [item["id"] for item in config["devices"] if item.get("enabled", True)]]
        csv_file(run / "frames.csv", frame_fields, frames)
        manifest = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in run.glob("*.csv")}
        (run / "manifest.sha256.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = analyze_one(run, planned)
        assert result["t95_s"] == 10 and result["t100_s"] == 10 and result["mission_success"] is True

        # Rebuild the same synthetic contract as a complete fixed-H follow-up
        # that does not attain either threshold.  It must be accepted as
        # right-censored, not discarded or assigned a fabricated time.
        censored_cycles = []
        for step, (elapsed, confirmed) in enumerate(((0, 0), (45, 180), (90, 300))):
            diagnostics = {"policy": {"mission_state": "running", "mission_elapsed_s": elapsed,
                                       "evaluation_cell_count": 384,
                                       "confirmed_count": confirmed, "reachable_count": 384,
                                       "reachable_ratio": 1.0,
                                       "coverage_fraction": confirmed / 384, "planner_ms": 3 + step}}
            censored_cycles.append({"schema_version": 1, "run_id": run.name, "utc": "", "t_s": elapsed,
                                    "step": step, "dt_s": 0.1, "work_ms": 4, "deadline_miss": False,
                                    "detections": 0, "rays": 0, "localizations": 0, "tracks": 0,
                                    "measured_tracks": 0, "metrics_json": "{}",
                                    "diagnostics_json": json.dumps(diagnostics)})
        csv_file(run / "cycles.csv", cycle_fields, censored_cycles)
        csv_file(run / "events.csv", event_fields,
                 [event for event in events if json.loads(event["payload_json"]).get("type") != "mission_completed"])
        manifest = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in run.glob("*.csv")}
        (run / "manifest.sha256.json").write_text(json.dumps(manifest), encoding="utf-8")
        censored = analyze_one(run, planned)
        assert (censored["t95_censored"] is True and censored["t100_censored"] is True and
                math.isnan(censored["t95_s"]) and math.isnan(censored["t100_s"]) and
                math.isfinite(censored["uac_h_s"]) and censored["mission_success"] is False)

        with (run / "cycles.csv").open("a", encoding="utf-8") as stream:
            stream.write("tampered\n")
        try:
            analyze_one(run, planned)
        except RejectRun as exc:
            assert "hash mismatch" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("tampered log was accepted")
    print("PASS: paired seeds/order, completed and right-censored contracts accepted; tampered run rejected; no hardware contacted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
