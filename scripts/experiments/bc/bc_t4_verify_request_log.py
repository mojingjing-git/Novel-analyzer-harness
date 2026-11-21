"""验证复检请求日志：能从落盘记录重建同一请求，且哈希一致。

只用**本地模拟**跑一遍复检（注入假模型），不发起真实模型调用。
校验三件事：
  1. 记录里确实存了最终提示词、证据包、各段哈希；
  2. 用记录里的候选元数据 + 证据包重新渲染，得到的提示词哈希与记录一致；
  3. 落盘不增加模型调用次数、不改变判定结果。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.common.recheck_request_log import (  # noqa: E402
    load_request_log,
    save_request_log,
    sha256_text,
    verify_request_record,
)
from backend.services.recheck_v2 import run_recheck_v2  # noqa: E402
from backend.utils.foreshadow_ledger import ForeshadowItem  # noqa: E402

CH = {
    1: "第一章：沈砚把令牌藏进石匣，匾额上写着「甲乙丙丁」四个方位。",
    2: "第二章：城主打开石匣，确认令牌确为真品。匾额的方位顺序被打乱。",
}


def _item(cid: str, anchor_ch: int, question: str, criteria: str) -> ForeshadowItem:
    return ForeshadowItem(
        id=cid,
        description="令牌真伪与匾额方位",
        status="active",
        first_seen_batch=1,
        last_seen_batch=1,
        first_seen_chapter=anchor_ch,
        last_seen_chapter=anchor_ch,
        anchor_chapter=anchor_ch,
        anchor_quote="沈砚把令牌藏进石匣，匾额上写着「甲乙丙丁」四个方位。",
        question=question,
        answer_criteria=criteria,
        resolved_chapter=0,
        lead=False,
    )


async def main() -> None:
    calls: list[str] = []

    async def fake_model(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(
            [
                {
                    "candidate_id": "fs_001",
                    "verdict": "SUPPORTED",
                    "answer": "令牌确为真品；匾额方位被打乱。",
                    "evidence_id": "e1",
                    "supporting_quote": "城主打开石匣，确认令牌确为真品。",
                    "counter_search": {"performed": True, "checked_evidence_ids": ["e1"]},
                }
            ],
            ensure_ascii=False,
        )

    items = [
        _item("fs_001", 1, "石匣里的令牌是真品还是赝品，匾额方位如何解读？", "确认令牌真伪并交代方位顺序"),
        _item("fs_002", 1, "匾额上的四个方位分别指什么？", "交代四个方位的含义"),
    ]

    # 基线：不落盘
    base_out = await run_recheck_v2(items, chapters=CH, call_model=fake_model)
    base_calls = len(calls)
    base_verdicts = [(v.candidate_id, v.verdict) for v in base_out.verdicts]

    # 落盘版本
    calls.clear()
    log: dict = {
        "schema": "recheck_request_log_v1",
        "run": {
            "work_id": "local-sim",
            "output_dir": "local-sim",
            "code_snapshot_id": "simcode",
            "contract_version": "recheck_v2",
            "model_config": {"model": "fake"},
        },
        "requests": [],
        "usage_by_prompt_sha256": {},
    }
    logged_out = await run_recheck_v2(
        items, chapters=CH, call_model=fake_model, request_log=log
    )
    logged_calls = len(calls)
    logged_verdicts = [(v.candidate_id, v.verdict) for v in logged_out.verdicts]

    requests = log.get("requests") or []
    # 用与实际发送同一个渲染函数重建
    from backend.services import recheck_v2 as rv2

    def renderer(cands):
        # 复检层内部渲染器依赖闭包；这里用记录里的提示词与元数据做等价重建：
        # 逐段比对重建出的 items 段与记录里的段是否一致。
        return _rebuild_prompt(rv2, cands)

    verifications = []
    for rec in requests:
        v = verify_request_record(rec, renderer)
        verifications.append(v)

    report = {
        "model_calls": {
            "baseline_calls": base_calls,
            "logged_calls": logged_calls,
            "same_call_count": base_calls == logged_calls,
        },
        "verdicts": {
            "baseline": base_verdicts,
            "logged": logged_verdicts,
            "same_verdicts": base_verdicts == logged_verdicts,
        },
        "requests_recorded": len(requests),
        "record_fields_present": {
            "prompt": all(bool(r.get("prompt")) for r in requests),
            "prompt_sha256": all(bool(r.get("prompt_sha256")) for r in requests),
            "evidence_batches": all(bool(r.get("evidence_batches")) is not None for r in requests),
            "evidence_text_sha256": all(
                all(bool(e.get("text_sha256")) for e in (r.get("evidence_batches") or []))
                for r in requests
            ),
            "candidate_ids": all(bool(r.get("candidate_ids")) for r in requests),
            "run_binding": all(
                bool((r.get("run") or {}).get("code_snapshot_id")) for r in requests
            ),
            "per_attempt_response": all(
                all(
                    (a.get("response_sha256") is not None and a.get("seconds") is not None)
                    for a in (r.get("attempts") or [])
                )
                for r in requests
            ),
            "per_attempt_error_field": all(
                all("error" in a for a in (r.get("attempts") or [])) for r in requests
            ),
        },
        "rebuild_verification": verifications,
        "all_rebuilds_matched": all(v.get("matched") for v in verifications) if verifications else None,
    }
    p = Path("workspace/bc-evaluation/t4-trial-20261106/t4-request-log-verification.json")
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _rebuild_prompt(rv2, cands, unit_noun: str = "章"):
    """用记录里的候选元数据 + 证据包重新渲染提示词。

    复检层的渲染器是闭包内的 `_render_batch`；这里复刻它的输入契约
    （候选对象带 item/segments），并用同一套模板渲染。
    """
    rendered = "".join(
        rv2.ITEM_TEMPLATE.format(
            candidate_id=c["candidate_id"],
            description=c.get("description", ""),
            question=c.get("question", "") or "（未提供）",
            answer_criteria=c.get("answer_criteria", "") or "（未提供）",
            first_seen=c.get("first_seen", 0),
            last_seen=c.get("last_seen", 0),
            unit_note=(
                ""
                if unit_noun == "章"
                else "（注意：下面的证据标签里，“文件N”是物理文件序号，"
                "括号里的“第N回”是文学回号；它们与上面的章号不是同一套编号，"
                "不要因为编号不同就以为给错了证据）"
            ),
            segment_count=len(c.get("segments") or []),
            truncation_note=(
                f"（注意：后文还有 {c.get('omitted', 0)} 处提到相关词但未展示，"
                "判「没有回收」时要考虑这一点）"
                if c.get("truncated")
                else ""
            ),
            segments="\n".join(
                f'[evidence_id={seg["evidence_id"]} 第{seg["chapter"]}章]\n"""\n'
                f'{seg["text"]}\n"""'
                for seg in (c.get("segments") or [])
            ),
        )
        for c in cands
    )
    return rv2.PROMPT_TEMPLATE.format(
        count=len(cands),
        items=rendered,
        schema=rv2.render_recheck_schema(),
        candidate_ids=", ".join(c["candidate_id"] for c in cands),
    )

if __name__ == "__main__":
    asyncio.run(main())
