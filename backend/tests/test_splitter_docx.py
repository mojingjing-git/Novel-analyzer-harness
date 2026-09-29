"""DOCX 输入路径测试（Plan: docs/PLAN-docx-input-2026-09-28.md）

覆盖三类风险：

1. **行锚点前提**：15 条章正则带 `^` 且配套 `len(s) <= 80` 整行闸门。DOCX 里没有换行符，
   若转换不当会让「标题+正文」合并成超长行 → 闸门判负 → **整章被静默吞掉**
   （§10 记过的同类事故）。故本文件核心目标是验证"标题独占一行"。
2. **异常分诊**：加密 docx 抛 `RuntimeError`（不是 `BadZipFile`），漏掉会被
   `routes_splitter.py` 的 `except Exception` 吞成 500。
3. **自动检测未被污染**：`_chapter_regex_for_mode` 只扫 `lines[:800]`，故必须断言
   DOCX 与 TXT 选出同一个 `detected_pattern`（这是最敏感的探针）。

注意 word_count 实为字符数（len(body)），故不做 TXT/DOCX 相等断言。
"""
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.services import splitter_service
from backend.services.splitter_service import SplitOptions, save_split
from backend.utils.docx_extract import extract_docx_text


# ---------------------------------------------------------------------------
# fixture 构造：内存中合成最小合法 docx，不入库二进制
# ---------------------------------------------------------------------------

_DOCUMENT_XML_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
)
_DOCUMENT_XML_TAIL = "</w:body></w:document>"


def _para(text: str, style: str = "") -> str:
    """构造一个 w:p 片段。style 为空则不写 pStyle。"""
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    # 用 <w:t> 而非直接把 text 塞进去，模拟真实 Word 输出（含 entity 转义由调用方保证）
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def build_docx(path: Path, xml_body: str, *, members: dict | None = None) -> Path:
    """合成一个 docx（zip）。members 可覆盖/追加内部成员名。"""
    xml = _DOCUMENT_XML_HEAD + xml_body + _DOCUMENT_XML_TAIL
    # 最小合法 docx 需要的其余成员
    defaults = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/></Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
        ),
        "word/document.xml": xml,
    }
    if members:
        defaults.update(members)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in defaults.items():
            zf.writestr(name, data)
    return path


def sample_novel_xml(n_chapters: int = 6, body_repeat: int = 30) -> str:
    """生成 n_chapters 章的正文 XML：Heading 样式标题 + 长正文段落。"""
    parts = []
    for i in range(1, n_chapters + 1):
        parts.append(_para(f"第{i}章 开局{i}", style="Heading 1"))
        parts.append(_para("正文内容。" * body_repeat))
    return "".join(parts)


def sample_novel_txt(n_chapters: int = 6, body_repeat: int = 30) -> str:
    """与 sample_novel_xml 内容对应的 TXT 版本。"""
    lines = []
    for i in range(1, n_chapters + 1):
        lines.append(f"第{i}章 开局{i}")
        lines.append("正文内容。" * body_repeat)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Case 1：等价性（核心）
# ---------------------------------------------------------------------------

def test_txt_docx_equivalence(tmp_path):
    """TXT 与 DOCX 必须切出相同的 index/title/volume/num 与 detected_pattern。

    word_count 不断言——它是字符数（len(body)），DOCX 路径因标题前后补空行必然漂升。
    """
    txt_path = tmp_path / "book.txt"
    docx_path = tmp_path / "book.docx"
    txt_path.write_text(sample_novel_txt(), encoding="utf-8")
    build_docx(docx_path, sample_novel_xml())

    out_txt = tmp_path / "out_txt"
    out_docx = tmp_path / "out_docx"
    save_split(txt_path, out_txt)
    save_split(docx_path, out_docx)

    from backend.services.splitter_service import preview_split

    p_txt = preview_split(txt_path, preview_count=1000)
    p_docx = preview_split(docx_path, preview_count=1000)

    assert p_txt["total_chapters"] == p_docx["total_chapters"] >= 5, (
        f"章节数不一致或不合理: txt={p_txt['total_chapters']} docx={p_docx['total_chapters']}"
    )
    # detected_pattern 是"自动检测是否被带偏"的最敏感探针
    assert p_txt["detected_pattern"] == p_docx["detected_pattern"], (
        f"detected_pattern 漂移: txt={p_txt['detected_pattern']!r} "
        f"docx={p_docx['detected_pattern']!r}"
    )
    signatures = lambda p: [(c["index"], c["title"], c["volume"], c["num"]) for c in p["chapters"]]
    assert signatures(p_txt) == signatures(p_docx), "四元组必须强一致"


