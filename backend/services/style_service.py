"""
风格分析服务
在后台 asyncio.Task 中运行风格分析，进度通过 ProgressHub 广播
"""

import asyncio
import logging
from typing import Optional

from ..progress_hub import get_hub

logger = logging.getLogger(__name__)

# 模块级单例任务
_style_task: Optional[asyncio.Task] = None
_style_phase: str = ""  # 当前阶段：computing_stats / llm_extract / done / error


def is_running() -> bool:
    global _style_task
    return _style_task is not None and not _style_task.done()


async def start_style_analysis(book_id: str, use_llm: bool = True, limit: int = 50) -> bool:
    """启动风格分析"""
    global _style_task
    if is_running():
        return False

    from .book_service import get_output_dir
    from ..core.style_analyzer import analyze_book

    output_dir = get_output_dir(book_id)
    if output_dir is None or not output_dir.exists():
        raise ValueError(f"书目不存在: {book_id}")

    blocks_dir = output_dir.parent / "blocks"
    if not blocks_dir.exists() or not any(blocks_dir.glob("*.txt")):
        raise ValueError(f"章节文件目录不存在或为空: {blocks_dir}")

    book_name = output_dir.parent.name
    hub = get_hub()

    async def _run():
        global _style_task, _style_phase
        try:
            _style_phase = "computing_stats"
            await hub.log(f"风格分析: 《{book_name}》 {limit}章 [{'LLM+代码' if use_llm else '纯代码'}模式]")
            # analyze_book 本身是 async，必须直接 await（不能用 to_thread 包，否则拿到未 await 的协程）
            style_md = await analyze_book(blocks_dir, book_name, use_llm, limit)
            _style_phase = "done"
            if style_md and len(style_md) > 50:
                out_path = output_dir / "style.md"
                await asyncio.to_thread(out_path.write_text, style_md, encoding="utf-8")
                await hub.log(f"风格分析完成: {out_path}")
            else:
                await hub.log("风格分析返回内容为空", level="warn")
        except Exception as e:
            _style_phase = "error"
            logger.error(f"风格分析失败: {e}", exc_info=True)
            await hub.log(f"风格分析失败: {e}", level="error")
        finally:
            _style_task = None

    _style_task = asyncio.create_task(_run())
    return True


def stop_style_analysis() -> bool:
    """停止风格分析"""
    global _style_task
    if _style_task is not None and not _style_task.done():
        _style_task.cancel()
        return True
    return False


def status() -> dict:
    """获取风格分析状态"""
    return {"running": is_running(), "phase": _style_phase}
