"""交互保护：取消不丢账、不把工作线程留在半空（PLAN v2 §4 S8 C-16/C-18）。

后台线程里跑的活有个反直觉的性质：**取消等待方不会取消工作线程**。于是

* 若在 `await` 被取消时就结算预算/释放预留，实际仍在跑的请求会变成"没记账的消耗"；
* 若把"任务结束"写在 `finally` 里但 `finally` 在取消时立刻执行，结论会被记成
  `CANCELLED` 而线程后来其实成功写盘了 —— 账本与磁盘对不上。

本模块只做一件事：把"工作线程真正结束"作为记账与状态转换的**唯一触发点**。
取消只影响"还要不要等"，不影响"已经发生的怎么记"。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

T = TypeVar("T")

FINISHED = "FINISHED"
ABANDONED = "ABANDONED"


@dataclass
class ThreadJob(Generic[T]):
    """一个提交给线程池的活。``result`` 在线程真正结束后才可读。"""

    future: "asyncio.Future[T]"
    cancelled_waiter: bool = False

    @property
    def done(self) -> bool:
        return self.future.done()


async def run_thread_job(
    fn: Callable[[], T],
    *,
    on_finish: Callable[[T | None, BaseException | None], None] | None = None,
    shield: bool = True,
) -> ThreadJob[T]:
    """在默认执行器里跑 ``fn``，并保证 ``on_finish`` 在线程**真正结束**后才被调用。

    ``shield=True`` 时取消**等待方**不会打断登记：``on_finish`` 仍会在随后到达。
    这正是"取消要能停下游、但不能丢账"的实现方式——账由线程结束触发，不由取消触发。
    """
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[T]" = loop.create_future()

    def _resolve(result: T | None, exc: BaseException | None) -> None:
        # 线程侧回调：先记账，再唤醒等待方。顺序反了会出现"await 返回时账还没记"。
        if on_finish is not None:
            on_finish(result, exc)
        if future.done():
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)  # type: ignore[arg-type]

    async def _worker() -> None:
        try:
            result = await loop.run_in_executor(None, fn)
        except BaseException as exc:  # noqa: BLE001 - 线程侧的异常要原样交给等待方
            loop.call_soon_threadsafe(_resolve, None, exc)
            return
        loop.call_soon_threadsafe(_resolve, result, None)

    task = asyncio.ensure_future(_worker())
    job: ThreadJob[T] = ThreadJob(future=future)

    try:
        if shield:
            await asyncio.shield(task)
        else:
            await task
    except asyncio.CancelledError:
        job.cancelled_waiter = True
        # 不 cancel task：线程还会跑完，记账由它自己触发
        raise

    # 线程侧的异常已经交给 future；这里把它抛给等待方，保持"调用即抛"的直觉。
    # on_finish 在抛之前就已执行（顺序在 _resolve 里固定），所以记账不会丢。
    if future.done() and future.exception() is not None:
        raise future.exception()  # type: ignore[misc]

    return job


@dataclass
class LockGuard:
    """记录"是否在持锁时 await 过"。仅用于测试与断言，运行时不拦。"""

    violations: list[str] = field(default_factory=list)

    def check(self, *, holding: bool, awaiting: bool, where: str = "") -> None:
        if holding and awaiting:
            self.violations.append(where or "unknown")
