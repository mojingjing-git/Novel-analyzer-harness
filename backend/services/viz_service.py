"""
可视化数据聚合服务
从 result.json 聚合时间线/关系图/地图所需数据
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

from backend.config.constants import (
    FORESHADOW_CATEGORY_DEFS,
    FORESHADOW_CATEGORY_FALLBACK,
    FORESHADOW_CATEGORY_SCHEMA_VERSION,
    FORESHADOW_TYPE_MAP_FILE,
)
from backend.models.analysis_result import AnalysisResult
from backend.utils.aggregate_utils import CHAR_NAME_SUFFIXES, JSONAggregator

logger = logging.getLogger(__name__)


def _load_results(output_dir: Path) -> List[AnalysisResult]:
    aggregator = JSONAggregator(output_dir)
    return aggregator.load_all_chapters()


def _load_type_category_map(output_dir: Path) -> Dict[str, str]:
    """
    加载 per-book type→category 映射（final_summary 服务持久化的文件）。
    映射缺失/schema 不匹配/解析失败：返回空 dict，调用方降级到原始 type 显示。
    """
    map_path = output_dir / FORESHADOW_TYPE_MAP_FILE
    if not map_path.exists():
        return {}
    try:
        with open(map_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema_version") != FORESHADOW_CATEGORY_SCHEMA_VERSION:
            return {}
        return data.get("mapping", {}) or {}
    except Exception as e:
        logger.debug(f"viz_service 加载 type 映射失败: {e}，按原始 type 降级显示")
        return {}


def timeline_data(output_dir: Path) -> Dict[str, Any]:
    """时间线数据：核心事件泳道 + 伏笔埋设/回收"""
    results = _load_results(output_dir)
    type_to_cat = _load_type_category_map(output_dir)
    valid_names = {name for name, _, _ in FORESHADOW_CATEGORY_DEFS}
    events = []
    foreshadows = []

    for result in results:
        ch = result.chapter_number
        for event in result.core_events:
            events.append({
                "chapter": ch,
                "id": event.id,
                "event": event.event,
                "characters": event.characters,
                "function": event.function,
                "importance": event.importance,
            })
        for fs in result.foreshadowing:
            ftype = fs.type or ""
            # 源头约束落地后 type 即合法类别；旧数据才查映射兜底
            if ftype in valid_names:
                category = ftype
            else:
                category = type_to_cat.get(ftype, FORESHADOW_CATEGORY_FALLBACK)
            foreshadows.append({
                "chapter": ch,
                "clue": fs.clue,
                "type": ftype,
                "category": category,
                "confidence": fs.confidence,
                "importance": fs.importance,
            })

    return {
        "events": events,
        "foreshadows": foreshadows,
        "chapters": [r.chapter_number for r in results],
        "category_map_loaded": bool(type_to_cat),
    }


def _aggregate_chars_edges(
    results: List[AnalysisResult],
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """从 results 聚合 nodes_map / edges_map（同章共现建边，weight=同章次数）

    抽出为独立函数，便于 graph_data() 先对全集跑一次拿总数、再对过滤后跑一次拿数据。
    """
    nodes_map: Dict[str, Dict[str, Any]] = {}
    edges_map: Dict[str, Dict[str, Any]] = {}

    def normalize_name(name: str) -> str:
        # 去除常见后缀
        # 后缀列表与聚合侧共用单一来源；此处的 strip 时机与"纯后缀输入"处理
        # 保持原有差异（aggregate 先 strip 且纯后缀时返回原名），不做顺手统一。
        for suffix in CHAR_NAME_SUFFIXES:
            if name.endswith(suffix):
                return name[:-len(suffix)].strip()
        return name.strip()

    for result in results:
        ch = result.chapter_number
        chars_in_chapter = set()
        for event in result.core_events:
            raw_chars = event.characters
            if isinstance(raw_chars, list):
                raw_chars = ", ".join(str(c) for c in raw_chars)
            chars = [normalize_name(c) for c in str(raw_chars).replace("，", ",").split(",") if c.strip()]
            for c in chars:
                chars_in_chapter.add(c)
                if c not in nodes_map:
                    nodes_map[c] = {"id": c, "name": c, "chapters": [], "event_count": 0}
                nodes_map[c]["chapters"].append(ch)
                nodes_map[c]["event_count"] += 1

        # 同章角色建边
        chars = sorted(chars_in_chapter)
        for i in range(len(chars)):
            for j in range(i + 1, len(chars)):
                a, b = chars[i], chars[j]
                key = f"{a}|{b}"
                if key not in edges_map:
                    edges_map[key] = {"source": a, "target": b, "chapters": [], "weight": 0}
                edges_map[key]["chapters"].append(ch)
                edges_map[key]["weight"] += 1

    return nodes_map, edges_map


def graph_data(
    output_dir: Path,
    chapter_start: Optional[int] = None,
    chapter_end: Optional[int] = None,
    min_edge_weight: int = 1,
    max_nodes: int = 200,
    min_node_count: int = 1,
) -> Dict[str, Any]:
    """关系图数据：角色作为节点，同章出现/关系变化作为边

    支持章节范围切片 + Top-N 截断 + 边权阈值过滤。
    """
    all_results = _load_results(output_dir)

    # 全书章节范围（始终来自全集，过滤前记录；用于前端 slider 边界/重置）
    chapter_min = min(r.chapter_number for r in all_results) if all_results else None
    chapter_max = max(r.chapter_number for r in all_results) if all_results else None

    # 全量统计（章节过滤前的角色/边总数；用于 total_characters/total_edges）
    full_nodes_map, full_edges_map = _aggregate_chars_edges(all_results)
    total_characters_before = len(full_nodes_map)
    total_edges_before = len(full_edges_map)

    # 章节范围过滤
    if chapter_start is not None or chapter_end is not None:
        results = [r for r in all_results
                   if (chapter_start is None or r.chapter_number >= chapter_start)
                   and (chapter_end is None or r.chapter_number <= chapter_end)]
    else:
        results = all_results

    # 过滤后聚合
    nodes_map, edges_map = _aggregate_chars_edges(results)

    # 节点 count 阈值过滤
    nodes_map = {k: v for k, v in nodes_map.items()
                 if v["event_count"] >= min_node_count}

    # 边权重阈值过滤（同时要求两端节点仍在 nodes_map 中）
    edges_map = {k: v for k, v in edges_map.items()
                 if v["weight"] >= min_edge_weight
                 and v["source"] in nodes_map
                 and v["target"] in nodes_map}

    # Top-N 截断（按 event_count 降序，超出 max_nodes 的节点剔除，并连带剔除其所有边）
    if len(nodes_map) > max_nodes:
        sorted_names = sorted(nodes_map.items(),
                              key=lambda x: x[1]["event_count"], reverse=True)
        keep = {n for n, _ in sorted_names[:max_nodes]}
        nodes_map = {k: v for k, v in nodes_map.items() if k in keep}
        edges_map = {k: v for k, v in edges_map.items()
                     if v["source"] in keep and v["target"] in keep}

    return {
        "nodes": [{"id": n["id"], "name": n["name"], "event_count": n["event_count"]} for n in nodes_map.values()],
        "edges": [{"source": e["source"], "target": e["target"], "weight": e["weight"]} for e in edges_map.values()],
        "total_characters": total_characters_before,
        "total_edges": total_edges_before,
        "filtered": {
            "chapter_start": chapter_start,
            "chapter_end": chapter_end,
            "min_edge_weight": min_edge_weight,
            "max_nodes": max_nodes,
            "min_node_count": min_node_count,
        },
        "chapter_range": {
            "min": chapter_min,
            "max": chapter_max,
        },
    }


def map_data(output_dir: Path) -> Dict[str, Any]:
    """地图数据：地点层级 + 空间关系

    2026-08-22 重构：严格要求归一化文件存在。
    - 有 normalized 文件 + 任一 chapter 含 _normalized_ref → 返回归一化数据
    - 否则返回 needs_normalization=True + 空数组，由前端 MapPage 引导用户归一化
    - 删除了旧版 raw chapter 聚合兜底（避免用户误以为"不归一化也能看地图"）
    """
    output_subdir = output_dir / "output"
    if not output_subdir.is_dir():
        output_subdir = output_dir

    locations_normalized_path = output_subdir / "locations_normalized.json"
    spatial_normalized_path = output_subdir / "spatial_relationships_normalized.json"

    use_normalized = False
    if locations_normalized_path.exists() and spatial_normalized_path.exists():
        for cf in output_subdir.glob("chapter_*_result.json"):
            try:
                with open(cf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("_normalized_ref") == "locations_normalized.json":
                    use_normalized = True
                    break
            except Exception:
                continue

    if not use_normalized:
        return {"locations": [], "relationships": [], "needs_normalization": True}

    try:
        with open(locations_normalized_path, "r", encoding="utf-8") as f:
            loc_norm = json.load(f)
        with open(spatial_normalized_path, "r", encoding="utf-8") as f:
            rel_norm = json.load(f)
        return {
            "locations": [
                {
                    "id": loc["canonical_name"],
                    "name": loc["canonical_name"],
                    "aliases": loc.get("aliases", []),
                    "parent": loc.get("parent", ""),
                    "type": loc.get("type", ""),
                    "description": loc.get("description", ""),
                    "chapters": [],
                }
                for loc in loc_norm.get("locations", [])
            ],
            "relationships": rel_norm.get("relationships", []),
            "needs_normalization": False,
        }
    except Exception as e:
        logger.warning(f"读归一化数据失败: {e}")
        return {"locations": [], "relationships": [], "needs_normalization": True}
