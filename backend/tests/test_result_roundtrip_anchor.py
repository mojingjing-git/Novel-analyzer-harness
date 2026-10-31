"""锚点字段的往返地基：序列化、旧数据兼容、去重链保留。

复审 R1 的原话是"字段要全链路传递，否则等于没加"。这里逐环钉住：
模型 → `Foreshadowing` → `to_dict` → `from_dict` → `deduplicate_foreshadows`。
"""

from __future__ import annotations

from backend.models.analysis_result import AnalysisResult, Foreshadowing
from backend.utils.text_utils import deduplicate_foreshadows


def test_foreshadowing_anchor_fields_survive_to_dict_and_from_dict():
    item = Foreshadowing(
        clue="铜匣里的旧契",
        anchor_quote="铜匣打开，里面只有一张旧契。",
        anchor_chapter=2,
        motif_key="铜匣",
    )
    result = AnalysisResult(chapter_number=1, foreshadowing=[item])

    restored = AnalysisResult.from_dict(result.to_dict())
    got = restored.foreshadowing[0]
    assert got.anchor_quote == "铜匣打开，里面只有一张旧契。"
    assert got.anchor_chapter == 2
    assert got.motif_key == "铜匣"


def test_anchor_stats_is_a_real_dataclass_field_and_roundtrips():
    result = AnalysisResult(chapter_number=1)
    # 必须是字段（能进 to_dict），不是动态属性——否则重启后统计凭空消失
    assert "anchor_stats" in result.to_dict()
    result.anchor_stats = {"total": 3, "located": 2, "missing": 1, "by_kind": {"EXACT": 2}}

    restored = AnalysisResult.from_dict(result.to_dict())
    assert restored.anchor_stats["total"] == 3
    assert restored.anchor_stats["located"] == 2


def test_old_payload_without_anchor_fields_still_loads():
    payload = {
        "chapter_number": 5,
        "foreshadowing": [{"clue": "旧数据没有锚点", "type": "物件", "confidence": "中"}],
    }
    restored = AnalysisResult.from_dict(payload)
    item = restored.foreshadowing[0]
    assert item.anchor_quote == ""
    assert item.anchor_chapter == 0
    assert item.motif_key == ""
    assert restored.anchor_stats == {"total": 0, "located": 0, "missing": 0}


def test_deduplicate_foreshadows_keeps_anchor_of_representative():
    clues = [
        (3, "铜匣打开，里面只有一张旧契。", "物件", "高", "高",
         "铜匣打开，里面只有一张旧契。", 3, "铜匣"),
        (9, "铜匣里那张旧契又出现了。", "物件", "中", "中"),
    ]
    catalog = deduplicate_foreshadows(clues)
    assert catalog, "两条线索应被合并成一条"
    assert catalog[0]["anchor_quote"] == "铜匣打开，里面只有一张旧契。"
    assert catalog[0]["anchor_chapter"] == 3
    assert catalog[0]["motif_key"] == "铜匣"
