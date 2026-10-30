"""vnext 书目发现与索引（PLAN v2 §1.1-4/5：不猜形状、不回退解析）。"""

from __future__ import annotations

import json

import pytest

from backend.vnext import project, registry
from backend.vnext.constants import VnextFormatError


def _make_project(root, book_id: str, name: str, n: int = 3):
    chapters = [(i, f"第{i}章\n正文{i}\n") for i in range(1, n + 1)]
    return project.create_project(root, name, chapters, book_id=book_id)


def test_scan_accepts_vnext_project(tmp_path) -> None:
    book = _make_project(tmp_path / "data", "bk_aaaaaa00001", "测试书")
    scan = registry.scan_projects(tmp_path / "data")

    assert [p.book_id for p in scan.accepted] == ["bk_aaaaaa00001"]
    assert scan.accepted[0].path == book
    assert scan.accepted[0].total_chapters == 3
    assert scan.rejected == []


def test_legacy_dir_is_rejected_not_guessed(tmp_path) -> None:
    """旧目录即使长得像书（有 blocks/ 和 output/），也必须被拒绝。

    旧实现用"有没有 blocks/ 就算书"，结果任何同名目录都会被当书打开——
    打开后发现字段不对，再靠默认值兜底，错误就被吞掉了。
    """
    root = tmp_path / "data"
    legacy = root / "旧书"
    (legacy / "blocks").mkdir(parents=True)
    (legacy / "output").mkdir(parents=True)
    (legacy / "blocks" / "0001.txt").write_text("正文", encoding="utf-8")

    scan = registry.scan_projects(root)
    assert scan.accepted == []
    assert [(r.path.name, r.reason) for r in scan.rejected] == [("旧书", "LEGACY_DIR")]


def test_corrupt_manifest_is_reported_as_unparseable(tmp_path) -> None:
    root = tmp_path / "data"
    bad = root / "坏书"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("{oops", encoding="utf-8")

    scan = registry.scan_projects(root)
    assert scan.accepted == []
    assert scan.rejected[0].reason == "UNPARSEABLE"


def test_manifest_with_wrong_format_is_unsupported(tmp_path) -> None:
    root = tmp_path / "data"
    other = root / "别的版本"
    other.mkdir(parents=True)
    (other / "manifest.json").write_text(
        json.dumps({"format_id": "novel-analyzer", "schema_version": 1}, ensure_ascii=False),
        encoding="utf-8",
    )

    scan = registry.scan_projects(root)
    assert scan.rejected[0].reason == "UNSUPPORTED_FORMAT"


def test_scan_ignores_files_and_hidden_dirs(tmp_path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / "readme.txt").write_text("x", encoding="utf-8")
    (root / ".trash").mkdir()

    scan = registry.scan_projects(root)
    assert scan.accepted == [] and scan.rejected == []


def test_index_roundtrip_uses_relative_paths(tmp_path) -> None:
    data = tmp_path / "data"
    _make_project(data, "bk_bbbbbb00002", "乙书")
    registry.refresh_index(data)

    raw = json.loads((data / "books_vnext.json").read_text(encoding="utf-8"))
    assert raw["format_id"] == "novel-analyzer-vnext"
    assert not raw["books"][0]["path"].startswith("/")  # 相对路径，可搬目录
    assert "bk_bbbbbb00002" in raw["books"][0]["path"]

    index = registry.read_index(data)
    assert list(index) == ["bk_bbbbbb00002"]
    assert index["bk_bbbbbb00002"].path.exists()


def test_index_missing_file_means_empty(tmp_path) -> None:
    assert registry.read_index(tmp_path / "空") == {}


def test_index_rejects_entry_without_book_id(tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "books_vnext.json").write_text(
        json.dumps(
            {"format_id": "novel-analyzer-vnext", "schema_version": 1, "books": [{"name": "x"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(VnextFormatError):
        registry.read_index(data)


def test_open_project_by_book_id_not_by_name(tmp_path) -> None:
    data = tmp_path / "data"
    _make_project(data, "bk_cccccc00003", "同名书")
    _make_project(data, "bk_dddddd00004", "同名书")  # 同名不同 id

    manifest = registry.open_project(data, "bk_dddddd00004")
    assert manifest.book_id == "bk_dddddd00004"
    assert registry.scan_projects(data).rejected == []  # id 唯一，不靠名字区分


def test_open_project_unknown_id_raises(tmp_path) -> None:
    data = tmp_path / "data"
    _make_project(data, "bk_eeeeee00005", "书")
    with pytest.raises(VnextFormatError):
        registry.open_project(data, "bk_not_exist")
