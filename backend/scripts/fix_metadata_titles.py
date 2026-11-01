"""
修复脚本：
扫所有 workspace 下的 blocks/metadata.json，如果 title 是错值（含句末标点 / 含"作者/著" /
超过 30 字符），用 _clean_book_name(parent_dir.name) 覆盖。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.final_summary import (
    _looks_valid_book_name,
    _clean_book_name,
)

WORKSPACE = PROJECT_ROOT / "workspace"


def fix_metadata(metadata_path: Path) -> bool:
    """修复单个 metadata.json。返回是否做了修改。"""
    try:
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] 解析失败: {e}")
        return False
    title = meta.get("title", "")
    if _looks_valid_book_name(title):
        return False  # 已是合法值，不动
    # 错值 → 用目录名覆盖（_clean 剥《》/括号）
    book_dir = metadata_path.parent.parent  # blocks/ → 书目录
    new_title = _clean_book_name(book_dir.name)
    meta["title"] = new_title
    metadata_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return True


def main():
    fixed = 0
    skipped = 0
    for blocks_dir in WORKSPACE.rglob("blocks"):
        if not blocks_dir.is_dir():
            continue
        meta = blocks_dir / "metadata.json"
        if not meta.exists():
            continue
        book = blocks_dir.parent.name
        old_title = json.loads(meta.read_text(encoding="utf-8")).get("title", "")
        was_fixed = fix_metadata(meta)
        if was_fixed:
            print(f"[FIX] {book}: {old_title!r} -> {json.loads(meta.read_text(encoding='utf-8'))['title']!r}")
            fixed += 1
        else:
            print(f"  [SKIP] {book} (title 合法)")
            skipped += 1
    print(f"\n完成: 修复 {fixed} 本, 跳过 {skipped} 本")


if __name__ == "__main__":
    main()
