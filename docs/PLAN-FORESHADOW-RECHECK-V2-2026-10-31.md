# PLAN：伏笔复检 V2 —— 前置加锚点、后置整块替换（在 1.x 底座上落地）

- 日期：2026-10-31
- 版本：v2（完整版，取代同日 draft）
- 状态：待批准后执行
- 适用底座：**1.x 现役管线**（不改数据空间、不重写分析链路）
- 前置决定（用户已定）：**老结果撇掉重跑**；整本分析期间**不启动最终总结**

---

## 1. 一句话目标

让每条伏笔从被识别的那一刻起就带**原文坐标**；总结阶段的"伏笔复检"整块换成
**证据可核验、覆盖可计数、结论可回退**的新实现——在你现有的软件上直接生效，不重跑第二遍分析。

---

## 2. 为什么必须做（现状事实，均带证据）

| # | 现状 | 证据 | 后果 |
| --- | --- | --- | --- |
| 1 | 伏笔只有模型概括，没有原文锚点 | `models/analysis_result.py:32` `Foreshadowing` 只有 `clue/type/implication/confidence/importance`；真实结果里 `clue = "内容简介预告许七安起初只想自保…"` | 程序无法把"这条伏笔"钉到原文任何一句上，追溯无从谈起 |
| 2 | 伏笔身份按出现顺序编号 | `utils/text_utils.py:325` `f"fs_{g_idx + 1:03d}"` | 补入一条线索，后面全体位移；跨轮次无法认定"同一条" |
| 3 | 复检只在有"活跃"伏笔时才跑 | `services/final_summary.py:1386`、`:1400` 两句 `无活跃伏笔，跳过全书复检` 后 `return` | 真实账本 700 条里 `active = 0`（`resolved 154 / dormant 546`）⇒ **复检从未真正执行过** |
| 4 | 休眠判据是 200 章硬阈值 | `utils/foreshadow_ledger.py:105` `reconcile_and_update` | 78% 伏笔被判死，`active` 归零，形成 #3 的死循环 |
| 5 | "支持"无需证据 | `final_summary.py:1322-1336` 只按 `is_resolved_in_this_batch` 落 `resolved` | 模型说支持就算回收；低置信只标 `needs_review`，不阻断 |
| 6 | 复检靠模型自报位置 | 同上，模型给 span，数不出就废 | 实测（C2 30 条标注）：**20 条判"支持"、引文逐字命中 9/9，但 11 条因 span 对不上被判 INVALID** |

> 一句话：**问题不是"复检写得不好"，而是"复检没有可核验的原料，且它从来没有被真正执行过"。**

---

## 3. 现状链路（我们要动的边界）

```
逐章分析（前置，本轮只加两个字段）
  analyzer.analyze_chapter(:95) → prompt_builder.build_messages(:320) → LLM
  → validate_json_response 内解析 → AnalysisResult → 写 chapter_N_result.json
  → memory_state._merge_result_into(:343) 累积知识库

总结（后置，本轮只换第 2 步）
  阶段1 分卷摘要 + _build_foreshadow_catalog(:789) ──┐
  阶段2 _run_global_foreshadow_recheck(:1380) ◀──────┘  ← 整块替换
  阶段3 风格分析（并行）
  阶段4 最终报告；:1720 audit_text_for_report
```

**改动边界**：前置只碰"伏笔字段 + 落盘前校验"；后置只碰阶段 2 及其账本/渲染。
`pipeline.py`、`memory_state` 的合并逻辑、结果文件命名、前端，**一律不动**。

---

## 4. 六条设计决策

### D1 锚点由前置采集，且在落盘前校验

- 提示词要求模型给 `anchor_quote`（逐字、8–40 字、不改标点、不拼接）。
- `analyzer` 在写盘前用 `citations.verify_citation` 在**本章正文**里定位：
  - 命中 → `anchor_quote` 规范化为原文切片 `text[start:end]`，记 `anchor_chapter`
  - 未命中 → 置空并累计 `anchor_missing`，**不抛错**（一条伏笔缺锚点不该让整章失败）
- 产出 `anchor_stats: {total, located, missing}` 写进结果，作为**可用性指标**。

> 理由：校验需要正文，`analyzer` 手上就有；等复检再验，等于先花完钱再发现问题。

### D2 伏笔身份改为稳定键，`fs_001` 降为别名

- `entity_key = sha256(book_id | anchor_quote_hash | motif_key)[:16]`
- 账本条目主键变 `entity_key`；旧的 `id` 字段保留（`fs_001` 只用于展示与兼容）
- 后果：新增一条线索不会让已有条目位移；同一伏笔跨轮次稳定。

