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

import os
import re
import secrets
import shutil
import threading
import time
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


#: 目录名/ID 白名单：字母数字开头，允许 . _ -，总长 64。
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: Windows 保留设备名。它们能以目录名写进去，但之后任何 open() 都会指向设备而不是文件。
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: 事务暂存目录前缀。以点开头 → ``registry.scan_projects`` 会跳过，
#: 半成品永远不会被当成项目；残留由 :func:`cleanup_stale_staging` 清理。
STAGING_PREFIX = ".staging-"


def validate_name(value: str, *, field: str = "name") -> str:
    """校验"会被拼进文件路径的名字"。

    这是**唯一**允许把外部输入变成路径分量的地方。拒绝：空、路径分隔符、``..``、
    盘符、Windows 保留设备名、超长、非 ASCII（不拒绝会让跨平台行为不一致）。
    """
    if not isinstance(value, str) or not value:
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE, f"{field} 不能为空", path=field
        )
    if not _NAME_RE.match(value):
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE,
            f"{field} 只能由字母、数字、下划线、点、连字符组成且以字母/数字开头：{value!r}",
            path=field,
        )
    if value.strip(".") == "":
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE, f"{field} 不能只由点组成：{value!r}", path=field
        )
    if value.split(".")[0].upper() in _RESERVED_NAMES:
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE, f"{field} 是系统保留名：{value!r}", path=field
        )
    return value


def cleanup_stale_staging(data_root: Path) -> list[str]:
    """删除上次异常退出留下的暂存目录，返回删掉的名字。

    为什么要显式清理：暂存目录以点开头，扫描会跳过它——"看不见"等于"永远不会被发现"。
    进程被强杀时不会走 ``except``，所以这条清理必须有人在启动时做。
    """
    root = Path(data_root)
    if not root.is_dir():
        return []
    removed: list[str] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name.startswith(STAGING_PREFIX):
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
    return removed


def _publish_dir(staging: Path, final_dir: Path) -> None:
    """把暂存目录整体换成最终目录。

    Windows 上 ``os.replace`` 对目录有两条限制，写死在这里，免得下次有人"优化"成
    跨卷暂存：
    1. 目标必须不存在（POSIX 可覆盖空目录，Windows 抛 ``WinError 5``）；
    2. 源与目标必须同卷（放 ``%TEMP%`` 会 ``EXDEV``）。
    故暂存目录一律建在 ``data_root`` 下。
    """
    if final_dir.exists():
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE,
            f"项目已存在，拒绝覆盖：{final_dir}",
            path=str(final_dir),
        )
    last: OSError | None = None
    for attempt in range(6):
        try:
            os.replace(staging, final_dir)
            return
        except PermissionError as exc:  # 杀毒/索引器短暂持有句柄
            last = exc
            time.sleep(0.05 * (attempt + 1))
    assert last is not None
    raise last


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
    # 用 `is None` 而不是 `or`：显式传空串是调用方的错误，不该被悄悄替换成默认值
    # （否则"目录名写错"会变成"用了 book_id 当目录名"，排查时毫无线索）。
    raw_book_id = book_id if book_id is not None else f"bk_{secrets.token_hex(6)}"
    book_id = validate_name(raw_book_id, field="book_id")
    dir_label = validate_name(dir_name if dir_name is not None else book_id, field="dir_name")
    final_dir = data_root / dir_label

    # 已存在的 vnext 项目不覆盖：覆盖会让旧结果的引用指向新正文。
    if manifest_path(final_dir).exists():
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE,
            f"项目已存在，拒绝覆盖：{final_dir}",
            path=str(final_dir),
        )

    entries: list[ChapterEntry] = []
    payloads: list[tuple[str, str]] = []
    rel_chapters = chapters_dir(final_dir).relative_to(final_dir).as_posix()
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
        payloads.append((f"{rel_chapters}/{chapter_id}.txt", normalized))

    if not entries:
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE, "项目必须至少包含一章"
        )

    snapshot_id = _snapshot_id(entries)
    manifest = Manifest(
        book_id=book_id,
        workspace_id=workspace_id_for(data_root),
        title=title,
        revision=1,
        source_snapshot_id=snapshot_id,
        chapters=entries,
    )
    index_rel = _source_index_path(final_dir).relative_to(final_dir)

    # 事务：全部写在暂存目录里，最后一步整套换上。中途失败绝不留下"半个项目"——
    # 那种目录没有 manifest，会被 registry 判成 LEGACY_DIR，用户看到的是
    # "我的新项目变成旧版了"，比直接报错更误导。
    data_root.mkdir(parents=True, exist_ok=True)
    staging = data_root / f"{STAGING_PREFIX}{secrets.token_hex(8)}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
        for rel_path, text in payloads:
            path = staging / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp{secrets.token_hex(4)}")
            tmp.write_text(text, encoding="utf-8", newline="\n")
            tmp.replace(path)
        write_json_atomic(
            staging / index_rel,
            stamp_payload(
                {
                    "book_id": book_id,
                    "source_snapshot_id": snapshot_id,
                    "text_normalization_version": TEXT_NORMALIZATION_VERSION,
                    "chapters": [c.to_dict() for c in entries],
                }
            ),
        )
        # manifest 最后写：它是"这个目录算不算项目"的唯一判据。
        write_json_atomic(staging / MANIFEST_FILE_NAME, manifest.to_dict())
        _publish_dir(staging, final_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final_dir


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
