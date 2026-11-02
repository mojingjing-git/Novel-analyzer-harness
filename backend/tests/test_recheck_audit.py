"""audit 模式（存量回收清算）的判定核与管线契约。

核心反转点：open 模式里"SUPPORTED 却列反证"只是降级 INSUFFICIENT；
audit 模式里反证引文是"自述不成立"的直接证据 → 必须 QUESTIONED(contradicted)。
其余门禁：VERIFIED 三条件（答案非空 / 反证已执行且 ID 属于本题 / 引文逐字落在
点名证据段）；结论两态无中间态；单轮不补问。
"""

from __future__ import annotations

import json

import pytest

from backend.services.recheck_v2 import run_recheck_audit
from backend.utils.foreshadow_ledger import ForeshadowItem

CHAPTERS = {
    1: "第一回：主角在市集发现铜匣，铜匣上锁着奇怪的纹路，他决意查明来历。这是全书第一个悬念。",
    2: "第二回：铜匣的来历终于揭晓，是前朝遗物，由祖辈相传而下。众人惊叹不已。",
    3: "第三回：北境战事骤起，边关告急，朝堂震动。",
    4: "第四回：江南水患，漕运改道，与铜匣无关的支线情节推进。",
}

EVID_RE = None  # 在 _mock 里惰性编译（与段落渲染格式耦合）


def _audit_item(fid: str, **kw) -> ForeshadowItem:
    defaults = dict(
        id=fid,
        description="铜匣来历之谜",
        first_seen_chapter=1,
        first_seen_batch=0,
        last_seen_chapter=2,
        last_seen_batch=1,
        status="resolved",
        source_type="llm_judged",
        resolution="第二回揭示铜匣为前朝遗物，由祖辈相传而下",
        resolved_chapter=2,
        anchor_quote="铜匣上锁着奇怪的纹路",
        anchor_chapter=1,
        question="铜匣的来历是什么",
        answer_criteria="后文明确交代铜匣的来源",
        needs_review=False,
    )
    defaults.update(kw)
    return ForeshadowItem(**defaults)


def _mock_audit(calls: list[str], *, verdict: str = "VERIFIED", reason_code: str = "supported",
                counter_quotes: list[str] | None = None, quote_override: str | None = None):
    """从 audit 提示词里解析候选与证据段，回确定性结论。

    VERIFIED 的引文取其第一个证据段的首 12 字（逐字命中）；
    支持 quote_override 注入伪造引文以触发门禁失败。
    """

    async def _call(prompt: str) -> str:
        calls.append(prompt)
        import re

        pattern = re.compile(r"\[evidence_id=([0-9a-f]{12})[^\n]*\]\n\"\"\"\n([^\n]{1,12})")
        payloads = []
        for block in prompt.split("### 候选 candidate_id=")[1:]:
            cid = block.split("\n", 1)[0].strip()
            m = pattern.search(block)
            if m is None:
                continue
            eid, seg_head = m.group(1), m.group(2)
            payload = {
                "candidate_id": cid,
                "verdict": verdict,
                "reason_code": reason_code,
                "answer": "自述与原文一致" if verdict == "VERIFIED" else "自述未获支持",
                "supporting_quote": quote_override if quote_override is not None else seg_head,
                "evidence_id": eid,
                "counter_search": {
                    "performed": True,
                    "checked_evidence_ids": [eid],
                    "counter_quotes": counter_quotes or [],
                },
            }
            payloads.append(payload)
        return json.dumps(payloads, ensure_ascii=False)

    return _call


@pytest.mark.asyncio
async def test_gate_reversal_counter_quotes_downgrade():
    """模型自报 VERIFIED 但列了反证引文 → 必须 QUESTIONED(contradicted)，不是 VERIFIED。

    这是 audit 与 open 的语义反转点：open 里这只降级 INSUFFICIENT，
    audit 里反证是"自述不成立"的直接证据。
    """
    item = _audit_item("fs_1")
    calls: list[str] = []
    outcome = await run_recheck_audit(
        [item], CHAPTERS, _mock_audit(calls, counter_quotes=["原文某处与自述相反的句子"]),
        max_calls=5, batch_size=1,
    )

    assert outcome.coverage["questioned"] == 1
    v = outcome.verdicts[0]
    assert v.verdict == "QUESTIONED"
    assert v.reason == "contradicted"
    # 落账：status/source_type 不动，quality 质疑 + 待人工
    assert item.status == "resolved"
    assert item.source_type == "llm_judged"
    assert item.resolution_quality == "questioned"
    assert item.needs_review is True


@pytest.mark.asyncio
async def test_verified_happy_path_clears_needs_review():
    item = _audit_item("fs_1", needs_review=True)
    calls: list[str] = []
    outcome = await run_recheck_audit(
        [item], CHAPTERS, _mock_audit(calls, verdict="VERIFIED"),
        max_calls=5, batch_size=1,
    )

    assert outcome.coverage["verified"] == 1
    v = outcome.verdicts[0]
    assert v.verdict == "VERIFIED"
    assert v.counter_search_performed is True
    assert v.quote_hash
    assert item.resolution_quality == "verified"
    assert item.needs_review is False
    assert item.status == "resolved"
    assert item.source_type == "llm_judged"
    assert outcome.applied["fs_1"] == "CONFIRMED"


