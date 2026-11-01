"""章节正文必须有上限——超窗是确定性失败，白烧全部重试预算"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core.prompt_builder import PromptBuilder
from backend.models.knowledge import KnowledgeBase


def test_long_content_truncated_with_tail_kept():
    pb = PromptBuilder(max_content_chars=500)
    msgs = pb.build_messages("头" + "中" * 600 + "尾结局", 7, KnowledgeBase())
    user = msgs[1]["content"]
    assert pb_last_body(user) <= 600
    assert "已截去开头" in user
    assert user.rstrip().endswith("尾结局") or "尾结局" in user[-200:]
    assert "头" * 50 not in user[-300:]  # 开头确实被丢


def test_short_content_untouched():
    pb = PromptBuilder(max_content_chars=500)
    msgs = pb.build_messages("短正文" * 10, 1, KnowledgeBase())
    assert "已截去开头" not in msgs[1]["content"]
    assert "短正文" in msgs[1]["content"]


def pb_last_body(user_prompt: str) -> int:
    """取 [当前分析文本] 段之后的正文长度（粗略：最后一个标记之后）"""
    marker = "[当前分析文本"
    idx = user_prompt.rfind(marker)
    return len(user_prompt[idx:])
