from __future__ import annotations

import importlib.util
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


HERE = Path(__file__).resolve().parent
V2_SCRIPT = HERE.parent / "figure_redesign_preview_v2_github_style" / "plot_preview_v2.py"
SPEC = importlib.util.spec_from_file_location("preview_v2", V2_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)


PALE = "#DCEBEA"
PALE_OUTER = "#EEF5F4"
PALE_BLUE = "#DCE8EF"
PALE_GREY = "#E8E8E8"


def smooth_between(values: np.ndarray, points_per_segment: int = 100) -> tuple[np.ndarray, np.ndarray]:
    """Cosine interpolation used only to draw a smooth connector between categories."""
    values = np.asarray(values, dtype=float)
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    for i in range(len(values) - 1):
        t = np.linspace(0.0, 1.0, points_per_segment, endpoint=i == len(values) - 2)
        s = 0.5 - 0.5 * np.cos(np.pi * t)
        xs.append(i + t)
        ys.append(values[i] * (1.0 - s) + values[i + 1] * s)
    return np.concatenate(xs), np.concatenate(ys)


def density_envelope(
    ax: plt.Axes,
    values: np.ndarray,
    y: float,
    *,
    color: str,
    height: float = 0.18,
    xmin: float | None = None,
    xmax: float | None = None,
) -> None:
    """Draw a descriptive Gaussian-kernel silhouette; it is not a confidence interval."""
    v = np.asarray(values, dtype=float)
    spread = max(float(np.ptp(v)), 1e-6)
    lo = float(v.min() - 0.10 * spread) if xmin is None else xmin
    hi = float(v.max() + 0.10 * spread) if xmax is None else xmax
    grid = np.linspace(lo, hi, 240)
    sd = float(np.std(v, ddof=1)) if len(v) > 1 else spread
    q75, q25 = np.quantile(v, [0.75, 0.25])
    robust = float((q75 - q25) / 1.349) if q75 > q25 else sd
    scale = min(sd, robust) if sd > 0 and robust > 0 else max(sd, spread / 5)
    bw = max(0.90 * scale * len(v) ** (-0.2), spread / 30, 1e-5)
    density = np.exp(-0.5 * ((grid[:, None] - v[None, :]) / bw) ** 2).sum(axis=1)
    if density.max() > 0:
        density = density / density.max() * height
    ax.fill_between(grid, y - density, y + density, color=color, linewidth=0, zorder=0)


def vertical_density_envelope(
    ax: plt.Axes,
    values: np.ndarray,
    x: float,
    *,
    color: str,
    width: float = 0.13,
) -> None:
    """Vertical counterpart of density_envelope for effects plotted on the y-axis."""
    v = np.asarray(values, dtype=float)
    spread = max(float(np.ptp(v)), 1e-6)
    grid = np.linspace(float(v.min() - 0.10 * spread), float(v.max() + 0.10 * spread), 240)
    sd = float(np.std(v, ddof=1)) if len(v) > 1 else spread
    q75, q25 = np.quantile(v, [0.75, 0.25])
    robust = float((q75 - q25) / 1.349) if q75 > q25 else sd
    scale = min(sd, robust) if sd > 0 and robust > 0 else max(sd, spread / 5)
    bw = max(0.90 * scale * len(v) ** (-0.2), spread / 30, 1e-5)
    density = np.exp(-0.5 * ((grid[:, None] - v[None, :]) / bw) ** 2).sum(axis=1)
    if density.max() > 0:
        density = density / density.max() * width
    ax.fill_betweenx(grid, x - density, x + density, color=color, linewidth=0, zorder=0)


def quantile_envelope_panel(
    ax: plt.Axes,
    data: dict[str, dict[str, np.ndarray]],
    metric: str,
    ylabel: str,
) -> None:
    matrix = np.column_stack([data[m][metric] for m in base.METHODS])
    q10, q25, med, q75, q90 = np.quantile(matrix, [0.10, 0.25, 0.50, 0.75, 0.90], axis=0)
    x, q10s = smooth_between(q10)
    _, q25s = smooth_between(q25)
    _, meds = smooth_between(med)
    _, q75s = smooth_between(q75)
    _, q90s = smooth_between(q90)
    ax.fill_between(x, q10s, q90s, color=PALE_OUTER, linewidth=0, zorder=0)
    ax.fill_between(x, q25s, q75s, color=PALE, linewidth=0, zorder=1)
    ax.plot(x, meds, color=base.TEXT, lw=0.9, zorder=2)
    for idx, color in enumerate(base.METHOD_COLORS):
        xj = idx + base.jitter(matrix.shape[0], 0.055)
        ax.scatter(
            xj,
            matrix[:, idx],
            s=10,
            facecolor=color,
            edgecolor="white",
            linewidth=0.3,
            zorder=3,
        )
    ax.set_xlim(-0.32, 2.32)
    ax.set_xticks(np.arange(3), base.METHOD_LABELS)
    ax.set_ylabel(ylabel)
    base.clean(ax)


