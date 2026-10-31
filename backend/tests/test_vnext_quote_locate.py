"""D-19（2026-10-31）：偏移由程序算，模型只负责"抄对句子"。

这一组用例钉住 §10.5 第 7/8 条：

* 给了 ``locate`` 回调 → 用 ``locate_quote(quote, 正文, reported=span)`` 定位并回验，
  ``supporting_span`` 从"必填字段"降级为"破歧义的提示"；
* 只给旧的 ``quote_within_range`` → 退化模式，span 仍必填且要逐字对上；
* 两个都不给 → ``NO_QUOTE_VERIFIER``，不能让模型自己给自己打分。

最关键的一条是 :func:`test_span_is_ignored_when_quote_not_inside_it`：
模型把偏移算错（但句子抄对了）时必须**仍然通过**——这正是本次改动的收益。
"""

from __future__ import annotations

from backend.vnext.resolution import (
    INVALID,
    REASON_AMBIGUOUS_QUOTE,
    REASON_NO_QUOTE,
    REASON_NO_VERIFIER,
    REASON_QUOTE_NOT_FOUND,
    REASON_QUOTE_TOO_SHORT,
    SUPPORTED,
    locate_quote,
    parse_recheck,
)

CANDIDATES = {"cd_1"}
EVIDENCE = {"ev_1", "ev_2"}

TEXT1 = "书页泛黄。王冠早已熔成金锭，此事千真万确。此后无人再提。"
QUOTE = "王冠早已熔成金锭，此事千真万确"  # 15 字，正文里出现 1 次
QUOTE_SPAN = (TEXT1.index(QUOTE), TEXT1.index(QUOTE) + len(QUOTE))

DOUBLE = "前半段无关。这一日无事发生众人各自散去。中间垫一句。这一日无事发生众人各自散去。结尾。"
TWICE = "这一日无事发生众人各自散去"  # 13 字，正文里出现 2 次
FIRST = DOUBLE.index(TWICE)
SECOND = DOUBLE.index(TWICE, FIRST + 1)

TEXT2 = "另一章的正文，跟那句引文毫无关系，只是用来当反证。"


def _payload(**over):
    base = {
        "candidate_id": "cd_1",
        "verdict": "SUPPORTED",
        "answer": "王冠已被熔毁",
        "evidence_id": "ev_1",
        "supporting_quote": QUOTE,
        "counter_search": {"performed": True, "checked_evidence_ids": ["ev_1"], "counter_quotes": []},
    }
    base.update(over)
    return base


def _parse(payload, *, text_by_id=None):
    """新契约主路径：``locate`` + ``evidence_text`` 两个参数都要给。"""
    texts = {"ev_1": TEXT1, "ev_2": TEXT2}
    if text_by_id:
        texts.update(text_by_id)
    return parse_recheck(
        payload,
        allowed_candidate_ids=CANDIDATES,
        allowed_evidence_ids=EVIDENCE,
        locate=lambda quote, eid: locate_quote(quote, texts.get(eid, ""), reported=None),
        evidence_text=lambda eid: texts.get(eid, ""),
    )


def _parse_legacy(payload, *, within=None):
    return parse_recheck(
        payload,
        allowed_candidate_ids=CANDIDATES,
        allowed_evidence_ids=EVIDENCE,
        quote_within_range=within if within is not None else (lambda q, s, e: True),
    )


# ---------------------------------------------------------------------------
# 1. 只给引文、不给 span：位置由程序算
# ---------------------------------------------------------------------------


def test_quote_without_span_is_supported_and_located() -> None:
    """``supporting_span`` 不再是必需字段。"""
    verdict = _parse(_payload())
    assert verdict.verdict == SUPPORTED
    assert verdict.located_span == QUOTE_SPAN
    assert verdict.reported_span is None


