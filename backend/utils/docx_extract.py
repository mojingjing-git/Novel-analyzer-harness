"""DOCX 正文抽取（纯标准库：zipfile + xml.etree）。

存在理由
--------
切章器（`backend/services/splitter_service.py`）的 15 条章正则全部带 `^` 行首锚点，
并配套 `0 < len(s) <= 80` 的整行长度闸门（`splitter_service.py:822`）。这两道约束
赖以成立的前提是**「一行一个语义单元」**——而 DOCX 里根本不存在换行符：

- DOCX 的原子单位是 `<w:p>`（段落），段落之间在渲染上是分隔的，但文本流里没有 `\\n`
- 一个 `<w:p>` 内部还可以用 `<w:br/>` 渲染出多个视觉行

因此本模块的职责不是"重新发明切章规则"，而是**忠实重建 txt 那样"一行一个语义单元"
的输入形态**，让下游 1243 行切章逻辑以为自己还在吃 txt。

设计要点
--------
1. `<w:br/>` 转换行（恢复段内换行），`<w:tab/>` 转空格
2. **一个 `<w:p>` 输出一行**：逐段落输出时标题天然已独占一行，
   因此**不再额外注入空行**。注入空行会让 DOCX 与 TXT 的行结构不等价——多出的空行
   会被 split_text 保留进 blocks/NNNN.txt，而下游 style_analyzer 按「第 offset 个
   汉字」定位采样窗口（style_analyzer.py:283），偏移被推后会导致风格采样错位。
   _is_heading 因此只剩一个职责：当「标题 + 正文」共存于**同一个 `<w:p>`**
   时把它们劈成两行，避免标题被折进超长行而遭 len(s) <= 80 闸门判负。
3. 递归遍历 w:p 而非只取 w:body 直接子节点——网页转制的小说 DOCX 常把正文塞进单
   单元格表格，只取直接子节点会抽到空文本。
4. 异常一律转 ValueError（routes_splitter 把 ValueError 映射为 4xx；其余异常会被
   except Exception 吞成 500）。注意加密 docx 抛的是 RuntimeError 而非 BadZipFile。
"""

import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# WordprocessingML 主命名空间。不要用前缀硬编码，不同生成器写出的前缀可能不同。
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _qn(tag: str) -> str:
    """把本地标签名转成带命名空间的 Clark 记法（如 'p' → '{...}p'）。"""
    return f"{_W_NS}{tag}"


# Word 会产出 Heading 1/2/3/4...、Title、Subtitle 等，用正则覆盖数字后缀，
# 而不是只比对几个字面量。
_HEADING_STYLE_RE = re.compile(r"^(heading\s*\d*|title|subtitle)$", re.IGNORECASE)

_ZIP_MAGIC = b"PK\x03\x04"


def _is_heading(paragraph: ET.Element) -> bool:
    """段落是否为 heading/title 样式。命中则需要强制独占一行。"""
    ppr = paragraph.find(_qn("pPr"))
    if ppr is None:
        return False
    pstyle = ppr.find(_qn("pStyle"))
    if pstyle is None:
        return False
    val = (pstyle.get(_qn("val")) or "").strip()
    return bool(val) and _HEADING_STYLE_RE.match(val) is not None


def _iter_runs(paragraph: ET.Element):
    """按文档序产出段落内的节点，涵盖嵌套结构（hyperlink 内的 run 等）。"""
    return paragraph.iter()


def _paragraph_text(paragraph: ET.Element) -> str:
    """抽取单个 `<w:p>` 的文本内容。
    - `w:t`  → 文本（保留原始空格，XML 里的 xml:space="preserve" 已由 ElementTree 处理）
    - `w:tab` → 单个空格
    - `w:br`  → 换行符（恢复段内视觉换行）
    """
    buf: list[str] = []
    for node in _iter_runs(paragraph):
        tag = node.tag
        if tag == _qn("t"):
            buf.append(node.text or "")
        elif tag == _qn("tab"):
            buf.append(" ")
        elif tag == _qn("br"):
            buf.append("\n")
    return "".join(buf)


def _extract_paragraphs(root: ET.Element) -> list[tuple[str, bool]]:
    """产出 (段落文本, 是否 heading) 列表。递归遍历以覆盖表格内正文。"""
    body = root.find(_qn("body"))
    if body is None:
        return []
    out: list[tuple[str, bool]] = []
    # 用 iter 而非直接子节点遍历：网页转制的 DOCX 常把整章正文放进单单元格表格，
    # 只取 body 的直接子节点会漏得一干二净。文档序一般即阅读序。
    for para in body.iter(_qn("p")):
        # 段落自身的文本用逐节点抽取；若外层段落内含嵌套 w:p（文本框场景），
        # 跳过内层（由外层统一产出），避免同一段重复计入。
        out.append((_paragraph_text(para), _is_heading(para)))
    return out


def _read_document_xml(file_path: Path) -> bytes:
    """打开 docx 并读出 word/document.xml，异常按类型分诊。"""
    # 先校验 ZIP magic，区分「根本不是 zip」与「zip 但损坏」，两者的用户提示不同。
    try:
        with open(file_path, "rb") as fh:
            magic = fh.read(4)
    except OSError as exc:
        raise ValueError(f"无法读取文件: {exc}") from exc

    if magic != _ZIP_MAGIC:
        raise ValueError("不是有效的 DOCX 文件（未检测到 ZIP 头，可能是旧版 .doc 或文件已损坏）")

    try:
        with zipfile.ZipFile(file_path) as zf:
            try:
                return zf.read("word/document.xml")
            except KeyError as exc:
                raise ValueError("不是有效的 DOCX 文件（缺少 word/document.xml）") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("DOCX 文件已损坏（无法解压）") from exc
    except RuntimeError as exc:
        # 加密的 docx：zipfile 在读成员时抛 RuntimeError（password required），
        # 这不是 BadZipFile，漏掉这一分支会让用户看到 500 而非明确提示。
        msg = str(exc).lower()
        if "password" in msg or "encrypted" in msg:
            raise ValueError("DOCX 文档已加密，无法读取（请先去除密码保护）") from exc
        raise ValueError(f"DOCX 解压失败: {exc}") from exc


def extract_docx_text(file_path: Path) -> str:
    """把 .docx 转成纯文本，形态对齐 txt（每行一个语义单元）。

    Args:
        file_path: .docx 文件路径

    Returns:
        纯文本，换行符统一为 `\n`。

    Raises:
        ValueError: 文件损坏 / 加密 / 非 DOCX / 内容为空。
    """
    raw = _read_document_xml(file_path)
    if not raw:
        raise ValueError("DOCX 内容为空（word/document.xml 无数据）")

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"DOCX 内部结构无法解析: {exc}") from exc

    paragraphs = _extract_paragraphs(root)

    lines: list[str] = []
    for text, _heading in paragraphs:
        # 一个段落一行。标题段落本就独占一个 <w:p>，无需再插空行——插入空行会
        # 让 DOCX 与 TXT 行结构不等价，多出的空行会进 blocks/NNNN.txt 并把
        # style_analyzer 的采样偏移推后。
        lines.append(text.strip())

    content = "\n".join(lines)

    if not content.strip():
        raise ValueError("DOCX 未抽取到任何正文内容（可能是空文档或内容全在不支持的元素里）")

    return content
