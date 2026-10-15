"""Check the pair judge against the harness's simulated learner.

    PYTHONPATH=src:. .venv/bin/python training/persona_check.py training/data/dpo_pairs.jsonl

For every pair the judge decided, build the harness's memory model
(`harness/learning.py`, the FSRS-style model behind every persona) in the
state the packet describes -- the ledger's known terms seeded as strong
traces, its unknown terms as none -- and play each candidate briefing into
it as a sequence of glossed encounters at `read` quality. The learner's gain
from a briefing is how much more of the domain vocabulary it can define
afterwards (P(can define) summed over the terms the briefing put in front of
it, averaged over many random draws, because encoding is stochastic).

The question answered: does the briefing the judge preferred also teach the
simulated reader more? Agreement is reported per decision and overall.

What this can and cannot say. The simulated learner is driven by the harness's
structural events -- which terms a briefing defined, for a reader in what
state -- not by the prose. It therefore validates the judge's choice of WHAT
to define, which is exactly the fault the stage 1 measurement found, and it
is silent on wording, accuracy of a figure, or an expert being talked down to.
Those stay with the hard rules and the judge. It makes no model calls and
costs nothing to run.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from conversational_agent import config
from conversational_agent.local_client import extract_json_object
from harness.learning import LearningSpec, MemoryModel
from training.generate_sft import normalize_term

HERE = Path(__file__).resolve().parent
DEFAULT_PAIRS = HERE / "data" / "dpo_pairs.jsonl"
DEFAULT_OUT = HERE / "data" / "persona_check.json"

# How much of the domain a reader in each band starts with, for the learning
# spec's prior-knowledge trait. The bands come from the product's own
# proficiency readout; the values are the harness fixtures' typical settings.
PRIOR_BY_BAND = {"beginner": 0.1, "developing": 0.4, "conversant": 0.7, "fluent": 0.9}
DRAWS = 30
HORIZON = timedelta(days=1)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def packet_of(pair: dict[str, Any]) -> dict[str, Any]:
    body = json.loads(pair["prompt"][1]["content"])
    return body["context"] if isinstance(body, dict) and "context" in body else body


def parse_answer(text: str) -> dict[str, Any] | None:
    candidate = extract_json_object(text or "")
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def terms_of(output: dict[str, Any] | None, key: str) -> list[str]:
    return [normalize_term(t) for t in (output or {}).get(key) or [] if isinstance(t, str) and t.strip()]


def reader_state(packet: dict[str, Any]) -> tuple[set[str], set[str], float]:
    """(known terms, unknown terms, prior-knowledge trait) from the packet."""
    ledger = packet.get("concept_ledger") or {}
    known = {normalize_term(t) for state in config.KNOWN_STATES for t in ledger.get(state, [])}
    unknown = {normalize_term(t) for t in ledger.get(config.CONCEPT_UNKNOWN, [])} - known
    prior = PRIOR_BY_BAND.get(str(packet.get("proficiency")), 0.4)
    return known, unknown, prior


def learning_gain(
    packet: dict[str, Any], output: dict[str, Any] | None, *, draws: int = DRAWS, seed: str = "persona-check"
) -> float:
    """Expected number of additional terms the reader can define a day after
    reading this briefing, over the terms the briefing used."""
    known, unknown, prior = reader_state(packet)
    explained = terms_of(output, "explained_terms")
    used = list(dict.fromkeys(terms_of(output, "terms_used") + explained))
    if not used:
        return 0.0
    universe = sorted(known | unknown | set(used))
    at = datetime(2026, 9, 1, tzinfo=UTC)
    spec = LearningSpec(prior_knowledge=prior, memory_rate=1.0)
    event_key = str((packet.get("events") or [{}])[0].get("headline") or "event")
    totals = []
    for draw in range(draws):
        model = MemoryModel.seeded(
            learning=spec, seed=f"{seed}:{draw}", persona_id="reader", universe=universe,
            knows=known, does_not_know=unknown, at=at,
        )
        before = sum(model.ladder(t, at + HORIZON)[1] for t in used)
        for term in explained:
            model.glossed(term, "read", at, context_id=event_key)
        after = sum(model.ladder(t, at + HORIZON)[1] for t in used)
        totals.append(after - before)
    return statistics.mean(totals)


def check_pair(pair: dict[str, Any], *, draws: int = DRAWS) -> dict[str, Any]:
    packet = packet_of(pair)
    chosen = parse_answer(pair["chosen"])
    rejected = parse_answer(pair["rejected"])
    g_chosen = learning_gain(packet, chosen, draws=draws)
    g_rejected = learning_gain(packet, rejected, draws=draws)
    if abs(g_chosen - g_rejected) < 0.05:
        verdict = "tie"
    else:
        verdict = "agree" if g_chosen > g_rejected else "disagree"
    return {
        "id": pair.get("id"),
        "profile": pair.get("profile"),
        "decided_by": pair.get("decided_by"),
        "margin": pair.get("margin"),
        "gain_chosen": round(g_chosen, 3),
        "gain_rejected": round(g_rejected, 3),
        "verdict": verdict,
        "defined_chosen": terms_of(chosen, "explained_terms"),
        "defined_rejected": terms_of(rejected, "explained_terms"),
    }


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for who in ("all", "judge", "rules"):
        rows = results if who == "all" else [r for r in results if r["decided_by"] == who]
        if not rows:
            continue
        counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("agree", "disagree", "tie")}
        decided = counts["agree"] + counts["disagree"]
        out[who] = {
            "pairs": len(rows),
            **counts,
            "agreement_when_decided": round(counts["agree"] / decided, 3) if decided else None,
        }
    return out


def format_summary(summary: dict[str, Any]) -> str:
    lines = ["Does the simulated reader learn more from the briefing the pair preferred?"]
    lines.append(f"  {'decided by':<12}{'pairs':>6}{'agree':>7}{'disagree':>10}{'tie':>5}   agreement")
    for who, s in summary.items():
        agreement = "-" if s["agreement_when_decided"] is None else f"{s['agreement_when_decided']:.0%}"
        lines.append(f"  {who:<12}{s['pairs']:>6}{s['agree']:>7}{s['disagree']:>10}{s['tie']:>5}   {agreement}")
    lines.append("")
    lines.append("A tie means both briefings defined the same terms for this reader, so the")
    lines.append("learner cannot tell them apart; those pairs were decided on prose or faithfulness.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pairs", type=Path, nargs="?", default=DEFAULT_PAIRS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--draws", type=int, default=DRAWS, help="random draws per briefing (default 30)")
    parser.add_argument("--only-judge", action="store_true", help="check only the pairs the judge decided")
    args = parser.parse_args(argv)
    if not args.pairs.exists():
        print(f"No pairs file at {args.pairs}.", file=sys.stderr)
        return 2
    pairs = read_jsonl(args.pairs)
    if args.only_judge:
        pairs = [p for p in pairs if p.get("decided_by") == "judge"]
    results = [check_pair(p, draws=args.draws) for p in pairs]
    summary = summarise(results)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(format_summary(summary))
    print()
    print(f"Full results: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
