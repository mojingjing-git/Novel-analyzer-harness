"""请求体里的"非标准透传字段"：思考开关 + 推理档位。

- ``thinking_mode`` 决定**要不要思考**、按各家协议怎么关；
- ``reasoning_effort`` 决定**想多深**（low/medium/high/xhigh/max）。

两者是不同的事，但都走 ``extra_body`` 透传：OpenAI SDK 的 ``create()`` 只认自己
签名里的参数，写死档位会直接 TypeError。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.core.llm_client import _extra_body


@dataclass
class _Cfg:
    thinking_mode: dict = field(default_factory=dict)
    reasoning_effort: str = ""


def test_empty_config_produces_no_body():
    assert _extra_body(_Cfg()) == {}


def test_thinking_mode_is_passed_through():
    body = _extra_body(_Cfg(thinking_mode={"enable_thinking": False}))

    assert body == {"enable_thinking": False}


def test_reasoning_effort_is_passed_through():
    body = _extra_body(_Cfg(reasoning_effort="low"))

    assert body == {"reasoning_effort": "low"}


def test_both_can_coexist():
    body = _extra_body(
        _Cfg(thinking_mode={"thinking": {"type": "disabled"}}, reasoning_effort="low")
    )

    assert body["thinking"] == {"type": "disabled"}
    assert body["reasoning_effort"] == "low"


def test_blank_effort_is_ignored():
    """空字符串 = 不传，交给服务端默认（而不是传一个空档位）。"""
    assert _extra_body(_Cfg(reasoning_effort="  ")) == {}
    assert _extra_body(_Cfg(reasoning_effort="")) == {}
