# 2.0 管线：形态与缺口清单

> 本文描述**当前状态**与 2.0 的目标形态，不写变更流水账。
> 背景数据：`docs/baseline-data.md`（实测基线）、`docs/issues.md`（仍成立的问题）。

## 1. 为什么要合并

仓库里现在并存两条管线：

| | 1.x 生产管线 | vnext |
|---|---|---|
| 代码 | `backend/core/pipeline.py`(1268) + `backend/services/final_summary.py`(1970) + `backend/utils/memory_state.py` + `backend/utils/foreshadow_ledger.py` | `backend/vnext/`（25 个模块）+ `backend/services/vnext_service.py` + `backend/api/routes_vnext.py` |
| 强项 | **作业层完整**：分块分析 → 滚动摘要 → 伏笔账本 → 四阶段总结 → 报告；前端 78 文件 / 12631 行 | **数据层语义强**：有序覆盖、排他快照、来源溯源、证据门禁、预算与缓存、熔断、原子写 |
| 弱项 | 数据层语义弱（见下） | **没有作业层**：`run_analysis` 0 处生产调用；`cutoff_view`/`build_coverage` 仍是 0 调用（`breaker`/`PromptLimits` 已收割进 core，见缺口表） |

所以现状不是"新管线更好"，而是**旧管线跑得起来但语义不可靠，新管线语义可靠但跑不起来**。
2.0 = 用 1.x 的作业层做骨架，把 vnext 的语义接进去，然后**只保留一条**。

## 2. 缺口清单

### A. 接线缺口：vnext 的机制接进 1.x 的作业层

| 缺口 | 现状 | 影响 |
|---|---|---|
| `run_analysis` 没有生产入口 | 0 处非测试调用 | vnext 整条分析链在生产上跑不起来 |
| ~~`PromptLimits` 没接配置~~ 已接 | `backend/core/prompt_limits.py`，analyzer 全 9 项来自 config | 4 个新配置字段 `max_relationships` / `max_themes` / `max_content_chars` / `max_verified_facts_in_prompt`（与知识库裁剪字段 `max_verified_facts` 分层）；vnext 侧留转发桩 |
| ~~账户级熔断没有调用方~~ 已接 | `backend/core/breaker.py`，`llm_client` 逐调用记账 + 各入口短路 | 欠费/认证失效第一次结构化命中即跳闸，同实例后续调用零成本短路，回包携带跳闸 verdict 供 pipeline 登记致命块；队列级提前停止未做（调用快速排空失败，零 API 成本） |
| 排他快照没进知识库写入 | `cutoff_view` 0 处非测试调用 | 1.x 的滚动摘要与人物状态只有一个 `last_updated_chapter` 标量 → 越早补跑的块既进不了摘要，又会被后面高章号的结果覆盖 |
| 有序覆盖没进 1.x | `build_coverage` 0 处非测试调用 | 覆盖表按"点数与文件集合不得变化"的旧口径冻结，抽公共调用函数就会失真 |
| 子产物原子写 | `backend/utils/aggregate_utils.py` 约 10 处 `open(...,'w')+json.dump` | 写到一半中断会留下半个 JSON |

### B. 语义冲突：两边都有同一件事，口径不同，合并时必须选一个

