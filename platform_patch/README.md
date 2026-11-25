# ZR10 4.0.0 实机上下文扩展

本目录提供一个**精确快照补丁**，把帧健康和命令执行结果送入用户策略，同时保持原有策略的构造与位置参数调用兼容。补丁只支持 `manifest.json` 锁定的 ZR10 平台 4.0.0 源码字节；版本号相同但文件有任何本地改动也会拒绝安装。

补丁不包含设备地址、视频地址或现场配置，也不会打开网络连接。它不修改 `hardware.py`、`actions.py` 或 SDK，因此既不改变 `velocity_p` 控制律，也不改变原有看门狗与停机路径。

## 扩展内容

- `PolicyContext.frames`：本周期新解码且尚未报告的 `device_id -> FrameStatus`。
- `PolicyContext.command_results`：每台设备最近一次 `device_id -> CommandResult`。
- `VisionPipeline`：检测关闭时仍计算并发布帧健康；帧状态使用独立邮箱，不伪造检测结果。
- `frames.csv`：每个控制周期、每台设备至多记录一条尚未报告的最新帧状态，包括时间、分辨率、亮度、对比度、清晰度和拒绝原因。
- `metadata.json`：增加 `platform_patch_version`；全局 `schema_version` 仍为 `1`。
- 控制模式增加 `requires_video`，使无检测的覆盖策略仍可启动视频管线。

只改动下列六个文件：

1. `zr10lab/models.py`
2. `zr10lab/vision.py`
3. `zr10lab/recording.py`
4. `zr10lab/runtime.py`
5. `zr10lab/control_modes.py`
6. `zr10lab/control_center.py`

详细数据语义见 [API.md](API.md)。

## 安装

所有命令都应使用平台虚拟环境中的 Python。以下 `<PLATFORM_ROOT>` 表示含有 `zr10lab/` 的平台工程根目录，示例不含任何现场地址。

```powershell
& <PYTHON> -B ral_hil_stage1/platform_patch/verify_patch.py `
  --platform-root <PLATFORM_ROOT>
```

第一次应返回 `state: original`。随后执行默认 dry-run；它完成哈希门禁、临时叠加树编译和无网络 smoke test，但不提交改动：

```powershell
& <PYTHON> -B ral_hil_stage1/platform_patch/install_patch.py `
  --platform-root <PLATFORM_ROOT> `
  --python <PYTHON> `
  --full-tests
```

检查 JSON 结果中的 `ok: true`、`state_after: original` 和所有 preflight 检查后，再显式安装：

```powershell
& <PYTHON> -B ral_hil_stage1/platform_patch/install_patch.py `
  --platform-root <PLATFORM_ROOT> `
  --python <PYTHON> `
  --full-tests `
  --apply
```

成功结果给出 `backup_id`。安装器在提交前再次核验源哈希，逐文件使用同目录原子替换；任何中途失败都会用内存中的原字节恢复已替换文件。原文件副本和安装报告保存在平台根目录下的 `.ral_hil_patch_backups/<backup_id>/`。

最后再次核验：

```powershell
& <PYTHON> -B ral_hil_stage1/platform_patch/verify_patch.py `
  --platform-root <PLATFORM_ROOT>
```

此时必须是 `state: patched`。`mixed`、`drift` 或任一哈希不匹配都应停止实验，不要手工绕过门禁。

## 回退

默认仍是 dry-run：

```powershell
& <PYTHON> -B ral_hil_stage1/platform_patch/rollback_patch.py `
  --platform-root <PLATFORM_ROOT> `
  --backup-id <BACKUP_ID>
```

确认后增加 `--apply`。回退前同时校验当前文件和备份原文件；回退失败时会重新写回回退前字节。正常补丁状态及因进程中断而形成的 `mixed` 状态均可恢复，但 `mixed` 中每个文件仍必须精确匹配 manifest 的原始或补丁哈希；任何未知漂移仍会拒绝。成功后平台必须回到 `state: original`，已验证备份不会删除。

## 论文实机模式约束

论文的完整执行确认必须同时使用新鲜姿态、到位/稳定窗口、新帧和命令结果。用户策略应在启动时检查 `frames` 与 `command_results` 接口是否存在；默认实机配置缺失任一接口就明确失败，不得静默把姿态到位解释为完成观测。

只有显式启用非论文用途的 `allow_pose_only_fallback` 时，策略才可降级为姿态确认。此类运行必须在 metadata/diagnostics 中标记，不能与完整实机结果混合统计。

## 本目录自测

```powershell
& <PYTHON> -B -m pytest -q ral_hil_stage1/platform_patch/tests
```

测试覆盖精确转换哈希、旧 `PolicyContext` 位置参数兼容、无检测帧健康、命令结果传递、`frames.csv`、dry-run、重复安装拒绝、漂移拒绝、端点脱敏和事务回退。

