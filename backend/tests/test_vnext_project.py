"""vnext 项目：建项目 / 读项目 / 清单提交（PLAN v2 §4 S2-2、S2-3、S2-4）。"""

from __future__ import annotations

import json
import threading

import pytest

from backend.vnext import project
from backend.vnext.constants import FORMAT_ID, SCHEMA_VERSION, VnextFormatError


def _chapters(n: int = 3, *, prefix: str = "第{i}章\n正文内容{i}\n") -> list[tuple[int, str]]:
    return [(i, prefix.format(i=i)) for i in range(1, n + 1)]


def test_create_project_writes_manifest_chapters_and_index(tmp_path) -> None:
    book = project.create_project(tmp_path, "《测试书》", _chapters(3), book_id="bk_test000001")
    assert project.manifest_path(book).is_file()
    assert project._source_index_path(book).is_file()
    files = sorted(p.name for p in project.chapters_dir(book).iterdir())
    assert len(files) == 3
    assert all(name.endswith(".txt") and name.startswith("ch_") for name in files)


def test_manifest_roundtrip_and_format_marker(tmp_path) -> None:
    book = project.create_project(tmp_path, "《测试书》", _chapters(2), book_id="bk_test000002")
    raw = json.loads(project.manifest_path(book).read_text(encoding="utf-8"))
    assert raw["format_id"] == FORMAT_ID
    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["revision"] == 1

    m = project.load_manifest(book)
    assert m.book_id == "bk_test000002"
    assert [c.ordinal for c in m.chapters] == [1, 2]
    assert all(len(c.content_hash) == 64 for c in m.chapters)


def test_chapter_text_is_normalized_on_disk(tmp_path) -> None:
    book = project.create_project(
        tmp_path, "《测试书》", [(1, "第一章\r\n正文  \r\n")], book_id="bk_test000003"
    )
    m = project.load_manifest(book)
    text = project.read_chapter_text(book, m.chapters[0].chapter_id)
    assert text == "第一章\n正文\n"


def test_display_no_gaps_are_preserved(tmp_path) -> None:
    """章号不要求连续：断号是原文事实，不能重编号。"""
    book = project.create_project(
        tmp_path, "《测试书》", [(1, "甲\n"), (5, "乙\n"), (9, "丙\n")], book_id="bk_test000004"
    )
    m = project.load_manifest(book)
    assert [c.display_no for c in m.chapters] == [1, 5, 9]
    assert [c.ordinal for c in m.chapters] == [1, 2, 3]


def test_chapter_id_does_not_depend_on_text(tmp_path) -> None:
    """**核心不变量**：正文变了 chapter_id 不能变，否则"改一章"会变成"换了一章"，
    下游 STALE_INPUT 无法沿依赖传播。"""
    a = project.create_project(tmp_path / "a", "书", [(1, "原始正文\n")], book_id="bk_same000001")
    b = project.create_project(tmp_path / "b", "书", [(1, "完全不同的正文\n")], book_id="bk_same000001")
    ma, mb = project.load_manifest(a), project.load_manifest(b)
    assert ma.chapters[0].chapter_id == mb.chapters[0].chapter_id
    assert ma.chapters[0].content_hash != mb.chapters[0].content_hash
    # 快照 ID 随正文变化
    assert ma.source_snapshot_id != mb.source_snapshot_id


def test_identical_text_chapters_get_distinct_ids(tmp_path) -> None:
    """两章正文完全一样时，身份仍要能区分（靠 ordinal，不靠内容）。"""
    book = project.create_project(
        tmp_path, "书", [(1, "同样的一章\n"), (2, "同样的一章\n")], book_id="bk_dup0000001"
    )
    m = project.load_manifest(book)
    ids = {c.chapter_id for c in m.chapters}
    assert len(ids) == 2
    assert m.chapters[0].content_hash == m.chapters[1].content_hash


