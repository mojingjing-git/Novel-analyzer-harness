# PLAN 补丁 v2 · S6「原文证据与全自动裁决」

- 状态：已合入[主计划文档 v2.1](PLAN-v2-long-novel-reliability-2026-10-30.md)（2026-10-30）；这是文档合入，不代表业务实现完成
- 基线：`3a27b6c`
- 修订：v2 相对 v1 的变更见 §0；v3 增量（复检覆盖披露 + 未复检状态）见 §13，已同步并入 v2.1
- 目标：**取消人工确认/驳回/撤销，改为程序按固定规则自动裁决**
- 不做承诺：**不承诺零误回收**。最终状态命名为 `AUTO_RESOLVED`，语义是"有原文依据的自动判定"，不是"已证明无误"

> 本文件保留补丁 v2 的提案记录。实施以主计划 v2.1 为准；合入时的定位、截断、Token 估算与评测边界澄清见[合入对照](REPORT-plan-v2-autonomous-merge-2026-10-30.md)。逐行变化见[diff](patches/PLAN-v2-autonomous-resolution-2026-10-30.patch)。

---

## 0. 相对 v1 的修订点

| # | v1 的问题 | v2 的处理 |
|---|---|---|
| 1 | "答案语义相容"无可行方法，且与"裁决器无模型调用"矛盾 | 选定接口 B：**由 recheck 承担答案比较**，程序只比较枚举（§4.1、§4.7） |
| 2 | `evidence_id` 按顺序生成，重蹈 R2 覆辙 | 改为来源锚点哈希，展示序号另设（§4.3） |
| 3 | 状态表把"候选无效"直接映射为实体 `OPEN`，会抹掉已成立的结论 | 引入候选级记录 + 实体级归约（§5） |
| 4 | 反证搜索被重要度放宽，与"统一证据标准"矛盾；上下文窗口定义含糊 | 反证搜索**始终执行**；窗口按句段取材并显式标记截断（§4.4、§4.5） |
| 5 | `BUDGET_SKIPPED` 与语义终态混为一谈，预算提高后无法继续 | 区分"可复用终态"与"未执行"，后者不写完成标记（§6） |
| 6 | T21 要求"状态不随输入变化而变" | 改为"ID 不被挪用、结论不串实体、旧结论可被取代但留痕"（§9） |
| 7 | 由反例推出"词面分数与回收负相关" | **删除该结论**，仅保留"不是可靠准入条件"（§1.1） |
| 8 | 把"同一悬念的未解决复述"当成去重隐患 | **纠正**：那本应合并为同一实体；真正要测的是"同一对象的不同悬念不得合并"（§1.2） |

---

## 1. 前提修正

### 1.1 词面分数不是可靠的准入条件

用仓库现有函数 `backend/utils/text_utils.py:162 _extract_keywords`（2–4 字中文关键词）与 `difflib.SequenceMatcher` 实测：

| 候选文本 | SequenceMatcher | keyword Jaccard | 语义 |
|---|---|---|---|
| `那顶失踪王冠究竟藏在哪里，漕运账册底下是否隐藏着答案？至今仍无人知道。` | **0.871** | **0.808** | 未回收（复述原问题） |
| `他撒谎说：那顶失踪王冠藏在漕运账册底下，我亲眼见过。` | 0.528 | 0.281 | 未回收（谎言） |
| `他梦见那顶失踪王冠藏在漕运账册底下。` | 0.622 | 0.301 | 未回收（梦境） |
| `若是那顶失踪王冠藏在漕运账册底下，又当如何？` | 0.612 | 0.291 | 未回收（假设） |
| `那顶失踪王冠早已熔成金锭，此刻正压在漕运账册底下。` | 0.500 | 0.250 | ✅ 真回收 |

**可成立的结论**：词面分数**不能**作为准入闸门——最高分出现在明确的未回收例子上，且否定、假设、谎言、梦境都可能取得较高分数。

**不可成立的推论**：这些样本**不足以**证明"词面分数与回收负相关"，也不足以断言"回收必然降低重合度"。真实语料中回收段落常与悬念共享大量实体词，样本太小、构造偏差明显。该结论已从设计依据中删除。

