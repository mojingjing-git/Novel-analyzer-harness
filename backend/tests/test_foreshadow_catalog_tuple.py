"""伏笔总表构建的元组契约回归测试（10 元组解包不得崩溃）。

守的底线
--------
``FinalSummaryRunner._build_foreshadow_catalog`` 往 ``all_clues`` 里存的是 **10 元组**：

    (ch, clue, ftype, conf, imp,
     anchor_quote, anchor_chapter, motif_key, question, answer_criteria)

第 6..10 位是「锚点 + 三问」（复检 V2 的逐字证据入口）。下游一旦写成固定 5 位解包
（``for ch, clue, ftype, conf, imp in all_clues``），**只要书里有伏笔**就抛
``ValueError: too many values to unpack (expected 5)``，整个总结阶段直接崩。
现在写的是 ``for ch, clue, ftype, conf, imp, *extra in all_clues``；这组用例把它钉死。

为什么是行为测试（而不是「源码里不许出现 5 元组解包」的文本断言）
------------------------------------------------------------
这段逻辑**能低成本隔离**，所以没有走降级路线：

* ``FinalSummaryRunner.__init__`` 确实很重（构造 ``LLMClient``、``ForeshadowLedger.load``、
  探测书名），但 ``_build_foreshadow_catalog`` 本身只碰两样实例状态：
  ``self.config.analysis`` 的 5 个阈值字段，和 ``self.output_dir``（type→category
  映射文件所在地）。因此用 ``object.__new__(FinalSummaryRunner)`` 跳过 ``__init__``，
  手工挂上这两个属性即可——不 mock 任何方法。
* ``self.output_dir`` 指向 pytest 的 ``tmp_path``。那里本来就没有映射文件，于是
  ``_load_foreshadow_type_map()`` 走**真实代码路径**的「文件不存在 → 返回 None」分支，
  「旧书自由式 type 兜底」这条链也一并被真跑到了。
* 被测链是完整的：收集 → importance/confidence 阈值过滤 → category 归一 →
  ``text_utils.deduplicate_foreshadows``（它自己按 ``item[5:10]`` 读锚点与三问，
  与建表侧是**同一处位置契约**）→ 综合排序 → 分层截断。没有任何一段被桩替换。
* 全离线、无随机：不 import 也不调用 LLM，不读真实书籍素材（``results`` 全是内存
  构造的 dict），不写 ``output_dir`` 之外的任何路径。
* ``_build_foreshadow_catalog`` 是 async 但没有 await 点，用 ``asyncio.run`` 驱动，
  不依赖 pytest-asyncio 的 mode 配置。
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

from backend.config.constants import (
    FORESHADOW_CATEGORY_DEFS,
    FORESHADOW_CATEGORY_FALLBACK,
)
from backend.services.final_summary import FinalSummaryRunner
from backend.utils.text_utils import deduplicate_foreshadows

# ---------------------------------------------------------------- 测试素材

# 50 类之外的自由式 type：走「映射文件缺失 → 兜底 category」分支
LEGACY_TYPE = "旧版自由式类型"

# 一个真实存在的类名（合法 → category 直接用 type 本身，走另一条分支）
VALID_CATEGORY = FORESHADOW_CATEGORY_DEFS[0][0]
# 另一个真实类名，但不在下面 kept_categories 里 → 命中 category 过滤分支
DROPPED_CATEGORY = FORESHADOW_CATEGORY_DEFS[1][0]

# clue 必须 >= 15 字，否则在建表阶段就被丢掉（见 _build_foreshadow_catalog 的守卫）
CLUE_A = "他当年在雪地里捡到的那枚铜钱其实是祠堂暗格的钥匙"
CLUE_B = "母亲留下的那封没有署名的信提到过一次渡口沉船"
CLUE_C = "师父闭关之前反复叮嘱的那句口诀缺了最后两个字"
CLUE_D = "账房先生每夜子时都要往井里投一枚旧钱币"

ANCHOR_A = "那枚铜钱被他塞进贴身的荷包，再没拿出来过"
ANCHOR_D = "账房先生低头数着铜钱，井口传来回声"

# 位置哨兵：写成英文，避免任何归一化/编码干扰，便于断言「第 N 位落到了第 N 个键」
SENTINEL_MOTIF = "MOTIF-KEY-SENTINEL"
SENTINEL_QUESTION = "QUESTION-SENTINEL"
SENTINEL_CRITERIA = "ANSWER-CRITERIA-SENTINEL"


# ---------------------------------------------------------------- 隔离装置


def _make_runner(output_dir: Path, **analysis_overrides) -> FinalSummaryRunner:
    """用 ``object.__new__`` 绕过 ``__init__``，只挂上本方法真正用到的属性。

    这样既不构造 ``LLMClient``（无网络），也不 load 伏笔账本、不探测书名，
    而 ``config.analysis`` / ``output_dir`` 仍是真实对象、真实类型。
    """
    fields = dict(
        foreshadow_min_importance="低",
        foreshadow_min_confidence="低",
        # 只留兜底类 + 首类，便于用一次调用覆盖「type 合法」与「type 需兜底」两条分支
        foreshadow_kept_categories=[FORESHADOW_CATEGORY_FALLBACK, VALID_CATEGORY],
        max_foreshadow_catalog_high=500,
        max_foreshadow_catalog_mid=200,
    )
    fields.update(analysis_overrides)
    runner = object.__new__(FinalSummaryRunner)
    runner.config = types.SimpleNamespace(analysis=types.SimpleNamespace(**fields))
    runner.output_dir = Path(output_dir)
    return runner


def _build(runner: FinalSummaryRunner, results: list) -> list:
    """``_build_foreshadow_catalog`` 无 await 点，直接 asyncio.run 驱动。"""
    return asyncio.run(runner._build_foreshadow_catalog(results))


def _foreshadow(**overrides) -> dict:
    """一条 10 元组全字段的原始伏笔 dict（模拟新 prompt 约束下的产出）。"""
    fs = {
        "clue": CLUE_A,
        "type": LEGACY_TYPE,
        "confidence": "高",
        "importance": "高",
        "anchor_quote": ANCHOR_A,
        "anchor_chapter": 7,
        "motif_key": "铜钱",
        "question": "铜钱究竟能打开什么锁",
        "answer_criteria": "铜钱插进祠堂暗格并转动",
    }
    fs.update(overrides)
    return fs


def _chapter(ch: int, *foreshadows: dict) -> dict:
    """构造一条逐章分析结果（``_load_results`` 的产物形态）。"""
    return {"ch": ch, "foreshadowing_raw": list(foreshadows)}


# ---------------------------------------------------------------- 1. 10 元组不崩


def test_ten_field_foreshadow_does_not_raise(tmp_path):
    """核心回归：书里有伏笔（10 元组）时，整条构建链不得抛 ValueError。"""
    runner = _make_runner(tmp_path)

    catalog = _build(runner, [_chapter(3, _foreshadow())])

    assert len(catalog) == 1
    item = catalog[0]
    assert item["clue"] == CLUE_A
    assert item["first_seen"] == 3
    assert item["last_seen"] == 3


def test_multiple_ten_field_foreshadows_across_chapters(tmp_path):
    """多章多条 10 元组：不得崩，且不会互相吞并。"""
    runner = _make_runner(tmp_path)

    catalog = _build(
        runner,
        [
            _chapter(1, _foreshadow(clue=CLUE_A, anchor_quote=ANCHOR_A)),
            _chapter(2, _foreshadow(clue=CLUE_B, anchor_quote="母亲把那封信压在妆奁底下")),
            _chapter(3, _foreshadow(clue=CLUE_C, anchor_quote="师父说到一半就住了口")),
        ],
    )

    assert len(catalog) == 3
    assert sorted(item["first_seen"] for item in catalog) == [1, 2, 3]


# ------------------------------------------------- 2. *extra 按位置还原（不串位）


def test_extra_fields_restored_in_exact_position(tmp_path):
    """第 6..10 位必须一一落到对应键上：位置错一位就等于锚点造假。"""
    runner = _make_runner(tmp_path)

    catalog = _build(
        runner,
        [
            _chapter(
                5,
                _foreshadow(
                    anchor_quote=ANCHOR_A,
                    anchor_chapter=41,
                    motif_key=SENTINEL_MOTIF,
                    question=SENTINEL_QUESTION,
                    answer_criteria=SENTINEL_CRITERIA,
                ),
            )
        ],
    )

    assert len(catalog) == 1
    item = catalog[0]
    assert item["anchor_quote"] == ANCHOR_A
    assert item["anchor_chapter"] == 41
    assert item["motif_key"] == SENTINEL_MOTIF
    assert item["question"] == SENTINEL_QUESTION
    assert item["answer_criteria"] == SENTINEL_CRITERIA


def test_anchor_fields_survive_filtering_gaps(tmp_path):
    """中间夹着被过滤掉的条目时，保留下来的条目仍带自己的锚点（不串位）。"""
    runner = _make_runner(tmp_path, foreshadow_min_importance="中")

    catalog = _build(
        runner,
        [
            _chapter(1, _foreshadow(clue=CLUE_A, importance="高", anchor_quote=ANCHOR_A,
                                    question=SENTINEL_QUESTION)),
            # importance=低 → 被阈值丢掉
            _chapter(2, _foreshadow(clue=CLUE_B, importance="低", anchor_quote="不该出现的锚点")),
            # type 合法但 category 不在 kept 里 → 被 category 过滤丢掉
            _chapter(3, _foreshadow(clue=CLUE_C, type=DROPPED_CATEGORY, importance="高",
                                    anchor_quote="另一个不该出现的锚点")),
            _chapter(4, _foreshadow(clue=CLUE_D, importance="中", anchor_quote=ANCHOR_D,
                                    question="井里到底有什么")),
        ],
    )

    by_clue = {item["clue"]: item for item in catalog}
    assert set(by_clue) == {CLUE_A, CLUE_D}
    assert by_clue[CLUE_A]["anchor_quote"] == ANCHOR_A
    assert by_clue[CLUE_A]["question"] == SENTINEL_QUESTION
    assert by_clue[CLUE_D]["anchor_quote"] == ANCHOR_D
    assert by_clue[CLUE_D]["question"] == "井里到底有什么"


def test_category_is_not_pushed_into_anchor_quote_slot(tmp_path):
    """旧实现把 category 塞进第 6 位，dedup 会把它当 anchor_quote（静默错位）。

    这条用例直接盯住那种形态：type 是合法类名（会被当成 category），
    但 anchor_quote 必须还是原文，不能变成类名。
    """
    runner = _make_runner(tmp_path)

    catalog = _build(
        runner,
        [
            _chapter(
                2,
                _foreshadow(type=VALID_CATEGORY, anchor_quote=ANCHOR_A,
                            motif_key=SENTINEL_MOTIF),
            )
        ],
    )

    assert len(catalog) == 1
    item = catalog[0]
    assert item["type"] == VALID_CATEGORY
    assert item["anchor_quote"] == ANCHOR_A
    assert item["anchor_quote"] != VALID_CATEGORY
    assert item["motif_key"] == SENTINEL_MOTIF


# ------------------------------------------------------------ 3. 5 元组旧形态


def test_legacy_foreshadow_without_anchor_fields_does_not_raise(tmp_path):
    """旧库数据（没有锚点/三问字段）：extra 为空 → 用默认值补位，不得崩。"""
    runner = _make_runner(tmp_path)

    catalog = _build(
        runner,
        [
            _chapter(
                4,
                {"clue": CLUE_A, "type": LEGACY_TYPE,
                 "confidence": "中", "importance": "中"},
            )
        ],
    )

    assert len(catalog) == 1
    item = catalog[0]
    # 前 5 位照常
    assert item["clue"] == CLUE_A
    assert item["type"] == LEGACY_TYPE
    assert item["confidence"] == "中"
    assert item["importance"] == "中"
    # 第 6..10 位缺省补位
    assert item["anchor_quote"] == ""
    assert item["anchor_chapter"] == 0
    assert item["motif_key"] == ""
    assert item["question"] == ""
    assert item["answer_criteria"] == ""


def test_dedup_accepts_both_tuple_shapes():
    """建表侧的 10 元组与旧库的 5 元组，都是 ``deduplicate_foreshadows`` 的合法输入。

    这是同一处位置契约的另半边：它按 ``item[5:10]`` 读锚点与三问，超长不报错、
    缺省补默认值。
    """
    ten = [(1, CLUE_A, LEGACY_TYPE, "高", "高",
            ANCHOR_A, 9, "铜钱", "缺什么", "怎么做"),
           (2, CLUE_D, LEGACY_TYPE, "中", "中",
            ANCHOR_D, 12, "井", "井底有什么", "下去看")]
    five = [(1, CLUE_B, LEGACY_TYPE, "高", "高"),
            (2, CLUE_C, LEGACY_TYPE, "中", "中")]

    catalog_ten = deduplicate_foreshadows(ten)
    catalog_five = deduplicate_foreshadows(five)

    assert len(catalog_ten) == 2
    assert len(catalog_five) == 2
    assert {item["anchor_quote"] for item in catalog_ten} == {ANCHOR_A, ANCHOR_D}
    # 5 元组没有第 6..10 位 → 全部走默认值
    assert {item["anchor_quote"] for item in catalog_five} == {""}
    assert {item["anchor_chapter"] for item in catalog_five} == {0}
    assert {item["question"] for item in catalog_five} == {""}


def test_legacy_and_ten_field_entries_coexist(tmp_path):
    """同一本书里新旧两种产出混排（升级过程中的真实形态）：不崩、条数对得上。"""
    runner = _make_runner(tmp_path)

    catalog = _build(
        runner,
        [
            _chapter(1, _foreshadow(clue=CLUE_A, anchor_quote=ANCHOR_A)),
            _chapter(2, {"clue": CLUE_B, "type": LEGACY_TYPE,
                         "confidence": "中", "importance": "中"}),
        ],
    )

    by_clue = {item["clue"]: item for item in catalog}
    assert set(by_clue) == {CLUE_A, CLUE_B}
    assert by_clue[CLUE_A]["anchor_quote"] == ANCHOR_A
    assert by_clue[CLUE_B]["anchor_quote"] == ""


# ------------------------------------------------------------------ 4. 空列表


def test_empty_input_returns_empty_catalog(tmp_path):
    """空输入：不崩，返回空表。"""
    runner = _make_runner(tmp_path)

    assert _build(runner, []) == []
    assert _build(runner, [{"ch": 1, "foreshadowing_raw": []}]) == []
    # 缺 key 的分章结果同样不得崩
    assert _build(runner, [{"ch": 1}]) == []


def test_junk_entries_are_skipped_not_fatal(tmp_path):
    """非 dict 条目、过短 clue、空 clue：跳过而不是抛错，且不影响正常条目。"""
    runner = _make_runner(tmp_path)

    catalog = _build(
        runner,
        [
            _chapter(
                1,
                "不是 dict",
                None,
                {"clue": "太短"},
                {"clue": "   "},
                _foreshadow(),
            )
        ],
    )

    assert len(catalog) == 1
    assert catalog[0]["clue"] == CLUE_A


# ------------------------------------------------------ 5. 透传进 dedup 后仍带锚点


def test_anchor_passes_through_dedup_merge(tmp_path):
    """跨章合并时，代表条目（最早一章）的锚点被原样带出，且第一条不丢。"""
    runner = _make_runner(tmp_path)

    catalog = _build(
        runner,
        [
            # 后一章先出现（构造乱序，验证 dedup 按章排序取代表）
            _chapter(5, _foreshadow(clue=CLUE_A, anchor_quote="第五章的说法",
                                    anchor_chapter=5, question="后出现的问法")),
            _chapter(2, _foreshadow(clue=CLUE_A, anchor_quote=ANCHOR_A,
                                    anchor_chapter=2, question=SENTINEL_QUESTION)),
        ],
    )

    assert len(catalog) == 1
    item = catalog[0]
    assert item["first_seen"] == 2
    assert item["last_seen"] == 5
    assert item["evidence_chapters"] == [2, 5]
    assert item["merged_count"] == 2
    # 代表条目 = 首次出现那章
    assert item["anchor_quote"] == ANCHOR_A
    assert item["anchor_chapter"] == 2
    assert item["question"] == SENTINEL_QUESTION
