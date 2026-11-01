"""风格分析 token 消耗必须接入 runner 统计（否则恒为 (0,0) 盲区）"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import AppConfig
from backend.core import style_analyzer
from backend.services.final_summary import FinalSummaryRunner


def _make_runner(tmp_path):
    return FinalSummaryRunner(
        config=AppConfig(), output_dir=tmp_path,
        start_chapter=1, end_chapter=4, batch_size=2, concurrency=2,
        on_progress=lambda p: None, on_token_stats=lambda p: None)


def test_extract_profile_forwards_token_sink(tmp_path):
    captured = {}

    def fake_stats(blocks_dir):
        return {"chapter_count": 1}, [(1, "样本文本")]

    async def fake_semantic(prompt, api_config, token_sink=None):
        captured["called_with_sink"] = token_sink is not None
        if token_sink:
            token_sink((123, 45))
        return {f: "x" for f in style_analyzer.SEMANTIC_FIELDS}

    with patch.object(style_analyzer, "compute_book_stats", fake_stats), \
         patch.object(style_analyzer, "call_llm_semantic", fake_semantic):
        asyncio.run(style_analyzer.extract_style_profile(
            tmp_path, "书名", {},
            token_sink=lambda t: captured.setdefault("got", t)))

    assert captured.get("called_with_sink") is True
    assert captured.get("got") == (123, 45)


def test_runner_records_style_tokens(tmp_path):
    runner = _make_runner(tmp_path)

    def fake_extract(blocks_dir, book_name, api_config, token_sink=None):
        token_sink((100, 200))
        return {"statistical": {}, "semantic": {}}

    # 先建 blocks 目录使 _run_style_extraction 的路径检查通过
    (runner.output_dir.parent / "blocks").mkdir(parents=True, exist_ok=True)
    with patch("backend.services.final_summary.extract_style_profile", fake_extract):
        asyncio.run(runner._run_style_extraction())

    assert runner._tokens_style == (100, 200), f"style token 应被记录，实际 {runner._tokens_style}"
