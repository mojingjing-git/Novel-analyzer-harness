#!/usr/bin/env python
"""vnext 端到端验收（离线，零模型调用）。

跑一遍"从原文到裁决"的完整链路，逐条核对 PLAN v2 的验收判据。它不替代业务测试：
业务测试查"函数对不对"，这里查"整条链拼起来还给不给同样的结论"。

    python scripts/acceptance_vnext.py            # 全部
    python scripts/acceptance_vnext.py -v         # 逐条打印

输出最后一行是 ``ACCEPTED n/n`` 或 ``FAILED k``，便于 CI 与人工核对。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.vnext import blocks, knowledge, project, rolling, tasks  # noqa: E402
from backend.vnext.budget import RECHECK, Budget, BudgetCap, default_budget  # noqa: E402
from backend.vnext.citations import verify_fact  # noqa: E402
from backend.vnext.foreshadow import (  # noqa: E402
    Entity,
    Occurrence,
    cutoff_view,
    make_entity_id,
    make_occurrence_id,
)
from backend.vnext.knowledge import (  # noqa: E402
    CharacterStateRecord,
    FactRecord,
    KnowledgeStore,
    SourceRef,
)
from backend.vnext.prompt_limits import PromptLimits  # noqa: E402
from backend.vnext.prompt_view import build_prompt_window  # noqa: E402
from backend.vnext.recheck_plan import (  # noqa: E402
    RecheckUnit,
    plan_recheck,
    record_plan_outcome,
    summarize_from_ledger,
)
from backend.vnext.resolution import (  # noqa: E402
    COMPLETED,
    INSUFFICIENT,
    SUPPORTED,
    CandidateVerdict,
    count_verdicts,
    parse_recheck,
)

CHECKS: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, "PASS" if condition else f"FAIL {detail}"))


def _src(chapter: int, start: int = 0, end: int = 6) -> SourceRef:
    return SourceRef(f"ch_{chapter:04d}", chapter, start, end, f"h{chapter}")


def _book(tmp: Path, chapters: int = 8):
    items = [(i, f"第{i}章\n这里是第{i}章的正文内容，供验收使用。\n") for i in range(1, chapters + 1)]
    book_dir = project.create_project(tmp / "bk", "验收样本", items, book_id="bk_accept")
    return book_dir, project.load_manifest(book_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="vnext 端到端验收")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        book_dir, manifest = _book(tmp)

        # 1. 从原文建项目：章节身份稳定、正文可回读
        check("建项目：章节数与清单一致", len(manifest.chapters) == 8, len(manifest.chapters))
        first = project.read_chapter_text(book_dir, manifest.chapters[0].chapter_id)
        check("建项目：正文可回读", "第1章" in first, first[:12])

        # 2. 块规划 + 失效判定
        plans = blocks.plan_blocks(manifest, block_size=4)
        check("块规划：8 章 / 每块 4 章 = 2 块", len(plans) == 2, len(plans))
        blocks.write_block(book_dir, plans[0])
        loaded = blocks.load_block(book_dir, plans[0].block_signature)
        check("块：落盘后可读回且签名一致", loaded.block_signature == plans[0].block_signature)
        check("块：内容未变时判定为新鲜", blocks.is_block_fresh(manifest, loaded))

        # 3. 覆盖视图：签名不符不继承完成
        slot = rolling.BlockSlot(0, "stale_sig", rolling.DONE, "r_old")
        coverage = rolling.build_coverage(plans, batch_size=1, states={0: slot})
        check("覆盖：签名不符降为未完成", coverage.commit_cursor == 0, coverage.commit_cursor)
        done_slot = rolling.BlockSlot(0, plans[0].block_signature, rolling.DONE, "r1")
        coverage2 = rolling.build_coverage(plans, batch_size=1, states={0: done_slot})
        check("覆盖：签名一致则继承完成", coverage2.commit_cursor == 1, coverage2.commit_cursor)

        # 4. 知识快照：上界排他、未知来源排除
        facts = [
            FactRecord("f1", "第一章内容", _src(1)),
            FactRecord("f2", "第八内容章", _src(8)),
            FactRecord("ghost", "无来源", None),
        ]
        snapshot = knowledge.build_snapshot(exclusive_before=5, facts=facts)
        check("快照：上界排他", [f.fact_id for f in snapshot.facts] == ["f1"],
              [f.fact_id for f in snapshot.facts])
        check("快照：未知来源被排除且可追溯", snapshot.dropped_unknown_range == ("ghost",),
              snapshot.dropped_unknown_range)

        # 5. 归约：分批 ≡ 一次给全
        bulk = knowledge.KnowledgeStore()
        bulk.add_block(block_signature="b1", facts=facts[:2], character_records=[])
        split = knowledge.KnowledgeStore()
        split.add_block(block_signature="b1", facts=facts[:1], character_records=[])
        split.add_block(block_signature="b2", facts=facts[1:2], character_records=[])
        check("归约：分批与一次给全等价", bulk.reduced() == split.reduced())

        # 6. Prompt 窗口：库不裁剪、截断被报告
        store = knowledge.KnowledgeStore()
        store.add_block(
            block_signature="b1",
            facts=[FactRecord(f"f{i}", f"事实{i}", _src(i)) for i in range(1, 7)],
            character_records=[CharacterStateRecord("甲", "在", _src(1))],
        )
        window = build_prompt_window(store, exclusive_before=99, limits=PromptLimits(max_verified_facts=3))
        check("窗口：按限额裁剪", len(window.facts) == 3, len(window.facts))
        check("窗口：截断被报告", window.dropped.get("facts") == 3, window.dropped)
        check("窗口：库未被裁剪", len(store.reduced()[0]) == 6, len(store.reduced()[0]))

        # 7. 引用校验：编造引文判否、真引用判是
        chapter_text = "方醒从棺中坐起，四下寂静。"
        real = verify_fact(
            FactRecord("f1", "方醒从棺中坐起", SourceRef("ch_0001", 1, 0, 8, "h1")),
            chapter_text=chapter_text,
        )[0]
        fake = verify_fact(
            FactRecord("f2", "方醒推门走进雨里", SourceRef("ch_0001", 1, 0, 8, "h1")),
            chapter_text=chapter_text,
        )[0]
        check("证据：真引文升级为已定位", real.verification_state == "EVIDENCE_LOCATED")
        check("证据：编造引文仍是未验证", fake.verification_state == "UNVERIFIED")

        # 8. 伏笔视图：前缀不变性
        e1 = Entity(entity_id=make_entity_id(book_id="bk", seed_occurrence_id="o3", motif_key="棺"),
                    book_id="bk", motif="棺中人", seed_occurrence_id="o3")

        def occ(ch: int) -> Occurrence:
            return Occurrence(
                occurrence_id=make_occurrence_id(book_id="bk", source=_src(ch), kind="mention"),
                entity_id=e1.entity_id,
                source=_src(ch),
                quote="原文",
            )

        before = cutoff_view(entities=[e1], occurrences=[occ(3)], cutoff_ordinal=11)
        after = cutoff_view(entities=[e1], occurrences=[occ(3), occ(12)], cutoff_ordinal=11)
        check("伏笔：追加未来证据不改变过去视图", before == after)

        # 9. 裁决：无证据不回收；有证据才自动回收
        no_ev = parse_recheck(
            {"candidate_id": "cd_1", "verdict": "SUPPORTED", "answer": "已回收",
             "counter_search": {"performed": True}},
            allowed_candidate_ids={"cd_1"}, allowed_evidence_ids={"ev_1"},
            quote_within_range=lambda q, s, e: True,
        )
        good = parse_recheck(
            {"candidate_id": "cd_2", "verdict": "SUPPORTED", "answer": "已回收",
             # D-19：引文要够长（>= MIN_QUOTE_CHARS=10），否则 QUOTE_TOO_SHORT 先抢跑
             "evidence_id": "ev_1", "supporting_span": [0, 12], "supporting_quote": "证据齐备的原文片段在这里",
             "counter_search": {"performed": True, "checked_evidence_ids": ["ev_1"]}},
            allowed_candidate_ids={"cd_2"}, allowed_evidence_ids={"ev_1", "ev_2"},
            quote_within_range=lambda q, s, e: True,
        )
        check("裁决：缺证据不算支持", no_ev.verdict != SUPPORTED, no_ev.reason)
        check("裁决：证据齐备才算支持", good.verdict == SUPPORTED)

        # 10. 覆盖计数：重复候选不得冒充完成
        dup = count_verdicts(
            [CandidateVerdict("cd_1", SUPPORTED, answer="a"), CandidateVerdict("cd_1", SUPPORTED, answer="a")],
            expected_candidate_ids=["cd_1", "cd_2"],
        )
        check("计数：重复候选被发现", dup.duplicates == 1, dup.duplicates)
        check("计数：缺候选记为未核对", dup.not_checked == 1, dup.not_checked)

        # 11. 任务缓存：同输入复用、失败不冒充成功
        ledger = tasks.TaskLedger()
        unit = RecheckUnit(entity_id="fs_1", candidate_ids=("cd_1",), inputs={"model": "m1"})
        plan = plan_recheck([unit], ledger=ledger)
        check("任务：首次规划需要新调用", plan.new_calls == 1, plan.new_calls)
        key = plan.task_keys["fs_1"]
        ledger.record(tasks.TaskRecord(task_key=key, kind="recheck", stage="resolve", status="SUCCEEDED"))
        again = plan_recheck([unit], ledger=ledger)
        check("任务：同输入第二轮复用缓存", again.new_calls == 0 and len(again.reuse) == 1)

        no_cand = RecheckUnit(entity_id="fs_empty", candidate_ids=(), inputs={"model": "m1"})
        plan2 = plan_recheck([no_cand], ledger=ledger)
        record_plan_outcome(plan2, ledger=ledger)
        record = ledger.latest(plan2.task_keys["fs_empty"])
        check("任务：无候选记为结论而非跳过", record is not None and record.status == "NO_CANDIDATE")

        # 12. 预算：超额即停、未完成可续
        tight = Budget(caps={RECHECK: BudgetCap(max_calls=0, max_tokens=0)})
        blocked = plan_recheck([RecheckUnit("fs_x", ("cd_1",), {"model": "m1"})], ledger=tasks.TaskLedger(), budget=tight)
        check("预算：超额记跳过而非失败", len(blocked.budget_skipped) == 1 and blocked.new_calls == 0)
        dflt = default_budget()
        check("预算：默认分组齐备", set(dflt.snapshot()["caps"]) >= {RECHECK}, sorted(dflt.snapshot()["caps"])[:3])

        # 13. 汇总：跳过不许算完成
        summary = summarize_from_ledger(ledger, kind="recheck", stage="resolve")
        check("汇总：可只靠记录重建", summary.new_calls == 1, summary.as_dict())
        final = count_verdicts(
            [CandidateVerdict("cd_1", SUPPORTED, answer="a", evidence_id="ev_1", supporting_span=(0, 1))],
            expected_candidate_ids=["cd_1"],
        )
        check("汇总：无缺口时才 COMPLETED",
              __import__("backend.vnext.resolution", fromlist=["recheck_status"]).recheck_status(
                  started=True, finished=True, stopped=False, counts=final) == COMPLETED)

        # 14. 生产入口闭环：执行 → 落盘 → 重启 → 复用（用真实正文与真实校验器）
        from backend.vnext.budget import rebuild_usage
        from backend.vnext.recheck_plan import RecheckUnit as _Unit
        from backend.vnext.runner import load_ledger, run_recheck, save_ledger

        real_text = project.read_chapter_text(book_dir, manifest.chapters[0].chapter_id)
        span = (0, 20)  # D-19：引文至少 10 字，取够 20 字
        real_quote = real_text[span[0] : span[1]]
        unit = _Unit(
            entity_id="fs_real",
            candidate_ids=("cd_real",),
            inputs={
                "model": "m1",
                "chapter": manifest.chapters[0].chapter_id,
                "hash": manifest.chapters[0].content_hash,
            },
        )
        ledger_file = book_dir / "recheck_ledger.json"
        calls = {"n": 0}

        def _model(_unit):
            calls["n"] += 1
            return {
                "candidate_id": "cd_real",
                "verdict": "SUPPORTED",
                "answer": "方醒活着",
                "evidence_id": "ev_real",
                "supporting_span": list(span),
                "supporting_quote": real_quote,
                "counter_search": {"performed": True, "checked_evidence_ids": ["ev_other"]},
            }

        def _verify(text: str, start: int, end: int) -> bool:
            return real_text[start:end] == text

        def _evidence(_unit):
            return {"ev_real", "ev_other"}

        run1 = run_recheck(
            [unit], ledger=load_ledger(ledger_file), call_model=_model,
            budget=default_budget(), verify=_verify, allowed_evidence_ids=_evidence,
            persist=lambda l: save_ledger(ledger_file, l), tokens_per_call=11,
        )
        check("生产入口：首次执行调用一次模型", calls["n"] == 1, calls["n"])
        check("生产入口：真实引文通过校验", run1.executed == ["fs_real"], run1.as_dict())
        check("生产入口：账本已落盘", ledger_file.is_file())

        run2 = run_recheck(
            [unit], ledger=load_ledger(ledger_file), call_model=_model,
            budget=default_budget(), verify=_verify, allowed_evidence_ids=_evidence,
            persist=lambda l: save_ledger(ledger_file, l),
        )
        check(
            "生产入口：重启后复用且不再调用",
            calls["n"] == 1 and run2.reused == ["fs_real"],
            f"calls={calls['n']} reused={run2.reused}",
        )
        usage = rebuild_usage(load_ledger(ledger_file).to_dicts())
        check(
            "生产入口：重启后费用可重建",
            "recheck" in usage and usage["recheck"].calls == 1,
            usage,
        )

    passed = sum(1 for _, status in CHECKS if status == "PASS")
    total = len(CHECKS)
    if args.verbose:
        for name, status in CHECKS:
            print(f"  [{'PASS' if status == 'PASS' else 'FAIL'}] {name}"
                  + ("" if status == "PASS" else f"  ({status})"))
    failures = [(n, s) for n, s in CHECKS if s != "PASS"]
    for name, status in failures:
        print(f"FAILED: {name} -> {status}")
    print(f"ACCEPTED {passed}/{total}" if not failures else f"FAILED {len(failures)}/{total}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
