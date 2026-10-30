---
doc_type: plan
doc_version: "1.0"
date: "2026-10-30"
status: done
parent_plan: "docs/PLAN-v2-long-novel-reliability-2026-10-30.md"
parent_plan_version: "2.2"
design_ref: "docs/DESIGN-D01-verdict-transport-2026-10-30.md"
baseline_commit: "5937cb1"
baseline_tests: "892 collected / exit 0"
---

# S1 任务拆解：错误分类及调用结果贯通

本文件把主计划 §4 S1 的 7 条范围拆成**可独立提交、每步仓库常绿**的工作单元。不新增验收项，只做落位；验收编号仍以主计划 §5 为准。

---

## 0. 开工关卡核对

| 关卡 | 状态 | 依据 |
|---|---|---|
| 计划文档定稿 | **通过** | 主计划 v2.2（T01–T47 唯一 47 条）、delta v2.2.1、D-01 设计文档三处 scope 词汇已统一 |
| `scope` 词汇冲突 | **已消除** | 两文档 `CALL/BLOCK/ACCOUNT` 残留 0 |
| S0 基线 | **通过** | `5937cb1` 已提交；892 collected / exit 0；CI 与代理净化已入库 |
| 数据契约依赖 | 不适用 | S1 不碰 KB / 伏笔账本 / 快照，纯错误判定与调用结果传输 |
| 文档分工红线 | 已确认 | 主会话只改代码；本文件的 U7 文档部分由文档侧执行 |

**结论：S1 可以开工。** 主计划 §7 划给 S1 的验收为 T01–T04 + T45。

---

## 1. 拆分原则

1. **一单元一提交，仓库常绿**。任何单元结束后 `python -m pytest -q` 必须全绿。因此"改返回类型"与"改调用者"**不得**跨提交——它们必须落在同一单元。
2. **不做双轨兼容**。主计划 S1 第 5 条明确"不保留只为旧元组服务的兼容入口"，故不设"先返回新类型再逐步迁移"的中间态。
3. **判定只算一次**。异常/流事件 → 单次调用结果 → 客户端重试结果 → analyzer → pipeline → 外层补跑与熔断，每层消费同一份 `ErrorVerdict`，不对显示字符串重新分类。
4. **不顺手扩范围**。C-07（全局信号量统一）、C-14（拆分九任务）、S7 缓存均**不在 S1**，各自归 S8a / S9 / S7。

---

## 2. 工作单元

### U1 · `error_types.py` 骨架（零行为变化）

- **新建** `backend/core/error_types.py`
- 枚举：`ErrorClass`、`Confidence`、`SourceChannel`、`ErrorScope`、`UsageStatus`
- 数据：`ErrorVerdict`（frozen）、`LLMResult`（frozen，`tokens: Optional` + `usage_status`）
- 显式 JSON DTO / 编解码（不依赖 dataclass 默认形状）
- **改动范围**：仅新建文件，不接入任何调用链
- **验收**：新增 `backend/tests/test_error_types.py` —— 枚举取值集合、`ErrorScope` 四值齐全、DTO 往返保真、frozen 不可赋值、`tokens=None → usage_status=UNKNOWN`
- **规模**：S　**依赖**：无

### U2 · 判定策略收敛为单一纯函数

- `backend/core/error_taxonomy.py` 增 `compute_action(cls, scope, policy, retry_ctx) -> str`，`retry_ctx` 含连续审核命中次数、剩余预算与剩余轮数
- 删除 `llm_client.py` 内四处 `_error_action` 拷贝（设计文档记为 `:840` / `:991` / `:1238` / `:1391`），调用点改为消费统一结果
- **不做**：不改 `classify_exception` 的识别能力，不改返回族
- **验收**：T01（四条调用路径对同一错误给出同一 action/scope/等待/费用策略）；新增 `backend/tests/test_verdict_policy.py` 覆盖 `ErrorClass × ErrorScope × 重试上下文`
- **规模**：M　**依赖**：U1

### U3 · 来源通道分级 + C-01 止血

- `classify_exception` 产出可信 `source_channel`（`SDK_ATTRIBUTE` / `ERROR_ENVELOPE` / `STATUS_FALLBACK`）
- `classify_message` 降级为 `TEXT_FALLBACK`：**不得**产生 `stop`，除非命中经审查的错误语境白名单；否则判 `UNKNOWN` 走有限重试，不无限退避
- 普通文本中的 `401` / `1008` / `1026` 不能取得"结构化来源"身份
- **验收**：T02；T45 的 a/b 两段；三条实测负样本必须不触发永久停机、不登记 `_fatal_blocks`：
  - `第 401 章 我叫王二`
  - `…赚了 1008 块`
  - `总字数 1026 万…`
- **规模**：M　**依赖**：U1（可先于 U2 做，但同文件建议顺序执行）

### U4 · 非流式贯通：`chat` / `chat_with_retry` → `LLMResult`

