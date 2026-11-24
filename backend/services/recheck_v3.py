"""伏笔复检 V3：双向蒸馏路由 + 块内精排 + 有界多跳侦探 + 四防对称外科终审。

设计目标：
- 全自动零人工闭环：以四态分层结论（``RESOLVED_EXPLICIT`` / ``RESOLVED_IMPLICIT`` /
  ``PARTIALLY_RESOLVED`` / ``UNRESOLVED [ATMOSPHERIC_HOOK | UNFILLED_PIT]``）直接落账，
  不再因“证据齐全主义”或次要支线留白误杀真伏笔，亦不受阶段 1 误报冻结影响休眠。
- 跨题材与跨篇幅自适应：自动识别高文档频率主视角停用词、按章块规模自适应切分路由宏块、
  支持小语料快路径、支持超长章多窗锚点切片、支持 ``·`` 译名切分与 [正篇/番外] 标签隔离。
- 向后兼容现有账本与进度契约：复用 ``backend/services/recheck_v2.py`` 的 ``RecheckOutcome``、
  ``_display_chapter`` 与 ``backend/services/recheck_resolution.py`` 的 ``CandidateVerdict`` / ``count_verdicts``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from backend.common.recheck_request_log import sha256_text
from backend.core.evidence import EvidenceIndex, _keywords_for
from backend.core.text_units import build_display_labels, locate_with_mode, unit_label
from backend.services.recheck_v2 import (
    AUTO_RESOLVED,
    OPEN,
    RECHECK_DETAIL_FILENAME,
    RecheckOutcome,
    _display_chapter,
    _evidence_id,
)
from backend.utils.json_utils import extract_json_from_text, safe_save_json
from backend.services.recheck_resolution import (
    INSUFFICIENT,
    STOPPED,
    SUPPORTED,
    CandidateVerdict,
    count_verdicts,
    recheck_status,
)

logger = logging.getLogger(__name__)

DEFAULT_V3_BATCH_SIZE = 8
DEFAULT_V3_COMPACT_BATCH_SIZE = 4
DEFAULT_V3_CONCURRENCY = 8
DEFAULT_V3_MAX_CALLS = 1200
DEFAULT_V3_POOL_MAX_CHARS = 95_000
DEFAULT_V3_COMPACT_POOL_MAX_CHARS = 45_000
V3_PROMPT_HARD_CAP_CHARS = 110_000
U1_SUPERCHUNK_TARGET_CHARS = 58_000
U1_COMPACT_SUPERCHUNK_TARGET_CHARS = 30_000
U1_SPLIT_BATCH_SIZE = 50
U1_COMPACT_SPLIT_BATCH_SIZE = 25
U1_ROUTE_Q_BATCH = 75
U1_COMPACT_ROUTE_Q_BATCH = 40

WIDE_CHAPTER_CHARS = 6800
COMPACT_CHAPTER_CHARS = 2800

ALLOWED_V3_TIERS = (
    "RESOLVED_EXPLICIT",
    "RESOLVED_IMPLICIT",
    "PARTIALLY_RESOLVED",
    "UNRESOLVED",
)

_GENERIC_ENTITY_STOP = frozenset({
    "主人公", "主角", "男主", "女主", "众人", "对方", "他们", "我们", "自己",
    "神秘", "幕后", "真相", "组织", "势力", "身份", "来历", "秘密", "原因",
    "情况", "关系", "力量", "存在", "人员", "皇帝", "朝廷", "江湖", "世界",
    "后续", "事件", "线索", "人物", "东西", "地方", "事情", "时候", "问题",
})


def _safe_int_unit(val: Any) -> int | None:
    try:
        return int(str(val or "").strip().lstrip("Uu"))
    except (TypeError, ValueError):
        return None


def _normalize_verdict(v: str) -> str:
    s = (v or "").strip().upper()
    if s in ALLOWED_V3_TIERS:
        return s
    if s in ("SUPPORTED", "RESOLVED"):
        return "RESOLVED_EXPLICIT"
    if s in ("IMPLICIT", "IMPLICIT_RESOLVED"):
        return "RESOLVED_IMPLICIT"
    if s in ("PARTIAL", "PARTIALLY"):
        return "PARTIALLY_RESOLVED"
    return "UNRESOLVED"


def _parse_json_payload(raw_text: str) -> Any:
    text = (raw_text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    stripped = re.sub(r"^```(?:json)?\s*", "", text)
    stripped = re.sub(r"\s*```\s*$", "", stripped).strip()
    try:
        return json.loads(stripped)
    except Exception:
        pass
    lb, rb = stripped.find("["), stripped.rfind("]")
    lc, rc = stripped.find("{"), stripped.rfind("}")
    candidates: list[str] = []
    if lb != -1 and rb > lb and (lc == -1 or lb < lc):
        candidates.append(stripped[lb : rb + 1])
    if lc != -1 and rc > lc:
        candidates.append(stripped[lc : rc + 1])
    if lb != -1 and rb > lb:
        candidates.append(stripped[lb : rb + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception:
            continue
    extracted = extract_json_from_text(text)
    if isinstance(extracted, str):
        try:
            return json.loads(extracted)
        except Exception:
            return None
    return extracted


def _salvage_json_array_rows(raw_text: str) -> dict[str, dict[str, Any]]:
    """从可能被输出截断的 JSON 数组中抢救已闭合的对象行（按 fs_id 建索引）。"""
    text = (raw_text or "").strip()
    start = text.find("[")
    if start < 0:
        return {}
    out: dict[str, dict[str, Any]] = {}
    in_str = False
    esc = False
    depth = 0
    obj_start = -1
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_start >= 0:
                    try:
                        obj = json.loads(text[obj_start : i + 1])
                        if isinstance(obj, Mapping) and obj.get("fs_id"):
                            out[str(obj["fs_id"])] = dict(obj)
                    except Exception:
                        pass
                    obj_start = -1
    return out


def _parse_rows_by_id(raw_text: str) -> dict[str, dict[str, Any]]:
    """解析模型返回的多条结果并按 fs_id 映射为字典。"""
    parsed = _parse_json_payload(raw_text)
    rows = (
        parsed
        if isinstance(parsed, list)
        else (
            [parsed]
            if isinstance(parsed, dict) and "fs_id" in parsed
            else (parsed.get("items") or parsed.get("results") or [] if isinstance(parsed, dict) else [])
        )
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, Mapping) and row.get("fs_id"):
            out[str(row["fs_id"])] = dict(row)
    return out


def _select_primary_evidence(
    evidence_chain: Sequence[Mapping[str, Any]],
    setup_unit: int = 0,
) -> Mapping[str, Any] | None:
    """选择主落账证据：优先设伏后兑现单元，仅当全为前驱回指时才回退首条。"""
    if not evidence_chain:
        return None
    if setup_unit > 0:
        post: list[tuple[int, int, Mapping[str, Any]]] = []
        for idx, ev in enumerate(evidence_chain):
            u = _safe_int_unit(ev.get("unit_id"))
            if u is not None and u >= setup_unit:
                role = str(ev.get("role") or "").strip().lower()
                is_non_backref = 0 if role != "causal_backref" else 1
                post.append((is_non_backref, u, ev))
        if post:
            post.sort(key=lambda r: (r[0], r[1]))
            return post[0][2]
    return evidence_chain[0]


def _normalize_for_match(s: str) -> str:
    s = re.sub(r"[\s\u3000]+", "", s or "")
    for a, b in [("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'")]:
        s = s.replace(a, b)
    return s


def _find_quote_in_unit(unit_text: str, quote: str) -> int | None:
    q = (quote or "").strip()
    if len(q) < 6 or not unit_text:
        return None
    pos = unit_text.find(q)
    if pos >= 0:
        return pos
    nq = _normalize_for_match(q)
    if len(nq) < 6:
        return None
    norm_chars: list[str] = []
    orig_idx: list[int] = []
    for i, ch in enumerate(unit_text):
        if re.match(r"[\s\u3000]", ch):
            continue
        if ch in ("“", "”"):
            norm_chars.append('"')
        elif ch in ("‘", "’"):
            norm_chars.append("'")
        else:
            norm_chars.append(ch)
        orig_idx.append(i)
    npos = "".join(norm_chars).find(nq)
    return orig_idx[npos] if npos >= 0 else None


# ==============================================================================
# 通用跨题材预处理：主角自识别 / 正篇番外标注 / 译名拆分 / 自适应分块
# ==============================================================================


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


# ==============================================================================
# 中立通用 Prompt 模板（去除任何特定作品示例）
# ==============================================================================

U1_SPLIT_PROMPT = """你是长篇小说伏笔问题架构师。请对以下 {count} 条伏笔逐条完成五项拆解：
1. `core_question`：提炼最本质、可独立判定的核心悬念（保留原问题中合法的“A或B”并列分支，不要擅自改偏题意）。
2. `bonus_aspects`：仅属锦上添花的次要细节或附加追问（没有它也算核心已解；若无则写 ""）。
3. `setup_event_anchor`：用 20~40 字精确锁定该伏笔在埋设章的【具体事件/特定人物与部门身份/发生时点】，防止后续把另一场相似事件张冠李戴（例如区分“初入宗门时的引荐”与“后续他案中的戴罪立功推荐”；区分特定姓氏官署如“工部刘某案”与“礼部李某案”）。
4. `needs_pre_setup_backref`：布尔值（true/false）。如果该问题本质在追问“某人此前为何早已认识主角、某旧物的先前来历、关系裂痕的前史、或抵达前谁已动手”（答案可能在设问前不久章节），设为 true；否则 false。
5. `key_entities`：4~8 个最有检索区分度的人名、具体物件名、地名、契约/情感核心词或专有概念（2~6字）。

