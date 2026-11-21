# T2 跨作品分层抽样与原文盲读标注协议报告

## 1. 抽样与隔离协议
- **分层清单**：[`docs/research/bc-improvement-2026-11-05/corpus-plan.json`](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/corpus-plan.json)
- **冻结清单哈希**：`54e4061f92b95dce68d02dd570df26767949c0feb658ac4907f1a57263118592`
- **题材分组（4 组 × 开发集 4 部 / 留出集 4 部）**：
  1. `industrial_scifi`：开发集 `ind_dev`（《学霸的黑科技系统》）/ 留出集 `ind_holdout`（《超神机械师》）
  2. `gaming_esports`：开发集 `game_dev`（《亏成首富从游戏开始》）/ 留出集 `game_holdout`（《网游之近战法师》）
  3. `urban_daily`：开发集 `urban_dev`（《大医凌然》）/ 留出集 `urban_holdout`（《我真没想重生啊》）
  4. `history_court`：开发集 `hist_dev`（《秦吏》）/ 留出集 `hist_holdout`（《宰执天下》）
- **位置分层（4 层/部）**：每部作品按章节四分位随机抽取 `early`、`mid_early`、`mid_late`、`late` 各 1 个连续窗口（`window_chars = 12000`，种子 `bc-improvement-20261105-v1`）。
- **整书隔离**：全部 11 部已暴露历史作品均已登记在 `previously_exposed_titles`，与本次 8 部作品零交集；留出集在 M1..M3 期间保持 `metadata_only` 未导出状态。

## 2. 原文优先（Source-First）双人盲读标注规则
1. **盲读输入**：标注员仅读取由 [`scripts/bc_corpus.py`](file:///F:/AI/01_项目/小说分析器/scripts/bc_corpus.py) 导出的纯原文窗口包（不含任何模型输出、候选列表、账本或提示词）。
2. **两类观察点严格分列**：
   - **登记项（`registration`）**：窗口内埋下悬念、承诺、秘密、异常或未完计划，且在当前窗口内尚未完全揭晓，评测采集/去重/总表/账本流转是否登记并保留（对应 `B1_UNREGISTERED` 与 `B4_FLOW_LOST`）。
   - **回答项（`resolution`）**：窗口内既有埋设引句（`anchor_quote`），又在后文位置给出了明确解答/兑现引句（`resolution_quote`，满足 `resolution_local_span[0] > local_span[0]`），评测检索与复检判定是否召回（对应 `B2_EVIDENCE_MISSED` 与 `B3_JUDGMENT_MISSED`）。
3. **双人独立复核与冲突裁决**：
   - 初标（`luna_blind_annotator_6597582e`）与复核（`main_agent_blind_reviewer`）独立核对原文切片坐标、问题定义与回收判据。
   - 开发集 56 条观察点中，**52 条直接一致（初始一致率 `92.86%`）**，4 条边界样本（`dev_obs_011`、`dev_obs_015`、`dev_obs_022`、`dev_obs_037`）经裁决细化了 `question` 与 `answer_criteria` 的可核实边界并记录 `adjudicated_refined`。

## 3. 开发集盲读参考集统计（[`t2-dev-reference.json`](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/t2-dev-reference.json)）

| 作品 ID | 题材分组 | 窗口数 | 登记项 (`registration`) | 回答项 (`resolution`) | 合计 |
|---|---|---:|---:|---:|---:|
| `ind_dev` | `industrial_scifi` | 4 | 8 | 6 | 14 |
| `game_dev` | `gaming_esports` | 4 | 8 | 6 | 14 |
| `urban_dev` | `urban_daily` | 4 | 8 | 6 | 14 |
| `hist_dev` | `history_court` | 4 | 8 | 6 | 14 |
| **合计** | **4 组** | **16** | **32** | **24** | **56** |

- **原文校验**：56 条 `anchor_quote` 及 24 条 `resolution_quote` 100% 通过字节级切片与先后顺序校验（`observations_sha256 = 62016748aabe1ba10168a319aac9c9d129da0972b29d372adaf8b8dbf3fe3b99`）。
- **版权保护**：Git 跟踪产物 [`t2-dev-reference.json`](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/t2-dev-reference.json) 仅保存 `sample_id`、`local_span`、引文长度与 SHA256 摘要，不包含任何小说原文。