**因此**：词面分数仅用于候选排序、异常提示、去重（去重场景的语义与准入不同，见 §1.2）；"是否回答了原悬念"由模型语义判断完成，但**自动化**，不需要人。

### 1.2 同一悬念的未解决复述，应当合并

v1 把"王冠藏在哪？"与"王冠藏在哪？至今无人知道"视为去重隐患，这是错的。

正确语义：后者是**同一悬念的再次提及**，应当：

- 归属**同一** `entity_id`
- 新增一条 `occurrence`（记录新的提及位置）
- 更新 `recency_state` / 最近提及位置
- `resolution_state` 保持 `OPEN`

**真正需要测试的是另一种情况**：同一对象、**不同悬念**不得仅凭相似度合并。例如：

- `谁藏了王冠`（施事者未知）
- `王冠藏在哪里`（位置未知）

二者共享"王冠"这一实体，但回答的是两个不同问题。若被合并，回收其一会错误地关闭另一个。这应作为 S5 的验收项。

### 1.3 两阶段不构成独立验证

批次调和（`backend/services/final_summary.py:139`）与全书复检（`:242`）输入同源于卷摘要，会共同继承摘要中的错误。**不得**按"两个独立判断"计算可靠性。

修正方向是改变分工：让第二阶段回到**原文**核对，而不是"再看一遍摘要"（§4.1）。

### 1.4 复用既有阶段 ≠ 零额外成本

recheck 从"看卷摘要"变为"看卷摘要 + 原文证据片段 + 上下文"，候选集合从"仅 `OPEN`"扩大到"`OPEN` + 待核对候选"：

- 每条候选附带上下文需要实际字符预算
- 批次数可能翻倍

必须给 recheck 增加预算上限与 Token 装箱（§10），**不得**声明"零额外调用"。

---

## 2. 补丁 P1 · 状态枚举与授权者

### 原文（v2 §2.3 约束最后一条）

> 默认仅人工操作可设置 `CONFIRMED_RESOLVED`

### 替换为

```
resolution_state 取值：OPEN / PROPOSED_RESOLVED / AUTO_RESOLVED

- OPEN               当前不存在有效候选证据
- PROPOSED_RESOLVED  存在有效候选，但语义核对未通过、或结论冲突、或材料不足
- AUTO_RESOLVED      存在通过全部自动条件的候选（第 4.6 节）

授权者：resolution_state 是**实体级归约结果**（第 5 节），由程序依据候选级裁决计算得出。
        不提供人工确认入口。预留 decision_actor 字段，当前只会产生 evidence_verifier。

术语约束：文档、API、前端、报告中禁止出现"已确认回收 / confirmed / verified resolved"
        等暗示"已证明无误"的措辞；统一使用"自动判定已回收 · 查看原文依据"。

注意：实体状态不由单个候选直接写入。无效候选只影响它自身，不得把实体降级（第 5 节）。
```

> `CONFIRMED_RESOLVED` 一词从全部产物中移除。

---

## 3. 接口选择说明（对应修订点 1）

两阶段之间"答案是否相容"必须有一个可执行的判定方法。可选方案：

**方案 A：结构化答案 + 程序比较**
要求两阶段都输出 `question_id` + 结构化答案（实体/属性/值）+ 事件范围，程序只比较明确支持的结构。缺点：需要先建立悬念-答案的本体结构（谁藏、藏在哪里、何时、为何……），属于大工程量，且自由文本答案难以稳定结构化。

**方案 B：由 recheck 承担答案比较（本版采用）**
reconciliation 提出候选答案；recheck 在原文上下文中判断"该假设是否被这段原文支持"，直接输出裁决枚举。程序**只比较枚举值**，不比较自然语言。

采用 B 的理由：

1. 裁决器保持零模型调用——比较发生在 recheck 内部，输出是枚举
2. 避免"两次独立语义判断再比较"带来的两套判断标准不一致
3. recheck 本来就要读原文，让它同时回答"这段原文支持哪个答案"不增加调用次数

