"""
Excel导出工具
将聚合分析结果导出为Excel工作簿（每类数据一个Sheet）
"""

import json
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_RISKY_PREFIXES = ("=", "+", "-", "@")


def _safe_cell_value(val) -> str:
    """Excel 公式注入消毒：以 = + - @ 开头的文本前置单引号强制按文本存储。

    P2 修复（2026-08-24）：LLM 产出的分析文本可能形如 =cmd|'/c calc'!A1，
    openpyxl 原样写入会被 Excel 当公式求值（DDE 注入面）。负数显示不受影响
    （前导撇号被 Excel 隐藏，值为文本 "-5"）。"""
    s = str(val) if val is not None else ""
    if s.startswith(_RISKY_PREFIXES):
        return "'" + s
    return s


def export_aggregated_to_excel(aggregated_dir: Path, output_path: Path = None) -> Path:
    """
    将聚合目录下的所有JSON导出为Excel

    Args:
        aggregated_dir: 聚合JSON所在目录
        output_path: 输出Excel路径，默认 aggregated_dir/analysis_data.xlsx

    Returns:
        生成的Excel文件路径
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    if output_path is None:
        output_path = aggregated_dir / "analysis_data.xlsx"

    if not aggregated_dir.exists() or not any(aggregated_dir.glob("*.json")):
        raise FileNotFoundError(f"聚合目录为空或不存在: {aggregated_dir}")

    wb = Workbook()
    wb.remove(wb.active)

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell_align = Alignment(vertical="top", wrap_text=True)

    def _write_sheet(ws, headers: list, rows: list):
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
        for r, row in enumerate(rows, 2):
            for c, val in enumerate(row, 1):
                cell = ws.cell(row=r, column=c, value=_safe_cell_value(val))
                cell.alignment = cell_align

    def _load_json(name: str) -> Optional[dict]:
        fp = aggregated_dir / name
        if fp.exists():
            with open(fp, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None

    # 1. 核心事件
    data = _load_json("core_events_aggregated.json")
    if data:
        ws = wb.create_sheet("核心事件")
        rows = []
        for ch_key, events in data.items():
            ch = ch_key.replace("chapter_", "")
            for ev in events:
                rows.append([ch, ev.get("id", ""), ev.get("event", ""),
                            ev.get("characters", ""), ev.get("function", "")])
        _write_sheet(ws, ["章节", "事件ID", "事件", "角色", "功能"], rows)

    # 2. 人物弧光
    data = _load_json("character_arcs_aggregated.json")
    if data:
        ws = wb.create_sheet("人物弧光")
        rows = []
        for ch_key, arcs in data.items():
            ch = ch_key.replace("chapter_", "")
            for arc in arcs:
                rows.append([ch, arc.get("name", ""), arc.get("surface_action", ""),
                            arc.get("inner_motivation", ""), arc.get("change_delta", ""),
                            arc.get("driver", "")])
        _write_sheet(ws, ["章节", "角色", "表面行为", "深层动机", "变化量", "驱动事件"], rows)

    # 3. 伏笔
    data = _load_json("foreshadowing_aggregated.json")
    if data:
        ws = wb.create_sheet("伏笔")
        rows = []
        for ch_key, fores in data.items():
            ch = ch_key.replace("chapter_", "")
            for fs in fores:
                rows.append([ch, fs.get("clue", ""), fs.get("type", ""),
                            fs.get("implication", ""), fs.get("confidence", "")])
        _write_sheet(ws, ["章节", "线索", "类型", "暗示", "置信度"], rows)

    # 4. 角色追踪
    data = _load_json("character_tracking_aggregated.json")
    if data:
        ws = wb.create_sheet("角色追踪")
        rows = []
        for char_name, info in data.items():
            chapters = info.get("chapters", [])
            rows.append([char_name, info.get("first_appearance", ""),
                        len(chapters), info.get("total_events", 0),
                        _format_chapter_range(chapters)])
        _write_sheet(ws, ["角色", "首次出现", "出场章节数", "参与事件数", "出场范围"], rows)

    # 5. 世界观
    data = _load_json("world_building_aggregated.json")
    if data:
        ws = wb.create_sheet("世界观")
        rows = []
        for timeline in data.get("timeline", []):
            rows.append(["时间线", timeline.get("chapter", ""), timeline.get("timeline", "")])
        for elem in data.get("elements", []):
            rows.append(["元素", elem.get("first_appearance", ""), str(elem.get("element", ""))])
        _write_sheet(ws, ["类型", "章节", "内容"], rows)

    # 6. 未解决问题
    data = _load_json("unresolved_questions_aggregated.json")
    if data:
        ws = wb.create_sheet("未解决问题")
        rows = []
        for ch_key, questions in data.items():
            ch = ch_key.replace("chapter_", "")
            for q in (questions if isinstance(questions, list) else [questions]):
                rows.append([ch, str(q) if q else ""])
        _write_sheet(ws, ["章节", "问题"], rows)

    # 7. 新线索
    data = _load_json("new_leads_aggregated.json")
    if data:
        ws = wb.create_sheet("新线索")
        rows = []
        for ch_key, leads in data.items():
            ch = ch_key.replace("chapter_", "")
            for lead in (leads if isinstance(leads, list) else [leads]):
                rows.append([ch, str(lead) if lead else ""])
        _write_sheet(ws, ["章节", "线索"], rows)

    # 8. 长期弧光
    data = _load_json("long_term_arcs_aggregated.json")
    if data:
        ws = wb.create_sheet("长期弧光")
        rows = []
        for arc in data.get("long_term_arcs", []):
            rows.append([arc.get("first_mentioned", ""), str(arc.get("arc", ""))])
        _write_sheet(ws, ["首次出现", "弧光内容"], rows)

    # 9. 逻辑漏洞
    data = _load_json("plot_holes_aggregated.json")
    if data:
        ws = wb.create_sheet("逻辑漏洞")
        rows = []
        for hole in data.get("plot_holes", []):
            rows.append([hole.get("chapter", ""), hole.get("hole", "")])
        _write_sheet(ws, ["章节", "漏洞"], rows)

    # 10. 主题元素
    data = _load_json("long_context_insights_aggregated.json")
    if data:
        ws = wb.create_sheet("主题元素")
        rows = []
        for t in data.get("thematic_elements", []):
            rows.append([t.get("first_mentioned", ""), str(t.get("element", ""))])
        _write_sheet(ws, ["首次出现", "主题"], rows)

        # 角色关系
        rels = data.get("character_relationships", {})
        if rels:
            ws2 = wb.create_sheet("角色关系")
            rel_rows = []
            for pair, entries in rels.items():
                if isinstance(entries, list):
                    for e in entries:
                        rel_rows.append([pair, e.get("chapter", ""), e.get("description", "")])
                else:
                    rel_rows.append([pair, "", str(entries)])
            _write_sheet(ws2, ["关系对", "章节", "描述"], rel_rows)

    # 调整列宽
    for ws in wb.worksheets:
        for col in ws.columns:
            max_len = 0
            for cell in col:
                if cell.value:
                    max_len = max(max_len, min(len(str(cell.value)), 60))
            ws.column_dimensions[col[0].column_letter].width = max_len + 2

    if not wb.worksheets:
        # P2 修复：目录里只有非识别名 JSON 时，11 个 _load_json 全 None，
        # 删默认 sheet 后零工作表 → wb.save 抛 IndexError 接口 500；改为明确报错。
        raise ValueError(f"聚合目录缺少可识别的分析 JSON 文件，无法导出 Excel: {aggregated_dir}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    logger.info(f"Excel已导出: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")
    return output_path




def _format_chapter_range(chapters: List[int]) -> str:
    """格式化章节列表为可读范围，如 '1-5, 8, 10-15'"""
    if not chapters:
        return ""
    chapters = sorted(set(chapters))
    ranges = []
    start = chapters[0]
    end = chapters[0]
    for ch in chapters[1:]:
        if ch == end + 1:
            end = ch
        else:
            ranges.append((start, end))
            start = end = ch
    ranges.append((start, end))
    parts = []
    for s, e in ranges:
        if s == e:
            parts.append(str(s))
        else:
            parts.append(f"{s}-{e}")
    return ", ".join(parts)
