"""登记位置规则小试的运行器部件（离线可验证，不含模型调用）。

模型只输出 `source_unit_id` 与逐字引文；**精确跨度由宿主定位**：
在声明单元的原文中找唯一 `[start, end)`。歧义、越界或不匹配一律单列，
**不默取第一个命中**。

坐标是原始文本的 Unicode 字符下标，区间 `[start, end)`。

包含的部件：
- 前缀清单冻结（单元 ID、内容哈希、可见范围终点）
- 模板装配与逐请求哈希
- 宿主定位与 schema 转换
- token 预检（对输入/输出上限）
- 预算记账（调用数与 token 硬上限，重试也占预算）
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "registration_pilot_v1"


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 前缀清单
# --------------------------------------------------------------------------

def build_prefix_manifest(
    *,
    work_id: str,
    prefix_id: str,
    units: Mapping[int, str],
    checkpoint: int,
    registration_checkpoint: int | None = None,
) -> dict:
    """冻结一个前缀的清单。

    单元 ID 是稳定物理序号；`checkpoint` 是本次可见范围终点。
    """
    ids = sorted(int(k) for k in units)
    entries = []
    for u in ids:
        text = units[u]
        entries.append(
            {
                "unit_id": int(u),
                "chars": len(text),
                "sha256": _sha(text),
            }
        )
    visible = [e for e in entries if e["unit_id"] <= int(checkpoint)]
    return {
        "schema": SCHEMA,
        "work_id": work_id,
        "prefix_id": prefix_id,
        "checkpoint": int(checkpoint),
        "visible_range_end": int(checkpoint),
        "registration_checkpoint": (
            int(registration_checkpoint) if registration_checkpoint is not None else int(checkpoint)
        ),
        "unit_count": len(entries),
        "visible_unit_count": len(visible),
        "units": entries,
        "units_sha256": _sha(json.dumps(entries, ensure_ascii=False, sort_keys=True)),
    }


def render_source_units(units: Mapping[int, str], checkpoint: int) -> str:
    """把可见单元渲染成带稳定 ID 的原文块。

    单元标签不是原文的一部分——渲染时另起一行标注，
    定位时必须回到**原始单元文本**上，不能把标签算进坐标。
    """
    parts = []
    for u in sorted(int(k) for k in units):
        if u > int(checkpoint):
            break
        parts.append(f"[unit_id={u}]\n{units[u]}")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 宿主定位
# --------------------------------------------------------------------------

class LocateError(ValueError):
    """定位失败：歧义、越界或不存在。"""


def locate_quote_in_unit(unit_text: str, quote: str) -> tuple[int, int]:
    """在单元原文里定位引文，返回唯一 `[start, end)`。

    歧义（多处命中）、不存在都抛错——**不默取第一个命中**。
    """
    q = (quote or "").strip()
    if not q:
        raise LocateError("empty_quote")
    positions: list[int] = []
    start = 0
    while True:
        idx = unit_text.find(q, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + 1
    if not positions:
        raise LocateError("quote_not_found")
    if len(positions) > 1:
        raise LocateError(f"quote_not_unique:{len(positions)}")
    pos = positions[0]
    return pos, pos + len(q)


def locate_citation(
    *,
    units: Mapping[int, str],
    source_unit_id: Any,
    anchor_quote: str,
    checkpoint: int,
) -> dict:
    """定位一条引用，返回跨度或失败原因。"""
    try:
        uid = int(source_unit_id)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_unit_id", "detail": str(source_unit_id)}
    if uid not in units:
        return {"ok": False, "reason": "unit_not_found", "unit_id": uid}
    if uid > int(checkpoint):
        return {"ok": False, "reason": "unit_out_of_visible_range", "unit_id": uid}
    try:
        s, e = locate_quote_in_unit(units[uid], anchor_quote)
    except LocateError as exc:
        return {"ok": False, "reason": str(exc), "unit_id": uid}
    return {
        "ok": True,
        "unit_id": uid,
        "start": s,
        "end": e,
        "chars": e - s,
        "quote_sha256": _sha(anchor_quote),
    }


# --------------------------------------------------------------------------
# 输出解析与 schema 转换
# --------------------------------------------------------------------------

REQUIRED_ITEM_FIELDS = (
    "clue",
    "question",
    "answer_criteria",
    "implication",
    "motif_key",
    "source_unit_id",
    "anchor_quote",
)


def parse_generator_output(raw: str) -> dict:
    """解析生成器输出。

    输出结构损坏 → `parse_ok=False`（走格式重试规则）；
    原文不支持、位置不合格是**质量结果**，不以"格式重试"名义重新生成。
    """
    text = (raw or "").strip()
    if not text:
        return {"parse_ok": False, "reason": "empty_response", "complete": None, "items": []}
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"parse_ok": False, "reason": f"invalid_json:{exc.msg}", "complete": None, "items": []}
    if not isinstance(obj, dict):
        return {"parse_ok": False, "reason": "not_an_object", "complete": None, "items": []}
    complete = obj.get("complete")
    raw_items = obj.get("foreshadowing")
    if raw_items is None:
        return {"parse_ok": False, "reason": "missing_foreshadowing", "complete": complete, "items": []}
    if not isinstance(raw_items, list):
        return {"parse_ok": False, "reason": "foreshadowing_not_a_list", "complete": complete, "items": []}
    items = []
    for i, it in enumerate(raw_items):
        if not isinstance(it, dict):
            items.append({"index": i, "ok": False, "reason": "item_not_an_object"})
            continue
        missing = [f for f in REQUIRED_ITEM_FIELDS if not str(it.get(f) or "").strip()]
        if missing:
            items.append({"index": i, "ok": False, "reason": "missing_fields:" + ",".join(missing)})
            continue
        items.append(
            {
                "index": i,
                "ok": True,
                "item_id": f"i{i}",
                "clue": str(it.get("clue")),
                "question": str(it.get("question")),
                "answer_criteria": str(it.get("answer_criteria")),
                "implication": str(it.get("implication")),
                "motif_key": str(it.get("motif_key")),
                "source_unit_id": it.get("source_unit_id"),
                "anchor_quote": str(it.get("anchor_quote")),
            }
        )
    return {"parse_ok": True, "complete": complete, "items": items}


def convert_to_host_schema(
    parsed: Mapping[str, Any],
    *,
    units: Mapping[int, str],
    checkpoint: int,
    prefix_id: str,
    registration_checkpoint: int,
) -> list[dict]:
    """把模型输出转成宿主条目：追加精确跨度与冻结信息。

    模型原始响应**不因宿主追加而改变**——跨度、item_id、prefix_id
    都是宿主追加的字段。
    """
    out = []
    for it in parsed.get("items") or []:
        if not it.get("ok"):
            out.append(
                {
                    "item_id": it.get("item_id") or f"i{it.get('index')}",
                    "ok": False,
                    "reason": it.get("reason"),
                }
            )
            continue
        loc = locate_citation(
            units=units,
            source_unit_id=it.get("source_unit_id"),
            anchor_quote=it.get("anchor_quote"),
            checkpoint=checkpoint,
        )
        row = {
            "item_id": it.get("item_id"),
            "question": it.get("question"),
            "answer_criteria": it.get("answer_criteria"),
            "implication": it.get("implication"),
            "motif_key": it.get("motif_key"),
            "source_unit_id": it.get("source_unit_id"),
            "anchor_quote": it.get("anchor_quote"),
            "visible_prefix_id": prefix_id,
            "registration_checkpoint": registration_checkpoint,
            "visible_range_end": int(checkpoint),
        }
        if loc.get("ok"):
            row.update(
                {
                    "ok": True,
                    "citation_start": loc["start"],
                    "citation_end": loc["end"],
                    "citation_chars": loc["chars"],
                    "quote_sha256": loc["quote_sha256"],
                }
            )
        else:
            row.update({"ok": False, "reason": loc.get("reason"), "detail": loc.get("detail")})
        out.append(row)
    return out


# --------------------------------------------------------------------------
# 预算
# --------------------------------------------------------------------------

class Budget:
    """预算记账。**重试也占预算**；语义不满意不重试。"""

    def __init__(
        self,
        *,
        max_attempts: int = 42,
        max_input_tokens: int = 336000,
        max_output_tokens: int = 84000,
        per_request_input_limit: int = 20000,
        per_request_output_limit: int = 2000,
    ) -> None:
        self.max_attempts = int(max_attempts)
        self.max_input_tokens = int(max_input_tokens)
        self.max_output_tokens = int(max_output_tokens)
        self.per_request_input_limit = int(per_request_input_limit)
        self.per_request_output_limit = int(per_request_output_limit)
        self.attempts = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def precheck(self, input_tokens: int, output_tokens: int) -> dict:
        """token 预检：装不下就别发。

        传入的应是**完整请求的保守上界**（见 `request_token_bound`），
        不是粗估——粗估不能证明不超限。
        """
        if int(input_tokens) > self.per_request_input_limit:
            return {
                "ok": False,
                "reason": "per_request_input_limit_exceeded",
                "input_tokens": int(input_tokens),
                "limit": self.per_request_input_limit,
            }
        if int(output_tokens) > self.per_request_output_limit:
            return {
                "ok": False,
                "reason": "per_request_output_limit_exceeded",
                "output_tokens": int(output_tokens),
                "limit": self.per_request_output_limit,
            }
        if self.attempts + 1 > self.max_attempts:
            return {"ok": False, "reason": "max_attempts_reached"}
        if self.input_tokens + int(input_tokens) > self.max_input_tokens:
            return {"ok": False, "reason": "max_input_tokens_reached"}
        if self.output_tokens + int(output_tokens) > self.max_output_tokens:
            return {"ok": False, "reason": "max_output_tokens_reached"}
        return {"ok": True}

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.attempts += 1
        self.input_tokens += int(input_tokens)
        self.output_tokens += int(output_tokens)

    def load_archive(self, archive_path: Path | str) -> dict:
        """从用量台账中加载已发生的消耗与在途预留。

        严格校验：文件损坏或非合法 JSON 抛出 ValueError，
        绝不静默重置为空台账继续发送。
        """
        p = Path(archive_path)
        if not p.is_file():
            return {"loaded": False, "reason": "file_not_found"}
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"台账文件损坏或非合法JSON，拒绝继续请求：{p}") from exc
        if not isinstance(doc, dict):
            raise ValueError(f"台账格式错误（非字典对象），拒绝继续请求：{p}")
        if doc.get("schema") != "registration_usage_archive_v1":
            raise ValueError(f"台账 schema 不匹配（{doc.get('schema')}），拒绝继续请求：{p}")
        records = doc.get("records")
        if not isinstance(records, list):
            raise ValueError(f"台账 records 字段缺失或非列表，拒绝继续请求：{p}")
        reservations = doc.get("reservations") or []
        if not isinstance(reservations, list):
            raise ValueError(f"台账 reservations 字段非列表，拒绝继续请求：{p}")

        def _rec_in(r: dict) -> int:
            if r.get("usage_status") == "unknown":
                return int(r.get("occupied_input_tokens") or 0)
            val = r.get("input_tokens")
            return int(val if val is not None else (r.get("occupied_input_tokens") or 0))

        def _rec_out(r: dict) -> int:
            if r.get("usage_status") == "unknown":
                return int(r.get("occupied_output_tokens") or 0)
            val = r.get("output_tokens")
            return int(val if val is not None else (r.get("occupied_output_tokens") or 0))

        self.attempts = len(records) + len(reservations)
        self.input_tokens = sum(_rec_in(r) for r in records) + sum(
            int(r.get("projected_input_tokens") or 0) for r in reservations
        )
        self.output_tokens = sum(_rec_out(r) for r in records) + sum(
            int(r.get("projected_output_tokens") or 0) for r in reservations
        )
        return {
            "loaded": True,
            "attempts": self.attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    def summary(self) -> dict:
        return {
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "input_tokens": self.input_tokens,
            "max_input_tokens": self.max_input_tokens,
            "output_tokens": self.output_tokens,
            "max_output_tokens": self.max_output_tokens,
            "attempts_remaining": max(0, self.max_attempts - self.attempts),
        }


def estimate_tokens(text: str) -> int:
    """粗估 token 数。

    这是**初筛**用的估计，不是计费依据，也不是硬上限的依据；
    实际用量以模型返回的 token 计数为准。
    """
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    other = len(text) - ascii_chars
    # 英文按 ~4 字符/token，其他按 ~1.7 字符/token（中文常用字偏密）
    return int(ascii_chars / 4 + other / 1.7) + 1


def conservative_token_bound(text: str, model: str = "") -> int:
    """**保守上界**，用于硬上限预检。

    系数由端点实测标定：
    1. 默认 / `codex/gpt-6-luna`：
       参考发现请求 p1 = 4371 个非 ASCII 字符 + 1127 个 ASCII 字符，端点报告 7675 输入 token。
       即非 ASCII 约 1.69 token/字符，取 1.75 / 0.30 作保守系数。
    2. `cn:deepseek-v4.1-flash`（2026-11-07 切换）：
       实测 game/p2 (6990 字符) 报 4573 token (~0.54 token/字符)，
       game/p3 (10133 字符) 报 6372 token (~0.55 token/字符)。
       为避免长请求在盲审阶段被 1.75 系数误拦，对 deepseek 端点标定 0.85 / 0.30 作保守上界。
    """
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    other = len(text) - ascii_chars
    m = (model or "").lower()
    if "deepseek" in m:
        return int(other * 0.85 + ascii_chars * 0.30) + 16
    return int(other * 1.75 + ascii_chars * 0.30) + 16


def request_token_bound(*parts: str, model: str = "") -> int:
    """完整请求的保守上界。

    必须把**实际要发的全部内容**都算进来——包括盲审阶段
    送入的参考条目与系统输出；只看初筛时的原文前缀会漏掉输入膨胀。
    """
    return sum(conservative_token_bound(p or "", model=model) for p in parts)


async def run_guarded_llm_request(
    *,
    client: Any,
    archive_path: Path | str,
    work_id: str,
    prefix_id: str,
    arm: str,
    stage: str,
    messages: list[dict],
    projected_input_tokens: int,
    projected_output_tokens: int,
    request_sha: str,
    max_runner_attempts: int = 1,
    max_attempts_cap: int = 42,
    max_input_tokens_cap: int = 336000,
    max_output_tokens_cap: int = 84000,
    per_request_input_limit: int = 20000,
    per_request_output_limit: int = 2000,
    extra: dict | None = None,
) -> dict:
    """受控 LLM 请求执行器：严格单次预留与原子结算，禁止隐式网络重试。

    每次尝试必须在发送前经 UsageArchive.transaction 取得预留；
    若发生超时/网络异常/无 usage 响应，一律原子沉淀为 unknown 并保留占用；
    外层若配置了重试，必须由本循环在下一次发送前重新申请预留与限额检查。
    """
    from backend.common.registration_usage_archive import UsageArchive

    last_res = None
    last_error = None
    records_settled = []

    for attempt_idx in range(1, max_runner_attempts + 1):
        # 1. 事务预留名额与预算
        with UsageArchive.transaction(archive_path) as archive:
            resv = archive.reserve(
                work_id=work_id,
                prefix_id=prefix_id,
                arm=arm,
                stage=stage,
                projected_input_tokens=projected_input_tokens,
                projected_output_tokens=projected_output_tokens,
                max_attempts=max_attempts_cap,
                max_input_tokens=max_input_tokens_cap,
                max_output_tokens=max_output_tokens_cap,
                per_request_input_limit=per_request_input_limit,
                per_request_output_limit=per_request_output_limit,
            )
            if not resv["ok"]:
                return {
                    "ok": False,
                    "reason": "budget_blocked",
                    "detail": resv,
                    "attempt_idx": attempt_idx,
                    "records_settled": records_settled,
                    "last_res": last_res,
                    "last_error": last_error,
                }
            reservation_id = resv["reservation_id"]

        # 2. 单次真实传输（关闭一切隐式重试）
        call_error = None
        res = None
        try:
            if hasattr(client, "chat_single"):
                res = await client.chat_single(messages=messages, max_tokens=projected_output_tokens)
            else:
                res = await client.chat_auto(messages=messages, max_tokens=projected_output_tokens, disable_retries=True)
        except Exception as exc:
            call_error = exc

        # 3. 结果判别与原子结算
        if call_error is not None:
            with UsageArchive.transaction(archive_path) as archive:
                rec = archive.settle_reservation(
                    reservation_id,
                    request_sha256=request_sha,
                    model=getattr(client.config, "model", "unknown"),
                    usage_status="unknown",
                    success=False,
                    error=str(call_error),
                    sampling={
                        "temperature": float(getattr(client.config, "temperature", 0) or 0),
                        "reasoning_effort": str(getattr(client.config, "reasoning_effort", "") or ""),
                    },
                    extra=extra,
                )
                records_settled.append(rec)
            last_error = str(call_error)
            continue

        success = bool(getattr(res, "success", False))
        tokens = getattr(res, "tokens", None)
        has_tokens = (
            tokens is not None
            and isinstance(tokens, (tuple, list))
            and len(tokens) >= 2
            and tokens[0] is not None
            and (tokens[0] > 0 or tokens[1] > 0)
        )

        if success:
            if has_tokens:
                usage_status = "measured"
                in_tok = int(tokens[0])
                out_tok = int(tokens[1] or 0)
            else:
                # 缺失 usage 的成功响应也保持 unknown
                usage_status = "unknown"
                in_tok = None
                out_tok = None

            with UsageArchive.transaction(archive_path) as archive:
                rec = archive.settle_reservation(
                    reservation_id,
                    request_sha256=request_sha,
                    model=getattr(client.config, "model", "unknown"),
                    usage_status=usage_status,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    success=True,
                    error=None,
                    sampling={
                        "temperature": float(getattr(client.config, "temperature", 0) or 0),
                        "reasoning_effort": str(getattr(client.config, "reasoning_effort", "") or ""),
                    },
                    extra=extra,
                )
                records_settled.append(rec)

            return {
                "ok": True,
                "result": res,
                "attempt_idx": attempt_idx,
                "usage_status": usage_status,
                "records_settled": records_settled,
            }
        else:
            if has_tokens:
                usage_status = "measured"
                in_tok = int(tokens[0])
                out_tok = int(tokens[1] or 0)
            else:
                usage_status = "unknown"
                in_tok = None
                out_tok = None

            with UsageArchive.transaction(archive_path) as archive:
                rec = archive.settle_reservation(
                    reservation_id,
                    request_sha256=request_sha,
                    model=getattr(client.config, "model", "unknown"),
                    usage_status=usage_status,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    success=False,
                    error=getattr(res, "error", None) or "call_failed",
                    sampling={
                        "temperature": float(getattr(client.config, "temperature", 0) or 0),
                        "reasoning_effort": str(getattr(client.config, "reasoning_effort", "") or ""),
                    },
                    extra=extra,
                )
                records_settled.append(rec)
            last_res = res
            last_error = getattr(res, "error", None) or "call_failed"
            continue

    return {
        "ok": False,
        "reason": "attempts_exhausted",
        "last_error": last_error,
        "last_res": last_res,
        "attempt_idx": max_runner_attempts,
        "records_settled": records_settled,
    }
