"""外部复审（2026-10-30）六条发现的回归用例。

每条都对应一处"看起来能跑、实际给出错误结论"的地方：

1. 结果协议与契约不符（`<result_id>.json`、`SUCCEEDED`、必须登记）—— 见
   `test_vnext_results_queue.py`；
2. 队列恢复不重算进度 → 源变了仍显示"已跑完"；
3. 书目定位被一个坏目录堵死；同一 `book_id` 出现在两个目录时任取其一；
4. `block_size` 配置非法时被静默转换（`true`→1、`2.9`→2）或抛未封装异常。
"""

from __future__ import annotations

import json

import pytest

from backend.services import vnext_service
from backend.vnext import blocks, project, queue, registry, results
from backend.vnext.constants import VnextFormatError


def _env_root(tmp_path, monkeypatch):
    root = tmp_path / "novelroot"
    root.mkdir()
    monkeypatch.setenv("NOVEL_ROOT", str(root))
    return root


def _make_book(root, name: str, n: int = 4, *, book_id: str, dir_name: str | None = None):
    chapters = [(i, f"第{i}章\n正文{i}\n") for i in range(1, n + 1)]
    return project.create_project(
        root / "workspace_vnext", name, chapters, book_id=book_id, dir_name=dir_name
    )


# ---------------------------------------------------------------------------
# 3a. 同一 book_id 出现在两个目录
# ---------------------------------------------------------------------------


def test_duplicate_book_id_is_rejected_not_silently_picked(tmp_path) -> None:
    data_root = tmp_path / "workspace_vnext"
    _make_book(tmp_path, "甲书", book_id="bk_dup0000001", dir_name="a")
    _make_book(tmp_path, "乙书", book_id="bk_dup0000001", dir_name="b")

    scan = registry.scan_projects(data_root)
    assert len(scan.accepted) == 1
    dup = [r for r in scan.rejected if r.reason == "DUPLICATE_BOOK_ID"]
    assert len(dup) == 1
    assert "冲突" in dup[0].detail


def test_duplicate_book_id_still_locatable_by_id(tmp_path) -> None:
    data_root = tmp_path / "workspace_vnext"
    _make_book(tmp_path, "甲书", book_id="bk_dup0000002", dir_name="a")
    _make_book(tmp_path, "乙书", book_id="bk_dup0000002", dir_name="b")

    ref = registry.locate_project(data_root, "bk_dup0000002")
    assert ref.name == "甲书"  # 保留排序靠前的那个，且不抛异常


# ---------------------------------------------------------------------------
# 3b. 坏目录不能堵死好书
# ---------------------------------------------------------------------------


