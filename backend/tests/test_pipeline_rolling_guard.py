"""rolling 更新守卫：
1. _find_locked_milestones 对字符串型 paradigm_layers 元素不抛 AttributeError
2. 补跑后的同步 rolling 更新失败不得冲出（吞异常，保住收尾落盘）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core.pipeline import AnalysisPipeline, _find_locked_milestones


def test_find_locked_milestones_tolerates_string_layers():
    milestones = ["ch1: 起点", "ch50: 金丹"]
    layers = ["修仙期", {"chapters": "40-60", "text": "结丹期"}]  # 混入字符串元素
    locked = _find_locked_milestones(milestones, layers)          # 此前在此抛 AttributeError
    assert 0 in locked                 # 首条里程碑永远锁定
    assert 1 in locked                 # dict 层 40-60 命中 ch50


def test_safe_post_retry_rolling_update_swallows_errors():
    p = AnalysisPipeline.__new__(AnalysisPipeline)   # 跳过 __init__，只测该方法

    async def boom(*args, **kwargs):
        raise RuntimeError("LLM 返回畸形 rolling schema")

    p._update_rolling_summary = boom

    state_box = {"last_chapter": 10}
    asyncio.run(p._safe_post_retry_rolling_update(
        rolling_client=None, output_dir=Path("unused"),
        _rolling_state=state_box, retried_block_ids=[11],
        state=None, completed_count=3, total=5))

    assert state_box["last_chapter"] == 10  # 失败后基线不动
