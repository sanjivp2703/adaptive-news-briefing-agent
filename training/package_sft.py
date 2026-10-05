"""Package the clean briefing rows into chat-format training files.

Reads the two clean files `extract_sft.py` and `generate_sft.py` write and
turns every row into the three-message shape a chat model is trained on:

    {"messages": [{"role": "system",    "content": <prompt + schema tail>},
                  {"role": "user",      "content": <the packet, as sent>},
                  {"role": "assistant", "content": <the answer, as JSON>}],
     "meta": {...}}

Two things are deliberately byte-identical to what the running system does,
because a model trained on one string and served another would be trained on
the wrong thing:

* the **system** message is the production briefing prompt followed by the
  exact "## Output format" tail `LocalModelClient` appends at serve time
  (same constant, same schema, same `json.dumps` settings);
* the **user** message is the packet serialised exactly as `Judge` serialises
  it on the wire (`indent=2, sort_keys=True, default=str, ensure_ascii=False`).
  The `input` field in the clean files is `json.loads` of that wire string,
  so dumping it back with the same settings reproduces it.

The **assistant** message is the teacher's answer as one JSON object holding
exactly the schema's fields, in the schema's order. The clean files drop the
schema's `reasoning` field; it is restored here from the judgment log the row
came from (`training/data/generated.db` for generated rows, the live run's DB
for real rows), because the served schema requires it and the prompt asks for
it. Rows whose reasoning cannot be recovered are dropped and counted, never
invented. Rows written under the older v5 prompt have no `subdomains`; those
get an empty list and `meta.backfilled` says so.

Split: real rows always go to train (they are the only production-shaped
examples and there are eight of them). Generated rows are split per
`meta.profile` so every profile keeps the same train/val ratio, shuffled by
`--seed`, so the same seed always gives the same files.

Run:  PYTHONPATH=src:. .venv/bin/python training/package_sft.py [--seed N]
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from conversational_agent.judgment import load_prompt
from conversational_agent.judgments import BRIEFING_SCHEMA
from conversational_agent.local_client import SCHEMA_TAIL

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "training" / "data"
LIVE_DB_DIR = ROOT / "eval-runs" / "live"
GENERATED_DB = DATA_DIR / "generated.db"
GENERATED_CLEAN = DATA_DIR / "sft_generated.clean.jsonl"
REAL_CLEAN = DATA_DIR / "sft_briefings.clean_nogate.jsonl"
TRAIN_OUT = DATA_DIR / "sft_train.jsonl"
VAL_OUT = DATA_DIR / "sft_val.jsonl"

OUTPUT_FIELDS: tuple[str, ...] = tuple(BRIEFING_SCHEMA["properties"].keys())
REQUIRED_FIELDS: tuple[str, ...] = tuple(BRIEFING_SCHEMA["required"])
REAL_PROFILE = "real"
CHARS_PER_TOKEN = 4  # the rough rule of thumb; the notebook measures for real


# --- The three messages ----------------------------------------------------


def system_message(prompt_text: str, schema: dict[str, Any] = BRIEFING_SCHEMA) -> str:
    """Exactly what `LocalModelClient.complete_json` puts in the system slot."""
    return prompt_text + SCHEMA_TAIL + "```json\n" + json.dumps(schema, indent=2) + "\n```"


def user_message(packet: dict[str, Any]) -> str:
    """Exactly what `Judge.call` puts in the user slot (see judgment.py)."""
    return json.dumps(packet, indent=2, sort_keys=True, default=str, ensure_ascii=False)


def assistant_message(output: dict[str, Any]) -> str:
    """The answer as one JSON object: the schema's fields, in schema order."""
    ordered = {k: output[k] for k in OUTPUT_FIELDS if k in output}
    return json.dumps(ordered, ensure_ascii=False)


