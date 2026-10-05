"""Shared fixtures. Everything here is offline: no network, no credentials.

Every test gets its own temp-file SQLite DB via `tmp_path`, so no test can see
another's rows and ordering never matters.
"""

from __future__ import annotations

from typing import Any

import pytest

from conversational_agent import config
from conversational_agent.judgment import Judge, StubClient
from conversational_agent.store import Store


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def store(db_path):
    s = Store(db_path)
    yield s
    s.close()


@pytest.fixture
def judge(stub, store):
    return Judge(client=stub, store=store)


@pytest.fixture
def stub():
    return StubClient()


class ScriptedFeed:
    """Stands in for `EventFeed`. Returns fixed candidates per group name.

    `raises_for` names a group whose fetch blows up, so a sweep's
    one-bad-group-must-not-stop-the-rest behaviour can be exercised.
    """

    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        by_group: dict[str, list[dict[str, Any]]] | None = None,
        raises_for: str | None = None,
    ):
        self.events = events or []
        self.by_group = by_group or {}
        self.raises_for = raises_for
        self.fetch_calls: list[str] = []

    def fetch(self, scope, group, recent_events):
        self.fetch_calls.append(group.id)
        if self.raises_for is not None and group.name == self.raises_for:
            raise RuntimeError("feed exploded")
        return list(self.by_group.get(group.name, self.events))


@pytest.fixture
def build_system(db_path):
    """Build the real wired system with only the LLM client and feed stubbed."""

    def _build(llm_client, feed=None, **kwargs):
        from conversational_agent import app

        return app.build(
            db_path=db_path,
            llm_client=llm_client,
            feed=feed or ScriptedFeed(),
            offline=True,
            **kwargs,
        )

    return _build


# --- Canned verdicts -------------------------------------------------------
# Handlers return the judgment point's schema-shaped verdict dict.


def material_verdict(score: float = 90.0):
    return lambda ctx: {
        "is_material": True,
        "materiality_score": score,
        "reasoning": "stub: material",
    }


def surface_only(event_ids: list[str]):
    """interrupt_timing verdict that surfaces exactly `event_ids`."""

    def handler(ctx):
        pending = ctx.get("pending_events", [])
        return {
            "should_surface": True,
            "event_ids": list(event_ids),
            "raise_topic": False,
            "framing": "Here's what's new.",
            "held_back_count": max(0, len(pending) - len(event_ids)),
            "reasoning": "stub: surface a subset",
        }

    return handler


def expose(scope, group_id: str, term: str, times: int) -> list[str]:
    """Use `term` in front of the user `times` separate times.

    A helper rather than a loop in each test because repeated exposure only
    means anything across *separate* calls -- one call passing a list of the
    same term repeated would be a different thing entirely. Collects every
    return value so a test can assert the whole run promoted nothing.
    """
    promoted: list[str] = []
    for _ in range(times):
        promoted.extend(scope.note_exposure(group_id, [term]))
    return promoted


def known_terms(scope, group_id: str) -> set[str]:
    return {c.term for c in scope.ledger(group_id) if c.is_known}


# --- The web surface --------------------------------------------------------
# An App whose "live" system is the real wired one, built offline with canned
# verdicts. Shared by the web API tests and the no-score tests.

WEB_EVENT = {
    "headline": "Arsenal sign a new striker for a club-record fee",
    "detail": "The deal is worth 105 million pounds including add-ons.",
    "occurred_at": "2026-10-01T09:00:00+00:00",
    "source_url": "https://example.com/arsenal-striker",
    "source_name": "Example Sport",
}


def web_stub() -> StubClient:
    stub = StubClient()
    stub.register(config.MATERIALITY, material_verdict())
    stub.register(
        config.INTERRUPT_TIMING,
        lambda ctx: {
            "should_surface": True,
            "event_ids": [e["id"] for e in ctx.get("pending_events", [])],
            "raise_topic": True,
            "framing": "One thing happened that people will be talking about.",
            "held_back_count": 0,
            "reasoning": "stub: surface everything",
        },
    )
    stub.register(
        config.BRIEFING,
        lambda ctx: {
            "topic": "record signing",
            "briefing": "Arsenal bought a striker. The price included add-ons, "
            "which are extra payments owed if he plays well.",
            "explained_terms": ["add-ons"],
            "terms_used": ["add-ons", "club record"],
            "reasoning": "stub",
        },
    )
    stub.register(
        config.GAP_ROUTING,
        lambda ctx: {"gap_size": "small", "explanation": "stub", "search_focus": "", "reasoning": "stub"},
    )
    stub.register(
        config.THREAD_REPLY,
        lambda ctx: {
            "answer": "A release clause is a set price that lets a player leave.",
            "source_url": None,
            "terms_used": ["release clause"],
            "explained_terms": ["release clause"],
            "reasoning": "stub",
        },
    )
    stub.register(
        config.CONCEPT_EVIDENCE,
        lambda ctx: {
            "understood": [],
            "not_understood": [],
            "asked_about": ["release clause"],
            "already_knew": False,
            "reasoning": "stub: they asked what a release clause is",
        },
    )
    return stub


def ok(web, method, path, body=None, user=None, query=None):
    """Dispatch one API call and return its payload, asserting it succeeded."""
    from conversational_agent.web.server import dispatch

    status, payload = dispatch(web, method, path, query or {}, body or {}, user)
    assert status == 200, f"{method} {path} -> {status}: {payload}"
    return payload


@pytest.fixture
def feed():
    return ScriptedFeed(events=[WEB_EVENT])


@pytest.fixture
def web(db_path, feed):
    from conversational_agent import app as app_module
    from conversational_agent.web.server import App

    def factory():
        return app_module.build(db_path=db_path, llm_client=web_stub(), feed=feed, offline=True)

    return App(db_path=db_path, system_factory=factory, live_ready=lambda: True)


@pytest.fixture
def sam(web):
    return ok(web, "POST", "/api/users", {"name": "Sam"})["id"]


@pytest.fixture
def league(web, sam):
    return ok(
        web, "POST", "/api/groups", {"name": "Premier League", "description": "English football"}, sam
    )["id"]
