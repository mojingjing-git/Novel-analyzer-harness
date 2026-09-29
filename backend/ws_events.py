"""WS 事件协议（类型常量 + payload 模型 + 工厂函数）——本文件即协议文档。

前端镜像定义：frontend/src/api/useProgressSocket.ts 的 ProgressMessage 可辨识联合。
**wire 格式约定**：`{"type": <WSType>, "payload": {...}}`；修改任何字段必须同步前端
类型定义。所有工厂都是**纯同步函数**（HubLogHandler 在同步线程上下文调用）。
"""

from enum import StrEnum
from typing import Any, Dict, List, Optional


class WSType(StrEnum):
    """wire 层消息 type 全集"""

    LOG = "log"
    PROGRESS = "progress"
    BLOCK_START = "block_start"
    BLOCK_DONE = "block_done"
    DISCOVERY = "discovery"
    TOKEN_STATS = "token_stats"
    TOKEN_DELTA = "token_delta"
    STATE_CHANGE = "state_change"
    SUMMARY_PROGRESS = "summary_progress"
    LOCATION_NORMALIZATION_PROGRESS = "location_normalization_progress"


class WSState(StrEnum):
    """state_change 消息的 state 取值全集（分析 / 总结 / 归一化三个域）"""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    DONE = "done"
    SUMMARY_RUNNING = "summary_running"
    SUMMARY_STOPPED = "summary_stopped"
    SUMMARY_DONE = "summary_done"
    SUMMARY_FAILED = "summary_failed"
    LOCATION_NORMALIZATION_RUNNING = "location_normalization_running"
    LOCATION_NORMALIZATION_DONE = "location_normalization_done"
    LOCATION_NORMALIZATION_STOPPED = "location_normalization_stopped"
    LOCATION_NORMALIZATION_FAILED = "location_normalization_failed"


class PipelineStatus(StrEnum):
    """pipeline 内部 _emit 回调协议的 status 取值（queue_service.on_progress 翻译层消费）"""

    START = "start"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # 内容审核拦截（必须翻译为 block_done，否则前端车道泄漏）
    ROLLING_STARTED = "rolling_started"
    ROLLING_UPDATED = "rolling_updated"
    FAILED_SUMMARY = "failed_summary"


# ---- payload 契约（TypedDict 供文档与前后端镜像；工厂函数按下述结构构造）----
# LogPayload:            {level: "info"|"warn"|"error", text: str,
#                         source: "business"|"python", category: str}
# ProgressPayload:       {current: int, total: int, eta: str}
# BlockStartPayload:     {chapter: int, range: str, progress: int, total: int, ts: float}
# BlockDonePayload:      {chapter: int, ok: bool, range: str,
#                         elapsed?: float|null, tokens?: int|list|null}
#                         （续跑补发路径 tokens 为 (0,0) 元组 → JSON 数组）
# DiscoveryPayload:      {events: int, foreshadows: list[str], characters: list[str],
#                         unresolved: list[str]}
# TokenStatsPayload:     {category: str, input_tokens: int, output_tokens: int,
#                         current_tokens?: int, source?: "summary"}
# TokenDeltaPayload:     {context: str, session_id: str, unit_idx: int,
#                         delta: {...}, timestamp: float}
# StateChangePayload:    {state: WSState, detail: str}
# SummaryProgressPayload / LocationNormalizationProgressPayload：
#     宽松 Dict（final_summary / location_normalizer 透传 + book_id/batches_done 注入），
#     子类型见 final_summary._emit_progress：status / phase / batch_done /
#     batch_failed / ledger_updated / complete；前端只读 type/phase/total_batches/
#     batch/message/batches_done。


def log_event(level: str, text: str, source: str = "business", category: str = "") -> Dict[str, Any]:
    return {
        "type": WSType.LOG,
        "payload": {"level": level, "text": text, "source": source, "category": category},
    }


def progress_event(current: int, total: int, eta: str = "") -> Dict[str, Any]:
    return {
        "type": WSType.PROGRESS,
        "payload": {"current": current, "total": total, "eta": eta},
    }


def block_start_event(chapter: int, range_text: str, progress: int, total: int, ts: float) -> Dict[str, Any]:
    return {
        "type": WSType.BLOCK_START,
        "payload": {"chapter": chapter, "range": range_text, "progress": progress, "total": total, "ts": ts},
    }


def block_done_event(chapter: int, ok: bool, range_text: str = "",
                     elapsed: Optional[float] = None, tokens: Optional[Any] = None) -> Dict[str, Any]:
    """payload 恒含 range/elapsed/tokens 三键（elapsed/tokens 可为 null——
    与既有 wire 格式逐键一致；原先 progress_hub.block_done 便捷方法因形状
    不同且无调用方已删除）"""
    return {
        "type": WSType.BLOCK_DONE,
        "payload": {
            "chapter": chapter,
            "ok": ok,
            "range": range_text,
            "elapsed": elapsed,
            "tokens": tokens,
        },
    }


def discovery_event(events: int, foreshadows: List[str], characters: List[str],
                    unresolved: List[str]) -> Dict[str, Any]:
    return {
        "type": WSType.DISCOVERY,
        "payload": {
            "events": events,
            "foreshadows": foreshadows,
            "characters": characters,
            "unresolved": unresolved,
        },
    }


def state_change_event(state: WSState, detail: str = "") -> Dict[str, Any]:
    return {"type": WSType.STATE_CHANGE, "payload": {"state": state, "detail": detail}}


def token_delta_event(context: str, session_id: str, unit_idx: int,
                      delta: Dict[str, Any], timestamp: float) -> Dict[str, Any]:
    return {
        "type": WSType.TOKEN_DELTA,
        "payload": {
            "context": context,
            "session_id": session_id,
            "unit_idx": unit_idx,
            "delta": delta,
            "timestamp": timestamp,
        },
    }
