"""vnext 数据空间：隔离基建与常量对齐（PLAN v2 §4 S2-1）。

``pytest_isolation`` 刻意不 import ``backend.*``（它必须在任何 backend import 之前
运行），所以格式标识在两处各写一份字面量。这个文件就是那道防漂移的锁：
改一处忘了另一处，这里会红。
"""

from __future__ import annotations

import json

import pytest
import pytest_isolation

from backend.vnext import constants as C


def test_format_id_and_version_aligned() -> None:
    """两处字面量必须一致，否则隔离根写出的合成产物会被自己判成不合法。"""
    assert pytest_isolation.VNEXT_FORMAT_ID == C.FORMAT_ID
    assert pytest_isolation.VNEXT_SCHEMA_VERSION == C.SCHEMA_VERSION


def test_isolation_root_has_vnext_config() -> None:
    """合成配置必须存在且指向独立工作区，不能是旧的 workspace/。"""
    root = pytest_isolation.ISOLATION_ROOT
    cfg = json.loads((root / C.CONFIG_FILE_NAME).read_text(encoding="utf-8"))
    assert cfg["workspace_dir"] == C.WORKSPACE_DIR_NAME
    assert (root / C.WORKSPACE_DIR_NAME).is_dir()


@pytest.mark.parametrize(
    "file_name, container_key",
    [(C.QUEUE_STATE_FILE_NAME, "items"), (C.BOOK_INDEX_FILE_NAME, "books")],
)
def test_synthetic_state_files_are_valid_but_empty(file_name: str, container_key: str) -> None:
    """队列与书目索引的合成文件必须"空但合法"。

    合法：带 format_id / schema_version，能被 vnext 校验器接受。
    空：不含任何伪造的分析产物——需要产物的用例必须自己造。
    """
    path = pytest_isolation.ISOLATION_ROOT / file_name
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format_id"] == C.FORMAT_ID
    assert data["schema_version"] == C.SCHEMA_VERSION
    assert data[container_key] == []
