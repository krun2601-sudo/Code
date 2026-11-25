# 四机 ZR10 实物实验工具链（阶段一）

这套工具负责配置、预检证据、预注册、日志完整性检查和统计。`rtsp_stability_check.py` 会只读连接四路RTSP，但不导入云台SDK、不发送控制命令；批处理运行器只在操作者明确使用实机流程后调用平台。`pause` 是策略可用性掩码，不关闭网卡、不暂停进程、不阻塞看门狗，也不伪造ACK。

## V2冻结流程（当前主流程）

旧五方法流程保留在本文后半部，仅用于复核历史42次实验。V2按以下顺序执行：

```powershell
# 1. 四路视频只读稳定性：每路至少5 min，最大断流2 s
python rtsp_stability_check.py --config D:/private/v2_base.yaml `
  --duration-s 300 --max-gap-s 2 --output D:/private/v2/rtsp_5min.json

# 2. 生成分运行标定计划；输出 train/calibration/test 三个 overlay
python generate_commissioning_plan.py --config D:/private/v2_base.yaml `
  --output-prefix D:/private/v2/commissioning --per-device 60 --seed 20261020

# 3. 三个 overlay 必须作为三个独立实物会话执行，然后拟合模型
python fit_execution_model.py --runs D:/private/v2/commissioning_runs `
  --plan D:/private/v2/commissioning.json --output-dir D:/private/v2/model

# 4. 审核模型，把模型SHA-256和pose_error_bounds_deg写回私有配置后，生成六次开发运行
python generate_v2_confirmatory_plan.py --stage development `
  --config D:/private/v2_base.yaml --output-prefix D:/private/v2/dev_plan `
  --overlay-dir D:/private/v2/dev_overlays --overlay-extends D:/private/v2_base.yaml

# 5. 执行、分析六次开发运行
python run_confirmatory_batch.py --plan D:/private/v2/dev_plan.json `
  --control-root D:/private/v2/dev_control --runs-root D:/private/v2/dev_runs `
  --kit-root D:/path/to/ZR10_Algorithm_Kit --resume
python analyze_runs.py --runs D:/private/v2/dev_runs --plan D:/private/v2/dev_plan.json `
  --batch-state D:/private/v2/dev_control/batch_state.json --output-dir D:/private/v2/dev_analysis

# 6. 全部开发门槛通过后，才可生成50次正式计划
python audit_v2_readiness.py --rtsp-report D:/private/v2/rtsp_5min.json `
  --execution-model D:/private/v2/model/execution_model.json `
  --development-analysis-dir D:/private/v2/dev_analysis `
  --output D:/private/v2/readiness.json
python generate_v2_confirmatory_plan.py --stage confirmatory `
  --readiness-report D:/private/v2/readiness.json --config D:/private/v2_base.yaml `
  --output-prefix D:/private/v2/formal_plan --overlay-dir D:/private/v2/formal_overlays `
  --overlay-extends D:/private/v2_base.yaml
```

正式分析必须增加 `--batch-state <formal_control/batch_state.json>`。脚本会保留并列出被作废的原始运行，只选取每个计划行当前有效的实物尝试。最终是否允许启动论文由 `v2_decision_gates.json` 决定。

## 1. 文件和边界

- `../configs/public_four_zr10_template.yaml`：可公开的完整结构模板。IP 只保留非地址占位符，RTSP、权重和模型路径为空，标定标志为 `false`，因此不能直接用于实机。
- `../configs/private_site_overlay.yaml`：仅继承现有现场 YAML 并覆盖实验参数；没有复制 IP、RTSP 或权重。原文件 SHA-256（本阶段读取时）为 `cf499069d14c59fa79ce974f9d774bd648bffc29fadde4057509fe8a0e91e200`。若原文件变化，应重新预检，而不是忽略哈希变化。
- `preflight.py`：纯文件只读检查。无实时快照时会明确写 `ready_to_move=false`；这不是失败地“假装通过”。
- `generate_plan.py`：五方法、三条件、默认八区组，共 120 个独立 run。`pair_id` 是配对统计键；六区组/90 run 仅作为时间受限的 pilot 下限。
- `generate_commissioning_plan.py`：每设备 60 次预注册转移，固定 36/12/12 的 train/calibration/test 划分，并生成可由真实 `MyPolicy` 读取的 commissioning 序列 overlay；生成器本身不执行运动。
- `fit_execution_model.py`：设备相关方向回归 + 单侧 split-conformal 上界。
- `prepare_run_context.py`：把一个计划行与完整通过的预检报告绑定成只读出处文件，不代表操作者授权启动。
- `analyze_runs.py`：严格拒绝不完整、改写、哈希不符或预检未通过的会话；一个 run 才是一个样本。

