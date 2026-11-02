# PLAN：阶段 0 收口——判定落账修复、真书回归、打开 V2 复检

> 输入：`docs/PLAN-2.0-merge-2026-11-02.md`、`docs/PLAN-2.0-stage0-2026-11-02.md`、
> `docs/data-contract-2.0.md`、`docs/issues.md`、`docs/data/anchor-identity-dfm.json`。
> 复审：AGY 只读复审（2026-11-02 19:44），4 条 P1 全部采纳并入本稿。

---

## 1. 已定决策

| # | 决策 | 定值 | 来源 |
|---|---|---|---|
| **G-1** | 复检判定模型 | `codex/gpt-6-luna`，思考档位 `low` | 用户指定 |
| **G-2** | 全局输出上限 | `api.max_tokens` 130000 → **65536** | 用户指定 |
| **G-3** | V2 复检开关 | 本轮打开；**打开动作在段 E**，即小批与生产端到端都通过之后 | 用户指定 + 复审 P1-2 |
| **G-4** | 空判定状态表达 | 维持 `PARTIAL` + 明确 `status_reason` + ERROR 日志，**不新增 FAILED 状态** | 复审 §7-1 |
| **G-5** | 判定通道配置 | 独立配置项 `analysis.recheck_base_url`，不做按前缀的隐式路由 | 复审 §7-2 |
| **G-6** | 三语料范围 | 阶段 0 只做三语料的**离线**切分/定位断言；耗时 LLM 复检拆到阶段 1 | 复审 §7-3 |

---

## 2. 症结：`checked = 0` 的完整因果链（已用日志证实）

1. `.tmp_stage0_recheck/run2.log` 自己打印的通道：
   `模型 https://api.minimaxi.com/v1 / MiniMax-M3.1-Flash`，但
   `复检模型：codex/gpt-6-luna` → **网关专用模型名打到了官方 MiniMax 通道**。
2. 日志里 2523 行命中 `400`：
   `invalid params, unknown model 'codex/gpt-6-luna' (2013)`，官方端每次请求都拒绝。
3. `chat_stream_with_retry` 把 400 捕获成 `StreamResult(success=False, content="")`，**不抛异常**。
4. `call_model`（`measure_recheck_v2_pipeline.py:309-318`、`final_summary.py:1498-1502`）
   只取 `res.content or ""`，**丢掉 success 与 error**。
5. 于是 `run_recheck_v2` 的 `isinstance(result, BaseException)` 永远为假 →
   `failed_batches: 0`、`calls: 280`、`checked: 0`、1671 条 `not_checked`。

**结论**：不是判定质量问题，是"请求被拒 + 失败被静默"双重缺陷。
这才是阶段 0 收口的唯一硬阻塞。

### 2.1 生产侧同类隐患

`final_summary.py:1490-1496` 只 `replace(cfg.api, model=recheck_model)`，
**不换 base_url**；一旦 `recheck_model` 是网关专用名，生产路径会 100% 重演该事故。

### 2.2 两次冒烟为什么看起来"卡死"

2523 行 400 全部走了退避重试（`max_retries=5` + 指数退避 + 300s 超时），
加上 `python … | Select-String` 让 stdout 变块缓冲、`job_output` 只能看到
"no new output"——于是"在重试"被读成"卡死"，被 kill 了两次。

---

## 3. 要做的事

### 段 A：传输层探针（≤2 次调用，10 分钟）

`scripts/probe_recheck_transport.py`：`LLMClient(replace(api, base_url=本地网关,
model="codex/gpt-6-luna", reasoning_effort="low", max_tokens=32768))` 连发 2 次
`用一句话回答：你是谁`，逐次记录 TTFT / 总耗时 / content 长度 / `success` / `error`，
单次 60s 硬超时。

**判据**：2 次都 `success=True`、content 非空、< 60s → 进段 B；否则先修传输层。

### 段 B：单候选复检（≤3 次调用）

`scripts/probe_recheck_one.py`：复用 `.tmp_stage0_recheck` 的 226 个结果文件重建账本，
取 1 条带锚点候选，走 `run_recheck_v2(max_calls=1, batch_size=1)`，打印
渲染提示词长度、**原始回包前 2000 字**、`_extract_payloads` 命中数、
`parse_recheck` 的 invalid_reason、最终 verdict 4 态。

**判据**：拿到 1 条 verdict。若回包非 JSON → 只做最小输出约束修正，不改判定语义。

### 段 C：代码与配置（1.5 小时，含 pytest）

