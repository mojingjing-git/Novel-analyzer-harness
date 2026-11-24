"""数据模型与核心工具函数测试（AnalysisResult、JSON 容错修复、脱敏、编码、聚合、Excel、角色卡）。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from openpyxl import load_workbook

from backend.core.analyzer import validate_parsed_analysis
from backend.core.llm_failure_logger import redact
from backend.core.text_units import (
    build_display_labels,
    find_literal,
    load_blocks_as_files,
    locate_in_units,
    unit_label,
)
from backend.models.analysis_result import AnalysisResult
from backend.services import viz_service
from backend.utils.aggregate_utils import JSONAggregator, _normalize_char_name
from backend.utils.character_card_generator import CharacterCardGenerator
from backend.utils.excel_export import export_aggregated_to_excel
from backend.utils.json_utils import (
    _normalize_fullwidth_outside_strings,
    parse_json_robust,
    repair_missing_quotes,
    safe_save_json,
)
from backend.utils.text_utils import detect_and_decode, detect_encoding


# ---------------------------------------------------------------------------
# AnalysisResult & validate_parsed_analysis
# ---------------------------------------------------------------------------

def test_analysis_result_roundtrip_and_normalization():
    data = {
        "chapter_number": 5,
        "core_events": [
            {"id": 1, "event": "主角突破", "characters": ["张三", "李四"], "function": "推进", "importance": "high"},
            {"id": 2, "event": "次要事件", "importance": "垃圾值"},
        ],
        "character_arcs": [
            {"name": "张三", "surface_action": "战斗", "inner_motivation": "复仇", "change_delta": "成长", "driver": "仇恨"},
        ],
        "foreshadowing": [
            {"clue": "神秘宝物", "type": "plant", "implication": "后续剧情", "confidence": "high", "importance": "High"},
        ],
        "locations": [{"name": "青云门", "parent": "", "type": "宗门", "description": "修仙宗门"}],
        "spatial_relationships": [{"from": "主峰", "to": "后山", "relation": "北面"}],
        "updated_knowledge": {"timeline": "时间线A", "world_building": "灵气复苏的世界"},
    }
    res = AnalysisResult.from_dict(data)
    assert [e.importance for e in res.core_events] == ["高", "中"]
    assert res.core_events[0].characters == "张三, 李四"
    assert res.foreshadowing[0].confidence == "高"
    assert res.foreshadowing[0].importance == "高"
    assert res.updated_knowledge.world_building == ["灵气复苏的世界"]

    out = res.to_dict()
    assert out["chapter_number"] == 5
    assert out["spatial_relationships"][0]["from"] == "主峰"
    assert "from_" not in out["spatial_relationships"][0]


def test_validate_parsed_analysis_rejects_string_nested_objects():
    ok = {"core_events": [], "cross_block": {"summary": "s"}, "long_context_insights": {}, "updated_knowledge": {}}
    assert validate_parsed_analysis(ok) == (True, "")
    is_valid, err = validate_parsed_analysis(dict(ok, long_context_insights="本章无洞察"))
    assert not is_valid and "long_context_insights" in err
    is_valid2, err2 = validate_parsed_analysis(dict(ok, updated_knowledge="无"))
    assert not is_valid2 and "updated_knowledge" in err2


# ---------------------------------------------------------------------------
# JSON 容错解析与原子保存
# ---------------------------------------------------------------------------

def test_parse_json_robust_repairs_and_guards(tmp_path):
    # 键漏开引号 & 值漏开引号
    d1, detail1 = parse_json_robust('{"core_events":[{"id":1,"event":"a"},{id":2,"event":"b"}]}')
    assert d1 is not None and d1["core_events"][1]["id"] == 2
    assert "漏引号" in (detail1 or "")

    d2, _ = parse_json_robust('{"a":1,"b":、伏笔线索"}')
    assert d2 is not None and d2["b"] == "、伏笔线索"

    # 全角标点只在字符串外归一化
    fw = '{"canonical_name": "宁安县"，"event": "太子跳下凡间，袖子被挂"}'
    assert '"event": "太子跳下凡间，袖子被挂"' in _normalize_fullwidth_outside_strings(fw)
    d3, detail3 = parse_json_robust(fw)
    assert d3 is not None and d3["canonical_name"] == "宁安县" and detail3 is None

    # 合法 JSON 不被改动
    valid = '{"a":{"b":[1,2]},"d":"\\"esc\\""}'
    assert repair_missing_quotes(valid) == valid

    # 非 dict 顶层一律判失败
    for bad in ("", "[1,2,3]", '"hello"', "['a','b']", "null", "123"):
        res, _ = parse_json_robust(bad)
        assert res is None

    # 并发 safe_save_json
    target = tmp_path / "t.json"
    errors = []
    threads = [
        threading.Thread(target=lambda idx=i: None if safe_save_json({"i": idx}, target) else errors.append(idx))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and "i" in json.loads(target.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 脱敏 redact()
# ---------------------------------------------------------------------------

def test_redact_keys_and_boundaries():
    text = (
        "OpenAI: sk-abcdefghij1234567890abcd "
        "Anthropic: sk-ant-api03-abcdefghij1234567890 "
        "Header: Authorization: Bearer sk-cccccccccccccccccccccccccccc "
        "x-api-key: sk-ant-api03-1234567890abcdefghij "
        "Short: sk-id [Retry-After:120]"
    )
    out = redact(text)
    assert "sk-abcdefghij1234567890abcd" not in out
    assert "sk-ant-api03-abcdefghij1234567890" not in out
    assert "sk-cccccccccccccccccccccccccccc" not in out
    assert "Authorization: Bearer ***" in out
    assert "x-api-key: ***" in out
    assert "sk-id" in out
    assert "[Retry-After:120]" in out
    assert redact("") == ""
    assert redact(None) is None


# ---------------------------------------------------------------------------
# 编码检测与文本单元定位
# ---------------------------------------------------------------------------

def test_text_encoding_detection(tmp_path):
    big5_p = tmp_path / "big5.txt"
    big5_p.write_bytes("這是一個測試章節，主角前往靈山尋找失落的功法。".encode("big5"))
    assert detect_encoding(big5_p, ["utf-8", "gbk", "gb18030", "big5"]) == "big5"
    assert "靈山" in detect_and_decode(big5_p, ["utf-8", "gbk", "gb18030", "big5"])

    gbk_p = tmp_path / "gbk.txt"
    gbk_p.write_bytes("这是一个测试章节，主角前往灵山寻找失落的功法。".encode("gbk"))
    assert detect_encoding(gbk_p, ["utf-8", "gbk", "gb18030", "big5"]) == "gbk"


def test_text_units_location_and_labels(tmp_path):
    assert find_literal("abc", "xyz") is None
    assert find_literal("abc", "b") == 1
    units = {1: "第十二回 起\n同一句话。", 2: "第二回\n别的。", 3: "第三回\n同一句话。"}
    assert locate_in_units(units, "同一句话。") == (1, 7)
    assert locate_in_units(units, "同一句话。", prefer=3) == (3, 4)
    labels = build_display_labels(units)
    assert labels[1] == "第 12 回"
    assert unit_label(1, labels, noun="文件") == "文件1（第 12 回）"

    for name in ("1.txt", "2.txt", "split_report.txt"):
        (tmp_path / name).write_text("内容", encoding="utf-8")
    loaded, excluded = load_blocks_as_files(tmp_path)
    assert sorted(loaded) == [1, 2] and excluded == ["split_report.txt"]


# ---------------------------------------------------------------------------
# 聚合、角色后缀归一、Excel 导出与角色卡生成
# ---------------------------------------------------------------------------

def test_aggregator_and_char_suffix_normalization(tmp_path):
    assert _normalize_char_name("张三（主角）") == "张三"
    assert _normalize_char_name("李四(已故)") == "李四"

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "chapter_1_result.json").write_text(json.dumps({
        "chapter_number": 1,
        "cross_block": {"summary": "第一章", "unresolved_questions": [], "new_leads": []},
        "core_events": [{"event": "出场", "characters": "张三（主角）, 李四"}],
        "character_arcs": [{"name": "张三", "surface_action": "行动", "inner_motivation": "动机", "change_delta": "变化", "driver": "驱动"}],
        "foreshadowing": [], "plot_holes": [], "locations": [], "spatial_relationships": [],
        "long_context_insights": {}, "updated_knowledge": {},
    }, ensure_ascii=False), encoding="utf-8")

    agg = JSONAggregator(out_dir)
    agg.aggregate_full_output(output_dir=out_dir)
    single = out_dir / "novel_analysis_aggregated.json"
    assert single.exists() and "1" in json.loads(single.read_text(encoding="utf-8"))["chapters"]
    tracking = json.loads((out_dir / "character_tracking_aggregated.json").read_text(encoding="utf-8"))
    assert "张三" in tracking and "张三（主角）" not in tracking

    from backend.utils.aggregate_utils import split_character_names
    assert split_character_names("张三、李四（主角），王五") == ["张三", "李四", "王五"]

    # 角色卡生成器
    gen = CharacterCardGenerator(out_dir)
    summaries = gen.get_character_summaries()
    assert summaries[0]["name"] == "张三"
    card = gen.get_card_data("张三")
    assert card["name"] == "张三" and len(card["arcs"]) == 1

    # Excel 公式注入防护
    (out_dir / "core_events_aggregated.json").write_text(json.dumps(
        {"chapter_1": [{"id": "e1", "event": "=cmd|'/c calc'!A1", "characters": "张三", "function": "铺垫"}]},
        ensure_ascii=False,
    ), encoding="utf-8")
    xlsx_path = export_aggregated_to_excel(out_dir, tmp_path / "a.xlsx")
    wb = load_workbook(xlsx_path)
    cell = wb["核心事件"].cell(row=2, column=3)
    assert cell.data_type != "f" and str(cell.value).startswith("'")

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    (empty_dir / "unrecognized.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="可识别"):
        export_aggregated_to_excel(empty_dir, tmp_path / "b.xlsx")


def test_style_limit_and_character_card_logging(tmp_path, caplog, capsys):
    from backend.core.style_analyzer import compute_book_stats

    blocks_dir = tmp_path / "blocks"
    blocks_dir.mkdir()
    for ch in range(1, 7):
        (blocks_dir / f"{ch:04d}.txt").write_text(
            f"第{ch}章 「他说什么？」方醒缓缓点头，抬手揭开了桌上的那只青铜古匣，目光沉静如水。\n",
            encoding="utf-8",
        )

    agg, samples = compute_book_stats(blocks_dir, limit=3)
    assert agg["chapter_count"] == 3
    assert all(ch <= 3 for ch, _ in samples)

    bad_dir = tmp_path / "bad_agg"
    bad_dir.mkdir()
    (bad_dir / "character_tracking_aggregated.json").write_text("{not-json", encoding="utf-8")
    gen = CharacterCardGenerator(bad_dir)
    with caplog.at_level("WARNING"):
        ok = gen.load_aggregated_data()
    assert ok is False
    assert capsys.readouterr().out == ""
    assert any("加载聚合数据失败" in rec.message for rec in caplog.records)
