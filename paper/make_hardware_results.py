"""Build publication-ready commissioning data and figures from verified logs.

The script reads one completed hardware run plus the frozen execution-model
predictions.  It exports only transition-level, device-anonymous measurements;
network endpoints and raw SDK payloads are never copied into the paper folder.
"""
from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
RUN = (
    PROJECT
    / "private"
    / "live_20261018"
    / "commissioning_runs_full"
    / "hardware_20261018T141423_783935Z"
)
MODEL_DIR = PROJECT / "private" / "live_20261018" / "execution_model_full"
OUT_DATA = ROOT / "data"
OUT_FIG = ROOT / "figures"
OUT_DATA.mkdir(parents=True, exist_ok=True)
OUT_FIG.mkdir(parents=True, exist_ok=True)

DEVICE_LABELS = {
    "zr10_25": "ZR10-1",
    "zr10_26": "ZR10-2",
    "zr10_27": "ZR10-3",
    "zr10_28": "ZR10-4",
}
COLORS = {
    "ZR10-1": "#386CB0",
    "ZR10-2": "#7FC97F",
    "ZR10-3": "#F0027F",
    "ZR10-4": "#BF5B17",
}


def truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def quantile(values: list[float], probability: float) -> float:
    data = sorted(values)
    position = (len(data) - 1) * probability
    lo = math.floor(position)
    hi = math.ceil(position)
    if lo == hi:
        return data[lo]
    return data[lo] * (hi - position) + data[hi] * (position - lo)


