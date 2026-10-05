"""Plain data records passed between components.

Deliberately dumb: no behaviour, no DB access. Components exchange these so
that a sub-agent can be handed a fixture in a test exactly as easily as a row
from the store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class User:
    id: str
    kind: str  # 'real' | 'persona'
    display_name: str
    profile: dict[str, Any] = field(default_factory=dict)

    @property
    def is_persona(self) -> bool:
        return self.kind == "persona"

    @classmethod
    def from_row(cls, row) -> User:
        return cls(
            id=row["id"],
            kind=row["kind"],
            display_name=row["display_name"],
            profile=_loads(row["profile_json"], {}),
        )


@dataclass(frozen=True)
class Group:
    id: str
    user_id: str
    name: str
    description: str | None
    poll_interval_minutes: int
    last_polled_at: str | None
    created_at: str

    @classmethod
    def from_row(cls, row) -> Group:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            description=row["description"],
            poll_interval_minutes=row["poll_interval_minutes"],
            last_polled_at=row["last_polled_at"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Goal:
    id: str
    user_id: str
    group_id: str
    description: str
    deadline: str
    status: str
    created_at: str

    @property
    def deadline_dt(self) -> datetime:
        return datetime.fromisoformat(self.deadline)

    @classmethod
    def from_row(cls, row) -> Goal:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            group_id=row["group_id"],
            description=row["description"],
            deadline=row["deadline"],
            status=row["status"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Concept:
    """One term, and the evidence for what the user understands of it.

    There is no score here on purpose. `state` records what was actually
    observed. `exposure_count` records how often we put the term in front of
    them -- useful for knowing whether it is new to the conversation, but
    explicitly NOT evidence: measured against ground truth, inferring
    understanding from unquestioned exposure was anti-predictive.
    """

    user_id: str
    group_id: str
    term: str
    state: str
    exposure_count: int
    correct_uses: int
    misunderstandings: int
    evidence: str | None
    first_seen_at: str
    last_seen_at: str
    explained_at: str | None
    confirmed_at: str | None
    # Explanations of this term the user has actually read: glosses inside
    # briefings they read or skimmed, plus answers to their own questions.
    read_explanations: int = 0
    # Which slice of the group the term belongs to ('appellation rules',
    # 'transfer market'), as labelled by the call that named it. None until a
    # labelling call has seen the term. A fact about the term, not the person:
    # it changes no state and moves no band on its own.
    subdomain: str | None = None

    @property
    def is_known(self) -> bool:
        """Held well enough that a briefing need not re-gloss the term."""
        from . import config

        return self.state in config.KNOWN_STATES

    @property
    def counts_toward_band(self) -> bool:
        """Strong enough to move the derived proficiency band.

        `confirmed` always. `familiar` / `explained` only once the term has
        been explained-and-read READ_EXPLANATIONS_BEFORE_BAND times. Mirrors
        `UserScope.proficiency`; kept here so reports can show the same split.
        """
        from . import config

        if self.state == config.CONCEPT_CONFIRMED:
            return True
        return (
            self.state in config.BAND_STATES
            and self.read_explanations >= config.READ_EXPLANATIONS_BEFORE_BAND
        )

    @classmethod
    def from_row(cls, row) -> Concept:
        return cls(
            user_id=row["user_id"],
            group_id=row["group_id"],
            term=row["term"],
            state=row["state"],
            exposure_count=row["exposure_count"],
            correct_uses=row["correct_uses"],
            misunderstandings=row["misunderstandings"],
            evidence=row["evidence"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            explained_at=row["explained_at"],
            confirmed_at=row["confirmed_at"],
            read_explanations=(
                int(row["read_explanations"] or 0)
                if "read_explanations" in row.keys()
                else 0
            ),
            subdomain=(row["subdomain"] or None) if "subdomain" in row.keys() else None,
        )


@dataclass(frozen=True)
class MonitorEvent:
    id: str
    user_id: str
    group_id: str
    occurred_at: str
    headline: str
    detail: str | None
    source_url: str | None
    source_name: str | None
    is_material: bool | None
    materiality_score: float | None
    materiality_reason: str | None
    reported_as_trigger: bool
    surfaced_at: str | None
    origin: str = "live_search"

    @classmethod
    def from_row(cls, row) -> MonitorEvent:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            group_id=row["group_id"],
            occurred_at=row["occurred_at"],
            headline=row["headline"],
            detail=row["detail"],
            source_url=row["source_url"],
            source_name=row["source_name"],
            is_material=None if row["is_material"] is None else bool(row["is_material"]),
            materiality_score=row["materiality_score"],
            materiality_reason=row["materiality_reason"],
            reported_as_trigger=bool(row["reported_as_trigger"]),
            surfaced_at=row["surfaced_at"],
            origin=row["origin"] if "origin" in row.keys() else "live_search",
        )


@dataclass(frozen=True)
class Exchange:
    """One briefing and the thread that followed it.

    The system states the substance and stops. Whether the user says anything
    at all is up to them, and a thread with no user turns is a perfectly normal
    outcome -- so there is no `opener`, no `reference_answer`, and no single
    `user_reply` here any more. The evidence fields are filled in once, at
    close, from the whole thread rather than from one reply.

    `dwell_ms` / `scroll_fraction` / `read_quality` describe how the briefing
    was READ. They are attention, never comprehension, and nothing in this
    record may be routed into the concept ledger. Display metrics are famously
    worthless as comprehension proxies; they are good enough to close an item
    out of a "new" count and to report engagement, and nothing more.
    """

    id: str
    user_id: str
    group_id: str
    raised_at: str
    topic: str | None
    briefing: str
    dwell_ms: int | None
    scroll_fraction: float | None
    read_quality: str | None
    reading_source: str | None
    closed_at: str | None
    understood: list[str]
    not_understood: list[str]
    asked_about: list[str]
    already_knew: bool | None
    created_at: str
    # The MonitorEvent this briefing was written from, or None when it was
    # composed over several events (or over nothing specific). This is how a
    # thread reply gets to see the source detail rather than only the
    # briefing's paraphrase of it.
    event_id: str | None = None
    # Terms the briefing defined inline. At close, if the briefing was read or
    # skimmed, exactly these terms are credited with one read explanation.
    explained_terms: list[str] = field(default_factory=list)
    # 0-10 attention readout; same proxies as read_quality, finer grained.
    attention: float | None = None
    # informed | lazy | unresolved once the briefing was skipped; else None.
    skip_kind: str | None = None

    @property
    def is_closed(self) -> bool:
        return self.closed_at is not None

    @classmethod
    def from_row(cls, row) -> Exchange:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            group_id=row["group_id"],
            raised_at=row["raised_at"],
            topic=row["topic"],
            briefing=row["briefing"],
            dwell_ms=row["dwell_ms"],
            scroll_fraction=row["scroll_fraction"],
            read_quality=row["read_quality"],
            reading_source=row["reading_source"],
            closed_at=row["closed_at"],
            understood=_loads(row["understood"], []),
            not_understood=_loads(row["not_understood"], []),
            asked_about=_loads(row["asked_about"], []),
            already_knew=(
                None if row["already_knew"] is None else bool(row["already_knew"])
            ),
            created_at=row["created_at"],
            event_id=row["event_id"] if "event_id" in row.keys() else None,
            explained_terms=(
                _loads(row["explained_terms"], [])
                if "explained_terms" in row.keys()
                else []
            ),
            attention=row["attention"] if "attention" in row.keys() else None,
            skip_kind=row["skip_kind"] if "skip_kind" in row.keys() else None,
        )


@dataclass(frozen=True)
class Turn:
    """One utterance inside a thread.

    `seq` orders the thread and is unique per exchange. `speaker` is `user` or
    `system`; the briefing itself is stored on the Exchange, so seq 1 is
    normally the user's first question.
    """

    id: str
    exchange_id: str
    user_id: str
    seq: int
    speaker: str  # 'user' | 'system'
    text: str
    created_at: str

    @property
    def is_user(self) -> bool:
        return self.speaker == "user"

    @classmethod
    def from_row(cls, row) -> Turn:
        return cls(
            id=row["id"],
            exchange_id=row["exchange_id"],
            user_id=row["user_id"],
            seq=row["seq"],
            speaker=row["speaker"],
            text=row["text"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class SearchResult:
    """A raw web-search hit, before any credibility judgment is applied."""

    title: str
    url: str
    snippet: str
    source_name: str = ""
