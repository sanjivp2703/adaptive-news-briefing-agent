"""Reader-facing payloads: what the person using the product sees.

The rule this module lives under is the product's central one: nothing on the
user's surface is a test. So nothing here reads the derived band, a count of
what is "known", or the before/after a closed thread returns. The functions
below are scanned by `tests/test_web_no_score.py`, which fails if any of them
so much as reads one of those fields -- not printing a number today is one
edit away from printing it; not reading it is the property that holds.

Every function takes a `UserScope` (or builds one from a user id), so there is
no path here that can name two users at once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..models import Exchange, Goal, Group, MonitorEvent, User
from ..reporting import last_engaged, parse_deadline
from ..store import Store, UserScope
from . import ApiError

MAX_NAME = 80
MAX_DESCRIPTION = 400
MAX_QUESTION = 2000


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


# --- Users -----------------------------------------------------------------


def user_payload(store: Store, user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "name": user.display_name,
        "kind": user.kind,
        "groups": len(store.scope(user.id).list_groups()),
    }


def list_users(store: Store, include_personas: bool = False) -> list[dict[str, Any]]:
    users = store.list_users() if include_personas else store.list_users(kind="real")
    return [user_payload(store, u) for u in users]


def create_user(store: Store, name: Any) -> dict[str, Any]:
    display = _clean(name, MAX_NAME)
    if not display:
        raise ApiError(400, "A name is needed.")
    if store.find_user_by_name(display):
        raise ApiError(409, f"Someone called {display!r} already exists.")
    return user_payload(store, store.create_user(display, kind="real"))


# --- Groups and goals ------------------------------------------------------


def goal_payload(goal: Goal | None) -> dict[str, Any] | None:
    if goal is None:
        return None
    return {
        "id": goal.id,
        "group_id": goal.group_id,
        "description": goal.description,
        "deadline": goal.deadline,
        "status": goal.status,
    }


def group_payload(scope: UserScope, group: Group) -> dict[str, Any]:
    """One group as the reader sees it.

    `new` is the CLI's NEW column: things that happened and have not been shown
    yet. It is a fact about our queue, never a claim about the person.
    `untold` is how many stories could still be briefed, which is what decides
    whether a "tell me" button has anything behind it.
    """
    open_threads = scope.open_exchanges(group.id)
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "poll_interval_minutes": group.poll_interval_minutes,
        "last_polled_at": group.last_polled_at,
        "created_at": group.created_at,
        "new": scope.behind_count(group.id),
        "untold": len(scope.untold_events(group.id)),
        "last_engaged": last_engaged(scope, group.id),
        "goal": goal_payload(scope.active_goal(group.id)),
        "open_exchange_id": open_threads[-1].id if open_threads else None,
    }


def list_groups(scope: UserScope) -> list[dict[str, Any]]:
    return [group_payload(scope, g) for g in scope.list_groups()]


def require_group(scope: UserScope, group_id: str) -> Group:
    group = scope.get_group(group_id)
    if group is None:
        raise ApiError(404, "No such group.")
    return group


def create_group(
    scope: UserScope, name: Any, description: Any = None, poll_interval: Any = None
) -> dict[str, Any]:
    clean_name = _clean(name, MAX_NAME)
    if not clean_name:
        raise ApiError(400, "Give the group a name, like \"Premier League\".")
    if scope.find_group_by_name(clean_name):
        raise ApiError(409, f"You already follow a group named {clean_name!r}.")
    interval = None
    if poll_interval not in (None, ""):
        try:
            interval = int(poll_interval)
        except (TypeError, ValueError):
            raise ApiError(400, "The check interval must be a whole number of minutes.") from None
        if interval < 5:
            raise ApiError(400, "The check interval must be at least 5 minutes.")
    group = scope.create_group(
        name=clean_name,
        description=_clean(description, MAX_DESCRIPTION) or None,
        poll_interval_minutes=interval,
    )
    return group_payload(scope, group)


def list_goals(scope: UserScope) -> list[dict[str, Any]]:
    names = {g.id: g.name for g in scope.list_groups()}
    out = []
    for goal in scope.list_goals():
        row = goal_payload(goal) or {}
        row["group_name"] = names.get(goal.group_id, "")
        out.append(row)
    return out


def add_goal(scope: UserScope, group: Group, description: Any, deadline: Any) -> dict[str, Any]:
    text = _clean(description, MAX_DESCRIPTION)
    if not text:
        raise ApiError(400, "Say what you are preparing for.")
    try:
        parsed = parse_deadline(str(deadline or ""))
    except ValueError:
        raise ApiError(400, "Pick a date for the goal.") from None
    if datetime.fromisoformat(parsed) <= datetime.now(UTC):
        raise ApiError(400, "That date is already in the past.")
    if scope.active_goal(group.id):
        raise ApiError(409, "This group already has an active goal. Close it first.")
    try:
        goal = scope.create_goal(group.id, text, parsed)
    except ValueError as exc:
        raise ApiError(409, str(exc)) from None
    return goal_payload(goal) or {}


def close_goal(scope: UserScope, goal_id: str, expired: bool = False) -> dict[str, Any]:
    goal = scope.get_goal(goal_id)
    if goal is None:
        raise ApiError(404, "No such goal.")
    if goal.status == "active":
        scope.close_goal(goal.id, "expired" if expired else "completed")
    return goal_payload(scope.get_goal(goal_id)) or {}


# --- Events, exchanges, threads --------------------------------------------


def event_payload(event: MonitorEvent, group_name: str = "") -> dict[str, Any]:
    return {
        "id": event.id,
        "group_id": event.group_id,
        "group_name": group_name,
        "headline": event.headline,
        "detail": event.detail,
        "source_name": event.source_name,
        "source_url": event.source_url,
        "occurred_at": event.occurred_at,
        "why_it_matters": event.materiality_reason,
    }


def exchange_payload(scope: UserScope, exchange: Exchange) -> dict[str, Any]:
    """A briefing and the thread under it. The substance, and nothing else."""
    group = scope.get_group(exchange.group_id)
    source = None
    if exchange.event_id:
        event = scope.get_event(exchange.event_id)
        if event is not None:
            source = {
                "headline": event.headline,
                "source_name": event.source_name,
                "source_url": event.source_url,
                "occurred_at": event.occurred_at,
            }
    return {
        "id": exchange.id,
        "group_id": exchange.group_id,
        "group_name": group.name if group else "",
        "raised_at": exchange.raised_at,
        "topic": exchange.topic,
        "briefing": exchange.briefing,
        "closed": exchange.is_closed,
        # Whether a reading signal is already on record, so the browser knows
        # whether its own measurement is still wanted. Not the signal itself.
        "reading_recorded": exchange.read_quality is not None,
        "source": source,
        "turns": [
            {"speaker": t.speaker, "text": t.text, "at": t.created_at}
            for t in scope.thread_turns(exchange.id)
        ],
    }


def require_exchange(scope: UserScope, exchange_id: str) -> Exchange:
    exchange = scope.get_exchange(exchange_id)
    if exchange is None:
        raise ApiError(404, "No such thread.")
    return exchange


def open_threads(scope: UserScope) -> list[dict[str, Any]]:
    """Briefings that were given and never closed -- a tab shut mid-thread."""
    return [exchange_payload(scope, e) for e in scope.open_exchanges()]


def group_detail(scope: UserScope, group: Group) -> dict[str, Any]:
    payload = group_payload(scope, group)
    payload["goals"] = [goal_payload(g) for g in scope.list_goals(group.id)]
    payload["history"] = [
        exchange_payload(scope, e) for e in scope.exchange_history(group.id, limit=30)
    ]
    return payload


# --- Reading behaviour -----------------------------------------------------


def record_reading(scope: UserScope, exchange: Exchange, reading: Any) -> bool:
    """Record how the briefing was read, once, from what the browser observed.

    This is the first surface that can actually see dwell and scroll -- the
    CLI could only proxy them from time-to-first-keystroke -- so the source is
    `observed`. Recorded once per briefing: after the first question the
    person is reading our answers, not the briefing, and a second measurement
    would be measuring something else. Attention only; `record_reading`
    writes to the exchange row and nowhere else.
    """
    if exchange.read_quality is not None or not isinstance(reading, dict):
        return False
    dwell = reading.get("dwell_ms")
    scroll = reading.get("scroll_fraction")
    try:
        dwell_ms = None if dwell is None else max(0, min(int(dwell), 6 * 3600 * 1000))
        fraction = None if scroll is None else max(0.0, min(float(scroll), 1.0))
    except (TypeError, ValueError):
        return False
    if dwell_ms is None and fraction is None:
        return False
    scope.record_reading(
        exchange.id, dwell_ms=dwell_ms, scroll_fraction=fraction, source="observed"
    )
    return True


# --- The live paths: session, briefing, thread, polling --------------------


def open_session(system: Any, scope: UserScope) -> dict[str, Any]:
    """`cli session`, as data: what is worth putting in front of them now."""
    briefing = system.orchestrator.open_session(scope)
    names = {g.id: g.name for g in scope.list_groups()}
    topic = None
    if briefing.topic_group is not None:
        topic = {
            "group_id": briefing.topic_group.id,
            "group_name": briefing.topic_group.name,
            "reason": briefing.topic_reason,
        }
    return {
        "framing": briefing.framing,
        "quiet": briefing.quiet,
        "events": [event_payload(e, names.get(e.group_id, "")) for e in briefing.events],
        "held_back": briefing.held_back_count,
        "topic": topic,
        "why": briefing.reasoning,
    }


def brief(system: Any, scope: UserScope, group: Group) -> dict[str, Any] | None:
    """State the substance of the next untold story, and stop.

    None when there is nothing untold: a story is never told twice, and an
    empty queue is answered with silence rather than a reworded repeat.
    """
    result = system.assessor.raise_topic(scope, group)
    if result is None:
        return None
    return exchange_payload(scope, require_exchange(scope, result.exchange_id))


def ask(
    system: Any, scope: UserScope, exchange_id: str, text: Any, reading: Any = None
) -> dict[str, Any]:
    exchange = require_exchange(scope, exchange_id)
    if exchange.is_closed:
        raise ApiError(409, "This thread is finished.")
    question = str(text or "").strip()[:MAX_QUESTION]
    if not question:
        raise ApiError(400, "Type a question first.")
    record_reading(scope, exchange, reading)
    group = require_group(scope, exchange.group_id)
    answer = system.assessor.respond(scope, group, exchange.id, question)
    return {"text": answer.text, "source_url": answer.source_url}


def close(
    system: Any, scope: UserScope, exchange_id: str, reading: Any = None
) -> dict[str, Any]:
    """The person stopped. Read the thread for evidence, and say nothing of it.

    `close_thread` returns what the thread revealed; none of that is returned
    here. The reader is told the thread is done, and that is all.
    """
    exchange = require_exchange(scope, exchange_id)
    if exchange.is_closed:
        return {"closed": True}
    record_reading(scope, exchange, reading)
    group = require_group(scope, exchange.group_id)
    system.assessor.close_thread(scope, group, exchange.id)
    return {"closed": True}


def poll_group(system: Any, scope: UserScope, group: Group) -> dict[str, Any]:
    """Look for news in one group now, regardless of its interval."""
    result = system.monitor.poll(scope, group)
    return {
        "group_id": group.id,
        "candidates_seen": result.candidates_seen,
        "material_found": result.material_count,
        "note": result.note,
    }


def sweep(system: Any) -> dict[str, Any]:
    """`cli poll --once`: every group whose interval has elapsed, all users."""
    from ..scheduler import sweep_once

    result = sweep_once(system)
    return {
        "polled": result.polled,
        "material_found": result.material_found,
        "skipped_personas": result.skipped_personas,
        "notes": list(result.notes),
        "errors": list(result.errors),
    }
