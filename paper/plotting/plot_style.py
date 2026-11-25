"""Shared, deliberately restrained style for manuscript data figures."""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


# Okabe-Ito-derived palette: color-blind safe and legible in print.
BLUE = "#0072B2"
VERMILION = "#D55E00"
GREEN = "#009E73"
MAGENTA = "#CC79A7"
GRAY = "#6B7280"
LIGHT_GRAY = "#B8C0C8"
GRID = "#D9DDE2"
BLACK = "#202124"


def apply_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [
                "Microsoft YaHei",
                "Noto Sans CJK SC",
                "SimHei",
                "Arial",
                "DejaVu Sans",
            ],
            "axes.unicode_minus": False,
            "font.size": 8.0,
            "axes.labelsize": 8.0,
            "axes.titlesize": 8.5,
            "xtick.labelsize": 7.4,
            "ytick.labelsize": 7.4,
            "legend.fontsize": 7.0,
            "axes.linewidth": 0.8,
            "lines.linewidth": 1.35,
            "figure.dpi": 180,
            "savefig.dpi": 450,
            "savefig.facecolor": "white",
            "savefig.bbox": "tight",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def empirical_cdf(values: list[float] | np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = np.sort(np.asarray(values, dtype=float))
    y = np.arange(1, x.size + 1, dtype=float) / x.size
    return x, y


def draw_ecdf(ax, values, *, label: str, color: str, marker: str = "") -> None:
    x, y = empirical_cdf(values)
    x_step = np.r_[x[0], x]
    y_step = np.r_[0.0, y]
    ax.step(x_step, y_step, where="post", color=color, label=label, zorder=3)
    if marker:
        ax.plot(x, y, linestyle="none", marker=marker, markersize=2.5,
                markerfacecolor="white", markeredgewidth=0.7,
                markeredgecolor=color, zorder=4)
    # A quiet rug retains the run-level observations without visual clutter.
    ax.plot(x, np.full_like(x, 0.018), "|", color=color, markersize=3.0,
            markeredgewidth=0.65, alpha=0.75, zorder=4)


def style_axis(ax, *, grid: str = "both", full_frame: bool = True) -> None:
    if full_frame:
        for spine in ax.spines.values():
            spine.set_visible(True)
            spine.set_color(BLACK)
            spine.set_linewidth(0.75)
    else:
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color(BLACK)
        ax.spines[["left", "bottom"]].set_linewidth(0.75)
    ax.tick_params(direction="out", length=3.0, width=0.7, color=BLACK)
    ax.grid(axis=grid, color=GRID, linewidth=0.5, alpha=0.80)
    ax.set_axisbelow(True)


def panel_title(ax, panel: str, title: str) -> None:
    ax.set_title(f"{panel} {title}", loc="left", fontweight="bold", pad=5)


def save_figure(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, pad_inches=0.025)
    plt.close(fig)

