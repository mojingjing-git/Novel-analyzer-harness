"""error_types 单测（U1）

覆盖 docs/DESIGN-D01-verdict-transport-2026-10-30.md §3 与
docs/PLAN-v2-long-novel-reliability-2026-10-30.md §2.5 的类型契约：

- 五个枚举的取值集合（尤其 ErrorScope 恰好 4 个值）
- ErrorVerdict / LLMResult 的 to_dict → from_dict 往返相等
- frozen 不可赋值
- tokens=None → usage_status == UNKNOWN
- from_dict 遇到未知枚举字符串不抛异常（降级而不是抛错）
- ErrorClass 与 backend/core/error_taxonomy.py 的 11 类取值一致
- error_types 不得反向 import error_taxonomy / llm_stream
"""
import ast
import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core import error_types  # noqa: E402
from backend.core.error_taxonomy import ErrorClass as TaxonomyErrorClass  # noqa: E402
from backend.core.error_types import (  # noqa: E402
    Confidence,
    ErrorClass,
    ErrorScope,
    ErrorVerdict,
    LLMResult,
    SourceChannel,
    UsageStatus,
)


def _verdict(**over) -> ErrorVerdict:
    """构造一个字段齐全的 ErrorVerdict（测试基准样本）"""
    data = dict(
        cls=ErrorClass.RATE_LIMIT,
        confidence=Confidence.STRUCTURED,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        provider_code="1027",
        http_status=429,
        action="retry_after",
        scope=ErrorScope.ACCOUNT_GLOBAL,
        retry_after=1.5,
        message="展示用文案，不参与判定",
    )
    data.update(over)
    return ErrorVerdict(**data)


def _result(**over) -> LLMResult:
    """构造一个字段齐全的 LLMResult（测试基准样本）"""
    data = dict(
        success=False,
        content="",
        error="boom",
        tokens=(11, 22),
        usage_status=UsageStatus.KNOWN,
        call_stats={"attempts": 2, "failed_tokens": 3},
        verdict=_verdict(),
        attempts=2,
    )
    data.update(over)
    return LLMResult(**data)


# ===========================================================================
# 一、枚举取值集合
# ===========================================================================

def test_error_class_has_exactly_eleven_members():
    """ErrorClass 11 类（与 error_taxonomy 一致）"""
    assert len(ErrorClass) == 11
    assert {m.name for m in ErrorClass} == {
        "AUTH", "BILLING", "MODERATION_INPUT", "MODERATION_OUTPUT",
        "CONTEXT_LENGTH", "BAD_REQUEST", "RATE_LIMIT", "SERVER",
        "NETWORK", "TIMEOUT", "UNKNOWN",
    }


def test_error_class_values_match_error_taxonomy():
    """ErrorClass 取值集合必须与 backend/core/error_taxonomy.py 逐一一致

    这是"复用既有 11 类、不另立一套字符串"的守护断言。
    """
    assert {m.name: m.value for m in ErrorClass} == {
        m.name: m.value for m in TaxonomyErrorClass
    }


def test_confidence_values():
    """Confidence：STRUCTURED / TEXT / NONE"""
    assert {m.name: m.value for m in Confidence} == {
        "STRUCTURED": "STRUCTURED",
        "TEXT": "TEXT",
        "NONE": "NONE",
    }


def test_source_channel_values():
    """SourceChannel：SDK_ATTRIBUTE / ERROR_ENVELOPE / STATUS_FALLBACK / TEXT_FALLBACK"""
    assert {m.name: m.value for m in SourceChannel} == {
        "SDK_ATTRIBUTE": "SDK_ATTRIBUTE",
        "ERROR_ENVELOPE": "ERROR_ENVELOPE",
        "STATUS_FALLBACK": "STATUS_FALLBACK",
        "TEXT_FALLBACK": "TEXT_FALLBACK",
    }


def test_error_scope_has_exactly_four_members():
    """ErrorScope 恰好四个值（§3.5 / §2.5）"""
    assert len(ErrorScope) == 4
    assert {m.name: m.value for m in ErrorScope} == {
        "ACCOUNT_GLOBAL": "ACCOUNT_GLOBAL",
        "REQUEST_LOCAL": "REQUEST_LOCAL",
        "TRANSIENT": "TRANSIENT",
        "UNKNOWN": "UNKNOWN",
    }


def test_error_scope_forbids_legacy_names():
    """旧词汇 CALL / BLOCK / ACCOUNT 不得再出现"""
    names = {m.name for m in ErrorScope}
    assert names.isdisjoint({"CALL", "BLOCK", "ACCOUNT"})


