from __future__ import annotations

import copy
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


WORKSPACE = Path(__file__).resolve().parents[3]
STAGE = WORKSPACE / "ral_hil_stage1"
KIT = WORKSPACE / "ZR10_Algorithm_Kit"
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(STAGE))

from algorithms.user_policy import MyPolicy  # noqa: E402
from zr10lab.config import DeviceConfig, LabConfig  # noqa: E402
from zr10lab.models import CommandResult, Telemetry  # noqa: E402


def make_config(method: str = "ea_cml", *, devices: int = 1,
                require_frame: bool = True, require_ack: bool = True,
                settle_s: float = 0.10, dwell_s: float = 0.10,
                frame_timeout_s: float = 0.30) -> LabConfig:
    configured = []
    positions = [(0.0, -2.0, 0.0), (0.0, 2.0, 0.0)]
    for index in range(devices):
        configured.append(DeviceConfig(
            id=f"cam_{index + 1}",
            ip=f"127.0.0.{index + 11}",
            position_m=positions[index],
            mount_rpy_deg=(0.0, 0.0, 0.0),
            yaw_limits_deg=(-90.0, 90.0),
            pitch_limits_deg=(-45.0, 45.0),
            initial_yaw_deg=0.0,
            initial_pitch_deg=0.0,
            initial_zoom=1.0,
            max_slew_dps=30.0,
            camera={
                "image_size": [640, 480],
                "intrinsics": [{
                    "zoom": 1.0, "fx": 420.0, "fy": 420.0,
                    "cx": 320.0, "cy": 240.0, "dist": [0, 0, 0, 0, 0],
                }],
            },
            control={"mode": "velocity_p", "use_ack": True},
        ))
    return LabConfig(
        devices=configured,
        system={"rate_hz": 10, "telemetry_stale_s": 0.5,
                "max_frame_age_s": 0.6, "action_ttl_s": 0.5},
        policy={
            "method": method,
            "evaluation_points_m": [[20.0, -1.0, 1.0], [20.0, 1.0, 1.0]],
            "fixed_zoom": 1.0,
            "safe_envelope_deg": {"yaw": [-35.0, 35.0], "pitch": [-20.0, 20.0]},
            "fov_margin": 0.02,
            "planner_budget_ms": 100.0,
            "planner_max_evaluations": 10000,
            "replan_min_interval_s": 0.0,
            "prediction_overrun_grace_s": 100.0,
            "fault_recovery_hold_s": 0.2,
            "execution_confirmation": {
                "pose_tolerance_deg": 0.5,
                "max_rate_dps": 0.5,
                "settle_hold_s": settle_s,
                "min_dwell_s": dwell_s,
                "distinct_frames": 2,
                "frame_timeout_s": frame_timeout_s,
                "transition_timeout_s": 10.0,
                "require_ack": require_ack,
                "require_frame": require_frame,
                "allow_pose_only_fallback": False,
            },
        },
        action_space={"enabled": ["yaw_deg", "pitch_deg"]},
    )


def state(device_id: str, t: float, yaw: float = 0.0, pitch: float = 0.0,
          *, connected: bool = True) -> Telemetry:
    return Telemetry(device_id, t, yaw, pitch, zoom=1.0,
                     yaw_rate_dps=0.0, pitch_rate_dps=0.0,
                     connected=connected, source="simulation")


def context(t: float, states: dict[str, Telemetry], *, step: int = 0,
            frames=None, results=None, metrics=None):
    return SimpleNamespace(
        t=float(t), dt=0.1, step=step, devices=states,
        detections=(), rays=(), tracks=(), metrics=dict(metrics or {}),
        frames=dict(frames or {}), command_results=dict(results or {}),
    )


def usable_frame(device_id: str, frame_id: int, t: float):
    return SimpleNamespace(device_id=device_id, frame_id=frame_id,
                           capture_t=t, received_t=t, usable=True,
                           resolution_ok=True)


def pose_at_current_targets(policy: MyPolicy, t: float) -> dict[str, Telemetry]:
    result = {}
    for device_id in policy.device_ids:
        task = policy.active[device_id]
        if task is None:
            result[device_id] = state(device_id, t)
        else:
            candidate = policy.candidates[task.candidate_id]
            result[device_id] = state(device_id, t, candidate.yaw_deg, candidate.pitch_deg)
    return result


def first_actions(policy: MyPolicy, start: float = 100.0):
    states = {device_id: state(device_id, start) for device_id in policy.device_ids}
    frames = ({device_id: usable_frame(device_id, 1, start)
               for device_id in policy.device_ids} if policy.require_frame else {})
    decision = policy.decide(context(start, states, frames=frames))
    return decision, states


def test_mission_clock_waits_for_connection_local_frame_readiness_without_faults():
    policy = MyPolicy(make_config(require_frame=True, require_ack=False))
    device_id = policy.device_ids[0]
    states = {device_id: state(device_id, 100.0)}

    waiting = policy.decide(context(100.0, states))
    assert waiting.diagnostics["mission_state"] == "STARTUP_WAIT"
    assert waiting.diagnostics["startup_wait_by_device"] == {device_id: "frame_not_ready"}
    assert waiting.diagnostics["events"] == []
    assert waiting.diagnostics["fault_by_device"] == {}
    assert policy.mission_started_t is None
    assert waiting.actions[device_id].yaw_deg is None
    assert waiting.actions[device_id].pitch_deg is None

    ready = policy.decide(context(
        100.4, {device_id: state(device_id, 100.4)}, step=1,
        frames={device_id: usable_frame(device_id, 7, 100.4)},
    ))
    assert policy.mission_started_t == pytest.approx(100.4)
    assert ready.diagnostics["mission_elapsed_s"] == pytest.approx(0.0)
    assert any(event["type"] == "mission_started" for event in ready.diagnostics["events"])
    assert device_id in ready.actions


