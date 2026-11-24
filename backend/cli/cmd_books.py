"""CLI 书目只读查询与导出命令集：list / status / chapter / search / characters / report / export。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional

from .runtime import (
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    CliContext,
    CliError,
    discover_books,
    emit_output,
    resolve_book,
    resolve_two_positionals,
)


def _read_json_if_exists(path: Path) -> Optional[Any]:
    import json

    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_positive_int(raw: str, label: str) -> int:
    try:
        val = int(raw)
    except ValueError as exc:
        raise CliError(EXIT_USAGE, "invalid_argument", f"{label}必须为正整数: {raw}") from exc
    if val <= 0:
        raise CliError(EXIT_USAGE, "invalid_argument", f"{label}必须为正整数: {raw}")
    return val


def handle_books_list(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """列出工作区与运行目录中已发现的全部书目及制品概览。"""
    from backend.services.book_artifacts import book_info

    books_map = discover_books(ctx)
    rows = [book_info(bid, bdir) for bid, bdir in sorted(books_map.items(), key=lambda kv: kv[0])]
    data = {
        "runtime_dir": ctx.runtime_dir.as_posix(),
        "total": len(rows),
        "books": rows,
    }
    if not rows:
        human = f"未发现任何书目（运行目录: {ctx.runtime_dir}）"
    else:
        lines = [f"共发现 {len(rows)} 本书目（运行目录: {ctx.runtime_dir}）："]
        for row in rows:
            flags = [
                flag
                for cond, flag in (
                    (row.get("has_report"), "报告"),
                    (row.get("has_ledger"), "账本"),
                    (row.get("has_aggregated"), "聚合"),
                    (row.get("has_atlas"), "图谱"),
                )
                if cond
            ]
            flag_str = ",".join(flags) if flags else "无产物"
            lines.append(
                f"- {row['id']} | 分章 {row.get('total_blocks', 0)} | "
                f"已分析块 {row.get('result_count', 0)} | {flag_str}"
            )
        human = "\n".join(lines)
    emit_output(ctx, data, human)
    return data


def handle_books_status(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """查看单本书目的分章、分析进度、伏笔账本、复检、风格画像与 Token 统计。"""
    from backend.core.file_processor import FileProcessor
    from backend.services.book_artifacts import (
        book_info,
        load_recheck_raw,
        load_style_profile_from_output,
    )
    from backend.utils.foreshadow_ledger import ForeshadowLedger

    book_id, book_dir = resolve_book(ctx, getattr(args, "book", None))
    output_dir = book_dir / "output"
    blocks_dir = book_dir / "blocks"
    info = book_info(book_id, book_dir)

    chapter_nums: List[int] = []
    if blocks_dir.is_dir():
        chapter_nums = FileProcessor(blocks_dir).scan_chapters()

    ledger_summary: Optional[Dict[str, Any]] = None
    ledger_path = output_dir / "foreshadow_ledger.json"
    if ledger_path.exists():
        ledger = ForeshadowLedger.load(ledger_path)
        ledger_summary = {
            "counts": ledger.get_item_count(),
            "lifecycle_bars": ledger.lifecycle_bars(),
            "batch_size_snapshot": ledger.batch_size_snapshot,
            "total_chapters": ledger.total_chapters,
        }

    recheck_raw = load_recheck_raw(output_dir)
    recheck_summary: Optional[Dict[str, Any]] = None
    if recheck_raw is not None:
        recheck_summary = {
            "status": str(recheck_raw.get("status") or ""),
            "status_reason": str(recheck_raw.get("status_reason") or ""),
            "candidates_total": int(recheck_raw.get("candidates_total") or 0),
            "generated_at": str(recheck_raw.get("generated_at") or ""),
            "coverage": recheck_raw.get("coverage") if isinstance(recheck_raw.get("coverage"), dict) else {},
        }

    style = load_style_profile_from_output(output_dir, book_id)
    token_stats = _read_json_if_exists(output_dir / "token_stats.json")
    summary_token_stats = _read_json_if_exists(output_dir / "summary_token_stats.json")

    data = {
        "book": info,
        "chapters_available": len(chapter_nums),
        "chapter_range": [chapter_nums[0], chapter_nums[-1]] if chapter_nums else None,
        "has_recheck_verdicts": recheck_raw is not None,
        "has_style_profile": bool(style.get("exists")),
        "ledger": ledger_summary,
        "recheck": recheck_summary,
        "token_stats": token_stats,
        "summary_token_stats": summary_token_stats,
    }
    lines = [
        f"书目: {book_id} ({book_dir.as_posix()})",
        f"物理章节: {len(chapter_nums)} | 已分析块: {info.get('result_count', 0)}",
        f"产物: 报告={'有' if info.get('has_report') else '无'} | "
        f"账本={'有' if ledger_summary else '无'} | "
        f"复检={'有' if recheck_raw else '无'} | "
        f"风格={'有' if style.get('exists') else '无'}",
    ]
    if ledger_summary:
        c = ledger_summary["counts"]
        lines.append(
            f"伏笔统计: 总计 {c.get('total', 0)} "
            f"(活跃 {c.get('active', 0)} / 已回收 {c.get('resolved', 0)} / 休眠 {c.get('dormant', 0)})"
        )
    emit_output(ctx, data, "\n".join(lines))
    return data


def handle_books_chapter(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """按物理章节号读取原文窗口与块级分析结果（支持 block_size>1 与 0001.txt）。"""
    from backend.core.file_processor import FileProcessor
    from backend.services.book_artifacts import load_chapter_result_for_chapter

    book_arg, chap_raw = resolve_two_positionals(ctx, args.arg1, args.arg2, "章节号")
    chapter_num = _parse_positive_int(chap_raw, "章节号")
    offset = int(getattr(args, "offset", 0) or 0)
    limit = int(getattr(args, "limit", 6000) or 6000)
    if offset < 0 or limit <= 0:
        raise CliError(EXIT_USAGE, "invalid_argument", "offset 不能为负，且 limit 必须为正整数")

    book_id, book_dir = resolve_book(ctx, book_arg)
    view = str(getattr(args, "view", "both") or "both")
    blocks_dir = book_dir / "blocks"
    output_dir = book_dir / "output"

    text_payload: Optional[Dict[str, Any]] = None
    if view in ("text", "both") and blocks_dir.is_dir():
        fp = FileProcessor(blocks_dir)
        available = fp.scan_chapters()
        if chapter_num in available:
            raw_text = fp.read_chapter(chapter_num)
            if raw_text is not None:
                sliced = raw_text[offset : offset + limit]
                text_payload = {
                    "total_chars": len(raw_text),
                    "offset": offset,
                    "limit": limit,
                    "truncated": (offset + limit) < len(raw_text),
                    "content": sliced,
                }

    analysis_payload: Optional[Dict[str, Any]] = None
    if view in ("analysis", "both"):
        analysis_payload = load_chapter_result_for_chapter(output_dir, chapter_num)

    if view == "text" and text_payload is None:
        raise CliError(EXIT_NOT_FOUND, "chapter_not_found", f"书目 {book_id} 中未找到第 {chapter_num} 章原文")
    if view == "analysis" and analysis_payload is None:
        raise CliError(EXIT_NOT_FOUND, "analysis_not_found", f"书目 {book_id} 中未找到第 {chapter_num} 章分析结果")
    if view == "both" and text_payload is None and analysis_payload is None:
        raise CliError(EXIT_NOT_FOUND, "chapter_not_found", f"书目 {book_id} 中第 {chapter_num} 章原文与分析结果均不存在")

    data = {
        "book_id": book_id,
        "chapter": chapter_num,
        "view": view,
        "text": text_payload,
        "analysis": analysis_payload,
    }
    lines = [f"书目 {book_id} 第 {chapter_num} 章 (view={view})"]
    if text_payload is not None:
        lines.append(
            f"[原文 {text_payload['offset']}..{text_payload['offset'] + len(text_payload['content'])} "
            f"/ {text_payload['total_chars']} 字]\n{text_payload['content']}"
        )
    if analysis_payload is not None:
        lines.append(
            f"[分析块 chapter={analysis_payload.get('result_block_chapter')}, "
            f"source_chapters={analysis_payload.get('source_chapters')}]"
        )
    emit_output(ctx, data, "\n".join(lines))
    return data


def handle_books_search(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """在书目物理分章中执行字面检索（先按范围过滤章号，再逐章读取）。"""
    from backend.core.file_processor import FileProcessor

    book_arg, query = resolve_two_positionals(ctx, args.arg1, args.arg2, "检索词")
    if not query.strip():
        raise CliError(EXIT_USAGE, "empty_query", "检索词不能为空")

    from_ch = getattr(args, "from_chapter", None)
    to_ch = getattr(args, "to_chapter", None)
    if from_ch is not None and to_ch is not None and from_ch > to_ch:
        raise CliError(EXIT_USAGE, "invalid_range", "from-chapter 不能大于 to-chapter")

    max_hits = int(getattr(args, "max_hits", 20) or 20)
    radius = int(getattr(args, "radius", 180) if getattr(args, "radius", None) is not None else 180)
    if max_hits <= 0 or radius < 0:
        raise CliError(EXIT_USAGE, "invalid_argument", "max-hits 必须为正整数，且 radius 不能为负")

    book_id, book_dir = resolve_book(ctx, book_arg)
    blocks_dir = book_dir / "blocks"
    if not blocks_dir.is_dir():
        raise CliError(EXIT_NOT_FOUND, "blocks_not_found", f"书目 {book_id} 缺少 blocks/ 原文目录")

    fp = FileProcessor(blocks_dir)
    all_chapters = fp.scan_chapters()
    if not all_chapters:
        raise CliError(EXIT_NOT_FOUND, "blocks_empty", f"书目 {book_id} 的 blocks/ 目录中没有正文章节")

    target_chapters = [
        ch
        for ch in all_chapters
        if (from_ch is None or ch >= from_ch) and (to_ch is None or ch <= to_ch)
    ]

    hits: List[Dict[str, Any]] = []
    truncated = False
    for ch in target_chapters:
        text = fp.read_chapter(ch)
        if not text:
            continue
        pos = 0
        while True:
            found = text.find(query, pos)
            if found < 0:
                break
            if len(hits) >= max_hits:
                truncated = True
                break
            w_start = max(0, found - radius)
            w_end = min(len(text), found + len(query) + radius)
            hits.append({
                "chapter": ch,
                "offset": found,
                "snippet": text[w_start:w_end],
            })
            pos = found + max(1, len(query))
        if truncated:
            break

    data = {
        "book_id": book_id,
        "query": query,
        "from_chapter": from_ch,
        "to_chapter": to_ch,
        "scanned_chapters": len(target_chapters),
        "total_hits": len(hits),
        "truncated": truncated,
        "hits": hits,
    }
    if not hits:
        human = f"未在书目 {book_id}（扫描 {len(target_chapters)} 章）中找到匹配: {query}"
    else:
        lines = [f"命中 {len(hits)} 条（扫描 {len(target_chapters)} 章，truncated={truncated}）："]
        for h in hits:
            preview = h["snippet"].replace("\n", " ")
            lines.append(f"- 第 {h['chapter']} 章 @{h['offset']}: {preview}")
        human = "\n".join(lines)
    emit_output(ctx, data, human)
    return data


def handle_books_characters(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """读取或按名查看角色卡摘要与详情。"""
    from backend.services.book_artifacts import count_results
    from backend.utils.aggregate_utils import ensure_aggregated_cache
    from backend.utils.character_card_generator import CharacterCardGenerator

    book_id, book_dir = resolve_book(ctx, getattr(args, "book", None))
    output_dir = book_dir / "output"
    if count_results(output_dir)[0] <= 0:
        raise CliError(EXIT_NOT_FOUND, "no_analysis_results", f"书目 {book_id} 尚无章节分析结果")

    ensure_aggregated_cache(output_dir)
    generator = CharacterCardGenerator(output_dir)
    target_name = (getattr(args, "name", None) or "").strip()

    if target_name:
        try:
            card = generator.get_card_data(target_name)
        except KeyError as exc:
            raise CliError(EXIT_NOT_FOUND, "character_not_found", f"未找到角色: {target_name}") from exc
        data: Dict[str, Any] = {"book_id": book_id, "character": card}
        human = f"角色: {card.get('name', target_name)} | 登场章数: {card.get('appearance_count', 0)}"
        emit_output(ctx, data, human)
        return data

    summaries = generator.get_character_summaries()
    if not summaries:
        raise CliError(EXIT_NOT_FOUND, "no_characters", f"书目 {book_id} 未提取到角色信息")

    offset = int(getattr(args, "offset", 0) or 0)
    limit = int(getattr(args, "limit", 0) or 0)
    if offset < 0 or limit < 0:
        raise CliError(EXIT_USAGE, "invalid_argument", "offset 与 limit 不能为负")
    page = summaries[offset : offset + limit] if limit > 0 else summaries[offset:]

    data = {
        "book_id": book_id,
        "total": len(summaries),
        "offset": offset,
        "characters": page,
    }
    lines = [f"书目 {book_id} 角色列表（展示 {len(page)}/{len(summaries)}）："]
    for ch in page:
        lines.append(f"- {ch.get('name', '')} | 出场 {ch.get('appearances', ch.get('appearance_count', 0))} 次")
    emit_output(ctx, data, "\n".join(lines))
    return data


def handle_books_report(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """读取最终总结报告（支持分片分页及可选风格画像）。"""
    from backend.services.book_artifacts import load_style_profile_from_output

    book_id, book_dir = resolve_book(ctx, getattr(args, "book", None))
    output_dir = book_dir / "output"
    report_path = output_dir / "final_summary_report.md"
    if not report_path.exists():
        raise CliError(EXIT_NOT_FOUND, "report_not_found", f"书目 {book_id} 尚未生成最终总结报告")

    offset = int(getattr(args, "offset", 0) or 0)
    limit = int(getattr(args, "limit", 0) or 0)
    if offset < 0 or limit < 0:
        raise CliError(EXIT_USAGE, "invalid_argument", "offset 与 limit 不能为负")

    raw_text = report_path.read_text(encoding="utf-8")
    sliced = raw_text[offset : offset + limit] if limit > 0 else raw_text[offset:]
    truncated = (offset + len(sliced)) < len(raw_text)

    data: Dict[str, Any] = {
        "book_id": book_id,
        "total_chars": len(raw_text),
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
        "report": sliced,
    }
    if getattr(args, "include_style", False):
        data["style_profile"] = load_style_profile_from_output(output_dir, book_id)

    emit_output(ctx, data, sliced)
    return data


def handle_books_export(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """按需更新聚合缓存并导出分析结果到 Excel（.xlsx）。"""
    from backend.services.book_artifacts import count_results
    from backend.utils.aggregate_utils import ensure_aggregated_cache
    from backend.utils.excel_export import export_aggregated_to_excel

    book_id, book_dir = resolve_book(ctx, getattr(args, "book", None))
    output_dir = book_dir / "output"
    res_count = count_results(output_dir)[0]
    if res_count <= 0:
        raise CliError(EXIT_NOT_FOUND, "no_analysis_results", f"书目 {book_id} 尚无章节分析结果，无法导出")

    raw_out = getattr(args, "output", None)
    target_path = Path(raw_out).expanduser().resolve() if raw_out else (output_dir / f"{book_id}_分析报告.xlsx")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    aggregated_dir = ensure_aggregated_cache(output_dir)
    exported = export_aggregated_to_excel(aggregated_dir, target_path)

    data = {
        "book_id": book_id,
        "chapters_count": res_count,
        "output_path": exported.as_posix(),
    }
    emit_output(ctx, data, f"已导出 Excel: {exported.as_posix()}（共 {res_count} 个分析块）")
    return data
