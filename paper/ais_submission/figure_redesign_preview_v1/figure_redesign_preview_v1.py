from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np


HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "plotting" / "data"

COLORS = {
    "mag": "#66717E",
    "measured_static": "#C87900",
    "uac_robust_repair_v2": "#00897B",
    "blue": "#31688E",
    "adverse": "#D97706",
    "grid": "#D9E0E7",
    "text": "#20252B",
    "muted": "#89939E",
}

METHODS = ["mag", "measured_static", "uac_robust_repair_v2"]
METHOD_LABELS = ["MAG", "Calibrated\nstatic", "ECR-Cover"]


def configure_style() -> None:
    mpl.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 7.8,
            "axes.titlesize": 8.6,
            "axes.titleweight": "semibold",
            "axes.labelsize": 7.8,
            "xtick.labelsize": 7.2,
            "ytick.labelsize": 7.2,
            "axes.edgecolor": COLORS["text"],
            "axes.linewidth": 0.7,
            "xtick.major.width": 0.7,
            "ytick.major.width": 0.7,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def tidy_axis(ax: plt.Axes, *, grid: str = "x") -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(length=3, color=COLORS["text"])
    if grid == "x":
        ax.grid(axis="x", color=COLORS["grid"], linewidth=0.55, zorder=0)
    elif grid == "y":
        ax.grid(axis="y", color=COLORS["grid"], linewidth=0.55, zorder=0)


def deterministic_jitter(n: int, width: float = 0.10) -> np.ndarray:
    if n == 1:
        return np.zeros(1)
    pattern = np.array([0, -1, 1, -0.5, 0.5, -1.5, 1.5, -0.25, 0.25, 0.75])
    if n <= len(pattern):
        return pattern[:n] / 1.5 * width
    return np.linspace(-width, width, n)


def strip_summary(
    ax: plt.Axes,
    values: np.ndarray,
    y: float,
    color: str,
    *,
    point_size: float = 18,
) -> None:
    vals = np.sort(np.asarray(values, dtype=float))
    yj = y + deterministic_jitter(len(vals), 0.13)
    ax.scatter(
        vals,
        yj,
        s=point_size,
        facecolor=color,
        edgecolor="white",
        linewidth=0.45,
        alpha=0.88,
        zorder=4,
    )
    q1, med, q3 = np.quantile(vals, [0.25, 0.50, 0.75])
    ax.plot([vals.min(), vals.max()], [y, y], color=color, linewidth=0.75, alpha=0.75, zorder=2)
    ax.plot([q1, q3], [y, y], color=color, linewidth=3.2, solid_capstyle="round", zorder=3)
    ax.scatter([med], [y], s=25, marker="D", facecolor="white", edgecolor=color, linewidth=1.2, zorder=5)


def panel_label(ax: plt.Axes, label: str, x: float = -0.17) -> None:
    ax.text(x, 1.08, label, transform=ax.transAxes, fontsize=9.5, fontweight="bold", va="top")


def load_nominal() -> dict[str, dict[str, np.ndarray]]:
    rows = [r for r in read_csv(DATA / "formal_run_metrics_wide.csv") if r["condition"] == "nominal"]
    result: dict[str, dict[str, np.ndarray]] = {}
    for method in METHODS:
        mr = sorted((r for r in rows if r["method"] == method), key=lambda r: int(r["block"]))
        result[method] = {
            "uac": np.array([float(r["uac_h_s"]) for r in mr]),
            "t100": np.array([float(r["t100_s"]) for r in mr]),
            "block": np.array([int(r["block"]) for r in mr]),
        }
    return result


