"""Detection runs in the background; delivery is pull-only.

Two separate spec guarantees that fail in opposite directions:
  * detection must NOT be gated on a session (no retroactive computation);
  * surfacing must ONLY happen in a session, and only for what the
    Orchestrator actually chose to show.

The second is what `behind_count` now reports to the user, so an event marked
surfaced when it was never shown does not just lose the item -- it also tells
them they are caught up when they are not.
"""

from __future__ import annotations

import pytest
from conftest import ScriptedFeed, material_verdict, surface_only

from conversational_agent import config
from conversational_agent.judgment import StubClient

CANDIDATES = [
    {
        "headline": "Star guard traded to Denver",
        "occurred_at": "2026-09-01T10:00:00+00:00",
        "detail": "three-team deal",
        "source_url": "https://example.com/a",
        "source_name": "example.com",
    },
    {
        "headline": "Coach fired after 0-6 start",
        "occurred_at": "2026-09-02T10:00:00+00:00",
        "detail": "interim named",
        "source_url": "https://example.com/b",
        "source_name": "example.com",
    },
]


@pytest.fixture
def polled(build_system):
    """Run one background poll with no session ever opened."""
    stub = StubClient()
    stub.register(config.MATERIALITY, material_verdict())
    system = build_system(stub, feed=ScriptedFeed(events=CANDIDATES))

    user = system.store.create_user("nba-fan", kind="real")
    scope = system.store.scope(user.id)
    group = scope.create_group("NBA fans")

    result = system.monitor.poll(scope, group)
    yield system, stub, scope, group, result
    system.close()


# Prevents: detection being computed only when a session opens, which would
# make the Monitor's "continuous background process" a lie -- and prevents a
# poll auto-delivering to a user who never opened a session.
def test_a_background_poll_queues_events_without_any_session(polled):
    _system, _stub, scope, group, result = polled

    assert result.candidates_seen == 2 and result.material_count == 2
    # Written to the store by the poll itself, with no session in sight.
    assert len(scope.recent_events(group.id)) == 2
    assert scope.get_group(group.id).last_polled_at is not None

    pending = scope.pending_events()
    assert len(pending) == 2
    assert all(e.surfaced_at is None for e in pending)
    assert all(e.reported_as_trigger for e in pending)
    # Nothing was shown, so the user is two items behind.
    assert scope.behind_count(group.id) == 2


# Prevents: the Orchestrator marking everything surfaced when it only showed
# some of it, so held-back events are lost forever instead of waiting for a
# later session -- collapsing "record it" and "interrupt with it" into one bar,
# and understating how far behind the user actually is.
def test_events_held_back_by_the_orchestrator_stay_pending(polled):
    system, stub, scope, group, _result = polled
    first, second = scope.pending_events()

    stub.register(config.INTERRUPT_TIMING, surface_only([first.id]))
    briefing = system.orchestrator.open_session(scope)

    assert [e.id for e in briefing.events] == [first.id]
    assert scope.get_event(first.id).surfaced_at is not None

    still_pending = scope.pending_events()
    assert [e.id for e in still_pending] == [second.id]
    assert still_pending[0].surfaced_at is None
    assert scope.behind_count(group.id) == 1


# Prevents: the live failure in checkpoint 4 -- a group flagged for attention
# (never engaged) with every material event already briefed got the same story
# re-raised five sessions running. Attention is not news. A topic is raised
# only for a group with something UNTOLD, whatever the judgment says.
def test_no_topic_is_raised_for_a_group_with_nothing_untold(polled):
    system, stub, scope, group, _result = polled
    first, second = scope.pending_events()

    def raise_everything(ctx):
        return {
            "should_surface": True,
            "event_ids": [first.id, second.id],
            "raise_topic": True,
            "framing": "x",
            "held_back_count": 0,
        }

    stub.register(config.INTERRUPT_TIMING, raise_everything)
    stub.register(
        config.BRIEFING,
        lambda ctx: {
            "topic": "t",
            "briefing": "the substance.",
            "explained_terms": [],
            "terms_used": [],
        },
    )

    # Both events untold: a topic is raised, and the user has never spoken.
    session = system.orchestrator.open_session(scope)
    assert session.topic_group is not None
    system.assessor.raise_topic(scope, group)
    system.assessor.raise_topic(scope, group)
    assert scope.untold_events(group.id) == []
    assert scope.user_turn_count(group.id) == 0, "still never engaged"

    # Still never engaged, judgment still says raise_topic -- but nothing new.
    session = system.orchestrator.open_session(scope)
    assert session.topic_group is None
    assert system.assessor.raise_topic(scope, group) is None
