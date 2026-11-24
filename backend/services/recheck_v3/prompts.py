"""伏笔复检 V3 常量与中立通用 Prompt 模板（Layer 1：零内部依赖）。"""

from __future__ import annotations

DEFAULT_V3_BATCH_SIZE = 8
DEFAULT_V3_COMPACT_BATCH_SIZE = 4
DEFAULT_V3_CONCURRENCY = 8
DEFAULT_V3_MAX_CALLS = 1200
DEFAULT_V3_POOL_MAX_CHARS = 95_000
DEFAULT_V3_COMPACT_POOL_MAX_CHARS = 45_000
V3_PROMPT_HARD_CAP_CHARS = 110_000
U1_SUPERCHUNK_TARGET_CHARS = 58_000
U1_COMPACT_SUPERCHUNK_TARGET_CHARS = 30_000
U1_SPLIT_BATCH_SIZE = 50
U1_COMPACT_SPLIT_BATCH_SIZE = 25
U1_ROUTE_Q_BATCH = 75
U1_COMPACT_ROUTE_Q_BATCH = 40

WIDE_CHAPTER_CHARS = 6800
COMPACT_CHAPTER_CHARS = 2800

ALLOWED_V3_TIERS = (
    "RESOLVED_EXPLICIT",
    "RESOLVED_IMPLICIT",
    "PARTIALLY_RESOLVED",
    "UNRESOLVED",
)

_GENERIC_ENTITY_STOP = frozenset({
    "主人公", "主角", "男主", "女主", "众人", "对方", "他们", "我们", "自己",
    "神秘", "幕后", "真相", "组织", "势力", "身份", "来历", "秘密", "原因",
    "情况", "关系", "力量", "存在", "人员", "皇帝", "朝廷", "江湖", "世界",
    "后续", "事件", "线索", "人物", "东西", "地方", "事情", "时候", "问题",
})


# ==============================================================================
# 中立通用 Prompt 模板（去除任何特定作品示例）
# ==============================================================================

U1_SPLIT_PROMPT = """你是长篇小说伏笔问题架构师。请对以下 {count} 条伏笔逐条完成五项拆解：
1. `core_question`：提炼最本质、可独立判定的核心悬念（保留原问题中合法的“A或B”并列分支，不要擅自改偏题意）。
2. `bonus_aspects`：仅属锦上添花的次要细节或附加追问（没有它也算核心已解；若无则写 ""）。
3. `setup_event_anchor`：用 20~40 字精确锁定该伏笔在埋设章的【具体事件/特定人物与部门身份/发生时点】，防止后续把另一场相似事件张冠李戴（例如区分“初入宗门时的引荐”与“后续他案中的戴罪立功推荐”；区分特定姓氏官署如“工部刘某案”与“礼部李某案”）。
4. `needs_pre_setup_backref`：布尔值（true/false）。如果该问题本质在追问“某人此前为何早已认识主角、某旧物的先前来历、关系裂痕的前史、或抵达前谁已动手”（答案可能在设问前不久章节），设为 true；否则 false。
5. `key_entities`：4~8 个最有检索区分度的人名、具体物件名、地名、契约/情感核心词或专有概念（2~6字）。

待拆解条目：
{items_text}

输出纯 JSON 数组（长度严格 {count}，原样保留 fs_id）：
[{{"fs_id": "fs_xxx", "core_question": "...", "bonus_aspects": "...", "setup_event_anchor": "...", "needs_pre_setup_backref": false, "key_entities": ["词1", "词2"]}}]
"""

U1_ALIAS_PAIR_PROMPT = """你是长篇小说实体与名称演化档案员。请阅读本段剧情蒸馏记录（单元 {chunk_start}~{chunk_end}），**只提取高确信度的【同人/同物马甲与名称演化对照】**（绝不输出剧情长摘要）：
覆盖类型：代号对应真名（如“代号甲->真名某人”）、器物俗称或重命名对照（如“无名残简->正式秘典”“旧佩剑->新名剑”）、易容/伪装/身份反转揭晓、西幻或译名全称简称对照（如“全名·姓氏->简称”）。

【单元 {chunk_start}~{chunk_end}】
{chunk_text}

输出纯 JSON 数组（每段最多 15 条，若无则 `[]`）：
[{{"early_term": "...", "true_identity": "...", "relation": "即本人/真名/改名/马甲", "units": [12]}}]
"""

