from __future__ import annotations

import time

from zr10lab.config import DeviceConfig, LabConfig
from zr10lab.models import PolicyContext, Telemetry

from algorithms.user_policy import MyPolicy


def _config() -> LabConfig:
    devices = [
        DeviceConfig(
            id=f"cam_{index + 1}",
            ip=f"127.0.0.{index + 11}",
            position_m=(0.0, -2.0 if index == 0 else 2.0, 0.0),
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
        )
        for index in range(2)
    ]
    return LabConfig(
        devices=devices,
        system={
            "rate_hz": 10, "duration_s": 60.0, "telemetry_stale_s": 0.5,
            "max_frame_age_s": 0.6, "action_ttl_s": 0.5,
        },
        policy={
            "method": "uac_robust_repair_v2",
            "evaluation_points_m": [
                [20.0, -1.0, 1.0], [20.0, 1.0, 1.0],
                [22.0, -1.5, 2.0], [22.0, 1.5, 2.0],
            ],
            "fixed_zoom": 1.0,
            "safe_envelope_deg": {
                "yaw": [-35.0, 35.0], "pitch": [-20.0, 20.0],
            },
            "planner_budget_ms": 100.0,
            "initial_planner_budget_ms": 100.0,
            "repair_planner_budget_ms": 40.0,
            "planner_deadline_guard_ms": 5.0,
            "planner_max_evaluations": 10000,
            "execution_confirmation": {
                "pose_tolerance_deg": 0.5,
                "max_rate_dps": 0.5,
                "settle_hold_s": 0.1,
                "min_dwell_s": 0.1,
                "stationary_upper_s": 0.5,
                "distinct_frames": 2,
                "frame_timeout_s": 0.3,
                "transition_timeout_s": 10.0,
                "require_ack": True,
                "require_frame": True,
            },
            "robust_objective": {
                "fault_recovery_bound_s": 8.0,
                "repair_witness_margin_s": 0.5,
                "repair_candidate_pool_per_device": 8,
                "repair_max_insertions": 2,
            },
        },
        action_space={"enabled": ["yaw_deg", "pitch_deg"]},
    )


def _context(policy: MyPolicy, t: float = 100.0) -> PolicyContext:
    states = {
        device_id: Telemetry(
            device_id=device_id,
            t=t,
            yaw_deg=float(device.initial_yaw_deg),
            pitch_deg=float(device.initial_pitch_deg),
            zoom=float(device.initial_zoom),
            raw={"zoom_known": True, "zoom_stable": True},
        )
        for device_id, device in policy.devices.items()
    }
    return PolicyContext(t=t, dt=0.1, step=0, devices=states)


def test_v2_local_search_never_exceeds_evaluation_cap() -> None:
    policy = MyPolicy(_config())
    policy.initial_local_search_max_evaluations = 7
    context = _context(policy)

    routes = policy._make_plan(context, set(policy.reachable))

    assert any(routes.values())
    assert policy.last_plan_audit["evaluations"] <= 7
    assert policy.last_plan_audit["evaluation_limit"] == 7
    assert policy.last_plan_audit["evaluation_limit_respected"] is True


def test_repair_witness_requires_confirmation_before_recovery_margin() -> None:
    policy = MyPolicy(_config())
    context = _context(policy)
    healthy_id, faulted_id = policy.device_ids[:2]
    candidate = next(
        candidate for candidate in policy.candidates_by_device[healthy_id]
        if candidate.coverage
    )
    cell = next(iter(candidate.coverage))
    routes = {device_id: [] for device_id in policy.device_ids}
    routes[healthy_id] = [candidate.candidate_id]
    starts = policy._start_poses(context)
    upper, _ = policy._planning_edge(
        healthy_id,
        starts[healthy_id],
        (candidate.yaw_deg, candidate.pitch_deg),
        True,
    )
    ownership = {faulted_id: {cell}}

    feasible_offsets = {device_id: 0.0 for device_id in policy.device_ids}
    feasible_offsets[faulted_id] = upper + policy.repair_witness_margin_s + 0.1
    feasible = policy._repair_witness(routes, starts, feasible_offsets, ownership)
    assert feasible["feasible_before_recovery"] is True

    infeasible_offsets = dict(feasible_offsets)
    infeasible_offsets[faulted_id] = max(0.0, upper - 0.1)
    infeasible = policy._repair_witness(routes, starts, infeasible_offsets, ownership)
    assert infeasible["feasible_before_recovery"] is False


def test_repair_search_obeys_evaluation_and_deadline_caps() -> None:
    policy = MyPolicy(_config())
    policy.repair_local_search_max_evaluations = 3
    context = _context(policy)
    starts = policy._start_poses(context)
    baseline = policy._plan_mag_complete(context, set(policy.reachable), starts)
    target = set(policy.reachable)
    objective = policy._plan_objective(baseline, starts, target)
    faulted_id = policy.device_ids[0]
    owned = set()
    for candidate_id in baseline[faulted_id]:
        owned.update(policy.candidates[candidate_id].coverage & target)
    offsets = {device_id: 0.0 for device_id in policy.device_ids}
    offsets[faulted_id] = 100.0
    policy.faults[faulted_id] = object()  # only membership is used by the search

    _, _, audit = policy._search_repair_insertions(
        baseline,
        objective,
        starts,
        offsets,
        target,
        owned,
        {faulted_id: owned},
        time.perf_counter() + 1.0,
    )

    assert audit["evaluations"] <= 3
    assert audit["evaluation_limit_respected"] is True

