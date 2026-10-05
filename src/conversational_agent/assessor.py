"""The Assessor: the concept ledger, and what to do about what it says.

Three rules govern everything in this file.

**1. The system states the substance. It never asks the user a question.**

The previous design named a subject and withheld the substance so the reply
would reveal familiarity. A real user could not answer a single opener, because
they were a beginner in every domain they had asked to follow -- which is the
*primary* case, not an edge case. That was fixed by briefing first.

The briefing-then-question shape that replaced it was still wrong, in a subtler
way. Every briefing ended in a question *we* chose, and choosing a question
means encoding what we assume the user knows: *"does a harvest running this far
ahead of normal sound like good news for the wine, or more like a warning sign
to you?"* cannot be answered by someone who does not already know what fast
ripening does to wine. The system was still testing people, just politely.

So `raise_topic` returns a briefing and nothing else. No closing question, no
invitation, no offered threads. It ends when the substance ends.

**2. An exchange is a thread, and the user decides when it is over.**

Briefing -> the user may ask -> `respond` answers -> they may ask again -> until
they stop. There is no judgment point that decides a conversation is finished,
because there is nothing for such a call to be right or wrong about. The user
stopping *is* the end. `close_thread` is called once it is quiet, and it reads
evidence, it does not adjudicate completion.

This inverts where the evidence comes from, for the better. What someone
chooses to ask localises their knowledge far more precisely than their answer
to a question we picked -- "what's a derogation?" and "was that a chaptalisation
year?" come from very different people, and neither is a reaction to our
framing.

**3. Knowledge is a ledger of concepts with evidence, not a score.**

`unknown` -> `provisional` / `familiar` -> `explained` -> `confirmed`, weakest
evidence to strongest, with a revealed misunderstanding reverting a term to
`unknown` from any state. Proficiency is a coarse band *derived* from that
ledger and is consulted only as a prior where there is no evidence about a
specific term. Being slightly wrong is acceptable by design: the cost is one
extra explanation, not a corrupted number that then has to be un-corrupted.

**Reading behaviour reaches the ledger through exactly one door.** Dwell and
scroll are recorded on the exchange as their own fact. The concept-evidence
judgment never sees them. The single sanctioned use is at `close_thread`: if
the briefing *defined* a term inline (`Exchange.explained_terms`) and its
`read_quality` was anything but `skipped`, that term is credited one read
explanation and, from `unknown`, becomes `familiar`. One such credit stops the
term being re-glossed; READ_EXPLANATIONS_BEFORE_BAND of them move the band.
That is a human decision (checkpoint 3): *"we explained and they read or
skimmed should count as they have familiarity; over time this will grow into
more confidence."* It is not the deleted `assumed` state -- that was silence
after mere usage, and it measured anti-predictive. `familiar` requires an
explanation to have been given and the text to have been looked at, and the
harness measures its precision by read count exactly as it did for `assumed`.

There is no decay. Concepts do not go stale, and falling behind on events is
countable (`scope.behind_count`) rather than modelled -- which is what
`session_need` below does, in plain arithmetic, with no LLM call.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import config, judgments, search
from .judgment import Judge, JudgmentError
from .models import Group, MonitorEvent
from .store import UserScope

# A goal is "soon" inside this window. A week is roughly the horizon at which
# someone starts actually preparing for a specific occasion; the exact number
# does not need to be right, because nothing irreversible hangs off it -- it
# only nudges which group gets attention first.
GOAL_SOON_DAYS = 7

# How much of the ledger to put in front of a prompt. The full ledger grows
# without bound; the prompt only needs enough to pitch the briefing and know
# which terms to define.
LEDGER_TERMS_PER_STATE = config.LEDGER_TERMS_PER_STATE

# What the reader is told when the answering call fails. Named because
# `close_thread` must recognise it: a turn carrying this text is not an answer.
NO_ANSWER_TEXT = (
    "I can't get you a reliable answer to that right now, so I'd "
    "rather not guess at it."
)


# --- Frozen contract -------------------------------------------------------


@dataclass
class Briefing:
    """What we put in front of the user: the substance, and only that.

    `briefing` is never empty and never elided. It is the product. There is no
    `opener` field and there must not be one -- the absence is the contract.
    """

    exchange_id: str
    topic: str
    briefing: str
    explained_terms: list[str] = field(default_factory=list)
    terms_used: list[str] = field(default_factory=list)


@dataclass
class Answer:
    """One reply to one question the user asked.

    `source_url` is None when the answer came from what we already knew, and
    also when we said plainly that we did not know -- which is a legitimate,
    intended answer, not a failure.
    """

    text: str
    source_url: str | None = None


@dataclass
class ThreadOutcome:
    """What a whole thread revealed, once the user stopped.

    No grade and no score: `understood` / `not_understood` / `asked_about` are
    observations over every turn, and the two proficiency fields are derived
    readouts included so a caller can see the ledger moved -- not stored state.

    `turn_count` counts the user's turns, not ours. Zero is a normal outcome:
    plenty of people read a briefing and have nothing to ask.
    """

    understood: list[str]
    not_understood: list[str]
    asked_about: list[str]
    already_knew: bool | None
    newly_known: list[str]
    proficiency_before: str
    proficiency_after: str
    turn_count: int


# --- Deterministic session need (no LLM) -----------------------------------


def session_need(
    scope: UserScope, group: Group, *, now: datetime | None = None
) -> dict[str, Any]:
    """Does this group warrant attention? Plain counting, no judgment call.

    This replaces the deleted `decay_due` judgment point. That call existed to
    estimate how far a number had drifted; there is no number, and the three
    things that actually make a group worth raising are all directly
    observable:

      * `behind`        -- material events detected and not yet shown. Falling
                           behind is countable, so count it.
      * `never_engaged` -- we have raised something and they have never spoken
                           (or we have never raised anything at all). Cold start
                           is the primary case and must not be silent.
      * `goal_soon`     -- an active goal with a deadline inside GOAL_SOON_DAYS.

    Deliberately not an LLM call: spending a model round-trip to compare two
    integers was the part of the old design that aged worst.

    `never_engaged` counts USER TURNS, not briefings delivered and not
    briefings read. A briefing that was opened and studied for two minutes with
    nothing said back is still someone who has never engaged, and treating
    reading as engagement here would be the reading-behaviour leak arriving by
    a side door -- it would change what we raise and how often, on the strength
    of a signal that does not carry it.
    """
    moment = now or datetime.now(UTC)

    behind = scope.behind_count(group.id)
    never_engaged = scope.user_turn_count(group.id) == 0

    goal = scope.active_goal(group.id)
    goal_soon = False
    if goal is not None:
        try:
            days_left = (goal.deadline_dt - moment).total_seconds() / 86400.0
        except (TypeError, ValueError):
            days_left = None
        if days_left is not None and days_left <= GOAL_SOON_DAYS:
            goal_soon = True

    return {"behind": behind, "never_engaged": never_engaged, "goal_soon": goal_soon}


def warrants_attention(need: dict[str, Any]) -> bool:
    """Any one of the three signals is enough to put a group on the shortlist."""
    return bool(need.get("behind")) or bool(need.get("never_engaged")) or bool(
        need.get("goal_soon")
    )


# --- Helpers ---------------------------------------------------------------


def _norm(terms: Iterable[str]) -> list[str]:
    """Normalise and de-duplicate concept terms, preserving first-seen order.

    The ledger keys on a lowercased, stripped term, so anything heading for it
    is normalised here rather than at four separate call sites.
    """
    seen: list[str] = []
    known: set[str] = set()
    for raw in terms or []:
        if not isinstance(raw, str):
            continue
        term = raw.strip().lower()
        if term and term not in known:
            known.add(term)
            seen.append(term)
    return seen


def _ledger_summary(scope: UserScope, group_id: str) -> dict[str, list[str]]:
    """The ledger as the briefing prompt needs to see it: terms grouped by state.

    Ordered most-recently-seen first (that is `scope.ledger`'s order) and
    capped, so a long-lived group does not push the actual news out of the
    prompt.
    """
    buckets: dict[str, list[str]] = {state: [] for state in config.CONCEPT_STATES}
    for concept in scope.ledger(group_id):
        bucket = buckets.setdefault(concept.state, [])
        if len(bucket) < LEDGER_TERMS_PER_STATE:
            bucket.append(concept.term)
    return buckets


def _known_terms(scope: UserScope, group_id: str) -> set[str]:
    return {c.term for c in scope.ledger(group_id) if c.is_known}


def _thread_context(scope: UserScope, exchange_id: str) -> list[dict[str, Any]]:
    """The thread as a prompt needs to see it: speaker and text, in order.

    Deliberately carries no reading behaviour. The turns are what was said; how
    long the briefing was looked at is a different fact, stored in a different
    place, and it must not travel with the conversation into any call whose
    output reaches the ledger.
    """
    return [
        {"seq": t.seq, "speaker": t.speaker, "text": t.text}
        for t in scope.thread_turns(exchange_id)
    ]


def _event_context(event: MonitorEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "headline": event.headline,
        "detail": event.detail,
        "occurred_at": event.occurred_at,
        "source_name": event.source_name,
        "source_url": event.source_url,
    }


class Assessor:
    def __init__(self, judge: Judge, client: Any = None, *, reply_search: bool = True):
        """
        `client` is the raw SDK client used for server-side web search; None
        means no search is possible (offline, or the harness's cheap runs).

        `reply_search` decides whether `respond` may go and look something up
        when neither the source event nor the briefing holds the answer. On by
        default, because the alternative -- "I don't have that", said to a user
        who asked a perfectly answerable question -- was the failure the first
        live run kept producing. Off is for cheap offline eval runs; with it off
        (or with no client) the reply says plainly that it could not look.
        """
        self.judge = judge
        self.client = client
        self.reply_search = reply_search

    # --- State the substance, and stop -------------------------------------

    def raise_topic(
        self,
        scope: UserScope,
        group: Group,
        event: MonitorEvent | None = None,
    ) -> Briefing | None:
        """Give the user the substance. Ask them nothing. Never tell it twice.

        Returns None when there is nothing new to say, and the caller says
        nothing. That is a normal outcome, not a failure: the human's rule
        (checkpoint 4) is that a story is never repeated and silence is the
        right response to an empty queue. The earlier version fell back to
        "recent material events" and then to "whatever is ongoing", which in
        a live run produced the same Rioja story five sessions running,
        reworded each time.

        `event` pins the briefing to one specific development; it is refused
        (None) if a briefing was already written from it. With no `event`, the
        single most material untold event is briefed -- exactly one, and its
        id is always recorded on the exchange, which is what keeps it out of
        the untold queue from then on.

        Nothing here opens a turn. The briefing is stored on the exchange, and
        the thread starts empty -- because the next thing said, if anything is
        said at all, is the user's.
        """
        if event is not None:
            # A pinned event is the caller's choice of subject; the only thing
            # checked here is that it has not been told already.
            if scope.event_told(event.id):
                return None
            events = [event]
        else:
            untold = scope.untold_events(group.id)
            if not untold:
                return None
            events = [untold[0]]

        ledger = _ledger_summary(scope, group.id)
        proficiency = scope.proficiency(group.id)
        goal = scope.active_goal(group.id)
        history = scope.exchange_history(group.id, limit=10)

        verdict = judgments.write_briefing(
            self.judge,
            group={"name": group.name, "description": group.description},
            ledger=ledger,
            proficiency=proficiency,
            # Depth per topic, not per person: the prompt locates the event in
            # the group's subdomains and pitches each part at the user's
            # familiarity there. Built from the ledger's existing evidence,
            # grouped by label -- nothing about reading travels in it.
            subdomain_familiarity=scope.subdomain_familiarity(group.id),
            events=[_event_context(e) for e in events],
            # Topic only. The old version passed the previous openers too,
            # which is now both meaningless and a way for a question to leak
            # back in as an example to imitate.
            previously_raised=[{"topic": e.topic} for e in history],
            active_goal=(
                {"description": goal.description, "deadline": goal.deadline}
                if goal
                else None
            ),
            behind_count=scope.behind_count(group.id),
            # How this person's skips have resolved so far. A prior for pitch
            # and glossing density, usable only once a few skips are resolved.
            reading_pattern=scope.reading_pattern(group.id),
            user_id=scope.user_id,
            group_id=group.id,
        )

        briefing_text = str(verdict.get("briefing", "")).strip()
        explained_terms = _norm(verdict.get("explained_terms", []))
        # Every domain term the briefing put in front of them, defined ones
        # included -- asking the model for this is far more reliable than
        # trying to recover terms from the prose afterwards.
        terms_used = _norm(list(verdict.get("terms_used", [])) + explained_terms)
        # Which slice each term belongs to, per the model. A missing or empty
        # map (an older verdict, the offline stub) is simply no labels.
        subdomains = judgments.subdomain_map(verdict.get("subdomains"))

        with scope.transaction():
            exchange_id = scope.open_exchange(
                group.id,
                briefing=briefing_text,
                topic=verdict.get("topic") or None,
                # Always exactly one event, always linked. The link is what
                # retires the event from `untold_events`, so a briefing that
                # forgot to record it would be a story told twice.
                event_id=events[0].id,
                explained_terms=explained_terms,
            )
            # Exposure only, here -- deliberately NOT `mark_explained`.
            #
            # We defined these terms unprompted, and at this moment we do not
            # know they have read a word of it. Promoting an unprompted
            # definition straight to "known" would re-create exactly the
            # failure the score had -- confidence manufactured out of our own
            # output rather than out of anything the user did. The defined
            # terms are stored on the exchange instead; `close_thread` credits
            # them once the reading signal says the briefing was looked at.
            #
            # The subdomain label rides along. It is a fact about the term
            # ("this is an appellation-rules term"), not about the user, so
            # storing it at exposure time breaks nothing: exposure still
            # promotes nothing, the label changes no state.
            if terms_used:
                scope.note_exposure(group.id, terms_used, subdomains=subdomains)

        return Briefing(
            exchange_id=exchange_id,
            topic=str(verdict.get("topic", "")),
            briefing=briefing_text,
            explained_terms=explained_terms,
            terms_used=terms_used,
        )

    # --- Answer what they asked --------------------------------------------

    def respond(
        self, scope: UserScope, group: Group, exchange_id: str, user_text: str
    ) -> Answer:
        """The user said something inside a thread. Answer it.

        This makes no assessment and writes nothing to the ledger. The user is
        mid-conversation; extracting evidence turn by turn would both waste
        calls and answer the wrong question, since what a thread reveals is
        visible only once it is whole. Evidence extraction happens exactly once,
        in `close_thread`.

        **`respond` records both turns itself: theirs, then ours.** Callers
        must not `add_turn` around it -- doing so double-counts, and every
        turn-based measure (user_turn_count, the evidence extractor's view of
        the thread, `never_engaged`) silently doubles with it. The ordering is
        deliberate: if the answering call fails, their question is still on the
        record and still reaches the evidence extractor, because what they
        chose to ask is true whether or not we managed to answer it.

        Where the answer comes from, in order:

        1. The source event the briefing was written from, when the exchange
           is linked to one. The briefing is a paraphrase and routinely drops
           specifics (a percentage, a date, a name) that the event detail
           holds. The first live run refused *"what potential alcohol were they
           picking at?"* with "I don't have a real figure" while "over 11%
           potential alcohol" sat in the event detail -- and the next briefing,
           built from the same event, stated it unprompted.
        2. The briefing itself.
        3. A search, when gap routing says the answer is not held and
           `reply_search` is on. Searching is allowed; inventing is not.
        4. A plain statement that we do not have it -- and, when a search was
           attempted, that we looked.
        """
        exchange = scope.get_exchange(exchange_id)
        if exchange is None:
            raise KeyError(f"No such exchange for this user: {exchange_id}")

        scope.add_turn(exchange_id, "user", user_text)

        group_ctx = {"name": group.name, "description": group.description}
        thread = _thread_context(scope, exchange_id)
        proficiency = scope.proficiency(group.id)
        # Per-slice familiarity, so the answer is pitched at the user's depth
        # in the subdomain the question falls in, not at the group band.
        familiarity = scope.subdomain_familiarity(group.id)

        # The source material. `get_event` is user-scoped, so a link can only
        # ever resolve to this user's own event.
        source_event: dict[str, Any] | None = None
        if exchange.event_id:
            event = scope.get_event(exchange.event_id)
            if event is not None:
                source_event = _event_context(event)

        # Gap routing, per question: can we answer this from what we hold, or
        # does it need a real source? A question is not a failure to be
        # remediated -- it is the most precise thing a user can hand us -- but
        # "answer it well" and "do not invent the answer" are the same
        # requirement here, and this is the call that decides which applies.
        # It sees the source event for the same reason the reply does: whether
        # we hold the answer depends on what we hold, not on what we said.
        try:
            routing = judgments.judge_gap_routing(
                self.judge,
                group=group_ctx,
                briefing=exchange.briefing,
                thread=thread,
                evidence={"asked_about": [], "not_understood": [], "understood": []},
                proficiency=proficiency,
                user_id=scope.user_id,
                group_id=group.id,
                source_event=source_event,
                subdomain_familiarity=familiarity,
            )
        except JudgmentError:
            routing = None

        supporting_source: dict[str, Any] | None = None
        lookup: dict[str, Any] | None = None
        if routing is not None and routing.get("gap_size") == "large":
            focus = routing.get("search_focus") or exchange.topic or user_text
            lookup = self._lookup(group, focus)
            if lookup.get("found"):
                supporting_source = {
                    "summary": lookup["summary"],
                    "source_url": lookup.get("source_url"),
                }

        try:
            verdict = judgments.write_thread_reply(
                self.judge,
                group=group_ctx,
                briefing=exchange.briefing,
                thread=thread,
                question=user_text,
                ledger=_ledger_summary(scope, group.id),
                proficiency=proficiency,
                supporting_source=supporting_source,
                user_id=scope.user_id,
                group_id=group.id,
                source_event=source_event,
                lookup=lookup,
                subdomain_familiarity=familiarity,
            )
        except JudgmentError:
            # Say so, rather than say something. A fabricated answer is worse
            # than an absent one because the user will repeat it.
            scope.add_turn(exchange_id, "system", NO_ANSWER_TEXT)
            return Answer(text=NO_ANSWER_TEXT, source_url=None)

        answer_text = str(verdict.get("answer", "")).strip()
        source_url = verdict.get("source_url") or (
            supporting_source.get("source_url") if supporting_source else None
        )

        with scope.transaction():
            scope.add_turn(exchange_id, "system", answer_text)
            # Our answer put terms in front of them too. Exposure only, for
            # exactly the same reason as the briefing: our own output is not
            # evidence about them.
            answer_terms = _norm(
                list(verdict.get("terms_used", []))
                + list(verdict.get("explained_terms", []))
            )
            if answer_terms:
                scope.note_exposure(group.id, answer_terms)

        return Answer(text=answer_text, source_url=source_url)

    # --- Read the whole thread, once it is quiet ---------------------------

    def close_thread(
        self, scope: UserScope, group: Group, exchange_id: str
    ) -> ThreadOutcome:
        """The user stopped. Extract what the whole thread revealed.

        Called when the conversation has gone quiet -- by the caller, on the
        user's behaviour, never on a judgment of ours that the conversation was
        complete. There is no such judgment point and there should not be: a
        model deciding a person is done talking would be wrong in the one
        direction that cannot be recovered from.

        A thread with no user turns is normal and cheap: it writes nothing to
        the ledger and makes no LLM call, because silence is not evidence in
        either direction. That was measured -- inferring understanding from
        unquestioned exposure was anti-predictive at every threshold tried.
        """
        exchange = scope.get_exchange(exchange_id)
        if exchange is None:
            raise KeyError(f"No such exchange for this user: {exchange_id}")

        proficiency_before = scope.proficiency(group.id)
        known_before = _known_terms(scope, group.id)
        turns = scope.thread_turns(exchange_id)
        user_turns = [t for t in turns if t.is_user]

        # The one place reading behaviour touches the ledger. Two conditions,
        # both required: the briefing DEFINED the term (not merely used it),
        # and the reading signal says the briefing was looked at. A missing
        # signal is treated as skipped -- absence of evidence of reading is
        # not evidence of reading.
        was_read = exchange.read_quality not in (None, "skipped")
        glossed = list(exchange.explained_terms) if was_read else []

        if not user_turns:
            with scope.transaction():
                if glossed:
                    scope.mark_read_explanation(
                        group.id,
                        glossed,
                        evidence=f"defined in a briefing they {exchange.read_quality}",
                    )
                scope.close_exchange(exchange_id, [], [], [], None)
            known_after = _known_terms(scope, group.id)
            return ThreadOutcome(
                understood=[],
                not_understood=[],
                asked_about=[],
                already_knew=None,
                newly_known=sorted(known_after - known_before),
                proficiency_before=proficiency_before,
                proficiency_after=scope.proficiency(group.id),
                turn_count=0,
            )

        # Concept evidence, over the WHOLE thread. Note what is not passed: the
        # exchange's dwell, scroll and read_quality. This is the only call whose
        # output writes to the ledger, so it is the one that must never see
        # reading behaviour -- if it could, "studied it for four minutes" would
        # start to look like evidence of understanding, which is exactly the
        # inference the ledger exists to refuse.
        try:
            evidence = judgments.judge_concept_evidence(
                self.judge,
                group={"name": group.name, "description": group.description},
                briefing=exchange.briefing,
                thread=_thread_context(scope, exchange_id),
                ledger_terms=_ledger_summary(scope, group.id),
                user_id=scope.user_id,
                group_id=group.id,
                # Existing slice labels, names only, so new labels converge on
                # them. No counts, no bands: this call writes to the ledger and
                # is given nothing derived from it beyond the term states.
                subdomain_labels=scope.subdomain_labels(group.id),
            )
        except JudgmentError:
            # Close it anyway, with no thread evidence recorded. An unread
            # thread is a lost observation; a guessed one is a corrupted
            # ledger. The reading credit does not depend on this call, so it
            # still applies.
            with scope.transaction():
                if glossed:
                    scope.mark_read_explanation(
                        group.id,
                        glossed,
                        evidence=f"defined in a briefing they {exchange.read_quality}",
                    )
                scope.close_exchange(exchange_id, [], [], [], None)
            known_after = _known_terms(scope, group.id)
            return ThreadOutcome(
                understood=[],
                not_understood=[],
                asked_about=[],
                already_knew=None,
                newly_known=sorted(known_after - known_before),
                proficiency_before=proficiency_before,
                proficiency_after=scope.proficiency(group.id),
                turn_count=len(user_turns),
            )

        understood = _norm(evidence.get("understood", []))
        not_understood = _norm(evidence.get("not_understood", []))
        asked_about = _norm(evidence.get("asked_about", []))
        raw_already_knew = evidence.get("already_knew")
        already_knew = None if raw_already_knew is None else bool(raw_already_knew)
        # Slice labels for the terms named above. Missing means no labels;
        # the store applies its own churn rule to whatever is here.
        subdomains = judgments.subdomain_map(evidence.get("subdomains"))

        # Ledger writes. Resolve overlaps first: the three lists come from one
        # model call and can name the same term twice, and `mark_*` overwrites
        # state unconditionally, so precedence has to be explicit rather than
        # dependent on call order.
        #
        #   asked_about wins    -- they told us directly, and we answered in
        #                          the thread, so `explained` is honest.
        #   then not_understood -- a revealed miss outranks an inferred grasp;
        #                          the cost of being wrong here is one extra
        #                          explanation, which is the cheap direction.
        #   then understood.
        asked_set = set(asked_about)
        missed = [t for t in not_understood if t not in asked_set]
        missed_set = set(missed)
        got = [t for t in understood if t not in asked_set and t not in missed_set]

        # Did we actually answer anything? `explained` means "they asked and we
        # explained"; claiming it when every reply was the could-not-answer
        # fallback would be a lie the system never revisits. This is a
        # thread-level check: one real answer is taken to cover the terms the
        # thread asked about.
        answered = any(
            t.speaker == "system" and t.text != NO_ANSWER_TEXT for t in turns
        )

        # A gloss they then asked about did not land, and a gloss on a term
        # they got wrong plainly did not either -- neither earns a read credit.
        # Terms they asked about get their credit through `mark_explained`.
        # Compared on the ledger's canonical form, so "ETF flows" asked about
        # and "spot ETF flows" glossed resolve to the same row and are not
        # double-credited.
        excluded = {
            scope.canonical_term(group.id, t) for t in (*asked_set, *missed_set)
        }
        read_credit = [
            t for t in glossed if scope.canonical_term(group.id, t) not in excluded
        ]

        with scope.transaction():
            if read_credit:
                scope.mark_read_explanation(
                    group.id,
                    read_credit,
                    evidence=f"defined in a briefing they {exchange.read_quality}",
                )
            if got:
                scope.mark_correct_use(
                    group.id,
                    got,
                    evidence="used correctly in their own words",
                    subdomains=subdomains,
                )
            if missed:
                scope.mark_unknown(
                    group.id,
                    missed,
                    evidence="thread revealed a misunderstanding",
                    subdomains=subdomains,
                )
            if asked_about:
                if answered:
                    scope.mark_explained(
                        group.id,
                        asked_about,
                        evidence="asked about it; we explained",
                        subdomains=subdomains,
                    )
                else:
                    scope.mark_unknown(
                        group.id,
                        asked_about,
                        evidence="asked about it; no explanation given yet",
                        subdomains=subdomains,
                        misunderstood=False,
                    )
            scope.close_exchange(
                exchange_id, understood, not_understood, asked_about, already_knew
            )
            # What did earlier SKIPS in this group mean? Now that this thread
            # has produced evidence about specific terms, any skipped briefing
            # that defined those terms gets labelled: informed if they turned
            # out to hold them, lazy if they turned out not to. This reads the
            # ledger's evidence; it writes none. A skip with no later evidence
            # stays unresolved -- silence still says nothing.
            scope.resolve_skips(
                group.id,
                known_now=got,
                lacking_now=[*missed, *asked_about],
            )
            # Events are deliberately not marked engaged here. `engaged_at`
            # is written only by the eval harness; switching it on for real
            # users is a measurement change to make on purpose.

        known_after = _known_terms(scope, group.id)
        return ThreadOutcome(
            understood=understood,
            not_understood=not_understood,
            asked_about=asked_about,
            already_knew=already_knew,
            newly_known=sorted(known_after - known_before),
            proficiency_before=proficiency_before,
            proficiency_after=scope.proficiency(group.id),
            turn_count=len(user_turns),
        )

    # --- Large gaps: find a real resource, never invent one -----------------

    def _lookup(self, group: Group, focus: str) -> dict[str, Any]:
        """Go and look, when the answer is not held. Never invent.

        Returns a structured outcome rather than a bare summary, because the
        reply has to tell the user the truth about what happened, and there are
        four different truths: we found something (and here is where it came
        from), we looked and found nothing, we could not look, and we were told
        not to look. The old version folded the last three into a sentence
        posing as a source, which is how "search isn't available" ended up
        being quoted back at users as if it were a fact about the world.

            attempted   -- a search actually ran
            found       -- it produced a usable source
            summary     -- headline + detail of the best hit, when found
            source_url  -- the hit's URL, when found; never constructed
            note        -- why nothing was found / attempted, for the prompt
        """
        if not self.reply_search:
            return {
                "attempted": False,
                "found": False,
                "summary": None,
                "source_url": None,
                "note": "Search is disabled for this session.",
            }
        if self.client is None:
            return {
                "attempted": False,
                "found": False,
                "summary": None,
                "source_url": None,
                "note": "Search isn't available in this session (no client).",
            }
        try:
            found = search.discover_events(
                self.judge,
                self.client,
                group,
                recent_events=[],
                remediation_focus=focus,
            )
        except Exception as exc:
            # Broad on purpose: this is plumbing around a network call, and a
            # search failure must not fail the reply.
            return {
                "attempted": True,
                "found": False,
                "summary": None,
                "source_url": None,
                "note": f"Couldn't retrieve a source ({exc}).",
            }

        events = found.get("events", [])
        if not events:
            return {
                "attempted": True,
                "found": False,
                "summary": None,
                "source_url": None,
                "note": found.get("note") or "Search found no reliable source.",
            }
        best = max(events, key=lambda e: e.get("confidence", 0))
        summary = f"{best.get('headline', '')} — {best.get('detail', '')}".strip(" —")
        return {
            "attempted": True,
            "found": bool(summary),
            "summary": summary or None,
            "source_url": best.get("source_url"),
            "source_name": best.get("source_name"),
            "note": "",
        }