def build_example(row: dict[str, Any], prompt_text: str) -> dict[str, Any]:
    """One clean row -> one chat example. Raises if a required field is absent
    (call `complete_output` first)."""
    output = row["output"]
    missing = [k for k in REQUIRED_FIELDS if k not in output]
    if missing:
        raise ValueError(f"row {row.get('id')} is missing output fields {missing}")
    meta = dict(row.get("meta") or {})
    is_real = meta.get("profile") is None
    return {
        "messages": [
            {"role": "system", "content": system_message(prompt_text)},
            {"role": "user", "content": user_message(row["input"])},
            {"role": "assistant", "content": assistant_message(output)},
        ],
        "meta": {
            "id": row.get("id"),
            "source": "real" if is_real else "generated",
            "source_db": row.get("source_db"),
            "profile": meta.get("profile") or REAL_PROFILE,
            "persona_id": meta.get("persona_id"),
            "group_family": meta.get("group_family"),
            "group_id": row.get("group_id"),
            "focus_subdomain": meta.get("focus_subdomain"),
            "prompt_version": row.get("prompt_version"),
            "teacher": row.get("model"),
            "log_id": meta.get("log_id") or row.get("id"),
            "backfilled": list(row.get("_backfilled") or []),
            "checks_passed": ((row.get("quality") or {}).get("checks") or {}).get("passed"),
        },
    }


# --- Restoring the fields the extractors dropped ---------------------------


def _reasoning_from_db(db_path: Path, log_id: str) -> str | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT verdict_json, reasoning FROM judgment_log WHERE id = ?", (log_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    verdict_json, reasoning_col = row
    if verdict_json:
        try:
            verdict = json.loads(verdict_json)
        except json.JSONDecodeError:
            verdict = None
        if isinstance(verdict, dict) and isinstance(verdict.get("reasoning"), str):
            return verdict["reasoning"]
    return reasoning_col if isinstance(reasoning_col, str) and reasoning_col else None


def complete_output(
    row: dict[str, Any],
    *,
    generated_db: Path = GENERATED_DB,
    live_db_dir: Path = LIVE_DB_DIR,
) -> bool:
    """Fill `reasoning` (from the source log) and `subdomains` (empty, v5 rows)
    in place. Returns False when reasoning is unrecoverable -- drop the row."""
    output = row.get("output")
    if not isinstance(output, dict):
        return False
    backfilled: list[str] = []
    if "reasoning" not in output:
        meta = row.get("meta") or {}
        if meta.get("log_id"):
            reasoning = _reasoning_from_db(generated_db, meta["log_id"])
        else:
            reasoning = _reasoning_from_db(live_db_dir / f"{row.get('source_db')}.db", row["id"])
        if reasoning is None:
            return False
        output["reasoning"] = reasoning
        backfilled.append("reasoning")
    if "subdomains" not in output:
        output["subdomains"] = []
        backfilled.append("subdomains")
    row["_backfilled"] = backfilled
    return True


# --- Split -----------------------------------------------------------------


