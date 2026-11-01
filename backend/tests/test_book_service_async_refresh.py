"""get_book_path miss 后不得再在事件循环内阻塞式全量扫描；
冷启动（映射为空）除外。"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.services import book_service


class _StubService:
    def __init__(self, roots):
        from types import SimpleNamespace
        self.queue = SimpleNamespace(items=[])
        self.config_manager = SimpleNamespace(
            config=SimpleNamespace(working_directory=""))
        self.workspace_path = roots


def test_warm_miss_does_not_block(monkeypatch, tmp_path):
    """_books 已预热时 miss：不得同步调 refresh_books（会冻结循环），
    应触发后台单飞并立即返回 None"""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(book_service, "_books", {"已知书": ws})
    monkeypatch.setattr(book_service, "_manual_books", {})

    spawned = []
    class FakeThread:
        def __init__(self, target=None, daemon=None, **k):
            spawned.append(target)
            self.daemon = daemon
        def start(self): pass
    monkeypatch.setattr(book_service.threading, "Thread", FakeThread)

    result = book_service.get_book_path("不存在的新书")
    assert result is None
    assert len(spawned) == 1, "miss 应回台触发一次单飞刷新"


def test_cold_start_still_blocks_for_usability(monkeypatch, tmp_path):
    """_books 为空（进程刚起）：保留一次阻塞刷新保证首查可用"""
    calls = {"n": 0}
    def fake_refresh():
        calls["n"] += 1
        book_service._books.clear()
        return {}
    monkeypatch.setattr(book_service, "_books", {})
    monkeypatch.setattr(book_service, "refresh_books", fake_refresh)
    monkeypatch.setattr(book_service, "_manual_books", {})

    result = book_service.get_book_path("任意")
    assert calls["n"] == 1
    assert result is None


def test_single_flight_no_stampede(monkeypatch, tmp_path):
    """刷新进行中时后续 miss 不叠加线程"""
    monkeypatch.setattr(book_service, "_books", {"a": tmp_path})
    monkeypatch.setattr(book_service, "_manual_books", {})
    monkeypatch.setattr(book_service, "_refreshing", True)

    spawned = []
    class FakeThread:
        def __init__(self, *a, **k): pass
        def start(self): pass
    monkeypatch.setattr(book_service.threading, "Thread", FakeThread)

    assert book_service.get_book_path("不存在") is None
    assert not spawned, "单飞保护下不应重复拉起刷新线程"
