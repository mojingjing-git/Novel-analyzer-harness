"""流式 Token 进度回调工厂（三条生产路径的共用主体）

只收敛 ``StreamChunk -> ProgressHub.broadcast_token_delta`` 这一段，
不接管 LLM 重试、并发槽与计费统计。

契约：

- 只对 ``content`` / ``usage`` 广播，``reasoning`` / ``error`` 不新增广播；
- 创建时启动独立的 ``time.monotonic()`` 计时，调用间互不共享，重试不重置；
- ``output_tokens`` 取 ``estimated_total_tokens or 0``（累计值由 LLMClient 维护，
  此处不二次累加），速率为 ``output_tokens / max(elapsed, 0.1)``。

``context`` / ``session_id`` / ``unit_idx`` 由调用方按自身来源传入：
章节为 ``chapter_number``、总结各阶段为 ``batch_idx``、风格固定 0；
``session_id`` 缺失时回退 ``"unknown"``。
"""

import time as _time

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from ..progress_hub import ProgressHub
    from .llm_stream import StreamChunk


BROADCAST_CHUNK_TYPES = ("content", "usage")


def make_stream_progress_callback(
    hub: "ProgressHub",
    context: str,
    session_id: str,
    unit_idx: int = 0,
):
    """创建把 StreamChunk 转成 token_delta 广播的异步回调。

    计时在创建时启动，语义等同原三处 ``started_at = time.monotonic()`` 的位置。
    """
    started_at = _time.monotonic()

    async def on_progress(chunk: "StreamChunk") -> None:
        if chunk.type not in BROADCAST_CHUNK_TYPES:
            return
        elapsed = _time.monotonic() - started_at
        output_tokens = chunk.estimated_total_tokens or 0
        await hub.broadcast_token_delta(
            context=context,
            session_id=session_id or "unknown",
            unit_idx=unit_idx,
            delta={
                "output_tokens": output_tokens,
                "rate_tokens_per_sec": output_tokens / max(elapsed, 0.1),
                "elapsed_sec": elapsed,
            },
        )

    return on_progress
