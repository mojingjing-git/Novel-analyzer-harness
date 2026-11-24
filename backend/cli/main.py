"""CLI 主入口：统一参数解析（前/后置全局标志透明）、命令分发与错误脱敏。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .cmd_books import (
    handle_books_chapter,
    handle_books_characters,
    handle_books_export,
    handle_books_list,
    handle_books_report,
    handle_books_search,
    handle_books_status,
)
from .cmd_ledger import (
    handle_ledger_batch_review,
    handle_ledger_list,
    handle_ledger_review,
    handle_ledger_show,
    handle_ledger_verdicts,
)
from .cmd_pipeline import (
    handle_analyze,
    handle_config_get,
    handle_config_health,
    handle_config_patch,
    handle_config_presets,
    handle_config_reset,
    handle_config_schema,
    handle_config_set,
    handle_config_show,
    handle_recheck,
    handle_split,
    handle_summary,
)
from .runtime import (
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    AppendOverrideAction,
    CliArgumentParser,
    CliContext,
    CliError,
    MergingNamespace,
    collect_known_secrets,
    configure_cli_io,
    emit_error,
    extract_raw_argv_override_secrets,
    load_cli_config,
)

HandlerFn = Callable[[CliContext, argparse.Namespace], object]


def _make_common_parser() -> CliArgumentParser:
    """构造共享全局参数解析器；全部使用 SUPPRESS 避免子命令默认值覆盖前置全局标志。"""
    common = CliArgumentParser(add_help=False)
    common.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="显式指定 runtime 数据根目录")
    common.add_argument("--book-dir", default=argparse.SUPPRESS, help="显式指定单本书目目录（含 blocks/ 或 output/）")
    common.add_argument("--config", dest="config_file", default=argparse.SUPPRESS, help="显式指定配置文件路径（覆盖 runtime/config.json）")
    common.add_argument(
        "-o", "--set",
        dest="config_overrides",
        action=AppendOverrideAction,
        default=argparse.SUPPRESS,
        metavar="KEY=VALUE",
        help="临时覆盖单个配置项（可重复，支持点路径或唯一短键名）",
    )
    common.add_argument("--server", default=argparse.SUPPRESS, help="指定运行中桌面/Web 后端 URL")
    common.add_argument("--token", default=argparse.SUPPRESS, help="显式传入 X-Session-Token")
    common.add_argument("--offline", action="store_true", default=argparse.SUPPRESS, help="禁止代理运行中服务，强制本地直读/直写")
    common.add_argument("--json", dest="as_json", action="store_true", default=argparse.SUPPRESS, help="在 stdout 输出结构化纯 JSON")
    common.add_argument("--progress-jsonl", action="store_true", default=argparse.SUPPRESS, help="向 stderr 输出单行 NDJSON 实时进度事件")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="提高 stderr 日志输出级别")
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="静默非 JSON 常规输出")
    return common


def _register_books_parser(subparsers: argparse._SubParsersAction, common: CliArgumentParser) -> None:
    p_books = subparsers.add_parser("books", parents=[common], help="书目查询、原文片段与导出")
    b_sub = p_books.add_subparsers(dest="books_cmd", parser_class=CliArgumentParser)

    p_list = b_sub.add_parser("list", parents=[common], help="列出已发现的全部书目")
    p_list.set_defaults(handler=handle_books_list)

    p_status = b_sub.add_parser("status", parents=[common], help="查看单本书目状态与产物统计")
    p_status.add_argument("book", nargs="?", default=None, help="书名或书目录路径（--book-dir 时可省略）")
    p_status.set_defaults(handler=handle_books_status)

    p_chap = b_sub.add_parser("chapter", parents=[common], help="查看单章原文与块级分析结果")
    p_chap.add_argument("arg1", nargs="?", default=None, help="<book>（或 --book-dir 时的 <chapter>）")
    p_chap.add_argument("arg2", nargs="?", default=None, help="<chapter>")
    p_chap.add_argument("--view", choices=("text", "analysis", "both"), default="both", help="返回内容视图")
    p_chap.add_argument("--offset", type=int, default=0, help="正文字符起始偏移")
    p_chap.add_argument("--limit", type=int, default=6000, help="正文字符返回上限")
    p_chap.set_defaults(handler=handle_books_chapter)

    p_search = b_sub.add_parser("search", parents=[common], help="在物理分章原文中执行字面检索")
    p_search.add_argument("arg1", nargs="?", default=None, help="<book>（或 --book-dir 时的 <query>）")
    p_search.add_argument("arg2", nargs="?", default=None, help="<query>")
    p_search.add_argument("--from-chapter", type=int, default=None, help="起始章节号")
    p_search.add_argument("--to-chapter", type=int, default=None, help="结束章节号")
    p_search.add_argument("--max-hits", type=int, default=20, help="最大命中返回条数")
    p_search.add_argument("--radius", type=int, default=180, help="命中前后上下文窗口字符半径")
    p_search.set_defaults(handler=handle_books_search)

    p_chars = b_sub.add_parser("characters", parents=[common], help="查看角色卡列表或单个角色卡详情")
    p_chars.add_argument("book", nargs="?", default=None, help="书名或书目录路径")
    p_chars.add_argument("--name", default=None, help="角色名或别名")
    p_chars.add_argument("--offset", type=int, default=0, help="角色列表起始偏移")
    p_chars.add_argument("--limit", type=int, default=0, help="角色列表条数上限（0 表示不限制）")
    p_chars.set_defaults(handler=handle_books_characters)

    p_report = b_sub.add_parser("report", parents=[common], help="查看最终总结 Markdown 报告")
    p_report.add_argument("book", nargs="?", default=None, help="书名或书目录路径")
    p_report.add_argument("--offset", type=int, default=0, help="报告字符起始偏移")
    p_report.add_argument("--limit", type=int, default=0, help="报告字符上限（0 表示全文）")
    p_report.add_argument("--style", dest="include_style", action="store_true", help="一并附带 style_profile.json")
    p_report.set_defaults(handler=handle_books_report)

    p_export = b_sub.add_parser("export", parents=[common], help="导出聚合分析结果到 Excel (.xlsx)")
    p_export.add_argument("book", nargs="?", default=None, help="书名或书目录路径")
    p_export.add_argument("--output", default=None, help="导出 .xlsx 输出路径")
    p_export.set_defaults(handler=handle_books_export)


def _register_ledger_parser(subparsers: argparse._SubParsersAction, common: CliArgumentParser) -> None:
    p_ledger = subparsers.add_parser("ledger", parents=[common], help="伏笔账本、复检裁决与人工/模型复核")
    l_sub = p_ledger.add_subparsers(dest="ledger_cmd", parser_class=CliArgumentParser)

    p_list = l_sub.add_parser("list", parents=[common], help="筛选并分页列出伏笔条目")
    p_list.add_argument("book", nargs="?", default=None, help="书名或书目录路径")
    p_list.add_argument("--status", choices=("active", "resolved", "dormant"), default=None, help="按状态筛选")
    p_list.add_argument("--needs-review", action=argparse.BooleanOptionalAction, default=None, help="按待确认状态筛选")
    p_list.add_argument("--importance", choices=("高", "中", "低"), default=None, help="按重要度筛选")
    p_list.add_argument("--source-type", default=None, help="按来源类型筛选")
    p_list.add_argument("--sort", choices=("score", "first_seen", "last_seen"), default="score", help="排序方式")
    p_list.add_argument("--offset", type=int, default=0, help="分页起始偏移")
    p_list.add_argument("--limit", type=int, default=50, help="每页条数")
    p_list.set_defaults(handler=handle_ledger_list)

    p_show = l_sub.add_parser("show", parents=[common], help="查看单条伏笔明细、裁决及原文证据窗口")
    p_show.add_argument("arg1", nargs="?", default=None, help="<book>（或 --book-dir 时的 <item_id>）")
    p_show.add_argument("arg2", nargs="?", default=None, help="<item_id>")
    p_show.add_argument("--with-evidence", action="store_true", help="提取埋设锚点与回收证据链的原文窗口")
    p_show.add_argument("--radius", type=int, default=240, help="原文证据窗口前后字符半径")
    p_show.set_defaults(handler=handle_ledger_show)

    p_verdicts = l_sub.add_parser("verdicts", parents=[common], help="查看 V2/V3 复检逐条裁决列表")
    p_verdicts.add_argument("book", nargs="?", default=None, help="书名或书目录路径")
    p_verdicts.add_argument("--tier", default=None, help="按 V3 层级筛选（如 RESOLVED_EXPLICIT / UNRESOLVED_STILL_OPEN）")
    p_verdicts.add_argument("--verdict", default=None, help="按 V2/V3 verdict 字段筛选")
    p_verdicts.add_argument("--needs-review", action=argparse.BooleanOptionalAction, default=None, help="按账本待确认状态筛选")
    p_verdicts.add_argument("--gate-demoted", action=argparse.BooleanOptionalAction, default=None, help="按证据门控降级标记筛选")
    p_verdicts.add_argument("--offset", type=int, default=0, help="分页起始偏移")
    p_verdicts.add_argument("--limit", type=int, default=50, help="每页条数")
    p_verdicts.set_defaults(handler=handle_ledger_verdicts)

    p_review = l_sub.add_parser("review", parents=[common], help="复核覆写单条伏笔状态")
    p_review.add_argument("arg1", nargs="?", default=None, help="<book>（或 --book-dir 时的 <item_id>）")
    p_review.add_argument("arg2", nargs="?", default=None, help="<item_id>")
    p_review.add_argument("--action", required=True, choices=("confirm_resolved", "reject_to_active", "mark_dormant"), help="复核动作")
    p_review.add_argument("--resolution", default=None, help="确认回收时的回收说明")
    p_review.add_argument("--resolved-chapter", type=int, default=None, help="确认回收时的回收章节")
    p_review.add_argument("--note", default=None, help="附加审核备注")
    p_review.set_defaults(handler=handle_ledger_review)

    p_batch = l_sub.add_parser("batch-review", parents=[common], help="从 JSON 文件或 stdin (`-`) 原子批量复核伏笔")
    p_batch.add_argument("book", nargs="?", default=None, help="书名或书目录路径")
    p_batch.add_argument("--file", required=True, help="JSON 文件路径或 `-` 从 stdin 读取")
    p_batch.set_defaults(handler=handle_ledger_batch_review)


def _register_pipeline_and_config_parsers(subparsers: argparse._SubParsersAction, common: CliArgumentParser) -> None:
    p_split = subparsers.add_parser("split", parents=[common], help="切分整本小说 TXT 到工作区")
    p_split.add_argument("file", help="源小说 TXT 文件路径")
    p_split.add_argument("--name", default=None, help="目标书名（默认由文件名/内容自动推断）")
    p_split.add_argument("--workspace", default=None, help="覆盖配置中的 workspace 目录")
    p_split.add_argument("--pattern", default="", help="自定义正则切分规则")
    p_split.add_argument("--mode", choices=("auto", "regex", "length"), default="auto", help="切分模式")
    p_split.add_argument("--use-volume", action=argparse.BooleanOptionalAction, default=True, help="是否识别分卷标题")
    p_split.add_argument("--min-words", type=int, default=0, help="最小字数阈值")
    p_split.add_argument("--merge-tiny", action="store_true", default=False, help="自动合并过短章节")
    p_split.add_argument("--max-words", type=int, default=0, help="单章字数上限（0 表示不拆）")
    p_split.add_argument("--remove-ads", action="store_true", default=False, help="移除常见广告文本")
    p_split.add_argument("--strip-whitespace", action=argparse.BooleanOptionalAction, default=True, help="整理首尾空白行")
    p_split.add_argument("--dry-run", action="store_true", help="仅预览切分结果而不写入磁盘")
    p_split.add_argument("--preview-count", type=int, default=20, help="预览模式下返回的章节数上限")
    p_split.add_argument("--force", action="store_true", help="允许覆盖已存在的 blocks/ 目录")
    p_split.set_defaults(handler=handle_split)

    p_analyze = subparsers.add_parser("analyze", parents=[common], help="运行章节分析流水线")
    p_analyze.add_argument("book", nargs="?", default=None, help="书名或书目录路径")
    p_analyze.add_argument("--start", type=int, default=None, help="起始章节号")
    p_analyze.add_argument("--end", type=int, default=None, help="结束章节号")
    p_analyze.add_argument("--concurrency", type=int, default=None, help="覆盖并发数")
    p_analyze.add_argument("--block-size", type=int, default=None, help="覆盖每次分析合并章数")
    p_analyze.add_argument("--model", default=None, help="覆盖主分析模型")
    p_analyze.set_defaults(handler=handle_analyze)

    p_summary = subparsers.add_parser("summary", parents=[common], help="运行全书最终总结与伏笔审计")
    p_summary.add_argument("book", nargs="?", default=None, help="书名或书目录路径")
    p_summary.add_argument("--start", type=int, default=None, help="起始章节号")
    p_summary.add_argument("--end", type=int, default=None, help="结束章节号（默认 999999）")
    p_summary.add_argument("--batch-size", type=int, default=None, help="覆盖分批总结章数")
    p_summary.add_argument("--concurrency", type=int, default=None, help="覆盖总结并发数")
    p_summary.add_argument("--recheck-version", choices=("v2", "v3"), default=None, help="覆盖 Stage 2 复检引擎版本")
    p_summary.add_argument("--model", default=None, help="覆盖总结模型")
    p_summary.set_defaults(handler=handle_summary)

    p_recheck = subparsers.add_parser("recheck", parents=[common], help="单独重跑 Stage 2 带证据伏笔复检")
    p_recheck.add_argument("book", nargs="?", default=None, help="书名或书目录路径")
    p_recheck.add_argument("--version", choices=("v2", "v3"), default=None, help="复检引擎版本")
    p_recheck.add_argument("--concurrency", type=int, default=None, help="覆盖复检并发数")
    p_recheck.add_argument("--max-calls", type=int, default=None, help="覆盖复检最大 LLM 调用上限")
    p_recheck.add_argument("--v2-batch-size", type=int, default=None, help="覆盖 V2 单批条目数")
    p_recheck.add_argument("--gray-u4", action=argparse.BooleanOptionalAction, default=None, help="是否启用 V3 灰区 U4 终审")
    p_recheck.add_argument("--mid-single-hop", action=argparse.BooleanOptionalAction, default=None, help="是否启用 V3 中置信度单跳加速")
    p_recheck.add_argument("--model", default=None, help="覆盖复检模型")
    p_recheck.add_argument("--from-baseline", action="store_true", help="从 stage1_ledger_baseline.json 重建干净账本并保留人工覆写")
    p_recheck.add_argument("--include-dormant", action="store_true", help="临时纳入 dormant 伏笔参与本轮复检")
    p_recheck.set_defaults(handler=handle_recheck)

    p_cfg = subparsers.add_parser("config", parents=[common], help="配置展示、元模型查询、读写与运行健康检查")
    c_sub = p_cfg.add_subparsers(dest="config_cmd", parser_class=CliArgumentParser)

    p_cshow = c_sub.add_parser("show", parents=[common], help="展示脱敏后的当前配置")
    p_cshow.set_defaults(handler=handle_config_show)

    p_chealth = c_sub.add_parser("health", parents=[common], help="检查依赖完整性、配置及活体服务状态")
    p_chealth.set_defaults(handler=handle_config_health)

    p_cschema = c_sub.add_parser("schema", parents=[common], help="列出全量配置元模型与分组说明")
    p_cschema.add_argument("--group", default=None, help="按分组 ID 过滤")
    p_cschema.add_argument("--search", default=None, help="按路径、标签或说明关键字搜索")
    p_cschema.set_defaults(handler=handle_config_schema)

    p_cget = c_sub.add_parser("get", parents=[common], help="读取单个配置项的生效值与元数据")
    p_cget.add_argument("key", help="配置点路径或唯一短键名")
    p_cget.add_argument("--include-credentials", action="store_true", help="明文显示敏感凭据字段（默认脱敏）")
    p_cget.set_defaults(handler=handle_config_get)

    p_cset = c_sub.add_parser("set", parents=[common], help="持久化设置一个或多个配置项（支持活体服务同步）")
    p_cset.add_argument("assignments", nargs="+", metavar="KEY=VALUE", help="一条或多条 KEY=VALUE 赋值")
    p_cset.set_defaults(handler=handle_config_set)

    p_cpatch = c_sub.add_parser("patch", parents=[common], help="从 JSON 文件或 stdin (`-`) 批量局部更新配置")
    p_cpatch.add_argument("--file", required=True, help="JSON 补丁文件路径或 `-` 从 stdin 读取")
    p_cpatch.set_defaults(handler=handle_config_patch)

    p_creset = c_sub.add_parser("reset", parents=[common], help="将指定键或全部配置重置为默认值")
    p_creset.add_argument("keys", nargs="*", help="要重置的配置键名（与 --all 二选一）")
    p_creset.add_argument("--all", dest="reset_all", action="store_true", help="重置全量配置（默认保留端点/密钥凭据）")
    p_creset.add_argument("--include-credentials", action="store_true", help="配合 --all 时一并重置端点与密钥凭据")
    p_creset.set_defaults(handler=handle_config_reset)

    p_cpresets = c_sub.add_parser("presets", parents=[common], help="列出内置 API 厂商预设")
    p_cpresets.set_defaults(handler=handle_config_presets)


def build_parser() -> CliArgumentParser:
    common = _make_common_parser()
    root = CliArgumentParser(
        prog="python -m backend.cli",
        parents=[common],
        description="Novel Analyzer CLI — 书目检索、伏笔账本复核与分析流水线命令接口",
    )
    sub = root.add_subparsers(dest="command", parser_class=CliArgumentParser)
    _register_books_parser(sub, common)
    _register_ledger_parser(sub, common)
    _register_pipeline_and_config_parsers(sub, common)
    return root


def _default_runtime_dir() -> Path:
    from backend.config.paths import get_runtime_dir

    return get_runtime_dir()


def _ctx_from_namespace(ns: Optional[argparse.Namespace], fallback_json: bool) -> CliContext:
    raw_rt = getattr(ns, "runtime_dir", None) if ns is not None else None
    rt_dir = Path(raw_rt).expanduser().resolve() if raw_rt else _default_runtime_dir().resolve()
    raw_bdir = getattr(ns, "book_dir", None) if ns is not None else None
    bdir = Path(raw_bdir) if raw_bdir else None
    raw_cfg = getattr(ns, "config_file", None) if ns is not None else None
    cfg_file = Path(raw_cfg) if raw_cfg else None
    raw_ovr = getattr(ns, "config_overrides", None) if ns is not None else None
    overrides = tuple(str(x) for x in raw_ovr) if raw_ovr else ()
    return CliContext(
        runtime_dir=rt_dir,
        explicit_book_dir=bdir,
        config_file=cfg_file,
        config_overrides=overrides,
        server_url=getattr(ns, "server", None) if ns is not None else None,
        token=getattr(ns, "token", None) if ns is not None else None,
        offline=bool(getattr(ns, "offline", False)) if ns is not None else False,
        as_json=bool(getattr(ns, "as_json", fallback_json)) if ns is not None else fallback_json,
        progress_jsonl=bool(getattr(ns, "progress_jsonl", False)) if ns is not None else False,
        verbose=bool(getattr(ns, "verbose", False)) if ns is not None else False,
        quiet=bool(getattr(ns, "quiet", False)) if ns is not None else False,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口：保证全局标志前置/后置一致、日志隔离及错误凭据脱敏。"""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    pre_json = "--json" in raw_argv
    parser = build_parser()

    ns: Optional[argparse.Namespace] = None
    ctx = _ctx_from_namespace(None, fallback_json=pre_json)
    configure_cli_io(
        verbose=("--verbose" in raw_argv or "-v" in raw_argv),
        quiet=("--quiet" in raw_argv or "-q" in raw_argv),
    )

    try:
        ns = parser.parse_args(raw_argv, namespace=MergingNamespace())
        ctx = _ctx_from_namespace(ns, fallback_json=pre_json)
        configure_cli_io(verbose=ctx.verbose, quiet=ctx.quiet)

        handler: Optional[HandlerFn] = getattr(ns, "handler", None)
        if handler is None:
            raise CliError(EXIT_USAGE, "missing_command", "请指定完整子命令，使用 --help 查看可用命令")
        handler(ctx, ns)
        return EXIT_OK
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_OK
        return code
    except CliError as err:
        secrets = extract_raw_argv_override_secrets(raw_argv)
        try:
            cfg, _ = load_cli_config(ctx, require_valid=False, apply_overrides=False)
            secrets.extend(collect_known_secrets(ctx, cfg))
        except Exception:
            secrets.extend(collect_known_secrets(ctx, None))
        emit_error(ctx, err, secrets)
        return err.exit_code
    except KeyboardInterrupt:
        emit_error(ctx, CliError(EXIT_INTERRUPTED, "interrupted", "任务已由用户中断"))
        return EXIT_INTERRUPTED
    except Exception as exc:
        secrets = extract_raw_argv_override_secrets(raw_argv)
        try:
            cfg, _ = load_cli_config(ctx, require_valid=False, apply_overrides=False)
            secrets.extend(collect_known_secrets(ctx, cfg))
        except Exception:
            secrets.extend(collect_known_secrets(ctx, None))
        emit_error(ctx, CliError(EXIT_ERROR, "unexpected_error", str(exc) or exc.__class__.__name__), secrets)
        return EXIT_ERROR
