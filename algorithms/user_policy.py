"""Execution-confirmed cooperative field-of-view coverage for ZR10 4.0.0.

This module deliberately contains the complete deployable policy in one file.
It consumes only the public ``PolicyContext`` contract (plus the optional
``frames`` and ``command_results`` fields added by ``platform_patch``) and
returns absolute gimbal-angle ``Action`` objects.  It never imports an SDK,
opens a camera/network connection, sleeps, or reads simulator truth.

Five preregistered methods share exactly the same execution state machine and
completion gates:

``fpr``
    Fixed-partition raster.  It never reallocates work across partitions.
``mag``
    Motion-aware append-only greedy planning with the nominal time model.
``open_cml``
    Full-position insertion using the nominal time model, without repair.
``measured_cml``
    Full-position insertion using a measured end-to-end execution model,
    without repair.
``ea_cml``
    The measured model plus event-triggered residual-task repair.
``uac_robust_repair_v2``
    Robust first-coverage latency optimization.  A complete MAG route is kept
    as the incumbent, a measured-model seed and deterministic neighborhoods
    may improve it, and no accepted plan may increase the MAG robust T100.

The insertion score is a deterministic heuristic proxy, not a global-optimum
claim.  Its exact definition and every default are documented in README.md.
"""
from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zr10lab.config import DeviceConfig, LabConfig
from zr10lab.geometry import intrinsics_at_zoom, project_world_points, world_to_gimbal_angles
from zr10lab.models import Action, Decision, PolicyContext, Telemetry


ALGORITHM_VERSION = "2.4.3"

METHOD_ALIASES = {
    "fpr": "fpr",
    "raster": "fpr",
    "fixed_partition_raster": "fpr",
    "mag": "mag",
    "motion_aware_greedy": "mag",
    "open_cml": "open_cml",
    "open-cml": "open_cml",
    "open_loop_cml": "open_cml",
    "open-loop-cml": "open_cml",
    "measured_cml": "measured_cml",
    "measured-cml": "measured_cml",
    "measured_static": "measured_cml",
    "ea_cml": "ea_cml",
    "ea-cml": "ea_cml",
    "full_eacml": "ea_cml",
    "full-eacml": "ea_cml",
    "uac_robust_repair_v2": "uac_robust_repair_v2",
    "uac-robust-repair-v2": "uac_robust_repair_v2",
    "v2": "uac_robust_repair_v2",
    "commissioning": "commissioning",
}

V2_METHOD = "uac_robust_repair_v2"
REPAIR_METHODS = frozenset({"ea_cml", V2_METHOD})
MEASURED_METHODS = frozenset({"measured_cml", "ea_cml", V2_METHOD})
INSERTION_METHODS = frozenset({"open_cml", "measured_cml", "ea_cml"})
MAIN_METHODS = frozenset({"fpr", "mag", "open_cml", "measured_cml", "ea_cml", V2_METHOD})


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _positive(value: Any, name: str, *, allow_zero: bool = False) -> float:
    result = _finite(value, name)
    if result < 0 if allow_zero else result <= 0:
        relation = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {relation}")
    return result


def _integer(value: Any, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ValueError(f"{name} must be an integer")
    result = int(value)
    if result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


def _cell_centers(lo: float, hi: float, count: int) -> tuple[float, ...]:
    width = (hi - lo) / count
    return tuple(lo + (index + 0.5) * width for index in range(count))


def _quantize(value: float, step: float) -> float:
    # Round halves away from zero; unlike Python round this has no parity rule.
    magnitude = math.floor(abs(value) / step + 0.5) * step
    result = math.copysign(magnitude, value)
    return 0.0 if abs(result) < step * 1e-10 else float(result)


def _angle_error(a: float, b: float) -> float:
    # Mechanical range is below one revolution, but wrapping makes the helper
    # correct for any later configuration with a signed +/-180 degree boundary.
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _json_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return str(value)


@dataclass(frozen=True)
class Cell:
    index: int
    cell_id: str
    point_m: tuple[float, float, float]


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    device_id: str
    yaw_deg: float
    pitch_deg: float
    coverage: frozenset[int]
    nominal_coverage: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class CommissioningRow:
    commissioning_id: str
    device_id: str
    step: int
    transition_id: str
    split: str
    planned_from_yaw_deg: float
    planned_from_pitch_deg: float
    planned_to_yaw_deg: float
    planned_to_pitch_deg: float


@dataclass
class ActiveTask:
    candidate_id: str
    task_id: str
    attempt_id: str
    stage: str
    assigned_t: float
    from_yaw_deg: float
    from_pitch_deg: float
    predicted_s: float
    command_started_t: float | None = None
    settle_started_t: float | None = None
    dwell_started_t: float | None = None
    frame_confirm_started_t: float | None = None
    last_action_issued_t: float | None = None
    ack_seen: bool = False
    frame_ids: set[int] = field(default_factory=set)
    task_kind: str = "coverage"
    transition_id: str = ""
    physical_frame_gate_t: float | None = None
    confirmation_frame_id: int | None = None
    confirmation_frame_capture_t: float | None = None
    confirmation_frame_received_t: float | None = None
    prediction_bound_exceeded_emitted: bool = False


@dataclass
class FaultState:
    reason: str
    since_t: float
    details: str = ""
    task_id: str = ""
    attempt_id: str = ""
    candidate_id: str = ""


@dataclass
class PauseInjection:
    """Frozen policy-only logical pause, triggered by time or coverage state."""

    mode: str
    duration_s: float
    configured_device_id: str | None = None
    start_after_mission_s: float | None = None
    trigger_coverage_fraction: float | None = None
    selection_rule: str = "configured_device"
    selected_device_id: str | None = None
    selected_unique_residual_count: int = 0
    started_t: float | None = None


@dataclass(frozen=True)
class PlanObjective:
    """Robust route objective; lower lexicographic values are better."""

    uncovered_count: int
    uac_h_s: float
    t95_s: float
    t100_s: float
    total_motion_deg: float
    load_cv: float

    def search_key(self) -> tuple[float, ...]:
        return (float(self.uncovered_count), self.uac_h_s, self.t95_s,
                self.total_motion_deg, self.load_cv, self.t100_s)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "uncovered_count": self.uncovered_count,
            "uac_h_s": self.uac_h_s,
            "t95_s": self.t95_s,
            "t100_s": self.t100_s,
            "total_motion_deg": self.total_motion_deg,
            "load_cv": self.load_cv,
        }


