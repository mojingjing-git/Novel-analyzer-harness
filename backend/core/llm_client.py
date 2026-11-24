"""
LLM客户端封装（asyncio 版）
提供带重试机制和温度退火的 OpenAI 兼容 API 调用

行为契约：stop 检查在循环顶部、温度退火 max(0, temp-attempt*step)、
退避 min(2^(retry+1), 60)、认证失败即停、验证失败注入格式修正 hint、failed_tokens 累加
"""

import json
import re
import asyncio
import inspect
import logging
import threading
from dataclasses import replace
from types import SimpleNamespace
from collections import namedtuple
from typing import AsyncIterator, Tuple, Optional, List, Callable, Awaitable
from openai import AsyncOpenAI, APITimeoutError, APIError, AuthenticationError
import anthropic
from anthropic import AsyncAnthropic

from ..config.settings import APIConfig
from ..core.moderation import (
    mark_moderation, is_moderation_message, is_moderation_error,
)
from ..common.llm_signals import (  # noqa: F401  （兼容导出：权威实现在 common）
    moderation_hit_from_exception,
)
from .error_taxonomy import (
    ErrorClass, ClassVerdict,
    classify_exception, classify_message,
)
# ErrorClass 只有一处定义（error_types），error_taxonomy 对它 re-export，
# 两者是同一个类对象；下面仍按 .value 重建，只是为了不依赖导入顺序的显式兜底。
from .error_types import (
    ErrorVerdict, LLMResult, UsageStatus,
    Confidence as _ResConfidence, ErrorClass as _ResErrorClass,
    ErrorScope, SourceChannel,
)
from .llm_failure_logger import _get_failure_logger, redact as _redact
from .breaker import Breaker
from .llm_stream import (
    StreamChunk, StreamResult, estimate_token_count,
    parse_anthropic_response, parse_anthropic_stream_event, parse_openai_stream_event,
)

logger = logging.getLogger(__name__)


def detect_provider(config: APIConfig) -> str:
    """判定请求协议格式：显式 provider 优先，否则按特征自动检测。

    auto 检测规则（按序）：
    1. base_url 含 "anthropic" → anthropic
    2. api_key 以 "sk-ant-" 开头 → anthropic
    3. model 以 "claude-" 开头 → anthropic
    其余 → openai
    """
    if config.provider and config.provider != "auto":
        return "openai" if config.provider == "openai" else "anthropic"
    base = (config.base_url or "").lower()
    key = config.api_key or ""
    model = config.model or ""
    if "anthropic" in base:
        return "anthropic"
    if key.startswith("sk-ant-"):
        return "anthropic"
    if model.lower().startswith("claude-"):
        return "anthropic"
    return "openai"


# FailureLogger 在 llm_failure_logger.py：类 + 进程级单例 + _get_failure_logger 访问器，
# 本文件经导入使用。moderation_hit_from_exception 的实现已下沉 backend/common/llm_signals.py，
# 由上方 import 兼容导出（test_llm_moderation_sniff 等历史路径不变）。


_MODERATION_CLASSES = (ErrorClass.MODERATION_INPUT, ErrorClass.MODERATION_OUTPUT)

# stop 类错误的用户可读提示
_STOP_HINTS = {
    ErrorClass.BILLING: "（API 余额不足，请充值后重试）",
    ErrorClass.CONTEXT_LENGTH: "（本章块超模型上下文，建议调小 analysis.block_size）",
    ErrorClass.BAD_REQUEST: "（请求参数错误，请检查模型名/参数配置）",
}
# stop 类错误在 FailureLogger 里的 error_type（AUTH 沿用原有 RetryNeeded 流程）
_STOP_ERROR_TYPES = {
    ErrorClass.BILLING: "BillingBlocked",
    ErrorClass.CONTEXT_LENGTH: "ContextLengthBlocked",
    ErrorClass.BAD_REQUEST: "BadRequestBlocked",
}

# 单次 attempt 失败后的决策结果（由 LLMClient._decide_retry 统一给出）：
#   action ∈ {"stop", "continue"}；error 为可能被"标记审核 / 追加提示语"改写后的错误串；
#   wait_time 只有温度层有意义（下一次重试的等待秒数），退避层原样回传调用点算好的值；
#   verdict 是该次决断所用的 ClassVerdict，调用点据此把结论搬进结果对象，
#   不再二次分类（分类只发生一次，消费方只读）。
# 决断结论由结果对象自带（LLMResult / StreamResult / StreamChunk 的 verdict 字段），
# 本字段由 _to_class_verdict 从结果对象上的 ErrorVerdict 还原。
RetryDecision = namedtuple(
    "RetryDecision", ("action", "error", "moderation_hits", "wait_time", "verdict")
)

# 单次尝试的统一出口（_retry_chain 骨架与流式/非流式适配器之间的协议）。
# kind:
#   "done"   —— 终态（成功 / 用户取消）：result 携带适配器构造好的返回对象，骨架原样返回
#   "error"  —— API 错误：error/verdict 交骨架走 _decide_retry 决断树
#   "retry_soon" —— 验证失败 / 空响应等"固定语义重试"：不走决断树；适配器已完成
#               记账与失败登记及各自的附加等待（温度层 3*(attempt+1)、退避层无），
#               骨架只更新 last_error 并进入下一轮
_AttemptOutcome = namedtuple(
    "_AttemptOutcome", ("kind", "result", "error", "verdict")
)
_AttemptOutcome.__new__.__defaults__ = (None, "", None)


# 类目 → 作用域（仅对可信通道生效，键为类目字符串值）：
# 账户级才有可能触发熔断；上下文超长/坏请求/审核拦截是请求自身的问题；
# 限流/服务端/网络/超时是暂时性问题。表里没有的（UNKNOWN 等）留 UNKNOWN。
_SCOPE_BY_CLASS = {
    "auth": ErrorScope.ACCOUNT_GLOBAL,
    "billing": ErrorScope.ACCOUNT_GLOBAL,
    "context_length": ErrorScope.REQUEST_LOCAL,
    "bad_request": ErrorScope.REQUEST_LOCAL,
    "moderation_input": ErrorScope.REQUEST_LOCAL,
    "moderation_output": ErrorScope.REQUEST_LOCAL,
    "rate_limit": ErrorScope.TRANSIENT,
    "server": ErrorScope.TRANSIENT,
    "network": ErrorScope.TRANSIENT,
    "timeout": ErrorScope.TRANSIENT,
}

# 置信度 → (跨层置信度, 来源通道)。taxonomy 内部还会细分"异常属性/错误体/状态兜底"，
# 折成三元组后只保留了 structured/text/none，故此处只能按此三档还原。
_CONFIDENCE_MAP = {
    "structured": (_ResConfidence.STRUCTURED, SourceChannel.SDK_ATTRIBUTE),
    "text": (_ResConfidence.TEXT, SourceChannel.TEXT_FALLBACK),
    "none": (_ResConfidence.NONE, SourceChannel.TEXT_FALLBACK),
}


def _extra_body(config) -> dict:
    """OpenAI 兼容请求体里那些"非标准但要透传"的字段。

    两件事合成一个 body：

    - ``thinking_mode``：要不要思考、按各家协议怎么关（OpenAI 系的
      ``enable_thinking``、mimo/GLM 的 ``thinking.type=disabled``）；
    - ``reasoning_effort``：想多深（low/medium/high/xhigh/max）。它是**档位**，
      和"要不要思考"不是一回事——实测 low 与 max 的思考量差约 3 倍。

    走 ``extra_body`` 而不是顶层 kwarg：OpenAI SDK 的 ``create()`` 只认自己
    签名里的参数，写死档位会在这套 SDK 上直接 TypeError。
    """
    body = dict(getattr(config, "thinking_mode", None) or {})
    effort = str(getattr(config, "reasoning_effort", "") or "").strip()
    if effort:
        body.setdefault("reasoning_effort", effort)
    return body


def _to_error_verdict(verdict: Optional[ClassVerdict],
                      error: str = "") -> Optional[ErrorVerdict]:
    """ClassVerdict（策略层三元组）→ ErrorVerdict（跨层传输对象）。

    这是两个类型之间的唯一转换点。转换约定：
    - action 原样搬运：策略函数算好的结论就是它，消费方只读、不重算；
    - scope 只给结构化结论填具体值，文本通道一律 UNKNOWN——若允许按错误串升级，
      "第 401 章 / 赚了 1008 块"这类正文文本就能把整本书判成账户级故障；
    - retry_after 仅在 RATE_LIMIT 时从错误串的 [Retry-After:N] 标记读回；
    - provider_code / http_status 需要 ClassVerdict 额外携带字段，本模块不动策略层，
      先留 None（消费方只做展示，不参与判定）。
    """
    if verdict is None:
        return None
    conf, channel = _CONFIDENCE_MAP.get(
        verdict.confidence, (_ResConfidence.NONE, SourceChannel.TEXT_FALLBACK)
    )
    try:
        cls = _ResErrorClass(verdict.cls.value)
    except ValueError:  # 策略层新增了本模块不认识的类目
        cls = _ResErrorClass.UNKNOWN
    scope = ErrorScope.UNKNOWN
    if conf is _ResConfidence.STRUCTURED:
        scope = _SCOPE_BY_CLASS.get(cls.value, ErrorScope.UNKNOWN)
    retry_after = None
    if cls is _ResErrorClass.RATE_LIMIT:
        m = re.search(r'Retry-After:(\d+)', error or "")
        if m:
            try:
                retry_after = max(1.0, float(m.group(1)))
            except ValueError:
                retry_after = None
    return ErrorVerdict(
        cls=cls,
        confidence=conf,
        source_channel=channel,
        action=verdict.action,
        scope=scope,
        retry_after=retry_after,
        message=error or "",
    )


