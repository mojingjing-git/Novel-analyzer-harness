"""聚合主文件必须走 safe_save_json 原子写（open('w') 直写在并发 GET 下
会产出截断 JSON 使后续读取 500）。用源码契约锁 + 行为双重验证。"""
import inspect
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _write_one_chapter(d: Path):
    """写入最小可解析的章节结果文件（字段与 AnalysisResult.from_dict 对齐，
    让 discover→load→aggregate 全链路走真实代码，不 mock）"""
    d.mkdir(parents=True, exist_ok=True)
    (d / "chapter_1_result.json").write_text(json.dumps({
        "chapter_number": 1,
        "cross_block": {"summary": "第一章", "unresolved_questions": [], "new_leads": []},
        "core_events": [{"event": "开局"}],
        "character_arcs": [], "foreshadowing": [], "plot_holes": [],
        "locations": [], "spatial_relationships": [],
        "long_context_insights": {}, "updated_knowledge": {},
    }, ensure_ascii=False), encoding="utf-8")


def test_aggregate_main_file_uses_safe_save_json(tmp_path):
    """源码契约：聚合主文件的落盘语句必须是 safe_save_json，且不得保留写模式裸 open"""
    from backend.utils.aggregate_utils import JSONAggregator

    source = inspect.getsource(JSONAggregator.aggregate_to_single_json)
    # 先剥离注释再匹配：修复说明注释里允许出现字面量 open('w')，契约只约束真实代码
    code_only = re.sub(r"#.*", "", source)
    assert "safe_save_json" in source, "聚合主文件必须走 safe_save_json 原子写"
    assert not re.search(r"open\([^)]*['\"]w['\"]", code_only), \
        "聚合主文件不得用 open('w') 直写（并发下会产出交错/截断 JSON）"


def test_aggregate_main_file_output_parseable(tmp_path):
    """行为验证：真实目录跑一次完整聚合，产物存在、JSON 可解析且章节键正确"""
    from backend.utils.aggregate_utils import JSONAggregator

    # __init__ 只存目录；把真实章节文件放进该目录即可触发内部 discover+load 流程
    out_dir = tmp_path / "out"
    _write_one_chapter(out_dir)

    agg = JSONAggregator(out_dir)
    produced = agg.aggregate_to_single_json()

    assert produced.exists(), "聚合产物文件必须存在"
    data = json.loads(produced.read_text(encoding="utf-8"))
    assert "chapters" in data and "1" in data["chapters"], \
        f"产物应含章节键 '1'，实际键：{list(data.get('chapters', {}))}"
