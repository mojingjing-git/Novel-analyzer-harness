"""单块分析（B1a）：把"库里有什么 + 这几章正文"变成一条可校验的块结果。

四条硬约束，全是复审踩出来的：

1. **全程 async**。``llm_client.chat`` 是 async 且返回 ``LLMResult``；同步 IO 走
   ``asyncio.to_thread``。在 FastAPI 处理函数里 ``asyncio.run`` 会直接炸。
2. **``extracted`` 必须带 ``SourceRef``**（契约 §16.1）。没有来源的事实重放时会丢，
   于是"续跑"得到空库——那等于换了一套输入。
3. **上下文指纹进依赖**。真正进 Prompt 的是裁剪后的窗口，所以指纹取
   ``PromptWindow.fingerprint()``（含限额与 dropped），不是快照的。
4. **解析失败要显式报错**。模型偶尔返回半截 JSON，``parse_json_robust`` 可能修出一个
   语法合法但语义错误的对象；宁可报 ``UNPARSEABLE_ARTIFACT``，也不许把空结果当成功落盘。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .blocks import BlockPlan
from .citations import CITATION_INSTRUCTION, verify_citation
from .constants import ErrorCode, VnextFormatError
from .fingerprint import canonical_json, content_hash
from .knowledge import (
    CharacterStateRecord,
    FactRecord,
    KnowledgeStore,
    SourceRef,
    records_from_extracted,
)
from .prompt_limits import PromptLimits
from .prompt_view import PromptWindow, build_prompt_window
from .project import load_manifest, read_chapter_text, chapters_dir
from .results import (
    ST_SUCCEEDED,
    ResultRecord,
    ResultStrategy,
    expected_dependency,
    make_result_id,
    publish_result,
)

#: Prompt 组织方式的版本。改 system/user 结构就 +1。
PROMPT_STRATEGY_VERSION = 1

SYSTEM_PROMPT = """你是一名严谨的中文长篇小说分析员。逐块阅读原文，只输出**可核验**的信息。

输出必须是一个 JSON 对象，形如：
{"facts": [{"fact_id": "f-1", "text": "发生了什么（一句话）",
            "quote": {"start": 0, "end": 12, "text": "原文连续片段"}}],
 "characters": [{"name": "人物名", "state": "本章结束时的处境/状态",
                 "quote": {"start": 0, "end": 12, "text": "原文连续片段"}}]}