### D3 裁决四枚举 + 证据门禁（移植 vnext `resolution.py`）

```
SUPPORTED / DIFFERENT_ANSWER / CONTRADICTED / INSUFFICIENT
```

`SUPPORTED` 必须同时满足（九条，实现即 `shared/verdict.py`）：

1. `verdict == SUPPORTED`
2. `answer` 非空
3. 无 `material_insufficient_reason`
4. `counter_search.performed is True` 且 `counter_quotes` 为空
5. `evidence_id` 属于本次复检允许的集合
6. `supporting_quote` 非空且长度 ≥ 6
7. 引文能在该证据原文里定位，且回验通过（`EXACT` 严等 / 其余双侧规范化相等）
8. 引文出现 ≥ 2 次时，模型自报位置能指到其中一次
9. `checked_evidence_ids` 非空且全部属于本次任务

任一条不满足 → 对应 reason（`QUOTE_NOT_FOUND` / `QUOTE_TOO_SHORT` / `AMBIGUOUS_QUOTE` /
`COUNTER_SEARCH_UNVERIFIED` / `FOREIGN_ID` …），**一律不算"已回收"**。

> 实测依据：把"位置由程序算"接进去后，同一批 30 条标注召回 0.45 → **0.95~1.00**，误报 0。

### D4 覆盖必须计数披露，禁止静默跳过

每次复检产出一份 `coverage`：

```json
{"candidates_total": 700, "checked": 640, "located": 612,
 "supported": 88, "insufficient": 470, "conflict": 40, "invalid": 42,
 "stale": 0, "not_checked": 60, "budget_skipped": 60,
 "calls": 18, "tokens": {"prompt": 120000, "completion": 40000}}
```

- 守恒式：`candidates_total == supported + insufficient + conflict + invalid + stale + not_checked`
- **空候选也要跑过一次并记 `COMPLETED(0)`**，让"没有候选"与"没有运行"可区分
- `recheck_status ∈ {NOT_RUN, RUNNING, COMPLETED, PARTIAL, STOPPED}`，与"伏笔是否已回收"正交

### D5 批规划预算化

- 沿用 `analysis.foreshadow_recheck_batch_size`（默认 40）
- 新增单轮上限 `analysis.foreshadow_recheck_max_calls`（默认 200）与 `..._max_tokens`
- 超预算 → 剩余候选记 `budget_skipped`，`recheck_status = PARTIAL`，**可续跑**

### D6 可回退

- 新增配置 `analysis.foreshadow_recheck_v2`（默认 `false`）
- `false` → 旧实现原样；`true` → 新实现。切换不改代码。
- 首次启用时把旧账本备份为 `foreshadow_ledger.v3.bak.json`，按新结果重建。

---

## 5. 数据契约变更

### 5.1 逐章结果（`chapter_N_result.json` 的 `foreshadowing[]`）

```json
{
  "clue": "许七安连续试探系统，没有回应",
  "anchor_quote": "许七安又喊了一声“系统爸爸”，屋里静得只剩烛火噼啪。",
  "anchor_chapter": 7,
  "motif_key": "系统沉默",
  "type": "系统",
  "implication": "...",
  "confidence": "高",
  "importance": "高"
}
```

新增字段全部有默认值 ⇒ 旧结果仍可读（但缺锚点，复检会判 `insufficient/NO_EVIDENCE`）。

### 5.2 账本（`foreshadow_ledger.json`，`SCHEMA_VERSION` 3 → 4）

```json
{
  "schema_version": 4,
  "items": [{
    "id": "fs_001",                    // 展示用，兼容旧数据
    "entity_key": "9f3a...16hex",      // 主键
    "description": "...",
    "motif_key": "系统沉默",
    "anchor_quote": "…逐字原文…",
    "anchor_chapter": 7,
    "first_seen_chapter": 7,
    "last_seen_chapter": 12,
    "status": "open|resolved|dormant",
    "resolution_state": "OPEN|PROPOSED_RESOLVED|CONFIRMED_RESOLVED",
    "evidence": [{"chapter": 12, "quote": "...", "start": 1024, "end": 1061,
                  "content_hash": "…", "verdict": "SUPPORTED"}],
    "recheck_status": "COMPLETED",
    "notes": "..."
  }]
}
```

### 5.3 报告（`foreshadow_audit.md`）

```
## 伏笔审计 总计 700 条
覆盖：候选 700 · 已核对 640 · 定位成功 612 · 预算跳过 60
分类：未回收 470 · 自动判定已回收 88 · 证据不足 42 · 休眠 100

### 自动判定已回收（88）
- [fs_001] 系统沉默 — 第 7 章埋，第 12 章回收
  依据：「许七安又喊了一声“系统爸爸”，屋里静得只剩烛火噼啪。」（第12章 · 码点 1024–1061）
  反证检索：已查 3 段（ev_12_a, ev_12_b, ev_07_c），未发现相反表述
  ...
```

