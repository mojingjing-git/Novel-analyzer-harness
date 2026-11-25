"""
分析流水线（asyncio 版）
- 普通 async 类，进度经 async 回调发布（由 service 层转发 WebSocket）
- asyncio.Semaphore + asyncio.as_completed 控制并发
- 三阶段循环：串行预热 → 流式并发 → 失败补跑（最多3轮）
- 批次 Rolling：asyncio.Task 后台执行，不阻塞分析；asyncio.Event 同步
- 磁盘产物由 MemoryState 统一落盘
"""

import asyncio
from collections import deque
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

from ..config.settings import AppConfig
from ..config.constants import (
    OUTPUT_DIR_NAME,
    ROLLING_SUMMARY_MAX_TOKENS, ROLLING_SUMMARY_MAX_RETRIES,
    ROLLING_SUMMARY_TEMPERATURE,
    ROLLING_EARLY_CHAPTERS,
    ROLLING_MOMENTUM_WINDOW, ROLLING_ARCHIVE_TRIGGER_COUNT,
    ROLLING_ARCHIVE_THINKING_MIN_TOKENS,
)
from .file_processor import FileProcessor
from .analyzer import BatchAnalysisResult, NovelAnalyzer
from .batch_packer import (
    effective_batch_cap,
    is_thinking_enabled,
    pack_chapter_batches,
    resolve_thinking_effort,
)
from .knowledge_base import KnowledgeBaseManager
from .llm_client import LLMClient
from .memory_state import MemoryState
from ..core.moderation import is_moderation_error, is_moderation_message
from ..core.error_taxonomy import ErrorClass, classify_message
from .error_types import ErrorScope, ErrorVerdict, ErrorClass as VerdictErrorClass
from ..ws_events import PipelineStatus
from ..utils.export_utils import export_summary_report
from ..utils.json_utils import extract_json_from_text, safe_parse_json

logger = logging.getLogger(__name__)

# 进度回调类型：async fn(dict)
ProgressCallback = Callable[[dict], Awaitable[None]]


def _fmt_batch_range(chapters: Sequence[int]) -> str:
    """格式化章节批次范围字符串，供进度与日志使用。"""
    chs = [int(c) for c in chapters]
    if not chs:
        return "ch0"
    if len(chs) == 1:
        return f"ch{chs[0]}"
    return f"ch{chs[0]}-ch{chs[-1]}"


def _fmt_range(block_id: int, block_chs, block_size: int) -> str:
    """格式化章节范围字符串（保留兼容入口）。"""
    if block_chs:
        return _fmt_batch_range(block_chs)
    if block_size > 1:
        return f"ch{block_id}-ch{block_id + block_size - 1}"
    return f"ch{block_id}"


def _usage_status_of(retry_info) -> str:
    """从块的 retry_info 读出用量状态，只返回 "KNOWN" 或 "UNKNOWN"。

    - 缺省 KNOWN：未透传 usage_status 的调用方（含测试桩）保持原有统计口径，
      成功调用本来就拿得到 usage，对外数字不变；
    - 只有明确 UNKNOWN（本次没拿到 usage）才在对外载荷里打未知标记，
      避免下游把"未知"读成"零消耗"。
    兼容 UsageStatus 枚举与裸字符串两种输入。
    """
    raw = getattr((retry_info or {}).get("usage_status"), "value",
                  (retry_info or {}).get("usage_status"))
    return "UNKNOWN" if str(raw or "KNOWN").upper() == "UNKNOWN" else "KNOWN"


# ==================== 结构化 Rolling 摘要 Prompt ====================

STRUCTURED_ROLLING_EARLY_PROMPT = """你是一个小说信息提取器。从以下章节数据中提取结构化信息，输出纯 JSON（不要任何 markdown 包裹）。

## 输出 Schema
{{
  "global_milestones": ["ch1-30: 一句话概括该段剧情", ...],
  "paradigm_layers": [
    {{"id": "layer1", "chapters": "1-N", "core_rules": "该阶段的核心规则/力量体系", "is_active": true}}
  ],
  "current_context": {{"location": "当前位置", "current_goal": "当前目标", "immediate_threat": "当前威胁"}},
  "active_causal_chains": ["原因A → 导致B → 推动C", ...],
  "recent_momentum": ["ch{N}: 事件描述", ...]
}}

## 规则
1. global_milestones：每条 ≤30 字，概括一个阶段的核心剧情转折，最多 5 条
2. paradigm_layers：如果世界观/力量体系有重大切换才新增条目
3. current_context：提取最后 10 章的状态
4. active_causal_chains：找出推动剧情的因果链，每条 ≤40 字
5. recent_momentum：最近 10 个关键事件，每条必须以 ch{{N}}: 开头，≤20 字
6. 只提取原文中明确出现的信息，不要编造"""

STRUCTURED_ROLLING_USER_TEMPLATE = """【章节数据】
{chapters}

请输出 JSON："""

STRUCTURED_ROLLING_SYSTEM_PROMPT = """你是一个小说信息更新器。根据【当前数据】和【新章节信息】，输出更新后的完整 JSON。

## 规则
1. 保留【当前数据】中仍有价值的条目，追加【新章节信息】中的新发现
2. 不要重复已有条目
3. recent_momentum 每条必须以 ch{{N}}: 开头，≤20 字
4. global_milestones 每条 ≤30 字
5. active_causal_chains 每条 ≤40 字
6. 如果新章节中出现了范式切换（世界观/力量体系重大变化），在 paradigm_layers 中新增条目并设 is_active=true，旧的设为 false
7. 更新 current_context 为最新状态
8. 只输出 JSON，不要任何 markdown 包裹或解释

## 输出 Schema
{{
  "global_milestones": ["ch1-30: 一句话概括", ...],
  "paradigm_layers": [
    {{"id": "layer1", "chapters": "1-N", "core_rules": "规则描述", "is_active": false}},
    {{"id": "layer2", "chapters": "N+1-M", "core_rules": "新规则描述", "is_active": true}}
  ],
  "current_context": {{"location": "当前位置", "current_goal": "当前目标", "immediate_threat": "当前威胁"}},
  "active_causal_chains": ["原因A → 导致B → 推动C", ...],
  "recent_momentum": ["ch{N}: 事件描述", ...]
}}"""

STRUCTURED_ROLLING_USER_UPDATE_TEMPLATE = """【当前数据】
{current_data}

【新章节信息】
{new_chapters}

请输出更新后的完整 JSON："""


# ==================== 结构化滚动总结辅助函数 ====================

_DEFAULT_ROLLING_SCHEMA = {
    "global_milestones": [],
    "paradigm_layers": [],
    "current_context": {"location": "", "current_goal": "", "immediate_threat": ""},
    "active_causal_chains": [],
    "recent_momentum": []
}


def _validate_and_fill_rolling_schema(raw_dict: dict) -> dict:
    """确保 LLM 输出的 JSON 包含所有必填字段，缺失的用默认值补全"""
    result = {}
    for key, default_val in _DEFAULT_ROLLING_SCHEMA.items():
        val = raw_dict.get(key)
        if val is None:
            result[key] = default_val if not isinstance(default_val, dict) else dict(default_val)
        elif isinstance(default_val, list) and not isinstance(val, list):
            result[key] = []
        elif isinstance(default_val, dict) and not isinstance(val, dict):
            result[key] = dict(default_val)
        else:
            result[key] = val
    # 确保 current_context 子字段存在
    ctx = result.get("current_context", {})
    if not isinstance(ctx, dict):
        ctx = {}
    for k, v in _DEFAULT_ROLLING_SCHEMA["current_context"].items():
        ctx.setdefault(k, v)
    result["current_context"] = ctx
    return result