def acknowledged(action, device_id: str, t: float, *, failed: bool = False):
    return CommandResult(device_id, t, action,
                         "error" if failed else "acknowledged",
                         sent=True, ack=not failed,
                         error="synthetic failure" if failed else "")


def test_move_settle_dwell_frame_confirm_and_ack_is_not_completion():
    policy = MyPolicy(make_config())
    first, _ = first_actions(policy)
    device_id = policy.device_ids[0]
    action = first.actions[device_id]
    assert policy.active[device_id] is not None
    assert policy.active[device_id].stage == "MOVE"
    attempt_id = policy.active[device_id].attempt_id
    assert attempt_id in action.reason

    states = pose_at_current_targets(policy, 100.05)
    ack = acknowledged(action, device_id, 100.05)
    settled = policy.decide(context(100.05, states, step=1, results={device_id: ack}))
    assert settled.diagnostics["confirmed_count"] == 0
    assert policy.active[device_id].ack_seen is True
    assert policy.active[device_id].stage == "SETTLE"

    states = pose_at_current_targets(policy, 100.16)
    dwell = policy.decide(context(100.16, states, step=2, results={device_id: ack}))
    assert dwell.diagnostics["confirmed_count"] == 0
    assert policy.active[device_id].stage == "DWELL"

    states = pose_at_current_targets(policy, 100.27)
    one_frame = policy.decide(context(
        100.27, states, step=3,
        frames={device_id: usable_frame(device_id, 1, 100.27)},
        results={device_id: ack},
    ))
    assert one_frame.diagnostics["confirmed_count"] == 0
    assert policy.active[device_id].stage == "FRAME_CONFIRM"

    states = pose_at_current_targets(policy, 100.37)
    completed = policy.decide(context(
        100.37, states, step=4,
        frames={device_id: usable_frame(device_id, 2, 100.37)},
        results={device_id: ack},
    ))
    assert completed.diagnostics["confirmed_count"] > 0
    assert policy.active[device_id].stage == "COMPLETE"
    kinds = [event["type"] for event in completed.diagnostics["events"]]
    assert kinds[:2] == ["coverage_confirmed", "transition_completed"]
    transition = completed.diagnostics["events"][1]
    assert transition["attempt_id"] == attempt_id
    assert transition["time_definition"] == "command_issued_to_frame_confirm"
    assert transition["actual_s"] == pytest.approx(0.37)

    count = completed.diagnostics["confirmed_count"]
    after = policy.decide(context(100.47, pose_at_current_targets(policy, 100.47), step=5))
    assert after.diagnostics["confirmed_count"] >= count


def test_frame_timeout_never_confirms_and_starts_repair_fault():
    policy = MyPolicy(make_config(require_ack=False, frame_timeout_s=0.2))
    first, _ = first_actions(policy)
    device_id = policy.device_ids[0]
    assert first.diagnostics["confirmed_count"] == 0
    for step, t in enumerate((100.05, 100.16, 100.27), start=1):
        policy.decide(context(t, pose_at_current_targets(policy, t), step=step))
    assert policy.active[device_id].stage == "FRAME_CONFIRM"
    timed_out = policy.decide(context(100.50, pose_at_current_targets(policy, 100.50), step=4))
    assert timed_out.diagnostics["confirmed_count"] == 0
    assert any(event["type"] == "fault_started" and event["reason"] == "frame_timeout"
               for event in timed_out.diagnostics["events"])
    assert device_id in policy.faults


def test_command_failure_is_not_an_ack_or_completion():
    policy = MyPolicy(make_config())
    first, _ = first_actions(policy)
    device_id = policy.device_ids[0]
    failed = acknowledged(first.actions[device_id], device_id, 100.05, failed=True)
    decision = policy.decide(context(100.05, pose_at_current_targets(policy, 100.05),
                                     step=1, results={device_id: failed}))
    assert decision.diagnostics["confirmed_count"] == 0
    assert any(event["type"] == "fault_started" and event["reason"] == "command_failure"
               for event in decision.diagnostics["events"])


def test_overlapping_second_task_is_revalidated_as_stale_before_command():
    policy = MyPolicy(make_config("fpr", devices=2, require_frame=False,
                                  require_ack=False, settle_s=0.0, dwell_s=0.0))
    first, _ = first_actions(policy)
    assert all(policy.active[device_id] is not None for device_id in policy.device_ids)
    first_device, second_device = policy.device_ids

    # Drive both to FRAME_CONFIRM, then complete only the first.  Its wide FoV
    # covers both evaluation cells, making the second active task stale.
    for step, t in enumerate((100.05, 100.06), start=1):
        policy.decide(context(t, pose_at_current_targets(policy, t), step=step))
    completed = policy.decide(context(100.07, pose_at_current_targets(policy, 100.07), step=3))
    assert completed.diagnostics["confirmed_count"] == len(policy.reachable)
    coverage_events = [event for event in completed.diagnostics["events"]
                       if event["type"] == "coverage_confirmed"]
    assert len(coverage_events) == 1

    next_decision = policy.decide(context(100.08, pose_at_current_targets(policy, 100.08), step=4))
    assert next_decision.diagnostics["confirmed_count"] == len(policy.reachable)
    assert not [event for event in next_decision.diagnostics["events"]
                if event["type"] == "coverage_confirmed"]
    assert all(action.yaw_deg is None and action.pitch_deg is None
               for action in next_decision.actions.values())


