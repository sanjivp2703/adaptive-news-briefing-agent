"""Measure a student against Claude's recorded briefings, without using Claude's text as a target.

    PYTHONPATH=src:. .venv/bin/python training/compare_vs_claude.py training/data/dpo2_answers.jsonl --label "stage 2"
    PYTHONPATH=src:. .venv/bin/python training/compare_vs_claude.py training/data/r1_answers.jsonl --label "round 1" --head-to-head

On the 45 held-out packets, both the student's answer and Claude's recorded
answer are scored by `reward.score_briefing` against the packet label: hard
rules (including the definition cap), substance, gap handling, over-gloss,
fit, composite, and the number of definitions each side made. The report is
the two side by side, overall and per reader profile. Claude is measured
here, never used as the target.

`--head-to-head` adds the blind judge: for each packet, a Claude judge sees
the student's briefing and Claude's in random order, twice with the order
swapped, and picks. The student's win rate over the pairs the judge decided
consistently is the one number where Claude's own judgment is the yardstick.
About a cent a call; 90 calls.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from training.evaluate_student import other_events_for, packet_of, parse_answer, read_jsonl
from training.label_packets import DEFAULT_OUT as DEFAULT_LABELS
from training.label_packets import load_labels
from training.reward import score_briefing

HERE = Path(__file__).resolve().parent
DEFAULT_VAL = HERE / "data" / "sft_val.jsonl"
FIELDS = ("rules_passed", "substance", "gap", "overgloss", "learning", "fit", "composite", "definitions")
PROFILE_FIELDS = ("substance", "gap", "overgloss", "composite", "definitions")


def compare(
    rows: list[dict[str, Any]], answers: dict[str, str], *, labels: dict[str, dict[str, Any]] | None = None, head_to_head=None
) -> dict[str, Any]:
    others = other_events_for(rows)
    labels = labels or {}
    results = []
    for row in rows:
        rid = row["meta"].get("id")
        if rid not in answers:
            raise RuntimeError(f"no student answer for row {rid!r}")
        packet = packet_of(row)
        group = row["meta"].get("group_family") or "?"
        s_out, c_out = parse_answer(answers[rid]), parse_answer(row["messages"][2]["content"])
        label = labels.get(rid)
        student = score_briefing(packet, s_out, others.get(group, []), label=label)
        claude = score_briefing(packet, c_out, others.get(group, []), label=label)
        item = {"id": rid, "profile": row["meta"].get("profile") or "real",
                "student": {f: getattr(student, f) for f in FIELDS}, "claude": {f: getattr(claude, f) for f in FIELDS}}
        if head_to_head is not None:
            item["head_to_head"] = head_to_head(packet, answers[rid], row["messages"][2]["content"])
        results.append(item)
    return {"results": results, "summary": summarise(results)}


def _mean(results, who, field):
    vals = [r[who][field] for r in results]
    if field == "rules_passed":
        return sum(1 for v in vals if v)
    return round(statistics.mean(vals), 3) if vals else None


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(results), "overall": {}, "by_profile": {}}
    for who in ("student", "claude"):
        out["overall"][who] = {f: _mean(results, who, f) for f in FIELDS}
    wins = sum(1 for r in results if r["student"]["composite"] > r["claude"]["composite"])
    ties = sum(1 for r in results if r["student"]["composite"] == r["claude"]["composite"])
    out["overall"]["student_higher_composite"] = wins
    out["overall"]["ties"] = ties
    by: dict[str, list] = defaultdict(list)
    for r in results:
        by[r["profile"]].append(r)
    for profile, rows in by.items():
        out["by_profile"][profile] = {"n": len(rows), **{who: {f: _mean(rows, who, f) for f in PROFILE_FIELDS} for who in ("student", "claude")}}
    h2h = [r["head_to_head"] for r in results if r.get("head_to_head")]
    if h2h:
        decided = [h for h in h2h if h.get("winner") in ("student", "claude")]
        out["head_to_head"] = {"decided": len(decided), "dropped": len(h2h) - len(decided),
                               "student_wins": sum(1 for h in decided if h["winner"] == "student"),
                               "student_win_rate": round(sum(1 for h in decided if h["winner"] == "student") / len(decided), 3) if decided else None}
    return out


def format_summary(summary: dict[str, Any], label: str) -> str:
    o = summary["overall"]
    lines = [f"{label} vs Claude's recorded briefings, {summary['n']} held-out packets (student | Claude):"]
    for f in FIELDS:
        a, b = o["student"][f], o["claude"][f]
        lines.append(f"  {f:<14} {a!s:>8} | {b!s:<8}")
    lines.append(f"  student has the higher composite on {o['student_higher_composite']} packets, ties {o['ties']}")
    lines.append("")
    lines.append(f"  {'profile':<22}{'n':>3}  " + "  ".join(f"{f:>15}" for f in PROFILE_FIELDS))
    for profile, s in summary["by_profile"].items():
        cells = [f"{s['student'][f]:5.2f} | {s['claude'][f]:5.2f}" for f in PROFILE_FIELDS]
        lines.append(f"  {profile:<22}{s['n']:>3}  " + "  ".join(f"{c:>15}" for c in cells))
    if "head_to_head" in summary:
        h = summary["head_to_head"]
        lines.append("")
        lines.append(f"Blind head-to-head (Claude judging, order-swapped): student wins {h['student_wins']} of {h['decided']} decided "
                     f"({h['student_win_rate']:.0%}); {h['dropped']} dropped as order-dependent.")
    return "\n".join(lines)


def claude_head_to_head(client: Any, spend: dict[str, float]):
    from training.rank_pairs import claude_judge

    judge = claude_judge(client, spend)

    def compare_pair(packet: dict[str, Any], student_text: str, claude_text: str) -> dict[str, Any]:
        first = judge(packet, student_text, claude_text)
        second = judge(packet, claude_text, student_text)
        pick_first = "student" if first["winner"] == "A" else "claude"
        pick_second = "claude" if second["winner"] == "A" else "student"
        if pick_first != pick_second:
            return {"winner": "disagree", "reasoning": first.get("reasoning", "")}
        return {"winner": pick_first, "margin": first.get("margin"), "reasoning": first.get("reasoning", "")}

    return compare_pair


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("answers", type=Path, help="student answers for the held-out split (id + answer per line)")
    parser.add_argument("--label", default="student")
    parser.add_argument("--val", type=Path, default=DEFAULT_VAL)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS, help="packet labels from label_packets.py")
    parser.add_argument("--head-to-head", action="store_true", help="blind Claude judge, student vs Claude (about 90 calls)")
    args = parser.parse_args(argv)

    rows = read_jsonl(args.val)
    answers = {rec["id"]: rec.get("answer") or "" for rec in read_jsonl(args.answers)}
    h2h = None
    spend = {"usd": 0.0, "calls": 0}
    if args.head_to_head:
        from conversational_agent.app import MissingCredentials, _anthropic_raw_client
        from conversational_agent.judgment import AnthropicClient

        try:
            h2h = claude_head_to_head(AnthropicClient(_anthropic_raw_client()), spend)
        except MissingCredentials as exc:
            print(exc, file=sys.stderr)
            return 2
    labels = load_labels(args.labels)
    if not labels:
        print(f"no packet labels at {args.labels}; run training/label_packets.py first (gap falls back to the ledger)", file=sys.stderr)
    report = compare(rows, answers, labels=labels, head_to_head=h2h)
    report["label"] = args.label
    report["judge_spend_usd"] = round(spend["usd"], 2)
    out = args.out or HERE / "data" / f"compare_{args.label.replace(' ', '_')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(format_summary(report["summary"], args.label))
    print()
    print(f"Full results: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
