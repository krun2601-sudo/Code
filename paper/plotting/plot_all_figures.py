"""Compatibility entry point for all manuscript figures.

The editable data-figure code now lives in ``paper/plotting``.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Polygon

from .build_all import main as build_data_figures


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
OUT = ROOT / "figures"
OUT.mkdir(parents=True, exist_ok=True)

FORMAL = PROJECT / "private" / "v2_20261020" / "formal_v242" / "analysis"
REPAIR = PROJECT / "private" / "v2_20261020" / "repair_revalidation_v243"

NAVY = "#173F5F"
BLUE = "#4C78A8"
TEAL = "#2A9D8F"
AMBER = "#E9C46A"
ORANGE = "#F28E2B"
RED = "#D55E00"
SLATE = "#5B6573"
LIGHT = "#F3F6F8"
GRID = "#D7DEE5"


def configure() -> None:
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [
                "Microsoft YaHei",
                "Noto Sans CJK SC",
                "SimHei",
                "DejaVu Sans",
            ],
            "axes.unicode_minus": False,
            "font.size": 8.2,
            "axes.labelsize": 8.2,
            "axes.titlesize": 9.0,
            "xtick.labelsize": 7.6,
            "ytick.labelsize": 7.6,
            "legend.fontsize": 7.2,
            "figure.dpi": 160,
            "savefig.dpi": 360,
            "savefig.bbox": "tight",
        }
    )


def rounded_box(ax, xy, wh, text, face=LIGHT, edge=NAVY, size=8.0, lw=1.1):
    patch = FancyBboxPatch(
        xy,
        wh[0],
        wh[1],
        boxstyle="round,pad=0.012,rounding_size=0.018",
        facecolor=face,
        edgecolor=edge,
        linewidth=lw,
    )
    ax.add_patch(patch)
    ax.text(
        xy[0] + wh[0] / 2,
        xy[1] + wh[1] / 2,
        text,
        ha="center",
        va="center",
        fontsize=size,
        color=NAVY,
        linespacing=1.28,
    )
    return patch


def arrow(ax, start, end, text="", color=SLATE, rad=0.0, style="-"):
    item = FancyArrowPatch(
        start,
        end,
        arrowstyle="-|>",
        mutation_scale=10,
        linewidth=1.05,
        color=color,
        linestyle=style,
        connectionstyle=f"arc3,rad={rad}",
    )
    ax.add_patch(item)
    if text:
        mx = (start[0] + end[0]) / 2
        my = (start[1] + end[1]) / 2
        ax.text(mx, my + 0.022, text, ha="center", va="bottom", fontsize=7.1, color=color)


def panel_label(ax, label: str) -> None:
    ax.text(-0.10, 1.04, label, transform=ax.transAxes, fontweight="bold", fontsize=9.2)


def clean_axis(ax, grid_axis="y") -> None:
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color("#8C99A5")
    ax.tick_params(color="#8C99A5", length=3)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.55, alpha=0.75)
    ax.set_axisbelow(True)


def fig_framework() -> None:
    fig, ax = plt.subplots(figsize=(7.15, 3.55))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.text(0.015, 0.955, "首次覆盖计划", fontweight="bold", color=NAVY, fontsize=9.1, va="top")
    rounded_box(ax, (0.03, 0.66), (0.19, 0.17), "区域单元与候选视向\n冻结的可行覆盖映射", "#E8F1F8")
    rounded_box(ax, (0.295, 0.66), (0.19, 0.17), "MAG 保底解\nUAC 严格改进 / T100 不增", "#E5F2EC")
    rounded_box(ax, (0.56, 0.66), (0.17, 0.17), "安全动作仲裁\n方位 / 俯仰", "#FFF4D6")
    rounded_box(ax, (0.81, 0.66), (0.16, 0.17), "4 × ZR10\nvelocity_p + RTSP", "#FDE9D9")
    arrow(ax, (0.22, 0.745), (0.295, 0.745))
    arrow(ax, (0.485, 0.745), (0.56, 0.745))
    arrow(ax, (0.73, 0.745), (0.81, 0.745))

    ax.plot([0.025, 0.975], [0.58, 0.58], color=GRID, lw=0.9)
    ax.text(0.015, 0.545, "执行证据", fontweight="bold", color=NAVY, fontsize=9.1, va="top")
    labels = [
        ("命令已发送\nACK 仅确认链路", "#F4E1E1"),
        ("姿态进入误差门\n角速度满足阈值", "#E8F1F8"),
        ("连续稳定 + 驻留", "#FFF4D6"),
        ("到位后的可用新帧\n帧—姿态满足同步界", "#E5F2EC"),
        ("按实测姿态投影视场\n更新首次覆盖时刻", "#DCEBEC"),
    ]
    xs = [0.03, 0.225, 0.42, 0.615, 0.81]
    for idx, ((label, face), x) in enumerate(zip(labels, xs)):
        rounded_box(ax, (x, 0.30), (0.16, 0.135), label, face, size=7.5)
        if idx < 4:
            arrow(ax, (x + 0.16, 0.367), (xs[idx + 1], 0.367), color=TEAL)

    rounded_box(ax, (0.22, 0.045), (0.56, 0.13), "设备事件形成剩余问题：原队列为保底\n仅提交 UAC 改善、T100 不增且具有恢复前见证的修复", "#F5E6DE", RED, size=7.3)
    arrow(ax, (0.89, 0.30), (0.78, 0.175), color=RED, rad=0.05)
    arrow(ax, (0.22, 0.11), (0.39, 0.66), color=RED, rad=-0.22, style="--")

    fig.savefig(OUT / "final_fig1_framework.png", facecolor="white")
    plt.close(fig)


def fig_guarantees() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(7.15, 3.05), gridspec_kw={"wspace": 0.22})

    ax = axes[0]
    ax.set_aspect("equal")
    ax.set_xlim(-1.15, 1.15)
    ax.set_ylim(-0.85, 1.05)
    ax.axis("off")
    panel_label(ax, "(a)")
    ax.set_title("执行容差诱导的规划安全核心", pad=3, fontweight="bold")
    colors = ["#B8D8EB", "#CDE8D9", "#FBE8B2", "#F8CEC0"]
    shifts = [(-0.10, -0.06), (0.10, -0.06), (-0.10, 0.06), (0.10, 0.06)]
    for (dx, dy), color in zip(shifts, colors):
        tri = Polygon(
            [[0 + dx, -0.68 + dy], [-0.72 + dx, 0.78 + dy], [0.72 + dx, 0.78 + dy]],
            closed=True,
            facecolor=color,
            edgecolor=BLUE,
            linewidth=0.65,
            alpha=0.42,
        )
        ax.add_patch(tri)
    robust = Polygon(
        [[0, -0.54], [-0.52, 0.66], [0.52, 0.66]],
        closed=True,
        facecolor=TEAL,
        edgecolor=NAVY,
        linewidth=1.2,
        alpha=0.62,
    )
    ax.add_patch(robust)
    ax.scatter([0], [-0.68], s=30, color=NAVY, zorder=5)
    ax.text(0, -0.82, "候选视向", ha="center", color=NAVY)
    ax.annotate("四个角点视场的交集", xy=(0.38, 0.42), xytext=(-0.72, 0.92),
                arrowprops=dict(arrowstyle="->", color=TEAL, lw=1.0), color=TEAL, fontsize=7.5)
    ax.text(
        0,
        0.00,
        "10× 名义视场 7.00° × 3.94°\ne = 1°，角面积约保留 35.2%",
        ha="center",
        va="center",
        fontsize=7.0,
        color=SLATE,
        linespacing=1.2,
    )

    ax = axes[1]
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    panel_label(ax, "(b)")
    ax.set_title("两个阶段共享的基线保持逻辑", pad=3, fontweight="bold")
    rounded_box(ax, (0.04, 0.71), (0.36, 0.15), "初始规划保底\n完整 MAG 路线", "#E8F1F8", size=7.7)
    rounded_box(ax, (0.60, 0.71), (0.36, 0.15), "事件修复保底\n保持当前任务与原队列", "#E8F1F8", size=7.0)
    rounded_box(ax, (0.18, 0.40), (0.64, 0.17), "候选完整？  UAC 严格改善？  T100 不增？\n修复阶段还需恢复前重分配见证", "#FFF4D6", ORANGE, size=7.8)
    rounded_box(ax, (0.04, 0.10), (0.36, 0.14), "提交新可行解\n继续搜索", "#E5F2EC", TEAL, size=7.5)
    rounded_box(ax, (0.60, 0.10), (0.36, 0.14), "保留当前可行解\n预算到期返回", "#F5E6DE", RED, size=7.4)
    arrow(ax, (0.22, 0.71), (0.40, 0.57), color=SLATE)
    arrow(ax, (0.78, 0.71), (0.60, 0.57), color=SLATE)
    arrow(ax, (0.44, 0.40), (0.26, 0.24), "接受", TEAL)
    arrow(ax, (0.56, 0.40), (0.74, 0.24), "拒绝", RED)
    fig.savefig(OUT / "final_fig2_guarantees.png", facecolor="white")
    plt.close(fig)


def main() -> None:
    configure()
    fig_framework()
    fig_guarantees()
    build_data_figures()
    for path in sorted(OUT.glob("final_fig*.png")):
        print(path)


if __name__ == "__main__":
    main()
