"""持久用量账本（UsageLedger）

统计面板的固化底座：跨进程、跨会话**只增不减**的 token 累计。
与 AnalysisStats（会话纯值对象，每次分析 start 即 reset）彻底解耦——
账本挂在回调分发层（pipeline_events / SummaryService.on_token_stats），
不感知会话生命周期。

口径：
- per_model 维度是费用唯一依据（跨书换模型不会串价）；
- categories 只作 token 汇总展示，category_models 是展示用模型名单；
- 缓存命中按 payload 直读的 delta 累计（OpenAI 协议 prompt_tokens 已含
  命中部分，费用侧由 calc_model_cost 拆分计价）；
- 失败/重试 token 单列（failed_tokens/retries），不进费用。
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

LEDGER_VERSION = 1
#: 节流窗口（秒）：运行期至多每 2s 落盘一次，批末由 trailing timer 补写
SAVE_DEBOUNCE_SECONDS = 2.0


def _empty_state() -> Dict[str, Any]:
    return {
        "version": LEDGER_VERSION,
        "updated_at": None,
        "per_model": {},        # {model: {in, out, cached}}
        "categories": {},       # {category: {in, out}}   仅展示，不算费
        "category_models": {},  # {category: [model...]}  展示用名单
        "chapters_done": 0,
        "retries": 0,
        "failed_tokens": 0,
        "elapsed_seconds": 0.0,
    }


class UsageLedger:
    """全局累计账本。线程安全（锁内改状态、锁外拷快照写盘）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._state = _empty_state()
        self._dirty = False
        self._last_save = 0.0
        self._save_timer: Optional[Any] = None  # asyncio.TimerHandle
        self.load()

    # ---------- 加载 / 清零 ----------

    def load(self) -> None:
        """启动加载。文件缺失/损坏一律降级为空账本（不崩、记日志）。"""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            with self._lock:
                state = _empty_state()
                for key in state:
                    if key in data:
                        state[key] = data[key]
                self._state = state
                self._dirty = False
            logger.info("用量账本已加载：%s（per_model %d 项）",
                        self.path.name, len(self._state["per_model"]))
        except Exception as e:
            logger.warning("用量账本加载失败，降级为空账本：%s（%s）", self.path, e)

    def clear(self) -> None:
        """显式清零（旧文件备份 .bak）。"""
        with self._lock:
            try:
                if self.path.exists():
                    shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
            except Exception as e:
                logger.warning("账本备份失败（继续清零）：%s", e)
            self._state = _empty_state()
            self._dirty = True
        self.save_sync()

    # ---------- 累积入口 ----------

    def add_token(self, payload: dict) -> None:
        """token 事件（分析侧 _emit_tokens / 总结侧 on_token_stats 的 payload）。"""
        model = (payload.get("model") or "").strip()
        in_tok = int(payload.get("input_tokens") or 0)
        out_tok = int(payload.get("output_tokens") or 0)
        if not model and in_tok == 0 and out_tok == 0:
            return
        if not model:
            model = "unknown"
        cat = payload.get("category") or "unknown"
        cached = int(payload.get("cached_tokens") or 0)
        with self._lock:
            pm = self._state["per_model"].setdefault(model, {"in": 0, "out": 0, "cached": 0})
            pm["in"] += in_tok
            pm["out"] += out_tok
            pm["cached"] += cached
            cat_entry = self._state["categories"].setdefault(cat, {"in": 0, "out": 0})
            cat_entry["in"] += in_tok
            cat_entry["out"] += out_tok
            models = self._state["category_models"].setdefault(cat, [])
            if model not in models:
                models.append(model)
            self._mark_dirty()

    def add_chapter(self, payload: dict) -> None:
        """每章统计（耗时/重试/失败/缓存 delta；成功才计章数）。"""
        elapsed = float(payload.get("elapsed") or 0)
        retries = int(payload.get("retries") or 0)
        failed = int(payload.get("failed_tokens") or 0)
        cached = int(payload.get("cached_tokens") or 0)
        with self._lock:
            if payload.get("status", "done") == "done":
                self._state["chapters_done"] += 1
            self._state["retries"] += retries
            self._state["failed_tokens"] += failed
            self._state["elapsed_seconds"] += elapsed
            # 缓存命中挂在 per_model：chapter payload 的 model 字段（分析侧恒有）
            model = (payload.get("model") or "").strip()
            if model and cached:
                pm = self._state["per_model"].setdefault(model, {"in": 0, "out": 0, "cached": 0})
                pm["cached"] += cached
            self._mark_dirty()

    def _mark_dirty(self) -> None:
        self._state["updated_at"] = datetime_iso()
        self._dirty = True

    # ---------- 读取 ----------

    def snapshot(self) -> Dict[str, Any]:
        """账本快照（深拷贝，供路由/落盘，调用方可随意改动）。"""
        with self._lock:
            return copy.deepcopy(self._state)

    # ---------- 落盘 ----------

    def schedule_save(self, loop=None) -> None:
        """运行期节流落盘：距上次 ≥2s 立即异步写，否则挂 trailing timer 补写。

        在事件循环内调用（回调都是 async 上下文）；拿不到 loop 时退化为同步写。
        """
        with self._lock:
            if not self._dirty:
                return
            due = time.monotonic() - self._last_save >= SAVE_DEBOUNCE_SECONDS
        if due:
            self._do_async_save(loop)
            return
        # 未到窗口：挂 trailing timer（幂等，已有 timer 就等它）
        if self._save_timer is not None:
            return
        try:
            import asyncio
            loop = loop or asyncio.get_running_loop()
            remaining = SAVE_DEBOUNCE_SECONDS - (time.monotonic() - self._last_save)
            self._save_timer = loop.call_later(
                max(remaining, 0.05), lambda: self._do_async_save(loop))
        except RuntimeError:
            self.save_sync()

    def _do_async_save(self, loop=None) -> None:
        """异步落盘（事件循环内）：拷快照后 to_thread 写，不阻塞分析。"""
        with self._lock:
            if self._save_timer is not None:
                self._save_timer = None
            if not self._dirty:
                return
            snapshot = copy.deepcopy(self._state)
            self._dirty = False
            self._last_save = time.monotonic()

        async def _write() -> None:
            import asyncio
            try:
                await asyncio.to_thread(self._write_snapshot, snapshot)
            except Exception as e:
                logger.warning("用量账本异步落盘失败：%s", e)

        try:
            import asyncio
            loop = loop or asyncio.get_running_loop()
            loop.create_task(_write())
        except RuntimeError:
            self._write_snapshot(snapshot)

    def save_sync(self) -> None:
        """同步落盘：进程退出 / 清零等关键时机调用（atexit / lifespan shutdown）。"""
        with self._lock:
            if self._save_timer is not None:
                self._save_timer = None
            if not self._dirty:
                return
            snapshot = copy.deepcopy(self._state)
            self._dirty = False
            self._last_save = time.monotonic()
        self._write_snapshot(snapshot)

    def _write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


def datetime_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ---------- 进程级单例 ----------

from ..services.queue_manager import PROJECT_ROOT  # noqa: E402  (复用落盘根惯例)

_LEDGER: Optional[UsageLedger] = None
_LEDGER_LOCK = threading.Lock()


def get_usage_ledger() -> UsageLedger:
    global _LEDGER
    with _LEDGER_LOCK:
        if _LEDGER is None:
            _LEDGER = UsageLedger(PROJECT_ROOT / "usage_state.json")
        return _LEDGER
