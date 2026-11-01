"""阶段 0-2：正文身份落到文件级。

锁住 AGY 复审的三条 P1：

- **P1-1** 全书定位**绝不能**触发 ``difflib``（``locate_quote`` 找不到时会回退
  ``citations.verify_citation`` 的 ``SequenceMatcher``，在 435 万字符上是 $O(N\\cdot M)$，
  单次就能卡住几分钟）；
- **P1-2** 账本的 ``resolved_chapter`` 必须仍是**文学章号**，不能被写成文件序号；
- **P1-3** 提示词里的证据标签必须区分"文件N"与"第N回"。
"""

from __future__ import annotations

import json
import re

import pytest

from backend.core import citations
from backend.core.text_units import (
    build_display_labels,
    find_literal,
    load_blocks_as_files,
    locate_in_units,
    unit_label,
)
from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem

EVIDENCE_RE = re.compile(r"evidence_id=([0-9a-f]{12})")


# ---------------------------------------------------------------- 文本单元

def test_find_literal_returns_none_when_missing():
    """`str.find` 没找到返回 -1，**不是** None——不转的话会把"没找到"当成命中。

    这个 bug 曾让 `locate_in_units` 永远返回第一个单元。
    """
    assert find_literal("abc", "xyz") is None
    assert find_literal("abc", "") is None
    assert find_literal("", "abc") is None
    assert find_literal("abc", "b") == 1


def test_locate_in_units_prefers_the_hint_unit():
    units = {1: "第一回\n甲段内容。", 2: "第二回\n乙段内容。"}
    assert locate_in_units(units, "乙段内容。", prefer=2) == (2, 4)


def test_locate_in_units_takes_the_earliest_occurrence():
    """伏笔埋在前面：同一句引文在多处出现时取最早。"""
    units = {1: "甲\n同一句话。", 2: "乙\n别的。", 3: "丙\n同一句话。"}
    unit, offset = locate_in_units(units, "同一句话。")
    assert unit == 1
    assert offset == 2


def test_locate_in_units_returns_none_when_absent():
    assert locate_in_units({1: "甲"}, "根本不在这句话") is None


def test_load_blocks_as_files_uses_natural_order_and_excludes_non_text(tmp_path):
    for name in ("1.txt", "2.txt", "10.txt", "split_report.txt", "notes.md"):
        (tmp_path / name).write_text("内容", encoding="utf-8")
    units, excluded = load_blocks_as_files(tmp_path)

    assert sorted(units) == [1, 2, 10], "键是文件名里的数字，不是排序后的序号"
    assert excluded == ["split_report.txt"], "非正文文件必须被排除并报告"
    assert units[10] == "内容"


def test_display_labels_parse_the_round_number_from_the_file():
    units = {1: "第十二回 起\n正文。", 2: "没有标题的正文。"}
    labels = build_display_labels(units)

    assert labels[1] == "第 12 回"
    assert 2 not in labels


def test_unit_label_shows_both_numbering_systems():
    display = {366: "第 12 回"}
    assert unit_label(366, display, noun="文件") == "文件366（第 12 回）"
    assert unit_label(366, None, noun="文件") == "文件366"
    assert unit_label(12, None, noun="章") == "第 12 章"


# ---------------------------------------------------------------- P1-1

@pytest.mark.asyncio
async def test_book_wide_search_never_touches_difflib(monkeypatch):
    """全书定位不许碰 difflib——把它换成会炸的实现，定位仍要成功。

    `locate_quote` 在逐字找不到时会回退 `citations.verify_citation`，
    里面是 `difflib.SequenceMatcher`；拿 435 万字符去跑，单次就能卡死几分钟。
    """
    def _boom(*_args, **_kwargs):
        raise AssertionError("全书定位触发了 difflib 回退（会卡死整轮复检）")

    monkeypatch.setattr(citations, "verify_citation", _boom)

    units = {1: "第一回\n无关内容。", 2: "第二回\n方醒打开铜匣，里面只有一张旧契。"}
    item = ForeshadowItem(
        id="fs_1", description="旧契", first_seen_chapter=1, first_seen_batch=0,
        last_seen_chapter=1, last_seen_batch=0, status="active",
        anchor_quote="方醒打开铜匣，里面只有一张旧契。", anchor_chapter=1,
    )

    async def _model(prompt: str) -> str:
        eid = EVIDENCE_RE.search(prompt).group(1)
        return json.dumps([{
            "candidate_id": "fs_1", "verdict": "SUPPORTED", "answer": "只有旧契",
            "material_insufficient_reason": "", "evidence_id": eid,
            "supporting_span": [0, 12], "supporting_quote": "方醒打开铜匣",
            "counter_search": {"performed": True, "checked_evidence_ids": [eid],
                               "counter_quotes": []},
        }], ensure_ascii=False)

    outcome = await run_recheck_v2(
        [item], chapters=units, call_model=_model, max_calls=2, unit_noun="文件",
    )

    assert outcome.dropped_anchor_missing == 0
    assert outcome.coverage["candidates_total"] == 1


