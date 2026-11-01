# PLAN：2.0 阶段 0-3（v2）—— 定位策略修正：来源章号 + 块范围

> 承接 [plan-stage0-2-review-2026-11-02.md](./research/plan-stage0-2-review-2026-11-02.md)（阶段 0-2 复审）
> 与 [plan-stage0-3-review-2026-11-02.md](./research/plan-stage0-3-review-2026-11-02.md)（本计划的 v1 复审）。
> **v1 的方案（「回号优先」）已被自己的复审推翻**，改为「来源章号 + 块范围」。

---

## 1. v1 方案为什么被推翻

v1 主张：用文件内解析出的回号建「回号 → 文件列表」索引，采集侧章号 → 候选文件 → 逐个找。

复审用**原文上下文**核实了 4 条关键伏笔，确认 v1 的方向有效（回号优先命中 1662/1669 = 99.58%），
但指出三个硬伤，本计划全部采纳：

| P1/P2 | 复审结论 | 本计划的处置 |
|---|---|---|
| **P1-1** 同回号多文件"升序取第一个"会**跨卷截胡**：`"原来是你！"` 应在卷五文件 806，却被卷二文件 242 抢走；而两者回号都是 12，差值为 0 → 置信度机制报不出来 | **不用"回号索引 + 升序"**。改用**来源章号 + 块范围**（见 §2），候选范围唯一，不存在升序取舍 |
| **P1-2** 改 `locate_in_units` 返回值会破坏 `owner, _offset = hit` 的解包（生产 + 脚本 + 测试三处） | **不改现有签名**。新增独立函数 `locate_with_mode()` 返回 NamedTuple；旧函数保留原样 |
| **P1-3** `locate_mode` / `low_confidence` **没有数据载体**（`CandidateVerdict` 是 frozen dataclass，没有这两个字段；未进入候选的条目更没有 verdict） | 载体放在**候选级旁路**：`RecheckOutcome.locate_records: dict[candidate_id, dict]`，导出明细时并入。不污染判定数据模型 |
| **P2-1** 低置信度判据"回号差 > 3"**逻辑击穿**：回号优先时差值恒为 0（实测 0 条），兜底时差值几十上百 → 阈值无意义 | 置信度改判为：**兜底命中（fallback）** 或 **命中文件超出候选范围**（跨卷跳跃） |
| **P2-2** 在调用方层层加参数（`build_round_index` 传进 `run_recheck_v2`）破坏封装 | 只加**一个**参数：`source_blocks`（候选块范围），其余在 `run_recheck_v2` 内部派生 |
| **P3** "兜底 ≤ 5%" 太松（实测 0.42%） | 收紧为：**兜底命中 ≤ 1%**，且每条兜底都要能人工核对 |

## 2. 新方案：来源章号 + 块范围

### 2.1 实测依据（本次新增）

1. **采集是 4 章合并分析**（`block_size=4`）。章节结果文件 `chapter_N_result.json` 的 N 是**源书章号**；
   伏笔的 `anchor_quote` 可能落在块内**任一章**（N..N+3）。
   实测 225 个结果文件各取首条伏笔：**100% 落在 [N, N+3] 内**，
   偏移分布：N 本身 124、+1 49、+2 26、+3 25。
   （偏移**不单调**——chapter_13 偏 +3、chapter_113 偏 +1——这排除了"文件编号错位"解释，
   证实了"块范围"解释。）
2. **账本里的 `anchor_chapter` 是回号，不是源书章号**：它是"合并块内标题反查出的回号"
   （见阶段 0-2）。所以账本条目**不能**直接用块范围定位——这也解释了为什么按账本章号定位只有 21.9%。
3. **块范围能唯一确定候选文件**：在 [N, N+3] 内，实测 364/366 条**唯一命中**（仅 2 条多处）。

### 2.2 定位链（三段）

```
① 候选文件 = [source_chapter, source_chapter + block_size - 1]
   （source_chapter = 该伏笔来自哪个章节结果文件 = 源书章号）
   → 在其内逐字找引文；唯一命中即采用（无排序取舍，不存在跨卷截胡）

② 未命中 → 全书纯字面取最早（兜底，标记 fallback）

③ 仍未命中 → 记 absent（引文不在书里）
```

### 2.3 `source_chapter` 怎么来（AGY P1-3 的正解）

账本条目当前**没有**源书章号，必须补上：

- `ForeshadowItem` 新增字段 `source_chapter: int = 0`（**可选字段，向后兼容**：旧账本读到 0）；
- 重建/装载账本时从 `chapter_N_result.json` 的文件名取 N 写入；
  去重合并时取**最早来源**的那个 N（与 `first_seen_batch` 一致）；
