"""卷摘要断点续跑测试：
- checkpoint 落盘/恢复往返
- batch_size 变化时忽略旧断点
- 重跑时已恢复批次不再调用卷摘要/reconciliation LLM
- reconciliation 缺失时重跑只补 reconciliation
"""
import asyncio
import json
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import AppConfig
from backend.services.final_summary import FinalSummaryRunner


def _write_results(output_dir: Path, chapters: int):
    """写入 N 个最小 chapter_*_result.json（extract_key_fields 需要 summary 非空）"""
    output_dir.mkdir(parents=True, exist_ok=True)
    for ch in range(1, chapters + 1):
        data = {
            "chapter_number": ch,
            "cross_block": {"summary": f"第{ch}章摘要内容", "unresolved_questions": [], "new_leads": []},
            "core_events": [],
            "character_arcs": [],
            "foreshadowing": [],
            "plot_holes": [],
            "long_context_insights": {},
            "updated_knowledge": {},
        }
        (output_dir / f"chapter_{ch}_result.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # final_summary.run() 需归一化文件存在才不早退
    output_subdir = output_dir / "output" if (output_dir / "output").is_dir() else output_dir
    (output_subdir / "locations_normalized.json").write_text(
        json.dumps({"schema_version": 1, "locations": [], "chapter_mtimes_hash": ""}, ensure_ascii=False),
        encoding="utf-8")
    (output_subdir / "spatial_relationships_normalized.json").write_text(
        json.dumps({"schema_version": 1, "relationships": [], "chapter_mtimes_hash": ""}, ensure_ascii=False),
        encoding="utf-8")


def _make_runner(output_dir: Path, batch_size: int = 2) -> FinalSummaryRunner:
    return FinalSummaryRunner(
        config=AppConfig(),
        output_dir=output_dir,
        start_chapter=1,
        end_chapter=4,
        batch_size=batch_size,
        concurrency=2,
        on_progress=lambda p: None,
        on_token_stats=lambda p: None,
    )


def test_checkpoint_roundtrip(tmp_path):
    """断点保存/恢复往返：卷摘要与 reconciliation 都能恢复，batch_size 变化时忽略"""
    runner = _make_runner(tmp_path, batch_size=2)
    batch_tasks = [(1, 1, 2, "p1"), (2, 3, 4, "p2")]

    asyncio.run(runner._save_checkpoint_batch(1, 1, 2, "卷一摘要", {"reconciliation": []}))
    asyncio.run(runner._save_checkpoint_batch(2, 3, 4, "卷二摘要", None))  # 只落盘卷摘要

    volumes, recons = asyncio.run(runner._load_checkpoint(batch_tasks))
    assert volumes == {1: "卷一摘要", 2: "卷二摘要"}
    assert 1 in recons and 2 not in recons, "未落盘 reconciliation 的批次不应恢复 reconciliation"

    # batch_size 变化 → 忽略旧断点
    runner3 = _make_runner(tmp_path, batch_size=3)
    volumes3, _ = asyncio.run(runner3._load_checkpoint(batch_tasks))
    assert volumes3 == {}, "batch_size 不匹配时应忽略旧断点"


def test_resume_skips_completed_llm_calls(tmp_path):
    """端到端：第一次正常跑完后，第二次重跑完全跳过卷摘要/reconciliation 的 LLM 调用"""
    _write_results(tmp_path, 4)
    runner1 = _make_runner(tmp_path)

    with patch.object(FinalSummaryRunner, '_call_llm_summary', new=AsyncMock(return_value="这是卷摘要正文内容")), \
         patch.object(FinalSummaryRunner, '_call_llm_reconciliation', new=AsyncMock(return_value={"reconciliation": []})), \
         patch.object(FinalSummaryRunner, '_call_llm_final', new=AsyncMock(return_value="最终报告正文")):
        report = asyncio.run(runner1.run())
    assert report and "最终报告" in report

    # checkpoint 已落盘
    cp = tmp_path / "final_summary_checkpoint"
    assert (cp / "volume_1.md").exists() and (cp / "volume_2.md").exists()
    assert (cp / "recon_1.json").exists() and (cp / "recon_2.json").exists()

    # 第二次重跑（模拟重启）：卷摘要/reconciliation 零调用，只有最终报告一次
    runner2 = _make_runner(tmp_path)
    summary_mock = AsyncMock(return_value="不应被调用")
    recon_mock = AsyncMock(return_value={"reconciliation": []})
    final_mock = AsyncMock(return_value="第二次报告")
    with patch.object(FinalSummaryRunner, '_call_llm_summary', new=summary_mock), \
         patch.object(FinalSummaryRunner, '_call_llm_reconciliation', new=recon_mock), \
         patch.object(FinalSummaryRunner, '_call_llm_final', new=final_mock):
        report2 = asyncio.run(runner2.run())
    assert report2 and "第二次报告" in report2
    summary_mock.assert_not_awaited()
    recon_mock.assert_not_awaited()
    final_mock.assert_awaited_once()


def test_resume_reconciliation_only_when_missing(tmp_path):
    """端到端：上次 reconciliation 未完成（只落盘卷摘要）时，重跑只补 reconciliation，不重跑卷摘要"""
    _write_results(tmp_path, 4)
    runner1 = _make_runner(tmp_path)

    # 第一次：reconciliation 全部返回 None（模拟该阶段失败/中断）
    with patch.object(FinalSummaryRunner, '_call_llm_summary', new=AsyncMock(return_value="卷摘要A")), \
         patch.object(FinalSummaryRunner, '_call_llm_reconciliation', new=AsyncMock(return_value=None)), \
         patch.object(FinalSummaryRunner, '_call_llm_final', new=AsyncMock(return_value="报告一")):
        asyncio.run(runner1.run())

    cp = tmp_path / "final_summary_checkpoint"
    assert (cp / "volume_1.md").exists() and not (cp / "recon_1.json").exists(), \
        "reconciliation 失败不应落盘（下次补跑）"

    # 第二次重跑：卷摘要零调用，reconciliation 补跑 2 批
    runner2 = _make_runner(tmp_path)
    summary_mock = AsyncMock(return_value="不应调用")
    recon_mock = AsyncMock(return_value={"reconciliation": []})
    with patch.object(FinalSummaryRunner, '_call_llm_summary', new=summary_mock), \
         patch.object(FinalSummaryRunner, '_call_llm_reconciliation', new=recon_mock), \
         patch.object(FinalSummaryRunner, '_call_llm_final', new=AsyncMock(return_value="报告二")):
        asyncio.run(runner2.run())
    summary_mock.assert_not_awaited()
    assert recon_mock.await_count == 2, f"应补跑全部批次 reconciliation，实际 {recon_mock.await_count}"


def test_stale_volume_file_gets_overwritten(tmp_path):
    """残留的陈旧 volume_N.md 必须被新生成内容覆盖：batch_size 变化后新摘要写不进盘、
    manifest 却登记旧文件 → 过期内容中毒报告"""
    runner = _make_runner(tmp_path, batch_size=2)
    cp = tmp_path / "final_summary_checkpoint"
    cp.mkdir(parents=True)
    vf = cp / "volume_1.md"
    vf.write_text("陈旧的旧分卷方案内容", encoding="utf-8")

    asyncio.run(runner._save_checkpoint_batch(1, 1, 2, "全新卷摘要", None))

    assert vf.read_text(encoding="utf-8") == "全新卷摘要"


def test_checkpoint_invalidated_when_results_change(tmp_path):
    """章节数据变化（mtime/size 变化）后旧断点整体作废，且残留 volume/recon 文件被清空"""
    _write_results(tmp_path, 4)
    runner = _make_runner(tmp_path, batch_size=2)
    batch_tasks = [(1, 1, 2, "p1"), (2, 3, 4, "p2")]

    asyncio.run(runner._save_checkpoint_batch(1, 1, 2, "卷一", {"reconciliation": []}))
    asyncio.run(runner._save_checkpoint_batch(2, 3, 4, "卷二", {"reconciliation": []}))

    volumes, _ = asyncio.run(runner._load_checkpoint(batch_tasks))
    assert volumes == {1: "卷一", 2: "卷二"}

    # 模拟重新分析覆盖了第 3 章结果（内容不同 → size/mtime 变化）
    time.sleep(0.02)  # 保证 mtime_ns 变化（Windows 时钟粒度）
    changed = json.loads((tmp_path / "chapter_3_result.json").read_text(encoding="utf-8"))
    changed["cross_block"]["summary"] = "重写后的第三章摘要"
    (tmp_path / "chapter_3_result.json").write_text(
        json.dumps(changed, ensure_ascii=False), encoding="utf-8")

    volumes2, recons2 = asyncio.run(runner._load_checkpoint(batch_tasks))
    assert volumes2 == {} and recons2 == {}, "章节数据变化后断点必须整体作废"
    cp = tmp_path / "final_summary_checkpoint"
    assert not (cp / "volume_1.md").exists(), "作废断点的残留文件必须被清空"
    assert not (cp / "recon_2.json").exists()


def test_checkpoint_invalidated_when_manifest_corrupt(tmp_path):
    """manifest.json 损坏时清空整个断点目录（只忽略会让残留文件继续毒化下次登记）"""
    _write_results(tmp_path, 4)
    runner = _make_runner(tmp_path, batch_size=2)
    cp = tmp_path / "final_summary_checkpoint"
    cp.mkdir(parents=True)
    (cp / "volume_1.md").write_text("孤儿卷摘要", encoding="utf-8")
    (cp / "manifest.json").write_text("{broken json", encoding="utf-8")

    volumes, _ = asyncio.run(runner._load_checkpoint([(1, 1, 2, "p1")]))
    assert volumes == {}
    assert not (cp / "volume_1.md").exists(), "孤儿 volume 文件必须随 manifest 一起清掉"
