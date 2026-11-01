"""编码选择按「采样罚分择优」——
Big5 书不得落入 gb18030 乱码；两入口（detect_and_decode/detect_encoding）行为一致。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.utils.text_utils import detect_and_decode, detect_encoding

BIG5_TEXT = "這是一個測試章節，主角前往靈山尋找失落的功法。"
GBK_TEXT = "这是一个测试章节，主角前往灵山寻找失落的功法。"


def _write_bytes(p: Path, data: bytes):
    p.write_bytes(data)


def test_big5_book_not_moibaked_by_gb18030(tmp_path):
    p = tmp_path / "big5.txt"
    _write_bytes(p, BIG5_TEXT.encode("big5"))

    text = detect_and_decode(p, ["utf-8", "gbk", "gb18030", "big5"])
    assert "靈山" in text and "測試" in text, f"繁体书被错误解码：{text[:40]}"

    assert detect_encoding(p, ["utf-8", "gbk", "gb18030", "big5"]) == "big5"


def test_gbk_simplified_still_detected(tmp_path):
    p = tmp_path / "gbk.txt"
    _write_bytes(p, GBK_TEXT.encode("gbk"))
    text = detect_and_decode(p, ["utf-8", "gbk", "gb18030", "big5"])
    assert "灵山" in text
    assert detect_encoding(p, ["utf-8", "gbk", "gb18030", "big5"]) == "gbk"


def test_utf8_priority_kept(tmp_path):
    p = tmp_path / "u8.txt"
    _write_bytes(p, GBK_TEXT.encode("utf-8"))
    assert detect_encoding(p, ["utf-8", "gbk"]) == "utf-8"


def test_utf16le_nobom_ascii_still_works(tmp_path):
    p = tmp_path / "u16.txt"
    _write_bytes(p, "Hello Chapter 1.\n".encode("utf-16-le"))
    text = detect_and_decode(p, ["utf-8", "gbk", "utf-16"])
    assert "Hello Chapter 1." in text
    assert "\x00" not in text


def test_all_fail_raises(tmp_path):
    p = tmp_path / "bin.txt"
    _write_bytes(p, b"\xff\xfe\x00\x00\xff\xfe")
    with pytest.raises(Exception):
        detect_and_decode(p, ["utf-8", "gbk"])


def test_both_entries_agree(tmp_path):
    p = tmp_path / "big5b.txt"
    _write_bytes(p, ("章一：" + BIG5_TEXT).encode("big5"))
    prios = ["utf-8", "gbk", "gb18030", "big5"]
    enc = detect_encoding(p, prios)
    text = detect_and_decode(p, prios)
    # 两入口选中的编码一致（text 能以该编码无损往返）
    assert text.encode(enc).decode(enc) == text
