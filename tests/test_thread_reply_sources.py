"""A thread reply answers from the source material, and looks things up.

From the first live run: the source event said *"pick Chardonnay on 6 August at
over 11% potential alcohol"*, the briefing omitted the figure, the user asked
*"still under 11%?"*, and the reply said "I don't have a real figure". The next
briefing, built from the same event, stated the figure unprompted. The reply
call had only ever seen the briefing; the no-fabrication rule fired on a fact
the system held.

Two things had to change for that to be impossible: an exchange must know which
event it was written from, and the reply must see that event. A third closes
the remaining gap: when the answer really is not held, the reply may search
before saying so -- and must still say so, plainly, when the search finds
nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
import types

import pytest

from conversational_agent import config, db
from conversational_agent.assessor import Assessor
from conversational_agent.store import Store

EVENT_DETAIL = (
    "grower Vincent Finot at Montgueux (Aube) obtained a derogation to pick "
    "Chardonnay on 6 August at over 11% potential alcohol... Merlot and "
    "Semillon... cut at around 9% potential alcohol with total acidity of 7-8 g/L"
)
QUESTION = "what potential alcohol were they picking at that early - still under 11%?"


# --- Stubs -----------------------------------------------------------------


def _briefing_handler(ctx):
    return {
        "topic": "record-early 2026 harvest",
        "briefing": "Picking began on 6 August under a special derogation.",
        "explained_terms": ["derogation"],
        "terms_used": ["derogation", "harvest"],
        "reasoning": "stub",
    }


def _route(size: str):
    return lambda ctx: {
        "gap_size": size,
        "explanation": "stub",
        "search_focus": "Champagne 2026 yields",
        "reasoning": "stub",
    }


def _reply_handler(ctx):
    return {
        "answer": "stub answer",
        "source_url": None,
        "terms_used": ["potential alcohol"],
        "explained_terms": [],
        "reasoning": "stub",
    }


def _queries_handler(ctx):
    return {
        "queries": ["Champagne 2026 yields"],
        "expected_signals": "yield figures",
        "domain_confidence": "medium",
        "reasoning": "stub",
    }


def _fabricating_source_handler(ctx):
    """Must never be reached when search returned nothing."""
    return {
        "events": [
            {
                "headline": "FABRICATED yield collapse",
                "detail": "invented",
                "source_url": "https://example.invalid/made-up",
                "source_name": "example.invalid",
                "confidence": 99,
            }
        ],
        "excluded_count": 0,
        "exclusion_notes": "",
        "reasoning": "stub",
    }


def _selecting_source_handler(ctx):
    return {
        "events": [
            {
                "headline": "CIVC sets 2026 Champagne yield",
                "detail": "The appellation yield was set at 9,000 kg/ha.",
                "source_url": "https://example.org/civc-2026",
                "source_name": "example.org",
                "confidence": 80,
            }
        ],
        "excluded_count": 0,
        "exclusion_notes": "",
        "reasoning": "stub",
    }


class _EmptySearchClient:
    def __init__(self):
        self.messages = types.SimpleNamespace(
            create=lambda **kw: types.SimpleNamespace(content=[])
        )


class _HitSearchClient:
    def __init__(self):
        hit = types.SimpleNamespace(
            url="https://example.org/civc-2026", title="CIVC yield", page_age="1d"
        )
        block = types.SimpleNamespace(type="web_search_tool_result", content=[hit])
        self.messages = types.SimpleNamespace(
            create=lambda **kw: types.SimpleNamespace(content=[block])
        )


class _ExplodingSearchClient:
    def __init__(self):
        def _boom(**kw):
            raise AssertionError("search must not be called when reply_search is off")

        self.messages = types.SimpleNamespace(create=_boom)


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def world(store):
    user = store.create_user("pilar", kind="real")
    scope = store.scope(user.id)
    group = scope.create_group("Wine trade")
    event_id = scope.record_event(
        group.id,
        headline="First 2026 grapes picked in Champagne on 6 August under derogation",
        detail=EVENT_DETAIL,
        source_name="Vitisphere",
        source_url="https://example.org/champagne-2026-early-pick",
    )
    return types.SimpleNamespace(
        scope=scope, group=group, event=scope.get_event(event_id)
    )


def _contexts(stub, point):
    # StubClient records the whole wire payload; the judgment's own context is
    # the inner `context` object.
    return [c["context"]["context"] for c in stub.calls if c["point"] == point]


# --- The link ---------------------------------------------------------------


# Prevents: an exchange that cannot say which event it came from -- which is
# what made the source unreachable from the reply in the first place -- and a
# link across users, which would surface one user's event detail in another's
# thread.
def test_open_exchange_links_to_this_users_event_only(store, world):
    scope = world.scope
    linked = scope.open_exchange(world.group.id, briefing="b", event_id=world.event.id)
    unlinked = scope.open_exchange(world.group.id, briefing="b")
    assert scope.get_exchange(linked).event_id == world.event.id
    assert scope.get_exchange(unlinked).event_id is None

    other = store.scope(store.create_user("bob", kind="persona").id)
    other_group = other.create_group("Wine trade")
    with pytest.raises(KeyError):
        other.open_exchange(other_group.id, briefing="b", event_id=world.event.id)


# Prevents: `raise_topic(event=...)` briefing from an event and then dropping
# the id on the floor -- and, since checkpoint 4, a story being told twice.
# Every briefing is written from exactly one untold event and records it; a
# told event is never briefed again; nothing untold means no briefing at all.
# (Human rule: "it should never repeat the story; if there's nothing new to
# say, don't say anything.")
def test_raise_topic_records_the_single_source_event(judge, stub, world):
    stub.register(config.BRIEFING, _briefing_handler)
    assessor = Assessor(judge)

    pinned = assessor.raise_topic(world.scope, world.group, event=world.event)
    assert world.scope.get_exchange(pinned.exchange_id).event_id == world.event.id

    # Told once: pinning it again is refused, and the free choice skips it.
    assert assessor.raise_topic(world.scope, world.group, event=world.event) is None
    assert assessor.raise_topic(world.scope, world.group) is None, (
        "nothing untold -> nothing said"
    )

    second = world.scope.record_event(world.group.id, headline="second development")
    world.scope.apply_materiality(second, is_material=True, score=90.0, reason="t")
    nxt = assessor.raise_topic(world.scope, world.group)
    assert world.scope.get_exchange(nxt.exchange_id).event_id == second
    assert assessor.raise_topic(world.scope, world.group) is None
    assert world.scope.untold_events(world.group.id) == []


# --- The reply sees the source ---------------------------------------------


# Prevents: the live failure. The reply (and the router deciding whether it
# needs a search) must see the event detail, distinct from the briefing, and
# neither call may see reading behaviour.
def test_respond_context_carries_the_source_event_when_linked(judge, stub, world):
    stub.register(config.BRIEFING, _briefing_handler)
    stub.register(config.GAP_ROUTING, _route("small"))
    stub.register(config.THREAD_REPLY, _reply_handler)
    assessor = Assessor(judge)
    briefing = assessor.raise_topic(world.scope, world.group, event=world.event)
    world.scope.record_reading(briefing.exchange_id, dwell_ms=240_000, scroll_fraction=1.0)

    assessor.respond(world.scope, world.group, briefing.exchange_id, QUESTION)

    (reply_ctx,) = _contexts(stub, config.THREAD_REPLY)
    assert reply_ctx["source_event"]["id"] == world.event.id
    assert reply_ctx["source_event"]["detail"] == EVENT_DETAIL
    assert reply_ctx["source_event"]["source_name"] == "Vitisphere"
    assert reply_ctx["briefing_we_gave"] == briefing.briefing
    assert reply_ctx["question"] == QUESTION
    assert reply_ctx["lookup"] is None and reply_ctx["supporting_source"] is None

    (routing_ctx,) = _contexts(stub, config.GAP_ROUTING)
    assert routing_ctx["source_event"]["detail"] == EVENT_DETAIL

    for ctx in (reply_ctx, routing_ctx):
        blob = json.dumps(ctx)
        assert "dwell" not in blob and "scroll" not in blob and "read_quality" not in blob
        # The per-subdomain readout travels in these contexts now; it is built
        # from ledger evidence only, and nothing about reading may ride with it.
        assert "subdomain_familiarity" in ctx
        for field in ("reading_source", "attention", "skip_kind"):
            assert f'"{field}"' not in blob, f"{field} leaked into the {ctx.keys()} context"


def test_respond_context_has_no_source_event_when_unlinked(judge, stub, world):
    stub.register(config.GAP_ROUTING, _route("small"))
    stub.register(config.THREAD_REPLY, _reply_handler)
    exchange_id = world.scope.open_exchange(world.group.id, briefing="composed briefing")

    Assessor(judge).respond(world.scope, world.group, exchange_id, QUESTION)

    (reply_ctx,) = _contexts(stub, config.THREAD_REPLY)
    assert reply_ctx["source_event"] is None


# --- The reply can look things up ------------------------------------------


# Prevents: "I don't have that" as the immediate answer to a question the
# material does not cover. With search on (the default), a `large` routing
# runs the search; when it finds nothing the reply is told so -- attempted,
# not found -- and source selection is never consulted, so nothing can be
# invented from an empty result.
def test_respond_searches_when_the_answer_is_not_held_and_reports_nothing_found(
    judge, stub, world
):
    stub.register(config.GAP_ROUTING, _route("large"))
    stub.register(config.QUERY_FORMULATION, _queries_handler)
    stub.register(config.SOURCE_SELECTION, _fabricating_source_handler)
    stub.register(config.THREAD_REPLY, _reply_handler)
    exchange_id = world.scope.open_exchange(
        world.group.id, briefing="b", event_id=world.event.id
    )

    answer = Assessor(judge, client=_EmptySearchClient()).respond(
        world.scope, world.group, exchange_id, "yields must be down too?"
    )

    points = [c["point"] for c in stub.calls]
    assert config.QUERY_FORMULATION in points
    assert config.SOURCE_SELECTION not in points
    (reply_ctx,) = _contexts(stub, config.THREAD_REPLY)
    assert reply_ctx["lookup"]["attempted"] is True
    assert reply_ctx["lookup"]["found"] is False
    assert reply_ctx["supporting_source"] is None
    assert answer.source_url is None


def test_respond_hands_a_found_source_to_the_reply(judge, stub, world):
    stub.register(config.GAP_ROUTING, _route("large"))
    stub.register(config.QUERY_FORMULATION, _queries_handler)
    stub.register(config.SOURCE_SELECTION, _selecting_source_handler)
    stub.register(config.THREAD_REPLY, _reply_handler)
    exchange_id = world.scope.open_exchange(world.group.id, briefing="b")

    answer = Assessor(judge, client=_HitSearchClient()).respond(
        world.scope, world.group, exchange_id, "yields must be down too?"
    )

    (reply_ctx,) = _contexts(stub, config.THREAD_REPLY)
    assert reply_ctx["lookup"]["found"] is True
    assert reply_ctx["supporting_source"]["source_url"] == "https://example.org/civc-2026"
    assert "9,000 kg/ha" in reply_ctx["supporting_source"]["summary"]
    assert answer.source_url == "https://example.org/civc-2026"


# Prevents: the flag the harness uses for cheap offline runs quietly not
# working -- a search call is a network call and a spend.
def test_reply_search_can_be_switched_off(judge, stub, world):
    stub.register(config.GAP_ROUTING, _route("large"))
    stub.register(config.QUERY_FORMULATION, _queries_handler)
    stub.register(config.THREAD_REPLY, _reply_handler)
    exchange_id = world.scope.open_exchange(world.group.id, briefing="b")

    Assessor(judge, client=_ExplodingSearchClient(), reply_search=False).respond(
        world.scope, world.group, exchange_id, "yields must be down too?"
    )

    assert config.QUERY_FORMULATION not in [c["point"] for c in stub.calls]
    (reply_ctx,) = _contexts(stub, config.THREAD_REPLY)
    assert reply_ctx["lookup"] == {
        "attempted": False,
        "found": False,
        "summary": None,
        "source_url": None,
        "note": "Search is disabled for this session.",
    }


def test_build_threads_reply_search_through(build_system, stub):
    system = build_system(stub, reply_search=False)
    try:
        assert system.assessor.reply_search is False
    finally:
        system.close()


# --- Turn accounting -------------------------------------------------------


# Prevents: a caller wrapping `respond` in its own add_turn calls. `respond`
# owns both writes; the harness once double-counted every turn by adding them
# outside.
def test_respond_records_exactly_the_users_turn_and_its_own(judge, stub, world):
    stub.register(config.GAP_ROUTING, _route("small"))
    stub.register(config.THREAD_REPLY, _reply_handler)
    exchange_id = world.scope.open_exchange(
        world.group.id, briefing="b", event_id=world.event.id
    )

    Assessor(judge).respond(world.scope, world.group, exchange_id, QUESTION)

    turns = world.scope.thread_turns(exchange_id)
    assert [(t.speaker, t.text) for t in turns] == [
        ("user", QUESTION),
        ("system", "stub answer"),
    ]


# --- Migration -------------------------------------------------------------


# Prevents: a live store created before the column existed silently keeping
# the old shape. `CREATE TABLE IF NOT EXISTS` does nothing to an existing
# table, so the column has to be added on open.
def test_existing_store_without_event_id_column_is_migrated_on_open(tmp_path):
    path = tmp_path / "old.db"
    old_schema = re.sub(
        r",\n\s*-- The event this briefing.*?event_id\s+TEXT REFERENCES "
        r"monitor_events\(id\) ON DELETE SET NULL",
        "",
        db.SCHEMA,
        flags=re.S,
    )
    assert "event_id" not in old_schema
    legacy = sqlite3.connect(path)
    legacy.executescript(old_schema)
    legacy.close()

    store = Store(path)
    try:
        columns = {
            row["name"] for row in store.conn.execute("PRAGMA table_info(exchanges)")
        }
        assert "event_id" in columns
        scope = store.scope(store.create_user("migrated", kind="real").id)
        group = scope.create_group("Wine trade")
        event_id = scope.record_event(group.id, headline="h", detail=EVENT_DETAIL)
        exchange_id = scope.open_exchange(group.id, briefing="b", event_id=event_id)
        assert scope.get_exchange(exchange_id).event_id == event_id
    finally:
        store.close()