def figure3() -> None:
    data = base.nominal_data()
    d_mag = data["uac_robust_repair_v2"]["uac"] - data["mag"]["uac"]
    d_static = data["uac_robust_repair_v2"]["uac"] - data["measured_static"]["uac"]
    effects = [
        (d_mag, -4.9413626957, -6.7071104255, -3.9104763393),
        (d_static, -0.2654152243, -1.0463878434, -0.1104119954),
    ]

    fig = plt.figure(figsize=(7.00, 2.35))
    gs = fig.add_gridspec(1, 3, left=0.065, right=0.993, bottom=0.23, top=0.94, wspace=0.43, width_ratios=[1.0, 1.18, 1.0])
    ax_a, ax_b, ax_c = [fig.add_subplot(gs[i]) for i in range(3)]
    quantile_envelope_panel(ax_a, data, "uac", "UAC (s)")
    quantile_envelope_panel(ax_c, data, "t100", "T100 (s)")

    for i, (raw, med, lo, hi) in enumerate(effects):
        y = 1 - i
        density_envelope(ax_b, raw, y, color=PALE_BLUE, height=0.18)
        ax_b.scatter(raw, y + base.jitter(len(raw), 0.10), s=8, facecolor="white", edgecolor="#8E8E8E", linewidth=0.5, zorder=2)
        ax_b.plot([lo, hi], [y, y], color=base.TEXT, lw=0.95, zorder=3)
        ax_b.plot([lo, lo], [y - 0.07, y + 0.07], color=base.TEXT, lw=0.65)
        ax_b.plot([hi, hi], [y - 0.07, y + 0.07], color=base.TEXT, lw=0.65)
        ax_b.scatter([med], [y], s=14, facecolor=base.ECR, edgecolor="white", linewidth=0.3, zorder=4)
        ax_b.text(-7.25, y + 0.24, f"{med:.2f} [{lo:.2f}, {hi:.2f}]", fontsize=6.0, ha="left", va="bottom")
    ax_b.axvline(0, color=base.REF, lw=0.6, linestyle=(0, (2.2, 2.2)))
    ax_b.set_xlim(-7.4, 0.45)
    ax_b.set_ylim(-0.48, 1.48)
    ax_b.set_yticks([1, 0], ["vs MAG", "vs static"])
    ax_b.set_xlabel("ECR-Cover − baseline UAC (s)")
    base.clean(ax_b)

    base.letter(ax_a, "a")
    base.letter(ax_b, "b", x=-0.13)
    base.letter(ax_c, "c")
    for ext in ("png", "pdf", "svg"):
        fig.savefig(HERE / f"figure3_concept_v3.{ext}", dpi=600 if ext == "png" else None)
    plt.close(fig)


def repair_summary_panel(ax: plt.Axes, off: np.ndarray, on: np.ndarray, delta: np.ndarray) -> None:
    matrix = np.column_stack([off, on])
    q10, q25, med, q75, q90 = np.quantile(matrix, [0.10, 0.25, 0.50, 0.75, 0.90], axis=0)
    x, q10s = smooth_between(q10, 180)
    _, q25s = smooth_between(q25, 180)
    _, meds = smooth_between(med, 180)
    _, q75s = smooth_between(q75, 180)
    _, q90s = smooth_between(q90, 180)
    ax.fill_between(x, q10s, q90s, color=PALE_OUTER, linewidth=0)
    ax.fill_between(x, q25s, q75s, color=PALE, linewidth=0)
    ax.plot(x, meds, color=base.ECR, lw=1.0)

    ax.scatter(0 + base.jitter(len(off), 0.045), off, s=10, facecolor="white", edgecolor=base.MAG, linewidth=0.5, zorder=3)
    colours = np.where(delta > 0, base.ADVERSE, base.ECR)
    ax.scatter(1 + base.jitter(len(on), 0.045), on, s=10, facecolor=colours, edgecolor="white", linewidth=0.3, zorder=3)
    ax.set_xlim(-0.24, 1.24)
    ax.set_xticks([0, 1], ["Repair off", "Repair on"])
    ax.set_ylabel("UAC (s)")
    base.clean(ax)


