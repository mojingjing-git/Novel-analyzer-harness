"""纯路径书目产物读写与账本覆写层（无进程级单例、无日志/API 客户端导入副作用）。"""

from __future__ import annotations

from dataclasses import asdict
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..common.file_lock import BookFileLock
from ..config.constants import STORY_ATLAS_FILE, STYLE_PROFILE_CHECKPOINT_REL
from ..utils.foreshadow_ledger import ForeshadowItem, ForeshadowLedger

logger = logging.getLogger(__name__)


def is_book_dir(d: Path) -> bool:
    """书目录判定：含 ``blocks/`` 或 ``output/`` 子目录。"""
    return d.is_dir() and ((d / "blocks").exists() or (d / "output").exists())


def count_results(output_dir: Path) -> Tuple[int, int, int]:
    """统计已落盘章节结果：``(数量, 最小章号, 最大章号)``。"""
    chapters: List[int] = []
    if output_dir.exists():
        for f in output_dir.glob("chapter_*_result.json"):
            m = re.match(r"chapter_(\d+)_result\.json", f.name)
            if m:
                chapters.append(int(m.group(1)))
    if not chapters:
        return 0, 0, 0
    return len(chapters), min(chapters), max(chapters)


def book_info(book_id: str, path: Path) -> Dict[str, Any]:
    """构建单本书目的只读状态摘要。"""
    output_dir = path / "output"
    result_count, ch_min, ch_max = count_results(output_dir)
    blocks_dir = path / "blocks"
    total_blocks = (
        len([f for f in blocks_dir.glob("*.txt") if re.match(r"^\d+\.txt$", f.name)])
        if blocks_dir.exists()
        else 0
    )
    has_report = output_dir.exists() and any(output_dir.glob("final_summary_report*"))
    return {
        "id": book_id,
        "name": book_id,
        "path": path.as_posix(),
        "total_blocks": total_blocks,
        "total_chapters": total_blocks,
        "result_count": result_count,
        "chapter_min": ch_min,
        "chapter_max": ch_max,
        "has_report": has_report,
        "has_ledger": (output_dir / "foreshadow_ledger.json").exists(),
        "has_audit": (output_dir / "foreshadow_audit.md").exists(),
        "has_aggregated": (output_dir / "aggregated").exists(),
        "has_atlas": (output_dir / STORY_ATLAS_FILE).exists(),
    }


def load_style_profile_from_output(output_dir: Path, book_id: str) -> Dict[str, Any]:
    """从指定 ``output_dir`` 读取 ``style_profile.json``。"""
    path = output_dir / STYLE_PROFILE_CHECKPOINT_REL
    empty: Dict[str, Any] = {
        "book_id": book_id,
        "exists": False,
        "statistical": None,
        "semantic": None,
        "sample_chapters": [],
    }
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        profile = raw.get("profile") if isinstance(raw, dict) else None
        if not isinstance(profile, dict):
            return empty
        stat = profile.get("statistical")
        sem = profile.get("semantic")
        samples = profile.get("sample_chapters") or []
        if not isinstance(stat, dict) or not stat:
            return empty
        return {
            "book_id": book_id,
            "exists": True,
            "statistical": stat,
            "semantic": sem if isinstance(sem, dict) else None,
            "sample_chapters": samples if isinstance(samples, list) else [],
        }
    except Exception as e:
        logger.warning(f"读取 style_profile.json 失败 ({book_id}): {e}")
        return empty


def load_recheck_raw(output_dir: Path) -> Optional[Dict[str, Any]]:
    """读取原始 ``recheck_verdicts.json`` 字典，不存在或损坏返回 ``None``。"""
    path = output_dir / "recheck_verdicts.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception as e:
        logger.warning(f"读取 recheck_verdicts.json 失败 ({output_dir}): {e}")
        return None