平台需先安装本项目 `platform_patch`：策略上下文必须具有 `frames`、`command_results`，记录器必须产生 `frames.csv` 和 `metadata.platform_patch_version`。没有这些字段的旧日志不会作为论文实物证据接收。

## 2. 推荐顺序

以下命令均在 `ral_hil_stage1/tools` 所在目录执行；也可使用绝对路径。

### 2.1 静态预检（不会连接设备）

```powershell
python preflight.py `
  --config ../configs/private_site_overlay.yaml `
  --phase commissioning `
  --expected-extends-sha256 cf499069d14c59fa79ce974f9d774bd648bffc29fadde4057509fe8a0e91e200 `
  --output ../private/preflight_static.json
```

静态检查即使通过，实时项仍是 `NOT_EVALUATED`，`ready_to_move` 必然为 `false`。完整预检需要平台导出的只读快照（结构见 `../configs/preflight_snapshot.example.json`）或一个已有、完整的 patched-platform run：

```powershell
python preflight.py --config ../configs/private_site_overlay.yaml `
  --snapshot D:/private/preflight_snapshot.json --require-live `
  --output D:/private/preflight_pass.json
```

每台设备均须同时满足：新鲜且连接的遥测、`zoom_known=true`、`zoom_stable=true`、实际倍率 10、1920×1080 新鲜可用帧、最近命令已发送且 ACK 成功。历史上“曾跑通”不能替代本次预检。主实验把 `policy.evaluation_grid.shape=[8,12,4]` 冻结为 `M=384` 个评价单元；它不是候选视向集合，候选视向由各策略独立构造。`scan_grid` 仅是兼容旧加载器的 deprecated alias，必须与 `evaluation_grid.shape` 完全相同。正式 experiment 预检要求 `reachable_count=evaluation_cell_count=384` 且 `reachable_ratio=1`；只要有一个单元不可达，就先调整 ROI、标定或候选视向并重新预检，不能缩小 T100 的分母。commissioning 预检不要求覆盖任务记录。

现场文件目前四机内参完全相同、畸变全零且视频延迟为零；预检会把这些列为警告。`calibration_verified: true` 只是原平台的软件门，不是物理证据。正式主实验使用 `--phase experiment` 时，还会要求 `policy.calibration_evidence` 的站姿、逐机内参、固定倍率和视频延迟四项均有日期化记录并给出非空 `evidence_id`；同时实际读取时间模型，检查四设备模型齐全、所有系数与 conformal padding 非负、目标结束于 `FRAME_CONFIRM` 且路由规则明确禁止重复加 dwell。倍率 10 应先通过现场批准的手动流程设置并读回；实验 overlay 随后把动作空间冻结为 yaw/pitch，分析脚本不会替用户改变倍率。

### 2.2 转移时间标定计划

```powershell
python generate_commissioning_plan.py `
  --config ../configs/private_site_overlay.yaml `
  --output-prefix ../configs/generated/commissioning_plan_seed20261018 `
  --per-device 60 --seed 20261018 `
  --overlay-extends ../private_site_overlay.yaml
```

除CSV/JSON外，命令会写出 `.train.overlay.yaml`、`.calibration.overlay.yaml` 和 `.test.overlay.yaml` 三个覆盖层。三者继承同一配置，但只选择对应集合的 `transition_ids`；必须作为三个独立实物会话执行。覆盖层只是配置，不会自行启动平台。若希望保持可搬移的相对继承路径，可传 `--overlay-extends`；`--overlay-output` 指定的是三个文件共同使用的文件名前缀。