待拆解条目：
{items_text}

输出纯 JSON 数组（长度严格 {count}，原样保留 fs_id）：
[{{"fs_id": "fs_xxx", "core_question": "...", "bonus_aspects": "...", "setup_event_anchor": "...", "needs_pre_setup_backref": false, "key_entities": ["词1", "词2"]}}]
"""

U1_ALIAS_PAIR_PROMPT = """你是长篇小说实体与名称演化档案员。请阅读本段剧情蒸馏记录（单元 {chunk_start}~{chunk_end}），**只提取高确信度的【同人/同物马甲与名称演化对照】**（绝不输出剧情长摘要）：
覆盖类型：代号对应真名（如“代号甲->真名某人”）、器物俗称或重命名对照（如“无名残简->正式秘典”“旧佩剑->新名剑”）、易容/伪装/身份反转揭晓、西幻或译名全称简称对照（如“全名·姓氏->简称”）。

【单元 {chunk_start}~{chunk_end}】
{chunk_text}

输出纯 JSON 数组（每段最多 15 条，若无则 `[]`）：
[{{"early_term": "...", "true_identity": "...", "relation": "即本人/真名/改名/马甲", "units": [12]}}]
"""

U1_ALIAS_CONSOLIDATE_PROMPT = """你是专有名词词典编纂官。以下是分块抽取的实体/器物/代号对照候选（共 {alias_count} 条）。
请去重合并，保留最核心、准确、无歧义的 **25~75 条别名等价对**（剔除泛化词与主观总结；`early_term` 与 `true_identity` 都保留简洁短名词，方便精确检索），并可参考分卷摘要校验人名拼写：

