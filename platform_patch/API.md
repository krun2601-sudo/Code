# 策略接口与时间语义

## 数据流

```text
RTSP 解码帧 ──> 帧健康评估 ──> 独立一次性邮箱 ──> PolicyContext.frames
                                                  └─> frames.csv

策略 Action ──> velocity_p 执行器 ──> SDK 应答/错误 ──> 最新结果邮箱
                                                    ├─> commands.csv
                                                    └─> PolicyContext.command_results
```

视频检测邮箱和帧健康邮箱彼此独立。浏览器预览、检测消费和策略帧确认不会互相取走数据。原始图像不进入 `PolicyContext`，策略只获得健康元数据。

## `PolicyContext` 兼容扩展

新增字段位于 dataclass 末尾并有空字典默认值：

```python
frames: dict[str, FrameStatus] = field(default_factory=dict)
command_results: dict[str, CommandResult] = field(default_factory=dict)
```

因此原有 `PolicyContext(t, dt, step, devices, detections, rays, tracks, metrics)` 位置参数保持有效。

`frames` 是**增量邮箱**：每台设备只有出现尚未向策略报告的新解码帧时才有条目；读取后该条目被消费。缺少设备键表示本周期没有新帧，不能沿用上周期帧再次确认驻留。过期帧仍以 `usable=False`、`reason=stale_frame` 报告和记录，便于区分“没有解码帧”与“持续收到过期帧”；它绝不能用于完成确认。

`command_results` 是**最近结果快照**：每台设备最多一个条目，可能对应较早周期动作。策略必须用 `result.action.issued_t`、动作目标和本地 transition 标识关联，不能把 `ack=True` 自动解释为当前目标已经到位。

## `FrameStatus`

| 字段 | 类型 | 语义 |
|---|---|---|
| `device_id` | `str` | 平台设备标识 |
| `frame_id` | `int` | 解码源单调递增帧号 |
| `capture_t` | `float` | `received_t - camera.video_latency_s` 的主机侧曝光时刻估计 |
| `received_t` | `float` | 主机单调时钟下的解码接收时刻 |
| `width`, `height` | `int` | 实际解码分辨率 |
| `usable` | `bool` | 时间戳新鲜、分辨率一致且全部启用的轻量质量门均通过 |
| `resolution_ok` | `bool` | 实际分辨率等于该设备 `camera.image_size` |
| `mean_luma` | `float` | 稀疏采样灰度均值，范围约为 0–255 |
| `contrast` | `float` | 稀疏采样灰度标准差 |
| `sharpness` | `float` | 水平/垂直一阶灰度差绝对值均值；只作在线退化门，不是相机 MTF |
| `skipped_frames` | `int` | 管线启动以来按帧号估算的累计跳帧数 |
| `reason` | `str` | 分号分隔拒绝原因；通过时为空 |

`capture_t` 不是相机硬件时间戳，也不证明多机同步。它只能在同一进程、同一会话的单调时钟域内比较。论文中应表述为“经标定视频时延修正的主机接收时间估计”。

V2覆盖证据使用有界时间对齐：只在当前新鲜遥测样本的 `state.t` 与该帧 `capture_t` 的差不超过冻结的 `frame_pose_max_gap_s` 时，才按该遥测姿态重新投影视场。日志同时保存两个时间戳及其差值。该机制给出可审计的近邻姿态—帧关联，不应表述为硬件级同步曝光姿态。

默认质量阈值只强制分辨率一致；现场调试后可在 `policy.frame_quality` 增加保守阈值：

```yaml
policy:
  frame_quality:
    min_mean_luma: 0.0
    max_mean_luma: 255.0
    min_contrast: 0.0
    min_sharpness: 0.0
```

阈值必须依据预检与 commissioning 日志冻结，不能在看到主实验结果后调参。分辨率不一致始终不可用，不能通过阈值关闭。

## `CommandResult`

该类型沿用平台 4.0.0：

```python
CommandResult(
    device_id, t, action, status,
    sent=False, ack=False, latency_ms=0.0,
    applied={}, error="",
)
```

- `sent`：命令已进入底层发送路径。
- `ack`：SDK/设备对命令链路给出应答。
- `status`、`error`：执行器给出的结果分类和错误文本。
- `applied`：底层实际采用的协议参数记录。

ACK 不是到位证明。到位必须由随后新鲜遥测的角误差、角速度和连续稳定窗口确认；帧确认还必须来自稳定/驻留之后的新 `FrameStatus`。

commissioning 与调度器应使用同一个转移时间定义：`actual_s` 从首次发出新目标动作开始，到该目标进入 `FRAME_CONFIRM` 为止。这个端到端量已经包含转动、稳定窗口、dwell 和等待合格新帧；若调度器使用它的保守分位数上界，不得再额外叠加 dwell 或帧等待时间。

## `frames.csv`

表头为平台通用四列加帧字段：

```text
schema_version,run_id,utc,t_s,
device_id,frame_id,capture_t,received_t,width,height,age_s,
usable,resolution_ok,mean_luma,contrast,sharpness,skipped_frames,reason
```

其中 `t_s` 是相对本次记录器起点的实验时间，`capture_t` 和 `received_t` 保留主机单调时钟原值，`age_s = 当前控制周期单调时刻 - capture_t`。全局 `schema_version` 保持 `1`；新增表的存在由 `metadata.platform_patch_version` 区分。

## 控制模式

自定义模式可声明：

```yaml
control_center:
  modes:
    - id: coverage_hil
      policy: algorithms.user_policy:MyPolicy
      requires_detection: false
      requires_calibration: true
      requires_video: true
```

`requires_video: true` 会在不启用目标检测的情况下启动 RTSP 与帧健康管线。命令行 `run_hardware` 对应使用 `policy.requires_detection: false`，从而避免为纯区域覆盖加载检测器。

## 策略日志契约

平台继续把 `Decision.diagnostics` 原样写入 `cycles.csv` 的 `diagnostics_json.policy`。策略级事件采用紧凑 `diagnostics["events"]` 列表，由分析器从周期日志读取，不在补丁层重复写 `events.csv`，从而避免同一事件双计数。

每周期至少包含：`mission_state`、`mission_elapsed_s`、`confirmed_count`、`reachable_count`、`coverage_fraction`、`planner_ms`。`confirmed_count` 只增不减，`reachable_count` 在一个冻结任务集内固定。事件类型与字段由算法文档统一，包括 `coverage_confirmed`、`transition_completed`、`fault_started`、`fault_recovered`、`mission_started` 和 `mission_completed`。

