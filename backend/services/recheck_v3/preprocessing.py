"""伏笔复检 V3 跨题材预处理、主角停用词与自适应分块（Layer 2：依赖 prompts 常量）。"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend.core.evidence import EvidenceIndex
from backend.core.text_units import unit_label

from .prompts import WIDE_CHAPTER_CHARS, _GENERIC_ENTITY_STOP


def detect_protagonist_stopwords(
    chapters: Mapping[int, str],
    candidates: Sequence[Mapping[str, Any]],
    index: EvidenceIndex,
) -> frozenset[str]:
    """基于语料文档频率自动识别高频主视角人名，绝不硬编码特定书名人物。"""
    base = {"主人公", "主角", "男主", "女主"}
    total = len(chapters)
    if total < 12 or not candidates:
        return frozenset(base)

    term_counts: Counter[str] = Counter()
    for c in candidates:
        item = c["item"]
        ek = str(getattr(item, "entity_key", "") or "")
        desc = str(getattr(item, "description", "") or "")[:60]
        q = str(getattr(item, "question", "") or "")[:60]
        for seg in re.split(r"[/、，,（）()\s；;:：·•・]+", f"{ek} {desc} {q}"):
            s = seg.strip("《》“”\"' ")
            if 2 <= len(s) <= 4 and s not in _GENERIC_ENTITY_STOP and s not in base:
                term_counts[s] += 1
        # 也检查 2~3 字人名前缀候选
        for m in re.finditer(r"[\u4e00-\u9fff]{2,3}", desc[:24]):
            w = m.group(0)
            if w not in _GENERIC_ENTITY_STOP and w not in base:
                term_counts[w] += 1

    threshold = max(6, int(total * 0.42))
    detected = set(base)
    for tok, freq in term_counts.most_common(40):
        if freq < 2:
            continue
        df = len(index._hit_chapters(tok))
        if df >= threshold:
            detected.add(tok)
    return frozenset(detected)


def tokenize_entity_tokens(
    text: str,
    *,
    protagonist_stops: frozenset[str] = frozenset(),
) -> list[str]:
    """拆分别名与实体词（支持 ``·`` 译名与分隔符，保留完整译名及独立词段）。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw_part in re.split(r"[/、，,（）()\s；;:：→>=—–-]+", text or ""):
        s = raw_part.strip("《》“”\"' ")
        if not s:
            continue
        candidates = [s]
        if any(sep in s for sep in ("·", "•", "・", ".")):
            for sub in re.split(r"[·•・.]+", s):
                sub_s = sub.strip()
                if len(sub_s) >= 2:
                    candidates.append(sub_s)
        for tok in candidates:
            if len(tok) < 2 or tok in protagonist_stops or tok in _GENERIC_ENTITY_STOP or tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
    return out


def build_unit_headers(chapters: Mapping[int, str]) -> dict[int, dict[str, Any]]:
    """提取每个单元的首行标题，并通用标注 [正篇] 与 [番外/外传篇]。"""
    ordered = sorted(chapters)
    max_u = ordered[-1] if ordered else 0
    titles: dict[int, str] = {}
    finale_unit: int | None = None

    for u in ordered:
        first_line = ""
        for line in (chapters.get(u) or "").splitlines():
            s = line.strip()
            if s:
                first_line = s[:75]
                break
        # 若无章回前缀且像长正文句（非短标题），不将句子冒充章标题
        is_likely_title = bool(
            first_line
            and (
                re.match(r"^(?:第\s*[0-9零〇一二三四五六七八九十百千万两]+|\d+[、.\s])", first_line)
                or (len(first_line) <= 32 and not any(p in first_line for p in ("。", "！", "？", "，")))
                or re.search(r"番外|外传|前传|后记|特典|序章|楔子|尾声|大结局|完本感言", first_line)
            )
        )
        title_str = first_line if is_likely_title else ""
        titles[u] = title_str
        if (
            finale_unit is None
            and max_u >= 20
            and u >= max_u // 2
            and re.search(r"大结局|完本感言|全书完|终章|尾声", title_str)
        ):
            finale_unit = u

    headers: dict[int, dict[str, Any]] = {}
    for u, title in titles.items():
        is_epilogue = bool(
            (finale_unit is not None and u > finale_unit)
            or re.search(r"(?:番外|外传|前传|后记|特典|IF线|完本感言)", title)
        )
        if is_epilogue:
            head_part = f" | {title}" if title else ""
            tag = f"【番外/外传篇（属正篇大结局外独立视角或后日谈，切勿将番外角色经历混同为主线同期事件）{head_part}】"
        else:
            tag = f"【正篇 | {title}】" if title else "【正篇】"
        headers[u] = {"raw_title": title, "is_epilogue": is_epilogue, "tag": tag}
    return headers


