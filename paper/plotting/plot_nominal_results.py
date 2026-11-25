"""Plot the nominal-condition hardware results from the frozen analysis table."""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from .plot_style import (
    BLUE,
    GREEN,
    GRAY,
    MAGENTA,
    VERMILION,
    apply_style,
    draw_ecdf,
    panel_title,
    save_figure,
    style_axis,
)


PAPER = Path(__file__).resolve().parents[1]
PROJECT = PAPER.parent
SOURCE = PROJECT / "private" / "v2_20261020" / "formal_v242" / "analysis"
OUTPUT = PAPER / "figures" / "final_fig3_nominal.png"

METHODS = [
    ("mag", "MAG", BLUE, "o"),
    ("measured_static", "实测静态", VERMILION, "s"),
    ("uac_robust_repair_v2", "ECR-Cover", GREEN, "^"),
]


def read_nominal_rows() -> list[dict[str, str]]:
    path = SOURCE / "run_metrics_wide.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row["condition"] == "nominal"]


def plot() -> Path:
    apply_style()
    rows = read_nominal_rows()
    fig, axes = plt.subplots(
        1,
        3,
        figsize=(7.15, 2.35),
        gridspec_kw={"width_ratios": [1.12, 1.0, 1.12], "wspace": 0.42},
    )

    ax = axes[0]
    for key, label, color, marker in METHODS:
        values = [float(row["uac_h_s"]) for row in rows if row["method"] == key]
        draw_ecdf(ax, values, label=label, color=color, marker=marker)
    ax.set_xlim(1.5, 11.5)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("UAC (s)")
    ax.set_ylabel("经验累积分布")
    panel_title(ax, "(a)", "UAC，10 次/方法")
    ax.legend(frameon=False, loc="lower right", handlelength=2.2)
    style_axis(ax)

    ax = axes[1]
    effects = [
        ("相对 MAG", -4.9413626957, -6.7071104255, -3.9104763393, GREEN),
        ("相对实测静态", -0.2654152243, -1.0463878434, -0.1104119954, MAGENTA),
    ]
    y = np.arange(len(effects))[::-1]
    ax.axvline(0.0, color=GRAY, linestyle="--", linewidth=0.9, zorder=1)
    for yi, (name, med, lo, hi, color) in zip(y, effects):
        ax.plot([lo, hi], [yi, yi], color=color, linewidth=2.0,
                solid_capstyle="round", zorder=3)
        ax.plot([lo, hi], [yi, yi], "|", color=color, markersize=7.0,
                markeredgewidth=1.0, zorder=4)
        ax.scatter(med, yi, marker="D", s=28, facecolor="white",
                   edgecolor=color, linewidth=1.2, zorder=5)
        ax.text(hi, yi + 0.20, f"{med:.2f}  [{lo:.2f}, {hi:.2f}]",
                color=color, fontsize=6.9, ha="right", va="bottom")
    ax.set_yticks(y, [item[0] for item in effects])
    ax.set_ylim(-0.55, 1.55)
    ax.set_xlim(-7.5, 1.0)
    ax.set_xlabel("UAC 配对中位差 (s)")
    panel_title(ax, "(b)", "效应量与 95% CI")
    style_axis(ax, grid="x", full_frame=False)

    ax = axes[2]
    for key, label, color, marker in METHODS:
        values = [float(row["t100_s"]) for row in rows if row["method"] == key]
        draw_ecdf(ax, values, label=label, color=color, marker=marker)
    ax.set_xlim(10.0, 27.0)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("T100 (s)")
    ax.set_ylabel("经验累积分布")
    panel_title(ax, "(c)", "完整覆盖时间")
    style_axis(ax)

    save_figure(fig, OUTPUT)
    return OUTPUT


if __name__ == "__main__":
    print(plot())

