"""阶段 0-3：候选块范围定位。

锁住 AGY 复审（plan-stage0-3-v2-review）的 3 条 P1：

- **P1-1** 旧账本不得全量走全书兜底 —— 定位起始块取 ``first_seen_chapter``（它本来就存着
  物理文件序号），旧账本无缝享受 100% 块范围命中率；
- **P1-2** 块内多处命中必须有裁决规则，且**严禁跳出块范围去兜底**（跳出去就重演跨卷截胡）；
- **P1-3** 定位诊断要有载体 —— ``locate_records`` 落在明细**顶层**，掉队条目也有记录。
"""

from __future__ import annotations

import json
import re

import pytest

from backend.core.text_units import LocateHit, locate_with_mode
from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem

EVIDENCE_RE = re.compile(r"evidence_id=([0-9a-f]{12})")

QUOTE = "方醒打开铜匣，里面只有一张旧契。"


def _units() -> dict[int, str]:
    return {
        10: "第十回 甲\n无关内容。",
        12: "第十二回 乙\n方醒打开铜匣，里面只有一张旧契。",
        13: "第十三回 丙\n另一处无关内容。",
    }


def _item(**kw) -> ForeshadowItem:
    base = dict(
        id="fs_1", description="旧契", first_seen_chapter=12, first_seen_batch=0,
        last_seen_chapter=12, last_seen_batch=0, status="active",
        anchor_quote=QUOTE, anchor_chapter=12,
    )
    base.update(kw)
    return ForeshadowItem(**base)


async def _model(prompt: str) -> str:
    eid = EVIDENCE_RE.search(prompt).group(1)
    return json.dumps([{
        "candidate_id": "fs_1", "verdict": "INSUFFICIENT", "answer": "",
        "material_insufficient_reason": "证据不足", "evidence_id": eid,
        "supporting_span": [0, 12], "supporting_quote": "方醒打开铜匣",
        "counter_search": {"performed": True, "checked_evidence_ids": [eid],
                           "counter_quotes": []},
    }], ensure_ascii=False)


# ---------------------------------------------------------------- 范围定位

def test_range_hit_is_used_when_inside_the_candidate_block():
    hit = locate_with_mode(_units(), QUOTE, candidate_range=(12, 15))

    assert hit.mode == "range"
    assert hit.unit == 12


def test_out_of_range_falls_back_to_earliest_and_is_flagged():
    """范围外才走兜底——兜底就是跨卷错挂的高风险路径，必须能被识别出来。"""
    hit = locate_with_mode(_units(), QUOTE, candidate_range=(500, 503))

    assert hit.mode == "fallback"
    assert hit.unit == 12


def test_absent_when_the_quote_is_nowhere():
    hit = locate_with_mode(_units(), "根本不存在的引文", candidate_range=(1, 5))

    assert hit.mode == "absent"
    assert hit.unit is None


def test_multi_hit_inside_range_never_leaves_the_range():
    """块内多处命中：优先匹配回号；无法消歧取块内首命中——**绝不跳出范围**。"""
    units = {
        20: "第二十回 甲\n同一句话。",
        21: "第二十一回 乙\n同一句话。",
        99: "第九十九回 丙\n同一句话。",
    }
    hit = locate_with_mode(units, "同一句话。", candidate_range=(20, 23))

    assert hit.mode == "range"
    assert hit.unit in (20, 21), "必须落在候选范围内"
    assert hit.unit != 99


def test_multi_hit_prefers_the_matching_round():
    """回号匹配的优先：anchor_round=21 时选 21。"""
    units = {
        20: "第二十回 甲\n同一句话。",
        21: "第二十一回 乙\n同一句话。",
    }
    hit = locate_with_mode(
        units, "同一句话。", candidate_range=(20, 23),
        display_labels={20: "第 20 回", 21: "第 21 回"}, anchor_round=21,
    )

    assert hit.unit == 21


def test_locate_hit_keeps_the_two_item_unpacking_contract():
    """既有调用方 `owner, _offset = hit` 不能炸。"""
    hit = LocateHit(12, 34, "range")

    owner, offset = hit
    assert (owner, offset) == (12, 34)
    assert hit.mode == "range"


# ---------------------------------------------------------------- 复检集成

@pytest.mark.asyncio
async def test_old_ledger_without_source_field_still_uses_the_block_range():
    """P1-1：旧账本没有新字段，但 ``first_seen_chapter`` 本来就是物理序号，
    所以旧账本**不得**全量走兜底（那会让跨卷错挂全量复活）。"""
    item = _item()  # 没有 source_chapter 之类的额外字段
    outcome = await run_recheck_v2(
        [item], chapters=_units(), call_model=_model, max_calls=2,
        unit_noun="文件", display_labels={12: "第 12 回"}, block_size=4,
    )

    record = outcome.locate_records["fs_1"]
    assert record["mode"] == "range", "旧账本也必须走块范围命中"
    assert record["low_confidence"] is False
    assert record["source_chapter"] == 12


@pytest.mark.asyncio
async def test_locate_records_are_exported_at_the_top_level_of_the_detail():
    """P1-3：掉队条目没有 verdict 实例，定位诊断必须在明细顶层。"""
    item = _item()
    outcome = await run_recheck_v2(
        [item], chapters=_units(), call_model=_model, max_calls=2,
        unit_noun="文件", display_labels={12: "第 12 回"}, block_size=4,
    )

    detail = outcome.detail()
    assert "locate_records" in detail
    assert detail["locate_records"]["fs_1"]["unit"] == 12
    # summary() 不能带明细（它会进进度事件）
    assert "locate_records" not in outcome.summary()