def draw_figure3() -> None:
    nominal = load_nominal()
    diff_mag = nominal["uac_robust_repair_v2"]["uac"] - nominal["mag"]["uac"]
    diff_static = nominal["uac_robust_repair_v2"]["uac"] - nominal["measured_static"]["uac"]

    frozen = [
        ("vs MAG", diff_mag, -4.9413626957, -6.7071104255, -3.9104763393),
        ("vs calibrated\nstatic", diff_static, -0.2654152243, -1.0463878434, -0.1104119954),
    ]

    fig = plt.figure(figsize=(7.00, 2.72), constrained_layout=False)
    gs = fig.add_gridspec(1, 3, left=0.115, right=0.985, bottom=0.20, top=0.87, wspace=0.47, width_ratios=[1.0, 1.0, 1.24])
    axes = [fig.add_subplot(gs[0, i]) for i in range(3)]

    for ax, metric, xlabel, title in [
        (axes[0], "uac", "UAC (s)", "First-coverage latency"),
        (axes[1], "t100", "T100 (s)", "Completion tail"),
    ]:
        for idx, method in enumerate(METHODS):
            y = 2 - idx
            strip_summary(ax, nominal[method][metric], y, COLORS[method])
        ax.set_yticks([2, 1, 0], METHOD_LABELS)
        ax.set_ylim(-0.45, 2.45)
        ax.set_xlabel(xlabel)
        ax.set_title(title, pad=8)
        tidy_axis(ax, grid="x")
        ax.annotate(
            "lower is better",
            xy=(0.98, 1.01),
            xytext=(0.49, 1.01),
            xycoords="axes fraction",
            textcoords="axes fraction",
            ha="center",
            va="bottom",
            fontsize=6.8,
            color=COLORS["muted"],
            arrowprops=dict(arrowstyle="-|>", color=COLORS["muted"], lw=0.65),
        )

    ax = axes[2]
    for idx, (label, raw, med, lo, hi) in enumerate(frozen):
        y = 1 - idx
        jitter = deterministic_jitter(len(raw), 0.15)
        ax.scatter(raw, y + jitter, s=17, facecolor="#B8C1CA", edgecolor="white", linewidth=0.4, zorder=3)
        ax.plot([lo, hi], [y, y], color=COLORS["blue"], lw=1.35, zorder=4)
        ax.plot([lo, lo], [y - 0.11, y + 0.11], color=COLORS["blue"], lw=1.0, zorder=4)
        ax.plot([hi, hi], [y - 0.11, y + 0.11], color=COLORS["blue"], lw=1.0, zorder=4)
        ax.scatter([med], [y], s=37, marker="D", facecolor=COLORS["uac_robust_repair_v2"], edgecolor="white", linewidth=0.7, zorder=5)
        ax.text(
            -7.35,
            y + 0.27,
            f"{med:.2f}  [{lo:.2f}, {hi:.2f}]",
            fontsize=6.7,
            color=COLORS["text"],
            va="bottom",
        )
    ax.axvline(0, color=COLORS["muted"], lw=0.85, linestyle=(0, (3, 2)), zorder=1)
    ax.set_xlim(-7.5, 0.55)
    ax.set_ylim(-0.50, 1.50)
    ax.set_yticks([1, 0], [x[0] for x in frozen])
    ax.set_xlabel("Paired UAC difference (s)")
    ax.set_title("ECR-Cover effect", pad=8)
    tidy_axis(ax, grid="x")
    ax.text(0, 1.02, "no difference", transform=ax.get_xaxis_transform(), ha="right", va="bottom", fontsize=6.7, color=COLORS["muted"])

    for label, ax in zip(["(a)", "(b)", "(c)"], axes):
        panel_label(ax, label)

    for ext in ["png", "pdf", "svg"]:
        fig.savefig(HERE / f"figure3_redesign_concept_v1.{ext}", dpi=320 if ext == "png" else None)
    plt.close(fig)


def load_repair() -> list[dict[str, str]]:
    return sorted(read_csv(DATA / "paired_repair_effects.csv"), key=lambda r: int(r["block"]))


