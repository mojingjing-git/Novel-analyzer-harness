"""分析会话统计累加（AnalysisStats）
把散落在 AnalysisService 上的 7 个统计属性收敛为一个值对象。
生命周期 = 一次分析会话（start 重置 / finally 冻结）。

边界：不持有 pipeline——运行中实时 KV 缓存命中由 AnalysisService 读
`self._pipeline._analyzer` 算好后作为参数传入 session_snapshot()。
"""

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AnalysisStats:
    """一次分析会话的 token/耗时累计（分类统计 + 每章记录 + 重试成本）"""

    def __init__(self) -> None:
        # 分类 token 累计 {category: {"input_tokens": int, "output_tokens": int}}
        self.token_stats: Dict[str, Dict[str, int]] = {}
        # 每章统计记录 [(chapter, elapsed, input_tokens, output_tokens)]
        self.chapter_stats: List[Dict[str, Any]] = []
        self.total_retries = 0
        self.total_failed_tokens = 0
        # KV cache 命中 token 累计（运行中实时读当前 analyzer；结束后用累计值）
        self.total_cached_tokens = 0
        self.start_time: float = 0.0
        # 运行结束时刻（冻结耗时用：结束后 elapsed 不再随 time.time() 增长）
        self.end_time: Optional[float] = None

    def reset(self) -> None:
        """新会话开始：清空累计并记录开始时刻（AnalysisService.start() 调用）"""
        self.token_stats = {}
        self.chapter_stats = []
        self.total_retries = 0
        self.total_failed_tokens = 0
        self.total_cached_tokens = 0
        self.start_time = time.time()
        self.end_time = None

    def record_chapter(self, payload: dict) -> None:
        """记录每章统计（含重试成本），供 /api/analysis/token_stats 展示"""
        self.chapter_stats.append({
            "chapter": payload.get("chapter", 0),
            "status": payload.get("status", "done"),
            "elapsed": payload.get("elapsed", 0),
            "input_tokens": payload.get("input_tokens", 0),
            "output_tokens": payload.get("output_tokens", 0),
            "retries": payload.get("retries", 0),
            "failed_tokens": payload.get("failed_tokens", 0),
        })
        self.total_retries += payload.get("retries", 0) or 0
        self.total_failed_tokens += payload.get("failed_tokens", 0) or 0

    def accumulate_token_stats(self, payload: dict) -> None:
        """按分类累加 token。直接使用 input_tokens/output_tokens（不用 or 回退
        current_tokens：input_tokens=0 是合法值，or 会错误回退到 input+output 之和）"""
        cat = payload.get("category", "unknown")
        if cat not in self.token_stats:
            self.token_stats[cat] = {"input_tokens": 0, "output_tokens": 0}
        self.token_stats[cat]["input_tokens"] += payload.get("input_tokens", 0) or 0
        self.token_stats[cat]["output_tokens"] += payload.get("output_tokens", 0) or 0

    def baseline(self) -> Dict[str, Any]:
        """本书开始前的累计快照（token 落盘基线），
        本书 finally 中做差得本书消耗"""
        return {
            "chapter_stats_len": len(self.chapter_stats),
            "categories": {k: dict(v) for k, v in self.token_stats.items()},
            "cached": self.total_cached_tokens,
            "retries": self.total_retries,
            "failed_tokens": self.total_failed_tokens,
        }

    def freeze(self) -> None:
        """冻结结束时刻：任务完成/被取消/异常后 is_running 即变 False，
        此时 elapsed 必须停止增长"""
        self.end_time = time.time()

    def session_snapshot(self, *, running: bool, cached_tokens: int) -> Dict[str, Any]:
        """/api/analysis/token_stats 响应体。
        elapsed：结束后用冻结的 end_time，否则结束后会随当前时间无限增长。"""
        if self.end_time:
            elapsed = self.end_time - self.start_time
        elif self.start_time:
            elapsed = time.time() - self.start_time
        else:
            elapsed = 0.0
        return {
            "categories": self.token_stats,
            "chapter_stats": self.chapter_stats[-200:],  # 最近200章
            "elapsed": elapsed,
            "running": running,
            "total_retries": self.total_retries,
            "total_failed_tokens": self.total_failed_tokens,
            "cached_tokens": cached_tokens or 0,
        }

    def book_consumption(self, item, baseline: Dict[str, Any]) -> Dict[str, Any]:
        """本书消耗 = 会话累计 - 本书开始前基线（output/token_stats.json 的 payload）"""
        categories = {}
        for cat, v in self.token_stats.items():
            before = baseline["categories"].get(cat, {"input_tokens": 0, "output_tokens": 0})
            categories[cat] = {
                "input_tokens": v["input_tokens"] - before["input_tokens"],
                "output_tokens": v["output_tokens"] - before["output_tokens"],
            }
        return {
            "type": "analysis",
            "book_id": item.name,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": (item.end_time or time.time()) - (item.start_time or time.time()),
            "categories": categories,
            "total_retries": self.total_retries - baseline["retries"],
            "total_failed_tokens": self.total_failed_tokens - baseline["failed_tokens"],
            "cached_tokens": self.total_cached_tokens - baseline["cached"],
            "chapter_stats": self.chapter_stats[baseline["chapter_stats_len"]:],
        }
