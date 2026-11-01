"""blocks_dir=null 的队列项必须在路由层被跳过/拒绝，而不是 Path(None) 500。
本文件锁定 QueueItem.from_dict 对 null/空串的行为契约（路由包装以此为前提）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.services.queue_manager import PROJECT_ROOT, QueueItem


def test_from_dict_null_blocks_dir_raises():
    with pytest.raises(Exception):
        QueueItem.from_dict({"name": "x", "blocks_dir": None})


def test_from_dict_empty_blocks_dir_resolves_project_root():
    item = QueueItem.from_dict({"name": "x", "blocks_dir": ""})
    assert item.blocks_dir == PROJECT_ROOT  # 路由层必须据此把空串条目挡掉