def test_blocks_content_matches_txt(tmp_path):
    """落盘的 blocks/NNNN.txt 内容必须与 TXT 路径一致。

    复审发现：若 DOCX 路径额外注入空行，四元组断言仍会通过（元数据不涉及正文），
    但每章正文头部会多出空行。下游 style_analyzer 按「第 offset 个汉字」定位采样
    窗口（style_analyzer.py:283），偏移被推后会让风格采样错位。故必须比对正文。
    """
    txt_path = tmp_path / "book.txt"
    docx_path = tmp_path / "book.docx"
    txt_path.write_text(sample_novel_txt(n_chapters=4), encoding="utf-8")
    build_docx(docx_path, sample_novel_xml(n_chapters=4))

    out_txt = tmp_path / "out_txt"
    out_docx = tmp_path / "out_docx"
    save_split(txt_path, out_txt)
    save_split(docx_path, out_docx)

    files_txt = sorted(f.name for f in out_txt.glob("*.txt"))
    files_docx = sorted(f.name for f in out_docx.glob("*.txt"))
    assert files_txt == files_docx, "落盘文件列表不一致"

    for name in files_txt:
        a = (out_txt / name).read_text(encoding="utf-8")
        b = (out_docx / name).read_text(encoding="utf-8")
        assert a == b, (
            f"{name} 正文不一致（可能是空行/重复标题）。\n"
            f"  txt : {a[:120]!r}\n  docx: {b[:120]!r}"
        )


# ---------------------------------------------------------------------------
# Case 2：Heading 样式被识别（多种样式名）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("style", ["Heading 1", "Heading 2", "heading 3", "Title", "heading"])
def test_heading_styles_force_own_line(tmp_path, style):
    """标题段落必须独占一行（这正是 _is_heading 存在的唯一目的）。"""
    p = build_docx(tmp_path / "h.docx", _para("第1章 开局", style=style) + _para("正文。" * 20))
    text = extract_docx_text(p)
    lines = text.split("\n")
    assert lines[0] == "第1章 开局", f"标题未独占首行: {lines[:3]!r}"
    assert lines[1].startswith("正文"), f"正文应紧随其后: {lines[:3]!r}"


def test_heading_and_body_in_same_paragraph_are_split(tmp_path):
    """「标题 + 正文」共存于同一个 <w:p>（中间只有 w:br）时必须劈成两行。

    这是 _is_heading 唯一真正起作用的场景：不劈的话标题会被折进超长行，
    遭 len(s) <= 80 闸门判负 → 整章被静默吞掉。
    """
    xml = (
        '<w:p><w:pPr><w:pStyle w:val="Heading 1"/></w:pPr>'
        "<w:r><w:t>第1章 开局</w:t><w:br/><w:t>" + "内容" * 500 + "</w:t></w:r></w:p>"
    )
    text = extract_docx_text(build_docx(tmp_path / "hb.docx", xml))
    lines = text.split("\n")
    assert lines[0] == "第1章 开局", f"标题未从同段落中劈出: {lines[0][:40]!r}"
    assert len(lines[0]) <= 80, "劈出的标题行仍超长，会被闸门判负"


def test_no_extra_blank_lines_around_heading(tmp_path):
    """标题前后不得注入额外空行——必须与 TXT 行结构严格等价。"""
    xml = (_para("第1章 开局", style="Heading 1") + _para("正文。" * 20)
           + _para("第2章 继续", style="Heading 1") + _para("正文。" * 20))
    text = extract_docx_text(build_docx(tmp_path / "nb.docx", xml))
    lines = text.split("\n")
    assert lines == ["第1章 开局", "正文。" * 20, "第2章 继续", "正文。" * 20], (
        f"出现了多余的空行，会让 blocks 正文偏移: {lines!r}"
    )


def test_heading_not_merged_with_body(tmp_path):
    """核心防吞章：标题不得与正文并进同一超长行（否则 len(s)<=80 闸门判负）。"""
    p = build_docx(
        tmp_path / "m.docx",
        _para("第1章 开局", style="Heading 1") + _para("内容" * 500),
    )
    text = extract_docx_text(p)
    long_lines = [ln for ln in text.split("\n") if len(ln) > 80]
    assert all("第1章" not in ln for ln in long_lines), "标题被合并进了超长行，会被 80 字闸门吞掉"


# ---------------------------------------------------------------------------
# Case 3：表格内正文（网页转制场景）
# ---------------------------------------------------------------------------

def test_table_wrapped_body_is_extracted(tmp_path):
    """网页转制的 DOCX 常把正文塞进单单元格表格，递归遍历必须能抽到。

    复审发现：原先只断言「文本里有」，太弱——单章时自动检测的 scores>=5 闸门过不了，
    会静默回落单章且 detected_pattern 变成兜底串。故这里用 ≥6 章并要求 pattern 正确。
    """
    cell = "".join(
        _para(f"第{i}章 表格章", style="Heading 1") + _para("正文内容。" * 30)
        for i in range(1, 7)
    )
    xml = f"<w:tbl><w:tr><w:tc>{cell}</w:tc></w:tr></w:tbl>"
    p = build_docx(tmp_path / "t.docx", xml)
    text = extract_docx_text(p)
    assert "第1章 表格章" in text, "表格内正文未被抽取"

    from backend.services.splitter_service import preview_split

    pr = preview_split(p, preview_count=10)
    assert pr["total_chapters"] == 6, (
        f"表格内 6 章未能全部切出，实际 {pr['total_chapters']} 章"
        f"（pattern={pr['pattern_name']}）"
    )
    assert pr["pattern_name"] == "第X章(中文)", (
        f"表格场景 detected_pattern 退化为兜底串: {pr['pattern_name']}"
    )


