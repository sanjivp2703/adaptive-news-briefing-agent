"""One typed wrapper per judgment point.

Each function here is a pure judgment: it takes an explicit context dict,
returns a structured `Verdict`, and has no side effects beyond the trace row
the `Judge` writes. Keeping them pure is what lets the eval harness call any
one of them in isolation with a fixed input and score the result.

The schemas are written out explicitly rather than generated, because the
schema *is* the contract the eval harness scores against -- it should be
visible and reviewable, not inferred from a type annotation.
"""

from __future__ import annotations

from typing import Any

from . import config
from .judgment import Judge, Verdict, schema

# --- Schemas ---------------------------------------------------------------

MATERIALITY_SCHEMA = schema(
    {
        "is_material": {"type": "boolean"},
        "materiality_score": {"type": "number", "minimum": 0, "maximum": 100},
    },
    ["is_material", "materiality_score"],
)

# Subdomain labels for the terms a call names: which slice of the group each
# belongs to (`appellation rules`, `funding mechanics`). In-process this is a
# {term: subdomain} map, and every consumer (`subdomain_map` below, the store)
# treats it as one. On the wire it is a list of {term, subdomain} pairs,
# because the structured-output API requires `additionalProperties: false` on
# every object and rejects any other value -- a free-form map cannot be
# expressed in a strict schema. `subdomain_map` converts either shape.
#
# Listed in `required` so a live call always produces it (an empty list is a
# valid answer); code still treats a missing key as {} so an older verdict, or
# a stub that omits it, is fine.
_SUBDOMAIN_PAIRS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "term": {"type": "string"},
            "subdomain": {"type": "string"},
        },
        "required": ["term", "subdomain"],
        "additionalProperties": False,
    },
}


def subdomain_map(raw: Any) -> dict[str, str]:
    """{term: subdomain} from a verdict's `subdomains`, whatever its shape.

    Accepts the wire form (a list of {term, subdomain} objects), the natural
    form (a dict), or nothing at all -- a verdict without the key, a stub that
    never heard of it, an old log row -- which is {}. Keys are lowercased and
    stripped to match the term normalisation applied everywhere else; the
    store canonicalises the labels themselves.
    """
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = (
            (entry.get("term"), entry.get("subdomain"))
            for entry in raw
            if isinstance(entry, dict)
        )
    else:
        return out
    for term, label in items:
        if not isinstance(term, str) or not isinstance(label, str):
            continue
        key = term.strip().lower()
        if key and label.strip():
            out[key] = label
    return out


# Evidence extracted from a reply -- deliberately NOT a grade. Each field is
# an observation about specific concepts, which is what the ledger stores; a
# single number would have to be un-inferred again before it could be used.
CONCEPT_EVIDENCE_SCHEMA = schema(
    {
        "understood": {"type": "array", "items": {"type": "string"}},
        "not_understood": {"type": "array", "items": {"type": "string"}},
        "asked_about": {"type": "array", "items": {"type": "string"}},
        # Nullable: plenty of replies simply do not say either way, and
        # guessing false there is a silent, uncorrected error.
        "already_knew": {"type": ["boolean", "null"]},
        # One label per term named in the three lists above. A fact about the
        # term, so it rides along with the evidence rather than being a fourth
        # kind of evidence.
        "subdomains": _SUBDOMAIN_PAIRS,
    },
    ["understood", "not_understood", "asked_about", "already_knew", "subdomains"],
)

GAP_ROUTING_SCHEMA = schema(
    {
        "gap_size": {"type": "string", "enum": ["none", "small", "large"]},
        "explanation": {"type": "string"},
        "search_focus": {"type": "string"},
    },
    ["gap_size", "explanation", "search_focus"],
)

INTERRUPT_TIMING_SCHEMA = schema(
    {
        "should_surface": {"type": "boolean"},
        "event_ids": {"type": "array", "items": {"type": "string"}},
        "raise_topic": {"type": "boolean"},
        "framing": {"type": "string"},
        "held_back_count": {"type": "integer", "minimum": 0},
    },
    ["should_surface", "event_ids", "raise_topic", "framing", "held_back_count"],
)

