"""
分析结果数据模型
定义单次章节分析的结构化输出
（自旧项目 models/analysis_result.py 移植；新增 locations / spatial_relationships 地点字段）
"""

from dataclasses import dataclass, field
from typing import List, Any


@dataclass
class CoreEvent:
    """核心事件"""
    id: int = 0
    event: str = ""
    characters: str = ""
    function: str = ""
    importance: str = "中"  # 高/中/低（时间线重要度过滤；旧数据缺省按中）


@dataclass
class CharacterArc:
    """人物弧光"""
    name: str = ""
    surface_action: str = ""
    inner_motivation: str = ""
    change_delta: str = ""
    driver: str = ""


@dataclass
class Foreshadowing:
    """伏笔"""
    clue: str = ""
    type: str = ""
    implication: str = ""
    confidence: str = "中"  # "高"/"中"/"低"
    importance: str = "中"  # 高/中/低（时间线重要度过滤；旧数据缺省按中）
    # 2026-10-31 伏笔复检 V2：原文锚点。没有锚点就等于没有证据，
    # 复检只能靠模型自述"我支持"——那正是旧实现判错了还看不出来的原因。
    anchor_quote: str = ""       # 逐字取自本章正文的一句（校验后可能被清空）
    anchor_chapter: int = 0      # 锚点实际所在章（校验时反查得出）
    motif_key: str = ""          # 2-6 字稳定母题词，供实体登记用


def _norm_importance(value) -> str:
    """归一化 importance：接受 高/中/低 及英文 high/medium/low，非法值按中"""
    if not value:
        return "中"
    m = {"高": "高", "中": "中", "低": "低", "high": "高", "medium": "中", "low": "低",
         "High": "高", "Medium": "中", "Low": "低"}
    return m.get(str(value).strip(), "中")


@dataclass
class Location:
    """地点（新增：地图可视化数据源）"""
    name: str = ""
    parent: str = ""       # 上级地点，表达层级（青云门→主峰）
    type: str = ""         # 地点类型（城市/宗门/秘境等）
    description: str = ""


@dataclass
class SpatialRel:
    """空间关系（新增：A 在 B 以北等）。JSON 键为 from/to/relation，from 是关键字故字段名用 from_"""
    from_: str = ""
    to: str = ""
    relation: str = ""


@dataclass
class CrossBlock:
    """承上启下摘要"""
    summary: str
    unresolved_questions: List[str] = field(default_factory=list)
    new_leads: List[str] = field(default_factory=list)
    contextual_link: str = ""


@dataclass
class UpdatedKnowledge:
    """更新的知识库"""
    timeline: str = ""
    world_building: List[str] = field(default_factory=list)


