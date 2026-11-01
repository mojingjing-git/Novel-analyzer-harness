"""
测试 AnalysisResult from_dict / to_dict 往返序列化
"""
import json
import sys
from pathlib import Path

# 确保可以导入 backend 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.models.analysis_result import AnalysisResult, CoreEvent, CharacterArc, Foreshadowing, Location, SpatialRel


def test_importance_parsing():
    """core_events/foreshadowing 的 importance：英文归一化、非法值/缺省按中"""
    data = {
        "chapter_number": 1,
        "core_events": [
            {"id": 1, "event": "A", "importance": "高"},
            {"id": 2, "event": "B"},
            {"id": 3, "event": "C", "importance": "low"},
            {"id": 4, "event": "D", "importance": "垃圾值"},
        ],
        "foreshadowing": [
            {"clue": "x", "importance": "中"},
            {"clue": "y"},
            {"clue": "z", "importance": "High"},
        ],
    }
    result = AnalysisResult.from_dict(data)
    assert [e.importance for e in result.core_events] == ["高", "中", "低", "中"]
    assert [f.importance for f in result.foreshadowing] == ["中", "中", "高"]
    print("✅ test_importance_parsing passed")


def test_basic_roundtrip():
    """基本字段往返"""
    data = {
        "chapter_number": 5,
        "core_events": [
            {"id": 1, "event": "主角突破", "characters": "张三", "function": "推进"},
        ],
        "character_arcs": [
            {"name": "张三", "surface_action": "战斗", "inner_motivation": "复仇", "change_delta": "成长", "driver": "仇恨"},
        ],
        "foreshadowing": [
            {"clue": "神秘宝物", "type": "plant", "implication": "后续剧情", "confidence": "高"},
        ],
        "plot_holes": ["无明显逻辑漏洞"],
        "cross_block": {
            "summary": "承上启下",
            "unresolved_questions": [],
            "new_leads": [],
            "contextual_link": "链接",
        },
        "updated_knowledge": {
            "timeline": "时间线A",
            "world_building": ["世界观1"],
        },
        "long_context_insights": {
            "thematic_elements": ["主题1"],
            "pattern": "模式",
            "foreshadowing_network": "网络",
            "pacing": "节奏",
            "long_term_arcs": ["弧光1"],
        },
        "raw_response": "",
        "block_size": 1,
    }

    result = AnalysisResult.from_dict(data)
    out = result.to_dict()
    assert out["chapter_number"] == 5
    assert len(out["core_events"]) == 1
    assert out["core_events"][0]["event"] == "主角突破"
    assert len(out["character_arcs"]) == 1
    assert out["character_arcs"][0]["name"] == "张三"
    assert len(out["foreshadowing"]) == 1
    assert out["foreshadowing"][0]["confidence"] == "高"
    print("✅ test_basic_roundtrip passed")


def test_locations_spatial_rels():
    """地点和空间关系"""
    data = {
        "chapter_number": 3,
        "core_events": [],
        "locations": [
            {"name": "青云门", "parent": "", "type": "宗门", "description": "修仙宗门"},
            {"name": "主峰", "parent": "青云门", "type": "地点", "description": "主殿所在"},
        ],
        "spatial_relationships": [
            {"from": "主峰", "to": "后山", "relation": "北面"},
        ],
    }
    result = AnalysisResult.from_dict(data)
    assert len(result.locations) == 2
    assert result.locations[0].name == "青云门"
    assert result.locations[1].parent == "青云门"
    assert len(result.spatial_relationships) == 1
    assert result.spatial_relationships[0].from_ == "主峰"

    # to_dict: from_ → from
    out = result.to_dict()
    assert out["spatial_relationships"][0]["from"] == "主峰"
    assert "from_" not in out["spatial_relationships"][0]
    print("✅ test_locations_spatial_rels passed")


