"""费用估算（models.dev 挂牌价）的算术与映射契约。

关键口径（AGY 复审 R-1）：OpenAI 协议 prompt_tokens **已包含** cached_tokens，
输入费只对未命中部分按 input 价计——直接 in×input + cached×cache_read 会把
缓存部分收两次费。类目→模型映射按 runner 两客户端结构（recheck→判定模型，
其余→summary_model or api.model）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.api.routes_analysis import _estimate_session_cost
from backend.core.model_catalog import calc_model_cost

MINIMAX = "MiniMax-M3.1-Flash"  # 0.15 / 0.60 / cache_read 0.015
LUNA = "codex/gpt-6-luna"  # 0.10 / 0.50 / cache_read 0.01（normalize 后命中）


def test_calc_model_cost_splits_cached_from_uncached():
    """1M 输入其中 200k 走缓存：800k×0.15 + 200k×0.015 + 100k×0.6 = $0.183"""
    r = calc_model_cost(MINIMAX, input_tokens=1_000_000, output_tokens=100_000, cached_tokens=200_000)
    assert r is not None
    assert abs(r["usd"] - 0.183) < 1e-9


def test_calc_model_cost_clamps_cached_over_input():
    """缓存命中不可能超过输入总量：超出部分被钳到 in。"""
    r = calc_model_cost(MINIMAX, input_tokens=100, output_tokens=10, cached_tokens=500)
    assert r is not None
    # (0×0.15 + 100×0.015 + 10×0.6) / 1e6
    assert abs(r["usd"] - (100 * 0.015 + 10 * 0.6) / 1_000_000) < 1e-12


def test_calc_model_cost_unpriced_returns_none():
    assert calc_model_cost("完全未知的私有模型") is None


def test_estimate_maps_recheck_to_recheck_model():
    """summary 侧 recheck 类目 → 判定模型牌价；其余 → summary_model or api.model。"""
    analysis = {"categories": {"chapter": {"input_tokens": 1_000_000, "output_tokens": 100_000}},
                "cached_tokens": 200_000}
    summary = {"categories": {
        "recheck": {"input_tokens": 500_000, "output_tokens": 50_000},
        "summary": {"input_tokens": 300_000, "output_tokens": 30_000},
    }}
    cost = _estimate_session_cost(
        analysis, summary, api_model=MINIMAX, summary_model="", recheck_model=LUNA,
    )
    # analysis(minimax) 0.183 + recheck(luna) 0.075 + summary(minimax) 0.063
    assert abs(cost["usd"] - 0.321) < 1e-9
    assert cost["unpriced"] == []
    assert cost["total_tokens"] == 2_180_000  # 含缓存的 200k（总量=实际过协议的 token）
    assert cost["catalog_version"]


def test_estimate_prefers_summary_model_over_api_model():
    """summary 侧非 recheck 类目走 summary_model（runner 的 self._llm 语义）。"""
    analysis = {"categories": {}, "cached_tokens": 0}
    summary = {"categories": {"style": {"input_tokens": 1_000_000, "output_tokens": 0}}}
    cost = _estimate_session_cost(
        analysis, summary, api_model=MINIMAX, summary_model=LUNA, recheck_model=LUNA,
    )
    # 1M × 0.1（luna 输入价）——若错用 minimax 会是 0.15
    assert abs(cost["usd"] - 0.1) < 1e-9


def test_estimate_all_unpriced_yields_usd_none():
    """全部用量都未定价：usd=None（不是 $0.00），unpriced 点名模型。"""
    analysis = {"categories": {"chapter": {"input_tokens": 999_999, "output_tokens": 1}}, "cached_tokens": 0}
    summary = {"categories": {"recheck": {"input_tokens": 10, "output_tokens": 0}}}
    cost = _estimate_session_cost(
        analysis, summary, api_model="未知模型甲", summary_model="", recheck_model="未知模型乙",
    )
    assert cost["usd"] is None
    assert cost["unpriced"] == ["未知模型乙", "未知模型甲"]  # sorted
    assert cost["total_tokens"] == 1_000_010  # 999,999+1（分析）+ 10（复检）


def test_estimate_empty_session_is_zero_not_none():
    """没有任何用量：usd=0.0（不是 None——None 专指"有用量但没法定价"）。"""
    cost = _estimate_session_cost(
        {"categories": {}, "cached_tokens": 0}, {"categories": {}},
        api_model=MINIMAX, summary_model="", recheck_model=LUNA,
    )
    assert cost["usd"] == 0.0
    assert cost["total_tokens"] == 0
