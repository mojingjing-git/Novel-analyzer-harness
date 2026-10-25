---
doc_type: plan
status: done
---

# 车道显示修复收尾归档（PLAN-lane-display 的对账与关闭）

| 字段 | 值 |
|---|---|
| **日期** | 2026-09-29（2026-09-30 按独立复审修订） |
| **目标** | 核实 `PLAN-lane-display-concurrency-2026-09-28.md` 全部任务已落地，补齐 Phase 4 残余验证，归档（status: done）+ 补 CHANGELOG / agent.md 记录 |
| **范围** | 文档 + dist 新鲜度核验 + 目验；**不改任何已实现的车道逻辑** |
| **不做** | 不重写车道代码；不处理 62 文件的 git 提交（1 号事项） |

---

## 0. 背景与对账结果

原 plan（`docs/PLAN-lane-display-concurrency-2026-09-28.md`）status 仍为 draft，
但其实现已全部落在未提交的工作树中。对账表经独立复审逐条实证（2026-09-30）：

| 原任务 | 状态 | 证据 |
|---|---|---|
| T1.1 pipeline 事件补 `range`/`block_size` | ✅ | 六处 emit 全带（pipeline.py:463-469 worker START、:713-718 同步 START、:769-780 DONE、:787-792 SKIPPED、:794-800 FAILED、:521-527 异常 FAILED）。**注**：续跑补发 DONE（:337-345）不带 range/block_size——有意限速设计，前端显示 `block N` 兜底，见 §1-D3 |
| T1.2 pipeline_events 优先取 `range` | ✅ | `pipeline_events.py:133`（START）与 `:145`（DONE/FAILED/SKIPPED）均 `payload.get("range") or message` |
| T2.1 `normalizeRange` + 超限截断 | ✅ | `laneRegistry.ts:36-46`（剥 `[补跑]/[重试]` 前缀、`开始分析`、`...`、`分析完成/失败/异常`、`内容审核拦截，已跳过`）+ `:108-117`（startedAt 升序保留最早 concurrency 个） |
| T2.2 QueuePage 读 `p.range` + grace 5s + 传 concurrency | ✅ | `QueuePage.vue:170-171` `reconcileActiveBlocks(kept, inflight, Date.now(), 5000, currentConcurrency.value)`；`:258` currentConcurrency 来自 `status.concurrency` |
| T2.3 useProgressSocket payload 注释 | ✅ | `:22-23,32-33` |
| T3.1 test_queue_service 的 range 用例 | ✅ **存在** | `test_queue_service.py:362-370`：payload 同时含 message 与 range，断言发布事件 `range == "ch9-ch10"`（结构化优先）。此前 grep 未命中属命名差异（不叫 structured_range），**无需补测试** |
| T3.2 后端 structured_range 两测试 | ✅ | `test_pipeline_inflight.py:138,169` |
| T3.3 laneRegistry vitest | ✅ | 实数 18 个 `it()`（normalizeRange 5 + GC 6 + reconcile 7） |
| Phase 4.1 `python -m pytest` | ✅ | 2026-09-29 实跑全绿 |
| Phase 4.2 vitest | ✅ | 2026-09-29 实跑全绿（当时 84 例；另一会话 10.29 已扩到 225，执行日以实跑为准） |
| Phase 4.3 `npm run build` | ✅ | dist 已于 09-30 00:10 重建（见 D2） |
| Phase 4.4 手动目验 | ⬜ | 本 plan 唯一实质残余（§1-D3） |

**结论**：代码实现完整、测试齐备，缺的只是目验 + 文档归档。

## 1. 设计决策

### D1：归档方式 = plan 内加"实施结果"章节 + status 翻转

车道修复体量小（展示层 + 事件转发层），不写独立 DELIVER 文档。在原 plan
文末加 `## 7. 实施结果`，frontmatter `status: draft → done`。
**注**：docs/ 下现有 6 个 PLAN 均为 `status: draft`、无完成态先例——
本次在原 plan §7 中写明"done 即本仓库的完成态约定"，确立惯例。

### D2：dist 已于 09-30 00:10 重建且含新代码——从"必须重建"降级为"新鲜度核验"

复审实测：`frontend/dist/assets/index-Kib1V3oi.js` mtime = Sep 30 00:10，
`find frontend/src -newer dist/index.html` 为空（无源码比 dist 新），
bundle 内 grep 到 `开始分析`×2、`内容审核拦截，已跳过`×1（normalizeRange
字面量，laneRegistry.ts:41,44）——**车道代码已在 dist 里**（另一会话构建）。
T2 因此改为核验动作：`npm run build` 或证明 dist mtime 晚于全部 src mtime。

