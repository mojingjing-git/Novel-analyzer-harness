"""写入一致性：两件必须一起发生的事，不能只发生一件（PLAN v2 §4 S8 C-09）。

发布一个结果包含两步：**写结果文件** 与 **登记进 manifest**。这两步之间任何一处崩溃，
都会留下"看起来完成"的假象：

* 文件写了、登记没成 → 目录里躺着一个谁都不引用的大文件（占用空间，且下次扫描会当成孤儿）；
* 登记成了、文件没写 → 进度显示完成，真要读内容时才发现没有（这是最坏的一种，因为它骗人）。

本模块把这两步包成一个**可注入故障**的操作，让上面两种情形都能被测试稳定复现，
而不是靠"运气不好才发现"。故障点名字故意写成"业务语言"（before_write/after_write/…），
因为读测试的人需要一眼看出"崩在哪一步"。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

# 故障注入点
BEFORE_WRITE = "before_write"
AFTER_WRITE = "after_write"
BEFORE_COMMIT = "before_commit"
AFTER_COMMIT = "after_commit"

POINTS = (BEFORE_WRITE, AFTER_WRITE, BEFORE_COMMIT, AFTER_COMMIT)


class FaultHook(Protocol):
    def __call__(self, point: str) -> None: ...


@dataclass
class PublishOutcome:
    """一次发布的结果。``published=False`` 时 ``cleanup_done`` 说明有没有留下垃圾。"""

    published: bool
    wrote_file: bool = False
    committed: bool = False
    cleanup_done: bool = False
    error: str = ""


def publish_atomically(
    *,
    write_file: Callable[[], None],
    commit: Callable[[], None],
    remove_file: Callable[[], None] | None = None,
    fault: FaultHook | None = None,
) -> PublishOutcome:
    """先写文件、再登记；任一步失败都要回到"两件都没成"的状态。

    补偿动作（``remove_file``）必须提供：文件已写但登记失败时，若不删掉，
    下次扫描会把它当成"孤儿结果"。孤儿比失败更麻烦——它看起来像一次成功。
    """
    outcome = PublishOutcome(published=False)

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
    except Exception as exc:  # noqa: BLE001 - 任何失败都按"没发布成功"处理
        outcome.error = f"{type(exc).__name__}: {exc}"
        if outcome.wrote_file and not outcome.committed and remove_file is not None:
            try:
                remove_file()
                outcome.cleanup_done = True
            except OSError:
                # 删不掉也要如实记录：留下的是"孤儿文件"，需要在扫描时被识别
                outcome.cleanup_done = False
        return outcome

    outcome.published = True
    return outcome


def orphan_files(results_dir: Path, known_ids: set[str]) -> list[Path]:
    """目录里有、但没人引用的结果文件。用来在启动时提示或清理。"""
    results_dir = Path(results_dir)
    if not results_dir.is_dir():
        return []
    return sorted(
        path
        for path in results_dir.glob("*.json")
        if path.stem not in known_ids
    )
