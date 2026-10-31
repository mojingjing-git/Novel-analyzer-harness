---
plan_id: D19-quote-locate
title: D-19 引文定位下沉 + 两处契约收口（键名单一来源、校验器补 evidence_id）
plan_version: 2.0
status: reviewed
implementation_status: not_started
baseline_commit: 23f44b2
created: 2026-10-31
revised: 2026-10-31（吸收复审 R1–R12）
references:
  - docs/MEASURE-C2-C3-2026-10-31.md
  - docs/data-contract-vnext.md §13.3
  - 复审：agent f442f84b（只读，R1–R12）
---

# D-19：让模型只给引文，位置由程序算

## 0. 为什么做（实测证据）

| 事实 | 数字 | 出处 |
| --- | --- | --- |
| 模型判"支持"时引文逐字命中率 | **9/9 = 100%** | C2 |
| 同批因**位置对不上**被判 `INVALID` | **11/20 = 55%** | C2 |
| C3 同类失败 | **5/18 = 28%** | C3 |
| `INVALID` 是否进结果缓存 | **否**（`cached_as_terminal` 对 `INVALID` 恒 False） | `resolution.py:93-95` |
| `INVALID` 是否计入"已核对" | **否**（`checked` 恒 False） | `resolution.py:85` |
| 12 个 `INVALID` 分支里 `retryable=True` 的 | **仅 3 个**（`MALFORMED`/`MISSING_FIELD`/`UNKNOWN_VERDICT`） | `resolution.py:80/112/116/125` |

后果两条，都不是"多花一次钱"这么轻：

- `INVALID` 不进缓存 ⇒ 同一候选被**反复重问**，每次大概率数错同一个数；
- `INVALID` 不计入已核对 ⇒ 覆盖状态长期停在 `PARTIAL`（`resolution.py:436-437`），那 55% 永远显示"本轮未核对"。

另有两处是实测撞出来的：**Prompt 键名与解析器无机制保证一致**（两轮烧掉 36 次调用 / ~109k token）；
**校验器拿不到 `evidence_id`**（`resolution.py:103`），只能验"某段里对得上"，验不了"是不是点名的这段"。

## 1. 目标 / 非目标

**目标**：D-19 定位下沉；P0 键名单一来源；P2 校验器补 `evidence_id`。

**非目标**：

- 不动判定枚举、`reduce_entity` 归约规则、反证检索的必做性。
- 不放松"引文必须是原文逐字子串"。
- 不重跑 C1/C2/C3 真实测量（另行批预算；本批只做离线复现）。

**修订说明**：初稿把"不改覆盖计数"列为非目标，复审证明站不住（R4）——新 reason 会落进
`not_checked`，`invalid_by_reason` 反而变空。已改为**必须显式补 expected 路径用例**，见 §4。

## 2. 契约变更

### 2.1 SUPPORTED 的判据（三件套 + 程序定位）

| 字段 | 现在 | 改后 |
| --- | --- | --- |
| `evidence_id` | 必填 | 必填（不变） |
| `supporting_quote` | 必填 | **必填**（不变） |
| `supporting_span` | 必填且必须逐字对上 | **可选，只作"提示"**；存进 `reported_span`，不参与判据 |

判据：`supporting_quote` 能在 `evidence_id` 指向的那段正文里被**定位器**接受；位置由程序算，写 `located_span`。

`reported_span` 与程序定位结果不一致时：

- 若 `reported_span` 窗口内**恰好包含一次**该引文 → 采纳 `reported_span`（模型指对了位置）；
- 若窗口内**不含**该引文 → **判失败** `INVALID/SPAN_DISAGREES`。
  （复审 V1：只留痕可被"编造 span + 抄对引文"穿透——段长 2000 字、引文是章末套话时，
  `SUPPORTED` 会一路缓存进 `active_results` 被固化。）

### 2.2 reason 码与 retryable

