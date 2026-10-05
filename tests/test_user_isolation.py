"""Cross-user data isolation -- the spec's #1 named risk.

The real human account and N persona accounts run concurrently in one process
against one database. A scoping bug leaks one user's concept ledger, exchange
history or group data into another's session.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from conversational_agent import config
from conversational_agent import store as store_module


@pytest.fixture
def two_users(store):
    """Two users who each own a group with the *same* name, plus a row in
    every user-scoped table, so a query that forgets its user_id filter
    matches the wrong user's data instead of matching nothing."""
    alice = store.create_user("alice", kind="real")
    bob = store.create_user("bob", kind="persona", profile={"style": "noisy"})
    a, b = store.scope(alice.id), store.scope(bob.id)

    a_group = a.create_group("NBA fans")
    b_group = b.create_group("NBA fans")

    for _ in range(config.CORRECT_USES_BEFORE_CONFIRMED):
        b.mark_correct_use(b_group.id, ["pick swap"], evidence="bob used it correctly")
    b.note_exposure(b_group.id, ["luxury tax"])
    b_goal = b.create_goal(b_group.id, "watch party", "2999-01-01T00:00:00+00:00")
    b_event = b.record_event(b_group.id, headline="bob-only trade news")
    b.apply_materiality(b_event, is_material=True, score=95.0, reason="bob's")
    b_exchange = b.open_exchange(
        b_group.id,
        briefing="bob-only briefing",
        topic="trades",
    )
    b.add_turn(b_exchange, "user", "bob-only question")

    return {
        "store": store,
        "a": a,
        "b": b,
        "a_group": a_group,
        "b_group": b_group,
        "b_goal": b_goal,
        "b_event": b_event,
        "b_exchange": b_exchange,
    }


# Prevents: alice opening a session and seeing bob's groups, concept ledger,
# monitor events or exchange history because a read forgot its user_id filter.
def test_one_users_scope_cannot_read_another_users_rows(two_users):
    a, t = two_users["a"], two_users
    b_group, b_event, b_exchange, b_goal = (
        t["b_group"],
        t["b_event"],
        t["b_exchange"],
        t["b_goal"],
    )

    assert a.get_group(b_group.id) is None
    assert a.get_event(b_event) is None
    assert a.get_exchange(b_exchange) is None
    assert a.get_goal(b_goal.id) is None
    assert a.active_goal(b_group.id) is None
    assert a.recent_events(b_group.id) == []
    assert a.exchange_history(b_group.id) == []

    # The ledger is the most sensitive read of all: it is a claim about what a
    # specific person does and does not understand.
    assert a.ledger(b_group.id) == []
    assert a.concept_state(b_group.id, "pick swap") == config.CONCEPT_UNKNOWN
    assert a.proficiency(b_group.id) == config.BEGINNER
    assert a.behind_count(b_group.id) == 0

    # Same-name lookups must resolve within the caller's own data only.
    assert a.find_group_by_name("NBA fans").id == t["a_group"].id
    assert [g.id for g in a.list_groups()] == [t["a_group"].id]
    assert [g.id for g in a.list_goals()] == []
    # Alice's pending queue must not include bob's material event.
    assert [e.id for e in a.pending_events()] == []


# Prevents: alice's Monitor/Assessor silently rewriting bob's concept ledger,
# marking bob's events surfaced, or closing bob's goal.
def test_one_users_scope_cannot_mutate_another_users_rows(two_users):
    a, b, t = two_users["a"], two_users["b"], two_users
    b_group, b_event, b_exchange, b_goal = (
        t["b_group"],
        t["b_event"],
        t["b_exchange"],
        t["b_goal"],
    )

    a.mark_polled(b_group.id, when="2020-01-01T00:00:00+00:00")
    a.apply_materiality(b_event, is_material=False, score=0.0, reason="alice stomped it")
    a.mark_surfaced([b_event])
    # The whole thread API refuses outright rather than no-op'ing. That is
    # stronger than the silent-zero-rows behaviour of the older writes:
    # reaching into another user's conversation is a caller bug, and it should
    # be loud rather than quietly doing nothing.
    for reach in (
        lambda: a.add_turn(b_exchange, "user", "alice barging in"),
        lambda: a.record_reading(b_exchange, dwell_ms=999_999, scroll_fraction=1.0),
        lambda: a.close_exchange(
            b_exchange,
            understood=["everything"],
            not_understood=[],
            asked_about=[],
            already_knew=True,
        ),
    ):
        with pytest.raises(KeyError):
            reach()
    a.close_goal(b_goal.id, "completed")
    # Writing the ledger is the one that must land in *alice's* rows, not
    # bob's: a UserScope has no way to name another user, so this silently
    # creates alice's own concept rather than corrupting bob's.
    a.mark_unknown(b_group.id, ["pick swap"], evidence="alice stomped it")

    with pytest.raises(KeyError):
        a.create_goal(b_group.id, "alice's goal", "2999-01-01T00:00:00+00:00")

    still = b.get_group(b_group.id)
    assert still.last_polled_at is None
    assert b.concept_state(b_group.id, "pick swap") == config.CONCEPT_CONFIRMED
    assert {c.term for c in b.ledger(b_group.id)} == {"pick swap", "luxury tax"}

    event = b.get_event(b_event)
    assert event.is_material is True and event.surfaced_at is None
    assert [e.id for e in b.pending_events()] == [b_event]
    assert b.behind_count(b_group.id) == 1

    exchange = b.get_exchange(b_exchange)
    # Bob's thread is untouched: no turns from alice, no reading
    # recorded, never closed, no evidence extracted.
    assert exchange.closed_at is None
    assert exchange.understood == [] and exchange.asked_about == []
    assert exchange.dwell_ms is None and exchange.read_quality is None
    assert [t.text for t in b.thread_turns(b_exchange)] == ["bob-only question"]
    assert b.get_goal(b_goal.id).status == "active"


