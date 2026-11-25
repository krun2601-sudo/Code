from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np


HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "plotting" / "data"

# Restrained palette: colour identifies methods, not statistical objects.
MAG = "#555555"
STATIC = "#C77C22"
ECR = "#177E7A"
PAIR = "#C8C8C8"
TEXT = "#1A1A1A"
REF = "#777777"
ADVERSE = "#A64632"

METHODS = ("mag", "measured_static", "uac_robust_repair_v2")
METHOD_COLORS = (MAG, STATIC, ECR)
METHOD_LABELS = ("MAG", "Static", "ECR-Cover")


def set_style() -> None:
    """A local, dependency-free implementation of the useful SciencePlots/Nature defaults."""
    mpl.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
            "font.size": 7,
            "axes.labelsize": 7,
            "axes.titlesize": 7,
            "xtick.labelsize": 6.5,
            "ytick.labelsize": 6.5,
            "legend.fontsize": 6.5,
            "axes.linewidth": 0.5,
            "lines.linewidth": 0.8,
            "lines.markersize": 3.0,
            "xtick.major.size": 2.5,
            "ytick.major.size": 2.5,
            "xtick.major.width": 0.5,
            "ytick.major.width": 0.5,
            "xtick.direction": "out",
            "ytick.direction": "out",
            "axes.grid": False,
            "legend.frameon": False,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.035,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
        }
    )


def rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def clean(ax: plt.Axes) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(colors=TEXT)


def letter(ax: plt.Axes, value: str, x: float = -0.16) -> None:
    ax.text(x, 1.04, value, transform=ax.transAxes, ha="left", va="bottom", fontsize=8, fontweight="bold")


def jitter(n: int, width: float) -> np.ndarray:
    base = np.array([0, -0.55, 0.55, -1, 1, -0.27, 0.27, -0.78, 0.78, 0.42], dtype=float)
    if n <= len(base):
        return base[:n] * width
    return np.linspace(-width, width, n)


def nominal_data() -> dict[str, dict[str, np.ndarray]]:
    data = [r for r in rows(DATA / "formal_run_metrics_wide.csv") if r["condition"] == "nominal"]
    out: dict[str, dict[str, np.ndarray]] = {}
    for method in METHODS:
        subset = sorted((r for r in data if r["method"] == method), key=lambda r: int(r["block"]))
        out[method] = {
            "uac": np.array([float(r["uac_h_s"]) for r in subset]),
            "t100": np.array([float(r["t100_s"]) for r in subset]),
        }
    return out


def paired_method_panel(ax: plt.Axes, data: dict[str, dict[str, np.ndarray]], metric: str, ylabel: str) -> None:
    matrix = np.column_stack([data[m][metric] for m in METHODS])
    x = np.arange(3)
    for row in matrix:
        ax.plot(x, row, color=PAIR, lw=0.55, zorder=1)
    for idx, color in enumerate(METHOD_COLORS):
        ax.scatter(
            np.full(matrix.shape[0], idx),
            matrix[:, idx],
            s=12,
            facecolor=color,
            edgecolor="white",
            linewidth=0.35,
            zorder=3,
        )
        med = float(np.median(matrix[:, idx]))
        ax.plot([idx - 0.16, idx + 0.16], [med, med], color=TEXT, lw=1.25, zorder=4)
    ax.set_xlim(-0.32, 2.32)
    ax.set_xticks(x, METHOD_LABELS)
    ax.set_ylabel(ylabel)
    clean(ax)


