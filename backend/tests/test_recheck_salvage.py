"""伏笔打捞管线（run_salvage）的契约测试。

覆盖 PLAN-foreshadow-rescue §7 验收：白名单、块级归属校验、来源块进证据集、
audit 协议适配（v1 式响应按 semantic_reject 统计）、关闭条件（AND/OR/UNCERTAIN）、
落账规则（active→resolved、questioned 不改状态、notes 去重）、资格集含提名条目。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.run_salvage import (  # noqa: E402
    apply_to_shadow,
    closing_satisfied,
    layer_a_match,
    layer_b_verify,
    load_distilled_stream,
    load_questions,
    stratified_sample,
    validate_prereg_row,
)
from backend.utils.foreshadow_ledger import ForeshadowItem, ForeshadowLedger  # noqa: E402


def _item(fid: str, *, status="active", needs_review=True, question="问题？",
          criteria="解释跨越规则", description="背景描述", last_seen=100, **kw):
    base = dict(id=fid, description=description, first_seen_chapter=1, first_seen_batch=0,
                last_seen_chapter=last_seen, last_seen_batch=0,
                confidence=0.8, source_type="catalog_scan",
                question=question, answer_criteria=criteria,
                status=status, needs_review=needs_review, resolution_quality="")
    base.update(kw)
    return ForeshadowItem(**base)


def _prereg(closing="OR"):
    if closing == "OR":
        return {"branches": [{"branch_id": "c1", "text": "解释跨越规则"}],
                "closing": {"type": "OR", "closable": ["c1"]}}
    if closing == "AND":
        return {"branches": [{"branch_id": "c1", "text": "解释跨越规则"},
                             {"branch_id": "c2", "text": "揭示糖块来源"}],
                "closing": {"type": "AND"}}
    return {"branches": [], "closing": {"type": "UNCERTAIN"}}


def _candidate(fid="fs_001", block_id=1, quote="两界众生皆是同一真实存在的两重投影"):
    return {"fs_id": fid, "source_block_id": block_id, "source_chapter_range": [1, 4],
            "refs": [{"chapter": 3, "role": "resolution"}],
            "digest_quote": quote, "rationale": "机制解释", "confidence": "high"}


# ---------------------------------------------------------------- 资格集


def test_eligibility_is_active_and_needs_review_including_nominations():
    items = [
        _item("fs_001"),
        _item("fs_002", status="resolved", needs_review=False, resolution_quality="verified"),
        _item("fs_003", question="", criteria=""),                      # lead
        _item("fs_004", notes=["自述回收待证据裁决（批次3）：自报 ch9"]),  # 提名
        _item("fs_005", status="dormant"),
    ]
    eligible = [i for i in items
                if i.status == "active" and i.needs_review and (i.question or "").strip()]
    ids = {i.id for i in eligible}
    assert ids == {"fs_001", "fs_004"}  # resolved/lead/dormant 排除；提名保留


# ---------------------------------------------------------------- Layer A


@pytest.mark.asyncio
async def test_layer_a_rejects_bad_id_quote_and_block(tmp_path):
    stream = [{"block_id": 1, "chapter_range": (1, 4),
               "text": "[蒸馏块 1]\n跨线连接：第3章两界众生皆是同一真实存在的两重投影"}]
    questions = load_questions([_item("fs_001")], set())
    good = _candidate()
    bad_id = _candidate(fid="fs_999")
    bad_quote = _candidate(quote="这句根本不在蒸馏流里出现过的编话")
    calls = []

    async def call_model(prompt):
        calls.append(prompt)
        return json.dumps([good, bad_id, bad_quote])

    candidates, stats = await layer_a_match(call_model, questions, stream)
    assert stats["rejected_bad_id"] == 1
    assert stats["rejected_quote"] == 1
    assert [c["fs_id"] for c in candidates] == ["fs_001"]
    # 问题清单为稳定前缀：每个流分段调用都带全量问题（KV 缓存友好的前提）
    assert all("fs_001 | " in c for c in calls)


@pytest.mark.asyncio
async def test_layer_a_skips_uncertain_prereg(tmp_path):
    """预登记 UNCERTAIN 的条目不进 Layer A（转人工）。"""
    stream = [{"block_id": 1, "chapter_range": (1, 4), "text": "x"}]
    items = [_item("fs_001"), _item("fs_002")]
    questions = load_questions(items, set())
    questions = [q for q in questions if q["fs_id"] != "fs_002"]  # UNCERTAIN 剔除

    async def call_model(prompt):
        assert "fs_002" not in prompt
        return "[]"

    candidates, _ = await layer_a_match(call_model, questions, stream)
    assert candidates == []


# ---------------------------------------------------------------- Layer B


def _write_block(tmp_path: Path, ch: int, text: str) -> None:
    (tmp_path / "blocks").mkdir(exist_ok=True)
    (tmp_path / "blocks" / f"{ch:04d}.txt").write_text(text, encoding="utf-8")


RESOLUTION_TEXT = "第3章末尾，三身论被正面道出：两界众生皆是同一真实存在的两重投影，记忆与行为两边同步。"


@pytest.mark.asyncio
async def test_layer_b_verifies_with_source_block_evidence(tmp_path):
    """VERIFIED 全链：证据集含来源块原文；落账 resolved+verified+notes。"""
    _write_block(tmp_path, 1, "第1章 李火旺捡到糖块。")
    _write_block(tmp_path, 3, RESOLUTION_TEXT)
    item = _item("fs_001", last_seen=1)
    items_by_id = {"fs_001": item}
    cand = _candidate()
    prereg = {"fs_001": _prereg("OR")}

    captured = {}

    async def call_model(prompt):
        captured["prompt"] = prompt
        # 逐段解析 evidence_id → 段文本，取"三身论"所在段（引文必须落在它里）
        seg_map = {}
        for part in prompt.split("--- evidence_id=")[1:]:
            ev = part.split("（")[0].strip()
            seg_map[ev] = part
        target = next(ev for ev, s in seg_map.items() if "三身论被正面道出" in s)
        assert "三身论被正面道出" in prompt  # 来源块原文进了证据集
        return json.dumps({
            "candidate_id": "fs_001", "verdict": "VERIFIED", "reason_code": "supported",
            "answer": "三身论正面解释了两界同步机制",
            "supporting_quote": "两界众生皆是同一真实存在的两重投影",
            "evidence_id": target,
            "counter_search": {"performed": True, "checked_evidence_ids": [target], "counter_quotes": []},
            "branch_hits": [{"branch_id": "c1", "evidence_id": target}],
        })

    results, stats = await layer_b_verify(call_model, [cand], prereg, items_by_id,
                                          tmp_path / "blocks", concurrency=1)
    assert stats["verified"] == 1
    r = results[0]
    assert r["outcome"] == "verified"
    assert "三身论" in captured["prompt"]

    shadow = ForeshadowLedger()
    shadow.items = [item]
    actions = apply_to_shadow(shadow, results, items_by_id)
    assert actions["applied"] == 1
    assert item.status == "resolved"
    assert item.resolution_quality == "verified"
    assert item.needs_review is False
    assert any(n.startswith("打捞：") for n in item.notes)


@pytest.mark.asyncio
async def test_layer_b_v1_style_response_is_semantic_reject(tmp_path):
    """v1 式 SUPPORTED/quote 响应：schema 拒绝 → QUESTIONED（semantic_rejects，不是 format）。"""
    _write_block(tmp_path, 1, RESOLUTION_TEXT)
    item = _item("fs_001")
    cand = _candidate()
    prereg = {"fs_001": _prereg("OR")}

    async def call_model(prompt):
        ev_id = prompt.split("evidence_id=")[1].split("（")[0].strip()
        return json.dumps({"candidate_id": "fs_001", "verdict": "SUPPORTED",
                           "quote": RESOLUTION_TEXT[:20], "evidence_id": ev_id})

    results, stats = await layer_b_verify(call_model, [cand], prereg,
                                          {"fs_001": item}, tmp_path / "blocks", concurrency=1)
    assert stats["semantic_rejects"] == 1
    assert results[0]["outcome"] == "questioned"
    assert results[0]["record"].reason == "no_evidence"  # "supported" 被枚举校验剥掉


@pytest.mark.asyncio
async def test_layer_b_fabricated_quote_not_verified(tmp_path):
    """引文不在证据段里（伪造）→ QUESTIONED(no_evidence)，绝不 verified。"""
    _write_block(tmp_path, 1, RESOLUTION_TEXT)
    item = _item("fs_001")
    cand = _candidate()
    prereg = {"fs_001": _prereg("OR")}

    async def call_model(prompt):
        ev_id = prompt.split("evidence_id=")[1].split("（")[0].strip()
        return json.dumps({
            "candidate_id": "fs_001", "verdict": "VERIFIED", "reason_code": "supported",
            "answer": "编造的回答",
            "supporting_quote": "这句话根本不在任何证据段里出现过的引文",
            "evidence_id": ev_id,
            "counter_search": {"performed": True, "checked_evidence_ids": [ev_id], "counter_quotes": []},
            "branch_hits": [{"branch_id": "c1", "evidence_id": ev_id}],
        })

    results, stats = await layer_b_verify(call_model, [cand], prereg,
                                          {"fs_001": item}, tmp_path / "blocks", concurrency=1)
    assert stats["verified"] == 0
    assert results[0]["record"].reason == "no_evidence"


@pytest.mark.asyncio
async def test_layer_b_missing_branch_hits_goes_to_human(tmp_path):
    """VERIFIED 但 branch_hits 缺失（AND 型关不满）→ closing_unmet 转人工，不落 resolved。"""
    _write_block(tmp_path, 1, RESOLUTION_TEXT)
    item = _item("fs_001")
    cand = _candidate()
    prereg = {"fs_001": _prereg("AND")}

    async def call_model(prompt):
        ev_id = prompt.split("evidence_id=")[1].split("（")[0].strip()
        return json.dumps({
            "candidate_id": "fs_001", "verdict": "VERIFIED", "reason_code": "supported",
            "answer": "只回答了 c1 分支",
            "supporting_quote": "两界众生皆是同一真实存在的两重投影",
            "evidence_id": ev_id,
            "counter_search": {"performed": True, "checked_evidence_ids": [ev_id], "counter_quotes": []},
            "branch_hits": [{"branch_id": "c1", "evidence_id": ev_id}],  # AND 缺 c2
        })

    results, stats = await layer_b_verify(call_model, [cand], prereg,
                                          {"fs_001": item}, tmp_path / "blocks", concurrency=1)
    assert stats["verified"] == 0
    assert results[0]["outcome"] == "closing_unmet"
    # 落账：不改状态，只加 note
    shadow = ForeshadowLedger()
    shadow.items = [item]
    actions = apply_to_shadow(shadow, results, {"fs_001": item})
    assert actions["applied"] == 0 and actions["closing_unmet"] == 1
    assert item.status == "active" and item.needs_review is True


def test_closing_satisfied_rules():
    or_reg = {"branches": [{"branch_id": "c1"}, {"branch_id": "c2"}],
              "closing": {"type": "OR", "closable": ["c1", "c2"]}}
    and_reg = {"branches": [{"branch_id": "c1"}, {"branch_id": "c2"}],
               "closing": {"type": "AND"}}
    assert closing_satisfied(or_reg["closing"], [{"branch_id": "c2"}], or_reg) is True
    assert closing_satisfied(or_reg["closing"], [{"branch_id": "c3"}], or_reg) is False
    # AND 的 branch 全集来自 reg["branches"]（PREREG 的 closing 里没有 branches）
    assert closing_satisfied(and_reg["closing"],
                             [{"branch_id": "c1"}, {"branch_id": "c2"}], and_reg) is True
    assert closing_satisfied(and_reg["closing"], [{"branch_id": "c1"}], and_reg) is False
    assert closing_satisfied({"type": "UNCERTAIN"}, [], {"branches": [], "closing": {}}) is None


@pytest.mark.asyncio
async def test_layer_b_and_closing_satisfied_when_all_branches_hit(tmp_path):
    """AND 型两个分支都命中 → verified（v2 的 AND 恒假修复的回归）。"""
    _write_block(tmp_path, 1, RESOLUTION_TEXT + " 糖块本身来自上界祭品。")
    item = _item("fs_001")
    cand = _candidate()
    prereg = {"fs_001": _prereg("AND")}

    async def call_model(prompt):
        ev_id = prompt.split("evidence_id=")[1].split("（")[0].strip()
        return json.dumps({
            "candidate_id": "fs_001", "verdict": "VERIFIED", "reason_code": "supported",
            "answer": "两个分支均被回答",
            "supporting_quote": "两界众生皆是同一真实存在的两重投影",
            "evidence_id": ev_id,
            "counter_search": {"performed": True, "checked_evidence_ids": [ev_id], "counter_quotes": []},
            "branch_hits": [{"branch_id": "c1", "evidence_id": ev_id},
                            {"branch_id": "c2", "evidence_id": ev_id}],
        })

    results, stats = await layer_b_verify(call_model, [cand], prereg,
                                          {"fs_001": item}, tmp_path / "blocks", concurrency=1)
    assert stats["verified"] == 1
    assert results[0]["outcome"] == "verified"


@pytest.mark.asyncio
async def test_layer_b_branch_hits_missing_is_format_reject(tmp_path):
    """B7：VERIFIED 但 branch_hits 整个缺失 = schema 违规 → format_reject（不是 closing_unmet）。"""
    _write_block(tmp_path, 1, RESOLUTION_TEXT)
    item = _item("fs_001")
    prereg = {"fs_001": _prereg("OR")}

    async def call_model(prompt):
        ev_id = prompt.split("evidence_id=")[1].split("（")[0].strip()
        return json.dumps({
            "candidate_id": "fs_001", "verdict": "VERIFIED", "reason_code": "supported",
            "answer": "没写 branch_hits",
            "supporting_quote": "两界众生皆是同一真实存在的两重投影",
            "evidence_id": ev_id,
            "counter_search": {"performed": True, "checked_evidence_ids": [ev_id], "counter_quotes": []},
        })

    results, stats = await layer_b_verify(call_model, [_candidate()], prereg,
                                          {"fs_001": item}, tmp_path / "blocks", concurrency=1)
    assert stats["format_rejects"] == 1
    assert results[0]["outcome"] == "format_reject"


@pytest.mark.asyncio
async def test_resolved_chapter_comes_from_quote_segment(tmp_path):
    """P1-3 回归：引文落在指针窗段时 resolved_chapter 取该段章号，不是来源块首章。"""
    _write_block(tmp_path, 1, "第1章 李火旺捡到糖块。")
    _write_block(tmp_path, 3, RESOLUTION_TEXT)  # 引文在 ch3（来源块 range 1-4 的第二段）
    item = _item("fs_001")
    prereg = {"fs_001": _prereg("OR")}

    async def call_model(prompt):
        seg_map = {}
        for part in prompt.split("--- evidence_id=")[1:]:
            ev = part.split("（")[0].strip()
            seg_map[ev] = part
        target = next(ev for ev, s in seg_map.items() if "三身论被正面道出" in s)
        return json.dumps({
            "candidate_id": "fs_001", "verdict": "VERIFIED", "reason_code": "supported",
            "answer": "三身论解释",
            "supporting_quote": "两界众生皆是同一真实存在的两重投影",
            "evidence_id": target,
            "counter_search": {"performed": True, "checked_evidence_ids": [target], "counter_quotes": []},
            "branch_hits": [{"branch_id": "c1", "evidence_id": target}],
        })

    results, _ = await layer_b_verify(call_model, [_candidate()], prereg,
                                      {"fs_001": item}, tmp_path / "blocks", concurrency=1)
    assert results[0]["outcome"] == "verified"
    shadow = ForeshadowLedger()
    shadow.items = [item]
    apply_to_shadow(shadow, results, {"fs_001": item})
    assert item.resolved_chapter == 3  # 不是 1（来源块首章）


def test_da_protection_records_but_never_flips():
    """C6：DIFFERENT_ANSWER 条目 EVEN VERIFIED 也只加 note，不改 status。"""
    item = _item("fs_001")
    shadow = ForeshadowLedger()
    shadow.items = [item]
    results = [{"fs_id": "fs_001", "outcome": "verified",
                "record": type("R", (), {"reason": "supported", "answer": "x"})(),
                "chapter": 1007}]
    actions = apply_to_shadow(shadow, results, {"fs_001": item}, da_ids={"fs_001"})
    assert actions["applied"] == 0 and actions["da_protected"] == 1
    assert item.status == "active" and item.needs_review is True
    assert any("DIFFERENT_ANSWER" in n for n in item.notes)


# ---------------------------------------------------------------- 预登记后验校验


def test_prereg_rejects_fragment_branches():
    """碎分支（只抽主语/人物名）→ 校验拒绝，转 UNCERTAIN。

    实测缺陷：criteria"丹阳子或瓶女之爹对瓶碎事件的发现与追查"被拆成
    "丹阳子"/"瓶女之爹"/"对事件的发现与追查"，只命中人物就会误关闭。
    """
    criteria = "丹阳子或瓶女之爹对瓶碎事件的发现与追查"
    bad = {"branches": [{"branch_id": "c1", "text": "丹阳子"},
                        {"branch_id": "c2", "text": "瓶女之爹"},
                        {"branch_id": "c3", "text": "对瓶碎事件的发现与追查"}],
           "closing": {"type": "OR", "closable": ["c1", "c2", "c3"]}}
    ok, why = validate_prereg_row(bad, criteria)
    assert not ok and why.startswith("fragment_branch")

    good = {"branches": [{"branch_id": "c1", "text": "丹阳子对瓶碎事件的发现与追查"},
                         {"branch_id": "c2", "text": "瓶女之爹对瓶碎事件的发现与追查"}],
            "closing": {"type": "OR", "closable": ["c1", "c2"]}}
    ok2, why2 = validate_prereg_row(good, criteria)
    assert ok2, why2


def test_prereg_rejects_non_verbatim_and_bad_closable():
    criteria = "解释玉佩传送的机制"
    non_verbatim = {"branches": [{"branch_id": "c1", "text": "解释玉佩如何跨越两界机制"}],
                    "closing": {"type": "AND"}}
    assert validate_prereg_row(non_verbatim, criteria)[0] is False
    bad_or = {"branches": [{"branch_id": "c1", "text": "解释玉佩传送的机制"}],
              "closing": {"type": "OR", "closable": ["c9"]}}
    assert validate_prereg_row(bad_or, criteria)[0] is False
    uncertain = {"branches": [], "closing": {"type": "UNCERTAIN"}}
    assert validate_prereg_row(uncertain, criteria)[0] is True


# ---------------------------------------------------------------- 反证定位


@pytest.mark.asyncio
async def test_contradicted_requires_located_counter_quote(tmp_path):
    """反证引文必须能逐字定位；定位不到 → 降级 no_evidence（不许当已验证反证）。"""
    _write_block(tmp_path, 1, RESOLUTION_TEXT)
    item = _item("fs_001")
    prereg = {"fs_001": _prereg("OR")}

    async def call_model(prompt):
        ev_id = prompt.split("evidence_id=")[1].split("（")[0].strip()
        return json.dumps({
            "candidate_id": "fs_001", "verdict": "QUESTIONED", "reason_code": "contradicted",
            "answer": "该处只说还没有解释",
            "counter_search": {"performed": True, "checked_evidence_ids": [ev_id],
                               "counter_quotes": ["这句反证根本不在原文里出现过的"]},
        })

    results, stats = await layer_b_verify(call_model, [_candidate()], prereg,
                                          {"fs_001": item}, tmp_path / "blocks", concurrency=1)
    r = results[0]
    assert r["outcome"] == "questioned"
    assert r["reason_final"] == "no_evidence"        # 降级
    assert r["counter_quotes"][0]["located"] is False
    assert stats["contradiction_unlocated"] == 1


@pytest.mark.asyncio
async def test_contradicted_kept_when_quote_locates(tmp_path):
    _write_block(tmp_path, 1, RESOLUTION_TEXT)
    item = _item("fs_001")

    async def call_model(prompt):
        ev_id = prompt.split("evidence_id=")[1].split("（")[0].strip()
        return json.dumps({
            "candidate_id": "fs_001", "verdict": "QUESTIONED", "reason_code": "contradicted",
            "answer": "原文明确说尚未交代",
            "counter_search": {"performed": True, "checked_evidence_ids": [ev_id],
                               "counter_quotes": ["记忆与行为两边同步"]},
        })

    results, stats = await layer_b_verify(call_model, [_candidate()], {"fs_001": _prereg("OR")},
                                          {"fs_001": item}, tmp_path / "blocks", concurrency=1)
    assert results[0]["reason_final"] == "contradicted"
    assert results[0]["counter_quotes"][0]["located"] is True
    assert stats["contradicted"] == 1


# ---------------------------------------------------------------- 多候选归并


def test_multiple_verified_candidates_apply_once():
    """同一伏笔多候选都通过：只落账一次，其余计 duplicate_verified（不重复累加）。"""
    item = _item("fs_001")
    shadow = ForeshadowLedger()
    shadow.items = [item]
    rec = type("R", (), {"reason": "supported", "answer": "答案"})()
    results = [{"fs_id": "fs_001", "outcome": "verified", "record": rec, "chapter": 5},
               {"fs_id": "fs_001", "outcome": "verified", "record": rec, "chapter": 900}]
    actions = apply_to_shadow(shadow, results, {"fs_001": item})
    assert actions["applied"] == 1
    assert actions["duplicate_verified"] == 1
    assert item.resolved_chapter == 5  # 第一条通过的为准


# ---------------------------------------------------------------- 落账规则


@pytest.mark.asyncio
async def test_questioned_and_resolved_items_not_mutated(tmp_path):
    """questioned 只加 note 不改状态；已 verified 条目跳过；notes 去重（重跑不双写）。"""
    q_item = _item("fs_001")
    done_item = _item("fs_002", status="resolved", needs_review=False, resolution_quality="verified")
    shadow = ForeshadowLedger()
    shadow.items = [q_item, done_item]
    _write_block(tmp_path, 1, RESOLUTION_TEXT)

    async def call_model(prompt):
        return "这不是 JSON"  # → format_reject？不：unparseable → format_reject 不动账本
    results, stats = await layer_b_verify(call_model, [_candidate()],
                                          {"fs_001": _prereg("OR")},
                                          {"fs_001": q_item}, tmp_path / "blocks", concurrency=1)
    assert stats["format_rejects"] == 1
    assert q_item.status == "active"  # format_reject 不动账本

    # questioned 路径：只加 note；重跑去重
    q_results = [{"fs_id": "fs_001", "outcome": "questioned",
                  "record": type("R", (), {"reason": "no_evidence"})()}]
    apply_to_shadow(shadow, q_results, {"fs_001": q_item})
    apply_to_shadow(shadow, list(q_results), {"fs_001": q_item})  # 重跑
    assert q_item.status == "active" and q_item.needs_review is True
    assert sum(1 for n in q_item.notes if n.startswith("打捞：")) == 1  # 去重

    # 已 verified 条目跳过
    d_results = [{"fs_id": "fs_002", "outcome": "verified",
                  "record": type("R", (), {"reason": "supported", "answer": "x"})(),
                  "chapter": 3}]
    actions = apply_to_shadow(shadow, d_results, {"fs_002": done_item})
    assert actions["skipped"] == 1


def test_stratified_sample_respects_quota_and_seed():
    items = [_item(f"fs_{i:03d}", last_seen=(i % 1000)) for i in range(100, 160)]
    a = stratified_sample(items, 10, seed=42)
    b = stratified_sample(items, 10, seed=42)
    assert a == b  # 种子可复现
    assert len(a) == 10 and len(set(a)) == 10


def test_distilled_stream_load(tmp_path):
    d = tmp_path / "output"
    d.mkdir()
    (d / "chapter_1_result.json").write_text(json.dumps({
        "chapter_number": 1, "block_size": 4,
        "cross_block": {"contextual_link": "兑现第1章糖块伏笔", "new_leads": ["线索A"],
                        "unresolved_questions": [], "summary": "s"},
        "updated_knowledge": {"timeline": "t", "world_building": ["三身论"]},
    }, ensure_ascii=False), encoding="utf-8")
    stream = load_distilled_stream(d)
    assert len(stream) == 1
    assert stream[0]["chapter_range"] == (1, 4)
    assert "兑现第1章糖块伏笔" in stream[0]["text"]
    assert "三身论" in stream[0]["text"]
