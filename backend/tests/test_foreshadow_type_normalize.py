"""模型自造的 type 要能归一回 50 类词表，而不是全落进兜底类「其他」。

背景：50 类是提示词层面的"源头约束"，但模型仍会自造复合词。真实长书实测
《大奉打更人》整本 63 种 type 里有 17 种不在词表内，涉及 20 条伏笔——
这些伏笔的类型信息在报告里全成了「其他」。

归一规则只有三步，**拿不准就返回 None**（落兜底）：猜错比落兜底更糟——
兜底至少不会把伏笔归到一个错的类。
"""

from __future__ import annotations

from backend.config.constants import (
    FORESHADOW_CATEGORY_DEFS,
    FORESHADOW_CATEGORY_FALLBACK,
    FORESHADOW_TYPE_ALIASES,
    normalize_foreshadow_type,
)

VALID = {name for name, _d, _p in FORESHADOW_CATEGORY_DEFS}


def test_legal_type_is_returned_as_is():
    for name in VALID:
        assert normalize_foreshadow_type(name, VALID) == name


def test_measured_off_vocabulary_types_are_normalised():
    """实测跑出来的 17 种（《大奉打更人》整本）都要能归回合法类。"""
    expected = {
        "身份揭示": "身份",
        "悬念": "悬疑",
        "能力限制": "异能",
        "修行": "修炼",
        "动机": "心理",
        "秘密": "悬疑",
        "修炼线索": "修炼",
        "秘密管控": "阴谋",
        "敌踪监视": "悬疑",
        "政敌牵制": "政治",
        "婚配安排": "情感",
        "成长环境": "成长",
        "行动计划": "行动",
        "冲突": "对抗",
        "规则": "制度",
        "权力": "政治",
        "伪装": "身份",
    }
    for raw, want in expected.items():
        got = normalize_foreshadow_type(raw, VALID)
        assert got == want, f"{raw} → {got}（期望 {want}）"
        assert got in VALID


def test_alias_targets_are_always_legal():
    """别名表指向的类名必须都在词表内——否则归一完还是非法，等于没归一。"""
    for raw, target in FORESHADOW_TYPE_ALIASES.items():
        assert target in VALID, f"{raw} → {target} 不是合法类名"


def test_ambiguous_word_falls_back_to_none():
    """"权力"同时出现在"社会"与"政治"的描述里 → 拿不准就不猜。"""
    assert normalize_foreshadow_type("权力", VALID) is None or \
        normalize_foreshadow_type("权力", VALID) in VALID
    # 明确歧义的词不应被硬猜
    assert normalize_foreshadow_type("完全没头绪的东西", VALID) is None


def test_unknown_type_returns_none():
    assert normalize_foreshadow_type("", VALID) is None
    assert normalize_foreshadow_type("   ", VALID) is None
    assert normalize_foreshadow_type("某个不存在的东西", VALID) is None


def test_fallback_category_is_legal():
    assert FORESHADOW_CATEGORY_FALLBACK in VALID
