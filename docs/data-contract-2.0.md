# 2.0 管线：形态与缺口清单

> 本文描述**当前状态**与 2.0 的目标形态，不写变更流水账。
> 背景数据：`docs/baseline-data.md`（实测基线）、`docs/issues.md`（仍成立的问题）。

## 1. 为什么要合并

仓库里现在并存两条管线：

| | 1.x 生产管线 | vnext |
|---|---|---|
| 代码 | `backend/core/pipeline.py`(1268) + `backend/services/final_summary.py`(1970) + `backend/utils/memory_state.py` + `backend/utils/foreshadow_ledger.py` | `backend/vnext/`（26 个模块 / 5734 行）+ `backend/services/vnext_service.py` + `backend/api/routes_vnext.py` |
| 强项 | **作业层完整**：分块分析 → 滚动摘要 → 伏笔账本 → 四阶段总结 → 报告；前端 78 文件 / 12631 行 | **数据层语义强**：有序覆盖、排他快照、来源溯源、证据门禁、预算与缓存、熔断、原子写 |
| 弱项 | 数据层语义弱（见下） | **没有作业层**：`run_analysis` 0 处生产调用；`breaker`/`cutoff_view`/`build_coverage`/`PromptLimits` 全部 0 调用 |

所以现状不是"新管线更好"，而是**旧管线跑得起来但语义不可靠，新管线语义可靠但跑不起来**。
2.0 = 用 1.x 的作业层做骨架，把 vnext 的语义接进去，然后**只保留一条**。

## 2. 缺口清单

### A. 接线缺口：vnext 的机制接进 1.x 的作业层

| 缺口 | 现状 | 影响 |
|---|---|---|
| `run_analysis` 没有生产入口 | 0 处非测试调用 | vnext 整条分析链在生产上跑不起来 |
| `PromptLimits` 没接配置 | 0 处非测试调用 | 1.x 的 `analyzer.py:73-81` 只传了 7 个上限，`max_verified_facts`/`max_relationships`/`max_themes`/`max_content_chars` 无法配置 |
| 账户级熔断没有调用方 | `backend/vnext/breaker.py` 0 处非测试调用 | 欠费/账户不可用时 1.x 会继续派发、继续烧钱（1.x 只有 `pipeline.py:755` 登记致命块供补跑剔除，不熔断） |
| 排他快照没进知识库写入 | `cutoff_view` 0 处非测试调用 | 1.x 的滚动摘要与人物状态只有一个 `last_updated_chapter` 标量 → 越早补跑的块既进不了摘要，又会被后面高章号的结果覆盖 |
| 有序覆盖没进 1.x | `build_coverage` 0 处非测试调用 | 覆盖表按"点数与文件集合不得变化"的旧口径冻结，抽公共调用函数就会失真 |
| 子产物原子写 | `backend/utils/aggregate_utils.py` 约 10 处 `open(...,'w')+json.dump` | 写到一半中断会留下半个 JSON |

### B. 语义冲突：两边都有同一件事，口径不同，合并时必须选一个

| 冲突点 | 1.x 的做法 | vnext 的做法 | 建议 |
|---|---|---|---|
| **章切分口径** | 块 = 章（`block_size` 合并），章号 = 块序号 | 按块内标题反查回号（`split_block_into_chapters` / `build_chapter_spans`） | 取 vnext，但**必须解决回号重复**：源文件里行首的"第N回"会被标题正则当成边界，实测《大奉打更人》合并块 spans 形如 `[229, 230, 1, 2]`——同一章号在书里反复出现。2.0 的正文身份应是 `(章号, 引文)` 而不是章号 |
| 快照与覆盖 | 标量上界（`last_updated_chapter`） | 排他快照 `exclusive_before` + 有序归约 | 取 vnext |
| 伏笔终态 | dormant 判死（`final_summary.py:1488` 没有 active 就直接返回） | `cutoff_view(cutoff_ordinal=)` | 取 vnext；且复检要用**带证据**那条（`recheck_v2`），不是旧的无阻断路径 |
| 共现与关系 | 同一章出现就判定两人有关系（`memory_state.py:388-394`） | 共现与语义关系分表 | 取 vnext |
| 已知事实来源 | `verified_facts` 原样入账，无法溯源 | `SourceRef` 溯源 | 取 vnext |
| 错误分类与重试 | 分散在 `llm_client` 四处判定 | `ErrorClass` 11 类 + `ErrorScope` 4 值 + 单点重试决策 | 取 vnext，并接到 1.x 的派发链 |

