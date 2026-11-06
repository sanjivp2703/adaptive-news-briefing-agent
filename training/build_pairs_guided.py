"""Round 3 pairs: the student's persona-guided rewrite against its own draft.

    PYTHONPATH=src:. .venv/bin/python training/build_pairs_guided.py ~/Downloads/guided_revisions.jsonl

Input: `guided_revisions.jsonl` from `notebooks/06_guided_revisions.ipynb`,
up to three rewrites per flagged draft. A rewrite becomes the chosen side,
with the draft as the rejected side, only when it

- passes every hard rule (`reward.score_briefing`), including the definition cap;
- fixed at least half the words the persona lost, and at least one: each
  fixed word is gone from the text or now explained in it. (Requiring all
  of them left 6 beginner pairs: told which words to fix, the 3B student
  still mostly rewords around them.);
- kept the story: substance no more than 0.1 below the draft's;
- actually changed the text.

Among qualifying rewrites the one with the highest reward composite wins.
Both sides are the student's own writing; the persona only flagged words.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from training.generate_sft import normalize_term
from training.label_packets import load_labels
from training.rank_pairs import other_events_by_group, packet_of, parse_answer, read_jsonl
from training.reward import definitions, score_briefing, term_in_text

HERE = Path(__file__).resolve().parent
SUBSTANCE_SLACK = 0.1
FIX_SHARE = 0.5


def share_fixed(output: dict[str, Any] | None, lost: list[str]) -> float:
    if not lost:
        return 1.0
    text = str((output or {}).get("briefing") or "")
    explained = set(definitions(output, lost))
    return sum(1 for w in lost if (not term_in_text(w, text)) or normalize_term(w) in explained) / len(lost)


def fixed_enough(output: dict[str, Any] | None, lost: list[str]) -> bool:
    share = share_fixed(output, lost)
    return share >= FIX_SHARE and share > 0


def choose(rec, packet, others, label):
    draft = parse_answer(rec["draft"])
    d = score_briefing(packet, draft, others, label=label)
    best = None
    reasons = Counter()
    for text in rec.get("revisions") or []:
        out = parse_answer(text)
        if out is None or str(out.get("briefing") or "").strip() == str((draft or {}).get("briefing") or "").strip():
            reasons["unchanged or unparseable"] += 1
            continue
        s = score_briefing(packet, out, others, label=label)
        if not s.rules_passed:
            reasons["broke a rule"] += 1
        elif not fixed_enough(out, rec.get("not_understood") or []):
            reasons["left a flagged word unexplained"] += 1
        elif s.substance + SUBSTANCE_SLACK < d.substance:
            reasons["dropped facts"] += 1
        elif best is None or s.composite > best[1].composite:
            best = (text, s)
    return best, d, reasons


def build(train_rows, revisions, labels):
    by_id = {r["meta"]["id"]: r for r in train_rows}
    others = other_events_by_group(train_rows)
    pairs, why = [], Counter()
    for rec in revisions:
        row = by_id.get(rec["id"])
        if row is None:
            continue
        g = row["meta"].get("group_family") or "?"
        best, d, reasons = choose(rec, packet_of(row), others.get(g, []), labels.get(rec["id"]))
        why.update(reasons)
        if best is None:
            why["no qualifying rewrite"] += 1
            continue
        pairs.append({"id": rec["id"], "profile": row["meta"].get("profile"), "prompt": row["messages"][:2],
                      "chosen": best[0], "rejected": rec["draft"], "decided_by": "persona-guided rewrite",
                      "reasoning": f"fixed {', '.join(rec.get('not_understood') or []) or 'over-explanation'}; "
                                   f"reward {best[1].composite:.3f} vs {d.composite:.3f}"})
    return pairs, why


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("revisions", type=Path)
    ap.add_argument("--train", type=Path, default=HERE / "data" / "sft_train.jsonl")
    ap.add_argument("--out", type=Path, default=HERE / "data" / "dpo_pairs_r3.jsonl")
    args = ap.parse_args(argv)
    pairs, why = build(read_jsonl(args.train), read_jsonl(args.revisions), load_labels())
    with args.out.open("w", encoding="utf-8") as fh:
        for p in pairs:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"{len(pairs)} pairs; by profile {dict(Counter(p['profile'] for p in pairs))}")
    print(f"rewrites rejected: {dict(why)}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
