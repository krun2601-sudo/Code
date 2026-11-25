"""Figure 2: feasible-map construction and baseline-preserving acceptance rules."""

from __future__ import annotations

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Polygon, Rectangle

from io_utils import save_all
from style import BLACK, BLUE, GRAY, LIGHT_GRAY, MM, NAVY, ORANGE, PALE, TEAL, apply_style, panel_label


def gate(ax, y, baseline, candidate, accepted):
    ax.text(0.02, y + 0.13, baseline, fontsize=7.7, weight="bold", va="center")
    ax.add_patch(Rectangle((0.24, y + 0.04), 0.18, 0.18, facecolor="white", edgecolor=NAVY, lw=1.0))
    ax.text(0.33, y + 0.13, "Retained\nbaseline", ha="center", va="center", fontsize=6.7)
    ax.add_patch(Rectangle((0.49, y + 0.04), 0.18, 0.18, facecolor="white", edgecolor=ORANGE, lw=1.0))
    ax.text(0.58, y + 0.13, candidate, ha="center", va="center", fontsize=6.6)
    ax.add_patch(Rectangle((0.74, y), 0.25, 0.26, facecolor=PALE, edgecolor=TEAL, lw=1.0))
    ax.text(0.865, y + 0.13, accepted, ha="center", va="center", fontsize=5.9, linespacing=1.18)
    for x0, x1 in ((0.42, 0.49), (0.67, 0.74)):
        ax.add_patch(FancyArrowPatch((x0, y + 0.13), (x1, y + 0.13), arrowstyle="-|>", mutation_scale=8, lw=1.0, color=GRAY))


def main():
    apply_style()
    fig, axes = plt.subplots(1, 2, figsize=(178 * MM, 72 * MM), gridspec_kw={"width_ratios": [0.86, 1.34]})
    ax, bx = axes

    # Panel a: a schematic only. The intersection is the planning feasible map,
    # not a physical confidence region.
    ax.set_xlim(-1.25, 1.25)
    ax.set_ylim(-0.9, 1.1)
    ax.set_aspect("equal")
    ax.axis("off")
    nominal = [(-0.95, -0.45), (0.95, -0.45), (0.62, 0.82), (-0.62, 0.82)]
    ax.add_patch(Polygon(nominal, closed=True, fill=False, edgecolor=NAVY, linewidth=1.5, label="Nominal field of view"))
    shifts = [(-0.08, -0.05), (0.08, -0.05), (-0.08, 0.05), (0.08, 0.05)]
    for sx, sy in shifts:
        shifted = [(x + sx, y + sy) for x, y in nominal]
        ax.add_patch(Polygon(shifted, closed=True, fill=False, edgecolor=LIGHT_GRAY, linewidth=0.75))
    core = [(-0.78, -0.38), (0.78, -0.38), (0.48, 0.69), (-0.48, 0.69)]
    ax.add_patch(Polygon(core, closed=True, facecolor="#D9EEE9", edgecolor=TEAL, linewidth=1.2))
    ax.scatter([0], [-0.65], marker="^", s=30, color=BLACK, zorder=5)
    ax.annotate("camera", (0, -0.65), (0.27, -0.75), fontsize=6.9, arrowprops={"arrowstyle": "-", "color": GRAY, "lw": 0.7})
    ax.text(0, 0.15, "four-corner\nintersection", ha="center", va="center", fontsize=7.0, color=TEAL, weight="bold")
    ax.text(0, -0.92, "Planning feasible map only\n(not a physical confidence region)", ha="center", va="top", fontsize=6.7, color=GRAY)
    panel_label(ax, "a", -0.05, 1.03)
    ax.set_title("Frozen feasible-coverage setting", pad=9, weight="bold")

    bx.set_xlim(0, 1)
    bx.set_ylim(0, 1)
    bx.axis("off")
    gate(bx, 0.58, "Initial planning", "Candidate\ncomplete route", "Complete\nΔUAC < 0\nT100 ≤ MAG")
    gate(bx, 0.17, "Event repair", "Candidate\nrepair", "ΔUAC < −δs\nΔT100 ≤ 0\npre-recovery witness")
    bx.text(0.33, 0.89, "MAG solution", ha="center", va="center", fontsize=6.8, color=NAVY)
    bx.text(0.33, 0.48, "Continue original queues", ha="center", va="center", fontsize=6.8, color=NAVY)
    bx.text(0.50, 0.03, "An unverified candidate never replaces the executable baseline.", ha="center", fontsize=7.0, color=GRAY)
    panel_label(bx, "b", -0.03, 1.03)
    bx.set_title("Baseline-preserving decision rules", pad=9, weight="bold")

    fig.subplots_adjust(left=0.03, right=0.995, bottom=0.12, top=0.87, wspace=0.12)
    save_all(fig, "figure2_baseline_rules")
    plt.close(fig)


if __name__ == "__main__":
    main()

