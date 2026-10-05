"""The harness's adapter over the system's thread API.

The runner drives one briefing-and-thread round through three calls on the
real `Assessor`, plus the store's reading record:

    Assessor.raise_topic(scope, group, event=None) -> Briefing | None
    Assessor.respond(scope, group, exchange_id, user_text) -> Answer
    Assessor.close_thread(scope, group, exchange_id) -> ThreadOutcome
    UserScope.record_reading(exchange_id, dwell_ms, scroll_fraction, source)

`SystemContract` wraps those calls and does three small things on top of them:

* **Normalises what comes back** into frozen, lower-cased projections
  (`Briefing`, `Answer`, `ThreadOutcome`), so the metrics compare terms without
  caring how the system happened to case or order them.
* **Checks the turn count around `respond`.** `respond` records both the user's
  turn and its own. If the harness also recorded the user's turn, every turn
  count in the report would double without any error, so the adapter counts
  turns before and after and files a note when the difference is not exactly
  one user turn followed by one system turn.
* **Reads the stored reading columns back** (`stored_reading`), so the harness
  can compare the band the store derived with the band it derives itself from
  the same two numbers.

There is no harness-side implementation of any system behaviour here: every
call lands in `src/`, so every number in the report measures the system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from conversational_agent.models import Group, MonitorEvent
from conversational_agent.store import UserScope

# --- Projections of what the system returns --------------------------------


@dataclass(frozen=True)
class Briefing:
    exchange_id: str
    topic: str
    briefing: str
    explained_terms: tuple[str, ...] = ()
    terms_used: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    text: str
    source_url: str | None = None


@dataclass(frozen=True)
class ThreadOutcome:
    understood: tuple[str, ...] = ()
    not_understood: tuple[str, ...] = ()
    asked_about: tuple[str, ...] = ()
    already_knew: bool | None = None
    newly_known: tuple[str, ...] = ()
    proficiency_before: str = ""
    proficiency_after: str = ""
    turn_count: int = 0


def _norm(terms: Any) -> tuple[str, ...]:
    out: list[str] = []
    for raw in terms or ():
        term = str(raw).strip().lower()
        if term and term not in out:
            out.append(term)
    return tuple(out)


# --- The adapter -----------------------------------------------------------


class SystemContract:
    """The one object the runner drives a round through."""

    def __init__(self, system: Any) -> None:
        self.system = system
        self.assessor = system.assessor
        # Things the adapter observed that a reader of the report should see,
        # such as `respond` not leaving exactly two new turns behind.
        self.notes: list[str] = []

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    # --- store side ------------------------------------------------------

    def record_reading(
        self,
        scope: UserScope,
        exchange_id: str,
        *,
        dwell_ms: int | None = None,
        scroll_fraction: float | None = None,
        source: str = "observed",
    ) -> None:
        scope.record_reading(
            exchange_id,
            dwell_ms=dwell_ms,
            scroll_fraction=scroll_fraction,
            source=source,
        )

    def stored_reading(self, scope: UserScope, exchange_id: str) -> dict[str, Any]:
        """Read the reading columns back out of storage.

        A plain read of the `exchanges` row. It exists so the harness can check
        the band the system derived against the band the harness derived from
        the same two numbers, rather than assuming the two agree.
        """
        row = _conn(scope).execute(
            "SELECT dwell_ms, scroll_fraction, read_quality, reading_source"
            " FROM exchanges WHERE id = ? AND user_id = ?",
            (exchange_id, scope.user_id),
        ).fetchone()
        if row is None:
            return {}
        return {
            "dwell_ms": row["dwell_ms"],
            "scroll_fraction": row["scroll_fraction"],
            "read_quality": row["read_quality"],
            "reading_source": row["reading_source"],
        }

    # --- assessor side ---------------------------------------------------

    def raise_topic(
        self, scope: UserScope, group: Group, event: MonitorEvent | None = None
    ) -> Briefing | None:
        raw = self.assessor.raise_topic(scope, group, event=event)
        if raw is None:
            return None
        return Briefing(
            exchange_id=str(raw.exchange_id or ""),
            topic=str(raw.topic or ""),
            briefing=str(raw.briefing or ""),
            explained_terms=_norm(raw.explained_terms),
            terms_used=_norm(raw.terms_used),
        )

    def respond(
        self, scope: UserScope, group: Group, exchange_id: str, user_text: str
    ) -> Answer:
        """The user said something; answer it. Both turns end up on the record.

        `Assessor.respond` owns both writes: it records the user's turn first
        and then its own, so that a failed answering call still leaves the
        question on the record for the evidence extractor. The caller must not
        record the user's turn as well. Getting that wrong is silent in either
        direction (record it twice and every turn count doubles; record it
        never and the extractor never sees the question), so the turn count is
        checked here on every call.
        """
        before = len(scope.thread_turns(exchange_id))
        raw = self.assessor.respond(scope, group, exchange_id, user_text)
        answer = Answer(text=str(raw.text or ""), source_url=raw.source_url)

        after = scope.thread_turns(exchange_id)
        if len(after) - before != 2:
            self.note(
                f"respond() changed the turn count by {len(after) - before}, not 2. "
                "The contract does not say who records the user's turn; if both "
                "sides do it, every turn count in this report is doubled."
            )
        elif [t.speaker for t in after[-2:]] != ["user", "system"]:
            self.note(
                "respond() left the last two turns as "
                f"{[t.speaker for t in after[-2:]]}, not ['user', 'system']. The "
                "user's question must be on the record before the answer, so a "
                "failed answering call still leaves the question for the "
                "extractor."
            )
        return answer

    def close_thread(
        self, scope: UserScope, group: Group, exchange_id: str
    ) -> ThreadOutcome:
        """Read the whole thread for evidence, write the ledger, close it."""
        raw = self.assessor.close_thread(scope, group, exchange_id)
        return ThreadOutcome(
            understood=_norm(raw.understood),
            not_understood=_norm(raw.not_understood),
            asked_about=_norm(raw.asked_about),
            already_knew=raw.already_knew,
            newly_known=_norm(raw.newly_known),
            proficiency_before=str(raw.proficiency_before or ""),
            proficiency_after=str(raw.proficiency_after or ""),
            turn_count=int(raw.turn_count or 0),
        )


# --- helpers ---------------------------------------------------------------


def _conn(scope: UserScope):
    """The scope's connection, for `stored_reading`'s direct read.

    This reaches past a private attribute, and it is confined to this module.
    The read goes through the same `user_id` binding the store itself uses, so
    it cannot see another user's rows.
    """
    return scope._conn
