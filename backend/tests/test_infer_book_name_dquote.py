"""双层书名号：

regex `[《<]([^》>]{1,80})[》>]` 在双层书名号 `《《xxx》》` 上会匹配错：
- 输入: 《《xxx》》
- 错误匹配: 《《xxx》（少一个外层 》）
- 正确匹配: 《xxx》（最内层内容）

真实场景: 用户上传 `《《大王饶命》》（精校版）.txt` 后:
- 错误 infer_book_name 返回 `《《大王饶命》`（少外层）
- splitter 用这个名建目录 workspace/《《大王饶命》/
- queue_item.name = subdir.name = `《《大王饶命》`
- 后续分析/总结按 item.name 找目录，但目录名跟"逻辑书名"对不上
- 报"书目不存在: 《大王饶命》"（前端显示用 item.name 装饰，2层《》）

修法: 用 `《+([^《》]+)》+` 找任意深度的最内层内容，输出 `《content》`。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.services.splitter_service import infer_book_name


# ---------------------------------------------------------------------------
# P5c: 双层/多层书名号
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename,expected", [
    # 真实用户 bug 案例
    ("《《大王饶命》》（精校版）.txt", "《大王饶命》"),
    # 单层
    ("《从姑获鸟开始》（精校版）.txt", "《从姑获鸟开始》"),
    ("《超级能源强国》（精校版）.txt", "《超级能源强国》"),
    # 双层不带后缀
    ("《《xxx》》.txt", "《xxx》"),
    # 三层
    ("《《《xxx》》》.txt", "《xxx》"),
    # 双层 + 校对版
    ("《《xxx》》（校对版）.txt", "《xxx》"),
    # 双层 + 精校版
    ("《《xxx》》（精校版）.txt", "《xxx》"),
    # 双层 + 完整版
    ("《《xxx》》（完整版）.txt", "《xxx》"),
    # 双层 + 全本
    ("《《xxx》》（全本）.txt", "《xxx》"),
    # 双层无括号但有版本
    ("《《xxx》》精校版.txt", "《xxx》"),
    # 半角书名号
    ("<<xxx>>（精校版）.txt", "<xxx>"),
    ("<<xxx>>.txt", "<xxx>"),
    # 半角双层
    ("<<<xxx>>>.txt", "<xxx>"),
    # 无《》走 fallback 路径
    ("斗破苍穹.txt", "斗破苍穹"),
    ("斗破苍穹精校版.txt", "斗破苍穹"),
])
def test_inner_quote_extraction(filename, expected):
    actual = infer_book_name(Path(filename))
    assert actual == expected, f"{filename!r} → {actual!r} (期望 {expected!r})"
