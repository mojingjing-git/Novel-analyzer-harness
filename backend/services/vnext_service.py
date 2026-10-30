"""vnext 服务门面：把 `backend.vnext` 的能力接到应用层（PLAN v2 §4 S2 接线）。

这一层只做四件事，每件都对应 S2 的一条门槛：

1. **目录发现只有一个判据**——有没有合法的 ``manifest.json``。旧目录不是"兼容"，
   而是被明确拒绝并给出原因（``LEGACY_DIR``），不去猜它像不像一本书。
2. **书目以 ``book_id`` 为身份**——书名只是展示字段。改书名不该变成另一本书。
3. **进度由块签名重算**——不数 ``chapter_*_result.json`` 文件个数。文件在不在
   不等于"这个块还算数"。
4. **队列状态独立文件**——``queue_state_vnext.json``，与旧 ``queue_state.json``
   互不影响；读不出来就报错，不"当成空队列"继续跑。

刻意不在这里做：不写迁移、不读旧产物、不做字段转换。上层要么用 vnext，要么
明确拿到 ``VnextFormatError``。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from backend.vnext import blocks, project, queue, registry, results
from backend.vnext.constants import (
    CONFIG_FILE_NAME,
    WORKSPACE_DIR_NAME,
    ErrorCode,
    VnextFormatError,
)


def novel_root() -> Path:
    """项目根，口径与 ``app.py`` / ``queue_manager.py`` 保持一致。"""
    raw = os.environ.get("NOVEL_ROOT")
    if raw:
        return Path(raw).resolve()
    return Path(__file__).resolve().parent.parent.parent


def data_root() -> Path:
    """vnext 数据根：``<NOVEL_ROOT>/workspace_vnext``。"""
    return novel_root() / WORKSPACE_DIR_NAME


def queue_path() -> Path:
    return queue.queue_state_path(novel_root())


# ---------------------------------------------------------------------------
# 书目
# ---------------------------------------------------------------------------


def list_books() -> dict[str, Any]:
    """列出可用于 vnext 的书目，并**显式列出被拒绝的目录及原因**。

    拒绝列表不是给开发者看的调试信息，而是"用户点了旧目录才能得到解释"的来源；
    因此 ``message`` 用中文写清楚下一步该做什么。
    """
    # 异常退出遗留的暂存目录以点开头，扫描会跳过它——不主动清就永远不会被发现。
    # 放在这里而不是 scan_projects 里：扫描应当是只读的，清理是入口的职责。
    try:
        project.cleanup_stale_staging(data_root())
    except OSError:
        pass

    scan = registry.scan_projects(data_root())
    return {
        "data_root": str(data_root()),
        "books": [
            {
                "book_id": ref.book_id,
                "title": ref.name,
                "path": str(ref.path),
                "chapter_count": ref.total_chapters,
            }
            for ref in scan.accepted
        ],
        "rejected": [
            {
                "path": str(item.path),
                "name": item.path.name,
                "reason": str(item.reason),
                "message": item.detail,
            }
            for item in scan.rejected
        ],
    }


def open_book(book_id: str) -> dict[str, Any]:
    """按 ``book_id`` 打开一本书的摘要（含块进度）。"""
    ref = registry.locate_project(data_root(), book_id)
    manifest = project.load_manifest(ref.path)
    plans = blocks.plan_blocks(manifest, block_size=block_size())
    verdict = results.classify(manifest, plans, results.list_results(ref.path))
    return {
        "book_id": manifest.book_id,
        "title": manifest.title,
        "revision": manifest.revision,
        "chapter_count": len(manifest.chapters),
        "block_count": len(plans),
        "done_blocks": len(verdict.done),
        "stale_blocks": len(verdict.stale),
        "missing_blocks": len(verdict.missing),
        "source_snapshot_id": manifest.source_snapshot_id,
    }


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------


#: 队列的「读-改-写」必须串行。`save_queue_state` 是原子写，但「先读列表、再整体写回」
#: 不是原子的：两个并发入队会各读到旧列表、各写回一份，后写的把先写的条目吞掉。
#: 原子写只保证「这次写不会写出半个文件」，不保证「不会丢别人的更新」。
_QUEUE_LOCK = threading.Lock()


def load_queue() -> queue.QueueState:
    """读队列状态。文件不存在=空队列；格式不对=抛错（不静默当空）。"""
    return queue.load_queue_state(queue_path())


def save_queue(state: queue.QueueState) -> None:
    queue.save_queue_state(queue_path(), state)


def restore_queue_after_restart() -> queue.QueueState:
    """进程启动时调用：回落遗留的 running，**并按当前源重算进度**。

    只回落状态是不够的。磁盘上的 ``completed_blocks`` 是上一次运行时的结论，源变了
    它就不成立——复现过的最坏情形是：改了一章之后，书籍详情显示 ``1 done + 1 missing``，
    而队列仍写着 ``done 2/2, stale=0``，于是用户以为跑完了。

    所以这里对每个能打开的书重算一次（``progress_from_manifest`` 是纯计算，不触模型）。
    算不出来的书（项目被删/被换成旧目录）保留历史状态，但显式写上原因，让人知道
    "这条记录不是当前进度的结论"。
    """
    from backend.vnext import blocks, project as project_mod, registry, results as results_mod

    state = load_queue()
    queue.normalize_after_restart(state)

    root = data_root()
    scan = registry.scan_projects(root)
    by_id = {ref.book_id: ref for ref in scan.accepted}

    size = block_size()
    for entry in state.entries:
        ref = by_id.get(entry.book_id)
        if ref is None:
            # 项目不在了：不改写历史结论，但把"为什么不能重算"写清楚
            entry.error_message = "项目不可用，进度未校验"
            entry.updated_at = time.time()
            continue
        try:
            manifest = project_mod.load_manifest(ref.path)
            plans = blocks.plan_blocks(manifest, block_size=size)
            records = results_mod.list_results(ref.path)
            queue.progress_from_manifest(
                entry, manifest, plans, records, strategy=result_strategy()
            )
        except VnextFormatError as exc:
            entry.error_message = f"进度重算失败：{exc.code.value}"
            entry.updated_at = time.time()
    save_queue(state)
    return state


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def block_size() -> int:
    """块大小来自 vnext 配置；文件不存在时用默认 4（与 1.x 一致）。

    注意：只在**文件不存在**时兜底。文件存在但内容非法必须报错——否则"配置写坏了"
    会被静默当成"用默认值"，用户永远不知道自己改的没生效。
    """
    cfg_path = novel_root() / CONFIG_FILE_NAME
    if not cfg_path.is_file():
        return 4
    payload = _read_config(cfg_path)
    analysis = payload.get("analysis")
    if analysis is None:
        analysis = {}
    if not isinstance(analysis, dict):
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE,
            f"analysis 必须是对象，实际 {type(analysis).__name__}",
            path=str(cfg_path),
        )
    raw = analysis.get("block_size", 4)
    # bool 是 int 的子类：`True` 会被 int() 顺成 1，配置写成 `block_size: true`
    # 会静默变成"每章一块"。浮点同理（2.9 → 2 是截断不是四舍五入），都拒绝。
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE,
            f"block_size 必须是整数：{raw!r}",
            path=str(cfg_path),
        )
    if raw < 1:
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE, f"block_size 必须 >= 1：{raw}", path=str(cfg_path)
        )
    return raw


def _read_config(path: Path) -> dict:
    from backend.vnext.artifacts import read_json_artifact

    return dict(read_json_artifact(path))


def result_strategy() -> results.ResultStrategy:
    """当前"生成条件"：Prompt 策略版本 + 模型指纹 + 上下文策略版本。

    这三个值一起决定"旧结果还算不算数"。模型指纹取 ``api.model`` 与几个会影响输出的
    参数；配置里没写就留空——留空的后果是"换模型不会被察觉"，这比"凭空失效"更糟，
    所以只要能读到就一定要读。
    """
    parts: list[str] = []
    cfg_path = novel_root() / CONFIG_FILE_NAME
    if cfg_path.is_file():
        payload = _read_config(cfg_path)
        api = payload.get("api") or {}
        if isinstance(api, dict):
            for key in ("model", "temperature", "max_tokens", "thinking_mode"):
                value = api.get(key)
                if value not in (None, "", {}):
                    parts.append(f"{key}={value}")
    return results.ResultStrategy(
        prompt_strategy_version=1,
        model_fingerprint="|".join(parts),
        context_strategy_version=results.CONTEXT_STRATEGY_VERSION,
    )


# ---------------------------------------------------------------------------
# 从原文创建项目（S2 基础入口）
# ---------------------------------------------------------------------------


def create_book(
    title: str,
    *,
    text: Optional[str] = None,
    chapters: Optional[list[tuple[str, str]]] = None,
    book_id: Optional[str] = None,
    dir_name: Optional[str] = None,
) -> dict:
    """从原文（或已切好的章节）创建 vnext 项目。

    两种输入：

    * ``chapters``：调用方已经切好，直接写；
    * ``text``：走**同一套**切章器（``splitter_service.split_text``）——不另写一份
      正则，否则两套切章规则会分叉，同一本书在不同入口切出不同章节。

    切章结果为空一律报错，不建"零章项目"：那会让后续每一步都在处理空集合，
    错误要到很远的地方才显形。
    """
    if chapters is None:
        if not text or not text.strip():
            raise VnextFormatError(
                ErrorCode.INVALID_FIELD_VALUE, "text 与 chapters 至少提供一个"
            )
        from backend.services import splitter_service

        split = splitter_service.split_text(text, external_title=title)
        if not split.chapters:
            raise VnextFormatError(
                ErrorCode.INVALID_FIELD_VALUE,
                "切章结果为空：没有识别到任何章节起始行（可改用 chapters 直接指定）",
            )
        chapters = [
            (i + 1, f"{block.title}\n{block.content}") for i, block in enumerate(split.chapters)
        ]
    if not chapters:
        raise VnextFormatError(ErrorCode.INVALID_FIELD_VALUE, "章节列表为空")

    book_dir = project.create_project(
        data_root(), title, chapters, book_id=book_id, dir_name=dir_name
    )
    # create_project 返回的是项目目录（Path），字段要回读一次清单才有——
    # 直接拿返回值当 Manifest 用会 AttributeError。
    manifest = project.load_manifest(book_dir)
    return {
        "book_id": manifest.book_id,
        "title": manifest.title,
        "total_chapters": len(manifest.chapters),
        "revision": manifest.revision,
        "source_snapshot_id": manifest.source_snapshot_id,
    }


def list_blocks(book_id: str) -> dict:
    """块级进度：断点恢复的读入口。

    返回每块的签名、成员章数与当前状态（done / stale:<原因> / missing:<原因>），
    并标注哪些块可以续跑。调用方据此决定"从哪继续"，而不是去数目录里的文件。
    """
    ref = registry.locate_project(data_root(), book_id)
    manifest = project.load_manifest(ref.path)
    plans = blocks.plan_blocks(manifest, block_size=block_size())
    verdict = results.classify(
        manifest, plans, results.list_results(ref.path), strategy=result_strategy()
    )
    by_sig_display = {p.block_signature: p.start_display_no for p in plans}
    items = []
    for plan in plans:
        sig = plan.block_signature
        if sig in verdict.done:
            state = "done"
        elif sig in verdict.stale:
            state = f"stale:{verdict.stale[sig]}"
        else:
            state = f"missing:{verdict.missing.get(sig, 'NOT_REGISTERED')}"
        items.append(
            {
                "block_signature": sig,
                "chapter_count": len(plan.chapter_ids),
                "start_display_no": by_sig_display.get(sig),
                "state": state,
            }
        )
    return {
        "book_id": manifest.book_id,
        "total_blocks": len(plans),
        "done_blocks": len(verdict.done),
        "stale_blocks": len(verdict.stale),
        "missing_blocks": len(verdict.missing),
        "blocks": items,
    }


def _queue_payload(state: queue.QueueState, **extra: object) -> dict:
    payload = {
        "entries": [entry.to_dict() for entry in state.entries],
        "updated_at": state.updated_at,
        # 本版没有分析流水线：入队不等于开跑。界面必须据这个字段说实话。
        "analysis_started": False,
    }
    payload.update(extra)
    return payload


def enqueue_book(payload: dict) -> dict:
    """把项目放进队列。已存在则幂等（不重复插入）。

    **判重不是可选项**：`QueueEntry` 的 `from_dict` 遇到重复 `book_id` 会抛
    `INVALID_FIELD_VALUE`，而 `load_queue_state` 不做兜底 —— 一旦写进一条重复条目，
    此后读队列恒 422，队列页与进度页会一起打不开。这是本模块唯一能「毁掉状态文件」的操作。
    """
    book_id = payload.get("book_id")
    if not isinstance(book_id, str) or not book_id.strip():
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE, "缺少 book_id（必须是非空字符串）"
        )
    book_id = book_id.strip()

    with _QUEUE_LOCK:
        state = load_queue()
        entry = state.find(book_id)
        created = entry is None
        if created:
            detail = open_book(book_id)          # 项目打不开就在这里抛，不会写脏队列
            entry = queue.QueueEntry(
                book_id=book_id,
                name=detail["title"],
                status=queue.STATUS_PENDING,
                total_chapters=detail["chapter_count"],
                updated_at=time.time(),
            )
            state.entries.append(entry)
            save_queue(state)
    return _queue_payload(state, enqueued=book_id, created=created)


def dequeue_book(book_id: str) -> dict:
    book_id = (book_id or "").strip()
    with _QUEUE_LOCK:
        state = load_queue()
        entry = state.find(book_id)
        if entry is None:
            raise VnextFormatError(ErrorCode.NOT_FOUND, f"队列里没有 {book_id}")
        state.entries.remove(entry)
        save_queue(state)
    return _queue_payload(state, removed=book_id)
