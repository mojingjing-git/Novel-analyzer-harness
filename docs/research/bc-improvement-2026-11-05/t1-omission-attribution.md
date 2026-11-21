# M1 / T1 B 类遗漏全链路追踪与归因报告（2026-11-05）

本文件对应 [BC改进任务书.md](file:///F:/AI/01_项目/小说分析器/docs/BC改进任务书.md) 中的 **T1｜建立 B 类遗漏追踪（M1，P0）** 交付物。机读归因结果见同目录 [t1-omission-attribution.json](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/t1-omission-attribution.json)，核心追踪器见 [backend/common/b_trace.py](file:///F:/AI/01_项目/小说分析器/backend/common/b_trace.py)，离线回放脚本见 [scripts/bc_trace_audit.py](file:///F:/AI/01_项目/小说分析器/scripts/bc_trace_audit.py)，回归测试见 [backend/tests/test_b_trace.py](file:///F:/AI/01_项目/小说分析器/backend/tests/test_b_trace.py)。

## 1. 追踪架构与零侵入保障

- **纯标准库追踪器**：在 [backend/common/b_trace.py](file:///F:/AI/01_项目/小说分析器/backend/common/b_trace.py) 中实现 [`TraceRecord`](file:///F:/AI/01_项目/小说分析器/backend/common/b_trace.py#L94-L119)、[`BTraceCollector`](file:///F:/AI/01_项目/小说分析器/backend/common/b_trace.py#L122-L202) 与 [`attribute_reference_observations`](file:///F:/AI/01_项目/小说分析器/backend/common/b_trace.py#L256-L445)，包含任务书要求的全部统一字段（`run_id`、`source_sha256`、`source_span`、`reference_obs_id`、`item_id`、`stage`、`state_before`、`state_after`、`reason_code`、`category`、`artifact_path`、`elapsed_ms`、`call_stats`）。
- **可选开关零副作用**：
  - 在 [`deduplicate_foreshadows`](file:///F:/AI/01_项目/小说分析器/backend/utils/text_utils.py#L193-L385)、[`FinalSummaryRunner._build_foreshadow_catalog`](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L917-L1120) 与 [`run_recheck_v2`](file:///F:/AI/01_项目/小说分析器/backend/services/recheck_v2.py#L247-L710) 中接入默认关闭的 `trace: Any = None` 参数。
  - 单元测试 [`test_b_trace.py`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_b_trace.py) 验证：当 `trace=None` 或开启 `trace` 时，业务返回值与账本状态逐字段完全一致；未知原因码显式归入 `UNLOCATED`，绝不静默归零。

## 2. 存量工作区全链路阶段漏斗与损失表

通过 [scripts/bc_trace_audit.py](file:///F:/AI/01_项目/小说分析器/scripts/bc_trace_audit.py) 对《道诡异仙》与《大奉打更人》两部完整存量工作区做全链路回放（0 次外部 API 调用）：

| 管线阶段 / 指标 | 《道诡异仙》（262 块 / 1046 章） | 《大奉打更人》（232 块 / 922 章） | 归因类别与说明 |
|---|---:|---:|---|
| 章节结果文件数 (`result_files`) | 262 | 226 | 正常产出章节块 |
| 章节解析跳过文件数 (`PARSE_JSON_FAILED`) | 0 | **6** | **B4 流转丢失**：6 个块（24 章）因 JSON 解析失败生成 `_skipped.json`，整块伏笔丢失 |
| 空伏笔块数 (`EMPTY_CHAPTER_FORESHADOWS`) | 0 | **1** | **B1 登记遗漏**：该块未提取出任何伏笔记录 |
| 原始提取伏笔总数 (`raw_clues_total`) | 1,683 | 1,880 | 各块 `foreshadowing` 原始条目总和 |
| 长度过滤丢弃 (`SHORT_CLUE_DROPPED`, `<15` 字) | 1 | 1 | **B4 流转丢失**：短描述直接丢弃 |
| 重要度/置信度过滤丢弃 (`IMPORTANCE/CONFIDENCE_FILTERED`) | 129 | 37 | **B4 流转丢失**：`importance=低` 或 `confidence=低` 被过滤 |
| 进入去重候选数 (`after_filter`) | 1,553 | 1,842 | 过滤后保留条目 |
| 去重后条目数 (`after_dedup`) | 1,552（仅合并 1 条） | 1,842（合并 0 条） | **去重形同虚设**：`SequenceMatcher >= 0.6` 对跨章长句几乎不触发合并 |
| 高重要度截断丢弃 (`HIGH_CAP_TRUNCATED`, 上限 500) | **357**（857 $\to$ 500） | **569**（1,069 $\to$ 500） | **B4 流转丢失（头号损失源）**：同分稳定排序下直接切掉全书后部高重要度伏笔 |
| 中重要度截断丢弃 (`MID_CAP_TRUNCATED`, 上限 200) | **495**（695 $\to$ 200） | **573**（773 $\to$ 200） | **B4 流转丢失（头号损失源）**：同分稳定排序下直接切掉后部中重要度伏笔 |
| 入账总表条目数 (`ledger_total`) | 700 | 700 | 硬性截断在 500 高 + 200 中 |
| 复检缺锚点丢弃 (`DROPPED_NO_ANCHOR`) | **37** | — | **B4 流转丢失**：缺少 `anchor_quote` 或 `anchor_chapter <= 0` 未进复检 |
| 提名回收章未进证据包 (`NOMINATED_CHAPTER_NOT_IN_BUNDLE`) | **69 / 97**（active 提名项） | — | **B2 取证遗漏**：阶段一调和提名的 `resolved_chapter` 未传入 `bundle_for` |
| 证据命中超窗截断 (`EVIDENCE_HITS_TRUNCATED`) | **492** | — | **B2 取证遗漏**：关键词命中章超过 5 个窗口上限被截断 |
| 提名章已进包但判 `INSUFFICIENT` / `CONFLICT` | **44**（28 + 16） | — | **B3 判定遗漏**：证据已进窗口但模型未完成关闭 |
| 复检 `INVALID`（`FOREIGN_ID` 12 + `QUOTE_NOT_FOUND` 9） | **21** | — | **B4 流转丢失**：批次串号或跨段引文 ID 错配，且补问轮 `step=len(queue)` 打包失败 |
| `knowledge.json` 事实上限覆盖 (`KB_CONTEXT_TRIMMED`) | `verified_facts = 200`（触顶） | `verified_facts = 200`（触顶） | **B4 流转丢失**：直接切片覆盖持久化列表，未分离完整存储与上下文视图 |
| **已记录损失事件原因覆盖率 (`event_reason_coverage_rate`)** | **100.0% (`unlocated = 0`)** | **100.0% (`unlocated = 0`)** | 仅度量管线内部已记录损失事件的已知原因映射率（当 `loss_events == 0` 时返回 `None` 而非 `1.0`） |
| **独立参考遗漏归因率 (`reference_attribution_rate`)** | **`N/A`（无全书盲标参考集）** | **`N/A`（无全书盲标参考集）** | 严格区分于内部事件覆盖率：未传入独立参考观测集时显式标记 `N/A`；在 8 部带独立盲标参考集的作品上实测为 **100.0%**（开发集 `34/34`、留出集 `58/58`，未匹配参考遗漏显式计入 `reference_omissions_unmatched`） |

## 3. 核心根因诊断与 T3 修复优先级排序

基于全链路追踪数据，跨作品最突出的两类确定性主因如下（将在 T3 中通过特性开关与回归测试定向修复）：

1. **根因一（B4 流转丢失：去重失灵 + 硬截断切尾 + 完整存储被覆盖）**：
   - [`deduplicate_foreshadows`](file:///F:/AI/01_项目/小说分析器/backend/utils/text_utils.py#L193-L385) 仅靠全文 `SequenceMatcher >= 0.6` 精筛，导致跨章同一母题（`motif_key` 相同或核心实体+问题相同）的线索几乎 0 合并（《道诡异仙》1553 条仅合并 1 条，《大奉打更人》1842 条合并 0 条）；同时若代表条目缺锚点/三问而同组后文条目有锚点/三问，合并时还会丢失后文锚点与三问。
   - 因为去重不合并，99.9% 条目的 `evidence_count=1, span=0`，所有同重要度+同置信度条目 `composite_score` 完全并列（如高+高均为 `331`）。Python 稳定排序保持章节升序后，`high_items[:500]` 与 `mid_items[:200]` **直接把全书后半程一刀切掉**：《道诡异仙》第 753 章之后 **0 条**高重要度伏笔入账（丢弃 357 条高 + 495 条中），《大奉打更人》第 541 章之后 **0 条**伏笔入账（丢弃 569 条高 + 573 条中）！
   - 同时 [`KnowledgeBaseManager._trim_kb`](file:///F:/AI/01_项目/小说分析器/backend/core/knowledge_base.py#L305-L312) 与总表截断未分离“完整持久化存储”与“送入模型的预算裁剪视图”。
2. **根因二（B2 取证遗漏 + B4 复检串号/补问批次缺陷）**：
   - [`run_recheck_v2`](file:///F:/AI/01_项目/小说分析器/backend/services/recheck_v2.py#L410-L419) 调用 `index.bundle_for` 时只传了 `[question, description]`，漏传阶段一调和已提名的 `resolved_chapter`、`resolution`、`answer_criteria`、`entity_key` 与 `evidence_chapters`，导致《道诡异仙》97 条带提名章的待核项中有 **69 条（71.1%）** 连提名回收章都没进入复检证据包！
   - [`EvidenceIndex.find_mentions`](file:///F:/AI/01_项目/小说分析器/backend/core/evidence.py#L206) 严格跳过 `ch <= anchor_chapter`，导致同章/同块内锚点后方的就地回收无法被检索，且单章多命中时只取首个关键词而不按多关键词密度选窗。
   - [`run_recheck_v2`](file:///F:/AI/01_项目/小说分析器/backend/services/recheck_v2.py#L553) 补问轮写成了 `step = len(queue) if attempt == 1 else max(batch_size, 1)`，将全书所有待补问候选塞进单次超大请求，且 `_consume` 未对同候选内跨证据段的唯一逐字引文做段内 ID 对齐，导致 21 条候选落入 `INVALID`（12 条 `FOREIGN_ID`、9 条 `QUOTE_NOT_FOUND`）。
