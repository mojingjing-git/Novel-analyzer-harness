"""原文证据与全自动裁决：把模型的"核对答复"变成程序可判定的状态。

接口分工（PLAN v2 §4 S6 的接口 B）：

* 调和阶段提出候选答案 —— 它永远是**假设**，不是结论；
* 复检阶段读"原悬念 + 候选答案 + 埋设原文 + 回收上下文"，逐条给出固定四枚举的判定；
* **程序裁决**：校验 ID 与范围、把不合法响应打成 ``INVALID``、按候选归约到实体。
  程序不比较自由文本，不做语义判断，也不在裁决器内部再调模型。

三条最容易写错的规则，单独强调：

1. **未知枚举不能当通过。** 模型返回 ``"supported"``（小写）或 ``"OK"`` 都算解析失败，
   记 ``INVALID`` 并给出 reason_code，**绝不缓存成语义通过**。一次"宽容解析"会让
   后面的所有环节都以为这条已经核对过。
2. **反证核对必须显式执行。** ``counter_evidence = null`` 不能单独放行：模型完全没查反证
   与"查了没有反证"在数据里必须可区分，否则"没查"会被读成"确证"。
3. **失效只影响自己。** 一条新来的垃圾候选不能让另一个已被支持、且证据独立的结论降级；
   被推翻的旧结论要留 ``superseded_by``，而不是静默消失。
"""

from __future__ import annotations

from . import citations

import hashlib
import json

from dataclasses import dataclass, field
from typing import Callable, Final, Iterable, Mapping

# 模型只允许给出这四个结论（固定枚举，不接受近义词）
SUPPORTED = "SUPPORTED"
DIFFERENT_ANSWER = "DIFFERENT_ANSWER"
CONTRADICTED = "CONTRADICTED"
INSUFFICIENT = "INSUFFICIENT"

# 程序侧状态（模型无法直接产生）
INVALID = "INVALID"

MODEL_VERDICTS = (SUPPORTED, DIFFERENT_ANSWER, CONTRADICTED, INSUFFICIENT)

# 实体级归约后的状态
AUTO_RESOLVED = "AUTO_RESOLVED"
PROPOSED_RESOLVED = "PROPOSED_RESOLVED"
OPEN = "OPEN"

# 复检任务状态（与 resolution_state 正交：前者=任务跑完没跑完，后者=悬念解决没解决）
NOT_RUN = "NOT_RUN"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
PARTIAL = "PARTIAL"
STOPPED = "STOPPED"

# reason_code：程序侧判 INVALID 的原因，全部可枚举、可统计
REASON_UNKNOWN_VERDICT = "UNKNOWN_VERDICT"
REASON_MISSING_FIELD = "MISSING_FIELD"
REASON_FOREIGN_ID = "FOREIGN_ID"
REASON_NO_COUNTER_SEARCH = "NO_COUNTER_SEARCH"
REASON_COUNTER_QUOTES_PRESENT = "COUNTER_QUOTES_PRESENT"
#: 反证检索的声明无法核实（没给 ID、或 ID 不属于本任务）
REASON_COUNTER_SEARCH_UNVERIFIED = "COUNTER_SEARCH_UNVERIFIED"
REASON_MATERIAL_INSUFFICIENT = "MATERIAL_INSUFFICIENT"
REASON_QUOTE_OUT_OF_RANGE = "QUOTE_OUT_OF_RANGE"
REASON_EMPTY_ANSWER = "EMPTY_ANSWER"
REASON_MALFORMED = "MALFORMED_PAYLOAD"
# 2026-10-30 复审修正：证据门禁的三个缺口
REASON_NO_EVIDENCE = "NO_EVIDENCE_ID"
REASON_NO_QUOTE = "NO_SUPPORTING_QUOTE"
REASON_NO_VERIFIER = "NO_QUOTE_VERIFIER"
# 2026-10-31 D-19：偏移不再由模型负责
REASON_QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
REASON_QUOTE_TOO_SHORT = "QUOTE_TOO_SHORT"
REASON_AMBIGUOUS_QUOTE = "AMBIGUOUS_QUOTE"

