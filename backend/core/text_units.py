"""2.0 的"文本单元"：加载、定位、展示标签。

## 为什么要有这个模块

阶段 0-2 的核心结论：**正文身份不能是章号**。实测（《大奉打更人》922 文件 / 434.9 万字）：

- 采集侧记的 ``anchor_chapter`` 是"合并块内按标题反查出的回号"（12、13、14、15）；
- 复检侧按单文件切分得到的是**文件序号**（1..922）；
- 两套章号空间不对齐 → 按记录章号定位只有 22%；连事后复现合并块口径也只有 31%。

所以 2.0 把检索单元降级为**物理文件**：``{文件序号: 原文}``。文件序号唯一、
文件内容是原文，取窗与检索都不再依赖"这本书怎么分章"。

## 三条硬约束

1. **绝不触发 ``difflib``**。``services/recheck_resolution.py`` 的 ``locate_quote`` 在找不到时会回退到
   ``core/citations.py:174`` 的 ``difflib.SequenceMatcher``，那是 $O(N\\cdot M)$——
   拿 435 万字符的全书去跑，单次就能卡住几十秒到几分钟。本模块只做**纯字面匹配**
   （``str.find``，Boyer-Moore-Horspool，C 实现）。
2. **加载口径只有一处**。生产路径（``final_summary._collect_chapters``）与测量脚本
   （``scripts/measure_recheck_v2_pipeline.py``）都调 ``load_blocks_as_files``，
   避免"字典序 1,10,2 vs 自然序"和"要不要忽略非数字 txt"这类双口径漂移。
3. **展示标签与身份分离**。文件序号是身份，回号（"第 12 回"）是给人看的；
   两者都要出现在提示词里，否则模型会看到"首次出现：第12章"配"【文件366】"而困惑。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

#: 章节标题（用于从文件内解析回号，**仅作展示**）
_TITLE_RE = re.compile(r"^\s*第\s*([0-9零〇一二三四五六七八九十百千万两]+)\s*[章回节]", re.MULTILINE)


class LocateHit(tuple):
    """一次定位的结果：命中单元、单元内偏移、命中方式。

    **刻意不是三字段 NamedTuple**：既有调用方写的是 ``owner, _offset = hit``，
    三元素元组解包会直接 ``ValueError: too many values to unpack``（AGY 复审 P1-2）。
    所以这里做成"解包只给两项 + 命中方式作为额外属性"的元组子类：

    - ``owner, offset = hit`` —— 照旧能用；
    - ``hit.unit`` / ``hit.offset`` / ``hit.mode`` —— 属性访问。
    """

    __slots__ = ()

    def __new__(cls, unit: int | None, offset: int | None, mode: str = "absent"):
        return super().__new__(cls, (unit, offset, mode))

    def __iter__(self):
        # 只暴露两项：解包契约按旧形状走
        return iter((self[0], self[1]))

    @property
    def unit(self) -> int | None:
        return self[0]

    @property
    def offset(self) -> int | None:
        return self[1]

    @property
    def mode(self) -> str:
        return self[2]


def load_blocks_as_files(blocks_dir: Path) -> tuple[dict[int, str], list[str]]:
    """按**自然数序**读 blocks 目录 → ``{文件序号: 原文}``。

    只收 ``<纯数字>.txt``；其它 txt（如 ``split_report.txt``——它不是正文，
    却曾被当成第 922 章参与分析与复检）被排除并**显式报告**。

    键用**文件名里的数字**（``10.txt`` → 10），不是排序后的序号：这样
    "第 10 个文件"和"第 10 段正文"是同一件事，且中间插入文件不会让后面全部移位。
    """
    units: dict[int, str] = {}
    excluded: list[str] = []
    if not blocks_dir or not Path(blocks_dir).is_dir():
        return units, excluded
    for path in sorted(Path(blocks_dir).glob("*.txt")):
        if not path.stem.isdigit():
            excluded.append(path.name)
            continue
        try:
            units[int(path.stem)] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            excluded.append(path.name)
    return units, excluded


def find_literal(text: str, needle: str) -> int | None:
    """纯字面定位：找不到就是找不到，**不做模糊回退**。

    这里必须与 ``recheck_resolution.locate_quote`` 分道扬镳——后者找不到时会
    回退 ``difflib``，在全书规模上不可用。
    """
    if not text or not needle:
        return None
    pos = text.find(needle)
    # `str.find` 没找到返回 **-1**，不是 None——不转的话调用方的 `is not None`
    # 判断永远成立，会把"没找到"当成"在第 0 个单元命中"。
    return pos if pos >= 0 else None


def locate_in_units(
    units: Mapping[int, str],
    quote: str,
    *,
    prefer: int | None = None,
) -> tuple[int, int] | None:
    """在单元集合里定位引文 → ``(单元键, 偏移)``。

    - ``prefer`` 指定的单元先试（快路径）；
    - 否则按键升序扫全书，取**最早**出现——伏笔埋设在前面，全书首次出现就是埋设处。

    只做字面匹配，因此**不会**因为引文与原文有细微差异而"卡住"。
    """
    q = (quote or "").strip()
    if not q or not units:
        return None
    if prefer is not None and prefer in units:
        pos = find_literal(units[prefer], q)
        if pos is not None:
            return prefer, pos
    for key in sorted(units):
        if key == prefer:
            continue
        pos = find_literal(units[key], q)
        if pos is not None:
            return key, pos
    return None


def locate_with_mode(
    units: Mapping[int, str],
    quote: str,
    *,
    candidate_range: tuple[int, int] | None = None,
    display_labels: Mapping[int, str] | None = None,
    anchor_round: int | None = None,
) -> LocateHit:
    """在**候选范围**内定位引文，并报告命中方式。

    候选范围 = 物理文件序号区间 ``[start, end]``。采集是 ``block_size`` 章合并分析，
    一条伏笔的引文只能落在产生它的那一块里（实测 1669/1669 = 100% 落在
    ``[N, N+3]`` 内，范围外 0 条）。**范围是唯一的**——因此不会出现"同回号多文件
    按升序取第一个"那种跨卷截胡。

    命中方式：

    - ``range``：在候选范围内命中；
    - ``fallback``：范围内没命中，退到全书最早出现（跨卷错挂风险高，应视为低置信度）；
    - ``absent``：全书都没有。

    范围内**多处**命中时：优先取回号等于 ``anchor_round`` 的那个文件；
    无法消歧就取范围内**首个**命中——**绝不跳出范围去兜底**（跳出去就会重演跨卷错挂）。
    """
    q = (quote or "").strip()
    if not q or not units:
        return LocateHit(None, None, "absent")

    if candidate_range is not None:
        start, end = candidate_range
        hits = [k for k in sorted(units) if start <= k <= end and find_literal(units[k], q) is not None]
        if hits:
            if len(hits) == 1:
                return LocateHit(hits[0], find_literal(units[hits[0]], q), "range")
            if anchor_round:
                for k in hits:
                    disp = (display_labels or {}).get(k) or ""
                    if disp.replace("第", "").replace("回", "").strip() == str(anchor_round):
                        return LocateHit(k, find_literal(units[k], q), "range")
            return LocateHit(hits[0], find_literal(units[hits[0]], q), "range")

    for key in sorted(units):
        pos = find_literal(units[key], q)
        if pos is not None:
            return LocateHit(key, pos, "fallback")
    return LocateHit(None, None, "absent")


def build_display_labels(units: Mapping[int, str]) -> dict[int, str]:
    """单元 → 回号标签（如 ``"第 12 回"``），只用于展示。

    回号转成**阿拉伯数字**：候选元数据里写的是"首次出现：第12章"，证据标签若写
    "第 十二 回"就是又一套写法，模型会以为编号对不上（AGY 复审 P1-3）。
    解析不出标题的单元不出现��映射里（调用方显示成"文件 N"）。
    """
    from backend.core.chapter_split import cn_to_int

    labels: dict[int, str] = {}
    for key, text in units.items():
        m = _TITLE_RE.search(text or "")
        if not m:
            continue
        number = cn_to_int(m.group(1))
        if not number:
            continue
        labels[key] = f"第 {number} 回"
    return labels


def unit_label(unit: int, display: Mapping[int, str] | None = None, *, noun: str = "文件") -> str:
    """单元的展示标签：``文件366（第12回）`` / ``第 12 章``。

    ``noun="章"`` 是 1.x 的旧格式（``第 12 章``），保持不变；
    ``noun="文件"`` 是 2.0 的格式，把**物理文件序号**与**文学回号**同时给出，
    避免模型把两个数字体系搞混。
    """
    disp = (display or {}).get(unit) or ""
    if noun == "章":
        base = f"第 {unit} 章"
    else:
        base = f"文件{unit}"
    if disp:
        base = f"{base}（{disp}）"
    return base