【分卷摘要参考（校准人名）】
{volume_brief}

【候选词条】
{raw_aliases}

输出纯 JSON 数组：
[{{"early_term": "...", "true_identity": "...", "relation": "等价/真名/改名", "units": [12]}}]
"""

U1_ROUTE_PROMPT = """你是长篇小说高精度剧情线索路由器。下面是本书一段紧凑的【蒸馏事件流】（单元 {chunk_start}~{chunk_end}），以及待扫描的 {q_count} 条伏笔核心问题。

任务：逐一审视本段所有事件块 `[单元A-B]`，找出**在本段中真正被回答、揭晓真身/马甲、说明先前来历成因、给出实体代价、或完成人物情感/动机坦白与关系转折收束**的伏笔！
原则：
1. 若人物/器物改名换姓、换用马甲或译名简称出现，亦或设问的前提在后文以反转方式澄清收束，务必路由命中！
2. 本段若揭示了设问前不久章节中已埋下的直接前因，亦务必路由命中！
3. `units` 必须填写命中事件所在 `[单元A-B]` 区间内的整数单元号；`clue` 用 25 字内简述“本段揭示了什么及涉及何人/何物”（尽量带原文专有名词，便于下层精排）。

【本段蒸馏事件流（单元 {chunk_start}~{chunk_end}）】
{chunk_text}

