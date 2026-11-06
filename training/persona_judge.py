"""Claude reads a briefing as one of the harness personas and flags the words that persona would not follow.

    set -a && source ~/.config/news_agent/env && set +a
    PYTHONPATH=src:. .venv/bin/python training/persona_judge.py vocab                      # expand each persona's word list, once
    PYTHONPATH=src:. .venv/bin/python training/persona_judge.py judge training/data/r2_answers.jsonl --label "round 2"
    PYTHONPATH=src:. .venv/bin/python training/persona_judge.py drafts training/data/candidates_r2.jsonl

The 3B student can rewrite a term in plain words when told which term, but it
cannot tell which terms a given reader lacks. A persona can. Claude plays the
persona and only *flags* words: it writes no briefing text, so it stays a
judge, never the author.

Which persona reads which packet: the persona of the packet's topic whose
archetype fits the packet's reader profile (beginner -> barely informed,
partial and returning -> partially informed, experts -> well informed),
nearest level when the topic has none. The packet's own concept ledger always
wins: a term it marks known is understood, one it marks unknown is not unless
the briefing explains it. The persona's background decides everything else.

`vocab` expands each persona's short know/don't-know lists over every term
the packet labels (`label_packets.py`) found in that persona's topic, so the
judge has a full word list instead of 3 to 12 terms. It writes
`training/data/persona_vocab.json` and leaves the harness persona files
untouched, so harness runs are unchanged.

`judge` scores a file of held-out answers, and Claude's recorded briefings
for the same packets, by the persona's flags. `drafts` picks the student's
best candidate per training packet by the reward and flags it, for the guided
rewrite in `notebooks/06_guided_revisions.ipynb`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from conversational_agent import config
from conversational_agent.judgment import schema
from training.generate_sft import normalize_term
from training.label_packets import load_labels
from training.rank_pairs import other_events_by_group, packet_of, parse_answer, read_jsonl
from training.reward import reader_state, score_briefing

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PERSONA_DIRS = [ROOT / "harness" / "personas", ROOT / "harness" / "personas" / "generated"]
VOCAB_PATH = HERE / "data" / "persona_vocab.json"
JUDGE_CACHE = HERE / "data" / "persona_judgments.json"
JUDGE_MODEL = config.SONNET

TOPICS = {"startup": "startups", "wine": "wine", "premier league": "premier league", "nfl": "nfl",
          "crypto": "crypto", "ai industry": "ai"}
LEVELS = ["disengaged", "barely_informed", "partially_informed", "well_informed"]
PROFILE_LEVEL = {"beginner": "barely_informed", "partial": "partially_informed", "returning_reader": "partially_informed",
                 "expert": "well_informed", "expert_one_subdomain": "well_informed", "real": "partially_informed"}


def topic_of(name: str | None) -> str | None:
    low = (name or "").lower()
    for key, topic in TOPICS.items():
        if key in low:
            return topic
    return None


def load_personas() -> list[dict[str, Any]]:
    out = []
    for d in PERSONA_DIRS:
        for f in sorted(d.glob("[0-9]*.json")):
            p = json.loads(f.read_text(encoding="utf-8"))
            g = p.get("group")
            p["_topic"] = topic_of(g.get("name") if isinstance(g, dict) else g)
            out.append(p)
    return out


def pick_persona(personas: list[dict[str, Any]], topic: str | None, profile: str, rid: str) -> dict[str, Any] | None:
    pool = [p for p in personas if p["_topic"] == topic]
    if not pool:
        return None
    want = LEVELS.index(PROFILE_LEVEL.get(profile, "partially_informed"))
    best = min(abs(LEVELS.index(p["archetype"]) - want) for p in pool if p["archetype"] in LEVELS)
    near = [p for p in pool if p["archetype"] in LEVELS and abs(LEVELS.index(p["archetype"]) - want) == best]
    return near[int(hashlib.sha1(rid.encode()).hexdigest(), 16) % len(near)]


def persona_card(p: dict[str, Any], vocab: dict[str, Any] | None) -> dict[str, Any]:
    card = {"name": p.get("display_name"), "level": p.get("archetype"), "background": p.get("notes"),
            "familiar_subdomains": p.get("familiar_subdomains"), "knows": p.get("knows"), "does_not_know": p.get("does_not_know")}
    if vocab:
        card["knows"] = sorted(set(map(str, card["knows"] or [])) | set(vocab.get("knows", [])))
        card["does_not_know"] = sorted(set(map(str, card["does_not_know"] or [])) | set(vocab.get("does_not_know", [])))
    return card


# --- Claude calls -----------------------------------------------------------------

VOCAB_PROMPT = """You decide which words a specific person already understands.

You get a person (their level, background, the subdomains they follow, and short lists of words they are known to know and not know) and a list of terms from news in their topic, each with its subdomain.

