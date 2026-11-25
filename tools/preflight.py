#!/usr/bin/env python3
"""Read-only preflight for the four-ZR10 coverage experiment.

The tool only reads YAML/JSON/CSV files.  It contains no socket, SDK, or motor
control path.  A config-only invocation is intentionally *not* an authorization
to move: live checks remain NOT_EVALUATED and ``ready_to_move`` is false.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import (
    PROTOCOL_VERSION, canonical_json, config_sha256, hardware_config_sha256,
    load_yaml_with_inheritance, parse_json, read_csv, sha256_bytes, sha256_file,
    truthy, write_json,
)


def finding(check: str, status: str, detail: str, device_id: str = "") -> dict[str, str]:
    return {"check": check, "status": status, "device_id": device_id, "detail": detail}


def latest_by_device(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        key = row.get("device_id", "")
        if not key:
            continue
        try:
            time_value = float(row.get("t_s", "nan"))
        except ValueError:
            continue
        old = result.get(key)
        if old is None or time_value >= float(old.get("t_s", "-inf")):
            result[key] = row
    return result


def evaluation_grid_info(config: dict[str, Any]) -> dict[str, Any]:
    """Return an auditable summary of the frozen evaluation-cell grid."""
    grid = config.get("policy", {}).get("evaluation_grid", {})
    shape = grid.get("shape") if isinstance(grid, dict) else None
    shape_valid = (
        isinstance(shape, list) and len(shape) == 3 and
        all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in shape)
    )
    computed = math.prod(shape) if shape_valid else None
    declared = grid.get("cell_count") if isinstance(grid, dict) else None
    declared_valid = (
        isinstance(declared, int) and not isinstance(declared, bool) and
        computed is not None and declared == computed
    )
    return {
        "shape": shape,
        "computed_cell_count": computed,
        "declared_cell_count": declared,
        "shape_valid": shape_valid,
        "declared_count_matches": declared_valid,
        "role": grid.get("role") if isinstance(grid, dict) else None,
        "construction": grid.get("construction") if isinstance(grid, dict) else None,
        "frozen_within_run": grid.get("frozen_within_run") if isinstance(grid, dict) else None,
    }


def coverage_task_from_cycles(run_dir: Path) -> dict[str, Any]:
    """Extract the last explicit task-set record from read-only cycle logs."""
    path = run_dir / "cycles.csv"
    if not path.is_file():
        return {}
    for row in reversed(read_csv(path)):
        document = parse_json(row.get("diagnostics_json"), {})
        policy = document.get("policy", document) if isinstance(document, dict) else {}
        if not isinstance(policy, dict):
            continue
        evaluation_count = next((policy.get(key) for key in
                                 ("evaluation_cell_count", "total_cell_count", "M")
                                 if policy.get(key) is not None), None)
        reachable_count = next((policy.get(key) for key in
                                ("reachable_count", "reachable_cell_count")
                                if policy.get(key) is not None), None)
        if evaluation_count is None or reachable_count is None:
            continue
        try:
            evaluation_count = int(evaluation_count)
            reachable_count = int(reachable_count)
            ratio = float(policy.get("reachable_ratio", reachable_count / evaluation_count))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        return {
            "evaluation_cell_count": evaluation_count,
            "reachable_count": reachable_count,
            "reachable_ratio": ratio,
            "task_set_hash": policy.get("task_set_hash", ""),
            "source": "cycles.csv:diagnostics_json.policy",
        }
    return {}


def snapshot_from_run(run_dir: Path) -> dict[str, Any]:
    telemetry_path = run_dir / "telemetry_stream.csv"
    if not telemetry_path.is_file() or telemetry_path.stat().st_size < 10:
        telemetry_path = run_dir / "telemetry.csv"
    telemetry = latest_by_device(read_csv(telemetry_path))
    frames = latest_by_device(read_csv(run_dir / "frames.csv")) if (run_dir / "frames.csv").is_file() else {}
    command_rows = read_csv(run_dir / "commands.csv") if (run_dir / "commands.csv").is_file() else []
    # ``held`` rows are scheduler de-duplication records: no packet was sent, so
    # they must not replace the last transport result when auditing ACK health.
    # This is especially important after mission completion, when repeated
    # idle-stop actions are normally held while the most recent transmitted
    # zero-speed command has already been acknowledged.
    commands = latest_by_device([row for row in command_rows if truthy(row.get("sent", False))])
    # Hardware shutdown stops are issued below the policy/scheduler layer and
    # therefore appear in events.csv rather than commands.csv.  Treat the
    # latest acknowledged stop as a transport result so a completed run proves
    # its final safe-stop ACK instead of appearing to have a stale command.
    if (run_dir / "events.csv").is_file():
        for row in read_csv(run_dir / "events.csv"):
            if row.get("kind") != "stop":
                continue
            payload = parse_json(row.get("payload_json"), {})
            key = str(payload.get("device_id", ""))
            if not key:
                continue
            stop_row = {
                "device_id": key,
                "t_s": row.get("t_s", 0.0),
                "status": f"{payload.get('reason', 'stop')}_stop",
                "sent": True,
                "ack": payload.get("ack", False),
                "error": payload.get("error", ""),
            }
            previous = commands.get(key)
            if previous is None or float(stop_row["t_s"]) >= float(previous.get("t_s", 0.0)):
                commands[key] = stop_row
    all_times = []
    for table in (telemetry, frames, commands):
        for row in table.values():
            try:
                all_times.append(float(row.get("t_s", 0)))
            except ValueError:
                pass
    end_t = max(all_times, default=0.0)
    devices: dict[str, Any] = {}
    for key in sorted(set(telemetry) | set(frames) | set(commands)):
        tr = telemetry.get(key, {})
        fr = frames.get(key, {})
        cr = commands.get(key, {})
        raw = parse_json(tr.get("raw_json"), {}) if tr else {}
        devices[key] = {
            "telemetry": {
                "connected": truthy(tr.get("connected", False)),
                "sample_t": tr.get("sample_t"),
                "age_s": (float(tr.get("age_s") or 0.0) + max(0.0, end_t - float(tr.get("t_s", end_t)))) if tr else None,
                "zoom": tr.get("zoom"),
                "raw": raw,
            } if tr else None,
            "frame": {
                "frame_id": fr.get("frame_id"),
                "capture_t": fr.get("capture_t"),
                "age_s": float(fr.get("age_s") or 0.0) + max(0.0, end_t - float(fr.get("t_s", end_t))),
                "width": fr.get("width"),
                "height": fr.get("height"),
                "usable": truthy(fr.get("usable", False)),
                "resolution_ok": truthy(fr.get("resolution_ok", False)),
            } if fr else None,
            "command_result": {
                "status": cr.get("status", ""),
                "sent": truthy(cr.get("sent", False)),
                "ack": truthy(cr.get("ack", False)),
                "error": cr.get("error", ""),
                "age_s": max(0.0, end_t - float(cr.get("t_s", end_t))),
            } if cr else None,
        }
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    run_config = metadata.get("config")
    actual_hardware_hash = hardware_config_sha256(run_config) if isinstance(run_config, dict) else ""
    return {
        "schema_version": 1,
        "source": "completed_run_logs",
        "source_run_id": metadata.get("run_id", run_dir.name),
        "captured_monotonic_s": end_t,
        "hardware_config_sha256": actual_hardware_hash,
        "platform_version": metadata.get("platform_version", ""),
        "platform_patch_version": metadata.get("platform_patch_version", ""),
        "coverage_task": coverage_task_from_cycles(run_dir),
        "devices": devices,
    }


def static_checks(config: dict[str, Any], phase: str,
                  config_source: Path | None = None) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    active = [device for device in config.get("devices", []) if device.get("enabled", True) is True]
    results.append(finding("four_enabled_devices", "PASS" if len(active) == 4 else "FAIL",
                           f"enabled={len(active)}; required=4"))
    ids = [str(device.get("id", "")) for device in active]
    results.append(finding("unique_nonempty_device_ids", "PASS" if len(set(ids)) == 4 and all(ids) else "FAIL",
                           f"ids={ids}"))
    fixed_zoom = config.get("policy", {}).get("fixed_zoom")
    for device in active:
        key = str(device.get("id", ""))
        endpoint = str(device.get("ip", "")).strip()
        endpoint_ok = bool(endpoint) and not endpoint.upper().startswith(("REPLACE", "REDACTED", "UNSET"))
        results.append(finding("device_endpoint_configured", "PASS" if endpoint_ok else "FAIL",
                               f"configured={endpoint_ok}; value intentionally not echoed", key))
        video_endpoint = str(device.get("rtsp_url", "")).strip()
        video_endpoint_ok = bool(video_endpoint) and not video_endpoint.upper().startswith(("REPLACE", "REDACTED", "UNSET"))
        results.append(finding("video_endpoint_configured", "PASS" if video_endpoint_ok else "FAIL",
                               f"configured={video_endpoint_ok}; value intentionally not echoed", key))
        control = device.get("control", {})
        results.append(finding("velocity_p", "PASS" if control.get("mode") == "velocity_p" else "FAIL",
                               f"mode={control.get('mode')!r}", key))
        results.append(finding("ack_enabled", "PASS" if control.get("use_ack") is True else "FAIL",
                               f"use_ack={control.get('use_ack')!r}", key))
        results.append(finding("calibration_flag", "PASS" if device.get("calibration_verified") is True else "FAIL",
                               "flag only; it is not independent metrology evidence", key))
        image_size = device.get("camera", {}).get("image_size", [])
        ok_size = isinstance(image_size, list) and len(image_size) == 2 and all(float(v) > 0 for v in image_size)
        results.append(finding("configured_image_size", "PASS" if ok_size else "FAIL",
                               f"image_size={image_size}", key))
        limits = device.get("zoom_limits", [1, 10])
        zoom_ok = fixed_zoom is not None and len(limits) == 2 and float(limits[0]) <= float(fixed_zoom) <= float(limits[1])
        results.append(finding("fixed_zoom_in_limits", "PASS" if zoom_ok else "FAIL",
                               f"fixed_zoom={fixed_zoom}; limits={limits}", key))
    intrinsic_tables = [device.get("camera", {}).get("intrinsics", []) for device in active]
    if len(intrinsic_tables) == 4 and len({canonical_json(table) for table in intrinsic_tables}) == 1:
        results.append(finding("per_device_intrinsics_distinctness", "WARN",
                               "all four intrinsic tables are byte-identical; verify each device independently"))
    all_distortion_zero = bool(active) and all(
        all(abs(float(value)) <= 1e-12 for row in device.get("camera", {}).get("intrinsics", [])
            for value in row.get("dist", []))
        for device in active
    )
    if all_distortion_zero:
        results.append(finding("distortion_coefficients", "WARN",
                               "all configured distortion coefficients are zero; retain only if calibration supports this"))
    all_latency_zero = bool(active) and all(
        abs(float(device.get("camera", {}).get("video_latency_s", 0.0))) <= 1e-12 for device in active
    )
    if all_latency_zero:
        results.append(finding("video_latency", "WARN",
                               "all configured video latencies are zero; capture_t is uncorrected host timing"))
    policy = config.get("policy", {})
    grid = evaluation_grid_info(config)
    grid_ok = grid["shape_valid"] and grid["declared_count_matches"]
    results.append(finding(
        "evaluation_grid_schema", "PASS" if grid_ok else "FAIL",
        f"shape={grid['shape']}; computed_M={grid['computed_cell_count']}; "
        f"declared_M={grid['declared_cell_count']}; role={grid['role']!r}; "
        f"frozen_within_run={grid['frozen_within_run']!r}",
    ))
    semantic_ok = grid["role"] == "evaluation_cells" and grid["frozen_within_run"] is True
    results.append(finding(
        "evaluation_grid_semantics", "PASS" if semantic_ok else "FAIL",
        "evaluation_grid is the frozen task-cell set; candidate viewpoints are policy-defined separately",
    ))
    dense_ok = grid["computed_cell_count"] == 384
    results.append(finding(
        "evaluation_cell_count", "PASS" if dense_ok else "FAIL",
        f"configured_M={grid['computed_cell_count']}; preregistered_M=384",
    ))
    alias = policy.get("scan_grid")
    alias_ok = alias is None or alias == grid["shape"]
    results.append(finding(
        "deprecated_scan_grid_alias", "PASS" if alias_ok else "FAIL",
        f"scan_grid={alias}; evaluation_grid.shape={grid['shape']}; alias_only=true",
    ))
    confirmation = policy.get("execution_confirmation", {})
    for key in ("pose_tolerance_deg", "max_rate_dps", "settle_hold_s", "min_dwell_s",
                "distinct_frames", "frame_timeout_s", "transition_timeout_s"):
        value = confirmation.get(key)
        ok = value is not None and not isinstance(value, bool) and float(value) > 0
        results.append(finding(f"execution_confirmation.{key}", "PASS" if ok else "FAIL", f"value={value!r}"))
    for key in ("require_ack", "require_frame"):
        results.append(finding(f"execution_confirmation.{key}",
                               "PASS" if confirmation.get(key) is True else "FAIL",
                               f"value={confirmation.get(key)!r}"))
    results.append(finding("pose_only_fallback_disabled",
                           "PASS" if confirmation.get("allow_pose_only_fallback") is False else "FAIL",
                           f"value={confirmation.get('allow_pose_only_fallback')!r}"))
    enabled_actions = config.get("action_space", {}).get("enabled", [])
    results.append(finding("fixed_zoom_action_mask", "PASS" if set(enabled_actions) == {"yaw_deg", "pitch_deg"} else "FAIL",
                           f"enabled={enabled_actions}"))
    results.append(finding("detector_disabled", "PASS" if config.get("detector", {}).get("type") == "none" else "FAIL",
                           f"type={config.get('detector', {}).get('type')!r}"))
    results.append(finding("video_context_required", "PASS" if policy.get("requires_video") is True else "FAIL",
                           f"requires_video={policy.get('requires_video')!r}"))
    evidence = policy.get("calibration_evidence", {})
    evidence_keys = ("station_pose_verified", "per_device_intrinsics_verified",
                     "fixed_zoom_verified", "video_latency_measured")
    evidence_flags = {key: evidence.get(key) for key in evidence_keys}
    evidence_ok = all(evidence.get(key) is True for key in evidence_keys)
    evidence_id = str(evidence.get("evidence_id", "")).strip()
    evidence_ok = evidence_ok and evidence_id not in {"", "UNSET"}
    # Model-coverage experiments need a dated local angular datum and verified
    # fixed zoom.  Independent station/intrinsic/latency metrology is required
    # only for absolute world-frame claims, which this protocol explicitly does
    # not make.  Keep those missing items visible as WARN instead of converting
    # an honest scope limitation into an unrelated experiment-start failure.
    local_evidence = str(evidence.get("local_angular_datum_evidence", "")).strip()
    model_coverage_evidence_ok = (
        evidence.get("fixed_zoom_verified") is True
        and evidence_id not in {"", "UNSET"}
        and bool(local_evidence)
    )
    evidence_status = "PASS" if evidence_ok else "WARN"
    results.append(finding("dated_calibration_evidence", evidence_status,
                           f"flags={evidence_flags}; evidence_id={evidence_id!r}"))
    if phase == "experiment":
        results.append(finding(
            "model_coverage_evidence_boundary",
            "PASS" if model_coverage_evidence_ok else "FAIL",
            "local angular datum and fixed-zoom evidence are required; "
            "absolute station/intrinsic/video-latency flags may remain false",
        ))
    if phase == "experiment":
        model = policy.get("execution_model", {})
        model_declared = model.get("status") == "calibrated" and bool(model.get("model_path"))
        results.append(finding("execution_model_calibrated", "PASS" if model_declared else "FAIL",
                               f"status={model.get('status')!r}; model_path_set={bool(model.get('model_path'))}"))
        artifact_ok = False
        artifact_detail = "model is not declared calibrated"
        if model_declared:
            try:
                model_path = Path(str(model["model_path"]))
                if not model_path.is_absolute():
                    if config_source is None:
                        raise ValueError("relative model_path cannot be resolved without config source")
                    model_path = config_source.resolve().parent / model_path
                document = json.loads(model_path.resolve().read_text(encoding="utf-8"))
                actual_model_hash = sha256_file(model_path.resolve())
                expected_model_hash = str(model.get("sha256", "")).strip().lower()
                if expected_model_hash and actual_model_hash.lower() != expected_model_hash:
                    raise ValueError("execution-model artifact hash differs from policy.execution_model.sha256")
                models = document.get("models")
                if not isinstance(models, dict):
                    raise ValueError("models mapping is missing")
                missing = sorted(set(ids) - set(models))
                if missing:
                    raise ValueError(f"missing device models: {missing}")
                required = {"intercept", "yaw_positive_deg", "yaw_negative_deg", "pitch_up_deg", "pitch_down_deg"}
                for device_id in ids:
                    row = models[device_id]
                    coefficients = row.get("coefficients") if isinstance(row, dict) else None
                    if not isinstance(coefficients, dict) or not required <= set(coefficients):
                        raise ValueError(f"{device_id}: incomplete coefficients")
                    values = [float(coefficients[key]) for key in required]
                    padding = float(row.get("one_sided_padding_s"))
                    if any(not math.isfinite(value) or value < 0 for value in (*values, padding)):
                        raise ValueError(f"{device_id}: non-finite or negative coefficient/padding")
                target = str(document.get("target_definition", ""))
                if "FRAME_CONFIRM" not in target and target != "command_issued_to_frame_confirm":
                    raise ValueError("target_definition does not end at FRAME_CONFIRM")
                routing_rule = str(document.get("routing_cost_rule", ""))
                if "do not add dwell" not in routing_rule.lower():
                    raise ValueError("routing_cost_rule does not forbid adding dwell twice")
                protocol = str(policy.get("experiment", {}).get("protocol_version", ""))
                if protocol == "ral-hil-2":
                    if int(document.get("schema_version", 0)) < 2:
                        raise ValueError("ral-hil-2 requires execution-model schema_version >= 2")
                    if "disjoint completed source runs" not in str(document.get("split_rule", "")):
                        raise ValueError("ral-hil-2 model lacks run-disjoint split evidence")
                    bounds = document.get("pose_error_bounds_deg", {})
                    robust = policy.get("robust_fov", {})
                    if (float(robust.get("pose_error_yaw_deg", -1)) < float(bounds.get("yaw", math.inf)) or
                            float(robust.get("pose_error_pitch_deg", -1)) < float(bounds.get("pitch", math.inf))):
                        raise ValueError("robust-FOV pose bounds are below model calibration evidence")
                    for device_id in ids:
                        coefficients = models[device_id].get("coefficients", {})
                        if "dual_axis_parallel" not in coefficients:
                            raise ValueError(f"{device_id}: missing dual-axis parallel-motion feature")
                        banded = models[device_id].get("one_sided_padding_s_by_regime", {})
                        if set(banded) != {"small", "medium", "large"}:
                            raise ValueError(f"{device_id}: missing small/medium/large conformal padding")
                artifact_ok = True
                artifact_detail = f"sha256={actual_model_hash}; device_models={len(ids)}"
            except OSError as exc:
                artifact_detail = f"model artifact unreadable ({type(exc).__name__}); path intentionally not echoed"
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                artifact_detail = str(exc)
        results.append(finding("execution_model_artifact", "PASS" if artifact_ok else "FAIL", artifact_detail))
    return results


def kit_reference_checks(kit_root: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Verify the immutable 4.0.0 reference snapshot, not a patched live tree."""
    results: list[dict[str, str]] = []
    evidence: dict[str, Any] = {"kit_root_present": kit_root.is_dir()}
    snapshot_path = kit_root / "PLATFORM_SNAPSHOT.json"
    if not snapshot_path.is_file():
        return [finding("kit_reference_snapshot", "FAIL", "PLATFORM_SNAPSHOT.json missing")], evidence
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [finding("kit_reference_snapshot", "FAIL", str(exc))], evidence
    version = snapshot.get("platform_version")
    results.append(finding("kit_reference_version", "PASS" if version == "4.0.0" else "FAIL",
                           f"platform_version={version!r}; required='4.0.0'"))
    mismatches = []
    source_files = snapshot.get("source_files", {})
    for relative, expected in source_files.items():
        path = kit_root / relative
        if not path.is_file():
            mismatches.append(f"missing:{relative}")
        elif sha256_file(path).lower() != str(expected).lower():
            mismatches.append(f"changed:{relative}")
    results.append(finding("kit_reference_source_hashes", "PASS" if not mismatches else "FAIL",
                           f"checked={len(source_files)}; mismatches={mismatches[:8]}"))
    evidence.update({"platform_snapshot_sha256": sha256_file(snapshot_path),
                     "platform_version": version, "source_file_count": len(source_files),
                     "source_hash_mismatches": mismatches})
    return results, evidence