【待检索伏笔（共 {q_count} 条）】
{questions_text}

输出纯 JSON 数组，仅列出本段确实命中的伏笔（未命中不要硬凑；若全无命中输出 `[]`）：
[{{"fs_id": "fs_xxx", "units": [12, 14], "clue": "揭示了核心物主身份与动机"}}]
"""

U3_DETECTIVE_PROMPT = """你是零人工长篇小说伏笔深度核查侦探。下面为每条伏笔提供【核心主问】、【可免责次要支线】、【设问事件锚点】以及若干段**完整原文窗口**（每段明确标注 `【正篇】` 或 `【番外/外传篇】`）。

核心原则：
1. 以 `core_question` 或任一合法分支（或人物关系/情感心结/结局收束）为判决基准：**不要因为次要支线 `bonus_aspects` 留白而把真回收判成 UNRESOLVED！**
2. 若正文用换词/马甲/规则并线/立场坦白或前提反转方式实质回答了核心悬念（哪怕推翻了原问中关于时间或凶手的预设），可判 `RESOLVED_IMPLICIT` 或 `RESOLVED_EXPLICIT`；若核心已解而另有分支待续，判 `PARTIALLY_RESOLVED`。
3. **四防红线（严防误判）**：
   - `WRONG_EVENT_OR_ENTITY`：严禁拿无关人物或另一场相似事件替代 `setup_event_anchor`（必须核对具体姓氏、官署案名与场景细节，严防把【番外/外传篇】经历混同正篇同期事件！）；
   - `MODALITY_CONFUSION`：梦境试探、他人传闻、虚张声势、未执行计划，不能当作客观已发生事实；
   - `PREMISE_RESTATEMENT`：单纯复述设问前提不算回收；
   - `UNGROUNDED_LEAP`：若正文始终未提及原问指向的特定专有名词/暗号、或角色自始至终未能识破，结论必须诚实判 `UNRESOLVED`（标注 `ATMOSPHERIC_HOOK` 或 `UNFILLED_PIT`），严禁靠“作者写了类似内容”硬猜归因！
4. **主动多跳深挖工具 `follow_up`**：
   只要现有窗口还不够确凿、或只看到中继线索需要顺藤摸瓜查别的章节，请在 `follow_up` 中点名要查的**具体关键词**（最多 5 个）与/或**单元号区间** `[[起始单元, 结束单元]]`（最多 3 段），系统将自动抽取对应正文送给你下一跳核查（最多 3 跳）。若已确凿或确无后续，`follow_up` 置 `null`。

待核查 {count} 条：
{items_block}

