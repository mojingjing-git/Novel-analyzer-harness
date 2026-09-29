"""
角色卡生成器
基于聚合数据生成详细的角色卡片
"""

import json
from pathlib import Path
from typing import Dict, List

class CharacterCardGenerator:
    """角色卡生成器"""
    
    def __init__(self, aggregated_dir: Path):
        """
        初始化角色卡生成器
        
        Args:
            aggregated_dir: 聚合数据目录
        """
        self.aggregated_dir = Path(aggregated_dir)
        self.character_data = None
        self.character_arcs = None
        self.core_events = None
        self.long_context_insights = None
        
    def _find_aggregated(self, name: str) -> Path | None:
        """聚合文件可能直接位于 aggregated_dir，也可能在其 ``aggregated`` 子目录中
        （路由只把 character_tracking 聚合到前者，其余文件常驻后者）。两处都查。"""
        candidates = [
            self.aggregated_dir / name,
            self.aggregated_dir / "aggregated" / name,
        ]
        for p in candidates:
            if p.exists():
                return p
        return None

    def load_aggregated_data(self):
        """加载所有聚合数据"""
        try:
            # 加载角色追踪数据
            tracking_path = self._find_aggregated("character_tracking_aggregated.json")
            if tracking_path is not None:
                with open(tracking_path, 'r', encoding='utf-8') as f:
                    self.character_data = json.load(f)
            
            # 加载人物弧光数据
            arcs_path = self._find_aggregated("character_arcs_aggregated.json")
            if arcs_path is not None:
                with open(arcs_path, 'r', encoding='utf-8') as f:
                    self.character_arcs = json.load(f)
            
            # 加载核心事件数据
            events_path = self._find_aggregated("core_events_aggregated.json")
            if events_path is not None:
                with open(events_path, 'r', encoding='utf-8') as f:
                    self.core_events = json.load(f)
            
            # 加载长上下文洞察
            insights_path = self._find_aggregated("long_context_insights_aggregated.json")
            if insights_path is not None:
                with open(insights_path, 'r', encoding='utf-8') as f:
                    self.long_context_insights = json.load(f)
            
            return True
            
        except Exception as e:
            print(f"加载聚合数据失败: {e}")
            return False
    

    def get_character_summaries(self) -> List[Dict]:
        """角色卡索引：返回 [{name, first_appearance, total_events, chapters_count}]，按事件数降序。

        供 API `/api/books/{id}/characters` 使用（前端 CharactersPage 的列表）。
        """
        if not self.character_data:
            if not self.load_aggregated_data():
                return []
        # load_aggregated_data 在 tracking 文件缺失时仍可能返回 True，
        # 此时 character_data 仍为 None，需二次兜底避免 .items() 崩溃
        if not self.character_data:
            return []
        summaries = []
        for name, data in self.character_data.items():
            summaries.append({
                "name": name,
                "first_appearance": data.get("first_appearance"),
                "total_events": data.get("total_events", 0),
                "chapters_count": len(data.get("chapters", [])),
            })
        summaries.sort(key=lambda x: x.get("total_events", 0), reverse=True)
        return summaries

    def get_card_data(self, character_name: str) -> Dict:
        """单个角色卡的结构化数据，供 API `/api/books/{id}/characters/{name}` 使用。

        返回 dict（而非纯文本），由前端以玻璃卡片形式渲染；缺失数据则抛 KeyError（→ 404）。
        """
        if not self.character_data:
            if not self.load_aggregated_data():
                raise KeyError(f"角色 '{character_name}' 数据未加载")
        # 同上：load 后仍需确认 character_data 已填充，否则对 None 做 `in` 会 TypeError
        if not self.character_data:
            raise KeyError(f"角色 '{character_name}' 数据未加载")
        if character_name not in self.character_data:
            raise KeyError(f"角色 '{character_name}' 不存在")
        char_info = self.character_data[character_name]
        return {
            "name": character_name,
            "first_appearance": char_info.get("first_appearance"),
            "chapters": char_info.get("chapters", []),
            "total_events": char_info.get("total_events", 0),
            "arcs": self._get_character_arcs(character_name),
            "events": self._get_character_events(character_name),
            "states": self._get_character_states(character_name),
            "relationships": self._analyze_relationships(character_name),
        }

    
    def _get_character_arcs(self, character_name: str) -> List[Dict]:
        """获取角色的弧光数据"""
        arcs = []
        if not self.character_arcs:
            return arcs
        
        for chapter_key, chapter_arcs in self.character_arcs.items():
            for arc in chapter_arcs:
                if arc['name'] == character_name:
                    chapter_num = int(chapter_key.replace('chapter_', ''))
                    arcs.append({
                        'chapter': chapter_num,
                        'surface_action': arc['surface_action'],
                        'inner_motivation': arc['inner_motivation'],
                        'change_delta': arc['change_delta'],
                        'driver': arc['driver']
                    })
        
        arcs.sort(key=lambda x: x['chapter'])
        return arcs
    
    def _get_character_events(self, character_name: str) -> List[Dict]:
        """获取角色参与的事件"""
        events = []
        if not self.character_data or character_name not in self.character_data:
            return events
        
        char_info = self.character_data[character_name]
        return char_info.get('events', [])
    
    def _get_character_states(self, character_name: str) -> Dict[int, str]:
        """获取角色状态演变"""
        states = {}
        if not self.character_data or character_name not in self.character_data:
            return states
        
        char_info = self.character_data[character_name]
        state_history = char_info.get('state_history', {})
        
        for chapter_str, state in state_history.items():
            try:
                chapter_num = int(chapter_str)
                states[chapter_num] = state
            except ValueError:
                continue
        
        return states
    
    def _analyze_relationships(self, character_name: str) -> Dict[str, Dict]:
        """分析角色关系"""
        relationships = {}
        
        if not self.core_events:
            return relationships
        
        # 从事件中提取角色关系
        for chapter_key, events in self.core_events.items():
            chapter_num = int(chapter_key.replace('chapter_', ''))
            
            for event in events:
                raw_chars = event.get('characters', '')
                if isinstance(raw_chars, list):
                    raw_chars = ", ".join(str(c) for c in raw_chars)
                else:
                    raw_chars = str(raw_chars)
                event_characters = [c.strip() for c in raw_chars.replace("，", ",").split(',') if c.strip()]
                
                if character_name in event_characters:
                    # 这个事件涉及的其他角色都是相关角色
                    for other_char in event_characters:
                        if other_char != character_name:
                            if other_char not in relationships:
                                relationships[other_char] = {
                                    'first_encounter': chapter_num,
                                    'joint_events': [],
                                    'event_count': 0
                                }
                            
                            relationships[other_char]['joint_events'].append({
                                'chapter': chapter_num,
                                'event': event.get('event', ''),
                                'function': event.get('function', '')
                            })
                            relationships[other_char]['event_count'] += 1
        
        return relationships