QUERY_FORMULATION_SCHEMA = schema(
    {
        "queries": {"type": "array", "items": {"type": "string"}},
        "expected_signals": {"type": "string"},
        "domain_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    ["queries", "expected_signals", "domain_confidence"],
)

SOURCE_SELECTION_SCHEMA = schema(
    {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "detail": {"type": "string"},
                    "source_url": {"type": "string"},
                    "source_name": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 100},
                },
                "required": [
                    "headline",
                    "detail",
                    "source_url",
                    "source_name",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "excluded_count": {"type": "integer", "minimum": 0},
        "exclusion_notes": {"type": "string"},
    },
    ["events", "excluded_count", "exclusion_notes"],
)

# The briefing, and nothing else. `briefing` is required and non-empty by
# contract: the substance is the product, and withholding it to test the user
# is what made the previous design unanswerable for a beginner.
#
# Note what is ABSENT and must stay absent: there is no `opener` field and no
# `reference_answer` field. The schema is the enforcement point. A prompt rule
# saying "do not ask a question" can be softened by the next editor; a schema
# with nowhere to put a question cannot quietly grow one back, and a
# `reference_answer` field only exists to score a reply against an expected
# one, which is the testing posture the whole redesign removed.
BRIEFING_SCHEMA = schema(
    {
        "briefing": {"type": "string"},
        "topic": {"type": "string"},
        # Every domain term the briefing puts in front of the user. Asked for
        # explicitly because the exposure ledger needs it, and recovering terms
        # from the prose afterwards is guesswork.
        "terms_used": {"type": "array", "items": {"type": "string"}},
        # The subset of `terms_used` that was defined inline because the ledger
        # marked it unknown.
        "explained_terms": {"type": "array", "items": {"type": "string"}},
        # One label per term in `terms_used` / `explained_terms`: which slice
        # of the group it belongs to. Stored on the concept row at exposure
        # time, so the slice a term sits in is known before the user has said
        # anything about it -- which is what lets a later readout say "she is
        # fluent in the slice this new term belongs to".
        "subdomains": _SUBDOMAIN_PAIRS,
    },
    ["briefing", "topic", "terms_used", "explained_terms", "subdomains"],
)

# One answer to one question the user asked. `source_url` is nullable and must
# stay nullable: the honest outcome for "I don't have a reliable source for
# that" is an answer that says so, not a plausible-looking URL.
THREAD_REPLY_SCHEMA = schema(
    {
        "answer": {"type": "string"},
        "source_url": {"type": ["string", "null"]},
        # Terms the answer puts in front of the user, and the subset defined
        # inline -- same contract as the briefing, because an answer is just as
        # capable of introducing unexplained vocabulary as a briefing is.
        "terms_used": {"type": "array", "items": {"type": "string"}},
        "explained_terms": {"type": "array", "items": {"type": "string"}},
    },
    ["answer", "source_url", "terms_used", "explained_terms"],
)


# --- Judgment calls --------------------------------------------------------


def judge_materiality(
    judge: Judge,
    *,
    group: dict[str, Any],
    candidate_event: dict[str, Any],
    recent_events: list[dict[str, Any]],
    user_id: str,
    group_id: str,
) -> Verdict:
    """would a conversant person be expected to know this?"""
    return judge(
        config.MATERIALITY,
        context={
            "group": group,
            "candidate_event": candidate_event,
            "recent_events": recent_events,
        },
        schema=MATERIALITY_SCHEMA,
        user_id=user_id,
        group_id=group_id,
    )