def test_usage_status_values():
    """UsageStatus：KNOWN / UNKNOWN"""
    assert {m.name: m.value for m in UsageStatus} == {
        "KNOWN": "KNOWN",
        "UNKNOWN": "UNKNOWN",
    }


def test_error_scope_default_is_not_request_local():
    """scope 不得默认成 REQUEST_LOCAL（§3.5）"""
    assert ErrorVerdict(
        cls=ErrorClass.UNKNOWN,
        confidence=Confidence.NONE,
        source_channel=SourceChannel.TEXT_FALLBACK,
    ).scope is ErrorScope.UNKNOWN


# ===========================================================================
# 二、DTO 往返
# ===========================================================================

def test_error_verdict_to_dict_shape():
    """to_dict 是显式形状：枚举按值（字符串）输出，键名固定"""
    d = _verdict().to_dict()
    assert set(d) == {
        "cls", "confidence", "source_channel", "provider_code",
        "http_status", "action", "scope", "retry_after", "message",
    }
    assert d["cls"] == "rate_limit"
    assert d["confidence"] == "STRUCTURED"
    assert d["source_channel"] == "SDK_ATTRIBUTE"
    assert d["scope"] == "ACCOUNT_GLOBAL"
    assert d["http_status"] == 429
    assert d["retry_after"] == 1.5


def test_error_verdict_roundtrip():
    """ErrorVerdict：to_dict → from_dict 往返相等"""
    original = _verdict()
    restored = ErrorVerdict.from_dict(original.to_dict())
    assert restored == original


def test_error_verdict_roundtrip_with_none_fields():
    """可选字段为 None 时同样往返相等"""
    original = _verdict(provider_code=None, http_status=None, retry_after=None)
    restored = ErrorVerdict.from_dict(original.to_dict())
    assert restored == original
    assert restored.provider_code is None
    assert restored.http_status is None
    assert restored.retry_after is None


def test_llm_result_roundtrip():
    """LLMResult：to_dict → from_dict 往返相等（含嵌套 verdict）"""
    original = _result()
    restored = LLMResult.from_dict(original.to_dict())
    assert restored == original
    assert restored.verdict == original.verdict
    assert restored.tokens == (11, 22)


def test_llm_result_roundtrip_success_without_verdict():
    """成功结果（verdict 为 None、tokens 未知）同样往返相等"""
    original = LLMResult(
        success=True,
        content="正文",
        error="",
        tokens=None,
        usage_status=UsageStatus.UNKNOWN,
        call_stats={"attempts": 1},
        verdict=None,
        attempts=1,
    )
    restored = LLMResult.from_dict(original.to_dict())
    assert restored == original
    assert restored.verdict is None
    assert restored.tokens is None
    assert restored.usage_status is UsageStatus.UNKNOWN


def test_llm_result_to_dict_tokens_is_json_list():
    """tokens 在 JSON 形态下是 [p, c]，未知时是 null"""
    d = _result().to_dict()
    assert d["tokens"] == [11, 22]
    assert LLMResult.from_dict(d).tokens == (11, 22)
    unknown = LLMResult(success=False, content="", error="x", tokens=None).to_dict()
    assert unknown["tokens"] is None


def test_dto_is_json_serializable():
    """DTO 输出必须是 JSON 可序列化的（json.dumps 不报错）"""
    import json

    payload = _result().to_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


# ===========================================================================
# 三、frozen 不可赋值
# ===========================================================================

def test_error_verdict_is_frozen():
    """ErrorVerdict 是 frozen dataclass，赋值抛 FrozenInstanceError"""
    v = _verdict()
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.cls = ErrorClass.AUTH
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.message = "改"


def test_llm_result_is_frozen():
    """LLMResult 是 frozen dataclass，赋值抛 FrozenInstanceError"""
    r = _result()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.success = True
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.attempts = 9


def test_llm_result_is_not_tuple_like():
    """LLMResult 不实现 __iter__，不提供位置式解包（§3.3）"""
    r = _result()
    assert not hasattr(type(r), "__iter__")
    assert not hasattr(type(r), "__getitem__")
    with pytest.raises(TypeError):
        tuple(r)
    with pytest.raises(TypeError):
        _ok, _content = r  # noqa: F841  —— 位置式解包必须失败


# ===========================================================================
# 四、usage 未知的一致性
# ===========================================================================

def test_tokens_none_defaults_to_unknown_usage():
    """tokens=None 时 usage_status 必须是 UNKNOWN"""
    r = LLMResult(success=False, content="", error="断连", tokens=None)
    assert r.tokens is None
    assert r.usage_status is UsageStatus.UNKNOWN


