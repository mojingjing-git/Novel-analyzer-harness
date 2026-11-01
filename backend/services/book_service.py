"""
书目服务
维护 书名 -> 书目录 的映射（书目录 = workspace/{书名}/，含 blocks/ 与 output/）。
来源：队列条目、队列条目所在根目录的兄弟目录、"分析结果"归档目录、config.working_directory。
GET /api/books 时刷新；其余接口按 id 惰性解析。
"""

import json
import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any

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


def _is_book_dir(d: Path) -> bool:
    """书目录判定：含 blocks/ 或 output/ 子目录"""
    return d.is_dir() and ((d / "blocks").exists() or (d / "output").exists())


def _candidate_roots() -> List[Path]:
    """收集可能包含书目录的根目录"""
    service = get_service()
    roots: List[Path] = []

    for item in service.queue.items:
        parent = item.workspace_dir.parent
        if parent.exists():
            roots.append(parent)
        archive = parent / "分析结果"
        if archive.exists():
            roots.append(archive)

    wd = service.config_manager.config.working_directory
    if wd:
        wd_path = Path(wd)
        if wd_path.exists():
            # working_directory 本身就是书目录，其父目录作为根
            roots.append(wd_path.parent if _is_book_dir(wd_path) else wd_path)
            archive = wd_path.parent / "分析结果"
            if archive.exists():
                roots.append(archive)

    # 工作区目录（workspace/）及其归档目录作为候选根
    ws = service.workspace_path
    if ws.exists():
        roots.append(ws)
        archive = ws / "分析结果"
        if archive.exists():
            roots.append(archive)

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


def _count_results(output_dir: Path) -> tuple[int, int, int]:
    """统计结果文件：(数量, 最小章号, 最大章号)"""
    chapters = []
    if output_dir.exists():
        for f in output_dir.glob("chapter_*_result.json"):
            m = re.match(r"chapter_(\d+)_result\.json", f.name)
            if m:
                chapters.append(int(m.group(1)))
    if not chapters:
        return 0, 0, 0
    return len(chapters), min(chapters), max(chapters)


def book_info(book_id: str, path: Path) -> Dict[str, Any]:
    """单本书的状态摘要"""
    output_dir = path / "output"
    result_count, ch_min, ch_max = _count_results(output_dir)
    blocks_dir = path / "blocks"
    total_blocks = len([f for f in blocks_dir.glob("*.txt")
                        if re.match(r"^\d+\.txt$", f.name)]) if blocks_dir.exists() else 0
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
    }


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