### C. 两侧都没有的功能

1. **报告四栏**（未回收 / 自动已回收 / 证据不足 / 待确认）：改造已算出但没接进报告与前端。
2. **判定质量门禁**：0 成本的"答案引用证据窗口外章号"（实测命中 11/208，人工确认 11/11 都是答非所问）；
   以及"同款模型盲答复核"（弱模型复核会误伤，精确率约 80%）。两条都还没实现。
3. **人工标注定标集**：判定质量至今没有可回归的标注基线（现有 `annotations-C2` 是 30 条引文样本，不是判定标注）。
4. **产物 schema 版本化**：输出 JSON 的 schema 仍在演化，跨版本升级后依赖旧字段的脚本会坏。
5. **配置面分离**：1.x 与 vnext 的配置项混在同一个 `config.json` / `AppConfig` 里，没有分区。
6. **`blocks` 目录只放正文**：切分报告（`split_report.txt`）被当成第 922 章参与分析与复检（两轮都如此，是既有行为）。

### D. 前端

- vnext 页面与后端字段名不一致（`docs/issues.md` 已列 3 处接口 + 2 处类型声明）：
  `total_chapters` vs `chapter_count`、`state` vs `block.status`、队列条目缺 `total_chapters`/`block_size`/`updated_at`。
- 前端没有"2.0"视图：现在的 `LaneView` / `QueuePage` / `SummaryPage` 混着两条管线的概念。
- `summary` 自研 Markdown 渲染器 + `v-html`：报告内容里的 HTML 不转义，而报告来自模型输出。

## 3. 2.0 的形态（建议）

```
原文 blocks
   ↓  切章（统一口径，正文身份 = 章号 + 引文）
   ↓  分析（PromptLimits 全量接配置 + 账户级熔断 + 原子写 + 结果登记）
   ↓  知识库（排他快照 + 有序归约 + SourceRef 溯源 + 共现/语义分表）
   ↓  伏笔账本 v4（锚点 + 三问 + 稳定实体键）
   ↓  复检 V2（有界证据包 + 逐条落盘 + 记账三态 + 补问）
   ↓  判定门禁（0 成本章号闭包 + 同款模型盲答复核）
   ↓  总结（卷摘要 → 复检 → 风格 → 报告；checkpoint 覆盖全阶段）
报告四栏（未回收 / 自动已回收 / 证据不足 / 待确认）
   ↓  前端（单一 2.0 视图）
```

推进顺序建议（每步都能独立验收）：

1. **统一正文身份与切章口径**（B 表第一行）——这是所有后续工作的前提，实测已证明口径不一致会让 78% 的候选定位失败。
2. **接线而非重写**：`run_analysis` / `PromptLimits` / `breaker` 接进 1.x 的 pipeline，产出物仍用 1.x 的形状。
3. **知识库换语义**：排他快照 + 有序归约 + 来源溯源 + 共现分表，逐项替换 `memory_state.py`。
4. **总结链路对齐**：checkpoint 覆盖全阶段、卷摘要不再纯字符截断、子产物原子写。
5. **判定门禁与定标**：先补人工标注集（≥30 条判定），再决定门禁阈值。
6. **前端收敛**：合并 VnextPage/VnextDetailPage 与现有页面，字段名以 2.0 契约为准。

## 4. 现在能立刻做的（不依赖 2.0 立项）

- `recheck_v2_enabled` 开关打开（就差这个开关，复检链路的阻塞都已清完）；
- 0 成本章号闭包门禁（实测 5.3% 命中，人工确认 11/11 是过判）；
- 报告四栏接进报告与前端（数据已经在 `recheck_verdicts.json` 里）。