#: 引文最短长度。低于这个长度的句子容易在长段里"必然命中"（实测语料里
#: 「这一日无事」出现 13 次），等于把闸门交给巧合。
MIN_QUOTE_CHARS = 10

#: 接受为证据的引用匹配级别。`SEGMENTED`（省略号拼引文）与 `FUZZY`（相似即过）
#: 一律不接受——它们会把"编一句像样的话"变成证据。
ACCEPTED_CITATION_KINDS = frozenset({"EXACT", "NORMALIZED", "RELOCATED"})


@dataclass(frozen=True)
class CandidateVerdict:
    """一条候选的裁决结果（候选级，不是实体级）。"""

    candidate_id: str
    verdict: str
    answer: str = ""
    evidence_id: str | None = None
    supporting_span: tuple[int, int] | None = None
    counter_search_performed: bool = False
    reason: str = ""
    #: 解析失败（枚举不认识、字段缺失、payload 不是对象）可重跑；语义结论不可重跑。
    #: 区分这两者是为了不让"模型这次没按格式答"变成"这个候选被判无效"。
    retryable: bool = False
    #: 程序定位出的位置（唯一权威）；`supporting_span` 只是模型自报，存 `reported_span`
    located_span: tuple[int, int] | None = None
    reported_span: tuple[int, int] | None = None
    #: 引文在该证据里出现多次且靠 span 才定位到其中一处
    ambiguous: bool = False
    #: sha256(引文)[:12]：用于账本去重键，避免"位置微调"被当成新结论
    quote_hash: str = ""

    @property
    def checked(self) -> bool:
        """是否真的核对过（可计入 COMPLETED 的分母）。"""
        return self.cached_as_terminal and not self.retryable

    @property
    def cached_as_terminal(self) -> bool:
        """只有"真的核对过"的结果才能缓存复用。

        ``INVALID`` 不在此列的那部分（解析失败）也不该缓存成"已完成"——但它本身
        是终态（重复询问同一个候选不会得到更好的结果），所以按终态处理，
        只是不计入"已核对数"。
        """
        return self.verdict in (SUPPORTED, DIFFERENT_ANSWER, CONTRADICTED, INSUFFICIENT)


#: 模型回包被读取的全部键名——**唯一来源**。
#: 2026-10-31 事故：Prompt 与解析器各自手写字段名，两轮共 36 次调用（约 109k token）
#: 全部因键名不一致被判 ``NO_EVIDENCE_ID`` / ``NO_SUPPORTING_QUOTE``。
#: 现在 Prompt 由 :func:`render_recheck_schema` 渲染，而用例
#: ``test_vnext_schema_keys`` 双向核对"常量里的键"与"解析器实际读取的键"。
RECHECK_MODEL_KEYS: Final[Mapping[str, object]] = {
    "candidate_id": "candidate_id",
    "verdict": "verdict",
    "answer": "answer",
    "material_insufficient_reason": "material_insufficient_reason",
    "evidence_id": "evidence_id",
    "supporting_span": "supporting_span",
    "supporting_quote": "supporting_quote",
    "counter_search": {
        "performed": "performed",
        "checked_evidence_ids": "checked_evidence_ids",
        "counter_quotes": "counter_quotes",
    },
}


def iter_model_keys(keys: Mapping[str, object] | None = None, prefix: str = ""):
    """按 ``a.b`` 形式逐层列出键（含嵌套）。"""
    for name, value in (keys or RECHECK_MODEL_KEYS).items():
        path = f"{prefix}{name}"
        if isinstance(value, Mapping):
            yield from iter_model_keys(value, prefix=f"{path}.")
        else:
            yield path


