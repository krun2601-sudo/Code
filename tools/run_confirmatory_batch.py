#!/usr/bin/env python3
"""Run the frozen real-ZR10 confirmatory plan with per-run integrity gates.

Every measured run is preceded by concurrent homing, a synchronized live
snapshot, the experiment preflight, and creation of a hash-bound run context.
The platform recorder is closed before the run context is copied into the
session.  The same strict analysis gate used for the paper is then applied
immediately; the batch stops on the first failed gate and is safely resumable.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import shutil
import subprocess
import sys
import time
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# When this file is launched by path, Python exposes ``tools/`` but not its
# parent.  The platform imports the frozen custom policy as
# ``algorithms.user_policy``, so make the project root explicit before any
# platform/runtime import occurs.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analyze_runs import analyze_one, load_plan
from capture_preflight_snapshot import _main as capture_snapshot
from common import sha256_file, write_json
from home_all import main as home_all
from zr10lab.config import load_config
from zr10lab.runtime import run_hardware


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()


def atomic_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    write_json(temporary, value)
    temporary.replace(path)


def run_tool(command: list[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        command, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        tail = completed.stdout[-2000:].replace("\n", " | ")
        raise RuntimeError(
            f"tool failed with code {completed.returncode}: {' '.join(command)}; tail={tail}"
        )


def load_state(path: Path, plan_id: str) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema": "zr10-confirmatory-batch-v1",
            "plan_id": plan_id,
            "created_utc": utc_now(),
            "updated_utc": utc_now(),
            "runs": {},
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("plan_id") != plan_id:
        raise ValueError("existing batch state belongs to a different plan")
    return value


def safe_metric_summary(metric: dict[str, Any]) -> dict[str, Any]:
    def clean(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    return {
        key: clean(metric.get(key)) for key in (
            "run_id", "plan_run_id", "method", "condition", "mission_duration_s",
            "t95_s", "t95_reached", "t100_s", "t100_reached", "uac_h_s",
            "coverage_final", "mission_success", "command_count",
            "ack_failure_count", "command_error_count", "unusable_frame_count",
            "deadline_miss_rate", "fault_count", "replan_count", "source_dir",
        )
    }


def homing_args(config: Path, output: Path, timeout_s: float) -> Namespace:
    return Namespace(
        config=str(config), output=output, connect_timeout_s=10.0,
        timeout_s=timeout_s, zoom=10.0, zoom_tolerance=0.05,
        initial_yaw_limit_deg=100.0, initial_pitch_limit_deg=30.0,
        abort_yaw_deg=105.0, abort_pitch_deg=35.0,
        pose_tolerance_deg=0.7, final_tolerance_deg=1.0,
        max_rate_dps=0.8, settle_hold_s=0.5, command_ttl_s=0.35,
        interval_s=0.1, post_stop_s=0.5,
    )


def snapshot_args(config: Path, output: Path) -> Namespace:
    return Namespace(
        config=str(config), output=output, connect_timeout_s=10.0,
        frame_timeout_s=20.0, max_snapshot_frame_age_s=0.35,
        post_stop_s=0.05, zoom=10.0,
    )


async def run_one(
    row: dict[str, Any], plan_path: Path, control_root: Path, runs_root: Path,
    kit_root: Path, home_timeout_s: float,
) -> tuple[Path, dict[str, Any]]:
    plan_run_id = str(row["plan_run_id"])
    config = Path(str(row["overlay_path"])).resolve()
    work = control_root / plan_run_id
    work.mkdir(parents=True, exist_ok=True)
    console = work / "console.log"
    snapshot = work / "live_snapshot.json"
    preflight = work / "preflight.json"
    context = work / "run_context.json"

    with console.open("a", encoding="utf-8") as stream, \
            contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        code = await home_all(homing_args(config, work / "home_before.json", home_timeout_s))
        if code != 0:
            raise RuntimeError("pre-run home gate failed")
        code = await capture_snapshot(snapshot_args(config, snapshot))
        if code != 0:
            raise RuntimeError("live snapshot gate failed")

    python = sys.executable
    tools = Path(__file__).resolve().parent
    run_tool([
        python, "-B", str(tools / "preflight.py"),
        "--config", str(config), "--snapshot", str(snapshot),
        "--phase", "experiment", "--kit-root", str(kit_root),
        "--output", str(preflight), "--require-live",
    ], work / "preflight_console.log")
    run_tool([
        python, "-B", str(tools / "prepare_run_context.py"),
        "--plan", str(plan_path), "--plan-run-id", plan_run_id,
        "--preflight", str(preflight), "--output", str(context),
        "--operator-arm-authorized",
    ], work / "context_console.log")

    cfg = load_config(config)
    with console.open("a", encoding="utf-8") as stream, \
            contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        run_dir = Path(await run_hardware(cfg, output=str(runs_root), armed=True)).resolve()
    shutil.copy2(context, run_dir / "run_context.json")
    write_json(work / "last_run_dir.json", {"run_dir": str(run_dir), "created_utc": utc_now()})
    metric = analyze_one(run_dir, row)
    write_json(work / "immediate_metrics.json", safe_metric_summary(metric))
    return run_dir, metric


async def safe_final_home(config: Path, control_root: Path, timeout_s: float) -> dict[str, Any]:
    output = control_root / "final_home.json"
    log = control_root / "final_home_console.log"
    try:
        with log.open("a", encoding="utf-8") as stream, \
                contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            code = await home_all(homing_args(config, output, timeout_s))
        return {"attempted": True, "pass": code == 0, "report": str(output)}
    except Exception as exc:  # final-home failure must remain visible in state
        return {"attempted": True, "pass": False, "report": str(output),
                "error": f"{type(exc).__name__}: {exc}"}


async def main(args: argparse.Namespace) -> int:
    plan_path = args.plan.resolve()
    plan, meta = load_plan(plan_path)
    for relative, expected in meta.get("frozen_software_sha256", {}).items():
        artifact = (PROJECT_ROOT / str(relative)).resolve()
        if not artifact.is_relative_to(PROJECT_ROOT):
            raise RuntimeError(f"frozen software path escapes project root: {relative}")
        if not artifact.is_file() or sha256_file(artifact) != str(expected):
            raise RuntimeError(f"frozen software hash mismatch before hardware use: {relative}")
    ordered = sorted(plan.values(), key=lambda row: int(row["run_index"]))
    selected = [row for row in ordered if int(row["run_index"]) >= args.start_index]
    if args.max_runs is not None:
        selected = selected[:args.max_runs]
    control_root = args.control_root.resolve()
    runs_root = args.runs_root.resolve()
    control_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)
    state_path = control_root / "batch_state.json"
    journal = control_root / "journal.jsonl"
    state = load_state(state_path, str(meta["plan_id"]))
    state.setdefault("block_failure_count", {})
    state.setdefault("excluded_run_dirs", {})
    if state.get("investigation_required"):
        raise RuntimeError(
            "batch state requires investigation after two failed block attempts; "
            "create a new reviewed control root after resolving the cause"
        )
    state["plan_file"] = str(plan_path)
    state["run_root"] = str(runs_root)
    state["selected_start_index"] = args.start_index
    state["selected_max_runs"] = args.max_runs
    atomic_state(state_path, state)

    logging.getLogger().setLevel(logging.WARNING)
    last_config = Path(str(selected[-1]["overlay_path"])).resolve() if selected else None
    batch_error: str | None = None
    try:
        for ordinal, row in enumerate(selected, 1):
            plan_run_id = str(row["plan_run_id"])
            previous = state["runs"].get(plan_run_id, {})
            if args.resume and previous.get("status") == "accepted":
                print(f"SKIP {plan_run_id} already accepted", flush=True)
                continue
            started = time.monotonic()
            print(
                f"START {plan_run_id} index={row['run_index']}/{len(ordered)} "
                f"method={row['method']} condition={row['condition']}", flush=True,
            )
            append_jsonl(journal, {"utc": utc_now(), "event": "run_started",
                                   "plan_run_id": plan_run_id, "row": row})
            state["runs"][plan_run_id] = {
                "status": "running", "started_utc": utc_now(),
                "run_index": int(row["run_index"]), "method": row["method"],
                "condition": row["condition"],
            }
            state["updated_utc"] = utc_now()
            atomic_state(state_path, state)
            write_json(control_root / plan_run_id / "last_run_dir.json", {
                "run_dir": "", "attempt_started_utc": utc_now(),
            })
            try:
                run_dir, metric = await run_one(
                    row, plan_path, control_root, runs_root,
                    args.kit_root.resolve(), args.home_timeout_s,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                block_key = str(int(row["block"]))
                state["block_failure_count"][block_key] = int(
                    state["block_failure_count"].get(block_key, 0)
                ) + 1
                last_run_path = control_root / plan_run_id / "last_run_dir.json"
                if last_run_path.is_file():
                    try:
                        failed_run_dir = str(json.loads(
                            last_run_path.read_text(encoding="utf-8")
                        )["run_dir"])
                        if failed_run_dir:
                            state["excluded_run_dirs"][failed_run_dir] = (
                                f"failed gate in block {block_key}: {error}"
                            )
                    except (OSError, json.JSONDecodeError, KeyError):
                        pass
                invalidated_ids: list[str] = []
                for candidate in ordered:
                    if int(candidate["block"]) != int(row["block"]):
                        continue
                    candidate_id = str(candidate["plan_run_id"])
                    record = state["runs"].get(candidate_id, {})
                    if record.get("status") == "accepted":
                        record["status"] = "invalidated_block_retry"
                        record["invalidated_utc"] = utc_now()
                        record["invalidation_reason"] = (
                            f"another run in block {block_key} failed; full block must be repeated"
                        )
                        if record.get("run_dir"):
                            state["excluded_run_dirs"][str(record["run_dir"])] = record[
                                "invalidation_reason"
                            ]
                        invalidated_ids.append(candidate_id)
                state["runs"][plan_run_id].update({
                    "status": "failed", "finished_utc": utc_now(), "error": error,
                    "elapsed_s": time.monotonic() - started,
                })
                if state["block_failure_count"][block_key] >= 2:
                    state["investigation_required"] = True
                state["updated_utc"] = utc_now()
                atomic_state(state_path, state)
                append_jsonl(journal, {"utc": utc_now(), "event": "run_failed",
                                       "plan_run_id": plan_run_id, "error": error,
                                       "block": int(row["block"]),
                                       "invalidated_plan_run_ids": invalidated_ids,
                                       "investigation_required": state.get("investigation_required", False)})
                raise
            summary = safe_metric_summary(metric)
            state["runs"][plan_run_id].update({
                "status": "accepted", "finished_utc": utc_now(),
                "elapsed_s": time.monotonic() - started,
                "run_dir": str(run_dir), "metrics": summary,
            })
            state["updated_utc"] = utc_now()
            atomic_state(state_path, state)
            append_jsonl(journal, {"utc": utc_now(), "event": "run_accepted",
                                   "plan_run_id": plan_run_id, "run_dir": str(run_dir),
                                   "metrics": summary})
            print(
                f"ACCEPT {plan_run_id} coverage={metric['coverage_final']:.6f} "
                f"T95={metric['t95_s']} T100={metric['t100_s']} "
                f"faults={metric['fault_count']} elapsed={time.monotonic()-started:.1f}s",
                flush=True,
            )
            cooldown = float(row.get("cooldown_s", args.cooldown_s))
            if ordinal < len(selected) and cooldown > 0:
                await asyncio.sleep(cooldown)
    except Exception as exc:
        batch_error = f"{type(exc).__name__}: {exc}"
        print(f"BATCH_STOP {batch_error}", flush=True)
    finally:
        if last_config is not None:
            final_home = await safe_final_home(last_config, control_root, args.home_timeout_s)
            state["final_home"] = final_home
            print(f"FINAL_HOME pass={final_home.get('pass')}", flush=True)
        state["updated_utc"] = utc_now()
        state["batch_error"] = batch_error
        state["accepted_count"] = sum(
            item.get("status") == "accepted" for item in state["runs"].values()
        )
        atomic_state(state_path, state)
    print(json.dumps({
        "state": str(state_path), "accepted_count": state["accepted_count"],
        "selected_count": len(selected), "batch_error": batch_error,
        "final_home": state.get("final_home"),
    }, ensure_ascii=False, indent=2), flush=True)
    return 0 if batch_error is None and state.get("final_home", {}).get("pass") else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--control-root", required=True, type=Path)
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--kit-root", required=True, type=Path)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--max-runs", type=int)
    parser.add_argument("--cooldown-s", type=float, default=10.0)
    parser.add_argument("--home-timeout-s", type=float, default=45.0)
    parser.add_argument("--resume", action="store_true")
    parsed = parser.parse_args()
    if parsed.start_index < 1:
        parser.error("--start-index must be positive")
    if parsed.max_runs is not None and parsed.max_runs < 1:
        parser.error("--max-runs must be positive")
    raise SystemExit(asyncio.run(main(parsed)))
