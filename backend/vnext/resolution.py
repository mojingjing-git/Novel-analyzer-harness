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

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

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
REASON_MATERIAL_INSUFFICIENT = "MATERIAL_INSUFFICIENT"
REASON_QUOTE_OUT_OF_RANGE = "QUOTE_OUT_OF_RANGE"
REASON_EMPTY_ANSWER = "EMPTY_ANSWER"
REASON_MALFORMED = "MALFORMED_PAYLOAD"


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

    @property
    def cached_as_terminal(self) -> bool:
        """只有"真的核对过"的结果才能缓存复用。

        ``INVALID`` 不在此列的那部分（解析失败）也不该缓存成"已完成"——但它本身
        是终态（重复询问同一个候选不会得到更好的结果），所以按终态处理，
        只是不计入"已核对数"。
        """
        return self.verdict in (SUPPORTED, DIFFERENT_ANSWER, CONTRADICTED, INSUFFICIENT)


def parse_recheck(
    payload: Mapping[str, object],
    *,
    allowed_candidate_ids: Iterable[str],
    allowed_evidence_ids: Iterable[str],
    quote_within_range: Callable[[str, int, int], bool] | None = None,
) -> CandidateVerdict:
    """把模型返回的一条核对结果变成受校验的裁决。

    校验顺序：结构 → 枚举 → ID 归属 → 答案非空 → 反证已执行 → 支持引文落在给定范围内。
    任何一步失败都返回 ``INVALID`` 并带 reason_code，**不抛异常**：一条坏响应
    不该让整批复检崩掉，但必须被计数。
    """
    if not isinstance(payload, Mapping):
        return CandidateVerdict("", INVALID, reason=REASON_MALFORMED)

    candidate_id = payload.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        return CandidateVerdict("", INVALID, reason=REASON_MISSING_FIELD)
    if candidate_id not in set(allowed_candidate_ids):
        return CandidateVerdict(candidate_id, INVALID, reason=REASON_FOREIGN_ID)

    raw_verdict = payload.get("verdict")
    # 严格枚举：大小写不同、近义词、缺失值都算解析失败——不能"宽容"成通过
    if raw_verdict not in MODEL_VERDICTS:
        return CandidateVerdict(candidate_id, INVALID, reason=REASON_UNKNOWN_VERDICT)

    answer = payload.get("answer")
    answer = answer.strip() if isinstance(answer, str) else ""
    counter = payload.get("counter_search")
    performed = bool(isinstance(counter, Mapping) and counter.get("performed") is True)

    if raw_verdict == SUPPORTED:
        if not answer:
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_EMPTY_ANSWER)
        if payload.get("material_insufficient_reason"):
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_MATERIAL_INSUFFICIENT)
        if not performed:
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_NO_COUNTER_SEARCH)
        assert isinstance(counter, Mapping)
        counter_quotes = counter.get("counter_quotes") or []
        if counter_quotes:
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_COUNTER_QUOTES_PRESENT)

        evidence_id = payload.get("evidence_id")
        span = payload.get("supporting_span")
        if evidence_id is not None:
            if evidence_id not in set(allowed_evidence_ids):
                return CandidateVerdict(candidate_id, INVALID, reason=REASON_FOREIGN_ID)
        if isinstance(span, (list, tuple)) and len(span) == 2:
            start, end = span
            if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end):
                return CandidateVerdict(candidate_id, INVALID, reason=REASON_QUOTE_OUT_OF_RANGE)
            quote = payload.get("supporting_quote")
            if quote_within_range is not None and isinstance(quote, str) and quote:
                if not quote_within_range(quote, start, end):
                    return CandidateVerdict(candidate_id, INVALID, reason=REASON_QUOTE_OUT_OF_RANGE)
            return CandidateVerdict(
                candidate_id, raw_verdict, answer=answer,
                evidence_id=evidence_id if isinstance(evidence_id, str) else None,
                supporting_span=(start, end), counter_search_performed=True,
            )
        return CandidateVerdict(
            candidate_id, raw_verdict, answer=answer,
            evidence_id=evidence_id if isinstance(evidence_id, str) else None,
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
            "stale": self.stale,
            "not_checked": self.not_checked,
        }


def count_verdicts(
    verdicts: Iterable[CandidateVerdict],
    *,
    candidates_total: int,
    stale: int = 0,
) -> CoverageCounts:
    """把一批裁决统计成覆盖计数（``conflict`` = DIFFERENT_ANSWER + CONTRADICTED）。"""
    supported = insufficient = conflict = invalid = located = 0
    by_reason: dict[str, int] = {}
    checked = 0
    for verdict in verdicts:
        if verdict.verdict == SUPPORTED:
            supported += 1
        elif verdict.verdict == DIFFERENT_ANSWER or verdict.verdict == CONTRADICTED:
            conflict += 1
        elif verdict.verdict == INSUFFICIENT:
            insufficient += 1
        elif verdict.verdict == INVALID:
            invalid += 1
            by_reason[verdict.reason or "UNSPECIFIED"] = \
                by_reason.get(verdict.reason or "UNSPECIFIED", 0) + 1
        if verdict.cached_as_terminal:
            checked += 1
        if verdict.supporting_span is not None:
            located += 1

    accounted = supported + insufficient + conflict + invalid + stale
    return CoverageCounts(
        candidates_total=candidates_total,
        checked=checked,
        located=located,
        supported=supported,
        insufficient=insufficient,
        conflict=conflict,
        invalid=invalid,
        stale=stale,
        not_checked=max(0, candidates_total - accounted),
        invalid_by_reason=by_reason,
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
    if counts.not_checked > 0:
        return PARTIAL
    return COMPLETED
