"""P2：自由文本误判的瞬时网关错误应多获一次温度尝试（阈值 2→3）"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import asyncio

from backend.config.settings import AppConfig
from backend.core.error_types import LLMResult, UsageStatus
from backend.core.llm_client import LLMClient
from backend.core.moderation import mark_moderation

# 真实链路中 chat() 会按自由文本关键词给错误打 [MODERATION] 标记后再返回；
# 这里 mock 掉 chat，因此直接构造带标记的最终形态（等价于生产中 WAF 502 被误判）
BLOCKED = mark_moderation("API错误: Request blocked by WAF")


def _client():
    return LLMClient(AppConfig().api)


def test_transient_blocked_recovers_on_third_call():
    c = _client()
    seq = [LLMResult(False, "", BLOCKED),
           LLMResult(False, "", BLOCKED),
           LLMResult(True, "OK", "", (1, 2), UsageStatus.KNOWN)]
    with patch.object(LLMClient, "chat", new=AsyncMock(side_effect=seq)), \
         patch.object(LLMClient, "_sleep", new=AsyncMock()):
        res = asyncio.run(
            c.chat_with_retry([{"role": "user", "content": "x"}]))
    assert res.success and res.content == "OK"
    assert res.call_stats["attempts"] == 3


def test_persistent_blocked_gives_up_without_backoff():
    c = _client()
    blocked = LLMResult(False, "", BLOCKED)
    with patch.object(LLMClient, "chat", new=AsyncMock(return_value=blocked)), \
         patch.object(LLMClient, "_sleep", new=AsyncMock()) as zzz:
        res = asyncio.run(
            c.chat_with_retry([{"role": "user", "content": "x"}]))
    assert not res.success
    # 第 3 次命中即放弃：不进入指数退避（attempts 停在温度循环上限，无退避轮次）
    assert res.call_stats["attempts"] == 3
    # 只发生温度等待(3s/6s)，没有任何指数退避等待
    assert [call_.args[0] for call_ in zzz.call_args_list] == [3, 6]
