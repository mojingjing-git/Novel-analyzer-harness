"""get_kb_snapshot 必须把 self.rolling["rolling_structured"]
注入返回的快照副本——该字段只在 build_temp_knowledge 导出路径注入，
分析主链路拿到的 rolling_structured 恒为空 {}，整个后台 rolling 子系统白跑。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core.memory_state import MemoryState
from backend.models.analysis_result import AnalysisResult


STRUCTURED = {
    "paradigm_layers": [{"chapters": "1-10", "text": "升级流"}],
    "global_milestones": ["ch1: 开局"],
    "recent_momentum": [],
    "active_causal_chains": [],
    "current_context": {},
}


def _result(ch: int) -> AnalysisResult:
    return AnalysisResult.from_dict({
        "chapter_number": ch,
        "cross_block": {"summary": f"第{ch}章摘要", "unresolved_questions": [], "new_leads": []},
        "core_events": [],
        "character_arcs": [],
        "foreshadowing": [],
        "plot_holes": [],
        "long_context_insights": {},
        "updated_knowledge": {},
    })


def test_full_snapshot_injects_rolling_structured():
    state = MemoryState()
    state.rolling = {"rolling_structured": STRUCTURED, "last_updated_chapter": 10}
    snap = state.get_kb_snapshot()
    assert snap.rolling_structured == STRUCTURED


def test_subset_snapshot_injects_rolling_structured():
    state = MemoryState()
    asyncio.run(state.add_result(_result(1)))
    asyncio.run(state.add_result(_result(2)))
    state.rolling = {"rolling_structured": STRUCTURED}

    # 先建大 limit 缓存，再请求小 limit（走子集重建分支并写入 _snapshot_cache_kb）
    snap_big = state.get_kb_snapshot(chapter_limit=99)   # >= max(keys)：全量拷贝分支
    assert snap_big.rolling_structured == STRUCTURED

    snap_small = state.get_kb_snapshot(chapter_limit=1)  # 子集重建分支
    assert snap_small.rolling_structured == STRUCTURED

    # 注入只发生在返回副本上：内部缓存对象的 rolling_structured 不得是共享引用
    cached = state._snapshot_cache_kb
    assert cached is not None
    assert cached.rolling_structured is not STRUCTURED


def test_empty_rolling_keeps_default():
    state = MemoryState()
    snap = state.get_kb_snapshot()
    assert snap.rolling_structured == {} or snap.rolling_structured is not None
    assert snap.rolling_structured != STRUCTURED
