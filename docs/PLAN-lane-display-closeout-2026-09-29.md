---
doc_type: plan
status: draft
---

# 车道显示修复收尾归档（PLAN-lane-display 的对账与关闭）

| 字段 | 值 |
|---|---|
| **日期** | 2026-09-29 |
| **目标** | 核实 `PLAN-lane-display-concurrency-2026-09-28.md` 全部任务已落地，补齐 Phase 4 残余验证，归档（status: done）+ 补 CHANGELOG / agent.md 记录 |
| **范围** | 文档 + dist 重建 + 至多 1 个缺失测试；**不改任何已实现的车道逻辑** |
| **不做** | 不重写车道代码；不处理 62 文件的 git 提交（1 号事项，另一会话负责） |

---

## 0. 背景与对账结果

原 plan（`docs/PLAN-lane-display-concurrency-2026-09-28.md`）status 仍为 draft，
但其实现已全部落在未提交的工作树中。2026-09-29 逐项对账：

| 原任务 | 状态 | 证据 |
|---|---|---|
| T1.1 pipeline 事件补 `range`/`block_size` | ✅ | `git diff backend/core/pipeline.py`：START/DONE/FAILED/SKIPPED emit 均已补 |
| T1.2 pipeline_events 优先取 `range` | ✅ | `range_text = payload.get("range") or message` 已落 |
| T2.1 `normalizeRange` + 超限截断 | ✅ | `laneRegistry.ts:36-46`（normalizeRange）+ `:112-117`（startedAt 最早优先截断），测试 11→18 例 |
| T2.2 QueuePage 读 `p.range` + grace 5s + 传 concurrency | ✅ | `QueuePage.vue:170-171`：`reconcileActiveBlocks(kept, inflight, Date.now(), 5000, currentConcurrency.value)` |
| T2.3 useProgressSocket payload 注释 | ✅ | `:22-23,32-33` range 结构化 + message 旧后端兜底注释 |
| T3.2 后端 structured_range 两测试 | ✅ | `test_pipeline_inflight.py:138,169`（test_start/test_done_event_has_structured_range） |
| T3.1 test_queue_service 的 range 用例 | ⚠ 待核实 | grep 未命中 `structured_range`，可能以其他命名存在；核对后缺则补 |
| T3.3 laneRegistry vitest | ✅ | 18 例（normalizeRange 5 + GC 6 + reconcile 7） |
| Phase 4.1 `python -m pytest` | ✅ | 2026-09-29 实跑全绿（exit 0） |
| Phase 4.2 vitest | ✅ | 2026-09-29 实跑 84/84 |
| Phase 4.3 `npm run build` | ⬜ 未验证 | 本 plan 执行 |
| Phase 4.4 手动 8 车道格式统一 | ⬜ 未验证 | 见 §2 说明：昨晚真书跑的是旧 dist，需重建后目验 |

**结论**：代码实现完整，缺的只是收尾动作。

## 1. 设计决策

### D1：归档方式 = plan 内加"实施结果"章节 + status 翻转，不另写 DELIVER 文档

车道修复体量小（展示层 + 事件转发层），不满足独立 DELIVER 报告的体量
（对照：`DELIVER-splitter-i18n` 对应的是多 Phase 大改）。在原 plan 文末加
`## 7. 实施结果`（改动摘要 / 回归数字 / 遗留），frontmatter `status: draft → done`。

### D2：dist 必须重建，否则桌面端跑不到新前端

昨晚 886 章真书验证的是**旧 dist**（车道 UI 未参与）。`run_desktop.bat`
robocopy 的是 backend + desktop.py，前端始终用 `frontend/dist/`——
不重建 dist，Phase 4.4 的目验无意义。构建后需重启桌面端再目验。

### D3：目验用短书 / 现有书重跑，不新起长跑

目验只需并发 ≥ 2 的几分钟运行（观察车道格式统一 + 头部计数 ≤ 并发值）。
可用现有书的失败块重跑（`《我的 26岁女房客》` 有 1 个 failed 块），
顺带把该书的失败块补掉，一举两得。

## 2. 任务清单

| Phase | # | 动作 | 验收 |
|---|---|---|---|
| 1 | T1 | 核实 T3.1：`grep -n "range" backend/tests/test_queue_service.py`；无则按原 plan 补一条「payload 带 range 时事件用 range」用例 | 测试存在且绿 |
| 2 | T2 | `cd frontend && npm run build`（vue-tsc + vite） | 0 错 |
| 3 | T3 | 重启桌面端 → 重跑失败块 → 目验：车道全程 `chN-chM` 单一格式；头部计数 ≤ concurrency；完成释放正常 | 截图存档或文字记录 |
| 4 | T4 | 原 plan 文档：加 `## 7. 实施结果`，`status: done` | — |
| 5 | T5 | CHANGELOG `[Unreleased]` 补「车道显示一致性 + 计数准确性修复」条目（与桌面无控制台 / DOCX 并列，含根因一句话 + 三层防御摘要） | — |
| 6 | T6 | agent.md 加 `### 10.29 车道显示修复`（格式按 §10 写作模板：改动/原因/回归），并核对 §2/§6 中 laneRegistry 行数描述 | — |

## 3. 验收标准

1. 原 plan 7 个 Phase 任务全部有 ✅ 证据（本文件 §0 表格升级为全 ✅）
2. dist 为最新（`frontend/dist/assets` mtime 晚于本次 build）
3. pytest / vitest / build 三绿
4. CHANGELOG 与 agent.md 均有车道修复记录（后续 session 不再误判其为"未实施"）

## 4. 兼容性与回滚

- 纯文档 + 构建 + 至多 1 个测试；无行为改动，无回滚需求
- 若 T3 目验发现车道格式仍混乱：**停下来升级为 bug**（说明对账有遗漏），
  不在本 plan 内顺手修——保持"收尾归档"边界

## 5. 协调注意

- 另一会话正在加测试（可能触碰 `test_queue_service.py` / `laneRegistry.test.ts`），
  T1 实施前先 `git status` 对账
- T3 需要启动桌面端跑分析：确认另一会话没有正在跑 pytest/uvicorn 占用端口