默认每设备60条，共240个设备转移样本；四台并行执行时是60个调度步。小、中、大转角各20条/设备。策略把计划里的 `transition_id` 原值透传到 `transition_completed`；`attempt_id` 不能替代计划ID。拟合使用日志中的实测起止姿态和端到端实际时间，并按 `pose_plan_tolerance_deg` 拒绝超差样本。train/calibration/test为36/12/12条/设备，且来自三个完整运行；拟合器若发现一个源运行中出现多个集合标签会直接拒绝，以阻断同运行泄漏。

事件契约见 `examples/policy_event_contract.json`。策略把事件列表放在当周期 `Decision.diagnostics["events"]`，平台将其原样记录到 `cycles.csv/diagnostics_json.policy.events`；不要同时再写一份策略事件到 `events.csv`。分析器为早期原型兼容两条通道并去重。也可先把实测事件整理到 `examples/measured_transitions_blank.csv` 所示列：

```powershell
python fit_execution_model.py `
  --runs D:/private/commissioning_runs `
  --plan ../configs/generated/commissioning_plan_seed20261018.csv `
  --output-dir D:/private/execution_model
```

每设备基准模型特征为截距、正/负 yaw 位移和上/下 pitch 位移。训练采用 Huber IRLS；每个加权子问题使用 Kit 已声明依赖 SciPy 的 NNLS 求解，截距和四个方向斜率均强制非负，ridge 只稳定斜率。模型 JSON 记录迭代次数、鲁棒尺度、最小权重和收敛状态。系数只用 train；单侧 conformal padding 只用 calibration：

`actual_s` 的冻结定义是“该转移首条相关命令发出，到对应 `FRAME_CONFIRM` 完成”，其中已经包含运动、连续稳定窗口、驻留和新鲜帧确认。每条记录必须带 `time_definition=command_issued_to_frame_confirm`；若同时给出两个时间戳，拟合器会重算时差，并在与 `actual_s` 相差超过 1 ms 时拒绝。路由/分配代价直接使用 conformal 上界，**不得再额外加一次 dwell**。

主实验 `transition_completed.predicted_s` 是该方法实际用于路由的端到端时间估计，并用 `prediction_kind` 区分 `conformal_upper`、`nominal_end_to_end` 或（只允许在 commissioning 阶段出现的）`nominal_fallback_upper`。事件同时记录 `point_prediction_s` 与 `conformal_padding_s`。统计输出把三种口径分开：`routing_prediction_*` 评价实际路由估计，`point_prediction_*` 评价事件中的点估计，`upper_prediction_*` 只统计真正的 conformal 上界事件；早期 `prediction_*` 列仅保留为路由估计兼容别名。点模型的正式 MAE/RMSE/低估率仍以独立 commissioning test split 的 `model_summary.csv` 为准。

`k = ceil((n_cal + 1) * (1 - alpha))`，取按升序排列的第 `k` 个 `actual - point_prediction`，并用 `max(0,q)` 保守化。若 `k>n_cal`，有限样本经验分位应为无穷上界；工具不会静默把它截成样本最大值，而是拒绝并要求提高 `alpha` 或增加 calibration 样本。默认 `n_cal=12, alpha=0.1` 时 `k=12`，规则可执行。

test只报告最终MAE、RMSE、点预测低估率、分档上界经验覆盖率和平均上界宽度，绝不反向调模型。模型同时给出校准运行中的方位/俯仰到位误差最大值，作为鲁棒视场侵蚀的下界证据。生成状态是 `fitted_pending_review`；审核后，才可在私有配置中设为 `calibrated`，写入模型路径、模型SHA-256和不小于该证据的姿态误差界。

### 2.3 120-run 随机区组计划

仓库中随附的 `configs/generated/study_plan_8blocks_seed2401.*` 和 120 份 overlay 只用于在上机前核查结构、随机顺序和配对约束；它们按尚未标定的阶段一配置生成，**不能直接作为正式主实验计划执行**。完成 commissioning、审核模型后，先在私有配置中写入 `execution_model.status: calibrated` 和经审核的 `model_path`，再运行下述命令重新生成计划。新计划会把最终时间模型、标定证据和硬件配置共同绑定到哈希。

```powershell
python generate_plan.py `
  --config ../configs/private_site_overlay.yaml `
  --output-prefix ../configs/generated/study_plan_8blocks_seed2401 `
  --blocks 8 --seed 2401 `
  --overlay-dir ../configs/generated/run_overlays `
  --overlay-extends ../../private_site_overlay.yaml
```

