"""Publication style and envelope helpers for Figures 3 and 4.

Shaded quantile envelopes are descriptive summaries of run-level variation.
Kernel silhouettes are descriptive density estimates.  Neither is a confidence
interval; confidence intervals are drawn separately with capped black lines.
"""

from __future__ import annotations

import matplotlib as mpl
import numpy as np


MM = 1.0 / 25.4

# Muted, colour-blind-friendly palette chosen for white journal pages.
MAG = "#56616B"
STATIC = "#D38A2C"
ECR = "#168982"
EFFECT = "#356F95"
ADVERSE = "#B64A3A"
TEXT = "#22282F"
REFERENCE = "#7B8187"
POINT_EDGE = "#858B90"
BAND_OUTER = "#EFF6F4"
BAND_INNER = "#D6E9E5"
DENSITY_BLUE = "#DBE8F0"
DENSITY_GREY = "#E7E8E9"


def apply_envelope_style() -> None:
    mpl.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
            "font.size": 7.0,
            "axes.labelsize": 7.0,
            "axes.titlesize": 7.0,
            "xtick.labelsize": 6.5,
            "ytick.labelsize": 6.5,
            "legend.fontsize": 6.5,
            "axes.linewidth": 0.5,
            "axes.edgecolor": TEXT,
            "axes.labelcolor": TEXT,
            "xtick.color": TEXT,
            "ytick.color": TEXT,
            "text.color": TEXT,
            "lines.linewidth": 0.85,
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


def clean_axis(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def panel_label(ax, value: str, x: float = -0.16) -> None:
    ax.text(
        x,
        1.04,
        value,
        transform=ax.transAxes,
        ha="left",
        va="bottom",
        fontsize=8.0,
        fontweight="bold",
    )


def jitter(n: int, width: float) -> np.ndarray:
    pattern = np.array([0, -0.55, 0.55, -1, 1, -0.27, 0.27, -0.78, 0.78, 0.42], dtype=float)
    if n <= len(pattern):
        return pattern[:n] * width
    return np.linspace(-width, width, n)


def smooth_between(values: np.ndarray, points_per_segment: int = 100) -> tuple[np.ndarray, np.ndarray]:
    """Cosine interpolation used only as a visual connector between categories."""
    values = np.asarray(values, dtype=float)
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    for i in range(len(values) - 1):
        t = np.linspace(0.0, 1.0, points_per_segment, endpoint=i == len(values) - 2)
        weight = 0.5 - 0.5 * np.cos(np.pi * t)
        xs.append(i + t)
        ys.append(values[i] * (1.0 - weight) + values[i + 1] * weight)
    return np.concatenate(xs), np.concatenate(ys)


def density_curve(
    values: np.ndarray,
    *,
    lower: float | None = None,
    upper: float | None = None,
    count: int = 240,
) -> tuple[np.ndarray, np.ndarray]:
    """Return a normalized one-dimensional Gaussian-kernel density curve."""
    values = np.asarray(values, dtype=float)
    spread = max(float(np.ptp(values)), 1e-6)
    lo = float(values.min() - 0.10 * spread) if lower is None else lower
    hi = float(values.max() + 0.10 * spread) if upper is None else upper
    grid = np.linspace(lo, hi, count)
    sd = float(np.std(values, ddof=1)) if len(values) > 1 else spread
    q75, q25 = np.quantile(values, [0.75, 0.25])
    robust = float((q75 - q25) / 1.349) if q75 > q25 else sd
    scale = min(sd, robust) if sd > 0 and robust > 0 else max(sd, spread / 5)
    bandwidth = max(0.90 * scale * len(values) ** (-0.2), spread / 30, 1e-5)
    density = np.exp(-0.5 * ((grid[:, None] - values[None, :]) / bandwidth) ** 2).sum(axis=1)
    if density.max() > 0:
        density = density / density.max()
    return grid, density


def horizontal_density(ax, values: np.ndarray, y: float, *, color: str, height: float, lower=None, upper=None) -> None:
    grid, density = density_curve(values, lower=lower, upper=upper)
    ax.fill_between(grid, y - density * height, y + density * height, color=color, linewidth=0, zorder=0)


def vertical_density(ax, values: np.ndarray, x: float, *, color: str, width: float) -> None:
    grid, density = density_curve(values)
    ax.fill_betweenx(grid, x - density * width, x + density * width, color=color, linewidth=0, zorder=0)
