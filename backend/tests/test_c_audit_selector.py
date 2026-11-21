"""T4：C 类按需审核选择器（select_audit_route）测试。

验证：
1. 默认关闭（enabled=False）时一律走 current_only，不触发 claims_v2；
2. 简单单句事实回答 + OR/简单条件 + 无软信号 + 无模态风险 -> 走 current_only（节省 claims_v2 调用）；
3. 多断言扩写、AND 合取/排他条件、梦境/比方/未兑现阶段词、机械软信号 -> 触发 claims_v2_on_demand；
4. 硬机械缺陷（quote_equals_anchor）直接路由到 hard_reject_mechanical，不浪费模型调用。
"""

from __future__ import annotations

from backend.common.c_audit_selector import select_audit_route


def test_disabled_by_default_routes_to_current_only():
    item = {
        "candidate_id": "fs_155",
        "question": "反向传送是否产生结果？李医生动机是什么？",
        "answer_criteria": "交代尝试行为以及李医生真实动机",
        "answer": "李火旺决定尝试反向传送，同时证实产生了两块玉佩，虽然李医生动机尚未交代。",
        "supporting_quote": "自己刚刚的一切都是在做梦",
    }
    dec = select_audit_route(item, enabled=False)
    assert dec.trigger_claims_v2 is False
    assert dec.route == "current_only"


def test_simple_supported_item_skips_claims_v2():
    item = {
        "candidate_id": "fs_014",
        "question": "丹阳子是否真的不认识字？",
        "answer_criteria": "确认丹阳子识字与否",
        "answer": "第14章明确交代丹阳子确实不认识字。",
        "supporting_quote": "丹阳子确实不认识字，并且还非常非常排斥别人认字。",
        "anchor_quote": "丹阳子盯着竹简半晌不语",
        "anchor_chapter": 2,
        "chapter": 14,
    }
    dec = select_audit_route(item, enabled=True)
    assert dec.trigger_claims_v2 is False
    assert dec.route == "current_only"
    assert dec.trigger_reasons == []


def test_dream_or_metaphor_cue_triggers_claims_v2():
    item = {
        "candidate_id": "fs_017",
        "question": "两个世界如何相互影响？",
        "answer_criteria": "揭示两界交互机制",
        "answer": "赵霜点指出平行世界可以相互影响。",
        "supporting_quote": "在他的那个角度，平行世界是可以相互影响的！这是打个比方。",
        "anchor_quote": "李火旺分不清哪边才是真的",
        "anchor_chapter": 1,
        "chapter": 1044,
    }
    dec = select_audit_route(item, enabled=True)
    assert dec.trigger_claims_v2 is True
    assert dec.route == "claims_v2_on_demand"
    assert any("modality_or_stage_cue" in r for r in dec.trigger_reasons)


def test_conjunction_or_exclusivity_triggers_claims_v2():
    item = {
        "candidate_id": "fs_655",
        "question": "这只眼睛属于哪位司命？计划目的是什么？",
        "answer_criteria": "交代归属司命、来历，以及真实目的",
        "answer": "第466章交代计划是为了对抗天狗食日的天灾。",
        "supporting_quote": "是为了对抗着这天狗食日的天灾",
        "anchor_quote": "已经死去上千年司命的一只眼睛",
        "anchor_chapter": 437,
        "chapter": 466,
    }
    dec = select_audit_route(item, enabled=True)
    assert dec.trigger_claims_v2 is True
    assert dec.route == "claims_v2_on_demand"
    assert "criteria_conjunction_or_exclusivity" in dec.trigger_reasons


def test_hard_mechanical_defect_short_circuits_without_v2_call():
    item = {
        "candidate_id": "fs_384",
        "question": "铜匣里有什么？",
        "answer_criteria": "交代铜匣内物",
        "answer": "铜匣底下那层是空的。",
        "supporting_quote": "铜匣底下那层是空的",
        "anchor_quote": "方醒打开铜匣，铜匣底下那层是空的。",
        "anchor_chapter": 1,
        "chapter": 10,
    }
    dec = select_audit_route(item, enabled=True)
    assert dec.trigger_claims_v2 is False
    assert dec.route == "hard_reject_mechanical"
    assert "quote_equals_anchor" in dec.hard_signals


def test_imperative_directive_in_quote_triggers_claims_v2():
    item = {
        "candidate_id": "cmd_vs_done",
        "question": "云烨向笑苍生下达了哪些指令？",
        "answer_criteria": "说明三项指令和时限要求。",
        "answer": "笑苍生已经要回配饰、牵回剩下的七匹战马，并转告商人。",
        "supporting_quote": "你去把我的配饰要回来，再顺便把剩下的七匹战马也牵回来。",
        "anchor_chapter": 1,
        "chapter": 2,
    }
    dec = select_audit_route(item, enabled=True)
    assert dec.trigger_claims_v2 is True
    assert dec.route == "claims_v2_on_demand"
    assert any("modality_or_stage_cue" in r for r in dec.trigger_reasons)