def render_recheck_schema(*, indent: int = 2) -> str:
    """由 :data:`RECHECK_MODEL_KEYS` 渲染出给模型看的 JSON 字段示例。

    占位值只是"类型示意"，不参与解析；键名才是契约。
    """
    sample: dict[str, object] = {}
    for name, value in RECHECK_MODEL_KEYS.items():
        if isinstance(value, Mapping):
            sample[name] = {key: f"<{key}>" for key in value}
        else:
            sample[name] = f"<{name}>"
    return json.dumps(sample, ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class LocateResult:
    """一次引文定位的结果。"""

    found: bool
    start: int | None = None
    end: int | None = None
    occurrences: int = 0
    ambiguous: bool = False
    kind: str = ""


def count_occurrences(text: str, quote: str) -> list[int]:
    """列出引文在文本里的**全部**起点（含重叠）。

    ``"哈哈哈".count("哈哈") == 1``，但实际有 2 个位置——所以不能用 ``str.count``。
    """
    positions: list[int] = []
    pos = text.find(quote)
    while pos >= 0:
        positions.append(pos)
        pos = text.find(quote, pos + 1)
    return positions


def _normalized(text: str) -> str:
    """尽量复用 citations 的规范化口径，避免第二套语义。"""
    normalize = getattr(citations, "_normalize", None)
    return normalize(text) if callable(normalize) else text


def locate_quote(
    quote: str, text: str, *, reported: tuple[int, int] | None = None
) -> LocateResult:
    """在**指定证据的正文**里定位引文，返回权威位置。

    - 只接受 ``EXACT/NORMALIZED/RELOCATED``；
    - 定位后按级别回验（严等或双侧规范化相等），不通过就当没找到；
    - 多处出现时用 ``reported`` 破歧义：窗口内恰含一次就采纳它，否则标 ambiguous。
    """
    if not quote or not text:
        return LocateResult(False)
    if len(quote) < MIN_QUOTE_CHARS:
        return LocateResult(False)

    # 1) 先做最硬的一步：逐字查找。C2/C3 的实测引文 30/30 都是逐字子串，
    #    绝大多数情况在这里就结束了，不必依赖映射。
    positions = count_occurrences(text, quote)
    if positions:
        return _from_positions(positions, len(quote), "EXACT", reported)

    # 2) 逐字找不到时才退到 citations 的容忍匹配（NORMALIZED/RELOCATED），
    #    并且必须回验：切片与引文规范化后相等才算数。
    check = citations.verify_citation(quote_text=quote, chapter_text=text)
    if not check.located or check.match_kind not in ACCEPTED_CITATION_KINDS:
        return LocateResult(False, kind=check.match_kind or "")
    n_start, n_end = check.corrected_start, check.corrected_end
    if not (isinstance(n_start, int) and isinstance(n_end, int) and 0 <= n_start < n_end <= len(text)):
        return LocateResult(False, kind=check.match_kind)
    if _normalized(text[n_start:n_end]) != _normalized(quote):
        return LocateResult(False, kind=check.match_kind)
    return LocateResult(True, n_start, n_end, occurrences=1, kind=check.match_kind)


def _from_positions(
    positions: list[int], length: int, kind: str, reported: tuple[int, int] | None
) -> LocateResult:
    """多处出现时用模型自报窗口破歧义；破不了就标 ambiguous。"""
    if len(positions) == 1:
        return LocateResult(True, positions[0], positions[0] + length, occurrences=1, kind=kind)
    if reported is not None:
        inside = [p for p in positions if reported[0] <= p and p + length <= reported[1]]
        if len(inside) == 1:
            return LocateResult(
                True, inside[0], inside[0] + length, occurrences=len(positions), ambiguous=True, kind=kind
            )
    return LocateResult(
        True, positions[0], positions[0] + length,
        occurrences=len(positions), ambiguous=True, kind=kind,
    )


def _reported_span(raw: object) -> tuple[int, int] | None:
    """模型自报位置：形状非法就当没给（不判失败）。"""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        a, b = raw
        if isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool) and 0 <= a < b:
            return (a, b)
    return None


