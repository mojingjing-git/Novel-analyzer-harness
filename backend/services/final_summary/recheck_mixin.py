"""Stage 2 伏笔复检编排与明细落盘 Mixin（Layer 2）。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, List, Optional

from backend.utils.json_utils import safe_save_json

from .parsers import (
    _recheck_progress_message,
    recheck_channel,
    recheck_conflict_reason,
    recheck_overrides,
)

logger = logging.getLogger(__name__)


class RecheckMixin:
    """封装带证据伏笔复检（V3 / V2）的原文装载、通道制备、运行与归档。"""

    def _collect_chapters(self) -> dict:
        """整本按物理单元（文件）切好的正文：{单元键: 正文}。"""
        from backend.core.text_units import load_blocks_as_files

        blocks_dir = self._resolve_blocks_dir()
        if blocks_dir is None or not blocks_dir.is_dir():
            return {}
        units, excluded = load_blocks_as_files(blocks_dir)
        if excluded:
            logger.warning(f"复检：已排除 {len(excluded)} 个非正文文件：{excluded[:5]}")
        return units

    def _prepare_recheck_context(self, *, contract_version: str) -> tuple[Any, dict, dict, dict, int, int] | None:
        """解析复检独立通道、准备停止响应客户端、模型快照与请求日志。通道冲突返回 None。"""
        overrides = recheck_overrides(self.config)
        conflict = recheck_conflict_reason(self.config)
        if conflict:
            logger.error(conflict)
            self.last_recheck_summary = {
                "status": "PARTIAL",
                "status_reason": conflict,
                "calls": 0,
            }
            return None

        default_calls = 1200 if contract_version == "recheck_v3" else 300
        max_calls = max(1, int(getattr(self.config.analysis, "recheck_max_calls", default_calls) or default_calls))
        max_tokens = int(getattr(self.config.analysis, "recheck_max_tokens", 32768) or 32768)

        from backend.core.llm_client import LLMClient

        effective_model, effective_base = recheck_channel(self.config)
        base_kw = {"json_mode": "default", **overrides}
        recheck_client = LLMClient(replace(self.config.api, **base_kw))
        if overrides:
            logger.info(
                "复检使用独立通道：%s @ %s（思考档位 %s，超时 %s）",
                overrides.get("model", f"(跟随主模型 {effective_model})"),
                overrides.get("base_url", "(跟随主通道)"),
                overrides.get("reasoning_effort", "(跟随主配置)"),
                overrides.get("timeout", "(跟随主配置)"),
            )
        else:
            logger.info(
                "复检跟随主通道：%s @ %s",
                effective_model,
                effective_base,
            )
        self._recheck_client = recheck_client
        if self._stop_requested:
            stop_rc = getattr(recheck_client, "request_stop", None)
            if callable(stop_rc):
                stop_rc()
            self._recheck_client = None
            return None

        self._recheck_model_name = (
            getattr(getattr(recheck_client, "config", None), "model", "")
            or effective_model
            or ""
        )
        eff_model, eff_base = self._recheck_channel()
        audit_m_cfg = {
            "model": eff_model,
            "base_url": eff_base,
            "temperature": float(getattr(getattr(recheck_client, "config", None), "temperature", 0) or 0),
            "reasoning_effort": str(getattr(getattr(recheck_client, "config", None), "reasoning_effort", "") or ""),
            "max_tokens": max_tokens,
        }
        request_log: dict = {}
        try:
            from backend.common.recheck_request_log import (
                compute_recheck_code_snapshot,
                make_run_identity,
            )

            request_log = {
                "schema": "recheck_request_log_v1",
                "run": make_run_identity(
                    work_id=str(self.output_dir),
                    output_dir=str(self.output_dir),
                    code_snapshot_id=compute_recheck_code_snapshot(contract_version),
                    model_config=audit_m_cfg,
                    contract_version=contract_version,
                ),
                "requests": [],
                "usage_by_prompt_sha256": {},
            }
        except Exception as exc:
            logger.warning(f"准备复检请求日志失败，本轮不记录：{exc}")
            request_log = {}

        return recheck_client, overrides, audit_m_cfg, request_log, max_calls, max_tokens

    def _make_recheck_caller(self, recheck_client: Any, *, max_tokens: int, request_log: dict) -> Callable[[str], Any]:
        import inspect

        supports_on_progress = False
        try:
            sig = inspect.signature(recheck_client.chat_auto)
            supports_on_progress = "on_progress" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
        except (TypeError, ValueError):
            supports_on_progress = False
        seq_counter = 0

        async def _call_model(prompt: str) -> str:
            nonlocal seq_counter
            seq_counter += 1
            call_seq = seq_counter
            lane_id = 910000 + call_seq
            label = f"伏笔复检 #{call_seq}"
            lane_start = getattr(self, "_lane_start", None)
            lane_done = getattr(self, "_lane_done", None)
            make_progress = getattr(self, "_make_on_progress", None)
            if callable(lane_start):
                lane_start(lane_id, label, "recheck")
            ok = False
            try:
                if supports_on_progress and callable(make_progress):
                    res = await recheck_client.chat_auto(
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=max_tokens,
                        on_progress=make_progress("summary_phase_2", call_seq),
                    )
                else:
                    res = await recheck_client.chat_auto(
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=max_tokens,
                    )
                self._record_tokens("recheck", getattr(res, "tokens", None))
                if request_log:
                    from backend.common.recheck_request_log import sha256_text

                    usage = getattr(res, "tokens", None)
                    if usage is not None:
                        request_log.setdefault("usage_by_prompt_sha256", {})[
                            sha256_text(prompt)
                        ] = usage
                if not getattr(res, "success", True):
                    raise RuntimeError(
                        f"复检调用失败：{getattr(res, 'error', '') or '未知错误'}"
                    )
                ok = True
                return getattr(res, "content", "") or ""
            finally:
                if callable(lane_done):
                    lane_done(lane_id, label, ok, "recheck")

        return _call_model

    async def _finalize_recheck_outcome(self, outcome, *, engine_label: str, request_log: dict) -> None:
        if request_log and request_log.get("requests"):
            try:
                from backend.common.recheck_request_log import save_request_log

                log_payload = {
                    "schema": request_log.get("schema"),
                    "run": request_log.get("run"),
                    "requests": request_log.get("requests"),
                }
                save_request_log(self.output_dir / "recheck_request_log.json", log_payload)
            except Exception as exc:
                logger.warning(f"保存 recheck_request_log.json 失败：{exc}")
        self.ledger.save(self.ledger_path)
        self.last_recheck_summary = outcome.summary()
        skip_empty_stop_persist = (
            self._stop_requested
            and int(outcome.coverage.get("checked", 0) or 0) == 0
            and (self.output_dir / "recheck_verdicts.json").exists()
        )
        detail_path = (
            None
            if skip_empty_stop_persist
            else await asyncio.to_thread(self._persist_recheck_detail, outcome)
        )
        if detail_path is not None:
            self.last_recheck_summary["verdicts_file"] = str(detail_path)
        logger.info(
            f"复检 {engine_label} 完成：{outcome.status} 调用 {outcome.calls} 次，"
            f"覆盖 {outcome.coverage.get('checked', 0)}/{outcome.coverage.get('candidates_total', 0)}，"
            f"逐条明细 {detail_path.name if detail_path else '未落盘'}"
        )
        self._emit_progress({
            "type": "recheck_done",
            "recheck": dict(self.last_recheck_summary),
            "message": f"伏笔复检 {engine_label} 完成（{outcome.status}，调用 {outcome.calls} 次）",
        })

    def _emit_recheck_progress(self, summary: dict, *, engine: str) -> None:
        """统一归一化 V3 / V2 复检实时子进度，推送结构化计数供前端进度条与步骤卡展示。"""
        stage = str(summary.get("stage") or "recheck")
        hop = int(summary.get("hop") or 0)
        cov = summary.get("coverage") if isinstance(summary.get("coverage"), dict) else {}
        items_total = int(
            summary.get("items_total")
            or summary.get("total")
            or cov.get("candidates_total")
            or 0
        )
        raw_stage_total = summary.get("stage_total")
        stage_total = int(
            raw_stage_total
            if raw_stage_total is not None
            else (summary.get("total") or items_total)
        )
        raw_stage_done = summary.get("stage_done")
        if raw_stage_done is not None:
            stage_done = int(raw_stage_done)
        elif stage == "build_candidates":
            stage_done = int(summary.get("built") or 0)
        else:
            stage_done = int(summary.get("checked") or 0)
        raw_remaining = summary.get("remaining")
        remaining = int(
            raw_remaining
            if raw_remaining is not None
            else max(0, stage_total - stage_done)
        )
        checked = int(
            summary.get("checked")
            if summary.get("checked") is not None
            else (cov.get("checked") or 0)
        )
        calls = int(summary.get("calls") or 0)
        unit = str(summary.get("unit") or ("批" if stage in ("u1_alias", "u1_route") else "条"))
        rp = {
            "engine": engine,
            "stage": stage,
            "hop": hop,
            "stage_done": stage_done,
            "stage_total": stage_total,
            "remaining": max(0, remaining),
            "items_total": items_total,
            "checked": checked,
            "calls": calls,
            "unit": unit,
        }
        msg = "" if summary.get("silent") else _recheck_progress_message(summary)
        self._emit_progress({
            "type": "recheck_progress",
            "recheck_progress": rp,
            "message": msg,
        })

    async def _run_recheck_v3(
        self,
        results: List[dict],
        volume_summaries: Optional[List[str]] = None,
    ) -> Optional[Any]:
        """阶段 2（V3 默认引擎）：双向路由 + 剧情簇 NMS + 多跳侦探 + 四防对称外科终审。"""
        from backend.core.model_catalog import lookup_model_spec
        from backend.core.text_units import build_display_labels
        from backend.services.recheck_v3 import V3RecheckOptions, run_recheck_v3

        chapters = self._collect_chapters()
        if not chapters:
            self.last_recheck_summary = {
                "status": "NOT_RUN",
                "status_reason": "取不到原文，未执行复检",
                "calls": 0,
            }
            logger.warning("复检 V3：未执行（没有原文）")
            return None

        prep = self._prepare_recheck_context(contract_version="recheck_v3")
        if prep is None:
            return None
        recheck_client, overrides, audit_m_cfg, request_log, max_calls, max_tokens = prep

        recheck_config = int(getattr(self.config.analysis, "recheck_concurrency", 0) or 0)
        base_conc = int(getattr(self.config.analysis, "concurrency", 0) or 0) or self.concurrency or 1
        v3_concurrency = max(1, recheck_config if recheck_config > 0 else base_conc)

        analysis_cfg = getattr(self.config, "analysis", None)

        def _cfg_int(name: str, default: int) -> int:
            val = getattr(analysis_cfg, name, None)
            return default if val is None else int(val)

        v3_mode = str(getattr(analysis_cfg, "recheck_v3_mode", "auto") or "auto").strip().lower()
        if v3_mode == "wide":
            compact_mode = False
        elif v3_mode == "compact":
            compact_mode = True
        else:
            compact_mode = max_tokens < 16384
            if not compact_mode:
                try:
                    spec = lookup_model_spec(self._recheck_model_name or "")
                    ctx_lim = int((spec or {}).get("limit", {}).get("context", 0) or 0)
                    if 0 < ctx_lim <= 36000:
                        compact_mode = True
                except Exception:
                    pass

        v3_options = V3RecheckOptions(
            batch_size=_cfg_int("recheck_v3_batch_size", 0),
            pool_max_chars=_cfg_int("recheck_v3_pool_max_chars", 0),
            prompt_hard_cap_chars=_cfg_int("recheck_v3_prompt_hard_cap_chars", 0),
            u1_superchunk_chars=_cfg_int("recheck_v3_u1_superchunk_chars", 0),
            u1_split_batch_size=_cfg_int("recheck_v3_u1_split_batch_size", 0),
            u1_route_q_batch=_cfg_int("recheck_v3_u1_route_q_batch", 0),
            chapter_chars=_cfg_int("recheck_v3_chapter_chars", 0),
            max_post_windows=_cfg_int("recheck_v3_max_post_windows", 0),
            max_pre_windows=_cfg_int("recheck_v3_max_pre_windows", 1),
            hop2_new_cap=_cfg_int("recheck_v3_hop2_new_cap", 0),
            hop3_new_cap=_cfg_int("recheck_v3_hop3_new_cap", 0),
            max_hops=_cfg_int("recheck_v3_max_hops", 3),
            max_audit_windows=_cfg_int("recheck_v3_max_audit_windows", 0),
            pre_lookback_units=_cfg_int("recheck_v3_pre_lookback_units", 45),
            nms_cluster_radius=_cfg_int("recheck_v3_nms_cluster_radius", 3),
            nms_max_per_cluster=_cfg_int("recheck_v3_nms_max_per_cluster", 2),
        )

        _call_model = self._make_recheck_caller(recheck_client, max_tokens=max_tokens, request_log=request_log)
        prev_conc = getattr(self, "_active_concurrency", getattr(self, "concurrency", 1))
        self._active_concurrency = v3_concurrency
        self._emit_progress({
            "type": "phase", "phase": "recheck", "phase_label": "带证据伏笔复检 (V3)",
            "concurrency": v3_concurrency, "real_lanes": True,
            "message": "[阶段2/4] 带证据伏笔复检 V3...",
        })
        self._emit_progress({"type": "status", "message": "正在构建全书伏笔路由与核查..."})

        try:
            from backend.services.book_artifacts import infer_result_block_size
            outcome = await run_recheck_v3(
                self.ledger.items,
                chapters=chapters,
                call_model=_call_model,
                output_dir=self.output_dir,
                volume_summaries=volume_summaries,
                max_calls=max_calls,
                concurrency=v3_concurrency,
                unit_noun="文件",
                display_labels=build_display_labels(chapters),
                block_size=infer_result_block_size(self.output_dir, default=1),
                compact_mode=compact_mode,
                enable_gray_u4=bool(getattr(analysis_cfg, "recheck_v3_gray_u4", False)),
                mid_single_hop=bool(getattr(analysis_cfg, "recheck_v3_mid_single_hop", False)),
                options=v3_options,
                request_log=request_log or None,
                should_stop=lambda: self._stop_requested,
                emit=lambda summary: self._emit_recheck_progress(summary, engine="v3"),
            )
        finally:
            rc = getattr(self, "_recheck_client", None)
            if rc is not None and getattr(rc, "breaker_tripped", False) is True:
                self._recheck_breaker_tripped = True
                raw_reason = getattr(rc, "breaker_reason", "")
                if isinstance(raw_reason, str) and raw_reason.strip():
                    self._recheck_breaker_reason = raw_reason.strip()
            self._active_concurrency = prev_conc
            self._recheck_client = None

        await self._finalize_recheck_outcome(outcome, engine_label="V3", request_log=request_log)
        return outcome

    async def _run_recheck_v2(self, results: List[dict]) -> Optional[Any]:
        """阶段 2（V2 兼容引擎）：单跳严格带证据复检。"""
        from backend.core.text_units import build_display_labels
        from backend.services.recheck_v2 import run_recheck_v2

        chapters = self._collect_chapters()
        if not chapters:
            self.last_recheck_summary = {
                "status": "NOT_RUN",
                "status_reason": "取不到原文，未执行复检",
                "calls": 0,
            }
            logger.warning("复检 V2：未执行（没有原文）")
            return None

        prep = self._prepare_recheck_context(contract_version="recheck_v2")
        if prep is None:
            return None
        recheck_client, _overrides, audit_m_cfg, request_log, max_calls, max_tokens = prep

        batch_size = max(1, int(getattr(self.config.analysis, "recheck_v2_batch_size", 6) or 6))
        recheck_config = int(getattr(self.config.analysis, "recheck_concurrency", 0) or 0)
        base_conc = int(getattr(self.config.analysis, "concurrency", 0) or 0) or self.concurrency or 1
        v2_conc = max(1, recheck_config if recheck_config > 0 else base_conc)
        _call_model = self._make_recheck_caller(recheck_client, max_tokens=max_tokens, request_log=request_log)
        prev_conc = getattr(self, "_active_concurrency", getattr(self, "concurrency", 1))
        self._active_concurrency = v2_conc
        self._emit_progress({
            "type": "phase", "phase": "recheck", "phase_label": "带证据伏笔复检",
            "concurrency": v2_conc, "real_lanes": True,
            "message": "[阶段2/4] 带证据伏笔复检...",
        })
        self._emit_progress({"type": "status", "message": "正在核对伏笔证据..."})

        try:
            from backend.services.book_artifacts import infer_result_block_size
            v2_analysis = getattr(self.config, "analysis", None)
            v2_wr = getattr(v2_analysis, "recheck_v2_window_radius", None)
            v2_mw = getattr(v2_analysis, "recheck_v2_max_windows", None)
            v2_rr = getattr(v2_analysis, "recheck_v2_retry_rounds", None)
            outcome = await run_recheck_v2(
                self.ledger.items,
                chapters=chapters,
                call_model=_call_model,
                max_calls=max_calls,
                batch_size=batch_size,
                unit_noun="文件",
                display_labels=build_display_labels(chapters),
                block_size=infer_result_block_size(self.output_dir, default=1),
                window_radius=400 if v2_wr is None else int(v2_wr),
                max_windows=3 if v2_mw is None else int(v2_mw),
                retry_rounds=2 if v2_rr is None else int(v2_rr),
                request_log=request_log or None,
                should_stop=lambda: self._stop_requested,
                concurrency=v2_conc,
                emit=lambda summary: self._emit_recheck_progress(summary, engine="v2"),
            )
        finally:
            rc = getattr(self, "_recheck_client", None)
            if rc is not None and getattr(rc, "breaker_tripped", False) is True:
                self._recheck_breaker_tripped = True
                raw_reason = getattr(rc, "breaker_reason", "")
                if isinstance(raw_reason, str) and raw_reason.strip():
                    self._recheck_breaker_reason = raw_reason.strip()
            self._active_concurrency = prev_conc
            self._recheck_client = None
        await self._finalize_recheck_outcome(outcome, engine_label="V2", request_log=request_log)
        return outcome

    def _recheck_channel(self) -> tuple[str, str]:
        """复检实际使用的 ``(model, base_url)``（见模块级 ``recheck_channel``）。"""
        return recheck_channel(self.config)

    def _persist_recheck_detail(self, outcome) -> Path | None:
        """把逐条判定写进 output 目录，成功返回路径，失败返回 ``None``。"""
        from backend.services.recheck_v2 import (
            RECHECK_DETAIL_FILENAME,
            dump_recheck_detail,
        )

        detail_model, detail_base_url = self._recheck_channel()
        path = self.output_dir / RECHECK_DETAIL_FILENAME
        payload = dump_recheck_detail(
            outcome,
            model=detail_model,
            base_url=detail_base_url,
        )
        try:
            saved = safe_save_json(payload, path, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f"复检明细落盘失败：{exc}")
            return None
        if not saved:
            logger.warning(f"复检明细落盘失败（safe_save_json 返回 False）：{path}")
            return None
        return path
