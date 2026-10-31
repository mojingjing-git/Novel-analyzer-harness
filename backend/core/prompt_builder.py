"""
提示词构建器
构建长上下文专用的系统提示和用户提示
（自旧项目 core/prompt_builder.py 移植；SYSTEM_PROMPT 新增 locations / spatial_relationships 输出要求）
"""

from typing import List
from ..config.constants import DEFAULT_MAX_ARC_LENGTH, FORESHADOW_CATEGORY_DEFS
from ..models.knowledge import KnowledgeBase

SYSTEM_PROMPT = """# Role: 小说结构分析师

你正在逐章分析一部长篇小说。你会收到当前章节的全文，以及从前面各章积累下来的知识库上下文。

## 知识库上下文解读

知识库上下文会以带方括号标题的区域分块提供（位置可能在 system 或 user 消息中），各区域含义如下：
- **[完整故事历史]** — 包含两层：「全书主线概要」（覆盖第1章到当前章的完整主线摘要，约600字）和「近期逐章记录」（最近N章的逐章事件摘要列表）。帮你把握整体剧情走向和近期进展
- **[前情摘要]** — 最近几章的承上启下摘要，帮你做紧邻章衔接
- **[当前时间线]** — 故事当前的时间点
- **[角色当前状态]** — 各角色的最新行为/状态（从前面各章的人物弧光自动汇总），分析时参考这些状态判断角色是否发生了变化
- **[角色关系]** — 角色之间的配对关系及演变
- **[已验证事实]** — 前面各章已确立的核心事件
- **[已知世界观]** — 目前已确立的世界观元素（地点、规则、势力等），分析时区分“已知元素的深化”和“全新元素的引入”
- **[主题元素]** — 已识别的宏观主题维度
- **[伏笔网络]** — 前面各章的伏笔关联摘要，帮你判断本章是否回收了旧伏笔或触发了伏笔链条

利用这些上下文找出跨章呼应、伏笔回收、角色状态延续。特别注意：如果角色在[角色当前状态]中的描述与本章行为一致，说明角色尚未变化，不要重复报告相同的状态。

## 分析规则

1. 挖掘反常细节、矛盾言行作为伏笔；`implication` 说明它**暗示了什么尚待交代的事**，不要写成对未来剧情的预测（禁止"将…/可能…/未来会…"式推演，那不是悬念，没有答案）
2. `question` 必须是**能在后文被回答**的具体事项；只有氛围、背景、人物性格的描述不算伏笔。`answer_criteria` 只写"什么算回答"，不要写"答案应该是什么"——写死一个结局会把"明确取消/反转"误判成未回收
3. 使用文中具体人名/地名，避免泛称
4. 无相应内容时填空数组 `[]` 或空字符串 `""`，不要编造
5. `contextual_link` 必须包含三步：
   ① 引用至少1个具体前文章节号（如"第184章"）
   ② 简述该章发生了什么具体事件
   ③ 说明与本章哪段情节如何关联
   bad: "前面章节的军营经历让主角对权威产生怀疑"
   good: "第184章张铁柱被同僚背叛后独自突围 → 本章第3段他拒绝接受上级指令，不再信任任何权威"
6. 时间线使用具体年月，确实未知时标注"约XX年"而非"19xx年"
7. thematic_elements 最多5条。每条必须是本质不同的主题维度。禁止"X与Y"排列组合式输出（如"力量与代价"和"力量的代价"只能保留一条）。优先选最宏观的核心主题。
8. long_context_insights 只输出 thematic_elements、pattern、foreshadowing_network、pacing 四个字段。不要输出 character_states、character_relationships、verified_facts、relationship_evolution、long_term_arcs、active_foreshadowing、resolved_foreshadowing。
9. `locations` 记录本章明确出现或首次提及的地点：`name` 用原文地名；`parent` 填其所属的上级地点（如"主峰"的 parent 是"青云门"），无上级或不明确时填 `""`；`type` 填地点类型（城市/宗门/秘境/国家/建筑等）；`description` 一句话描述。只记录有情节意义的地点，不要罗列一笔带过的泛称。
10. `spatial_relationships` 记录本章明确陈述的地点间空间关系（如"A 在 B 以北"、"C 距 D 三百里"），`from`/`to` 必须是 locations 中出现过或前文已知的地名，`relation` 用原文表述。没有明确空间信息时填空数组。
11. `importance` 标注该事件/伏笔对全书主线的重要程度：`高`（推动主线、决定人物命运、全书关键转折）、`中`（影响当前情节线、推进支线）、`低`（局部细节、氛围渲染、日常过渡）。每章 core_events 中 `高` 不超过 2 条。

## 伏笔分类约束（重要）

`foreshadowing` 每条必须给 `anchor_quote`：**从本章正文里逐字照抄**一句能支撑这条伏笔的话，长度不少于 6 个字，连同标点一起原样抄，不要改写、不要拼接、不要加省略号。若本章找不到这样的原句，就不要列这条伏笔。`motif_key` 给一个 2-6 字的稳定母题词（同一伏笔在后续章节复用同一个词）。

`foreshadowing` 的 `type` 字段必须从下列 50 类中选**一个**，禁止自创类型。找不到完全匹配时选最接近的类别，实在无法归类才选"其他"。这是全书统一分类，同义/近义必须归到同一类。

## Output Schema (纯JSON，无markdown包裹)

{
  "core_events": [{"id": 1, "event": "事件", "characters": "角色", "function": "作用", "importance": "高|中|低"}],
  "character_arcs": [{"name": "角色", "surface_action": "表面行为", "inner_motivation": "深层动机", "change_delta": "变化量", "driver": "触发事件"}],
  "foreshadowing": [{"clue": "本章出现了什么原文细节", "question": "这个细节留下了什么尚待交代的具体事项（含明确疑问：谁/是否/为何/如何/下落/结果）", "answer_criteria": "后文出现什么内容才足以关闭这个事项（写结果类型，不要把某个具体结局写成唯一答案）", "type": "伏笔分类表中的类别", "implication": "它暗示了什么尚待交代的事（不要预测未来剧情）", "confidence": "高|中|低", "importance": "高|中|低", "anchor_quote": "本章正文里逐字照抄的一句（≥6字，原样含标点）", "motif_key": "2-6字稳定母题词"}],
  "plot_holes": ["逻辑漏洞"],
  "locations": [{"name": "地点名", "parent": "上级地点或空", "type": "地点类型", "description": "一句话描述"}],
  "spatial_relationships": [{"from": "地点A", "to": "地点B", "relation": "空间关系描述"}],
  "cross_block": {
    "summary": "本章摘要",
    "unresolved_questions": ["遗留疑点"],
    "new_leads": ["新线索"],
    "contextual_link": "与哪章哪事件呼应（必须具体引用章号）"
  },
  "updated_knowledge": {
    "timeline": "本章时间点（具体年月）",
    "world_building": ["新增世界观元素"]
  },
  "long_context_insights": {
    "thematic_elements": ["主题1", "主题2", "主题3"],
    "pattern": "跨章重复出现的叙事模式（一句话）",
    "foreshadowing_network": "伏笔之间的关联分析（2-3句话）",
    "pacing": "节奏分析（一句话）"
  }
}

"""

