"""Figure 4: repair ablation and planning-time budget with descriptive envelopes."""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

from envelope_style import (
    ADVERSE,
    BAND_INNER,
    BAND_OUTER,
    DENSITY_BLUE,
    DENSITY_GREY,
    ECR,
    MAG,
    MM,
    POINT_EDGE,
    REFERENCE,
    TEXT,
    apply_envelope_style,
    clean_axis,
    horizontal_density,
    jitter,
    panel_label,
    smooth_between,
    vertical_density,
)
from io_utils import read_csv, read_json, save_all


def repair_summary_panel(ax, off: np.ndarray, on: np.ndarray, difference: np.ndarray) -> None:
    matrix = np.column_stack([off, on])
    q10, q25, median, q75, q90 = np.quantile(matrix, [0.10, 0.25, 0.50, 0.75, 0.90], axis=0)
    x, q10_s = smooth_between(q10, 180)
    _, q25_s = smooth_between(q25, 180)
    _, median_s = smooth_between(median, 180)
    _, q75_s = smooth_between(q75, 180)
    _, q90_s = smooth_between(q90, 180)
    ax.fill_between(x, q10_s, q90_s, color=BAND_OUTER, linewidth=0, zorder=0)
    ax.fill_between(x, q25_s, q75_s, color=BAND_INNER, linewidth=0, zorder=1)
    ax.plot(x, median_s, color=ECR, lw=1.05, zorder=2)

    ax.scatter(
        jitter(len(off), 0.045),
        off,
        s=10,
        facecolor="white",
        edgecolor=MAG,
        linewidth=0.5,
        zorder=3,
    )
    colors = np.where(difference > 0, ADVERSE, ECR)
    ax.scatter(
        1 + jitter(len(on), 0.045),
        on,
        s=10,
        facecolor=colors,
        edgecolor="white",
        linewidth=0.3,
        zorder=3,
    )
    ax.set_xlim(-0.24, 1.24)
    ax.set_xticks([0, 1], ["Repair off", "Repair on"])
    ax.set_ylabel("UAC (s)")
    clean_axis(ax)


