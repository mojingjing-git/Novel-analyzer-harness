"""vnext 身份与指纹：所有"这份数据属于哪个任务/哪个来源"的判据。

统一原则（PLAN v2 §2.1）：

* **指纹只由内容与策略版本决定**，不含时间戳、不含随机 ID、不含"当前最大章号"
  这类会随进度漂移的东西。否则同一份输入二次运行会得到新指纹，缓存全废。
* **拼接方式必须精确到字节**：字段顺序、分隔符、编码、截断长度全部写死。含糊的
  "拼接后取哈希"会在实现者之间产生互不兼容的两种指纹——这是最难查的一类 bug。
* **截断长度不同的用途刻意分开**：展示用的短 ID（``evidence_id`` 12 位）与
  比对用的长指纹（64 位）不是同一个东西，不能互相替代。

本模块是纯函数，无 IO、无全局状态，便于单测与并发调用。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# 文本规范化
# ---------------------------------------------------------------------------

#: 规范化规则版本。改变规则必须 +1，并让所有 ``content_hash`` 失效。
#: 规则：去 BOM → 换行统一为 LF → 行尾空白（空格/制表）删除 → 连续 3 行以上
#: 空行压成 2 行 → 文末保证恰好一个 LF。**不做**全角/半角转换、不做简繁转换、
#: 不删空格内的内容——那些会改变字符数，使已保存的 source_ref 偏移失效。
TEXT_NORMALIZATION_VERSION = 1


def normalize_chapter_text(raw: str) -> str:
    """把导入的章节原文规范成"可被 source_ref 引用的权威文本"。"""
    text = raw.lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in text.split("\n")]
    # 压缩连续空行：保留至多 1 个空行（即最多连续 2 个 \n）
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        out.append(line)
    text = "\n".join(out).strip("\n")
    return text + "\n"


def content_hash(normalized_text: str) -> str:
    """章节规范文本的内容哈希（64 位十六进制）。

    入参必须是 :func:`normalize_chapter_text` 的输出。这里不替调用方规范化：
    若允许"传原文也能算"，同一条章就会有第二种哈希，引用校验立刻失效。
    """
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 规范 JSON 序列化（指纹的唯一输入格式）
# ---------------------------------------------------------------------------


def canonical_json(payload: object) -> str:
    """规范的 JSON 文本：键排序、无多余空白、``ensure_ascii=False``。

    ``ensure_ascii`` 必须是 False——中文被转成 ``\\uXXXX`` 虽然"也能算"，但会让
    指纹依赖 Python 的转义实现，跨语言/跨版本重算就对不上。
    """
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash_parts(parts: Iterable[str], *, length: int | None = None) -> str:
    """用 ``\\x1f``（单元分隔符）拼接后取 sha256。

    选 ``\\x1f`` 是因为它不可能出现在正常文本或 ID 里，避免 ``a|b`` 与 ``a|``+``b``
    这类拼接歧义（用 ``|`` 会产生碰撞）。
    """
    joined = "\x1f".join(parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return digest[:length] if length else digest


# ---------------------------------------------------------------------------
# 各项指纹
# ---------------------------------------------------------------------------


def block_signature(
    chapter_hashes: Sequence[tuple[str, str]],
    *,
    block_strategy_version: int,
) -> str:
    """块身份：绑定"实际有序的 (chapter_id, content_hash) 列表"与策略版本。

    块起始章与 ``block_size`` 都不单独参与——它们已被章节列表隐含。这样
    「同样 size 但内容不同」「同样起始章但成员不同」都会得到不同签名，
    而"重排章节顺序"同样会改变签名（顺序参与拼接）。
    """
    payload = {
        "block_strategy_version": block_strategy_version,
        "chapters": [[cid, chash] for cid, chash in chapter_hashes],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def dependency_fingerprint(dependencies: Mapping[str, object]) -> str:
    """结果对上下文的依赖指纹。

    只接受**能被规范化成 JSON** 的值；调用方负责把"用了哪几个上游结果"整理成
    类似 ``{"context_results": ["r1", "r2"], "rolling_snapshot": "..."}`` 的形状。
    """
    return hashlib.sha256(canonical_json(dependencies).encode("utf-8")).hexdigest()


def task_key(
    *,
    kind: str,
    stage: str,
    inputs: Mapping[str, object],
    strategy_versions: Mapping[str, int],
) -> str:
    """任务身份：同类同阶段同输入同策略 = 同一个任务。

    刻意不含 ``run_id``：跨运行复用是要的（同一份输入重跑应当命中缓存）。
    运行维度的区分放在 ``run_id`` 字段里，不进来。
    """
    payload = {
        "kind": kind,
        "stage": stage,
        "inputs": inputs,
        "strategy_versions": strategy_versions,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def evidence_id(
    *,
    book_id: str,
    chapter_id: str,
    chapter_content_hash: str,
    start: int,
    end: int,
) -> str:
    """证据身份：来源锚点哈希，12 位十六进制。

    与 PLAN v2 §2.3 一致：绑定 ``book_id + chapter_id + content_hash + start + end``。
    不含引文文本本身——引文是"证据内容"，改了引文但指向同一锚点仍是同一条证据；
    而 ``content_hash`` 会随正文变化，足以让整条锚点失效。
    """
    return _hash_parts(
        [book_id, chapter_id, chapter_content_hash, str(start), str(end)],
        length=12,
    )


def result_id(
    *,
    block_sig: str,
    prompt_strategy_version: int,
    model_fingerprint: str,
    dependency_fp: str,
    extracted_fp: str = "",
) -> str:
    """结果身份：一块内容 + 一套生成条件 = 一个结果版本。

    同一块用不同模型/不同 Prompt 策略重算，得到不同 ``result_id``；这正是
    "重算版本不能同时合并"的实现基础。
    """
    return hashlib.sha256(
        canonical_json(
            {
                "block_signature": block_sig,
                "prompt_strategy_version": prompt_strategy_version,
                "model_fingerprint": model_fingerprint,
                "dependency_fingerprint": dependency_fp,
                # 内容指纹必须进身份：同条件重跑得到同 ID 会**原地覆盖**旧内容，
                # 于是"结果变了"在 ordered_result_ids 上表现为没变，重放与失效都不触发。
                "extracted_fingerprint": extracted_fp,
            }
        ).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# source_ref 偏移
# ---------------------------------------------------------------------------


def codepoint_len(text: str) -> int:
    """Unicode 码点长度（不是 UTF-16 码元长度）。

    ``len()`` 在 Python 里就是码点数——但若有人从 JS/前端拿 ``string.length``
    算偏移，emoji 与部分汉字会差 1，引用就会错位。所以这里显式提供一个函数名，
    让"用的是码点"这件事在调用处可读。
    """
    return len(text)


def slice_source_ref(normalized_text: str, start: int, end: int) -> str:
    """按左闭右开码点区间取引文。越界直接抛 ``IndexError``，不截断。"""
    if start < 0 or end < start or end > len(normalized_text):
        raise IndexError(f"source_ref 越界: [{start}, {end}) 长度 {len(normalized_text)}")
    return normalized_text[start:end]


def normalize_for_display(text: str) -> str:
    """仅用于展示的归一（去首尾空白 + NFKC）。**不得**用于指纹或偏移计算。"""
    return unicodedata.normalize("NFKC", text).strip()
