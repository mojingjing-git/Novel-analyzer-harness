"""LLM 错误信号嗅探（provider 无关的纯函数）。

从 core/llm_client.py 收敛而来：权威实现在本包，llm_client 保留同名兼容导出。
新增"错误信号"类判定（账户级熔断、限流识别等）应放在这里，而不是 client 或各引擎里。
"""

from .error_taxonomy import ErrorClass, classify_exception
from .moderation import is_moderation_message

__all__ = ["moderation_hit_from_exception", "is_moderation_message"]


def moderation_hit_from_exception(e) -> bool:
    """从 API 异常嗅探内容审核拦截信号（纯函数，供单测）。

    结构化判定委托 classify_exception。OpenAI SDK 的 APIStatusError.body 是 Any 类型，
    聚合网关可能返回字符串/数组错误体，非 dict body 必须防御性跳过，
    否则 .get("code") 会抛 AttributeError 且无人捕获，导致整批章节零重试直接报废；
    body message 文本兜底同样保留。"""
    verdict = classify_exception(e)
    if verdict is not None:
        return verdict.cls in (ErrorClass.MODERATION_INPUT, ErrorClass.MODERATION_OUTPUT)
    body = getattr(e, 'body', None) or {}
    body_msg = ""
    if isinstance(body, dict):
        err_inner = body.get("error") or {}
        body_msg = str(err_inner.get("message", "")) if isinstance(err_inner, dict) else str(body)
    return is_moderation_message(body_msg) or is_moderation_message(str(e))
