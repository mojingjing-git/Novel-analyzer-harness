"""错误判定的共享类型（error_types）

最底层叶子模块：只放"共享枚举 + 纯数据类型"，不含 IO / 策略 / 协议逻辑。
设计规格：docs/DESIGN-D01-verdict-transport-2026-10-30.md §3；
契约清单：docs/PLAN-v2-long-novel-reliability-2026-10-30.md §2.5。

模块职责
--------
1. 定义跨层共享枚举：ErrorClass / Confidence / SourceChannel / ErrorScope / UsageStatus
2. 定义两个纯数据类型：ErrorVerdict（错误判定结论）、LLMResult（一次模型调用的结果）
3. 提供显式 JSON DTO（to_dict / from_dict）——不依赖 dataclass 默认序列化形状

依赖方向（硬约束）
------------------
    error_taxonomy ─┐
                    ├─→ error_types ←─ llm_stream
    llm_client ─────┘

error_types **不得** import error_taxonomy / llm_stream / llm_client：
它是叶子模块，策略层（error_taxonomy）与协议层（llm_stream）都单向依赖它；
一旦反向依赖就形成循环导入，并让"策略层依赖协议层"。
（backend/tests/test_error_types.py 用 AST 断言守护这条约束。）

关键语义（消费方不得自行推导）
------------------------------
- action：由唯一的策略函数计算后固化在 verdict 里；外层只消费，不再自行推导
- message：仅用于展示与日志；**任何判定逻辑不得读取它**
- scope：只能取 ErrorScope 枚举值（恰好 4 个）；不得自由字符串，
  不得默认成 REQUEST_LOCAL；UNKNOWN 必须保留，且不得凭错误字符串升级为账户熔断
- tokens：未拿到 usage 时为 None 且 usage_status=UNKNOWN——
  不得写成 (0, 0)（那是"消耗为零"的错误陈述），也不得沿用上一次 attempt 的计数
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple


__all__ = [
    "ErrorClass",
    "Confidence",
    "SourceChannel",
    "ErrorScope",
    "UsageStatus",
    "ErrorVerdict",
    "LLMResult",
]


# ---------------------------------------------------------------------------
# 共享枚举
# ---------------------------------------------------------------------------

class ErrorClass(str, Enum):
    """错误类别（11 类）

    取值集合与 backend/core/error_taxonomy.py 历史上定义的那套**逐一保持一致**
    （成员名与字符串值都相同）。2026-10-30（S1/U6b）起，本模块是该枚举的**唯一定义处**：
    ``error_taxonomy`` 已改为 ``from .error_types import ErrorClass`` 并 re-export，
    两边是同一个类对象。这样 ``is`` 身份比较在跨模块场景才成立——两份同值 str-Enum
    会让 ``==`` 为真而 ``is`` 为假，判定被静默判错。

    为何当初不在本模块反向 import：本模块是最底层叶子，``error_taxonomy`` 要返回本模块的
    ``ErrorVerdict``，即 error_taxonomy → error_types 单向；反向 import 会形成循环。
    两者取值一致性由 backend/tests/test_error_types.py 断言守护。
    """

    AUTH = "auth"
    BILLING = "billing"
    MODERATION_INPUT = "moderation_input"
    MODERATION_OUTPUT = "moderation_output"
    CONTEXT_LENGTH = "context_length"
    BAD_REQUEST = "bad_request"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    NETWORK = "network"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class Confidence(Enum):
    """判定置信度（§3.1）

    - STRUCTURED：命中结构化码 / 码串扫描等可信通道
    - TEXT：文本兜底判定
    - NONE：没有判定（内部缺失 verdict 时按有限策略处理）

    取值即成员名（大写），JSON 形态自解释。
    与 error_taxonomy.ClassVerdict.confidence（"structured" / "text"）的映射：
    ``Confidence(cv.confidence.upper())``。
    """

    STRUCTURED = "STRUCTURED"
    TEXT = "TEXT"
    NONE = "NONE"


class SourceChannel(Enum):
    """判定来源通道（§3.1 / §6）——用于可信度判断与审计

    - SDK_ATTRIBUTE：SDK 结构化属性（异常对象的 code / status / body）
    - ERROR_ENVELOPE：约定的错误 envelope（确认来自响应体错误结构，而非正文）
    - STATUS_FALLBACK：HTTP 状态码兜底
    - TEXT_FALLBACK：文本兜底（可信度最低）

    禁止从任意显示文本解析错误码——小说正文本身也可能包含 {"code":1027}。
    """

    SDK_ATTRIBUTE = "SDK_ATTRIBUTE"
    ERROR_ENVELOPE = "ERROR_ENVELOPE"
    STATUS_FALLBACK = "STATUS_FALLBACK"
    TEXT_FALLBACK = "TEXT_FALLBACK"


class ErrorScope(Enum):
    """错误作用域（§3.5）——**恰好四个值**

    - ACCOUNT_GLOBAL：账户/端点级——绑定 endpoint + 不含密钥的凭据指纹
      （如 api_key 前 6 位 + sha256 前 8 位）；只有它能触发熔断
    - REQUEST_LOCAL：仅本次请求/本块受影响，不整场停机
    - TRANSIENT：临时性，可自由退避重试
    - UNKNOWN：作用域不明。**必须保留**，且不得凭错误字符串升级为账户熔断

    不得再出现 CALL / BLOCK / ACCOUNT；不允许自由字符串，不默认成 REQUEST_LOCAL。
    """

    ACCOUNT_GLOBAL = "ACCOUNT_GLOBAL"
    REQUEST_LOCAL = "REQUEST_LOCAL"
    TRANSIENT = "TRANSIENT"
    UNKNOWN = "UNKNOWN"


class UsageStatus(Enum):
    """Token 用量是否已知（§4）

    - KNOWN：拿到了 usage（可含流式中断前已知的部分）
    - UNKNOWN：未拿到 usage（断连 / 超时 / 解析失败）——此时 tokens 必须为 None
    """

    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# DTO 辅助（私有）
# ---------------------------------------------------------------------------

def _enum_value(raw: Any) -> Any:
    """取枚举的值：枚举实例 → .value；其它原样返回（容忍调用方直接塞字符串）"""
    return raw.value if isinstance(raw, Enum) else raw


def _enum_from_value(enum_cls, raw: Any, default):
    """按值解析枚举，未知 / 缺失值降级为 default（**不抛异常**）

    容忍顺序：已是枚举实例 → 精确值匹配 → 忽略大小写匹配 → default。
    """
    if isinstance(raw, enum_cls):
        return raw
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        return enum_cls(text)
    except ValueError:
        pass
    lowered = text.lower()
    for member in enum_cls:
        if member.name.lower() == lowered:
            return member
        if str(member.value).lower() == lowered:
            return member
    return default


def _optional_str(raw: Any) -> Optional[str]:
    """可空字符串：None → None；非字符串按 str() 归一（厂商码可能是数字）"""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    return str(raw)


def _optional_int(raw: Any) -> Optional[int]:
    """可空整数：无法解析 → None"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _optional_float(raw: Any) -> Optional[float]:
    """可空浮点数（retry_after 秒）：无法解析 → None"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _text(raw: Any, default: str = "") -> str:
    """文本：None → default；其余转 str"""
    if raw is None:
        return default
    return str(raw)


def _parse_tokens(raw: Any) -> Optional[Tuple[int, int]]:
    """解析 tokens：None / 非法 → None；[p, c] / (p, c) / {"prompt":..,"completion":..} → (int, int)

    未知用量一律返回 None——**不得**退化成 (0, 0)。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        prompt = raw.get("prompt", raw.get("prompt_tokens"))
        completion = raw.get("completion", raw.get("completion_tokens"))
        if prompt is None or completion is None:
            return None
        raw = (prompt, completion)
    if isinstance(raw, (list, tuple)):
        if len(raw) != 2:
            return None
        try:
            return (int(raw[0]), int(raw[1]))
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ErrorVerdict:
    """错误判定结论（§3.2 完整传输契约）

    字段：
    - cls：错误类别
    - confidence：判定置信度
    - source_channel：判定来自哪条通道——用于可信度判断与审计
    - provider_code：厂商原始码，如 "1027" / "insufficient_quota"
    - http_status：HTTP 状态码
    - action：stop / backoff / retry_after / continue，来自唯一的策略函数；
      消费方只读取，**不得**自行重新推导
    - scope：ACCOUNT_GLOBAL / REQUEST_LOCAL / TRANSIENT / UNKNOWN
    - retry_after：秒；来源见 §3.5（SDK_ATTRIBUTE 优先，ERROR_ENVELOPE 次之）
    - message：展示用，**不参与任何判定**

    默认值说明：provider_code / http_status / retry_after 缺省为 None；
    scope 缺省为 UNKNOWN（**不是** REQUEST_LOCAL，作用域不明时保守）；
    action 缺省为 "backoff"，对齐 error_taxonomy.RETRY_POLICY[UNKNOWN]——
    仅作兜底，实际调用方应显式传入策略函数的结果。
    """

    cls: ErrorClass
    confidence: Confidence
    source_channel: SourceChannel
    provider_code: Optional[str] = None
    http_status: Optional[int] = None
    action: str = "backoff"
    scope: ErrorScope = ErrorScope.UNKNOWN
    retry_after: Optional[float] = None
    message: str = ""

    # -- 永久停止判定 --------------------------------------------------------

    @property
    def is_permanent(self) -> bool:
        """是否"永久停止"结论：必须同时是结构化可信通道 + stop 类动作。

        这是判断永久停止的**唯一谓词**（语义与 error_taxonomy.ClassVerdict.is_permanent()
        一致）：文本通道的判定恒为 False——纯文本错误永远不能登记为致命块，
        避免把可自愈的失败误判成"重试成功率恒为零"（C-01 防线）。
        """
        confidence = self.confidence
        if not isinstance(confidence, Confidence):
            # 容错：容忍调用方直接塞字符串（如 "STRUCTURED"），未知取值降级为 NONE
            confidence = _enum_from_value(Confidence, confidence, Confidence.NONE)
        return confidence is Confidence.STRUCTURED and self.action == "stop"

    # -- 显式 JSON DTO（不依赖 dataclass 默认序列化形状）--------------------

    def to_dict(self) -> Dict[str, Any]:
        """转成 JSON 可用的 dict：枚举按值输出（字符串），缺省值输出 null"""
        return {
            "cls": _enum_value(self.cls),
            "confidence": _enum_value(self.confidence),
            "source_channel": _enum_value(self.source_channel),
            "provider_code": self.provider_code,
            "http_status": self.http_status,
            "action": self.action,
            "scope": _enum_value(self.scope),
            "retry_after": self.retry_after,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ErrorVerdict":
        """从 dict 还原；**未知枚举值降级而不抛异常**

        降级规则：cls → UNKNOWN，confidence → NONE，scope → UNKNOWN，
        source_channel → TEXT_FALLBACK（可信度最低的一档）。
        缺失字段按字段默认值补齐。
        """
        data = data or {}
        return cls(
            cls=_enum_from_value(ErrorClass, data.get("cls"), ErrorClass.UNKNOWN),
            confidence=_enum_from_value(Confidence, data.get("confidence"), Confidence.NONE),
            source_channel=_enum_from_value(
                SourceChannel, data.get("source_channel"), SourceChannel.TEXT_FALLBACK
            ),
            provider_code=_optional_str(data.get("provider_code")),
            http_status=_optional_int(data.get("http_status")),
            action=_text(data.get("action"), "backoff"),
            scope=_enum_from_value(ErrorScope, data.get("scope"), ErrorScope.UNKNOWN),
            retry_after=_optional_float(data.get("retry_after")),
            message=_text(data.get("message"), ""),
        )


@dataclass(frozen=True)
class LLMResult:
    """一次模型调用的结果（§3.3）

    字段：
    - success：是否成功
    - content：成功时的正文
    - error：展示用错误串，**不承载判定**
    - tokens：Optional[(prompt_tokens, completion_tokens)]；None = 未知（§4）
    - usage_status：KNOWN / UNKNOWN；tokens 为 None 时必须是 UNKNOWN
    - call_stats：调用统计（如 {"attempts": N, "failed_tokens": M}）
    - verdict：错误判定结论；成功时为 None
    - attempts：尝试次数

    **不实现 __iter__**，不提供位置式解包——旧元组形状在 S1 一次性删除。
    """

    success: bool
    content: str
    error: str
    tokens: Optional[Tuple[int, int]] = None
    usage_status: UsageStatus = UsageStatus.UNKNOWN
    call_stats: dict = field(default_factory=dict)
    verdict: Optional[ErrorVerdict] = None
    attempts: int = 1

    def __post_init__(self) -> None:
        """一致性校验：tokens 为 None 时 usage_status 必须是 UNKNOWN

        反方向（tokens 有值而 usage_status=UNKNOWN）表示"只知道部分"，
        §4 允许流式部分消费后中断这样表达，故不报错。
        """
        if self.tokens is None and _enum_value(self.usage_status) != "UNKNOWN":
            raise ValueError(
                "tokens 为 None 时 usage_status 必须是 UsageStatus.UNKNOWN"
                f"（当前 {self.usage_status!r}）；未知消耗不得写成 (0, 0)"
            )

    # -- 显式 JSON DTO（不依赖 dataclass 默认序列化形状）--------------------

    def to_dict(self) -> Dict[str, Any]:
        """转成 JSON 可用的 dict：枚举按值输出，tokens 输出 [p, c]（未知为 null）"""
        return {
            "success": bool(self.success),
            "content": self.content,
            "error": self.error,
            "tokens": list(self.tokens) if self.tokens is not None else None,
            "usage_status": _enum_value(self.usage_status),
            "call_stats": dict(self.call_stats or {}),
            "verdict": self.verdict.to_dict() if self.verdict is not None else None,
            "attempts": int(self.attempts),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "LLMResult":
        """从 dict 还原；**未知枚举值降级而不抛异常**

        降级规则：usage_status → UNKNOWN；tokens 缺失 / 非法 → None。
        tokens 为 None 时强制 usage_status=UNKNOWN（契约自洽优先于忠实还原）。

        usage_status 键缺失时按 tokens 推断（有值 → KNOWN）；键存在但取值不认识时
        一律降级为 UNKNOWN（不按 tokens 反推），避免把"未知"洗成"已知"。
        """
        data = data or {}
        tokens = _parse_tokens(data.get("tokens"))
        if "usage_status" in data:
            usage_status = _enum_from_value(
                UsageStatus, data.get("usage_status"), UsageStatus.UNKNOWN
            )
        else:
            usage_status = UsageStatus.KNOWN if tokens is not None else UsageStatus.UNKNOWN
        if tokens is None:
            usage_status = UsageStatus.UNKNOWN
        verdict_raw = data.get("verdict")
        verdict = ErrorVerdict.from_dict(verdict_raw) if isinstance(verdict_raw, dict) else None
        attempts_raw = data.get("attempts", 1)
        attempts = int(attempts_raw) if attempts_raw is not None else 1
        return cls(
            success=bool(data.get("success", False)),
            content=_text(data.get("content"), ""),
            error=_text(data.get("error"), ""),
            tokens=tokens,
            usage_status=usage_status,
            call_stats=dict(data.get("call_stats") or {}),
            verdict=verdict,
            attempts=attempts,
        )
