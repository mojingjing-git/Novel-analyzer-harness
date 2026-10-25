"""
JSON聚合工具
将所有章节的分析JSON结果汇总到单一文件中，支持多种聚合模式
"""
import json
import re
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional

from ..models.analysis_result import AnalysisResult
from ..utils.json_utils import safe_save_json
from ..utils.text_utils import is_text_duplicate

logger = logging.getLogger(__name__)


CHAR_NAME_SUFFIXES = (
    '（主角）', '（配角）', '（已故）', '（重要）', '（次要）',
    '（女主）', '（男主）', '（反派）', '（龙套）',
    '(主角)', '(配角)', '(已故)', '(重要)', '(次要)',
    '(女主)', '(男主)', '(反派)', '(龙套)'
)

def _normalize_char_name(name: str) -> str:
    """归一化角色名：去除常见括号标注后缀，用于dict key去重"""
    name = name.strip()
    for suffix in CHAR_NAME_SUFFIXES:
        if name.endswith(suffix):
            base = name[:-len(suffix)].strip()
            if base:
                return base
    return name


class JSONAggregator:
    """JSON聚合器"""

    @staticmethod
    def _make_hashable(item: Any) -> Any:
        """将不可哈希的类型转为可哈希的字符串表示，用于set操作"""
        if isinstance(item, (str, int, float, bool, type(None))):
            return item
        return json.dumps(item, sort_keys=True, ensure_ascii=False)

    def __init__(self, output_dir: Path):
        """
        初始化聚合器

        Args:
            output_dir: JSON文件所在的输出目录
        """
        self.output_dir = Path(output_dir)
        self.results: List[AnalysisResult] = []

    def discover_json_files(self) -> List[Path]:
        """
        扫描目录中所有的章节JSON文件（优先 _data.json，回退 _result.json）

        Returns:
            按章节号排序的JSON文件列表
        """
        if not self.output_dir.exists():
            logger.warning(f"输出目录不存在: {self.output_dir}")
            return []

        # 收集所有章号及其文件路径
        chapters: Dict[int, Path] = {}
        for filepath in self.output_dir.iterdir():
            if not filepath.is_file():
                continue
            # 优先匹配 _data.json
            m = re.match(r'chapter_(\d+)_data\.json$', filepath.name, re.IGNORECASE)
            if m:
                ch = int(m.group(1))
                chapters[ch] = filepath
                continue
            # 回退 _result.json（当 _data.json 不存在时）
            m = re.match(r'chapter_(\d+)_result\.json$', filepath.name, re.IGNORECASE)
            if m:
                ch = int(m.group(1))
                if ch not in chapters:
                    chapters[ch] = filepath

        json_files = sorted(chapters.items(), key=lambda x: x[0])
        logger.info(f"发现 {len(json_files)} 个章节JSON文件")
        return [f[1] for f in json_files]

    def load_chapter_json(self, json_path: Path) -> Optional[AnalysisResult]:
        """
        加载单个章节的JSON文件

        Args:
            json_path: JSON文件路径

        Returns:
            AnalysisResult对象，失败返回None
        """
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            result = AnalysisResult.from_dict(data)
            logger.info(f"成功加载: {json_path.name} (第{result.chapter_number}章)")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败 {json_path.name}: {e}")
            return None
        except Exception as e:
            logger.error(f"加载失败 {json_path.name}: {e}")
            return None

    def load_all_chapters(self) -> List[AnalysisResult]:
        """
        加载目录中所有章节的JSON结果

        Returns:
            成功加载的AnalysisResult列表
        """
        json_files = self.discover_json_files()
        self.results = []

        for json_path in json_files:
            result = self.load_chapter_json(json_path)
            if result is not None:
                self.results.append(result)

        logger.info(f"总计成功加载 {len(self.results)} 个章节")
        return self.results

    def aggregate_to_single_json(self, output_path: Path = None,
                                  include_raw: bool = False) -> Path:
        """
        将所有章节聚合为单个JSON文件

        Args:
            output_path: 输出文件路径，默认为 output/novel_analysis_aggregated.json
            include_raw: 是否包含原始LLM响应文本

        Returns:
            生成的聚合JSON文件路径
        """
        if not self.results:
            self.load_all_chapters()

        if output_path is None:
            output_path = self.output_dir / "novel_analysis_aggregated.json"

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 构建聚合数据结构
        aggregated = {
            "metadata": {
                "generated_at": datetime.now().isoformat(),
                "total_chapters": len(self.results),
                "chapter_range": {
                    "start": min(r.chapter_number for r in self.results) if self.results else 0,
                    "end": max(r.chapter_number for r in self.results) if self.results else 0
                },
                "version": "1.0"
            },
            "chapters": {}
        }

        for result in self.results:
            chapter_data = result.to_dict()

            # 可选移除原始响应（减少文件大小）
            if not include_raw:
                chapter_data.pop('raw_response', None)

            aggregated["chapters"][str(result.chapter_number)] = chapter_data

        # P2 修复（2026-08-24）：open('w') 截断直写在并发 GET 触发实时聚合时
        # 会产出交错/截断 JSON，后续读取全部 500；统一走仓库标准原子原语。
        safe_save_json(aggregated, output_path, ensure_ascii=False)

        logger.info(f"聚合JSON已生成: {output_path}")
        logger.info(f"文件大小: {output_path.stat().st_size / 1024:.1f} KB")

        return output_path

    def aggregate_core_events(self, output_path: Path = None) -> Dict[str, List[Dict]]:
        """
        聚合所有章节的核心事件

        Args:
            output_path: 输出文件路径

        Returns:
            按章节分组的核心事件字典
        """
        if not self.results:
            self.load_all_chapters()

        aggregated = {}
        for result in self.results:
            chapter_key = f"chapter_{result.chapter_number}"
            aggregated[chapter_key] = []
            for event in result.core_events:
                aggregated[chapter_key].append({
                    "id": event.id,
                    "event": event.event,
                    "characters": event.characters,
                    "function": event.function
                })

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(aggregated, f, ensure_ascii=False, indent=2)
            logger.info(f"核心事件聚合已保存: {output_path}")

        return aggregated

    def aggregate_character_arcs(self, output_path: Path = None) -> Dict[str, List[Dict]]:
        """
        聚合所有章节的人物弧光

        Args:
            output_path: 输出文件路径

        Returns:
            按章节分组的人物弧光字典
        """
        if not self.results:
            self.load_all_chapters()

        aggregated = {}
        for result in self.results:
            chapter_key = f"chapter_{result.chapter_number}"
            aggregated[chapter_key] = []
            for arc in result.character_arcs:
                aggregated[chapter_key].append({
                    "name": arc.name,
                    "surface_action": arc.surface_action,
                    "inner_motivation": arc.inner_motivation,
                    "change_delta": arc.change_delta,
                    "driver": arc.driver
                })

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(aggregated, f, ensure_ascii=False, indent=2)
            logger.info(f"人物弧光聚合已保存: {output_path}")

        return aggregated

    def aggregate_foreshadowing(self, output_path: Path = None) -> Dict[str, List[Dict]]:
        """
        聚合所有章节的伏笔

        Args:
            output_path: 输出文件路径

        Returns:
            按章节分组的伏笔字典
        """
        if not self.results:
            self.load_all_chapters()

        aggregated = {}
        for result in self.results:
            chapter_key = f"chapter_{result.chapter_number}"
            aggregated[chapter_key] = []
            for foreshadow in result.foreshadowing:
                aggregated[chapter_key].append({
                    "clue": foreshadow.clue,
                    "type": foreshadow.type,
                    "implication": foreshadow.implication,
                    "confidence": foreshadow.confidence
                })

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(aggregated, f, ensure_ascii=False, indent=2)
            logger.info(f"伏笔聚合已保存: {output_path}")

        return aggregated

    def aggregate_character_tracking(self, output_path: Path = None) -> Dict[str, Dict]:
        """
        聚合角色追踪信息（每个角色出现在哪些章节）

        Args:
            output_path: 输出文件路径

        Returns:
            角色追踪字典
        """
        if not self.results:
            self.load_all_chapters()

        character_map: Dict[str, Dict] = {}

        for result in self.results:
            for event in result.core_events:
                raw_chars = event.characters
                if isinstance(raw_chars, list):
                    raw_chars = ", ".join(str(c) for c in raw_chars)
                chars = [c.strip() for c in str(raw_chars).replace("，", ",").split(',') if c.strip()]
                for char in chars:
                    key = _normalize_char_name(char)
                    if key not in character_map:
                        character_map[key] = {
                            "name": char,
                            "chapters": [],
                            "events": [],
                            "character_arcs": [],
                            "state_history": {},
                            "first_appearance": None,
                            "total_events": 0
                        }
                    else:
                        # 用更短的名字版本作为展示名
                        if len(char) < len(character_map[key]["name"]):
                            character_map[key]["name"] = char
                    
                    # 记录首次出现
                    if character_map[key]["first_appearance"] is None:
                        character_map[key]["first_appearance"] = result.chapter_number
                    
                    character_map[key]["chapters"].append(result.chapter_number)
                    character_map[key]["events"].append({
                        "chapter": result.chapter_number,
                        "event_id": event.id,
                        "event": event.event,
                        "function": event.function
                    })
                    character_map[key]["total_events"] += 1

            # 收集角色弧光
            for arc in result.character_arcs:
                arc_key = _normalize_char_name(arc.name)
                if arc_key in character_map:
                    character_map[arc_key]["character_arcs"].append({
                        "chapter": result.chapter_number,
                        "surface_action": arc.surface_action,
                        "inner_motivation": arc.inner_motivation,
                        "change_delta": arc.change_delta
                    })

            # 收集角色状态（从 character_arcs 表面行为派生）
            for arc in result.character_arcs:
                arc_key = _normalize_char_name(arc.name)
                if arc.name and arc.surface_action and arc_key in character_map:
                    character_map[arc_key]["state_history"][str(result.chapter_number)] = arc.surface_action

        # 对章节列表去重并排序
        for char_data in character_map.values():
            char_data["chapters"] = sorted(list(set(char_data["chapters"])))

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(character_map, f, ensure_ascii=False, indent=2)
            logger.info(f"角色追踪聚合已保存: {output_path}")

        return character_map

    def aggregate_unresolved_questions(self, output_path: Path = None) -> Dict[str, List[str]]:
        if not self.results:
            self.load_all_chapters()

        aggregated = {}
        for result in self.results:
            chapter_key = f"chapter_{result.chapter_number}"
            aggregated[chapter_key] = list(result.cross_block.unresolved_questions)

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(aggregated, f, ensure_ascii=False, indent=2)
            logger.info(f"未解决问题聚合已保存: {output_path}")

        return aggregated

    def aggregate_new_leads(self, output_path: Path = None) -> Dict[str, List[str]]:
        if not self.results:
            self.load_all_chapters()

        aggregated = {}
        for result in self.results:
            chapter_key = f"chapter_{result.chapter_number}"
            aggregated[chapter_key] = list(result.cross_block.new_leads)

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(aggregated, f, ensure_ascii=False, indent=2)
            logger.info(f"新线索聚合已保存: {output_path}")

        return aggregated

    def aggregate_world_building(self, output_path: Path = None) -> Dict[str, Dict]:
        """
        聚合世界观构建信息

        Args:
            output_path: 输出文件路径

        Returns:
            世界观构建字典
        """
        if not self.results:
            self.load_all_chapters()

        world_building = {
            "timeline": [],
            "elements": [],
            "evolution": []
        }

        # 收集时间线
        for result in self.results:
            if hasattr(result, 'updated_knowledge') and hasattr(result.updated_knowledge, 'timeline'):
                world_building["timeline"].append({
                    "chapter": result.chapter_number,
                    "timeline": result.updated_knowledge.timeline
                })
        
        # 收集世界观元素（使用 seen 集合按 element 字符串去重）
        seen_elements = set()
        for result in self.results:
            if hasattr(result, 'updated_knowledge') and hasattr(result.updated_knowledge, 'world_building'):
                for element in result.updated_knowledge.world_building:
                    key = self._make_hashable(element)
                    if key not in seen_elements:
                        seen_elements.add(key)
                        world_building["elements"].append({
                            "element": element,
                            "first_appearance": result.chapter_number
                        })

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(world_building, f, ensure_ascii=False, indent=2)
            logger.info(f"世界观构建聚合已保存: {output_path}")

        return world_building

    def aggregate_long_term_arcs(self, output_path: Path = None) -> Dict[str, List[str]]:
        """
        聚合长期弧光信息

        Args:
            output_path: 输出文件路径

        Returns:
            长期弧光字典
        """
        if not self.results:
            self.load_all_chapters()

        long_term_arcs = []
        all_arcs = set()

        for result in self.results:
            if hasattr(result, 'long_context_insights') and hasattr(result.long_context_insights, 'long_term_arcs'):
                for arc in result.long_context_insights.long_term_arcs:
                    key = self._make_hashable(arc)
                    if key not in all_arcs:
                        long_term_arcs.append({
                            "arc": arc,
                            "first_mentioned": result.chapter_number
                        })
                        all_arcs.add(key)

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump({"long_term_arcs": long_term_arcs}, f, ensure_ascii=False, indent=2)
            logger.info(f"长期弧光聚合已保存: {output_path}")

        return {"long_term_arcs": long_term_arcs}

    def aggregate_long_context_insights(self, output_path: Path = None) -> Dict[str, Dict]:
        """
        聚合长上下文洞察信息

        Args:
            output_path: 输出文件路径

        Returns:
            长上下文洞察字典
        """
        if not self.results:
            self.load_all_chapters()

        insights = {
            "verified_facts": [],
            "character_states_evolution": {},
            "pacing_analysis": [],
            "character_relationships": {},
            "foreshadowing_network": [],
            "thematic_elements": []
        }

        # 收集已验证事实（从 core_events.event 派生）
        all_facts = set()
        for result in self.results:
            for event in result.core_events:
                if event.event:
                    key = self._make_hashable(event.event)
                    if key not in all_facts:
                        insights["verified_facts"].append({
                            "fact": event.event,
                            "first_verified": result.chapter_number
                        })
                        all_facts.add(key)

        # 收集角色状态演变（从 character_arcs.surface_action 派生）
        for result in self.results:
            for arc in result.character_arcs:
                if arc.name and arc.surface_action:
                    if arc.name not in insights["character_states_evolution"]:
                        insights["character_states_evolution"][arc.name] = []
                    insights["character_states_evolution"][arc.name].append({
                        "chapter": result.chapter_number,
                        "state": arc.surface_action
                    })

        # 收集节奏分析
        for result in self.results:
            if result.long_context_insights.pacing:
                insights["pacing_analysis"].append({
                    "chapter": result.chapter_number,
                    "pacing": result.long_context_insights.pacing
                })

        # 收集伏笔网络
        for result in self.results:
            if result.long_context_insights.foreshadowing_network:
                insights["foreshadowing_network"].append({
                    "chapter": result.chapter_number,
                    "network": result.long_context_insights.foreshadowing_network
                })

        # 收集角色关系（从同章角色配对推导）
        # D9 修复：配对 key 归一化（按名字排序），避免 A-B 与 B-A 生成两条重复记录
        for result in self.results:
            arcs = [a for a in result.character_arcs if a.name]
            for i, a1 in enumerate(arcs):
                for a2 in arcs[i+1:]:
                    # 归一化：名字字典序小的在前，保证 (A,B) 与 (B,A) 落到同一 key
                    first, second = (a1, a2) if a1.name <= a2.name else (a2, a1)
                    pair = f"{first.name}-{second.name}"
                    if pair not in insights["character_relationships"]:
                        insights["character_relationships"][pair] = []
                    insights["character_relationships"][pair].append({
                        "chapter": result.chapter_number,
                        "description": f"{first.change_delta}；{second.change_delta}"
                    })

        # 收集主题元素（模糊去重）
        seen_themes = set()
        for result in self.results:
            for t in result.long_context_insights.thematic_elements:
                if not is_text_duplicate(t, seen_themes):
                    seen_themes.add(t)
                    insights["thematic_elements"].append({
                        "element": t,
                        "first_mentioned": result.chapter_number
                    })

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(insights, f, ensure_ascii=False, indent=2)
            logger.info(f"长上下文洞察聚合已保存: {output_path}")

        return insights

    def aggregate_plot_holes(self, output_path: Path = None) -> Dict[str, List[str]]:
        """
        聚合逻辑漏洞信息

        Args:
            output_path: 输出文件路径

        Returns:
            逻辑漏洞字典
        """
        if not self.results:
            self.load_all_chapters()

        plot_holes = []
        for result in self.results:
            if hasattr(result, 'plot_holes'):
                for hole in result.plot_holes:
                    plot_holes.append({
                        "chapter": result.chapter_number,
                        "hole": str(hole)
                    })

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump({"plot_holes": plot_holes}, f, ensure_ascii=False, indent=2)
            logger.info(f"逻辑漏洞聚合已保存: {output_path}")

        return {"plot_holes": plot_holes}

    def aggregate_full_output(self, output_dir: Path = None,
                               include_raw: bool = False,
                               should_stop=None) -> Dict[str, Path]:
        """
        执行全面聚合，生成多个聚合文件

        Args:
            output_dir: 输出目录，默认为output/aggregated
            include_raw: 是否包含原始LLM响应
            should_stop: 可选的无参回调，返回True时提前终止聚合（支持用户停止）

        Returns:
            生成的所有文件路径字典
        """
        def _stopped():
            return bool(should_stop and should_stop())

        if not self.results:
            self.load_all_chapters()

        if output_dir is None:
            output_dir = self.output_dir / "aggregated"
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        generated_files = {}

        # 1. 完整聚合JSON
        full_json_path = output_dir / "novel_analysis_aggregated.json"
        self.aggregate_to_single_json(full_json_path, include_raw=include_raw)
        generated_files["full_aggregation"] = full_json_path
        if _stopped():
            logger.info("聚合被用户停止（已生成部分文件）")
            return generated_files

        # 2. 核心事件聚合
        events_path = output_dir / "core_events_aggregated.json"
        self.aggregate_core_events(events_path)
        generated_files["core_events"] = events_path
        if _stopped():
            logger.info("聚合被用户停止（已生成部分文件）")
            return generated_files

        # 3. 人物弧光聚合
        arcs_path = output_dir / "character_arcs_aggregated.json"
        self.aggregate_character_arcs(arcs_path)
        generated_files["character_arcs"] = arcs_path
        if _stopped():
            logger.info("聚合被用户停止（已生成部分文件）")
            return generated_files

        # 4. 伏笔聚合
        foreshadow_path = output_dir / "foreshadowing_aggregated.json"
        self.aggregate_foreshadowing(foreshadow_path)
        generated_files["foreshadowing"] = foreshadow_path
        if _stopped():
            logger.info("聚合被用户停止（已生成部分文件）")
            return generated_files

        # 5. 角色追踪聚合
        tracking_path = output_dir / "character_tracking_aggregated.json"
        self.aggregate_character_tracking(tracking_path)
        generated_files["character_tracking"] = tracking_path
        if _stopped():
            logger.info("聚合被用户停止（已生成部分文件）")
            return generated_files

        # 6. 跨章节问题聚合
        questions_path = output_dir / "unresolved_questions_aggregated.json"
        self.aggregate_unresolved_questions(questions_path)
        generated_files["unresolved_questions"] = questions_path
        if _stopped():
            logger.info("聚合被用户停止（已生成部分文件）")
            return generated_files

        # 7. 新线索聚合
        leads_path = output_dir / "new_leads_aggregated.json"
        self.aggregate_new_leads(leads_path)
        generated_files["new_leads"] = leads_path
        if _stopped():
            logger.info("聚合被用户停止（已生成部分文件）")
            return generated_files

        # 8. 世界观构建聚合
        worldbuilding_path = output_dir / "world_building_aggregated.json"
        self.aggregate_world_building(worldbuilding_path)
        generated_files["world_building"] = worldbuilding_path
        if _stopped():
            logger.info("聚合被用户停止（已生成部分文件）")
            return generated_files

        # 9. 长期弧光聚合
        long_arcs_path = output_dir / "long_term_arcs_aggregated.json"
        self.aggregate_long_term_arcs(long_arcs_path)
        generated_files["long_term_arcs"] = long_arcs_path
        if _stopped():
            logger.info("聚合被用户停止（已生成部分文件）")
            return generated_files

        # 10. 长上下文洞察聚合
        insights_path = output_dir / "long_context_insights_aggregated.json"
        self.aggregate_long_context_insights(insights_path)
        generated_files["long_context_insights"] = insights_path
        if _stopped():
            logger.info("聚合被用户停止（已生成部分文件）")
            return generated_files

        # 11. 逻辑漏洞聚合
        plot_holes_path = output_dir / "plot_holes_aggregated.json"
        self.aggregate_plot_holes(plot_holes_path)
        generated_files["plot_holes"] = plot_holes_path

        logger.info(f"全面聚合完成，共生成 {len(generated_files)} 个文件")
        for name, path in generated_files.items():
            size_kb = path.stat().st_size / 1024
            logger.info(f"  - {name}: {path.name} ({size_kb:.1f} KB)")

        return generated_files

    def get_statistics(self) -> Dict[str, Any]:
        """
        获取聚合统计信息

        Returns:
            统计信息字典
        """
        if not self.results:
            self.load_all_chapters()

        if not self.results:
            return {"total_chapters": 0}

        total_events = sum(len(r.core_events) for r in self.results)
        total_arcs = sum(len(r.character_arcs) for r in self.results)
        total_foreshadows = sum(len(r.foreshadowing) for r in self.results)
        total_holes = sum(
            len([h for h in r.plot_holes if h != "无明显逻辑漏洞"])
            for r in self.results
        )

        # 收集所有角色
        all_characters = set()
        for result in self.results:
            for event in result.core_events:
                raw_chars = event.characters
                if isinstance(raw_chars, list):
                    raw_chars = ", ".join(str(c) for c in raw_chars)
                chars = [c.strip() for c in str(raw_chars).replace("，", ",").split(',') if c.strip()]
                all_characters.update(chars)

        return {
            "total_chapters": len(self.results),
            "chapter_range": {
                "start": min(r.chapter_number for r in self.results),
                "end": max(r.chapter_number for r in self.results)
            },
            "total_core_events": total_events,
            "total_character_arcs": total_arcs,
            "total_foreshadowing": total_foreshadows,
            "total_plot_holes": total_holes,
            "unique_characters": len(all_characters),
            "characters_list": sorted(list(all_characters)),
            "average_events_per_chapter": round(total_events / len(self.results), 2),
            "average_arcs_per_chapter": round(total_arcs / len(self.results), 2),
            "average_foreshadowing_per_chapter": round(total_foreshadows / len(self.results), 2),
            "world_building_elements": len([elem for elem in self._get_all_world_building_elements()]),
            "long_term_arcs_count": len([arc for arc in self._get_all_long_term_arcs()]),
            "unresolved_questions_count": sum(
                len(result.cross_block.unresolved_questions)
                for result in self.results
            ),
            "new_leads_count": sum(
                len(result.cross_block.new_leads)
                for result in self.results
            )
        }

    def _get_all_world_building_elements(self) -> List[str]:
        """获取所有世界观元素"""
        elements: Dict[str, str] = {}
        for result in self.results:
            if hasattr(result, 'updated_knowledge') and hasattr(result.updated_knowledge, 'world_building'):
                for elem in result.updated_knowledge.world_building:
                    key = self._make_hashable(elem)
                    if key not in elements:
                        elements[key] = str(elem)
        return list(elements.values())

    def _get_all_long_term_arcs(self) -> List[str]:
        """获取所有长期弧光"""
        arcs: Dict[str, str] = {}
        for result in self.results:
            if hasattr(result, 'long_context_insights') and hasattr(result.long_context_insights, 'long_term_arcs'):
                for arc in result.long_context_insights.long_term_arcs:
                    key = self._make_hashable(arc)
                    if key not in arcs:
                        arcs[key] = str(arc)
        return list(arcs.values())


def aggregate_novel_analysis(
    output_dir: str = "workspace/output",
    aggregated_dir: str = None,
    include_raw: bool = False,
    should_stop=None
) -> Dict[str, Path]:
    """
    便捷函数：执行完整的小说分析聚合

    Args:
        output_dir: 章节JSON文件所在目录
        aggregated_dir: 聚合结果输出目录
        include_raw: 是否包含原始LLM响应
        should_stop: 可选的无参回调，返回True时提前终止聚合

    Returns:
        生成的文件路径字典
    """
    output_path = Path(output_dir)

    if aggregated_dir is None:
        aggregated_path = output_path / "aggregated"
    else:
        aggregated_path = Path(aggregated_dir)

    aggregator = JSONAggregator(output_path)

    return aggregator.aggregate_full_output(
        output_dir=aggregated_path,
        include_raw=include_raw,
        should_stop=should_stop
    )
