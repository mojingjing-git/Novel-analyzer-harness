"""S8 生产接线后的入口级故障测试（复审三第 1、2 条）。

这些用例打在**真实发布入口** `results.publish_result` 上，不是打在封装上——
封装测得再绿，只要没接进生产路径就等于没有保护。
"""

from __future__ import annotations

import pytest

from backend.vnext import blocks, consistency, project, results


def _book(tmp_path, chapters: int = 4):
    items = [(i, f"第{i}章\n正文{i}\n") for i in range(1, chapters + 1)]
    book_dir = project.create_project(tmp_path / "bk", "书", items, book_id="bk_s8")
    manifest = project.load_manifest(book_dir)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]
    return book_dir, manifest, plan


def _record(plan, manifest):
    return results.ResultRecord(
        result_id=results.make_result_id(
            block_signature=plan.block_signature,
            prompt_strategy_version=1,
            model_fingerprint="m1",
            dependency_fingerprint="d1",
        ),
        block_signature=plan.block_signature,
        source_chapters=[
            (cid, next(c.content_hash for c in manifest.chapters if c.chapter_id == cid))
            for cid in plan.chapter_ids
        ],
        status="SUCCEEDED",
        model_fingerprint="m1",
        prompt_strategy_version=1,
        dependency_fingerprint="d1",
        extracted={"core_events": []},
        prompt_tokens=1,
        completion_tokens=1,
        usage_status="KNOWN",
        created_at="2026-10-30T00:00:00Z",
    )


def test_publish_happy_path_registers_and_writes(tmp_path) -> None:
    book_dir, manifest, plan = _book(tmp_path)
    record = _record(plan, manifest)
    manifest = results.publish_result(book_dir, record)
    assert record.result_id in manifest.active_results
    assert results.result_path(book_dir, record.result_id).is_file()


def test_crash_before_write_leaves_manifest_untouched(tmp_path, monkeypatch) -> None:
    book_dir, manifest_before, plan = _book(tmp_path)
    record = _record(plan, manifest_before)

    def _boom(*_a, **_k):
        raise RuntimeError("崩在写文件之前")

    monkeypatch.setattr(results, "write_result", _boom)
    with pytest.raises(results.PublishFailed):
        results.publish_result(book_dir, record)

    assert results.load_manifest(book_dir).active_results == manifest_before.active_results
    assert not results.result_path(book_dir, record.result_id).exists()


def test_commit_failure_removes_file_and_manifest_unchanged(tmp_path, monkeypatch) -> None:
    """登记确实没成 → 文件必须删掉，否则留下一个没人引用的孤儿。"""
    book_dir, manifest_before, plan = _book(tmp_path)
    record = _record(plan, manifest_before)

    def _boom(*_a, **_k):
        raise RuntimeError("崩在提交之前")

    monkeypatch.setattr(results, "commit_manifest", _boom)
    with pytest.raises(results.PublishFailed) as excinfo:
        results.publish_result(book_dir, record)

    assert excinfo.value.outcome.file_removed is True
    assert not results.result_path(book_dir, record.result_id).exists()
    assert results.load_manifest(book_dir).active_results == manifest_before.active_results


def test_commit_written_then_raise_keeps_file_and_registration(tmp_path, monkeypatch) -> None:
    """复审三第 2 条的正例：manifest 写完才炸，文件已被引用，绝不能删。"""
    book_dir, manifest, plan = _book(tmp_path)
    record = _record(plan, manifest)
    real_commit = results.commit_manifest

    def _commit_then_boom(book_dir_arg, mutate, **kwargs):
        real_commit(book_dir_arg, mutate, **kwargs)   # 登记真的落盘了
        raise RuntimeError("登记成功之后才炸（例如随后写日志失败）")

    monkeypatch.setattr(results, "commit_manifest", _commit_then_boom)
    manifest = results.publish_result(book_dir, record)   # 探针发现已登记 → 视为成功

    assert record.result_id in manifest.active_results
    assert results.result_path(book_dir, record.result_id).is_file()


def test_unverifiable_commit_keeps_file_for_safety(tmp_path, monkeypatch) -> None:
    """提交状态不明 → 保留文件。孤儿能扫出来，误删不可逆。"""
    book_dir, manifest, plan = _book(tmp_path)
    record = _record(plan, manifest)

    def _boom(*_a, **_k):
        raise RuntimeError("提交失败")

    def _probe_boom(_book_dir):
        raise RuntimeError("探针也失败")

    monkeypatch.setattr(results, "commit_manifest", _boom)
    monkeypatch.setattr(results, "load_manifest", _probe_boom)
    with pytest.raises(results.PublishFailed) as excinfo:
        results.publish_result(book_dir, record)

    assert excinfo.value.outcome.kept_for_safety is True
    assert results.result_path(book_dir, record.result_id).is_file()


def test_orphan_scan_finds_kept_file(tmp_path, monkeypatch) -> None:
    """保留下来的文件要能被扫出来——否则"保守保留"就变成永久垃圾。"""
    book_dir, manifest, plan = _book(tmp_path)
    record = _record(plan, manifest)

    def _boom(*_a, **_k):
        raise RuntimeError("提交失败")

    monkeypatch.setattr(results, "commit_manifest", _boom)
    monkeypatch.setattr(results, "load_manifest", lambda *_a: (_ for _ in ()).throw(RuntimeError("probe")))
    with pytest.raises(results.PublishFailed):
        results.publish_result(book_dir, record)

    monkeypatch.undo()
    orphans = results.scan_orphans(book_dir)
    assert [p.stem for p in orphans] == [record.result_id]
