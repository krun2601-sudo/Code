# 阶段一交付目录

- `ral_hil_stage1_phase1.zip`：可移交的阶段一代码、配置、计划结构草案、协议和中文稿；不含原现场 YAML、IP/RTSP、检测权重、私有预检快照、仿真运行目录或缓存。正式主实验计划须在 commissioning 与时间模型审核完成后重新生成。
- `ral_hil_stage1_phase1.zip.sha256`：压缩包 SHA-256。
- `package_manifest.sha256.csv`：压缩包中每个受管文件的相对路径、字节数和 SHA-256；清单自身不递归列入。
- `VALIDATION.md`：交付验收摘要。

压缩包不是实机启动授权。平台补丁和策略安装器均默认 dry-run；真实运动仍需操作者显式 `--arm`，并先完成实时预检、限位/符号检查和急停演练。
