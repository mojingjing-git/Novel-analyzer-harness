"""
pipeline 在途块跟踪 + 异常兜底测试（车道堆叠修复）

验证：
- _analyze_one_block 在 LLM 调用窗口内登记 _inflight_blocks，结束后清空
  （预热/并发/补跑三条路径共用的单点，覆盖即全覆盖）
- analyze_block_with_progress 异常时带正确 chapter 发 failed 并记入失败集
  （原实现异常冲出 run() 整书标 failed；消费循环兜底发 chapter:0 被前端忽略 → 车道泄漏）
"""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.config.settings import APIConfig, AnalysisConfig, AppConfig
from backend.core.memory_state import MemoryState
from backend.core.pipeline import AnalysisPipeline
from backend.models.analysis_result import AnalysisResult


def make_config() -> AppConfig:
    return AppConfig(
        api=APIConfig(
            base_url="https://test.com", api_key="test", model="test",
            max_tokens=1000, timeout=10, max_retries=3, json_mode="default",
            temperature=0.1, temperature_step=0.1, temperature_max_retries=2,
            backoff_max_retries=1, thinking_mode={},
        ),
        analysis=AnalysisConfig(
            concurrency=2, block_size=2, encoding_priority=["utf-8"],
            checkpoint_interval=10,
        ),
    )


def make_pipeline() -> AnalysisPipeline:
    return AnalysisPipeline(
        config=make_config(),
        directory=Path("/test/blocks"),
        knowledge_file=Path("/test/knowledge.json"),
    )


def make_state() -> MemoryState:
    return MemoryState(checkpoint_interval=10, kb_limits={})


async def test_inflight_registered_during_call_and_cleared_after():
    pipeline = make_pipeline()
    state = make_state()
    fp = MagicMock()
    fp.read_block.return_value = "第一章 内容"

    captured = {}

    async def fake_analyze_chapter(block_id, content, kb, block_size=1, book_id=""):
        captured["during"] = dict(pipeline._inflight_blocks)
        return AnalysisResult(chapter_number=block_id), (10, 20), {"retries": 0, "failed_tokens": 0}

    analyzer = MagicMock()
    analyzer.analyze_chapter = fake_analyze_chapter

    pipeline._block_map = {100: [100, 101]}
    success, elapsed, ch_tokens, result, retry_info = await pipeline._analyze_one_block(
        analyzer, fp, state, 100, [100, 101], block_size=2)

    assert success is True
    during = captured["during"]
    assert set(during.keys()) == {100}
    assert during[100]["chapter"] == 100
    assert "range" in during[100] and during[100]["started_at"] > 0
    # 调用结束后必须清空，否则对账快照会把已完成块当在途
    assert pipeline._inflight_blocks == {}
    assert pipeline.inflight_blocks() == []


async def test_inflight_cleared_on_exception():
    pipeline = make_pipeline()
    state = make_state()
    fp = MagicMock()
    fp.read_block.return_value = "内容"

    async def boom(block_id, content, kb, block_size=1, book_id=""):
        raise RuntimeError("LLM 连接炸了")

    analyzer = MagicMock()
    analyzer.analyze_chapter = boom

    pipeline._block_map = {5: [5, 6]}
    # impl 的异常按现有语义向外传播（由 _worker / with_progress 层兜底），
    # 但 finally 必须保证在途登记被清掉
    with pytest.raises(RuntimeError):
        await pipeline._analyze_one_block(analyzer, fp, state, 5, [5, 6], block_size=2)
    assert pipeline._inflight_blocks == {}


async def test_with_progress_exception_emits_failed_with_correct_chapter():
    pipeline = make_pipeline()
    state = make_state()
    fp = MagicMock()
    fp.read_block.return_value = "内容"

    async def boom(block_id, content, kb, block_size=1, book_id=""):
        raise RuntimeError("解析器炸了")

    analyzer = MagicMock()
    analyzer.analyze_chapter = boom
    pipeline._analyzer = analyzer
    # run() 中才赋值的实例属性，测试里手动挂载
    pipeline.file_processor = fp
    pipeline.state = state

    emitted = []

    async def on_progress(payload):
        emitted.append(payload)

    pipeline._on_progress = on_progress
    pipeline._block_map = {7: [7, 8]}

    success, completed, ch_tokens, result, retry_info = await pipeline.analyze_block_with_progress(
        block_id=7, block_chs=[7, 8], block_size=2, total=10, label="补跑",
        progress_count=3, completed_count=3, emit_start=True,
    )

    assert success is False
    assert completed == 3  # 进度不因异常虚增
    failed_events = [p for p in emitted if p.get("status") == "failed"]
    assert failed_events, "异常必须发出 failed 事件"
    assert failed_events[-1]["chapter"] == 7, "chapter 必须是原 block_id（此前固定发 0 被前端忽略）"
    # 必须进失败集，补跑阶段才能捞到重试
    assert 7 in state._failed_chapters
    # 在途登记已清空
    assert pipeline._inflight_blocks == {}


async def test_start_event_has_structured_range():
    """start 事件必须带与 inflight_blocks 同源的 chN-chM，避免前端解析日志文案"""
    pipeline = make_pipeline()
    state = make_state()
    fp = MagicMock()
    fp.read_block.return_value = "内容"

    async def fake_analyze_chapter(block_id, content, kb, block_size=1, book_id=""):
        return AnalysisResult(chapter_number=block_id), (10, 20), {"retries": 0, "failed_tokens": 0}

    pipeline._analyzer = MagicMock()
    pipeline._analyzer.analyze_chapter = fake_analyze_chapter
    pipeline.file_processor = fp
    pipeline.state = state
    pipeline._block_map = {5: [5, 8]}
    emitted = []

    async def on_progress(payload):
        emitted.append(payload)

    pipeline._on_progress = on_progress
    await pipeline.analyze_block_with_progress(
        block_id=5, block_chs=[5, 8], block_size=2, total=10, label="",
        progress_count=2, completed_count=2, emit_start=True,
    )

    start = next(p for p in emitted if p.get("status") == "start")
    assert start["range"] == "ch5-ch8"
    assert start["block_size"] == 2


async def test_done_event_has_structured_range():
    """done/skipped/failed 事件同样带结构化 range，保持车道占位文案统一"""
    pipeline = make_pipeline()
    state = make_state()
    fp = MagicMock()
    fp.read_block.return_value = "内容"

    async def fake_analyze_chapter(block_id, content, kb, block_size=1, book_id=""):
        return AnalysisResult(chapter_number=block_id), (10, 20), {"retries": 0, "failed_tokens": 0}

    pipeline._analyzer = MagicMock()
    pipeline._analyzer.analyze_chapter = fake_analyze_chapter
    pipeline.file_processor = fp
    pipeline.state = state
    pipeline._block_map = {9: [9, 10]}
    emitted = []

    async def on_progress(payload):
        emitted.append(payload)

    pipeline._on_progress = on_progress
    await pipeline.analyze_block_with_progress(
        block_id=9, block_chs=[9, 10], block_size=2, total=10, label="补跑",
        progress_count=2, completed_count=2, emit_start=True,
    )

    done = next(p for p in emitted if p.get("status") == "done")
    assert done["range"] == "ch9-ch10"
    assert done["block_size"] == 2