def _extract_chapter_number(text) -> int:
    """从势头条目中提取章号（匹配 ch123、第123章、123章: 等格式）。
    容错：LLM 可能返回 dict 而非 str 的势头条目，dict 时退化为 0（不崩溃、不污染归档判断）。"""
    if isinstance(text, dict):
        text = text.get('text') or text.get('content') or text.get('chapter') or ''
    text = str(text)
    match = re.search(r'(?:ch|第)?(\d+)(?:章|:)', text, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _find_locked_milestones(milestones: list, paradigm_layers: list) -> set:
    """找出不可被 FIFO 淘汰的里程碑索引"""
    locked = set()
    if not milestones:
        return locked
    # 1. 永远锁定第一条里程碑（故事起点锚点）
    locked.add(0)
    # 2. 每个范式层的首条里程碑
    for layer in paradigm_layers:
        if not isinstance(layer, dict):
            # LLM 偶发返回字符串元素（如 ["修仙期"]）：跳过而非 AttributeError。
            # 异步路径会让该批 rolling 永久丢失（last_chapter 冻结），
            # 补跑后同步路径会把整场分析炸成 failed。
            continue
        chapters_str = layer.get("chapters", "")
        m = re.match(r'(\d+)', str(chapters_str))
        if not m:
            continue
        layer_start = int(m.group(1))
        for i, ms in enumerate(milestones):
            ch = _extract_chapter_number(ms)
            if ch >= layer_start and i not in locked:
                locked.add(i)
                break
    return locked


class AnalysisPipeline:
    """分析流水线（asyncio 版）"""

    def __init__(
        self,
        config: AppConfig,
        directory: Path,
        knowledge_file: Path,
        start_chapter: int = 1,
        end_chapter: Optional[int] = None,
        on_progress: Optional[ProgressCallback] = None,
        on_token_stats: Optional[ProgressCallback] = None,
        target_chapters: Optional[Sequence[int]] = None,
        force_rerun: bool = False,
        update_rolling: Optional[bool] = None,
    ):
        """
        Args:
            config: 应用配置
            directory: 章节文件目录（blocks/）
            knowledge_file: 知识库文件路径（用于最终保存合并结果）
            start_chapter: 起始章节
            end_chapter: 结束章节（None表示到最后）
            on_progress: 进度回调 async fn(dict)，payload 对齐 progress 信号
            on_token_stats: token 统计回调 async fn(dict)
            target_chapters: 指定定点重跑的章节列表（非空时仅重跑这些章并隐含 force_rerun）
            force_rerun: True 时忽略磁盘既有结果强制重跑目标范围
            update_rolling: 是否更新 rolling_summary.json；None 时定点重跑默认 False、全量默认 True
        """
        self.config = config
        self.directory = directory
        self.knowledge_file = knowledge_file
        self.start_chapter = start_chapter
        self.end_chapter = end_chapter
        self._stop_requested = False
        self._on_progress = on_progress
        self._on_token_stats = on_token_stats
        self.target_chapters: Optional[List[int]] = (
            sorted({int(c) for c in target_chapters}) if target_chapters is not None else None
        )
        self.force_rerun: bool = bool(force_rerun or (self.target_chapters is not None))
        self.update_rolling: Optional[bool] = update_rolling
        self._block_map: dict = {}  # block_id -> 实际章号列表（补跑时按原块恢复）
        self._rolling_early: int = ROLLING_EARLY_CHAPTERS  # 滚动摘要首生成阈值（可自适应）
        # 运行概览在途块（block_id -> {chapter, range, started_at}）：LLM 调用窗口内登记，
        # 供 /api/analysis/status#inflight_blocks 给前端车道对账（WS 丢 done 的治本纠偏源）
        self._inflight_blocks: dict = {}
        # 致命错误块登记（block_id -> 错误类别值，见 error_taxonomy）：
        # AUTH/BILLING/CONTEXT_LENGTH/BAD_REQUEST 类失败确定性不可自愈，补跑轮剔除不重试。
        # 审核类除外——skip_moderation_blocked=False 时用户明确要求补跑审核块
        self._fatal_blocks: dict = {}
        # 账户级熔断状态：欠费/认证失败首次结构化命中即停整场分析并透传给队列服务停队
        self._account_tripped: bool = False
        self._account_trip_reason: str = ""

    async def _emit(self, payload: dict) -> None:
        """发进度"""
        if self._on_progress:
            try:
                await self._on_progress(payload)
            except Exception as e:
                logger.warning(f"进度回调异常: {e}")

    async def _emit_tokens(self, payload: dict) -> None:
        """发 token 统计"""
        if self._on_token_stats:
            try:
                await self._on_token_stats(payload)
            except Exception as e:
                logger.warning(f"token 统计回调异常: {e}")

    def stop(self):
        """请求停止（联动 analyzer 与 rolling client，让进行中的 LLM 调用在下一检查点退出）"""
        self._stop_requested = True
        analyzer = getattr(self, '_analyzer', None)
        if analyzer is not None:
            analyzer.stop()
        rolling_client = getattr(self, '_rolling_client', None)
        if rolling_client is not None:
            rolling_client.request_stop()
        logger.info("收到停止请求")

    def _retryable_failed_blocks(self, failed_chapter_ids: list) -> list:
        """补跑列表过滤：剔除致命错误章节（AUTH/BILLING/CONTEXT_LENGTH/BAD_REQUEST）。"""
        retryable = [cid for cid in failed_chapter_ids if cid not in self._fatal_blocks]
        skipped_fatal = len(failed_chapter_ids) - len(retryable)
        if skipped_fatal:
            sample = ",".join(sorted({self._fatal_blocks[cid] for cid in failed_chapter_ids
                                      if cid in self._fatal_blocks}))
            logger.warning(f"补跑剔除{skipped_fatal}个致命错误章节（{sample}，重试成功率恒为零）")
        return retryable

    async def run(self) -> dict:
        """
        执行分析任务（三阶段：预热 → 动态并发池 → 缩容补跑）。
        请求层按批大小、正文字符与输出 Token 容量动态打包，落盘与进度严格按单章结算。
        返回 finished payload：{stopped, total_analyzed, errors, failed_chapters}
        """
        total_analyzed = 0
        self._fatal_blocks.clear()
        self._block_map.clear()
        self._account_tripped = False
        self._account_trip_reason = ""

        logger.info(f"开始分析任务（并发数={self.config.analysis.concurrency}）: {self.directory}")

        # 1. 初始化文件处理器
        self.file_processor = FileProcessor(
            self.directory,
            self.config.analysis.encoding_priority
        )

        # 2. 扫描章节（磁盘 IO 走线程池）
        all_chapters = await asyncio.to_thread(self.file_processor.scan_chapters)
        if not all_chapters:
            raise RuntimeError("未找到章节文件（需要1.txt, 2.txt等格式）")

        cfg_early = self.config.analysis.rolling_early_chapters or ROLLING_EARLY_CHAPTERS
        self._rolling_early = min(cfg_early, max(30, len(all_chapters) // 3))

        # 3. 确定分析范围与动态装包参数
        if self.end_chapter is None or self.end_chapter > all_chapters[-1]:
            self.end_chapter = all_chapters[-1]

        if self.target_chapters is not None:
            target_set = set(self.target_chapters)
            chapters_to_analyze = [c for c in all_chapters if c in target_set]
        else:
            chapters_to_analyze = [
                c for c in all_chapters
                if self.start_chapter <= c <= self.end_chapter
            ]
        valid_chapter_ids = set(chapters_to_analyze)
        total = len(chapters_to_analyze)
        effective_update_rolling = (
            bool(self.update_rolling)
            if self.update_rolling is not None
            else (self.target_chapters is None)
        )

        block_size = max(1, int(self.config.analysis.block_size or 1))
        max_content_chars = max(1, int(self.config.analysis.max_content_chars or 12000))
        max_tokens = self.config.api.max_tokens
        thinking_enabled = is_thinking_enabled(self.config.api)
        thinking_effort = resolve_thinking_effort(self.config.api)
        runtime_cap = effective_batch_cap(
            block_size,
            max_tokens,
            thinking_enabled,
            reasoning_effort=thinking_effort,
        )

        logger.info(f"共{total}章待分析（单批上限={block_size}章），并发数: {self.config.analysis.concurrency}")

        # 4. 确定输出目录 + 初始化 MemoryState（仅恢复单章产物，与 block_size 解耦）
        if self.config.working_directory:
            output_dir = Path(self.config.working_directory) / OUTPUT_DIR_NAME
        else:
            output_dir = Path(OUTPUT_DIR_NAME)
        output_dir.mkdir(parents=True, exist_ok=True)

        kb_limits = {
            'max_compressed_arcs': self.config.analysis.max_compressed_arcs,
            'max_recent_summaries': self.config.analysis.max_recent_summaries,
            'max_pacing_tracker': self.config.analysis.max_pacing_tracker,
            'max_foreshadow_network': self.config.analysis.max_foreshadow_network,
            'max_world_building': self.config.analysis.max_world_building,
            'max_verified_facts': self.config.analysis.max_verified_facts,
            'max_long_term_arcs': self.config.analysis.max_long_term_arcs,
            'max_thematic_elements': self.config.analysis.max_thematic_elements,
            'max_kb_character_states': getattr(self.config.analysis, 'max_kb_character_states', 200),
            'max_kb_character_relationships': getattr(self.config.analysis, 'max_kb_character_relationships', 300),
        }
        self.state = MemoryState(
            checkpoint_interval=self.config.analysis.checkpoint_interval,
            kb_limits=kb_limits,
        )
        state = self.state
        restore_chapters = set(all_chapters) if self.force_rerun else valid_chapter_ids
        restored = await state.restore_from_disk(
            output_dir, valid_chapters=restore_chapters
        )
        if restored > 0:
            logger.info(f"从磁盘恢复 {restored} 个已有单章 result")

        # 4a. 断点续跑 / 定点重跑范围初始化
        if self.force_rerun:
            done_chapters: set[int] = set()
            pending_chapters = list(chapters_to_analyze)
            restored_sorted: List[int] = []
        else:
            done_chapters = {c for c in chapters_to_analyze if c in state._flushed_chapters}
            skipped_set: set[int] = {c for c in chapters_to_analyze if c in state._skipped_chapters}
            pending_chapters = [
                c for c in chapters_to_analyze
                if c not in done_chapters and c not in skipped_set
            ]
            restored_sorted = sorted(done_chapters)
        if restored_sorted:
            logger.info(f"断点续跑：跳过已完成 {len(restored_sorted)} 章")
            emit_list = restored_sorted[-50:]
            offset = len(restored_sorted) - len(emit_list)
            for idx, ch in enumerate(emit_list, start=1):
                if idx % 20 == 0:
                    await asyncio.sleep(0)
                await self._emit({
                    "status": PipelineStatus.DONE,
                    "chapter": ch,
                    "progress": offset + idx,
                    "total": total,
                    "elapsed": 0,
                    "tokens": 0,
                    "usage_status": "KNOWN",
                    "range": f"ch{ch}",
                    "chapters_count": 1,
                    "restored": True,
                })

        if not pending_chapters:
            logger.info("所有章节已完成或已跳过，仍执行最终 KB 合并与落盘收尾")
            await state.flush_to_disk(output_dir)
            final_kb = await asyncio.to_thread(
                KnowledgeBaseManager.merge_results,
                output_dir,
                kb_limits=kb_limits,
                single_chapter_only=True,
            )
            await asyncio.to_thread(KnowledgeBaseManager(self.knowledge_file).save, final_kb)
            logger.info(f"最终知识库已保存: {self.knowledge_file}")
            all_done_results = sorted(state.results.values(), key=lambda r: r.chapter_number)
            if all_done_results:
                try:
                    summary_path = await asyncio.to_thread(
                        export_summary_report, all_done_results, output_dir)
                    logger.info(f"汇总报告已生成: {summary_path}")
                except Exception as e:
                    logger.warning(f"生成汇总报告失败: {e}")
            try:
                self.file_processor.clear_cache()
            except Exception:
                pass
            result_count = (
                len(done_chapters)
                if self.force_rerun
                else len([ch for ch in state.results if ch in valid_chapter_ids])
            )
            failed_chapters = state.failed_chapters_in(valid_chapter_ids)
            skipped_chapters = [
                f"{c}({r})" for c, r in state._skipped_chapters.items()
                if c in valid_chapter_ids
            ]
            return {
                "stopped": False,
                "total_analyzed": result_count,
                "errors": [f"失败{len(failed_chapters)}章"] if failed_chapters else [],
                "failed_chapters": failed_chapters,
                "skipped_chapters": skipped_chapters,
                "account_tripped": False,
                "account_trip_reason": "",
            }

        # 4b. 测量待跑章节字符数并执行动态装包
        def _measure_pending_lengths() -> dict[int, int]:
            lengths: dict[int, int] = {}
            for ch in pending_chapters:
                txt = self.file_processor.read_chapter(ch)
                lengths[ch] = len(txt) if txt is not None else 0
            return lengths

        chapter_lengths = await asyncio.to_thread(_measure_pending_lengths)
        initial_batches = pack_chapter_batches(
            pending_chapters,
            chapter_lengths,
            block_size=runtime_cap,
            max_content_chars=max_content_chars,
            max_tokens=max_tokens,
            thinking_enabled=thinking_enabled,
            reasoning_effort=thinking_effort,
            break_on_gap=True,
        )

        # 5. 初始化分析引擎
        analyzer = NovelAnalyzer(self.config)
        self._analyzer = analyzer

        # 6. 并发与预热参数（大批量时串行预热封顶 2 批，避免超时串行叠加阻塞全队）
        concurrency = max(1, int(self.config.analysis.concurrency or 1))
        skip_warmup = (restored > 10) or (self.force_rerun and restored > 0) or (self.target_chapters is not None)
        warmup_cap = 2 if block_size >= 4 else concurrency
        warmup_batches_count = 0 if skip_warmup else min(warmup_cap, concurrency, len(initial_batches))
        chapters_since_checkpoint = 0

        warmup_queue = deque(initial_batches[:warmup_batches_count])
        queue = deque(initial_batches[warmup_batches_count:])

        def _repack_queue_to_cap(q: deque, new_cap: int) -> None:
            if not q or new_cap < 1:
                return
            rebuilt: list[list[int]] = []
            changed = False
            for queued_batch in q:
                if len(queued_batch) > new_cap:
                    changed = True
                    rebuilt.extend(
                        pack_chapter_batches(
                            queued_batch,
                            chapter_lengths,
                            block_size=new_cap,
                            max_content_chars=max_content_chars,
                            max_tokens=max_tokens,
                            thinking_enabled=thinking_enabled,
                            reasoning_effort=thinking_effort,
                            break_on_gap=True,
                        )
                    )
                else:
                    rebuilt.append(queued_batch)
            if changed:
                q.clear()
                q.extend(rebuilt)

        def _maybe_shrink(target_cap: int) -> None:
            nonlocal runtime_cap
            clamped = max(1, min(runtime_cap, int(target_cap)))
            if clamped < runtime_cap:
                logger.warning(f"触发自适应缩批：单批上限 {runtime_cap} → {clamped} 章，重排待跑队列")
                runtime_cap = clamped
                _repack_queue_to_cap(warmup_queue, runtime_cap)
                _repack_queue_to_cap(queue, runtime_cap)

        # 6a. 串行预热：先跑少量批，建立初始 KB
        if not skip_warmup and warmup_batches_count > 0:
            logger.info(f"串行预热前{warmup_batches_count}批，建立初始知识库...")
            while warmup_queue and not self._stop_requested and not self._account_tripped:
                batch_chs = warmup_queue.popleft()
                ch_range = _fmt_batch_range(batch_chs)
                await self._emit({
                    "chapter": int(batch_chs[0]),
                    "status": PipelineStatus.START,
                    "progress": len(done_chapters),
                    "total": total,
                    "range": ch_range,
                    "block_size": len(batch_chs),
                    "message": f"[预热] 开始分析{ch_range}...",
                })
                try:
                    outcome, elapsed = await self._execute_one_batch(
                        analyzer, self.file_processor, state, batch_chs
                    )
                except Exception as e:
                    logger.error(f"[预热] 批{ch_range}分析异常: {e}", exc_info=True)
                    outcome = BatchAnalysisResult(
                        valid_results={},
                        missing_chapters=list(batch_chs),
                        tokens=(0, 0),
                        retry_info={
                            "retries": 0, "failed_tokens": 0,
                            "usage_status": "UNKNOWN", "error": f"批分析异常: {e}",
                        },
                    )
                    elapsed = 0.0
                followups, new_chs = await self._process_batch_outcome(
                    batch_chs, outcome, elapsed,
                    state=state, done_chapters=done_chapters,
                    valid_chapter_ids=valid_chapter_ids, total=total, label="预热",
                    on_shrink=_maybe_shrink,
                )
                chapters_since_checkpoint += len(new_chs)
                if state.should_checkpoint(chapters_since_checkpoint):
                    await state.flush_to_disk(output_dir)
                    chapters_since_checkpoint = 0
                if followups and not self._stop_requested and not self._account_tripped:
                    warmup_queue.extendleft(reversed(followups))

        # 6b. 动态并发池（deque + asyncio.wait(FIRST_COMPLETED)）
        if skip_warmup:
            logger.info(f"已有{restored}章结果，跳过预热，剩余{len(queue)}批直接并发（并发数={concurrency}）")
        else:
            logger.info(f"预热完成，剩余{len(queue)}批切换为流式并发模式（并发数={concurrency}）")

        rolling_client = LLMClient(self.config.api)
        self._rolling_client = rolling_client
        rolling_last_chapter = 0
        if isinstance(state.rolling, dict):
            rolling_last_chapter = state.rolling.get("last_updated_chapter", 0)
        if rolling_last_chapter == 0:
            if done_chapters:
                rolling_last_chapter = max(done_chapters)
            elif state._flushed_chapters:
                rolling_last_chapter = max(state._flushed_chapters)

        _rolling_done = asyncio.Event()
        _rolling_done.set()
        _completed_since_rolling = 0
        raw_interval = int(getattr(self.config.analysis, "rolling_update_interval", 0) or 0)
        _rolling_batch_size = max(1, raw_interval if raw_interval > 0 else concurrency)
        _rolling_state = {"last_chapter": rolling_last_chapter}

        async def _async_rolling(completed_chapter_ids):
            """后台 rolling 更新（asyncio.Task）"""
            try:
                new_last = await self._update_rolling_summary(
                    rolling_client, output_dir, _rolling_state["last_chapter"],
                    completed_chapter_ids, state)
                if self._handle_rolling_breaker(rolling_client):
                    return
                _rolling_state["last_chapter"] = new_last
                await self._emit({
                    "chapter": 0, "status": PipelineStatus.ROLLING_UPDATED,
                    "progress": len(done_chapters), "total": total,
                    "message": f"全书主线摘要已更新（纳入到第{new_last}章）"
                })
            except Exception as e:
                self._handle_rolling_breaker(rolling_client)
                logger.error(f"后台 rolling 更新异常: {e}")
            finally:
                _rolling_done.set()

        async def _run_batch_task(batch_chs: list[int]) -> Tuple[BatchAnalysisResult, float]:
            if self._stop_requested:
                return (
                    BatchAnalysisResult(
                        valid_results={},
                        missing_chapters=list(batch_chs),
                        tokens=(0, 0),
                        retry_info={
                            "retries": 0, "failed_tokens": 0,
                            "usage_status": "KNOWN", "error": "用户请求停止",
                        },
                        stopped=True,
                    ),
                    0.0,
                )
            ch_range = _fmt_batch_range(batch_chs)
            await self._emit({
                "chapter": int(batch_chs[0]),
                "status": PipelineStatus.START,
                "progress": len(done_chapters),
                "total": total,
                "range": ch_range,
                "block_size": len(batch_chs),
                "message": f"开始分析{ch_range}...",
            })
            try:
                return await self._execute_one_batch(analyzer, self.file_processor, state, batch_chs)
            except Exception as e:
                logger.error(f"批{ch_range}分析异常: {e}", exc_info=True)
                return (
                    BatchAnalysisResult(
                        valid_results={},
                        missing_chapters=list(batch_chs),
                        tokens=(0, 0),
                        retry_info={
                            "retries": 0, "failed_tokens": 0,
                            "usage_status": "UNKNOWN", "error": f"批分析异常: {e}",
                        },
                    ),
                    0.0,
                )

        active: dict[asyncio.Task, list[int]] = {}
        _rolling_pending_ids: list[int] = []

        try:
            while (queue or active) and not self._stop_requested and not self._account_tripped:
                while queue and len(active) < concurrency and not self._stop_requested and not self._account_tripped:
                    nxt = queue.popleft()
                    task = asyncio.create_task(_run_batch_task(nxt))
                    active[task] = nxt
                if not active:
                    break

                done_tasks, _ = await asyncio.wait(
                    active.keys(), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done_tasks:
                    batch_chs = active.pop(task)
                    try:
                        outcome, elapsed = await task
                    except asyncio.CancelledError:
                        continue
                    except Exception as e:
                        error_msg = f"批{_fmt_batch_range(batch_chs)}任务异常: {e}"
                        logger.error(error_msg, exc_info=True)
                        outcome = BatchAnalysisResult(
                            valid_results={},
                            missing_chapters=list(batch_chs),
                            tokens=(0, 0),
                            retry_info={
                                "retries": 0, "failed_tokens": 0,
                                "usage_status": "UNKNOWN", "error": error_msg,
                            },
                        )
                        elapsed = 0.0

                    followups, new_chs = await self._process_batch_outcome(
                        batch_chs, outcome, elapsed,
                        state=state, done_chapters=done_chapters,
                        valid_chapter_ids=valid_chapter_ids, total=total, label="",
                        on_shrink=_maybe_shrink,
                    )
                    if followups and not self._stop_requested and not self._account_tripped:
                        queue.extendleft(reversed(followups))

                    if new_chs:
                        _rolling_pending_ids.extend(new_chs)
                        _completed_since_rolling += 1
                        if effective_update_rolling and _completed_since_rolling >= _rolling_batch_size and _rolling_pending_ids:
                            await _rolling_done.wait()
                            _rolling_done.clear()
                            captured_ids = list(_rolling_pending_ids)
                            asyncio.create_task(_async_rolling(captured_ids))
                            _rolling_pending_ids.clear()
                            _completed_since_rolling = 0
                            await self._emit({
                                "chapter": 0,
                                "status": PipelineStatus.ROLLING_STARTED,
                                "progress": len(done_chapters),
                                "total": total,
                                "message": f"全书主线摘要更新中（后台，已完成{len(done_chapters)}章）...",
                            })

                    chapters_since_checkpoint += len(new_chs)
                    if state.should_checkpoint(chapters_since_checkpoint):
                        await state.flush_to_disk(output_dir)
                        chapters_since_checkpoint = 0

                    if self._stop_requested or self._account_tripped:
                        break
        finally:
            for t in list(active.keys()):
                if not t.done():
                    t.cancel()
            if active:
                await asyncio.gather(*active.keys(), return_exceptions=True)
            await _rolling_done.wait()

        # 7c. 失败章自动补跑（每轮按减半批上限重新装包串行补跑）
        _actually_retried_succeeded: set[int] = set()
        retry_rounds = max(0, int(getattr(self.config.analysis, "failed_block_retry_rounds", 3)))
        retry_cap = max(
            1,
            min(
                runtime_cap,
                effective_batch_cap(
                    block_size,
                    max_tokens,
                    thinking_enabled,
                    reasoning_effort=thinking_effort,
                ),
            ),
        )
        for retry_round in range(retry_rounds):
            if self._stop_requested or self._account_tripped:
                break
            failed_chs = [
                ch for ch in chapters_to_analyze
                if ch in state._failed_chapters
                and ch not in done_chapters
                and ch not in state._skipped_chapters
            ]
            if not failed_chs:
                break
            failed_chs = self._retryable_failed_blocks(failed_chs)
            if not failed_chs:
                logger.warning("补跑终止：失败章节全部为致命错误（API 余额不足/认证失败等），不再补跑")
                break
            retry_cap = max(1, retry_cap // 2)
            retry_lengths: dict[int, int] = {}
            for ch in failed_chs:
                if ch in chapter_lengths:
                    retry_lengths[ch] = chapter_lengths[ch]
                else:
                    txt = await asyncio.to_thread(self.file_processor.read_chapter, ch)
                    retry_lengths[ch] = len(txt) if txt is not None else 0
            retry_batches = pack_chapter_batches(
                failed_chs,
                retry_lengths,
                block_size=retry_cap,
                max_content_chars=max_content_chars,
                max_tokens=max_tokens,
                thinking_enabled=thinking_enabled,
                reasoning_effort=thinking_effort,
                break_on_gap=True,
            )
            logger.info(
                f"补跑第{retry_round + 1}轮: {len(failed_chs)}章 → {len(retry_batches)}批"
                f"（单批上限={retry_cap}章）"
            )
            retry_queue = deque(retry_batches)
            while retry_queue and not self._stop_requested and not self._account_tripped:
                batch_chs = retry_queue.popleft()
                for ch in batch_chs:
                    state._failed_chapters.pop(ch, None)
                ch_range = _fmt_batch_range(batch_chs)
                await self._emit({
                    "chapter": int(batch_chs[0]),
                    "status": PipelineStatus.START,
                    "progress": len(done_chapters),
                    "total": total,
                    "range": ch_range,
                    "block_size": len(batch_chs),
                    "message": f"[补跑] 开始分析{ch_range}...",
                })
                try:
                    outcome, elapsed = await self._execute_one_batch(
                        analyzer, self.file_processor, state, batch_chs
                    )
                except Exception as e:
                    logger.error(f"[补跑] 批{ch_range}分析异常: {e}", exc_info=True)
                    outcome = BatchAnalysisResult(
                        valid_results={},
                        missing_chapters=list(batch_chs),
                        tokens=(0, 0),
                        retry_info={
                            "retries": 0, "failed_tokens": 0,
                            "usage_status": "UNKNOWN", "error": f"批分析异常: {e}",
                        },
                    )
                    elapsed = 0.0
                followups, new_chs = await self._process_batch_outcome(
                    batch_chs, outcome, elapsed,
                    state=state, done_chapters=done_chapters,
                    valid_chapter_ids=valid_chapter_ids, total=total, label="补跑",
                )
                if new_chs:
                    _actually_retried_succeeded.update(new_chs)
                chapters_since_checkpoint += len(new_chs)
                if state.should_checkpoint(chapters_since_checkpoint):
                    await state.flush_to_disk(output_dir)
                    chapters_since_checkpoint = 0
                if followups and not self._stop_requested and not self._account_tripped:
                    retry_queue.extendleft(reversed(followups))

        # 补跑完成后更新 rolling summary（含尚未纳入的新增尾章与低章号补跑章）
        if effective_update_rolling and not self._stop_requested:
            retried_chapter_ids = sorted(
                {
                    ch for ch in chapters_to_analyze
                    if ch > _rolling_state["last_chapter"] and ch in state.results
                }
                | _actually_retried_succeeded
            )
            if retried_chapter_ids:
                logger.info(f"补跑完成后更新 rolling summary: {len(retried_chapter_ids)}章")
                await self._safe_post_retry_rolling_update(
                    rolling_client, output_dir, _rolling_state,
                    retried_chapter_ids, state, len(done_chapters), total)

        # 8. 收集成功/失败信息（仅统计当前范围内的章节）
        await state.flush_to_disk(output_dir)

        result_count = (
            len(done_chapters)
            if self.force_rerun
            else len([ch for ch in state.results if ch in valid_chapter_ids])
        )
        failed_chapters = state.failed_chapters_in(valid_chapter_ids)
        skipped_chapters = [
            f"{c}({r})" for c, r in state._skipped_chapters.items()
            if c in valid_chapter_ids
        ]
        total_analyzed = result_count

        if failed_chapters:
            await self._emit({
                "chapter": 0,
                "status": PipelineStatus.FAILED_SUMMARY,
                "progress": total_analyzed,
                "total": total,
                "message": f"失败章节: {', '.join(str(c) for c in failed_chapters)}"
            })

        # 9. 脚本合并所有单章结果生成最终 KB（纯 CPU/IO，走线程池）
        logger.info("开始脚本合并所有章节结果...")
        await asyncio.to_thread(KnowledgeBaseManager.invalidate_cache, output_dir)
        final_kb = await asyncio.to_thread(
            KnowledgeBaseManager.merge_results,
            output_dir,
            kb_limits=kb_limits,
            single_chapter_only=True,
        )
        await asyncio.to_thread(KnowledgeBaseManager(self.knowledge_file).save, final_kb)
        logger.info(f"最终知识库已保存: {self.knowledge_file}")

        # 10. 使用全量结果生成汇总报告
        all_results = sorted(state.results.values(), key=lambda r: r.chapter_number)
        if all_results:
            try:
                summary_path = await asyncio.to_thread(
                    export_summary_report, all_results, output_dir)
                logger.info(f"汇总报告已生成: {summary_path}")
            except Exception as e:
                logger.warning(f"生成汇总报告失败: {e}")

        # 11. 完成前释放章节内容缓存
        try:
            self.file_processor.clear_cache()
        except Exception:
            pass

        # 12. 完成
        logger.info(f"分析任务完成，成功{total_analyzed}章，失败{len(failed_chapters)}章"
                    f"{f'，内容审核拦截跳过{len(skipped_chapters)}章' if skipped_chapters else ''}")
        return {
            "stopped": self._stop_requested,
            "total_analyzed": total_analyzed,
            "errors": [f"失败{len(failed_chapters)}章"] if failed_chapters else [],
            "failed_chapters": failed_chapters,
            "skipped_chapters": skipped_chapters,
            "account_tripped": self._account_tripped,
            "account_trip_reason": self._account_trip_reason,
        }

    def inflight_blocks(self) -> list:
        """当前真实在途批快照（LLM 调用窗口内，天然 ≤ concurrency 且零丢失）"""
        return sorted(self._inflight_blocks.values(), key=lambda d: d["started_at"])

    async def _execute_one_batch(
        self,
        analyzer: NovelAnalyzer,
        file_processor: FileProcessor,
        state: MemoryState,
        batch_chs: Sequence[int],
    ) -> Tuple[BatchAnalysisResult, float]:
        """执行单批逐章槽位分析（预热/并发/补跑共用）。"""
        chs = [int(c) for c in batch_chs]
        lead_ch = chs[0]
        ch_range = _fmt_batch_range(chs)
        self._inflight_blocks[lead_ch] = {
            "chapter": lead_ch,
            "range": ch_range,
            "started_at": time.time(),
        }
        t_start = time.time()
        try:
            chapter_items = await asyncio.to_thread(file_processor.read_chapters_batch, chs)
            present_ids = {int(c) for c, _ in chapter_items}
            unreadable = [c for c in chs if c not in present_ids]
            if not chapter_items:
                return (
                    BatchAnalysisResult(
                        valid_results={},
                        missing_chapters=chs,
                        tokens=(0, 0),
                        retry_info={
                            "retries": 0, "failed_tokens": 0,
                            "usage_status": "KNOWN", "error": "读取失败",
                        },
                    ),
                    time.time() - t_start,
                )
            # 严禁将本章（>= lead_ch）的旧结果注入自身 Prompt，防止定点重跑自我泄漏
            temp_kb = await asyncio.to_thread(state.get_kb_snapshot, chapter_limit=lead_ch - 1)
            outcome = await analyzer.analyze_batch(
                chapter_items,
                temp_kb,
                book_id=str(self.directory) if self.directory else "",
            )
            if unreadable:
                missing = list(outcome.missing_chapters)
                for u in unreadable:
                    if u not in missing:
                        missing.append(u)
                outcome.missing_chapters = missing
            return outcome, time.time() - t_start
        finally:
            self._inflight_blocks.pop(lead_ch, None)

    async def _process_batch_outcome(
        self,
        batch_chs: Sequence[int],
        outcome: BatchAnalysisResult,
        elapsed: float,
        *,
        state: MemoryState,
        done_chapters: set[int],
        valid_chapter_ids: set[int],
        total: int,
        label: str = "",
        on_shrink: Optional[Callable[[int], None]] = None,
    ) -> Tuple[List[List[int]], List[int]]:
        """处理批量调用结果：按升序逐章并入 state、精准闭道、按错误分类拆批或标失败。

        Returns:
            ``(followup_batches, newly_succeeded_chapters)``
        """
        chs = [int(c) for c in batch_chs]
        lead_ch = chs[0]
        ch_range = _fmt_batch_range(chs)
        prefix = f"[{label}] " if label else ""

        valid_chs = sorted(outcome.valid_results.keys())
        merged_dicts: List[dict] = []
        for ch in valid_chs:
            res = outcome.valid_results[ch]
            res.chapter_number = ch
            res.block_size = 1
            res.source_chapters = [ch]
            await state.add_result(res)
            if ch in valid_chapter_ids:
                done_chapters.add(ch)
            d = res.to_dict()
            if isinstance(d, dict):
                d.pop("raw_response", None)
            merged_dicts.append(d)
            res.raw_response = ""

        retry_info = outcome.retry_info or {"retries": 0, "failed_tokens": 0}
        usage_status = _usage_status_of(retry_info)
        in_tok, out_tok = (
            outcome.tokens
            if isinstance(outcome.tokens, tuple) and len(outcome.tokens) == 2
            else (0, 0)
        )
        completed_count = len(done_chapters)
        missing = [c for c in outcome.missing_chapters if c not in outcome.valid_results]

        async def _emit_chapter_tokens() -> None:
            await self._emit_tokens({
                "category": "chapter",
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "current_tokens": in_tok + out_tok,
                "usage_status": usage_status,
                "model": self.config.api.model,
                "cached_tokens": retry_info.get("cached_tokens", 0),
            })

        # 1. 账户级熔断最高优先级：立即止损停线并闭合前端车道
        if outcome.account_tripped:
            reason_text = (outcome.account_trip_reason or "").strip() or "账户级永久错误（认证失败或余额不足）"
            self._account_tripped = True
            if not self._account_trip_reason:
                self._account_trip_reason = reason_text
            self.stop()
            logger.error(f"批{ch_range}触发账户级熔断，已立即停止流水线: {reason_text}")
            await self._emit({
                "chapter": lead_ch,
                "status": PipelineStatus.FAILED,
                "results": merged_dicts,
                "progress": completed_count,
                "total": total,
                "range": ch_range,
                "block_size": len(chs),
                "chapters_count": len(valid_chs),
                "retries": retry_info.get("retries", 0),
                "failed_tokens": retry_info.get("failed_tokens", 0),
                "message": f"{prefix}{ch_range}触发账户级熔断",
            })
            await _emit_chapter_tokens()
            return [], valid_chs

        # 2. 用户请求停止：不写失败/跳过、不拆批，闭合车道后退出
        err_text = str(retry_info.get("error") or "")
        if outcome.stopped or self._stop_requested or err_text == "用户请求停止":
            logger.info(f"批{ch_range}因停止中止，不写入失败标记")
            await self._emit({
                "chapter": lead_ch,
                "status": PipelineStatus.FAILED,
                "results": merged_dicts,
                "progress": completed_count,
                "total": total,
                "range": ch_range,
                "block_size": len(chs),
                "chapters_count": len(valid_chs),
                "retries": retry_info.get("retries", 0),
                "failed_tokens": retry_info.get("failed_tokens", 0),
                "message": f"{prefix}{ch_range}因停止中止",
            })
            if in_tok or out_tok or usage_status == "UNKNOWN":
                await _emit_chapter_tokens()
            return [], valid_chs

        # 3. 整批全部成功
        if not missing:
            await self._emit({
                "chapter": lead_ch,
                "status": PipelineStatus.DONE,
                "result": merged_dicts[-1] if merged_dicts else None,
                "results": merged_dicts,
                "progress": completed_count,
                "total": total,
                "elapsed": elapsed,
                "range": ch_range,
                "block_size": len(chs),
                "chapters_count": len(valid_chs),
                "tokens": in_tok + out_tok,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "retries": retry_info.get("retries", 0),
                "failed_tokens": retry_info.get("failed_tokens", 0),
                "usage_status": usage_status,
                "message": f"{prefix}{ch_range}分析完成",
            })
            await _emit_chapter_tokens()
            return [], valid_chs

        # 4. 部分成功：保留已完成章，仅把缺失章组成小批追回
        if len(chs) > 1 and len(valid_chs) > 0:
            if outcome.truncated and on_shrink is not None:
                on_shrink(max(1, len(valid_chs)))
            await self._emit({
                "chapter": lead_ch,
                "status": PipelineStatus.DONE,
                "result": merged_dicts[-1],
                "results": merged_dicts,
                "progress": completed_count,
                "total": total,
                "elapsed": elapsed,
                "range": ch_range,
                "block_size": len(chs),
                "chapters_count": len(valid_chs),
                "tokens": in_tok + out_tok,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "retries": retry_info.get("retries", 0),
                "failed_tokens": retry_info.get("failed_tokens", 0),
                "usage_status": usage_status,
                "message": f"{prefix}{ch_range}部分完成（{len(valid_chs)}/{len(chs)}章），缺失{missing}待补",
            })
            await _emit_chapter_tokens()
            return [missing], valid_chs

        # 5. 全失败分类（审核 / 上下文超长 / 非法参数 / 致命故障）
        verdict_dict = retry_info.get("verdict")
        structured = ErrorVerdict.from_dict(verdict_dict) if isinstance(verdict_dict, dict) else None
        text_verdict = classify_message(err_text) if structured is None else None

        is_mod = (
            is_moderation_error(err_text)
            or is_moderation_message(err_text)
            or (
                structured is not None
                and structured.cls in (VerdictErrorClass.MODERATION_INPUT, VerdictErrorClass.MODERATION_OUTPUT)
            )
            or (
                text_verdict is not None
                and text_verdict.cls in (ErrorClass.MODERATION_INPUT, ErrorClass.MODERATION_OUTPUT)
            )
        )
        is_ctx_len = (
            (structured is not None and structured.cls == VerdictErrorClass.CONTEXT_LENGTH)
            or (text_verdict is not None and text_verdict.cls == ErrorClass.CONTEXT_LENGTH)
        )
        is_bad_req = (
            (structured is not None and structured.cls == VerdictErrorClass.BAD_REQUEST)
            or (text_verdict is not None and text_verdict.cls == ErrorClass.BAD_REQUEST)
        )
        is_timeout = (
            (structured is not None and structured.cls == VerdictErrorClass.TIMEOUT)
            or (text_verdict is not None and text_verdict.cls == ErrorClass.TIMEOUT)
        )

        fatal_val = None
        if structured is not None:
            if structured.is_permanent:
                fatal_val = structured.cls.value
        elif text_verdict is not None:
            if (
                text_verdict.cls is not ErrorClass.UNKNOWN
                and text_verdict.is_permanent()
                and text_verdict.cls not in (ErrorClass.MODERATION_INPUT, ErrorClass.MODERATION_OUTPUT)
            ):
                fatal_val = text_verdict.cls.value
        if fatal_val in (
            VerdictErrorClass.MODERATION_INPUT.value,
            VerdictErrorClass.MODERATION_OUTPUT.value,
        ):
            fatal_val = None

        # 5a. 多章整批零成果且因 审核 / 上下文超长 / 截断 / 超时 失败 → 二分拆小批隔离，不提前写失败/跳过标记
        if len(chs) > 1 and (is_mod or is_ctx_len or outcome.truncated or is_timeout) and not is_bad_req:
            mid = len(chs) // 2
            followups = [chs[:mid], chs[mid:]]
            if (outcome.truncated or is_timeout or is_ctx_len) and on_shrink is not None:
                on_shrink(max(1, mid))
            logger.warning(
                f"{prefix}{ch_range}触发拆分重试（mod={is_mod}, ctx={is_ctx_len}, trunc={outcome.truncated}, timeout={is_timeout}）→ {followups}"
            )
            await self._emit({
                "chapter": lead_ch,
                "status": PipelineStatus.FAILED,
                "results": [],
                "progress": completed_count,
                "total": total,
                "range": ch_range,
                "block_size": len(chs),
                "chapters_count": 0,
                "retries": retry_info.get("retries", 0),
                "failed_tokens": retry_info.get("failed_tokens", 0),
                "message": f"{prefix}{ch_range}已拆分为两小批重试",
            })
            await _emit_chapter_tokens()
            return followups, []

        # 5b. 多章整批不可拆失败：逐章记入 failed 供后续减半重装包补跑
        if len(chs) > 1:
            reason = "读取失败" if "读取失败" in err_text else "分析失败（LLM响应解析失败或重试耗尽）"
            for ch in missing:
                if fatal_val is not None:
                    self._fatal_blocks[ch] = fatal_val
                state.add_failed(ch, reason)
            logger.warning(f"{prefix}批{ch_range}分析失败: {reason}，耗时{elapsed:.1f}s，tokens={outcome.tokens}")
            await self._emit({
                "chapter": lead_ch,
                "status": PipelineStatus.FAILED,
                "results": [],
                "progress": completed_count,
                "total": total,
                "range": ch_range,
                "block_size": len(chs),
                "chapters_count": 0,
                "retries": retry_info.get("retries", 0),
                "failed_tokens": retry_info.get("failed_tokens", 0),
                "message": f"{prefix}{ch_range}分析失败",
            })
            await _emit_chapter_tokens()
            return [], []

        # 6. 单章失败（已不可再拆）
        ch = lead_ch
        if self.config.analysis.skip_moderation_blocked and is_mod:
            state.add_skipped(ch, "内容审核拦截")
            logger.warning(f"第{ch}章内容审核拦截，跳过")
            await self._emit({
                "chapter": ch,
                "status": PipelineStatus.SKIPPED,
                "progress": completed_count,
                "total": total,
                "range": ch_range,
                "block_size": 1,
                "chapters_count": 0,
                "message": f"{prefix}{ch_range} 内容审核拦截，已跳过",
            })
            if in_tok or out_tok:
                await _emit_chapter_tokens()
            return [], []

        if fatal_val is not None:
            self._fatal_blocks[ch] = fatal_val
        reason = "读取失败" if "读取失败" in err_text else "分析失败（LLM响应解析失败或重试耗尽）"
        state.add_failed(ch, reason)
        logger.warning(f"第{ch}章分析失败: {reason}，耗时{elapsed:.1f}s，tokens={outcome.tokens}")
        await self._emit({
            "chapter": ch,
            "status": PipelineStatus.FAILED,
            "progress": completed_count,
            "total": total,
            "range": ch_range,
            "block_size": 1,
            "chapters_count": 0,
            "retries": retry_info.get("retries", 0),
            "failed_tokens": retry_info.get("failed_tokens", 0),
            "message": f"{prefix}{ch_range}分析失败",
        })
        await _emit_chapter_tokens()
        return [], []

    async def _emit_rolling_tokens(self, tokens, model: str = "", cached_tokens: int = 0):
        """rolling 调用 token 统计（asyncio 内直接 emit，无需线程缓冲）"""
        if not tokens or not isinstance(tokens, tuple) or len(tokens) < 2:
            return
        await self._emit_tokens({
            "category": "rolling",
            "input_tokens": int(tokens[0] or 0),
            "output_tokens": int(tokens[1] or 0),
            "model": model,
            "cached_tokens": cached_tokens,
        })

    async def _update_rolling_summary(self, rolling_client: LLMClient,
                                      output_dir: Path, last_chapter: int,
                                      batch_block_ids: list = None,
                                      state: MemoryState = None) -> int:
        """
        批末更新结构化滚动总结（JSON 增量方案）：
        - 首次跨过 ROLLING_EARLY_CHAPTERS 时生成初始 JSON
        - 之后每批增量更新，程序侧硬截断 + 归档
        """
        if self._stop_requested:
            return last_chapter

        # 1. 读取本批新完成的 result（从内存 state.results）
        new_results = []
        max_chapter = last_chapter
        if batch_block_ids and state:
            for block_id in sorted(batch_block_ids):
                if block_id not in state.results:
                    continue
                try:
                    data = state.results[block_id].to_dict()
                    new_results.append(data)
                    max_chapter = max(max_chapter, block_id)
                except Exception as e:
                    logger.warning(f"rolling 更新：获取 block {block_id} 失败: {e}")
        elif state:
            # 降级：全量扫描（首次生成或补跑时使用）
            for ch in sorted(state.results.keys()):
                if ch <= last_chapter:
                    continue
                try:
                    data = state.results[ch].to_dict()
                    new_results.append(data)
                    max_chapter = max(max_chapter, ch)
                except Exception as e:
                    logger.warning(f"rolling 更新：获取章节 {ch} 失败: {e}")

        if not new_results:
            logger.debug("rolling 更新：本批无新章，跳过")
            return last_chapter

        # 2. 读旧 rolling 数据（从内存，加锁防止并发写入）
        if state:
            async with state._rolling_lock:
                rolling_data = dict(state.rolling) if isinstance(state.rolling, dict) else {}
        else:
            rolling_data = {}

        # 3. 首次生成（跨过 self._rolling_early 边界；阈值按书长自适应，见 run()）
        has_structured = bool(rolling_data.get("rolling_structured"))
        if not has_structured and max_chapter >= self._rolling_early:
            await self._generate_early_summary(rolling_client, output_dir, rolling_data, state)
            if self._handle_rolling_breaker(rolling_client):
                return last_chapter

        # 3a. 检测旧格式降级（_legacy_text），触发重新初始化
        rs = rolling_data.get("rolling_structured") or {}
        if rs.get("_legacy_text"):
            logger.info("检测到旧格式降级，触发重新初始化...")
            await self._reinitialize_structured_rolling(rolling_client, output_dir, rolling_data, state)
            if self._handle_rolling_breaker(rolling_client):
                return last_chapter

        # 4. 增量更新结构化 JSON
        old_structured = rolling_data.get("rolling_structured", {})
        new_chapters_text = self._format_chapters_for_rolling(new_results)
        if batch_block_ids and any(bid <= last_chapter for bid in batch_block_ids):
            new_chapters_text = (
                "【注：本批含历史补跑章节，仅补充里程碑与因果链，不要将 current_context 倒退为历史章进度】\n\n"
                + new_chapters_text
            )

        messages = [
            {"role": "system", "content": STRUCTURED_ROLLING_SYSTEM_PROMPT},
            {"role": "user", "content": STRUCTURED_ROLLING_USER_UPDATE_TEMPLATE.format(
                current_data=json.dumps(old_structured, ensure_ascii=False, indent=2) if old_structured else "{}",
                new_chapters=new_chapters_text
            )}
        ]

        rolling_retries = max(0, int(getattr(self.config.analysis, "rolling_summary_max_retries", ROLLING_SUMMARY_MAX_RETRIES)))
        rolling_temp = float(getattr(self.config.analysis, "rolling_summary_temperature", ROLLING_SUMMARY_TEMPERATURE))
        raw_rolling_max_tok = int(getattr(self.config.analysis, "rolling_summary_max_tokens", 0) or 0)
        rolling_max_tok = raw_rolling_max_tok if raw_rolling_max_tok > 0 else ROLLING_SUMMARY_MAX_TOKENS

        new_json = None
        for attempt in range(1 + rolling_retries):
            if self._stop_requested:
                return last_chapter
            llm_result = await rolling_client.chat(
                messages,
                temperature=rolling_temp,
                max_tokens=rolling_max_tok
            )
            await self._emit_rolling_tokens(
                llm_result.tokens,
                model=self.config.api.model,
                cached_tokens=getattr(llm_result, "cached_tokens", 0) or 0,
            )
            if self._handle_rolling_breaker(rolling_client):
                return last_chapter
            if llm_result.success and llm_result.content and llm_result.content.strip():
                raw = extract_json_from_text(llm_result.content)
                if raw:
                    parsed = safe_parse_json(raw)
                    if parsed:
                        new_json = _validate_and_fill_rolling_schema(parsed)
                        break
                logger.warning(f"rolling JSON 解析失败（第{attempt + 1}次），重试...")
            else:
                logger.warning(f"rolling 更新第{attempt + 1}次调用失败: {llm_result.error}")

        if new_json is None:
            logger.warning(f"rolling 更新全部失败，保留旧数据（已纳入到第{last_chapter}章）")
            return last_chapter

        # 5. 程序侧硬截断 + 归档
        # 5a. paradigm_layers 截断
        max_paradigms = max(1, int(getattr(self.config.analysis, "rolling_max_paradigm_layers", 5) or 5))
        pl = new_json.get("paradigm_layers", [])
        if len(pl) > max_paradigms:
            new_json["paradigm_layers"] = pl[-max_paradigms:]

        # 5b. recent_momentum 归档检测
        archive_trigger = max(1, int(getattr(self.config.analysis, "rolling_archive_trigger_count", ROLLING_ARCHIVE_TRIGGER_COUNT) or ROLLING_ARCHIVE_TRIGGER_COUNT))
        momentum_win = max(1, int(getattr(self.config.analysis, "rolling_momentum_window", ROLLING_MOMENTUM_WINDOW) or ROLLING_MOMENTUM_WINDOW))
        momentum = new_json.get("recent_momentum", [])
        should_archive = False
        if len(momentum) >= archive_trigger:
            should_archive = True
            logger.info(f"recent_momentum 条目数({len(momentum)})达到归档触发阈值({archive_trigger})")
        else:
            # 跨度检测：提取首尾章号
            first_ch = _extract_chapter_number(momentum[0]) if momentum else None
            last_ch = _extract_chapter_number(momentum[-1]) if momentum else None
            if first_ch and last_ch and (last_ch - first_ch) >= momentum_win:
                should_archive = True
                logger.info(f"recent_momentum 跨度({first_ch}-{last_ch})超过归档窗口({momentum_win})")

        if should_archive and len(momentum) >= 3:
            try:
                await self._archive_momentum_to_milestone(rolling_client, new_json, momentum)
            except Exception as e:
                logger.warning(f"归档失败（不丢数据，下批再试）: {e}")

        # 5c. 硬截断 recent_momentum
        max_momentum = self.config.analysis.rolling_max_momentum
        if len(new_json.get("recent_momentum", [])) > max_momentum:
            new_json["recent_momentum"] = new_json["recent_momentum"][-max_momentum:]

        # 5d. 硬截断 global_milestones（锁定里程碑不占名额）
        max_milestones = self.config.analysis.rolling_max_milestones
        milestones = new_json.get("global_milestones", [])
        locked_indices = _find_locked_milestones(milestones, new_json.get("paradigm_layers", []))
        non_locked_count = len(milestones) - len(locked_indices)
        if non_locked_count > max_milestones:
            # 超出：从最旧的非锁定条目开始淘汰
            to_remove = non_locked_count - max_milestones
            new_milestones = []
            removed = 0
            for i, m in enumerate(milestones):
                if removed < to_remove and i not in locked_indices:
                    removed += 1
                else:
                    new_milestones.append(m)
            new_json["global_milestones"] = new_milestones

        # 5e. 硬截断 active_causal_chains
        max_chains = max(1, int(getattr(self.config.analysis, "rolling_max_causal_chains", 5) or 5))
        if len(new_json.get("active_causal_chains", [])) > max_chains:
            new_json["active_causal_chains"] = new_json["active_causal_chains"][-max_chains:]

        # 5f. 里程碑单条长度截断（>50字时截断到最近逗号）
        # 容错：LLM 可能返回 dict 而非 str 的里程碑条目，非 str 不截断（避免崩溃/丢数据）
        for i, m in enumerate(new_json.get("global_milestones", [])):
            if isinstance(m, str) and len(m) > 50:
                cut = m[:50].rfind('，')
                if cut > 20:
                    new_json["global_milestones"][i] = m[:cut]
                else:
                    new_json["global_milestones"][i] = m[:50]

        # 6. 存入内存（实际落盘由 checkpoint 控制）
        rolling_data["rolling_structured"] = new_json
        rolling_data["last_updated_chapter"] = max_chapter
        rolling_data["last_updated_time"] = datetime.now().isoformat()

        if state:
            async with state._rolling_lock:
                state.rolling.update(rolling_data)

        logger.info(f"结构化滚动总结已更新（纳入到第{max_chapter}章，"
                    f"里程碑{len(new_json.get('global_milestones', []))}条，"
                    f"因果链{len(new_json.get('active_causal_chains', []))}条，"
                    f"势头条目{len(new_json.get('recent_momentum', []))}条）")
        return max_chapter

    def _handle_rolling_breaker(self, rolling_client: LLMClient) -> bool:
        """若 rolling 客户端触发账户级熔断，记录原因并停止流水线。"""
        if getattr(rolling_client, "breaker_tripped", False) is not True:
            return False
        raw_reason = getattr(rolling_client, "breaker_reason", "")
        reason_text = (
            raw_reason.strip()
            if isinstance(raw_reason, str) and raw_reason.strip()
            else "滚动总结触发账户级永久错误"
        )
        self._account_tripped = True
        if not self._account_trip_reason:
            self._account_trip_reason = reason_text
        self.stop()
        logger.error(f"滚动总结触发账户级熔断，已停止流水线: {reason_text}")
        return True

    async def _safe_post_retry_rolling_update(self, rolling_client, output_dir,
                                              _rolling_state, retried_block_ids,
                                              state, completed_count, total) -> None:
        """补跑完成后的同步 rolling 更新。

        裸 await 会让畸形 rolling schema 的 AttributeError 直接冲出 run()，
        最终 flush / 最终 KB 合并 / 汇总报告全部跳过，整场已成功的分析被
        service 层标记为 failed。此处失败只告警，不影响收尾。"""
        try:
            new_last = await self._update_rolling_summary(
                rolling_client, output_dir, _rolling_state["last_chapter"],
                retried_block_ids, state)
            if self._handle_rolling_breaker(rolling_client):
                return
            _rolling_state["last_chapter"] = new_last
            await self._emit({
                "chapter": 0, "status": PipelineStatus.ROLLING_UPDATED,
                "progress": completed_count, "total": total,
                "message": "[补跑后] 全书主线摘要已更新"
            })
        except Exception as e:
            self._handle_rolling_breaker(rolling_client)
            logger.error(f"补跑后 rolling 更新失败（不影响分析收尾落盘）: {e}", exc_info=True)

    async def _archive_momentum_to_milestone(self, rolling_client: LLMClient,
                                             structured_data: dict, momentum: list) -> None:
        """将近期势头条目压缩为一条里程碑，追加到 global_milestones，清空 recent_momentum"""
        existing_milestones = structured_data.get("global_milestones", [])
        momentum_text = "\n".join(str(m) for m in momentum)
        existing_text = "\n".join(str(m) for m in existing_milestones[-3:]) if existing_milestones else "（无）"

        messages = [
            {"role": "system", "content": "你是一个文本压缩器。将以下近期事件压缩为一条 ≤30 字的里程碑描述。格式：ch{起始}-{结束}: 概括。只输出这一行，不要其他内容。"},
            {"role": "user", "content": f"近期事件：\n{momentum_text}\n\n现有里程碑（最近3条）：\n{existing_text}\n\n请输出一条压缩后的里程碑："}
        ]

        # 与全链路其它 LLM 调用一致，走带温度退火 + 退避重试的 chat_with_retry，
        # 避免单次失败直接丢弃势头归档（归档是热点路径上的关键节点）。
        archive_max_tok = max(1, int(getattr(self.config.analysis, "rolling_archive_max_tokens", 100) or 100))
        if is_thinking_enabled(self.config.api):
            archive_max_tok = max(archive_max_tok, ROLLING_ARCHIVE_THINKING_MIN_TOKENS)
        llm_result = await rolling_client.chat_with_retry(
            messages, max_tokens=archive_max_tok
        )
        await self._emit_rolling_tokens(
            llm_result.tokens,
            model=self.config.api.model,
            cached_tokens=getattr(llm_result, "cached_tokens", 0) or 0,
        )
        if llm_result.success and llm_result.content and llm_result.content.strip():
            milestone = llm_result.content.strip().strip('"').strip("'")
            if len(milestone) > 60:
                milestone = milestone[:60]
            structured_data.setdefault("global_milestones", []).append(milestone)
            structured_data["recent_momentum"] = []
            logger.info(f"归档完成：{len(momentum)}条势头条目 → 里程碑「{milestone}」")
        else:
            logger.warning(f"归档 LLM 调用失败: {llm_result.error}，保留原 recent_momentum")

    async def _reinitialize_structured_rolling(self, rolling_client: LLMClient,
                                               output_dir: Path, rolling_data: dict,
                                               state: MemoryState = None) -> None:
        """检测到旧格式 _legacy_text 时，用新格式重新初始化"""
        legacy_text = rolling_data.get("rolling_structured", {}).get("_legacy_text", "")
        if not legacy_text:
            return

        # 从内存 state.results 提取轻量数据
        early_results = []
        if state:
            for ch in sorted(state.results.keys()):
                try:
                    early_results.append(state.results[ch].to_dict())
                except Exception:
                    pass
                if len(early_results) >= self._rolling_early:
                    break

        if not early_results:
            logger.warning("重新初始化：无可用 result 文件")
            return

        chapters_text = self._format_chapters_for_rolling_light(early_results)
        messages = [
            {"role": "system", "content": STRUCTURED_ROLLING_EARLY_PROMPT},
            {"role": "user", "content": STRUCTURED_ROLLING_USER_TEMPLATE.format(chapters=chapters_text)}
        ]

        rolling_temp = float(getattr(self.config.analysis, "rolling_summary_temperature", ROLLING_SUMMARY_TEMPERATURE))
        raw_rolling_max_tok = int(getattr(self.config.analysis, "rolling_summary_max_tokens", 0) or 0)
        rolling_max_tok = raw_rolling_max_tok if raw_rolling_max_tok > 0 else ROLLING_SUMMARY_MAX_TOKENS
        llm_result = await rolling_client.chat(
            messages, temperature=rolling_temp, max_tokens=rolling_max_tok
        )
        await self._emit_rolling_tokens(
            llm_result.tokens,
            model=self.config.api.model,
            cached_tokens=getattr(llm_result, "cached_tokens", 0) or 0,
        )

        if llm_result.success and llm_result.content and llm_result.content.strip():
            raw = extract_json_from_text(llm_result.content)
            if raw:
                parsed = safe_parse_json(raw)
                if parsed:
                    new_structured = _validate_and_fill_rolling_schema(parsed)
                    rolling_data["rolling_structured"] = new_structured
                    if state:
                        async with state._rolling_lock:
                            state.rolling.update(rolling_data)
                    logger.info(f"重新初始化完成：旧格式降级 → 结构化 JSON（{len(new_structured.get('global_milestones', []))}条里程碑）")
                    return

        logger.warning("重新初始化失败，保留 _legacy_text 降级格式")

    async def _generate_early_summary(self, rolling_client: LLMClient,
                                      output_dir: Path, rolling_data: dict,
                                      state: MemoryState = None) -> None:
        """首次跨过 ROLLING_EARLY_CHAPTERS 时，一次性生成初始结构化 JSON"""
        early_results = []
        if state:
            for ch in sorted(state.results.keys()):
                try:
                    early_results.append(state.results[ch].to_dict())
                except Exception as e:
                    logger.warning(f"早期摘要：获取章节 {ch} 失败: {e}")
                if len(early_results) >= self._rolling_early:
                    break

        if not early_results:
            logger.warning(f"早期摘要：第1-{self._rolling_early}章无 result 数据")
            return

        # 轻量预处理：只取 summary + world_building + unresolved_questions
        chapters_text = self._format_chapters_for_rolling_light(early_results)
        messages = [
            {"role": "system", "content": STRUCTURED_ROLLING_EARLY_PROMPT},
            {"role": "user", "content": STRUCTURED_ROLLING_USER_TEMPLATE.format(chapters=chapters_text)}
        ]

        rolling_temp = float(getattr(self.config.analysis, "rolling_summary_temperature", ROLLING_SUMMARY_TEMPERATURE))
        raw_rolling_max_tok = int(getattr(self.config.analysis, "rolling_summary_max_tokens", 0) or 0)
        rolling_max_tok = raw_rolling_max_tok if raw_rolling_max_tok > 0 else ROLLING_SUMMARY_MAX_TOKENS
        llm_result = await rolling_client.chat(
            messages,
            temperature=rolling_temp,
            max_tokens=rolling_max_tok
        )
        await self._emit_rolling_tokens(
            llm_result.tokens,
            model=self.config.api.model,
            cached_tokens=getattr(llm_result, "cached_tokens", 0) or 0,
        )

        if llm_result.success and llm_result.content and llm_result.content.strip():
            raw = extract_json_from_text(llm_result.content)
            if raw:
                parsed = safe_parse_json(raw)
                if parsed:
                    new_structured = _validate_and_fill_rolling_schema(parsed)
                    rolling_data["rolling_structured"] = new_structured
                    rolling_data["schema_version"] = 3
                    if state:
                        async with state._rolling_lock:
                            state.rolling.update(rolling_data)
                    logger.info(f"早期结构化摘要已生成（第1-{self._rolling_early}章，"
                                f"里程碑{len(new_structured.get('global_milestones', []))}条）")
                    return
            logger.warning(f"早期摘要 JSON 解析失败: {llm_result.content[:200]}")
        else:
            logger.warning(f"早期摘要生成失败: {llm_result.error}")

    def _format_chapters_for_rolling(self, results: list) -> str:
        """格式化章节信息用于 rolling prompt（完整版）"""
        lines = []
        for data in results:
            ch = data.get("chapter_number", 0)
            cb = data.get("cross_block", {})
            summary = cb.get("summary", "") if isinstance(cb, dict) else ""
            events = data.get("core_events", [])
            event_texts = [e.get("event", "") for e in events
                           if isinstance(e, dict) and e.get("event")]
            uk = data.get("updated_knowledge", {})
            world = uk.get("world_building", []) if isinstance(uk, dict) else []
            unresolved = cb.get("unresolved_questions", [])[:3] if isinstance(cb, dict) else []
            block = f"第{ch}章：{summary}"
            if event_texts:
                block += "\n核心事件：" + "；".join(event_texts)
            if world:
                block += "\n世界观：" + "；".join(str(w) for w in world[:3])
            if unresolved:
                block += "\n悬念：" + "；".join(str(q) for q in unresolved if len(str(q)) < 50)
            lines.append(block)
        return "\n\n".join(lines)

    def _format_chapters_for_rolling_light(self, results: list) -> str:
        """轻量版：只取 summary + world_building + unresolved_questions（降低 token 消耗）"""
        lines = []
        for data in results:
            ch = data.get("chapter_number", 0)
            cb = data.get("cross_block", {})
            summary = cb.get("summary", "") if isinstance(cb, dict) else ""
            uk = data.get("updated_knowledge", {})
            world = uk.get("world_building", [])
            unresolved = cb.get("unresolved_questions", [])[:3] if isinstance(cb, dict) else []
            block = f"第{ch}章：{summary}"
            if world:
                block += "\n世界观：" + "；".join(str(w) for w in world[:3])
            if unresolved:
                block += "\n悬念：" + "；".join(str(q) for q in unresolved if len(str(q)) < 50)
            lines.append(block)
        return "\n\n".join(lines)