| 码 | 归属 | retryable | 含义 |
| --- | --- | --- | --- |
| `QUOTE_NOT_FOUND` | INVALID | **True** | 引文在该段里定位不到（编造或跨段拼接） |
| `SPAN_DISAGREES` | INVALID | **True** | 给了 span，但该窗口里没有这段引文 |
| `QUOTE_TOO_SHORT` | INVALID | **True** | 引文短于 `MIN_QUOTE_CHARS = 10`（与 `citations.FUZZY_MIN_CHARS` 同量级） |
| `AMBIGUOUS_QUOTE` | **INSUFFICIENT** | False | 引文在该段出现 ≥2 次且 span 未能破歧义 |
| `NO_SUPPORTING_QUOTE` / `NO_EVIDENCE_ID` / `EMPTY_ANSWER` | INVALID | **True**（本批统一） | 旧码，语义不变 |
| `FOREIGN_ID` | INVALID | False | 不属于本任务，重试无意义 |
| `NO_QUOTE_VERIFIER` | INVALID | False | 调用方没给定位器，属实现缺陷 |
| `QUOTE_OUT_OF_RANGE` | INVALID | True | **仅退化模式**（只给旧校验器）出现 |

`retryable` 对 `INVALID` 目前是惰性的（不进缓存、不计已核对），本批**统一设定**，让意图与统计一致（复审 R1）。

### 2.3 `CandidateVerdict` 新字段

- `located_span: tuple[int, int] | None`：程序定位结果（唯一权威位置）
- `reported_span: tuple[int, int] | None`：模型自报位置（只留痕）
- `ambiguous: bool`
- **不新增** `span_mismatch`：不一致只有两种结局（采纳模型位置 / 判失败），没有第三种。

`supporting_span` 保持原义（模型报的值）。**同时必须改 `resolution.py:349`**：`located` 计数从
"`supporting_span` 非空"改为"`located_span` 非空"（复审 R2），否则测试全绿而统计错。

## 3. 实现设计

### 3.1 定位：复用 `citations.verify_citation`，不新写 `str.find`

`citations.py:109-160` 已有 `verify_citation(*, quote_text, chapter_text, start=None, end=None)`
返回 `CitationCheck`，带 `corrected_start/corrected_end` 与六级判定。再写一套 `str.find` 就是两套定位语义。

**采用分级**：

| 级别 | 接受为证据 | 理由 |
| --- | --- | --- |
| `EXACT` | ✅ | 逐字命中 |
| `NORMALIZED` | ✅ | 只差空白/全半角，语义等价 |
| `RELOCATED` | ✅ | **偏移错、引文真**——正是本计划要处理的失败模式 |
| `SEGMENTED` | ❌ | 省略号跨段拼接，等于允许模型"拼引文" |
| `FUZZY` | ❌ | 相似即过，等于关掉反伪造闸门 |
| `MISMATCH` | ❌ → `QUOTE_NOT_FOUND` | |

歧义计数用**递推 find**（复审 R10：`"哈哈哈".count("哈哈") == 1`，实际 2 处）：

```python
def count_occurrences(text: str, quote: str) -> list[int]:
    positions, pos = [], text.find(quote)
    while pos >= 0:
        positions.append(pos)
        pos = text.find(quote, pos + 1)
    return positions
```

薄封装放在 `resolution.py`（不改 `citations` 的公共语义）：

```python
ACCEPTED_CITATION_KINDS = frozenset({"EXACT", "NORMALIZED", "RELOCATED"})

@dataclass(frozen=True)
class LocateResult:
    found: bool
    start: int | None = None
    end: int | None = None
    occurrences: int = 0
    ambiguous: bool = False
    kind: str = ""

def locate_quote(quote: str, text: str, *, reported: tuple[int, int] | None = None) -> LocateResult
```

内部：长度下限 → `verify_citation` → 级别过滤 → 歧义处理（span 窗口内恰含一次则采纳它的位置）。

