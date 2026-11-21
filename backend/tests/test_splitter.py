"""切分引擎（TXT / DOCX / 古籍）、复合标题校验与书名推断规则测试。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from backend.core.chapter_split import chapters_with_hash, split_chapters
from backend.services import splitter_service
from backend.services.final_summary import _clean_book_name, _looks_valid_book_name, detect_book_name
from backend.services.splitter_service import SplitOptions, save_split
from backend.tests.fixtures.classic_corpus import CLASSIC_TEXT, EXPECTED_ROUNDS, ROUND_MARKERS


def _t(*lines: str) -> str:
    return "\n".join(lines)


def test_chapter_split_headings_and_volume_reset():
    # 普通切章与正文前瞻引用不跳号
    text = _t(
        "第一章 起",
        "这里先卖个关子，第一百回方知分晓。",
        "",
        "第十回中写道：此案另有隐情。",
        "",
        "第二章 承",
        "第二回里已经埋下伏笔。",
        "",
        "第三章 转",
        "第三回水落石出。",
    )
    spans = split_chapters(text)
    assert [s.chapter_no for s in spans] == [1, 2, 3]

    # 分卷章号重置
    vol_text = _t(
        "第一卷 风起", "", "第一章 起", "卷一第一章。", "", "第二章 承", "卷一第二章。",
        "", "第二卷 云涌", "", "第一章 再起", "卷二第一章。", "", "第二章 再承", "卷二第二章。",
    )
    vol_spans = split_chapters(vol_text)
    assert [(s.chapter_no, s.volume_idx) for s in vol_spans] == [(1, 0), (2, 0), (1, 1), (2, 1)]
    assert sum(len(v) for v in chapters_with_hash(vol_text, first_chapter=1).values()) == len(vol_spans)

    # 句末标点不作为标题；无标题整段算一章
    assert [s.chapter_no for s in split_chapters(_t("第一章 起", "正文。", "", "第二章 他终于回来了。", "正文。"))] == [1]
    single = split_chapters("就是一段没有任何标题的正文。")
    assert len(single) == 1 and single[0].chapter_no == 1 and single[0].content_hash


def test_classic_corpus_splitting():
    spans = split_chapters(CLASSIC_TEXT)
    assert [s.chapter_no for s in spans] == EXPECTED_ROUNDS
    for s in spans:
        assert ROUND_MARKERS[s.chapter_no] in s.text


def _build_docx(path: Path, n_chapters: int = 4) -> Path:
    head = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    )
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="Heading 1"/></w:pPr><w:r><w:t>第{i}章 开局{i}</w:t></w:r></w:p>'
        f'<w:p><w:r><w:t>{"正文内容。" * 30}</w:t></w:r></w:p>'
        for i in range(1, n_chapters + 1)
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        zf.writestr("word/document.xml", head + body + "</w:body></w:document>")
    return path


def test_txt_and_docx_split_equivalence(tmp_path):
    txt_path = tmp_path / "book.txt"
    docx_path = _build_docx(tmp_path / "book.docx", n_chapters=4)
    txt_path.write_text(
        "\n".join(f"第{i}章 开局{i}\n" + "正文内容。" * 30 for i in range(1, 5)),
        encoding="utf-8",
    )

    r_txt = save_split(txt_path, tmp_path / "out_txt", SplitOptions())
    r_docx = save_split(docx_path, tmp_path / "out_docx", SplitOptions())
    assert r_txt["total_chapters"] == r_docx["total_chapters"] == 4
    ch_txt = json.loads((tmp_path / "out_txt" / "chapters.json").read_text(encoding="utf-8"))
    ch_docx = json.loads((tmp_path / "out_docx" / "chapters.json").read_text(encoding="utf-8"))
    assert [c["title"] for c in ch_txt] == [c["title"] for c in ch_docx]


def test_book_name_inference_and_pollution_guard(tmp_path):
    assert _looks_valid_book_name("凡人修仙传") is True
    assert _looks_valid_book_name("夜晚时分，一座繁华中透露着浮躁的二线城市。") is False
    assert _looks_valid_book_name("进化的四十六亿重奏 作者：相位行者") is False
    assert _clean_book_name("《凡人修仙传》（校对版）") == "凡人修仙传"

    src = tmp_path / "《测试书》.txt"
    src.write_text(("第一章 测试章节\n这是第一章内容。\n\n第二章 第二章节\n这是第二章内容。\n") * 30, encoding="utf-8")
    workspace = tmp_path / "workspace"
    splitter_service.save_to_workspace(src, workspace, "《测试书》", options=SplitOptions())
    meta = json.loads((workspace / "《测试书》" / "blocks" / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "《测试书》"

    # 被污染的 metadata.json 应回退到目录名
    polluted_dir = tmp_path / "《回退书》"
    (polluted_dir / "blocks").mkdir(parents=True)
    (polluted_dir / "output").mkdir(parents=True)
    (polluted_dir / "blocks" / "metadata.json").write_text(
        json.dumps({"title": "夜晚时分，一座繁华中透露着浮躁的二线城市。"}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert detect_book_name(polluted_dir / "output") == "回退书"
