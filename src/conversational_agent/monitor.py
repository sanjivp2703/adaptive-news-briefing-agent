"""The Monitor: watches a group for material developments.

One conceptual instance per (user, group). It runs on the background
scheduler, independent of whether a session is open, and its entire output is
rows in the store. It never calls the Orchestrator and never touches the user
-- "reported to the Orchestrator" is the `reported_as_trigger` column, nothing
more. That is what keeps the interrupt decision centralised in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import Any, Protocol

from . import judgments, search
from .judgment import Judge, JudgmentError
from .models import Group
from .store import UserScope


class EventFeed(Protocol):
    """Where candidate events come from.

    Real users get `LiveSearchFeed`, which hits the web. Persona users get a
    scripted feed injected by the eval harness, so persona runs exercise the
    same materiality judgment without live API load or nondeterminism.
    """

    def fetch(
        self, scope: UserScope, group: Group, recent_events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...


@dataclass
class LiveSearchFeed:
    """Real users: query formulation and source selection over live web search."""

    judge: Judge
    client: Any
    # Why the last fetch returned what it did. A poll that yields nothing is
    # ambiguous on its own -- search failed, the web was quiet, or everything
    # found was junk and correctly discarded are three very different states,
    # and the fix differs for each. Losing this made a live run undiagnosable.
    last_note: str = ""

    def fetch(
        self, scope: UserScope, group: Group, recent_events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        hours = None
        if group.last_polled_at:
            from datetime import datetime

            last = datetime.fromisoformat(group.last_polled_at)
            hours = (datetime.now(UTC) - last).total_seconds() / 3600.0
        found = search.discover_events(
            self.judge,
            self.client,
            group,
            recent_events=recent_events,
            hours_since_poll=hours,
        )
        queries = found.get("queries") or []
        self.last_note = "; ".join(
            part for part in (
                f"{len(queries)} quer{'y' if len(queries) == 1 else 'ies'}",
                f"{found.get('excluded_count', 0)} result(s) discarded"
                if found.get("excluded_count") else "",
                found.get("note", ""),
            ) if part
        )
        return found.get("events", [])


@dataclass
class PollResult:
    group_id: str
    candidates_seen: int
    material_count: int
    event_ids: list[str]
    note: str = ""


class Monitor:
    def __init__(self, judge: Judge, feed: EventFeed):
        self.judge = judge
        self.feed = feed

    def poll(self, scope: UserScope, group: Group) -> PollResult:
        """One polling cycle for one (user, group).

        Runs whether or not a session is open. Findings are written to the
        store either way; only the *surfacing* of them is session-gated, and
        that decision belongs to the Orchestrator.
        """
        recent = [
            {
                "headline": e.headline,
                "occurred_at": e.occurred_at,
                "is_material": e.is_material,
            }
            for e in scope.recent_events(group.id, limit=15)
        ]

        # Persona users must never reach live search. The scheduler already
        # skips them, but the guarantee belongs here too: it branches on user
        # kind rather than on a caller passing the right feed, so a direct
        # poll on a live-configured system cannot quietly bill real API calls
        # against a synthetic user.
        if scope.user_kind == "persona" and isinstance(self.feed, LiveSearchFeed):
            raise ValueError(
                f"Refusing to run live search for persona user {scope.user_id}: "
                "persona event feeds are injected by the eval harness."
            )

        candidates = self.feed.fetch(scope, group, recent)
        # A feed declares its own provenance; live search is the default so a
        # feed that doesn't care needn't implement it.
        origin = getattr(self.feed, "origin", "live_search")
        # Materiality is a question about the GROUP -- would someone conversant
        # here be expected to know this -- not about this particular user. It
        # used to be handed the user's knowledge level, which was both unused
        # and conceptually wrong: an event does not become more material
        # because the reader happens to be behind on it.
        group_ctx = {"name": group.name, "description": group.description}

        recorded: list[str] = []
        material = 0
        notes: list[str] = []
        feed_note = getattr(self.feed, "last_note", "")
        if feed_note:
            notes.append(feed_note)

        for candidate in candidates:
            event_id = scope.record_event(
                group.id,
                headline=candidate.get("headline", "").strip(),
                occurred_at=candidate.get("occurred_at"),
                detail=candidate.get("detail"),
                source_url=candidate.get("source_url"),
                source_name=candidate.get("source_name"),
                origin=origin,
            )
            recorded.append(event_id)
            try:
                verdict = judgments.judge_materiality(
                    self.judge,
                    group=group_ctx,
                    candidate_event=candidate,
                    recent_events=recent,
                    user_id=scope.user_id,
                    group_id=group.id,
                )
            except JudgmentError as exc:
                # A failed judgment leaves the event recorded but unjudged, so
                # it is visible in the store rather than silently dropped.
                notes.append(f"materiality judgment failed: {exc}")
                continue
            is_material = bool(verdict.get("is_material"))
            scope.apply_materiality(
                event_id,
                is_material=is_material,
                score=float(verdict.get("materiality_score", 0)),
                reason=verdict.reasoning,
            )
            if is_material:
                material += 1

        # Stamp the poll even when nothing was found -- a quiet poll is still
        # a poll, and the scheduler's due check reads this stamp.
        scope.mark_polled(group.id)

        return PollResult(
            group_id=group.id,
            candidates_seen=len(candidates),
            material_count=material,
            event_ids=recorded,
            note="; ".join(notes),
        )