def stratified_split(
    examples: list[dict[str, Any]], *, seed: int, val_fraction: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Real rows -> train. Every other profile gives `val_fraction` of its
    rows (at least one) to val. Shuffle order depends only on `seed` and the
    example ids, never on file order."""
    by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ex in examples:
        by_profile[ex["meta"]["profile"]].append(ex)
    train: list[dict[str, Any]] = []
    val: list[dict[str, Any]] = []
    for profile in sorted(by_profile):
        rows = sorted(by_profile[profile], key=lambda ex: str(ex["meta"]["id"]))
        if profile == REAL_PROFILE:
            train.extend(rows)
            continue
        rng = random.Random(f"{seed}:{profile}")
        rng.shuffle(rows)
        n_val = max(1, round(len(rows) * val_fraction)) if len(rows) > 1 else 0
        val.extend(rows[:n_val])
        train.extend(rows[n_val:])
    # A stable, seed-dependent order for the files themselves.
    random.Random(seed).shuffle(train)
    random.Random(seed + 1).shuffle(val)
    return train, val


# --- Stats -----------------------------------------------------------------


def token_estimate(example: dict[str, Any]) -> int:
    chars = sum(len(m["content"]) for m in example["messages"])
    return -(-chars // CHARS_PER_TOKEN)  # ceil


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def summarise(train: list[dict[str, Any]], val: list[dict[str, Any]]) -> dict[str, Any]:
    all_examples = train + val
    est = [token_estimate(ex) for ex in all_examples]
    answer_est = [
        -(-len(ex["messages"][2]["content"]) // CHARS_PER_TOKEN) for ex in all_examples
    ]
    system_est = -(-len(all_examples[0]["messages"][0]["content"]) // CHARS_PER_TOKEN) if all_examples else 0
    return {
        "train": len(train),
        "val": len(val),
        "train_by_profile": dict(Counter(ex["meta"]["profile"] for ex in train)),
        "val_by_profile": dict(Counter(ex["meta"]["profile"] for ex in val)),
        "backfilled": dict(Counter(f for ex in all_examples for f in ex["meta"]["backfilled"])),
        "tokens_est": {
            "system_only": system_est,
            "mean": round(sum(est) / len(est)) if est else 0,
            "p95": _percentile(est, 95),
            "max": max(est) if est else 0,
            "answer_mean": round(sum(answer_est) / len(answer_est)) if answer_est else 0,
            "answer_max": max(answer_est) if answer_est else 0,
        },
    }


def print_summary(stats: dict[str, Any], dropped: int) -> None:
    print(f"train: {stats['train']}   val: {stats['val']}   dropped (no recoverable reasoning): {dropped}")
    profiles = sorted(set(stats["train_by_profile"]) | set(stats["val_by_profile"]))
    print(f"{'profile':<24}{'train':>7}{'val':>6}")
    for p in profiles:
        print(f"{p:<24}{stats['train_by_profile'].get(p, 0):>7}{stats['val_by_profile'].get(p, 0):>6}")
    if stats["backfilled"]:
        print("backfilled fields:", ", ".join(f"{k}={v}" for k, v in sorted(stats["backfilled"].items())))
    t = stats["tokens_est"]
    print(
        "token estimate (chars/4) per example: "
        f"mean {t['mean']}, p95 {t['p95']}, max {t['max']}  "
        f"(system message alone ~{t['system_only']}; answer mean {t['answer_mean']}, max {t['answer_max']})"
    )
    print("The notebook measures real token counts with the model's own tokenizer and sets max_seq_length from them.")


# --- Pipeline --------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def package(
    *,
    generated_path: Path = GENERATED_CLEAN,
    real_path: Path = REAL_CLEAN,
    generated_db: Path = GENERATED_DB,
    live_db_dir: Path = LIVE_DB_DIR,
    seed: int = 20260916,
    val_fraction: float = 0.1,
    prompt_text: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    prompt_text = prompt_text if prompt_text is not None else load_prompt("briefing").text
    rows = read_jsonl(real_path) + read_jsonl(generated_path)
    examples: list[dict[str, Any]] = []
    dropped = 0
    prompt_mismatch = 0
    for row in rows:
        if row.get("system") and row["system"] != prompt_text:
            prompt_mismatch += 1
        if not complete_output(row, generated_db=generated_db, live_db_dir=live_db_dir):
            dropped += 1
            continue
        examples.append(build_example(row, prompt_text))
    if prompt_mismatch:
        print(
            f"warning: {prompt_mismatch} rows carry a system prompt that differs from the "
            "current production prompt; all examples are packaged with the CURRENT prompt.",
            file=sys.stderr,
        )
    train, val = stratified_split(examples, seed=seed, val_fraction=val_fraction)
    stats = summarise(train, val)
    stats["dropped"] = dropped
    stats["prompt_mismatch"] = prompt_mismatch
    return train, val, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--val-fraction", type=float, default=0.1)
    parser.add_argument("--generated", type=Path, default=GENERATED_CLEAN)
    parser.add_argument("--real", type=Path, default=REAL_CLEAN)
    parser.add_argument("--generated-db", type=Path, default=GENERATED_DB)
    parser.add_argument("--live-db-dir", type=Path, default=LIVE_DB_DIR)
    parser.add_argument("--train-out", type=Path, default=TRAIN_OUT)
    parser.add_argument("--val-out", type=Path, default=VAL_OUT)
    args = parser.parse_args(argv)

    train, val, stats = package(
        generated_path=args.generated,
        real_path=args.real,
        generated_db=args.generated_db,
        live_db_dir=args.live_db_dir,
        seed=args.seed,
        val_fraction=args.val_fraction,
    )
    write_jsonl(args.train_out, train)
    write_jsonl(args.val_out, val)
    print(f"wrote {args.train_out} and {args.val_out}")
    print_summary(stats, stats["dropped"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
