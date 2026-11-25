# ECR-Cover：AIS 重构、技术忠实性与写作审计报告

## 1. 交付定位

英文稿已由 RA-L Letter 式压缩结构重构为 **Advanced Intelligent Systems Research Article**，标题采用：

> Execution-Confirmed Cooperative Coverage with Baseline-Preserving Repair in Networked Directional Camera Systems

当前英文正文按 `texcount -inc -sum` 统计为 7475 个 text words；摘要 191 词。正文含 4 幅主图、3 个表、10 个编号公式和 4 个命题。Overleaf 工程基于 Wiley 官方 **Optimal Design, Two-Column (June 2026)** 模板整理，匿名题名页及作者、单位、基金、利益冲突、数据可得性、生成式 AI 使用披露均保留明确占位符。

技术事实的唯一来源保持为 `paper/ral_cn_final.md`。扩写仅用于补充问题背景、相关工作、方法解释、实验组织和证据边界；没有加入新的算法步骤、实验、统计方法或结论。

## 2. 结构诊断与重构结果

原稿适合 Letter：方法与实物证据集中，篇幅紧凑，但规划结构、物理执行、日志一致性与统计证据之间的层次不够展开。AIS 稿按以下逻辑重组：

1. **Introduction** 以 planned pointing 与 effective physical observation 的差别建立研究断点，避免把工作限定在光电经纬仪或单一应用。
2. **Related Work** 分为协同覆盖、主动/网络化相机、执行不确定性与韧性分配三条线；引用只承担 framing，不改变方法来源或创新归属。
3. **Problem Formulation** 明确可行覆盖映射、端到端执行时间上界和截断首次观测延迟；UAC 的 expected-delay 解释被限制在静止、均匀和必然检出的既定假设内。
4. **ECR-Cover** 依次描述系统闭环、基线保持初始规划、执行确认覆盖和事件修复；4 个命题分别对应规划结构、日志—模型一致性、条件完整性和模型内修复性质。
5. **Experimental System and Methods** 单列硬件、阈值、时间校准、协议、研究问题与统计方法，明确运行是统计单位，冻结的 100000 次 bootstrap、精确符号检验和 5% 非劣界没有更换。
6. **Results** 按时间模型、标称覆盖、修复消融、证据审计与预算组织；每节区分数据结果、技术解释和证据边界。
7. **Discussion** 集中讨论 planning–execution gap、三层证据关系和 8 项局限，避免在全文重复防御性限定。
8. **Conclusion** 只保留问题、闭环方法和实物证据实际支持的结果，没有外推 scalability、generalization、deployment 或 detection probability。

## 3. 技术忠实性审计

自动校验脚本 `qa/validate_fidelity.py` 以源稿、英文 LaTeX 和中文 Markdown 为三方输入，结果保存在 `qa/fidelity_validation.json`。最终状态为 **PASS**。

已锁定并核对：

- 10 个公式：逐式提取并在去除排版差异后比较数学 LaTeX，内容和顺序一致；
- 4 个命题：前提、结论及证明逻辑不变；
- 240 次分层转移、70 次正式运行、461 次覆盖确认事件、384 个单元；
- 10 个标称配对区组、10 个修复消融区组、50% 触发规则、25 s 逻辑暂停；
- 200 ms 初始规划预算、40 ms 事件修复预算；
- 表 1–3 的全部数值、精度和加粗关系；
- 标称 UAC 差值与区间、扰动比较、repair-on/off 差值、T100 比值和 1.05 非劣界；
- 9 个改善区组和 1 个不利区组均保留；
- ECR-Cover 与实测静态规划的扰动比较仍明确标注为不能单独归因于 repair。

四角交集的表述已按证据强度重新定位：它是冻结实现采用的共同可行覆盖映射，用于降低对名义视场边界单元的依赖；它不是包含站址、安装外参和逐机内参误差的物理置信域，也不是论文的主要创新。离线敏感性结果说明该选择会显著影响候选集合与任务数量，因此不被删除；同时不再把它包装为物理鲁棒性保证。

## 4. Claim audit

以下潜在误写已逐项检查并消除：

- 规划层不劣关系没有写成无条件物理性能保证；
- UAC 没有写成一般目标检测概率；
- 四角交集没有写成物理置信域；
- 配对统计关系没有扩大为跨部署因果或普遍最优性结论；
- 未出现无证据的 robustness、scalability、generalization 或 deployment claim；
- 扰动条件下 ECR-Cover 与静态规划的差异没有错误归因于 repair；
- 唯一不利 repair 区组没有省略；
- 预算结果仅解释为冻结 Windows/Python 平台上的观测合规，不解释为硬实时可调度性证明。

## 5. AI-writing audit

英文稿按 American English 统一。逐段修订重点包括：

- 删除模板化段末总结和无信息量连接词；
- 减少 `this work`、`this study`、`the proposed method` 的机械重复；
- 将 Introduction、Results 与 Discussion 中重复的贡献复述拆开；
- 把正面形容词替换为可核对的结果、条件或比较对象；
- 避免连续三项式模板罗列和不必要的大词；
- Results 先报样本与效应，再解释技术含义，Discussion 才处理外推边界；
- 将局限集中到单独小节，减少正文中反复出现的防御性句式；
- 对长句、名词堆叠和同构句式进行拆分，使语气接近领域作者的正常技术写作。

中文审阅稿与英文稿逐节对应，保留同一公式、命题、图表和结论层级。英文图在中文稿中复用，图注为中文，便于作者直接对照审阅。

## 6. 图表与可复现性审计

4 幅主图和 ToC graphic 均由 `plotting/scripts` 中的 Matplotlib 脚本生成。输入数据集中保存在 `plotting/data`，输出同时提供 PDF、SVG 和 600 dpi PNG。制图约束包括：

- 178 mm 双栏物理宽度设计；
- 色盲友好、低饱和配色，并用线型、标记和填充提供冗余区分；
- 保留运行级原始点和配对连线，不以柱状图或平滑密度掩盖 $n=10$；
- 置信区间直接使用冻结数值，不在绘图时重新估计；
- 去除渐变、阴影卡片、圆角面板和仪表盘式装饰；
- 3 个表均采用三线表，样本说明、精度和加粗逻辑不变。

## 7. 编译与页面检查

- 英文工程已使用本地 TeX Live 完整编译；交叉引用和文献引用均已解析。最终 Overleaf ZIP 另行解压到空目录并从零编译通过。
- 英文 PDF 共 13 页；最终物理尺寸下逐页检查图、表、公式、标题和参考文献。
- 中文 Word 共 10 页，公式为 Word 原生数学对象；通过 Microsoft Word 导出 PDF 后逐页检查。
- 中文 Word 内部对象计数：10 个显示公式、4 幅图、3 个表。
- 4 幅主图及 ToC graphic 均同时提供 PDF/SVG/PNG。
- 源稿及中英文稿 SHA-256、检查项和最终状态记录于 `qa/fidelity_validation.json`。

## 8. 最终变更声明

- changed equations: No
- changed experimental data: No
- added experiments: No
- added unsupported claims: No
- changed conclusions: No