代价与缓解：recheck 会看到候选答案，存在**锚定风险**。缓解措施：

- 候选答案只能以"**待核对假设（未经核实）**"形式出现，不带置信度、不带第一阶段解释、不带事件时间
- recheck 被强制要求输出反证搜索结果（§4.5），并允许返回 `DIFFERENT_ANSWER` 与 `CONTRADICTED`
- 锚定偏差的度量放到**研发评测**里做（盲测抽样），不进入用户运行流程（§11）

---

## 4. 自动裁决契约

### 4.1 两阶段分工

| 阶段 | 任务 | 主要输入 | 明确禁止 |
|---|---|---|---|
| reconciliation | 提出回收候选 + 说出**原悬念得到了什么答案** | 当前卷摘要、截至本卷的伏笔清单 | 不得自评"高置信即通过"；不得直接写状态 |
| recheck | 核对**原文**是否支持该答案，并**主动寻找反证** | 悬念原始 clue、程序定位的证据 quote、上下文窗口、候选答案（作为待核对假设） | **不得看到** reconciliation 的结论、置信度、解释、事件时间 |

recheck 输出结构固定：

```json
{
  "evidence_id": "ev_3f9a1c72b40e",
  "verdict": "SUPPORTED | DIFFERENT_ANSWER | CONTRADICTED | INSUFFICIENT",
  "supporting_span": { "start": 1042, "end": 1078 },
  "answer": "王冠已被熔毁",
  "counter_evidence": null,
  "counter_search_performed": true,
  "material_insufficient_reason": null,
  "reason": "..."
}
```

其中 `supporting_span` 必须落在**已提供的上下文窗口内**，且 `answer` 必须能由该 span 直接读出。

### 4.2 引用定位由程序完成

模型**不计算字符偏移**，只返回：

```json
{ "chapter_id": "ch_0340", "quote": "那顶王冠早已熔成金锭" }
```

程序流程：

```
1. 用 chapter 的 content_hash 确认原文版本未变
2. 在该章规范文本中查找 quote
3. 唯一匹配      → 生成 start/end 与 evidence_id，标记 EVIDENCE_LOCATED
   无匹配        → 候选 INVALID（reason = QUOTE_NOT_FOUND）
   多处匹配      → 本地扩窗重定位（见下），仍不唯一则候选 INVALID（reason = QUOTE_AMBIGUOUS）
4. 生成上下文窗口（§4.4）
```

**关于"扩窗重试一次"与 T19"不做第二次猜测"的矛盾（修订点 5 后半）**：

二者不冲突，必须写清区别：

- **允许**的是**本地定位重试**：扩大查找窗口 / 尝试规范化变体（去空白、全半角、引号归一），目的是把"多处匹配"收敛为唯一匹配。**不调用模型**，不计入语义重试。
- **不允许**的是**语义猜测重试**：对同一候选再次询问模型"你觉得是不是回收"。一次否决即终态。

判定为 INVALID 后不再重试。

### 4.3 `evidence_id` 必须由来源锚点生成

**不得**使用 `ev_0001` 这类顺序编号——新增早期证据会改变后续所有 ID，与 R2 同类缺陷。

```
evidence_id = "ev_" + sha256(
    book_id | chapter_id | content_hash | start | end
)[:12]
```

- 同一段原文 → 永远同一 ID
- 显示用序号另设 `display_index`，仅用于界面
- 跨章先后比较使用 **`(章节故事顺序, 章内偏移)` 元组**，其中章节故事顺序取 `chapters.json` 的规范顺序索引，**不得**直接比较不同章节的偏移数值

### 4.4 上下文窗口：按句段取材，显式记录截断

固定"±400 字"会切断完整话语（例如把"他撒谎说："留在窗口外）。规则改为：

```
1. 以 quote 所在句为锚
2. 向前、向后各扩展到完整句段（以 。！？…；\n 为界）
3. 直到覆盖 PRE_CHARS / POST_CHARS 的下限，或到达段落边界
4. 硬上限 MAX_CONTEXT_CHARS
5. 记录 context_start / context_end / context_truncated / chars_used
```