### 3.2 `parse_recheck` 判定顺序（按代码实际顺序重写，复审 R6）

```
payload 非 Mapping                         → INVALID/MALFORMED            (retryable)
candidate_id 缺失/非 str                    → INVALID/MISSING_FIELD        (retryable)
candidate_id 不属于本任务                    → INVALID/FOREIGN_ID
verdict 不在四枚举                          → INVALID/UNKNOWN_VERDICT      (retryable)
answer 为空                                 → INVALID/EMPTY_ANSWER         (retryable)
[以下仅 SUPPORTED]
material_insufficient_reason 有值            → INSUFFICIENT/MATERIAL_INSUFFICIENT
counter_search.performed 不是 True            → INSUFFICIENT/NO_COUNTER_SEARCH
counter_search.counter_quotes 非空            → INSUFFICIENT/COUNTER_QUOTES_PRESENT
evidence_id 缺失                             → INVALID/NO_EVIDENCE_ID       (retryable)
evidence_id 不属于本任务                      → INVALID/FOREIGN_ID
supporting_quote 缺失/空                      → INVALID/NO_SUPPORTING_QUOTE  (retryable)
引文短于 10 字                               → INVALID/QUOTE_TOO_SHORT      (retryable)
无定位器                                     → INVALID/NO_QUOTE_VERIFIER（退化模式走旧校验器）
定位失败                                     → INVALID/QUOTE_NOT_FOUND      (retryable)
给了 reported_span 而窗口内无该引文            → INVALID/SPAN_DISAGREES       (retryable)
引文 ≥2 处且 span 未能破歧义                  → INSUFFICIENT/AMBIGUOUS_QUOTE
checked_evidence_ids 缺失/外来                → INSUFFICIENT/COUNTER_SEARCH_UNVERIFIED
                                           → SUPPORTED（带 located_span/reported_span/ambiguous）
```

**ID 归属在枚举之前**（`resolution.py:117` vs `:121`），并补上 `MISSING_FIELD`(:116) 与
span 值域(:167) 两步的处置。

### 3.3 定位器签名（P2）

```python
locate: Callable[[str, str], LocateResult] | None   # (quote, evidence_id) -> LocateResult
```

- 给了 `locate` → 首选，`evidence_id` 真的参与定位（验"属于点名那段"）。
- 只给旧 `quote_within_range` → **退化模式**，行为与改前一致（`QUOTE_OUT_OF_RANGE` 仍可能出现）。
- 都不给 → `NO_QUOTE_VERIFIER`。
- **不提供"两个都传以旧为准"**：语义强的优先，混用会让结果无法解释。

**runner 侧必须新增正文来源**（复审 R8）：`run_recheck` 现在只有 ID 集合（`runner.py:145`、`:189`），
造不出 `locate`。新增 `evidence_text: Callable[[RecheckUnit, str], str] | None`，由调用方注入
"按 evidence_id 取正文"。

### 3.4 键名单一来源（P0）

`resolution.py` 的 11 处裸键名：`candidate_id`:114、`verdict`:121、`answer`:128、`counter_search`:130、
`performed`:131、`material_insufficient_reason`:139、`counter_quotes`:148、`evidence_id`:158、
`supporting_span`:164、`supporting_quote`:171、`checked_evidence_ids`:182。其中 3 个是**嵌套键**。

扁平 Mapping 表达不了嵌套结构（复审 R11），所以：

```python
RECHECK_MODEL_KEYS: Final[Mapping[str, object]] = {
    "candidate_id": "candidate_id",
    "verdict": "verdict",
    "answer": "answer",
    "material_insufficient_reason": "material_insufficient_reason",
    "evidence_id": "evidence_id",
    "supporting_span": "supporting_span",
    "supporting_quote": "supporting_quote",
    "counter_search": {
        "performed": "performed",
        "checked_evidence_ids": "checked_evidence_ids",
        "counter_quotes": "counter_quotes",
    },
}

def render_recheck_schema() -> str:  # 由上面的结构生成 JSON 字段示例
```

