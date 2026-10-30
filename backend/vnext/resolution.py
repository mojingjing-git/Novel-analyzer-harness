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
# 2026-10-30 复审修正：证据门禁的三个缺口
REASON_NO_EVIDENCE = "NO_EVIDENCE_ID"
REASON_NO_QUOTE = "NO_SUPPORTING_QUOTE"
REASON_NO_VERIFIER = "NO_QUOTE_VERIFIER"


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

        span = payload.get("supporting_span")
        if not (isinstance(span, (list, tuple)) and len(span) == 2):
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_NO_QUOTE)
        start, end = span[0], span[1]
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end):
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_QUOTE_OUT_OF_RANGE)

        quote = payload.get("supporting_quote")
        if not isinstance(quote, str) or not quote.strip():
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_NO_QUOTE)
        if quote_within_range is None:
            # 没有校验器就无法确认引文真在原文里；此时放行等于把校验让给模型自评
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_NO_VERIFIER)
        if not quote_within_range(quote, int(start), int(end)):
            return CandidateVerdict(candidate_id, INVALID, reason=REASON_QUOTE_OUT_OF_RANGE)

        return CandidateVerdict(
            candidate_id, raw_verdict, answer=answer,
            evidence_id=evidence_id,
            supporting_span=(int(start), int(end)),
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
        if verdict.supporting_span is not None:
            located += 1

    if expected is None:
        for verdict in rows:
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
