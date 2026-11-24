"""CLI 流水线执行与配置诊断命令集：split / analyze / summary / recheck / config。"""

from __future__ import annotations

import argparse
import asyncio
import copy
from dataclasses import asdict
import json
from pathlib import Path
import signal
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from .runtime import (
    EXIT_BREAKER,
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    CliContext,
    CliError,
    discover_live_server,
    emit_output,
    emit_progress_jsonl,
    ensure_server_idle_for_write,
    load_cli_config,
    mask_url_secrets,
    resolve_book,
    resolve_workspace_dir,
)


def _validate_book_name(raw_name: str, workspace_dir: Path) -> Path:
    """校验书名无路径穿越、保留分段或驱动器符号，并限定落在 workspace_dir 直接子目录。"""
    name = (raw_name or "").strip()
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or ":" in name
        or "\x00" in name
    ):
        raise CliError(EXIT_USAGE, "invalid_book_name", f"非法书名（禁止包含路径分隔符或保留段）: {raw_name!r}")

    ws_resolved = workspace_dir.resolve()
    target = (ws_resolved / name).resolve()
    try:
        rel = target.relative_to(ws_resolved)
    except ValueError as exc:
        raise CliError(EXIT_USAGE, "invalid_book_name", f"目标目录越出工作区边界: {name}") from exc
    if len(rel.parts) != 1 or rel.parts[0] != name:
        raise CliError(EXIT_USAGE, "invalid_book_name", f"目标目录必须为工作区直接子目录: {name}")
    return target


def _run_cancellable_async(
    coro_factory: Callable[[], Any],
    stop_fn: Callable[[], None],
) -> Any:
    """运行异步流水线并在收到 SIGINT/SIGTERM 时通知协作停止。"""
    state = {"interrupted": False}
    prev_handlers: Dict[int, Any] = {}
    is_main = threading.current_thread() is threading.main_thread()

    def _on_signal(_signum: int, _frame: Any) -> None:
        state["interrupted"] = True
        try:
            stop_fn()
        except Exception:
            pass

    if is_main:
        sigs = [signal.SIGINT]
        if hasattr(signal, "SIGTERM"):
            sigs.append(signal.SIGTERM)
        for sig in sigs:
            try:
                prev_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, _on_signal)
            except Exception:
                pass

    try:
        return asyncio.run(coro_factory())
    except KeyboardInterrupt as exc:
        state["interrupted"] = True
        try:
            stop_fn()
        except Exception:
            pass
        raise CliError(EXIT_INTERRUPTED, "interrupted", "任务已由用户中断") from exc
    finally:
        for sig, prev in prev_handlers.items():
            try:
                signal.signal(sig, prev)
            except Exception:
                pass
        if state["interrupted"]:
            # 若协程因协作退出而非抛 KeyboardInterrupt 返回，留给调用方检查标志
            pass


