"""整本隔离、确定性原文抽样与曝光记录；不依赖引擎，也不生成真值标签。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import uuid

SCHEMA = "bc_corpus_v1"
SPLITS = {"development", "validation", "holdout"}


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def object_hash(value: object) -> str:
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def work_key(title: str) -> str:
    """Same title/edition defaults to the same work; semantic aliases need plan group_id."""
    normalized = unicodedata.normalize("NFKC", title)
    match = re.search(r"《([^》]+)》", normalized)
    return re.sub(r"\s+", "", match[1] if match else normalized).casefold()


def decode_source(raw: bytes) -> tuple[str, str]:
    # No replacement characters or probabilistic decoder; fail instead of hiding corruption.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            if "\x00" in text:
                raise ValueError("source_contains_nul")
            return text, encoding
        except UnicodeDecodeError:
            pass
    raise ValueError("unsupported_source_encoding")


def sample_spans(text: str, work_id: str, seed: str, count: int, chars: int) -> list[dict]:
    """One uniform hash-derived start in each equal-length stratum, no semantic selection.

    Keep line boundaries when possible, never exceed the stratum. Offsets refer
    to decoded characters (CRLF preserved), not byte offsets or chapter numbers.
    """
    if type(count) is not int or type(chars) is not int or not 1 <= count <= 20 or not 100 <= chars <= 24000:
        raise ValueError("invalid_sampling_budget")
    if len(text) < count * (chars + 2):
        raise ValueError("source_too_short_for_sampling_plan")
    spans = []
    for i in range(count):
        low, high = len(text) * i // count, len(text) * (i + 1) // count
        room = high - low - chars
        offset = int(object_hash([seed, work_id, i]), 16) % (room + 1)
        start = low + offset
        # Only move to a paragraph boundary within 400 chars; do not hunt for content.
        newline = text.find("\n", start, min(start + 400, high - chars))
        if newline >= 0:
            start = newline + 1
        end = start + chars
        newline = text.find("\n", end, min(end + 400, high))
        if newline >= 0:
            end = newline + 1
        body = text[start:end]
        spans.append({"stratum": i, "span": [start, end], "text_sha256": digest(body),
                      "starts_at_line_boundary": start == 0 or text[start - 1] == "\n",
                      "ends_at_line_boundary": end == len(text) or text[end - 1] == "\n",
                      "context_scope": "local_window_not_whole_story"})
    return spans


def freeze_plan(plan: dict, protocol_root: Path) -> dict:
    """Machine reads/hashes sources, but returns NO novel text. Plan fixed before this call."""
    if plan.get("schema") != SCHEMA or not isinstance(plan.get("seed"), str) or not plan["seed"]:
        raise ValueError("invalid_corpus_plan")
    excluded = {work_key(x) for x in plan.get("previously_exposed_titles", [])}
    seen_ids, identities, works = set(), {}, []
    for spec in plan["works"]:
        wid, title, split = spec["work_id"], spec["title"], spec["split"]
        if not isinstance(wid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", wid) or wid in seen_ids:
            raise ValueError("invalid_or_duplicate_work_id")
        if split not in SPLITS or not isinstance(title, str) or not title.strip():
            raise ValueError("invalid_work_metadata")
        seen_ids.add(wid)
        path = Path(spec["path"]).resolve()
        title_keys = {work_key(title), work_key(path.stem)}
        if title_keys & excluded and split != "development":
            raise ValueError("previously_exposed_work_cannot_be_validation_or_holdout")
        if spec.get("prior_exposure", "none_declared") != "none_declared" and split != "development":
            raise ValueError("previous_exposure_requires_development_split")
        raw = path.read_bytes()
        text, encoding = decode_source(raw)
        # Whitespace/width changes are not different works. Revisions/aliases still need group_id.
        text_hash = digest(re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)))
        keys = ["title:" + key for key in title_keys] + ["content:" + text_hash,
                                                        "group:" + spec.get("group_id", work_key(title))]
        for key in keys:
            if key in identities:
                raise ValueError("same_work_or_content_crosses_splits" if identities[key] != split
                                 else "duplicate_work_or_content_in_plan")
            identities[key] = split
        spans = sample_spans(text, wid, plan["seed"], plan["windows_per_work"], plan["window_chars"])
        for row in spans:
            row["sample_id"] = object_hash({"work_id": wid, "source": digest(raw), **row})
        works.append({**spec, "path": str(path), "source_sha256": digest(raw),
                      "normalized_text_sha256": text_hash, "encoding": encoding,
                      "decoded_chars": len(text), "bytes": len(raw), "samples": spans})
    if not works:
        raise ValueError("empty_corpus")
    protocol = []
    root = protocol_root.resolve()
    for relative in plan["protocol_files"]:
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or path.suffix not in {".py", ".md", ".json"}:
            raise ValueError("invalid_protocol_path")
        # Never hash configuration/secrets as part of an experiment bundle.
        if path.name.lower() == "config.json" or ".env" in path.name.lower():
            raise ValueError("secret_configuration_not_allowed")
        protocol.append({"path": relative, "sha256": digest(path.read_bytes())})
    manifest = {"schema": SCHEMA, "plan_sha256": object_hash(plan), "seed": plan["seed"],
                "frozen_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
                "selection_note": plan.get("selection_note", ""), "works": works}
    manifest["manifest_id"] = object_hash(manifest)
    return manifest


def verify_manifest(manifest: dict) -> None:
    body = {k: v for k, v in manifest.items() if k != "manifest_id"}
    if manifest.get("schema") != SCHEMA or manifest.get("manifest_id") != object_hash(body):
        raise ValueError("manifest_changed_after_freeze")


def verify_protocol(manifest: dict, root: Path) -> None:
    for entry in manifest["protocol"]:
        path = (root / entry["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or digest(path.read_bytes()) != entry["sha256"]:
            raise ValueError("protocol_changed_since_freeze")


def export_packets(manifest: dict, work_ids: list[str], bundle_dir: Path, output: Path,
                   *, purpose: str, run_spec: dict | None = None, protocol_root: Path | None = None) -> dict:
    """Writes blind source packets, recording exposure BEFORE any text is exported.

    No labels, predictions or ledger items are read. The holdout can be opened
    once for a frozen final run, or consumed for development (never final after).
    This is an accidental-leakage guard, not a security boundary against file edits.
    """
    verify_manifest(manifest)
    if purpose not in {"validation", "development", "final_evaluation"} or not work_ids or len(set(work_ids)) != len(work_ids):
        raise ValueError("invalid_export_request")
    selected = [w for w in manifest["works"] if w["work_id"] in work_ids]
    if len(selected) != len(work_ids):
        raise ValueError("unknown_work_id")
    if output.exists():
        raise ValueError("output_already_exists")
    if purpose == "validation" and any(w["split"] != "validation" for w in selected):
        raise ValueError("validation_export_requires_validation_split")
    events_dir = bundle_dir / "exposures"
    history = [json.loads(p.read_text(encoding="utf-8")) for p in events_dir.glob("*.json")]
    if purpose == "final_evaluation":
        if any(w["split"] != "holdout" for w in selected):
            raise ValueError("final_evaluation_requires_holdout")
        if any(set(e["work_ids"]) & set(work_ids) for e in history):
            raise ValueError("holdout_already_exposed")
        if not isinstance(run_spec, dict) or not all(run_spec.get(k) for k in
               ("run_id", "model", "prompt_sha256", "max_calls", "max_output_tokens", "reference_policy")):
            raise ValueError("final_evaluation_requires_frozen_run_spec")
        if protocol_root is None:
            raise ValueError("protocol_root_required")
        verify_protocol(manifest, protocol_root)
    packets = []
    for work in selected:
        raw = Path(work["path"]).read_bytes()
        if digest(raw) != work["source_sha256"]:
            raise ValueError("source_changed_after_freeze")
        text, _ = decode_source(raw)
        for sample in work["samples"]:
            start, end = sample["span"]
            body = text[start:end]
            if digest(body) != sample["text_sha256"]:
                raise ValueError("sample_changed_after_freeze")
            packets.append({**sample, "work_id": work["work_id"], "split": work["split"],
                            "source_sha256": work["source_sha256"], "text": body,
                            "annotation": {"status": "unlabelled", "reviewer": None,
                                           "observations": [], "outside_context_questions": []}})
    event = {"event_id": uuid.uuid4().hex, "manifest_id": manifest["manifest_id"],
             "time": datetime.now(timezone.utc).isoformat(), "work_ids": work_ids,
             "purpose": purpose, "run_spec": run_spec, "output": str(output.resolve())}
    events_dir.mkdir(parents=True, exist_ok=True)
    # Serialize exports so two concurrent callers cannot both release a pristine holdout.
    lock_path = bundle_dir / ".export.lock"
    lock = lock_path.open("x", encoding="utf-8")
    try:
        with lock:
            current = [json.loads(p.read_text(encoding="utf-8")) for p in events_dir.glob("*.json")]
            if purpose == "final_evaluation" and any(set(e["work_ids"]) & set(work_ids) for e in current):
                raise ValueError("holdout_already_exposed")
            with (events_dir / f"{event['event_id']}.json").open("x", encoding="utf-8") as f:
                json.dump(event, f, ensure_ascii=False, indent=2)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as f:
                json.dump({"schema": SCHEMA, "manifest_id": manifest["manifest_id"],
                           "purpose": purpose, "event_id": event["event_id"], "packets": packets},
                          f, ensure_ascii=False, indent=2)
    finally:
        lock_path.unlink()
    return {"works": len(selected), "packets": len(packets), "purpose": purpose, "event_id": event["event_id"]}
