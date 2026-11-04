"""自动验收器 v1（acceptance_gate）的机械层与裁决合成测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.acceptance_gate import (  # noqa: E402
    gate,
    mechanical_increment_check,
    ngram_dice,
)

BASE = {"question": "黑太岁的来源是什么？", "criteria": "说明来源或本质",
        "answer": "第499章交代第一次强行修真导致先天一炁不稳定",
        "quote": "强行修真虽然勉强也能达到效果，但也会让自己体内空中的先天一炁变得不稳定",
        "description": "李火旺第一次强行修真的记载",
        "anchor_quote": "他强行修炼了长春功，体内多出一股先天一炁",
        "anchor_chapter": 499}


def test_mechanical_flags_quote_identical_to_anchor():
    """fs_384 型：引文就是锚点原文 → 复述已知，拒绝。"""
    item = dict(BASE, quote=BASE["anchor_quote"])
    hard, _soft = mechanical_increment_check(item)
    assert "quote_equals_anchor" in hard
    assert gate(item, {"verdict": "answer_ok", "reason": "x"})["decision"] == "拒绝"


def test_mechanical_flags_answer_contained_in_known():
    """答案整体包含在 description+锚点里 → 无增量。"""
    item = dict(BASE, answer="李火旺强行修炼了长春功")
    hard, _soft = mechanical_increment_check(item)
    assert "answer_in_known" in hard
    assert gate(item, {"verdict": "answer_ok", "reason": "x"})["decision"] == "拒绝"


def test_mechanical_passes_genuine_increment():
    item = dict(BASE)
    assert mechanical_increment_check(item) == ([], [])
    assert gate(item, {"verdict": "answer_ok", "reason": "新机制"})["decision"] == "接受"


def test_gate_nli_restating_rejects_even_without_mechanical_flags():
    """答案改写了已知信息（字面不重合）但语义复述 → NLI 拒绝。"""
    item = dict(BASE, answer="他修炼长春功后体内多出了先天一炁")  # 与锚点同义但字面不同
    assert mechanical_increment_check(item) == ([], [])
    assert gate(item, {"verdict": "restating", "reason": "与锚点同义"})["decision"] == "拒绝"


def test_gate_unclear_and_nli_error_go_to_doubt():
    item = dict(BASE)
    assert gate(item, {"verdict": "unclear", "reason": "看不出来"})["decision"] == "存疑"
    assert gate(item, None, "调用失败")["decision"] == "存疑"
    # NLI 认可 + 机械通过 = 唯一的接受路径
    assert gate(item, {"verdict": "answer_ok", "reason": "新机制"})["decision"] == "接受"


def test_gate_nli_error_never_accepts():
    d = gate(dict(BASE), None, "调用失败")
    assert d["decision"] == "存疑"


def test_ngram_dice_basic():
    assert ngram_dice("abc", "abc") == 1.0
    assert ngram_dice("abcdef", "abcxyz") < 0.5
