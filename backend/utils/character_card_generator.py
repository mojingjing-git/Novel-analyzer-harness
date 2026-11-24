"""
角色卡生成器
基于聚合数据生成角色卡片，并自动消费 story_atlas.json 的 aliases 映射实现全书级马甲归并。
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.config.constants import STORY_ATLAS_FILE
from backend.services.final_summary.atlas_extractor import ROLE_TIER_PRIORITY
from backend.utils.aggregate_utils import _normalize_char_name, split_character_names
from backend.utils.json_utils import safe_load_json

logger = logging.getLogger(__name__)


class CharacterCardGenerator:
    """角色卡片生成器

    优先从 aggregated 子目录读取 character_tracking / character_arcs / core_events，
    并叠加 output_dir/story_atlas.json 中的语义角色档案与别名映射表。
    """

    def __init__(self, aggregated_dir: Path):
        self.aggregated_dir = Path(aggregated_dir)
        self.character_data: Optional[Dict[str, Dict[str, Any]]] = None
        self.character_arcs: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self.core_events: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self.atlas_data: Optional[Dict[str, Any]] = None
        self._alias_map_built = False
        self._alias_to_canonical: Dict[str, str] = {}
        self._canonical_meta: Dict[str, Dict[str, Any]] = {}
        self._semantic_rels_by_char: Dict[str, List[Dict[str, Any]]] = {}
        self._merged_tracking: Optional[Dict[str, Dict[str, Any]]] = None

    def _find_aggregated(self, name: str) -> Path | None:
        """优先从 output/aggregated/ 读取最新缓存，不存在时回退根目录以兼容既有单测夹具。"""
        candidates = [
            self.aggregated_dir / "aggregated" / name,
            self.aggregated_dir / name,
        ]
        for p in candidates:
            if p.exists():
                return p
        return None

    def _load_story_atlas(self) -> None:
        atlas_path = self.aggregated_dir / STORY_ATLAS_FILE
        if not atlas_path.exists():
            self.atlas_data = None
            return
        data = safe_load_json(atlas_path)
        self.atlas_data = data if isinstance(data, dict) else None

    def load_aggregated_data(self) -> bool:
        """加载所有聚合数据与可选语义图谱（不因缺省文件中断可工作的部分）。"""
        try:
            tracking_path = self._find_aggregated("character_tracking_aggregated.json")
            if tracking_path is not None:
                with open(tracking_path, "r", encoding="utf-8") as f:
                    self.character_data = json.load(f)

            arcs_path = self._find_aggregated("character_arcs_aggregated.json")
            if arcs_path is not None:
                with open(arcs_path, "r", encoding="utf-8") as f:
                    self.character_arcs = json.load(f)

            events_path = self._find_aggregated("core_events_aggregated.json")
            if events_path is not None:
                with open(events_path, "r", encoding="utf-8") as f:
                    self.core_events = json.load(f)

            self._load_story_atlas()
            self._alias_map_built = False
            self._merged_tracking = None
            return True
        except Exception as e:
            logger.warning(f"加载聚合数据失败: {e}")
            return False

    def _ensure_alias_index(self) -> None:
        if self._alias_map_built:
            return
        self._alias_map_built = True
        self._alias_to_canonical = {}
        self._canonical_meta = {}
        self._semantic_rels_by_char = {}

        if not self.atlas_data:
            self._load_story_atlas()
        if not isinstance(self.atlas_data, dict):
            return

        raw_chars = self.atlas_data.get("characters") or []
        if not isinstance(raw_chars, list):
            return

        # 第一遍：收集所有规范角色名（拥有绝对保护权，禁止被其他角色的 aliases 吞并）
        candidates: List[Tuple[int, str, Dict[str, Any]]] = []
        canonical_set: Set[str] = set()
        for idx, item in enumerate(raw_chars):
            if not isinstance(item, dict):
                continue
            cn = _normalize_char_name(str(item.get("canonical_name") or ""))
            if not cn or cn in canonical_set:
                continue
            canonical_set.add(cn)
            self._canonical_meta[cn] = dict(item, canonical_name=cn)
            self._alias_to_canonical[cn] = cn
            candidates.append((idx, cn, item))

        # 第二遍：确定性仲裁非规范 alias（tier 优先级 -> 原总事件数降序 -> 列表顺序）
        raw_tracking = self.character_data or {}
        alias_claims: Dict[str, List[Tuple[int, int, int, str]]] = {}
        for idx, cn, item in candidates:
            tier = str(item.get("role_tier") or "key_support")
            prio = ROLE_TIER_PRIORITY.get(tier, 99)
            orig_events = int((raw_tracking.get(cn) or {}).get("total_events") or 0)
            for raw_al in (item.get("aliases") or []):
                al = _normalize_char_name(str(raw_al or ""))
                if not al or len(al) < 2 or al == cn:
                    continue
                if al in canonical_set:
                    continue
                alias_claims.setdefault(al, []).append((prio, -orig_events, idx, cn))

        for al, contenders in alias_claims.items():
            contenders.sort()
            winner = contenders[0][3]
            if len(contenders) > 1:
                others = [c[3] for c in contenders[1:]]
                logger.warning(f"别名竞争仲裁 '{al}': 由 '{winner}' 归并，忽略 {others}")
            self._alias_to_canonical[al] = winner

        # 预处理语义关系边按角色索引
        for rel in (self.atlas_data.get("relationships") or []):
            if not isinstance(rel, dict):
                continue
            src = _normalize_char_name(str(rel.get("source") or ""))
            tgt = _normalize_char_name(str(rel.get("target") or ""))
            if not src or not tgt or src == tgt:
                continue
            if src not in canonical_set or tgt not in canonical_set:
                continue
            self._semantic_rels_by_char.setdefault(src, []).append({
                "peer": tgt,
                "direction": "outgoing",
                "relation_tag": rel.get("relation_tag", ""),
                "category": rel.get("category", "complex"),
                "evolution": rel.get("evolution", ""),
                "evidence_chapters": rel.get("evidence_chapters", []),
            })
            self._semantic_rels_by_char.setdefault(tgt, []).append({
                "peer": src,
                "direction": "incoming",
                "relation_tag": rel.get("relation_tag", ""),
                "category": rel.get("category", "complex"),
                "evolution": rel.get("evolution", ""),
                "evidence_chapters": rel.get("evidence_chapters", []),
            })

    def _resolve_canonical(self, name: str) -> str:
        self._ensure_alias_index()
        cleaned = _normalize_char_name(str(name or ""))
        return self._alias_to_canonical.get(cleaned, cleaned)

    def _get_merged_tracking(self) -> Dict[str, Dict[str, Any]]:
        if self._merged_tracking is not None:
            return self._merged_tracking
        self._ensure_alias_index()

        merged: Dict[str, Dict[str, Any]] = {}
        seen_event_keys: Dict[str, Set[Tuple[int, str]]] = {}

        for raw_name, data in (self.character_data or {}).items():
            if not isinstance(data, dict):
                continue
            canon = self._resolve_canonical(raw_name)
            if not canon:
                continue
            if canon not in merged:
                merged[canon] = {
                    "name": canon,
                    "chapters": set(),
                    "events": [],
                    "state_history": {},
                    "first_appearance": None,
                }
                seen_event_keys[canon] = set()

            target = merged[canon]
            for ch in (data.get("chapters") or []):
                try:
                    target["chapters"].add(int(ch))
                except (TypeError, ValueError):
                    continue

            fa = data.get("first_appearance")
            if fa is not None:
                try:
                    fa_int = int(fa)
                    if target["first_appearance"] is None or fa_int < target["first_appearance"]:
                        target["first_appearance"] = fa_int
                except (TypeError, ValueError):
                    pass

            for ev in (data.get("events") or []):
                if not isinstance(ev, dict):
                    continue
                try:
                    ev_ch = int(ev.get("chapter") or 0)
                except (TypeError, ValueError):
                    ev_ch = 0
                ev_sig = str(ev.get("event_id") or ev.get("event") or "").strip()
                dedup_key = (ev_ch, ev_sig)
                if dedup_key in seen_event_keys[canon]:
                    continue
                seen_event_keys[canon].add(dedup_key)
                target["events"].append(ev)
                if ev_ch > 0:
                    target["chapters"].add(ev_ch)

            is_exact_canon = _normalize_char_name(raw_name) == canon
            for ch_str, st in (data.get("state_history") or {}).items():
                if ch_str not in target["state_history"] or is_exact_canon:
                    target["state_history"][ch_str] = st

        # 将仅有语义图谱档案但单章 0 事件的核心角色补入索引（防止从关系网点击弹出 404）
        for cn, meta in self._canonical_meta.items():
            if cn not in merged:
                fc = meta.get("first_chapter")
                merged[cn] = {
                    "name": cn,
                    "chapters": set(),
                    "events": [],
                    "state_history": {},
                    "first_appearance": int(fc) if fc else None,
                }

        finalized: Dict[str, Dict[str, Any]] = {}
        for cn, info in merged.items():
            sorted_chs = sorted(info["chapters"])
            sorted_events = sorted(info["events"], key=lambda e: int(e.get("chapter") or 0))
            fa = info["first_appearance"]
            if fa is None and sorted_chs:
                fa = sorted_chs[0]
            finalized[cn] = {
                "name": cn,
                "chapters": sorted_chs,
                "events": sorted_events,
                "total_events": len(sorted_events),
                "state_history": info["state_history"],
                "first_appearance": fa,
            }

        self._merged_tracking = finalized
        return finalized

    def get_character_summaries(self) -> List[Dict[str, Any]]:
        """角色卡索引（按合并后事件数降序，核心语义角色同权重优先）。"""
        if self.character_data is None:
            if not self.load_aggregated_data():
                return []
        merged = self._get_merged_tracking()
        if not merged:
            return []

        summaries: List[Dict[str, Any]] = []
        for name, data in merged.items():
            meta = self._canonical_meta.get(name) or {}
            in_atlas = bool(meta)
            summaries.append({
                "name": name,
                "aliases": list(meta.get("aliases") or []),
                "faction": str(meta.get("faction") or ""),
                "role_tier": str(meta.get("role_tier") or ""),
                "identity_summary": str(meta.get("identity_summary") or ""),
                "in_atlas": in_atlas,
                "first_appearance": data.get("first_appearance"),
                "total_events": data.get("total_events", 0),
                "chapters_count": len(data.get("chapters", [])),
            })

        summaries.sort(key=lambda x: (x["total_events"], 1 if x["in_atlas"] else 0), reverse=True)
        return summaries

    def get_card_data(self, character_name: str) -> Dict[str, Any]:
        """单个角色卡结构化详情（支持别名直查并自动合并别名下的全部弧光与事件）。"""
        if self.character_data is None:
            if not self.load_aggregated_data():
                raise KeyError(f"角色 '{character_name}' 数据未加载")
        merged = self._get_merged_tracking()
        if not merged:
            raise KeyError(f"角色 '{character_name}' 数据未加载")

        canonical = self._resolve_canonical(character_name)
        if canonical not in merged:
            raise KeyError(f"角色 '{character_name}' 不存在")

        char_info = merged[canonical]
        meta = self._canonical_meta.get(canonical) or {}
        return {
            "name": canonical,
            "first_appearance": char_info.get("first_appearance"),
            "chapters": char_info.get("chapters", []),
            "total_events": char_info.get("total_events", 0),
            "aliases": list(meta.get("aliases") or []),
            "faction": str(meta.get("faction") or ""),
            "role_tier": str(meta.get("role_tier") or ""),
            "identity_summary": str(meta.get("identity_summary") or ""),
            "arc_milestones": list(meta.get("arc_milestones") or []),
            "semantic_relationships": list(self._semantic_rels_by_char.get(canonical, [])),
            "arcs": self._get_character_arcs(canonical),
            "events": self._get_character_events(canonical),
            "states": self._get_character_states(canonical),
            "relationships": self._analyze_relationships(canonical),
        }

    def _get_character_arcs(self, canonical_name: str) -> List[Dict[str, Any]]:
        """获取角色的弧光数据（包含经 aliases 归并的别名记录）"""
        arcs: List[Dict[str, Any]] = []
        if not self.character_arcs:
            return arcs

        for chapter_key, chapter_arcs in self.character_arcs.items():
            try:
                chapter_num = int(str(chapter_key).replace("chapter_", ""))
            except ValueError:
                continue
            for arc in chapter_arcs:
                if self._resolve_canonical(arc.get("name", "")) == canonical_name:
                    arcs.append({
                        "chapter": chapter_num,
                        "surface_action": arc.get("surface_action", ""),
                        "inner_motivation": arc.get("inner_motivation", ""),
                        "change_delta": arc.get("change_delta", ""),
                        "driver": arc.get("driver", ""),
                    })

        arcs.sort(key=lambda x: x["chapter"])
        return arcs

    def _get_character_events(self, canonical_name: str) -> List[Dict[str, Any]]:
        merged = self._get_merged_tracking()
        if canonical_name not in merged:
            return []
        return list(merged[canonical_name].get("events", []))

    def _get_character_states(self, canonical_name: str) -> Dict[int, str]:
        merged = self._get_merged_tracking()
        if canonical_name not in merged:
            return {}
        states: Dict[int, str] = {}
        for chapter_str, state in (merged[canonical_name].get("state_history") or {}).items():
            try:
                states[int(chapter_str)] = state
            except ValueError:
                continue
        return states

    def _analyze_relationships(self, canonical_name: str) -> Dict[str, Dict[str, Any]]:
        """分析同场事件角色关系（按数字章号排序取首次相遇，按别名归并并过滤自环）。"""
        relationships: Dict[str, Dict[str, Any]] = {}
        if not self.core_events:
            return relationships

        sorted_chapters: List[Tuple[int, List[Dict[str, Any]]]] = []
        for chapter_key, events in self.core_events.items():
            try:
                ch_num = int(str(chapter_key).replace("chapter_", ""))
            except ValueError:
                continue
            sorted_chapters.append((ch_num, events or []))
        sorted_chapters.sort(key=lambda x: x[0])

        seen_joint: Set[Tuple[str, int, str]] = set()
        for chapter_num, events in sorted_chapters:
            for event in events:
                if not isinstance(event, dict):
                    continue
                resolved = []
                for raw_c in split_character_names(event.get("characters")):
                    rc = self._resolve_canonical(raw_c)
                    if rc:
                        resolved.append(rc)
                resolved_set = set(resolved)
                if canonical_name not in resolved_set:
                    continue

                ev_text = str(event.get("event") or "")
                ev_sig = str(event.get("id") or ev_text)
                for other in resolved_set:
                    if other == canonical_name:
                        continue
                    jkey = (other, chapter_num, ev_sig)
                    if jkey in seen_joint:
                        continue
                    seen_joint.add(jkey)
                    if other not in relationships:
                        relationships[other] = {
                            "first_encounter": chapter_num,
                            "joint_events": [],
                            "event_count": 0,
                        }
                    relationships[other]["joint_events"].append({
                        "chapter": chapter_num,
                        "event": ev_text,
                        "function": event.get("function", ""),
                    })
                    relationships[other]["event_count"] += 1

        return relationships