约束：

- 窗口必须以**完整句**起止，不允许以半个句子开头
- 若在到达 PRE_CHARS 下限前就撞到段落/章节边界，`context_truncated = true`，并记录实际检查范围
- 若 quote 起始位于句中（如引用从"王冠"开始但句子更早），必须把该句完整包含进来
- `context_truncated = true` 时，recheck **不得**返回 `SUPPORTED`，除非 `supporting_span` 完整落在窗口内且语义自足；否则返回 `INSUFFICIENT`

预算口径分离（原 v1 把两个不同限制混为一谈）：

| 口径 | 含义 |
|---|---|
| `MAX_CONTEXT_CHARS` | 单条候选的上下文硬上限 |
| 批次 Token 预算 | 每批所有候选的上下文 + 卷摘要合计上限，按 Token 装箱（§10） |

### 4.5 反证搜索始终执行

**删除** v1 §3.5 中"重要度决定是否强制反证搜索"的放宽。既然证据标准统一，反证搜索就是**统一必做项**。重要度只影响预算分配与处理优先级，不影响是否搜索。

`counter_evidence = null` **本身不构成放行理由**。必须同时满足：

- `counter_search_performed = true`
- `counter_evidence = null`
- `material_insufficient_reason = null`
- `context_truncated = false`，或虽截断但 `supporting_span` 自足（§4.4）

任一不满足 → `INSUFFICIENT`。

### 4.6 自动通过条件

全部满足才把候选记为 `SUPPORTED`：

1. **来源**：`chapter_id` 存在、`content_hash` 与当前一致、`block_signature` 未变
2. **引用**：`quote` 在规范原文中唯一匹配
3. **顺序**：回收位置的 `(故事顺序, 偏移)` **晚于**埋设位置（同章先埋后收合法）
4. **语义**：recheck 返回 `SUPPORTED`，且 `supporting_span` 在窗口内，`answer` 可由该 span 直接读出
5. **反证**：满足 §4.5 全部四项

不满足任一条 → 按 §4.7 落到对应枚举，**不重试、不等人**。

### 4.7 裁决器只比较枚举

程序侧（零模型调用）只做：

```
verdict == SUPPORTED           且 条件 1/2/3/5 通过  → candidate.adjudication = SUPPORTED
verdict == DIFFERENT_ANSWER                          → candidate.adjudication = CONFLICT
verdict == CONTRADICTED                              → candidate.adjudication = CONFLICT
verdict == INSUFFICIENT                              → candidate.adjudication = INSUFFICIENT
条件 1/2 失败                                        → candidate.adjudication = INVALID
```

程序**不比较** `answer` 与 reconciliation 的自然语言答案。二者是否相容完全由 recheck 的 `SUPPORTED / DIFFERENT_ANSWER` 表达。

---

## 5. 候选级记录与实体级归约（对应修订点 3）

### 5.1 候选记录

每个候选独立持久化，互不影响：

```
candidate {
  candidate_id      // uuid，不复用
  entity_id         // 指向伏笔实体，持久身份
  evidence_id
  source_ref        { chapter_id, content_hash, start, end }
  quote
  proposed_answer   // reconciliation 给出的答案（自由文本）
  adjudication      PENDING | SUPPORTED | INSUFFICIENT | CONFLICT | INVALID
  reason_code       // QUOTE_NOT_FOUND / QUOTE_AMBIGUOUS / WRONG_ENTITY /
                    // STALE_SOURCE / ORDER_VIOLATION / NO_COUNTER_SEARCH ...
  counter_search    { performed, found, evidence_id }
  context           { start, end, truncated, chars_used }
  task_key
  strategy_version
}
```

### 5.2 实体级归约

实体状态由**全部当前有效候选**统一归约，而不是某个候选直接写入：

