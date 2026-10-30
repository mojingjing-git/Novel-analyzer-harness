"""A1/A2：建项目必须是事务，外部名字不能变成任意路径。

两条都在挡同一件事——**失败要被看见，而不是变成一个看起来正常的东西**：

* 非事务建项目失败会留下没有 `manifest.json` 的目录，`registry` 把它判成 `LEGACY_DIR`，
  用户看到的是"我的新项目变成旧版了"。
* 未校验的 `book_id`/`dir_name` 能拼出 `..\\..\\x`，把文件写到数据根之外。
"""

from __future__ import annotations

import pytest

from backend.vnext import project, registry
from backend.vnext.constants import VnextFormatError

CHAPTERS = [(1, "第一章\n正文一\n"), (2, "第二章\n正文二\n")]


def _make(tmp_path, **kwargs):
    return project.create_project(tmp_path, "书", CHAPTERS, **kwargs)


# ---------------------------------------------------------------------------
# A1 事务
# ---------------------------------------------------------------------------


def test_success_leaves_no_staging_and_scans_as_project(tmp_path) -> None:
    book_dir = _make(tmp_path)
    assert (book_dir / "manifest.json").is_file()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(project.STAGING_PREFIX)] == []

    scan = registry.scan_projects(tmp_path)
    assert [ref.book_id for ref in scan.accepted] == [project.load_manifest(book_dir).book_id]
    assert scan.rejected == []


def test_failure_midway_leaves_no_half_project(tmp_path, monkeypatch) -> None:
    """写 manifest 时炸掉：目录不能留下，更不能留成"没有清单的项目"。"""
    real_write = project.write_json_atomic

    def boom(path, payload, **kwargs):
        if str(path).endswith("manifest.json"):
            raise OSError("磁盘满了")
        return real_write(path, payload, **kwargs)

    monkeypatch.setattr(project, "write_json_atomic", boom)
    with pytest.raises(OSError):
        _make(tmp_path, book_id="bk_fail000001")

    # 没有最终目录、没有暂存残留
    assert not (tmp_path / "bk_fail000001").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(project.STAGING_PREFIX)] == []
    # 关键：不会被误判成旧版本目录
    scan = registry.scan_projects(tmp_path)
    assert scan.rejected == []
    assert scan.accepted == []


def test_cleanup_removes_stale_staging(tmp_path) -> None:
    """进程被强杀时不会走 except，暂存目录只能靠启动清理。"""
    stale = tmp_path / f"{project.STAGING_PREFIX}deadbeef"
    (stale / "source" / "chapters").mkdir(parents=True)
    (stale / "source" / "chapters" / "x.txt").write_text("x", encoding="utf-8")

    # 扫描跳过点号目录 ⇒ 不清理就永远看不见
    assert registry.scan_projects(tmp_path).rejected == []
    removed = project.cleanup_stale_staging(tmp_path)
    assert removed == [stale.name]
    assert not stale.exists()


def test_existing_project_is_not_overwritten(tmp_path) -> None:
    book_dir = _make(tmp_path, book_id="bk_dup0000001")
    before = (book_dir / "manifest.json").read_text(encoding="utf-8")
    with pytest.raises(VnextFormatError):
        _make(tmp_path, book_id="bk_dup0000001")
    assert (book_dir / "manifest.json").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# A2 名字校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../escape",
        "a/b",
        "a\\b",
        "C:evil",
        "..",
        ".",
        "",
        "CON",
        "nul.txt",
        "中文书名",
        "with space",
        "-leading-dash",
        "x" * 65,
    ],
)
def test_invalid_names_are_rejected(tmp_path, bad) -> None:
    with pytest.raises(VnextFormatError) as exc:
        _make(tmp_path, dir_name=bad)
    assert exc.value.code.value == "INVALID_FIELD_VALUE"


def test_invalid_book_id_is_rejected_even_without_dir_name(tmp_path) -> None:
    """book_id 在 dir_name 缺省时会直接变成目录名，所以它自己也要校验。"""
    with pytest.raises(VnextFormatError):
        _make(tmp_path, book_id="../evil")


@pytest.mark.parametrize("good", ["bk_abc123", "A1", "book.2026-10", "x" * 64])
def test_valid_names_pass(tmp_path, good) -> None:
    book_dir = _make(tmp_path, dir_name=good)
    assert book_dir.name == good
    assert book_dir.parent == tmp_path


def test_validator_is_the_only_path_component_gate() -> None:
    """文档化契约：非法值统一抛 INVALID_FIELD_VALUE，不抛裸 ValueError。"""
    with pytest.raises(VnextFormatError) as exc:
        project.validate_name("a/b", field="dir_name")
    assert exc.value.code.value == "INVALID_FIELD_VALUE"
    assert exc.value.path == "dir_name"