def test_logical_pause_causes_repair_then_recovery_with_new_attempt():
    policy = MyPolicy(make_config(require_frame=False, require_ack=False))
    first, _ = first_actions(policy)
    device_id = policy.device_ids[0]
    old_attempt = policy.active[device_id].attempt_id
    paused = policy.decide(context(
        100.05, pose_at_current_targets(policy, 100.05), step=1,
        metrics={f"pause:{device_id}": 1},
    ))
    assert any(event["type"] == "fault_started" and event["reason"] == "logical_pause"
               for event in paused.diagnostics["events"])
    # A registered pause deliberately emits no command for the affected
    # device, so the last short-TTL velocity command expires naturally.
    assert device_id not in paused.actions
    assert policy.active[device_id] is None

    recovered = policy.decide(context(
        100.30, {device_id: state(device_id, 100.30)}, step=2,
        metrics={f"pause:{device_id}": 0},
    ))
    assert any(event["type"] == "fault_recovered" for event in recovered.diagnostics["events"])
    assert policy.active[device_id] is not None
    assert policy.active[device_id].attempt_id != old_attempt


@pytest.mark.parametrize("method", ["fpr", "mag", "open_cml", "measured_cml", "ea_cml",
                                     "uac_robust_repair_v2"])
def test_all_methods_are_selectable(method: str):
    needs_evidence = method == "uac_robust_repair_v2"
    policy = MyPolicy(make_config(method, require_frame=needs_evidence, require_ack=needs_evidence))
    decision, _ = first_actions(policy)
    assert decision.diagnostics["method"] == method
    assert decision.diagnostics["evaluation_cell_count"] == 2
    assert decision.diagnostics["reachable_count"] <= 2
    assert decision.diagnostics["reachable_ratio"] == pytest.approx(
        decision.diagnostics["reachable_count"] / 2
    )


def test_v2_requires_ack_and_fresh_frame_evidence():
    with pytest.raises(ValueError, match="requires both ACK and fresh-frame"):
        MyPolicy(make_config("uac_robust_repair_v2", require_frame=False, require_ack=True))
    with pytest.raises(ValueError, match="requires both ACK and fresh-frame"):
        MyPolicy(make_config("uac_robust_repair_v2", require_frame=True, require_ack=False))


def test_v2_plan_is_robust_uac_and_t100_non_regressing_against_mag():
    cfg = make_config("uac_robust_repair_v2", devices=2)
    cfg.policy["initial_planner_budget_ms"] = 200.0
    policy = MyPolicy(cfg)
    decision, _ = first_actions(policy)
    audit = decision.diagnostics["plan_audit"]
    assert audit["guarantee_reference"] == "mag"
    assert audit["uac_non_regression_verified"] is True
    assert audit["t100_non_regression_verified"] is True
    assert audit["final_objective"]["uncovered_count"] == 0
    assert audit["final_objective"]["uac_h_s"] <= audit["baseline_objective"]["uac_h_s"] + 1e-9
    assert audit["final_objective"]["t100_s"] <= audit["baseline_objective"]["t100_s"] + 1e-9
    assert any(event["type"] == "plan_completed" for event in decision.diagnostics["events"])


def test_v243_edge_prewarm_is_route_and_objective_equivalent_to_cold_planning():
    warm_cfg = make_config("uac_robust_repair_v2", devices=2)
    warm_cfg.policy["initial_planner_budget_ms"] = 5000.0
    cold_cfg = copy.deepcopy(warm_cfg)
    warm = MyPolicy(warm_cfg)
    cold = MyPolicy(cold_cfg)
    assert warm.static_precomputed_edge_count > 0

    # Remove only the static edge cache.  No route, objective, queue or policy
    # decision is reused by either side of the comparison.
    cold._planning_edge_cache.clear()
    cold.static_precomputed_edge_count = 0
    cold.static_precompute_ms = 0.0
    warm_decision, _ = first_actions(warm)
    cold_decision, _ = first_actions(cold)

    warm_routes = {
        device_id: list(route) for device_id, route in warm.routes.items()
    }
    cold_routes = {
        device_id: list(route) for device_id, route in cold.routes.items()
    }
    assert warm_routes == cold_routes
    warm_audit = dict(warm_decision.diagnostics["plan_audit"])
    cold_audit = dict(cold_decision.diagnostics["plan_audit"])
    runtime_fields = {
        "observed_planner_ms", "budget_contract_satisfied",
        "initial_edge_cache_prewarmed", "static_precomputed_edge_count",
    }
    for key in runtime_fields:
        warm_audit.pop(key, None)
        cold_audit.pop(key, None)
    assert warm_audit == cold_audit


