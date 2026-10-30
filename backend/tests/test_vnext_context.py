"""复检上下文取材（S6-4）：先扩到句子边界，再补目标，并如实交代截断。

关键区分：**到达章节尽头**不是截断；**撞硬上限**才是截断，且必须让下游知道
"还有材料但没给"——否则模型会拿半句话当全部证据。
"""

from __future__ import annotations

import pytest

from backend.vnext.context import (
    HARD_MAX_CHARS,
    build_context,
    context_covers,
)

TEXT = (
    "第一章　夜谈\n"
    "「你是不是杀了人？」少年盯着他。\n"
    "他没有回答。雨声渐密。\n"
    "过了很久，他才开口：「有些事，答了就是错。」\n"
)


def _index(needle: str) -> tuple[int, int]:
    start = TEXT.index(needle)
    return start, start + len(needle)


# ---------------------------------------------------------------------------
# 句子边界
# ---------------------------------------------------------------------------


def test_window_starts_at_sentence_boundary_not_mid_sentence() -> None:
    start, end = _index("有些事")
    window = build_context(TEXT, span_start=start, span_end=end, pre_target=3, post_target=0)
    # 即使目标只有 3 字，也必须扩到句子开头，不能把"过了很久，他才开口"切断
    assert TEXT[window.start:].startswith("过了很久")


def test_window_ends_after_sentence_delimiter() -> None:
    start, end = _index("他没有回答")
    window = build_context(TEXT, span_start=start, span_end=end, pre_target=0, post_target=0)
    assert "他没有回答。" in window.text
    assert window.text.endswith("。") or "雨声渐密" in window.text


def test_newline_counts_as_sentence_boundary() -> None:
    start, end = _index("第一章")
    window = build_context(TEXT, span_start=start, span_end=end, pre_target=0, post_target=0)
    assert not window.text.startswith("\n")


# ---------------------------------------------------------------------------
# 目标长度补充
# ---------------------------------------------------------------------------


def test_targets_expand_beyond_the_sentence() -> None:
    """句子边界之外还要按目标继续补——否则上下文太窄，判断没有依据。"""
    start, end = _index("他没有回答")
    narrow = build_context(TEXT, span_start=start, span_end=end, pre_target=0, post_target=0)
    wide = build_context(TEXT, span_start=start, span_end=end, pre_target=60, post_target=60)
    assert wide.chars_used > narrow.chars_used


def test_clamped_at_chapter_start() -> None:
    window = build_context(TEXT, span_start=0, span_end=5, pre_target=500, post_target=500)
    assert window.start == 0
    assert window.truncated is False


def test_clamped_at_chapter_end() -> None:
    end = len(TEXT)
    window = build_context(TEXT, span_start=end - 3, span_end=end, pre_target=500, post_target=500)
    assert window.end == end
    assert window.truncated is False   # 到尽头不是截断


# ---------------------------------------------------------------------------
# 硬上限
# ---------------------------------------------------------------------------


def test_hard_cap_marks_truncated_with_reason() -> None:
    long_text = "甲" * 5000 + "目标句。" + "乙" * 5000
    start = long_text.index("目标句")
    window = build_context(long_text, span_start=start, span_end=start + 3, pre_target=3000,
                           post_target=3000, hard_max=HARD_MAX_CHARS)
    assert window.truncated is True
    assert window.reason == "HARD_BUDGET"
    assert window.chars_used <= HARD_MAX_CHARS


def test_hard_cap_never_cuts_the_quote_itself() -> None:
    """引用被切掉的话，这条证据就没法核对了——必须保住引用完整。"""
    long_text = "甲" * 3000 + "关键引文在此。" + "乙" * 3000
    start = long_text.index("关键引文")
    end = start + 6
    window = build_context(long_text, span_start=start, span_end=end, pre_target=3000,
                           post_target=3000, hard_max=1000)
    assert context_covers(window, span_start=start, span_end=end)
    assert window.text[start - window.start:end - window.start] == "关键引文在此"


def test_quote_longer_than_hard_max_is_flagged() -> None:
    long_text = "甲" * 500 + "很长的引文" * 300 + "乙" * 500
    start = 500
    end = start + len("很长的引文") * 300
    window = build_context(long_text, span_start=start, span_end=end, hard_max=500)
    assert window.truncated is True
    assert window.reason == "QUOTE_EXCEEDS_HARD_MAX"


# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------


def test_chars_used_matches_text_length() -> None:
    start, end = _index("雨声渐密")
    window = build_context(TEXT, span_start=start, span_end=end)
    assert window.chars_used == len(window.text)
    assert window.text == TEXT[window.start:window.end]


def test_out_of_range_span_raises() -> None:
    with pytest.raises(IndexError):
        build_context(TEXT, span_start=-1, span_end=3)
    with pytest.raises(IndexError):
        build_context(TEXT, span_start=2, span_end=1)


def test_complete_property_reflects_truncation() -> None:
    start, end = _index("他没有回答")
    window = build_context(TEXT, span_start=start, span_end=end)
    assert window.complete is True
    assert context_covers(window, span_start=start, span_end=end)