### D3：目验路径改小范围起跑，规避续跑补发的 `block N` 伪影

**已知伪影**：断点续跑补发的 DONE 不带 range（pipeline.py:337-345 有意限速），
finished 槽位兜底显示 `block N`（queueFeed.ts:82）。重跑《我的 26岁女房客》
会触发 886 章全量 replay，finished 列表出现成片 `block N`——按原 §4
"格式混乱即停工升级"的字面标准会被误触发。

修法（两选一，优先 ①）：

1. **任选一本书小范围起跑几分钟**（start/end chapter 限一小段，并发 ≥ 2）：
   无 replay 干扰，活跃车道与 finished 槽位全程 `chN-chM`，目验最纯净。
   原方案"重跑失败块一举两得"的收益与目验纯净度冲突时，优先目验。
2. 若仍走失败块重跑：目验对象**仅为活跃车道格式 + 头部计数 ≤ concurrency**，
   finished 槽位的 `block N` 为已知兜底现象，不触发 §4 停工规则。

## 2. 任务清单

| Phase | # | 动作 | 验收 |
|---|---|---|---|
| 1 | T1 | ~~核实 T3.1~~ 已核实存在（§0 表格），跳过 | — |
| 2 | T2 | dist 新鲜度核验：`find frontend/src -newer frontend/dist/index.html` 为空则通过；否则 `npm run build` | 核验通过或构建 0 错 |
| 3 | T3 | 按 D3 方案目验：活跃车道全程 `chN-chM` 单一格式；头部计数 ≤ concurrency；完成释放正常；（如走方案②）`block N` 占位不判为失败 | 文字记录存档 |
| 4 | T4 | 原 plan 文档：加 `## 7. 实施结果`（含 D1 的约定声明），`status: done` | — |
| 5 | T5 | CHANGELOG `[Unreleased] - 2026-09-28` 段内按既有 `---` 分隔样式追加第三条「车道显示一致性 + 计数准确性修复」 | — |
| 6 | T6 | agent.md 加 **`### 10.30 车道显示修复`**（10.29 已被另一会话的"前端测试深度加强"占用，见 agent.md:1182），格式按 §10 写作模板；核对 §2/§6 中 laneRegistry 行数描述 | — |

## 3. 验收标准

1. 原 plan 全部 Phase 任务有 ✅ 证据（本文件 §0 表格即终版）
2. dist mtime 晚于全部前端源码 mtime（或本次重建完成）
3. pytest / vitest / build 三绿（记录执行日实跑数字，不沿用历史数字）
4. CHANGELOG 与 agent.md（10.30）均有车道修复记录

## 4. 兼容性与回滚

- 纯文档 + 核验 + 目验；无行为改动，无回滚需求
- T3 目验发现活跃车道格式仍混乱或计数超限 → **停下升级为 bug**（说明对账有
  遗漏），不在本 plan 内顺手修；`block N` finished 占位不在此列（D3 已排除）

## 5. 协调注意

- 另一会话的测试加强已落地（agent.md 10.29，84→225 用例 + coverage 基建），
  工作树新增大量测试文件；执行前 `git status` 全量对账一次
- T3 需要起后端跑分析：启动前确认端口空闲
  （`tail %LOCALAPPDATA%\NovelAnalyzer\run.log` 看最后启动时间 +
  `netstat -ano | findstr <端口>`），避免与另一会话的 uvicorn/pytest 撞车

---

## 6. 独立复审记录（2026-09-30）

复审结论：**修订后通过**。§0 对账表所有 ✅ 声明经逐条实证全部属实；
2 阻塞 + 3 重要 + 4 建议，已全部并入上文：

- 阻塞 1（agent.md 10.29 已被占用）→ T6 改 10.30
- 阻塞 2（§0 基线过期：dist 已重建含新代码、另一会话工作已落地）→ §0 表格、
  D2、T2、§5、验收 2 全部按新事实改写
- 重要 1（`block N` 伪影会误触停工规则）→ D3 重写目验路径
- 重要 2（T1.1 过度声明）→ 加注"续跑补发 DONE 除外"
- 重要 3（T3.1 实为存在）→ 翻 ✅ 并写入证据，防 executor 误补重复测试
- 建议采纳：端口检查落成可执行命令（§5）；回归数字以执行日实跑为准（验收 3）；
  status 约定声明（D1）；CHANGELOG 并入 09-28 段（T5）