def _build_rich_block_digest(data: Mapping[str, Any], start_u: int, end_u: int, label_range: str) -> str:
    lines: list[str] = [f"[单元{start_u}-{end_u} | {label_range}]"]

    ev_parts: list[str] = []
    for ev in (data.get("core_events") or [])[:7]:
        if isinstance(ev, Mapping):
            txt = str(ev.get("event") or "").strip()
            chars = str(ev.get("characters") or "").strip()
            if txt:
                txt = txt[:135] + ("…" if len(txt) > 135 else "")
                ev_parts.append(f"{txt}" + (f"({chars[:28]})" if chars else ""))
        elif isinstance(ev, str) and ev.strip():
            ev_parts.append(ev.strip()[:120])
    if ev_parts:
        lines.append("事件: " + " | ".join(ev_parts))

    arc_parts: list[str] = []
    for a in (data.get("character_arcs") or [])[:7]:
        if isinstance(a, Mapping):
            name = str(a.get("name") or "").strip()
            delta = str(a.get("change_delta") or a.get("surface_action") or "").strip()
            if name and delta:
                arc_parts.append(f"{name}:{delta[:55]}")
    if arc_parts:
        lines.append("人物: " + "; ".join(arc_parts))

    fs_parts: list[str] = []
    for f in (data.get("foreshadowing") or [])[:8]:
        if isinstance(f, Mapping):
            clue = str(f.get("clue") or f.get("description") or "").strip()
            link = str(f.get("contextual_link") or f.get("resolution") or "").strip()
            if clue and link and link != clue:
                fs_parts.append(f"{clue[:40]}=>{link[:75]}")
            elif link:
                fs_parts.append(link[:85])
            elif clue:
                fs_parts.append(clue[:65])
    if fs_parts:
        lines.append("伏笔: " + "; ".join(fs_parts))

    upd = data.get("updated_knowledge") or {}
    if isinstance(upd, Mapping):
        wb = upd.get("world_building") or []
        if isinstance(wb, list) and wb:
            wb_s = "；".join(str(x)[:65] for x in wb[:5] if x)
            if wb_s:
                lines.append("设定: " + wb_s)

    lci = data.get("long_context_insights") or {}
    if isinstance(lci, Mapping):
        fn = str(lci.get("foreshadowing_network") or "").strip()
        if fn:
            lines.append("脉络: " + fn[:180])

    return "\n".join(lines)