用例 #8 改为：**解析 `render_recheck_schema()` 的 JSON，取 key 集合，与"解析器实际读取的 key 集合"
双向相等**——不用子串判定（`supporting_evidence_id` 含 `evidence_id`，子串判定会放过历史 bug）。

### 3.5 影响面（已核对）

| 消费方 | 读了什么 | 影响 |
| --- | --- | --- |
| `runner.py:196-197` | `verdict.verdict` | 否 |
| `runner.py:226-232` | `candidate_id` + `verdict`（`apply_key`） | **是**：见 §3.6 |
| `runner.py:262-268` | `verdict.verdict`（`coverage_counts`） | 否 |
| `resolution.py:349` | `supporting_span` 判 located | **是**：改读 `located_span` |
| `test_vnext_resolution.py:80,85` | `verdict.supporting_span` | **是** |

`foreshadow.py` 不 import `resolution`（其 `span` 是实体跨度）；`recheck_plan.py` 不读 verdict → 无影响。

### 3.6 `apply_key` 纳入位置（复审 R12）

`runner._apply_key` 现在是 `entity|candidate|verdict`（`runner.py:226-232`），账本按 apply_key 去重
（`tasks.py:133-134`、`:178-179`）。位置从错到对时 apply_key 不变 → 修正被吞掉。
改为 `entity|candidate|verdict|located_start:located_end`。

## 4. 测试清单（离线，零真实调用）

| # | 用例 | 断言 |
| --- | --- | --- |
| 1 | 只给引文不给 span | `SUPPORTED`，`located_span` = 真实位置 |
| 2 | 给了错的 span，但窗口内含该引文一次 | `SUPPORTED`，`located_span` = `reported_span` |
| 3 | 给了 span，窗口里没有该引文 | `INVALID/SPAN_DISAGREES` |
| 4 | 编造引文 | `INVALID/QUOTE_NOT_FOUND` |
| 5 | 引文 8 字 | `INVALID/QUOTE_TOO_SHORT` |
| 6 | 引文出现 2 次且无 span | `INSUFFICIENT/AMBIGUOUS_QUOTE` |
| 7 | 引文出现 2 次，span 指向第 2 处 | `SUPPORTED`，`located_span` = 第 2 处，`ambiguous=True` |
| 8 | 引文在 ev_1、声称 ev_2 | `INVALID/QUOTE_NOT_FOUND`（旧校验器会误判通过） |
| 9 | `render_recheck_schema()` 键集合双向相等 | 与解析器读取集合完全一致 |
| 10 | 无定位器只有旧校验器 | 行为与改前一致（回归保护） |
| 11 | 覆盖计数走 **expected 路径**，含一个 `QUOTE_NOT_FOUND` | 该候选进 `not_checked`；`invalid_by_reason` 另有诊断计数（复审 R4） |
| 12 | `located` 计数 | 按 `located_span` 统计，不受 `supporting_span` 影响 |
| 13 | 同一结论、位置从错到对 | 账本接受第二条（`apply_key` 含位置） |

**必须同批改造的既有用例**（复审 R7，清单写在这里，避免"测试绿但语义错"）：

- `backend/tests/test_vnext_resolution.py`：`_payload()` 的 span 语义、`:80`/`:85` 断言、`:157-158` span=None 用例
- `backend/tests/test_vnext_review_round2.py:72-75`（span=None 期望 INVALID）、`:96-104`
- `backend/tests/test_vnext_runner.py:41-54`、`:70`、`:83`、`:97`、`:115`、`:131`、`:146`、`:158`
- `scripts/acceptance_vnext.py:251-264`

**禁止**为少改用例而保留"span 必填"双轨（`docs/PLAN-BC` §4 已明令）。

## 5. 分步与提交

