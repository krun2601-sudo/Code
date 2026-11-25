"""Export compact tables containing every datum drawn in Figures 3 and 4."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "plotting" / "data"
SCRIPTS = ROOT / "plotting" / "scripts"
EXPORT = ROOT / "data_exports"
sys.path.insert(0, str(SCRIPTS))

from envelope_style import density_curve, smooth_between  # noqa: E402


METHODS = ("mag", "measured_static", "uac_robust_repair_v2")
METHOD_LABELS = {"mag": "MAG", "measured_static": "Static", "uac_robust_repair_v2": "ECR-Cover"}


def read_csv(name: str) -> list[dict[str, str]]:
    with (DATA / name).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(name: str, fieldnames: list[str], records: list[dict]) -> None:
    EXPORT.mkdir(parents=True, exist_ok=True)
    with (EXPORT / name).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def nominal_tables() -> None:
    source = [row for row in read_csv("formal_run_metrics_wide.csv") if row["condition"] == "nominal"]
    source.sort(key=lambda row: (int(row["block"]), METHODS.index(row["method"])))
    runs = [
        {
            "block": row["block"],
            "pair_id": row["pair_id"],
            "method": row["method"],
            "method_label": METHOD_LABELS[row["method"]],
            "run_id": row["run_id"],
            "uac_s": row["uac_h_s"],
            "t100_s": row["t100_s"],
        }
        for row in source
    ]
    write_csv("figure3_nominal_runs.csv", list(runs[0]), runs)

    by_block: dict[int, dict[str, dict[str, str]]] = {}
    for row in source:
        by_block.setdefault(int(row["block"]), {})[row["method"]] = row
    paired: list[dict] = []
    for block, block_rows in sorted(by_block.items()):
        mag = float(block_rows["mag"]["uac_h_s"])
        static = float(block_rows["measured_static"]["uac_h_s"])
        ecr = float(block_rows["uac_robust_repair_v2"]["uac_h_s"])
        paired.append(
            {
                "block": block,
                "pair_id": block_rows["mag"]["pair_id"],
                "mag_uac_s": mag,
                "static_uac_s": static,
                "ecr_uac_s": ecr,
                "ecr_minus_mag_s": ecr - mag,
                "ecr_minus_static_s": ecr - static,
            }
        )
    write_csv("figure3_paired_uac_effects.csv", list(paired[0]), paired)

    quantile_rows: list[dict] = []
    envelope_rows: list[dict] = []
    matrices: dict[str, np.ndarray] = {}
    for metric, source_key in [("UAC", "uac_h_s"), ("T100", "t100_s")]:
        matrix = np.column_stack(
            [
                [float(row[source_key]) for row in sorted((r for r in source if r["method"] == method), key=lambda r: int(r["block"]))]
                for method in METHODS
            ]
        )
        matrices[metric] = matrix
        q10, q25, median, q75, q90 = np.quantile(matrix, [0.10, 0.25, 0.50, 0.75, 0.90], axis=0)
        for index, method in enumerate(METHODS):
            quantile_rows.append(
                {
                    "metric": metric,
                    "method_order": index,
                    "method": method,
                    "method_label": METHOD_LABELS[method],
                    "q10": q10[index],
                    "q25": q25[index],
                    "median": median[index],
                    "q75": q75[index],
                    "q90": q90[index],
                }
            )
        x, q10_s = smooth_between(q10)
        _, q25_s = smooth_between(q25)
        _, median_s = smooth_between(median)
        _, q75_s = smooth_between(q75)
        _, q90_s = smooth_between(q90)
        envelope_rows.extend(
            {
                "metric": metric,
                "x_method_order": xv,
                "q10": a,
                "q25": b,
                "median": c,
                "q75": d,
                "q90": e,
            }
            for xv, a, b, c, d, e in zip(x, q10_s, q25_s, median_s, q75_s, q90_s)
        )
    write_csv("figure3_quantile_summary.csv", list(quantile_rows[0]), quantile_rows)
    write_csv("figure3_envelope_samples.csv", list(envelope_rows[0]), envelope_rows)

    frozen = [
        {"contrast": "ECR-Cover minus MAG", "estimate_s": -4.9413626957, "ci95_low_s": -6.7071104255, "ci95_high_s": -3.9104763393},
        {"contrast": "ECR-Cover minus calibrated static", "estimate_s": -0.2654152243, "ci95_low_s": -1.0463878434, "ci95_high_s": -0.1104119954},
    ]
    write_csv("figure3_frozen_intervals.csv", list(frozen[0]), frozen)

    density_rows: list[dict] = []
    for contrast, values in [
        ("ECR-Cover minus MAG", np.array([row["ecr_minus_mag_s"] for row in paired])),
        ("ECR-Cover minus calibrated static", np.array([row["ecr_minus_static_s"] for row in paired])),
    ]:
        grid, density = density_curve(values)
        density_rows.extend({"contrast": contrast, "difference_s": x, "normalized_density": y} for x, y in zip(grid, density))
    write_csv("figure3_effect_density_samples.csv", list(density_rows[0]), density_rows)


def repair_tables() -> None:
    pairs = sorted(read_csv("paired_repair_effects.csv"), key=lambda row: int(row["block"]))
    write_csv("figure4_repair_pairs.csv", list(pairs[0]), pairs)

    off = np.array([float(row["uac_off_s"]) for row in pairs])
    on = np.array([float(row["uac_on_s"]) for row in pairs])
    matrix = np.column_stack([off, on])
    q10, q25, median, q75, q90 = np.quantile(matrix, [0.10, 0.25, 0.50, 0.75, 0.90], axis=0)
    summary = []
    for index, label in enumerate(["Repair off", "Repair on"]):
        summary.append(
            {
                "condition_order": index,
                "condition": label,
                "q10": q10[index],
                "q25": q25[index],
                "median": median[index],
                "q75": q75[index],
                "q90": q90[index],
            }
        )
    write_csv("figure4_uac_quantile_summary.csv", list(summary[0]), summary)
    x, q10_s = smooth_between(q10, 180)
    _, q25_s = smooth_between(q25, 180)
    _, median_s = smooth_between(median, 180)
    _, q75_s = smooth_between(q75, 180)
    _, q90_s = smooth_between(q90, 180)
    envelope = [
        {"x_condition_order": xv, "q10": a, "q25": b, "median": c, "q75": d, "q90": e}
        for xv, a, b, c, d, e in zip(x, q10_s, q25_s, median_s, q75_s, q90_s)
    ]
    write_csv("figure4_uac_envelope_samples.csv", list(envelope[0]), envelope)

    frozen = [
        {"quantity": "repair-on minus off UAC (s)", "estimate": -1.979, "ci95_low": -2.496, "ci95_high": -1.821},
        {"quantity": "T100 repair-on/off ratio", "estimate": 0.9980111963, "ci95_low": 0.9541716411, "ci95_high": 1.0000639547},
    ]
    write_csv("figure4_frozen_intervals.csv", list(frozen[0]), frozen)

    with (DATA / "V243_COMPLETE_AUDIT.json").open("r", encoding="utf-8") as handle:
        audit = json.load(handle)
    times: list[dict] = []
    for run_index, run in enumerate(audit["runs"], start=1):
        value = float(run["initial_planner_ms"])
        times.append(
            {
                "series": "Initial plan",
                "run_index": run_index,
                "event_index": "",
                "observed_ms": value,
                "budget_ms": 200.0,
                "observed_over_budget": value / 200.0,
            }
        )
        for event_index, event_value in enumerate(run["repair_planner_ms"], start=1):
            event = float(event_value)
            times.append(
                {
                    "series": "Event repair",
                    "run_index": run_index,
                    "event_index": event_index,
                    "observed_ms": event,
                    "budget_ms": 40.0,
                    "observed_over_budget": event / 40.0,
                }
            )
    write_csv("figure4_planning_times.csv", list(times[0]), times)

    density_rows: list[dict] = []
    series_values = {
        "UAC repair-on minus off (s)": np.array([float(row["uac_on_minus_off_s"]) for row in pairs]),
        "T100 repair-on/off ratio": np.array([float(row["t100_on_over_off"]) for row in pairs]),
        "Initial planning time / budget": np.array([row["observed_over_budget"] for row in times if row["series"] == "Initial plan"]),
        "Repair planning time / budget": np.array([row["observed_over_budget"] for row in times if row["series"] == "Event repair"]),
    }
    for series, values in series_values.items():
        lower = 0.90 if series.startswith("T100") else (0.0 if "time / budget" in series else None)
        upper = 1.055 if series.startswith("T100") else (0.62 if "time / budget" in series else None)
        grid, density = density_curve(values, lower=lower, upper=upper)
        density_rows.extend({"series": series, "x": x_value, "normalized_density": density_value} for x_value, density_value in zip(grid, density))
    write_csv("figure4_density_samples.csv", list(density_rows[0]), density_rows)


def main() -> None:
    nominal_tables()
    repair_tables()


if __name__ == "__main__":
    main()
