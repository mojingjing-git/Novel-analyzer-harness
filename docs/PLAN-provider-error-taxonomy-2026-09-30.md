---
doc_type: plan
status: implemented（2026-10-25：§8 独立复审 4 项 P2 + §9 二次复审 P2-5 已并入正文（D1–D5/§3/§4/§5/§7 为修订版）并完成实施，Phase 1–3 + T2.4 + T5.1 落地；Phase 4 全局熔断保留为可选增强）
---

# 厂商错误码分类体系（error_taxonomy）：审核拦截 / 计费 / 限流 / 上下文超长的确定性识别与重试决策

| 字段 | 值 |
|---|---|
| **日期** | 2026-09-30 |
| **目标** | ①修复流式路径审核拦截不识别（浪费重试）；②把"按字符串猜"的分散判定收敛为统一错误分类器，覆盖调研所得各厂商结构化码；③账单/参数类不可重试错误立即停，不再烧重试链 |
| **范围** | `backend/core/`（新模块 error_taxonomy.py + llm_client.py 接入）+ `backend/core/moderation.py`（码表吸收） |
| **不做** | 不改 [MODERATION] 标记格式与 pipeline skip 契约；不改 FailureLogger JSON 格式；不做输出侧审核的自动改写重试；不做 finish_reason 透传（后续增强）；不做 429 Retry-After 之外的精确等待解析 |

---

## 0. 背景与证据

### 0.1 已定位的 bug：流式异常不挂审核标记

`llm_client.py:618-622` 流式生成器的异常处理直接组合错误串 yield，**不做审核嗅探**；
全文件唯一的 `moderation_hit_from_exception` 调用在非流式 `chat()`（:407）。
下游重试循环只认 `[MODERATION]` 前缀（`is_moderation_error` = startswith），导致：
审核拦截被当普通失败 → 烧完整温度退火链 → pipeline 标 failed → **3 轮补跑 × 每轮再烧完整链**。

**本地日志实证**（api_failures.log，2026-09-30 挖掘）：

| 真实错误串（归一化） | 条数 | 现状 |
|---|---|---|
| `流式调用异常: APIError: output new_sensitive (1027)` | 558 | 未标记 → 全链重试（本 bug） |
| `流式调用异常: UnprocessableEntityError: 422 {...unprocessable_entity_error, "input new_sensitive..."}`（Anthropic 兼容皮肤） | ~5525 | 未标记 → 全链重试（同 bug，输入侧！） |
| `流式调用异常: APIStatusError: 402 {...insufficient_balance_error...}` | 650 | **计费错误被当普通失败烧重试链** |
| `API错误 (HTTP 429): {...rate_limit_error, "已达到 Token Plan 用量上限..."}` | ~1786 | 限流，退避正确 |
| `[MODERATION] API错误 (HTTP 400): content policy` | 1046 | ✅ 已正确标记（OpenAI 系） |
| `[MODERATION] API错误: Request blocked by WAF` | 889 | ✅ 文本兜底命中（"blocked"） |
| `[MODERATION] 未知错误: RuntimeError: 内容不符合安全规范` | 291 | ✅ 文本兜底命中（"安全规范"） |
| `流式调用异常: BadRequestError: 400 "you must provide a model parameter"` | 50 | **参数错误烧重试链** |
| `API错误 (HTTP 429): {...GoUsageLimitError, "Weekly usage limit reached. Resets..."}` | 58 | 周配额，退避（可接受） |

**浪费量级**：每个被拦块 ≈ 13-16 次计费调用（4 轮 × 每轮 3-4 次退火尝试，输入 token 每次计费）；
仅 1027 一类 558 条记录即对应约百万 token 级浪费。5525 条 422 输入拦截同理——且**输入侧拦截是确定性的，
重试成功率恒为零，全部是纯浪费**。

### 0.2 现有分类资产盘点

| 位置 | 机制 | 问题 |
|---|---|---|
| `moderation.py` | MODERATION_CODES（含 MiniMax 1026/1027 等 8 值）+ 文本正则 + `[MODERATION]` 标记 | 只覆盖审核单一维度；`moderation_hit_from_exception` 只在非流式路径被调 |
| `llm_client.py:795/911/1150/1263` | `"认证失败" in err or "401" in err` → 停 | 字符串硬编码；402/参数错/上下文超长不停（§8.1 复核：共 4 处） |
| `llm_client.py:1007 _is_429` | 字符串判定 + Retry-After 解析 | **429 ≠ 限流的陷阱**：OpenAI `insufficient_quota`、Kimi `exceeded_current_quota_error` 都是伪装成 429 的账单错误（调研确认），当前一律退避 |
| `llm_client.py:428/483` | `APIError_HTTP{status}` 记日志 | 仅记日志，不参与决策 |
| pipeline:672-690 | `[MODERATION]` → skipped（不进失败集、补跑不重试、落 _skipped.json） | 机制完好，等正确信号喂进来 |

## 1. 调研结论（两路子代理，官方文档为主）

> 完整逐厂商码表见调研报告（agent 输出），此处收编设计所需的结论。来源：OpenAI
> developers.openai.com / Anthropic platform.claude.com / DeepSeek api-docs.deepseek.com /
> MiniMax platform.minimax.io / 智谱 docs.bigmodel.cn / 火山方舟官方 Apifox 镜像 / 硅基流动
> docs.siliconflow.com / 百炼 help.aliyun.com / OpenRouter openrouter.ai/docs / Kimi
> platform.kimi.ai，全部官方或官方镜像（LM Studio/Ollama 例外，见附录注）。

### 1.1 高置信"内容审核拦截"结构化信号总表