Return JSON {"knows": [...]} listing exactly the terms from the list this person would understand if they met them in a news story, with no explanation. Judge from their background and level: a casual fan knows on-field words but not contract mechanics; a professional knows their trade's words. Copy each term exactly as given. Terms you leave out are taken as not known."""

VOCAB_SCHEMA = schema({"knows": {"type": "array", "items": {"type": "string"}}}, ["knows"])

JUDGE_PROMPT = """You are a specific reader. You read one short news briefing and report, honestly, which words lost you.

You get `reader`: who you are (level, background, subdomains you follow, words you know and do not know) and `record`, what the app knows about you: terms marked known you understand; terms marked unknown you do not, unless the briefing explains them. For any other word, judge from who you are.

Return JSON with:
- "not_understood": words or short phrases in the briefing you would not understand as written, because they are used without an explanation you could follow. Copy them exactly as they appear in the text. Not names of people, teams, places or companies.
- "needlessly_explained": terms the briefing explains that you already knew, so the explanation was wasted on you.
- "comment": one short sentence, in your own voice, on how the briefing read to you.

Be the reader, not an editor: flag only what would actually stop you, and nothing that would not."""

JUDGE_SCHEMA = schema({"not_understood": {"type": "array", "items": {"type": "string"}},
                       "needlessly_explained": {"type": "array", "items": {"type": "string"}},
                       "comment": {"type": "string"}}, ["not_understood", "needlessly_explained", "comment"])


def _client(spend: dict[str, float]):
    from conversational_agent.app import _anthropic_raw_client
    from conversational_agent.judgment import AnthropicClient

    client = AnthropicClient(_anthropic_raw_client())

    def call(system: str, user: dict[str, Any], schema_: dict[str, Any], max_tokens: int = 2000) -> dict[str, Any] | None:
        for _ in range(2):
            raw = client.complete_json(model=JUDGE_MODEL, system=system, user_content=json.dumps(user, ensure_ascii=False),
                                       schema=schema_, effort="low", max_tokens=max_tokens)
            price = config.PRICES_PER_MTOK.get(raw.model) or config.PRICES_PER_MTOK[JUDGE_MODEL]
            spend["usd"] += raw.input_tokens / 1e6 * price[0] + raw.output_tokens / 1e6 * price[1]
            spend["calls"] += 1
            out = parse_answer(raw.text)
            if isinstance(out, dict):
                return out
        return None

    return call


def build_vocab(call, personas, rows, labels) -> dict[str, Any]:
    terms_by_topic: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        topic = topic_of(row["meta"].get("group_family"))
        for t in (labels.get(row["meta"]["id"]) or {}).get("terms", []):
            terms_by_topic[topic].setdefault(t["term"], t.get("subdomain") or "")
    out = {}
    for p in personas:
        terms = terms_by_topic.get(p["_topic"], {})
        card = persona_card(p, None)
        listed = [{"term": t, "subdomain": s} for t, s in sorted(terms.items())]
        res = call(VOCAB_PROMPT, {"person": card, "terms": listed}, VOCAB_SCHEMA, max_tokens=8000) or {}
        knows = sorted({normalize_term(t) for t in res.get("knows") or []} & set(terms))
        out[p["id"]] = {"knows": knows, "does_not_know": sorted(set(terms) - set(knows))}
        print(f"  {p['id']:<24} {len(knows):>4} of {len(terms)} topic terms known")
    return out


def judge_one(call, persona, vocab, packet, output) -> dict[str, Any] | None:
    known, unknown, band, _ = reader_state(packet)
    user = {"reader": persona_card(persona, vocab.get(persona["id"])),
            "record": {"known": sorted(known), "unknown": sorted(unknown), "level": band},
            "briefing": str((output or {}).get("briefing") or "")}
    return call(JUDGE_PROMPT, user, JUDGE_SCHEMA, max_tokens=800)


def _key(rid: str, text: str) -> str:
    return rid + ":" + hashlib.sha1(text.encode()).hexdigest()[:12]


def judge_many(call, items, personas, vocab, cache, *, budget, spend, workers=6):
    """items: (row, answer_text). Returns flags per item, cached by (row id, text)."""
    todo = [(row, text) for row, text in items if _key(row["meta"]["id"], text) not in cache]

    def work(item):
        row, text = item
        persona = pick_persona(personas, topic_of(row["meta"].get("group_family")), row["meta"].get("profile") or "real", row["meta"]["id"])
        if persona is None or spend["usd"] >= budget:
            return item, None
        res = judge_one(call, persona, vocab, packet_of(row), parse_answer(text))
        return item, (dict(res, persona=persona["id"]) if res else None)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, ((row, text), res) in enumerate(pool.map(work, todo), 1):
            if res is not None:
                cache[_key(row["meta"]["id"], text)] = res
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}  ${spend['usd']:.2f}")
    return [cache.get(_key(row["meta"]["id"], text)) for row, text in items]


