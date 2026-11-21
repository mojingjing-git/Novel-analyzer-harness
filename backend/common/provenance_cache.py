"""T3: Authentic Request Identity, Provenance Verification, and Two-Layer Cache Contract.

Enforces strict separation between:
1. Raw model response cache (`raw_response_text` + full request identity recorded at call time)
2. Adjudication result cache (`decision`, `assessment_status`, `reasons`, `code_snapshot_id`, `adjudication_version`)
3. Independent label binding (`label_sample_id` bound to question, criteria, answer, quote, chapter, and evidence_fingerprint)

Guarantees:
- Never overwrites current identity hashes onto historical cache records to fake verification.
- Historical records missing `raw_response_text` or call-time provenance hashes are classified as `unverifiable`.
- Any change to answer, evidence, prompt, model config, or contract version triggers `mismatch` and blocks reuse.
- When request identity matches but adjudication code (`code_snapshot_id`) changes, re-adjudicates from `raw_response_text` and preserves `adjudication_history`.
- Sanitizes model/service configuration summaries so API keys or secrets are never stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from backend.common.c_claim_audit_gate import (
    base_sample_fingerprint,
    compute_gate_code_snapshot,
    evidence_fingerprint,
    extract_json_object,
    gate,
    sample_fingerprint,
)

PROVENANCE_SCHEMA_VERSION = "provenance_cache_v1"

FORBIDDEN_SECRET_KEYS = frozenset(
    {"api_key", "apikey", "key", "secret", "token", "password", "authorization", "bearer"}
)


def sanitize_model_config(config: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Extract a deterministic, secret-free model & sampling configuration summary."""
    if hasattr(config, "__dict__") and not isinstance(config, Mapping):
        raw_map: Mapping[str, Any] = vars(config)
    elif isinstance(config, Mapping):
        raw_map = config
    else:
        raw_map = {}

    base_url = str(raw_map.get("base_url") or "")
    host = ""
    if base_url:
        try:
            parsed = urlparse(base_url)
            host = parsed.netloc or parsed.path
        except ValueError:
            host = "redacted_url"

    sanitized = {
        "model": str(raw_map.get("model") or ""),
        "endpoint_host": host,
        "temperature": float(raw_map.get("temperature", 0) or 0),
        "reasoning_effort": str(raw_map.get("reasoning_effort") or ""),
        "max_tokens": int(raw_map.get("max_tokens", 0) or 0),
    }
    for k in list(sanitized.keys()):
        if k.lower() in FORBIDDEN_SECRET_KEYS:
            sanitized.pop(k, None)
    return sanitized


