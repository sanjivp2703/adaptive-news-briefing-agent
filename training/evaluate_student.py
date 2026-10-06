"""Score a fine-tuned briefing model against the teacher on the held-out split.

    export LOCAL_MODEL_BASE_URL=http://localhost:11434/v1
    export LOCAL_MODEL_NAME=news-briefer
    PYTHONPATH=src:. .venv/bin/python training/evaluate_student.py

For every row in `training/data/sft_val.jsonl` the student is sent exactly the
system and user messages it was trained on, and its answer is scored the same
way the teacher's was: the generator's hard rules (`run_checks`) and the
plainness bundle (reading grade, definitions per 100 words, domain-term
density). The teacher's answer in the same row is scored alongside, so the
output is a side-by-side table: per-rule pass rates, and plainness by reader
profile for both.

Results go to `training/data/student_eval.json` (every row, both answers,
every check) and a summary is printed. `--offline` scores a stand-in student
that replays the teacher's answer, which exercises the whole path with no
server and is what the tests run.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from conversational_agent import config
from conversational_agent.local_client import LocalModelClient, extract_json_object
from harness.plainness import plainness
from training.generate_sft import normalize_term, run_checks

HERE = Path(__file__).resolve().parent
DEFAULT_VAL = HERE / "data" / "sft_val.jsonl"
DEFAULT_OUT = HERE / "data" / "student_eval.json"
RULES = (
    "no_question_mark",
    "references_only_packet_event",
    "numbers_supported",
    "no_regloss",
    "explained_subset_of_used",
)
PROFILES = ("beginner", "partial", "expert_one_subdomain", "expert", "returning_reader", "real")
PLAINNESS_FIELDS = ("fk_grade", "definitions_per_100w", "domain_term_density")

Student = Callable[[list[dict[str, str]]], str]
"""Takes the chat messages (system, user) and returns the raw answer text."""


# --- Rows ----------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def packet_of(row: dict[str, Any]) -> dict[str, Any]:
    """The context the briefing call was given, recovered from the user message."""
    body = json.loads(row["messages"][1]["content"])
    return body["context"] if isinstance(body, dict) and "context" in body else body


def vocabulary_of(packet: dict[str, Any]) -> tuple[str, ...]:
    """Every term the ledger holds, any state: the group's vocabulary as this
    row saw it. The generator used the fixture's vocabulary; the packet is the
    closest stand-in the validation file carries."""
    ledger = packet.get("concept_ledger") or {}
    return tuple(sorted({normalize_term(t) for terms in ledger.values() for t in terms}))


def other_events_for(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Per group family, the headline+detail of every event in the split,
    so `references_only_packet_event` has something to compare against."""
    by_group: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        event = packet_of(row)["events"][0]
        text = f"{event.get('headline') or ''} {event.get('detail') or ''}"
        by_group[row["meta"].get("group_family") or "?"][event.get("headline") or text] = text
    return {g: list(v.values()) for g, v in by_group.items()}


def parse_answer(text: str) -> dict[str, Any] | None:
    """The JSON object in a model answer, or None when there is none."""
    candidate = extract_json_object(text or "")
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# --- Scoring ------------------------------------------------------------------


def score_answer(
    packet: dict[str, Any], output: dict[str, Any] | None, others: list[str]
) -> dict[str, Any]:
    event = packet["events"][0]
    own = f"{event.get('headline') or ''} {event.get('detail') or ''}"
    checks = run_checks(packet, output, [o for o in others if o != own])
    briefing = str((output or {}).get("briefing") or "")
    explained = [t for t in (output or {}).get("explained_terms") or [] if isinstance(t, str)]
    return {
        "parsed": output is not None,
        "checks": checks,
        "plainness": plainness(briefing, vocabulary_of(packet), explained),
        "briefing": briefing,
        "explained_terms": explained,
        "terms_used": [t for t in (output or {}).get("terms_used") or [] if isinstance(t, str)],
    }