U1_ALIAS_CONSOLIDATE_PROMPT = """你是专有名词词典编纂官。以下是分块抽取的实体/器物/代号对照候选（共 {alias_count} 条）。
请去重合并，保留最核心、准确、无歧义的 **25~75 条别名等价对**（剔除泛化词与主观总结；`early_term` 与 `true_identity` 都保留简洁短名词，方便精确检索），并可参考分卷摘要校验人名拼写：

【分卷摘要参考（校准人名）】
{volume_brief}

【候选词条】
{raw_aliases}

输出纯 JSON 数组：
[{{"early_term": "...", "true_identity": "...", "relation": "等价/真名/改名", "units": [12]}}]
"""

U1_ROUTE_PROMPT = """你是长篇小说高精度剧情线索路由器。下面是本书一段紧凑的【蒸馏事件流】（单元 {chunk_start}~{chunk_end}），以及待扫描的 {q_count} 条伏笔核心问题。

任务：逐一审视本段所有事件块 `[单元A-B]`，找出**在本段中真正被回答、揭晓真身/马甲、说明先前来历成因、给出实体代价、或完成人物情感/动机坦白与关系转折收束**的伏笔！
原则：
1. 若人物/器物改名换姓、换用马甲或译名简称出现，亦或设问的前提在后文以反转方式澄清收束，务必路由命中！
2. 本段若揭示了设问前不久章节中已埋下的直接前因，亦务必路由命中！
3. `units` 必须填写命中事件所在 `[单元A-B]` 区间内的整数单元号；`clue` 用 25 字内简述“本段揭示了什么及涉及何人/何物”（尽量带原文专有名词，便于下层精排）。

【本段蒸馏事件流（单元 {chunk_start}~{chunk_end}）】
{chunk_text}

【待检索伏笔（共 {q_count} 条）】
{questions_text}

输出纯 JSON 数组，仅列出本段确实命中的伏笔（未命中不要硬凑；若全无命中输出 `[]`）：
[{{"fs_id": "fs_xxx", "units": [12, 14], "clue": "揭示了核心物主身份与动机"}}]
"""

U3_DETECTIVE_PROMPT = """你是零人工长篇小说伏笔深度核查侦探。下面为每条伏笔提供【核心主问】、【可免责次要支线】、【设问事件锚点】以及若干段**完整原文窗口**（每段明确标注 `【正篇】` 或 `【番外/外传篇】`）。

核心原则：
1. 以 `core_question` 或任一合法分支（或人物关系/情感心结/结局收束）为判决基准：**不要因为次要支线 `bonus_aspects` 留白而把真回收判成 UNRESOLVED！**
2. 若正文用换词/马甲/规则并线/立场坦白或前提反转方式实质回答了核心悬念（哪怕推翻了原问中关于时间或凶手的预设），可判 `RESOLVED_IMPLICIT` 或 `RESOLVED_EXPLICIT`；若核心已解而另有分支待续，判 `PARTIALLY_RESOLVED`。
3. **四防红线（严防误判）**：
   - `WRONG_EVENT_OR_ENTITY`：严禁拿无关人物或另一场相似事件替代 `setup_event_anchor`（必须核对具体姓氏、官署案名与场景细节，严防把【番外/外传篇】经历混同正篇同期事件！）；
   - `MODALITY_CONFUSION`：梦境试探、他人传闻、虚张声势、未执行计划，不能当作客观已发生事实；
   - `PREMISE_RESTATEMENT`：单纯复述设问前提不算回收；
   - `UNGROUNDED_LEAP`：若正文始终未提及原问指向的特定专有名词/暗号、或角色自始至终未能识破，结论必须诚实判 `UNRESOLVED`（标注 `ATMOSPHERIC_HOOK` 或 `UNFILLED_PIT`），严禁靠“作者写了类似内容”硬猜归因！
4. **主动多跳深挖工具 `follow_up`**：
   只要现有窗口还不够确凿、或只看到中继线索需要顺藤摸瓜查别的章节，请在 `follow_up` 中点名要查的**具体关键词**（最多 5 个）与/或**单元号区间** `[[起始单元, 结束单元]]`（最多 3 段），系统将自动抽取对应正文送给你下一跳核查（最多 3 跳）。若已确凿或确无后续，`follow_up` 置 `null`。

待核查 {count} 条：
{items_block}

输出纯 JSON 数组（长度严格 {count}，原样保留 fs_id）：
[{{
  "fs_id": "fs_xxx",
  "verdict": "RESOLVED_EXPLICIT|RESOLVED_IMPLICIT|PARTIALLY_RESOLVED|UNRESOLVED",
  "unresolved_type": "",
  "confidence": "high|medium|low",
  "answer": "仅陈述所给原文逐字引文能直接证实的答案",
  "evidence_chain": [{{"unit_id": 12, "quote": "逐字连续原句>=6字", "role": "payoff"}}],
  "follow_up": {{"keyword_queries": ["词1"], "unit_ranges": [[10, 14]], "reason": "需要追查什么"}}
}}]
"""

