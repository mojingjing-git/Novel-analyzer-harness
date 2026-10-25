---
doc_type: plan
status: done
---

# 运行概览车道显示一致性 + 车道计数准确性修复计划

| 字段 | 值 |
|---|---|
| **日期** | 2026-09-28 |
| **目标** | 修复车道文本格式混乱 + 车道计数可能超过并发池上限的显示 bug |
| **范围** | 后端 WS 事件转发层 + 前端 QueuePage/LaneView/laneRegistry |
| **不做** | 不改 LLM 并发池本身（pipeline Semaphore / final_summary _llm_sem）——它们上限逻辑本身没有 bug |

---

## 0. 背景与根因

### 0.1 问题 1：车道内同时出现两种不同内容

用户截图显示同一时刻车道面板里：
- `ch1381-ch1384`（短格式）
- `开始分析ch1429-ch1432...`（长格式，带前缀）

根因有 **三个来源**，后端两个 + 前端一个：

| 来源 | 路径 | 文案 | 何时出现 |
|---|---|---|---|
| A: WS `block_start` | `pipeline.py:_worker` → `pipeline_events.on_progress` → `block_start_event(range_text=message)` | `"开始分析ch1429-ch1432..."`（人类可读日志格式） | 新车道刚启动时 |
| B: REST `inflight_blocks` 对账 | `pipeline.inflight_blocks()` → `/api/analysis/status` → `reconcileActiveBlocks()` | `"ch1381-ch1384"`（`_fmt_range()` 规范格式） | 5 秒轮询，超过 15s 宽限期的车道被对账覆盖 |
| C: WS `block_done` | `pipeline_events.on_progress` → `block_done_event(range_text=message)` | `"ch1381-ch1384分析完成"`（补进 finished 槽位时显示） | 块完成后 |

具体时序：车道启动时先收到 WS `block_start`（显示 A 格式）→ 15s 内 REST 对账
因 grace 期不覆盖它（`reconcileActiveBlocks` 默认 `graceMs=15000`）→ 15s 后
REST 对账接管，换成 B 格式。结果就是同一车道在不同时刻显示不同文案，
多车道并发时面板上同时可见两种格式。

### 0.2 问题 2：面板显示车道数 > 总并发池上限

`LaneView` 头部显示 `activeBlocks.size / concurrency`，其中：
- `activeBlocks` 由 WS `block_start`/`block_done` 增量记账，
  加上 15s 宽限期内不被 REST 对账覆盖的新块
- `concurrency` 来自 `status.concurrency`（pipeline 配置值）

理论上 pipeline 有 `asyncio.Semaphore(concurrency)` 限流，
`inflight_blocks` 天然 ≤ concurrency。但 `activeBlocks` 是 **WS 增量记账**，
存在两个超限路径：

1. **15s 宽限期窗口内超限**：车道 A 完成但 `block_done` 因 WS 队满被丢弃
   （ProgressHub 慢消费者驱逐策略，token_delta 优先丢，但断线重连期间也可能丢）
   → A 仍在 `activeBlocks` 里占位 → 新的 `block_start` 进来 → size 超限。
   要等 5s 轮询对账才被清掉（但 grace 期内不被清）。
2. **总结合成车道混入**：`summaryLanes.ts` 会把总结阶段的批次
   合成为 id ≥ 900000 的车道放进同一个 `activeBlocks` Map。
   对账时这些被保留（`id >= SYNTHETIC_LANE_ID_BASE`），但它们不占
   pipeline 的 `concurrency` 信号量，所以
   `activeBlocks.size` 会包含分析块 + 总结合成块，可能超过 `concurrency`。

### 0.3 为什么不在前端"格式化"就完事

前端能剥 `"开始分析"` 和 `"..."`，但 `block_done` 的 message 还带 `"分析完成"`，
skipped 带另外的文案。靠前端截断字符串太脆弱。**治本**：让 WS 事件
直接携带结构化的 `range` 字段（与 `inflight_blocks` 同源 `_fmt_range()`），
前端不再需要解析 `message`。

---

## 1. 设计决策

### D1：后端事件加结构化字段，message 保持不变（向后兼容）

`pipeline._emit` 的 START/DONE/FAILED/SKIPPED 事件都补上：
- `range`: `ch{start}-ch{end}` 规范格式（与 `inflight_blocks.range` 完全一致）
- `block_size`: 实际章数（前端可选用于显示 "ch1381-ch1384 (4章)"，本计划不显示）

