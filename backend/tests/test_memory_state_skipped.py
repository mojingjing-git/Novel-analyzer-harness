"""_skipped_chapters 必须随 checkpoint 持久化——否则重启续跑会对
同一批审核拦截块重新支付完整 LLM 重试链费用。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core.memory_state import MemoryState
from backend.models.analysis_result import AnalysisResult


def _result(ch: int) -> AnalysisResult:
    return AnalysisResult.from_dict({
        "chapter_number": ch,
        "cross_block": {"summary": f"第{ch}章", "unresolved_questions": [], "new_leads": []},
        "core_events": [], "character_arcs": [], "foreshadowing": [],
        "plot_holes": [], "long_context_insights": {}, "updated_knowledge": {},
    })


def test_skipped_marks_survive_flush_restore_roundtrip(tmp_path):
    state = MemoryState()
    state.add_skipped(42, "内容审核拦截")
    state.add_failed(43, "普通失败")

    asyncio.run(state.flush_to_disk(tmp_path))

    state2 = MemoryState()
    asyncio.run(state2.restore_from_disk(tmp_path))
    assert state2._skipped_chapters == {42: "内容审核拦截"}
    assert state2._failed_chapters == {43: "普通失败"}


def test_successful_result_clears_stale_skipped_file(tmp_path):
    state = MemoryState()
    state.add_skipped(7, "旧拦截")
    asyncio.run(state.add_result(_result(7)))
    asyncio.run(state.flush_to_disk(tmp_path))

    assert not (tmp_path / "chapter_7_skipped.json").exists(), \
        "章节成功后其陈旧 skipped 标记文件应被清理"