def figure4() -> None:
    repair = base.repair_data()
    off = np.array([float(r["uac_off_s"]) for r in repair])
    on = np.array([float(r["uac_on_s"]) for r in repair])
    delta = np.array([float(r["uac_on_minus_off_s"]) for r in repair])
    ratio = np.array([float(r["t100_on_over_off"]) for r in repair])
    with (base.DATA / "V243_COMPLETE_AUDIT.json").open("r", encoding="utf-8") as f:
        audit = base.json.load(f)
    initial_ms = np.array([float(r["initial_planner_ms"]) for r in audit["runs"]])
    event_ms = np.array([float(v) for r in audit["runs"] for v in r["repair_planner_ms"]])

    fig = plt.figure(figsize=(7.00, 2.42))
    outer = fig.add_gridspec(1, 3, left=0.065, right=0.993, bottom=0.23, top=0.94, wspace=0.45, width_ratios=[1.52, 0.90, 1.05])
    inner = outer[0].subgridspec(1, 2, wspace=0.28, width_ratios=[1.05, 0.72])
    ax_pair = fig.add_subplot(inner[0])
    ax_effect = fig.add_subplot(inner[1])
    ax_ratio = fig.add_subplot(outer[1])
    ax_time = fig.add_subplot(outer[2])

    repair_summary_panel(ax_pair, off, on, delta)

    vertical_density_envelope(ax_effect, delta, 0, color=PALE_GREY, width=0.13)
    pos = delta > 0
    ax_effect.scatter(np.zeros((~pos).sum()), delta[~pos] + base.jitter((~pos).sum(), 0.04), s=8, facecolor="white", edgecolor="#888888", linewidth=0.5)
    ax_effect.scatter(np.zeros(pos.sum()), delta[pos], s=10, facecolor=base.ADVERSE, edgecolor="white", linewidth=0.3, zorder=3)
    med, lo, hi = -1.979, -2.496, -1.821
    ax_effect.plot([0.58, 0.58], [lo, hi], color=base.TEXT, lw=0.95)
    ax_effect.plot([0.52, 0.64], [lo, lo], color=base.TEXT, lw=0.65)
    ax_effect.plot([0.52, 0.64], [hi, hi], color=base.TEXT, lw=0.65)
    ax_effect.scatter([0.58], [med], s=14, facecolor=base.TEXT, edgecolor="white", linewidth=0.3)
    ax_effect.axhline(0, color=base.REF, lw=0.6, linestyle=(0, (2.2, 2.2)))
    ax_effect.set_xlim(-0.18, 0.76)
    ax_effect.set_ylim(-2.75, 1.60)
    ax_effect.set_xticks([0, 0.58], ["Blocks", "Median\n95% CI"])
    ax_effect.set_ylabel("Repair-on − off (s)", labelpad=2)
    base.clean(ax_effect)

    density_envelope(ax_ratio, ratio, 1, color=PALE_BLUE, height=0.22, xmin=0.90, xmax=1.055)
    ax_ratio.scatter(ratio, 1 + base.jitter(len(ratio), 0.11), s=8, facecolor="white", edgecolor="#707070", linewidth=0.5, zorder=2)
    rmed, rlo, rhi = 0.9980111963, 0.9541716411, 1.0000639547
    ax_ratio.plot([rlo, rhi], [0, 0], color=base.TEXT, lw=0.95)
    ax_ratio.plot([rlo, rlo], [-0.07, 0.07], color=base.TEXT, lw=0.65)
    ax_ratio.plot([rhi, rhi], [-0.07, 0.07], color=base.TEXT, lw=0.65)
    ax_ratio.scatter([rmed], [0], s=14, facecolor=base.TEXT, edgecolor="white", linewidth=0.3)
    ax_ratio.axvline(1.00, color=base.REF, lw=0.65, linestyle="--")
    ax_ratio.axvline(1.05, color=base.REF, lw=0.65, linestyle=":")
    ax_ratio.set_xlim(0.90, 1.062)
    ax_ratio.set_ylim(-0.45, 1.45)
    ax_ratio.set_yticks([1, 0], ["Blocks", "Median\n95% CI"])
    ax_ratio.set_xlabel("T100 ratio (on/off)")
    ax_ratio.text(1.00, 1.42, "1.00", fontsize=5.8, ha="right", va="top", color=base.REF)
    ax_ratio.text(1.05, 1.42, "1.05", fontsize=5.8, ha="right", va="top", color=base.REF)
    base.clean(ax_ratio)

    norm_initial = initial_ms / 200.0
    norm_event = event_ms / 40.0
    for y, values, colour, face in [
        (1, norm_initial, PALE_BLUE, "white"),
        (0, norm_event, PALE_GREY, "#777777"),
    ]:
        density_envelope(ax_time, values, y, color=colour, height=0.20, xmin=0, xmax=0.62)
        ax_time.scatter(values, y + base.jitter(len(values), 0.10), s=7.5, facecolor=face, edgecolor="#666666", linewidth=0.4, zorder=2)
        medv = float(np.median(values))
        ax_time.plot([medv, medv], [y - 0.14, y + 0.14], color=base.TEXT, lw=1.0, zorder=3)
    ax_time.axvline(1.0, color=base.REF, lw=0.65, linestyle=":")
    ax_time.set_xlim(0, 1.04)
    ax_time.set_ylim(-0.42, 1.42)
    ax_time.set_yticks([1, 0], ["Initial\n(200 ms)", "Repair\n(40 ms)"])
    ax_time.set_xlabel("Planning time / budget")
    ax_time.text(1.0, 1.39, "budget", fontsize=5.8, ha="right", va="top", color=base.REF)
    base.clean(ax_time)

    base.letter(ax_pair, "a", x=-0.26)
    base.letter(ax_ratio, "b", x=-0.18)
    base.letter(ax_time, "c", x=-0.18)
    for ext in ("png", "pdf", "svg"):
        fig.savefig(HERE / f"figure4_concept_v3.{ext}", dpi=600 if ext == "png" else None)
    plt.close(fig)


def main() -> None:
    base.set_style()
    figure3()
    figure4()


if __name__ == "__main__":
    main()
