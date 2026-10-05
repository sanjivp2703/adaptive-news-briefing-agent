"""The no-hallucination fallback.

The spec names external-dependency failure as a risk and asks that the
fallback be explicitly tested. The dangerous outcome is not an error -- it is
a plausible-looking event the model invented because search gave it nothing.

Under the redesign this matters more, not less: the system now *briefs* the
user on what it found rather than quizzing them about it, so a fabricated
event is stated to them as fact.
"""

from __future__ import annotations

import types

import pytest

from conversational_agent import config, search


class _ExplodingSearchClient:
    """Server-side web search is down / rate-limited."""

    def __init__(self):
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        raise RuntimeError("503 upstream unavailable")


class _EmptySearchClient:
    """Search succeeded but matched nothing."""

    def __init__(self):
        self.messages = types.SimpleNamespace(
            create=lambda **kw: types.SimpleNamespace(content=[])
        )


def _queries_handler(ctx):
    return {
        "queries": ["latest wine tasting news"],
        "expected_signals": "vintage releases, awards",
        "domain_confidence": "medium",
        "reasoning": "stub: queries",
    }


def _fabricating_source_handler(ctx):
    """If judgment 5b is ever reached without real hits, it invents one --
    which is exactly what these tests must prove cannot happen."""
    return {
        "events": [
            {
                "headline": "FABRICATED: Bordeaux 2024 declared vintage of the century",
                "detail": "invented",
                "source_url": "https://example.invalid/made-up",
                "source_name": "example.invalid",
                "confidence": 99,
            }
        ],
        "excluded_count": 0,
        "exclusion_notes": "",
        "reasoning": "stub: fabricated",
    }


@pytest.fixture
def group(store):
    user = store.create_user("searcher", kind="real")
    return store.scope(user.id).create_group("wine tasting")


# Prevents: a search outage producing invented "news" that is then stated to
# the user as fact in a briefing. Also covers execute_search's contract: if it
# swallowed the transport failure and returned [], the note below would say
# "no results" instead of naming the outage, hiding it from the caller.
def test_discover_events_returns_nothing_when_search_is_unavailable(judge, stub, group):
    stub.register(config.QUERY_FORMULATION, _queries_handler)
    stub.register(config.SOURCE_SELECTION, _fabricating_source_handler)

    found = search.discover_events(judge, _ExplodingSearchClient(), group, recent_events=[])

    assert found["events"] == []
    assert found["queries"] == ["latest wine tasting news"]
    assert "503 upstream unavailable" in found["note"]
    # Source selection must never even be consulted with no results in hand.
    assert [c["point"] for c in stub.calls] == [config.QUERY_FORMULATION]

    with pytest.raises(search.SearchUnavailable):
        search.execute_search(_ExplodingSearchClient(), ["anything"])


# Prevents: "search found nothing" being upgraded into a confident event list
# rather than reported as the correct answer, which is an empty one.
def test_discover_events_returns_nothing_when_search_finds_no_results(judge, stub, group):
    stub.register(config.QUERY_FORMULATION, _queries_handler)
    stub.register(config.SOURCE_SELECTION, _fabricating_source_handler)

    found = search.discover_events(judge, _EmptySearchClient(), group, recent_events=[])

    assert found["events"] == []
    assert "no results" in found["note"].lower()
    assert [c["point"] for c in stub.calls] == [config.QUERY_FORMULATION]
