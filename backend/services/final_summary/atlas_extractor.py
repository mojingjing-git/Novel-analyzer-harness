"""全书语义图谱提取与清洗工具（Layer 1/2）。

在 FinalSummaryRunner 阶段 4 与最终报告并发执行：
- 基于分卷摘要与毫秒级确定性锚点，分两路并发产出：
  1. CharacterAtlas: 精选核心人物 + 马甲归并（严禁吞并独立投影体）+ 语义关系边
  2. WorldStageAtlas: 全书主线换地图阶段流 + 多世界/势力层级树
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.config.constants import STORY_ATLAS_SCHEMA_VERSION
from backend.utils.aggregate_utils import _normalize_char_name, split_character_names
from backend.utils.json_utils import extract_json_from_text, safe_load_json, safe_parse_json

logger = logging.getLogger(__name__)

VALID_ROLE_TIERS = frozenset({
    "protagonist",
    "major_ally",
    "major_antagonist",
    "key_support",
    "hidden_power",
})

ROLE_TIER_PRIORITY: Dict[str, int] = {
    "protagonist": 0,
    "major_ally": 1,
    "major_antagonist": 2,
    "key_support": 3,
    "hidden_power": 4,
}

VALID_REL_CATEGORIES = frozenset({
    "ally",
    "hostile",
    "mentor",
    "romance",
    "faction",
    "complex",
})

VALID_TREE_NODE_TYPES = frozenset({"world", "region", "faction", "landmark"})


def build_deterministic_anchors(
    output_dir: Path,
    vol_ranges: List[Tuple[int, int, int]],
) -> Dict[str, str]:
    """从 chapter_*_result.json 确定性预统计 Top-60 人物与 Top-50 地点锚点。"""
    char_stats: Dict[str, Dict[str, Any]] = {}
    loc_stats: Dict[str, Dict[str, Any]] = {}
    all_chapters: List[int] = []

    def _ensure_char(name: str) -> Dict[str, Any]:
        entry = char_stats.get(name)
        if entry is None:
            entry = {"chapters": set(), "event_count": 0, "arcs_sample": []}
            char_stats[name] = entry
        return entry

    chapter_files: List[Tuple[int, Path]] = []
    for cf in Path(output_dir).glob("chapter_*_result.json"):
        m = re.match(r"chapter_(\d+)_result\.json$", cf.name)
        if m:
            chapter_files.append((int(m.group(1)), cf))
    chapter_files.sort(key=lambda x: x[0])

    for ch_from_name, cf in chapter_files:
        data = safe_load_json(cf)
        if not isinstance(data, dict):
            continue
        ch = int(data.get("chapter_number") or ch_from_name)
        all_chapters.append(ch)

        for ev in data.get("core_events") or []:
            if not isinstance(ev, dict):
                continue
            for cname in split_character_names(ev.get("characters")):
                st = _ensure_char(cname)
                st["chapters"].add(ch)
                st["event_count"] += 1

        for arc in data.get("character_arcs") or []:
            if not isinstance(arc, dict):
                continue
            cname = _normalize_char_name(str(arc.get("name") or ""))
            if not cname:
                continue
            st = _ensure_char(cname)
            st["chapters"].add(ch)
            delta = (arc.get("change_delta") or arc.get("surface_action") or "").strip().replace("\n", " ")
            if delta and len(st["arcs_sample"]) < 2:
                st["arcs_sample"].append(f"c{ch}:{delta[:45]}")

        for loc in data.get("locations") or []:
            if not isinstance(loc, dict):
                continue
            lname = str(loc.get("name") or "").strip()
            if not lname:
                continue
            ls = loc_stats.get(lname)
            if ls is None:
                ls = {"chapters": set(), "parents": Counter(), "types": Counter()}
                loc_stats[lname] = ls
            ls["chapters"].add(ch)
            p = str(loc.get("parent") or "").strip()
            if p and p != lname:
                ls["parents"][p] += 1
            tp = str(loc.get("type") or "").strip()
            if tp:
                ls["types"][tp] += 1

    top_chars = sorted(
        char_stats.items(),
        key=lambda kv: (len(kv[1]["chapters"]), kv[1]["event_count"]),
        reverse=True,
    )[:60]
    char_lines = []
    for name, st in top_chars:
        chs = sorted(st["chapters"])
        sample = "; ".join(st["arcs_sample"])
        span = f"{chs[0]}~{chs[-1]}章" if chs else "未知"
        char_lines.append(
            f"- {name}: 出场{len(chs)}块({span}), 事件{st['event_count']}"
            + (f", 示例[{sample}]" if sample else "")
        )

    top_locs = sorted(
        loc_stats.items(),
        key=lambda kv: len(kv[1]["chapters"]),
        reverse=True,
    )[:50]
    loc_lines = []
    for lname, ls in top_locs:
        chs = sorted(ls["chapters"])
        span = f"{chs[0]}~{chs[-1]}章" if chs else "未知"
        top_p = "/".join(p for p, _ in ls["parents"].most_common(2)) or "无"
        top_t = ls["types"].most_common(1)[0][0] if ls["types"] else ""
        loc_lines.append(f"- {lname} ({top_t}): 出现{len(chs)}块({span}), 常见上级={top_p}")

    vol_lines = [f"卷{idx}: 第{s}-{e}章" for idx, s, e in vol_ranges]
    ch_span = (
        f"第{min(all_chapters)}章 - 第{max(all_chapters)}章"
        if all_chapters
        else (f"第{vol_ranges[0][1]}章 - 第{vol_ranges[-1][2]}章" if vol_ranges else "未知")
    )
    vol_header = "【分卷章节范围】\n" + ("；".join(vol_lines) if vol_lines else "无") + f"\n全书章号范围：{ch_span}\n\n"

    return {
        "char_anchor": vol_header + "【高频原始人物 Top-60】\n" + ("\n".join(char_lines) if char_lines else "（无）"),
        "world_anchor": vol_header + "【高频原始地点 Top-50】\n" + ("\n".join(loc_lines) if loc_lines else "（无）"),
    }


def compute_atlas_fingerprint(
    stage1_fp: str,
    volume_summaries: List[str],
    model: str,
    provider: str,
) -> str:
    """计算全书图谱内容与模型失效指纹。"""
    vol_hash = hashlib.sha256("\n".join(volume_summaries or []).encode("utf-8")).hexdigest()[:16]
    payload = {
        "schema_version": STORY_ATLAS_SCHEMA_VERSION,
        "stage1_fp": stage1_fp or "",
        "vol_hash": vol_hash,
        "model": str(model or ""),
        "provider": str(provider or "auto"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def _parse_json_dict(raw_text: str) -> Optional[Dict[str, Any]]:
    if not raw_text or not isinstance(raw_text, str):
        return None
    parsed = safe_parse_json(raw_text)
    if isinstance(parsed, dict):
        return parsed
    extracted = extract_json_from_text(raw_text)
    if extracted:
        parsed2 = safe_parse_json(extracted)
        if isinstance(parsed2, dict):
            return parsed2
    return None


def parse_and_sanitize_char_atlas(raw_text: str) -> Optional[Dict[str, Any]]:
    """解析并自愈清洗人物与语义关系 JSON（去重规范名、防别名吞并独立节点、过滤悬空/自环边）。"""
    data = _parse_json_dict(raw_text)
    if not isinstance(data, dict):
        return None
    raw_chars = data.get("characters")
    if not isinstance(raw_chars, list) or not raw_chars:
        return None

    clean_chars: List[Dict[str, Any]] = []
    seen_canon: Set[str] = set()
    for item in raw_chars:
        if not isinstance(item, dict):
            continue
        cn = _normalize_char_name(str(item.get("canonical_name") or ""))
        if not cn or cn in seen_canon:
            continue
        seen_canon.add(cn)

        raw_milestones = item.get("arc_milestones") or []
        clean_milestones: List[Dict[str, str]] = []
        if isinstance(raw_milestones, list):
            for ms in raw_milestones:
                if len(clean_milestones) >= 4:
                    break
                if isinstance(ms, dict):
                    m_text = str(ms.get("milestone") or "").strip()
                    if not m_text:
                        continue
                    clean_milestones.append({
                        "chapter_range": str(ms.get("chapter_range") or "").strip(),
                        "milestone": m_text,
                    })
                elif isinstance(ms, str) and ms.strip():
                    clean_milestones.append({"chapter_range": "", "milestone": ms.strip()})

        tier = str(item.get("role_tier") or "key_support").strip()
        if tier not in VALID_ROLE_TIERS:
            tier = "key_support"
        try:
            first_ch = int(item.get("first_chapter") or 0)
        except (TypeError, ValueError):
            first_ch = 0

        raw_aliases = item.get("aliases") or []
        aliases_raw_list = [str(a).strip() for a in raw_aliases if a] if isinstance(raw_aliases, list) else []

        clean_chars.append({
            "canonical_name": cn,
            "aliases": aliases_raw_list,
            "faction": str(item.get("faction") or "").strip(),
            "role_tier": tier,
            "first_chapter": max(0, first_ch),
            "identity_summary": str(item.get("identity_summary") or "").strip(),
            "arc_milestones": clean_milestones,
        })

    if not clean_chars:
        return None

    # 二次遍历：清洗 aliases（绝不允许 alias 命中其它角色的 canonical_name，禁 <2 字与自映射）
    for c in clean_chars:
        cn = c["canonical_name"]
        dedup_aliases: List[str] = []
        seen_al: Set[str] = set()
        for raw_al in c["aliases"]:
            al = _normalize_char_name(raw_al)
            if not al or len(al) < 2 or al == cn or al in seen_canon or al in seen_al:
                continue
            seen_al.add(al)
            dedup_aliases.append(al)
        c["aliases"] = dedup_aliases

    # 清洗关系边并严格过滤悬空与自环引用
    raw_rels = data.get("relationships") or []
    clean_rels: List[Dict[str, Any]] = []
    if isinstance(raw_rels, list):
        for rel in raw_rels:
            if not isinstance(rel, dict):
                continue
            src = _normalize_char_name(str(rel.get("source") or ""))
            tgt = _normalize_char_name(str(rel.get("target") or ""))
            if not src or not tgt or src == tgt:
                continue
            if src not in seen_canon or tgt not in seen_canon:
                continue
            cat = str(rel.get("category") or "complex").strip()
            if cat not in VALID_REL_CATEGORIES:
                cat = "complex"
            ev_chs: List[int] = []
            for ch in (rel.get("evidence_chapters") or []):
                try:
                    ev_chs.append(int(ch))
                except (TypeError, ValueError):
                    continue
            clean_rels.append({
                "source": src,
                "target": tgt,
                "relation_tag": str(rel.get("relation_tag") or "").strip() or "关联",
                "category": cat,
                "evolution": str(rel.get("evolution") or "").strip(),
                "evidence_chapters": sorted(set(c for c in ev_chs if c > 0)),
            })

    return {"characters": clean_chars, "relationships": clean_rels}


def sanitize_world_tree(nodes: Any, max_depth: int = 10) -> List[Dict[str, Any]]:
    """递归清洗 world_tree：按路径防环，限制最大深度 <= max_depth。"""
    if not isinstance(nodes, list):
        return []

    def _walk(items: list, depth: int, path_seen: Set[str]) -> List[Dict[str, Any]]:
        if depth > max_depth or not isinstance(items, list):
            return []
        out: List[Dict[str, Any]] = []
        for node in items:
            if not isinstance(node, dict):
                continue
            name = str(node.get("name") or "").strip()
            if not name or name in path_seen:
                continue
            ntype = str(node.get("node_type") or "region").strip()
            if ntype not in VALID_TREE_NODE_TYPES:
                ntype = "region"
            next_seen = path_seen | {name}
            children = _walk(node.get("children") or [], depth + 1, next_seen)
            out.append({
                "name": name,
                "node_type": ntype,
                "summary": str(node.get("summary") or "").strip(),
                "children": children,
            })
        return out

    return _walk(nodes, 1, set())


def parse_and_sanitize_world_atlas(raw_text: str) -> Optional[Dict[str, Any]]:
    """解析并清洗换地图阶段流 stages 与多根世界树 world_tree。"""
    data = _parse_json_dict(raw_text)
    if not isinstance(data, dict):
        return None

    raw_stages = data.get("stages") or []
    clean_stages: List[Dict[str, Any]] = []
    if isinstance(raw_stages, list):
        for idx, st in enumerate(raw_stages, start=1):
            if not isinstance(st, dict):
                continue
            title = str(st.get("title") or "").strip()
            if not title:
                continue
            try:
                s_no = int(st.get("stage_no") or idx)
            except (TypeError, ValueError):
                s_no = idx
            try:
                s_ch = int(st.get("start_chapter") or 0)
            except (TypeError, ValueError):
                s_ch = 0
            try:
                e_ch = int(st.get("end_chapter") or s_ch)
            except (TypeError, ValueError):
                e_ch = s_ch
            if e_ch < s_ch:
                s_ch, e_ch = e_ch, s_ch
            subs = [str(x).strip() for x in (st.get("key_sub_locations") or []) if str(x).strip()]
            factions = [str(x).strip() for x in (st.get("active_factions") or []) if str(x).strip()]
            clean_stages.append({
                "stage_no": s_no,
                "title": title,
                "start_chapter": max(0, s_ch),
                "end_chapter": max(0, e_ch),
                "world_realm": str(st.get("world_realm") or "").strip(),
                "primary_location": str(st.get("primary_location") or "").strip(),
                "key_sub_locations": subs,
                "active_factions": factions,
                "core_conflict": str(st.get("core_conflict") or "").strip(),
                "map_switch_reason": str(st.get("map_switch_reason") or "").strip(),
            })

    clean_stages.sort(key=lambda s: (s["start_chapter"], s["stage_no"]))
    for i, st in enumerate(clean_stages, start=1):
        st["stage_no"] = i

    clean_tree = sanitize_world_tree(data.get("world_tree") or [], max_depth=10)
    if not clean_stages and not clean_tree:
        return None
    return {"stages": clean_stages, "world_tree": clean_tree}