def parse_recheck(
    payload: Mapping[str, object],
    *,
    allowed_candidate_ids: Iterable[str],
    allowed_evidence_ids: Iterable[str],
    quote_within_range: Callable[[str, int, int], bool] | None = None,
    locate: Callable[[str, str], LocateResult] | None = None,
    evidence_text: Callable[[str], str] | None = None,
) -> CandidateVerdict:
    """把模型返回的一条核对结果变成受校验的裁决。

    校验顺序：结构 → 枚举 → ID 归属 → 答案非空 → 反证已执行 → 支持引文落在给定范围内。
    任何一步失败都返回 ``INVALID`` 并带 reason_code，**不抛异常**：一条坏响应
    不该让整批复检崩掉，但必须被计数。
    """
    if not isinstance(payload, Mapping):
        return CandidateVerdict("", INVALID, reason=REASON_MALFORMED, retryable=True)

    candidate_id = payload.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        return CandidateVerdict("", INVALID, reason=REASON_MISSING_FIELD, retryable=True)
    if candidate_id not in set(allowed_candidate_ids):
        # 答非所问：占了一个不属于本任务的 ID，这不是"这次没答好"，而是明显的错配
        return CandidateVerdict(candidate_id, INVALID, reason=REASON_FOREIGN_ID)

    raw_verdict = payload.get("verdict")
    # 严格枚举：大小写不同、近义词、缺失值都算解析失败——不能"宽容"成通过
    if raw_verdict not in MODEL_VERDICTS:
        return CandidateVerdict(
            candidate_id, INVALID, reason=REASON_UNKNOWN_VERDICT, retryable=True
        )

    answer = payload.get("answer")
    answer = answer.strip() if isinstance(answer, str) else ""
    counter = payload.get("counter_search")
    performed = bool(isinstance(counter, Mapping) and counter.get("performed") is True)

    if raw_verdict == SUPPORTED:
        # 下面这些"没答好"的情形一律落 INSUFFICIENT（材料/核对不足），不是 INVALID：
        # INVALID 的含义是"这条响应不可用"，INSUFFICIENT 的含义是"原文没给出足够证据"。
        # 前者会被当作候选本身有问题，后者才是这条悬念的真实处境。
        if not answer:
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_EMPTY_ANSWER)
        if payload.get("material_insufficient_reason"):
            return CandidateVerdict(
                candidate_id, INSUFFICIENT, reason=REASON_MATERIAL_INSUFFICIENT
            )
        if not performed:
            return CandidateVerdict(
                candidate_id, INSUFFICIENT, reason=REASON_NO_COUNTER_SEARCH
            )
        assert isinstance(counter, Mapping)
        counter_quotes = counter.get("counter_quotes") or []
        if counter_quotes:
            # 说"支持"却又列了反证：自相矛盾，交给人/下一轮，不能算支持
            return CandidateVerdict(
                candidate_id, INSUFFICIENT, reason=REASON_COUNTER_QUOTES_PRESENT
            )

        # ---- 证据门禁：没有"可定位的原文"，就不允许 SUPPORTED ----
        # 2026-10-30 复审：原先 evidence_id / span / quote 全缺也能过，于是
        # "模型觉得答对了"就成了 AUTO_RESOLVED。没有证据的支持只是又一次猜测。
        evidence_id = payload.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_NO_EVIDENCE)
        if evidence_id not in set(allowed_evidence_ids):
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_FOREIGN_ID)

        quote = payload.get("supporting_quote")
        if not isinstance(quote, str) or not quote.strip():
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_NO_QUOTE, retryable=True)
        quote = quote.strip()
        if len(quote) < MIN_QUOTE_CHARS:
            return CandidateVerdict(
                candidate_id, INVALID, reason=REASON_QUOTE_TOO_SHORT, retryable=True
            )
        reported = _reported_span(payload.get("supporting_span"))

        located: tuple[int, int] | None = None
        ambiguous = False
        if locate is not None:
            # 调用方注入的定位器**真的被调用**：签名带上 evidence_id，
            # 才能验"这段引文属于点名的那一段"（旧校验器做不到这件事）。
            result = locate(quote, evidence_id)
            if not result.found:
                return CandidateVerdict(
                    candidate_id, INVALID, reason=REASON_QUOTE_NOT_FOUND, retryable=True
                )
            if result.ambiguous and reported is None:
                # 出现在多处又没给 span：说不清是哪一处，宁可当"这次没答好"
                return CandidateVerdict(
                    candidate_id, INVALID, reason=REASON_AMBIGUOUS_QUOTE, retryable=True
                )
            if result.ambiguous and reported is not None and reported[1] - reported[0] >= len(quote):
                # 定位器只拿到 (quote, evidence_id)，破不了歧义时用模型自报窗口指路：
                # 窗口长度够装下引文就可采纳，并如实标 ambiguous
                located = (int(reported[0]), int(reported[0]) + len(quote))
            else:
                located = (int(result.start), int(result.end))
            ambiguous = bool(result.ambiguous)
        elif quote_within_range is not None:
            # 退化模式：只给旧校验器时沿用改前行为（span 必填、逐字对上）
            if reported is None:
                return CandidateVerdict(candidate_id, INVALID, reason=REASON_NO_QUOTE, retryable=True)
            if not quote_within_range(quote, reported[0], reported[1]):
                return CandidateVerdict(
                    candidate_id, INVALID, reason=REASON_QUOTE_OUT_OF_RANGE, retryable=True
                )
            located = reported
        else:
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_NO_VERIFIER)

        # "查过反证"不能只凭 performed=True 一句话：它声明查了哪些 ID，这些 ID 必须
        # 属于本任务。否则模型写一个 outside_this_task 就能把这道门糊过去。
        checked_ids = counter.get("checked_evidence_ids")
        if not isinstance(checked_ids, (list, tuple)) or not checked_ids:
            return CandidateVerdict(
                candidate_id, INSUFFICIENT, reason=REASON_COUNTER_SEARCH_UNVERIFIED
            )
        allowed = set(allowed_evidence_ids)
        foreign = [cid for cid in checked_ids if not isinstance(cid, str) or cid not in allowed]
        if foreign:
            return CandidateVerdict(
                candidate_id, INSUFFICIENT, reason=REASON_COUNTER_SEARCH_UNVERIFIED
            )

        return CandidateVerdict(
            candidate_id, raw_verdict, answer=answer,
            evidence_id=evidence_id,
            # supporting_span 只存模型自报值；权威位置是 located_span（程序算的）
            supporting_span=reported,
            located_span=located,
            reported_span=reported,
            ambiguous=ambiguous,
            quote_hash=hashlib.sha256(quote.encode("utf-8")).hexdigest()[:12],
            counter_search_performed=True,
        )

    # 其余三个枚举：答案可以留空（"答不对"本身也是结论），但仍需记录反证是否执行
    return CandidateVerdict(
        candidate_id,
        raw_verdict,
        answer=answer,
        counter_search_performed=performed,
    )


