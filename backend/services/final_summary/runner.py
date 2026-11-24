"""最终总结主执行器 FinalSummaryRunner（Layer 3）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.config.constants import STORY_ATLAS_FILE, STORY_ATLAS_SCHEMA_VERSION
from backend.config.settings import AppConfig
from backend.core.llm_client import LLMClient
from backend.utils.foreshadow_ledger import ForeshadowItem, ForeshadowLedger
from backend.utils.json_utils import safe_load_json, safe_save_json

from .atlas_extractor import (
    build_deterministic_anchors,
    compute_atlas_fingerprint,
    parse_and_sanitize_char_atlas,
    parse_and_sanitize_world_atlas,
)
from .catalog_mixin import CatalogMixin
from .checkpoint_mixin import CheckpointMixin
from .parsers import (
    build_reconciliation_retry_messages,
    build_volume_summary_retry_messages,
    detect_book_name,
    extract_analysis_text,
    extract_foreshadow_json,
    extract_key_fields,
    validate_reconciliation_json,
    validate_volume_summary,
)
from .prompts import (
    ATLAS_CHAR_SYSTEM_PROMPT,
    ATLAS_WORLD_SYSTEM_PROMPT,
    BATCH_SYSTEM_PROMPT,
    BATCH_USER_TEMPLATE,
    FINAL_SYSTEM_PROMPT,
    FINAL_USER_TEMPLATE,
    RECONCILIATION_SYSTEM_PROMPT,
    RECONCILIATION_USER_TEMPLATE,
)
from .recheck_mixin import RecheckMixin

logger = logging.getLogger(__name__)


class FinalSummaryRunner(CatalogMixin, CheckpointMixin, RecheckMixin):
    """最终总结执行器（asyncio）"""

    def __init__(
        self,
        config: AppConfig,
        output_dir: Path,
        start_chapter: int,
        end_chapter: int,
        batch_size: Optional[int] = None,
        concurrency: Optional[int] = None,
        ledger_path: Optional[Path] = None,
        on_progress: Optional[Callable[[dict], None]] = None,
        on_token_stats: Optional[Callable[[dict], None]] = None,
    ):
        self.config = config
        self.output_dir = Path(output_dir)
        self.start_chapter = start_chapter
        self.end_chapter = end_chapter
        resolved_bs = (
            batch_size
            if (batch_size is not None and batch_size > 0)
            else (getattr(self.config.analysis, "summary_batch_size", None) or 30)
        )
        self.batch_size = max(1, int(resolved_bs))
        resolved_conc = (
            concurrency
            if (concurrency is not None and concurrency > 0)
            else (getattr(self.config.analysis, "summary_concurrency", None) or self.config.analysis.concurrency)
        )
        self.concurrency = max(1, int(resolved_conc or 1))
        self._lock = asyncio.Lock()
        self._checkpoint_lock = asyncio.Lock()
        self._stop_requested = False
        self._on_progress = on_progress
        self._on_token_stats = on_token_stats

        self._llm_sem = asyncio.Semaphore(self.concurrency)
        self._inflight_count = 0
        self._peak_inflight = 0
        self._active_lanes: Dict[int, Dict[str, Any]] = {}
        self._active_concurrency: int = self.concurrency

        self._tokens_summary = (0, 0)
        self._tokens_final = (0, 0)
        self._tokens_reconciliation = (0, 0)
        self._tokens_recheck = (0, 0)
        self._tokens_style = (0, 0)
        self._tokens_atlas = (0, 0)
        self._style_task: Optional[asyncio.Task] = None
        self._atlas_tasks: List[asyncio.Task] = []

        _api_cfg = replace(
            self.config.api,
            model=self.config.api.summary_model or self.config.api.model,
            json_mode="default",
            timeout=self.config.api.summary_timeout,
            thinking_mode=self.config.api.summary_thinking_mode or self.config.api.thinking_mode,
        )
        self._llm = LLMClient(_api_cfg)
        self._summary_model_name = _api_cfg.model
        self._recheck_client: Optional[Any] = None
        self._recheck_breaker_tripped: bool = False
        self._recheck_breaker_reason: str = ""

        self.ledger_path = ledger_path or (self.output_dir / "foreshadow_ledger.json")
        self.ledger = ForeshadowLedger.load(self.ledger_path)
        self.ledger.batch_size_snapshot = self.batch_size
        self.book_name = detect_book_name(self.output_dir) or "未知小说"
        logger.info(f"检测到书名: {self.book_name}")

    def stop(self):
        self._stop_requested = True
        stop_main = getattr(getattr(self, "_llm", None), "request_stop", None)
        if callable(stop_main):
            stop_main()
        rc = getattr(self, "_recheck_client", None)
        if rc is not None and rc is not getattr(self, "_llm", None):
            stop_rc = getattr(rc, "request_stop", None)
            if callable(stop_rc):
                stop_rc()
        t = getattr(self, "_style_task", None)
        if t is not None and not t.done():
            t.cancel()
        for at in list(getattr(self, "_atlas_tasks", []) or []):
            if at is not None and not at.done():
                at.cancel()

    def _emit_progress(self, payload: dict) -> None:
        if self._on_progress:
            try:
                self._on_progress(payload)
            except Exception as e:
                logger.error(f"progress 回调异常: {e}")

    def _ensure_lane_state(self) -> tuple[Dict[int, Dict[str, Any]], int]:
        lanes = getattr(self, "_active_lanes", None)
        if lanes is None:
            lanes = {}
            self._active_lanes = lanes
        conc = int(getattr(self, "_active_concurrency", getattr(self, "concurrency", 1)) or 1)
        return lanes, max(1, conc)

    def _lane_start(self, lane_id: int, label: str, phase: str) -> None:
        lanes, conc = self._ensure_lane_state()
        ts = time.time()
        lanes[lane_id] = {"id": lane_id, "label": label, "phase": phase, "started_at": ts}
        self._emit_progress({
            "type": "lane_start",
            "lane_id": lane_id,
            "label": label,
            "phase": phase,
            "concurrency": conc,
            "ts": ts,
        })

    def _lane_done(self, lane_id: int, label: str, ok: bool, phase: str) -> None:
        lanes, conc = self._ensure_lane_state()
        lanes.pop(lane_id, None)
        self._emit_progress({
            "type": "lane_done",
            "lane_id": lane_id,
            "label": label,
            "ok": ok,
            "phase": phase,
            "concurrency": conc,
        })

    def get_inflight_lanes(self) -> List[Dict[str, Any]]:
        lanes = getattr(self, "_active_lanes", None) or {}
        return [dict(v) for v in lanes.values()]

    def _load_results(self) -> List[dict]:
        """加载并提取所有章节结果的关键字段（同步 IO，经 to_thread 调用）"""
        results = []
        for f in self.output_dir.glob("chapter_*_result.json"):
            m = re.match(r'chapter_(\d+)_result\.json', f.name)
            if not m:
                continue
            ch = int(m.group(1))
            if ch < self.start_chapter or ch > self.end_chapter:
                continue
            data = safe_load_json(f)
            if data is None:
                continue
            extracted = extract_key_fields(data)
            if extracted:
                extracted["ch"] = ch
                extracted["foreshadowing_raw"] = data.get("foreshadowing", [])
                results.append(extracted)
        results.sort(key=lambda x: x['ch'])
        return results

    def _format_batch(self, batch: List[dict]) -> str:
        return json.dumps(batch, ensure_ascii=False, indent=1)

    async def _acquire_llm_slot(self) -> None:
        """获取全局 LLM 并发槽（硬上限 = self.concurrency），同时记录 in-flight 峰值"""
        await self._llm_sem.acquire()
        self._inflight_count += 1
        if self._inflight_count > self._peak_inflight:
            self._peak_inflight = self._inflight_count

    def _release_llm_slot(self) -> None:
        """释放 LLM 并发槽"""
        self._inflight_count -= 1
        self._llm_sem.release()

    async def _call_llm_summary(self, prompt: str, batch_idx: int = 0) -> Optional[str]:
        """第一次调用：产出 Markdown 卷摘要（chat_auto + on_progress broadcast）"""
        messages = [
            {"role": "system", "content": BATCH_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        lane_id = 900000 + (batch_idx if batch_idx > 0 else 1)
        label = f"卷{batch_idx}·摘要" if batch_idx > 0 else "分卷摘要"
        await self._acquire_llm_slot()
        ok = False
        self._lane_start(lane_id, label, "batch")
        try:
            on_progress = self._make_on_progress("summary_phase_1", batch_idx)
            llm_result = await self._llm.chat_auto(
                messages, max_tokens=self.config.api.max_tokens,
                validate_response=validate_volume_summary,
                retry_messages_builder=build_volume_summary_retry_messages,
                on_progress=on_progress,
            )
            ok = bool(llm_result.success)
        finally:
            self._lane_done(lane_id, label, ok, "batch")
            self._release_llm_slot()
        self._record_tokens("summary", llm_result.tokens)
        if llm_result.success:
            return llm_result.content
        logger.error(f"卷摘要 LLM 调用失败: {llm_result.error}")
        return None

    async def _call_llm_final(self, prompt: str, batch_idx: int = 0) -> Optional[str]:
        """最终报告调用（chat_auto + on_progress broadcast）"""
        messages = [
            {"role": "system", "content": FINAL_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        lane_id = 930001
        label = "全书脉络报告"
        await self._acquire_llm_slot()
        ok = False
        self._lane_start(lane_id, label, "report")
        try:
            on_progress = self._make_on_progress("summary_phase_4", 1)
            llm_result = await self._llm.chat_auto(
                messages, max_tokens=self.config.api.max_tokens, validate_response=None,
                on_progress=on_progress,
            )
            ok = bool(llm_result.success)
        finally:
            self._lane_done(lane_id, label, ok, "report")
            self._release_llm_slot()
        self._record_tokens("final", llm_result.tokens)
        if llm_result.success:
            return llm_result.content
        logger.error(f"最终报告 LLM 调用失败: {llm_result.error}")
        return None

    async def _call_llm_reconciliation(self, prompt: str, batch_idx: int = 0) -> Optional[dict]:
        """第二次调用（chat_auto + on_progress broadcast）"""
        messages = [
            {"role": "system", "content": RECONCILIATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        lane_id = 905000 + (batch_idx if batch_idx > 0 else 1)
        label = f"卷{batch_idx}·调和" if batch_idx > 0 else "伏笔调和"
        await self._acquire_llm_slot()
        ok = False
        self._lane_start(lane_id, label, "batch")
        try:
            on_progress = self._make_on_progress("summary_phase_1_recon", batch_idx)
            llm_result = await self._llm.chat_auto(
                messages, max_tokens=self.config.api.max_tokens,
                validate_response=validate_reconciliation_json,
                retry_messages_builder=build_reconciliation_retry_messages,
                on_progress=on_progress,
            )
            ok = bool(llm_result.success)
        finally:
            self._lane_done(lane_id, label, ok, "batch")
            self._release_llm_slot()
        self._record_tokens("reconciliation", llm_result.tokens)
        if not llm_result.success:
            logger.warning(f"伏笔 reconciliation 调用失败: {llm_result.error}")
            return None
        return extract_foreshadow_json(llm_result.content)

    def _make_on_progress(self, context: str, unit_idx: int):
        """薄适配器：共用流式进度工厂，计时仍在拿到并发槽之后创建"""
        from backend.core.stream_progress import make_stream_progress_callback
        from backend.progress_hub import get_hub

        return make_stream_progress_callback(
            hub=get_hub(),
            context=context,
            session_id=getattr(self, "book_id", "") or "unknown",
            unit_idx=unit_idx,
        )

    def _record_tokens(self, category: str, tokens) -> None:
        """累积并发出分类 token 统计"""
        if not tokens or not isinstance(tokens, tuple) or len(tokens) < 2:
            return
        in_tok = int(tokens[0] or 0)
        out_tok = int(tokens[1] or 0)
        if category == "summary":
            self._tokens_summary = (self._tokens_summary[0] + in_tok,
                                    self._tokens_summary[1] + out_tok)
        elif category == "final":
            self._tokens_final = (self._tokens_final[0] + in_tok,
                                  self._tokens_final[1] + out_tok)
        elif category == "reconciliation":
            self._tokens_reconciliation = (self._tokens_reconciliation[0] + in_tok,
                                           self._tokens_reconciliation[1] + out_tok)
        elif category == "recheck":
            self._tokens_recheck = (self._tokens_recheck[0] + in_tok,
                                    self._tokens_recheck[1] + out_tok)
        elif category == "style":
            self._tokens_style = (self._tokens_style[0] + in_tok,
                                  self._tokens_style[1] + out_tok)
        elif category == "atlas":
            self._tokens_atlas = (self._tokens_atlas[0] + in_tok,
                                  self._tokens_atlas[1] + out_tok)
        if self._on_token_stats:
            try:
                self._on_token_stats({
                    "category": category,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "model": (getattr(self, "_recheck_model_name", "") if category == "recheck"
                              else getattr(self, "_summary_model_name", "")),
                })
            except Exception as e:
                logger.error(f"token_stats 回调异常: {e}")

    def get_token_stats(self) -> Dict[str, Dict[str, int]]:
        """分类 token 统计快照"""
        stats: Dict[str, Dict[str, int]] = {}
        for category, pair in [
            ("summary", self._tokens_summary),
            ("final", self._tokens_final),
            ("reconciliation", self._tokens_reconciliation),
            ("recheck", self._tokens_recheck),
            ("style", self._tokens_style),
            ("atlas", self._tokens_atlas),
        ]:
            entry: Dict[str, int] = {"input_tokens": pair[0], "output_tokens": pair[1]}
            model = (getattr(self, "_recheck_model_name", "") if category == "recheck"
                     else getattr(self, "_summary_model_name", ""))
            if model:
                entry["model"] = model
            stats[category] = entry
        return stats

    async def _call_llm_atlas_part(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        """全书语义图谱单路 LLM 调用（共享 self._llm 与并发槽）。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        atlas_tok_cfg = max(1024, int(getattr(self.config.analysis, "atlas_max_tokens", 32768) or 32768))
        if atlas_tok_cfg == 32768:
            max_tok = min(max(int(getattr(self.config.api, "max_tokens", 16384) or 16384), 16384), 32768)
        else:
            max_tok = atlas_tok_cfg
        is_char = system_prompt == ATLAS_CHAR_SYSTEM_PROMPT
        unit_idx = 2 if is_char else 3
        lane_id = 930000 + unit_idx
        label = "人物谱系图谱" if is_char else "世界舞台图谱"
        await self._acquire_llm_slot()
        ok = False
        self._lane_start(lane_id, label, "report")
        try:
            on_progress = self._make_on_progress("summary_phase_4", unit_idx)
            llm_result = await self._llm.chat_auto(
                messages, max_tokens=max_tok, validate_response=None, on_progress=on_progress,
            )
            ok = bool(llm_result.success)
        finally:
            self._lane_done(lane_id, label, ok, "report")
            self._release_llm_slot()
        self._record_tokens("atlas", llm_result.tokens)
        if llm_result.success:
            return llm_result.content
        logger.warning(f"图谱提取 LLM 调用失败: {llm_result.error}")
        return None

    async def _cancel_atlas_tasks(self) -> None:
        """停止或失败退出前取消并回收全部在途图谱子任务。"""
        tasks = [t for t in list(getattr(self, "_atlas_tasks", []) or []) if t is not None]
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except Exception:
                pass
        self._atlas_tasks = []

    async def _run_atlas_extraction(
        self,
        volume_summaries: List[str],
        batch_tasks: List[tuple],
        stage1_fp: str,
    ) -> None:
        """Stage 4 并发提取全书语义图谱（双子指纹独立缓存，异常降级不阻塞主报告）。"""
        if not volume_summaries or self._stop_requested:
            return

        a = self.config.analysis
        atlas_overrides = self._non_default_tuning(a, {
            "atlas_max_tokens": 32768,
            "atlas_max_characters": 60,
            "atlas_max_locations": 50,
            "volume_compress_threshold": 80000,
            "volume_compress_group": 5,
            "volume_compress_ratio": 0.5,
        })
        target_fp = compute_atlas_fingerprint(
            stage1_fp=stage1_fp,
            volume_summaries=volume_summaries,
            model=getattr(self, "_summary_model_name", ""),
            provider=getattr(getattr(self, "_llm", None), "config", self.config.api).provider,
            extra_overrides=atlas_overrides or None,
        )
        atlas_path = self.output_dir / STORY_ATLAS_FILE
        existing = await asyncio.to_thread(safe_load_json, atlas_path) if atlas_path.exists() else None
        if not isinstance(existing, dict):
            existing = {}

        has_valid_char = isinstance(existing.get("characters"), list) and len(existing["characters"]) > 0
        has_valid_world = (
            (isinstance(existing.get("stages"), list) and len(existing["stages"]) > 0)
            or (isinstance(existing.get("world_tree"), list) and len(existing["world_tree"]) > 0)
        )
        need_char = not (has_valid_char and existing.get("char_atlas_fp") == target_fp)
        need_world = not (has_valid_world and existing.get("world_atlas_fp") == target_fp)
        if not need_char and not need_world:
            logger.info("全书图谱：命中 story_atlas.json 双指纹缓存，跳过图谱 LLM 调用")
            return

        vol_ranges = [(idx, s, e) for idx, s, e, _ in sorted(batch_tasks, key=lambda t: t[0])]
        anchors = await asyncio.to_thread(
            build_deterministic_anchors,
            self.output_dir,
            vol_ranges,
            max_characters=max(1, int(getattr(a, "atlas_max_characters", 60) or 60)),
            max_locations=max(1, int(getattr(a, "atlas_max_locations", 50) or 50)),
        )
        compressed_vols = self._compress_volume_summaries(volume_summaries)
        vol_block = f"《{self.book_name}》全书分卷分析摘要（共{len(volume_summaries)}卷）：\n\n{compressed_vols}"

        char_task: Optional[asyncio.Task] = None
        world_task: Optional[asyncio.Task] = None
        sub_tasks: List[asyncio.Task] = []
        if need_char:
            char_task = asyncio.create_task(
                self._call_llm_atlas_part(ATLAS_CHAR_SYSTEM_PROMPT, anchors["char_anchor"] + "\n\n" + vol_block)
            )
            sub_tasks.append(char_task)
        if need_world:
            world_task = asyncio.create_task(
                self._call_llm_atlas_part(ATLAS_WORLD_SYSTEM_PROMPT, anchors["world_anchor"] + "\n\n" + vol_block)
            )
            sub_tasks.append(world_task)
        self._atlas_tasks.extend(sub_tasks)

        try:
            await asyncio.gather(*sub_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for st in sub_tasks:
                if not st.done():
                    st.cancel()
            await asyncio.gather(*sub_tasks, return_exceptions=True)
            raise

        if self._stop_requested:
            return

        updated = False
        chars_out = list(existing.get("characters") or []) if has_valid_char else []
        rels_out = list(existing.get("relationships") or []) if has_valid_char else []
        char_fp_out = str(existing.get("char_atlas_fp") or "") if has_valid_char else ""

        if char_task is not None and not char_task.cancelled():
            try:
                raw_char = char_task.result()
                parsed_char = parse_and_sanitize_char_atlas(raw_char) if raw_char else None
                if parsed_char is not None:
                    chars_out = parsed_char["characters"]
                    rels_out = parsed_char["relationships"]
                    char_fp_out = target_fp
                    updated = True
                else:
                    char_fp_out = ""
                    logger.warning("全书图谱 CharacterAtlas 解析为空或不合格，不锁定指纹")
            except Exception as e:
                char_fp_out = ""
                logger.warning(f"全书图谱 CharacterAtlas 异常: {e}")

        stages_out = list(existing.get("stages") or []) if has_valid_world else []
        tree_out = list(existing.get("world_tree") or []) if has_valid_world else []
        world_fp_out = str(existing.get("world_atlas_fp") or "") if has_valid_world else ""

        if world_task is not None and not world_task.cancelled():
            try:
                raw_world = world_task.result()
                parsed_world = parse_and_sanitize_world_atlas(raw_world) if raw_world else None
                if parsed_world is not None:
                    stages_out = parsed_world["stages"]
                    tree_out = parsed_world["world_tree"]
                    world_fp_out = target_fp
                    updated = True
                else:
                    world_fp_out = ""
                    logger.warning("全书图谱 WorldStageAtlas 解析为空或不合格，不锁定指纹")
            except Exception as e:
                world_fp_out = ""
                logger.warning(f"全书图谱 WorldStageAtlas 异常: {e}")

        if updated and not self._stop_requested:
            payload = {
                "schema_version": STORY_ATLAS_SCHEMA_VERSION,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "model": getattr(self, "_summary_model_name", ""),
                "char_atlas_fp": char_fp_out,
                "world_atlas_fp": world_fp_out,
                "characters": chars_out,
                "relationships": rels_out,
                "stages": stages_out,
                "world_tree": tree_out,
            }
            await asyncio.to_thread(safe_save_json, payload, atlas_path, ensure_ascii=False)
            logger.info(
                f"全书图谱已落盘 {atlas_path.name}: {len(chars_out)} 角色, {len(rels_out)} 关系, "
                f"{len(stages_out)} 阶段, {len(tree_out)} 世界根节点"
            )

    async def _cancel_style_task(self, style_task) -> None:
        """停止路径专用：cancel 并回收风格提取任务，杜绝孤儿任务继续烧 token"""
        if style_task is None:
            return
        if not style_task.done():
            style_task.cancel()
        try:
            await asyncio.gather(style_task, return_exceptions=True)
        except Exception:
            pass

    async def _run_style_extraction(self) -> Optional[dict]:
        """调用 style_analyzer 提取风格特征（命中 style_profile.json 缓存则跳过并发槽与 LLM）"""
        from backend.core.style_analyzer import extract_style_profile

        blocks_dir = self._resolve_blocks_dir()
        if blocks_dir is None or not blocks_dir.exists():
            logger.warning(f"风格分析：blocks 目录不存在: {blocks_dir}")
            return None

        blocks_fp = await asyncio.to_thread(self._compute_blocks_fingerprint)
        style_fp = self._compute_style_fingerprint(blocks_fp) if blocks_fp else ""
        if style_fp:
            cached = await asyncio.to_thread(self._load_style_checkpoint, style_fp)
            if cached is not None:
                logger.info("风格分析：命中 style_profile.json 断点缓存，跳过 LLM 调用")
                return cached

        style_api_cfg = replace(
            self.config.api,
            model=self.config.api.summary_model or self.config.api.model,
            thinking_mode=self.config.api.summary_thinking_mode or self.config.api.thinking_mode,
            timeout=self.config.api.summary_timeout,
        )
        a = self.config.analysis

        try:
            await self._acquire_llm_slot()
            ok = False
            self._lane_start(920001, "写作风格分析", "style")
            try:
                profile = await extract_style_profile(
                    blocks_dir,
                    self.book_name,
                    style_api_cfg,
                    token_sink=lambda t: self._record_tokens("style", t),
                    sample_n=max(1, int(getattr(a, "style_sample_chapters", 6) or 6)),
                    sample_chars=max(100, int(getattr(a, "style_sample_chars", 1500) or 1500)),
                    max_tokens=max(512, int(getattr(a, "style_max_tokens", 20000) or 20000)),
                    temperature=float(getattr(a, "style_temperature", 0.3)),
                    encoding_priority=getattr(a, "encoding_priority", None),
                )
                ok = profile is not None
            finally:
                self._lane_done(920001, "写作风格分析", ok, "style")
                self._release_llm_slot()
        except Exception as e:
            logger.warning(f"风格分析失败: {e}")
            return None

        if profile is not None and style_fp and not self._stop_requested and self._is_valid_style_profile(profile):
            await asyncio.to_thread(self._save_style_checkpoint, style_fp, profile)
        return profile

    def _format_style_for_prompt(self, style_profile: dict) -> str:
        """将 style_profile 格式化为自然语言段落，注入最终报告 prompt"""
        agg = style_profile.get("statistical", {})
        sem = style_profile.get("semantic", {})

        parts = ["## 写作风格特征\n"]

        parts.append("### 定量指标")
        parts.append(f"- 平均句长 {agg.get('avg_sentence_length', 0):.1f} 字，"
                     f"短句(≤10字) {agg.get('short_sentence_ratio', 0):.1%}，"
                     f"长句(≥50字) {agg.get('long_sentence_ratio', 0):.1%}")
        parts.append(f"- 对话占比 {agg.get('dialogue_ratio', 0):.1%}，"
                     f"词汇丰富度(TTR) {agg.get('ttr', 0):.2f}")
        parts.append(f"- 感官词密度 {agg.get('sensory_density', 0):.1f}/千字，"
                     f"比喻词频率 {agg.get('metaphor_freq', 0):.1f}/千字")
        parts.append(f"- 文言词占比 {agg.get('literary_ratio', 0):.2f}%，"
                     f"口语词占比 {agg.get('colloquial_ratio', 0):.2f}%")

        parts.append("\n### 定性特征")
        for key, label in [
            ('signature_expressions', '标志性表达'),
            ('narrative_rhythm', '叙事节奏'),
            ('dialogue_style', '对话风格'),
            ('rhetorical_preferences', '修辞偏好'),
            ('emotional_expression', '情感表达'),
            ('narrative_voice', '叙事视角'),
            ('information_control', '信息管控'),
            ('narrator_and_genre', '叙述者与类型'),
        ]:
            value = sem.get(key, "")
            if value and '未提取' not in value and '提取失败' not in value:
                parts.append(f"- **{label}**：{value}")

        return "\n".join(parts)

    async def run(self) -> Optional[str]:
        """执行完整总结流程。返回最终报告文本；用户停止返回 None；失败抛 RuntimeError"""
        t_start = time.time()

        output_subdir = self.output_dir / "output"
        if not output_subdir.is_dir():
            output_subdir = self.output_dir

        self._emit_progress({"type": "status", "message": "正在加载章节数据..."})
        results = await asyncio.to_thread(self._load_results)
        if not results:
            raise RuntimeError("未找到有效的章节分析结果")
        total_chapters = len(results)
        total_batches = (total_chapters + self.batch_size - 1) // self.batch_size
        results_fp = await asyncio.to_thread(self._compute_results_fingerprint)
        stage1_fp = self._compute_stage1_fingerprint(results_fp)
        logger.info(f"加载{total_chapters}块，分为{total_batches}批，每批{self.batch_size}章")

        foreshadow_catalog = await self._build_foreshadow_catalog(results)

        human_overrides = {
            i.id: i for i in self.ledger.items
            if getattr(i, "source_type", "") == "human_override"
        }
        clean_ledger = ForeshadowLedger(
            batch_size_snapshot=self.batch_size,
            total_chapters=results[-1]['ch'] if results else 0,
        )
        catalog_ids = set()
        added = 0
        for cat in foreshadow_catalog:
            lid = cat["id"]
            catalog_ids.add(lid)
            if lid in human_overrides:
                clean_ledger.items.append(human_overrides[lid])
            else:
                clean_ledger.items.append(ForeshadowItem(
                    id=lid, description=cat["clue"],
                    first_seen_chapter=cat["first_seen"], first_seen_batch=0,
                    last_seen_chapter=cat["last_seen"], last_seen_batch=0,
                    evidence_chapters=cat["evidence_chapters"],
                    confidence=0.8, source_type='catalog_scan',
                    composite_score=cat.get("composite_score", 0),
                    importance=cat.get("importance", "中"),
                    anchor_quote=cat.get("anchor_quote", "") or "",
                    anchor_chapter=int(cat.get("anchor_chapter") or 0),
                    entity_key=cat.get("entity_key", "") or "",
                    question=cat.get("question", "") or "",
                    answer_criteria=cat.get("answer_criteria", "") or "",
                    lead=not (cat.get("question") or "").strip()
                    and not (cat.get("answer_criteria") or "").strip(),
                ))
                added += 1
        for lid, item in human_overrides.items():
            if lid not in catalog_ids:
                clean_ledger.items.append(item)
        self.ledger = clean_ledger
        logger.info(f"伏笔总表构造干净账本: {added} 条规则条目，{len(human_overrides)} 条人工覆写")

        batch_tasks = []
        for i in range(0, total_chapters, self.batch_size):
            batch = results[i:i + self.batch_size]
            batch_idx = len(batch_tasks) + 1
            ch_start = batch[0]['ch']
            ch_end = batch[-1]['ch']
            batch_data = self._format_batch(batch)
            catalog_text = self._format_catalog_for_prompt(foreshadow_catalog, max_chapter=ch_end)
            summary_prompt = BATCH_USER_TEMPLATE.format(
                book_name=self.book_name, start=ch_start, end=ch_end,
                batch_data=batch_data, foreshadow_catalog=catalog_text,
                batch_min_words=self.config.analysis.batch_summary_min_words
            )
            batch_tasks.append((batch_idx, ch_start, ch_end, summary_prompt))

        self._emit_progress({
            "type": "phase", "phase": "batch", "phase_label": "分卷分析+伏笔调和",
            "total_batches": total_batches,
            "concurrency": self.concurrency, "real_lanes": True,
            "message": f"[阶段1/4] 分卷分析+伏笔调和（{len(batch_tasks)}批，并发{self.concurrency}）..."
        })

        logger.info(f"开始并发卷摘要：{len(batch_tasks)}批，并发数={self.concurrency}")

        volume_results, recon_results = await self._load_checkpoint(batch_tasks, stage1_fp=stage1_fp)
        stage1_baseline = await asyncio.to_thread(self._load_stage1_baseline, stage1_fp)
        stage1_full_hit = (
            stage1_baseline is not None
            and len(volume_results) == len(batch_tasks)
            and len(recon_results) == len(batch_tasks)
            and all(idx in volume_results and idx in recon_results for idx, _s, _e, _p in batch_tasks)
        )
        restored = len(volume_results)
        summary_done = len(volume_results)
        recon_done = len(recon_results)
        if stage1_full_hit:
            summary_done = total_batches
            recon_done = total_batches
            logger.info(f"断点续跑：命中 Stage 1 干净账本基线（{len(batch_tasks)} 批），跳过阶段 1 全部 LLM 调用")
            self._emit_progress({"type": "status", "message": f"已从 Stage 1 断点基线恢复 {len(batch_tasks)} 批..."})
            self._emit_progress({
                "type": "stage1_progress",
                "summary_done": summary_done,
                "recon_done": recon_done,
                "total_batches": total_batches,
            })
        elif restored:
            logger.info(f"断点续跑：恢复 {restored}/{len(batch_tasks)} 批卷摘要，跳过对应 LLM 调用")
            self._emit_progress({"type": "status",
                                 "message": f"已从断点恢复 {restored} 批卷摘要，继续剩余批次..."})
        else:
            self._emit_progress({"type": "status", "message": f"正在并发分析{len(batch_tasks)}批（并发{self.concurrency}）..."})

        if not stage1_full_hit and (summary_done > 0 or recon_done > 0):
            self._emit_progress({
                "type": "stage1_progress",
                "summary_done": summary_done,
                "recon_done": recon_done,
                "total_batches": total_batches,
            })

        async def process_batch_collect(idx: int, ch_start: int, ch_end: int, prompt: str):
            nonlocal summary_done, recon_done
            if self._stop_requested:
                return None

            if idx in volume_results:
                volume_summary = volume_results[idx]
                elapsed = 0.0
                restored_batch = True
            else:
                summary_response = await self._call_llm_summary(prompt, batch_idx=idx)
                if self._stop_requested:
                    return None
                if summary_response is None:
                    return {"status": "failed", "idx": idx, "ch_start": ch_start, "ch_end": ch_end}
                volume_summary = extract_analysis_text(summary_response)
                ok_vol, vol_err = validate_volume_summary(volume_summary)
                if not ok_vol:
                    logger.warning(f"[卷{idx}] 卷摘要校验未通过，跳过落盘: {vol_err}")
                    return {"status": "failed", "idx": idx, "ch_start": ch_start, "ch_end": ch_end}
                volume_results[idx] = volume_summary
                elapsed = time.time() - t_start
                restored_batch = False
                await self._save_checkpoint_batch(idx, ch_start, ch_end, volume_summary, None, stage1_fp=stage1_fp)
                summary_done = min(total_batches, summary_done + 1)
                self._emit_progress({
                    "type": "stage1_progress",
                    "summary_done": summary_done,
                    "recon_done": recon_done,
                    "total_batches": total_batches,
                })

            if idx in recon_results:
                foreshadow_data = recon_results[idx]
            else:
                active_block = self._build_active_foreshadows_block(ch_start, ch_end, foreshadow_catalog)
                reconciliation_prompt = RECONCILIATION_USER_TEMPLATE.format(
                    book_name=self.book_name, start=ch_start, end=ch_end,
                    batch_summary=volume_summary,
                    active_foreshadows_block=active_block
                )
                foreshadow_data = await self._call_llm_reconciliation(reconciliation_prompt, batch_idx=idx)
                if foreshadow_data is not None:
                    await self._save_checkpoint_batch(idx, ch_start, ch_end, None, foreshadow_data, stage1_fp=stage1_fp)
                if not self._stop_requested:
                    recon_done = min(total_batches, recon_done + 1)
                    self._emit_progress({
                        "type": "stage1_progress",
                        "summary_done": summary_done,
                        "recon_done": recon_done,
                        "total_batches": total_batches,
                    })
            return {
                "status": "done", "idx": idx, "ch_start": ch_start, "ch_end": ch_end,
                "volume_summary": volume_summary, "elapsed": elapsed,
                "foreshadow_data": foreshadow_data, "restored": restored_batch,
            }

        style_task = asyncio.create_task(self._run_style_extraction())
        self._style_task = style_task

        try:
            stage1_complete = False
            if stage1_full_hit:
                restored_ledger = ForeshadowLedger.from_dict(stage1_baseline["ledger"])
                if human_overrides:
                    r_map = {it.id: idx_pos for idx_pos, it in enumerate(restored_ledger.items)}
                    for lid, hi in human_overrides.items():
                        if lid in r_map:
                            restored_ledger.items[r_map[lid]] = hi
                        else:
                            restored_ledger.items.append(hi)
                self.ledger = restored_ledger
                for idx, _ch_start, _ch_end, _ in batch_tasks:
                    self._emit_progress({
                        "type": "batch_done", "batch": idx, "total_batches": total_batches,
                        "elapsed": 0.0, "message": f"[卷{idx}/{total_batches}] 已从断点恢复",
                    })
                stage1_complete = True
            else:
                tasks = [asyncio.create_task(process_batch_collect(idx, start, end, prompt))
                         for idx, start, end, prompt in batch_tasks]
                done_records: List[dict] = []
                stage1_has_failure = False
                for coro in asyncio.as_completed(tasks):
                    try:
                        result = await coro
                        if result is None:
                            continue
                        if result["status"] == "done":
                            idx = result["idx"]
                            self._emit_progress({
                                "type": "batch_done", "batch": idx, "total_batches": total_batches,
                                "elapsed": result["elapsed"],
                                "message": f"[卷{idx}/{total_batches}] {'已从断点恢复' if result.get('restored') else '完成'}"
                            })
                            done_records.append(result)
                        elif result["status"] == "failed":
                            idx = result["idx"]
                            stage1_has_failure = True
                            self._emit_progress({
                                "type": "batch_failed", "batch": idx,
                                "message": f"[卷{idx}] LLM调用失败，跳过"
                            })
                    except Exception as e:
                        stage1_has_failure = True
                        logger.error(f"卷异常: {e}")

                for rec in sorted(done_records, key=lambda r: r["idx"]):
                    async with self._lock:
                        self._apply_reconciliation(
                            rec["idx"], rec["ch_start"], rec["ch_end"], rec["foreshadow_data"]
                        )

                if self._stop_requested:
                    await self._cancel_style_task(style_task)
                    self._ensure_lane_state()[0].clear()
                    self._emit_progress({"type": "status", "message": "已停止"})
                    return None
                if not volume_results:
                    raise RuntimeError("所有批次均失败，无法生成总结")

                stage1_complete = (
                    not stage1_has_failure
                    and len(done_records) == len(batch_tasks)
                    and all(r.get("foreshadow_data") is not None for r in done_records)
                )
                if stage1_complete:
                    await asyncio.to_thread(self._save_stage1_baseline, stage1_fp)

            volume_summaries = []
            for idx, ch_start, ch_end, _ in batch_tasks:
                text = volume_results.get(idx, "")
                if text:
                    volume_summaries.append(f"=== 第{ch_start}-{ch_end}章 ===\n{text}")

            recheck_ver = str(
                getattr(self.config.analysis, "recheck_version", "v3") or "v3"
            ).strip().lower()
            norm_ver = "v2" if recheck_ver == "v2" else "v3"
            blocks_fp = await asyncio.to_thread(self._compute_blocks_fingerprint)
            stage2_fp = self._compute_stage2_fingerprint(stage1_fp, blocks_fp, norm_ver)

            stage2_loaded = False
            if stage1_complete and blocks_fp:
                stage2_ckpt = await asyncio.to_thread(self._load_stage2_checkpoint, stage2_fp)
                if stage2_ckpt is not None:
                    self.ledger = ForeshadowLedger.from_dict(stage2_ckpt["ledger"])
                    if human_overrides:
                        s_map = {it.id: idx_pos for idx_pos, it in enumerate(self.ledger.items)}
                        for lid, hi in human_overrides.items():
                            if lid in s_map:
                                self.ledger.items[s_map[lid]] = hi
                            else:
                                self.ledger.items.append(hi)
                    self.last_recheck_summary = dict(stage2_ckpt["recheck_summary"])
                    stage2_loaded = True
                    logger.info(f"断点续跑：命中 Stage 2 复检断点（{norm_ver}），跳过阶段 2 LLM 调用")
                    self._emit_progress({
                        "type": "phase", "phase": "recheck",
                        "phase_label": "带证据伏笔复检" if norm_ver == "v2" else "带证据伏笔复检 (V3)",
                        "concurrency": self.concurrency, "real_lanes": True,
                        "message": f"[阶段2/4] 伏笔复检 ({norm_ver.upper()}) 已从断点恢复",
                    })
                    self._emit_progress({
                        "type": "recheck_done",
                        "recheck": dict(self.last_recheck_summary),
                        "message": f"伏笔复检已从断点恢复（{self.last_recheck_summary.get('status', 'COMPLETED')}）",
                    })

            if not stage2_loaded:
                if norm_ver == "v2":
                    outcome = await self._run_recheck_v2(results)
                else:
                    outcome = await self._run_recheck_v3(results, volume_summaries)
                if self._stop_requested:
                    self.ledger.save(self.ledger_path)
                    await self._cancel_style_task(style_task)
                    self._ensure_lane_state()[0].clear()
                    self._emit_progress({"type": "status", "message": "已停止"})
                    return None
                if (
                    stage1_complete
                    and blocks_fp
                    and outcome is not None
                    and getattr(outcome, "status", None) == "COMPLETED"
                    and isinstance(getattr(outcome, "failed_batches", None), int)
                    and outcome.failed_batches == 0
                    and not (set(getattr(outcome, "not_checked", []) or []) - set(getattr(outcome, "dropped_ids", []) or []))
                ):
                    await asyncio.to_thread(self._save_stage2_checkpoint, stage2_fp)

            if not self._stop_requested:
                dormant_th = max(1, int(getattr(self.config.analysis, "dormant_chapter_threshold", 200) or 200))
                for idx, _ch_start, ch_end, _prompt in sorted(batch_tasks, key=lambda t: t[0]):
                    self.ledger.reconcile_and_update(idx, ch_end, dormant_threshold=dormant_th)

            self.ledger.save(self.ledger_path)
            self._emit_progress({"type": "ledger_updated", "counts": self.ledger.get_item_count()})

            if not style_task.done():
                self._emit_progress({
                    "type": "phase", "phase": "style", "phase_label": "风格分析",
                    "concurrency": self.concurrency, "real_lanes": True,
                    "message": "[阶段3/4] 正在提取写作风格特征..."
                })
            style_profile = await style_task
        finally:
            await self._cancel_style_task(style_task)
            self._style_task = None
        style_text = self._format_style_for_prompt(style_profile) if style_profile else "（风格分析未完成）"
        if self._stop_requested:
            self._ensure_lane_state()[0].clear()
            self._emit_progress({"type": "status", "message": "已停止"})
            return None

        self._emit_progress({
            "type": "phase", "phase": "report", "phase_label": "生成全书脉络报告",
            "concurrency": self.concurrency, "real_lanes": True,
            "message": "[阶段4/4] 正在生成全书脉络报告..."
        })
        foreshadow_context = self._build_foreshadow_context(foreshadow_catalog)
        audit_text = self.ledger.audit_text_for_report()
        final_prompt = FINAL_USER_TEMPLATE.format(
            book_name=self.book_name,
            volume_summaries=self._compress_volume_summaries(volume_summaries),
            foreshadow_context=foreshadow_context,
            foreshadow_audit=audit_text,
            style_profile=style_text,
            final_min_words=self.config.analysis.final_report_min_words
        )
        self._atlas_tasks = []
        atlas_task = asyncio.create_task(
            self._run_atlas_extraction(volume_summaries, batch_tasks, stage1_fp)
        )
        self._atlas_tasks.append(atlas_task)

        try:
            final_report = await self._call_llm_final(final_prompt, batch_idx=0)
            if self._stop_requested:
                await self._cancel_atlas_tasks()
                self._ensure_lane_state()[0].clear()
                self._emit_progress({"type": "status", "message": "已停止"})
                return None
            if final_report is None:
                await self._cancel_atlas_tasks()
                raise RuntimeError("最终汇总LLM调用失败")
            try:
                await atlas_task
            except Exception as e:
                logger.warning(f"全书图谱并发任务异常（已降级忽略）: {e}")
        finally:
            await self._cancel_atlas_tasks()

        audit_path = self.output_dir / "foreshadow_audit.md"
        await asyncio.to_thread(audit_path.write_text, audit_text, encoding='utf-8')

        elapsed = time.time() - t_start
        logger.info(f"final_summary 完成：peak in-flight = {self._peak_inflight} / concurrency = {self.concurrency}")
        if self._peak_inflight > self.concurrency:
            logger.error(f"BUG：peak in-flight {self._peak_inflight} 超 concurrency {self.concurrency}！")
        self._ensure_lane_state()[0].clear()
        self._emit_progress({"type": "complete", "elapsed": elapsed,
                             "message": f"全书脉络报告生成完成，耗时{elapsed:.0f}秒"})
        return final_report