def test_formal_mag_reference_is_the_exact_v2_incumbent():
    v2_cfg = make_config("uac_robust_repair_v2", devices=2)
    v2_cfg.policy["robust_fov"] = {
        "pose_error_yaw_deg": 0.5, "pose_error_pitch_deg": 0.5,
        "planning_pixel_margin": 0.0, "confirmation_pixel_margin": 0.0,
        "enforce_for_all_methods": True,
    }
    v2_cfg.policy["robust_objective"] = {
        "horizon_s": 60.0, "complete_mag_reference": True,
    }
    mag_cfg = copy.deepcopy(v2_cfg)
    mag_cfg.policy["method"] = "mag"
    v2 = MyPolicy(v2_cfg)
    mag = MyPolicy(mag_cfg)
    start_t = 100.0
    states = {device_id: state(device_id, start_t) for device_id in v2.device_ids}
    frames = {device_id: usable_frame(device_id, 1, start_t) for device_id in v2.device_ids}
    ctx = context(start_t, states, frames=frames)
    start_poses = v2._start_poses(ctx)
    incumbent = v2._plan_mag_complete(ctx, set(v2.reachable), start_poses)
    deployed_mag = mag._make_plan(ctx, set(mag.reachable))
    assert v2.candidates == mag.candidates
    assert deployed_mag == incumbent


@pytest.mark.parametrize("measured", [False, True])
def test_bitset_complete_seed_matches_exact_set_construction(measured: bool):
    policy = MyPolicy(make_config("uac_robust_repair_v2", devices=2))
    ctx = context(100.0, {
        device_id: state(device_id, 100.0) for device_id in policy.device_ids
    })
    start_poses = policy._start_poses(ctx)
    residual = set(policy.reachable)

    reference = {device_id: [] for device_id in policy.device_ids}
    tails = dict(start_poses)
    uncovered = set(residual)
    while uncovered:
        best = None
        for device_id in policy.device_ids:
            for candidate in policy.candidates_by_device[device_id]:
                overlap = candidate.coverage & uncovered
                if not overlap:
                    continue
                cost, _ = policy._planning_edge(
                    device_id, tails[device_id],
                    (candidate.yaw_deg, candidate.pitch_deg), measured,
                )
                key = (
                    -len(overlap) / (cost + policy.score_epsilon), cost,
                    device_id, candidate.candidate_id,
                )
                if best is None or key < best[0]:
                    best = (key, device_id, candidate)
        assert best is not None
        _, device_id, candidate = best
        reference[device_id].append(candidate.candidate_id)
        tails[device_id] = (candidate.yaw_deg, candidate.pitch_deg)
        uncovered.difference_update(candidate.coverage)

    optimized = policy._plan_greedy_complete(
        ctx, residual, start_poses, measured=measured,
    )
    assert optimized == reference


def test_v2_t100_constraint_is_not_clipped_at_uac_horizon():
    policy = MyPolicy(make_config("uac_robust_repair_v2", devices=1))
    policy.objective_horizon_s = 0.01
    device_id = policy.device_ids[0]
    ctx = context(100.0, {device_id: state(device_id, 100.0)},
                  frames={device_id: usable_frame(device_id, 1, 100.0)})
    start_poses = policy._start_poses(ctx)
    routes = policy._plan_mag_complete(ctx, set(policy.reachable), start_poses)
    objective = policy._plan_objective(routes, start_poses, set(policy.reachable))
    assert objective.uac_h_s <= policy.objective_horizon_s
    assert objective.t100_s > policy.objective_horizon_s


def test_random_small_instances_preserve_mag_uac_and_t100_bounds():
    for seed in range(20):
        rng = random.Random(seed)
        cfg = make_config("uac_robust_repair_v2", devices=2)
        cfg.policy["evaluation_points_m"] = [
            [rng.uniform(16.0, 28.0), rng.uniform(-4.0, 4.0), rng.uniform(0.5, 3.0)]
            for _ in range(rng.randint(3, 8))
        ]
        cfg.policy["initial_planner_budget_ms"] = 200.0
        policy = MyPolicy(cfg)
        decision, _ = first_actions(policy)
        audit = decision.diagnostics["plan_audit"]
        baseline = audit["baseline_objective"]
        final = audit["final_objective"]
        assert final["uncovered_count"] == 0
        assert final["uac_h_s"] <= baseline["uac_h_s"] + 1e-9
        assert final["t100_s"] <= baseline["t100_s"] + 1e-9
        assert audit["uac_non_regression_verified"] is True
        assert audit["t100_non_regression_verified"] is True


def test_v2_credits_measured_pose_projection_and_records_frame_evidence():
    policy = MyPolicy(make_config("uac_robust_repair_v2", settle_s=0.0, dwell_s=0.0))
    first, _ = first_actions(policy)
    device_id = policy.device_ids[0]
    action = first.actions[device_id]
    ack = acknowledged(action, device_id, 100.01)
    states = pose_at_current_targets(policy, 100.01)
    policy.decide(context(100.01, states, step=1, results={device_id: ack}))
    policy.decide(context(100.02, pose_at_current_targets(policy, 100.02), step=2,
                          results={device_id: ack}))
    policy.decide(context(100.03, pose_at_current_targets(policy, 100.03), step=3,
                          frames={device_id: usable_frame(device_id, 10, 100.03)},
                          results={device_id: ack}))
    completed = policy.decide(context(
        100.04, pose_at_current_targets(policy, 100.04), step=4,
        frames={device_id: usable_frame(device_id, 11, 100.04)},
        results={device_id: ack},
    ))
    coverage = next(event for event in completed.diagnostics["events"]
                    if event["type"] == "coverage_confirmed")
    assert coverage["evidence_mode"] == "measured_pose_fresh_frame_projection"
    assert coverage["frame_id"] == 11
    assert coverage["frame_capture_t"] == pytest.approx(100.04)
    assert coverage["frame_pose_gap_s"] == pytest.approx(0.0)
    assert coverage["measured_yaw_deg"] == pytest.approx(
        policy.candidates[coverage["planned_candidate_id"]].yaw_deg)


