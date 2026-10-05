"""Hand-written cases for the interrupt-timing judgment only.

Why this file exists at all. Every persona has exactly one group, which was
the right call for the persona fixtures -- it keeps a persona's convergence
attributable to its own answers. But it leaves interrupt timing barely
exercised: with one group there is nothing to prioritise *between*, and prioritising
across several groups competing for the same moment of attention is the whole
job. The persona suite would happily report interrupt timing as "exercised, no
errors" while never once asking it the question it exists to answer.

So: five synthetic multi-group contexts, each with an authored expectation
about what a defensible decision looks like.

The assertions are split in two, and the split matters:

*   **Structural** invariants hold for *any* valid verdict regardless of how
    good the reasoning is -- surfaced ids must come from the pending list, you
    cannot raise a topic for a group that needs no attention, held-back counts
    cannot exceed what was queued. A violation is a bug in the plumbing or the
    schema, not a bad judgment call. These run in both modes.

*   **Judgment** expectations are the actual content -- the deadline group must
    win, six queued items must not all be dumped at once. Offline these are
    NOT evaluated: the offline stub is a flat materiality threshold that
    ignores deadlines, dormancy and recency entirely, so scoring it would
    measure a heuristic written twenty lines away in this same repository.
    They are evaluated under `--live` only, and that is the point of them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conversational_agent import config, judgments
from conversational_agent.judgment import Judge, JudgmentError, StubClient

from .stub_judgments import interrupt_verdict

CASE_USER = "usr_interrupt_cases"


@dataclass
class Case:
    id: str
    what_it_probes: str
    groups: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    due: list[dict[str, Any]]
    hours_since_last_session: float | None
    recently_surfaced: list[dict[str, Any]]
    # Returns a list of failure strings; empty means the expectation held.
    expectation: Callable[[dict[str, Any], Case], list[str]]
    expectation_text: str = ""


def _event(
    event_id: str,
    group_id: str,
    group_name: str,
    headline: str,
    score: float,
    occurred_at: str,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "group_id": group_id,
        "group_name": group_name,
        "headline": headline,
        "detail": headline,
        "occurred_at": occurred_at,
        "materiality_score": score,
    }


def _group(
    group_id: str,
    name: str,
    proficiency: str = "developing",
    goal: dict[str, Any] | None = None,
    concepts_known: int = 0,
    concepts_seen: int = 0,
    unseen_events: int = 0,
) -> dict[str, Any]:
    """One group as the Orchestrator describes it to the interrupt-timing call.

    The proficiency band is coarse and derived; the two concept counts and the
    unseen-event count carry the detail -- which matters here because these
    cases exist to check that the interrupt decision is made on the right
    grounds.
    """
    return {
        "id": group_id,
        "name": name,
        "proficiency": proficiency,
        "concepts_known": concepts_known,
        "concepts_seen": concepts_seen,
        "unseen_events": unseen_events,
        "active_goal": goal,
    }


# --- Expectations ----------------------------------------------------------


def _expect_deadline_group_surfaced(verdict: dict[str, Any], case: Case) -> list[str]:
    ids = set(verdict.get("event_ids", []))
    must = {e["id"] for e in case.pending if e["group_id"] == "grp_deadline"}
    if not verdict.get("should_surface"):
        return ["nothing surfaced, but a goal deadline is two days out"]
    if not (ids & must):
        return [
            "the event in the group with a deadline in 2 days was not surfaced "
            f"(surfaced {sorted(ids)})"
        ]
    return []


def _expect_not_everything(verdict: dict[str, Any], case: Case) -> list[str]:
    ids = verdict.get("event_ids", [])
    if len(ids) >= len(case.pending):
        return [
            f"surfaced all {len(ids)} queued items at once; the point of the "
            "judgment is that recording and interrupting are different bars"
        ]
    if not ids:
        return ["surfaced nothing at all from six material items"]
    if int(verdict.get("held_back_count", 0)) == 0:
        return ["held_back_count is 0 while items were withheld"]
    return []


def _expect_quiet(verdict: dict[str, Any], case: Case) -> list[str]:
    failures = []
    if verdict.get("should_surface"):
        failures.append(
            "should_surface is true, but every pending item was already shown "
            "minutes ago"
        )
    if verdict.get("event_ids"):
        failures.append(f"re-surfaced already-seen events {verdict['event_ids']}")
    return failures


def _expect_active_over_dormant(verdict: dict[str, Any], case: Case) -> list[str]:
    ids = set(verdict.get("event_ids", []))
    failures = []
    if "evt_active" not in ids:
        failures.append("the high-materiality event in the active group was not surfaced")
    if "evt_dormant" in ids:
        failures.append(
            "surfaced a low-materiality event from a dormant group the user has "
            "not engaged with"
        )
    return failures


def _expect_silent(verdict: dict[str, Any], case: Case) -> list[str]:
    failures = []
    if verdict.get("should_surface"):
        failures.append("should_surface is true with nothing pending")
    if verdict.get("raise_topic"):
        failures.append(
            "raise_topic is true with nothing untold -- an overdue group alone "
            "must not produce a rerun of an old story"
        )
    if verdict.get("event_ids"):
        failures.append(
            f"surfaced events {verdict['event_ids']} when nothing was pending"
        )
    return failures


# --- The cases -------------------------------------------------------------


def build_cases() -> list[Case]:
    return [
        Case(
            id="deadline_wins",
            what_it_probes=(
                "a near goal deadline outranks a higher raw materiality score "
                "in a group with no deadline"
            ),
            groups=[
                _group(
                    "grp_deadline",
                    "Client's F1 team",
                    "developing",
                    goal={
                        "description": "Dinner with the client who sponsors the team",
                        "deadline": "2026-01-07T19:00:00+00:00",
                    },
                ),
                _group("grp_idle", "Premier League", "conversant"),
                _group("grp_quiet", "Formula E", "developing"),
            ],
            pending=[
                _event(
                    "evt_deadline",
                    "grp_deadline",
                    "Client's F1 team",
                    "Team principal resigns two days before the season opener",
                    68.0,
                    "2026-01-05T08:00:00+00:00",
                ),
                _event(
                    "evt_idle",
                    "grp_idle",
                    "Premier League",
                    "Record transfer fee agreed for a striker",
                    88.0,
                    "2026-01-05T07:00:00+00:00",
                ),
            ],
            due=[],
            hours_since_last_session=30.0,
            recently_surfaced=[],
            expectation=_expect_deadline_group_surfaced,
            expectation_text=(
                "the event in the group with a deadline in 2 days must be surfaced"
            ),
        ),
        Case(
            id="volume_restraint",
            what_it_probes="six queued items must not all be dumped in one session",
            groups=[_group("grp_ai", "AI research", "conversant")],
            pending=[
                _event(
                    f"evt_bulk_{i}",
                    "grp_ai",
                    "AI research",
                    headline,
                    score,
                    f"2026-01-0{4 + (i // 4)}T0{i % 8}:00:00+00:00",
                )
                for i, (headline, score) in enumerate(
                    [
                        ("Lab A ships a frontier model", 84.0),
                        ("Lab B publishes a scaling result", 72.0),
                        ("Regulator opens a consultation", 66.0),
                        ("Benchmark suite revised upward", 61.0),
                        ("Open-weights release from Lab C", 79.0),
                        ("Chip supplier raises guidance", 64.0),
                    ]
                )
            ],
            due=[],
            hours_since_last_session=72.0,
            recently_surfaced=[],
            expectation=_expect_not_everything,
            expectation_text="with 6 queued items it must not surface all 6",
        ),
        Case(
            id="just_shown",
            what_it_probes="nothing new since a session minutes ago",
            groups=[_group("grp_nba", "NBA Western Conference", "conversant")],
            pending=[
                _event(
                    "evt_seen",
                    "grp_nba",
                    "NBA Western Conference",
                    "Trade deadline passes with no moves",
                    58.0,
                    "2026-01-05T06:00:00+00:00",
                )
            ],
            due=[],
            hours_since_last_session=0.08,
            recently_surfaced=[
                {
                    "headline": "Trade deadline passes with no moves",
                    "surfaced_at": "2026-01-05T08:55:00+00:00",
                },
                {
                    "headline": "Star guard cleared to return",
                    "surfaced_at": "2026-01-05T08:55:00+00:00",
                },
            ],
            expectation=_expect_quiet,
            expectation_text=(
                "should_surface must be false when everything was just shown"
            ),
        ),
        Case(
            id="active_over_dormant",
            what_it_probes=(
                "a dormant group's marginal update must not ride along with an "
                "active group's real one"
            ),
            groups=[
                _group("grp_active", "Semiconductor supply chain", "conversant"),
                _group("grp_dormant", "Competitive bread baking", "beginner"),
            ],
            pending=[
                _event(
                    "evt_active",
                    "grp_active",
                    "Semiconductor supply chain",
                    "Largest foundry cuts capex guidance by a third",
                    91.0,
                    "2026-01-05T05:00:00+00:00",
                ),
                _event(
                    "evt_dormant",
                    "grp_dormant",
                    "Competitive bread baking",
                    "Regional qualifier reschedules to a Sunday",
                    34.0,
                    "2026-01-05T04:00:00+00:00",
                ),
            ],
            due=[],
            hours_since_last_session=48.0,
            recently_surfaced=[],
            expectation=_expect_active_over_dormant,
            expectation_text=(
                "the active group's high-materiality event is surfaced and the "
                "dormant group's marginal one is not"
            ),
        ),
        Case(
            id="topic_without_events",
            what_it_probes="an overdue group with nothing new is NOT a reason to speak",
            groups=[_group("grp_wine", "Natural wine importers", "developing")],
            pending=[],
            due=[
                {
                    "group_id": "grp_wine",
                    "group_name": "Natural wine importers",
                    "urgency": "high",
                    "topics": ["carbonic maceration", "importer portfolios"],
                    "reason": "Level 38 is well under the conversant threshold and "
                    "nothing has been checked in nine days.",
                }
            ],
            hours_since_last_session=216.0,
            recently_surfaced=[],
            expectation=_expect_silent,
            expectation_text=(
                "with nothing queued there is nothing new to say: should_surface "
                "false, raise_topic false, no events invented (human rule, "
                "checkpoint 4 -- never repeat a story; say nothing when there is "
                "nothing new)"
            ),
        ),
    ]


# --- Structural invariants -------------------------------------------------


def structural_failures(verdict: dict[str, Any], case: Case) -> list[str]:
    """Invariants any valid verdict satisfies, however it reasons."""
    failures: list[str] = []
    pending_ids = [e["id"] for e in case.pending]
    ids = list(verdict.get("event_ids", []))

    unknown = [i for i in ids if i not in pending_ids]
    if unknown:
        failures.append(f"surfaced event ids not in the pending queue: {unknown}")
    if len(set(ids)) != len(ids):
        failures.append(f"duplicate event ids in the verdict: {ids}")
    if verdict.get("should_surface") and not ids:
        failures.append("should_surface is true but no event ids were chosen")
    if not verdict.get("should_surface") and ids:
        failures.append("should_surface is false but event ids were chosen")
    held = verdict.get("held_back_count", 0)
    if not isinstance(held, int) or held < 0 or held > len(pending_ids):
        failures.append(
            f"held_back_count {held!r} outside 0..{len(pending_ids)} pending items"
        )
    if verdict.get("raise_topic") and not case.due:
        failures.append("raise_topic is true but no group was needing attention")
    if not str(verdict.get("framing", "")).strip():
        failures.append("framing is empty; the user would be shown nothing")
    return failures


# --- Runner ----------------------------------------------------------------


@dataclass
class CaseOutcome:
    case_id: str
    verdict: dict[str, Any] | None
    error: str | None
    structural: list[str]
    judgment: list[str] | None  # None offline: deliberately not evaluated
    surfaced: list[str] = field(default_factory=list)


def run_cases(judge: Judge, *, live: bool) -> dict[str, Any]:
    """Run every case through the real interrupt-timing call path."""
    outcomes: list[CaseOutcome] = []
    for case in build_cases():
        verdict_data: dict[str, Any] | None = None
        error: str | None = None
        try:
            verdict = judgments.judge_interrupt_timing(
                judge,
                groups_summary=case.groups,
                pending_events=case.pending,
                groups_needing_attention=case.due,
                hours_since_last_session=case.hours_since_last_session,
                recently_surfaced=case.recently_surfaced,
                user_id=CASE_USER,
            )
            verdict_data = dict(verdict.data)
        except JudgmentError as exc:
            error = str(exc)

        if verdict_data is None:
            outcomes.append(
                CaseOutcome(
                    case_id=case.id,
                    verdict=None,
                    error=error,
                    structural=[f"judgment call failed: {error}"],
                    judgment=None,
                )
            )
            continue

        outcomes.append(
            CaseOutcome(
                case_id=case.id,
                verdict=verdict_data,
                error=None,
                structural=structural_failures(verdict_data, case),
                # Offline the expectation is not evaluated at all -- see the
                # module docstring. Not "evaluated and ignored": not run.
                judgment=(case.expectation(verdict_data, case) if live else None),
                surfaced=sorted(verdict_data.get("event_ids", [])),
            )
        )

    return _summarise(outcomes, live=live)


def _summarise(outcomes: list[CaseOutcome], *, live: bool) -> dict[str, Any]:
    structural_bad = [
        f"{o.case_id}: {failure}" for o in outcomes for failure in o.structural
    ]
    lines: list[str] = []
    cases = {c.id: c for c in build_cases()}
    for outcome in outcomes:
        case = cases[outcome.case_id]
        status = "ok" if not outcome.structural else "STRUCTURAL FAIL"
        if live and outcome.judgment is not None:
            status = "PASS" if not outcome.judgment else "FAIL"
        lines.append(f"[{status:>15}] {outcome.case_id} -- {case.expectation_text}")
        for failure in outcome.structural:
            lines.append(f"                  ! {failure}")
        for failure in outcome.judgment or []:
            lines.append(f"                  ! {failure}")

    if live:
        judged = [o for o in outcomes if o.judgment is not None]
        passed = sum(1 for o in judged if not o.judgment and not o.structural)
        summary = f"{passed}/{len(judged)} cases met their expectation"
    else:
        summary = (
            f"{len(outcomes)} cases ran; structural invariants "
            f"{'OK' if not structural_bad else 'VIOLATED'}. "
            "Expectations not evaluated offline (the stub ignores deadlines, "
            "dormancy and recency by construction) -- use --live."
        )

    return {
        "mode": "live" if live else "offline",
        "summary": summary,
        "lines": lines,
        "structural_failures": structural_bad,
        "judgment_failures": (
            sorted(
                f"{o.case_id}: {failure}"
                for o in outcomes
                for failure in (o.judgment or [])
            )
            if live
            else "not measured offline (the offline stub ignores the case's premise)"
        ),
        "cases": [
            {
                "case_id": o.case_id,
                "probes": cases[o.case_id].what_it_probes,
                "expectation": cases[o.case_id].expectation_text,
                "surfaced": o.surfaced,
                "should_surface": (o.verdict or {}).get("should_surface"),
                "raise_topic": (o.verdict or {}).get("raise_topic"),
                "held_back_count": (o.verdict or {}).get("held_back_count"),
                "structural_failures": o.structural,
                **({"judgment_failures": o.judgment} if live else {}),
            }
            for o in outcomes
        ],
    }


def _standalone_judge(db_path: Path, live: bool, run_id: str | None) -> tuple[Judge, Any]:
    from conversational_agent.store import Store

    store = Store(db_path)
    if live:
        from conversational_agent import app
        from conversational_agent.judgment import AnthropicClient

        client = AnthropicClient(app._anthropic_raw_client())
    else:
        client = StubClient()
        client.register(config.INTERRUPT_TIMING, interrupt_verdict)
    return Judge(client=client, store=store, run_id=run_id), store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Interrupt-timing case suite."
    )
    parser.add_argument("--live", action="store_true", help="score against real models")
    parser.add_argument("--db", type=Path, default=None, help="scratch DB path")
    parser.add_argument("--json", type=Path, default=None, help="write JSON artifact")
    args = parser.parse_args(argv)

    import tempfile

    tmp = None
    db_path = args.db
    if db_path is None:
        tmp = tempfile.TemporaryDirectory(prefix="harness-interrupt-")
        db_path = Path(tmp.name) / "interrupt_cases.db"

    judge, store = _standalone_judge(db_path, args.live, run_id=None)
    try:
        report = run_cases(judge, live=args.live)
    finally:
        store.close()
        if tmp is not None:
            tmp.cleanup()

    print(f"Interrupt-timing cases ({report['mode']})")
    print(report["summary"])
    for line in report["lines"]:
        print(line)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )

    failed = bool(report["structural_failures"]) or (
        args.live and bool(report["judgment_failures"])
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
