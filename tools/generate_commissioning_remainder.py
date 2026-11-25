#!/usr/bin/env python3
"""Generate an auditable commissioning continuation overlay.

The tool is deliberately read-only with respect to prior runs.  It accepts
only transition IDs embedded in completed, manifest-valid run logs, subtracts
those IDs from one preregistered split, and writes an overlay containing the
exact remainder.  This avoids silently repeating successful calibration
samples when a fixed-duration hardware run ends before its mission does.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from common import sha256_file, write_json
from fit_execution_model import discover_run_dirs, parse_json, read_csv, verify_manifest


def completed_transition_ids(runs: Path) -> tuple[list[str], list[dict[str, str]]]:
    accepted: list[str] = []
    rejected_runs: list[dict[str, str]] = []
    for run_dir in discover_run_dirs(runs):
        try:
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            if metadata.get("completed") is not True:
                raise ValueError("session completed != true")
            problems = verify_manifest(run_dir)
            if problems:
                raise ValueError("; ".join(problems))
            for cycle in read_csv(run_dir / "cycles.csv"):
                diagnostics = parse_json(cycle.get("diagnostics_json"), {})
                policy = diagnostics.get("policy", diagnostics) if isinstance(diagnostics, dict) else {}
                embedded = policy.get("events", []) if isinstance(policy, dict) else []
                if isinstance(embedded, dict):
                    embedded = [embedded]
                if not isinstance(embedded, list):
                    raise ValueError("diagnostics.policy.events must be a list")
                for event in embedded:
                    if not isinstance(event, dict):
                        continue
                    if event.get("type", event.get("event")) != "transition_completed":
                        continue
                    transition_id = str(event.get("transition_id", "")).strip()
                    if transition_id:
                        accepted.append(transition_id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            rejected_runs.append({"run": str(run_dir), "reason": str(exc)})
    return accepted, rejected_runs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--split", required=True, choices=("train", "calibration", "test"))
    parser.add_argument("--extends", required=True,
                        help="Literal path stored in the generated YAML extends field")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    document = json.loads(args.plan.read_text(encoding="utf-8"))
    rows = [row for row in document.get("rows", []) if str(row.get("split")) == args.split]
    if not rows:
        raise SystemExit(f"plan contains no rows for split {args.split!r}")
    planned_ids = [str(row["transition_id"]) for row in rows]
    if len(planned_ids) != len(set(planned_ids)):
        raise SystemExit("plan contains duplicate transition IDs")
    planned_set = set(planned_ids)
    device_by_id = {str(row["transition_id"]): str(row["device_id"]) for row in rows}

    observed, rejected_runs = completed_transition_ids(args.runs)
    counts = Counter(identifier for identifier in observed if identifier in planned_set)
    duplicates = sorted(identifier for identifier, count in counts.items() if count > 1)
    if duplicates:
        raise SystemExit(f"completed logs already contain duplicate transition IDs: {duplicates}")
    completed = set(counts)
    remaining = [identifier for identifier in planned_ids if identifier not in completed]

    planned_by_device: dict[str, int] = defaultdict(int)
    completed_by_device: dict[str, int] = defaultdict(int)
    remaining_by_device: dict[str, int] = defaultdict(int)
    for identifier in planned_ids:
        planned_by_device[device_by_id[identifier]] += 1
    for identifier in completed:
        completed_by_device[device_by_id[identifier]] += 1
    for identifier in remaining:
        remaining_by_device[device_by_id[identifier]] += 1

    overlay: dict[str, Any] = {
        "extends": args.extends,
        "policy": {
            "commissioning": {
                "plan_path": str(args.plan.resolve()),
                "commissioning_id": str(document.get("commissioning_id", "")),
                "run_group": args.split,
                "transition_ids": remaining,
            }
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False), encoding="utf-8")
    report = {
        "schema_version": 1,
        "plan": str(args.plan.resolve()),
        "plan_sha256": sha256_file(args.plan.resolve()),
        "runs": str(args.runs.resolve()),
        "split": args.split,
        "planned": len(planned_ids),
        "completed": len(completed),
        "remaining": len(remaining),
        "planned_by_device": dict(sorted(planned_by_device.items())),
        "completed_by_device": dict(sorted(completed_by_device.items())),
        "remaining_by_device": dict(sorted(remaining_by_device.items())),
        "rejected_runs": rejected_runs,
        "output": str(args.output.resolve()),
        "output_sha256": sha256_file(args.output.resolve()),
    }
    write_json(args.report.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if remaining else 3


if __name__ == "__main__":
    raise SystemExit(main())