@pytest.mark.asyncio
async def test_fabricated_quote_cannot_verify():
    """伪造引文（原文中不存在）→ VERIFIED 门禁失败 → QUESTIONED(no_evidence)。"""
    item = _audit_item("fs_1")
    calls: list[str] = []
    outcome = await run_recheck_audit(
        [item], CHAPTERS,
        _mock_audit(calls, verdict="VERIFIED", quote_override="这句话在任何证据段里都不存在呢"),
        max_calls=5, batch_size=1,
    )

    v = outcome.verdicts[0]
    assert v.verdict == "QUESTIONED"
    assert v.reason == "no_evidence"
    assert item.resolution_quality == "questioned"


@pytest.mark.asyncio
async def test_model_questioned_reason_passthrough():
    item = _audit_item("fs_1")
    calls: list[str] = []
    outcome = await run_recheck_audit(
        [item], CHAPTERS,
        _mock_audit(calls, verdict="QUESTIONED", reason_code="off_topic"),
        max_calls=5, batch_size=1,
    )

    v = outcome.verdicts[0]
    assert v.verdict == "QUESTIONED"
    assert v.reason == "off_topic"
    assert item.resolution_quality == "questioned"


@pytest.mark.asyncio
async def test_forced_window_puts_claimed_chapter_first():
    """D-14：自报回收章必须成为固定窗 1——即使检索 hint 的词全都命中别处。

    resolution 的用词只指向第 3 回（北境战事），检索段不会选中第 2 回；
    没有强制窗的话模型连自述材料都看不到（实测命中率 27.8%）。
    """
    item = _audit_item("fs_1", resolution="此事与北境战事和边关告急有关", resolved_chapter=2)
    calls: list[str] = []
    await run_recheck_audit([item], CHAPTERS, _mock_audit(calls), max_calls=5, batch_size=1)

    assert calls, "没有发出任何调用"
    prompt = calls[0]
    # 第 2 回的正文必须作为证据段出现（固定窗），且位于第一个 evidence 段
    idx_claimed = prompt.find("铜匣的来历终于揭晓")
    assert idx_claimed > 0, "自报回收章的固定窗缺失"
    first_evid = prompt.find("[evidence_id=")
    assert "第二回" in prompt[first_evid:first_evid + 400], "自报章不是第一段证据"


@pytest.mark.asyncio
async def test_qualification_and_conservation():
    items = [
        _audit_item("fs_ok"),                                                    # 进
        _audit_item("fs_verified", resolution_quality="verified"),               # 已复审 → 跳过
        _audit_item("fs_auto", source_type="auto_recheck"),                      # 非 llm_judged → 跳过
        _audit_item("fs_active", status="active"),                               # 非 resolved → 跳过
        _audit_item("fs_lead", lead=True),                                       # lead → 跳过
        _audit_item("fs_noanchor", anchor_quote=""),                             # 无锚点 → dropped
    ]
    calls: list[str] = []
    outcome = await run_recheck_audit(items, CHAPTERS, _mock_audit(calls), max_calls=50, batch_size=2)

    assert outcome.input_total == len(items)
    assert outcome.leads_skipped == 1
    assert outcome.coverage["candidates_total"] == 1
    assert outcome.dropped_no_anchor == 1
    # 守恒：候选桶（核对了/没核对）+ 掉队桶（无锚点）+ 跳过桶（资格不符）+ lead = 输入总数
    cov = outcome.coverage
    assert cov["verified"] + cov["questioned"] + cov["not_checked"] == cov["candidates_total"] == 1
    assert cov["skipped_ineligible"] == 3  # 已复审 / 非 llm_judged / 非 resolved
    assert cov["verified"] + cov["questioned"] + cov["not_checked"] + cov["skipped_ineligible"] \
        + outcome.dropped_no_anchor + outcome.dropped_anchor_missing + outcome.leads_skipped == len(items)
    assert outcome.calls == 1  # 单条候选恰好一次调用


@pytest.mark.asyncio
async def test_budget_exhaustion_single_pass_no_retry():
    """预算耗尽 → 未核对的落 not_checked；且 audit 单轮不补问（calls 不翻倍）。"""
    items = [_audit_item(f"fs_{i}") for i in range(1, 7)]
    calls: list[str] = []
    outcome = await run_recheck_audit(
        items, CHAPTERS, _mock_audit(calls), max_calls=1, batch_size=6,
    )

    assert outcome.calls == 1  # 6 条一批只花 1 次调用；没有第二轮补问
    cov = outcome.coverage
    assert cov["verified"] + cov["questioned"] + cov["not_checked"] == cov["candidates_total"] == 6
    assert len(outcome.not_checked) == 0


@pytest.mark.asyncio
async def test_empty_raw_is_sentinel_not_silent():
    """回包全空：calls>0 且 0 判定 → PARTIAL + 原因 + raw_sample，不得静默。"""
    async def _empty(prompt: str) -> str:
        return ""

    item = _audit_item("fs_1")
    outcome = await run_recheck_audit([item], CHAPTERS, _empty, max_calls=5, batch_size=1)
    assert outcome.calls == 1
    assert outcome.status == "PARTIAL"
    assert "全部未返回可解析判定" in outcome.status_reason
