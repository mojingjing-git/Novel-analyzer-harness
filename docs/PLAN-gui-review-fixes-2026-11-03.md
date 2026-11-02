# PLAN：GUI 对账速修——复检 token 记账 + 阶段事件/过时文案（v2，经复审修订）

> 输入：GUI↔后端全量对账报告（2026-11-03）的 A1 / B1 / B3 三项。
> v2：经 AGY 复审修订——采纳其全部 P1/P2/P3（记账时机颠倒、V2 漏发 phase 事件、测试脱靶、文案方位相反、QueuePage 映射分叉）；阶段名由「带证据复检」修订为**中性的「伏笔复检」**（`phase` 键被 V2 与旧路径共用，旧路径确实不带证据，改「带证据」会替它说谎）。
> A2–A8、B2、B4–B5 **不在本计划内**（见 §6 不做）。

---

## 1. 事实依据（已核实）

| # | 现象 | 证据 |
|---|---|---|
| **A1** | V2 复检（现在是默认路径）的 token 消耗**不进任何账**：`_record_tokens("recheck", …)` 只有旧路径在调（`final_summary.py:750`）；V2 的 `_call_model`（`:1556-1567`）把 `res.tokens` 丢弃。前端展示位早已就绪（`summaryProgress.ts:80` `recheck: '全书复检'`），永远等不到数据——GUI 跑完 280 次调用，token 统计里看不到复检花费 | final_summary.py:750 / :1556；summaryProgress.ts:80 |
| **B1** | 设置页「复检批大小」(`foreshadow_recheck_batch_size`=40) 的 tooltip 仍是旧路径语义（"全书伏笔复检时每次调用携带的活跃伏笔数"），与新加的「判定批大小」(`recheck_v2_batch_size`=6) 并排无区分——V2 默认开启后，用户调 40 实际不生效 | SettingsPage.vue:740 |
| **B3** | V2 已是默认路径，阶段名仍叫「全书伏笔复检」：后端 phase_label（`final_summary.py:1693`）、前端阶段条（`summaryProgress.ts:10,18`）、token 类别标签（`:80`）、QueuePage 自己的映射（`QueuePage.vue:127`） | 见左 |

已核的两个技术前提（决定 A1 写法）：

1. `_record_tokens`（`final_summary.py:771-800`）对 `None` / 非二元组直接 return——usage 未知的调用天然不计，与 summary/final 等既有类别口径一致；
2. `LLMClient` 本身不携带记账回调（runner 里 `self._llm = LLMClient(_api_cfg)`，`:606`），token 记账**只发生在各调用点**——在 `_call_model` 里补一行是唯一记账点，**不会双计**。

---

## 2. 改动

| # | 文件 | 改动 |
|---|---|---|
| **C-1** | `backend/services/final_summary.py`（`_run_recheck_v2._call_model`） | **在 `if not success: raise` 之前**补 `self._record_tokens("recheck", getattr(res, "tokens", None))`。复审修正：失败调用（校验失败/流中断重试耗尽）的 `res.tokens` 仍持有已扣费 token，且既有 4 处调用点（`:687/:708/:731/:754`）全部是**先记账后判成败**——放在 raise 之后会丢失败账。`_record_tokens` 对 None/非二元组直接 return（`:773`），零安全风险 |
| **C-2** | `frontend/src/pages/SettingsPage.vue`（旧批大小行）+ `docs/configuration.md` 伏笔复检节 | label「复检批大小」→「旧复检批大小」，tooltip 前置「仅当关闭上方『启用带证据复检』时生效（旧的全书复检路径）；带证据复检的批大小见**上方**复检分组的『判定批大小』」（复审修正：V2 卡片在设置页位于其上方，原文案写"下方"方向反了）。configuration.md 伏笔复检节按现状重写（V2 默认开、32768、补 5 个新字段、"设置页没有输入框"一句作废） |
| **C-3a** | `backend/services/final_summary.py`（`_run_recheck_v2`，调 `run_recheck_v2` 之前） | **补发 phase 事件**（复审 P1：V2 从不发 phase，前端阶段条整个复检期停在 batch）：`{"type": "phase", "phase": "recheck", "phase_label": "带证据伏笔复检", "message": "[阶段2/4] 带证据复检…"}`。不带 `total_batches`（V2 是波次计账不是批索引，LaneView 合成块需要批索引；`applySummaryProgress` 对 recheck 在 totalBatches=0 时只做阶段切换不清账，可接受） |
| **C-3b** | `frontend/src/utils/summaryProgress.ts`（`:10/:18/:80`）、`frontend/src/pages/QueuePage.vue`（`:126-130`） | 共享展示标签统一为中性「伏笔复检」（`phase` 键两路共用，中性名对两路都准确）；token 标签 `recheck` 同改。QueuePage 删除本地 phase 映射，改用 `phaseLabelOf`（复审 P3：两份映射已出现措辞分叉） |
| **C-3c** | 旧路径 `final_summary.py:1697-1699` **不改** | 旧路径确实是不带证据的全书复检，保持「全书伏笔复检」准确（对复审 P3 建议的有意不采纳，理由即中性命名理由的镜像） |
| **C-4** | `backend/tests/test_recheck_v2_wiring.py` | 按复审 P2 改为**接线测试**：真实 `FinalSummaryRunner`（`test_recheck_guard.py` 的 `_make_runner` 形态）+ monkeypatch `final_summary.run_recheck_v2` 截获 `call_model` + fake `_llm.chat_auto` 返回带 tokens 的结果 → 断言 (a) 成功调用后 `get_token_stats()["recheck"]` 累计；(b) `success=False` 的调用**抛 RuntimeError 且记账已发生**（锁死"先记账后抛"的顺序，防后人改回） |

