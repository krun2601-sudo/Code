#!/usr/bin/env python3
"""Create a campaign-level integrity and hardware-health audit."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import canonical_json, read_csv, sha256_bytes, sha256_file, verify_manifest, write_json


def parse(value: str) -> dict[str, Any]:
    document = json.loads(value) if value else {}
    return document if isinstance(document, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    plan_document = json.loads(args.plan.read_text(encoding="utf-8"))
    plan_rows = {str(row["plan_run_id"]): row for row in plan_document["rows"]}
    state = json.loads(args.state.read_text(encoding="utf-8"))
    metrics = {row["plan_run_id"]: row for row in read_csv(args.analysis / "run_metrics_wide.csv")}
    run_rows: list[dict[str, Any]] = []
    total_bytes = 0
    health = Counter()
    startup_cycles: list[int] = []
    followups: list[float] = []
    versions: set[str] = set()
    manifest_failures: dict[str, list[str]] = {}
    for plan_run_id, planned in plan_rows.items():
        recorded = state.get("runs", {}).get(plan_run_id, {})
        run_dir = Path(str(recorded.get("run_dir", "")))
        if not run_dir.is_dir():
            run_rows.append({"plan_run_id": plan_run_id, "status": "missing"})
            continue
        problems = verify_manifest(run_dir)
        if problems:
            manifest_failures[plan_run_id] = problems
        run_bytes = sum(path.stat().st_size for path in run_dir.iterdir() if path.is_file())
        total_bytes += run_bytes
        cycles = read_csv(run_dir / "cycles.csv")
        waiting = 0
        first_mission_t = math.nan
        last_t = math.nan
        for row in cycles:
            last_t = float(row["t_s"])
            policy = parse(row.get("diagnostics_json", "")).get("policy", {})
            versions.add(str(policy.get("algorithm_version", "")))
            if policy.get("mission_state") == "STARTUP_WAIT":
                waiting += 1
            elif not math.isfinite(first_mission_t):
                first_mission_t = float(row["t_s"])
        followup = last_t - first_mission_t
        startup_cycles.append(waiting)
        followups.append(followup)
        metric = metrics.get(plan_run_id, {})
        t100_reached = str(metric.get("t100_reached", "")).strip().lower() in {"1", "true", "yes"}
        for key in ("ack_failure_count", "command_error_count", "unusable_frame_count"):
            health[key] += int(float(metric.get(key, 0) or 0))
        health["deadline_miss_count"] += round(
            float(metric.get("deadline_miss_rate", 0) or 0) * len(cycles)
        )
        run_rows.append({
            "plan_run_id": plan_run_id,
            "run_index": int(planned["run_index"]),
            "method": planned["method"],
            "condition": planned["condition"],
            "status": recorded.get("status"),
            "run_dir": str(run_dir),
            "bytes": run_bytes,
            "cycle_rows": len(cycles),
            "startup_wait_cycles": waiting,
            "mission_followup_s": followup,
            "t100_reached": t100_reached,
            "manifest_ok": not problems,
        })

    rejection_rows = read_csv(args.analysis / "rejection_report.csv")
    final_home = state.get("final_home", {})
    complete = (
        len(run_rows) == len(plan_rows) == 42
        and all(row.get("status") == "accepted" and row.get("manifest_ok") for row in run_rows)
        and len(metrics) == 42 and not rejection_rows
        and state.get("accepted_count") == 42 and state.get("batch_error") is None
        and final_home.get("pass") is True
        and all(row.get("t100_reached") or
                float(row.get("mission_followup_s", 0.0)) >= float(plan_document["frozen_horizon_s"])
                for row in run_rows)
        and versions == {str(plan_document["expected_policy_version"])}
    )
    payload: dict[str, Any] = {
        "schema": "zr10-confirmatory-campaign-audit-v1",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "complete_and_accepted": complete,
        "plan_sha256": sha256_file(args.plan),
        "expected_policy_sha256": plan_document["expected_policy_sha256"],
        "observed_policy_versions": sorted(versions),
        "planned_runs": len(plan_rows),
        "accepted_state_runs": sum(row.get("status") == "accepted" for row in run_rows),
        "analysis_rows": len(metrics),
        "analysis_rejections": len(rejection_rows),
        "manifest_failure_count": len(manifest_failures),
        "manifest_failures": manifest_failures,
        "total_run_bytes": total_bytes,
        "startup_wait_cycles": {
            "min": min(startup_cycles, default=0),
            "max": max(startup_cycles, default=0),
            "mean": sum(startup_cycles) / len(startup_cycles) if startup_cycles else 0,
        },
        "mission_followup_s": {
            "min": min(followups, default=0.0),
            "max": max(followups, default=0.0),
        },
        "health_totals": dict(health),
        "final_home": final_home,
        "runs": sorted(run_rows, key=lambda row: int(row.get("run_index", 0))),
    }
    clean = dict(payload)
    payload["audit_sha256"] = sha256_bytes(canonical_json(clean))
    write_json(args.output, payload)
    print(json.dumps({key: payload[key] for key in (
        "complete_and_accepted", "planned_runs", "accepted_state_runs", "analysis_rows",
        "analysis_rejections", "manifest_failure_count", "total_run_bytes",
        "startup_wait_cycles", "mission_followup_s", "health_totals", "final_home",
        "audit_sha256",
    )}, ensure_ascii=False, indent=2))
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