def reduce_entity(verdicts: Iterable[CandidateVerdict]) -> tuple[str, CandidateVerdict | None]:
    """按 §2.3 把同一实体的所有当前有效候选归约成实体状态。

    * 任一条 ``SUPPORTED`` → ``AUTO_RESOLVED``（并返回那条作为依据）；
    * 没有任何 SUPPORTED，但存在 ``INSUFFICIENT``/``DIFFERENT_ANSWER``/``CONTRADICTED``
      的有效核对 → ``PROPOSED_RESOLVED``（有候选、证据不足或冲突）；
    * 只有 ``INVALID`` 或完全没有候选 → ``OPEN``。

    **一条 INVALID 不会把已有 SUPPORTED 拉下来**——垃圾候选只影响它自己。
    """
    supported = None
    has_evidence_bearing = False
    for verdict in verdicts:
        if verdict.verdict == SUPPORTED:
            supported = verdict
            break
        if verdict.verdict in (INSUFFICIENT, DIFFERENT_ANSWER, CONTRADICTED):
            has_evidence_bearing = True
    if supported is not None:
        return AUTO_RESOLVED, supported
    if has_evidence_bearing:
        return PROPOSED_RESOLVED, None
    return OPEN, None


@dataclass(frozen=True)
class CoverageCounts:
    """一次复检的覆盖计数。缺了它，就不许把这次复检标成完成。"""

    candidates_total: int = 0
    checked: int = 0
    located: int = 0
    supported: int = 0
    insufficient: int = 0
    conflict: int = 0
    invalid: int = 0
    stale: int = 0
    not_checked: int = 0
    invalid_by_reason: dict[str, int] = field(default_factory=dict)
    #: 同一 candidate 出现多行裁决（批次不合法：同一个候选不能有两个结论）
    duplicates: int = 0
    #: 不属于本批候选集合的裁决行（模型答非所问）
    foreign: int = 0
    #: 是否拿到了"预期候选集合"。没有它就无法核对覆盖完整性——数字能对上也可能漏了候选
    expected_known: bool = True

    def conservation_error(self) -> str | None:
        """候选维度守恒检查：每个候选必须**恰好**落进一个终态桶。

        ``conflict`` 由 ``DIFFERENT_ANSWER + CONTRADICTED`` 组成（都是"原文给了另一个答案"），
        因此等式里只出现一次。
        """
        total = self.supported + self.insufficient + self.conflict + self.invalid \
            + self.stale + self.not_checked
        if total != self.candidates_total:
            return (f"候选守恒被破坏：总数 {self.candidates_total} != "
                    f"分项之和 {total}")
        if self.checked > self.candidates_total:
            return f"已核对数 {self.checked} 超过候选总数 {self.candidates_total}"
        if sum(self.invalid_by_reason.values()) != self.invalid:
            return (f"INVALID 分项之和 {sum(self.invalid_by_reason.values())} "
                    f"!= INVALID 总数 {self.invalid}")
        return None

    def integrity_error(self) -> str | None:
        if not self.expected_known:
            return "缺少预期候选集合，无法核对覆盖完整性"
        """批次完整性：重复行与外来行都不影响守恒，但必须让这轮复检不能被标 COMPLETED。

        数字加起来对不代表核对过——同一候选给两次答案时，和照样能凑齐，但其中一行的
        结论根本没被采用过。
        """
        if self.duplicates:
            return f"有 {self.duplicates} 行重复候选裁决（同一候选出现多次）"
        if self.foreign:
            return f"有 {self.foreign} 行裁决不属于本批候选"
        return None

    def as_dict(self) -> dict:
        return {
            "candidates_total": self.candidates_total,
            "checked": self.checked,
            "located": self.located,
            "supported": self.supported,
            "insufficient": self.insufficient,
            "conflict": self.conflict,
            "invalid": self.invalid,
            "invalid_by_reason": dict(sorted(self.invalid_by_reason.items())),
            "duplicates": self.duplicates,
            "foreign": self.foreign,
            "stale": self.stale,
            "not_checked": self.not_checked,
            "expected_known": self.expected_known,
        }


