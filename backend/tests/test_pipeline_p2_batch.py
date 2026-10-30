"""P2 批量修复验证：
1. MemoryState.failed_chapters_in 按 valid_block_ids 过滤（跨场失败标记不再误报）
2. _archive_momentum_to_milestone 对非 str 势头条目不再 TypeError
3. get_kb_snapshot（to_thread 工作线程）vs add_result 并发读写不再炸 RuntimeError
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core.error_types import LLMResult, UsageStatus
from backend.core.memory_state import MemoryState
from backend.core.pipeline import AnalysisPipeline
from backend.models.analysis_result import AnalysisResult


def test_failed_chapters_in_filters_by_scope():
    state = MemoryState()
    state._failed_chapters.update({95: "旧场失败", 3: "本轮失败"})
    assert state.failed_chapters_in({3, 7, 11}) == [3]
    assert state.failed_chapters_in(set()) == []


class _FakeRollingClient:
    async def chat_with_retry(self, messages, max_tokens=None):
        # U4：返回 LLMResult 对象（不再是 5 元组）
        return LLMResult(True, "ch1-3: 测试里程碑", "", (0, 0), UsageStatus.KNOWN)


def test_archive_momentum_tolerates_non_str_entries():
    p = AnalysisPipeline.__new__(AnalysisPipeline)

    async def _noop_emit(tokens):
        pass

    p._emit_rolling_tokens = _noop_emit

    structured = {"global_milestones": ["ch1: 起点", {"text": "dict 条目"}],
                  "recent_momentum": []}
    momentum = [{"text": "dict 势头"}, "ch5: 正常条目"]

    # 此前 "\n".join(momentum) 对 dict 元素直接 TypeError
    asyncio.run(p._archive_momentum_to_milestone(_FakeRollingClient(), structured, momentum))

    assert structured["recent_momentum"] == []
    assert any(isinstance(m, str) and "测试里程碑" in m for m in structured["global_milestones"])


def _make_result(ch: int) -> AnalysisResult:
    """最小 AnalysisResult（与 test_memory_state_rolling._result 同构）"""
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


def test_concurrent_snapshot_vs_add_no_runtime_error():
    """P2 收口回归：工作线程并发快照 vs 事件循环写入不得炸
    dictionary changed size during iteration（概率性，跑足轮次提高捕获率）"""
    import random

    async def main():
        state = MemoryState()
        stop = False
        errors = []

        def reader():
            while not stop:
                try:
                    state.get_kb_snapshot(chapter_limit=random.choice([None, 5, 50, 500]))
                except RuntimeError as e:
                    errors.append(e)
                    return

        import threading
        t = threading.Thread(target=reader)
        t.start()
        for ch in range(1, 300):
            await state.add_result(_make_result(ch))
        stop = True
        t.join()

        assert errors == [], f"并发读写竞态复现: {errors[:3]}"

    asyncio.run(main())
