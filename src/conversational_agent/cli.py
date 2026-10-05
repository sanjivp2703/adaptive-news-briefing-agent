"""The v1 interaction surface: an argparse CLI over the whole system.

Two audiences share one binary, which is why the command set looks wider than
a normal CLI:

  * the *user* surface (init/user/group/goal/session/poll) -- the pull-only
    delivery the spec mandates, plus the group/goal lifecycle that gives the
    rest of the system something to operate on. Nothing on this surface ever
    prints a grade, a score, a level or a proficiency band: the product is a
    well-informed friend telling you what happened, not a test;
  * the *operator* surface (ledger/models/prompts/traces/trace/stats/runs) -- the
    LLMOps console, because the human wants to see what each judgment point
    actually did, on which model, at what cost, under which prompt version,
    and to change routing via env override without editing code.

Two rules shape the implementation:

  * Every read or write of a user's own data goes through `store.scope(uid)`.
    There is no path here that names two users at once.
  * The operator commands make zero LLM calls and never need credentials, so
    a failed live run can always be diagnosed afterwards.

Stdlib only -- no click/rich/typer -- so the CLI adds no dependency to a
project whose only third-party import is the Anthropic SDK.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config, judgment, reporting
from .models import Group, User
from .store import Store


class CliError(Exception):
    """A user-facing failure. Printed as one line, never as a traceback."""


# --- Small formatting helpers ---------------------------------------------


def _out(line: str = "") -> None:
    print(line)


def _trunc(value: Any, width: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def _ts(stamp: str | None, width: str = "short") -> str:
    """ISO timestamp -> something that fits in a column."""
    if not stamp:
        return "-"
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp[:19]
    if width == "short":
        return moment.strftime("%m-%d %H:%M")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], indent: str = "") -> None:
    """Left-aligned fixed-width table. Columns sized to their content."""
    cells = [[("-" if c is None else str(c)) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    _out(indent + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
    _out(indent + "  ".join("-" * w for w in widths))
    for row in cells:
        _out(indent + "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())


def _rule(title: str) -> None:
    _out()
    _out(f"--- {title} " + "-" * max(0, 60 - len(title)))


def _pct(part: float, whole: float) -> str:
    return "0.0%" if not whole else f"{100.0 * part / whole:.1f}%"


# --- Context: DB, user resolution -----------------------------------------


def _db_path(args: argparse.Namespace) -> Path | None:
    override = getattr(args, "db", None)
    return Path(override) if override else None


def _open_store(args: argparse.Namespace) -> Store:
    return Store(_db_path(args))


def _resolve_user(store: Store, requested: str | None) -> User:
    """Active user: --user, else $CONVAGENT_USER, else the sole real user.

    Everything downstream takes a UserScope built from this id, so getting it
    wrong is the only way to touch the wrong user's data -- hence the explicit
    error rather than a guess when it is ambiguous.
    """
    name = requested or os.environ.get("CONVAGENT_USER")
    if name:
        user = store.get_user(name) or store.find_user_by_name(name)
        if user is None:
            raise CliError(
                f"No user {name!r}. Run `user list` to see who exists, "
                f"or `user add {name!r}` to create them."
            )
        return user

    real = store.list_users(kind="real")
    if len(real) == 1:
        return real[0]
    if not real:
        raise CliError(
            "No real user exists yet. Run `init --name \"Your Name\"` "
            "(or `user add \"Your Name\"`) first."
        )
    names = ", ".join(u.display_name for u in real)
    raise CliError(
        f"{len(real)} real users exist ({names}); which one? "
        "Pass --user <name-or-id> or set CONVAGENT_USER."
    )


def _resolve_user_optional(store: Store, requested: str | None) -> User | None:
    """User resolution for the operator commands, which must never hard-fail.

    A trace view on a fresh or multi-user DB should still print something
    useful, so ambiguity degrades to "system-level rows only" rather than an
    error the human has to clear before they can debug.
    """
    try:
        return _resolve_user(store, requested)
    except CliError:
        if requested:
            raise
        return None


def _resolve_group(scope, token: str) -> Group:
    group = scope.find_group_by_name(token) or scope.get_group(token)
    if group is None:
        raise CliError(
            f"No group {token!r} for this user. Run `group list` to see yours."
        )
    return group


def _live_system(args: argparse.Namespace, *, run_id: str | None = None):
    """Build the full system, turning a missing key into one clear line."""
    from .app import MissingCredentials, build

    try:
        return build(db_path=_db_path(args), run_id=run_id)
    except MissingCredentials:
        raise CliError(
            "This command makes live Anthropic calls and no credentials were "
            "found. Set ANTHROPIC_API_KEY in your environment. Only `session` "
            "and `poll` need it — init, user, group (including `group add`), "
            "ledger, goal, models, prompts, traces, trace, stats and runs all "
            "work without it."
        ) from None


def _parse_deadline(raw: str) -> str:
    try:
        return reporting.parse_deadline(raw)
    except ValueError:
        raise CliError(
            f"Could not read deadline {raw!r}. Use YYYY-MM-DD or a full ISO "
            "timestamp like 2026-09-11T19:00:00Z."
        ) from None


# --- Ledger helpers --------------------------------------------------------
#
# Everything below reads the concept ledger. Nothing here derives, formats or
# prints a score: the ledger is a set of observed facts about terms, and the
# proficiency band is a coarse read-time derivation shown only on the
# builder-facing surfaces (`group show`, `ledger`), never in a session.

_STATE_ORDER = config.STATES_STRONGEST_FIRST
_STATE_GLOSS = config.STATE_GLOSS


def _state_counts(ledger) -> dict[str, int]:
    counts = {state: 0 for state in _STATE_ORDER}
    for concept in ledger:
        counts[concept.state] = counts.get(concept.state, 0) + 1
    return counts


def _known_total(ledger) -> tuple[int, int]:
    return sum(1 for c in ledger if c.is_known), len(ledger)


_last_engaged = reporting.last_engaged


# --- Commands: identity ----------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    target = _db_path(args) or config.db_path()
    store = _open_store(args)  # opening the store creates the schema
    try:
        _out(f"Store ready at {target}")
        if args.name:
            existing = store.find_user_by_name(args.name)
            if existing:
                _out(f"User already exists: {existing.display_name} ({existing.id})")
            else:
                user = store.create_user(args.name, kind="real")
                _out(f"Created real user: {user.display_name} ({user.id})")
        else:
            _out("No --name given; add a user with `user add \"Your Name\"`.")
        _out("Override the location any time with CONVAGENT_DB=/path/to.db")
    finally:
        store.close()
    return 0


def cmd_user_list(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        users = store.list_users()
        if not users:
            _out("No users yet. Create one with `user add \"Your Name\"`.")
            return 0
        rows = []
        for user in users:
            scope = store.scope(user.id)
            rows.append(
                [user.display_name, user.kind, user.id, len(scope.list_groups())]
            )
        _table(["NAME", "KIND", "ID", "GROUPS"], rows)
    finally:
        store.close()
    return 0


def cmd_user_add(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        if store.find_user_by_name(args.name):
            raise CliError(f"A user named {args.name!r} already exists.")
        user = store.create_user(args.name, kind="real")
        _out(f"Created real user: {user.display_name} ({user.id})")
    finally:
        store.close()
    return 0


# --- Commands: groups ------------------------------------------------------


def cmd_group_add(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        user = _resolve_user(store, getattr(args, "user", None))
        scope = store.scope(user.id)
        if scope.find_group_by_name(args.name):
            raise CliError(
                f"You already have a group named {args.name!r}. "
                "Group names are unique per user."
            )

        # A group is now just name/description/poll interval. There is no
        # decay rate to guess, so creating one makes no LLM call and needs no
        # credentials -- and the ledger starts empty, which is the honest
        # cold-start state rather than a seeded number.
        group = scope.create_group(
            name=args.name,
            description=args.description,
            poll_interval_minutes=args.poll_interval,
        )
        _out(
            f"Created group {group.name!r} ({group.id}) for {user.display_name}: "
            f"polling every {group.poll_interval_minutes} min."
        )
        _out(
            "Nothing is assumed about what you already know — the first briefs "
            "explain terms inline."
        )
    finally:
        store.close()
    return 0


def cmd_group_list(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        user = _resolve_user(store, getattr(args, "user", None))
        scope = store.scope(user.id)
        groups = scope.list_groups()
        if not groups:
            _out(
                f"No groups yet for {user.display_name}. Create one with "
                "`group add \"NBA fans\"`."
            )
            return 0
        rows = []
        for group in groups:
            goal = scope.active_goal(group.id)
            rows.append(
                [
                    _trunc(group.name, 28),
                    scope.behind_count(group.id),
                    _ts(_last_engaged(scope, group.id)),
                    _trunc(goal.description, 34) if goal else "-",
                    _ts(goal.deadline) if goal else "-",
                    _ts(group.last_polled_at),
                ]
            )
        _out(f"Groups for {user.display_name} ({user.id})")
        _out()
        # NEW replaces the old LEVEL column, and is deliberately not called
        # "behind". It counts what we have not told you yet -- a fact about our
        # queue. "Behind" would assert something about what you know, which
        # delivery data cannot support: being shown a thing is not the same as
        # having taken it in.
        _table(
            ["GROUP", "NEW", "LAST ENGAGED", "ACTIVE GOAL", "DEADLINE", "LAST POLL"],
            rows,
        )
        _out()
        _out(
            "NEW = things that happened in this group that we haven't shown you "
            "yet. LAST ENGAGED = the last time you replied to something."
        )
    finally:
        store.close()
    return 0


def cmd_group_show(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        user = _resolve_user(store, getattr(args, "user", None))
        scope = store.scope(user.id)
        group = _resolve_group(scope, args.name)
        ledger = scope.ledger(group.id)
        counts = _state_counts(ledger)
        known, total = _known_total(ledger)

        _out(f"{group.name}   ({group.id})")
        _out(f"  user            {user.display_name} ({user.id})")
        if group.description:
            _out(f"  description     {group.description}")
        _out(f"  created         {_ts(group.created_at, 'long')}")
        _out(f"  poll interval   {group.poll_interval_minutes} min")
        _out(f"  last polled     {_ts(group.last_polled_at, 'long')}")
        _out(f"  last engaged    {_ts(_last_engaged(scope, group.id), 'long')}")
        _out(
            f"  new             {scope.behind_count(group.id)} thing(s) that "
            "happened and haven't been shown to you yet"
        )
        # Diagnostic only. This band is derived from the ledger on read, is
        # never stored, and is never shown in a session -- it exists so the
        # builder can see which prior the Assessor is working from.
        _out(
            f"  proficiency     {scope.proficiency(group.id)}  "
            f"(derived from {known}/{total} concept(s) known; internal, never "
            "shown to the user)"
        )

        goal = scope.active_goal(group.id)
        _rule("Active goal")
        if goal:
            _out(f"  {goal.description}")
            _out(f"  deadline {_ts(goal.deadline, 'long')}   id {goal.id}")
        else:
            _out("  none — the group is in maintenance mode.")

        _rule("Concept ledger")
        if ledger:
            for state in _STATE_ORDER:
                terms = [c.term for c in ledger if c.state == state]
                if not terms:
                    continue
                _out(f"  {state} ({len(terms)}) — {_STATE_GLOSS[state]}")
                _out(f"    {_trunc(', '.join(sorted(terms)), 200)}")
            _out()
            _out(
                "  " + "  ".join(f"{s}={counts[s]}" for s in _STATE_ORDER)
                + f"  total={total}"
            )
            _out(f"  Full detail, with evidence: `ledger \"{group.name}\"`.")
        else:
            _out(
                "  empty — nothing has been raised with this group yet. That is "
                "the correct cold start, not a gap."
            )

        _rule("Recent events")
        events = scope.recent_events(group.id, limit=10)
        if events:
            _table(
                ["WHEN", "MATERIAL", "SURFACED", "HEADLINE"],
                [
                    [
                        _ts(e.occurred_at),
                        "-" if e.is_material is None else ("yes" if e.is_material else "no"),
                        "yes" if e.surfaced_at else "no",
                        _trunc(e.headline, 58),
                    ]
                    for e in events
                ],
                indent="  ",
            )
        else:
            _out("  none yet — run `poll --once` (or wait for the poller).")

        _rule("Recent exchanges")
        history = scope.exchange_history(group.id, limit=5)
        if history:
            for exchange in history:
                _out(f"  {_ts(exchange.raised_at, 'long')}  [{exchange.topic or 'general'}]")
                if exchange.briefing:
                    _out(f"    briefed: {_trunc(exchange.briefing, 90)}")
                turns = scope.thread_turns(exchange.id)
                user_turns = [t for t in turns if t.is_user]
                if exchange.read_quality:
                    _out(f"    read:    {exchange.read_quality}")
                if not user_turns:
                    _out("    (said nothing)")
                    continue
                _out(f"    asked:   {_trunc(user_turns[0].text, 90)}"
                     + (f"  (+{len(user_turns) - 1} more)" if len(user_turns) > 1 else ""))
                # What the reply revealed, as terms -- not a grade. There is
                # deliberately no score to print here.
                revealed = []
                if exchange.understood:
                    revealed.append(f"understood {', '.join(exchange.understood)}")
                if exchange.asked_about:
                    revealed.append(f"asked about {', '.join(exchange.asked_about)}")
                if exchange.not_understood:
                    revealed.append(f"missed {', '.join(exchange.not_understood)}")
                if exchange.already_knew:
                    revealed.append("had already seen it")
                if revealed:
                    _out(f"    ledger:  {_trunc('; '.join(revealed), 90)}")
        else:
            _out("  none yet — `session` raises a topic and records the reply.")
    finally:
        store.close()
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    """The builder-facing view of what the system believes, and why.

    This is the thing to open when the product behaves oddly: every term, the
    state it is in, how much weak evidence backs it, and the evidence string
    that put it there. It is not a report card and is not shown to the user in
    a session -- it is the audit trail behind the Assessor's choices.
    """
    store = _open_store(args)
    try:
        user = _resolve_user(store, getattr(args, "user", None))
        scope = store.scope(user.id)
        group = _resolve_group(scope, args.group)
        full_ledger = scope.ledger(group.id)
        ledger = full_ledger

        if args.state:
            if args.state not in config.CONCEPT_STATES:
                raise CliError(
                    f"Unknown state {args.state!r}. "
                    f"Known: {', '.join(config.CONCEPT_STATES)}"
                )
            ledger = [c for c in ledger if c.state == args.state]

        _out(f"Concept ledger — {group.name} ({group.id}) · {user.display_name}")
        _out()
        if not ledger:
            _out(
                "  empty."
                if not args.state
                else f"  no concepts in state {args.state!r}."
            )
            _out(
                "  A new group starts with an empty ledger by design: the system "
                "assumes nothing and explains terms inline."
            )
            return 0

        _table(
            ["TERM", "STATE", "SUBDOMAIN", "SEEN", "FIRST", "LAST", "EVIDENCE"],
            [
                [
                    _trunc(c.term, 30),
                    c.state,
                    _trunc(c.subdomain or "", 22),
                    c.exposure_count,
                    _ts(c.first_seen_at),
                    _ts(c.last_seen_at),
                    _trunc(c.evidence, 52),
                ]
                for c in sorted(
                    ledger,
                    key=lambda c: (_STATE_ORDER.index(c.state), c.term),
                )
            ],
        )
        counts = _state_counts(full_ledger)
        known, total = _known_total(full_ledger)
        _out()
        _out("  ".join(f"{state}={counts[state]}" for state in _STATE_ORDER))
        _out(
            f"{known} of {total} concept(s) count as known "
            f"(states: {', '.join(config.KNOWN_STATES)})."
        )
        _out(
            f"Derived proficiency band: {scope.proficiency(group.id)} — computed "
            "on read from those counts, never stored and never shown to the user."
        )
        _out(
            "SEEN = times the term was used in front of them unquestioned. "
            "Exposure is counted but never promotes a term: measured against "
            "ground truth, silence was anti-predictive."
        )
    finally:
        store.close()
    return 0


# --- Commands: goals -------------------------------------------------------


def cmd_goal_add(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        user = _resolve_user(store, getattr(args, "user", None))
        scope = store.scope(user.id)
        group = _resolve_group(scope, args.group)
        deadline = _parse_deadline(args.deadline)
        if datetime.fromisoformat(deadline) <= datetime.now(UTC):
            raise CliError(
                f"That deadline ({_ts(deadline, 'long')}) is already in the past."
            )
        existing = scope.active_goal(group.id)
        if existing:
            raise CliError(
                f"{group.name!r} already has an active goal: "
                f"{existing.description!r} (due {_ts(existing.deadline, 'long')}, "
                f"id {existing.id}). Close it first: `goal close {existing.id}`."
            )
        try:
            goal = scope.create_goal(group.id, args.description, deadline)
        except ValueError as exc:
            raise CliError(str(exc)) from None
        _out(
            f"Added goal to {group.name!r}: {goal.description!r} "
            f"due {_ts(goal.deadline, 'long')} (id {goal.id})."
        )
    finally:
        store.close()
    return 0


def cmd_goal_list(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        user = _resolve_user(store, getattr(args, "user", None))
        scope = store.scope(user.id)
        group_id = None
        label = f"all groups for {user.display_name}"
        if args.group:
            group = _resolve_group(scope, args.group)
            group_id = group.id
            label = f"{group.name!r}"
        goals = scope.list_goals(group_id)
        if not goals:
            _out(
                f"No goals for {label}. Add one with "
                "`goal add \"<group>\" \"<what you're preparing for>\" "
                "--deadline YYYY-MM-DD`."
            )
            return 0
        names = {g.id: g.name for g in scope.list_groups()}
        _table(
            ["GOAL ID", "GROUP", "STATUS", "DEADLINE", "DESCRIPTION"],
            [
                [
                    goal.id,
                    _trunc(names.get(goal.group_id, "?"), 22),
                    goal.status,
                    _ts(goal.deadline, "long"),
                    _trunc(goal.description, 46),
                ]
                for goal in goals
            ],
        )
    finally:
        store.close()
    return 0


def cmd_goal_close(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        user = _resolve_user(store, getattr(args, "user", None))
        scope = store.scope(user.id)
        goal = scope.get_goal(args.goal_id)
        if goal is None:
            raise CliError(
                f"No goal {args.goal_id!r} for this user. Run `goal list` to see yours."
            )
        if goal.status != "active":
            _out(f"Goal {goal.id} is already {goal.status}; nothing to do.")
            return 0
        status = "expired" if args.expired else "completed"
        scope.close_goal(goal.id, status)
        _out(f"Goal {goal.id} marked {status}: {goal.description!r}")
        # Worth stating out loud: the spec treats a goal as a temporary
        # intensity spike, and people expect closing one to "finish" the group.
        known, total = _known_total(scope.ledger(goal.group_id))
        _out(
            f"Concept ledger untouched — {total} term(s) still recorded for this "
            f"group ({known} known). The group keeps running in maintenance mode."
        )
    finally:
        store.close()
    return 0


# --- Commands: session -----------------------------------------------------


def _raise_topic(system, scope, group: Group, reason: str, no_input: bool) -> None:
    """Brief, then let them ask until they stop. Never a quiz, never a score.

    The system states the substance and stops. Whatever the user types next is
    answered in the same thread; a blank line ends it, and only then is the
    thread read for evidence. Nothing printed here is a grade, a level, a band,
    or a before/after. If there is nothing untold in the group, nothing is
    printed at all -- a story is never repeated.
    """
    started = datetime.now(UTC)
    brief = system.assessor.raise_topic(scope, group)
    if brief is None:
        return
    _rule(group.name)
    if reason:
        _out(f"why now: {reason}")
    _out()
    _out(brief.briefing)
    _out()
    if no_input:
        _out(
            f"(--no-input: not waiting. Exchange {brief.exchange_id} is recorded "
            "and stays open.)"
        )
        return

    first = True
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            text = ""
        if first:
            # The CLI cannot see dwell or scroll -- text simply prints -- so it
            # records time-to-first-keystroke and labels it as the proxy it is.
            elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            scope.record_reading(
                brief.exchange_id,
                dwell_ms=elapsed_ms,
                scroll_fraction=None,
                source="response_time_proxy",
            )
            first = False
        if not text:
            break
        answer = system.assessor.respond(scope, group, brief.exchange_id, text)
        _out()
        _out(answer.text)
        if answer.source_url:
            _out(f"  source: {answer.source_url}")
        _out()
    system.assessor.close_thread(scope, group, brief.exchange_id)


def cmd_session(args: argparse.Namespace) -> int:
    system = _live_system(args)
    try:
        user = _resolve_user(system.store, getattr(args, "user", None))
        scope = system.store.scope(user.id)
        briefing = system.orchestrator.open_session(scope)

        _out(f"=== Session · {user.display_name} · "
             f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} ===")
        _out()
        _out(briefing.framing or "(no framing returned)")

        if briefing.events:
            _out()
            _out(f"What happened while you were away ({len(briefing.events)}):")
            names = {g.id: g.name for g in scope.list_groups()}
            for index, event in enumerate(briefing.events, start=1):
                _out()
                _out(f"  {index}. [{names.get(event.group_id, '?')}] {event.headline}")
                if event.detail:
                    _out(f"     {event.detail}")
                source = event.source_name or ""
                if event.source_url:
                    source = f"{source} — {event.source_url}".strip(" —")
                if source:
                    _out(f"     source: {source}")
                if event.materiality_reason:
                    _out(f"     why it matters: {event.materiality_reason}")

        if briefing.held_back_count:
            _out()
            _out(
                f"  ({briefing.held_back_count} lower-value item(s) held back for "
                "a later session.)"
            )

        if briefing.topic_group is not None:
            _raise_topic(
                system, scope, briefing.topic_group, briefing.topic_reason, args.no_input
            )

        if args.why and briefing.reasoning:
            _rule("Why this session looks like this")
            _out(f"  {briefing.reasoning}")
    except Exception as exc:
        if isinstance(exc, CliError):
            raise
        raise CliError(f"Session failed: {type(exc).__name__}: {exc}") from exc
    finally:
        system.close()
    return 0


# --- Commands: polling -----------------------------------------------------


def cmd_poll(args: argparse.Namespace) -> int:
    from .scheduler import run_forever, sweep_once

    system = _live_system(args)
    try:
        if args.forever:
            try:
                run_forever(system, tick_seconds=args.tick)
            except KeyboardInterrupt:
                _out()
                _out("Poller stopped.")
            return 0

        result = sweep_once(system)
        _out(
            f"Polled {result.polled} group(s); {result.material_found} material "
            f"finding(s); skipped {result.skipped_personas} persona group(s) "
            "(their feeds are injected by the harness)."
        )
        for note in result.notes:
            _out(f"  - {note}")
        for err in result.errors:
            _out(f"  ! {err}")
        if not result.polled and not result.errors:
            _out("Nothing was due. Poll intervals are per-group; see `group list`.")
    finally:
        system.close()
    return 0


# --- Commands: LLMOps console ---------------------------------------------


def cmd_models(args: argparse.Namespace) -> int:
    rows = []
    for point in config.ROUTED_CALLS:
        try:
            version = judgment.load_prompt(point).version
        except judgment.JudgmentError:
            version = "MISSING"
        model_env = os.environ.get(f"MODEL_{point.upper()}")
        effort_env = os.environ.get(f"EFFORT_{point.upper()}")
        overrides = []
        if model_env:
            overrides.append("model")
        if effort_env:
            overrides.append("effort")
        rows.append(
            [
                point,
                "judgment" if point in config.ALL_JUDGMENT_POINTS else "generative",
                config.model_for(point),
                config.effort_for(point),
                version,
                "+".join(overrides) if overrides else "-",
            ]
        )
    _table(["POINT", "KIND", "MODEL", "EFFORT", "PROMPT", "ENV OVERRIDE"], rows)
    _out()
    _out(
        f"{len(config.ALL_JUDGMENT_POINTS)} scored judgment points + "
        f"{len(config.ROUTED_CALLS) - len(config.ALL_JUDGMENT_POINTS)} generative calls "
        f"= {len(config.ROUTED_CALLS)} routed calls."
    )
    _out(
        "Re-route without a code change by exporting MODEL_<POINT> / "
        "EFFORT_<POINT>, e.g. MODEL_MATERIALITY=claude-sonnet-5."
    )
    return 0


def cmd_prompts(args: argparse.Namespace) -> int:
    if args.point:
        if args.point not in config.ROUTED_CALLS:
            raise CliError(
                f"Unknown judgment point {args.point!r}. "
                f"Known: {', '.join(config.ROUTED_CALLS)}"
            )
        try:
            prompt = judgment.load_prompt(args.point)
        except judgment.JudgmentError as exc:
            raise CliError(str(exc)) from None
        path = config.PROMPTS_DIR / f"{args.point}.md"
        _out(f"# {prompt.point}   version {prompt.version}")
        _out(f"# {path}")
        _out("-" * 70)
        _out(prompt.text)
        return 0

    rows = []
    for point in config.ROUTED_CALLS:
        path = config.PROMPTS_DIR / f"{point}.md"
        if not path.exists():
            rows.append([point, "MISSING", "-", "-", str(path)])
            continue
        prompt = judgment.load_prompt(point)
        stat = path.stat()
        rows.append(
            [
                point,
                prompt.version,
                f"{len(prompt.text)}c",
                _ts(datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()),
                path.name,
            ]
        )
    _table(["POINT", "VERSION", "SIZE", "MODIFIED", "FILE"], rows)
    _out()
    _out("Print one in full with `prompts <point>`.")
    return 0


def _trace_rows(
    store: Store, args: argparse.Namespace, *, limit: int
) -> tuple[list, str]:
    """Judgment-log rows for the scope the flags ask for. See `reporting.trace_rows`."""
    run_id = getattr(args, "run_id", None)
    all_users = bool(getattr(args, "all_users", False))
    user = None
    if not run_id and not all_users:
        user = _resolve_user_optional(store, getattr(args, "user", None))
    try:
        return reporting.trace_rows(
            store,
            user=user,
            point=getattr(args, "point", None),
            run_id=run_id,
            all_users=all_users,
            limit=limit,
        )
    except ValueError as exc:
        raise CliError(str(exc)) from None


def cmd_traces(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        rows, scope_label = _trace_rows(store, args, limit=args.limit)
        _out(f"Judgment calls — scope: {scope_label}")
        _out()
        if not rows:
            _out(
                "No traces recorded yet. Run a `session`, a `poll --once`, or an "
                "eval run first — every judgment call writes one row here."
            )
            return 0

        if not args.full:
            _table(
                ["WHEN", "POINT", "MODEL", "PROMPT", "MS", "IN", "OUT", "VERDICT", "REASONING"],
                [
                    [
                        _ts(r["created_at"]),
                        r["judgment_point"],
                        r["model"],
                        r["prompt_version"],
                        r["latency_ms"] if r["latency_ms"] is not None else "-",
                        r["input_tokens"] if r["input_tokens"] is not None else "-",
                        r["output_tokens"] if r["output_tokens"] is not None else "-",
                        "ERROR" if r["error"] else _trunc(r["verdict_json"], 34),
                        _trunc(r["error"] or r["reasoning"], 44),
                    ]
                    for r in rows
                ],
            )
            _out()
            _out(f"{len(rows)} call(s). `traces --full` or `trace <id>` for detail.")
            return 0

        for index, row in enumerate(rows):
            if index:
                _out()
            _print_trace_detail(row)
        return 0
    finally:
        store.close()


def _print_json(raw: str | None, indent: str = "    ") -> None:
    if not raw:
        _out(f"{indent}(none)")
        return
    try:
        text = json.dumps(json.loads(raw), indent=2, sort_keys=True)
    except (TypeError, ValueError):
        text = raw
    for line in text.splitlines():
        _out(indent + line)


def _print_trace_detail(row) -> None:
    _out("=" * 74)
    _out(f"{row['judgment_point']}   {row['id']}")
    _out("=" * 74)
    cost = reporting.cost(
        row["model"], row["input_tokens"] or 0, row["output_tokens"] or 0
    )
    _out(f"  when     {_ts(row['created_at'], 'long')}")
    _out(f"  model    {row['model']}  (effort {row['effort'] or '-'})")
    _out(f"  prompt   version {row['prompt_version']}")
    _out(f"  user     {row['user_id'] or '-'}   group {row['group_id'] or '-'}")
    _out(f"  run      {row['run_id'] or '-'}")
    _out(
        f"  cost     {row['latency_ms'] or 0} ms, "
        f"{row['input_tokens'] or 0} in / {row['output_tokens'] or 0} out tokens, "
        f"{f'${cost:.6f}' if cost is not None else 'price unknown for this model'}"
    )
    if row["error"]:
        _out()
        _out("  ERROR")
        _out(f"    {row['error']}")
    _out()
    _out("  INPUT CONTEXT")
    _print_json(row["input_json"])
    _out()
    _out("  VERDICT")
    _print_json(row["verdict_json"])
    _out()
    _out("  REASONING")
    for line in (row["reasoning"] or "(none)").splitlines() or ["(none)"]:
        _out(f"    {line}")


def cmd_trace(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        row = store.conn.execute(
            "SELECT * FROM judgment_log WHERE id = ?", (args.log_id,)
        ).fetchone()
        if row is None:
            raise CliError(
                f"No judgment call {args.log_id!r}. Run `traces` to list recent ids."
            )
        if not args.all_users and row["user_id"] is not None:
            user = _resolve_user_optional(store, getattr(args, "user", None))
            if user is None or row["user_id"] != user.id:
                # Same posture as `group show`: another user's row reports as
                # not found rather than revealing that it exists.
                raise CliError(
                    f"No judgment call {args.log_id!r} for this user. "
                    "Pass --user <owner> or --all-users if you meant another user's call."
                )
        _print_trace_detail(row)
    finally:
        store.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        # Aggregates want the whole history, not one display page.
        rows, scope_label = _trace_rows(store, args, limit=reporting.ALL_ROWS)

        _out(f"Judgment-call stats — scope: {scope_label}")
        _out()
        if not rows:
            _out(
                "No traces recorded yet. Run a `session`, a `poll`, or an "
                "eval run first."
            )
            return 0

        stats = reporting.aggregate_stats(rows)

        def line(entry: dict[str, Any]) -> list[Any]:
            return [
                entry["point"],
                entry["calls"],
                entry["errors"],
                _pct(entry["errors"], entry["calls"]),
                entry["mean_ms"],
                entry["p95_ms"],
                f"{entry['input_tokens']:,}",
                f"{entry['output_tokens']:,}",
                f"${entry['cost']:.4f}" + ("" if entry["cost_known"] else " +?"),
                _trunc(",".join(entry["models"]), 30) if entry["point"] != "TOTAL" else "",
            ]

        _table(
            ["POINT", "CALLS", "ERR", "ERR%", "MEAN MS", "P95 MS", "IN TOK", "OUT TOK", "COST", "MODELS"],
            [line(entry) for entry in (*stats["rows"], stats["total"])],
        )
        _out()
        priced = ", ".join(
            f"{model} ${price[0]:g}/${price[1]:g} per 1M in/out"
            for model, price in sorted(config.PRICES_PER_MTOK.items())
        )
        _out(f"Pricing: {priced}.")
        if not stats["total"]["cost_known"]:
            _out(
                "  +? = some calls used a model with no pricing entry; their tokens "
                "are counted but their cost is not estimated."
            )
    finally:
        store.close()
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        rows = store.eval_runs(limit=args.limit)
        if not rows:
            _out(
                "No eval runs recorded yet. The persona harness writes one row "
                "per run; run it first."
            )
            return 0
        table_rows = []
        for row in rows:
            metrics = {}
            if row["metrics_json"]:
                try:
                    parsed = json.loads(row["metrics_json"])
                    if isinstance(parsed, dict):
                        metrics = parsed
                except ValueError:
                    metrics = {}
            # Show only scalar metrics: a nested per-persona blob would blow the
            # column apart, and `--limit 1` plus the raw JSON is the way to read it.
            summary = ", ".join(
                f"{k}={v}"
                for k, v in list(metrics.items())
                if isinstance(v, (int, float, str, bool))
            )
            table_rows.append(
                [
                    row["id"],
                    row["suite"],
                    _ts(row["started_at"]),
                    _ts(row["finished_at"]) if row["finished_at"] else "running",
                    "-" if row["passed"] is None else ("pass" if row["passed"] else "FAIL"),
                    _trunc(summary, 58) if summary else "-",
                ]
            )
        _table(["RUN ID", "SUITE", "STARTED", "FINISHED", "RESULT", "METRICS"], table_rows)
        _out()
        _out("Drill into one run's calls with `traces --run-id <id>` or `stats --run-id <id>`.")
    finally:
        store.close()
    return 0


# --- Parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # --user/--db live on a parent with SUPPRESS defaults so they can be given
    # before OR after the subcommand without the leaf parser clobbering the
    # top-level value with None.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--user",
        default=argparse.SUPPRESS,
        metavar="NAME_OR_ID",
        help="Act as this user (default: $CONVAGENT_USER, else the sole real user).",
    )
    common.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="SQLite store to use (default: $CONVAGENT_DB, else the packaged data dir).",
    )

    parser = argparse.ArgumentParser(
        prog="python -m conversational_agent.cli",
        description=(
            "Conversational competence agent. Track groups you want to stay "
            "conversant with; open a session to be told what happened and "
            "talk about it; inspect every LLM judgment the system made."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands that need credentials: session and poll.\n"
            "Everything else (init, user, group, ledger, goal, models, "
            "prompts, traces, trace, stats, runs) runs offline."
        ),
    )
    parser.add_argument("--user", default=None, metavar="NAME_OR_ID", help=argparse.SUPPRESS)
    parser.add_argument("--db", default=None, metavar="PATH", help=argparse.SUPPRESS)
    subs = parser.add_subparsers(dest="command", metavar="<command>")

    # init
    p_init = subs.add_parser(
        "init", parents=[common], help="Create the store (and optionally a real user)."
    )
    p_init.add_argument("--name", help="Display name for the real human user.")
    p_init.set_defaults(func=cmd_init)

    # user
    p_user = subs.add_parser("user", help="Users (real humans and eval personas).")
    user_subs = p_user.add_subparsers(dest="user_command", metavar="<subcommand>")
    p_user_list = user_subs.add_parser("list", parents=[common], help="List all users.")
    p_user_list.set_defaults(func=cmd_user_list)
    p_user_add = user_subs.add_parser("add", parents=[common], help="Add a real user.")
    p_user_add.add_argument("name", help="Display name.")
    p_user_add.set_defaults(func=cmd_user_add)
    p_user.set_defaults(func=lambda a: _needs_subcommand(p_user))

    # group
    p_group = subs.add_parser("group", help="Groups you want to stay conversant with.")
    group_subs = p_group.add_subparsers(dest="group_command", metavar="<subcommand>")
    p_group_add = group_subs.add_parser(
        "add", parents=[common], help="Create a group."
    )
    p_group_add.add_argument("name", help='Group name, e.g. "NBA fans".')
    p_group_add.add_argument("--description", help="What this group actually talks about.")
    p_group_add.add_argument(
        "--poll-interval",
        type=int,
        metavar="MIN",
        help=f"Monitor poll interval in minutes (default {config.DEFAULT_POLL_INTERVAL_MINUTES}).",
    )
    p_group_add.set_defaults(func=cmd_group_add)
    p_group_list = group_subs.add_parser(
        "list",
        parents=[common],
        help="List your groups: what's new, when last engaged, active goal.",
    )
    p_group_list.set_defaults(func=cmd_group_list)
    p_group_show = group_subs.add_parser(
        "show", parents=[common], help="Full detail for one group."
    )
    p_group_show.add_argument("name", help="Group name or id.")
    p_group_show.set_defaults(func=cmd_group_show)
    p_group.set_defaults(func=lambda a: _needs_subcommand(p_group))

    # ledger
    p_ledger = subs.add_parser(
        "ledger",
        parents=[common],
        help="Every concept the system tracks for a group, its state and evidence.",
        description=(
            "The builder-facing view of what the system believes about a group "
            "and why. Open this when the product behaves oddly. Not a report "
            "card, and never shown to the user in a session."
        ),
    )
    p_ledger.add_argument("group", help="Group name or id.")
    p_ledger.add_argument(
        "--state",
        metavar="STATE",
        help=f"Show only one state: {', '.join(config.CONCEPT_STATES)}.",
    )
    p_ledger.set_defaults(func=cmd_ledger)

    # goal
    p_goal = subs.add_parser("goal", help="Time-bound goals layered on a group.")
    goal_subs = p_goal.add_subparsers(dest="goal_command", metavar="<subcommand>")
    p_goal_add = goal_subs.add_parser(
        "add", parents=[common], help="Attach a goal to a group."
    )
    p_goal_add.add_argument("group", help="Group name or id.")
    p_goal_add.add_argument("description", help='e.g. "dinner with 3 founders Thursday".')
    p_goal_add.add_argument(
        "--deadline",
        required=True,
        metavar="WHEN",
        help="YYYY-MM-DD (end of that day, UTC) or a full ISO timestamp.",
    )
    p_goal_add.set_defaults(func=cmd_goal_add)
    p_goal_list = goal_subs.add_parser(
        "list", parents=[common], help="List goals, active and historical."
    )
    p_goal_list.add_argument("group", nargs="?", help="Restrict to one group.")
    p_goal_list.set_defaults(func=cmd_goal_list)
    p_goal_close = goal_subs.add_parser(
        "close", parents=[common], help="Complete (or expire) a goal."
    )
    p_goal_close.add_argument("goal_id", help="Goal id from `goal list`.")
    p_goal_close.add_argument(
        "--expired",
        action="store_true",
        help="Mark expired rather than completed.",
    )
    p_goal_close.set_defaults(func=cmd_goal_close)
    p_goal.set_defaults(func=lambda a: _needs_subcommand(p_goal))

    # session
    p_session = subs.add_parser(
        "session",
        parents=[common],
        help="Open a session: what's material, then a briefing; ask whatever you like.",
    )
    p_session.add_argument(
        "--no-input",
        action="store_true",
        help="Print the briefing, then exit instead of waiting for questions.",
    )
    p_session.add_argument(
        "--why",
        action="store_true",
        help="Also print the interrupt-vs-wait judgment's reasoning.",
    )
    p_session.set_defaults(func=cmd_session)

    # poll
    p_poll = subs.add_parser(
        "poll", parents=[common], help="Run the background Monitor sweep."
    )
    p_poll.add_argument("--once", action="store_true", help="One sweep, then exit (default).")
    p_poll.add_argument("--forever", action="store_true", help="Run continuously until Ctrl-C.")
    p_poll.add_argument(
        "--tick",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Seconds between sweeps in --forever mode (default 300).",
    )
    p_poll.set_defaults(func=cmd_poll)

    # --- LLMOps console ---
    p_models = subs.add_parser(
        "models", parents=[common], help="Model + effort + prompt version per routed call."
    )
    p_models.set_defaults(func=cmd_models)

    p_prompts = subs.add_parser(
        "prompts", parents=[common], help="List prompt versions, or print one in full."
    )
    p_prompts.add_argument("point", nargs="?", help="Judgment point to print in full.")
    p_prompts.set_defaults(func=cmd_prompts)

    p_traces = subs.add_parser(
        "traces", parents=[common], help="Recent judgment calls from the log."
    )
    p_traces.add_argument("--point", help="Filter to one judgment point.")
    p_traces.add_argument("--run-id", dest="run_id", help="Filter to one eval run.")
    p_traces.add_argument("--limit", type=int, default=20, help="Rows to show (default 20).")
    p_traces.add_argument(
        "--full",
        action="store_true",
        help="Print full input context, verdict JSON and reasoning for each call.",
    )
    p_traces.add_argument(
        "--all-users",
        dest="all_users",
        action="store_true",
        help="Include every user's calls (default: the active user's, plus system-level).",
    )
    p_traces.set_defaults(func=cmd_traces)

    p_trace = subs.add_parser(
        "trace", parents=[common], help="One judgment call in full detail."
    )
    p_trace.add_argument("log_id", help="Judgment-log id from `traces`.")
    p_trace.add_argument(
        "--all-users", dest="all_users", action="store_true", help=argparse.SUPPRESS
    )
    p_trace.set_defaults(func=cmd_trace)

    p_stats = subs.add_parser(
        "stats", parents=[common], help="Per-judgment-point cost, latency and error rollup."
    )
    p_stats.add_argument("--run-id", dest="run_id", help="Restrict to one eval run.")
    p_stats.add_argument(
        "--all-users",
        dest="all_users",
        action="store_true",
        help="Include every user's calls (default: the active user's, plus system-level).",
    )
    p_stats.set_defaults(func=cmd_stats)

    p_runs = subs.add_parser("runs", parents=[common], help="Eval-harness run history.")
    p_runs.add_argument("--limit", type=int, default=20, help="Rows to show (default 20).")
    p_runs.set_defaults(func=cmd_runs)

    return parser


def _needs_subcommand(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        print("Interrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:  # `| head` on a long trace dump
        return 0


if __name__ == "__main__":
    sys.exit(main())
