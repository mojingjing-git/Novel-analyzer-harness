"""vnext 自动裁决（resolution）、顺序分析执行器（runner）与服务路由测试。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.services import vnext_service
from backend.vnext import project
from backend.vnext.prompt_limits import PromptLimits
from backend.vnext.resolution import (
    INSUFFICIENT,
    INVALID,
    REASON_NO_COUNTER_SEARCH,
    REASON_QUOTE_NOT_FOUND,
    SUPPORTED,
    locate_quote,
    parse_recheck,
)
from backend.vnext.results import ResultStrategy
from backend.vnext.runner import run_analysis

TEXT = "书页泛黄。王冠早已熔成金锭，此事千真万确。此后无人再提。"
QUOTE = "王冠早已熔成金锭，此事千真万确"
SPAN = (TEXT.index(QUOTE), TEXT.index(QUOTE) + len(QUOTE))


def _parse(over: dict):
    payload = {
        "candidate_id": "cd_1",
        "evidence_id": "ev_1",
        "verdict": "SUPPORTED",
        "supporting_quote": QUOTE,
        "supporting_span": list(SPAN),
        "answer": "王冠已被熔毁",
        "counter_search": {"performed": True, "checked_evidence_ids": ["ev_1"], "counter_quotes": []},
        "material_insufficient_reason": None,
    }
    payload.update(over)
    return parse_recheck(
        payload,
        allowed_candidate_ids={"cd_1"},
        allowed_evidence_ids={"ev_1"},
        locate=lambda q, eid: locate_quote(q, TEXT, reported=None),
        evidence_text=lambda eid: TEXT,
    )


def test_vnext_resolution_gates():
    v_ok = _parse({})
    assert v_ok.verdict == SUPPORTED and v_ok.located_span == SPAN

    v_bad_quote = _parse({"supporting_quote": "原文根本不存在的一段长引文内容"})
    assert v_bad_quote.verdict == INVALID and v_bad_quote.reason == REASON_QUOTE_NOT_FOUND

    v_no_counter = _parse({"counter_search": {"performed": False, "checked_evidence_ids": [], "counter_quotes": []}})
    assert v_no_counter.verdict == INSUFFICIENT and v_no_counter.reason == REASON_NO_COUNTER_SEARCH


async def test_vnext_runner_sequential_analysis_and_resume(tmp_path):
    chapters = [(1, "第一章\n方醒从棺中坐起。\n"), (2, "第二章\n他推开棺盖。\n")]
    book = project.create_project(tmp_path, "书", chapters, book_id="bk_run0000001")

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, temperature=0.1, max_tokens=None):
            self.calls += 1
            quote = "方醒从棺中坐起" if self.calls == 1 else "他推开棺盖"

            class _R:
                content = json.dumps({"facts": [{"fact_id": f"f{self.calls}", "text": quote, "quote": {"start": 4, "end": 4 + len(quote), "text": quote}}], "characters": []}, ensure_ascii=False)
                tokens = (10, 5)
                usage_status = "KNOWN"

            return _R()

    client = FakeClient()
    out1 = await run_analysis(book, client=client, limits=PromptLimits(), strategy=ResultStrategy(model_fingerprint="m1"), block_size=1)
    assert out1.stop_reason == "completed" and out1.committed_blocks == 2 and client.calls == 2

    # 再次运行应跳过已完成块（零 LLM 调用）
    out2 = await run_analysis(book, client=client, limits=PromptLimits(), strategy=ResultStrategy(model_fingerprint="m1"), block_size=1)
    assert out2.stop_reason == "completed" and client.calls == 2


def test_vnext_service_and_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_ROOT", str(tmp_path))
    chapters = [(i, f"第{i}章\n正文{i}\n") for i in range(1, 5)]
    project.create_project(tmp_path / "workspace_vnext", "测试书", chapters, book_id="bk_svc00000001")

    legacy = tmp_path / "workspace_vnext" / "《旧书》" / "blocks"
    legacy.mkdir(parents=True)
    (legacy / "0001.txt").write_text("旧正文", encoding="utf-8")

    payload = vnext_service.list_books()
    assert [b["book_id"] for b in payload["books"]] == ["bk_svc00000001"]
    assert any(r["name"] == "《旧书》" and r["reason"] == "LEGACY_DIR" for r in payload["rejected"])

    # 默认冻结不挂载 /api/vnext/*
    monkeypatch.delenv("NOVEL_ENABLE_VNEXT_ROUTES", raising=False)
    frozen_client = TestClient(create_app())
    assert frozen_client.get("/api/vnext/books").status_code == 404

    # 显式启用开关后挂载路由
    monkeypatch.setenv("NOVEL_ENABLE_VNEXT_ROUTES", "1")
    client = TestClient(create_app())
    r = client.get("/api/vnext/books")
    assert r.status_code == 200 and len(r.json()["books"]) == 1
