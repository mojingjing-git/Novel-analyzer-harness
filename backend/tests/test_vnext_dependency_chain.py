"""块间依赖链：局部失效的正确边界（PLAN v2 §2.4 / 复审第 4 条延伸）。

只比块签名会说"这块还能用"，但某块读了前情摘要——前情变了它其实已经过期。
这里把"依赖"显式化：每块的期望依赖 = 前面各块**当前有效结果 ID** 的有序列表 +
生成条件（Prompt 策略 / 模型 / 上下文策略）。

于是失效边界变得可判定：
- 第 3 块换了结果 → 第 4、5 块失效，第 1、2 块不受影响；
- 换模型或改 Prompt 策略 → 全部失效；
- 前面的块没完成 → 后面的块不算完成（依赖不成立）。
"""

from __future__ import annotations

import pytest

from backend.vnext import blocks, project, results


def _book(tmp_path, n: int = 6, *, book_id: str = "bk_chain000001"):
    chapters = [(i, f"第{i}章\n正文{i}\n") for i in range(1, n + 1)]
    return project.create_project(tmp_path, "书", chapters, book_id=book_id)


def _make_record(manifest_o, plan, strategy, prior, *, model_tag="m"):
    """按"当前依赖"造一条结果（模拟正常产出的样子）。"""
    hashes = {c.chapter_id: c.content_hash for c in manifest_o.chapters}
    dep = results.expected_dependency(prior, strategy)
    rid = results.make_result_id(
        block_signature=plan.block_signature,
        prompt_strategy_version=strategy.prompt_strategy_version,
        model_fingerprint=strategy.model_fingerprint,
        dependency_fingerprint=dep,
    )
    return results.ResultRecord(
        result_id=rid,
        block_signature=plan.block_signature,
        source_chapters=tuple((cid, hashes[cid]) for cid in plan.chapter_ids),
        status=results.ST_SUCCEEDED,
        prompt_strategy_version=strategy.prompt_strategy_version,
        model_fingerprint=strategy.model_fingerprint,
        dependency_fingerprint=dep,
    )


def _publish_chain(book, plans, strategy, *, upto: int | None = None):
    """按顺序发布前 upto 块（None = 全部），返回提交后的清单与各块记录。"""
    manifest = project.load_manifest(book)
    prior: list[str | None] = []
    records = []
    limit = len(plans) if upto is None else upto
    for plan in plans[:limit]:
        record = _make_record(manifest, plan, strategy, prior)
        manifest = results.publish_result(book, record)
        prior.append(record.result_id)
        records.append(record)
    return manifest, records


STRATEGY = results.ResultStrategy(
    prompt_strategy_version=1, model_fingerprint="model=m|temperature=0.1"
)


def test_full_chain_counts_as_done(tmp_path) -> None:
    book = _book(tmp_path, 6)
    plans = blocks.plan_blocks(project.load_manifest(book), block_size=2)
    manifest, _ = _publish_chain(book, plans, STRATEGY)

    verdict = results.classify(
        manifest, plans, results.list_results(book), strategy=STRATEGY
    )
    assert len(verdict.done) == len(plans)
    assert verdict.stale == {}
    assert verdict.missing == {}


def test_replacing_a_middle_result_invalidates_only_later_blocks(tmp_path) -> None:
    """第 2 块重算 → 第 3 块失效，第 1 块不受影响。"""
    book = _book(tmp_path, 6)
    plans = blocks.plan_blocks(project.load_manifest(book), block_size=2)
    manifest, records = _publish_chain(book, plans, STRATEGY)

    # 第 2 块重算：依赖没变，但产出了一个新的 result_id
    recomputed = results.ResultRecord(
        result_id="r" * 64,
        block_signature=plans[1].block_signature,
        source_chapters=records[1].source_chapters,
        status=results.ST_SUCCEEDED,
        dependency_fingerprint=records[1].dependency_fingerprint,
    )
    manifest = results.publish_result(book, recomputed)

    verdict = results.classify(
        manifest, plans, results.list_results(book), strategy=STRATEGY
    )
    assert plans[0].block_signature in verdict.done
    assert plans[1].block_signature in verdict.done
    # 第 3 块的依赖里含"第 2 块的新 result_id"，于是失效
    assert verdict.stale[plans[2].block_signature] == results.STALE_DEPENDENCY