每个 `block × condition` 是一个配对组，五种方法各运行一次且组内顺序随机。推荐八个区组 × 三条件 × 五方法 = 120 个独立 run；六区组 × 三条件 × 五方法 = 90 仅是 pilot 下限。六配对下双侧 Wilcoxon 的最小 p 值为 0.03125，四项 Holm 后不可能显著，因此不能把 90-run 版本设计成“保证显著”的确认性研究。即使采用八区组，也以配对效应量和置信区间为主，p 值只按预注册方案解释，绝不承诺显著。

N=2/3/4、三档 ROI 尺度、重叠目标和任务密度只列在 `experiment.optional_diagnostics` 中，不与五方法×三条件×八区组做全因子交叉。若现场时间允许，最多预先选三个 one-factor-at-a-time 诊断 profile，单独标注为探索性；它们不能替代固定 N=4、M=384 的确认性主实验，也不能在看过主结果后再挑选。

如果需要逐 run YAML，可额外传 `--overlay-dir D:/private/plan_overlays --overlay-extends ../private_site_overlay.yaml`。这些 YAML 只含 `extends` 和方法/条件/种子，不复制现场端点。`heterogeneous_delay` 只延迟策略对“已确认”的认账时刻；`pause` 只把一台设备暂时从策略可用集合移除，并让旧动作按 TTL 自然过期。两者都不能替代真实网络故障结论，应在论文中称为受控软件扰动。

每次运行前绑定对应计划行与同一硬件配置的完整 `--phase experiment` 预检；commissioning 阶段的通过报告不能绑定为主实验出处：

```powershell
python prepare_run_context.py `
  --plan ../configs/generated/study_plan_8blocks_seed2401.csv `
  --plan-run-id ralhil-2401-8b-R001 `
  --preflight D:/private/preflight_pass.json `
  --output D:/private/run_context.json
```

运行结束后，把该文件原样放进对应 run 目录。工具不会把 `operator_arm_authorization` 改成 `true`；启动授权仍只由现场操作者在平台完成。

### 2.4 现场启动边界

平台补丁与策略都以 dry-run 为默认值。先在各自目录预览并审核，再显式安装：

```powershell
python ../platform_patch/install_patch.py `
  --platform-root D:/1Xingrunqiang/pycharmpro/M_zr10_V1/ZR10_EXP_Plant/zr10_cooperative_lab `
  --python D:/1Xingrunqiang/pycharmpro/M_zr10_V1/ZR10_EXP_Plant/.venv/Scripts/python.exe

python ../algorithms/install_to_platform.py `
  --platform-root D:/1Xingrunqiang/pycharmpro/M_zr10_V1/ZR10_EXP_Plant/zr10_cooperative_lab
```

审核 dry-run 输出后，分别增加 `--apply`。策略部署后，现场平台可直接导入 `algorithms.user_policy:MyPolicy`。实际运动只允许由操作者在平台根目录显式给出 `--arm`：

```powershell
# Commissioning：train/calibration/test 三个 overlay 分别运行，不能合并为一个会话。
./.venv/Scripts/python.exe -m zr10lab run --mode hardware `
  --config D:/private/commissioning.train.overlay.yaml `
  --duration 1200 --arm --ui

# 主实验：一次只运行对应计划行的 overlay，固定评价时限 90 s。
./.venv/Scripts/python.exe -m zr10lab run --mode hardware `
  --config D:/Desktop/Tasks/航天会议/效能评估/work/ral_hil_stage1/configs/generated/run_overlays/001_measured_static_nominal.yaml `
  --duration 90 --arm --ui
