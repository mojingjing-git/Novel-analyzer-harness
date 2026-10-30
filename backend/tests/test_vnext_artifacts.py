"""vnext 产物门禁与原子写（PLAN v2 §4 S2-1 / S2-5，§1.1-4 拒绝路径）。"""

from __future__ import annotations

import json

import pytest

from backend.vnext import artifacts, constants
from backend.vnext.constants import SCHEMA_VERSION, VnextFormatError


# ---------------------------------------------------------------------------
# 格式门禁
# ---------------------------------------------------------------------------


def test_legacy_payload_without_marker_is_rejected() -> None:
    """旧产物（无 format_id / schema_version）必须报"不属于本版本"。"""
    with pytest.raises(VnextFormatError) as exc:
        artifacts.assert_vnext_payload({"chapter_number": 1, "core_events": []})
    assert exc.value.code is constants.ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT
    assert "旧版本产物" in exc.value.detail


def test_wrong_format_family_is_rejected() -> None:
    """只有版本号、格式族不对，也算走错门（不能靠版本号蒙混）。"""
    with pytest.raises(VnextFormatError) as exc:
        artifacts.assert_vnext_payload({"format_id": "novel-analyzer", "schema_version": 1})
    assert exc.value.code is constants.ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT


def test_future_schema_version_is_rejected_distinctly() -> None:
    """版本过高要走 UNSUPPORTED_SCHEMA_VERSION，而不是笼统的"格式不支持"。

    两者对用户的意义不同：前者是"你升级软件"，后者是"这不是本版本的数据"。
    """
    with pytest.raises(VnextFormatError) as exc:
        artifacts.assert_vnext_payload(
            {"format_id": constants.FORMAT_ID, "schema_version": SCHEMA_VERSION + 1}
        )
    assert exc.value.code is constants.ErrorCode.UNSUPPORTED_SCHEMA_VERSION


def test_non_object_payload_is_rejected() -> None:
    """顶层是数组/字符串同样属于"不属于本版本"。"""
    with pytest.raises(VnextFormatError) as exc:
        artifacts.assert_vnext_payload([{"format_id": constants.FORMAT_ID}])
    assert exc.value.code is constants.ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT


def test_valid_payload_passes_through_unchanged() -> None:
    payload = {"format_id": constants.FORMAT_ID, "schema_version": SCHEMA_VERSION, "x": 1}
    assert artifacts.assert_vnext_payload(payload) is payload


def test_corrupt_json_is_its_own_error_code(tmp_path) -> None:
    """写坏的 JSON 与"格式不支持"要分开：前者可以重写，后者是走错了门。"""
    path = tmp_path / "broken.json"
    path.write_text('{"format_id": ', encoding="utf-8")
    with pytest.raises(VnextFormatError) as exc:
        artifacts.read_json_artifact(path)
    assert exc.value.code is constants.ErrorCode.UNPARSEABLE_ARTIFACT


def test_stamp_payload_does_not_mutate_input() -> None:
    src = {"x": 1}
    out = artifacts.stamp_payload(src)
    assert "format_id" not in src
    assert out["format_id"] == constants.FORMAT_ID
    assert out["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------


def test_write_then_read_roundtrip(tmp_path) -> None:
    path = tmp_path / "deep" / "manifest.json"  # 父目录不存在也要能写
    artifacts.write_json_atomic(path, {"a": "中文", "b": [1, 2]})
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"a": "中文", "b": [1, 2]}


def test_write_leaves_no_temp_files(tmp_path) -> None:
    """临时文件必须收干净——残留的 .tmp 会被目录扫描当成产物。"""
    path = tmp_path / "m.json"
    artifacts.write_json_atomic(path, {"a": 1})
    artifacts.write_json_atomic(path, {"a": 2})  # 覆盖写
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "m.json"]
    assert leftovers == []
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}


def test_write_uses_lf_newlines(tmp_path) -> None:
    """行尾固定 LF：CRLF 会让同一份数据在 git 里反复翻整行。"""
    path = tmp_path / "m.json"
    artifacts.write_json_atomic(path, {"a": 1})
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


def test_failed_write_leaves_original_intact(tmp_path, monkeypatch) -> None:
    """写失败时旧文件不能被破坏（否则一次异常就丢掉整本 manifest）。"""
    path = tmp_path / "m.json"
    artifacts.write_json_atomic(path, {"ok": True})

    def boom(*_args, **_kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(artifacts.os, "replace", boom)
    with pytest.raises(OSError):
        artifacts.write_json_atomic(path, {"ok": False})

    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert [p.name for p in tmp_path.iterdir()] == ["m.json"]