def test_v2_rejects_frame_without_synchronized_pose_sample():
    cfg = make_config("uac_robust_repair_v2", settle_s=0.0, dwell_s=0.0,
                      frame_timeout_s=1.0)
    cfg.system["pose_max_gap_s"] = 0.05
    policy = MyPolicy(cfg)
    first, _ = first_actions(policy)
    device_id = policy.device_ids[0]
    ack = acknowledged(first.actions[device_id], device_id, 100.01)
    for step, t in enumerate((100.01, 100.02, 100.03), 1):
        policy.decide(context(t, pose_at_current_targets(policy, t), step=step,
                              results={device_id: ack}))
    rejected = usable_frame(device_id, 20, 100.10)
    decision = policy.decide(context(
        100.30, pose_at_current_targets(policy, 100.30), step=4,
        frames={device_id: rejected}, results={device_id: ack},
    ))
    assert decision.diagnostics["confirmed_count"] == 0


def test_v2_requires_a_synchronized_frame_in_the_completion_cycle():
    cfg = make_config(
        "uac_robust_repair_v2", settle_s=0.0, dwell_s=0.5,
        frame_timeout_s=1.0,
    )
    cfg.system["pose_max_gap_s"] = 0.05
    policy = MyPolicy(cfg)
    first, _ = first_actions(policy)
    device_id = policy.device_ids[0]
    ack = acknowledged(first.actions[device_id], device_id, 100.01)

    policy.decide(context(
        100.01, pose_at_current_targets(policy, 100.01), step=1,
        results={device_id: ack},
    ))
    policy.decide(context(
        100.02, pose_at_current_targets(policy, 100.02), step=2,
        results={device_id: ack},
    ))
    for step, t in enumerate((100.03, 100.04), 3):
        policy.decide(context(
            t, pose_at_current_targets(policy, t), step=step,
            frames={device_id: usable_frame(device_id, step, t)},
            results={device_id: ack},
        ))
    assert len(policy.active[device_id].frame_ids) == 2

    stale = policy.decide(context(
        100.60, pose_at_current_targets(policy, 100.60), step=5,
        frames={device_id: usable_frame(device_id, 4, 100.04)},
        results={device_id: ack},
    ))
    assert stale.diagnostics["confirmed_count"] == 0
    assert policy.active[device_id].stage == "FRAME_CONFIRM"
    assert not any(event["type"] == "coverage_confirmed"
                   for event in stale.diagnostics["events"])

    completed = policy.decide(context(
        100.61, pose_at_current_targets(policy, 100.61), step=6,
        frames={device_id: usable_frame(device_id, 5, 100.61)},
        results={device_id: ack},
    ))
    coverage = next(event for event in completed.diagnostics["events"]
                    if event["type"] == "coverage_confirmed")
    assert completed.diagnostics["confirmed_count"] > 0
    assert coverage["frame_id"] == 5
    assert coverage["frame_pose_gap_s"] <= 0.05


def test_coverage_triggered_pause_selects_largest_unique_owner():
    cfg = make_config("uac_robust_repair_v2", devices=2)
    cfg.policy["fault_injection"] = {
        "enabled": True,
        "kind": "logical_availability_pause",
        "implementation": "policy_availability_mask",
        "trigger": "coverage_fraction",
        "trigger_coverage_fraction": 0.5,
        "device_selection": "max_unique_reserved_coverage",
        "duration_s": 8.0,
        "last_command_expires_by_ttl": True,
        "no_sdk_fault_injection": True,
    }
    policy = MyPolicy(cfg)
    first_actions(policy)
    policy.confirmed.add(next(iter(policy.reachable)))
    policy._update_coverage_triggered_pause(context(
        100.1, pose_at_current_targets(policy, 100.1), step=1,
    ))
    assert policy.logical_pause is not None
    assert policy.logical_pause.started_t == pytest.approx(100.1)
    assert policy.logical_pause.selected_device_id in policy.device_ids


def test_v2_online_repair_is_non_regressing_or_explicit_noop():
    def fault_and_replan(minimum_gain_s: float):
        policy = MyPolicy(make_config("uac_robust_repair_v2", devices=2))
        first_actions(policy)
        faulted, healthy = policy.device_ids
        interrupted_candidate = policy.active[faulted].candidate_id
        ctx = context(
            100.10,
            {device_id: state(device_id, 100.10) for device_id in policy.device_ids},
            step=1,
        )
        events = []
        policy._start_fault(ctx, faulted, "logical_pause", events)
        policy.pending_replan_since_t = ctx.t
        policy.pending_replan_reason = "unit_test_fault"
        policy.switch_min_gain_s = minimum_gain_s
        policy._replan(ctx, replace_queues=True, events=events)
        repair_event = next(event for event in events
                            if event["type"] in {"replan_applied", "replan_noop"})
        return policy, faulted, healthy, interrupted_candidate, repair_event

    accepted, faulted, healthy, interrupted, event = fault_and_replan(0.0)
    comparison = event["comparison"]
    assert event["type"] == "replan_applied"
    assert event["effective"] is True
    assert comparison["decision"] == (
        "accepted_strict_uac_gain_t100_nonincrease_with_pre_recovery_witness"
    )
    assert comparison["pre_recovery_witness"]["feasible_before_recovery"] is True
    assert (comparison["proposal_objective"]["uac_h_s"] <
            comparison["continuation_objective"]["uac_h_s"])
    assert (comparison["proposal_objective"]["t100_s"] <=
            comparison["continuation_objective"]["t100_s"])
    assert accepted.routes[healthy]
    assert event["reassigned_cell_count"] > 0
    assert accepted.pending_reassigned_cells

    rejected, faulted, healthy, interrupted, event = fault_and_replan(100.0)
    assert event["type"] == "replan_noop"
    assert event["effective"] is False
    assert event["comparison"]["decision"] == "rejected_uac_gain_below_switch_threshold"
    assert rejected.routes[faulted] == [interrupted]
    assert rejected.routes[healthy] == []
    assert not rejected.pending_reassigned_cells