class MyPolicy:
    """Deterministic, auditable execution-aware coverage policy."""

    def __init__(self, cfg: LabConfig):
        self.cfg = cfg
        self.devices = {device.id: device for device in cfg.active_devices}
        self.device_ids = tuple(sorted(self.devices))
        if not self.device_ids:
            raise ValueError("at least one active device is required")

        raw_method = cfg.policy.get("method")
        if raw_method is None:
            control_mode = str(cfg.policy.get("control_mode", "")).strip().lower()
            raw_method = control_mode if control_mode in METHOD_ALIASES else "ea_cml"
        requested = str(raw_method).strip().lower()
        try:
            self.method = METHOD_ALIASES[requested]
        except KeyError as exc:
            raise ValueError(f"unsupported policy.method {requested!r}; choose {sorted(METHOD_ALIASES)}") from exc
        self.commissioning_mode = self.method == "commissioning"
        self.planner_variant = {
            "fpr": "capability_constrained_balanced_partition_3d_serpentine",
            "mag": "deterministic_append_only_motion_aware_greedy",
            "open_cml": "deterministic_full_position_insertion_prune_2opt",
            "measured_cml": "deterministic_full_position_insertion_prune_2opt",
            "ea_cml": "deterministic_full_position_insertion_prune_2opt",
            V2_METHOD: "mag_incumbent_robust_uac_anytime_local_search",
            "commissioning": "preregistered_parallel_transition_sequence",
        }[self.method]
        self.is_v2 = self.method == V2_METHOD

        self.enabled = frozenset(str(name) for name in cfg.action_space.get("enabled", ["yaw_deg", "pitch_deg"]))
        unknown = self.enabled - {"yaw_deg", "pitch_deg", "zoom", "focal_length_mm", "focus_direction",
                                  "zoom_direction", "autofocus", "gimbal_mode", "photo", "record_toggle",
                                  "hdr_toggle", "osd", "encoding"}
        if unknown:
            raise ValueError(f"unknown action dimensions: {sorted(unknown)}")

        self.ttl_s = _positive(cfg.system.get("action_ttl_s", 0.5), "system.action_ttl_s")
        self.telemetry_stale_s = _positive(cfg.system.get("telemetry_stale_s", 0.5),
                                            "system.telemetry_stale_s")
        self.max_frame_age_s = _positive(cfg.system.get("max_frame_age_s", 0.6),
                                         "system.max_frame_age_s")

        gate = cfg.policy.get("execution_confirmation", {})
        if not isinstance(gate, Mapping):
            raise ValueError("policy.execution_confirmation must be a mapping")
        self.pose_tolerance_deg = _positive(gate.get("pose_tolerance_deg",
                                                     cfg.policy.get("scan_settle_tolerance_deg", 1.0)),
                                            "execution_confirmation.pose_tolerance_deg")
        self.max_rate_dps = _positive(gate.get("max_rate_dps", 1.0),
                                      "execution_confirmation.max_rate_dps", allow_zero=True)
        self.settle_hold_s = _positive(gate.get("settle_hold_s", 0.30),
                                       "execution_confirmation.settle_hold_s", allow_zero=True)
        self.min_dwell_s = _positive(gate.get("min_dwell_s", cfg.policy.get("scan_dwell_s", 0.8)),
                                     "execution_confirmation.min_dwell_s", allow_zero=True)
        self.stationary_confirmation_upper_s = _positive(
            gate.get("stationary_upper_s", self.settle_hold_s + self.min_dwell_s + 0.5),
            "execution_confirmation.stationary_upper_s",
        )
        if self.stationary_confirmation_upper_s < self.settle_hold_s + self.min_dwell_s:
            raise ValueError(
                "execution_confirmation.stationary_upper_s must include settle_hold_s and min_dwell_s"
            )
        self.distinct_frames = _integer(gate.get("distinct_frames", 3),
                                        "execution_confirmation.distinct_frames", minimum=1)
        self.frame_timeout_s = _positive(gate.get("frame_timeout_s", 2.0),
                                         "execution_confirmation.frame_timeout_s")
        self.frame_pose_max_gap_s = _positive(
            gate.get("frame_pose_max_gap_s", cfg.system.get("pose_max_gap_s", 0.2)),
            "execution_confirmation.frame_pose_max_gap_s",
        )
        self.transition_timeout_s = _positive(gate.get("transition_timeout_s", 12.0),
                                              "execution_confirmation.transition_timeout_s")
        self.require_ack = bool(gate.get("require_ack", False))
        self.require_frame = bool(gate.get("require_frame", False))
        self.use_measured_pose_coverage = bool(
            gate.get("measured_pose_projection", self.is_v2)
        )
        if bool(gate.get("allow_pose_only_fallback", False)) and self.require_frame:
            raise ValueError("pose-only fallback is incompatible with execution-confirmed paper mode")
        if (self.is_v2 or self.use_measured_pose_coverage) and (
                not self.require_ack or not self.require_frame):
            raise ValueError(
                "measured-pose coverage evidence requires both ACK and fresh-frame confirmation"
            )

        self.fixed_zoom = {
            device_id: _finite(cfg.policy.get("fixed_zoom", self.devices[device_id].initial_zoom),
                               f"fixed_zoom[{device_id}]")
            for device_id in self.device_ids
        }
        self.zoom_tolerance = _positive(cfg.policy.get("fixed_zoom_tolerance", 0.08),
                                       "policy.fixed_zoom_tolerance", allow_zero=True)
        self.require_zoom_verified = bool(cfg.policy.get("require_zoom_verified", self.require_frame))

        safe = cfg.policy.get("safe_envelope_deg", {})
        if not isinstance(safe, Mapping):
            raise ValueError("policy.safe_envelope_deg must be a mapping")
        global_yaw = safe.get("yaw", (-35.0, 35.0))
        global_pitch = safe.get("pitch", (-20.0, 20.0))
        if len(global_yaw) != 2 or len(global_pitch) != 2:
            raise ValueError("safe_envelope_deg yaw/pitch must each contain two limits")
        self.safe_limits: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {}
        for device_id, device in self.devices.items():
            yaw = (max(float(global_yaw[0]), float(device.yaw_limits_deg[0])),
                   min(float(global_yaw[1]), float(device.yaw_limits_deg[1])))
            pitch = (max(float(global_pitch[0]), float(device.pitch_limits_deg[0])),
                     min(float(global_pitch[1]), float(device.pitch_limits_deg[1])))
            if not all(math.isfinite(x) for x in (*yaw, *pitch)) or yaw[0] >= yaw[1] or pitch[0] >= pitch[1]:
                raise ValueError(f"{device_id}: empty/non-finite software safe envelope")
            self.safe_limits[device_id] = (yaw, pitch)

        self.lambda_b = _positive(cfg.policy.get("lambda_balance", 0.15),
                                  "policy.lambda_balance", allow_zero=True)
        self.lambda_u = _positive(cfg.policy.get("lambda_uncertainty", 0.25),
                                  "policy.lambda_uncertainty", allow_zero=True)
        self.score_epsilon = _positive(cfg.policy.get("score_epsilon", 1e-6),
                                       "policy.score_epsilon")
        self.health_weights = tuple(float(x) for x in cfg.policy.get("uncertainty_health_weights", [0.35, 0.35, 0.30]))
        if len(self.health_weights) != 3 or any(not math.isfinite(x) or x < 0 for x in self.health_weights):
            raise ValueError("uncertainty_health_weights must be three finite non-negative values")

        self.planner_budget_ms = _positive(cfg.policy.get("planner_budget_ms", 40.0),
                                           "policy.planner_budget_ms")
        self.initial_planner_budget_ms = _positive(
            cfg.policy.get("initial_planner_budget_ms", 200.0 if self.is_v2 else self.planner_budget_ms),
            "policy.initial_planner_budget_ms",
        )
        self.repair_planner_budget_ms = _positive(
            cfg.policy.get("repair_planner_budget_ms", 40.0),
            "policy.repair_planner_budget_ms",
        )
        self.planner_deadline_guard_ms = _positive(
            cfg.policy.get("planner_deadline_guard_ms", 5.0),
            "policy.planner_deadline_guard_ms",
            allow_zero=True,
        )
        if self.planner_deadline_guard_ms >= min(
                self.initial_planner_budget_ms, self.repair_planner_budget_ms):
            raise ValueError(
                "policy.planner_deadline_guard_ms must be smaller than both V2 planner budgets"
            )
        self.planner_max_evaluations = _integer(cfg.policy.get("planner_max_evaluations", 50000),
                                                "policy.planner_max_evaluations")
        self.initial_local_search_max_evaluations = _integer(
            cfg.policy.get("initial_local_search_max_evaluations", 96),
            "policy.initial_local_search_max_evaluations",
            minimum=1,
        )
        self.repair_local_search_max_evaluations = _integer(
            cfg.policy.get("repair_local_search_max_evaluations", 48),
            "policy.repair_local_search_max_evaluations",
            minimum=1,
        )
        self.local_search_passes = _integer(cfg.policy.get("local_search_passes", 2),
                                            "policy.local_search_passes", minimum=0)
        self.max_candidates_per_device = _integer(cfg.policy.get("max_candidates_per_device", 512),
                                                  "policy.max_candidates_per_device")
        quantization = cfg.policy.get("candidate_quantization_deg", [0.5, 0.5])
        if len(quantization) != 2:
            raise ValueError("candidate_quantization_deg must be [yaw_step, pitch_step]")
        self.quantization_deg = (
            _positive(quantization[0], "candidate_quantization_deg[0]"),
            _positive(quantization[1], "candidate_quantization_deg[1]"),
        )
        self.fov_margin = _finite(cfg.policy.get("fov_margin", 0.08), "policy.fov_margin")
        if not 0 <= self.fov_margin < 0.5:
            raise ValueError("policy.fov_margin must be in [0, 0.5)")
        robust_fov = cfg.policy.get("robust_fov", {})
        if not isinstance(robust_fov, Mapping):
            raise ValueError("policy.robust_fov must be a mapping")
        self.use_robust_candidate_coverage = bool(
            robust_fov.get("enforce_for_all_methods", self.is_v2)
        )
        self.robust_pose_yaw_deg = _positive(
            robust_fov.get("pose_error_yaw_deg", self.pose_tolerance_deg),
            "robust_fov.pose_error_yaw_deg", allow_zero=True,
        )
        self.robust_pose_pitch_deg = _positive(
            robust_fov.get("pose_error_pitch_deg", self.pose_tolerance_deg),
            "robust_fov.pose_error_pitch_deg", allow_zero=True,
        )
        self.robust_pixel_margin = _finite(
            robust_fov.get("planning_pixel_margin", 0.0 if self.is_v2 else self.fov_margin),
            "robust_fov.planning_pixel_margin",
        )
        self.confirmation_pixel_margin = _finite(
            robust_fov.get("confirmation_pixel_margin", 0.0 if self.is_v2 else self.fov_margin),
            "robust_fov.confirmation_pixel_margin",
        )
        if not 0 <= self.robust_pixel_margin < 0.5 or not 0 <= self.confirmation_pixel_margin < 0.5:
            raise ValueError("robust_fov pixel margins must be in [0, 0.5)")

        robust_objective = cfg.policy.get("robust_objective", {})
        if not isinstance(robust_objective, Mapping):
            raise ValueError("policy.robust_objective must be a mapping")
        self.complete_mag_reference = bool(
            robust_objective.get("complete_mag_reference", False)
        )
        self.opportunistic_tail_nonregression = bool(
            robust_objective.get("opportunistic_tail_nonregression", self.is_v2)
        )
        self.opportunistic_tail_factor = _positive(
            robust_objective.get("opportunistic_tail_factor", 1.0),
            "robust_objective.opportunistic_tail_factor",
        )
        initial_pose = robust_objective.get("initial_pose_box", {})
        if not isinstance(initial_pose, Mapping):
            raise ValueError("robust_objective.initial_pose_box must be a mapping")
        self.initial_pose_box_enabled = bool(initial_pose.get("enabled", False))
        self.initial_pose_yaw_bound_deg = _positive(
            initial_pose.get("yaw_bound_deg", 1.0),
            "robust_objective.initial_pose_box.yaw_bound_deg",
            allow_zero=True,
        )
        self.initial_pose_pitch_bound_deg = _positive(
            initial_pose.get("pitch_bound_deg", 1.0),
            "robust_objective.initial_pose_box.pitch_bound_deg",
            allow_zero=True,
        )
        self._planning_with_initial_pose_box = False
        experiment = cfg.policy.get("experiment", {})
        if not isinstance(experiment, Mapping):
            experiment = {}
        self.objective_horizon_s = _positive(
            robust_objective.get("horizon_s", experiment.get("horizon_s", cfg.system.get("duration_s", 60.0))),
            "robust_objective.horizon_s",
        )
        self.objective_epsilon_s = _positive(
            robust_objective.get("epsilon_s", 1e-6), "robust_objective.epsilon_s",
        )
        self.switch_min_gain_s = _positive(
            robust_objective.get("switch_min_gain_s", 0.05),
            "robust_objective.switch_min_gain_s", allow_zero=True,
        )
        self.fault_recovery_bound_s = _positive(
            robust_objective.get("fault_recovery_bound_s", 8.0),
            "robust_objective.fault_recovery_bound_s", allow_zero=True,
        )
        self.repair_witness_margin_s = _positive(
            robust_objective.get("repair_witness_margin_s", 0.5),
            "robust_objective.repair_witness_margin_s", allow_zero=True,
        )
        self.repair_candidate_pool_per_device = _integer(
            robust_objective.get("repair_candidate_pool_per_device", 12),
            "robust_objective.repair_candidate_pool_per_device",
        )
        self.repair_max_insertions = _integer(
            robust_objective.get("repair_max_insertions", 3),
            "robust_objective.repair_max_insertions",
        )

        self.replan_min_interval_s = _positive(cfg.policy.get("replan_min_interval_s", 0.5),
                                               "policy.replan_min_interval_s", allow_zero=True)
        self.prediction_overrun_grace_s = _positive(cfg.policy.get("prediction_overrun_grace_s", 0.35),
                                                    "policy.prediction_overrun_grace_s", allow_zero=True)
        self.fault_recovery_hold_s = _positive(cfg.policy.get("fault_recovery_hold_s", 0.5),
                                               "policy.fault_recovery_hold_s", allow_zero=True)

        self.fault_kind, self.confirmation_delay_s, self.logical_pause = self._parse_fault_injection()
        self.commissioning_start_tolerance_deg = _positive(
            cfg.policy.get("commissioning", {}).get(
                "start_pose_tolerance_deg", max(1.0, self.pose_tolerance_deg)
            ) if isinstance(cfg.policy.get("commissioning", {}), Mapping) else max(1.0, self.pose_tolerance_deg),
            "policy.commissioning.start_pose_tolerance_deg",
        )
        self.commissioning_continuity_tolerance_deg = _positive(
            cfg.policy.get("commissioning", {}).get("continuity_tolerance_deg", 1e-3)
            if isinstance(cfg.policy.get("commissioning", {}), Mapping) else 1e-3,
            "policy.commissioning.continuity_tolerance_deg",
            allow_zero=True,
        )

        self.cells, self.evaluation_grid = self._build_cells()
        self._intrinsics_by_device: dict[str, Any] = {}
        self._target_angles_by_device: dict[str, tuple[tuple[float, float], ...]] = {}
        self.candidates = self._build_candidates()
        self.commissioning_rows: tuple[CommissioningRow, ...] = ()
        self.commissioning_meta: dict[str, tuple[str, CommissioningRow]] = {}
        self.commissioning_routes_template: dict[str, tuple[str, ...]] = {
            device_id: () for device_id in self.device_ids
        }
        if self.commissioning_mode:
            self.commissioning_rows = self._load_commissioning_rows()
            self._add_commissioning_candidates()
        self.candidates_by_device = {
            device_id: tuple(candidate for candidate in self.candidates.values()
                             if candidate.device_id == device_id)
            for device_id in self.device_ids
        }
        # The frozen evaluation grid has dense integer indices.  Python integer
        # bitsets preserve exact set semantics while making the two complete
        # greedy seed constructions predictable under four-stream RTSP load.
        # This cache is built outside the measured planner budget and does not
        # change candidate order or any tie-break key.
        self._candidate_coverage_masks = {
            candidate_id: sum(1 << index for index in candidate.coverage)
            for candidate_id, candidate in self.candidates.items()
        }
        reachable: set[int] = set()
        for candidate in self.candidates.values():
            reachable.update(candidate.coverage)
        self.reachable = frozenset(reachable)
        grid_document = cfg.policy.get("evaluation_grid")
        self.require_full_reachability = bool(cfg.policy.get(
            "require_full_reachability", isinstance(grid_document, Mapping)
        )) and not self.commissioning_mode
        if self.require_full_reachability and len(self.reachable) != len(self.cells):
            raise ValueError(
                "infeasible frozen evaluation task: "
                f"reachable_count={len(self.reachable)} < evaluation_cell_count={len(self.cells)}"
            )
        self.execution_models, self.execution_model_status = self._load_execution_models()
        self._planning_edge_cache: dict[
            tuple[str, float, float, float, float, bool, bool], tuple[float, float]
        ] = {}
        self.static_precompute_ms = 0.0
        self.static_precomputed_edge_count = 0
        if (self.method in MEASURED_METHODS and
                bool(cfg.policy.get("strict_measured_model", False)) and
                self.execution_model_status != "measured"):
            raise ValueError("measured method requested but no reviewed compatible execution model is available")
        self.reset()
        if self.is_v2:
            precompute_started = time.perf_counter()
            self.static_precomputed_edge_count = self._prewarm_v2_initial_edges()
            self.static_precompute_ms = (time.perf_counter() - precompute_started) * 1000.0
            # The edge cache is immutable with respect to execution state and
            # deliberately survives reset; no lifecycle state may do so.
            self.reset()

    # ------------------------------------------------------------------
    # Static geometry and model loading
    # ------------------------------------------------------------------
    def _build_cells(self) -> tuple[tuple[Cell, ...], tuple[int, int, int]]:
        explicit = self.cfg.policy.get("evaluation_points_m")
        if explicit is not None:
            points: list[tuple[float, float, float]] = []
            for index, raw in enumerate(explicit):
                if len(raw) != 3:
                    raise ValueError(f"evaluation_points_m[{index}] must contain x,y,z")
                point = tuple(float(value) for value in raw)
                if not all(math.isfinite(value) for value in point):
                    raise ValueError(f"evaluation_points_m[{index}] is non-finite")
                points.append(point)
            if not points:
                raise ValueError("evaluation_points_m cannot be empty")
            return tuple(Cell(i, f"cell_{i:04d}", point) for i, point in enumerate(points)), (len(points), 1, 1)

        roi = self.cfg.policy.get("roi", {"x": [100.0, 160.0], "y": [-50.0, 50.0], "z": [35.0, 85.0]})
        if not isinstance(roi, Mapping):
            raise ValueError("policy.roi must be a mapping")
        bounds: dict[str, tuple[float, float]] = {}
        for axis in ("x", "y", "z"):
            raw = roi.get(axis)
            if not isinstance(raw, Sequence) or len(raw) != 2:
                raise ValueError(f"policy.roi.{axis} must contain [low, high]")
            lo, hi = float(raw[0]), float(raw[1])
            if not math.isfinite(lo) or not math.isfinite(hi) or lo >= hi:
                raise ValueError(f"policy.roi.{axis} must be finite and increasing")
            bounds[axis] = (lo, hi)

        grid_document = self.cfg.policy.get("evaluation_grid", self.cfg.policy.get("coverage_grid"))
        declared_count: int | None = None
        if isinstance(grid_document, Mapping):
            raw_grid = grid_document.get("shape")
            if grid_document.get("cell_count") is not None:
                declared_count = _integer(grid_document["cell_count"],
                                          "evaluation_grid.cell_count")
        elif grid_document is not None:
            # Accepted for a small legacy/offline configuration.  The public
            # experiment configuration uses the explicit mapping schema.
            raw_grid = grid_document
        else:
            # Deprecated compatibility alias only.  It is never interpreted as
            # a candidate-view list; candidates are still generated below by
            # inverse geometry, angle quantisation, and coverage-bitset dedupe.
            raw_grid = self.cfg.policy.get("scan_grid", [3, 5, 2])
        if not isinstance(raw_grid, Sequence) or len(raw_grid) != 3:
            raise ValueError("policy.evaluation_grid must contain three integers")
        grid = tuple(_integer(n, "evaluation_grid") for n in raw_grid)
        if declared_count is not None and declared_count != math.prod(grid):
            raise ValueError(
                f"evaluation_grid.cell_count={declared_count} does not equal product(shape)={math.prod(grid)}"
            )
        alias = self.cfg.policy.get("scan_grid")
        if alias is not None and tuple(int(value) for value in alias) != grid:
            raise ValueError("deprecated policy.scan_grid alias must equal evaluation_grid.shape")
        maximum = _integer(self.cfg.policy.get("max_evaluation_cells", 2000),
                           "policy.max_evaluation_cells")
        if math.prod(grid) > maximum:
            raise ValueError(f"evaluation grid has {math.prod(grid)} cells, exceeding {maximum}")

        xs = _cell_centers(*bounds["x"], grid[0])
        ys = _cell_centers(*bounds["y"], grid[1])
        zs = _cell_centers(*bounds["z"], grid[2])
        points: list[tuple[float, float, float]] = []
        # A deterministic 3-D serpentine order is also the FPR within-partition
        # visit order; it is not used as evidence that a cell was observed.
        for iz, z in enumerate(zs):
            y_order = ys if iz % 2 == 0 else tuple(reversed(ys))
            for iy, y in enumerate(y_order):
                x_order = xs if (iy + iz) % 2 == 0 else tuple(reversed(xs))
                points.extend((float(x), float(y), float(z)) for x in x_order)
        return tuple(Cell(i, f"cell_{i:04d}", point) for i, point in enumerate(points)), grid

    def _inside_safe(self, device_id: str, yaw: float, pitch: float) -> bool:
        yaw_limits, pitch_limits = self.safe_limits[device_id]
        return yaw_limits[0] <= yaw <= yaw_limits[1] and pitch_limits[0] <= pitch <= pitch_limits[1]

    @staticmethod
    def _direction_from_angles(yaw_deg: float, pitch_deg: float) -> tuple[float, float, float]:
        yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
        cp = math.cos(pitch)
        return cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch)

    def _pinhole_coverage(self, device: DeviceConfig, yaw: float, pitch: float,
                          target_angles: Sequence[tuple[float, float]], intrinsics: Any,
                          margin: float | None = None) -> frozenset[int]:
        # Exact zero-distortion pinhole projection in the candidate camera
        # frame.  Mount rotation is already removed by world_to_gimbal_angles.
        cy, sy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
        edge = self.fov_margin if margin is None else margin
        u_min, u_max = edge * intrinsics.width, (1.0 - edge) * intrinsics.width
        v_min, v_max = edge * intrinsics.height, (1.0 - edge) * intrinsics.height
        covered: set[int] = set()
        for index, (target_yaw, target_pitch) in enumerate(target_angles):
            x, y, z = self._direction_from_angles(target_yaw, target_pitch)
            # R_candidate^T = Ry(+pitch) Rz(-yaw).
            x1, y1 = cy * x + sy * y, -sy * x + cy * y
            xc, yc, zc = cp * x1 + sp * z, y1, -sp * x1 + cp * z
            if xc <= 1e-9:
                continue
            u = intrinsics.cx - intrinsics.fx * yc / xc
            v = intrinsics.cy - intrinsics.fy * zc / xc
            if u_min <= u <= u_max and v_min <= v <= v_max:
                covered.add(index)
        return frozenset(covered)

    def _coverage_at_pose(self, device_id: str, yaw: float, pitch: float,
                          margin: float) -> frozenset[int]:
        """Project the frozen evaluation cells from one measured gimbal pose."""
        device = self.devices[device_id]
        intrinsics = self._intrinsics_by_device[device_id]
        target_angles = self._target_angles_by_device[device_id]
        distortion = tuple(float(x) for x in getattr(intrinsics, "dist", ()))
        if any(abs(value) > 1e-12 for value in distortion):
            state = Telemetry(device_id, 0.0, yaw, pitch, zoom=self.fixed_zoom[device_id])
            projected = project_world_points([cell.point_m for cell in self.cells], state, device)
            return frozenset(
                index for index, pixel in enumerate(projected)
                if pixel is not None and
                margin * intrinsics.width <= pixel[0] <= (1.0 - margin) * intrinsics.width and
                margin * intrinsics.height <= pixel[1] <= (1.0 - margin) * intrinsics.height
            )
        return self._pinhole_coverage(device, yaw, pitch, target_angles, intrinsics, margin)

    def _robust_candidate_coverage(self, device_id: str, yaw: float, pitch: float) -> frozenset[int]:
        """Cells visible for every corner of the configured pose-error box."""
        yaw_offsets = (-self.robust_pose_yaw_deg, self.robust_pose_yaw_deg)
        pitch_offsets = (-self.robust_pose_pitch_deg, self.robust_pose_pitch_deg)
        if self.robust_pose_yaw_deg == 0:
            yaw_offsets = (0.0,)
        if self.robust_pose_pitch_deg == 0:
            pitch_offsets = (0.0,)
        coverages = [
            self._coverage_at_pose(device_id, yaw + dyaw, pitch + dpitch,
                                   self.robust_pixel_margin)
            for dyaw in yaw_offsets for dpitch in pitch_offsets
        ]
        if not coverages:
            return frozenset()
        robust = set(coverages[0])
        for coverage in coverages[1:]:
            robust.intersection_update(coverage)
        return frozenset(robust)

    def _build_candidates(self) -> dict[str, Candidate]:
        result: dict[str, Candidate] = {}
        for device_id in self.device_ids:
            device = self.devices[device_id]
            zoom = self.fixed_zoom[device_id]
            if not device.zoom_limits[0] <= zoom <= device.zoom_limits[1]:
                raise ValueError(f"{device_id}: fixed zoom outside device limits")
            intrinsics = intrinsics_at_zoom(device, zoom)
            target_angles = tuple(world_to_gimbal_angles(cell.point_m, device) for cell in self.cells)
            self._intrinsics_by_device[device_id] = intrinsics
            self._target_angles_by_device[device_id] = target_angles
            seeds: set[tuple[float, float]] = set()
            for yaw, pitch in target_angles:
                quantized = (_quantize(yaw, self.quantization_deg[0]),
                             _quantize(pitch, self.quantization_deg[1]))
                if self._inside_safe(device_id, *quantized):
                    seeds.add(quantized)

            by_coverage: dict[frozenset[int], tuple[float, float]] = {}
            for yaw, pitch in sorted(seeds):
                coverage = (self._robust_candidate_coverage(device_id, yaw, pitch)
                            if self.use_robust_candidate_coverage else
                            self._coverage_at_pose(device_id, yaw, pitch, self.fov_margin))
                if not coverage:
                    continue
                previous = by_coverage.get(coverage)
                home_cost = abs(yaw - device.initial_yaw_deg) + abs(pitch - device.initial_pitch_deg)
                if previous is None:
                    by_coverage[coverage] = (yaw, pitch)
                else:
                    old_cost = abs(previous[0] - device.initial_yaw_deg) + abs(previous[1] - device.initial_pitch_deg)
                    if (home_cost, yaw, pitch) < (old_cost, previous[0], previous[1]):
                        by_coverage[coverage] = (yaw, pitch)

            ordered = sorted(((yaw, pitch, coverage) for coverage, (yaw, pitch) in by_coverage.items()),
                             key=lambda item: (item[0], item[1], tuple(sorted(item[2]))))
            if len(ordered) > self.max_candidates_per_device:
                raise ValueError(
                    f"{device_id}: {len(ordered)} unique candidates exceed max_candidates_per_device="
                    f"{self.max_candidates_per_device}; coarsen evaluation_grid/angle quantization explicitly"
                )
            for index, (yaw, pitch, coverage) in enumerate(ordered):
                candidate_id = f"{device_id}:q{index:04d}"
                nominal_coverage = self._coverage_at_pose(
                    device_id, yaw, pitch, self.confirmation_pixel_margin
                )
                result[candidate_id] = Candidate(
                    candidate_id, device_id, yaw, pitch, coverage, nominal_coverage
                )
        return result

    def _parse_fault_injection(
        self,
    ) -> tuple[str, dict[str, float], PauseInjection | None]:
        """Validate the frozen, policy-only perturbation schema once.

        Both perturbations are event-clock masks.  This module never sleeps,
        blocks a watchdog, disconnects a socket, or calls a device SDK.
        """
        raw = self.cfg.policy.get("fault_injection", {})
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValueError("policy.fault_injection must be a mapping")
        if not bool(raw.get("enabled", False)):
            return "none", {device_id: 0.0 for device_id in self.device_ids}, None
        if self.commissioning_mode:
            raise ValueError("fault_injection must be disabled in commissioning mode")

        kind = str(raw.get("kind", "")).strip().lower()
        if kind == "confirmation_delay":
            implementation = str(raw.get("implementation", "policy_event_clock_only"))
            if implementation != "policy_event_clock_only":
                raise ValueError("confirmation_delay implementation must be policy_event_clock_only")
            if raw.get("never_block_watchdog", True) is not True:
                raise ValueError("confirmation_delay.never_block_watchdog must be true")
            values = raw.get("delay_s_by_device_slot")
            if (not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or
                    len(values) != len(self.device_ids)):
                raise ValueError(
                    "confirmation_delay.delay_s_by_device_slot must contain one value per sorted active device"
                )
            delays = {
                device_id: _positive(values[slot], f"delay_s_by_device_slot[{slot}]", allow_zero=True)
                for slot, device_id in enumerate(self.device_ids)
            }
            return kind, delays, None

        if kind == "logical_availability_pause":
            implementation = str(raw.get("implementation", "policy_availability_mask"))
            if implementation != "policy_availability_mask":
                raise ValueError("logical_availability_pause implementation must be policy_availability_mask")
            if raw.get("last_command_expires_by_ttl", True) is not True:
                raise ValueError("logical_availability_pause.last_command_expires_by_ttl must be true")
            if raw.get("no_sdk_fault_injection", True) is not True:
                raise ValueError("logical_availability_pause.no_sdk_fault_injection must be true")
            duration = _positive(raw.get("duration_s", 0.0), "fault_injection.duration_s")
            trigger = str(raw.get("trigger", "mission_time")).strip().lower()
            if trigger == "coverage_fraction":
                fraction = _positive(raw.get("trigger_coverage_fraction", 0.5),
                                     "fault_injection.trigger_coverage_fraction")
                if fraction > 1.0:
                    raise ValueError("fault_injection.trigger_coverage_fraction must be <= 1")
                selection = str(raw.get("device_selection", "max_unique_reserved_coverage")).strip().lower()
                if selection != "max_unique_reserved_coverage":
                    raise ValueError(
                        "coverage-triggered pause device_selection must be max_unique_reserved_coverage"
                    )
                pause = PauseInjection(
                    mode="coverage_fraction", duration_s=duration,
                    trigger_coverage_fraction=fraction, selection_rule=selection,
                )
            elif trigger == "mission_time":
                slot = _integer(raw.get("device_slot", -1), "fault_injection.device_slot", minimum=0)
                if slot >= len(self.device_ids):
                    raise ValueError("fault_injection.device_slot is outside sorted active devices")
                start = _positive(raw.get("start_after_mission_s", 0.0),
                                  "fault_injection.start_after_mission_s", allow_zero=True)
                pause = PauseInjection(
                    mode="mission_time", duration_s=duration,
                    configured_device_id=self.device_ids[slot],
                    start_after_mission_s=start, selection_rule="configured_device",
                )
            else:
                raise ValueError("logical_availability_pause.trigger must be mission_time or coverage_fraction")
            return kind, {device_id: 0.0 for device_id in self.device_ids}, pause
        raise ValueError(
            "enabled policy.fault_injection.kind must be confirmation_delay or logical_availability_pause"
        )

    @staticmethod
    def _commissioning_number(row: Mapping[str, Any], key: str) -> float:
        try:
            value = float(row[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"commissioning row {key!r} must be numeric") from exc
        return _finite(value, f"commissioning.{key}")

    def _load_commissioning_rows(self) -> tuple[CommissioningRow, ...]:
        options = self.cfg.policy.get("commissioning", {})
        if not isinstance(options, Mapping):
            raise ValueError("policy.commissioning must be a mapping")
        inline = options.get("transitions")
        path_text = str(options.get("plan_path", "")).strip()
        if inline is not None and path_text:
            raise ValueError("commissioning accepts either transitions or plan_path, not both")
        raw_rows: Any
        document_commissioning_id = ""
        if inline is not None:
            raw_rows = inline
        elif path_text:
            path = self._resolve_model_path(path_text)
            try:
                if path.suffix.lower() == ".csv":
                    with path.open("r", encoding="utf-8-sig", newline="") as stream:
                        raw_rows = list(csv.DictReader(stream))
                else:
                    document = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(document, Mapping):
                        document_commissioning_id = str(document.get("commissioning_id", ""))
                        raw_rows = document.get("rows")
                    else:
                        raw_rows = document
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"cannot read commissioning plan {path}: {exc}") from exc
        else:
            raise ValueError("commissioning mode requires commissioning.plan_path or commissioning.transitions")
        if (not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)) or
                not raw_rows):
            raise ValueError("commissioning plan must contain at least one transition row")

        selected_raw = options.get("transition_ids", ())
        if not isinstance(selected_raw, Sequence) or isinstance(selected_raw, (str, bytes)):
            raise ValueError("commissioning.transition_ids must be a sequence")
        selected = tuple(str(value) for value in selected_raw)
        if len(set(selected)) != len(selected) or any(not value for value in selected):
            raise ValueError("commissioning.transition_ids must be unique non-empty strings")
        selected_set = set(selected)
        expected_id = str(options.get("commissioning_id", "")).strip()
        if expected_id and document_commissioning_id and expected_id != document_commissioning_id:
            raise ValueError("commissioning_id does not match plan document")

        parsed: list[CommissioningRow] = []
        seen_ids: set[str] = set()
        for index, raw_row in enumerate(raw_rows):
            if not isinstance(raw_row, Mapping):
                raise ValueError(f"commissioning row {index} must be a mapping")
            transition_id = str(raw_row.get("transition_id", "")).strip()
            if not transition_id:
                raise ValueError(f"commissioning row {index} has an empty transition_id")
            if transition_id in seen_ids:
                raise ValueError(f"duplicate commissioning transition_id {transition_id!r}")
            seen_ids.add(transition_id)
            if selected_set and transition_id not in selected_set:
                continue
            device_id = str(raw_row.get("device_id", "")).strip()
            if device_id not in self.devices:
                raise ValueError(f"commissioning transition {transition_id!r} names unknown device {device_id!r}")
            try:
                raw_step = int(raw_row.get("step", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"commissioning transition {transition_id!r} has invalid step") from exc
            step = _integer(raw_step, f"commissioning[{transition_id}].step")
            commissioning_id = str(raw_row.get("commissioning_id", document_commissioning_id)).strip()
            if expected_id and commissioning_id != expected_id:
                raise ValueError(f"commissioning transition {transition_id!r} has a different commissioning_id")
            row = CommissioningRow(
                commissioning_id=commissioning_id,
                device_id=device_id,
                step=step,
                transition_id=transition_id,
                split=str(raw_row.get("split", "")).strip(),
                planned_from_yaw_deg=self._commissioning_number(raw_row, "planned_from_yaw_deg"),
                planned_from_pitch_deg=self._commissioning_number(raw_row, "planned_from_pitch_deg"),
                planned_to_yaw_deg=self._commissioning_number(raw_row, "planned_to_yaw_deg"),
                planned_to_pitch_deg=self._commissioning_number(raw_row, "planned_to_pitch_deg"),
            )
            if not self._inside_safe(device_id, row.planned_from_yaw_deg, row.planned_from_pitch_deg):
                raise ValueError(f"commissioning transition {transition_id!r} planned_from is outside safe limits")
            if not self._inside_safe(device_id, row.planned_to_yaw_deg, row.planned_to_pitch_deg):
                raise ValueError(f"commissioning transition {transition_id!r} planned_to is outside safe limits")
            parsed.append(row)
        if selected_set - seen_ids:
            raise ValueError(f"commissioning transition_ids not found: {sorted(selected_set - seen_ids)}")
        if not parsed:
            raise ValueError("commissioning transition selection is empty")

        by_device: dict[str, list[CommissioningRow]] = {device_id: [] for device_id in self.device_ids}
        for row in parsed:
            by_device[row.device_id].append(row)
        ordered: list[CommissioningRow] = []
        ids = {row.commissioning_id for row in parsed if row.commissioning_id}
        if len(ids) > 1:
            raise ValueError("commissioning rows contain multiple commissioning_id values")
        for device_id in self.device_ids:
            rows = sorted(by_device[device_id], key=lambda row: (row.step, row.transition_id))
            if len({row.step for row in rows}) != len(rows):
                raise ValueError(f"{device_id}: duplicate commissioning step")
            for previous, current in zip(rows, rows[1:]):
                if current.step == previous.step + 1:
                    discontinuity = max(
                        _angle_error(previous.planned_to_yaw_deg, current.planned_from_yaw_deg),
                        abs(previous.planned_to_pitch_deg - current.planned_from_pitch_deg),
                    )
                    if discontinuity > self.commissioning_continuity_tolerance_deg:
                        raise ValueError(
                            f"{device_id}: adjacent commissioning rows are discontinuous at step {current.step}"
                        )
            ordered.extend(rows)
        return tuple(ordered)

    def _add_commissioning_candidates(self) -> None:
        routes: dict[str, list[str]] = {device_id: [] for device_id in self.device_ids}
        rows_by_device = {
            device_id: [row for row in self.commissioning_rows if row.device_id == device_id]
            for device_id in self.device_ids
        }
        for device_id in self.device_ids:
            previous_target: tuple[float, float] | None = None
            for row in rows_by_device[device_id]:
                planned_from = (row.planned_from_yaw_deg, row.planned_from_pitch_deg)
                needs_preposition = previous_target is None or max(
                    _angle_error(previous_target[0], planned_from[0]),
                    abs(previous_target[1] - planned_from[1]),
                ) > self.commissioning_continuity_tolerance_deg
                if needs_preposition:
                    prep_id = f"commissioning:preposition:{row.transition_id}"
                    self.candidates[prep_id] = Candidate(
                        prep_id, device_id, planned_from[0], planned_from[1], frozenset()
                    )
                    self.commissioning_meta[prep_id] = ("preposition", row)
                    routes[device_id].append(prep_id)
                sample_id = f"commissioning:sample:{row.transition_id}"
                self.candidates[sample_id] = Candidate(
                    sample_id, device_id, row.planned_to_yaw_deg, row.planned_to_pitch_deg, frozenset()
                )
                self.commissioning_meta[sample_id] = ("sample", row)
                routes[device_id].append(sample_id)
                previous_target = (row.planned_to_yaw_deg, row.planned_to_pitch_deg)
        self.commissioning_routes_template = {
            device_id: tuple(routes[device_id]) for device_id in self.device_ids
        }

    def _resolve_model_path(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        source = Path(self.cfg.source) if self.cfg.source else None
        return ((source.parent if source else Path.cwd()) / path).resolve()

    def _load_execution_models(self) -> tuple[dict[str, Mapping[str, Any]], str]:
        options = self.cfg.policy.get("execution_model", {})
        if not isinstance(options, Mapping):
            raise ValueError("policy.execution_model must be a mapping")
        path_text = str(options.get("model_path", "")).strip()
        if not path_text:
            return {}, "nominal_fallback"
        path = self._resolve_model_path(path_text)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read execution model {path}: {exc}") from exc
        target = str(document.get("target_definition", ""))
        if "FRAME_CONFIRM" not in target and target != "command_issued_to_frame_confirm":
            raise ValueError("execution model target must end at FRAME_CONFIRM")
        rule = str(document.get("routing_cost_rule", ""))
        if rule and "do not add dwell" not in rule.lower():
            raise ValueError("execution model routing_cost_rule does not confirm dwell-inclusive use")
        models = document.get("models")
        if not isinstance(models, Mapping):
            raise ValueError("execution model must contain a models mapping")
        parsed: dict[str, Mapping[str, Any]] = {}
        required = {"intercept", "yaw_positive_deg", "yaw_negative_deg", "pitch_up_deg", "pitch_down_deg"}
        for device_id in self.device_ids:
            row = models.get(device_id)
            if not isinstance(row, Mapping):
                continue
            coefficients = row.get("coefficients")
            if not isinstance(coefficients, Mapping) or not required <= set(coefficients):
                raise ValueError(f"{device_id}: execution model coefficients are incomplete")
            values = [float(coefficients[key]) for key in sorted(required)]
            padding = float(row.get("one_sided_padding_s", 0.0))
            banded = row.get("one_sided_padding_s_by_regime", {})
            if banded and (not isinstance(banded, Mapping) or set(banded) != {"small", "medium", "large"}):
                raise ValueError(
                    f"{device_id}: one_sided_padding_s_by_regime must contain small/medium/large"
                )
            band_values = [float(value) for value in banded.values()] if isinstance(banded, Mapping) else []
            thresholds = row.get("angle_regime_thresholds_deg", [3.0, 12.0])
            if (not isinstance(thresholds, Sequence) or isinstance(thresholds, (str, bytes)) or
                    len(thresholds) != 2 or not 0 < float(thresholds[0]) < float(thresholds[1])):
                raise ValueError(f"{device_id}: angle_regime_thresholds_deg must be two increasing positives")
            if any(not math.isfinite(value) or value < 0 for value in (*values, padding, *band_values)):
                raise ValueError(f"{device_id}: execution model coefficients/padding must be finite and non-negative")
            parsed[device_id] = row
        return parsed, "measured" if len(parsed) == len(self.device_ids) else "partial_nominal_fallback"

    # ------------------------------------------------------------------
    # Lifecycle and low-level gates
    # ------------------------------------------------------------------
    def reset(self) -> None:
        if getattr(self, "require_full_reachability", False) and len(self.reachable) != len(self.cells):
            raise ValueError(
                "infeasible frozen evaluation task at reset: "
                f"reachable_count={len(self.reachable)} < evaluation_cell_count={len(self.cells)}"
            )
        self.confirmed: set[int] = set()
        if getattr(self, "commissioning_mode", False):
            self.routes = {
                device_id: list(self.commissioning_routes_template[device_id])
                for device_id in self.device_ids
            }
        else:
            self.routes = {device_id: [] for device_id in self.device_ids}
        self.active: dict[str, ActiveTask | None] = {device_id: None for device_id in self.device_ids}
        self.faults: dict[str, FaultState] = {}
        self.attempt_counts: dict[str, int] = {}
        self.commissioning_completed_ids: set[str] = set()
        self.seen_result_signatures: set[tuple[Any, ...]] = set()
        self.mission_started_t: float | None = None
        self.mission_completed_t: float | None = None
        self.mission_started_emitted = False
        self.mission_completed_emitted = False
        self.pending_replan = False
        self.pending_replan_reason = ""
        self.pending_replan_since_t: float | None = None
        self.last_replan_t = -math.inf
        self.last_planner_ms = 0.0
        self.total_plans = 0
        self.last_plan_audit: dict[str, Any] = {}
        self.last_effective_replan_t: float | None = None
        self.pending_reassigned_cells: dict[int, dict[str, Any]] = {}
        self.decision_count = 0
        self._last_context_t: float | None = None
        if self.logical_pause is not None:
            self.logical_pause.selected_device_id = None
            self.logical_pause.selected_unique_residual_count = 0
            self.logical_pause.started_t = None

    def _elapsed(self, t: float) -> float:
        return max(0.0, t - self.mission_started_t) if self.mission_started_t is not None else 0.0

    def _fresh_state(self, context: PolicyContext, device_id: str) -> Telemetry | None:
        state = context.devices.get(device_id)
        if state is None or not state.connected:
            return None
        age = context.t - state.t
        if not math.isfinite(age) or not 0 <= age <= self.telemetry_stale_s:
            return None
        return state

    def _pause_reason(self, context: PolicyContext, device_id: str) -> str | None:
        metrics = context.metrics if isinstance(context.metrics, Mapping) else {}
        for key in (f"pause:{device_id}", f"pause_{device_id}", f"device_pause.{device_id}"):
            if bool(metrics.get(key, False)):
                return "logical_pause"
        if self.fault_kind != "logical_availability_pause" or self.logical_pause is None:
            return None
        pause = self.logical_pause
        if pause.mode == "mission_time" and pause.started_t is None:
            start = float(pause.start_after_mission_s or 0.0)
            if self._elapsed(context.t) + 1e-9 >= start:
                pause.selected_device_id = pause.configured_device_id
                pause.started_t = (self.mission_started_t or context.t) + start
        if device_id != pause.selected_device_id or pause.started_t is None:
            return None
        return ("logical_pause"
                if pause.started_t <= context.t < pause.started_t + pause.duration_s
                else None)

    def _owned_residual_by_device(self) -> dict[str, set[int]]:
        """Current active/queued robust coverage ownership, excluding evidence."""
        owned: dict[str, set[int]] = {device_id: set() for device_id in self.device_ids}
        for device_id in self.device_ids:
            task = self.active.get(device_id)
            if task is not None and task.stage != "COMPLETE" and task.task_kind == "coverage":
                owned[device_id].update(self.candidates[task.candidate_id].coverage - self.confirmed)
            for candidate_id in self.routes[device_id]:
                owned[device_id].update(self.candidates[candidate_id].coverage - self.confirmed)
        return owned

    def _update_coverage_triggered_pause(self, context: PolicyContext) -> None:
        pause = self.logical_pause
        if (pause is None or pause.mode != "coverage_fraction" or pause.started_t is not None or
                self.mission_started_t is None):
            return
        threshold = float(pause.trigger_coverage_fraction or 1.0)
        if len(self.confirmed) / max(1, len(self.cells)) + 1e-12 < threshold:
            return
        owned = self._owned_residual_by_device()
        scored: list[tuple[int, int, str]] = []
        for device_id in self.device_ids:
            if device_id in self.faults or (self.active.get(device_id) is None and not self.routes[device_id]):
                continue
            others: set[int] = set()
            for other_id, cells in owned.items():
                if other_id != device_id:
                    others.update(cells)
            unique_count = len(owned[device_id] - others)
            scored.append((-unique_count, -len(owned[device_id]), device_id))
        if not scored:
            return
        score = min(scored)
        pause.selected_device_id = score[2]
        pause.selected_unique_residual_count = -score[0]
        pause.started_t = float(context.t)

    def _base_unavailable_reason(self, context: PolicyContext, device_id: str) -> str | None:
        pause = self._pause_reason(context, device_id)
        if pause:
            return pause
        state = context.devices.get(device_id)
        if state is None:
            return "missing_telemetry"
        if not state.connected:
            return "disconnected"
        age = context.t - state.t
        if not math.isfinite(age) or age < 0 or age > self.telemetry_stale_s:
            return "stale_telemetry"
        if self.require_zoom_verified and state.source != "simulation":
            if not bool(state.raw.get("zoom_known", False)):
                return "zoom_unknown"
            if not bool(state.raw.get("zoom_stable", False)):
                return "zoom_unstable"
            if abs(float(state.zoom) - self.fixed_zoom[device_id]) > self.zoom_tolerance:
                return "fixed_zoom_mismatch"
        return None

    def _pose_stable(self, state: Telemetry, candidate: Candidate) -> bool:
        return (_angle_error(state.yaw_deg, candidate.yaw_deg) <= self.pose_tolerance_deg and
                abs(state.pitch_deg - candidate.pitch_deg) <= self.pose_tolerance_deg and
                abs(state.yaw_rate_dps) <= self.max_rate_dps and
                abs(state.pitch_rate_dps) <= self.max_rate_dps)

    def _task_reachable_now(self, state: Telemetry, candidate: Candidate) -> bool:
        if not self._inside_safe(candidate.device_id, candidate.yaw_deg, candidate.pitch_deg):
            return False
        if "yaw_deg" not in self.enabled and _angle_error(state.yaw_deg, candidate.yaw_deg) > self.pose_tolerance_deg:
            return False
        if "pitch_deg" not in self.enabled and abs(state.pitch_deg - candidate.pitch_deg) > self.pose_tolerance_deg:
            return False
        return True

    def _fresh_usable_frame(self, context: PolicyContext, device_id: str) -> Any | None:
        frames = getattr(context, "frames", {})
        if not isinstance(frames, Mapping):
            return None
        frame = frames.get(device_id)
        if frame is None or not bool(getattr(frame, "usable", False)):
            return None
        capture_t = float(getattr(frame, "capture_t", math.nan))
        age = context.t - capture_t
        if not math.isfinite(age) or not -0.05 <= age <= self.max_frame_age_s:
            return None
        return frame

    def _hold_action(self, context: PolicyContext, device_id: str, reason: str) -> Action:
        # Empty Action stops a prior velocity_p command in the platform's
        # hardware adapter; it does not invent an out-of-envelope target.
        return Action(device_id, issued_t=float(context.t), ttl_s=self.ttl_s, reason=reason)

    def _target_action(self, context: PolicyContext, task: ActiveTask, candidate: Candidate) -> Action:
        if task.command_started_t is None:
            task.command_started_t = float(context.t)
        task.last_action_issued_t = float(context.t)
        return Action(
            candidate.device_id,
            yaw_deg=candidate.yaw_deg if "yaw_deg" in self.enabled else None,
            pitch_deg=candidate.pitch_deg if "pitch_deg" in self.enabled else None,
            issued_t=float(context.t),
            ttl_s=self.ttl_s,
            reason=f"{self.method}:move:{task.attempt_id}",
        )

    def _result_signature(self, result: Any) -> tuple[Any, ...]:
        action = getattr(result, "action", None)
        return (
            getattr(result, "device_id", ""),
            float(getattr(result, "t", -1.0)),
            str(getattr(result, "status", "")),
            str(getattr(result, "error", "")),
            float(getattr(action, "issued_t", -1.0)) if action is not None else -1.0,
            str(getattr(action, "reason", "")) if action is not None else "",
        )

    def _related_result(self, context: PolicyContext, task: ActiveTask, device_id: str) -> Any | None:
        results = getattr(context, "command_results", {})
        if not isinstance(results, Mapping):
            return None
        result = results.get(device_id)
        if result is None:
            return None
        action = getattr(result, "action", None)
        if action is None or task.attempt_id not in str(getattr(action, "reason", "")):
            return None
        issued_t = float(getattr(action, "issued_t", -math.inf))
        if task.command_started_t is not None and issued_t + 1e-9 < task.command_started_t:
            return None
        return result

    @staticmethod
    def _result_failed(result: Any) -> bool:
        status = str(getattr(result, "status", "")).strip().lower()
        error = str(getattr(result, "error", "")).strip()
        bad = ("error", "failed", "rejected", "timeout", "watchdog_interrupted", "not_sent")
        return bool(error) or any(token in status for token in bad)

    # ------------------------------------------------------------------
    # Transition-time model and planner
    # ------------------------------------------------------------------
    def _nominal_edge(self, device_id: str, start: tuple[float, float],
                      target: tuple[float, float]) -> tuple[float, float]:
        device = self.devices[device_id]
        dyaw = _angle_error(target[0], start[0])
        dpitch = abs(target[1] - start[1])
        speed = _positive(self.cfg.policy.get("nominal_slew_dps", device.max_slew_dps),
                          "policy.nominal_slew_dps")
        base = _positive(self.cfg.policy.get("nominal_command_overhead_s", 0.20),
                         "policy.nominal_command_overhead_s", allow_zero=True)
        frame_wait = (_positive(self.cfg.policy.get("nominal_frame_wait_s", 0.20),
                                "policy.nominal_frame_wait_s", allow_zero=True)
                      if self.require_frame else 0.0)
        # Axes move concurrently in velocity_p.  This is the nominal comparator;
        # measured methods use the calibrated end-to-end model below.
        prediction = base + max(dyaw, dpitch) / speed + self.settle_hold_s + self.min_dwell_s + frame_wait
        uncertainty = _positive(self.cfg.policy.get("nominal_uncertainty_s", 0.0),
                                "policy.nominal_uncertainty_s", allow_zero=True)
        return prediction, uncertainty

    def _measured_edge(self, device_id: str, start: tuple[float, float],
                       target: tuple[float, float]) -> tuple[float, float]:
        # Commissioning fits contain only commanded non-zero transitions.  Its
        # regression intercept must therefore not be extrapolated to a view
        # that is already inside the execution pose gate.  Such a task still
        # pays the complete settle/dwell/new-frame confirmation time, captured
        # by the independently configured conservative stationary upper bound.
        if self._is_stationary_transition(start, target):
            return self.stationary_confirmation_upper_s, 0.0
        row = self.execution_models.get(device_id)
        if row is None:
            nominal, uncertainty = self._nominal_edge(device_id, start, target)
            penalty = _positive(self.cfg.policy.get("uncalibrated_penalty_s", 1.0),
                                "policy.uncalibrated_penalty_s", allow_zero=True)
            return nominal + penalty, uncertainty + penalty
        coefficients = row["coefficients"]
        dyaw = (target[0] - start[0] + 180.0) % 360.0 - 180.0
        dpitch = target[1] - start[1]
        point = (
            float(coefficients["intercept"]) +
            float(coefficients["yaw_positive_deg"]) * max(dyaw, 0.0) +
            float(coefficients["yaw_negative_deg"]) * max(-dyaw, 0.0) +
            float(coefficients["pitch_up_deg"]) * max(dpitch, 0.0) +
            float(coefficients["pitch_down_deg"]) * max(-dpitch, 0.0) +
            float(coefficients.get("dual_axis_parallel", 0.0)) *
            (1.0 if abs(dyaw) > 1e-9 and abs(dpitch) > 1e-9 else 0.0)
        )
        padding = float(row.get("one_sided_padding_s", 0.0))
        banded = row.get("one_sided_padding_s_by_regime")
        if isinstance(banded, Mapping):
            low, high = (float(value) for value in row.get("angle_regime_thresholds_deg", [3.0, 12.0]))
            magnitude = max(_angle_error(target[0], start[0]), abs(dpitch))
            regime = "small" if magnitude <= low else "medium" if magnitude <= high else "large"
            padding = float(banded[regime])
        # The fitted target already ends at FRAME_CONFIRM and includes dwell;
        # adding settle/dwell/frame time here would double count it.
        return max(0.0, point + padding), max(0.0, padding)

    def _model_padding(self, device_id: str, start: tuple[float, float],
                       target: tuple[float, float]) -> float:
        if self._is_stationary_transition(start, target):
            return 0.0
        row = self.execution_models.get(device_id)
        if row is None:
            return 0.0
        banded = row.get("one_sided_padding_s_by_regime")
        if not isinstance(banded, Mapping):
            return float(row.get("one_sided_padding_s", 0.0))
        low, high = (float(value) for value in row.get("angle_regime_thresholds_deg", [3.0, 12.0]))
        magnitude = max(_angle_error(target[0], start[0]), abs(target[1] - start[1]))
        regime = "small" if magnitude <= low else "medium" if magnitude <= high else "large"
        return float(banded[regime])

    def _is_stationary_transition(self, start: tuple[float, float],
                                  target: tuple[float, float]) -> bool:
        return (
            _angle_error(target[0], start[0]) <= self.pose_tolerance_deg and
            abs(target[1] - start[1]) <= self.pose_tolerance_deg
        )

    def _edge(self, device_id: str, start: tuple[float, float],
              target: tuple[float, float], measured: bool) -> tuple[float, float]:
        return self._measured_edge(device_id, start, target) if measured else self._nominal_edge(device_id, start, target)

    def _planning_edge(self, device_id: str, start: tuple[float, float],
                       target: tuple[float, float], measured: bool) -> tuple[float, float]:
        """Return a deterministic first-edge upper over the homing residual box."""
        cache_key = (
            device_id,
            float(start[0]), float(start[1]),
            float(target[0]), float(target[1]),
            bool(measured), bool(self._planning_with_initial_pose_box),
        )
        cached = self._planning_edge_cache.get(cache_key)
        if cached is not None:
            return cached
        device = self.devices[device_id]
        home = (float(device.initial_yaw_deg), float(device.initial_pitch_deg))
        if (not self._planning_with_initial_pose_box or
                _angle_error(start[0], home[0]) > 1e-9 or
                abs(start[1] - home[1]) > 1e-9):
            value = self._edge(device_id, start, target, measured)
            self._planning_edge_cache[cache_key] = value
            return value
        yaw_offsets = (-self.initial_pose_yaw_bound_deg, self.initial_pose_yaw_bound_deg)
        pitch_offsets = (-self.initial_pose_pitch_bound_deg, self.initial_pose_pitch_bound_deg)
        if self.initial_pose_yaw_bound_deg == 0:
            yaw_offsets = (0.0,)
        if self.initial_pose_pitch_bound_deg == 0:
            pitch_offsets = (0.0,)
        values = [
            self._edge(
                device_id,
                (home[0] + dyaw, home[1] + dpitch),
                target,
                measured,
            )
            for dyaw in yaw_offsets for dpitch in pitch_offsets
        ]
        value = max(item[0] for item in values), max(item[1] for item in values)
        self._planning_edge_cache[cache_key] = value
        return value

    def _prewarm_v2_initial_edges(self) -> int:
        """Populate deterministic initial-seed edges before hardware starts.

        The runtime constructs the policy before starting RTSP or device
        workers.  Both complete V2 seeds begin at the configured home and use
        only immutable candidate geometry plus the frozen execution model.
        Replaying the seed constructions here fills the expensive lazy edge
        entries without caching a route or objective.  The measured planner
        still reconstructs both seeds, evaluates their objectives, and runs
        the same bounded local search.
        """
        if not self.is_v2 or not self.reachable:
            return 0
        before = len(self._planning_edge_cache)
        start_poses = {
            device_id: (
                float(self.devices[device_id].initial_yaw_deg),
                float(self.devices[device_id].initial_pitch_deg),
            )
            for device_id in self.device_ids
        }
        previous_box = self._planning_with_initial_pose_box
        self._planning_with_initial_pose_box = bool(self.initial_pose_box_enabled)
        try:
            residual = set(self.reachable)
            for measured in (False, True):
                routes = self._plan_greedy_complete(
                    None, residual, start_poses, measured=measured,
                )
                # Greedy scoring touches every edge from each selected tail.
                # This additional evaluation is read-only and warms the exact
                # committed route transitions used by the objective function.
                self._plan_objective(routes, start_poses, residual)
        finally:
            self._planning_with_initial_pose_box = previous_box
        return len(self._planning_edge_cache) - before

    def _start_poses(self, context: PolicyContext, *, configured_home: bool = False
                     ) -> dict[str, tuple[float, float]]:
        result = {}
        for device_id, device in self.devices.items():
            if configured_home:
                result[device_id] = (
                    float(device.initial_yaw_deg), float(device.initial_pitch_deg)
                )
                continue
            state = self._fresh_state(context, device_id)
            result[device_id] = ((float(state.yaw_deg), float(state.pitch_deg)) if state else
                                 (float(device.initial_yaw_deg), float(device.initial_pitch_deg)))
        return result

    def _route_cost(self, device_id: str, route: Sequence[str], start_pose: tuple[float, float],
                    measured: bool) -> tuple[float, float]:
        total = uncertainty = 0.0
        previous = start_pose
        for candidate_id in route:
            candidate = self.candidates[candidate_id]
            cost, spread = self._planning_edge(
                device_id, previous, (candidate.yaw_deg, candidate.pitch_deg), measured
            )
            total += cost
            uncertainty += spread
            previous = (candidate.yaw_deg, candidate.pitch_deg)
        return total, uncertainty

    def _plan_objective(self, routes: Mapping[str, Sequence[str]],
                        start_poses: Mapping[str, tuple[float, float]], target: set[int],
                        *, start_offsets: Mapping[str, float] | None = None,
                        coverage_mode: str = "robust") -> PlanObjective:
        """Evaluate robust first-cover latency from the exact route sequence."""
        if coverage_mode not in {"robust", "nominal"}:
            raise ValueError("coverage_mode must be robust or nominal")
        if not target:
            return PlanObjective(0, 0.0, 0.0, 0.0, 0.0, 0.0)
        first = {index: math.inf for index in target}
        loads: list[float] = []
        total_motion = 0.0
        offsets = start_offsets or {}
        for device_id in self.device_ids:
            elapsed = max(0.0, float(offsets.get(device_id, 0.0)))
            previous = start_poses[device_id]
            for candidate_id in routes.get(device_id, ()):
                candidate = self.candidates[candidate_id]
                upper, _ = self._planning_edge(
                    device_id, previous, (candidate.yaw_deg, candidate.pitch_deg), True)
                elapsed += upper
                total_motion += math.hypot(
                    _angle_error(candidate.yaw_deg, previous[0]),
                    candidate.pitch_deg - previous[1],
                )
                candidate_coverage = (
                    candidate.nominal_coverage
                    if coverage_mode == "nominal" and candidate.nominal_coverage
                    else candidate.coverage
                )
                for index in candidate_coverage & target:
                    first[index] = min(first[index], elapsed)
                previous = (candidate.yaw_deg, candidate.pitch_deg)
            loads.append(elapsed)
        uncovered = sum(not math.isfinite(value) for value in first.values())
        raw_times = sorted(first.values())
        clipped = [min(value, self.objective_horizon_s) for value in raw_times]
        quantile_index = max(0, math.ceil(0.95 * len(raw_times)) - 1)
        mean_load = sum(loads) / len(loads) if loads else 0.0
        load_cv = ((math.sqrt(sum((value - mean_load) ** 2 for value in loads) / len(loads)) /
                    mean_load) if loads and mean_load > 0 else 0.0)
        return PlanObjective(
            uncovered_count=uncovered,
            uac_h_s=sum(clipped) / len(clipped),
            t95_s=raw_times[quantile_index],
            t100_s=max(raw_times),
            total_motion_deg=total_motion,
            load_cv=load_cv,
        )

    def _v2_objective_acceptable(self, proposal: PlanObjective, incumbent: PlanObjective,
                                 tail_cap_s: float, *, minimum_uac_gain_s: float = 0.0) -> bool:
        if proposal.uncovered_count != 0 or proposal.t100_s > tail_cap_s + self.objective_epsilon_s:
            return False
        gain = incumbent.uac_h_s - proposal.uac_h_s
        return gain > max(minimum_uac_gain_s, self.objective_epsilon_s)

    def _plan_greedy_complete(
        self,
        context: PolicyContext,
        residual: set[int],
        start_poses: Mapping[str, tuple[float, float]],
        *,
        measured: bool,
    ) -> dict[str, list[str]]:
        """Complete deterministic append-only construction for a V2 seed."""
        routes = {device_id: [] for device_id in self.device_ids}
        tails = dict(start_poses)
        uncovered_mask = sum(1 << index for index in residual)
        evaluations = 0
        while uncovered_mask:
            best: tuple[Any, ...] | None = None
            for device_id in self.device_ids:
                if device_id in self.faults:
                    continue
                for candidate in self.candidates_by_device[device_id]:
                    overlap_mask = (
                        self._candidate_coverage_masks[candidate.candidate_id]
                        & uncovered_mask
                    )
                    if not overlap_mask:
                        continue
                    evaluations += 1
                    if evaluations > self.planner_max_evaluations:
                        raise ValueError(
                            "V2 complete seed exceeded planner_max_evaluations before complete coverage"
                        )
                    cost, _ = self._planning_edge(
                        device_id, tails[device_id],
                        (candidate.yaw_deg, candidate.pitch_deg), measured,
                    )
                    gain = overlap_mask.bit_count()
                    key = (-gain / (cost + self.score_epsilon), cost,
                           device_id, candidate.candidate_id)
                    if best is None or key < best[0]:
                        best = (key, device_id, candidate)
            if best is None:
                break
            _, device_id, candidate = best
            routes[device_id].append(candidate.candidate_id)
            tails[device_id] = (candidate.yaw_deg, candidate.pitch_deg)
            uncovered_mask &= ~self._candidate_coverage_masks[candidate.candidate_id]
        return routes

    def _plan_mag_complete(self, context: PolicyContext, residual: set[int],
                           start_poses: Mapping[str, tuple[float, float]]) -> dict[str, list[str]]:
        """MAG construction without a wall-clock early exit for the V2 incumbent."""
        return self._plan_greedy_complete(
            context, residual, start_poses, measured=False
        )

    @staticmethod
    def _copy_routes(routes: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
        return {device_id: list(route) for device_id, route in routes.items()}

    def _plan_v2(self, context: PolicyContext, residual: set[int],
                 start_poses: Mapping[str, tuple[float, float]], deadline: float
                 ) -> dict[str, list[str]]:
        """MAG-incumbent anytime search with a hard robust-T100 guard."""
        baseline = self._plan_mag_complete(context, residual, start_poses)
        baseline_objective = self._plan_objective(baseline, start_poses, residual)
        if baseline_objective.uncovered_count:
            raise ValueError(
                "V2 cannot establish the MAG non-regression invariant: incumbent is incomplete"
            )
        tail_cap = baseline_objective.t100_s
        baseline_nominal_objective = self._plan_objective(
            baseline, start_poses, residual, coverage_mode="nominal"
        )
        nominal_tail_cap = baseline_nominal_objective.t100_s * self.opportunistic_tail_factor
        incumbent = self._copy_routes(baseline)
        incumbent_objective = baseline_objective
        incumbent_nominal_objective = baseline_nominal_objective
        accepted_moves: list[str] = []
        evaluations = 0
        evaluation_limit = min(
            self.planner_max_evaluations,
            (self.initial_local_search_max_evaluations
             if self.total_plans == 0 else self.repair_local_search_max_evaluations),
        )
        operation_counts: dict[str, int] = {}
        rejection_counts: dict[str, int] = {}
        search_stop_reason = "neighborhoods_exhausted"

        def increment(counter: dict[str, int], key: str) -> None:
            counter[key] = counter.get(key, 0) + 1

        def should_stop() -> bool:
            nonlocal search_stop_reason
            if evaluations >= evaluation_limit:
                search_stop_reason = "evaluation_limit"
                return True
            if time.perf_counter() >= deadline:
                search_stop_reason = "wall_clock_deadline"
                return True
            return False

        def rejection_reason(proposal: PlanObjective, current: PlanObjective,
                             nominal: PlanObjective) -> str:
            if proposal.uncovered_count != 0:
                return "incomplete_coverage"
            if proposal.t100_s > tail_cap + self.objective_epsilon_s:
                return "t100_cap_exceeded"
            if (self.opportunistic_tail_nonregression and
                    nominal.t100_s > nominal_tail_cap + self.objective_epsilon_s):
                return "opportunistic_t100_cap_exceeded"
            if current.uac_h_s - proposal.uac_h_s <= self.objective_epsilon_s:
                return "uac_not_strictly_improved"
            return "unknown"

        def acceptable(robust: PlanObjective, current: PlanObjective,
                       nominal: PlanObjective) -> bool:
            if not self._v2_objective_acceptable(robust, current, tail_cap):
                return False
            return (
                not self.opportunistic_tail_nonregression or
                nominal.t100_s <= nominal_tail_cap + self.objective_epsilon_s
            )

        # The second seed is a complete deterministic measured-time static
        # construction.  Unlike the former deadline-truncated insertion seed,
        # it cannot change with CPU scheduling or enter the feasible set only
        # because a partial construction happened to stop at a different view.
        if time.perf_counter() < deadline:
            measured_seed = self._plan_greedy_complete(
                context, residual, start_poses, measured=True
            )
            seed_objective = self._plan_objective(measured_seed, start_poses, residual)
            seed_nominal_objective = self._plan_objective(
                measured_seed, start_poses, residual, coverage_mode="nominal"
            )
            increment(operation_counts, "measured_static_seed")
            if acceptable(seed_objective, incumbent_objective, seed_nominal_objective):
                incumbent = self._copy_routes(measured_seed)
                incumbent_objective = seed_objective
                incumbent_nominal_objective = seed_nominal_objective
                accepted_moves.append("measured_static_seed")
            else:
                increment(rejection_counts, rejection_reason(
                    seed_objective, incumbent_objective, seed_nominal_objective
                ))
        else:
            search_stop_reason = "wall_clock_deadline"

        def consider(proposal: dict[str, list[str]], kind: str) -> bool:
            nonlocal incumbent, incumbent_objective, incumbent_nominal_objective, evaluations
            if should_stop():
                return False
            evaluations += 1
            increment(operation_counts, kind)
            objective = self._plan_objective(proposal, start_poses, residual)
            nominal_objective = self._plan_objective(
                proposal, start_poses, residual, coverage_mode="nominal"
            )
            if acceptable(objective, incumbent_objective, nominal_objective):
                incumbent = proposal
                incumbent_objective = objective
                incumbent_nominal_objective = nominal_objective
                accepted_moves.append(kind)
                return True
            increment(rejection_counts, rejection_reason(
                objective, incumbent_objective, nominal_objective
            ))
            return False

        # Deterministic first-improvement neighborhoods.  A restart after each
        # accepted move makes every committed plan auditable and feasible.
        changed = True
        while changed and not should_stop():
            changed = False
            for device_id in self.device_ids:
                route = incumbent[device_id]
                for index in range(len(route)):
                    if should_stop():
                        break
                    proposal = self._copy_routes(incumbent)
                    proposal[device_id].pop(index)
                    if consider(proposal, "redundant_delete"):
                        changed = True
                        break
                if changed or should_stop():
                    break
            if changed:
                continue

            for device_id in self.device_ids:
                route = incumbent[device_id]
                for source in range(len(route)):
                    candidate_id = route[source]
                    for target_position in range(len(route)):
                        if target_position == source:
                            continue
                        if should_stop():
                            break
                        proposal = self._copy_routes(incumbent)
                        proposal[device_id].pop(source)
                        proposal[device_id].insert(target_position, candidate_id)
                        if consider(proposal, "intra_route_relocate"):
                            changed = True
                            break
                    if changed:
                        break
                if changed or should_stop():
                    break
            if changed:
                continue

            for source_device in self.device_ids:
                route = incumbent[source_device]
                for source_index, removed_id in enumerate(route):
                    if should_stop():
                        break
                    other_coverage: set[int] = set()
                    for device_id, other_route in incumbent.items():
                        for index, candidate_id in enumerate(other_route):
                            if device_id == source_device and index == source_index:
                                continue
                            other_coverage.update(self.candidates[candidate_id].coverage & residual)
                    required = (self.candidates[removed_id].coverage & residual) - other_coverage
                    for target_device in self.device_ids:
                        if target_device in self.faults:
                            continue
                        replacements = [candidate for candidate in self.candidates_by_device[target_device]
                                        if required <= candidate.coverage and
                                        candidate.candidate_id not in incumbent[target_device]]
                        replacements.sort(key=lambda candidate: (
                            len(candidate.coverage - required), candidate.yaw_deg,
                            candidate.pitch_deg, candidate.candidate_id))
                        for candidate in replacements[:16]:
                            for position in range(len(incumbent[target_device]) + 1):
                                if should_stop():
                                    break
                                proposal = self._copy_routes(incumbent)
                                proposal[source_device].pop(source_index)
                                proposal[target_device].insert(position, candidate.candidate_id)
                                if consider(proposal, "view_substitute_or_cross_device_relocate"):
                                    changed = True
                                    break
                            if changed:
                                break
                        if changed:
                            break
                    if changed:
                        break
                if changed or should_stop():
                    break

        self.last_plan_audit = {
            "guarantee_reference": "mag",
            "baseline_objective": baseline_objective.as_dict(),
            "final_objective": incumbent_objective.as_dict(),
            "tail_cap_s": tail_cap,
            "opportunistic_baseline_objective": baseline_nominal_objective.as_dict(),
            "opportunistic_final_objective": incumbent_nominal_objective.as_dict(),
            "opportunistic_tail_cap_s": nominal_tail_cap,
            "opportunistic_tail_nonregression_verified": (
                incumbent_nominal_objective.t100_s <= nominal_tail_cap + self.objective_epsilon_s
            ),
            "uac_non_regression_verified": (
                incumbent_objective.uac_h_s <= baseline_objective.uac_h_s + self.objective_epsilon_s),
            "t100_non_regression_verified": (
                incumbent_objective.t100_s <= baseline_objective.t100_s + self.objective_epsilon_s),
            "accepted_moves": accepted_moves,
            "operation_counts": operation_counts,
            "rejection_counts": rejection_counts,
            "evaluations": evaluations,
            "evaluation_limit": evaluation_limit,
            "evaluation_limit_respected": evaluations <= evaluation_limit,
            "search_stop_reason": search_stop_reason,
            "initial_pose_box_enabled": self._planning_with_initial_pose_box,
            "initial_pose_box_deg": {
                "yaw": self.initial_pose_yaw_bound_deg,
                "pitch": self.initial_pose_pitch_bound_deg,
            },
        }
        return incumbent

    def _health_penalty(self, context: PolicyContext, device_id: str) -> float:
        state = self._fresh_state(context, device_id)
        if state is None:
            return 3.0
        age_term = min(1.0, max(0.0, context.t - state.t) / self.telemetry_stale_s)
        rate_scale = max(self.max_rate_dps, 1.0)
        rate_term = min(1.0, math.hypot(state.yaw_rate_dps, state.pitch_rate_dps) / rate_scale)
        frame = getattr(context, "frames", {}).get(device_id) if isinstance(getattr(context, "frames", {}), Mapping) else None
        if frame is None:
            frame_term = 0.0
        else:
            frame_term = min(1.0, max(0.0, context.t - float(getattr(frame, "capture_t", context.t))) /
                             self.max_frame_age_s)
            if not bool(getattr(frame, "usable", False)):
                frame_term = 1.0
        return (self.health_weights[0] * age_term + self.health_weights[1] * rate_term +
                self.health_weights[2] * frame_term)

    def _reachable_candidates(self, device_id: str, residual: set[int]) -> Iterable[Candidate]:
        return (candidate for candidate in self.candidates_by_device[device_id]
                if candidate.coverage & residual)

    def _plan_fpr(self, residual: set[int]) -> dict[str, list[str]]:
        """Capability-constrained balanced fixed partition plus snake scan.

        Residual cells retain the frozen z/y/x serpentine order and are split
        into equal contiguous index intervals before execution.  Ownership is
        therefore fixed by task geometry and device identity, not by a motion
        model, health signal, or observed completion time.  If the preferred
        device cannot see a cell, the first cyclically following capable device
        owns it.  This keeps the non-adaptive baseline auditable while avoiding
        the degenerate two-camera allocation produced by nearest-station rules
        when every ROI point lies on the same side of a compact four-camera rig.
        """
        routes = {device_id: [] for device_id in self.device_ids}
        partition: dict[int, str] = {}
        ordered_cells = [cell for cell in self.cells if cell.index in residual]
        for rank, cell in enumerate(ordered_cells):
            possible = [device_id for device_id in self.device_ids
                        if any(cell.index in candidate.coverage for candidate in self.candidates_by_device[device_id])]
            if not possible:
                continue
            preferred_slot = min(
                len(self.device_ids) - 1,
                rank * len(self.device_ids) // max(1, len(ordered_cells)),
            )
            cyclic = self.device_ids[preferred_slot:] + self.device_ids[:preferred_slot]
            partition[cell.index] = next(device_id for device_id in cyclic if device_id in possible)
        for device_id in self.device_ids:
            assigned = {index for index, owner in partition.items() if owner == device_id}
            assigned_covered: set[int] = set()
            for cell in self.cells:
                if cell.index not in assigned or cell.index in assigned_covered:
                    continue
                choices = [candidate for candidate in self.candidates_by_device[device_id]
                           if cell.index in candidate.coverage]
                chosen = min(choices, key=lambda candidate: (-len(candidate.coverage & assigned),
                                                             candidate.yaw_deg, candidate.pitch_deg,
                                                             candidate.candidate_id))
                routes[device_id].append(chosen.candidate_id)
                assigned_covered.update(chosen.coverage & assigned)
        return routes

    def _plan_mag(self, context: PolicyContext, residual: set[int], start_poses: Mapping[str, tuple[float, float]],
                  deadline: float) -> dict[str, list[str]]:
        routes = {device_id: [] for device_id in self.device_ids}
        tails = dict(start_poses)
        uncovered = set(residual)
        evaluations = 0
        while uncovered and evaluations < self.planner_max_evaluations and time.perf_counter() < deadline:
            best: tuple[Any, ...] | None = None
            for device_id in self.device_ids:
                if device_id in self.faults:
                    continue
                for candidate in self._reachable_candidates(device_id, uncovered):
                    evaluations += 1
                    cost, _ = self._planning_edge(
                        device_id, tails[device_id],
                        (candidate.yaw_deg, candidate.pitch_deg), False)
                    gain = len(candidate.coverage & uncovered)
                    score = gain / (cost + self.score_epsilon)
                    key = (-score, cost, device_id, candidate.candidate_id)
                    if best is None or key < best[0]:
                        best = (key, device_id, candidate)
                    if evaluations >= self.planner_max_evaluations or time.perf_counter() >= deadline:
                        break
                if evaluations >= self.planner_max_evaluations or time.perf_counter() >= deadline:
                    break
            if best is None:
                break
            _, device_id, candidate = best
            routes[device_id].append(candidate.candidate_id)
            tails[device_id] = (candidate.yaw_deg, candidate.pitch_deg)
            uncovered.difference_update(candidate.coverage)
        return routes

    def _insertion_delta(self, device_id: str, route: Sequence[str], position: int,
                         candidate: Candidate, start_pose: tuple[float, float], measured: bool) -> tuple[float, float]:
        previous = start_pose if position == 0 else (
            self.candidates[route[position - 1]].yaw_deg,
            self.candidates[route[position - 1]].pitch_deg,
        )
        following = None if position == len(route) else (
            self.candidates[route[position]].yaw_deg,
            self.candidates[route[position]].pitch_deg,
        )
        first_cost, first_uncertainty = self._planning_edge(
            device_id, previous, (candidate.yaw_deg, candidate.pitch_deg), measured)
        second_cost = second_uncertainty = replaced_cost = replaced_uncertainty = 0.0
        if following is not None:
            second_cost, second_uncertainty = self._planning_edge(
                device_id, (candidate.yaw_deg, candidate.pitch_deg), following, measured)
            replaced_cost, replaced_uncertainty = self._planning_edge(
                device_id, previous, following, measured
            )
        # Floating-point/model non-metric effects must never create a negative
        # denominator or reward.  This equals exact before/after additive route
        # cost at the insertion point, then is explicitly truncated at zero.
        return (max(0.0, first_cost + second_cost - replaced_cost),
                max(0.0, first_uncertainty + second_uncertainty - replaced_uncertainty))

    def _prune_redundant(self, routes: dict[str, list[str]], target: set[int]) -> None:
        for device_id in reversed(self.device_ids):
            index = len(routes[device_id]) - 1
            while index >= 0:
                candidate_id = routes[device_id][index]
                union: set[int] = set()
                for other_device, route in routes.items():
                    for other_index, other_id in enumerate(route):
                        if other_device == device_id and other_index == index:
                            continue
                        union.update(self.candidates[other_id].coverage)
                if target <= union:
                    routes[device_id].pop(index)
                index -= 1

    def _two_opt(self, routes: dict[str, list[str]], start_poses: Mapping[str, tuple[float, float]],
                 measured: bool, deadline: float) -> None:
        for _ in range(self.local_search_passes):
            changed = False
            for device_id in self.device_ids:
                route = routes[device_id]
                if len(route) < 3:
                    continue
                baseline = self._route_cost(device_id, route, start_poses[device_id], measured)[0]
                for left in range(len(route) - 1):
                    for right in range(left + 2, len(route) + 1):
                        if time.perf_counter() >= deadline:
                            return
                        proposal = route[:left] + list(reversed(route[left:right])) + route[right:]
                        value = self._route_cost(device_id, proposal, start_poses[device_id], measured)[0]
                        if value + 1e-12 < baseline:
                            routes[device_id] = route = proposal
                            baseline = value
                            changed = True
            if not changed:
                break

    def _plan_insertion(self, context: PolicyContext, residual: set[int],
                        start_poses: Mapping[str, tuple[float, float]], deadline: float,
                        measured: bool) -> dict[str, list[str]]:
        routes = {device_id: [] for device_id in self.device_ids}
        loads = {device_id: 0.0 for device_id in self.device_ids}
        uncovered = set(residual)
        selected: set[str] = set()
        evaluations = 0
        while uncovered and evaluations < self.planner_max_evaluations and time.perf_counter() < deadline:
            current_makespan = max(loads.values(), default=0.0)
            best: tuple[Any, ...] | None = None
            for device_id in self.device_ids:
                if device_id in self.faults:
                    continue
                route = routes[device_id]
                other_max = max((load for key, load in loads.items() if key != device_id), default=0.0)
                health = self._health_penalty(context, device_id)
                for candidate in self._reachable_candidates(device_id, uncovered):
                    if candidate.candidate_id in selected:
                        continue
                    gain = len(candidate.coverage & uncovered)
                    for position in range(len(route) + 1):
                        delta_l, delta_uncertainty = self._insertion_delta(
                            device_id, route, position, candidate, start_poses[device_id], measured)
                        new_load = loads[device_id] + delta_l
                        delta_team = max(0.0, max(new_load, other_max) - current_makespan)
                        uncertainty = delta_uncertainty + health * delta_l
                        denominator = (delta_team + self.lambda_b * delta_l +
                                       self.lambda_u * uncertainty + self.score_epsilon)
                        score = gain / denominator
                        key = (-score, delta_team, delta_l, uncertainty,
                               device_id, candidate.candidate_id, position)
                        if best is None or key < best[0]:
                            best = (key, device_id, candidate, position, delta_l)
                        evaluations += 1
                        if evaluations >= self.planner_max_evaluations or time.perf_counter() >= deadline:
                            break
                    if evaluations >= self.planner_max_evaluations or time.perf_counter() >= deadline:
                        break
                if evaluations >= self.planner_max_evaluations or time.perf_counter() >= deadline:
                    break
            if best is None:
                break
            _, device_id, candidate, position, delta_l = best
            routes[device_id].insert(position, candidate.candidate_id)
            loads[device_id] += delta_l
            selected.add(candidate.candidate_id)
            uncovered.difference_update(candidate.coverage)

        planned: set[int] = set()
        for route in routes.values():
            for candidate_id in route:
                planned.update(self.candidates[candidate_id].coverage & residual)
        if planned:
            self._prune_redundant(routes, planned)
            self._two_opt(routes, start_poses, measured, deadline)
        return routes

    def _make_plan(self, context: PolicyContext, residual: set[int]) -> dict[str, list[str]]:
        begin = time.perf_counter()
        budget_ms = (self.initial_planner_budget_ms if self.is_v2 and self.total_plans == 0
                     else self.repair_planner_budget_ms if self.is_v2
                     else self.planner_budget_ms)
        guard_ms = self.planner_deadline_guard_ms if self.is_v2 else 0.0
        effective_budget_ms = max(0.0, budget_ms - guard_ms)
        deadline = begin + effective_budget_ms / 1000.0
        initial_pose_box = bool(self.initial_pose_box_enabled and self.total_plans == 0)
        start_poses = self._start_poses(context, configured_home=initial_pose_box)
        self._planning_with_initial_pose_box = initial_pose_box
        self.last_plan_audit = {}
        try:
            if self.method == "fpr":
                routes = self._plan_fpr(residual)
            elif self.method == "mag":
                routes = (self._plan_mag_complete(context, residual, start_poses)
                          if self.complete_mag_reference else
                          self._plan_mag(context, residual, start_poses, deadline))
            elif self.is_v2:
                routes = self._plan_v2(context, residual, start_poses, deadline)
            else:
                routes = self._plan_insertion(context, residual, start_poses, deadline,
                                              self.method in MEASURED_METHODS)
        finally:
            self._planning_with_initial_pose_box = False
        self.last_planner_ms = (time.perf_counter() - begin) * 1000.0
        self.last_plan_audit.update({
            "configured_budget_ms": budget_ms,
            "deadline_guard_ms": guard_ms,
            "effective_search_budget_ms": effective_budget_ms,
            "observed_planner_ms": self.last_planner_ms,
            "budget_contract_satisfied": self.last_planner_ms <= budget_ms,
            "initial_edge_cache_prewarmed": bool(
                self.is_v2 and self.static_precomputed_edge_count > 0
            ),
            "static_precomputed_edge_count": self.static_precomputed_edge_count,
        })
        self.total_plans += 1
        return routes

    # ------------------------------------------------------------------
    # Runtime state machine and repair
    # ------------------------------------------------------------------
    def _event(self, kind: str, context: PolicyContext, **fields: Any) -> dict[str, Any]:
        return {
            "type": kind,
            "mission_elapsed_s": self._elapsed(context.t),
            **{key: _json_scalar(value) if not isinstance(value, (list, dict)) else value
               for key, value in fields.items()},
        }

    def _start_fault(self, context: PolicyContext, device_id: str, reason: str,
                     events: list[dict[str, Any]], details: str = "") -> None:
        current = self.faults.get(device_id)
        if current is not None and current.reason == reason:
            return
        task = self.active.get(device_id)
        task_id = task.task_id if task is not None else ""
        attempt_id = task.attempt_id if task is not None else ""
        candidate_id = task.candidate_id if task is not None else ""
        self.faults[device_id] = FaultState(reason, float(context.t), details,
                                            task_id, attempt_id, candidate_id)
        events.append(self._event("fault_started", context, device_id=device_id,
                                  reason=reason, details=details,
                                  task_id=task_id, attempt_id=attempt_id))
        if (reason == "logical_pause" and self.logical_pause is not None and
                self.logical_pause.selected_device_id == device_id):
            events.append(self._event(
                "injection_started", context,
                injection_kind="logical_availability_pause",
                device_id=device_id,
                configured_duration_s=self.logical_pause.duration_s,
                trigger_mode=self.logical_pause.mode,
                trigger_coverage_fraction=self.logical_pause.trigger_coverage_fraction,
                selected_unique_residual_count=self.logical_pause.selected_unique_residual_count,
                event_clock_only=True,
            ))
        if self.method in REPAIR_METHODS:
            if self.active.get(device_id) is not None:
                self.active[device_id] = None
            self.pending_replan = True
            if self.pending_replan_since_t is None:
                self.pending_replan_since_t = float(context.t)
                self.pending_replan_reason = f"fault:{reason}:{device_id}"

    def _recover_fault(self, context: PolicyContext, device_id: str,
                       events: list[dict[str, Any]]) -> None:
        fault = self.faults.pop(device_id, None)
        if fault is None:
            return
        events.append(self._event("fault_recovered", context, device_id=device_id,
                                  reason=fault.reason,
                                  task_id=fault.task_id,
                                  attempt_id=fault.attempt_id,
                                  fault_duration_s=max(0.0, context.t - fault.since_t)))
        if (fault.reason == "logical_pause" and self.logical_pause is not None and
                self.logical_pause.selected_device_id == device_id):
            events.append(self._event(
                "injection_ended", context,
                injection_kind="logical_availability_pause",
                device_id=device_id,
                actual_duration_s=max(0.0, context.t - fault.since_t),
                event_clock_only=True,
            ))
        active = self.active.get(device_id)
        if active is not None:
            state = self._fresh_state(context, device_id)
            candidate = self.candidates[active.candidate_id]
            _, active.attempt_id = self._next_attempt(active.candidate_id, active.task_id)
            active.stage = "MOVE"
            active.assigned_t = float(context.t)
            if state is not None:
                active.from_yaw_deg = float(state.yaw_deg)
                active.from_pitch_deg = float(state.pitch_deg)
                active.predicted_s = self._predicted_for_assignment(state, candidate)
            active.command_started_t = None
            active.settle_started_t = None
            active.dwell_started_t = None
            active.frame_confirm_started_t = None
            active.physical_frame_gate_t = None
            active.confirmation_frame_id = None
            active.confirmation_frame_capture_t = None
            active.confirmation_frame_received_t = None
            active.last_action_issued_t = None
            active.ack_seen = False
            active.frame_ids.clear()
            active.prediction_bound_exceeded_emitted = False
        if self.method in REPAIR_METHODS:
            self.pending_replan = True
            if self.pending_replan_since_t is None:
                self.pending_replan_since_t = float(context.t)
                self.pending_replan_reason = f"recovery:{fault.reason}:{device_id}"

    def _refresh_faults(self, context: PolicyContext, events: list[dict[str, Any]]) -> None:
        for device_id in self.device_ids:
            base_reason = self._base_unavailable_reason(context, device_id)
            current = self.faults.get(device_id)
            if base_reason:
                self._start_fault(context, device_id, base_reason, events)
                continue
            if current is None:
                continue
            recover = False
            if current.reason in {"missing_telemetry", "disconnected", "stale_telemetry",
                                  "zoom_unknown", "zoom_unstable", "fixed_zoom_mismatch",
                                  "logical_pause"}:
                recover = True
            elif current.reason == "frame_timeout":
                recover = self._fresh_usable_frame(context, device_id) is not None
            elif context.t - current.since_t >= self.fault_recovery_hold_s:
                recover = self._fresh_state(context, device_id) is not None
            if recover:
                self._recover_fault(context, device_id, events)

    def _next_attempt(self, candidate_id: str, task_id_override: str | None = None) -> tuple[str, str]:
        count = self.attempt_counts.get(candidate_id, 0) + 1
        self.attempt_counts[candidate_id] = count
        task_id = task_id_override or f"task:{candidate_id}"
        return task_id, f"{task_id}/attempt:{count:03d}"

    def _predicted_for_assignment(self, state: Telemetry, candidate: Candidate) -> float:
        measured = self.method in MEASURED_METHODS
        return self._edge(candidate.device_id, (state.yaw_deg, state.pitch_deg),
                          (candidate.yaw_deg, candidate.pitch_deg), measured)[0]

    def _assign_available(self, context: PolicyContext,
                          events: list[dict[str, Any]]) -> None:
        for device_id in self.device_ids:
            if self.active[device_id] is not None or device_id in self.faults:
                continue
            state = self._fresh_state(context, device_id)
            if state is None:
                continue
            route = self.routes[device_id]
            while route:
                candidate_id = route.pop(0)
                candidate = self.candidates[candidate_id]
                meta = self.commissioning_meta.get(candidate_id)
                task_kind = "coverage"
                transition_id = ""
                if meta is not None:
                    task_kind, row = meta
                    transition_id = row.transition_id if task_kind == "sample" else ""
                    if task_kind == "sample":
                        mismatch = max(
                            _angle_error(state.yaw_deg, row.planned_from_yaw_deg),
                            abs(state.pitch_deg - row.planned_from_pitch_deg),
                        )
                        if mismatch > self.commissioning_start_tolerance_deg:
                            route.insert(0, candidate_id)
                            self._start_fault(
                                context, device_id, "commissioning_start_pose_mismatch", events,
                                f"transition_id={row.transition_id},error_deg={mismatch:.6f}",
                            )
                            break
                elif not (candidate.coverage - self.confirmed):
                    continue
                if not self._task_reachable_now(state, candidate):
                    continue
                task_override = f"task:{transition_id}" if transition_id else None
                task_id, attempt_id = self._next_attempt(candidate_id, task_override)
                self.active[device_id] = ActiveTask(
                    candidate_id=candidate_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    stage="MOVE",
                    assigned_t=float(context.t),
                    from_yaw_deg=float(state.yaw_deg),
                    from_pitch_deg=float(state.pitch_deg),
                    predicted_s=self._predicted_for_assignment(state, candidate),
                    task_kind=task_kind,
                    transition_id=transition_id,
                )
                break

    def _collect_frame(self, context: PolicyContext, task: ActiveTask, device_id: str) -> bool:
        if task.dwell_started_t is None:
            return False
        frame = self._fresh_usable_frame(context, device_id)
        if frame is None:
            return False
        capture_t = float(getattr(frame, "capture_t", -math.inf))
        if capture_t + 1e-9 < task.dwell_started_t:
            return False
        if self.use_measured_pose_coverage:
            state = self._fresh_state(context, device_id)
            if state is None or abs(float(state.t) - capture_t) > self.frame_pose_max_gap_s:
                return False
        frame_id = int(getattr(frame, "frame_id", -1))
        if frame_id >= 0:
            task.frame_ids.add(frame_id)
            task.confirmation_frame_id = frame_id
            task.confirmation_frame_capture_t = capture_t
            task.confirmation_frame_received_t = float(getattr(frame, "received_t", capture_t))
            return True
        return False

    def _complete_task(self, context: PolicyContext, device_id: str, task: ActiveTask,
                       candidate: Candidate, state: Telemetry,
                       events: list[dict[str, Any]]) -> None:
        # Required stale-task revalidation immediately before completion.  A
        # commissioning transition is not a coverage task, so it is keyed by
        # the preregistered transition_id instead of a coverage bitset.
        newly_confirmed: list[int] = []
        if task.task_kind == "coverage":
            evidence_coverage = (self._coverage_at_pose(
                device_id, float(state.yaw_deg), float(state.pitch_deg),
                self.confirmation_pixel_margin,
            ) if self.use_measured_pose_coverage else candidate.coverage)
            newly_confirmed = sorted((evidence_coverage & self.reachable) - self.confirmed)
            if not newly_confirmed:
                task.stage = "COMPLETE"
                return
            self.confirmed.update(newly_confirmed)
        elif task.task_kind == "preposition":
            task.stage = "COMPLETE"
            _, row = self.commissioning_meta[task.candidate_id]
            events.append(self._event(
                "commissioning_preposition_confirmed", context,
                device_id=device_id,
                next_transition_id=row.transition_id,
                yaw_deg=float(state.yaw_deg),
                pitch_deg=float(state.pitch_deg),
                recorded_sample=False,
            ))
            return
        else:
            self.commissioning_completed_ids.add(task.transition_id)
        task.stage = "COMPLETE"
        elapsed = max(0.0, context.t - (task.command_started_t if task.command_started_t is not None else task.assigned_t))
        model_row = self.execution_models.get(device_id) if self.method in MEASURED_METHODS else None
        if model_row is not None:
            padding = self._model_padding(
                device_id, (task.from_yaw_deg, task.from_pitch_deg),
                (candidate.yaw_deg, candidate.pitch_deg),
            )
            prediction_kind = "conformal_upper"
            point_prediction = max(0.0, task.predicted_s - padding)
        elif self.method in MEASURED_METHODS:
            padding = 0.0
            prediction_kind = "nominal_fallback_upper"
            point_prediction = task.predicted_s
        else:
            padding = 0.0
            prediction_kind = "nominal_end_to_end"
            point_prediction = task.predicted_s
        if task.task_kind == "coverage":
            events.append(self._event(
                "coverage_confirmed", context,
                device_id=device_id,
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                cell_ids=[self.cells[index].cell_id for index in newly_confirmed],
                confirmed_count=len(self.confirmed),
                coverage_fraction=len(self.confirmed) / len(self.cells),
                evidence_mode=("measured_pose_fresh_frame_projection" if self.use_measured_pose_coverage
                               else "planned_candidate_after_execution_gate"),
                planned_candidate_id=candidate.candidate_id,
                planned_yaw_deg=candidate.yaw_deg,
                planned_pitch_deg=candidate.pitch_deg,
                measured_yaw_deg=float(state.yaw_deg),
                measured_pitch_deg=float(state.pitch_deg),
                pose_sample_t=float(state.t),
                frame_id=task.confirmation_frame_id,
                frame_capture_t=task.confirmation_frame_capture_t,
                frame_received_t=task.confirmation_frame_received_t,
                frame_pose_gap_s=(abs(float(state.t) - task.confirmation_frame_capture_t)
                                  if task.confirmation_frame_capture_t is not None else None),
            ))
            reassigned = [index for index in newly_confirmed if index in self.pending_reassigned_cells]
            if reassigned:
                origins = sorted({str(self.pending_reassigned_cells[index]["original_device_id"])
                                  for index in reassigned})
                trigger_t = min(float(self.pending_reassigned_cells[index]["trigger_t"])
                                for index in reassigned)
                events.append(self._event(
                    "reassigned_coverage_confirmed", context,
                    device_id=device_id,
                    original_device_ids=origins,
                    cell_ids=[self.cells[index].cell_id for index in reassigned],
                    response_latency_s=max(0.0, context.t - trigger_t),
                    before_original_device_recovery=any(origin in self.faults for origin in origins),
                    frame_id=task.confirmation_frame_id,
                    frame_capture_t=task.confirmation_frame_capture_t,
                ))
                for index in reassigned:
                    self.pending_reassigned_cells.pop(index, None)
        row = (self.commissioning_meta[task.candidate_id][1]
               if task.task_kind == "sample" else None)
        physical_gate_t = (task.physical_frame_gate_t
                           if task.physical_frame_gate_t is not None else float(context.t))
        command_t = task.command_started_t if task.command_started_t is not None else task.assigned_t
        settle_start_t = task.settle_started_t if task.settle_started_t is not None else physical_gate_t
        settle_complete_t = task.dwell_started_t if task.dwell_started_t is not None else physical_gate_t
        dwell_complete_t = (task.frame_confirm_started_t
                            if task.frame_confirm_started_t is not None else physical_gate_t)
        events.append(self._event(
            "transition_completed", context,
            transition_id=task.transition_id or task.attempt_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            device_id=device_id,
            from_yaw_deg=task.from_yaw_deg,
            from_pitch_deg=task.from_pitch_deg,
            to_yaw_deg=float(state.yaw_deg),
            to_pitch_deg=float(state.pitch_deg),
            from_pose_deg=[task.from_yaw_deg, task.from_pitch_deg],
            to_pose_deg=[float(state.yaw_deg), float(state.pitch_deg)],
            planned_from_yaw_deg=(row.planned_from_yaw_deg if row else task.from_yaw_deg),
            planned_from_pitch_deg=(row.planned_from_pitch_deg if row else task.from_pitch_deg),
            planned_to_yaw_deg=(row.planned_to_yaw_deg if row else candidate.yaw_deg),
            planned_to_pitch_deg=(row.planned_to_pitch_deg if row else candidate.pitch_deg),
            commissioning_id=(row.commissioning_id if row else ""),
            split=(row.split if row else ""),
            commissioning_sample=task.task_kind == "sample",
            command_issued_t=command_t,
            frame_confirm_t=float(context.t),
            physical_frame_gate_t=physical_gate_t,
            t_in=command_t,
            t_set=settle_complete_t,
            t_dw=dwell_complete_t,
            t_frm=physical_gate_t,
            t_motion_stable_start=settle_start_t,
            t_complete=float(context.t),
            motion_s=max(0.0, settle_start_t - command_t),
            settle_s=max(0.0, settle_complete_t - settle_start_t),
            dwell_s=max(0.0, dwell_complete_t - settle_complete_t),
            frame_wait_s=max(0.0, physical_gate_t - dwell_complete_t),
            injected_confirmation_delay_s=max(0.0, context.t - physical_gate_t),
            actual_s=elapsed,
            predicted_s=task.predicted_s,
            prediction_kind=prediction_kind,
            point_prediction_s=point_prediction,
            conformal_padding_s=padding,
            time_definition="command_issued_to_frame_confirm",
            frame_count=len(task.frame_ids),
            confirmation_frame_id=task.confirmation_frame_id,
            confirmation_frame_capture_t=task.confirmation_frame_capture_t,
            confirmation_frame_received_t=task.confirmation_frame_received_t,
            measured_pose_yaw_deg=float(state.yaw_deg),
            measured_pose_pitch_deg=float(state.pitch_deg),
            pose_sample_t=float(state.t),
        ))

    def _advance_task(self, context: PolicyContext, device_id: str,
                      events: list[dict[str, Any]]) -> Action | None:
        task = self.active[device_id]
        if task is None:
            return None
        candidate = self.candidates[task.candidate_id]

        # Required stale-task revalidation before issuing another command.
        if task.task_kind == "coverage" and not (candidate.coverage - self.confirmed):
            task.stage = "COMPLETE"
            return self._hold_action(context, device_id, f"{self.method}:stale_task_stop")
        state = self._fresh_state(context, device_id)
        if state is None or device_id in self.faults:
            return None
        if not self._task_reachable_now(state, candidate):
            self._start_fault(context, device_id, "task_not_reachable", events,
                              f"candidate={candidate.candidate_id}")
            return None

        related = self._related_result(context, task, device_id)
        if related is not None:
            signature = self._result_signature(related)
            if signature not in self.seen_result_signatures:
                self.seen_result_signatures.add(signature)
                if self._result_failed(related):
                    self._start_fault(context, device_id, "command_failure", events,
                                      str(getattr(related, "error", "")) or str(getattr(related, "status", "")))
                    return None
                if bool(getattr(related, "ack", False)):
                    task.ack_seen = True

        if task.command_started_t is not None:
            elapsed = context.t - task.command_started_t
            injected_delay = self.confirmation_delay_s.get(device_id, 0.0)
            if elapsed > self.transition_timeout_s + injected_delay:
                self._start_fault(context, device_id, "transition_timeout", events,
                                  f"elapsed={elapsed:.3f}s")
                return None
            if (self.method in MEASURED_METHODS and
                    elapsed > task.predicted_s + self.prediction_overrun_grace_s + injected_delay and
                    not task.prediction_bound_exceeded_emitted):
                # A calibrated one-sided prediction bound is a planning bound,
                # not a device-health detector.  Occasional exceedances are
                # retained as auditable evidence but must not manufacture a
                # fault or trigger task migration.  Physical transition/frame
                # timeouts and telemetry/command failures remain fault paths.
                task.prediction_bound_exceeded_emitted = True
                events.append(self._event(
                    "prediction_bound_exceeded", context,
                    device_id=device_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    candidate_id=task.candidate_id,
                    elapsed_s=float(elapsed),
                    predicted_upper_s=float(task.predicted_s),
                    grace_s=float(self.prediction_overrun_grace_s),
                    exceedance_s=float(
                        elapsed - task.predicted_s - self.prediction_overrun_grace_s - injected_delay
                    ),
                    fault=False,
                    replan_requested=False,
                ))

        stable = self._pose_stable(state, candidate)
        if task.stage == "MOVE":
            if stable:
                task.stage = "SETTLE"
                task.settle_started_t = float(context.t)
        elif task.stage == "SETTLE":
            if not stable:
                task.stage = "MOVE"
                task.settle_started_t = None
            elif context.t - float(task.settle_started_t) >= self.settle_hold_s:
                task.stage = "DWELL"
                task.dwell_started_t = float(context.t)
        elif task.stage in {"DWELL", "FRAME_CONFIRM"}:
            if not stable:
                if task.physical_frame_gate_t is not None:
                    events.append(self._event(
                        "injection_aborted", context,
                        injection_kind="confirmation_delay",
                        device_id=device_id,
                        attempt_id=task.attempt_id,
                        reason="pose_gate_lost",
                    ))
                task.stage = "MOVE"
                task.settle_started_t = None
                task.dwell_started_t = None
                task.frame_confirm_started_t = None
                task.physical_frame_gate_t = None
                task.confirmation_frame_id = None
                task.confirmation_frame_capture_t = None
                task.confirmation_frame_received_t = None
                task.frame_ids.clear()
            else:
                synchronized_frame_now = self._collect_frame(context, task, device_id)
                dwell_done = context.t - float(task.dwell_started_t) >= self.min_dwell_s
                if task.stage == "DWELL" and dwell_done:
                    task.stage = "FRAME_CONFIRM"
                    task.frame_confirm_started_t = float(context.t)
                if task.stage == "FRAME_CONFIRM":
                    physical_gate = (
                        (not self.require_ack or task.ack_seen)
                        and (
                            not self.require_frame
                            or (
                                len(task.frame_ids) >= self.distinct_frames
                                and synchronized_frame_now
                            )
                        )
                    )
                    if physical_gate and task.physical_frame_gate_t is None:
                        task.physical_frame_gate_t = float(context.t)
                        delay = self.confirmation_delay_s.get(device_id, 0.0)
                        if delay > 0:
                            events.append(self._event(
                                "injection_started", context,
                                injection_kind="confirmation_delay",
                                device_id=device_id,
                                attempt_id=task.attempt_id,
                                configured_delay_s=delay,
                                physical_frame_gate_t=task.physical_frame_gate_t,
                                event_clock_only=True,
                            ))
                    if task.physical_frame_gate_t is not None:
                        delay = self.confirmation_delay_s.get(device_id, 0.0)
                        if context.t + 1e-9 >= task.physical_frame_gate_t + delay:
                            if delay > 0:
                                events.append(self._event(
                                    "injection_ended", context,
                                    injection_kind="confirmation_delay",
                                    device_id=device_id,
                                    attempt_id=task.attempt_id,
                                    configured_delay_s=delay,
                                    actual_delay_s=max(0.0, context.t - task.physical_frame_gate_t),
                                    event_clock_only=True,
                                ))
                            self._complete_task(context, device_id, task, candidate, state, events)
                            return self._hold_action(context, device_id, f"{self.method}:complete_stop")
                    if (self.require_frame and task.physical_frame_gate_t is None and
                            task.frame_confirm_started_t is not None and
                            context.t - task.frame_confirm_started_t > self.frame_timeout_s):
                        self._start_fault(context, device_id, "frame_timeout", events,
                                          f"usable_distinct={len(task.frame_ids)}")
                        return None
        return self._target_action(context, task, candidate)

    def _reserved_coverage(self) -> set[int]:
        result: set[int] = set()
        for task in self.active.values():
            if task is not None and task.stage != "COMPLETE":
                result.update(self.candidates[task.candidate_id].coverage)
        return result

    def _repair_queue_start_state(
        self, context: PolicyContext
    ) -> tuple[dict[str, tuple[float, float]], dict[str, float]]:
        """Return conservative queue start poses/times after active work.

        Online repair must not pretend that a healthy device can start its new
        queue while its already-issued task is still executing.  The remaining
        calibrated upper is used while the original prediction is still live;
        after an overrun, a fresh upper is recomputed from the measured pose.
        """
        poses = self._start_poses(context)
        offsets = {device_id: 0.0 for device_id in self.device_ids}
        for device_id, task in self.active.items():
            if task is None or task.stage == "COMPLETE" or device_id in self.faults:
                continue
            candidate = self.candidates[task.candidate_id]
            target = (candidate.yaw_deg, candidate.pitch_deg)
            if task.command_started_t is not None:
                elapsed = max(0.0, context.t - task.command_started_t)
                remaining = task.predicted_s - elapsed
            else:
                remaining = -1.0
            if remaining < 0.0:
                state = self._fresh_state(context, device_id)
                measured = ((state.yaw_deg, state.pitch_deg) if state is not None
                            else poses[device_id])
                remaining, _ = self._edge(device_id, measured, target, True)
            poses[device_id] = target
            offsets[device_id] = max(0.0, float(remaining))
        return poses, offsets

    def _repair_witness(
        self,
        routes: Mapping[str, Sequence[str]],
        start_poses: Mapping[str, tuple[float, float]],
        start_offsets: Mapping[str, float],
        original_owned: Mapping[str, set[int]],
    ) -> dict[str, Any]:
        """Find the earliest robustly predicted reassigned-cell confirmation."""
        witnesses: list[dict[str, Any]] = []
        for faulted_id, cells in sorted(original_owned.items()):
            recovery_window_s = max(0.0, float(start_offsets.get(faulted_id, 0.0)))
            best: tuple[float, str, str, int] | None = None
            for device_id in self.device_ids:
                if device_id == faulted_id or device_id in self.faults:
                    continue
                elapsed = max(0.0, float(start_offsets.get(device_id, 0.0)))
                previous = start_poses[device_id]
                for candidate_id in routes.get(device_id, ()):
                    candidate = self.candidates[candidate_id]
                    upper, _ = self._planning_edge(
                        device_id, previous, (candidate.yaw_deg, candidate.pitch_deg), True
                    )
                    elapsed += upper
                    covered = candidate.coverage & cells
                    if covered:
                        item = (elapsed, device_id, candidate_id, len(covered))
                        if best is None or item < best:
                            best = item
                    previous = (candidate.yaw_deg, candidate.pitch_deg)
            predicted_s = best[0] if best is not None else math.inf
            feasible = (
                best is not None and
                predicted_s + self.repair_witness_margin_s <=
                recovery_window_s + self.objective_epsilon_s
            )
            witnesses.append({
                "faulted_device_id": faulted_id,
                "predicted_first_reassigned_cover_s": predicted_s,
                "recovery_window_s": recovery_window_s,
                "required_margin_s": self.repair_witness_margin_s,
                "feasible_before_recovery": feasible,
                "healthy_device_id": best[1] if best is not None else "",
                "candidate_id": best[2] if best is not None else "",
                "predicted_cell_count": best[3] if best is not None else 0,
            })
        feasible_rows = [row for row in witnesses if row["feasible_before_recovery"]]
        selected = min(
            feasible_rows,
            key=lambda row: (
                float(row["predicted_first_reassigned_cover_s"]),
                str(row["faulted_device_id"]),
            ),
            default=None,
        )
        return {
            "time_model": "calibrated_one_sided_upper",
            "feasible_before_recovery": selected is not None,
            "selected": selected,
            "by_faulted_device": witnesses,
        }

    def _search_repair_insertions(
        self,
        baseline_routes: Mapping[str, Sequence[str]],
        baseline_objective: PlanObjective,
        start_poses: Mapping[str, tuple[float, float]],
        start_offsets: Mapping[str, float],
        residual: set[int],
        transferable: set[int],
        original_owned: Mapping[str, set[int]],
        deadline: float,
    ) -> tuple[dict[str, list[str]], PlanObjective, dict[str, Any]]:
        """Search healthy-queue insertions with an observable-repair witness."""
        proposal = self._copy_routes(baseline_routes)
        proposal_objective = baseline_objective
        evaluation_limit = min(
            self.planner_max_evaluations, self.repair_local_search_max_evaluations
        )
        evaluations = 0
        selected_ids: set[str] = set()
        accepted: list[dict[str, Any]] = []
        stop_reason = "no_strict_improvement"

        ranked: list[tuple[tuple[Any, ...], Candidate]] = []
        for device_id in self.device_ids:
            if device_id in self.faults:
                continue
            existing = set(proposal[device_id])
            per_device: list[tuple[tuple[Any, ...], Candidate]] = []
            for candidate in self.candidates_by_device[device_id]:
                gain = len(candidate.coverage & transferable)
                if gain == 0 or candidate.candidate_id in existing:
                    continue
                upper, _ = self._planning_edge(
                    device_id, start_poses[device_id],
                    (candidate.yaw_deg, candidate.pitch_deg), True,
                )
                eta = max(self.score_epsilon, start_offsets[device_id] + upper)
                key = (-gain / eta, eta, -gain, candidate.candidate_id)
                per_device.append((key, candidate))
            ranked.extend(sorted(per_device)[:self.repair_candidate_pool_per_device])
        ranked.sort(key=lambda item: (*item[0], item[1].device_id))

        for _ in range(self.repair_max_insertions):
            best: tuple[tuple[Any, ...], dict[str, list[str]], PlanObjective,
                        dict[str, Any], Candidate, int] | None = None
            for _, candidate in ranked:
                if candidate.candidate_id in selected_ids:
                    continue
                device_id = candidate.device_id
                for position in range(len(proposal[device_id]) + 1):
                    if evaluations >= evaluation_limit:
                        stop_reason = "evaluation_limit"
                        break
                    if time.perf_counter() >= deadline:
                        stop_reason = "wall_clock_deadline"
                        break
                    trial = self._copy_routes(proposal)
                    trial[device_id].insert(position, candidate.candidate_id)
                    objective = self._plan_objective(
                        trial, start_poses, residual, start_offsets=start_offsets
                    )
                    evaluations += 1
                    if not self._v2_objective_acceptable(
                        objective, proposal_objective, baseline_objective.t100_s
                    ):
                        continue
                    witness = self._repair_witness(
                        trial, start_poses, start_offsets, original_owned
                    )
                    if not witness["feasible_before_recovery"]:
                        continue
                    selected_witness = witness["selected"] or {}
                    key = (
                        float(selected_witness.get(
                            "predicted_first_reassigned_cover_s", math.inf
                        )),
                        *objective.search_key(),
                        device_id,
                        candidate.candidate_id,
                        position,
                    )
                    if best is None or key < best[0]:
                        best = (key, trial, objective, witness, candidate, position)
                if stop_reason in {"evaluation_limit", "wall_clock_deadline"}:
                    break
            if best is None:
                break
            _, proposal, proposal_objective, witness, candidate, position = best
            selected_ids.add(candidate.candidate_id)
            accepted.append({
                "candidate_id": candidate.candidate_id,
                "device_id": candidate.device_id,
                "queue_position": position,
                "objective": proposal_objective.as_dict(),
                "witness": witness["selected"],
            })
            stop_reason = "maximum_insertions" if (
                len(accepted) >= self.repair_max_insertions
            ) else "no_strict_improvement"

        final_witness = self._repair_witness(
            proposal, start_poses, start_offsets, original_owned
        )
        audit = {
            "repair_search": "healthy_queue_insertion_with_pre_recovery_witness",
            "candidate_pool_size": len(ranked),
            "accepted_insertions": accepted,
            "evaluations": evaluations,
            "evaluation_limit": evaluation_limit,
            "evaluation_limit_respected": evaluations <= evaluation_limit,
            "stop_reason": stop_reason,
            "witness": final_witness,
        }
        return proposal, proposal_objective, audit

    def _replan(self, context: PolicyContext, *, replace_queues: bool,
                events: list[dict[str, Any]] | None = None,
                reason: str = "queue_exhausted") -> None:
        trigger_t = self.pending_replan_since_t
        trigger_reason = self.pending_replan_reason or reason
        effective = False
        event_kind = "replan_applied"
        comparison: dict[str, Any] = {}
        reassigned_count = 0

        if self.is_v2 and replace_queues:
            repair_begin = time.perf_counter()
            budget_ms = self.repair_planner_budget_ms
            effective_budget_ms = max(0.0, budget_ms - self.planner_deadline_guard_ms)
            deadline = repair_begin + effective_budget_ms / 1000.0
            baseline_routes = self._copy_routes(self.routes)
            for device_id, fault in self.faults.items():
                if fault.candidate_id and fault.candidate_id not in baseline_routes[device_id]:
                    baseline_routes[device_id].insert(0, fault.candidate_id)
            residual = set(self.reachable - self.confirmed - self._reserved_coverage())
            start_poses, start_offsets = self._repair_queue_start_state(context)
            for device_id, fault in self.faults.items():
                if (fault.reason == "logical_pause" and self.logical_pause is not None and
                        self.logical_pause.started_t is not None):
                    start_offsets[device_id] = max(
                        0.0, self.logical_pause.started_t + self.logical_pause.duration_s - context.t)
                else:
                    start_offsets[device_id] = self.fault_recovery_bound_s
            baseline_objective = self._plan_objective(
                baseline_routes, start_poses, residual, start_offsets=start_offsets)
            original_owned: dict[str, set[int]] = {}
            for faulted_id in self.faults:
                own: set[int] = set()
                for candidate_id in baseline_routes[faulted_id]:
                    own.update(self.candidates[candidate_id].coverage & residual)
                other: set[int] = set()
                for device_id, route in baseline_routes.items():
                    if device_id == faulted_id:
                        continue
                    for candidate_id in route:
                        other.update(self.candidates[candidate_id].coverage & residual)
                original_owned[faulted_id] = own - other
            transferable = set().union(*original_owned.values()) if original_owned else set()
            try:
                if transferable:
                    proposal, proposal_objective, repair_audit = self._search_repair_insertions(
                        baseline_routes,
                        baseline_objective,
                        start_poses,
                        start_offsets,
                        residual,
                        transferable,
                        original_owned,
                        deadline,
                    )
                else:
                    proposal = self._copy_routes(baseline_routes)
                    proposal_objective = baseline_objective
                    repair_audit = {
                        "repair_search": "none_no_transferable_cells",
                        "evaluations": 0,
                        "evaluation_limit": min(
                            self.planner_max_evaluations,
                            self.repair_local_search_max_evaluations,
                        ),
                        "evaluation_limit_respected": True,
                        "witness": self._repair_witness(
                            proposal, start_poses, start_offsets, original_owned
                        ),
                    }
                witness = repair_audit.get("witness", {})
                effective = self._v2_objective_acceptable(
                    proposal_objective, baseline_objective, baseline_objective.t100_s,
                    minimum_uac_gain_s=self.switch_min_gain_s,
                ) and witness.get("feasible_before_recovery") is True
            except ValueError as exc:
                proposal = self._copy_routes(baseline_routes)
                proposal_objective = PlanObjective(
                    len(residual), self.objective_horizon_s, self.objective_horizon_s,
                    self.objective_horizon_s, 0.0, 0.0)
                repair_audit = {"repair_rejected_error": str(exc)}
                witness = {}
                effective = False
            self.last_planner_ms = (time.perf_counter() - repair_begin) * 1000.0
            self.last_plan_audit = {
                **repair_audit,
                "configured_budget_ms": budget_ms,
                "deadline_guard_ms": self.planner_deadline_guard_ms,
                "effective_search_budget_ms": effective_budget_ms,
                "observed_planner_ms": self.last_planner_ms,
                "budget_contract_satisfied": self.last_planner_ms <= budget_ms,
            }
            self.total_plans += 1
            comparison = {
                "continuation_objective": baseline_objective.as_dict(),
                "proposal_objective": proposal_objective.as_dict(),
                "minimum_uac_gain_s": self.switch_min_gain_s,
                "transferable_cell_count": len(transferable),
                "transferable_cell_ids": [self.cells[index].cell_id for index in sorted(transferable)],
                "pre_recovery_witness": witness,
            }
            if effective:
                comparison["decision"] = (
                    "accepted_strict_uac_gain_t100_nonincrease_with_pre_recovery_witness"
                )
            elif not transferable:
                comparison["decision"] = "rejected_no_transferable_exclusive_cells"
            elif proposal_objective.uncovered_count:
                comparison["decision"] = "rejected_incomplete_proposal"
            elif proposal_objective.t100_s > baseline_objective.t100_s + self.objective_epsilon_s:
                comparison["decision"] = "rejected_t100_increase"
            elif witness.get("feasible_before_recovery") is not True:
                comparison["decision"] = "rejected_no_pre_recovery_witness"
            else:
                comparison["decision"] = "rejected_uac_gain_below_switch_threshold"
            if effective:
                self.routes = self._copy_routes(proposal)
                proposed_by_device: dict[str, set[int]] = {device_id: set() for device_id in self.device_ids}
                for device_id, route in self.routes.items():
                    for candidate_id in route:
                        proposed_by_device[device_id].update(self.candidates[candidate_id].coverage & residual)
                for faulted_id, cells in original_owned.items():
                    reassigned = set().union(*(
                        coverage for device_id, coverage in proposed_by_device.items()
                        if device_id != faulted_id
                    )) & cells
                    for index in reassigned:
                        self.pending_reassigned_cells[index] = {
                            "original_device_id": faulted_id,
                            "trigger_t": float(trigger_t if trigger_t is not None else context.t),
                        }
                    reassigned_count += len(reassigned)
                self.last_effective_replan_t = float(context.t)
            else:
                self.routes = baseline_routes
                event_kind = "replan_noop"
        else:
            if replace_queues:
                self.routes = {device_id: [] for device_id in self.device_ids}
            residual = set(self.reachable - self.confirmed - self._reserved_coverage())
            if not residual:
                self.last_planner_ms = 0.0
            else:
                planned = self._make_plan(context, residual)
                for device_id in self.device_ids:
                    if replace_queues:
                        self.routes[device_id] = planned[device_id]
                    else:
                        self.routes[device_id].extend(planned[device_id])
                effective = any(planned.values())
        self.pending_replan = False
        self.last_replan_t = float(context.t)
        if events is not None:
            events.append(self._event(
                event_kind if replace_queues else "plan_completed", context,
                reason=trigger_reason,
                trigger_mission_elapsed_s=(self._elapsed(trigger_t) if trigger_t is not None else None),
                applied_mission_elapsed_s=self._elapsed(context.t),
                response_latency_s=(max(0.0, context.t - trigger_t) if trigger_t is not None else 0.0),
                residual_count=len(residual),
                queued_task_count=sum(len(route) for route in self.routes.values()),
                active_reserved_count=sum(task is not None for task in self.active.values()),
                planner_ms=self.last_planner_ms,
                planner_variant=self.planner_variant,
                effective=effective,
                reassigned_cell_count=reassigned_count,
                comparison=comparison,
                plan_audit=self.last_plan_audit,
            ))
        self.pending_replan_reason = ""
        self.pending_replan_since_t = None

    def decide(self, context: PolicyContext) -> Decision:
        call_started = time.perf_counter()
        events: list[dict[str, Any]] = []
        actions: dict[str, Action] = {}
        self.decision_count += 1
        self.last_planner_ms = 0.0

        if self._last_context_t is not None and context.t + 1e-9 < self._last_context_t:
            # A new monotonic epoch without reset is unsafe; do not reuse old
            # transition timestamps.  Normal Experiment lifecycle calls reset.
            self.reset()
        self._last_context_t = float(context.t)

        # Define mission t=0 only after every device has fresh telemetry, a
        # verified fixed zoom, and a fresh usable video frame in this runtime
        # connection.  The batch preflight is deliberately a separate
        # connection, so carrying readiness across it would introduce a
        # method-dependent random startup transient.  While waiting we issue
        # only explicit hold/stop actions and do not record fault events.
        if self.mission_started_t is None:
            startup_wait: dict[str, str] = {}
            for device_id in self.device_ids:
                reason = self._base_unavailable_reason(context, device_id)
                if reason is None and self.require_frame:
                    if self._fresh_usable_frame(context, device_id) is None:
                        reason = "frame_not_ready"
                if reason is not None:
                    startup_wait[device_id] = reason
            if startup_wait:
                for device_id in self.device_ids:
                    if self._fresh_state(context, device_id) is not None:
                        actions[device_id] = self._hold_action(
                            context, device_id, f"{self.method}:startup_wait_stop"
                        )
                diagnostics: dict[str, Any] = {
                    "algorithm": "execution_aware_cooperative_fov_coverage",
                    "algorithm_version": ALGORITHM_VERSION,
                    "method": self.method,
                    "planner_variant": self.planner_variant,
                    "decision_count": self.decision_count,
                    "mission_state": "STARTUP_WAIT",
                    "mission_elapsed_s": 0.0,
                    "confirmed_count": 0,
                    "reachable_count": len(self.reachable),
                    "reachable_ratio": (len(self.reachable) / len(self.cells)
                                        if self.cells else 0.0),
                    "coverage_fraction": 0.0,
                    "evaluation_cell_count": len(self.cells),
                    "evaluation_grid": list(self.evaluation_grid),
                    "candidate_count": len(self.candidates),
                    "planner_ms": 0.0,
                    "static_precompute_ms": self.static_precompute_ms,
                    "static_precomputed_edge_count": self.static_precomputed_edge_count,
                    "policy_compute_ms": (time.perf_counter() - call_started) * 1000.0,
                    "planner_calls": self.total_plans,
                    "queued_task_count": 0,
                    "stage_by_device": {device_id: "STARTUP_WAIT" for device_id in self.device_ids},
                    "attempt_by_device": {device_id: "" for device_id in self.device_ids},
                    "fault_by_device": {},
                    "startup_wait_by_device": startup_wait,
                    "execution_model_status": self.execution_model_status,
                    "fault_injection_kind": self.fault_kind,
                    "commissioning_mode": self.commissioning_mode,
                    "commissioning_completed_count": 0,
                    "commissioning_transition_count": len(self.commissioning_rows),
                    "completion_gate": {
                        "require_ack": self.require_ack,
                        "require_frame": self.require_frame,
                        "distinct_frames": self.distinct_frames,
                    },
                    "events": [],
                }
                return Decision(actions, diagnostics)

        if self.mission_started_t is None:
            self.mission_started_t = float(context.t)
        if not self.mission_started_emitted:
            events.append(self._event("mission_started", context,
                                      evaluation_cell_count=len(self.cells),
                                      reachable_count=len(self.reachable),
                                      reachable_ratio=len(self.reachable) / len(self.cells),
                                      method=self.method,
                                      commissioning_transition_count=len(self.commissioning_rows)))
            self.mission_started_emitted = True

        # COMPLETE is an explicit one-cycle state.  Only now is the device made
        # idle and eligible for another task.
        for device_id, task in tuple(self.active.items()):
            if task is not None and task.stage == "COMPLETE":
                self.active[device_id] = None

        self._update_coverage_triggered_pause(context)
        self._refresh_faults(context, events)

        for device_id in self.device_ids:
            action = self._advance_task(context, device_id, events)
            if action is not None:
                actions[device_id] = action

        if not self.commissioning_mode and self.method in REPAIR_METHODS and self.pending_replan:
            if context.t - self.last_replan_t >= self.replan_min_interval_s:
                self._replan(context, replace_queues=True, events=events,
                             reason="event_triggered_residual_repair")

        residual_unreserved = set(self.reachable - self.confirmed - self._reserved_coverage())
        queued = any(self.routes[device_id] for device_id in self.device_ids)
        if (not self.commissioning_mode and residual_unreserved and not queued and
                not self.pending_replan):
            # Planner budgets may intentionally return a route chunk.  Planning
            # the next chunk after queues drain is not fault repair and does not
            # let static baselines reassign an interrupted active task.
            self._replan(context, replace_queues=False, events=events, reason="planner_chunk")

        self._assign_available(context, events)
        for device_id in self.device_ids:
            if device_id not in actions and self.active[device_id] is not None:
                action = self._advance_task(context, device_id, events)
                if action is not None:
                    actions[device_id] = action

        # For fresh idle devices, an empty action explicitly stops a prior
        # velocity command.  During a registered logical pause no action is
        # emitted or renewed: the last command expires by its short TTL without
        # ever blocking the 10 Hz decision loop or watchdog.
        for device_id in self.device_ids:
            if device_id in actions:
                continue
            if self._pause_reason(context, device_id):
                continue
            state = self._fresh_state(context, device_id)
            if state is not None:
                actions[device_id] = self._hold_action(context, device_id, f"{self.method}:idle_stop")

        main_complete = len(self.confirmed) == len(self.cells)
        commissioning_complete = (
            self.commissioning_mode and
            len(self.commissioning_completed_ids) == len(self.commissioning_rows)
        )
        if ((commissioning_complete if self.commissioning_mode else main_complete) and
                self.mission_completed_t is None):
            self.mission_completed_t = float(context.t)
        if self.mission_completed_t is not None and not self.mission_completed_emitted:
            events.append(self._event("mission_completed", context,
                                      confirmed_count=len(self.confirmed),
                                      evaluation_cell_count=len(self.cells),
                                      reachable_count=len(self.reachable),
                                      coverage_fraction=len(self.confirmed) / len(self.cells),
                                      commissioning_completed_count=len(self.commissioning_completed_ids),
                                      commissioning_transition_count=len(self.commissioning_rows)))
            self.mission_completed_emitted = True

        active_stages = {device_id: (task.stage if task else "IDLE")
                         for device_id, task in self.active.items()}
        active_attempts = {device_id: (task.attempt_id if task else "")
                           for device_id, task in self.active.items()}
        queued_count = sum(len(route) for route in self.routes.values())
        if self.mission_completed_t is not None:
            mission_state = "COMPLETE"
        elif self.faults and not any(self.active.values()) and queued_count == 0:
            mission_state = "DEGRADED"
        elif self.last_planner_ms > 0 and not any(self.active.values()):
            mission_state = "PLANNING"
        else:
            mission_state = "RUNNING"

        diagnostics: dict[str, Any] = {
            "algorithm": "execution_aware_cooperative_fov_coverage",
            "algorithm_version": ALGORITHM_VERSION,
            "method": self.method,
            "planner_variant": self.planner_variant,
            "decision_count": self.decision_count,
            "mission_state": mission_state,
            "mission_elapsed_s": self._elapsed(context.t),
            "confirmed_count": len(self.confirmed),
            "confirmed_cell_ids": [self.cells[index].cell_id for index in sorted(self.confirmed)],
            "reachable_count": len(self.reachable),
            "reachable_ratio": (len(self.reachable) / len(self.cells) if self.cells else 0.0),
            "coverage_fraction": len(self.confirmed) / len(self.cells),
            "evaluation_cell_count": len(self.cells),
            "evaluation_grid": list(self.evaluation_grid),
            "candidate_count": len(self.candidates),
            "planner_ms": self.last_planner_ms,
            "static_precompute_ms": self.static_precompute_ms,
            "static_precomputed_edge_count": self.static_precomputed_edge_count,
            "policy_compute_ms": (time.perf_counter() - call_started) * 1000.0,
            "planner_calls": self.total_plans,
            "plan_audit": self.last_plan_audit,
            "queued_task_count": queued_count,
            "stage_by_device": active_stages,
            "attempt_by_device": active_attempts,
            "fault_by_device": {device_id: fault.reason for device_id, fault in sorted(self.faults.items())},
            "execution_model_status": self.execution_model_status,
            "fault_injection_kind": self.fault_kind,
            "commissioning_mode": self.commissioning_mode,
            "commissioning_completed_count": len(self.commissioning_completed_ids),
            "commissioning_transition_count": len(self.commissioning_rows),
            "completion_gate": {
                "require_ack": self.require_ack,
                "require_frame": self.require_frame,
                "distinct_frames": self.distinct_frames,
                "frame_pose_max_gap_s": self.frame_pose_max_gap_s,
            },
            "events": events,
        }
        return Decision(actions, diagnostics)