def build_adaptive_digest_chunks(
    chapters: Mapping[int, str],
    display_labels: Mapping[int, str],
    *,
    output_dir: Path | None = None,
    block_size: int = 4,
    blocks_per_macro: int = 15,
) -> tuple[
    list[dict[str, Any]],
    list[tuple[int, int, str]],
    dict[int, tuple[int, int, str]],
    dict[str, list[dict[str, Any]]],
    list[str],
]:
    """构建全书事件块与按篇幅自适应切分的路由宏块。"""
    ordered = sorted(chapters)
    max_u = ordered[-1] if ordered else 0
    block_tuples: list[tuple[int, int, str]] = []
    unit_to_block: dict[int, tuple[int, int, str]] = {}
    recon_claims: dict[str, list[dict[str, Any]]] = {}
    vol_texts: list[str] = []

    ch_files: list[Path] = []
    if output_dir is not None and output_dir.is_dir():
        ch_files = sorted(
            output_dir.glob("chapter_*_result.json"),
            key=lambda p: int(p.stem.split("_")[1]) if p.stem.split("_")[1].isdigit() else 0,
        )

    if ch_files:
        for idx_f, p in enumerate(ch_files):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            stem_parts = p.stem.split("_")
            if len(stem_parts) < 2 or not stem_parts[1].isdigit():
                continue
            start_u = int(stem_parts[1])
            if idx_f + 1 < len(ch_files):
                nxt = ch_files[idx_f + 1].stem.split("_")
                next_u = int(nxt[1]) if len(nxt) >= 2 and nxt[1].isdigit() else start_u + block_size
                end_u = min(max_u, next_u - 1)
            else:
                bs = int(data.get("block_size") or block_size or 4)
                end_u = min(max_u, start_u + bs - 1)
            if end_u < start_u:
                end_u = start_u
            l_start = display_labels.get(start_u, f"单元{start_u}")
            l_end = display_labels.get(end_u, f"单元{end_u}")
            label_range = l_start if l_start == l_end else f"{l_start}~{l_end}"
            digest = _build_rich_block_digest(data, start_u, end_u, label_range)
            bt = (start_u, end_u, digest)
            block_tuples.append(bt)
            for u in range(start_u, end_u + 1):
                unit_to_block[u] = bt

    if not block_tuples and ordered:
        step = max(1, block_size)
        for i in range(0, len(ordered), step):
            grp = ordered[i : i + step]
            su, eu = grp[0], grp[-1]
            l_start = display_labels.get(su, f"单元{su}")
            l_end = display_labels.get(eu, f"单元{eu}")
            label_range = l_start if l_start == l_end else f"{l_start}~{l_end}"
            snips = []
            for cu in grp:
                t = (chapters.get(cu) or "").strip()
                if t:
                    snips.append(f"U{cu}:{t[:180]}")
            digest = f"[单元{su}-{eu} | {label_range}]\n事件: " + " | ".join(snips)
            bt = (su, eu, digest)
            block_tuples.append(bt)
            for u in range(su, eu + 1):
                unit_to_block[u] = bt

    if output_dir is not None:
        ckpt_dir = output_dir / "final_summary_checkpoint"
        if ckpt_dir.is_dir():
            for rp in sorted(ckpt_dir.glob("recon_*.json")):
                try:
                    rdata = json.loads(rp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for row in (rdata.get("reconciliation") or rdata.get("foreshadow_reconciliation") or []):
                    if not isinstance(row, Mapping) or not row.get("is_resolved_in_this_batch"):
                        continue
                    fid = str(row.get("foreshadow_id") or "")
                    if fid:
                        recon_claims.setdefault(fid, []).append({
                            "resolved_chapter": row.get("resolved_chapter"),
                            "resolution_summary": str(row.get("resolution_summary") or ""),
                            "confidence": str(row.get("confidence") or ""),
                            "source": rp.name,
                        })
            from backend.services.final_summary.parsers import validate_volume_summary

            for vp in sorted(ckpt_dir.glob("volume_*.md")):
                try:
                    vtxt = vp.read_text(encoding="utf-8")
                    if validate_volume_summary(vtxt)[0]:
                        vol_texts.append(f"=== {vp.name} ===\n{vtxt[:1200]}")
                except Exception:
                    pass

    chunk_sz = max(1, blocks_per_macro)
    macro_chunks: list[dict[str, Any]] = []
    for ci in range(0, len(block_tuples), chunk_sz):
        grp = block_tuples[ci : ci + chunk_sz]
        macro_chunks.append({
            "chunk_id": len(macro_chunks),
            "start_unit": grp[0][0],
            "end_unit": grp[-1][1],
            "blocks": grp,
            "text": "\n\n".join(b[2] for b in grp),
        })
    return macro_chunks, block_tuples, unit_to_block, recon_claims, vol_texts


def _disambiguate_chapter_to_units(
    chapters: Mapping[int, str],
    display_labels: Mapping[int, str],
    label_num_to_units: Mapping[int, list[int]],
    chapter_num: Any,
    *,
    after_unit: int,
) -> list[int]:
    try:
        num = int(chapter_num or 0)
    except (TypeError, ValueError):
        return []
    if num <= 0:
        return []
    out: list[int] = []
    for u in label_num_to_units.get(num, []):
        if u > after_unit and u not in out:
            out.append(u)
    if num in chapters and num > after_unit and num not in out:
        out.append(num)
    return out


def _expand_units(
    chapters: Mapping[int, str],
    seed_units: Sequence[int],
    *,
    min_unit: int,
    radius: int = 1,
) -> list[int]:
    if not chapters:
        return []
    max_u = max(chapters)
    out: list[int] = []
    seen: set[int] = set()
    for su in seed_units:
        for delta in (0, 1, -1, 2, -2):
            if abs(delta) > radius:
                continue
            u = su + delta
            if min_unit <= u <= max_u and u in chapters and u not in seen:
                seen.add(u)
                out.append(u)
    return out


def _filter_discriminative_terms(
    chapters: Mapping[int, str],
    index: EvidenceIndex,
    terms: Sequence[str],
    *,
    max_df_ratio: float = 0.35,
) -> list[str]:
    total = max(1, len(chapters))
    out: list[str] = []
    seen: set[str] = set()
    for t in terms:
        s = (t or "").strip()
        if len(s) < 2 or s in seen or s in _GENERIC_ENTITY_STOP:
            continue
        seen.add(s)
        df = len(index._hit_chapters(s))
        if 0 < df <= max(2, int(total * max_df_ratio)):
            out.append(s)
    out.sort(key=lambda w: (-len(w), len(index._hit_chapters(w))))
    return out


def _find_mentions_in_range(
    chapters: Mapping[int, str],
    index: EvidenceIndex,
    keywords: Sequence[str],
    *,
    min_u: int,
    max_u: int,
    limit: int = 10,
    max_df_ratio: float = 0.35,
) -> list[tuple[int, float]]:
    clean = _filter_discriminative_terms(chapters, index, keywords, max_df_ratio=max_df_ratio)
    if not clean:
        return []
    total = max(1, len(chapters))
    weights: dict[str, float] = {}
    hits_by_term: dict[str, frozenset[int]] = {}
    for term in clean[:28]:
        hits = index._hit_chapters(term)
        if not hits:
            continue
        hits_by_term[term] = hits
        weights[term] = len(term) * math.log((total + 1) / (len(hits) + 0.5))
    if not weights:
        return []
    scored: list[tuple[float, int, int]] = []
    for ch in chapters:
        if ch < min_u or ch > max_u:
            continue
        s = 0.0
        n_hit = 0
        for term, w in weights.items():
            if ch in hits_by_term[term]:
                s += w
                n_hit += 1
        if s > 0:
            scored.append((s * (1.0 + 0.2 * min(n_hit, 5)), ch, n_hit))
    scored.sort(key=lambda x: (-x[0], -x[2], x[1]))
    return [(ch, sc) for sc, ch, _ in scored[:limit]]


def _score_chapter_against_terms(text: str, weighted_terms: Sequence[tuple[str, float]]) -> float:
    if not text:
        return 0.0
    score = 0.0
    seen: set[str] = set()
    for term, w in weighted_terms:
        if not term or len(term) < 2 or term in seen:
            continue
        if term in text:
            seen.add(term)
            score += w
    return score


def _nms_episode_select(
    candidates: Sequence[tuple[int, float, list[str]]],
    *,
    max_windows: int = 8,
    cluster_radius: int = 3,
    max_per_cluster: int = 2,
) -> list[tuple[int, float, list[str]]]:
    sorted_cands = sorted(candidates, key=lambda x: (-x[1], x[0]))
    clusters: list[list[tuple[int, float, list[str]]]] = []
    for cand in sorted_cands:
        u = cand[0]
        placed = False
        for cl in clusters:
            if any(abs(u - c[0]) <= cluster_radius for c in cl):
                cl.append(cand)
                placed = True
                break
        if not placed:
            clusters.append([cand])

    chosen: list[tuple[int, float, list[str]]] = []
    chosen_u: set[int] = set()
    for cl in clusters[: min(6, len(clusters))]:
        top = cl[0]
        chosen.append(top)
        chosen_u.add(top[0])
        if len(chosen) >= max_windows:
            return chosen

    leftover: list[tuple[int, int, tuple[int, float, list[str]]]] = []
    for ci, cl in enumerate(clusters):
        used_in_cl = sum(1 for c in cl if c[0] in chosen_u)
        for c in cl:
            if c[0] not in chosen_u:
                leftover.append((ci, used_in_cl, c))
                used_in_cl += 1
    leftover.sort(key=lambda x: (1 if x[1] < max_per_cluster else 2, -x[2][1], x[2][0]))
    for _ci, rank_in_cl, cand in leftover:
        if len(chosen) >= max_windows:
            break
        if cand[0] in chosen_u:
            continue
        if rank_in_cl >= max_per_cluster and len(sorted_cands) > max_windows:
            continue
        chosen.append(cand)
        chosen_u.add(cand[0])

    if len(chosen) < max_windows:
        for cand in sorted_cands:
            if cand[0] not in chosen_u:
                chosen.append(cand)
                chosen_u.add(cand[0])
                if len(chosen) >= max_windows:
                    break
    return chosen


def format_unit_segment(
    chapters: Mapping[int, str],
    display_labels: Mapping[int, str],
    unit_headers: Mapping[int, Mapping[str, Any]],
    unit_id: int,
    *,
    src: str,
    focus_terms: Sequence[str],
    min_offset: int = 0,
    is_pre_setup: bool = False,
    max_chars: int = WIDE_CHAPTER_CHARS,
    unit_noun: str = "文件",
) -> str:
    """格式化单章正文：标准章节整章直喂；超大章自动按不重叠密集群切成 2 段防截盲。"""
    raw = chapters.get(unit_id, "")
    if not raw:
        return ""
    text = raw[min_offset:] if min_offset > 0 else raw
    if not text.strip():
        return ""

    hdr = unit_headers.get(unit_id) or {}
    raw_title = str(hdr.get("raw_title") or "")
    ep_tag = str(hdr.get("tag") or "【正篇】")

    if len(text) <= max_chars + 400:
        body = text.strip()
        trunc_note = "（整章完整原文）"
    else:
        positions: list[tuple[int, str]] = []
        for kw in focus_terms[:22]:
            if not kw or len(kw) < 2:
                continue
            p = text.find(kw)
            while p >= 0:
                positions.append((p, kw))
                p = text.find(kw, p + max(1, len(kw)))

        if positions:
            # 对万字大章选取最多 2 个非重叠高密度锚点窗口
            win_half = max(900, max_chars // 4)
            scored_anchors: list[tuple[int, int]] = []
            for p, _ in positions:
                lo, hi = max(0, p - win_half), min(len(text), p + win_half)
                sc = sum(len(w) for pp, w in positions if lo <= pp < hi)
                scored_anchors.append((sc, p))
            scored_anchors.sort(key=lambda x: (-x[0], x[1]))

            spans: list[tuple[int, int]] = []
            for _sc, center in scored_anchors:
                st = max(0, center - win_half)
                ed = min(len(text), center + win_half)
                if any(not (ed <= s0 or st >= e0) for s0, e0 in spans):
                    continue
                spans.append((st, ed))
                if len(spans) >= 2:
                    break
            spans.sort()
            # 合并过近窗口
            merged: list[tuple[int, int]] = []
            for st, ed in spans:
                if merged and st - merged[-1][1] <= 300:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], ed))
                else:
                    merged.append((st, ed))
            # 保证至少一个足够宽的窗
            if len(merged) == 1:
                st0, ed0 = merged[0]
                extra = max(0, max_chars - (ed0 - st0))
                st0 = max(0, st0 - extra // 2)
                ed0 = min(len(text), ed0 + extra - (extra // 2))
                merged = [(st0, ed0)]

            body_parts = [
                ("…" if st > 0 else "") + text[st:ed].strip() + ("…" if ed < len(text) else "")
                for st, ed in merged
            ]
            body = "\n……（中略）……\n".join(body_parts)
            if merged[0][0] > 120 and raw_title:
                body = f"{raw_title}\n……（上略）……\n{body}"
        else:
            half = max_chars // 2
            body = f"{text[:half].strip()}\n……（中略）……\n{text[-half:].strip()}"
        trunc_note = f"（多锚点节选，原文全长{len(text)}字）"

    lbl = unit_label(unit_id, display_labels, noun=unit_noun)
    ev_id = f"U{unit_id:04d}"
    pre_banner = " | 【前置因果回溯窗（设问前章节，用于核查此前埋下的前因/旧识/来历）】" if is_pre_setup else ""
    return (
        f"### 证据段 ev_id={ev_id} | unit_id={unit_id} | 章节={lbl} | {ep_tag}{pre_banner} | 来源={src} {trunc_note}\n"
        f"{body}"
    )