要求：
1. 每条都得有 quote，且 quote.text 必须是本章原文的**连续片段**（逐字，不改标点）。
2. start/end 是该片段在本章正文里的码点位置（左闭右开）。
3. 拿不准、找不到原文支撑的，宁可不写；不要概括成"可能有"，也不要编造引文。
4. 只输出 JSON，不要解释、不要 Markdown 代码块。
""" + "\n" + CITATION_INSTRUCTION


@dataclass(frozen=True)
class BlockRun:
    """一次单块分析的结果。"""

    record: ResultRecord
    window: PromptWindow
    prompt_chars: int

    @property
    def prompts_tokens_unknown(self) -> bool:
        return self.record.usage_status != "KNOWN"


def build_messages(
    *,
    title: str,
    block_label: str,
    chapters: list[tuple[str, str]],
    window: PromptWindow,
) -> list[dict[str, str]]:
    """组 Prompt：system 固定（利于前缀缓存），user 按块拼。"""
    parts: list[str] = [f"书名：{title}", f"本块：{block_label}", ""]
    if window.facts or window.character_states:
        parts.append("[已知前情]")
        if window.facts:
            parts.append("已确认事实：")
            parts.extend(f"- {text}" for text in window.facts)
        if window.character_states:
            parts.append("人物状态：")
            parts.extend(f"- {name}：{state}" for name, state in window.character_states)
        if window.truncated:
            # 不告知"还有更多被截掉"，模型会把"没提到"当成"不存在"，然后重复发现
            dropped = "，".join(f"{k} 省略 {v} 条" for k, v in window.dropped.items())
            parts.append(f"（注意：更早的记录因篇幅被截断：{dropped}）")
    else:
        parts.append("（这是故事开头，暂无前情）")
    parts.append("")
    parts.append("[本章正文]")
    for chapter_id, text in chapters:
        parts.append(f"### {chapter_id}")
        parts.append(text)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def parse_block_output(raw: str) -> dict:
    """把模型输出解析成 ``extracted``。失败一律抛错，不返回空对象。"""
    from backend.utils.json_utils import parse_json_robust

    payload, error = parse_json_robust(raw)
    if not isinstance(payload, Mapping):
        raise VnextFormatError(
            ErrorCode.UNPARSEABLE_ARTIFACT,
            f"模型输出不是 JSON 对象：{error or '解析失败'}",
        )
    facts = payload.get("facts", [])
    characters = payload.get("characters", [])
    if not isinstance(facts, list) or not isinstance(characters, list):
        raise VnextFormatError(
            ErrorCode.UNPARSEABLE_ARTIFACT, "facts / characters 必须是数组"
        )
    return {"facts": facts, "characters": characters}


def _source_from_quote(
    quote: object, *, chapter_id: str, chapter_ordinal: int, chapter_text: str
) -> tuple[SourceRef, str] | None:
    """把模型给的 quote 变成 ``(来源, 引文)``。区间不合法就返回 None（不算证据）。"""
    if not isinstance(quote, Mapping):
        return None
    try:
        start = int(quote["start"])
        end = int(quote["end"])
        text = str(quote.get("text") or "")
    except (KeyError, TypeError, ValueError):
        return None
    if start < 0 or end <= start or end > len(chapter_text) or not text:
        return None
    return (
        SourceRef(
            chapter_id=chapter_id,
            chapter_ordinal=chapter_ordinal,
            start=start,
            end=end,
            content_hash=content_hash(chapter_text),
        ),
        text,
    )


def _verify_state(quote_text: str, source: SourceRef, chapter_text: str) -> str:
    """核对的**是引文，不是结论文字**——结论是概括，本来就不可能逐字出现在正文里。"""
    check = verify_citation(
        quote_text=quote_text,
        chapter_text=chapter_text,
        start=source.start,
        end=source.end,
    )
    return check.verification_state


def normalize_extracted(
    raw: Mapping[str, Any],
    *,
    chapter_id: str,
    chapter_ordinal: int,
    chapter_text: str,
) -> dict:
    """把模型给的 facts/characters 补上 ``source`` 与 ``fact_id``。

    没有可用 quote 的条目**丢弃**——按契约 §16.1，没有来源的事实重放时也留不下，
    留着只会让"库里有东西"变成假象。
    """
    facts: list[dict] = []
    for index, item in enumerate(raw.get("facts", []) or [], start=1):
        if not isinstance(item, Mapping):
            continue
        located = _source_from_quote(
            item.get("quote"),
            chapter_id=chapter_id,
            chapter_ordinal=chapter_ordinal,
            chapter_text=chapter_text,
        )
        text = str(item.get("text") or "").strip()
        if located is None or not text:
            continue
        source, quote_text = located
        facts.append(
            {
                "fact_id": str(item.get("fact_id") or f"{chapter_id}-f{index}"),
                "text": text,
                "source": source.to_dict(),
                "verification_state": _verify_state(quote_text, source, chapter_text),
            }
        )

    characters: list[dict] = []
    for item in raw.get("characters", []) or []:
        if not isinstance(item, Mapping):
            continue
        located = _source_from_quote(
            item.get("quote"),
            chapter_id=chapter_id,
            chapter_ordinal=chapter_ordinal,
            chapter_text=chapter_text,
        )
        name = str(item.get("name") or "").strip()
        if located is None or not name:
            continue
        source, _quote_text = located
        characters.append(
            {
                "name": name,
                "state": str(item.get("state") or "").strip(),
                "uncertain": bool(item.get("uncertain", False)),
                "source": source.to_dict(),
            }
        )
    return {"facts": facts, "characters": characters}


def extracted_fingerprint(extracted: Mapping[str, Any]) -> str:
    """内容指纹：进 ``result_id``，让"内容变了"必然改变身份。"""
    return content_hash(canonical_json(extracted))


class BlockFailure(RuntimeError):
    """块级失败，保留结构化判定（尤其是"是否永久"）。"""

    def __init__(
        self,
        message: str,
        *,
        verdict: object = None,
        permanent: bool = False,
        raw: str = "",
    ) -> None:
        super().__init__(message)
        self.verdict = verdict
        #: 原始输出（解析失败时留下前 4000 字），否则只能看到"UNPARSEABLE_ARTIFACT"
        self.raw = raw
        #: 明确标为永久（审核拦截/鉴权/欠费）——上层据此决定是"停下"还是"跳过留 gap"
        self.permanent = permanent


async def analyze_block(
    book_dir: Path,
    plan: BlockPlan,
    *,
    client: Any,
    limits: PromptLimits,
    strategy: ResultStrategy,
    prior_result_ids: list[str | None],
    store: KnowledgeStore | None = None,
    title: str | None = None,
    model_fingerprint: str | None = None,
) -> BlockRun:
    """分析一个块并落盘。**调用方负责持有书锁与预算**（这里只做纯计算 + publish）。"""
    book_dir = Path(book_dir)
    manifest = await asyncio.to_thread(load_manifest, book_dir)
    chapters = manifest.chapters
    by_id = {c.chapter_id: c for c in chapters}

    missing = [cid for cid in plan.chapter_ids if cid not in by_id]
    if missing:
        raise VnextFormatError(
            ErrorCode.MISSING_REQUIRED_FIELD, f"块成员不在清单里：{missing}"
        )

    first_ordinal = min(by_id[cid].ordinal for cid in plan.chapter_ids)
    window = build_prompt_window(
        store or KnowledgeStore(), exclusive_before=first_ordinal, limits=limits
    )

    texts: list[tuple[str, str]] = []
    for cid in plan.chapter_ids:
        texts.append((cid, await asyncio.to_thread(read_chapter_text, book_dir, cid)))
    merged = "\n".join(text for _, text in texts)

    label = f"第 {plan.start_display_no or first_ordinal} 章起共 {len(plan.chapter_ids)} 章"
    messages = build_messages(
        title=title or manifest.title, block_label=label, chapters=texts, window=window
    )
    prompt_chars = sum(len(m["content"]) for m in messages)

    result = await client.chat(messages)
    raw_text = str(getattr(result, "content", "") or "")
    if not raw_text.strip():
        # 空文本几乎不是"模型没按格式答"，而是**调用本身失败了**——厂商侧拦截
        # （MiniMax: 422 `input new_sensitive (1026)`）或欠费/鉴权，返回一个空结果
        # 而不是抛异常。若这里直接拿空串去解析，就成了 `UNPARSEABLE_ARTIFACT`，
        # 于是"永久错误"被伪装成"格式错误"，上层再也没法按永久错误处理。
        verdict = getattr(result, "verdict", None)
        detail = str(getattr(result, "error", "") or "")
        permanent = False
        is_perm = getattr(verdict, "is_permanent", None)
        if callable(is_perm):
            try:
                permanent = bool(is_perm())
            except TypeError:
                permanent = bool(is_perm)
        if not permanent:
            # 文本通道按设计"永不判永久"（C-01 止血），所以审核拦截这类
            # 只能在这里用专门嗅探器补一次，否则永久错误会被当成普通失败。
            try:
                from backend.core import llm_client as core_llm

                permanent = bool(
                    core_llm.moderation_hit_from_exception(result)
                    or core_llm.is_moderation_message(f"{detail} {raw_text}")
                )
            except Exception:  # noqa: BLE001
                permanent = False
        raise BlockFailure(
            f"模型返回空内容（error={detail or '未知'}）",
            verdict=verdict,
            permanent=permanent,
        )
    try:
        parsed = parse_block_output(raw_text)
    except Exception as exc:  # noqa: BLE001
        raise BlockFailure(
            f"输出解析失败（{exc}）", verdict=getattr(result, "verdict", None), raw=raw_text[:4000]
        ) from exc
    # 多章块：先用整块文本定位 quote（章边界在原文里通常仍有换行，取第一处能落地的章）
    extracted = normalize_extracted(
        parsed,
        chapter_id=plan.chapter_ids[0],
        chapter_ordinal=first_ordinal,
        chapter_text=merged,
    )

    dep = expected_dependency(
        prior_result_ids,
        strategy,
        context_fingerprint=window.fingerprint(),
    )
    tokens = getattr(result, "tokens", None)
    usage_status = str(getattr(result, "usage_status", "UNKNOWN"))
    record = ResultRecord(
        result_id=make_result_id(
            block_signature=plan.block_signature,
            prompt_strategy_version=PROMPT_STRATEGY_VERSION,
            model_fingerprint=model_fingerprint if model_fingerprint is not None else strategy.model_fingerprint,
            dependency_fingerprint=dep,
            extracted_fingerprint=extracted_fingerprint(extracted),
        ),
        block_signature=plan.block_signature,
        source_chapters=tuple((c, by_id[c].content_hash) for c in plan.chapter_ids),
        status=ST_SUCCEEDED,
        model_fingerprint=model_fingerprint if model_fingerprint is not None else strategy.model_fingerprint,
        prompt_strategy_version=PROMPT_STRATEGY_VERSION,
        dependency_fingerprint=dep,
        extracted=extracted,
        prompt_tokens=tokens[0] if tokens else None,
        completion_tokens=tokens[1] if tokens else None,
        usage_status=usage_status,
    )
    await asyncio.to_thread(publish_result, book_dir, record)
    return BlockRun(record=record, window=window, prompt_chars=prompt_chars)


def make_chat_caller(client: Any) -> Callable[[list[dict]], Awaitable[str]]:
    """把 ``LLMClient`` 适配成"给 messages 返回文本"的最小调用方。"""

    async def call(messages: list[dict]) -> str:
        result = await client.chat(messages)
        return str(getattr(result, "content", "") or "")

    return call
