"""vnext 结果落盘与队列恢复（PLAN v2 §4 S2-4/6：进度由块签名重算，不数文件）。"""

from __future__ import annotations

import json

import pytest

from backend.vnext import blocks, project, queue, results
from backend.vnext.constants import VnextFormatError


def _book(tmp_path, n: int = 4, *, book_id: str = "bk_qu000000001", root=None):
    chapters = [(i, f"第{i}章\n正文{i}\n") for i in range(1, n + 1)]
    return project.create_project((root or tmp_path), "书", chapters, book_id=book_id)


# ---------------------------------------------------------------------------
# 结果读写
# ---------------------------------------------------------------------------


def test_result_roundtrip_keeps_unknown_usage_unknown(tmp_path) -> None:
    book = _book(tmp_path)
    results.write_result(
        book,
        results.ResultRecord(
            block_signature="sig1",
            status=results.STATUS_DONE,
            usage_status="UNKNOWN",
            prompt_tokens=None,
            completion_tokens=None,
        ),
    )
    record = results.read_result(book, "sig1")
    assert record is not None
    # 关键：未知必须保持 None，不能落成 0（否则"不知道"变成"花了 0 元"）
    assert record.prompt_tokens is None
    assert record.usage_status == "UNKNOWN"


def test_result_rejects_unknown_status(tmp_path) -> None:
    book = _book(tmp_path)
    with pytest.raises(VnextFormatError):
        results.write_result(book, results.ResultRecord(block_signature="s", status="halfway"))


def test_list_results_skips_corrupt_file(tmp_path) -> None:
    """单个坏文件不该让整本书无法读取进度。"""
    book = _book(tmp_path)
    results.write_result(book, results.ResultRecord(block_signature="ok", status=results.STATUS_DONE))
    (results.results_dir(book) / "broken.json").write_text("{", encoding="utf-8")

    listed = results.list_results(book)
    assert set(listed) == {"ok"}


# ---------------------------------------------------------------------------
# 失效分类
# ---------------------------------------------------------------------------


def test_classify_splits_done_stale_missing(tmp_path) -> None:
    book = _book(tmp_path, 4)
    manifest = project.load_manifest(book)
    plans = blocks.plan_blocks(manifest, block_size=2)  # [1,2] [3,4]

    results.write_result(
        book, results.ResultRecord(block_signature=plans[0].block_signature, status=results.STATUS_DONE)
    )
    results.write_result(
        book, results.ResultRecord(block_signature=plans[1].block_signature, status=results.STATUS_FAILED)
    )
    records = results.list_results(book)

    verdict = results.classify(manifest, plans, records)
    assert set(verdict.done) == {plans[0].block_signature}
    assert verdict.stale[plans[1].block_signature] == results.STALE_NOT_DONE
    assert verdict.missing == []


def test_classify_marks_block_stale_after_text_change(tmp_path) -> None:
    """正文改动后，即使结果文件还在，也必须判失效——这是 S2 门槛的核心。"""
    root = tmp_path / "v1"
    book = _book(root, 4)
    manifest = project.load_manifest(book)
    plans = blocks.plan_blocks(manifest, block_size=2)
    for plan in plans:
        results.write_result(
            book, results.ResultRecord(block_signature=plan.block_signature, status=results.STATUS_DONE)
        )
    records = results.list_results(book)

    # 改第 3 章，重建同一 book_id 的项目（模拟重新导入同一本书）
    chapters = [(i, f"第{i}章\n正文{i}\n") for i in range(1, 5)]
    chapters[2] = (3, "第3章\n改过的内容\n")
    edited = project.create_project(tmp_path / "v2" / "bk_qu000000001", "书", chapters, book_id="bk_qu000000001")
    new_manifest = project.load_manifest(edited)
    new_plans = blocks.plan_blocks(new_manifest, block_size=2)

    verdict = results.classify(new_manifest, new_plans, records)
    # 失效是局部的：含第 3 章的块签名变了 → 落 missing 待重算；
    # 第 1-2 章那块的签名没变 → 仍然算完成，不该被牵连重跑。
    assert set(verdict.done) == {new_plans[0].block_signature}
    assert verdict.missing == [new_plans[1].block_signature]