def evaluate(
    rows: list[dict[str, Any]], student: Student, *, progress: Callable[[str], None] = print
) -> dict[str, Any]:
    others = other_events_for(rows)
    results = []
    started = time.monotonic()
    for i, row in enumerate(rows, 1):
        packet = packet_of(row)
        group = row["meta"].get("group_family") or "?"
        t0 = time.monotonic()
        raw = student(row["messages"][:2])
        seconds = round(time.monotonic() - t0, 2)
        teacher_output = parse_answer(row["messages"][2]["content"])
        results.append(
            {
                "id": row["meta"].get("id"),
                "profile": row["meta"].get("profile") or "real",
                "group_family": group,
                "student_seconds": seconds,
                "student_raw": raw,
                "student": score_answer(packet, parse_answer(raw), others.get(group, [])),
                "teacher": score_answer(packet, teacher_output, others.get(group, [])),
            }
        )
        progress(f"  {i}/{len(rows)}  {seconds:5.1f}s  {row['meta'].get('profile')}")
    return {
        "rows": len(rows),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "results": results,
        "summary": summarise(results),
    }


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"rules": {}, "plainness_by_profile": {}, "parsed": {}}
    for who in ("student", "teacher"):
        out["parsed"][who] = sum(1 for r in results if r[who]["parsed"])
        out["rules"][who] = {
            rule: sum(1 for r in results if r[who]["checks"].get(rule)) for rule in RULES
        }
        out["rules"][who]["all"] = sum(1 for r in results if r[who]["checks"].get("passed"))
    for profile in PROFILES:
        rows = [r for r in results if r["profile"] == profile]
        if not rows:
            continue
        entry: dict[str, Any] = {"n": len(rows)}
        for who in ("student", "teacher"):
            entry[who] = {
                field: round(statistics.mean(r[who]["plainness"][field] for r in rows), 2)
                for field in PLAINNESS_FIELDS
            }
        out["plainness_by_profile"][profile] = entry
    secs = [r["student_seconds"] for r in results]
    out["student_seconds_mean"] = round(statistics.mean(secs), 1) if secs else None
    return out


def format_summary(summary: dict[str, Any], n: int) -> str:
    lines = [f"Hard rules, passed out of {n} held-out packets (student | teacher):"]
    for rule in (*RULES, "all"):
        s, t = summary["rules"]["student"][rule], summary["rules"]["teacher"][rule]
        lines.append(f"  {rule:<30} {s:3d} | {t:3d}")
    lines.append(f"  {'valid JSON answer':<30} {summary['parsed']['student']:3d} | {summary['parsed']['teacher']:3d}")
    lines.append("")
    lines.append("Plainness by reader profile (student | teacher):")
    lines.append(f"  {'profile':<22}{'n':>4}  {'grade':>13}  {'defs/100w':>13}  {'terms/100w':>13}")
    for profile, entry in summary["plainness_by_profile"].items():
        cells = [
            f"{entry['student'][f]:5.2f} | {entry['teacher'][f]:5.2f}" for f in PLAINNESS_FIELDS
        ]
        lines.append(f"  {profile:<22}{entry['n']:>4}  " + "  ".join(cells))
    if summary.get("student_seconds_mean") is not None:
        lines.append("")
        lines.append(f"Student answered in {summary['student_seconds_mean']} s per packet on average.")
    return "\n".join(lines)


# --- Students -----------------------------------------------------------------


def local_student(client: LocalModelClient, max_tokens: int = 1200) -> Student:
    """Sends the row's own system and user messages, unchanged, to the local
    endpoint. Not `complete_json`: that would append the schema tail a second
    time, and the training rows already contain it."""

    def ask(messages: list[dict[str, str]]) -> str:
        body = {
            "model": client.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        status, text = client.post(client.endpoint, body, client._headers(), client.timeout)
        if status >= 400:
            raise RuntimeError(f"local model returned HTTP {status}: {text[:300]}")
        payload = json.loads(text)
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("local model returned no choices")
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    return ask


def replay_student(rows: list[dict[str, Any]]) -> Student:
    """Offline stand-in: answers every packet with the teacher's own answer, so
    the pipeline can be exercised and the student columns should equal the
    teacher columns exactly."""
    answers = {row["messages"][1]["content"]: row["messages"][2]["content"] for row in rows}

    def ask(messages: list[dict[str, str]]) -> str:
        return answers[messages[1]["content"]]

    return ask


# --- CLI -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--val", type=Path, default=DEFAULT_VAL, help="held-out split (default: training/data/sft_val.jsonl)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the full results")
    parser.add_argument("--limit", type=int, default=None, help="score only the first N rows")
    parser.add_argument("--offline", action="store_true", help="replay the teacher as the student; no server needed")
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--model", default=None,
                        help="served model name to score instead of $LOCAL_MODEL_NAME, e.g. the untuned base model")
    args = parser.parse_args(argv)

    if not args.val.exists():
        print(f"No validation file at {args.val}. Run training/package_sft.py first.", file=sys.stderr)
        return 2
    rows = read_jsonl(args.val)
    if args.limit:
        rows = rows[: args.limit]

    if args.offline:
        student = replay_student(rows)
        label = "offline replay of the teacher"
    else:
        if not os.environ.get(config.LOCAL_MODEL_BASE_URL_ENV):
            print(
                f"Set {config.LOCAL_MODEL_BASE_URL_ENV} and {config.LOCAL_MODEL_NAME_ENV} "
                "to point at the served student, or pass --offline.",
                file=sys.stderr,
            )
            return 2
        client = LocalModelClient(model=args.model)
        student = local_student(client, max_tokens=args.max_tokens)
        label = f"{client.model} at {client.base_url}"

    print(f"Scoring {len(rows)} held-out packets against {label}")
    report = evaluate(rows, student)
    report["student"] = label
    report["val_file"] = str(args.val)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print()
    print(format_summary(report["summary"], len(rows)))
    print()
    print(f"Full results: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