| 冲突点 | 1.x 的做法 | vnext 的做法 | 建议 |
|---|---|---|---|
| **章切分口径** | `book_service.py:175-177` 一文件=一章；块=章 | `backend/core/anchor.py:89` `build_chapter_spans` 按标题反查回号 | 取 vnext，但**必须解决回号重复**：源文件里行首的"第N回"会被标题正则当成边界，实测《大奉打更人》合并块 spans 形如 `[229, 230, 1, 2]`——同一章号在书里反复出现。2.0 的正文身份应是 `(章号, 引文)` 而不是章号 |
| **快照与覆盖** | 标量上界（`pipeline.py:448,1069` 的 `last_updated_chapter`） | 排他快照 `exclusive_before`（`vnext/knowledge.py:148-198,341`） | 取 vnext |
| **共现与关系** | 同章两两配对直接写成关系（`memory_state.py:388-394`） | 共现与语义关系分表（`vnext/graph.py`：`CooccurrenceEdge` vs `SemanticRelationship`，"共现不是关系"） | 取 vnext |
| **伏笔终态** | dormant 判死（`foreshadow_ledger.py:142`；`final_summary.py:1487-1489` 因 active=0 直接跳过全书复检） | `cutoff_view`（`vnext/foreshadow.py`）按故事位置出候选、**不判死** | 取 vnext；复检用**带证据**那条（`recheck_v2`），不是旧的无阻断路径 |
| **账本** | `foreshadow_ledger.json`，`SCHEMA_VERSION = 5`（`foreshadow_ledger.py:24`） | 自有 `recheck_ledger.json` + `tasks.json`（`vnext/runner.py`） | 合并成一套，取 v5 的形状 |
| **已知事实来源** | `verified_facts` 原样入账，无法溯源 | `SourceRef` 溯源 | 取 vnext |
| **错误分类** | `backend/core/error_taxonomy.py` 一套枚举与置信度 | `backend/core/error_types.py` 的 `ErrorVerdict`/`ErrorScope` 4 值，判据只认结构化通道 | 取 vnext，并把 1.x 的 `llm_client` 四处重试决策收敛到单点 |
| **提示词限额** | 9 项统一在 `backend/core/prompt_limits.py`，analyzer 全量接配置（`max_relationships` / `max_themes` / `max_content_chars` / `max_verified_facts_in_prompt` 为新增字段） | `backend/vnext/prompt_limits.py` 转发桩 | **已取 vnext** |
| **证据 id** | `backend/core/evidence.py:84` | `backend/vnext/fingerprint.py` 的 `evidence_id`（内容哈希口径） | **两套同名实现**，合并时必须选一个 |
| **裁剪时机** | 落盘前裁剪（`memory_state.py:414`）——"库里有什么取决于当初配了多大窗口" | 窗口是**读取时**的动作（`vnext/prompt_view.py`），裁剪时如实报告丢了什么 | 取 vnext |
| 引用定位 | — | — | **已统一**（`vnext/citations.py` 已收敛到 `backend/core/citations.py`）✅ |

### C. 两侧都没有的功能

1. **总结/最终报告链路**：vnext 全库无 `final_report` 生成（只有 `vnext/budget.py:34,225` 的预算项），
   1.x 的 `final_summary.py` 有 1969 行四阶段。**这条不是接线，是新写。**
2. **伏笔总表 + 50 类分类**：vnext 只有 occurrence/entity/candidate 三层，无总表、无分类。
3. **队列执行器**：vnext 的 `queue.py` 只有状态读写（`normalize_after_restart`/`apply_progress`），
   **没有执行器**；1.x 有 `queue_service.py` / `queue_manager.py`。
4. **分析启动入口**：vnext 的 9 个端点（`routes_vnext.py:72-213`）无启动、无总结、无报告。
5. **判定质量门禁**：0 成本的"答案引用证据窗口外章号"（实测命中 11/208，人工确认 11/11 都是答非所问）；
   以及"同款模型盲答复核"（弱模型复核会误伤，精确率约 80%）。两条都还没实现。
6. **人工标注定标集**：判定质量至今没有可回归的标注基线（现有 `annotations-C2` 是 30 条引文样本，不是判定标注）。
7. **产物 schema 版本化**：输出 JSON 的 schema 仍在演化，跨版本升级后依赖旧字段的脚本会坏。
8. **配置面合并**：`settings.py` 里 vnext 相关几乎为零（只有通用的 `workspace_dir`），
   vnext 自带 `config_vnext.json`，且**与 `config.json` 的 `workspace_dir` 没有任何联动**——两套配置各管各的。
9. **`blocks` 目录只放正文**：切分报告（`split_report.txt`）被当成第 922 章参与分析与复检（两轮都如此，是既有行为）。
10. **风格分析 / 聚合产物**：vnext 无 `style_analyzer`、无 `aggregate_utils` 的等价物。

### D. 前端

- vnext 页面与后端字段名不一致（`docs/issues.md` 已列；其中"总章数"一条已由
  `vnext_service.py:78` 修好，剩下 blocks 的 `state`/`status`、queue 的 `total_chapters` 仍在）：
  `frontend/src/pages/VnextDetailPage.vue:30` 读 `b.status === 'done'`，
  后端给的是 `state`（`vnext_service.py:319-330`）→ 块状态列空白；
  `frontend/src/api/client.ts:386-395` 声明的 `total_chapters`，`routes_vnext.py:92-105` 不返回它。
- 前端没有"2.0"视图：现在的 `LaneView` / `QueuePage` / `SummaryPage` 混着两条管线的概念。
- `summary` 自研 Markdown 渲染器 + `v-html`：报告内容里的 HTML 不转义，而报告来自模型输出。

