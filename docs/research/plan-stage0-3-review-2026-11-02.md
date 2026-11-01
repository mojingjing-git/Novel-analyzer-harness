# 2.0 阶段 0-3 计划复审报告（回号优先定位与置信度审计）

> 评审对象：`docs/PLAN-2.0-stage0-2026-11-02.md`  
> 评审日期：2026-11-02  
> 评审结论：**需要修改后执行**（回号优先定位方向正确且实证有效，但同回号多文件升序截胡、低置信度判据逻辑击穿、函数返回值破坏现有解包契约三大缺陷必须修正）。

---

## 1. 代码事实核实（抽查 15 处）

| # | 核查对象 | 计划/背景陈述 | 代码实际情况 | 结论 |
|---|---|---|---|---|
| 1 | `backend/core/text_units.py:37-58` | `load_blocks_as_files` 按自然数序读 blocks，排除非数字 txt | 遍历 `*.txt`，仅保留 `stem.isdigit()` 并按 `int(stem)` 存入字典，非数字文件名（如 `split_report.txt`）收集于 `excluded` 返回 | **属实** |
| 2 | `backend/core/text_units.py:61-72` | `find_literal` 纯字面定位，绝不做模糊回退 | 调 `text.find(needle)`，未命中返回 `None`（防御了 `pos=0` 与 `-1` 判定），未引入任何 `difflib` | **属实** |
| 3 | `backend/core/text_units.py:75-101` | `locate_in_units` 优先试 prefer，否则按键升序取全书最早 | 若 `prefer in units` 优先匹配命中即返；未中则遍历 `sorted(units)` 取全书最早命中单元 | **属实** |
| 4 | `backend/core/text_units.py:104-122` | `build_display_labels` 正则解析回号转阿拉伯数字 | 正则 `_TITLE_RE` 匹配首个 `^\s*第\s*([0-9零〇...]+)\s*[章回节]`，调 `cn_to_int` 生成 `第 {number} 回`，无标题单元跳过 | **属实** |
| 5 | `backend/core/text_units.py:125-139` | `unit_label` 支持章与文件两套标签格式 | `noun="章"` 渲染 `第 {unit} 章`；`noun="文件"` 渲染 `文件{unit}（第 {number} 回）` | **属实** |
| 6 | `backend/services/recheck_v2.py:311` | 复检定位调用 `locate_in_units` | `hit = locate_in_units(chapters or {}, quote, prefer=chapter if index is not None else None)` | **属实** |
| 7 | `backend/services/recheck_v2.py:318-323` | 命中单元与记录章号不符时记录修正 | 当 `owner != chapter`，写入 `outcome.resolved_units` 与 `outcome.resolved_chapters` | **属实** |
| 8 | `backend/services/recheck_v2.py:338-346` | 候选构建字段内容 | 构造字典含 `item`, `candidate_id`, `evidence_id`, `chapter`, `unit` 5 项，无 `locate_mode` 或 `low_confidence` 字段 | **属实** |
| 9 | `backend/services/recheck_v2.py:157-183` | `dump_recheck_detail` 落盘字段 | 包含 `generated_at`, `status`, `coverage`, `verdicts` 等，顶层与 verdict 内部均无 `locate_mode` 与 `low_confidence` | **属实** |
| 10 | `backend/services/final_summary.py:1414-1436` | `_collect_chapters` 读取 blocks 构建单元 | 调用 `load_blocks_as_files` 载入 `units`，统一排除非正文文件 | **属实** |
| 11 | `backend/services/final_summary.py:1502-1515` | 生产调用 `run_recheck_v2` | 传入 `unit_noun="文件"` 与 `display_labels=build_display_labels(chapters)` | **属实** |
| 12 | `scripts/measure_recheck_v2_pipeline.py:277, 322-331` | 测量脚本读取 blocks 与调用复检 | 同样调 `load_blocks_as_files` 与 `build_display_labels` 传给 `run_recheck_v2` | **属实** |
| 13 | `backend/utils/foreshadow_ledger.py:36-61` | `ForeshadowItem` 字段构成 | 明确定义 20 个字段，包含 `anchor_quote`, `anchor_chapter`, `resolved_chapter`, `confidence`, `needs_review` 等 | **属实** |
| 14 | `backend/utils/foreshadow_ledger.py:137-141` | 休眠跨度是否消费 `resolved_chapter` | 休眠判定 `(current_chapter - item.last_seen_chapter) >= DORMANT_CHAPTER_THRESHOLD` 依赖 `last_seen_chapter`；`resolved_chapter` 仅用于报表输出、汇总及前端展示 | **有出入**（休眠计算不使用 `resolved_chapter`） |
| 15 | `scripts/check_anchor_identity.py:98-150` | 脚本当前断言分母结构 | 当前是 `located`、`absent`、`unresolved` **三分母**与 2 项对照指标，计划改动点 6 要求改为四分母 | **属实** |