1. **S-1（零行为变化，先做）**：`RECHECK_MODEL_KEYS` + `render_recheck_schema()`；解析器改走常量；用例 #9。
2. **S-2（行为变化，主菜）**：`locate_quote`（复用 `citations.verify_citation`）+ `locate` 回调 +
   `located_span`/`reported_span`/`ambiguous` + 新 reason 码 + `:349` 计数修正 + 覆盖计数诊断计数；
   用例 #1–#8、#10–#12；同批改 §4 列出的既有用例与 `acceptance_vnext.py`。
3. **S-3（收尾）**：`run_recheck` 新增 `evidence_text` 与 `locate`；`apply_key` 纳入位置（#13）；
   两个测量脚本跟着改。

每步独立提交、独立跑相关集；三步合并后跑一次全量。

## 6. 验收门槛

- 全量 `python -m pytest -o addopts= -q`：**≥1485 passed**。
- `python scripts/acceptance_vnext.py`：**32/32**。
- **离线构造样例**复现 C2 的失败模式：从 `docs/MEASURE-C2-2026-10-31.json` 取 11 条
  `SUPPORTED→INVALID` 的 `candidate_id`，用其章节正文与标注引文重新构造"引文对、span 错"的样例，
  断言改后判 `SUPPORTED`。**注意**（复审 R9）：C2 的 JSON rows 未存 `supporting_quote`，
  所以这些是**构造样例**，报告里必须如此标注，不得写成"原样复现"。
- `grep`：`resolution.py` 内除 `RECHECK_MODEL_KEYS` 定义处与 docstring 外，不再出现裸键名。

## 7. 风险与取舍

| 风险 | 处置 |
| --- | --- |
| 门禁从"quote ∈ span"放宽为"quote ∈ 整段" | 只接受 `EXACT/NORMALIZED/RELOCATED`；拒绝 `SEGMENTED/FUZZY`；10 字下限；span 与引文矛盾**判失败**而非留痕 |
| 短引文在长段里几乎必然命中 | 实测：C2 的 30 条引文在同章出现次数**全部为 1**，平均 15.2 字；`这一日无事` 全语料 13 次 → 靠下限 + 歧义判据挡住退化 |
| 歧义取首次可能错配说话人 | 优先用 `reported_span` 破歧义；破不了 → `INSUFFICIENT`（不猜） |
| 新 reason 影响统计口径 | 已改非目标：补 expected 路径用例；`not_checked` 语义不变，`invalid_by_reason` 保留诊断计数（复审 R4） |
| 旧校验器路径被悄悄废弃 | 保留退化模式 + 用例 #10；契约文档标注"退化模式" |
| 契约文档没跟上 | 同批改 `docs/data-contract-vnext.md:516-518`（"四件套"→"三件套 + 程序定位"）（复审 R5） |

## 8. 复审结论（2026-10-31，NEEDS_REVISION → 已并入本文 v2.0）

| 项 | 处置 |
| --- | --- |
| R1 `INVALID` 可重试说法失真 | 已改：仅 3/12，并为 9 分支统一设定 retryable（§2.2） |
| R2 `:349` 用 `supporting_span` 计数 | 已改：计数改读 `located_span`，与 S-2 同提交（§2.3） |
| R3 `AMBIGUOUS_QUOTE` 归 INVALID 与用例冲突 | 已改：归 `INSUFFICIENT`，只作标记 + 计数（§2.2） |
| R4 "不改覆盖计数"站不住 | 已删该非目标，补 expected 路径用例 #11（§4） |
| R5 契约文档漏改 | 已列入 §7，与 S-2 同批 |
| R6 §3.2 顺序错、漏两步 | 已按代码重写（§3.2） |
| R7 用例清单不全 | 已列四处（§4） |
| R8 runner 无正文来源 | 已加 `evidence_text` 参数（§3.3、S-3） |
| R9 "11 条全变 SUPPORTED"不可证 | 已改为构造样例并强制标注（§6） |
| R10 `str.count` 漏重叠、无下限 | 已改递推计数 + 10 字下限（§3.1、§2.2） |
| R11 扁平 Mapping / 子串判定 | 已改嵌套结构 + JSON key 集合双向相等（§3.4） |
| R12 `apply_key` 不含位置 | 已纳入 `located_span`（§3.6、用例 #13） |
| 附加：复用 `citations.verify_citation` | 已采纳，不新写 `str.find`（§3.1） |

