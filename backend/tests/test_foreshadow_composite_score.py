"""伏笔组合因子排序 + composite_score 保留测试"""
import pytest
from backend.utils.foreshadow_ledger import ForeshadowItem, SCHEMA_VERSION


def test_foreshadow_item_has_composite_score():
    """ForeshadowItem 新增 composite_score 和 importance 字段"""
    item = ForeshadowItem(
        id="fs_001", description="test",
        first_seen_chapter=1, first_seen_batch=0,
        last_seen_chapter=100, last_seen_batch=0,
    )
    assert item.composite_score == 0  # 默认值
    assert item.importance == "中"  # 默认值


def test_foreshadow_item_from_dict_backward_compat():
    """旧 ledger JSON（无 composite_score/importance）能正常反序列化"""
    old_data = {
        "id": "fs_001",
        "description": "test",
        "first_seen_chapter": 1,
        "first_seen_batch": 0,
        "last_seen_chapter": 100,
        "last_seen_batch": 0,
        "status": "active",
        "confidence": 0.8,
        "evidence_chapters": [1, 50],
        "source_type": "catalog_scan",
    }
    item = ForeshadowItem.from_dict(old_data)
    assert item.composite_score == 0
    assert item.importance == "中"
    assert item.id == "fs_001"


def test_schema_version_bumped():
    """SCHEMA_VERSION 升级到 5（v5 加 question/answer_criteria/lead；v4 加锚点与 entity_key）"""
    assert SCHEMA_VERSION == 5
