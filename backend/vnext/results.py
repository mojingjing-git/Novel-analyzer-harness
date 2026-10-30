"""块结果落盘与新鲜度判定（PLAN v2 §2.4 / §4 S2-6）。

结果文件按块签名命名，一眼能看出它属于哪一块；判定"还能不能复用"不靠文件
是否存在，而靠**重算块签名**——正文改了、块重组了，同一个文件名就不会再被
当成有效结果。这就是 S2 门槛里"更改块组成或正文使相关缓存失效"的落地点。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .artifacts import read_json_artifact, stamp_payload, write_json_atomic
from .blocks import BlockPlan, verify_block
from .constants import ErrorCode, VnextFormatError
from .project import Manifest

RESULT_DIR_NAME = "results"

#: 结果状态。只有 ``DONE`` 能被当作"这一块已完成"；
#: ``STALE`` 不是写进去的状态，而是读出来后的判定结论（见 ``classify``）。
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"
_WRITABLE_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED, STATUS_CANCELLED})

#: 判定为失效的原因，供界面区分"要重算"与"根本换了一套源"。
STALE_BLOCK_CHANGED = "BLOCK_SIGNATURE_MISMATCH"
STALE_NOT_DONE = "NOT_DONE"
STALE_MISSING_BLOCK = "BLOCK_MISSING"


def results_dir(book_dir: Path) -> Path:
    return Path(book_dir) / RESULT_DIR_NAME


def result_path(book_dir: Path, block_signature: str) -> Path:
    return results_dir(book_dir) / f"{block_signature}.json"


@dataclass(frozen=True)
class ResultRecord:
    block_signature: str
    status: str
    result_id: str | None = None
    usage_status: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return stamp_payload(
            {
                "block_signature": self.block_signature,
                "status": self.status,
                "result_id": self.result_id,
                "usage_status": self.usage_status,
                # 未知用量保持 None：写 0 会让"不知道"变成"花了 0 元"
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "error": self.error,
                "updated_at": self.updated_at or time.time(),
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping) -> "ResultRecord":
        for key in ("block_signature", "status"):
            if key not in data:
                raise VnextFormatError(
                    ErrorCode.MISSING_REQUIRED_FIELD, f"结果记录缺字段：{key}"
                )
        status = str(data["status"])
        if status not in _WRITABLE_STATUSES:
            raise VnextFormatError(
                ErrorCode.INVALID_FIELD_VALUE, f"非法的结果状态：{status}"
            )
        return cls(
            block_signature=str(data["block_signature"]),
            status=status,
            result_id=data.get("result_id"),
            usage_status=data.get("usage_status"),
            prompt_tokens=data.get("prompt_tokens"),
            completion_tokens=data.get("completion_tokens"),
            error=data.get("error"),
            updated_at=float(data.get("updated_at") or 0.0),
        )


def write_result(book_dir: Path, record: ResultRecord) -> Path:
    if record.status not in _WRITABLE_STATUSES:
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE, f"不能写入状态：{record.status}"
        )
    path = result_path(book_dir, record.block_signature)
    write_json_atomic(path, record.to_dict())
    return path


def read_result(book_dir: Path, block_signature: str) -> ResultRecord | None:
    path = result_path(book_dir, block_signature)
    if not path.is_file():
        return None
    return ResultRecord.from_dict(dict(read_json_artifact(path)))


def list_results(book_dir: Path) -> dict[str, ResultRecord]:
    """按块签名索引全部结果。坏文件跳过并计数不给调用方 —— 单个坏文件
    不该让整本书打不开（它会在 ``classify`` 里自然落到"未完成"）。"""
    out: dict[str, ResultRecord] = {}
    directory = results_dir(book_dir)
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            record = ResultRecord.from_dict(dict(read_json_artifact(path)))
        except VnextFormatError:
            continue
        out[record.block_signature] = record
    return out


@dataclass
class Classified:
    """把"磁盘上有什么"翻译成"哪些块算完成、哪些要重算"。"""

    done: dict[str, ResultRecord]
    stale: dict[str, str]  # block_signature -> 原因
    missing: list[str]     # 计划里有、磁盘上没有的块签名


def classify(
    manifest: Manifest,
    plans: list[BlockPlan],
    records: Mapping[str, ResultRecord] | None = None,
) -> Classified:
    """判定每个计划块的状态。

    判定顺序：签名是否还对得上 → 状态是否 DONE。签名先过是因为签名不符意味着
    "这块的内容已经变了"，此时状态是什么都无意义。
    """
    if records is None:
        records = {}
    done: dict[str, ResultRecord] = {}
    stale: dict[str, str] = {}
    missing: list[str] = []

    for plan in plans:
        record = records.get(plan.block_signature)
        if record is None:
            missing.append(plan.block_signature)
            continue
        try:
            verify_block(manifest, plan)
        except VnextFormatError:
            stale[plan.block_signature] = STALE_BLOCK_CHANGED
            continue
        if record.status != STATUS_DONE:
            stale[plan.block_signature] = STALE_NOT_DONE
            continue
        done[plan.block_signature] = record

    # 磁盘上有、但当前计划里没有的块：不是"失效"，是"不再需要"
    return Classified(done=done, stale=stale, missing=missing)
