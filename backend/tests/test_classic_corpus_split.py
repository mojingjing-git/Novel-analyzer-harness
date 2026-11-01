"""古籍形态回归：回目带对句、无空行、汉字回号。

语料见 `backend/tests/fixtures/classic_corpus.py`（合成文本，非原著段落）。
"""

from __future__ import annotations

from backend.core.chapter_split import chapter_number_from_heading, split_chapters
from backend.core.text_units import build_display_labels, find_literal, locate_in_units
from backend.tests.fixtures.classic_corpus import (
    CLASSIC_TEXT,
    EXPECTED_ROUNDS,
    REFERENCE_SENTENCE,
    ROUND_MARKERS,
)


def test_classic_round_headings_are_split():
    spans = split_chapters(CLASSIC_TEXT)

    assert [s.chapter_no for s in spans] == EXPECTED_ROUNDS


def test_classic_rounds_keep_their_own_content():
    """每一回的正文不能串到相邻回里。"""
    spans = split_chapters(CLASSIC_TEXT)

    for span in spans:
        marker = ROUND_MARKERS.get(span.chapter_no)
        assert marker and marker in span.text, f"第{span.chapter_no}回的内容串位了"


def test_classic_round_number_is_parsed_from_the_heading():
    spans = split_chapters(CLASSIC_TEXT)

    heading = spans[0].text.splitlines()[0]
    assert chapter_number_from_heading(heading) == 1


def test_reference_sentence_inside_the_body_is_not_a_heading():
    """正文里的"第七回中写道"不能被当成新的回目。"""
    text = CLASSIC_TEXT.replace("话说宁国荣国二府之祖", REFERENCE_SENTENCE + "话说宁国荣国二府之祖")
    spans = split_chapters(text)

    assert [s.chapter_no for s in spans] == EXPECTED_ROUNDS


def test_classic_display_labels_use_the_round_number():
    """古籍语料里的"第 N 回"标签要能解析出来（提示词要同时给出两套编号）。"""
    labels = build_display_labels({1: CLASSIC_TEXT})

    assert labels.get(1) == "第 1 回"


def test_quote_inside_a_classic_round_is_locatable():
    """古籍形态下的定位：引文在某一回里，必须能定位到**那一回**（文件级身份）。"""
    units = {1: CLASSIC_TEXT}
    quote = "甄士隐的英莲亦被拐去"

    hit = locate_in_units(units, quote)
    assert hit is not None
    unit, offset = hit
    assert unit == 1
    assert find_literal(CLASSIC_TEXT, quote) == offset
