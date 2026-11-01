"""复检上下文超限保护测试
- 超预算时按埋设章砍卷降级
- 超预算且无卷范围时跳过
- 单卷超预算且伏笔在第1章时跳过
- 预算内保留全文
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import AppConfig
from backend.services.final_summary import FinalSummaryRunner


def _make_runner(output_dir: Path) -> FinalSummaryRunner:
    return FinalSummaryRunner(
        config=AppConfig(),
        output_dir=output_dir,
        start_chapter=1,
        end_chapter=99999,
        batch_size=30,
        concurrency=1,
        on_progress=lambda p: None,
        on_token_stats=None,
    )


def _mk_volumes(n, chars):
    """n 卷，每卷 chars 字符；返回 (volume_summaries, volume_ranges)，每卷 30 章"""
    vols, ranges = [], []
    for i in range(n):
        cs, ce = i * 30 + 1, (i + 1) * 30
        vols.append(f"=== 第{cs}-{ce}章 ===\n" + "x" * chars)
        ranges.append((cs, ce))
    return vols, ranges


def _mk_items(first_seens):
    return [{"id": f"fs_{i:03d}", "clue": f"伏笔{i}", "first_seen": fs,
             "last_seen": fs, "evidence_chapters": [fs]}
            for i, fs in enumerate(first_seens)]


def test_plan_recheck_under_budget_keeps_full_text(tmp_path):
    runner = _make_runner(tmp_path)
    vols, ranges = _mk_volumes(2, 1000)
    items = _mk_items([5, 40])
    plans = runner._plan_recheck_batches(items, "\n\n".join(vols), vols, ranges)
    assert len(plans) == 1
    assert plans[0][0] == items
    assert "第1-30章" in plans[0][1] and "第31-60章" in plans[0][1]


def test_plan_recheck_over_budget_drops_pre_burial_volumes(tmp_path):
    """10 卷×2 万字符=20 万>预算 15 万；伏笔全埋在后段→砍掉 ch_end < 组内最早 first_seen 的卷"""
    runner = _make_runner(tmp_path)
    vols, ranges = _mk_volumes(10, 20000)
    items = _mk_items([250, 260, 270, 280, 290, 295])
    plans = runner._plan_recheck_batches(items, "\n\n".join(vols), vols, ranges)
    assert plans, "砍卷后应有可用计划"
    for _items, text in plans:
        assert "第1-30章" not in text          # 埋设前的卷已被砍掉
        assert "第271-300章" in text          # 埋设点所在的卷必须保留
        assert len(text) <= 150000


def test_plan_recheck_still_over_budget_skips(tmp_path):
    """单卷就超预算且伏笔埋在第 1 章→无卷可砍→整组跳过（返回空），不调用 LLM 烧钱"""
    runner = _make_runner(tmp_path)
    vols, ranges = _mk_volumes(1, 200000)
    plans = runner._plan_recheck_batches(_mk_items([1]), vols[0], vols, ranges)
    assert plans == []


def test_plan_recheck_over_budget_without_ranges_skips(tmp_path):
    """无卷范围信息且超预算→跳过（返回空），不裸调 LLM 赌 400"""
    runner = _make_runner(tmp_path)
    vols, _ = _mk_volumes(1, 200000)
    plans = runner._plan_recheck_batches(_mk_items([1]), vols[0], vols, None)
    assert plans == []
