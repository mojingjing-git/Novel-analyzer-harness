"""Unit and regression tests for T3 Class B omission repairs (Fix 1: B4 flow/storage, Fix 2: B2 evidence/recheck)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.common.b_trace import BTraceCollector
from backend.config.constants import (
    FORESHADOW_CATEGORY_FALLBACK,
    MAX_VERIFIED_FACTS,
    MAX_WORLD_BUILDING,
)
from backend.core.anchor import build_chapter_spans, verify_result_anchors
from backend.core.evidence import EvidenceIndex
from backend.core.knowledge_base import KnowledgeBaseManager
from backend.models.knowledge import KnowledgeBase
from backend.services.final_summary import FinalSummaryRunner
from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem
from backend.utils.text_utils import deduplicate_foreshadows


def test_b4_dedup_preserve_merged_metadata():
    clues = [
        # First appearance lacks anchor_quote and question, shares motif_key + keyword
        (10, "李火旺发现苍蜣登阶仪式需要黑太岁作为引子", "核心悬念", "高", "高", "", 0, "苍蜣登阶", "", ""),
        # Second appearance has anchor_quote, anchor_chapter=14, question, answer_criteria
        (
            16,
            "李火旺再次调查苍蜣登阶仪式的代价与真相",
            "核心悬念",
            "高",
            "高",
            "苍蜣登阶必以黑太岁入药",
            14,
            "苍蜣登阶",
            "苍蜣登阶的真正代价是什么？",
            "揭示苍蜣登阶对心素身体的改造后果",
        ),
    ]
    out_legacy = deduplicate_foreshadows(clues, preserve_merged_metadata=False)
    assert len(out_legacy) == 2  # SequenceMatcher < 0.6 did not merge

    out_fixed = deduplicate_foreshadows(clues, preserve_merged_metadata=True)
    assert len(out_fixed) == 1
    item = out_fixed[0]
    assert item["merged_count"] == 2
    assert item["anchor_quote"] == "苍蜣登阶必以黑太岁入药"
    assert item["anchor_chapter"] == 14
    assert item["question"] == "苍蜣登阶的真正代价是什么？"
    assert item["answer_criteria"] == "揭示苍蜣登阶对心素身体的改造后果"
    assert item["evidence_chapters"] == [10, 14, 16]


@pytest.mark.asyncio
async def test_b4_stratified_catalog_prevents_tail_chopping():
    runner = FinalSummaryRunner.__new__(FinalSummaryRunner)
    runner.config = SimpleNamespace(
        analysis=SimpleNamespace(
            foreshadow_min_importance="中",
            foreshadow_min_confidence="中",
            foreshadow_kept_categories=[FORESHADOW_CATEGORY_FALLBACK],
            max_foreshadow_catalog_high=10,
            max_foreshadow_catalog_mid=5,
            fix_b4_flow_and_storage=False,
        )
    )
    runner._load_foreshadow_type_map = MagicMock(return_value={})
    runner._imp_at_least = FinalSummaryRunner._imp_at_least

    # 30 distinct clues across chapters 1..300 (all score 331 when single-hit)
    distinct_nouns = [
        "青铜鼎炉沉入深渊古井底部", "玄铁重剑插在荒漠断崖石壁", "白玉龙佩藏于皇宫御书房暗格",
        "紫金凤冠遗落在塞外古战场遗址", "朱雀火羽封印在火山熔岩密室", "玄武龟甲浮现在北海冰川裂缝",
        "苍龙断角悬挂在宗门试炼塔顶层", "白虎战牙埋在西南十万大山祭坛", "昆仑古镜映出千年前王朝覆灭景象",
        "崆峒金印镇压着海底龙宫灵脉泉眼", "东皇铜钟在满月之夜独自鸣响三声", "轩辕神剑被锈迹包裹无人能够拔出",
        "盘古巨斧劈开虚空留下永恒裂痕", "炼妖陶壶内部传出远古凶兽咆哮", "昊天宝塔第九层关押着神秘老者",
        "伏羲古琴断弦之后依然自动奏曲", "神农百草鼎熬制出罕见九转金丹", "女娲补天石散发出五彩斑斓霞光",
        "九州山河鼎记载着失传已久的阵法", "山河社稷图卷轴内自成一方小世界", "太极阴阳图在石壁上缓缓旋转不息",
        "诛仙古阵残图出现在拍卖会压轴席", "戮仙断剑在雷雨夜吸收天雷之力", "陷仙古剑令靠近者陷入幻境迷失",
        "绝仙残剑被盲眼老仆随身背负多年", "混元珍珠伞撑开后遮蔽漫天星辰", "定海神珠在渔村少年怀中微微发热",
        "打神金鞭悬挂在破败道观三清像前", "封神残榜上浮现出模糊不清的名字", "乾坤金圈在古墓石棺表面留下凹痕",
    ]
    results = []
    for i in range(1, 31):
        ch = i * 10
        clue_text = distinct_nouns[i - 1]
        results.append(
            {
                "ch": ch,
                "foreshadowing_raw": [
                    {
                        "clue": f"【卷{i}】{clue_text}",
                        "type": FORESHADOW_CATEGORY_FALLBACK,
                        "confidence": "高",
                        "importance": "高",
                        "anchor_quote": clue_text,
                        "anchor_chapter": ch,
                        "motif_key": f"motif_{i}",
                        "question": f"为何{clue_text}？",
                        "answer_criteria": f"解释{clue_text}的原因",
                    }
                ],
            }
        )

    cat_legacy = await runner._build_foreshadow_catalog(results, preserve_full_catalog=False)
    assert len(cat_legacy) == 10
    assert max(c["first_seen"] for c in cat_legacy) == 100  # Tail after ch 100 completely chopped!

    cat_fixed = await runner._build_foreshadow_catalog(results, preserve_full_catalog=True)
    assert len(cat_fixed) == 10
    assert len(runner._last_full_catalog) == 30  # Full catalog preserved
    assert max(c["first_seen"] for c in cat_fixed) >= 270  # Late-book chapters represented!


def test_b4_trim_kb_preserves_archive(tmp_path):
    kb = KnowledgeBase()
    kb.verified_facts = [f"fact_{i}" for i in range(MAX_VERIFIED_FACTS + 50)]
    kb.world_building = [f"wb_{i}" for i in range(MAX_WORLD_BUILDING + 1)]

    KnowledgeBaseManager._trim_kb(kb, preserve_archive=True)
    assert len(kb.verified_facts) == MAX_VERIFIED_FACTS
    assert len(kb.world_building) == MAX_WORLD_BUILDING
    assert len(kb._archive["verified_facts"]) == MAX_VERIFIED_FACTS + 50
    assert len(kb._archive["world_building"]) == MAX_WORLD_BUILDING + 1
    assert kb._trim_stats["verified_facts"] == {
        "total": MAX_VERIFIED_FACTS + 50,
        "kept": MAX_VERIFIED_FACTS,
        "dropped": 50,
    }

    # Regression for Issue #4: serialization and save/load roundtrip must preserve _archive (all 201 items)
    reloaded = KnowledgeBase.from_dict(kb.to_dict())
    assert len(reloaded.world_building) == MAX_WORLD_BUILDING
    assert len(reloaded._archive["world_building"]) == MAX_WORLD_BUILDING + 1
    assert len(reloaded.get_full_field("world_building")) == MAX_WORLD_BUILDING + 1
    assert reloaded._trim_stats["world_building"]["total"] == MAX_WORLD_BUILDING + 1

    mgr = KnowledgeBaseManager(tmp_path / "knowledge.json")
    mgr.save(reloaded)
    disk_kb = mgr.load()
    assert len(disk_kb.world_building) == MAX_WORLD_BUILDING
    assert len(disk_kb._archive["world_building"]) == MAX_WORLD_BUILDING + 1
    assert len(disk_kb.get_full_field("world_building")) == MAX_WORLD_BUILDING + 1

    # Re-trimming a reloaded KB must not lose the archived 201st item
    KnowledgeBaseManager._trim_kb(disk_kb, preserve_archive=True)
    assert len(disk_kb._archive["world_building"]) == MAX_WORLD_BUILDING + 1

    # Regression for F1: appending 2+ items after reload makes len(cur_list) == 202 > len(old_list) == 201;
    # re-trimming must merge with old_list and retain all 203 items including wb_0!
    disk_kb.world_building.extend(["wb_201", "wb_202"])
    assert len(disk_kb.get_full_field("world_building")) == MAX_WORLD_BUILDING + 3
    KnowledgeBaseManager._trim_kb(disk_kb, preserve_archive=True)
    assert len(disk_kb.world_building) == MAX_WORLD_BUILDING
    assert len(disk_kb._archive["world_building"]) == MAX_WORLD_BUILDING + 3
    assert disk_kb._archive["world_building"][0] == "wb_0"
    assert disk_kb._archive["world_building"][-2:] == ["wb_201", "wb_202"]
    assert disk_kb._trim_stats["world_building"] == {
        "total": MAX_WORLD_BUILDING + 3,
        "kept": MAX_WORLD_BUILDING,
        "dropped": 3,
    }

    # Multi-round save/load + batch append (3 more items -> 206 total)
    mgr.save(disk_kb)
    round2_kb = mgr.load()
    round2_kb.world_building.extend(["wb_203", "wb_204", "wb_205"])
    KnowledgeBaseManager._trim_kb(round2_kb, preserve_archive=True)
    assert len(round2_kb._archive["world_building"]) == MAX_WORLD_BUILDING + 6
    assert round2_kb._archive["world_building"][0] == "wb_0"
    assert round2_kb._archive["world_building"][-1] == "wb_205"


def test_b4_anchor_expected_range_filters_false_heading():
    block_text = (
        "第八百三十一章 旧档重开\n"
        "他翻开那本泛黄的笔记，只见扉页写着：\n"
        "第一章 创世之初\n"
        "那枚青铜古印静静躺在祭坛中央。\n"
        "第八百三十二章 祭坛异变\n"
        "古印忽然发出刺目的红光。\n"
    )
    # Without expected_range, the false in-body "第一章 创世之初" corrupts span to chapter 1
    spans_legacy = build_chapter_spans(block_text, 831)
    assert any(ch == 1 for ch, _s, _e in spans_legacy)

    # With expected_range=(831, 834), the false heading is ignored
    spans_fixed = build_chapter_spans(block_text, 831, expected_range=(831, 834))
    assert [ch for ch, _s, _e in spans_fixed] == [831, 832]

    res = SimpleNamespace(
        foreshadowing=[
            SimpleNamespace(anchor_quote="那枚青铜古印静静躺在祭坛中央。", anchor_chapter=0)
        ]
    )
    verify_result_anchors(res, block_text, 831, expected_range=(831, 834))
    assert res.foreshadowing[0].anchor_chapter == 831


def test_b2_bundle_for_nominated_chapters_and_density_window():
    chapters = {
        1: "【第1章】他把一枚玄铁令牌藏进石匣。",
        50: "【第50章】众人在客栈里随口聊起江湖传闻。" + ("无关闲聊内容。" * 80) + "有人提到玄铁令牌现世，而且石匣已经被打开，真相大白。",
        88: "【第88章】城主终于亮出那枚信物，揭开了二十年前的血案真相。",
    }
    index = EvidenceIndex(chapters)
    # Without nominated_chapters, chapter 88 has no lexical overlap with "玄铁令牌/石匣" and is missed
    b_legacy = index.bundle_for(
        anchor_chapter=1,
        anchor_quote="他把一枚玄铁令牌藏进石匣。",
        hint_texts=["玄铁令牌的下落是什么？"],
        radius=40,
    )
    assert 88 not in b_legacy.chapters

    # With nominated_chapters=[88] and density_window=True, chapter 88 is included and ch 50 window centers on the dense cluster
    b_fixed = index.bundle_for(
        anchor_chapter=1,
        anchor_quote="他把一枚玄铁令牌藏进石匣。",
        hint_texts=["玄铁令牌的下落是什么？", "石匣被打开"],
        nominated_chapters=[88],
        density_window=True,
        radius=40,
    )
    assert 88 in b_fixed.chapters
    assert "二十年前的血案真相" in b_fixed.text
    assert "真相大白" in b_fixed.text


@pytest.mark.asyncio
async def test_b2_recheck_enhanced_retrieval_and_intra_candidate_alignment():
    chapters = {
        1: "他把一枚玄铁令牌藏进石匣。",
        88: "城主终于亮出玄铁令牌，打开石匣揭开了二十年前的血案真相。",
    }
    item = ForeshadowItem(
        id="fs_001",
        description="玄铁令牌与石匣的秘密",
        status="active",
        first_seen_batch=1,
        last_seen_batch=1,
        first_seen_chapter=1,
        last_seen_chapter=1,
        anchor_chapter=1,
        anchor_quote="他把一枚玄铁令牌藏进石匣。",
        question="石匣里的秘密何时揭开？",
        answer_criteria="打开石匣揭开真相",
        resolved_chapter=88,
        resolution="第88章城主打开石匣揭开血案真相",
        lead=False,
    )

    # Model returns SUPPORTED with a valid verbatim quote from segment 2 (ch 88),
    # but mistakenly writes evidence_id of segment 1 (ch 1)
    async def fake_model(prompt: str) -> str:
        import re
        eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
        wrong_eid = eids[0] if eids else "ev_wrong"
        return json.dumps(
            [
                {
                    "candidate_id": "fs_001",
                    "verdict": "SUPPORTED",
                    "answer": "第88章城主打开石匣揭开真相。",
                    "evidence_id": wrong_eid,
                    "supporting_quote": "城主终于亮出玄铁令牌，打开石匣揭开了二十年前的血案真相。",
                    "counter_search": {
                        "performed": True,
                        "checked_evidence_ids": [wrong_eid],
                        "counter_quotes": [],
                    },
                }
            ],
            ensure_ascii=False,
        )

    trace = BTraceCollector(run_id="t3_unit")
    outcome = await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=fake_model,
        trace=trace,
        enhanced_retrieval=True,
    )
    assert len(outcome.verdicts) == 1
    assert outcome.verdicts[0].verdict == "SUPPORTED"
    assert outcome.verdicts[0].checked is True
    assert any(r.reason_code == "INTRA_CANDIDATE_EVIDENCE_ID_ALIGNED" for r in trace.records)


@pytest.mark.asyncio
async def test_b2_recheck_alignment_never_fabricates_counter_search():
    """Regression for Issue #3: intra-candidate evidence_id alignment must NOT fabricate
    counter_search.performed=True when the model response omits counter_search.
    """
    chapters = {
        1: "他把一枚玄铁令牌藏进石匣。",
        88: "城主终于亮出玄铁令牌，打开石匣揭开了二十年前的血案真相。",
    }
    item = ForeshadowItem(
        id="fs_no_cs",
        description="玄铁令牌与石匣的秘密",
        status="active",
        first_seen_batch=1,
        last_seen_batch=1,
        first_seen_chapter=1,
        last_seen_chapter=1,
        anchor_chapter=1,
        anchor_quote="他把一枚玄铁令牌藏进石匣。",
        question="石匣里的秘密何时揭开？",
        answer_criteria="打开石匣揭开真相",
        resolved_chapter=88,
        resolution="第88章城主打开石匣揭开血案真相",
        lead=False,
    )

    async def model_without_counter_search(prompt: str) -> str:
        import re
        eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
        wrong_eid = eids[0] if eids else "ev_wrong"
        return json.dumps(
            [
                {
                    "candidate_id": "fs_no_cs",
                    "verdict": "SUPPORTED",
                    "answer": "第88章城主打开石匣揭开真相。",
                    "evidence_id": wrong_eid,
                    "supporting_quote": "城主终于亮出玄铁令牌，打开石匣揭开了二十年前的血案真相。",
                }
            ],
            ensure_ascii=False,
        )

    out_off = await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=model_without_counter_search,
        enhanced_retrieval=False,
    )
    out_on = await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=model_without_counter_search,
        enhanced_retrieval=True,
    )
    assert out_off.verdicts[0].verdict == "INSUFFICIENT"
    assert out_off.verdicts[0].reason == "NO_COUNTER_SEARCH"
    assert out_off.verdicts[0].counter_search_performed is False
    assert out_on.verdicts[0].verdict == "INSUFFICIENT"
    assert out_on.verdicts[0].reason == "NO_COUNTER_SEARCH"
    assert out_on.verdicts[0].counter_search_performed is False



@pytest.mark.asyncio
async def test_b2_recheck_rejects_quote_equals_anchor():
    """Regression test: citing the exact anchor_quote as resolution evidence must be downgraded to INSUFFICIENT (QUOTE_EQUALS_ANCHOR)."""
    chapters = {
        1: "第九章：凌然问：“达芬奇睡眠法真的有那么神奇？不可能的，弄不好只有长的帅，才有用。”",
    }
    item = ForeshadowItem(
        id="fs_anchor_echo",
        description="凌然高强度手术仍精力旺盛的原因",
        status="active",
        first_seen_batch=1,
        last_seen_batch=1,
        first_seen_chapter=1,
        last_seen_chapter=1,
        anchor_chapter=1,
        anchor_quote="达芬奇睡眠法真的有那么神奇？不可能的，弄不好只有长的帅，才有用。",
        entity_key="凌然_精力",
        question="凌然连续高强度工作仍保持旺盛精力的原因是什么？",
        answer_criteria="交代凌然保持精力的真正原因",
        resolved_chapter=1,
        resolution="提到凌然使用特殊睡眠法",
        evidence_chapters=[1],
        lead=False,
    )

    async def model_echoing_anchor(prompt: str) -> str:
        import re
        eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
        return json.dumps(
            [
                {
                    "candidate_id": "fs_anchor_echo",
                    "verdict": "SUPPORTED",
                    "answer": "原文点出凌然依靠达芬奇睡眠法保持精力。",
                    "evidence_id": eids[0],
                    "supporting_quote": "达芬奇睡眠法真的有那么神奇？",
                    "counter_search": {
                        "performed": True,
                        "checked_evidence_ids": list(eids),
                    },
                }
            ],
            ensure_ascii=False,
        )

    out = await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=model_echoing_anchor,
        enhanced_retrieval=True,
    )
    assert out.verdicts[0].verdict == "INSUFFICIENT"
    assert out.verdicts[0].reason == "QUOTE_EQUALS_ANCHOR"


@pytest.mark.asyncio
async def test_f5_recheck_v2_wires_enable_c_on_demand_audit():
    """Regression for F5: enable_c_on_demand_audit wires select_audit_route into run_recheck_v2 and dump_recheck_detail."""
    from backend.services.recheck_v2 import dump_recheck_detail

    chapters = {
        1: "第一章：方醒打开铜匣，铜匣底下那层是空的。",
        14: "第十四章：丹阳子确实不认识字，并且还非常非常排斥别人认字。",
    }
    item_echo = ForeshadowItem(
        id="fs_echo",
        description="铜匣夹层",
        status="active",
        first_seen_batch=1,
        last_seen_batch=1,
        first_seen_chapter=1,
        last_seen_chapter=1,
        anchor_chapter=1,
        anchor_quote="方醒打开铜匣，铜匣底下那层是空的。",
        question="铜匣里有什么？",
        answer_criteria="交代铜匣内物",
        lead=False,
    )
    item_ok = ForeshadowItem(
        id="fs_ok",
        description="丹阳子识字与否",
        status="active",
        first_seen_batch=1,
        last_seen_batch=1,
        first_seen_chapter=1,
        last_seen_chapter=1,
        anchor_chapter=1,
        anchor_quote="方醒打开铜匣",
        question="丹阳子是否真的不认识字？",
        answer_criteria="确认丹阳子识字与否",
        resolved_chapter=14,
        lead=False,
    )

    async def fake_model(prompt: str) -> str:
        import re
        rows = []
        for block in prompt.split("### 候选 candidate_id=")[1:]:
            cid = block.split()[0]
            eids = re.findall(r"\[evidence_id=([a-f0-9]+)", block)
            if cid == "fs_echo":
                rows.append({
                    "candidate_id": "fs_echo",
                    "verdict": "SUPPORTED",
                    "answer": "铜匣底下那层是空的。",
                    "evidence_id": eids[0],
                    "supporting_quote": "铜匣底下那层是空的",
                    "counter_search": {"performed": True, "checked_evidence_ids": eids},
                })
            elif cid == "fs_ok":
                rows.append({
                    "candidate_id": "fs_ok",
                    "verdict": "SUPPORTED",
                    "answer": "第14章明确交代丹阳子确实不认识字。",
                    "evidence_id": eids[-1],
                    "supporting_quote": "丹阳子确实不认识字，并且还非常非常排斥别人认字。",
                    "counter_search": {"performed": True, "checked_evidence_ids": eids},
                })
        return json.dumps(rows, ensure_ascii=False)

    out = await run_recheck_v2(
        [item_echo, item_ok],
        chapters=chapters,
        call_model=fake_model,
        enhanced_retrieval=False,
        enable_c_on_demand_audit=True,
    )
    v_map = {v.candidate_id: v for v in out.verdicts}
    assert v_map["fs_echo"].verdict == "INSUFFICIENT"
    assert v_map["fs_echo"].reason == "MECHANICAL_INCREMENT_REJECTED"
    assert v_map["fs_ok"].verdict == "SUPPORTED"
    assert out.audit_routes["fs_echo"]["route"] == "hard_reject_mechanical"
    assert out.audit_routes["fs_ok"]["route"] == "current_only"
    detail = dump_recheck_detail(out, model="test-m", base_url="http://localhost")
    assert "audit_routes" in detail
    assert detail["audit_routes"]["fs_ok"]["route"] == "current_only"


def test_f2_calibrated_b_matcher_rejects_same_entity_same_chapter_different_question():
    """Regression for F2: same entity + same chapter with a different question must NEVER match as a recall hit."""
    from scripts.experiments.bc.bc_eval_b_zero_leakage import (
        match_observation_to_items,
        match_observations_one_to_one,
        score_observation_item_pair,
        verify_resolution_evidence,
    )
    from backend.services.recheck_v2 import CandidateVerdict

    # 1. Reviewer's exact minimal reproduction: "张三的铁匣里是什么" vs "剑法能否练成"
    ob_zhang = {
        "obs_id": "repro_zhang",
        "anchor_quote": "铁匣锁了三道",
        "entity": "张三",
        "question": "铁匣里是什么",
        "answer_criteria": "说明铁匣内物",
    }
    it_wrong = {
        "id": "fs_wrong",
        "first_seen": 1,
        "anchor_chapter": 1,
        "anchor_quote": "无",
        "clue": "张三练剑",
        "question": "剑法能否练成",
        "answer_criteria": "练成与否",
    }
    diag_wrong = score_observation_item_pair(ob_zhang, it_wrong, obs_anchor_ch=1, packet_ch_range=(1, 6))
    assert diag_wrong["status"] == "unmatched"
    assert diag_wrong["score"] == 0.0
    assert match_observation_to_items(ob_zhang, [it_wrong], obs_anchor_ch=1, packet_ch_range=(1, 6)) is None

    # 2. Real dev_obs_009 ("新宏观经济学模型如何命名") vs fs_027 ("模型产生怎样的学术成果、由谁发表")
    ob_009 = {
        "obs_id": "dev_obs_009",
        "anchor_quote": "只是关于这个模型……我们该如何命名它？",
        "entity": "L-Z模型",
        "question": "陆舟改进后的新宏观经济学模型在报告会上将被如何命名以区分原版Lu-Bewley模型？",
        "answer_criteria": "报告会现场学者提议并确定改进后模型的新名称（L-Z模型）",
        "resolution_quote": "因此，我提议用一个全新的方式对其命名……即L-Z模型。",
    }
    fs_027 = {
        "id": "fs_027",
        "first_seen": 15,
        "anchor_chapter": 15,
        "anchor_quote": "而这几张草稿纸上的东西，就是经过了改进之后的Lu-Bewley模型。",
        "clue": "陆舟留下改进后的Lu-Bewley模型草稿",
        "question": "改进后的Lu-Bewley模型究竟会产生怎样的学术成果，陆舟又会让谁以何种方式发表它？",
        "answer_criteria": "后文需要交代模型由谁整理成论文、谁在会议上报告",
    }
    assert match_observation_to_items(ob_009, [fs_027], obs_anchor_ch=18, packet_ch_range=(15, 20)) is None

    # 3. 1-to-1 matching prevents two observations from claiming the same candidate item
    it_box = {
        "id": "fs_box",
        "first_seen": 1,
        "anchor_chapter": 1,
        "anchor_quote": "铁匣锁了三道",
        "clue": "张三的铁匣锁了三道，里面藏着未知内物",
        "question": "张三的铁匣里究竟装的是什么内物？",
        "answer_criteria": "说明铁匣内物",
    }
    ob_zhang_dup = dict(ob_zhang, obs_id="repro_zhang_2")
    assigned = match_observations_one_to_one(
        [ob_zhang, ob_zhang_dup],
        [it_box],
        {"repro_zhang": (1, 2, (1, 6)), "repro_zhang_2": (1, 2, (1, 6))},
    )
    strict_hits = [oid for oid, m in assigned.items() if m["diag"]["status"] == "strict_equivalent"]
    assert len(strict_hits) == 1

    # 4. Resolution verification rejects DIFFERENT_ANSWER and unverified answers
    v_diff = CandidateVerdict(candidate_id="fs_box", verdict="DIFFERENT_ANSWER", answer="铁匣被扔了", quote="铁匣被扔进河里")
    res_diff = verify_resolution_evidence(
        {**ob_zhang, "resolution_quote": "打开铁匣，里面是一卷残破羊皮古图"},
        it_box,
        v_diff,
        a_ch=1,
        r_ch=2,
        chapters={1: "铁匣锁了三道", 2: "打开铁匣，里面是一卷残破羊皮古图。铁匣被扔进河里。"},
    )
    assert res_diff["verified"] is False
    assert res_diff["reason"] == "different_answer_not_strict_support"


