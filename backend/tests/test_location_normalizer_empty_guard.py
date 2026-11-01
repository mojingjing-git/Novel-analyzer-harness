"""归一化全批次解析为零条目时必须整体判败，
不得落盘空 normalized 文件 + 当前 hash（否则永久短路，地图数据恒空）。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.services.location_normalizer import LocationNormalizer


class FakeLLM:
    """chat 总是『成功』返回 '[]'：模拟模型系统性违反 schema 但 HTTP 成功"""

    def __init__(self):
        self._stop_requested = False

    async def chat(self, messages, *a, **k):
        return True, "[]", "", (0, 0)


def _groups(n=2):
    # 在 brief 基础上补齐 build_location_prompt 直接下标访问的最小字段，
    # 结构与 aggregate_locations 产出一致——保证走的是『解析为空』路径而非 KeyError
    return [
        {
            "canonical_name": f"地点{i}",
            "aliases": [f"地点{i}"],
            "types": {},
            "parents": {},
            "chapter_count": 1,
            "sample_desc": "",
        }
        for i in range(n)
    ]


def test_all_empty_batches_abort_phase_0a(tmp_path):
    norm = LocationNormalizer(
        output_dir=tmp_path, llm_client=FakeLLM(),  # type: ignore[arg-type]
        concurrency=2, locations_batch_size=1)   # 2 个 group → 2 个批次
    result = asyncio.run(norm._run_phase_0a_batches(_groups(2)))
    assert result is None, "全部批次解析为零条目时应熔断返回 None（返回 [] 会被当成成功）"


def test_run_refuses_to_persist_empty_normalization(tmp_path):
    # 准备最小输入：一个 chapter json 带 locations
    (tmp_path / "chapter_1_result.json").write_text(json.dumps({
        "chapter_number": 1,
        "locations": [{"name": "青云山", "parent": "", "type": "", "description": ""}],
        "spatial_relationships": [],
    }, ensure_ascii=False), encoding="utf-8")

    norm = LocationNormalizer(
        output_dir=tmp_path, llm_client=FakeLLM(),  # type: ignore[arg-type]
        concurrency=1, locations_batch_size=1)
    result = asyncio.run(norm.run())
    assert result is False, "全空归一化结果必须判败"

    sub = tmp_path / "output"
    base = sub if sub.is_dir() else tmp_path
    assert not (base / "locations_normalized.json").exists(), \
        "绝不能落盘空归一化数据（会带上当前hash永久短路）"
