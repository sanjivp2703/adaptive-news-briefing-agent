"""Web search: two judgments (query formulation, source selection) with plumbing between.

The spec treats "dynamic source/query selection" as one judgment point. In
practice it is two decisions with a deterministic step in the middle:

    formulate queries  (judgment 5a)  -- what should we search for?
      -> execute search (plain plumbing) -- run it, return raw hits
    -> select sources  (judgment 5b)  -- which of these do we trust?

Splitting them this way keeps both halves pure and separately scoreable: the
eval harness can feed fixed search results to 5b without hitting the network,
and score 5a's queries without depending on what the web happened to return
that day. Fusing them into one tool-using call would make both untestable.

Search runs on Anthropic's server-side web search tool, so there is no
third-party search provider or extra credential.
"""

from __future__ import annotations

from typing import Any

from . import config, judgments
from .judgment import Judge, JudgmentError
from .models import Group, SearchResult


class SearchUnavailable(RuntimeError):
    """The search tool could not be reached or returned an error."""


def execute_search(client: Any, queries: list[str]) -> list[SearchResult]:
    """Run queries through the server-side web search tool; return raw hits.

    Deliberately dumb. It does not judge, filter, rank, or summarise -- that is
    judgment 5b's job, and keeping it separate is what makes 5b's failures
    attributable to 5b.
    """
    if not queries:
        return []

    instruction = (
        "Run a web search for each of the following queries and return the "
        "results. Do not analyse, rank, or summarise them.\n\n"
        + "\n".join(f"- {q}" for q in queries)
    )
    try:
        response = client.messages.create(
            model=config.SEARCH_EXECUTION_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": instruction}],
            tools=[{**config.WEB_SEARCH_TOOL, "max_uses": config.MAX_SEARCH_USES}],
        )
    except Exception as exc:
        raise SearchUnavailable(f"Search call failed: {exc}") from exc

    results: list[SearchResult] = []
    for block in response.content:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        content = block.content
        # A successful result's content is a list of hits; an error's content
        # is a single object with an error_code. Branch before indexing.
        if not isinstance(content, list):
            code = getattr(content, "error_code", "unknown")
            raise SearchUnavailable(f"Web search returned an error: {code}")
        for hit in content:
            url = getattr(hit, "url", "") or ""
            results.append(
                SearchResult(
                    title=getattr(hit, "title", "") or "",
                    url=url,
                    snippet=(getattr(hit, "page_age", "") or ""),
                    source_name=_domain_of(url),
                )
            )
    return results


def _domain_of(url: str) -> str:
    if "://" not in url:
        return url
    rest = url.split("://", 1)[1]
    return rest.split("/", 1)[0]


def discover_events(
    judge: Judge,
    client: Any,
    group: Group,
    *,
    recent_events: list[dict[str, Any]],
    hours_since_poll: float | None = None,
    remediation_focus: str | None = None,
) -> dict[str, Any]:
    """The full search pipeline. Returns discovered candidate events.

    Never raises on an empty or failed search -- it returns an empty event list
    with a note. An empty result is a correct answer ("this poll found
    nothing") and is meaningfully different from a fabricated one.
    """
    group_ctx = {"name": group.name, "description": group.description}

    try:
        query_verdict = judgments.judge_query_formulation(
            judge,
            group=group_ctx,
            recent_events=recent_events,
            hours_since_poll=hours_since_poll,
            remediation_focus=remediation_focus,
            user_id=group.user_id,
            group_id=group.id,
        )
    except JudgmentError as exc:
        # Same contract as every other failure path here: a failed search
        # yields nothing, never a remembered guess. Previously this one
        # propagated while the source-selection failure below was caught --
        # an asymmetry with no reason behind it.
        return {"events": [], "queries": [], "note": f"Query formulation failed: {exc}"}

    queries = [q for q in query_verdict.get("queries", []) if q.strip()]
    # Hard cap rather than trusting the prompt and the tool budget to stay in
    # sync: exceeding max_uses fails the entire search, so dropping a fifth
    # query is strictly better than losing the first four.
    if len(queries) > config.MAX_SEARCH_USES:
        queries = queries[: config.MAX_SEARCH_USES]
    if not queries:
        return {"events": [], "queries": [], "note": "No queries were produced."}

    try:
        raw = execute_search(client, queries)
    except SearchUnavailable as exc:
        # The no-hallucination fallback: a failed search yields nothing, never
        # a remembered guess.
        return {"events": [], "queries": queries, "note": str(exc)}

    if not raw:
        return {
            "events": [],
            "queries": queries,
            "note": "Search returned no results.",
        }

    try:
        selection = judgments.judge_source_selection(
            judge,
            group=group_ctx,
            expected_signals=query_verdict.get("expected_signals", ""),
            domain_confidence=query_verdict.get("domain_confidence", "medium"),
            results=[
                {
                    "title": r.title,
                    "url": r.url,
                    "snippet": r.snippet,
                    "source_name": r.source_name,
                }
                for r in raw
            ],
            user_id=group.user_id,
            group_id=group.id,
        )
    except JudgmentError as exc:
        return {"events": [], "queries": queries, "note": f"Source judgment failed: {exc}"}

    return {
        "events": selection.get("events", []),
        "queries": queries,
        "domain_confidence": query_verdict.get("domain_confidence", "medium"),
        "excluded_count": selection.get("excluded_count", 0),
        "note": selection.get("exclusion_notes", ""),
    }