# ---------------------------------------------------------------------------
# Case 4：w:tab / w:br
# ---------------------------------------------------------------------------

def test_tab_and_br_are_translated(tmp_path):
    xml = (
        '<w:p><w:r><w:t>甲</w:t><w:tab/><w:t>乙</w:t><w:br/><w:t>丙</w:t></w:r></w:p>'
    )
    text = extract_docx_text(build_docx(tmp_path / "tb.docx", xml))
    assert "甲 乙" in text, "w:tab 未转成空格"
    assert "乙\n丙" in text, "w:br 未转成换行"


# ---------------------------------------------------------------------------
# Case 5：多段落
# ---------------------------------------------------------------------------

def test_paragraph_per_line(tmp_path):
    xml = _para("第一段") + _para("第二段") + _para("第三段")
    text = extract_docx_text(build_docx(tmp_path / "p.docx", xml))
    body = [ln for ln in text.split("\n") if ln.strip()]
    assert body == ["第一段", "第二段", "第三段"]


# ---------------------------------------------------------------------------
# Case 6a：异常 E1
# ---------------------------------------------------------------------------

def test_not_a_zip_raises(tmp_path):
    p = tmp_path / "fake.docx"
    p.write_bytes(b"this is definitely not a zip file at all")
    with pytest.raises(ValueError, match="不是有效的 DOCX"):
        extract_docx_text(p)


def test_corrupt_zip_raises(tmp_path):
    p = tmp_path / "corrupt.docx"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 100)  # 有 magic 但结构损坏
    with pytest.raises(ValueError):
        extract_docx_text(p)


def test_zero_byte_raises(tmp_path):
    p = tmp_path / "zero.docx"
    p.write_bytes(b"")
    with pytest.raises(ValueError):
        extract_docx_text(p)


def test_missing_document_xml_raises(tmp_path):
    p = tmp_path / "nodoc.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("readme.txt", "no document.xml here")
    with pytest.raises(ValueError, match="缺少 word/document.xml"):
        extract_docx_text(p)


def test_empty_body_raises(tmp_path):
    p = build_docx(tmp_path / "empty.docx", "")
    with pytest.raises(ValueError, match="未抽取到任何正文"):
        extract_docx_text(p)


# ---------------------------------------------------------------------------
# Case 6b：加密docx —— RuntimeError 而非 BadZipFile
# ---------------------------------------------------------------------------

def test_encrypted_docx_reports_encryption(tmp_path):
    """加密 docx 走 RuntimeError 分支，必须给出"加密"提示而非裸 500。

    真实加密 docx 难以合成，这里在 ZipFile.read 层抛出带 password 字样的
    RuntimeError 来精确复现该分支。
    """
    p = build_docx(tmp_path / "enc.docx", sample_novel_xml())

    class _EncryptedZip(zipfile.ZipFile):
        def read(self, name, pwd=None):  # noqa: ARG002
            raise RuntimeError("File is encrypted, password required for extraction")

    import backend.utils.docx_extract as dx

    orig = dx.zipfile.ZipFile
    dx.zipfile.ZipFile = _EncryptedZip
    try:
        with pytest.raises(ValueError, match="加密"):
            extract_docx_text(p)
    finally:
        dx.zipfile.ZipFile = orig


# ---------------------------------------------------------------------------
# Case 7：自动检测未被污染（800 行采样窗口回归探针）
# ---------------------------------------------------------------------------

def test_detected_pattern_stable_on_long_book(tmp_path):
    """长书场景：`_chapter_regex_for_mode` 只扫 lines[:800]，
    DOCX 的行数膨胀不得让自动检测选出不同正则。
    """
    txt_path = tmp_path / "long.txt"
    docx_path = tmp_path / "long.docx"
    txt_path.write_text(sample_novel_txt(n_chapters=200), encoding="utf-8")
    build_docx(docx_path, sample_novel_xml(n_chapters=200))

    from backend.services.splitter_service import preview_split

    p_txt = preview_split(txt_path, preview_count=1)
    p_docx = preview_split(docx_path, preview_count=1)
    assert p_docx["detected_pattern"] == p_txt["detected_pattern"]
    assert p_docx["pattern_name"] == p_txt["pattern_name"]
    assert p_docx["total_chapters"] == p_txt["total_chapters"] >= 100
