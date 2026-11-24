"""CLI 伏笔账本与复检裁决命令集：list / show / verdicts / review / batch-review。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple

from .runtime import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    CliContext,
    CliError,
    LiveServer,
    discover_live_server,
    emit_output,
    load_cli_config,
    resolve_book,
    resolve_two_positionals,
    server_matches_book_dir,
)

VALID_REVIEW_ACTIONS = ("confirm_resolved", "reject_to_active", "mark_dormant")


def handle_ledger_list(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """筛选并分页展示伏笔账本条目及七栏生命周期计数。"""
    from backend.services.book_artifacts import load_foreshadow_ranking_from_output

    book_id, book_dir = resolve_book(ctx, getattr(args, "book", None))
    output_dir = book_dir / "output"
    try:
        ranking_data = load_foreshadow_ranking_from_output(output_dir, book_id)
    except FileNotFoundError as exc:
        raise CliError(EXIT_NOT_FOUND, "ledger_not_found", f"书目 {book_id} 的伏笔账本不存在，请先运行最终总结") from exc

    items = list(ranking_data.get("ranking") or [])
    status_filter = getattr(args, "status", None)
    needs_review_filter = getattr(args, "needs_review", None)
    importance_filter = getattr(args, "importance", None)
    source_type_filter = getattr(args, "source_type", None)

    filtered: List[Dict[str, Any]] = []
    for it in items:
        if status_filter and it.get("status") != status_filter:
            continue
        if needs_review_filter is not None and bool(it.get("needs_review")) is not needs_review_filter:
            continue
        if importance_filter and it.get("importance") != importance_filter:
            continue
        if source_type_filter and it.get("source_type") != source_type_filter:
            continue
        filtered.append(it)

    sort_by = str(getattr(args, "sort", "score") or "score")
    if sort_by == "first_seen":
        filtered.sort(key=lambda x: (x.get("first_seen_chapter", 0), x.get("id", "")))
    elif sort_by == "last_seen":
        filtered.sort(key=lambda x: (x.get("last_seen_chapter", 0), x.get("id", "")), reverse=True)
    else:
        filtered.sort(key=lambda x: x.get("composite_score", 0), reverse=True)

    offset = int(getattr(args, "offset", 0) or 0)
    limit = int(getattr(args, "limit", 50) or 50)
    if offset < 0 or limit <= 0:
        raise CliError(EXIT_USAGE, "invalid_argument", "offset 不能为负，且 limit 必须为正整数")
    page = filtered[offset : offset + limit]

    data = {
        "book_id": book_id,
        "total": ranking_data.get("total", len(items)),
        "filtered_total": len(filtered),
        "offset": offset,
        "limit": limit,
        "lifecycle_bars": ranking_data.get("lifecycle_bars", {}),
        "items": page,
    }
    lines = [
        f"书目 {book_id} 伏笔列表（筛选 {len(filtered)}/{data['total']}，当前返回 {len(page)} 条）："
    ]
    for it in page:
        lines.append(
            f"- {it['id']} [{it['status']}|重要:{it['importance']}|待确认:{it['needs_review']}] "
            f"ch{it['first_seen_chapter']}->{it['last_seen_chapter']} | {it['description'][:60]}"
        )
    emit_output(ctx, data, "\n".join(lines))
    return data


def _slice_around(text: str, pos: int, needle_len: int, radius: int) -> Dict[str, Any]:
    start = max(0, pos - radius)
    end = min(len(text), pos + needle_len + radius)
    return {
        "offset": pos,
        "window_start": start,
        "window_end": end,
        "snippet": text[start:end],
    }


def _extract_item_evidence(
    ctx: CliContext,
    book_dir: Path,
    item: Any,
    verdict: Optional[Dict[str, Any]],
    locate_records: Dict[str, Any],
    radius: int,
) -> Dict[str, Any]:
    """为单条伏笔定位埋设原文窗与回收证据窗（零 fuzzy difflib）。"""
    from backend.core.file_processor import FileProcessor
    from backend.core.text_units import find_literal, load_blocks_as_files, locate_with_mode

    blocks_dir = book_dir / "blocks"
    if not blocks_dir.is_dir():
        return {"available": False, "reason": "blocks_dir_missing", "setup": None, "payoffs": []}

    cfg, _ = load_cli_config(ctx, require_valid=False)
    block_size = max(1, int(getattr(getattr(cfg, "analysis", None), "block_size", 4) or 4))

    fp = FileProcessor(blocks_dir, cfg.analysis.encoding_priority)
    available_chs = fp.scan_chapters()
    unit_cache: Dict[int, str] = {}

    def _get_unit(unit_id: int) -> Optional[str]:
        if unit_id in unit_cache:
            return unit_cache[unit_id]
        if unit_id not in available_chs:
            return None
        txt = fp.read_chapter(unit_id)
        if txt is not None:
            unit_cache[unit_id] = txt
        return txt

    # 1. 埋设原文窗：优先复用 recheck_verdicts.json 中的 locate_records
    setup_win: Optional[Dict[str, Any]] = None
    rec = locate_records.get(item.id) if isinstance(locate_records, dict) else None
    quote = str(getattr(item, "anchor_quote", "") or "").strip()
    if isinstance(rec, dict) and rec.get("unit") is not None:
        su = int(rec["unit"])
        txt = _get_unit(su)
        if txt is not None:
            off = rec.get("anchor_offset")
            if off is None and quote:
                off = find_literal(txt, quote)
            if off is not None and 0 <= int(off) < len(txt):
                win = _slice_around(txt, int(off), max(1, len(quote)), radius)
                setup_win = {"unit": su, "mode": str(rec.get("mode") or "locate_record"), **win}
            elif quote:
                setup_win = {"unit": su, "mode": str(rec.get("mode") or "unit_head"), **_slice_around(txt, 0, min(len(txt), 120), radius)}

    # 回退：未记录 locate_records 时先查候选章窗，再全书字面检索
    if setup_win is None and quote:
        first_seen = max(1, int(getattr(item, "first_seen_chapter", 1) or 1))
        cand_end = first_seen + max(7, block_size * 2)
        cand_units: Dict[int, str] = {}
        for ch in available_chs:
            if first_seen <= ch <= cand_end:
                txt = _get_unit(ch)
                if txt is not None:
                    cand_units[ch] = txt
        hit = locate_with_mode(cand_units, quote, candidate_range=(first_seen, cand_end))
        if hit.unit is None:
            all_units, _ = load_blocks_as_files(blocks_dir)
            hit = locate_with_mode(all_units, quote, candidate_range=(first_seen, cand_end))
            unit_cache.update(all_units)
        if hit.unit is not None and hit.offset is not None:
            txt = unit_cache.get(hit.unit) or _get_unit(hit.unit) or ""
            setup_win = {
                "unit": hit.unit,
                "mode": hit.mode,
                **_slice_around(txt, int(hit.offset), len(quote), radius),
            }

    # 2. 回收证据窗：优先使用 V3 evidence_chain，其次 verdict.quote
    payoffs: List[Dict[str, Any]] = []
    if verdict:
        chain = verdict.get("evidence_chain") or []
        for entry in chain:
            if not isinstance(entry, dict):
                continue
            raw_u = entry.get("unit_id")
            try:
                uid = int(raw_u) if raw_u is not None else None
            except (TypeError, ValueError):
                uid = None
            ev_quote = str(entry.get("quote") or "").strip()
            win_info: Dict[str, Any] = {"unit": uid, "quote": ev_quote}
            if uid is not None:
                txt = _get_unit(uid)
                if txt is not None and ev_quote:
                    pos = find_literal(txt, ev_quote)
                    if pos is not None:
                        win_info.update(_slice_around(txt, pos, len(ev_quote), radius))
            payoffs.append(win_info)

        if not payoffs and verdict.get("quote"):
            vq = str(verdict["quote"]).strip()
            rch = verdict.get("resolved_chapter") or getattr(item, "resolved_chapter", None)
            try:
                uid = int(rch) if rch else None
            except (TypeError, ValueError):
                uid = None
            if uid is not None:
                txt = _get_unit(uid)
                if txt is not None:
                    pos = find_literal(txt, vq)
                    if pos is not None:
                        payoffs.append({"unit": uid, "quote": vq, **_slice_around(txt, pos, len(vq), radius)})

    return {"available": True, "setup": setup_win, "payoffs": payoffs}


def handle_ledger_show(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """展示单条伏笔明细、最新复检裁决以及可选的原文证据窗口。"""
    from backend.services.book_artifacts import (
        load_recheck_raw,
        load_recheck_verdicts_from_output,
    )
    from backend.utils.foreshadow_ledger import ForeshadowLedger

    book_arg, item_id = resolve_two_positionals(ctx, args.arg1, args.arg2, "伏笔 ID")
    book_id, book_dir = resolve_book(ctx, book_arg)
    output_dir = book_dir / "output"
    ledger_path = output_dir / "foreshadow_ledger.json"
    if not ledger_path.exists():
        raise CliError(EXIT_NOT_FOUND, "ledger_not_found", f"书目 {book_id} 的伏笔账本不存在")

    ledger = ForeshadowLedger.load(ledger_path)
    item = ledger._find_item(item_id)
    if item is None:
        raise CliError(EXIT_NOT_FOUND, "item_not_found", f"未找到伏笔条目: {item_id}")

    verdicts_data = load_recheck_verdicts_from_output(output_dir, book_id)
    verdict = next(
        (v for v in (verdicts_data.get("verdicts") or []) if v.get("id") == item_id),
        None,
    )

    data: Dict[str, Any] = {
        "book_id": book_id,
        "item": asdict(item),
        "verdict": verdict,
    }
    if getattr(args, "with_evidence", False):
        radius = int(getattr(args, "radius", 240) if getattr(args, "radius", None) is not None else 240)
        if radius < 0:
            raise CliError(EXIT_USAGE, "invalid_argument", "radius 不能为负")
        raw = load_recheck_raw(output_dir) or {}
        locate_records = raw.get("locate_records") if isinstance(raw.get("locate_records"), dict) else {}
        data["evidence"] = _extract_item_evidence(ctx, book_dir, item, verdict, locate_records, radius)

    lines = [
        f"伏笔 {item.id} [{item.status}] (book={book_id})",
        f"描述: {item.description}",
        f"章节跨度: 第{item.first_seen_chapter}章 -> 第{item.last_seen_chapter}章 | "
        f"重要度: {item.importance} | 待确认: {item.needs_review}",
    ]
    if item.question:
        lines.append(f"问题: {item.question}")
    if item.resolution:
        lines.append(f"回收: {item.resolution} (ch{item.resolved_chapter})")
    if verdict:
        lines.append(f"复检裁决: verdict={verdict.get('verdict')} tier={verdict.get('tier')} reason={verdict.get('reason')}")
    emit_output(ctx, data, "\n".join(lines))
    return data


def handle_ledger_verdicts(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """筛选并查看 V2/V3 伏笔复检逐条裁决。"""
    from backend.services.book_artifacts import load_recheck_verdicts_from_output

    book_id, book_dir = resolve_book(ctx, getattr(args, "book", None))
    output_dir = book_dir / "output"
    verdict_data = load_recheck_verdicts_from_output(output_dir, book_id)
    if not verdict_data.get("exists"):
        raise CliError(EXIT_NOT_FOUND, "verdicts_not_found", f"书目 {book_id} 尚无复检裁决明细 (recheck_verdicts.json)")

    verdicts = list(verdict_data.get("verdicts") or [])
    tier_filter = getattr(args, "tier", None)
    verdict_filter = getattr(args, "verdict", None)
    needs_review_filter = getattr(args, "needs_review", None)
    gate_demoted_filter = getattr(args, "gate_demoted", None)

    filtered: List[Dict[str, Any]] = []
    for v in verdicts:
        if tier_filter and v.get("tier") != tier_filter:
            continue
        if verdict_filter and v.get("verdict") != verdict_filter:
            continue
        if needs_review_filter is not None and bool(v.get("ledger_needs_review")) is not needs_review_filter:
            continue
        if gate_demoted_filter is not None and bool(v.get("gate_demoted")) is not gate_demoted_filter:
            continue
        filtered.append(v)

    offset = int(getattr(args, "offset", 0) or 0)
    limit = int(getattr(args, "limit", 50) or 50)
    if offset < 0 or limit <= 0:
        raise CliError(EXIT_USAGE, "invalid_argument", "offset 不能为负，且 limit 必须为正整数")
    page = filtered[offset : offset + limit]

    data = {
        **verdict_data,
        "filtered_total": len(filtered),
        "offset": offset,
        "limit": limit,
        "verdicts": page,
    }
    lines = [
        f"书目 {book_id} 复检明细 (engine={verdict_data.get('engine')}, status={verdict_data.get('status')}, "
        f"筛选 {len(filtered)}/{len(verdicts)}，展示 {len(page)} 条)："
    ]
    for v in page:
        lines.append(
            f"- {v['id']} | verdict={v.get('verdict')} tier={v.get('tier') or '-'} | "
            f"needs_review={v.get('ledger_needs_review')} | {v.get('reason', '')[:60]}"
        )
    emit_output(ctx, data, "\n".join(lines))
    return data


def _map_local_review_error(exc: Exception, book_id: str) -> CliError:
    from backend.common.file_lock import BookLockError

    if isinstance(exc, BookLockError):
        return CliError(EXIT_CONFLICT, "book_locked", "书目正在被其他进程操作，请稍后重试")
    if isinstance(exc, LookupError):
        return CliError(EXIT_NOT_FOUND, "ledger_not_found", f"书目 {book_id} 的伏笔账本不存在")
    if isinstance(exc, KeyError):
        missing = exc.args[0] if exc.args else ""
        return CliError(EXIT_NOT_FOUND, "item_not_found", f"未找到伏笔条目: {missing}")
    if isinstance(exc, ValueError):
        return CliError(EXIT_USAGE, "invalid_argument", str(exc))
    if isinstance(exc, RuntimeError):
        return CliError(EXIT_ERROR, "save_failed", str(exc))
    return CliError(EXIT_ERROR, "review_failed", str(exc))


def _post_review_via_server(
    server: LiveServer,
    endpoint: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    import httpx

    headers: Dict[str, str] = {}
    if server.token:
        headers["X-Session-Token"] = server.token
    url = f"{server.url.rstrip('/')}{endpoint}"
    try:
        with httpx.Client(timeout=15.0, trust_env=False) as client:
            resp = client.post(url, json=payload, headers=headers)
    except Exception as exc:
        raise CliError(EXIT_ERROR, "server_request_failed", f"请求运行中服务失败 ({url}): {exc}") from exc

    if resp.status_code == 409:
        detail = _extract_http_detail(resp) or "服务端任务运行中或账本被锁定"
        raise CliError(EXIT_CONFLICT, "server_conflict", detail)
    if resp.status_code == 404:
        raise CliError(EXIT_NOT_FOUND, "not_found", _extract_http_detail(resp) or "目标资源不存在")
    if resp.status_code in (400, 422):
        raise CliError(EXIT_USAGE, "invalid_argument", _extract_http_detail(resp) or "请求参数无效")
    if resp.status_code in (401, 403):
        raise CliError(EXIT_ERROR, "auth_failed", _extract_http_detail(resp) or "Session Token 鉴权失败")
    if resp.status_code >= 400:
        raise CliError(EXIT_ERROR, "server_error", _extract_http_detail(resp) or f"服务端返回 HTTP {resp.status_code}")

    data = resp.json()
    if isinstance(data, dict):
        return {**data, "via": "server", "server_url": server.url}
    return {"result": data, "via": "server", "server_url": server.url}


def _extract_http_detail(resp: Any) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except Exception:
        pass
    return ""


def _proxy_or_local_review(
    ctx: CliContext,
    book_id: str,
    book_dir: Path,
    *,
    item_id: Optional[str] = None,
    single_payload: Optional[Dict[str, Any]] = None,
    batch_entries: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    from backend.services.book_artifacts import (
        batch_review_foreshadow_ledger,
        review_foreshadow_ledger,
    )

    server = discover_live_server(ctx)
    if server is not None:
        if server_matches_book_dir(server, book_id, book_dir):
            if single_payload is not None and item_id is not None:
                return _post_review_via_server(
                    server, f"/api/books/{book_id}/ledger/{item_id}/review", single_payload
                )
            if batch_entries is not None:
                return _post_review_via_server(
                    server, f"/api/books/{book_id}/ledger/batch_review", {"items": batch_entries}
                )
        elif not server.from_lock:
            raise CliError(
                EXIT_CONFLICT,
                "server_book_mismatch",
                f"指定服务端 {server.url} 中的书目 {book_id} 与本地目录 {book_dir} 不匹配，拒绝越权写入",
            )

    output_dir = book_dir / "output"
    try:
        if single_payload is not None and item_id is not None:
            res = review_foreshadow_ledger(
                output_dir,
                book_id,
                item_id,
                single_payload["action"],
                resolution=single_payload.get("resolution"),
                resolved_chapter=single_payload.get("resolved_chapter"),
                note=single_payload.get("note"),
            )
            return {**res, "via": "local"}
        assert batch_entries is not None
        res = batch_review_foreshadow_ledger(output_dir, book_id, batch_entries)
        return {**res, "via": "local"}
    except Exception as exc:
        raise _map_local_review_error(exc, book_id) from exc


def handle_ledger_review(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """对单条伏笔条目提交复核覆写（活跃服务代理或本地加锁落盘）。"""
    book_arg, item_id = resolve_two_positionals(ctx, args.arg1, args.arg2, "伏笔 ID")
    action = str(args.action or "").strip()
    if action not in VALID_REVIEW_ACTIONS:
        raise CliError(EXIT_USAGE, "invalid_action", f"不支持的复核动作: {action}")

    resolved_chapter = getattr(args, "resolved_chapter", None)
    if resolved_chapter is not None and resolved_chapter <= 0:
        raise CliError(EXIT_USAGE, "invalid_argument", "resolved-chapter 必须为正整数")

    book_id, book_dir = resolve_book(ctx, book_arg)
    payload: Dict[str, Any] = {
        "action": action,
        "resolution": getattr(args, "resolution", None),
        "resolved_chapter": resolved_chapter,
        "note": getattr(args, "note", None),
    }
    data = _proxy_or_local_review(
        ctx, book_id, book_dir, item_id=item_id, single_payload=payload
    )
    item = data.get("item", {})
    emit_output(
        ctx,
        data,
        f"已复核伏笔 {item_id} -> status={item.get('status')} (via={data.get('via')})",
    )
    return data


def _read_batch_input(file_arg: str) -> Any:
    if file_arg == "-":
        if hasattr(sys.stdin, "buffer"):
            raw_text = sys.stdin.buffer.read().decode("utf-8-sig")
        else:
            raw_text = sys.stdin.read().lstrip("\ufeff")
    else:
        path = Path(file_arg).expanduser()
        if not path.exists():
            raise CliError(EXIT_NOT_FOUND, "input_not_found", f"批量复核输入文件不存在: {path}")
        raw_text = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(raw_text)
    except Exception as exc:
        raise CliError(EXIT_USAGE, "invalid_json", f"批量复核输入不是有效 JSON: {exc}") from exc


def handle_ledger_batch_review(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """从 JSON 文件或标准输入 (`-`) 读取复核列表并原子批量落盘。"""
    book_id, book_dir = resolve_book(ctx, getattr(args, "book", None))
    parsed = _read_batch_input(str(args.file))

    if isinstance(parsed, dict) and "items" in parsed:
        raw_entries = parsed["items"]
    elif isinstance(parsed, list):
        raw_entries = parsed
    else:
        raise CliError(EXIT_USAGE, "invalid_payload", "批量复核 JSON 必须为列表或包含 items 字段的对象")

    if not isinstance(raw_entries, list) or not raw_entries:
        raise CliError(EXIT_USAGE, "empty_payload", "批量复核条目列表不能为空")

    entries: List[Dict[str, Any]] = []
    for idx, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise CliError(EXIT_USAGE, "invalid_payload", f"第 {idx + 1} 项必须为 JSON 对象")
        item_id = str(entry.get("item_id") or entry.get("id") or "").strip()
        action = str(entry.get("action") or "").strip()
        if not item_id:
            raise CliError(EXIT_USAGE, "invalid_payload", f"第 {idx + 1} 项缺少 item_id")
        if action not in VALID_REVIEW_ACTIONS:
            raise CliError(EXIT_USAGE, "invalid_action", f"第 {idx + 1} 项动作无效: {action}")
        entries.append({
            "item_id": item_id,
            "action": action,
            "resolution": entry.get("resolution"),
            "resolved_chapter": entry.get("resolved_chapter"),
            "note": entry.get("note"),
        })

    data = _proxy_or_local_review(ctx, book_id, book_dir, batch_entries=entries)
    emit_output(
        ctx,
        data,
        f"已原子批量复核 {data.get('updated_count', len(entries))} 条伏笔 (book={book_id}, via={data.get('via')})",
    )
    return data
