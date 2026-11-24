"""CLI 运行时基础：退出码契约、I/O 编码与日志流隔离、凭证脱敏、书目解析与活体服务发现。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from backend.config.settings import AppConfig, ConfigManager
from backend.services.book_artifacts import is_book_dir

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_BREAKER = 5
EXIT_INTERRUPTED = 130

ENV_SESSION_TOKEN = "NOVEL_ANALYZER_SESSION_TOKEN"

_SECRET_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}"), "sk-ant-***"),
    (re.compile(r"sk-[A-Za-z0-9\-_]{16,}"), "sk-***"),
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_.~+/=]{8,}"), r"\1***"),
    (re.compile(r"(?i)(x-(?:api|session)-token\s*[:=]\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(x-api-key\s*[:=]\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)([?&](?:api_?key|key|token)=)[^&\s\"']+"), r"\1***"),
    (re.compile(r"(?i)(https?://)([^/@\s]+:[^/@\s]+@)"), r"\1***:***@"),
)


class CliError(Exception):
    """带结构化错误类别与进程退出码的 CLI 异常。"""

    def __init__(
        self,
        exit_code: int,
        error_type: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = int(exit_code)
        self.error_type = error_type
        self.message = message
        self.details = details or {}


class CliArgumentParser(argparse.ArgumentParser):
    """将参数解析失败转为 CliError(EXIT_USAGE)，确保 --json 下仍输出纯 JSON 错误。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise CliError(EXIT_USAGE, "usage_error", message)


@dataclass
class CliContext:
    runtime_dir: Path
    explicit_book_dir: Optional[Path] = None
    server_url: Optional[str] = None
    token: Optional[str] = None
    offline: bool = False
    as_json: bool = False
    progress_jsonl: bool = False
    verbose: bool = False
    quiet: bool = False


@dataclass
class LiveServer:
    url: str
    token: Optional[str]
    from_lock: bool = False


def configure_cli_io(verbose: bool = False, quiet: bool = False) -> None:
    """保证控制台 UTF-8 输出且日志不污染 stdout；不破坏 pytest capsys/caplog 捕获器。"""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        enc = str(getattr(stream, "encoding", "") or "").lower().replace("_", "-")
        if callable(reconf) and enc != "utf-8":
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass

    root = logging.getLogger()
    loggers: List[logging.Logger] = [root]
    for obj in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(obj, logging.Logger):
            loggers.append(obj)
    for lgr in loggers:
        for handler in list(lgr.handlers):
            # 保留 pytest LogCaptureHandler；仅将指向 stdout 的 StreamHandler 转往 stderr
            if type(handler).__name__ == "LogCaptureHandler":
                continue
            if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) is sys.stdout:
                set_stream = getattr(handler, "setStream", None)
                if callable(set_stream):
                    set_stream(sys.stderr)

    if quiet:
        root.setLevel(logging.ERROR)
    elif verbose:
        root.setLevel(logging.DEBUG)

    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def redact_secrets(text: str, extra_secrets: Sequence[str] = ()) -> str:
    """脱敏 API Key、Session Token 及 URL 内嵌凭据。"""
    if not text:
        return text
    out = text
    for sec in extra_secrets:
        s = (sec or "").strip()
        if len(s) >= 6:
            out = out.replace(s, "***")
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def mask_url_secrets(url: str) -> str:
    """隐藏 URL 中的用户名/密码及常见密钥查询参数。"""
    if not url:
        return url
    return redact_secrets(url)


def load_cli_config(ctx: CliContext, *, require_valid: bool = True) -> Tuple[AppConfig, bool]:
    """显式按 ``ctx.runtime_dir / config.json`` 加载配置，不依赖进程全局 cwd/env。"""
    cfg_path = ctx.runtime_dir / "config.json"
    cm = ConfigManager(cfg_path)
    cfg = cm.load()
    load_failed = bool(getattr(cm, "_load_failed", False))
    if require_valid and load_failed:
        raise CliError(
            EXIT_USAGE,
            "invalid_config",
            f"配置文件解析失败（{cfg_path}），请检查 JSON 语法或编码 BOM",
        )
    return cfg, load_failed


def resolve_workspace_dir(ctx: CliContext, cfg: AppConfig) -> Path:
    ws = Path(cfg.workspace_dir)
    if ws.is_absolute():
        return ws
    return ctx.runtime_dir / ws


def _resolve_runtime_rel_path(ctx: CliContext, raw_path: str) -> Path:
    p = Path(raw_path)
    return p if p.is_absolute() else (ctx.runtime_dir / p)


