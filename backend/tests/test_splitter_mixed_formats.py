"""主导格式窄化不得吞掉混排的真实章节行，也不得被高频噪声带偏。
统一入口 preview_split/save_split 走 _compile_pattern；此处直接测编译产物。

实现说明：
- splitter_service 的实际模块级函数是 _chapter_regex_for_mode(options, lines)
  （无 _compile_pattern 导出名），测试按实际签名调用；
- 原样例合并式行「第三卷 第N卷起首」在现有 CHAPTER_PATTERNS 中本就无对应候选
  （任何模式下得分均为 0，不经过该门槛逻辑），故混排样例统一使用现实中最常见的
  合并式行「第三卷 第N章」——对应既有候选「第一卷第一章」
  （^第X卷\\s*第Y[章节回]），可真实驱动次级并入路径。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.services import splitter_service
from backend.services.splitter_service import (
    SplitOptions,
    _chapter_regex_for_mode,
)


def _compile(lines):
    opts = SplitOptions()
    return _chapter_regex_for_mode(opts, [l.rstrip("\n") for l in lines])


def splitter_save(src, out):
    return splitter_service.save_split(src, out)


def test_mixed_volume_chapter_lines_are_included():
    """第X章为主 + 第三卷第X章混排：合并式行必须作为切分边界（不得被吞）"""
    lines = []
    mixed = []
    num = 0
    for v in ("一", "二"):
        for _i in range(3):
            num += 1
            lines.append(f"第{num}章 标题{num}")
            lines.append("正文内容。" * 20)
            mixed.append(f"第三卷 第{num}章")
            lines.append(mixed[-1])
    rx, name = _compile(lines)
    # 现状：第X章(中文) 字符类含 \d，与阿拉伯并列时按 CHAPTER_PATTERNS 顺序取前者
    assert name == "第X章(中文)", "pattern_name 仅作展示，不得因并入次级而改变"
    for m in mixed:
        assert rx.search(m) is not None, f"合并式行未成为切分边界: {m}"


def test_real_world_split_mixed_book(tmp_path):
    src = tmp_path / "mixed.txt"
    parts = []
    for block_start in (1, 11, 21):
        parts.append(f"第{block_start}章 卷内首页")
        parts.append("剧情推进。" * 50)
        parts.append(f"第三卷 第{block_start}章")   # 混排合并式行（此前被吞）
        parts.append("过渡剧情。" * 30)
        for c in range(block_start + 1, block_start + 4):
            parts.append(f"第{c}章 后续")
            parts.append("剧情推进。" * 50)
    src.write_text("\n".join(parts), encoding="utf-8")
    out = tmp_path / "blocks"
    splitter_save(src, out)
    chapters_json = json.loads((out / "chapters.json").read_text(encoding="utf-8"))
    # 12 个真实标题（4 组 × 3 章）+ 3 条合并式行 = 15 个独立边界
    assert len(chapters_json) == 15, \
        f"应为 15 章（12 真实标题 + 3 合并式行），实际 {len(chapters_json)}"
    mixed_titles = [c["title"] for c in chapters_json if c["title"].startswith("第三卷")]
    assert len(mixed_titles) == 3, \
        f"合并式行应成为独立章节边界，实际 {len(mixed_titles)} 条: {mixed_titles}"
    for c in chapters_json:
        if c["title"].startswith("第三卷"):
            continue
        body = (out / f"{c['index']:04d}.txt").read_text(encoding="utf-8")
        for ln in body.split("\n"):
            assert not ln.startswith("第三卷 第"), \
                f"合并式行被吞进《{c['title']}》正文（回归）: {ln[:30]}"


def test_noise_list_dominant_not_included():
    """编号列表作为次级格式且比值低于 15% 时不得并入（防过度切分回归）：
    主导 60 章，数字点编号仅 8 次 < max(2, int(60*0.15))=9 → 排除"""
    lines = []
    for i in range(1, 61):
        lines.append(f"第{i}章 标题{i}")
        lines.append("正文内容。" * 20)
    noise = [f"{i}. 列表条目内容{i}" for i in range(1, 9)]
    lines.extend(noise)
    rx, _ = _compile(lines)
    for i in range(1, 61):
        assert rx.match(f"第{i}章 标题{i}") is not None
    for n in noise:
        assert rx.match(n) is None, f"低比额噪声被并入切分边界: {n}"


def test_low_ratio_secondary_excluded():
    """次级格式只出现 1 次（< 绝对阈值 2）不并入"""
    lines = []
    for i in range(1, 9):
        lines.append(f"第{i}章 标题{i}")
        lines.append("正文内容。" * 30)
    lines.insert(4, "第三卷 第一章孤例")   # 仅 1 次
    rx, _ = _compile(lines)
    assert not rx.match("第三卷 第一章孤例")


@pytest.mark.parametrize("secondary_count,included", [(2, False), (3, True)])
def test_secondary_ratio_gate_boundary(secondary_count, included):
    """双门槛边界：主导 20 章 → 阈值 max(2, int(20*0.15))=3；
    次级 2 次（<3）排除，3 次（=3 边界相等）入选"""
    lines = []
    for i in range(1, 21):
        lines.append(f"第{i}章 标题{i}")
        lines.append("正文内容。" * 20)
    for k in range(1, secondary_count + 1):
        lines.append(f"第三卷 第{k}章")
    rx, _ = _compile(lines)
    assert (rx.match("第三卷 第1章") is not None) is included


def test_special_chapters_still_included_when_narrowed():
    """特章族维持无条件并入（现状锁死）：窄化激活时序章/番外仍可识别"""
    lines = []
    for i in range(1, 9):
        lines.append(f"第{i}章 标题{i}")
        lines.append("正文内容。" * 30)
    lines += ["序章 楔子", "番外 一场大梦"]
    rx, name = _compile(lines)
    assert name == "第X章(中文)"
    assert rx.match("序章 楔子") is not None
    assert rx.match("番外 一场大梦") is not None