| 厂商 | 信号 | 位置 | 输入侧/输出侧 |
|---|---|---|---|
| MiniMax | `base_resp.status_code` **1026**（输入涉敏）/**1027**（输出涉敏）；Anthropic 皮肤 422 `unprocessable_entity_error` + message 含 `new_sensitive`；OpenAI 兼容 200 响应 `input_sensitive/output_sensitive` 布尔字段 | body / 200 响应 | 1026 输入（确定性）/ 1027 输出（概率性） |
| 智谱 GLM | 业务码 **1301**（HTTP 400）"输入或生成内容可能包含不安全或敏感内容" | body `error.code` | 双侧 |
| 火山方舟 | **SensitiveContentDetected** / **SensitiveContentDetected.SevereViolation**（HTTP 400，区分严重档） | body `error.code` | 输入侧 |
| 百炼 Qwen | **DataInspectionFailed / data_inspection_failed**（HTTP 400） | body `code` | 双侧 |
| DeepSeek | 200 响应 `finish_reason="content_filter"`（无错误码；错误响应无审核码，"审核返回 404"系社区误传，低置信） | 200 响应 | 输出侧 |
| OpenAI | `error.code = content_policy_violation`（400）/ `moderation_blocked`（图像）；200 响应 `finish_reason=content_filter` | body | 双侧 |
| OpenRouter | HTTP 403 + typed `error_type = content_policy_violation / refusal` | body（Chat 皮肤 `metadata.error_type`，Anthropic 皮肤 `error.error_type`） | 双侧 |
| Kimi | HTTP 400 + `error.code = content_filter` | body | 双侧 |
| Anthropic | 200 响应 `stop_reason="refusal"`（非错误）；无错误态审核码 | 200 响应 | 输出侧 |
| 硅基流动 | **无官方结构化码**，只能 message 关键词兜底（低置信） | — | — |

**普遍规律（决定重试策略）**：输入侧审核 = 确定性拦截 → 立即停；输出侧审核 = 概率性 → 保留现有"连续 3 次短路"阈值即可，短路后标记走 skip 链。

### 1.2 billing 信号总表（最大的"按状态码必错"陷阱）

| 厂商 | 余额不足的真实形态 | 陷阱 |
|---|---|---|
| DeepSeek | **HTTP 402** "You have run out of balance" | — |
| MiniMax | base_resp **1008**；Anthropic 皮肤 **HTTP 402** `insufficient_balance_error` | — |
| OpenAI | **HTTP 429** + `error.code = insufficient_quota / credit_balance_exhausted / billing_hard_limit_reached`，官方原话"重试不会恢复访问"，**openai-python SDK 还会自动重试它** | 429 伪装 |
| Kimi | **HTTP 429** + `exceeded_current_quota_error`（"账户余额不足或已被停用"） | 429 伪装 |
| 智谱 | 业务码 **1113**（"账户已欠费"）——**HTTP 是 429 不是 402** | 429 伪装 |
| 火山方舟 | **HTTP 403** `AccountOverdueError` | 403 伪装成鉴权 |
| 百炼 | **HTTP 400** `Arrearage`；注意 `insufficient_quota` 在百炼语义是 **限流**不是欠费 | 400 伪装 + 同名不同义 |
| OpenRouter | **HTTP 402** "insufficient credits"（例外：`limit_source=openrouter_in_flight_budget` 时带 Retry-After 可重试） | — |
| Anthropic | **HTTP 402** `billing_error`（正式码；旧形态 400/403 + "credit balance is too low" 做文本兜底） | — |

**结论：billing 识别必须按"业务码/错误类型字符串白名单"，绝不能按 HTTP 状态码。**

### 1.3 其它关键结构化信号

- **context_length**：OpenAI `context_length_exceeded`（400）；MiniMax `1039`；智谱 `1261`；百炼藏身 400 `InternalError.Algo.InvalidParameter`（须 message 正则 "input length"/"Range of input length"/"exceed"）；OpenRouter `context_length_exceeded`（typed）；Ollama/LM Studio 仅 message 正则（"context length"/"prompt longer"）
- **auth**：各家 401 + 智谱 1000/1001/1003/1005、方舟 AuthenticationError、百炼 InvalidApiKey、OpenAI invalid_api_key；**403 例外**：OpenRouter 的审核拦截也在 403（typed 可区分）、方舟 403 是欠费
- **rate_limit**：各家 429；typed：`rate_limit_error/rate_limit_exceeded`、智谱 1302/1308/1310、方舟 RateLimitExceeded.*、百炼 Throttling.*、方舟 `ModelLoadingError`（模型加载中，值得退避）
- **server**：500/502/503/529（Anthropic overloaded）、OpenRouter provider_overloaded/provider_unavailable、智谱 1200/1230、方舟 ServerOverloaded/InternalServiceError
- **Retry-After**：仅 OpenAI（含 `retry-after-ms`、`x-should-retry`）与 Anthropic/OpenRouter 文档确认；国内六家官方文档均未记载 → 国内厂商限流按指数退避即可（现有行为保持）

### 1.4 重试决策矩阵（分类器的输出契约）

| ErrorClass | 决策 | 理由 |
|---|---|---|
| `AUTH` | 立即停（现状保持） | 修不 key 不会自愈 |
| `BILLING` | **立即停**（新增） | 官方明示"重试不会恢复" |
| `MODERATION_INPUT` | 立即停 + `[MODERATION]` 标记 → skip 链 | 确定性拦截，重试成功率 0 |
| `MODERATION_OUTPUT` | 现有"连续 3 次短路" + 标记 → skip 链 | 概率性，3 次阈值已含重试 |
| `CONTEXT_LENGTH` | 立即停（错误信息提示调 block_size/截断） | 原样重试必然再超 |
| `BAD_REQUEST` | 立即停（新增） | 参数/模型名错误不自愈 |
| `RATE_LIMIT` | 尊重 Retry-After → 指数退避（现状保持） | — |
| `SERVER` / `NETWORK` / `TIMEOUT` | 指数退避（现状保持） | 瞬时 |
| `UNKNOWN` | 现有链（退避） | 保守 |

## 2. 设计决策

### D1：新建 `backend/core/error_taxonomy.py`，moderation.py 保持契约不变

- `ErrorClass` 枚举（§1.4 全类：AUTH / BILLING / MODERATION_INPUT / MODERATION_OUTPUT / CONTEXT_LENGTH / BAD_REQUEST / RATE_LIMIT / SERVER / NETWORK / TIMEOUT / UNKNOWN）+ `RETRY_POLICY: dict[ErrorClass, Literal["stop", "backoff", "retry_after"]]`
  - **不设无侧的 MODERATION**（P2-5 消解 §8 P3-1 枚举不一致）：审核码必须落 INPUT 或 OUTPUT，双侧码默认 OUTPUT
  - MODERATION_OUTPUT 的"何时停"（structured 首次 / text 连续 3 次）由调用方短路计数器表达，**不塞进 RETRY_POLICY**（§8 P3-2）
- **`classify_exception(e) -> Optional[ClassVerdict]`**：异常对象在场时调用，**只做结构化通道**，无命中返回 `None`（调用方 `or classify_message(...)` 兜底，天然消解 §8 P3-1 的双 miss None 分支）：
  1. 异常属性码：`e.code` / `e.type`（openai-python 从 body 解析的一等属性；anthropic 的 `e.type`）
  2. `e.body` dict 深挖：OpenAI 风格 `error.code/error.type`；Anthropic 风格 `error.type`；MiniMax `base_resp.status_code`；方舟/硅基 `error.code` 字符串（非 dict body 防御性跳过，对齐 moderation_hit_from_exception 的 P1 修复语义）
  3. HTTP 状态码兜底（仅无 body 码命中时）：401/403→AUTH、402→BILLING、413→CONTEXT_LENGTH、429→RATE_LIMIT、5xx→SERVER；**400/404/422 不做状态兜底**（未识别的 400 保守按 UNKNOWN 退避，避免误停补跑）
- **`classify_message(msg) -> ClassVerdict`**：字符串通道，**永不返回 None**（默认 UNKNOWN）：
  1. 码串扫描：消息中出现的结构化码（数字码 `\b` 边界、snake_case 码子串）→ structured 置信——这是判定结果"穿过错误字符串边界"的通道（重试循环只见字符串）
  2. 类文本正则（按类优先序）→ text 置信
- `moderation.py` 的 `mark_moderation` / `is_moderation_error` / `MODERATION_MARKER` **原样保留**（pipeline/analyzer 契约零改动）；`MODERATION_CODES`/文本表迁移进 taxonomy 并扩容（旧 10 值全量吸收，含 `safety`/`moderation` 等裸词码），moderation.py 的判定函数改为转发（保持旧测试兼容）
- `ClassVerdict` = `(ErrorClass, confidence)`，confidence 仅分 `structured` / `text`

### D2：码表以"错误类型/业务码字符串白名单"为主体（2026-10-25 修订版，并入 §8 P2-1/3/4、§9 P2-5/P3-6）

按调研 1.1-1.3 收编（每组附来源厂商注释）。**审核码逐条带侧标注**（input/output/both），
**both 默认落 MODERATION_OUTPUT**（保守：保留 3 次短路语义；MiniMax OpenAI 兼容 200 响应的
`input_sensitive`/`output_sensitive` 布尔字段可精确切侧）：

- 结构化码表（异常对象通道，码值归一化小写后精确匹配）：
  - **审核**：`1026`(input, MiniMax)、`1027`(output, MiniMax)、`1301`(both→output, 智谱)、
    `sensitivecontentdetected*`(input, 方舟，前缀覆盖 `.SevereViolation`)、`datainspectionfailed`/`data_inspection_failed`(both→output, 百炼)、
    `content_policy_violation`(both→output, OpenAI/OpenRouter)、`content_filter`(both→output, OpenAI/Kimi)、
    `moderation_blocked`(OpenAI 图像)、`refusal`(OpenRouter，**仅 body 通道**)、
    旧 MODERATION_CODES 裸词码吸收（`safety`/`moderation`/`policy_violation`/`unsupported_content`/`inappropriate_content`/`harmful_content`，**仅 body 通道**）
  - **计费**：`insufficient_balance_error`(MiniMax 皮肤 402)、`1008`(MiniMax base_resp)、`insufficient_quota`(OpenAI/Kimi 429 伪装)、
    `credit_balance_exhausted`、`billing_hard_limit_reached`(OpenAI)、`exceeded_current_quota_error`(Kimi)、`1113`(智谱 429 伪装)、
    `accountoverdueerror`(方舟 403)、`arrearage`(百炼 400)、`billing_error`(Anthropic 402)
  - **上下文超长**：`context_length_exceeded`、`1039`(MiniMax)、`1261`(智谱)、`request_too_large`(413 语义)
  - **认证**：`invalid_api_key`、`authentication_error`、`invalid_authentication_error`(Anthropic)、`authenticationerror`(方舟)、`invalidapikey`(百炼)、
    智谱 `1000/1001/1003/1005`（**仅 body 通道**，"1000" 数字太易误报）；**`AccessDenied` 不入表**（方舟 403 语境实为欠费，由 `AccountOverdueError` 在 BILLING 优先级下正确归类）；`invalid_request_error` 不入表（太泛）
  - **参数错**：`modelnotfound`/`model_not_found`、`20012`(硅基，官方语义=模型不存在)、`1211`、`invalid_parameter`(方舟)、`missingparameter`、`2013`(MiniMax)
  - **限流**：`rate_limit_error`/`rate_limit_exceeded`、智谱 `1302/1308/1310`（**仅 body 通道**）、`modellingloadingerror` 不入表（走 SERVER 退避）
- **422 条件规则（P2-1）**：`unprocessable_entity_error` + HTTP 422 + message 命中审核文本 → MODERATION_INPUT（MiniMax Anthropic 皮肤输入拦截，占日志 ~5525 条）；
  无敏感 message 的 `unprocessable_entity_error`（真 Anthropic）→ BAD_REQUEST
- **消息码串扫描白名单（P3-6 第二套表）**：仅收误报风险低的码——`1026/1027/1113/1008/1039/1261/20012/1211/2013`（数字码 `\b` 边界）+
  全部 snake_case/camel 码（`insufficient_balance_error`、`content_policy_violation`、`sensitivecontentdetected` 前缀等）；
  **排除**：`refusal`、裸词码（`safety`/`moderation` 等）、智谱 1000/1302 系数字码——只存在于 body 通道
- 文本兜底正则（按类优先序）：
  - 审核：现有表全量保留 + `[MODERATION]` 标记本身 + 输入侧升级模式（`input_sensitive`/`input new_sensitive`/`输入内容涉敏` → INPUT，其余 → OUTPUT）
  - billing："余额不足"/"欠费"/"insufficient balance"/"out of balance"/"credit balance"/"insufficient credits"
  - context_length："context length"/"input length"/"prompt is too long"/"prompt (is )?longer"/"maximum context"
  - auth："认证失败"/`\b401\b`/"invalid api key"/"unauthorized"
  - bad_request（窄表，防误伤）："must provide a model"/"model does not exist"/"model not found"/"模型不存在"
  - rate_limit：`\b429\b`/"rate limit"/"too many requests"/"throttling"/"用量上限"/"usage limit"
  - **server/network/timeout（P2-3 新增）**：server（"error code: 5xx"/"http 5xx"/"overloaded"/"bad gateway"/"service unavailable"/"服务器繁忙"）、
    network（"connection (error|reset|refused)"/"econn"/"reset by peer"/"ssl"/"连接失败"）、timeout（"硬超时"/"timeout"/"timed out"/"请求超时"/"API 无响应"）
- **跨类目判定优先级（P2-4 写死）**：MODERATION → BILLING → CONTEXT_LENGTH → AUTH → BAD_REQUEST → RATE_LIMIT → SERVER → NETWORK → TIMEOUT → UNKNOWN；
  通道优先级：body 码表 > 消息码串扫描 > HTTP 状态兜底 > 文本正则。
  唯一前插规则：百炼 `Throttling.*` 前缀**先于一切**判 RATE_LIMIT（覆盖 `insufficient_quota` 在百炼的限流语义）
- **`_is_429` 替换（P3-10）**：重试循环内 6 处 `"429" in err` 子串判定全部替换为 `classify_message(err).cls is RATE_LIMIT`（`_is_429` 方法保留为兼容壳，test_streaming 有打桩引用）

### D3：流式异常修复是 Phase 2 第一项（已诊断的 llm_client.py:618-622）

异常对象**还在场**时立即 `classify_exception`，命中即按类标记后再 yield error chunk——
这是"结构化字段优先"唯一能拿到异常对象的时机，也是本 plan 的直接动机。
2026-10-25 修订（§8 P3-1 / §9 P3-9）：`classify_message` 永不返回 None，`or` 链天然安全；
审核枚举改为 INPUT/OUTPUT 二分；RATE_LIMIT 时顺手补上流式路径从未做过的 Retry-After 提取：

```python
except Exception as e:
    error_msg = f"流式调用异常: {type(e).__name__}: {_redact(str(e))}"
    verdict = classify_exception(e) or classify_message(error_msg)
    if verdict.cls in (ErrorClass.MODERATION_INPUT, ErrorClass.MODERATION_OUTPUT):
        error_msg = mark_moderation(error_msg)
    elif verdict.cls is ErrorClass.RATE_LIMIT:
        ra = _retry_after_from_exception(e)   # P3-9：流式此前从未提取，复用 [Retry-After:N] 字符串通道
        if ra:
            error_msg += f" [Retry-After:{ra}]"
    logger.warning(error_msg)
    yield StreamChunk(type="error", error=error_msg)
```

（billing/context_length 的"立即停"不在生成器层做——生成器只负责标记，决策统一收在重试循环，见 D4。）

### D4：重试循环按 RETRY_POLICY 决策（替换散落的字符串判定）

`chat_stream_with_retry` / `chat_with_retry` 的 error chunk/last_error 处理（温度层 ×2 + 退避层 ×2，共 4 处同构分支）：

- error chunk 到手先过 `classify_message`（永不 None）
- **审核分支（policy 之外的特判，§8 P3-2）**：verdict 为 MODERATION_INPUT/OUTPUT 且串未带标记则补 `mark_moderation`；
  满足以下任一即短路返回（error_type=ModerationBlocked，错误串带标记 → pipeline skip 链）：
  - `cls is MODERATION_INPUT`（确定性输入拦截，首次即停，无论置信度）
  - `confidence == "structured"`（结构化码无瞬时误判风险；1027 等码可经消息码串扫描以 structured 置信穿越字符串边界）
  - `moderation_hits >= 2`（text 置信维持现有"连续 3 次短路"——test_moderation_threshold 的语义守卫，§9 P3-8）
- **stop 类（AUTH/BILLING/CONTEXT_LENGTH/BAD_REQUEST）**：不再进入温度退火，直接 `_build_result` 返回最终错误
  （§8 P3-4：**必须沿用 `_build_result`**，failed_tokens 累加 / last_consumed_tokens / call_stats 契约不变）；
  错误信息附加用户可读提示——BILLING："（API 余额不足，请充值后重试）"、CONTEXT_LENGTH："（本章块超模型上下文，建议调小 analysis.block_size）"、
  BAD_REQUEST："（请求参数错误，请检查模型名/参数配置）"；AUTH 保持现有日志与返回
- 退避层入口的 `is_moderation_error(last_error) and moderation_hits >= 2` 检查原样保留（structured/INPUT 已在温度层首次短路，到不了这里）
- `chat()` 非流式路径：APIError（:407）与 anthropic.APIStatusError（:468，现仅文本嗅探，§9 P3-11）两处改走 `classify_exception(e)`，
  行为对齐；:295/:335/:507 三处响应态/文本嗅探维持现状（非异常对象场景）

### D5：pipeline 补跑 fatal 过滤（必做，P2-2 最小方案②）+ 全局熔断（可选 Phase 4）

- skip 链（:679-689）原样不动——喂进正确标记后自动生效：MODERATION_INPUT 首次分析即 skipped、0 补跑、断点续跑不重付
- **必做（原"可选"升级，§8 P2-2 方案②）**：`_analyze_one_block_common` 失败路径对 `retry_info["error"]` 过 `classify_message`，
  命中 stop 类（AUTH/BILLING/CONTEXT_LENGTH/BAD_REQUEST；**审核类除外**——`skip_moderation_blocked=False` 时用户明确要求补跑审核块）
  则登记 `pipeline._fatal_blocks[block_id]`；补跑轮启动前把 fatal 块从重试列表剔除（全部为 fatal 则整轮终止并告警）。
  欠费场景浪费从"剩余块数 × 4 次调用"降到"剩余块数 × 1 次"。改动 ≈15 行（§9.1 成本修正：错误分类须经 retry_info 传递，
  `state.add_failed` 的原因串语义不变）
- **可选 Phase 4（增强，可裁剪）**：并发轮内全局熔断——连续 ≥3 个不同块 fatal 时主动 `request_stop` 整场分析并经 WS `state_change`
  广播原因（"API 余额不足，已自动停止"），把"剩余块数 × 1"也省掉。风险：误熔断需 ≥3 个独立块佐证 + fatal 类全部是高置信结构化判定，可控

## 3. 任务清单

| Phase | # | 文件 | 改动 | 验收 |
|---|---|---|---|---|
| 1 | T1.1 | `backend/core/error_taxonomy.py`（新建） | ErrorClass/RETRY_POLICY/ClassVerdict + 码表（D2 修订版）+ `classify_exception`/`classify_message` | 单测全绿（gold fixtures） |
| 1 | T1.2 | `backend/core/moderation.py` | 判定函数转发 taxonomy；`mark_moderation`/`is_moderation_error` 原样 | 既有 test_moderation/test_llm_moderation_sniff 零改动通过 |
| 2 | T2.1 | `backend/core/llm_client.py:618-622` | 流式异常 classify + 标记 + Retry-After 提取（D3） | 新测试：MiniMax 1027 真实异常 → error chunk 带 `[MODERATION]`；429 → chunk 带 `[Retry-After:N]` |
| 2 | T2.2 | `llm_client.py` 重试循环（流式温度 :767-806 / 流式退避 :859-915 / 非流式温度 :1116-1161 / 非流式退避 :1163-1265，含 :1263） | error chunk 先 `classify_message`；按 D4 决策：stop 类 `_build_result` 立即返回 + 可读提示；审核 INPUT/structured 首次短路；`_is_429` 6 处替换 | 新测试：402 insufficient_balance_error 首次尝试即停；`you must provide a model parameter` 不重试；1027 structured 单次调用即短路 |
| 2 | T2.3 | `llm_client.py` chat() 非流式异常分支（:407 APIError / :468 anthropic.APIStatusError） | `moderation_hit_from_exception` / 文本嗅探 → `classify_exception` 对齐（:295/:335/:507 维持现状） | 既有测试回归 |
| 2 | T2.4 | `backend/core/pipeline.py` | 补跑 fatal 过滤（D5 必做项）：`_fatal_blocks` 登记 + 补跑列表剔除 | 新测试：billing 失败块不进补跑列表；transient 失败块正常补跑；全 fatal → 终止补跑 |
| 3 | T3.1 | `backend/tests/test_error_taxonomy.py`（新建） | gold fixtures（§4 表全量 + 歧义样本）+ 集成用例（T2.1/T2.2/T2.4 验收） | 全绿 |
| 4（可选） | T4.1 | `backend/core/pipeline.py` + `queue_service.py` | 并发轮全局熔断（D5 增强项） | 新测试：3 连 fatal → stop + state_change 广播 |
| 5 | T5.1 | `agent.md` + CHANGELOG | §5.1 核心模块表加 error_taxonomy 行 + 10.33 条目 | — |

## 4. 测试计划：本地日志 gold fixtures（真实错误串 → 期望分类）

全部取自 api_failures.log 实际样本（脱敏后），既是单测也是防回归契约：

| 输入（真实串/异常模拟） | 期望 ErrorClass | 期望决策 |
|---|---|---|
| 异常 `APIError("output new_sensitive (1027)")`（流式生成器抛出） | MODERATION_OUTPUT（structured：消息码串扫描 `\b1027\b` / text: sensitive） | structured 首次短路 + 标记（单块 1 次调用，§8 P3-7 数字联动） |
| 异常 `UnprocessableEntityError(422, body={"error":{"type":"unprocessable_entity_error","message":"input new_sensitive..."}})` | MODERATION_INPUT（structured：422 条件规则 / text: input new_sensitive，P2-1） | 标记 → skip，立即停 |
| 异常 422 + `unprocessable_entity_error` **无**敏感 message（真 Anthropic 参数错） | BAD_REQUEST | 立即停 |
| 异常 `APIStatusError(402, body={"error":{"type":"insufficient_balance_error"}})` | BILLING | 首次即停 |
| 异常 429 + body code `insufficient_quota`（OpenAI 伪装） | BILLING（**不是** rate_limit） | 首次即停 |
| 异常 429 + body `{"error":{"code":"1113","message":"账户已欠费"}}` | BILLING | 首次即停 |
| 异常 429 + `{"error":{"type":"rate_limit_error","message":"已达到 Token Plan 速率限制"}}` | RATE_LIMIT | 退避 |
| 异常 403 + `{"error":{"code":"AccountOverdueError"}}`（方舟，P2-4 歧义样本） | BILLING（非 AUTH） | 首次即停 |
| 异常 400 + body code `InternalError.Algo.InvalidParameter` + message "Range of input length should be [1, 131072]"（百炼，P2-4 歧义样本） | CONTEXT_LENGTH（非 BAD_REQUEST） | 立即停 + block_size 提示 |
| `API错误 (HTTP 400): content policy` | MODERATION_OUTPUT（text） | 现有行为 |
| `未知错误: RuntimeError: 内容不符合安全规范` | MODERATION_OUTPUT（text） | 现有行为 |
| `[MODERATION] API错误: Request blocked by WAF`（text 置信守卫，§9 P3-8） | MODERATION_OUTPUT（text，**非** structured） | 连续 3 次短路（test_moderation_threshold 语义） |
| 异常 400 + body `{"error":{"code":20012,"message":"Model does not exist..."}}`（硅基流动） | BAD_REQUEST | 立即停 |
| 异常 400 + `{"error":{"code":"SensitiveContentDetected.SevereViolation"}}`（方舟） | MODERATION_INPUT | 立即停 + 标记 |
| 异常 400 + message "Range of input length should be [1, 131072]"（百炼，纯文本） | CONTEXT_LENGTH（text 兜底） | 立即停 + block_size 提示 |
| `"认证失败，请检查API Key: ..."` | AUTH | 立即停（现状） |
| `"请求硬超时 (timeout=230s): API 无响应"` | TIMEOUT→退避 | 现状（不回归） |
| `"transient error"` / `"stream truncated mid-response"` | UNKNOWN→退避 | 现状（不回归） |
| `"服务器繁忙，请稍后再试"` / `"The model is overloaded"` | SERVER→退避（P2-3 新表） | 退避 |
| 消息仅含 "refusal" 一词（P3-6 守卫） | UNKNOWN（refusal 仅 body 通道） | 退避 |

## 5. 验收标准

1. 真实场景重放：对大明 7 个 1027 块的日志串跑 classify → 全部 MODERATION_OUTPUT（structured，消息码串 `\b1027\b`）；
   流式修复后单块成本 16 次 → **1 次计费调用**（structured 首次短路）、0 补跑（§8 P3-7 数字联动：1027 经消息码串扫描为 structured）
2. 现有测试零回归：test_moderation / test_llm_moderation_sniff / **test_moderation_threshold**（§9 P3-8）/ test_streaming / test_anthropic_provider /
   test_pipeline* / test_queue_service 全绿不改断言
3. 新 gold fixtures 全绿；`python -m pytest` 全绿
4. 日志观察：修复后新跑的书里，审核拦截错误串以 `[MODERATION]` 开头、错误类型出现 `ModerationBlocked`、队列报告出现"内容审核拦截跳过 N 块"；
   billing 类错误 1 次尝试即停且错误信息含充值提示
5. agent.md / CHANGELOG 同步（§9.6）

## 6. 兼容性与回滚

- `[MODERATION]` 标记格式、pipeline skip 契约、FailureLogger 格式均不变 → 前端/队列/断点续跑零感知
- 行为变化只有两类：①本来要重试 N 次的 stop 类错误现在停 1 次（省钱，无正确性风险——官方文档背书）；②structured 审核命中 1 次即短路（原 3 次）
- 回滚：revert llm_client.py 接入点即可；error_taxonomy.py 是纯新增模块，moderation.py 转发层保留则旧测试不受影响

## 7. 不做（显式排除）

- 不做输出侧审核的自动改写/降级重试（LLM 改写成本 > 直接跳过）
- 不做 finish_reason=content_filter / stop_reason=refusal 的 200 响应透传（需要动 chat() 返回签名，涉及 4 个调用方——列为后续增强；空响应嗅探 :820-824 已兜住最痛场景）
- 不做 GLM `next_flush_time` 等限流重置时间的精确等待解析（保持指数退避）
- **OpenRouter `limit_source=openrouter_in_flight_budget` 的 402 例外不单独实现（§8 P3-5 显式决策）**：按 BILLING 停——该例外仅在并发超高时触发，本项目并发下损失可忽略，实现需引入厂商上下文参数
- 不禁用 openai-python SDK 内建自动重试（客户端层 stop 决策先于 SDK 生效的场景已覆盖；SDK 层 429 重试只在 SDK 收到响应后发生，billing 429 会在客户端层被 classify 截获）
- 不做逐厂商 SDK 异常子类的 isinstance 分型（两 SDK 的 402 均无专属异常类，字段判定已足够）
- LM Studio/Ollama 码表不做结构化收录（无官方码表，仅 message 正则兜底）

---

## 8. 独立复审（2026-10-25，结论：需修订后执行）

复审方式：对照当前 HEAD（`f318a5c`，含第三批清理后的工作区）逐条核实 §0 论断与设计可行性；全部关键论断复核属实。以下为需修订项；与正文冲突时以本节为准。

### 8.1 已核实属实的关键论断（复核证据）

- 流式生成器异常无审核嗅探：`llm_client.py:618-622` ✓；`moderation_hit_from_exception` 全文件唯一调用在非流式 `chat()` :407 ✓
- 401 硬编码即停：:795 / :911 / :1150 ✓（**另发现第 4 处 :1263**，§0.2 与 T2.2/T2.3 范围漏列）
- `_is_429` :1007 + Retry-After 解析（:411-421 / :471-477 / :801-802 / :885-886）✓
- 三次短路 `moderation_hits >= 2`：流式 :775-786 / 非流式 :1122-1134 ✓
- pipeline skip 链实际 :677-689（§0.2 写 672-690，小漂移）+ `skip_moderation_blocked` 默认 True（settings.py:181）✓
- 补跑最多 3 轮（pipeline.py:6 / :542）+ 温度退火 3 + 退避 1（constants.py:59-60）→ "单块全链 ≈ 4 轮 × 3-4 次 = 13-16 次" 的数学成立 ✓
- 空响应嗅探 :820-824 ✓；`[MODERATION]` 标记契约 moderation.py:14 / 54-64 ✓
- 小误：MODERATION_CODES 实为 **10 值**（moderation.py:17-24），非 §0.2 所写 8 值，不影响结论

### 8.2 P2（必须修订）

**P2-1 占最大日志量（~5525 条）的 Anthropic 皮肤 422 输入拦截，D2 码表缺结构化触发器。**
gold fixture 行 2 期望 MODERATION_INPUT「立即停」，但 `MODERATION_STRUCTURED` 未收 `unprocessable_entity_error`，也无任何输入侧模式（`input_sensitive` / `input new_sensitive`）。实际 `classify_exception(UnprocessableEntityError(422, body={"error":{"type":"unprocessable_entity_error","message":"input new_sensitive..."}}))` 无结构化命中 → 落文本 `"sensitive"` → MODERATION（text）→ 只能走 3 次短路（每块 3 次调用而非 1 次），且 input/output 无法区分，"确定性输入拦截立即停"（§1.1 普遍规律）对该最大样本落空。
→ 修订：422 语境 `unprocessable_entity_error` 收进 MODERATION_INPUT 结构化表（Anthropic 皮肤专属，附来源注释）；补输入侧 message 模式；gold fixture 行 2 的置信度标注对齐。

**P2-2 stop 类只停单块调用链，pipeline 层仍整场烧钱——Phase 4 不应是"可选"。**
stop 后块标 failed（pipeline :686-689）→ 仍进 3 轮补跑（:542）每轮再烧 1 次；并发 worker 下原轮剩余每块也各烧 1 次 → billing 场景整场浪费 ≈ 剩余块数 × 4 次调用（D5 自己承认 652 块 × 20K+）。不做 pipeline 层动作时，目标③"账单/参数类立即停、不再烧重试链"只完成一半。
→ 修订（二选一）：① Phase 4 升级为必做；② 最小替代：补跑轮启动前检查上一轮 failed 块的错误分类，全为 fatal（AUTH/BILLING）则直接终止补跑——不需要 WS 熔断/广播，pipeline 改动仅几行。推荐 ②+① 中至少落 ②；① 的 3 连 fatal 阈值与广播可后续增强。

**P2-3 D2 码表缺 SERVER / NETWORK / TIMEOUT 文本兜底表。**
§1.4 有这三类、gold fixture 有 TIMEOUT / UNKNOWN 行，但 D2 只枚举了 moderation / billing / context_length / auth / bad_request 五张表 → T1.1 无法落地 TIMEOUT / UNKNOWN 判定。
→ 修订：D2 补三张小表（如 `timeout|timed out|硬超时`、`connection|reset|SSL|ECONN`、5xx 语义），或在 T1.1 验收里明确这三类只按现有 `_is_429`/文本路径透传为 UNKNOWN+退避。

**P2-4 跨类目判定优先级未定义。**
同一输入可同时命中多表：百炼 `InternalError.Algo.InvalidParameter` + input-length message → BAD_REQUEST（`invalid_parameter` 在表）vs CONTEXT_LENGTH（文本兜底）；方舟 403 `AccountOverdueError`（billing）vs `AccessDenied`（auth 表，同为 403 语境）。
→ 修订：写死固定判定顺序（建议 MODERATION → BILLING → CONTEXT_LENGTH → AUTH → BAD_REQUEST → RATE_LIMIT → SERVER → NETWORK/TIMEOUT → UNKNOWN；通道优先级沿用"body 码表 > HTTP 状态 > 文本"），gold fixtures 补 2 个歧义样本锁死顺序。

### 8.3 P3（实施时落实）

1. **D3 伪代码两处**：`ErrorClass.MODERATION` 与 §1.4 枚举（MODERATION_INPUT / MODERATION_OUTPUT）不一致；`classify_exception(e) or classify_message(...)` 需处理双 miss 的 `None` 分支（否则 `verdict.cls` AttributeError）。
2. **RETRY_POLICY 类型**：`Literal["stop","backoff","retry_after"]` 表达不了 MODERATION_* 的"计数短路"语义——moderation 特判放在 policy 之外，设计文档写明，不为凑类型硬塞。
3. **401 即停共 4 处**（:795 / :911 / :1150 / :1263）：T2.2 / T2.3 范围补 :1263。
4. **stop 路径沿用 `_build_result`**：failed_tokens 累加、last_consumed_tokens、统计契约（llm_client.py 文件头第 5-6 行行为契约）写进 T2.2 验收，防止新 stop 分支绕过计费统计。
5. **OpenRouter in-flight budget 例外**：§1.2 确认 402 + `limit_source=openrouter_in_flight_budget` 带 Retry-After 可重试，但设计与 §7 均未落地——显式列入 §7（接受按 BILLING 停的损失）或实现，二选一。
6. **body-only 码两套表**：`refusal` 等仅限 error body 语境的码只进 `classify_exception` 的 body 通道，不得进 `classify_message` 正则表（正文已注明，落实为两套独立表 + 防误报用例）。
7. **验收数字联动**：§5.1 "单块 16 次 → 3 次计费调用" 按 1027 走 text 置信 3 次短路计算；实施 P2-1 后若 1027/输入侧改 structured，数字相应变为 1 次——验收标准按最终码表回填。另补一条：test_queue_service / test_pipeline 既有 skip 链测试零回归。

### 8.4 复审结论

调研扎实（来源可溯、低置信项已标注）、证据可复核（本复审对当前代码逐条核实全中）、范围克制（不做清单明确、契约零改动）。**按 §8.2 四项修订后即可进入实施**；P3 各项落进对应任务清单条目。建议实施顺序维持 Phase 1→2→3（P2-2 选最小替代方案②时，pipeline 改动并入 Phase 2 验收）。

---

## 9. 二次复审（2026-10-25，结论：维持「修订后执行」，新增 1 项 P2）

复审方式：对照当前 HEAD（`c2ec540`，自 §8 复审以来代码零变动）对 §8 全部论断逐条复核，
并独立扫描正文与 §8 均未覆盖的设计缺口。与正文/§8 冲突时，以本节为准。

### 9.1 §8 复核结果

- **§8.1 全部论断在当前 HEAD 复核属实，行号零漂移**：流式 :618-622 无嗅探、
  `moderation_hit_from_exception` 唯一调用 :407、401 硬编码 4 处（:795/:911/:1150/:1263）、
  `_is_429` :1007、moderation 短路 :775-786/:870-879（流式）与 :1122-1134/:1172-1182（非流式）、
  pipeline skip 链 :679-689、补跑 `range(3)` :543、constants 3+1（:59-60）、
  settings.py:181 `skip_moderation_blocked=True`、MODERATION_CODES 10 值。
- **§8.2 四项 P2 全部成立，修订方向正确**。仅对 P2-2 方案②的成本估算修正：
  `state._failed_chapters` 现只存泛化原因串（pipeline.py:686「分析失败（LLM响应解析失败或重试耗尽）」），
  错误分类须经 :678 的 `retry_info["error"]` 在 `_analyze_one_block_common` 处分类后随 `add_failed` 携带——
  实际 ≈10 行、跨 2 处，非「仅几行」；且 failed 原因串流入收尾报告与 WS FAILED 消息，携带分类时不得改变现有语义。
- **「契约零改动」复核成立**：moderation 契约函数生产调用方仅 pipeline.py:33/:679 与 llm_client.py，
  前端零依赖（grep 全库确认）。

### 9.2 新增发现

**P2-5（与 §8.2 四项同级）：MODERATION_INPUT / OUTPUT 的判侧机制缺失。**
§1.4 决策矩阵与 gold fixtures（行1 1027→OUTPUT、行2 422→INPUT、行10 SensitiveContentDetected→INPUT）
均要求按「侧」分流：INPUT 立即停、OUTPUT 3 次短路。但 D2 的 `MODERATION_STRUCTURED` 是一张平表
（1026 与 1027 同表），D1 的 `ClassVerdict` 只有 `(ErrorClass, confidence)` 无侧向通道——
同表码如何映射到不同 ErrorClass 未定义；「双侧码」（content_filter / 1301 / DataInspectionFailed）
更无默认侧。T1.1 无法据此写出满足 fixtures 的实现。
→ 修订：结构化码每条带侧标注（input / output / both）；**both 默认落 OUTPUT**（保守，保留 3 次短路）；
MiniMax OpenAI 兼容 200 响应的 `input_sensitive` / `output_sensitive` 布尔字段可精确切侧。
§8 P3-1 的枚举不一致（`ErrorClass.MODERATION` vs `*_INPUT`/`*_OUTPUT`）在此一并消解：
枚举不设无侧 MODERATION，both 码直接落 OUTPUT。

**P3-8：零回归清单补 `test_moderation_threshold.py`。**
该文件断言的恰是 T2.2 要动的语义（文本标记连续 3 次命中即停、`attempts == 3`、不进退避、
等待序列 `[3, 6]`）。设计上 text 置信维持阈值、测试应原样通过——它正是防「structured 首次短路」
误伤文本路径的守卫，必须进 §5.2 清单。

**P3-9：流式路径从未提取 Retry-After；D3 重写 except 块是补上它的唯一时机。**
非流式 chat() :411-421/:471-477 从 `e.response.headers` 提取并注入 `[Retry-After:N]`，
流式 :618-622 从未做过（429 一律落 30/60/90s 默认退避）。D3 伪代码里 `e` 在手，
补一行 header 提取即可复用既有 `_retry_after_seconds` 解析通道；不做则显式记入 §7。

**P3-10：D4 应声明 `_is_429` 的去留。**
`"429" in error`（:1007）是子串匹配（"1429 tokens" 即误判），且 :801/:885/:862-863/:1155/:1187/:1165
多处依赖它（含 429 时退避轮加码到 3）。T2.2 以 RETRY_POLICY 决策后，应同步把重试循环内
`_is_429` 的使用替换为 RATE_LIMIT 类判定（或明确保留并接受子串误判），避免双轨判定漂移。

**P3-11：T2.3 的「5 处」点名 anthropic.APIStatusError 分支（:460-487）。**
该分支现只做文本嗅探（`e.response.text`），无结构化 body 判定——Anthropic 原生协议的
402 `billing_error` / 结构化审核码识别全靠 `classify_exception(e)` 从这里进来；
§0.1 表中 650 条 402 样本虽走流式（D3 覆盖），非流式 Anthropic 直连用户由该分支覆盖，验收点名。

### 9.3 结论

§8 判定维持：调研扎实、证据可复核、范围克制，核心设计成立。按 **§8.2 四项 + P2-5 共 5 项 P2**
修订后即可实施；P3-8~P3-11 并入对应任务清单条目（P3-8 → §5.2/T1.2 验收，P3-9/P3-10 → T2.1/T2.2，
P3-11 → T2.3）。实施顺序不变（Phase 1→2→3），P2-2 走方案②（成本按 §9.1 修正）。

---

## 附录：调研方法与置信度说明

- **官方直抓**：DeepSeek、智谱（docs.bigmodel.cn `.md` 直抓）、MiniMax（platform.minimax.io）、硅基流动、百炼（help.aliyun.com）、OpenAI（developers.openai.com 新域）、Anthropic（platform.claude.com）、OpenRouter、Kimi（platform.kimi.ai）
- **官方镜像交叉**：火山方舟官方页 JS 渲染不可抓，采用官方 Apifox 镜像（doubao.apifox.cn）全文 + 官方页搜索摘要交叉验证，高-中置信
- **源码验证**：openai-python `_exceptions.py` / `_base_client.py`、anthropic-sdk-python `_exceptions.py`、Ollama `openai/openai.go`（错误格式映射仅 3 种 type、code 恒 null）
- **低置信已标注**：LM Studio 错误行为（社区来源）；DeepSeek"审核 404"（社区误传，未采信）；硅基流动 20011（无官方出处，未采信；20012 官方语义 = 模型不存在）
