"""本次窗口会话 token 累计测试
- SummaryService.session_token_stats() 汇总分类小计正确
- 会话累计跨多次总结任务叠加
- GET /api/analysis/token_stats/session 端点返回分析+总结结构
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.summary_service import SummaryService
from backend.api.routes_analysis import router


def _mk_stats(pairs: dict):
    return {cat: {"input_tokens": i, "output_tokens": o} for cat, (i, o) in pairs.items()}


def test_session_stats_accumulates_across_tasks():
    """两次总结任务的 token 分类跨任务叠加，总量正确"""
    svc = SummaryService()
    svc._started_at = 100.0
    svc._finished_at = 120.0
    stats = _mk_stats({"summary": (300, 150), "recheck": (100, 50)})
    for cat, pair in stats.items():
        entry = svc._session_token_stats.setdefault(cat, {"input_tokens": 0, "output_tokens": 0})
        entry["input_tokens"] += pair["input_tokens"]
        entry["output_tokens"] += pair["output_tokens"]
    svc._session_elapsed += svc._finished_at - svc._started_at

    # 第二次总结
    svc._started_at = 200.0
    svc._finished_at = 230.0
    stats2 = _mk_stats({"summary": (500, 250), "style": (50, 25)})
    for cat, pair in stats2.items():
        entry = svc._session_token_stats.setdefault(cat, {"input_tokens": 0, "output_tokens": 0})
        entry["input_tokens"] += pair["input_tokens"]
        entry["output_tokens"] += pair["output_tokens"]
    svc._session_elapsed += svc._finished_at - svc._started_at

    result = svc.session_token_stats()
    assert result["input_tokens"] == 950        # 400 + 550
    assert result["output_tokens"] == 475       # 200 + 275
    assert result["categories"]["summary"]["input_tokens"] == 800   # 300 + 500
    assert result["categories"]["recheck"]["input_tokens"] == 100
    assert result["categories"]["style"]["output_tokens"] == 25
    assert result["elapsed"] == 50.0            # 20 + 30


def test_session_stats_empty():
    """无总结任务时返回零结构"""
    svc = SummaryService()
    result = svc.session_token_stats()
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert result["categories"] == {}
    assert result["elapsed"] == 0.0


def test_session_endpoint_structure():
    """端点返回 analysis（AnalysisService.token_stats）+ summary（会话累计）"""
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    analysis_stats = {"categories": {"chapter": {"input_tokens": 100, "output_tokens": 50}},
                      "chapter_stats": [], "elapsed": 10.0, "running": False,
                      "total_retries": 2, "total_failed_tokens": 30, "cached_tokens": 20}
    summary_stats = {"categories": {}, "input_tokens": 60, "output_tokens": 25, "elapsed": 5.0}

    class _FakeAnalysisService:
        def token_stats(self):
            return analysis_stats

    class _FakeSummaryService:
        def session_token_stats(self):
            return summary_stats

    with patch("backend.api.routes_analysis.get_service", return_value=_FakeAnalysisService()), \
         patch("backend.services.summary_service.get_summary_service",
               return_value=_FakeSummaryService()) as _:
        resp = client.get("/api/analysis/token_stats/session")

    assert resp.status_code == 200
    data = resp.json()
    assert data["analysis"]["categories"]["chapter"]["input_tokens"] == 100
    assert data["analysis"]["total_retries"] == 2
    assert data["summary"]["input_tokens"] == 60
    assert data["summary"]["output_tokens"] == 25
