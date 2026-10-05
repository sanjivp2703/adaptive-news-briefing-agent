"""Configuration: model routing, paths, and tunable knobs.

Model routing is a first-class, per-judgment-point knob (not a single global
model) so the eval harness can measure whether a given judgment point actually
needs a stronger model, and only that point gets upgraded.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Model IDs -------------------------------------------------------------

OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"
# Not routed to any judgment point. The eval harness's cheap regression mode
# voices the *persona* with it (harness/config.py); nothing in `src/` uses it.
HAIKU = "claude-haiku-4-5-20251001"

# USD per million tokens, (input, output), first-party rates as of 2026-06.
# The judgment log stores uncached input and output token counts only, so a
# cost derived from this table understates a run that wrote cache and
# overstates one that read it. A model absent from the table has unknown cost;
# callers show tokens and say so rather than guess a price.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    OPUS: (5.00, 25.00),
    SONNET: (2.00, 10.00),
    HAIKU: (1.00, 5.00),
}

# --- The judgment points --------------------------------------------------
# Six scored judgment points: the spec's five, with source/query selection
# split into its two halves. These names are the canonical identifiers used in
# the judgment log, the prompt filenames, the MODEL_/EFFORT_ env overrides and
# the eval harness metrics. Keep the strings stable: renaming one orphans its
# historical log rows.

MATERIALITY = "materiality"
GAP_ROUTING = "gap_routing"
INTERRUPT_TIMING = "interrupt_timing"
QUERY_FORMULATION = "query_formulation"
SOURCE_SELECTION = "source_selection"
# Reads a whole thread for which concepts the user holds, missed, or asked
# about. The only call whose output writes to the ledger.
CONCEPT_EVIDENCE = "concept_evidence"

ALL_JUDGMENT_POINTS = (
    MATERIALITY,
    GAP_ROUTING,
    INTERRUPT_TIMING,
    QUERY_FORMULATION,
    SOURCE_SELECTION,
    CONCEPT_EVIDENCE,
)

# The two generative calls. Both get the same routing, versioned prompt and
# tracing as everything else, but neither is a scored judgment point: writing
# quality is observed through the harness rather than scored against labels.
#
# `briefing` states the substance of one event and stops. It asks nothing.
BRIEFING = "briefing"

# Answering a question the user asked inside a thread. This is the call that
# only exists because the system stopped asking: the user's own question is now
# the thing being responded to, and it is the best signal in the system.
THREAD_REPLY = "thread_reply"

GENERATIVE_CALLS = (BRIEFING, THREAD_REPLY)

# Everything with a prompt file and a model route, scored or not.
ROUTED_CALLS = ALL_JUDGMENT_POINTS + GENERATIVE_CALLS

# Per-judgment-point model routing.
#
# Opus for the two calls where a wrong answer is most costly and the reasoning
# is least mechanical: materiality (a false negative is the exact embarrassment
# the product exists to prevent) and the two halves of source/query selection
# (which is what lets the system work for a group nobody anticipated).
#
# Sonnet for the three that operate on bounded, more structured context.
#
# Override any of these with an env var, e.g. MODEL_MATERIALITY=claude-sonnet-5,
# which is how the eval harness runs A/B comparisons without a code change.
_DEFAULT_MODELS = {
    MATERIALITY: OPUS,
    QUERY_FORMULATION: OPUS,
    SOURCE_SELECTION: OPUS,
    # On Opus because its errors propagate: it is the only call that writes
    # to the ledger, and every measurement downstream reads the ledger.
    CONCEPT_EVIDENCE: OPUS,
    GAP_ROUTING: SONNET,
    INTERRUPT_TIMING: SONNET,
    BRIEFING: SONNET,
    THREAD_REPLY: SONNET,
}


def model_for(judgment_point: str) -> str:
    """Resolve the model for a judgment point, honouring env-var overrides.

    A point routed to the local seat (see `local_model_points`) reports the
    local model's name, because that is who actually answers: the judgment
    log, `cli models` and the harness config block all read this function,
    and each must name the model that wrote the row.
    """
    if judgment_point not in _DEFAULT_MODELS:
        raise KeyError(
            f"Unknown judgment point {judgment_point!r}; "
            f"expected one of {sorted(_DEFAULT_MODELS)}"
        )
    if judgment_point in local_model_points():
        return local_model_name()
    env_key = f"MODEL_{judgment_point.upper()}"
    return os.environ.get(env_key) or _DEFAULT_MODELS[judgment_point]


# --- Local / open-model seat -----------------------------------------------
# The v2 training track puts a distilled model in the briefing seat. Three
# env vars, read here and in `local_client.py`, and nowhere else. With
# LOCAL_MODEL_BASE_URL unset, nothing below changes any behaviour.

LOCAL_MODEL_BASE_URL_ENV = "LOCAL_MODEL_BASE_URL"  # e.g. http://localhost:11434/v1
LOCAL_MODEL_NAME_ENV = "LOCAL_MODEL_NAME"  # what the server calls the model
LOCAL_MODEL_API_KEY_ENV = "LOCAL_MODEL_API_KEY"  # optional bearer token
LOCAL_MODEL_POINTS_ENV = "LOCAL_MODEL_POINTS"  # comma list; default: briefing
LOCAL_MODEL_DEFAULT_POINTS = (BRIEFING,)


def local_model_enabled() -> bool:
    return bool(os.environ.get(LOCAL_MODEL_BASE_URL_ENV))


def local_model_name() -> str:
    return os.environ.get(LOCAL_MODEL_NAME_ENV) or "local-model"


def local_model_points() -> frozenset[str]:
    """Routed calls the local seat answers. Empty unless the seat is enabled."""
    if not local_model_enabled():
        return frozenset()
    raw = os.environ.get(LOCAL_MODEL_POINTS_ENV)
    if raw is None:
        return frozenset(LOCAL_MODEL_DEFAULT_POINTS)
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


# Effort per judgment point. Lower effort is cheaper and faster; these are
# bounded classification-shaped calls, so "medium" is the starting point and
# the eval harness is what justifies raising it.
_DEFAULT_EFFORT = {
    MATERIALITY: "high",
    QUERY_FORMULATION: "medium",
    SOURCE_SELECTION: "medium",
    CONCEPT_EVIDENCE: "high",
    GAP_ROUTING: "low",
    INTERRUPT_TIMING: "medium",
    BRIEFING: "medium",
    # Answering a real question the user asked, in their thread, with the
    # ledger and the turns so far in front of it. Bounded and conversational --
    # the expensive part of getting this right is the prompt, not the budget.
    THREAD_REPLY: "medium",
}


def effort_for(judgment_point: str) -> str:
    env_key = f"EFFORT_{judgment_point.upper()}"
    return os.environ.get(env_key) or _DEFAULT_EFFORT[judgment_point]


# --- Knowledge model -------------------------------------------------------
#
# Knowledge is a ledger of concepts with the evidence behind each, NOT a score.
# Two things were previously fused and are now separated:
#
#   concepts/terminology -- do not decay. Once known, known.
#   current events       -- do not decay either; the user simply has not been
#                           told yet, which is countable from the event queue.
#
# So there is no level, no decay rate, and no decay-due judgment call.

# Concept states, weakest to strongest evidence.
CONCEPT_UNKNOWN = "unknown"      # default, or reverted here on a misunderstanding
CONCEPT_PROVISIONAL = "provisional"  # they used it correctly once; awaiting a second use
CONCEPT_FAMILIAR = "familiar"    # we defined it in a briefing and they read that briefing
CONCEPT_EXPLAINED = "explained"  # they asked, we explained
CONCEPT_CONFIRMED = "confirmed"  # they used it correctly themselves, twice

CONCEPT_STATES = (
    CONCEPT_UNKNOWN,
    CONCEPT_PROVISIONAL,
    CONCEPT_FAMILIAR,
    CONCEPT_EXPLAINED,
    CONCEPT_CONFIRMED,
)
# Terms we will not gloss again when writing a briefing. One correct use, or
# one read explanation, is enough to stop defining a term back to the person.
KNOWN_STATES = (
    CONCEPT_PROVISIONAL,
    CONCEPT_FAMILIAR,
    CONCEPT_EXPLAINED,
    CONCEPT_CONFIRMED,
)

# Display order (strongest evidence first) and a one-line reading of each
# state, shared by the CLI and the web console so neither can drop a state.
STATES_STRONGEST_FIRST = tuple(reversed(CONCEPT_STATES))
STATE_GLOSS = {
    CONCEPT_CONFIRMED: "used it correctly themselves, twice",
    CONCEPT_EXPLAINED: "they asked, and we explained it",
    CONCEPT_FAMILIAR: "we defined it in a briefing they read",
    CONCEPT_PROVISIONAL: "used correctly once; awaiting a second use",
    CONCEPT_UNKNOWN: "not known, or reverted after a misunderstanding",
}

# How many terms per state a prompt is shown from the ledger.
LEDGER_TERMS_PER_STATE = 40

# States that CAN count toward the derived proficiency band. `provisional` is
# excluded: one correct use is roughly a 0.43 posterior, and the briefing
# handed the user the term moments earlier, so echoing it back may be parroting.
#
# `confirmed` always counts. `familiar` and `explained` count only once the
# term has been explained-and-read at least READ_EXPLANATIONS_BEFORE_BAND
# times -- see `UserScope.proficiency`. A single explanation is enough to stop
# re-glossing, not enough to move the band.
BAND_STATES = (CONCEPT_FAMILIAR, CONCEPT_EXPLAINED, CONCEPT_CONFIRMED)

# `familiar` is the ONE sanctioned path from reading behaviour into the ledger,
# and it is deliberately narrow. It needs two positive acts -- we defined the
# term in a briefing, AND the user read or skimmed that briefing (read_quality
# other than `skipped`) -- and it is applied at thread close, never by
# `record_reading` itself. It is not the deleted `assumed` state coming back:
# `assumed` was silence after mere usage, which measured anti-predictive.
# `familiar` requires an explanation to have been in front of them and the
# text to have been looked at. Human decision, checkpoint 3: "we explained and
# they read or skimmed should count as they have familiarity; over time this
# will grow into more confidence."
#
# "Over time" is this threshold. Each explanation they read -- a gloss in a
# read briefing, or an answer to their own question -- increments
# `read_explanations`. At this many, the term counts toward the band. It
# mirrors CORRECT_USES_BEFORE_CONFIRMED: one signal changes how we write to
# them, two change what we believe about them. The harness measures
# P(known | familiar) split by this count, exactly the test that killed
# `assumed`; if the 2+ bucket does not beat the 1 bucket, this threshold rises.
READ_EXPLANATIONS_BEFORE_BAND = 2

# A single correct unprompted use is real but weak evidence: under standard
# guess/slip assumptions it lands near a 0.43 posterior, and here there is a
# specific confound -- we just used the term in the briefing, so echoing it
# back may be parroting rather than knowing. Two independent uses required.
CORRECT_USES_BEFORE_CONFIRMED = 2

# Exposure is COUNTED but never promotes a concept to anything.
#
# The original design promoted a term to `assumed` after three unquestioned
# exposures. The harness measured that inference directly against persona
# ground truth and it was anti-predictive at every threshold tried (1/2/3/5/8):
# lift -0.34 at one exposure, -1.00 at three and five. Raising the bar only
# made it fire less often, never more accurately. The literature predicted
# this -- question-asking runs about 0.11 per student per hour regardless of
# understanding (Graesser & Person 1994), so silence is the likely outcome
# under both hypotheses and separates neither.
#
# The count is still worth keeping: it tells the opener whether a term has been
# put in front of the user before, which is useful for phrasing. It just is not
# evidence about what they know.

# Proficiency is a coarse band DERIVED from the ledger -- never stored, never
# decayed. It is consulted only as a prior when there is no evidence about a
# specific term, so being slightly wrong costs one extra explanation or one
# clarifying question. Everyone starts at beginner.
BEGINNER = "beginner"
DEVELOPING = "developing"
CONVERSANT = "conversant"
FLUENT = "fluent"


def proficiency_band(known: int, encountered: int) -> str:
    """Derive the band from ledger counts.

    Requires both a floor on absolute concepts known and a ratio, so someone
    who has met three terms and knows all three is not called fluent.
    """
    if encountered == 0 or known < 3:
        return BEGINNER
    ratio = known / encountered
    if known >= 25 and ratio >= 0.8:
        return FLUENT
    if known >= 12 and ratio >= 0.6:
        return CONVERSANT
    if known >= 3 and ratio >= 0.35:
        return DEVELOPING
    return BEGINNER


# --- Subdomains: where inside the group the user is at home ----------------
#
# Human direction (2026-09-14): "the glossary shouldn't be an exact word to
# word matching they should cover the subjects subdomains within the group
# that the user is familiar with and use that among other factors to decide
# how in depth the harness needs to explain a certain topic for the user to
# understand."
#
# Each ledger term carries an optional `subdomain` label -- a short heading a
# specialist would file it under (`viticulture & harvest`, `appellation
# rules`, `funding mechanics`). The label is assigned by the same calls that
# already name the term, from the same evidence; nothing new is inferred
# about the user. What it adds is a way to AGGREGATE: the per-term ledger
# cannot know that a viticulturist will hold a viticulture term she has never
# been observed using, but the subdomain's known/attested counts can say she
# is fluent in that slice, and the briefing can pitch that part accordingly.
#
# The label is a fact about the term, not about the person, so it lives on the
# concept row. It never changes a state, never moves the group band, and is
# never shown to the user.

# The bucket for terms that have not been labelled yet. Reported alongside
# the real subdomains so the readout is complete, but a term here counts
# toward no other subdomain -- an unlabelled term must not inflate or dilute
# a slice it was never assigned to.
UNLABELLED_SUBDOMAIN = "(unlabelled)"

# Floors for the per-subdomain band. Same three thresholds as the group band
# (absolute known, then known/attested ratio), set lower because a subdomain
# is a slice of the group's vocabulary, not the whole of it.
#
# The group floors (3 / 12 / 25 known) were chosen so that a whole-group
# ledger cannot read as conversant off a handful of terms. A subdomain the
# size of `appellation rules` may only ever surface eight or ten distinct
# terms across months of briefings; under the group floors it could never
# reach `conversant`, however completely the user held it, and the label
# would carry no information at exactly the moment it is meant to -- when
# an expert's fluency is concentrated in one corner of the group. The ratio
# floors are HIGHER than the group's for the same reason: with few terms in
# the denominator, a lower ratio would let two lucky confirmations dominate.
SUBDOMAIN_DEVELOPING_MIN_KNOWN = 2
SUBDOMAIN_DEVELOPING_MIN_RATIO = 0.5
SUBDOMAIN_CONVERSANT_MIN_KNOWN = 4
SUBDOMAIN_CONVERSANT_MIN_RATIO = 0.7
SUBDOMAIN_FLUENT_MIN_KNOWN = 8
SUBDOMAIN_FLUENT_MIN_RATIO = 0.8


def subdomain_band(known: int, attested: int) -> str:
    """Band for one subdomain, from the same known/attested counts as the
    group band (computed by the same predicates in `UserScope`), with the
    floors above. A prior for pitching one part of a briefing; consulted only
    where the per-term ledger has nothing to say about the term at hand."""
    if attested == 0 or known < SUBDOMAIN_DEVELOPING_MIN_KNOWN:
        return BEGINNER
    ratio = known / attested
    if known >= SUBDOMAIN_FLUENT_MIN_KNOWN and ratio >= SUBDOMAIN_FLUENT_MIN_RATIO:
        return FLUENT
    if known >= SUBDOMAIN_CONVERSANT_MIN_KNOWN and ratio >= SUBDOMAIN_CONVERSANT_MIN_RATIO:
        return CONVERSANT
    if known >= SUBDOMAIN_DEVELOPING_MIN_KNOWN and ratio >= SUBDOMAIN_DEVELOPING_MIN_RATIO:
        return DEVELOPING
    return BEGINNER


# --- Eval harness pass/fail ------------------------------------------------

# The harness no longer measures distance to a number. It measures whether the
# system's ledger matches what a persona actually knows -- set agreement,
# reported as precision and recall over concepts. A failure is directly
# readable: "the system thinks they know 'vintage', and they do not."
LEDGER_MIN_PRECISION = 0.75
LEDGER_MIN_RECALL = 0.70
LEDGER_MAX_INTERACTIONS = 8


# --- Paths -----------------------------------------------------------------

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
PROMPTS_DIR = PACKAGE_ROOT / "prompts"


def db_path() -> Path:
    """Location of the SQLite store.

    Overridable so the eval harness and unit tests can point at a scratch DB
    instead of the real one.
    """
    override = os.environ.get("CONVAGENT_DB")
    if override:
        return Path(override)
    return PROJECT_ROOT / "data" / "conversational_agent.db"


# --- Search ----------------------------------------------------------------

# Anthropic's server-side web search tool. Declared in the `tools` array of a
# Messages API call; runs on Anthropic's infrastructure, so there is no
# separate search provider, API key, or client-side execution loop.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}

# Model used for the search-execution call itself. This call is plumbing (run
# a query, return raw results) rather than a judgment point -- the judgment
# about which results to trust is a separate, logged call.
SEARCH_EXECUTION_MODEL = SONNET
# Must be >= the most queries query-formulation is allowed to produce (its
# prompt asks for two to four), or the tool returns max_uses_exceeded and the
# whole poll comes back empty. It was 3 against a prompt asking for up to 4,
# so every live poll failed and reported itself as "the web was quiet".
MAX_SEARCH_USES = 6

# --- Polling ---------------------------------------------------------------

# Fixed interval only in v1 (adaptive/volatility-based polling is explicitly
# out of scope). Per-group override lives on the group record.
DEFAULT_POLL_INTERVAL_MINUTES = 360
