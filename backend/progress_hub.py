"""
进度推送中枢（WebSocket 广播）

统一的进度通道，替代旧项目的 Qt 信号槽。分析任务通过 ProgressHub.publish()
发布消息，所有连接的 WebSocket 客户端都会收到。

**wire 协议的唯一权威定义在 backend/ws_events.py**（WSType/WSState/
PipelineStatus 常量 + payload 契约 + 工厂函数）；本文件只是广播管道，
新增消息类型请先在 ws_events.py 登记。
"""

import asyncio
import logging
import queue as thread_queue
import time
from typing import Any, Dict, Set

from .ws_events import (
    WSType,
    WSState,
    log_event,
    progress_event,
    state_change_event,
    token_delta_event,
)

logger = logging.getLogger(__name__)


class ThrottledBroadcaster:
    """按 channel key 独立节流（150ms）；适合流式 token 增量广播 + 多 block 并发。

    H16 (2026-08-26) P1-8 修订：
    - channel_key 三元组 (context, session_id, unit_idx) 独立桶，不合并最新值
    - 多 block 并发广播不互相覆盖

    设计：emit() 立即调度；同 key 在 MIN_INTERVAL (150ms) 内只发一次；不同 key 独立计时。
    """

    MIN_INTERVAL = 0.15  # 150ms

    def __init__(self, hub: "ProgressHub"):
        self._hub = hub
        self._last_emit: Dict[str, float] = {}
        self._pending_tasks: Dict[str, asyncio.Task] = {}

    async def emit(self, channel_key: str, message: Dict[str, Any]) -> None:
        now = asyncio.get_running_loop().time()
        elapsed = now - self._last_emit.get(channel_key, 0)
        if elapsed >= self.MIN_INTERVAL:
            await self._hub.publish(message)
            self._last_emit[channel_key] = now
        elif channel_key not in self._pending_tasks:
            self._pending_tasks[channel_key] = asyncio.create_task(
                self._delayed_emit(channel_key, message)
            )

    async def _delayed_emit(self, channel_key: str, message: Dict[str, Any]) -> None:
        try:
            await asyncio.sleep(self.MIN_INTERVAL)
            await self._hub.publish(message)
            self._last_emit[channel_key] = asyncio.get_running_loop().time()
        finally:
            self._pending_tasks.pop(channel_key, None)


class ProgressHub:
    """异步进度广播中枢（单用户桌面应用，单例）"""

    def __init__(self) -> None:
        # 每个连接持有一个独立队列，publish 时向所有队列投递
        self._queues: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        # H16 (2026-08-26)：流式 token 增量广播节流器
        self._token_throttler = ThrottledBroadcaster(self)

    async def subscribe(self) -> asyncio.Queue:
        """新连接订阅，返回其专属消息队列"""
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._queues.add(q)
        logger.debug("WS 订阅者接入，当前 %d 个", len(self._queues))
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        """连接断开时注销队列"""
        async with self._lock:
            self._queues.discard(q)
        logger.debug("WS 订阅者断开，当前 %d 个", len(self._queues))

    async def publish(self, message: Dict[str, Any]) -> None:
        """向所有订阅者广播一条消息"""
        async with self._lock:
            targets = list(self._queues)
        for q in targets:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # 慢消费者：分级驱逐而非无脑丢最旧——token_delta 高频可再生先丢，
                # log 次之；block_start/block_done/progress 等低频关键事件保住
                # （前端车道记账靠它们配对，丢一条 = 永久僵尸车道）
                try:
                    items = []
                    while True:  # 排空队列（QueueFull 时必有积压）
                        items.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    pass

                def _droppable_rank(m: Dict[str, Any]) -> int:
                    t = m.get("type")
                    if t == WSType.TOKEN_DELTA:
                        return 0
                    if t == WSType.LOG:
                        return 1
                    return 2

                try:
                    evict_idx = min(range(len(items)), key=lambda i: _droppable_rank(items[i]))
                    del items[evict_idx]
                    for m in items:
                        q.put_nowait(m)
                    q.put_nowait(message)
                except Exception:
                    pass  # 驱逐兜底失败不阻塞发布方


    # ---- 便捷方法 ----
    async def log(self, text: str, level: str = "info", category: str = "") -> None:
        # source="business"：业务事件消息（队列/总结/状态提示等），
        # 与 HubLogHandler 转发的 python 技术日志区分（见 LogConsole 简化模式）
        # category：来源分类（如 summary），前端可据此按页面过滤显示
        await self.publish(log_event(level, text, source="business", category=category))

    async def progress(self, current: int, total: int, eta: str = "") -> None:
        await self.publish(progress_event(current, total, eta))

    async def state_change(self, state: WSState, detail: str = "") -> None:
        await self.publish(state_change_event(state, detail))

    async def broadcast_token_delta(
        self,
        context: str,
        session_id: str,
        unit_idx: int,
        delta: Dict[str, Any],
    ) -> None:
        """流式 token 增量广播（H16）：自动节流 5-10/s

        channel_key 三元组 (context, session_id, unit_idx) 独立桶——
        多 block 并发不互相覆盖；同一 block 的流式 chunk 150ms 内合并。

        Args:
            context: 'analysis' | 'summary_phase_1' | 'summary_phase_2' | 'summary_phase_3' | 'summary_phase_4'
            session_id: 书 ID（用于多本书并发隔离）
            unit_idx: block_idx（分析）或 batch_idx（总结）
            delta: {output_tokens, rate_tokens_per_sec, elapsed_sec, eta_sec?, ...}
        """
        channel_key = f"{context}:{session_id}:{unit_idx}"
        msg = token_delta_event(context, session_id, unit_idx, delta, time.time())
        await self._token_throttler.emit(channel_key, msg)


