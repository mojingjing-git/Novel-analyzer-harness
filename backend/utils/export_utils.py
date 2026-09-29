"""
导出工具
将分析结果导出为各种格式
"""

from pathlib import Path
from typing import List

from ..models.analysis_result import AnalysisResult


def _format_chapter_list(chapter_nums: List[int]) -> str:
    """
    Format a list of chapter numbers for human-readable display.

    Deduplicates and sorts. Returns "第3章, 第5章, 第7章" format.
    Empty input returns empty string.

    Args:
        chapter_nums: List of chapter numbers (may contain duplicates, may be unsorted).

    Returns:
        Comma-separated string like "第3章, 第5章, 第7章"; empty string for empty input.
    """
    if not chapter_nums:
        return ""
    return ', '.join(f"第{n}章" for n in sorted(set(chapter_nums)))

def export_summary_report(all_results: List[AnalysisResult], output_dir: Path) -> Path:
    """
    生成汇总报告

    Args:
        all_results: 所有章节的分析结果列表
        output_dir: 输出目录

    Returns:
        生成的文件路径
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / "analysis_summary.md"

    content = "# 小说分析汇总报告\n\n"
    content += f"**总章节数**: {len(all_results)}\n\n"

    # 章节目录
    content += "## 章节目录\n\n"
    content += "| 章节 | 核心事件数 | 人物弧光数 | 伏笔数 | 逻辑漏洞 |\n"
    content += "|------|-----------|-----------|--------|----------|\n"

    for result in all_results:
        event_count = len(result.core_events)
        arc_count = len(result.character_arcs)
        foreshadow_count = len(result.foreshadowing)
        hole_count = len([h for h in result.plot_holes if str(h) != "无明显逻辑漏洞"])

        content += f"| 第{result.chapter_number}章 | {event_count} | {arc_count} | {foreshadow_count} | {hole_count} |\n"

    content += "\n"

    # 角色统计
    content += "## 角色出现统计\n\n"
    character_appearances = {}
    for result in all_results:
        for event in result.core_events:
            raw_chars = event.characters
            if isinstance(raw_chars, list):
                raw_chars = ", ".join(str(c) for c in raw_chars)
            chars = [c.strip() for c in str(raw_chars).replace("，", ",").split(',') if c.strip()]
            for char in chars:
                if char not in character_appearances:
                    character_appearances[char] = []
                character_appearances[char].append(result.chapter_number)

    for char, chapters in sorted(character_appearances.items()):
        chapter_text = _format_chapter_list(chapters)
        content += f"- **{char}**: 出现在{chapter_text}\n"

    content += "\n"

    # 伏笔汇总
    content += "## 伏笔汇总\n\n"
    all_foreshadows = []
    for result in all_results:
        for foreshadow in result.foreshadowing:
            all_foreshadows.append((result.chapter_number, foreshadow))

    if all_foreshadows:
        for chapter, foreshadow in all_foreshadows:
            content += f"- **[第{chapter}章]** [{foreshadow.confidence}] {foreshadow.clue}\n"
            content += f"  - 暗示: {foreshadow.implication}\n\n"
    else:
        content += "无明显伏笔\n\n"

    # 世界观元素
    content += "## 世界观元素\n\n"
    world_elements = set()
    for result in all_results:
        for element in result.updated_knowledge.world_building:
            world_elements.add(element)

    if world_elements:
        for element in sorted(world_elements):
            content += f"- {element}\n"
    else:
        content += "无明显世界观元素\n"

    content += "\n"

    # 主题元素
    content += "## 主题元素\n\n"
    themes = set()
    for result in all_results:
        for t in result.long_context_insights.thematic_elements:
            themes.add(t)
    if themes:
        for t in sorted(themes):
            content += f"- {t}\n"
    else:
        content += "无\n"
    content += "\n"

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

    return filepath
