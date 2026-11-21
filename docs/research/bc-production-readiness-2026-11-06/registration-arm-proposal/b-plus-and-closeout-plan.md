# 伏笔登记 Rule B+ 靶向验证、生产开关接入与结案总表收尾计划（方向 1 + 2 + 3）

## 一、目标与执行顺序

为一次性高质量完成方向 1（结案总表归档）、方向 2（`p3` 前缀靶向验证 `Rule B+`）与方向 3（生产管线可选开关接入与试用配置沉淀），采用**「先跑方向 2 靶向实测 → 再做方向 3 生产代码接入与单测 → 最后做方向 1 总表汇总归档」**的流水线顺序，使最终写入 [`结案总表.md`](file:///F:/AI/01_项目/小说分析器/docs/research/bc-production-readiness-2026-11-06/结案总表.md) 的数据同时涵盖 `Rule A vs Rule B` 全量小试与 `Rule B+` 修复验证结论。

---

## 二、方向二：用剩余额度靶向验证 `Rule B+`（`t4_hist_holdout/p3` 与 `t4_game_holdout/p3`）

### 2.1 实验设计与防覆盖原则
1. **复用已冻结合格参考集（0 额外发现成本）**：
   - 直接读取已冻结且 `pending_count == 0` 的 `reference-eligible-t4_hist_holdout-p3.json`（2 条合格参考）与 `reference-eligible-t4_game_holdout-p3.json`（8 条合格参考）。
2. **新增独立脚本 `scripts/bc_t4_run_b_plus_p3_verification.py`，绝不覆盖既有 A/B 产物**：
   - **生成阶段（2 次调用，`stage="generation"`，`arm="B_PLUS"`）**：
     - System 提示词：`common-system.txt + "\n\n" + arm-b-plus-rule.txt`
     - User 提示词：与 A/B 两臂完全一致的 `common-user-template.txt` 渲染结果（`visible_range_end=3`）
     - 模型与参数：严格保持 `cn:deepseek-v4.1-flash`、`temperature=0.1`、`max_output_tokens=2000`
     - 独立落盘文件：
       - `generation-t4_hist_holdout-p3-arm_b_plus.json`
       - `generation-t4_game_holdout-p3-arm_b_plus.json`
       - `freeze-generation-t4_hist_holdout-p3-arm_b_plus.json`（含 `check_isolation` 硬隔离校验）
       - `freeze-generation-t4_game_holdout-p3-arm_b_plus.json`（含 `check_isolation` 硬隔离校验）
   - **去标识混排盲审阶段（2 次调用，`stage="blind_review"`，`arm="-"`）**：
     - **防输出截断设计（关键约束）**：由于单次请求输出上限 `per_request_output_limit = 2000` tokens，而此前 `p3` 的双臂盲审输出已达 `1,686 ~ 1,687` tokens，若将 `A + B + B+` 三臂约 30 条候选塞入同一次请求会触发 2,000 token 输出截断风险。因此，本次盲审包将 **`Arm A`（对照组）与 `Arm B+`（修复候选组）** 的 `host_items` 进行去标识混排（使用独立确定性种子 `SHA256("blind_review_b_plus_v1:{work_id}:{prefix_id}")`），候选总数约 13~18 条，安全落在 2,000 输出 token 限额内。
     - 独立落盘文件：
       - `blind-package-t4_hist_holdout-p3-b_plus.json` / `blind-review-t4_hist_holdout-p3-b_plus.json`
       - `blind-package-t4_game_holdout-p3-b_plus.json` / `blind-review-t4_game_holdout-p3-b_plus.json`
       - 汇总三臂对比文件：`b-plus-p3-verification-summary.json`（在 `p3` 上横向对比 `Arm A`、`Arm B` 原版、`Arm B+` 修复版的 `no_ground_rate`、`already_answered_rate`、`span_localization_rate`、`anchor_sufficiency` 与 `missed_registration_rate`）。

### 2.2 预算合规预检
- 当前 `reference-discovery-usage.json` 余额：
  - 剩余尝试次数：**8 次**（已用 `34 / 42`）
  - 剩余输入 tokens：**103,213 tokens**（已占 `232,787 / 336,000`）
  - 剩余输出 tokens：**50,276 tokens**（已占 `33,724 / 84,000`）
- 本次 4 次调用预计消耗：
  - 尝试次数：**4 次**（完成后总计 `38 / 42` 次，仍余 4 次容错裕量）
  - 保守输入上界：2 次生成约 `17,800` tokens + 2 次盲审约 `26,500` tokens ≈ **44,300 tokens**（远低于剩余 `103,213` tokens，且每次单请求均 `< 14,500 < 20,000`）
  - 预留输出上界：`4 × 2,000 = 8,000` tokens（远低于剩余 `50,276` tokens）

---

## 三、方向三：生产管线可选开关接入与试用配置沉淀

### 3.1 代码改动清单
1. `backend/config/settings.py`：
   - 在 `AnalysisConfig` 的 B/C 改进独立实验开关区新增字段 `enable_anchor_b_plus_rule: bool = False`，默认值严格为 `False`，确保未开启时基线行为与 prompt 哈希零变化。
2. `backend/core/prompt_builder.py`：
   - 定义常量 `ANCHOR_B_PLUS_RULE_TEXT`（对应 `arm-b-plus-rule.txt` 的三步规则：① 截至本章末尾的全局未解守卫；② 候选引文末尾的局部前提齐备守卫；③ 同题合格句中选本章最早一句）。
   - 在 `PromptBuilder.__init__` 新增可选参数 `enable_anchor_b_plus_rule: bool = False`。
   - 在 `PromptBuilder.build_messages` 中，仅当 `self.enable_anchor_b_plus_rule` 为 `True` 时，将 `ANCHOR_B_PLUS_RULE_TEXT` 注入系统提示词的 `## 伏笔分类约束（重要）` 段落。
3. `backend/core/analyzer.py`：
   - 在 `NovelAnalyzer.__init__` 构造 `PromptBuilder` 时传入 `enable_anchor_b_plus_rule=bool(getattr(config.analysis, "enable_anchor_b_plus_rule", False))`。
4. 推荐试用配置模板：
   - 新增 `docs/research/bc-production-readiness-2026-11-06/recommended-trial-config.json`（不含任何真实私钥），开启已验证达标的 `fix_b4_flow_and_storage`、`fix_b2_evidence_and_recheck`、`enable_b_trace`，并附带 `enable_anchor_b_plus_rule` 开关说明。
5. 自动化回归测试（`backend/tests/test_bc_production_readiness.py`）：
   - 新增测试用例验证：`enable_anchor_b_plus_rule=False` 时 `PromptBuilder` 输出与原 `SYSTEM_PROMPT` 完全一致；`enable_anchor_b_plus_rule=True` 时包含“全局未解守卫”与“引文处前提齐备”规则；`_coerce_fields(AnalysisConfig, ...)` 正确解析布尔开关；`NovelAnalyzer` 正确透传该配置。

---

## 四、方向一：更新《结案总表.md》与《pilot-evaluation-report.md》

1. 更新 `docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal/pilot-evaluation-report.md`：
   - 增补第 7.3 节「`Rule B+` 在 `p3` 前缀上的靶向修复验证实测结果」，展示 `Arm A vs Arm B vs Arm B+` 在 `hist/p3` 与 `game/p3` 上的实测指标对比与最终台账消耗。
2. 更新 `docs/research/bc-production-readiness-2026-11-06/结案总表.md`：
   - 在「一、工程状态」与「四、采用决定」中补充 `enable_anchor_b_plus_rule` 开关状态与最新回归测试通过数；
   - 在「六、已定位但未修复的缺陷」及「六之四、登记评估的尺子与冻结」中，将“锚点选晚导致登记滞后”从“待验证假设”更新为已完成两臂小试与 `Rule B+` 靶向验证的实测结论；
   - 在「七、证据索引」中补齐 `pilot-evaluation-report.md`、`pilot-metrics-summary.json`、`b-plus-p3-verification-summary.json` 与 `reference-discovery-usage.json`。
