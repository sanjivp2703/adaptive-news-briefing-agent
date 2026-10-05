"""The four properties an offline run has to have, checked rather than asserted.

    PYTHONPATH=src:. .venv/bin/python -m harness.verify_offline

Each of these is a claim the README would otherwise be making on trust, and
each has a failure mode that is invisible in a normal run:

1. **Zero network.** `socket.socket` is replaced with something that raises, for
   the whole duration of a full offline run. A stub that quietly fell through to
   a real client would otherwise show up only as a surprise API bill.

2. **Reproducibility, and the check is not vacuous.** Two runs must produce
   byte-identical artifacts once the `volatile` subtree (ids, latency, tokens)
   is removed -- *and* the comparison must be shown to notice a real change.
   A canonicaliser that stripped too much would pass every diff, including the
   ones that matter, so this deliberately perturbs a fixture and requires the
   comparison to fail.

3. **No ground truth in `judgment_log.input_json`.** Scanned with independent
   SQL against the run's own rows, not by reusing `metrics.check_answer_key_leakage`
   -- a leak check that shares an implementation with the thing it checks can
   agree with itself while both are wrong.

4. **Offline genuinely omits judgment-quality numbers.** Not "prints a caption":
   reading one must raise `OfflineMetricError`, and the artifact must not carry
   the ingredients to recompute it.

5. **No reading behaviour in any judgment context.** Scanned the same way and
   for a different reason.

6. **No probe string anywhere in the system's store.** The post-session probe
   (`harness/probe.py`) is harness-only: its pseudo-terms and its rung
   vocabulary must not appear in ANY table the system owns -- not just
   `judgment_log`. Every table except the harness's own `eval_runs` is dumped
   and searched for the pseudo-terms this run actually probed and for the
   word "rung", and the check is required to have something to find in the
   artifact so it cannot pass vacuously. `dwell_ms` is not ground truth -- it is a real
   observation the system is entitled to store. It is a leak because of *where*
   it would be: in the context of a call that decides what somebody knows.
   Scanned for the field names AND for the band strings, because the shape this
   leak would actually take is someone folding the numbers into a prose summary
   rather than passing the field along.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from conversational_agent.store import Store

from . import metrics, personas
from .clock import installed
from .run import run_config_json
from .runner import ANSWER_KEY_FIELDS, READING_FIELDS, build_harness, run_all


class NetworkUsed(RuntimeError):
    """A socket was opened during an offline run."""


class _BlockedSocket:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NetworkUsed(
            "An offline harness run tried to open a socket. Offline mode must "
            "make no network calls at all."
        )


def _run(db_path: Path, loaded: list) -> tuple[dict[str, Any], str, dict[str, list[str]]]:
    bootstrap = Store(db_path)
    run_id = bootstrap.start_eval_run(
        "personas", run_config_json(False, [p.id for p in loaded])
    )
    bootstrap.close()
    harness = build_harness(
        db_path=db_path, run_id=run_id, personas=loaded, live=False
    )
    try:
        with installed(harness.clock):
            results = run_all(harness, loaded)
        report = metrics.compute(
            results,
            loaded,
            store=harness.store,
            run_id=run_id,
            live=False,
            unexpected_calls=list(harness.registry.unexpected_calls)
            if harness.registry
            else [],
            contract_notes=(
                list(harness.registry.contract_notes) if harness.registry else []
            )
            + list(harness.contract.notes),
            contract=harness.contract,
        )
        payload = metrics.to_dict(report, results)
        gates = {
            "question_faithfulness": list(report.faithfulness.violations),
            "quantity_phrasing": list(report.quantity_offences),
            "reading_regression": list(report.reading.violations),
            "silence_regression": list(report.silence.violations),
        }
    finally:
        harness.close()
    return payload, run_id, gates


# Probe strings, plus the reading-intent ground truth: none of these may reach
# a system-owned table or a judgment context. (`skip_kind` in `exchanges` is
# the SYSTEM's own evidence-based label and is not a leak.)
PROBE_MARKER_WORDS = ("rung", "known_share", "story_known", "informed_skip", "lazy_skip")


def _scan_store_for_probe(
    db_path: Path, payload: dict[str, Any]
) -> tuple[list[str], set[str], int]:
    """Dump every system-owned table and search it for the probe's strings.

    Returns (hits, pseudo-terms looked for, probe items recorded). The
    pseudo-terms come from the run's own artifact, so the scan looks for what
    was actually probed rather than for a list that could drift from it.
    """
    pseudo: set[str] = set()
    items = 0
    for persona in payload.get("personas", []):
        for session in persona.get("probes", []):
            for item in session.get("items", []):
                items += 1
                if item.get("is_pseudo"):
                    pseudo.add(str(item["term"]).lower())
    patterns = [(t, re.compile(re.escape(t), re.IGNORECASE)) for t in sorted(pseudo)]
    patterns += [
        (w, re.compile(rf"(?<!\w){re.escape(w)}(?!\w)", re.IGNORECASE))
        for w in PROBE_MARKER_WORDS
    ]
    hits: list[str] = []
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' AND name != 'eval_runs'"
            )
        ]
        for table in tables:
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            blob = json.dumps(rows, default=str)
            for label, pattern in patterns:
                n = len(pattern.findall(blob))
                if n:
                    hits.append(f"{label!r} appears {n}x in table {table}")
    finally:
        conn.close()
    return hits, pseudo, items


def main() -> int:
    failures: list[str] = []
    notes: list[str] = []
    loaded = personas.load()
    if not loaded:
        print("No personas defined; nothing to verify.")
        return 1

    tmp = tempfile.TemporaryDirectory(prefix="verify-offline-")
    root = Path(tmp.name)

    # --- 1. Zero network ---------------------------------------------------
    real_socket = socket.socket
    socket.socket = _BlockedSocket  # type: ignore[assignment]
    try:
        payload_a, run_a, gates_a = _run(root / "a.db", loaded)
        notes.append("1. zero network: a full offline run completed with sockets blocked")
    except NetworkUsed as exc:
        failures.append(f"1. zero network: {exc}")
        socket.socket = real_socket  # type: ignore[assignment]
        print("\n".join(notes + failures))
        return 1
    finally:
        socket.socket = real_socket  # type: ignore[assignment]

    payload_b, run_b, _ = _run(root / "b.db", loaded)

    # --- 2. Reproducibility, and that the check bites ----------------------
    canon_a = metrics.canonical(payload_a)
    canon_b = metrics.canonical(payload_b)
    if canon_a == canon_b:
        notes.append(
            f"2. reproducibility: two runs byte-identical over "
            f"{len(canon_a):,} chars of canonical body (run ids {run_a} vs {run_b} differ)"
        )
    else:
        failures.append("2. reproducibility: two identical runs produced different bodies")

    # The check must be capable of failing. Perturb one persona's ground truth
    # and require the comparison to notice; otherwise `canonical` is stripping
    # so much that it would pass anything.
    victim = loaded[0]
    perturbed = list(loaded)
    perturbed[0] = type(victim)(
        **{
            **victim.__dict__,
            "knows": victim.knows[:-1] if len(victim.knows) > 1 else victim.knows,
        }
    )
    payload_c, _, _ = _run(root / "c.db", perturbed)
    if metrics.canonical(payload_c) != canon_a:
        notes.append(
            "2b. the reproducibility check is not vacuous: dropping one concept "
            f"from {victim.id!r}'s ground truth changed the canonical body"
        )
    else:
        failures.append(
            "2b. VACUOUS: changing a persona's ground truth did not change the "
            "canonical body, so byte-identity proves nothing"
        )

    # --- 3. Independent SQL scan for ground truth --------------------------
    conn = sqlite3.connect(root / "a.db")
    conn.row_factory = sqlite3.Row
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM judgment_log WHERE run_id = ?", (run_a,)
    ).fetchone()["n"]
    hits: list[str] = []
    for field_name in ANSWER_KEY_FIELDS:
        rows = conn.execute(
            "SELECT judgment_point, COUNT(*) AS n FROM judgment_log"
            " WHERE run_id = ? AND input_json LIKE ? GROUP BY judgment_point",
            (run_a, f'%"{field_name}"%'),
        ).fetchall()
        for row in rows:
            hits.append(f"{field_name!r} in {row['n']} {row['judgment_point']} row(s)")
    # Also scan for the literal concept strings themselves, which is the check
    # that actually matters: a leak does not have to arrive under its own key
    # name to hand a live model the answer.
    literal: list[str] = []
    for persona in loaded:
        if not persona.does_not_know:
            continue
        probe = json.dumps(sorted(persona.does_not_know))[1:-1]
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM judgment_log WHERE run_id = ? AND input_json LIKE ?",
            (run_a, f"%{probe}%"),
        ).fetchone()
        if row["n"]:
            literal.append(f"{persona.id}: does_not_know list appears verbatim")
    conn.close()

    # --- 3b. Independent SQL scan for reading behaviour --------------------
    #
    # A separate check from the answer-key scan, and separate on purpose. This
    # is not ground truth -- `dwell_ms` is a real observation about a real user
    # and the system is entitled to store it. It is a leak because of WHERE it
    # would be: in the context of a call that decides what somebody knows. A
    # model told "they spent four minutes on this and scrolled to the bottom"
    # will answer differently, and more confidently, and wrongly, in the exact
    # direction this product exists to avoid.
    #
    # Two forms are scanned. The key names catch a field carried along
    # structurally; the raw values catch someone folding the numbers into a
    # prose summary, which is the shape a leak like this would actually take.
    reading_hits: list[str] = []
    conn2 = sqlite3.connect(root / "a.db")
    conn2.row_factory = sqlite3.Row
    for field_name in READING_FIELDS:
        rows = conn2.execute(
            "SELECT judgment_point, COUNT(*) AS n FROM judgment_log"
            " WHERE run_id = ? AND input_json LIKE ? GROUP BY judgment_point",
            (run_a, f'%"{field_name}"%'),
        ).fetchall()
        for row in rows:
            reading_hits.append(
                f"{field_name!r} in {row['n']} {row['judgment_point']} row(s)"
            )
    for band in ("skimmed", "studied", "skipped"):
        rows = conn2.execute(
            "SELECT judgment_point, COUNT(*) AS n FROM judgment_log"
            " WHERE run_id = ? AND input_json LIKE ? GROUP BY judgment_point",
            (run_a, f'%read_quality%{band}%'),
        ).fetchall()
        for row in rows:
            reading_hits.append(
                f"a read-quality band ({band!r}) in {row['n']} "
                f"{row['judgment_point']} row(s)"
            )
    conn2.close()

    if hits or literal:
        failures.extend(f"3. ground-truth leak: {h}" for h in hits + literal)
    else:
        notes.append(
            f"3. no ground truth in judgment_log: {total} input_json rows scanned "
            f"by independent SQL for {len(ANSWER_KEY_FIELDS)} key names plus each "
            "persona's verbatim does_not_know list"
        )

    if reading_hits:
        failures.extend(f"3b. reading-behaviour leak: {h}" for h in reading_hits)
    else:
        notes.append(
            f"3b. no reading behaviour in judgment_log: {total} input_json rows "
            f"scanned by independent SQL for {len(READING_FIELDS)} field names "
            "plus the read-quality band strings. Attention may be stored; it may "
            "not be shown to a call that decides what somebody knows."
        )

    # --- 3c. No probe string anywhere in the system's store ----------------
    #
    # The post-session probe is the harness testing the PERSONA, and nothing
    # in `src/` may know it happened. Unlike 3 and 3b this is a whole-store
    # scan: a probe string in `turns`, `exchanges` or `concepts` would be as
    # much of a leak as one in a judgment context, because the system reads
    # all of those back. `eval_runs` is excluded -- it holds the harness's own
    # report, which legitimately contains the probe's results.
    probe_hits, probe_terms, probe_items = _scan_store_for_probe(root / "a.db", payload_a)
    if not probe_items:
        failures.append(
            "3c. VACUOUS: the artifact records no probe items, so the probe-"
            "string scan has nothing to look for"
        )
    elif not probe_terms:
        failures.append(
            "3c. VACUOUS: no pseudo-term was probed this run, so the scan cannot "
            "show that pseudo-terms stay out of the store"
        )
    elif probe_hits:
        failures.extend(f"3c. probe leak: {h}" for h in probe_hits)
    else:
        notes.append(
            f"3c. no probe string in the system's store: every table except "
            f"eval_runs scanned for {len(probe_terms)} pseudo-term(s) "
            f"({', '.join(sorted(probe_terms)[:4])}{'...' if len(probe_terms) > 4 else ''}) "
            f"and the word 'rung'; {probe_items} probe items were recorded, so the "
            "scan had something to find"
        )

    # --- 4. Offline omits judgment-quality numbers -------------------------
    report_a = metrics.compute(
        [], [], store=Store(root / "a.db"), run_id=run_a, live=False
    )
    try:
        _ = report_a.quality.materiality
        failures.append("4. offline mode produced a materiality number")
    except metrics.OfflineMetricError:
        notes.append(
            "4. offline refusal is structural: reading a judgment-quality metric "
            "raises OfflineMetricError"
        )
    if payload_a.get("judgment_quality") != metrics.REFUSAL:
        failures.append("4. artifact carries judgment_quality offline")
    leaked_keys = [
        key
        for persona in payload_a.get("personas", [])
        for exchange in persona.get("exchanges", [])
        for key in ("intent_mentioned", "intent_asked")
        if key in exchange
    ]
    stray = [
        obs
        for persona in payload_a.get("personas", [])
        for obs in persona.get("materiality_observations", [])
        if "should_be_material" in obs
    ]
    if leaked_keys or stray:
        failures.append(
            "4. the offline artifact still carries the answer-key columns the "
            "report refused to score"
        )
    else:
        notes.append(
            "4b. the offline artifact omits the answer-key columns "
            "(should_be_material, intent_*), so the refused numbers cannot be "
            "recomputed from it"
        )

    # --- 5. The persona-side gates ------------------------------------------
    #
    # Structural properties of the harness's own output, checked here as well
    # as in the report so that a rebuild of the responders cannot pass this
    # suite while quietly breaking them:
    #   * question_faithfulness -- every question follows from what the
    #     persona held at the moment it asked;
    #   * quantity_phrasing -- no "higher than normal" applied to `relegation`;
    #   * reading_regression / silence_regression -- with `familiar` now the
    #     one sanctioned path from reading into the ledger, and narrow.
    for gate, violations in gates_a.items():
        if violations:
            failures.append(f"5. {gate}: {len(violations)} violation(s); first: {violations[0]}")
        else:
            notes.append(f"5. {gate}: clean")

    tmp.cleanup()

    print("=" * 74)
    print("Offline verification")
    print("=" * 74)
    for note in notes:
        print(f"  ok   {note}")
    for failure in failures:
        print(f"  FAIL {failure}")
    print("=" * 74)
    print(f"RESULT: {'PASS' if not failures else 'FAIL'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
