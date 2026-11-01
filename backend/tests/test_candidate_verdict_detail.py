"""``CandidateVerdict`` 的逐条判定明细：落盘形态、位置转换、语义矩阵、不可变性。

被测对象是 ``backend/vnext/resolution.py`` 里的 ``CandidateVerdict``（约 91-153 行）。
它刚为"逐条判定可审计"补了三样东西，此前没有任何测试：

* ``quote`` —— 模型这次判 SUPPORTED 时逐字抄的原文，事后审计的唯一入口；
* ``to_dict()`` —— 把二元组位置转成 ``[start, end]`` / ``None``，供落盘 JSON；
* ``checked`` / ``cached_as_terminal`` —— 是否真核对过、能否缓存复用。

本文件只做**行为锁定**：断言当前实现的真实行为，不改源码。全部用例是纯内存构造，
不联网、不调模型、不读磁盘。

两条与实现相关的事实，本文件按现状断言（发现的问题已上报，未在此修）：

1. ``to_dict()`` 引用了 ``self.checked`` / ``self.cached_as_terminal``，而这两个
   ``@property`` 定义在 ``to_dict`` **之后**。这仍然能工作——方法体在**调用时**才做
   属性查找，查找走类的 ``__dict__``，与定义先后无关（见
   ``test_to_dict_works_when_only_required_fields_supplied``）。
2. ``cached_as_terminal`` 的 docstring 说非解析失败的 ``INVALID`` "按终态处理"，
   但实现对**任何** ``INVALID`` 都返回 False；而 ``parse_recheck`` 里有多条不传
   ``retryable`` 的 ``INVALID`` 分支（FOREIGN_ID / EMPTY_ANSWER / NO_EVIDENCE /
   NO_VERIFIER），所以这条差异可达。见
   ``test_invalid_is_never_cached_as_terminal_even_when_not_retryable``。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from backend.vnext.resolution import (
    CONTRADICTED,
    DIFFERENT_ANSWER,
    INSUFFICIENT,
    INVALID,
    MODEL_VERDICTS,
    SUPPORTED,
    CandidateVerdict,
)

#: ``to_dict()`` 的键集合——任务约定的 15 个，一个不多一个不少（多一个是落盘噪音，
#: 少一个是审计缺口）。
EXPECTED_KEYS = (
    "candidate_id",
    "verdict",
    "answer",
    "evidence_id",
    "supporting_span",
    "counter_search_performed",
    "reason",
    "retryable",
    "located_span",
    "reported_span",
    "ambiguous",
    "quote_hash",
    "quote",
    "checked",
    "cached_as_terminal",
)

#: 可以缓存复用的语义结论（模型能直接给的那四个）。
TERMINAL_VERDICTS = (SUPPORTED, DIFFERENT_ANSWER, CONTRADICTED, INSUFFICIENT)


def _full_verdict(**overrides) -> CandidateVerdict:
    """构造一条"字段全给满"的裁决，方便逐字段比对。"""
    base = dict(
        candidate_id="cd_0001",
        verdict=SUPPORTED,
        answer="沈砚在第十二章打开了铜匣。",
        evidence_id="ev_1",
        supporting_span=(3, 9),
        counter_search_performed=True,
        reason="",
        retryable=False,
        located_span=(3, 9),
        reported_span=(2, 8),
        ambiguous=False,
        quote_hash="9f2a1c0b7d34",
        quote="地窖的门被撬开。",
    )
    base.update(overrides)
    return CandidateVerdict(**base)


# ---------------------------------------------------------------------------
# 1. to_dict：键集合完整、可 JSON 序列化
# ---------------------------------------------------------------------------


def test_to_dict_emits_exactly_the_audit_keys():
    """键集合必须与约定完全一致：多了是噪音，少了就是审计缺口。"""
    keys = list(_full_verdict().to_dict())
    assert set(keys) == set(EXPECTED_KEYS)
    assert len(keys) == len(EXPECTED_KEYS), f"键有重复或缺失：{keys}"
    # 顺序也锁住：落盘后按行比对 diff 时，顺序稳定才看得懂差异
    assert tuple(keys) == EXPECTED_KEYS


def test_to_dict_covers_every_declared_dataclass_field():
    """声明的每个字段都必须进 to_dict；只靠"我记得加"迟早漏一个。"""
    declared = {f.name for f in dataclasses.fields(CandidateVerdict)}
    assert declared <= set(_full_verdict().to_dict())


def test_to_dict_is_json_serialisable_and_round_trips():
    """``to_dict()`` 的产物必须能被 ``json.dumps(..., ensure_ascii=False)`` 直接吃掉。

    以前位置是元组，序列化会变成列表并在读回时类型漂移；这里同时验证中文引文不被
    转义（审计要能直接人读），以及 round-trip 后值不变。
    """
    verdict = _full_verdict()
    payload = verdict.to_dict()

    text = json.dumps(payload, ensure_ascii=False)
    assert "地窖的门被撬开。" in text, "中文引文被转义成了 \\uXXXX，审计时不可读"

    restored = json.loads(text)
    assert restored == payload
    assert restored["quote"] == verdict.quote
    assert restored["supporting_span"] == [3, 9]


# ---------------------------------------------------------------------------
# 2. 位置字段：二元组 -> [start, end]；None 保持 None
# ---------------------------------------------------------------------------


def test_tuple_spans_become_two_element_lists():
    """三个位置字段都要转换，且元素是 int（不是字符串数字）。"""
    payload = _full_verdict(
        supporting_span=(3, 9),
        located_span=(10, 20),
        reported_span=(11, 19),
    ).to_dict()

    for name, expected in (
        ("supporting_span", [3, 9]),
        ("located_span", [10, 20]),
        ("reported_span", [11, 19]),
    ):
        assert payload[name] == expected, name
        assert type(payload[name]) is list, f"{name} 必须是 list，不能是 tuple"
        assert all(type(v) is int for v in payload[name]), name


def test_missing_spans_stay_none_and_serialise_as_null():
    """缺位置时必须是 ``None``（JSON ``null``），不能是字符串 ``"null"`` 也不能报错。"""
    payload = _full_verdict(
        supporting_span=None,
        located_span=None,
        reported_span=None,
    ).to_dict()

    for name in ("supporting_span", "located_span", "reported_span"):
        assert payload[name] is None, f"{name} 应为 None"

    text = json.dumps(payload, ensure_ascii=False)
    assert '"supporting_span": null' in text
    assert '"null"' not in text, "被写成字符串 'null'，读回时还得再判类型"

    # 默认构造（三个位置都不给）同样成立
    defaulted = CandidateVerdict("cd_0002", INSUFFICIENT).to_dict()
    assert defaulted["supporting_span"] is None
    assert defaulted["located_span"] is None
    assert defaulted["reported_span"] is None


# ---------------------------------------------------------------------------
# 3. checked 语义矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "retryable", "expected_checked", "why"),
    [
        (SUPPORTED, False, True, "语义结论 + 不可重跑 = 已核对"),
        (SUPPORTED, True, False, "可重跑说明这次只是解析失败，不算核对过"),
        (INVALID, False, False, "终态但没核对过：不进 COMPLETED 的分子"),
        (INSUFFICIENT, False, True, "材料不足也是核对过的语义结论"),
        (DIFFERENT_ANSWER, False, True, "原文给了另一个答案，同样算核对过"),
        (CONTRADICTED, False, True, "被原文推翻，也算核对过"),
    ],
)
def test_checked_matrix(verdict, retryable, expected_checked, why):
    """``checked`` 的语义矩阵（这是 COMPLETED 分母的唯一依据）。"""
    row = CandidateVerdict("cd_0001", verdict, retryable=retryable)
    assert row.checked is expected_checked, why


def test_checked_iff_terminal_and_not_retryable():
    """穷举枚举 × retryable，锁定 ``checked == 属于四枚举 and not retryable``。"""
    for verdict in (*MODEL_VERDICTS, INVALID, "SOMETHING_ELSE"):
        for retryable in (False, True):
            row = CandidateVerdict("cd_0001", verdict, retryable=retryable)
            expected = (verdict in TERMINAL_VERDICTS) and not retryable
            assert row.checked is expected, (verdict, retryable)


def test_terminal_semantic_verdicts_are_cached():
    """四个语义结论都可缓存复用——否则同一候选会被反复重跑、反复计费。"""
    for verdict in TERMINAL_VERDICTS:
        row = CandidateVerdict("cd_0001", verdict)
        assert row.cached_as_terminal is True, verdict


def test_invalid_is_never_cached_as_terminal_even_when_not_retryable():
    """锁定当前（可疑）行为：任何 ``INVALID`` 都不是终态，即使 ``retryable=False``。

    可疑之处（已上报、未修改源码）：``cached_as_terminal`` 的 docstring 说非解析失败的
    ``INVALID``"本身是终态……所以按终态处理，只是不计入已核对数"，但实现对 ``INVALID``
    一律 False。``parse_recheck`` 里 ``REASON_FOREIGN_ID`` / ``REASON_EMPTY_ANSWER`` /
    ``REASON_NO_EVIDENCE`` / ``REASON_NO_VERIFIER`` 这几条分支都不传 ``retryable``，
    因此"终态但未核对"这一类根本不存在——它们会被当成"还没判"反复重跑。

    这条用例的作用是把现状钉住：等源码按 docstring 修好时，它会红，届时按新语义改。
    """
    retryable_false = CandidateVerdict("cd_0001", INVALID, reason="EMPTY_ANSWER")
    retryable_true = CandidateVerdict("cd_0002", INVALID, reason="UNKNOWN_VERDICT", retryable=True)

    assert retryable_false.retryable is False
    assert retryable_false.cached_as_terminal is False, "docstring 说它是终态，实现说不"
    assert retryable_false.checked is False

    assert retryable_true.cached_as_terminal is False
    assert retryable_true.checked is False


# ---------------------------------------------------------------------------
# 4. frozen dataclass：赋值必须抛 FrozenInstanceError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field_name", "new_value"),
    [
        ("candidate_id", "cd_9999"),
        ("verdict", INVALID),
        ("retryable", True),
        ("supporting_span", (0, 1)),
        ("quote", "另一句原文"),
        ("quote_hash", "deadbeef0000"),
    ],
)
def test_frozen_blocks_field_assignment(field_name, new_value):
    """``@dataclass(frozen=True)``：落盘所用的快照不能被下游悄悄改写。"""
    row = _full_verdict()
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(row, field_name, new_value)


def test_frozen_error_raises_before_any_mutation():
    """抛错之后原值必须完好——半个修改比分类型错更难查。"""
    row = _full_verdict()
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.verdict = INVALID  # type: ignore[misc]
    assert row.verdict == SUPPORTED
    # FrozenInstanceError 继承 AttributeError，所以旧的 except AttributeError 也接得住
    assert issubclass(dataclasses.FrozenInstanceError, AttributeError)


# ---------------------------------------------------------------------------
# 5. quote / quote_hash 原样往返
# ---------------------------------------------------------------------------


def test_quote_and_quote_hash_round_trip():
    """审计入口必须逐字保留：引文被截断/改写，事后就无法复核判定是否成立。"""
    quote = "他推开地窖的门，铜匣还在原处。"
    row = CandidateVerdict(
        "cd_0001",
        SUPPORTED,
        quote=quote,
        quote_hash="a1b2c3d4e5f6",
    )

    payload = row.to_dict()
    assert payload["quote"] == quote
    assert payload["quote_hash"] == "a1b2c3d4e5f6"
    assert payload["quote"] is row.quote, "同一对象、未经转换，才谈得上原样往返"

    restored = json.loads(json.dumps(payload, ensure_ascii=False))
    assert restored["quote"] == quote
    assert restored["quote_hash"] == "a1b2c3d4e5f6"


def test_empty_quote_and_hash_default_to_empty_strings():
    """默认是空串而不是 None：下游 ``if quote`` 与哈希拼接都不必再判类型。"""
    payload = CandidateVerdict("cd_0001", SUPPORTED).to_dict()
    assert payload["quote"] == ""
    assert payload["quote_hash"] == ""


def test_other_fields_pass_through_untouched():
    """``to_dict`` 只该改位置字段，其余原样搬运。"""
    payload = _full_verdict(
        verdict=CONTRADICTED,
        answer="不是铜匣，是木匣。",
        evidence_id="ev_7",
        counter_search_performed=True,
        reason="CONTRADICTED_BY_QUOTE",
        ambiguous=True,
    ).to_dict()

    assert payload["candidate_id"] == "cd_0001"
    assert payload["verdict"] == CONTRADICTED
    assert payload["answer"] == "不是铜匣，是木匣。"
    assert payload["evidence_id"] == "ev_7"
    assert payload["counter_search_performed"] is True
    assert payload["reason"] == "CONTRADICTED_BY_QUOTE"
    assert payload["ambiguous"] is True
    assert payload["retryable"] is False


# ---------------------------------------------------------------------------
# 事实判断：to_dict 引用"定义在它之后"的 property 是否真的可用
# ---------------------------------------------------------------------------


def test_to_dict_works_when_only_required_fields_supplied():
    """只给必填字段构造的实例也能调 ``to_dict()``，且 checked 系列有正确取值。

    这是"``to_dict`` 引用后面定义的 property"那条事实判断的正面证据：构造时没有、
    也不可能算出 ``checked``，它是在 ``to_dict()`` **调用时**才被查找求值的。
    """
    row = CandidateVerdict("cd_0001", SUPPORTED)  # 只给必填的两个字段
    payload = row.to_dict()

    # 方法确实跑通了，且两个 property 都参与求值而非抛 NameError/AttributeError
    assert payload["checked"] is True
    assert payload["cached_as_terminal"] is True

    # 换成不可缓存/可重跑的取值，同一个 to_dict 必须跟着变——证明是调用时求值，
    # 不是构造时缓存在实例上的字段
    other = CandidateVerdict("cd_0002", SUPPORTED, retryable=True).to_dict()
    assert other["checked"] is False
    assert other["cached_as_terminal"] is True


def test_checked_members_are_properties_not_dataclass_fields():
    """``checked`` / ``cached_as_terminal`` 是 property，不是字段，也不是实例属性。

    因此"定义在 ``to_dict`` 之后"不影响可用性：``self.checked`` 在方法体执行时走
    ``type(self).__mro__`` 的属性查找，与类体里的书写顺序无关。
    """
    class_dict = vars(CandidateVerdict)
    declared = {f.name for f in dataclasses.fields(CandidateVerdict)}

    for name in ("checked", "cached_as_terminal"):
        assert isinstance(class_dict[name], property), f"{name} 应是 property"
        assert name not in declared, f"{name} 不该是 dataclass 字段"

    # 类体书写顺序：to_dict 确实在两者之前（这就是那条"引用在后面"的事实）
    order = list(class_dict)
    assert order.index("to_dict") < order.index("checked")
    assert order.index("to_dict") < order.index("cached_as_terminal")

    # 实例上也没有同名字段，取值一律走 property
    row = _full_verdict()
    assert "checked" not in row.__dict__
    assert "cached_as_terminal" not in row.__dict__


def test_to_dict_returns_fresh_dict_and_fresh_span_lists():
    """每次返回新容器：调用方改了落盘用的 dict/list 不能污染不可变快照。"""
    row = _full_verdict()
    first = row.to_dict()
    second = row.to_dict()

    assert first is not second
    assert first == second
    assert first["supporting_span"] is not second["supporting_span"]

    first["supporting_span"].append(99)
    first["verdict"] = "TAMPERED"
    assert row.supporting_span == (3, 9)
    assert row.verdict == SUPPORTED
    assert row.to_dict() == second