def figure3() -> None:
    data = nominal_data()
    d_mag = data["uac_robust_repair_v2"]["uac"] - data["mag"]["uac"]
    d_static = data["uac_robust_repair_v2"]["uac"] - data["measured_static"]["uac"]
    effects = [
        ("MAG", d_mag, -4.9413626957, -6.7071104255, -3.9104763393),
        ("Static", d_static, -0.2654152243, -1.0463878434, -0.1104119954),
    ]

    fig = plt.figure(figsize=(7.00, 2.35))
    gs = fig.add_gridspec(1, 3, left=0.065, right=0.993, bottom=0.23, top=0.94, wspace=0.43, width_ratios=[1.0, 1.18, 1.0])
    ax_a = fig.add_subplot(gs[0])
    ax_b = fig.add_subplot(gs[1])
    ax_c = fig.add_subplot(gs[2])

    paired_method_panel(ax_a, data, "uac", "UAC (s)")
    paired_method_panel(ax_c, data, "t100", "T100 (s)")

    for i, (_, raw, med, lo, hi) in enumerate(effects):
        y = 1 - i
        ax_b.scatter(raw, y + jitter(len(raw), 0.13), s=9, facecolor="white", edgecolor="#9A9A9A", linewidth=0.55, zorder=2)
        ax_b.plot([lo, hi], [y, y], color=TEXT, lw=1.05, zorder=3)
        ax_b.plot([lo, lo], [y - 0.08, y + 0.08], color=TEXT, lw=0.7)
        ax_b.plot([hi, hi], [y - 0.08, y + 0.08], color=TEXT, lw=0.7)
        ax_b.scatter([med], [y], s=16, facecolor=ECR, edgecolor="white", linewidth=0.35, zorder=4)
        ax_b.text(-7.25, y + 0.25, f"{med:.2f} [{lo:.2f}, {hi:.2f}]", fontsize=6.0, ha="left", va="bottom")
    ax_b.axvline(0, color=REF, lw=0.6, linestyle=(0, (2.2, 2.2)), zorder=0)
    ax_b.set_xlim(-7.4, 0.45)
    ax_b.set_ylim(-0.48, 1.48)
    ax_b.set_yticks([1, 0], ["vs MAG", "vs static"])
    ax_b.set_xlabel("ECR-Cover − baseline UAC (s)")
    clean(ax_b)

    letter(ax_a, "a")
    letter(ax_b, "b", x=-0.13)
    letter(ax_c, "c")
    for ext in ("png", "pdf", "svg"):
        fig.savefig(HERE / f"figure3_concept_v2.{ext}", dpi=600 if ext == "png" else None)
    plt.close(fig)


def repair_data() -> list[dict[str, str]]:
    return sorted(rows(DATA / "paired_repair_effects.csv"), key=lambda r: int(r["block"]))


