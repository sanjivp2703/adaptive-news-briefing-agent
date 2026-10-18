"""Label each training packet's source story once: its jargon and its key facts.

    set -a && source ~/.config/news_agent/env && set +a
    PYTHONPATH=src:. .venv/bin/python training/label_packets.py            # about a dollar, resumable

The stage 3 reward decides what a briefing *needs* before the model writes
it, from the story and the reader rather than from the briefing's own term
list. That needs two things per packet that only a reader of the source can
produce:

- **terms**: the domain-specific terms in the story, each marked `central`
  when the story cannot be told without it, with the subdomain it belongs to;
- **facts**: three to five key facts, each with `anchors` (names, numbers,
  distinctive words that any faithful retelling contains) and the terms it
  involves, so a briefing that drops the jargon but keeps the fact gets credit
  for saying it plainly.

Claude labels the *source*, never a briefing, so this stays on the judge side
of the "Claude judges, Claude is not the target" line. Labels are cached in
`training/data/packet_labels.json` keyed by row id; a rerun only labels what
is missing. `--offline` produces a deterministic stand-in from the packet
alone so the pipeline can be exercised without a key; it has no judgment.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from conversational_agent import config
from conversational_agent.judgment import schema
from training.generate_sft import normalize_term, numbers_in
from training.rank_pairs import packet_of, parse_answer, read_jsonl

HERE = Path(__file__).resolve().parent
DEFAULT_TRAIN = HERE / "data" / "sft_train.jsonl"
DEFAULT_VAL = HERE / "data" / "sft_val.jsonl"
DEFAULT_OUT = HERE / "data" / "packet_labels.json"
LABEL_MODEL = config.SONNET
LABEL_EFFORT = "low"

LABEL_PROMPT = """You label one news item for an automatic scorer. You never write a briefing.

Return JSON with:
- "terms": the domain-specific terms in the item that a newcomer to this topic might not hold: jargon, named mechanisms, roles, abbreviations, rule names. Not ordinary English, not proper names of people or teams. For each: "term" (as it appears, short), "central" (true when the item cannot be told without it), "subdomain" (a two-or-three-word label for the area of the topic it belongs to, e.g. "injuries", "transfer rules", "monetary policy").
- "facts": three to five key facts of the item: who, what happened, the numbers, the outcome, why it matters. For each: "fact" (one plain sentence), "anchors" (2-4 short strings any faithful retelling would contain: names, numbers, distinctive words; no jargon unless unavoidable), "terms" (the terms from the list above this fact involves, possibly empty).

Be literal: only terms and facts that are in the item. Empty "terms" is fine for an item with no jargon."""

LABEL_SCHEMA = schema(
    {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"term": {"type": "string"}, "central": {"type": "boolean"}, "subdomain": {"type": "string"}},
                "required": ["term", "central", "subdomain"],
                "additionalProperties": False,
            },
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "anchors": {"type": "array", "items": {"type": "string"}},
                    "terms": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["fact", "anchors", "terms"],
                "additionalProperties": False,
            },
        },
    },
    ["terms", "facts"],
)


def source_of(packet: dict[str, Any]) -> str:
    event = (packet.get("events") or [{}])[0]
    return f"{event.get('headline') or ''}\n{event.get('detail') or ''}".strip()


def clean_label(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalise a label so the reward can rely on its shape; None if unusable."""
    if not isinstance(raw, dict):
        return None
    terms = []
    seen: set[str] = set()
    for item in raw.get("terms") or []:
        if not isinstance(item, dict) or not str(item.get("term") or "").strip():
            continue
        t = normalize_term(str(item["term"]))
        if t in seen:
            continue
        seen.add(t)
        terms.append({"term": t, "central": bool(item.get("central")), "subdomain": normalize_term(str(item.get("subdomain") or ""))})
    facts = []
    for item in raw.get("facts") or []:
        if not isinstance(item, dict) or not str(item.get("fact") or "").strip():
            continue
        anchors = [str(a).strip() for a in (item.get("anchors") or []) if str(a).strip()]
        if not anchors:
            continue
        facts.append({"fact": str(item["fact"]).strip(), "anchors": anchors,
                      "terms": [normalize_term(str(t)) for t in (item.get("terms") or []) if str(t).strip()]})
    if not facts and not terms:
        return None
    return {"terms": terms, "facts": facts}


