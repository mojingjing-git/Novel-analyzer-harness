# PLAN：2.0 阶段 0-2 —— 正文身份落到文件级，复检不再依赖章号

> 阶段 0 的第二个动作。承接 [PLAN-2.0-merge-2026-11-02.md](./PLAN-2.0-merge-2026-11-02.md)
> 的 D-1，但**方案有转向**，需复审。
> 前置：复检 V2 已落盘逐条明细、记账三态、补问、状态语义；
> 锚点定位已有"章号指错 → 全书回退搜索"的兜底（`backend/services/recheck_v2.py`）。

---

## 1. 新事实（本阶段要解决的根因）

实测（《大奉打更人》922 文件 / 434.9 万字 / 1669 条带锚点候选）：

| 口径 | 按采集记录的章号能定位 |
|---|---|
| 复检侧按**单文件**切分（章号 = 文件序号 1..922） | **366 / 1669 = 22%** |
| 事后**复现**采集侧的合并块切分（章号 = 块内标题反查的真实回号） | 512 / 1669 = **31%** |

两套章号空间根本不对齐：

- 采集侧（`backend/core/analyzer.py:233` → `verify_result_anchors` → `build_chapter_spans`）
  记的是**合并块内按标题反查出的回号**（如 12、13、14、15）；
- 复检侧按单文件切分得到的是**文件序号**（1..922）。

而且合并块口径只有 31%，说明**采集时的块文本与事后重建的不完全一致**
（分隔符/预处理/块边界都参与其中）——所以"复现采集侧口径"这条路本身不可靠。

结论：**章号不能当身份**。正文身份必须是与切分无关的东西。

## 2. 两个方案

### 方案 A：采集侧把锚点重新定位到文件级（记 `anchor_file_index` + `anchor_content_hash`）

- 采集落盘前，在**块的文件范围内**逐文件找引文 → 记文件序号与该文件正文哈希。
- 复检侧按 `(file_index, hash)` 直接定位。
- 优点：主路径快、兜底少、账本可审计。
- 代价：
  - 要把"块 → 文件列表"这条信息从 pipeline 传进 analyzer（`analyze_block_with_progress`
    目前只给块文本与 `chapter_number`）；
  - 结果模型（`models/analysis_result.py:32-43`）、账本 v5（`utils/foreshadow_ledger.py:24`）、
    **以及 `text_utils.deduplicate_foreshadows` 的位置契约**（`:246-255`，第 6..10 位）
    都要扩位（→ 第 6..12 位），波及所有调用方；
  - **历史产物没有这两个字段** → 所有已分析的书只能走降级路径。

### 方案 B（**本次采用**）：复检侧以"引文在全书的位置"为身份，文件只是展示与取窗单位

- 复检侧把 `blocks` 目录按**文件级**建映射：`{file_index: 文件全文}`；
  `EvidenceIndex` 的"章"就是"文件"（它本来就是按文本单元做检索与取窗，**不用改**）。
- 候选定位：**按引文在全书搜索** → 得出 `(file_index, 位置)`；取窗以该文件为单位。
- 同时把 `anchor_file_index` / `anchor_content_hash` 记进**逐条明细**（可审计、可增量）。
- 优点：
  - **不动采集侧** → 不碰 pipeline 数据流、不碰结果模型、不碰账本、不碰位置契约；
  - **历史产物天然可用**（引文本来就是逐字存在的）→ 没有兼容问题；
  - 定位率天然 100%（引文定位失败的那些本来就该记 missing）；
  - 语义更对：伏笔埋设处 = 引文在全书的最早出现（后面的重复引用不是埋设处）。
- 代价：每次复检要先扫一遍全书（1669 条 × 434 万字符，`str.find` 是 C 实现，
  实测秒级）；章节切分（回号）退化为展示用途。

**为什么 B 更对**：身份要**与切分无关**。引文的全局位置 + 所在文件就是身份；
章号/回号只是"这本书怎么分章"的产物，随时可能变（今天它就变了三次）。

## 3. 具体改动

| # | 文件:行 | 改动 |
|---|---|---|
| 1 | `backend/services/recheck_v2.py`（候选构建段） | 定位改为**按文件级**：先在候选记录的章文本里找（快路径，留作对照指标），失败则在**全书文本**里找 → 得到 `file_index`；**不再把"定位不到"直接丢弃** |
| 2 | 同上 | 候选的"章"改用 `file_index`（`EvidenceIndex` 的章号即文件序号）；`resolved_chapters` 改名/扩义为 `resolved_files`（记录 file_index → 原记录章号），审计可回看 |
| 3 | `backend/services/recheck_v2.py`（`dump_recheck_detail`） | 明细里加 `anchor_file_index` / `anchor_content_hash`（每条候选） |
| 4 | `backend/services/final_summary.py:_collect_chapters` | **按文件级**建映射（当前是 `split_block_into_chapters` 切出的"章"）；文件序号 = 排序后的序号 |
| 5 | `scripts/measure_recheck_v2_pipeline.py` | 同上：chapters 按文件级建（当前是按标题切分） |
| 6 | 新增 `scripts/check_anchor_identity.py` | **离线断言脚本**（不调模型、不花钱）：用已有产物算三个指标——① 按记录章号直接命中 ② 按文件级定位命中 ③ 定位失败 |
| 7 | `backend/tests/test_recheck_anchor_file_identity.py` | 单测：文件级定位命中；引文在多文件出现时取最早；引文不存在时记 missing；`resolved_files` 记录正确 |

