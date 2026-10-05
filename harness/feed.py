"""The scripted event feed -- one of the harness's three substitutions.

Implements the foundation's `EventFeed` protocol, so `Monitor.poll` runs its
real recording and materiality path over injected events instead of live web
search. The spec calls for exactly this: personas exercise the same materiality
judgment as a real Monitor without generating live API load per synthetic user.

Two properties this file exists to guarantee:

1. **The ground-truth label never reaches the system.** `should_be_material` is
   held in a side registry keyed by (group name, headline), not on the dict
   handed to the Monitor. If it rode along in the candidate event it would be
   written into `judgment_log.input_json` and would be sitting in the prompt
   under `--live`, and the materiality number would be measuring nothing.

2. **Each event is delivered at most once per run.** Events are staged per
   group and drained on fetch, so a second poll in the same round sees an empty
   feed rather than re-injecting -- which would double-count every event in the
   precision/recall denominator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from conversational_agent.models import Group
from conversational_agent.store import UserScope

from .clock import SimClock
from .personas import PersonaEvent


@dataclass
class ScriptedEventFeed:
    """Persona events, replayed deterministically.

    `origin` is read by `Monitor.poll` via `getattr(feed, "origin", ...)` and
    stamped on every row this feed produces, which is what lets the materiality
    metric score simulated events only. Mixing injected and live-search events
    in one precision figure would not be measuring anything.
    """

    clock: SimClock
    origin: str = "simulated_feed"

    # group_id -> events queued for the next fetch
    _staged: dict[str, list[PersonaEvent]] = field(default_factory=dict)
    # (group name, headline) -> ground-truth materiality, for the scorer only
    _labels: dict[tuple[str, str], bool] = field(default_factory=dict)
    # every event this feed has actually delivered, in delivery order
    delivered: list[dict[str, Any]] = field(default_factory=list)

    def stage(self, group: Group, events: list[PersonaEvent]) -> None:
        """Queue this round's events for the group's next poll."""
        bucket = self._staged.setdefault(group.id, [])
        for event in events:
            bucket.append(event)
            self._labels[(group.name, event.headline)] = event.should_be_material

    def truth_for(self, group_name: str, headline: str) -> bool | None:
        """The answer key. Read by the scorer and by the offline stub only."""
        return self._labels.get((group_name, headline))

    def fetch(
        self, scope: UserScope, group: Group, recent_events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        staged = self._staged.pop(group.id, [])
        candidates: list[dict[str, Any]] = []
        for event in staged:
            candidates.append(
                {
                    "headline": event.headline,
                    "detail": event.detail,
                    "source_url": event.source_url,
                    "source_name": event.source_name,
                    # Dated relative to the simulated clock, so an event is
                    # always "recent" from the Monitor's point of view.
                    "occurred_at": self.clock.offset_iso(-event.occurred_hours_ago),
                }
            )
        self.delivered.extend(
            {"group_id": group.id, "group_name": group.name, **c} for c in candidates
        )
        return candidates