# --- Static guard ----------------------------------------------------------
# Example-based tests above only cover queries that exist today. This scans the
# source so a *future* query that forgets its user_id filter fails here.

# `concepts` and `exchanges` replaced `knowledge_state` and `quiz_interactions`
# in the ledger redesign. The list must track the schema: a user-scoped table
# missing from here is a table the guard does not guard.
USER_SCOPED_TABLES = {
    "groups",
    "goals",
    "concepts",
    "monitor_events",
    "exchanges",
    "turns",
}

# Functions allowed to touch a user-scoped table without binding user_id,
# because they are deliberately cross-user. Adding a name here is a decision
# that should be argued for in review.
CROSS_USER_WHITELIST = {"groups_due_for_poll"}

_TABLE_RE = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)


def _sql_fragments(node: ast.AST) -> list[str]:
    """Every string literal in `node`, with f-strings flattened to their
    literal parts (a table name is never interpolated in this codebase)."""
    out: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
        elif isinstance(sub, ast.JoinedStr):
            out.append(
                "".join(
                    v.value
                    for v in sub.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            )
    return out


# Prevents: a query added to store.py next month that reads or writes a
# user-scoped table without binding user_id -- the exact shape of the leak
# example-based tests cannot anticipate.
def test_every_user_scoped_sql_statement_binds_user_id():
    _assert_table_list_matches_the_schema()

    source = Path(store_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing_whitelist = CROSS_USER_WHITELIST - set(functions)
    assert not missing_whitelist, (
        f"Whitelist names no longer in store.py: {sorted(missing_whitelist)}. "
        "Remove them so the whitelist cannot hide a real query."
    )

    scanned = 0
    offenders: list[str] = []
    for name, func in functions.items():
        for sql in _sql_fragments(func):
            tables = {t.lower() for t in _TABLE_RE.findall(sql)}
            if not tables & USER_SCOPED_TABLES:
                continue
            scanned += 1
            if name in CROSS_USER_WHITELIST:
                continue
            if "user_id" not in sql:
                offenders.append(f"{name}(): {' '.join(sql.split())[:110]}")

    assert not offenders, "SQL touching user-scoped tables without a user_id:\n" + "\n".join(
        offenders
    )
    # A scanner that matches nothing would pass vacuously; store.py has ~25
    # such statements, so a collapse to near-zero means the scan broke.
    assert scanned >= 15, f"Only {scanned} user-scoped statements found; the scan is broken."


# Prevents: the table list above silently falling out of step with the schema,
# which is how the guard quietly stops guarding a whole table -- and is exactly
# what the ledger redesign did to it by replacing two tables at once. Asserted
# first inside the guard, so a stale list fails the guard rather than passing it.
def _assert_table_list_matches_the_schema():
    from conversational_agent import db

    # A table is user-scoped when it carries a NOT NULL user_id. `users` has
    # none and `judgment_log` deliberately allows NULL (system-level calls),
    # so both correctly fall outside the guard.
    bodies = re.findall(
        r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", db.SCHEMA, re.I | re.S
    )
    assert len(bodies) >= 6, f"Only {len(bodies)} tables parsed; the scan broke."
    scoped = {
        name
        for name, body in bodies
        if re.search(r"\buser_id\s+TEXT NOT NULL", body, re.I)
    }
    assert scoped == USER_SCOPED_TABLES, (
        f"Schema has user-scoped tables {sorted(scoped)} but the guard checks "
        f"{sorted(USER_SCOPED_TABLES)}."
    )
