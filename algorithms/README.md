# 可部署协同覆盖策略

`user_policy.py` 是面向 ZR10 平台 4.0.0 与 `ral-hil-context-1.0.0` 补丁的单文件策略实现。入口为：

```text
algorithms.user_policy:MyPolicy
```

现场平台根目录原先没有 `algorithms` 包，因此仅把本目录留在论文工程中并不能保证平台进程可以导入策略。先预览部署，再由操作者显式提交：

```powershell
python install_to_platform.py `
  --platform-root D:/1Xingrunqiang/pycharmpro/M_zr10_V1/ZR10_EXP_Plant/zr10_cooperative_lab

python install_to_platform.py `
  --platform-root D:/1Xingrunqiang/pycharmpro/M_zr10_V1/ZR10_EXP_Plant/zr10_cooperative_lab `
  --apply
```

默认 dry-run 不写平台。安装器只管理 `algorithms/__init__.py` 和 `algorithms/user_policy.py`，遇到不同的既有文件会拒绝覆盖；确需替换时还要显式给出 `--replace-existing`。应用后保存 `report_path`，可先 dry-run 再用 `rollback_from_platform.py --report <install_report.json> --apply` 回退。平台补丁和算法部署是两个独立步骤，二者都完成后配置中的上述入口才可直接加载。

策略不连接 SDK、网络或视频，不读取仿真真值。输入仅为平台的 `PolicyContext`；输出为本地云台坐标中的**绝对**方位和俯仰角。底层 `velocity_p` 控制器负责把角度误差变成协议速度。因此，本策略不要也不会输出电机速度百分比。

## 1. 冻结任务集与候选视向

主实验配置采用：

```yaml
policy:
  evaluation_grid:
    shape: [8, 12, 4]
    cell_count: 384
    role: evaluation_cells
  scan_grid: [8, 12, 4]   # 仅为旧加载器保留，必须与 shape 相同
```

ROI 在三个轴向被分成等宽区间，使用每个区间的中心构造 `M=384` 个等权评价单元。`evaluation_cell_count` 始终等于 M；`reachable_count` 是四台设备全部候选视向可覆盖单元的并集大小，在一次 `reset` 到任务结束之间保持不变；`reachable_ratio=reachable_count/M`。策略不会把不可达单元从 M 中悄悄删除，也不会在发生故障后改写分母。

候选视向不是 `scan_grid` 中的点。每台设备独立执行以下确定性过程：

1. 对每个评价单元中心调用平台的 `world_to_gimbal_angles`，使用完整站址和安装旋转求本地方位/俯仰；
2. 方位和俯仰分别按 `candidate_quantization_deg` 量化，默认均为 0.5°，半格按远离零方向舍入；
3. 删除软件安全角域或机械角域外的姿态；
4. 在固定倍率内参和 `fov_margin` 下计算覆盖 bitset；零畸变使用等价的针孔投影，非零畸变调用平台批量投影函数；
5. 覆盖 bitset 完全相同的候选只保留相对 home 角程最小者，再按角度和单元编号确定性打破平局；
6. 每台候选数超过 `max_candidates_per_device` 时直接拒绝配置，不做可能改变可达集的静默截断。

这保证相同配置产生相同评价单元、候选视向和覆盖关系。几何覆盖仍是配置模型覆盖；没有外部标定证据时不能解释为绝对物理空间真值。

## 2. 五种主方法

配置名和规范化名称如下：

| 配置别名 | 规范名 | 计划代价 | 故障后跨设备修复 |
|---|---|---|---|
| `raster`, `fpr` | `fpr` | 固定分区蛇形次序 | 否 |
| `mag` | `mag` | 标称运动代价，末尾追加 | 否 |
| `open_loop_cml`, `open_cml` | `open_cml` | 标称端到端代价，全位置插入 | 否 |
| `measured_static`, `measured_cml` | `measured_cml` | 实测端到端上界，全位置插入 | 否 |
| `full_eacml`, `ea_cml` | `ea_cml` | 实测端到端上界，全位置插入 | 是 |

五种方法共用同一评价单元、候选视向、动作安全门、执行状态机、帧门和完成定义。`fpr` 按冻结的三维蛇形单元次序形成等长连续分区；首选设备不可达时才按固定设备顺序转交给下一台可达设备。随后在各设备分区内去除冗余视向；候选恰好覆盖其他分区时仍作为真实重叠保留，以便审计重复任务。该基线的所有权不依赖运动时间、健康量或运行结果。`mag` 只比较在当前队尾追加候选的边际覆盖/标称代价。后三种方法使用完整插入位置。

每次运行器重新连接设备后，任务时钟不会立即启动。策略先同时检查四台的新鲜遥测、固定倍率已知且稳定，以及当前连接产生的新鲜可用视频帧；条件未齐时只下发空动作保持/停止，不规划、不累计任务时间，也不生成故障事件。全部条件满足的第一个周期才记录 `mission_started` 并定义 `t=0`。实机记录时长比固定评价窗多 3 s，用于吸收这一连接就绪阶段；论文指标仍统一截断在 `H=120 s`。

## 3. 路由代价与启发式评分

标称边代价为：

```text
nominal_command_overhead_s
+ max(|delta_yaw|, |delta_pitch|) / nominal_slew_dps
+ settle_hold_s + min_dwell_s
+ nominal_frame_wait_s（仅 require_frame=true）
```

实测模型读取 `fit_execution_model.py` 输出的设备相关非负系数和单侧 split-conformal padding。其目标必须是从首次相关命令发出到 `FRAME_CONFIRM`，已经包含运动、稳定、dwell 和新鲜帧等待。路由直接使用：

```text
upper = intercept
      + b_yaw_pos * max(delta_yaw, 0)
      + b_yaw_neg * max(-delta_yaw, 0)
      + b_pitch_up * max(delta_pitch, 0)
      + b_pitch_down * max(-delta_pitch, 0)
      + one_sided_padding_s
