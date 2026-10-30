"""全局 LLM 并发上限 Sem 测试（2026-08-23）：
- 4 阶段 + 风格分析共用 self._llm_sem，硬上限 = self.concurrency
- 不论 concurrency 是 3 / 9 / 20，peak in-flight 都不应超配置值
- 风格提取与阶段 1 并行启动，最容易撞上限
"""
import asyncio
import json
import sys
from unittest.mock import AsyncMock, patch
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import AppConfig
from backend.core.error_types import LLMResult, UsageStatus
from backend.services.final_summary import FinalSummaryRunner


def _write_results(output_dir: Path, chapters: int):
    output_dir.mkdir(parents=True, exist_ok=True)
    for ch in range(1, chapters + 1):
        data = {
            "chapter_number": ch,
            "cross_block": {"summary": f"第{ch}章摘要内容", "unresolved_questions": [], "new_leads": []},
            "core_events": [],
            "character_arcs": [],
            "foreshadowing": [],
            "plot_holes": [],
            "long_context_insights": {},
            "updated_knowledge": {},
        }
        (output_dir / f"chapter_{ch}_result.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    output_subdir = output_dir / "output" if (output_dir / "output").is_dir() else output_dir
    (output_subdir / "locations_normalized.json").write_text(
        json.dumps({"schema_version": 1, "locations": [], "chapter_mtimes_hash": ""}, ensure_ascii=False),
        encoding="utf-8")
    (output_subdir / "spatial_relationships_normalized.json").write_text(
        json.dumps({"schema_version": 1, "relationships": [], "chapter_mtimes_hash": ""}, ensure_ascii=False),
        encoding="utf-8")


def _make_runner(output_dir: Path, concurrency: int) -> FinalSummaryRunner:
    return FinalSummaryRunner(
        config=AppConfig(),
        output_dir=output_dir,
        start_chapter=1,
        end_chapter=6,
        batch_size=2,
        concurrency=concurrency,
        on_progress=lambda p: None,
        on_token_stats=lambda p: None,
    )


def _instrument_inflight(runner: FinalSummaryRunner) -> dict:
    """包装 _acquire_llm_slot/_release_llm_slot，记录峰值与累计 acquire 次数"""
    state = {"peak": 0, "current": 0, "total_acquires": 0}
    original_acquire = runner._acquire_llm_slot
    original_release = runner._release_llm_slot

    async def tracked_acquire():
        await original_acquire()
        state["total_acquires"] += 1
        state["current"] += 1
        if state["current"] > state["peak"]:
            state["peak"] = state["current"]

    def tracked_release():
        state["current"] -= 1
        original_release()

    runner._acquire_llm_slot = tracked_acquire
    runner._release_llm_slot = tracked_release
    return state


def _mock_llm_calls_with_real_sem():
    """Mock 4 个 LLM 调用方法（让它们走真实 acquire/release 路径）。
    由于 AsyncMock 替换整个方法会绕过 Sem，这里用普通函数包装成 async，
    内部显式走 runner 的 _acquire_llm_slot/_release_llm_slot。
    """
    # 实际上更简单的做法：patch 的不是 _call_llm_xxx，而是更底层的 _llm.chat_with_retry
    # 让 _call_llm_xxx 真正执行（包括 acquire/release），但 LLM 调用是 mock 的
    pass


@pytest.mark.parametrize("concurrency", [3, 9])
def test_peak_inflight_bounded_by_configured_concurrency(tmp_path, concurrency):
    """用 concurrency=3 / 9 各跑一次，断言 in-flight 峰值严格 ≤ 配置值。
    风格提取与阶段 1 并行启动，理论上最容易撞上限——本测试就是为了抓住这个 bug。
    """
    _write_results(tmp_path, 6)  # 6 章 → 3 卷 → 阶段 1 至少 3 个 batch
    # 风格分析需要 blocks_dir 存在，否则 _run_style_extraction 提前 return
    blocks_dir = tmp_path.parent / "blocks"
    blocks_dir.mkdir(parents=True, exist_ok=True)
    runner = _make_runner(tmp_path, concurrency=concurrency)
    state = _instrument_inflight(runner)

    # 让 4 个 LLM 走真实 Sem（patch 底层 _llm.chat_with_retry 即可，_call_llm_* 自动执行 acquire/release）
    async def fake_chat(messages, *args, **kwargs):
        # 模拟 LLM 真实耗时（50ms）让并发能展开
        await asyncio.sleep(0.05)
        # U4：chat_auto 返回 LLMResult 对象
        return LLMResult(True, "{}", "", (10, 5), UsageStatus.KNOWN)

    style_return = {
        "statistical": {"avg_sentence_length": 20, "short_sentence_ratio": 0.3,
                        "long_sentence_ratio": 0.1, "dialogue_ratio": 0.4, "ttr": 0.5,
                        "sensory_density": 5.0, "metaphor_freq": 2.0,
                        "literary_ratio": 1.0, "colloquial_ratio": 2.0},
        "semantic": {"signature_expressions": "", "narrative_rhythm": "",
                     "dialogue_style": "", "rhetorical_preferences": "",
                     "emotional_expression": "", "narrative_voice": "",
                     "information_control": "", "narrator_and_genre": ""},
    }

    async def fake_style(_blocks_dir, _book_name, _api_config):
        await asyncio.sleep(0.05)
        return style_return

    with patch.object(runner._llm, 'chat_auto', new=fake_chat), \
         patch('backend.services.final_summary.extract_style_profile', new=fake_style):
        report = asyncio.run(runner.run())

    assert report is not None, "run() 应返回最终报告"
    assert state["peak"] <= concurrency, (
        f"in-flight 峰值 {state['peak']} 超过配置上限 {concurrency}！"
        " 全局 Sem 漏挂了某个 LLM 调用路径。"
    )
    # 同时核对 runner 自带的 _peak_inflight 计数器（生产日志会用）
    assert runner._peak_inflight <= concurrency
    # 风格提取确实并行启动并消耗了 1 个槽
    assert state["total_acquires"] >= 8, (
        f"应至少 8 次 acquire（3 summary + 3 recon + 1 final + 1 style），实际 {state['total_acquires']}"
    )


def test_global_sem_is_shared_across_phases(tmp_path):
    """全局 Sem 在 4 阶段 + 风格中复用同一个实例（不是各阶段新建）"""
    _write_results(tmp_path, 6)
    runner = _make_runner(tmp_path, concurrency=4)

    # 验证 init 时已创建 _llm_sem
    import asyncio
    sem = runner._llm_sem
    assert isinstance(sem, asyncio.Semaphore)
    # 内部计数器初值 = concurrency
    assert sem._value == 4  # type: ignore[attr-defined]
