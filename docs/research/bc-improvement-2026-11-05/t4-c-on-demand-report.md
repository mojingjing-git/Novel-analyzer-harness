# M3 / T4 C 类按需触发审核配对评估与标签独立性校准报告（2026-11-05 第二轮核验整改版）

本文件对应 [BC改进任务书.md](file:///F:/AI/01_项目/小说分析器/docs/BC改进任务书.md) 中的 **T4｜构建 C 类按需触发审核（M3，P1）** 交付物，并已完成针对 [independent-review-followup.md](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/independent-review-followup.md) 中 **F3（标签独立性与 `label_sample_id` 绑定）** 与 **F6（缓存溯源指纹与离线重算口径）** 的全面离线校准（0 新增在线调用）。

- **机读配对与溯源报告（主证据）**：[t4-c-on-demand-report.json](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/t4-c-on-demand-report.json)
- **按需触发选择器代码**：[backend/common/c_audit_selector.py](file:///F:/AI/01_项目/小说分析器/backend/common/c_audit_selector.py)
- **复检生产接线**：[backend/services/recheck_v2.py](file:///F:/AI/01_项目/小说分析器/backend/services/recheck_v2.py#L510-L605)、[backend/services/final_summary.py](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L1215-L1225)
- **配对评测与离线校准脚本**：[scripts/bc_eval_t4_c_on_demand.py](file:///F:/AI/01_项目/小说分析器/scripts/bc_eval_t4_c_on_demand.py)

---

## 1. 第二轮核验问题整改（F3 标签独立性 & F6 缓存溯源）

1. **停止自动认证历史未复核标签与模型自标正例（F3）**：
   - **修复前缺陷**：原 `scripts/bc_eval_t4_c_on_demand.py` 对 `acceptance_prepared_legacy55.json` 的 55 条样本直接覆写了 `c["label_sample_id"] = c["sample_id"]`，违反了冻结文件中“不得自动认证历史标签”的约束；且跨作品新增的 20 条正例直接按被测模型自身 `verdict in ("SUPPORTED", "DIFFERENT_ANSWER") and rch > ach` 标记为 `"支持"` 并赋予绑定指纹。
   - **校准方案**：
     - 对 `legacy55` 的 55 条样本，严格对照 [pilot12.json](file:///F:/AI/01_项目/小说分析器/docs/research/bc-evaluation-2026-11-05/pilot12.json) 保留真实 `label_sample_id`：其中仅 **5 条**（`fs_002`、`fs_042`、`fs_303`、`fs_384`、`fs_1000`）指纹完全一致（`label_status = "bound"`），**4 条**因 `pilot12` 绑定时带 `prereg` 而判为 `"stale"`，其余 **46 条**保持 `label_sample_id = null`（`label_status = "unbound"`）。
     - 对跨作品 30 条自然复检候选，仅将 **2 条经独立盲标参考回收集严格验证的正例**（`zl_ind_dev_repaired_fs_018` 对应 `dev_obs_006`、`zl_game_dev_repaired_fs_007` 对应 `dev_obs_016`）与 **10 条经同章回声/首分句无依据核实的负例**绑定 `label_sample_id = sample_id`（`label_status = "bound"`）；其余 **18 条模型自提名候选**保持 `label_sample_id = null`（`label_status = "unbound"`, `label_provenance = "model_self_nominated_unverified"`）。
     - 由此，85 条自然候选严格拆分为：**17 条独立绑定子集（`strict_bound_subset_17`）** 与 **68 条未绑定/待复核历史及模型提名诊断池**。

2. **补齐缓存溯源指纹并分离在线调用与离线策略重放（F6）**：
   - 在 `t4_live_paired_cache_v2.json` 与 `t4-c-on-demand-report.json` 的每条记录中写入 `sample_id`、`label_sample_id`、`label_status`、`label_provenance`、`prompt_sha256`、`model`（`deepseek-reasoner`）与 `contract_version`（`current` / `claims_v2`）。
   - 将 `execution_telemetry` 明确拆分为：
     - `live_network_calls`: `0`（本次校准全程离线重算）
     - `cached_reused_calls`: `170`（复用 85 条 $\times$ 2 臂真实测量缓存）
     - `offline_selector_replay_items`: `85`

---

## 2. 样本构成与标签绑定状态总览（85 条自然候选 + 16 条独立构造探针）

| 样本分群 / 来源 (`by_label_provenance`) | 样本数 | `label_status` | 说明 |
|---|---:|---|---|
| `pilot12_bound`（《道诡异仙》已复核绑定样本） | 5 | `bound` | 与 `pilot12.json` 冻结指纹 100% 一致（2 正例、3 负例） |
| `blind_reference_verified_positive`（跨作品盲标验证正例） | 2 | `bound` | 命中独立盲标回收参考集 `dev_obs_006`、`dev_obs_016` |
| `mechanical_or_source_verified_negative`（跨作品核实负例） | 10 | `bound` | 9 条同章铺垫回声（`rch == ach`）+ 1 条多子句首句越界 |
| **自然候选独立绑定子集小计 (`strict_bound_subset_17`)** | **17** | **`bound`** | **4 正例 + 13 负例，可作为严格绑定验收依据** |
| `legacy55_pilot12_stale_without_prereg` | 4 | `stale` | `pilot12` 含 `prereg` 而 `legacy55` 不含，指纹不一致 |
| `legacy55_historical_unbound` | 46 | `unbound` | 历史未复核样本（含 12 条 `待裁决`） |
| `model_self_nominated_unverified` | 18 | `unbound` | 被测模型自身 `repaired` 判为支持但未命中独立参考回收集的候选 |
| **自然候选全量诊断池合计 (`diagnostic_all_85_nominal`)** | **85** | 17 `bound` / 4 `stale` / 64 `unbound` | 仅作全量触发率、Token 成本与拒真原因拆解诊断 |
| **独立构造对抗探针集 (`constructed_regression_set_16`)** | **16** | **`bound`** | 8 正例 + 8 负例，独立分表报告 |

---

## 3. 严格独立绑定子集（`strict_bound_subset_17`）与构造探针集（`16` 条）结果

### 3.1 自然候选独立绑定子集（17 条：4 正例 + 13 负例）

| 指标 (`strict_bound_subset_17`) | `current` | `full_claims_v2` | `on_demand_claims_v2` | 说明 |
|---|---:|---:|---:|---|
| 绑定候选数 (`total_candidates`) | 17 | 17 | 17 | 仅统计 `label_status == "bound"` |
| 触发 `claims_v2` 次数 (`claims_v2_calls`) | 0 (0.00%) | 17 (100.00%) | **13 (76.47%)** | 节省 4 次 `claims_v2` 调用（`-23.53%`） |
| **负例误接受数 (`false_accepts`)** | **1 / 13 (7.69%)** | **0 / 13 (0.00%)** | **0 / 13 (0.00%)** | 拦截基线误接受，未触发样本 0 误接受 |
| 正例接受数 (`positive_accepts`) | 2 / 4 (50.00%) | 0 / 4 (0.00%) | 0 / 4 (0.00%) | 见第 4 节正例未通过原因拆解 |
| 实测总 Token (`total_tokens`) | 51,136 | 80,253 | **73,161** | 较 `full_claims_v2` `-8.84%`，较 `current` `+43.07%` |

### 3.2 构造对抗探针回归集（16 条：8 正例 + 8 负例，全部 `bound`）

| 构造回归集指标 (`constructed_regression_set_16`) | `current` | `full_claims_v2` | `on_demand_claims_v2` |
|---|---:|---:|---:|
| 样本总数（8 正例 + 8 负例） | 16 | 16 | 16 |
| `claims_v2` 触发次数 / 缩减率 | 0 (0.00%) | 16 (100.00%) | **11 (68.75%，缩减 -31.25%)** |
| 未触发负例误接受数 (`untriggered_false_accepts`) | — | — | **0** |
| 负例误接受数 (`false_accepts`) | 1 / 8 (12.50%) | **0 / 8 (0.00%)** | **0 / 8 (0.00%)** |
| 正例接受数 (`positive_accepts`) | 7 / 8 (87.50%) | **8 / 8 (100.00%)** | **7 / 8 (87.50%)** |
| 实测总 Token 消耗 (`total_tokens`) | 66,191 | 89,170 | **81,703（较全量 -8.37%，较 `current` +23.44%）** |

---

## 4. 关键诊断：51 条名义“支持”样本在 `claims_v2` 下的四类原因拆解（F3）

为什么不能把 85 条名义样本上 `full_claims_v2` 的低接受率（`9/51 = 17.65%`）简单归结为“`claims_v2` 审核过严需要放宽”？我们对全部 51 条名义“支持”样本（31 条 `legacy55` + 20 条跨作品模型自提名）进行了逐条独立归因（见 `t4-c-on-demand-report.json` 的 `positive_rejection_attribution_51`）：

| 归因类别 (`positive_attribution_bucket`) | 合计 (51 条) | `legacy55` (31 条) | 跨作品模型提名 (20 条) | 严格绑定正例 (4 条) | 实质含义与工程启示 |
|---|---:|---:|---:|---:|---|
| 1. `accepted_by_v2`（通过 `claims_v2` 审核） | **9** (17.65%) | 8 | 1 | 0 | 候选答案精炼且每个分句均有当前章节原文直接支撑 |
| 2. `candidate_answer_flawed_or_closing_unmet_in_both`（**候选答案本身有硬伤，连 `current` 也拒绝/存疑**） | **13** (25.49%) | 4 | **9** | 2 | **非 `claims_v2` 误杀！** 在 20 条跨作品模型自标“支持”样本中，有 **9 条（45%）** 因候选答案夹带跨章未证实推断或未满足关闭条件，连 `current` 基线都未予接受（如 `zl_ind_dev_repaired_fs_027`、`zl_game_dev_repaired_fs_007` 等）。把它们当成真值正例会严重高估审核误杀率 |
| 3. `closing_criteria_unmet_under_v2_prompt`（NLI 主判定未满足关闭条件或忠实性，先于原子拆解失败） | **9** (17.65%) | 4 | 5 | 1 | `current` 宽松放行，但在 `claims_v2` 提示词下模型主判定即发现 `criteria_satisfied=false` 或 `answer_supported=false`（`NLI=off_topic` / `NLI=unclear` / `缺少条件满足或答案忠实性确认`） |
| 4. `protocol_or_call_failure`（协议格式或分区覆盖错误） | **1** (1.96%) | 0 | 1 | 0 | `zl_game_dev_repaired_fs_010` 触发 `unknown_answer_frame`，属输出格式遵循问题而非审核标准过严 |
| 5. `claim_unit_granularity_or_overstatement_blocked`（`current` 接受，被 `claims_v2` 原子单元粒度/模态/归属拦截） | **19** (37.25%) | 15 | 4 | 1 | 候选回答主干正确，但复检生成的 `answer` 带有总结性修辞、推测模态词或复合因果，在 `claims_v2` 原子对齐中被判 `changed_attribution` / `changed_modality` / `missing_quote` |

### 结论与后续行动顺序

1. **绝不盲目放宽 `claims_v2` 审核门槛**：51 条名义正例中有 **13 条本身答案有缺陷（连 `current` 都不接受）**、**9 条未满足关闭判准**、**1 条协议格式错误**；真正属于“`current` 接受但被 `claims_v2` 原子粒度拦截”的仅 **19 条**（其中许多是因为上游复检生成 `answer` 时添加了原文未直说的总结性副句）。若直接放宽 `claims_v2`，将重新放行多子句越界与伪造因果的 C 类幻觉。
2. **全量 85 条自然候选的成本账本（F6 如实披露）**：
   - `on_demand_claims_v2` 将 `claims_v2` 调用次数从 85 次降至 **57 次（缩减 `-32.94%`）**；
   - 总 Token 消耗为 **366,002**，较 `full_claims_v2`（**403,693**）节省 **`-9.34%`**，但较 `current` 基线（**267,724**）仍增加 **`+36.71%`**。
3. **正确改进方向**：保持 `enable_c_on_demand_audit` 默认关闭（`False`）；下一步应先约束上游复检生成阶段只输出紧贴引文的精简结论（从源头减少 `answer` 中的修饰性越界子句），并完成剩余 `legacy55` 与跨作品样本的独立人工盲标绑定，而非放松下游审核器。