## 9. 复审二（v2.1）：三条致命矛盾与七处补漏

第二轮只读复审判 **NEEDS_REVISION**，指出 v2.0 有三处会让整批收益归零或自相矛盾。**本节覆盖上文的对应段落**，与上文冲突时以本节为准。

### 9.1 【致命】`SPAN_DISAGREES` 会让收益归零 → 删除该 reason

v2.0 §2.1 写"给了 span 但窗口里没有该引文 ⇒ 判失败"。复审实读 C2 的 11 条失败（`reason ∈ {MISSING_FIELD, QUOTE_OUT_OF_RANGE}`）指出：**它们正是"窗口不含引文"这一类**。按 v2.0 规则仍判 `INVALID` ⇒ 55% 的收益为 0，D-19 白做。

**改为**：`reported_span` **纯提示，永不判失败**。

| 情形 | 处置 |
| --- | --- |
| span 窗口内含该引文恰好一次 | 采纳它（`located_span = reported_span`） |
| span 窗口内含多次 | 用它破歧义（取窗口内那一次） |
| span 窗口内不含该引文 / span 形状非法 | **忽略 span**，走程序定位（`span_ignored=True`） |
| 程序定位也找不到 | `INVALID/QUOTE_NOT_FOUND` |

对应的反伪造防线不再是"span 一致"，而是**三条**：引文必须逐字存在（`EXACT/NORMALIZED/RELOCATED`）、长度 ≥10 字、歧义需 span 破或判 `INSUFFICIENT`。这与 §7 的风险表一致，但**必须删掉 §7 里"span 与引文矛盾判失败"那一行**，改为"span 只作提示"。

### 9.2 【致命】`located_span` 的可信度与 `apply_key`

复审指出 `RELOCATED` 的 `corrected_*` 来自 `citations._to_raw_offset`，该处自述"映射不精准…不作为权威偏移写回"（`citations.py:203-204`）。因此：

- **定位后必须回验**：`chapter_text[corrected_start:corrected_end] == quote`，通过才写 `located_span`；不通过视为"没找到"（`QUOTE_NOT_FOUND`）。这保证"权威位置"由构造保证自洽，而不是相信映射。
- **`apply_key` 改用 `entity|candidate|verdict|evidence_id|sha256(quote)[:12]`**，不用偏移（偏移会因文本微调漂移，导致同一结论被重复应用）。§3.6 的写法作废。

### 9.3 【致命】用例 #11 与守恒式冲突

`resolution.py:269-271` 有 `sum(invalid_by_reason.values()) == invalid`。而 expected 路径下 `retryable=True` 的 `INVALID` 走 `not_checked`（`:380-383`）且**不写** `invalid_by_reason`。所以 v2.0 #11 的"`invalid_by_reason` 另有诊断计数"会破坏守恒。

**改为**：`CoverageCounts` 新增**独立字段** `retried_reasons: dict[str,int]`（不参与任何守恒等式），#11 断言改为"该候选进 `not_checked`，且 `retried_reasons[QUOTE_NOT_FOUND]==1`"。

### 9.4 §3.2 顺序修正（answer 为空在 SUPPORTED 分支内）

v2.0 把"answer 为空"写在 `[以下仅 SUPPORTED]` 之前，而代码里它在 SUPPORTED 分支内（`resolution.py:133-138`），其余三枚举允许空答案（`:201-207`）。照 v2.0 实现会把空答案的 `DIFFERENT_ANSWER`/`INSUFFICIENT` 误判成 `INVALID`。