def judge_concept_evidence(
    judge: Judge,
    *,
    group: dict[str, Any],
    briefing: str | None,
    thread: list[dict[str, Any]],
    ledger_terms: dict[str, list[str]],
    user_id: str,
    group_id: str,
    subdomain_labels: list[str] | None = None,
) -> Verdict:
    """what does this whole thread reveal about what they hold?

    Reads the entire conversation, not a single reply. The unit of evidence is
    the thread because the richest signal in it -- what the user chose to ask --
    only exists across turns: a first question locates where their knowledge
    stops, and whether they ask a second, and what it is, says more than either
    question alone.

    Deliberately NOT given the reading behaviour on the exchange. Dwell and
    scroll are attention, never comprehension, and this is the one call whose
    output writes to the ledger, so it is the one call that must never see
    them.

    `subdomain_labels` is the list of slice labels already in use for this
    user and group -- names only, no counts and no bands -- so that the labels
    this call assigns converge on the existing ones rather than forking.
    """
    return judge(
        config.CONCEPT_EVIDENCE,
        context={
            "group": group,
            "briefing_we_gave": briefing,
            "thread": thread,
            "ledger": ledger_terms,
            "subdomain_labels": list(subdomain_labels or []),
        },
        schema=CONCEPT_EVIDENCE_SCHEMA,
        user_id=user_id,
        group_id=group_id,
    )


def judge_gap_routing(
    judge: Judge,
    *,
    group: dict[str, Any],
    briefing: str | None,
    thread: list[dict[str, Any]],
    evidence: dict[str, Any],
    proficiency: str,
    user_id: str,
    group_id: str,
    source_event: dict[str, Any] | None = None,
    subdomain_familiarity: dict[str, dict[str, Any]] | None = None,
) -> Verdict:
    """answer this from what you know, or go find a real source?

    Called per user question, inside a live thread, so it sees the turns so far
    rather than one reply. Keys off the concept evidence (what was asked, what
    was missed) rather than a grade, so the routing decision names the actual
    concept it is trying to close.

    `source_event` is the monitor event the briefing was written from, when
    the exchange is linked to one. The router must see it: "does this need a
    search" depends on what we already hold, and the event detail routinely
    holds specifics the briefing left out.

    `subdomain_familiarity` is the per-slice known/attested/band readout, so
    the `explanation` sketch is pitched at the user's depth in the slice the
    question falls in rather than at the whole-group band.
    """
    return judge(
        config.GAP_ROUTING,
        context={
            "group": group,
            "briefing_we_gave": briefing,
            "source_event": source_event,
            "thread": thread,
            "concept_evidence": evidence,
            "user_proficiency": proficiency,
            "subdomain_familiarity": subdomain_familiarity or {},
        },
        schema=GAP_ROUTING_SCHEMA,
        user_id=user_id,
        group_id=group_id,
    )


def judge_interrupt_timing(
    judge: Judge,
    *,
    groups_summary: list[dict[str, Any]],
    pending_events: list[dict[str, Any]],
    groups_needing_attention: list[dict[str, Any]],
    hours_since_last_session: float | None,
    recently_surfaced: list[dict[str, Any]],
    user_id: str,
) -> Verdict:
    """what actually reaches the user this session?"""
    return judge(
        config.INTERRUPT_TIMING,
        context={
            "groups": groups_summary,
            "pending_events": pending_events,
            "groups_needing_attention": groups_needing_attention,
            "hours_since_last_session": hours_since_last_session,
            "recently_surfaced": recently_surfaced,
        },
        schema=INTERRUPT_TIMING_SCHEMA,
        user_id=user_id,
    )


def judge_query_formulation(
    judge: Judge,
    *,
    group: dict[str, Any],
    recent_events: list[dict[str, Any]],
    hours_since_poll: float | None,
    remediation_focus: str | None,
    user_id: str,
    group_id: str,
) -> Verdict:
    """what should we actually search for?"""
    return judge(
        config.QUERY_FORMULATION,
        context={
            "group": group,
            "already_recorded_events": recent_events,
            "hours_since_last_poll": hours_since_poll,
            "remediation_focus": remediation_focus,
        },
        schema=QUERY_FORMULATION_SCHEMA,
        user_id=user_id,
        group_id=group_id,
    )


def judge_source_selection(
    judge: Judge,
    *,
    group: dict[str, Any],
    expected_signals: str,
    domain_confidence: str,
    results: list[dict[str, Any]],
    user_id: str,
    group_id: str,
) -> Verdict:
    """which of these results do we actually trust?"""
    return judge(
        config.SOURCE_SELECTION,
        context={
            "group": group,
            "expected_signals": expected_signals,
            "domain_confidence": domain_confidence,
            "search_results": results,
        },
        schema=SOURCE_SELECTION_SCHEMA,
        user_id=user_id,
        group_id=group_id,
    )