```

这些命令只是接口示例，不是对运动的自动授权。首次上机应先按 `docs/03_HARDWARE_PROTOCOL.md` 做单轴、小角度、急停可达的验收；每次运行前都用对应 overlay 完成实时预检并生成 `run_context.json`。commissioning 完成后先拟合和审核时间模型，再把状态改为 `calibrated` 并重新生成正式主实验计划；否则 Measured-CML-H 与 EA-CML-H 会按正式配置拒绝启动。

### 2.5 统计

```powershell
python analyze_runs.py `
  --runs D:/private/ral_hil_runs `
  --plan ../configs/generated/study_plan_8blocks_seed2401.csv `
  --output-dir D:/private/analysis
```

输出：

- `run_metrics_wide.csv`：每个独立 run 一行；
- `long_table.csv`：run × metric 长表，独立单位仍为 run；
- `main_table.csv`：方法 × 条件的中位数、四分位数、固定 H 下 T95/T100 达标率及删失数；
- `paired_bootstrap.csv`：同一 `pair_id` 内 `full_eacml - baseline` 的配对效应量与 bootstrap 95% CI；连续指标给出配对差中位数，固定 H 达标结局给出配对风险差（以比例输出，例如 0.25 表示 25 个百分点）；
- `hypothesis_tests.csv`：固定 H 达标率使用 Cochran Q 与 exact McNemar-Holm；完成时间只在双方均完成的配对上做探索性 Friedman/Wilcoxon-Holm。SciPy 不可用时为空并在摘要明确标注缺项；
- `time_to_threshold.csv`：T95/T100 的 time/event 格式，未达标者在 H 右删失而不是伪造完成时间；
- `rejection_report.csv`：损坏、未完成、哈希不符、预检不通过、重复和尚未执行的计划行；
- `analysis_summary.json`：统计边界与文件清单。
- `analysis_manifest.sha256.json`：计划和全部统计产物的 SHA-256。

固定评价时限 `H=90 s` 写入配置和计划。T95/T100 只由“执行确认覆盖”单调曲线计算；未在 H 前达到时记右删失，不填一个假的完成时间。`uac_h_s` 对每个 run 都积分到同一 H，是主要连续指标；完整覆盖后未覆盖比例为零。`mean_first_coverage_s` 只在 T100 完成时等于 UAC_H，未完成时留空。完成时间的秩检验只作完成配对探索，不能用“删掉失败样本后的显著性”替代删失处理；固定 H 达标率由 Cochran Q / exact McNemar-Holm分析。遥测积分得到实际角运动；负载 CV 优先用设备完成转移数，缺失时才明确标注为运动量替代。10 Hz 周期只用于重建曲线，绝不被当成 900 个统计样本。

## 3. 日志诊断契约

`cycles.csv/diagnostics_json.policy` 每周期至少应有：

```json
{
  "mission_state": "running",
  "mission_elapsed_s": 12.3,
  "evaluation_cell_count": 384,
  "confirmed_count": 324,
  "reachable_count": 384,
  "reachable_ratio": 1.0,
  "coverage_fraction": 0.84375,
  "planner_ms": 4.2
}
```

`evaluation_cell_count` 必须等于预注册 M=384；正式实验还必须满足 `reachable_count=384` 和 `reachable_ratio=1`，否则在预检阶段拒绝该配置。`confirmed_count` 不得下降，`coverage_fraction=confirmed_count/evaluation_cell_count`；绝不以可达子集缩小 T95/T100 的分母。详细策略事件放进 `diagnostics["events"]`，只在事件实际发生的周期输出一次；`events.csv` 继续保留平台级会话、设备与 UI 事件。不要把数百个 cell ID 在每个周期重复写一遍。分析器接受少量字段别名，但以上字段是冻结主契约。

## 4. 不能由这些数据支持的结论

四台云台、无目标和无外部真值条件只能验证闭环执行确认的区域/方向覆盖、调度时间、运动负载、帧与 ACK 可用性、受控软件扰动恢复。不能据此声称目标检测率、任意无规律运动目标的发现时间、三维定位精度、绝对指向精度、长时间可靠性或真实网络故障鲁棒性。