### 5.4 配置（`config.json` 的 `analysis`）

```json
"foreshadow_recheck_v2": false,
"foreshadow_recheck_max_calls": 200,
"foreshadow_recheck_max_tokens": 400000
```

---

## 6. 详细改动清单

### 阶段 0：前置（我写，约 60 行）

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `core/prompt_builder.py` | `:57` | `foreshadowing` schema 增 `anchor_quote`、`motif_key` 两个字段 |
| `core/prompt_builder.py` | `:50` 附近 | 增一句硬约束：逐字照抄、找不到留空、禁止改写拼接；编造会被程序判空 |
| `models/analysis_result.py` | `:32` | `Foreshadowing` 增 `anchor_quote: str = ""`、`motif_key: str = ""` 与 `anchor_chapter: int = 0` |
| `core/analyzer.py` | `analyze_chapter` 内、构造 `AnalysisResult` 之前 | 逐条校验锚点（见 D1），写入规范化切片、`anchor_chapter`、并汇总 `anchor_stats` |
| `core/analyzer.py` | 同上 | `anchor_stats` 落进 result 顶层（供 pipeline 汇总与日志） |
| `config/settings.py` | `AnalysisConfig` | 增三个配置项（`foreshadow_recheck_v2` 等），默认值见 §5.4 |

> 序列化不用改：`AnalysisResult.to_dict` 用 `asdict`（`:114-115`），新字段自动落盘。

### 阶段 1：你跑（不改代码）

```json
"analysis": { "auto_summary": false }
```

整本分析跑完即止（不点"开始总结"）。期间我只新增文件，不动运行路径。

### 阶段 2：移植（我写，全部新增文件）

| 新文件 | 内容 | 来源 | 规模 |
| --- | --- | --- | --- |
| `backend/shared/citation.py` | 五级引用匹配、`locate_quote`、规范化 | 搬 vnext `citations.py` | ~282 行 |
| `backend/shared/verdict.py` | 四枚举、九条判据、覆盖计数、守恒式 | 搬 vnext `resolution.py` | ~666 行 |
| `backend/shared/foreshadow_identity.py` | `entity_key` 派生、`motif_key` 归一 | 搬 vnext `foreshadow.py` 的键部分 | ~120 行 |
| `backend/services/foreshadow_recheck.py` | 复检主体：候选→批规划→裁决→状态迁移→四栏渲染 | 新写 | ~400 行 |

`foreshadow_recheck.py` 对外接口：

```python
async def run_recheck(
    *, ledger: ForeshadowLedger, results: list[dict], full_text: str,
    client, budget: RecheckBudget, verify=None,
) -> RecheckOutcome
# RecheckOutcome: coverage: CoverageCounts, verdicts: list[CandidateVerdict],
#                 report_markdown: str, status: str
```

### 阶段 3：接线（我写，改 `final_summary.py`）

| 位置 | 动作 |
| --- | --- |
| `:1380` `_run_global_foreshadow_recheck` | 改为 `foreshadow_recheck.run_recheck(...)`；`foreshadow_recheck_v2=false` 时走旧实现 |
| `:1386` / `:1400` | **删除**两句静默 `return` |
| `:1344` `_plan_recheck_batches` | 预算化；产出"本轮核对哪些候选"的清单 |
| `:1304` `_apply_reconciliation` | 消费新裁决；`SUPPORTED` → `resolved` 且写 `resolution_state=CONFIRMED_RESOLVED`；`INSUFFICIENT` → `open`；`CONFLICT` → `open + needs_review` |
| `:1693` `reconcile_and_update` | 休眠判据与复检结论联动（不再是 200 章硬阈值） |
| `:1720` `audit_text_for_report` | 四栏渲染（§5.3） |
| `:113` | 删掉"禁止同一批次回收"（同章先埋后收合法） |
| `final_summary.py:1526` | 建账本条目时带 `entity_key`/`anchor_quote`/`motif_key` |

---

## 7. 测试计划

### 7.1 单测（新增约 40 例）