```

代码不会再次添加 dwell 或帧等待。`transition_completed.predicted_s` 对实测方法表示该上界，同时记录 `prediction_kind=conformal_upper`、`point_prediction_s` 和 `conformal_padding_s`。若允许模型缺失回退，则明确记录 `execution_model_status` 和 `prediction_kind=nominal_fallback_upper`；正式主实验应设置 `strict_measured_model: true`。

把候选 q 插入设备 i 的路由位置 r 时，代码用被替换的前后两条边精确计算完整加性路由的前后差，并显式截断：

```text
delta_L = max(0, cost_after - cost_before)
delta_T = max(0, new_team_makespan - old_team_makespan)
```

不确定性项的实现定义为：

```text
U_iqr = max(0, route_uncertainty_after - route_uncertainty_before)
        + h_i * delta_L

h_i = 0.35 * telemetry_age_ratio
    + 0.35 * angular_rate_ratio
    + 0.30 * frame_health_ratio
```

各 ratio 截断到 `[0,1]`。没有新帧邮箱条目时 `frame_health_ratio=0`，避免把增量邮箱的正常空周期误判为视频故障；收到不可用帧时该项为 1。实测边的 route uncertainty 是 `one_sided_padding_s`，标称边默认由 `nominal_uncertainty_s` 给出。

实际评分为：

```text
score = marginal_unconfirmed_cell_count /
        (delta_T + lambda_balance * delta_L
         + lambda_uncertainty * U_iqr + score_epsilon)