def offline_label(packet: dict[str, Any]) -> dict[str, Any]:
    """Stand-in with no judgment: ledger-unknown terms that appear in the
    source count as central terms; each sentence of the detail is a fact
    anchored on its numbers and capitalised words."""
    source = source_of(packet)
    body = normalize_term(source)
    ledger = packet.get("concept_ledger") or {}
    terms = [{"term": normalize_term(t), "central": True, "subdomain": "general"}
             for t in ledger.get(config.CONCEPT_UNKNOWN, []) if normalize_term(t) and normalize_term(t) in body]
    facts = []
    for sentence in re.split(r"(?<=[.!])\s+", source.replace("\n", " ")):
        if not sentence.strip():
            continue
        anchors = [n["text"] for n in numbers_in(sentence)] + re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", sentence)[:3]
        if anchors:
            facts.append({"fact": sentence.strip(), "anchors": anchors[:4],
                          "terms": [t["term"] for t in terms if t["term"] in normalize_term(sentence)]})
    return {"terms": terms, "facts": facts[:5]}


def claude_labeler(client: Any, spend: dict[str, float]):
    def label(packet: dict[str, Any]) -> dict[str, Any] | None:
        user = json.dumps({"item": source_of(packet), "topic": (packet.get("group") or {}).get("name")}, ensure_ascii=False)
        for _ in range(2):
            raw = client.complete_json(model=LABEL_MODEL, system=LABEL_PROMPT, user_content=user,
                                       schema=LABEL_SCHEMA, effort=LABEL_EFFORT, max_tokens=2000)
            price = config.PRICES_PER_MTOK.get(raw.model) or config.PRICES_PER_MTOK[LABEL_MODEL]
            spend["usd"] += raw.input_tokens / 1e6 * price[0] + raw.output_tokens / 1e6 * price[1]
            spend["calls"] += 1
            cleaned = clean_label(parse_answer(raw.text))
            if cleaned is not None:
                return cleaned
        return None

    return label


def load_labels(path: Path | None = DEFAULT_OUT) -> dict[str, dict[str, Any]]:
    if path is None or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def label_rows(rows, labeler, existing: dict[str, Any], *, out: Path, budget: float, spend: dict[str, float],
               progress=print, workers: int = 6, chunk: int = 24):
    """Label what is missing, `workers` calls at a time, saving after every chunk."""
    labels = dict(existing)
    todo = [row for row in rows if row["meta"]["id"] not in labels]
    progress(f"{len(labels)} labelled, {len(todo)} to go")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(todo), chunk):
            if spend["usd"] >= budget:
                progress(f"budget of ${budget} reached after {done} new labels")
                break
            batch = todo[start:start + chunk]
            for row, label in zip(batch, pool.map(lambda r: labeler(packet_of(r)), batch), strict=True):
                if label is not None:
                    labels[row["meta"]["id"]] = label
            done += len(batch)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(labels, indent=1, ensure_ascii=False), encoding="utf-8")
            progress(f"  {done}/{len(todo)}  ${spend['usd']:.2f}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(labels, indent=1, ensure_ascii=False), encoding="utf-8")
    return labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--train", type=Path, default=DEFAULT_TRAIN)
    parser.add_argument("--val", type=Path, default=DEFAULT_VAL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--budget", type=float, default=3.0)
    parser.add_argument("--offline", action="store_true", help="deterministic stand-in labels, no key, no judgment")
    args = parser.parse_args(argv)

    rows = [r for p in (args.train, args.val) if p.exists() for r in read_jsonl(p)]
    spend = {"usd": 0.0, "calls": 0}
    if args.offline:
        labeler = offline_label
    else:
        from conversational_agent.app import MissingCredentials, _anthropic_raw_client
        from conversational_agent.judgment import AnthropicClient

        try:
            labeler = claude_labeler(AnthropicClient(_anthropic_raw_client()), spend)
        except MissingCredentials as exc:
            print(exc, file=sys.stderr)
            return 2
    labels = label_rows(rows, labeler, load_labels(args.out), out=args.out, budget=args.budget, spend=spend)
    n_terms = sum(len(v["terms"]) for v in labels.values())
    n_central = sum(1 for v in labels.values() for t in v["terms"] if t["central"])
    n_facts = sum(len(v["facts"]) for v in labels.values())
    print(f"{len(labels)} packets labelled: {n_terms} terms ({n_central} central), {n_facts} facts; spent ${spend['usd']:.2f} in {spend['calls']} calls")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
