"""S0 合成语料夹具（PLAN v2 §S0-5）。

目的：给 S2–S5 的测试提供一份**确定、可解释、覆盖边界**的中文语料，取代"借用真实
工作区"（真实语料既不可提交，也无法断言正确答案）。

覆盖的边界（每一条都对应一项验收场景）：
- ``断号``：第 17 章缺失，后续章节仍连续编号 —— 检验块签名与缺口恢复
- ``多章块``：块按 4 章切分，但第 17 章缺失使块边界出现空洞 —— 检验 source_ref 范围
- ``同章先埋后收``：第 12 章同一章内先埋伏笔再回收 —— 检验"回收位置必须晚于埋设位置"而不是"章号必须更大"
- ``补跑缺口``：第 9 章与第 22 章被标记为"首轮失败、补跑成功" —— 检验乱序合并与摘要缺口
- ``有互动 / 无互动``：第 14 章两名角色**对话**（真关系），第 15 章两名角色**只是同场出现**
  （仅在城东/城西各做各事）—— 检验共现边与语义关系分立
- ``引用歧义``：第 19 章"他又提起那顶铜匣"—— 引用真实存在、词面高度相关，但语义上
  **不是**回收 —— 检验自动裁决不会把它当作已回收

用法::

    from backend.tests.fixtures.vnext_corpus import build_corpus

    corpus = build_corpus(tmp_path)
    corpus.chapter_ids          # 23 个（24 章中缺 17）
    corpus.missing_chapters     # [17]
    corpus.block_ranges         # [(1,4),(5,8),(9,12),(13,16),(18,21),(22,24)]
    corpus.truth.foreshadows    # 伏笔埋设/回收的地面真值

所有文本都是**确定性生成**（无随机、无时间戳），同一版本下逐字节可重现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

TOTAL_CHAPTERS = 24
MISSING_CHAPTERS = (17,)
BLOCK_SIZE = 4

# 地面真值：伏笔（plant -> resolve，位置均落在章内固定段落）
FORESHADOWS = {
    "bronze_case": {
        "plant_chapter": 3,
        "plant_quote": "箱盖内壁刻着一行小字：丙申年冬，勿启。",
        "resolve_chapter": 20,
        "resolve_quote": "铜匣终于打开，里面只有一张泛黄的漕运旧契。",
        "same_chapter": False,
    },
    "jade_seal": {
        "plant_chapter": 8,
        "plant_quote": "她袖中那方玉印，边角缺了一小块。",
        "resolve_chapter": 12,
        "resolve_quote": "缺角的玉印按在供状上，朱砂还未干透。",
        "same_chapter": False,
    },
    "same_chapter_pair": {
        "plant_chapter": 12,
        "plant_quote": "案头那封没署名的信，火漆是少见的青色。",
        "resolve_chapter": 12,
        "resolve_quote": "青漆信封被拆开，落款正是失踪三日的账房。",
        "same_chapter": True,
    },
}

# 引用歧义：真实存在、词面相关，但不构成回收
AMBIGUOUS_QUOTE = "他又提起那顶铜匣，说那不过是寻常旧物。"

# 角色互动地面真值
INTERACTING_PAIR = ("沈砚", "柳七")
CO_PRESENT_ONLY_PAIR = ("柳七", "周嬷嬷")

# 首轮失败、补跑成功的章（供乱序合并用例）
RETRY_CHAPTERS = (9, 22)

_CHAPTER_BODIES: Dict[int, List[str]] = {
    1: ["永乐元年春，北平城的雪还没化尽。", "沈砚背着半旧的包袱进了城门。"],
    3: ["库房里堆着前任留下的杂物。", FORESHADOWS["bronze_case"]["plant_quote"]],
    8: ["柳七从后堂出来，福了一福。", FORESHADOWS["jade_seal"]["plant_quote"]],
    9: ["城西的粮价一日三涨。", "沈砚在米铺门口站了很久。"],
    12: [
        FORESHADOWS["same_chapter_pair"]["plant_quote"],
        "夜里起了风，廊下的灯晃个不停。",
        FORESHADOWS["jade_seal"]["resolve_quote"],
        FORESHADOWS["same_chapter_pair"]["resolve_quote"],
    ],
    14: [
        "沈砚把账册推到桌上。",
        "「这笔漕粮对不上。」他说。",
        "柳七想了想，回道：「上个月换过秤。」",
        "两人就着灯把三十七笔账从头核了一遍。",
    ],
    15: [
        "柳七在城东的布庄盘货。",
        "周嬷嬷在城西的宅子里清点箱笼。",
        "这一天谁也没有见着谁。",
    ],
    19: [AMBIGUOUS_QUOTE],
    20: ["地窖的门被撬开。", FORESHADOWS["bronze_case"]["resolve_quote"]],
    22: ["周嬷嬷递上一份旧名册。", "沈砚翻到第三页，停住了。"],
}


@dataclass
class CorpusTruth:
    foreshadows: dict = field(default_factory=lambda: dict(FORESHADOWS))
    ambiguous_quote: str = AMBIGUOUS_QUOTE
    interacting_pair: Tuple[str, str] = INTERACTING_PAIR
    co_present_only_pair: Tuple[str, str] = CO_PRESENT_ONLY_PAIR
    retry_chapters: Tuple[int, ...] = RETRY_CHAPTERS


@dataclass
class Corpus:
    root: Path
    chapter_ids: List[str]
    missing_chapters: List[int]
    block_ranges: List[Tuple[int, int]]
    truth: CorpusTruth

    def chapter_text(self, chapter: int) -> str:
        chapter_id = self.chapter_ids[self._index(chapter)]
        return (self.root / "blocks" / f"{chapter_id}.txt").read_text(encoding="utf-8")

    def _index(self, chapter: int) -> int:
        numbers = [int(cid.split("_")[1]) for cid in self.chapter_ids]
        return numbers.index(chapter)


def _chapter_text(number: int) -> str:
    lines = [f"第{number}章　{'风雪' if number % 2 else '晴昼'}"]
    lines.extend(_CHAPTER_BODIES.get(number, [f"这一日无事，只把旧卷翻过一遍（第{number}章）。"]))
    # 填充到稳定长度，使"长章"路径可被覆盖，同时保持确定性
    lines.append("　" + "此间琐事，不足为外人道也。" * (3 + number % 5))
    return "\n".join(lines) + "\n"


def build_corpus(root: Path) -> Corpus:
    """在 ``root`` 下重建语料，返回描述对象。重复调用会覆盖同名文件。"""
    root = Path(root)
    chapters_dir = root / "blocks"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    chapter_ids: List[str] = []
    for number in range(1, TOTAL_CHAPTERS + 1):
        if number in MISSING_CHAPTERS:
            continue
        chapter_id = f"ch_{number:04d}"
        (chapters_dir / f"{chapter_id}.txt").write_text(_chapter_text(number), encoding="utf-8")
        chapter_ids.append(chapter_id)

    present = [int(cid.split("_")[1]) for cid in chapter_ids]
    block_ranges: List[Tuple[int, int]] = []
    for start in present[::BLOCK_SIZE]:
        window = [c for c in present if start <= c < start + BLOCK_SIZE]
        block_ranges.append((window[0], window[-1]))

    return Corpus(
        root=root,
        chapter_ids=chapter_ids,
        missing_chapters=list(MISSING_CHAPTERS),
        block_ranges=block_ranges,
        truth=CorpusTruth(),
    )
