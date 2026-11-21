"""T4 Step 2: Independent reference observation contract + mechanical verifier.

Design rules (frozen in docs/BC生产管线收尾任务书.md §六):
- Every reference observation is a **source-grounded** question about a specific fictional
  event in the trial works, never a synthetic hard sentence.
- `anchor_quote` (setup) and `resolution_quote` (answer) MUST appear **verbatim** in the
  stated chapter of the frozen blocks. This is verified mechanically; unverifiable
  observations are discarded, never scored.
- Setup and answer observations are counted with **separate denominators**.
- Observations are produced by an independent annotator that only sees the source text,
  never any pipeline output.

Observation schema
------------------
{
  "obs_id": "<work_id>_<kind>_<nn>",
  "work_id": "...",
  "obs_type": "setup" | "resolution",
  "chapter": 12,                      # chapter holding the observation
  "anchor_quote": "...",              # verbatim sentence(s) in `chapter`
  "question": "...",
  "answer_criteria": "...",
  "entities": ["..."],
  "resolution_quote": "...",          # resolution only: verbatim in `resolution_chapter`
  "resolution_chapter": 5,            # resolution only
  "criteria_logic": "AND" | "OR" | None
}
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
TRIAL_ROOT = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"

OBS_TYPES = ("setup", "resolution")


def load_chapters(work_id: str, chapters: int = 12) -> dict[int, str]:
    blocks_dir = TRIAL_ROOT / work_id / "blocks"
    return {ch: (blocks_dir / f"{ch:04d}.txt").read_text(encoding="utf-8") for ch in range(1, chapters + 1)}


def _normalize(s: str) -> str:
    """Normalize only whitespace and full-width punctuation differences; keep characters intact."""
    return re.sub(r"[\s　]+", "", s or "")


def quote_occurs(quote: str, text: str) -> int:
    """Count verbatim occurrences of `quote` in `text` after whitespace normalization."""
    q = _normalize(quote)
    t = _normalize(text)
    if not q:
        return 0
    return t.count(q)


def verify_observation(ob: dict[str, Any], chapters: dict[int, str]) -> dict[str, Any]:
    """Mechanically verify one observation against the frozen source text."""
    problems: list[str] = []
    kind = ob.get("obs_type")
    if kind not in OBS_TYPES:
        problems.append(f"invalid_obs_type:{kind}")

    ch = int(ob.get("chapter") or 0)
    if ch not in chapters:
        problems.append(f"chapter_out_of_range:{ch}")
        return {"ok": False, "problems": problems}

    anchor_q = str(ob.get("anchor_quote") or "").strip()
    if len(anchor_q) < 8:
        problems.append("anchor_quote_too_short")
    elif quote_occurs(anchor_q, chapters[ch]) == 0:
        problems.append("anchor_quote_not_verbatim_in_chapter")

    if not str(ob.get("question") or "").strip():
        problems.append("missing_question")
    if not str(ob.get("answer_criteria") or "").strip():
        problems.append("missing_answer_criteria")
    if not ob.get("entities"):
        problems.append("missing_entities")

    if kind == "resolution":
        rch = int(ob.get("resolution_chapter") or 0)
        res_q = str(ob.get("resolution_quote") or "").strip()
        if rch not in chapters:
            problems.append(f"resolution_chapter_out_of_range:{rch}")
        elif len(res_q) < 8:
            problems.append("resolution_quote_too_short")
        elif quote_occurs(res_q, chapters[rch]) == 0:
            problems.append("resolution_quote_not_verbatim_in_chapter")
        elif rch <= ch:
            problems.append("resolution_not_after_setup_chapter")

    return {"ok": not problems, "problems": problems}


def verify_observation_set(
    observations: list[dict[str, Any]],
    work_ids: list[str],
    *,
    min_per_work: int = 20,
    min_setup: int = 10,
    min_resolution: int = 10,
) -> dict[str, Any]:
    """Verify a full reference observation set and report per-work denominators."""
    chapters_by_work = {wid: load_chapters(wid) for wid in work_ids}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for ob in observations:
        wid = str(ob.get("work_id") or "")
        if wid not in chapters_by_work:
            rejected.append({"obs": ob, "problems": [f"unknown_work_id:{wid}"]})
            continue
        oid = str(ob.get("obs_id") or "")
        if not oid or oid in seen_ids:
            rejected.append({"obs": ob, "problems": ["missing_or_duplicate_obs_id"]})
            continue
        res = verify_observation(ob, chapters_by_work[wid])
        if not res["ok"]:
            rejected.append({"obs": ob, "problems": res["problems"]})
            continue
        seen_ids.add(oid)
        accepted.append(ob)

    per_work: dict[str, Any] = {}
    for wid in work_ids:
        w_obs = [o for o in accepted if o["work_id"] == wid]
        setup = [o for o in w_obs if o["obs_type"] == "setup"]
        resolution = [o for o in w_obs if o["obs_type"] == "resolution"]
        per_work[wid] = {
            "total": len(w_obs),
            "setup": len(setup),
            "resolution": len(resolution),
            "meets_min_per_work": len(w_obs) >= min_per_work,
            "meets_min_setup": len(setup) >= min_setup,
            "meets_min_resolution": len(resolution) >= min_resolution,
        }

    total_setup = sum(v["setup"] for v in per_work.values())
    total_resolution = sum(v["resolution"] for v in per_work.values())
    return {
        "accepted": accepted,
        "rejected": rejected,
        "per_work": per_work,
        "totals": {
            "total": len(accepted),
            "setup": total_setup,
            "resolution": total_resolution,
        },
        "meets_thresholds": (
            len(accepted) >= 80
            and total_setup >= 40
            and total_resolution >= 40
            and all(v["meets_min_per_work"] for v in per_work.values())
        ),
    }


def load_observation_file(path: Path) -> list[dict[str, Any]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc["observations"] if isinstance(doc, dict) else doc