`message` 字段保留原日志格式不变，因为 `pipeline_events.on_progress`
还要拿它发 `hub.log()`（日志控制台依赖这些人类可读文案）。

同时 `pipeline_events.on_progress` 转发 `block_start_event` / `block_done_event`
时优先使用 `payload["range"]`，旧事件（无 range 字段）才回退到 message。
这保证老客户端/老测试不断。

### D2：前端不再解析 message 文本

`QueuePage.onMessage` 的 `block_start` / `block_done` handler 改为：
```ts
range: String(p.range ?? '')   // 后端保证有；旧后端回落 message → 由 laneRegistry.normalizeRange 兜底
```

新增 `laneRegistry.normalizeRange(raw: string): string`：
- 剥掉可选前缀 `[补跑] `、`开始分析`、尾部 `...`、尾部 `分析完成`、
  尾部 ` 内容审核拦截，已跳过`
- 返回 `chN-chM` 或原字符串（如果已经就是短格式）
- 这是防御旧后端 / 直接调 API 的场景，正常路径不触发

### D3：车道计数改为"分析活跃 + 总结活跃"分开统计

**核心问题**：`LaneView` 当前拿 `activeBlocks.size` 当运行数，
但这个 Map 混合了分析块（< 900000）和总结合成块（≥ 900000）。
对账时合成块被保留但不受分析并发池管理。

修法：`LaneView` 的头部计数改为 `activeBlocks.size`（保持不变，
因为总结合成块确实占 UI 上的车道槽位），但 **对账时分析块超限不再可能**：

1. `reconcileActiveBlocks` 的 grace 期从 **15s → 5s**：
   减少 WS 丢 done 后僵尸块多占的时间窗口，让对账更快纠偏
2. `QueuePage.syncLaneRegistry` 对账前先跑 GC
   （已有逻辑，保持不变）；对账本身用 `inflight_blocks` 整体替换分析块
   （已有逻辑，保持不变）
3. **关键新增**：`reconcileActiveBlocks` 返回前，若分析块（< 900000）
   数量超过 `concurrency` 参数，保留 startedAt 最早的 concurrency 个，
   超出的按 startedAt 倒序截断——这是防御最后一层：
   即使 inflight_blocks 或宽限期逻辑出问题，UI 也永远不会显示超限

### D4：block_done 完成事件也带 `range`（顺带修 finished 槽位文案）

`pipeline._handle_block_outcome` 发 DONE/FAILED/SKIPPED 时补 `range` 字段，
`pipeline_events` 转发时同 START 逻辑。这样 finished 车道占位
显示 `ch1381-ch1384` 而不是 `ch1381-ch1384分析完成`。

### D5：不新增全局 LLM 并发池

用户提到"总并发池包裹"，实际架构是：
- 章节分析：`AnalysisPipeline` 内部 `asyncio.Semaphore(concurrency)`，
  上限 = `analysis.concurrency`（这个范围内 `inflight_blocks` 零丢失）
- 最终总结：`FinalSummaryRunner._llm_sem`，上限 = `summary_concurrency`（或传入值）
- 两者 **不会同时运行**：`SummaryService.start()` 有双向互斥护栏
  （分析运行中不允许手动启动总结；总结运行中不允许启动分析；
  自动总结的 `allow_during_analysis=True` 只发生在队列收尾阶段，此时分析已结束）

所以不存在"分析 + 总结共享一个总池"的场景，不需要新增跨域池。
当前"偶尔车道 > 并发上限"是 **前端记账 bug**（见 0.2），不是后端真的超发。

---

## 2. 任务清单

### Phase 1：后端（预计 0.5 天）

| # | 文件 | 改动 | 验收 |
|---|---|---|---|
| T1.1 | `backend/core/pipeline.py` | `_worker` / `analyze_block_with_progress` 的 START emit、`_handle_block_outcome` 的 DONE/FAILED/SKIPPED emit 都加 `range` + `block_size` 字段 | 单测断言事件 payload 含 `range == "ch5-ch8"` |
| T1.2 | `backend/services/pipeline_events.py` | `on_progress` 转发 `block_start_event` / `block_done_event` 时 `range_text = payload.get("range") or message` | 单测：给 payload 造 `range` 字段 → 事件 `payload.range` 为短格式 |

### Phase 2：前端（预计 0.5 天）

