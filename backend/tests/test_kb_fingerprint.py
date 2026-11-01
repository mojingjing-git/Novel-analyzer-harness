"""KB 近邻增量必须校验 ≤best_limit 基线区间的文件指纹——
基线内容被重写后，陈旧 KB 不得固化到新缓存 key（否则会污染最终 knowledge.json）。"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core.knowledge_base import KnowledgeBaseManager


def _write_chapter(d: Path, ch: int, marker: str):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"chapter_{ch}_result.json").write_text(json.dumps({
        "chapter_number": ch,
        "cross_block": {"summary": f"第{ch}章：{marker}", "unresolved_questions": [], "new_leads": []},
        "core_events": [{"event": f"{marker}事件{ch}"}],
        "character_arcs": [], "foreshadowing": [], "plot_holes": [],
        "long_context_insights": {}, "updated_knowledge": {},
    }, ensure_ascii=False), encoding="utf-8")


def test_near_neighbor_rebuilds_when_baseline_content_changed(tmp_path):
    d = tmp_path / "output"
    _write_chapter(d, 1, "旧版第一章")

    kb1 = KnowledgeBaseManager.build_temp_knowledge(d, chapter_limit=1)
    assert any("旧版第一章" in s for s in kb1.recent_summaries)

    # 预热 limit=None 全量缓存条目（模拟预览端点传过不同 limit）
    KnowledgeBaseManager.build_temp_knowledge(d, chapter_limit=None)

    # 重写第 1 章（内容变化 + mtime 前移保证变化可测）
    _write_chapter(d, 1, "新版第一章改写")
    os.utime(d / "chapter_1_result.json",
             ns=(int(__import__('time').time() * 1e9) + 10_000_000,) * 2)

    kb2 = KnowledgeBaseManager.build_temp_knowledge(d, chapter_limit=2)
    _write_chapter(d, 2, "第二章新增")   # 触发近邻：best_limit=1 的缓存放大区间

    kb3 = KnowledgeBaseManager.build_temp_knowledge(d, chapter_limit=2)
    summaries = " ".join(kb3.recent_summaries)
    assert "新版第一章改写" in summaries, "基线被重写后近邻命中不得返回陈旧摘要"
    assert "第二章新增" in summaries


def test_near_neighbor_returns_copy_not_shared_object(tmp_path):
    """无新文件分支不能把同一 KB 对象别名存入新 key。

    白盒断言：近邻产物不得与缓存基线条目共享同一
    可变对象；且变异近邻产物不得污染基线缓存条目。"""
    from backend.core.knowledge_base import _temp_kb_cache

    d = tmp_path / "output"
    _write_chapter(d, 1, "甲")
    KnowledgeBaseManager.build_temp_knowledge(d, chapter_limit=1)
    baseline_entry = _temp_kb_cache[(str(d), 1)]

    kb_a = KnowledgeBaseManager.build_temp_knowledge(d, chapter_limit=None)

    nn_entry = _temp_kb_cache[(str(d), None)]
    assert nn_entry[2] is not baseline_entry[2], \
        "近邻无新文件分支把同一 KB 对象别名存入新 key（别名缺陷）"

    kb_a.recent_summaries.append("污染标记")
    kb_src = KnowledgeBaseManager.build_temp_knowledge(d, chapter_limit=1)
    assert "污染标记" not in " ".join(kb_src.recent_summaries), \
        "变异近邻产物不应污染基线缓存条目"
