"""Prompt 上下文限额：统一定义与配置校验。

用例按"配置实际会犯的错"组织：写错类型（bool/float/字符串）、写错符号（0/负数）、
整个 payload 形状不对、带了新版本的未知键。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import dataclasses
import json

import pytest

from backend.core import prompt_limits
from backend.core.prompt_limits import (
    DEFAULT_LIMITS,
    LIMIT_FIELDS,
    PromptLimits,
    from_dict,
    to_dict,
)


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------


def test_default_limits_match_documented_values() -> None:
    """9 个默认值等于文档里写死的那 9 个数字。"""
    assert DEFAULT_LIMITS.max_arcs == 30
    assert DEFAULT_LIMITS.max_summaries == 5
    assert DEFAULT_LIMITS.max_character_states == 20
    assert DEFAULT_LIMITS.max_world_items == 20
    assert DEFAULT_LIMITS.max_foreshadow_entries == 5
    assert DEFAULT_LIMITS.max_relationships == 15
    assert DEFAULT_LIMITS.max_verified_facts == 15
    assert DEFAULT_LIMITS.max_themes == 8
    assert DEFAULT_LIMITS.max_content_chars == 30000


def test_default_limits_cover_exactly_the_nine_fields() -> None:
    assert set(LIMIT_FIELDS) == {
        "max_arcs",
        "max_summaries",
        "max_character_states",
        "max_world_items",
        "max_foreshadow_entries",
        "max_relationships",
        "max_verified_facts",
        "max_themes",
        "max_content_chars",
    }
    assert len(LIMIT_FIELDS) == 9


def test_empty_payload_returns_all_defaults() -> None:
    assert from_dict({}) == DEFAULT_LIMITS


# ---------------------------------------------------------------------------
# 部分覆盖与未知键
# ---------------------------------------------------------------------------


def test_partial_override_only_changes_the_given_field() -> None:
    limits = from_dict({"max_arcs": 12})
    assert limits.max_arcs == 12
    assert limits.max_summaries == DEFAULT_LIMITS.max_summaries
    assert limits.max_character_states == DEFAULT_LIMITS.max_character_states
    assert limits.max_content_chars == DEFAULT_LIMITS.max_content_chars


def test_unknown_keys_are_ignored_and_absent_from_result() -> None:
    limits = from_dict({"max_arcs": 8, "future_option": 99, "max_prompts": 3})
    assert limits.max_arcs == 8
    assert not hasattr(limits, "future_option")
    assert not hasattr(limits, "max_prompts")
    assert "future_option" not in to_dict(limits)
    assert "max_prompts" not in to_dict(limits)


def test_unknown_key_with_illegal_value_is_not_validated() -> None:
    """未知键完全不被校验——它连"是不是数字"都不重要。"""
    limits = from_dict({"totally_unknown": "not-a-number", "max_themes": 3})
    assert limits.max_themes == 3
    assert set(to_dict(limits)) == set(LIMIT_FIELDS)


# ---------------------------------------------------------------------------
# 非法取值
# ---------------------------------------------------------------------------


def test_bool_true_is_rejected() -> None:
    """bool 是 int 的子类，但 ``True`` 不能被当成 1 悄悄接受。"""
    with pytest.raises(ValueError, match="max_arcs"):
        from_dict({"max_arcs": True})


def test_bool_false_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_summaries"):
        from_dict({"max_summaries": False})


def test_float_is_rejected_even_when_integral() -> None:
    """``2.0`` 说明配置写错了，不做隐式转换。"""
    with pytest.raises(ValueError, match="max_character_states"):
        from_dict({"max_character_states": 2.0})


def test_non_integral_float_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_themes"):
        from_dict({"max_themes": 8.5})


def test_numeric_string_is_rejected() -> None:
    """字符串数字不做隐式转换。"""
    with pytest.raises(ValueError, match="max_arcs"):
        from_dict({"max_arcs": "30"})


def test_zero_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_arcs"):
        from_dict({"max_arcs": 0})


def test_negative_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_relationships"):
        from_dict({"max_relationships": -5})


def test_none_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_content_chars"):
        from_dict({"max_content_chars": None})


def test_error_message_names_field_and_received_value() -> None:
    """错误消息必须说清是哪个字段、收到什么值。"""
    with pytest.raises(ValueError) as excinfo:
        from_dict({"max_themes": 0})
    message = str(excinfo.value)
    assert "max_themes" in message
    assert "0" in message
    assert "正整数" in message


def test_error_message_reports_the_offending_field_only() -> None:
    with pytest.raises(ValueError) as excinfo:
        from_dict({"max_verified_facts": -5, "max_arcs": 30})
    assert "max_verified_facts" in str(excinfo.value)
    assert "-5" in str(excinfo.value)


# ---------------------------------------------------------------------------
# payload 形状
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [[], ["max_arcs"], None, "max_arcs=30", 30, 3.5, object()],
    ids=["list", "list_of_str", "none", "str", "int", "float", "object"],
)
def test_non_mapping_payload_is_rejected(payload: object) -> None:
    with pytest.raises(ValueError, match="Mapping"):
        from_dict(payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 序列化与往返
# ---------------------------------------------------------------------------


def test_to_dict_returns_the_nine_known_keys() -> None:
    data = to_dict(DEFAULT_LIMITS)
    assert set(data) == set(LIMIT_FIELDS)
    assert data["max_arcs"] == 30
    assert data["max_content_chars"] == 30000


def test_to_dict_is_json_serializable() -> None:
    encoded = json.dumps(to_dict(from_dict({"max_arcs": 7})))
    assert json.loads(encoded)["max_arcs"] == 7


def test_to_dict_from_dict_roundtrip_is_lossless() -> None:
    original = PromptLimits(
        max_arcs=1,
        max_summaries=2,
        max_character_states=3,
        max_world_items=4,
        max_foreshadow_entries=5,
        max_relationships=6,
        max_verified_facts=7,
        max_themes=8,
        max_content_chars=9,
    )
    assert from_dict(to_dict(original)) == original


# ---------------------------------------------------------------------------
# describe 快照与不可变性
# ---------------------------------------------------------------------------


def test_describe_snapshot_of_defaults() -> None:
    assert DEFAULT_LIMITS.describe() == (
        "arcs=30 summaries=5 states=20 world=20 foreshadow=5 "
        "relations=15 facts=15 themes=8 chars=30000"
    )


def test_describe_snapshot_of_overridden_limits() -> None:
    limits = from_dict({"max_arcs": 11, "max_content_chars": 1200})
    assert limits.describe() == (
        "arcs=11 summaries=5 states=20 world=20 foreshadow=5 "
        "relations=15 facts=15 themes=8 chars=1200"
    )


def test_describe_is_single_line() -> None:
    assert "\n" not in DEFAULT_LIMITS.describe()


def test_dataclass_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_LIMITS.max_arcs = 99  # type: ignore[misc]


def test_vnext_shim_reexports_core_objects() -> None:
    """vnext 转发桩必须原样转发同一批对象（不产生第二份定义）。"""
    from backend.vnext import prompt_limits as shim

    assert shim.DEFAULT_LIMITS is DEFAULT_LIMITS
    assert shim.from_dict is from_dict
    assert shim.to_dict is to_dict
