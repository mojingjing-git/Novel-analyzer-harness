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
from typing import Optional, Dict, Any

from ..config.settings import AppConfig, ConfigManager
from ..core.pipeline import AnalysisPipeline
from ..progress_hub import get_hub
from .analysis_stats import AnalysisStats
from .pipeline_events import make_pipeline_callbacks
from ..ws_events import WSState
from .queue_manager import CONFIG_FILE, PROJECT_ROOT, QUEUE_STATE_FILE, QueueItem, QueueManager

logger = logging.getLogger(__name__)


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
        # 复审 Important #2：check_api 的 heal 纠正值需经 config_manager 落盘
        # 持久化（check_api 以 self.queue 身份执行，getattr(self, "config_manager")
        # 取的就是这里挂上的引用）
        self.queue.config_manager = self.config_manager
        self.queue.load_state(QUEUE_STATE_FILE)
        self._runner_task: Optional[asyncio.Task] = None
        self._pipeline: Optional[AnalysisPipeline] = None
        self._stop_requested = False
        # 一次分析会话的 token/耗时累计（分类统计/每章记录/重试成本/KV 命中），
        # 生命周期 = start 重置 → finally 冻结（实现见 analysis_stats.py）
        self.stats = AnalysisStats()
        # H17 (2026-08-26) 发现流：跨块"已见人物"集合（断点续跑后全量"首次"是已知限制）
        self._seen_characters: set = set()
        # 注意：启动期自动扫描已迁移到 app.py 的 lifespan 启动阶段（asyncio.to_thread 执行），
        # 不再在此处同步执行——否则首个触发 get_service() 的 async 请求会在事件循环内
        # 同步跑完整工作区扫描（网络盘多书时卡数秒~数十秒），冻结整个事件循环。

    # ---- 状态 ----
    @property
    def is_running(self) -> bool:
        return self._runner_task is not None and not self._runner_task.done()

    def status(self) -> Dict[str, Any]:
        # H17 P3 V2 修复（2026-08-26）：暴露 concurrency / block_size，
        # 前端 LaneView 之前误用 block_size（每块几章）当作 concurrency（并发块数），
        # 导致「并发车道 N/4」实际是「N/每块4章」——配置 concurrency=8 时实际 8 路并发但 UI 标 4。
        cm = getattr(self, "config_manager", None)
        analysis_cfg = getattr(getattr(cm, "config", None), "analysis", None) if cm else None
        # getattr 兜底：部分单测用 __new__ 构造实例（无 __init__ 属性）
        pipeline = getattr(self, "_pipeline", None)
        return {
            "running": self.is_running,
            "queue": self.queue.get_progress(),
            "items": [item.to_dict() for item in self.queue.items],
            "workspace_dir": self.workspace_path.as_posix(),
            "concurrency": getattr(analysis_cfg, "concurrency", None) if analysis_cfg else None,
            "block_size": getattr(analysis_cfg, "block_size", None) if analysis_cfg else None,
            # 运行概览车道对账源：pipeline 信号量窗口内的真实在途块（≤ concurrency，
            # 零丢失）。前端 WS 记账丢 done 时以此整体纠偏（车道堆叠修复）
            "inflight_blocks": pipeline.inflight_blocks() if pipeline is not None else [],
        }

    def token_stats(self) -> Dict[str, Any]:
        """返回分类 token 统计 + 每章记录 + 总耗时 + KV 缓存命中数（EFF-5）"""
        # KV cache 命中：运行中读当前 analyzer（实时）；结束后用累计值
        # getattr 兜底：部分单测用 __new__ 构造实例（无 __init__ 属性）
        cached_tokens = self.stats.total_cached_tokens
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is not None:
            analyzer = getattr(pipeline, '_analyzer', None)
            if analyzer is not None:
                try:
                    cached_tokens = analyzer.get_token_stats().get('cached_tokens', 0)
                except Exception:
                    cached_tokens = self.stats.total_cached_tokens
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
            done_blocks = self.queue.infer_progress(item)
            item.completed_chapters = done_blocks
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
            done_blocks = self.queue.infer_progress(item)
            item.completed_chapters = done_blocks
            idx = self.queue.add_item(item)
            if idx == len(self.queue.items) - 1:
                added += 1
        if added > 0:
            self.save_queue()
        return added

    # ---- 控制 ----
    def start(self) -> bool:
        """启动队列分析（已在运行/队列空/总结运行中则返回 False）"""
        if self.is_running:
            logger.warning("分析已在运行中")
            return False
        # 双向互斥：总结运行期间启动分析会并发读写 output/，总结读到半成品数据
        # （summary_service.start 已拦分析中启动总结，此处补反向护栏）
        try:
            from .summary_service import get_summary_service as get_summary
            if get_summary().is_running:
                logger.warning("总结任务运行中，不能启动分析")
                return False
        except Exception:
            pass
        if self.queue.is_empty or self.queue.is_finished:
            logger.warning("队列为空或已全部完成")
            return False
        self._stop_requested = False
        # P1-b (2026-08-26)：重置"已见人物"集合，避免上一本书的人物污染新书首次登场判定
        # （AnalysisService 是进程级单例，__init__ 只跑一次）
        self._seen_characters = set()
        # 新会话：清空 token/耗时累计并记录开始时刻
        self.stats.reset()
        self._runner_task = asyncio.create_task(self._run_queue())
        return True

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
                await hub.state_change(WSState.STOPPED, "分析已停止")
            else:
                # 2026-08-02 spec：队列全部完成且开启 auto_summary 时，
                # 对已完成的书逐本串行执行最终总结（单本失败记录日志继续下一本；
                # 期间用户可通过既有 /api/summary/stop 停止）
                if config.analysis.auto_summary:
                    await self._auto_summary_done_books(config)
                # P2 修复：自动总结期间用户可能已停止——此时不得广播“队列全部完成”
                if self._stop_requested:
                    await hub.state_change(WSState.STOPPED, "分析已停止（总结阶段中止）")
                else:
                    await hub.state_change(WSState.DONE, "队列全部完成")
            self._pipeline = None
        finally:
            # 冻结结束时刻：任务完成/被取消/异常后 is_running 即变 False，
            # 此时 token_stats 的 elapsed 必须停止增长
            self.stats.freeze()

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
        concurrency = max(1, config.analysis.summary_concurrency)
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
        item.block_size = max(1, config.analysis.block_size)
        item.start_time = time.time()
        self.save_queue()
        await hub.log(f"开始分析《{item.name}》（共{item.total_chapters}章）")
        await hub.state_change(WSState.RUNNING, f"分析中: {item.name}")

        # WS 事件翻译层（pipeline → ProgressHub）：工厂捕获 hub/item/stats/已见人物
        on_progress, on_token_stats = make_pipeline_callbacks(
            hub=hub, item=item, stats=self.stats,
            seen_characters=self._seen_characters)

        # token 落盘基线快照（2026-08-17）：本书开始前的累计值，finally 中做差得本书消耗
        stats_baseline = self.stats.baseline()

        self._pipeline = AnalysisPipeline(
            config=config,
            directory=item.blocks_dir,
            knowledge_file=knowledge_file,
            on_progress=on_progress,
            on_token_stats=on_token_stats,
        )

        try:
            result = await self._pipeline.run()
            if result.get("stopped"):
                item.status = "pending"  # 停止的书保持可续跑
                await hub.log(f"《{item.name}》分析已停止（可续跑）", level="warn")
            elif result.get("failed_chapters"):
                item.status = "done"  # 有失败块但整体完成
                item.error_message = f"失败{len(result['failed_chapters'])}块"
                await hub.log(
                    f"《{item.name}》完成，但有失败块: {result['failed_chapters']}",
                    level="warn")
            elif result.get("skipped_chapters"):
                item.status = "done"  # 有拦截跳过但整体完成
                item.error_message = f"内容审核拦截{len(result['skipped_chapters'])}块"
                await hub.log(
                    f"《{item.name}》完成，{len(result['skipped_chapters'])}块被内容审核拦截跳过: "
                    f"{result['skipped_chapters']}",
                    level="warn")
            else:
                item.status = "done"
                await hub.log(f"《{item.name}》分析完成（{result.get('total_analyzed', 0)}块）")
        except Exception as e:
            logger.error(f"《{item.name}》分析异常: {e}", exc_info=True)
            item.status = "failed"
            item.error_message = str(e)
            await hub.log(f"《{item.name}》分析异常: {e}", level="error")
        finally:
            item.end_time = time.time()
            # EFF-5：pipeline 置 None 前累计本书 KV 缓存命中数（结束后 token_stats 仍可见）
            if self._pipeline is not None:
                analyzer = getattr(self._pipeline, '_analyzer', None)
                if analyzer is not None:
                    try:
                        self.stats.total_cached_tokens += analyzer.get_token_stats().get('cached_tokens', 0) or 0
                    except Exception:
                        pass
            self._pipeline = None
            self._persist_book_token_stats(item, stats_baseline)

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

        P5d 修复（2026-08-27）：shutil.move 成功后必须更新 item.workspace_dir 和
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
