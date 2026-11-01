"""原子写与暂存失败行为验证：
1. save_split 暂存阶段失败 → 旧章文件原样保留、无 staging 残留
2. 正常重切分 → 新旧章正确交换
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.services import splitter_service

SRC = "\n\n".join(f"第{i}章 标题{i}\n" + "正文内容。" * 120 for i in range(1, 4))


def _save_once(tmp_path):
    src = tmp_path / "book.txt"
    src.write_text(SRC, encoding="utf-8")
    out = tmp_path / "blocks"
    r = splitter_service.save_split(src, out)
    return src, out, r


class BoomPool:
    """模拟线程池写盘阶段磁盘故障"""
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def map(self, fn, iterable):
        raise RuntimeError("模拟磁盘满")


def test_staging_failure_keeps_old_blocks(tmp_path, monkeypatch):
    src, out, first = _save_once(tmp_path)
    assert first["total_chapters"] == 3
    old_files = sorted(p.name for p in out.glob("*.txt"))

    monkeypatch.setattr(splitter_service, "ThreadPoolExecutor", BoomPool)
    with pytest.raises(RuntimeError):
        splitter_service.save_split(src, out)

    assert sorted(p.name for p in out.glob("*.txt")) == old_files, "旧章文件必须原样保留"
    assert not (out / "_split_staging").exists(), "暂存目录失败后必须清理"


def test_resplit_replaces_old_chapters(tmp_path):
    src, out, _ = _save_once(tmp_path)
    # 改源文件减到 2 章，再切一次
    shorter = "\n\n".join(f"第{i}章 标题{i}\n" + "正文内容。" * 120 for i in range(1, 3))
    src.write_text(shorter, encoding="utf-8")
    splitter_service.save_split(src, out)

    stems = sorted(p.stem for p in out.glob("*.txt") if p.stem.isdigit())
    assert stems == ["0001", "0002"], f"重切分后应为 2 章，实际 {stems}"
    import json
    titles = {c["index"] for c in json.loads((out / "chapters.json").read_text(encoding="utf-8"))}
    assert titles == {1, 2}