def load_recheck_verdicts_from_output(output_dir: Path, book_id: str) -> Dict[str, Any]:
    """读取落盘的 ``recheck_verdicts.json`` 并叠加账本实时状态字段。"""
    empty: Dict[str, Any] = {
        "book_id": book_id,
        "exists": False,
        "engine": "",
        "status": "",
        "status_reason": "",
        "model": "",
        "generated_at": "",
        "coverage": {},
        "candidates_total": 0,
        "verdicts": [],
    }
    raw = load_recheck_raw(output_dir)
    if raw is None:
        return empty
    try:
        coverage = raw.get("coverage") if isinstance(raw.get("coverage"), dict) else {}
        audit_routes = raw.get("audit_routes") if isinstance(raw.get("audit_routes"), dict) else {}
        applied_map = raw.get("applied") if isinstance(raw.get("applied"), dict) else {}
        resolved_map = raw.get("resolved_chapters") if isinstance(raw.get("resolved_chapters"), dict) else {}

        is_v3 = bool(coverage.get("v3_tiers")) or any(
            isinstance(r, dict) and r.get("engine") == "v3"
            for r in audit_routes.values()
        )
        engine = "v3" if is_v3 else "v2"

        ledger_by_id: Dict[str, Dict[str, Any]] = {}
        ledger_path = output_dir / "foreshadow_ledger.json"
        if ledger_path.exists():
            try:
                ledger_raw = json.loads(ledger_path.read_text(encoding="utf-8"))
                for item in ledger_raw.get("items") or []:
                    if isinstance(item, dict) and item.get("id"):
                        ledger_by_id[str(item["id"])] = item
            except Exception as le:
                logger.warning(f"补充读取 foreshadow_ledger.json 失败 ({book_id}): {le}")

        verdicts_out: List[Dict[str, Any]] = []
        for v in raw.get("verdicts") or []:
            if not isinstance(v, dict):
                continue
            cid = str(v.get("candidate_id") or v.get("id") or "")
            if not cid:
                continue
            ar = audit_routes.get(cid) if isinstance(audit_routes.get(cid), dict) else {}
            l_item = ledger_by_id.get(cid) or {}
            verdicts_out.append({
                "id": cid,
                "candidate_id": cid,
                "verdict": str(v.get("verdict") or ""),
                "tier": str(ar.get("tier") or ""),
                "unresolved_type": str(ar.get("unresolved_type") or ""),
                "answer": str(v.get("answer") or ""),
                "quote": str(v.get("quote") or ""),
                "reason": str(v.get("reason") or ""),
                "applied": str(applied_map.get(cid) or ""),
                "resolved_chapter": str(resolved_map.get(cid) or ""),
                "hops_used": int(ar.get("hops_used") or 1),
                "evidence_chain": list(ar.get("evidence_chain") or []),
                "gate_demoted": bool(ar.get("gate_demoted", False)),
                "gate_reason": str(ar.get("gate_reason") or ""),
                "repaired": bool(ar.get("repaired", False)),
                "description": str(l_item.get("description") or ""),
                "question": str(l_item.get("question") or ""),
                "first_seen_chapter": l_item.get("first_seen_chapter"),
                "importance": str(l_item.get("importance") or ""),
                "ledger_status": str(l_item.get("status") or ""),
                "ledger_needs_review": bool(l_item.get("needs_review", False)),
                "ledger_resolution_quality": str(l_item.get("resolution_quality") or ""),
                "ledger_source_type": str(l_item.get("source_type") or ""),
                "ledger_resolution": l_item.get("resolution"),
                "ledger_resolved_chapter": l_item.get("resolved_chapter"),
            })

        return {
            "book_id": book_id,
            "exists": True,
            "engine": engine,
            "status": str(raw.get("status") or ""),
            "status_reason": str(raw.get("status_reason") or ""),
            "model": str(raw.get("model") or ""),
            "generated_at": str(raw.get("generated_at") or ""),
            "coverage": coverage,
            "candidates_total": int(raw.get("candidates_total") or coverage.get("candidates_total") or len(verdicts_out)),
            "verdicts": verdicts_out,
        }
    except Exception as e:
        logger.warning(f"读取 recheck_verdicts.json 失败 ({book_id}): {e}")
        return empty


