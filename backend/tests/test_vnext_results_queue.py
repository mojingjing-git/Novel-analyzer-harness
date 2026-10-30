"""vnext 结果落盘与队列恢复（PLAN v2 §3.4 / §4 S2-6）。

协议要点（这版按契约收紧，四处曾是错的）：
- 文件名是 ``result_id``，不是块签名；
- 状态是 ``SUCCEEDED/FAILED/SKIPPED/CANCELLED/BLOCKED``，不是小写 done；
- **只有登记进 ``manifest.active_results`` 的结果才算数**；
- 登记的判定要看"送进去的章节"是否仍与块成员一致。

注意：``publish_result`` 返回**提交后的清单**。``commit_manifest`` 在锁内重新读清单
再改，调用方手里那份是发布前快照，拿它去 classify 会看到空的 ``active_results``。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from backend.vnext import blocks, project, queue, results
from backend.vnext.constants import VnextFormatError
from backend.vnext.fingerprint import content_hash, normalize_chapter_text


def _book(tmp_path, n: int = 4, *, book_id: str = "bk_qu000000001", title: str = "书"):
    chapters = [(i, f"第{i}章\n正文{i}\n") for i in range(1, n + 1)]
    return project.create_project(tmp_path, title, chapters, book_id=book_id)


def _publish(book, manifest, plan, *, status=results.ST_SUCCEEDED, dep="dep-1",
             created_at=1.0, model="m1"):
    """造一条结果并发布，返回 (记录, 提交后的清单)。"""
    hashes = {c.chapter_id: c.content_hash for c in manifest.chapters}
    source = tuple((cid, hashes[cid]) for cid in plan.chapter_ids)
    rid = results.make_result_id(
        block_signature=plan.block_signature,
        prompt_strategy_version=1,
        model_fingerprint=model,
        dependency_fingerprint=dep,
    )
    record = results.ResultRecord(
        result_id=rid,
        block_signature=plan.block_signature,
        source_chapters=source,
        status=status,
        model_fingerprint=model,
        dependency_fingerprint=dep,
        created_at=created_at,
    )
    return record, results.publish_result(book, record)


def _rewrite_chapter(book, manifest, ordinal: int, new_text: str):
    """改一章正文并提交清单（同一项目内，active_results 保留）。"""
    normalized = normalize_chapter_text(new_text)
    chapter = manifest.chapters[ordinal - 1]
    (book / "source" / "chapters" / f"{chapter.chapter_id}.txt").write_text(
        normalized, encoding="utf-8"
    )

    def _mutate(m):
        m.chapters[ordinal - 1] = replace(m.chapters[ordinal - 1],
                                          content_hash=content_hash(normalized))

    return project.commit_manifest(book, _mutate)


# ---------------------------------------------------------------------------
# 落盘协议
# ---------------------------------------------------------------------------


def test_result_file_is_named_by_result_id(tmp_path) -> None:
    book = _book(tmp_path, 2)
    manifest = project.load_manifest(book)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]
    record, _ = _publish(book, manifest, plan)

    path = results.result_path(book, record.result_id)
    assert path.is_file()
    assert path.name == f"{record.result_id}.json"
    assert path.name != f"{plan.block_signature}.json"  # 不是块签名命名


def test_unknown_usage_stays_unknown(tmp_path) -> None:
    book = _book(tmp_path, 2)
    manifest = project.load_manifest(book)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]
    record, _ = _publish(book, manifest, plan)

    reread = results.list_results(book)[record.result_id]
    assert reread.prompt_tokens is None
    assert reread.usage_status == "UNKNOWN"


def test_status_is_uppercase_vocabulary(tmp_path) -> None:
    book = _book(tmp_path, 2)
    with pytest.raises(VnextFormatError) as exc:
        results.write_result(
            book,
            results.ResultRecord(
                result_id="r1", block_signature="s1", source_chapters=(), status="done"
            ),
        )
    assert exc.value.code.value == "INVALID_FIELD_VALUE"


def test_list_results_ignores_file_whose_name_disagrees_with_inner_id(tmp_path) -> None:
    """改名不能让旧结果复活：文件名与内部 result_id 必须一致。"""
    book = _book(tmp_path, 2)
    manifest = project.load_manifest(book)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]
    record, _ = _publish(book, manifest, plan)

    original = results.result_path(book, record.result_id)
    original.rename(results.results_dir(book) / "renamed.json")
    assert results.list_results(book) == {}


# ---------------------------------------------------------------------------
# 登记与失效判定
# ---------------------------------------------------------------------------


def test_unregistered_result_does_not_count_as_done(tmp_path) -> None:
    """写进磁盘但没登记的结果不算完成——它可能是别的源留下的。"""
    book = _book(tmp_path, 2)
    manifest = project.load_manifest(book)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]

    hashes = {c.chapter_id: c.content_hash for c in manifest.chapters}
    record = results.ResultRecord(
        result_id="r-unregistered",
        block_signature=plan.block_signature,
        source_chapters=tuple((cid, hashes[cid]) for cid in plan.chapter_ids),
        status=results.ST_SUCCEEDED,
    )
    results.write_result(book, record)  # 只写，不登记

    verdict = results.classify(manifest, [plan], results.list_results(book))
    assert verdict.done == {}
    assert verdict.missing[plan.block_signature] == results.MISSING_NOT_REGISTERED
    # 文件在、没登记：单列出来（通常是别的源留下的旧版本）
    assert verdict.unregistered == ["r-unregistered"]
    assert verdict.orphans == []


def test_registered_but_missing_file_is_reported_separately(tmp_path) -> None:
    """登记了但文件被删 → 与"从没跑过"不同，要能分开报。"""
    book = _book(tmp_path, 2)
    manifest = project.load_manifest(book)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]
    record, manifest = _publish(book, manifest, plan)

    results.result_path(book, record.result_id).unlink()
    verdict = results.classify(manifest, [plan], results.list_results(book))
    assert verdict.missing[plan.block_signature] == results.MISSING_NOT_REGISTERED
    # "登记了但文件没了"按 result_id 单列：文件不在时无法归因到具体块
    assert verdict.orphans == [record.result_id]
    assert verdict.unregistered == []


def test_succeeded_registered_result_counts_as_done(tmp_path) -> None:
    """反向错误也要堵：合法登记 + SUCCEEDED 的结果必须被认。"""
    book = _book(tmp_path, 4)
    manifest = project.load_manifest(book)
    plans = blocks.plan_blocks(manifest, block_size=2)
    for plan in plans:
        _, manifest = _publish(book, manifest, plan)

    verdict = results.classify(manifest, plans, results.list_results(book))
    assert set(verdict.done) == {p.block_signature for p in plans}
    assert verdict.stale == {}
    assert verdict.missing == {}


def test_failed_status_is_stale_not_done(tmp_path) -> None:
    book = _book(tmp_path, 2)
    manifest = project.load_manifest(book)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]
    _, manifest = _publish(book, manifest, plan, status=results.ST_FAILED)

    verdict = results.classify(manifest, [plan], results.list_results(book))
    assert verdict.done == {}
    assert verdict.stale[plan.block_signature] == f"{results.STALE_NOT_DONE}:FAILED"


def test_changed_text_is_local_failure_not_global(tmp_path) -> None:
    """改第 3 章：含它的块签名变了 → 待重算；不含它的块仍然算完成。"""
    book = _book(tmp_path, 4)
    manifest = project.load_manifest(book)
    plans = blocks.plan_blocks(manifest, block_size=2)  # [1,2] [3,4]
    for plan in plans:
        _, manifest = _publish(book, manifest, plan)

    manifest2 = _rewrite_chapter(book, manifest, 3, "第3章\n改过的内容\n")
    new_plans = blocks.plan_blocks(manifest2, block_size=2)

    verdict = results.classify(manifest2, new_plans, results.list_results(book))
    assert new_plans[0].block_signature in verdict.done          # 未受影响
    assert new_plans[1].block_signature in verdict.missing       # 换了签名 → 没跑过


def test_source_chapters_mismatch_is_stale(tmp_path) -> None:
    """结果自称送进去的章节与块成员不一致（被改过/串过）→ 不能算完成。"""
    book = _book(tmp_path, 2)
    manifest = project.load_manifest(book)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]
    record, manifest = _publish(book, manifest, plan)

    path = results.result_path(book, record.result_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["source_chapters"] = [[raw["source_chapters"][0][0], "0" * 64]]
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    verdict = results.classify(manifest, [plan], results.list_results(book))
    assert verdict.stale[plan.block_signature] == results.STALE_SOURCE_MISMATCH


def test_dependency_change_marks_stale(tmp_path) -> None:
    book = _book(tmp_path, 2)
    manifest = project.load_manifest(book)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]
    _, manifest = _publish(book, manifest, plan, dep="dep-old")

    records = results.list_results(book)
    fresh = results.classify(manifest, [plan], records, dependency_fingerprint="dep-old")
    assert plan.block_signature in fresh.done

    stale = results.classify(manifest, [plan], records, dependency_fingerprint="dep-new")
    assert stale.stale[plan.block_signature] == results.STALE_DEPENDENCY


def test_publishing_newer_result_retires_older_one(tmp_path) -> None:
    """同块两条结果共存会让"取哪一条"不确定；发布新的即让旧的退出登记。"""
    book = _book(tmp_path, 2)
    manifest = project.load_manifest(book)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]
    old, _ = _publish(book, manifest, plan, model="m1")
    new, manifest2 = _publish(book, manifest, plan, model="m2")

    assert manifest2.active_results == [new.result_id]
    assert old.result_id not in manifest2.active_results

    verdict = results.classify(manifest2, [plan], results.list_results(book))
    assert verdict.done[plan.block_signature].result_id == new.result_id


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
        _, manifest = _publish(book, manifest, plan)

    entry = queue.QueueEntry(book_id=manifest.book_id, name=manifest.title)
    queue.progress_from_manifest(entry, manifest, plans, results.list_results(book))

    assert (entry.completed_blocks, entry.total_blocks, entry.stale_blocks) == (2, 2, 0)
    assert entry.status == queue.STATUS_DONE
