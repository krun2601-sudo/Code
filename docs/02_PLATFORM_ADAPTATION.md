# ZR10 4.0.0 平台适配说明

## 1. 适配对象与不修改原则

现场工程根目录为 `D:/1Xingrunqiang/pycharmpro/M_zr10_V1/ZR10_EXP_Plant/zr10_cooperative_lab`，版本标识为 4.0.0。阶段一没有改动该目录；最终核验时六个目标源文件仍全部处于补丁清单记录的 `original` 状态，现场 `control_center_saved.yaml` 的 SHA-256 为 `cf499069d14c59fa79ce974f9d774bd648bffc29fadde4057509fe8a0e91e200`。

论文实机模式需要两个独立部署步骤：

1. `platform_patch/` 在策略上下文和记录器中增加帧状态、最近命令结果、`frames.csv` 与无检测视频管线开关；
2. `algorithms/` 把审核后的 `algorithms.user_policy:MyPolicy` 安装到平台根目录，使现有动态策略加载器可以导入。

两套安装器都默认 dry-run，只有操作者显式给出 `--apply` 才写入平台；安装报告提供精确回退依据。

## 2. 为什么原接口不足

原 `PolicyContext` 只有遥测、检测、射线、轨迹和指标。关闭检测器后，策略既拿不到每路视频的新帧与质量，也拿不到命令发送/ACK/错误结果。因此，原接口只能实现“姿态看起来到位”，不能实现论文冻结的完成谓词：

```text
MOVE -> SETTLE -> DWELL -> FRAME_CONFIRM -> COMPLETE
```

平台补丁在已有字段之后追加：

- `frames: Mapping[str, FrameStatus]`：本周期尚未报告的新帧，含 frame ID、接收/估计采集时刻、分辨率和质量摘要；
- `command_results: Mapping[str, CommandResult]`：每设备最近命令结果；
- 控制模式字段 `requires_video`：检测关闭时仍启动视频解码与帧健康路径；
- `frames.csv`：保存论文完成证书所需的帧证据；
- `metadata.platform_patch_version`：让分析器拒绝未打补丁的旧日志。

字段追加保持旧 `PolicyContext` 位置参数兼容。补丁不改变 SDK 协议、`velocity_p` 控制器、安全限位、TTL 或看门狗语义。

## 3. 策略输入输出映射

策略输入只来自平台 `PolicyContext`：

| 输入 | 用途 |
|---|---|
| `devices` | 姿态误差、角速度、连接状态、遥测新鲜度、倍率可信度 |
| `frames` | 驻留后新帧、帧 ID 去重、分辨率与可用性门 |
| `command_results` | 相关命令的发送、ACK 和显式失败 |
| `t`, `dt`, `step` | 主机单调时钟上的状态转换与截止时限 |
| `metrics` | 仅保留旧测试中的暂停兼容入口；正式注入使用冻结配置 |

输出为平台本地云台坐标中的绝对 `yaw_deg`、`pitch_deg` 和短 TTL。策略不连接 SDK、不读取 RTSP、不输出速度百分比；平台现有 `velocity_p` 适配器完成角误差到协议速度的转换。

## 4. 配置继承与现场标识

`configs/private_site_overlay.yaml` 只通过 `extends` 引用原现场 YAML，不复制 IP、RTSP URL、检测权重或凭据。它把设备 ID 保持为真实的 `zr10_25`、`zr10_26`、`zr10_27`、`zr10_28`，并覆盖以下论文实验项：

- 固定 `N=4`、评价网格 `8×12×4`、`M=384`；
- 软件安全域方位 ±35°、俯仰 −20° 至 20°；
- 固定倍率 10×，动作空间只允许方位/俯仰；
- `requires_detection=false`、`requires_video=true`；
- 完成门、规划预算、90 s 固定评价时限与日志目录；
- 五种方法和两类策略层注入。

正式实验初始化与每次 `reset` 都要求 `reachable_count=evaluation_cell_count=384`。任何单元不可达都拒绝启动；不能把可达子集改成 T95/T100 的新分母。

## 5. 部署、核验与回退

平台补丁：

```powershell
python platform_patch/verify_patch.py --platform-root <ZR10_PLATFORM_ROOT>
python platform_patch/install_patch.py --platform-root <ZR10_PLATFORM_ROOT> --python <PLATFORM_PYTHON>
# 审核 dry-run 后
python platform_patch/install_patch.py --platform-root <ZR10_PLATFORM_ROOT> --python <PLATFORM_PYTHON> --full-tests --apply
```

策略：

```powershell
python algorithms/install_to_platform.py --platform-root <ZR10_PLATFORM_ROOT>
# 审核 dry-run 后
python algorithms/install_to_platform.py --platform-root <ZR10_PLATFORM_ROOT> --apply
```

回退前先 dry-run。平台补丁使用 `platform_patch/rollback_patch.py` 与安装报告；策略使用 `algorithms/rollback_from_platform.py --report <install_report.json>`。任一受管文件在安装后被人工修改，回退器都会拒绝覆盖，避免丢失现场编辑。

## 6. 尚未完成的现场证据

当前只能确认接口与离线合同，不代表实机就绪。正式运动前仍需：

- 安装并复核平台补丁和策略；
- 四路实时遥测、ACK 与 RTSP 预检；
- 核对历史上出现过的 1280×720 / 1920×1080 分辨率不一致；
- 逐机验证 10× 倍率、内参、畸变、站姿和视频时延；
- 小角度单轴试动、符号/限位核对与急停演练；
- commissioning 后审核时间模型并将状态显式改为 `calibrated`。

上述条件缺一时，工具保持 `ready_to_move=false`，不能把历史“曾跑通”当作本次实验的通过证据。