def test_plans_and_first_actions_are_deterministic():
    cfg = make_config("ea_cml", devices=2, require_frame=False, require_ack=False)
    first_policy, second_policy = MyPolicy(copy.deepcopy(cfg)), MyPolicy(copy.deepcopy(cfg))
    first, _ = first_actions(first_policy)
    second, _ = first_actions(second_policy)
    projected_first = {key: (value.yaw_deg, value.pitch_deg, value.reason)
                       for key, value in first.actions.items()}
    projected_second = {key: (value.yaw_deg, value.pitch_deg, value.reason)
                        for key, value in second.actions.items()}
    assert projected_first == projected_second
    assert first_policy.routes == second_policy.routes
    assert {key: value.candidate_id if value else None for key, value in first_policy.active.items()} == {
        key: value.candidate_id if value else None for key, value in second_policy.active.items()
    }


def test_measured_route_cost_does_not_add_dwell_twice(tmp_path: Path):
    cfg = make_config("measured_cml", require_frame=True, require_ack=False, dwell_s=9.0)
    device_id = cfg.active_devices[0].id
    model = {
        "schema_version": 1,
        "target_definition": "first related command issued to execution-confirming FRAME_CONFIRM",
        "routing_cost_rule": "use the calibrated upper prediction directly; do not add dwell again",
        "models": {
            device_id: {
                "coefficients": {
                    "intercept": 1.0,
                    "yaw_positive_deg": 0.0,
                    "yaw_negative_deg": 0.0,
                    "pitch_up_deg": 0.0,
                    "pitch_down_deg": 0.0,
                },
                "one_sided_padding_s": 0.5,
            }
        },
    }
    model_path = tmp_path / "execution_model.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    cfg.policy["execution_model"] = {"model_path": str(model_path)}
    policy = MyPolicy(cfg)
    upper, uncertainty = policy._measured_edge(device_id, (0.0, 0.0), (20.0, 10.0))
    assert upper == pytest.approx(1.5)
    assert uncertainty == pytest.approx(0.5)


def test_measured_route_cost_uses_angle_regime_padding(tmp_path: Path):
    cfg = make_config("uac_robust_repair_v2", require_frame=True, require_ack=True)
    device_id = cfg.active_devices[0].id
    model = {
        "schema_version": 2,
        "target_definition": "command_issued_to_frame_confirm",
        "routing_cost_rule": "use the calibrated upper prediction directly; do not add dwell again",
        "models": {
            device_id: {
                "coefficients": {
                    "intercept": 1.0, "yaw_positive_deg": 0.0,
                    "yaw_negative_deg": 0.0, "pitch_up_deg": 0.0,
                    "pitch_down_deg": 0.0,
                },
                "one_sided_padding_s": 3.0,
                "angle_regime_thresholds_deg": [3.0, 12.0],
                "one_sided_padding_s_by_regime": {
                    "small": 0.1, "medium": 0.2, "large": 0.3,
                },
            }
        },
    }
    model_path = tmp_path / "execution_model_v2.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    cfg.policy["execution_model"] = {"model_path": str(model_path)}
    policy = MyPolicy(cfg)

    assert policy._measured_edge(device_id, (0.0, 0.0), (3.0, 0.0)) == pytest.approx((1.1, 0.1))
    assert policy._measured_edge(device_id, (0.0, 0.0), (8.0, 0.0)) == pytest.approx((1.2, 0.2))
    assert policy._measured_edge(device_id, (179.0, 0.0), (-179.0, 0.0)) == pytest.approx((1.1, 0.1))
    assert policy._measured_edge(device_id, (0.0, 0.0), (20.0, 0.0)) == pytest.approx((1.3, 0.3))


def test_measured_route_cost_uses_confirmation_bound_for_stationary_view(tmp_path: Path):
    cfg = make_config("uac_robust_repair_v2", require_frame=True, require_ack=True)
    cfg.policy["execution_confirmation"]["stationary_upper_s"] = 1.8
    device_id = cfg.active_devices[0].id
    model = {
        "schema_version": 2,
        "target_definition": "command_issued_to_frame_confirm",
        "routing_cost_rule": "use the calibrated upper prediction directly; do not add dwell again",
        "models": {
            device_id: {
                "coefficients": {
                    "intercept": 10.0, "yaw_positive_deg": 1.0,
                    "yaw_negative_deg": 1.0, "pitch_up_deg": 1.0,
                    "pitch_down_deg": 1.0,
                },
                "one_sided_padding_s": 2.0,
                "angle_regime_thresholds_deg": [3.0, 12.0],
                "one_sided_padding_s_by_regime": {
                    "small": 2.0, "medium": 3.0, "large": 4.0,
                },
            }
        },
    }
    model_path = tmp_path / "execution_model_stationary.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    cfg.policy["execution_model"] = {"model_path": str(model_path)}
    policy = MyPolicy(cfg)

    assert policy._measured_edge(device_id, (0.0, 0.0), (0.5, 0.5)) == pytest.approx((1.8, 0.0))
    assert policy._model_padding(device_id, (0.0, 0.0), (0.5, 0.5)) == pytest.approx(0.0)
    assert policy._measured_edge(device_id, (0.0, 0.0), (2.0, 0.0))[0] > 10.0


