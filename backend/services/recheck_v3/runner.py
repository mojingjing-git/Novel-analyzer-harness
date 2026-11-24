"""伏笔复检 V3 主执行器：U1/U3/U4/U5 四防闭环调度（Layer 4）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from backend.common.recheck_request_log import sha256_text
from backend.core.evidence import EvidenceIndex, _keywords_for
from backend.core.text_units import build_display_labels
from backend.services.recheck_resolution import (
    INSUFFICIENT,
    STOPPED,
    SUPPORTED,
    CandidateVerdict,
    count_verdicts,
    recheck_status,
)
from backend.services.recheck_v2 import (
    AUTO_RESOLVED,
    OPEN,
    RECHECK_DETAIL_FILENAME,
    RecheckOutcome,
    _display_chapter,
    _evidence_id,
)
from backend.utils.json_utils import safe_save_json

from .evidence_pool import (
    _build_audit_bundle_and_lists,
    _build_balanced_superchunks,
    _inherit_cross_pool_units,
    _match_aliases_for_item,
    _pack_bins_overlap_pool,
    _render_detective_card_pool,
    _render_shared_pool_for_bin,
    _render_u4_card_pool,
    _resolve_setup_location,
    apply_v3_verdict_to_item,
    verify_evidence_chain_v3,
)
from .parsers import (
    _normalize_verdict,
    _parse_json_payload,
    _parse_rows_by_id,
    _safe_int_unit,
    _salvage_json_array_rows,
    _select_primary_evidence,
)
from .preprocessing import (
    _disambiguate_chapter_to_units,
    _expand_units,
    _filter_discriminative_terms,
    _find_mentions_in_range,
    _nms_episode_select,
    _score_chapter_against_terms,
    build_adaptive_digest_chunks,
    build_unit_headers,
    detect_protagonist_stopwords,
    format_unit_segment,
)
from .prompts import (
    ALLOWED_V3_TIERS,
    COMPACT_CHAPTER_CHARS,
    DEFAULT_V3_BATCH_SIZE,
    DEFAULT_V3_COMPACT_BATCH_SIZE,
    DEFAULT_V3_COMPACT_POOL_MAX_CHARS,
    DEFAULT_V3_CONCURRENCY,
    DEFAULT_V3_MAX_CALLS,
    DEFAULT_V3_POOL_MAX_CHARS,
    U1_ALIAS_CONSOLIDATE_PROMPT,
    U1_ALIAS_PAIR_PROMPT,
    U1_COMPACT_ROUTE_Q_BATCH,
    U1_COMPACT_SPLIT_BATCH_SIZE,
    U1_ROUTE_PROMPT,
    U1_ROUTE_Q_BATCH,
    U1_SPLIT_BATCH_SIZE,
    U1_SPLIT_PROMPT,
    U3_POOL_SINGLE_STAGE_PROMPT,
    U4_POOL_AUDITOR_PROMPT,
    U5_QUOTE_REPAIR_PROMPT,
    WIDE_CHAPTER_CHARS,
    _GENERIC_ENTITY_STOP,
)

logger = logging.getLogger(__name__)


async def run_recheck_v3(
    items: Sequence[Any],
    *,
    book_text: str = "",
    chapters: Mapping[int, str] | None = None,
    call_model: Callable[[str], Awaitable[str]],
    output_dir: Path | None = None,
    volume_summaries: Sequence[str] | None = None,
    max_calls: int = DEFAULT_V3_MAX_CALLS,
    batch_size: int | None = None,
    concurrency: int = DEFAULT_V3_CONCURRENCY,
    limiter: asyncio.Semaphore | None = None,
    emit: Callable[[dict], None] | None = None,
    unit_noun: str = "文件",
    display_labels: Mapping[int, str] | None = None,
    block_size: int = 4,
    compact_mode: bool = False,
    enable_gray_u4: bool = False,
    mid_single_hop: bool = False,
    request_log: dict[str, Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RecheckOutcome:
    """运行零人工 V3 共享池单阶段与可选灰区终审复检，返回兼容 RecheckOutcome。"""
    outcome = RecheckOutcome(input_total=len(items))
    if not chapters and book_text:
        chapters = {1: book_text}
    chapters_map: dict[int, str] = {int(k): v or "" for k, v in (chapters or {}).items()}
    if not chapters_map:
        outcome.status = "NOT_RUN"
        outcome.status_reason = "无可用正文单元"
        return outcome

    labels: dict[int, str] = dict(display_labels) if display_labels is not None else build_display_labels(chapters_map)
    label_num_to_units: dict[int, list[int]] = {}
    for u, lbl in sorted(labels.items()):
        m = re.search(r"(\d+)", lbl or "")
        if m:
            label_num_to_units.setdefault(int(m.group(1)), []).append(u)

    # 筛选合格条目（只复检 active 且非 lead，与 V2 资格契约一致）
    eligible: list[dict[str, Any]] = []
    leads_skipped = 0
    for item in items:
        if getattr(item, "lead", False):
            leads_skipped += 1
            continue
        if getattr(item, "source_type", "") == "human_override":
            continue
        if getattr(item, "status", "active") != "active":
            continue
        cid = str(getattr(item, "id", "") or "")
        if not cid:
            continue
        su, sa_off, loc_mode = _resolve_setup_location(
            item, chapters_map, labels, block_size=block_size
        )
        outcome.locate_records[cid] = {
            "unit": su,
            "mode": loc_mode,
            "source_chapter": int(getattr(item, "first_seen_chapter", 0) or 0),
            "anchor_offset": sa_off,
        }
        eligible.append({
            "fs_id": cid,
            "item": item,
            "setup_unit": su,
            "setup_after_offset": sa_off,
        })

    outcome.leads_skipped = leads_skipped
    if not eligible:
        empty = count_verdicts([], expected_candidate_ids=[], candidates_total=0)
        outcome.coverage = empty.as_dict()
        outcome.status = recheck_status(started=True, finished=True, stopped=False, counts=empty)
        return outcome

    sem = limiter if limiter is not None else asyncio.Semaphore(max(1, concurrency))
    max_chapter_chars = COMPACT_CHAPTER_CHARS if compact_mode else WIDE_CHAPTER_CHARS
    max_post_windows = 4 if compact_mode else 5
    max_pre_windows = 1
    hop2_new_cap, hop3_new_cap = (3, 2) if compact_mode else (4, 3)
    max_audit_windows = 6 if compact_mode else 8
    pool_max_chars = DEFAULT_V3_COMPACT_POOL_MAX_CHARS if compact_mode else DEFAULT_V3_POOL_MAX_CHARS
    split_bs = U1_COMPACT_SPLIT_BATCH_SIZE if compact_mode else U1_SPLIT_BATCH_SIZE
    route_q_batch = U1_COMPACT_ROUTE_Q_BATCH if compact_mode else U1_ROUTE_Q_BATCH
    route_prompt_cap = 50_000 if compact_mode else DEFAULT_V3_POOL_MAX_CHARS
    small_corpus = len(chapters_map) <= 12

    def _stopped() -> bool:
        return bool(should_stop is not None and should_stop())

    def _emit_stage(stage: str, *, extra: Mapping[str, Any] | None = None, silent: bool = False) -> None:
        if emit is None:
            return
        payload: dict[str, Any] = {
            "stage": stage,
            "calls": outcome.calls,
            "total": len(eligible),
            "items_total": len(eligible),
            "silent": silent,
        }
        if extra:
            payload.update(extra)
        emit(payload)

    async def _call_tracked(prompt: str, *, purpose: str = "recheck_v3") -> str:
        if _stopped():
            raise asyncio.CancelledError("user stopped")
        if outcome.calls >= max_calls:
            raise RuntimeError("BUDGET_EXHAUSTED")
        outcome.calls += 1
        err_msg = ""
        raw_resp = ""
        async with sem:
            if _stopped():
                raise asyncio.CancelledError("user stopped")
            try:
                raw_resp = await call_model(prompt)
            except Exception as exc:
                err_msg = f"{type(exc).__name__}:{str(exc)[:100]}"
                raise
            finally:
                if raw_resp and not outcome.raw_sample:
                    outcome.raw_sample = raw_resp[:2000]
                if request_log is not None:
                    usage_map = request_log.get("usage_by_prompt_sha256") or {}
                    psha = sha256_text(prompt)
                    request_log.setdefault("requests", []).append({
                        "purpose": purpose,
                        "prompt_sha256": psha,
                        "prompt_length": len(prompt),
                        "response_sha256": sha256_text(raw_resp),
                        "response_length": len(raw_resp),
                        "error": err_msg,
                        "usage": usage_map.get(psha),
                    })
        return raw_resp

    async def _batch_json_call(
        sub_items: Sequence[dict[str, Any]],
        build_prompt: Callable[[Sequence[dict[str, Any]]], str],
        *,
        purpose: str,
    ) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        if not sub_items or _stopped() or outcome.calls >= max_calls:
            return results
        expected = {x["fs_id"] for x in sub_items}
        try:
            raw = await _call_tracked(build_prompt(sub_items), purpose=purpose)
        except (asyncio.CancelledError, RuntimeError):
            return results
        except Exception:
            outcome.failed_batches += 1
            return results
        parsed = _parse_json_payload(raw)
        rows = (
            parsed
            if isinstance(parsed, list)
            else (parsed.get("items") or parsed.get("results") or [] if isinstance(parsed, dict) else [])
        )
        for r in rows:
            if isinstance(r, Mapping) and str(r.get("fs_id") or "") in expected:
                results[str(r["fs_id"])] = dict(r)
        if len(results) < len(expected):
            for k, v in _salvage_json_array_rows(raw).items():
                if k in expected and k not in results:
                    results[k] = v
        missing = [x for x in sub_items if x["fs_id"] not in results]
        if missing and len(sub_items) > 1 and not _stopped() and outcome.calls < max_calls:
            outcome.retried_batches += 1

            async def _fix_one(x: dict[str, Any]) -> None:
                if _stopped() or outcome.calls >= max_calls:
                    return
                try:
                    r2 = await _call_tracked(build_prompt([x]), purpose=f"{purpose}_retry")
                except Exception:
                    return
                p2 = _parse_json_payload(r2)
                rows2 = (
                    p2
                    if isinstance(p2, list)
                    else ([p2] if isinstance(p2, dict) and "fs_id" in p2 else (p2.get("items") or [] if isinstance(p2, dict) else []))
                )
                for row in rows2:
                    if isinstance(row, Mapping) and str(row.get("fs_id") or "") == x["fs_id"]:
                        results[x["fs_id"]] = dict(row)
                        return
                if rows2 and isinstance(rows2[0], Mapping):
                    d = dict(rows2[0])
                    d["fs_id"] = x["fs_id"]
                    results[x["fs_id"]] = d

            await asyncio.gather(*(_fix_one(x) for x in missing), return_exceptions=True)
        return results

    # 在后台线程构建 2-gram 位图与自适应块，避免阻塞事件循环
    index = EvidenceIndex(chapters_map)
    await asyncio.to_thread(index.ensure_indexed)
    unit_headers = await asyncio.to_thread(build_unit_headers, chapters_map)
    protagonist_stops = await asyncio.to_thread(
        detect_protagonist_stopwords, chapters_map, eligible, index
    )
    (
        macro_chunks,
        block_tuples,
        unit_to_block,
        recon_claims,
        ckpt_vol_briefs,
    ) = await asyncio.to_thread(
        build_adaptive_digest_chunks,
        chapters_map,
        labels,
        output_dir=output_dir,
        block_size=block_size,
        blocks_per_macro=15,
    )
    route_chunks = await asyncio.to_thread(
        _build_balanced_superchunks, block_tuples, compact_mode=compact_mode
    )
    if not route_chunks:
        route_chunks = macro_chunks

    # ---------- Stage U1: 批拆解 + 均衡超块别名词典 + 双向路由（带断点缓存与小语料快路径） ----------
    _emit_stage("u1_index", extra={
        "checked": 0,
        "stage_done": 0,
        "stage_total": len(eligible),
        "remaining": len(eligible),
        "unit": "条",
    })
    u1_cache_path = (
        output_dir / "final_summary_checkpoint" / "recheck_v3_u1_cache.json"
        if output_dir is not None
        else None
    )
    index_fp = hashlib.sha256(
        "|".join([
            "v3.2",
            "compact" if compact_mode else "wide",
            str(len(chapters_map)),
            str(max(chapters_map)),
            str(len(route_chunks)),
            ",".join(sorted(x["fs_id"] for x in eligible)),
        ]).encode("utf-8")
    ).hexdigest()[:16]

    splits: dict[str, dict[str, Any]] = {}
    alias_lexicon: list[dict[str, Any]] = []
    routed_hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    done_cids: set[int] = set()
    alias_done = small_corpus

    if u1_cache_path is not None and u1_cache_path.is_file():
        try:
            cached_u1 = json.loads(u1_cache_path.read_text(encoding="utf-8"))
            if isinstance(cached_u1, Mapping) and cached_u1.get("fingerprint") == index_fp:
                splits = dict(cached_u1.get("splits") or {})
                alias_lexicon = list(cached_u1.get("alias_lexicon") or [])
                alias_done = bool(cached_u1.get("alias_done", alias_done))
                for k, hs in (cached_u1.get("routed_hits") or {}).items():
                    routed_hits[k] = list(hs)
                done_cids = {int(c) for c in (cached_u1.get("chunks_done") or [])}
        except Exception:
            splits, alias_lexicon, done_cids = {}, [], set()

    # U1.1 核心拆解（bs=50 wide / 25 compact）
    missing_split = [x for x in eligible if x["fs_id"] not in splits]
    if missing_split and not _stopped() and outcome.calls < max_calls:
        split_total = len(eligible)
        split_done = len(splits)
        _emit_stage("u1_split", extra={
            "checked": 0,
            "stage_done": split_done,
            "stage_total": split_total,
            "remaining": max(0, split_total - split_done),
            "unit": "条",
        })
        split_batches = [missing_split[i : i + split_bs] for i in range(0, len(missing_split), split_bs)]

        def _render_split_prompt(sub: Sequence[dict[str, Any]]) -> str:
            lines = []
            for x in sub:
                it = x["item"]
                lines.append(
                    f"- fs_id={x['fs_id']} | setup_unit=U{x['setup_unit']} | "
                    f"question={getattr(it, 'question', '') or getattr(it, 'description', '')} | "
                    f"criteria={getattr(it, 'answer_criteria', '')} | "
                    f"desc={str(getattr(it, 'description', '') or '')[:130]}"
                )
            return U1_SPLIT_PROMPT.format(count=len(sub), items_text="\n".join(lines))

        async def _run_split_batch(b: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
            nonlocal split_done
            try:
                return await _batch_json_call(b, _render_split_prompt, purpose="u1_split")
            finally:
                if not _stopped():
                    split_done = min(split_total, split_done + len(b))
                    _emit_stage("u1_split", silent=True, extra={
                        "checked": 0,
                        "stage_done": split_done,
                        "stage_total": split_total,
                        "remaining": max(0, split_total - split_done),
                        "unit": "条",
                    })

        for res in await asyncio.gather(*(_run_split_batch(b) for b in split_batches)):
            splits.update(res)

    for x in eligible:
        fid = x["fs_id"]
        if fid not in splits:
            it = x["item"]
            splits[fid] = {
                "fs_id": fid,
                "core_question": str(getattr(it, "question", "") or getattr(it, "description", "")),
                "bonus_aspects": "",
                "setup_event_anchor": str(getattr(it, "description", "") or "")[:50],
                "needs_pre_setup_backref": False,
                "key_entities": [],
            }

    # U1.2 + U1.3：仅对中大型全书语料（>12 单元）执行均衡超块别名词典与双向路由
    if not small_corpus and not _stopped() and outcome.calls < max_calls:
        if not alias_done and route_chunks:
            alias_total = len(route_chunks)
            alias_step_done = 0
            if alias_total > 0:
                _emit_stage("u1_alias", extra={
                    "checked": 0,
                    "stage_done": 0,
                    "stage_total": alias_total,
                    "remaining": alias_total,
                    "unit": "批",
                })

            async def _extract_alias_chunk(chunk: Mapping[str, Any]) -> list[dict[str, Any]]:
                nonlocal alias_step_done
                if _stopped() or outcome.calls >= max_calls:
                    return []
                su, eu = int(chunk["start_unit"]), int(chunk["end_unit"])
                p = U1_ALIAS_PAIR_PROMPT.format(chunk_start=su, chunk_end=eu, chunk_text=chunk["text"])
                try:
                    raw = await _call_tracked(p, purpose="u1_alias_pair")
                except Exception:
                    return []
                finally:
                    if not _stopped() and alias_total > 0:
                        alias_step_done = min(alias_total, alias_step_done + 1)
                        _emit_stage("u1_alias", silent=True, extra={
                            "checked": 0,
                            "stage_done": alias_step_done,
                            "stage_total": alias_total,
                            "remaining": max(0, alias_total - alias_step_done),
                            "unit": "批",
                        })
                parsed = _parse_json_payload(raw)
                rows = parsed if isinstance(parsed, list) else (parsed.get("aliases") or [] if isinstance(parsed, dict) else [])
                out = []
                for r in rows if isinstance(rows, list) else []:
                    if isinstance(r, Mapping) and r.get("early_term") and r.get("true_identity"):
                        out.append({
                            "early_term": str(r["early_term"]).strip(),
                            "true_identity": str(r["true_identity"]).strip(),
                            "relation": str(r.get("relation") or "等价").strip(),
                            "units": [int(u) for u in (r.get("units") or []) if str(u).isdigit() or isinstance(u, int)],
                        })
                return out

            pair_lists = await asyncio.gather(*(_extract_alias_chunk(c) for c in route_chunks))
            raw_aliases = [al for sub_l in pair_lists for al in sub_l]
            if raw_aliases and not _stopped() and outcome.calls < max_calls:
                vol_src = list(volume_summaries or []) or ckpt_vol_briefs
                vol_brief = "\n\n".join(str(v)[:1200] for v in vol_src)[:9500] or "（无分卷摘要）"
                al_lines = [
                    f"- {a['early_term']} -> {a['true_identity']} ({a['relation']}, units={a['units'][:3]})"
                    for a in raw_aliases[:260]
                ]
                cons_p = U1_ALIAS_CONSOLIDATE_PROMPT.format(
                    alias_count=len(al_lines),
                    volume_brief=vol_brief,
                    raw_aliases="\n".join(al_lines),
                )
                try:
                    raw_c = await _call_tracked(cons_p, purpose="u1_alias_consolidate")
                    pc = _parse_json_payload(raw_c)
                    rc = pc if isinstance(pc, list) else (pc.get("aliases") or pc.get("items") or [] if isinstance(pc, dict) else [])
                    for r in rc if isinstance(rc, list) else []:
                        if isinstance(r, Mapping) and r.get("early_term") and r.get("true_identity"):
                            alias_lexicon.append({
                                "early_term": str(r["early_term"]).strip(),
                                "true_identity": str(r["true_identity"]).strip(),
                                "relation": str(r.get("relation") or "等价").strip(),
                                "units": [int(u) for u in (r.get("units") or []) if str(u).isdigit() or isinstance(u, int)],
                            })
                except Exception:
                    pass
                if not alias_lexicon:
                    alias_lexicon = raw_aliases
            alias_done = True

        def _build_chunk_route_batches(chunk: Mapping[str, Any]) -> list[tuple[list[dict[str, Any]], str]]:
            end_u = int(chunk["end_unit"])
            elig_chunk = [x for x in eligible if int(x["setup_unit"]) - 45 <= end_u]
            if not elig_chunk:
                return []

            def _render_route(sub: Sequence[dict[str, Any]]) -> str:
                q_lines = []
                for x in sub:
                    fid = x["fs_id"]
                    sp = splits[fid]
                    ents = ",".join(str(t) for t in (sp.get("key_entities") or [])[:5])
                    anchor = str(sp.get("setup_event_anchor") or "")[:36]
                    q_lines.append(
                        f"{fid} (设于U{x['setup_unit']} | {anchor}) | {sp.get('core_question')}"
                        + (f" [{ents}]" if ents else "")
                    )
                return U1_ROUTE_PROMPT.format(
                    chunk_start=chunk["start_unit"],
                    chunk_end=chunk["end_unit"],
                    chunk_text=chunk["text"],
                    q_count=len(sub),
                    questions_text="\n".join(q_lines),
                )

            batches: list[tuple[list[dict[str, Any]], str]] = []
            pos = 0
            while pos < len(elig_chunk):
                end = min(len(elig_chunk), pos + route_q_batch)
                p_str = _render_route(elig_chunk[pos:end])
                while end > pos + 1 and len(p_str) > route_prompt_cap:
                    end -= max(1, (end - pos) // 4)
                    p_str = _render_route(elig_chunk[pos:end])
                batches.append((elig_chunk[pos:end], p_str))
                pos = end
            return batches

        pending_chunks = [c for c in route_chunks if int(c["chunk_id"]) not in done_cids]
        if pending_chunks and not _stopped() and outcome.calls < max_calls:
            chunk_batches_map = {int(c["chunk_id"]): _build_chunk_route_batches(c) for c in route_chunks}
            route_total = sum(len(bs) for bs in chunk_batches_map.values())
            route_done = sum(
                len(chunk_batches_map[int(c["chunk_id"])])
                for c in route_chunks
                if int(c["chunk_id"]) in done_cids
            )
            if route_total > 0:
                _emit_stage("u1_route", extra={
                    "checked": 0,
                    "stage_done": route_done,
                    "stage_total": route_total,
                    "remaining": max(0, route_total - route_done),
                    "unit": "批",
                })

            async def _route_chunk(chunk: Mapping[str, Any]) -> None:
                cid = int(chunk["chunk_id"])
                sub_batches = chunk_batches_map.get(cid) or []
                if not sub_batches:
                    done_cids.add(cid)
                    return

                async def _route_sub(sub: Sequence[dict[str, Any]], prompt_str: str) -> bool:
                    nonlocal route_done
                    if _stopped() or outcome.calls >= max_calls:
                        return False
                    try:
                        raw = await _call_tracked(prompt_str, purpose="u1_route")
                    except Exception:
                        return False
                    finally:
                        if not _stopped() and route_total > 0:
                            route_done = min(route_total, route_done + 1)
                            _emit_stage("u1_route", silent=True, extra={
                                "checked": 0,
                                "stage_done": route_done,
                                "stage_total": route_total,
                                "remaining": max(0, route_total - route_done),
                                "unit": "批",
                            })
                    parsed = _parse_json_payload(raw)
                    rows = (
                        parsed
                        if isinstance(parsed, list)
                        else (parsed.get("hits") or parsed.get("items") or [] if isinstance(parsed, dict) else [])
                    )
                    if not rows:
                        rows = list(_salvage_json_array_rows(raw).values())
                    sub_ids = {x["fs_id"] for x in sub}
                    for r in rows if isinstance(rows, list) else []:
                        if not isinstance(r, Mapping):
                            continue
                        rfid = str(r.get("fs_id") or "")
                        if rfid not in sub_ids:
                            continue
                        us = []
                        for u in r.get("units") or []:
                            try:
                                ui = int(u)
                                if ui > 0:
                                    us.append(ui)
                            except (TypeError, ValueError):
                                pass
                        if us:
                            routed_hits[rfid].append({
                                "chunk_id": cid,
                                "units": us,
                                "clue": str(r.get("clue") or "")[:140],
                            })
                    return True

                sub_results = await asyncio.gather(*(_route_sub(sb, p_str) for sb, p_str in sub_batches))
                if all(sub_results):
                    done_cids.add(cid)

            await asyncio.gather(*(_route_chunk(c) for c in pending_chunks))

    if u1_cache_path is not None and not _stopped():
        try:
            u1_cache_path.parent.mkdir(parents=True, exist_ok=True)
            safe_save_json({
                "fingerprint": index_fp,
                "splits": splits,
                "alias_lexicon": alias_lexicon,
                "alias_done": alias_done,
                "routed_hits": dict(routed_hits),
                "chunks_done": sorted(done_cids),
            }, u1_cache_path, ensure_ascii=False)
        except Exception as exc:
            logger.warning("保存 V3 U1 缓存失败: %s", exc)

    # ---------- Stage U2: 本地高精度召回、严格前置门控与完整章节打包 ----------
    max_u = max(chapters_map)
    packs: dict[str, dict[str, Any]] = {}
    state: dict[str, dict[str, Any]] = {}

    for x in eligible:
        fid = x["fs_id"]
        it = x["item"]
        setup_unit = x["setup_unit"]
        setup_after_offset = x["setup_after_offset"]
        sp = splits[fid]
        core_q = str(sp.get("core_question") or getattr(it, "question", "") or getattr(it, "description", ""))
        bonus_asp = str(sp.get("bonus_aspects") or "")
        event_anchor = str(sp.get("setup_event_anchor") or getattr(it, "description", "") or "")[:90]
        needs_backref = bool(sp.get("needs_pre_setup_backref"))
        allow_pre_mode = bool(small_corpus or needs_backref)
        key_ents = [str(t) for t in (sp.get("key_entities") or []) if t]
        is_high_imp = str(getattr(it, "importance", "") or "") == "高"
        item_max_post = min(3, max_post_windows) if (mid_single_hop and not is_high_imp) else max_post_windows

        alias_terms, alias_hint, alias_units = _match_aliases_for_item(
            it, sp, alias_lexicon, protagonist_stops=protagonist_stops
        )
        q_extra_terms = _keywords_for(
            core_q,
            str(getattr(it, "anchor_quote", "") or ""),
            str(getattr(it, "description", "") or "")[:140],
            str(getattr(it, "answer_criteria", "") or ""),
        )
        disc_terms = _filter_discriminative_terms(
            chapters_map, index, key_ents + alias_terms + q_extra_terms, max_df_ratio=0.35
        )

        post_scores: dict[int, float] = {}
        post_srcs: dict[int, list[str]] = defaultdict(list)
        pre_scores: dict[int, float] = {}
        pre_srcs: dict[int, list[str]] = defaultdict(list)

        def _can_use_setup() -> bool:
            if setup_after_offset is None:
                return False
            return (len(chapters_map.get(setup_unit, "")) - int(setup_after_offset)) > 350

        def _bump(u: int, pts: float, src: str, *, allow_pre: bool = False) -> None:
            if u not in chapters_map:
                return
            if u == setup_unit and not _can_use_setup():
                return
            if u < setup_unit:
                if not allow_pre or not allow_pre_mode or u < max(1, setup_unit - 45):
                    return
                old = pre_scores.get(u, 0.0)
                bonus = 0.25 if src not in pre_srcs[u] else 0.0
                if src not in pre_srcs[u]:
                    pre_srcs[u].append(src)
                pre_scores[u] = max(old, pts) + bonus
                return
            old = post_scores.get(u, 0.0)
            bonus = 0.25 if src not in post_srcs[u] else 0.0
            if src not in post_srcs[u]:
                post_srcs[u].append(src)
            post_scores[u] = max(old, pts) + bonus

        if small_corpus:
            for cu in sorted(chapters_map):
                if cu >= setup_unit:
                    _bump(cu, 12.0, "全量小语料")
                else:
                    _bump(cu, 8.0, "全量小语料前文", allow_pre=True)

        u1_clues: list[str] = []
        for hit in routed_hits.get(fid, []):
            clue = str(hit.get("clue") or "")
            us = [int(u) for u in (hit.get("units") or [])]
            if clue and us:
                u1_clues.append(f"U{us[0]}:{clue}")
            clue_terms = _filter_discriminative_terms(chapters_map, index, _keywords_for(clue), max_df_ratio=0.45)
            weighted_terms = (
                [(t, 3.5) for t in clue_terms[:10]]
                + [(t, 2.2) for t in alias_terms[:8]]
                + [(t, 2.0) for t in key_ents[:6] if t not in protagonist_stops and t not in _GENERIC_ENTITY_STOP]
                + [(t, 1.0) for t in disc_terms[:8]]
            )
            seen_spans: set[tuple[int, int]] = set()
            for hu in us:
                blk = unit_to_block.get(hu)
                b_start, b_end = (blk[0], blk[1]) if blk else (max(1, hu - 1), min(max_u, hu + 1))
                span_key = (b_start, min(max_u, b_end + 2))
                if span_key in seen_spans:
                    continue
                seen_spans.add(span_key)
                post_ch: list[tuple[float, bool, int]] = []
                pre_ch: list[tuple[float, bool, int]] = []
                for cu in range(span_key[0], span_key[1] + 1):
                    if cu not in chapters_map:
                        continue
                    is_direct = cu in us
                    if cu >= setup_unit:
                        txt = (
                            chapters_map[cu][int(setup_after_offset) + 200 :]
                            if (cu == setup_unit and setup_after_offset is not None)
                            else chapters_map[cu]
                        )
                        post_ch.append((_score_chapter_against_terms(txt, weighted_terms), is_direct, cu))
                    elif allow_pre_mode and cu >= max(1, setup_unit - 45):
                        sc = _score_chapter_against_terms(chapters_map[cu], weighted_terms)
                        if is_direct or sc >= 4.0:
                            pre_ch.append((sc, is_direct, cu))

                post_ch.sort(key=lambda r: (-r[0], -int(r[1]), r[2]))
                if post_ch and post_ch[0][0] > 0:
                    top1_u = post_ch[0][2]
                    _bump(top1_u, 12.0, "U1块精排Top1")
                    for sc, _, cu in post_ch[1:3]:
                        if sc > 0:
                            _bump(cu, 9.5, "U1块精排Top2")
                    for nu in _expand_units(chapters_map, [top1_u], min_unit=setup_unit + 1, radius=1):
                        if nu != top1_u:
                            _bump(nu, 6.5, "U1精排邻章")
                else:
                    for hu_in in us:
                        if hu_in >= setup_unit:
                            _bump(hu_in, 10.0, "U1路由")
                            for nu in _expand_units(chapters_map, [hu_in], min_unit=setup_unit + 1, radius=1):
                                if nu != hu_in:
                                    _bump(nu, 5.5, "U1邻章")

                if allow_pre_mode:
                    pre_ch.sort(key=lambda r: (-r[0], -int(r[1]), r[2]))
                    for idx_p, (sc, is_dir, cu) in enumerate(pre_ch[:2]):
                        if is_dir or sc >= 4.0:
                            _bump(cu, 11.0 if idx_p == 0 else 8.5, "U1前驱精排", allow_pre=True)

        # Stage 1 recon 指针（同时兼容账本自述 resolved_chapter）
        recon_notes: list[str] = []
        recon_seeds: list[int] = []
        for claim in recon_claims.get(fid, []):
            rc = claim.get("resolved_chapter")
            if claim.get("resolution_summary"):
                recon_notes.append(f"[Stage1自述] {claim['resolution_summary'][:130]}")
            for u in _disambiguate_chapter_to_units(chapters_map, labels, label_num_to_units, rc, after_unit=setup_unit):
                if u not in recon_seeds:
                    recon_seeds.append(u)
        lrc = getattr(it, "resolved_chapter", None) or 0
        if lrc:
            for u in _disambiguate_chapter_to_units(chapters_map, labels, label_num_to_units, lrc, after_unit=setup_unit):
                if u not in recon_seeds:
                    recon_seeds.append(u)
        if getattr(it, "resolution", None) and not recon_notes:
            recon_notes.append(f"[账本自述] {str(it.resolution)[:130]}")

        for su_seed in recon_seeds:
            _bump(su_seed, 8.5, "Stage1指针")
            for nu in _expand_units(chapters_map, [su_seed], min_unit=setup_unit + 1, radius=2):
                if nu != su_seed:
                    _bump(nu, 5.0, "Stage1邻章")

        for au in alias_units:
            if au > setup_unit:
                _bump(au, 7.5, "别名揭示点")
            elif allow_pre_mode and au >= max(1, setup_unit - 45):
                _bump(au, 7.5, "别名先前提及", allow_pre=True)

        if not small_corpus:
            for rank, (lu, _sc) in enumerate(_find_mentions_in_range(chapters_map, index, disc_terms, min_u=setup_unit + 1, max_u=max_u, limit=10)):
                _bump(lu, max(2.5, 6.5 - rank * 0.45), "词频检索")
                if rank < 2:
                    for nu in _expand_units(chapters_map, [lu], min_unit=setup_unit + 1, radius=1):
                        if nu != lu:
                            _bump(nu, 3.8, "词频邻章")

            digest_terms = [t for t in disc_terms[:8] if len(t) >= 2]
            if digest_terms:
                w_digest = [(t, 2.5 if t in alias_terms or t in key_ents else 1.5) for t in digest_terms]
                for b_start, b_end, d_txt in block_tuples:
                    if b_end <= setup_unit:
                        continue
                    if _score_chapter_against_terms(d_txt, w_digest) >= 4.0:
                        best_cu, best_rsc = None, -1.0
                        for cu in range(max(setup_unit + 1, b_start), min(max_u, b_end + 1) + 1):
                            if cu in chapters_map:
                                rsc = _score_chapter_against_terms(chapters_map[cu], w_digest)
                                if rsc > best_rsc:
                                    best_rsc, best_cu = rsc, cu
                        if best_cu is not None:
                            _bump(best_cu, 5.8, "摘要匹配精排")

            if needs_backref:
                pre_lo = max(1, setup_unit - 45)
                if pre_lo < setup_unit:
                    for rank, (pu, _raw_idf) in enumerate(_find_mentions_in_range(chapters_map, index, disc_terms, min_u=pre_lo, max_u=setup_unit - 1, limit=6)):
                        _bump(pu, max(3.5, 7.5 - rank * 0.8), "前驱词频", allow_pre=True)

        post_cands = [(u, sc, post_srcs[u]) for u, sc in post_scores.items()]
        chosen_post = _nms_episode_select(post_cands, max_windows=item_max_post, cluster_radius=3, max_per_cluster=2)

        chosen_pre: list[tuple[int, float, list[str]]] = []
        if allow_pre_mode:
            pre_cands = sorted([(u, sc, pre_srcs[u]) for u, sc in pre_scores.items()], key=lambda r: (-r[1], r[0]))
            if pre_cands:
                chosen_pre = pre_cands[:max_pre_windows]

        allowed_post: dict[int, str] = {}
        allowed_pre: dict[int, str] = {}
        selected_post: list[int] = []
        selected_pre: list[int] = []
        segments: dict[int, str] = {}

        for u, _sc, srcs in chosen_post:
            min_off = (int(setup_after_offset) + 200) if (u == setup_unit and setup_after_offset is not None) else 0
            seg = format_unit_segment(
                chapters_map, labels, unit_headers, u,
                src="+".join(srcs[:3]), focus_terms=disc_terms,
                min_offset=min_off, is_pre_setup=False,
                max_chars=max_chapter_chars, unit_noun=unit_noun,
            )
            if seg:
                allowed_post[u] = chapters_map[u]
                selected_post.append(u)
                segments[u] = seg

        for u, _sc, srcs in chosen_pre:
            seg = format_unit_segment(
                chapters_map, labels, unit_headers, u,
                src="+".join(srcs[:3]), focus_terms=disc_terms,
                min_offset=0, is_pre_setup=True,
                max_chars=max_chapter_chars, unit_noun=unit_noun,
            )
            if seg:
                allowed_pre[u] = chapters_map[u]
                selected_pre.append(u)
                segments[u] = seg

        bundle_parts = [segments[u] for u in (selected_post + selected_pre) if u in segments]
        packs[fid] = {
            "core_question": core_q,
            "bonus_aspects": bonus_asp,
            "setup_event_anchor": event_anchor,
            "needs_pre_setup_backref": needs_backref,
            "key_entities": key_ents,
            "alias_terms": alias_terms,
            "alias_hint": alias_hint,
            "u1_clues": u1_clues[:6],
            "recon_note": "\n".join(recon_notes),
            "disc_terms": disc_terms,
            "bundle_text": "\n\n".join(bundle_parts) if bundle_parts else "（暂未检索到后文原文窗口）",
        }
        state[fid] = {
            "allowed_post": allowed_post,
            "allowed_pre": allowed_pre,
            "selected_post": selected_post,
            "selected_pre": selected_pre,
            "segments": segments,
            "hops_used": 1,
            "detective_res": {},
        }

    # ---------- Stage U3: 共享章节证据池 + 单阶段联合终审 + 有界 3 跳 ----------
    step_bs = (
        max(1, int(batch_size))
        if batch_size is not None
        else (DEFAULT_V3_COMPACT_BATCH_SIZE if compact_mode else DEFAULT_V3_BATCH_SIZE)
    )
    bin_pool_units_by_item: dict[str, set[int]] = defaultdict(set)

    async def _run_shared_pool_bins(
        bins: Sequence[list[tuple[dict[str, Any], list[int], list[int]]]],
        prompt_builder: Callable[[Sequence[tuple[dict[str, Any], list[int], list[int]]]], str],
        *,
        purpose: str,
        stage_name: str,
        total_items: int,
        base_checked: int = 0,
        hop: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        omitted_tups: list[tuple[dict[str, Any], list[int], list[int]]] = []
        done_cnt = 0

        async def _call_bin(
            bn: Sequence[tuple[dict[str, Any], list[int], list[int]]],
            call_purpose: str,
            *,
            emit_progress: bool,
        ) -> tuple[Sequence[tuple[dict[str, Any], list[int], list[int]]], dict[str, dict[str, Any]], bool]:
            nonlocal done_cnt
            pool_u = {u for _x, pu, preu in bn for u in (list(pu) + list(preu))}
            for x_item, _, _ in bn:
                bin_pool_units_by_item[x_item["fs_id"]].update(pool_u)
            if _stopped() or outcome.calls >= max_calls:
                return bn, {}, True
            exp_ids = {x_item["fs_id"] for x_item, _, _ in bn}
            try:
                raw = await _call_tracked(prompt_builder(bn), purpose=call_purpose)
            except (asyncio.CancelledError, RuntimeError):
                return bn, {}, True
            except Exception:
                outcome.failed_batches += 1
                return bn, {}, True
            finally:
                if emit_progress and not _stopped():
                    done_cnt = min(total_items, done_cnt + len(bn))
                    extra_ev: dict[str, Any] = {
                        "checked": base_checked + done_cnt,
                        "stage_done": done_cnt,
                        "stage_total": total_items,
                        "remaining": max(0, total_items - done_cnt),
                        "unit": "条",
                    }
                    if hop is not None:
                        extra_ev["hop"] = hop
                    _emit_stage(stage_name, silent=True, extra=extra_ev)

            parsed = _parse_rows_by_id(raw)
            if len(parsed) < len(exp_ids):
                for k, v in _salvage_json_array_rows(raw).items():
                    if k not in parsed:
                        parsed[k] = v
            if len(bn) == 1 and not any(fid in parsed for fid in exp_ids):
                raw_obj = _parse_json_payload(raw)
                cand = (
                    raw_obj[0]
                    if isinstance(raw_obj, list) and raw_obj
                    else (raw_obj if isinstance(raw_obj, dict) else None)
                )
                if isinstance(cand, Mapping):
                    row_copy = dict(cand)
                    sole_fid = bn[0][0]["fs_id"]
                    row_copy["fs_id"] = sole_fid
                    parsed[sole_fid] = row_copy
            return bn, {fid: r for fid, r in parsed.items() if fid in exp_ids}, False

        res_list = await asyncio.gather(*(_call_bin(b, purpose, emit_progress=True) for b in bins))
        for bn, matched, call_aborted in res_list:
            merged.update(matched)
            if call_aborted:
                continue
            for tup in bn:
                if tup[0]["fs_id"] not in matched and (len(bn) > 1 or not matched):
                    omitted_tups.append(tup)

        if omitted_tups and not _stopped() and outcome.calls < max_calls:
            outcome.retried_batches += 1
            retry_bins = [[tup] for tup in omitted_tups]
            r_list = await asyncio.gather(
                *(_call_bin(rb, f"{purpose}_retry", emit_progress=False) for rb in retry_bins)
            )
            for _bn, matched, _aborted in r_list:
                merged.update(matched)
        return merged

    if not _stopped() and outcome.calls < max_calls:
        hop1_total = len(eligible)
        _emit_stage("u3_hop", extra={
            "hop": 1,
            "checked": 0,
            "stage_done": 0,
            "stage_total": hop1_total,
            "remaining": hop1_total,
            "unit": "条",
        })
        unit_lists_h1 = {
            x["fs_id"]: (list(state[x["fs_id"]]["selected_post"]), list(state[x["fs_id"]]["selected_pre"]))
            for x in eligible
        }
        hop1_bins = _pack_bins_overlap_pool(
            chapters_map,
            eligible,
            unit_lists_h1,
            packs,
            max_k=step_bs,
            max_chars=pool_max_chars,
            max_chapter_chars=max_chapter_chars,
            prompt_overhead_chars=5500,
        )

        def _build_h1_bin(bn: Sequence[tuple[dict[str, Any], list[int], list[int]]]) -> str:
            pool_txt, _ = _render_shared_pool_for_bin(
                chapters_map, labels, unit_headers, bn, packs,
                max_chapter_chars=max_chapter_chars, unit_noun=unit_noun,
            )
            cards = [
                _render_detective_card_pool(x, packs[x["fs_id"]], pu, preu, 1)
                for x, pu, preu in bn
            ]
            return U3_POOL_SINGLE_STAGE_PROMPT.format(
                count=len(bn), shared_pool_text=pool_txt, items_block="\n\n".join(cards),
            )

        h1_res = await _run_shared_pool_bins(
            hop1_bins,
            _build_h1_bin,
            purpose="u3_hop1",
            stage_name="u3_hop",
            total_items=hop1_total,
            base_checked=0,
            hop=1,
        )
        for fid, row in h1_res.items():
            state[fid]["detective_res"] = row

    for hop_idx, max_new_u in ((2, hop2_new_cap), (3, hop3_new_cap)):
        if _stopped() or outcome.calls >= max_calls:
            break
        need_hop: list[dict[str, Any]] = []
        hop_unit_lists: dict[str, tuple[list[int], list[int]]] = {}

        for x in eligible:
            if mid_single_hop and str(getattr(x["item"], "importance", "") or "") != "高":
                continue
            fid = x["fs_id"]
            st = state[fid]
            r_prev = st["detective_res"] or {}
            v_prev = _normalize_verdict(str(r_prev.get("verdict") or "UNRESOLVED"))
            conf_prev = str(r_prev.get("confidence") or "medium").lower()
            fu = r_prev.get("follow_up")
            has_fu = isinstance(fu, Mapping) and (bool(fu.get("keyword_queries")) or bool(fu.get("unit_ranges")))

            if hop_idx == 2:
                should_hop = (v_prev == "UNRESOLVED") or has_fu or (v_prev != "RESOLVED_EXPLICIT" and conf_prev != "high")
                if v_prev == "RESOLVED_EXPLICIT" and conf_prev == "high" and not has_fu:
                    should_hop = False
            else:
                should_hop = (st["hops_used"] == 2) and has_fu and (v_prev in ("UNRESOLVED", "PARTIALLY_RESOLVED") or conf_prev != "high")

            if not should_hop:
                continue

            seen_all = set(st["selected_post"]) | set(st["selected_pre"])
            pk = packs[fid]
            su_item = x["setup_unit"]
            sa_off = x["setup_after_offset"]
            needs_backref = bool(pk["needs_pre_setup_backref"])
            allow_pre_hop = bool(needs_backref or small_corpus)

            raw_queries: list[str] = []
            unit_ranges: list[tuple[int, int]] = []
            if isinstance(fu, Mapping):
                raw_queries = [str(q).strip() for q in (fu.get("keyword_queries") or []) if str(q).strip()]
                for rng in fu.get("unit_ranges") or []:
                    if isinstance(rng, (list, tuple)) and rng:
                        try:
                            a = int(rng[0])
                            b = int(rng[1]) if len(rng) > 1 else a
                            lo, hi = min(a, b), max(a, b)
                            unit_ranges.append((lo, min(hi, lo + 8)))
                        except (TypeError, ValueError):
                            pass
                    elif isinstance(rng, (int, str)) and str(rng).isdigit():
                        v = int(rng)
                        unit_ranges.append((v, v))

            q_terms = _filter_discriminative_terms(
                chapters_map,
                index,
                raw_queries + pk["alias_terms"] + pk["key_entities"] + pk["disc_terms"][:6],
                max_df_ratio=0.38,
            ) or [q for q in raw_queries if len(q) >= 2]

            post_c, pre_c = [], []
            added_seen: set[int] = set()

            def _ok_u(u: int) -> bool:
                if u not in chapters_map or u in seen_all or u in added_seen:
                    return False
                if u == su_item and (sa_off is None or (len(chapters_map[u]) - int(sa_off)) <= 350):
                    return False
                return True

            for lo_r, hi_r in unit_ranges:
                for ch_req in range(max(1, lo_r), min(max_u, hi_r) + 1):
                    cand_us = [ch_req] + [
                        m for m in _disambiguate_chapter_to_units(
                            chapters_map, labels, label_num_to_units, ch_req, after_unit=max(0, su_item - 46)
                        ) if m != ch_req
                    ]
                    for cu in cand_us:
                        if not _ok_u(cu):
                            continue
                        if cu >= su_item:
                            added_seen.add(cu)
                            post_c.append((cu, 12.5, ["侦探点名范围"]))
                        elif allow_pre_hop and cu >= max(1, su_item - 45):
                            added_seen.add(cu)
                            pre_c.append((11.5, cu, "侦探前驱范围"))

            if q_terms:
                for rank, (lu, _sc) in enumerate(_find_mentions_in_range(chapters_map, index, q_terms, min_u=su_item + 1, max_u=max_u, limit=10)):
                    if _ok_u(lu):
                        added_seen.add(lu)
                        post_c.append((lu, max(3.5, 9.5 - rank * 0.6), ["多跳原文词检"]))
                    if rank < 2:
                        for nu in _expand_units(chapters_map, [lu], min_unit=su_item + 1, radius=1):
                            if _ok_u(nu):
                                added_seen.add(nu)
                                post_c.append((nu, 5.5, ["多跳邻章"]))
                if allow_pre_hop:
                    pre_lo = max(1, su_item - 45)
                    if pre_lo < su_item:
                        for rank, (pu, _sc) in enumerate(_find_mentions_in_range(chapters_map, index, q_terms, min_u=pre_lo, max_u=su_item - 1, limit=4)):
                            if _ok_u(pu):
                                added_seen.add(pu)
                                pre_c.append((max(4.0, 8.0 - rank * 0.8), pu, "多跳前驱词检"))

            post_nms = _nms_episode_select(post_c, max_windows=max_new_u, cluster_radius=3, max_per_cluster=2)
            pre_sorted = sorted(pre_c, key=lambda r: (-r[0], r[1])) if allow_pre_hop else []

            nu_post: list[int] = []
            nu_pre: list[int] = []
            for u, _sc, srcs in post_nms:
                min_off = (int(sa_off) + 200) if (u == su_item and sa_off is not None) else 0
                seg = format_unit_segment(
                    chapters_map, labels, unit_headers, u,
                    src=srcs[0], focus_terms=q_terms, min_offset=min_off,
                    is_pre_setup=False, max_chars=max_chapter_chars, unit_noun=unit_noun,
                )
                if seg:
                    st["allowed_post"][u] = chapters_map[u]
                    st["selected_post"].append(u)
                    st["segments"][u] = seg
                    nu_post.append(u)
            if allow_pre_hop:
                pre_budget = max(0, max_pre_windows - len(st["selected_pre"]))
                for _sc, pu, src in pre_sorted[:pre_budget]:
                    seg = format_unit_segment(
                        chapters_map, labels, unit_headers, pu,
                        src=src, focus_terms=q_terms, min_offset=0,
                        is_pre_setup=True, max_chars=max_chapter_chars, unit_noun=unit_noun,
                    )
                    if seg:
                        st["allowed_pre"][pu] = chapters_map[pu]
                        st["selected_pre"].append(pu)
                        st["segments"][pu] = seg
                        nu_pre.append(pu)

            if not (nu_post or nu_pre):
                continue

            st["hops_used"] = hop_idx
            cited_u = []
            for e in r_prev.get("evidence_chain") or []:
                if isinstance(e, Mapping):
                    cu = _safe_int_unit(e.get("unit_id"))
                    if cu is not None:
                        cited_u.append(cu)
            focus_u = cited_u + nu_post + nu_pre + st["selected_post"][:5] + st["selected_pre"][:1]
            f_post = list(dict.fromkeys(u for u in focus_u if u in st["allowed_post"]))
            f_pre = list(dict.fromkeys(u for u in focus_u if u in st["allowed_pre"] and u not in f_post))
            hop_unit_lists[fid] = (f_post, f_pre)
            need_hop.append(x)

        if not need_hop:
            break
        hn_total = len(need_hop)
        _emit_stage("u3_hop", extra={
            "hop": hop_idx,
            "checked": len(eligible) - hn_total,
            "stage_done": 0,
            "stage_total": hn_total,
            "remaining": hn_total,
            "unit": "条",
        })
        hn_bins = _pack_bins_overlap_pool(
            chapters_map,
            need_hop,
            hop_unit_lists,
            packs,
            max_k=step_bs,
            max_chars=pool_max_chars,
            max_chapter_chars=max_chapter_chars,
            prompt_overhead_chars=6000,
        )

        def _build_hn_bin(
            bn: Sequence[tuple[dict[str, Any], list[int], list[int]]],
            *,
            _hi: int = hop_idx,
        ) -> str:
            pool_txt, _ = _render_shared_pool_for_bin(
                chapters_map, labels, unit_headers, bn, packs,
                max_chapter_chars=max_chapter_chars, unit_noun=unit_noun,
            )
            cards = [
                _render_detective_card_pool(x, packs[x["fs_id"]], pu, preu, _hi, state[x["fs_id"]]["detective_res"])
                for x, pu, preu in bn
            ]
            return U3_POOL_SINGLE_STAGE_PROMPT.format(
                count=len(bn), shared_pool_text=pool_txt, items_block="\n\n".join(cards),
            )

        hn_res = await _run_shared_pool_bins(
            hn_bins,
            _build_hn_bin,
            purpose=f"u3_hop{hop_idx}",
            stage_name="u3_hop",
            total_items=hn_total,
            base_checked=len(eligible) - hn_total,
            hop=hop_idx,
        )
        for fid, new_r in hn_res.items():
            if not new_r:
                continue
            old_r = state[fid]["detective_res"] or {}
            new_v = _normalize_verdict(str(new_r.get("verdict") or "UNRESOLVED"))
            old_v = _normalize_verdict(str(old_r.get("verdict") or "UNRESOLVED"))
            if new_v != "UNRESOLVED" or old_v == "UNRESOLVED" or new_r.get("follow_up"):
                state[fid]["detective_res"] = new_r

    # U3 完成后先执行共享池跨题章节继受，再构建 U4 / U5 审计窗口
    for x in eligible:
        fid = x["fs_id"]
        rd_u3 = state[fid]["detective_res"]
        if rd_u3:
            _inherit_cross_pool_units(
                chapters_map, labels, unit_headers, x, packs[fid], state[fid], rd_u3,
                bin_pool_units_by_item[fid],
                max_chapter_chars=max_chapter_chars, unit_noun=unit_noun,
            )

    audit_bundles: dict[str, str] = {}
    audit_unit_lists: dict[str, tuple[list[int], list[int]]] = {}
    for x in eligible:
        fid = x["fs_id"]
        st = state[fid]
        rd = st["detective_res"] or {}
        if rd:
            b_txt, u_list = _build_audit_bundle_and_lists(
                st, rd, max_windows=max_audit_windows, fallback_bundle=packs[fid]["bundle_text"],
            )
            audit_bundles[fid] = b_txt
            audit_unit_lists[fid] = u_list

    # ---------- Stage U4: 可选灰区四防对称终审（默认关闭即 Tier2_NoU4，开启即 Tier2_GrayU4） ----------
    u4_map: dict[str, dict[str, Any]] = {}
    if enable_gray_u4 and not _stopped() and outcome.calls < max_calls:
        gray_items: list[dict[str, Any]] = []
        for x in eligible:
            fid = x["fs_id"]
            st = state[fid]
            pk = packs[fid]
            rd = st["detective_res"] or {}
            if not rd:
                continue
            raw_v = _normalize_verdict(str(rd.get("verdict") or "UNRESOLVED"))
            u3_conf = str(rd.get("confidence") or "").lower()
            ver_ev_pre, ev_ok_pre, _ = verify_evidence_chain_v3(
                chapters_map,
                x["item"],
                setup_unit=x["setup_unit"],
                raw_verdict=raw_v,
                raw_evidence_chain=rd.get("evidence_chain"),
                allowed_post_units=st["allowed_post"],
                allowed_pre_units=st["allowed_pre"],
                setup_after_offset=x["setup_after_offset"],
                needs_pre_setup_backref=pk["needs_pre_setup_backref"],
            )
            skip_u4 = (
                raw_v == "RESOLVED_EXPLICIT"
                and u3_conf == "high"
                and st["hops_used"] == 1
                and ev_ok_pre
                and any(int(e["unit_id"]) >= int(x["setup_unit"]) for e in ver_ev_pre)
            )
            if skip_u4:
                continue
            has_signal = bool(pk["u1_clues"] or pk["recon_note"] or st["hops_used"] > 1 or rd.get("evidence_chain"))
            if raw_v != "UNRESOLVED" or has_signal:
                gray_items.append(x)

        if gray_items and not _stopped() and outcome.calls < max_calls:
            u4_total = len(gray_items)
            _emit_stage("u4_audit", extra={
                "checked": len(eligible) - u4_total,
                "stage_done": 0,
                "stage_total": u4_total,
                "remaining": u4_total,
                "unit": "条",
            })
            u4_unit_lists = {x["fs_id"]: audit_unit_lists[x["fs_id"]] for x in gray_items}
            u4_bins = _pack_bins_overlap_pool(
                chapters_map,
                gray_items,
                u4_unit_lists,
                packs,
                max_k=step_bs,
                max_chars=pool_max_chars,
                max_chapter_chars=max_chapter_chars,
                prompt_overhead_chars=6000,
            )

            def _build_u4_bin(bn: Sequence[tuple[dict[str, Any], list[int], list[int]]]) -> str:
                pool_txt, _ = _render_shared_pool_for_bin(
                    chapters_map, labels, unit_headers, bn, packs,
                    max_chapter_chars=max_chapter_chars, unit_noun=unit_noun,
                )
                cards = [
                    _render_u4_card_pool(x, packs[x["fs_id"]], state[x["fs_id"]]["detective_res"] or {}, pu, preu)
                    for x, pu, preu in bn
                ]
                return U4_POOL_AUDITOR_PROMPT.format(
                    count=len(bn), shared_pool_text=pool_txt, items_block="\n\n".join(cards),
                )

            u4_map = await _run_shared_pool_bins(
                u4_bins,
                _build_u4_bin,
                purpose="u4_audit",
                stage_name="u4_audit",
                total_items=u4_total,
                base_checked=len(eligible) - u4_total,
            )
            for x in gray_items:
                fid = x["fs_id"]
                if fid in u4_map:
                    _inherit_cross_pool_units(
                        chapters_map, labels, unit_headers, x, packs[fid], state[fid], u4_map[fid],
                        bin_pool_units_by_item[fid],
                        max_chapter_chars=max_chapter_chars, unit_noun=unit_noun,
                    )
                    b_txt, u_list = _build_audit_bundle_and_lists(
                        state[fid], u4_map[fid], max_windows=max_audit_windows, fallback_bundle=packs[fid]["bundle_text"],
                    )
                    audit_bundles[fid] = b_txt
                    audit_unit_lists[fid] = u_list

    # ---------- Stage U5: 确定性引文校验 + 1 轮精确修复 + 兼容写入 RecheckOutcome & 账本 ----------
    tier_counts: Counter[str] = Counter()
    done_ids: set[str] = set()
    repair_queue: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    final_rows: dict[str, dict[str, Any]] = {}

    for x in eligible:
        fid = x["fs_id"]
        st = state[fid]
        pk = packs[fid]
        r4 = u4_map.get(fid) or st["detective_res"]
        if not r4:
            continue
        raw_v = _normalize_verdict(str(r4.get("verdict") or "UNRESOLVED"))
        ver_ev, ev_ok, demote_reason = verify_evidence_chain_v3(
            chapters_map,
            x["item"],
            setup_unit=x["setup_unit"],
            raw_verdict=raw_v,
            raw_evidence_chain=r4.get("evidence_chain"),
            allowed_post_units=st["allowed_post"],
            allowed_pre_units=st["allowed_pre"],
            setup_after_offset=x["setup_after_offset"],
            needs_pre_setup_backref=pk["needs_pre_setup_backref"],
        )
        if raw_v != "UNRESOLVED" and not ev_ok and fid in audit_bundles and not _stopped() and outcome.calls < max_calls:
            repair_queue.append((x, dict(r4), demote_reason))
            continue

        final_v = raw_v if ev_ok else "UNRESOLVED"
        sub_type = "" if final_v != "UNRESOLVED" else str(r4.get("unresolved_type") or "UNFILLED_PIT")
        final_rows[fid] = {
            "verdict": final_v,
            "unresolved_type": sub_type,
            "answer": str(r4.get("answer") or "").strip(),
            "evidence_chain": ver_ev,
            "gate_demoted": (raw_v != "UNRESOLVED" and not ev_ok),
            "gate_reason": demote_reason if not ev_ok else "",
            "repaired": False,
        }

    if repair_queue and not _stopped() and outcome.calls < max_calls:
        u5_total = len(repair_queue)
        u5_done = 0
        _emit_stage("u5_repair", extra={
            "checked": len(final_rows),
            "stage_done": 0,
            "stage_total": u5_total,
            "remaining": u5_total,
            "unit": "条",
        })

        async def _repair_one(x: dict[str, Any], r4: dict[str, Any], demote_reason: str) -> None:
            nonlocal u5_done
            fid = x["fs_id"]
            pk = packs[fid]
            st = state[fid]
            prompt = U5_QUOTE_REPAIR_PROMPT.format(
                fs_id=fid,
                verdict=r4.get("verdict"),
                demote_reason=demote_reason,
                core_question=pk["core_question"],
                setup_event_anchor=pk["setup_event_anchor"],
                prev_answer=str(r4.get("answer") or "")[:300],
                bundle_text=audit_bundles[fid],
            )
            r_rep = dict(r4)
            try:
                raw_rep = await _call_tracked(prompt, purpose="u5_repair")
                parsed_rep = _parse_json_payload(raw_rep)
                if isinstance(parsed_rep, list) and parsed_rep:
                    r_rep = dict(parsed_rep[0])
                elif isinstance(parsed_rep, Mapping):
                    r_rep = dict(parsed_rep)
            except Exception:
                pass
            raw_v = _normalize_verdict(str(r_rep.get("verdict") or "UNRESOLVED"))
            ver_ev, ev_ok, demote2 = verify_evidence_chain_v3(
                chapters_map,
                x["item"],
                setup_unit=x["setup_unit"],
                raw_verdict=raw_v,
                raw_evidence_chain=r_rep.get("evidence_chain"),
                allowed_post_units=st["allowed_post"],
                allowed_pre_units=st["allowed_pre"],
                setup_after_offset=x["setup_after_offset"],
                needs_pre_setup_backref=pk["needs_pre_setup_backref"],
            )
            final_v = raw_v if ev_ok else "UNRESOLVED"
            sub_type = "" if final_v != "UNRESOLVED" else str(r_rep.get("unresolved_type") or "UNFILLED_PIT")
            final_rows[fid] = {
                "verdict": final_v,
                "unresolved_type": sub_type,
                "answer": str(r_rep.get("answer") or r4.get("answer") or "").strip(),
                "evidence_chain": ver_ev,
                "gate_demoted": (raw_v != "UNRESOLVED" and not ev_ok),
                "gate_reason": demote2 if not ev_ok else "",
                "repaired": True,
            }
            if not _stopped():
                u5_done = min(u5_total, u5_done + 1)
                _emit_stage("u5_repair", silent=True, extra={
                    "checked": len(final_rows),
                    "stage_done": u5_done,
                    "stage_total": u5_total,
                    "remaining": max(0, u5_total - u5_done),
                    "unit": "条",
                })

        await asyncio.gather(*(_repair_one(x, r4, rs) for x, r4, rs in repair_queue), return_exceptions=True)

    # 汇总落账与写入兼容 RecheckOutcome（兑现章优先锚定）
    for x in eligible:
        fid = x["fs_id"]
        if fid not in final_rows:
            continue
        row = final_rows[fid]
        st = state[fid]
        it = x["item"]
        tier = row["verdict"]
        tier_counts[tier] += 1
        done_ids.add(fid)

        ev_list = row["evidence_chain"]
        is_resolved = tier in ("RESOLVED_EXPLICIT", "RESOLVED_IMPLICIT", "PARTIALLY_RESOLVED")
        if is_resolved and ev_list:
            best_ev = _select_primary_evidence(ev_list, x["setup_unit"]) or ev_list[0]
            best_u = int(best_ev["unit_id"])
            best_quote = str(best_ev["quote"])
            best_off = int(best_ev["offset"])
            eid = _evidence_id(best_u, chapters_map.get(best_u, ""))
            cv = CandidateVerdict(
                candidate_id=fid,
                verdict=SUPPORTED,
                answer=row["answer"],
                evidence_id=eid,
                supporting_span=(best_off, best_off + len(best_quote)),
                counter_search_performed=True,
                located_span=(best_off, best_off + len(best_quote)),
                quote_hash=sha256_text(best_quote)[:12],
                quote=best_quote,
            )
            fallback_ch = int(getattr(it, "anchor_chapter", 0) or getattr(it, "first_seen_chapter", 0) or 0)
            outcome.applied[fid] = AUTO_RESOLVED
            outcome.resolved_units[fid] = best_u
            outcome.resolved_chapters[fid] = _display_chapter(best_u, labels, fallback_ch, noun=unit_noun)
        else:
            cv = CandidateVerdict(
                candidate_id=fid,
                verdict=INSUFFICIENT,
                answer=row["answer"],
                counter_search_performed=True,
                reason=row["gate_reason"] or row["unresolved_type"] or "UNFILLED_PIT",
            )
            outcome.applied[fid] = OPEN

        outcome.verdicts.append(cv)
        outcome.audit_routes[fid] = {
            "engine": "v3",
            "tier": tier,
            "unresolved_type": row["unresolved_type"],
            "hops_used": st["hops_used"],
            "selected_units": st["selected_post"] + st["selected_pre"],
            "pre_selected_units": st["selected_pre"],
            "evidence_chain": ev_list,
            "gate_demoted": row["gate_demoted"],
            "gate_reason": row["gate_reason"],
            "repaired": row["repaired"],
        }
        apply_v3_verdict_to_item(
            it,
            tier=tier,
            unresolved_type=row["unresolved_type"],
            answer=row["answer"],
            evidence_chain=ev_list,
            display_labels=labels,
            unit_noun=unit_noun,
            setup_unit=x["setup_unit"],
        )

    outcome.not_checked = [x["fs_id"] for x in eligible if x["fs_id"] not in done_ids]
    counts = count_verdicts(
        outcome.verdicts,
        expected_candidate_ids=[x["fs_id"] for x in eligible],
        candidates_total=len(eligible),
        stale=0,
    )
    cov_dict = counts.as_dict()
    cov_dict["v3_tiers"] = {k: tier_counts.get(k, 0) for k in ALLOWED_V3_TIERS}
    cov_dict["v3_options"] = {
        "gray_u4": bool(enable_gray_u4),
        "mid_single_hop": bool(mid_single_hop),
    }
    outcome.coverage = cov_dict

    was_stopped = _stopped()
    outcome.status = (
        STOPPED
        if was_stopped
        else recheck_status(started=True, finished=True, stopped=False, counts=counts)
    )
    if outcome.not_checked:
        outcome.status = "PARTIAL" if not was_stopped else STOPPED
        outcome.status_reason = (
            "用户已停止"
            if was_stopped
            else f"存在未核对候选（{len(outcome.not_checked)} 条，可能已达 recheck_max_calls 上限）"
        )
    elif outcome.calls > 0 and int(cov_dict.get("checked", 0) or 0) == 0 and not was_stopped:
        outcome.status = "PARTIAL"
        outcome.status_reason = f"{outcome.calls} 次调用全部未返回可解析判定"

    _emit_stage("recheck", extra={"checked": len(done_ids), **outcome.summary()})
    return outcome
