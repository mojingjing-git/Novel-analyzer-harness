"""伏笔目录构建、格式化、生命周期分组与卷摘要压缩 Mixin（Layer 2）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from backend.config.constants import (
    FORESHADOW_CATEGORY_DEFS,
    FORESHADOW_CATEGORY_FALLBACK,
    FORESHADOW_CATEGORY_SCHEMA_VERSION,
    FORESHADOW_TYPE_MAP_FILE,
    normalize_foreshadow_type,
)
from backend.core.anchor import entity_key_for
from backend.utils.text_utils import deduplicate_foreshadows

from .parsers import split_lifecycle

logger = logging.getLogger(__name__)


class CatalogMixin:
    """伏笔总表筛选、格式化、分卷压缩与 reconciliation 应用逻辑。"""

    def _foreshadow_type_map_path(self) -> Path:
        return self.output_dir / FORESHADOW_TYPE_MAP_FILE

    def _load_foreshadow_type_map(self) -> Optional[Dict[str, str]]:
        """加载 per-book type→category 映射（旧数据被动兜底，无 LLM 调用）。"""
        path = self._foreshadow_type_map_path()
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("schema_version") != FORESHADOW_CATEGORY_SCHEMA_VERSION:
                logger.info(f"伏笔 type 映射 schema 版本不匹配 "
                            f"({data.get('schema_version')} -> {FORESHADOW_CATEGORY_SCHEMA_VERSION})，映射失效按原始 type 兜底")
                return None
            valid_names = {name for name, _, _ in FORESHADOW_CATEGORY_DEFS}
            mapping = data.get("mapping", {})
            return {k: v for k, v in mapping.items() if v in valid_names}
        except Exception as e:
            logger.warning(f"加载伏笔 type 映射失败: {e}，按原始 type 兜底")
            return None

    async def _build_foreshadow_catalog(
        self,
        results: List[dict],
    ) -> list:
        """从逐章分析结果的 foreshadowing 字段构建伏笔总表。"""
        a = self.config.analysis
        all_clues = []
        unique_types = set()
        for r in results:
            ch = r.get("ch", 0)
            foreshadows = r.get("foreshadowing_raw", [])
            if not foreshadows:
                continue
            for fs in foreshadows:
                if not isinstance(fs, dict):
                    continue
                clue = fs.get("clue", "")
                if not clue or len(clue.strip()) < 15:
                    continue
                ftype = fs.get("type", "") or ""
                conf = fs.get("confidence", "中") or "中"
                imp = fs.get("importance", "中") or "中"
                all_clues.append((
                    ch, clue, ftype, conf, imp,
                    fs.get("anchor_quote", "") or "",
                    int(fs.get("anchor_chapter") or 0),
                    fs.get("motif_key", "") or "",
                    fs.get("question", "") or "",
                    fs.get("answer_criteria", "") or "",
                ))
                if ftype:
                    unique_types.add(ftype)

        logger.info(f"伏笔总表原始数据：{len(all_clues)} 条线索，{len(unique_types)} 种 type")

        valid_names = {name for name, _, _ in FORESHADOW_CATEGORY_DEFS}
        type_to_category = self._load_foreshadow_type_map() or {}

        categorized = []
        skipped_importance = 0
        skipped_confidence = 0
        skipped_category = 0
        for ch, clue, ftype, conf, imp, *extra in all_clues:
            if not self._imp_at_least(imp, a.foreshadow_min_importance):
                skipped_importance += 1
                continue
            if not self._imp_at_least(conf, a.foreshadow_min_confidence):
                skipped_confidence += 1
                continue
            if ftype in valid_names:
                category = ftype
            else:
                category = type_to_category.get(ftype)
                if category is None:
                    category = normalize_foreshadow_type(ftype, valid_names)
                if category is None:
                    category = FORESHADOW_CATEGORY_FALLBACK
            if category not in set(a.foreshadow_kept_categories):
                skipped_category += 1
                continue
            categorized.append((ch, clue, ftype, conf, imp, *extra))

        logger.info(
            f"伏笔过滤：原 {len(all_clues)} 条 → "
            f"过滤后 {len(categorized)} 条（importance 丢 {skipped_importance}, "
            f"confidence 丢 {skipped_confidence}, category 丢 {skipped_category}）"
        )

        catalog = deduplicate_foreshadows(categorized)

        imp_order = {"高": 3, "中": 2, "低": 1}
        conf_order = {"高": 3, "中": 2, "低": 1}
        for c in catalog:
            evidence_count = len(c.get("evidence_chapters", []))
            span = c.get("last_seen", 0) - c.get("first_seen", 0)
            score = (
                imp_order.get(c.get("importance", "中"), 0) * 100
                + conf_order.get(c.get("confidence", "中"), 0) * 10
                + min(evidence_count, 50)
                + min(span, 49)
            )
            c["_sort_key"] = score
            c["composite_score"] = score
            c["entity_key"] = entity_key_for(c.get("motif_key", ""), str(c.get("id", "")))
        catalog.sort(key=lambda c: c["_sort_key"], reverse=True)
        for c in catalog:
            c.pop("_sort_key", None)

        high_items = [c for c in catalog if c.get("importance") == "高"]
        mid_items = [c for c in catalog if c.get("importance") == "中"]
        other_items = [c for c in catalog if c.get("importance") not in ("高", "中")]
        other_items.sort(
            key=lambda c: conf_order.get(c.get("confidence", "中"), 0),
            reverse=True,
        )

        def _stratified_select(items_list: list[dict], cap: int) -> tuple[list[dict], list[dict]]:
            if cap < 0 or len(items_list) <= cap:
                return list(items_list), []
            if cap == 0:
                return [], list(items_list)
            return list(items_list[:cap]), list(items_list[cap:])

        truncated = []
        max_high = a.max_foreshadow_catalog_high
        max_mid = a.max_foreshadow_catalog_mid
        kept_high, dropped_high = _stratified_select(high_items, max_high)
        truncated.extend(kept_high)
        high_kept = len(kept_high)
        if dropped_high:
            logger.info(f"高 importance 截断: {len(high_items)} -> {high_kept}")
        kept_mid, dropped_mid = _stratified_select(mid_items, max_mid)
        truncated.extend(kept_mid)
        mid_kept = len(kept_mid)
        if dropped_mid:
            logger.info(f"中 importance 截断: {len(mid_items)} -> {mid_kept}")
        truncated.extend(other_items)

        logger.info(
            f"伏笔总表构建完成：{len(all_clues)} 条原始 -> {len(categorized)} 条过滤 -> "
            f"{len(truncated)} 条入总表（高 {high_kept}/{len(high_items)}, "
            f"中 {mid_kept}/{len(mid_items)}, "
            f"其他 {len(other_items)}）"
        )
        return truncated

    @staticmethod
    def _imp_at_least(value: str, threshold: str) -> bool:
        """判断 value 是否 >= threshold（高>=中>=低）。"""
        order = {"高": 3, "中": 2, "低": 1}
        return order.get(value, 0) >= order.get(threshold, 0)

    def _format_catalog_for_prompt(self, catalog: list, max_chapter: int = 99999) -> str:
        """格式化截至某章节的伏笔表，用于注入 BATCH_PROMPT / RECONCILIATION_PROMPT。"""
        if not catalog:
            return "（暂无伏笔记录）\n"

        lines = []
        for item in catalog:
            if item["first_seen"] > max_chapter:
                continue
            chapters_str = ",".join(str(c) for c in item["evidence_chapters"][:8])
            merged = f" (合并{item['merged_count']}条)" if item["merged_count"] > 1 else ""
            ftype = item.get("type", "")
            type_prefix = f"[{ftype}] " if ftype else ""
            lines.append(f"[{item['id']}] {type_prefix}{item['clue']} (ch{chapters_str}){merged}")

        if not lines:
            return "（暂无伏笔记录）\n"

        return "\n".join(lines) + "\n"

    def _build_active_foreshadows_block(self, ch_start: int, ch_end: int, foreshadow_catalog: list = None) -> str:
        """构建【当前伏笔清单】文本块，使用伏笔总表的原始 clue"""
        if foreshadow_catalog:
            ledger_map = {i.id: i for i in self.ledger.items}
            items = [
                c for c in foreshadow_catalog
                if c["first_seen"] <= ch_end
                and (c["id"] not in ledger_map or (
                    ledger_map[c["id"]].status == 'active'
                    and not ledger_map[c["id"]].needs_review
                ))
            ]
        else:
            active_items = [i for i in self.ledger.items
                            if i.status == 'active' and not i.needs_review]
            items = [{"id": i.id, "clue": i.description, "evidence_chapters": i.evidence_chapters,
                      "first_seen": i.first_seen_chapter, "last_seen": i.last_seen_chapter}
                     for i in active_items]

        if not items:
            return "\n【当前伏笔清单】\n（暂无伏笔，foreshadow_reconciliation 输出空数组 [] 即可）\n"

        lines = [
            f"\n【当前伏笔清单 - 共{len(items)}个，必须逐一判断是否在第{ch_start}-{ch_end}章中被回收】",
            "（注意：仅当伏笔核心悬念在本批次中得到结论性解决时才标记回收；仅被提及/继续推进不算回收）\n"
        ]
        for i, item in enumerate(items, 1):
            eid = item["id"]
            clue = item["clue"]
            chapters_str = ",".join(str(c) for c in item.get("evidence_chapters", [])[:5])
            first = item.get("first_seen", 0)
            last = item.get("last_seen", 0)
            lines.append(f'{i}. id="{eid}"  描述: {clue}')
            lines.append(f"   首次出现: 第{first}章  最近出现: 第{last}章  证据: [{chapters_str}]")

        return "\n".join(lines)

    def _build_foreshadow_context(self, foreshadow_catalog: list = None) -> str:
        """构建供 FINAL_PROMPT 使用的伏笔上下文——伏笔总表 clue + 账本状态合并"""
        if not self.ledger.items and not foreshadow_catalog:
            return ""

        ledger_map = {i.id: i for i in self.ledger.items}

        all_items = []
        catalog_ids = set()
        if foreshadow_catalog:
            for cat in foreshadow_catalog:
                lid = cat["id"]
                catalog_ids.add(lid)
                ledger_item = ledger_map.get(lid)
                all_items.append({
                    "id": lid,
                    "clue": cat["clue"],
                    "first_seen": cat["first_seen"],
                    "last_seen": cat["last_seen"],
                    "evidence_chapters": cat["evidence_chapters"],
                    "status": ledger_item.status if ledger_item else "active",
                    "resolution": ledger_item.resolution if ledger_item else None,
                    "resolved_chapter": ledger_item.resolved_chapter if ledger_item else None,
                    "needs_review": bool(ledger_item.needs_review) if ledger_item else False,
                })

        for lid, item in ledger_map.items():
            if lid not in catalog_ids:
                all_items.append({
                    "id": lid,
                    "clue": item.description,
                    "first_seen": item.first_seen_chapter,
                    "last_seen": item.last_seen_chapter,
                    "evidence_chapters": item.evidence_chapters,
                    "status": item.status,
                    "resolution": item.resolution,
                    "resolved_chapter": item.resolved_chapter,
                    "needs_review": bool(item.needs_review),
                })

        if not all_items:
            return ""

        groups = split_lifecycle(all_items)
        active = groups["active"]
        pending = groups["pending"]
        resolved = groups["resolved"]
        dormant = groups["dormant"]
        frozen = groups["frozen"]

        parts = []
        parts.append(
            f"## 伏笔总表（共{len(all_items)}条：活跃{len(active)} / 待确认{len(pending)} / "
            f"已回收{len(resolved)} / 休眠{len(dormant)} / 冻结待审{len(frozen)}）\n"
        )

        if resolved:
            parts.append("### 已回收伏笔")
            for item in resolved:
                chapters_str = ",".join(str(c) for c in item["evidence_chapters"][:5])
                parts.append(f'- [{item["id"]}] {item["clue"]} (ch{chapters_str})')
                res = item.get("resolution") or "未记录"
                rch = item.get("resolved_chapter") or "?"
                parts.append(f'  - 回收: ch{rch}, {res}')
            parts.append("")

        if pending:
            parts.append("### 待确认伏笔（复检判定证据不足或冲突 / 采集自述待裁决）")
            for item in pending:
                chapters_str = ",".join(str(c) for c in item["evidence_chapters"][:5])
                parts.append(f'- [{item["id"]}] {item["clue"]} (ch{chapters_str})')
            parts.append("")

        if active:
            parts.append("### 仍活跃伏笔（请逐一分析是否在全书中已被回收）")
            for item in active:
                chapters_str = ",".join(str(c) for c in item["evidence_chapters"][:5])
                parts.append(f'- [{item["id"]}] {item["clue"]} (ch{chapters_str})')
            parts.append("")

        if dormant:
            parts.append("### 休眠伏笔（长期未出现，可能是作者遗忘）")
            for item in dormant:
                parts.append(f'- [{item["id"]}] {item["clue"]} (ch{item["first_seen"]}-{item["last_seen"]})')
            parts.append("")

        if frozen:
            parts.append("### 冻结待审伏笔（复检后待人工确认，已被冻结）")
            for item in frozen:
                parts.append(f'- [{item["id"]}] {item["clue"]} (ch{item["first_seen"]}-{item["last_seen"]})')
            parts.append("")

        parts.append("请在报告中特别关注：")
        parts.append('1. 标记为"已回收"的伏笔，分析其回收质量')
        parts.append('2. 标记为"休眠"的伏笔，这些可能是作者遗忘的伏笔')
        parts.append('3. 标记为"活跃"的伏笔——**请逐一核对**，判断它们在全书中是否实际上已经被回收了')
        parts.append('4. 标记为"待确认/冻结待审"的伏笔**不需要再逐一核对**——复检已给出"证据不足"结论，'
                     '请在报告中指出它们当前的进展、以及后文是否存在可能的真正回收位置')
        parts.append("5. 所有伏笔结论必须带证据章节号")

        return "\n".join(parts)

    def _compress_volume_summaries(self, volume_summaries: List[str]) -> str:
        """卷摘要拼接总字符超阈值时做轻量分层压缩（首尾各 1 组保留全文，中间组截断到 1/2）。"""
        threshold = self.config.analysis.volume_compress_threshold
        group_size = self.config.analysis.volume_compress_group
        joined = "\n\n".join(volume_summaries)
        if len(joined) <= threshold or len(volume_summaries) <= 1:
            return joined
        logger.info(f"最终报告卷摘要超阈值（{len(joined)} 字符 > {threshold}），"
                    f"执行分层压缩（{len(volume_summaries)} 卷，组大小 {group_size}）")
        groups = [volume_summaries[i:i + group_size]
                  for i in range(0, len(volume_summaries), group_size)]
        parts = []
        n = len(groups)
        for gi, g in enumerate(groups):
            if gi == 0 or gi == n - 1:
                parts.append("\n\n".join(g))
            else:
                trimmed = [vs[:len(vs) // 2] + "\n（此处已压缩，省略后半）" for vs in g]
                parts.append("\n\n".join(trimmed))
        return "\n\n".join(parts)

    def _apply_reconciliation(self, batch_idx: int, ch_start: int, ch_end: int, foreshadow_data: Optional[dict]) -> None:
        """应用 LLM 的 reconciliation 结果到账本。"""
        if foreshadow_data is None:
            logger.warning(f"批次{batch_idx}：reconciliation 缺失，伏笔状态保持不变")
            return
        reconciliation = foreshadow_data.get('reconciliation', []) or []
        for item in reconciliation:
            if not isinstance(item, dict):
                continue
            foreshadow_id = item.get('foreshadow_id', '')
            if not foreshadow_id:
                continue
            existing = self.ledger._find_item(foreshadow_id)
            is_resolved = bool(item.get('is_resolved_in_this_batch', False))
            if is_resolved:
                if existing and existing.status == 'active':
                    existing.resolution = item.get('resolution_summary') or item.get('reason') or ''
                    existing.resolved_chapter = item.get('resolved_chapter') or ch_end
                    existing.last_seen_chapter = ch_end
                    existing.last_seen_batch = batch_idx
                    existing.needs_review = True
                    note = f"自述回收待证据裁决（批次{batch_idx}）：自报 ch{existing.resolved_chapter}"
                    if note not in existing.notes:
                        existing.notes.append(note)
                    logger.info(f"自述回收降为提名: {foreshadow_id} (自报第{existing.resolved_chapter}章)")