def summarise(rows, flags_a, flags_b, labels=("student", "claude")):
    by = defaultdict(lambda: defaultdict(list))
    for row, a, b in zip(rows, flags_a, flags_b, strict=True):
        prof = row["meta"].get("profile") or "real"
        for who, f in zip(labels, (a, b), strict=True):
            if f is None:
                continue
            for k in ("not_understood", "needlessly_explained"):
                by[prof][f"{who}:{k}"].append(len(f.get(k) or []))
                by["ALL"][f"{who}:{k}"].append(len(f.get(k) or []))
    lines = [f"  {'profile':<22}" + "".join(f"{lbl + ' lost':>16}{lbl + ' wasted':>16}" for lbl in labels)]
    for prof in ["beginner", "partial", "returning_reader", "expert_one_subdomain", "expert", "real", "ALL"]:
        d = by.get(prof)
        if not d:
            continue
        cells = "".join(f"{statistics.mean(d[f'{w}:not_understood']):>16.2f}{statistics.mean(d[f'{w}:needlessly_explained']):>16.2f}" for w in labels)
        lines.append(f"  {prof:<22}{cells}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("vocab")
    j = sub.add_parser("judge")
    j.add_argument("answers", type=Path)
    j.add_argument("--label", default="student")
    d = sub.add_parser("drafts")
    d.add_argument("candidates", type=Path)
    d.add_argument("--out", type=Path, default=HERE / "data" / "persona_flags.jsonl")
    ap.add_argument("--budget", type=float, default=6.0)
    args = ap.parse_args(argv)

    spend = {"usd": 0.0, "calls": 0}
    try:
        call = _client(spend)
    except Exception as exc:  # MissingCredentials and friends
        print(exc, file=sys.stderr)
        return 2
    personas = load_personas()
    labels = load_labels()
    train = read_jsonl(HERE / "data" / "sft_train.jsonl")
    val = read_jsonl(HERE / "data" / "sft_val.jsonl")

    if args.cmd == "vocab":
        vocab = build_vocab(call, personas, train + val, labels)
        VOCAB_PATH.write_text(json.dumps(vocab, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {VOCAB_PATH}; spent ${spend['usd']:.2f}")
        return 0

    vocab = json.loads(VOCAB_PATH.read_text(encoding="utf-8")) if VOCAB_PATH.exists() else {}
    cache = json.loads(JUDGE_CACHE.read_text(encoding="utf-8")) if JUDGE_CACHE.exists() else {}
    try:
        if args.cmd == "judge":
            answers = {r["id"]: r.get("answer") or "" for r in read_jsonl(args.answers)}
            rows = [r for r in val if r["meta"]["id"] in answers]
            fs = judge_many(call, [(r, answers[r["meta"]["id"]]) for r in rows], personas, vocab, cache, budget=args.budget, spend=spend)
            fc = judge_many(call, [(r, r["messages"][2]["content"]) for r in rows], personas, vocab, cache, budget=args.budget, spend=spend)
            print(f"{args.label} vs Claude, read by the personas (mean words per briefing; lost = not understood, wasted = explained but already known):")
            print(summarise(rows, fs, fc))
        else:
            by_id = {r["meta"]["id"]: r for r in train}
            others = other_events_by_group(train)
            items = []
            for rec in read_jsonl(args.candidates):
                row = by_id.get(rec["id"])
                if row is None or not rec.get("candidates"):
                    continue
                packet = packet_of(row)
                g = row["meta"].get("group_family") or "?"
                scored = [(score_briefing(packet, parse_answer(t), others.get(g, []), label=labels.get(rec["id"])), t) for t in rec["candidates"]]
                passing = [st for st in scored if st[0].rules_passed] or scored
                items.append((row, max(passing, key=lambda st: st[0].composite)[1]))
            flags = judge_many(call, items, personas, vocab, cache, budget=args.budget, spend=spend)
            n = 0
            with args.out.open("w", encoding="utf-8") as fh:
                for (row, text), f in zip(items, flags, strict=True):
                    if f is None:
                        continue
                    fh.write(json.dumps({"id": row["meta"]["id"], "profile": row["meta"].get("profile"), "draft": text,
                                         "not_understood": f.get("not_understood") or [], "needlessly_explained": f.get("needlessly_explained") or [],
                                         "persona": f.get("persona")}, ensure_ascii=False) + "\n")
                    n += 1
            lost = [len(f.get("not_understood") or []) for f in flags if f]
            print(f"flagged {n} drafts; mean words lost per draft {statistics.mean(lost):.2f}; drafts with nothing lost {sum(1 for x in lost if x == 0)}")
            print(f"wrote {args.out}")
    finally:
        JUDGE_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"spent ${spend['usd']:.2f} in {spend['calls']} calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
