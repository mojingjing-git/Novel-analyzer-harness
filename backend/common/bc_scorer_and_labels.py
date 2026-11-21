"""T2: Calibrated B/C Scorer, Source-Span Coordinate Verifier, and Independent Label Contract.

Addresses R3-2, R3-4, and Section 四 (T2) of `docs/BC生产管线收尾任务书.md`:
1. Character/entity/chapter/bigram overlap is ONLY a candidate pair filter, never sufficient proof of question equivalence or answer correctness.
2. Enforces 1-to-1 exclusive scoring while explicitly logging multi-to-one conflicts (`multi_to_one_conflicts`).
3. Every scored pair emits a full `PairAdjudicationRecord` with:
   `ref_id`, `candidate_id`, `question_equivalence`, `answer_fact_status`, `evidence_attribution`,
   `closing_criteria_status`, `verbatim_quote`, `source_span` (bound to `source_sha256`, chapter & book char coordinates),
   `anchor_span`, `adjudication_source`, and `adjudication_reason`.
4. Temporal ordering is determined by actual character span coordinates (`res_span.book_char_start >= anchor_span.book_char_end`),
   so valid intra-subchapter setup->resolution is scored when after the anchor, while material before/overlapping the anchor is rejected.
   Duplicate quote occurrences without unique span disambiguation are flagged as `ambiguous` and never take the first hit as certain.
5. Verifies negation/antonym inversion, hearsay/rumor/dream/plan modality vs narrator fact, subject-object swaps,
   and conjunction (`AND`) vs disjunction (`OR`) closing criteria (recording `partial_answer` without counting as full resolution).
6. Separates directly observable Class C branch agreement categories (`both_accepted`, `both_not_accepted`,
   `branch_disagreement_cur_accept_v2_not`, `protocol_or_format_failure`) from source-verified independent review judgments.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import re
from typing import Any, Mapping, Sequence

SCORER_VERSION = "bc_scorer_t2_v1"


def count_occurrences(text: str, sub: str) -> list[int]:
    """Return all start indices of non-empty substring `sub` in `text`."""
    if not text or not sub:
        return []
    out: list[int] = []
    pos = text.find(sub)
    while pos >= 0:
        out.append(pos)
        pos = text.find(sub, pos + 1)
    return out

NEGATION_MARKERS = (
    "并非",
    "不是",
    "未曾",
    "并未",
    "没有",
    "不能",
    "不会",
    "未",
    "毫无",
    "绝非",
    "假",
    "赝品",
)

ANTONYM_PAIRS = (
    ("真品", "赝品"),
    ("真", "假"),
    ("成功", "失败"),
    ("活", "死"),
    ("存活", "身亡"),
    ("生还", "遇难"),
    ("确为", "并非"),
    ("是", "不是"),
    ("能够", "无法"),
    ("开启", "锁死"),
    ("真凶", "替罪羊"),
)

HEARSAY_MARKERS = (
    "据说",
    "传闻",
    "谣言",
    "传言",
    "坊间传言",
    "有人传播谣言",
    "有人怀疑",
    "怀疑",
    "猜测",
    "莫非",
    "大概",
    "似乎",
    "或许",
    "恐怕",
    "听说",
)

DREAM_MARKERS = (
    "梦见",
    "梦中",
    "做了一个梦",
    "幻觉",
    "幻想",
    "大梦初醒",
)

PLAN_MARKERS = (
    "打算",
    "计划",
    "准备",
    "企图",
    "想要",
    "拟于",
    "欲",
    "将要",
)

NARRATOR_CONFIRM_MARKERS = (
    "但真实情况是",
    "然而事实是",
    "最终确认",
    "查明真相",
    "证实",
    "确为",
    "原来是",
)

TRANSITIVE_VERBS = (
    "刺杀",
    "杀害",
    "击杀",
    "杀死",
    "击败",
    "打败",
    "救下",
    "救了",
    "欺骗",
    "隐瞒",
    "出卖",
    "背叛",
    "夺走",
    "抢走",
    "传位给",
    "嫁给",
    "收为",
    "挟持",
    "控制",
)


def _clean_text(s: str) -> str:
    return re.sub(r"[\s，,。；;：:、（）()\"'“”‘’！!？?…—\-《》【】\[\]]", "", s or "")


@dataclass(frozen=True)
class SourceSpan:
    found: bool
    ambiguous: bool = False
    occurrences: int = 0
    chapter: int = 0
    source_sha256: str = ""
    chapter_char_start: int | None = None
    chapter_char_end: int | None = None
    book_char_start: int | None = None
    book_char_end: int | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BookSpanIndexer:
    """Unified character coordinate and source fingerprint indexer across chapters/subchapters."""

    def __init__(self, chapters: Mapping[int, str]) -> None:
        self.chapters: dict[int, str] = {int(k): str(v) for k, v in sorted(chapters.items())}
        self.chapter_sha256: dict[int, str] = {}
        self.chapter_book_offsets: dict[int, int] = {}
        cursor = 0
        for ch, txt in self.chapters.items():
            self.chapter_sha256[ch] = hashlib.sha256(txt.encode("utf-8")).hexdigest()
            self.chapter_book_offsets[ch] = cursor
            cursor += len(txt) + 2  # "\n\n" separator between chapters

    def locate(
        self,
        quote: str,
        *,
        preferred_chapter: int | None = None,
        reported_span: tuple[int, int] | None = None,
    ) -> SourceSpan:
        q = (quote or "").strip()
        if not q:
            return SourceSpan(found=False, reason="empty_quote")

        search_chapters: list[int] = []
        if preferred_chapter is not None and preferred_chapter in self.chapters:
            search_chapters.append(preferred_chapter)
        for ch in self.chapters:
            if ch not in search_chapters:
                search_chapters.append(ch)

        for ch in search_chapters:
            txt = self.chapters[ch]
            positions = count_occurrences(txt, q)
            if not positions:
                # Try stripping trailing punctuation if at least 6 chars remain
                q_trim = q.strip("。，！？；：、“”‘’\"' ")
                if len(q_trim) >= 6:
                    positions = count_occurrences(txt, q_trim)
                    if positions:
                        q = q_trim
            if not positions:
                continue

            base_off = self.chapter_book_offsets[ch]
            sha = self.chapter_sha256[ch]
            if len(positions) == 1:
                s = positions[0]
                e = s + len(q)
                return SourceSpan(
                    found=True,
                    ambiguous=False,
                    occurrences=1,
                    chapter=ch,
                    source_sha256=sha,
                    chapter_char_start=s,
                    chapter_char_end=e,
                    book_char_start=base_off + s,
                    book_char_end=base_off + e,
                    reason="unique_exact_match",
                )

            # Multiple occurrences in the same chapter: check if reported_span uniquely disambiguates
            if reported_span is not None and len(reported_span) == 2:
                rs, re_pos = int(reported_span[0]), int(reported_span[1])
                in_window = [p for p in positions if rs <= p and (p + len(q)) <= re_pos]
                if len(in_window) == 1:
                    s = in_window[0]
                    e = s + len(q)
                    return SourceSpan(
                        found=True,
                        ambiguous=False,
                        occurrences=len(positions),
                        chapter=ch,
                        source_sha256=sha,
                        chapter_char_start=s,
                        chapter_char_end=e,
                        book_char_start=base_off + s,
                        book_char_end=base_off + e,
                        reason="disambiguated_by_reported_span",
                    )

            # T2 Rule 4: 重复引文定位不唯一时保留歧义，不取第一个命中充当确定位置
            return SourceSpan(
                found=True,
                ambiguous=True,
                occurrences=len(positions),
                chapter=ch,
                source_sha256=sha,
                chapter_char_start=None,
                chapter_char_end=None,
                book_char_start=None,
                book_char_end=None,
                reason=f"ambiguous_quote_occurrences:{len(positions)}",
            )

        return SourceSpan(found=False, reason="quote_not_found_in_source")


def check_span_strictly_after_anchor(
    anchor_span: SourceSpan,
    evidence_span: SourceSpan,
) -> tuple[bool, str]:
    """Verify via actual source coordinates that the resolution evidence occurs AFTER the setup anchor."""
    if not anchor_span.found or anchor_span.ambiguous or anchor_span.book_char_end is None:
        return False, "anchor_span_unlocated_or_ambiguous"
    if not evidence_span.found:
        return False, "evidence_span_not_found"
    if evidence_span.ambiguous or evidence_span.book_char_start is None:
        return False, "evidence_span_ambiguous"
    if evidence_span.book_char_start < anchor_span.book_char_end:
        return False, "quote_not_after_anchor_span"
    return True, "strictly_after_anchor_span"


def detect_evidence_attribution(quote: str, surrounding_context: str = "") -> str:
    """Classify the epistemic attribution/modality of a quote in its sentence context."""
    raw_ctx = (surrounding_context or quote or "").strip()
    q = (quote or "").strip()
    if q and q in raw_ctx and len(raw_ctx) > len(q) + 60:
        pos = raw_ctx.find(q)
        window = raw_ctx[max(0, pos - 25) : min(len(raw_ctx), pos + len(q) + 15)]
    else:
        window = raw_ctx

    # Check if the quote itself is inside a rumor/hearsay clause without narrator override inside the quote
    has_confirm_in_q = any(m in q for m in NARRATOR_CONFIRM_MARKERS)
    if not has_confirm_in_q:
        if any(m in q for m in DREAM_MARKERS) or any(m in window for m in DREAM_MARKERS):
            return "speculation_or_dream"
        if any(m in q for m in HEARSAY_MARKERS) or any(m in window for m in HEARSAY_MARKERS):
            return "rumor_or_hearsay"
        if any(m in q for m in PLAN_MARKERS):
            return "plan_or_intent"
    return "narrator_fact"


def check_answer_modality_escalation(
    answer: str,
    quote: str,
    surrounding_context: str = "",
) -> tuple[bool, str, str]:
    """Check if rumor, speculation, dream, or plan in the evidence is escalated to established fact in the answer."""
    attribution = detect_evidence_attribution(quote, surrounding_context)
    if attribution == "narrator_fact":
        return True, attribution, "ok_narrator_fact"

    ans = (answer or "").strip()
    # If evidence is rumor/dream/plan, does the answer preserve that framing?
    all_uncertainty_markers = HEARSAY_MARKERS + DREAM_MARKERS + PLAN_MARKERS + ("尚未证实", "未确认", "传言", "梦境")
    if any(m in ans for m in all_uncertainty_markers):
        return False, attribution, f"nonfactual_evidence_{attribution}_cannot_resolve_fact_question"
    return False, attribution, f"modality_escalated_from_{attribution}_to_certain_fact"


def check_negation_and_antonym_conflict(
    ref_text: str,
    cand_answer: str,
    cand_quote: str = "",
) -> tuple[bool, str]:
    """Check whether candidate answer/quote reverses the polarity or antonym conclusion of the reference."""
    ref_norm = _clean_text(ref_text)
    ans_norm = _clean_text(cand_answer)
    quo_norm = _clean_text(cand_quote)
    cand_combined = ans_norm + " " + quo_norm

    for pos_term, neg_term in ANTONYM_PAIRS:
        if pos_term in ("是", "真") and len(pos_term) == 1:
            continue
        ref_has_pos = pos_term in ref_norm and f"并非{pos_term}" not in ref_norm and f"不是{pos_term}" not in ref_norm
        ref_has_neg = neg_term in ref_norm or f"并非{pos_term}" in ref_norm or f"不是{pos_term}" in ref_norm

        cand_has_neg = neg_term in ans_norm or f"并非{pos_term}" in cand_combined or f"不是{pos_term}" in cand_combined
        cand_has_pos = (
            pos_term in ans_norm
            and f"并非{pos_term}" not in cand_combined
            and f"不是{pos_term}" not in cand_combined
            and neg_term not in ans_norm
        )

        if ref_has_pos and not ref_has_neg and cand_has_neg:
            return False, f"antonym_or_negation_conflict:ref={pos_term}_vs_cand={neg_term}"
        if ref_has_neg and not ref_has_pos and cand_has_pos:
            return False, f"antonym_or_negation_conflict:ref={neg_term}_vs_cand={pos_term}"

    # General negation check on shared content clauses (>= 2 chars after negation marker)
    for neg in ("并非", "不是", "并未", "没有", "未曾", "不能"):
        for m in re.finditer(re.escape(neg) + r"([\u4e00-\u9fa5]{2,6})", cand_combined):
            target_phrase = m.group(1)
            if target_phrase in ref_norm and (neg + target_phrase) not in ref_norm:
                return False, f"negation_conflict_on_{target_phrase}"
        for m in re.finditer(re.escape(neg) + r"([\u4e00-\u9fa5]{2,6})", ref_norm):
            target_phrase = m.group(1)
            if target_phrase in ans_norm and (neg + target_phrase) not in cand_combined:
                return False, f"missing_negation_on_{target_phrase}"

    return True, "polarity_consistent"


def check_subject_object_swap(
    ref_text: str,
    cand_answer: str,
) -> tuple[bool, str]:
    """Detect subject-object inversion around transitive predicates (e.g. 'A刺杀了B' vs 'B刺杀了A')."""
    for verb in TRANSITIVE_VERBS:
        if verb not in ref_text or verb not in cand_answer:
            continue
        pat = re.compile(r"([\u4e00-\u9fa5]{2,4})(?:被|遭|由)?(?:亲自|暗中|最终|已经|已)?(" + re.escape(verb) + r")了?([\u4e00-\u9fa5]{2,4})")
        m_ref = pat.search(ref_text)
        m_ans = pat.search(cand_answer)
        if m_ref and m_ans:
            ref_subj, _, ref_obj = m_ref.group(1), m_ref.group(2), m_ref.group(3)
            ans_subj, _, ans_obj = m_ans.group(1), m_ans.group(2), m_ans.group(3)
            ref_passive = bool(re.search(re.escape(ref_subj) + r"(?:被|遭|由)", ref_text))
            ans_passive = bool(re.search(re.escape(ans_subj) + r"(?:被|遭|由)", cand_answer))
            if ref_passive == ans_passive:
                if (ref_subj in ans_obj or ans_obj in ref_subj) and (ref_obj in ans_subj or ans_subj in ref_obj):
                    if ref_subj != ref_obj:
                        return False, f"subject_object_swapped:{ref_subj}_{verb}_{ref_obj}_vs_{ans_subj}_{verb}_{ans_obj}"
    return True, "roles_consistent"


def evaluate_closing_criteria_branches(
    criteria: str,
    answer: str,
    quote: str,
    *,
    prereg: Mapping[str, Any] | None = None,
    required_branches: Sequence[str] | None = None,
    logic_override: str | None = None,
) -> tuple[str, str]:
    """Evaluate AND / OR closing criteria satisfaction, distinguishing full satisfaction from partial_answer.

    Returns `(closing_status, reason)` where `closing_status` is one of:
    - `"satisfied"`
    - `"partial_answer"`
    - `"unmet"`
    """
    combined = _clean_text(answer) + " " + _clean_text(quote)

    branches: list[str] = []
    logic = logic_override or "SINGLE"

    if required_branches:
        branches = [str(b) for b in required_branches if str(b).strip()]
    elif isinstance(prereg, Mapping) and isinstance(prereg.get("closing"), Mapping):
        logic = str(prereg["closing"].get("type") or "AND").upper()
        for cl in prereg.get("clauses") or []:
            if isinstance(cl, Mapping) and cl.get("text"):
                branches.append(str(cl["text"]))

    if not branches and criteria:
        if any(k in criteria for k in ("同时交代", "且须", "并且", "以及", "既要", "并说明")):
            parts = re.split(r"并且|以及|且须|同时交代|并说明|、|及", criteria)
            parts = [p.strip(" ：:，。；") for p in parts if len(_clean_text(p)) >= 2]
            if len(parts) >= 2:
                branches = parts
                if logic == "SINGLE":
                    logic = "AND"
        elif any(k in criteria for k in ("或者", "任一", "之一")):
            parts = re.split(r"或者|或", criteria)
            parts = [p.strip(" ：:，。；") for p in parts if len(_clean_text(p)) >= 2]
            if len(parts) >= 2:
                branches = parts
                if logic == "SINGLE":
                    logic = "OR"

    if not branches:
        return "satisfied", "single_branch_or_unstructured"

    hits = 0
    for br in branches:
        br_clean = _clean_text(br)
        # Drop generic instruction prefixes
        br_clean = re.sub(r"^(交代|说明|确认|指出|揭示|查明|验证)", "", br_clean)
        if not br_clean:
            continue
        bgs = {br_clean[i : i + 2] for i in range(len(br_clean) - 1)} if len(br_clean) >= 2 else {br_clean}
        matched_bgs = sum(1 for bg in bgs if bg in combined)
        if matched_bgs >= max(1, int(len(bgs) * 0.5)):
            hits += 1

    if logic == "OR":
        if hits >= 1:
            return "satisfied", f"or_branch_satisfied:{hits}/{len(branches)}"
        return "unmet", f"or_branches_unmet:0/{len(branches)}"

    # Default AND for multi-branch requirements
    if hits == len(branches):
        return "satisfied", f"and_branches_all_satisfied:{hits}/{len(branches)}"
    if hits > 0:
        return "partial_answer", f"and_branches_partial:{hits}/{len(branches)}"
    return "unmet", f"and_branches_unmet:0/{len(branches)}"


@dataclass
class PairAdjudicationRecord:
    ref_id: str
    candidate_id: str
    obs_type: str  # "registration" | "resolution"
    question_equivalence: str  # "equivalent" | "non_equivalent" | "ambiguous" | "disputed"
    answer_fact_status: str
    evidence_attribution: str
    closing_criteria_status: str
    verbatim_quote: str
    source_span: dict[str, Any]
    anchor_span: dict[str, Any]
    adjudication_source: str
    adjudication_reason: str
    counted_as_hit: bool
    partial_answer: bool = False
    disputed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def adjudicate_reference_candidate_pair(
    obs: Mapping[str, Any],
    item: Mapping[str, Any],
    verdict: Mapping[str, Any] | None,
    *,
    indexer: BookSpanIndexer,
    independent_review_entry: Mapping[str, Any] | None = None,
) -> PairAdjudicationRecord:
    """Adjudicate a single (reference observation, system candidate) pair under T2 strict rules."""
    obs_id = str(obs.get("obs_id") or obs.get("ref_id") or "")
    cid = str(item.get("id") or item.get("candidate_id") or "")
    obs_type = str(obs.get("obs_type") or "registration")

    ref_anchor_q = str(obs.get("anchor_quote") or "")
    ref_anchor_ch = int(obs.get("anchor_subchapter") or obs.get("anchor_chapter") or 1)
    anchor_span = indexer.locate(ref_anchor_q, preferred_chapter=ref_anchor_ch)

    # Check if an explicit independent human/agent review entry governs this pair.
    #
    # Two governance modes:
    #   - **full**: the review entry supplies question equivalence *and* answer
    #     quality (answer_fact_status / evidence_attribution / closing_criteria_status).
    #     Used when a reviewer actually adjudicated the answer against the source.
    #   - **question_only**: the review entry supplies only the question-equivalence
    #     verdict; answer quality stays with the structural verifier below.
    #     Used when a reviewer judged "do these two talk about the same thing?"
    #     and nothing more — which is the common case for reference-observation
    #     pairing, and the one where substituting the verifier's own
    #     `anchor_chapter_mismatch` / `same_entity_different_question` verdicts
    #     would silently overrule an explicit human decision.
    scoped_review: Mapping[str, Any] | None = None
    review_q_eq: str | None = None
    if isinstance(independent_review_entry, Mapping):
        mode = str(independent_review_entry.get("governance_mode") or "full")
        if mode == "question_only":
            scoped_review = independent_review_entry
            review_q_eq = str(
                independent_review_entry.get("question_equivalence") or "non_equivalent"
            )
        else:
            q_eq = str(independent_review_entry.get("question_equivalence") or "non_equivalent")
            ans_stat = str(independent_review_entry.get("answer_fact_status") or "n/a_registration")
            ev_attr = str(independent_review_entry.get("evidence_attribution") or "narrator_fact")
            clos_stat = str(independent_review_entry.get("closing_criteria_status") or "n/a_registration")
            v_quote = str((verdict or {}).get("quote") or item.get("anchor_quote") or "")
            v_ch = int((verdict or {}).get("chapter") or item.get("resolved_chapter") or item.get("anchor_chapter") or ref_anchor_ch)
            ev_span = indexer.locate(v_quote, preferred_chapter=v_ch)
            is_disp = q_eq == "disputed" or ans_stat == "disputed" or clos_stat == "disputed"
            is_part = clos_stat == "partial_answer"
            if obs_type == "registration":
                hit = (q_eq == "equivalent") and not is_disp
            else:
                after_ok, _ = check_span_strictly_after_anchor(anchor_span, ev_span)
                hit = (
                    q_eq == "equivalent"
                    and ans_stat == "supported_fact"
                    and ev_attr == "narrator_fact"
                    and clos_stat == "satisfied"
                    and after_ok
                    and not is_disp
                )
            return PairAdjudicationRecord(
                ref_id=obs_id,
                candidate_id=cid,
                obs_type=obs_type,
                question_equivalence=q_eq,
                answer_fact_status=ans_stat,
                evidence_attribution=ev_attr,
                closing_criteria_status=clos_stat,
                verbatim_quote=v_quote,
                source_span=ev_span.to_dict(),
                anchor_span=anchor_span.to_dict(),
                adjudication_source=str(independent_review_entry.get("adjudication_source") or "independent_blind_review"),
                adjudication_reason=str(independent_review_entry.get("adjudication_reason") or "independent_review"),
                counted_as_hit=hit,
                partial_answer=is_part,
                disputed=is_disp,
            )

    # Structural & semantic verification when no manual override is provided
    cand_anchor_q = str(item.get("anchor_quote") or "")
    cand_q_text = str(item.get("question") or "") + " " + str(item.get("description") or item.get("clue") or "")
    ref_q_text = str(obs.get("question") or "")
    ref_a_clean = _clean_text(ref_anchor_q)
    cand_a_clean = _clean_text(cand_anchor_q)

    ref_entities: list[str] = []
    if isinstance(obs.get("entities"), Sequence) and not isinstance(obs.get("entities"), str):
        ref_entities.extend(str(e).strip() for e in obs["entities"] if str(e).strip())
    if obs.get("entity") and str(obs.get("entity")).strip() not in ref_entities:
        ref_entities.append(str(obs.get("entity")).strip())

    ref_a_bgs = {ref_a_clean[i : i + 2] for i in range(len(ref_a_clean) - 1)} if len(ref_a_clean) >= 2 else set()
    cand_a_bgs = {cand_a_clean[i : i + 2] for i in range(len(cand_a_clean) - 1)} if len(cand_a_clean) >= 2 else set()
    for ent in ref_entities:
        ec = _clean_text(ent)
        for i in range(len(ec) - 1):
            ref_a_bgs.discard(ec[i : i + 2])
            cand_a_bgs.discard(ec[i : i + 2])

    aq_exact = bool(
        len(ref_a_clean) >= 6
        and len(cand_a_clean) >= 6
        and (
            ref_a_clean in cand_a_clean
            or cand_a_clean in ref_a_clean
            or len(ref_a_bgs & cand_a_bgs) >= 5
        )
    )

    ref_q_clean = re.sub(
        r"(为什么|为何|如何|怎么|是否|能否|是不是|会如何|是什么|有什么|是谁|去哪|哪里|何时|后来|究竟|到底)",
        "",
        _clean_text(ref_q_text),
    )
    for ent in ref_entities:
        ref_q_clean = ref_q_clean.replace(_clean_text(ent), "")
    q_bgs = {ref_q_clean[i : i + 2] for i in range(len(ref_q_clean) - 1)} if len(ref_q_clean) >= 2 else set()
    cand_q_clean = _clean_text(cand_q_text)
    q_bg_hits = sum(1 for bg in q_bgs if bg in cand_q_clean)
    aq_bg_hits = len(ref_a_bgs & (cand_a_bgs | {cand_q_clean[i : i + 2] for i in range(len(cand_q_clean) - 1)}))
    ent_hits = sum(1 for ent in ref_entities if ent and ent in (cand_q_text + cand_anchor_q))

    cand_anchor_ch = int(item.get("anchor_chapter") or item.get("ch") or 0)
    anchor_ch_close = (cand_anchor_ch <= 0 or ref_anchor_ch <= 0 or abs(cand_anchor_ch - ref_anchor_ch) <= 1)

    # Question equivalence check: entity + chapter alone is NEVER enough
    pol_q_ok, pol_q_reason = check_negation_and_antonym_conflict(ref_q_text, cand_q_text)
    role_q_ok, role_q_reason = check_subject_object_swap(ref_q_text, cand_q_text)

    if not pol_q_ok or not role_q_ok:
        q_equiv = "non_equivalent"
        q_reason = pol_q_reason if not pol_q_ok else role_q_reason
    elif not anchor_ch_close and not aq_exact:
        q_equiv = "non_equivalent"
        q_reason = f"anchor_chapter_mismatch:cand={cand_anchor_ch}_vs_ref={ref_anchor_ch}"
    elif aq_exact and (q_bg_hits >= 1 or aq_bg_hits >= 4 or not q_bgs):
        q_equiv = "equivalent"
        q_reason = "anchor_quote_overlap_and_question_aligned"
    elif anchor_ch_close and (q_bg_hits >= 3 or (q_bg_hits >= 2 and len(q_bgs) <= 4)) and (ent_hits >= 1 or aq_bg_hits >= 3):
        q_equiv = "equivalent"
        q_reason = f"question_core_bigrams_matched:{q_bg_hits}"
    elif ent_hits >= 1 and q_bg_hits == 0:
        q_equiv = "non_equivalent"
        q_reason = "same_entity_different_question"
    elif q_bg_hits >= 1:
        q_equiv = "ambiguous"
        q_reason = f"weak_lexical_overlap_ambiguous:{q_bg_hits}"
    else:
        q_equiv = "non_equivalent"
        q_reason = "no_question_or_anchor_match"

    # An independent reviewer may have ruled on question equivalence already.
    # In `question_only` governance the reviewer's decision governs; the
    # structural rules above cannot overrule it (they are lexical heuristics
    # and would otherwise reject a correct pairing on anchor-chapter drift).
    if review_q_eq is not None:
        q_equiv = review_q_eq
        q_reason = f"independent_review:{review_q_eq}"

    if obs_type == "registration":
        cand_span = indexer.locate(cand_anchor_q, preferred_chapter=int(item.get("anchor_chapter") or ref_anchor_ch))
        return PairAdjudicationRecord(
            ref_id=obs_id,
            candidate_id=cid,
            obs_type="registration",
            question_equivalence=q_equiv,
            answer_fact_status="n/a_registration",
            evidence_attribution="narrator_fact",
            closing_criteria_status="n/a_registration",
            verbatim_quote=cand_anchor_q,
            source_span=cand_span.to_dict(),
            anchor_span=anchor_span.to_dict(),
            adjudication_source="structural_semantic_verifier",
            adjudication_reason=q_reason,
            counted_as_hit=(q_equiv == "equivalent"),
            partial_answer=False,
            disputed=(q_equiv == "disputed"),
        )

    # Resolution adjudication
    if q_equiv != "equivalent":
        return PairAdjudicationRecord(
            ref_id=obs_id,
            candidate_id=cid,
            obs_type="resolution",
            question_equivalence=q_equiv,
            answer_fact_status="unverified_question_mismatch",
            evidence_attribution="missing_or_foreign",
            closing_criteria_status="unmet",
            verbatim_quote=str((verdict or {}).get("quote") or ""),
            source_span=SourceSpan(found=False, reason="question_not_equivalent").to_dict(),
            anchor_span=anchor_span.to_dict(),
            adjudication_source="structural_semantic_verifier",
            adjudication_reason=f"setup_question_not_equivalent:{q_reason}",
            counted_as_hit=False,
        )

    if not isinstance(verdict, Mapping) or verdict.get("verdict") != "SUPPORTED":
        v_name = str((verdict or {}).get("verdict") or "NONE")
        return PairAdjudicationRecord(
            ref_id=obs_id,
            candidate_id=cid,
            obs_type="resolution",
            question_equivalence=q_equiv,
            answer_fact_status=f"verdict_{v_name.lower()}",
            evidence_attribution="missing_or_foreign",
            closing_criteria_status="unmet",
            verbatim_quote=str((verdict or {}).get("quote") or ""),
            source_span=SourceSpan(found=False, reason=f"verdict_is_{v_name}").to_dict(),
            anchor_span=anchor_span.to_dict(),
            adjudication_source="structural_semantic_verifier",
            adjudication_reason=f"candidate_verdict_not_supported:{v_name}",
            counted_as_hit=False,
        )

    cand_ans = str(verdict.get("answer") or "")
    cand_quote = str(verdict.get("quote") or "")
    cand_ev_ch = int(verdict.get("chapter") or item.get("resolved_chapter") or obs.get("resolution_subchapter") or ref_anchor_ch)
    rep_span = verdict.get("located_span")
    rep_tuple = (int(rep_span[0]), int(rep_span[1])) if isinstance(rep_span, (list, tuple)) and len(rep_span) == 2 else None

    ev_span = indexer.locate(cand_quote, preferred_chapter=cand_ev_ch, reported_span=rep_tuple)
    after_ok, after_reason = check_span_strictly_after_anchor(anchor_span, ev_span)
    if not after_ok:
        return PairAdjudicationRecord(
            ref_id=obs_id,
            candidate_id=cid,
            obs_type="resolution",
            question_equivalence=q_equiv,
            answer_fact_status="invalid_evidence_span",
            evidence_attribution="missing_or_foreign",
            closing_criteria_status="unmet",
            verbatim_quote=cand_quote,
            source_span=ev_span.to_dict(),
            anchor_span=anchor_span.to_dict(),
            adjudication_source="structural_semantic_verifier",
            adjudication_reason=after_reason,
            counted_as_hit=False,
        )

    # Check sentence context around ev_span in the chapter text
    ch_text = indexer.chapters.get(ev_span.chapter, "")
    ctx_start = max(0, (ev_span.chapter_char_start or 0) - 25)
    ctx_end = min(len(ch_text), (ev_span.chapter_char_end or 0) + 25)
    surrounding_ctx = ch_text[ctx_start:ctx_end]

    mod_ok, ev_attr, mod_reason = check_answer_modality_escalation(cand_ans, cand_quote, surrounding_ctx)
    if not mod_ok:
        return PairAdjudicationRecord(
            ref_id=obs_id,
            candidate_id=cid,
            obs_type="resolution",
            question_equivalence=q_equiv,
            answer_fact_status="modality_overstated",
            evidence_attribution=ev_attr,
            closing_criteria_status="unmet",
            verbatim_quote=cand_quote,
            source_span=ev_span.to_dict(),
            anchor_span=anchor_span.to_dict(),
            adjudication_source="structural_semantic_verifier",
            adjudication_reason=mod_reason,
            counted_as_hit=False,
        )

    ref_truth = str(obs.get("resolution_quote") or "") + "；" + str(obs.get("answer_criteria") or "")
    pol_ok, pol_reason = check_negation_and_antonym_conflict(ref_truth, cand_ans, cand_quote)
    if not pol_ok:
        return PairAdjudicationRecord(
            ref_id=obs_id,
            candidate_id=cid,
            obs_type="resolution",
            question_equivalence=q_equiv,
            answer_fact_status="negation_or_antonym_conflict",
            evidence_attribution=ev_attr,
            closing_criteria_status="unmet",
            verbatim_quote=cand_quote,
            source_span=ev_span.to_dict(),
            anchor_span=anchor_span.to_dict(),
            adjudication_source="structural_semantic_verifier",
            adjudication_reason=pol_reason,
            counted_as_hit=False,
        )

    role_ok, role_reason = check_subject_object_swap(ref_truth, cand_ans)
    if not role_ok:
        return PairAdjudicationRecord(
            ref_id=obs_id,
            candidate_id=cid,
            obs_type="resolution",
            question_equivalence=q_equiv,
            answer_fact_status="role_swapped",
            evidence_attribution=ev_attr,
            closing_criteria_status="unmet",
            verbatim_quote=cand_quote,
            source_span=ev_span.to_dict(),
            anchor_span=anchor_span.to_dict(),
            adjudication_source="structural_semantic_verifier",
            adjudication_reason=role_reason,
            counted_as_hit=False,
        )

    # Check if reference has a specific resolution_quote span and whether candidate quote aligns with the resolution fact
    ref_res_q = str(obs.get("resolution_quote") or "")
    ref_res_ch = int(obs.get("resolution_subchapter") or obs.get("resolution_chapter") or cand_ev_ch)
    if abs(ev_span.chapter - ref_res_ch) > 1:
        return PairAdjudicationRecord(
            ref_id=obs_id,
            candidate_id=cid,
            obs_type="resolution",
            question_equivalence=q_equiv,
            answer_fact_status="wrong_resolution_chapter",
            evidence_attribution=ev_attr,
            closing_criteria_status="unmet",
            verbatim_quote=cand_quote,
            source_span=ev_span.to_dict(),
            anchor_span=anchor_span.to_dict(),
            adjudication_source="structural_semantic_verifier",
            adjudication_reason=f"resolution_chapter_mismatch:ev={ev_span.chapter}_vs_ref={ref_res_ch}",
            counted_as_hit=False,
        )

    clos_status, clos_reason = evaluate_closing_criteria_branches(
        str(obs.get("answer_criteria") or ""),
        cand_ans,
        cand_quote,
        prereg=obs.get("prereg"),
        required_branches=obs.get("required_branches"),
        logic_override=obs.get("criteria_logic"),
    )
    if clos_status == "partial_answer":
        return PairAdjudicationRecord(
            ref_id=obs_id,
            candidate_id=cid,
            obs_type="resolution",
            question_equivalence=q_equiv,
            answer_fact_status="partial_only",
            evidence_attribution=ev_attr,
            closing_criteria_status="partial_answer",
            verbatim_quote=cand_quote,
            source_span=ev_span.to_dict(),
            anchor_span=anchor_span.to_dict(),
            adjudication_source="structural_semantic_verifier",
            adjudication_reason=clos_reason,
            counted_as_hit=False,
            partial_answer=True,
        )
    if clos_status != "satisfied":
        return PairAdjudicationRecord(
            ref_id=obs_id,
            candidate_id=cid,
            obs_type="resolution",
            question_equivalence=q_equiv,
            answer_fact_status="unsupported",
            evidence_attribution=ev_attr,
            closing_criteria_status="unmet",
            verbatim_quote=cand_quote,
            source_span=ev_span.to_dict(),
            anchor_span=anchor_span.to_dict(),
            adjudication_source="structural_semantic_verifier",
            adjudication_reason=clos_reason,
            counted_as_hit=False,
        )

    # Verify new resolution fact overlap against reference resolution quote/criteria
    ref_res_clean = _clean_text(ref_res_q)
    ref_crit_clean = re.sub(r"^(交代|说明|确认|揭示|指出)", "", _clean_text(str(obs.get("answer_criteria") or "")))
    res_bgs = (
        ({ref_res_clean[i : i + 2] for i in range(len(ref_res_clean) - 1)} if len(ref_res_clean) >= 2 else set())
        | ({ref_crit_clean[i : i + 2] for i in range(len(ref_crit_clean) - 1)} if len(ref_crit_clean) >= 2 else set())
    )
    setup_bgs = {ref_a_clean[i : i + 2] for i in range(len(ref_a_clean) - 1)} if len(ref_a_clean) >= 2 else set()
    new_res_bgs = res_bgs - setup_bgs
    target_bgs = new_res_bgs if len(new_res_bgs) >= 2 else res_bgs
    cand_ev_clean = _clean_text(cand_ans) + " " + _clean_text(cand_quote)
    bg_hits = sum(1 for bg in target_bgs if bg in cand_ev_clean)

    if bg_hits < 2 and not (ref_res_clean and (_clean_text(cand_quote) in ref_res_clean or ref_res_clean in _clean_text(cand_quote))):
        return PairAdjudicationRecord(
            ref_id=obs_id,
            candidate_id=cid,
            obs_type="resolution",
            question_equivalence=q_equiv,
            answer_fact_status="unsupported",
            evidence_attribution=ev_attr,
            closing_criteria_status="unmet",
            verbatim_quote=cand_quote,
            source_span=ev_span.to_dict(),
            anchor_span=anchor_span.to_dict(),
            adjudication_source="structural_semantic_verifier",
            adjudication_reason=f"insufficient_resolution_fact_overlap:{bg_hits}",
            counted_as_hit=False,
        )

    return PairAdjudicationRecord(
        ref_id=obs_id,
        candidate_id=cid,
        obs_type="resolution",
        question_equivalence="equivalent",
        answer_fact_status="supported_fact",
        evidence_attribution="narrator_fact",
        closing_criteria_status="satisfied",
        verbatim_quote=cand_quote,
        source_span=ev_span.to_dict(),
        anchor_span=anchor_span.to_dict(),
        adjudication_source="structural_semantic_verifier",
        adjudication_reason=f"verified_resolution_fact_and_span:bg_hits={bg_hits}",
        counted_as_hit=True,
    )


def assign_exclusive_matches_with_conflict_log(
    observations: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
    verdicts_by_cid: Mapping[str, Mapping[str, Any]] | None,
    *,
    indexer: BookSpanIndexer,
    independent_reviews: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Perform 1-to-1 exclusive matching between reference observations and system candidates.

    Explicitly logs any multi-to-one conflicts (`multi_to_one_conflicts`) where multiple
    reference observations compete for the same system candidate, ensuring each candidate
    is rewarded at most once per observation type while preserving full auditability.
    """
    v_map = verdicts_by_cid or {}
    ind_map = independent_reviews or {}

    all_pair_records: dict[tuple[str, str], PairAdjudicationRecord] = {}
    hit_candidates_by_obs: dict[str, list[str]] = {}
    obs_hitting_candidate: dict[str, list[str]] = {}

    for obs in observations:
        oid = str(obs.get("obs_id") or obs.get("ref_id") or "")
        hit_candidates_by_obs[oid] = []
        for it in items:
            cid = str(it.get("id") or it.get("candidate_id") or "")
            rec = adjudicate_reference_candidate_pair(
                obs,
                it,
                v_map.get(cid),
                indexer=indexer,
                independent_review_entry=ind_map.get((oid, cid)),
            )
            all_pair_records[(oid, cid)] = rec
            if rec.counted_as_hit:
                hit_candidates_by_obs[oid].append(cid)
                obs_hitting_candidate.setdefault(cid, []).append(oid)

    # Log multi-to-one conflicts where >1 reference observation matched the same candidate
    multi_to_one_conflicts: list[dict[str, Any]] = []
    for cid, oids in sorted(obs_hitting_candidate.items()):
        if len(oids) > 1:
            multi_to_one_conflicts.append(
                {
                    "candidate_id": cid,
                    "competing_reference_ids": oids,
                    "resolution_policy": "exclusive_1to1_primary_kept_secondary_reassigned_or_logged_conflict",
                }
            )

    used_cids: set[str] = set()
    assigned_records: dict[str, PairAdjudicationRecord] = {}

    for obs in observations:
        oid = str(obs.get("obs_id") or obs.get("ref_id") or "")
        assigned: PairAdjudicationRecord | None = None
        for cid in hit_candidates_by_obs.get(oid, []):
            if cid not in used_cids:
                used_cids.add(cid)
                assigned = all_pair_records[(oid, cid)]
                break
        if assigned is None:
            # Check if it hit a candidate that was already claimed by another reference observation
            competing = hit_candidates_by_obs.get(oid, [])
            if competing:
                base_rec = all_pair_records[(oid, competing[0])]
                assigned = PairAdjudicationRecord(
                    ref_id=oid,
                    candidate_id=competing[0],
                    obs_type=base_rec.obs_type,
                    question_equivalence=base_rec.question_equivalence,
                    answer_fact_status="multi_to_one_duplicate_suppressed",
                    evidence_attribution=base_rec.evidence_attribution,
                    closing_criteria_status=base_rec.closing_criteria_status,
                    verbatim_quote=base_rec.verbatim_quote,
                    source_span=base_rec.source_span,
                    anchor_span=base_rec.anchor_span,
                    adjudication_source=base_rec.adjudication_source,
                    adjudication_reason=f"candidate_{competing[0]}_already_claimed_by_another_reference_item",
                    counted_as_hit=False,
                    partial_answer=base_rec.partial_answer,
                    disputed=base_rec.disputed,
                )
            else:
                # Pick best diagnostic non-hit record (e.g. partial_answer, disputed, or ambiguous)
                best_diag: PairAdjudicationRecord | None = None
                for it in items:
                    cid = str(it.get("id") or it.get("candidate_id") or "")
                    rec = all_pair_records[(oid, cid)]
                    if rec.disputed or rec.partial_answer or rec.question_equivalence in ("equivalent", "ambiguous"):
                        best_diag = rec
                        break
                if best_diag is None:
                    best_diag = PairAdjudicationRecord(
                        ref_id=oid,
                        candidate_id="",
                        obs_type=str(obs.get("obs_type") or "registration"),
                        question_equivalence="non_equivalent",
                        answer_fact_status="unmatched",
                        evidence_attribution="missing_or_foreign",
                        closing_criteria_status="unmet",
                        verbatim_quote="",
                        source_span=SourceSpan(found=False, reason="no_candidate_matched").to_dict(),
                        anchor_span=indexer.locate(
                            str(obs.get("anchor_quote") or ""),
                            preferred_chapter=int(obs.get("anchor_subchapter") or obs.get("anchor_chapter") or 1),
                        ).to_dict(),
                        adjudication_source="structural_semantic_verifier",
                        adjudication_reason="no_matching_candidate_in_pool",
                        counted_as_hit=False,
                    )
                assigned = best_diag
        assigned_records[oid] = assigned

    hits = sum(1 for r in assigned_records.values() if r.counted_as_hit)
    partial_count = sum(1 for r in assigned_records.values() if r.partial_answer)
    disputed_count = sum(1 for r in assigned_records.values() if r.disputed)

    return {
        "total_reference_observations": len(observations),
        "strict_hits": hits,
        "recall": round(hits / max(len(observations), 1), 4),
        "partial_answers_count": partial_count,
        "disputed_count": disputed_count,
        "multi_to_one_conflicts": multi_to_one_conflicts,
        "records_by_ref_id": {oid: rec.to_dict() for oid, rec in assigned_records.items()},
    }


def classify_c_observable_branch_outcome(
    cur_row: Mapping[str, Any],
    v2_row: Mapping[str, Any],
) -> str:
    """Directly observable branch outcome classification (R3-4: never assumes candidate error without source review)."""
    v2_reasons = list(v2_row.get("reasons") or [])
    v2_claim_chk = v2_row.get("claim_check") or {}
    if (
        v2_claim_chk.get("errors")
        or set(v2_reasons)
        & {
            "unknown_answer_frame",
            "unknown_evidence_frame",
            "answer_not_fully_partitioned",
            "missing_answer_unit",
            "model_call_failed",
        }
        or any("NLI 调用失败" in str(r) for r in v2_reasons)
    ):
        return "protocol_or_format_failure"

    cur_acc = cur_row.get("decision") == "接受"
    v2_acc = v2_row.get("decision") == "接受"
    if cur_acc and v2_acc:
        return "both_accepted"
    if not cur_acc and not v2_acc:
        return "both_not_accepted"
    if cur_acc and not v2_acc:
        return "branch_disagreement_cur_accept_v2_not"
    return "branch_disagreement_v2_accept_cur_not"
