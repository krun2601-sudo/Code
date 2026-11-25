"""Figure 3: nominal hardware experiments with descriptive envelopes."""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

from envelope_style import (
    BAND_INNER,
    BAND_OUTER,
    DENSITY_BLUE,
    ECR,
    EFFECT,
    MAG,
    MM,
    POINT_EDGE,
    REFERENCE,
    STATIC,
    TEXT,
    apply_envelope_style,
    clean_axis,
    horizontal_density,
    jitter,
    panel_label,
    smooth_between,
)
from io_utils import read_csv, save_all


METHODS = ("mag", "measured_static", "uac_robust_repair_v2")
METHOD_LABELS = ("MAG", "Static", "ECR-Cover")
METHOD_COLORS = (MAG, STATIC, ECR)


def load_nominal() -> dict[str, dict[str, np.ndarray]]:
    rows = [row for row in read_csv("formal_run_metrics_wide.csv") if row["condition"] == "nominal"]
    result: dict[str, dict[str, np.ndarray]] = {}
    for method in METHODS:
        subset = sorted((row for row in rows if row["method"] == method), key=lambda row: int(row["block"]))
        assert len(subset) == 10, f"Expected 10 nominal runs for {method}."
        result[method] = {
            "uac": np.array([float(row["uac_h_s"]) for row in subset]),
            "t100": np.array([float(row["t100_s"]) for row in subset]),
        }
    return result


def quantile_envelope_panel(ax, data: dict[str, dict[str, np.ndarray]], metric: str, ylabel: str) -> None:
    matrix = np.column_stack([data[method][metric] for method in METHODS])
    q10, q25, median, q75, q90 = np.quantile(matrix, [0.10, 0.25, 0.50, 0.75, 0.90], axis=0)
    x, q10_s = smooth_between(q10)
    _, q25_s = smooth_between(q25)
    _, median_s = smooth_between(median)
    _, q75_s = smooth_between(q75)
    _, q90_s = smooth_between(q90)

    ax.fill_between(x, q10_s, q90_s, color=BAND_OUTER, linewidth=0, zorder=0)
    ax.fill_between(x, q25_s, q75_s, color=BAND_INNER, linewidth=0, zorder=1)
    ax.plot(x, median_s, color=TEXT, lw=0.95, zorder=2)
    for index, color in enumerate(METHOD_COLORS):
        ax.scatter(
            index + jitter(matrix.shape[0], 0.055),
            matrix[:, index],
            s=11,
            facecolor=color,
            edgecolor="white",
            linewidth=0.3,
            zorder=3,
        )
    ax.set_xlim(-0.32, 2.32)
    ax.set_xticks(np.arange(3), METHOD_LABELS)
    ax.set_ylabel(ylabel)
    clean_axis(ax)


def main() -> None:
    apply_envelope_style()
    data = load_nominal()
    diff_mag = data["uac_robust_repair_v2"]["uac"] - data["mag"]["uac"]
    diff_static = data["uac_robust_repair_v2"]["uac"] - data["measured_static"]["uac"]
    effects = [
        (diff_mag, -4.9413626957, -6.7071104255, -3.9104763393),
        (diff_static, -0.2654152243, -1.0463878434, -0.1104119954),
    ]

    fig = plt.figure(figsize=(178 * MM, 60 * MM))
    grid = fig.add_gridspec(
        1,
        3,
        left=0.065,
        right=0.993,
        bottom=0.23,
        top=0.94,
        wspace=0.43,
        width_ratios=[1.0, 1.18, 1.0],
    )
    ax_a, ax_b, ax_c = [fig.add_subplot(grid[i]) for i in range(3)]
    quantile_envelope_panel(ax_a, data, "uac", "UAC (s)")
    quantile_envelope_panel(ax_c, data, "t100", "T100 (s)")

    for index, (raw, median, low, high) in enumerate(effects):
        y = 1 - index
        horizontal_density(ax_b, raw, y, color=DENSITY_BLUE, height=0.18)
        ax_b.scatter(
            raw,
            y + jitter(len(raw), 0.10),
            s=8,
            facecolor="white",
            edgecolor=POINT_EDGE,
            linewidth=0.5,
            zorder=2,
        )
        ax_b.plot([low, high], [y, y], color=TEXT, lw=0.95, zorder=3)
        ax_b.plot([low, low], [y - 0.07, y + 0.07], color=TEXT, lw=0.65)
        ax_b.plot([high, high], [y - 0.07, y + 0.07], color=TEXT, lw=0.65)
        ax_b.scatter([median], [y], s=15, facecolor=EFFECT, edgecolor="white", linewidth=0.3, zorder=4)
        ax_b.text(-7.25, y + 0.24, f"{median:.2f} [{low:.2f}, {high:.2f}]", fontsize=6.0, ha="left")
    ax_b.axvline(0, color=REFERENCE, lw=0.6, linestyle=(0, (2.2, 2.2)))
    ax_b.set_xlim(-7.4, 0.45)
    ax_b.set_ylim(-0.48, 1.48)
    ax_b.set_yticks([1, 0], ["vs MAG", "vs static"])
    ax_b.set_xlabel("ECR-Cover − baseline UAC (s)")
    clean_axis(ax_b)

    panel_label(ax_a, "a")
    panel_label(ax_b, "b", x=-0.13)
    panel_label(ax_c, "c")
    save_all(fig, "figure3_nominal", dpi=600)
    plt.close(fig)


if __name__ == "__main__":
    main()
