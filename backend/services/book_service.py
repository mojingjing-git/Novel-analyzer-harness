"""
书目服务
维护 书名 -> 书目录 的映射（书目录 = workspace/{书名}/，含 blocks/ 与 output/）。
来源：队列条目、队列条目所在根目录的兄弟目录、"分析结果"归档目录、config.working_directory。
GET /api/books 时刷新；其余接口按 id 惰性解析。
"""

import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any

from .book_artifacts import (
    batch_review_foreshadow_ledger,
    book_info,
    count_results as _count_results,
    is_book_dir as _is_book_dir,
    load_foreshadow_ranking_from_output,
    load_recheck_verdicts_from_output,
    load_style_profile_from_output,
    review_foreshadow_ledger,
)
from .queue_service import get_service

logger = logging.getLogger(__name__)

# 书名 -> 书目录 映射（refresh 时重建；同名先到先得）
_books: Dict[str, Path] = {}

# 手动注册的书目（通过 register_scan_dir 添加），refresh 时保留
_manual_books: Dict[str, Path] = {}

# miss 后的事件循环内同步全量扫描会把网络盘延迟放大成
# 秒级 UI/WS 冻结。策略：
# 冷启动（映射空）保留一次阻塞刷新；此后 miss 仅后台单飞刷新 + 立即返回 None。
_refreshing = False
_refresh_lock = threading.Lock()
_books_lock = threading.RLock()
_ledger_review_locks: Dict[str, threading.Lock] = {}
_ledger_review_locks_guard = threading.Lock()


def _get_ledger_lock(book_id: str) -> threading.Lock:
    """按 book_id 获取串行化伏笔台账人工覆写写入锁。"""
    with _ledger_review_locks_guard:
        lock = _ledger_review_locks.get(book_id)
        if lock is None:
            lock = threading.Lock()
            _ledger_review_locks[book_id] = lock
        return lock


def _candidate_roots() -> List[Path]:
    """收集可能包含书目录的根目录（保证活跃工作区优先于归档目录）"""
    service = get_service()
    roots: List[Path] = []
    archive_roots: List[Path] = []

    # 工作区目录（workspace/）排最前，保证同名书优先命中活跃工作区而非 分析结果/
    ws = service.workspace_path
    if ws.exists():
        roots.append(ws)
        archive = ws / "分析结果"
        if archive.exists():
            archive_roots.append(archive)

    for item in service.queue.items:
        parent = item.workspace_dir.parent
        if parent.exists():
            roots.append(parent)
        archive = parent / "分析结果"
        if archive.exists():
            archive_roots.append(archive)

    wd = service.config_manager.config.working_directory
    if wd:
        wd_path = Path(wd)
        if wd_path.exists():
            # working_directory 本身就是书目录，其父目录作为根
            roots.append(wd_path.parent if _is_book_dir(wd_path) else wd_path)
            archive = wd_path.parent / "分析结果"
            if archive.exists():
                archive_roots.append(archive)

    roots.extend(archive_roots)

    # 去重（保持顺序）
    seen = set()
    unique = []
    for r in roots:
        key = r.resolve().as_posix().lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def refresh_books() -> Dict[str, Path]:
    """重建书目映射"""
    global _books
    with _books_lock:
        books: Dict[str, Path] = {}

        # 队列条目优先（名字即条目名）
        for item in get_service().queue.items:
            if item.workspace_dir.exists() and item.name not in books:
                books[item.name] = item.workspace_dir

        # 各根目录下扫描书目录
        for root in _candidate_roots():
            try:
                for sub in sorted(root.iterdir()):
                    if sub.name == "分析结果":
                        continue
                    if _is_book_dir(sub) and sub.name not in books:
                        books[sub.name] = sub
            except OSError as e:
                logger.warning(f"扫描书目根目录失败 {root}: {e}")

        # 合并手动注册的书目（队列/根目录扫描优先级更高）
        for name, path in _manual_books.items():
            if name not in books and path.exists():
                books[name] = path

        _books = books
        logger.info(f"书目刷新: {len(books)} 本")
        return books


def get_book_path(book_id: str) -> Optional[Path]:
    """按书名解析书目录。

    映射缺失时：冷启动（_books 空）阻塞刷一次保证可用；此后仅触发后台单飞
    刷新并立即返回 None（调用方本次 404，刷新完成后下次查询即命中）。"""
    global _refreshing
    path = _books.get(book_id)
    if path is not None and path.exists():
        return path

    if not _books:
        # 冷启动：一次阻塞刷新（此时通常还没有用户请求在等）
        refresh_books()
    else:
        with _refresh_lock:
            should_spawn = not _refreshing
            _refreshing = True
        if should_spawn:
            def _bg():
                global _refreshing
                try:
                    refresh_books()
                finally:
                    with _refresh_lock:
                        _refreshing = False
            threading.Thread(target=_bg, daemon=True, name="book-refresh").start()

    path = _books.get(book_id)
    if path is not None and path.exists():
        return path
    return None


