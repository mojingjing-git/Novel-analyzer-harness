"""B1b：顺序跑整本。三条硬性质，都用 fake client 验证（不触网）。

1. **顺序**：每块的上下文依赖"它之前的块"，跳着跑就是换了一套输入。
2. **失败要留痕**：失败块写一条 FAILED 结果，否则下次进来还是"没有结果"，看不出试过。
3. **续跑等价于一次跑完**：中途停 → 再跑 → 库指纹与块结果与"一次跑完"一致。
"""

from __future__ import annotations

import json

import pytest

from backend.vnext import blocks, project, rolling
from backend.vnext.analyzer import PROMPT_STRATEGY_VERSION
from backend.vnext.knowledge import store_fingerprint
from backend.vnext.prompt_limits import PromptLimits
from backend.vnext.results import ResultStrategy, classify, list_results
from backend.vnext.runner import knowledge_path, load_or_rebuild_store, run_analysis

CHAPTERS = [
    (1, "第一章\n方醒从棺中坐起。\n"),
    (2, "第二章\n他推开棺盖。\n"),
    (3, "第三章\n雨声渐密。\n"),
    (4, "第四章\n有人敲门。\n"),
]


def _payload(text: str, quote: str, start: int) -> str:
    return json.dumps(
        {
            "facts": [
                {
                    "fact_id": f"f-{text}",
                    "text": text,
                    "quote": {"start": start, "end": start + len(quote), "text": quote},
                }
            ],
            "characters": [],
        },
        ensure_ascii=False,
    )


class ScriptedClient:
    """按块序返回预设内容；可指定某次调用抛错。"""

    def __init__(self, contents: list[str], fail_at: int | None = None):
        self.contents = contents
        self.fail_at = fail_at
        self.calls: list[list[dict]] = []

    async def chat(self, messages, temperature=0.1, max_tokens=None):  # noqa: ANN001
        index = len(self.calls)
        self.calls.append(messages)
        if self.fail_at is not None and index == self.fail_at:
            raise RuntimeError("模拟模型超时")

        class _R:
            pass

        r = _R()
        r.content = self.contents[index % len(self.contents)]
        r.tokens = (10, 5)
        r.usage_status = "KNOWN"
        return r


@pytest.fixture()
def book(tmp_path):
    return project.create_project(tmp_path, "书", CHAPTERS, book_id="bk_run0000001")


# ---------------------------------------------------------------------------
# 正向
# ---------------------------------------------------------------------------


async def test_runs_all_blocks_in_order(book) -> None:
    texts = ["方醒从棺中坐起", "他推开棺盖", "雨声渐密", "有人敲门"]
    client = ScriptedClient([_payload(t, t, 4) for t in texts])
    outcome = await run_analysis(
        book,
        client=client,
        limits=PromptLimits(),
        strategy=ResultStrategy(model_fingerprint="m1"),
        block_size=1,
    )

    assert outcome.stop_reason == "completed"
    assert outcome.coverage.commit_cursor == 4
    assert outcome.coverage.gaps == ()
    assert outcome.committed_blocks == 4
    assert outcome.prompt_tokens == 40 and outcome.completion_tokens == 20
    assert outcome.store_fingerprint

    manifest = project.load_manifest(book)
    plans = blocks.plan_blocks(manifest, block_size=1)
    results = list_results(book)
    for plan in plans:
        matching = [r for r in results.values() if r.block_signature == plan.block_signature]
        assert matching and all(r.status == "SUCCEEDED" for r in matching)


async def test_second_run_makes_no_new_calls(book) -> None:
    texts = ["方醒从棺中坐起", "他推开棺盖", "雨声渐密", "有人敲门"]
    first = ScriptedClient([_payload(t, t, 4) for t in texts])
    await run_analysis(book, client=first, limits=PromptLimits(), strategy=ResultStrategy(model_fingerprint="m1"), block_size=1)

    second = ScriptedClient([_payload(t, t, 4) for t in texts])
    outcome = await run_analysis(book, client=second, limits=PromptLimits(), strategy=ResultStrategy(model_fingerprint="m1"), block_size=1)
    assert second.calls == []          # 全命中缓存
    assert outcome.coverage.commit_cursor == 4


