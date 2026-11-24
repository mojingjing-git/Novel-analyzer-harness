"""人物分隔、按需指纹聚合缓存、全书语义图谱清洗、/api/viz/atlas 与别名归并角色卡测试。"""
from __future__ import annotations

import json
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_aggregate, routes_books, routes_viz
from backend.config.constants import STORY_ATLAS_FILE, STORY_ATLAS_SCHEMA_VERSION
from backend.services.final_summary.atlas_extractor import (
    parse_and_sanitize_char_atlas,
    parse_and_sanitize_world_atlas,
    sanitize_world_tree,
)
from backend.utils.aggregate_utils import ensure_aggregated_cache, split_character_names
from backend.utils.character_card_generator import CharacterCardGenerator


def _write_chapter(out_dir, ch: int, characters: str, event_desc: str, arcs=None) -> None:
    (out_dir / f"chapter_{ch}_result.json").write_text(
        json.dumps(
            {
                "chapter_number": ch,
                "cross_block": {"summary": f"第{ch}章摘要", "unresolved_questions": [], "new_leads": []},
                "core_events": [
                    {
                        "id": f"e{ch}",
                        "event": event_desc,
                        "characters": characters,
                        "function": "推进",
                        "importance": "高",
                    }
                ],
                "character_arcs": arcs or [],
                "foreshadowing": [],
                "plot_holes": [],
                "locations": [],
                "spatial_relationships": [],
                "long_context_insights": {},
                "updated_knowledge": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_split_character_names_variants():
    assert split_character_names("李火旺、白灵淼，丹阳子,李岁;吕状元；诸葛渊") == [
        "李火旺", "白灵淼", "丹阳子", "李岁", "吕状元", "诸葛渊"
    ]
    assert split_character_names(["李火旺（主角）", "李火旺", "  ", "心素(已故)"]) == [
        "李火旺", "李火旺", "心素"
    ]
    assert split_character_names("") == []


def test_ensure_aggregated_cache_and_excel_auto_route(tmp_path, monkeypatch):
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    _write_chapter(out_dir, 1, "张三、李四（主角）", "初遇入局")

    agg_dir = ensure_aggregated_cache(out_dir)
    assert agg_dir == out_dir / "aggregated"
    fp_path = agg_dir / "_fingerprint.json"
    assert fp_path.exists()
    first_mtime = fp_path.stat().st_mtime_ns

    # 二次调用命中指纹，不重建
    ensure_aggregated_cache(out_dir)
    assert fp_path.stat().st_mtime_ns == first_mtime

    # 修改章节输入后缓存自动失活并重建
    _write_chapter(out_dir, 2, "张三、王五", "再起风波")
    ensure_aggregated_cache(out_dir)
    tracking = json.loads((agg_dir / "character_tracking_aggregated.json").read_text(encoding="utf-8"))
    assert "王五" in tracking and "李四" in tracking

    monkeypatch.setattr(routes_aggregate, "get_output_dir", lambda bid: out_dir if bid == "b1" else None)
    app = FastAPI()
    app.include_router(routes_aggregate.router)
    client = TestClient(app)

    # /files 不暴露 _fingerprint.json 等内部文件
    files_resp = client.get("/api/aggregate/b1/files")
    assert files_resp.status_code == 200
    names = [f["name"] for f in files_resp.json()["files"]]
    assert "_fingerprint.json" not in names
    assert "character_tracking_aggregated.json" in names

    # 未前置调用 /aggregate/run 时，删除 aggregated/ 后直调 /excel 仍自动补齐并导出
    import shutil
    from pathlib import Path
    shutil.rmtree(agg_dir)
    excel_resp = client.post("/api/aggregate/b1/excel")
    assert excel_resp.status_code == 200 and excel_resp.json()["ok"] is True
    assert Path(excel_resp.json()["path"]).exists()


def test_atlas_sanitization_protects_canonical_and_limits_cycles():
    # 1. 角色图谱：canonical 保护、短别名过滤、milestones <= 4、悬空与自环边过滤
    raw_char_json = json.dumps(
        {
            "characters": [
                {
                    "canonical_name": "李火旺",
                    "aliases": ["坐忘道红中", "李岁", "红", "李火旺"],  # 李岁为他者canonical，红<2字，李火旺为自身
                    "faction": "大齐",
                    "role_tier": "protagonist",
                    "first_chapter": 1,
                    "identity_summary": "主线核心视角人物",
                    "arc_milestones": [{"chapter_range": f"{i}", "milestone": f"m{i}"} for i in range(1, 7)],
                },
                {
                    "canonical_name": "李岁（投影）",
                    "aliases": ["岁岁"],
                    "faction": "清风观",
                    "role_tier": "major_ally",
                    "first_chapter": 20,
                    "identity_summary": "独立人物",
                    "arc_milestones": [],
                },
                {
                    "canonical_name": "李岁",
                    "aliases": [],
                    "faction": "清风观",
                    "role_tier": "major_ally",
                    "first_chapter": 20,
                    "identity_summary": "被引用为canonical的角色",
                    "arc_milestones": [],
                },
            ],
            "relationships": [
                {"source": "李火旺", "target": "李岁", "relation_tag": "父女", "category": "ally", "evidence_chapters": [20, 5]},
                {"source": "李火旺", "target": "李火旺", "relation_tag": "自环", "category": "complex"},
                {"source": "李火旺", "target": "不存在角色", "relation_tag": "悬空", "category": "hostile"},
            ],
        },
        ensure_ascii=False,
    )
    cleaned = parse_and_sanitize_char_atlas(raw_char_json)
    assert cleaned is not None
    li = next(c for c in cleaned["characters"] if c["canonical_name"] == "李火旺")
    assert li["aliases"] == ["坐忘道红中"]
    assert len(li["arc_milestones"]) == 4
    assert len(cleaned["relationships"]) == 1
    assert cleaned["relationships"][0]["target"] == "李岁"
    assert cleaned["relationships"][0]["evidence_chapters"] == [5, 20]

    # 2. 世界树：按路径防环 + 深度截断 <= 10
    deep_node: dict = {"name": "层12", "node_type": "landmark", "children": []}
    for lv in range(11, 0, -1):
        deep_node = {"name": f"层{lv}", "node_type": "region", "children": [deep_node]}
    cyclic_tree = [
        {
            "name": "根世界",
            "node_type": "world",
            "children": [{"name": "根世界", "node_type": "region", "children": []}, deep_node],
        }
    ]
    sanitized_tree = sanitize_world_tree(cyclic_tree, max_depth=10)
    # 第一子节点与祖先同名被裁除
    assert all(ch["name"] != "根世界" for ch in sanitized_tree[0]["children"])
    # 从 层1 到最深不得超过 max_depth=10
    cur = sanitized_tree[0]
    depth = 1
    while cur["children"]:
        cur = cur["children"][0]
        depth += 1
    assert depth <= 10

    raw_world_json = json.dumps(
        {
            "stages": [
                {"stage_no": 2, "title": "后段", "start_chapter": 50, "end_chapter": 100},
                {"stage_no": 1, "title": "开篇", "start_chapter": 1, "end_chapter": 49},
            ],
            "world_tree": [{"name": "大齐", "node_type": "world", "summary": "主世界", "children": []}],
        },
        ensure_ascii=False,
    )
    w = parse_and_sanitize_world_atlas(raw_world_json)
    assert w is not None and [s["title"] for s in w["stages"]] == ["开篇", "后段"]
    assert [s["stage_no"] for s in w["stages"]] == [1, 2]


def test_viz_atlas_and_alias_merged_character_card_routes(tmp_path, monkeypatch):
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    _write_chapter(
        out_dir,
        1,
        "李火旺、坐忘道红中",
        "同一章内用双身份行动",
        arcs=[{"name": "坐忘道红中", "surface_action": "伪装骗局", "inner_motivation": "破局", "change_delta": "成长", "driver": "执念"}],
    )
    _write_chapter(out_dir, 2, "坐忘道红中、李岁", "联合出手")

    monkeypatch.setattr("backend.services.book_service.get_output_dir", lambda bid: out_dir if bid == "b1" else None)
    app = FastAPI()
    app.include_router(routes_viz.router)
    app.include_router(routes_books.router)
    client = TestClient(app)

    # 无 story_atlas.json 时返回 exists=False
    empty_atlas = client.get("/api/viz/atlas/b1")
    assert empty_atlas.status_code == 200 and empty_atlas.json()["exists"] is False

    # 写入多世界根与 0 事件隐藏角色
    (out_dir / STORY_ATLAS_FILE).write_text(
        json.dumps(
            {
                "schema_version": STORY_ATLAS_SCHEMA_VERSION,
                "generated_at": "2026-11-23T00:00:00",
                "model": "test-model",
                "characters": [
                    {
                        "canonical_name": "李火旺",
                        "aliases": ["坐忘道红中"],
                        "faction": "清风观",
                        "role_tier": "protagonist",
                        "first_chapter": 1,
                        "identity_summary": "双世界行者",
                        "arc_milestones": [{"chapter_range": "1-2", "milestone": "初醒"}],
                    },
                    {
                        "canonical_name": "季灾",
                        "aliases": [],
                        "faction": "司命",
                        "role_tier": "hidden_power",
                        "first_chapter": 100,
                        "identity_summary": "幕后无单章事件角色",
                        "arc_milestones": [],
                    },
                ],
                "relationships": [
                    {"source": "李火旺", "target": "季灾", "relation_tag": "对立", "category": "hostile", "evolution": "宿命博弈", "evidence_chapters": [100]},
                ],
                "stages": [
                    {"stage_no": 1, "title": "大齐开局", "start_chapter": 1, "end_chapter": 50, "world_realm": "大齐", "primary_location": "清风观"},
                ],
                "world_tree": [
                    {"name": "大齐", "node_type": "world", "summary": "现实界域", "children": []},
                    {"name": "白玉京", "node_type": "world", "summary": "异界", "children": []},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    atlas_resp = client.get("/api/viz/atlas/b1")
    assert atlas_resp.status_code == 200
    atlas = atlas_resp.json()
    assert atlas["exists"] is True and len(atlas["world_tree"]) == 2

    # 角色列表自动聚合且含 0-event atlas 角色，无未聚合马甲条目泄露为主角外独立条（坐忘道红中已并入李火旺）
    list_resp = client.get("/api/books/b1/characters")
    assert list_resp.status_code == 200
    names = {c["name"] for c in list_resp.json()["characters"]}
    assert "李火旺" in names and "季灾" in names and "坐忘道红中" not in names

    # 别名直查不报 404，自动定向至规范名并合并事件（第1章同 event 去重后应计 2 事件，共现不含自身自环）
    alias_card = client.get("/api/books/b1/characters/坐忘道红中")
    assert alias_card.status_code == 200
    card = alias_card.json()["character"]
    assert card["name"] == "李火旺"
    assert card["aliases"] == ["坐忘道红中"]
    assert card["total_events"] == 2
    assert len(card["arcs"]) == 1
    assert "李火旺" not in card["relationships"]
    assert "坐忘道红中" not in card["relationships"]

    # 0 事件图谱角色直查不报 404
    zero_card = client.get("/api/books/b1/characters/季灾")
    assert zero_card.status_code == 200
    assert zero_card.json()["character"]["name"] == "季灾"
    assert zero_card.json()["character"]["total_events"] == 0