def _read_queue_items_raw(ctx: CliContext) -> List[Tuple[str, Path]]:
    qfile = ctx.runtime_dir / "queue_state.json"
    if not qfile.exists():
        return []
    try:
        data = json.loads(qfile.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    items_out: List[Tuple[str, Path]] = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        ws_raw = str(raw.get("workspace_dir") or "").strip()
        if not ws_raw:
            blocks_raw = str(raw.get("blocks_dir") or "").strip()
            if blocks_raw:
                ws_raw = str(_resolve_runtime_rel_path(ctx, blocks_raw).parent)
        if name and ws_raw:
            items_out.append((name, _resolve_runtime_rel_path(ctx, ws_raw)))
    return items_out


def discover_books(ctx: CliContext) -> Dict[str, Path]:
    """按运行目录发现书目，目录优先级与 ``book_service._candidate_roots`` 一致。"""
    cfg, _ = load_cli_config(ctx, require_valid=False)
    books: Dict[str, Path] = {}
    queue_items = _read_queue_items_raw(ctx)
    for name, ws_dir in queue_items:
        if ws_dir.exists() and is_book_dir(ws_dir) and name not in books:
            books[name] = ws_dir

    roots: List[Path] = []
    archive_roots: List[Path] = []

    ws = resolve_workspace_dir(ctx, cfg)
    if ws.exists():
        roots.append(ws)
        archive = ws / "分析结果"
        if archive.exists():
            archive_roots.append(archive)

    for _name, ws_dir in queue_items:
        parent = ws_dir.parent
        if parent.exists():
            roots.append(parent)
        archive = parent / "分析结果"
        if archive.exists():
            archive_roots.append(archive)

    if cfg.working_directory:
        wd = Path(cfg.working_directory)
        if not wd.is_absolute():
            wd = ctx.runtime_dir / wd
        if wd.exists():
            roots.append(wd.parent if is_book_dir(wd) else wd)
            archive = (wd.parent if is_book_dir(wd) else wd) / "分析结果"
            if archive.exists():
                archive_roots.append(archive)

    seen_roots: set[str] = set()
    ordered_roots: List[Path] = []
    for r in [*roots, *archive_roots]:
        try:
            key = os.path.normcase(os.path.realpath(str(r)))
        except OSError:
            key = str(r).lower()
        if key not in seen_roots:
            seen_roots.add(key)
            ordered_roots.append(r)

    for root in ordered_roots:
        try:
            for sub in sorted(root.iterdir()):
                if sub.name == "分析结果":
                    continue
                if is_book_dir(sub) and sub.name not in books:
                    books[sub.name] = sub
        except OSError:
            continue
    return books


def resolve_book(ctx: CliContext, book_arg: Optional[str]) -> Tuple[str, Path]:
    """解析目标书目，支持 ``--book-dir``、直接传入书目录路径或按书名检索。"""
    if ctx.explicit_book_dir is not None:
        p = ctx.explicit_book_dir.expanduser().resolve()
        if not is_book_dir(p):
            raise CliError(EXIT_NOT_FOUND, "book_dir_not_found", f"指定的书目目录无效（缺少 blocks/ 或 output/）: {p}")
        return p.name, p

    raw = (book_arg or "").strip()
    if not raw:
        raise CliError(EXIT_USAGE, "missing_book", "必须指定书名或 --book-dir")

    cand = Path(raw).expanduser()
    if cand.exists() and is_book_dir(cand):
        resolved = cand.resolve()
        return resolved.name, resolved

    books = discover_books(ctx)
    hit = books.get(raw)
    if hit is None or not hit.exists():
        raise CliError(EXIT_NOT_FOUND, "book_not_found", f"未找到书目: {raw}")
    return raw, hit.resolve()


def resolve_two_positionals(
    ctx: CliContext,
    arg1: Optional[str],
    arg2: Optional[str],
    second_label: str,
) -> Tuple[Optional[str], str]:
    """消除 ``--book-dir`` 与双位置参数 ``<book> <second>`` 的歧义。"""
    if ctx.explicit_book_dir is not None:
        if arg2 is not None and arg2.strip():
            return arg1, arg2.strip()
        if arg1 is not None and arg1.strip():
            return None, arg1.strip()
        raise CliError(EXIT_USAGE, "missing_argument", f"缺少必需参数: {second_label}")
    if not arg1 or not arg1.strip() or not arg2 or not arg2.strip():
        raise CliError(EXIT_USAGE, "missing_argument", f"需要提供 <book> 与 <{second_label}> 参数")
    return arg1.strip(), arg2.strip()


def _is_loopback_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").strip().lower()
        return host in ("127.0.0.1", "localhost", "::1")
    except Exception:
        return False


def _read_session_token_file(runtime_dir: Path) -> Optional[str]:
    tf = runtime_dir / "session.token"
    if not tf.exists():
        return None
    try:
        tok = tf.read_text(encoding="utf-8").strip()
        return tok or None
    except OSError:
        return None


def _probe_health(url: str) -> Dict[str, Any]:
    import httpx

    base = url.rstrip("/")
    with httpx.Client(timeout=2.0, trust_env=False) as client:
        resp = client.get(f"{base}/api/health")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or data.get("app") != "novel-analyzer-web":
            raise ValueError("目标端点不是 novel-analyzer-web 服务")
        return data


def discover_live_server(ctx: CliContext) -> Optional[LiveServer]:
    """探测运行中的桌面/Web 后端；显式 ``--server`` 不可达时直接报错，不静默回退本地写入。"""
    if ctx.offline:
        return None

    env_token = (os.environ.get(ENV_SESSION_TOKEN) or "").strip() or None
    explicit_tok = (ctx.token or "").strip() or env_token

    if ctx.server_url:
        base = ctx.server_url.strip().rstrip("/")
        try:
            _probe_health(base)
        except Exception as exc:
            raise CliError(
                EXIT_ERROR,
                "server_unreachable",
                f"无法连接指定后端服务 {base}: {exc}",
            ) from exc
        tok = explicit_tok
        if tok is None and _is_loopback_url(base):
            tok = _read_session_token_file(ctx.runtime_dir)
        return LiveServer(url=base, token=tok, from_lock=False)

    lock_file = ctx.runtime_dir / "app.lock"
    if not lock_file.exists():
        return None
    try:
        lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
        port = int(lock_data.get("port") or 0)
    except Exception:
        return None
    if port <= 0:
        return None

    base = f"http://127.0.0.1:{port}"
    try:
        _probe_health(base)
    except Exception:
        return None

    tok = explicit_tok or _read_session_token_file(ctx.runtime_dir)
    return LiveServer(url=base, token=tok, from_lock=True)


def server_matches_book_dir(server: LiveServer, book_id: str, book_dir: Path) -> bool:
    """校验服务端映射的书目路径是否与本地目标 ``book_dir`` 一致，防跨目录误改。"""
    import httpx

    try:
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            resp = client.get(f"{server.url}/api/books")
            resp.raise_for_status()
            rows = resp.json()
        target_canon = os.path.normcase(os.path.realpath(str(book_dir)))
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("id") == book_id and row.get("path"):
                srv_canon = os.path.normcase(os.path.realpath(str(row["path"])))
                return srv_canon == target_canon
    except Exception:
        return False
    return False


def ensure_server_idle_for_write(server: Optional[LiveServer]) -> None:
    """发现活跃服务时校验分析队列与总结均处于空闲态；探测失败按占用处理。"""
    if server is None:
        return
    import httpx

    try:
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            a_resp = client.get(f"{server.url}/api/analysis/status")
            s_resp = client.get(f"{server.url}/api/summary/status")
            a_resp.raise_for_status()
            s_resp.raise_for_status()
            a_data = a_resp.json()
            s_data = s_resp.json()
    except Exception as exc:
        raise CliError(
            EXIT_CONFLICT,
            "server_status_unknown",
            f"检测到运行中的后端服务 {server.url}，但状态探测失败，禁止并发写入: {exc}",
        ) from exc

    if a_data.get("running") or s_data.get("running"):
        raise CliError(
            EXIT_CONFLICT,
            "server_task_running",
            f"运行中的后端服务 ({server.url}) 正在执行分析或总结任务，请等待完成或停止后再写入",
        )


def collect_known_secrets(ctx: CliContext, cfg: Optional[AppConfig] = None) -> List[str]:
    secrets: List[str] = []
    if ctx.token:
        secrets.append(ctx.token)
    env_tok = os.environ.get(ENV_SESSION_TOKEN)
    if env_tok:
        secrets.append(env_tok)
    if cfg is not None:
        if getattr(cfg.api, "api_key", None):
            secrets.append(str(cfg.api.api_key))
        if getattr(cfg.analysis, "recheck_api_key", None):
            secrets.append(str(cfg.analysis.recheck_api_key))
    return secrets


def emit_progress_jsonl(event: Dict[str, Any]) -> None:
    """向 stderr 输出单行紧凑 NDJSON 进度事件，保持 stdout 纯净。"""
    sys.stderr.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def emit_output(ctx: CliContext, data: Any, human_text: str) -> None:
    if ctx.as_json:
        sys.stdout.write(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2) + "\n")
        sys.stdout.flush()
    elif not ctx.quiet:
        sys.stdout.write(human_text.rstrip() + "\n")
        sys.stdout.flush()


def emit_error(ctx: CliContext, err: CliError, extra_secrets: Sequence[str] = ()) -> None:
    safe_msg = redact_secrets(err.message, extra_secrets)
    if ctx.as_json:
        payload = {
            "ok": False,
            "error": {
                "type": err.error_type,
                "message": safe_msg,
                "details": err.details,
            },
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        sys.stdout.flush()
    else:
        sys.stderr.write(f"错误 [{err.error_type}]: {safe_msg}\n")
        sys.stderr.flush()
