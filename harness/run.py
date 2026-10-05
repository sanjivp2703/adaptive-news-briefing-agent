"""`python -m harness.run` -- the persona suite entry point.

    PYTHONPATH=src:. .venv/bin/python -m harness.run [--live] [--persona ID]
                                                     [--db PATH] [--json PATH]
                                                     [--rounds N] [--parallel N]
                                                     [--compare A B]

Registers an `eval_run` row capturing the model routing and prompt versions in
force, threads that run id through `app.build` so every judgment call this run
makes is tagged with it, prints a report, writes a diffable JSON artifact, and
exits non-zero when the run fails.

Offline (the default) makes no network calls at all and is byte-for-byte
reproducible; `harness.verify_offline` proves both rather than asserting them.

The default database is a throwaway temp file. Personas are real rows created
through the real user-scoped API -- that is the point of them -- but they have
no business landing in the user's actual store, and a harness that quietly
appends eight synthetic users per invocation to `data/conversational_agent.db`
would be a nasty surprise. Pointing `--db` at the real store is refused unless
`--allow-real-db` is passed explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from conversational_agent import config
from conversational_agent.store import Store

from . import interrupt_cases, metrics, personas
from .clock import installed
from .config import HarnessRouting
from .materiality_cache import MaterialityCache
from .runner import build_harness, merge_usage, run_all, run_all_parallel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACT_DIR = PROJECT_ROOT / "eval-runs"


def _relative_to_project(path: Path) -> str:
    """A fixture directory as recorded in the artifact: project-relative when
    it lives under the project, so two checkouts of the same tree agree."""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(Path(path).resolve())


def build_run_settings(
    *,
    fixture_dir: Path,
    persona_ids: list[str],
    rounds_cap: int | None,
    horizon_days: float | None,
    retention_probes: int,
    routing: HarnessRouting,
    ledger_truth: str = "dynamic",
    materiality_cache: bool = False,
) -> dict:
    """The artifact's non-volatile header. `harness.gate` refuses to pair two
    artifacts whose fixture set, rounds, horizon or harness routing differ.

    `--parallel` is deliberately NOT here: it changes wall-clock time only and
    the artifact must stay byte-identical across worker counts (README); it
    is recorded under `volatile` instead.
    """
    return {
        "schema": "run_settings/v1",
        "fixture_dir": _relative_to_project(fixture_dir),
        "persona_ids": list(persona_ids),
        "rounds_cap": rounds_cap,
        "horizon_days": horizon_days,
        "retention_probes": int(retention_probes or 0) if horizon_days is not None else 0,
        "harness_routing": routing.to_dict(),
        "ledger_truth": ledger_truth,
        "materiality_cache": bool(materiality_cache),
    }


@contextmanager
def fixture_dir(directory: Path | None) -> Iterator[None]:
    """Temporarily point the loader at a different fixture directory.

    Scoped rebinding, the same technique `clock.py` uses on `store.now_iso`,
    and for the same reason: it avoids adding a seam to a module that is done
    and is being edited elsewhere. Used by `--fixture-dir` for trying a
    candidate fixture set without disturbing the checked-in one.
    """
    if directory is None:
        yield
        return
    original = personas.FIXTURE_DIR
    personas.FIXTURE_DIR = directory  # type: ignore[assignment]
    try:
        yield
    finally:
        personas.FIXTURE_DIR = original  # type: ignore[assignment]


def run_config_json(
    live: bool, persona_ids: list[str], run_settings: dict | None = None
) -> str:
    """What was in force for this run -- the thing a future diff is against."""
    body = {
        "mode": "live" if live else "offline",
        "personas": persona_ids,
        "models": {p: config.model_for(p) for p in sorted(config.ROUTED_CALLS)},
        "effort": {p: config.effort_for(p) for p in sorted(config.ROUTED_CALLS)},
        "prompt_versions": metrics.prompt_versions(),
        "ledger_min_precision": config.LEDGER_MIN_PRECISION,
        "ledger_min_recall": config.LEDGER_MIN_RECALL,
        "ledger_max_interactions": config.LEDGER_MAX_INTERACTIONS,
    }
    if run_settings:
        body["run_settings"] = run_settings
    return json.dumps(body, indent=2, sort_keys=True)


def learning_summary(paths: list[Path]) -> int:
    """Print the learning summary table for existing artifacts and exit."""
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = metrics.learning_report_from_artifact(payload)
        rs = payload.get("run_settings") or {}
        voice = HarnessRouting.from_dict(rs.get("harness_routing")).voice_line if rs else (
            "persona voice: not recorded (pre-header artifact; legacy routing assumed)"
        )
        print(f"{path}  [{payload.get('mode', '?')}]  {voice}")
        print(metrics.render_learning_summary(entries) if entries else "    (no personas)")
        print()
    return 0


def _artifact_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_ARTIFACT_DIR / f"personas-{stamp}.json"


def compare(path_a: Path, path_b: Path) -> int:
    """Diff two artifacts on their reproducible body."""
    a = json.loads(path_a.read_text(encoding="utf-8"))
    b = json.loads(path_b.read_text(encoding="utf-8"))
    canon_a = metrics.canonical(a)
    canon_b = metrics.canonical(b)
    if canon_a == canon_b:
        print(f"IDENTICAL (ignoring the 'volatile' subtree)\n  {path_a}\n  {path_b}")
        return 0

    import difflib

    print(f"DIFFERENT\n  a: {path_a}\n  b: {path_b}\n")
    diff = difflib.unified_diff(
        canon_a.splitlines(),
        canon_b.splitlines(),
        fromfile=str(path_a),
        tofile=str(path_b),
        lineterm="",
    )
    for line in diff:
        print(line)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness.run", description="Persona-based evaluation harness."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="score against the real models (needs credentials, costs money)",
    )
    parser.add_argument("--persona", default=None, help="run a single persona by id")
    parser.add_argument("--db", type=Path, default=None, help="database path")
    parser.add_argument("--json", type=Path, default=None, help="artifact path")
    parser.add_argument(
        "--rounds", type=int, default=None, help="cap interactions per persona"
    )
    parser.add_argument(
        "--fixture-dir", type=Path, default=None, help="alternate fixture directory"
    )
    parser.add_argument(
        "--no-artifact", action="store_true", help="skip writing the JSON artifact"
    )
    parser.add_argument(
        "--skip-interrupt-cases",
        action="store_true",
        help="skip the interrupt-timing multi-group case suite",
    )
    parser.add_argument(
        "--no-remediation-search",
        action="store_true",
        help="live only: cut the Assessor's large-gap web search (cheaper run)",
    )
    # --- Horizon mode ------------------------------------------------------
    parser.add_argument(
        "--horizon-days",
        type=float,
        default=None,
        metavar="N",
        help=(
            "spread the same events over N simulated days: the clock advances "
            "N/(rounds + retention probes) days between rounds instead of the "
            "group's poll interval. Absent: today's behaviour, byte-identical."
        ),
    )
    parser.add_argument(
        "--retention-probes",
        type=int,
        default=0,
        metavar="K",
        help=(
            "horizon mode only: after the last briefing round add K probe-only "
            "days (no event, no monitor, no briefing, no thread; just the probe "
            "on earlier terms), spaced so the last lands on day N. Recorded as "
            "rounds with probe_only=true; never a briefing or a quiet round."
        ),
    )
    # --- Cheap regression mode ------------------------------------------------
    parser.add_argument(
        "--cheap",
        action="store_true",
        help=(
            "cheap regression: voice the persona (LLMResponder replies and the "
            "probe's persona answers) with Haiku and drop the remediation search. "
            "The probe GRADER stays on Sonnet -- it is the reward signal. The "
            "routing is recorded in the artifact header and the gate refuses to "
            "pair a cheap run against a full one."
        ),
    )
    parser.add_argument(
        "--persona-model",
        default=None,
        metavar="MODEL",
        help=(
            "model that voices the persona (overrides --cheap and "
            "HARNESS_PERSONA_MODEL). Same seed, different voice: a one-command A/B."
        ),
    )
    parser.add_argument(
        "--grader-model",
        default=None,
        metavar="MODEL",
        help="model that grades probe answers (overrides HARNESS_GRADER_MODEL)",
    )
    parser.add_argument(
        "--materiality-cache",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "JSON cache of materiality verdicts keyed on sha256(headline, detail, "
            "group description). Hits are replayed without a model call and their "
            "judgment_log rows are stamped model='cache' with zero tokens; misses "
            "are written back. The hit rate is printed and recorded."
        ),
    )
    parser.add_argument(
        "--learning-summary",
        nargs="+",
        metavar="ARTIFACT",
        type=Path,
        default=None,
        help="print the learning summary table for existing artifact(s) and exit",
    )
    parser.add_argument(
        "--allow-real-db",
        action="store_true",
        help="permit running against the real store (refused by default)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        metavar="N",
        help=(
            "replay up to N personas concurrently (default 1, sequential). "
            "Personas are independent by construction -- each has its own user, "
            "group and clock -- so this only changes wall-clock time. Worth it "
            "under --live, where a run is almost entirely API latency."
        ),
    )
    parser.add_argument(
        "--ledger-truth",
        choices=("dynamic", "static"),
        default="dynamic",
        help=(
            "which answer key the ledger gate is scored against: 'dynamic' "
            "(default; the persona memory model's P(can define) >= 0.5 at each "
            "snapshot) or 'static' (the fixture's `knows` plus asked-and-answered, "
            "never forgotten -- the pre-learning-model key, kept for comparison). "
            "Both are always reported; this picks which one gates."
        ),
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("A", "B"),
        type=Path,
        default=None,
        help="diff two artifacts and exit",
    )
    parser.add_argument(
        "--show-thread",
        metavar="PERSONA[:ROUND]",
        default=None,
        help=(
            "after the report, print one persona's thread verbatim -- briefing, "
            "the questions it asked, the answers it got. The report is a wall "
            "of aggregates; this is the thing they are about."
        ),
    )
    args = parser.parse_args(argv)

    if args.compare:
        return compare(*args.compare)
    if args.learning_summary:
        return learning_summary(args.learning_summary)

    if args.horizon_days is not None and args.horizon_days <= 0:
        print("--horizon-days takes a positive number of days.")
        return 2
    if args.retention_probes < 0:
        print("--retention-probes takes a non-negative integer.")
        return 2
    if args.retention_probes and args.horizon_days is None:
        print("--retention-probes needs --horizon-days: probe-only days are placed on the horizon.")
        return 2

    routing = HarnessRouting.resolve(
        cheap=args.cheap,
        persona_model_override=args.persona_model,
        grader_model_override=args.grader_model,
        remediation_search=not args.no_remediation_search,
    )

    with fixture_dir(args.fixture_dir):
        loaded = personas.load(args.persona)

    if not loaded:
        where = args.fixture_dir or personas.FIXTURE_DIR
        if args.persona:
            print(f"No persona with id {args.persona!r} in {where}.")
            return 1
        # Acceptance criterion: an empty fixture set is a clean no-op, not a
        # failure. There is nothing to regress.
        print(f"No personas defined (no *.json fixtures in {where}). Nothing to run.")
        return 0

    if args.parallel < 1:
        print("--parallel takes a positive integer.")
        return 2

    tmp = None
    db_path = args.db
    if db_path is None:
        tmp = tempfile.TemporaryDirectory(prefix="persona-harness-")
        db_path = Path(tmp.name) / "harness.db"
    elif db_path.resolve() == config.db_path().resolve() and not args.allow_real_db:
        print(
            f"Refusing to run against the real store at {db_path}.\n"
            "The harness creates persona users and writes judgment rows; it "
            "should not do that in your live database.\n"
            "Pass --allow-real-db if you genuinely mean to."
        )
        return 2

    if args.live and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        # Checked before the run row is allocated, so a missing key does not
        # leave a started-but-never-finished eval_run behind.
        print(
            "--live needs Anthropic credentials, and none were found.\n"
            "Set ANTHROPIC_API_KEY, or drop --live to run the offline "
            "plumbing suite (which needs no key and makes no network calls)."
        )
        return 2

    persona_ids = [p.id for p in loaded]
    run_settings = build_run_settings(
        fixture_dir=args.fixture_dir or personas.FIXTURE_DIR,
        persona_ids=persona_ids,
        rounds_cap=args.rounds,
        horizon_days=args.horizon_days,
        retention_probes=args.retention_probes,
        routing=routing,
        ledger_truth=args.ledger_truth,
        materiality_cache=args.materiality_cache is not None,
    )

    cache: MaterialityCache | None = None
    if args.materiality_cache is not None:
        cache = MaterialityCache(
            args.materiality_cache,
            prompt_version=metrics.prompt_versions().get(config.MATERIALITY),
        )

    # The run row is allocated on a short-lived connection so that `run_id`
    # exists before `app.build` is called with it.
    bootstrap = Store(db_path)
    run_id = bootstrap.start_eval_run(
        "personas", run_config_json(args.live, persona_ids, run_settings)
    )
    bootstrap.close()

    harness = build_harness(
        db_path=db_path,
        run_id=run_id,
        personas=loaded,
        live=args.live,
        remediation_search=routing.remediation_search,
        routing=routing,
        materiality_cache=cache,
    )

    workers = min(args.parallel, len(loaded))
    try:
        with installed(harness.clock):
            if workers > 1:
                # The personas are replayed on their own systems in worker
                # threads; `harness` is kept for the shared store, the judge the
                # interrupt cases need, and cleanup.
                outcome = run_all_parallel(
                    db_path=db_path,
                    run_id=run_id,
                    personas=loaded,
                    live=args.live,
                    remediation_search=routing.remediation_search,
                    max_rounds=args.rounds,
                    workers=workers,
                    routing=routing,
                    materiality_cache=cache,
                    horizon_days=args.horizon_days,
                    retention_probes=args.retention_probes,
                )
                results = outcome.results
                unexpected = list(outcome.unexpected_calls)
                notes = list(outcome.contract_notes)
                usage = dict(outcome.harness_usage)
            else:
                results = run_all(
                    harness,
                    loaded,
                    max_rounds=args.rounds,
                    horizon_days=args.horizon_days,
                    retention_probes=args.retention_probes,
                )
                unexpected = (
                    list(harness.registry.unexpected_calls) if harness.registry else []
                )
                notes = (
                    list(harness.registry.contract_notes) if harness.registry else []
                ) + list(harness.contract.notes)
                usage = merge_usage({}, harness.harness_usage())
            interrupt_report = (
                {}
                if args.skip_interrupt_cases
                else interrupt_cases.run_cases(harness.system.judge, live=args.live)
            )

        report = metrics.compute(
            results,
            loaded,
            store=harness.store,
            run_id=run_id,
            live=args.live,
            unexpected_calls=unexpected,
            contract_notes=notes,
            interrupt=interrupt_report,
            contract=harness.contract,
            ledger_truth=args.ledger_truth,
            run_settings=run_settings,
            harness_usage=usage,
        )
        payload = metrics.to_dict(report, results)
        payload["volatile"]["parallel"] = int(args.parallel)
        if cache is not None:
            cache.save()
            payload["volatile"]["materiality_cache"] = cache.stats()
        harness.store.finish_eval_run(
            run_id, json.dumps(payload, sort_keys=True, default=str), report.passed
        )
    finally:
        harness.close()
        if tmp is not None:
            tmp.cleanup()

    print(metrics.render(report, results))
    if cache is not None:
        stats = cache.stats()
        rate = "n/a" if stats["hit_rate"] is None else f"{stats['hit_rate']:.0%}"
        print(
            f"\nMateriality cache {stats['path']}: {stats['hits']} hit(s), "
            f"{stats['misses']} miss(es), hit rate {rate}; {stats['written']} verdict(s) "
            f"written, {stats['entries']} in file."
        )

    if args.show_thread:
        target, _, round_str = args.show_thread.partition(":")
        wanted = int(round_str) if round_str else None
        match = next((r for r in results if r.persona_id == target), None)
        if match is None:
            print(f"\nNo persona {target!r} in this run.")
        else:
            print()
            print(metrics.render_thread(match, wanted))

    if not args.no_artifact:
        artifact = _artifact_path(args.json)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"\nArtifact: {artifact}")
        # Harness-side sidecar: the probe's prompts, the persona's answers and
        # the grader's reasons. Deliberately NOT in the artifact (which is
        # also stored in the system's eval_runs table) -- this file lives
        # next to it on disk only, for human read-through of a live run.
        sidecar = artifact.with_suffix(".probe_answers.jsonl")
        with sidecar.open("w", encoding="utf-8") as fh:
            for r in results:
                for session in getattr(r, "probes", []) or []:
                    for item in session.items:
                        fh.write(json.dumps({
                            "persona_id": r.persona_id,
                            "round": session.round_index,
                            "term": item.term,
                            "kind": item.kind,
                            "is_pseudo": item.is_pseudo,
                            "sampled_rung": item.sampled_rung,
                            "graded_rung": item.graded_rung,
                            "confabulated": item.confabulated,
                            "days_since_last_exposure": item.days_since_last_exposure,
                            "answer": item.answer,
                            "grader_reason": item.grader_reason,
                        }, ensure_ascii=False) + "\n")
        print(f"Probe answers (harness-only): {sidecar}")
        print(
            "Reproducibility: compare two artifacts with "
            "`python -m harness.run --compare A B` (ignores the 'volatile' subtree)."
        )

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
