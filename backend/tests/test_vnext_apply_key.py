"""S-3：应用键里到底该放什么。

结论（D-19 §10.2）：**放结论与论据，不放位置**。

- 引文哈希要进键：换了原话就是换了一条论据，必须重新记账；
- 位置不进键：位置是派生物，模型自报错、程序算对之后，同一个结论不该被应用两次
  （否则历史会被追加两遍，或者相反——位置一变就被当成新结论）。
"""

from __future__ import annotations

from backend.vnext import runner
from backend.vnext.recheck_plan import RecheckUnit
from backend.vnext.resolution import CandidateVerdict, SUPPORTED


def _unit() -> RecheckUnit:
    return RecheckUnit(entity_id="fs_1", candidate_ids=("cd_1",), inputs={"q": ("ev_1",)})


def _verdict(**overrides) -> CandidateVerdict:
    base = dict(
        candidate_id="cd_1",
        verdict=SUPPORTED,
        answer="已回收",
        evidence_id="ev_1",
        quote_hash="aaaaaaaaaaaa",
        located_span=(6, 21),
    )
    base.update(overrides)
    return CandidateVerdict(**base)


def test_same_conclusion_same_quote_same_key() -> None:
    first = runner._apply_key(_unit(), _verdict())
    second = runner._apply_key(_unit(), _verdict())
    assert first == second


def test_different_quote_means_different_key() -> None:
    """换了原话就是另一条论据——不能因为结论相同就被去重吞掉。"""
    first = runner._apply_key(_unit(), _verdict(quote_hash="aaaaaaaaaaaa"))
    second = runner._apply_key(_unit(), _verdict(quote_hash="bbbbbbbbbbbb"))
    assert first != second


def test_different_evidence_means_different_key() -> None:
    first = runner._apply_key(_unit(), _verdict(evidence_id="ev_1"))
    second = runner._apply_key(_unit(), _verdict(evidence_id="ev_2"))
    assert first != second


def test_position_does_not_enter_the_key() -> None:
    """位置从错到正不该产生第二条应用记录。"""
    wrong = runner._apply_key(_unit(), _verdict(located_span=(0, 15)))
    right = runner._apply_key(_unit(), _verdict(located_span=(6, 21)))
    assert wrong == right


def test_different_verdict_means_different_key() -> None:
    first = runner._apply_key(_unit(), _verdict())
    second = runner._apply_key(_unit(), _verdict(verdict="INSUFFICIENT"))
    assert first != second
