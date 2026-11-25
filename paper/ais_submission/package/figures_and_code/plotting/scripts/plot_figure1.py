"""Figure 1 and ToC graphic: the ECR-Cover closed loop."""

from __future__ import annotations

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle

from io_utils import save_all
from style import BLACK, BLUE, GRAY, LIGHT_GRAY, MM, NAVY, ORANGE, PALE, TEAL, apply_style


def box(ax, xy, wh, title, lines, edge=NAVY, fill="white"):
    x, y = xy
    w, h = wh
    ax.add_patch(Rectangle((x, y), w, h, facecolor=fill, edgecolor=edge, linewidth=1.1))
    ax.text(x + 0.03 * w, y + h - 0.22 * h, title, fontsize=8.1, weight="bold", va="center")
    ax.text(x + 0.03 * w, y + h - 0.46 * h, lines, fontsize=6.9, va="top", linespacing=1.35, color=BLACK)


def arrow(ax, start, end, color=GRAY, style="-|>", lw=1.15, rad=0.0):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle=style,
            mutation_scale=8.5,
            linewidth=lw,
            color=color,
            connectionstyle=f"arc3,rad={rad}",
        )
    )


def main():
    apply_style()
    fig, ax = plt.subplots(figsize=(178 * MM, 82 * MM))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    box(ax, (0.02, 0.55), (0.21, 0.30), "Coverage planner", "Feasible map\nTime upper bounds\nUAC; T100 constrained", NAVY)
    box(ax, (0.28, 0.55), (0.18, 0.30), "Task queues", "Ordered viewpoints\nPer-camera timing", BLUE)
    box(ax, (0.51, 0.55), (0.20, 0.30), "Physical execution", "Velocity command\nGimbal dynamics\nRTSP acquisition", ORANGE)
    box(ax, (0.76, 0.55), (0.22, 0.30), "Confirmation gate", "ACK + stable pose\n+dwell + fresh frame\n+pose–frame synchronization", TEAL)

    arrow(ax, (0.23, 0.70), (0.28, 0.70), NAVY)
    arrow(ax, (0.46, 0.70), (0.51, 0.70), BLUE)
    arrow(ax, (0.71, 0.70), (0.76, 0.70), ORANGE)

    box(ax, (0.55, 0.10), (0.43, 0.26), "Execution-confirmed coverage ledger", "Measured pose at the fresh frame → projected field of view\nConfirmed cells update first-observation times", TEAL, PALE)
    arrow(ax, (0.87, 0.55), (0.82, 0.36), TEAL)

    box(ax, (0.02, 0.10), (0.44, 0.26), "Event-triggered residual-task repair", "Freeze confirmed cells; retain active healthy tasks\nCompare repair with continuing the original queues", ORANGE, "white")
    arrow(ax, (0.55, 0.23), (0.46, 0.23), GRAY)
    arrow(ax, (0.25, 0.36), (0.35, 0.55), ORANGE)

    ax.text(0.018, 0.925, "Planned pointing", fontsize=7.4, color=NAVY, weight="bold")
    ax.plot([0.115, 0.46], [0.936, 0.936], color=NAVY, lw=1.0)
    ax.text(0.51, 0.925, "Physical observation", fontsize=7.4, color=TEAL, weight="bold")
    ax.plot([0.625, 0.98], [0.936, 0.936], color=TEAL, lw=1.0)
    ax.axvline(0.485, 0.50, 0.89, color=LIGHT_GRAY, lw=0.8, ls="--")
    ax.text(0.485, 0.48, "planning–execution interface", ha="center", va="top", fontsize=6.8, color=GRAY)

    save_all(fig, "figure1_system_loop")
    plt.close(fig)

    # A compact derivative for the journal table-of-contents panel.
    fig, ax = plt.subplots(figsize=(85 * MM, 45 * MM))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    labels = [
        (0.03, "Plan", NAVY),
        (0.27, "Execute", ORANGE),
        (0.53, "Confirm", TEAL),
        (0.77, "Repair", BLUE),
    ]
    for x, label, color in labels:
        ax.add_patch(Rectangle((x, 0.52), 0.18, 0.25, facecolor="white", edgecolor=color, linewidth=1.1))
        ax.text(x + 0.09, 0.645, label, ha="center", va="center", fontsize=7.6, weight="bold", color=color)
    for a, b in zip(labels[:-1], labels[1:]):
        arrow(ax, (a[0] + 0.18, 0.645), (b[0], 0.645), GRAY)
    arrow(ax, (0.86, 0.50), (0.12, 0.34), TEAL, rad=-0.15)
    ax.text(0.49, 0.16, "Only measured-pose, fresh-frame evidence updates coverage", ha="center", fontsize=6.8)
    save_all(fig, "toc_graphic")
    plt.close(fig)


if __name__ == "__main__":
    main()