def test_prediction_bound_exceedance_is_diagnostic_not_fault():
    cfg = make_config("uac_robust_repair_v2", require_frame=True, require_ack=True)
    cfg.policy["prediction_overrun_grace_s"] = 0.0
    policy = MyPolicy(cfg)
    first, _ = first_actions(policy)
    device_id = policy.device_ids[0]
    task = policy.active[device_id]
    assert task is not None
    task.command_started_t = 100.0
    task.predicted_s = 0.01

    decision = policy.decide(context(
        100.2, pose_at_current_targets(policy, 100.2), step=1,
        results={device_id: acknowledged(first.actions[device_id], device_id, 100.2)},
    ))
    event = next(event for event in decision.diagnostics["events"]
                 if event["type"] == "prediction_bound_exceeded")
    assert event["fault"] is False
    assert event["replan_requested"] is False
    assert event["exceedance_s"] == pytest.approx(0.19)
    assert device_id not in policy.faults
    assert policy.pending_replan is False

    repeated = policy.decide(context(
        100.21, pose_at_current_targets(policy, 100.21), step=2,
        results={device_id: acknowledged(first.actions[device_id], device_id, 100.21)},
    ))
    assert not any(event["type"] == "prediction_bound_exceeded"
                   for event in repeated.diagnostics["events"])


def test_insertion_delta_is_explicitly_non_negative():
    policy = MyPolicy(make_config("open_cml", require_frame=False, require_ack=False))
    candidates = list(policy.candidates_by_device[policy.device_ids[0]])
    assert candidates
    delta, spread = policy._insertion_delta(
        policy.device_ids[0], [], 0, candidates[0], (0.0, 0.0), measured=False)
    assert delta >= 0.0
    assert spread >= 0.0


def test_mapping_evaluation_grid_is_the_frozen_task_set():
    cfg = make_config("ea_cml", require_frame=False, require_ack=False)
    cfg.policy.pop("evaluation_points_m")
    cfg.policy["roi"] = {"x": [10.0, 14.0], "y": [-2.0, 2.0], "z": [1.0, 3.0]}
    cfg.policy["evaluation_grid"] = {
        "shape": [2, 2, 1], "cell_count": 4,
        "role": "evaluation_cells", "construction": "uniform_roi_cell_centers",
    }
    cfg.policy["scan_grid"] = [2, 2, 1]
    policy = MyPolicy(cfg)
    decision, _ = first_actions(policy)
    assert len(policy.cells) == 4
    assert {cell.point_m[0] for cell in policy.cells} == {11.0, 13.0}
    assert {cell.point_m[1] for cell in policy.cells} == {-1.0, 1.0}
    assert {cell.point_m[2] for cell in policy.cells} == {2.0}
    assert decision.diagnostics["evaluation_cell_count"] == 4
    assert decision.diagnostics["reachable_ratio"] == pytest.approx(
        decision.diagnostics["reachable_count"] / 4
    )


def test_evaluation_grid_declared_count_must_match_shape():
    cfg = make_config(require_frame=False, require_ack=False)
    cfg.policy.pop("evaluation_points_m")
    cfg.policy["roi"] = {"x": [10.0, 14.0], "y": [-2.0, 2.0], "z": [1.0, 3.0]}
    cfg.policy["evaluation_grid"] = {"shape": [2, 2, 1], "cell_count": 5}
    with pytest.raises(ValueError, match="does not equal product"):
        MyPolicy(cfg)


def test_frozen_evaluation_grid_rejects_any_unreachable_cell():
    cfg = make_config(require_frame=False, require_ack=False)
    cfg.policy.pop("evaluation_points_m")
    cfg.policy["roi"] = {"x": [-14.0, -10.0], "y": [-1.0, 1.0], "z": [0.5, 1.5]}
    cfg.policy["evaluation_grid"] = {
        "shape": [2, 1, 1], "cell_count": 2,
        "role": "evaluation_cells", "construction": "uniform_roi_cell_centers",
    }
    cfg.policy["scan_grid"] = [2, 1, 1]
    with pytest.raises(ValueError, match="reachable_count=.*evaluation_cell_count"):
        MyPolicy(cfg)


def test_v2_nominal_tail_guard_preserves_robust_guarantees():
    policy = MyPolicy(make_config(
        "uac_robust_repair_v2", devices=2, require_frame=True, require_ack=True
    ))
    first_actions(policy)
    assert all(candidate.coverage <= candidate.nominal_coverage
               for candidate in policy.candidates.values())
    audit = policy.last_plan_audit
    assert audit["uac_non_regression_verified"] is True
    assert audit["t100_non_regression_verified"] is True
    assert audit["opportunistic_tail_nonregression_verified"] is True
    assert (audit["opportunistic_final_objective"]["t100_s"] <=
            audit["opportunistic_tail_cap_s"] + policy.objective_epsilon_s)


