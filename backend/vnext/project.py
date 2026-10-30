"""vnext 项目：建项目、读项目、提交清单（PLAN v2 §2.1 / §4 S2-2、S2-3、S2-4）。

三个不变量，本模块负责守住：

1. **身份不随内容漂移**。``book_id`` 建项目时定死；``chapter_id`` 绑定
   ``(book_id, ordinal)``。正文改写只改 ``content_hash``，因此"改一章 → 下游标
   STALE_INPUT"这条链路才成立。
2. **提交要带 revision**。清单读入时拿到 revision，写回时若磁盘上的 revision 已变，
   说明别人先提交了，此时不覆盖——返回 :class:`RevisionConflict`，由调用方重读。
3. **写入串行到"书"**。同一本书的清单提交共享一把锁；不同书之间不共享任何锁，
   避免一本卡死拖住全库。
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .artifacts import read_json_artifact, stamp_payload, write_json_atomic
from .constants import (
    FORMAT_ID,
    MANIFEST_FILE_NAME,
    SCHEMA_VERSION,
    ErrorCode,
    VnextFormatError,
)
from .fingerprint import (
    TEXT_NORMALIZATION_VERSION,
    block_signature,
    canonical_json,
    content_hash,
    normalize_chapter_text,
)

_TZ = timezone(timedelta(hours=8))


def _now() -> str:
    """本地时间 ISO8601（带偏移）。**不参与任何指纹**，只供人看。"""
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _short_id(prefix: str, *parts: str) -> str:
    import hashlib

    joined = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(joined).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChapterEntry:
    """清单里的章节条目（只放身份，不放正文）。"""

    chapter_id: str
    ordinal: int
    display_no: int | None
    content_hash: str

    def to_dict(self) -> dict:
        return {
            "chapter_id": self.chapter_id,
            "ordinal": self.ordinal,
            "display_no": self.display_no,
            "content_hash": self.content_hash,
        }


@dataclass
class Manifest:
    """项目清单的内存表示。``revision`` 是本模块做并发控制的唯一依据。"""

    book_id: str
    workspace_id: str
    title: str
    revision: int
    source_snapshot_id: str
    chapters: list[ChapterEntry] = field(default_factory=list)
    active_blocks: list[str] = field(default_factory=list)
    active_results: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return stamp_payload(
            {
                "book_id": self.book_id,
                "workspace_id": self.workspace_id,
                "title": self.title,
                "revision": self.revision,
                "source_snapshot_id": self.source_snapshot_id,
                "chapters": [c.to_dict() for c in self.chapters],
                "active_blocks": list(self.active_blocks),
                "active_results": list(self.active_results),
                "text_normalization_version": TEXT_NORMALIZATION_VERSION,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        _require(data, ("book_id", "workspace_id", "title", "revision",
                        "source_snapshot_id", "chapters"), kind="manifest")
        chapters = []
        for idx, raw in enumerate(data["chapters"]):
            if not isinstance(raw, dict):
                raise VnextFormatError(
                    ErrorCode.INVALID_FIELD_VALUE, f"chapters[{idx}] 不是对象"
                )
            _require(raw, ("chapter_id", "ordinal", "content_hash"),
                     kind=f"chapters[{idx}]")
            chapters.append(
                ChapterEntry(
                    chapter_id=str(raw["chapter_id"]),
                    ordinal=int(raw["ordinal"]),
                    display_no=raw.get("display_no"),
                    content_hash=str(raw["content_hash"]),
                )
            )
        return cls(
            book_id=str(data["book_id"]),
            workspace_id=str(data["workspace_id"]),
            title=str(data["title"]),
            revision=int(data["revision"]),
            source_snapshot_id=str(data["source_snapshot_id"]),
            chapters=chapters,
            active_blocks=list(data.get("active_blocks", [])),
            active_results=list(data.get("active_results", [])),
            created_at=str(data.get("created_at", _now())),
            updated_at=str(data.get("updated_at", _now())),
        )


def _require(mapping: dict, keys: Sequence[str], *, kind: str) -> None:
    """必需字段检查。缺字段是格式错误，**不用默认值补全**。"""
    missing = [k for k in keys if k not in mapping]
    if missing:
        raise VnextFormatError(
            ErrorCode.MISSING_REQUIRED_FIELD,
            f"{kind} 缺必需字段：{', '.join(missing)}",
        )


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def manifest_path(book_dir: Path) -> Path:
    return Path(book_dir) / MANIFEST_FILE_NAME


def chapters_dir(book_dir: Path) -> Path:
    return Path(book_dir) / "source" / "chapters"


def blocks_dir(book_dir: Path) -> Path:
    return Path(book_dir) / "blocks"


def results_dir(book_dir: Path) -> Path:
    return Path(book_dir) / "results"


def ledger_path(book_dir: Path) -> Path:
    return Path(book_dir) / "ledger" / "foreshadow.json"


def derived_dir(book_dir: Path) -> Path:
    return Path(book_dir) / "derived"


def cache_dir(book_dir: Path) -> Path:
    return Path(book_dir) / "cache"


def _source_index_path(book_dir: Path) -> Path:
    return Path(book_dir) / "source" / "index.json"


# ---------------------------------------------------------------------------
# 建项目
# ---------------------------------------------------------------------------


def workspace_id_for(data_root: Path) -> str:
    """数据根身份：跟随路径变化。

    换一个数据根就是换一个工作区——即使把旧目录拷过来，``workspace_id`` 也会变，
    这样"清单里引用的产物属于哪个根"能被查出，而不是靠运气。
    """
    return _short_id("ws", str(Path(data_root).resolve()).lower())


def create_project(
    data_root: Path,
    title: str,
    chapters: Iterable[tuple[int | None, str]],
    *,
    book_id: str | None = None,
    dir_name: str | None = None,
) -> Path:
    """从原文创建 vnext 项目，返回项目目录。

    ``chapters`` 是 ``(display_no, 原始正文)`` 序列，**顺序即故事顺序**。正文在这里
    只做规范化与落盘，不切块——切块是 :mod:`backend.vnext.blocks` 的事。
    """
    data_root = Path(data_root)
    book_id = book_id or f"bk_{secrets.token_hex(6)}"
    book_dir = data_root / (dir_name or book_id)

    # 已存在的 vnext 项目不覆盖：覆盖会让旧结果的引用指向新正文。
    if manifest_path(book_dir).exists():
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE,
            f"项目已存在，拒绝覆盖：{book_dir}",
            path=str(book_dir),
        )

    entries: list[ChapterEntry] = []
    payloads: list[tuple[Path, str]] = []
    for ordinal, (display_no, raw_text) in enumerate(chapters, start=1):
        normalized = normalize_chapter_text(raw_text)
        chapter_id = _short_id("ch", book_id, f"{ordinal:06d}")
        entries.append(
            ChapterEntry(
                chapter_id=chapter_id,
                ordinal=ordinal,
                display_no=display_no,
                content_hash=content_hash(normalized),
            )
        )
        payloads.append((chapters_dir(book_dir) / f"{chapter_id}.txt", normalized))

    if not entries:
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE, "项目必须至少包含一章"
        )

    for path, text in payloads:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 章节正文是纯文本，不是 JSON：用同目录临时文件 + replace 保证原子
        tmp = path.with_name(f".{path.name}.tmp{secrets.token_hex(4)}")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.replace(path)

    snapshot_id = _snapshot_id(entries)
    manifest = Manifest(
        book_id=book_id,
        workspace_id=workspace_id_for(data_root),
        title=title,
        revision=1,
        source_snapshot_id=snapshot_id,
        chapters=entries,
    )
    write_json_atomic(_source_index_path(book_dir), stamp_payload(
        {
            "book_id": book_id,
            "source_snapshot_id": snapshot_id,
            "text_normalization_version": TEXT_NORMALIZATION_VERSION,
            "chapters": [c.to_dict() for c in entries],
        }
    ))
    write_json_atomic(manifest_path(book_dir), manifest.to_dict())
    return book_dir


def _snapshot_id(entries: Sequence[ChapterEntry]) -> str:
    """源快照 ID：由"有序的 (chapter_id, content_hash)"决定。"""
    import hashlib

    payload = canonical_json([[c.chapter_id, c.content_hash] for c in entries])
    return f"src_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# 读项目
# ---------------------------------------------------------------------------


def is_vnext_project(path: Path) -> bool:
    """目录里有没有 vnext 清单（不解析内容，只判存在性，供快速筛选）。"""
    return manifest_path(path).is_file()


def load_manifest(book_dir: Path) -> Manifest:
    """读清单并校验格式。旧目录（有 ``blocks/`` 无清单）会在这里被拒。"""
    book_dir = Path(book_dir)
    if not manifest_path(book_dir).exists():
        raise VnextFormatError(
            ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
            "目录中没有 vnext 清单（manifest.json），不属于本版本",
            path=str(book_dir),
        )
    return Manifest.from_dict(dict(read_json_artifact(manifest_path(book_dir))))


def read_chapter_text(book_dir: Path, chapter_id: str) -> str:
    """读某章的**权威**正文。``source_ref`` 的偏移只对这个内容有效。"""
    path = chapters_dir(book_dir) / f"{chapter_id}.txt"
    if not path.is_file():
        raise VnextFormatError(
            ErrorCode.MISSING_REQUIRED_FIELD,
            f"章节正文缺失：{chapter_id}",
            path=str(path),
        )
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 提交（并发控制）
# ---------------------------------------------------------------------------


class RevisionConflict(Exception):
    """清单在本次提交前被别的任务改过。调用方应重读后重试。"""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"manifest revision 冲突：期望 {expected}，实际 {actual}")


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(book_dir: Path) -> threading.Lock:
    key = str(Path(book_dir).resolve()).lower()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def commit_manifest(
    book_dir: Path,
    mutate: Callable[[Manifest], None],
    *,
    expected_revision: int | None = None,
) -> Manifest:
    """读 → 改 → 写，revision +1。同一本书串行，跨书并行。

    ``expected_revision`` 是"我以为的版本"。省略时用刚读到的版本（即"只要期间
    没人改就成功"）。冲突时抛 :class:`RevisionConflict`，**不重试、不合并**——
    合并策略属于调用方的语义，放在这里迟早会写出"两边的改动都少了一半"。
    """
    book_dir = Path(book_dir)
    with _lock_for(book_dir):
        current = load_manifest(book_dir)
        if expected_revision is not None and current.revision != expected_revision:
            raise RevisionConflict(expected_revision, current.revision)
        mutate(current)
        current.revision += 1
        current.updated_at = _now()
        current.source_snapshot_id = _snapshot_id(current.chapters)
        # 先落盘成功，再让调用方认为"已提交"：本函数返回即代表磁盘已更新
        write_json_atomic(manifest_path(book_dir), current.to_dict())
        return current


def block_signature_for(
    manifest: Manifest,
    chapter_ids: Sequence[str],
    *,
    block_strategy_version: int = 1,
) -> str:
    """按清单里的正文哈希算块签名（避免调用方自己去查哈希时抄错）。"""
    by_id = {c.chapter_id: c.content_hash for c in manifest.chapters}
    missing = [cid for cid in chapter_ids if cid not in by_id]
    if missing:
        raise VnextFormatError(
            ErrorCode.MISSING_REQUIRED_FIELD,
            f"块成员不在清单中：{', '.join(missing)}",
        )
    pairs = [(cid, by_id[cid]) for cid in chapter_ids]
    return block_signature(pairs, block_strategy_version=block_strategy_version)