def test_legacy_dir_without_manifest_is_rejected(tmp_path) -> None:
    """旧目录（有 blocks/ 但无清单）必须报"不属于本版本"。"""
    legacy = tmp_path / "旧书"
    (legacy / "blocks").mkdir(parents=True)
    (legacy / "blocks" / "0001.txt").write_text("第一章", encoding="utf-8")
    with pytest.raises(VnextFormatError) as exc:
        project.load_manifest(legacy)
    assert exc.value.code.value == "UNSUPPORTED_ARTIFACT_FORMAT"
    assert "从原文创建新项目" in exc.value.detail or "不属于本版本" in exc.value.detail


def test_missing_required_field_is_not_filled_with_default(tmp_path) -> None:
    book = project.create_project(tmp_path, "书", _chapters(1), book_id="bk_missing00001")
    path = project.manifest_path(book)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("book_id")
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(VnextFormatError) as exc:
        project.load_manifest(book)
    assert exc.value.code.value == "MISSING_REQUIRED_FIELD"
    assert "book_id" in exc.value.detail


def test_create_project_refuses_to_overwrite(tmp_path) -> None:
    project.create_project(tmp_path, "书", _chapters(1), book_id="bk_twice000001")
    with pytest.raises(VnextFormatError):
        project.create_project(tmp_path, "书", _chapters(1), book_id="bk_twice000001")


def test_create_project_requires_at_least_one_chapter(tmp_path) -> None:
    with pytest.raises(VnextFormatError):
        project.create_project(tmp_path, "空书", [])


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------


def test_commit_manifest_bumps_revision_and_persists(tmp_path) -> None:
    book = project.create_project(tmp_path, "书", _chapters(1), book_id="bk_commit00001")

    def mut(m):
        m.active_blocks.append("sig-1")

    out = project.commit_manifest(book, mut)
    assert out.revision == 2
    assert project.load_manifest(book).active_blocks == ["sig-1"]


def test_commit_manifest_detects_stale_expected_revision(tmp_path) -> None:
    """拿到旧 revision 就提交 → 明确冲突，不静默覆盖。"""
    book = project.create_project(tmp_path, "书", _chapters(1), book_id="bk_conflict001")
    project.commit_manifest(book, lambda m: None)  # revision -> 2
    with pytest.raises(project.RevisionConflict) as exc:
        project.commit_manifest(book, lambda m: m.active_blocks.append("x"), expected_revision=1)
    assert exc.value.expected == 1 and exc.value.actual == 2
    assert project.load_manifest(book).active_blocks == []


def test_concurrent_commits_do_not_lose_updates(tmp_path) -> None:
    """同一本书 12 个并发提交：每次都要落地，不能互相覆盖。"""
    book = project.create_project(tmp_path, "书", _chapters(1), book_id="bk_threads0001")
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            project.commit_manifest(book, lambda m: m.active_blocks.append(f"sig-{i}"))
        except BaseException as exc:  # noqa: BLE001 - 测试里要看到所有异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    m = project.load_manifest(book)
    assert m.revision == 13
    assert len(m.active_blocks) == 12


# ---------------------------------------------------------------------------
# 块签名
# ---------------------------------------------------------------------------


def test_block_signature_for_uses_manifest_hashes(tmp_path) -> None:
    book = project.create_project(tmp_path, "书", _chapters(3), book_id="bk_blocks00001")
    m = project.load_manifest(book)
    ids = [c.chapter_id for c in m.chapters]
    sig = project.block_signature_for(m, ids)
    assert sig == project.block_signature_for(m, ids)
    assert sig != project.block_signature_for(m, list(reversed(ids)))


def test_block_signature_rejects_unknown_chapter(tmp_path) -> None:
    book = project.create_project(tmp_path, "书", _chapters(1), book_id="bk_unknown0001")
    m = project.load_manifest(book)
    with pytest.raises(VnextFormatError) as exc:
        project.block_signature_for(m, ["ch_notexist01"])
    assert exc.value.code.value == "MISSING_REQUIRED_FIELD"