| 文件 | 覆盖点 |
| --- | --- |
| `tests/test_anchor_quote.py`（10） | 逐字命中 / 模型改写（判缺）/ 空串 / 跨句拼接（判缺）/ 同章多次出现（取首次 + `ambiguous`）/ `anchor_stats` 计数 / 旧结果无字段（默认空） |
| `tests/test_shared_citation.py`（8） | 五级匹配 / 拒绝 `SEGMENTED`、`FUZZY` / 回验双侧规范化 / 重叠计数 |
| `tests/test_shared_verdict.py`（12） | 九条判据逐条拒绝 / 四枚举 / 反证不可核实 / 覆盖守恒式 / 空候选 `COMPLETED(0)` |
| `tests/test_foreshadow_identity.py`（5） | 同锚点同母题 → 同键 / 换母题 → 换键 / 旧 `fs_001` 兼容 |
| `tests/test_foreshadow_recheck.py`（5） | 候选构造 / 批预算 / `budget_skipped` / 续跑幂等 / 四栏渲染 |

### 7.2 集成（离线，不接产品路径）

拿阶段 1 真实结果 + 真账本跑一遍，输出四栏报告，**人眼抽查 20 条**。

### 7.3 回归

全量 `python -m pytest -o addopts= -q`（当前 1505 例）必须全绿；迭代期用 `scripts/pytest_related.py --run`。

---

## 8. 验收标准（分层，逐条可判）

| 层 | 标准 | 判定方式 |
| --- | --- | --- |
| L1 前置 | `anchor_stats.located / total ≥ 0.8` | 2 章真跑 |
| L2 前置 | 抽查 5 条 `anchor_quote`，必须**逐字**出现在对应章正文里 | 人工 |
| L3 移植 | 40 例单测通过；覆盖守恒式无差 | 自动 |
| L4 离线 | 四栏报告能生成；"已回收"每条都有可定位引文 | 人工抽 20 条 |
| L5 真数据 | 改前后对照表：活跃从 0 → N；出现"证据不足"这一新类别 | 报告 diff |
| L6 安全 | `foreshadow_recheck_v2=false` 时行为与改前完全一致 | 一次回归跑 |

---

## 9. 执行顺序与分工

| 步骤 | 谁 | 预计 |
| --- | --- | --- |
| 1. 阶段 0 前置改动 + 10 例 | 我 | 0.5 天 |
| 2. 小样本真跑（2 章）+ 达标判定 | 我（用你的 key 或本地网关） | 0.2 天 |
| 3. **你启动整本分析**（不跑总结） | 你 | 视书长而定 |
| 4. 阶段 2 移植（4 个新文件 + 33 例） | 我 | 1 天 |
| 5. 离线演练（拿你的真实结果） | 我 | 0.2 天 |
| 6. 阶段 3 接线 + 开关 | 我 | 0.5 天 |
| 7. 你的书上跑复检 + 抽查 20 条 | 我 + 你 | 0.3 天 |
| 8. 文档 + 提交 + 全量回归 | 我 | 0.3 天 |

第 3 步与第 4 步并行——这正是你提的方案的价值。

---

## 10. 风险与对策

| 风险 | 概率 | 对策 |
| --- | --- | --- |
| 模型不照抄锚点（`located < 0.8`） | 中 | L1 卡住，先调提示词再放行整本；不达标不进阶段 1 |
| 锚点校验拖慢分析 | 低 | 纯字符串匹配，单章 < 10ms；仅在有伏笔时执行 |
| 整本跑到一半我们改了代码 | 中 | 阶段 2 只新增文件；接线推迟到你跑完 |
| 新复检"证据不足"占比过高（> 60%） | 中 | 属提示词/批大小问题，不是逻辑问题；先调 prompt 再调批 |
| 账本 v3 混入 | 低 | 首次启用备份 + 重建 |
| 新实现效果不如旧的（体感） | 低 | 配置开关一键回退 |

---

## 11. 明确不做

- ❌ 不动 `pipeline.py`（并发/预热/补跑/停止）
- ❌ 不动 `memory_state.py` / `knowledge_base.py` 的合并逻辑
- ❌ 不改结果文件命名、目录结构、队列状态机
- ❌ 不动前端（四栏先落 `foreshadow_audit.md`）
- ❌ 不碰 `backend/vnext/`（冻结为参考实现，仅作为代码来源）
- ❌ 不做"历史账本迁移工具"（老账本直接备份后重建）

---

## 12. 附：本轮已实测的数字（供验收对照）

| 项 | 数字 |
| --- | --- |
| C2 标注（30 条）复测召回 | 0.45（旧，自报 span）→ **0.95~1.00**（新，程序定位），误报 0 |
| C3 采样（18 次） | `SUPPORTED` 12 → **17/18**，`INVALID` 5 → **0** |
| 真账本 | 700 条：`active 0 / resolved 154 / dormant 546`，37 条 `needs_review` |
| 真书规模 | 《三侠五义》64 章 / 510,806 字 / 32 块 / 938,921 token（分析一次） |
