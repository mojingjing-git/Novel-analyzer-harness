# M2 / T3 B 类主因定向修复与零泄漏校准验证报告（2026-11-05 第二轮核验整改版）

本文件对应 [BC改进任务书.md](file:///F:/AI/01_项目/小说分析器/docs/BC改进任务书.md) 中的 **T3｜修复最高频两类 B 根因（M2，P0）** 交付物，并已完成针对 [independent-review.md](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/independent-review.md) 与 [independent-review-followup.md](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/independent-review-followup.md)（F1、F2、F4、F5）的离线校准与工程整改。

- **零泄漏校准后成对评测结果（主证据，0 新增在线调用）**：[t3-b-fix-report.json](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/t3-b-fix-report.json)（由 [scripts/bc_eval_b_zero_leakage.py](file:///F:/AI/01_项目/小说分析器/scripts/bc_eval_b_zero_leakage.py) 基于已缓存结果离线重算生成）
- **原构造组件压力测试（归档备查，不作为端到端召回率依据）**：[t3-synthetic-stress-test.json](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/t3-synthetic-stress-test.json)
- **单元测试与最小复现回归测试**：[backend/tests/test_t3_b_fixes.py](file:///F:/AI/01_项目/小说分析器/backend/tests/test_t3_b_fixes.py)、[backend/tests/test_b_trace.py](file:///F:/AI/01_项目/小说分析器/backend/tests/test_b_trace.py)

---

## 1. 工程正确性修复与第二轮核验整改（F1、F2、F5）

1. **修复 `KnowledgeBase._archive` 多条增量裁剪丢旧归档缺陷（F1）**：
   - **问题根因**：原 [`KnowledgeBaseManager._trim_kb`](file:///F:/AI/01_项目/小说分析器/backend/core/knowledge_base.py#L318-L330) 在 `preserve_archive=True` 分支下仅当 `len(cur_list) > limit` 时才合并历史 `old_list`，且当单轮新增 $\ge 2$ 条记录（例如从 200 条一次增至 202 条，而 `old_list` 为 201 条）时，`len(cur_list) > len(old_list)` 导致跳过历史归档合并，丢失最早被裁掉的第 1 条旧记录。同时 [`KnowledgeBase.get_full_field`](file:///F:/AI/01_项目/小说分析器/backend/models/knowledge.py#L47-L54) 仅在 `len(archived) > len(current)` 时返回归档。
   - **修复方案**：在 [`KnowledgeBaseManager._trim_kb`](file:///F:/AI/01_项目/小说分析器/backend/core/knowledge_base.py#L318-L330) 中，只要 `preserve_archive=True` 且存在历史归档或触发裁剪，始终按确定性去重顺序合并 `old_list` 与 `cur_list` 写入 `_archive[field_name]`；在 [`KnowledgeBase.get_full_field`](file:///F:/AI/01_项目/小说分析器/backend/models/knowledge.py#L47-L54) 中对 `_archive[field_name]` 与当前视图列表做保序去重合并。
   - **回归测试**：[`test_b4_trim_kb_preserves_archive`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_t3_b_fixes.py#L131-L185) 覆盖 201 条裁剪 $\to$ 保存读回 $\to$ 一次新增 2 条（保留 203 条）$\to$ 再一次新增 3 条（保留 206 条）的多轮增量往返。

2. **校准 B 类召回评分器（F2）**：
   - **废止同章同实体宽松命中**：在 [`score_observation_item_pair`](file:///F:/AI/01_项目/小说分析器/scripts/bc_eval_b_zero_leakage.py#L77-L148) 中，仅共享同章同人物而无锚点引文/悬念问题核心词对齐的配对，一律标记为 `ambiguous`（进入独立诊断计数 `ambiguous_registration_flagged` / `ambiguous_resolution_flagged`），**绝不计入召回命中**。
   - **全局一对一（1-to-1）排他匹配**：在 [`match_observations_one_to_one`](file:///F:/AI/01_项目/小说分析器/scripts/bc_eval_b_zero_leakage.py#L166-L202) 中，每个系统候选条目最多只能匹配一条参考登记观测和一条参考回收观测，消除一对多重复计分。
   - **严格区分登记召回与回收验证（排除 `DIFFERENT_ANSWER`）**：在 [`verify_resolution_evidence`](file:///F:/AI/01_项目/小说分析器/scripts/bc_eval_b_zero_leakage.py#L205-L246) 中，回收观测必须满足：（1）`verdict == "SUPPORTED"`（排除 `DIFFERENT_ANSWER`）；（2）验证引文定位在参考回收子章邻域（$|ev\_ch - ref\_res\_ch| \le 1$）；（3）回答或引文命中回收判准新增信息词（非单纯重复铺垫章词面），才计入 `resolution_recalled`。同时逐条输出 `observation_audit` 供人工核查。
   - **回归测试**：[`test_f2_calibrated_b_matcher_rejects_same_entity_same_chapter_different_question`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_t3_b_fixes.py#L499-L579) 验证“张三的铁匣里是什么”绝不会命中“张三能否练成剑法”，且 1-to-1 排他匹配与 `DIFFERENT_ANSWER` 拦截生效。

3. **补齐生产配置开关接线（F5）**：
   - `fix_b4_flow_and_storage` 已接入 [`NovelAnalysisPipeline`](file:///F:/AI/01_项目/小说分析器/backend/core/pipeline.py#L106-L108)（传入 `preserve_archive`）与 [`FinalSummaryRunner`](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L244-L280)；
   - `enable_b_trace` 已接入 [`FinalSummaryRunner.run`](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L225-L315)，开启时自动收集全阶段追踪并落盘 `b_trace_audit.json`；
   - `enable_c_on_demand_audit` 已接入 [`run_recheck_v2`](file:///F:/AI/01_项目/小说分析器/backend/services/recheck_v2.py#L510-L605) 与 [`FinalSummaryRunner._run_recheck_v2`](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L1215-L1225)。所有开关默认保持 `False`。

---

## 2. 开发集（4 部作品，56 条参考观测）校准后实测结果（0 新增在线调用）

> **实验边界说明**：本评测为基于每部作品 4 个 15,000 字盲读文本包（切分为 28～29 个自然子章，按比例设 `14 高 + 6 中` 提示词预算上限）的自然文本成对评测。56 条参考观测由 32 条登记观测（`registration`）和 24 条回收观测（`resolution`：其中 10 条为跨自然子章回收，14 条在同一 2,000 字自然子章内完成铺垫与揭示）组成。

### 2.1 校准后质量指标对比（同一产物层、1-to-1 严格问题等价口径）

| 评测层级与指标 | Baseline（基线） | Repaired（修复后） | 净变化 ($\Delta$) | 校准后诊断说明 |
|---|---:|---:|---:|---|
| **Layer 0：章节原始提取严格登记匹配率** (`raw_extracted_registration`) | 26 / 32 (81.25%) | 26 / 32 (81.25%) | 0.00% | 6 条未严格匹配问题核心词（计入 `B1_UNREGISTERED`） |
| **Layer 1A：Prompt-View 严格登记召回率** (`prompt_view_registration`, 20 条上限) | 8 / 32 (25.00%) | 8 / 32 (25.00%) | **0.00%** | 20 条截断预算下，两边严格命中的特定参考伏笔均为 8 条（另有 6 条同章同人物不同问题被标为 `ambiguous` 不计入命中） |
| **Layer 1B：Ledger 账本总表严格登记召回率** (`ledger_registration`) | 8 / 32 (25.00%) | **21 / 32 (65.62%)** | **+40.62%** | **B4 修复真实有效**：完整总表入账消除了提示词 20 条切尾，使严格匹配的账本登记从 8 条升至 21 条（`B4_FLOW_LOST` 从 24 降至 7） |
| **Layer 1B 附加：回收观测的铺垫伏笔已入账数** (`resolution_setup_registered_in_ledger`) | 11 / 24 (45.83%) | **16 / 24 (66.67%)** | **+20.84%** | 更多待回收伏笔得以保留在账本中进入复检视野 |
| **Layer 2 诊断：复检触发 `SUPPORTED`/`DIFFERENT_ANSWER` 数（未验回收章）** | 4 / 24 (16.67%) | **8 / 24 (33.33%)** | **+16.66%** | 模型复检给出了更多闭合提名，但其中部分为 `DIFFERENT_ANSWER` 或未命中特定参考回收章 |
| **Layer 2 严格：经回收章+判准验证的回收召回率** (`resolution_recall`) | 2 / 24 (8.33%) | **2 / 24 (8.33%)** | **0.00%** | **严格回收闭环尚未显著改善**（跨子章回收为 `2/10 = 20.00%`；14 条同子章内即时揭示线索未被跨章复检机制捕获） |
| **同层综合召回率（Ledger 登记 + 严格验证回收）** | 10 / 56 (17.86%) | **23 / 56 (41.07%)** | **+23.21%** | 提升完全来自 **B4 账本完整保留（+13 条登记）**，而非 B2/B3 回收判准提升 |
| **C 类无引文/锚点同句误接受数** (`unsupported_accepts`) | 0 | **0** | **0** | 零误接受（`QUOTE_EQUALS_ANCHOR` 拦截生效） |

### 2.2 各作品校准后分项明细

| 作品 ID | 作品名 | Prompt-View 严格登记 (Base $\to$ Fix) | Ledger 严格登记 (Base $\to$ Fix) | 严格验证回收 (Base $\to$ Fix) | Ledger 层综合召回 (Base $\to$ Fix) | 复检调用数 (Base $\to$ Fix) | 复检 Token (Base $\to$ Fix) |
|---|---|---:|---:|---:|---:|---:|---:|
| `ind_dev` | 《学霸的黑科技系统》 | 3/8 $\to$ 3/8 | 3/8 $\to$ **6/8** | 1/6 $\to$ 1/6 | 28.57% $\to$ **50.00%** (`+21.43%`) | 3 $\to$ 7 | 38,874 $\to$ 117,111 |
| `game_dev` | 《亏成首富从游戏开始》 | 2/8 $\to$ 3/8 | 2/8 $\to$ **5/8** | 0/6 $\to$ **1/6** | 14.29% $\to$ **42.86%** (`+28.57%`) | 5 $\to$ 4 | 62,155 $\to$ 68,690 |
| `urban_dev` | 《大医凌然》 | 2/8 $\to$ 2/8 | 2/8 $\to$ **6/8** | 0/6 $\to$ 0/6 | 14.29% $\to$ **42.86%** (`+28.57%`) | 4 $\to$ 7 | 52,078 $\to$ 123,803 |
| `hist_dev` | 《秦吏》 | 1/8 $\to$ 0/8 | 1/8 $\to$ **4/8** | 1/6 $\to$ 0/6 | 14.29% $\to$ **28.57%** (`+14.28%`) | 4 $\to$ 8 | 60,036 $\to$ 139,475 |

---

## 3. 成本与工程结论（为何当前不开启默认优化）

1. **尺子校准后的真实画面**：
   - **已证实有效的部分（B4 传送带不丢线索）**：把 `ledger_full_catalog` 与 `prompt_view_catalog` 解耦后，账本严格登记召回率从 `25.00% (8/32)` 提升至 `65.62% (21/32)`（`+40.62%`），`B4_FLOW_LOST` 从 24 条降至 7 条。
   - **尚未解决的瓶颈（B2/B3 回收闭环）**：在剔除同人物不同问题的宽松匹配、排除 `DIFFERENT_ANSWER` 并校验回收章后，严格验证的回收召回率停留在 `8.33% (2/24)`（跨子章 `20.00%`）。原因在于：（a）24 条参考回收中有 14 条发生在同一自然子章内部，单遍章节提取将其作为未解伏笔登记但未在同章或后续章触发二次回收提名；（b）全量账本入包使进入复检的候选翻倍，但缺少针对性的跨章证据召回与候选筛除，导致复检调用数增加 `+62.50%`、Token 增加 `+110.69%`，却未转化为严格验证的回收增量。
2. **处置决定**：所有 4 个开关在 [`AnalysisConfig`](file:///F:/AI/01_项目/小说分析器/backend/config/settings.py#L140-L152) 中保持默认关闭（`False`），不作为已完成默认上线的优化结案。
