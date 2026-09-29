"""
地点归一化编排服务（单例）
- start(book_id): 异步启动 LocationNormalizer run()
- stop(): 请求停止
- 进度回调 → ProgressHub 广播（WS 消息 type=location_normalization_progress）
"""
import asyncio
import logging
import time
from dataclasses import replace
from typing import Optional, Dict, Any

from ..config.settings import AppConfig
from ..progress_hub import get_hub
from ..ws_events import WSType, WSState
from .location_normalizer import LocationNormalizer
from . import book_service
from ..core.llm_client import LLMClient

logger = logging.getLogger(__name__)


class LocationNormalizationService:
    """地点归一化编排层（单例，同一时刻只允许一个任务）"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._normalizer: Optional[LocationNormalizer] = None
        self._llm: Optional[LLMClient] = None
        self._book_id: str = ""
        self._phase: str = "idle"
        self._error: str = ""
        self._batches_done: int = 0
        self._total_batches: int = 0
        self._started_at: float = 0.0
        self._finished_at: float = 0.0
        self._token_stats: Dict[str, Dict[str, int]] = {}

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
            "error": self._error,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "token_stats": dict(self._token_stats),
        }

    def start(self, book_id: str, config: AppConfig) -> None:
        """启动归一化任务。已在运行抛 RuntimeError，书不存在抛 KeyError，无结果抛 ValueError"""
        if self.is_running:
            raise RuntimeError("地点归一化任务已在运行中")

        output_dir = book_service.get_output_dir(book_id)
        if output_dir is None:
            raise KeyError(f"书目不存在: {book_id}")
        if not output_dir.exists() or not any(output_dir.glob("chapter_*_result.json")):
            raise ValueError(f"《{book_id}》尚无章节分析结果，无法归一化")

        self._book_id = book_id
        self._phase = "starting"
        self._error = ""
        self._batches_done = 0
        self._total_batches = 0
        self._started_at = time.time()
        self._finished_at = 0.0
        self._token_stats = {}

        api_cfg = replace(
            config.api,
            model=config.api.summary_model or config.api.model,
            json_mode="default",
            timeout=config.api.summary_timeout,
            thinking_mode=config.api.summary_thinking_mode or config.api.thinking_mode,
        )
        self._llm = LLMClient(api_cfg)

        hub = get_hub()

        def on_progress(payload: dict) -> None:
            ptype = payload.get("type", "")
            if ptype == "phase":
                self._phase = payload.get("phase", self._phase)
                self._total_batches = payload.get("total_batches", self._total_batches)
            elif ptype == "batch_done":
                self._batches_done += 1
                self._total_batches = payload.get("total_batches", self._total_batches)
                self._phase = payload.get("phase", self._phase)
            message = payload.get("message", "")
            if message:
                asyncio.create_task(hub.log(message, level="info", category="location-normalization"))
            asyncio.create_task(hub.publish({
                "type": WSType.LOCATION_NORMALIZATION_PROGRESS,
                "payload": {**payload, "book_id": book_id, "batches_done": self._batches_done},
            }))

        self._normalizer = LocationNormalizer(
            output_dir=output_dir,
            llm_client=self._llm,
            concurrency=config.analysis.concurrency,
            on_progress=on_progress,
        )
        self._task = asyncio.create_task(self._run())

    def stop(self) -> bool:
        if not self.is_running:
            return False
        if self._normalizer is not None:
            self._normalizer.stop()
        return True

    async def _run(self) -> None:
        hub = get_hub()
        await hub.state_change(WSState.LOCATION_NORMALIZATION_RUNNING, f"地点归一化开始: {self._book_id}")
        normalizer = self._normalizer
        assert normalizer is not None
        try:
            ok = await normalizer.run()
            self._finished_at = time.time()
            if ok is True:
                self._phase = "complete"
                await hub.state_change(WSState.LOCATION_NORMALIZATION_DONE, f"地点归一化完成: {self._book_id}")
            elif ok is None:
                self._phase = "stopped"
                await hub.state_change(WSState.LOCATION_NORMALIZATION_STOPPED, f"地点归一化已停止: {self._book_id}")
            else:
                self._phase = "failed"
                self._error = "归一化失败（部分 batch 失败率过高）"
                await hub.state_change(WSState.LOCATION_NORMALIZATION_FAILED, self._error)
        except Exception as e:
            logger.error(f"地点归一化异常: {e}", exc_info=True)
            self._phase = "failed"
            self._error = str(e)
            self._finished_at = time.time()
            await hub.state_change(WSState.LOCATION_NORMALIZATION_FAILED, str(e))


_service: Optional[LocationNormalizationService] = None


def get_location_normalization_service() -> LocationNormalizationService:
    global _service
    if _service is None:
        _service = LocationNormalizationService()
    return _service