# 模块级单例
_hub: ProgressHub | None = None


def get_hub() -> ProgressHub:
    global _hub
    if _hub is None:
        _hub = ProgressHub()
    return _hub


# ==================== Python logging → WebSocket 转发 ====================

class HubLogHandler(logging.Handler):
    """
    将 Python logging 输出转发到 ProgressHub（WebSocket 广播）。
    使用线程安全队列，兼容 asyncio.to_thread 内的同步代码。
    """

    def __init__(self):
        super().__init__()
        self._q: thread_queue.Queue = thread_queue.Queue(maxsize=3000)

    def emit(self, record: logging.LogRecord) -> None:
        # 过滤自身和 uvicorn 内部日志，避免循环/噪音
        if record.name.startswith(("backend.progress_hub", "uvicorn", "httpx", "httpcore")):
            return
        try:
            msg = self.format(record)
            level = (
                "error" if record.levelno >= logging.ERROR
                else "warn" if record.levelno >= logging.WARNING
                else "info"
            )
            # source="python"：标识这是 Python logging 转发的"真日志"（技术细节，
            # 含每章 LLM 调用/token 统计），供前端"简化日志"面板过滤掉，
            # 避免与业务消息（hub.log，source="business"）混排重复。
            # category：按 logger 归属打标（最终总结阶段 → "summary"），
            # 供总结页抽屉按分类过滤显示（前端见 SummaryPage summaryLogs）
            category = ""
            if record.name.startswith(("backend.services.final_summary", "backend.services.summary_service")):
                category = "summary"
            self._q.put_nowait(log_event(level, msg, source="python", category=category))
        except thread_queue.Full:
            pass  # 队列满时丢弃，不阻塞业务线程
        except Exception:
            pass


async def _forward_logs(handler: HubLogHandler, hub: ProgressHub) -> None:
    """后台任务：从线程安全队列取日志，转发到 WebSocket（100ms 轮询）"""
    while True:
        drained = 0
        try:
            while drained < 50:  # 每轮最多取 50 条，避免饥饿
                msg = handler._q.get_nowait()
                await hub.publish(msg)
                drained += 1
        except thread_queue.Empty:
            pass
        except Exception:
            pass
        await asyncio.sleep(0.1 if drained == 0 else 0.02)


def install_log_forwarder() -> None:
    """安装日志转发器：在 root logger 上加 HubLogHandler，并启动后台转发任务。

    P1-8：先检查 root logger 是否已挂载同类型 handler——uvicorn --reload 每次
    热重载都会重新执行 app startup，重复挂载会让每条日志向 WS 推 N 份，
    且旧 handler 的线程安全队列常驻内存。
    """
    root = logging.getLogger()
    if any(isinstance(h, HubLogHandler) for h in root.handlers):
        logger.debug("HubLogHandler 已存在，跳过重复挂载")
        return

    handler = HubLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
    handler.setLevel(logging.INFO)
    root.addHandler(handler)

    hub = get_hub()
    asyncio.create_task(_forward_logs(handler, hub))
    logger.info("日志转发器已安装（Python logging → WebSocket）")