- 落盘后仍在账本里 → 下次复检不必重建。

**代价**：账本 schema 要动（加一个可选字段）。这是 AGY P1-3 逼出来的必然结论——
定位提示必须有地方存，否则复检永远只能猜。

## 3. 具体改动

| # | 文件 | 改动 |
|---|---|---|
| 1 | `backend/utils/foreshadow_ledger.py` | `ForeshadowItem` 加 `source_chapter: int = 0`（`to_dict`/`from_dict` 同步）；`SCHEMA_VERSION` 5 → 6（旧 v5 仍可加载，读到 0 走降级） |
| 2 | `backend/utils/text_utils.py` | 位置契约扩展：伏笔元组在现有第 6..10 位之后追加 `source_chapter`（第 11 位）。**必须同时改 `deduplicate_foreshadows` 的读与写两处**，并保证旧调用方（只传 10 位）仍可用 |
| 3 | 账本重建处（`scripts/measure_recheck_v2_pipeline.py` 与生产侧加载结果处） | 从 `chapter_N_result.json` 的文件名取源书章号 N，写入 `source_chapter` |
| 4 | `backend/core/text_units.py` | 新增 `locate_with_mode(units, quote, candidate_range) -> LocateHit(unit, offset, mode)`；`mode ∈ {range, fallback, absent}`。**`locate_in_units` 保持原签名与返回值不变** |
| 5 | `backend/services/recheck_v2.py` | 定位改用 `locate_with_mode`；候选范围来自 `item.source_chapter` 与 `block_size`；命中模式写进 `outcome.locate_records` |
| 6 | `backend/services/recheck_v2.py` | `RecheckOutcome.locate_records: dict[str, dict]`（candidate_id → {unit, mode, source_chapter, low_confidence}）；`dump_recheck_detail` 导出 |
| 7 | `backend/services/final_summary.py` / `scripts/measure_recheck_v2_pipeline.py` | 传入 `block_size`（配置项 `analysis.block_size`，默认 4） |
| 8 | `scripts/check_anchor_identity.py` | 断言改成**四分母**：范围命中 / 兜底命中 / 确凿不存在 / 存在但定位失败（必须为 0）；另报兜底清单 |
| 9 | `backend/tests/test_text_units_identity.py` | 补：范围唯一命中、范围外走兜底、兜底标记 low_confidence、旧账本（无 source_chapter）降级、1.x 行为不变 |

## 4. 验收

| 项 | 标准 |
|---|---|
| 范围命中率 | 1669 条候选中，**范围命中 ≥ 99%**（实测依据：225 样本 100%） |
| 兜底率 | **≤ 1%**，且兜底清单可人工逐条核对 |
| 不存在 | 确凿不存在的条数与引文事实一致 |
| 定位失败 | **必须为 0** |
| 截胡 | 同回号多文件的候选（12 条）逐条核对：命中的文件必须落在 [N, N+3] 内 |
| 旧账本 | 无 `source_chapter` 的旧账本仍可跑（降级到兜底），并产出降级告警 |
| 1.x 不变 | `unit_noun="章"` 时行为与阶段 0-2 一致 |
| 全量门禁 | `scripts/run_tests.py` 全绿 |

## 5. 风险与回滚

| 风险 | 缓解 |
|---|---|
| `block_size` 配错 → 候选范围不对 | 范围来自配置 `analysis.block_size`；范围外命中一律记 fallback（可发现） |
| `source_chapter` 缺失（旧账本） | 降级到兜底 + 告警；不硬报错 |
| 位置契约扩位影响旧调用方 | 读取处按"有第 11 位就用、没有就当 0"处理；写处统一写满 11 位 |
| 改账本 schema 影响下游 | `SCHEMA_VERSION` 6，旧 v5 JSON 仍可加载（`from_dict` 容错） |
| 回滚 | 改动集中在 `text_units.py`、`recheck_v2.py`、账本与 `text_utils` 两处；`git revert` 即可 |

## 6. 不做什么

- 不改采集侧的结果模型（`models/analysis_result.py` 不动）——源书章号由**文件名**提供。
- 不引入自动降级门禁（弱模型盲复核精确率 ~80%，不能当门禁）。
- 不改 `EvidenceIndex` 的取窗半径与检索语义。
- 不重跑整本（阶段 0 收口时统一跑）。
- 不实现"同回号多文件升序"（已被证明会截胡）。
