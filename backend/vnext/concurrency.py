"""交互保护：结算只认"线程真的结束了"，不认"等待方被取消"（PLAN v2 §4 S8 C-16/C-18）。

复审三第 3、4 条指出的两个坑，都源于把结算挂在事件循环的回调上：

1. **取消等待方 ≠ 线程结束**。`asyncio.to_thread` 取消 `await` 不会取消线程；如果结算挂在
   循环回调里，取消后再关循环，回调要么不来、要么提前带着 `CancelledError` 到达，
   于是"还没跑完的活"被当成"已结算"。
2. **结算回调自己抛错**会留下永远 pending 的 Future，等待方只能超时，而且没人知道为什么。

因此这里把结算放进**工作线程自己的 `finally`**：不依赖事件循环是否还活着、是否被取消。
循环只负责"把结果交回等待方"这一件事，交不回去也不影响账目。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass
class ThreadJob(Generic[T]):
    """一个提交给线程池的活。

    ``finished`` 由线程侧置位（在结算之后），所以 ``finished=True`` 蕴含"账已经记完"。
    ``settle_error`` 非空表示结算回调自己失败了——这种情况仍然算"线程结束"，
    但调用方必须知道账没记成（否则就是"以为记了"）。
    """

    future: "asyncio.Future[T]"
    cancelled_waiter: bool = False
    finished: bool = False
    settle_error: str = ""
    payload: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.future.done()


async def run_thread_job(
    fn: Callable[[], T],
    *,
    on_finish: Callable[[T | None, BaseException | None], None] | None = None,
    shield: bool = True,
) -> ThreadJob[T]:
    """在线程池执行 ``fn``，返回 :class:`ThreadJob`。

    ``on_finish`` 在**线程内部**、``fn`` 结束后立刻执行（成功或异常都会执行）。因此：

    * 等待方被取消 → 结算照常发生（线程跑完就记）；
    * 事件循环被关闭 → 结算仍照常发生（不经过循环）；
    * ``on_finish`` 抛错 → 记进 ``settle_error``，不会让 Future 永久挂起。
    """
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[T]" = loop.create_future()
    job: ThreadJob[T] = ThreadJob(future=future)

    def _worker() -> T:
        result: T | None = None
        error: BaseException | None = None
        try:
            result = fn()
            return result
        except BaseException as exc:  # noqa: BLE001 - 线程侧异常原样交给等待方
            error = exc
            raise
        finally:
            # 1) 先结算（线程内，与循环无关）
            if on_finish is not None:
                try:
                    on_finish(result, error)
                except BaseException as settle_exc:  # noqa: BLE001
                    job.settle_error = f"{type(settle_exc).__name__}: {settle_exc}"
            job.finished = True

            # 2) 再把结果交回等待方；循环已关就放弃（账已记完，不影响正确性）
            def _resolve() -> None:
                if future.done():
                    return
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(result)  # type: ignore[arg-type]

            try:
                loop.call_soon_threadsafe(_resolve)
            except RuntimeError:
                pass  # 事件循环已关闭：结算已完成，无需再通知

    try:
        if shield:
            await asyncio.shield(loop.run_in_executor(None, _worker))
        else:
            await loop.run_in_executor(None, _worker)
        await future
    except asyncio.CancelledError:
        job.cancelled_waiter = True
        raise

    if job.settle_error:
        # 线程结束了但账没记成：明确报出去，别让调用方以为记好了
        job.payload["settle_failed"] = True
    return job


async def wait_for_finish(job: ThreadJob[T], *, timeout: float | None = None) -> bool:
    """等待"线程真正结束"（含结算），返回是否结束。

    程序关闭时用它替代"直接取消"：``timeout`` 用完后仍未结束，调用方应把该任务记为
    未完成（``CANCELLED``/``FAILED``），而不是假定它成功了。
    """
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while not job.finished:
        if deadline is not None and loop.time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@dataclass
class LockGuard:
    """记录"是否在持锁时 await 过"。仅用于测试与断言，运行时不拦。"""

    violations: list[str] = field(default_factory=list)

    def check(self, *, holding: bool, awaiting: bool, where: str = "") -> None:
        if holding and awaiting:
            self.violations.append(where or "unknown")
