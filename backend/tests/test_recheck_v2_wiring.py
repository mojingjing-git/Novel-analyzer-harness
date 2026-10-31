"""阶段 2 接线：全文取材 + 开关默认关闭。

这里只测两件不该出错的事：
1. 复检拿到的是**原文**，不是卷摘要（拿摘要核引文等于没有证据）；
2. 开关默认关，旧行为不受影响。
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

from backend.config.settings import AnalysisConfig
from backend.services.final_summary import FinalSummaryRunner


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


def test_recheck_v2_switch_defaults_to_off():
    # 默认关：真书验证通过前，1.x 行为保持不变
    assert AnalysisConfig().recheck_v2_enabled is False
    assert AnalysisConfig().recheck_max_calls == 300
    assert AnalysisConfig().recheck_max_tokens == 60000
