"""Shared visual language for the AIS figures.

The figures are designed at their final physical width.  The palette is
color-blind friendly and every comparison is also distinguished by marker or
line style so that meaning is preserved in grayscale reproduction.
"""

from __future__ import annotations

import matplotlib as mpl


MM = 1.0 / 25.4

NAVY = "#274C77"
TEAL = "#188977"
ORANGE = "#D97706"
BLUE = "#3A6EA5"
GRAY = "#626D78"
LIGHT_GRAY = "#D7DDE3"
PALE = "#F3F5F6"
RED = "#B5473C"
BLACK = "#20262D"


def apply_style() -> None:
    mpl.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "DejaVu Sans"],
            "font.size": 8.0,
            "axes.titlesize": 8.5,
            "axes.labelsize": 8.0,
            "xtick.labelsize": 7.5,
            "ytick.labelsize": 7.5,
            "legend.fontsize": 7.2,
            "axes.linewidth": 0.7,
            "lines.linewidth": 1.35,
            "lines.markersize": 4.2,
            "xtick.major.width": 0.7,
            "ytick.major.width": 0.7,
            "xtick.major.size": 3.0,
            "ytick.major.size": 3.0,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": BLACK,
            "axes.labelcolor": BLACK,
            "xtick.color": BLACK,
            "ytick.color": BLACK,
            "text.color": BLACK,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "savefig.bbox": "tight",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
        }
    )


def panel_label(ax, label: str, x: float = -0.14, y: float = 1.06) -> None:
    shown = label if label.startswith("(") else f"({label})"
    ax.text(
        x,
        y,
        shown,
        transform=ax.transAxes,
        fontsize=9.0,
        fontweight="bold",
        ha="left",
        va="bottom",
    )


def clean_axis(ax, grid_axis: str | None = "y") -> None:
    if grid_axis:
        ax.grid(axis=grid_axis, color=LIGHT_GRAY, linewidth=0.55, alpha=0.8)
        ax.set_axisbelow(True)