**不改动**：`models/analysis_result.py`、`utils/foreshadow_ledger.py`、`utils/text_utils.py`
的位置契约、`core/pipeline.py`、`core/analyzer.py`、`core/anchor.py`。

## 4. 验收（全部可脚本判定，不调模型）

| 项 | 标准 | 实测（2026-11-02） |
|---|---|---|
| 文件级定位率 | **确切的 1669 条候选**：逐字命中 / 确凿不存在 / 存在但定位失败 | **1669 / 0 / 0** → 100% |
| 按记录章号直接命中 | 对照指标（历史基线 22%） | 366 = **21.9%** |
| 非正文文件排除 | `split_report.txt` 不得进单元集 | 已排除（921 单元，原 922） |
| 多文件同句引文 | 抽查清单（取最早，需人工核） | 13 条 |
| 历史兼容 | 旧产物（旧账本、无 file_index）直接可跑 | 无需迁移 |
| 单测 | P1-1/2/3 三条各有回归测试 | 11 例 |
| 全量门禁 | `scripts/run_tests.py` 全绿 | **1686 passed / 67.8s** |

断言脚本：`scripts/check_anchor_identity.py`（不调模型、不花钱，4.4 s 跑完整本）。

## 5. AGY 复审整改（P1 全部采纳）

| P1 | 整改 | 落点 |
|---|---|---|
| **P1-1** 全书搜索潜伏 `difflib`（`locate_quote` → `citations.verify_citation` → `SequenceMatcher`，435 万字符上是 $O(N\cdot M)$，单次卡死几分钟） | 全书定位改用**纯字面** `str.find`（`text_units.locate_in_units`），模糊回退只允许在**章级**文本上跑 | `backend/core/text_units.py`；回归测试把 `citations.verify_citation` 换成会炸的实现，定位仍须成功 |
| **P1-2** 账本 `resolved_chapter` 被注入文件序号 | 单元键（文件序号）与**文学章号**分离：`resolved_units` 记单元键，`resolved_chapter` 一律经 `_display_chapter` 转回回号（2.0）或保持单元键（1.x），**任何情况都不写文件序号** | `recheck_v2._display_chapter` / `_apply_state` |
| **P1-3** 提示词里文件序号冒充章号 | 证据标签改 `文件366（第 12 回）`，候选元数据加一句说明"两套编号不是同一套"；1.x 默认行为完全不变 | `evidence.bundle_for(unit_noun=…)` / `recheck_v2._seg_label` |
| P2-1 双口径 | 抽出 `text_units.load_blocks_as_files`，生产路径与测量脚本共用 | `backend/core/text_units.py` |
| P2-2 验收同义反复 | 改成三分母断言（命中 / 确凿不存在 / **存在但定位失败，必须为 0**） | `scripts/check_anchor_identity.py` |
| P3 跨度合理性 | 报告"多文件同句引文"清单（13 条）供人工抽查；本阶段不自动降级 | 同上 |

### 顺带修掉的一个真 bug

`str.find` 没找到返回 **-1** 而不是 `None` → `locate_in_units` 的 `is not None` 判断永远成立，
把"没找到"当成"在第 0 个单元命中"。回归测试已锁住（`test_find_literal_returns_none_when_missing`）。

## 6. 已知边界（本阶段不修，留档）

- **单文件多章的书**：`EvidenceIndex.find_mentions(after=unit)` 只在**更靠后的单元**里检索，
  同一个文件内部的跨章回收会漏查。检测办法：文件内能解析出多个章标题即为高风险。
  《大奉打更人》是 1 文件 = 1 章，不受影响。
- **引文在多处出现**：取**最早**出现（埋设处）。13 条多文件引文需人工抽查
  （短引文/套话有误挂风险，AGY P3）。

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 引文在全书多处出现 → 取错窗口 | 取**最早**出现（埋设处）；`resolved_units` 记录，审计可查；`quote` 仍逐字校验 |
| 全书扫描的性能 | 纯字面 `str.find`（BMH，C 实现）；实测 1669 条 × 434 万字 = **4.4 s** |
| 改变了复检的取窗语义（从"章内窗口"到"文件内窗口"） | 文件 = 一章（1.x 约定），窗口半径不变（`WINDOW_RADIUS=400` 字符） |
| 提示词格式变化影响既有基线 | 1.x 默认 `unit_noun="章"` 行为**逐字不变**；生产路径显式传"文件"，即 2.0 口径 |
| 回滚 | 改动集中在 `recheck_v2.py`、`text_units.py`（新）、`evidence.py`（参数化）、两个调用点；`git revert` 即可 |

## 8. 不做什么

- 不改采集侧（不记 file_index 到账本）——账本 schema 不动。
- 不改账本 schema、不改结果模型、不改 `text_utils` 的位置契约。
- 不动 `core/anchor.py` 的旧切章器（1.x 仍在用）。
- 不重跑整本（阶段 0 收口时才跑，用新代码跑一遍并对比指标）。
