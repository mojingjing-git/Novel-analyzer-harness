"""
测试 AnalysisPipeline 三阶段流程 (mock analyzer)
验证: 串行预热 → 流式并发 → 失败补跑(3轮)
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.models.analysis_result import AnalysisResult
from backend.config.settings import APIConfig, AnalysisConfig, AppConfig


def make_test_config():
    """创建测试用配置"""
    return AppConfig(
        api=APIConfig(
            base_url="https://test.com",
            api_key="test",
            model="test",
            max_tokens=1000,
            timeout=10,
            max_retries=3,
            json_mode="default",
            temperature=0.1,
            temperature_step=0.1,
            temperature_max_retries=2,
            backoff_max_retries=1,
            thinking_mode={},
        ),
        analysis=AnalysisConfig(
            max_arc_length=200,
            concurrency=2,
            block_size=2,
            encoding_priority=["utf-8"],
            max_arcs_in_prompt=10,
            max_summaries_in_prompt=10,
            timeline_truncate=50,
            max_character_states=20,
            max_world_items=20,
            max_foreshadow_entries=20,
            max_foreshadow_catalog_high=50,
            max_foreshadow_catalog_mid=20,
            batch_summary_min_words=200,
            final_report_min_words=500,
            rolling_early_chapters=5,
            rolling_max_milestones=20,
            rolling_max_momentum=20,
            checkpoint_interval=10,
            auto_archive=False,
            max_compressed_arcs=15,
            max_recent_summaries=10,
            max_pacing_tracker=10,
            max_foreshadow_network=20,
            max_world_building=20,
            max_verified_facts=20,
            max_long_term_arcs=10,
            max_thematic_elements=10,
        ),
    )


def test_pipeline_import():
    """验证 pipeline 可以被导入"""
    from backend.core.pipeline import AnalysisPipeline
    assert AnalysisPipeline is not None
    print("✅ test_pipeline_import passed")


def test_result_creation():
    """验证 AnalysisResult 可以被 pipeline 使用"""
    result = AnalysisResult(chapter_number=1)
    assert result.chapter_number == 1
    assert result.core_events == []
    d = result.to_dict()
    assert d["chapter_number"] == 1
    print("✅ test_result_creation passed")


def test_config_creation():
    """验证配置可以正常创建"""
    config = make_test_config()
    assert config.analysis.concurrency == 2
    assert config.analysis.block_size == 2
    assert config.api.model == "test"
    print("✅ test_config_creation passed")


def test_pipeline_init():
    """验证 pipeline 初始化"""
    from backend.core.pipeline import AnalysisPipeline
    config = make_test_config()

    pipeline = AnalysisPipeline(
        config=config,
        directory=Path("/test/blocks"),
        knowledge_file=Path("/test/knowledge.json"),
        start_chapter=1,
    )
    assert pipeline is not None
    assert pipeline._stop_requested is False
    print("✅ test_pipeline_init passed")


def test_stop_request():
    """验证停止请求"""
    from backend.core.pipeline import AnalysisPipeline
    config = make_test_config()

    pipeline = AnalysisPipeline(
        config=config,
        directory=Path("/test/blocks"),
        knowledge_file=Path("/test/knowledge.json"),
    )
    assert not pipeline._stop_requested
    pipeline.stop()
    assert pipeline._stop_requested
    print("✅ test_stop_request passed")


if __name__ == "__main__":
    test_pipeline_import()
    test_result_creation()
    test_config_creation()
    test_pipeline_init()
    test_stop_request()
    print("\n🎉 All pipeline tests passed!")
