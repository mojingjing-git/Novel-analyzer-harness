# D-01 实施规格：错误判定（ErrorVerdict）的类型化传输

- 状态：**已裁决**（外审选方案 A；7 项附带决定已定；本文为实施规格，不再是选型请求）
- 基线：`5937cb1`
- 相关：`docs/PLAN-v2-long-novel-reliability-2026-10-30.md` §S1、§2.5（待新增）、T01–T04、T45
- 裁决要点：**方案 A，不做 `__iter__` 过渡，一次性更新全部消费者与 mock**

---

## 1. 问题

同一件失败被判定两次、且第二次只能靠字符串猜：

```
:684 只 yield 错误字符串 → :788 attempt 只取字符串 → :807 返回值无判定
→ :840/_991/_1238/_1391 四处 _error_action 重新分类
→ :1108 StreamResult→元组时丢弃 → analyzer:175 只传字符串 → pipeline:711 再 classify 一次
```

同时有三处把显示字符串当语义用：

| 位置 | 现状 |
|---|---|
| `core/analyzer.py:175` | `retry_info["error"]` 回传；pipeline 靠 `[MODERATION]` **前缀**判 skipped |
| `services/location_normalizer.py:567` | `error` 进 `on_progress` 事件 |
| `services/location_normalizer.py:627` | 同上，前端 UI 可见 |

---

## 2. 已核实事实

### 2.1 签名

| 方法 | 位置 | 现状 |
|---|---|---|
| `chat` | `llm_client.py:267-272` | `Tuple[bool, str, str, Tuple[int,int]]` |
| `chat_with_retry` | `:1142-1148` | 五元组 |
| `chat_auto` | `:1084-1091` | 五元组 |
| `chat_stream` | `:572-577` | `AsyncIterator[StreamChunk]` |
| `chat_stream_with_retry` | `:705-712` | `StreamResult` |

`StreamChunk`（`llm_stream.py:13-35`）9 字段、`StreamResult`（`:38-48`）8 字段，**全部有默认值**，追加安全。

### 2.2 位置占用

- 第 4 位 `Tuple[int,int]` = `(prompt_tokens, completion_tokens)`，8 处按二元组消费——**已满**
- 第 5 位 `dict` 仅含 `attempts` / `failed_tokens`，13 个外部点中 **6 处直接丢弃**
  → 塞 dict 会静默丢字段

### 2.3 循环导入

`llm_stream.py` 与 `error_taxonomy.py` 双向零导入，均为叶子。类型下沉到独立叶子模块后，两者单向依赖它，不成环。

### 2.4 18 个生产调用点

13 外部 + 5 内部，清单见 §S1 附表 A（逐点行号与解包元数已核验，无遗漏）。`llm_probe.py:198` 与 `llm_client.py:301/633/646` 是原生 SDK 调用，不受影响。

---

## 3. 类型契约

### 3.1 分层（关键修正：`ClassVerdict` **不足以**承担传输契约）

```
error_types.py        ← 新增，最底层叶子：共享枚举 + 纯数据类型
   ├─ ErrorClass(str, Enum)       11 类
   ├─ Confidence(Enum)            STRUCTURED / TEXT / NONE
   ├─ SourceChannel(Enum)         SDK_ATTRIBUTE / ERROR_ENVELOPE / STATUS_FALLBACK / TEXT_FALLBACK
   ├─ ErrorScope(Enum)            ACCOUNT_GLOBAL / REQUEST_LOCAL / TRANSIENT / UNKNOWN
   ├─ UsageStatus(Enum)           KNOWN / UNKNOWN
   ├─ ErrorVerdict                完整传输契约
   └─ LLMResult                   调用结果

error_taxonomy.py     ← 保留 ClassVerdict 作为**分类器内部输出**
   classify_exception(e) -> ClassVerdict      （不改字段，37 条测试不动）
   classify_message(msg) -> ClassVerdict
   新增：to_error_verdict(cv, *, channel, http_status, provider_code, retry_after, scope)
        -> ErrorVerdict

llm_stream.py         → 单向 import error_types
llm_client.py         → import error_types + error_taxonomy + llm_stream（不变）
```

`error_taxonomy.py` **不得**导入 `llm_stream`（否则策略层反向依赖协议层）。

### 3.2 `ErrorVerdict`（新增，完整契约）

