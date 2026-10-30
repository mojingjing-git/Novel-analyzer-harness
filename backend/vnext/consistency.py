"""写入一致性：两件必须一起发生的事，不能只发生一件（PLAN v2 §4 S8 C-09）。

发布一个结果包含两步：**写结果文件** 与 **登记进 manifest**。两步之间任何一处失败，
都会留下"看起来完成"的假象：

* 文件写了、登记没成 → 目录里躺着一个谁都不引用的大文件（占空间，扫描会当成孤儿）；
* 登记成了、文件没写 → 进度显示完成，要读内容时才发现没有（最坏的一种，因为它骗人）。

**关键的判定原则（复审三第 2 条）**：能不能删文件，不能看"commit() 有没有正常返回"，
而要看**持久化状态**里到底有没有这条登记。回调"写完 manifest 之后才抛异常"是完全可能的
（例如提交成功、随后写日志炸了），此时文件已被引用，删掉就是删真数据。

因此本模块的规则是：

1. 提交是否成立，由 ``verify_commit()`` 读持久化状态判定，而不是由异常与否判定；
2. ``verify_commit`` 也没法判定时（没有探针 / 探针自己失败）→ **保留文件**。
   孤儿可以被扫出来、可以回收；被引用的文件删了就没了。两边代价不对等，必须偏保守。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

# 故障注入点（业务语言命名：读测试的人要一眼看出"崩在哪一步"）
BEFORE_WRITE = "before_write"
AFTER_WRITE = "after_write"
BEFORE_COMMIT = "before_commit"
AFTER_COMMIT = "after_commit"

POINTS = (BEFORE_WRITE, AFTER_WRITE, BEFORE_COMMIT, AFTER_COMMIT)


class FaultHook(Protocol):
    def __call__(self, point: str) -> None: ...


@dataclass
class PublishOutcome:
    """一次发布的结果。

    ``published`` 表示"数据处于一致状态且已被引用"；``file_removed`` 表示做了补偿删除。
    ``kept_for_safety`` 为真时说明"提交状态不明，故意保留文件"——这是可接受的结果，
    扫描阶段会把它当孤儿处理。
    """

    published: bool
    wrote_file: bool = False
    committed: bool = False
    file_removed: bool = False
    kept_for_safety: bool = False
    error: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def publish_atomically(
    *,
    write_file: Callable[[], None],
    commit: Callable[[], None],
    verify_commit: Callable[[], bool] | None = None,
    remove_file: Callable[[], None] | None = None,
    fault: FaultHook | None = None,
) -> PublishOutcome:
    """先写文件、再登记；失败时按**持久化状态**决定是否回滚。

    ``verify_commit`` 是"登记到底成没成"的唯一可信判据（通常实现为"重读 manifest 看有没有
    这个 id"）。它缺失或抛错时一律**不删文件**。
    """
    outcome = PublishOutcome(published=False)
    notes: list[str] = []

    def _trip(point: str) -> None:
        if fault is not None:
            fault(point)

    try:
        _trip(BEFORE_WRITE)
        write_file()
        outcome.wrote_file = True
        _trip(AFTER_WRITE)

        _trip(BEFORE_COMMIT)
        commit()
        outcome.committed = True
        _trip(AFTER_COMMIT)
    except Exception as exc:  # noqa: BLE001 - 任何失败都按"没确认发布"处理
        outcome.error = f"{type(exc).__name__}: {exc}"

        # 关键：commit() 抛错 ≠ 没提交。用持久化状态三态复核。
        state = _commit_state(verify_commit, notes)
        if state is True:
            outcome.committed = True
            outcome.published = True          # 数据其实是一致的
            outcome.notes = tuple(notes)
            return outcome

        if outcome.wrote_file and not outcome.committed:
            if remove_file is None:
                outcome.kept_for_safety = True
                notes.append("无法回滚：未提供 remove_file，保留文件待扫描")
            elif state is None:
                # 提交状态**不明** → 保留。孤儿能扫出来，被引用的文件删了就没了。
                outcome.kept_for_safety = True
                notes.append("无法确认登记状态，保留文件（孤儿可回收，误删不可逆）")
            else:
                try:
                    remove_file()
                    outcome.file_removed = True
                except OSError as cleanup_error:
                    outcome.kept_for_safety = True
                    notes.append(f"回滚失败：{type(cleanup_error).__name__}")
        outcome.notes = tuple(notes)
        return outcome

    outcome.published = True
    return outcome


def _commit_state(
    verify_commit: Callable[[], bool] | None, notes: list[str]
) -> bool | None:
    """读持久化状态判断登记是否成立，**三态**：

    * ``True``  —— 已登记（可以当成发布成功）；
    * ``False`` —— 明确没登记（可以安全回滚）；
    * ``None``  —— 不明（探针缺失或自己失败）。调用方必须按"保留文件"处理：
      孤儿可以事后扫出来回收，被引用的文件删掉就再也拿不回来了。
    """
    if verify_commit is None:
        return None
    try:
        return bool(verify_commit())
    except Exception as exc:  # noqa: BLE001 - 探针自己失败也算"不明"
        notes.append(f"提交探针失败：{type(exc).__name__}")
        return None


def orphan_files(results_dir: Path, known_ids: set[str]) -> list[Path]:
    """目录里有、但没人引用的结果文件。启动扫描用它把"不确定"变成"可回收"。"""
    results_dir = Path(results_dir)
    if not results_dir.is_dir():
        return []
    return sorted(
        path
        for path in results_dir.glob("*.json")
        if path.stem not in known_ids
    )
