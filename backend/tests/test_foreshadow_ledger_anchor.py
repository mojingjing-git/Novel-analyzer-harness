"""伏笔账本 v4：锚点与稳定实体键。

守两件事：
1. 旧账本（v3）能读——字段缺失补默认值，不报错、不迁移；
2. 锚点与 `entity_key` 能落盘再读回——它们是复检唯一的依据，不能只在内存里活着。
"""

from __future__ import annotations

import json
from dataclasses import asdict

from backend.core.anchor import entity_key_for
from backend.utils.foreshadow_ledger import (
    SCHEMA_VERSION,
    ForeshadowItem,
    ForeshadowLedger,
)


def _item(**kw) -> ForeshadowItem:
    base = dict(
        id="fs_001",
        description="铜匣里的旧契",
        first_seen_chapter=3,
        first_seen_batch=1,
        last_seen_chapter=9,
        last_seen_batch=3,
    )
    base.update(kw)
    return ForeshadowItem(**base)


def test_schema_version_bumped_to_4():
    assert SCHEMA_VERSION == 4


def test_item_carries_anchor_and_entity_key():
    item = _item(anchor_quote="铜匣打开，里面只有一张旧契。", anchor_chapter=3, entity_key="铜匣")
    restored = ForeshadowItem.from_dict(asdict(item))
    assert restored.anchor_quote == "铜匣打开，里面只有一张旧契。"
    assert restored.anchor_chapter == 3
    assert restored.entity_key == "铜匣"


def test_old_v3_payload_loads_with_empty_anchor():
    payload = {
        "id": "fs_009",
        "description": "旧账本没有锚点",
        "first_seen_chapter": 2,
        "first_seen_batch": 1,
        "last_seen_chapter": 5,
        "last_seen_batch": 2,
        "status": "dormant",
        "entity_key": "",
    }
    item = ForeshadowItem.from_dict(payload)
    assert item.anchor_quote == ""
    assert item.anchor_chapter == 0
    assert item.status == "dormant"


def test_from_dict_still_filters_unknown_keys():
    item = ForeshadowItem.from_dict(
        {**asdict(_item()), "legacy_text": "旧字段", "something_new": 1}
    )
    assert not hasattr(item, "legacy_text")
    assert not hasattr(item, "something_new")


def test_ledger_roundtrip_keeps_anchor(tmp_path):
    ledger = ForeshadowLedger()
    ledger.total_chapters = 40
    ledger.items = [
        _item(anchor_quote="铜匣打开，里面只有一张旧契。", anchor_chapter=3, entity_key="铜匣"),
        _item(id="fs_002", description="院墙外的脚印", anchor_quote="", entity_key="clue:fs_002"),
    ]
    path = tmp_path / "foreshadow_ledger.json"
    ledger.save(path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SCHEMA_VERSION

    loaded = ForeshadowLedger.load(path)
    assert loaded.items[0].anchor_quote == "铜匣打开，里面只有一张旧契。"
    assert loaded.items[0].anchor_chapter == 3
    assert loaded.items[0].entity_key == "铜匣"
    assert loaded.items[1].entity_key == "clue:fs_002"


def test_entity_key_prefers_motif_and_falls_back_to_id():
    assert entity_key_for("铜匣", "fs_001") == "铜匣"
    assert entity_key_for("  铜匣  ", "fs_001") == "铜匣"
    assert entity_key_for("", "fs_001") == "clue:fs_001"


def test_entity_key_is_stable_when_earlier_clue_arrives():
    # 补进更早的线索不应改变已登记实体的键——键变了历史就被拆成两条
    first = entity_key_for("铜匣", "fs_007")
    later_with_earlier_evidence = entity_key_for("铜匣", "fs_001")
    assert first == later_with_earlier_evidence


def test_ledger_load_tolerates_missing_anchor_fields(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "total_chapters": 40,
                "items": [
                    {
                        "id": "fs_001",
                        "description": "v3 的条目",
                        "first_seen_chapter": 1,
                        "first_seen_batch": 0,
                        "last_seen_chapter": 2,
                        "last_seen_batch": 0,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = ForeshadowLedger.load(path)
    assert loaded.items[0].anchor_quote == ""
    assert loaded.items[0].anchor_chapter == 0
