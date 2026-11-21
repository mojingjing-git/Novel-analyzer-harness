"""地点同义判定、聚合与地点归一化服务测试。"""
from __future__ import annotations

from backend.app import app
from backend.services.location_normalization_service import (
    LocationNormalizationService,
    get_location_normalization_service,
)
from backend.services.location_normalizer import aggregate_locations
from backend.utils.text_utils import is_same_location


def test_is_same_location_rules():
    assert is_same_location("宁安县", "宁安县") is True
    assert is_same_location("宁安县", "宁安县。") is True
    assert is_same_location("宁安县", "宁安县城") is False
    assert is_same_location("居安小阁", "居安小阁（主角）") is True
    assert is_same_location("大唐公司", "新大唐公司") is True
    assert is_same_location("宁安县", "原宁安县原址") is True
    assert is_same_location("大唐公司", "大唐公司会议室") is False
    assert is_same_location("京", "京") is False


def test_aggregate_locations():
    raw = [
        {"name": "宁安县", "parent": "大周王朝", "type": "城市", "description": "县城1", "chapter": 1},
        {"name": "宁安县", "parent": "大贞", "type": "城池", "description": "县城2", "chapter": 2},
        {"name": "居安小阁", "parent": "宁安县", "type": "建筑", "description": "甲", "chapter": 1},
        {"name": "居安小阁（主角）", "parent": "宁安县", "type": "住宅", "description": "乙", "chapter": 2},
        {"name": "", "parent": "", "type": "", "description": "", "chapter": 3},
    ]
    groups = aggregate_locations(raw)
    assert len(groups) == 2
    by_first = {g["aliases"][0]: g for g in groups}
    assert by_first["宁安县"]["chapter_count"] == 2
    assert by_first["宁安县"]["types"] == {"城市": 1, "城池": 1}


def test_location_normalization_service_and_routes():
    svc = LocationNormalizationService()
    st = svc.status()
    assert st["running"] is False and st["phase"] == "idle" and st["book_id"] == ""
    assert get_location_normalization_service() is get_location_normalization_service()

    def _collect_paths(routes):
        out = []
        for r in routes:
            if hasattr(r, "routes"):
                out.extend(_collect_paths(r.routes))
            elif hasattr(r, "original_router"):
                out.extend(_collect_paths(r.original_router.routes))
            elif hasattr(r, "path"):
                out.append(r.path)
        return out

    paths = _collect_paths(app.routes)
    assert "/api/location-normalization/start" in paths
    assert "/api/location-normalization/status" in paths
