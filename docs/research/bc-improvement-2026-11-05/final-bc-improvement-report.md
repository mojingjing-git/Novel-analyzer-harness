# B 类遗漏定位与修复 + C 类按需审核 阶段性核验与离线校准总结报告（2026-11-05 第二轮独立核验整改版）

本报告对应 [BC改进任务书.md](file:///F:/AI/01_项目/小说分析器/docs/BC改进任务书.md) 的阶段性汇总交付物，并已严格按照 [independent-review-followup.md](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/independent-review-followup.md) 的整改顺序，在**零新增付费模型调用（100% 复用已有缓存离线重算）**的前提下，完成了 **F1～F6 全部剩余问题**的工程修复、评分器校准、标签解耦、生产接线补齐与核心契约回归测试恢复。

> **核心验收定性（诚实报告原则）**：
> 1. **传送带（工程持久化、证据门禁与全链路生产接线）已修通并通过 93 项后端 + 33 项前端回归测试**。
> 2. **尺子（B 类严格 1-to-1 问题等价评分器、C 类独立绑定标签与四分类归因）已完成校准**。
> 3. **还不建议开启默认优化或宣布结案**：用校准后的严格尺子重算既有数据表明，**B4 完整账本保留能稳定提升伏笔登记召回率（开发集 `+40.62%`、已曝光留出集 `+27.08%`）**，但 **B2/B3 严格回收闭环率尚未改善（开发集 `8.33% -> 8.33%`、已曝光留出集 `6.25% -> 9.38%`）**，且复检 Token 成本显著上升（`+110.69%` / `+163.23%`）；C 类 20 个模型自提名正例中有 9 个本身答案含硬伤（连 `current` 也拒绝/存疑），不宜直接放宽 `claims_v2` 审核。因此，4 个特性开关在 [`AnalysisConfig`](file:///F:/AI/01_项目/小说分析器/backend/config/settings.py#L140-L152) 中**全部保持默认关闭（`False`）**，待完成候选筛除与未曝光新小说换货试产后再评估默认开启。

---

## 一、 第二轮独立核验（F1～F6）逐项整改对照表

| 编号与级别 | 第二轮独立核验指出的具体问题 | 整改措施、代码位置与验证证据 | 当前状态 |
|---|---|---|---|
| **F1 [P1]** | **归档在一次新增多条时仍丢旧数据**：201 条裁剪保存并读回后，一次追加 2 条（`len(cur_list)=202 > len(old_list)=201`），旧第 1 条记录丢失（仅剩 202 条而非 203 条） | 修改 [`KnowledgeBaseManager._trim_kb`](file:///F:/AI/01_项目/小说分析器/backend/core/knowledge_base.py#L318-L330) 与 [`KnowledgeBase.get_full_field`](file:///F:/AI/01_项目/小说分析器/backend/models/knowledge.py#L47-L54)：只要 `preserve_archive=True` 且存在历史归档或发生裁剪，始终按确定性去重顺序合并 `old_list` 与 `cur_list`；新增多轮多条（+2 条 $\to$ 203 条、+3 条 $\to$ 206 条）持久化往返测试 [`test_b4_trim_kb_preserves_archive`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_t3_b_fixes.py#L131-L185)。 | **已彻底修复并通过回归测试** |
| **F2 [P1]** | **B 类召回匹配过宽**：同人物+同章（`ent_hits >= 1 and ch_diff == 0`）无问题重合即可算命中；未做 1-to-1 排他匹配；`resolution` 未校验回收章引文与新增判准词，且将 `DIFFERENT_ANSWER` 算作成功回收 | 重构 [`scripts/bc_eval_b_zero_leakage.py`](file:///F:/AI/01_项目/小说分析器/scripts/bc_eval_b_zero_leakage.py)：<br>1. [`score_observation_item_pair`](file:///F:/AI/01_项目/小说分析器/scripts/bc_eval_b_zero_leakage.py#L77-L148) 强制要求锚点原句包含或问题核心词命中，仅同章同实体一律归入 `ambiguous` 诊断计数，不计入召回；<br>2. [`match_observations_one_to_one`](file:///F:/AI/01_项目/小说分析器/scripts/bc_eval_b_zero_leakage.py#L166-L202) 实行全局 1-to-1 排他匹配；<br>3. [`verify_resolution_evidence`](file:///F:/AI/01_项目/小说分析器/scripts/bc_eval_b_zero_leakage.py#L205-L246) 排除 `DIFFERENT_ANSWER`，强制校验回收引文位于参考回收子章邻域且命中回收新增判准词；<br>4. 离线重算生成 [t3-b-fix-report.json](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/t3-b-fix-report.json) 与 [m4-holdout-report.json](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/m4-holdout-report.json)（逐条含 `observation_audit`），并新增反例回归测试 [`test_f2_calibrated_b_matcher_rejects_same_entity_same_chapter_different_question`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_t3_b_fixes.py#L499-L579)。 | **已校准并完成离线重算** |
| **F3 [P1]** | **C 类新增 20 个正样本标签来自被测模型自身，且 `legacy55` 未复核标签被直接覆写为 `bound`** | 重构 [`scripts/bc_eval_t4_c_on_demand.py`](file:///F:/AI/01_项目/小说分析器/scripts/bc_eval_t4_c_on_demand.py)：<br>1. 停止自动覆写 `label_sample_id = sample_id`；严格对照 [`pilot12.json`](file:///F:/AI/01_项目/小说分析器/docs/research/bc-evaluation-2026-11-05/pilot12.json) 区分 `bound`（5 条）、`stale`（4 条）与 `unbound`（46 条）；<br>2. 跨作品 20 条模型自提名正例中，仅将 2 条经盲标回收集独立验证的正例设为 `bound`，其余 18 条设为 `unbound (model_self_nominated_unverified)`；<br>3. 独立报告 17 条严格绑定子集（`strict_bound_subset_17`）与 51 条名义正例的四分类归因拆解（`positive_rejection_attribution_51`）。 | **已校准并完成离线重算** |
| **F4 [P1]** | **后端测试从 2016 个缩减至 76 个，缺少证据隔离与门禁契约回归测试及精简映射表** | 1. 在 [`backend/tests/test_recheck.py`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_recheck.py#L229-L361) 中恢复跨候选/跨章节证据隔离回归测试（拦截 `CROSS_CANDIDATE_QUOTE` 与无关章节污染）；<br>2. 新建 [`backend/tests/test_bc_contracts.py`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_bc_contracts.py) 恢复 `acceptance_gate`、`claim_evidence`、`claim_evidence_v2`、`closure`、`evaluation_corpus`、`bc_corpus` 与 `bc_compare_claims` 核心契约测试；<br>3. 后端测试增至 **93 个用例**（+ 前端 33 个用例）全部通过，并在本报告第四节附上完整测试精简映射表。 | **已恢复并通过全套测试** |
| **F5 [P2]** | **生产接线未全部完成**：`enable_b_trace`、`enable_c_on_demand_audit` 与 `pipeline.py` 的 `fix_b4_flow_and_storage` 未接入真实主流程 | 1. [`backend/core/pipeline.py`](file:///F:/AI/01_项目/小说分析器/backend/core/pipeline.py#L106-L108) 将 `fix_b4_flow_and_storage` 传入 `KnowledgeBaseManager(preserve_archive=...)`；<br>2. [`backend/services/final_summary.py`](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L225-L315) 接入 `enable_b_trace`（落盘 `b_trace_audit.json`）与 `enable_c_on_demand_audit`；<br>3. [`backend/services/recheck_v2.py`](file:///F:/AI/01_项目/小说分析器/backend/services/recheck_v2.py#L510-L605) 接入 `enable_c_on_demand_audit`（调用 `select_audit_route` 并在 `HARD_REJECT_MECHANICAL` 时机械拒止、记录 `audit_routes`），通过回归测试 [`test_f5_recheck_v2_wires_enable_c_on_demand_audit`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_t3_b_fixes.py#L416-L496)。 | **已完成端到端接线并验证** |
| **F6 [P2]** | **C 类缓存缺少样本指纹与提示词哈希，离线重放被计入 `live_call_stats`** | 在 `t4_live_paired_cache_v2.json` 与 [`t4-c-on-demand-report.json`](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/t4-c-on-demand-report.json) 每条记录中补齐 `sample_id`、`label_sample_id`、`label_status`、`label_provenance`、`prompt_sha256`、`model`、`contract_version`；将 `execution_telemetry` 拆分为 `live_network_calls: 0`、`cached_reused_calls: 170`、`offline_selector_replay_items: 85`。 | **已彻底整改** |

---

## 二、 离线校准后的核心评测结论（尺子校准后的真实进展与短板）

### 1. B 类伏笔召回：B4 账本保留有效（登记召回显著提升），但 B2/B3 严格回收闭环仍未突破

| 评测集与实验定位 | 严格 1-to-1 指标 | Baseline | Repaired | 净变化 ($\Delta$) | 校准后真实结论 |
|---|---|---:|---:|---:|---|
| **开发集（4 部作品，56 条观测：32 登记 + 24 回收［10 跨子章 + 14 同子章］）** | Layer 1A：Prompt-View 严格登记召回（20 条上限） | 25.00% (8/32) | 25.00% (8/32) | **0.00%** | 剔除同人物不同问题的宽松匹配后，20 条截断视图下两边严格命中持平 |
| **开发集** | **Layer 1B：Ledger 账本总表严格登记召回** | 25.00% (8/32) | **65.62% (21/32)** | **+40.62%** | **B4 修复确实有效**：完整账本持久化消除了截断丢失（`B4_FLOW_LOST` 从 24 降至 7） |
| **开发集** | Layer 1B 附加：回收观测的铺垫伏笔入账率 | 45.83% (11/24) | **66.67% (16/24)** | **+20.84%** | 更多待回收伏笔得以保留在账本中 |
| **开发集** | Layer 2 严格：经回收章+判准验证的回收召回 (`resolution_recall`) | 8.33% (2/24) | **8.33% (2/24)** | **0.00%** | **B2/B3 回收闭环尚未改善**（跨子章为 `2/10 = 20.00%`；复检触发数虽从 4 升至 8，但多出的 4 条因 `DIFFERENT_ANSWER` 或未命中参考回收章被严格规则剔除） |
| **开发集** | 同层综合召回率（Ledger 登记 + 严格回收） | 17.86% (10/56) | **41.07% (23/56)** | **+23.21%** | 收益全部来自 B4 账本登记保留，同时复检调用增加 `+62.50%`、Token 增加 `+110.69%` |
| **已曝光留出集重测（4 部作品，80 条观测：48 登记 + 32 回收［27 跨子章 + 5 同子章］）** | Layer 1A：Prompt-View 严格登记召回（20 条上限） | 10.42% (5/48) | **16.67% (8/48)** | **+6.25%** | 小幅改善 |
| **已曝光留出集重测** | **Layer 1B：Ledger 账本总表严格登记召回** | 10.42% (5/48) | **37.50% (18/48)** | **+27.08%** | **B4 修复稳定复现**：已提取的 21 条严格匹配伏笔中保留了 18 条（`B4_FLOW_LOST` 从 27 降至 4） |
| **已曝光留出集重测** | Layer 2 严格：经回收章+判准验证的回收召回 (`resolution_recall`) | 6.25% (2/32) | **9.38% (3/32)** | **+3.13%** | 跨子章严格回收从 `2/27 (7.41%)` 微升至 `3/27 (11.11%)`，尚未达到 `+15%` 验收目标 |
| **已曝光留出集重测** | 同层综合召回率（Ledger 登记 + 严格回收） | 8.75% (7/80) | **26.25% (21/80)** | **+17.50%** | 复检调用增加 `+92.86%`、Token 增加 `+163.23%`（成本超预算） |

### 2. C 类按需触发审核：安全性与调用缩减达标，但模型自标正例揭示上游候选答案质量问题

1. **严格独立绑定子集（`strict_bound_subset_17`：17 条自然绑定样本）与构造探针集（`16` 条）**：
   - 在 17 条严格绑定自然样本（5 条 `pilot12` + 2 条盲标验证正例 + 10 条核实负例）上，`on_demand_claims_v2` 将负例误接受从 `current` 的 `1/13 (7.69%)` 降至 **`0/13 (0.00%)`**，未触发样本误接受为 **`0`**。
   - 在 16 条独立构造探针（`constructed_regression_set_16`）上，`on_demand_claims_v2` 触发 11/16 次（调用缩减 **`-31.25%`**），负例误接受 **`0/8`**，正例接受 **`7/8 (87.50%)`**。
2. **51 条名义“支持”样本的四分类归因（为何绝不能直接放宽 `claims_v2`）**：
   - `accepted_by_v2`（通过审核）：**9 条**（`legacy55` 8 条 + 跨作品 1 条）
   - `candidate_answer_flawed_or_closing_unmet_in_both`（**候选答案本身有缺陷，连 `current` 基线也拒绝/存疑**）：**13 条**（`legacy55` 4 条 + **跨作品模型自提名 9 条**）。这直接证明：**被测模型自己判为“支持”的 20 个跨作品候选中，有 45%（9/20）连宽松基线都无法通过**，将其当作真值正例会严重夸大 `claims_v2` 的误杀率！
   - `closing_criteria_unmet_under_v2_prompt`（NLI 主判定认为未满足关闭条件或忠实性，先于原子拆解失败）：**9 条**（`legacy55` 4 条 + 跨作品 5 条）
   - `protocol_or_call_failure`（`unknown_answer_frame` 协议格式错误）：**1 条**（跨作品 1 条）
   - `claim_unit_granularity_or_overstatement_blocked`（`current` 接受，被 `claims_v2` 原子单元粒度/模态/归属拦截）：**19 条**（`legacy55` 15 条 + 跨作品 4 条）。
3. **Token 成本全口径对比（85 条自然候选）**：
   - `current`：`267,724 tokens`
   - `full_claims_v2`：`403,693 tokens`（较 `current` `+50.79%`）
   - `on_demand_claims_v2`：`366,002 tokens`（调用次数缩减 **`-32.94%`**，Token 较 `full_claims_v2` 节省 **`-9.34%`**，但较 `current` 仍增加 **`+36.71%`**）。

---

## 三、 后续迈向“换一本小说也稳定有效”的三步行动路线

对照“小说质检流水线”的可靠性标准：
1. **第一步（修传送带）—— 已完成**：
   - `KnowledgeBase._archive` 多轮多条增量裁剪与保存读回已做到零丢失（201 $\to$ 203 $\to$ 206 条验证通过）；
   - `run_recheck_v2` 段内引文对齐绝不伪造 `counter_search.performed=True`；
   - 4 个特性开关已全部接入 `pipeline.py`、`final_summary.py` 与 `recheck_v2.py` 真实生产路径。
2. **第二步（校准尺子）—— 已完成**：
   - B 类评分器已改为 1-to-1 严格问题等价匹配 + 回收引文章节与判准词双重校验；
   - C 类评测已剥离未复核历史标签与模型自提名伪正例，并完成 51 条名义正例的四分类归因。
3. **第三步（定向攻克回收闭环与候选限流后，拿未曝光新小说试产）—— 下一步重点**：
   - **攻克 B2/B3 回收闭环与复检限流**：在进入 `run_recheck_v2` 前，仅对“具备明确跨章实体/母题共现候选证据段”的未解伏笔发起 LLM 复检（过滤掉无后文证据支撑的纯铺垫条目），从而在不增加复检调用次数（控制在 `+25%` 内）的前提下提升严格回收召回率；同时支持同一子章内“前铺垫、后揭示”线索的章内闭环识别。
   - **约束上游复检回答生成精简度**：在 `recheck_v2` 生成 `answer` 时禁止添加原文未直说的总结性副句与推测模态词，从源头减少 `claim_unit_granularity_or_overstatement_blocked`（19 条），而不是盲目放宽 `claims_v2` 审核标准。
   - **全新未曝光作品换货试产**：上述两项改进完成后，选取从未参与调试的新小说通过真实生产管线（`NovelAnalysisPipeline` + `FinalSummaryRunner`）做一次性盲测验收，达标后再将开关切换为默认开启。

---

## 四、 回归测试覆盖恢复与测试精简映射表（F4）

针对此前测试套件精简过程中缺失的 B/C 专项契约测试，本次已恢复并补齐至 **后端 93 个用例 + 前端 33 个用例（共 126 个用例，100% 通过）**：

| 原测试文件 / 模块领域 | 当前承载测试文件 | 覆盖的核心契约与回归场景 | 用例数 |
|---|---|---|---:|
| `test_recheck_v2.py` + `test_recheck_evidence_isolation.py` | [`backend/tests/test_recheck.py`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_recheck.py) | 证据包构建、反证搜索门禁、跨候选引文串用拦截（`CROSS_CANDIDATE_QUOTE`）、无关章节证据隔离、终端缓存与重试语义 | 20 |
| `test_acceptance_gate.py` + `test_claim_evidence.py` + `test_claim_evidence_v2.py` + `test_closure.py` + `test_evaluation_corpus.py` + `test_bc_corpus.py` + `test_bc_compare_claims.py` | [`backend/tests/test_bc_contracts.py`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_bc_contracts.py) | 机械增量检查、`AND`/`OR` 预注册分支闭合、`claim_evidence` v1/v2 原子主张分区与模态/归属/引文门禁、`sample_fingerprint` 与 `label_status`（`bound`/`stale`/`unbound`）防自动认证、语料防泄漏校验、配对比较统计 | 16 |
| T3/F1/F2/F5 专项修复回归测试 | [`backend/tests/test_t3_b_fixes.py`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_t3_b_fixes.py) | B4 归档多轮多条（201 $\to$ 203 $\to$ 206）持久化、母题分桶去重保留锚点三问、段内对齐不伪造 `counter_search`、`QUOTE_EQUALS_ANCHOR` 拦截、F2 严格评分器拒绝同人物不同问题、F5 `enable_c_on_demand_audit` 在 `run_recheck_v2` 中的端到端路由接线 | 11 |
| T1 B 类追踪与归因测试 | [`backend/tests/test_b_trace.py`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_b_trace.py) | B1～B4 阶段追踪事件发射、`event_reason_coverage_rate` 与 `reference_attribution_rate` 分离 | 4 |
| T4 C 类按需审核选择器测试 | [`backend/tests/test_c_audit_selector.py`](file:///F:/AI/01_项目/小说分析器/backend/tests/test_c_audit_selector.py) | 单子句免触发、多子句/逻辑连接词/机械硬缺陷/软信号触发路由 | 6 |
| 核心管线、知识库、API、LLM 客户端与解析器测试 | `test_api.py`, `test_core.py`, `test_foreshadowing.py`, `test_knowledge_and_consolidation.py`, `test_llm_client.py`, `test_parser_and_export.py` | 章节切分、LLM 重试与流式降级、知识库合并与持久化、阶段一/二汇总、FastAPI 路由与导出 | 36 |
| 前端组件与状态管理测试 | `frontend/src/__tests__/*` | 仪表盘、伏笔卡片、阅读器、状态管理与 API 交互 | 33 |
| **合计** | **后端 11 个测试文件 + 前端 4 个测试文件** | **核心生产契约与 B/C 门禁隔离 100% 覆盖** | **126** |
