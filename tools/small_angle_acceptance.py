"""Production-path small-angle ZR10 motion acceptance with automatic stop.

The selected units move one at a time from the private local datum to a small
positive yaw target and back to zero.  The production HardwareFleet supplies
telemetry freshness, ACK handling, action leases, independent watchdogs, and
repeated shutdown stops.  Any envelope, timeout, telemetry, ACK, mode, zoom,
or frame failure aborts the sequence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from read_only_hardware_audit import _read_frame
from zr10lab.config import load_config
from zr10lab.hardware import HardwareFleet
from zr10lab.models import Action


async def _query_modes(cfg: Any) -> dict[str, str]:
    from siyi_sdk import connect_udp

    async def one(device: Any) -> tuple[str, str]:
        client = None
        try:
            client = await asyncio.wait_for(
                connect_udp(device.ip, device.port, timeout=0.8, max_retries=0, auto_reconnect=False),
                1.5,
            )
            value = await asyncio.wait_for(client.get_gimbal_mode(), 1.0)
            return device.id, getattr(value, "name", str(value))
        finally:
            if client is not None:
                await asyncio.wait_for(client.close(), 1.0)

    return dict(await asyncio.gather(*(one(device) for device in cfg.active_devices)))


def _state_row(value: Any) -> dict[str, Any]:
    row = asdict(value)
    row["sampled_utc_s"] = time.time()
    return row


async def _wait_ready(fleet: HardwareFleet, args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + args.connect_timeout_s
    while time.monotonic() < deadline:
        states = fleet.states()
        ready = all(
            state.connected
            and state.raw.get("zoom_known") is True
            and abs(state.zoom - args.zoom) <= args.zoom_tolerance
            for state in states.values()
        )
        if ready:
            return states
        await asyncio.sleep(0.1)
    states = fleet.states()
    missing = {
        key: {"connected": state.connected, "zoom": state.zoom, "zoom_known": state.raw.get("zoom_known")}
        for key, state in states.items()
        if not state.connected or state.raw.get("zoom_known") is not True or abs(state.zoom - args.zoom) > args.zoom_tolerance
    }
    raise TimeoutError(f"hardware fleet did not become ready: {missing}")


async def _drive(
    fleet: HardwareFleet,
    device_id: str,
    target_yaw: float,
    target_pitch: float,
    phase: str,
    args: argparse.Namespace,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    started = time.monotonic()
    settle_started: float | None = None
    phase_result_start = len(results)
    samples: list[dict[str, Any]] = []
    while True:
        now = time.monotonic()
        state = fleet.states()[device_id]
        samples.append(_state_row(state))
        if not state.connected:
            raise RuntimeError(f"{device_id} telemetry became stale during {phase}")
        if not all(math.isfinite(value) for value in (state.yaw_deg, state.pitch_deg, state.yaw_rate_dps, state.pitch_rate_dps)):
            raise RuntimeError(f"{device_id} non-finite telemetry during {phase}")
        if abs(state.yaw_deg) > args.abort_yaw_deg or abs(state.pitch_deg) > args.abort_pitch_deg:
            raise RuntimeError(
                f"{device_id} left acceptance envelope during {phase}: yaw={state.yaw_deg}, pitch={state.pitch_deg}"
            )
        if now - started > args.phase_timeout_s:
            raise TimeoutError(
                f"{device_id} did not reach {target_yaw:.2f}/{target_pitch:.2f} deg during {phase}; "
                f"last={state.yaw_deg:.2f}/{state.pitch_deg:.2f}"
            )
        action = Action(
            device_id,
            yaw_deg=target_yaw,
            pitch_deg=target_pitch,
            reason=f"small_angle_acceptance:{phase}",
            issued_t=now,
            ttl_s=args.command_ttl_s,
        )
        fleet.submit({device_id: action})
        pose_ok = (
            abs(state.yaw_deg - target_yaw) <= args.pose_tolerance_deg
            and abs(state.pitch_deg - target_pitch) <= args.pose_tolerance_deg
        )
        rate_ok = abs(state.yaw_rate_dps) <= args.max_settle_rate_dps and abs(state.pitch_rate_dps) <= args.max_settle_rate_dps
        if pose_ok and rate_ok:
            settle_started = settle_started or now
            if now - settle_started >= args.settle_hold_s:
                break
        else:
            settle_started = None
        await asyncio.sleep(args.control_interval_s)

    cancel_started = time.monotonic()
    await fleet.devices[device_id].cancel_pending(f"{phase}_complete")
    await asyncio.sleep(args.post_stop_s)
    final_state = fleet.states()[device_id]
    if not final_state.connected:
        raise RuntimeError(f"{device_id} telemetry became stale after stopping during {phase}")
    final_values = (
        final_state.yaw_deg,
        final_state.pitch_deg,
        final_state.yaw_rate_dps,
        final_state.pitch_rate_dps,
    )
    if not all(math.isfinite(value) for value in final_values):
        raise RuntimeError(f"{device_id} non-finite telemetry after stopping during {phase}")
    final_yaw_error = abs(final_state.yaw_deg - target_yaw)
    final_pitch_error = abs(final_state.pitch_deg - target_pitch)
    if (
        final_yaw_error > args.final_pose_tolerance_deg
        or final_pitch_error > args.final_pose_tolerance_deg
    ):
        raise RuntimeError(
            f"{device_id} post-stop pose gate failed during {phase}: "
            f"error={final_yaw_error:.2f}/{final_pitch_error:.2f} deg, "
            f"limit={args.final_pose_tolerance_deg:.2f} deg"
        )
    phase_results = results[phase_result_start:]
    ack_count = sum(
        1
        for item in phase_results
        if item.get("device_id") == device_id and item.get("ack") is True and item.get("status") == "acknowledged"
    )
    expected_cancellations = [
        item
        for item in phase_results
        if item.get("device_id") == device_id
        and item.get("status") == "watchdog_interrupted"
        and float(item.get("t", -math.inf)) >= cancel_started
    ]
    errors = [
        item
        for item in phase_results
        if item.get("device_id") == device_id
        and (
            item.get("status") == "error"
            or (
                item.get("status") == "watchdog_interrupted"
                and float(item.get("t", -math.inf)) < cancel_started
            )
        )
    ]
    if ack_count < 1 or errors:
        raise RuntimeError(f"{device_id} {phase} ACK/result gate failed: ack_count={ack_count}, errors={len(errors)}")
    return {
        "phase": phase,
        "target": {"yaw_deg": target_yaw, "pitch_deg": target_pitch},
        "elapsed_s": time.monotonic() - started,
        "acknowledged_command_count": ack_count,
        "expected_cancelled_pending_count": len(expected_cancellations),
        "samples": samples,
        "post_stop_pose_error_deg": {
            "yaw": final_yaw_error,
            "pitch": final_pitch_error,
            "limit": args.final_pose_tolerance_deg,
        },
        "final_state": _state_row(final_state),
    }


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    device_map = {device.id: device for device in cfg.active_devices}
    selected = args.devices or list(device_map)
    unknown = [key for key in selected if key not in device_map]
    if unknown:
        raise ValueError(f"unknown devices: {unknown}")
    output = args.output.resolve()
    frame_root = output.parent / f"{output.stem}_frames"
    events: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    telemetry: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    frames: list[dict[str, Any]] = []
    error: str | None = None
    modes_before = await _query_modes(cfg)
    if any(modes_before.get(key) != "FOLLOW" for key in selected):
        error = f"selected units are not in FOLLOW mode: {modes_before}"

    def event_sink(kind: str, payload: dict[str, Any]) -> None:
        if kind != "telemetry":
            events.append({"kind": kind, **payload})

    def result_sink(value: Any) -> None:
        results.append(asdict(value))

    def telemetry_sink(value: Any) -> None:
        telemetry.append(_state_row(value))

    fleet = HardwareFleet(cfg, event_sink=event_sink, result_sink=result_sink, telemetry_sink=telemetry_sink)
    initial_states: dict[str, Any] = {}
    final_states: dict[str, Any] = {}
    if error is None:
        try:
            await fleet.start()
            ready = await _wait_ready(fleet, args)
            initial_states = {key: _state_row(value) for key, value in ready.items()}
            unsafe = {
                key: (value.yaw_deg, value.pitch_deg)
                for key, value in ready.items()
                if abs(value.yaw_deg) > args.initial_envelope_deg or abs(value.pitch_deg) > args.initial_envelope_deg
            }
            if unsafe:
                raise RuntimeError(f"local datum no longer fresh enough for acceptance: {unsafe}")
            for device_id in selected:
                before_frame = await asyncio.to_thread(
                    _read_frame, device_map[device_id], frame_root / f"{device_id}_before", None, args.frame_reads
                )
                before_frame["phase"] = "before"
                frames.append(before_frame)
                if not before_frame.get("ok") or not before_frame.get("resolution_ok"):
                    raise RuntimeError(f"{device_id} pre-motion frame gate failed: {before_frame}")

                outward = await _drive(
                    fleet,
                    device_id,
                    args.delta_yaw_deg,
                    args.delta_pitch_deg,
                    "outward",
                    args,
                    results,
                )
                phases.append({"device_id": device_id, **outward})
                target_frame = await asyncio.to_thread(
                    _read_frame, device_map[device_id], frame_root / f"{device_id}_target", None, args.frame_reads
                )
                target_frame["phase"] = "target"
                frames.append(target_frame)
                if not target_frame.get("ok") or not target_frame.get("resolution_ok"):
                    raise RuntimeError(f"{device_id} target frame gate failed: {target_frame}")

                returned = await _drive(fleet, device_id, 0.0, 0.0, "return", args, results)
                phases.append({"device_id": device_id, **returned})
                return_frame = await asyncio.to_thread(
                    _read_frame, device_map[device_id], frame_root / f"{device_id}_return", None, args.frame_reads
                )
                return_frame["phase"] = "return"
                frames.append(return_frame)
                if not return_frame.get("ok") or not return_frame.get("resolution_ok"):
                    raise RuntimeError(f"{device_id} return frame gate failed: {return_frame}")
            final_states = {key: _state_row(value) for key, value in fleet.states().items()}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                await fleet.close()
            except Exception as exc:
                close_error = f"{type(exc).__name__}: {exc}"
                error = f"{error}; close={close_error}" if error else f"close={close_error}"

    passed = error is None and len(phases) == 2 * len(selected) and all(item.get("ok") for item in frames)
    report = {
        "schema": "zr10-small-angle-acceptance-v2",
        "created_utc_s": time.time(),
        "config": str(Path(args.config).resolve()),
        "selected_devices": selected,
        "target_delta_yaw_deg": args.delta_yaw_deg,
        "target_delta_pitch_deg": args.delta_pitch_deg,
        "modes_before": modes_before,
        "initial_states": initial_states,
        "phases": phases,
        "frames": frames,
        "final_states": final_states,
        "command_results": results,
        "events": events,
        "telemetry": telemetry,
        "error": error,
        "pass": passed,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "schema": report["schema"],
        "selected_devices": selected,
        "target_delta_yaw_deg": args.delta_yaw_deg,
        "target_delta_pitch_deg": args.delta_pitch_deg,
        "modes_before": modes_before,
        "phase_summaries": [
            {
                "device_id": item["device_id"],
                "phase": item["phase"],
                "elapsed_s": item["elapsed_s"],
                "acknowledged_command_count": item["acknowledged_command_count"],
                "expected_cancelled_pending_count": item["expected_cancelled_pending_count"],
                "final_yaw_deg": item["final_state"]["yaw_deg"],
                "final_pitch_deg": item["final_state"]["pitch_deg"],
                "post_stop_pose_error_deg": item["post_stop_pose_error_deg"],
            }
            for item in phases
        ],
        "frame_count": len(frames),
        "error": error,
        "pass": passed,
    }, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--devices", nargs="*")
    parser.add_argument("--delta-yaw-deg", type=float, default=2.0)
    parser.add_argument("--delta-pitch-deg", type=float, default=0.0)
    parser.add_argument("--zoom", type=float, default=10.0)
    parser.add_argument("--zoom-tolerance", type=float, default=0.05)
    parser.add_argument("--connect-timeout-s", type=float, default=10.0)
    parser.add_argument("--initial-envelope-deg", type=float, default=2.0)
    parser.add_argument("--abort-yaw-deg", type=float, default=6.0)
    parser.add_argument("--abort-pitch-deg", type=float, default=4.0)
    parser.add_argument("--pose-tolerance-deg", type=float, default=0.5)
    parser.add_argument("--final-pose-tolerance-deg", type=float, default=1.0)
    parser.add_argument("--max-settle-rate-dps", type=float, default=0.8)
    parser.add_argument("--settle-hold-s", type=float, default=0.5)
    parser.add_argument("--phase-timeout-s", type=float, default=10.0)
    parser.add_argument("--command-ttl-s", type=float, default=0.35)
    parser.add_argument("--control-interval-s", type=float, default=0.1)
    parser.add_argument("--post-stop-s", type=float, default=0.35)
    parser.add_argument("--frame-reads", type=int, default=30)
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
