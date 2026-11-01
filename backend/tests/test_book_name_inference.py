"""
书名推断测试
- splitter 写 metadata.json.title 用 infer_book_name 结果
- _looks_valid_book_name 拒"句末标点"和"作者/著"两种错值
- detect_book_name 错值 fallback 到目录名
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.services import splitter_service
from backend.services.final_summary import (
    detect_book_name,
    _looks_valid_book_name,
    _clean_book_name,
)


def test_splitter_save_to_workspace_writes_inferred_title(tmp_path):
    """修法 A：save_to_workspace 透传 book_name 到 metadata.json.title"""
    # 准备：1 个合法的 txt，文件名含《书名》
    src = tmp_path / "《测试书》.txt"
    src.write_text(
        "第一章 测试章节\n这是第一章内容。\n\n第二章 第二章节\n这是第二章内容。\n" * 50,
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    result = splitter_service.save_to_workspace(
        src, workspace, "《测试书》",
        options=splitter_service.SplitOptions(),
    )

    meta_path = workspace / "《测试书》" / "blocks" / "metadata.json"
    assert meta_path.exists(), "metadata.json 应被创建"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["title"] == "《测试书》", f"期望《测试书》，实际: {meta.get('title')!r}"
    print(f"✓ 修法 A 验证：metadata.json.title = {meta['title']!r}")


def test_looks_valid_book_name_rejects_pollution():
    """兜底：_looks_valid_book_name 拒含句末标点 / "作者" / "著" 的 title"""
    # 合法书名 → True
    assert _looks_valid_book_name("轮回乐园") is True
    assert _looks_valid_book_name("凡人修仙传") is True
    # 错值：含 "。" → False
    assert _looks_valid_book_name("夜晚时分，一座繁华中透露着浮躁的二线城市。") is False
    # 错值：含 "作者" 在中间 → False
    assert _looks_valid_book_name("进化的四十六亿重奏 作者：相位行者") is False
    # 错值：仅 "著" → False
    assert _looks_valid_book_name("本书 著") is False
    # 错值：含 "？" → False
    assert _looks_valid_book_name("这章讲了什么？") is False
    # 错值：含 "！" → False
    assert _looks_valid_book_name("你好！") is False
    print("✓ _looks_valid_book_name 兜底规则测试通过")


def test_clean_book_name_strips_quotes_and_parens():
    """_clean_book_name 剥离书名号 / 尾部括号备注"""
    assert _clean_book_name("《轮回乐园》") == "轮回乐园"
    assert _clean_book_name("《凡人修仙传》（校对版）") == "凡人修仙传"
    assert _clean_book_name("凡人修仙传 (校对版)") == "凡人修仙传"
    assert _clean_book_name("【大王饶命】") == "大王饶命"
    print("✓ _clean_book_name 测试通过")


def test_detect_book_name_fallback_on_pollution(tmp_path):
    """detect_book_name：metadata.json 错值时 fallback 到目录名"""
    # 模拟：手写一个"被污染"的 metadata.json
    book_dir = tmp_path / "《测试书》"
    blocks_dir = book_dir / "blocks"
    blocks_dir.mkdir(parents=True)
    (blocks_dir / "metadata.json").write_text(
        json.dumps({"title": "夜晚时分，一座繁华中透露着浮躁的二线城市。"}, ensure_ascii=False),
        encoding="utf-8",
    )
    # 探测：output_dir 模拟
    output_dir = tmp_path / "《测试书》" / "output"
    output_dir.mkdir(parents=True)

    result = detect_book_name(output_dir)
    # _looks_valid_book_name 拒"夜晚时分..." → fallback 到目录名
    assert result == "测试书", f"期望 fallback 到 '测试书'，实际: {result!r}"
    print(f"✓ detect_book_name 错值 fallback 测试通过：返回 {result!r}")


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_splitter_save_to_workspace_writes_inferred_title(tmp)
    test_looks_valid_book_name_rejects_pollution()
    test_clean_book_name_strips_quotes_and_parens()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_detect_book_name_fallback_on_pollution(tmp)
    print("\n所有测试通过")