```
valid = [c for c in candidates if c.adjudication != INVALID and c.source_fresh]

if any(c.adjudication == SUPPORTED for c in valid):
        entity.resolution_state = AUTO_RESOLVED
elif any(c.adjudication in (PENDING, INSUFFICIENT, CONFLICT) for c in valid):
        entity.resolution_state = PROPOSED_RESOLVED
else:
        entity.resolution_state = OPEN
```

### 5.3 失效只影响自身

- 收到一条**伪造引用**的新候选 → 该候选记 `INVALID`，**不触发**实体重算到 OPEN
- 只有当**支撑现有结论的那条证据**失效时（`source_fresh` 变 false、或该候选被推翻），才重新归约
- 重新归约保留历史：被取代的结论记 `superseded_by` + `superseded_at`，不覆盖删除

这样修正了 v1 的缺陷：已成立的 `AUTO_RESOLVED` 不会因为之后混入一条垃圾候选而被抹掉。

---

## 6. 状态、缓存与恢复（对应修订点 5）

必须区分**语义终态（可复用）**与**未执行（可续跑）**：

| 情形 | candidate.adjudication / 标记 | 是否写完成标记 | 何时可复用 |
|---|---|---|---|
| 通过全部条件 | `SUPPORTED` | 是 | `task_key` + `strategy_version` + `content_hash` 不变即复用 |
| 语义核验：材料不足 | `INSUFFICIENT` | 是 | 同上 |
| 语义核验：结论冲突 / 反证 | `CONFLICT` | 是 | 同上 |
| 候选无效（引文未命中/多义/错实体/来源过期） | `INVALID` + `reason_code` | 是 | 同上（但来源新鲜后再判） |
| **预算不足未执行** | `PENDING` + `BUDGET_SKIPPED` | **否** | **用户提高预算后必须继续处理** |
| 调用失败 | `PENDING` + `FAILED` | **否** | 按续跑策略重试 |
| 被取消 | `PENDING` + `CANCELLED` | **否** | 重跑时处理 |

关键约束：

- `BUDGET_SKIPPED` **绝不可**写入"已完成"，不得阻止后续更高预算的运行
- 语义终态的复用条件必须包含 `strategy_version` 与 `content_hash`；任一变则重判
- 重判时旧记录保留为历史条目，不原地覆盖

---

## 7. 实体身份与内容指纹

- `entity_id`：**持久化身份**，首次发现伏笔时分配，此后永不改变。**不得**由伏笔描述哈希生成（描述改写会换 ID，造成状态错位，即 R2 根因）
- `content_fingerprint`：用于 `source_ref`、去重分组、缓存 key、变更检测；**不参与身份判定**
- 去重（`text_utils.deduplicate_foreshadows`）产生的是"是否同一实体"的判定，其输出必须映射到**已存在的** `entity_id`，不得重新编号
- 同一实体的多次提及 → 新增 `occurrence`，不改身份（§1.2）

---

## 8. 补丁 P2 · S6 批次定义

### 原文（v2 §4）

> S6：原文证据、候选校验和人工确认闭环

### 替换为

```
S6 · 原文证据与全自动裁决

交付物：
1. 程序侧引用定位器（chapter_id + quote → 唯一匹配 → anchor-hash evidence_id → 上下文窗口）
2. 两阶段分工改造：recheck 不得读取 reconciliation 的结论/置信度/解释/时间，
   仅接受"待核对假设"形式的候选答案
3. 候选级记录 + 实体级归约（第 5 节）；失效只影响自身
4. 自动裁决器（第 4.6/4.7 节，纯枚举比较，零模型调用）
5. 状态与缓存契约（第 6 节），区分语义终态与未执行
6. 前端：只读证据展示（展开显示 quote + 上下文高亮 + 来源章节跳转），
   **不含**确认/驳回/撤销控件
7. 测试 T19 / T21 改写 + 新增 T35–T39

明确不做：人工确认、驳回、撤销、人工覆盖入口、相关 409 冲突处理、审核待办列表
```

### 前端工作量

从"页面级功能"降为"一个只读展开组件 + 措辞调整"。

---

## 9. 补丁 P3 · 测试矩阵

### T19 改写（按失败原因分类，不再统一 PROPOSED）