- 两个非流式路径返回 `LLMResult`（含 `verdict`、`content`、`tokens`/`usage_status`、`call_stats`）
- **同批**改造非流式消费者，否则仓库不绿：
  - `backend/core/pipeline.py`（`:1059-1061` 五元组解包）
  - `backend/services/final_summary.py`（`:625` / `:646` / `:667` / `:690`）
  - `backend/services/style_analyzer.py`（`:357`）
  - `backend/services/settings.py`（`:137`）
  - `backend/core/llm_client.py`（`:157` / `:458` / `:465` 等内部解包点）
- **U2 的临时槽在 U4 期间保留，由 U5 删除**：`llm_client.py:110` 的 `_VERDICT_SLOT = contextvars.ContextVar(...)` 与 `_stash_structured_verdict` / `_take_structured_verdict` 是"把结构化结论藏进进程内隐式通道"，与 §2.5「判定随调用结果显式传递、共享客户端不得用 `last_error` 跨请求传判定」相冲突，**只允许存在于 U2 与 U5 之间**。U4 只做非流式，流式结论暂时仍需该槽承载结构化停止，故**不在 U4 删除**。U5 给 `StreamResult` 加 `verdict` 后，结构化结论由结果对象显式携带，`_decide_retry(verdict=...)` 直接读结果对象，此时**必须**删除该槽及 `:876` / `:1252` 的清槽语句、`:996` / `:1119` / `:1345` / `:1465` 的取槽调用。**已于 U5 完成（2026-10-30）：`_VERDICT_SLOT` 与两个配套函数全部删除，`backend/` 全目录 grep 零残留；`StreamChunk` / `StreamResult` 现在各自携带 `verdict`，`chat_auto` 流式分支也把 verdict 搬进 `LLMResult`。**
- 顺带修 `pipeline.py:711-712`：`RETRY_POLICY[_verdict.cls] == "stop"` 改为 `_verdict.is_permanent()`，否则文本 AUTH/BILLING 在 pipeline 层仍会被登记进 `_fatal_blocks`（llm_client 已修好、上游仍在漏判）。
- 顺带修 `pipeline.py:711-712`：`RETRY_POLICY[_verdict.cls] == "stop"` 改为 `_verdict.is_permanent()`，否则文本 AUTH/BILLING 在 pipeline 层仍会被登记进 `_fatal_blocks`（llm_client 已修好、上游仍在漏判）。
- **验收**：T45（非流式段：显示串不含错误码、`Retry-After` 与费用统计正确、DTO 往返）；全量 `pytest -q` 绿
- **规模**：L　**依赖**：U1、U2

### U5 · 流式贯通：`chat_stream` / `chat_stream_with_retry` / `chat_auto` → `StreamResult.verdict` + `LLMResult`

- `backend/core/llm_stream.py` 的 `StreamChunk` / `StreamResult` 追加 `verdict: Optional[ErrorVerdict]`；`StreamChunk` 亦需带完整契约（含 `scope` 与 `retry_after`），不只在终结结果上补齐
- `chat_auto` 的分流结果同步为 `LLMResult`
- **同批**改造流式消费者：`backend/core/analyzer.py`（`:159` 的 `chat_auto` 五元组）、`backend/core/pipeline.py` 的流式分支
- **验收**：T45（流式段：永久错误首次明确停止、外层不补跑）；T03 的流式用例
- **规模**：L　**依赖**：U4

### U6 · 并发不串判定 + usage 未知不宣称为零

- 判定随每次调用独立传递；共享客户端**不得**用单个 `last_error` 字段跨并发请求传递
- 取消或断连后取不到 usage → `usage_status=UNKNOWN`（或估计），**不得**报零消耗
- **验收**：T03（并发成功/失败不串判定）、T04（未知 usage 与已知消耗分别断言）
- **规模**：M　**依赖**：U4、U5

### U7 · S1 收口

- 全量 `python -m pytest -q`（`backend/tests` + `tests` 两处，覆盖 892 基线）
- 同步 `agent.md` / `CHANGELOG.md` / `docs/architecture.md`（**文档侧执行**，主会话不改文档）
- 在 `docs/VERIFY-LOG.md` 追加一行 `doc_check` + 一行 `business_test`
- **验收**：门槛行全部成立（负样本不永久停机 / 永久错误首次明确停止且不补跑 / 并发不串 / 429 审核取消符合策略）
- **规模**：S　**依赖**：U1–U6

---

## 3. 顺序与并行

```
U1 ──┬── U2 ──┐
     └── U3 ──┴── U4 ── U5 ── U6 ── U7
```

- U2 与 U3 都动 `error_taxonomy.py` + `llm_client.py`，**不建议并行**（同文件冲突成本高于并行收益）
- U4 与 U5 是最大两块，必须串行：U5 依赖 U4 建立的 DTO 与消费者形态
- U3 可以提到 U2 之前做——它是**唯一立刻产生线上止血效果**的单元（现状是审核/数字误判直接把块判死）

---

## 4. 三个已知坑（实施时必须显式处理）

