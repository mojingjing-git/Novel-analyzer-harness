"""存档与 Prompt 窗口分离（S3-8）：库不裁剪，窗口才裁剪，且截断必须被报告。

这组用例的价值在于挡住一个"看起来完全正常"的退化：把裁剪写进落盘路径。那种实现下
库的大小由配置决定，改一次限额、重跑一次，历史条目就悄悄消失，且没有任何报错。
"""

from __future__ import annotations

import pytest

from backend.vnext.knowledge import (
    CharacterStateRecord,
    FactRecord,
    KnowledgeStore,
    SourceRef,
)
from backend.vnext.prompt_limits import PromptLimits
from backend.vnext.prompt_view import build_prompt_window, count_knowledge


def _src(chapter: int, start: int = 0) -> SourceRef:
    return SourceRef(f"ch_{chapter:04d}", chapter, start, start + 5, f"h{chapter}")


def _fact(fid: str, chapter: int) -> FactRecord:
    return FactRecord(fact_id=fid, text=f"事实{fid}", source=_src(chapter))


def _store(fact_count: int = 5, char_count: int = 3) -> KnowledgeStore:
    store = KnowledgeStore()
    store.add_block(
        block_signature="blk",
        facts=[_fact(f"f{i}", i) for i in range(1, fact_count + 1)],
        character_records=[
            CharacterStateRecord(f"人{i}", "在", _src(1)) for i in range(1, char_count + 1)
        ],
    )
    return store


# ---------------------------------------------------------------------------
# 库不裁剪
# ---------------------------------------------------------------------------


def test_store_keeps_everything_regardless_of_limits() -> None:
    store = _store(fact_count=50)
    limits = PromptLimits(max_verified_facts=5)
    build_prompt_window(store, exclusive_before=99, limits=limits)
    # 窗口只给 5 条，但库里仍然 50 条
    assert count_knowledge(store)["facts"] == 50


def test_building_a_window_does_not_mutate_the_store() -> None:
    store = _store(fact_count=10)
    before = store.to_dict()
    build_prompt_window(store, exclusive_before=99, limits=PromptLimits(max_verified_facts=2))
    assert store.to_dict() == before


def test_different_limits_produce_different_windows_from_same_store() -> None:
    """同一份库，窗口大小不同 —— 这正是"限额是读取参数、不是数据形状"的证据。"""
    store = _store(fact_count=20)
    small = build_prompt_window(store, exclusive_before=99, limits=PromptLimits(max_verified_facts=3))
    large = build_prompt_window(store, exclusive_before=99, limits=PromptLimits(max_verified_facts=10))
    assert len(small.facts) == 3
    assert len(large.facts) == 10


# ---------------------------------------------------------------------------
# 窗口裁剪方向与报告
# ---------------------------------------------------------------------------


def test_facts_window_takes_the_nearest_ones() -> None:
    """截断时保留"离当前块最近"的事实，而不是最早的那几条。"""
    store = _store(fact_count=10)   # f1..f10，来源章 1..10
    win = build_prompt_window(store, exclusive_before=99, limits=PromptLimits(max_verified_facts=3))
    assert win.facts == ("事实f8", "事实f9", "事实f10")


def test_window_reports_what_was_dropped() -> None:
    store = _store(fact_count=10, char_count=8)
    win = build_prompt_window(
        store,
        exclusive_before=99,
        limits=PromptLimits(max_verified_facts=4, max_character_states=5),
    )
    assert win.truncated is True
    assert win.dropped == {"facts": 6, "character_states": 3}


def test_no_truncation_means_no_report_field() -> None:
    """没截断就不要出现 truncated 字段——否则模型会无谓地警惕。"""
    store = _store(fact_count=2, char_count=1)
    win = build_prompt_window(store, exclusive_before=99, limits=PromptLimits())
    assert win.truncated is False
    assert "truncated" not in win.to_dict()


def test_window_respects_exclusive_before() -> None:
    """窗口仍然受故事位置约束：上界之后的内容不会因为"限额还有空位"就漏进来。"""
    store = _store(fact_count=10)
    win = build_prompt_window(store, exclusive_before=5, limits=PromptLimits())
    assert win.facts == ("事实f1", "事实f2", "事实f3", "事实f4")


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------


def test_character_states_are_deterministic() -> None:
    """人物截断按名字排序，不按到达顺序——否则同一份输入会给出不同 Prompt。"""
    store = _store(fact_count=1, char_count=6)
    win = build_prompt_window(
        store, exclusive_before=99, limits=PromptLimits(max_character_states=3)
    )
    names = [name for name, _ in win.character_states]
    assert names == sorted(names)
    assert names == names[:3]


def test_payload_shape_is_json_ready() -> None:
    store = _store(fact_count=3, char_count=2)
    payload = build_prompt_window(store, exclusive_before=99, limits=PromptLimits()).to_dict()
    assert set(payload) == {"exclusive_before", "facts", "character_states"}
    assert all(isinstance(text, str) for text in payload["facts"])
    assert all(set(item) == {"name", "state"} for item in payload["character_states"])
