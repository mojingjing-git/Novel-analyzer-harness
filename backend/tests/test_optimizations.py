"""优化项测试：快照缓存不泄漏未来章节 / 断号目录读取 / 休眠判定 / 倒排索引去重"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core.file_processor import FileProcessor
from backend.core.memory_state import MemoryState
from backend.models.analysis_result import (
    AnalysisResult, CoreEvent, CrossBlock,
    UpdatedKnowledge, LongContextInsights,
)
from backend.utils.foreshadow_ledger import ForeshadowLedger, ForeshadowItem


def _result(ch: int, event: str) -> AnalysisResult:
    r = AnalysisResult(chapter_number=ch)
    r.core_events = [CoreEvent(id=1, event=event, characters="A", function="f")]
    r.cross_block = CrossBlock(summary=f"summary-{ch}", unresolved_questions=[], new_leads=[], contextual_link="")
    r.updated_knowledge = UpdatedKnowledge(timeline=f"t{ch}", world_building=[f"w{ch}"])
    r.long_context_insights = LongContextInsights(
        thematic_elements=[f"theme{ch}"], pattern="", foreshadowing_network="", pacing="")
    return r


def test_snapshot_cache_no_future_leak():
    """请求更小 chapter_limit 时不得返回含未来章节的缓存超集"""
    state = MemoryState()
    for ch in range(1, 6):
        asyncio.run(state.add_result(_result(ch, f"event{ch}")))

    # 先构建大 limit 的缓存（模拟并发下高章号块先完成）
    kb_high = state.get_kb_snapshot(chapter_limit=5)
    assert "event5" in kb_high.verified_facts

    # 再请求小 limit：必须严格只含 <=2 的结果
    kb_low = state.get_kb_snapshot(chapter_limit=2)
    facts = set(kb_low.verified_facts)
    assert "event1" in facts and "event2" in facts
    assert "event5" not in facts, "未来章节知识泄漏进了低章号快照"

    # 增量扩展路径：3 -> 5 应能拿到 event5，且仍不含不存在章节
    kb_inc = state.get_kb_snapshot(chapter_limit=5)
    assert "event5" in kb_inc.verified_facts
    assert "event1" in kb_inc.verified_facts


def test_read_block_uses_actual_chapters(tmp_path):
    """断号目录（1/3/5.txt）按实际章号列表读取，不静默丢章"""
    for ch in (1, 3, 5):
        (tmp_path / f"{ch}.txt").write_text(f"第{ch}章内容", encoding="utf-8")
    fp = FileProcessor(tmp_path)
    assert fp.scan_chapters() == [1, 3, 5]

    content = fp.read_block([1, 3])
    assert "第1章内容" in content and "第3章内容" in content

    content2 = fp.read_block([5])
    assert content2 == "第5章内容"


def test_dormancy_uses_current_chapter():
    """休眠判定用当前进度章而非全书末尾章数，后埋伏笔不被提前判死"""
    ledger = ForeshadowLedger()
    ledger.total_chapters = 1000
    item = ForeshadowItem(
        id="fs_001", description="埋在第800章的伏笔", first_seen_chapter=800,
        last_seen_chapter=800, first_seen_batch=0, last_seen_batch=0,
        status="active")
    ledger.items.append(item)

    # 旧行为会在 batch 15 时用 total_chapters 误判休眠；新行为用 current_chapter
    ledger.reconcile_and_update(batch_idx=15, current_chapter=450)
    assert item.status == "active", "后埋伏笔被提前判为休眠"

    # 真正"200 章未见 + 15 批未见"才休眠
    ledger.reconcile_and_update(batch_idx=20, current_chapter=1000)
    assert item.status == "dormant"


def test_deduplicate_foreshadows_index_semantics():
    """倒排索引去重的合并/独立语义"""
    from backend.utils.text_utils import deduplicate_foreshadows

    clues = [
        (1, "噬血珠佛印减弱，禁制松动", "道具伏笔", "高"),
        (5, "噬血珠佛印进一步减弱", "道具伏笔", "高"),
        (2, "田灵儿御剑飞过青云山", "情节伏笔", "中"),
    ]
    catalog = deduplicate_foreshadows(clues)
    merged = [c for c in catalog if c["merged_count"] == 2]
    single = [c for c in catalog if c["merged_count"] == 1]
    assert len(merged) == 1, f"相似线索应合并，实际: {catalog}"
    assert len(single) == 1, f"无关线索应独立，实际: {catalog}"
