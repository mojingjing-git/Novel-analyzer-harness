---
doc: S4–S6 执行拆解
plan: docs/PLAN-v2-long-novel-reliability-2026-10-30.md §4 S4/S5/S6
plan_version: 2.2
status: s4_s6_done
baseline_commit: f420d2f
created: 2026-10-30
---

# S4–S6 执行拆解：有序覆盖、稳定实体、全自动裁决

三批的共同点：**结论必须能被程序复算**。S4 复算"覆盖到哪"，S5 复算"这个悬念是谁"，
S6 复算"这条证据够不够格"。凡是要靠排序序号、完成顺序或模型自评置信度才能得出的结论，
一律不算。

## S4：有序 rolling 与缺口恢复（C-03）

| 单元 | 内容 | 状态 |
| --- | --- | --- |
| U1 | 覆盖视图：`commit_cursor` 连续前缀、`committed_batches`、`consumable_upto` **对齐批次**、`gaps`、`dirty_from`、`missing` | ✅ `backend/vnext/rolling.py` + 19 例 |
| U2 | 状态语义：`DONE` 推进 / `TERMINAL_SKIPPED` 越过但留 gap 并标 `PARTIAL` / `PENDING_RETRY` **暂停推进** | ✅ 同上 |
| U3 | 结果映射：只认与当前块签名一致的结果；失败/取消 → `PENDING_RETRY`；签名不符 → 忽略 | ✅ `slots_from_results` |
| U4 | 声明范围：`claimed_range()` 从**真实首章**起算，不称"全文前缀" | ✅ 同上 |
| U5 | 实际 rolling 提交与摘要重建接线到流水线 | ⏳ 属 S7（需真实执行器） |

**关键判据（已落用例）**：① 1、3 完成而 2 缺失时不得宣称"接近完成"；② 10 种完成顺序得到同一覆盖；
③ 脏点使 `consumable_upto` 收缩到脏批次起点并置 `rebuild_required`；④ 跳过不等于完整。

> 说明：`consumable_upto` 与 `claimed_range` 的差别是刻意的——**可消费**对齐批次（安全），
> **可声明**用实际完成位置（诚实）。把两者合成一个数，就会出现"摘要只吃了 2 块、报告声明覆盖 3 块"。

## S5：稳定伏笔实体与截止视图（C-27 / R2 / R3）

| 单元 | 内容 | 状态 |
| --- | --- | --- |
| U1 | 身份：`occurrence_id`（完整 64 hex，含 kind）、`entity_id`（`fs_` + 16 hex，只依赖首次出现）、`candidate_id`（`cd_` + 16 hex，含卷起点与批次） | ✅ `backend/vnext/foreshadow.py` |
| U2 | `register()`：seed 取**来源最早**的出现（不是列表首个）；幂等，不因补入更早证据改号 | ✅ |
| U3 | `cutoff_view()`：先筛可见来源 → **整条排除**（有任何一条越过截止点的实体整个不出现）→ 算属性 → 过滤 → 排序 → 截断 | ✅ |
| U4 | 去重语义：同一悬念再提 = 同实体新增 occurrence；**不同悬念不因文本相似合并**（合并只认 caller 给的 `entity_id`） | ✅ 由 API 形状保证（本层不做相似度判断） |
| U5 | 只读诊断输出（D-03）：格式版本、来源 revision、三类计数、状态分布、`recheck_status`、跳过原因 | ⏳ 待做（需与只读 API 同源） |

**关键判据**：补入更早证据后 `entity_id` 不变、`first_seen` 变小；固定 1–10 章、只改 11 章以后，
前 10 章视图完全不变。

## S6：原文证据与全自动裁决（C-05 / C-10 / R4）

| 单元 | 内容 | 状态 |
| --- | --- | --- |
| U1 | 严格枚举：`SUPPORTED`/`DIFFERENT_ANSWER`/`CONTRADICTED`/`INSUFFICIENT`；小写/近义词/缺失 → `INVALID` + reason，**不得当通过** | ✅ `backend/vnext/resolution.py` + 34 例 |
| U2 | 反证必查：`performed is True` 才算查过；`counter_quotes` 非空 → `INVALID`；"没查"与"查了没有"必须可区分 | ✅ |
| U3 | 证据闸门：外来 ID、空答案、材料不足、引文越界 → `INVALID`，**不循环追问模型** | ✅ |
| U4 | 实体归约：任一 `SUPPORTED` → `AUTO_RESOLVED`；仅不足/冲突 → `PROPOSED_RESOLVED`；只有垃圾 → `OPEN`；**垃圾候选不降级已支持结论** | ✅ `reduce_entity` |
| U5 | 覆盖计数：候选维度守恒式 + `INVALID` 按 reason 分组求和校验 | ✅ `CoverageCounts.conservation_error` |
| U6 | 任务状态：`NOT_RUN`/`RUNNING`/`STOPPED`/`PARTIAL`/`COMPLETED` 有序互斥；**空候选跑过 = COMPLETED(0)**、**缺汇总 = PARTIAL** | ✅ `recheck_status` |
| U7 | 引用定位（`quote_hint` → 程序定位 → `evidence_id`）与上下文取材（`context_start/end`、`context_truncated`） | ◑ 取材完成（`context.py` 12 例）；定位复用 `citations.verify_citation` + `fingerprint.evidence_id`，接线待 S7 |
| U8 | 只读呈现与报告口径（四类分栏） | ⏳ 待做（前端批次） |

**关键判据**：引用错误/过期/顺序错误不能通过；新垃圾候选不降级独立有效结论；
不确定结果完成后可缓存、恢复幂等；每次复检留下可核对覆盖计数。

## 与 S7 的边界

S4–S6 建的是**判定与状态层**：给定输入，它必须给出唯一、可复算的结论。
把这些接进真实分析流水线（真跑 LLM、真写摘要、真出报告）是 S7 的工作。
本批因此**不改** 1.x 的 `pipeline.py` / `final_summary.py`。

## 未决

1. `ContextStrategy` 的切片规则（前置/后置字符目标）需要真实语料标定，本批只留接口。
2. `cutoff_view` 的 `importance_score` 是确定性公式（`mentions*2 + min(span,20)`），
   是否加入"重要度声明"字段由 S7 的真实数据决定。
