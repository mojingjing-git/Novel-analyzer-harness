"""CLI 全链路契约测试：解析与流隔离、只读检索、账本原子覆写/代理/锁冲突、切分、分析、总结与独立复检。"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
import sys
from typing import Any, Dict, List

import openpyxl
import pytest
from starlette.testclient import TestClient

from backend.common.file_lock import BookFileLock
from backend.cli.main import main as cli_main
from backend.cli.runtime import (
    EXIT_BREAKER,
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
)
from backend.utils.foreshadow_ledger import ForeshadowItem, ForeshadowLedger


def _write_config(
    rt_dir: Path,
    *,
    api_key: str = "sk-test-secret-key-9876543210",
    recheck_api_key: str = "sk-recheck-secret-1234567890",
    base_url: str = "https://user:pass@example.invalid/v1",
    workspace_dir: str = "workspace",
) -> Path:
    rt_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = rt_dir / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "api": {
                    "base_url": base_url,
                    "api_key": api_key,
                    "model": "test-model",
                    "provider": "auto",
                },
                "analysis": {
                    "block_size": 4,
                    "concurrency": 2,
                    "recheck_version": "v3",
                    "recheck_api_key": recheck_api_key,
                    "recheck_base_url": "https://rkuser:rkpass@recheck.invalid/v1",
                },
                "gui": {},
                "workspace_dir": workspace_dir,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return cfg_path


def _seed_book(
    rt_dir: Path,
    book_name: str = "测试小说",
    *,
    zero_pad_blocks: bool = True,
    with_results: bool = True,
    with_ledger: bool = True,
    with_verdicts: bool = True,
    with_report: bool = True,
) -> Path:
    book_dir = rt_dir / "workspace" / book_name
    blocks_dir = book_dir / "blocks"
    output_dir = book_dir / "output"
    blocks_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    chapters = {
        1: "第1章 初遇\n夜雨潇潇，林尘在古庙拾起一枚刻着青莲纹路的残破玉佩，放入怀中。",
        2: "第2章 暗潮\n城主府张灯结彩，顾青暗中遣人打探玉佩来历，却无所得。",
        3: "第3章 试探\n林尘在茶楼与周墨对弈，周墨盯着茶杯轻声问起古庙旧事。",
        4: "第4章 揭晓\n密室灯火摇曳，林尘取出残破玉佩与族谱相合，原来青莲玉佩正是周家失落信物，周墨当场跪地认主。",
        5: "第5章 余波\n风雪散尽，旧巷深处的铁盒却再未有人开启，百年沉寂。",
    }
    for num, text in chapters.items():
        fname = f"{num:04d}.txt" if zero_pad_blocks else f"{num}.txt"
        (blocks_dir / fname).write_text(text, encoding="utf-8")

    if with_results:
        (output_dir / "chapter_1_result.json").write_text(
            json.dumps(
                {
                    "chapter": 1,
                    "block_size": 4,
                    "source_chapters": [1, 2, 3, 4],
                    "core_events": [
                        {
                            "chapter": 1,
                            "event": "古庙拾玉佩",
                            "characters": "林尘, 周墨",
                            "importance": "高",
                        },
                        {
                            "chapter": 4,
                            "event": "玉佩认主",
                            "characters": "林尘, 顾青",
                            "importance": "高",
                        },
                    ],
                    "character_changes": [
                        {"character": "林尘", "change": "得到青莲玉佩"},
                        {"character": "周墨", "change": "认出信物身份"},
                    ],
                    "foreshadowing": [
                        {
                            "clue": "青莲残破玉佩来历",
                            "type": "埋伏笔",
                            "chapter": 1,
                            "anchor_quote": "一枚刻着青莲纹路的残破玉佩",
                        }
                    ],
                    "timeline": [{"chapter": 1, "time": "雨夜", "event": "拾得玉佩"}],
                    "cross_block": {"unresolved_questions": ["铁盒何人所留？"]},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    if with_ledger:
        ledger = ForeshadowLedger(batch_size_snapshot=30, total_chapters=5)
        ledger.items = [
            ForeshadowItem(
                id="fs_001",
                description="青莲残破玉佩来历",
                first_seen_chapter=1,
                first_seen_batch=1,
                last_seen_chapter=4,
                last_seen_batch=1,
                status="active",
                source_type="catalog_scan",
                evidence_chapters=[1, 4],
                confidence=0.85,
                needs_review=True,
                importance="高",
                composite_score=340,
                anchor_quote="一枚刻着青莲纹路的残破玉佩",
                anchor_chapter=1,
                question="青莲玉佩究竟属于哪个家族？",
                answer_criteria="原文明确交代玉佩所属家族",
            ),
            ForeshadowItem(
                id="fs_002",
                description="茶楼试探暗线",
                first_seen_chapter=2,
                first_seen_batch=1,
                last_seen_chapter=3,
                last_seen_batch=1,
                status="active",
                source_type="catalog_scan",
                evidence_chapters=[2, 3],
                confidence=0.72,
                needs_review=False,
                importance="中",
                composite_score=220,
                anchor_quote="周墨盯着茶杯轻声问起古庙旧事",
                anchor_chapter=3,
                question="周墨为何问起古庙旧事？",
            ),
            ForeshadowItem(
                id="fs_003",
                description="深巷尘封铁盒",
                first_seen_chapter=5,
                first_seen_batch=1,
                last_seen_chapter=5,
                last_seen_batch=1,
                status="dormant",
                source_type="catalog_scan",
                evidence_chapters=[5],
                confidence=0.5,
                needs_review=False,
                importance="低",
                composite_score=110,
                anchor_quote="旧巷深处的铁盒",
                anchor_chapter=5,
                question="铁盒里装了什么？",
            ),
        ]
        ledger.save(output_dir / "foreshadow_ledger.json")

    if with_verdicts:
        (output_dir / "recheck_verdicts.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-11-24T12:00:00",
                    "model": "test-model",
                    "status": "COMPLETED",
                    "status_reason": "",
                    "candidates_total": 2,
                    "coverage": {"candidates_total": 2, "checked": 2, "v3_tiers": {"RESOLVED_EXPLICIT": 1}},
                    "applied": {"fs_001": "needs_review"},
                    "resolved_chapters": {"fs_001": 4},
                    "locate_records": {
                        "fs_001": {
                            "unit": 1,
                            "mode": "literal",
                            "source_chapter": 1,
                            "anchor_offset": 18,
                        }
                    },
                    "audit_routes": {
                        "fs_001": {
                            "engine": "v3",
                            "tier": "RESOLVED_EXPLICIT",
                            "unresolved_type": "",
                            "hops_used": 2,
                            "gate_demoted": False,
                            "gate_reason": "",
                            "repaired": False,
                            "evidence_chain": [
                                {"unit_id": 4, "quote": "青莲玉佩正是周家失落信物"}
                            ],
                        },
                        "fs_002": {
                            "engine": "v3",
                            "tier": "UNRESOLVED_STILL_OPEN",
                            "unresolved_type": "no_clear_answer",
                            "hops_used": 1,
                            "gate_demoted": True,
                            "gate_reason": "缺少直接回应",
                            "repaired": False,
                            "evidence_chain": [],
                        },
                    },
                    "verdicts": [
                        {
                            "candidate_id": "fs_001",
                            "verdict": "SUPPORTED",
                            "answer": "属于周家信物",
                            "quote": "青莲玉佩正是周家失落信物",
                            "reason": "第4章明示青莲玉佩与周家信物对应",
                        },
                        {
                            "candidate_id": "fs_002",
                            "verdict": "INSUFFICIENT",
                            "answer": "",
                            "quote": "",
                            "reason": "后文未明言动机",
                        },
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    if with_report:
        (output_dir / "final_summary_report.md").write_text(
            "# 测试小说总结\n\n第1章拾得青莲玉佩，第4章揭晓身世。\n" + ("脉络细节。" * 20),
            encoding="utf-8",
        )
        (output_dir / "final_summary_checkpoint").mkdir(parents=True, exist_ok=True)
        (output_dir / "final_summary_checkpoint" / "style_profile.json").write_text(
            json.dumps(
                {
                    "style_fp": "abc",
                    "profile": {
                        "statistical": {"avg_sentence_len": 24},
                        "semantic": {"tone": "悬疑古风"},
                        "sample_chapters": [1, 4],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return book_dir


def _run_json(capsys: pytest.CaptureFixture[str], argv: List[str]) -> tuple[int, Dict[str, Any], str]:
    code = cli_main(argv)
    cap = capsys.readouterr()
    payload = json.loads(cap.out)
    return code, payload, cap.err


# ==================== 1. 全局参数位置、JSON 错误契约与日志隔离 ====================


def test_global_json_flag_positions_and_parse_error_and_logging(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rt = tmp_path / "rt"
    _write_config(rt)
    _seed_book(rt, "甲书")

    # --json 放在子命令之前
    c1, p1, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "books", "list"])
    assert c1 == EXIT_OK
    assert p1["ok"] is True
    assert p1["data"]["total"] == 1

    # --json 放在子命令之后
    c2, p2, _ = _run_json(capsys, ["books", "list", "--runtime-dir", str(rt), "--json"])
    assert c2 == EXIT_OK
    assert p2["data"]["books"][0]["id"] == "甲书"

    # 解析错误（缺少必填动作或非法参数）在 --json 下仍返回纯 JSON + EXIT_USAGE
    c_err, p_err, _ = _run_json(capsys, ["--json", "ledger", "review", "甲书", "fs_001"])
    assert c_err == EXIT_USAGE
    assert p_err["ok"] is False
    assert p_err["error"]["type"] == "usage_error"

    # 无子命令也输出纯 JSON + EXIT_USAGE
    c_miss, p_miss, _ = _run_json(capsys, ["--json", "books"])
    assert c_miss == EXIT_USAGE
    assert p_miss["ok"] is False
    assert p_miss["error"]["type"] == "missing_command"

    # logging.StreamHandler(sys.stdout) 必须被重路由至 stderr，不污染 stdout JSON
    leak_logger = logging.getLogger("cli_stdout_leak_probe")
    stdout_handler = logging.StreamHandler(sys.stdout)
    leak_logger.addHandler(stdout_handler)
    try:
        leak_logger.setLevel(logging.WARNING)
        c_log, p_log, err_txt = _run_json(
            capsys, ["--json", "--runtime-dir", str(rt), "config", "health"]
        )
        assert c_log == EXIT_OK
        assert p_log["ok"] is True
        leak_logger.warning("should_go_stderr_not_stdout")
        assert stdout_handler.stream is sys.stderr
        cap2 = capsys.readouterr()
        assert cap2.out == ""
        assert "should_go_stderr_not_stdout" in cap2.err
        _ = err_txt
    finally:
        leak_logger.removeHandler(stdout_handler)


# ==================== 2. books 子命令 ====================


def test_books_commands_full_coverage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rt = tmp_path / "rt"
    _write_config(rt)
    book_dir = _seed_book(rt, "青莲传", zero_pad_blocks=True)
    empty_book = _seed_book(
        rt,
        "空书",
        with_results=False,
        with_ledger=False,
        with_verdicts=False,
        with_report=False,
    )

    # status
    c, p, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "books", "status", "青莲传"])
    assert c == EXIT_OK
    assert p["data"]["chapters_available"] == 5
    assert p["data"]["has_style_profile"] is True
    assert p["data"]["ledger"]["counts"]["total"] == 3

    # chapter：0001.txt + block_size=4 下查询第 3 章（正文来自第3章，分析来自第1块），支持分页
    c, p, _ = _run_json(
        capsys,
        ["--json", "--book-dir", str(book_dir), "books", "chapter", "3", "--offset", "4", "--limit", "10"],
    )
    assert c == EXIT_OK
    assert p["data"]["text"]["truncated"] is True
    assert len(p["data"]["text"]["content"]) == 10
    assert p["data"]["analysis"]["result_block_chapter"] == 1
    assert p["data"]["analysis"]["source_chapters"] == [1, 2, 3, 4]

    c, p, _ = _run_json(capsys, ["--json", "--book-dir", str(book_dir), "books", "chapter", "999"])
    assert c == EXIT_NOT_FOUND

    # search：先按章节范围过滤再检索 + 空检索词报错
    c, p, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "books", "search", "青莲传", "玉佩", "--from-chapter", "2", "--to-chapter", "4"],
    )
    assert c == EXIT_OK
    hit_chapters = {h["chapter"] for h in p["data"]["hits"]}
    assert hit_chapters == {2, 4}

    c, _, _ = _run_json(capsys, ["--json", "--book-dir", str(book_dir), "books", "search", "   "])
    assert c == EXIT_USAGE

    # characters：列表、按名查询、不存在角色与空分析书目 exit 3
    c, p, _ = _run_json(capsys, ["--json", "--book-dir", str(book_dir), "books", "characters"])
    assert c == EXIT_OK
    names = {ch["name"] for ch in p["data"]["characters"]}
    assert "林尘" in names

    c, p, _ = _run_json(capsys, ["--json", "--book-dir", str(book_dir), "books", "characters", "--name", "林尘"])
    assert c == EXIT_OK
    assert p["data"]["character"]["name"] == "林尘"

    c, _, _ = _run_json(capsys, ["--json", "--book-dir", str(book_dir), "books", "characters", "--name", "不存在人物"])
    assert c == EXIT_NOT_FOUND

    c, _, _ = _run_json(capsys, ["--json", "--book-dir", str(empty_book), "books", "characters"])
    assert c == EXIT_NOT_FOUND

    # report：分页 + 风格画像
    c, p, _ = _run_json(
        capsys,
        ["--json", "--book-dir", str(book_dir), "books", "report", "--offset", "0", "--limit", "15", "--style"],
    )
    assert c == EXIT_OK
    assert p["data"]["truncated"] is True
    assert p["data"]["style_profile"]["exists"] is True

    c, _, _ = _run_json(capsys, ["--json", "--book-dir", str(empty_book), "books", "report"])
    assert c == EXIT_NOT_FOUND

    # export：生成有效 xlsx；空书目返回 exit 3
    out_xlsx = tmp_path / "exports" / "out.xlsx"
    c, p, _ = _run_json(
        capsys,
        ["--json", "--book-dir", str(book_dir), "books", "export", "--output", str(out_xlsx)],
    )
    assert c == EXIT_OK
    assert out_xlsx.is_file()
    wb = openpyxl.load_workbook(out_xlsx)
    assert len(wb.sheetnames) >= 1
    wb.close()

    c, _, _ = _run_json(capsys, ["--json", "--book-dir", str(empty_book), "books", "export"])
    assert c == EXIT_NOT_FOUND


# ==================== 3. ledger 查询、证据窗口、原子批量与 HTTP 代理 ====================


def test_ledger_query_and_with_evidence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rt = tmp_path / "rt"
    _write_config(rt)
    book_dir = _seed_book(rt, "青莲传")

    # list 过滤与排序
    c, p, _ = _run_json(
        capsys,
        ["--json", "--book-dir", str(book_dir), "ledger", "list", "--status", "active", "--needs-review", "--sort", "first_seen"],
    )
    assert c == EXIT_OK
    assert [it["id"] for it in p["data"]["items"]] == ["fs_001"]
    assert "lifecycle_bars" in p["data"]

    # show --with-evidence: fs_001 命中 locate_records + payoffs evidence_chain
    c, p, _ = _run_json(
        capsys,
        ["--json", "--book-dir", str(book_dir), "ledger", "show", "fs_001", "--with-evidence", "--radius", "40"],
    )
    assert c == EXIT_OK
    ev = p["data"]["evidence"]
    assert ev["setup"]["unit"] == 1
    assert "残破玉佩" in ev["setup"]["snippet"]
    assert len(ev["payoffs"]) == 1
    assert ev["payoffs"][0]["unit"] == 4
    assert "周家失落信物" in ev["payoffs"][0]["snippet"]

    # fs_002 无 locate_records，走 locate_with_mode 回退精确定位到单元 3
    c, p, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "ledger", "show", "青莲传", "fs_002", "--with-evidence"],
    )
    assert c == EXIT_OK
    assert p["data"]["evidence"]["setup"]["unit"] == 3
    assert "古庙旧事" in p["data"]["evidence"]["setup"]["snippet"]

    # verdicts 按 tier 与 gate_demoted 过滤
    c, p, _ = _run_json(
        capsys,
        ["--json", "--book-dir", str(book_dir), "ledger", "verdicts", "--tier", "RESOLVED_EXPLICIT"],
    )
    assert c == EXIT_OK
    assert [v["id"] for v in p["data"]["verdicts"]] == ["fs_001"]

    c, p, _ = _run_json(
        capsys,
        ["--json", "--book-dir", str(book_dir), "ledger", "verdicts", "--gate-demoted"],
    )
    assert c == EXIT_OK
    assert [v["id"] for v in p["data"]["verdicts"]] == ["fs_002"]


def test_ledger_review_atomic_rollback_bom_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rt = tmp_path / "rt"
    _write_config(rt)
    book_dir = _seed_book(rt, "青莲传")

    # 1) 原子回滚：第 2 条 ID 不存在，第 1 条在磁盘上必须完全未改动
    bad_batch = [
        {"item_id": "fs_001", "action": "confirm_resolved", "resolution": "已确认", "resolved_chapter": 4},
        {"item_id": "fs_missing", "action": "mark_dormant"},
    ]
    bad_file = tmp_path / "bad_batch.json"
    bad_file.write_bytes(b"\xef\xbb\xbf" + json.dumps(bad_batch, ensure_ascii=False).encode("utf-8"))

    c, p, _ = _run_json(
        capsys,
        ["--json", "--offline", "--book-dir", str(book_dir), "ledger", "batch-review", "--file", str(bad_file)],
    )
    assert c == EXIT_NOT_FOUND
    assert p["ok"] is False
    reloaded = ForeshadowLedger.load(book_dir / "output" / "foreshadow_ledger.json")
    fs1 = reloaded._find_item("fs_001")
    assert fs1 is not None and fs1.status == "active" and fs1.source_type != "human_override"

    # 2) stdin "-" 带 UTF-8 BOM + {"items": [...]} 包装格式成功原子提交
    ok_payload = {
        "items": [
            {"id": "fs_001", "action": "confirm_resolved", "resolution": "第4章认主", "resolved_chapter": 4},
            {"item_id": "fs_002", "action": "reject_to_active", "note": "仅铺垫"},
        ]
    }
    raw_bytes = b"\xef\xbb\xbf" + json.dumps(ok_payload, ensure_ascii=False).encode("utf-8")
    fake_stdin = io.TextIOWrapper(io.BytesIO(raw_bytes), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", fake_stdin)

    c, p, _ = _run_json(
        capsys,
        ["--json", "--offline", "--book-dir", str(book_dir), "ledger", "batch-review", "--file", "-"],
    )
    assert c == EXIT_OK
    assert p["data"]["updated_count"] == 2
    assert p["data"]["via"] == "local"
    after = ForeshadowLedger.load(book_dir / "output" / "foreshadow_ledger.json")
    assert after._find_item("fs_001").status == "resolved"
    assert after._find_item("fs_002").source_type == "human_override"

    # 3) BookFileLock 跨句柄竞争 -> 返回 EXIT_CONFLICT (4)
    with BookFileLock(book_dir / ".book.lock"):
        c_lk, p_lk, _ = _run_json(
            capsys,
            ["--json", "--offline", "--book-dir", str(book_dir), "ledger", "review", "fs_003", "--action", "mark_dormant"],
        )
        assert c_lk == EXIT_CONFLICT
        assert p_lk["error"]["type"] == "book_locked"


def test_ledger_live_server_proxy_and_canonical_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import httpx
    from backend.app import app as fastapi_app
    from backend.services import book_service

    rt = tmp_path / "rt"
    _write_config(rt)
    book_dir = _seed_book(rt, "代理测试书")
    other_dir = _seed_book(tmp_path / "other_rt", "代理测试书")

    token = "cli-live-proxy-secret-token"
    monkeypatch.setenv("NOVEL_ANALYZER_SESSION_TOKEN", token)
    (rt / "session.token").write_text(token, encoding="utf-8")
    (rt / "app.lock").write_text(json.dumps({"pid": 12345, "port": 18765, "started_at": 1}), encoding="utf-8")

    # 校验 server 路径匹配：指向 other_dir 时冲突/回退；切回真实 book_dir 后走 HTTP 代理
    scan_target = {"roots": [other_dir.parent]}
    monkeypatch.setattr(book_service, "_candidate_roots", lambda: list(scan_target["roots"]))
    book_service.refresh_books()

    test_client = TestClient(fastapi_app)

    class _TestClientBridge:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> "_TestClientBridge":
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

        def get(self, url: str, **kwargs: Any) -> Any:
            path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url
            return test_client.get(path, **kwargs)

        def post(self, url: str, **kwargs: Any) -> Any:
            path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url
            return test_client.post(path, **kwargs)

    monkeypatch.setattr(httpx, "Client", _TestClientBridge)

    # 场景 A：app.lock 自动发现服务端指向另一同名目录 -> 显式 --server 报 4 冲突，自动发现回退本地写
    c_mis, p_mis, _ = _run_json(
        capsys,
        [
            "--json",
            "--runtime-dir", str(rt),
            "--server", "http://127.0.0.1:18765",
            "--book-dir", str(book_dir),
            "ledger", "review", "fs_001", "--action", "confirm_resolved",
        ],
    )
    assert c_mis == EXIT_CONFLICT
    assert p_mis["error"]["type"] == "server_book_mismatch"

    c_fb, p_fb, _ = _run_json(
        capsys,
        [
            "--json",
            "--runtime-dir", str(rt),
            "--book-dir", str(book_dir),
            "ledger", "review", "fs_001", "--action", "mark_dormant",
        ],
    )
    assert c_fb == EXIT_OK
    assert p_fb["data"]["via"] == "local"

    # 场景 B：将服务端书目录切回真实 book_dir -> 自动带 X-Session-Token 走 HTTP 批量覆写端点
    book_service._books.clear()
    book_service._manual_books.clear()
    scan_target["roots"] = [book_dir.parent]
    book_service.refresh_books()

    batch_file = tmp_path / "srv_batch.json"
    batch_file.write_text(
        json.dumps([{"item_id": "fs_002", "action": "confirm_resolved", "resolution": "HTTP代理确认", "resolved_chapter": 4}]),
        encoding="utf-8",
    )
    c_srv, p_srv, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "--book-dir", str(book_dir), "ledger", "batch-review", "--file", str(batch_file)],
    )
    assert c_srv == EXIT_OK
    assert p_srv["data"]["via"] == "server"
    assert p_srv["data"]["updated_count"] == 1
    assert ForeshadowLedger.load(book_dir / "output" / "foreshadow_ledger.json")._find_item("fs_002").status == "resolved"


# ==================== 4. split / analyze / summary / recheck ====================


def test_split_dry_run_save_and_path_traversal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rt = tmp_path / "rt"
    _write_config(rt)
    src = tmp_path / "新书.txt"
    src.write_text(
        "第1章 开始\n" + ("这是第一章正文内容。" * 5) + "\n\n第2章 继续\n" + ("这是第二章正文内容。" * 5),
        encoding="utf-8",
    )

    # 路径穿越必须拒绝
    for bad_name in ("../escape", "sub/dir", "C:drive", ".."):
        c_bad, p_bad, _ = _run_json(
            capsys,
            ["--json", "--runtime-dir", str(rt), "split", str(src), "--name", bad_name],
        )
        assert c_bad == EXIT_USAGE
        assert p_bad["error"]["type"] == "invalid_book_name"

    # dry-run 不创建任何目录
    c_dry, p_dry, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "split", str(src), "--name", "干跑测试", "--dry-run"],
    )
    assert c_dry == EXIT_OK
    assert p_dry["data"]["dry_run"] is True
    assert p_dry["data"]["total_chapters"] >= 2
    assert not (rt / "workspace" / "干跑测试").exists()

    # 正式切分落盘；再次不带 --force 报冲突 exit 4
    c_ok, p_ok, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "split", str(src), "--name", "干跑测试"],
    )
    assert c_ok == EXIT_OK
    assert (rt / "workspace" / "干跑测试" / "blocks").is_dir()

    c_dup, p_dup, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "split", str(src), "--name", "干跑测试"],
    )
    assert c_dup == EXIT_CONFLICT
    assert p_dup["error"]["type"] == "target_exists"


def test_analyze_progress_token_stats_breaker_and_secret_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import backend.core.pipeline as pipeline_mod

    rt = tmp_path / "rt"
    secret_key = "sk-analyze-secret-abcdef999999"
    _write_config(rt, api_key=secret_key)
    book_dir = _seed_book(rt, "分析测试书", with_results=False, with_ledger=False, with_verdicts=False, with_report=False)

    mode = {"behavior": "ok"}

    class _FakePipeline:
        def __init__(self, config, directory, knowledge_file, start_chapter=1, end_chapter=None, on_progress=None, on_token_stats=None):
            assert config.working_directory == str(book_dir)
            self.on_progress = on_progress
            self.on_token_stats = on_token_stats

        def stop(self) -> None:
            pass

        async def run(self) -> Dict[str, Any]:
            if mode["behavior"] == "secret_crash":
                raise RuntimeError(f"upstream failed with key {secret_key} and https://u:p@host/x")
            if self.on_progress:
                await self.on_progress({
                    "chapter": 1,
                    "status": "done",
                    "progress": 1,
                    "total": 1,
                    "elapsed": 1.2,
                    "tokens": 150,
                    "result": {"huge_blob": "should_be_stripped"},
                    "message": "第1章完成",
                })
            if self.on_token_stats:
                await self.on_token_stats({"category": "analysis", "input_tokens": 100, "output_tokens": 50, "model": "test-model"})
            if mode["behavior"] == "breaker":
                return {
                    "stopped": True,
                    "total_analyzed": 0,
                    "errors": [],
                    "failed_chapters": [],
                    "skipped_chapters": [],
                    "account_tripped": True,
                    "account_trip_reason": "账户欠费",
                }
            return {
                "stopped": False,
                "total_analyzed": 1,
                "errors": [],
                "failed_chapters": [],
                "skipped_chapters": [],
                "account_tripped": False,
                "account_trip_reason": "",
            }

    monkeypatch.setattr(pipeline_mod, "AnalysisPipeline", _FakePipeline)

    # 1) 正常完成 + NDJSON 进度到 stderr + token_stats.json 写入 + strip "result"
    c_ok, p_ok, err = _run_json(
        capsys,
        ["--json", "--progress-jsonl", "--runtime-dir", str(rt), "analyze", "分析测试书", "--concurrency", "3"],
    )
    assert c_ok == EXIT_OK
    assert p_ok["data"]["result"]["total_analyzed"] == 1
    assert (book_dir / "output" / "token_stats.json").is_file()
    ndjson_lines = [json.loads(line) for line in err.strip().splitlines() if line.strip().startswith("{")]
    assert any(ev.get("event") == "progress" and "result" not in ev for ev in ndjson_lines)

    # 2) 账户级熔断 -> EXIT_BREAKER (5)
    mode["behavior"] = "breaker"
    c_brk, p_brk, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "analyze", "分析测试书"],
    )
    assert c_brk == EXIT_BREAKER
    assert p_brk["error"]["type"] == "account_breaker_tripped"

    # 3) 异常信息里的 API Key 与 URL 密码必须脱敏
    mode["behavior"] = "secret_crash"
    c_sec, p_sec, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "analyze", "分析测试书"],
    )
    assert c_sec == EXIT_ERROR
    assert secret_key not in p_sec["error"]["message"]
    assert "u:p@" not in p_sec["error"]["message"]
    assert "***" in p_sec["error"]["message"]


def test_summary_and_standalone_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import backend.services.final_summary.runner as runner_mod

    rt = tmp_path / "rt"
    _write_config(rt)
    book_dir = _seed_book(rt, "复检测试书")
    output_dir = book_dir / "output"
    cp_dir = output_dir / "final_summary_checkpoint"
    cp_dir.mkdir(parents=True, exist_ok=True)
    # 预置 stage1 baseline + stage2 断点 + 卷摘要
    baseline_ledger = ForeshadowLedger.load(output_dir / "foreshadow_ledger.json")
    baseline_ledger.items.append(
        ForeshadowItem(
            id="fs_base_only",
            description="基线恢复条目",
            first_seen_chapter=1,
            first_seen_batch=1,
            last_seen_chapter=2,
            last_seen_batch=1,
            status="active",
        )
    )
    (cp_dir / "stage1_ledger_baseline.json").write_text(
        json.dumps({"stage1_fp": "fp1", "ledger": baseline_ledger.to_dict()}, ensure_ascii=False),
        encoding="utf-8",
    )
    stage2_ckpt = cp_dir / "stage2_recheck_state.json"
    stage2_ckpt.write_text("{}", encoding="utf-8")

    # 把一条已有条目标为 human_override，验证 --from-baseline 保留人工覆写
    cur_ledger = ForeshadowLedger.load(output_dir / "foreshadow_ledger.json")
    cur_ledger.apply_human_override("fs_001", "confirm_resolved", resolution="人工锁定", resolved_chapter=4)
    cur_ledger.batch_size_snapshot = 42
    cur_ledger.save(output_dir / "foreshadow_ledger.json")

    captured: Dict[str, Any] = {}
    runner_mode = {"kind": "ok"}

    class _DummyOutcome:
        def summary(self) -> Dict[str, Any]:
            return {"status": "COMPLETED", "calls": 3}

    class _FakeRunner:
        def __init__(self, config, output_dir, start_chapter=1, end_chapter=999999, batch_size=None, concurrency=None, ledger_path=None, on_progress=None, on_token_stats=None):
            captured["init"] = {
                "working_directory": config.working_directory,
                "end_chapter": end_chapter,
                "recheck_concurrency": config.analysis.recheck_concurrency,
                "recheck_max_calls": config.analysis.recheck_max_calls,
                "gray_u4": config.analysis.recheck_v3_gray_u4,
                "mid_single_hop": config.analysis.recheck_v3_mid_single_hop,
            }
            self.config = config
            self.output_dir = output_dir
            self.batch_size = batch_size or 30
            self.ledger_path = ledger_path or (output_dir / "foreshadow_ledger.json")
            self.ledger = ForeshadowLedger.load(self.ledger_path)
            # 模拟真实 FinalSummaryRunner.__init__ 覆盖 batch_size_snapshot 的行为
            self.ledger.batch_size_snapshot = self.batch_size
            self._llm = type("_Llm", (), {"breaker_tripped": runner_mode["kind"] == "summary_breaker", "breaker_reason": "总结熔断"})()
            self._recheck_breaker_tripped = runner_mode["kind"] == "recheck_breaker"
            self._recheck_breaker_reason = "复检通道熔断" if self._recheck_breaker_tripped else ""
            self._stop_requested = False
            self.last_recheck_summary: Dict[str, Any] = {}

        def stop(self) -> None:
            self._stop_requested = True

        def get_token_stats(self) -> Dict[str, Dict[str, int]]:
            return {"summary": {"input_tokens": 200, "output_tokens": 80, "model": "test-model"}}

        def _load_results(self) -> List[dict]:
            return [{"ch": 1}]

        async def run(self) -> Any:
            if runner_mode["kind"] == "summary_breaker":
                return None
            return "# 新总结报告\n内容"

        async def _run_recheck_v3(self, results, volume_summaries=None):
            captured["recheck_items"] = {it.id: (it.status, it.source_type) for it in self.ledger.items}
            captured["batch_size_snapshot_at_recheck"] = self.ledger.batch_size_snapshot
            if runner_mode["kind"] == "recheck_none":
                self.last_recheck_summary = {"status": "NOT_RUN", "status_reason": "通道冲突"}
                return None
            self.last_recheck_summary = {"status": "COMPLETED", "calls": 3}
            self.ledger.save(self.ledger_path)
            return _DummyOutcome()

        async def _run_recheck_v2(self, results):
            return await self._run_recheck_v3(results)

    monkeypatch.setattr(runner_mod, "FinalSummaryRunner", _FakeRunner)

    # 1) summary 正常执行与落盘
    c_sum, p_sum, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "summary", "复检测试书"])
    assert c_sum == EXIT_OK
    assert captured["init"]["end_chapter"] == 999999
    assert (output_dir / "summary_token_stats.json").is_file()
    assert "# 新总结报告" in (output_dir / "final_summary_report.md").read_text(encoding="utf-8")

    # 2) summary 熔断 -> EXIT_BREAKER
    runner_mode["kind"] = "summary_breaker"
    c_sb, p_sb, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "summary", "复检测试书"])
    assert c_sb == EXIT_BREAKER
    assert p_sb["error"]["type"] == "breaker_tripped"

    # 3) 独立 recheck（无 --from-baseline）：验证保留现行 batch_size_snapshot=42 及分析参数注入
    runner_mode["kind"] = "ok"
    c_snap, p_snap, _ = _run_json(
        capsys,
        [
            "--json",
            "--runtime-dir", str(rt),
            "recheck", "复检测试书",
            "--concurrency", "5",
            "--max-calls", "120",
            "--gray-u4",
            "--no-mid-single-hop",
        ],
    )
    assert c_snap == EXIT_OK
    assert p_snap["data"]["recheck"]["status"] == "COMPLETED"
    assert captured["init"]["working_directory"] == str(book_dir)
    assert captured["init"]["recheck_concurrency"] == 5
    assert captured["init"]["recheck_max_calls"] == 120
    assert captured["init"]["gray_u4"] is True
    assert captured["init"]["mid_single_hop"] is False
    assert captured["batch_size_snapshot_at_recheck"] == 42

    # 4) 独立 recheck --from-baseline + --include-dormant：恢复基线条目、保留 human_override、唤醒 dormant 并清除 stage2 断点
    stage2_ckpt.write_text("{}", encoding="utf-8")
    c_rc, p_rc, _ = _run_json(
        capsys,
        [
            "--json",
            "--runtime-dir", str(rt),
            "recheck", "复检测试书",
            "--from-baseline",
            "--include-dormant",
        ],
    )
    assert c_rc == EXIT_OK
    assert p_rc["data"]["recheck"]["status"] == "COMPLETED"
    # baseline 条目被恢复、human_override 被保留、dormant 被临时激活参与复检且未解决后恢复 dormant
    assert captured["recheck_items"]["fs_base_only"][0] == "active"
    assert captured["recheck_items"]["fs_001"][1] == "human_override"
    assert captured["recheck_items"]["fs_003"][0] == "active"
    assert ForeshadowLedger.load(output_dir / "foreshadow_ledger.json")._find_item("fs_003").status == "dormant"
    assert not stage2_ckpt.exists()

    # 4) outcome is None -> exit 1 且带 status_reason
    runner_mode["kind"] = "recheck_none"
    c_none, p_none, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "recheck", "复检测试书"])
    assert c_none == EXIT_ERROR
    assert p_none["error"]["type"] == "recheck_not_run"
    assert "通道冲突" in p_none["error"]["message"]


# ==================== 5. config show / health 与密钥脱敏 ====================


def test_config_show_health_masking_and_corrupt_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rt = tmp_path / "rt"
    secret1 = "sk-primary-top-secret-111111"
    secret2 = "sk-recheck-top-secret-222222"
    _write_config(rt, api_key=secret1, recheck_api_key=secret2)

    c_show, p_show, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "config", "show"])
    assert c_show == EXIT_OK
    dumped = json.dumps(p_show, ensure_ascii=False)
    assert secret1 not in dumped
    assert secret2 not in dumped
    assert "user:pass@" not in dumped
    assert "rkuser:rkpass@" not in dumped
    assert p_show["data"]["config"]["api"]["api_key_set"] is True
    assert p_show["data"]["config"]["analysis"]["recheck_api_key_set"] is True

    # config health 不泄漏环境变量 token
    tok_val = "live-session-secret-token-xyz"
    monkeypatch.setenv("NOVEL_ANALYZER_SESSION_TOKEN", tok_val)
    c_h, p_h, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "config", "health"])
    assert c_h == EXIT_OK
    assert p_h["data"]["config_valid"] is True
    assert tok_val not in json.dumps(p_h, ensure_ascii=False)

    # 损坏的 config.json -> config show 报 EXIT_USAGE (2)
    (rt / "config.json").write_text("{broken json", encoding="utf-8")
    c_bad, p_bad, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "config", "show"])
    assert c_bad == EXIT_USAGE
    assert p_bad["error"]["type"] == "invalid_config"


def test_cli_config_file_merging_set_overrides_and_secret_redaction(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rt = tmp_path / "rt"
    _write_config(rt, api_key="sk-rt-default-123456")
    ext_cfg = tmp_path / "ext_preset.json"
    ext_cfg.write_text(
        json.dumps({
            "api": {"base_url": "https://api.example.invalid/v1", "api_key": "sk-ext-key-999999", "model": "gpt6-luna"},
            "analysis": {"block_size": 12, "recheck_v3_mode": "auto"},
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    # 1) 显式 --config 不存在 -> EXIT_NOT_FOUND (3)
    c_nf, p_nf, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "--config", str(tmp_path / "missing.json"), "config", "show"],
    )
    assert c_nf == EXIT_NOT_FOUND
    assert p_nf["error"]["type"] == "config_not_found"

    # 2) 前置/中间/后置多层 -o / --set 全部累积生效，且不修改磁盘 ext_preset.json
    c_ov, p_ov, _ = _run_json(
        capsys,
        [
            "--json",
            "--runtime-dir", str(rt),
            "--config", str(ext_cfg),
            "--set", "api.max_tokens=16384",
            "-o", "block_size=8",
            "config",
            "-o", "recheck_v3_mode=compact",
            "show",
            "--set", "recheck_v3_max_hops=1",
        ],
    )
    assert c_ov == EXIT_OK
    cfg_out = p_ov["data"]["config"]
    assert cfg_out["api"]["model"] == "gpt6-luna"
    assert cfg_out["api"]["max_tokens"] == 16384
    assert cfg_out["analysis"]["block_size"] == 8
    assert cfg_out["analysis"]["recheck_v3_mode"] == "compact"
    assert cfg_out["analysis"]["recheck_v3_max_hops"] == 1
    assert json.loads(ext_cfg.read_text(encoding="utf-8"))["analysis"]["block_size"] == 12

    # 3) --set 同时传明文 api_key 与越界参数触发校验失败时，原始密钥不得出现在 stderr/stdout
    raw_secret = "custom-plain-secret-key-abcdef987654"
    c_err, p_err, err_txt = _run_json(
        capsys,
        [
            "--json",
            "--runtime-dir", str(rt),
            "--set", f"api.api_key={raw_secret}",
            "-o", "analysis.block_size=999",
            "config", "show",
        ],
    )
    assert c_err == EXIT_USAGE
    assert raw_secret not in json.dumps(p_err, ensure_ascii=False)
    assert raw_secret not in err_txt


def test_cli_config_schema_get_set_patch_reset_and_presets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rt = tmp_path / "rt"
    secret = "sk-stored-secret-abcdef123456"
    _write_config(rt, api_key=secret)

    # 1) schema 过滤检索
    c_sc, p_sc, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "config", "schema"])
    assert c_sc == EXIT_OK
    assert p_sc["data"]["total_fields"] == 131
    assert p_sc["data"]["matched_fields"] == 131

    c_sf, p_sf, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "config", "schema", "--group", "recheck_tuning", "--search", "recheck_v3_pool_max_chars"],
    )
    assert c_sf == EXIT_OK
    assert [f["path"] for f in p_sf["data"]["fields"]] == ["analysis.recheck_v3_pool_max_chars"]

    # 2) get 默认脱敏，--include-credentials 可见；短键名解析有效
    c_g1, p_g1, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "config", "get", "api_key"])
    assert c_g1 == EXIT_OK
    assert p_g1["data"]["value"] == "***"
    assert p_g1["data"]["is_set"] is True

    c_g2, p_g2, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "config", "get", "api.api_key", "--include-credentials"],
    )
    assert c_g2 == EXIT_OK
    assert p_g2["data"]["value"] == secret

    # 3) set / patch(文件与stdin) + 未知键拒绝
    c_set, p_set, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "config", "set", "recheck_v3_mode=wide", "block_size=15"],
    )
    assert c_set == EXIT_OK
    assert p_set["data"]["config"]["analysis"]["recheck_v3_mode"] == "wide"
    assert p_set["data"]["config"]["analysis"]["block_size"] == 15

    patch_file = tmp_path / "patch.json"
    patch_file.write_text(json.dumps({"analysis": {"atlas_max_characters": 80}}), encoding="utf-8")
    c_pf, p_pf, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "config", "patch", "--file", str(patch_file)],
    )
    assert c_pf == EXIT_OK
    assert p_pf["data"]["config"]["analysis"]["atlas_max_characters"] == 80

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"analysis": {"dormant_chapter_threshold": 150}})))
    c_ps, p_ps, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "config", "patch", "--file", "-"],
    )
    assert c_ps == EXIT_OK
    assert p_ps["data"]["config"]["analysis"]["dormant_chapter_threshold"] == 150

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"analysis": {"no_such_field": 1}})))
    c_bad_p, p_bad_p, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "config", "patch", "--file", "-"],
    )
    assert c_bad_p == EXIT_USAGE
    assert p_bad_p["error"]["type"] == "unknown_config_key"

    # 4) reset 单键 & reset --all 默认保凭据、--include-credentials 全清
    c_rk, p_rk, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "config", "reset", "block_size"])
    assert c_rk == EXIT_OK
    assert p_rk["data"]["config"]["analysis"]["block_size"] == 1
    assert p_rk["data"]["config"]["analysis"]["recheck_v3_mode"] == "wide"

    c_ra, p_ra, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "config", "reset", "--all"])
    assert c_ra == EXIT_OK
    assert p_ra["data"]["credentials_preserved"] is True
    assert p_ra["data"]["config"]["analysis"]["recheck_v3_mode"] == "auto"
    assert json.loads((rt / "config.json").read_text(encoding="utf-8"))["api"]["api_key"] == secret

    c_rac, p_rac, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "config", "reset", "--all", "--include-credentials"],
    )
    assert c_rac == EXIT_OK
    assert p_rac["data"]["credentials_preserved"] is False
    assert json.loads((rt / "config.json").read_text(encoding="utf-8"))["api"]["api_key"] == ""

    # 5) presets 列出内置预设
    c_pr, p_pr, _ = _run_json(capsys, ["--json", "--runtime-dir", str(rt), "config", "presets"])
    assert c_pr == EXIT_OK
    assert isinstance(p_pr["data"]["presets"], dict) and len(p_pr["data"]["presets"]) >= 1


def test_cli_config_mutation_routes_to_live_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    from backend.api import routes_settings
    from backend.config.settings import ConfigManager

    rt = tmp_path / "rt"
    _write_config(rt, api_key="sk-live-cfg-123456")
    srv_cfg_path = tmp_path / "srv_config.json"
    srv_cfg_path.write_text((rt / "config.json").read_text(encoding="utf-8"), encoding="utf-8")
    srv_mgr = ConfigManager(srv_cfg_path)
    srv_mgr.load()
    fake_analysis = SimpleNamespace(is_running=False, config_manager=srv_mgr)
    fake_summary = SimpleNamespace(is_running=False)
    monkeypatch.setattr(routes_settings, "get_service", lambda: fake_analysis)
    monkeypatch.setattr("backend.services.summary_service.get_summary_service", lambda: fake_summary)

    token = "cli-settings-live-token"
    monkeypatch.setenv("NOVEL_ANALYZER_SESSION_TOKEN", token)
    (rt / "session.token").write_text(token, encoding="utf-8")
    (rt / "app.lock").write_text(json.dumps({"pid": 1001, "port": 18888, "started_at": 1}), encoding="utf-8")

    mini_app = FastAPI()

    @mini_app.get("/api/health")
    async def _health():
        return {"app": "novel-analyzer-web", "status": "ok"}

    mini_app.include_router(routes_settings.router)
    test_client = TestClient(mini_app)
    seen_calls: List[tuple[str, str]] = []

    class _SettingsBridge:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> "_SettingsBridge":
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

        def get(self, url: str, **kwargs: Any) -> Any:
            path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url
            seen_calls.append(("GET", path))
            return test_client.get(path, **kwargs)

        def request(self, method: str, url: str, **kwargs: Any) -> Any:
            path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url
            seen_calls.append((method, path))
            return test_client.request(method, path, **kwargs)

    monkeypatch.setattr(httpx, "Client", _SettingsBridge)

    c_set, p_set, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "config", "set", "analysis.block_size=9"],
    )
    assert c_set == EXIT_OK
    assert p_set["data"]["persistence"] == "live_server"
    assert ("PATCH", "/api/settings") in seen_calls
    assert json.loads(srv_cfg_path.read_text(encoding="utf-8"))["analysis"]["block_size"] == 9

    c_rst, p_rst, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "config", "reset", "--all"],
    )
    assert c_rst == EXIT_OK
    assert p_rst["data"]["persistence"] == "live_server"
    assert ("PUT", "/api/settings") in seen_calls
    srv_disk = json.loads(srv_cfg_path.read_text(encoding="utf-8"))
    assert srv_disk["analysis"]["block_size"] == 1
    assert srv_disk["api"]["api_key"] == "sk-live-cfg-123456"


def test_cli_analyze_knowledge_file_and_summary_concurrency_wiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import backend.core.pipeline as pipeline_mod
    import backend.services.final_summary.runner as runner_mod

    rt = tmp_path / "rt"
    _write_config(rt)
    book_dir = _seed_book(rt, "连线测试书", with_ledger=False, with_verdicts=False, with_report=False)
    seen: Dict[str, Any] = {}

    class _FakePipe:
        def __init__(self, config, directory, knowledge_file, **_kw: Any) -> None:
            seen["knowledge_file"] = Path(knowledge_file)
            seen["max_tokens"] = config.api.max_tokens

        def stop(self) -> None:
            pass

        async def run(self) -> Dict[str, Any]:
            return {"stopped": False, "total_analyzed": 1, "errors": [], "failed_chapters": []}

    class _FakeSumRunner:
        def __init__(self, config, output_dir, **kw: Any) -> None:
            seen["summary_concurrency"] = config.analysis.summary_concurrency
            seen["main_concurrency"] = config.analysis.concurrency
            seen["kw_concurrency"] = kw.get("concurrency")
            self.ledger = ForeshadowLedger()

        def stop(self) -> None:
            pass

        def get_token_stats(self) -> Dict[str, Any]:
            return {}

        async def run(self) -> str:
            return "# 报告"

    monkeypatch.setattr(pipeline_mod, "AnalysisPipeline", _FakePipe)
    monkeypatch.setattr(runner_mod, "FinalSummaryRunner", _FakeSumRunner)

    c_an, _p_an, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "-o", "knowledge_file=custom_kb.json", "-o", "max_tokens=32768", "analyze", "连线测试书"],
    )
    assert c_an == EXIT_OK
    assert seen["knowledge_file"] == book_dir / "custom_kb.json"
    assert seen["max_tokens"] == 32768

    c_su, _p_su, _ = _run_json(
        capsys,
        ["--json", "--runtime-dir", str(rt), "summary", "连线测试书", "--concurrency", "7"],
    )
    assert c_su == EXIT_OK
    assert seen["summary_concurrency"] == 7
    assert seen["main_concurrency"] == 2
    assert seen["kw_concurrency"] == 7
