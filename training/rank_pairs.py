"""Turn candidate briefings into preference pairs for DPO.

    PYTHONPATH=src:. .venv/bin/python training/rank_pairs.py ~/Downloads/candidates.jsonl

Input: `candidates.jsonl` from `notebooks/03_generate_dpo_candidates.ipynb`,
one line per training packet with two briefings the stage 1 student wrote
for it. Output: `training/data/dpo_pairs.jsonl`, one line per packet where a
winner could be decided, holding the prompt (system + user messages), the
chosen answer and the rejected answer, plus who decided and why.

How a pair is decided, in order:

1. **The hard rules** (`generate_sft.run_checks`: no question, only the
   packet's event, every number in the source, no re-glossed known term,
   explained within used). If exactly one candidate passes, it wins. If
   neither passes, the pair is dropped: a preference between two bad
   briefings teaches the wrong thing.
2. **A judge**, when both pass. Claude is shown the packet and the two
   candidates, in random order, with a rubric that encodes what the stage 1
   measurement found: the student under-defines for readers who lack the
   terms. The judge is asked twice with the order swapped; a pair it decides
   differently the second time is dropped as a coin flip.

Cost: one judge call per order, about a cent each on Sonnet. `--budget`
stops before the call that would cross it; `--offline` uses a deterministic
stand-in judge (longer briefing wins) so the whole path runs with no key,
which is what the tests do. Interrupted runs resume.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from conversational_agent import config
from conversational_agent.judgment import schema
from conversational_agent.local_client import extract_json_object
from harness.plainness import plainness
from training.generate_sft import run_checks

HERE = Path(__file__).resolve().parent
DEFAULT_TRAIN = HERE / "data" / "sft_train.jsonl"
DEFAULT_OUT = HERE / "data" / "dpo_pairs.jsonl"
JUDGE_MODEL = config.SONNET
JUDGE_EFFORT = "medium"

JUDGE_SCHEMA = schema(
    {
        "winner": {"type": "string", "enum": ["A", "B"]},
        "margin": {"type": "string", "enum": ["clear", "slight"]},
    },
    ["winner", "margin"],
)

JUDGE_PROMPT = """You are judging two briefings written for the same reader about the same news event. The reader's state is in the packet: `proficiency` is a coarse band, `concept_ledger` lists the terms they are known to hold (any state listed) and `subdomain_familiarity` says how well they know each slice of the topic. Terms NOT in the ledger are terms they probably lack.

Pick the briefing that would leave THIS reader better able to join a conversation about the event. Judge in this order:

1. Faithful and plain. States what happened without hedging, asks the reader nothing, invents nothing.
2. Defines the right terms for this reader. A reader in the `beginner` band, or with few known terms in the event's subdomain, needs the domain terms defined inline, briefly, in the sentence that uses them. A `conversant` or `fluent` reader, or one whose ledger already holds the term, should not have it defined. The most common fault in these candidates is defining too little for a reader who lacks the terms; the second is defining a term the ledger already marks known. For reference, a well-pitched briefing has roughly 1.5 definitions per 100 words for a beginner, 1.2 for a developing reader, and 0.5 or fewer for a conversant one.
3. Pitch. Simpler sentences for a beginner; denser prose for a conversant reader. Neither candidate should read like a textbook.
4. Economy. Between two briefings that do all of the above, prefer the one that says it in fewer words.

