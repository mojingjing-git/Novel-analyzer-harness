"""P5d（2026-08-27）_archive_item 路径更新修复：

bug: shutil.move 把书目录搬到 base_dir/分析结果/ 但没更新 item.workspace_dir
后果: _books 缓存旧路径（已搬走），get_book_path 命中后 path.exists() False
      → 报"书目不存在" 即使归档后书目录实际存在
真实案例: 《大王饶命》分析完成后自动归档，auto_summary 找目录失败

修法: _archive_item 搬走成功后更新 item.workspace_dir + item.blocks_dir，
      同步调 save_queue 持久化。
"""
import os
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def test_archive_item_updates_workspace_dir():
    """_archive_item 成功后 item.workspace_dir 必须指向新位置"""
    from backend.services.queue_manager import QueueItem
    from backend.services.queue_service import AnalysisService
    from backend.config.settings import AppConfig

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # 模拟工作区目录结构
        ws = tmp_path / "workspace"
        ws.mkdir()
        book_dir = ws / "《测试书》"
        book_dir.mkdir()
        (book_dir / "blocks").mkdir()
        (book_dir / "blocks" / "0001.txt").write_text("第一章", encoding="utf-8")
        (book_dir / "output").mkdir()
        (book_dir / "output" / "chapter_1_result.json").write_text("{}", encoding="utf-8")

        item = QueueItem(
            name="《测试书》",
            blocks_dir=book_dir / "blocks",
            workspace_dir=book_dir,
            status="done",
            total_chapters=1,
        )
        # mock config_manager with auto_archive enabled
        from backend.services import book_service
        from backend.services import queue_service as queue_service_module
        from backend.services.queue_service import get_service
        svc = get_service()
        original_config_manager = svc.config_manager
        # Step0/R0：快照进程级队列/书目缓存，测试结束后恢复（避免污染其他用例）
        original_items = svc.queue._items
        original_books = dict(book_service._books)
        original_manual_books = dict(book_service._manual_books)

        class _MockCfg:
            class _Config:
                class _Analysis:
                    auto_archive = True
                analysis = _Analysis()
            config = _Config()
        svc.config_manager = _MockCfg()
        try:
            # Step0/R0 守卫：save_queue 的落盘目标必须是被隔离的运行根，
            # 不能是真实仓库 queue_state.json
            assert queue_service_module.QUEUE_STATE_FILE.parent == (
                Path(os.environ["NOVEL_ROOT"]).resolve()
            ), f"queue_state 未隔离: {queue_service_module.QUEUE_STATE_FILE}"
            svc._archive_item(item)

            # 关键断言: item.workspace_dir 必须指向新位置
            assert item.workspace_dir == ws / "分析结果" / "《测试书》", (
                f"item.workspace_dir 应该指向归档后位置，实际 {item.workspace_dir}"
            )
            assert item.blocks_dir == ws / "分析结果" / "《测试书》" / "blocks", (
                f"item.blocks_dir 应该指向归档后位置，实际 {item.blocks_dir}"
            )
            # 旧位置应该已被搬走
            assert not book_dir.exists(), f"旧位置 {book_dir} 应该已被搬走"
            # 新位置应该存在并有 blocks/ + output/
            assert item.workspace_dir.exists(), f"新位置 {item.workspace_dir} 应该存在"
            assert (item.workspace_dir / "blocks").exists()
            assert (item.workspace_dir / "output").exists()
            # book_name 不变
            assert item.name == "《测试书》", f"book_name 不变，实际 {item.name}"
        finally:
            svc.config_manager = original_config_manager
            svc.queue._items = original_items
            book_service._books.clear()
            book_service._books.update(original_books)
            book_service._manual_books.clear()
            book_service._manual_books.update(original_manual_books)


def test_archive_then_get_book_path_finds_new_location():
    """归档后 book_service.get_book_path 用 book_id 能找到新位置（之前找不到旧路径）"""
    from backend.services import book_service
    from backend.services import queue_service as queue_service_module
    from backend.services.book_service import refresh_books, get_book_path
    from backend.services.queue_manager import QueueItem, QueueManager
    from backend.services.queue_service import get_service
    from backend.config.settings import AppConfig

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ws = tmp_path / "workspace"
        ws.mkdir()
        book_dir = ws / "《测试书2》"
        book_dir.mkdir()
        (book_dir / "blocks").mkdir()
        (book_dir / "blocks" / "0001.txt").write_text("第一章", encoding="utf-8")
        (book_dir / "output").mkdir()

        # mock queue with this item
        svc = get_service()
        original_config_manager = svc.config_manager
        # Step0/R0：快照队列/书目缓存，避免 _items 替换与 _books 重建污染其他用例
        original_items = svc.queue._items
        original_books = dict(book_service._books)
        original_manual_books = dict(book_service._manual_books)

        class _MockCfg:
            class _Config:
                working_directory = None
                workspace_dir = str(ws)  # AnalysisService.workspace_path 读这个
                class _Analysis:
                    auto_archive = True
                analysis = _Analysis()
            config = _Config()
        svc.config_manager = _MockCfg()

        item = QueueItem(
            name="《测试书2》",
            blocks_dir=book_dir / "blocks",
            workspace_dir=book_dir,
            status="done",
        )
        # items 是只读 property，直接改 _items
        svc.queue._items = [item]

        try:
            # Step0/R0 守卫：归档触发的 save_queue 必须落在隔离运行根，
            # 不能覆写真实仓库 queue_state.json
            assert queue_service_module.QUEUE_STATE_FILE.parent == (
                Path(os.environ["NOVEL_ROOT"]).resolve()
            ), f"queue_state 未隔离: {queue_service_module.QUEUE_STATE_FILE}"
            # 归档
            svc._archive_item(item)

            # 重新刷新 _books（实际代码中 refresh_books 会被调用）
            refresh_books()

            # 现在应该能找到
            path = get_book_path("《测试书2》")
            assert path is not None, f"get_book_path 返回 None，但归档后书目录存在 {item.workspace_dir}"
            assert path == item.workspace_dir
            assert path.exists()
        finally:
            svc.config_manager = original_config_manager
            svc.queue._items = original_items
            book_service._books.clear()
            book_service._books.update(original_books)
            book_service._manual_books.clear()
            book_service._manual_books.update(original_manual_books)
