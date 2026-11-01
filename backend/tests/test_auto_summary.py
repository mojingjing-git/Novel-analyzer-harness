"""自动总结配置与队列收尾逻辑的单元测试"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import AppConfig, AnalysisConfig
from backend.services.queue_manager import QueueItem, QueueManager
from backend.services.queue_service import AnalysisService


def test_analysis_config_auto_summary_defaults():
    """AnalysisConfig 字段默认值（auto_summary=False / 并发2 / 批次30）"""
    cfg = AnalysisConfig()
    assert cfg.auto_summary is False
    assert cfg.summary_concurrency == 2
    assert cfg.summary_batch_size == 30


def test_analysis_config_auto_summary_roundtrip():
    """0802 spec：新字段经 AppConfig.from_dict/to_dict 往返不丢失"""
    data = {
        "api": {},
        "analysis": {"auto_summary": True, "summary_concurrency": 4, "summary_batch_size": 25},
        "gui": {},
    }
    cfg = AppConfig.from_dict(data)
    assert cfg.analysis.auto_summary is True
    assert cfg.analysis.summary_concurrency == 4
    assert cfg.analysis.summary_batch_size == 25
    back = cfg.to_dict()
    assert back["analysis"]["auto_summary"] is True
    assert back["analysis"]["summary_concurrency"] == 4
    assert back["analysis"]["summary_batch_size"] == 25


def test_auto_summary_picks_done_books_only():
    """0802 spec：自动总结只选取 status==done 的书（失败/暂停/未完成不算），
    且 start 携带 allow_during_analysis=True（队列收尾阶段分析任务仍在运行中）。"""
    import backend.services.summary_service as summary_module

    # 绕过 __init__ 构造最小实例（避免触发工作区扫描/读队列文件）
    runner = AnalysisService.__new__(AnalysisService)
    runner.queue = QueueManager()
    runner.queue.add_item(QueueItem(name="已完本", blocks_dir=Path("/x/blocks"), workspace_dir=Path("/x"), status="done"))
    runner.queue.add_item(QueueItem(name="失败本", blocks_dir=Path("/y/blocks"), workspace_dir=Path("/y"), status="failed"))
    runner.queue.add_item(QueueItem(name="未跑本", blocks_dir=Path("/z/blocks"), workspace_dir=Path("/z"), status="pending"))
    runner._stop_requested = False
    runner.save_queue = lambda: None  # 不落盘

    cfg = AppConfig()
    cfg.analysis.auto_summary = True
    cfg.analysis.summary_batch_size = 30
    cfg.analysis.summary_concurrency = 2

    started: list = []

    class FakeSummary:
        def __init__(self):
            self.is_running = False
            self._phase = "complete"

        def start(self, book_id, start_chapter, end_chapter, batch_size, concurrency, allow_during_analysis=False):
            started.append((book_id, batch_size, concurrency, allow_during_analysis))

        def status(self):
            return {"phase": self._phase, "error": ""}

        def stop(self):
            self.is_running = False

    fake = FakeSummary()
    original = summary_module.get_summary_service
    summary_module.get_summary_service = lambda: fake
    try:
        asyncio.run(runner._auto_summary_done_books(cfg))
    finally:
        summary_module.get_summary_service = original

    assert len(started) == 1, f"只应总结 done 的书，实际: {started}"
    book_id, batch_size, concurrency, allow = started[0]
    assert book_id == "已完本"
    assert batch_size == 30
    assert concurrency == 2
    assert allow is True, "自动总结必须携带 allow_during_analysis=True（绕过队列运行中护栏）"
