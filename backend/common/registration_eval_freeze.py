"""登记评估的冻结与隔离检查。

两件事：

1. **冻结**：把问题、条件、来源跨度、输入前缀哈希、规则版本固定下来。
   冻结后**拒绝原位覆盖**——修订另建版本。
2. **隔离检查**：证明可见范围没有泄露后文。

隔离不是"抽查几个字面片段没出现"——那只能说明未发现所抽查的逐字片段，
不能证明没有语义泄露。可检查的部分必须是硬的：

- **类别齐全**：正文前缀、摘要、知识库、检索材料四类都要在清单里；
  未使用的类别要**显式标 unused**，不能用空串冒充。
- **内容哈希复算**：清单里的哈希要与实际内容一致；填了假哈希不算通过。
- **与冻结记录一致**：传入的材料必须就是冻结时登记的那份。
- **辅助材料只从允许的前缀构建**：辅助材料里的内容必须能在前缀里找到；
  改写后文答案的摘要同样会被这一条挡住。
- **字面探针**：只作补充检查，不能单独作为无泄露的证据。

离线检查器可以读后文；生成与标注进程不能继承它的输入或上下文。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "registration_eval_freeze_v1"

#: 送入模型的材料类别。每一类都必须出现在清单里。
MATERIAL_KINDS = ("body_prefix", "summary", "knowledge", "retrieval")

#: 辅助材料：不是正文前缀本身，必须可由前缀溯源。
AUXILIARY_KINDS = ("summary", "knowledge", "retrieval")

#: 辅助材料里连续片段的最小长度。短于此的片段不做溯源判定——
#: 它们撞车概率高，判了也不可信。
AUXILIARY_NGRAM = 8


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    """只做空白归一。不做 Unicode 规范化——坐标必须落在原始文本上。"""
    return "\n".join(line.strip() for line in (text or "").splitlines())


def build_freeze_record(
    *,
    work_id: str,
    prefix_id: str,
    prefix_unit_ids: Sequence[int],
    prefix_sha256: str,
    materials: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
    rules_version: str,
    generator_model: str = "",
    prompt_template_version: str = "",
    code_version: str = "",
    budget: Mapping[str, Any] | None = None,
    run_id: str = "",
    arm: str = "",
    observations: Sequence[Mapping[str, Any]] = (),
    requests: Sequence[Mapping[str, Any]] = (),
) -> dict:
    """构造一份冻结记录。

    `materials`：实际送入模型的材料，每项
    {kind, sha256, chars, source_prefix_id, generation?, used: True|False}。
    `used=False` 表示该类别**显式未使用**；未使用的类别不得填内容。
    `items`：被冻结的登记条目，每项
    {item_id, question, criteria, source_span, visible_prefix_id}。
    `observations`：实际观察边界，每项
    {item_id, citation_char, visible_range_end, registration_checkpoint, wall_clock}。
    """
    kinds = sorted({str(m.get("kind") or "") for m in materials})
    declared_unused = sorted(
        {str(m.get("kind") or "") for m in materials if m.get("used") is False}
    )
    return {
        "schema": SCHEMA,
        "work_id": work_id,
        "prefix_id": prefix_id,
        "prefix_unit_ids": [int(u) for u in prefix_unit_ids],
        "prefix_sha256": prefix_sha256,
        "materials": [dict(m) for m in materials],
        "material_kinds": kinds,
        "material_kinds_complete": set(kinds) >= set(MATERIAL_KINDS),
        "declared_unused_kinds": declared_unused,
        "materials_all_hashed": all(str(m.get("sha256") or "") for m in materials),
        "items": [dict(i) for i in items],
        "observations": [dict(o) for o in observations],
        "requests": [dict(r) for r in requests],
        "rules_version": rules_version,
        "prompt_template_version": prompt_template_version,
        "generator_model": generator_model,
        "code_version": code_version,
        "budget": dict(budget or {}),
        "run_id": run_id,
        "arm": arm,
        "frozen": True,
    }


def check_isolation(
    *,
    record: Mapping[str, Any],
    materials: Sequence[Mapping[str, Any]] | None = None,
    prefix_text: str = "",
    later_text: str = "",
) -> dict:
    """核验可见范围有没有泄露后文。

    `materials`：传入的实际材料；不给就用冻结记录里登记的。
    `prefix_text`：允许的原文前缀全文。
    `later_text`：前缀之后的原文（只用于字面补充检查）。
    """
    problems: list[str] = []
    frozen = [dict(m) for m in (record.get("materials") or [])]
    mats = [dict(m) for m in (materials if materials is not None else frozen)]

    # 1) 类别齐全
    kinds = {str(m.get("kind") or "") for m in mats}
    missing = [k for k in MATERIAL_KINDS if k not in kinds]
    if missing:
        problems.append("missing_material_kinds:" + ",".join(missing))
    unknown_kinds = sorted(kinds - set(MATERIAL_KINDS))
    if unknown_kinds:
        problems.append("unknown_material_kinds:" + ",".join(unknown_kinds))

    # 2) 与冻结记录一致
    if materials is not None:
        frozen_by_kind = {str(m.get("kind") or ""): m for m in frozen}
        for m in mats:
            f = frozen_by_kind.get(str(m.get("kind") or ""))
            if f is None:
                problems.append("material_not_in_frozen_record:" + str(m.get("kind")))
                continue
            if str(m.get("sha256") or "") != str(f.get("sha256") or ""):
                problems.append("material_differs_from_frozen:" + str(m.get("kind")))

    # 3) 内容哈希复算 + 显式未使用声明
    for m in mats:
        kind = str(m.get("kind") or "")
        content = m.get("content")
        used = m.get("used", True)
        if used is False:
            # 显式未使用：不得带内容
            if content:
                problems.append("declared_unused_but_has_content:" + kind)
            continue
        if content is None or str(content) == "":
            problems.append("material_without_content:" + kind)
            continue
        actual = _sha(str(content))
        declared = str(m.get("sha256") or "")
        if not declared:
            problems.append("material_without_hash:" + kind)
        elif actual != declared:
            problems.append("material_hash_mismatch:" + kind)

    # 4) 辅助材料必须能溯源到允许的前缀
    if prefix_text:
        pnorm = _norm(prefix_text)
        for m in mats:
            if str(m.get("kind") or "") not in AUXILIARY_KINDS:
                continue
            if m.get("used", True) is False:
                continue
            content = str(m.get("content") or "")
            if not content:
                continue
            bad = _ungrounded_spans(content, pnorm)
            if bad:
                problems.append(
                    "auxiliary_not_grounded_in_prefix:%s:%d" % (m.get("kind"), len(bad))
                )

    # 5) 字面探针（只作补充）
    literal = _literal_leak_check(mats, later_text)
    if literal["leaked"]:
        problems.append("literal_later_text_leaked:%d" % len(literal["leaked"]))

    return {
        "ok": not problems,
        "problems": problems,
        "checked_materials": len(mats),
        "material_kinds": sorted(kinds),
        "declared_unused_kinds": sorted(
            {str(m.get("kind") or "") for m in mats if m.get("used") is False}
        ),
        "auxiliary_grounding": {
            "checked": sum(
                1
                for m in mats
                if str(m.get("kind") or "") in AUXILIARY_KINDS and m.get("used", True) is not False
            ),
            "note": "辅助材料每个连续片段都必须能在前缀里找到；改写后文同样会被挡住",
        },
        "literal_probe": {
            "note": "只作补充检查：阴性只表示未发现所抽查的逐字片段，不能证明没有语义泄露",
            "leaked": literal["leaked"],
        },
    }


def _ungrounded_spans(content: str, prefix_norm: str) -> list[str]:
    """找出辅助材料里**不能**在前缀中溯源的连续片段。"""
    norm = _norm(content)
    bad: list[str] = []
    for i in range(0, max(1, len(norm) - AUXILIARY_NGRAM + 1)):
        piece = norm[i : i + AUXILIARY_NGRAM]
        if len(piece) < AUXILIARY_NGRAM:
            break
        if piece not in prefix_norm:
            bad.append(piece)
            if len(bad) >= 5:
                break
    return bad


def _strip_lead_label(text: str) -> str:
    t = (text or "").strip()
    for sep in ("：", ":"):
        idx = t.find(sep)
        if 0 < idx <= 12:
            head = t[:idx]
            if head.startswith("第") and len(head) <= 12:
                return t[idx + 1 :].strip()
    return t


def _literal_leak_check(mats: Sequence[Mapping[str, Any]], later_text: str) -> dict:
    later = (later_text or "").strip()
    if not later:
        return {"leaked": []}
    probes: list[str] = []
    for base in (later, _strip_lead_label(later)):
        if base and base not in probes:
            probes.append(base)
    if len(later) > 120:
        step = max(60, len(later) // 4)
        for i in range(0, max(1, len(later) - 40), step):
            piece = later[i : i + 120]
            if len(piece) >= 60:
                probes.append(piece)
            if len(probes) >= 6:
                break
    leaked: list[dict] = []
    for m in mats:
        content = str(m.get("content") or "")
        if not content:
            continue
        for probe in probes:
            if probe in content:
                leaked.append(
                    {"kind": m.get("kind"), "probe_sha256": _sha(probe), "probe_chars": len(probe)}
                )
                break
    return {"leaked": leaked}


def save_freeze(path: Path, record: Mapping[str, Any]) -> bool:
    """落盘冻结记录。**拒绝原位覆盖**——修订另建版本。

    已存在且内容不同时抛错；完全相同则视为重复写入，放行。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"冻结文件已存在但无法解析，拒绝覆盖：{path}") from exc
        if json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) != payload:
            raise ValueError(
                f"冻结文件已存在且内容不同，拒绝原位覆盖：{path}；修订请另建版本"
            )
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return True


def load_freeze(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
