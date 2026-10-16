"""Rebuild the DPO pair set so it cannot be satisfied by defining nothing.

    PYTHONPATH=src:. .venv/bin/python training/build_pairs_v2.py

What went wrong with the first pair set (`dpo_pairs.jsonl`). The hard rules
reject a candidate that re-glosses a known term, and that was the most common
rule break, so in 55 of the 90 rules-decided pairs the rejected briefing was
the one that defined more. Across the whole set the chosen briefing averaged
0.26 definitions and the rejected 0.41. DPO learned that preference exactly:
the first-pass model passes 44 of 45 hard rules and defines nothing for
anyone. A proxy satisfied by the degenerate answer.

What this builds instead (`dpo_pairs_v2.jsonl`):

1. **Judge-decided pairs are kept** (240). Both candidates passed the rules
   and the judge chose with the reader in mind; 41 prefer the briefing that
   defines more, 21 the one that defines less.
2. **Rule breakers are paired against the teacher**, not against the other
   candidate. The teacher's briefing for that packet defines the terms the
   reader lacks and none it holds, so the signal is "this is what right looks
   like", never "fewer definitions".
3. **Learner-decided pairs are added**: for every training packet, each
   candidate is compared with the teacher by the harness's simulated reader
   (`persona_check.learning_gain`). Where the teacher's briefing teaches
   clearly more, the pair (teacher, candidate) is added. This is the persona
   acting as the judge, on the one dimension it can see.

Every pair's chosen side is checked against the hard rules; a chosen side
that fails is dropped. The script prints the definition balance of the result
so a lean in either direction is visible before training.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from training.persona_check import learning_gain
from training.rank_pairs import other_events_by_group, packet_of, parse_answer, read_jsonl, rules_verdict

HERE = Path(__file__).resolve().parent
DEFAULT_PAIRS = HERE / "data" / "dpo_pairs.jsonl"
DEFAULT_CANDIDATES = HERE / "data" / "candidates.jsonl"
DEFAULT_TRAIN = HERE / "data" / "sft_train.jsonl"
DEFAULT_OUT = HERE / "data" / "dpo_pairs_v2.jsonl"
LEARNER_MARGIN = 0.3  # expected extra terms the reader can define a day later


def n_definitions(text: str) -> int:
    return len((parse_answer(text) or {}).get("explained_terms") or [])


def build(
    pairs_v1: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    train_rows: list[dict[str, Any]],
    *,
    learner_margin: float = LEARNER_MARGIN,
    draws: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_id = {row["meta"]["id"]: row for row in train_rows}
    others = other_events_by_group(train_rows)
    out: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()

    # 1. judge-decided pairs, unchanged
    for pair in pairs_v1:
        if pair.get("decided_by") == "judge":
            out.append(dict(pair, source="judge"))
            stats["judge"] += 1

    # 2 and 3. teacher-anchored pairs from the raw candidates
    for rec in candidates:
        row = by_id.get(rec["id"])
        if row is None:
            continue
        packet = packet_of(row)
        teacher_text = row["messages"][2]["content"]
        group = row["meta"].get("group_family") or "?"
        verdicts = rules_verdict(packet, rec["candidates"][:2] + [teacher_text], others.get(group, []))
        if not verdicts[-1]["passed"]:
            stats["teacher_fails_rules"] += 1
            continue
        teacher_gain = learning_gain(packet, parse_answer(teacher_text), draws=draws)
        for text, verdict in zip(rec["candidates"][:2], verdicts[:2], strict=False):
            base = {"id": rec["id"], "profile": row["meta"].get("profile"), "prompt": row["messages"][:2],
                    "chosen": teacher_text, "rejected": text, "decided_by": "teacher"}
            if not verdict["passed"]:
                broken = [k for k, v in verdict["checks"].items() if k not in ("details", "passed") and not v]
                out.append(dict(base, source="rules", reasoning="candidate failed: " + ", ".join(broken or ["not valid JSON"])))
                stats["rules_vs_teacher"] += 1
                continue
            gain = learning_gain(packet, parse_answer(text), draws=draws)
            if teacher_gain - gain >= learner_margin:
                out.append(dict(base, source="learner",
                                reasoning=f"simulated reader learns {teacher_gain:.2f} from the teacher vs {gain:.2f}"))
                stats["learner_vs_teacher"] += 1

    chosen_defs = [n_definitions(p["chosen"]) for p in out]
    rejected_defs = [n_definitions(p["rejected"]) for p in out]
    lean = Counter("chosen defines more" if c > r else "chosen defines fewer" if c < r else "same"
                   for c, r in zip(chosen_defs, rejected_defs, strict=True))
    summary = {
        "pairs": len(out),
        **{f"from_{k}": v for k, v in stats.items()},
        "mean_definitions_chosen": round(statistics.mean(chosen_defs), 2) if out else 0,
        "mean_definitions_rejected": round(statistics.mean(rejected_defs), 2) if out else 0,
        "lean": dict(lean),
        "profiles": dict(Counter(p.get("profile") for p in out)),
    }
    return out, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pairs", type=Path, default=DEFAULT_PAIRS)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--train", type=Path, default=DEFAULT_TRAIN)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--learner-margin", type=float, default=LEARNER_MARGIN)
    parser.add_argument("--draws", type=int, default=20)
    args = parser.parse_args(argv)
    pairs, summary = build(read_jsonl(args.pairs), read_jsonl(args.candidates), read_jsonl(args.train),
                           learner_margin=args.learner_margin, draws=args.draws)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")
    for key, value in summary.items():
        print(f"  {key:<28} {value}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