| 场景 | 期望 |
|---|---|
| 引用在原文中不存在 | 候选 `INVALID` / `QUOTE_NOT_FOUND`；实体状态**不变** |
| 引用多处歧义（扩窗后仍不唯一） | 候选 `INVALID` / `QUOTE_AMBIGUOUS`；实体状态**不变** |
| 实体错配（引用属另一条伏笔） | 候选 `INVALID` / `WRONG_ENTITY`；实体状态**不变** |
| 来源过期（`content_hash` 变化） | 候选标记 `STALE_SOURCE`，待来源稳定后重判；期间不计入归约 |
| 引用存在但语义不支持 | 候选 `INSUFFICIENT`；实体归约为 `PROPOSED_RESOLVED` |
| **已有 `AUTO_RESOLVED` 实体会后收到一条伪造候选** | 实体**保持** `AUTO_RESOLVED`（关键新增断言，对应修订点 3） |

### T21 改写（幂等、可重放、身份不挪用）

| 断言 | 说明 |
|---|---|
| 固定模型返回 / 命中缓存时，两次运行产生相同候选集与实体状态 | 幂等（**不要求**重新调用模型必然得到相同答案） |
| 描述改写、去重分组变化导致输入变化时，**允许**重判并允许状态改变 | 输入变了，结论可变 |
| 重判后旧结论必须留痕（`superseded_by`），不得静默覆盖 | 可追溯 |
| **任何情况下不得把结论从实体 A 挪到实体 B**；`entity_id` 不得被重新分配给另一条故事线 | 身份不挪用（替代原"状态永不改变"） |
| 并发处理同一 `entity_id` 不产生双写或状态倒退 | 并发安全 |

### 新增测试

| 编号 | 名称 | 断言 |
|---|---|---|
| **T35** | 复述型否定 | clue 被后文完整复述并追加"至今仍无人知道" → 不得 `SUPPORTED`（实测 0.871） |
| **T36** | 答案冲突 | reconciliation 提"王冠被熔毁"、recheck 判 `DIFFERENT_ANSWER`（答案"王冠被找回"）→ `CONFLICT`，实体不得 `AUTO_RESOLVED` |
| **T37** | 上下文截断保护 | 引用命中但 `context_truncated=true` 且决定性语句在窗口外 → recheck 必须 `INSUFFICIENT` |
| **T38** | 引用定位三态 | quote 存在/不存在/多处出现 → `SUPPORTED路径` / `QUOTE_NOT_FOUND` / `QUOTE_AMBIGUOUS`；模型给错偏移不影响判定 |
| **T39** | 说谎／梦境／假设负样本 | 三类文本（0.528 / 0.622 / 0.612）均不得 `SUPPORTED` |
| **T40** | 同实体不同悬念 | `谁藏了王冠` 与 `王冠藏在哪里` **不得**仅凭相似度合并为同一 `entity_id`（S5 验收） |
| **T41** | 同悬念复述应合并 | `王冠藏在哪？` 与 `王冠藏在哪？至今无人知道` 必须归**同一** `entity_id`，新增 `occurrence`，`resolution_state` 保持 `OPEN` |
| **T42** | 预算跳过可续跑 | `BUDGET_SKIPPED` 不写完成标记；提高预算后同 `task_key` 必须继续处理 |
| **T43** | 只分析不总结 / 总结被取消 | 账本 `recheck_status` 为 `NOT_RUN`/`STOPPED`；API 与报告显示"未复检"；不得显示为已核对、已完成或"无候选"；未复检时对外不出现 `AUTO_RESOLVED` |
| **T44** | 候选集为空 / 全部 INVALID / 全部 STALE | 复检仍记 `COMPLETED` 并写覆盖计数（候选 0、有效 0、各 reason_code 与跳过原因）；报告显示"本次无有效候选"；禁止沿用 `无活跃伏笔，跳过全书复检` 的静默返回 |

### 现有测试处置（硬规则）

v2 计划须新增：

> 旧契约测试若锁定已判定为缺陷的行为，必须重写或删除，**禁止**为使测试通过而保留旧语义。

