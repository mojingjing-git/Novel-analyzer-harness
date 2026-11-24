"""伏笔复检 V3（backend.services.recheck_v3）单元与集成契约测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.config.settings import AppConfig
from backend.core.evidence import EvidenceIndex
from backend.services.final_summary import (
    FinalSummaryRunner,
    _recheck_progress_message,
)
from backend.services.recheck_v2 import AUTO_RESOLVED, OPEN
from backend.services.recheck_v3 import (
    COMPACT_CHAPTER_CHARS,
    WIDE_CHAPTER_CHARS,
    _salvage_json_array_rows,
    _select_primary_evidence,
    apply_v3_verdict_to_item,
    build_adaptive_digest_chunks,
    build_unit_headers,
    detect_protagonist_stopwords,
    format_unit_segment,
    run_recheck_v3,
    tokenize_entity_tokens,
    verify_evidence_chain_v3,
)
from backend.utils.foreshadow_ledger import ForeshadowItem, ForeshadowLedger
from backend.services.recheck_resolution import STOPPED


def _make_item(
    fid: str,
    *,
    desc: str = "神秘铜盒的真正来历",
    question: str = "神秘铜盒是谁留下的，用途是什么？",
    anchor_quote: str = "",
    anchor_chapter: int = 1,
    first_seen_chapter: int = 1,
    last_seen_chapter: int = 1,
    last_seen_batch: int = 0,
    entity_key: str = "铜盒",
    needs_review: bool = False,
    resolution: str | None = None,
    resolved_chapter: int | None = None,
) -> ForeshadowItem:
    return ForeshadowItem(
        id=fid,
        description=desc,
        first_seen_chapter=first_seen_chapter,
        first_seen_batch=0,
        last_seen_chapter=last_seen_chapter,
        last_seen_batch=last_seen_batch,
        status="active",
        question=question,
        entity_key=entity_key,
        anchor_quote=anchor_quote,
        anchor_chapter=anchor_chapter,
        needs_review=needs_review,
        resolution=resolution,
        resolved_chapter=resolved_chapter,
    )


def test_apply_v3_verdict_resolved_tiers_and_literary_chapter() -> None:
    """三种回收分级正确写回账本并按 display_labels 映射文学章号。"""
    labels = {1: "第 1 回", 2: "第 56 回", 3: "第 108 回", 4: "第 112 回"}

    it_exp = _make_item("fs_exp")
    apply_v3_verdict_to_item(
        it_exp,
        tier="RESOLVED_EXPLICIT",
        unresolved_type="",
        answer="铜盒乃先师所留镇魂之器。",
        evidence_chain=[{"unit_id": 2, "offset": 10, "quote": "铜盒正是先师所留", "role": "payoff"}],
        display_labels=labels,
        unit_noun="文件",
    )
    assert it_exp.status == "resolved"
    assert it_exp.resolved_chapter == 56
    assert it_exp.resolution == "铜盒乃先师所留镇魂之器。"
    assert it_exp.source_type == "auto_recheck_v3"
    assert it_exp.resolution_quality == "verified"
    assert it_exp.needs_review is False
    assert "V3复检：显式回收已核实" in it_exp.notes

    it_imp = _make_item("fs_imp")
    apply_v3_verdict_to_item(
        it_imp,
        tier="RESOLVED_IMPLICIT",
        unresolved_type="",
        answer="黑袍人摘下面具，正是昔日守墓人。",
        evidence_chain=[{"unit_id": 3, "offset": 0, "quote": "面具下竟是昔日守墓人", "role": "payoff"}],
        display_labels=labels,
        unit_noun="文件",
    )
    assert it_imp.status == "resolved"
    assert it_imp.resolved_chapter == 108
    assert it_imp.resolution_quality == "verified"
    assert it_imp.needs_review is False
    assert "V3复检：隐式/别名兑现已核实" in it_imp.notes

    it_part = _make_item("fs_part")
    apply_v3_verdict_to_item(
        it_part,
        tier="PARTIALLY_RESOLVED",
        unresolved_type="",
        answer="真凶身份已揭露，其幕后资金流向尚未写明。",
        evidence_chain=[{"unit_id": 4, "offset": 5, "quote": "真凶当众供出一切", "role": "payoff"}],
        display_labels=labels,
        unit_noun="文件",
    )
    assert it_part.status == "resolved"
    assert it_part.resolved_chapter == 112
    assert it_part.resolution.startswith("[核心已回收·支线留白] ")
    assert it_part.resolution_quality == "verified"
    assert it_part.needs_review is False
    assert "V3复检：核心已回收（次要支线留白）" in it_part.notes


def test_apply_v3_verdict_unresolved_clears_stage1_tentative_and_allows_dormant() -> None:
    """UNRESOLVED 必须清空阶段 1 暂挂标记，使旧条目后续能正常进入 dormant。"""
    labels = {1: "第1章", 2: "第2章"}
    it_hook = _make_item(
        "fs_hook",
        first_seen_chapter=1,
        last_seen_chapter=2,
        last_seen_batch=0,
        needs_review=True,
        resolution="阶段1疑似回收",
        resolved_chapter=5,
    )
    it_pit = _make_item(
        "fs_pit",
        first_seen_chapter=1,
        last_seen_chapter=2,
        last_seen_batch=0,
        needs_review=True,
        resolution="阶段1未核实提名",
        resolved_chapter=8,
    )

    apply_v3_verdict_to_item(
        it_hook,
        tier="UNRESOLVED",
        unresolved_type="ATMOSPHERIC_HOOK",
        answer="仅为氛围收尾意象。",
        evidence_chain=[],
        display_labels=labels,
    )
    apply_v3_verdict_to_item(
        it_pit,
        tier="UNRESOLVED",
        unresolved_type="UNFILLED_PIT",
        answer="全文未再提及。",
        evidence_chain=[],
        display_labels=labels,
    )

    for it in (it_hook, it_pit):
        assert it.status == "active"
        assert it.needs_review is False
        assert it.resolution is None
        assert it.resolved_chapter is None

    assert "V3复检：氛围彩蛋钩子（未设实质待填坑）" in it_hook.notes
    assert "V3复检：未见实证回收" in it_pit.notes

    ledger = ForeshadowLedger(items=[it_hook, it_pit])
    ledger.reconcile_and_update(batch_idx=20, current_chapter=260)
    assert it_hook.status == "dormant"
    assert it_pit.status == "dormant"


def test_verify_evidence_chain_v3_guards_and_pre_setup_backref() -> None:
    """确定性证据门禁：拦截同章重述与锚点复读，支持合法前置因果回指。"""
    filler = "甲乙丙丁" * 65  # 260 chars > 200 char same-chapter guard
    chapters = {
        1: "第一章 溯源\n早年寒冰谷深处封印着一柄残剑，剑柄刻着古篆青莲印记。",
        5: f"第五章 现世\n林昭捡起断裂铜牌，上面有个缺口。{filler}原来那断裂铜牌正是寒冰谷门主失落已久的旧信物。",
        8: "第八章 余波\n林昭捡起断裂铜牌，上面有个缺口。众人看着青莲印记面色大变。",
    }
    item = _make_item("fs_guard", anchor_quote="林昭捡起断裂铜牌，上面有个缺口。", anchor_chapter=5)

    # 1) 同章设伏附近（offset < setup_after_offset + 200）必须被拦下
    ver1, ok1, reason1 = verify_evidence_chain_v3(
        chapters,
        item,
        setup_unit=5,
        raw_verdict="RESOLVED_EXPLICIT",
        raw_evidence_chain=[{"unit_id": 5, "quote": "第五章 现世\n林昭捡起", "role": "payoff"}],
        allowed_post_units={5: chapters[5], 8: chapters[8]},
        allowed_pre_units={},
        setup_after_offset=4,
        needs_pre_setup_backref=False,
    )
    assert ok1 is False
    assert ver1 == []
    assert "same_chapter_setup_restatement" in reason1

    # 2) 复读 anchor_quote（即使在后续章节出现）必须被拦下
    ver2, ok2, reason2 = verify_evidence_chain_v3(
        chapters,
        item,
        setup_unit=5,
        raw_verdict="RESOLVED_EXPLICIT",
        raw_evidence_chain=[{"unit_id": 8, "quote": "林昭捡起断裂铜牌，上面有个缺口。", "role": "payoff"}],
        allowed_post_units={8: chapters[8]},
        allowed_pre_units={},
        setup_after_offset=4,
        needs_pre_setup_backref=False,
    )
    assert ok2 is False
    assert ver2 == []
    assert "repeats_anchor_quote" in reason2

    # 3) 同章 +200 字符之后的真正回收应当通过
    ver3, ok3, _ = verify_evidence_chain_v3(
        chapters,
        item,
        setup_unit=5,
        raw_verdict="RESOLVED_EXPLICIT",
        raw_evidence_chain=[{"unit_id": 5, "quote": "原来那断裂铜牌正是寒冰谷门主失落已久的旧信物。", "role": "payoff"}],
        allowed_post_units={5: chapters[5]},
        allowed_pre_units={},
        setup_after_offset=4,
        needs_pre_setup_backref=False,
    )
    assert ok3 is True
    assert ver3[0]["unit_id"] == 5

    # 4) 未开启前置因果回指且不在 allowed_pre_units 时拒绝 u < setup_unit
    ver4, ok4, reason4 = verify_evidence_chain_v3(
        chapters,
        item,
        setup_unit=5,
        raw_verdict="RESOLVED_EXPLICIT",
        raw_evidence_chain=[{"unit_id": 1, "quote": "剑柄刻着古篆青莲印记。", "role": "payoff"}],
        allowed_post_units={8: chapters[8]},
        allowed_pre_units={},
        setup_after_offset=4,
        needs_pre_setup_backref=False,
    )
    assert ok4 is False
    assert "before_setup_unit_1<5" in reason4

    # 5) 开启 needs_pre_setup_backref 时允许回指前文因果单元 u < setup_unit
    ver5, ok5, _ = verify_evidence_chain_v3(
        chapters,
        item,
        setup_unit=5,
        raw_verdict="RESOLVED_IMPLICIT",
        raw_evidence_chain=[{"unit_id": 1, "quote": "剑柄刻着古篆青莲印记。", "role": "causal_backref"}],
        allowed_post_units={8: chapters[8]},
        allowed_pre_units={},
        setup_after_offset=4,
        needs_pre_setup_backref=True,
    )
    assert ok5 is True
    assert ver5[0]["unit_id"] == 1


def test_generalization_helpers() -> None:
    """5 项通用性辅助函数：主视角停用词、自适应分块、译名切分、大章双窗、正篇/番外标注。"""
    # 1) detect_protagonist_stopwords: < 12 章只返回基础词；>= 12 章识别高 DF 人名
    small_ch = {i: f"第{i}章\n陈平安在小镇走动。" for i in range(1, 6)}
    small_cands = [
        {"item": _make_item("a1", desc="陈平安拿到了泥瓶", entity_key="陈平安/泥瓶")},
        {"item": _make_item("a2", desc="陈平安路过廊桥", entity_key="陈平安/廊桥")},
    ]
    small_idx = EvidenceIndex(small_ch)
    small_idx.ensure_indexed()
    assert detect_protagonist_stopwords(small_ch, small_cands, small_idx) == frozenset({"主人公", "主角", "男主", "女主"})

    big_ch = {
        i: (
            f"第{i}章\n陈平安练拳之后沿溪边巡夜。"
            + ("剑气长城传来警钟。" if i in (3, 9) else "")
        )
        for i in range(1, 17)
    }
    big_cands = [
        {"item": _make_item("b1", desc="陈平安与剑气长城的约定", entity_key="陈平安/剑气长城")},
        {"item": _make_item("b2", desc="陈平安收起古剑", entity_key="陈平安/古剑")},
    ]
    big_idx = EvidenceIndex(big_ch)
    big_idx.ensure_indexed()
    stops = detect_protagonist_stopwords(big_ch, big_cands, big_idx)
    assert "陈平安" in stops
    assert "剑气长城" not in stops

    # 2) build_adaptive_digest_chunks: 小语料与大型语料分块数量契约
    ch_8 = {i: f"第{i}章\n短章内容{i}。" for i in range(1, 9)}
    lbl_8 = {i: f"第{i}章" for i in range(1, 9)}
    macros_8, blocks_8, u2b_8, _, _ = build_adaptive_digest_chunks(ch_8, lbl_8, block_size=4, blocks_per_macro=15)
    assert len(blocks_8) == 2
    assert len(macros_8) == 1
    assert macros_8[0]["start_unit"] == 1 and macros_8[0]["end_unit"] == 8
    assert u2b_8[6][:2] == (5, 8)

    ch_240 = {i: f"第{i}章\n长篇章节内容{i}。" for i in range(1, 241)}
    lbl_240 = {i: f"第{i}章" for i in range(1, 241)}
    macros_240, blocks_240, _, _, _ = build_adaptive_digest_chunks(ch_240, lbl_240, block_size=4, blocks_per_macro=15)
    assert len(blocks_240) == 60
    assert len(macros_240) == 4
    assert macros_240[-1]["start_unit"] == 181 and macros_240[-1]["end_unit"] == 240

    # 3) tokenize_entity_tokens: 中点译名与别名拆分，同时过滤主视角停用词
    toks = tokenize_entity_tokens(
        "克莱恩·莫雷蒂 / 愚者—周明瑞（主人公）",
        protagonist_stops=frozenset({"周明瑞"}),
    )
    assert "克莱恩·莫雷蒂" in toks
    assert "克莱恩" in toks
    assert "莫雷蒂" in toks
    assert "愚者" in toks
    assert "周明瑞" not in toks
    assert "主人公" not in toks

    # 4) format_unit_segment: 万字大章双窗口聚拢 + 宽/窄模式字符控制
    giant_body = (
        "第三章 万字长章\n"
        + ("无关闲话铺垫。" * 40)
        + "他在密室角落找到了半块血玉佩，背面刻着星纹。"
        + ("漫长赶路描写。" * 1400)
        + "地底石门缓缓开启，里面静静躺着那口青铜棺。"
        + ("结尾过渡环境。" * 80)
    )
    giant_ch = {3: giant_body}
    giant_lbl = {3: "第3章"}
    giant_hdr = build_unit_headers(giant_ch)
    wide_seg = format_unit_segment(
        giant_ch,
        giant_lbl,
        giant_hdr,
        3,
        src="关键词命中",
        focus_terms=["血玉佩", "青铜棺"],
        max_chars=WIDE_CHAPTER_CHARS,
    )
    assert "……（中略）……" in wide_seg
    assert "血玉佩" in wide_seg
    assert "青铜棺" in wide_seg

    compact_seg = format_unit_segment(
        giant_ch,
        giant_lbl,
        giant_hdr,
        3,
        src="关键词命中",
        focus_terms=["血玉佩", "青铜棺"],
        max_chars=COMPACT_CHAPTER_CHARS,
    )
    assert len(compact_seg) < len(wide_seg)

    # 5) build_unit_headers: [正篇] 与 [番外/外传篇]（含无标题正文块）
    ch_headers = {i: f"第{i}章 正文\n平铺直叙的正文。" for i in range(1, 31)}
    ch_headers[1] = "第一章 风起云涌\n正文开始。"
    ch_headers[12] = "他在昏暗的走廊里停下了脚步，仔细倾听着门后传来的微弱呼吸声，心里盘算着对策。\n第二行。"
    ch_headers[26] = "第二十六章 大结局\n一切尘埃落定。"
    ch_headers[27] = "许多年过去了，小镇上的茶馆依旧每天清晨准时开门迎客，老掌柜擦拭着柜台。\n后续几句。"
    ch_headers[28] = "番外一 岁月静好\n外传故事。"
    headers = build_unit_headers(ch_headers)

    assert headers[1]["is_epilogue"] is False
    assert headers[1]["tag"] == "【正篇 | 第一章 风起云涌】"
    assert headers[12]["raw_title"] == ""
    assert headers[12]["is_epilogue"] is False
    assert headers[12]["tag"] == "【正篇】"
    assert headers[26]["is_epilogue"] is False
    assert headers[27]["raw_title"] == ""
    assert headers[27]["is_epilogue"] is True
    assert headers[27]["tag"].startswith("【番外/外传篇")
    assert " | 】" not in headers[27]["tag"]
    assert headers[28]["is_epilogue"] is True
    assert headers[28]["raw_title"] == "番外一 岁月静好"


def test_run_recheck_v3_multihop_repair_and_small_corpus_fast_path() -> None:
    """小语料跳过大块别名路由；支持 Hop2 追查 + U5 修复，UNRESOLVED 清除阶段 1 暂挂。"""
    chapters = {
        1: "第一章 初遇\n夜雨中林渊在荒庙门槛拾得一枚残缺铁牌。",
        2: "第二章 线索\n铁牌背面只有半个模糊纹路，看不出来历。",
        3: "第三章 闲谈\n集市茶摊有人议论北境风雪。",
        4: "第四章 风铃\n窗外檐角铜铃在暮风里无声晃动。",
        5: "第五章 真相\n暗阁长老终于供认不讳，那块锈蚀残片乃是十年前夜巡司遗落的调兵虎符。",
        6: "第六章 余韵\n天明时分一切归于平静。",
    }
    labels = {i: f"第 {i} 回" for i in range(1, 7)}
    it1 = _make_item(
        "fs_1",
        desc="荒庙残缺铁牌的真实来历",
        question="林渊拾得的残缺铁牌到底是什么信物？",
        anchor_quote="夜雨中林渊在荒庙门槛拾得一枚残缺铁牌。",
        anchor_chapter=1,
        entity_key="铁牌",
    )
    it2 = _make_item(
        "fs_2",
        desc="铜铃晃动的氛围描写",
        question="铜铃究竟暗指什么？",
        anchor_quote="窗外檐角铜铃在暮风里无声晃动。",
        anchor_chapter=4,
        entity_key="铜铃",
        needs_review=True,
        resolution="阶段1误判提名",
        resolved_chapter=6,
    )

    seen_purposes: list[str] = []

    async def _fake_model(prompt: str) -> str:
        if "待拆解条目：" in prompt:
            seen_purposes.append("u1_split")
            return json.dumps([
                {
                    "fs_id": "fs_1",
                    "core_question": "铁牌是什么信物",
                    "bonus_aspects": "",
                    "setup_event_anchor": "拾得残缺铁牌",
                    "needs_pre_setup_backref": False,
                    "key_entities": ["铁牌"],
                },
                {
                    "fs_id": "fs_2",
                    "core_question": "铜铃暗指什么",
                    "bonus_aspects": "",
                    "setup_event_anchor": "檐角铜铃晃动",
                    "needs_pre_setup_backref": False,
                    "key_entities": ["铜铃"],
                },
            ], ensure_ascii=False)
        if "只提取高确信度的" in prompt or "待检索伏笔（共" in prompt:
            seen_purposes.append("should_not_run_u1_large")
            return "[]"
        if "你是零人工长篇小说伏笔深度核查侦探" in prompt:
            if "第 1/3 跳" in prompt:
                seen_purposes.append("u3_hop1")
                return json.dumps([
                    {
                        "fs_id": "fs_1",
                        "verdict": "RESOLVED_EXPLICIT",
                        "unresolved_type": "",
                        "confidence": "high",
                        "answer": "长老供认铁牌是夜巡司调兵虎符。",
                        "evidence_chain": [{"unit_id": 5, "quote": "这句不是逐字原文用来触发修复", "role": "payoff"}],
                        "follow_up": None,
                    },
                    {
                        "fs_id": "fs_2",
                        "verdict": "UNRESOLVED",
                        "unresolved_type": "ATMOSPHERIC_HOOK",
                        "confidence": "high",
                        "answer": "仅氛围描写，无后续待填坑。",
                        "evidence_chain": [],
                        "follow_up": None,
                    },
                ], ensure_ascii=False)
            seen_purposes.append("u3_hop2")
            return "[]"
        if "你是长篇小说终审总编辑" in prompt:
            seen_purposes.append("u4_audit")
            return json.dumps([
                {
                    "fs_id": "fs_1",
                    "verdict": "RESOLVED_EXPLICIT",
                    "unresolved_type": "",
                    "answer": "铁牌乃夜巡司调兵虎符。",
                    "evidence_chain": [{"unit_id": 5, "quote": "仍是不存在的改写引文测试降级修复", "role": "payoff"}],
                },
                {
                    "fs_id": "fs_2",
                    "verdict": "UNRESOLVED",
                    "unresolved_type": "ATMOSPHERIC_HOOK",
                    "answer": "纯氛围渲染。",
                    "evidence_chain": [],
                },
            ], ensure_ascii=False)
        if "确定性原文校验未能在所给正文单元中找到你为" in prompt:
            seen_purposes.append("u5_repair")
            return json.dumps({
                "fs_id": "fs_1",
                "verdict": "RESOLVED_EXPLICIT",
                "unresolved_type": "",
                "answer": "铁牌是十年前夜巡司遗落的调兵虎符。",
                "evidence_chain": [{"unit_id": 5, "quote": "那块锈蚀残片乃是十年前夜巡司遗落的调兵虎符。", "role": "payoff"}],
            }, ensure_ascii=False)
        return "[]"

    stages: list[str] = []
    outcome = asyncio.run(
        run_recheck_v3(
            [it1, it2],
            chapters=chapters,
            call_model=_fake_model,
            display_labels=labels,
            emit=lambda ev: stages.append(str(ev.get("stage") or "")),
            unit_noun="文件",
            batch_size=4,
            concurrency=2,
        )
    )

    assert "should_not_run_u1_large" not in seen_purposes
    assert "u3_hop1" in seen_purposes and "u5_repair" in seen_purposes
    assert outcome.status == "COMPLETED"
    assert outcome.applied["fs_1"] == AUTO_RESOLVED
    assert outcome.applied["fs_2"] == OPEN
    assert outcome.resolved_chapters["fs_1"] == 5
    assert outcome.audit_routes["fs_1"]["repaired"] is True
    assert outcome.coverage["v3_tiers"]["RESOLVED_EXPLICIT"] == 1
    assert outcome.coverage["v3_tiers"]["UNRESOLVED"] == 1
    assert it1.status == "resolved"
    assert it1.resolved_chapter == 5
    assert it2.status == "active"
    assert it2.needs_review is False
    assert it2.resolution is None
    assert it2.resolved_chapter is None
    assert "u5_repair" in stages


def test_run_recheck_v3_multihop_followup_and_routing() -> None:
    """中长篇（>12 单元）启用别名路由，并通过 Hop 2 follow_up 补捞首轮未选中的远章。"""
    chapters = {i: f"第{i}章 日常\n众人沿着官道平静赶路，无事发生。" for i in range(1, 15)}
    chapters[1] = "第一章 起因\n夜市摊主递给沈砚半块青纹残玉，叮嘱切莫示人。"
    chapters[2] = "第二章 余绪\n沈砚将残玉贴身收好，继续北上。"
    chapters[13] = "第十三章 揭晓\n东宫侍卫长当众认出那昔年皇室印玺碎片乃是太子流落民间的贴身信物。"
    labels = {i: f"第 {i} 回" for i in range(1, 15)}

    it = _make_item(
        "fs_hop",
        desc="夜市残玉的真实来历",
        question="夜市残玉到底是什么物件？",
        anchor_quote="夜市摊主递给沈砚半块青纹残玉，叮嘱切莫示人。",
        anchor_chapter=1,
        entity_key="青纹残玉",
    )
    purposes: list[str] = []

    async def _model(prompt: str) -> str:
        if "待拆解条目：" in prompt:
            purposes.append("u1_split")
            return json.dumps([{
                "fs_id": "fs_hop",
                "core_question": "残玉是什么物件",
                "bonus_aspects": "",
                "setup_event_anchor": "沈砚获赠青纹残玉",
                "needs_pre_setup_backref": False,
                "key_entities": ["青纹残玉"],
            }], ensure_ascii=False)
        if "只提取高确信度的" in prompt:
            purposes.append("u1_alias_pair")
            return "[]"
        if "待检索伏笔（共" in prompt:
            purposes.append("u1_route")
            return "[]"
        if "你是零人工长篇小说伏笔深度核查侦探" in prompt:
            if "第 1/3 跳" in prompt:
                purposes.append("u3_hop1")
                return json.dumps([{
                    "fs_id": "fs_hop",
                    "verdict": "UNRESOLVED",
                    "unresolved_type": "UNFILLED_PIT",
                    "confidence": "low",
                    "answer": "前几章尚未揭晓，需追查后续第13章。",
                    "evidence_chain": [],
                    "follow_up": {"keyword_queries": ["皇室印玺"], "unit_ranges": [[13, 13]], "reason": "追查第13章"},
                }], ensure_ascii=False)
            purposes.append("u3_hop2")
            return json.dumps([{
                "fs_id": "fs_hop",
                "verdict": "RESOLVED_EXPLICIT",
                "unresolved_type": "",
                "confidence": "high",
                "answer": "实为太子流落民间的贴身皇室印玺碎片。",
                "evidence_chain": [{"unit_id": 13, "quote": "那昔年皇室印玺碎片乃是太子流落民间的贴身信物。", "role": "payoff"}],
                "follow_up": None,
            }], ensure_ascii=False)
        if "你是长篇小说终审总编辑" in prompt:
            purposes.append("u4_audit")
            return json.dumps([{
                "fs_id": "fs_hop",
                "verdict": "RESOLVED_EXPLICIT",
                "unresolved_type": "",
                "answer": "实为太子流落民间的皇室印玺碎片。",
                "evidence_chain": [{"unit_id": 13, "quote": "那昔年皇室印玺碎片乃是太子流落民间的贴身信物。", "role": "payoff"}],
            }], ensure_ascii=False)
        return "[]"

    outcome = asyncio.run(
        run_recheck_v3(
            [it],
            chapters=chapters,
            call_model=_model,
            display_labels=labels,
            unit_noun="文件",
            batch_size=2,
            concurrency=2,
        )
    )
    assert "u1_alias_pair" in purposes and "u1_route" in purposes
    assert "u3_hop1" in purposes and "u3_hop2" in purposes
    assert "u4_audit" not in purposes
    assert outcome.status == "COMPLETED"
    assert outcome.coverage["v3_options"] == {"gray_u4": False, "mid_single_hop": False}
    assert outcome.resolved_chapters["fs_hop"] == 13
    assert outcome.audit_routes["fs_hop"]["hops_used"] == 2
    assert it.status == "resolved"
    assert it.resolved_chapter == 13


def test_run_recheck_v3_midflight_should_stop() -> None:
    """运行中途触发 should_stop 时必须立即停机返回 STOPPED，不误改未核对条目。"""
    chapters = {
        1: "第一章\n主角捡到神秘玉珏。",
        2: "第二章\n玉珏在月光下泛出微光。",
        3: "第三章\n众人来到城门外。",
    }
    it = _make_item("fs_stop", anchor_quote="主角捡到神秘玉珏。", anchor_chapter=1)
    stop_flag = False

    async def _stopping_model(prompt: str) -> str:
        nonlocal stop_flag
        if "待拆解条目：" in prompt:
            stop_flag = True
            return json.dumps([{"fs_id": "fs_stop", "core_question": "玉珏来历", "key_entities": ["玉珏"]}])
        return "[]"

    outcome = asyncio.run(
        run_recheck_v3(
            [it],
            chapters=chapters,
            call_model=_stopping_model,
            should_stop=lambda: stop_flag,
        )
    )
    assert outcome.status == STOPPED
    assert "停止" in outcome.status_reason
    assert "fs_stop" in outcome.not_checked
    assert it.status == "active"


async def test_final_summary_recheck_version_routing_and_progress(tmp_path: Path) -> None:
    """FinalSummaryRunner 默认路由至 V3，显式配 v2 时路由至 V2；配置默认与进度文案契约。"""
    assert AppConfig().analysis.recheck_version == "v3"
    assert AppConfig().analysis.recheck_max_calls == 1200

    assert "双向路由索引" in _recheck_progress_message({"stage": "u1_index"})
    assert "第 2/3 跳" in _recheck_progress_message({"stage": "u3_hop", "hop": 2})
    assert "终审" in _recheck_progress_message({"stage": "u4_audit"})
    assert "修复" in _recheck_progress_message({"stage": "u5_repair"})

    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True)
    blocks_dir = tmp_path / "blocks"
    blocks_dir.mkdir(parents=True)
    (blocks_dir / "0001.txt").write_text("第一章 起\n正文测试内容。\n", encoding="utf-8")
    (out_dir / "chapter_1_result.json").write_text(
        json.dumps(
            {
                "chapter_number": 1,
                "cross_block": {"summary": "第1章摘要内容", "unresolved_questions": [], "new_leads": []},
                "core_events": [],
                "character_arcs": [],
                "foreshadowing": [],
                "plot_holes": [],
                "long_context_insights": {},
                "updated_knowledge": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    for version, exp_v3, exp_v2 in [("v3", 1, 0), ("v2", 0, 1)]:
        run_out = tmp_path / f"run_{version}"
        run_out.mkdir(parents=True, exist_ok=True)
        (run_out / "chapter_1_result.json").write_text(
            (out_dir / "chapter_1_result.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        cfg = AppConfig()
        cfg.working_directory = str(tmp_path)
        cfg.api.api_key = "test-key"
        cfg.api.model = "test-model"
        cfg.analysis.recheck_version = version

        runner = FinalSummaryRunner(
            config=cfg,
            output_dir=run_out,
            start_chapter=1,
            end_chapter=1,
            batch_size=1,
            concurrency=1,
        )
        with (
            patch.object(runner, "_call_llm_summary", new=AsyncMock(return_value="卷摘要正文")),
            patch.object(runner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})),
            patch.object(runner, "_run_style_extraction", new=AsyncMock(return_value={})),
            patch.object(runner, "_call_llm_final", new=AsyncMock(return_value="最终报告内容")),
            patch.object(runner, "_run_recheck_v3", new_callable=AsyncMock) as mock_v3,
            patch.object(runner, "_run_recheck_v2", new_callable=AsyncMock) as mock_v2,
        ):
            report = await runner.run()
            assert report == "最终报告内容"
            assert mock_v3.await_count == exp_v3
            assert mock_v2.await_count == exp_v2


def test_recheck_follows_main_api_when_blank_and_applies_reasoning_effort(tmp_path: Path) -> None:
    """recheck_* 全空时严格跟随主 api.*（非 summary_model），recheck_concurrency=0 跟随 analysis.concurrency，reasoning_effort 穿透 thinking_mode。"""
    from backend.config.settings import AnalysisConfig, APIConfig

    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True)

    # 1. 全空回退：即使配了 summary_model/summary_thinking_mode，recheck 仍严格跟随主 api.model & json_mode="default"
    cfg_blank = AppConfig(
        api=APIConfig(
            base_url="https://main.example/v1",
            api_key="main-key",
            model="main-model",
            summary_model="heavy-summary-model",
            json_mode="strict",
            thinking_mode={"reasoning_effort": "high"},
            summary_thinking_mode={"reasoning_effort": "low"},
        ),
        analysis=AnalysisConfig(
            concurrency=9,
            summary_concurrency=0,
            recheck_concurrency=0,
            recheck_version="v3",
        ),
        working_directory=str(tmp_path),
    )
    runner_blank = FinalSummaryRunner(config=cfg_blank, output_dir=out_dir, start_chapter=1, end_chapter=1)
    assert runner_blank.concurrency == 9
    prep = runner_blank._prepare_recheck_context(contract_version="recheck_v3")
    assert prep is not None
    recheck_client, overrides, *_ = prep
    assert overrides == {}
    assert recheck_client.config.model == "main-model"
    assert recheck_client.config.json_mode == "default"
    assert recheck_client.config.thinking_mode == {"reasoning_effort": "high"}

    # 2. 独立 recheck_reasoning_effort 不被主 api.thinking_mode 吞掉，同步覆盖 thinking_mode 与 reasoning_effort
    cfg_effort = AppConfig(
        api=APIConfig(
            base_url="https://main.example/v1",
            api_key="main-key",
            model="main-model",
            thinking_mode={"reasoning_effort": "high"},
        ),
        analysis=AnalysisConfig(
            concurrency=5,
            recheck_concurrency=0,
            recheck_reasoning_effort="low",
        ),
        working_directory=str(tmp_path),
    )
    runner_effort = FinalSummaryRunner(config=cfg_effort, output_dir=out_dir, start_chapter=1, end_chapter=1)
    prep_effort = runner_effort._prepare_recheck_context(contract_version="recheck_v3")
    assert prep_effort is not None
    recheck_client2, overrides2, *_ = prep_effort
    assert overrides2["reasoning_effort"] == "low"
    assert overrides2["thinking_mode"] == {"reasoning_effort": "low"}
    assert recheck_client2.config.thinking_mode == {"reasoning_effort": "low"}
    assert recheck_client2.config.reasoning_effort == "low"


async def test_recheck_v3_and_v2_concurrency_zero_follows_analysis_concurrency(tmp_path: Path) -> None:
    """V3 与 V2 在 recheck_concurrency=0 时均严格回退到 analysis.concurrency。"""
    from unittest.mock import patch as _patch
    from backend.config.settings import AnalysisConfig, APIConfig
    from backend.services.recheck_v2 import RecheckOutcome

    blocks_dir = tmp_path / "blocks"
    out_dir = tmp_path / "output"
    blocks_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    (blocks_dir / "0001.txt").write_text("第一章 起\n正文内容。\n", encoding="utf-8")

    seen_v3_conc: list[int] = []
    seen_v2_conc: list[int] = []

    async def fake_v3(*args, **kwargs):
        seen_v3_conc.append(kwargs.get("concurrency", -1))
        return RecheckOutcome(calls=0, status="COMPLETED", not_checked=[], coverage={"checked": 0, "candidates_total": 0})

    async def fake_v2(*args, **kwargs):
        seen_v2_conc.append(kwargs.get("concurrency", -1))
        return RecheckOutcome(calls=0, status="COMPLETED", not_checked=[], coverage={"checked": 0, "candidates_total": 0})

    for ver in ("v3", "v2"):
        cfg = AppConfig(
            api=APIConfig(base_url="https://api.example/v1", api_key="k", model="m"),
            analysis=AnalysisConfig(concurrency=7, recheck_concurrency=0, recheck_version=ver),
            working_directory=str(tmp_path),
        )
        runner = FinalSummaryRunner(config=cfg, output_dir=out_dir, start_chapter=1, end_chapter=1)
        if ver == "v3":
            with _patch("backend.services.recheck_v3.run_recheck_v3", new=fake_v3):
                await runner._run_recheck_v3([], volume_summaries=[])
        else:
            with _patch("backend.services.recheck_v2.run_recheck_v2", new=fake_v2):
                await runner._run_recheck_v2([])

    assert seen_v3_conc == [7]
    assert seen_v2_conc == [7]


def test_run_recheck_v3_emits_incremental_stage_counts_and_messages() -> None:
    """run_recheck_v3 在各子阶段实时递增 stage_done / remaining，且进度文案完整覆盖 U1 子阶段。"""
    assert "问题拆解" in _recheck_progress_message({"stage": "u1_split", "stage_done": 1, "stage_total": 3, "remaining": 2, "calls": 1})
    assert "别名词典" in _recheck_progress_message({"stage": "u1_alias", "stage_done": 1, "stage_total": 2, "remaining": 1, "unit": "批", "calls": 2})
    assert "双向路由" in _recheck_progress_message({"stage": "u1_route", "stage_done": 2, "stage_total": 5, "remaining": 3, "unit": "批", "calls": 3})
    assert "剩余 2 条" in _recheck_progress_message({"stage": "u3_hop", "hop": 1, "stage_done": 3, "stage_total": 5, "remaining": 2, "calls": 4})

    chapters = {i: f"第{i}章 日常\n众人沿着官道赶路。" for i in range(1, 15)}
    chapters[1] = "第一章 起因\n夜市摊主递给沈砚半块青纹残玉，叮嘱切莫示人。"
    chapters[2] = "第二章 线索\n店小二提起墨玉令牌。"
    chapters[13] = "第十三章 揭晓\n昔年皇室印玺碎片原是太子流落民间的信物，店小二的墨玉令牌也在东宫库房寻回。"
    labels = {i: f"第 {i} 回" for i in range(1, 15)}

    it1 = _make_item(
        "fs_p1",
        desc="夜市残玉来历",
        question="夜市残玉到底是什么？",
        anchor_quote="夜市摊主递给沈砚半块青纹残玉，叮嘱切莫示人。",
        anchor_chapter=1,
    )
    it2 = _make_item(
        "fs_p2",
        desc="墨玉令牌下落",
        question="墨玉令牌流落何处？",
        anchor_quote="店小二提起墨玉令牌。",
        anchor_chapter=2,
    )
    emitted: list[dict] = []
    confidence_val = "high"

    async def _model(prompt: str) -> str:
        if "待拆解条目：" in prompt:
            return json.dumps([
                {"fs_id": "fs_p1", "core_question": "残玉是什么", "bonus_aspects": "", "setup_event_anchor": "获赠残玉", "needs_pre_setup_backref": False, "key_entities": ["残玉"]},
                {"fs_id": "fs_p2", "core_question": "令牌下落", "bonus_aspects": "", "setup_event_anchor": "提起令牌", "needs_pre_setup_backref": False, "key_entities": ["令牌"]},
            ], ensure_ascii=False)
        if "只提取高确信度的" in prompt or "待检索伏笔（共" in prompt:
            return "[]"
        if "你是零人工长篇小说伏笔深度核查侦探" in prompt:
            rows = []
            if "fs_p1" in prompt:
                rows.append({
                    "fs_id": "fs_p1",
                    "verdict": "RESOLVED_EXPLICIT",
                    "unresolved_type": "",
                    "confidence": confidence_val,
                    "answer": "是皇室印玺碎片。",
                    "evidence_chain": [{"unit_id": 13, "quote": "昔年皇室印玺碎片原是太子流落民间的信物", "role": "payoff"}],
                    "follow_up": None,
                })
            if "fs_p2" in prompt:
                rows.append({
                    "fs_id": "fs_p2",
                    "verdict": "RESOLVED_EXPLICIT",
                    "unresolved_type": "",
                    "confidence": confidence_val,
                    "answer": "在东宫库房寻回。",
                    "evidence_chain": [{"unit_id": 13, "quote": "店小二的墨玉令牌也在东宫库房寻回。", "role": "payoff"}],
                    "follow_up": None,
                })
            return json.dumps(rows, ensure_ascii=False)
        if "你是长篇小说终审总编辑" in prompt:
            rows = []
            if "fs_p1" in prompt:
                rows.append({
                    "fs_id": "fs_p1",
                    "verdict": "RESOLVED_EXPLICIT",
                    "unresolved_type": "",
                    "answer": "是皇室印玺碎片。",
                    "evidence_chain": [{"unit_id": 13, "quote": "昔年皇室印玺碎片原是太子流落民间的信物", "role": "payoff"}],
                })
            if "fs_p2" in prompt:
                rows.append({
                    "fs_id": "fs_p2",
                    "verdict": "RESOLVED_EXPLICIT",
                    "unresolved_type": "",
                    "answer": "在东宫库房寻回。",
                    "evidence_chain": [{"unit_id": 13, "quote": "店小二的墨玉令牌也在东宫库房寻回。", "role": "payoff"}],
                })
            return json.dumps(rows, ensure_ascii=False)
        return "[]"

    # 默认 enable_gray_u4=False：不发出 u4_audit，U1/U3 各阶段实时递增归零
    outcome = asyncio.run(
        run_recheck_v3(
            [it1, it2],
            chapters=chapters,
            call_model=_model,
            display_labels=labels,
            unit_noun="文件",
            batch_size=1,
            concurrency=2,
            emit=emitted.append,
        )
    )
    assert outcome.status == "COMPLETED"
    assert [e for e in emitted if e.get("stage") == "u4_audit"] == []

    for stage_name in ("u1_split", "u1_alias", "u1_route", "u3_hop"):
        events = [e for e in emitted if e.get("stage") == stage_name]
        assert events, f"缺少阶段 {stage_name} 的进度事件"
        assert all(e.get("items_total") == 2 for e in events)
        assert any(e.get("stage_done", 0) > 0 for e in events), f"{stage_name} 未递增 stage_done: {events}"
        assert events[-1]["stage_done"] == events[-1]["stage_total"]
        assert events[-1]["remaining"] == 0

    # 开启 enable_gray_u4=True 且置信度为 medium 时：触发 u4_audit 递增与归零
    for it in (it1, it2):
        it.status = "active"
        it.resolution = None
        it.resolved_chapter = None
    emitted.clear()
    confidence_val = "medium"
    outcome_u4 = asyncio.run(
        run_recheck_v3(
            [it1, it2],
            chapters=chapters,
            call_model=_model,
            display_labels=labels,
            unit_noun="文件",
            batch_size=1,
            concurrency=2,
            enable_gray_u4=True,
            emit=emitted.append,
        )
    )
    assert outcome_u4.status == "COMPLETED"
    u4_events = [e for e in emitted if e.get("stage") == "u4_audit"]
    assert u4_events
    assert any(e.get("stage_done", 0) > 0 for e in u4_events)
    assert u4_events[-1]["stage_done"] == u4_events[-1]["stage_total"]
    assert u4_events[-1]["remaining"] == 0


def test_select_primary_evidence_prefers_post_setup_payoff() -> None:
    """证据链同时含设伏前因果回指与设伏后兑现时，resolved_chapter 必须优先锚定设伏后兑现单元。"""
    labels = {i: f"第 {i} 回" for i in range(1, 25)}
    chain_mixed = [
        {"unit_id": 3, "offset": 5, "quote": "早年埋下的铜符因果", "role": "causal_backref"},
        {"unit_id": 18, "offset": 12, "quote": "最终揭晓铜符真相", "role": "payoff"},
    ]
    best = _select_primary_evidence(chain_mixed, setup_unit=10)
    assert best is not None and int(best["unit_id"]) == 18

    # 即使没有 explicit causal_backref，也会选取 earliest post-setup payoff，仅全为 pre-setup 时才回退
    chain_only_pre = [{"unit_id": 3, "offset": 5, "quote": "全为前置因果", "role": "causal_backref"}]
    assert int(_select_primary_evidence(chain_only_pre, setup_unit=10)["unit_id"]) == 3

    it_mixed = _make_item("fs_mix", anchor_chapter=10, first_seen_chapter=10)
    apply_v3_verdict_to_item(
        it_mixed,
        tier="RESOLVED_EXPLICIT",
        unresolved_type="",
        answer="第18回揭晓真相。",
        evidence_chain=chain_mixed,
        display_labels=labels,
        setup_unit=10,
    )
    assert it_mixed.resolved_chapter == 18

    # 端到端 run_recheck_v3 也按 post-setup 兑现章落账 RecheckOutcome
    chapters = {
        3: "第三章 前因\n当年老道留下一张暗红色符箓图谱。",
        10: "第十章 设伏\n沈砚认出那枚古旧铜符与图谱呼应，究竟何意？",
        18: "第十八章 兑现\n石碑裂开后露出真相，铜符乃是启动地宫枢机的钥匙。",
    }
    it_e2e = _make_item(
        "fs_pin",
        desc="古旧铜符的真实用途",
        question="古旧铜符究竟有何用途？",
        anchor_quote="沈砚认出那枚古旧铜符与图谱呼应",
        anchor_chapter=10,
        first_seen_chapter=10,
    )

    async def _model(prompt: str) -> str:
        if "待拆解条目：" in prompt:
            return json.dumps([{
                "fs_id": "fs_pin",
                "core_question": "铜符有何用途",
                "bonus_aspects": "",
                "setup_event_anchor": "认出铜符与图谱呼应",
                "needs_pre_setup_backref": True,
                "key_entities": ["铜符"],
            }], ensure_ascii=False)
        if "你是零人工长篇小说伏笔深度核查侦探" in prompt:
            return json.dumps([{
                "fs_id": "fs_pin",
                "verdict": "RESOLVED_EXPLICIT",
                "unresolved_type": "",
                "confidence": "high",
                "answer": "前因埋于第3章，第18章证实为地宫枢机钥匙。",
                "evidence_chain": [
                    {"unit_id": 3, "quote": "当年老道留下一张暗红色符箓图谱。", "role": "causal_backref"},
                    {"unit_id": 18, "quote": "铜符乃是启动地宫枢机的钥匙。", "role": "payoff"},
                ],
                "follow_up": None,
            }], ensure_ascii=False)
        return "[]"

    outcome = asyncio.run(
        run_recheck_v3([it_e2e], chapters=chapters, call_model=_model, display_labels=labels)
    )
    assert outcome.resolved_units["fs_pin"] == 18
    assert outcome.resolved_chapters["fs_pin"] == 18
    assert it_e2e.resolved_chapter == 18


def test_run_recheck_v3_shared_pool_dedup_and_cross_pool_inheritance() -> None:
    """共享证据池对同批共现单元只输出一次原文，并允许跨题继受同池章节通过 U5。"""
    chapters = {i: f"第{i}章 过渡\n平静赶路。" for i in range(1, 16)}
    chapters[1] = "第一章 起因\n主角拿到铁匣。"
    chapters[2] = "第二章 线索\n主角听到寒钟。"
    chapters[10] = "第十章 对质\n密室之中同时揭出铁匣秘密，并与寒钟线索相连。"
    chapters[12] = "第十二章 余响\n密诏揭晓：铁匣内藏另一信物，实乃先帝亲赐紫金铃。"
    labels = {i: f"第 {i} 回" for i in range(1, 16)}

    it_a = _make_item("fs_a", desc="铁匣秘密", question="铁匣藏着什么？", anchor_quote="主角拿到铁匣。", anchor_chapter=1, entity_key="铁匣")
    it_b = _make_item("fs_b", desc="寒钟来历", question="寒钟是什么旧器？", anchor_quote="主角听到寒钟。", anchor_chapter=2, entity_key="寒钟")
    u3_prompts: list[str] = []

    async def _model(prompt: str) -> str:
        if "待拆解条目：" in prompt:
            return json.dumps([
                {"fs_id": "fs_a", "core_question": "铁匣藏着什么", "bonus_aspects": "", "setup_event_anchor": "拿到铁匣", "needs_pre_setup_backref": False, "key_entities": ["铁匣"]},
                {"fs_id": "fs_b", "core_question": "寒钟是什么", "bonus_aspects": "", "setup_event_anchor": "听到寒钟", "needs_pre_setup_backref": False, "key_entities": ["寒钟"]},
            ], ensure_ascii=False)
        if "只提取高确信度的" in prompt or "待检索伏笔（共" in prompt:
            return json.dumps([
                {"fs_id": "fs_a", "units": [10, 12], "clue": "揭出铁匣与第12章密诏"},
                {"fs_id": "fs_b", "units": [10], "clue": "寒钟线索"},
            ], ensure_ascii=False) if "待检索伏笔（共" in prompt else "[]"
        if "你是零人工长篇小说伏笔深度核查侦探" in prompt:
            u3_prompts.append(prompt)
            return json.dumps([
                {
                    "fs_id": "fs_a",
                    "verdict": "RESOLVED_EXPLICIT",
                    "unresolved_type": "",
                    "confidence": "high",
                    "answer": "密室揭出铁匣秘密。",
                    "evidence_chain": [{"unit_id": 10, "quote": "密室之中同时揭出铁匣秘密", "role": "payoff"}],
                    "follow_up": None,
                },
                {
                    "fs_id": "fs_b",
                    "verdict": "RESOLVED_IMPLICIT",
                    "unresolved_type": "",
                    "confidence": "high",
                    "answer": "在共享池第12章密诏证实为先帝亲赐紫金铃。",
                    "evidence_chain": [{"unit_id": 12, "quote": "实乃先帝亲赐紫金铃。", "role": "payoff"}],
                    "follow_up": None,
                },
            ], ensure_ascii=False)
        return "[]"

    outcome = asyncio.run(
        run_recheck_v3([it_a, it_b], chapters=chapters, call_model=_model, display_labels=labels, batch_size=8)
    )
    assert len(u3_prompts) == 1
    assert u3_prompts[0].count("ev_id=U0010") == 1
    assert u3_prompts[0].count("ev_id=U0012") == 1
    assert outcome.status == "COMPLETED"
    assert outcome.applied["fs_a"] == AUTO_RESOLVED
    assert outcome.applied["fs_b"] == AUTO_RESOLVED
    assert outcome.resolved_units["fs_b"] == 12
    assert 12 in outcome.audit_routes["fs_b"]["selected_units"]
    assert outcome.audit_routes["fs_a"]["repaired"] is False
    assert outcome.audit_routes["fs_b"]["repaired"] is False


def test_run_recheck_v3_gray_u4_partition_and_auto_accept() -> None:
    """enable_gray_u4=True 时，Hop1 高置信显式回收自动放行，仅灰区条目进入 U4。"""
    chapters = {
        1: "第一章\n主角拾得玄铁匕首。",
        2: "第二章\n暗处传出神秘哨声。",
        5: "第五章\n铁匠认出玄铁匕首是北疆镇军佩刀；神秘哨声则疑似巡夜暗号。",
    }
    labels = {i: f"第 {i} 回" for i in range(1, 6)}
    it_hi = _make_item("fs_hi", desc="匕首来历", question="匕首来历如何？", anchor_quote="主角拾得玄铁匕首。", anchor_chapter=1)
    it_gray = _make_item("fs_gray", desc="神秘哨声", question="哨声代表何意？", anchor_quote="暗处传出神秘哨声。", anchor_chapter=2)
    u4_prompts: list[str] = []

    async def _model(prompt: str) -> str:
        if "待拆解条目：" in prompt:
            return json.dumps([
                {"fs_id": "fs_hi", "core_question": "匕首来历", "bonus_aspects": "", "setup_event_anchor": "拾得匕首", "needs_pre_setup_backref": False, "key_entities": ["匕首"]},
                {"fs_id": "fs_gray", "core_question": "哨声含义", "bonus_aspects": "", "setup_event_anchor": "听到哨声", "needs_pre_setup_backref": False, "key_entities": ["哨声"]},
            ], ensure_ascii=False)
        if "你是零人工长篇小说伏笔深度核查侦探" in prompt:
            return json.dumps([
                {
                    "fs_id": "fs_hi",
                    "verdict": "RESOLVED_EXPLICIT",
                    "unresolved_type": "",
                    "confidence": "high",
                    "answer": "是北疆镇军佩刀。",
                    "evidence_chain": [{"unit_id": 5, "quote": "玄铁匕首是北疆镇军佩刀", "role": "payoff"}],
                    "follow_up": None,
                },
                {
                    "fs_id": "fs_gray",
                    "verdict": "RESOLVED_IMPLICIT",
                    "unresolved_type": "",
                    "confidence": "medium",
                    "answer": "疑似巡夜暗号。",
                    "evidence_chain": [{"unit_id": 5, "quote": "神秘哨声则疑似巡夜暗号。", "role": "payoff"}],
                    "follow_up": None,
                },
            ], ensure_ascii=False)
        if "你是长篇小说终审总编辑" in prompt:
            u4_prompts.append(prompt)
            return json.dumps([{
                "fs_id": "fs_gray",
                "verdict": "UNRESOLVED",
                "unresolved_type": "UNFILLED_PIT",
                "answer": "仅推测疑似，尚未实证。",
                "evidence_chain": [],
            }], ensure_ascii=False)
        return "[]"

    outcome = asyncio.run(
        run_recheck_v3([it_hi, it_gray], chapters=chapters, call_model=_model, display_labels=labels, enable_gray_u4=True)
    )
    assert len(u4_prompts) == 1
    assert "fs_hi" not in u4_prompts[0]
    assert "fs_gray" in u4_prompts[0]
    assert outcome.applied["fs_hi"] == AUTO_RESOLVED
    assert outcome.applied["fs_gray"] == OPEN


def test_run_recheck_v3_mid_single_hop_and_strict_backref_gating() -> None:
    """mid_single_hop=True 保留高重要度多跳而跳过中重要度二三跳；needs_backref=False 严格屏蔽前驱窗。"""
    chapters = {i: f"第{i}章 平常\n众人沿途赶路。" for i in range(1, 16)}
    chapters[2] = "第二章 旧事\n昔日太虚门规提及青铜古镜不可擅动。"
    chapters[5] = "第五章 设伏\n沈砚得到一面刻有星纹的青铜古镜，同时客栈掌柜提及暗语。"
    chapters[14] = "第十四章 揭秘\n宗门长老当众说明那玄鸟宝匣本是历代掌门亲传令牌。"
    labels = {i: f"第 {i} 回" for i in range(1, 16)}

    it_high = _make_item("fs_high", desc="青铜古镜来历", question="这面青铜古镜究竟是什么旧物？", anchor_quote="沈砚得到一面刻有星纹的青铜古镜", anchor_chapter=5, first_seen_chapter=5)
    it_high.importance = "高"
    it_mid = _make_item("fs_mid", desc="客栈暗语", question="掌柜暗语何意？", anchor_quote="客栈掌柜提及暗语", anchor_chapter=5, first_seen_chapter=5)
    it_mid.importance = "中"
    hop2_prompts: list[str] = []

    async def _model(prompt: str) -> str:
        if "待拆解条目：" in prompt:
            return json.dumps([
                {"fs_id": "fs_high", "core_question": "古镜来历", "bonus_aspects": "", "setup_event_anchor": "获青铜古镜", "needs_pre_setup_backref": False, "key_entities": ["青铜古镜"]},
                {"fs_id": "fs_mid", "core_question": "暗语含义", "bonus_aspects": "", "setup_event_anchor": "提及暗语", "needs_pre_setup_backref": False, "key_entities": ["暗语"]},
            ], ensure_ascii=False)
        if "只提取高确信度的" in prompt or "待检索伏笔（共" in prompt:
            return "[]"
        if "你是零人工长篇小说伏笔深度核查侦探" in prompt:
            if "第 1/3 跳" in prompt:
                return json.dumps([
                    {
                        "fs_id": "fs_high",
                        "verdict": "UNRESOLVED",
                        "unresolved_type": "UNFILLED_PIT",
                        "confidence": "low",
                        "answer": "需追查第14章。",
                        "evidence_chain": [],
                        "follow_up": {"keyword_queries": ["玄鸟宝匣"], "unit_ranges": [[14, 14]], "reason": "查14章"},
                    },
                    {
                        "fs_id": "fs_mid",
                        "verdict": "UNRESOLVED",
                        "unresolved_type": "UNFILLED_PIT",
                        "confidence": "low",
                        "answer": "未解，若允许也会追查。",
                        "evidence_chain": [],
                        "follow_up": {"keyword_queries": ["玄鸟宝匣"], "unit_ranges": [[14, 14]], "reason": "支线查14章"},
                    },
                ], ensure_ascii=False)
            hop2_prompts.append(prompt)
            return json.dumps([{
                "fs_id": "fs_high",
                "verdict": "RESOLVED_EXPLICIT",
                "unresolved_type": "",
                "confidence": "high",
                "answer": "历代掌门亲传令牌。",
                "evidence_chain": [{"unit_id": 14, "quote": "那玄鸟宝匣本是历代掌门亲传令牌。", "role": "payoff"}],
                "follow_up": None,
            }], ensure_ascii=False)
        return "[]"

    outcome = asyncio.run(
        run_recheck_v3(
            [it_high, it_mid],
            chapters=chapters,
            call_model=_model,
            display_labels=labels,
            mid_single_hop=True,
        )
    )
    assert len(hop2_prompts) == 1
    assert "fs_high" in hop2_prompts[0]
    assert "fs_mid" not in hop2_prompts[0]
    assert outcome.audit_routes["fs_high"]["hops_used"] == 2
    assert outcome.audit_routes["fs_mid"]["hops_used"] == 1
    # needs_pre_setup_backref=False 时，即使第2章包含"青铜古镜"也不能进入 pre_selected_units
    assert outcome.audit_routes["fs_high"]["pre_selected_units"] == []
    assert outcome.applied["fs_high"] == AUTO_RESOLVED
    assert outcome.applied["fs_mid"] == OPEN


def test_salvage_json_array_rows_recovers_truncated_prefix() -> None:
    """输出被 max_tokens 截断时，应抢救出已闭合的前缀 JSON 对象。"""
    truncated = (
        '```json\n[\n'
        '  {"fs_id": "fs_1", "verdict": "RESOLVED_EXPLICIT", "answer": "已解"},\n'
        '  {"fs_id": "fs_2", "verdict": "UNRESOLVED", "answer": "花括号}测试"},\n'
        '  {"fs_id": "fs_3", "verdict": "RESO'
    )
    salvaged = _salvage_json_array_rows(truncated)
    assert set(salvaged.keys()) == {"fs_1", "fs_2"}
    assert salvaged["fs_2"]["answer"] == "花括号}测试"
