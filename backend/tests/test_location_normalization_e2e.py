"""端到端集成测试：API -> Service -> Normalizer -> 落盘"""
import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.app import app
from backend.core.error_types import LLMResult, UsageStatus
from backend.services import book_service
from backend.services import location_normalization_service as loc_norm_svc


@pytest.fixture
def reset_module_state():
    """清空 book_service / location_normalization_service 的模块级单例状态，
    保证测试间互不干扰（这些是单例，不主动重置会泄漏到下一个测试）。"""
    book_service._books.clear()
    book_service._manual_books.clear()
    loc_norm_svc._service = None
    yield
    book_service._books.clear()
    book_service._manual_books.clear()
    loc_norm_svc._service = None


@pytest.fixture
def mock_book(tmp_path, reset_module_state):
    """构造一本书，含 3 个 chapter_*.json；注册到 book_service 让 get_output_dir 能找到。"""
    book_id = "测试书"
    output_dir = tmp_path / book_id
    (output_dir / "output").mkdir(parents=True)
    for i in range(1, 4):
        chapter = {
            "chapter_number": i,
            "locations": [
                {"name": "宁安县" if i == 1 else "宁安县城", "type": "县城",
                 "parent": "京畿府", "description": "测试"},
            ],
            "spatial_relationships": [
                {"from": "宁安县", "to": "京畿府", "relation": "隶属"},
            ],
        }
        (output_dir / "output" / f"chapter_{i}_result.json").write_text(
            json.dumps(chapter, ensure_ascii=False), encoding="utf-8"
        )
    book_service.register_scan_dir(output_dir)
    return book_id, output_dir


def test_full_flow_creates_normalized_files(mock_book):
    book_id, output_dir = mock_book
    with patch("backend.services.location_normalization_service.LLMClient") as MockLLM:
        mock_client = MockLLM.return_value
        mock_client.chat = AsyncMock(return_value=LLMResult(
            True,
            json.dumps({"locations": [{"canonical_name": "宁安县",
                                       "aliases": ["宁安县", "宁安县城"]}]}),
            "",
            (100, 50),
            UsageStatus.KNOWN,
        ))
        mock_client.config.model = "test-model"
        mock_client._stop_requested = False

        with TestClient(app) as client:
            resp = client.post("/api/location-normalization/start",
                               json={"book_id": book_id})
            assert resp.status_code == 200, resp.text

            s = {"running": True, "phase": "starting", "error": ""}
            for _ in range(60):
                time.sleep(0.5)
                s = client.get("/api/location-normalization/status").json()
                if not s["running"]:
                    break
            assert s["phase"] == "complete", f"phase={s['phase']} error={s['error']}"

            loc_norm = output_dir / "output" / "locations_normalized.json"
            assert loc_norm.exists()
            payload = json.loads(loc_norm.read_text(encoding="utf-8"))
            assert len(payload["locations"]) == 1
            assert payload["locations"][0]["canonical_name"] == "宁安县"

            sp_norm = output_dir / "output" / "spatial_relationships_normalized.json"
            assert sp_norm.exists()


def test_map_api_returns_needs_normalization_for_unnormalized(mock_book):
    """未归一化时 /api/viz/map/{book_id} 必须返回 needs_normalization=true（不允许 raw 兜底）"""
    book_id, _ = mock_book
    with TestClient(app) as client:
        resp = client.get(f"/api/viz/map/{book_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_normalization"] is True
    assert body["locations"] == []
    assert body["relationships"] == []


def test_map_api_returns_data_after_normalization(mock_book):
    """归一化完成后，/api/viz/map/{book_id} 返回真实数据 + needs_normalization=false"""
    book_id, output_dir = mock_book
    (output_dir / "output" / "locations_normalized.json").write_text(json.dumps({
        "schema_version": 1,
        "locations": [{"canonical_name": "宁安县", "aliases": ["宁安县", "宁安县城"],
                       "parent": "", "type": "城市", "description": "测试"}],
        "chapter_mtimes_hash": "",
    }, ensure_ascii=False), encoding="utf-8")
    (output_dir / "output" / "spatial_relationships_normalized.json").write_text(json.dumps({
        "schema_version": 1,
        "relationships": [{"from": "宁安县", "to": "京畿府"}],
        "chapter_mtimes_hash": "",
    }, ensure_ascii=False), encoding="utf-8")
    for ch in range(1, 4):
        path = output_dir / "output" / f"chapter_{ch}_result.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_normalized_ref"] = "locations_normalized.json"
        data["_normalized_spatial_ref"] = "spatial_relationships_normalized.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with TestClient(app) as client:
        resp = client.get(f"/api/viz/map/{book_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_normalization"] is False
    assert len(body["locations"]) == 1
    assert body["locations"][0]["name"] == "宁安县"