"""伏笔复检 V3 超块分桶、共享证据池与证据链核验落账（Layer 3：依赖 Layers 1–2）。"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping, Sequence

from backend.core.text_units import locate_with_mode
from backend.services.recheck_v2 import _display_chapter

from .parsers import (
    _find_quote_in_unit,
    _normalize_for_match,
    _safe_int_unit,
    _select_primary_evidence,
)
from .preprocessing import format_unit_segment, tokenize_entity_tokens
from .prompts import (
    U1_COMPACT_SUPERCHUNK_TARGET_CHARS,
    U1_SUPERCHUNK_TARGET_CHARS,
    V3_PROMPT_HARD_CAP_CHARS,
    WIDE_CHAPTER_CHARS,
    _GENERIC_ENTITY_STOP,
)


def _build_balanced_superchunks(
    block_tuples: Sequence[tuple[int, int, str]],
    *,
    compact_mode: bool = False,
    target_chars: int | None = None,
    prompt_hard_cap_chars: int = V3_PROMPT_HARD_CAP_CHARS,
) -> list[dict[str, Any]]:
    """按字符预算将事件块序列均衡切分为超块，防止超长书越过上下文悬崖。"""
    if not block_tuples:
        return []
    resolved_target = (
        int(target_chars)
        if (target_chars is not None and int(target_chars) > 0)
        else (U1_COMPACT_SUPERCHUNK_TARGET_CHARS if compact_mode else U1_SUPERCHUNK_TARGET_CHARS)
    )
    cap_base = int(prompt_hard_cap_chars) if int(prompt_hard_cap_chars or 0) > 0 else V3_PROMPT_HARD_CAP_CHARS
    hard_cap = max(resolved_target, cap_base - 20_000)
    tot_chars = sum(len(b[2]) for b in block_tuples)
    target_chunks = max(1, math.ceil(tot_chars / max(1, resolved_target)))
    target = tot_chars / target_chunks

    grouped: list[list[tuple[int, int, str]]] = []
    cur: list[tuple[int, int, str]] = []
    cur_len = 0
    for b in block_tuples:
        b_len = len(b[2])
        if cur and cur_len + b_len > hard_cap:
            grouped.append(cur)
            cur = []
            cur_len = 0
        cur.append(b)
        cur_len += b_len
        if len(grouped) < target_chunks - 1 and cur_len >= target:
            grouped.append(cur)
            cur = []
            cur_len = 0
    if cur:
        grouped.append(cur)

    chunks: list[dict[str, Any]] = []
    for idx, grp in enumerate(grouped):
        txt = "\n\n".join(b[2] for b in grp)
        chunks.append({
            "chunk_id": idx,
            "start_unit": grp[0][0],
            "end_unit": grp[-1][1],
            "block_count": len(grp),
            "char_len": len(txt),
            "text": txt,
        })
    return chunks


def _build_shared_pool_segment(
    chapters: Mapping[int, str],
    display_labels: Mapping[int, str],
    unit_headers: Mapping[int, Mapping[str, Any]],
    u: int,
    items_in_bin: Sequence[Mapping[str, Any]],
    packs: Mapping[str, Mapping[str, Any]],
    *,
    is_pre: bool = False,
    trim_offset: int | None = None,
    max_chapter_chars: int = WIDE_CHAPTER_CHARS,
    unit_noun: str = "文件",
) -> str:
    """生成去重共享证据池的单章格式化片段。"""
    if not chapters.get(u):
        return ""
    merged_terms: list[str] = []
    seen_t: set[str] = set()
    for x in items_in_bin:
        for t in packs[x["fs_id"]]["disc_terms"]:
            if t not in seen_t:
                seen_t.add(t)
                merged_terms.append(t)
    min_off = (int(trim_offset) + 200) if trim_offset is not None else 0
    seg = format_unit_segment(
        chapters,
        display_labels,
        unit_headers,
        u,
        src="共享证据池",
        focus_terms=merged_terms,
        min_offset=min_off,
        is_pre_setup=is_pre,
        max_chars=max_chapter_chars,
        unit_noun=unit_noun,
    )
    if trim_offset is not None and seg:
        seg = seg.replace(f"ev_id=U{u:04d}", f"ev_id=U{u:04d}（设问偏移{trim_offset}字后）", 1)
    return seg


def _render_shared_pool_for_bin(
    chapters: Mapping[int, str],
    display_labels: Mapping[int, str],
    unit_headers: Mapping[int, Mapping[str, Any]],
    bin_entries: Sequence[tuple[dict[str, Any], list[int], list[int]]],
    packs: Mapping[str, Mapping[str, Any]],
    *,
    max_chapter_chars: int = WIDE_CHAPTER_CHARS,
    unit_noun: str = "文件",
) -> tuple[str, int]:
    """为同一批条目构建按单元去重的共享正文证据池，返回 `(pool_text, unique_count)`。"""
    items_only = [x for x, _, _ in bin_entries]
    seg_map: dict[tuple[int, int | None], str] = {}
    for x, post_u, pre_u in bin_entries:
        su = int(x["setup_unit"])
        sa_off = x.get("setup_after_offset")
        for u in post_u:
            off = sa_off if (u == su and sa_off is not None) else None
            key = (u, off)
            if key not in seg_map:
                txt = _build_shared_pool_segment(
                    chapters, display_labels, unit_headers, u, items_only, packs,
                    is_pre=False, trim_offset=off,
                    max_chapter_chars=max_chapter_chars, unit_noun=unit_noun,
                )
                if txt:
                    seg_map[key] = txt
        for pu in pre_u:
            key = (pu, None)
            if key not in seg_map:
                txt = _build_shared_pool_segment(
                    chapters, display_labels, unit_headers, pu, items_only, packs,
                    is_pre=True, trim_offset=None,
                    max_chapter_chars=max_chapter_chars, unit_noun=unit_noun,
                )
                if txt:
                    seg_map[key] = txt

    ordered = [seg_map[k] for k in sorted(seg_map.keys(), key=lambda kv: (kv[0], kv[1] if kv[1] is not None else -1))]
    body = "\n\n".join(ordered) if ordered else "（暂未检索到后文原文窗口）"
    return f"【共享正文证据池（共 {len(ordered)} 个去重章节片段）】\n\n{body}", len(ordered)


def _pack_bins_overlap_pool(
    chapters: Mapping[int, str],
    entries: Sequence[dict[str, Any]],
    unit_lists: Mapping[str, tuple[list[int], list[int]]],
    packs: Mapping[str, Mapping[str, Any]],
    *,
    max_k: int,
    max_chars: int,
    max_chapter_chars: int = WIDE_CHAPTER_CHARS,
    prompt_overhead_chars: int = 5000,
    prompt_hard_cap_chars: int = V3_PROMPT_HARD_CAP_CHARS,
) -> list[list[tuple[dict[str, Any], list[int], list[int]]]]:
    """按候选章节重叠度贪心装箱，同时约束单批条数 <= max_k 与预估 prompt <= max_chars。"""
    raw_unit_chars: dict[int, int] = {
        u: min(len(txt), max_chapter_chars + 300) + 120
        for u, txt in chapters.items()
    }
    card_chars: dict[str, int] = {}
    for x in entries:
        fid = x["fs_id"]
        pk = packs[fid]
        card_chars[fid] = (
            260
            + len(pk["core_question"])
            + len(pk["bonus_aspects"])
            + len(pk["setup_event_anchor"])
            + len(pk["alias_hint"])
            + len(pk["recon_note"])
        )

    rem = list(entries)
    bins: list[list[tuple[dict[str, Any], list[int], list[int]]]] = []
    cap_base = int(prompt_hard_cap_chars) if int(prompt_hard_cap_chars or 0) > 0 else V3_PROMPT_HARD_CAP_CHARS
    safe_cap = max(8_000, min(max_chars, cap_base - 1500) - prompt_overhead_chars)

    while rem:
        cur = rem.pop(0)
        cur_fid = cur["fs_id"]
        post_c, pre_c = unit_lists[cur_fid]
        b_items: list[tuple[dict[str, Any], list[int], list[int]]] = [(cur, list(post_c), list(pre_c))]
        b_units: set[int] = set(post_c) | set(pre_c)
        b_pool_chars = sum(raw_unit_chars.get(u, 2500) for u in b_units)
        b_cards_chars = card_chars[cur_fid]

        while rem and len(b_items) < max_k:
            best_pos: int | None = None
            best_score: tuple[int, int] | None = None
            for pos, cand in enumerate(rem):
                cfid = cand["fs_id"]
                cp, cpre = unit_lists[cfid]
                cu_set = set(cp) | set(cpre)
                new_units = cu_set - b_units
                added_pool = sum(raw_unit_chars.get(u, 2500) for u in new_units)
                added_card = card_chars[cfid]
                if b_pool_chars + added_pool + b_cards_chars + added_card <= safe_cap:
                    overlap = len(cu_set & b_units)
                    score = (added_pool + added_card, -overlap)
                    if best_score is None or score < best_score:
                        best_score = score
                        best_pos = pos
            if best_pos is None:
                break
            nxt = rem.pop(best_pos)
            nfid = nxt["fs_id"]
            np, npre = unit_lists[nfid]
            nu_set = set(np) | set(npre)
            added_u = nu_set - b_units
            b_units |= nu_set
            b_pool_chars += sum(raw_unit_chars.get(u, 2500) for u in added_u)
            b_cards_chars += card_chars[nfid]
            b_items.append((nxt, list(np), list(npre)))

        bins.append(b_items)
    return bins


def _render_detective_card_pool(
    x: Mapping[str, Any],
    pk: Mapping[str, Any],
    post_u: Sequence[int],
    pre_u: Sequence[int],
    hop: int,
    prev: Mapping[str, Any] | None = None,
    *,
    max_hops: int = 3,
) -> str:
    su = int(x["setup_unit"])
    sa_off = x.get("setup_after_offset")
    bound_note = f"U{su}" + (f"（含本条设问偏移{sa_off}字后分段）" if sa_off is not None and su in post_u else "")
    post_str = ", ".join(f"U{u:04d}" for u in post_u) or "无"
    pre_str = ", ".join(f"U{u:04d}" for u in pre_u) if pre_u else "无"
    prev_line = ""
    if prev:
        prev_line = (
            f"\n【上一轮(第{hop - 1}跳)结论】：{prev.get('verdict')} | {str(prev.get('answer') or '')[:180]}"
            f"\n【上一轮已引用】：{json.dumps(prev.get('evidence_chain') or [], ensure_ascii=False)}"
        )
    return (
        f"#### fs_id={x['fs_id']}（第 {hop}/{max_hops} 跳）\n"
        f"【核心主问(core_question)】：{pk['core_question']}\n"
        f"【可免责次要支线(bonus_aspects，未写出不得判 UNRESOLVED)】：{pk['bonus_aspects'] or '无'}\n"
        f"【设问事件锚点(设问章 {bound_note})】：{pk['setup_event_anchor']}\n"
        f"{pk['alias_hint'] + chr(10) if pk['alias_hint'] else ''}"
        f"【本条专属推荐单元】：[{post_str}] | 【本条合法前置因果单元】：[{pre_str}]\n"
        f"线索提示：{'; '.join(pk['u1_clues']) or '无'} | {pk['recon_note'] or ''}"
        f"{prev_line}"
    )


def _render_u4_card_pool(
    x: Mapping[str, Any],
    pk: Mapping[str, Any],
    rd: Mapping[str, Any],
    chosen_post: Sequence[int],
    chosen_pre: Sequence[int],
) -> str:
    post_str = ", ".join(f"U{u:04d}" for u in chosen_post) or "无"
    pre_str = ", ".join(f"U{u:04d}" for u in chosen_pre) if chosen_pre else "无"
    return (
        f"#### fs_id={x['fs_id']}\n"
        f"【核心主问(core_question)】：{pk['core_question']}\n"
        f"【可免责次要支线(bonus_aspects，仅此缺漏时不得判 UNRESOLVED)】：{pk['bonus_aspects'] or '无'}\n"
        f"【设问事件锚点(U{x['setup_unit']})】：{pk['setup_event_anchor']}\n"
        f"{pk['alias_hint'] + chr(10) if pk['alias_hint'] else ''}"
        f"【本条可用证据单元】：[{post_str}] | 【合法前置因果单元】：[{pre_str}]\n"
        f"【前轮侦探结论】：{rd.get('verdict')} | {str(rd.get('answer') or '')}\n"
        f"【前轮侦探引文】：{json.dumps(rd.get('evidence_chain') or [], ensure_ascii=False)}"
    )


def _inherit_cross_pool_units(
    chapters: Mapping[int, str],
    display_labels: Mapping[int, str],
    unit_headers: Mapping[int, Mapping[str, Any]],
    x: Mapping[str, Any],
    pk: Mapping[str, Any],
    st: dict[str, Any],
    res_row: Mapping[str, Any],
    pool_units: set[int],
    *,
    max_chapter_chars: int = WIDE_CHAPTER_CHARS,
    unit_noun: str = "文件",
    pre_lookback_units: int = 45,
) -> int:
    """将模型合法引用的同池章节继受为该条目的允许单元，供 U5 确定性校验放行。"""
    if not pool_units or not res_row:
        return 0
    inherited = 0
    su_item = int(x["setup_unit"])
    sa_off = x.get("setup_after_offset")
    can_setup = sa_off is not None and (len(chapters.get(su_item, "")) - int(sa_off)) > 350
    needs_backref = bool(pk.get("needs_pre_setup_backref"))

    for e in res_row.get("evidence_chain") or []:
        if not isinstance(e, Mapping):
            continue
        cu = _safe_int_unit(e.get("unit_id"))
        if cu is None or cu not in pool_units or cu not in chapters:
            continue
        if cu > su_item or (cu == su_item and can_setup):
            if cu not in st["allowed_post"]:
                min_off = (int(sa_off) + 200) if (cu == su_item and sa_off is not None) else 0
                seg = format_unit_segment(
                    chapters,
                    display_labels,
                    unit_headers,
                    cu,
                    src="共享池跨题继受",
                    focus_terms=pk["disc_terms"],
                    min_offset=min_off,
                    is_pre_setup=False,
                    max_chars=max_chapter_chars,
                    unit_noun=unit_noun,
                )
                if seg:
                    st["allowed_post"][cu] = chapters[cu]
                    if cu not in st["selected_post"]:
                        st["selected_post"].insert(0, cu)
                    st["segments"][cu] = seg
                    inherited += 1
        elif needs_backref and max(1, su_item - pre_lookback_units) <= cu < su_item:
            if cu not in st["allowed_pre"]:
                seg = format_unit_segment(
                    chapters,
                    display_labels,
                    unit_headers,
                    cu,
                    src="共享池前置继受",
                    focus_terms=pk["disc_terms"],
                    min_offset=0,
                    is_pre_setup=True,
                    max_chars=max_chapter_chars,
                    unit_noun=unit_noun,
                )
                if seg:
                    st["allowed_pre"][cu] = chapters[cu]
                    if cu not in st["selected_pre"]:
                        st["selected_pre"].append(cu)
                    st["segments"][cu] = seg
                    inherited += 1
    return inherited


def _build_audit_bundle_and_lists(
    st: Mapping[str, Any],
    rd: Mapping[str, Any],
    *,
    max_windows: int = 8,
    fallback_bundle: str = "（暂未检索到后文原文窗口）",
) -> tuple[str, tuple[list[int], list[int]]]:
    """按已引用单元、后文首选与前置单元构造审计文本窗口与单元清单。"""
    cited_u: list[int] = []
    for e in rd.get("evidence_chain") or []:
        if isinstance(e, Mapping):
            cu = _safe_int_unit(e.get("unit_id"))
            if cu is not None:
                cited_u.append(cu)
    ordered_u = cited_u + list(st["selected_post"]) + list(st["selected_pre"])
    chosen_u: list[int] = []
    for u in ordered_u:
        if u in st["segments"] and u not in chosen_u:
            chosen_u.append(u)
            if len(chosen_u) >= max_windows:
                break
    bundle = "\n\n".join(st["segments"][u] for u in chosen_u) if chosen_u else fallback_bundle
    c_post = [u for u in chosen_u if u in st["allowed_post"]]
    c_pre = [u for u in chosen_u if u in st["allowed_pre"] and u not in c_post]
    return bundle, (c_post, c_pre)


# ==============================================================================
# Stage U2/U5 核心确定性组件（供生产与单元测试复用）
# ==============================================================================


def _match_aliases_for_item(
    item: Any,
    split_entry: Mapping[str, Any],
    alias_lexicon: Sequence[Mapping[str, Any]],
    *,
    protagonist_stops: frozenset[str],
) -> tuple[list[str], str, list[int]]:
    q_text = " ".join([
        str(getattr(item, "question", "") or ""),
        str(getattr(item, "description", "") or ""),
        str(getattr(item, "entity_key", "") or ""),
        str(split_entry.get("core_question") or ""),
        " ".join(str(x) for x in (split_entry.get("key_entities") or [])),
    ])
    ek_parts = tokenize_entity_tokens(
        str(getattr(item, "entity_key", "") or ""),
        protagonist_stops=protagonist_stops,
    )

    alias_terms: list[str] = []
    seen_t: set[str] = set()
    hint_pairs: list[str] = []
    alias_units: list[int] = []

    def _add_term(t: str) -> None:
        s = t.strip()
        if len(s) >= 2 and s not in protagonist_stops and s not in _GENERIC_ENTITY_STOP and s not in seen_t:
            seen_t.add(s)
            alias_terms.append(s)

    for al in alias_lexicon:
        et = str(al.get("early_term") or "")
        ti = str(al.get("true_identity") or "")
        tokens = tokenize_entity_tokens(f"{et}/{ti}", protagonist_stops=protagonist_stops)
        if not tokens:
            continue
        hit = False
        for tok in tokens:
            if tok in q_text:
                hit = True
                break
            if any(tok == ek or (min(len(tok), len(ek)) >= 3 and (tok in ek or ek in tok)) for ek in ek_parts):
                hit = True
                break
        if hit:
            hint_pairs.append(f"{et}={ti}")
            for tok in tokens:
                _add_term(tok)
            for u in al.get("units") or []:
                try:
                    ui = int(u)
                    if ui > 0 and ui not in alias_units:
                        alias_units.append(ui)
                except (TypeError, ValueError):
                    pass

    hint_str = (
        "已知实体/代号/译名对照（仅辅助识别场景，所有结论须严格以所给原文为准）：" + "；".join(hint_pairs[:6])
        if hint_pairs
        else ""
    )
    return alias_terms, hint_str, alias_units


def _resolve_setup_location(
    item: Any,
    chapters: Mapping[int, str],
    display_labels: Mapping[int, str],
    *,
    block_size: int = 4,
) -> tuple[int, int | None, str]:
    first_ch = int(getattr(item, "first_seen_chapter", 1) or 1)
    anchor_quote = str(getattr(item, "anchor_quote", "") or "").strip()
    anchor_ch = getattr(item, "anchor_chapter", 0) or 0
    if not chapters:
        return first_ch, None, "no_chapters"

    lo = max(1, first_ch)
    hi = first_ch + max(7, block_size * 2)
    if anchor_quote:
        hit = locate_with_mode(
            chapters,
            anchor_quote,
            candidate_range=(lo, hi),
            display_labels=display_labels,
            anchor_round=int(anchor_ch) if anchor_ch else None,
        )
        if hit.unit is not None and hit.mode in ("range", "fallback"):
            return int(hit.unit), hit.offset, f"locate_{hit.mode}"

    # 若 anchor_quote 为空或未字面命中，用 description 短句在 [first_ch, hi] 内定位
    desc = str(getattr(item, "description", "") or "").strip()
    target_u: int | None = None
    if anchor_ch:
        for u, lbl in sorted(display_labels.items()):
            if lbl.replace("第", "").replace("回", "").strip() == str(anchor_ch) and lo <= u <= hi:
                target_u = u
                break
    if target_u is None:
        target_u = first_ch if first_ch in chapters else min(chapters)

    txt = chapters.get(target_u, "")
    for seg in re.split(r"[，。；！？、\s]+", desc):
        if len(seg) >= 8:
            p = txt.find(seg[:16])
            if p >= 0:
                return target_u, p, "desc_substr"
    return target_u, None, "fallback_first_seen"


def verify_evidence_chain_v3(
    chapters: Mapping[int, str],
    item: Any,
    *,
    setup_unit: int,
    raw_verdict: str,
    raw_evidence_chain: Any,
    allowed_post_units: Mapping[int, str],
    allowed_pre_units: Mapping[int, str],
    setup_after_offset: int | None,
    needs_pre_setup_backref: bool,
    pre_lookback_units: int = 45,
) -> tuple[list[dict[str, Any]], bool, str]:
    """V3 确定性原文子串与防复述校验门禁（支持合法前置因果单元）。"""
    anchor_quote = _normalize_for_match(str(getattr(item, "anchor_quote", "") or ""))
    if raw_verdict == "UNRESOLVED":
        return [], True, ""
    if not isinstance(raw_evidence_chain, list):
        raw_evidence_chain = []

    all_allowed: dict[int, str] = {**allowed_post_units, **allowed_pre_units}
    verified: list[dict[str, Any]] = []
    rejected_reasons: list[str] = []

    for entry in raw_evidence_chain:
        if not isinstance(entry, Mapping):
            continue
        quote = str(entry.get("quote") or entry.get("supporting_quote") or "").strip()
        role = str(entry.get("role") or "payoff").strip()
        nq = _normalize_for_match(quote)
        if len(nq) < 6:
            rejected_reasons.append("quote_too_short")
            continue

        raw_u = entry.get("unit_id")
        if isinstance(raw_u, str) and raw_u.upper().startswith("U"):
            raw_u = raw_u[1:]
        try:
            cited_u = int(raw_u)
        except (TypeError, ValueError):
            cited_u = -1

        matched_u: int | None = None
        matched_off: int | None = None

        if cited_u in all_allowed:
            off = _find_quote_in_unit(all_allowed[cited_u], quote)
            if off is not None:
                matched_u, matched_off = cited_u, off

        if matched_u is None:
            for cand_u, cand_txt in all_allowed.items():
                off = _find_quote_in_unit(cand_txt, quote)
                if off is not None:
                    matched_u, matched_off = cand_u, off
                    break

        if matched_u is None and cited_u in chapters:
            off = _find_quote_in_unit(chapters[cited_u], quote)
            if off is not None:
                matched_u, matched_off = cited_u, off

        if matched_u is None or matched_off is None:
            rejected_reasons.append("quote_not_in_allowed_units")
            continue

        if matched_u < setup_unit:
            is_allowed_pre = (matched_u in allowed_pre_units) or (
                needs_pre_setup_backref and matched_u >= max(1, setup_unit - pre_lookback_units)
            )
            if not is_allowed_pre:
                rejected_reasons.append(f"before_setup_unit_{matched_u}<{setup_unit}")
                continue

        if matched_u == setup_unit:
            min_ok = (int(setup_after_offset) + 200) if setup_after_offset is not None else 999_999_999
            if matched_off < min_ok:
                rejected_reasons.append("same_chapter_setup_restatement")
                continue

        if anchor_quote and len(nq) >= 8 and (nq in anchor_quote or anchor_quote in nq):
            rejected_reasons.append("repeats_anchor_quote")
            continue

        verified.append({
            "unit_id": matched_u,
            "offset": matched_off,
            "quote": quote,
            "role": role,
        })

    if verified:
        return verified, True, ""

    reason = ",".join(sorted(set(rejected_reasons))) or "no_verified_post_setup_quote"
    return [], False, reason


def apply_v3_verdict_to_item(
    item: Any,
    *,
    tier: str,
    unresolved_type: str,
    answer: str,
    evidence_chain: Sequence[Mapping[str, Any]],
    display_labels: Mapping[int, str] | None,
    unit_noun: str = "文件",
    setup_unit: int = 0,
) -> None:
    """将 V3 四态结果零人工落回账本条目；未回收者主动清除阶段 1 冻结标志避免阻塞休眠。"""
    notes = getattr(item, "notes", None)
    clean_ans = (answer or "").strip()

    if tier in ("RESOLVED_EXPLICIT", "RESOLVED_IMPLICIT", "PARTIALLY_RESOLVED"):
        eff_setup = int(
            setup_unit or getattr(item, "anchor_chapter", 0) or getattr(item, "first_seen_chapter", 0) or 0
        )
        best_ev = _select_primary_evidence(evidence_chain, eff_setup) if evidence_chain else None
        best_u = int(_safe_int_unit(best_ev.get("unit_id")) or 0) if best_ev is not None else 0
        fallback_ch = int(getattr(item, "anchor_chapter", 0) or getattr(item, "first_seen_chapter", 0) or 0)
        located_ch = (
            _display_chapter(best_u, display_labels, fallback_ch, noun=unit_noun)
            if best_u > 0
            else fallback_ch
        )
        prefix = "[核心已回收·支线留白] "
        final_res = (
            (clean_ans if clean_ans.startswith(prefix) else prefix + clean_ans)
            if tier == "PARTIALLY_RESOLVED"
            else clean_ans
        )
        item.status = "resolved"
        item.resolution = final_res
        item.resolved_chapter = int(located_ch or 0)
        item.source_type = "auto_recheck_v3"
        item.resolution_quality = "verified"
        item.needs_review = False
        if isinstance(notes, list):
            tag = (
                "V3复检：隐式/别名兑现已核实"
                if tier == "RESOLVED_IMPLICIT"
                else "V3复检：核心已回收（次要支线留白）"
                if tier == "PARTIALLY_RESOLVED"
                else "V3复检：显式回收已核实"
            )
            if tag not in notes:
                notes.append(tag)
        return

    # UNRESOLVED：绝不把无证据条目留给“待人工确认”冻结——清空阶段 1 未核实提名以便正常进入休眠判定
    if getattr(item, "needs_review", False):
        item.needs_review = False
        if getattr(item, "status", "active") == "active":
            item.resolution = None
            item.resolved_chapter = None
    if isinstance(notes, list):
        msg = (
            "V3复检：氛围彩蛋钩子（未设实质待填坑）"
            if (unresolved_type or "").upper() == "ATMOSPHERIC_HOOK"
            else "V3复检：未见实证回收"
        )
        if msg not in notes:
            notes.append(msg)