# ---------------------------------------------------------------- P1-2

@pytest.mark.asyncio
async def test_resolved_chapter_keeps_the_novel_chapter_number_not_the_file_index():
    """账本里的 resolved_chapter 必须是**回号**，不能是文件序号。

    写错的后果：'在第几章回收'整体错位，休眠跨度计算也跟着偏。
    """
    units = {1: "第一回 甲\n无关内容。", 366: "第十二回 乙\n方醒打开铜匣，里面只有一张旧契。"}
    item = ForeshadowItem(
        id="fs_1", description="旧契", first_seen_chapter=12, first_seen_batch=0,
        last_seen_chapter=12, last_seen_batch=0, status="active",
        anchor_quote="方醒打开铜匣，里面只有一张旧契。", anchor_chapter=12,
    )

    async def _model(prompt: str) -> str:
        eid = EVIDENCE_RE.search(prompt).group(1)
        return json.dumps([{
            "candidate_id": "fs_1", "verdict": "SUPPORTED", "answer": "只有旧契",
            "material_insufficient_reason": "", "evidence_id": eid,
            "supporting_span": [0, 12], "supporting_quote": "方醒打开铜匣",
            "counter_search": {"performed": True, "checked_evidence_ids": [eid],
                               "counter_quotes": []},
        }], ensure_ascii=False)

    outcome = await run_recheck_v2(
        [item], chapters=units, call_model=_model, max_calls=2,
        unit_noun="文件", display_labels={366: "第 12 回"},
    )

    assert item.status == "resolved"
    assert item.resolved_chapter == 12, "必须是回号 12，不能是文件序号 366"
    assert outcome.resolved_units.get("fs_1") == 366, "文件序号记在 resolved_units"


# ---------------------------------------------------------------- P1-3

@pytest.mark.asyncio
async def test_evidence_label_shows_file_and_round_number():
    """证据标签必须同时给出两个编号体系，模型才不会以为证据错位。"""
    units = {366: "第十二回 乙\n方醒打开铜匣，里面只有一张旧契。"}
    item = ForeshadowItem(
        id="fs_1", description="旧契", first_seen_chapter=12, first_seen_batch=0,
        last_seen_chapter=12, last_seen_batch=0, status="active",
        anchor_quote="方醒打开铜匣，里面只有一张旧契。", anchor_chapter=366,
    )
    seen: dict[str, str] = {}

    async def _model(prompt: str) -> str:
        seen["prompt"] = prompt
        eid = EVIDENCE_RE.search(prompt).group(1)
        return json.dumps([{
            "candidate_id": "fs_1", "verdict": "INSUFFICIENT", "answer": "",
            "material_insufficient_reason": "证据不足", "evidence_id": eid,
            "supporting_span": [0, 12], "supporting_quote": "方醒打开铜匣",
            "counter_search": {"performed": True, "checked_evidence_ids": [eid],
                               "counter_quotes": []},
        }], ensure_ascii=False)

    await run_recheck_v2(
        [item], chapters=units, call_model=_model, max_calls=2,
        unit_noun="文件", display_labels={366: "第 12 回"},
    )

    prompt = seen["prompt"]
    assert "文件366（第 12 回）" in prompt, "证据标签必须同时给出文件序号与回号"
    assert "第366章" not in prompt, "不能把文件序号伪装成章号"


@pytest.mark.asyncio
async def test_legacy_chapter_labels_are_unchanged():
    """1.x 口径不变：标签仍是「第12章」，提示词里也不插新说明。"""
    units = {12: "第十二回\n方醒打开铜匣，里面只有一张旧契。"}
    item = ForeshadowItem(
        id="fs_1", description="旧契", first_seen_chapter=12, first_seen_batch=0,
        last_seen_chapter=12, last_seen_batch=0, status="active",
        anchor_quote="方醒打开铜匣，里面只有一张旧契。", anchor_chapter=12,
    )
    seen: dict[str, str] = {}

    async def _model(prompt: str) -> str:
        seen["prompt"] = prompt
        eid = EVIDENCE_RE.search(prompt).group(1)
        return json.dumps([{
            "candidate_id": "fs_1", "verdict": "INSUFFICIENT", "answer": "",
            "material_insufficient_reason": "证据不足", "evidence_id": eid,
            "supporting_span": [0, 12], "supporting_quote": "方醒打开铜匣",
            "counter_search": {"performed": True, "checked_evidence_ids": [eid],
                               "counter_quotes": []},
        }], ensure_ascii=False)

    await run_recheck_v2([item], chapters=units, call_model=_model, max_calls=2)

    prompt = seen["prompt"]
    assert "第12章" in prompt
    assert "文件12" not in prompt
    assert "不是同一套编号" not in prompt