def load_foreshadow_ranking_from_output(output_dir: Path, book_id: str) -> Dict[str, Any]:
    """从指定 ``output_dir`` 读取 ``foreshadow_ledger.json`` 并计算综合排行与七栏计数。"""
    ledger_path = output_dir / "foreshadow_ledger.json"
    if not ledger_path.exists():
        raise FileNotFoundError("伏笔账本不存在，请先运行最终总结")

    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    items = data.get("items", [])

    ranking: List[Dict[str, Any]] = []
    for item in items:
        evidence_count = len(item.get("evidence_chapters", []))
        first_seen = item.get("first_seen_chapter", 0)
        last_seen = item.get("last_seen_chapter", 0)
        span = last_seen - first_seen
        score = item.get("composite_score", 0)

        if score == 0 and item.get("importance"):
            imp_order = {"高": 3, "中": 2, "低": 1}
            conf_val = item.get("confidence", 0.8)
            if conf_val >= 0.8:
                conf_str = "高"
            elif conf_val >= 0.5:
                conf_str = "中"
            else:
                conf_str = "低"
            score = (
                imp_order.get(item.get("importance", "中"), 0) * 100
                + imp_order.get(conf_str, 0) * 10
                + min(evidence_count, 50)
                + min(span, 49)
            )

        ranking.append({
            "id": item.get("id", ""),
            "description": item.get("description", ""),
            "composite_score": score,
            "importance": item.get("importance", "中"),
            "confidence": item.get("confidence", 0.8),
            "status": item.get("status", "active"),
            "first_seen_chapter": first_seen,
            "last_seen_chapter": last_seen,
            "span": span,
            "evidence_count": evidence_count,
            "evidence_chapters": item.get("evidence_chapters", []),
            "resolution": item.get("resolution"),
            "resolved_chapter": item.get("resolved_chapter"),
            "needs_review": bool(item.get("needs_review", False)),
            "resolution_quality": item.get("resolution_quality", ""),
            "source_type": item.get("source_type", ""),
        })

    ranking.sort(key=lambda x: x["composite_score"], reverse=True)
    lifecycle_bars = ForeshadowLedger.from_dict(data).lifecycle_bars()

    return {
        "book_id": book_id,
        "total": len(ranking),
        "ranking": ranking,
        "lifecycle_bars": lifecycle_bars,
    }


def load_chapter_result_for_chapter(output_dir: Path, chapter: int) -> Optional[Dict[str, Any]]:
    """读取包含目标章号 ``chapter`` 的块级分析结果（兼容 ``block_size > 1`` 块合并场景）。"""
    if chapter <= 0 or not output_dir.exists():
        return None

    direct = output_dir / f"chapter_{chapter}_result.json"
    if direct.exists():
        try:
            data = json.loads(direct.read_text(encoding="utf-8"))
            src = data.get("source_chapters")
            return {
                "chapter": chapter,
                "result_block_chapter": chapter,
                "source_chapters": list(src) if isinstance(src, list) and src else [chapter],
                "data": data,
            }
        except Exception as e:
            logger.error(f"读取章节结果失败 {direct}: {e}")
            return None

    candidates: List[Tuple[int, Path]] = []
    for f in output_dir.glob("chapter_*_result.json"):
        m = re.match(r"chapter_(\d+)_result\.json", f.name)
        if m:
            candidates.append((int(m.group(1)), f))
    candidates.sort(key=lambda x: x[0])

    for block_id, fpath in candidates:
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        src = data.get("source_chapters")
        if isinstance(src, list) and src:
            if chapter in src:
                return {
                    "chapter": chapter,
                    "result_block_chapter": block_id,
                    "source_chapters": list(src),
                    "data": data,
                }
        else:
            bs = max(1, int(data.get("block_size") or 1))
            if block_id <= chapter < block_id + bs:
                return {
                    "chapter": chapter,
                    "result_block_chapter": block_id,
                    "source_chapters": list(range(block_id, block_id + bs)),
                    "data": data,
                }
    return None