def count_verdicts(
    verdicts: Iterable[CandidateVerdict],
    *,
    expected_candidate_ids: Iterable[str] | None = None,
    candidates_total: int | None = None,
    stale: int = 0,
) -> CoverageCounts:
    """按**预期候选集合**逐项归类，而不是数返回了多少行。

    差别在两种真实场景：

    * 模型把同一个候选答了两次 → 数行数会得到 ``supported=2, not_checked=0``，
      看起来"两条都核对完了"；按集合归类后它是 1 条重复 + 其余未核对。
    * 模型答了本批之外的 ID → 数行数会把它算进分母，凑巧还能守恒。

    因此优先用 ``expected_candidate_ids``；只给 ``candidates_total`` 时退化为
    "没有预期集合"的宽容模式（仍能挡住外来行与重复行，但无法报"缺了哪几条"）。
    """
    rows = list(verdicts)
    expected = list(expected_candidate_ids) if expected_candidate_ids is not None else None
    total = len(expected) if expected is not None else int(candidates_total or 0)

    supported = insufficient = conflict = invalid = located = 0
    by_reason: dict[str, int] = {}
    checked = 0
    duplicates = 0
    foreign = 0
    not_checked = 0

    def _bucket(verdict: CandidateVerdict) -> None:
        nonlocal supported, insufficient, conflict, invalid, located, checked
        if verdict.verdict == SUPPORTED:
            supported += 1
        elif verdict.verdict in (DIFFERENT_ANSWER, CONTRADICTED):
            conflict += 1
        elif verdict.verdict == INSUFFICIENT:
            insufficient += 1
        else:  # INVALID
            invalid += 1
            reason = verdict.reason or "UNSPECIFIED"
            by_reason[reason] = by_reason.get(reason, 0) + 1
        if verdict.checked:
            checked += 1
        if verdict.located_span is not None:
            located += 1

    if expected is None:
        seen_ids: set[str] = set()
        for verdict in rows:
            if verdict.candidate_id in seen_ids:
                duplicates += 1
            seen_ids.add(verdict.candidate_id)
            _bucket(verdict)
        accounted = supported + insufficient + conflict + invalid + stale
        not_checked = max(0, total - accounted)
    else:
        by_id: dict[str, list[CandidateVerdict]] = {}
        for verdict in rows:
            if verdict.candidate_id in set(expected):
                by_id.setdefault(verdict.candidate_id, []).append(verdict)
            else:
                foreign += 1
        for candidate_id in expected:
            got = by_id.get(candidate_id, [])
            if not got:
                not_checked += 1
                continue
            if len(got) > 1:
                # 同一候选两个结论：采用不了，按无效计，并把多出来的行数记进 duplicates
                duplicates += len(got) - 1
                invalid += 1
                by_reason["DUPLICATE_CANDIDATE"] = by_reason.get("DUPLICATE_CANDIDATE", 0) + 1
                continue
            verdict = got[0]
            if not verdict.checked:
                # 解析失败可重跑：属于"还没核对"，不是"核对完了但无效"
                not_checked += 1
                continue
            _bucket(verdict)

    return CoverageCounts(
        candidates_total=total,
        # 候选总数为 0 时无需预期集合也能确认完整（空集合是已知的）
        expected_known=(expected is not None) or total == 0,
        checked=checked,
        located=located,
        supported=supported,
        insufficient=insufficient,
        conflict=conflict,
        invalid=invalid,
        stale=stale,
        not_checked=not_checked,
        invalid_by_reason=by_reason,
        duplicates=duplicates,
        foreign=foreign,
    )


