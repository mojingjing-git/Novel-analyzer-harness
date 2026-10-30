"""地点归一化相关测试（2026-08-21 spec）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.utils.text_utils import is_same_location


class TestIsSameLocation:
    def test_exact_match_returns_true(self):
        assert is_same_location("宁安县", "宁安县") is True

    def test_normalized_match_returns_true(self):
        # 标点不影响
        assert is_same_location("宁安县", "宁安县。") is True

    def test_short_county_vs_full_county_below_threshold(self):
        # "宁安县" vs "宁安县城" — SequenceMatcher ratio = 0.75（4/6）
        # 应判定为不同地点，避免短名误伤
        assert is_same_location("宁安县", "宁安县城") is False

    def test_same_name_with_role_suffix_merges(self):
        # "居安小阁" vs "居安小阁（主角）" — ratio 0.91，合并
        assert is_same_location("居安小阁", "居安小阁（主角）") is True

    def test_completely_different_names_returns_false(self):
        assert is_same_location("大贞", "大秀") is False

    def test_short_strings_below_min_length(self):
        # 长度 ≤ 1 的字符串走不到 SequenceMatcher 分支，直接返回 False
        assert is_same_location("京", "京") is False  # 长度 1，但 normalize 后也是 1

    # === 2026-08-21 修复新算法：对称剥离前后缀修饰符 ===

    def test_prefix_modifier_merged(self):
        """前缀软修饰（新/老/旧/原）应识别 → 合并"""
        assert is_same_location("大唐公司", "新大唐公司") is True
        assert is_same_location("大唐公司", "老大唐公司") is True
        assert is_same_location("宁安县", "旧宁安县") is True
        assert is_same_location("宁安县", "原宁安县") is True

    def test_compound_prefix_merged(self):
        """复合前缀（原址/旧址）应识别 → 合并"""
        assert is_same_location("宁安县", "原宁安县原址") is True
        assert is_same_location("宁安县", "旧宁安县旧址") is True

    def test_no_entity_word_merge(self):
        """实体词（会议室/顶层）不应合并（v1 substring 规则会误并）"""
        # 这两个本质不同：一个是公司，一个是公司内的会议室
        assert is_same_location("大唐公司", "大唐公司会议室") is False
        # 一个是酒店，一个是酒店顶层
        assert is_same_location("新罗酒店", "新罗酒店顶层") is False

    def test_chained_prefix_modifier(self):
        """链式前缀修饰应递归剥离（'旧原宁安县' → '宁安县'）→ 合并"""
        assert is_same_location("宁安县", "旧原宁安县") is True

    def test_suffix_modifier_merged(self):
        """后缀软修饰（主角/已废弃等）应识别 → 合并（括号由 normalize 阶段剥离）"""
        assert is_same_location("宁安县", "宁安县新址") is True   # 审查反馈补的
        assert is_same_location("居安小阁", "居安小阁已废弃") is True
        assert is_same_location("宁安县", "宁安县境内") is True
        assert is_same_location("大唐公司", "大唐公司内部") is True


from backend.services.location_normalizer import (
    aggregate_locations,
    aggregate_spatial_pairs,
    _estimate_group_chars,
    _split_into_batches_by_budget,
    _LOCATION_PROMPT_BUDGET_CHARS,
    _LOCATION_BATCH_MAX_GROUPS,
)


class TestAggregateLocations:
    def test_groups_by_exact_name(self):
        raw = [
            {"name": "宁安县", "parent": "大周王朝", "type": "城市", "description": "县城1", "chapter": 1},
            {"name": "宁安县", "parent": "大周王朝", "type": "城市", "description": "县城2", "chapter": 2},
            {"name": "居安小阁", "parent": "宁安县", "type": "建筑", "description": "小屋", "chapter": 3},
        ]
        groups = aggregate_locations(raw)
        assert len(groups) == 2
        by_name = {g["aliases"][0]: g for g in groups}
        assert by_name["宁安县"]["chapter_count"] == 2
        assert by_name["居安小阁"]["chapter_count"] == 1

    def test_groups_similar_names_via_sequence_matcher(self):
        raw = [
            {"name": "居安小阁", "parent": "宁安县", "type": "建筑", "description": "甲", "chapter": 1},
            {"name": "居安小阁（主角）", "parent": "宁安县", "type": "住宅", "description": "乙", "chapter": 2},
        ]
        groups = aggregate_locations(raw)
        assert len(groups) == 1
        assert set(groups[0]["aliases"]) == {"居安小阁", "居安小阁（主角）"}

    def test_aggregates_types_and_parents(self):
        raw = [
            {"name": "宁安县", "parent": "大周王朝", "type": "城市", "description": "", "chapter": 1},
            {"name": "宁安县", "parent": "大贞", "type": "城池", "description": "", "chapter": 2},
        ]
        groups = aggregate_locations(raw)
        assert groups[0]["types"] == {"城市": 1, "城池": 1}
        assert groups[0]["parents"] == {"大周王朝": 1, "大贞": 1}

    def test_skips_empty_names(self):
        raw = [
            {"name": "", "parent": "", "type": "", "description": "", "chapter": 1},
            {"name": "居安小阁", "parent": "", "type": "建筑", "description": "", "chapter": 2},
        ]
        groups = aggregate_locations(raw)
        assert len(groups) == 1

    def test_sorted_by_chapter_count_desc(self):
        raw = [
            {"name": "低频", "parent": "", "type": "城市", "description": "", "chapter": 1},
            {"name": "高频", "parent": "", "type": "城市", "description": "", "chapter": 1},
            {"name": "高频", "parent": "", "type": "城市", "description": "", "chapter": 2},
            {"name": "高频", "parent": "", "type": "城市", "description": "", "chapter": 3},
        ]
        groups = aggregate_locations(raw)
        assert groups[0]["aliases"] == ["高频"]
        assert groups[1]["aliases"] == ["低频"]


class TestAggregateSpatialPairs:
    def test_groups_same_pair_across_chapters(self):
        raw = [
            {"from": "宁安县", "to": "德胜府", "relation": "位于东南方向，约两三百里", "chapter": 1},
            {"from": "宁安县", "to": "德胜府", "relation": "计缘带陆乘风魂魄飞行约半个时辰可达", "chapter": 2},
        ]
        pairs = aggregate_spatial_pairs(raw)
        assert len(pairs) == 1
        assert pairs[0]["from"] == "宁安县"
        assert pairs[0]["to"] == "德胜府"
        assert len(pairs[0]["relations"]) == 2
        assert pairs[0]["chapters"] == [1, 2]

    def test_separates_pairs_with_different_endpoints(self):
        raw = [
            {"from": "宁安县", "to": "德胜府", "relation": "相邻", "chapter": 1},
            {"from": "宁安县", "to": "大贞", "relation": "从属", "chapter": 1},
        ]
        pairs = aggregate_spatial_pairs(raw)
        assert len(pairs) == 2

    def test_skips_empty_endpoints(self):
        raw = [
            {"from": "", "to": "德胜府", "relation": "相邻", "chapter": 1},
            {"from": "宁安县", "to": "", "relation": "相邻", "chapter": 1},
            {"from": "宁安县", "to": "大贞", "relation": "从属", "chapter": 1},
        ]
        pairs = aggregate_spatial_pairs(raw)
        assert len(pairs) == 1
        assert pairs[0]["to"] == "大贞"

    def test_sorted_by_chapter_count_desc(self):
        raw = [
            {"from": "A", "to": "B", "relation": "", "chapter": 1},
            {"from": "C", "to": "D", "relation": "", "chapter": 1},
            {"from": "C", "to": "D", "relation": "", "chapter": 2},
            {"from": "C", "to": "D", "relation": "", "chapter": 3},
        ]
        pairs = aggregate_spatial_pairs(raw)
        assert pairs[0]["from"] == "C"


import json

from backend.services.location_normalizer import (
    build_location_prompt,
    parse_and_validate_locations,
)


class TestBuildLocationPrompt:
    def test_returns_system_and_user_messages(self):
        batch = [
            {
                "aliases": ["宁安县", "宁安县城"],
                "types": {"城市": 2},
                "parents": {"大周王朝": 5},
                "chapter_count": 87,
                "sample_desc": "县城描述",
            }
        ]
        messages = build_location_prompt(batch, batch_idx=1, total_batches=5)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "batch 1/5" in messages[1]["content"]
        assert "宁安县" in messages[1]["content"]
        assert "城市:2" in messages[1]["content"]

    def test_system_prompt_includes_hard_constraints(self):
        messages = build_location_prompt([], 1, 1)
        sys = messages[0]["content"]
        assert "canonical_name" in sys
        assert "字面" in sys or "完全匹配" in sys
        assert "JSON" in sys


class TestParseAndValidateLocations:
    def test_accepts_valid_canonical(self):
        batch = [
            {"aliases": ["宁安县", "宁安县城"], "types": {"城市": 2}, "parents": {"大周王朝": 5}, "chapter_count": 87, "sample_desc": ""}
        ]
        llm_text = json.dumps({
            "locations": [
                {"canonical_name": "宁安县", "aliases": ["宁安县", "宁安县城"], "parent": "大周王朝", "type": "县城", "description": "测试"}
            ]
        }, ensure_ascii=False)
        result = parse_and_validate_locations(llm_text, batch)
        assert len(result) == 1
        assert result[0]["canonical_name"] == "宁安县"
        assert "宁安县城" in result[0]["aliases"]

    def test_rejects_hallucinated_canonical(self):
        # LLM 编造了 "京城市" 这个不存在的 alias
        batch = [
            {"aliases": ["宁安县"], "types": {"城市": 1}, "parents": {"大周王朝": 1}, "chapter_count": 5, "sample_desc": ""}
        ]
        llm_text = json.dumps({
            "locations": [
                {"canonical_name": "京城市", "aliases": ["宁安县"], "parent": "", "type": "城市", "description": ""}
            ]
        }, ensure_ascii=False)
        result = parse_and_validate_locations(llm_text, batch)
        assert result == []  # 整条拒收

    def test_handles_empty_locations(self):
        batch = []
        llm_text = json.dumps({"locations": []})
        result = parse_and_validate_locations(llm_text, batch)
        assert result == []

    def test_invalid_json_returns_empty(self):
        result = parse_and_validate_locations("not json {{{", [])
        assert result == []

    def test_keeps_valid_entries_rejects_invalid_in_batch(self):
        batch = [
            {"aliases": ["A"], "types": {"城": 1}, "parents": {"X": 1}, "chapter_count": 1, "sample_desc": ""},
            {"aliases": ["B"], "types": {"城": 1}, "parents": {"X": 1}, "chapter_count": 1, "sample_desc": ""},
        ]
        llm_text = json.dumps({
            "locations": [
                {"canonical_name": "A", "aliases": ["A"], "parent": "X", "type": "城", "description": ""},
                {"canonical_name": "WRONG", "aliases": ["B"], "parent": "X", "type": "城", "description": ""},
            ]
        }, ensure_ascii=False)
        result = parse_and_validate_locations(llm_text, batch)
        assert len(result) == 1
        assert result[0]["canonical_name"] == "A"


from backend.services.location_normalizer import (
    build_spatial_prompt,
    parse_and_validate_spatial,
)


class TestBuildSpatialPrompt:
    def test_returns_system_and_user_messages(self):
        pairs = [{"from": "宁安县", "to": "德胜府", "relations": ["相邻"], "chapters": [1, 2]}]
        whitelist = [{"canonical": "宁安县", "aliases": ["宁安县"]}, {"canonical": "德胜府", "aliases": ["德胜府"]}]
        messages = build_spatial_prompt(pairs, whitelist, batch_idx=1, total_batches=3)
        assert len(messages) == 2
        assert "batch 1/3" in messages[1]["content"]
        assert "白名单" in messages[1]["content"] or "canonical" in messages[1]["content"]
        assert "宁安县" in messages[1]["content"]

    def test_system_prompt_mentions_whitelist_constraint(self):
        messages = build_spatial_prompt([], [], 1, 1)
        sys = messages[0]["content"]
        assert "canonical_name" in sys or "字面" in sys or "引用" in sys


class TestParseAndValidateSpatial:
    def test_accepts_valid_pair(self):
        llm_text = json.dumps({
            "relationships": [
                {"from": "宁安县", "to": "德胜府", "direction": "东南", "distance_text": "约两三百里", "distance_estimate_km": 130, "relation_type": "相邻", "evidence_chapters": [42]}
            ]
        }, ensure_ascii=False)
        result = parse_and_validate_spatial(llm_text, {"宁安县", "德胜府"})
        assert len(result) == 1
        assert result[0]["from"] == "宁安县"

    def test_rejects_from_not_in_whitelist(self):
        llm_text = json.dumps({
            "relationships": [
                {"from": "京城市", "to": "德胜府", "direction": "", "distance_text": "", "distance_estimate_km": None, "relation_type": "", "evidence_chapters": [1]}
            ]
        }, ensure_ascii=False)
        result = parse_and_validate_spatial(llm_text, {"宁安县", "德胜府"})
        assert result == []

    def test_rejects_to_not_in_whitelist(self):
        llm_text = json.dumps({
            "relationships": [
                {"from": "宁安县", "to": "未知地", "direction": "", "distance_text": "", "distance_estimate_km": None, "relation_type": "", "evidence_chapters": [1]}
            ]
        }, ensure_ascii=False)
        result = parse_and_validate_spatial(llm_text, {"宁安县", "德胜府"})
        assert result == []

    def test_rejects_empty_evidence_chapters(self):
        llm_text = json.dumps({
            "relationships": [
                {"from": "宁安县", "to": "德胜府", "direction": "", "distance_text": "", "distance_estimate_km": None, "relation_type": "", "evidence_chapters": []}
            ]
        }, ensure_ascii=False)
        result = parse_and_validate_spatial(llm_text, {"宁安县", "德胜府"})
        assert result == []

    def test_invalid_json_returns_empty(self):
        result = parse_and_validate_spatial("not json", {"宁安县"})
        assert result == []


import asyncio
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.location_normalizer import LLMClient, LocationNormalizer
from backend.core.error_types import LLMResult, UsageStatus


def _setup_output_dir(tmp_path: Path, chapters: int = 5):
    """写 N 个最小 chapter_*.json"""
    (tmp_path / "output").mkdir(exist_ok=True)
    for ch in range(1, chapters + 1):
        data = {
            "chapter_number": ch,
            "core_events": [],
            "character_arcs": [],
            "foreshadowing": [],
            "plot_holes": [],
            "locations": [
                {"name": "宁安县", "parent": "大周王朝", "type": "城市", "description": "测试县城"}
            ] if ch == 1 else [
                {"name": "宁安县城", "parent": "大周王朝", "type": "城池", "description": ""}
            ],
            "spatial_relationships": [
                {"from": "宁安县", "to": "德胜府", "relation": "相邻", "chapter": ch}
            ] if ch <= 3 else [],
        }
        (tmp_path / "output" / f"chapter_{ch}_result.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )


def _make_mock_llm(responses):
    """构造 mock LLM client，按调用顺序返回 LLMResult（U4：不再是四元组）"""
    client = MagicMock()
    client.model = "mock-model"
    client._stop_requested = False
    client.chat = AsyncMock(side_effect=[
        LLMResult(True, r, "", (10, 20), UsageStatus.KNOWN) for r in responses
    ])
    return client


class TestLocationNormalizerRun:
    def test_runs_all_phases_and_writes_outputs(self, tmp_path):
        _setup_output_dir(tmp_path, chapters=3)

        # Phase 0a batch 返回：归一化宁安县 + 宁安县城 → "宁安县"
        # Phase 0a consol (2026-08-23 删除：跨 batch consolidation 不再需要)
        # Phase 0b batch 返回：宁安县→德胜府
        responses = [
            json.dumps({"locations": [
                {"canonical_name": "宁安县", "aliases": ["宁安县", "宁安县城"], "parent": "大周王朝", "type": "城市", "description": "测试"}
            ]}, ensure_ascii=False),
            json.dumps({"relationships": [
                {"from": "宁安县", "to": "宁安县", "direction": "东南", "distance_text": "约两三百里", "distance_estimate_km": 130, "relation_type": "相邻", "evidence_chapters": [1, 2, 3]}
            ]}, ensure_ascii=False),
        ]
        mock_llm = _make_mock_llm(responses)

        norm = LocationNormalizer(
            output_dir=tmp_path,
            llm_client=mock_llm,
            concurrency=2,
        )
        result = asyncio.run(norm.run())
        assert result is True

        # 验证落盘
        assert (tmp_path / "output" / "locations_normalized.json").exists()
        assert (tmp_path / "output" / "spatial_relationships_normalized.json").exists()
        # 验证 spatial 路径真正被走到：白名单仅含 "宁安县"，from/to 都必须命中
        spatial_data = json.loads((tmp_path / "output" / "spatial_relationships_normalized.json").read_text(encoding="utf-8"))
        assert len(spatial_data["relationships"]) == 1
        assert spatial_data["relationships"][0]["from"] == "宁安县"
        assert spatial_data["relationships"][0]["to"] == "宁安县"
        # 验证 _normalized_ref 加到 chapter_*.json
        for ch in range(1, 4):
            data = json.loads((tmp_path / "output" / f"chapter_{ch}_result.json").read_text(encoding="utf-8"))
            assert data.get("_normalized_ref") == "locations_normalized.json"
            assert data.get("_normalized_spatial_ref") == "spatial_relationships_normalized.json"

    def test_returns_false_when_phase_fails(self, tmp_path):
        _setup_output_dir(tmp_path, chapters=3)
        mock_llm = MagicMock()
        mock_llm.model = "mock-model"
        mock_llm._stop_requested = False
        mock_llm.chat = AsyncMock(side_effect=Exception("LLM 故障"))

        norm = LocationNormalizer(output_dir=tmp_path, llm_client=mock_llm, concurrency=1)
        result = asyncio.run(norm.run())
        assert result is False

    def test_skips_when_normalized_files_current(self, tmp_path):
        _setup_output_dir(tmp_path, chapters=3)

        first_desc = "第一次描述"
        first_responses = [
            json.dumps({"locations": [
                {"canonical_name": "宁安县", "aliases": ["宁安县", "宁安县城"], "parent": "大周王朝", "type": "城市", "description": first_desc}
            ]}, ensure_ascii=False),
            json.dumps({"relationships": [
                {"from": "宁安县", "to": "宁安县", "direction": "东南", "distance_text": "约两三百里", "distance_estimate_km": 130, "relation_type": "FIRST_REL", "evidence_chapters": [1, 2, 3]}
            ]}, ensure_ascii=False),
        ]
        norm1 = LocationNormalizer(
            output_dir=tmp_path, llm_client=_make_mock_llm(first_responses), concurrency=1
        )
        assert asyncio.run(norm1.run()) is True

        loc_path = tmp_path / "output" / "locations_normalized.json"
        persisted_after_first = json.loads(loc_path.read_text(encoding="utf-8"))
        assert persisted_after_first["locations"][0]["description"] == first_desc

        second_desc = "第二次描述"
        second_responses = [
            json.dumps({"locations": [
                {"canonical_name": "宁安县", "aliases": ["宁安县", "宁安县城"], "parent": "大周王朝", "type": "城市", "description": second_desc}
            ]}, ensure_ascii=False),
            json.dumps({"relationships": [
                {"from": "宁安县", "to": "宁安县", "direction": "东南", "distance_text": "约两三百里", "distance_estimate_km": 130, "relation_type": "SECOND_REL", "evidence_chapters": [1, 2, 3]}
            ]}, ensure_ascii=False),
        ]
        norm2 = LocationNormalizer(
            output_dir=tmp_path, llm_client=_make_mock_llm(second_responses), concurrency=1
        )
        assert asyncio.run(norm2.run()) is True

        persisted_after_second = json.loads(loc_path.read_text(encoding="utf-8"))
        assert persisted_after_second["locations"][0]["description"] == first_desc

    def test_reruns_when_chapter_mtime_changes(self, tmp_path):
        _setup_output_dir(tmp_path, chapters=3)

        import os

        first_desc = "第一次描述"
        first_responses = [
            json.dumps({"locations": [
                {"canonical_name": "宁安县", "aliases": ["宁安县", "宁安县城"], "parent": "大周王朝", "type": "城市", "description": first_desc}
            ]}, ensure_ascii=False),
            json.dumps({"relationships": [
                {"from": "宁安县", "to": "宁安县", "direction": "东南", "distance_text": "约两三百里", "distance_estimate_km": 130, "relation_type": "FIRST_REL", "evidence_chapters": [1, 2, 3]}
            ]}, ensure_ascii=False),
        ]
        norm1 = LocationNormalizer(
            output_dir=tmp_path, llm_client=_make_mock_llm(first_responses), concurrency=1
        )
        assert asyncio.run(norm1.run()) is True

        loc_path = tmp_path / "output" / "locations_normalized.json"

        bumped_path = tmp_path / "output" / "chapter_2_result.json"
        future_time = 2_000_000_000
        os.utime(bumped_path, (future_time, future_time))

        second_desc = "第二次描述"
        second_responses = [
            json.dumps({"locations": [
                {"canonical_name": "宁安县", "aliases": ["宁安县", "宁安县城"], "parent": "大周王朝", "type": "城市", "description": second_desc}
            ]}, ensure_ascii=False),
            json.dumps({"relationships": [
                {"from": "宁安县", "to": "宁安县", "direction": "东南", "distance_text": "约两三百里", "distance_estimate_km": 130, "relation_type": "SECOND_REL", "evidence_chapters": [1, 2, 3]}
            ]}, ensure_ascii=False),
        ]
        mock_llm2 = _make_mock_llm(second_responses)
        norm2 = LocationNormalizer(
            output_dir=tmp_path, llm_client=mock_llm2, concurrency=1
        )
        assert asyncio.run(norm2.run()) is True

        assert mock_llm2.chat.call_count > 0, "Expected rerun when chapter mtime changed"
        persisted_after_second = json.loads(loc_path.read_text(encoding="utf-8"))
        assert persisted_after_second["locations"][0]["description"] == second_desc


from backend.services.viz_service import map_data


class TestMapDataNormalized:
    def test_returns_normalized_data_when_ref_exists(self, tmp_path):
        (tmp_path / "output").mkdir()
        chapter = {
            "_normalized_ref": "locations_normalized.json",
            "_normalized_spatial_ref": "spatial_relationships_normalized.json",
            "locations": [],
            "spatial_relationships": [],
        }
        (tmp_path / "output" / "chapter_1_result.json").write_text(
            json.dumps(chapter, ensure_ascii=False), encoding="utf-8"
        )
        normalized = {
            "schema_version": 1,
            "locations": [
                {
                    "canonical_name": "宁安县",
                    "aliases": ["宁安县", "宁安县城"],
                    "parent": "大周王朝",
                    "type": "城市",
                    "description": "测试",
                    "chapter_count": 5,
                }
            ],
        }
        (tmp_path / "output" / "locations_normalized.json").write_text(
            json.dumps(normalized, ensure_ascii=False), encoding="utf-8"
        )
        spatial = {
            "schema_version": 1,
            "relationships": [
                {
                    "from": "宁安县",
                    "to": "德胜府",
                    "direction": "东南",
                    "distance_text": "约两三百里",
                    "distance_estimate_km": 130,
                    "relation_type": "相邻",
                    "evidence_chapters": [1, 2],
                }
            ],
        }
        (tmp_path / "output" / "spatial_relationships_normalized.json").write_text(
            json.dumps(spatial, ensure_ascii=False), encoding="utf-8"
        )

        result = map_data(tmp_path)
        assert len(result["locations"]) == 1
        assert result["locations"][0]["name"] == "宁安县"
        assert "宁安县城" in result["locations"][0]["aliases"]
        assert len(result["relationships"]) == 1
        assert result["relationships"][0]["direction"] == "东南"
        assert result["needs_normalization"] is False

    def test_map_data_requires_normalized_files(self, tmp_path):
        (tmp_path / "output").mkdir()
        chapter = {
            "chapter_number": 1,
            "locations": [{"name": "宁安县", "parent": "", "type": "", "description": ""}],
            "spatial_relationships": [],
        }
        (tmp_path / "output" / "chapter_1_result.json").write_text(
            json.dumps(chapter, ensure_ascii=False), encoding="utf-8"
        )

        result = map_data(tmp_path)
        assert result["needs_normalization"] is True
        assert result["locations"] == []
        assert result["relationships"] == []
class TestBudgetBatchSplit:
    """2026-08-21 修复：按字符预算切分 batch（防 Phase 0a 超时）"""

    def test_estimate_group_chars_includes_all_fields(self):
        g = {
            "aliases": ["大唐公司", "新罗酒店"],
            "types": {"建筑": 5, "企业": 1},
            "parents": {"济州岛": 20},
            "chapter_count": 40,
            "sample_desc": "测试描述",
        }
        chars = _estimate_group_chars(g)
        # 应包含 aliases(20+8) + types(20+30) + parents(20+15) + 80 固定 = ~193
        assert 150 < chars < 300

    def test_split_batches_respects_char_budget(self):
        # 每个 group 约 130 chars；预算 300 chars 强制每批 ≤ 3 个
        groups = [
            {"aliases": ["x" * 30], "types": {"y": 1}, "parents": {"z": 1}, "chapter_count": 1, "sample_desc": ""}
            for _ in range(20)
        ]
        batches = _split_into_batches_by_budget(groups, budget_chars=400, max_groups=1000)
        # 验证每批累计 chars ≤ 预算（容许最后一组 < budget）
        for b in batches[:-1]:
            chars = sum(_estimate_group_chars(g) for g in b)
            assert chars <= 400, f"batch over budget: {chars} > 400"
        # 至少 2 批（证明不是 1 batch 全塞）
        assert len(batches) >= 2

    def test_split_batches_single_batch_when_under_budget(self):
        groups = [
            {"aliases": ["短"], "types": {}, "parents": {}, "chapter_count": 1, "sample_desc": ""}
            for _ in range(10)
        ]
        batches = _split_into_batches_by_budget(groups, budget_chars=10000, max_groups=1000)
        assert len(batches) == 1
        assert len(batches[0]) == 10

    def test_split_batches_respects_group_cap(self):
        """max_groups 是硬上限（防御性）：即使字符预算允许也强制切"""
        # 每个 group 1 char；budget 1M；max_groups=5 → 必须 5 个一组
        groups = [
            {"aliases": ["a"], "types": {}, "parents": {}, "chapter_count": 1, "sample_desc": ""}
            for _ in range(20)
        ]
        batches = _split_into_batches_by_budget(groups, budget_chars=1_000_000, max_groups=5)
        assert all(len(b) <= 5 for b in batches)
        assert len(batches) == 4  # 20 / 5 = 4

    def test_constants_have_expected_defaults(self):
        """防御：预算值与 max_groups 默认值与 spec 一致"""
        assert _LOCATION_PROMPT_BUDGET_CHARS == 18000
        assert _LOCATION_BATCH_MAX_GROUPS == 1000


class TestOnProgressCallback:
    """on_progress callback + 部分失败容忍（2026-08-22 Task 1）"""

    async def test_on_progress_called_per_batch(self, tmp_path):
        """每个 batch 完成时触发 on_progress，含正确 payload"""
        _setup_output_dir(tmp_path, chapters=3)
        progress_calls = []
        def on_progress(payload):
            progress_calls.append(payload)

        responses = [
            json.dumps({"locations": [
                {"canonical_name": "宁安县", "aliases": ["宁安县", "宁安县城"], "parent": "大周王朝", "type": "城市", "description": "测试"}
            ]}, ensure_ascii=False),
            json.dumps({"relationships": [
                {"from": "宁安县", "to": "宁安县", "direction": "东南", "distance_text": "约两三百里", "distance_estimate_km": 130, "relation_type": "相邻", "evidence_chapters": [1, 2, 3]}
            ]}, ensure_ascii=False),
        ]
        mock_llm = _make_mock_llm(responses)

        norm = LocationNormalizer(
            output_dir=tmp_path, llm_client=mock_llm, concurrency=1,
            on_progress=on_progress,
        )
        result = await norm.run()
        assert result is True

        phase_0a_calls = [c for c in progress_calls if c.get("type") == "batch_done" and c.get("phase") == "0a-batches"]
        assert len(phase_0a_calls) >= 1
        assert "batch_idx" in phase_0a_calls[0]
        assert "total_batches" in phase_0a_calls[0]
        assert phase_0a_calls[0]["type"] == "batch_done"

    async def test_partial_batch_failure_tolerated(self, tmp_path):
        """50% 以下 batch 失败时，其他 batch 结果被保留"""
        call_count = 0
        async def mock_chat(messages, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return LLMResult(False, "", "模拟 LLM 错误")
            return LLMResult(True, '{"locations":[{"canonical_name":"A","aliases":["A"]}]}',
                             "", (100, 50), UsageStatus.KNOWN)

        mock_llm = MagicMock()
        mock_llm.model = "mock-model"
        mock_llm._stop_requested = False
        mock_llm.chat = mock_chat

        norm = LocationNormalizer(output_dir=tmp_path, llm_client=mock_llm, concurrency=1)

        groups = [
            {"aliases": [f"地点{i}" * 5], "types": {}, "parents": {}, "chapter_count": 1, "sample_desc": ""}
            for i in range(2000)
        ]
        result = await norm._run_phase_0a_batches(groups)
        assert result is not None
        assert len(result) > 0


async def test_stop_propagates_to_llm_client(tmp_path):
    """stop() 应调用 llm_client.request_stop()"""
    with patch("backend.services.location_normalizer.LLMClient") as MockLLM:
        mock_client = MockLLM.return_value
        mock_client.config.model = "test-model"
        mock_client.request_stop = MagicMock()
        norm = LocationNormalizer(output_dir=tmp_path, llm_client=mock_client)
        norm.stop()
        mock_client.request_stop.assert_called_once()


async def test_typed_payload_drives_service_state(tmp_path):
    """typed batch_done 事件必须能驱动 service 的 _batches_done / _total_batches / _phase
    （2026-08-22 Critical #1 集成测试）"""
    _setup_output_dir(tmp_path, chapters=3)

    state = {"phase": "starting", "total_batches": 0, "batches_done": 0}

    def on_progress(payload: dict) -> None:
        ptype = payload.get("type", "")
        if ptype == "phase":
            state["phase"] = payload.get("phase", state["phase"])
            state["total_batches"] = payload.get("total_batches", state["total_batches"])
        elif ptype == "batch_done":
            state["batches_done"] += 1
            state["total_batches"] = payload.get("total_batches", state["total_batches"])
            state["phase"] = payload.get("phase", state["phase"])

    responses = [
        json.dumps({"locations": [
            {"canonical_name": "宁安县", "aliases": ["宁安县", "宁安县城"], "parent": "大周王朝", "type": "城市", "description": "测试"}
        ]}, ensure_ascii=False),
        json.dumps({"relationships": [
            {"from": "宁安县", "to": "宁安县", "direction": "东南", "distance_text": "约两三百里", "distance_estimate_km": 130, "relation_type": "相邻", "evidence_chapters": [1, 2, 3]}
        ]}, ensure_ascii=False),
    ]
    mock_llm = _make_mock_llm(responses)

    norm = LocationNormalizer(
        output_dir=tmp_path, llm_client=mock_llm, concurrency=1,
        on_progress=on_progress,
    )
    result = await norm.run()
    assert result is True

    assert state["batches_done"] >= 2, f"batches_done 没递增（仍为 {state['batches_done']}）— typed batch_done 未被识别"
    assert state["total_batches"] > 0, f"total_batches 没被设置（仍为 {state['total_batches']}）— typed batch_done 未携带 total_batches"
    assert state["phase"] in ("0a-batches", "0b-batches"), f"phase 未被 typed batch_done 更新（仍为 {state['phase']}）"


async def test_run_returns_none_when_stop_requested_before_llm(tmp_path):
    """run() 在 _stop_requested=True 且无 LLM 调用时应返回 None（用户停止）
    （2026-08-22 Critical #2）"""
    _setup_output_dir(tmp_path, chapters=3)

    mock_llm = MagicMock()
    mock_llm.model = "mock-model"
    mock_llm._stop_requested = True
    mock_llm.request_stop = MagicMock()
    mock_llm.chat = AsyncMock()

    norm = LocationNormalizer(output_dir=tmp_path, llm_client=mock_llm, concurrency=1)
    result = await norm.run()

    assert result is None, f"应返回 None（用户停止），实际 {result!r}"
    mock_llm.chat.assert_not_called()
