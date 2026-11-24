"""
最终总结编排服务（单例）
- start(): 为指定书启动 FinalSummaryRunner asyncio.Task
- stop(): 请求停止（重试链下一检查点退出）
- 进度回调 → ProgressHub 广播（WS 消息 type=summary_progress / token_stats）
- 完成后报告写入 output_dir/final_summary_report.md（对齐旧 main_window 行为）
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional, Dict, Any

from ..config.settings import AppConfig
from ..progress_hub import get_hub
from .final_summary import FinalSummaryRunner
from ..ws_events import WSType, WSState
from .queue_service import get_service as get_analysis_service
from . import book_service

logger = logging.getLogger(__name__)


class SummaryService:
    """最终总结运行编排层（单例，同一时刻只允许一个总结任务）"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._runner: Optional[FinalSummaryRunner] = None
        self._book_id: str = ""
        self._phase: str = "idle"
        self._error: str = ""
        self._batches_done: int = 0
        self._total_batches: int = 0
        self._summary_done: int = 0
        self._recon_done: int = 0
        self._recheck_progress: Dict[str, Any] = {}
        self._started_at: float = 0.0
        self._finished_at: float = 0.0
        self._concurrency: int = 1
        # 本次窗口会话的总结 token 累计（内存态，重启清零；跨书累计）
        self._session_token_stats: Dict[str, Dict[str, int]] = {}
        self._session_elapsed: float = 0.0

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.is_running,
            "book_id": self._book_id,
            "phase": self._phase,
            "batches_done": self._batches_done,
            "total_batches": self._total_batches,
            "summary_done": self._summary_done,
            "recon_done": self._recon_done,
            "error": self._error,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "concurrency": getattr(self._runner, "_active_concurrency", self._concurrency) if (self.is_running and self._runner) else self._concurrency,
            "inflight_lanes": self._runner.get_inflight_lanes() if (self.is_running and self._runner and hasattr(self._runner, "get_inflight_lanes")) else [],
            "token_stats": self._runner.get_token_stats() if self._runner else {},
            "recheck_progress": dict(self._recheck_progress),
            # 复检的账（状态/覆盖计数/明细文件）：总结页直接展示，跑完不用翻文件。
            # getattr 容错：测试里的假 runner 不带这个属性，属正常形态
            "recheck": (getattr(self._runner, "last_recheck_summary", None) or {}) if self._runner else {},
        }

    def start(self, book_id: str, start_chapter: int, end_chapter: int,
              batch_size: Optional[int] = None, concurrency: Optional[int] = None,
              allow_during_analysis: bool = False) -> None:
        """启动总结任务。已在运行抛 RuntimeError，书不存在抛 KeyError，无结果抛 ValueError

        allow_during_analysis：仅供队列自动总结使用——该场景在
        队列分析任务自身的收尾阶段（_run_queue 内）调用，get_analysis_service()
        仍为 running，但所有章节已分析完毕、result.json 已完整，读取是安全的。
        普通用户入口（/api/summary/start）保持护栏：分析期间禁止手动总结。
        """
        if self.is_running:
            raise RuntimeError("总结任务已在运行中")
        # 并发护栏：分析正在运行时启动总结会读取半成品 chapter_*_result.json，导致报告错乱
        if not allow_during_analysis and get_analysis_service().is_running:
            raise RuntimeError("分析任务正在进行中，请先停止或等待其完成后再启动最终总结")

        output_dir = book_service.get_output_dir(book_id)
        if output_dir is None:
            raise KeyError(f"书目不存在: {book_id}")
        if not output_dir.exists() or not any(output_dir.glob("chapter_*_result.json")):
            raise ValueError(f"《{book_id}》尚无章节分析结果")

        # 每次启动重读配置（与 AnalysisService 一致）
        config: AppConfig = get_analysis_service().config_manager.load()
        if not batch_size or batch_size <= 0:
            batch_size = getattr(config.analysis, "summary_batch_size", None) or 30
        resolved_batch_size = max(1, int(batch_size))
        if not concurrency or concurrency <= 0:
            concurrency = getattr(config.analysis, "summary_concurrency", None) or config.analysis.concurrency
        self._concurrency = max(1, int(concurrency))

        self._book_id = book_id
        self._phase = "starting"
        self._error = ""
        self._batches_done = 0
        self._total_batches = 0
        self._summary_done = 0
        self._recon_done = 0
        self._recheck_progress = {}
        self._started_at = time.time()
        self._finished_at = 0.0

        hub = get_hub()

        def on_progress(payload: dict) -> None:
            """runner 进度（事件循环内的同步回调）→ 状态更新 + WS 广播"""
            ptype = payload.get("type", "")
            if ptype == "phase":
                self._phase = payload.get("phase", self._phase)
                self._total_batches = payload.get("total_batches", self._total_batches)
                if self._phase in ("recheck", "style", "report"):
                    self._recheck_progress = {}
            elif ptype == "stage1_progress":
                if payload.get("summary_done") is not None:
                    self._summary_done = int(payload["summary_done"])
                if payload.get("recon_done") is not None:
                    self._recon_done = int(payload["recon_done"])
                self._total_batches = payload.get("total_batches", self._total_batches)
            elif ptype == "batch_done":
                self._batches_done += 1
                self._total_batches = payload.get("total_batches", self._total_batches)
                self._summary_done = max(self._summary_done, self._batches_done)
                self._recon_done = max(self._recon_done, self._batches_done)
            elif ptype == "recheck_progress":
                rp = payload.get("recheck_progress")
                if isinstance(rp, dict):
                    self._recheck_progress = dict(rp)
            elif ptype in ("recheck_done", "complete"):
                if ptype == "complete":
                    self._phase = "complete"
                self._recheck_progress = {}
            message = payload.get("message", "")
            if message:
                level = "error" if ptype == "batch_failed" else "info"
                asyncio.create_task(hub.log(message, level=level, category="summary"))
            asyncio.create_task(hub.publish({
                "type": WSType.SUMMARY_PROGRESS,
                "payload": {
                    **payload,
                    "book_id": book_id,
                    "batches_done": self._batches_done,
                    "summary_done": self._summary_done,
                    "recon_done": self._recon_done,
                },
            }))

        def on_token_stats(payload: dict) -> None:
            # 持久账本（跨会话累计；模型名来自 FinalSummaryRunner 的 payload）
            from .usage_ledger import get_usage_ledger
            _ledger = get_usage_ledger()
            _ledger.add_token(payload)
            _ledger.schedule_save()
            asyncio.create_task(hub.publish({
                "type": WSType.TOKEN_STATS,
                "payload": {**payload, "source": "summary"},
            }))

        self._runner = FinalSummaryRunner(
            config=config,
            output_dir=output_dir,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            batch_size=resolved_batch_size,
            concurrency=self._concurrency,
            on_progress=on_progress,
            on_token_stats=on_token_stats,
        )
        self._task = asyncio.create_task(self._run(output_dir))

    def session_token_stats(self) -> Dict[str, Any]:
        """本次窗口会话的总结 token 累计（内存态，重启清零）"""
        total = {"input_tokens": 0, "output_tokens": 0}
        for v in self._session_token_stats.values():
            total["input_tokens"] += v.get("input_tokens", 0)
            total["output_tokens"] += v.get("output_tokens", 0)
        return {
            "categories": dict(self._session_token_stats),
            "input_tokens": total["input_tokens"],
            "output_tokens": total["output_tokens"],
            "elapsed": self._session_elapsed,
        }

    def stop(self) -> bool:
        if not self.is_running:
            return False
        if self._runner is not None:
            self._runner.stop()
        return True

    async def _run(self, output_dir: Path) -> None:
        hub = get_hub()
        await hub.state_change(WSState.SUMMARY_RUNNING, f"最终总结开始: {self._book_id}")
        try:
            report = await self._runner.run()
            if report is None:
                self._phase = "stopped"
                await hub.state_change(WSState.SUMMARY_STOPPED, f"总结已停止: {self._book_id}")
            else:
                report_path = output_dir / "final_summary_report.md"
                await asyncio.to_thread(report_path.write_text, report, encoding="utf-8")
                logger.info(f"全书脉络报告已保存: {report_path}")
                self._phase = "complete"
                await hub.state_change(WSState.SUMMARY_DONE, f"总结完成: {self._book_id}")
        except Exception as e:
            logger.error(f"最终总结异常: {e}", exc_info=True)
            self._phase = "failed"
            self._error = str(e)
            await hub.log(f"最终总结失败: {e}", level="error")
            await hub.state_change(WSState.SUMMARY_FAILED, str(e))
        finally:
            self._finished_at = time.time()
            # 会话累计 + 总结 token 落盘：历史成本可查，重启不丢
            if self._runner is not None:
                try:
                    stats = self._runner.get_token_stats()
                    if stats:
                        # 会话累计（内存态，本次窗口跨书汇总；重启清零）
                        for cat, pair in stats.items():
                            entry = self._session_token_stats.setdefault(
                                cat, {"input_tokens": 0, "output_tokens": 0})
                            entry["input_tokens"] += int(pair.get("input_tokens") or 0)
                            entry["output_tokens"] += int(pair.get("output_tokens") or 0)
                        if self._started_at:
                            self._session_elapsed += self._finished_at - self._started_at
                        from ..utils.json_utils import safe_save_json
                        payload = {
                            "type": "summary",
                            "book_id": self._book_id,
                            "phase": self._phase,
                            "started_at": self._started_at,
                            "finished_at": self._finished_at,
                            "elapsed": self._finished_at - self._started_at if self._started_at else 0,
                            "categories": stats,
                        }
                        await asyncio.to_thread(safe_save_json, payload,
                                                output_dir / "summary_token_stats.json")
                except Exception as e:
                    logger.warning(f"总结 token 统计落盘失败: {e}")


# 模块级单例
_service: Optional[SummaryService] = None


def get_summary_service() -> SummaryService:
    global _service
    if _service is None:
        _service = SummaryService()
    return _service