def draw_figure4() -> None:
    repair = load_repair()
    uac_off = np.array([float(r["uac_off_s"]) for r in repair])
    uac_on = np.array([float(r["uac_on_s"]) for r in repair])
    uac_diff = np.array([float(r["uac_on_minus_off_s"]) for r in repair])
    ratios = np.array([float(r["t100_on_over_off"]) for r in repair])

    with (DATA / "V243_COMPLETE_AUDIT.json").open("r", encoding="utf-8") as f:
        audit = json.load(f)
    initial_ms = np.array([float(r["initial_planner_ms"]) for r in audit["runs"]])
    repair_ms = np.array([float(v) for r in audit["runs"] for v in r["repair_planner_ms"]])

    fig = plt.figure(figsize=(7.00, 2.82), constrained_layout=False)
    outer = fig.add_gridspec(1, 3, left=0.075, right=0.985, bottom=0.20, top=0.86, wspace=0.48, width_ratios=[1.52, 0.90, 1.08])
    inner = outer[0].subgridspec(1, 2, wspace=0.24, width_ratios=[1.15, 0.70])
    ax_pair = fig.add_subplot(inner[0, 0])
    ax_delta = fig.add_subplot(inner[0, 1])
    ax_ratio = fig.add_subplot(outer[1])
    ax_budget = fig.add_subplot(outer[2])

    # (a) Paired raw observations and estimation view.
    for off, on, diff in zip(uac_off, uac_on, uac_diff):
        color = COLORS["adverse"] if diff > 0 else COLORS["uac_robust_repair_v2"]
        ax_pair.plot([0, 1], [off, on], color=color, lw=1.0, alpha=0.77, zorder=2)
        ax_pair.scatter([0], [off], s=20, facecolor=COLORS["mag"], edgecolor="white", linewidth=0.45, zorder=3)
        ax_pair.scatter([1], [on], s=23, facecolor=color, edgecolor="white", linewidth=0.45, zorder=3)
    ax_pair.set_xlim(-0.18, 1.18)
    ax_pair.set_ylim(2.70, 7.10)
    ax_pair.set_xticks([0, 1], ["Repair off", "Repair on"], fontsize=6.9)
    ax_pair.set_ylabel("UAC (s)")
    tidy_axis(ax_pair, grid="y")
    ax_pair.spines["bottom"].set_visible(False)
    ax_pair.tick_params(axis="x", length=0)

    yj = uac_diff + deterministic_jitter(len(uac_diff), 0.08)
    adverse = uac_diff > 0
    ax_delta.scatter(np.zeros((~adverse).sum()), yj[~adverse], s=17, facecolor=COLORS["uac_robust_repair_v2"], edgecolor="white", linewidth=0.35, alpha=0.86)
    ax_delta.scatter(np.zeros(adverse.sum()), yj[adverse], s=19, facecolor=COLORS["adverse"], edgecolor="white", linewidth=0.35, alpha=0.95)
    med = float(np.median(uac_diff))
    # Frozen bootstrap interval used in the manuscript.
    lo, hi = -2.496, -1.821
    ax_delta.plot([0.50, 0.50], [lo, hi], color=COLORS["blue"], lw=1.35)
    ax_delta.plot([0.43, 0.57], [lo, lo], color=COLORS["blue"], lw=1.0)
    ax_delta.plot([0.43, 0.57], [hi, hi], color=COLORS["blue"], lw=1.0)
    ax_delta.scatter([0.50], [med], s=34, marker="D", facecolor=COLORS["blue"], edgecolor="white", linewidth=0.6, zorder=4)
    ax_delta.axhline(0, color=COLORS["muted"], lw=0.8, linestyle=(0, (3, 2)))
    ax_delta.set_xlim(-0.24, 0.75)
    ax_delta.set_ylim(2.70 - np.median(uac_off), 7.10 - np.median(uac_off))
    ax_delta.set_xticks([0, 0.50], ["Blocks", "Median\n95% CI"], fontsize=6.6)
    ax_delta.set_title("Repair-on minus off (s)\n−1.98 [−2.50, −1.82]", fontsize=6.6, pad=6, fontweight="normal")
    tidy_axis(ax_delta, grid="y")
    ax_delta.spines["bottom"].set_visible(False)
    ax_delta.tick_params(axis="x", length=0)

    # (b) All block-level ratios plus frozen noninferiority interval.
    y_raw = 0.25 + deterministic_jitter(len(ratios), 0.11)
    ax_ratio.scatter(ratios, y_raw, s=19, facecolor=COLORS["blue"], edgecolor="white", linewidth=0.45, alpha=0.88, zorder=3)
    ratio_med, ratio_lo, ratio_hi = 0.9980111963, 0.9541716411, 1.0000639547
    ax_ratio.plot([ratio_lo, ratio_hi], [0.78, 0.78], color=COLORS["uac_robust_repair_v2"], lw=1.45, zorder=4)
    ax_ratio.plot([ratio_lo, ratio_lo], [0.70, 0.86], color=COLORS["uac_robust_repair_v2"], lw=1.0)
    ax_ratio.plot([ratio_hi, ratio_hi], [0.70, 0.86], color=COLORS["uac_robust_repair_v2"], lw=1.0)
    ax_ratio.scatter([ratio_med], [0.78], s=37, marker="D", facecolor=COLORS["uac_robust_repair_v2"], edgecolor="white", linewidth=0.6, zorder=5)
    ax_ratio.axvline(1.00, color=COLORS["muted"], lw=0.85, linestyle=(0, (3, 2)))
    ax_ratio.axvline(1.05, color="#C2413B", lw=0.9, linestyle=(0, (1.5, 2)))
    ax_ratio.text(1.00, 0.90, "1.00", transform=ax_ratio.get_xaxis_transform(), ha="right", va="top", fontsize=6.2, color=COLORS["muted"])
    ax_ratio.text(1.05, 0.98, "1.05 NI margin", transform=ax_ratio.get_xaxis_transform(), ha="right", va="top", fontsize=6.2, color="#C2413B")
    ax_ratio.text(0.905, 0.82, "0.998 [0.954, 1.000]", fontsize=6.2, va="bottom", color=COLORS["uac_robust_repair_v2"])
    ax_ratio.set_xlim(0.90, 1.065)
    ax_ratio.set_ylim(0.0, 1.0)
    ax_ratio.set_yticks([])
    ax_ratio.set_xlabel("T100 ratio: repair on/off")
    ax_ratio.set_title("T100 noninferiority", pad=10)
    tidy_axis(ax_ratio, grid="x")

    # (c) Normalized headroom, with real-time medians printed directly.
    runtime_rows = [
        ("Initial", initial_ms / 200.0, initial_ms, COLORS["blue"], 200),
        ("Repair", repair_ms / 40.0, repair_ms, COLORS["adverse"], 40),
    ]
    for idx, (label, norm, raw, color, budget) in enumerate(runtime_rows):
        y = 1 - idx
        vals = np.sort(norm)
        ax_budget.scatter(vals, y + deterministic_jitter(len(vals), 0.16), s=15, facecolor=color, edgecolor="white", linewidth=0.35, alpha=0.78, zorder=3)
        q1, median, q3 = np.quantile(vals, [0.25, 0.5, 0.75])
        ax_budget.plot([vals.min(), vals.max()], [y, y], color=color, lw=0.75, alpha=0.75)
        ax_budget.plot([q1, q3], [y, y], color=color, lw=3.0, solid_capstyle="round")
        ax_budget.scatter([median], [y], s=24, marker="D", facecolor="white", edgecolor=color, linewidth=1.05, zorder=4)
        ax_budget.text(
            min(max(vals.max() + 0.035, 0.36), 0.72),
            y,
            f"median {np.median(raw):.2f} ms",
            fontsize=6.4,
            color=color,
            va="center",
        )
    ax_budget.axvline(1.0, color="#C2413B", lw=0.9, linestyle=(0, (1.5, 2)))
    ax_budget.text(1.0, 0.99, "budget", transform=ax_budget.get_xaxis_transform(), ha="right", va="top", fontsize=6.4, color="#C2413B")
    ax_budget.set_xlim(0, 1.04)
    ax_budget.set_ylim(-0.45, 1.45)
    ax_budget.set_yticks([1, 0], ["Initial plan\n(200 ms)", "Event repair\n(40 ms)"], fontsize=6.8)
    ax_budget.set_xlabel("Observed time / budget")
    ax_budget.set_title("Planning-time headroom", pad=9)
    tidy_axis(ax_budget, grid="x")

    panel_label(ax_pair, "(a)", x=-0.28)
    panel_label(ax_ratio, "(b)", x=-0.18)
    panel_label(ax_budget, "(c)", x=-0.20)
    fig.text(0.075, 0.945, "Repair effect on first coverage", fontsize=8.6, fontweight="semibold", color=COLORS["text"])

    for ext in ["png", "pdf", "svg"]:
        fig.savefig(HERE / f"figure4_redesign_concept_v1.{ext}", dpi=320 if ext == "png" else None)
    plt.close(fig)


def main() -> None:
    configure_style()
    draw_figure3()
    draw_figure4()


if __name__ == "__main__":
    main()
