"""
知识库数据模型
定义持久化知识库的结构
"""

import logging
from dataclasses import dataclass, field
from typing import List, Dict

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeBase:
    """持久化知识库"""
    story_timeline: str = "故事刚开始"
    recent_summaries: List[str] = field(default_factory=list)
    compressed_arcs: List[str] = field(default_factory=list)

    character_states: Dict[str, str] = field(default_factory=dict)
    verified_facts: List[str] = field(default_factory=list)
    long_term_arcs: List[str] = field(default_factory=list)
    character_relationships: Dict[str, str] = field(default_factory=dict)
    world_building: List[str] = field(default_factory=list)
    thematic_elements: List[str] = field(default_factory=list)
    foreshadowing_network: List[str] = field(default_factory=list)
    rolling_summary: str = ""  # 兼容旧版单层摘要
    rolling_layer_early: str = ""  # 早期固定摘要（第1-N章）
    rolling_layer_recent: str = ""  # 近期滚动摘要（第N+1-当前章）
    rolling_layer_boundary: int = 0  # 分层边界章号
    rolling_structured: dict = field(default_factory=dict)  # 结构化滚动总结 JSON

    def to_dict(self) -> dict:
        """转换为字典"""
        from dataclasses import asdict
        return asdict(self)

    @staticmethod
    def _load_rolling_structured(data: dict) -> dict:
        """加载结构化滚动总结，含旧格式降级逻辑"""
        rs = data.get('rolling_structured')
        if isinstance(rs, dict) and rs and '_legacy_text' not in rs:
            return rs
        # 降级：旧格式 layer_early/layer_recent → _legacy_text 过渡
        early = data.get('rolling_layer_early', '')
        recent = data.get('rolling_layer_recent', '')
        if early or recent:
            return {"_legacy_text": f"{early}\n{recent}".strip(), "_schema_version": 1}
        # 兼容旧版 rolling_summary
        old_summary = data.get('rolling_summary', '')
        if old_summary:
            return {"_legacy_text": old_summary, "_schema_version": 1}
        return {}

    @staticmethod
    def _normalize_string_list(items: list) -> list:
        """将列表中的每个元素都转为字符串（处理嵌套列表或字典的情况）"""
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
    def from_dict(cls, data: dict) -> 'KnowledgeBase':
        """从字典创建"""
        if not isinstance(data, dict):
            logger.warning(f"knowledge.json 内容非 dict（类型={type(data).__name__}），使用空知识库")
            return cls()
        # 对所有List[str]字段做类型归一化，防止嵌套列表/字典导致unhashable type错误
        list_str_fields = [
            'recent_summaries', 'compressed_arcs',
            'verified_facts', 'long_term_arcs', 'world_building',
            'thematic_elements', 'foreshadowing_network'
        ]
        normalized = {}
        for field in list_str_fields:
            raw = data.get(field, [])
            normalized[field] = cls._normalize_string_list(raw) if isinstance(raw, list) else []

        # 防御性检查字典字段
        character_states = data.get('character_states', {})
        if not isinstance(character_states, dict):
            character_states = {}

        character_relationships = data.get('character_relationships', {})
        if not isinstance(character_relationships, dict):
            character_relationships = {}


        result = cls(
            story_timeline=data.get('story_timeline', '故事刚开始'),
            recent_summaries=normalized['recent_summaries'],
            compressed_arcs=normalized['compressed_arcs'],
            character_states=character_states,
            verified_facts=normalized['verified_facts'],
            long_term_arcs=normalized['long_term_arcs'],
            character_relationships=character_relationships,
            world_building=normalized['world_building'],
            thematic_elements=normalized['thematic_elements'],
            foreshadowing_network=normalized['foreshadowing_network'],
            rolling_summary=data.get('rolling_summary', ''),
            rolling_layer_early=data.get('rolling_layer_early', ''),
            rolling_layer_recent=data.get('rolling_layer_recent', ''),
            rolling_layer_boundary=data.get('rolling_layer_boundary', 0),
            rolling_structured=cls._load_rolling_structured(data)
        )
        # 降级后清空旧字段，避免存储冗余
        if '_legacy_text' in (result.rolling_structured or {}):
            result.rolling_layer_early = ""
            result.rolling_layer_recent = ""
        return result
