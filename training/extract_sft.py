"""Pull every logged briefing call out of the live eval databases as SFT data.

One training example = the exact packet the briefing call received (the
`input_json` the Judge logged) + the exact answer the model gave (the
`verdict_json`). Nothing is rewritten; the point of the log is that it is the
ground truth of what happened.

Usage (from the project root):

    PYTHONPATH=src:. .venv/bin/python training/extract_sft.py

Writes `training/data/sft_briefings.jsonl` (everything) and
`training/data/sft_briefings.clean.jsonl` (the rows worth training on), and
prints a summary. Reads the databases read-only; never touches credentials.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from conversational_agent import config
from conversational_agent.judgment import load_prompt

LIVE_DIR = PROJECT_ROOT / "eval-runs" / "live"
DEFAULT_DBS = [
    "all5c",
    "all5",
    "probe4_sam",
    "probe4_pilar",
    "sub4_pilar",
    # Older prompt versions. Tagged by their prompt_version; still usable.
    "pilar",
    "pilar2",
    "sam",
    "sam2",
    "sam3",
]
# Briefing prompt versions close enough to the current one to train on.
CLEAN_PROMPT_VERSIONS = {"v5", "v6", "v7"}


# --- Reading one database --------------------------------------------------


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _loads(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _norm_terms(raw: Any) -> set[str]:
    if isinstance(raw, str):
        raw = _loads(raw)
    if not isinstance(raw, list):
        return set()
    return {t.strip().lower() for t in raw if isinstance(t, str) and t.strip()}


def _exchanges_by_briefing(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """The exchange rows keyed by their briefing text, so a logged verdict can
    be joined to the thread that followed it. Older schemas lack
    `explained_terms`; `asked_about` has been there since the start."""
    cols = _columns(conn, "exchanges")
    if "briefing" not in cols or "asked_about" not in cols:
        return {}
    by_text: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute("SELECT id, briefing, asked_about FROM exchanges"):
        if row["briefing"]:
            by_text[row["briefing"].strip()].append(row)
    return by_text


def _reask(
    briefing: str | None,
    explained: set[str],
    exchanges: dict[str, list[sqlite3.Row]],
) -> bool | None:
    """True if the user later asked about a term this briefing had defined.

    None when the briefing cannot be joined to an exchange, or the exchange
    was never closed (no `asked_about` extracted yet). An exact, normalised
    term match -- the same normalisation the ledger uses -- so "reask" means
    the thread's evidence extractor named the very term we glossed.
    """
    if not briefing or not explained:
        return None if not briefing else False
    matches = exchanges.get(briefing.strip())
    if not matches:
        return None
    asked: set[str] = set()
    saw_closed = False
    for row in matches:
        if row["asked_about"] is None:
            continue
        saw_closed = True
        asked |= _norm_terms(row["asked_about"])
    if not saw_closed:
        return None
    return bool(asked & explained)


def extract_db(
    name: str, path: Path, current_version: str, current_text: str
) -> list[dict[str, Any]]:
    conn = _connect(path)
    try:
        runs = {r["id"]: r["passed"] for r in conn.execute("SELECT id, passed FROM eval_runs")}
        exchanges = _exchanges_by_briefing(conn)
        rows = conn.execute(
            """
            SELECT id, created_at, user_id, group_id, prompt_version, model,
                   input_json, verdict_json, error, run_id,
                   input_tokens, output_tokens, latency_ms
            FROM judgment_log
            WHERE judgment_point = 'briefing'
            ORDER BY created_at, id
            """
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for r in rows:
        payload = _loads(r["input_json"])
        verdict = _loads(r["verdict_json"]) if r["error"] is None else None
        output = None
        if isinstance(verdict, dict):
            output = {
                k: verdict[k]
                for k in ("briefing", "topic", "explained_terms", "terms_used", "subdomains")
                if k in verdict
            }
        briefing = (output or {}).get("briefing") if output else None
        explained = _norm_terms((output or {}).get("explained_terms")) if output else set()
        words = len(briefing.split()) if isinstance(briefing, str) else 0
        definitions = len(explained)

        run_passed = runs.get(r["run_id"]) if r["run_id"] else None
        record: dict[str, Any] = {
            "id": r["id"],
            "source_db": name,
            "run_id": r["run_id"],
            "user_id": r["user_id"],
            "group_id": r["group_id"],
            "prompt_version": r["prompt_version"],
            "model": r["model"],
            "created_at": r["created_at"],
            "system_text_available": r["prompt_version"] == current_version,
        }
        if record["system_text_available"]:
            record["system"] = current_text
        record["input"] = payload
        record["output"] = output
        record["quality"] = {
            "gate_run_passed": None if run_passed is None else bool(run_passed),
            "errored": r["error"] is not None,
            "error": (r["error"][:200] if r["error"] else None),
            "question_mark_in_briefing": ("?" in briefing) if isinstance(briefing, str) else False,
            "words": words,
            "definitions": definitions,
            "defs_per_100w": round(100.0 * definitions / words, 2) if words else 0.0,
            "reask": _reask(briefing, explained, exchanges),
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "latency_ms": r["latency_ms"],
        }
        out.append(record)
    return out


# --- The clean split -------------------------------------------------------


def _dedup_key(record: dict[str, Any]) -> tuple[str, str]:
    context = ((record.get("input") or {}).get("context")) or {}
    events = context.get("events") or []
    headlines = tuple(
        (e.get("headline") or "").strip() for e in events if isinstance(e, dict)
    )
    ledger = context.get("concept_ledger") or {}
    return (json.dumps(headlines, ensure_ascii=False), json.dumps(ledger, sort_keys=True))


def is_clean(record: dict[str, Any], gate: str) -> bool:
    q = record["quality"]
    if q["errored"] or record["output"] is None:
        return False
    if q["question_mark_in_briefing"]:
        return False
    if record["prompt_version"] not in CLEAN_PROMPT_VERSIONS:
        return False
    if gate == "require" and q["gate_run_passed"] is False:
        return False
    return True


def clean_split(records: list[dict[str, Any]], gate: str) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for record in records:
        if not is_clean(record, gate):
            continue
        key = _dedup_key(record)
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


# --- Summary ---------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in records if r["output"] is not None]
    reasks = [r["quality"]["reask"] for r in ok if r["quality"]["reask"] is not None]
    return {
        "rows": len(records),
        "with_output": len(ok),
        "mean_words": round(_mean([r["quality"]["words"] for r in ok]), 1),
        "mean_defs_per_100w": round(_mean([r["quality"]["defs_per_100w"] for r in ok]), 2),
        "question_marks": sum(1 for r in ok if r["quality"]["question_mark_in_briefing"]),
        "reask_known": len(reasks),
        "reask_rate": round(_mean([1.0 if v else 0.0 for v in reasks]), 3) if reasks else None,
    }


def print_summary(
    records: list[dict[str, Any]],
    clean: list[dict[str, Any]],
    clean_nogate: list[dict[str, Any]],
    out_dir: Path,
) -> None:
    def block(title: str, counter: Counter) -> None:
        print(f"\n{title}")
        for key, n in counter.most_common():
            print(f"  {str(key):<24} {n:>4}")

    per_db = Counter()
    per_db_err = Counter()
    for r in records:
        per_db[r["source_db"]] += 1
        if r["quality"]["errored"]:
            per_db_err[r["source_db"]] += 1
    print("\nRows per DB (errored in brackets)")
    for name in DEFAULT_DBS + sorted(set(per_db) - set(DEFAULT_DBS)):
        if name in per_db or name in DEFAULT_DBS:
            print(f"  {name:<24} {per_db.get(name, 0):>4}  [{per_db_err.get(name, 0)}]")
    block("Rows per prompt version", Counter(r["prompt_version"] for r in records))
    block("Rows per model", Counter(r["model"] for r in records))
    block(
        "Gate outcome of the run each row came from",
        Counter(str(r["quality"]["gate_run_passed"]) for r in records),
    )

    print("\nQuality (all rows with an output)")
    for k, v in _stats(records).items():
        print(f"  {k:<20} {v}")
    versions = sorted(CLEAN_PROMPT_VERSIONS)
    print(f"\nClean split -- sft_briefings.clean.jsonl (versions={versions}, deduped, gate must not have failed)")
    for k, v in _stats(clean).items():
        print(f"  {k:<20} {v}")
    print("\nClean split -- sft_briefings.clean_nogate.jsonl (same, gate verdict ignored)")
    for k, v in _stats(clean_nogate).items():
        print(f"  {k:<20} {v}")
    print(f"\nWrote sft_briefings.jsonl, .clean.jsonl and .clean_nogate.jsonl under {out_dir}")


# --- Main ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--db",
        action="append",
        default=None,
        help="database name (in eval-runs/live) or path; repeatable. Default: the known live set.",
    )
    parser.add_argument("--out-dir", type=Path, default=HERE / "data")
    args = parser.parse_args(argv)

    prompt = load_prompt(config.BRIEFING)
    names = args.db or DEFAULT_DBS
    records: list[dict[str, Any]] = []
    for name in names:
        path = Path(name)
        if not path.exists():
            path = LIVE_DIR / (name if name.endswith(".db") else f"{name}.db")
        if not path.exists():
            print(f"  skip {name}: no such database at {path}", file=sys.stderr)
            continue
        records.extend(extract_db(path.stem, path, prompt.version, prompt.text))

    # Two clean files, same filters except for the gate:
    #   sft_briefings.clean.jsonl        -- a row whose eval run FAILED its
    #                                       gate is excluded (unknown is fine)
    #   sft_briefings.clean_nogate.jsonl -- the gate verdict is recorded on
    #                                       each row but does not filter
    # Both exist because, as of writing, every live run has failed the gate --
    # on ledger precision/recall, which says nothing about the briefing prose
    # (the `no_questions_to_user` check passed in all of them). The strict
    # file is the rule as specified; the other is what there is to train on
    # until a run passes. Which one to use is a human call, not the script's.
    clean = clean_split(records, "require")
    clean_nogate = clean_split(records, "ignore")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "sft_briefings.jsonl": records,
        "sft_briefings.clean.jsonl": clean,
        "sft_briefings.clean_nogate.jsonl": clean_nogate,
    }
    for filename, rows in outputs.items():
        with (args.out_dir / filename).open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"Current briefing prompt: {prompt.version} ({len(prompt.text)} chars)")
    print_summary(records, clean, clean_nogate, args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