def write_briefing(
    judge: Judge,
    *,
    group: dict[str, Any],
    ledger: dict[str, list[str]],
    proficiency: str,
    events: list[dict[str, Any]],
    previously_raised: list[dict[str, Any]],
    active_goal: dict[str, Any] | None,
    behind_count: int,
    user_id: str,
    group_id: str,
    reading_pattern: dict[str, Any] | None = None,
    subdomain_familiarity: dict[str, dict[str, Any]] | None = None,
) -> Verdict:
    """Routed LLM call (not a scored judgment point) -- state the substance.

    It gives the news plainly, pitched at the user's band and defining whatever
    the ledger marks unknown, and then **stops**. It asks nothing.

    The previous version of this call ended every briefing with a question we
    had chosen, and a question we choose necessarily presupposes what we think
    the user knows -- *"does a harvest running this far ahead of normal sound
    like good news for the wine, or more like a warning sign to you?"* is
    unanswerable unless you already know what fast ripening does to wine. That
    was still a test, administered politely. What the user chooses to ask
    instead is both kinder and strictly better evidence, and it costs us
    nothing but the question.

    `subdomain_familiarity` is the per-slice readout ({subdomain: {known,
    attested, band}}) that lets the briefing pitch depth per topic rather than
    per person. The event's subdomain is NOT pre-computed here; the prompt
    locates the event in the group's slices itself.
    """
    return judge(
        config.BRIEFING,
        context={
            "group": group,
            "concept_ledger": ledger,
            "proficiency": proficiency,
            "subdomain_familiarity": subdomain_familiarity or {},
            "events": events,
            "previously_raised": previously_raised,
            "active_goal": active_goal,
            "events_not_yet_seen": behind_count,
            # Counts and a smoothed share only -- never dwell, scroll or
            # read_quality themselves. The briefing may know that this person's
            # skips tend to prove informed; it may not see how long they looked.
            "reading_pattern": reading_pattern or {},
        },
        schema=BRIEFING_SCHEMA,
        user_id=user_id,
        group_id=group_id,
    )


def write_thread_reply(
    judge: Judge,
    *,
    group: dict[str, Any],
    briefing: str | None,
    thread: list[dict[str, Any]],
    question: str,
    ledger: dict[str, list[str]],
    proficiency: str,
    supporting_source: dict[str, Any] | None,
    user_id: str,
    group_id: str,
    source_event: dict[str, Any] | None = None,
    lookup: dict[str, Any] | None = None,
    subdomain_familiarity: dict[str, dict[str, Any]] | None = None,
) -> Verdict:
    """Routed LLM call (not a scored judgment point) -- answer what was asked.

    Three tiers of material, in the order the prompt is told to consult them:

    * `source_event` -- the monitor event the briefing was written from
      (headline, detail, source). This is the fix for the first live run, where
      the reply refused a figure that was in the event detail and absent from
      the briefing: the call had only ever seen the paraphrase.
    * `briefing_we_gave` -- what the user actually read.
    * `supporting_source` -- a real retrieved source, when gap routing decided
      the question needed one and the search found something. `lookup` says
      whether a search was attempted at all and what came of it, so the reply
      can say "I looked and found nothing" rather than a bare "I don't have
      that" -- those are different statements and the user deserves the
      accurate one.

    When none of them holds the answer the prompt's instruction stands: say
    plainly that you do not know, rather than producing something plausible.
    """
    return judge(
        config.THREAD_REPLY,
        context={
            "group": group,
            "source_event": source_event,
            "briefing_we_gave": briefing,
            "thread": thread,
            "question": question,
            "concept_ledger": ledger,
            "proficiency": proficiency,
            "subdomain_familiarity": subdomain_familiarity or {},
            "supporting_source": supporting_source,
            "lookup": lookup,
        },
        schema=THREAD_REPLY_SCHEMA,
        user_id=user_id,
        group_id=group_id,
    )
