"""C 类按需审核选择器（仅依赖 Python 标准库与 backend.common 同层模块）。

用途（对应 docs/BC改进任务书.md T4）：
- 默认不开启（`enabled=False` 时一律走 `current` 快速路径，零额外调用）；
- 开启按需审核（`enabled=True`）时，仅根据**决策时已知信号**决定是否将候选升级到 `claims_v2` 声明级审计：
  1. 候选答案分句数与扩写/转折特征（`answer_units` >= 5，或 `answer_units` >= 3 且含转折/保留/并列扩写词）；
  2. 关闭条件结构（`prereg` 中的 `AND`/`UNCERTAIN`，或 `criteria` 含合取/排他要求）；
  3. 语气、归属、梦境/比方与实现进度风险词（引文、答案或局部窗口中的推测/梦境/比方/筹备信号）；
  4. 机械筛查软信号（`quote_contains_anchor`、`answer_in_known`、`evidence_not_after_anchor`、`exclusivity_unverified`）。
- 严禁读取人工/参考真值标签或事后判定结果。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Mapping

from backend.common.claim_evidence_v2 import answer_units
from backend.common.closure import validate_prereg_row


SCHEMA = "c_audit_selector_v1"

#: 候选答案中提示“部分关闭 + 顺带扩写”或“转折保留”的连接词
EXTENSION_OR_CONCESSION_RE = re.compile(
    r"(但|然而|不过|虽|尚未|未证实|未交代|同时|此外|不仅)"
)

#: 关闭条件中的合取/排他性要求（决策时已知）
CONJUNCTION_OR_EXCLUSIVITY_RE = re.compile(
    r"(以及|并且|同时满足|缺一不可|分别交代|排[除他]|只[有是]|无其他|没有其他|唯一)"
)

#: 排他性回答词汇
EXCLUSION_ANSWER_MARKERS = ("不是", "没有其他", "只有", "排除", "并非", "唯一")

#: 语气、叙事框定（梦境/比方/转述）与未兑现阶段（计划/命令/筹备）风险词
MODALITY_STAGE_RISK_RE = re.compile(
    r"(做梦|梦里|梦境|梦醒|打个比方|比方|平行世界|猜测|推测|怀疑|疑似|或许|"
    r"据说|传闻|自称|声称|下令联络|你去把|去把|命人去|准备尝试|打算|筹备|拟定)"
)


def _norm(s: str) -> str:
    return re.sub(r"\W+", "", s or "", flags=re.UNICODE)


def _ngrams(s: str, n: int = 2) -> set[str]:
    return {s[i : i + n] for i in range(len(s) - n + 1)} if len(s) >= n else ({s} if s else set())


def compute_mechanical_signals(item: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """计算决策时已知的硬机械缺陷与软机械风险信号（与 acceptance_gate 口径一致）。"""
    hard: list[str] = []
    soft: list[str] = []
    quote_n = _norm(str(item.get("supporting_quote") or item.get("quote") or ""))
    answer_n = _norm(str(item.get("answer") or item.get("candidate_answer") or ""))
    anchor_n = _norm(str(item.get("anchor_quote") or ""))
    desc_n = _norm(str(item.get("description") or item.get("clue") or ""))
    known_n = anchor_n + desc_n

    if len(quote_n) >= 6 and anchor_n:
        if quote_n in anchor_n:
            hard.append("quote_equals_anchor")
        elif anchor_n in quote_n:
            soft.append("quote_contains_anchor")
        elif known_n and quote_n in known_n:
            soft.append("quote_in_known")

    if len(answer_n) >= 10 and known_n:
        ga, gb = _ngrams(answer_n), _ngrams(known_n)
        if ga and (len(ga & gb) / len(ga)) >= 0.70:
            soft.append("answer_in_known")

    chapter = item.get("chapter") or item.get("resolved_chapter")
    anchor_ch = item.get("anchor_chapter")
    if isinstance(chapter, int) and isinstance(anchor_ch, int) and chapter > 0 and anchor_ch > 0:
        if chapter <= anchor_ch:
            soft.append("evidence_not_after_anchor")

    criteria_txt = str(item.get("answer_criteria") or item.get("criteria") or "")
    answer_txt = str(item.get("answer") or item.get("candidate_answer") or "")
    if re.search(r"排[除他]|只[有是]|无其他|没有其他", criteria_txt):
        if not any(m in answer_txt for m in EXCLUSION_ANSWER_MARKERS):
            soft.append("exclusivity_unverified")

    return hard, soft


@dataclass(frozen=True)
class AuditSelectionDecision:
    """单条候选的按需审核路由决策结果。"""

    candidate_id: str
    enabled: bool
    trigger_claims_v2: bool
    route: str  # "current_only" | "claims_v2_on_demand" | "hard_reject_mechanical"
    trigger_reasons: list[str] = field(default_factory=list)
    answer_unit_count: int = 0
    criteria_logic_type: str = "UNKNOWN"
    hard_signals: list[str] = field(default_factory=list)
    soft_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def select_audit_route(
    item: Mapping[str, Any],
    *,
    enabled: bool = False,
    max_simple_units: int = 4,
) -> AuditSelectionDecision:
    """基于决策时已知信号决定该候选走 `current_only` 还是升级触发 `claims_v2`。"""
    cid = str(item.get("candidate_id") or item.get("fs_id") or item.get("id") or "")
    answer = str(item.get("answer") or item.get("candidate_answer") or "").strip()
    quote = str(item.get("supporting_quote") or item.get("quote") or "").strip()
    criteria = str(item.get("answer_criteria") or item.get("criteria") or "").strip()
    context_window = str(item.get("context_window") or "").strip()

    units = answer_units(answer) if answer else []
    unit_count = len(units)

    prereg = item.get("prereg") if isinstance(item.get("prereg"), dict) else None
    logic_type = "UNKNOWN"
    if prereg is not None:
        ok, _ = validate_prereg_row(prereg, criteria)
        if ok and prereg.get("criteria_source", criteria) == criteria:
            logic_type = str(prereg.get("closing", {}).get("type") or "UNKNOWN")

    hard, soft = compute_mechanical_signals(item)

    if not enabled:
        return AuditSelectionDecision(
            candidate_id=cid,
            enabled=False,
            trigger_claims_v2=False,
            route="current_only",
            trigger_reasons=[],
            answer_unit_count=unit_count,
            criteria_logic_type=logic_type,
            hard_signals=hard,
            soft_signals=soft,
        )

    if hard:
        return AuditSelectionDecision(
            candidate_id=cid,
            enabled=True,
            trigger_claims_v2=False,
            route="hard_reject_mechanical",
            trigger_reasons=[f"hard:{h}" for h in hard],
            answer_unit_count=unit_count,
            criteria_logic_type=logic_type,
            hard_signals=hard,
            soft_signals=soft,
        )

    reasons: list[str] = []

    # 信号 1：多断言长分句（> max_simple_units）或 >=3 分句且含转折/未证实/并列扩写特征
    if unit_count > max_simple_units:
        reasons.append(f"multi_clause_answer:{unit_count}")
    elif unit_count >= 3 and EXTENSION_OR_CONCESSION_RE.search(answer):
        reasons.append(f"clause_with_extension_or_concession:{unit_count}")

    # 信号 2：合取/排他关闭条件
    if logic_type in ("AND", "UNCERTAIN"):
        reasons.append(f"prereg_logic:{logic_type}")
    elif CONJUNCTION_OR_EXCLUSIVITY_RE.search(criteria):
        reasons.append("criteria_conjunction_or_exclusivity")

    # 信号 3：语气/归属/梦境/比方/未兑现阶段风险词
    combined_surface = f"{answer}\n{quote}\n{context_window}"
    m_risk = MODALITY_STAGE_RISK_RE.search(combined_surface)
    if m_risk:
        reasons.append(f"modality_or_stage_cue:{m_risk.group(0)}")

    # 信号 4：机械筛查软信号
    for s in soft:
        reasons.append(f"soft:{s}")

    trigger = len(reasons) > 0
    return AuditSelectionDecision(
        candidate_id=cid,
        enabled=True,
        trigger_claims_v2=trigger,
        route="claims_v2_on_demand" if trigger else "current_only",
        trigger_reasons=reasons,
        answer_unit_count=unit_count,
        criteria_logic_type=logic_type,
        hard_signals=hard,
        soft_signals=soft,
    )