1. **旧元组没有兼容入口**：生产侧 8 处位置解包（analyzer 1 / pipeline 1 / final_summary 4 / style_analyzer 1 / settings 1），测试侧约 20 个文件引用 `chat_with_retry` / `chat_stream_with_retry`。**它们必须随 U4/U5 同批改**，不能"回头再改"，也不能靠 `__iter__` 伪装元组（D-01 已裁决排除该方案）。
2. **文本兜底止血的边界**：`classify_message` 不再判 `stop`，不等于"所有纯文本错误都重试"——预算耗尽与轮数上限仍然要停。止血只针对"把正常文本里的数字当成厂商码"这一条路径。
3. **测试里那些锁定旧行为的断言**：改 `LLMResult` 后，断言 `isinstance(result, tuple)` 或五元组解包的测试会失败。这些属于**旧契约测试**，应改写而不是迁就实现；改写时以主计划 §5 的验收意图为准。

---

## 5. 不在 S1 内

| 事项 | 归属 |
|---|---|
| 全局 LLM 信号量统一（现在 `pipeline.py:466` 与 `final_summary.py:534` 是两个独立实例，并行可达 2× concurrency） | S8a |
| 单次调用背九类任务拆分（C-14） | S9（且需 A/B） |
| 复检任务缓存、确定性重放、预算冻结 | S7 |
| 新数据空间、读写格式校验 | S2 |
| 伏笔实体稳定 ID 与截至当前卷视图 | S5 / S6 |

---

## 6. 派工口径

- **代码侧**（主会话）：U1 → U7，按 §3 顺序，一单元一提交
- **文档侧**：U7 的三份文档同步 + `VERIFY-LOG` 记录；S1 完成后回填本文件"实际完成情况"
- **本文件不新增验收项**；若实施中发现需要新验收项，先回主计划 §5 加编号，再在单元内引用

---

## 7. 完成情况（2026-10-30 收口）

**结论：S1 完成，全量 892 → 962 passed / exit 0（父 agent 亲自跑，657.79s）。** 执行中从 7 个单元扩到 9 个，原因是发现了两个原拆解未预见的问题。

| 单元 | 内容 | 结果 |
|---|---|---|
| U1 | `error_types.py` 骨架（422 行）+ 29 例 | 完成 |
| U3 | C-01 止血：文本通道不得 stop + 8 条守护用例 | 完成 |
| U2 | 四处重复决断收敛为 `_decide_retry()`；`_error_action` 改 `is_permanent()` + 18 例 | 完成 |
| **U4** | 非流式对象化：`chat`/`chat_with_retry`/`chat_auto` → `LLMResult`，迁移 8 处生产消费者（含原清单遗漏的 `location_normalizer` / `queue_manager`） | 完成 |
| **U4b** | 拆解外新增：`_fatal_blocks` 在 U4 后失去输入源 → 补 `ErrorVerdict.is_permanent` + verdict 经 `retry_info` 贯通 analyzer→pipeline + 4 例 | 完成 |
| U5 | `StreamChunk`/`StreamResult` 携带 `verdict`；**删除 U2 的 `contextvars` 临时槽** + 2 例 | 完成 |
| U6 | usage 未知语义（`usage_status` 到达 `block_done` / `token_stats`）+ 6 路并发隔离 + `_decide_retry` 类型守卫 + 8 例 | 完成 |
| **U6b** | 拆解外新增：`ErrorClass` 收敛为单一定义（`error_taxonomy` re-export），消除 `is` 静默 False 的地雷；修续跑跳过块 `"tokens": (0,0)` 元组载荷 | 完成 |
| U7 | 收口：`agent.md` §10.36、`CHANGELOG.md`、`docs/architecture.md`、本文件、`docs/VERIFY-LOG.md` | 完成 |

**与原计划的偏差（都要记住的教训）**

1. **U4 之后 `_fatal_blocks` 变成死代码**：文本通道禁止 stop 后，pipeline 手上的错误字符串永远判不出"永久"，于是"补跑剔除"能力静默消失。原拆解把 verdict 贯通只写进了 U4/U5 的"消费者迁移"，没意识到 pipeline 的 fatal 判定本身就是消费者。教训：**改判定语义时，要顺着"谁在消费这个判定"一路走到底，而不是只走一层。**
2. **`ErrorClass` 曾有两份定义**：U1 出于避免循环导入的考虑复制了枚举，`==` 为真、`is` 为假，靠转换函数维系。这是在埋地雷而非解决问题——正解是让 `error_taxonomy` re-export，一步到位。
3. **U2 的 `contextvars` 槽**是"把判定藏进隐式通道"，与 §2.5 直接冲突。它当时确实必要（异常被字符串化后只剩文本通道），但必须设期限：U5 一旦让结果对象携带 verdict 就删除。**允许临时机制，但必须写死移除条件与移除者。**

**留给后续批次的项**

- `service` 层费用聚合尚未按 `usage_status` 分桶，"未知消耗的 0"仍会进累加器（S9 规模治理一起做）
- 429/500 重试耗尽后 verdict 降级为 `text`，"重试耗尽的 429"与"文本 429"从此不可区分（方向安全，非阻塞）
- `analyzer.analyze_chapter` 对外 `(result, tokens, retry_info)` 形状未变，`tokens` 在未知时仍是 `(0,0)`，由 `retry_info["usage_status"]` 表达未知（对外契约留到 S2 数据契约改造时统一）