# ---------------------------------------------------------------------------
# 失败留痕与停点
# ---------------------------------------------------------------------------


async def test_failure_writes_failed_record_and_stops(book) -> None:
    texts = ["方醒从棺中坐起", "他推开棺盖", "雨声渐密", "有人敲门"]
    client = ScriptedClient([_payload(t, t, 4) for t in texts], fail_at=1)
    outcome = await run_analysis(book, client=client, limits=PromptLimits(), strategy=ResultStrategy(model_fingerprint="m1"), block_size=1)

    assert outcome.stop_reason.startswith("failed:block1")
    assert outcome.coverage.commit_cursor == 1
    # 没有结果 ≠ 跳过：gaps 只记 TERMINAL_SKIPPED，未跑过的块在 missing 里
    assert outcome.coverage.missing == (1, 2, 3)

    results = list_results(book)
    assert any(r.status == "FAILED" for r in results.values())


async def test_resume_after_failure_matches_single_pass(book) -> None:
    """中途失败 → 再跑一次 → 库指纹与"一次跑完"完全相同。这是 B0 存在的理由。"""
    texts = ["方醒从棺中坐起", "他推开棺盖", "雨声渐密", "有人敲门"]
    payloads = [_payload(t, t, 4) for t in texts]

    first = ScriptedClient(payloads, fail_at=1)
    await run_analysis(book, client=first, limits=PromptLimits(), strategy=ResultStrategy(model_fingerprint="m1"), block_size=1)

    second = ScriptedClient(payloads[1:] + payloads)
    resumed = await run_analysis(book, client=second, limits=PromptLimits(), strategy=ResultStrategy(model_fingerprint="m1"), block_size=1)

    # 另建一本，一次跑完做对照
    from pathlib import Path

    # 对照必须**同一个 book_id**：chapter_id 由 (book_id, ordinal) 决定，
    # 换个 book_id 就是换了一套章节身份，指纹当然不同。
    other = project.create_project(Path(book).parent / "ctrl", "书", CHAPTERS, book_id="bk_run0000001")
    straight_client = ScriptedClient(payloads)
    straight = await run_analysis(other, client=straight_client, limits=PromptLimits(), strategy=ResultStrategy(model_fingerprint="m1"), block_size=1)

    assert resumed.coverage.commit_cursor == 4
    assert resumed.store_fingerprint == straight.store_fingerprint


async def test_block_limit_stops_early_but_keeps_prefix(book) -> None:
    texts = ["方醒从棺中坐起", "他推开棺盖", "雨声渐密", "有人敲门"]
    client = ScriptedClient([_payload(t, t, 4) for t in texts])
    outcome = await run_analysis(
        book, client=client, limits=PromptLimits(),
        strategy=ResultStrategy(model_fingerprint="m1"), block_size=1, max_blocks=2,
    )
    assert outcome.stop_reason == "block_limit"
    assert outcome.coverage.commit_cursor == 2
    assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# 库重建
# ---------------------------------------------------------------------------


async def test_store_rebuilds_from_results_when_file_missing(book) -> None:
    texts = ["方醒从棺中坐起", "他推开棺盖", "雨声渐密", "有人敲门"]
    await run_analysis(book, client=ScriptedClient([_payload(t, t, 4) for t in texts]),
                       limits=PromptLimits(), strategy=ResultStrategy(model_fingerprint="m1"), block_size=1)
    before = store_fingerprint(load_or_rebuild_store(book))

    knowledge_path(book).unlink()
    after = store_fingerprint(load_or_rebuild_store(book))
    assert before == after       # 重放 ≡ 原库