输出纯 JSON 数组（长度严格 {count}，原样保留 fs_id）：
[{{
  "fs_id": "fs_xxx",
  "verdict": "RESOLVED_EXPLICIT|RESOLVED_IMPLICIT|PARTIALLY_RESOLVED|UNRESOLVED",
  "unresolved_type": "",
  "confidence": "high|medium|low",
  "answer": "仅陈述所给原文逐字引文能直接证实的答案",
  "evidence_chain": [{{"unit_id": 12, "quote": "逐字连续原句>=6字", "role": "payoff"}}],
  "follow_up": {{"keyword_queries": ["词1"], "unit_ranges": [[10, 14]], "reason": "需要追查什么"}}
}}]
"""

U4_AUDITOR_PROMPT = """你是长篇小说终审总编辑，负责对 {count} 条伏笔执行**最终质量裁决、防过杀兜底与外科重锚定**。

请严格同步执行以下两条对称铁律：

### 第一律：四类硬缺陷核查（严打假阳性）
1. **事件/实体精确对齐（防张冠李戴）**：引文是否真正对应【设问事件锚点】？如果涉及同职衔（如两位不同姓氏/不同部门的大员）、两个不同案件或【番外/外传篇】独立时间线，严禁张冠李戴顶包！
2. **模态真实性（防梦境/传闻当真）**：引文若只是梦境、他人听说、唬人幌子或未实行计划，不得作实证！
3. **实质推进（防前提复述）**：引文是否确实推进了答案或人物/物件终局，而非原地重述旧前提？
4. **极性与零猜测裁剪（防无据脑补）**：如果某专有名词/暗号（或某次预设行动）在全文中毫无提及或角色明确未曾识破，严禁仅凭间接背景跳跃推论，必须判 `UNRESOLVED`；对确已解决的主干，外科剪除引文无法直接支撑的臆测句。

### 第二律：核心/支线分离锁定 + 反预设修正 + 窗内重锚定（严防把真伏笔一刀砍死）
1. 看清下方分开列出的 `【核心主问】` 与 `【可免责次要支线】`：
   **只要下文任何一段完整正文中，存在不犯上述四条硬伤的逐字引文，能直接证实：**
   - 【核心主问】的核心悬念、合法分支、或情感/动机的明确揭示，**或**
   - 即使推翻了原问中关于“之前发生/何时达成”的时间预设，但紧接当场/后文已经当面交代了真相、作案者或归属结局，**或**
   - 该悬念的实质性收束与归宿（如契约方死亡、真身现形揭破伪装、物件源头交代完毕），
   **则严禁以次要支线缺失或量化程度未明为由输出 `UNRESOLVED`！必须判为 `RESOLVED_EXPLICIT` / `RESOLVED_IMPLICIT` / `PARTIALLY_RESOLVED`**，并在 `answer` 中老实区分“已实证部分”与“未明示留白”。
2. **窗内重锚定**：如果初审引偏了片段而同批另有一章正写着真答案，请立即改从那一章重新摘取无伤逐字原句作 `evidence_chain`！

待终审 {count} 条：
{items_block}

输出纯 JSON 数组（长度严格 {count}，原样保留 fs_id）：
[{{
  "fs_id": "fs_xxx",
  "verdict": "RESOLVED_EXPLICIT|RESOLVED_IMPLICIT|PARTIALLY_RESOLVED|UNRESOLVED",
  "unresolved_type": "",
  "answer": "经四防核验及外科裁剪后的干净确切结论",
  "evidence_chain": [{{"unit_id": 12, "quote": "逐字连续原句>=6字", "role": "payoff"}}]
}}]
"""

U5_QUOTE_REPAIR_PROMPT = """确定性原文校验未能在所给正文单元中找到你为 `fs_id={fs_id}` 所引原句（原因：{demote_reason}）。
请对照下文完整正文：
1. 若确有直接支持核心答案或部分回收的原句，请从下文**逐字一字不差地复制** 1~2 句连续原文（>=8 字，带原标点）并填准整数 `unit_id`；
2. 若反复核对确无合法原句，改判 `UNRESOLVED`。

【核心主问】：{core_question}
【事件锚点】：{setup_event_anchor}
【原结论参考】：{prev_answer}

{bundle_text}

