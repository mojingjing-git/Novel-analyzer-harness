"""vnext 产物读写：格式门禁 + 原子写。

两条硬规则（PLAN v2 §4 S2）：

1. **没有格式标记就拒绝**。缺 ``format_id`` / ``schema_version`` 的文件不是
   "可以猜一猜的旧文件"，而是"不属于本版本"。这里不提供任何降级解析、
   不补默认值、不尝试修复损坏的 JSON。
2. **写必须原子**。临时文件与目标同目录（跨盘 rename 不是原子的）、名字唯一
   （并发写不能互相覆盖）、写完 ``fsync`` 再 ``os.replace``。

为什么不用 ``backend/utils/json_utils.safe_save_json``：那个函数是旧格式的写入器，
它带 ``json_repair`` 兜底与"失败返回 default"的语义。在 vnext 里"写失败"必须抛出，
让调用方不能把"没写进去"当成"写成功了"——这正是 S2 要堵的口子。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from .constants import FORMAT_ID, SCHEMA_VERSION, ErrorCode, VnextFormatError

#: Windows 上 ``os.replace`` 会因杀软/索引器短暂持有句柄而抛 PermissionError。
#: 不重试的话，一次偶发就把"已提交"变成"没提交"。次数与间隔都写死，不做指数退避——
#: 这里等的是句柄释放，不是负载下降。
_REPLACE_RETRIES = 6
_REPLACE_BACKOFF_SEC = 0.05


# ---------------------------------------------------------------------------
# 格式门禁
# ---------------------------------------------------------------------------


def assert_vnext_payload(data: Any, *, path: str | Path | None = None) -> Mapping[str, Any]:
    """校验"这是一个 vnext 产物"，返回原对象（不改内容）。

    顺序固定：顶层类型 → 格式族 → 版本。先判格式族再判版本，是为了让旧文件
    得到 ``UNSUPPORTED_ARTIFACT_FORMAT``（用户可见"此目录不属于本版本"），
    而不是版本号错配这种会误导人的结论。
    """
    where = str(path) if path is not None else None
    if not isinstance(data, Mapping):
        raise VnextFormatError(
            ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
            f"顶层不是 JSON 对象（实际 {type(data).__name__}）",
            path=where,
        )
    if "format_id" not in data and "schema_version" not in data:
        raise VnextFormatError(
            ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
            "缺少格式标记（format_id / schema_version），判定为旧版本产物",
            path=where,
        )
    format_id = data.get("format_id")
    if format_id != FORMAT_ID:
        raise VnextFormatError(
            ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
            f"format_id 为 {format_id!r}，本版本只接受 {FORMAT_ID!r}",
            path=where,
        )
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise VnextFormatError(
            ErrorCode.UNSUPPORTED_SCHEMA_VERSION,
            f"schema_version 为 {version!r}，本实现只支持 {SCHEMA_VERSION}",
            path=where,
        )
    return data


def read_json_artifact(path: Path) -> Mapping[str, Any]:
    """读一个 vnext JSON 产物并校验格式。

    文件不存在不在本函数职责内（调用方需要区分"没有"与"有但不合法"），
    因此这里让 ``FileNotFoundError`` 自然抛出。
    """
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VnextFormatError(
            ErrorCode.UNPARSEABLE_ARTIFACT,
            f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）",
            path=str(path),
        ) from exc
    return assert_vnext_payload(data, path=path)


def stamp_payload(payload: Mapping[str, Any]) -> dict:
    """给待写出的对象补上格式标记（浅拷贝，不改入参）。"""
    out = dict(payload)
    out["format_id"] = FORMAT_ID
    out["schema_version"] = SCHEMA_VERSION
    return out


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------


def write_json_atomic(path: Path, payload: Mapping[str, Any], *, indent: int = 2) -> None:
    """原子写 JSON。失败抛异常，绝不"尽量写一点"。

    调用方若需要"写了才算成功"的语义（例如发布 manifest 引用），必须在本函数
    正常返回之后才更新内存状态——顺序反了就会出现"内存说已提交、磁盘没这个文件"。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=indent)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp_path, path)
        tmp_path = None  # 成功替换后临时文件已不存在
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _replace_with_retry(src: Path, dst: Path) -> None:
    last_exc: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:  # Windows：句柄/索引器短暂占用
            last_exc = exc
            if attempt < _REPLACE_RETRIES - 1:
                time.sleep(_REPLACE_BACKOFF_SEC * (attempt + 1))
    assert last_exc is not None
    raise last_exc