def handle_split(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """切分整本小说 TXT（支持 --dry-run 预览与工作区写入，防路径穿越）。"""
    from backend.common.file_lock import BookFileLock, BookLockError
    from backend.services.splitter_service import (
        SplitOptions,
        infer_book_name,
        preview_split,
        save_to_workspace,
    )

    src_path = Path(str(args.file)).expanduser().resolve()
    if not src_path.is_file():
        raise CliError(EXIT_NOT_FOUND, "source_not_found", f"输入小说文件不存在: {src_path}")

    cfg, _ = load_cli_config(ctx, require_valid=True)
    ws_override = getattr(args, "workspace", None)
    workspace_dir = (
        Path(ws_override).expanduser().resolve()
        if ws_override
        else resolve_workspace_dir(ctx, cfg)
    )
    book_name = (getattr(args, "name", None) or "").strip() or infer_book_name(src_path)
    book_dir = _validate_book_name(book_name, workspace_dir)

    opts = SplitOptions(
        pattern=str(getattr(args, "pattern", "") or ""),
        mode=str(getattr(args, "mode", "auto") or "auto"),
        use_volume=bool(getattr(args, "use_volume", True)),
        min_words=int(getattr(args, "min_words", 0) or 0),
        merge_tiny=bool(getattr(args, "merge_tiny", False)),
        max_words=int(getattr(args, "max_words", 0) or 0),
        remove_ads=bool(getattr(args, "remove_ads", False)),
        strip_whitespace=bool(getattr(args, "strip_whitespace", True)),
    )

    if getattr(args, "dry_run", False):
        preview_count = max(1, int(getattr(args, "preview_count", 20) or 20))
        res = preview_split(src_path, options=opts, preview_count=preview_count)
        data = {"dry_run": True, "book_name": book_name, **res}
        emit_output(
            ctx,
            data,
            f"[预览] 书名={book_name} | 总章节={res.get('total_chapters', 0)} | 规则={res.get('rule', '')}",
        )
        return data

    ensure_server_idle_for_write(discover_live_server(ctx))
    blocks_dir = book_dir / "blocks"
    if blocks_dir.exists() and any(blocks_dir.glob("*.txt")) and not getattr(args, "force", False):
        raise CliError(
            EXIT_CONFLICT,
            "target_exists",
            f"目标目录已存在切分结果 ({blocks_dir})，请使用 --force 覆盖",
        )

    book_dir.mkdir(parents=True, exist_ok=True)
    try:
        with BookFileLock(book_dir / ".book.lock"):
            res = save_to_workspace(src_path, workspace_dir, book_name, options=opts)
    except BookLockError as exc:
        raise CliError(EXIT_CONFLICT, "book_locked", "目标书目正被其他进程操作，请稍后重试") from exc

    data = {"dry_run": False, **res}
    emit_output(
        ctx,
        data,
        f"切分完成: {book_name} -> {res.get('output_dir', '')} (共 {res.get('saved_count', 0)} 章)",
    )
    return data


def handle_analyze(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """运行章节分析流水线（持书目文件锁，产出 token_stats.json）。"""
    from backend.common.file_lock import BookFileLock, BookLockError
    from backend.core.pipeline import AnalysisPipeline
    from backend.services.analysis_stats import AnalysisStats
    from backend.utils.json_utils import safe_save_json
    from backend.ws_events import PipelineStatus

    book_id, book_dir = resolve_book(ctx, getattr(args, "book", None))
    blocks_dir = book_dir / "blocks"
    if not blocks_dir.is_dir():
        raise CliError(EXIT_NOT_FOUND, "blocks_not_found", f"书目 {book_id} 缺少 blocks/ 正文目录")
    output_dir = book_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg, _ = load_cli_config(ctx, require_valid=True)
    run_cfg = copy.deepcopy(cfg)
    run_cfg.working_directory = str(book_dir)
    if getattr(args, "concurrency", None):
        run_cfg.analysis.concurrency = max(1, int(args.concurrency))
    if getattr(args, "block_size", None):
        run_cfg.analysis.block_size = max(1, int(args.block_size))
    if getattr(args, "model", None):
        run_cfg.api.model = str(args.model).strip()

    start_ch = max(1, int(getattr(args, "start", None) or 1))
    end_ch_raw = getattr(args, "end", None)
    end_ch = int(end_ch_raw) if end_ch_raw is not None else None
    if end_ch is not None and end_ch < start_ch:
        raise CliError(EXIT_USAGE, "invalid_range", "end 不能小于 start")

    ensure_server_idle_for_write(discover_live_server(ctx))
    stats = AnalysisStats()
    stats.reset()
    baseline = stats.baseline()
    t_start = time.time()

    async def _on_progress(payload: Dict[str, Any]) -> None:
        status = payload.get("status", "")
        if status in (PipelineStatus.DONE, PipelineStatus.FAILED, PipelineStatus.SKIPPED):
            stats.record_chapter(payload)
        if ctx.progress_jsonl:
            compact = {k: v for k, v in payload.items() if k != "result"}
            emit_progress_jsonl({"event": "progress", **compact})

    async def _on_token_stats(payload: Dict[str, Any]) -> None:
        stats.accumulate_token_stats(payload)
        if ctx.progress_jsonl:
            emit_progress_jsonl({"event": "token_stats", **payload})

    pipeline = AnalysisPipeline(
        config=run_cfg,
        directory=blocks_dir,
        knowledge_file=output_dir / "knowledge_base.json",
        start_chapter=start_ch,
        end_chapter=end_ch,
        on_progress=_on_progress,
        on_token_stats=_on_token_stats,
    )

    try:
        with BookFileLock(book_dir / ".book.lock"):
            result = _run_cancellable_async(pipeline.run, pipeline.stop)
    except BookLockError as exc:
        raise CliError(EXIT_CONFLICT, "book_locked", "书目正被其他进程操作，请稍后重试") from exc

    t_end = time.time()
    consumption = stats.book_consumption(
        SimpleNamespace(name=book_id, start_time=t_start, end_time=t_end),
        baseline,
    )
    if stats.chapter_stats or stats.token_stats:
        safe_save_json(consumption, output_dir / "token_stats.json", ensure_ascii=False)

    if isinstance(result, dict) and result.get("account_tripped"):
        reason = str(result.get("account_trip_reason") or "账户级熔断已触发")
        raise CliError(EXIT_BREAKER, "account_breaker_tripped", reason, details={"result": result})
    if isinstance(result, dict) and result.get("stopped"):
        raise CliError(EXIT_INTERRUPTED, "interrupted", "分析任务已停止", details={"result": result})

    data = {
        "book_id": book_id,
        "result": result,
        "token_stats": consumption,
    }
    analyzed = result.get("total_analyzed", 0) if isinstance(result, dict) else 0
    failed = len(result.get("failed_chapters", [])) if isinstance(result, dict) else 0
    emit_output(ctx, data, f"分析完成: book={book_id}, 成功={analyzed} 块, 失败={failed} 块")
    return data


def handle_summary(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """运行全书最终总结三阶段流水线并生成报告与伏笔账本。"""
    from backend.common.file_lock import BookFileLock, BookLockError
    from backend.services.book_artifacts import count_results
    from backend.services.final_summary.runner import FinalSummaryRunner
    from backend.utils.json_utils import safe_save_json

    book_id, book_dir = resolve_book(ctx, getattr(args, "book", None))
    output_dir = book_dir / "output"
    if count_results(output_dir)[0] <= 0:
        raise CliError(EXIT_NOT_FOUND, "no_analysis_results", f"书目 {book_id} 尚无章节分析结果")

    cfg, _ = load_cli_config(ctx, require_valid=True)
    run_cfg = copy.deepcopy(cfg)
    run_cfg.working_directory = str(book_dir)
    if getattr(args, "concurrency", None):
        run_cfg.analysis.concurrency = max(1, int(args.concurrency))
    if getattr(args, "batch_size", None):
        run_cfg.analysis.summary_batch_size = max(1, int(args.batch_size))
    if getattr(args, "recheck_version", None):
        run_cfg.analysis.recheck_version = str(args.recheck_version)
    if getattr(args, "model", None):
        run_cfg.api.summary_model = str(args.model).strip()

    start_ch = max(1, int(getattr(args, "start", None) or 1))
    end_ch = int(getattr(args, "end", None) or 999999)
    if end_ch < start_ch:
        raise CliError(EXIT_USAGE, "invalid_range", "end 不能小于 start")

    ensure_server_idle_for_write(discover_live_server(ctx))
    t_start = time.time()

    def _on_progress(payload: Dict[str, Any]) -> None:
        if ctx.progress_jsonl:
            emit_progress_jsonl({"event": "summary_progress", **payload})

    def _on_token_stats(payload: Dict[str, Any]) -> None:
        if ctx.progress_jsonl:
            emit_progress_jsonl({"event": "token_stats", **payload})

    runner = FinalSummaryRunner(
        config=run_cfg,
        output_dir=output_dir,
        start_chapter=start_ch,
        end_chapter=end_ch,
        batch_size=getattr(args, "batch_size", None),
        concurrency=getattr(args, "concurrency", None),
        on_progress=_on_progress,
        on_token_stats=_on_token_stats,
    )

    try:
        with BookFileLock(book_dir / ".book.lock"):
            report = _run_cancellable_async(runner.run, runner.stop)
    except BookLockError as exc:
        raise CliError(EXIT_CONFLICT, "book_locked", "书目正被其他进程操作，请稍后重试") from exc

    t_end = time.time()
    token_stats = runner.get_token_stats()
    if any((v.get("input_tokens", 0) or v.get("output_tokens", 0)) for v in token_stats.values()):
        safe_save_json(
            {
                "type": "summary",
                "book_id": book_id,
                "phase": "summary",
                "started_at": t_start,
                "finished_at": t_end,
                "elapsed": round(t_end - t_start, 2),
                "categories": token_stats,
            },
            output_dir / "summary_token_stats.json",
            ensure_ascii=False,
        )

    breaker_tripped = (
        getattr(runner, "_recheck_breaker_tripped", False) is True
        or getattr(getattr(runner, "_llm", None), "breaker_tripped", False) is True
    )
    if breaker_tripped:
        reason = (
            getattr(runner, "_recheck_breaker_reason", "")
            or getattr(getattr(runner, "_llm", None), "breaker_reason", "")
            or "模型通道熔断已触发"
        )
        raise CliError(EXIT_BREAKER, "breaker_tripped", str(reason))

    if getattr(runner, "_stop_requested", False):
        raise CliError(EXIT_INTERRUPTED, "interrupted", "最终总结任务已停止")

    if not report:
        raise CliError(EXIT_ERROR, "summary_failed", "最终总结未生成有效报告")

    report_path = output_dir / "final_summary_report.md"
    report_path.write_text(report, encoding="utf-8")

    data = {
        "book_id": book_id,
        "report_path": report_path.as_posix(),
        "report_chars": len(report),
        "recheck": getattr(runner, "last_recheck_summary", None),
        "ledger_counts": runner.ledger.get_item_count(),
        "token_stats": token_stats,
    }
    emit_output(
        ctx,
        data,
        f"最终总结完成: book={book_id} -> {report_path.as_posix()} ({len(report)} 字)",
    )
    return data


def _load_existing_volume_summaries(
    runner: Any,
    results: List[dict],
) -> List[str]:
    """从已有断点目录组装卷摘要文本，供独立 V3 复检补充远距上下文。"""
    cp = runner.output_dir / "final_summary_checkpoint"
    if not cp.is_dir() or not results:
        return []
    batch_size = max(1, int(getattr(runner, "batch_size", 30) or 30))
    summaries: List[str] = []
    for idx, i in enumerate(range(0, len(results), batch_size), start=1):
        vf = cp / f"volume_{idx}.md"
        if not vf.is_file():
            continue
        batch = results[i : i + batch_size]
        ch_start = batch[0]["ch"]
        ch_end = batch[-1]["ch"]
        try:
            text = vf.read_text(encoding="utf-8")
            if text.strip():
                summaries.append(f"=== 第{ch_start}-{ch_end}章 ===\n{text}")
        except OSError:
            continue
    return summaries


def handle_recheck(ctx: CliContext, args: argparse.Namespace) -> Dict[str, Any]:
    """独立重跑 Stage 2 伏笔复检（保持 batch_size_snapshot，清陈旧 stage2 断点）。"""
    from backend.common.file_lock import BookFileLock, BookLockError
    from backend.services.final_summary.runner import FinalSummaryRunner
    from backend.utils.foreshadow_ledger import ForeshadowLedger
    from backend.utils.json_utils import safe_load_json

    book_id, book_dir = resolve_book(ctx, getattr(args, "book", None))
    output_dir = book_dir / "output"
    ledger_path = output_dir / "foreshadow_ledger.json"
    if not ledger_path.exists():
        raise CliError(EXIT_NOT_FOUND, "ledger_not_found", f"书目 {book_id} 的伏笔账本不存在，无法独立复检")

    cfg, _ = load_cli_config(ctx, require_valid=True)
    run_cfg = copy.deepcopy(cfg)
    run_cfg.working_directory = str(book_dir)

    version = str(getattr(args, "version", None) or getattr(run_cfg.analysis, "recheck_version", "v3") or "v3").lower()
    norm_ver = "v2" if version == "v2" else "v3"
    run_cfg.analysis.recheck_version = norm_ver

    if getattr(args, "concurrency", None):
        conc = max(1, int(args.concurrency))
        run_cfg.analysis.recheck_concurrency = conc
        run_cfg.analysis.concurrency = conc
    if getattr(args, "max_calls", None):
        run_cfg.analysis.recheck_max_calls = max(1, int(args.max_calls))
    if getattr(args, "v2_batch_size", None):
        run_cfg.analysis.recheck_v2_batch_size = max(1, int(args.v2_batch_size))
    if getattr(args, "gray_u4", None) is not None:
        run_cfg.analysis.recheck_v3_gray_u4 = bool(args.gray_u4)
    if getattr(args, "mid_single_hop", None) is not None:
        run_cfg.analysis.recheck_v3_mid_single_hop = bool(args.mid_single_hop)
    if getattr(args, "model", None):
        run_cfg.analysis.recheck_model = str(args.model).strip()

    ensure_server_idle_for_write(discover_live_server(ctx))

    existing_ledger = ForeshadowLedger.load(ledger_path)
    orig_snapshot = existing_ledger.batch_size_snapshot
    human_overrides = {
        it.id: it
        for it in existing_ledger.items
        if getattr(it, "source_type", "") == "human_override"
    }

    def _on_progress(payload: Dict[str, Any]) -> None:
        if ctx.progress_jsonl:
            emit_progress_jsonl({"event": "recheck_progress", **payload})

    runner = FinalSummaryRunner(
        config=run_cfg,
        output_dir=output_dir,
        start_chapter=1,
        end_chapter=999999,
        concurrency=getattr(args, "concurrency", None),
        ledger_path=ledger_path,
        on_progress=_on_progress,
    )
    runner.ledger.batch_size_snapshot = orig_snapshot

    if getattr(args, "from_baseline", False):
        baseline_path = output_dir / "final_summary_checkpoint" / "stage1_ledger_baseline.json"
        baseline = safe_load_json(baseline_path) if baseline_path.exists() else None
        if not isinstance(baseline, dict) or not isinstance(baseline.get("ledger"), dict):
            raise CliError(EXIT_NOT_FOUND, "baseline_not_found", f"未找到有效的 Stage 1 基线快照: {baseline_path}")
        restored = ForeshadowLedger.from_dict(baseline["ledger"])
        if not restored.batch_size_snapshot:
            restored.batch_size_snapshot = orig_snapshot
        if human_overrides:
            id_map = {it.id: idx for idx, it in enumerate(restored.items)}
            for lid, hi in human_overrides.items():
                if lid in id_map:
                    restored.items[id_map[lid]] = hi
                else:
                    restored.items.append(hi)
        runner.ledger = restored

    dormant_reactivated_ids: set[str] = set()
    if getattr(args, "include_dormant", False):
        for it in runner.ledger.items:
            if it.status == "dormant" and getattr(it, "source_type", "") != "human_override":
                dormant_reactivated_ids.add(it.id)
                it.status = "active"

    async def _run_stage2() -> Any:
        results = await asyncio.to_thread(runner._load_results)
        if norm_ver == "v2":
            return await runner._run_recheck_v2(results)
        volume_summaries = await asyncio.to_thread(_load_existing_volume_summaries, runner, results)
        return await runner._run_recheck_v3(results, volume_summaries or None)

    try:
        with BookFileLock(book_dir / ".book.lock"):
            outcome = _run_cancellable_async(_run_stage2, runner.stop)
    except BookLockError as exc:
        raise CliError(EXIT_CONFLICT, "book_locked", "书目正被其他进程操作，请稍后重试") from exc

    breaker_tripped = (
        getattr(runner, "_recheck_breaker_tripped", False) is True
        or getattr(getattr(runner, "_llm", None), "breaker_tripped", False) is True
    )
    if breaker_tripped:
        reason = getattr(runner, "_recheck_breaker_reason", "") or "复检通道熔断已触发"
        raise CliError(EXIT_BREAKER, "breaker_tripped", str(reason))

    if getattr(runner, "_stop_requested", False):
        raise CliError(EXIT_INTERRUPTED, "interrupted", "独立伏笔复检已停止")

    if outcome is None:
        summary_info = getattr(runner, "last_recheck_summary", None) or {}
        reason = str(summary_info.get("status_reason") or "伏笔复检未执行")
        raise CliError(EXIT_ERROR, "recheck_not_run", reason, details={"recheck": summary_info})

    # 若以 --include-dormant 唤醒，对未解决且无需人工介入的条目恢复 dormant
    if dormant_reactivated_ids:
        restored_any = False
        for it in runner.ledger.items:
            if it.id in dormant_reactivated_ids and it.status == "active" and not it.needs_review:
                it.status = "dormant"
                restored_any = True
        if restored_any:
            runner.ledger.save(runner.ledger_path)

    # 清除陈旧 Stage 2 断点，避免后续 summary 运行覆盖独立复检结果
    stage2_ckpt = output_dir / "final_summary_checkpoint" / "stage2_recheck_state.json"
    try:
        stage2_ckpt.unlink(missing_ok=True)
    except OSError:
        pass

    summary_info = getattr(runner, "last_recheck_summary", None) or outcome.summary()
    data = {
        "book_id": book_id,
        "engine": norm_ver,
        "recheck": summary_info,
        "counts": runner.ledger.get_item_count(),
        "lifecycle_bars": runner.ledger.lifecycle_bars(),
        "token_stats": runner.get_token_stats(),
    }
    emit_output(
        ctx,
        data,
        f"独立复检完成 ({norm_ver.upper()}): book={book_id} status={summary_info.get('status', '')} "
        f"calls={summary_info.get('calls', 0)}",
    )
    return data


def handle_config_show(ctx: CliContext, _args: argparse.Namespace) -> Dict[str, Any]:
    """输出脱敏后的当前生效配置（永不输出明文密钥或 URL 凭据）。"""
    cfg, _ = load_cli_config(ctx, require_valid=True)
    raw = asdict(cfg)

    api_sec = raw.get("api", {})
    raw_api_key = str(api_sec.get("api_key") or "")
    api_sec["api_key_set"] = bool(raw_api_key.strip())
    api_sec["api_key"] = "***" if raw_api_key.strip() else ""
    api_sec["base_url"] = mask_url_secrets(str(api_sec.get("base_url") or ""))

    ana_sec = raw.get("analysis", {})
    raw_rc_key = str(ana_sec.get("recheck_api_key") or "")
    ana_sec["recheck_api_key_set"] = bool(raw_rc_key.strip())
    ana_sec["recheck_api_key"] = "***" if raw_rc_key.strip() else ""
    ana_sec["recheck_base_url"] = mask_url_secrets(str(ana_sec.get("recheck_base_url") or ""))

    data = {
        "runtime_dir": ctx.runtime_dir.as_posix(),
        "config_path": (ctx.runtime_dir / "config.json").as_posix(),
        "config": raw,
    }
    human = (
        f"运行目录: {ctx.runtime_dir}\n"
        f"主模型: {api_sec.get('model')} | base_url={api_sec.get('base_url')} | "
        f"api_key_set={api_sec.get('api_key_set')}\n"
        f"复检引擎: {ana_sec.get('recheck_version')} | recheck_model={ana_sec.get('recheck_model') or '(跟随主模型)'} | "
        f"recheck_api_key_set={ana_sec.get('recheck_api_key_set')}"
    )
    emit_output(ctx, data, human)
    return data


def handle_config_health(ctx: CliContext, _args: argparse.Namespace) -> Dict[str, Any]:
    """检查运行目录、配置文件合法性、关键依赖完整性及活体后端状态。"""
    from backend.common.dep_health import check_dependency_health

    _cfg, load_failed = load_cli_config(ctx, require_valid=False)
    deps = check_dependency_health()

    live_info: Dict[str, Any] = {"running": False, "url": None, "from_lock": False, "token_present": False}
    try:
        srv = discover_live_server(ctx)
        if srv is not None:
            live_info = {
                "running": True,
                "url": srv.url,
                "from_lock": srv.from_lock,
                "token_present": bool(srv.token),
            }
    except CliError as exc:
        live_info = {
            "running": False,
            "url": ctx.server_url,
            "from_lock": False,
            "token_present": False,
            "error": exc.message,
        }

    cfg_path = ctx.runtime_dir / "config.json"
    data = {
        "runtime_dir": ctx.runtime_dir.as_posix(),
        "config_exists": cfg_path.exists(),
        "config_valid": not load_failed,
        "dependencies": deps,
        "live_server": live_info,
    }
    human = (
        f"运行目录: {ctx.runtime_dir}\n"
        f"配置有效: {not load_failed} | 依赖状态: {deps.get('status')} | "
        f"活体后端: {live_info.get('url') or '未运行'}"
    )
    emit_output(ctx, data, human)
    return data
