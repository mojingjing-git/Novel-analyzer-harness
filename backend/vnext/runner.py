"""vnext 复检执行入口：把规划、预算、裁决、账本、落盘串成一条真实链路。

为什么要它（复审 2026-10-30 第 1 条）：``tasks``/``budget``/``recheck_plan``/``resolution``
单独都测过，但**没有任何生产入口消费它们**，于是"27/27 通过"只能证明零件能拼在一起，
不能证明"跑一次 → 落盘 → 重启 → 复用"这条链成立。本模块就是那条链。

三条纪律：

1. **调用前预留、调用后结算**：规划阶段拿到的预留必须由这里 settle（或确认未发出时
   release），否则额度永远挂在途，后续轮次全被挡。
2. **账本先落盘再前进**：每完成一个单元就持久化一次。中途崩溃时"已经花了钱的那几次"
   必须留在账上，否则重启后预算会被重复花掉。
3. **模型只提供素材，不提供结论**：交给 ``call_model`` 的是单元；返回的 payload 一律
   过 :func:`resolution.parse_recheck` 的门禁。跳过门禁的"支持"等于没有校验。

本模块不碰网络、不读小说正文：取材由调用方注入（``chapter_text`` / ``verify``），
这样离线也能完整跑一遍链路。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from . import artifacts, project, rolling
from .analyzer import PROMPT_STRATEGY_VERSION, analyze_block
from .blocks import BlockPlan, plan_blocks
from .constants import VnextFormatError
from .budget import Budget, Usage
from .knowledge import (
    KnowledgeStore,
    load_store,
    records_from_extracted,
    save_store,
    store_fingerprint,
)
from .prompt_limits import PromptLimits
from .prompt_view import build_prompt_window
from .results import (
    ResultRecord,
    ResultStrategy,
    classify,
    list_results,
    make_result_id,
    publish_result,
)
from .recheck_plan import (
    RecheckPlan,
    RecheckSummary,
    RecheckUnit,
    plan_recheck,
    record_plan_outcome,
    summarize_from_ledger,
)
from .resolution import (
    CONTRADICTED,
    DIFFERENT_ANSWER,
    INSUFFICIENT,
    SUPPORTED,
    CandidateVerdict,
    count_verdicts,
    parse_recheck,
)
from .tasks import (
    CANCELLED,
    FAILED,
    STALE_INPUT,
    SUCCEEDED,
    TaskLedger,
    TaskRecord,
)

LEDGER_FILE_NAME = "recheck_ledger.json"

#: 裁决结论 → 任务状态。SUPPORTED/不足/冲突都是**终态**（可复用），
#: 只有"响应不可用"才落到 FAILED 等可重跑的状态。
_STATUS_BY_VERDICT = {
    SUPPORTED: SUCCEEDED,
    INSUFFICIENT: "INSUFFICIENT",
    DIFFERENT_ANSWER: "CONFLICT",
    CONTRADICTED: "CONFLICT",
}


@dataclass
class RunOutcome:
    """一次复检运行的账。字段够回答"跑了吗 / 花了多少 / 为什么没跑完"。"""

    executed: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    no_candidate: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    verdicts: dict[str, CandidateVerdict] = field(default_factory=dict)
    usage: Usage = Usage()
    stopped_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "executed": list(self.executed),
            "reused": list(self.reused),
            "no_candidate": list(self.no_candidate),
            "skipped": list(self.skipped),
            "failed": list(self.failed),
            "calls": self.usage.calls,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "stopped_reason": self.stopped_reason,
        }


def ledger_path(book_dir: Path) -> Path:
    return Path(book_dir) / LEDGER_FILE_NAME


def load_ledger(path: Path) -> TaskLedger:
    """读账本。文件不存在 = 空账本（首次运行）；格式不对 = 报错，不当空处理。"""
    path = Path(path)
    if not path.is_file():
        return TaskLedger()
    payload = artifacts.read_json_artifact(path)
    rows = payload.get("records")
    if not isinstance(rows, list):
        raise ValueError(f"账本缺少 records 列表：{path}")
    return TaskLedger.from_dicts(rows)


def save_ledger(path: Path, ledger: TaskLedger) -> None:
    artifacts.write_json_atomic(
        path, artifacts.stamp_payload({"records": ledger.to_dicts()})
    )


def run_recheck(
    units: Sequence[RecheckUnit],
    *,
    ledger: TaskLedger,
    call_model: Callable[[RecheckUnit], Mapping[str, object]],
    budget: Budget | None = None,
    strategy_versions: Mapping[str, int] | None = None,
    verify: Callable[[str, int, int], bool] | None = None,
    locate: Callable[[str, str], object] | None = None,
    evidence_text: Callable[[RecheckUnit, str], str] | None = None,
    allowed_evidence_ids: Callable[[RecheckUnit], Iterable[str]] | None = None,
    persist: Callable[[TaskLedger], None] | None = None,
    kind: str = "recheck",
    stage: str = "resolve",
    tokens_per_call: int = 0,
) -> RunOutcome:
    """跑一轮复检。

    ``call_model`` 返回的是**原始 payload**（模拟模型输出），一切判定都走门禁；
    ``persist`` 在每个单元结束后被调用一次，用于把账本落盘。
    """
    outcome = RunOutcome()
    plan = plan_recheck(
        units,
        ledger=ledger,
        budget=budget,
        stage=stage,
        strategy_versions=strategy_versions,
        tokens_per_call=tokens_per_call,
    )

    outcome.reused = [u.entity_id for u in plan.reuse]
    outcome.no_candidate = list(plan.no_candidate)
    outcome.skipped = [u.entity_id for u in plan.budget_skipped]
    record_plan_outcome(plan, ledger=ledger, kind=kind, stage=stage)
    _flush(persist, ledger)

    for unit in plan.pending:
        key = plan.task_keys[unit.entity_id]
        reservation = plan.reservations.get(unit.entity_id)
        try:
            payload = call_model(unit)
        except Exception as exc:  # noqa: BLE001 - 模型/网络失败都按"这次失败"记账
            if reservation is not None and budget is not None:
                spent = budget.settle(reservation, actual=Usage(calls=1))
                outcome.usage = outcome.usage.plus(spent)
            ledger.record(
                TaskRecord(task_key=key, kind=kind, stage=stage, status=FAILED,
                           group="recheck", calls=1, reason=f"{type(exc).__name__}: {exc}")
            )
            outcome.failed.append(unit.entity_id)
            _flush(persist, ledger)
            continue

        evidence_ids = set(allowed_evidence_ids(unit)) if allowed_evidence_ids else set()
        verdict = parse_recheck(
            payload,
            allowed_candidate_ids=set(unit.candidate_ids),
            allowed_evidence_ids=evidence_ids,
            quote_within_range=verify,
                    locate=locate,
                    evidence_text=(
                        (lambda eid, _u=unit: evidence_text(_u, eid)) if evidence_text else None
                    ),
        )
        outcome.verdicts[unit.entity_id] = verdict
        status = _STATUS_BY_VERDICT.get(verdict.verdict, FAILED)
        if reservation is not None and budget is not None:
            spent = budget.settle(
                reservation,
                actual=Usage(calls=1, prompt_tokens=tokens_per_call),
            )
            outcome.usage = outcome.usage.plus(spent)
        ledger.record(
            TaskRecord(
                task_key=key, kind=kind, stage=stage, status=status, group="recheck",
                calls=1, prompt_tokens=tokens_per_call,
                apply_key=_apply_key(unit, verdict) if status != FAILED else None,
                result_ref=verdict.evidence_id or verdict.candidate_id,
                reason=verdict.reason,
            )
        )
        if status == FAILED:
            outcome.failed.append(unit.entity_id)
        else:
            outcome.executed.append(unit.entity_id)
        _flush(persist, ledger)

    if outcome.skipped:
        outcome.stopped_reason = f"{len(outcome.skipped)} 个单元因预算未执行"
    elif outcome.failed:
        outcome.stopped_reason = f"{len(outcome.failed)} 个单元失败"
    return outcome


def _apply_key(unit: RecheckUnit, verdict: CandidateVerdict) -> str:
    """应用键：同一实体 + 同一候选 + 同一结论只应用一次。

    用结论而不是时间戳：重跑得到同一结论时不该重复应用（历史会被追加两次），
    得到不同结论时才是一个新的应用。
    """
    # 位置**不进键**：它是派生物，位置从错到正不该重复应用同一结论。
    # 引文哈希要进键：换了证据/换了原话就是另一条论据，必须重新记账。
    return (
        f"{unit.entity_id}|{verdict.candidate_id}|{verdict.verdict}"
        f"|{verdict.evidence_id or '-'}|{verdict.quote_hash or '-'}"
    )


def _flush(persist: Callable[[TaskLedger], None] | None, ledger: TaskLedger) -> None:
    if persist is not None:
        persist(ledger)


def summarize_run(outcome: RunOutcome, *, candidates_total: int = 0) -> RecheckSummary:
    summary = RecheckSummary(
        candidates_total=candidates_total,
        new_calls=len(outcome.executed),
        cache_reused=len(outcome.reused),
        no_candidate=len(outcome.no_candidate),
        failed=len(outcome.failed),
        budget_skipped=len(outcome.skipped),
        prompt_tokens=outcome.usage.prompt_tokens,
        completion_tokens=outcome.usage.completion_tokens,
    )
    return summary


def mark_stale_by_source(ledger: TaskLedger, *, source_changed: Iterable[str]) -> int:
    """来源变化时，把受影响任务标 ``STALE_INPUT``（未完成的才动）。"""
    changed = 0
    for key in set(source_changed):
        changed += ledger.mark_stale(key)
    return changed


def coverage_counts(
    outcome: RunOutcome,
    *,
    expected_candidate_ids: Sequence[str],
) -> "count_verdicts_result":
    verdicts = [v for v in outcome.verdicts.values()]
    return count_verdicts(verdicts, expected_candidate_ids=list(expected_candidate_ids))

# ---------------------------------------------------------------------------
# B1b：顺序跑整本
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisOutcome:
    """一次整本分析的结果摘要。"""

    committed_blocks: int
    coverage: "rolling.Coverage"
    stop_reason: str
    prompt_tokens: int
    completion_tokens: int
    unknown_usage_blocks: int
    store_fingerprint: str
    #: 因永久错误被跳过（留 gap）的块数
    skipped_blocks: int = 0

    def to_dict(self) -> dict:
        return {
            "committed_blocks": self.committed_blocks,
            "commit_cursor": self.coverage.commit_cursor,
            "gaps": list(self.coverage.gaps),
            "partial": self.coverage.partial,
            "stop_reason": self.stop_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "unknown_usage_blocks": self.unknown_usage_blocks,
            "skipped_blocks": self.skipped_blocks,
            "store_fingerprint": self.store_fingerprint,
        }


def knowledge_path(book_dir: Path) -> Path:
    return project.derived_dir(book_dir) / "knowledge.json"


def load_or_rebuild_store(book_dir: Path, *, plans: Sequence["BlockPlan"] | None = None) -> KnowledgeStore:
    """读库；读不出来就按块序**重放**结果重建（纯本地，零模型调用）。

    重放是安全的：``extracted`` 里带着来源，重放的库与一次跑完的库指纹相同
    （见 ``test_vnext_knowledge_persist.py``）。读不出来时不能"当空库继续跑"——
    那会让续跑得到与一次跑完不同的结论，且没人看得出来。
    """
    path = knowledge_path(book_dir)
    if path.is_file():
        try:
            store, _meta = load_store(path)
            return store
        except VnextFormatError:
            pass

    store = KnowledgeStore()
    records = list_results(book_dir)
    signature_to_plan = {p.block_signature: p for p in (plans or [])}
    for result in sorted(records.values(), key=lambda r: r.created_at):
        facts, chars = records_from_extracted(result.extracted)
        store.add_block(
            block_signature=result.block_signature,
            facts=facts,
            character_records=chars,
        )
        signature_to_plan.pop(result.block_signature, None)
    return store




def _expected_context_fingerprints(
    manifest, plans: Sequence[BlockPlan], records: Mapping[str, object], *, limits: PromptLimits
) -> dict[str, str]:
    """按块序重放，算出"每块分析时应当看到的窗口指纹"。

    这是依赖链能判"续跑降质"的前提：期望值必须由**当前结果重放出的库**计算，
    而不是拿库里现有的东西反推——否则库被改坏了，期望值也跟着坏。
    """
    by_sig: dict[str, object] = {}
    for record in records.values():
        if getattr(record, "status", None) == "SUCCEEDED":
            by_sig[getattr(record, "block_signature", "")] = record

    ordinal_of = {c.chapter_id: c.ordinal for c in manifest.chapters}
    store = KnowledgeStore()
    fingerprints: dict[str, str] = {}
    for plan in plans:
        first = min(ordinal_of[cid] for cid in plan.chapter_ids if cid in ordinal_of)
        fingerprints[plan.block_signature] = build_prompt_window(
            store, exclusive_before=first, limits=limits
        ).fingerprint()
        record = by_sig.get(plan.block_signature)
        if record is not None:
            facts, chars = records_from_extracted(getattr(record, "extracted", {}))
            store.add_block(
                block_signature=plan.block_signature,
                facts=facts,
                character_records=chars,
            )
    return fingerprints


def _is_permanent(exc: BaseException) -> bool:
    """这个错误重跑会不会变好？只看结构化判定，不猜字符串。"""
    if getattr(exc, "permanent", False):
        return True
    verdict = getattr(exc, "verdict", None)
    is_permanent = getattr(verdict, "is_permanent", None)
    if callable(is_permanent):
        try:
            return bool(is_permanent())
        except TypeError:  # 属性不是方法
            return bool(is_permanent)
    # 内容审核拦截：厂商把它塞在 422 的 body 里（MiniMax: `input new_sensitive (1026)`），
    # 走 error_taxonomy 的码表未必能认出来，但 core 里已有专门嗅探器，直接复用。
    try:
        from backend.core import llm_client

        if llm_client.moderation_hit_from_exception(exc):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.core import error_taxonomy

        return bool(error_taxonomy.classify_exception(exc).is_permanent())
    except Exception:  # noqa: BLE001 - 判定不了就当非永久（保守：不跳）
        return False


async def run_analysis(
    book_dir: Path,
    *,
    client: object,
    limits: "PromptLimits",
    strategy: "ResultStrategy | None" = None,
    block_size: int = 4,
    max_blocks: int | None = None,
    title: str | None = None,
    plans: Sequence["BlockPlan"] | None = None,
    skip_permanent: bool = False,
) -> AnalysisOutcome:
    """按块序跑整本。顺序是硬的：每块的上下文依赖"它之前的块"，跳着跑就换了一套输入。"""
    book_dir = Path(book_dir)
    manifest = await asyncio.to_thread(project.load_manifest, book_dir)
    plans = list(plans) if plans is not None else plan_blocks(manifest, block_size=block_size)
    strategy = strategy or ResultStrategy(model_fingerprint="")

    store = await asyncio.to_thread(load_or_rebuild_store, book_dir, plans=plans)
    records = await asyncio.to_thread(list_results, book_dir)
    context_fingerprints = _expected_context_fingerprints(
        manifest, plans, records, limits=limits
    )
    verdict = classify(
        manifest, plans, records, strategy=strategy,
        context_fingerprints=context_fingerprints,
    )

    prior: list[str | None] = []
    committed = 0
    analysed = 0
    stop_reason = "completed"
    prompt_tokens = 0
    completion_tokens = 0
    unknown_usage = 0
    skipped: list[str] = []

    for index, plan in enumerate(plans):
        done = verdict.done.get(plan.block_signature)
        if done is not None:
            # 跳过判据必须是 classify.done（验过依赖），不是 is_block_fresh（只验源）
            prior.append(done.result_id)
            committed += 1
            continue
        if max_blocks is not None and analysed >= max_blocks:
            stop_reason = "block_limit"
            break
        try:
            run = await analyze_block(
                book_dir,
                plan,
                client=client,
                limits=limits,
                strategy=strategy,
                prior_result_ids=list(prior),
                store=store,
                title=title or manifest.title,
            )
        except Exception as exc:  # noqa: BLE001 - 记 FAILED 再停，不吞掉类型
            if skip_permanent and _is_permanent(exc):
                # 永久错误（审核拦截/鉴权/欠费）不是"这次没跑好"：重跑一万次也一样。
                # 记成 SKIPPED 并**留 gap**，让覆盖率如实说"这一段没分析过"，
                # 而不是让整本书卡在一个块上（默认仍然停，跳过要显式开）。
                await asyncio.to_thread(
                    _record_failure, book_dir, plan, strategy, str(exc), "SKIPPED"
                )
                skipped.append(plan.block_signature)
                prior.append(None)
                continue
            await asyncio.to_thread(
                _record_failure, book_dir, plan, strategy, str(exc)
            )
            stop_reason = f"failed:block{index}"
            break

        analysed += 1
        committed += 1
        if run.record.usage_status == "KNOWN":
            prompt_tokens += run.record.prompt_tokens or 0
            completion_tokens += run.record.completion_tokens or 0
        else:
            unknown_usage += 1

        # 库的读改写与 publish 同一把书锁：两路并发 publish 会丢事实，
        # 而"丢事实"与"没分析过"在界面上长得一样。
        with project._lock_for(book_dir):  # noqa: SLF001 - 同包内复用书锁
            facts, chars = records_from_extracted(run.record.extracted)
            store.add_block(
                block_signature=plan.block_signature,
                facts=facts,
                character_records=chars,
            )
            save_store(
                knowledge_path(book_dir),
                store,
                meta={
                    "source_snapshot_id": manifest.source_snapshot_id,
                    "ordered_result_ids": [r for r in prior if r] + [run.record.result_id],
                },
            )
        prior.append(run.record.result_id)

    if skipped:
        stop_reason = ("completed_with_skips" if stop_reason == "completed"
                       else f"{stop_reason};skipped={len(skipped)}")
    results = await asyncio.to_thread(list_results, book_dir)
    slots = rolling.slots_from_results(plans, results.values())
    coverage = rolling.build_coverage(plans, batch_size=max(1, block_size), states=slots)
    return AnalysisOutcome(
        committed_blocks=committed,
        coverage=coverage,
        stop_reason=stop_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        unknown_usage_blocks=unknown_usage,
        store_fingerprint=store_fingerprint(store),
        skipped_blocks=len(skipped),
    )


def _record_failure(
    book_dir: Path,
    plan: "BlockPlan",
    strategy: "ResultStrategy",
    message: str,
    status: str = "FAILED",
) -> None:
    """把失败也写成一条结果：否则下次进来还是"没有结果"，看不出试过。"""
    record = ResultRecord(
        result_id=make_result_id(
            block_signature=plan.block_signature,
            prompt_strategy_version=PROMPT_STRATEGY_VERSION,
            model_fingerprint=strategy.model_fingerprint,
            dependency_fingerprint="",
        ),
        block_signature=plan.block_signature,
        source_chapters=(),
        status=status,
        model_fingerprint=strategy.model_fingerprint,
        prompt_strategy_version=PROMPT_STRATEGY_VERSION,
        error=message,
    )
    publish_result(book_dir, record)
