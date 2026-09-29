"""WS 事件翻译层（pipeline → ProgressHub）
自 queue_service.py 拆出（2026-08-31 职责拆分）：把 AnalysisPipeline 的
on_progress/on_token_stats 回调从编排层的嵌套闭包改为显式工厂。

捕获面（复审确认恰好完整）：hub（广播）、item（写 completed_chapters、
读 start_time）、stats（每章统计 + token 累计）、seen_characters（发现流求差）。
save_queue 不在此层——其调用点都在 AnalysisService 方法里。
"""

import time
from typing import List, Optional

from ..ws_events import (
    PipelineStatus,
    block_done_event, block_start_event, discovery_event,
)

# H17 (2026-08-26) 发现流提取
# 停用词表：过滤"众人/他们/旁白"等指代词，避免"新人物"列表噪音
# 上线后从 api_failures / 用户反馈迭代词表；独立常量好改
_DISCOVERY_STOP_WORDS = frozenset({
    "众人", "他们", "对方", "旁白", "自己", "他", "她", "它", "我", "你",
    "我们", "你们", "它们", "她们", "他们俩", "他们三人", "某人", "何人",
    "此人", "那人", "大家", "无名", "路人", "某人影",
})


def _extract_discovery(result: Optional[dict], seen_characters: set) -> Optional[dict]:
    """从块结果提取发现流摘要（plan Task 1.2）

    4 类：
    - core_events 数
    - 新伏笔（foreshadowing[].clue 截 24 字，最多 5 条）
    - 新人物（首次登场：seen_characters 集合求差，最多 5 条）
    - 遗留悬念（cross_block.unresolved_questions 截 24 字，最多 2 条）

    跨块伏笔去重明确不做（plan 决策 5）。
    续跑后人物"全量首次"是已知限制（plan 标注 Open Question）。
    """
    if not isinstance(result, dict):
        return None
    core_events = result.get("core_events") or []
    foreshadowing = result.get("foreshadowing") or []
    cross_block = result.get("cross_block") or {}
    unresolved = cross_block.get("unresolved_questions") or []

    # 新人物：core_events[].characters 逗号/顿号分隔 → 拆词 → 过滤停用词/长度 → 已见集合求差
    new_characters: List[str] = []
    for ev in core_events:
        chars_str = ev.get("characters", "")
        if not chars_str:
            continue
        for name in str(chars_str).replace("、", ",").split(","):
            name = name.strip()
            if not name or len(name) < 2 or len(name) > 8:
                continue
            if name in _DISCOVERY_STOP_WORDS:
                continue
            if name in seen_characters:
                continue
            new_characters.append(name)
            seen_characters.add(name)

    return {
        "events": len(core_events),
        "foreshadows": [str(f.get("clue", ""))[:24] for f in foreshadowing][:5],
        "characters": new_characters[:5],
        "unresolved": [str(q)[:24] for q in unresolved][:2],
    }


def _format_eta(seconds: float) -> str:
    """把剩余秒数格式化为最易读的 2 段中文（X 时 Y 分 / X 分 Y 秒 / X 秒）

    设计：与"小红点/进度条"等简明 UI 保持一致（Win11 任务栏 ETA 也用 2 段）。
    """
    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return ""
    s = int(round(seconds))
    if s <= 0:
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}时{m:02d}分"
    if m > 0:
        return f"{m}分{sec:02d}秒"
    return f"{sec}秒"


def _compute_eta(start_time: float, current: int, total: int, now: float) -> str:
    """根据全程平均速率外推剩余时间，格式化为 UI 友好字符串。

    返回空字符串表示"暂无法估算"（刚开始、已完成、参数非法）。
    设计：全程平均比滑动窗口更稳——并发场景（concurrency≥4）下进度
    事件频率高，全程平均反映稳态速率，初期几块慢启动会被后续稳态
    拉平；不用滑动窗口避免复杂度。
    """
    if not start_time or current <= 0 or total <= current:
        return ""
    elapsed = max(now - start_time, 0.1)
    rate = current / elapsed
    remaining_sec = (total - current) / max(rate, 1e-6)
    return _format_eta(remaining_sec)


def make_pipeline_callbacks(hub, item, stats, seen_characters):
    """构造 AnalysisPipeline 的进度回调（on_progress, on_token_stats）

    on_progress：pipeline 进度 → WS 消息（type 对齐旧 Qt 信号）
    - status=start → 转发 block_start（带 range 文本）
    - done/failed/skipped → 转发 block_done + 每章统计 + discovery（仅成功块）
      skipped 也必须转发：否则前端已登记的 start 车道收不到 done，
      activeBlocks 永久泄漏（车道堆叠修复）
    on_token_stats：累积分类 token 统计 + 转发
    """

    async def on_progress(payload: dict) -> None:
        status = payload.get("status", "")
        message = payload.get("message", "")
        if message:
            level = "error" if status in (PipelineStatus.FAILED, PipelineStatus.FAILED_SUMMARY) else "info"
            await hub.log(message, level=level)
        if "progress" in payload and "total" in payload:
            current = payload["progress"]
            total = payload["total"]
            item.completed_chapters = current
            eta_str = _compute_eta(item.start_time, current, total, time.time())
            await hub.progress(current, total, eta=eta_str)

        # H17: block_start 转发（带章范围文本 message 复用现有"开始分析第X-Y章..."）
        if status == PipelineStatus.START:
            range_text = payload.get("range") or message
            await hub.publish(block_start_event(
                chapter=payload.get("chapter", 0),  # block_id（块起始章号）
                range_text=range_text,
                progress=payload.get("progress", 0),
                total=payload.get("total", 0),
                ts=time.time(),
            ))

        # skipped（内容审核拦截）也必须转发：否则前端已登记的 start 车道
        # 收不到 done，activeBlocks 永久泄漏（车道堆叠修复）
        if status in (PipelineStatus.DONE, PipelineStatus.FAILED, PipelineStatus.SKIPPED):
            range_text = payload.get("range") or message
            await hub.publish(block_done_event(
                chapter=payload.get("chapter", 0),
                ok=status == PipelineStatus.DONE,
                range_text=range_text,
                elapsed=payload.get("elapsed"),
                tokens=payload.get("tokens"),
            ))
            # 记录每章统计（失败块同样计入：重试成本不可因失败而归零）
            stats.record_chapter(payload)

            # H17: discovery 提取（仅成功块，result 是 AnalysisResult dataclass dict）
            if status == "done":
                result = payload.get("result")
                discovery = _extract_discovery(result, seen_characters)
                if discovery:
                    await hub.publish(discovery_event(**discovery))

    async def on_token_stats(payload: dict) -> None:
        # 累积分类 token 统计（实现在 analysis_stats.AnalysisStats）
        stats.accumulate_token_stats(payload)
        await hub.publish({"type": "token_stats", "payload": payload})

    return on_progress, on_token_stats