def test_tokens_none_with_known_usage_is_rejected():
    """tokens=None 但 usage_status=KNOWN —— 契约违规，构造时即报错"""
    with pytest.raises(ValueError):
        LLMResult(
            success=False,
            content="",
            error="断连",
            tokens=None,
            usage_status=UsageStatus.KNOWN,
        )


def test_tokens_known_with_known_usage():
    """有 usage：tokens=(p, c) + usage_status=KNOWN"""
    r = LLMResult(
        success=True,
        content="x",
        error="",
        tokens=(1, 2),
        usage_status=UsageStatus.KNOWN,
    )
    assert r.tokens == (1, 2)
    assert r.usage_status is UsageStatus.KNOWN


def test_from_dict_tokens_none_forces_unknown_usage():
    """from_dict：tokens 缺失 / 为 null → usage_status 降级为 UNKNOWN"""
    r = LLMResult.from_dict({"success": False, "error": "x", "tokens": None})
    assert r.tokens is None
    assert r.usage_status is UsageStatus.UNKNOWN


def test_from_dict_infers_usage_status_when_key_absent():
    """usage_status 键缺失时按 tokens 推断：有 tokens → KNOWN"""
    r = LLMResult.from_dict({"success": True, "content": "x", "tokens": [1, 2]})
    assert r.usage_status is UsageStatus.KNOWN
    assert r.tokens == (1, 2)


# ===========================================================================
# 五、from_dict 容忍未知枚举
# ===========================================================================

def test_from_dict_unknown_enum_values_do_not_raise():
    """未知枚举字符串不抛异常，一律降级"""
    verdict = ErrorVerdict.from_dict({
        "cls": "NOT_A_CLASS",
        "confidence": "VERY_CONFIDENT",
        "source_channel": "MAGIC",
        "scope": "ACCOUNT",  # 旧词汇也按未知处理
        "action": "stop",
        "message": "x",
    })
    assert verdict.cls is ErrorClass.UNKNOWN
    assert verdict.confidence is Confidence.NONE
    assert verdict.source_channel is SourceChannel.TEXT_FALLBACK
    assert verdict.scope is ErrorScope.UNKNOWN


def test_from_dict_unknown_usage_status_does_not_raise():
    """LLMResult.from_dict：未知 usage_status 不抛异常，降级为 UNKNOWN"""
    r = LLMResult.from_dict({
        "success": False,
        "error": "x",
        "tokens": [1, 2],
        "usage_status": "MAYBE",
    })
    assert r.usage_status is UsageStatus.UNKNOWN
    assert r.tokens == (1, 2)


def test_from_dict_missing_fields_uses_defaults():
    """字段缺失按默认值补齐，不抛异常"""
    verdict = ErrorVerdict.from_dict({})
    assert verdict.cls is ErrorClass.UNKNOWN
    assert verdict.confidence is Confidence.NONE
    assert verdict.source_channel is SourceChannel.TEXT_FALLBACK
    assert verdict.scope is ErrorScope.UNKNOWN
    assert verdict.provider_code is None
    assert verdict.http_status is None
    assert verdict.retry_after is None

    r = LLMResult.from_dict({})
    assert r.success is False
    assert r.content == ""
    assert r.error == ""
    assert r.tokens is None
    assert r.usage_status is UsageStatus.UNKNOWN
    assert r.call_stats == {}
    assert r.verdict is None
    assert r.attempts == 1


def test_from_dict_none_input_is_safe():
    """from_dict(None) 不抛异常"""
    assert ErrorVerdict.from_dict(None).cls is ErrorClass.UNKNOWN
    assert LLMResult.from_dict(None).usage_status is UsageStatus.UNKNOWN


def test_from_dict_tolerates_bad_scalar_types():
    """标量类型不合法时降级而不是抛异常"""
    verdict = ErrorVerdict.from_dict({
        "http_status": "不是数字",
        "retry_after": {},
        "provider_code": 1027,
    })
    assert verdict.http_status is None
    assert verdict.retry_after is None
    assert verdict.provider_code == "1027"


# ===========================================================================
# 六、模块边界（不得反向依赖）
# ===========================================================================

def test_error_types_does_not_import_upstream_modules():
    """error_types 是叶子：不得 import error_taxonomy / llm_stream / llm_client"""
    source = Path(error_types.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
    forbidden = {"error_taxonomy", "llm_stream", "llm_client"}
    assert not any(
        name.split(".")[-1] in forbidden for name in imported
    ), f"error_types 出现反向依赖：{sorted(imported)}"