def test_prompt_strategy_change_invalidates_everything(tmp_path) -> None:
    book = _book(tmp_path, 4)
    plans = blocks.plan_blocks(project.load_manifest(book), block_size=2)
    manifest, _ = _publish_chain(book, plans, STRATEGY)

    bumped = results.ResultStrategy(
        prompt_strategy_version=2,
        model_fingerprint=STRATEGY.model_fingerprint,
    )
    verdict = results.classify(
        manifest, plans, results.list_results(book), strategy=bumped
    )
    assert verdict.done == {}
    assert set(verdict.stale.values()) == {results.STALE_DEPENDENCY}


def test_model_change_invalidates_everything(tmp_path) -> None:
    book = _book(tmp_path, 4)
    plans = blocks.plan_blocks(project.load_manifest(book), block_size=2)
    manifest, _ = _publish_chain(book, plans, STRATEGY)

    other = results.ResultStrategy(
        prompt_strategy_version=1, model_fingerprint="model=另一个模型"
    )
    verdict = results.classify(
        manifest, plans, results.list_results(book), strategy=other
    )
    assert verdict.done == {}


def test_incomplete_prefix_breaks_dependents(tmp_path) -> None:
    """只跑完前两块：第 3 块即使有旧结果也不能算完成（它的依赖里有"缺失"）。"""
    book = _book(tmp_path, 6)
    plans = blocks.plan_blocks(project.load_manifest(book), block_size=2)
    manifest, _ = _publish_chain(book, plans, STRATEGY, upto=2)

    verdict = results.classify(
        manifest, plans, results.list_results(book), strategy=STRATEGY
    )
    assert len(verdict.done) == 2
    # 6 章 / 每块 2 章 = 3 块；第 3 块没有结果，也没有可依赖的前序
    assert set(verdict.missing) == {plans[2].block_signature}


def test_text_change_invalidates_block_and_its_successors(tmp_path) -> None:
    book = _book(tmp_path, 6)
    plans = blocks.plan_blocks(project.load_manifest(book), block_size=2)
    manifest, _ = _publish_chain(book, plans, STRATEGY)

    from dataclasses import replace

    from backend.vnext.fingerprint import content_hash, normalize_chapter_text

    text = normalize_chapter_text("第3章\n改过的内容\n")
    (book / "source" / "chapters" / f"{manifest.chapters[2].chapter_id}.txt").write_text(
        text, encoding="utf-8"
    )

    def _mutate(m):
        m.chapters[2] = replace(m.chapters[2], content_hash=content_hash(text))

    manifest2 = project.commit_manifest(book, _mutate)
    new_plans = blocks.plan_blocks(manifest2, block_size=2)

    verdict = results.classify(
        manifest2, new_plans, results.list_results(book), strategy=STRATEGY
    )
    assert new_plans[0].block_signature in verdict.done   # 第 1-2 章未受影响
    assert new_plans[1].block_signature in verdict.missing  # 含第 3 章的块换了签名
    # 第 3 块本身没变，但它依赖的前序断了 → 判失效（而不是"没跑过"）
    assert verdict.stale[new_plans[2].block_signature] == results.STALE_DEPENDENCY


def test_no_strategy_keeps_block_level_semantics(tmp_path) -> None:
    """不传 strategy 时退化为"只比块签名与状态"——供早期扫描/预览使用。"""
    book = _book(tmp_path, 4)
    plans = blocks.plan_blocks(project.load_manifest(book), block_size=2)
    manifest, _ = _publish_chain(book, plans, STRATEGY)

    verdict = results.classify(manifest, plans, results.list_results(book))
    assert len(verdict.done) == 2