**关于 C-4 的诚实边界**：`_run_recheck_v2` 用 monkeypatch 掉 `run_recheck_v2` 与 `_collect_chapters` 走最小路径，`run_recheck_v2` 内部（波次/补问/重试）已有自己的测试覆盖；本测试锁的是"记账发生在 runner 层、且先于 raise"这两个会被无意改掉的点。

---

## 3. 验收

| 项 | 标准 | 判定方式 |
|---|---|---|
| 门禁 | `pytest` 全绿（≥1710，含 C-4 两例）、`vitest` 全绿、`vue-tsc + vite build` 通过 | `run_tests.py` / `npm test` / `npm run build` |
| 记账 | 成功与失败调用都进 recheck 账、先记账后抛 | C-4 接线测试 |
| 阶段事件 | V2 复检开始即发 `phase=recheck` → 总结页步骤条进入复检步 | C-4 截获 `_emit_progress` 断言 + GUI 实跑 |
| 文案 | 设置页两个批大小可区分且方位正确；configuration.md 伏笔复检节与现状一致 | 人工核对 |
| GUI 生效 | 重启 GUI 后实跑：token 行出现「伏笔复检 入X 出Y」、步骤条走复检步 | 用户确认 |

---

## 4. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R-1 | token 双计 | 已核：LLMClient 无自带回调、记账只在调用点；C-1 是唯一新增记账点（复审重点 2 亦确认 `_call_model` 是唯一入口，无绕行路径） |
| R-2 | usage 未知的调用不计入 → 统计偏低 | 与 summary/final/style 既有口径一致，**本计划不改**该口径 |
| R-3 | 阶段事件/标签改动碰坏前端逻辑 | `phase` 键与权重不变；`applySummaryProgress` 对 recheck+totalBatches=0 只做阶段切换（已读源码确认）；C-4 截获 emit 断言事件形状 |
| R-4 | 改完不生效的"假象" | GUI 在启动时才 robocopy 后端代码、dist 是构建期产物——本计划完成 build 后**不自动重启**用户窗口，交付说明里写明需重启 |

---

## 5. 工作量

约 40 分钟（三处小改 + 1 个测试 + 两条门禁 + build）。

## 6. 不做

- A2 复检逐条判定查看页（数据在 `recheck_verdicts.json`，值得一个独立页面，另立计划）；
- A3 切章保存按钮、A4 手动登记外部书、A5 队列整包写回/外部目录扫描；
- A6 知识库限制 GUI + D-9 三项配置；A7 锚点开关；A8 streaming/reasoning_effort 入口；
- B2 json_mode 二态化与本地通道提示；B4 新版空间「未接线」横幅；B5 `max_retries` 清理。