输出单个纯 JSON 对象：
{{"fs_id": "{fs_id}", "verdict": "{verdict}", "unresolved_type": "", "answer": "...", "evidence_chain": [{{"unit_id": 12, "quote": "逐字连续原句", "role": "payoff"}}]}}
"""

U3_POOL_SINGLE_STAGE_PROMPT = """你是零人工长篇小说伏笔深度核查侦探兼终审总编辑。本批共核查 {count} 条伏笔，下方先给出按章节单元去重合并的【共享正文证据池】（每段明确标注 `【正篇】` 或 `【番外/外传篇】`），随后逐条给出每条伏笔的题卡与各自专属推荐单元。

请一次性在“四防除伪”与“防过杀锁定”双铁律下完成核查（需要补充章节时可发起 `follow_up` 下一跳，否则直接一步定稿）：

### 第一律：四类硬缺陷核查（严防假阳性，逐条独立隔离）
1. **事件/实体精确对齐**：引文是否真正对应本条【设问事件锚点】？严禁拿同批他题的章节、不同案件/官署/姓氏、或【番外/外传篇】独立时间线顶包！严禁引用早于本条 `setup_unit` 的章作兑现（除非列入【本条合法前置因果单元】）。
2. **模态真实性**：梦境、传闻、虚张声势、未执行计划不得作客观实证！
3. **实质推进**：单纯复述设问前提不算回收。
4. **极性与零猜测裁剪**：若原问指向特定专有名词/暗号但全文毫无提及或角色未曾识破，必须判 `UNRESOLVED`（标注 `ATMOSPHERIC_HOOK` 或 `UNFILLED_PIT`）；对确已解决的主干，裁剪掉引文无法直接支撑的臆测句。

### 第二律：核心/支线分离 + 反预设修正（严防过杀真伏笔）
只要【共享正文证据池】内本条专属推荐单元（或合理后续单元）中，存在不犯上述四条硬伤的逐字连续引文，能直接证实：
- 本条【核心主问】核心悬念、合法分支或情感/动机的明确揭示，**或**
- 即使推翻了原问的时间/凶手预设但紧接/后文当面交代了真相或归宿结局，
则**严禁因【可免责次要支线】未写明或缺乏量化数字而判 UNRESOLVED**！应判 `RESOLVED_EXPLICIT` / `RESOLVED_IMPLICIT` / `PARTIALLY_RESOLVED`，并在 `answer` 中诚实区分“已实证部分”与“未明示留白”。

### 第三律：多跳追查 `follow_up`
若现有池中章节尚缺关键一环，请在 `follow_up` 给出关键词（最多 5 个）与/或单元区间 `[[起始, 结束]]`（最多 3 段）；已确凿或确无后续时置 `null`。

{shared_pool_text}

=== 待核查与终审 {count} 条伏笔题卡 ===
{items_block}

输出纯 JSON 数组（必须严格覆盖全部 {count} 个 fs_id，原样保留 fs_id，不得漏项）：
[{{
  "fs_id": "fs_xxx",
  "verdict": "RESOLVED_EXPLICIT|RESOLVED_IMPLICIT|PARTIALLY_RESOLVED|UNRESOLVED",
  "unresolved_type": "",
  "confidence": "high|medium|low",
  "answer": "经四防核验与裁剪后的干净确切结论",
  "evidence_chain": [{{"unit_id": 12, "quote": "逐字连续原句>=6字", "role": "payoff"}}],
  "follow_up": null
}}]
"""

U4_POOL_AUDITOR_PROMPT = """你是长篇小说终审总编辑，负责对 {count} 条伏笔执行**最终质量裁决、防过杀兜底与外科重锚定**。下方先给出按单元去重合并的【共享正文证据池】，随后逐条列出各伏笔的题卡、前轮侦探结论与专属单元清单。