def build_request_identity(
    item: Mapping[str, Any],
    *,
    prompt: str,
    model_config: Mapping[str, Any] | Any,
    contract_version: str,
    code_snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Build complete, deterministic identity for an audit/evaluation request."""
    item_with_contract = dict(item)
    if contract_version and contract_version != "current":
        item_with_contract["evidence_contract_version"] = contract_version
    else:
        item_with_contract.pop("evidence_contract_version", None)

    inp_fp = sample_fingerprint(item_with_contract)
    base_inp_fp = base_sample_fingerprint(item_with_contract)
    ev_fp = evidence_fingerprint(item_with_contract.get("evidence_segments"))
    ans_sha = hashlib.sha256(str(item_with_contract.get("answer") or "").encode("utf-8")).hexdigest()
    q_sha = hashlib.sha256(str(item_with_contract.get("quote") or "").encode("utf-8")).hexdigest()
    prompt_sha = hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()

    sanitized_cfg = sanitize_model_config(model_config)
    cfg_raw = json.dumps(sanitized_cfg, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    cfg_sha = hashlib.sha256(cfg_raw.encode("utf-8")).hexdigest()

    snap = code_snapshot_id or compute_gate_code_snapshot()

    req_payload = {
        "input_fingerprint": inp_fp,
        "prompt_sha256": prompt_sha,
        "evidence_fingerprint": ev_fp,
        "answer_sha256": ans_sha,
        "quote_sha256": q_sha,
        "model_config_sha256": cfg_sha,
        "contract_version": str(contract_version or "current"),
    }
    req_id = hashlib.sha256(
        json.dumps(req_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "request_id": req_id,
        "input_fingerprint": inp_fp,
        "base_input_fingerprint": base_inp_fp,
        "prompt_sha256": prompt_sha,
        "evidence_fingerprint": ev_fp,
        "answer_sha256": ans_sha,
        "quote_sha256": q_sha,
        "model_config_digest": sanitized_cfg,
        "model_config_sha256": cfg_sha,
        "contract_version": str(contract_version or "current"),
        "code_snapshot_id": snap,
    }


@dataclass(frozen=True)
class CacheVerificationResult:
    status: str  # "verified_hit" | "re_adjudicate_needed" | "mismatch" | "unverifiable" | "failed_or_incomplete"
    reusable: bool
    can_re_adjudicate: bool
    reason: str
    mismatched_fields: tuple[str, ...] = field(default_factory=tuple)


def verify_cache_entry(
    entry: Mapping[str, Any] | None,
    expected_identity: Mapping[str, Any],
) -> CacheVerificationResult:
    """Verify a cached record against expected request identity BEFORE reuse.

    Never mutates `entry` or backfills missing provenance fields.
    """
    if not isinstance(entry, Mapping):
        return CacheVerificationResult(
            status="failed_or_incomplete",
            reusable=False,
            can_re_adjudicate=False,
            reason="entry_not_mapping",
        )

    if (
        entry.get("status") in ("failed", "failed_or_incomplete", "error")
        or entry.get("provider_filtered") is True
        or entry.get("measured_live") is False
        or not entry.get("decision")
    ):
        return CacheVerificationResult(
            status="failed_or_incomplete",
            reusable=False,
            can_re_adjudicate=False,
            reason="entry_failed_or_incomplete",
        )

    # Check if authentic call-time provenance and raw response exist
    raw_resp = entry.get("raw_response_text")
    has_call_time_flag = entry.get("provenance_recorded_at_call_time") is True
    required_prov_keys = (
        "request_id",
        "input_fingerprint",
        "prompt_sha256",
        "evidence_fingerprint",
        "answer_sha256",
        "model_config_sha256",
        "contract_version",
    )
    missing_keys = [k for k in required_prov_keys if not entry.get(k)]
    if not isinstance(raw_resp, str) or not raw_resp.strip() or not has_call_time_flag or missing_keys:
        return CacheVerificationResult(
            status="unverifiable",
            reusable=False,
            can_re_adjudicate=False,
            reason="missing_raw_response_or_call_time_provenance",
        )

    mismatched: list[str] = []
    for k in (
        "answer_sha256",
        "quote_sha256",
        "evidence_fingerprint",
        "input_fingerprint",
        "prompt_sha256",
        "model_config_sha256",
        "contract_version",
    ):
        exp_val = expected_identity.get(k)
        got_val = entry.get(k)
        if exp_val and got_val != exp_val:
            mismatched.append(k)

    if mismatched:
        return CacheVerificationResult(
            status="mismatch",
            reusable=False,
            can_re_adjudicate=False,
            reason=f"identity_mismatch:{','.join(mismatched)}",
            mismatched_fields=tuple(mismatched),
        )

    if entry.get("request_id") != expected_identity.get("request_id"):
        return CacheVerificationResult(
            status="mismatch",
            reusable=False,
            can_re_adjudicate=False,
            reason="request_id_mismatch",
            mismatched_fields=("request_id",),
        )

    if entry.get("code_snapshot_id") != expected_identity.get("code_snapshot_id"):
        return CacheVerificationResult(
            status="re_adjudicate_needed",
            reusable=False,
            can_re_adjudicate=True,
            reason="code_snapshot_changed",
        )

    return CacheVerificationResult(
        status="verified_hit",
        reusable=True,
        can_re_adjudicate=False,
        reason="verified_exact_match",
    )


def build_verified_cache_record(
    item: Mapping[str, Any],
    identity: Mapping[str, Any],
    *,
    raw_response_text: str,
    gate_result: Mapping[str, Any] | None,
    tokens: int,
    seconds: float,
    attempts: int,
    success: bool,
    error: str | None = None,
) -> dict[str, Any]:
    """Create a new cache record with full call-time provenance and raw response."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    if not success or not isinstance(raw_response_text, str) or not raw_response_text.strip() or not gate_result:
        return {
            "fs_id": item.get("fs_id") or item.get("candidate_id") or "",
            "status": "failed_or_incomplete",
            "provenance_recorded_at_call_time": True,
            **dict(identity),
            "raw_response_text": raw_response_text or "",
            "decision": None,
            "assessment_status": "unconfirmed",
            "reasons": [error or "call_failed_or_empty_response"],
            "tokens": int(tokens or 0),
            "seconds": round(float(seconds or 0.0), 3),
            "attempts": int(attempts or 1),
            "measured_live": False,
            "recorded_at": now_iso,
        }

    adjudication = {
        "adjudication_version": 1,
        "code_snapshot_id": identity["code_snapshot_id"],
        "decision": gate_result["decision"],
        "assessment_status": gate_result.get("assessment_status", "unconfirmed"),
        "reasons": list(gate_result.get("reasons", [])),
        "claim_check": gate_result.get("claim_check"),
        "adjudicated_at": now_iso,
    }
    return {
        "fs_id": item.get("fs_id") or item.get("candidate_id") or "",
        "status": "ok",
        "provenance_recorded_at_call_time": True,
        **dict(identity),
        "raw_response_text": raw_response_text,
        "decision": adjudication["decision"],
        "assessment_status": adjudication["assessment_status"],
        "reasons": adjudication["reasons"],
        "claim_check": adjudication["claim_check"],
        "adjudication_version": 1,
        "adjudication_history": [adjudication],
        "tokens": int(tokens or 0),
        "seconds": round(float(seconds or 0.0), 3),
        "attempts": int(attempts or 1),
        "measured_live": True,
        "recorded_at": now_iso,
    }


def re_adjudicate_cached_record(
    entry: Mapping[str, Any],
    item: Mapping[str, Any],
    expected_identity: Mapping[str, Any],
    *,
    gate_fn: Any = None,
    ordering_fn: Any = None,
) -> dict[str, Any]:
    """Re-adjudicate an existing verified raw response under new adjudication rules/code snapshot.

    ``ordering_fn``：可选钩子，签名 ``(item, nli_obj) -> dict | None``。
    时序判定依赖原文与实际引用跨度，两者都不在请求身份里——
    重裁必须用**最新**的时序结果，否则原文变了旧结论仍可能被沿用。
    """
    check = verify_cache_entry(entry, expected_identity)
    if check.status not in ("re_adjudicate_needed", "verified_hit"):
        raise ValueError(f"Cannot re-adjudicate cache entry with status={check.status}: {check.reason}")

    raw_text = str(entry["raw_response_text"])
    nli_obj = extract_json_object(raw_text)
    arm_item = dict(item)
    contract = expected_identity.get("contract_version", "current")
    if contract and contract != "current":
        arm_item["evidence_contract_version"] = contract
    else:
        arm_item.pop("evidence_contract_version", None)

    if ordering_fn is not None:
        ordering = ordering_fn(arm_item, nli_obj)
        if ordering is not None:
            arm_item["evidence_ordering"] = ordering

    active_gate = gate_fn or gate
    g = active_gate(arm_item, nli_obj, None if nli_obj is not None else "invalid_json_in_raw_response")

    prev_history = list(entry.get("adjudication_history") or [])
    if not prev_history and entry.get("decision"):
        prev_history.append(
            {
                "adjudication_version": int(entry.get("adjudication_version", 1) or 1),
                "code_snapshot_id": entry.get("code_snapshot_id", "unknown"),
                "decision": entry.get("decision"),
                "assessment_status": entry.get("assessment_status", "unconfirmed"),
                "reasons": list(entry.get("reasons", [])),
                "claim_check": entry.get("claim_check"),
                "adjudicated_at": entry.get("recorded_at", ""),
            }
        )

    new_ver = len(prev_history) + 1
    now_iso = datetime.now().isoformat(timespec="seconds")
    new_adj = {
        "adjudication_version": new_ver,
        "code_snapshot_id": expected_identity["code_snapshot_id"],
        "decision": g["decision"],
        "assessment_status": g.get("assessment_status", "unconfirmed"),
        "reasons": list(g.get("reasons", [])),
        "claim_check": g.get("claim_check"),
        "adjudicated_at": now_iso,
    }
    updated = dict(entry)
    updated["code_snapshot_id"] = expected_identity["code_snapshot_id"]
    updated["decision"] = new_adj["decision"]
    updated["assessment_status"] = new_adj["assessment_status"]
    updated["reasons"] = new_adj["reasons"]
    updated["claim_check"] = new_adj["claim_check"]
    updated["adjudication_version"] = new_ver
    updated["adjudication_history"] = prev_history + [new_adj]
    return updated


def compute_label_binding_fingerprint(item: Mapping[str, Any]) -> str:
    """Compute a deterministic fingerprint over the exact fields a human/independent label binds to."""
    payload = {
        "fs_id": item.get("fs_id") or item.get("candidate_id") or "",
        "question": item.get("question") or "",
        "criteria": item.get("criteria") or item.get("answer_criteria") or "",
        "description": item.get("description") or "",
        "anchor_quote": item.get("anchor_quote") or "",
        "anchor_chapter": item.get("anchor_chapter"),
        "answer": item.get("answer") or "",
        "quote": item.get("quote") or item.get("supporting_quote") or "",
        "chapter": item.get("chapter"),
        "prereg": item.get("prereg"),
        "evidence_fingerprint": evidence_fingerprint(item.get("evidence_segments")),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_label_binding(item: Mapping[str, Any]) -> dict[str, Any]:
    """Verify whether an item's label is authentically bound to its current question, criteria, answer, and evidence."""
    lbl = item.get("label")
    if not lbl:
        return {"label_status": "unlabeled", "eligible_for_official_metrics": False, "reason": "no_label"}
    bound_id = item.get("label_sample_id")
    if not bound_id:
        return {"label_status": "unbound", "eligible_for_official_metrics": False, "reason": "missing_label_sample_id"}

    cur_sample_fp = sample_fingerprint(item)
    cur_base_fp = base_sample_fingerprint(item)
    cur_lbl_fp = compute_label_binding_fingerprint(item)

    if bound_id in (cur_sample_fp, cur_base_fp, cur_lbl_fp):
        return {
            "label_status": "bound",
            "eligible_for_official_metrics": True,
            "reason": "verified_bound",
            "bound_fingerprint": bound_id,
        }
    return {
        "label_status": "stale",
        "eligible_for_official_metrics": False,
        "reason": "label_bound_to_old_answer_or_evidence",
        "bound_fingerprint": bound_id,
        "current_fingerprint": cur_base_fp,
    }


def atomic_save_json(path: Path, payload: Any) -> None:
    """Atomically write JSON to disk after validating serialization so interrupted writes leave no partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    # Validate round-trip parse before touching disk
    json.loads(serialized)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(serialized, encoding="utf-8")
    tmp.replace(path)
