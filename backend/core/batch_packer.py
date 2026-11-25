"""逐章批量分析装包与思考状态判定。

在请求层将连续多个章节按字符预算、输出 Token 容量与 ``block_size`` 上限
动态打包为一批，落盘阶段仍还原为标准单章结果。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

from ..config.constants import (
    BATCH_EST_OUT_TOKENS_PER_CHAPTER,
    BATCH_EST_OUT_TOKENS_PER_CHAPTER_THINKING,
    BATCH_EST_OUT_TOKENS_PER_CHAPTER_THINKING_HIGH,
    BATCH_EST_OUT_TOKENS_PER_CHAPTER_THINKING_MEDIUM,
    BATCH_OUTPUT_BUDGET_RATIO,
)
from .model_catalog import lookup_model_spec

_WRAPPER_OVERHEAD_CHARS = 60


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def is_thinking_enabled(api_config: Any) -> bool:
    """判定当前 API 配置与模型是否实际开启推理思考。

    优先级：
    1. ``thinking_mode`` / ``reasoning_effort`` 中的显式关闭标记直接返回 ``False``；
    2. 显式开启标记或非 ``none`` 深度档位直接返回 ``True``；
    3. 全空配置时查询模型能力目录，避免常开推理模型被误判为关闭。
    """
    tm_raw = _get_field(api_config, "thinking_mode", None)
    tm: Mapping[str, Any] = tm_raw if isinstance(tm_raw, Mapping) else {}

    raw_effort = tm.get("reasoning_effort")
    if raw_effort is None or str(raw_effort).strip() == "":
        raw_effort = _get_field(api_config, "reasoning_effort", "")
    effort = str(raw_effort or "").strip().lower()

    thinking_obj = tm.get("thinking")
    thinking_type = (
        str(thinking_obj.get("type") or "").strip().lower()
        if isinstance(thinking_obj, Mapping)
        else ""
    )
    tpl_kwargs = tm.get("chat_template_kwargs")
    tpl_enable = (
        tpl_kwargs.get("enable_thinking")
        if isinstance(tpl_kwargs, Mapping)
        else None
    )
    reasoning_obj = tm.get("reasoning")
    reasoning_sub_effort = (
        str(reasoning_obj.get("effort") or "").strip().lower()
        if isinstance(reasoning_obj, Mapping)
        else ""
    )

    # 1. 显式关闭优先
    if (
        tm.get("enable_thinking") is False
        or thinking_type == "disabled"
        or tpl_enable is False
        or effort == "none"
        or reasoning_sub_effort == "none"
    ):
        return False

    # 2. 显式开启 / 指定思考深度
    if (
        tm.get("enable_thinking") is True
        or thinking_type == "enabled"
        or tpl_enable is True
        or bool(effort)
        or bool(reasoning_sub_effort)
    ):
        return True

    # 3. 空配置兜底：识别常开或默认启用思考的推理模型
    model = str(_get_field(api_config, "model", "") or "").strip()
    if not model:
        return False
    spec = lookup_model_spec(model)
    if not spec:
        return False
    if spec.get("is_always_on") is True:
        return True
    if spec.get("reasoning") is True:
        provider = str(_get_field(api_config, "provider", "") or "").strip().lower()
        base_url = str(_get_field(api_config, "base_url", "") or "").strip().lower()
        api_key = str(_get_field(api_config, "api_key", "") or "").strip()
        is_anthropic = (
            provider == "anthropic"
            or (
                provider in ("", "auto")
                and (
                    "anthropic" in base_url
                    or api_key.startswith("sk-ant-")
                    or model.lower().startswith("claude-")
                )
            )
        )
        return not is_anthropic
    return False


def resolve_thinking_effort(api_config: Any) -> str:
    """解析实际生效的思考深度档位（``""`` / ``"low"`` / ``"medium"`` / ``"high"``）。"""
    if not is_thinking_enabled(api_config):
        return ""
    tm_raw = _get_field(api_config, "thinking_mode", None)
    tm: Mapping[str, Any] = tm_raw if isinstance(tm_raw, Mapping) else {}

    raw_effort = tm.get("reasoning_effort")
    if raw_effort is None or str(raw_effort).strip() == "":
        raw_effort = _get_field(api_config, "reasoning_effort", "")
    effort = str(raw_effort or "").strip().lower()
    if not effort:
        reasoning_obj = tm.get("reasoning")
        if isinstance(reasoning_obj, Mapping):
            effort = str(reasoning_obj.get("effort") or "").strip().lower()
    if effort in ("high", "xhigh", "max"):
        return "high"
    if effort == "medium":
        return "medium"
    if effort == "low":
        return "low"

    thinking_obj = tm.get("thinking")
    if isinstance(thinking_obj, Mapping):
        budget = thinking_obj.get("budget_tokens")
        if isinstance(budget, (int, float)) and not isinstance(budget, bool):
            if budget >= 16384:
                return "high"
            if budget >= 8192:
                return "medium"
    return "low"


def effective_batch_cap(
    block_size: int,
    max_tokens: int | None,
    thinking_enabled: bool,
    *,
    reasoning_effort: str = "",
) -> int:
    """结合 ``block_size`` 与模型输出上限计算单批最多容纳的章节数。"""
    if max_tokens is None or int(max_tokens) <= 0:
        return 1
    bs = max(1, int(block_size or 1))
    if not thinking_enabled:
        per_ch = BATCH_EST_OUT_TOKENS_PER_CHAPTER
    else:
        eff = str(reasoning_effort or "").strip().lower()
        if eff in ("high", "xhigh", "max"):
            per_ch = BATCH_EST_OUT_TOKENS_PER_CHAPTER_THINKING_HIGH
        elif eff == "medium":
            per_ch = BATCH_EST_OUT_TOKENS_PER_CHAPTER_THINKING_MEDIUM
        else:
            per_ch = BATCH_EST_OUT_TOKENS_PER_CHAPTER_THINKING
    max_by_out = max(
        1,
        int((int(max_tokens) * BATCH_OUTPUT_BUDGET_RATIO) // max(1, per_ch)),
    )
    return max(1, min(bs, max_by_out))


def pack_chapter_batches(
    chapters: Sequence[int],
    chapter_lengths: Mapping[int, int],
    *,
    block_size: int,
    max_content_chars: int,
    max_tokens: int | None,
    thinking_enabled: bool,
    reasoning_effort: str = "",
    break_on_gap: bool = False,
) -> List[List[int]]:
    """按批大小、正文字符上限与预估输出预算将章节列表装包。

    单章自身超过 ``max_content_chars`` 时独立成 ``[ch]``，不在批内跨章截断。
    当 ``break_on_gap=True`` 且相邻章号不连续时自动断开新开一批，避免跨章号断层混批。
    """
    cap = effective_batch_cap(
        block_size,
        max_tokens,
        thinking_enabled,
        reasoning_effort=reasoning_effort,
    )
    char_cap = max(1, int(max_content_chars or 1))

    batches: List[List[int]] = []
    cur: List[int] = []
    cur_chars = 0
    for ch in chapters:
        txt_len = max(0, int(chapter_lengths.get(ch, 0) or 0))
        wrapped_len = txt_len + _WRAPPER_OVERHEAD_CHARS
        gap_break = bool(break_on_gap and cur and int(ch) != int(cur[-1]) + 1)
        if cur and (gap_break or len(cur) >= cap or cur_chars + wrapped_len > char_cap):
            batches.append(cur)
            cur = []
            cur_chars = 0
        cur.append(ch)
        cur_chars += wrapped_len
    if cur:
        batches.append(cur)
    return batches
