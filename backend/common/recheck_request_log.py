"""初始复检的请求落盘（行车记录仪）。

目的：把"没找到 / 没送到 / 没判断对"三者分开。
- 旧运行只留下缓存里的哈希，且缓存里存的是**单候选**渲染的哈希，
  而实际调用发的是**批次**提示词——两者对不上，无法据此复原历史请求。
- 本模块在**实际调用处**记录：最终提示词、证据包、各段哈希，
  以及作品、运行批次、候选 id、代码版本。

它只记录，不参与判定：不影响模型调用次数，也不改变任何 verdict。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "recheck_request_log_v1"


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def compute_recheck_code_snapshot(contract_version: str = "recheck_v3") -> str:
    """计算当前复检引擎源码字节的 SHA-256 前 16 位（仅路径哈希，不跨层 import services）。

    兼容单文件模块与子包目录：子包按相对 POSIX 路径升序迭代所有 ``*.py``，
    并对换行符做 ``\\r\\n -> \\n`` 归一化以保持跨平台确定性。
    """
    is_v2 = "v2" in (contract_version or "").lower()
    services_dir = Path(__file__).resolve().parents[1] / "services"
    pkg_dir = services_dir / ("recheck_v2" if is_v2 else "recheck_v3")
    file_path = services_dir / ("recheck_v2.py" if is_v2 else "recheck_v3.py")
    try:
        if pkg_dir.is_dir():
            py_files = sorted(pkg_dir.rglob("*.py"), key=lambda p: p.relative_to(pkg_dir).as_posix())
            if not py_files:
                return "unknown"
            h = hashlib.sha256()
            for p in py_files:
                rel = p.relative_to(pkg_dir).as_posix().encode("utf-8")
                normalized = p.read_bytes().replace(b"\r\n", b"\n")
                h.update(rel + b"\0" + normalized + b"\0")
            return h.hexdigest()[:16]
        normalized = file_path.read_bytes().replace(b"\r\n", b"\n")
        return hashlib.sha256(normalized).hexdigest()[:16]
    except OSError:
        return "unknown"


def make_run_identity(
    *,
    work_id: str,
    output_dir: str,
    code_snapshot_id: str,
    model_config: Mapping[str, Any] | None = None,
    contract_version: str = "recheck_v2",
) -> dict:
    return {
        "work_id": work_id,
        "output_dir": output_dir,
        "code_snapshot_id": code_snapshot_id,
        "contract_version": contract_version,
        "model_config": dict(model_config or {}),
    }


def build_request_record(
    *,
    run: Mapping[str, Any],
    batch_index: int,
    round_index: int,
    candidate_ids: list[str],
    prompt: str,
    evidence_batches: list[dict],
    attempt_records: list[dict],
    contract_version: str = "recheck_v2",
) -> dict:
    """构造一条请求记录。

    `evidence_batches` 每项：candidate_id / evidence_id / chapter / text / text_sha256。
    `attempt_records` 每项：attempt / seconds / response_text / response_sha256 / error / usage。
    """
    prompt_sha = sha256_text(prompt)
    ev_fp_by_cid = {
        c["candidate_id"]: sha256_text(
            "\n".join(
                f"{s.get('evidence_id')}\n{s.get('text')}"
                for s in c.get("segments") or []
            )
        )
        for c in evidence_batches
    }
    request_id = sha256_text(
        json.dumps(
            {
                "prompt_sha256": prompt_sha,
                "candidate_ids": list(candidate_ids),
                "evidence": ev_fp_by_cid,
                "code_snapshot_id": run.get("code_snapshot_id"),
                "contract_version": contract_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return {
        "schema": SCHEMA,
        "request_id": request_id,
        "recorded_at": None,  # 由落盘方填时间戳
        "run": dict(run),
        "batch_index": batch_index,
        "round_index": round_index,
        "candidate_ids": list(candidate_ids),
        "prompt": prompt,
        "prompt_sha256": prompt_sha,
        "evidence_batches": evidence_batches,
        "evidence_fingerprint_by_candidate": ev_fp_by_cid,
        "attempts": attempt_records,
        "contract_version": contract_version,
    }


def verify_request_record(record: Mapping[str, Any], renderer) -> dict:
    """用记录里的证据包重建提示词，校验哈希是否一致。

    `renderer(candidates)` 由调用方提供——必须与实际发请求时用的是**同一个**渲染函数，
    否则重建出来的提示词与历史请求不可比。

    返回 {matched, rebuilt_prompt_sha256, recorded_prompt_sha256, reason}。
    """
    recorded_sha = str(record.get("prompt_sha256") or "")
    if not recorded_sha:
        return {
            "matched": False,
            "recorded_prompt_sha256": "",
            "rebuilt_prompt_sha256": "",
            "reason": "record_has_no_prompt_sha256",
        }
    evidence_by_cid: dict[str, list[dict]] = {}
    for e in record.get("evidence_batches") or []:
        evidence_by_cid.setdefault(str(e.get("candidate_id")), []).append(
            {
                "evidence_id": e.get("evidence_id"),
                "chapter": e.get("chapter"),
                "text": e.get("text"),
            }
        )
    candidates = []
    for meta in record.get("candidate_meta") or []:
        c = dict(meta)
        c["segments"] = evidence_by_cid.get(str(meta.get("candidate_id")), [])
        candidates.append(c)
    if not candidates:
        return {
            "matched": False,
            "recorded_prompt_sha256": recorded_sha,
            "rebuilt_prompt_sha256": "",
            "reason": "record_has_no_candidate_meta",
        }
    try:
        rebuilt = renderer(candidates)
    except Exception as exc:  # noqa: BLE001
        return {
            "matched": False,
            "recorded_prompt_sha256": recorded_sha,
            "rebuilt_prompt_sha256": "",
            "reason": f"render_failed:{type(exc).__name__}",
        }
    rebuilt_sha = sha256_text(rebuilt)
    return {
        "matched": rebuilt_sha == recorded_sha,
        "recorded_prompt_sha256": recorded_sha,
        "rebuilt_prompt_sha256": rebuilt_sha,
        "reason": "ok" if rebuilt_sha == recorded_sha else "prompt_rebuild_mismatch",
    }


def save_request_log(path: Path, log: Mapping[str, Any]) -> None:
    """原子落盘。写不动就跳过——记录失败不该让整轮复检崩掉。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        return


def load_request_log(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
