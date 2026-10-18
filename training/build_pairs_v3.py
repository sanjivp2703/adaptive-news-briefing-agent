"""Stage 3 pairs: each packet's best student briefing against its worst, by reward.

    PYTHONPATH=src:. .venv/bin/python training/build_pairs_v3.py ~/Downloads/candidates_r1.jsonl

Input: `candidates_r{N}.jsonl` from `notebooks/05_generate_round_candidates.ipynb`,
four student briefings per training packet. Every candidate is scored by
`reward.score_briefing` against the packet's label (`label_packets.py`: the
story's central terms and key facts). For each packet:

- the best candidate must pass the hard rules, or the packet is skipped;
- when the reader has a gap, the best candidate must handle at least part of
  it (gap score above zero), or the packet is skipped: a pair never teaches
  stranding or dropping the terms the reader lacks;
- the worst candidate is the lowest-scoring one (a rule breaker if there is
  one, else the lowest composite);
- the chosen side must handle the gap at least as well as the rejected
  side, unless the rejected side broke a rule other than re-glossing or
  declaring an unused term; otherwise the next-worst candidate is tried.
  This stops the set from ever teaching "define less", the stage 2 first-pass
  and stage 3 round-1 failure;
- the pair is kept when best beats worst by at least `--margin` on the
  composite. Below that the two are too alike to teach anything, unless
  `--judge` is on, in which case a Claude judge breaks the near-tie between
  the top two candidates, order-swapped, and the pair is kept only if it
  agrees with itself.

No teacher text appears anywhere. The chosen side is always something the
student wrote.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from training.label_packets import DEFAULT_OUT as DEFAULT_LABELS
from training.label_packets import load_labels
from training.rank_pairs import Judge, other_events_by_group, packet_of, parse_answer, read_jsonl
from training.reward import Score, definitions, score_briefing

HERE = Path(__file__).resolve().parent
DEFAULT_TRAIN = HERE / "data" / "sft_train.jsonl"
MARGIN = 0.15
GAP_SLACK = 0.05
LENIENT_RULES = ("no_regloss", "explained_subset_of_used")


def score_candidates(
    packet: dict[str, Any], texts: list[str], others: list[str], *, label: dict[str, Any] | None = None
) -> list[Score]:
    return [score_briefing(packet, parse_answer(t), others, label=label) for t in texts]


def choose_pair(
    texts: list[str], scores: list[Score], *, margin: float, judge: Judge | None, packet: dict[str, Any]
) -> dict[str, Any] | None:
    order = sorted(range(len(texts)), key=lambda i: scores[i].composite, reverse=True)
    best, worst = order[0], order[-1]
    if not scores[best].rules_passed:
        return None
    if scores[best].details.get("gap") and scores[best].gap <= 0.0:
        return None  # the reader lacks terms and the best candidate strands or drops them all
    gap = scores[best].composite - scores[worst].composite
    # A pair must never teach "define less". The chosen side has to handle the
    # reader's gap at least as well as the rejected side, unless the rejected
    # side broke a rule other than re-glossing (an invented number, a question,
    # a wrong event, the definition cap), which is a lesson about something
    # else. This is the guard the stage 2 first pass and the round-1 pairs
    # lacked: round 1 measured coverage, which is 1.0 for a reader who lacks
    # nothing, so it passed whenever the chosen side defined nothing.
    serious = [f for f in scores[worst].rule_failures if f not in LENIENT_RULES]
    if scores[best].gap + GAP_SLACK < scores[worst].gap and not serious:
        # Look for the lowest-scoring candidate the chosen side still out-teaches.
        for cand in reversed(order[1:]):
            if scores[best].gap + GAP_SLACK >= scores[cand].gap or any(
                f not in LENIENT_RULES for f in scores[cand].rule_failures
            ):
                worst = cand
                gap = scores[best].composite - scores[worst].composite
                break
        else:
            return None
    if gap >= margin:
        return {"chosen": texts[best], "rejected": texts[worst], "decided_by": "reward",
                "margin": round(gap, 3), "reasoning": f"reward {scores[best].composite:.3f} vs {scores[worst].composite:.3f}"
                + (f"; worst failed {', '.join(scores[worst].rule_failures)}" if scores[worst].rule_failures else "")}
    if judge is None or len(order) < 2:
        return None
    a, b = texts[order[0]], texts[order[1]]
    first = judge(packet, a, b)
    second = judge(packet, b, a)
    pick_first = a if first["winner"] == "A" else b
    pick_second = b if second["winner"] == "A" else a
    if pick_first != pick_second:
        return None
    return {"chosen": pick_first, "rejected": b if pick_first is a else a, "decided_by": "judge",
            "margin": round(gap, 3), "reasoning": first.get("reasoning", "")}


def build(
    train_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    margin: float = MARGIN,
    judge: Judge | None = None,
    labels: dict[str, dict[str, Any]] | None = None,
    progress: Callable[[str], None] = lambda _: None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_id = {row["meta"]["id"]: row for row in train_rows}
    others = other_events_by_group(train_rows)
    labels = labels or {}
    pairs: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()
    best_scores: list[float] = []
    chosen_gap: list[float] = []
    rejected_gap: list[float] = []
    for i, rec in enumerate(candidates, 1):
        row = by_id.get(rec["id"])
        if row is None or len(rec.get("candidates") or []) < 2:
            continue
        packet = packet_of(row)
        group = row["meta"].get("group_family") or "?"
        texts = list(rec["candidates"])
        scores = score_candidates(packet, texts, others.get(group, []), label=labels.get(rec["id"]))
        best_scores.append(max(s.composite for s in scores))
        pair = choose_pair(texts, scores, margin=margin, judge=judge, packet=packet)
        if pair is None:
            stats["skipped"] += 1
            continue
        pairs.append({"id": rec["id"], "profile": row["meta"].get("profile"), "round": rec.get("round"),
                      "prompt": row["messages"][:2], **pair})
        stats[pair["decided_by"]] += 1
        by_text = {t: s for t, s in zip(texts, scores, strict=True)}
        chosen_gap.append(by_text[pair["chosen"]].gap)
        rejected_gap.append(by_text[pair["rejected"]].gap)
        if i % 25 == 0:
            progress(f"  {i}/{len(candidates)}  pairs {len(pairs)}")
    chosen_defs = [len(definitions(parse_answer(p["chosen"]))) for p in pairs]
    rejected_defs = [len(definitions(parse_answer(p["rejected"]))) for p in pairs]
    lean = Counter("chosen defines more" if c > r else "chosen defines fewer" if c < r else "same"
                   for c, r in zip(chosen_defs, rejected_defs, strict=True))
    summary = {
        "packets": len(best_scores), "pairs": len(pairs), **dict(stats),
        "labelled_packets": sum(1 for rec in candidates if rec["id"] in labels),
        "mean_best_composite": round(statistics.mean(best_scores), 3) if best_scores else None,
        "mean_gap_chosen": round(statistics.mean(chosen_gap), 3) if chosen_gap else None,
        "mean_gap_rejected": round(statistics.mean(rejected_gap), 3) if rejected_gap else None,
        "mean_definitions_chosen": round(statistics.mean(chosen_defs), 2) if pairs else None,
        "mean_definitions_rejected": round(statistics.mean(rejected_defs), 2) if pairs else None,
        "lean": dict(lean),
        "profiles": dict(Counter(p.get("profile") for p in pairs)),
    }
    return pairs, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--train", type=Path, default=DEFAULT_TRAIN)
    parser.add_argument("--out", type=Path, default=None, help="default: training/data/dpo_pairs_r{round}.jsonl")
    parser.add_argument("--margin", type=float, default=MARGIN)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS, help="packet labels from label_packets.py")
    parser.add_argument("--judge", action="store_true", help="let a Claude judge break near-ties (spends API credit)")
    parser.add_argument("--budget", type=float, default=10.0)
    args = parser.parse_args(argv)

    train_rows = read_jsonl(args.train)
    candidates = read_jsonl(args.candidates)
    rnd = (candidates[0].get("round") if candidates else None) or 1
    out = args.out or HERE / "data" / f"dpo_pairs_r{rnd}.jsonl"

    judge: Judge | None = None
    spend = {"usd": 0.0, "calls": 0}
    if args.judge:
        from conversational_agent.app import MissingCredentials, _anthropic_raw_client
        from conversational_agent.judgment import AnthropicClient
        from training.rank_pairs import claude_judge

        try:
            inner = claude_judge(AnthropicClient(_anthropic_raw_client()), spend)
        except MissingCredentials as exc:
            print(exc, file=sys.stderr)
            return 2

        def judge(packet, a, b):  # type: ignore[misc]
            if spend["usd"] >= args.budget:
                raise RuntimeError(f"judge budget of ${args.budget} reached")
            return inner(packet, a, b)

    random.seed(0)
    print(f"Scoring {len(candidates)} packets' candidates -> {out}")
    labels = load_labels(args.labels)
    if not labels:
        print(f"no packet labels at {args.labels}; run training/label_packets.py first (gap falls back to the ledger)", file=sys.stderr)
    pairs, summary = build(train_rows, candidates, margin=args.margin, judge=judge, labels=labels, progress=print)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")
    summary["judge_spend_usd"] = round(spend["usd"], 2)
    for key, value in summary.items():
        print(f"  {key:<24} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