def test_initial_pose_box_uses_worst_corner_and_fixed_home_reference():
    cfg = make_config(
        "uac_robust_repair_v2", devices=2, require_frame=True, require_ack=True
    )
    cfg.policy["robust_objective"] = {
        "initial_pose_box": {
            "enabled": True, "yaw_bound_deg": 1.0, "pitch_bound_deg": 1.0,
        }
    }
    policy = MyPolicy(cfg)
    device_id = policy.device_ids[0]
    home = (policy.devices[device_id].initial_yaw_deg,
            policy.devices[device_id].initial_pitch_deg)
    target = (2.0, 1.0)
    policy._planning_with_initial_pose_box = True
    robust = policy._planning_edge(device_id, home, target, True)
    corners = [
        policy._edge(device_id, (home[0] + dyaw, home[1] + dpitch), target, True)
        for dyaw in (-1.0, 1.0) for dpitch in (-1.0, 1.0)
    ]
    policy._planning_with_initial_pose_box = False
    assert robust[0] == pytest.approx(max(value[0] for value in corners))
    assert robust[1] == pytest.approx(max(value[1] for value in corners))

    varied = {
        policy.device_ids[0]: state(policy.device_ids[0], 100.0, 0.8, -0.6),
        policy.device_ids[1]: state(policy.device_ids[1], 100.0, -0.7, 0.9),
    }
    assert policy._start_poses(context(100.0, varied), configured_home=True) == {
        key: (value.initial_yaw_deg, value.initial_pitch_deg)
        for key, value in policy.devices.items()
    }


def test_configured_confirmation_delay_is_event_clock_only():
    cfg = make_config(require_frame=False, require_ack=False, settle_s=0.0, dwell_s=0.0)
    cfg.policy["fault_injection"] = {
        "enabled": True,
        "kind": "confirmation_delay",
        "implementation": "policy_event_clock_only",
        "delay_s_by_device_slot": [0.20],
        "never_block_watchdog": True,
    }
    policy = MyPolicy(cfg)
    first, _ = first_actions(policy)
    device_id = policy.device_ids[0]
    assert first.diagnostics["confirmed_count"] == 0

    for step, t in enumerate((100.01, 100.02), start=1):
        policy.decide(context(t, pose_at_current_targets(policy, t), step=step))
    gated = policy.decide(context(100.03, pose_at_current_targets(policy, 100.03), step=3))
    assert gated.diagnostics["confirmed_count"] == 0
    assert any(event["type"] == "injection_started" for event in gated.diagnostics["events"])

    completed = policy.decide(context(100.24, pose_at_current_targets(policy, 100.24), step=4))
    assert completed.diagnostics["confirmed_count"] > 0
    assert any(event["type"] == "injection_ended" for event in completed.diagnostics["events"])


def test_configured_logical_pause_masks_only_the_selected_device():
    cfg = make_config(devices=2, require_frame=False, require_ack=False)
    cfg.policy["fault_injection"] = {
        "enabled": True,
        "kind": "logical_availability_pause",
        "implementation": "policy_availability_mask",
        "device_slot": 0,
        "start_after_mission_s": 0.04,
        "duration_s": 0.20,
        "last_command_expires_by_ttl": True,
        "no_sdk_fault_injection": True,
    }
    policy = MyPolicy(cfg)
    first, _ = first_actions(policy)
    paused_id, healthy_id = policy.device_ids
    assert paused_id in first.actions and healthy_id in first.actions

    paused = policy.decide(context(100.05, pose_at_current_targets(policy, 100.05), step=1))
    assert paused_id not in paused.actions
    assert healthy_id in paused.actions
    assert any(event["type"] == "injection_started" and event["device_id"] == paused_id
               for event in paused.diagnostics["events"])

    recovered = policy.decide(context(100.30, pose_at_current_targets(policy, 100.30), step=2))
    assert any(event["type"] == "injection_ended" and event["device_id"] == paused_id
               for event in recovered.diagnostics["events"])


def test_commissioning_inline_sequence_uses_registered_transition_ids():
    cfg = make_config("commissioning", require_frame=False, require_ack=False,
                      settle_s=0.0, dwell_s=0.0)
    cfg.policy["commissioning"] = {
        "commissioning_id": "commission-test",
        "transition_ids": ["transition-001", "transition-002"],
        "transitions": [
            {
                "commissioning_id": "commission-test", "device_id": "cam_1",
                "step": 1, "transition_id": "transition-001", "split": "train",
                "planned_from_yaw_deg": 0.0, "planned_from_pitch_deg": 0.0,
                "planned_to_yaw_deg": 10.0, "planned_to_pitch_deg": 0.0,
            },
            {
                "commissioning_id": "commission-test", "device_id": "cam_1",
                "step": 2, "transition_id": "transition-002", "split": "test",
                "planned_from_yaw_deg": 10.0, "planned_from_pitch_deg": 0.0,
                "planned_to_yaw_deg": 0.0, "planned_to_pitch_deg": 0.0,
            },
        ],
    }
    policy = MyPolicy(cfg)
    assert tuple(row.transition_id for row in policy.commissioning_rows) == (
        "transition-001", "transition-002"
    )
    route = policy.commissioning_routes_template["cam_1"]
    assert route == (
        "commissioning:preposition:transition-001",
        "commissioning:sample:transition-001",
        "commissioning:sample:transition-002",
    )