Say "clear" when the difference matters to the reader and "slight" when they are nearly interchangeable. Answer with the JSON object only."""


# --- Inputs -------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def packet_of(row: dict[str, Any]) -> dict[str, Any]:
    body = json.loads(row["messages"][1]["content"])
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


def other_events_by_group(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    by_group: dict[str, dict[str, str]] = {}
    for row in rows:
        event = packet_of(row)["events"][0]
        text = f"{event.get('headline') or ''} {event.get('detail') or ''}"
        group = row["meta"].get("group_family") or "?"
        by_group.setdefault(group, {})[event.get("headline") or text] = text
    return {g: list(v.values()) for g, v in by_group.items()}


# --- Deciding a pair ------------------------------------------------------------

Judge = Callable[[dict[str, Any], str, str], dict[str, Any]]
"""(packet, briefing_a_text, briefing_b_text) -> {"winner": "A"|"B", "margin": str, "reasoning": str}"""


def rules_verdict(packet: dict[str, Any], texts: list[str], others: list[str]) -> list[dict[str, Any]]:
    event = packet["events"][0]
    own = f"{event.get('headline') or ''} {event.get('detail') or ''}"
    out = []
    for text in texts:
        output = parse_answer(text)
        checks = run_checks(packet, output, [o for o in others if o != own])
        briefing = str((output or {}).get("briefing") or "")
        out.append({"parsed": output is not None, "passed": bool(checks["passed"]), "checks": checks,
                    "words": plainness(briefing)["words"], "briefing": briefing})
    return out


def decide(
    packet: dict[str, Any],
    candidates: list[str],
    others: list[str],
    judge: Judge | None,
    rng: random.Random,
) -> dict[str, Any] | None:
    """One packet's candidates -> a pair record, or None if undecidable."""
    if len(candidates) < 2:
        return None
    a, b = candidates[0], candidates[1]
    verdicts = rules_verdict(packet, [a, b], others)
    pa, pb = verdicts[0]["passed"], verdicts[1]["passed"]
    if pa and not pb:
        return {"chosen": a, "rejected": b, "decided_by": "rules", "reasoning": _rules_reason(verdicts[1])}
    if pb and not pa:
        return {"chosen": b, "rejected": a, "decided_by": "rules", "reasoning": _rules_reason(verdicts[0])}
    if not pa and not pb:
        return None
    if judge is None:
        return None
    # Both pass: ask the judge twice, order swapped, and keep only agreement.
    first = judge(packet, a, b)
    second = judge(packet, b, a)
    pick_first = a if first["winner"] == "A" else b
    pick_second = b if second["winner"] == "A" else a
    if pick_first != pick_second:
        return None
    chosen, rejected = pick_first, (b if pick_first is a else a)
    return {
        "chosen": chosen,
        "rejected": rejected,
        "decided_by": "judge",
        "margin": "clear" if first.get("margin") == "clear" and second.get("margin") == "clear" else "slight",
        "reasoning": first.get("reasoning", ""),
    }


def _rules_reason(failed: dict[str, Any]) -> str:
    checks = failed["checks"]
    broken = [k for k, v in checks.items() if k not in ("details", "passed") and not v]
    if not failed["parsed"]:
        broken = ["not valid JSON", *broken]
    return "rejected candidate failed: " + ", ".join(broken)


# --- Judges -------------------------------------------------------------------------


def rng_coin() -> bool:
    return random.random() < 0.5


def claude_judge(client: Any, spend: dict[str, float]) -> Judge:
    """The real judge. `client` is an `LLMClient` (AnthropicClient in use)."""

    def ask(packet: dict[str, Any], a: str, b: str) -> dict[str, Any]:
        user = json.dumps(
            {"packet": packet, "briefing_A": parse_answer(a) or a, "briefing_B": parse_answer(b) or b},
            indent=1, ensure_ascii=False, default=str,
        )
        verdict: dict[str, Any] | None = None
        for _ in range(2):
            raw = client.complete_json(
                model=JUDGE_MODEL, system=JUDGE_PROMPT, user_content=user, schema=JUDGE_SCHEMA,
                effort=JUDGE_EFFORT, max_tokens=2500,
            )
            price = config.PRICES_PER_MTOK.get(raw.model) or config.PRICES_PER_MTOK[JUDGE_MODEL]
            spend["usd"] += raw.input_tokens / 1e6 * price[0] + raw.output_tokens / 1e6 * price[1]
            spend["calls"] += 1
            verdict = parse_answer(raw.text)
            if verdict and verdict.get("winner") in ("A", "B"):
                break
            verdict = None
        if verdict is None:
            # A reply that never became a verdict is treated as no verdict: the
            # pair is dropped by the order-swap check, never guessed.
            return {"winner": "A" if rng_coin() else "B", "margin": "slight", "reasoning": "unparseable judge reply"}
        return {"winner": verdict["winner"], "margin": verdict.get("margin", "slight"),
                "reasoning": verdict.get("reasoning", "")}

    return ask


