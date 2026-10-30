"""块结果落盘与新鲜度判定（PLAN v2 §2.4 / §3.4 / §4 S2-6）。

协议在这一版按契约收紧，三处与原实现不同——都是为了"结果算不算数"只有一个答案：

1. **文件名是 ``result_id``，不是块签名**。同一条链可以针对同一块产出多个结果
   （换模型、换 Prompt 策略），块签名会让后写的覆盖先写的，历史版本凭空消失。
2. **状态是 ``SUCCEEDED/FAILED/SKIPPED/CANCELLED/BLOCKED``**，与任务状态同词表。
   小写 ``done`` 是另一套词表，混用必然在边界出错。
3. **只有登记在 ``manifest.active_results`` 里的结果才算数**。"文件存在"不等于
   "这是当前有效结果"：换源、重算、回滚都会让文件留在磁盘上但失去登记。

判定顺序：登记 → 状态 → 源章节是否仍一致 → （可选）依赖指纹。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .artifacts import read_json_artifact, stamp_payload, write_json_atomic
from .consistency import PublishOutcome, orphan_files, publish_atomically
from .blocks import BlockPlan
from .constants import ErrorCode, VnextFormatError
from .fingerprint import dependency_fingerprint as _fingerprint_dependencies
from .fingerprint import result_id as compute_result_id
from .project import Manifest, commit_manifest, load_manifest

RESULT_DIR_NAME = "results"

#: "上下文怎么拼"的版本（前情取哪些块、截断多少字符）。与 Prompt 措辞版本分开演进。
CONTEXT_STRATEGY_VERSION = 1

#: 结果状态（与 §3.4 / §3.6 同词表）。``BLOCKED`` 是"账户级错误导致整本停摆"，
#: 与 ``FAILED`` 的区别是有没有人需要动手。
ST_SUCCEEDED = "SUCCEEDED"
ST_FAILED = "FAILED"
ST_SKIPPED = "SKIPPED"
ST_CANCELLED = "CANCELLED"
ST_BLOCKED = "BLOCKED"
RESULT_STATUSES = frozenset({ST_SUCCEEDED, ST_FAILED, ST_SKIPPED, ST_CANCELLED, ST_BLOCKED})

#: 失效原因。``STALE_*`` 不是写进文件的状态，而是读出来后的结论。
STALE_BLOCK_CHANGED = "BLOCK_SIGNATURE_MISMATCH"
STALE_NOT_DONE = "NOT_DONE"
STALE_SOURCE_MISMATCH = "SOURCE_CHAPTERS_MISMATCH"
STALE_DEPENDENCY = "DEPENDENCY_CHANGED"
STALE_MISSING_BLOCK = "BLOCK_MISSING"

MISSING_NOT_REGISTERED = "NOT_REGISTERED"
MISSING_FILE_ABSENT = "FILE_ABSENT"


def results_dir(book_dir: Path) -> Path:
    return Path(book_dir) / RESULT_DIR_NAME


def result_path(book_dir: Path, result_id: str) -> Path:
    return results_dir(book_dir) / f"{result_id}.json"


@dataclass(frozen=True)
class ResultRecord:
    """一条块结果。字段与契约 §3.4 一一对应。"""

    result_id: str
    block_signature: str
    source_chapters: tuple[tuple[str, str], ...]  # ((chapter_id, content_hash), …)
    status: str
    model_fingerprint: str = ""
    prompt_strategy_version: int = 1
    dependency_fingerprint: str = ""
    extracted: dict = field(default_factory=dict)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_status: str = "UNKNOWN"
    error: str | None = None
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return stamp_payload(
            {
                "result_id": self.result_id,
                "block_signature": self.block_signature,
                "source_chapters": [[cid, chash] for cid, chash in self.source_chapters],
                "status": self.status,
                "model_fingerprint": self.model_fingerprint,
                "prompt_strategy_version": self.prompt_strategy_version,
                "dependency_fingerprint": self.dependency_fingerprint,
                "extracted": self.extracted,
                # 未知用量写 null + UNKNOWN：写 0 会让"不知道"变成"花了 0 元"
                "usage": {
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "usage_status": self.usage_status,
                },
                "error": self.error,
                "created_at": self.created_at or time.time(),
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping) -> "ResultRecord":
        for key in ("result_id", "block_signature", "source_chapters", "status"):
            if key not in data:
                raise VnextFormatError(
                    ErrorCode.MISSING_REQUIRED_FIELD, f"结果记录缺字段：{key}"
                )
        status = str(data["status"])
        if status not in RESULT_STATUSES:
            raise VnextFormatError(
                ErrorCode.INVALID_FIELD_VALUE,
                f"非法的结果状态：{status}（只接受 {'/'.join(sorted(RESULT_STATUSES))}）",
            )
        raw_chapters = data["source_chapters"]
        if not isinstance(raw_chapters, list):
            raise VnextFormatError(
                ErrorCode.INVALID_FIELD_VALUE, "source_chapters 必须是数组"
            )
        chapters: list[tuple[str, str]] = []
        for item in raw_chapters:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise VnextFormatError(
                    ErrorCode.INVALID_FIELD_VALUE, f"source_chapters 元素形状非法：{item!r}"
                )
            chapters.append((str(item[0]), str(item[1])))
        usage = data.get("usage") or {}
        if not isinstance(usage, Mapping):
            raise VnextFormatError(ErrorCode.INVALID_FIELD_VALUE, "usage 必须是对象")
        return cls(
            result_id=str(data["result_id"]),
            block_signature=str(data["block_signature"]),
            source_chapters=tuple(chapters),
            status=status,
            model_fingerprint=str(data.get("model_fingerprint") or ""),
            prompt_strategy_version=int(data.get("prompt_strategy_version") or 1),
            dependency_fingerprint=str(data.get("dependency_fingerprint") or ""),
            extracted=dict(data.get("extracted") or {}),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            usage_status=str(usage.get("usage_status") or "UNKNOWN"),
            error=data.get("error"),
            created_at=float(data.get("created_at") or 0.0),
        )


def make_result_id(
    *,
    block_signature: str,
    prompt_strategy_version: int,
    model_fingerprint: str,
    dependency_fingerprint: str,
) -> str:
    """按契约算 ``result_id``（与 :mod:`backend.vnext.fingerprint` 同源）。"""
    return compute_result_id(
        block_sig=block_signature,
        prompt_strategy_version=prompt_strategy_version,
        model_fingerprint=model_fingerprint,
        dependency_fp=dependency_fingerprint,
    )


def write_result(book_dir: Path, record: ResultRecord) -> Path:
    """只写文件，不动清单。要"生效"请用 :func:`publish_result`。

    分开的理由：写文件是原子的，登记清单也是原子的，但两者不能同时原子。把
    "写"与"发布"分成两步，失败时可重试而不产生"文件在、清单没登记"之外的中间态，
    而那种中间态恰好会被 :func:`classify` 判成无效——安全侧。
    """
    if record.status not in RESULT_STATUSES:
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE, f"不能写入状态：{record.status}"
        )
    path = result_path(book_dir, record.result_id)
    write_json_atomic(path, record.to_dict())
    return path


class PublishFailed(RuntimeError):
    """发布未确认成功。``outcome`` 里带着"到底哪一步、数据现在一致不一致"。"""

    def __init__(self, outcome: PublishOutcome) -> None:
        super().__init__(outcome.error or "发布未确认")
        self.outcome = outcome


def publish_result(book_dir: Path, record: ResultRecord) -> Manifest:
    """写文件并登记进 ``manifest.active_results``，返回**提交后的新清单**。

    返回值是重点：``commit_manifest`` 在锁内重新读了一份清单来改，调用方手里那份
    是发布之前的快照。拿旧快照去 :func:`classify` 会看到空的 ``active_results``，
    于是刚刚发布的结果被判成"没登记"——这个坑排查起来很像"结果丢了"。
    """
    def _mutate(manifest: Manifest) -> None:
        kept: list[str] = []
        for rid in manifest.active_results:
            if rid == record.result_id:
                continue
            other = _read_result_raw(book_dir, rid)
            if other is not None and other.block_signature == record.block_signature:
                continue  # 同块的旧版本：让位
            kept.append(rid)
        kept.append(record.result_id)
        manifest.active_results = kept

    def _verify() -> bool:
        """登记是否成立——读**持久化状态**，不看上面那个回调有没有正常返回。

        回调完全可能在"已经写进 manifest"之后才抛错（比如随后的日志写入失败）。
        那时文件已经被引用，再删就是删真数据。
        """
        try:
            current = load_manifest(book_dir)
        except VnextFormatError:
            return False
        return record.result_id in current.active_results

    outcome = publish_atomically(
        write_file=lambda: write_result(book_dir, record),
        commit=lambda: commit_manifest(book_dir, _mutate),
        verify_commit=_verify,
        remove_file=lambda: result_path(book_dir, record.result_id).unlink(missing_ok=True),
    )
    if not outcome.published:
        # 未确认发布：把原因带出去，让调用方决定重试还是记失败。
        # 注意 outcome.committed 为真时数据其实是一致的（已登记），只是流程上有异常。
        raise PublishFailed(outcome)

    return load_manifest(book_dir)


def _read_result_raw(book_dir: Path, result_id: str) -> ResultRecord | None:
    path = result_path(book_dir, result_id)
    if not path.is_file():
        return None
    try:
        return ResultRecord.from_dict(dict(read_json_artifact(path)))
    except VnextFormatError:
        return None


def scan_orphans(book_dir: Path) -> list[Path]:
    """目录里有、但没人引用的结果文件。

    这是 :func:`consistency.orphan_files` 的生产用法：发布时"提交状态不明就保留文件"
    会留下孤儿，启动扫描把它们找出来——孤儿可回收，误删不可逆。
    """
    try:
        manifest = load_manifest(book_dir)
    except VnextFormatError:
        return []
    return orphan_files(results_dir(book_dir), known_ids=set(manifest.active_results))


def list_results(book_dir: Path) -> dict[str, ResultRecord]:
    """按 ``result_id`` 索引全部结果文件。坏文件跳过——单个坏文件不该让整本书打不开。"""
    out: dict[str, ResultRecord] = {}
    directory = results_dir(book_dir)
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            record = ResultRecord.from_dict(dict(read_json_artifact(path)))
        except VnextFormatError:
            continue
        if path.stem != record.result_id:
            # 文件名与内部 ID 不一致：不认（否则改名就能让旧结果"复活"）
            continue
        out[record.result_id] = record
    return out


@dataclass
class Classified:
    """把"磁盘上有什么"翻译成"哪些块算完成、哪些要重算"。"""

    done: dict[str, ResultRecord]
    stale: dict[str, str]          # block_signature -> 原因
    missing: dict[str, str]        # block_signature -> 原因（当前无有效结果）
    #: 登记在册但文件不在的结果 ID —— "跑过但结果没了"，与"从没跑过"不同
    orphans: list[str] = field(default_factory=list)
    #: 文件在但未登记的结果 ID —— 通常是别的源留下的旧版本，不算错，但值得知道
    unregistered: list[str] = field(default_factory=list)


def _newest(records: Sequence[ResultRecord]) -> ResultRecord | None:
    return max(records, key=lambda r: r.created_at) if records else None


@dataclass(frozen=True)
class ResultStrategy:
    """产出结果时用的"条件"。三者任一变化，旧结果就不该再被当作当前结论。

    ``context_strategy_version`` 是"上下文怎么拼"的版本（前情取哪些、截断多少）。
    它与 ``prompt_strategy_version`` 分开，是因为它们会独立演进：改 Prompt 措辞
    和改"给模型看哪几章前情"是两件事，合成一个版本号会让任一方变动都白跑全库。
    """

    prompt_strategy_version: int = 1
    model_fingerprint: str = ""
    context_strategy_version: int = CONTEXT_STRATEGY_VERSION


def expected_dependency(
    prior_result_ids: Sequence[str | None], strategy: ResultStrategy
) -> str:
    """算"这一块现在应该依赖什么"。

    ``prior_result_ids`` 是**按顺序**的、此前各块当前有效的结果 ID（未完成的位置是
    ``None``）。用位置而不是"已完成集合"，是为了让"前面换了一块"精确地传导下去：
    第 3 块换了结果，第 4、5 块必失效，而第 1、2 块不受影响。
    """
    return _fingerprint_dependencies(
        {
            "prior_results": list(prior_result_ids),
            "context_strategy_version": strategy.context_strategy_version,
            "prompt_strategy_version": strategy.prompt_strategy_version,
            "model_fingerprint": strategy.model_fingerprint,
        }
    )


def classify(
    manifest: Manifest,
    plans: list[BlockPlan],
    records: Mapping[str, ResultRecord] | None = None,
    *,
    strategy: ResultStrategy | None = None,
    dependency_fingerprint: str | None = None,
) -> Classified:
    """判定每个计划块的状态。

    规则（顺序固定，任一不过即失效）：

    1. **必须被登记**：``result_id ∈ manifest.active_results``。没登记的可能是
       别的源留下的，也可能是写的中间态，都不算。
    2. **状态必须 SUCCEEDED**：FAILED/SKIPPED/CANCELLED/BLOCKED 都不是"完成"。
    3. **送进去的章节必须还是这几章、这几个哈希**：否则这结果回答的是另一个问题。
    4. **依赖必须仍是当前依赖**：给了 ``strategy`` 就按"前面各块的结果 ID"逐块滚动
       计算期望依赖。前面任何一块换了结果、或 Prompt/模型/上下文策略变了，它之后
       的块都会失效——而它之前的块不受影响。这就是"局部失效"的准确边界。

    只按块签名判断会漏掉跨块依赖：某块读了前情摘要，前情变了它却"签名没变"，
    于是被当成还能用——那正是"看起来跑完了、实际是旧结论"的来源。
    """
    if records is None:
        records = {}
    active = set(manifest.active_results)
    hash_by_chapter = {c.chapter_id: c.content_hash for c in manifest.chapters}
    by_block: dict[str, list[ResultRecord]] = {}
    for record in records.values():
        if record.result_id in active:
            by_block.setdefault(record.block_signature, []).append(record)

    done: dict[str, ResultRecord] = {}
    stale: dict[str, str] = {}
    missing: dict[str, str] = {}
    prior: list[str | None] = []

    for plan in plans:
        expected_source = tuple(
            (cid, hash_by_chapter.get(cid, "")) for cid in plan.chapter_ids
        )
        if strategy is not None:
            expected_dep = expected_dependency(prior, strategy)
        else:
            expected_dep = dependency_fingerprint

        candidates = by_block.get(plan.block_signature, [])
        record = _newest(candidates)
        reason: str | None = None
        if record is None:
            # "当前没有有效结果"。至于"登记了但文件没了"与"文件在但没登记"，
            # 都无法归因到某个具体块（文件不在就不知道它属于哪块），因此在下面按
            # result_id 单列，不硬塞进块的结论里。
            missing[plan.block_signature] = MISSING_NOT_REGISTERED
        elif record.status != ST_SUCCEEDED:
            reason = f"{STALE_NOT_DONE}:{record.status}"
        elif record.source_chapters != expected_source:
            reason = STALE_SOURCE_MISMATCH
        elif expected_dep is not None and record.dependency_fingerprint != expected_dep:
            reason = STALE_DEPENDENCY

        if reason is not None:
            stale[plan.block_signature] = reason
            prior.append(None)  # 这一块没有当前有效结果，后面的块依赖也就断了
            continue

        if record is not None:
            done[plan.block_signature] = record
            prior.append(record.result_id)
        else:
            prior.append(None)

    return Classified(
        done=done,
        stale=stale,
        missing=missing,
        orphans=[rid for rid in manifest.active_results if rid not in records],
        unregistered=[rid for rid in records if rid not in active],
    )