def test_classify_flags_changed_block_when_signature_reused(tmp_path) -> None:
    """签名被沿用但内容已变的情形：必须判 STALE，不能算完成。"""
    book = _book(tmp_path, 2)
    manifest = project.load_manifest(book)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]

    forged = blocks.BlockPlan(
        block_signature="deadbeef" * 8,
        chapter_ids=plan.chapter_ids,
        block_strategy_version=plan.block_strategy_version,
    )
    records = {"deadbeef" * 8: results.ResultRecord(block_signature="deadbeef" * 8, status=results.STATUS_DONE)}

    verdict = results.classify(manifest, [forged], records)
    assert verdict.done == {}
    assert verdict.stale["deadbeef" * 8] == results.STALE_BLOCK_CHANGED


# ---------------------------------------------------------------------------
# 队列状态
# ---------------------------------------------------------------------------


def test_queue_roundtrip(tmp_path) -> None:
    path = queue.queue_state_path(tmp_path)
    state = queue.QueueState(entries=[queue.QueueEntry(book_id="b1", name="书一")])
    queue.save_queue_state(path, state)

    loaded = queue.load_queue_state(path)
    assert loaded.find("b1").name == "书一"


def test_queue_missing_file_is_empty_but_legacy_payload_is_error(tmp_path) -> None:
    assert queue.load_queue_state(tmp_path / "none.json").entries == []

    legacy = tmp_path / "queue_state.json"
    legacy.write_text(json.dumps({"items": []}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(VnextFormatError) as exc:
        queue.load_queue_state(legacy)
    assert exc.value.code.value == "UNSUPPORTED_ARTIFACT_FORMAT"


def test_queue_rejects_duplicate_book_id(tmp_path) -> None:
    path = queue.queue_state_path(tmp_path)
    payload = queue.QueueState(
        entries=[
            queue.QueueEntry(book_id="b1", name="甲"),
            queue.QueueEntry(book_id="b1", name="乙"),
        ]
    ).to_dict()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(VnextFormatError):
        queue.load_queue_state(path)


def test_restart_resets_only_running_and_clears_its_error(tmp_path) -> None:
    state = queue.QueueState(
        entries=[
            queue.QueueEntry(book_id="b1", name="A", status=queue.STATUS_RUNNING, error_message="上次崩了"),
            queue.QueueEntry(book_id="b2", name="B", status=queue.STATUS_DONE),
            queue.QueueEntry(book_id="b3", name="C", status=queue.STATUS_FAILED, error_message="真失败"),
        ]
    )
    queue.normalize_after_restart(state)

    assert state.find("b1").status == queue.STATUS_PENDING
    assert state.find("b1").error_message == ""      # 矛盾记录清掉
    assert state.find("b2").status == queue.STATUS_DONE
    assert state.find("b3").error_message == "真失败"  # 历史结论不改写


def test_apply_progress_done_requires_no_stale(tmp_path) -> None:
    entry = queue.QueueEntry(book_id="b1", name="A")
    queue.apply_progress(entry, completed_blocks=2, total_blocks=2, stale_blocks=0, total_chapters=4)
    assert entry.status == queue.STATUS_DONE

    entry2 = queue.QueueEntry(book_id="b1", name="A", status=queue.STATUS_DONE)
    queue.apply_progress(entry2, completed_blocks=2, total_blocks=3, stale_blocks=1, total_chapters=4)
    assert entry2.status == queue.STATUS_PENDING  # 曾经完成也不再算完成


def test_progress_from_manifest_end_to_end(tmp_path) -> None:
    book = _book(tmp_path, 4)
    manifest = project.load_manifest(book)
    plans = blocks.plan_blocks(manifest, block_size=2)
    for plan in plans:
        results.write_result(
            book, results.ResultRecord(block_signature=plan.block_signature, status=results.STATUS_DONE)
        )

    entry = queue.QueueEntry(book_id=manifest.book_id, name=manifest.title)
    queue.progress_from_manifest(entry, manifest, plans, results.list_results(book))

    assert (entry.completed_blocks, entry.total_blocks, entry.stale_blocks) == (2, 2, 0)
    assert entry.status == queue.STATUS_DONE
