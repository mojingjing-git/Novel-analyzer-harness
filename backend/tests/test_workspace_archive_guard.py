"""归档移动失败且回滚亦失败时，返回错误必须携带滞留目录名（否则用户
以为书丢了——实际改名滞留在工作区）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.services import workspace_service


def test_archive_move_failure_reports_stuck_tmp_name(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    book = ws / "书A"
    book.mkdir(parents=True)
    monkeypatch.setattr(workspace_service, "_get_workspace_path", lambda: ws)
    monkeypatch.setattr(workspace_service, "_get_archive_root", lambda: tmp_path / "archive")

    def boom(src, dst):
        raise OSError("模拟网络盘故障")

    monkeypatch.setattr(workspace_service.shutil, "move", boom)

    orig_rename = Path.rename
    calls = {"n": 0}

    def fake_rename(self, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            return orig_rename(self, dst)      # 第一步 workspace 内改名成功
        raise OSError("回滚也失败")            # 第二步回滚 rename 失败

    monkeypatch.setattr(Path, "rename", fake_rename)

    result = workspace_service.archive_novel("书A")
    assert result["ok"] is False
    assert "书A-" in result["error"], "错误信息必须包含滞留的临时目录名"