def figure4() -> None:
    repair = repair_data()
    off = np.array([float(r["uac_off_s"]) for r in repair])
    on = np.array([float(r["uac_on_s"]) for r in repair])
    delta = np.array([float(r["uac_on_minus_off_s"]) for r in repair])
    ratio = np.array([float(r["t100_on_over_off"]) for r in repair])
    with (DATA / "V243_COMPLETE_AUDIT.json").open("r", encoding="utf-8") as f:
        audit = json.load(f)
    initial_ms = np.array([float(r["initial_planner_ms"]) for r in audit["runs"]])
    event_ms = np.array([float(v) for r in audit["runs"] for v in r["repair_planner_ms"]])

    fig = plt.figure(figsize=(7.00, 2.42))
    outer = fig.add_gridspec(1, 3, left=0.065, right=0.993, bottom=0.23, top=0.94, wspace=0.45, width_ratios=[1.52, 0.90, 1.05])
    inner = outer[0].subgridspec(1, 2, wspace=0.28, width_ratios=[1.05, 0.72])
    ax_pair = fig.add_subplot(inner[0])
    ax_effect = fig.add_subplot(inner[1])
    ax_ratio = fig.add_subplot(outer[1])
    ax_time = fig.add_subplot(outer[2])

    # Paired raw UAC values.
    for v0, v1, d in zip(off, on, delta):
        colour = ADVERSE if d > 0 else PAIR
        ax_pair.plot([0, 1], [v0, v1], color=colour, lw=0.65, zorder=1)
        ax_pair.scatter([0], [v0], s=11, facecolor="white", edgecolor=MAG, linewidth=0.55, zorder=2)
        ax_pair.scatter([1], [v1], s=11, facecolor=TEXT if d <= 0 else ADVERSE, edgecolor="white", linewidth=0.30, zorder=3)
    for x, values in [(0, off), (1, on)]:
        med = float(np.median(values))
        ax_pair.plot([x - 0.14, x + 0.14], [med, med], color=TEXT, lw=1.25, zorder=4)
    ax_pair.set_xlim(-0.25, 1.25)
    ax_pair.set_xticks([0, 1], ["Repair off", "Repair on"])
    ax_pair.set_ylabel("UAC (s)")
    clean(ax_pair)

    # Classic estimation-statistics companion axis.
    ax_effect.scatter(np.zeros(len(delta)), delta + jitter(len(delta), 0.045), s=8, facecolor="white", edgecolor="#8A8A8A", linewidth=0.5)
    med, lo, hi = -1.979, -2.496, -1.821
    ax_effect.plot([0.58, 0.58], [lo, hi], color=TEXT, lw=1.0)
    ax_effect.plot([0.52, 0.64], [lo, lo], color=TEXT, lw=0.65)
    ax_effect.plot([0.52, 0.64], [hi, hi], color=TEXT, lw=0.65)
    ax_effect.scatter([0.58], [med], s=15, facecolor=TEXT, edgecolor="white", linewidth=0.3, zorder=3)
    ax_effect.axhline(0, color=REF, lw=0.6, linestyle=(0, (2.2, 2.2)))
    ax_effect.set_xlim(-0.18, 0.76)
    ax_effect.set_ylim(-2.75, 1.60)
    ax_effect.set_xticks([0, 0.58], ["Blocks", "Median\n95% CI"])
    ax_effect.set_ylabel("Repair-on − off (s)", labelpad=2)
    clean(ax_effect)

    # T100 ratios: observations and the prespecified summary on separate rows.
    ax_ratio.scatter(ratio, 1 + jitter(len(ratio), 0.16), s=9, facecolor="white", edgecolor="#6E6E6E", linewidth=0.55, zorder=2)
    rmed, rlo, rhi = 0.9980111963, 0.9541716411, 1.0000639547
    ax_ratio.plot([rlo, rhi], [0, 0], color=TEXT, lw=1.0, zorder=3)
    ax_ratio.plot([rlo, rlo], [-0.08, 0.08], color=TEXT, lw=0.65)
    ax_ratio.plot([rhi, rhi], [-0.08, 0.08], color=TEXT, lw=0.65)
    ax_ratio.scatter([rmed], [0], s=15, facecolor=TEXT, edgecolor="white", linewidth=0.3, zorder=4)
    ax_ratio.axvline(1.00, color=REF, lw=0.65, linestyle="--")
    ax_ratio.axvline(1.05, color=REF, lw=0.65, linestyle=":")
    ax_ratio.set_xlim(0.90, 1.062)
    ax_ratio.set_ylim(-0.45, 1.45)
    ax_ratio.set_yticks([1, 0], ["Blocks", "Median\n95% CI"])
    ax_ratio.set_xlabel("T100 ratio (on/off)")
    ax_ratio.text(1.00, 1.42, "1.00", fontsize=5.8, ha="right", va="top", color=REF)
    ax_ratio.text(1.05, 1.42, "1.05", fontsize=5.8, ha="right", va="top", color=REF)
    clean(ax_ratio)

    # Planning times, normalized only to put unequal budgets on one physical scale.
    time_rows = [
        (1, initial_ms / 200.0, "Initial\n(200 ms)", "white"),
        (0, event_ms / 40.0, "Repair\n(40 ms)", "#777777"),
    ]
    for y, values, _, face in time_rows:
        values = np.sort(values)
        ax_time.scatter(values, y + jitter(len(values), 0.15), s=8, facecolor=face, edgecolor="#666666", linewidth=0.45, zorder=2)
        med = float(np.median(values))
        ax_time.plot([med, med], [y - 0.16, y + 0.16], color=TEXT, lw=1.1, zorder=3)
    ax_time.axvline(1.0, color=REF, lw=0.65, linestyle=":")
    ax_time.set_xlim(0, 1.04)
    ax_time.set_ylim(-0.42, 1.42)
    ax_time.set_yticks([1, 0], [time_rows[0][2], time_rows[1][2]])
    ax_time.set_xlabel("Planning time / budget")
    ax_time.text(1.0, 1.39, "budget", fontsize=5.8, ha="right", va="top", color=REF)
    clean(ax_time)

    letter(ax_pair, "a", x=-0.26)
    letter(ax_ratio, "b", x=-0.18)
    letter(ax_time, "c", x=-0.18)
    for ext in ("png", "pdf", "svg"):
        fig.savefig(HERE / f"figure4_concept_v2.{ext}", dpi=600 if ext == "png" else None)
    plt.close(fig)


def main() -> None:
    set_style()
    figure3()
    figure4()


if __name__ == "__main__":
    main()