| # | 文件 | 改动 |
|---|---|---|
| **C-0** | `scripts/measure_recheck_v2_pipeline.py:309`、`backend/services/final_summary.py:1498` | `call_model` 加 `if not getattr(res,"success",True): raise RuntimeError(res.error or "LLM call failed")`，让失败如实进 `failed_batches`（**复审 P1-1**） |
| C-1 | `config.json` | `api.max_tokens=65536`；新增 `analysis.recheck_model="codex/gpt-6-luna"`、`recheck_base_url="http://127.0.0.1:3425/v1"`、`recheck_reasoning_effort="low"`、`recheck_concurrency=4`、`recheck_v2_batch_size=6`。**`recheck_v2_enabled` 此处保持 false**（复审 P1-2） |
| C-2 | `backend/config/settings.py` | 新增 `recheck_base_url: str = ""`、`recheck_reasoning_effort: str = "low"`；`recheck_max_tokens` 60000 → 32768（复审 P3-8）；`recheck_v2_enabled` 默认值**暂不动**，段 E 再改 |
| C-3 | `backend/services/final_summary.py` | 复检客户端独立构造：`replace(api, model=recheck_model, base_url=recheck_base_url, reasoning_effort=recheck_reasoning_effort)`；若 base_url 含 `minimaxi.com` 且 model 不在官方目录 → **直接中止复检**并记 `status=PARTIAL` + `status_reason="配置冲突：官方通道不支持该判定模型，已阻断"`，**不静默降级走主模型**（复审 P1-3） |
| C-4 | `backend/services/recheck_v2.py` | 收尾哨兵：`calls>0 and checked==0` → `PARTIAL` + reason「N 次调用全部未返回可解析判定（检查判定模型/网关）」+ `logger.error` + `detail()["raw_sample"]`（首包前 2000 字） |
| C-5 | `scripts/measure_recheck_v2_pipeline.py` | 同上判据 → 打印原因并 `return 2`；`dump_recheck_detail` 传 `recheck_model`/`recheck_base_url` 而非 `cfg.api.*`（复审 P2-6） |
| C-6 | `backend/tests/test_recheck_v2_guards.py` | 新增：失败回包 → `failed_batches>0` 且 `checked==0` 时哨兵生效；空串回包 → PARTIAL + reason；围栏/逗号串仍可解析 |
| C-7 | `config.example.json` | 补齐 `recheck_*` 与 `streaming_enabled` 等缺项，与 `settings.py` 对齐 |

**验收**：C-6 全绿；`pytest` 全绿（基线 1700 例）。

### 段 D：真书回归（约 1.5 小时）

1. **小批**：`--max-calls 20 --batch-size 6 --recheck-concurrency 4`。
   判据：**`checked / (calls × batch_size) ≥ 0.80`**（复审 P1-4 修正了原公式；
   `checked/calls` 会被"每次只答 1 条"蒙混过去）。
2. **整本**：`--max-calls 300 --batch-size 6 --recheck-concurrency 4`，约 30–40 分钟。
   产出 `docs/data/MEASURE-RECHECK-V2-PIPELINE.json` + `docs/data/verdicts-dfm-stage0.json`。
3. **对账（两级，复审 P2-7）**：
   - ① `coverage.conservation_error() is None`（dropped 已计入 `not_checked`，
     不可再用 `checked+not_checked+dropped==total`）；
   - ② 未触发调用预算上限时，`checked / candidates_total ≥ 95%`。
4. **审计**：`python scripts/audit_recheck_verdicts.py`，章号闭包 0 越界。
5. **定位不回归**：`python scripts/check_anchor_identity.py` → 范围命中 100%、兜底 0、未解析 0。
6. **三语料（仅离线）**：长篇/合成多卷/古籍切片的**切分与定位断言**；LLM 复检留给阶段 1。

### 段 E：打开开关（收口动作）

1. `config.json` 与 `settings.py`：`recheck_v2_enabled = true`；
2. 小书端到端跑一次完整总结链路，确认报告出现 4 态判定、`output/recheck_verdicts.json`
   的 `model`/`base_url` 是复检通道；
3. 旧账本保护：若活跃伏笔有效锚点数为 0 → WARNING 提示"账本无锚点，V2 无从核对，
   建议重新分析或回退 V1"（复审 P3-9）；
4. 文档：`data-contract-2.0.md` 补块范围定位/四分母/失败哨兵；`baseline-data.md` 更新基线。

---

## 4. 验收（全部可脚本判定）

| 项 | 标准 | 判定方式 |
|---|---|---|
| 传输 | 2 次直连 `success=True`、非空、< 60s | 段 A 退出码 |
| 单候选 | 1 条 verdict 落账 | 段 B 退出码 |
| 失败可见 | 400/空回包 → `failed_batches>0`、非 0 退出 | C-6 |
| 小批 | `checked / (calls × batch_size) ≥ 0.80` | 测量 JSON |
| 整本 | ①守恒无错 ②未触上限时 `checked/candidates_total ≥ 95%` | `conservation_error()` |
| 定位不回归 | 100% 范围命中、兜底 0、未解析 0 | `check_anchor_identity.py` |
| 门禁 | `pytest` ≥1700 全绿、`vitest` 全绿 | `run_tests.py` + `npm test` |
| 开关 | 小书端到端出现 4 态判定、明细元数据正确 | 人工核对 + JSON |

---

## 5. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R-1 | 先开开关后验证 → 生产报告全"未核对" | 开关只在段 E 打开；段 D 小批必须先过 |
| R-2 | 配置冲突再次静默（官方通道 + 网关模型） | C-0 让失败显性化；C-3 直接阻断不降级 |
| R-3 | 本地网关被并发打满 | `recheck_concurrency=4`（不再跟随 `analysis.concurrency=8`） |
| R-4 | `low` 档判定质量下降 | 抽 20 条人工判读；虚高则回 `medium` 复跑（成本 ×3） |
| R-5 | `max_tokens` 与各上游上限不一致 | `recheck_max_tokens` 降到 32768；采集侧单独观察截断 |
| R-6 | 旧账本无锚点 → 0 判定被误读成"没问题" | 段 E-3 的 WARNING + 状态仍标 PARTIAL |

---

## 6. 不做

- 不改 `backend/vnext/resolution.py` 的判定语义与 5 状态机（复审 §7-1）。
- 不推进阶段 1 接线（`PromptLimits`/`breaker`/`tasks`/原子写）。
- 不做阶段 4 的定标集。
- 不重构复检提示词，只做段 B 允许的最小输出约束。
- 不改前端；`pipeline_version` 灰度开关属阶段 1。