@dataclass
class LongContextInsights:
    """长上下文深度洞察（精简版：仅保留需要LLM语义理解的字段）"""
    thematic_elements: List[str] = field(default_factory=list)
    pattern: str = ""
    foreshadowing_network: str = ""
    pacing: str = ""
    # 兼容旧格式：新prompt不再输出此字段，但from_dict仍能读取旧JSON
    long_term_arcs: List[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """完整的分析结果"""
    chapter_number: int
    core_events: List[CoreEvent] = field(default_factory=list)
    character_arcs: List[CharacterArc] = field(default_factory=list)
    foreshadowing: List[Foreshadowing] = field(default_factory=list)
    #: 锚点采集统计：total/located/missing。**必须是 dataclass 字段**——
    #: 动态赋值不会被 to_dict 序列化，重启后统计就没了（复审 R1）。
    anchor_stats: dict = field(default_factory=lambda: {"total": 0, "located": 0, "missing": 0})
    plot_holes: List[str] = field(default_factory=lambda: ["无明显逻辑漏洞"])
    locations: List[Location] = field(default_factory=list)  # 新增：地点（旧 result.json 无此字段，默认空）
    spatial_relationships: List[SpatialRel] = field(default_factory=list)  # 新增：空间关系
    cross_block: CrossBlock = field(default_factory=lambda: CrossBlock(
        summary="", unresolved_questions=[], new_leads=[], contextual_link=""
    ))
    updated_knowledge: UpdatedKnowledge = field(default_factory=UpdatedKnowledge)
    long_context_insights: LongContextInsights = field(default_factory=LongContextInsights)
    raw_response: str = ""
    block_size: int = 1  # 该结果对应的块大小（N章合并），用于断点续跑时检测 block_size 变更

    def to_dict(self) -> dict:
        """转换为字典（用于JSON序列化）；spatial_relationships 的 from_ 键映射回 from"""
        from dataclasses import asdict
        d = asdict(self)
        d['spatial_relationships'] = [
            {'from': r.get('from_', ''), 'to': r.get('to', ''), 'relation': r.get('relation', '')}
            for r in d.get('spatial_relationships', [])
        ]
        return _clean_for_json(d)

    @staticmethod
    def _normalize_string_list(items: list) -> list:
        """将列表中的每个元素都转为字符串（处理嵌套列表或字典的情况）

        P2 修复（2026-08-24）：LLM 类型漂移可能把整个数组字段输出为一个字符串，
        此前直接迭代该字符串产出单字垃圾列表并持久化污染 KB/聚合/prompt；
        现整串收编为单元素。非列表非字符串的输入一律回落空列表。"""
        if isinstance(items, str):
            stripped = items.strip()
            return [stripped] if stripped else []
        if not isinstance(items, list):
            return []
        result = []
        for item in items:
            if isinstance(item, list):
                result.append(", ".join(str(x) for x in item))
            elif isinstance(item, dict):
                result.append(str(item))
            else:
                result.append(str(item))
        return result


    @classmethod
    def from_dict(cls, data: dict) -> 'AnalysisResult':
        """从字典创建（用于JSON反序列化）；旧格式无 locations/spatial_relationships 时默认空列表"""
        # 处理嵌套对象
        core_events = []
        for e in data.get('core_events', []):
            if not isinstance(e, dict):
                continue
            # normalize characters 字段，确保为字符串
            chars = e.get('characters', '')
            if chars is None:
                # LLM 输出 null → 空串，绝不能变成字面量 "None"（否则聚合/导出/图谱出现虚构角色）
                e['characters'] = ''
            elif isinstance(chars, list):
                e['characters'] = ", ".join(str(x or '') for x in chars)
            core_events.append(CoreEvent(id=e.get("id", 0), event=str(e.get("event", "")), characters=str(e.get("characters", "")), function=str(e.get("function", "")), importance=_norm_importance(e.get("importance"))))
        character_arcs = []
        for a in data.get('character_arcs', []):
            if isinstance(a, dict):
                try:
                    # normalize characters-like fields if they are lists
                    for key in ('surface_action', 'inner_motivation', 'change_delta', 'driver'):
                        if isinstance(a.get(key), list):
                            a[key] = ", ".join(str(x) for x in a[key])
                    character_arcs.append(CharacterArc(name=str(a.get("name", "")), surface_action=str(a.get("surface_action", "")), inner_motivation=str(a.get("inner_motivation", "")), change_delta=str(a.get("change_delta", "")), driver=str(a.get("driver", ""))))
                except Exception:
                    continue
        foreshadowing_raw = data.get('foreshadowing', [])
        foreshadowing = []
        # 兼容LLM输出中可能缺失字段的情况，尽量用默认值构造 Foreshadowing
        for f in foreshadowing_raw:
            if isinstance(f, dict):
                try:
                    clue = f.get('clue', '')
                    ftype = f.get('type', '')
                    implication = f.get('implication', '')
                    # 映射英文confidence到中文
                    _confidence_map = {"high": "高", "medium": "中", "low": "低", "High": "高", "Medium": "中", "Low": "低"}
                    raw_confidence = f.get("confidence", "中")
                    confidence = _confidence_map.get(raw_confidence, raw_confidence) or "中"
                    foreshadowing.append(Foreshadowing(
                        clue=clue,
                        type=ftype,
                        implication=implication,
                        confidence=confidence,
                        importance=_norm_importance(f.get("importance")),
                        anchor_quote=str(f.get('anchor_quote') or '').strip(),
                        anchor_chapter=int(f.get('anchor_chapter') or 0),
                        motif_key=str(f.get('motif_key') or '').strip(),
                    ))
                except Exception as e:
                    import logging; logging.getLogger(__name__).debug(f"跳过异常伏笔: {e}")
                    # 忽略单条伏笔的解析异常，继续处理其他项
                    continue
            else:
                # 跳过非字典项
                continue

        # 新增：地点列表（旧格式无此字段 → 空列表）
        locations = []
        for loc in data.get('locations', []) or []:
            if not isinstance(loc, dict):
                continue
            locations.append(Location(
                name=str(loc.get('name', '')),
                parent=str(loc.get('parent', '') or ''),
                type=str(loc.get('type', '') or ''),
                description=str(loc.get('description', '') or ''),
            ))

        # 新增：空间关系列表（JSON 键 from/to/relation；兼容 from_）
        spatial_relationships = []
        for rel in data.get('spatial_relationships', []) or []:
            if not isinstance(rel, dict):
                continue
            spatial_relationships.append(SpatialRel(
                from_=str(rel.get('from', rel.get('from_', '')) or ''),
                to=str(rel.get('to', '') or ''),
                relation=str(rel.get('relation', '') or ''),
            ))

        cross_block_data = data.get('cross_block') or {}
        unresolved_raw = cross_block_data.get('unresolved_questions', [])
        new_leads_raw = cross_block_data.get('new_leads', [])

        def _ensure_str_list(value):
            """兼容旧版字符串和新版数组"""
            if isinstance(value, list):
                return [str(v) for v in value]
            if isinstance(value, str):
                v = value.strip()
                if not v or v in ("无明显遗留问题", "无明显新线索", "无", "none"):
                    return []
                return [s.strip() for s in v.replace("，", ",").split(',') if s.strip()]
            return []

        cross_block = CrossBlock(
            summary=cross_block_data.get('summary', ''),
            unresolved_questions=_ensure_str_list(unresolved_raw),
            new_leads=_ensure_str_list(new_leads_raw),
            contextual_link=cross_block_data.get('contextual_link', '')
        )

        updated_knowledge_data = data.get('updated_knowledge') or {}
        # 时间线字段可能在某些LLM输出中为列表，需要统一为字符串
        timeline_raw = updated_knowledge_data.get('timeline', '')
        if isinstance(timeline_raw, list):
            timeline_raw = ", ".join(str(t) for t in timeline_raw)
        updated_knowledge = UpdatedKnowledge(
            timeline=timeline_raw,
            world_building=cls._normalize_string_list(updated_knowledge_data.get('world_building', []))
        )

        lci_data = data.get('long_context_insights') or {}

        long_context_insights = LongContextInsights(
            thematic_elements=cls._normalize_string_list(lci_data.get('thematic_elements', [])),
            pattern=lci_data.get('pattern', ''),
            foreshadowing_network=lci_data.get('foreshadowing_network', ''),
            pacing=lci_data.get('pacing', ''),
            long_term_arcs=cls._normalize_string_list(lci_data.get('long_term_arcs', []))
        )

        return cls(
            chapter_number=data.get('chapter_number', 0),
            core_events=core_events,
            character_arcs=character_arcs,
            foreshadowing=foreshadowing,
            anchor_stats=dict(data.get('anchor_stats') or {"total": 0, "located": 0, "missing": 0}),
            plot_holes=cls._normalize_string_list(data.get('plot_holes', ["无明显逻辑漏洞"])),
            locations=locations,
            spatial_relationships=spatial_relationships,
            cross_block=cross_block,
            updated_knowledge=updated_knowledge,
            long_context_insights=long_context_insights,
            raw_response=data.get('raw_response', ''),
            block_size=data.get('block_size', 1)
        )


def _clean_for_json(obj: Any) -> Any:
    """递归清理不可JSON序列化的类型（set → list 等）"""
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_for_json(v) for v in obj]
    if isinstance(obj, set):
        return [_clean_for_json(v) for v in obj]
    return obj