---

## 2. 核心技术前提判断

### 2.1 回号索引靠得住吗？
- **物理事实**：在《大奉打更人》921 个正文文件中，`build_display_labels` 成功为 **918 个文件** 解析出规范回号，仅 3 个文件无标题（428 尾声无题、916 后记、917 番外篇第〇章）。
- **多文件对应同一回号的物理本质**：实测全书共有 230 个回号对应多个文件。这是因为长篇网络小说采用了**分卷重置章号**结构：
  - 卷一（文件 1~230）：第 1~230 章；
  - 卷二（文件 231~500）：第 1~269 章；
  - 卷三（文件 501~639）：第 1~139 章；
  - 卷四（文件 640~794）：第 1~155 章；
  - 卷五（文件 795~917）：第 1~123 章。
- **结论**：第 96 回对应 5 个文件（96, 326, 596, 735, 890）在物理层面完全真实合规，非分块切分错乱。“回号索引”作为第一级候选过滤网是成立的。

### 2.2 采集侧章号可信吗？
- **效力定位**：采集侧的 `anchor_chapter` 来自大模型在合并块内读到的标题回号。实测表明，在 1669 条带锚点候选里：
  - 按“回号优先”在对应回号的文件列表中检索，命中率达到 **1662 / 1669（99.58%）**；
  - 仅有 7 条未在回号中命中（因为这 7 条恰好位于无标题的 428、916、917 三个文件中，触发全书兜底命中）。
- **结论**：采集侧章号虽不能作为全局文件身份，但作为**文学回号的定位提示（Hint）高度可信**（99.6%），远优于无上下文指引的盲目全书扫描。

### 2.3 "取最早"到底错在哪？（实测 4 条关键伏笔还原）
经直接检索 `workspace/《大奉打更人》/blocks/` 原文与 `output/chapter_*_result.json`，核实情况如下：

| 引文 | 采集侧章号与来源批次 | 全书最早出现文件 | 回号优先命中文件 | 上下文事实还原与伏笔归属 |
|---|---|---|---|---|
| “血屠三千里，血屠三千里，请朝廷派兵讨伐……” | 章号 96（`chapter_325_result.json`） | 文件 324（卷二 第 94 回） | 文件 326（卷二 第 96 回） | **采集意图在 326**：324 是李妙真野外初招残魂，326 是李妙真向许七安展示残魂并正式立案，模型针对 326 提问“残魂与镇北王冲突”，取 324 会丢失许七安交涉现场。 |
| “金刚神功？！” | 章号 124（`chapter_621_result.json`） | 文件 364（卷二 第 134 回） | 文件 624（卷三 第 124 回） | **取最早发生严重张冠李戴**：364 是许七安密林战妖族，624 是武林盟老盟主曹青阳突破三品惊呆净缘。模型针对“曹青阳突破”提问，取最早直接跨卷漂移 260 个文件（近 130 万字），复检证据完全错配。 |
| ““福如……东海，大吉……大利。”” | 章号 171（`chapter_169_result.json`） | 文件 85（卷一 第 85 回） | 文件 171（卷一 第 171 回） | **取最早错配场景**：85 是恒远在寺庙捡到被残害的人犬，171 是许七安与宋廷风查平阳郡主密室发现同类人犬。模型提问针对密室现场，取最早切到恒远念佛，导致破案证据丢失。 |
| “但行好事，莫问前程。” | 章号 106（`chapter_897_result.json`） | 文件 261（卷二 第 31 回） | 文件 900（卷五 第 106 回） | **取最早跨越 639 个文件**：261 是李妙真云州剿匪赠言，900 是卷五少年展开信纸读到赠言。模型在卷五提问反噬，取最早直接翻回全书开篇，时空跨度彻底崩溃。 |

