"""Generate data-free manuscript schematics.

These figures contain no experimental values.  Result figures are generated only
after stage-two logs pass integrity checks.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "figures"
OUT.mkdir(parents=True, exist_ok=True)


def configure() -> None:
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "font.size": 8.5,
        "figure.dpi": 160,
        "savefig.dpi": 320,
    })


def box(ax, xy, wh, text, face="#F4F7FA", edge="#334E68", fontsize=8.2, lw=1.0):
    patch = FancyBboxPatch(
        xy, wh[0], wh[1], boxstyle="round,pad=0.012,rounding_size=0.014",
        facecolor=face, edgecolor=edge, linewidth=lw,
    )
    ax.add_patch(patch)
    ax.text(xy[0] + wh[0] / 2, xy[1] + wh[1] / 2, text,
            ha="center", va="center", fontsize=fontsize, color="#102A43")
    return patch


def arrow(ax, start, end, text=None, color="#486581", rad=0.0, dashed=False):
    a = FancyArrowPatch(
        start, end, arrowstyle="-|>", mutation_scale=10,
        linewidth=1.0, color=color,
        connectionstyle=f"arc3,rad={rad}",
        linestyle="--" if dashed else "-",
    )
    ax.add_patch(a)
    if text:
        mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
        ax.text(mx, my + 0.018, text, ha="center", va="bottom", fontsize=7.2, color=color)
    return a


def fig_system_state_machine() -> None:
    fig = plt.figure(figsize=(7.15, 4.25))
    ax = fig.add_axes([0.025, 0.08, 0.95, 0.87])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.text(0.01, 0.96, "(a) 执行闭环系统", fontweight="bold", fontsize=9.5, va="top")
    box(ax, (0.04, 0.67), (0.16, 0.15), "区域模型\n候选视向与覆盖矩阵", face="#EAF2F8")
    box(ax, (0.28, 0.67), (0.16, 0.15), "EA-CML-H\n计划/剩余任务修复", face="#D9EAD3")
    box(ax, (0.52, 0.67), (0.16, 0.15), "动作仲裁与安全门\n绝对方位/俯仰", face="#FFF2CC")
    box(ax, (0.76, 0.67), (0.18, 0.15), "4 × ZR10\nvelocity_p + RTSP", face="#FCE5CD")
    arrow(ax, (0.20, 0.745), (0.28, 0.745), "任务")
    arrow(ax, (0.44, 0.745), (0.52, 0.745), "动作")
    arrow(ax, (0.68, 0.745), (0.76, 0.745), "命令")

    box(ax, (0.70, 0.43), (0.12, 0.11), "遥测\n姿态/角速度", face="#F4CCCC")
    box(ax, (0.84, 0.43), (0.12, 0.11), "帧状态\n时刻/分辨率/质量", face="#F4CCCC")
    box(ax, (0.50, 0.43), (0.14, 0.11), "命令结果\nACK ≠ 到位", face="#F4CCCC")
    arrow(ax, (0.82, 0.67), (0.76, 0.54), color="#A61C00")
    arrow(ax, (0.85, 0.67), (0.90, 0.54), color="#A61C00")
    arrow(ax, (0.79, 0.67), (0.57, 0.54), color="#A61C00")
    arrow(ax, (0.70, 0.485), (0.44, 0.70), "执行反馈", color="#A61C00", rad=-0.14)
    arrow(ax, (0.84, 0.485), (0.43, 0.68), color="#A61C00", rad=-0.20)
    arrow(ax, (0.50, 0.485), (0.42, 0.67), color="#A61C00", rad=-0.12)

    ax.plot([0.02, 0.98], [0.35, 0.35], color="#BCCCDC", lw=0.8)
    ax.text(0.01, 0.31, "(b) 每设备观测确认状态机", fontweight="bold", fontsize=9.5, va="top")
    names = ["MOVE", "SETTLE", "DWELL", "FRAME\nCONFIRM", "COMPLETE"]
    x0, gap, bw = 0.05, 0.19, 0.13
    colors = ["#EAF2F8", "#D9EAD3", "#FFF2CC", "#FCE5CD", "#D0E0E3"]
    for j, (name, c) in enumerate(zip(names, colors)):
        x = x0 + j * gap
        box(ax, (x, 0.11), (bw, 0.12), name, face=c, fontsize=8.2)
        if j < len(names) - 1:
            labels = ["进入误差门", "连续稳定", "驻留完成", "有效新帧"]
            arrow(ax, (x + bw, 0.17), (x0 + (j + 1) * gap, 0.17), labels[j])
    arrow(ax, (0.73, 0.105), (0.27, 0.105), color="#A61C00", rad=-0.16, dashed=True)
    ax.text(0.50, 0.075, "超时 / 陈旧 / 失败 / 暂停 → 仅修复未确认任务",
            ha="center", va="top", fontsize=7.2, color="#A61C00")
    ax.text(0.5, 0.005, "覆盖只在 COMPLETE 事件后更新；计划视向和 ACK 都不是完成证据",
            ha="center", va="bottom", fontsize=7.8, color="#334E68")
    fig.savefig(OUT / "fig1_system_state_machine.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def fig_execution_repair() -> None:
    fig, ax = plt.subplots(figsize=(7.15, 3.25))
    ax.set_xlim(0, 100)
    ax.set_ylim(-0.3, 4.7)
    ax.axis("off")
    ax.text(0, 4.45, "执行时间线与事件触发残余任务修复（示意，不含实验数据）",
            fontweight="bold", fontsize=9.5, va="top")

    colors = ["#5B8FF9", "#61DDAA", "#65789B", "#F6BD16"]
    for i in range(4):
        y = 3.55 - i * 0.83
        ax.text(0, y + 0.18, f"设备 {i+1}", ha="left", va="center", fontsize=8)
        segments = [(10, 17, "M"), (27, 7, "S/D"), (34, 6, "F"), (43, 14, "M"), (57, 8, "S/D"), (65, 5, "F")]
        shift = i * 2.3
        for start, width, label in segments:
            s = min(start + shift, 91)
            w = min(width, 96 - s)
            if w <= 0:
                continue
            ax.add_patch(Rectangle((s, y), w, 0.36, facecolor=colors[i], alpha=0.75, edgecolor="white"))
            if w > 4:
                ax.text(s + w / 2, y + 0.18, label, ha="center", va="center", fontsize=6.5, color="white")
        ax.plot([8, 96], [y, y], color="#9FB3C8", lw=0.5, zorder=0)

    event_x = 53
    ax.axvline(event_x, ymin=0.08, ymax=0.84, color="#A61C00", lw=1.25, ls="--")
    ax.text(event_x + 1, 4.04, "设备 2 帧超时", color="#A61C00", fontsize=7.8, va="top")
    ax.annotate("冻结已确认集合", xy=(event_x, 0.5), xytext=(33, 0.05),
                arrowprops=dict(arrowstyle="->", color="#486581"), fontsize=7.5, color="#334E68")
    ax.annotate("以当前实测姿态重规划", xy=(event_x + 3, 1.3), xytext=(61, 0.05),
                arrowprops=dict(arrowstyle="->", color="#486581"), fontsize=7.5, color="#334E68")
    ax.annotate("未确认任务转移到活动设备", xy=(70, 2.0), xytext=(75, 4.05),
                arrowprops=dict(arrowstyle="->", color="#486581"), fontsize=7.5, ha="center", color="#334E68")
    ax.text(10, -0.18, "M: MOVE    S/D: SETTLE + DWELL    F: FRAME_CONFIRM",
            fontsize=7.2, color="#627D98")
    fig.savefig(OUT / "fig2_execution_repair.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def fig_experiment_design() -> None:
    fig, ax = plt.subplots(figsize=(7.15, 4.0))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(0.01, 0.965, "配对随机区组与证据链", fontweight="bold", fontsize=9.5, va="top")

    box(ax, (0.03, 0.72), (0.20, 0.15), "Commissioning\n拟合 / 校准 / 测试分离", face="#EAF2F8")
    box(ax, (0.30, 0.72), (0.18, 0.15), "冻结时间模型\n配置与代码哈希", face="#D9EAD3")
    box(ax, (0.55, 0.72), (0.18, 0.15), "生成随机区组计划\n种子在运行前固定", face="#FFF2CC")
    box(ax, (0.80, 0.72), (0.17, 0.15), "42 次主运行\nNominal 30 + Pause 12", face="#FCE5CD", fontsize=7.0)
    arrow(ax, (0.23, 0.795), (0.30, 0.795))
    arrow(ax, (0.48, 0.795), (0.55, 0.795))
    arrow(ax, (0.73, 0.795), (0.80, 0.795))

    methods = ["FPR", "MAG", "Nominal-CML-H", "Measured-CML-H", "EA-CML-H"]
    conditions = ["Nominal", "Pause"]
    ax.text(0.03, 0.61, "同一区组内：相同任务、home、设备状态和完成谓词；只改变方法",
            fontsize=8.2, color="#334E68")
    x0, y0, cw, ch = 0.23, 0.24, 0.105, 0.13
    for j, m in enumerate(methods):
        ax.text(x0 + j * cw + cw / 2, 0.58, m, ha="center", va="center", fontsize=7.5, rotation=15)
    for i, cond in enumerate(conditions):
        y = y0 + (1 - i) * ch
        ax.text(0.02, y + ch / 2, cond, ha="left", va="center", fontsize=7.5)
        for j in range(5):
            active = cond == "Nominal" or j in (3, 4)
            c = ("#D9EAD3" if j == 4 else "#EAF2F8") if active else "#F2F2F2"
            rect = Rectangle((x0 + j * cw, y), cw * 0.90, ch * 0.72,
                             facecolor=c, edgecolor="#627D98", lw=0.6)
            ax.add_patch(rect)
            label = "6 配对运行" if active else "—"
            ax.text(x0 + j * cw + cw * 0.45, y + ch * 0.36, label,
                    ha="center", va="center", fontsize=6.8)

    box(ax, (0.79, 0.20), (0.19, 0.31),
        "运行级分析\n\nT95 / T100\nUAC_H 与达标率\n右删失与配对差值\nHodges–Lehmann + CI\nNominal: Friedman–Holm\nPause: 配对置换",
        face="#F4F7FA", fontsize=6.35)
    arrow(ax, (0.745, 0.35), (0.79, 0.35))
    ax.text(0.768, 0.385, "完整日志", ha="center", va="bottom", fontsize=6.6, color="#486581")
    ax.text(0.5, 0.07, "每次运行是统计样本；控制周期、任务和覆盖单元不作为独立重复",
            ha="center", va="center", fontsize=7.8, color="#A61C00")
    fig.savefig(OUT / "fig3_experiment_design.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def main() -> None:
    configure()
    fig_system_state_machine()
    fig_execution_repair()
    fig_experiment_design()
    print(f"wrote figures to {OUT}")


if __name__ == "__main__":
    main()
