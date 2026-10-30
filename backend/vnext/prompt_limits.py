"""Prompt 上下文限额：9 个上限的统一定义与配置校验。

**为什么收进一个不可变 dataclass**：旧代码里这 9 个上限是 ``prompt_builder.py`` 里的
散装硬编码数字（max_arcs=30、max_summaries=5、max_character_states=20、max_world_items=20、
max_foreshadow_entries=5、max_relationships=15、max_verified_facts=15、max_themes=8、
max_content_chars=30000）。散装数字会漂移：其中 4 个在 analyzer 里根本没被传进去，
于是"配置了也不生效"——配置层看起来支持，实际链路不消费。

把它们收成一处定义后，"上限是多少"只有一个答案，"配置能不能生效"也就只有一个入口。

**本期定位**：内部设置。不新增任何用户可见的设置项，只有测试与构建入口会用它。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from .constants import ErrorCode, VnextFormatError

__all__ = [
    "PromptLimits",
    "DEFAULT_LIMITS",
    "LIMIT_FIELDS",
    "from_dict",
    "to_dict",
]

#: ``describe()`` 里每个字段的短名，顺序固定，便于快照断言。
_DESCRIBE_ALIASES: tuple[tuple[str, str], ...] = (
    ("max_arcs", "arcs"),
    ("max_summaries", "summaries"),
    ("max_character_states", "states"),
    ("max_world_items", "world"),
    ("max_foreshadow_entries", "foreshadow"),
    ("max_relationships", "relations"),
    ("max_verified_facts", "facts"),
    ("max_themes", "themes"),
    ("max_content_chars", "chars"),
)


@dataclass(frozen=True)
class PromptLimits:
    """逐章分析 Prompt 的上下文上限（全部为"取前 N 条"的上限）。

    字段名与旧 ``prompt_builder.py`` 的参数名一致，便于对照迁移。
    全部为**严格正整数**：0 与负数没有意义（取 0 条等于什么都不分析）。
    """

    max_arcs: int = 30
    max_summaries: int = 5
    max_character_states: int = 20
    max_world_items: int = 20
    max_foreshadow_entries: int = 5
    max_relationships: int = 15
    max_verified_facts: int = 15
    max_themes: int = 8
    max_content_chars: int = 30000

    def describe(self) -> str:
        """单行摘要，字段顺序固定。

        例如 ``arcs=30 summaries=5 states=20 world=20 foreshadow=5 relations=15
        facts=15 themes=8 chars=30000``。用于快照测试与日志。
        """
        return " ".join(
            f"{alias}={getattr(self, name)}" for name, alias in _DESCRIBE_ALIASES
        )


#: 默认限额。构建入口可以直接拿它当基线。
DEFAULT_LIMITS = PromptLimits()

#: 全部受支持的字段名，顺序即 :meth:`PromptLimits.describe` 的输出顺序。
LIMIT_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(PromptLimits))


def _reject(field: str, value: Any) -> VnextFormatError:
    """构造"该字段的取值非法"错误。

    detail 必须同时说清**是哪个字段**与**收到什么值**：配置出错时，这是调用方
    唯一能据此定位到配置文件那一行的信息。
    """
    return VnextFormatError(
        ErrorCode.INVALID_FIELD_VALUE,
        f"{field} 必须是正整数，收到 {value!r}",
        path=f"prompt_limits.{field}",
    )


def from_dict(payload: Mapping[str, object]) -> PromptLimits:
    """从 JSON dict 构造限额对象。

    规则：

    - 只认识 9 个已知键；**未知键一律忽略**（配置可能来自更新的版本，
      旧实现不该因此拒绝整份配置），返回对象不含它们。
    - 每个已知键的值必须是**严格正整数**：``bool`` 拒绝（``True`` 会被
      ``int()`` 悄悄变成 1），``float`` 即使等于整数也拒绝（``2.0`` 说明
      写错了），字符串数字拒绝（不做隐式转换），``0`` 与负数拒绝。
    - ``payload`` 不是 ``Mapping`` 时同样拒绝。

    非法取值抛 :class:`VnextFormatError`，``code`` 为
    :attr:`ErrorCode.INVALID_FIELD_VALUE`。
    """
    if not isinstance(payload, Mapping):
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE,
            f"prompt_limits 必须是 JSON 对象（Mapping），收到 {type(payload).__name__}",
            path="prompt_limits",
        )

    values: dict[str, int] = {}
    for name in LIMIT_FIELDS:
        if name not in payload:
            continue
        raw = payload[name]
        # bool 是 int 的子类，必须在 isinstance(raw, int) 之前显式排除，
        # 否则 True 会变成 1、"写了什么就是什么"的约定会被悄悄破坏。
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise _reject(name, raw)
        if raw <= 0:
            raise _reject(name, raw)
        values[name] = raw

    return PromptLimits(**values)


def to_dict(limits: PromptLimits) -> dict:
    """把限额对象序列化成 9 键的普通 dict（可直接 JSON 序列化）。"""
    return {name: getattr(limits, name) for name in LIMIT_FIELDS}