**结论**：计划对这 4 条的定性完全属实。“取最早”在多卷重置回号与金句重复引用场景下，会发生确定性的跨卷错挂与证据张冠李戴。

### 2.4 低置信度阈值 3 是否合理？
- **逻辑击穿**：
  计划定义：“命中文件与采集侧回号索引的位置差 > 阈值（默认 3）→ `low_confidence=True`”。
  但第一阶段是“回号优先”：`round_index[c]` 查找的文件，其解析出的回号**严格等于 c**，两者的回号差值**恒等于 0**！
  实测 1669 条候选里，回号差值分布为：`|hit_round - c| > 0` 的数量是 **0 条**！
  仅有 7 条 fallback 条目由于文件无标题，其 `hit_round` 为 `None`。
- **结论**：阈值 3 是缺乏推演的无意义数字。在回号优先命中下，差值永远为 0；在兜底命中下，差值往往高达几十甚至数百，阈值 3 根本起不到渐进区分的作用。

### 2.5 同回号多文件时"取第一个命中的"是否安全？
- **存在确定性截胡漏洞（重大安全隐患）**：
  在 1669 条全量实测中，发现典型反例：
  - 引文：`“原来是你！”`
  - 采集来源：`chapter_805_result.json`（第五卷，对应文件 806，回号 12）
  - 关联问题：`蛊神为何认识或关注许七安所承载的半国气运……`
  - 检索候选文件：`round_index[12]` 包含 `[12, 242, 512, 651, 806]`（分属 5 卷）。
  - 其中文件 242（卷二第 12 回）与文件 806（卷五第 12 回）同时包含该短句。
  - **若按计划“升序取第一个命中”**：程序直接命中了卷二的 **文件 242**！
  - **后果**：一条关于卷五蛊神与大劫的核心伏笔，被卷二完全无关的同名短语截胡，切窗严重错误。且因两者回号均为 12，差值为 0，**计划设计的低置信度机制完全无法报警**！

---

## 3. 计划挑毛病与盲点审查

### 3.1 改动点 2 破坏现有函数调用契约（解包崩溃）
- 计划改动点 2 拟将 `locate_in_units` 改为“返回命中的文件与命中方式（`round` / `fallback`）”。
- **现有调用方契约**：
  - `recheck_v2.py:314`：`owner, _offset = hit`；
  - `check_anchor_identity.py:110`：`unit, offset = hit`；
  - `test_text_units_identity.py:48, 54`：`assert locate_in_units(...) == (2, 4)`。
- **后果**：直接增加第三个返回值或修改元组结构，将导致现有生产与测试代码全线抛出 `ValueError: too many values to unpack`。必须保持 `(unit, offset)` 基础签名，或通过命名元组/扩展对象向前兼容。

### 3.2 改动点 3 & 4 的明细字段缺乏数据模型支撑
- 计划承诺将 `locate_mode` 与 `low_confidence` 写进“逐条明细”。
- **事实**：`dump_recheck_detail` 的明细来源于 `CandidateVerdict.to_dict()`（`backend/vnext/resolution.py:92`），该类是 `@dataclass(frozen=True)`，并无此两字段。若不修改数据结构，明细根本无处落盘。
- 此外，对于未能进入 candidates 的条目（如 `dropped_anchor_missing`），根本没有 `CandidateVerdict` 实例，此类条目的定位诊断信息将彻底丢失。

