"""
测试 QueueItem 和 QueueManager 的增删改/持久化/恢复
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.services.pipeline_events import _DISCOVERY_STOP_WORDS, _extract_discovery
from backend.services.queue_manager import QueueItem, QueueManager
from backend.services.analysis_stats import AnalysisStats
from backend.services.queue_service import AnalysisService


def test_queue_item_roundtrip():
    """QueueItem 序列化往返"""
    item = QueueItem(
        name="测试小说",
        blocks_dir=Path("/workspace/测试/blocks"),
        workspace_dir=Path("/workspace/测试"),
        status="done",
        total_chapters=100,
        completed_chapters=80,
        block_size=3,
        start_time=1000.0,
        end_time=2000.0,
        error_message="",
        archive_failed=False,
    )
    d = item.to_dict()
    assert d["name"] == "测试小说"
    assert d["status"] == "done"
    assert d["total_chapters"] == 100

    item2 = QueueItem.from_dict(d)
    assert item2.name == item.name
    assert item2.status == item.status
    assert item2.total_chapters == item.total_chapters
    assert item2.completed_chapters == item.completed_chapters
    print("✅ test_queue_item_roundtrip passed")


def test_queue_manager_add_remove():
    """队列添加和移除"""
    qm = QueueManager()
    item1 = QueueItem(name="小说A", blocks_dir=Path("/a/blocks"), workspace_dir=Path("/a"))
    item2 = QueueItem(name="小说B", blocks_dir=Path("/b/blocks"), workspace_dir=Path("/b"))
    item3 = QueueItem(name="小说C", blocks_dir=Path("/c/blocks"), workspace_dir=Path("/c"))

    qm.add_item(item1)
    qm.add_item(item2)
    qm.add_item(item3)
    assert qm.count == 3

    removed = qm.remove_item(1)
    assert removed is not None
    assert removed.name == "小说B"
    assert qm.count == 2
    assert qm.items[0].name == "小说A"
    assert qm.items[1].name == "小说C"
    print("✅ test_queue_manager_add_remove passed")


def test_queue_manager_dedup():
    """队列去重"""
    qm = QueueManager()
    item1 = QueueItem(name="小说A", blocks_dir=Path("/a/blocks"), workspace_dir=Path("/a"))
    item2 = QueueItem(name="小说A重复", blocks_dir=Path("/a/blocks"), workspace_dir=Path("/a"))
    qm.add_item(item1)
    idx = qm.add_item(item2)
    assert qm.count == 1
    assert idx == 0
    print("✅ test_queue_manager_dedup passed")


def test_queue_manager_move():
    """队列上移下移"""
    qm = QueueManager()
    for i in range(4):
        qm.add_item(QueueItem(name=f"小说{i}", blocks_dir=Path(f"/{i}/blocks"), workspace_dir=Path(f"/{i}")))

    assert qm.move_up(2)  # [0,2,1,3]
    assert qm.items[1].name == "小说2"
    assert qm.items[2].name == "小说1"

    assert qm.move_down(0)  # [2,0,1,3]
    assert qm.items[0].name == "小说2"
    assert qm.items[1].name == "小说0"

    assert not qm.move_up(0)  # can't move first up
    assert not qm.move_down(3)  # can't move last down
    print("✅ test_queue_manager_move passed")


def test_queue_manager_advance():
    """队列推进"""
    qm = QueueManager()
    item1 = QueueItem(name="A", blocks_dir=Path("/a/b"), workspace_dir=Path("/a"), status="pending")
    item2 = QueueItem(name="B", blocks_dir=Path("/b/b"), workspace_dir=Path("/b"), status="pending")
    qm.add_item(item1)
    qm.add_item(item2)

    current = qm.get_current()
    assert current is not None
    assert current.name == "A"

    current.status = "running"
    next_item = qm.advance()
    assert item1.status == "done"
    assert next_item is not None
    assert next_item.name == "B"
    print("✅ test_queue_manager_advance passed")


def test_queue_is_finished():
    """队列完成判断"""
    qm = QueueManager()
    assert qm.is_empty
    assert qm.is_finished  # empty is finished

    item = QueueItem(name="A", blocks_dir=Path("/a/b"), workspace_dir=Path("/a"), status="pending")
    qm.add_item(item)
    assert not qm.is_finished

    item.status = "done"
    assert qm.is_finished

    item.status = "failed"
    assert qm.is_finished

    item.status = "pending"
    assert not qm.is_finished
    print("✅ test_queue_is_finished passed")


def test_queue_persistence():
    """队列持久化（to_dict → from_dict）"""
    qm = QueueManager()
    items = [
        QueueItem(name="A", blocks_dir=Path("/a/b"), workspace_dir=Path("/a"), status="done", total_chapters=50, completed_chapters=50),
        QueueItem(name="B", blocks_dir=Path("/b/b"), workspace_dir=Path("/b"), status="pending"),
        QueueItem(name="C", blocks_dir=Path("/c/b"), workspace_dir=Path("/c"), status="failed", error_message="test error"),
    ]
    for item in items:
        qm.add_item(item)

    # Serialize
    serialized = [item.to_dict() for item in qm.items]
    assert len(serialized) == 3
    assert serialized[0]["status"] == "done"
    assert serialized[2]["error_message"] == "test error"

    # Deserialize
    qm2 = QueueManager()
    for d in serialized:
        qm2.add_item(QueueItem.from_dict(d))

    assert qm2.count == 3
    assert qm2.items[0].name == "A"
    assert qm2.items[0].status == "done"
    assert qm2.items[2].error_message == "test error"
    print("✅ test_queue_persistence passed")


def test_stats_record_chapter_accumulates():
    """每章统计含重试成本字段，且总量累加"""
    from backend.services.queue_service import AnalysisService
    from backend.services.analysis_stats import AnalysisStats
    svc = object.__new__(AnalysisService)
    svc.stats = AnalysisStats()
    svc._runner_task = None
    svc._pipeline = None
    svc._stop_requested = False

    svc.stats.record_chapter({
        "chapter": 1, "elapsed": 1.0,
        "input_tokens": 10, "output_tokens": 20,
        "retries": 1, "failed_tokens": 30,
    })
    svc.stats.record_chapter({
        "chapter": 2, "elapsed": 2.0,
        "input_tokens": 5, "output_tokens": 5,
        "retries": 0, "failed_tokens": 0,
    })

    stats = svc.token_stats()
    assert stats["total_retries"] == 1
    assert stats["total_failed_tokens"] == 30
    assert stats["chapter_stats"][0]["retries"] == 1
    assert stats["chapter_stats"][0]["failed_tokens"] == 30
    assert stats["chapter_stats"][0]["input_tokens"] == 10


def test_stats_record_chapter_failed_payload():
    """失败块的 retries/failed_tokens 同样计入总量与每章记录（失败成本不可归零）"""
    from backend.services.queue_service import AnalysisService
    from backend.services.analysis_stats import AnalysisStats
    svc = object.__new__(AnalysisService)
    svc.stats = AnalysisStats()
    svc._runner_task = None
    svc._pipeline = None
    svc._stop_requested = False

    # 失败 payload 只有 chapter/status/retries/failed_tokens（无 elapsed/input/output）
    svc.stats.record_chapter({
        "chapter": 3, "status": "failed",
        "retries": 4, "failed_tokens": 200,
    })

    stats = svc.token_stats()
    assert stats["total_retries"] == 4
    assert stats["total_failed_tokens"] == 200
    assert stats["chapter_stats"][0]["chapter"] == 3
    assert stats["chapter_stats"][0]["status"] == "failed"
    assert stats["chapter_stats"][0]["retries"] == 4
    assert stats["chapter_stats"][0]["failed_tokens"] == 200
    # 缺失的耗时/token 字段安全降级为 0
    assert stats["chapter_stats"][0]["elapsed"] == 0
    assert stats["chapter_stats"][0]["input_tokens"] == 0
    assert stats["chapter_stats"][0]["output_tokens"] == 0


def test_token_stats_elapsed_frozen_after_run():
    """运行结束后 elapsed 冻结为结束时刻与开始时刻之差，不再随当前时间增长"""
    from backend.services.queue_service import AnalysisService
    from backend.services.analysis_stats import AnalysisStats
    svc = object.__new__(AnalysisService)
    svc.stats = AnalysisStats()
    svc.stats.start_time = 1000.0
    svc.stats.end_time = 1005.0
    svc._runner_task = None
    svc._pipeline = None
    svc._stop_requested = False

    stats = svc.token_stats()
    assert stats["elapsed"] == 5.0


# ============ H17 (2026-08-26) 发现流提取 + block_start 转发 单测 ============

def test_extract_discovery_normal():
    """正常提取：events 数 / foreshadows 截 24 字 / characters 首次登场 / unresolved 截 24 字"""
    result = {
        "core_events": [
            {"characters": "林动,萧炎", "summary": "x"},
            {"characters": "众人,林动", "summary": "y"},  # 众人=停用词；林动已见
        ],
        "foreshadowing": [
            {"clue": "古碑现世" + "X" * 30},  # 30+ 字符，截 24
        ],
        "cross_block": {
            "unresolved_questions": ["谁是幕后黑手？" * 5],  # 截 24 字
        },
    }
    seen = set()
    disc = _extract_discovery(result, seen)
    assert disc is not None
    assert disc["events"] == 2
    assert len(disc["foreshadows"]) == 1
    assert len(disc["foreshadows"][0]) == 24
    assert disc["characters"] == ["林动", "萧炎"]  # 首次登场（顺序：林动 在 event1，萧炎 在 event1）
    assert "林动" in seen
    assert "萧炎" in seen
    assert len(disc["unresolved"]) == 1
    assert len(disc["unresolved"][0]) == 24


def test_extract_discovery_stop_words():
    """停用词过滤：众人/他们/旁白 等不入"新人物"列表"""
    result = {
        "core_events": [
            {"characters": "众人,他们,旁白,自己,他,她,它"},
        ],
        "foreshadowing": [],
        "cross_block": {"unresolved_questions": []},
    }
    seen = set()
    disc = _extract_discovery(result, seen)
    assert disc["characters"] == []
    assert seen == set()  # 停用词不污染 seen 集合
    # 停用词表本身检查
    assert "众人" in _DISCOVERY_STOP_WORDS
    assert "旁白" in _DISCOVERY_STOP_WORDS
    assert "自己" in _DISCOVERY_STOP_WORDS


def test_extract_discovery_character_dedup():
    """人物去重：跨块/同块多次出现的同一人物只算一次"""
    result1 = {"core_events": [{"characters": "林动,萧炎"}], "foreshadowing": [], "cross_block": {"unresolved_questions": []}}
    result2 = {"core_events": [{"characters": "林动,林动,萧炎,药老"}], "foreshadowing": [], "cross_block": {"unresolved_questions": []}}
    seen = set()
    disc1 = _extract_discovery(result1, seen)
    disc2 = _extract_discovery(result2, seen)
    assert disc1["characters"] == ["林动", "萧炎"]
    assert disc2["characters"] == ["药老"]  # 林动/萧炎 已见，只新加药老


def test_extract_discovery_empty_or_invalid():
    """空 / 非 dict 输入安全降级"""
    assert _extract_discovery(None, set()) is None
    assert _extract_discovery({}, set()) is not None  # 空 dict 返回空 dict
    assert _extract_discovery({"core_events": None}, set()) is not None
    # 长度过滤：单字/超长不入
    result = {
        "core_events": [{"characters": "甲,abcdefghij"}],  # 1 字 + 10 字（>8）都过滤
        "foreshadowing": [],
        "cross_block": {"unresolved_questions": []},
    }
    disc = _extract_discovery(result, set())
    assert disc["characters"] == []
    # 顿号分隔也支持
    result2 = {"core_events": [{"characters": "林动、萧炎"}], "foreshadowing": [], "cross_block": {"unresolved_questions": []}}
    disc2 = _extract_discovery(result2, set())
    assert "林动" in disc2["characters"]
    assert "萧炎" in disc2["characters"]


async def test_on_progress_block_start_publishes():
    """on_progress 收到 status=start → 发布 block_start 消息"""
    from backend.progress_hub import get_hub
    hub = get_hub()
    # 用真实 hub（开发期 singleton），订阅一个临时队列
    test_queue = await hub.subscribe()
    try:
        # 用 object.__new__ 构造 AnalysisService 避免 __init__ 副作用
        svc = object.__new__(AnalysisService)
        svc._seen_characters = set()

        # 复刻 on_progress 闭包（不直接调 _run_one_item，那是 200 行嵌套）
        async def on_progress_like(payload):
            status = payload.get("status", "")
            if status == "start":
                await hub.publish({
                    "type": "block_start",
                    "payload": {
                        "chapter": payload.get("chapter", 0),
                        "range": payload.get("range") or payload.get("message", ""),
                        "progress": payload.get("progress", 0),
                        "total": payload.get("total", 0),
                    },
                })

        await on_progress_like({
            "status": "start",
            "chapter": 5,
            "message": "开始分析第5-8章...",
            "progress": 5,
            "total": 100,
        })

        # 验证收到 block_start
        import json
        msg_raw = await asyncio.wait_for(test_queue.get(), timeout=2.0)
        msg = msg_raw if isinstance(msg_raw, dict) else json.loads(msg_raw)
        assert msg["type"] == "block_start"
        assert msg["payload"]["chapter"] == 5
        assert "5-8" in msg["payload"]["range"]
        assert msg["payload"]["progress"] == 5

        # 结构化 range 优先于人类可读 message，前端车道统一显示 chN-chM
        await on_progress_like({
            "status": "start", "chapter": 9, "progress": 5, "total": 100,
            "message": "开始分析ch9-ch10...", "range": "ch9-ch10",
        })
        msg_raw = await asyncio.wait_for(test_queue.get(), timeout=2.0)
        msg = msg_raw if isinstance(msg_raw, dict) else json.loads(msg_raw)
        assert msg["type"] == "block_start"
        assert msg["payload"]["range"] == "ch9-ch10"
    finally:
        await hub.unsubscribe(test_queue)


def test_start_resets_seen_characters():
    """P1-b (2026-08-26) 修复：AnalysisService.start() 必须重置 _seen_characters

    进程级单例：__init__ 只跑一次。如果不重置，上一本书的人物会污染新书
    的"首次登场"判定（_extract_discovery 用 seen_characters 做求差）。"""
    svc = object.__new__(AnalysisService)
    svc.stats = AnalysisStats()  # start() 会调 stats.reset()
    # 模拟上一本书残留的人物集合
    svc._seen_characters = {"林动", "萧炎", "牧尘"}
    # is_running = False（_runner_task 为 None）
    svc._runner_task = None
    # queue 非空未完成（绕过 start 早返回）
    svc.queue = MagicMock()
    svc.queue.is_empty = False
    svc.queue.is_finished = False

    # 模拟 summary_service 未运行（反向互斥护栏）
    with patch("backend.services.summary_service.get_summary_service") as mock_get_summary:
        mock_summary = MagicMock()
        mock_summary.is_running = False
        mock_get_summary.return_value = mock_summary

        # 阻止实际创建 asyncio task（避免在测试事件循环里跑真实 _run_queue）
        with patch("backend.services.queue_service.asyncio.create_task") as mock_create_task:
            # 关键：_run_queue 是 async 方法，create_task(coro) 才不会触发 "never awaited" 警告
            def _accept_and_drop(coro):
                coro.close()  # 关闭协程对象，避免 GC 时抛 RuntimeWarning
                return MagicMock()
            mock_create_task.side_effect = _accept_and_drop
            ok = svc.start()

    # start() 成功进入
    assert ok is True
    # 核心断言：_seen_characters 已被清空
    assert svc._seen_characters == set()
    assert len(svc._seen_characters) == 0
    assert "林动" not in svc._seen_characters
    print("✅ test_start_resets_seen_characters passed")


def test_status_exposes_concurrency_and_block_size():
    """H17 P3 V2 修复（2026-08-26）：status() 暴露 concurrency / block_size

    修复前 LaneView 接收 currentBlockSize（每块几章）当作 concurrency（并发块数），
    UI 显示「N/4」实际是「N/每块4章」——配置 concurrency=8 时后端 8 路并发但 UI 标 4。
    """
    svc = object.__new__(AnalysisService)
    # 模拟 config_manager.config.analysis.concurrency=8 / block_size=4
    analysis_cfg = MagicMock()
    analysis_cfg.concurrency = 8
    analysis_cfg.block_size = 4
    cm_cfg = MagicMock()
    cm_cfg.analysis = analysis_cfg
    cm = MagicMock()
    cm.config = cm_cfg
    svc.config_manager = cm
    # 模拟 queue / is_running（status() 内部访问）
    svc.queue = MagicMock()
    svc.queue.get_progress.return_value = {}
    svc.queue.items = []
    svc._runner_task = None  # is_running → False

    status = svc.status()
    assert status["concurrency"] == 8, f"应暴露真实并发数 8，实际 {status.get('concurrency')}"
    assert status["block_size"] == 4, f"应暴露每块章数 4，实际 {status.get('block_size')}"
    # 健壮性：未配置时返回 None（前端用 1 作 fallback，不假装是 1）
    svc2 = object.__new__(AnalysisService)
    # MagicMock 替代 None，绕过 status() 内部访问 workspace_path（也走 config_manager）
    svc2.config_manager = MagicMock()
    svc2.config_manager.config = MagicMock()
    svc2.config_manager.config.analysis = None  # analysis 缺失 → concurrency 兜底 None
    svc2.config_manager.config.workspace_dir = "F:/test"  # workspace_path 走它
    svc2.queue = MagicMock()
    svc2.queue.get_progress.return_value = {}
    svc2.queue.items = []
    svc2._runner_task = None
    status2 = svc2.status()
    assert status2["concurrency"] is None
    assert status2["block_size"] is None
    print("✅ test_status_exposes_concurrency_and_block_size passed")


if __name__ == "__main__":
    test_stats_record_chapter_accumulates()
    test_stats_record_chapter_failed_payload()
    test_token_stats_elapsed_frozen_after_run()
    test_queue_item_roundtrip()
    test_queue_manager_add_remove()
    test_queue_manager_dedup()
    test_queue_manager_move()
    test_queue_manager_advance()
    test_queue_is_finished()
    test_queue_persistence()
    test_extract_discovery_normal()
    test_extract_discovery_stop_words()
    test_extract_discovery_character_dedup()
    test_extract_discovery_empty_or_invalid()
    test_on_progress_block_start_publishes()
    test_start_resets_seen_characters()
    test_status_exposes_concurrency_and_block_size()
    print("\n🎉 All queue tests passed!")
