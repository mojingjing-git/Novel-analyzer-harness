"""
可视化数据聚合服务
提供时间线（单章结果聚合）与全书语义图谱（story_atlas.json）只读视图。
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Any

from backend.config.constants import (
    FORESHADOW_CATEGORY_DEFS,
    FORESHADOW_CATEGORY_FALLBACK,
    FORESHADOW_CATEGORY_SCHEMA_VERSION,
    FORESHADOW_TYPE_MAP_FILE,
    STORY_ATLAS_FILE,
    STORY_ATLAS_SCHEMA_VERSION,
)
from backend.models.analysis_result import AnalysisResult
from backend.services.final_summary.atlas_extractor import sanitize_world_tree
from backend.utils.aggregate_utils import JSONAggregator
from backend.utils.json_utils import safe_load_json

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


def atlas_data(output_dir: Path) -> Dict[str, Any]:
    """读取全书语义图谱 story_atlas.json（不存在或损坏时安全返回 exists=False 空结构）。"""
    empty: Dict[str, Any] = {
        "exists": False,
        "schema_version": STORY_ATLAS_SCHEMA_VERSION,
        "generated_at": "",
        "model": "",
        "characters": [],
        "relationships": [],
        "stages": [],
        "world_tree": [],
    }
    atlas_path = Path(output_dir) / STORY_ATLAS_FILE
    if not atlas_path.exists():
        return empty

    data = safe_load_json(atlas_path)
    if not isinstance(data, dict):
        return empty

    raw_chars = data.get("characters")
    raw_rels = data.get("relationships")
    raw_stages = data.get("stages")
    raw_tree = data.get("world_tree")

    chars = [c for c in (raw_chars if isinstance(raw_chars, list) else []) if isinstance(c, dict)]
    canon_set = {str(c.get("canonical_name") or "").strip() for c in chars if str(c.get("canonical_name") or "").strip()}
    rels = [
        r for r in (raw_rels if isinstance(raw_rels, list) else [])
        if isinstance(r, dict)
        and str(r.get("source") or "").strip() in canon_set
        and str(r.get("target") or "").strip() in canon_set
        and str(r.get("source") or "").strip() != str(r.get("target") or "").strip()
    ]
    stages = [s for s in (raw_stages if isinstance(raw_stages, list) else []) if isinstance(s, dict)]
    stages.sort(key=lambda s: (int(s.get("start_chapter") or 0), int(s.get("stage_no") or 0)))
    world_tree = sanitize_world_tree(raw_tree or [], max_depth=10)

    has_any = bool(chars or stages or world_tree)
    if not has_any:
        return empty

    return {
        "exists": True,
        "schema_version": int(data.get("schema_version") or STORY_ATLAS_SCHEMA_VERSION),
        "generated_at": str(data.get("generated_at") or ""),
        "model": str(data.get("model") or ""),
        "characters": chars,
        "relationships": rels,
        "stages": stages,
        "world_tree": world_tree,
    }
