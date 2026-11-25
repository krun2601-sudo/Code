"""Issue the platform's audited zero-velocity/lock startup sequence to all ZR10s.

This sends no position target and no non-zero velocity.  It deliberately uses
the production HardwareFleet path so stop repetition, ACK accounting,
watchdog ordering, telemetry normalization, and shutdown behavior are the same
as an armed experiment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zr10lab.config import load_config
from zr10lab.hardware import HardwareFleet


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    events: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    def event_sink(kind: str, payload: dict[str, Any]) -> None:
        if kind != "telemetry":
            events.append({"kind": kind, **payload})

    def result_sink(value: Any) -> None:
        results.append(asdict(value))

    fleet = HardwareFleet(cfg, event_sink=event_sink, result_sink=result_sink)
    started = time.monotonic()
    snapshots: dict[str, Any] = {}
    error: str | None = None
    try:
        await fleet.start()
        deadline = time.monotonic() + args.connect_timeout_s
        while time.monotonic() < deadline:
            current = fleet.states()
            if all(item.connected for item in current.values()):
                break
            await asyncio.sleep(0.1)
        current = fleet.states()
        if not all(item.connected for item in current.values()):
            missing = [key for key, value in current.items() if not value.connected]
            raise TimeoutError(f"devices did not become telemetry-fresh: {missing}")
        await asyncio.gather(
            *(unit.cancel_pending("commissioning_zero_velocity_lock") for unit in fleet.devices.values())
        )
        await asyncio.sleep(args.settle_s)
        snapshots = {key: asdict(value) for key, value in fleet.states().items()}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await fleet.close()
        except Exception as exc:
            close_error = f"{type(exc).__name__}: {exc}"
            error = f"{error}; close={close_error}" if error else f"close={close_error}"

    device_ids = {device.id for device in cfg.active_devices}
    stop_events = [item for item in events if item.get("kind") == "stop"]
    acknowledged = {
        device_id: any(item.get("device_id") == device_id and item.get("ack") is True for item in stop_events)
        for device_id in device_ids
    }
    locked = {
        device_id: any(
            item.get("kind") == "mode_sent"
            and item.get("device_id") == device_id
            and item.get("mode") == "lock"
            for item in events
        )
        for device_id in device_ids
    }
    passed = (
        error is None
        and all(acknowledged.values())
        and all(locked.values())
        and len(snapshots) == len(device_ids)
        and all(item.get("connected") for item in snapshots.values())
    )
    report = {
        "schema": "zr10-safe-stop-all-v1",
        "created_utc_s": time.time(),
        "config": str(Path(args.config).resolve()),
        "nonzero_motion_commands_sent": False,
        "zero_velocity_stop_requested": True,
        "lock_mode_requested": True,
        "stop_acknowledged": acknowledged,
        "lock_mode_sent": locked,
        "snapshots_before_close": snapshots,
        "events": events,
        "command_results": results,
        "error": error,
        "elapsed_s": time.monotonic() - started,
        "pass": passed,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "schema", "nonzero_motion_commands_sent", "stop_acknowledged", "lock_mode_sent", "error", "elapsed_s", "pass"
    )}, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--connect-timeout-s", type=float, default=10.0)
    parser.add_argument("--settle-s", type=float, default=2.0)
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
