"""地点归一化编排服务测试"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.services.location_normalization_service import (
    LocationNormalizationService,
    get_location_normalization_service,
)


def test_status_initial_state():
    svc = LocationNormalizationService()
    s = svc.status()
    assert s["running"] is False
    assert s["phase"] == "idle"
    assert s["book_id"] == ""


def test_singleton_pattern():
    a = get_location_normalization_service()
    b = get_location_normalization_service()
    assert a is b


def test_routes_registered():
    """新路由已注册到 app"""
    from backend.app import app

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
    assert '/api/location-normalization/start' in paths
    assert '/api/location-normalization/stop' in paths
    assert '/api/location-normalization/status' in paths