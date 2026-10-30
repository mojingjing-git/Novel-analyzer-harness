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
import time
from pathlib import Path
from typing import Any

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
            queue.progress_from_manifest(entry, manifest, plans, records)
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