def test_quote_twice_with_span_pointing_second_is_supported() -> None:
    """两处出现 + span 指向第 2 处 → 采纳第 2 处，并如实标记歧义。"""
    span = (SECOND, SECOND + len(TWICE))
    verdict = _parse(
        _payload(supporting_quote=TWICE, supporting_span=list(span)),
        text_by_id={"ev_1": DOUBLE},
    )
    assert verdict.verdict == SUPPORTED
    assert verdict.located_span == span
    assert verdict.ambiguous is True


# ---------------------------------------------------------------------------
# 2. span 只是提示：错得离谱也不影响结论
# ---------------------------------------------------------------------------


def test_span_is_ignored_when_quote_not_inside_it() -> None:
    """本次改动的核心收益：模型把偏移算错、句子抄对 → 仍然 SUPPORTED。

    改前这条会被 ``QUOTE_OUT_OF_RANGE`` 判死，哪怕引文逐字都在原文里。
    """
    verdict = _parse(_payload(supporting_span=[0, 2]))
    assert verdict.verdict == SUPPORTED
    assert verdict.located_span == QUOTE_SPAN
    assert verdict.located_span != (0, 2)


def test_wrong_span_still_passes_in_legacy_mode() -> None:
    """退化模式：窗口不是引文的精确位置，但只要窗口里确实含它一次就通过。

    此时没有定位器，程序只能采信模型自报的窗口，所以 ``located_span`` 就是它。
    """
    span = (2, 24)  # 引文在 5..20，窗口松了一点
    assert QUOTE in TEXT1[span[0] : span[1]]
    verdict = _parse_legacy(
        _payload(supporting_span=list(span)),
        within=lambda quote, start, end: quote in TEXT1[start:end],
    )
    assert verdict.verdict == SUPPORTED
    assert verdict.located_span == span


# ---------------------------------------------------------------------------
# 3. 引文本身有问题：编造 / 太短 / 多处出现却说不出哪一处
# ---------------------------------------------------------------------------


def test_fabricated_quote_is_quote_not_found() -> None:
    verdict = _parse(_payload(supporting_quote="这句话在正文里根本没有出现过"))
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_QUOTE_NOT_FOUND


def test_short_quote_is_quote_too_short() -> None:
    """5 字：短到无法当证据用。"""
    verdict = _parse(_payload(supporting_quote="王冠早已熔"))
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_QUOTE_TOO_SHORT


def test_eight_char_unique_quote_is_still_evidence() -> None:
    """8 字但唯一：C2 复测里「地窖的门被撬开。」正是这种情况，不该被长度误杀。

    短句撞巧合的风险由**出现次数**兜住，不靠长度硬挡。
    """
    verdict = _parse(_payload(supporting_quote="王冠早已熔成金锭"))
    assert verdict.verdict == SUPPORTED
    assert verdict.located_span is not None


def test_quote_twice_without_span_is_ambiguous() -> None:
    verdict = _parse(_payload(supporting_quote=TWICE), text_by_id={"ev_1": DOUBLE})
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_AMBIGUOUS_QUOTE


def test_quote_from_other_evidence_is_quote_not_found() -> None:
    """引文在 ev_1、却声称 ev_2：证据与文本对不上，按找不到处理。"""
    verdict = _parse(_payload(evidence_id="ev_2"))
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_QUOTE_NOT_FOUND


# ---------------------------------------------------------------------------
# 4. 没有校验器 / 退化模式
# ---------------------------------------------------------------------------


def test_no_verifier_is_rejected() -> None:
    """既没 ``locate`` 也没 ``quote_within_range``：无从确认引文真在原文里。"""
    verdict = parse_recheck(
        _payload(),
        allowed_candidate_ids=CANDIDATES,
        allowed_evidence_ids=EVIDENCE,
    )
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_NO_VERIFIER


def test_legacy_mode_requires_span() -> None:
    """退化模式保留旧语义：没有 span 就没有可校验的窗口。"""
    verdict = _parse_legacy(_payload())
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_NO_QUOTE