```python
@dataclass(frozen=True)
class ErrorVerdict:
    cls: ErrorClass
    confidence: Confidence
    source_channel: SourceChannel   # 判定来自哪条通道——用于可信度判断与审计
    provider_code: Optional[str]    # 厂商原始码，如 "1027" / "insufficient_quota"
    http_status: Optional[int]
    action: str                     # stop / backoff / retry_after / continue，来自 RETRY_POLICY
    scope: ErrorScope               # ACCOUNT_GLOBAL / REQUEST_LOCAL / TRANSIENT / UNKNOWN
    retry_after: Optional[float]    # 秒；来源见 §3.5
    message: str                    # 展示用，不参与任何判定
```

设计约束：

- `action` 由**唯一的策略函数**计算后**固化**在 verdict 里：输入为 `cls × scope × policy` **加**重试上下文（连续审核命中次数、剩余预算与剩余轮数），不得只按错误类别推导；外层只消费，不再自行推导
- `message` 只用于展示/日志；**任何判定逻辑不得读取它**
- 全部字段可 JSON 往返（显式 DTO，见 §3.6）

### 3.3 `LLMResult`

```python
@dataclass(frozen=True)
class LLMResult:
    success: bool
    content: str
    error: str                                  # 展示用，不承载判定
    tokens: Optional[Tuple[int, int]]           # None = 未知（§4）
    usage_status: UsageStatus                   # KNOWN / UNKNOWN
    call_stats: dict = field(default_factory=dict)
    verdict: Optional[ErrorVerdict] = None      # 成功时为 None
    attempts: int = 1
```

**不实现 `__iter__`**，不提供位置式解包。旧元组形状在 S1 一次性删除，不留兜底。

### 3.4 `StreamChunk` / `StreamResult`

两者都追加：

```
verdict: Optional[ErrorVerdict] = None
```

**`StreamChunk` 必须携带完整 `ErrorVerdict`**——不能只带分类、把 `scope`/`retry_after` 留到终结结果，否则流式异常头里的信息会在中间丢失，且多处字段可能互相矛盾。

### 3.5 `scope` 与 `retry_after`

- `scope` 按枚举取值 **`ACCOUNT_GLOBAL` / `REQUEST_LOCAL` / `TRANSIENT` / `UNKNOWN`**，**不允许自由字符串**，不默认成 `REQUEST_LOCAL`；与主计划 §4 S1、S8a 及 T03/T25 同一套词汇
  - `REQUEST_LOCAL`：仅本次请求/本块受影响（含重试与补跑），不整场停机
  - `TRANSIENT`：临时性，可自由退避重试
  - `ACCOUNT_GLOBAL`：账户/端点级——绑定 `endpoint` + **不含密钥的凭据指纹**（如 api_key 前 6 位 + sha256 前 8 位），不得记录明文；只有它能触发熔断
  - `UNKNOWN`：作用域不明。**必须保留**，且**不得**凭错误字符串升级为账户熔断
- `retry_after` 从异常边界一路传到决策层：`SDK_ATTRIBUTE`（`Retry-After` 头 / SDK 字段）优先，`ERROR_ENVELOPE` 次之，缺失为 `None`

### 3.6 序列化

错误与统计元数据用**显式 DTO / 编解码**，不依赖 dataclass/NamedTuple 的默认 JSON 形状；**不**把响应正文写入 checkpoint。需要的字段：`cls / scope / action / provider_code / http_status / attempts / usage_status`。

---

## 4. usage 未知的表达（修正）

`tokens: Tuple[int,int]` 无法表达"断连后没拿到 usage"。规则：

| 场景 | `tokens` | `usage_status` |
|---|---|---|
| 正常返回且有 usage | `(p, c)` | `KNOWN` |
| 断连 / 超时 / 解析失败，未拿到 usage | `None` | `UNKNOWN` |
| 流式部分消费后中断 | 已知部分 or `None` | `KNOWN`（部分）或 `UNKNOWN` |

约束：

- **不得**把未知写成 `(0, 0)`——那是"消耗为零"的错误陈述
- **不得**沿用上一次 attempt 的计数
- 费用聚合必须按 `usage_status` 分桶统计，报告中"未知消耗"单独成列

---

## 5. 判定沿革语义（修正：「保留首个因果」需分层）

原表述"保留首个因果判定，不以最后一次覆盖"过于笼统，会导致 `429 → AUTH` 场景把限流传给外层，从而**重跑一个已认证失败的请求**。改为三条，必须同时成立：

| 情形 | 规则 |
|---|---|
| **同一次错误逐层包装** | 保留**原始原因**。包装层（如 `HTTPStatusError` 包 `APIError`）不得用自己的 cls 覆盖内层 |
| **不同 attempt** | 每个 attempt **各自**保留一条记录；**终态采用"导致最终停止/最终结果"的那次判定** |
| **重试后成功** | 当前结果为成功；历史失败仅留在 attempt 记录与统计中，不进入 `result.verdict` |