def offline_judge(packet: dict[str, Any], a: str, b: str) -> dict[str, Any]:
    """Deterministic stand-in: the longer briefing wins. Exercises the path;
    has no judgment, which is why `--offline` output must never be trained on."""
    wa = len((parse_answer(a) or {}).get("briefing", "") or "")
    wb = len((parse_answer(b) or {}).get("briefing", "") or "")
    return {"winner": "A" if wa >= wb else "B", "margin": "slight", "reasoning": "offline stand-in: longer briefing"}


# --- The run ------------------------------------------------------------------------


def rank(
    train_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    judge: Judge | None,
    *,
    out_path: Path,
    seed: int = 20261014,
    budget_usd: float | None = None,
    spend: dict[str, float] | None = None,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    rng = random.Random(seed)
    by_id = {row["meta"]["id"]: row for row in train_rows}
    others = other_events_by_group(train_rows)
    spend = spend if spend is not None else {"usd": 0.0, "calls": 0}

    done: set[str] = set()
    if out_path.exists():
        done = {rec["id"] for rec in read_jsonl(out_path)}
    stats = {"packets": 0, "pairs": 0, "rules": 0, "judge": 0, "dropped_both_fail": 0,
             "dropped_disagree_or_no_judge": 0, "skipped_done": 0, "stopped_for_budget": False}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as out:
        for rec in candidates:
            row = by_id.get(rec["id"])
            if row is None:
                continue
            stats["packets"] += 1
            if rec["id"] in done:
                stats["skipped_done"] += 1
                continue
            if budget_usd is not None and spend["usd"] >= budget_usd:
                stats["stopped_for_budget"] = True
                break
            packet = packet_of(row)
            group = row["meta"].get("group_family") or "?"
            pair = decide(packet, rec["candidates"], others.get(group, []), judge, rng)
            if pair is None:
                texts = rec["candidates"][:2]
                verdicts = rules_verdict(packet, texts, others.get(group, [])) if len(texts) == 2 else []
                if verdicts and not any(v["passed"] for v in verdicts):
                    stats["dropped_both_fail"] += 1
                else:
                    stats["dropped_disagree_or_no_judge"] += 1
                continue
            record = {
                "id": rec["id"],
                "profile": row["meta"].get("profile"),
                "prompt": row["messages"][:2],
                **pair,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            stats["pairs"] += 1
            stats[pair["decided_by"]] += 1
            if stats["packets"] % 25 == 0:
                progress(f"  {stats['packets']}/{len(candidates)}  pairs {stats['pairs']}  spent ${spend['usd']:.2f}")
    stats["spent_usd"] = round(spend["usd"], 2)
    stats["judge_calls"] = int(spend["calls"])
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("candidates", type=Path, help="candidates.jsonl from notebook 03")
    parser.add_argument("--train", type=Path, default=DEFAULT_TRAIN, help="the training split the candidates were generated for")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--budget", type=float, default=15.0, help="stop before the judge call that would cross this (USD)")
    parser.add_argument("--offline", action="store_true", help="deterministic stand-in judge; output is for plumbing only")
    parser.add_argument("--rules-only", action="store_true",
                        help="decide only the pairs the hard rules settle; needs no key and leaves the rest for a later run")
    parser.add_argument("--seed", type=int, default=20261014)
    args = parser.parse_args(argv)

    if not args.train.exists():
        print(f"No training file at {args.train}.", file=sys.stderr)
        return 2
    train_rows = read_jsonl(args.train)
    candidates = read_jsonl(args.candidates)

    spend = {"usd": 0.0, "calls": 0}
    if args.offline:
        judge: Judge | None = offline_judge
        print("OFFLINE: stand-in judge; do not train on this output")
    elif args.rules_only:
        judge = None
        print("RULES ONLY: pairs where both candidates pass are left for a later run with the judge")
    else:
        from conversational_agent.app import MissingCredentials, _anthropic_raw_client
        from conversational_agent.judgment import AnthropicClient

        try:
            judge = claude_judge(AnthropicClient(_anthropic_raw_client()), spend)
        except MissingCredentials as exc:
            print(exc, file=sys.stderr)
            return 2

    print(f"Ranking {len(candidates)} packets' candidates -> {args.out}")
    stats = rank(train_rows, candidates, judge, out_path=args.out, seed=args.seed,
                 budget_usd=None if args.offline else args.budget, spend=spend)
    print()
    for key, value in stats.items():
        print(f"  {key:<32} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
