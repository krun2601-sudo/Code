#!/usr/bin/env python3
"""Pre-register executable four-device commissioning transitions and splits.

This writes a plan plus a policy overlay, but never executes transitions or
communicates with a gimbal.  Actual from/to poses must be logged by the policy
at runtime; the fitter never substitutes planned values for measured values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import PROTOCOL_VERSION, load_yaml_with_inheritance, write_csv, write_json


FIELDS = [
    "schema_version", "protocol_version", "commissioning_id", "device_id",
    "step", "transition_id", "split", "planned_from_yaw_deg",
    "planned_from_pitch_deg", "planned_to_yaw_deg", "planned_to_pitch_deg",
    "planned_distance_deg", "direction_stratum", "distance_stratum",
    "angle_regime", "commissioning_run_group", "pose_plan_tolerance_deg",
    "seed", "sequence_overlay_path", "status",
]


def device_seed(seed: int, device_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}|{device_id}".encode()).digest()[:4], "big")


def generate_targets(count: int, seed: int, yaw_limit: tuple[float, float],
                     pitch_limit: tuple[float, float]) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    current = (0.0, 0.0)
    targets: list[tuple[float, float]] = []
    regimes = {
        "small": (1.0, 3.0),
        "medium": (4.0, 12.0),
        "large": (13.0, 26.0),
    }
    for step in range(count):
        regime = ("small", "medium", "large")[step % 3]
        low, high = regimes[regime]
        for _ in range(1000):
            # Large transfers use yaw as the dominant axis because the pitch
            # safety interval can be narrower.  Small/medium transfers alternate
            # the dominant axis and signs to identify all direction coefficients.
            dominant = "yaw" if regime == "large" or (step // 3) % 2 == 0 else "pitch"
            magnitude = rng.uniform(low, high)
            sign = -1.0 if (step // 3) % 2 else 1.0
            secondary = rng.uniform(-0.45 * magnitude, 0.45 * magnitude)
            delta_yaw = sign * magnitude if dominant == "yaw" else secondary
            delta_pitch = sign * magnitude if dominant == "pitch" else secondary
            yaw = current[0] + delta_yaw
            pitch = current[1] + delta_pitch
            if not (yaw_limit[0] <= yaw <= yaw_limit[1] and
                    pitch_limit[0] <= pitch <= pitch_limit[1]):
                yaw = current[0] - delta_yaw
                pitch = current[1] - delta_pitch
            achieved = max(abs(yaw - current[0]), abs(pitch - current[1]))
            if (yaw_limit[0] <= yaw <= yaw_limit[1] and
                    pitch_limit[0] <= pitch <= pitch_limit[1] and
                    low <= achieved <= high):
                current = (round(yaw, 3), round(pitch, 3))
                targets.append(current)
                break
        else:  # pragma: no cover - limits above make this unreachable
            raise RuntimeError("could not generate a safe nontrivial transition")
    return targets


def split_counts(count: int) -> dict[str, int]:
    if count < 48:
        raise ValueError("at least 48 transitions/device are required")
    if count == 48:
        return {"train": 24, "calibration": 12, "test": 12}
    calibration = max(12, int(round(count * 0.2)))
    test = max(12, int(round(count * 0.2)))
    return {"train": count - calibration - test, "calibration": calibration, "test": test}


def transition_strata(transitions: list[dict[str, float]]) -> list[tuple[str, str]]:
    """Direction x distance-tertile strata used before assigning data splits."""
    if not transitions:
        return []
    by_distance = sorted(range(len(transitions)), key=lambda index: transitions[index]["distance"])
    distance_tier: dict[int, str] = {}
    names = ("short", "medium", "long")
    for rank, index in enumerate(by_distance):
        distance_tier[index] = names[min(2, rank * 3 // len(transitions))]
    result: list[tuple[str, str]] = []
    for index, transition in enumerate(transitions):
        delta_yaw = transition["to_yaw"] - transition["from_yaw"]
        delta_pitch = transition["to_pitch"] - transition["from_pitch"]
        yaw_direction = "yaw_pos" if delta_yaw > 1e-9 else ("yaw_neg" if delta_yaw < -1e-9 else "yaw_zero")
        pitch_direction = ("pitch_up" if delta_pitch > 1e-9 else
                           ("pitch_down" if delta_pitch < -1e-9 else "pitch_zero"))
        result.append((f"{yaw_direction}+{pitch_direction}", distance_tier[index]))
    return result


def stratified_split_labels(transitions: list[dict[str, float]], seed: int) -> tuple[list[str], list[tuple[str, str]]]:
    """Assign exact split totals after interleaving direction-distance strata."""
    count = len(transitions)
    targets = split_counts(count)
    strata = transition_strata(transitions)
    grouped: dict[tuple[str, str], list[int]] = {}
    for index, stratum in enumerate(strata):
        grouped.setdefault(stratum, []).append(index)
    rng = random.Random(seed)
    for members in grouped.values():
        rng.shuffle(members)
    keys = sorted(grouped)
    rng.shuffle(keys)
    interleaved: list[int] = []
    while any(grouped[key] for key in keys):
        for key in keys:
            if grouped[key]:
                interleaved.append(grouped[key].pop())

    # A deficit scheduler preserves exact totals and spreads each label across
    # the interleaved strata; a seeded tie breaker avoids systematic direction
    # preference while remaining exactly reproducible.
    remaining = dict(targets)
    assigned = {name: 0 for name in targets}
    labels = [""] * count
    tie_order = list(targets)
    rng.shuffle(tie_order)
    tie_rank = {name: rank for rank, name in enumerate(tie_order)}
    for position, index in enumerate(interleaved, 1):
        candidates = [name for name, left in remaining.items() if left > 0]
        label = max(
            candidates,
            key=lambda name: (
                targets[name] * position / count - assigned[name],
                -tie_rank[name],
            ),
        )
        labels[index] = label
        assigned[label] += 1
        remaining[label] -= 1
    if assigned != targets or any(not label for label in labels):
        raise AssertionError(f"stratified split assignment failed: assigned={assigned}, targets={targets}")
    return labels, strata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-prefix", required=True, type=Path)
    parser.add_argument("--per-device", type=int, default=60)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--overlay-output", type=Path,
                        help="Executable commissioning overlay; default is <output-prefix>.overlay.yaml")
    parser.add_argument("--overlay-extends",
                        help="Literal YAML extends value; default is the absolute --config path")
    args = parser.parse_args()
    if args.per_device < 48:
        parser.error("--per-device must be at least 48")
    config = load_yaml_with_inheritance(args.config)
    devices = [device for device in config.get("devices", []) if device.get("enabled", True) is True]
    if len(devices) != 4:
        raise SystemExit(f"expected four enabled devices, found {len(devices)}")
    commissioning_id = f"commission-{args.seed}-{args.per_device}pd"
    csv_path = args.output_prefix.with_suffix(".csv").resolve()
    json_path = args.output_prefix.with_suffix(".json").resolve()
    overlay_base = (args.overlay_output.resolve() if args.overlay_output else
                    args.output_prefix.with_suffix(".overlay.yaml").resolve())
    overlay_paths = {
        split: overlay_base.with_name(f"{overlay_base.stem}.{split}{overlay_base.suffix}")
        for split in ("train", "calibration", "test")
    }
    pose_tolerance = float(config.get("policy", {}).get("execution_confirmation", {}).get("pose_tolerance_deg", 1.0))
    if not math.isfinite(pose_tolerance) or pose_tolerance <= 0:
        raise SystemExit("policy.execution_confirmation.pose_tolerance_deg must be positive")
    rows: list[dict[str, Any]] = []
    for device in devices:
        key = str(device["id"])
        safe = config.get("policy", {}).get("safe_envelope_deg", {})
        yaw = safe.get("yaw", [-35, 35])
        pitch = safe.get("pitch", [-20, 20])
        mechanical_yaw = device.get("yaw_limits_deg", yaw)
        mechanical_pitch = device.get("pitch_limits_deg", pitch)
        yaw_limit = (max(float(yaw[0]), float(mechanical_yaw[0])), min(float(yaw[1]), float(mechanical_yaw[1])))
        pitch_limit = (max(float(pitch[0]), float(mechanical_pitch[0])), min(float(pitch[1]), float(mechanical_pitch[1])))
        dseed = device_seed(args.seed, key)
        targets = generate_targets(args.per_device, dseed, yaw_limit, pitch_limit)
        previous = (0.0, 0.0)
        transitions: list[dict[str, float]] = []
        for target in targets:
            transitions.append({
                "from_yaw": previous[0], "from_pitch": previous[1],
                "to_yaw": target[0], "to_pitch": target[1],
                "distance": math.hypot(target[0] - previous[0], target[1] - previous[1]),
            })
            previous = target
        labels, strata = stratified_split_labels(transitions, dseed ^ 0xA5A5A5A5)
        for index, (transition, label, stratum) in enumerate(zip(transitions, labels, strata), 1):
            transition_id = f"{commissioning_id}:{key}:{index:03d}"
            rows.append({
                "schema_version": 1,
                "protocol_version": PROTOCOL_VERSION,
                "commissioning_id": commissioning_id,
                "device_id": key,
                "step": index,
                "transition_id": transition_id,
                "split": label,
                "planned_from_yaw_deg": transition["from_yaw"],
                "planned_from_pitch_deg": transition["from_pitch"],
                "planned_to_yaw_deg": transition["to_yaw"],
                "planned_to_pitch_deg": transition["to_pitch"],
                "planned_distance_deg": round(transition["distance"], 6),
                "direction_stratum": stratum[0],
                "distance_stratum": stratum[1],
                "pose_plan_tolerance_deg": pose_tolerance,
                "seed": dseed,
                "angle_regime": ("small" if max(abs(transition["to_yaw"] - transition["from_yaw"]),
                                                  abs(transition["to_pitch"] - transition["from_pitch"])) <= 3.0
                                 else "medium" if max(abs(transition["to_yaw"] - transition["from_yaw"]),
                                                      abs(transition["to_pitch"] - transition["from_pitch"])) <= 12.0
                                 else "large"),
                "commissioning_run_group": label,
                "sequence_overlay_path": str(overlay_paths[label]),
                "status": "planned",
            })
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        device_counts = counts.setdefault(str(row["device_id"]), {"train": 0, "calibration": 0, "test": 0})
        device_counts[str(row["split"])] += 1
    write_csv(csv_path, rows, FIELDS)
    policy_overlays = {}
    for split, overlay_path in overlay_paths.items():
        policy_overlays[split] = {
            "method": "commissioning",
            "fault_injection": {"kind": "none", "enabled": False},
            "commissioning": {
                "plan_path": str(json_path),
                "commissioning_id": commissioning_id,
                "run_group": split,
                "transition_ids": [str(row["transition_id"]) for row in rows if row["split"] == split],
            },
        }
    write_json(json_path, {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "commissioning_id": commissioning_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "per_device": args.per_device,
        "split_counts": counts,
        "safety": "generation only; the overlay is inert until the operator explicitly runs the platform; targets are clipped to the configured safe/mechanical intersection",
        "anti_leakage": "train/calibration/test are three separate hardware runs; the fitter rejects any source run containing multiple split labels",
        "split_stratification": "seeded interleaving by signed yaw/pitch direction and within-device distance tertile",
        "policy_overlays": policy_overlays,
        "sequence_overlay_paths": {key: str(value) for key, value in overlay_paths.items()},
        "rows": rows,
    })
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyYAML is required to write the commissioning overlay") from exc
    for split, overlay_path in overlay_paths.items():
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_document = {
            "extends": args.overlay_extends or str(args.config.resolve()),
            "policy": policy_overlays[split],
        }
        overlay_path.write_text(yaml.safe_dump(
            overlay_document, allow_unicode=True, sort_keys=False,
        ), encoding="utf-8")
    print(json.dumps({"commissioning_id": commissioning_id, "rows": len(rows),
                      "split_counts": counts, "csv": str(csv_path), "json": str(json_path),
                      "overlays": {key: str(value) for key, value in overlay_paths.items()}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