即：**因果按"错误实例"追溯，按"attempt 序列"决断。**

---

## 6. 兜底与来源可信规则（修正）

上一版的"字段形态匹配即可解析错误码"与"两次一致"都不成立：

- 小说正文同样可以包含 `{"code":1027}`——**字符形态不能证明来源可信**
- 外审从未建议"两次一致"阈值，予以撤销

改为：

```
1. 提供者边界始终产出 ErrorVerdict（含 UNKNOWN）
2. 数字码只允许从**可信通道**解析：
   - SDK 结构化属性（异常对象的 code / status / body）
   - 约定的错误 envelope（确认来自响应体错误结构，而非正文）
   禁止从任意显示文本解析错误码
3. 可信错误通道内，允许受限文本分类（如厂商 message 中明确的 "insufficient balance"）
4. 内部缺失 verdict → 记 CONTRACT_VIOLATION，按 UNKNOWN 的有限策略处理；
   **不重新扫描显示字符串**，不尝试恢复 structured 身份
5. 已识别的错误按既有 cls × scope × policy 行动，不引入统一的"N 次一致"新规则；
   审核类沿用现有连续 3 次阈值
```

---

## 7. 已裁决的 7 项

| 问题 | 决定 |
|---|---|
| 选型 / 过渡 | **方案 A**，不实现 `__iter__`，一次性更新消费者与 mock |
| `scope` 是否进 S1 | **进**。枚举 `ACCOUNT_GLOBAL`/`REQUEST_LOCAL`/`TRANSIENT`/`UNKNOWN`，不用自由字符串、不默认 `REQUEST_LOCAL`；账户范围绑定端点与不含密钥的凭据标识；`UNKNOWN` 不得凭字符串升级为熔断 |
| `retry_after` 是否进 S1 | **进**。重试等待当前就需要，必须从异常边界传到决策层 |
| 类型位置 | **`error_types.py`**（共享枚举与纯数据类型），分类器与流模块单向依赖它 |
| 是否可序列化 | 错误与统计元数据用**显式 DTO/编解码**；不要求把响应正文写入 checkpoint；不依赖 NamedTuple 默认 JSON 形状 |
| "两次一致" | **不采纳**，按 §6 的来源与契约规则处理 |
| Token 消费方 | **全部改为命名字段访问**，明确处理未知 usage，删除旧元组形状兜底 |

---

## 8. 实施清单（S1）

1. 新建 `backend/core/error_types.py`（§3.1 的枚举与数据类型）
2. `error_taxonomy.py` 增加 `to_error_verdict(...)`；`ClassVerdict` 保持不变
3. `llm_stream.py`：`StreamChunk` / `StreamResult` 追加 `verdict`（完整 `ErrorVerdict`）
4. `llm_client.py`：
   - `:684` yield 时附 verdict；`:788` attempt 取 `chunk.verdict`；`:807` 返回值携带
   - `:840` / `:991` / `:1238` / `:1391` 四处 `_error_action` 收敛为单点，直接消费 verdict
   - `:1108` `StreamResult → LLMResult` 保留 verdict
   - `chat` / `chat_with_retry` / `chat_auto` 统一返回 `LLMResult`
5. 18 个调用点改为属性访问；三处隐式字符串契约改为读 `verdict`
6. `pipeline.py:711` 删除二次 `classify_message`，消费 `retry_info["verdict"]`
7. 全部 mock / fixture 同步；删除旧元组形状断言

---

## 9. 验收

`T01–T04` 覆盖分类与贯通；**T45 补充三条序列测试**：

| 序列 | 断言 |
|---|---|
| `429 → AUTH` | 终态用 **AUTH**（导致停止的那次），不得把限流传给外层导致重跑认证失败请求 |
| 失败 → 成功 | 结果为成功；历史失败只出现在 attempt 记录与统计，`verdict` 为空 |
| 有 usage 的失败 → 无 usage 的断连 | 后者 `usage_status=UNKNOWN`、`tokens=None`；**不得**写成 0，**不得**沿用前次计数 |

与 T04（两个并发块分别失败与成功）一起验证 verdict、attempts、Token 不串用。

---

## 10. 风险

| 项 | 说明 |
|---|---|
| 改动面 | 18 个生产点 + 全部相关 mock；S1 须交付逐点「旧形状 → 新形状」表 |
| 回滚 | 纯内部类型改造，无数据格式变化，可整体 revert |
| 不承诺 | 不宣称判定准确率提升；准确率取决于码表与可信来源规则（§6） |
| 新增文件 | `error_types.py` 增加一个模块，但消除了 `error_taxonomy ↔ llm_stream` 未来的成环风险 |