### 3.3 `low_confidence` 标记无任何消费者，沦为死代码
- 计划明确强调：“不自动降级判定……只标记，不改判定、不改状态”。
- 但前端、API 接口、总结报告均未设计对该字段的展示或消费。既不触发人工复核（`needs_review`），又不影响通过门禁，在实际数据上触发数又为 0，该字段当前纯属形式主义产物。

### 3.4 改动点 5 传参冗余，破坏接口封装
- 计划改动点 5 要求在 `final_summary.py` 与 `measure_recheck_v2_pipeline.py` 中分别调用 `build_round_index` 并传入 `run_recheck_v2`。
- `run_recheck_v2` 已经接收了 `display_labels`。由 `display_labels` 构建 `round_index` 仅需 2 行倒排逻辑。在顶层调用链路新增参数不仅破坏封装，还增加了多处维护口径漂移的风险。

### 3.5 验收标准“兜底率 ≤ 5%”缺乏实质性约束力
- 实测表明，在无标题文件外，正常回号优先的覆盖率为 99.58%，兜底率仅 **0.42%**（7 条）。
- 5% 的阈值过于宽松（相当于允许多达 83 条错误降级）。必须将指标形式化为：“无标题单元命中外，回号索引覆盖率应 ≥ 99%”。

### 3.6 风险表遗漏项
1. **多卷同回号短引文升序截胡风险**（如“原来是你！”命中卷二而非卷五，见 2.5 节）。
2. **`locate_in_units` 返回值破坏性变更导致现有解包报错的回归风险**。
3. **`CandidateVerdict` 缺少字段导致明细序列化失效风险**。

---

## 4. 结论与分级整改建议

### 一句话结论
**需要修改后执行**：回号优先定位策略能将锚点命中准确度从 21.9% 提升至 99.6%，定性方向完全正确；但计划存在同回号多文件升序截胡、低置信度判据逻辑击穿、以及修改函数返回值引发调用解包崩溃三大硬伤，必须修正设计后方可动工。

### P1 / P2 / P3 分级表

| 级别 | 问题与修改动作（每条一句话） |
|---|---|
| **P1** | 修正同回号多文件检索策略，当命中多文件时禁止机械升序，须结合条目来源批次（batch）选择物理距离最近的文件以防跨卷截胡。 |
| **P1** | 严禁破坏 `locate_in_units` 现有的 `(unit, offset)` 返回值解包契约，命中方式应通过命名元组属性或专用查询函数提供。 |
| **P1** | 明确 `locate_mode` 与置信度标记的数据载体，在 `CandidateVerdict` 与 `RecheckOutcome` 中补齐定义及明细导出契约。 |
| **P2** | 废弃无效的“回号位置差 > 3”判据，将低置信度定义修正为：全书兜底命中（fallback）、或同回号命中但出现跨卷物理跳跃。 |
| **P2** | 简化调用接口，由 `run_recheck_v2` 内部直接从 `display_labels` 派生回号索引，避免在调用方层层增加入参。 |
| **P3** | 收紧验收标准门禁，将宽松的“兜底 ≤ 5%”精确定义为“除已知无标题正文单元（≤1%）外，其余必须全部经回号索引精确命中”。 |

---

## 5. 开工前必须先定的事

1. **定死多卷同名章的消歧策略**：
   - 当 `round_index[c]` 包含多个文件且均包含引文字面时，必须利用伏笔的来源信息（账本 `first_seen_batch` 或采集批次所属文件范围）选择物理序号最贴近的文件，严禁盲目按文件序号升序截胡。
2. **定死 `locate_in_units` 的契约兼容性**：
   - 决定是采用扩展类 `NamedTuple`（如 `LocateHit(unit, offset, mode)`，允许解包为前两项）还是通过入参标志/独立函数返回，确保现有解包代码 100% 不坏。
3. **决定 `low_confidence` 的真实用途**：
   - 若保留，必须明确其实际消费场景（例如：回写账本 `item.needs_review = True`，或在 `recheck_verdicts.json` 顶层单列待审计列表）；若无任何消费者，应立即从生产字段中剔除，仅保留在审计诊断输出中。
