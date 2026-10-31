"""永久错误 vs 一次性失败：这个判定决定"停下"还是"跳过留 gap"。

真书上踩到的形状：MiniMax 对《三侠五义》的某些块回 422 `input new_sensitive (1026)`，
客户端**不抛异常**，而是返回一个空内容的失败结果。若直接拿空串去解析，就会变成
`UNPARSEABLE_ARTIFACT`——永久错误被伪装成格式错误，上层再也分不出来。
"""

from __future__ import annotations

from backend.vnext.analyzer import BlockFailure
from backend.vnext.runner import _is_permanent


def test_explicit_permanent_flag_wins() -> None:
    assert _is_permanent(BlockFailure("审核拦截", permanent=True)) is True


def test_plain_block_failure_is_not_permanent() -> None:
    """没标永久、判定也认不出来：保守当"可能重跑会好"，不跳。"""
    assert _is_permanent(BlockFailure("模型返回空内容（error=未知）")) is False


def test_moderation_text_is_detected_without_structured_verdict() -> None:
    """厂商把拦截码塞在 422 的 body 里，文本通道认不出来——专门嗅探器要兜住。"""
    exc = BlockFailure("API错误 (HTTP 422): {'error': {'message': 'input new_sensitive (1026)'}}")
    assert _is_permanent(exc) is True
