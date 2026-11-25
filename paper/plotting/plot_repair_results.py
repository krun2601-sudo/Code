"""Plot the preregistered repair on/off hardware ablation."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from .plot_style import (
    BLUE,
    GREEN,
    GRAY,
    LIGHT_GRAY,
    VERMILION,
    apply_style,
    draw_ecdf,
    panel_title,
    save_figure,
    style_axis,
)


PAPER = Path(__file__).resolve().parents[1]
PROJECT = PAPER.parent
SOURCE = PROJECT / "private" / "v2_20261020" / "repair_revalidation_v243"
OUTPUT = PAPER / "figures" / "final_fig4_repair.png"


def read_pairs() -> list[dict[str, str]]:
    path = SOURCE / "analysis" / "paired_repair_effects.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_audit() -> dict:
    return json.loads((SOURCE / "V243_COMPLETE_AUDIT.json").read_text(encoding="utf-8"))


def plot() -> Path:
    apply_style()
    pairs = read_pairs()
    audit = read_audit()
    fig, axes = plt.subplots(
        1,
        3,
        figsize=(7.15, 2.65),
        gridspec_kw={"width_ratios": [1.30, 1.02, 1.08], "wspace": 0.45},
    )

    # Paired dumbbell plot: run identity remains visible without a decorative slope chart.
    ax = axes[0]
    ordered = sorted(pairs, key=lambda row: float(row["uac_off_s"]), reverse=True)
    y = np.arange(1, len(ordered) + 1)
    for yi, row in zip(y, ordered):
        off = float(row["uac_off_s"])
        on = float(row["uac_on_s"])
        connector = GREEN if on < off else VERMILION
        ax.plot([on, off], [yi, yi], color=connector, linewidth=1.15,
                alpha=0.72, zorder=2)
        ax.scatter(off, yi, s=22, facecolor="white", edgecolor=GRAY,
                   linewidth=1.0, marker="s", zorder=3)
        ax.scatter(on, yi, s=25, facecolor=GREEN if on < off else VERMILION,
                   edgecolor="white", linewidth=0.45, marker="o", zorder=4)
    ax.scatter(3.05, 0.0, s=22, facecolor="white", edgecolor=GRAY,
               linewidth=1.0, marker="s", clip_on=False, zorder=5)
    ax.text(3.18, 0.0, "关闭修复", va="center", fontsize=6.9, color=GRAY)
    ax.scatter(4.72, 0.0, s=25, facecolor=GREEN, edgecolor="white",
               linewidth=0.45, marker="o", clip_on=False, zorder=5)
    ax.text(4.85, 0.0, "启用修复", va="center", fontsize=6.9, color=GREEN)
    ax.set_yticks(y, [str(i) for i in range(1, len(ordered) + 1)])
    ax.set_ylim(-0.6, len(ordered) + 0.7)
    ax.invert_yaxis()
    ax.set_xlim(2.6, 7.15)
    ax.set_xlabel("UAC (s)")
    ax.set_ylabel("配对区组")
    panel_title(ax, "(a)", "修复开关的配对 UAC")
    style_axis(ax, grid="x", full_frame=False)

    # Run-level T100 ratios plus the registered noninferiority boundary.
    ax = axes[1]
    ratios = np.asarray([float(row["t100_on_over_off"]) for row in pairs])
    blocks = np.asarray([int(row["block"]) for row in pairs])
    ax.axvspan(0.90, 1.05, color="#E8F3EE", alpha=0.75, zorder=0)
    ax.axvline(1.00, color=GRAY, linestyle="--", linewidth=0.85, label="等效线 1.00")
    ax.axvline(1.05, color=VERMILION, linestyle=":", linewidth=1.15,
               label="非劣界 1.05")
    ax.scatter(ratios, blocks, s=22, color=BLUE, edgecolor="white",
               linewidth=0.45, zorder=3)
    med = 0.9980111963
    lo, hi = 0.9541716411, 1.0000639547
    ax.plot([lo, hi], [0, 0], color=GREEN, linewidth=2.2,
            solid_capstyle="round", zorder=4)
    ax.plot([lo, hi], [0, 0], "|", color=GREEN, markersize=7,
            markeredgewidth=1.0, zorder=5)
    ax.scatter(med, 0, marker="D", s=28, facecolor="white",
               edgecolor=GREEN, linewidth=1.2, zorder=6)
    ax.text((lo + hi) / 2, 0.34, f"{med:.3f}  [{lo:.3f}, {hi:.3f}]",
            color=GREEN, fontsize=6.6, ha="center")
    ax.set_yticks([0, *range(1, 11)], ["汇总", *[str(i) for i in range(1, 11)]])
    ax.set_ylim(-0.9, 10.7)
    ax.set_xlim(0.90, 1.06)
    ax.set_xlabel("T100：启用 / 关闭")
    panel_title(ax, "(b)", "逐区组比值与非劣界")
    style_axis(ax, grid="x", full_frame=False)

    # Empirical CDF of computation time normalized by its own frozen budget.
    ax = axes[2]
    initial = [float(item["initial_planner_ms"]) / 200.0 for item in audit["runs"]]
    repair = [float(value) / 40.0 for item in audit["runs"] for value in item["repair_planner_ms"]]
    draw_ecdf(ax, initial, label="初始规划（20 次）", color=BLUE, marker="o")
    draw_ecdf(ax, repair, label="事件重规划（40 次）", color=GREEN, marker="s")
    ax.axvline(1.0, color=VERMILION, linestyle="--", linewidth=1.0,
               label="预算上限")
    ax.set_xlim(0, 1.04)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("规划耗时 / 预算")
    ax.set_ylabel("经验累积分布")
    panel_title(ax, "(c)", "计算预算利用率")
    ax.legend(frameon=False, loc="lower right", handlelength=2.0)
    style_axis(ax)

    save_figure(fig, OUTPUT)
    return OUTPUT


if __name__ == "__main__":
    print(plot())

