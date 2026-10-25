"""Step4-4A: character-name suffix single-source regression.

R4 review scope: unify the duplicated character-name suffix list between
`aggregate_utils._normalize_char_name` and `viz_service._aggregate_chars_edges`
into one source of truth, WITHOUT changing either path's behavior.

Preserved differences (recorded, deliberately not unified):

- strip timing: aggregate strips first, viz strips only at return
- pure-suffix input: aggregate returns the original name, viz returns ""
- only one suffix layer is stripped on both sides (consecutive annotations stay)
- Chinese and English parentheses, case sensitivity, legal bracket names and
  unrecognized suffixes behave exactly as before

Tests drive the REAL production entry points:

- `JSONAggregator.aggregate_character_tracking` on a real chapter directory
- `viz_service.graph_data` / `_aggregate_chars_edges` on the same fixtures

so character merge results/counts, node IDs and edge weights are compared, not
just the helper or the source wiring.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.models.analysis_result import AnalysisResult  # noqa: E402
from backend.services import viz_service  # noqa: E402
from backend.utils.aggregate_utils import (  # noqa: E402
    CHAR_NAME_SUFFIXES,
    JSONAggregator,
    _normalize_char_name,
)


# ---------------------------------------------------------------------------
# Fixtures: real chapter JSON files on disk, loaded by the real aggregator
# ---------------------------------------------------------------------------

def _chapter_payload(chapter_number: int, events, arcs=None):
    return {
        "chapter_number": chapter_number,
        "cross_block": {
            "summary": f"summary-{chapter_number}",
            "unresolved_questions": [],
            "new_leads": [],
        },
        "core_events": [
            {"event": e[0], "characters": e[1]} for e in events
        ],
        "character_arcs": arcs or [],
        "foreshadowing": [],
        "plot_holes": [],
        "locations": [],
        "spatial_relationships": [],
        "long_context_insights": {},
        "updated_knowledge": {},
    }


def _write_chapters(base: Path, payloads):
    base.mkdir(parents=True, exist_ok=True)
    for i, payload in enumerate(payloads, 1):
        p = base / f"chapter_{i}_result.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def char_dir(tmp_path):
    """A real chapter directory with suffix variants of the same character."""
    payloads = [
        _chapter_payload(1, [
            ("\u5f20\u4e09\u51fa\u573a", "\u5f20\u4e09\uff08\u4e3b\u89d2\uff09"),
            ("\u674e\u56db\u51fa\u573a", "\u674e\u56db"),
            ("\u738b\u4e94\u51fa\u573a", "\u738b\u4e94(\u526f\u672c)"),
        ]),
        _chapter_payload(2, [
            ("\u5f20\u4e09\u518d\u51fa\u573a", "\u5f20\u4e09"),
            ("\u674e\u56db\u518d\u51fa\u573a", "\u674e\u56db(\u5df2\u6545)"),
            ("\u8d75\u516d\u51fa\u573a", "\u8d75\u516d"),
        ]),
    ]
    _write_chapters(tmp_path / "output" / "blocks", payloads)
    return tmp_path / "output" / "blocks"


# ---------------------------------------------------------------------------
# 1) The suffix list is a single source of truth
# ---------------------------------------------------------------------------

def test_char_name_suffixes_is_the_single_source():
    """The list lives in aggregate_utils and viz imports it, not a copy."""
    import inspect

    from backend.services import viz_service as vs

    assert vs.CHAR_NAME_SUFFIXES is CHAR_NAME_SUFFIXES, (
        "viz_service must import the same list object, not a copy"
    )

    source = inspect.getsource(vs._aggregate_chars_edges)
    # the literal suffix list must no longer be spelled out inside viz_service
    assert "(\u4e3b\u89d2)" not in source, (
        "viz_service still contains a duplicated suffix literal"
    )
    assert "CHAR_NAME_SUFFIXES" in source


def test_suffix_list_content_and_order_unchanged():
    """The list itself is byte-for-byte the historical one."""
    assert CHAR_NAME_SUFFIXES == (
        '\uff08\u4e3b\u89d2\uff09', '\uff08\u914d\u89d2\uff09', '\uff08\u5df2\u6545\uff09',
        '\uff08\u91cd\u8981\uff09', '\uff08\u6b21\u8981\uff09',
        '\uff08\u5973\u4e3b\uff09', '\uff08\u7537\u4e3b\uff09', '\uff08\u53cd\u6d3e\uff09',
        '\uff08\u9f99\u5957\uff09',
        '(\u4e3b\u89d2)', '(\u914d\u89d2)', '(\u5df2\u6545)', '(\u91cd\u8981)',
        '(\u6b21\u8981)',
        '(\u5973\u4e3b)', '(\u7537\u4e3b)', '(\u53cd\u6d3e)', '(\u9f99\u5957)',
    )


# ---------------------------------------------------------------------------
# 2) Preserved behavioral differences (documented, not unified)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        # Chinese and English parentheses
        ("\u5f20\u4e09\uff08\u4e3b\u89d2\uff09", "\u5f20\u4e09"),
        ("\u5f20\u4e09(\u4e3b\u89d2)", "\u5f20\u4e09"),
        # case is preserved
        ("ZHANGSAN(\u4e3b\u89d2)", "ZHANGSAN"),
        # only one layer is stripped; a consecutive unknown annotation stays
        ("\u5f20\u4e09\uff08\u4e3b\u89d2\uff09\uff08\u9644\u6ce8\uff09",
         "\u5f20\u4e09\uff08\u4e3b\u89d2\uff09\uff08\u9644\u6ce8\uff09"),
        ("\u5f20\u4e09\uff08\u4e3b\u89d2\uff09\uff08\u5df2\u6545\uff09",
         "\u5f20\u4e09\uff08\u4e3b\u89d2\uff09"),
        # two identical suffixes: only one is stripped
        ("\u5f20\u4e09\uff08\u4e3b\u89d2\uff09\uff08\u4e3b\u89d2\uff09",
         "\u5f20\u4e09\uff08\u4e3b\u89d2\uff09"),
        # unrecognized suffix stays
        ("\u5f20\u4e09\uff08\u526f\u672c\uff09", "\u5f20\u4e09\uff08\u526f\u672c\uff09"),
        # legal bracket name without a known suffix is untouched
        ("\u5f20\u4e09\uff08A\uff09", "\u5f20\u4e09\uff08A\uff09"),
        # no parentheses at all
        ("\u5f20\u4e09", "\u5f20\u4e09"),
        # suffix at the start, not the end
        ("\uff08\u4e3b\u89d2\uff09\u5f20\u4e09", "\uff08\u4e3b\u89d2\uff09\u5f20\u4e09"),
        # fullwidth punctuation that is not a suffix
        ("\u5f20\u4e09\u30088\u3009\u4e3b\u89d2", "\u5f20\u4e09\u30088\u3009\u4e3b\u89d2"),
    ],
)
def test_normalize_preserves_documented_behavior(raw, expected):
    assert _normalize_char_name(raw) == expected


def test_aggregate_strips_first_and_keeps_pure_suffix_input():
    """aggregate: strips first; a pure-suffix input keeps the original name."""
    assert _normalize_char_name("  \u5f20\u4e09\uff08\u4e3b\u89d2\uff09  ") == "\u5f20\u4e09"
    assert _normalize_char_name("\u5f20\u4e09\uff08\u4e3b\u89d2\uff09 ") == "\u5f20\u4e09"
    assert _normalize_char_name("\u5f20\u4e09\uff08\u4e3b\u89d2\uff09\t") == "\u5f20\u4e09"
    assert _normalize_char_name("\u5f20\u4e09\uff08\u4e3b\u89d2\uff09\n") == "\u5f20\u4e09"
    # pure suffix: base is empty so the original is returned
    assert _normalize_char_name("\uff08\u4e3b\u89d2\uff09") == "\uff08\u4e3b\u89d2\uff09"
    assert _normalize_char_name("(\u4e3b\u89d2)") == "(\u4e3b\u89d2)"


def test_viz_keeps_its_own_strip_timing_and_pure_suffix_behavior():
    """viz: strips only at return, so pure-suffix input becomes "".

    This is the documented difference that must NOT be unified away.
    """
    nodes, _edges = viz_service._aggregate_chars_edges([
        AnalysisResult.from_dict(_chapter_payload(1, [
            ("e1", "  \u5f20\u4e09\uff08\u4e3b\u89d2\uff09  "),
        ])),
    ])
    # leading whitespace blocks endswith(), so the name is only stripped
    assert list(nodes) == ["\u5f20\u4e09\uff08\u4e3b\u89d2\uff09"]

    nodes2, _e2 = viz_service._aggregate_chars_edges([
        AnalysisResult.from_dict(_chapter_payload(1, [("e1", "\u5f20\u4e09\uff08\u4e3b\u89d2\uff09 ")])),
    ])
    assert list(nodes2) == ["\u5f20\u4e09\uff08\u4e3b\u89d2\uff09"]

    nodes3, _e3 = viz_service._aggregate_chars_edges([
        AnalysisResult.from_dict(_chapter_payload(1, [("e1", "\uff08\u4e3b\u89d2\uff09")])),
    ])
    assert list(nodes3) == [""]


# ---------------------------------------------------------------------------
# 3) Real production entry: character aggregation merge + counts
# ---------------------------------------------------------------------------

def test_aggregate_character_tracking_merges_suffix_variants(char_dir):
    """Real aggregation: variants merge into one key with the short display name."""
    agg = JSONAggregator(char_dir)
    tracking = agg.aggregate_character_tracking()

    assert set(tracking) == {
        "\u5f20\u4e09", "\u674e\u56db", "\u738b\u4e94(\u526f\u672c)", "\u8d75\u516d",
    }, f"unexpected merge result: {sorted(tracking)}"

    zhang = tracking["\u5f20\u4e09"]
    # the shortest surface form wins as the display name
    assert zhang["name"] == "\u5f20\u4e09"
    assert zhang["chapters"] == [1, 2]
    assert zhang["total_events"] == 2

    li = tracking["\u674e\u56db"]
    assert li["name"] == "\u674e\u56db"
    assert li["chapters"] == [1, 2]
    assert li["total_events"] == 2

    # \u738b\u4e94(\u526f\u672c) is an unrecognized suffix, so it stays separate
    assert tracking["\u738b\u4e94(\u526f\u672c)"]["name"] == "\u738b\u4e94(\u526f\u672c)"
    assert tracking["\u738b\u4e94(\u526f\u672c)"]["total_events"] == 1

    assert tracking["\u8d75\u516d"]["total_events"] == 1


def test_aggregate_character_count_is_stable(char_dir):
    """The merged character count must not drift after the extraction."""
    agg = JSONAggregator(char_dir)
    tracking = agg.aggregate_character_tracking()
    assert len(tracking) == 4


# ---------------------------------------------------------------------------
# 4) Real production entry: viz graph nodes / IDs / edge weights
# ---------------------------------------------------------------------------

def test_graph_data_nodes_and_edges(char_dir):
    """Real graph_data: node IDs, node count and edge weights on the fixtures."""
    data = viz_service.graph_data(char_dir)

    nodes = {n["id"] for n in data["nodes"]}
    # aggregate-side merge applies: \u5f20\u4e09\uff08\u4e3b\u89d2\uff09 and \u5f20\u4e09 collapse
    assert "\u5f20\u4e09" in nodes
    assert "\u5f20\u4e09\uff08\u4e3b\u89d2\uff09" not in nodes
    assert "\u674e\u56db" in nodes
    # \u738b\u4e92(\u526f\u672c) is unrecognized, so it survives as its own node
    assert "\u738b\u4e94(\u526f\u672c)" in nodes
    assert "\u8d75\u516d" in nodes

    # total counts before filtering
    assert data["total_characters"] == 4
    assert data["total_edges"] == 5

    # edge weights: each pair co-occurs once per chapter
    weights = {(e["source"], e["target"]): e["weight"] for e in data["edges"]}
    assert weights[("\u5f20\u4e09", "\u674e\u56db")] == 2
    assert weights[("\u5f20\u4e09", "\u738b\u4e94(\u526f\u672c)")] == 1
    assert weights[("\u674e\u56db", "\u738b\u4e94(\u526f\u672c)")] == 1

    # node event counts reflect the merged view
    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id["\u5f20\u4e09"]["event_count"] == 2
    assert by_id["\u674e\u56db"]["event_count"] == 2
    assert by_id["\u738b\u4e94(\u526f\u672c)"]["event_count"] == 1
    assert by_id["\u8d75\u516d"]["event_count"] == 1


def test_graph_data_min_edge_weight_filter(char_dir):
    """Edge filtering still behaves the same on the merged graph."""
    data = viz_service.graph_data(char_dir, min_edge_weight=2)
    assert {(e["source"], e["target"]) for e in data["edges"]} == {
        ("\u5f20\u4e09", "\u674e\u56db"),
    }


def test_graph_data_chapter_range(char_dir):
    """Chapter-range slicing still works on the merged graph."""
    data = viz_service.graph_data(char_dir, chapter_start=2, chapter_end=2)
    assert {n["id"] for n in data["nodes"]} == {
        "\u5f20\u4e09", "\u674e\u56db", "\u8d75\u516d",
    }
    weights = {(e["source"], e["target"]): e["weight"] for e in data["edges"]}
    assert all(w == 1 for w in weights.values())