def test_confidence_mapping():
    """英文置信度映射"""
    data = {
        "chapter_number": 1,
        "foreshadowing": [
            {"clue": "线索1", "type": "plant", "implication": "", "confidence": "high"},
            {"clue": "线索2", "type": "recall", "implication": "", "confidence": "medium"},
            {"clue": "线索3", "type": "develop", "implication": "", "confidence": "low"},
        ],
    }
    result = AnalysisResult.from_dict(data)
    assert result.foreshadowing[0].confidence == "高"
    assert result.foreshadowing[1].confidence == "中"
    assert result.foreshadowing[2].confidence == "低"
    print("✅ test_confidence_mapping passed")


def test_json_serializable():
    """to_dict 结果可JSON序列化"""
    data = {
        "chapter_number": 1,
        "core_events": [{"id": 1, "event": "test", "characters": ["A", "B"], "function": "f"}],
    }
    result = AnalysisResult.from_dict(data)
    out = result.to_dict()
    json_str = json.dumps(out, ensure_ascii=False)
    parsed = json.loads(json_str)
    assert parsed["chapter_number"] == 1
    print("✅ test_json_serializable passed")


def test_empty_data():
    """空数据默认值"""
    result = AnalysisResult.from_dict({})
    assert result.chapter_number == 0
    assert result.core_events == []
    assert result.foreshadowing == []
    assert result.locations == []
    assert result.spatial_relationships == []
    print("✅ test_empty_data passed")


def test_characters_list_normalization():
    """characters 字段列表归一化"""
    data = {
        "chapter_number": 1,
        "core_events": [
            {"id": 1, "event": "test", "characters": ["张三", "李四"], "function": "f"},
        ],
    }
    result = AnalysisResult.from_dict(data)
    assert result.core_events[0].characters == "张三, 李四"
    print("✅ test_characters_list_normalization passed")


def test_normalize_string_list_absorbs_whole_string():
    """LLM 把数组字段漂移成单个字符串时应整串收编，而非拆成单字垃圾"""
    from backend.models.analysis_result import AnalysisResult
    assert AnalysisResult._normalize_string_list("修仙世界有灵气") == ["修仙世界有灵气"]
    assert AnalysisResult._normalize_string_list("") == []
    assert AnalysisResult._normalize_string_list(["a", 1]) == ["a", "1"]
    assert AnalysisResult._normalize_string_list(None) == []


def test_from_dict_world_building_string_drift():
    from backend.models.analysis_result import AnalysisResult
    r = AnalysisResult.from_dict({
        "chapter_number": 1,
        "updated_knowledge": {"world_building": "灵气复苏的世界"},
        "long_context_insights": {},
    })
    assert r.updated_knowledge.world_building == ["灵气复苏的世界"]


def test_validate_rejects_string_nested_objects():
    """真值字符串绕过 validate 时，from_dict 里 .get() 崩 → 块永久丢失"""
    from backend.core.analyzer import validate_parsed_analysis
    ok = {"core_events": [], "cross_block": {"summary": "s"},
          "long_context_insights": {}, "updated_knowledge": {}}
    assert validate_parsed_analysis(ok) == (True, "")

    bad = dict(ok, long_context_insights="本章无洞察")
    is_valid, err = validate_parsed_analysis(bad)
    assert not is_valid and "long_context_insights" in err

    bad2 = dict(ok, updated_knowledge="无")
    is_valid, err = validate_parsed_analysis(bad2)
    assert not is_valid and "updated_knowledge" in err

    # 缺 updated_knowledge 键仍应通过（保持既有宽松语义，from_dict 有默认值）
    missing_ok = {k: v for k, v in ok.items() if k != "updated_knowledge"}
    assert validate_parsed_analysis(missing_ok)[0] is True


if __name__ == "__main__":
    test_basic_roundtrip()
    test_locations_spatial_rels()
    test_confidence_mapping()
    test_json_serializable()
    test_empty_data()
    test_characters_list_normalization()
    print("\n🎉 All model tests passed!")