def recheck_status(
    *,
    started: bool,
    finished: bool,
    stopped: bool,
    counts: CoverageCounts | None,
) -> str:
    """有序互斥地判定复检任务状态（§2.3 的口径）。

    顺序：没开始 → ``NOT_RUN``；已开始未结束 → ``RUNNING``；被中断 → ``STOPPED``；
    有未核对但已跑过一轮 → ``PARTIAL``；否则 ``COMPLETED``。

    **候选为空也必须走完这条链**：空候选 + 跑过 = ``COMPLETED``（计数 0），
    空候选 + 没跑 = ``NOT_RUN``。这两者在产物里必须可区分，否则"没运行"会显示成
    "已核对、无发现"。
    """
    if not started:
        return NOT_RUN
    if not finished:
        return RUNNING if not stopped else STOPPED
    if stopped:
        return STOPPED
    if counts is None:
        # 跑完了却没有覆盖汇总 → 不许标记完成
        return PARTIAL
    error = counts.conservation_error()
    if error is not None:
        return PARTIAL
    # 数字加起来对不代表核对过：重复行/外来行让这轮不能被标完成
    # （2026-10-30 复审 #4：同一候选答两次时和照样凑齐，但其中一行没被采用）
    if counts.integrity_error() is not None:
        return PARTIAL
    if counts.not_checked > 0:
        return PARTIAL
    return COMPLETED
