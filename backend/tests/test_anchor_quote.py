"""伏笔锚点校验：定位、规范化、章归属、跨章拒绝。

这些用例守的是同一条底线：**没有逐字证据就不算证据**。
"""

from __future__ import annotations

from backend.core.anchor import (
    MIN_ANCHOR_CHARS,
    build_chapter_spans,
    locate_anchor,
    verify_result_anchors,
)
from backend.models.analysis_result import AnalysisResult, Foreshadowing

BLOCK = (
    "第一章 起\n"
    "方醒睁眼，先摸了摸怀里的铜匣。\n"
    "第二章 承\n"
    "铜匣打开，里面只有一张旧契。\n"
    "纸角已经发脆，字迹却清楚。\n"
)


def _result(*quotes: str) -> AnalysisResult:
    return AnalysisResult(
        chapter_number=1,
        foreshadowing=[Foreshadowing(clue=f"线索{i}", anchor_quote=q) for i, q in enumerate(quotes)],
    )


def test_build_spans_splits_on_chapter_headings():
    spans = build_chapter_spans(BLOCK, 1)
    assert [c for c, _s, _e in spans] == [1, 2]
    assert spans[0][1] == 0
    assert spans[1][1] == spans[0][2]


def test_build_spans_without_heading_is_single_block_span():
    text = "没有任何标题的一段正文。"
    assert build_chapter_spans(text, 7) == [(7, 0, len(text))]


def test_locate_exact_returns_true_offsets():
    hit = locate_anchor(BLOCK, "铜匣打开，里面只有一张旧契。")
    assert hit is not None
    start, end, kind = hit
    assert BLOCK[start:end] == "铜匣打开，里面只有一张旧契。"
    assert kind == "EXACT"


def test_locate_tolerates_whitespace_differences():
    # 模型把换行吞掉：规范化后仍应命中，且返回的是**原文**坐标
    hit = locate_anchor(BLOCK, "纸角已经发脆，字迹却清楚。")
    assert hit is not None
    assert BLOCK[hit[0]:hit[1]].startswith("纸角已经发脆")


def test_locate_rejects_too_short_quote():
    assert len("铜匣打开") == 4 < MIN_ANCHOR_CHARS
    assert locate_anchor(BLOCK, "铜匣打开") is None


def test_locate_rejects_absent_quote():
    assert locate_anchor(BLOCK, "这句话原文里根本没有出现过。") is None


def test_verify_writes_back_original_slice_and_chapter():
    result = _result("铜匣打开，里面只有一张旧契。")
    stats = verify_result_anchors(result, BLOCK, 1)
    item = result.foreshadowing[0]
    assert stats["located"] == 1 and stats["missing"] == 0
    assert item.anchor_quote == "铜匣打开，里面只有一张旧契。"
    assert item.anchor_chapter == 2  # 由块内标题反查，不是块首章


def test_verify_clears_fabricated_quote():
    result = _result("方醒其实是前朝太子，血书为证。")
    stats = verify_result_anchors(result, BLOCK, 1)
    assert stats == {"total": 1, "located": 0, "missing": 1, "by_kind": {}}
    assert result.foreshadowing[0].anchor_quote == ""
    assert result.foreshadowing[0].anchor_chapter == 0


def test_verify_rejects_quote_crossing_chapter_boundary():
    # 从第一章末尾跨到第二章开头 = 模型在拼接
    quote = "先摸了摸怀里的铜匣。\n第二章 承\n铜匣打开"
    result = _result(quote)
    stats = verify_result_anchors(result, BLOCK, 1)
    assert stats["missing"] == 1
    assert result.foreshadowing[0].anchor_quote == ""


def test_verify_counts_empty_quote_as_missing():
    result = _result("")
    assert verify_result_anchors(result, BLOCK, 1)["missing"] == 1


def test_verify_without_foreshadowing_returns_zeros():
    result = AnalysisResult(chapter_number=1)
    assert verify_result_anchors(result, BLOCK, 1) == {
        "total": 0,
        "located": 0,
        "missing": 0,
        "by_kind": {},
    }


def test_verify_counts_by_kind():
    result = _result("铜匣打开，里面只有一张旧契。")
    stats = verify_result_anchors(result, BLOCK, 1)
    assert stats["by_kind"] == {"EXACT": 1}
