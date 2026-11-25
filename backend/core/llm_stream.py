"""流式协议数据结构与解析器
流式调用的数据结构（StreamChunk/StreamResult）与 OpenAI/Anthropic 协议的
事件/响应解析纯函数。LLMClient 的 chat/chat_stream 经导入使用；
单测直接对模块函数做单元解析。
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple

# 协议层只向 error_types 单向依赖。流式块与终结结果都必须能携带
# 完整判定契约（含 scope / retry_after），故 verdict 用 error_types.ErrorVerdict；
# 本模块不得反向 import llm_client 或 error_taxonomy。
from .error_types import ErrorVerdict


# 流式 LLM 调用数据结构
@dataclass
class StreamChunk:
    """流式响应的一个 chunk

    字段说明：
    - type: "content" | "reasoning" | "usage" | "error"
    - text: type=content 时的增量文本
    - reasoning_text: type=reasoning 时的思考链增量（M2.7 等思考型模型）
    - estimated_total_tokens: 累计输出 token 估算（字符数 / 4，含 reasoning）
      **重要**：在 chat_stream_with_retry 内部由调用方维护累计值，传入时
      已是累计值，前端直接用作动画的 target_value
    - usage_*: type=usage 时的真实 usage（OpenAI 最后一个 chunk、Anthropic 每 chunk）
    - is_final: 是否最后一个 chunk（用于结束标志）
    - error: type=error 时的错误消息
    - verdict: type=error 时的完整判定契约。"流式块必须携带完整
      契约（含 scope 与 retry_after）"，不允许只在终结结果上补齐——错误块带着
      cls/confidence/action/scope/retry_after 一起外传，重试层与上层消费方只读它。
      其余类型的块（content/reasoning/usage）为 None。
    """
    type: str = ""
    text: str = ""
    reasoning_text: str = ""
    estimated_total_tokens: int = 0
    usage_prompt_tokens: int = 0
    usage_completion_tokens: int = 0
    usage_cached_tokens: int = 0
    is_final: bool = False
    is_thinking: bool = False
    error: str = ""
    verdict: Optional[ErrorVerdict] = None
    finish_reason: str = ""


def estimate_token_count(text: str) -> int:
    """快速估算文本 token 数：
    - 中文/全角标点：1 字符 ≈ 1.2 tokens
    - 英文/ASCII：1 token ≈ 4 字符（满足既有英文测试合同）
    """
    if not text:
        return 0
    cjk_count = sum(
        1 for c in text
        if '\u4e00' <= c <= '\u9fff'
        or '\u3000' <= c <= '\u303f'
        or '\uff01' <= c <= '\uffee'
        or '\u201c' <= c <= '\u2026'
    )
    ascii_count = len(text) - cjk_count
    return int(cjk_count * 1.2 + ascii_count / 4.0)


@dataclass
class StreamResult:
    """流式调用结束后的最终结果（与 chat() 返回值字段对齐）"""
    success: bool
    content: str
    error: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    reasoning_chars: int  # 思考链总字符数（被剥离的）
    call_stats: dict = field(default_factory=dict)  # {"attempts": N, "failed_tokens": M}
    # 失败时的完整判定契约（cls/confidence/action/scope/retry_after 俱全），
    # 由 chat_stream 在异常现场判定后随结果外传；成功恒为 None（与 LLMResult 口径一致）。
    verdict: Optional[ErrorVerdict] = None
    finish_reason: str = ""


def parse_anthropic_response(response) -> Tuple[str, int, int, int]:
    """Anthropic 响应解析：拼接 text block，丢弃 thinking block。
    返回 (content, input_tokens, output_tokens, cache_read_tokens)"""
    text_parts = []
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
    content = "".join(text_parts)
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "input_tokens", 0) or 0
    completion_tokens = getattr(usage, "output_tokens", 0) or 0
    cached_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
    return content, prompt_tokens, completion_tokens, cached_tokens


def parse_openai_stream_event(event) -> Optional[StreamChunk]:
    """OpenAI 流式事件 → StreamChunk

    事件类型：
    - 有 choices[0].delta.content：内容 chunk
    - 有 choices[0].delta.reasoning_content：思考链 chunk（M2.7 等）
    - 无 choices 但有 usage：usage-only chunk（最后一个，stream_options=include_usage 才有）
    """
    if not getattr(event, "choices", None):
        if getattr(event, "usage", None):
            u = event.usage
            cached = 0
            ptd = getattr(u, "prompt_tokens_details", None)
            if ptd is not None:
                raw_cached = ptd.get("cached_tokens") if isinstance(ptd, dict) else getattr(ptd, "cached_tokens", None)
                if isinstance(raw_cached, (int, float)) and not isinstance(raw_cached, bool):
                    cached = int(raw_cached)
            if not cached:
                raw_hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
                if isinstance(raw_hit, (int, float)) and not isinstance(raw_hit, bool):
                    cached = int(raw_hit)
            return StreamChunk(
                type="usage",
                usage_prompt_tokens=u.prompt_tokens or 0,
                usage_completion_tokens=u.completion_tokens or 0,
                usage_cached_tokens=cached,
                is_final=True,
            )
        return None

    choice = event.choices[0]
    raw_fr = getattr(choice, "finish_reason", None)
    fr = raw_fr if isinstance(raw_fr, str) else ""
    delta = getattr(choice, "delta", None)
    if not delta:
        if fr:
            return StreamChunk(type="finish", finish_reason=fr, is_final=True)
        return None

    raw_text = getattr(delta, "content", "")
    text = raw_text if isinstance(raw_text, str) else ""

    raw_reasoning = getattr(delta, "reasoning_content", "")
    reasoning = raw_reasoning if isinstance(raw_reasoning, str) else ""

    if not reasoning:
        details = getattr(delta, "reasoning_details", None)
        if isinstance(details, str):
            reasoning = details
        elif isinstance(details, list):
            reasoning = "".join(
                str(d.get("text", "") if isinstance(d, dict) else d)
                for d in details
                if isinstance(d, (str, dict))
            )
        elif isinstance(details, dict):
            reasoning = str(details.get("text", "") or "")

    if not reasoning:
        raw_r = getattr(delta, "reasoning", "")
        if isinstance(raw_r, str):
            reasoning = raw_r

    if reasoning:
        return StreamChunk(
            type="reasoning",
            reasoning_text=reasoning,
            is_thinking=True,
            finish_reason=fr,
        )
    if text:
        return StreamChunk(type="content", text=text, is_thinking=False, finish_reason=fr)
    if fr:
        return StreamChunk(type="finish", finish_reason=fr, is_final=True)
    return None


def parse_anthropic_stream_event(event) -> Optional[StreamChunk]:
    """Anthropic 流式事件 → StreamChunk

    事件类型：
    - content_block_delta(type=text)：内容 chunk
    - content_block_delta(type=thinking)：思考链 chunk
    - message_delta.usage：增量 usage（每 chunk 都有）及 stop_reason
    """
    etype = getattr(event, "type", "")
    if etype == "content_block_delta":
        delta = getattr(event, "delta", None)
        if not delta:
            return None
        dtype = getattr(delta, "type", "")
        if dtype == "text":
            return StreamChunk(type="content", text=getattr(delta, "text", "") or "", is_thinking=False)
        if dtype == "thinking":
            return StreamChunk(type="reasoning", reasoning_text=getattr(delta, "thinking", "") or "", is_thinking=True)
    elif etype == "message_delta":
        delta = getattr(event, "delta", None)
        raw_sr = getattr(delta, "stop_reason", None) if delta else None
        fr = raw_sr if isinstance(raw_sr, str) else ""
        usage = getattr(event, "usage", None)
        if usage:
            return StreamChunk(
                type="usage",
                usage_completion_tokens=getattr(usage, "output_tokens", 0) or 0,
                finish_reason=fr,
                is_final=False,
            )
        if fr:
            return StreamChunk(type="finish", finish_reason=fr, is_final=True)
    return None