def _to_class_verdict(verdict: Optional[ErrorVerdict]) -> Optional[ClassVerdict]:
    """ErrorVerdict（结果对象上的传输形态）→ ClassVerdict（策略层三元组）。

    与 _to_error_verdict 互为逆变换，是重试层消费「结果对象自带结论」的唯一入口：
    分类只在异常现场做一次（chat()/chat_stream()），这里只做类型还原，**不重新分类**。
    - 类目：按 .value 重建（两个 str-Enum 成员名/取值相同但身份不同，不能按身份比较），
      本模块不认识的类目降级为 UNKNOWN，交给策略表按暂态处理；
    - 置信度：error_types.Confidence 的取值是大写 "STRUCTURED"/"TEXT"/"NONE"，
      策略层用小写 "structured"/"text"/"none"（is_permanent 按字面量比较），故降级小写；
    - action 原样搬运（含 "stop"）。上游若没给 action，ErrorVerdict 默认为 "backoff"，
      于是纯文本兜底结论绝不会被还原成 stop（方向保守）。
    """
    if verdict is None:
        return None
    try:
        cls = ErrorClass(verdict.cls.value)
    except ValueError:  # 跨层出现了策略层不认识的类目
        cls = ErrorClass.UNKNOWN
    raw_conf = getattr(verdict.confidence, "value", verdict.confidence)
    confidence = str(raw_conf).lower()
    if confidence not in ("structured", "text", "none"):
        confidence = "none"
    return ClassVerdict(cls, confidence, verdict.action)


def _add_tokens(
    acc: Optional[Tuple[int, int]], add: Tuple[int, int]
) -> Tuple[int, int]:
    """累加用量（None 视为 0）。"""
    base = acc if acc is not None else (0, 0)
    return (base[0] + add[0], base[1] + add[1])