def live_checks(config: dict[str, Any], snapshot: dict[str, Any] | None,
                hardware_hash: str, phase: str) -> list[dict[str, str]]:
    if snapshot is None:
        return [finding("live_snapshot", "NOT_EVALUATED",
                        "provide --snapshot or --run-dir; no device was contacted")]
    results: list[dict[str, str]] = []
    results.append(finding("snapshot_hardware_hash",
                           "PASS" if snapshot.get("hardware_config_sha256") == hardware_hash else "FAIL",
                           f"snapshot={snapshot.get('hardware_config_sha256')}; expected={hardware_hash}"))
    results.append(finding("platform_version", "PASS" if snapshot.get("platform_version") == "4.0.0" else "FAIL",
                           f"platform_version={snapshot.get('platform_version')!r}; required='4.0.0'"))
    results.append(finding("platform_patch", "PASS" if snapshot.get("platform_patch_version") else "FAIL",
                           f"platform_patch_version={snapshot.get('platform_patch_version')!r}"))
    grid = evaluation_grid_info(config)
    task = snapshot.get("coverage_task")
    task_recorded = isinstance(task, dict) and task.get("evaluation_cell_count") is not None and task.get("reachable_count") is not None and task.get("reachable_ratio") is not None
    task_required = phase == "experiment"
    results.append(finding(
        "coverage_task_recorded", "PASS" if (task_recorded or not task_required) else "FAIL",
        (f"coverage_task={task!r}; experiment phase requires explicit M, reachable_count, and reachable_ratio"
         if task_required else
         f"coverage_task={task!r}; not required during commissioning"),
    ))
    if task_recorded:
        try:
            live_m = int(task["evaluation_cell_count"])
            reachable = int(task["reachable_count"])
            ratio = float(task["reachable_ratio"])
        except (TypeError, ValueError):
            live_m, reachable, ratio = -1, -1, math.nan
        expected_m = grid["computed_cell_count"]
        m_ok = expected_m == 384 and live_m == expected_m
        reachable_ok = live_m == 384 and reachable == live_m
        ratio_ok = reachable_ok and math.isfinite(ratio) and 0.0 < ratio <= 1.0 and abs(ratio - reachable / live_m) <= 1e-6
        results.append(finding(
            "live_evaluation_cell_count", "PASS" if m_ok else "FAIL",
            f"live_M={live_m}; configured_M={expected_m}; preregistered_M=384",
        ))
        results.append(finding(
            "all_evaluation_cells_reachable", "PASS" if ratio_ok else "FAIL",
            f"reachable_count={reachable}; M={live_m}; reachable_ratio={ratio}; required=384/384=1.0",
        ))
    active = [device for device in config.get("devices", []) if device.get("enabled", True) is True]
    values = snapshot.get("devices", {})
    for device in active:
        key = str(device.get("id", ""))
        item = values.get(key)
        if not isinstance(item, dict):
            results.append(finding("device_snapshot_present", "FAIL", "missing", key))
            continue
        telemetry = item.get("telemetry") or {}
        stale_limit = float(config.get("system", {}).get("telemetry_stale_s", 0.5))
        telem_ok = telemetry.get("connected") is True and telemetry.get("age_s") is not None and 0 <= float(telemetry["age_s"]) <= stale_limit
        results.append(finding("fresh_connected_telemetry", "PASS" if telem_ok else "FAIL",
                               f"connected={telemetry.get('connected')}; age_s={telemetry.get('age_s')}; limit={stale_limit}", key))
        state = telemetry.get("state") or {}
        safe = config.get("policy", {}).get("safe_envelope_deg", {})
        yaw_limits = safe.get("yaw", [-35.0, 35.0])
        pitch_limits = safe.get("pitch", [-20.0, 20.0])
        try:
            yaw_value = float(state.get("yaw_deg"))
            pitch_value = float(state.get("pitch_deg"))
            pose_inside = (
                float(yaw_limits[0]) <= yaw_value <= float(yaw_limits[1])
                and float(pitch_limits[0]) <= pitch_value <= float(pitch_limits[1])
            )
        except (TypeError, ValueError, IndexError):
            yaw_value = pitch_value = math.nan
            pose_inside = False
        results.append(finding(
            "startup_pose_within_safe_envelope",
            "PASS" if pose_inside else "FAIL",
            f"yaw={yaw_value}; pitch={pitch_value}; yaw_limits={yaw_limits}; pitch_limits={pitch_limits}",
            key,
        ))
        raw = telemetry.get("raw") or {}
        zoom_state_ok = raw.get("zoom_known") is True and raw.get("zoom_stable") is True
        results.append(finding("zoom_known_stable", "PASS" if zoom_state_ok else "FAIL",
                               f"zoom_known={raw.get('zoom_known')}; zoom_stable={raw.get('zoom_stable')}", key))
        fixed_zoom = float(config.get("policy", {}).get("fixed_zoom", 10.0))
        try:
            zoom_error = abs(float(telemetry.get("zoom")) - fixed_zoom)
        except (TypeError, ValueError):
            zoom_error = float("inf")
        results.append(finding("fixed_zoom_reached", "PASS" if zoom_error <= 0.15 else "FAIL",
                               f"zoom={telemetry.get('zoom')}; target={fixed_zoom}; tolerance=0.15", key))
        frame = item.get("frame") or {}
        expected = tuple(int(v) for v in device.get("camera", {}).get("image_size", [1920, 1080]))
        actual = (int(frame.get("width", -1)), int(frame.get("height", -1)))
        max_age = float(config.get("system", {}).get("max_frame_age_s", 0.6))
        frame_ok = (frame.get("usable") is True and frame.get("resolution_ok") is True and
                    actual == expected and frame.get("age_s") is not None and
                    0 <= float(frame["age_s"]) <= max_age)
        results.append(finding("fresh_usable_frame", "PASS" if frame_ok else "FAIL",
                               f"actual={actual}; expected={expected}; age_s={frame.get('age_s')}; max_age_s={max_age}; usable={frame.get('usable')}", key))
        command = item.get("command_result") or {}
        ack_age_limit = max(2.0, 4.0 * float(device.get("control", {}).get("command_timeout_s", 0.25)))
        try:
            ack_age = float(command.get("age_s"))
        except (TypeError, ValueError):
            ack_age = float("inf")
        ack_ok = (command.get("sent") is True and command.get("ack") is True and
                  not command.get("error") and 0 <= ack_age <= ack_age_limit)
        results.append(finding("latest_command_ack", "PASS" if ack_ok else "FAIL",
                               f"status={command.get('status')!r}; sent={command.get('sent')}; ack={command.get('ack')}; "
                               f"error={command.get('error')!r}; age_s={command.get('age_s')}; max_age_s={ack_age_limit}", key))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Private overlay or public template YAML")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--snapshot", type=Path, help="Read-only JSON snapshot from the runtime/operator")
    source.add_argument("--run-dir", type=Path, help="Read the latest telemetry/frame/ACK rows from an existing run")
    parser.add_argument("--phase", choices=("commissioning", "experiment"), default="commissioning")
    parser.add_argument("--kit-root", type=Path,
                        default=Path(__file__).resolve().parents[2] / "ZR10_Algorithm_Kit",
                        help="Immutable Kit 4.0.0 reference used for source-hash verification")
    parser.add_argument("--expected-source-sha256", help="Optional SHA-256 pin for the top-level YAML file")
    parser.add_argument("--expected-extends-sha256",
                        help="Optional SHA-256 pin for the direct YAML named by top-level extends")
    parser.add_argument("--output", type=Path, help="Write the complete JSON report")
    parser.add_argument("--require-live", action="store_true",
                        help="Return nonzero unless all live snapshot checks pass")
    args = parser.parse_args()

    config = load_yaml_with_inheritance(args.config)
    full_hash = config_sha256(config)
    hardware_hash = hardware_config_sha256(config)
    execution_model_hash = None
    model_path_value = str(config.get("policy", {}).get("execution_model", {}).get("model_path", "")).strip()
    if model_path_value:
        try:
            model_path = Path(model_path_value)
            if not model_path.is_absolute():
                model_path = args.config.resolve().parent / model_path
            execution_model_hash = sha256_file(model_path.resolve())
        except OSError:
            execution_model_hash = None
    results = static_checks(config, args.phase, args.config.resolve())
    kit_results, kit_evidence = kit_reference_checks(args.kit_root.resolve())
    results.extend(kit_results)
    if args.expected_source_sha256:
        actual = sha256_file(args.config.resolve())
        results.append(finding("top_level_yaml_hash",
                               "PASS" if actual.lower() == args.expected_source_sha256.lower() else "FAIL",
                               f"actual={actual}; expected={args.expected_source_sha256.lower()}"))
    if args.expected_extends_sha256:
        try:
            import yaml
            top = yaml.safe_load(args.config.read_text(encoding="utf-8-sig"))
            extends = Path(str(top["extends"]))
            if not extends.is_absolute():
                extends = args.config.resolve().parent / extends
            actual = sha256_file(extends.resolve())
            results.append(finding("direct_extends_yaml_hash",
                                   "PASS" if actual.lower() == args.expected_extends_sha256.lower() else "FAIL",
                                   f"actual={actual}; expected={args.expected_extends_sha256.lower()}"))
        except (OSError, KeyError, TypeError, ImportError, ValueError) as exc:
            results.append(finding("direct_extends_yaml_hash", "FAIL", str(exc)))
    snapshot = None
    if args.snapshot:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    elif args.run_dir:
        snapshot = snapshot_from_run(args.run_dir.resolve())
    live = live_checks(config, snapshot, hardware_hash, args.phase)
    static_results = list(results)
    results.extend(live)
    static_pass = not any(item["status"] == "FAIL" for item in static_results)
    live_evaluated = all(item["status"] != "NOT_EVALUATED" for item in live)
    live_pass = live_evaluated and all(item["status"] == "PASS" for item in live)
    report: dict[str, Any] = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only",
        "network_or_motion_performed": False,
        "phase": args.phase,
        "config_sha256": full_hash,
        "hardware_config_sha256": hardware_hash,
        "execution_model_sha256": execution_model_hash,
        "top_level_yaml_sha256": sha256_file(args.config.resolve()),
        "kit_reference": kit_evidence,
        "coverage_task_configuration": evaluation_grid_info(config),
        "coverage_task_observed": (snapshot.get("coverage_task") if isinstance(snapshot, dict) else None),
        "policy_source_sha256": (snapshot.get("policy_source_sha256") if isinstance(snapshot, dict) else None),
        "policy_algorithm_version": (snapshot.get("policy_algorithm_version") if isinstance(snapshot, dict) else None),
        "static_pass": static_pass,
        "live_evaluated": live_evaluated,
        "live_pass": live_pass,
        "ready_to_move": bool(static_pass and live_pass),
        "findings": results,
    }
    report["report_sha256"] = sha256_bytes(canonical_json(report))
    if args.output:
        write_json(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not static_pass:
        return 1
    if args.require_live and not live_pass:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