def get_output_dir(book_id: str) -> Optional[Path]:
    path = get_book_path(book_id)
    if path is None:
        return None
    return path / "output"


def latest_chapter(book_id: str) -> int:
    """返回已分析完成的最大章节号（从 chapter_*_result.json 推断），无结果返回 0"""
    output_dir = get_output_dir(book_id)
    if not output_dir or not output_dir.exists():
        return 0
    return _count_results(output_dir)[2]


def load_style_profile(book_id: str) -> Optional[Dict[str, Any]]:
    """读取总结 Stage 3 落盘的 style_profile.json；无书返回 None，缺失或损坏返回 exists=False。"""
    output_dir = get_output_dir(book_id)
    if output_dir is None:
        return None
    return load_style_profile_from_output(output_dir, book_id)


def load_recheck_verdicts(book_id: str) -> Optional[Dict[str, Any]]:
    """读取落盘的 recheck_verdicts.json（兼容 V3/V2）；无书返回 None，缺失或损坏返回 exists=False。"""
    output_dir = get_output_dir(book_id)
    if output_dir is None:
        return None
    return load_recheck_verdicts_from_output(output_dir, book_id)


def load_foreshadow_ranking(book_id: str) -> Dict[str, Any]:
    """按 book_id 读取伏笔排行；无书抛 LookupError，缺账本抛 FileNotFoundError。"""
    output_dir = get_output_dir(book_id)
    if output_dir is None:
        raise LookupError("book_not_found")
    return load_foreshadow_ranking_from_output(output_dir, book_id)


def review_foreshadow_item(
    book_id: str,
    item_id: str,
    action: str,
    *,
    resolution: Optional[str] = None,
    resolved_chapter: Optional[int] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """人工复核覆写指定书目的伏笔账本条目，按 book_id 加锁并校验总结未运行。"""
    output_dir = get_output_dir(book_id)
    if output_dir is None:
        raise LookupError("book_not_found")
    if not (output_dir / "foreshadow_ledger.json").exists():
        raise LookupError("ledger_not_found")

    lock = _get_ledger_lock(book_id)
    with lock:
        # 延迟导入避免与 summary_service（顶层 import book_service）循环依赖
        from .summary_service import get_summary_service

        summary_svc = get_summary_service()
        if summary_svc.is_running and summary_svc.status().get("book_id") == book_id:
            raise RuntimeError("summary_running")

        return review_foreshadow_ledger(
            output_dir,
            book_id,
            item_id,
            action,
            resolution=resolution,
            resolved_chapter=resolved_chapter,
            note=note,
        )


def batch_review_foreshadow_items(
    book_id: str,
    entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """在单书进程内锁与跨进程文件锁保护下原子批量覆写伏笔条目。"""
    output_dir = get_output_dir(book_id)
    if output_dir is None:
        raise LookupError("book_not_found")
    if not (output_dir / "foreshadow_ledger.json").exists():
        raise LookupError("ledger_not_found")

    lock = _get_ledger_lock(book_id)
    with lock:
        from .summary_service import get_summary_service

        summary_svc = get_summary_service()
        if summary_svc.is_running and summary_svc.status().get("book_id") == book_id:
            raise RuntimeError("summary_running")

        return batch_review_foreshadow_ledger(output_dir, book_id, entries)


def list_books() -> List[Dict[str, Any]]:
    """全部书目状态（刷新后返回）"""
    books = refresh_books()
    return [book_info(name, path) for name, path in books.items()]


def register_scan_dir(base_dir: Path) -> List[Dict[str, Any]]:
    """手动注册：扫描给定目录下的书目录并并入映射"""
    added = []
    if _is_book_dir(base_dir):
        candidates = [base_dir]
    else:
        candidates = [d for d in sorted(base_dir.iterdir()) if _is_book_dir(d)] if base_dir.exists() else []
    with _books_lock:
        for d in candidates:
            _manual_books[d.name] = d
            added.append(book_info(d.name, d))
    return added


def load_chapter_result(book_id: str, chapter: int) -> Optional[dict]:
    """读取单章完整结果 JSON"""
    output_dir = get_output_dir(book_id)
    if output_dir is None:
        return None
    f = output_dir / f"chapter_{chapter}_result.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"读取章节结果失败 {f}: {e}")
        return None