U4_AUDITOR_PROMPT = """你是长篇小说终审总编辑，负责对 {count} 条伏笔执行**最终质量裁决、防过杀兜底与外科重锚定**。

请严格同步执行以下两条对称铁律：

### 第一律：四类硬缺陷核查（严打假阳性）
1. **事件/实体精确对齐（防张冠李戴）**：引文是否真正对应【设问事件锚点】？如果涉及同职衔（如两位不同姓氏/不同部门的大员）、两个不同案件或【番外/外传篇】独立时间线，严禁张冠李戴顶包！
2. **模态真实性（防梦境/传闻当真）**：引文若只是梦境、他人听说、唬人幌子或未实行计划，不得作实证！
3. **实质推进（防前提复述）**：引文是否确实推进了答案或人物/物件终局，而非原地重述旧前提？
4. **极性与零猜测裁剪（防无据脑补）**：如果某专有名词/暗号（或某次预设行动）在全文中毫无提及或角色明确未曾识破，严禁仅凭间接背景跳跃推论，必须判 `UNRESOLVED`；对确已解决的主干，外科剪除引文无法直接支撑的臆测句。

### 第二律：核心/支线分离锁定 + 反预设修正 + 窗内重锚定（严防把真伏笔一刀砍死）
1. 看清下方分开列出的 `【核心主问】` 与 `【可免责次要支线】`：
   **只要下文任何一段完整正文中，存在不犯上述四条硬伤的逐字引文，能直接证实：**
   - 【核心主问】的核心悬念、合法分支、或情感/动机的明确揭示，**或**
   - 即使推翻了原问中关于“之前发生/何时达成”的时间预设，但紧接当场/后文已经当面交代了真相、作案者或归属结局，**或**
   - 该悬念的实质性收束与归宿（如契约方死亡、真身现形揭破伪装、物件源头交代完毕），
   **则严禁以次要支线缺失或量化程度未明为由输出 `UNRESOLVED`！必须判为 `RESOLVED_EXPLICIT` / `RESOLVED_IMPLICIT` / `PARTIALLY_RESOLVED`**，并在 `answer` 中老实区分“已实证部分”与“未明示留白”。
2. **窗内重锚定**：如果初审引偏了片段而同批另有一章正写着真答案，请立即改从那一章重新摘取无伤逐字原句作 `evidence_chain`！

待终审 {count} 条：
{items_block}

输出纯 JSON 数组（长度严格 {count}，原样保留 fs_id）：
[{{
  "fs_id": "fs_xxx",
  "verdict": "RESOLVED_EXPLICIT|RESOLVED_IMPLICIT|PARTIALLY_RESOLVED|UNRESOLVED",
  "unresolved_type": "",
  "answer": "经四防核验及外科裁剪后的干净确切结论",
  "evidence_chain": [{{"unit_id": 12, "quote": "逐字连续原句>=6字", "role": "payoff"}}]
}}]
"""

U5_QUOTE_REPAIR_PROMPT = """确定性原文校验未能在所给正文单元中找到你为 `fs_id={fs_id}` 所引原句（原因：{demote_reason}）。
请对照下文完整正文：
1. 若确有直接支持核心答案或部分回收的原句，请从下文**逐字一字不差地复制** 1~2 句连续原文（>=8 字，带原标点）并填准整数 `unit_id`；
2. 若反复核对确无合法原句，改判 `UNRESOLVED`。

【核心主问】：{core_question}
【事件锚点】：{setup_event_anchor}
【原结论参考】：{prev_answer}

{bundle_text}

输出单个纯 JSON 对象：
{{"fs_id": "{fs_id}", "verdict": "{verdict}", "unresolved_type": "", "answer": "...", "evidence_chain": [{{"unit_id": 12, "quote": "逐字连续原句", "role": "payoff"}}]}}
"""

