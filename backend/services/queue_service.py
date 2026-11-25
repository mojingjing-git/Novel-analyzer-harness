"""
队列服务：分析运行编排层（AnalysisService 单例）
- 持有当前 AnalysisPipeline，进度回调经 pipeline_events 转发到 ProgressHub（WebSocket 广播）
- 队列管理（QueueItem/QueueManager）见 queue_manager.py
- token 统计累加见 analysis_stats.py；WS 事件翻译见 pipeline_events.py
"""

import asyncio
import logging
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Literal, Optional, Sequence, Tuple, Union

from ..common.chapter_spec import parse_chapter_spec
from ..common.file_lock import BookFileLock, BookLockError
from ..config.settings import AppConfig, ConfigManager, validate_settings_payload
from ..core.file_processor import FileProcessor
from ..core.llm_failure_logger import redact
from ..core.pipeline import AnalysisPipeline
from ..progress_hub import get_hub
from .analysis_stats import AnalysisStats
from .pipeline_events import make_pipeline_callbacks
from ..ws_events import WSState
from .queue_manager import CONFIG_FILE, PROJECT_ROOT, QUEUE_STATE_FILE, QueueItem, QueueManager

logger = logging.getLogger(__name__)

_ALLOWED_API_OVERRIDES = frozenset({
    "model", "temperature", "max_tokens", "reasoning_effort", "thinking_mode", "timeout",
})
_ALLOWED_ANALYSIS_OVERRIDES = frozenset({
    "block_size", "concurrency", "max_content_chars",
})


def _apply_thinking_effort_override(cfg: AppConfig, effort_raw: str) -> str:
    """统一将 ``reasoning_effort`` 覆盖写入 ``cfg.api``（同步刷新 ``thinking_mode``）。"""
    effort = str(effort_raw or "").strip().lower()
    valid_efforts = ("", "none", "low", "medium", "high", "xhigh", "max")
    if effort not in valid_efforts:
        raise ValueError(f"不支持的 reasoning_effort: {effort_raw!r}")
    tm = dict(cfg.api.thinking_mode) if isinstance(cfg.api.thinking_mode, dict) else {}
    for conflict_key in ("enable_thinking", "thinking", "reasoning"):
        tm.pop(conflict_key, None)
    if effort == "none":
        cfg.api.reasoning_effort = ""
        tm["reasoning_effort"] = "none"
    elif effort == "":
        cfg.api.reasoning_effort = ""
        tm.pop("reasoning_effort", None)
    else:
        cfg.api.reasoning_effort = effort
        tm["reasoning_effort"] = effort
    cfg.api.thinking_mode = tm
    return effort


def _apply_rerun_overrides(
    base_cfg: AppConfig,
    overrides: Optional[Dict[str, Any]],
) -> Tuple[AppConfig, Dict[str, Any]]:
    """对克隆配置应用严格白名单过滤后的定点重跑临时覆盖参数（不写盘）。"""
    run_cfg = AppConfig.from_dict(base_cfg.to_dict())
    if not overrides:
        return run_cfg, {}
    if not isinstance(overrides, dict):
        raise ValueError("overrides 必须为字典")

    api_patch: Dict[str, Any] = {}
    analysis_patch: Dict[str, Any] = {}
    recorded: Dict[str, Any] = {}
    effort_override: Optional[str] = None

    for key, val in overrides.items():
        if val is None:
            continue
        if key == "api" and isinstance(val, dict):
            for k2, v2 in val.items():
                if k2 not in _ALLOWED_API_OVERRIDES:
                    raise ValueError(f"不允许通过重跑覆盖参数: api.{k2}")
                if k2 == "reasoning_effort":
                    effort_override = str(v2)
                elif v2 is not None:
                    api_patch[k2] = v2
                    recorded[k2] = v2
        elif key == "analysis" and isinstance(val, dict):
            for k2, v2 in val.items():
                if k2 not in _ALLOWED_ANALYSIS_OVERRIDES:
                    raise ValueError(f"不允许通过重跑覆盖参数: analysis.{k2}")
                if v2 is not None:
                    analysis_patch[k2] = v2
                    recorded[k2] = v2
        elif key in _ALLOWED_API_OVERRIDES:
            if key == "reasoning_effort":
                effort_override = str(val)
            else:
                api_patch[key] = val
                recorded[key] = val
        elif key in _ALLOWED_ANALYSIS_OVERRIDES:
            analysis_patch[key] = val
            recorded[key] = val
        else:
            raise ValueError(f"不允许通过重跑覆盖参数: {key}")

    validate_payload: Dict[str, Any] = {}
    if api_patch:
        validate_payload["api"] = api_patch
    if analysis_patch:
        validate_payload["analysis"] = analysis_patch
    if validate_payload:
        errs = validate_settings_payload(validate_payload)
        if errs:
            first_msg = errs[0].get("msg") or "覆盖参数校验失败"
            raise ValueError(str(first_msg))
        merged = run_cfg.to_dict()
        if api_patch:
            merged["api"].update(api_patch)
        if analysis_patch:
            merged["analysis"].update(analysis_patch)
        run_cfg = AppConfig.from_dict(merged)

    if effort_override is not None:
        normalized = _apply_thinking_effort_override(run_cfg, effort_override)
        recorded["reasoning_effort"] = normalized or "none"

    return run_cfg, recorded


