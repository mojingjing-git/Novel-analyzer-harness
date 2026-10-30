"""书目发现与索引：只认得 vnext 项目（PLAN v2 §1.1.5 / §4 S2-4）。

旧目录在这里被**识别但拒绝**：扫描时看有没有 ``manifest.json``，没有就归为
``LEGACY_DIR`` 记在拒绝清单里，绝不去猜 ``blocks/`` 之类的形状。这与旧
``book_service._is_book_dir``（"有 blocks/ 就算书"）的区别是本质的：后者会把
任何同名目录都当书打开，前者只承认自己写的清单。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

from .artifacts import read_json_artifact, stamp_payload, write_json_atomic
from .constants import BOOK_INDEX_FILE_NAME, ErrorCode, VnextFormatError
from .project import Manifest, is_vnext_project, load_manifest

#: 拒绝原因。刻意区分"旧版本目录"与"坏了"，因为用户动作不同：
#: 前者去新建项目，后者可能要恢复备份。
RejectReason = Literal[
    "LEGACY_DIR",           # 没有 manifest.json：旧版本产物
    "UNSUPPORTED_FORMAT",   # 有清单但不是本版本格式
    "UNPARSEABLE",          # 有清单但读不出来
    "MISSING_SOURCE",       # 清单在，但 source/index.json 缺失
    "DUPLICATE_BOOK_ID",    # 同一 book_id 出现在两个目录：身份冲突，不能任取其一
]


@dataclass(frozen=True)
class ProjectRef:
    book_id: str
    name: str
    path: Path
    total_chapters: int

    def to_dict(self, *, base: Path | None = None) -> dict:
        where = self.path
        if base is not None:
            try:
                where = self.path.resolve().relative_to(base.resolve())
            except ValueError:
                pass
        return {
            "book_id": self.book_id,
            "name": self.name,
            "path": where.as_posix(),
            "total_chapters": self.total_chapters,
        }


@dataclass(frozen=True)
class RejectedDir:
    path: Path
    reason: RejectReason
    detail: str = ""


@dataclass
class ScanResult:
    accepted: list[ProjectRef] = field(default_factory=list)
    rejected: list[RejectedDir] = field(default_factory=list)

    @property
    def rejected_paths(self) -> list[Path]:
        return [r.path for r in self.rejected]


def scan_projects(data_root: Path) -> ScanResult:
    """扫描数据根下的一层子目录，分出"可打开"与"拒绝"两类。

    只扫一层：更深层目录不是项目，扫进去会把归档区/备份区当成书。
    """
    data_root = Path(data_root)
    result = ScanResult()
    if not data_root.is_dir():
        return result

    for child in sorted(data_root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.is_file():
            result.rejected.append(
                RejectedDir(child, "LEGACY_DIR", "缺少 manifest.json（旧版本目录）")
            )
            continue
        try:
            manifest = load_manifest(child)
        except VnextFormatError as exc:
            reason: RejectReason = (
                "UNPARSEABLE" if exc.code is ErrorCode.UNPARSEABLE_ARTIFACT
                else "UNSUPPORTED_FORMAT"
            )
            result.rejected.append(RejectedDir(child, reason, exc.detail))
            continue
        result.accepted.append(
            ProjectRef(
                book_id=manifest.book_id,
                name=manifest.title,
                path=child,
                total_chapters=len(manifest.chapters),
            )
        )
    _reject_duplicate_identities(result)
    return result


def _reject_duplicate_identities(result: ScanResult) -> None:
    """同一 ``book_id`` 出现在多个目录时，**保留第一个、其余全部拒绝**。

    为什么不是"取最新"或"取内容多的"：这两个判据都会让"打开哪一本书"取决于磁盘
    状态，同一份数据在不同机器上可能打开不同的书。身份冲突必须让人看见并手动处理，
    所以冲突目录连同原因一起进拒绝清单，界面上会出现两条提示而不是悄悄选一个。
    """
    seen: dict[str, ProjectRef] = {}
    kept: list[ProjectRef] = []
    for ref in result.accepted:
        first = seen.get(ref.book_id)
        if first is None:
            seen[ref.book_id] = ref
            kept.append(ref)
            continue
        result.rejected.append(
            RejectedDir(
                ref.path,
                "DUPLICATE_BOOK_ID",
                f"book_id {ref.book_id} 与 {first.path.name} 冲突（已保留第一个）",
            )
        )
    result.accepted = kept


def locate_project(data_root: Path, book_id: str) -> ProjectRef:
    """按 ``book_id`` 定位项目，返回带路径的引用。

    走的是 :func:`scan_projects` 的**已校验结果**，不自己再遍历一遍目录：否则一本
    排序靠前的坏清单会在这里抛异常，把另一本好书也堵死（同一份数据，扫描能看到、
    打开却打不开，是最难解释的那种故障）。重复身份也已在扫描阶段被剔除。
    """
    scan = scan_projects(data_root)
    for ref in scan.accepted:
        if ref.book_id == book_id:
            return ref
    detail = f"未找到 book_id={book_id} 的 vnext 项目"
    conflicts = [r for r in scan.rejected if r.reason == "DUPLICATE_BOOK_ID"
                 and book_id in r.detail]
    if conflicts:
        detail += f"（存在身份冲突：{conflicts[0].path.name}）"
    raise VnextFormatError(ErrorCode.NOT_FOUND, detail)


def open_project(data_root: Path, book_id: str) -> Manifest:
    """按 ``book_id`` 打开项目；不存在或不合法都明确报错。

    这里不做"名字模糊匹配"——旧实现把书名当主键，改名即失联；
    vnext 的主键是 ``book_id``，名字只是展示字段。
    """
    ref = locate_project(data_root, book_id)
    return load_manifest(ref.path)


# ---------------------------------------------------------------------------
# 索引文件（books_vnext.json）
# ---------------------------------------------------------------------------


def _index_path(data_root: Path) -> Path:
    return Path(data_root) / BOOK_INDEX_FILE_NAME


def read_index(data_root: Path) -> dict[str, ProjectRef]:
    """读索引；文件不存在返回空（首次运行），格式不对抛错。"""
    path = _index_path(data_root)
    if not path.is_file():
        return {}
    payload = read_json_artifact(path)
    entries = payload.get("books")
    if not isinstance(entries, list):
        raise VnextFormatError(
            ErrorCode.MISSING_REQUIRED_FIELD, "索引缺 books 列表", path=str(path)
        )
    out: dict[str, ProjectRef] = {}
    base = Path(data_root)
    for raw in entries:
        if not isinstance(raw, dict) or "book_id" not in raw:
            raise VnextFormatError(
                ErrorCode.MISSING_REQUIRED_FIELD, "索引条目缺 book_id", path=str(path)
            )
        where = Path(str(raw.get("path", "")))
        if not where.is_absolute():
            where = base / where
        out[str(raw["book_id"])] = ProjectRef(
            book_id=str(raw["book_id"]),
            name=str(raw.get("name", "")),
            path=where,
            total_chapters=int(raw.get("total_chapters", 0)),
        )
    return out


def write_index(data_root: Path, refs: Iterable[ProjectRef]) -> None:
    """写索引。路径存**相对数据根**，换盘符/搬目录后仍能打开。"""
    data_root = Path(data_root)
    ordered = sorted(refs, key=lambda r: r.name)
    payload = stamp_payload(
        {
            "updated_at": time.time(),
            "books": [r.to_dict(base=data_root) for r in ordered],
        }
    )
    write_json_atomic(_index_path(data_root), payload)


def refresh_index(data_root: Path) -> ScanResult:
    """按实际扫描结果重建索引，并把拒绝清单作为返回值带出（不写进索引）。

    拒绝项刻意不入索引：索引是"可打开的书"，不是"磁盘上见过的东西"。
    """
    scan = scan_projects(data_root)
    write_index(data_root, scan.accepted)
    return scan