| # | 文件 | 改动 | 验收 |
|---|---|---|---|
| T2.1 | `frontend/src/utils/laneRegistry.ts` | 新增 `normalizeRange()` 纯函数；`reconcileActiveBlocks` 增加 `concurrency` 可选参数，超限时按 startedAt 最早优先截断分析块 | vitest：长格式输入归一化 / 超限截断 / 正常不截断 |
| T2.2 | `frontend/src/pages/QueuePage.vue` | `onMessage` 的 `block_start` / `block_done` 读 `p.range` 而不是 `p.message`；`syncLaneRegistry` 调 `reconcileActiveBlocks` 时传 `concurrency`；grace 从默认 15s 降到 5s | vitest 或手动验证新车道立即显示 `chN-chM` |
| T2.3 | `frontend/src/api/useProgressSocket.ts` | `WSBlockStartPayload` / `WSBlockDonePayload` 的 `range` 注释更新（后端保证结构化，message 仅为日志） | 类型检查通过 |

### Phase 3：测试（预计 0.5 天）

| # | 文件 | 改动 |
|---|---|---|
| T3.1 | `backend/tests/test_queue_service.py` | `test_on_progress_block_start_publishes` 补第二条：payload 带 `range` 字段时事件用 `range` 而非 message |
| T3.2 | `backend/tests/test_pipeline_inflight.py` | 补 `test_start_event_has_structured_range` + `test_done_event_has_structured_range` |
| T3.3 | `frontend/src/utils/laneRegistry.test.ts` | 新增 `normalizeRange` describe 块（短格式 / 长格式 / 前缀 / 尾巴）+ `reconcileActiveBlocks` 超限截断用例 |

### Phase 4：验证

1. `python -m pytest`（463 用例全绿）
2. `cd frontend && npm test`（vitest 全绿）
3. `cd frontend && npm run build`（vue-tsc + vite 构建通过）
4. 手动：启动分析，确认 8 车道显示统一 `chN-chM` 格式，头部计数 ≤ 8

---

## 3. 兼容性与回滚

- 后端 `range` 字段是新增字段，旧前端读 `message` 不受影响（向后兼容）
- 前端读 `p.range ?? ''`，旧后端没 range 时显示空 → `lane.blockId` 兜底；
  但本仓库前后端同发，不存在新旧混搭
- 回滚：整个改动只碰展示层 + 事件转发层，revert 即可，不影响分析逻辑

## 4. 不做（显式排除）

- 不合并分析/总结两个 Semaphore 成一个跨域总池（架构上不需要，且有互斥护栏）
- 不改 `ProgressHub` 的驱逐策略（token_delta 优先丢是正确设计）
- 不改 `inflight_blocks` 的登记时机（LLM 调用窗口是正确的粒度）
- 不改 `_fmt_range()` 的输出格式（`chN-chM` 是唯一规范格式）
- 不做 WS 重连后事件重放（已有 5s REST 轮询对账兜底，收益不值得复杂度）

---

## 7. 实施结果（2026-09-30 归档）

> **约定声明**：本仓库 docs/ 下 PLAN 此前无完成态先例，自本文件起 `status: done` 即完成态。

全部 Phase 1-3 任务落地（commit `b901795`，2026-09-28）；对账明细见
`PLAN-lane-display-closeout-2026-09-29.md` §0。

**回归**：
- 后端 pytest 全绿（2026-09-29 实跑，含 `test_pipeline_inflight.py:138,169`
  structured_range 两例 + `test_queue_service.py:362-370` range 优先用例）
- 前端 vitest 全绿（执行日 84 例；2026-09-30 另一工作会话已扩至 225 例，见 agent.md 10.29）
- `npm run build` 通过，dist 于 09-30 00:10 重建（bundle 实测含 normalizeRange 字面量）

**目验**（2026-09-30，uvicorn :8123 + 真实队列跑《带着仓库到大明》尾部补跑）：
- 活跃车道全程单一 `chN-chM` 格式（`ch1089-ch1092` / `ch1461-ch1464`），无 `开始分析...` 长文案混入
- 头部计数 2/8 → 1/8，恒 ≤ concurrency；REST `inflight_blocks` 与 UI 一致
- 失败块（7 块 MiniMax 1027 内容审核拦截，3 轮补跑）车道即时释放，跑完 `0 / 8 运行中`，无僵尸
- **已知伪影（非 bug）**：续跑补发的 DONE 不带 range（pipeline.py:337 有意限速），
  finished 槽位兜底显示 `block N`——目验中如期出现，与设计一致