class AnalysisService:
    """
    队列运行编排层（单例）。
    - start(): 启动队列消费 asyncio.Task，逐本运行 AnalysisPipeline
    - stop(): 停止当前 pipeline 并中断队列
    - 进度回调 → ProgressHub 广播（WS 消息 type 对齐旧 Qt 信号）
    """

    def __init__(self):
        self.config_manager = ConfigManager(CONFIG_FILE)
        self.config_manager.load()
        self.queue = QueueManager()
        # check_api 的 heal 纠正值需经 config_manager 落盘
        # 持久化（check_api 以 self.queue 身份执行，getattr(self, "config_manager")
        # 取的就是这里挂上的引用）
        self.queue.config_manager = self.config_manager
        self.queue.load_state(QUEUE_STATE_FILE)
        self._runner_task: Optional[asyncio.Task] = None
        self._pipeline: Optional[AnalysisPipeline] = None
        self._stop_requested = False
        self._account_tripped_reason: str = ""
        self._run_mode: str = "idle"
        self._rerun_meta: Optional[Dict[str, Any]] = None
        # 一次分析会话的 token/耗时累计（分类统计/每章记录/重试成本/KV 命中），
        # 生命周期 = start 重置 → finally 冻结（实现见 analysis_stats.py）
        self.stats = AnalysisStats()
        # 发现流：跨块"已见人物"集合（断点续跑后全量"首次"是已知限制）
        self._seen_characters: set = set()
        # 注意：启动期自动扫描在 app.py 的 lifespan 启动阶段执行（asyncio.to_thread），
        # 此处不同步执行——否则首个触发 get_service() 的 async 请求会在事件循环内
        # 同步跑完整工作区扫描（网络盘多书时卡数秒~数十秒），冻结整个事件循环。

    # ---- 状态 ----
    @property
    def is_running(self) -> bool:
        return self._runner_task is not None and not self._runner_task.done()

    def status(self) -> Dict[str, Any]:
        # 暴露 concurrency / block_size，
        # 前端 LaneView 必须区分两者：block_size（每块几章）与 concurrency（并发块数）
        # 混用会让「并发车道 N/4」实际是「N/每块4章」——配置 concurrency=8 时实际 8 路并发但 UI 标 4。
        cm = getattr(self, "config_manager", None)
        analysis_cfg = getattr(getattr(cm, "config", None), "analysis", None) if cm else None
        # getattr 兜底：部分单测用 __new__ 构造实例（无 __init__ 属性）
        pipeline = getattr(self, "_pipeline", None)
        return {
            "running": self.is_running,
            "mode": getattr(self, "_run_mode", "idle") if self.is_running else "idle",
            "rerun": getattr(self, "_rerun_meta", None) if self.is_running else None,
            "queue": self.queue.get_progress(),
            "items": [item.to_dict() for item in self.queue.items],
            "workspace_dir": self.workspace_path.as_posix(),
            "concurrency": getattr(analysis_cfg, "concurrency", None) if analysis_cfg else None,
            "block_size": getattr(analysis_cfg, "block_size", None) if analysis_cfg else None,
            # 运行概览车道对账源：pipeline 信号量窗口内的真实在途块（≤ concurrency，
            # 零丢失）。前端 WS 记账丢 done 时以此整体纠偏
            "inflight_blocks": pipeline.inflight_blocks() if pipeline is not None else [],
        }

    def token_stats(self) -> Dict[str, Any]:
        """返回分类 token 统计 + 每章记录 + 总耗时 + KV 缓存命中数"""
        cached_tokens = getattr(self.stats, "total_cached_tokens", 0) or 0
        return self.stats.session_snapshot(running=self.is_running, cached_tokens=cached_tokens)


    @property
    def workspace_path(self) -> Path:
        """解析工作区绝对路径"""
        ws = self.config_manager.config.workspace_dir
        ws_path = Path(ws)
        if not ws_path.is_absolute():
            ws_path = PROJECT_ROOT / ws_path
        return ws_path

    def _auto_scan_workspace(self) -> None:
        """启动时自动扫描工作区目录，将小说加入队列"""
        ws = self.workspace_path
        if not ws.exists():
            ws.mkdir(parents=True, exist_ok=True)
            logger.info(f"已创建工作区目录: {ws}")
            return
        found = self.queue.scan_directory(ws)
        if not found:
            logger.info(f"工作区 {ws} 中未发现小说")
            return
        added = 0
        for item in found:
            item.block_size = max(1, self.config_manager.config.analysis.block_size)
            done_chapters = self.queue.infer_progress(item)
            item.completed_chapters = done_chapters
            idx = self.queue.add_item(item)
            if idx == len(self.queue.items) - 1:
                added += 1
        if added > 0:
            self.save_queue()
            logger.info(f"工作区自动扫描: 新增 {added} 本小说")

    def scan_workspace(self) -> int:
        """手动刷新工作区扫描（供前端调用）"""
        ws = self.workspace_path
        if not ws.exists():
            return 0
        found = self.queue.scan_directory(ws)
        added = 0
        for item in found:
            item.block_size = max(1, self.config_manager.config.analysis.block_size)
            done_chapters = self.queue.infer_progress(item)
            item.completed_chapters = done_chapters
            idx = self.queue.add_item(item)
            if idx == len(self.queue.items) - 1:
                added += 1
        if added > 0:
            self.save_queue()
        return added

    # ---- 控制 ----
    def start_blocker(self) -> Literal["analysis_running", "summary_running", "queue_finished"] | None:
        if self.is_running:
            return "analysis_running"
        # 总结运行时需要独占分析产物。
        try:
            from .summary_service import get_summary_service
            if get_summary_service().is_running:
                return "summary_running"
        except Exception:
            logger.debug("无法读取总结运行状态", exc_info=True)
        if self.queue.is_finished:
            return "queue_finished"
        return None

    def start(self) -> bool:
        """队列未被阻塞时启动分析。"""
        blocker = self.start_blocker()
        if blocker:
            logger.warning("无法启动分析: %s", blocker)
            return False
        self._stop_requested = False
        self._account_tripped_reason = ""
        self._run_mode = "queue"
        self._rerun_meta = None
        # 重置"已见人物"集合，避免上一本书的人物污染新书首次登场判定
        # （AnalysisService 是进程级单例，__init__ 只跑一次）
        self._seen_characters = set()
        # 新会话：清空 token/耗时累计并记录开始时刻
        self.stats.reset()
        self._runner_task = asyncio.create_task(self._run_queue())
        return True

    def start_rerun(
        self,
        book_id: str,
        chapters: Union[str, Sequence[int]],
        *,
        overrides: Optional[Dict[str, Any]] = None,
        update_rolling: bool = False,
    ) -> Dict[str, Any]:
        """启动指定章节或区间的定点重跑（与全书队列/总结互斥并持 .book.lock）。"""
        if self.is_running:
            raise RuntimeError("analysis_running")
        try:
            from .summary_service import get_summary_service
            if get_summary_service().is_running:
                raise RuntimeError("summary_running")
        except RuntimeError:
            raise
        except Exception:
            logger.debug("无法读取总结运行状态", exc_info=True)

        book_dir: Optional[Path] = None
        for q_item in self.queue.items:
            if q_item.name == book_id and q_item.workspace_dir.exists():
                book_dir = q_item.workspace_dir
                break
        if book_dir is None:
            from . import book_service
            book_dir = book_service.get_book_path(book_id)
            if book_dir is None:
                book_service.refresh_books()
                book_dir = book_service.get_book_path(book_id)
        if book_dir is None or not book_dir.exists():
            raise FileNotFoundError(f"书目不存在: {book_id}")

        blocks_dir = book_dir / "blocks"
        if not blocks_dir.is_dir():
            raise ValueError(f"书目 {book_id} 缺少 blocks/ 正文目录，无法重跑")

        available = FileProcessor(blocks_dir, self.config_manager.config.analysis.encoding_priority).scan_chapters()
        if not available:
            raise ValueError(f"书目 {book_id} 的 blocks/ 目录中未找到章节文件")
        parsed_chapters = parse_chapter_spec(chapters, available_chapters=available)

        base_cfg = self.config_manager.load()
        run_cfg, recorded_overrides = _apply_rerun_overrides(base_cfg, overrides)
        run_cfg.working_directory = str(book_dir)

        lock = BookFileLock(book_dir / ".book.lock")
        lock.acquire()
        try:
            self._stop_requested = False
            self._account_tripped_reason = ""
            self._seen_characters = set()
            self.stats.reset()
            self._run_mode = "rerun"
            self._rerun_meta = {
                "book_id": book_id,
                "chapters": parsed_chapters,
                "total": len(parsed_chapters),
                "overrides": recorded_overrides,
            }
            self._runner_task = asyncio.create_task(
                self._run_rerun(
                    book_id=book_id,
                    book_dir=book_dir,
                    blocks_dir=blocks_dir,
                    parsed_chapters=parsed_chapters,
                    run_cfg=run_cfg,
                    update_rolling=bool(update_rolling),
                    lock=lock,
                )
            )
        except Exception:
            self._run_mode = "idle"
            self._rerun_meta = None
            lock.release()
            raise

        return {
            "ok": True,
            "book_id": book_id,
            "chapters": parsed_chapters,
            "total": len(parsed_chapters),
        }

    def stop(self) -> bool:
        """请求停止：当前 pipeline 在下一检查点退出，队列不再推进"""
        if not self.is_running:
            return False
        self._stop_requested = True
        if self._pipeline is not None:
            self._pipeline.stop()
        return True

    def save_queue(self) -> None:
        self.queue.save_state(QUEUE_STATE_FILE)

    # ---- 队列消费循环 ----
    async def _run_queue(self) -> None:
        hub = get_hub()
        config = self.config_manager.load()  # 每次启动重读配置

        try:
            # API 预检查
            await hub.log("API 预检查中...")
            if not await self.queue.check_api(config):
                await hub.log("API 预检查失败，请检查设置中的 API 配置", level="error")
                await hub.state_change(WSState.IDLE, "API 预检查失败")
                return

            await hub.state_change(WSState.RUNNING, "队列分析开始")

            while not self._stop_requested:
                item = self.queue.get_current()
                if item is None or item.status not in ("pending", "running"):
                    break
                if not self.queue.validate_item(item):
                    self.queue.mark_current("failed", "blocks 目录无效")
                    await hub.log(f"《{item.name}》blocks 目录无效，跳过", level="error")
                    self.queue.advance()
                    self.save_queue()
                    continue

                await self._run_one_item(item, config)
                if self._stop_requested:
                    break
                self.queue.advance()
                self.save_queue()

            # 收尾
            self.save_queue()
            if self._stop_requested:
                tripped_reason = getattr(self, "_account_tripped_reason", "")
                if tripped_reason:
                    await hub.state_change(WSState.STOPPED, f"账户熔断停队：{tripped_reason}")
                else:
                    await hub.state_change(WSState.STOPPED, "分析已停止")
            else:
                # 队列全部完成且开启 auto_summary 时，
                # 对已完成的书逐本串行执行最终总结（单本失败记录日志继续下一本；
                # 期间用户可通过既有 /api/summary/stop 停止）
                latest_cfg = self.config_manager.load()
                if latest_cfg.analysis.auto_summary:
                    await self._auto_summary_done_books(latest_cfg)
                # 自动总结期间用户可能已停止——此时不得广播“队列全部完成”
                if self._stop_requested:
                    await hub.state_change(WSState.STOPPED, "分析已停止（总结阶段中止）")
                else:
                    await hub.state_change(WSState.DONE, "队列全部完成")
            self._pipeline = None
        finally:
            self._run_mode = "idle"
            self._rerun_meta = None
            # 冻结结束时刻：任务完成/被取消/异常后 is_running 即变 False，
            # 此时 token_stats 的 elapsed 必须停止增长
            self.stats.freeze()

    async def _run_rerun(
        self,
        *,
        book_id: str,
        book_dir: Path,
        blocks_dir: Path,
        parsed_chapters: Sequence[int],
        run_cfg: AppConfig,
        update_rolling: bool,
        lock: BookFileLock,
    ) -> None:
        hub = get_hub()
        knowledge_file = book_dir / run_cfg.knowledge_file
        ch_list = list(parsed_chapters)
        ch_desc = f"第{ch_list[0]}章" if len(ch_list) == 1 else f"共{len(ch_list)}章({ch_list[0]}..{ch_list[-1]})"
        proxy_item = SimpleNamespace(
            name=book_id,
            start_time=time.time(),
            completed_chapters=0,
        )
        on_progress, on_token_stats = make_pipeline_callbacks(
            hub=hub,
            item=proxy_item,
            stats=self.stats,
            seen_characters=self._seen_characters,
        )

        try:
            await hub.log(f"定点重跑 API 预检查中（《{book_id}》{ch_desc}）...")
            if not await self.queue.check_api(run_cfg, persist_heal=False):
                await hub.log("定点重跑 API 预检查失败，请检查模型或 API 配置", level="error")
                await hub.state_change(WSState.IDLE, "API 预检查失败")
                return

            await hub.state_change(WSState.RUNNING, f"定点重跑: {book_id} ({ch_desc})")
            await hub.log(f"开始定点重跑《{book_id}》{ch_desc}")

            self._pipeline = AnalysisPipeline(
                config=run_cfg,
                directory=blocks_dir,
                knowledge_file=knowledge_file,
                target_chapters=ch_list,
                force_rerun=True,
                update_rolling=update_rolling,
                on_progress=on_progress,
                on_token_stats=on_token_stats,
            )
            result = await self._pipeline.run()

            # 若该书在队列中，按磁盘现状重算进度（不覆盖为本次重跑章数）
            for q_item in self.queue.items:
                if q_item.name == book_id:
                    q_item.completed_chapters = self.queue.infer_progress(q_item)
                    self.save_queue()
                    break

            if result.get("account_tripped"):
                safe_reason = redact(str(result.get("account_trip_reason") or "账户认证失败或余额不足"))
                await hub.log(f"定点重跑检测到账户级永久错误（{safe_reason}），已中止", level="error")
                await hub.state_change(WSState.STOPPED, f"账户熔断中止：{safe_reason}")
            elif result.get("stopped") or self._stop_requested:
                await hub.log(f"《{book_id}》定点重跑已停止（未重跑或失败章仍保留旧结果）", level="warn")
                await hub.state_change(WSState.STOPPED, "定点重跑已停止")
            elif result.get("failed_chapters"):
                await hub.log(f"《{book_id}》定点重跑部分失败: {result['failed_chapters']}", level="warn")
                await hub.state_change(WSState.DONE, f"定点重跑完成（失败{len(result['failed_chapters'])}章）")
            else:
                await hub.log(f"《{book_id}》定点重跑完成（共{result.get('total_analyzed', 0)}章，知识库已刷新）")
                await hub.state_change(WSState.DONE, f"定点重跑完成: {book_id}")
        except Exception as e:
            logger.error(f"《{book_id}》定点重跑异常: {e}", exc_info=True)
            await hub.log(f"《{book_id}》定点重跑异常: {e}", level="error")
            await hub.state_change(WSState.STOPPED, f"定点重跑异常: {e}")
        finally:
            self._pipeline = None
            self._run_mode = "idle"
            self._rerun_meta = None
            self.stats.freeze()
            try:
                lock.release()
            except Exception:
                pass

    async def _auto_summary_done_books(self, config: AppConfig) -> None:
        """队列中 status==done 的书逐本自动最终总结（延迟导入避免循环依赖：
        summary_service 顶层 import 本模块的 get_service）。"""
        from .summary_service import get_summary_service as get_summary

        hub = get_hub()
        done_books = [item.name for item in self.queue.items if item.status == "done"]
        if not done_books:
            return
        await hub.log(f"队列完成，自动总结 {len(done_books)} 本书...")
        summary = get_summary()
        batch_size = max(5, config.analysis.summary_batch_size)
        concurrency = max(1, config.analysis.summary_concurrency or config.analysis.concurrency)
        for book_id in done_books:
            if self._stop_requested:
                await hub.log("自动总结已停止（用户停止）", level="warn")
                return
            try:
                summary.start(book_id, 1, 99999, batch_size, concurrency,
                              allow_during_analysis=True)
                await hub.log(f"自动总结开始: {book_id}")
                # 等待本书总结完成（轮询；不阻塞其它逻辑，逐本串行保证 API 并发可控）
                while summary.is_running:
                    if self._stop_requested:
                        summary.stop()
                        break
                    await asyncio.sleep(1.0)
                if summary.status().get("phase") == "complete":
                    await hub.log(f"自动总结完成: {book_id}")
                else:
                    await hub.log(f"自动总结未完成或失败: {book_id}（{summary.status().get('error', '')}）", level="warn")
            except Exception as e:
                await hub.log(f"自动总结失败（{book_id}）: {e}", level="error")
        self.save_queue()

    async def _run_one_item(self, item: QueueItem, base_config: AppConfig) -> None:
        """运行单本书的完整分析"""
        hub = get_hub()

        # 每本书克隆配置：working_directory 指向该书工作区（output/ 落在其中）
        config = AppConfig.from_dict(base_config.to_dict())
        config.working_directory = str(item.workspace_dir)
        knowledge_file = item.workspace_dir / config.knowledge_file

        item.status = "running"
        item.error_message = ""
        item.block_size = max(1, config.analysis.block_size)
        item.start_time = time.time()
        self.save_queue()
        await hub.log(f"开始分析《{item.name}》（共{item.total_chapters}章）")
        await hub.state_change(WSState.RUNNING, f"分析中: {item.name}")

        # WS 事件翻译层（pipeline → ProgressHub）：工厂捕获 hub/item/stats/已见人物
        on_progress, on_token_stats = make_pipeline_callbacks(
            hub=hub, item=item, stats=self.stats,
            seen_characters=self._seen_characters)

        # token 落盘基线快照：本书开始前的累计值，finally 中做差得本书消耗
        stats_baseline = self.stats.baseline()

        self._pipeline = AnalysisPipeline(
            config=config,
            directory=item.blocks_dir,
            knowledge_file=knowledge_file,
            on_progress=on_progress,
            on_token_stats=on_token_stats,
        )

        try:
            lock = BookFileLock(item.workspace_dir / ".book.lock")
            lock.acquire()
            try:
                result = await self._pipeline.run()
                if result.get("account_tripped"):
                    raw_reason = str(result.get("account_trip_reason") or "账户认证失败或余额不足")
                    safe_reason = redact(raw_reason)
                    item.status = "pending"
                    item.error_message = f"账户熔断已停队：{safe_reason}"
                    self._account_tripped_reason = safe_reason
                    self._stop_requested = True
                    await hub.log(
                        f"检测到账户级永久错误（{safe_reason}），已自动挂起分析队列，请检查 API Key 或余额后重试",
                        level="error",
                    )
                elif result.get("stopped"):
                    item.status = "pending"  # 停止的书保持可续跑
                    await hub.log(f"《{item.name}》分析已停止（可续跑）", level="warn")
                elif result.get("failed_chapters"):
                    item.status = "done"  # 有失败章但整体完成
                    item.error_message = f"失败{len(result['failed_chapters'])}章"
                    await hub.log(
                        f"《{item.name}》完成，但有失败章节: {result['failed_chapters']}",
                        level="warn")
                elif result.get("skipped_chapters"):
                    item.status = "done"  # 有拦截跳过但整体完成
                    item.error_message = f"内容审核拦截{len(result['skipped_chapters'])}章"
                    await hub.log(
                        f"《{item.name}》完成，{len(result['skipped_chapters'])}章被内容审核拦截跳过: "
                        f"{result['skipped_chapters']}",
                        level="warn")
                else:
                    item.status = "done"
                    await hub.log(f"《{item.name}》分析完成（共{result.get('total_analyzed', 0)}章）")
            finally:
                item.end_time = time.time()
                self._pipeline = None
                self._persist_book_token_stats(item, stats_baseline)
                lock.release()
        except BookLockError as e:
            logger.warning(f"《{item.name}》正被其他进程占用，跳过本项: {e}")
            item.status = "pending"
            item.error_message = "书目正在被其他进程占用"
            item.end_time = time.time()
            self._pipeline = None
            await hub.log(f"《{item.name}》正在被其他进程占用，已跳过本次执行", level="warn")
        except Exception as e:
            logger.error(f"《{item.name}》分析异常: {e}", exc_info=True)
            item.status = "failed"
            item.error_message = str(e)
            item.end_time = time.time()
            self._pipeline = None
            await hub.log(f"《{item.name}》分析异常: {e}", level="error")

        # 完成后自动归档（可选）
        if item.status == "done" and self.config_manager.config.analysis.auto_archive:
            await asyncio.to_thread(self._archive_item, item)

    def _persist_book_token_stats(self, item: QueueItem, baseline: Dict[str, Any]) -> None:
        """本书分析 token 消耗落盘 output/token_stats.json（重启后可查历史成本）。
        失败仅记日志，不影响主流程。做差逻辑在 analysis_stats.book_consumption。"""
        try:
            from ..utils.json_utils import safe_save_json
            output_dir = item.workspace_dir / "output"
            if not output_dir.exists():
                return
            payload = self.stats.book_consumption(item, baseline)
            safe_save_json(payload, output_dir / "token_stats.json")
        except Exception as e:
            logger.warning(f"《{item.name}》token 统计落盘失败: {e}")

    def _archive_item(self, item: QueueItem) -> None:
        """归档整个书目录到 base_dir/分析结果/（失败仅标记，不影响主流程）

        shutil.move 成功后必须更新 item.workspace_dir 和
        item.blocks_dir 指向新位置，否则：
        - _books 缓存的 path 仍是旧路径（已搬走）
        - get_book_path 命中旧路径后 path.exists() False → 返回 None
        - 后续 _auto_summary_done_books 调 summary.start(book_id) 抛
          "书目不存在: 《xxx》"，即使归档后的书目录实际存在
        真实案例: 《大王饶命》分析完成后归档到 workspace/分析结果/《大王饶命》/，
        但 item.workspace_dir 还指向 workspace/《大王饶命》/（已不存在），
        auto_summary 报"书目不存在"。

        同步调 save_queue 把新路径持久化到磁盘，重启后也是对的。
        """
        try:
            archive_root = item.workspace_dir.parent / "分析结果"
            archive_root.mkdir(parents=True, exist_ok=True)
            target = archive_root / item.workspace_dir.name
            if target.exists():
                raise FileExistsError(f"归档目标已存在: {target}")
            shutil.move(str(item.workspace_dir), str(target))
            # 关键：更新 item 的路径引用，否则后续 _books 缓存旧路径
            item.workspace_dir = target
            item.blocks_dir = target / "blocks"
            self.save_queue()  # 持久化新路径（重启后也对）
            logger.info(f"《{item.name}》已归档到 {target}")
        except Exception as e:
            item.archive_failed = True
            logger.warning(f"《{item.name}》归档失败: {e}")


# 模块级单例
_service: Optional[AnalysisService] = None


def get_service() -> AnalysisService:
    global _service
    if _service is None:
        _service = AnalysisService()
    return _service
