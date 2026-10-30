"""Prompt 视图：把"知识库里的全部内容"裁剪成"这一次要给模型看的窗口"。

与 :mod:`backend.vnext.knowledge` 的分工必须清楚：

* **知识库保存全部**，永不因为"提示词放不下"而删数据。旧实现在落盘前做裁剪
  （`_trim_kb` 只裁 5 个列表、两个 dict 不裁），结果是"库里有什么取决于当初配了多大窗口"，
  重跑一次、改一次配置，历史就没了——而且没有任何报错。
* **窗口是读取时的动作**。这里按限额取子集、报告丢了什么，然后把子集交给 Prompt 构建。
  同一份库可以被不同限额读成不同窗口，库本身不动。

另一个刻意的选择：限额截断**必须被报告**（``truncated``）。模型不知道"上面还有 800 条
被截掉了"，就会把"没提到"当成"不存在"，于是在后续章节里重新发现、重新推理——那正是
长篇小说分析里最典型的退化。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .knowledge import KnowledgeSnapshot, KnowledgeStore
from .prompt_limits import PromptLimits


@dataclass(frozen=True)
class PromptWindow:
    """给一次 Prompt 用的上下文窗口。"""

    exclusive_before: int
    facts: tuple[str, ...] = ()
    character_states: tuple[tuple[str, str], ...] = ()
    dropped: dict[str, int] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        return any(count > 0 for count in self.dropped.values())

    def to_dict(self) -> dict:
        payload = {
            "exclusive_before": self.exclusive_before,
            "facts": list(self.facts),
            "character_states": [
                {"name": name, "state": state} for name, state in self.character_states
            ],
        }
        if self.truncated:
            # 只在实际截断时出现：让模型知道"还有更多，只是没给你"
            payload["truncated"] = dict(self.dropped)
        return payload


def build_prompt_window(
    store: KnowledgeStore,
    *,
    exclusive_before: int,
    limits: PromptLimits,
) -> PromptWindow:
    """按限额裁剪出一个窗口。**不修改** store。

    裁剪方向：事实取**来源位置最靠后**的 N 条（离当前块最近的上下文才相关），
    渲染时再按时间正序排列——给模型看的东西应该按故事时间读，选择依据则是"远近"。
    人物状态按名字排序取前 N 条：这里没有"最近活跃"信息，用确定性的顺序优于用
    到达顺序（后者会让同一份输入在不同运行下给出不同 Prompt）。
    """
    snapshot = store.snapshot(exclusive_before=exclusive_before)
    return _window_from_snapshot(snapshot, limits=limits)


def _window_from_snapshot(snapshot: KnowledgeSnapshot, *, limits: PromptLimits) -> PromptWindow:
    facts = snapshot.facts
    dropped: dict[str, int] = {}

    if len(facts) > limits.max_verified_facts:
        dropped["facts"] = len(facts) - limits.max_verified_facts
        # 取最近的 N 条（快照内已按来源位置升序，故取尾部），再升序渲染
        facts = facts[-limits.max_verified_facts :]

    states = snapshot.character_states
    if len(states) > limits.max_character_states:
        dropped["character_states"] = len(states) - limits.max_character_states
        states = tuple(sorted(states, key=lambda pair: pair[0])[: limits.max_character_states])

    return PromptWindow(
        exclusive_before=snapshot.exclusive_before,
        facts=tuple(fact.text for fact in facts),
        character_states=states,
        dropped=dropped,
    )


def count_knowledge(store: KnowledgeStore) -> dict:
    """库存量（未被任何窗口裁剪过的真实数量）。"""
    facts, chars = store.reduced()
    return {"facts": len(facts), "character_records": len(chars)}
