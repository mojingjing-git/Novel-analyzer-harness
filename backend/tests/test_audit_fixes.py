"""
审计项测试
- routes_prompt: read_block 传章号列表 + chapter_limit 用真实章号
- memory_state: flush_to_disk 迭代 _failed_chapters 快照（并发插入不会崩）
- memory_state: 增量 KB 乱序合并不覆盖 timeline
- settings: env API key 不回写 config.json
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.models.analysis_result import AnalysisResult
from backend.core.memory_state import MemoryState
from backend.utils.json_utils import safe_save_json


def make_result(ch: int, timeline: str, summary: str) -> AnalysisResult:
    return AnalysisResult.from_dict({
        "chapter_number": ch,
        "core_events": [],
        "character_arcs": [],
        "foreshadowing": [],
        "plot_holes": [],
        "locations": [],
        "spatial_relationships": [],
        "cross_block": {"summary": summary, "unresolved_questions": [], "new_leads": [], "contextual_link": ""},
        "updated_knowledge": {"timeline": timeline, "world_building": []},
        "long_context_insights": {},
    })


# ---------- #2: flush_to_disk 并发插入失败键 ----------

async def test_flush_failed_chapters_snapshot(tmp_path, monkeypatch):
    """flush_to_disk 对 _failed_chapters 做快照迭代：并发插入不中断"""
    state = MemoryState()
    state.add_failed(1, "失败原因1")

    calls = {"n": 0}
    orig = safe_save_json

    def fake_save(data, path):
        calls["n"] += 1
        if calls["n"] == 1:
            state.add_failed(99, "并发插入")  # 模拟 worker 在 flush 期间 add_failed
        return orig(data, path)

    monkeypatch.setattr("backend.core.memory_state.safe_save_json", fake_save)
    await state.flush_to_disk(tmp_path)  # 不应抛异常（并发插入时 dict 尺寸变化）
    assert (tmp_path / "chapter_1_failed.json").exists()
    # 并发插入的新键本次 flush 不写入（快照语义），下一次 flush 补写
    await state.flush_to_disk(tmp_path)
    assert (tmp_path / "chapter_99_failed.json").exists()


# ---------- #6: 乱序合并不覆盖 timeline ----------

async def test_kb_merge_out_of_order_timeline():
    """章号 5 完成后章号 3 迟到：timeline/摘要不被倒灌"""
    state = MemoryState()
    await state.add_result(make_result(5, "第5章时间线", "第5章摘要"))
    await state.add_result(make_result(3, "第3章时间线", "第3章摘要"))  # 乱序迟到

    kb = state.get_kb_snapshot()  # 全量快照
    assert kb.story_timeline == "第5章时间线"  # 不被第3章覆盖
    assert "第5章摘要" in kb.recent_summaries
    assert "第3章摘要" not in kb.recent_summaries  # 迟到的进度类内容不并入
    # 但非进度类内容（world_building 等）仍并入
    assert state._kb_last_chapter == 5


# ---------- #8: env key 不回写 ----------

def test_save_skips_env_key(tmp_path, monkeypatch):
    from backend.config.settings import ConfigManager
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"api": {"api_key": "disk-key", "model": "m"}}), encoding="utf-8")
    monkeypatch.setenv("LLM_API_KEY", "env-secret")

    mgr = ConfigManager(cfg_path)
    cfg = mgr.load()
    assert cfg.api.api_key == "env-secret"  # env 覆盖
    assert mgr.save()
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["api"]["api_key"] == ""  # 不回写明文


def test_save_keeps_disk_key_without_env(tmp_path, monkeypatch):
    from backend.config.settings import ConfigManager
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"api": {"api_key": "disk-key", "model": "m"}}), encoding="utf-8")

    mgr = ConfigManager(cfg_path)
    mgr.load()
    assert mgr.save()
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["api"]["api_key"] == "disk-key"  # 非 env 来源照常写回


# ---------- #1: routes_prompt 章号列表调用 ----------

async def test_prompt_preview_read_block_list(tmp_path, monkeypatch):
    """prompt 预览：read_block 收到章号列表，chapter_limit 用真实章号"""
    import backend.api.routes_prompt as rp
    import backend.services.prompt_preview as pp
    from backend.config.settings import AppConfig

    captured = {"chapters": None, "chapter_limit": None}
    fs_bag = {}

    class FakeFP:
        def __init__(self, *a, **k):
            pass

        def scan_chapters(self):
            return [1, 2]

        def read_block(self, chapters):
            captured["chapters"] = chapters
            return "块内容"

    cfg = AppConfig()
    cfg.analysis.block_size = 2
    (tmp_path / "blocks").mkdir(exist_ok=True)  # 路由要求 blocks 目录存在

    # 业务编排下沉在 backend/services/prompt_preview.py，桩点随之
    monkeypatch.setattr(pp, "FileProcessor", FakeFP)
    monkeypatch.setattr(pp, "get_book_path", lambda bid: tmp_path)
    monkeypatch.setattr(pp, "get_output_dir", lambda bid: tmp_path / "output")

    def fake_config_load(self):
        return cfg

    monkeypatch.setattr(pp.ConfigManager, "load", fake_config_load)

    def fake_build_temp_knowledge(output_dir, chapter_limit=None):
        captured["chapter_limit"] = chapter_limit
        from backend.models.knowledge import KnowledgeBase
        return KnowledgeBase()

    monkeypatch.setattr(pp.KnowledgeBaseManager, "build_temp_knowledge", fake_build_temp_knowledge)

    req = rp.PromptPreviewRequest(book_id="test", chapter=1, max_chars=0)
    result = await rp.prompt_preview(req)

    assert captured["chapters"] == [1, 2]  # 章号列表而非 (ch, bs)
    assert captured["chapter_limit"] == 0  # 第一块之前无知识
    assert "块内容" in result["user_prompt"]


def run_all_sync():
    asyncio.run(test_flush_failed_chapters_snapshot(Path("."), None))
    asyncio.run(test_kb_merge_out_of_order_timeline())
    test_save_skips_env_key(Path("."), None)
    test_save_keeps_disk_key_without_env(Path("."), None)