**顺序改为**：`… → verdict 枚举 → answer 取回（不做判据） → if verdict==SUPPORTED: {answer 非空 → material → performed → counter_quotes → evidence_id → quote → 长度 → 定位 → checked_evidence_ids}`.

`supporting_span` 值域：形状非法即 `reported_span=None`（忽略），**不判失败**（与 9.1 一致）。

### 9.5 其余补漏

| 项 | 处置 |
| --- | --- |
| `evidence_text` 必须与 `allowed_evidence_ids` 成对 | `run_recheck` 里二者同时给才启用 `locate`；只给一个 → 抛 `ValueError`（配置错误不该静默降级）。`evidence_text` 返回空串 → 该单元 `INVALID/QUOTE_NOT_FOUND` |
| 歧义计数的作用域 | 一律在**原文**（raw `chapter_text`）上数，规范化只发生在 `verify_citation` 内部；span 破歧义也用原文坐标 |
| `AMBIGUOUS_QUOTE` 被当终态缓存 | 标 `retryable=True`（虽然归属 INSUFFICIENT）→ `checked=False`，下一轮会再问；计划里写明"歧义不是结论，是可重试的未核对" |
| R7 漏的调用点 | 补 `scripts/acceptance_vnext.py:166-178`、`:217`、`test_vnext_resolution.py:174-176`、`scripts/measure_c2_annotations.py:125-127`、`scripts/measure_c3_sampling.py:255-262` |
| P0 落不到 prompt | 两个测量脚本的 SYSTEM/SCHEMA 改为**渲染自 `render_recheck_schema()`**，不再手写字段列表（这正是 109k token 事故的根因） |
| 行号引用 | `citations.py` 区间是 109-197；`CitationCheck` 字段名 `match_kind`（值大写字符串，`ACCEPTED_CITATION_KINDS` 可匹配） |
| C2 JSON 键名 | 是 `id` 不是 `candidate_id`；且 rows 无引文字段 → §6 的样例只能**构造**，不能"原样复现"（v2.0 已标注，保留） |
| §6 验收措辞 | 改为"引文对、**不给 span**"与"引文对、**给错 span**"两种构造样例都必须判 `SUPPORTED`，与用例 #1/#2 对齐 |

### 9.6 v2.1 判据速查（实现时以这张表为准）

```
SUPPORTED 需要同时满足：
1. verdict == SUPPORTED
2. answer 非空
3. 无 material_insufficient_reason
4. counter_search.performed is True 且 counter_quotes 为空
5. evidence_id 属于本任务
6. supporting_quote 非空且长度 ≥ 10
7. 引文能在该 evidence 原文里定位（EXACT/NORMALIZED/RELOCATED），并回验 text[s:e]==quote
8. checked_evidence_ids 非空且全部属于本任务
9. 若引文出现 ≥2 次：reported_span 能指到其中一次，否则 INVALID/AMBIGUOUS_QUOTE
```

## 10. 复审三（v2.2）：四处硬伤与最终定稿

第三轮复审（只审 §9 增量）判 NEEDS_REVISION，四处成立。**本节再次覆盖上文**；与 §9 冲突时以本节为准。

### 10.1 回验必须用规范化比较（否则自毁）

`citations._to_raw_offset`（`citations.py:200-215`）只跳过 P/Z 类字符，映射出的原文切片可能夹着标点：规范化引文 `ab` 落在原文 `a、b` 上会得到 `a、`。若用严等回验 `text[cs:ce] == quote`，`§3.1` 明确接受的 `NORMALIZED`/`RELOCATED` 会被全部打成 `QUOTE_NOT_FOUND`——正好抹掉本计划要修的失败类型。

**定稿**：`EXACT` 用严等回验；`NORMALIZED`/`RELOCATED` 用双侧规范化后比较：`_normalize(text[cs:ce]) == _normalize(quote)`（`_normalize` 取自 `citations`）。

