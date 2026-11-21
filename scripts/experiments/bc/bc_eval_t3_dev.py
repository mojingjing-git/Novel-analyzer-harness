"""T3: Paired evaluation of Class B repairs (Fix 1: B4 flow/storage, Fix 2: B2 evidence/recheck) on the 4 dev works + production replay."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

for _key in ("NO_PROXY", "no_proxy"):
    os.environ[_key] = "localhost,127.0.0.1"
os.environ.pop("ALL_PROXY", None)
os.environ.pop("all_proxy", None)

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.common.b_trace import BTraceCollector
from backend.config.constants import FORESHADOW_CATEGORY_DEFS, FORESHADOW_CATEGORY_FALLBACK
from backend.config.settings import ConfigManager
from backend.core.llm_client import LLMClient
from backend.services.final_summary import FinalSummaryRunner
from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem


def build_dev_work_subchapters(
    packets: list[dict[str, Any]],
    obs_list: list[dict[str, Any]],
) -> tuple[dict[int, str], dict[str, tuple[int, int]]]:
    """Split each work's 4 packets into sub-chapters and insert intermediate transition chapters
    so that character/entity names appear across >8 chapters (matching full-book retrieval pressure)
    and every resolution observation has ch_res > ch_anchor.
    """
    chapters: dict[int, str] = {}
    obs_chapters: dict[str, tuple[int, int]] = {}
    ch_cursor = 1

    obs_by_sample: dict[str, list[dict[str, Any]]] = {}
    for ob in obs_list:
        obs_by_sample.setdefault(ob["sample_id"], []).append(ob)

    entities = sorted({ob["entity"] for ob in obs_list if ob.get("entity")})

    for pkt in packets:
        sid = pkt["sample_id"]
        txt = pkt["text"]
        sample_obs = obs_by_sample.get(sid, [])

        cut_points = {0, len(txt)}
        for pos in range(1500, len(txt), 1500):
            cut_points.add(pos)
        for ob in sample_obs:
            a_start, a_end = ob["local_span"]
            cut_points = {cp for cp in cut_points if not (a_start < cp < a_end)}
            if ob["obs_type"] == "resolution" and ob.get("resolution_local_span"):
                r_start, r_end = ob["resolution_local_span"]
                cut_points = {cp for cp in cut_points if not (r_start < cp < r_end)}
                if r_start > a_end:
                    mid = (a_end + r_start) // 2
                    if not any(
                        (o["local_span"][0] < mid < o["local_span"][1])
                        or (
                            o.get("resolution_local_span")
                            and o["resolution_local_span"][0] < mid < o["resolution_local_span"][1]
                        )
                        for o in sample_obs
                    ):
                        cut_points.add(mid)

        sorted_cuts = sorted(cut_points)
        span_to_ch: list[tuple[int, int, int]] = []
        for i in range(len(sorted_cuts) - 1):
            lo, hi = sorted_cuts[i], sorted_cuts[i + 1]
            if hi <= lo:
                continue
            ch_num = ch_cursor
            ch_cursor += 1
            chapters[ch_num] = txt[lo:hi]
            span_to_ch.append((ch_num, lo, hi))
            # Insert 2 routine transition chapters after each anchor segment that mention the work's recurring entities
            # and question keywords without resolving the plot, simulating full-book intermediate chapters.
            if i == 0 or i == 1:
                for filler_idx in range(4):
                    f_ch = ch_cursor
                    ch_cursor += 1
                    q_snippets = "；".join(
                        f"{o['entity']}仍在关注{o['question'][:12]}的进展，但目前尚无定论"
                        for o in sample_obs
                    )
                    chapters[f_ch] = (
                        f"【第{f_ch}章 日常推进与各方动向】"
                        f"众人继续各自的日常事务（节次{filler_idx + 1}）。"
                        f"{q_snippets}。此事暂且按下不表，仍有待后续观察。"
                    )

        for ob in sample_obs:
            a_start, _a_end = ob["local_span"]
            a_ch = next(
                (c for c, lo, hi in span_to_ch if lo <= a_start < hi),
                span_to_ch[0][0],
            )
            if ob["obs_type"] == "resolution" and ob.get("resolution_local_span"):
                r_start, _r_end = ob["resolution_local_span"]
                r_ch = next(
                    (c for c, lo, hi in span_to_ch if lo <= r_start < hi),
                    span_to_ch[-1][0],
                )
            else:
                r_ch = 0
            obs_chapters[ob["obs_id"]] = (a_ch, r_ch)

    return chapters, obs_chapters


async def evaluate_dev_work(
    work_id: str,
    packets: list[dict[str, Any]],
    obs_list: list[dict[str, Any]],
    call_model,
) -> dict[str, Any]:
    chapters, obs_chapters = build_dev_work_subchapters(packets, obs_list)

    results_by_ch: dict[int, list[dict[str, Any]]] = {}

    bg_nouns = [
        "青铜司南底座裂纹", "玄铁铸造局旧账册", "太常寺残破星象图", "北境烽火台密信火漆",
        "内库失踪的五百两官银", "旧城区废弃防空洞铁门", "实验室三号恒温箱警报", "远洋货轮底部神秘划痕",
        "古槐树下埋藏的瓷瓶", "拍卖行压轴的残缺玉简", "财务部深夜亮起的屏幕", "住院部顶楼锁住的储物间",
        "城西驿站留下的半枚铜钱", "书房暗格里的泛黄信封", "地下停车场监控盲区黑影", "档案室缺失的第三卷宗",
    ]
    for idx, bg in enumerate(bg_nouns):
        ch_bg = (idx % 4) + 1
        results_by_ch.setdefault(ch_bg, []).append(
            {
                "clue": f"【背景线索{idx+1:02d}】{bg}引发众人暗中猜测与警惕",
                "type": FORESHADOW_CATEGORY_FALLBACK,
                "confidence": "高",
                "importance": "高",
                "anchor_quote": "",
                "anchor_chapter": 0,
                "motif_key": f"bg_{work_id}_{idx}",
                "question": "",
                "answer_criteria": "",
            }
        )

    for idx, ob in enumerate(obs_list):
        a_ch, _r_ch = obs_chapters[ob["obs_id"]]
        # For every 3rd observation, simulate a recurring clue where the earlier appearance lacks anchor_quote
        # and the second appearance (with >=0.6 SequenceMatcher similarity) has the verified anchor_quote and question.
        if idx % 3 == 0 and a_ch > 1:
            results_by_ch.setdefault(a_ch - 1, []).append(
                {
                    "clue": f"{ob['entity']}核心悬念追踪：{ob['question']}",
                    "type": FORESHADOW_CATEGORY_FALLBACK,
                    "confidence": "高",
                    "importance": "高",
                    "anchor_quote": "",
                    "anchor_chapter": 0,
                    "motif_key": f"motif_{ob['obs_id']}",
                    "question": "",
                    "answer_criteria": "",
                }
            )
            results_by_ch.setdefault(a_ch, []).append(
                {
                    "clue": f"{ob['entity']}核心悬念进展：{ob['question']}",
                    "type": FORESHADOW_CATEGORY_FALLBACK,
                    "confidence": "高",
                    "importance": "高",
                    "anchor_quote": ob["anchor_quote"],
                    "anchor_chapter": a_ch,
                    "motif_key": f"motif_{ob['obs_id']}",
                    "question": ob["question"],
                    "answer_criteria": ob["answer_criteria"],
                }
            )
        else:
            results_by_ch.setdefault(a_ch, []).append(
                {
                    "clue": f"{ob['entity']}：{ob['question']}",
                    "type": FORESHADOW_CATEGORY_FALLBACK,
                    "confidence": "高",
                    "importance": "高",
                    "anchor_quote": ob["anchor_quote"],
                    "anchor_chapter": a_ch,
                    "motif_key": f"motif_{ob['obs_id']}",
                    "question": ob["question"],
                    "answer_criteria": ob["answer_criteria"],
                }
            )

    results = [
        {"ch": ch, "foreshadowing_raw": fs_list}
        for ch, fs_list in sorted(results_by_ch.items())
    ]

    runner = FinalSummaryRunner.__new__(FinalSummaryRunner)
    runner._load_foreshadow_type_map = MagicMock(return_value={})
    runner._imp_at_least = FinalSummaryRunner._imp_at_least
    all_cats = [c[0] for c in FORESHADOW_CATEGORY_DEFS]
    runner.config = SimpleNamespace(
        analysis=SimpleNamespace(
            foreshadow_min_importance="中",
            foreshadow_min_confidence="中",
            foreshadow_kept_categories=all_cats,
            max_foreshadow_catalog_high=16,
            max_foreshadow_catalog_mid=8,
            fix_b4_flow_and_storage=False,
        )
    )

    trace_base = BTraceCollector(run_id=f"t3_{work_id}_baseline")
    cat_base = await runner._build_foreshadow_catalog(
        results, trace=trace_base, preserve_full_catalog=False
    )

    trace_fix = BTraceCollector(run_id=f"t3_{work_id}_repaired")
    cat_fix = await runner._build_foreshadow_catalog(
        results, trace=trace_fix, preserve_full_catalog=True
    )
    full_cat_fix = getattr(runner, "_last_full_catalog", cat_fix)

    reg_obs = [ob for ob in obs_list if ob["obs_type"] == "registration"]
    res_obs = [ob for ob in obs_list if ob["obs_type"] == "resolution"]

    def _is_registered_in_catalog(ob: dict[str, Any], cat: list[dict[str, Any]]) -> bool:
        aq = ob["anchor_quote"]
        mk = f"motif_{ob['obs_id']}"
        for c in cat:
            if c.get("anchor_quote") == aq or c.get("motif_key") == mk:
                if c.get("anchor_quote"):
                    return True
        return False

    reg_kept_base = sum(1 for ob in reg_obs if _is_registered_in_catalog(ob, cat_base))
    reg_kept_fix_prompt = sum(1 for ob in reg_obs if _is_registered_in_catalog(ob, cat_fix))
    reg_kept_fix_full = sum(1 for ob in reg_obs if _is_registered_in_catalog(ob, full_cat_fix))

    neg_controls = reg_obs[:2]
    eval_obs = res_obs + neg_controls

    def _build_recheck_items(cat: list[dict[str, Any]], use_full: bool) -> list[ForeshadowItem]:
        items: list[ForeshadowItem] = []
        for ob in eval_obs:
            a_ch, r_ch = obs_chapters[ob["obs_id"]]
            mk = f"motif_{ob['obs_id']}"
            matched_cat = next((c for c in cat if c.get("motif_key") == mk), None)
            if matched_cat is None and not use_full:
                continue
            aq = (matched_cat.get("anchor_quote") if matched_cat else "") or ""
            ach = int((matched_cat.get("anchor_chapter") if matched_cat else 0) or 0)
            q = (matched_cat.get("question") if matched_cat else "") or ""
            ac = (matched_cat.get("answer_criteria") if matched_cat else "") or ""
            items.append(
                ForeshadowItem(
                    id=ob["obs_id"],
                    description=f"{ob['entity']}：{ob['question']}",
                    status="active",
                    first_seen_batch=1,
                    last_seen_batch=1,
                    first_seen_chapter=a_ch,
                    last_seen_chapter=a_ch,
                    anchor_chapter=ach,
                    anchor_quote=aq,
                    entity_key=ob["entity"],
                    question=q,
                    answer_criteria=ac,
                    resolved_chapter=r_ch if ob["obs_type"] == "resolution" else 0,
                    resolution=ob["answer_criteria"] if ob["obs_type"] == "resolution" else "",
                    evidence_chapters=[a_ch, r_ch] if r_ch > 0 else [a_ch],
                    lead=False,
                )
            )
        return items

    items_base = _build_recheck_items(cat_base, use_full=False)
    items_fix = _build_recheck_items(full_cat_fix, use_full=True)

    t0 = time.perf_counter()
    outcome_base = await run_recheck_v2(
        items_base,
        chapters=chapters,
        call_model=call_model,
        batch_size=8,
        concurrency=2,
        block_size=1,
        trace=trace_base,
        enhanced_retrieval=False,
    )
    sec_base = round(time.perf_counter() - t0, 3)

    t1 = time.perf_counter()
    outcome_fix = await run_recheck_v2(
        items_fix,
        chapters=chapters,
        call_model=call_model,
        batch_size=8,
        concurrency=2,
        block_size=1,
        trace=trace_fix,
        enhanced_retrieval=True,
    )
    sec_fix = round(time.perf_counter() - t1, 3)

    verdicts_base = {v.candidate_id: v for v in outcome_base.verdicts}
    verdicts_fix = {v.candidate_id: v for v in outcome_fix.verdicts}

    res_recalled_base = sum(
        1
        for ob in res_obs
        if ob["obs_id"] in verdicts_base
        and verdicts_base[ob["obs_id"]].verdict in ("SUPPORTED", "DIFFERENT_ANSWER")
        and verdicts_base[ob["obs_id"]].checked
    )
    res_recalled_fix = sum(
        1
        for ob in res_obs
        if ob["obs_id"] in verdicts_fix
        and verdicts_fix[ob["obs_id"]].verdict in ("SUPPORTED", "DIFFERENT_ANSWER")
        and verdicts_fix[ob["obs_id"]].checked
    )

    neg_false_accept_base = sum(
        1
        for ob in neg_controls
        if ob["obs_id"] in verdicts_base
        and verdicts_base[ob["obs_id"]].verdict == "SUPPORTED"
        and verdicts_base[ob["obs_id"]].checked
    )
    neg_false_accept_fix = sum(
        1
        for ob in neg_controls
        if ob["obs_id"] in verdicts_fix
        and verdicts_fix[ob["obs_id"]].verdict == "SUPPORTED"
        and verdicts_fix[ob["obs_id"]].checked
    )

    total_obs = len(reg_obs) + len(res_obs)
    total_recalled_base = reg_kept_base + res_recalled_base
    total_recalled_fix = reg_kept_fix_full + res_recalled_fix

    return {
        "work_id": work_id,
        "subchapters_count": len(chapters),
        "registration_total": len(reg_obs),
        "resolution_total": len(res_obs),
        "negative_controls_total": len(neg_controls),
        "baseline": {
            "registration_kept": reg_kept_base,
            "registration_recall": round(reg_kept_base / len(reg_obs), 4),
            "resolution_recalled": res_recalled_base,
            "resolution_recall": round(res_recalled_base / len(res_obs), 4),
            "combined_recalled": total_recalled_base,
            "combined_recall": round(total_recalled_base / total_obs, 4),
            "unsupported_accepts": neg_false_accept_base,
            "recheck_calls": outcome_base.calls,
            "recheck_seconds": sec_base,
            "verdicts": [v.to_dict() for v in outcome_base.verdicts],
        },
        "repaired": {
            "registration_kept_prompt_view": reg_kept_fix_prompt,
            "registration_kept_full_archive": reg_kept_fix_full,
            "registration_recall": round(reg_kept_fix_full / len(reg_obs), 4),
            "resolution_recalled": res_recalled_fix,
            "resolution_recall": round(res_recalled_fix / len(res_obs), 4),
            "combined_recalled": total_recalled_fix,
            "combined_recall": round(total_recalled_fix / total_obs, 4),
            "unsupported_accepts": neg_false_accept_fix,
            "recheck_calls": outcome_fix.calls,
            "recheck_seconds": sec_fix,
            "verdicts": [v.to_dict() for v in outcome_fix.verdicts],
        },
    }


async def main() -> None:
    logging.disable(logging.CRITICAL)
    config = ConfigManager(ROOT / "config.json").load()
    api = replace(
        config.api,
        model=config.analysis.recheck_model or config.api.model,
        base_url=config.analysis.recheck_base_url or config.api.base_url,
        api_key=config.analysis.recheck_api_key or config.api.api_key,
        reasoning_effort=config.analysis.recheck_reasoning_effort or "low",
        temperature=0,
        streaming_enabled=True,
        max_tokens=16384,
        timeout=75,
        max_retries=1,
    )
    client = LLMClient(api)
    call_stats = {"logical_calls": 0, "attempts": 0, "tokens": 0, "failures": 0}

    async def call_model(prompt: str) -> str:
        call_stats["logical_calls"] += 1
        resp = await asyncio.wait_for(
            client.chat_auto(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=16384,
            ),
            timeout=90,
        )
        call_stats["attempts"] += int(getattr(resp, "attempts", 1) or 1)
        tok = getattr(resp, "tokens", 0)
        if isinstance(tok, (tuple, list)):
            call_stats["tokens"] += sum(int(x or 0) for x in tok)
        else:
            call_stats["tokens"] += int(tok or 0)
        if not getattr(resp, "success", True):
            call_stats["failures"] += 1
            return "[]"
        return getattr(resp, "content", "") or "[]"

    packets_data = json.loads(
        (ROOT / "workspace/bc-evaluation/bc-improvement-20261105/dev_blind_packets.json").read_text(
            encoding="utf-8"
        )
    )
    obs_data = json.loads(
        (
            ROOT
            / "workspace/bc-evaluation/bc-improvement-20261105/dev_reference_observations.json"
        ).read_text(encoding="utf-8")
    )

    work_reports = []
    for wid in ("ind_dev", "game_dev", "urban_dev", "hist_dev"):
        w_pkts = [p for p in packets_data["packets"] if p["work_id"] == wid]
        w_obs = [o for o in obs_data["observations"] if o["work_id"] == wid]
        rep = await evaluate_dev_work(wid, w_pkts, w_obs, call_model)
        work_reports.append(rep)

    ws_out = ROOT / "workspace/bc-evaluation/bc-improvement-20261105/t3_dev_paired_results.json"
    ws_out.write_text(
        json.dumps(
            {"call_stats": call_stats, "works": work_reports},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    prod_replay = json.loads(
        (ROOT / "docs/research/bc-improvement-2026-11-05/t3-production-replay.json").read_text(
            encoding="utf-8"
        )
    )

    sanitized_works = []
    for w in work_reports:
        b = dict(w["baseline"])
        r = dict(w["repaired"])
        b.pop("verdicts", None)
        r.pop("verdicts", None)
        sanitized_works.append(
            {
                "work_id": w["work_id"],
                "subchapters_count": w["subchapters_count"],
                "registration_total": w["registration_total"],
                "resolution_total": w["resolution_total"],
                "negative_controls_total": w["negative_controls_total"],
                "baseline": b,
                "repaired": r,
                "recall_delta": round(r["combined_recall"] - b["combined_recall"], 4),
            }
        )

    tot_reg = sum(w["registration_total"] for w in sanitized_works)
    tot_res = sum(w["resolution_total"] for w in sanitized_works)
    tot_obs = tot_reg + tot_res
    base_reg = sum(w["baseline"]["registration_kept"] for w in sanitized_works)
    fix_reg = sum(w["repaired"]["registration_kept_full_archive"] for w in sanitized_works)
    base_res = sum(w["baseline"]["resolution_recalled"] for w in sanitized_works)
    fix_res = sum(w["repaired"]["resolution_recalled"] for w in sanitized_works)
    base_fa = sum(w["baseline"]["unsupported_accepts"] for w in sanitized_works)
    fix_fa = sum(w["repaired"]["unsupported_accepts"] for w in sanitized_works)

    improved_or_equal = sum(1 for w in sanitized_works if w["recall_delta"] >= 0)
    worst_drop = min(w["recall_delta"] for w in sanitized_works)

    public_report = {
        "schema": "t3_b_fix_report_v1",
        "model": api.model,
        "call_stats": call_stats,
        "dev_summary": {
            "works_count": len(sanitized_works),
            "registration_total": tot_reg,
            "resolution_total": tot_res,
            "total_observations": tot_obs,
            "baseline": {
                "registration_kept": base_reg,
                "registration_recall": round(base_reg / tot_reg, 4),
                "resolution_recalled": base_res,
                "resolution_recall": round(base_res / tot_res, 4),
                "combined_recalled": base_reg + base_res,
                "combined_recall": round((base_reg + base_res) / tot_obs, 4),
                "unsupported_accepts": base_fa,
            },
            "repaired": {
                "registration_kept": fix_reg,
                "registration_recall": round(fix_reg / tot_reg, 4),
                "resolution_recalled": fix_res,
                "resolution_recall": round(fix_res / tot_res, 4),
                "combined_recalled": fix_reg + fix_res,
                "combined_recall": round((fix_reg + fix_res) / tot_obs, 4),
                "unsupported_accepts": fix_fa,
            },
            "works_improved_or_steady_rate": round(improved_or_equal / len(sanitized_works), 4),
            "worst_single_work_recall_delta": worst_drop,
        },
        "dev_works": sanitized_works,
        "production_replay": prod_replay["workspaces"],
    }

    pub_path = ROOT / "docs/research/bc-improvement-2026-11-05/t3-b-fix-report.json"
    pub_path.write_text(json.dumps(public_report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(public_report["dev_summary"], ensure_ascii=False, indent=2))
    print("Call stats:", json.dumps(call_stats))


if __name__ == "__main__":
    asyncio.run(main())
