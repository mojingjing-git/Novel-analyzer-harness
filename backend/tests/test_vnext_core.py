"""vnext 核心数据层测试（Project 清单、排他上界知识快照、稳定伏笔实体与候选视图）。"""
from __future__ import annotations

import json

import pytest

from backend.vnext import foreshadow as fs
from backend.vnext import project
from backend.vnext.constants import FORMAT_ID, SCHEMA_VERSION, VnextFormatError
from backend.vnext.knowledge import FactRecord, SourceRef, build_snapshot, snapshot_for_block


def test_vnext_project_manifest_and_chapter_identity(tmp_path):
    chapters = [(1, "第一章\r\n正文1  \r\n"), (5, "第五章\n正文5\n")]
    book = project.create_project(tmp_path / "a", "《测试书》", chapters, book_id="bk_test000001")
    raw = json.loads(project.manifest_path(book).read_text(encoding="utf-8"))
    assert raw["format_id"] == FORMAT_ID and raw["schema_version"] == SCHEMA_VERSION

    m = project.load_manifest(book)
    assert [c.display_no for c in m.chapters] == [1, 5]
    assert [c.ordinal for c in m.chapters] == [1, 2]
    assert project.read_chapter_text(book, m.chapters[0].chapter_id) == "第一章\n正文1\n"

    # 正文变化不改变 chapter_id
    book_b = project.create_project(tmp_path / "b", "《测试书》", [(1, "完全不同正文\n"), (5, "第五章\n正文5\n")], book_id="bk_test000001")
    mb = project.load_manifest(book_b)
    assert m.chapters[0].chapter_id == mb.chapters[0].chapter_id
    assert m.chapters[0].content_hash != mb.chapters[0].content_hash

    # 旧目录拒绝打开
    legacy = tmp_path / "旧书"
    (legacy / "blocks").mkdir(parents=True)
    with pytest.raises(VnextFormatError):
        project.load_manifest(legacy)


def test_vnext_knowledge_snapshot_bounds_and_determinism():
    def _fact(fid: str, ch: int | None) -> FactRecord:
        src = SourceRef(chapter_id=f"ch_{ch:04d}", chapter_ordinal=ch, start=0, end=5, content_hash=f"h{ch}") if ch else None
        return FactRecord(fact_id=fid, text=f"事实{fid}", source=src)

    facts = [_fact("c", 5), _fact("a", 1), _fact("b", 4), _fact("ghost", None)]
    snap = build_snapshot(exclusive_before=5, facts=facts)
    assert [f.fact_id for f in snap.facts] == ["a", "b"]
    assert snap.dropped_unknown_range == ("ghost",)
    assert [f.fact_id for f in snapshot_for_block(block_first_ordinal=5, facts=facts).facts] == ["a", "b"]

    s2 = build_snapshot(exclusive_before=5, facts=list(reversed(facts)))
    assert snap == s2


def test_vnext_foreshadow_entity_id_and_upper_bound():
    book_id = "book-a"
    e1 = fs.make_entity_id(book_id=book_id, seed_occurrence_id="anchor-1")
    e2 = fs.make_entity_id(book_id=book_id, seed_occurrence_id="anchor-2")

    def _occ(eid: str, ordinal: int) -> fs.Occurrence:
        src = SourceRef(chapter_id=f"ch{ordinal:03d}", chapter_ordinal=ordinal, start=0, end=6, content_hash="h0")
        return fs.Occurrence(
            occurrence_id=fs.make_occurrence_id(book_id=book_id, source=src, kind="mention"),
            entity_id=eid, source=src, quote="那把伞", kind="mention",
        )

    occs = [_occ(e1, 2), _occ(e2, 3), _occ(e2, 7)]
    entities, _ = fs.register(book_id=book_id, occurrences=occs)
    assert len(entities) == 2

    # 排他上界：cutoff_ordinal=5 时，e2 在第 7 章的出现不计入可见记录
    rows = fs.cutoff_view(entities=entities, occurrences=occs, cutoff_ordinal=5)
    by_id = {r["entity_id"]: r for r in rows}
    assert by_id[e1]["mention_count"] == 1
    assert by_id[e2]["mention_count"] == 1 and by_id[e2]["last_seen_ordinal"] == 3
