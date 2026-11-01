"""越界队列项（workspace_dir 在工作区之外）删除时
必须使用显式路径，而不是把名字重新解析回工作区（同名误删 / 无名 500）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.services import workspace_service


def test_explicit_outside_path_is_used(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "外部书"
    outside.mkdir()

    monkeypatch.setattr(workspace_service, "_get_workspace_path", lambda: ws)
    captured = {}
    monkeypatch.setattr(workspace_service, "_move_to_trash", lambda d: captured.setdefault("target", d))

    result = workspace_service.delete_novel_to_trash("外部书", novel_path=outside)
    assert result == {"ok": True}
    assert Path(captured["target"]) == outside


def test_workspace_same_name_not_deleted_when_explicit_given(tmp_path, monkeypatch):
    """工作区存在同名书 + 提供显式路径 → 只删显式路径那本"""
    ws = tmp_path / "ws"
    (ws / "同名书").mkdir(parents=True)
    outside = tmp_path / "scan_dir" / "同名书"
    outside.mkdir(parents=True)

    monkeypatch.setattr(workspace_service, "_get_workspace_path", lambda: ws)
    captured = {}
    monkeypatch.setattr(workspace_service, "_move_to_trash", lambda d: captured.setdefault("target", d))

    result = workspace_service.delete_novel_to_trash("同名书", novel_path=outside)
    assert result == {"ok": True}
    assert Path(captured["target"]) == outside          # 删的是 scan 目录里的
    assert (ws / "同名书").exists()                      # 工作区那本安然无恙


def test_default_behavior_still_resolves_workspace(tmp_path, monkeypatch):
    """不传 novel_path 时的默认行为：按名字解析到工作区"""
    ws = tmp_path / "ws"
    book = ws / "书A"
    book.mkdir(parents=True)

    monkeypatch.setattr(workspace_service, "_get_workspace_path", lambda: ws)
    captured = {}
    monkeypatch.setattr(workspace_service, "_move_to_trash", lambda d: captured.setdefault("target", d))

    result = workspace_service.delete_novel_to_trash("书A")
    assert result == {"ok": True}
    assert Path(captured["target"]) == book


def test_missing_dir_reports_error(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(workspace_service, "_get_workspace_path", lambda: ws)
    monkeypatch.setattr(workspace_service, "_move_to_trash", lambda d: None)

    result = workspace_service.delete_novel_to_trash("不存在", novel_path=tmp_path / "nowhere")
    assert result["ok"] is False
