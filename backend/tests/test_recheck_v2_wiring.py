"""阶段 2 接线：全文取材 + 开关默认关闭 + 复检调用记账。

这里只测不该出错的事：
1. 复检拿到的是**原文**，不是卷摘要（拿摘要核引文等于没有证据）；
2. 开关默认开（V2 是默认路径），旧行为回退靠关开关；
3. V2 调 LLM 的唯一入口 `_call_model` 把花费记进 recheck 账——
   且记账发生在抛错**之前**（失败回包的 tokens 已扣费）。
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace

import pytest

from backend.config.settings import AnalysisConfig, AppConfig
from backend.services.final_summary import FinalSummaryRunner
from backend.services.recheck_v2 import RecheckOutcome


def _runner_like(tmp_path: pathlib.Path, *, working_directory=None):
    return SimpleNamespace(
        output_dir=tmp_path / "output",
        config=SimpleNamespace(working_directory=working_directory),
    )


def test_collect_book_text_reads_chapters_in_order(tmp_path):
    (tmp_path / "output").mkdir()
    blocks = tmp_path / "blocks"
    blocks.mkdir()
    (blocks / "0001.txt").write_text("第一章 起\n方醒打开铜匣。", encoding="utf-8")
    (blocks / "0002.txt").write_text("第二章 承\n旧契上写着三年后归还。", encoding="utf-8")
    # 非 txt 文件不该被卷进来
    (blocks / "chapters.json").write_text("[]", encoding="utf-8")

    runner = _runner_like(tmp_path)
    text = FinalSummaryRunner._collect_book_text(runner)

    assert text.index("第一章") < text.index("第二章")
    assert "旧契上写着三年后归还。" in text
    assert "chapters.json" not in text


def test_collect_book_text_falls_back_to_working_directory(tmp_path):
    (tmp_path / "output").mkdir()
    other = tmp_path / "elsewhere"
    (other / "blocks").mkdir(parents=True)
    (other / "blocks" / "0001.txt").write_text("第一章 起\n正文在这里。", encoding="utf-8")

    runner = _runner_like(tmp_path, working_directory=str(other))
    assert "正文在这里。" in FinalSummaryRunner._collect_book_text(runner)


def test_collect_book_text_without_blocks_returns_empty(tmp_path):
    (tmp_path / "output").mkdir()
    runner = _runner_like(tmp_path)
    # 取不到原文时必须返回空串——调用方据此记"未核对"，而不是拿空文去核对
    assert FinalSummaryRunner._collect_book_text(runner) == ""


def test_recheck_v2_switch_defaults_to_on():
    # 真书验证已过（《大奉打更人》1669 候选：checked 1664、SUPPORTED 185），
    # V2 成为默认路径；要回退 1.x 的老复检就把这个开关关掉。
    assert AnalysisConfig().recheck_v2_enabled is True
    assert AnalysisConfig().recheck_max_calls == 300
    # 60000 会顶到部分上游的输出上限而被拒；判定回包（6 候选 JSON）约 2k token
    assert AnalysisConfig().recheck_max_tokens == 32768


# ============ 复检调用记账（token 账与阶段事件） ============

def _make_runner(output_dir: pathlib.Path, on_progress) -> FinalSummaryRunner:
    return FinalSummaryRunner(
        config=AppConfig(),
        output_dir=output_dir,
        start_chapter=1,
        end_chapter=99999,
        batch_size=30,
        concurrency=1,
        on_progress=on_progress,
        on_token_stats=None,
    )


class _FakeResult:
    def __init__(self, success: bool, tokens):
        self.success = success
        self.content = "[]" if success else ""
        self.error = "" if success else "boom"
        self.tokens = tokens


class _FakeLLM:
    def __init__(self, result: _FakeResult):
        self._result = result
        self.calls = 0

    async def chat_auto(self, **kwargs):
        self.calls += 1
        return self._result


def _install_fake_pipeline(monkeypatch, tmp_path: pathlib.Path, result: _FakeResult):
    """把 run_recheck_v2 换成截获 call_model 的假件，返回 (runner, events, captured)。

    run_recheck_v2 在 `_run_recheck_v2` 内是**函数内 import**，必须 patch 源模块属性；
    `_collect_chapters` patch 成固定章节，避开文件系统。
    """
    import backend.services.recheck_v2 as recheck_v2_mod

    events: list[dict] = []
    runner = _make_runner(tmp_path, events.append)
    fake_llm = _FakeLLM(result)
    runner._llm = fake_llm
    monkeypatch.setattr(runner, "_collect_chapters", lambda: {1: "第一章 方醒打开铜匣。"})

    captured: dict = {}

    async def _fake_run_recheck_v2(items, **kwargs):
        captured["call_model"] = kwargs["call_model"]
        return RecheckOutcome()

    monkeypatch.setattr(recheck_v2_mod, "run_recheck_v2", _fake_run_recheck_v2)
    return runner, events, captured


def test_recheck_v2_call_model_records_tokens_and_emits_phase(tmp_path, monkeypatch):
    runner, events, captured = _install_fake_pipeline(
        monkeypatch, tmp_path, _FakeResult(True, (100, 40))
    )
    asyncio.run(runner._run_recheck_v2(results=[]))

    # 阶段事件：V2 一开始就发 phase=recheck，前端步骤条才能进入复检步
    phase_events = [e for e in events if e.get("type") == "phase" and e.get("phase") == "recheck"]
    assert phase_events and "带证据" in phase_events[0].get("phase_label", "")

    # 记账：call_model 是 V2 调 LLM 的唯一入口，花费必须进 recheck 账
    asyncio.run(captured["call_model"]("提示词A"))
    asyncio.run(captured["call_model"]("提示词B"))
    assert runner.get_token_stats()["recheck"] == {"input_tokens": 200, "output_tokens": 80}


def test_recheck_v2_failed_call_still_records_tokens(tmp_path, monkeypatch):
    """校验失败/流中断重试耗尽的回包 tokens 已扣费——记账必须发生在抛错之前。"""
    runner, events, captured = _install_fake_pipeline(
        monkeypatch, tmp_path, _FakeResult(False, (30, 0))
    )
    asyncio.run(runner._run_recheck_v2(results=[]))

    with pytest.raises(RuntimeError, match="复检调用失败"):
        asyncio.run(captured["call_model"]("提示词"))
    assert runner.get_token_stats()["recheck"] == {"input_tokens": 30, "output_tokens": 0}
