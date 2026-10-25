"""Step1-1A：角色卡生成器回归（第三批清理，2026-10-14）

背景：``CharacterCardGenerator.long_context_insights`` 只有初始化/赋值、没有输出消费者，
但读取 ``long_context_insights_aggregated.json`` 失败会走 ``load_aggregated_data`` 的
``except`` 分支返回 False —— 于是合法 ``character_tracking`` 的索引变空、有效角色详情
抛 KeyError（路由 404）。Step1-1A 删除该状态与读链。

本文件用**真实生成器**与**真实路由入口**验证：

- 正常索引排序 / 详情字段；
- 洞察文件缺失 / 损坏 / 为空数组时，不影响索引与详情（这是修复的核心契约）；
- 空目录 / 不存在角色的既有异常契约（``KeyError`` → 路由 404）；
- arcs / events 等**真正的数据依赖**仍然按原契约要求字段，缺字段仍抛异常
  （不因删除洞察读取而把真实数据错误一起吞掉）。

全部使用 tmp 目录合成聚合文件，不读真实 workspace。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.utils.character_card_generator import CharacterCardGenerator  # noqa: E402


TRACKING = {
    "林动": {
        "first_appearance": 1,
        "total_events": 7,
        "chapters": [1, 2, 5],
        "events": [{"chapter": 1, "event": "古碑现世"}],
        "state_history": {"1": "懵懂", "2": "坚定"},
    },
    "萧炎": {
        "first_appearance": 2,
        "total_events": 3,
        "chapters": [2],
        "events": [],
        "state_history": {},
    },
}

ARCS = {
    "chapter_2": [
        {
            "name": "林动",
            "surface_action": "进入古碑",
            "inner_motivation": "变强",
            "change_delta": "获得传承",
            "driver": "复仇",
        }
    ]
}

# 注意：`_analyze_relationships` 只把全角逗号（U+FF0C）归一为分隔符后再按半角逗号切分；
# 顿号（U+3001）**不**在支持的切分字符内（既有行为，本步不改）。fixture 用真实数据形态，
# 并在下方用一条用例锁定这一现状契约。
EVENTS = {
    "chapter_2": [
        {
            "characters": "林动，萧炎",
            "event": "古碑争锋",
            "function": "立敌",
        }
    ]
}


def _write(base: Path, name: str, payload) -> None:
    (base / name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def book_output(tmp_path):
    """构造 output/ 目录（含 tracking/arcs/events 三个聚合文件）"""
    out = tmp_path / "output"
    out.mkdir()
    _write(out, "character_tracking_aggregated.json", TRACKING)
    _write(out, "character_arcs_aggregated.json", ARCS)
    _write(out, "core_events_aggregated.json", EVENTS)
    return out


# ---------------------------------------------------------------------------
# 状态与读链已删除
# ---------------------------------------------------------------------------

def test_no_long_context_insights_state(book_output):
    """生成器不再持有 long_context_insights 状态，也不再读该聚合文件"""
    gen = CharacterCardGenerator(book_output)
    assert not hasattr(gen, "long_context_insights"), "无用状态未删除"

    _write(book_output, "long_context_insights_aggregated.json", {"pacing": []})
    assert gen.load_aggregated_data() is True
    assert not hasattr(gen, "long_context_insights"), "加载后不应重建该状态"


def test_insights_file_opened_after_removal(book_output, monkeypatch):
    """读链删除后，不再打开 long_context_insights_aggregated.json"""
    opened: list[str] = []
    real_open = open

    def spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy_open)
    gen = CharacterCardGenerator(book_output)
    gen.load_aggregated_data()

    assert not any("long_context_insights" in path for path in opened), (
        f"仍打开了洞察文件: {opened}"
    )


# ---------------------------------------------------------------------------
# 正常数据契约（洞察文件各种形态都不影响）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "insights",
    [
        pytest.param(None, id="missing"),
        pytest.param("{ 这不是 JSON", id="corrupted"),
        pytest.param([], id="empty_list"),
        pytest.param({}, id="empty_dict"),
        pytest.param({"pacing": [{"chapter": 1}]}, id="valid"),
    ],
)
def test_summaries_and_card_survive_insights_states(book_output, insights):
    """洞察文件缺失/损坏/为空/正常，索引与详情都必须照常工作"""
    if insights is not None:
        (book_output / "long_context_insights_aggregated.json").write_text(
            json.dumps(insights, ensure_ascii=False)
            if not isinstance(insights, str)
            else insights,
            encoding="utf-8",
        )

    gen = CharacterCardGenerator(book_output)
    summaries = gen.get_character_summaries()

    assert [item["name"] for item in summaries] == ["林动", "萧炎"], (
        f"索引应按 total_events 降序且完整: {summaries}"
    )
    assert summaries[0]["first_appearance"] == 1
    assert summaries[0]["total_events"] == 7
    assert summaries[0]["chapters_count"] == 3

    card = gen.get_card_data("林动")
    assert card["name"] == "林动"
    assert card["total_events"] == 7
    assert card["chapters"] == [1, 2, 5]
    assert card["arcs"] == [
        {
            "chapter": 2,
            "surface_action": "进入古碑",
            "inner_motivation": "变强",
            "change_delta": "获得传承",
            "driver": "复仇",
        }
    ]
    assert card["events"] == [{"chapter": 1, "event": "古碑现世"}]
    assert card["states"] == {1: "懵懂", 2: "坚定"}
    assert card["relationships"]["萧炎"]["event_count"] == 1


# ---------------------------------------------------------------------------
# 空目录 / 不存在角色：既有异常契约
# ---------------------------------------------------------------------------

def test_empty_dir_summaries_returns_empty(tmp_path):
    """空目录：索引返回空列表（不抛异常）"""
    gen = CharacterCardGenerator(tmp_path / "nothing")
    assert gen.get_character_summaries() == []


def test_empty_dir_card_raises_keyerror(tmp_path):
    """空目录：详情抛 KeyError（路由转 404）"""
    gen = CharacterCardGenerator(tmp_path / "nothing")
    with pytest.raises(KeyError):
        gen.get_card_data("任何人")


def test_unknown_character_raises_keyerror(book_output):
    """不存在的角色：详情抛 KeyError（路由转 404）"""
    gen = CharacterCardGenerator(book_output)
    with pytest.raises(KeyError):
        gen.get_card_data("不存在的人")


# ---------------------------------------------------------------------------
# 真正的数据依赖：缺字段仍必须抛异常（不能被"全吞异常"掩盖）
# ---------------------------------------------------------------------------

def test_arcs_missing_required_field_still_raises(book_output):
    """arcs 缺少必需字段（driver）仍抛 KeyError —— 真实数据错误不得被吞掉"""
    _write(
        book_output,
        "character_arcs_aggregated.json",
        {"chapter_2": [{"name": "林动", "surface_action": "x",
                        "inner_motivation": "y", "change_delta": "z"}]},
    )
    gen = CharacterCardGenerator(book_output)
    with pytest.raises(KeyError):
        gen.get_card_data("林动")


def test_tracking_corrupted_raises_keyerror(book_output):
    """tracking 文件损坏：load 失败 → 详情抛 KeyError（原契约）"""
    (book_output / "character_tracking_aggregated.json").write_text(
        "{ 坏了", encoding="utf-8"
    )
    gen = CharacterCardGenerator(book_output)
    with pytest.raises(KeyError):
        gen.get_card_data("林动")


def test_find_aggregated_supports_nested_dir(tmp_path):
    """聚合文件在 output/aggregated/ 下也能被找到（_find_aggregated 双候选）"""
    out = tmp_path / "output"
    nested = out / "aggregated"
    nested.mkdir(parents=True)
    _write(nested, "character_tracking_aggregated.json", TRACKING)
    _write(nested, "character_arcs_aggregated.json", ARCS)
    _write(nested, "core_events_aggregated.json", EVENTS)

    gen = CharacterCardGenerator(out)
    assert [item["name"] for item in gen.get_character_summaries()] == ["林动", "萧炎"]


# ---------------------------------------------------------------------------
# 真实路由入口：KeyError → 404，正常 → 200
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from backend.api.routes_books import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_route_card_returns_404_for_unknown_character(client, book_output, monkeypatch):
    """不存在的角色 → 路由 404（KeyError 契约）"""
    import backend.api.routes_books as routes_books
    import backend.utils.aggregate_utils as aggregate_utils

    class _NoopAggregator(aggregate_utils.JSONAggregator):
        def aggregate_character_tracking(self, output_path=None):
            return {}

    # 路由在函数体内 import，patch 模块属性才生效
    monkeypatch.setattr(aggregate_utils, "JSONAggregator", _NoopAggregator)
    monkeypatch.setattr(
        routes_books.book_service, "get_output_dir", lambda book_id: book_output
    )

    resp = client.get("/api/books/测试书/characters/不存在的人")
    assert resp.status_code == 404, resp.text


def test_route_characters_ok_with_corrupted_insights(client, book_output, monkeypatch):
    """洞察文件损坏时，列表与详情端点仍 200（修复的核心契约）"""
    import backend.api.routes_books as routes_books

    (book_output / "long_context_insights_aggregated.json").write_text(
        "{ 坏了", encoding="utf-8"
    )

    import backend.utils.aggregate_utils as aggregate_utils

    class _NoopAggregator(aggregate_utils.JSONAggregator):
        def aggregate_character_tracking(self, output_path=None):
            return {}

    monkeypatch.setattr(aggregate_utils, "JSONAggregator", _NoopAggregator)
    monkeypatch.setattr(
        routes_books.book_service, "get_output_dir", lambda book_id: book_output
    )

    listing = client.get("/api/books/测试书/characters")
    assert listing.status_code == 200, listing.text
    names = [item["name"] for item in listing.json()["characters"]]
    assert names == ["林动", "萧炎"], names

    card = client.get("/api/books/测试书/characters/林动")
    assert card.status_code == 200, card.text
    assert card.json()["character"]["name"] == "林动"


def test_ideographic_comma_not_a_separator(book_output):
    """现状契约：顿号（U+3001）不切分人物，整串被当作一个名字

    这是既有行为（本步不修）：`_analyze_relationships` 只归一全角逗号 U+FF0C。
    锁定现状，避免后续误当作回归。
    """
    _write(
        book_output,
        "core_events_aggregated.json",
        {"chapter_2": [{"characters": "林动、萧炎", "event": "争锋", "function": "立敌"}]},
    )
    gen = CharacterCardGenerator(book_output)
    card = gen.get_card_data("林动")
    # "林动、萧炎" 不等于 "林动"，故不产生任何关系
    assert card["relationships"] == {}