def main() -> None:
    apply_envelope_style()
    pairs = sorted(read_csv("paired_repair_effects.csv"), key=lambda row: int(row["block"]))
    audit = read_json("V243_COMPLETE_AUDIT.json")
    assert len(pairs) == 10, "Expected ten repair pairs."

    off = np.array([float(row["uac_off_s"]) for row in pairs])
    on = np.array([float(row["uac_on_s"]) for row in pairs])
    difference = np.array([float(row["uac_on_minus_off_s"]) for row in pairs])
    ratio = np.array([float(row["t100_on_over_off"]) for row in pairs])
    initial_ms = np.array([float(row["initial_planner_ms"]) for row in audit["runs"]])
    repair_ms = np.array([float(value) for row in audit["runs"] for value in row["repair_planner_ms"]])

    fig = plt.figure(figsize=(178 * MM, 61.5 * MM))
    outer = fig.add_gridspec(
        1,
        3,
        left=0.065,
        right=0.993,
        bottom=0.23,
        top=0.94,
        wspace=0.45,
        width_ratios=[1.52, 0.90, 1.05],
    )
    inner = outer[0].subgridspec(1, 2, wspace=0.28, width_ratios=[1.05, 0.72])
    ax_pair = fig.add_subplot(inner[0])
    ax_effect = fig.add_subplot(inner[1])
    ax_ratio = fig.add_subplot(outer[1])
    ax_time = fig.add_subplot(outer[2])

    repair_summary_panel(ax_pair, off, on, difference)

    vertical_density(ax_effect, difference, 0, color=DENSITY_GREY, width=0.13)
    favorable = difference <= 0
    ax_effect.scatter(
        np.zeros(favorable.sum()),
        difference[favorable] + jitter(favorable.sum(), 0.04),
        s=8,
        facecolor="white",
        edgecolor=POINT_EDGE,
        linewidth=0.5,
    )
    ax_effect.scatter(
        np.zeros((~favorable).sum()),
        difference[~favorable],
        s=10,
        facecolor=ADVERSE,
        edgecolor="white",
        linewidth=0.3,
        zorder=3,
    )
    median, low, high = -1.979, -2.496, -1.821
    ax_effect.plot([0.58, 0.58], [low, high], color=TEXT, lw=0.95)
    ax_effect.plot([0.52, 0.64], [low, low], color=TEXT, lw=0.65)
    ax_effect.plot([0.52, 0.64], [high, high], color=TEXT, lw=0.65)
    ax_effect.scatter([0.58], [median], s=14, facecolor=TEXT, edgecolor="white", linewidth=0.3)
    ax_effect.axhline(0, color=REFERENCE, lw=0.6, linestyle=(0, (2.2, 2.2)))
    ax_effect.set_xlim(-0.18, 0.76)
    ax_effect.set_ylim(-2.75, 1.60)
    ax_effect.set_xticks([0, 0.58], ["Blocks", "Median\n95% CI"])
    ax_effect.set_ylabel("Repair-on − off (s)", labelpad=2)
    clean_axis(ax_effect)

    horizontal_density(ax_ratio, ratio, 1, color=DENSITY_BLUE, height=0.22, lower=0.90, upper=1.055)
    ax_ratio.scatter(
        ratio,
        1 + jitter(len(ratio), 0.11),
        s=8,
        facecolor="white",
        edgecolor="#68747D",
        linewidth=0.5,
        zorder=2,
    )
    ratio_median, ratio_low, ratio_high = 0.9980111963, 0.9541716411, 1.0000639547
    ax_ratio.plot([ratio_low, ratio_high], [0, 0], color=TEXT, lw=0.95)
    ax_ratio.plot([ratio_low, ratio_low], [-0.07, 0.07], color=TEXT, lw=0.65)
    ax_ratio.plot([ratio_high, ratio_high], [-0.07, 0.07], color=TEXT, lw=0.65)
    ax_ratio.scatter([ratio_median], [0], s=14, facecolor=TEXT, edgecolor="white", linewidth=0.3)
    ax_ratio.axvline(1.00, color=REFERENCE, lw=0.65, linestyle="--")
    ax_ratio.axvline(1.05, color=REFERENCE, lw=0.65, linestyle=":")
    ax_ratio.set_xlim(0.90, 1.062)
    ax_ratio.set_ylim(-0.45, 1.45)
    ax_ratio.set_yticks([1, 0], ["Blocks", "Median\n95% CI"])
    ax_ratio.set_xlabel("T100 ratio (on/off)")
    ax_ratio.text(1.00, 1.42, "1.00", fontsize=5.8, ha="right", va="top", color=REFERENCE)
    ax_ratio.text(1.05, 1.42, "1.05", fontsize=5.8, ha="right", va="top", color=REFERENCE)
    clean_axis(ax_ratio)

    initial_normalized = initial_ms / 200.0
    repair_normalized = repair_ms / 40.0
    for y, values, color, face in [
        (1, initial_normalized, DENSITY_BLUE, "white"),
        (0, repair_normalized, DENSITY_GREY, "#727B83"),
    ]:
        horizontal_density(ax_time, values, y, color=color, height=0.20, lower=0, upper=0.62)
        ax_time.scatter(
            values,
            y + jitter(len(values), 0.10),
            s=7.5,
            facecolor=face,
            edgecolor="#626C74",
            linewidth=0.4,
            zorder=2,
        )
        observed_median = float(np.median(values))
        ax_time.plot([observed_median, observed_median], [y - 0.14, y + 0.14], color=TEXT, lw=1.0, zorder=3)
    ax_time.axvline(1.0, color=REFERENCE, lw=0.65, linestyle=":")
    ax_time.set_xlim(0, 1.04)
    ax_time.set_ylim(-0.42, 1.42)
    ax_time.set_yticks([1, 0], ["Initial\n(200 ms)", "Repair\n(40 ms)"])
    ax_time.set_xlabel("Planning time / budget")
    ax_time.text(1.0, 1.39, "budget", fontsize=5.8, ha="right", va="top", color=REFERENCE)
    clean_axis(ax_time)

    panel_label(ax_pair, "a", x=-0.26)
    panel_label(ax_ratio, "b", x=-0.18)
    panel_label(ax_time, "c", x=-0.18)
    save_all(fig, "figure4_repair", dpi=600)
    plt.close(fig)


if __name__ == "__main__":
    main()