```

默认 `lambda_balance=0.15`、`lambda_uncertainty=0.25`、`score_epsilon=1e-6`。平局依次按团队瓶颈增量、路由增量、不确定性、设备 ID、候选 ID 和插入位置确定。插入后执行确定性的冗余删除和最多 2 轮设备内 2-opt。

该评分只是可审计的启发式代理，不是目标函数的精确变换，也不声称全局最优。每次计划默认受 `planner_budget_ms=40` 和 `planner_max_evaluations=50000` 双重限制；预算内至少形成的路由块可先执行，队列耗尽后再为尚未保留的残余单元生成下一块。`local_search_passes=2`。每周期不调用 `sleep`，不做网络/磁盘读取或在线训练。

## 4. 物理完成状态机

每个任务 attempt 使用固定 `attempt_id`，所有目标动作和完成事件沿用该 ID：

```text
MOVE -> SETTLE -> DWELL -> FRAME_CONFIRM -> COMPLETE
```

- `MOVE`：每周期续租同一绝对目标角。ACK 只把 `ack_seen` 置真，不改变完成状态。
- `SETTLE`：方位/俯仰误差分别不超过 `pose_tolerance_deg`，两轴速率分别不超过 `max_rate_dps`，并连续保持 `settle_hold_s`。门失效立即回到 MOVE。
- `DWELL`：从稳定窗口结束后计时；门失效会清空本 attempt 的 dwell 与帧集合并回到 MOVE。
- `FRAME_CONFIRM`：仅接受 capture time 不早于 dwell 起点、年龄不超过 `system.max_frame_age_s`、`usable=true` 且 frame ID 不重复的新邮箱帧。必须同时满足 ACK 门（若启用）和 `distinct_frames` 帧门。
- `COMPLETE`：只保留一个决策周期；完成前再次计算 `candidate.coverage - confirmed`。如果已被另一台设备覆盖，当前任务作为 stale task 停止，不重复记数。

`confirmed_count` 只增不减。命令发送前也执行同一 stale-task 检查，因此重叠设备不会向一个已完成任务继续发运动命令。

## 5. 故障和剩余任务修复

以下事件触发故障：缺失/断开/陈旧遥测、逻辑暂停、固定倍率未知或不稳定、相关命令错误、任务当前不可达、总转移超时、帧确认超时，以及 EA-CML-H 中实测完成时间超过预测上界加 `prediction_overrun_grace_s`。

只有 `ea_cml` 会立即丢弃故障设备的未确认 attempt、冻结 `confirmed`、清空未开始队列，并从健康设备当前实测姿态为残余单元重新规划。其他四种方法保留当前 attempt 与固定分配，设备恢复后用新的 attempt ID 从 MOVE 重新开始，不借故障获得跨设备重分配收益。重规划受 `replan_min_interval_s` 去抖。

逻辑暂停期间不为受影响设备输出或续租动作，上一条短 TTL 速度命令自然失效；这不阻塞 10 Hz 决策循环或平台看门狗。陈旧或断开遥测时同样不输出动作。恢复、失败和覆盖事件均只在实际发生的周期写入一次。

## 6. 每周期日志契约

`Decision.diagnostics` 至少包含：

```text
mission_state, mission_elapsed_s,
evaluation_cell_count, reachable_count, reachable_ratio,
confirmed_count, coverage_fraction,
planner_ms, policy_compute_ms,
stage_by_device, attempt_by_device, fault_by_device,
execution_model_status, events
```

事件包括 `mission_started`、`coverage_confirmed`、`transition_completed`、`fault_started`、`fault_recovered` 和 `mission_completed`。`coverage_confirmed.cell_ids` 只在确认周期出现，避免每个 10 Hz 周期重复数百个 ID。

## 7. 验证

离线契约检查（不连接硬件）：

```powershell
python ZR10_Algorithm_Kit/tools/check_algorithm.py `
  --platform-root ZR10_Algorithm_Kit `
  --plugin-root ral_hil_stage1 `
  --entry algorithms.user_policy:MyPolicy `
  --config ZR10_Algorithm_Kit/configs/sim.yaml `
  --output ral_hil_stage1/tests/algorithm_policy/checker_report
```

定向状态机测试：

```powershell
python -m pytest -q ral_hil_stage1/tests/algorithm_policy/test_user_policy.py `
  --basetemp=ral_hil_stage1/tests/algorithm_policy/.pytest_tmp
```

这些检查验证接口、动作限位、缺失遥测退化、状态机、ACK/帧门、失败、两机重叠 stale task、两类受控注入、commissioning 序列、全可达硬门、暂停恢复、确定性、五方法切换、部署/回退和 dwell 不重复计入。它们不替代四台设备的标定、预检和实物验证。