# 伏笔 50 类定义表：恒定文本，追加在 SYSTEM_PROMPT 后（前缀稳定，KV cache 友好）。
# 仅注入 name + description；anchors 用于后端校验/页面展示，不占 prompt 体积。
FORESHADOW_CATEGORY_TEXT = "\n".join(
    f"  {name}：{desc}" for name, desc, _ in FORESHADOW_CATEGORY_DEFS
)


class PromptBuilder:
    """提示词构建器"""

    def __init__(
        self,
        max_arc_length: int = DEFAULT_MAX_ARC_LENGTH,
        max_arcs: int = 30,
        max_summaries: int = 5,
        timeline_truncate: int = 200,
        max_character_states: int = 20,
        max_world_items: int = 20,
        max_foreshadow_entries: int = 5,
        max_relationships: int = 15,
        max_verified_facts: int = 15,
        max_themes: int = 8,
        max_content_chars: int = 30000,
    ):
        self.max_arc_length = max_arc_length
        self.max_arcs = max_arcs
        self.max_summaries = max_summaries
        self.timeline_truncate = timeline_truncate
        self.max_character_states = max_character_states
        self.max_world_items = max_world_items
        self.max_foreshadow_entries = max_foreshadow_entries
        # P1 修复：以下三个字段此前派生但从未注入 prompt，现启用注入
        self.max_relationships = max_relationships
        self.max_verified_facts = max_verified_facts
        self.max_themes = max_themes
        # P2 修复：正文是唯一无上限的 prompt 注入项；超窗确定性失败且退火无效。
        # 3 万字≈为 32k-token 窗口的 system+九路上下文留足余量；后续可接配置。
        self.max_content_chars = max_content_chars

    def build_messages(
        self,
        chapter_content: str,
        chapter_number: int,
        knowledge_base: KnowledgeBase
    ) -> List[dict]:
        """
        构建完整的消息列表

        Args:
            chapter_content: 当前章节内容
            chapter_number: 章节号
            knowledge_base: 当前知识库状态

        Returns:
            OpenAI格式的messages列表
        """
        # 构建完整故事历史
        history_parts = []
        # 第一层：结构化滚动总结（优先）或旧格式降级
        rendered_rolling = self._render_structured_rolling(knowledge_base.rolling_structured)
        if rendered_rolling:
            history_parts.append(rendered_rolling)
        # 第二层：近期逐章 timeline（保持原逻辑）
        if knowledge_base.compressed_arcs:
            history_parts.append("—— 近期逐章记录 ——")
            history_parts.extend(str(item) for item in knowledge_base.compressed_arcs[-self.max_arcs:])
        full_history = "\n\n".join(history_parts) if history_parts else "（暂无历史主线，这是故事的开始）"

        # 构建前情摘要
        if knowledge_base.recent_summaries:
            contextual_summary = "\n".join(knowledge_base.recent_summaries[-self.max_summaries:])
        else:
            contextual_summary = "（无前情摘要）"

        # 时间线
        timeline = knowledge_base.story_timeline[:self.timeline_truncate]

        # 角色当前状态
        char_states = knowledge_base.character_states
        if char_states:
            items = list(char_states.items())[-self.max_character_states:]
            char_state_text = "\n".join(f"- {name}: {state}" for name, state in items)
        else:
            char_state_text = "（暂无角色状态）"

        # 世界观元素
        world = knowledge_base.world_building
        if world:
            world_text = "\n".join(f"- {w}" for w in world[-self.max_world_items:])
        else:
            world_text = "（暂无世界观元素）"

        # 伏笔网络
        foreshadow = knowledge_base.foreshadowing_network
        if foreshadow:
            foreshadow_text = "\n".join(foreshadow[-self.max_foreshadow_entries:])
        else:
            foreshadow_text = "（暂无伏笔网络）"

        # 角色关系（P1：启用注入，此前为死字段）
        relationships = knowledge_base.character_relationships
        if relationships:
            rel_items = list(relationships.items())[-self.max_relationships:]
            rel_text = "\n".join(f"- {pair}: {desc}" for pair, desc in rel_items)
        else:
            rel_text = "（暂无角色关系）"

        # 已验证事实（P1：启用注入，此前为死字段）
        facts = knowledge_base.verified_facts
        if facts:
            fact_text = "\n".join(f"- {f}" for f in facts[-self.max_verified_facts:])
        else:
            fact_text = "（暂无已验证事实）"

        # 主题元素（P1：启用注入，此前为死字段）
        themes = knowledge_base.thematic_elements
        if themes:
            theme_text = "\n".join(f"- {t}" for t in themes[-self.max_themes:])
        else:
            theme_text = "（暂无主题元素）"

        # KV cache 友好排序：变化频率低的块放前面（prefix 稳定，提高远端 prompt cache 命中），
        # 变化频率高的块（故事历史每章追加、章节正文每章全变）放后面。
        # 同时把累积去重、极少变化的 [已知世界观] / [主题元素] 挪进 system，
        # 与固定的 SYSTEM_PROMPT 合并成稳定前缀。
        system_prompt = f"""{SYSTEM_PROMPT}

[伏笔分类表]
{FORESHADOW_CATEGORY_TEXT}

[已知世界观]
{world_text}

[主题元素]
{theme_text}"""

        # P2：尾部保序截断——开头丢弃量显式告知模型，避免其困惑叙事断裂
        content_text = chapter_content
        if len(content_text) > self.max_content_chars:
            dropped = len(content_text) - self.max_content_chars
            content_text = (
                f"【注意】原文过长，已截去开头约 {dropped} 字，以下为保留的后段：\n"
                + content_text[-self.max_content_chars:]
            )

        user_prompt = f"""[当前时间线]
{timeline}

[角色当前状态]
{char_state_text}

[角色关系]
{rel_text}

[已验证事实]
{fact_text}

[前情摘要]
{contextual_summary}

[伏笔网络]
{foreshadow_text}

[完整故事历史]
{full_history}

[当前分析文本 - 第{chapter_number}章]
{content_text}

请对上述章节进行深度分析，严格按照JSON格式输出分析结果。"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        return messages

    @staticmethod
    def _render_structured_rolling(structured_data: dict) -> str:
        """渲染结构化滚动总结为可读文本，注入到分析 prompt 中"""
        if not structured_data:
            return ""

        # 降级兼容：旧格式 _legacy_text（优先检查）
        if "_legacy_text" in structured_data:
            return structured_data["_legacy_text"]

        # 真·空值保护：所有列表字段都为空则不注入
        has_content = bool(
            structured_data.get("global_milestones") or
            structured_data.get("paradigm_layers") or
            structured_data.get("recent_momentum") or
            structured_data.get("active_causal_chains") or
            (isinstance(structured_data.get("current_context"), dict) and any(
                structured_data["current_context"].get(k) for k in ("location", "current_goal", "immediate_threat")
            ))
        )
        if not has_content:
            return ""

        lines = ["【故事时间轴与范式状态】"]

        # 全局里程碑
        milestones = structured_data.get("global_milestones", [])
        if milestones:
            lines.append("全局里程碑：")
            for m in milestones:
                lines.append(f"- {m}")

        # 范式记录
        paradigms = structured_data.get("paradigm_layers", [])
        if paradigms:
            lines.append("范式记录：")
            for p in paradigms:
                if not isinstance(p, dict):
                    continue
                active_flag = "✅ 当前" if p.get("is_active") else "📌 历史"
                lines.append(f"- {active_flag} {p.get('chapters', '?')}：{p.get('core_rules', '')}")

        # 当前上下文
        ctx = structured_data.get("current_context", {})
        if ctx and any(ctx.get(k) for k in ("location", "current_goal", "immediate_threat")):
            lines.append(f"当前上下文：位置={ctx.get('location', '?')}，目标={ctx.get('current_goal', '?')}，威胁={ctx.get('immediate_threat', '?')}")

        # 因果链
        chains = structured_data.get("active_causal_chains", [])
        if chains:
            lines.append("活跃因果链：")
            for c in chains:
                lines.append(f"- {c}")

        # 近期势头
        momentum = structured_data.get("recent_momentum", [])
        if momentum:
            lines.append("近期势头：")
            for r in momentum[-15:]:
                lines.append(f"- {r}")

        return "\n".join(lines)