已知至少两处：

- `backend/tests/test_memory_state_rolling.py:43-54`（断言 `chapter_limit=1` 的快照 `== STRUCTURED`）
- `backend/services/final_summary.py:113` 的 prompt 禁止"同一批次回收"，需同步删改（同章先埋后收合法）

---

## 10. 补丁 P4 · 预算与装箱

v2 §3.2 需新增：

```
recheck 预算：
- 候选上限：每批 ≤ foreshadow_recheck_batch_size（默认 40）
- 单条上下文硬上限：MAX_CONTEXT_CHARS（建议 1200，按句段取材，记录 chars_used）
- 批次装箱：按 Token 估算（中文按 字符数 / 1.6）累加
  卷摘要 + Σ(候选 clue + quote + 上下文) ≤ RECHECK_BATCH_TOKEN_BUDGET
  超限则切下一批，不得用"条数 × 固定字数"估算
- recheck 总调用数计入 analysis 预算组；超预算记 BUDGET_SKIPPED（未执行态，可续跑）
- 不得声明"零额外调用"；实现后以实测 token 对比为准
semantic_review_max_calls 默认保持 0（语义核对已并入 recheck，非额外调用）
```

---

## 11. 残余风险、不承诺项与研发评测

| 项 | 承诺 | 不承诺 |
|---|---|---|
| 引用真实性 | 程序逐字校验 + 唯一匹配，模型无法伪造 | 不保证角色在该引用中说的是真话 |
| 语义回收 | recheck 在原文上下文中判断，可自动完成 | **不保证零误回收**；假阳与假阴并存 |
| 可靠性 | 五条件全过才记 `SUPPORTED` | 不宣称 `AUTO_RESOLVED` 等同于人工确认 |
| 成本 | 有 Token 装箱与预算上限 | 不承诺与改造前等成本 |
| 流程 | 全自动，无人工节点 | 不提供人工纠错入口（保留 `decision_actor` 字段以便未来扩展） |
| 锚定风险 | 有 §3 的缓解措施 | 不承诺消除；偏差度量在研发评测中做 |

**研发评测（不进用户运行流程）**：对已产出 `AUTO_RESOLVED` 的样本做**盲测抽样**——重新运行 recheck 但不给候选答案，比较两次裁决的一致率，用一致率估计锚定偏差。该抽样仅在评测/标注流程中执行，不写入产品的任务状态。

---

## 12. 待合入清单（扩展自 v1，共 15 处）

| # | 目标位置 | 动作 |
|---|---|---|
| 1 | v2 §2.3 约束最后一条 | 按本项目 §2 替换 |
| 2 | v2 §2.3 `resolution_state` 枚举 | 按本项目 §2 替换（去 `CONFIRMED_RESOLVED`） |
| 3 | v2 §2.4 缓存/任务状态 | 补入"语义终态 vs 未执行"区分（本项目 §6） |
| 4 | v2 §3.1 不变量 | 新增"无效候选不得降级已成立结论""`entity_id` 不得挪用" |
| 5 | v2 §3.2 预算 | 按本项目 §10 追加 Token 装箱 |
| 6 | v2 §4 S5 | 补 T40 / T41（同一对象不同悬念不得合并；同悬念复述应合并） |
| 7 | v2 §4 S6 | 按本项目 §8 替换 |
| 8 | v2 §4 S7 | 缓存与收敛规则按本项目 §6 对齐 |
| 9 | v2 §4 S9 | 移除"低重要度单阶段通过"等分级放宽表述 |
| 10 | v2 §4 S10 | 移除人工决策相关发布项 |
| 11 | v2 §5 T19 | 按本项目 §9 改写 |
| 12 | v2 §5 T21 | 按本项目 §9 改写 |
| 13 | v2 §5 测试矩阵 | 追加 T35–T44 |
| 14 | v2 §7 覆盖表 | C-05 / C-10 行改指 T19–T21、T35–T42；新增 C-27 定义（当前两文档均未定义） |
| 15 | v2 §8 完成定义（Done） | 移除"人工确认闭环""审核待办"等人工语义；保留"标注评测"为研发流程项，明确不属于产品运行流程 |