请严格同步执行以下两条对称铁律（各条逐一独立核验，严禁跨题混引）：
### 第一律：四类硬缺陷核查（严打假阳性）
1. **事件/实体精确对齐（防张冠李戴）**：引文是否真正对应本条【设问事件锚点】？严禁拿他题背景、不同案件/人物、或【番外/外传篇】顶包！未列入【本条合法前置因果单元】且早于本条 `setup_unit` 的章严禁引用。
2. **模态真实性（防梦境/传闻当真）**：梦境、传闻、唬人幌子或未实行计划不得作实证！
3. **实质推进（防前提复述）**：引文是否确实推进了答案或结局（禁止原地重述旧前提）？
4. **极性与零猜测裁剪（防无据脑补）**：专有名词/暗号毫无提及或未曾识破时必须判 `UNRESOLVED`；主干已解者剪除无引文支撑的臆测句。

### 第二律：核心/支线分离锁定 + 反预设修正 + 窗内重锚定
只要本条可用证据单元中存在不犯上述四条硬伤的逐字连续原句，能证实【核心主问】、合法分支、情感揭示、或推翻预设后的实质收束/结局，**严禁以次要支线缺失或量化未明为由输出 `UNRESOLVED`**！如果初审引偏了片段而同池另有一章正写着真答案，立即改从该章重新摘取无伤逐字原句作 `evidence_chain`。

{shared_pool_text}

=== 待终审 {count} 条伏笔 ===
{items_block}

输出纯 JSON 数组（必须严格覆盖全部 {count} 个 fs_id，原样保留 fs_id，不得漏项）：
[{{
  "fs_id": "fs_xxx",
  "verdict": "RESOLVED_EXPLICIT|RESOLVED_IMPLICIT|PARTIALLY_RESOLVED|UNRESOLVED",
  "unresolved_type": "",
  "answer": "经四防核验及外科裁剪后的干净确切结论",
  "evidence_chain": [{{"unit_id": 12, "quote": "逐字连续原句>=6字", "role": "payoff"}}]
}}]
"""


def _build_balanced_superchunks(
    block_tuples: Sequence[tuple[int, int, str]],
    *,
    compact_mode: bool = False,
) -> list[dict[str, Any]]:
    """按字符预算将事件块序列均衡切分为超块，防止超长书越过上下文悬崖。"""
    if not block_tuples:
        return []
    target_chars = U1_COMPACT_SUPERCHUNK_TARGET_CHARS if compact_mode else U1_SUPERCHUNK_TARGET_CHARS
    hard_cap = max(target_chars, V3_PROMPT_HARD_CAP_CHARS - 20_000)
    tot_chars = sum(len(b[2]) for b in block_tuples)
    target_chunks = max(1, math.ceil(tot_chars / max(1, target_chars)))
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
    safe_cap = min(max_chars, V3_PROMPT_HARD_CAP_CHARS - 1500) - prompt_overhead_chars

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
        f"#### fs_id={x['fs_id']}（第 {hop}/3 跳）\n"
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
        elif needs_backref and max(1, su_item - 45) <= cu < su_item:
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
                needs_pre_setup_backref and matched_u >= max(1, setup_unit - 45)
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


# ==============================================================================
# 主执行器：run_recheck_v3
# ==============================================================================


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


__all__ = [
    "ALLOWED_V3_TIERS",
    "DEFAULT_V3_BATCH_SIZE",
    "DEFAULT_V3_COMPACT_BATCH_SIZE",
    "DEFAULT_V3_CONCURRENCY",
    "DEFAULT_V3_MAX_CALLS",
    "U3_POOL_SINGLE_STAGE_PROMPT",
    "U4_POOL_AUDITOR_PROMPT",
    "_salvage_json_array_rows",
    "_select_primary_evidence",
    "apply_v3_verdict_to_item",
    "build_adaptive_digest_chunks",
    "build_unit_headers",
    "detect_protagonist_stopwords",
    "format_unit_segment",
    "run_recheck_v3",
    "tokenize_entity_tokens",
    "verify_evidence_chain_v3",
]
