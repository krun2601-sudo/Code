# 论文数据图绘制代码

本目录集中保存论文全部绘图代码。上级目录的 `make_final_figures.py` 只是兼容入口，不包含绘图实现。

## 文件

- `plot_style.py`：统一字体、颜色、线宽、坐标轴和经验累积分布函数。
- `plot_all_figures.py`：生成方法框架图、规划安全设置与基线保持机制图，并调用数据图脚本。
- `plot_nominal_results.py`：生成标称实验结果图 `final_fig3_nominal.png`。
- `plot_repair_results.py`：生成修复消融与计算预算图 `final_fig4_repair.png`。
- `build_all.py`：一次生成两幅数据结果图，适合只调整统计图时使用。

## 数据来源

脚本直接读取冻结分析结果，不在代码内重写运行级数据：

- `../../private/v2_20261020/formal_v242/analysis/run_metrics_wide.csv`
- `../../private/v2_20261020/repair_revalidation_v243/analysis/paired_repair_effects.csv`
- `../../private/v2_20261020/repair_revalidation_v243/V243_COMPLETE_AUDIT.json`

输出写入 `../figures/`。统计量、样本量和非劣界均与论文正文一致。

## 运行

从 `paper` 目录执行：

```powershell
python make_final_figures.py
```

仅重绘数据结果图时可执行 `python -m plotting.build_all`。

常用修改集中在 `plot_style.py`；版式和每个子图的轴范围位于对应绘图脚本中。若修改了图形尺寸或标签，需重新生成论文 DOCX 并检查分页。

