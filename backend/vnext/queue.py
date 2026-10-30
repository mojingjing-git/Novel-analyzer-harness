"""vnext 队列状态与恢复（PLAN v2 §2.4 / §4 S2-4）。

与旧实现的关键差别：旧队列把 ``blocks_dir`` / ``workspace_dir`` 绝对路径当身份，
恢复时用 ``glob chapter_*_result.json`` 数文件推断进度，没有任何"这些结果还配得上
当前源吗"的校验。这里进度必须**由块签名重算得出**，对不上就是 ``STALE``。

进程崩溃后遗留的 ``running`` 一律回落 ``pending``：不猜它跑到哪，让块级判定说。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .artifacts import read_json_artifact, stamp_payload, write_json_atomic
from .constants import QUEUE_STATE_FILE_NAME, ErrorCode, VnextFormatError

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
VALID_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED}
)


@dataclass
class QueueEntry:
    book_id: str
    name: str
    status: str = STATUS_PENDING
    total_chapters: int = 0
    block_size: int = 1
    completed_blocks: int = 0
    total_blocks: int = 0
    stale_blocks: int = 0
    error_message: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "book_id": self.book_id,
            "name": self.name,
            "status": self.status,
            "total_chapters": self.total_chapters,
            "block_size": self.block_size,
            "completed_blocks": self.completed_blocks,
            "total_blocks": self.total_blocks,
            "stale_blocks": self.stale_blocks,
            "error_message": self.error_message,
            "updated_at": self.updated_at or time.time(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QueueEntry":
        for key in ("book_id", "name"):
            if key not in data:
                raise VnextFormatError(
                    ErrorCode.MISSING_REQUIRED_FIELD, f"队列条目缺字段：{key}"
                )
        status = str(data.get("status", STATUS_PENDING))
        if status not in VALID_STATUSES:
            raise VnextFormatError(
                ErrorCode.INVALID_FIELD_VALUE, f"非法的队列状态：{status}"
            )
        return cls(
            book_id=str(data["book_id"]),
            name=str(data["name"]),
            status=status,
            total_chapters=int(data.get("total_chapters", 0)),
            block_size=int(data.get("block_size", 1)),
            completed_blocks=int(data.get("completed_blocks", 0)),
            total_blocks=int(data.get("total_blocks", 0)),
            stale_blocks=int(data.get("stale_blocks", 0)),
            error_message=str(data.get("error_message", "")),
            updated_at=float(data.get("updated_at") or 0.0),
        )


@dataclass
class QueueState:
    entries: list[QueueEntry] = field(default_factory=list)
    updated_at: float = 0.0

    def find(self, book_id: str) -> QueueEntry | None:
        for entry in self.entries:
            if entry.book_id == book_id:
                return entry
        return None

    def to_dict(self) -> dict:
        return stamp_payload(
            {
                "updated_at": self.updated_at or time.time(),
                "entries": [e.to_dict() for e in self.entries],
            }
        )

    @classmethod
    def from_dict(cls, data: dict) -> "QueueState":
        raw = data.get("entries")
        if not isinstance(raw, list):
            raise VnextFormatError(
                ErrorCode.MISSING_REQUIRED_FIELD, "队列状态缺 entries 列表"
            )
        entries = [QueueEntry.from_dict(dict(item)) for item in raw]
        seen: set[str] = set()
        for entry in entries:
            if entry.book_id in seen:
                raise VnextFormatError(
                    ErrorCode.INVALID_FIELD_VALUE, f"队列里出现重复 book_id：{entry.book_id}"
                )
            seen.add(entry.book_id)
        return cls(entries=entries, updated_at=float(data.get("updated_at") or 0.0))


def queue_state_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / QUEUE_STATE_FILE_NAME


def load_queue_state(path: Path) -> QueueState:
    """读队列状态。文件不存在 → 空队列（首次运行）；格式不对 → 报错。

    刻意不做"读不出来就当空队列"：那会让一次格式变更静默清空用户的任务列表。
    """
    path = Path(path)
    if not path.is_file():
        return QueueState()
    return QueueState.from_dict(dict(read_json_artifact(path)))


def save_queue_state(path: Path, state: QueueState) -> None:
    write_json_atomic(Path(path), state.to_dict())


def normalize_after_restart(state: QueueState) -> QueueState:
    """把"上次崩在半路"的条目回落成 ``pending``。

    只动 ``running``：``done``/``failed``/``skipped`` 是用户可见的历史结论，重启
    不该改写。同时清掉错误信息——旧实现只改状态不清原因，会留下"pending 却带着
    上次的 error_message"这种自相矛盾的记录。
    """
    for entry in state.entries:
        if entry.status == STATUS_RUNNING:
            entry.status = STATUS_PENDING
            entry.error_message = ""
    state.updated_at = time.time()
    return state


def apply_progress(
    entry: QueueEntry,
    *,
    completed_blocks: int,
    total_blocks: int,
    stale_blocks: int,
    total_chapters: int,
) -> QueueEntry:
    """把块级判定结果写回条目。

    完成与否只看 ``completed_blocks == total_blocks and stale_blocks == 0``，
    不看"上次状态是什么"——状态是结论，不是依据。
    """
    entry.completed_blocks = completed_blocks
    entry.total_blocks = total_blocks
    entry.stale_blocks = stale_blocks
    entry.total_chapters = total_chapters
    if total_blocks and completed_blocks == total_blocks and stale_blocks == 0:
        entry.status = STATUS_DONE
    elif entry.status in (STATUS_DONE, STATUS_SKIPPED):
        # 曾经完成过，但现在有块失效/缺失 → 退回 pending 等待重算
        entry.status = STATUS_PENDING
    entry.updated_at = time.time()
    return entry


def progress_from_manifest(
    entry: QueueEntry,
    manifest,
    plans: list,
    records: dict | None = None,
):
    """从 manifest + 块计划直接算出条目进度（不读旧状态）。"""
    from .results import classify

    verdict = classify(manifest, plans, records)
    return apply_progress(
        entry,
        completed_blocks=len(verdict.done),
        total_blocks=len(plans),
        stale_blocks=len(verdict.stale),
        total_chapters=len(manifest.chapters),
    )
