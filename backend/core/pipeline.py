"""
分析流水线（asyncio 版）
自旧项目 workers/analysis_worker.py 移植：
- QThread → 普通 async 类，进度经 async 回调发布（由 service 层转发 WebSocket）
- ThreadPoolExecutor + as_completed → asyncio.Semaphore + asyncio.as_completed
- 三阶段循环保留：串行预热 → 流式并发 → 失败补跑（最多3轮）
- 批次 Rolling：asyncio.Task 后台执行，不阻塞分析；asyncio.Event 同步
- 磁盘格式与旧项目零改动（MemoryState 统一落盘）
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..config.settings import AppConfig
from ..config.constants import (
    OUTPUT_DIR_NAME,
    ROLLING_SUMMARY_MAX_TOKENS, ROLLING_SUMMARY_MAX_RETRIES,
    ROLLING_SUMMARY_TEMPERATURE,
    ROLLING_EARLY_CHAPTERS,
    ROLLING_MOMENTUM_WINDOW, ROLLING_ARCHIVE_TRIGGER_COUNT,
)
from .file_processor import FileProcessor
from .analyzer import NovelAnalyzer
from .knowledge_base import KnowledgeBaseManager
from .llm_client import LLMClient
from .memory_state import MemoryState
from ..core.moderation import is_moderation_error
from ..core.error_taxonomy import ErrorClass, RETRY_POLICY, classify_message
from ..ws_events import PipelineStatus
from ..utils.export_utils import export_summary_report
from ..utils.json_utils import extract_json_from_text, safe_parse_json

logger = logging.getLogger(__name__)

# 进度回调类型：async fn(dict)
ProgressCallback = Callable[[dict], Awaitable[None]]


def _fmt_range(block_id: int, block_chs, block_size: int) -> str:
    """格式化章节范围字符串，供进度日志使用。block_chs 为 None 时按 block_id 推导。"""
    if block_chs:
        return f"ch{block_chs[0]}-ch{block_chs[-1]}" if block_size > 1 else f"ch{block_id}"
    return f"ch{block_id}-ch{block_id + block_size - 1}" if block_size > 1 else f"ch{block_id}"


# ==================== 结构化 Rolling 摘要 Prompt（原样移植） ====================

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


# ==================== 结构化滚动总结辅助函数（原样移植） ====================

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
            # 补跑后同步路径会把整场分析炸成 failed（P1 2026-08-24）。
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
    """分析流水线（asyncio 版，替代旧 AnalysisWorker）"""

    def __init__(
        self,
        config: AppConfig,
        directory: Path,
        knowledge_file: Path,
        start_chapter: int = 1,
        end_chapter: Optional[int] = None,
        on_progress: Optional[ProgressCallback] = None,
        on_token_stats: Optional[ProgressCallback] = None,
    ):
        """
        Args:
            config: 应用配置
            directory: 章节文件目录（blocks/）
            knowledge_file: 知识库文件路径（用于最终保存合并结果）
            start_chapter: 起始章节
            end_chapter: 结束章节（None表示到最后）
            on_progress: 进度回调 async fn(dict)，payload 对齐旧 progress 信号
            on_token_stats: token 统计回调 async fn(dict)
        """
        self.config = config
        self.directory = directory
        self.knowledge_file = knowledge_file
        self.start_chapter = start_chapter
        self.end_chapter = end_chapter
        self._stop_requested = False
        self._on_progress = on_progress
        self._on_token_stats = on_token_stats
        self._block_map: dict = {}  # block_id -> 实际章号列表（补跑时按原块恢复）
        self._rolling_early: int = ROLLING_EARLY_CHAPTERS  # 滚动摘要首生成阈值（可自适应）
        # 运行概览在途块（block_id -> {chapter, range, started_at}）：LLM 调用窗口内登记，
        # 供 /api/analysis/status#inflight_blocks 给前端车道对账（WS 丢 done 的治本纠偏源）
        self._inflight_blocks: dict = {}
        # 致命错误块登记（block_id -> 错误类别值，error_taxonomy T2.4/P2-2②）：
        # AUTH/BILLING/CONTEXT_LENGTH/BAD_REQUEST 类失败确定性不可自愈，补跑轮剔除不重试。
        # 审核类除外——skip_moderation_blocked=False 时用户明确要求补跑审核块
        self._fatal_blocks: dict = {}

    async def _emit(self, payload: dict) -> None:
        """发进度（替代旧 progress.emit）"""
        if self._on_progress:
            try:
                await self._on_progress(payload)
            except Exception as e:
                logger.warning(f"进度回调异常: {e}")

    async def _emit_tokens(self, payload: dict) -> None:
        """发 token 统计（替代旧 token_stats.emit）"""
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

    def _retryable_failed_blocks(self, failed_block_ids: list) -> list:
        """补跑列表过滤（T2.4）：剔除致命错误块（AUTH/BILLING/CONTEXT_LENGTH/BAD_REQUEST，
        见 _analyze_one_block_impl 的登记逻辑）。全部为致命错误时返回空列表，调用方据此终止补跑。"""
        retryable = [bid for bid in failed_block_ids if bid not in self._fatal_blocks]
        skipped_fatal = len(failed_block_ids) - len(retryable)
        if skipped_fatal:
            sample = ",".join(sorted({self._fatal_blocks[bid] for bid in failed_block_ids
                                      if bid in self._fatal_blocks}))
            logger.warning(f"补跑剔除{skipped_fatal}个致命错误块（{sample}，重试成功率恒为零）")
        return retryable

    async def run(self) -> dict:
        """
        执行分析任务（三阶段：预热 → 并发 → 补跑）。
        返回 finished payload：{stopped, total_analyzed, errors, failed_chapters}
        异常向上抛出，由调用方（service）捕获并转为 error 消息。
        """
        total_analyzed = 0
        self._fatal_blocks.clear()  # 断点续跑/复用实例时防止上一轮的 fatal 登记残留

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

        # 滚动摘要首生成阈值自适应（QUA-2）：短书按总章数 1/3 触发（否则全书
        # 分析完都不会生成结构化滚动摘要，prompt 缺失"全书主线概要"层）；
        # 长书维持配置值（默认 100 章）。
        cfg_early = self.config.analysis.rolling_early_chapters or ROLLING_EARLY_CHAPTERS
        self._rolling_early = min(cfg_early, max(30, len(all_chapters) // 3))

        # 3. 确定分析范围
        if self.end_chapter is None or self.end_chapter > all_chapters[-1]:
            self.end_chapter = all_chapters[-1]

        chapters_to_analyze = [
            c for c in all_chapters
            if self.start_chapter <= c <= self.end_chapter
        ]

        # 3a. 块化：将连续章节合并为分析块（按"实际存在的章号"切块）
        block_size = max(1, self.config.analysis.block_size)
        blocks = []
        for i in range(0, len(chapters_to_analyze), block_size):
            block_chapters = chapters_to_analyze[i:i + block_size]
            blocks.append((block_chapters[0], block_chapters))
        # 保存块 -> 实际章号映射：断号目录下 read_block 必须按实际章号读（P0-4），
        # 补跑阶段 block_chs=None 时也据此恢复原块内容。
        self._block_map = {bid: chs for bid, chs in blocks}

        total = len(blocks)
        logger.info(f"共{total}块待分析（每块{block_size}章），并发数: {self.config.analysis.concurrency}")

        # 4. 确定输出目录 + 初始化 MemoryState
        if self.config.working_directory:
            output_dir = Path(self.config.working_directory) / OUTPUT_DIR_NAME
        else:
            output_dir = Path(OUTPUT_DIR_NAME)
        output_dir.mkdir(parents=True, exist_ok=True)

        # 传入知识库限制配置，使 _trim_kb 使用用户配置而非硬编码常量
        kb_limits = {
            'max_compressed_arcs': self.config.analysis.max_compressed_arcs,
            'max_recent_summaries': self.config.analysis.max_recent_summaries,
            'max_pacing_tracker': self.config.analysis.max_pacing_tracker,
            'max_foreshadow_network': self.config.analysis.max_foreshadow_network,
            'max_world_building': self.config.analysis.max_world_building,
            'max_verified_facts': self.config.analysis.max_verified_facts,
            'max_long_term_arcs': self.config.analysis.max_long_term_arcs,
            'max_thematic_elements': self.config.analysis.max_thematic_elements,
        }
        self.state = MemoryState(
            checkpoint_interval=self.config.analysis.checkpoint_interval,
            kb_limits=kb_limits,
        )
        state = self.state
        # 恢复内部走线程池（MINOR-7）：千章级续跑不再阻塞事件循环
        restored = await state.restore_from_disk(output_dir, block_size=block_size)
        if restored > 0:
            logger.info(f"从磁盘恢复 {restored} 个已有 result")

        # 4a. 断点续跑：跳过已落盘的块
        completed = {bid for bid, _ in blocks if bid in state._flushed_chapters}
        skipped_blocks = [(bid, chs) for bid, chs in blocks if bid in completed]
        remaining_blocks = [(bid, chs) for bid, chs in blocks
                            if bid not in completed and bid not in state._skipped_chapters]
        if skipped_blocks:
            logger.info(f"断点续跑：跳过已完成{len(skipped_blocks)}块 → 块ID: {[b[0] for b in skipped_blocks]}")
            # 补发 block_done 事件，使前端续跑时也能看到这些章「已完成」（与正常完成表现一致）
            for idx, (bid, _chs) in enumerate(skipped_blocks, start=1):
                if idx % 50 == 0:
                    # 补发洪峰限速：每块 ~3 条消息，千章级续跑一次灌 2700+ 条会打满
                    # 每连接 WS 队列（maxsize=1000）触发丢最旧，挤掉真实 block_done
                    await asyncio.sleep(0)
                await self._emit({
                    "status": PipelineStatus.DONE,
                    "chapter": bid,
                    "progress": idx,
                    "total": total,
                    "elapsed": 0,
                    "tokens": (0, 0),
                })
        if not remaining_blocks:
            logger.info("所有块已完成，无需分析；仍执行最终 KB 合并与落盘收尾")
            # 不能在此直接 return：否则会跳过最终 KB 合并/保存（knowledge.json 不刷新）
            valid_block_ids = {b[0] for b in blocks}
            await state.flush_to_disk(output_dir)
            final_kb = await asyncio.to_thread(KnowledgeBaseManager.merge_results, output_dir)
            await asyncio.to_thread(KnowledgeBaseManager(self.knowledge_file).save, final_kb)
            logger.info(f"最终知识库已保存: {self.knowledge_file}")
            # 全量已完成：同样重生成汇总报告（保持与正常路径一致）
            all_done_results = sorted(state.results.values(), key=lambda r: r.chapter_number)
            if all_done_results:
                try:
                    summary_path = await asyncio.to_thread(
                        export_summary_report, all_done_results, output_dir)
                    logger.info(f"汇总报告已生成: {summary_path}")
                except Exception as e:
                    logger.warning(f"生成汇总报告失败: {e}")
            result_count = len([ch for ch in state.results if ch in valid_block_ids])
            failed_chapters = state.failed_chapters_in(valid_block_ids)
            skipped_chapters = [f"{c}({r})" for c, r in state._skipped_chapters.items()]
            return {
                "stopped": False, "total_analyzed": result_count,
                "errors": [f"失败{len(failed_chapters)}块"] if failed_chapters else [],
                "failed_chapters": failed_chapters,
                "skipped_chapters": skipped_chapters,
            }

        # 5. 初始化分析引擎
        analyzer = NovelAnalyzer(self.config)
        self._analyzer = analyzer

        # 6. 并发参数
        concurrency = max(1, self.config.analysis.concurrency)

        # 6a. 串行预热：前 N=concurrency 块串行处理，建立初始KB
        # 续跑时已有产物 >10 跳过预热（KB 已从磁盘重建）
        skip_warmup = restored > 10
        warmup_count = 0 if skip_warmup else min(concurrency, len(remaining_blocks))
        # BUG FIX: 进度从已恢复的块数开始，避免断点续跑时进度条从 0 跳变
        completed_count = len(skipped_blocks)

        if not skip_warmup:
            logger.info(f"串行预热前{warmup_count}块，建立初始知识库...")
            for i in range(warmup_count):
                if self._stop_requested:
                    break
                block_id, block_chs = remaining_blocks[i]
                # BUG FIX: 预热 start 事件使用 completed_count（而非循环索引 i），
                # 避免前端进度条从已完成数"回退"到 0
                _, completed_count, _, _, _ = await self.analyze_block_with_progress(
                    block_id=block_id, block_chs=block_chs, block_size=block_size,
                    total=total, label="预热",
                    progress_count=completed_count, completed_count=completed_count,
                    emit_start=True,
                )

        # 6b. 流式并发池：剩余任务经 Semaphore 限流，as_completed 持续消费
        remaining = remaining_blocks[warmup_count:]
        if skip_warmup:
            logger.info(f"已有{restored}块结果，跳过预热，剩余{len(remaining_blocks)}块直接并发（并发数={concurrency}）")
        else:
            logger.info(f"预热完成，剩余{len(remaining)}块切换为流式并发模式（并发数={concurrency}）")

        # rolling 独立 LLMClient 实例（不污染主分析 stats，不走重试链）
        rolling_client = LLMClient(self.config.api)
        self._rolling_client = rolling_client
        # rolling_last_chapter 优先从内存恢复的 state.rolling
        rolling_last_chapter = 0
        if isinstance(state.rolling, dict):
            rolling_last_chapter = state.rolling.get("last_updated_chapter", 0)
        if rolling_last_chapter == 0:
            if skip_warmup and state._flushed_chapters:
                rolling_last_chapter = max(state._flushed_chapters)
            elif not skip_warmup and remaining_blocks[:warmup_count]:
                rolling_last_chapter = max(
                    b_chs[-1] if b_chs else b_id
                    for b_id, b_chs in remaining_blocks[:warmup_count]
                )

        # Rolling 后台任务同步：asyncio.Event（初始 set = 无 rolling 在运行）
        _rolling_done = asyncio.Event()
        _rolling_done.set()
        _completed_since_rolling = 0
        _rolling_batch_size = concurrency  # 每 N 块完成触发一次 rolling
        _rolling_state = {"last_chapter": rolling_last_chapter}

        async def _async_rolling(completed_block_ids):
            """后台 rolling 更新（asyncio.Task）"""
            try:
                new_last = await self._update_rolling_summary(
                    rolling_client, output_dir, _rolling_state["last_chapter"],
                    completed_block_ids, state)
                _rolling_state["last_chapter"] = new_last
                await self._emit({
                    "chapter": 0, "status": PipelineStatus.ROLLING_UPDATED,
                    "progress": completed_count, "total": total,
                    "message": f"全书主线摘要已更新（纳入到第{new_last}章）"
                })
            except Exception as e:
                logger.error(f"后台 rolling 更新异常: {e}")
            finally:
                _rolling_done.set()

        # Semaphore 限流的并发 worker
        sem = asyncio.Semaphore(concurrency)

        async def _worker(block_id, block_chs):
            async with sem:
                if self._stop_requested:
                    return block_id, block_chs, False, 0.0, (0, 0), None, {"retries": 0, "failed_tokens": 0}, True
                # H17 P3 V2 修复（2026-08-26）：并发模式必须 emit start
                # 原因：前端 activeBlocks 依赖 block_start 消息登记车道。
                # 原代码 _worker 只调 _analyze_one_block（不 emit），由 _handle_block_outcome
                # 触发 done。结果：预热阶段（用 analyze_block_with_progress）正常，
                # 并发阶段 activeBlocks 永远为空 → 「并发车道 0/N 运行中」错误，
                # 车道也看不到运行时间（progressBar/lastTokenAt 不更新）。
                # 修复：在 _analyze_one_block 前补发 start，_handle_block_outcome 照常
                # 触发 done。semaphore 内部 emit 保证只在真正拿到槽位后才登记活跃。
                ch_range = _fmt_range(block_id, block_chs, block_size)
                await self._emit({
                    "chapter": block_id, "status": PipelineStatus.START,
                    "progress": completed_count, "total": total,
                    "range": ch_range, "block_size": len(block_chs or [block_id]),
                    "message": f"开始分析{ch_range}..."
                })
                try:
                    success, elapsed, ch_tokens, result, retry_info = await self._analyze_one_block(
                        analyzer, self.file_processor, state, block_id, block_chs,
                        block_size=block_size)
                except Exception as e:
                    # 异常必须带原 block_id 走 failed：消费循环的 except 兜底拿不到
                    # block_id（固定发 chapter:0），前端 blockId>0 检查会忽略，
                    # 已登记的 start 车道将永远收不到 done（车道堆叠泄漏源之一）
                    logger.error(f"块{block_id}分析异常: {e}", exc_info=True)
                    state.add_failed(block_id, f"块分析异常: {e}")
                    success, elapsed, ch_tokens, result, retry_info = (
                        False, 0.0, (0, 0), None, {"retries": 0, "failed_tokens": 0})
                return block_id, block_chs, success, elapsed, ch_tokens, result, retry_info, False

        tasks = [asyncio.create_task(_worker(bid, chs)) for bid, chs in remaining]

        _rolling_pending_ids = []
        batch_idx = 0  # checkpoint 计数

        for fut in asyncio.as_completed(tasks):
            if self._stop_requested:
                break
            try:
                block_id, block_chs, success, elapsed, ch_tokens, result, retry_info, skipped = await fut
                if skipped:
                    continue
                _, completed_count = await self._handle_block_outcome(
                    success=success, block_id=block_id, block_chs=block_chs,
                    block_size=block_size, total=total, label="",
                    completed_count=completed_count, elapsed=elapsed,
                    ch_tokens=ch_tokens, result=result, retry_info=retry_info,
                )
            except Exception as e:
                error_msg = f"块分析异常: {e}"
                logger.error(error_msg, exc_info=True)
                await self._emit({
                    "chapter": 0, "status": PipelineStatus.FAILED, "progress": completed_count,
                    "total": total, "message": error_msg
                })
                continue

            # 流式 rolling 触发：每 N 块完成触发一次
            _completed_since_rolling += 1
            _rolling_pending_ids.append(block_id)
            if _completed_since_rolling >= _rolling_batch_size:
                await _rolling_done.wait()  # 等上一轮 rolling 完成
                _rolling_done.clear()
                captured_ids = list(_rolling_pending_ids)
                asyncio.create_task(_async_rolling(captured_ids))
                _rolling_pending_ids.clear()
                _completed_since_rolling = 0
                await self._emit({
                    "chapter": 0, "status": PipelineStatus.ROLLING_STARTED,
                    "progress": completed_count, "total": total,
                    "message": f"全书主线摘要更新中（后台，已完成{completed_count}块）..."
                })

            # Checkpoint（不等 rolling）
            batch_idx += 1
            if state.should_checkpoint(batch_idx - 1):
                await state.flush_to_disk(output_dir)

        # 取消未完成任务（stop 时）
        for t in tasks:
            if not t.done():
                t.cancel()
        # 流式循环结束，等最后一轮 rolling 完成
        await _rolling_done.wait()

        # 提前计算 valid_block_ids（补跑阶段也需要使用）
        valid_block_ids = {b[0] for b in blocks}

        # 7c. 失败块自动补跑（最多3轮，每轮串行逐个重试以确保KB正确）
        for retry_round in range(3):
            if self._stop_requested:
                break
            failed_block_ids = [bid for bid, _ in blocks
                                if bid in state._failed_chapters and bid not in state.results]
            if not failed_block_ids:
                break
            # T2.4（P2-2②）：致命错误块（欠费/认证/参数/上下文）确定性不可自愈，不进补跑
            failed_block_ids = self._retryable_failed_blocks(failed_block_ids)
            if not failed_block_ids:
                logger.warning("补跑终止：失败块全部为致命错误（API 余额不足/认证失败等），不再补跑")
                break
            logger.info(f"补跑第{retry_round + 1}轮: {len(failed_block_ids)}个失败块")
            for block_id in failed_block_ids:
                if self._stop_requested:
                    break
                state._failed_chapters.pop(block_id, None)
                _, completed_count, _, _, _ = await self.analyze_block_with_progress(
                    block_id=block_id, block_chs=None, block_size=block_size,
                    total=total, label="补跑",
                    progress_count=completed_count, completed_count=completed_count,
                    emit_start=True,
                )

        # 补跑成功后更新 rolling summary（失败只告警，绝不阻断收尾落盘）
        if not self._stop_requested:
            retried_block_ids = [
                bid for bid, _ in blocks
                if bid in valid_block_ids and bid > _rolling_state["last_chapter"] and bid in state.results
            ]
            if retried_block_ids:
                logger.info(f"补跑完成后更新 rolling summary: {len(retried_block_ids)}个块")
                await self._safe_post_retry_rolling_update(
                    rolling_client, output_dir, _rolling_state,
                    retried_block_ids, state, completed_count, total)

        # 8. 收集成功/失败信息（仅统计当前范围内的块）
        await state.flush_to_disk(output_dir)

        result_count = len([ch for ch in state.results if ch in valid_block_ids])
        failed_chapters = state.failed_chapters_in(valid_block_ids)
        skipped_chapters = [f"{c}({r})" for c, r in state._skipped_chapters.items()]
        total_analyzed = result_count

        if failed_chapters:
            await self._emit({
                "chapter": 0,
                "status": PipelineStatus.FAILED_SUMMARY,
                "progress": total_analyzed,
                "total": total,
                "message": f"失败章节: {', '.join(str(c) for c in failed_chapters)}"
            })

        # 9. 脚本合并所有结果生成最终KB（纯 CPU/IO，走线程池）
        logger.info("开始脚本合并所有章节结果...")
        final_kb = await asyncio.to_thread(KnowledgeBaseManager.merge_results, output_dir)
        await asyncio.to_thread(KnowledgeBaseManager(self.knowledge_file).save, final_kb)
        logger.info(f"最终知识库已保存: {self.knowledge_file}")

        # 10. 使用全量结果生成汇总报告（含断点恢复的旧块：续跑必须重生成完整报告，
        # 不能只用本轮新块覆盖磁盘上已存在的完整版）
        all_results = sorted(state.results.values(), key=lambda r: r.chapter_number)
        if all_results:
            try:
                summary_path = await asyncio.to_thread(
                    export_summary_report, all_results, output_dir)
                logger.info(f"汇总报告已生成: {summary_path}")
            except Exception as e:
                logger.warning(f"生成汇总报告失败: {e}")

        # 11. 完成前释放章节内容缓存（全书内容无界常驻内存，分析结束即清空）
        try:
            self.file_processor.clear_cache()
        except Exception:
            pass

        # 12. 完成
        logger.info(f"分析任务完成，成功{total_analyzed}块，失败{len(failed_chapters)}块"
                    f"{f'，内容审核拦截跳过{len(skipped_chapters)}块' if skipped_chapters else ''}")
        return {
            "stopped": self._stop_requested,
            "total_analyzed": total_analyzed,
            "errors": [f"失败{len(failed_chapters)}块"] if failed_chapters else [],
            "failed_chapters": failed_chapters,
            "skipped_chapters": skipped_chapters
        }

    def inflight_blocks(self) -> list:
        """当前真实在途块快照（LLM 调用窗口内，天然 ≤ concurrency 且零丢失）"""
        return sorted(self._inflight_blocks.values(), key=lambda d: d["started_at"])

    async def _analyze_one_block(self, analyzer, file_processor, state,
                                 block_id: int, block_chs=None,
                                 block_size: int = 1) -> tuple:
        """
        分析单个块的公共逻辑（预热/并发/补跑共用）。
        纯分析+保存，不 emit progress（由调用方负责）。

        在途登记放在这一层而非 _worker：预热与补跑不走 _worker，
        三条路径在此单点汇聚才能覆盖全部车道。

        Args:
            block_chs: 本块实际章号列表；None 时回退到 _block_map（补跑场景），
                       再回退 [block_id]（兼容旧调用）。

        Returns:
            (success, elapsed, ch_tokens, result, retry_info)
        """
        chs = block_chs or self._block_map.get(block_id) or [block_id]
        self._inflight_blocks[block_id] = {
            "chapter": block_id,
            "range": _fmt_range(block_id, chs, block_size),
            "started_at": time.time(),
        }
        try:
            return await self._analyze_one_block_impl(
                analyzer, file_processor, state, block_id, chs, block_size)
        finally:
            self._inflight_blocks.pop(block_id, None)

    async def _analyze_one_block_impl(self, analyzer, file_processor, state,
                                      block_id: int, chs: list,
                                      block_size: int = 1) -> tuple:
        """_analyze_one_block 的实际分析体（chs 已解析，在途登记由外层负责）"""
        content = await asyncio.to_thread(file_processor.read_block, chs)
        if content is None:
            state.add_failed(block_id, "读取失败")
            return False, 0.0, (0, 0), None, {"retries": 0, "failed_tokens": 0}

        # P2 修复：快照构建是 CPU 密集深拷贝，事件循环内裸调会让并发 worker/
        # WS 广播整体冻结；内部 _snapshot_lock 是 threading.Lock，to_thread 安全。
        temp_kb = await asyncio.to_thread(state.get_kb_snapshot, chapter_limit=block_id)
        t_start = time.time()
        result, ch_tokens, retry_info = await analyzer.analyze_chapter(
            block_id, content, temp_kb, block_size=block_size,
            # H16 Phase 3：session_id 借用 directory 路径（保证多书并发隔离）
            book_id=str(self.directory) if self.directory else "")
        elapsed = time.time() - t_start

        if result is None:
            err_text = (retry_info or {}).get("error", "")
            if (self.config.analysis.skip_moderation_blocked
                    and is_moderation_error(err_text)):
                # 内容审核拦截：标记 skipped（不进失败集、补跑不重试）
                retry_info["moderation_skip"] = True
                state.add_skipped(block_id, "内容审核拦截")
                logger.warning(f"块{block_id}内容审核拦截，跳过（重试1次后仍拦截）")
                return False, elapsed, ch_tokens, None, retry_info
            # 致命错误登记（T2.4，P2-2 最小方案②）：stop 类错误重试成功率恒为零，
            # 补跑轮据此剔除——欠费场景整场浪费从"剩余块数×4 次调用"降到"×1 次"。
            # 审核类不登记（见 __init__ 注释）；err_text 为空/UNKNOWN 保守不登记
            _verdict = classify_message(err_text)
            if _verdict.cls is not ErrorClass.UNKNOWN and RETRY_POLICY[_verdict.cls] == "stop" \
                    and _verdict.cls not in (ErrorClass.MODERATION_INPUT, ErrorClass.MODERATION_OUTPUT):
                self._fatal_blocks[block_id] = _verdict.cls.value
            reason = "分析失败（LLM响应解析失败或重试耗尽）"
            state.add_failed(block_id, reason)
            logger.warning(f"块{block_id}分析失败: {reason}，耗时{elapsed:.1f}s，tokens={ch_tokens}")
            return False, elapsed, ch_tokens, None, retry_info

        await state.add_result(result)
        return True, elapsed, ch_tokens, result, retry_info

    async def analyze_block_with_progress(
        self, *, block_id: int, block_chs, block_size: int, total: int,
        label: str, progress_count: int, completed_count: int,
        emit_start: bool = True,
    ):
        """
        同步块路径（预热 + 补跑两段循环共享逻辑）。
        包括：emit start → _analyze_one_block → _handle_block_outcome。

        Returns:
            (success, new_completed_count, ch_tokens, result, retry_info)
        """
        if self._stop_requested:
            return False, completed_count, (0, 0), None, {"retries": 0, "failed_tokens": 0}

        ch_range = _fmt_range(block_id, block_chs, block_size)
        prefix = f"[{label}] " if label else ""

        if emit_start:
            await self._emit({
                "chapter": block_id, "status": PipelineStatus.START,
                "progress": progress_count, "total": total,
                "range": ch_range, "block_size": len(block_chs or [block_id]),
                "message": f"{prefix}开始分析{ch_range}..."
            })

        try:
            success, elapsed, ch_tokens, result, retry_info = await self._analyze_one_block(
                self._analyzer, self.file_processor, self.state, block_id, block_chs,
                block_size=block_size,
            )

            success, new_completed = await self._handle_block_outcome(
                success=success, block_id=block_id, block_chs=block_chs,
                block_size=block_size, total=total, label=label,
                completed_count=completed_count, elapsed=elapsed,
                ch_tokens=ch_tokens, result=result, retry_info=retry_info,
            )
        except Exception as e:
            # 预热/补跑路径的异常兜底：不包裹会冲出 run() 被服务层整书标 failed；
            # 且必须带正确 chapter 发 failed，否则前端已登记的 start 车道泄漏
            logger.error(f"{prefix}块{block_id}分析异常: {e}", exc_info=True)
            self.state.add_failed(block_id, f"块分析异常: {e}")
            await self._emit({
                "chapter": block_id, "status": PipelineStatus.FAILED,
                "progress": completed_count, "total": total,
                "range": ch_range, "block_size": len(block_chs or [block_id]),
                "retries": 0, "failed_tokens": 0,
                "message": f"{prefix}{ch_range}分析异常",
            })
            return False, completed_count, (0, 0), None, {"retries": 0, "failed_tokens": 0}
        return success, new_completed, ch_tokens, result, retry_info

    async def _handle_block_outcome(
        self, *, success: bool, block_id: int, block_chs, block_size: int,
        total: int, label: str, completed_count: int, elapsed: float,
        ch_tokens, result, retry_info: Optional[dict] = None,
    ):
        """单块结果后处理（accumulate + emit done/failed + token stats）。

        Returns:
            (success, new_completed_count)
        """
        retry_info = retry_info or {"retries": 0, "failed_tokens": 0}
        ch_range = _fmt_range(block_id, block_chs, block_size)
        prefix = f"[{label}] " if label else ""

        if success:
            completed_count += 1
            # 剔除 raw_response 再广播：否则每个 block_done 携带整段原文（可达 13 万 token），
            # WS 带宽与前端内存都会被拖垮；raw_response 仅用于本次广播，
            # 落盘与广播均剔除（详见 memory_state.flush_to_disk，前端经 /api/books 读取结构化结果）。
            result_dict = result.to_dict() if result else None
            if isinstance(result_dict, dict):
                result_dict.pop("raw_response", None)
            await self._emit({
                "chapter": block_id, "status": PipelineStatus.DONE,
                "result": result_dict,
                "progress": completed_count, "total": total, "elapsed": elapsed,
                "range": ch_range, "block_size": len(block_chs or [block_id]),
                "tokens": ch_tokens[0] + ch_tokens[1],
                "input_tokens": ch_tokens[0], "output_tokens": ch_tokens[1],
                "retries": retry_info.get("retries", 0),
                "failed_tokens": retry_info.get("failed_tokens", 0),
                "message": f"{prefix}{ch_range}分析完成"
            })
            # 广播后立即释放 raw_response：整段 LLM 原文在内存里只服务于这次广播，
            # 继续持有会让长书内存随章节线性增长（万章级 = 数百 MB 死重）
            if result is not None:
                result.raw_response = ""
        else:
            if retry_info.get("moderation_skip"):
                # 内容审核拦截跳过：单独状态（非失败），进度不计、不触发 block_done 失败
                await self._emit({
                    "chapter": block_id, "status": PipelineStatus.SKIPPED,
                    "progress": completed_count, "total": total,
                    "range": ch_range, "block_size": len(block_chs or [block_id]),
                    "message": f"{prefix}{ch_range} 内容审核拦截，已跳过"
                })
                return False, completed_count
            await self._emit({
                "chapter": block_id, "status": PipelineStatus.FAILED,
                "progress": completed_count, "total": total,
                "range": ch_range, "block_size": len(block_chs or [block_id]),
                "retries": retry_info.get("retries", 0),
                "failed_tokens": retry_info.get("failed_tokens", 0),
                "message": f"{prefix}{ch_range}分析失败"
            })

        # token 统计：发送本块实际消耗（非累计值，避免 on_token_stats 重复累加）
        # 累计值由 LLMClient.get_stats() 维护，前端可通过 /api/analysis/token_stats 获取
        in_tok, out_tok = ch_tokens if isinstance(ch_tokens, tuple) and len(ch_tokens) == 2 else (0, 0)
        await self._emit_tokens({
            "category": "chapter",
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "current_tokens": in_tok + out_tok,  # 兼容旧前端读取 current_tokens
        })
        return success, completed_count

    async def _emit_rolling_tokens(self, tokens):
        """rolling 调用 token 统计（asyncio 内直接 emit，无需旧版线程缓冲）"""
        if not tokens or not isinstance(tokens, tuple) or len(tokens) < 2:
            return
        await self._emit_tokens({
            "category": "rolling",
            "input_tokens": int(tokens[0] or 0),
            "output_tokens": int(tokens[1] or 0),
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

        # 3a. 检测旧格式降级（_legacy_text），触发重新初始化
        rs = rolling_data.get("rolling_structured") or {}
        if rs.get("_legacy_text"):
            logger.info("检测到旧格式降级，触发重新初始化...")
            await self._reinitialize_structured_rolling(rolling_client, output_dir, rolling_data, state)

        # 4. 增量更新结构化 JSON
        old_structured = rolling_data.get("rolling_structured", {})
        new_chapters_text = self._format_chapters_for_rolling(new_results)

        messages = [
            {"role": "system", "content": STRUCTURED_ROLLING_SYSTEM_PROMPT},
            {"role": "user", "content": STRUCTURED_ROLLING_USER_UPDATE_TEMPLATE.format(
                current_data=json.dumps(old_structured, ensure_ascii=False, indent=2) if old_structured else "{}",
                new_chapters=new_chapters_text
            )}
        ]

        new_json = None
        for attempt in range(1 + ROLLING_SUMMARY_MAX_RETRIES):
            if self._stop_requested:
                return last_chapter
            success, content, error, tokens = await rolling_client.chat(
                messages,
                temperature=ROLLING_SUMMARY_TEMPERATURE,
                max_tokens=ROLLING_SUMMARY_MAX_TOKENS
            )
            await self._emit_rolling_tokens(tokens)
            if success and content and content.strip():
                raw = extract_json_from_text(content)
                if raw:
                    parsed = safe_parse_json(raw)
                    if parsed:
                        new_json = _validate_and_fill_rolling_schema(parsed)
                        break
                logger.warning(f"rolling JSON 解析失败（第{attempt + 1}次），重试...")
            else:
                logger.warning(f"rolling 更新第{attempt + 1}次调用失败: {error}")

        if new_json is None:
            logger.warning(f"rolling 更新全部失败，保留旧数据（已纳入到第{last_chapter}章）")
            return last_chapter

        # 5. 程序侧硬截断 + 归档
        # 5a. paradigm_layers 截断
        pl = new_json.get("paradigm_layers", [])
        if len(pl) > 5:  # 范式层上限
            new_json["paradigm_layers"] = pl[-5:]

        # 5b. recent_momentum 归档检测
        momentum = new_json.get("recent_momentum", [])
        should_archive = False
        if len(momentum) >= ROLLING_ARCHIVE_TRIGGER_COUNT:
            should_archive = True
            logger.info(f"recent_momentum 条目数({len(momentum)})达到归档触发阈值({ROLLING_ARCHIVE_TRIGGER_COUNT})")
        else:
            # 跨度检测：提取首尾章号
            first_ch = _extract_chapter_number(momentum[0]) if momentum else None
            last_ch = _extract_chapter_number(momentum[-1]) if momentum else None
            if first_ch and last_ch and (last_ch - first_ch) >= ROLLING_MOMENTUM_WINDOW:
                should_archive = True
                logger.info(f"recent_momentum 跨度({first_ch}-{last_ch})超过归档窗口({ROLLING_MOMENTUM_WINDOW})")

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
        max_chains = 5  # 因果链条目上限
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

    async def _safe_post_retry_rolling_update(self, rolling_client, output_dir,
                                              _rolling_state, retried_block_ids,
                                              state, completed_count, total) -> None:
        """补跑完成后的同步 rolling 更新（P1 修复 2026-08-24）。

        原实现裸 await：畸形 rolling schema 的 AttributeError 直接冲出 run()，
        最终 flush / 最终 KB 合并 / 汇总报告全部跳过，整场已成功的分析被
        service 层标记为 failed。此处失败只告警，不影响收尾。"""
        try:
            _rolling_state["last_chapter"] = await self._update_rolling_summary(
                rolling_client, output_dir, _rolling_state["last_chapter"],
                retried_block_ids, state)
            await self._emit({
                "chapter": 0, "status": PipelineStatus.ROLLING_UPDATED,
                "progress": completed_count, "total": total,
                "message": "[补跑后] 全书主线摘要已更新"
            })
        except Exception as e:
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
        success, content, error, tokens, _call_stats = await rolling_client.chat_with_retry(
            messages, max_tokens=100
        )
        await self._emit_rolling_tokens(tokens)
        if success and content and content.strip():
            milestone = content.strip().strip('"').strip("'")
            if len(milestone) > 60:
                milestone = milestone[:60]
            structured_data.setdefault("global_milestones", []).append(milestone)
            structured_data["recent_momentum"] = []
            logger.info(f"归档完成：{len(momentum)}条势头条目 → 里程碑「{milestone}」")
        else:
            logger.warning(f"归档 LLM 调用失败: {error}，保留原 recent_momentum")

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

        success, content, error, tokens = await rolling_client.chat(
            messages, temperature=ROLLING_SUMMARY_TEMPERATURE, max_tokens=ROLLING_SUMMARY_MAX_TOKENS
        )
        await self._emit_rolling_tokens(tokens)

        if success and content and content.strip():
            raw = extract_json_from_text(content)
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

        success, content, error, tokens = await rolling_client.chat(
            messages,
            temperature=ROLLING_SUMMARY_TEMPERATURE,
            max_tokens=ROLLING_SUMMARY_MAX_TOKENS
        )
        await self._emit_rolling_tokens(tokens)

        if success and content and content.strip():
            raw = extract_json_from_text(content)
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
            logger.warning(f"早期摘要 JSON 解析失败: {content[:200]}")
        else:
            logger.warning(f"早期摘要生成失败: {error}")

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
