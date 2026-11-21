"""T1：B 类遗漏追踪与归因器测试。

验证：
1. 统一追踪记录字段齐全，B1/B2/B3/B4 与 UNLOCATED 显式统计、不静默归零；
2. 关闭追踪开关（trace=None）时，去重、总表构建、复检 V2 业务结果零变化；
3. 开启追踪开关时，能完整记录过滤、去重合并、截断、缺锚点、提名章漏检、判定失败等各阶段原因码；
4. 原文参考观察集（登记项 + 回答项）归因可定位率 >= 90%。
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict
import json
import re
from types import SimpleNamespace

from backend.config.constants import FORESHADOW_CATEGORY_FALLBACK

from backend.common.b_trace import (
    CAT_B1_UNREGISTERED,
    CAT_B2_EVIDENCE_MISSED,
    CAT_B3_JUDGMENT_MISSED,
    CAT_B4_FLOW_LOST,
    CAT_KEPT,
    CAT_UNLOCATED,
    BTraceCollector,
    attribute_reference_observations,
)
from backend.services.final_summary import FinalSummaryRunner
from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem
from backend.utils.text_utils import deduplicate_foreshadows


def test_trace_collector_counts_unlocated_explicitly():
    trace = BTraceCollector(run_id="t1_unit", source_sha256="abc123")
    trace.record(
        item_id="fs_1",
        stage="catalog_truncate",
        state_before="dedup",
        state_after="dropped",
        reason_code="HIGH_CAP_TRUNCATED",
    )
    trace.record(
        item_id="fs_2",
        stage="unknown_stage",
        state_before="x",
        state_after="y",
        reason_code="MYSTERY_REASON_NOT_IN_TABLE",
    )
    summary = trace.summary()
    assert summary["total_loss_events"] == 2
    assert summary["located_loss_events"] == 1
    assert summary["unlocated_loss_events"] == 1
    assert summary["loss_by_category"][CAT_B4_FLOW_LOST] == 1
    assert summary["loss_by_category"][CAT_UNLOCATED] == 1


def test_deduplicate_foreshadows_zero_business_diff_with_trace():
    clues = [
        (
            1,
            "铜匣底下那层是空的而且刻着古怪的云纹图案",
            "剧情伏笔",
            "高",
            "高",
            "",
            0,
            "铜匣云纹",
            "",
            "",
        ),
        (
            5,
            "铜匣底下那层是空的而且刻着古怪的云纹图案再次浮现",
            "剧情伏笔",
            "高",
            "高",
            "铜匣底下那层是空的",
            5,
            "铜匣云纹",
            "铜匣底下的云纹代表什么？",
            "揭晓云纹来历",
        ),
    ]
    out_off = deduplicate_foreshadows(copy.deepcopy(clues), similarity_threshold=0.5, trace=None)
    trace = BTraceCollector(run_id="dedup_test", source_sha256="s1")
    out_on = deduplicate_foreshadows(copy.deepcopy(clues), similarity_threshold=0.5, trace=trace)

    assert out_off == out_on, "开启追踪不得改变去重返回结果"
    reasons = [r.reason_code for r in trace.records]
    assert "DEDUP_REPRESENTATIVE_KEPT" in reasons
    assert "DEDUP_MERGED_INTO_GROUP" in reasons
    assert "DEDUP_ANCHOR_LOST_ON_MERGE" in reasons
    assert "DEDUP_QUESTION_LOST_ON_MERGE" in reasons


def test_build_catalog_zero_business_diff_and_traces_truncation():
    dummy = SimpleNamespace()
    dummy.config = SimpleNamespace(
        analysis=SimpleNamespace(
            foreshadow_min_importance="中",
            foreshadow_min_confidence="中",
            foreshadow_kept_categories=["剧情伏笔", "身份伏笔", "因果伏笔", FORESHADOW_CATEGORY_FALLBACK],
            max_foreshadow_catalog_high=1,
            max_foreshadow_catalog_mid=1,
        )
    )
    dummy._load_foreshadow_type_map = lambda: {}
    dummy._imp_at_least = FinalSummaryRunner._imp_at_least

    results = [
        {
            "ch": 1,
            "foreshadowing_raw": [
                {"clue": "太短", "type": "剧情伏笔", "importance": "高", "confidence": "高"},
                {"clue": "这是一条重要性为低的伏笔线索超过十五个字", "type": "剧情伏笔", "importance": "低", "confidence": "高"},
                {"clue": "北境长城地底埋藏着前朝遗留的玄铁重剑与兵符", "type": "剧情伏笔", "importance": "高", "confidence": "高"},
                {"clue": "南疆苗寨巫女手中握着失传百年的九黎圣蛊秘卷", "type": "剧情伏笔", "importance": "高", "confidence": "高"},
            ],
        }
    ]
    out_off = asyncio.run(FinalSummaryRunner._build_foreshadow_catalog(dummy, copy.deepcopy(results), trace=None))
    trace = BTraceCollector(run_id="cat_test", source_sha256="s2")
    out_on = asyncio.run(FinalSummaryRunner._build_foreshadow_catalog(dummy, copy.deepcopy(results), trace=trace))

    assert out_off == out_on, "开启追踪不得改变总表构建结果"
    reasons = {r.reason_code for r in trace.records}
    assert "SHORT_CLUE_DROPPED" in reasons
    assert "IMPORTANCE_FILTERED" in reasons
    assert "HIGH_CAP_TRUNCATED" in reasons
    assert "CATALOG_KEPT" in reasons


def test_recheck_v2_zero_business_diff_and_traces_losses():
    chapters = {
        1: "第一章\n方醒打开铜匣，铜匣底下那层是空的。\n",
        5: "第五章\n密室里终于找到了当年失踪的暗格机关图纸。\n",
    }
    items_off = [
        ForeshadowItem(
            id="fs_ok",
            description="铜匣夹层下落",
            first_seen_chapter=1,
            first_seen_batch=0,
            last_seen_chapter=1,
            last_seen_batch=0,
            anchor_quote="铜匣底下那层是空的。",
            anchor_chapter=1,
            resolved_chapter=5,
        ),
        ForeshadowItem(
            id="fs_no_anchor",
            description="缺锚点伏笔",
            first_seen_chapter=1,
            first_seen_batch=0,
            last_seen_chapter=1,
            last_seen_batch=0,
            anchor_quote="",
            anchor_chapter=0,
        ),
    ]
    items_on = copy.deepcopy(items_off)

    async def call_model(prompt: str) -> str:
        m = re.search(r"\[evidence_id=([0-9a-f]{12})", prompt)
        eid = m.group(1) if m else ""
        return json.dumps(
            [
                {
                    "candidate_id": "fs_ok",
                    "verdict": "INSUFFICIENT",
                    "answer": "",
                    "evidence_id": eid,
                    "supporting_quote": "",
                    "counter_search": {"performed": True, "checked_evidence_ids": [eid], "counter_quotes": []},
                }
            ],
            ensure_ascii=False,
        )

    res_off = asyncio.run(run_recheck_v2(items_off, chapters=chapters, call_model=call_model, trace=None))
    trace = BTraceCollector(run_id="rv_test", source_sha256="s3")
    res_on = asyncio.run(run_recheck_v2(items_on, chapters=chapters, call_model=call_model, trace=trace))

    assert res_off.summary() == res_on.summary()
    assert [asdict(it) for it in items_off] == [asdict(it) for it in items_on]
    reasons = {r.reason_code for r in trace.records}
    assert "DROPPED_NO_ANCHOR" in reasons
    assert "NOMINATED_CHAPTER_NOT_IN_BUNDLE" in reasons
    assert "VERDICT_INSUFFICIENT_NO_EVIDENCE" in reasons


def test_attribute_reference_observations_covers_b1_b2_b3_b4():
    observations = [
        {
            "obs_id": "obs_reg_hit",
            "obs_type": "registration",
            "chapter": 1,
            "anchor_quote": "铜匣底下那层是空的",
            "keywords": ["铜匣", "空的"],
        },
        {
            "obs_id": "obs_b1",
            "obs_type": "registration",
            "chapter": 2,
            "anchor_quote": "黑衣人在井底刻下血字",
            "keywords": ["井底", "血字"],
        },
        {
            "obs_id": "obs_b4_cap",
            "obs_type": "registration",
            "chapter": 800,
            "anchor_quote": "青铜巨门后的第七枚骨片",
            "keywords": ["青铜巨门", "骨片"],
        },
        {
            "obs_id": "obs_b2_miss",
            "obs_type": "resolution",
            "chapter": 1,
            "resolution_chapter": 5,
            "anchor_quote": "玉佩跟丹药都带出来了",
            "keywords": ["玉佩", "带出来"],
        },
        {
            "obs_id": "obs_b3_miss",
            "obs_type": "resolution",
            "chapter": 10,
            "resolution_chapter": 20,
            "anchor_quote": "古镜背面的铭文正是长生诀",
            "keywords": ["古镜", "长生诀"],
        },
    ]
    raw_clues = [
        {"id": "r1", "chapter": 1, "clue": "铜匣底下那层是空的"},
        {"id": "r2", "chapter": 800, "clue": "青铜巨门后的第七枚骨片"},
        {"id": "r3", "chapter": 1, "clue": "玉佩跟丹药都带出来了"},
        {"id": "r4", "chapter": 10, "clue": "古镜背面的铭文正是长生诀"},
    ]
    catalog_items = [
        {"id": "fs_001", "clue": "铜匣底下那层是空的"},
        {"id": "fs_800", "clue": "青铜巨门后的第七枚骨片"},
        {"id": "fs_002", "clue": "玉佩跟丹药都带出来了"},
        {"id": "fs_010", "clue": "古镜背面的铭文正是长生诀"},
    ]
    ledger_items = [
        {"id": "fs_001", "description": "铜匣底下那层是空的", "status": "active", "anchor_quote": "铜匣底下那层是空的", "anchor_chapter": 1},
        {"id": "fs_002", "description": "玉佩跟丹药都带出来了", "status": "active", "anchor_quote": "玉佩", "anchor_chapter": 1, "resolved_chapter": 5},
        {"id": "fs_010", "description": "古镜背面的铭文正是长生诀", "status": "active", "anchor_quote": "古镜", "anchor_chapter": 10},
    ]
    recheck_bundles = {
        "fs_002": [1, 3, 6],   # 漏掉第 5 章 -> B2
        "fs_010": [10, 20],    # 含第 20 章但判 INSUFFICIENT -> B3
    }
    recheck_verdicts = {
        "fs_002": {"verdict": "INSUFFICIENT", "reason": ""},
        "fs_010": {"verdict": "INSUFFICIENT", "reason": ""},
    }

    report = attribute_reference_observations(
        observations,
        raw_clues=raw_clues,
        catalog_items=catalog_items,
        ledger_items=ledger_items,
        recheck_bundles=recheck_bundles,
        recheck_verdicts=recheck_verdicts,
    )
    assert report["total_observations"] == 5
    assert report["hits"] == 1
    assert report["omissions"] == 4
    assert report["unlocated_omissions"] == 0
    assert report["locatable_rate"] == 1.0
    by_cat = report["by_category"]
    assert by_cat[CAT_KEPT] == 1
    assert by_cat[CAT_B1_UNREGISTERED] == 1
    assert by_cat[CAT_B4_FLOW_LOST] == 1
    assert by_cat[CAT_B2_EVIDENCE_MISSED] == 1
    assert by_cat[CAT_B3_JUDGMENT_MISSED] == 1


def test_b_trace_summary_separates_event_coverage_and_reference_attribution():
    """Regression for Issue #5: empty loss events must not report 1.0, and unmatched reference
    omissions must be counted explicitly in reference_omissions_unmatched.
    """
    empty_trace = BTraceCollector(run_id="empty_run")
    s_empty = empty_trace.summary()
    assert s_empty["event_reason_coverage_rate"] is None
    assert s_empty["reference_attribution_rate"] is None
    assert s_empty["reference_attribution_status"] == "N/A"
    assert s_empty["locatable_rate"] is None

    trace = BTraceCollector(run_id="ref_run")
    trace.record(
        item_id="fs_1",
        stage="catalog_build",
        state_before="candidate",
        state_after="dropped",
        reason_code="HIGH_CAP_TRUNCATED",
        reference_obs_id="obs_1",
    )
    s_ref = trace.summary(
        reference_omissions=[
            {"obs_id": "obs_1"},
            {"obs_id": "obs_unmatched_mystery"},
        ]
    )
    assert s_ref["event_reason_coverage_rate"] == 1.0
    assert s_ref["reference_omissions_total"] == 2
    assert s_ref["reference_omissions_attributed"] == 1
    assert s_ref["reference_omissions_unmatched"] == 1
    assert s_ref["reference_attribution_rate"] == 0.5
    assert s_ref["locatable_rate"] == 0.5