def test_broken_manifest_does_not_block_other_books(tmp_path) -> None:
    data_root = tmp_path / "workspace_vnext"
    # 名字排序在前，且清单是坏的
    broken = data_root / "aaa_broken"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{ not json", encoding="utf-8")

    _make_book(tmp_path, "正常书", book_id="bk_good0000001")

    ref = registry.locate_project(data_root, "bk_good0000001")
    assert ref.name == "正常书"

    scan = registry.scan_projects(data_root)
    assert any(r.reason == "UNPARSEABLE" for r in scan.rejected)


# ---------------------------------------------------------------------------
# 4. block_size 配置校验
# ---------------------------------------------------------------------------


def _write_cfg(root, analysis_value) -> None:
    payload = {
        "format_id": "novel-analyzer-vnext",
        "schema_version": 1,
        "workspace_dir": "workspace_vnext",
        "analysis": analysis_value,
    }
    (root / "config_vnext.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_block_size_rejects_bool_true(tmp_path, monkeypatch) -> None:
    """`true` 会被 int() 顺成 1，等于把"每章一块"悄悄生效。"""
    root = _env_root(tmp_path, monkeypatch)
    _write_cfg(root, {"block_size": True})
    with pytest.raises(VnextFormatError) as exc:
        vnext_service.block_size()
    assert exc.value.code.value == "INVALID_FIELD_VALUE"


def test_block_size_rejects_float(tmp_path, monkeypatch) -> None:
    """`2.9` 截断成 2 是"改了一半"，不是四舍五入，必须报错。"""
    root = _env_root(tmp_path, monkeypatch)
    _write_cfg(root, {"block_size": 2.9})
    with pytest.raises(VnextFormatError):
        vnext_service.block_size()


def test_block_size_rejects_non_object_analysis(tmp_path, monkeypatch) -> None:
    """`analysis: [1]` 曾抛未封装的 AttributeError（接口直接 500）。"""
    root = _env_root(tmp_path, monkeypatch)
    _write_cfg(root, [1])
    with pytest.raises(VnextFormatError) as exc:
        vnext_service.block_size()
    assert exc.value.code.value == "INVALID_FIELD_VALUE"


def test_block_size_accepts_valid_int_and_defaults_only_when_absent(tmp_path, monkeypatch) -> None:
    root = _env_root(tmp_path, monkeypatch)
    assert vnext_service.block_size() == 4  # 文件不存在 → 默认

    _write_cfg(root, {"block_size": 3})
    assert vnext_service.block_size() == 3


# ---------------------------------------------------------------------------
# 2. 队列恢复必须按当前源重算
# ---------------------------------------------------------------------------


def test_restore_recomputes_progress_instead_of_trusting_stored_counters(
    tmp_path, monkeypatch
) -> None:
    """复现复审给的最坏情形：改一章之后，队列仍写着 done 2/2。"""
    root = _env_root(tmp_path, monkeypatch)
    book = _make_book(root, "书", 4, book_id="bk_restore0001")

    manifest = project.load_manifest(book)
    plans = blocks.plan_blocks(manifest, block_size=2)
    for plan in plans:
        hashes = {c.chapter_id: c.content_hash for c in manifest.chapters}
        rid = results.make_result_id(
            block_signature=plan.block_signature,
            prompt_strategy_version=1,
            model_fingerprint="m",
            dependency_fingerprint="d",
        )
        manifest = results.publish_result(
            book,
            results.ResultRecord(
                result_id=rid,
                block_signature=plan.block_signature,
                source_chapters=tuple((cid, hashes[cid]) for cid in plan.chapter_ids),
                status=results.ST_SUCCEEDED,
                dependency_fingerprint="d",
            ),
        )

    # 队列里记着"两块都完成"
    state = queue.QueueState(
        entries=[
            queue.QueueEntry(
                book_id="bk_restore0001", name="书",
                status=queue.STATUS_DONE,
                completed_blocks=2, total_blocks=2, stale_blocks=0,
            )
        ]
    )
    vnext_service.save_queue(state)

    # 改第 3 章（同项目内提交，active_results 保留但块签名全变）
    from dataclasses import replace
    from backend.vnext.fingerprint import content_hash, normalize_chapter_text

    new_text = normalize_chapter_text("第3章\n改过的内容\n")
    (book / "source" / "chapters" / f"{manifest.chapters[2].chapter_id}.txt").write_text(
        new_text, encoding="utf-8"
    )

    def _mutate(m):
        m.chapters[2] = replace(m.chapters[2], content_hash=content_hash(new_text))

    project.commit_manifest(book, _mutate)

    restored = vnext_service.restore_queue_after_restart()
    entry = restored.find("bk_restore0001")
    # 结论必须是"还没跑完"，而不是沿用磁盘上的 2/2
    assert entry.status != queue.STATUS_DONE
    assert entry.completed_blocks < entry.total_blocks


def test_restore_marks_unavailable_project_with_reason(tmp_path, monkeypatch) -> None:
    """项目被删/被换成旧目录时，保留历史状态但要写清原因。"""
    root = _env_root(tmp_path, monkeypatch)
    state = queue.QueueState(
        entries=[
            queue.QueueEntry(
                book_id="bk_gone0000001", name="没了", status=queue.STATUS_DONE,
                completed_blocks=2, total_blocks=2,
            )
        ]
    )
    vnext_service.save_queue(state)

    restored = vnext_service.restore_queue_after_restart()
    entry = restored.find("bk_gone0000001")
    assert entry.status == queue.STATUS_DONE      # 历史结论不改写
    assert "不可用" in entry.error_message        # 但要说明"这不是当前进度"