### E. vnext 模块的调用方分布（2.0 接线工作量的直接依据）

| 类别 | 模块 | 数量 |
|---|---|---|
| 有生产调用方 | artifacts、blocks、constants、project、queue、registry、results、**resolution** | 8 / 25（其中 7 个只被 vnext 自己的 API 层调用，**只有 resolution 进了 1.x 生产路径**）|
| 已收敛到 `backend/core/`（vnext 留转发桩） | citations、prompt_limits、breaker | 3 |
| 仅 `scripts/` 调用（实现完整、有验收脚本，但无产品入口） | budget、context、foreshadow、knowledge、prompt_view、recheck_plan、rolling、runner、tasks | 9 |
| 无任何外部调用 | analyzer、concurrency、consistency、fingerprint、graph | 5 |

最后那 5 个**不是死代码，是建好了还没人用的地基**——`consistency`（原子提交）、
`graph`（共现/关系分表）、`fingerprint`（指纹口径）、`concurrency`（结算只认真结束），
正是 2.0 最该接上的部分。`breaker` 与 `prompt_limits` 已收割进 core 并接入 1.x
（见缺口表）。

详细盘点（含每条的 file:line 与 grep 证据）见
[research/vnext-audit-2026-11-02.md](./research/vnext-audit-2026-11-02.md)。

## 3. 2.0 的形态（建议）

```
原文 blocks
   ↓  切章（统一口径，正文身份 = 文件序号 + 该文件正文）
   ↓  分析（PromptLimits 全量接配置 + 账户级熔断 + 原子写 + 结果登记）
   ↓  知识库（排他快照 + 有序归约 + SourceRef 溯源 + 共现/语义分表）
   ↓  伏笔账本 v5（锚点 + 三问 + 稳定实体键）
   ↓  复检 V2（物理块范围定位 + 有界证据包 + 逐条落盘 + 记账三态 + 补问）
   ↓  判定门禁（0 成本章号闭包 + 同款模型盲答复核）
   ↓  总结（卷摘要 → 复检 → 风格 → 报告；checkpoint 覆盖全阶段）
报告四栏（未回收 / 自动已回收 / 证据不足 / 待确认）
   ↓  前端（单一 2.0 视图）
```

### 3.1 定位与失败的可观测契约

**定位四面计数**（`scripts/check_anchor_identity.py` 产出、`anchor-identity-dfm.json` 即样本）：
`range_hits`（候选范围内命中）/ `fallback_hits`（范围内没有、退到全书最早）/
`absent`（全书都没有）/ `unresolved`（命中却定位不到窗口，要求恒为 0）。
候选范围来自采集侧的**物理文件序号** `first_seen_chapter` 与 `analysis.block_size`
（区间 `[N, N+block_size-1]`），**不是**账本里记的文学回号——两套编号不对齐，
按回号定位实测只有 22%。

**失败必须显形**：判定调用返回 `success=False`（4xx／超时／网关拒绝）时，调用方
必须抛出而非取空串，让 `failed_batches` 如实计数；一轮里 `calls > 0` 而
`checked == 0` 判为事故——状态记 `PARTIAL`、原因写明、首个原始回包留在明细的
`raw_sample`，测量脚本同步非 0 退出。

推进顺序建议（每步都能独立验收）：

1. **统一正文身份与切章口径**（B 表第一行）——这是所有后续工作的前提，实测已证明口径不一致会让 78% 的候选定位失败。
2. **接线而非重写**：`run_analysis` / `PromptLimits` / `breaker` 接进 1.x 的 pipeline，产出物仍用 1.x 的形状。
3. **知识库换语义**：排他快照 + 有序归约 + 来源溯源 + 共现分表，逐项替换 `memory_state.py`。
4. **总结链路对齐**：checkpoint 覆盖全阶段、卷摘要不再纯字符截断、子产物原子写。
5. **判定门禁与定标**：先补人工标注集（≥30 条判定），再决定门禁阈值。
6. **前端收敛**：合并 VnextPage/VnextDetailPage 与现有页面，字段名以 2.0 契约为准。

## 4. 现在能立刻做的（不依赖 2.0 立项）

- 0 成本章号闭包门禁（实测 5.3% 命中，人工确认 11/11 是过判）；
- 报告四栏接进报告与前端（数据已经在 `recheck_verdicts.json` 里）。
