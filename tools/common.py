"""Shared, dependency-light helpers for the RA-L hardware protocol tools.

This module never opens a socket or sends a device command.  YAML is the only
optional dependency; it is already required by ZR10_Algorithm_Kit 4.0.0.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
PROTOCOL_VERSION = "ral-hil-1"
METHODS = ("raster", "mag", "open_loop_cml", "measured_static", "full_eacml")
CONDITIONS = ("nominal", "heterogeneous_delay", "pause")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def deep_merge(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(parent))
    for key, value in child.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = json.loads(json.dumps(value))
    return result


def load_yaml_with_inheritance(path: str | Path, seen: set[Path] | None = None) -> dict[str, Any]:
    """Load the same recursive mapping inheritance used by Kit 4.0.0."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - actionable import message
        raise RuntimeError("PyYAML is required to read ZR10 YAML configuration") from exc
    source = Path(path).resolve()
    visited = set() if seen is None else seen
    if source in visited:
        raise ValueError(f"cyclic YAML inheritance at {source}")
    visited.add(source)
    data = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{source}: top-level YAML value must be a mapping")
    data = dict(data)
    base = data.pop("extends", None)
    if base is None:
        return data
    base_path = Path(str(base))
    if not base_path.is_absolute():
        base_path = source.parent / base_path
    return deep_merge(load_yaml_with_inheritance(base_path.resolve(), visited), data)


def normalized_config(config: dict[str, Any]) -> dict[str, Any]:
    """Remove loader provenance only; retain every experiment-affecting value."""
    clean = json.loads(json.dumps(config))
    clean.pop("source", None)
    return clean


def config_sha256(config: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(normalized_config(config)))


def hardware_config(config: dict[str, Any]) -> dict[str, Any]:
    """Subset that must remain fixed across randomized method/condition overlays."""
    system_keys = (
        "rate_hz", "duration_s", "telemetry_stale_s", "max_frame_age_s", "pose_max_gap_s",
        "action_ttl_s", "require_all_devices", "startup_timeout_s",
    )
    policy_keys = (
        "fixed_zoom", "roi", "evaluation_grid", "scan_points_m", "scan_grid", "scan_dwell_s", "fov_margin",
        "safe_envelope_deg", "execution_confirmation", "frame_quality",
        "calibration_evidence", "strict_measured_model", "execution_model",
    )
    return {
        "devices": json.loads(json.dumps(config.get("devices", []))),
        "system": {key: config.get("system", {}).get(key) for key in system_keys},
        "policy_geometry_and_confirmation": {
            key: config.get("policy", {}).get(key) for key in policy_keys
        },
        "action_space": json.loads(json.dumps(config.get("action_space", {}))),
    }


def hardware_config_sha256(config: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(hardware_config(config)))


def pair_frozen_config(config: dict[str, Any]) -> dict[str, Any]:
    """Method-agnostic task instance and injected condition for paired runs."""
    policy = config.get("policy", {})
    experiment = policy.get("experiment", {})
    experiment_keys = (
        "protocol_version", "plan_id", "block", "pair_id", "condition",
        "horizon_s", "instance_seed", "injection_seed",
    )
    return {
        "hardware_and_task_geometry": hardware_config(config),
        "paired_experiment": {key: experiment.get(key) for key in experiment_keys},
        "fault_injection": json.loads(json.dumps(policy.get("fault_injection", {}))),
    }


def pair_frozen_config_sha256(config: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(pair_frozen_config(config)))


def parse_json(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON field: {value[:120]!r}") from exc


def finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric, got {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {number!r}")
    return number


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def percentile(values: Iterable[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return math.nan
    if not 0 <= probability <= 1:
        raise ValueError("probability must be in [0,1]")
    position = probability * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def median(values: Iterable[float]) -> float:
    return percentile(values, 0.5)


def safe_mean(values: Iterable[float]) -> float:
    items = [float(value) for value in values]
    return sum(items) / len(items) if items else math.nan


def sample_sd(values: Iterable[float]) -> float:
    items = [float(value) for value in values]
    if len(items) < 2:
        return math.nan
    mean = safe_mean(items)
    return math.sqrt(sum((value - mean) ** 2 for value in items) / (len(items) - 1))


def coefficient_of_variation(values: Iterable[float]) -> float:
    items = [float(value) for value in values]
    mean = safe_mean(items)
    if not items or abs(mean) <= 1e-12:
        return 0.0 if items and all(abs(value) <= 1e-12 for value in items) else math.nan
    sd = math.sqrt(sum((value - mean) ** 2 for value in items) / len(items))
    return sd / abs(mean)


def wrap_delta_deg(current: float, previous: float) -> float:
    return (current - previous + 180.0) % 360.0 - 180.0


def verify_manifest(run_dir: Path) -> list[str]:
    problems: list[str] = []
    manifest_path = run_dir / "manifest.sha256.json"
    if not manifest_path.is_file():
        return ["missing manifest.sha256.json"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid manifest: {exc}"]
    if not isinstance(manifest, dict) or not manifest:
        return ["manifest is empty or not a mapping"]
    for name, expected in manifest.items():
        path = run_dir / str(name)
        if not path.is_file():
            problems.append(f"manifest member missing: {name}")
        elif sha256_file(path).lower() != str(expected).lower():
            problems.append(f"hash mismatch: {name}")
    return problems


def discover_run_dirs(root: Path) -> list[Path]:
    if (root / "metadata.json").is_file():
        return [root.resolve()]
    return sorted(path.parent.resolve() for path in root.rglob("metadata.json"))