def read_transitions() -> dict[str, dict[str, object]]:
    transitions: dict[str, dict[str, object]] = {}
    with (RUN / "cycles.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            diagnostics = json.loads(row["diagnostics_json"] or "{}")
            policy = diagnostics.get("policy", {})
            for event in policy.get("events", []):
                if event.get("type") == "transition_completed":
                    transitions.setdefault(str(event["transition_id"]), event)
    if len(transitions) != 240:
        raise RuntimeError(f"expected 240 unique transitions, found {len(transitions)}")
    return transitions


def read_predictions() -> dict[str, dict[str, str]]:
    with (MODEL_DIR / "predictions.csv").open(encoding="utf-8", newline="") as handle:
        rows = {row["transition_id"]: row for row in csv.DictReader(handle)}
    if len(rows) != 240:
        raise RuntimeError(f"expected 240 predictions, found {len(rows)}")
    return rows


def transition_table() -> list[dict[str, object]]:
    transitions = read_transitions()
    predictions = read_predictions()
    output: list[dict[str, object]] = []
    for transition_id in sorted(transitions):
        event = transitions[transition_id]
        prediction = predictions[transition_id]
        device = DEVICE_LABELS[str(event["device_id"])]
        actual = float(event["actual_s"])
        nominal = float(event["predicted_s"])
        point = float(prediction["point_prediction_s"])
        upper = float(prediction["upper_prediction_s"])
        yaw_error = abs(
            (float(event["to_yaw_deg"]) - float(event["planned_to_yaw_deg"]) + 180.0)
            % 360.0
            - 180.0
        )
        pitch_error = abs(float(event["to_pitch_deg"]) - float(event["planned_to_pitch_deg"]))
        output.append(
            {
                "transition_id": transition_id,
                "device": device,
                "split": str(event["split"]),
                "delta_yaw_deg": float(event["planned_to_yaw_deg"])
                - float(event["planned_from_yaw_deg"]),
                "delta_pitch_deg": float(event["planned_to_pitch_deg"])
                - float(event["planned_from_pitch_deg"]),
                "actual_s": actual,
                "nominal_prediction_s": nominal,
                "fitted_prediction_s": point,
                "upper_prediction_s": upper,
                "upper_covers": actual <= upper + 1e-12,
                "yaw_endpoint_error_deg": yaw_error,
                "pitch_endpoint_error_deg": pitch_error,
                "motion_s": float(event["motion_s"]),
                "settle_s": float(event["settle_s"]),
                "dwell_s": float(event["dwell_s"]),
                "frame_count": int(event["frame_count"]),
            }
        )
    return output


def write_transition_table(rows: list[dict[str, object]]) -> None:
    path = OUT_DATA / "commissioning_transitions.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_link_metrics() -> tuple[dict[str, list[float]], dict[str, list[float]], dict[str, int]]:
    command_latency: dict[str, list[float]] = defaultdict(list)
    frame_age: dict[str, list[float]] = defaultdict(list)
    counters = {
        "sent_commands": 0,
        "acknowledged_commands": 0,
        "command_errors": 0,
        "frames": 0,
        "usable_frames": 0,
        "resolution_ok_frames": 0,
    }
    with (RUN / "commands.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if not truthy(row["sent"]):
                continue
            label = DEVICE_LABELS[row["device_id"]]
            counters["sent_commands"] += 1
            counters["acknowledged_commands"] += int(truthy(row["ack"]))
            counters["command_errors"] += int(bool(row["error"]))
            if row["latency_ms"]:
                command_latency[label].append(float(row["latency_ms"]))
    with (RUN / "frames.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            label = DEVICE_LABELS[row["device_id"]]
            counters["frames"] += 1
            counters["usable_frames"] += int(truthy(row["usable"]))
            counters["resolution_ok_frames"] += int(truthy(row["resolution_ok"]))
            frame_age[label].append(float(row["age_s"]))
    return command_latency, frame_age, counters


def configure() -> None:
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "font.size": 8.2,
            "axes.labelsize": 8.2,
            "axes.titlesize": 8.8,
            "legend.fontsize": 7.2,
            "figure.dpi": 170,
            "savefig.dpi": 360,
        }
    )


def empirical_cdf(values: list[float]) -> tuple[np.ndarray, np.ndarray]:
    x = np.sort(np.asarray(values, dtype=float))
    y = np.arange(1, len(x) + 1, dtype=float) / len(x)
    return x, y


def make_figure(rows: list[dict[str, object]]) -> None:
    configure()
    command_latency, frame_age, _ = read_link_metrics()
    fig, axes = plt.subplots(2, 2, figsize=(7.15, 5.45))
    ax = axes[0, 0]
    actual = np.asarray([float(row["actual_s"]) for row in rows])
    nominal = np.asarray([float(row["nominal_prediction_s"]) for row in rows])
    positions = [1, 2]
    bp = ax.boxplot(
        [nominal, actual], positions=positions, widths=0.55, patch_artist=True,
        showfliers=False, medianprops={"color": "#102A43", "linewidth": 1.3},
        whiskerprops={"color": "#486581"}, capprops={"color": "#486581"},
    )
    for patch, color in zip(bp["boxes"], ["#D9EAF7", "#FCE5CD"]):
        patch.set_facecolor(color)
        patch.set_edgecolor("#486581")
    ax.scatter(np.full_like(nominal, 1.0) + np.linspace(-0.11, 0.11, len(nominal)), nominal,
               s=5, alpha=0.18, color="#386CB0", linewidths=0)
    ax.scatter(np.full_like(actual, 2.0) + np.linspace(-0.11, 0.11, len(actual)), actual,
               s=5, alpha=0.18, color="#BF5B17", linewidths=0)
    ax.set_xticks(positions, ["标称模型", "实测完成时间"])
    ax.set_ylabel("端到端时间  s")
    ax.set_title("(a) 标称模型系统性低估  n=240", loc="left", fontweight="bold")
    ax.grid(axis="y", color="#E6E6E6", linewidth=0.6)

    ax = axes[0, 1]
    test_rows = [row for row in rows if row["split"] == "test"]
    for label in DEVICE_LABELS.values():
        subset = [row for row in test_rows if row["device"] == label]
        x = np.asarray([float(row["actual_s"]) for row in subset])
        y = np.asarray([float(row["fitted_prediction_s"]) for row in subset])
        upper = np.asarray([float(row["upper_prediction_s"]) for row in subset])
        ax.vlines(x, y, upper, color=COLORS[label], alpha=0.45, linewidth=0.9)
        ax.scatter(x, y, label=label, color=COLORS[label], s=18, edgecolor="white", linewidth=0.35)
        missed = np.asarray([not bool(row["upper_covers"]) for row in subset])
        if missed.any():
            ax.scatter(x[missed], upper[missed], marker="x", color="#A61C00", s=26, linewidth=1.1)
    lo, hi = 18, 37
    ax.plot([lo, hi], [lo, hi], "--", color="#627D98", linewidth=0.9, label="理想预测")
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_xlabel("实测完成时间  s")
    ax.set_ylabel("点预测及单侧上界  s")
    ax.set_title("(b) 独立测试集  n=48", loc="left", fontweight="bold")
    ax.grid(color="#E6E6E6", linewidth=0.6)
    ax.legend(ncol=2, frameon=False, loc="upper left")

    ax = axes[1, 0]
    yaw_error = [float(row["yaw_endpoint_error_deg"]) for row in rows]
    pitch_error = [float(row["pitch_endpoint_error_deg"]) for row in rows]
    for values, label, color in (
        (yaw_error, "方位误差", "#386CB0"),
        (pitch_error, "俯仰误差", "#F0027F"),
    ):
        x, y = empirical_cdf(values)
        ax.step(x, y, where="post", label=label, color=color, linewidth=1.35)
    ax.axvline(1.0, color="#A61C00", linestyle="--", linewidth=1.0, label="完成门限 1°")
    ax.set_xlim(0, 1.05)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("终点绝对误差  deg")
    ax.set_ylabel("经验累积分布")
    ax.set_title("(c) 端点误差  240 次转移", loc="left", fontweight="bold")
    ax.grid(color="#E6E6E6", linewidth=0.6)
    ax.legend(frameon=False, loc="lower right")

    ax = axes[1, 1]
    for label in DEVICE_LABELS.values():
        x, y = empirical_cdf(command_latency[label])
        ax.step(x, y, where="post", color=COLORS[label], linewidth=1.0, label=f"{label} ACK")
    ax.set_xlim(0, 20)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("命令 ACK 延迟  ms")
    ax.set_ylabel("经验累积分布")
    ax.set_title("(d) 60,666 条实发命令", loc="left", fontweight="bold")
    ax.grid(color="#E6E6E6", linewidth=0.6)
    ax.legend(frameon=False, loc="lower right", ncol=2)

    fig.tight_layout(pad=1.1, h_pad=1.3, w_pad=1.5)
    fig.savefig(OUT_FIG / "fig4_hardware_commissioning.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def write_summary(rows: list[dict[str, object]]) -> None:
    command_latency, frame_age, counters = read_link_metrics()
    actual = [float(row["actual_s"]) for row in rows]
    nominal = [float(row["nominal_prediction_s"]) for row in rows]
    test = [row for row in rows if row["split"] == "test"]
    test_errors = [float(row["actual_s"]) - float(row["fitted_prediction_s"]) for row in test]
    summary = {
        "run_id": RUN.name,
        "transition_count": len(rows),
        "test_transition_count": len(test),
        "actual_s": {
            "mean": statistics.fmean(actual),
            "median": statistics.median(actual),
            "p95": quantile(actual, 0.95),
            "min": min(actual),
            "max": max(actual),
        },
        "nominal_model": {
            "mean_prediction_s": statistics.fmean(nominal),
            "mae_s": statistics.fmean(abs(a - p) for a, p in zip(actual, nominal)),
            "rmse_s": math.sqrt(statistics.fmean((a - p) ** 2 for a, p in zip(actual, nominal))),
            "underprediction_rate": statistics.fmean(a > p for a, p in zip(actual, nominal)),
            "mean_actual_to_prediction_ratio": statistics.fmean(a / p for a, p in zip(actual, nominal)),
        },
        "fitted_model_test": {
            "mae_s": statistics.fmean(abs(error) for error in test_errors),
            "rmse_s": math.sqrt(statistics.fmean(error * error for error in test_errors)),
            "underprediction_rate": statistics.fmean(error > 0 for error in test_errors),
            "upper_coverage": statistics.fmean(bool(row["upper_covers"]) for row in test),
        },
        "endpoint_error_deg": {
            "yaw_max": max(float(row["yaw_endpoint_error_deg"]) for row in rows),
            "pitch_max": max(float(row["pitch_endpoint_error_deg"]) for row in rows),
        },
        "link": counters,
        "ack_latency_ms_p95_by_device": {
            label: quantile(values, 0.95) for label, values in command_latency.items()
        },
        "frame_age_s_p95_by_device": {
            label: quantile(values, 0.95) for label, values in frame_age.items()
        },
    }
    (OUT_DATA / "commissioning_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    rows = transition_table()
    write_transition_table(rows)
    write_summary(rows)
    make_figure(rows)
    print(OUT_DATA / "commissioning_transitions.csv")
    print(OUT_DATA / "commissioning_summary.json")
    print(OUT_FIG / "fig4_hardware_commissioning.png")


if __name__ == "__main__":
    main()
