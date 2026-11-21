"""生成 4 份独立评审任务包：每部作品一份，供独立评审代理逐观察判定。

纪律（任务书 §六）：
- 评审代理**不得**看到管线判定（verdict）、臂名、任何统计数字；
- 只给：参考观察（问题/答案标准/锚点/章节）、候选（问题/答案标准/线索/锚点/章节）、
  以及对应章节的原文；
- 结论只有三档：equivalent（讲的是同一件事）/ ambiguous（可能同一件但不足以判定）/
  non_equivalent（不是同一件事）。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TRIAL_ROOT = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = TRIAL_ROOT / "t4-review-tasks"
WORK_IDS = ["t4_ind_holdout", "t4_game_holdout", "t4_urban_holdout", "t4_hist_holdout"]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for wid in WORK_IDS:
        obs = json.loads((TRIAL_ROOT / wid / "reference_observations.json").read_text(encoding="utf-8"))["observations"]
        led = json.loads(
            (TRIAL_ROOT / "runs" / wid / "b_fixed" / "foreshadow_ledger.json").read_text(encoding="utf-8")
        )["items"]
        chapters = {
            ch: (TRIAL_ROOT / wid / "blocks" / f"{ch:04d}.txt").read_text(encoding="utf-8")
            for ch in range(1, 13)
        }
        task = {
            "work_id": wid,
            "instructions": (
                "你是独立评审员。逐条参考观察，判断该观察的伏笔与哪条候选**在讲同一件事**。"
                "只看：观察的 question/answer_criteria/anchor_quote/章节，"
                "候选的 question/answer_criteria/description/anchor_quote/anchor_chapter，"
                "以及对应章节的原文。不存在管线判定，也不要去猜。"
                "判定档位："
                "equivalent = 两边问的确实是同一件事（可以是同一件事的不同侧面）；"
                "ambiguous = 可能相关但不足以判定；"
                "non_equivalent = 不是同一件事。"
                "宁可判 non_equivalent，也不要把'都提到同一个人/同一章'当成同一件事。"
            ),
            "output_schema": {
                "obs_id": "参考观察 id",
                "equivalent_candidate_ids": ["讲同一件事的候选 id"],
                "notes": "简短理由",
            },
            "observations": obs,
            "candidates": [
                {
                    "id": i.get("id"),
                    "question": i.get("question"),
                    "answer_criteria": i.get("answer_criteria"),
                    "description": i.get("description"),
                    "anchor_quote": i.get("anchor_quote"),
                    "anchor_chapter": i.get("anchor_chapter"),
                }
                for i in led
            ],
            "chapter_text": {str(k): v for k, v in chapters.items()},
        }
        p = OUT_DIR / f"{wid}.json"
        p.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        print(wid, "->", p, "obs:", len(obs), "cands:", len(led))


if __name__ == "__main__":
    main()