def _extract_provider_code(exc) -> Optional[str]:
    """从 SDK 异常里取厂商错误码（envelope 的 error.code / error.type，或响应头）。

    只认"厂商自己给的字段"，不从显示文本里正则捞——那正是误判来源。
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            for key in ("code", "type"):
                value = err.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for key in ("code", "type"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers:
        for name in ("x-error-code", "x-request-error-code"):
            value = headers.get(name)
            if value:
                return str(value).strip()
    return None


def _extract_http_status(exc) -> Optional[int]:
    """从 SDK 异常里取真实 HTTP 状态码。"""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _verdict_from_exception(exc, error_text: str) -> ErrorVerdict:
    """在异常现场构造**完整**判定（provider_code / http_status / 来源通道都在）。

    时机是关键：异常对象一旦被字符串化，厂商码与 HTTP 状态就只剩"文本里有没有那串数字"
    可猜。异常现场一次性把该带的都带上：provider_code / http_status 随结论一并产出，
    来源通道不再退化成 SDK_ATTRIBUTE。
    """
    base = classify_exception(exc) or classify_message(error_text)
    env = _to_error_verdict(base, error_text)
    code = _extract_provider_code(exc)
    status = _extract_http_status(exc)
    channel = env.source_channel
    if code is not None and env.confidence is _ResConfidence.STRUCTURED:
        # 有厂商错误码 ⇒ 这次是"读到了错误信封"，而不是"只看到 SDK 类名"
        channel = SourceChannel.ERROR_ENVELOPE
    env = replace(env, provider_code=code, http_status=status, source_channel=channel)
    # 限流：Retry-After 优先从**响应头**取（那是厂商给的结构化值）；
    # 显示串里的 "[Retry-After:N]" 是为字符串通道兜底注入的标记，
    # 只作兜底，而不是唯一来源。
    if env.cls is _ResErrorClass.RATE_LIMIT and env.retry_after is None:
        raw = _retry_after_from_exception(exc)
        if raw:
            try:
                env = replace(env, retry_after=max(1.0, float(raw)))
            except (TypeError, ValueError):
                pass
    return env


def _as_error_verdict(
    verdict: Optional["ErrorVerdict | ClassVerdict"], error: str = ""
) -> Optional[ErrorVerdict]:
    """把两种判定形态统一成 ``ErrorVerdict``，**已经是就原样返回**。

    已是 ErrorVerdict 的原样返回；只有确实是 ClassVerdict 三元组时才走转换，
    避免结果对象上原本完整的判定（provider_code / http_status / source_channel /
    retry_after）被"按置信度重建"成仿制品：来源从 ERROR_ENVELOPE 变成 SDK_ATTRIBUTE，
    重试等待也跟着退化。
    """
    if verdict is None:
        return None
    if isinstance(verdict, ErrorVerdict):
        return verdict
    return _to_error_verdict(verdict, error)


def _fail_result(error: str, verdict: Optional[ClassVerdict] = None) -> LLMResult:
    """失败结果对象。

    失败路径一律拿不到 usage → tokens=None + usage_status=UNKNOWN，
    **不得**写成 (0, 0)：那会与"真的消耗了 0 token"混为一谈，统计口径失真。
    """
    return LLMResult(
        success=False, content="", error=error,
        verdict=_as_error_verdict(verdict, error),
    )


def _error_action(error: str, moderation_hits: int,
                  verdict: Optional[ClassVerdict] = None) -> Tuple[str, ClassVerdict]:
    """重试循环统一错误决策。返回 (action, verdict)：
    - "moderation_stop"：审核短路（INPUT 确定性 / structured 首次 / text 连续第 3 次）
    - "moderation_count"：审核 text 置信，计入 3 次短路计数器后继续重试
    - "stop"：**仅结构化通道**的永久停止（verdict.is_permanent()：structured 且 stop）
    - "continue"：按退避/Retry-After 继续（文本通道的 AUTH/BILLING 等一律落这里）
    MODERATION_OUTPUT 的"何时停"由本函数的计数器参数表达（连续命中计数），
    策略表只表达"停不停"。

    唯一判据是 verdict.is_permanent()，文本通道（confidence=text/none）永不可能返回 stop：
    若绕过 resolve_action 施加的文本通道上限，纯文本误判也能永久熔断整块。

    verdict 参数：调用方手里若已有结论（异常对象还在场时的 classify_exception），
    可直接传入以免二次文本降级；不传则按 error 文本分类。四条重试路径传入的是
    _to_class_verdict(结果对象.verdict)：结论在 chat()/chat_stream() 的异常现场只算一次，
    随 LLMResult / StreamResult / StreamChunk 外传，错误串一旦字符串化就只剩文本通道。"""
    if verdict is None:
        verdict = classify_message(error)
    if verdict.cls in _MODERATION_CLASSES:
        if (verdict.cls is ErrorClass.MODERATION_INPUT
                or verdict.confidence == "structured"
                or moderation_hits >= 2):
            return "moderation_stop", verdict
        return "moderation_count", verdict
    if verdict.is_permanent():
        return "stop", verdict
    return "continue", verdict


def _retry_after_from_exception(e) -> str:
    """从异常对象提取 Retry-After 响应头。
    返回可注入错误串的字符串（如 "30"），无则空串。"""
    resp = getattr(e, 'response', None)
    if resp is not None and getattr(resp, 'headers', None):
        ra = resp.headers.get('retry-after')
        if ra:
            return str(ra).strip()
    return ""


class LLMClient:
    """OpenAI兼容API客户端（异步）"""

    def __init__(self, config: APIConfig):
        self.config = config
        self.provider = detect_provider(config)
        sdk_max_retries = getattr(config, "sdk_max_retries", 0)
        if getattr(config, "max_retries", None) == 0:
            sdk_max_retries = 0

        if self.provider == "anthropic":
            self.client = None
            self.anthropic = AsyncAnthropic(
                base_url=config.base_url or None,
                api_key=config.api_key if config.api_key else "empty",
                max_retries=sdk_max_retries,
            )
        else:
            self.anthropic = None
            self.client = AsyncOpenAI(
                base_url=config.base_url,
                api_key=config.api_key if config.api_key else "empty",
                max_retries=sdk_max_retries,
            )
        self._stats_lock = threading.Lock()
        self._request_count = 0
        self._attempts = 0
        self._total_tokens = 0
        self._failed_tokens = 0
        self._cached_tokens = 0
        self._stop_requested = False  # 外部可设置，让进行中的重试链尽快退出
        self._stop_event = asyncio.Event()  # 触发后立即取消进行中的 API 请求
        # 账户级熔断：欠费/认证失效等结构化判定的账户级永久错误第一次命中即跳闸，
        # 之后本实例的所有调用入口零成本短路（见 backend/core/breaker.py 的三条边界）
        self._breaker = Breaker()
        self._tripped_verdict: Optional[ErrorVerdict] = None  # 触发跳闸的那次判定，短路回包复用

    @property
    def breaker_tripped(self) -> bool:
        """账户级熔断器是否已跳闸。"""
        return not self._breaker.allows()

    @property
    def breaker_reason(self) -> str:
        """账户级熔断跳闸原因。"""
        return self._breaker.reason

    @property
    def tripped_verdict(self) -> Optional[ErrorVerdict]:
        """触发账户级熔断的那次结构化判定。"""
        return self._tripped_verdict

    def request_stop(self):
        """请求停止：让正在进行的 chat_with_retry 重试链在下一检查点退出，
        并立即取消当前进行中的 API 请求（不等超时）"""
        self._stop_requested = True
        self._stop_event.set()

    def _breaker_observe(self, success: bool, verdict, error: str) -> None:
        """把一次调用的结论喂给账户级熔断器（记账收敛点）。

        非流式在 chat()（重试漏斗的每次尝试都经此入口，跳闸后剩余尝试在
        chat() 闸门即时短路）；流式在 _build_stream_result（本漏斗唯一出口）。
        只认结构化判定——字符串/None 一律不跳闸（见 core/breaker.py 三条边界）。
        """
        if success:
            self._breaker.record_success()
            return
        if verdict is None:
            return
        if self._breaker.record_failure(verdict, detail=(error or "")[:80]):
            self._tripped_verdict = verdict

    def _breaker_blocked_result(self) -> LLMResult:
        """熔断打开时的非流式短路回包。

        tokens=None + UNKNOWN（结果对象校验组合）、attempts=0（未发生传输）、
        verdict 复用跳闸时缓存的对象——pipeline 靠 is_permanent 登记致命块，
        补跑轮据此跳过，不再重复计费。
        """
        return LLMResult(
            success=False, content="",
            error=f"熔断：{self._breaker.reason}（跳过本次调用）",
            tokens=None, usage_status=UsageStatus.UNKNOWN,
            call_stats={"attempts": 0, "failed_tokens": 0},
            verdict=self._tripped_verdict, attempts=0,
        )

    def _breaker_blocked_stream_result(self) -> StreamResult:
        """熔断打开时的流式短路回包（字段口径同 _breaker_blocked_result）。"""
        return StreamResult(
            success=False, content="",
            error=f"熔断：{self._breaker.reason}（跳过本次调用）",
            prompt_tokens=0, completion_tokens=0,
            cached_tokens=0, reasoning_chars=0,
            call_stats={"attempts": 0, "failed_tokens": 0},
            verdict=self._tripped_verdict,
        )

    @staticmethod
    def _strip_thinking(content: str) -> str:
        """移除思考链标签（兼容各模型：<think>, <思考>, <reasoning> 等）"""
        if not content:
            return content
        # 处理 <think>...</think> 及各种变体（大小写不敏感，支持嵌套空白）
        content = re.sub(r'<think[^>]*>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<思考>.*?</思考>', '', content, flags=re.DOTALL)
        content = re.sub(r'<reasoning>.*?</reasoning>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<thought>.*?</thought>', '', content, flags=re.DOTALL | re.IGNORECASE)
        return content.strip()

    @staticmethod
    async def list_models(base_url: str, api_key: str, provider: str = "auto") -> List[str]:
        """获取API可用的模型列表（支持 openai/anthropic 两种格式）"""
        if provider == "anthropic" or (provider == "auto" and "anthropic" in (base_url or "").lower()):
            # Anthropic 格式：GET /models + x-api-key 头
            if not (base_url or "").strip():
                logger.error("获取模型列表失败: anthropic 端点 base_url 为空")
                return []
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30) as hclient:
                    resp = await hclient.get(
                        f"{base_url.rstrip('/')}/models",
                        headers={
                            "x-api-key": api_key or "",
                            "anthropic-version": "2023-06-01",
                        },
                    )
                    if resp.status_code != 200:
                        logger.error(f"获取模型列表失败: HTTP {resp.status_code}")
                        return []
                    data = resp.json()
                    ids = [str(m.get("id", "")) for m in data.get("data", []) if m.get("id")]
                    return sorted(ids)
            except Exception as e:
                logger.error(f"获取模型列表失败: {e}")
                return []
        try:
            client = AsyncOpenAI(base_url=base_url, api_key=api_key if api_key else "empty")
            result = await client.models.list()
            # 标准端点：返回分页对象，模型列表在 .data 里（每个元素有 .id）
            raw = getattr(result, "data", None)
            if raw is None:
                # 部分非标准端点直接返回 list
                raw = result
            ids: List[str] = []
            for m in raw:
                # 兼容 Model 对象、dict、或直接就是 id 字符串 / (字段,值) tuple 的情况
                if isinstance(m, str):
                    ids.append(m)
                elif isinstance(m, tuple) and len(m) == 2 and m[0] == "data":
                    # pydantic BaseModel 默认的 __iter__ 会把字段迭代成 (名, 值)，
                    # 说明 result 没被解析成标准分页对象，此时从 .data 取值已在上面处理，
                    # 这里跳过这种顶层 tuple，避免误用。
                    continue
                elif hasattr(m, "id"):
                    ids.append(str(m.id))
                elif isinstance(m, dict) and "id" in m:
                    ids.append(str(m["id"]))
            return sorted(ids)
        except Exception as e:
            logger.error(f"获取模型列表失败: {e}")
            return []

    def _build_anthropic_payload(self, messages: List[dict], temperature: float, max_tokens: int) -> dict:
        """Anthropic /v1/messages 载荷构造：system 提取为顶层参数，相邻同角色消息合并。"""
        system_parts: List[str] = []
        msgs: List[dict] = []
        for m in messages:
            role = str(m.get("role", "user"))
            content = m.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if role == "system":
                system_parts.append(content)
            elif role in ("user", "assistant"):
                msgs.append({"role": role, "content": content})
            else:
                msgs.append({"role": "user", "content": content})
        # Anthropic 要求 user/assistant 交替；合并相邻同角色消息
        merged: List[dict] = []
        for m in msgs:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] += "\n\n" + m["content"]
            else:
                merged.append(dict(m))
        payload: dict = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": merged,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        # 思考控制：Anthropic 认 thinking: {"type": "enabled"/"disabled", "budget_tokens": N}。
        # 只放行 thinking 键——残留的 enable_thinking 等 OpenAI 系字段会被 SDK 原样透传导致 API 400
        if self.config.thinking_mode:
            anthro_extra = {k: v for k, v in self.config.thinking_mode.items() if k == "thinking"}
            if anthro_extra:
                payload.update(anthro_extra)
        return payload


    async def chat(
        self,
        messages: List[dict],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None
    ) -> LLMResult:
        """非流式单次调用（返回 LLMResult 对象）。

        熔断闸门：账户级永久错误跳闸后这里零成本短路。重试漏斗
        （chat_with_retry）的每次尝试都经此入口，跳闸后同一漏斗内
        剩余尝试即时退出，不再对已死的账户继续计费请求。
        """
        if not self._breaker.allows():
            return self._breaker_blocked_result()
        result = await self._chat_once(messages, temperature=temperature, max_tokens=max_tokens)
        self._breaker_observe(result.success, result.verdict, result.error)
        return result

    async def _chat_once(
        self,
        messages: List[dict],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None
    ) -> LLMResult:
        """chat() 的原始实现（单次传输，无熔断逻辑——闸门在 chat() 包装层）。"""
        if max_tokens is None:
            max_tokens = self.config.max_tokens

        # 结构化输出：仅对远程 OpenAI 兼容 API 生效（Anthropic 无 response_format，
        # prompt 已要求纯 JSON，跳过），本地模型(localhost)跳过
        extra_kwargs = {}
        if (self.provider != "anthropic" and self.config.json_mode != "default"
                and "localhost" not in self.config.base_url and "127.0.0.1" not in self.config.base_url):
            extra_kwargs["response_format"] = {"type": "json_object"}

        # 思考模式控制：OpenAI 兼容用 extra_body；Anthropic 在载荷构造时注入
        if self.provider != "anthropic":
            body = _extra_body(self.config)
            if body:
                extra_kwargs["extra_body"] = body

        try:
            # 三分竞争：API 请求完成 / 用户请求停止 / 硬超时（任选其一先到）
            if self.provider == "anthropic":
                anthropic_client = self.anthropic
                assert anthropic_client is not None  # provider=anthropic 时 __init__ 已构造
                api_task = asyncio.create_task(
                    anthropic_client.messages.create(
                        **self._build_anthropic_payload(messages, temperature, max_tokens)
                    )
                )
            else:
                openai_client = self.client
                assert openai_client is not None  # provider=openai 时 __init__ 已构造
                api_task = asyncio.create_task(
                    openai_client.chat.completions.create(
                        model=self.config.model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=self.config.timeout,
                        **extra_kwargs
                    )
                )
            stop_wait = asyncio.create_task(self._stop_event.wait())
            hard_timeout = asyncio.create_task(asyncio.sleep(self.config.timeout + 30))  # 硬超时：比 SDK timeout 多 30s 余量

            try:
                done, _pending = await asyncio.wait(
                    {api_task, stop_wait, hard_timeout},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if api_task in done:
                    response = api_task.result()
                elif stop_wait in done:
                    # 用户请求停止：立即取消进行中的请求，不等超时
                    logger.info("用户请求停止，取消进行中的 API 请求")
                    return _fail_result("用户请求停止")
                else:
                    # 硬超时：取消后走下方 asyncio.TimeoutError 分支
                    raise asyncio.TimeoutError
            finally:
                # 统一清理：正常返回/异常/外部 Task.cancel 都不泄漏子任务
                # （外部 cancel 时 asyncio.wait 抛 CancelledError，此处确保底层请求被真正取消）
                for t in (api_task, stop_wait, hard_timeout):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(api_task, stop_wait, hard_timeout, return_exceptions=True)

            with self._stats_lock:
                self._request_count += 1

            if self.provider == "anthropic":
                content, prompt_tokens, completion_tokens, cached_tokens = parse_anthropic_response(response)
                # total 口径对齐 OpenAI usage.total_tokens（OpenAI 的 total 含缓存命中部分）
                total_tokens = prompt_tokens + completion_tokens + cached_tokens
                if not content:
                    error_msg = "API返回空内容（可能触发内容过滤）"
                    # stop_reason=refusal（Anthropic 安全拒绝）或含审核关键词 → 按拦截处理
                    stop_reason = getattr(response, "stop_reason", None) or ""
                    if stop_reason == "refusal" or is_moderation_message(str(stop_reason)):
                        error_msg = mark_moderation(error_msg)
                        logger.warning("检测到内容审核拦截（空内容/refusal），将重试1次后跳过该章节")
                    logger.warning(error_msg)
                    return _fail_result(error_msg, classify_message(error_msg))
                with self._stats_lock:
                    self._total_tokens += total_tokens
                    self._cached_tokens += cached_tokens
                original_len = len(content)
                stripped = self._strip_thinking(content)
                if stripped:
                    content = stripped
                    if original_len > len(content):
                        logger.debug(f"已移除思考链: {original_len} -> {len(content)} 字符")
                cache_hint = f"，缓存命中{cached_tokens}" if cached_tokens else ""
                logger.info(f"API请求成功，使用{total_tokens} tokens (输入{prompt_tokens}+输出{completion_tokens}{cache_hint})")
                # usage 缺失时 parse_anthropic_response 返回 0/0，那是"未知"而不是"零消耗"
                tokens = ((prompt_tokens, completion_tokens)
                          if getattr(response, "usage", None) is not None else None)
                return LLMResult(
                    success=True, content=content, error="", tokens=tokens,
                    cached_tokens=cached_tokens if tokens is not None else 0,
                    usage_status=(UsageStatus.KNOWN if tokens is not None
                                  else UsageStatus.UNKNOWN),
                )

            prompt_tokens = 0
            completion_tokens = 0
            cached_tokens = 0
            if response.usage is not None:
                prompt_tokens = response.usage.prompt_tokens
                completion_tokens = response.usage.completion_tokens
                with self._stats_lock:
                    self._total_tokens += response.usage.total_tokens
                # 读取 KV cache 命中数（厂商扩展字段，无则 0）
                # OpenAI: usage.prompt_tokens_details.cached_tokens
                # DeepSeek: usage.prompt_cache_hit_tokens
                # 智谱/Qwen: 部分版本在 prompt_tokens_details
                ptd = getattr(response.usage, "prompt_tokens_details", None)
                if ptd is not None and getattr(ptd, "cached_tokens", None):
                    cached_tokens = ptd.cached_tokens
                cached_tokens = cached_tokens or getattr(response.usage, "prompt_cache_hit_tokens", 0) or 0
                with self._stats_lock:
                    self._cached_tokens += cached_tokens

            if not response.choices:
                error_msg = "API返回空choices列表（可能触发内容过滤）"
                # 空 choices 通常是审核过滤（content_filter），按拦截处理
                error_msg = mark_moderation(error_msg)
                logger.warning("检测到内容审核拦截（空choices），将重试1次后跳过该章节")
                return _fail_result(error_msg, classify_message(error_msg))

            message = response.choices[0].message
            content = message.content or ""
            # mimo-v2.5 等推理模型：思考过程在 reasoning_content，直接丢弃
            reasoning = getattr(message, 'reasoning_content', None)
            if reasoning:
                logger.debug(f"丢弃推理模型 reasoning_content（{len(reasoning)}字符）")

            # 移除思考链标签（本地模型如Qwen3/Gemma4默认输出思考过程）
            original_len = len(content) if content else 0
            stripped = self._strip_thinking(content)
            # 如果 strip 后变空但原始内容非空（思考链被截断），保留原始内容
            if stripped:
                content = stripped
                if original_len > len(content):
                    logger.debug(f"已移除思考链: {original_len} -> {len(content)} 字符")
            elif content and original_len > 0:
                logger.debug(f"思考链 strip 后为空，保留原始内容（{original_len}字符）")
            cache_hint = f"，缓存命中{cached_tokens}" if cached_tokens else ""
            logger.info(f"API请求成功，使用{response.usage.total_tokens if response.usage else 0} tokens (输入{prompt_tokens}+输出{completion_tokens}{cache_hint})")

            tokens = ((prompt_tokens, completion_tokens)
                      if response.usage is not None else None)
            return LLMResult(
                success=True, content=content, error="", tokens=tokens,
                cached_tokens=cached_tokens if tokens is not None else 0,
                usage_status=(UsageStatus.KNOWN if tokens is not None
                              else UsageStatus.UNKNOWN),
            )

        except asyncio.TimeoutError:
            error_msg = f"请求硬超时 (timeout={self.config.timeout + 30}s): API 无响应"
            logger.warning(error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="HardTimeout",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return _fail_result(error_msg, classify_message(error_msg))

        except AuthenticationError as e:
            error_msg = f"认证失败，请检查API Key: {_redact(str(e))}"
            logger.error(error_msg)
            # 异常对象还在场：结构化结论写进结果对象，重试层再从结果对象读回
            verdict = _verdict_from_exception(e, error_msg)

            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="AuthenticationError",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return _fail_result(error_msg, verdict)

        except APITimeoutError as e:
            error_msg = f"请求超时 (timeout={self.config.timeout}s): {_redact(str(e))}"
            logger.warning(error_msg)
            # 超时属暂时性，结论走文本通道
            verdict = _verdict_from_exception(e, error_msg)

            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="TimeoutError",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return _fail_result(error_msg, verdict)

        except APIError as e:
            status_code = getattr(e, 'status_code', None)
            error_msg = f"API错误 (HTTP {status_code}): {_redact(str(e))}"

            # 错误分类：结构化通道（异常属性码/body 深挖/状态兜底）+ 文本兜底。
            # 审核类打标记；stop/billing 类决策由重试循环按 ClassVerdict.is_permanent() 统一做
            verdict = _verdict_from_exception(e, error_msg)
            if verdict.cls in _MODERATION_CLASSES:
                error_msg = mark_moderation(error_msg)
                logger.warning("检测到内容审核拦截，将重试1次后跳过该章节")

            if status_code == 429:
                # 提取 Retry-After（秒），供 chat_with_retry 做长退避（429 专属）
                retry_after = ""
                resp = getattr(e, 'response', None)
                if resp is not None and getattr(resp, 'headers', None):
                    ra = resp.headers.get('retry-after')
                    if ra:
                        retry_after = str(ra).strip()
                if retry_after:
                    error_msg += f" [Retry-After:{retry_after}]"
                logger.warning(f"触发速率限制(429)，尊重 Retry-After={retry_after or '无'} 长退避重试")

            logger.error(error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type=f"APIError_HTTP{status_code}",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return _fail_result(error_msg, verdict)

        except anthropic.AuthenticationError as e:
            error_msg = f"认证失败，请检查API Key: {_redact(str(e))}"
            logger.error(error_msg)
            verdict = _verdict_from_exception(e, error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="AuthenticationError",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return _fail_result(error_msg, verdict)

        except anthropic.APITimeoutError as e:
            error_msg = f"请求超时 (timeout={self.config.timeout}s): {_redact(str(e))}"
            logger.warning(error_msg)
            verdict = _verdict_from_exception(e, error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="TimeoutError",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return _fail_result(error_msg, verdict)

        except anthropic.APIStatusError as e:
            status_code = getattr(e, 'status_code', None)
            error_msg = f"API错误 (HTTP {status_code}): {_redact(str(e))}"
            # 错误分类：结构化通道（异常属性码/body/状态兜底）+ 文本兜底，
            # Anthropic 皮肤的结构化码（402 billing_error / 422 审核条件规则）由此进入
            verdict = _verdict_from_exception(e, error_msg)
            if verdict.cls in _MODERATION_CLASSES:
                error_msg = mark_moderation(error_msg)
                logger.warning("检测到内容审核拦截，将重试1次后跳过该章节")
            if status_code == 429:
                resp = getattr(e, 'response', None)
                if resp is not None and getattr(resp, 'headers', None):
                    ra = resp.headers.get('retry-after')
                    if ra:
                        error_msg += f" [Retry-After:{str(ra).strip()}]"
                logger.warning("触发速率限制(429)，尊重 Retry-After 长退避重试")
            logger.error(error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type=f"APIError_HTTP{status_code}",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return _fail_result(error_msg, verdict)

        except anthropic.APIConnectionError as e:
            error_msg = f"连接失败: {_redact(str(e))}"
            logger.error(error_msg)
            verdict = _verdict_from_exception(e, error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="ConnectionError",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return _fail_result(error_msg, verdict)

        except Exception as e:
            error_msg = f"未知错误: {type(e).__name__}: {_redact(str(e))}"
            logger.error(error_msg, exc_info=True)
            verdict = _verdict_from_exception(e, error_msg)

            # 内容审核拦截识别（智谱/小米等自由文本消息走这里）
            if is_moderation_message(error_msg):
                error_msg = mark_moderation(error_msg)
                logger.warning("检测到内容审核拦截，将重试1次后跳过该章节")
                verdict = classify_message(error_msg)

            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type=type(e).__name__,
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return _fail_result(error_msg, verdict)

    async def chat_stream(
        self,
        messages: List[dict],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
    ) -> "AsyncIterator[StreamChunk]":
        """流式 LLM 调用：每收到一个 chunk 立即 yield（不阻塞等完整响应）。

        与 chat() 区别：
        - chat() 等完整响应，10.5 分钟硬超时才能发现连接被切；chat_stream 边生成边吐
        - usage 字段：OpenAI 在最后一个 chunk（choices 为空），Anthropic 在 message_delta 事件
        - stop 语义：每 chunk 检查 self._stop_requested，true 时调 stream.close() 优雅退出
        - stream_options 降级：部分聚合网关不认 include_usage 会 400，降级为不带该参数

        调用方责任（chat_stream_with_retry 已封装）：
        - 累积 full_content / full_reasoning
        - 维护 accumulated_output_tokens
        - 调 self._strip_thinking 剥离 <think> 标签
        - 调 validate_response 验证（兼容同步/异步）
        """
        if max_tokens is None:
            max_tokens = self.config.max_tokens

        extra_kwargs = {}
        if (self.provider != "anthropic"
                and self.config.json_mode != "default"
                and "localhost" not in self.config.base_url
                and "127.0.0.1" not in self.config.base_url):
            extra_kwargs["response_format"] = {"type": "json_object"}

        if self.provider != "anthropic":
            body = _extra_body(self.config)
            if body:
                extra_kwargs["extra_body"] = body

        try:
            if self.provider == "anthropic":
                # Anthropic 流式：async with + current_message_snapshot 收尾
                async with self.anthropic.messages.stream(
                    **self._build_anthropic_payload(messages, temperature, max_tokens)
                ) as stream:
                    async for event in stream:
                        if self._stop_requested:
                            await stream.close()
                            return
                        chunk = parse_anthropic_stream_event(event)
                        if chunk:
                            yield chunk
                    # 收尾：补一个 usage chunk
                    snap = stream.current_message_snapshot
                    if snap and getattr(snap, "usage", None):
                        u = snap.usage
                        yield StreamChunk(
                            type="usage",
                            usage_prompt_tokens=getattr(u, "input_tokens", 0) or 0,
                            usage_completion_tokens=getattr(u, "output_tokens", 0) or 0,
                            usage_cached_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                            is_final=True,
                        )
            else:
                # OpenAI 流式：先尝试带 stream_options，不支持则降级
                stream = None
                try:
                    stream = await self.client.chat.completions.create(
                        model=self.config.model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=self.config.timeout,
                        stream=True,
                        stream_options={"include_usage": True},
                        **extra_kwargs,
                    )
                except (TypeError, APIError) as e:
                    if "stream_options" in str(e):
                        logger.warning(f"该端点不支持 stream_options，降级不带该参数: {e}")
                        stream = await self.client.chat.completions.create(
                            model=self.config.model,
                            messages=messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            timeout=self.config.timeout,
                            stream=True,
                            **extra_kwargs,
                        )
                    else:
                        raise

                async for event in stream:
                    if self._stop_requested:
                        if hasattr(stream, "close"):
                            await stream.close()
                        return
                    chunk = parse_openai_stream_event(event)
                    if chunk:
                        yield chunk
        except asyncio.CancelledError:
            # 调用方主动 cancel
            raise
        except Exception as e:
            # 整个调用级别错误（连接失败、API 4xx/5xx 等）作为 error chunk yield。
            # 异常对象还在场——结构化分类唯一时机。
            # 审核类打标记（下游重试循环/pipeline skip 链据 [MODERATION] 前缀行动）；
            # RATE_LIMIT 顺手提取 Retry-After。
            # stop 类（billing/参数/上下文）的"立即停"不在生成器层做，决策统一收在重试循环。
            error_msg = f"流式调用异常: {type(e).__name__}: {_redact(str(e))}"
            verdict = _verdict_from_exception(e, error_msg)
            if verdict.cls in _MODERATION_CLASSES:
                error_msg = mark_moderation(error_msg)
            elif verdict.cls is ErrorClass.RATE_LIMIT:
                ra = _retry_after_from_exception(e)
                if ra:
                    error_msg += f" [Retry-After:{ra}]"
            logger.warning(error_msg)
            # 错误块本身携带完整契约，重试层直接读 chunk.verdict 决断。
            # retry_after 是从错误串的 [Retry-After:N] 标记读回的，故转换必须排在标记拼接之后。
            yield StreamChunk(
                type="error", error=error_msg,
                verdict=_as_error_verdict(verdict, error_msg),
            )
    @staticmethod
    async def _call_validate_response(validate_fn, content: str, timeout: float = 30.0):
        """兼容同步/异步 validate_response；30s 超时（与 chat_with_retry 风格一致）

        现有 validator 多为同步函数，调用时用 asyncio.to_thread 包裹；
        这里保持同步/异步双兼容，避免破坏既有 validator。
        """
        try:
            if inspect.iscoroutinefunction(validate_fn):
                return await asyncio.wait_for(validate_fn(content), timeout=timeout)
            return await asyncio.wait_for(
                asyncio.to_thread(validate_fn, content),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return False, "验证超时（30秒）"
        except Exception as e:
            return False, f"验证异常: {e}"

    def _decide_retry(
        self,
        error: str,
        *,
        moderation_hits: int,
        attempt: int,
        total_attempts: int,
        temperature: float,
        messages_length: int,
        phase: str,
        moderation_log: str,
        wait_time: Optional[float] = None,
        verdict: Optional[ErrorVerdict] = None,
    ) -> RetryDecision:
        """单次 attempt 失败后的统一决策 + 失败登记。

        chat_stream_with_retry 与 chat_with_retry 的温度层/退避层共用此处的决断树
        （决断 + 失败日志 + 停止返回 + 退避计算），四条调用路径只保留各自的结果包装
        （StreamResult / LLMResult）与重试循环控制。

        决断顺序与失败日志口径：
        1. _error_action 给出 moderation_stop / moderation_count / stop / continue
        2. moderation_stop：标记审核 → ModerationBlocked 登记 → 立即停
        3. moderation_count：审核文本连续命中计数 +1，本轮继续
        4. stop 且非 AUTH：追加 _STOP_HINTS 提示语 → 对应 error_type 登记 → 立即停
        5. 通用登记（温度层 RetryNeeded / 退避层 ExponentialBackoff）
        6. stop 且 AUTH：记录后即停（不追加提示语）
        7. continue：温度层给出本次等待秒数（RATE_LIMIT 尊重 Retry-After，上限 120；
           其余 3*(attempt+1)）；退避层的等待已由调用点算好，原样回传。

        参数 phase ∈ {"temperature", "backoff"}——只影响通用失败登记的 error_type 与
        是否把等待秒数写进失败日志（原口径）。attempt 为温度层轮次下标（退避层不使用）。
        停止结果由调用点按各自返回结构构造，本方法不返回业务结果对象。
        """
        # 类型守卫。本形参只接受"结果对象上的跨层形态" ErrorVerdict（或 None）。
        # 策略层 ClassVerdict 的字段恰好同形，传进来会被 _to_class_verdict 静默重包一层，
        # 看起来"能用"；一旦两侧字段演进就会静默降级成"不 stop"（永久错误变可重试）。
        # 这里直接拒收，让类型错位在调用点立刻暴露，而不是变成隐性坑。
        if verdict is not None and not isinstance(verdict, ErrorVerdict):
            raise TypeError(
                "verdict 应传 ErrorVerdict（或 None）；"
                f"实际收到 {type(verdict).__name__}。"
                "ClassVerdict 请先用 _to_error_verdict 转换后再传入。"
            )

        # 入参是结果对象上的 ErrorVerdict（跨层传输形态），这里**只借用**策略层
        # 三元组来算 action，判定本身仍用原始的 ErrorVerdict 往下传，
        # 以免 provider_code / http_status / source_channel / retry_after 全丢，
        # 最终结果上的判定退化成"凭置信度重建"的仿制品。
        raw_verdict = verdict
        action, class_verdict = _error_action(
            error, moderation_hits, _to_class_verdict(raw_verdict)
        )
        verdict = raw_verdict if raw_verdict is not None else _to_error_verdict(class_verdict, error)
        max_retries = self.config.temperature_max_retries + self.config.backoff_max_retries
        is_backoff = phase == "backoff"

        if action == "moderation_stop":
            if not is_moderation_error(error):
                error = mark_moderation(error)
            _get_failure_logger().record_failure(
                attempt_num=total_attempts,
                max_retries=max_retries,
                temperature=temperature,
                error_type="ModerationBlocked",
                error_message=error,
                messages_length=messages_length,
            )
            logger.warning(moderation_log)
            return RetryDecision("stop", error, moderation_hits, 0, verdict)

        if action == "moderation_count":
            moderation_hits += 1

        if action == "stop" and verdict.cls is not ErrorClass.AUTH:
            hint = _STOP_HINTS.get(verdict.cls, "")
            if hint and hint not in error:
                error = error + hint
            _get_failure_logger().record_failure(
                attempt_num=total_attempts,
                max_retries=max_retries,
                temperature=temperature,
                error_type=_STOP_ERROR_TYPES.get(verdict.cls, "StopBlocked"),
                error_message=error,
                messages_length=messages_length,
                **({"wait_time": wait_time} if is_backoff else {}),
            )
            logger.error(f"⛔ {verdict.cls.value} 类错误（不可重试），停止重试: {error}")
            return RetryDecision("stop", error, moderation_hits, 0, verdict)

        if is_backoff:
            _get_failure_logger().record_failure(
                attempt_num=total_attempts,
                max_retries=max_retries,
                temperature=temperature,
                error_type="ExponentialBackoff",
                error_message=error,
                wait_time=wait_time,
                messages_length=messages_length,
            )
        else:
            _get_failure_logger().record_failure(
                attempt_num=total_attempts,
                max_retries=max_retries,
                temperature=temperature,
                error_type="RetryNeeded",
                error_message=error,
                wait_time=0,
                messages_length=messages_length,
            )

        if action == "stop":  # AUTH：记录后即停（不追加提示语）
            logger.error("⛔ 认证错误，停止重试")
            return RetryDecision("stop", error, moderation_hits, 0, verdict)

        if is_backoff:
            return RetryDecision("continue", error, moderation_hits, wait_time, verdict)
        if class_verdict.cls is ErrorClass.RATE_LIMIT:
            # 限流：尊重 Retry-After，退避拉长（限流是暂时的，值得等）。
            # 优先用结构化判定里的 typed retry_after——结构化通道拿到的秒数是数字；
            # 文本通道才需要从错误串里正则捞。
            typed = getattr(raw_verdict, "retry_after", None)
            next_wait = min(
                typed or self._retry_after_seconds(error) or 30 * (attempt + 1), 120
            )
        else:
            next_wait = 3 * (attempt + 1)
        return RetryDecision("continue", error, moderation_hits, next_wait, verdict)

    async def _retry_chain(
        self,
        messages: List[dict],
        *,
        max_tokens: Optional[int],
        try_once,
        hooks,
        moderation_log_temperature: str,
        moderation_log_backoff: str,
    ):
        """流式/非流式共用的重试骨架：温度退火 → 指数退避。

        骨架只拥有控制流（stop 检查 / 尝试计数 / 温度计算 / 决策树调用 / 等待 / 汇总），
        两条链的差异全部收敛在适配器闭包里：

        - ``try_once(temp, attempt, total_attempts, phase, wait_time=None)`` 执行单次
          尝试并处理成功路径（验证 / 记账 / 成功构造），返回 :class:`_AttemptOutcome`；
        - ``hooks.on_stop()`` —— 用户停止的终态结果；
        - ``hooks.on_decision_stop(decision)`` —— 决策树判停的终态结果；
        - ``hooks.on_moderation_short_circuit(last_error, total_attempts, final_temp)``
          —— 温度层审核连续命中后的退避层短路；
        - ``hooks.on_final_failure(last_error, total_attempts, backoff_rounds)``
          —— 所有轮次耗尽的终态结果。

        行为保真备注：退避层决策传入的 ``attempt`` 是温度层最后一轮下标（原实现
        两链皆如此，``_decide_retry`` 在 backoff 相不使用该参数）；退避层等待秒数
        的 ``30 * (retry_num + 1)`` 基数与温度层决策里的 ``30 * (attempt + 1)``
        是两个口径，不得互串。
        """
        messages_length = len(str(messages))
        last_error = ""
        total_attempts = 0
        moderation_hits = 0
        temp_attempt = 0  # 温度层最后一轮下标（退避层决策沿用，见 docstring）

        # === 温度退火层 ===
        for attempt in range(self.config.temperature_max_retries):
            if self._stop_requested:
                logger.info("⛔ 检测到停止请求，终止重试链")
                return await hooks.on_stop(total_attempts)
            total_attempts += 1
            with self._stats_lock:
                self._attempts += 1

            temp = max(0.0, self.config.temperature - attempt * self.config.temperature_step)
            logger.info(f"\n[温度退火] 尝试 {total_attempts}，"
                        f"temperature={temp} (第{attempt + 1}/{self.config.temperature_max_retries}次)")

            outcome = await try_once(temp, attempt, total_attempts, "temperature")
            if outcome.kind == "done":
                return outcome.result
            last_error = outcome.error
            temp_attempt = attempt
            if outcome.kind == "retry_soon":
                # 附加等待（3*(attempt+1)）由适配器按各自原行为完成
                continue

            decision = self._decide_retry(
                outcome.error,
                moderation_hits=moderation_hits,
                verdict=outcome.verdict,
                attempt=attempt,
                total_attempts=total_attempts,
                temperature=temp,
                messages_length=messages_length,
                phase="temperature",
                moderation_log=moderation_log_temperature,
            )
            moderation_hits = decision.moderation_hits
            last_error = decision.error
            notified = getattr(hooks, "on_decided", None)
            if notified:
                await notified(decision)
            if decision.action == "stop":
                return await hooks.on_decision_stop(decision, total_attempts)

            if attempt < self.config.temperature_max_retries - 1:
                await self._sleep(decision.wait_time)

        # === 指数退避层（429 限流时额外多试几轮：限流通常是暂时的，快速放弃会丢块）===
        final_temp = max(
            0.0,
            self.config.temperature
            - (self.config.temperature_max_retries - 1) * self.config.temperature_step,
        )
        # 限流判定走分类结论（仅在允许退避时对限流加码；若 backoff_max_retries 为 0 则严格不退避）
        backoff_rounds = (
            max(self.config.backoff_max_retries, 3)
            if (self.config.backoff_max_retries > 0 and classify_message(last_error).cls is ErrorClass.RATE_LIMIT)
            else self.config.backoff_max_retries
        )
        for retry_num in range(backoff_rounds):
            if self._stop_requested:
                logger.info("⛔ 检测到停止请求，终止重试链")
                return await hooks.on_stop(total_attempts)
            # 温度循环已连续命中审核拦截 → 直接放弃，不进指数退避，
            # 避免对"永久拒绝"白等 30 分钟
            if is_moderation_error(last_error) and moderation_hits >= 2:
                return await hooks.on_moderation_short_circuit(
                    last_error, total_attempts, final_temp
                )
            total_attempts += 1
            with self._stats_lock:
                self._attempts += 1

            if classify_message(last_error).cls is ErrorClass.RATE_LIMIT:
                wait_time = min(self._retry_after_seconds(last_error) or 30 * (retry_num + 1), 120)
            else:
                wait_time = min(2 ** (retry_num + 1), 60)

            logger.info(f"\n[指数退避] 尝试 {total_attempts}，"
                        f"等待{wait_time}秒，temperature={final_temp} "
                        f"(第{retry_num + 1}/{backoff_rounds}次)")
            await self._sleep(wait_time)

            outcome = await try_once(final_temp, retry_num, total_attempts, "backoff", wait_time)
            if outcome.kind == "done":
                return outcome.result
            last_error = outcome.error
            if outcome.kind == "error":
                decision = self._decide_retry(
                    outcome.error,
                    moderation_hits=moderation_hits,
                    verdict=outcome.verdict,
                    attempt=temp_attempt,
                    total_attempts=total_attempts,
                    temperature=final_temp,
                    messages_length=messages_length,
                    phase="backoff",
                    moderation_log=moderation_log_backoff,
                    wait_time=wait_time,
                )
                moderation_hits = decision.moderation_hits
                last_error = decision.error
                notified = getattr(hooks, "on_decided", None)
                if notified:
                    await notified(decision)
                if decision.action == "stop":
                    return await hooks.on_decision_stop(decision, total_attempts)

        return await hooks.on_final_failure(last_error, total_attempts, backoff_rounds)

    async def chat_stream_with_retry(
        self,
        messages: List[dict],
        max_tokens: Optional[int] = None,
        validate_response: Optional[Callable] = None,
        retry_messages_builder: Optional[Callable] = None,
        on_progress: Optional[Callable[[StreamChunk], Awaitable[None]]] = None,
    ) -> StreamResult:
        """流式重试包装：行为契约与 chat_with_retry 完全一致
        （温度退火 → 指数退避；429 Retry-After；审核短路；认证即停；FailureLogger；统计累加）。

        控制流（stop 检查 / 计数 / 决策树 / 等待 / 汇总）在 _retry_chain 骨架；
        本方法只提供流式特有的三件事：

        - 单次尝试：_run_stream_attempt（chunk 消费 / 取消检测 / partial content 保留）；
        - 成功路径：思考链剥离 → token 记账 → 空响应嗅探 → 同步/异步双兼容验证
          （_call_validate_response）；
        - 结果构造：_build_stream_result（StreamResult 口径 + 熔断记账收敛点）。
        """
        if not self._breaker.allows():
            # 熔断打开：零成本短路，不进重试循环
            return self._breaker_blocked_stream_result()
        messages_length = len(str(messages))
        call_failed_tokens = 0
        last_consumed_tokens: Tuple[int, int] = (0, 0)
        accumulated_output_tokens = 0  # 累计值
        final_content = ""  # 保留 partial content
        final_reasoning = ""
        last_usage: Optional[StreamChunk] = None
        last_verdict: Optional[ClassVerdict] = None  # 最近一次决断结论（失败结果携带它）

        logger.info("=" * 60)
        logger.info(f"开始流式API调用 - Model: {self.config.model}")
        logger.info(f"配置: temperature={self.config.temperature}, temperature_step={self.config.temperature_step}, "
                    f"temperature_max_retries={self.config.temperature_max_retries}, "
                    f"backoff_max_retries={self.config.backoff_max_retries}")
        if validate_response:
            logger.info("启用响应验证回调")
        logger.info("=" * 60)

        def _build_stream_result(success, content, error, prompt_tok, comp_tok,
                          cached_tokens=0, reasoning_chars=0, verdict=None,
                          total_attempts=0):
            # verdict 是本次决断用的 ClassVerdict（决策层形态），出口统一转成 ErrorVerdict
            # （跨层传输形态）；成功结果恒为 None，与 LLMResult 口径一致。
            result = StreamResult(
                success=success, content=content, error=error,
                prompt_tokens=prompt_tok, completion_tokens=comp_tok,
                cached_tokens=cached_tokens, reasoning_chars=reasoning_chars,
                call_stats={"attempts": total_attempts, "failed_tokens": call_failed_tokens},
                verdict=None if success else _as_error_verdict(verdict, error),
            )
            # 熔断记账收敛点（流式）：本漏斗的每个出口都经此构造；
            # 非流式路径由 chat() 逐次尝试自记，两处不可重复
            self._breaker_observe(success, result.verdict, error)
            return result

        def _record_validation_failure(validation_error, temp, total_attempts):
            nonlocal call_failed_tokens
            if last_consumed_tokens[0] > 0 or last_consumed_tokens[1] > 0:
                with self._stats_lock:
                    self._failed_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
                call_failed_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
            _get_failure_logger().record_failure(
                attempt_num=total_attempts,
                max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                temperature=temp,
                error_type="ValidationFailed",
                error_message=f"响应验证失败: {validation_error}",
                wait_time=0,
                messages_length=messages_length,
            )

        async def _run_stream_attempt(temp):
            """单次流式 attempt：返回 (status, content, reasoning, usage, err, verdict)。

            status ∈ {"ok", "error", "cancelled"}；
            verdict 是错误块自带的 ErrorVerdict（chat_stream 在异常现场算好后挂在 chunk 上），
            正常结束为 None——结论随块流转。"""
            nonlocal accumulated_output_tokens, last_consumed_tokens
            nonlocal final_content, final_reasoning, last_usage
            attempt_content = ""
            attempt_reasoning = ""
            attempt_error = ""
            attempt_verdict = None
            accumulated_output_tokens = 0  # 单次 attempt 独立统计，重试不污染初值
            try:
                async for chunk in self.chat_stream(messages, temperature=temp, max_tokens=max_tokens):
                    if chunk.type == "content":
                        attempt_content += chunk.text
                        accumulated_output_tokens += estimate_token_count(chunk.text)
                    elif chunk.type == "reasoning":
                        attempt_reasoning += chunk.reasoning_text
                        accumulated_output_tokens += estimate_token_count(chunk.reasoning_text)
                    elif chunk.type == "usage":
                        last_usage = chunk
                        last_consumed_tokens = (chunk.usage_prompt_tokens, chunk.usage_completion_tokens)
                    elif chunk.type == "error":
                        attempt_error = chunk.error
                        attempt_verdict = chunk.verdict
                        break
                    # on_progress 传累计值（保持 type="content" 兼容现有白名单契约，用 is_thinking 携带推理状态）
                    if on_progress:
                        await on_progress(StreamChunk(
                            type="content",
                            estimated_total_tokens=accumulated_output_tokens,
                            text=chunk.text if chunk.type == "content" else "",
                            is_thinking=bool(chunk.type == "reasoning" or getattr(chunk, "is_thinking", False)),
                        ))
            except asyncio.CancelledError:
                return ("cancelled", attempt_content, attempt_reasoning, last_usage,
                        "用户请求停止", None)
            finally:
                # 同步 attempt_content → final_content
                # 失败路径（认证/审核/退避耗尽）依赖 final_content 作为残缺 JSON 兜底
                # 保留最新 attempt 的 partial（含 reasoning）以便重试失败后增量补全
                final_content = attempt_content
                final_reasoning = attempt_reasoning

            if attempt_error:
                return ("error", attempt_content, attempt_reasoning, last_usage,
                        attempt_error, attempt_verdict)
            return ("ok", attempt_content, attempt_reasoning, last_usage, "", None)

        async def try_once(temp, attempt, total_attempts, phase, wait_time=None):
            """单次流式尝试 + 成功路径处理；错误/验证失败交还骨架控制流。"""
            nonlocal messages, call_failed_tokens
            status, content, reasoning, usage, err, chunk_verdict = \
                await _run_stream_attempt(temp)

            if status == "cancelled":
                return _AttemptOutcome("done", result=_build_stream_result(
                    False, content, err, 0, 0, reasoning_chars=len(reasoning),
                    total_attempts=total_attempts))

            if status == "error":
                logger.warning(f"❌ 尝试失败: {err}" if phase == "temperature"
                               else f"❌ 重试失败: {err}")
                if last_consumed_tokens[0] > 0 or last_consumed_tokens[1] > 0:
                    with self._stats_lock:
                        self._failed_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
                    call_failed_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
                return _AttemptOutcome("error", error=err, verdict=chunk_verdict)

            # status == "ok"：剥离思考链 + 累计成功 token
            stripped = self._strip_thinking(content)
            if stripped:
                content = stripped

            if last_consumed_tokens[0] > 0 or last_consumed_tokens[1] > 0:
                with self._stats_lock:
                    self._total_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
                    if usage:
                        self._cached_tokens += usage.usage_cached_tokens

            # 空响应检测（moderation 嗅探）：温度层登记 EmptyResponse 并冷却，退避层直接重试
            if not content.strip():
                error_msg = "API返回空内容（可能触发内容过滤）"
                if usage and (is_moderation_message(str(usage)) or is_moderation_error(error_msg)):
                    error_msg = mark_moderation(error_msg)
                logger.warning(error_msg)
                if phase == "temperature":
                    _get_failure_logger().record_failure(
                        attempt_num=total_attempts,
                        max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                        temperature=temp, error_type="EmptyResponse",
                        error_message=error_msg, messages_length=messages_length,
                    )
                    if attempt < self.config.temperature_max_retries - 1:
                        await self._sleep(3 * (attempt + 1))
                return _AttemptOutcome("retry_soon", error=error_msg)

            if validate_response:
                is_valid, validation_error = await self._call_validate_response(
                    validate_response, content, timeout=30)
                if not is_valid:
                    logger.warning(f"❌ 响应验证失败: {validation_error}")
                    if retry_messages_builder:
                        messages = retry_messages_builder(validation_error, messages)
                    _record_validation_failure(validation_error, temp, total_attempts)
                    if phase == "temperature" and attempt < self.config.temperature_max_retries - 1:
                        await self._sleep(3 * (attempt + 1))
                    return _AttemptOutcome("retry_soon",
                                           error=f"响应验证失败: {validation_error}")

            # 成功
            logger.info(f"✅ API请求成功（第{total_attempts}次尝试）")
            logger.info(f"使用Tokens: {last_consumed_tokens}")
            logger.info("=" * 60)
            return _AttemptOutcome("done", result=_build_stream_result(
                True, content, "",
                last_consumed_tokens[0], last_consumed_tokens[1],
                cached_tokens=usage.usage_cached_tokens if usage else 0,
                reasoning_chars=len(reasoning),
                total_attempts=total_attempts,
            ))

        async def on_stop(total_attempts):
            return _build_stream_result(False, final_content, "用户请求停止",
                                 last_consumed_tokens[0], last_consumed_tokens[1],
                                 total_attempts=total_attempts)

        async def on_decided(decision):
            nonlocal last_verdict
            last_verdict = decision.verdict

        async def on_decision_stop(decision, total_attempts):
            return _build_stream_result(False, final_content, decision.error,
                                 last_consumed_tokens[0], last_consumed_tokens[1],
                                 verdict=decision.verdict, total_attempts=total_attempts)

        async def on_moderation_short_circuit(short_error, total_attempts, final_temp):
            _get_failure_logger().record_failure(
                attempt_num=total_attempts,
                max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                temperature=final_temp, error_type="ModerationBlocked",
                error_message=short_error, messages_length=messages_length,
            )
            logger.warning("⛔ 内容审核拦截（重试1次后仍拦截），跳过该章节")
            return _build_stream_result(False, final_content, short_error,
                                 last_consumed_tokens[0], last_consumed_tokens[1],
                                 verdict=last_verdict or classify_message(short_error),
                                 total_attempts=total_attempts)

        async def on_final_failure(last_error, total_attempts, backoff_rounds):
            final_msg = (f"所有重试均失败（温度退火{self.config.temperature_max_retries}次 + "
                         f"指数退避{backoff_rounds}次 = 共{total_attempts}次）。最后错误: {last_error}")
            logger.error("\n" + "=" * 60)
            logger.error("⛔ API调用最终失败")
            logger.error(f"总尝试次数: {total_attempts}")
            logger.error(f"最后错误: {last_error}")
            summary = _get_failure_logger().get_summary()
            logger.error(f"失败统计: {summary}")
            logger.error("=" * 60)
            return _build_stream_result(False, final_content, final_msg,
                                 last_consumed_tokens[0], last_consumed_tokens[1],
                                 verdict=last_verdict or classify_message(last_error),
                                 total_attempts=total_attempts)

        hooks = SimpleNamespace(
            on_stop=on_stop, on_decided=on_decided, on_decision_stop=on_decision_stop,
            on_moderation_short_circuit=on_moderation_short_circuit,
            on_final_failure=on_final_failure,
        )
        return await self._retry_chain(
            messages, max_tokens=max_tokens, try_once=try_once, hooks=hooks,
            moderation_log_temperature=(
                "⛔ 内容审核拦截，跳过该章节（结构化/输入侧首次命中，或文本连续3次）"),
            moderation_log_backoff="⛔ 内容审核拦截，跳过该章节",
        )

    async def chat_single(
        self,
        messages: List[dict],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        """明确的单次非流式传输入口（返回 LLMResult 对象）。

        关闭所有包装层重试（无温度退火循环、无指数退避、无 429 隐式加码）。
        对应且仅对应一次底层网络传输尝试，供外部预算控制器做逐次预留与独立结算。
        """
        temp = self.config.temperature if temperature is None else temperature
        return await self.chat(messages, temperature=temp, max_tokens=max_tokens)

    async def chat_auto(
        self,
        messages: List[dict],
        max_tokens: Optional[int] = None,
        validate_response: Optional[Callable] = None,
        retry_messages_builder: Optional[Callable] = None,
        on_progress: Optional[Callable[[StreamChunk], Awaitable[None]]] = None,
        *,
        disable_retries: bool = False,
    ) -> LLMResult:
        """根据 self.config.streaming_enabled 自动选 chat_with_retry / chat_stream_with_retry

        业务接入统一入口：
        - disable_retries=True → 直接调 chat_single（单次传输，关闭客户端与 SDK 层所有重试）
        - streaming_enabled=True（默认）→ chat_stream_with_retry（流式，每 chunk 回调 on_progress）
        - streaming_enabled=False → chat_with_retry（非流式 API，on_progress 忽略）

        两条分支统一返回 LLMResult。流式分支从 StreamResult 的字段构造；
        StreamResult 自带 verdict，这里原样透传，使 final_summary / style_analyzer
        在流式失败时也能拿到判定。
        """
        if disable_retries:
            return await self.chat_single(messages, max_tokens=max_tokens)
        if self.config.streaming_enabled:
            result = await self.chat_stream_with_retry(
                messages, max_tokens=max_tokens,
                validate_response=validate_response,
                retry_messages_builder=retry_messages_builder,
                on_progress=on_progress,
            )
            # 流式 usage 缺失时 StreamResult 是 0/0；两个都为 0 视为"没拿到 usage"
            raw_tokens = (result.prompt_tokens, result.completion_tokens)
            tokens = raw_tokens if (raw_tokens[0] or raw_tokens[1]) else None
            return LLMResult(
                success=result.success,
                content=result.content,
                error=result.error,
                tokens=tokens,
                cached_tokens=(result.cached_tokens or 0) if tokens is not None else 0,
                usage_status=(UsageStatus.KNOWN if tokens is not None
                              else UsageStatus.UNKNOWN),
                call_stats=result.call_stats,
                verdict=result.verdict,
                attempts=int(result.call_stats.get("attempts", 0) or 0),
            )
        # 非流式：on_progress 被忽略（保持接口一致）
        return await self.chat_with_retry(
            messages, max_tokens=max_tokens,
            validate_response=validate_response,
            retry_messages_builder=retry_messages_builder,
        )

    @staticmethod
    def _is_429(error: str) -> bool:
        """错误消息是否为 429 限流。

        兼容壳：重试决策一律走 classify_message 的分类结论，不再用子串判定。
        """
        return "429" in (error or "")

    @staticmethod
    def _retry_after_seconds(error: str) -> Optional[float]:
        """从错误消息提取 Retry-After 秒数（chat() 已在 429 分支注入 [Retry-After:N]）"""
        m = re.search(r'Retry-After:(\d+)', error or "")
        if m:
            try:
                return max(1.0, float(m.group(1)))
            except ValueError:
                return None
        return None

    async def _sleep(self, seconds: float):
        """重试等待的薄封装（测试注入点，行为同 asyncio.sleep）"""
        await asyncio.sleep(seconds)

    def _build_result(
        self,
        success: bool,
        content: str,
        error: str,
        tokens: Optional[Tuple[int, int]],
        *,
        total_attempts: int,
        failed_tokens: int,
        verdict: Optional["ErrorVerdict | ClassVerdict"] = None,
        prior_tokens: Optional[Tuple[int, int]] = None,
        cached_tokens: int = 0,
    ) -> LLMResult:
        """构造非流式调用结果对象。

        这是非流式路径唯一的 LLMResult 出口（chat() 的直接返回除外），
        保证 attempts/tokens/usage_status/verdict 四处口径一致：
        - tokens 为 None ⟺ usage_status=UNKNOWN（__post_init__ 会拦自相矛盾的组合）；
        - call_stats 的 attempts 与对象上的 attempts 同源，不再各自维护；
        - verdict 只有失败时才有，且由 _decide_retry / chat() 的现场分类提供，
          此处不再二次分类（策略结论只算一次）。
        """
        return LLMResult(
            success=success,
            content=content,
            error=error,
            tokens=tokens,
            cached_tokens=cached_tokens if tokens is not None else 0,
            usage_status=(UsageStatus.KNOWN if tokens is not None
                          else UsageStatus.UNKNOWN),
            call_stats={"attempts": total_attempts, "failed_tokens": failed_tokens},
            verdict=None if success else _as_error_verdict(verdict, error),
            attempts=total_attempts,
            prior_known_tokens=prior_tokens,
        )

    async def chat_with_retry(
        self,
        messages: List[dict],
        max_tokens: Optional[int] = None,
        validate_response: Optional[Callable[[str], Tuple[bool, str]]] = None,
        retry_messages_builder: Optional[Callable[[str, List[dict]], List[dict]]] = None
    ) -> LLMResult:
        """非流式重试包装（温度退火 → 指数退避），返回 LLMResult 对象。

        tokens 保留"最后一次已扣费但未被采用"的用量（验证失败/后续全失败时也在），
        一次都没拿到 usage 就是 None + usage_status=UNKNOWN。
        verdict 由 _decide_retry 的决断结论带出，停止或最终失败时随结果返回，成功为 None。

        控制流在 _retry_chain 骨架；本方法只提供单次尝试（chat() 调用 + 线程化验证）
        与结果构造（_build_result，attempts / prior_known_tokens 口径）。
        """
        if not self._breaker.allows():
            # 熔断打开：零成本短路，不进重试循环
            return self._breaker_blocked_result()
        messages_length = len(str(messages))
        call_failed_tokens = 0
        # 跟踪最后一次 API 成功调用消耗的 tokens（即使 JSON 验证失败，API 已扣费）
        # 一次都没拿到 usage 时是 None，不用 (0, 0) 冒充"零消耗"
        # 三种口径必须分开，混用会让"本次未知"冒充"本次已知"。
        # - attempt_tokens：**本次尝试自己**报的用量（拿不到就是 None）
        # - last_consumed_tokens：最近一次报过用量的尝试（可能来自更早的尝试），只用于统计
        # - prior_known_tokens：已被确认消耗、但产出未被采用的累计（费用必须记账，但不能算进 tokens）
        attempt_tokens: Optional[Tuple[int, int]] = None
        prior_known_tokens: Optional[Tuple[int, int]] = None
        last_consumed_tokens: Optional[Tuple[int, int]] = None

        logger.info("=" * 60)
        logger.info(f"开始API调用 - Model: {self.config.model}")
        logger.info(f"配置: temperature={self.config.temperature}, temperature_step={self.config.temperature_step}, "
                   f"temperature_max_retries={self.config.temperature_max_retries}, "
                   f"backoff_max_retries={self.config.backoff_max_retries}")
        if validate_response:
            logger.info("启用响应验证回调")
        logger.info("=" * 60)

        async def try_once(temp, attempt, total_attempts, phase, wait_time=None):
            nonlocal messages, call_failed_tokens
            nonlocal attempt_tokens, prior_known_tokens, last_consumed_tokens
            result = await self.chat(
                messages, temperature=temp, max_tokens=max_tokens
            )
            content = result.content
            tokens = result.tokens
            attempt_tokens = tokens

            if result.success:
                if validate_response:
                    # 在线程中运行验证，避免 json5/ast.literal_eval 等同步解析器阻塞事件循环
                    try:
                        is_valid, validation_error = await asyncio.wait_for(
                            asyncio.to_thread(validate_response, content),
                            timeout=30
                        )
                    except asyncio.TimeoutError:
                        logger.warning("❌ 响应验证超时（30秒），视为验证失败")
                        is_valid, validation_error = False, "验证超时（30秒）"
                    except Exception as e:
                        logger.warning(f"❌ 响应验证异常: {e}")
                        is_valid, validation_error = False, f"验证异常: {e}"
                    if not is_valid:
                        logger.warning(f"❌ 响应验证失败: {validation_error}")
                        logger.info(f"[DEBUG] LLM响应(全文): {repr(content)}")
                        # 累加失败 tokens（API 已消耗，但响应无效）
                        if tokens is not None:
                            with self._stats_lock:
                                self._failed_tokens += tokens[0] + tokens[1]
                            call_failed_tokens += tokens[0] + tokens[1]
                            # 这次的确花了钱，但产出被丢弃：记进"历史已知"，不进本次 tokens
                            prior_known_tokens = _add_tokens(prior_known_tokens, tokens)
                            last_consumed_tokens = tokens
                        last_error_msg = f"响应验证失败: {validation_error}"
                        # 构建带格式修正提示的messages用于重试
                        if retry_messages_builder:
                            messages = retry_messages_builder(validation_error, messages)
                        _get_failure_logger().record_failure(
                            attempt_num=total_attempts,
                            max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                            temperature=temp,
                            error_type="ValidationFailed",
                            error_message=last_error_msg,
                            wait_time=0 if phase == "temperature" else (wait_time or 0),
                            messages_length=messages_length,
                        )
                        if phase == "temperature" and attempt < self.config.temperature_max_retries - 1:
                            wait = 3 * (attempt + 1)
                            logger.info(f"⏳ 等待{wait}秒后进行下一次温度尝试...")
                            await self._sleep(wait)
                        return _AttemptOutcome("retry_soon", error=last_error_msg)

                logger.info(f"✅ API请求成功（第{total_attempts}次尝试）")
                logger.info(f"使用Tokens: {tokens}")
                logger.info("=" * 60)
                return _AttemptOutcome("done", result=self._build_result(
                    True, content, "", tokens,
                    total_attempts=total_attempts, failed_tokens=call_failed_tokens,
                    prior_tokens=prior_known_tokens,
                    cached_tokens=getattr(result, "cached_tokens", 0) or 0,
                ))

            error = result.error
            logger.warning(f"❌ 尝试失败: {error}" if phase == "temperature"
                           else f"❌ 重试失败: {error}")

            # 本次尝试若真的扣了费，记进"历史已知"；它不会被当成下一次尝试的用量。
            # （原实现退避层只更新 last_consumed、不进 prior_known，保持原口径。）
            if attempt_tokens is not None and (attempt_tokens[0] > 0 or attempt_tokens[1] > 0):
                if phase == "temperature":
                    prior_known_tokens = _add_tokens(prior_known_tokens, attempt_tokens)
                last_consumed_tokens = attempt_tokens
            return _AttemptOutcome("error", error=error, verdict=result.verdict)

        async def on_stop(total_attempts):
            return self._build_result(
                False, "", "用户请求停止", attempt_tokens,
                total_attempts=total_attempts, failed_tokens=call_failed_tokens,
                prior_tokens=prior_known_tokens,
            )

        async def on_decision_stop(decision, total_attempts):
            return self._build_result(
                False, "", decision.error, attempt_tokens,
                total_attempts=total_attempts, failed_tokens=call_failed_tokens,
                prior_tokens=prior_known_tokens,
                verdict=decision.verdict,
            )

        async def on_moderation_short_circuit(short_error, total_attempts, final_temp):
            _get_failure_logger().record_failure(
                attempt_num=total_attempts,
                max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                temperature=final_temp,
                error_type="ModerationBlocked",
                error_message=short_error,
                messages_length=messages_length,
            )
            logger.warning("⛔ 内容审核拦截（重试1次后仍拦截），跳过该章节，不再重试")
            return self._build_result(
                False, "", short_error, attempt_tokens,
                total_attempts=total_attempts, failed_tokens=call_failed_tokens,
                prior_tokens=prior_known_tokens,
                verdict=classify_message(short_error),
            )

        async def on_final_failure(last_error, total_attempts, backoff_rounds):
            final_msg = f"所有重试均失败（温度退火{self.config.temperature_max_retries}次 + " \
                        f"指数退避{self.config.backoff_max_retries}次 = 共{total_attempts}次）。" \
                        f"最后错误: {last_error}"
            logger.error("\n" + "=" * 60)
            logger.error("⛔ API调用最终失败")
            logger.error(f"总尝试次数: {total_attempts}")
            logger.error(f"最后错误: {last_error}")

            summary = _get_failure_logger().get_summary()
            logger.error(f"失败统计: {summary}")
            logger.error("=" * 60)

            return self._build_result(
                False, "", final_msg, attempt_tokens,
                total_attempts=total_attempts, failed_tokens=call_failed_tokens,
                prior_tokens=prior_known_tokens,
                verdict=classify_message(last_error),
            )

        hooks = SimpleNamespace(
            on_stop=on_stop, on_decision_stop=on_decision_stop,
            on_moderation_short_circuit=on_moderation_short_circuit,
            on_final_failure=on_final_failure,
        )
        return await self._retry_chain(
            messages, max_tokens=max_tokens, try_once=try_once, hooks=hooks,
            moderation_log_temperature="⛔ 内容审核拦截，跳过该章节，不再重试",
            moderation_log_backoff="⛔ 内容审核拦截，跳过该章节，不再重试",
        )

    def get_stats(self) -> dict:
        return {
            "total_requests": self._request_count,
            "total_attempts": self._attempts,
            "total_tokens": self._total_tokens,
            "failed_tokens": self._failed_tokens,
            "cached_tokens": self._cached_tokens,
            "failure_log_summary": _get_failure_logger().get_summary()
        }