### 10.2 `quote_hash` 进判据，位置不再进 `apply_key`

`CandidateVerdict`（`resolution.py:67-95`）没有 `supporting_quote` 字段，`runner._apply_key`（`runner.py:226-232`）拿不到引文，`sha256(quote)` 无从计算。

**定稿**：`CandidateVerdict` 新增 `quote_hash: str = ""`（`parse_recheck` 内部算 `sha256(quote)[:12]`）；`apply_key = entity|candidate|verdict|evidence_id|quote_hash`。

**用例 #13 作废**（原写"apply_key 含位置"）。替代为：**同一结论、不同引文 → apply_key 不同 → 两条都记账**；**同一结论、同一引文、位置从错到对 → apply_key 相同 → 第二条 `applied=False`**，位置修正不重放（位置是派生物，不是结论；结论不变就不该重复应用）。

### 10.3 歧义不可解析时用 `INVALID`，不用 `INSUFFICIENT`

`INSUFFICIENT` 属 `tasks.py:33` 的 `REUSABLE`，`recheck_plan.py:93` 会直接复用缓存结果 ⇒ 歧义一旦落 `INSUFFICIENT` 就**永久粘住**，`retryable` 只影响 `checked`/`not_checked`，不影响复用。

**定稿**：`AMBIGUOUS_QUOTE` 归 **`INVALID`**（不缓存、下一轮重问，模型有机会补 span）。这**部分推翻 R3**：R3 反对的是"有 span 能破歧义却仍判 INVALID"，那种情形现在是 `SUPPORTED`；只有"破不了歧义"才落 `INVALID`。

### 10.4 §9.1 作废清单补齐 + 判定顺序唯一化

三处必须一并作废（改前仍写着 `SPAN_DISAGREES`）：§2.2 表行（`plan:74`）、§3.2（`plan:160`）、§4 用例 #3（`plan:238`）。

**唯一判定顺序**（取代 §3.2 与 §9.6 两处）：

```
verdict 枚举 → [SUPPORTED] answer 非空 → material → performed → counter_quotes
→ evidence_id → quote 非空且 ≥10 字 → 定位（EXACT/NORMALIZED/RELOCATED + 回验）
→ 歧义：occurrences>1 时，若 reported_span 恰指其中一次则采纳，
        否则 INVALID/AMBIGUOUS_QUOTE
→ checked_evidence_ids → SUPPORTED
```

§9.1 表中"span 窗口内含多次→取窗口内那一次"措辞作废：span 只用于①窗口内恰含一次则该处即定位结果；②窗口内含多次则取窗口内那一次；③窗口内不含则忽略。其中②与"occurrences>1 需span定位"是同一件事，不构成矛盾。

### 10.5 最终交付判据（实现只看这一张）

| # | 条件（SUPPORTED 全满足） |
| --- | --- |
| 1 | `verdict == "SUPPORTED"` |
| 2 | `answer` 非空（此项只在 SUPPORTED 分支检查） |
| 3 | 无 `material_insufficient_reason` |
| 4 | `counter_search.performed is True` 且 `counter_quotes` 为空 |
| 5 | `evidence_id` 属于本任务 |
| 6 | `supporting_quote` 非空且 `len >= 10` |
| 7 | 定位成功且回验通过（`EXACT` 严等 / 其余双侧规范化相等） |
| 8 | 引文出现 ≥2 次时，`reported_span` 能指到其中一次 |
| 9 | `checked_evidence_ids` 非空且全部属于本任务 |

任一条不满足时的 reason：2→`EMPTY_ANSWER`、3/4→`INSUFFICIENT`(对应码)、5→`FOREIGN_ID`、6→`NO_SUPPORTING_QUOTE`/`QUOTE_TOO_SHORT`、7→`QUOTE_NOT_FOUND`、8→`AMBIGUOUS_QUOTE`、9→`COUNTER_SEARCH_UNVERIFIED`。