U3_POOL_SINGLE_STAGE_PROMPT = """你是零人工长篇小说伏笔深度核查侦探兼终审总编辑。本批共核查 {count} 条伏笔，下方先给出按章节单元去重合并的【共享正文证据池】（每段明确标注 `【正篇】` 或 `【番外/外传篇】`），随后逐条给出每条伏笔的题卡与各自专属推荐单元。

请一次性在“四防除伪”与“防过杀锁定”双铁律下完成核查（需要补充章节时可发起 `follow_up` 下一跳，否则直接一步定稿）：

### 第一律：四类硬缺陷核查（严防假阳性，逐条独立隔离）
1. **事件/实体精确对齐**：引文是否真正对应本条【设问事件锚点】？严禁拿同批他题的章节、不同案件/官署/姓氏、或【番外/外传篇】独立时间线顶包！严禁引用早于本条 `setup_unit` 的章作兑现（除非列入【本条合法前置因果单元】）。
2. **模态真实性**：梦境、传闻、虚张声势、未执行计划不得作客观实证！
3. **实质推进**：单纯复述设问前提不算回收。
4. **极性与零猜测裁剪**：若原问指向特定专有名词/暗号但全文毫无提及或角色未曾识破，必须判 `UNRESOLVED`（标注 `ATMOSPHERIC_HOOK` 或 `UNFILLED_PIT`）；对确已解决的主干，裁剪掉引文无法直接支撑的臆测句。

### 第二律：核心/支线分离 + 反预设修正（严防过杀真伏笔）
只要【共享正文证据池】内本条专属推荐单元（或合理后续单元）中，存在不犯上述四条硬伤的逐字连续引文，能直接证实：
- 本条【核心主问】核心悬念、合法分支或情感/动机的明确揭示，**或**
- 即使推翻了原问的时间/凶手预设但紧接/后文当面交代了真相或归宿结局，
则**严禁因【可免责次要支线】未写明或缺乏量化数字而判 UNRESOLVED**！应判 `RESOLVED_EXPLICIT` / `RESOLVED_IMPLICIT` / `PARTIALLY_RESOLVED`，并在 `answer` 中诚实区分“已实证部分”与“未明示留白”。

### 第三律：多跳追查 `follow_up`
若现有池中章节尚缺关键一环，请在 `follow_up` 给出关键词（最多 5 个）与/或单元区间 `[[起始, 结束]]`（最多 3 段）；已确凿或确无后续时置 `null`。

{shared_pool_text}

=== 待核查与终审 {count} 条伏笔题卡 ===
{items_block}

输出纯 JSON 数组（必须严格覆盖全部 {count} 个 fs_id，原样保留 fs_id，不得漏项）：
[{{
  "fs_id": "fs_xxx",
  "verdict": "RESOLVED_EXPLICIT|RESOLVED_IMPLICIT|PARTIALLY_RESOLVED|UNRESOLVED",
  "unresolved_type": "",
  "confidence": "high|medium|low",
  "answer": "经四防核验与裁剪后的干净确切结论",
  "evidence_chain": [{{"unit_id": 12, "quote": "逐字连续原句>=6字", "role": "payoff"}}],
  "follow_up": null
}}]
"""

U4_POOL_AUDITOR_PROMPT = """你是长篇小说终审总编辑，负责对 {count} 条伏笔执行**最终质量裁决、防过杀兜底与外科重锚定**。下方先给出按单元去重合并的【共享正文证据池】，随后逐条列出各伏笔的题卡、前轮侦探结论与专属单元清单。

请严格同步执行以下两条对称铁律（各条逐一独立核验，严禁跨题混引）：
### 第一律：四类硬缺陷核查（严打假阳性）
1. **事件/实体精确对齐（防张冠李戴）**：引文是否真正对应本条【设问事件锚点】？严禁拿他题背景、不同案件/人物、或【番外/外传篇】顶包！未列入【本条合法前置因果单元】且早于本条 `setup_unit` 的章严禁引用。
2. **模态真实性（防梦境/传闻当真）**：梦境、传闻、唬人幌子或未实行计划不得作实证！
3. **实质推进（防前提复述）**：引文是否确实推进了答案或结局（禁止原地重述旧前提）？
4. **极性与零猜测裁剪（防无据脑补）**：专有名词/暗号毫无提及或未曾识破时必须判 `UNRESOLVED`；主干已解者剪除无引文支撑的臆测句。

### 第二律：核心/支线分离锁定 + 反预设修正 + 窗内重锚定
只要本条可用证据单元中存在不犯上述四条硬伤的逐字连续原句，能证实【核心主问】、合法分支、情感揭示、或推翻预设后的实质收束/结局，**严禁以次要支线缺失或量化未明为由输出 `UNRESOLVED`**！如果初审引偏了片段而同池另有一章正写着真答案，立即改从该章重新摘取无伤逐字原句作 `evidence_chain`。

{shared_pool_text}

=== 待终审 {count} 条伏笔 ===
{items_block}

输出纯 JSON 数组（必须严格覆盖全部 {count} 个 fs_id，原样保留 fs_id，不得漏项）：
[{{
  "fs_id": "fs_xxx",
  "verdict": "RESOLVED_EXPLICIT|RESOLVED_IMPLICIT|PARTIALLY_RESOLVED|UNRESOLVED",
  "unresolved_type": "",
  "answer": "经四防核验及外科裁剪后的干净确切结论",
  "evidence_chain": [{{"unit_id": 12, "quote": "逐字连续原句>=6字", "role": "payoff"}}]
}}]
"""
