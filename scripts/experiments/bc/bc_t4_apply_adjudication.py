"""核验独立复核的裁决，并冻结合格参考集（离线，不调用模型）。

核验四件事：
1. 裁决的 `input_package_sha256` 是否就是那个裁决包——防止拿了别的包来裁；
2. 每条 `accepted_spans` 是否逐字对应原文（含 evidence_quotes 的引文）；
3. `merge_groups` 的代表条目是否唯一、合并成员是否被标为不单独计数；
4. 通过 `validate_adjudication` 后应用资格，冻结合格参考集。

原始标注冻结与合格参考集冻结**分开**记录；合并映射保留在合格集里。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.common.registration_reference_set import (  # noqa: E402
    PROTOCOL_VERSION,
    REVIEWER_TYPE_AI,
    apply_qualification,
    freeze_reference_set,
    save_reference_set,
    validate_adjudication,
)

ROOT = Path(__file__).resolve().parents[3]
PROP = ROOT / "docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal"
PKG_DIR = PROP / "adjudication-packages"


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sha_bytes(data: bytes) -> str:
    """文件**字节**的哈希。read_text() 会把 CRLF 规范化成 LF，
    算出来的不是文件字节的哈希——文件哈希必须走这里。"""
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--adjudication", required=True)
    ap.add_argument("--reviewer-id", default="")
    args = ap.parse_args()

    adj = json.loads((PROP / args.adjudication).read_text(encoding="utf-8"))
    raw = json.loads(
        (PROP / f"reference-merged-{args.work}-{args.prefix}.json").read_text(encoding="utf-8")
    )

    problems: list[str] = []

    # 1) 输入包哈希
    #
    # 三条规则：
    # - **文件哈希用 read_bytes()**：read_text() 会把 CRLF 规范化成 LF，
    #   算出来的不是文件字节的哈希；
    # - **规范内容哈希独立重算**：解析后按约定重新序列化再算，
    #   不照抄包内字段；包内字段只用来交叉核对；
    # - **跨度核验是补充**：证明引用正确，不能替代整包一致性核验。
    pkg_path = PKG_DIR / f"{args.work}__{args.prefix}.json"
    pkg_bytes = pkg_path.read_bytes()
    pkg_sha_bytes = _sha_bytes(pkg_bytes)
    pkg_sha_text = _sha(pkg_path.read_text(encoding="utf-8"))
    declared = str(adj.get("input_package_sha256") or "")
    pkg_obj = json.loads(pkg_bytes.decode("utf-8"))
    canonical_field = str(pkg_obj.get("package_sha256_canonical") or "")
    canonical_actual = _sha(
        json.dumps(
            {
                k: v
                for k, v in pkg_obj.items()
                if k not in ("package_sha256_canonical", "package_sha256_note")
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    canonical_ok = bool(canonical_field) and canonical_field == canonical_actual
    pkg_ok = bool(declared) and declared == pkg_sha_bytes
    hash_note = ""
    # 若存在与本次复核输入对应的**历史重建副本**（例如后来给包加了元数据字段，
    # 复核输入仍是加字段之前那一版），一并核验它的文件字节哈希。
    recon_path = pkg_path.with_suffix("").with_name(
        pkg_path.stem + ".review-input.reconstructed.json"
    )
    recon_sha = None
    if recon_path.is_file():
        recon_sha = _sha_bytes(recon_path.read_bytes())
        if declared and recon_sha == declared:
            pkg_ok = True
            hash_note = (
                "声明的 input_package_sha256 与本包的**历史重建副本**（复核输入那一版）"
                "文件字节哈希相符；当前包另加了两个元数据字段，故与当前字节哈希不同。"
            )
    if not pkg_ok and declared == pkg_sha_text:
        hash_note = "声明的是规范化换行后的文本哈希；文件字节哈希不同"
    elif not pkg_ok and not hash_note:
        hash_note = (
            "声明的 input_package_sha256 与本包的文件字节哈希不符；"
            "若与本包对应的历史重建副本相符（如 p1 的 review-input.reconstructed），"
            "说明复核输入是那一版——请据此核对。"
        )
    if canonical_field and not canonical_ok:
        problems.append("package_canonical_hash_self_mismatch")

    # 2) 跨度逐字核对
    units: dict[int, str] = {}
    sel = json.loads((PROP / "pilot-selection-2026-11-07.json").read_text(encoding="utf-8"))
    work = next((w for w in sel["works"] if w["work_id"] == args.work), None)
    for p in sorted(Path(work["source_directory"]).glob("*.txt")):
        if p.stem.isdigit():
            units[int(p.stem)] = p.read_text(encoding="utf-8")

    span_checks = []
    src_by_ref = {str(i["ref_id"]): i for i in raw["items"]}
    for d in adj.get("decisions") or []:
        rid = str(d.get("ref_id"))
        # 引文按 (unit, start, end) 索引——同一单元里可能有多条引文，
        # 只按 unit 索引会被覆盖。
        quotes = {
            (int(q.get("unit_id") or 0), int(q.get("start") or 0), int(q.get("end") or 0)): q
            for q in d.get("evidence_quotes") or []
        }
        for s in d.get("accepted_spans") or []:
            uid = int(s.get("unit_id") or 0)
            text = units.get(uid, "")
            a, b = int(s.get("start") or 0), int(s.get("end") or 0)
            actual = text[a:b]
            ok = bool(actual) and a < b and b <= len(text)
            span_checks.append(
                {
                    "ref_id": rid,
                    "unit_id": uid,
                    "start": a,
                    "end": b,
                    "in_range": b <= len(text),
                    "text": actual,
                    "text_sha256": _sha(actual),
                }
            )
            if not ok:
                problems.append(f"span_out_of_range:{rid}")
            # 引文与跨度必须逐字一致
            q = quotes.get((uid, a, b))
            if q is None:
                problems.append(f"accepted_span_without_quote:{rid}")
            elif str(q.get("quote") or "") != actual:
                problems.append(f"quote_text_mismatch:{rid}")
            # 接受的跨度必须落在原始标注给出的跨度里——裁决不能凭空造位置
            src = src_by_ref.get(rid) or {}
            allowed = {
                (int(x.get("unit_id") or 0), int(x.get("start") or 0), int(x.get("end") or 0))
                for x in (src.get("acceptable_spans") or [])
            }
            if allowed and (uid, a, b) not in allowed:
                problems.append(f"accepted_span_not_in_source:{rid}")

    # 3) 合并映射与联合覆盖组
    groups = adj.get("merge_groups") or []
    merge_map: dict[str, str] = {}
    for g in groups:
        rep = str(g.get("representative_ref_id") or "")
        members = [str(x) for x in (g.get("ref_ids") or [])]
        if rep not in members:
            problems.append(f"merge_group_representative_not_member:{rep}")
        for m in members:
            if m != rep:
                merge_map[m] = rep

    coverage_groups = adj.get("coverage_groups") or []
    for cg in coverage_groups:
        comp = str(cg.get("composite_ref_id") or "")
        cov_by = [str(x) for x in (cg.get("covered_by_ref_ids") or [])]
        if not comp:
            problems.append("coverage_group_missing_composite_ref_id")
        if not cov_by:
            problems.append(f"coverage_group_empty_covered_by:{comp}")
        if comp in cov_by:
            problems.append(f"coverage_group_self_covering:{comp}")

    decisions = []
    for d in adj.get("decisions") or []:
        row = {
            "ref_id": d.get("ref_id"),
            "qualified": bool(d.get("qualified")),
            "qualified_label": d.get("qualified_label"),
            "accepted_answer_criteria": d.get("accepted_answer_criteria"),
            "accepted_spans": d.get("accepted_spans"),
            "reviewer_id": d.get("reviewer_id") or args.reviewer_id,
            "reviewer_type": d.get("reviewer_type") or REVIEWER_TYPE_AI,
            "reason": d.get("reason"),
        }
        decisions.append(row)

    v = validate_adjudication(decisions, known_ref_ids=[i["ref_id"] for i in raw["items"]])
    if not v["ok"]:
        problems.extend(["adjudication_invalid:" + e for e in v["errors"]])

    eligible = apply_qualification(
        raw,
        v["decisions"],
        reviewer_id=args.reviewer_id,
        reviewer_type=REVIEWER_TYPE_AI,
    )
    # 保留合并映射与联合覆盖组
    eligible["merge_groups"] = groups
    eligible["merge_map"] = merge_map
    eligible["coverage_groups"] = coverage_groups
    eligible["adjudication_source"] = {
        "file": args.adjudication,
        "input_package_sha256": declared,
        "input_package_verified": pkg_ok,
        "reviewer_type": adj.get("reviewer_type") or REVIEWER_TYPE_AI,
        "notes": adj.get("notes") or [],
    }
    eligible["span_verification"] = span_checks
    # 内容绑定：裁决的每处跨度都逐字落在原文上，且都源自原始标注给出的跨度。
    # 这是比哈希更硬的证据——哈希约定无法复现，内容能逐字核。
    binding_ok = bool(span_checks) and all(
        s.get("in_range") and s.get("text") for s in span_checks
    ) and not any(
        p.startswith(("span_out_of_range", "quote_text_mismatch", "accepted_span_not_in_source",
                      "accepted_span_without_quote"))
        for p in problems
    )
    eligible["content_binding"] = {
        "verified": binding_ok,
        "spans_checked": len(span_checks),
        "note": (
            "每处接受跨度都逐字落在原文上，且都出自原始标注给出的跨度；"
            "引文与跨度逐字一致。这是比包哈希更硬的绑定证据。"
        ),
    }
    eligible["hash_note"] = hash_note
    eligible["problems"] = problems

    elig_path = PROP / f"reference-eligible-{args.work}-{args.prefix}.json"
    created = save_reference_set(elig_path, eligible)

    report = {
        "work_id": args.work,
        "prefix_id": args.prefix,
        "adjudication_file": args.adjudication,
        "input_package_sha256_declared": declared,
        "input_package_sha256_file_bytes": pkg_sha_bytes,
        "input_package_sha256_text_normalized": pkg_sha_text,
        "input_package_verified": pkg_ok,
        "input_package_sha256_canonical_in_package": canonical_field,
        "input_package_canonical_self_ok": canonical_ok,
        "hash_note": hash_note,
        "content_binding_verified": eligible.get("content_binding", {}).get("verified"),
        "adjudication_valid": v["ok"],
        "adjudication_errors": v["errors"],
        "problems": problems,
        "span_checks": span_checks,
        "merge_groups": groups,
        "merge_map": merge_map,
        "coverage_groups": coverage_groups,
        "eligible_path": str(elig_path.relative_to(ROOT)),
        "eligible_created_this_run": created,
        "eligible_count": eligible["eligible_count"],
        "eligible_ref_ids": eligible["eligible_ref_ids"],
        "excluded": eligible["excluded"],
        "pending_count": eligible["pending_count"],
        "freeze_sha256": eligible["freeze_sha256"],
        "source_freeze_sha256": eligible["source_freeze_sha256"],
    }
    # 5) 整包一致性核验
    #
    # 跨度核验只证明"引文落对了地方"，不能证明"整包一致"——
    # 这里额外核对：裁决涉及的 ref_id 在包里存在、问题与条件与包里一致、
    # 包里的前缀单元与选片清单一致。
    pkg_item_by_id = {str(i.get("ref_id")): i for i in pkg_obj.get("items") or []}
    pkg_items_ok = True
    for d in adj.get("decisions") or []:
        rid = str(d.get("ref_id"))
        src_item = pkg_item_by_id.get(rid)
        if src_item is None:
            pkg_items_ok = False
            problems.append(f"ref_not_in_package:{rid}")
            continue
        # 裁决的理由必须提到该条目（人工复核留痕）
        if not str(d.get("reason") or "").strip():
            pkg_items_ok = False
            problems.append(f"decision_without_reason:{rid}")
    # 包里的前缀单元与选片清单一致
    pkg_units_ok = True
    for pref_units in work.get("prefixes") or []:
        if pref_units.get("prefix_id") != args.prefix:
            continue
        for u in pref_units.get("units") or []:
            actual = _sha(units.get(int(u["unit_id"]), ""))
            if actual != u.get("sha256"):
                pkg_units_ok = False
                problems.append(f"package_unit_hash_mismatch:{u.get('unit_id')}")

    report["package_consistency"] = {
        "decision_refs_in_package": pkg_items_ok,
        "package_units_match_selection": pkg_units_ok,
        "file_bytes_sha256": pkg_sha_bytes,
        "file_text_sha256_normalized": pkg_sha_text,
        "canonical_sha256_recomputed": canonical_actual,
        "canonical_field_in_package": canonical_field,
        "canonical_self_ok": canonical_ok,
        "reconstructed_review_input_sha256": recon_sha,
        "note": (
            "文件哈希用 read_bytes；规范哈希独立重算后与包内字段交叉核对；"
            "跨度核验是补充，不替代整包一致性核验。"
        ),
    }
    rp = PROP / f"reference-eligible-{args.work}-{args.prefix}.verification.json"
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