另需补：全计划范围检查——`§1.2 保留能力` 中的 pause/cancel 与人工无关，可保留；凡出现"人工确认 / 人工驳回 / 审核队列 / 待办"的表述一律清除或改为"只读查看"。

---

## 13. v3 增量 · 复检覆盖披露与"未复检"状态（已直接并入 v2.1）

### 13.1 背景

v2 补丁合入 v2.1 后，发现两处**用户可感知的静默**仍未覆盖，二者都不是判据缺陷，而是"这一步到底跑没跑"无法被外部核对：

1. **伏笔复检在总结阶段**（`backend/services/final_summary.py:1679`）。只跑逐章分析、不跑总结（或 `auto_summary` 关闭、总结被取消）的书，账本停在阶段 1 的中间态，但产物中没有任何"未复检"的标记。
2. **候选集为空时静默返回**。现有代码在 `final_summary.py:1384-1387` 以 `无活跃伏笔，跳过全书复检` 直接 `return`，只写一行日志。真实数据下 `active = 0`，导致这一步每次都被跳过，而报告里的"活跃 0 条"看起来像一个正常结论。

v2.1 的 S6 第 10 条已要求区分"尚无有效候选"与"本次未核对"，但缺少两项可执行约束：**覆盖计数必须落盘**、**未运行总结必须有自己的状态值**。

### 13.2 并入内容（v2.1 实际改动）

| v2.1 位置 | 改动 |
|---|---|
| §4 S6 第 11 条（新增） | 复检覆盖必须计数披露：候选总数/提交核对/定位成功/SUPPORTED/INSUFFICIENT/CONFLICT/INVALID（分 reason_code）/STALE/未核对数及原因/调用数与花费；汇总缺失则不得标记完成；候选为空也要记 `COMPLETED` 且计数为 0 |
| §4 S6 第 12 条（新增） | 账本持久化 `recheck_status`（`NOT_RUN`/`RUNNING`/`COMPLETED`/`PARTIAL`/`STOPPED`）；仅完成分析时必为 `NOT_RUN`/`STOPPED`；API/Timeline/报告一律显示"未复检"；阶段 1 只产候选，不得在未复检时对外呈现 `AUTO_RESOLVED` |
| §4 S6 门槛（追加） | 每次复检必须留下可核对的覆盖计数；未运行总结的书必须在产物中显示"未复检" |
| §5 测试矩阵（新增 T43、T44） | 见本文件 §9 |
| §7 覆盖表 C-05/C-10/R4 行 | 验收项由 `T19–T21、T24、T35–T39、T42` 扩为 `…、T42–T44` |

### 13.3 与既有设计的关系

- 不改变接口 B、不改变裁决规则、不增加模型调用
- 覆盖计数是**程序侧统计**，复用已有的 `_emit_progress` 事件即可承载
- `recheck_status` 与 `resolution_state` 正交：前者描述"任务跑没跑完"，后者描述"悬念解决没解决"。二者不得互相代替（对应 v2.1 §2.3 的"任务完成与伏笔是否解决是独立状态"）
- 取消现有"无活跃伏笔即 return"的路径：改为**先归约候选集，再记录 0 条结果**，使"没有候选"和"没有运行"在产物中永远可区分

### 13.4 验收现场应该看到什么

| 运行情形 | 报告里应出现 |
|---|---|
| 只跑了分析 | 伏笔段显示"未复检（尚未运行总结）"，无 `AUTO_RESOLVED` |
| 总结跑到阶段 2 被取消 | "复检未完成（已停止）"，已完成的候选保留状态 |
| 复检正常跑完、有候选 | "本次复检覆盖 N 条候选：自动判定已回收 a / 证据不足 b / 冲突 c / 无效 d" |
| 复检正常跑完、无候选 | "本次无有效候选（0 条）"，而非"无活跃伏笔，跳过" |
| 预算不足 | "N 条因预算不足未核对，可调高预算续跑" |