def infer_result_block_size(output_dir: Path, default: int = 1) -> int:
    """从磁盘结果文件推断实际分析粒度（兼容历史合块归档与单章协议产物）。"""
    fallback = max(1, int(default or 1))
    if not output_dir or not output_dir.exists():
        return fallback
    candidates: List[Tuple[int, Path]] = []
    for f in output_dir.glob("chapter_*_result.json"):
        m = re.match(r"chapter_(\d+)_result\.json", f.name)
        if m:
            candidates.append((int(m.group(1)), f))
    candidates.sort(key=lambda x: x[0])

    inferred = 0
    sampled = 0
    for _ch, fpath in candidates:
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        sampled += 1
        raw_bs = data.get("block_size")
        bs_val = (
            int(raw_bs)
            if isinstance(raw_bs, (int, float)) and not isinstance(raw_bs, bool)
            else 1
        )
        src = data.get("source_chapters")
        src_len = len(src) if isinstance(src, list) else 0
        inferred = max(inferred, 1, bs_val, src_len)
        if sampled >= 20:
            break
    return inferred if sampled > 0 else fallback


def review_foreshadow_ledger(
    output_dir: Path,
    book_id: str,
    item_id: str,
    action: str,
    *,
    resolution: Optional[str] = None,
    resolved_chapter: Optional[int] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """在跨进程书目文件锁保护下对单条伏笔条目应用人工覆写。"""
    ledger_path = output_dir / "foreshadow_ledger.json"
    if not ledger_path.exists():
        raise LookupError("ledger_not_found")

    with BookFileLock(output_dir.parent / ".book.lock"):
        ledger = ForeshadowLedger.load(ledger_path)
        item = ledger.apply_human_override(
            item_id,
            action,
            resolution=resolution,
            resolved_chapter=resolved_chapter,
            note=note,
        )
        if not ledger.save(ledger_path):
            raise RuntimeError("ledger_save_failed")
        return {
            "book_id": book_id,
            "item": asdict(item),
            "lifecycle_bars": ledger.lifecycle_bars(),
            "counts": ledger.get_item_count(),
        }


def batch_review_foreshadow_ledger(
    output_dir: Path,
    book_id: str,
    entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """在跨进程文件锁保护下原子批量覆写伏笔条目（任一条校验失败则整批不落盘）。"""
    if not isinstance(entries, list) or not entries:
        raise ValueError("批量复核条目列表不能为空")

    normalized: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"第 {idx + 1} 项格式无效，必须为对象")
        item_id = str(entry.get("item_id") or entry.get("id") or "").strip()
        action = str(entry.get("action") or "").strip()
        if not item_id:
            raise ValueError(f"第 {idx + 1} 项缺少 item_id")
        if item_id in seen_ids:
            raise ValueError(f"存在重复的伏笔条目 ID: {item_id}")
        seen_ids.add(item_id)
        normalized.append({
            "item_id": item_id,
            "action": action,
            "resolution": entry.get("resolution"),
            "resolved_chapter": entry.get("resolved_chapter"),
            "note": entry.get("note"),
        })

    ledger_path = output_dir / "foreshadow_ledger.json"
    if not ledger_path.exists():
        raise LookupError("ledger_not_found")

    with BookFileLock(output_dir.parent / ".book.lock"):
        ledger = ForeshadowLedger.load(ledger_path)
        updated: List[ForeshadowItem] = []
        for entry in normalized:
            updated.append(
                ledger.apply_human_override(
                    entry["item_id"],
                    entry["action"],
                    resolution=entry["resolution"],
                    resolved_chapter=entry["resolved_chapter"],
                    note=entry["note"],
                )
            )
        if not ledger.save(ledger_path):
            raise RuntimeError("ledger_save_failed")
        return {
            "book_id": book_id,
            "updated_count": len(updated),
            "items": [asdict(it) for it in updated],
            "lifecycle_bars": ledger.lifecycle_bars(),
            "counts": ledger.get_item_count(),
        }
