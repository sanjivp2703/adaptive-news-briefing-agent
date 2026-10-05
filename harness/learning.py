"""The persona learning model: what a synthetic reader remembers, and for how long.

Implements section 0 ("What to build") of
`harness/personas/research/learning_model.md`. That document is the spec; this
file is its arithmetic and nothing more. Every constant below is copied from
it with the citation the review gave, so a future recalibration against
real-user data can change a number here and point at the evidence for doing so.

What it is for. Until now a persona's knowledge was two static lists: `knows`
and `does_not_know`, with exactly one transition (asked-and-answered moved a
term from the second to the first, forever). A real reader's knowledge of a
briefing term is neither binary nor permanent: a gloss read once is ~30%
recallable a week later, an answer to one's own question sticks better, and
anything unused for a month fades. The eight-session harness spans days, so
the ground truth the ledger is scored against has to move the way a person's
does -- otherwise a system that correctly stops re-explaining a term the reader
asked about last week is marked wrong for it, and one that re-explains a term
the reader has forgotten is marked right.

The model is FSRS's difficulty/stability/retrievability core (the best-
calibrated retrievability function available; fit on ~350M real reviews) with
two additions the review argues for: an explicit encoding-probability front end
from the incidental-vocabulary literature, because none of the spaced-
repetition models has a notion of "read past it and nothing stuck", and a
per-event scaling `k` so that reading, asking and retrieving strengthen a
trace by different amounts.

Two traits per persona (`LearningSpec`), five numbers per term (`TermTrace`),
seven events (`MemoryModel.*`). Time is always the harness `SimClock`'s,
handed in as `at`; nothing here reads the wall clock.

Ground truth never travels with the system. A `MemoryModel` is the persona's
private mind: the runner consults it to decide how the persona behaves and the
scorer consults it to grade the ledger, and no projection of it is ever placed
in a dict that reaches `judge(...)`.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# =============================================================================
# CONSTANTS -- one table, each with its citation from learning_model.md §0.
# These are the spec. Change them here and only here.
# =============================================================================

# --- Traits (§0.1) -----------------------------------------------------------
LEARNING_SCHEMA = "v1"
PRIOR_KNOWLEDGE_RANGE = (0.0, 1.0)
# `memory_rate` m: multiplier on stability. Groningen ACT-R decay ranges
# 0.19-0.44 around 0.30, i.e. roughly +-35% (Sense & van Rijn 2018).
MEMORY_RATE_RANGE = (0.7, 1.4)
# Encoding multiplier from prior knowledge: 1 + 0.4*(K - 0.5), i.e. 0.8-1.2.
# Modest by design: prior knowledge predicts post-test LEVEL (r = .53) but not
# normalised GAIN (r = -.06) (Simonsmeier et al. 2022). Never compounds.
PRIOR_KNOWLEDGE_ENCODING_SLOPE = 0.4
# Cap on P(encode) after the multiplier (§0.3 rule 1).
P_ENCODE_CAP = 0.9

# --- Retrievability (§0.2) ---------------------------------------------------
# FSRS-4.5 power law, calibrated so R(S) = 0.9:  R = (1 + 19/81 * t/S)^-0.5.
# Power law, not exponential: Murre & Dros (2015) and the FSRS benchmarks.
FSRS_FACTOR = 19.0 / 81.0
FSRS_DECAY = -0.5

# --- The knowledge ladder (§0.2) ---------------------------------------------
# P(can define | recognises) = 0.65 + 0.35 * min(1, (n_ret + n_seed) / 3).
#
# The 0.65 floor is the meaning-recall / meaning-recognition pick-up ratio for
# NEWLY LEARNED words (9%/15% immediate, 12%/17% delayed; Webb, Uchihara &
# Yanagisawa 2023; Pellicer-Sanchez & Schmitt 2010). It is a fact about a word
# met once or twice, not about a word someone uses at work: the live
# read-through (`eval-runs/live/probe4_pilar.json`) gave a head sommelier a 35%
# chance of failing to define `potential alcohol` twelve hours after using it
# fluently three times, which is the 0.65 applied where it does not belong.
# The ratio therefore consolidates toward 1.0 with retrieval practice: every
# successful retrieval or correct use (`n_ret`) closes a third of the gap, and
# a term seeded as known carries `n_seed = KNOWN_SEED_STRENGTH` retrievals'
# worth of consolidation from the start, so a seeded expert term sits at ~1.0
# and a term retrieved three times reaches it. Recognition (R) still decays
# with time; what no longer decays is the ability to define a word you can
# still recognise and have used repeatedly (Bahrick 1984: permastore).
RECALL_GIVEN_RECOGNITION = 0.65
DEFINE_CONSOLIDATION_SPAN = 1.0 - RECALL_GIVEN_RECOGNITION  # 0.35
DEFINE_CONSOLIDATION_EVENTS = 3
# Retrievals' worth of consolidation a seeded-known term starts with. Equal to
# `DEFINE_CONSOLIDATION_EVENTS` so a seeded term is fully consolidated.
KNOWN_SEED_STRENGTH = 3
# P(can use in a NEW context | can define) = 0.5 + 0.25*min(2, n_ctx - 1):
# 0.5 after one context, 1.0 after three (contextual-diversity studies, §3.4:
# Pagan & Nation 2019; Norman et al. 2022; Bolger et al. 2008).
USE_BASE = 0.5
USE_PER_CONTEXT = 0.25
USE_CONTEXT_CAP = 2

# --- The gain kernel (§0.3) --------------------------------------------------
# g(R, D, S) = e^1.49 * (11 - D) * S^-0.14 * (e^(0.94*(1-R)) - 1)
# FSRS-4 default weights, fit on Anki review data (open-spaced-repetition).
# 0.94 (`w10`) is the knob that moves the spacing ridgeline (Cepeda et al.
# 2008 check in §2.2).
GAIN_SCALE = math.exp(1.49)
GAIN_DIFFICULTY_CEILING = 11.0
GAIN_STABILITY_EXPONENT = -0.14
GAIN_RETRIEVABILITY_WEIGHT = 0.94

# --- Event scaling k (§0.3) --------------------------------------------------
# FSRS's kernel is fit to successful retrievals with feedback, so k = 1 there.
# The others are set from ratios in §2.3 and are THE constants to tune against
# real-user data.
K_RETRIEVAL = 1.0  # rule 4: full kernel
# Restudy: Roediger & Karpicke (2006) one-week 42% vs 56% converts to a
# stability ratio of ~1.8 for retrieval over restudy.
K_RESTUDY = 0.55  # rule 2
# Asked-and-answered: generation/elaboration, d ~ 0.40 over reading (Bertsch
# et al. 2007); between restudy and retrieval.
K_ASK = 0.75  # rule 3
# Asking drops the term's difficulty by one (an elaborated representation).
ASK_DIFFICULTY_DROP = 1

# --- First glossed encounter (§0.3 rule 1) -----------------------------------
# read quality -> (P(encode), S0 in days). L2 single-encounter pick-up is ~15%
# unglossed (Swanborn & de Glopper 1999; Nagy et al. 1985/1987); glossing
# raises immediate learning 26.6% -> 45.3% (Yanagisawa, Webb & Uchihara 2020);
# adult L1 readers of their own domain sit above L2 learners, hence 0.5 for a
# normal read. S0 = 0.4 d gives R ~ 0.79 after a day, 0.33 after a week, 0.16
# after a month -- the once-read delayed rates in §2.
ENCODING_BY_READ_QUALITY: dict[str, tuple[float, float]] = {
    "skipped": (0.00, 0.00),
    "skimmed": (0.25, 0.15),
    "read": (0.50, 0.40),
    "studied": (0.70, 0.80),
}
# The row an elaborated answer is at least as good as (used when a persona asks
# about a term that never encoded; see `asked_and_answered`).
STUDIED_QUALITY = "studied"
# The row probe feedback is treated as, for a term with no trace.
FEEDBACK_QUALITY = "read"

# --- Failed retrieval (§0.3 rule 5) ------------------------------------------
# FSRS lapse rule: S' = 2.18 * D^-0.05 * ((S+1)^0.34 - 1) * e^(1.26*(1-R)).
LAPSE_SCALE = 2.18
LAPSE_DIFFICULTY_EXPONENT = -0.05
LAPSE_STABILITY_EXPONENT = 0.34
LAPSE_RETRIEVABILITY_WEIGHT = 1.26

# --- Difficulty (§0.2) --------------------------------------------------------
# 1-10; abstract, polysemous or numeric terms are harder. Default 5; a term the
# fixture declares as a quantity gets one notch harder.
DIFFICULTY_DEFAULT = 5
DIFFICULTY_QUANTITY = 6
DIFFICULTY_MIN = 1
DIFFICULTY_MAX = 10

# --- Seeding a term the persona already knows (the t = 0 seed) --------------
# S = 365 days, n_ctx = 3 (met in enough contexts to use it anywhere).
KNOWN_SEED_STABILITY_DAYS = 365.0
KNOWN_SEED_CONTEXTS = 3

# --- Subdomains: prior knowledge as a shape, not a list -----------------------
# A group's vocabulary is partitioned into 3-6 subdomains (fixture
# `group.subdomains`), and a persona carries a weight 0-1 per subdomain
# (`familiar_subdomains`) saying how at home it is there. At t = 0 every
# vocabulary term in subdomain d with weight w is seeded as known with
# probability w (deterministic from the persona seed), except the labelled
# `knows` (always) and `does_not_know` (never). The labelled lists stay the
# scored subset; the weights decide the rest of the glossary, and they are what
# the system's own `subdomain_familiarity` is measured against.
#
# A first glossed encounter in subdomain d has P(encode) multiplied by
# An unlisted term first met inside a subdomain of weight w is seeded as
# already held with probability PRIOR_KNOWN_OOV * w (0.76 at w = 0.95). The
# first live subdomain run showed an expert answering "i don't know" to
# `hectolitres per hectare` because it was outside the authored vocabulary.
PRIOR_KNOWN_OOV = 0.8

# (0.7 + 0.6 * w): 0.7 in a subdomain the reader knows nothing of, 1.3 in one
# they live in. This is the "prior knowledge helps new learning IN-DOMAIN"
# finding (Witherby & Carpenter 2022: cooking/football knowledge predicted
# learning of new facts in that domain only; Hambrick 2003), kept modest and,
# like the K multiplier, never compounding. A term glossed at runtime that is
# not in the vocabulary is assigned to a subdomain by the system's own label
# where one exists, else by token overlap with the subdomain's terms, else the
# group's least-weighted subdomain (the reader most likely met it nowhere).
SUBDOMAIN_ENCODING_BASE = 0.7
SUBDOMAIN_ENCODING_SLOPE = 0.6
SUBDOMAIN_COUNT_RANGE = (3, 6)
# Consistency between the labelled lists and the weights: a `knows` term may
# not sit in a subdomain weighted at or below this, a `does_not_know` term may
# not sit in one weighted at or above the other.
SUBDOMAIN_KNOWS_MIN_WEIGHT = 0.2
SUBDOMAIN_DNK_MAX_WEIGHT = 0.8
# |prior_knowledge - vocabulary-weighted mean of the weights| may not exceed
# this: K is the generator's summary of the same shape, and the two must agree.
SUBDOMAIN_K_TOLERANCE = 0.15

# --- Behaviour thresholds (the runner's rule, stated once) ------------------
# A term is askable-as-unknown when P(can define) < 0.5 and presupposable in a
# question when P(can use) >= 0.5; "known at time t" for the ledger's ground
# truth is P(can define) >= 0.5.
DEFINE_THRESHOLD = 0.5
USE_THRESHOLD = 0.5

SECONDS_PER_DAY = 86_400.0


# =============================================================================
# Traits
# =============================================================================


@dataclass(frozen=True)
class LearningSpec:
    """The two per-persona learning traits, from the fixture's `learning` block.

    The block is the frozen contract shared with the persona generator:

        "learning": {"schema": "v1", "prior_knowledge": 0.0-1.0,
                     "memory_rate": 0.7-1.4}

    Exactly those keys. The review (§0.1, §6) explicitly excludes age, working
    memory, learning style and interest as separate traits -- age and verbal
    ability are expressed through `memory_rate`, interest through read quality
    and question-asking, which the harness already simulates -- so an unknown
    key is refused rather than ignored, or dead configuration would look live.
    """

    prior_knowledge: float
    memory_rate: float
    schema: str = LEARNING_SCHEMA

    ALLOWED_KEYS = frozenset({"schema", "prior_knowledge", "memory_rate"})

    @property
    def encoding_multiplier(self) -> float:
        """1 + 0.4*(K - 0.5): 0.8 at K = 0, 1.2 at K = 1."""
        return 1.0 + PRIOR_KNOWLEDGE_ENCODING_SLOPE * (self.prior_knowledge - 0.5)

    @classmethod
    def from_raw(cls, raw: Any, *, persona_id: str = "?") -> LearningSpec:
        # Imported here so `learning.py` depends on nothing in `personas`.
        from .personas import FixtureError

        if raw is None:
            raise FixtureError(
                f"{persona_id!r}: fixture has no `learning` block. Add\n"
                '  "learning": {"schema": "v1", "prior_knowledge": <0.0-1.0>, '
                '"memory_rate": <0.7-1.4>}\n'
                "prior_knowledge is how much of the domain glossary this person "
                "starts with (it should agree with `knows`); memory_rate is a "
                "multiplier on how long anything they learn lasts (1.0 = "
                "population default; 0.7 forgets fastest, 1.4 slowest). See "
                "harness/LEARNING.md."
            )
        if not isinstance(raw, dict):
            raise FixtureError(f"{persona_id!r}: `learning` must be an object.")
        unknown = sorted(set(raw) - cls.ALLOWED_KEYS)
        if unknown:
            raise FixtureError(
                f"{persona_id!r}: `learning` declares unknown key(s) {unknown}. "
                f"The block takes exactly {sorted(cls.ALLOWED_KEYS)}. The learning-"
                "model review deliberately excludes age, working memory, learning "
                "style and interest as traits (learning_model.md §6); express age "
                "or verbal ability through memory_rate, interest through the "
                "reading profile and turn rates."
            )
        missing = sorted(cls.ALLOWED_KEYS - set(raw))
        if missing:
            raise FixtureError(
                f"{persona_id!r}: `learning` is missing {missing}; all of "
                f"{sorted(cls.ALLOWED_KEYS)} are required."
            )
        schema = str(raw["schema"])
        if schema != LEARNING_SCHEMA:
            raise FixtureError(
                f"{persona_id!r}: learning.schema is {schema!r}; this harness "
                f"implements {LEARNING_SCHEMA!r}."
            )
        try:
            k = float(raw["prior_knowledge"])
            m = float(raw["memory_rate"])
        except (TypeError, ValueError) as exc:
            raise FixtureError(
                f"{persona_id!r}: learning.prior_knowledge and learning.memory_rate "
                f"must be numbers ({exc})."
            ) from exc
        lo, hi = PRIOR_KNOWLEDGE_RANGE
        if not lo <= k <= hi:
            raise FixtureError(
                f"{persona_id!r}: learning.prior_knowledge={k} is outside [{lo}, {hi}]."
            )
        lo, hi = MEMORY_RATE_RANGE
        if not lo <= m <= hi:
            raise FixtureError(
                f"{persona_id!r}: learning.memory_rate={m} is outside [{lo}, {hi}] "
                "(the +-35% individual range around the population default, "
                "Sense & van Rijn 2018)."
            )
        return cls(prior_knowledge=k, memory_rate=m, schema=schema)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "prior_knowledge": self.prior_knowledge,
            "memory_rate": self.memory_rate,
        }


# =============================================================================
# Per-term state
# =============================================================================


@dataclass
class TermTrace:
    """One (persona, term) memory trace -- §0.2.

    `S` is stability in days: the time for retrievability to fall to 0.90.
    `S = 0` never occurs on a stored trace; "no trace" is the absence of the
    entry. `D` is difficulty 1-10. `n_ctx` counts DISTINCT contexts (news
    items, answers) the term was met or used in; `n_ret` counts successful
    retrievals. `t_last` is the simulated time of the last event.
    """

    S: float
    D: float
    n_ctx: int
    n_ret: int
    t_last: datetime
    # Retrievals' worth of consolidation the trace was born with:
    # `KNOWN_SEED_STRENGTH` for a term seeded as known at t = 0, else 0. Enters
    # the define rung only (see `define_given_recognition`).
    n_seed: int = 0

    def as_dict(self) -> dict[str, Any]:
        # `S_days` is the stored, population-default stability; the persona's
        # `memory_rate` multiplies it at read time.
        return {
            "S_days": round(self.S, 4),
            "D": self.D,
            "n_ctx": self.n_ctx,
            "n_ret": self.n_ret,
            "n_seed": self.n_seed,
            "t_last": self.t_last.isoformat(),
        }


# =============================================================================
# The equations, as plain functions (so the tests can hit them directly)
# =============================================================================


def retrievability(t_days: float, stability_days: float) -> float:
    """R(t, S) = (1 + 19/81 * t/S)^-0.5, the FSRS-4.5 power law. R(S) = 0.9."""
    if stability_days <= 0.0:
        return 0.0
    t = max(0.0, float(t_days))
    return (1.0 + FSRS_FACTOR * t / stability_days) ** FSRS_DECAY


def gain(R: float, D: float, S: float) -> float:
    """g(R, D, S) = e^1.49 * (11 - D) * S^-0.14 * (e^(0.94*(1-R)) - 1)."""
    if S <= 0.0:
        return 0.0
    r = min(1.0, max(0.0, R))
    return (
        GAIN_SCALE
        * (GAIN_DIFFICULTY_CEILING - D)
        * (S**GAIN_STABILITY_EXPONENT)
        * (math.exp(GAIN_RETRIEVABILITY_WEIGHT * (1.0 - r)) - 1.0)
    )


def lapse_stability(R: float, D: float, S: float) -> float:
    """FSRS lapse rule: 2.18 * D^-0.05 * ((S+1)^0.34 - 1) * e^(1.26*(1-R)).

    Capped at the prior S, as FSRS itself does: a failure may not leave the
    trace stronger than a success would have.
    """
    r = min(1.0, max(0.0, R))
    lapsed = (
        LAPSE_SCALE
        * (D**LAPSE_DIFFICULTY_EXPONENT)
        * ((S + 1.0) ** LAPSE_STABILITY_EXPONENT - 1.0)
        * math.exp(LAPSE_RETRIEVABILITY_WEIGHT * (1.0 - r))
    )
    return max(1e-6, min(S, lapsed))


def use_factor(n_ctx: int) -> float:
    """0.5 + 0.25*min(2, n_ctx - 1): the contextual-diversity gate on rung 3."""
    return USE_BASE + USE_PER_CONTEXT * min(USE_CONTEXT_CAP, max(0, n_ctx - 1))


def define_given_recognition(n_ret: int, n_seed: int = 0) -> float:
    """P(can define | recognises) = 0.65 + 0.35 * min(1, (n_ret + n_seed) / 3).

    0.65 for a word met once or twice; 1.0 for a seeded-known term or one
    retrieved / correctly used three times. See the constant block.
    """
    events = max(0, int(n_ret)) + max(0, int(n_seed))
    consolidation = min(1.0, events / float(DEFINE_CONSOLIDATION_EVENTS))
    return RECALL_GIVEN_RECOGNITION + DEFINE_CONSOLIDATION_SPAN * consolidation


def subdomain_encoding_multiplier(weight: float) -> float:
    """(0.7 + 0.6 * w): the in-domain encoding advantage of a familiar subdomain."""
    w = min(1.0, max(0.0, float(weight)))
    return SUBDOMAIN_ENCODING_BASE + SUBDOMAIN_ENCODING_SLOPE * w


def _clamp_difficulty(d: float) -> float:
    return float(max(DIFFICULTY_MIN, min(DIFFICULTY_MAX, d)))


def _tokens(text: str) -> set[str]:
    """Content tokens of a term or label, for the near-match comparisons."""
    from conversational_agent.store import normalize_term

    stop = {"the", "a", "an", "of", "in", "on", "for", "and", "to", "&", "vs"}
    return {t for t in normalize_term(str(text)).split() if t and t not in stop}


def token_overlap(a: str, b: str) -> float:
    """Jaccard overlap of content tokens; 0 when either side is empty."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# =============================================================================
# Subdomains: the taxonomy a persona's prior knowledge is shaped by
# =============================================================================


@dataclass(frozen=True)
class SubdomainMap:
    """A group's subdomain taxonomy plus one persona's weight for each.

    `terms` maps a subdomain name to the terms it holds -- every vocabulary
    term exactly once, plus optional anchor terms that are not in the
    vocabulary (they give a runtime out-of-vocabulary term something to match
    against). `weights` maps each of the same names to 0.0-1.0. Names are
    normalised like terms so the system's free-text labels can be compared
    against them.

    Harness-only ground truth: never in `system_visible_group`, never in a dict
    handed to `judge(...)`.
    """

    terms: dict[str, tuple[str, ...]]
    weights: dict[str, float]

    @staticmethod
    def norm(name: str) -> str:
        from conversational_agent.store import normalize_term

        return normalize_term(str(name))

    @classmethod
    def from_raw(
        cls,
        subdomains: Mapping[str, Iterable[str]] | None,
        familiar: Mapping[str, Any] | None,
        *,
        persona_id: str = "?",
        vocabulary: Iterable[str] = (),
        knows: Iterable[str] = (),
        does_not_know: Iterable[str] = (),
        prior_knowledge: float | None = None,
    ) -> SubdomainMap | None:
        """Parse and validate the fixture's two blocks together.

        Returns None when the fixture declares neither (a legacy fixture: the
        labelled lists then decide everything, exactly as before). Declaring
        one without the other is an error, as is any inconsistency between the
        weights and the labelled lists.
        """
        from .personas import FixtureError

        if not subdomains and not familiar:
            return None
        if not subdomains or not familiar:
            raise FixtureError(
                f"{persona_id!r}: `group.subdomains` and `familiar_subdomains` must "
                "be declared together (the taxonomy and this persona's weights over "
                "it). See harness/LEARNING.md, 'Subdomains'."
            )
        if not isinstance(subdomains, Mapping) or not isinstance(familiar, Mapping):
            raise FixtureError(
                f"{persona_id!r}: `group.subdomains` must be an object "
                "{name: [terms]} and `familiar_subdomains` an object {name: weight}."
            )
        norm_term = lambda t: str(t).strip().lower()
        terms: dict[str, tuple[str, ...]] = {}
        owner: dict[str, str] = {}
        for raw_name, raw_terms in subdomains.items():
            name = cls.norm(raw_name)
            if not name:
                raise FixtureError(f"{persona_id!r}: a subdomain has an empty name.")
            if name in terms:
                raise FixtureError(
                    f"{persona_id!r}: subdomain {raw_name!r} is declared twice "
                    "(names are compared after normalisation)."
                )
            cleaned = tuple(dict.fromkeys(norm_term(t) for t in (raw_terms or ()) if norm_term(t)))
            if not cleaned:
                raise FixtureError(
                    f"{persona_id!r}: subdomain {raw_name!r} holds no terms. Every "
                    "subdomain needs at least one term (a vocabulary term or an "
                    "anchor) or nothing can ever be assigned to it."
                )
            for t in cleaned:
                if t in owner:
                    raise FixtureError(
                        f"{persona_id!r}: term {t!r} is in both subdomain "
                        f"{owner[t]!r} and {name!r}; every term belongs to exactly one."
                    )
                owner[t] = name
            terms[name] = cleaned
        lo, hi = SUBDOMAIN_COUNT_RANGE
        if not lo <= len(terms) <= hi:
            raise FixtureError(
                f"{persona_id!r}: {len(terms)} subdomain(s) declared; a group takes "
                f"{lo}-{hi} (short lowercase noun phrases covering its vocabulary)."
            )
        weights: dict[str, float] = {}
        for raw_name, raw_w in familiar.items():
            name = cls.norm(raw_name)
            if name not in terms:
                raise FixtureError(
                    f"{persona_id!r}: familiar_subdomains names {raw_name!r}, which "
                    f"is not one of the group's subdomains {sorted(terms)}."
                )
            try:
                w = float(raw_w)
            except (TypeError, ValueError) as exc:
                raise FixtureError(
                    f"{persona_id!r}: familiar_subdomains[{raw_name!r}] must be a "
                    f"number 0.0-1.0 ({exc})."
                ) from exc
            if not 0.0 <= w <= 1.0:
                raise FixtureError(
                    f"{persona_id!r}: familiar_subdomains[{raw_name!r}]={w} is outside [0, 1]."
                )
            weights[name] = w
        missing = sorted(set(terms) - set(weights))
        if missing:
            raise FixtureError(
                f"{persona_id!r}: familiar_subdomains gives no weight for {missing}; "
                "every subdomain needs one (0.0 is a valid answer)."
            )
        vocab = tuple(dict.fromkeys(norm_term(t) for t in vocabulary))
        uncovered = sorted(t for t in vocab if t not in owner)
        if uncovered:
            raise FixtureError(
                f"{persona_id!r}: vocabulary term(s) {uncovered} are in no subdomain. "
                "Every vocabulary term must belong to exactly one."
            )
        for t in (norm_term(k) for k in knows):
            d = owner.get(t)
            if d is not None and weights[d] <= SUBDOMAIN_KNOWS_MIN_WEIGHT:
                raise FixtureError(
                    f"{persona_id!r}: {t!r} is in `knows` but its subdomain {d!r} is "
                    f"weighted {weights[d]} (<= {SUBDOMAIN_KNOWS_MIN_WEIGHT}). A person "
                    "does not hold a term from a field they know nothing of; raise the "
                    "weight or move the term."
                )
        for t in (norm_term(k) for k in does_not_know):
            d = owner.get(t)
            if d is not None and weights[d] >= SUBDOMAIN_DNK_MAX_WEIGHT:
                raise FixtureError(
                    f"{persona_id!r}: {t!r} is in `does_not_know` but its subdomain "
                    f"{d!r} is weighted {weights[d]} (>= {SUBDOMAIN_DNK_MAX_WEIGHT}). "
                    "A gap in a field they are at home in needs a lower weight or a "
                    "different subdomain."
                )
        smap = cls(terms=terms, weights=weights)
        if prior_knowledge is not None and vocab:
            mean = smap.vocabulary_weighted_mean(vocab)
            if abs(mean - float(prior_knowledge)) > SUBDOMAIN_K_TOLERANCE:
                raise FixtureError(
                    f"{persona_id!r}: learning.prior_knowledge={prior_knowledge} but the "
                    f"vocabulary-weighted mean of familiar_subdomains is {mean:.2f} "
                    f"(tolerance {SUBDOMAIN_K_TOLERANCE}). K is the one-number summary "
                    "of the same shape; make them agree."
                )
        return smap

    # --- reads ------------------------------------------------------------

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self.terms)

    def of(self, term: str) -> str | None:
        """The subdomain a term is declared in, or None."""
        t = str(term).strip().lower()
        for name, held in self.terms.items():
            if t in held:
                return name
        # Second pass on the store's normalisation, so "run rate" finds "run-rate".
        wanted = self.norm(t)
        for name, held in self.terms.items():
            if any(self.norm(h) == wanted for h in held):
                return name
        return None

    def weight(self, name: str | None) -> float:
        if name is None:
            return 0.0
        return float(self.weights.get(self.norm(name), 0.0))

    @property
    def least_weighted(self) -> str:
        return min(self.weights, key=lambda n: (self.weights[n], n))

    def match_label(self, label: str | None) -> tuple[str | None, str]:
        """Map a free-text label (the system's) onto a subdomain name.

        Returns (name, how) with how in {"exact", "near", "none"}: exact after
        normalisation, else the best token-overlap match above zero.
        """
        if not label:
            return None, "none"
        wanted = self.norm(label)
        if wanted in self.terms:
            return wanted, "exact"
        best, score = None, 0.0
        for name in self.terms:
            s = token_overlap(wanted, name)
            if s > score:
                best, score = name, s
        return (best, "near") if best is not None else (None, "none")

    def nearest(self, term: str, system_label: str | None = None) -> tuple[str, str]:
        """Assign a term (usually one outside the vocabulary) to a subdomain.

        Returns (name, how) with how in {"declared", "system_label",
        "token_overlap", "least_weighted"}, in that order of preference.
        """
        declared = self.of(term)
        if declared is not None:
            return declared, "declared"
        by_label, how = self.match_label(system_label)
        if by_label is not None:
            return by_label, "system_label"
        best, score = None, 0.0
        for name, held in self.terms.items():
            s = max((token_overlap(term, h) for h in held), default=0.0)
            s = max(s, token_overlap(term, name))
            if s > score:
                best, score = name, s
        if best is not None:
            return best, "token_overlap"
        return self.least_weighted, "least_weighted"

    def vocabulary_weighted_mean(self, vocabulary: Iterable[str]) -> float:
        """Mean weight over the vocabulary, each term at its subdomain's weight."""
        vocab = [str(t).strip().lower() for t in vocabulary]
        if not vocab:
            return 0.0
        total = 0.0
        for t in vocab:
            total += self.weight(self.of(t))
        return total / len(vocab)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subdomains": {n: list(t) for n, t in self.terms.items()},
            "familiar_subdomains": dict(self.weights),
        }


# =============================================================================
# The model
# =============================================================================


@dataclass
class MemoryModel:
    """One persona's memory for the domain glossary. Ground truth; never shown
    to the system.

    Construction seeds the t = 0 state from the fixture: every term in `knows`
    starts as a strong, multiply-contextualised trace, every term in
    `does_not_know` with none, and -- when the fixture carries a subdomain
    taxonomy -- every other glossary term is seeded as known with probability
    equal to its subdomain's weight. From then on the static lists are not
    consulted -- the runner drives the seven events and reads the ladder.
    """

    learning: LearningSpec
    universe: tuple[str, ...]
    rng: random.Random
    traces: dict[str, TermTrace] = field(default_factory=dict)
    difficulty: dict[str, float] = field(default_factory=dict)
    # Every time a term was put in front of the persona (any non-skipped
    # glossed encounter, answer, or probe feedback). Separate from `t_last`
    # because an exposure that did not encode still counts as an exposure for
    # "days since last exposure".
    exposures: dict[str, list[datetime]] = field(default_factory=dict)
    # Context ids (exchange ids, probe ids) each term has been met in, so the
    # runner can ask "is this a new context for this term?" without keeping
    # its own ledger of them.
    contexts: dict[str, set[str]] = field(default_factory=dict)
    # An audit trail of events, for the report and for debugging. Never
    # serialised into anything the system sees.
    log: list[tuple[str, str, str]] = field(default_factory=list)
    # The subdomain taxonomy and this persona's weights (None for a legacy
    # fixture, in which case the labelled lists decide everything).
    subdomains: SubdomainMap | None = None
    # term -> (subdomain, how it was assigned), for every term the model has
    # placed: vocabulary terms at construction ("declared"), runtime terms at
    # their first gloss. Report material.
    assignments: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Vocabulary terms seeded as known BY WEIGHT (not in `knows`), for the report.
    seeded_by_weight: tuple[str, ...] = ()

    # --- construction ---------------------------------------------------

    @classmethod
    def seeded(
        cls,
        *,
        learning: LearningSpec,
        seed: Any,
        persona_id: str,
        universe: Iterable[str],
        knows: Iterable[str],
        at: datetime,
        quantities: Iterable[str] = (),
        difficulty: Mapping[str, float] | None = None,
        does_not_know: Iterable[str] = (),
        subdomains: SubdomainMap | None = None,
    ) -> MemoryModel:
        """Seed the t = 0 state.

        Every term in `knows` is a strong trace; every term in `does_not_know`
        has none. With a `SubdomainMap`, every OTHER vocabulary term is seeded
        as known with probability equal to its subdomain's weight, from a
        `Random` of its own (so the seeding does not shift the event stream).
        Without one, only `knows` is seeded -- the pre-subdomain behaviour.
        """
        rng = random.Random(f"{seed}:{persona_id}:memory")
        terms = tuple(dict.fromkeys(str(t).strip().lower() for t in universe))
        quantity_set = {str(t).strip().lower() for t in quantities}
        diff: dict[str, float] = {}
        for term in terms:
            base = DIFFICULTY_QUANTITY if term in quantity_set else DIFFICULTY_DEFAULT
            if difficulty and term in difficulty:
                base = difficulty[term]
            diff[term] = _clamp_difficulty(base)
        model = cls(
            learning=learning, universe=terms, rng=rng, difficulty=diff, subdomains=subdomains
        )
        known = {str(t).strip().lower() for t in knows}
        unknown = {str(t).strip().lower() for t in does_not_know}
        seeded_terms: list[str] = list(dict.fromkeys(str(t).strip().lower() for t in knows))
        by_weight: list[str] = []
        if subdomains is not None:
            seed_rng = random.Random(f"{seed}:{persona_id}:seed-subdomains")
            for term in terms:
                name = subdomains.of(term)
                if name is not None:
                    model.assignments[term] = (name, "declared")
                if term in known or term in unknown:
                    continue
                w = subdomains.weight(name)
                # Drawn for every unlabelled term, in vocabulary order, so a
                # change to one weight moves only that term's outcome.
                draw = seed_rng.random()
                if draw < w:
                    seeded_terms.append(term)
                    by_weight.append(term)
        for term in seeded_terms:
            if term not in diff:
                diff[term] = float(DIFFICULTY_DEFAULT)
            model.traces[term] = TermTrace(
                S=KNOWN_SEED_STABILITY_DAYS,
                D=diff[term],
                n_ctx=KNOWN_SEED_CONTEXTS,
                n_ret=0,
                t_last=at,
                n_seed=KNOWN_SEED_STRENGTH,
            )
        model.seeded_by_weight = tuple(by_weight)
        return model

    def _seed_known(self, term: str, at: datetime) -> TermTrace:
        """Seed a term as already held, exactly as `seeded` does at t = 0."""
        tr = TermTrace(
            S=KNOWN_SEED_STABILITY_DAYS,
            D=self._d(term),
            n_ctx=KNOWN_SEED_CONTEXTS,
            n_ret=0,
            t_last=at,
            n_seed=KNOWN_SEED_STRENGTH,
        )
        self.traces[term] = tr
        return tr

    # --- subdomains ---------------------------------------------------------

    def subdomain_of(self, term: str, system_label: str | None = None) -> str | None:
        """The subdomain this term sits in, assigning (and remembering) it if new.

        Vocabulary terms come from the taxonomy; anything else is placed by the
        system's own label, then token overlap, then the least-weighted
        subdomain. None when the persona carries no taxonomy.
        """
        if self.subdomains is None:
            return None
        key = str(term).strip().lower()
        placed = self.assignments.get(key)
        if placed is not None:
            return placed[0]
        name, how = self.subdomains.nearest(key, system_label)
        self.assignments[key] = (name, how)
        self._note("subdomain", key, f"{name} ({how})")
        return name

    def subdomain_weight(self, term: str, system_label: str | None = None) -> float | None:
        name = self.subdomain_of(term, system_label)
        if name is None:
            return None
        return self.subdomains.weight(name) if self.subdomains else None

    # --- reads ------------------------------------------------------------

    def _d(self, term: str) -> float:
        return self.difficulty.get(term, float(DIFFICULTY_DEFAULT))

    def trace(self, term: str) -> TermTrace | None:
        return self.traces.get(term.strip().lower())

    def has_trace(self, term: str) -> bool:
        return term.strip().lower() in self.traces

    def retrievability_of(self, term: str, at: datetime) -> float:
        """R at `at`, with `memory_rate` applied to S: R(t, S*m)."""
        tr = self.trace(term)
        if tr is None:
            return 0.0
        t = (at - tr.t_last).total_seconds() / SECONDS_PER_DAY
        # `S` is stored unscaled; `memory_rate` is applied here and only here
        # (§0.3 rule 6: "R(t, S*m) is computed on demand"), so the gain and
        # lapse kernels see the population-default S.
        return retrievability(t, tr.S * self.learning.memory_rate)

    def ladder(self, term: str, at: datetime) -> tuple[float, float, float]:
        """(P(recognises), P(can define), P(can use in a new context)).

        Marginal probabilities down the ladder of §0.2: each rung is
        conditional on the one below.
        """
        tr = self.trace(term)
        if tr is None:
            return (0.0, 0.0, 0.0)
        p_rec = self.retrievability_of(term, at)
        p_def = p_rec * define_given_recognition(tr.n_ret, tr.n_seed)
        p_use = p_def * use_factor(tr.n_ctx)
        return (p_rec, p_def, p_use)

    def sample_rung(self, term: str, at: datetime) -> int:
        """Draw the rung this persona would actually reach right now, 0..3."""
        tr = self.trace(term)
        if tr is None:
            return 0
        if self.rng.random() >= self.retrievability_of(term, at):
            return 0
        if self.rng.random() >= define_given_recognition(tr.n_ret, tr.n_seed):
            return 1
        if self.rng.random() >= use_factor(tr.n_ctx):
            return 2
        return 3

    def is_known(self, term: str, at: datetime) -> bool:
        """The ledger's dynamic ground truth: P(can define) >= 0.5 at `at`."""
        return self.ladder(term, at)[1] >= DEFINE_THRESHOLD

    def is_askable(self, term: str, at: datetime) -> bool:
        """Would this persona honestly ask what the term means?"""
        return self.ladder(term, at)[1] < DEFINE_THRESHOLD

    def is_presupposable(self, term: str, at: datetime) -> bool:
        """Could this persona lean on the term in a question about a new story?"""
        return self.ladder(term, at)[2] >= USE_THRESHOLD

    def known_terms(self, at: datetime) -> tuple[str, ...]:
        return tuple(sorted(t for t in self.traces if self.is_known(t, at)))

    def askable_terms(self, at: datetime) -> tuple[str, ...]:
        return tuple(sorted(t for t in self.universe if self.is_askable(t, at)))

    def presupposable_terms(self, at: datetime) -> tuple[str, ...]:
        return tuple(sorted(t for t in self.traces if self.is_presupposable(t, at)))

    def last_exposure(self, term: str) -> datetime | None:
        seen = self.exposures.get(term.strip().lower())
        return seen[-1] if seen else None

    def days_since_exposure(self, term: str, at: datetime) -> float | None:
        last = self.last_exposure(term)
        if last is None:
            return None
        return max(0.0, (at - last).total_seconds() / SECONDS_PER_DAY)

    def exposed_terms(self) -> tuple[str, ...]:
        return tuple(sorted(self.exposures))

    def new_context(self, term: str, context_id: str) -> bool:
        """Record `context_id` for `term`; True if it had not been met there before."""
        key = term.strip().lower()
        seen = self.contexts.setdefault(key, set())
        if context_id in seen:
            return False
        seen.add(context_id)
        return True

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {t: tr.as_dict() for t, tr in sorted(self.traces.items())}

    # --- events (§0.3) ----------------------------------------------------

    def _note(self, event: str, term: str, detail: str) -> None:
        self.log.append((event, term, detail))

    def _expose(self, term: str, at: datetime) -> None:
        self.exposures.setdefault(term, []).append(at)

    def _seed_from_row(self, term: str, quality: str, at: datetime) -> TermTrace:
        """A fresh trace at the S0 of one encoding-table row, D from the hint."""
        _, s0 = ENCODING_BY_READ_QUALITY[quality]
        tr = TermTrace(
            S=s0,
            D=self._d(term),
            n_ctx=1,
            n_ret=0,
            t_last=at,
        )
        self.traces[term] = tr
        return tr

    def _grow(self, tr: TermTrace, k: float, R: float) -> float:
        """S' = S * (1 + k * g(R, D, S)); returns the gain kernel value."""
        g = gain(R, tr.D, tr.S)
        tr.S = tr.S * (1.0 + k * g)
        return g

    def first_gloss(
        self,
        term: str,
        read_quality: str,
        at: datetime,
        system_label: str | None = None,
    ) -> bool:
        """Rule 1: first glossed encounter. Draw whether any trace forms.

        Returns True if a trace was formed. `read_quality` is the harness's
        four-band reading measure (`skipped` / `skimmed` / `read` / `studied`).
        P(encode) is multiplied by the prior-knowledge multiplier, then -- when
        the persona carries a subdomain taxonomy -- by the in-domain multiplier
        `(0.7 + 0.6 * w)` for the term's subdomain (a term outside the
        vocabulary is placed first; `system_label` is the system's own label
        for it, used as the first hint), and capped at 0.9. S0 is the table's
        (`memory_rate` scales it at read time).
        """
        term = term.strip().lower()
        if term in self.traces:
            # Not a first encounter after all; a restudy in a new context.
            return self.reread(term, read_quality, at, new_context=True)
        if read_quality not in ENCODING_BY_READ_QUALITY:
            read_quality = "read"
        p_encode, _ = ENCODING_BY_READ_QUALITY[read_quality]
        if p_encode <= 0.0:
            self._note("first_gloss", term, f"{read_quality}: not seen")
            return False
        self._expose(term, at)
        w = self.subdomain_weight(term, system_label)
        # A term the fixture never listed, met for the first time inside a
        # subdomain the reader is at home in, is more likely something they
        # already held than something new: prior knowledge sets the LEVEL
        # (Simonsmeier et al. 2022), not just the encoding rate. Seed it as
        # known with probability PRIOR_KNOWN_OOV * w before any encoding draw.
        # Declared vocabulary terms are excluded -- their status is authored.
        placed = self.assignments.get(term)
        if w is not None and (placed is None or placed[1] != "declared"):
            if self.rng.random() < PRIOR_KNOWN_OOV * w:
                tr = self._seed_known(term, at)
                self._note("first_gloss", term, f"already held (prior, w={w:.2f})")
                return True
        p = p_encode * self.learning.encoding_multiplier
        in_domain = ""
        if w is not None:
            p *= subdomain_encoding_multiplier(w)
            in_domain = f" w={w:.2f}"
        p = min(P_ENCODE_CAP, p)
        draw = self.rng.random()
        if draw >= p:
            self._note(
                "first_gloss", term, f"{read_quality}: no trace (p={p:.2f}{in_domain})"
            )
            return False
        tr = self._seed_from_row(term, read_quality, at)
        self._note(
            "first_gloss", term, f"{read_quality}: S0={tr.S:.3f} (p={p:.2f}{in_domain})"
        )
        return True

    def reread(
        self, term: str, read_quality: str, at: datetime, new_context: bool
    ) -> bool:
        """Rule 2: a re-read of the gloss after a gap. k = 0.55.

        Same context leaves `n_ctx` alone; a new news item adds one. A skipped
        read is no event at all. A re-read of a term that never encoded is a
        first encounter and is drawn as one.
        """
        term = term.strip().lower()
        if read_quality == "skipped":
            self._note("reread", term, "skipped: not seen")
            return False
        tr = self.traces.get(term)
        if tr is None:
            return self.first_gloss(term, read_quality, at)
        self._expose(term, at)
        R = self.retrievability_of(term, at)
        g = self._grow(tr, K_RESTUDY, R)
        if new_context:
            tr.n_ctx += 1
        tr.t_last = at
        self._note("reread", term, f"R={R:.2f} g={g:.2f} -> S={tr.S:.3f}")
        return True

    def asked_and_answered(self, term: str, at: datetime) -> None:
        """Rule 3: asked a follow-up about the term and read the answer.

        A studied re-exposure with elaboration: k = 0.75, `n_ctx += 1` (the
        answer is a new context), `D -= 1` (min 1). Two readings the review
        leaves implicit are made explicit here and recorded in LEARNING.md:
        a term that had never encoded forms a trace with certainty (the
        persona generated the question and read the answer -- this is the
        generation effect, not incidental pick-up), and an existing trace is
        floored at the `studied` S0 so that a skim followed by an elaborated
        answer is never weaker than a studied first read.
        """
        term = term.strip().lower()
        self._expose(term, at)
        floor = ENCODING_BY_READ_QUALITY[STUDIED_QUALITY][1]
        tr = self.traces.get(term)
        if tr is None:
            tr = self._seed_from_row(term, STUDIED_QUALITY, at)
            tr.D = _clamp_difficulty(tr.D - ASK_DIFFICULTY_DROP)
            self._note("asked_and_answered", term, f"new trace S0={tr.S:.3f}")
            return
        R = self.retrievability_of(term, at)
        g = self._grow(tr, K_ASK, R)
        tr.S = max(tr.S, floor)
        tr.D = _clamp_difficulty(tr.D - ASK_DIFFICULTY_DROP)
        tr.n_ctx += 1
        tr.t_last = at
        self._note("asked_and_answered", term, f"R={R:.2f} g={g:.2f} -> S={tr.S:.3f}")

    def retrieval_success(
        self, term: str, at: datetime, new_context: bool, feedback: bool
    ) -> None:
        """Rule 4: a successful retrieval -- a probe answered at rung >= 2, or
        the term used correctly in the persona's own question.

        k = 1.0 (the full FSRS kernel), `n_ret += 1`, `n_ctx += 1` if the use
        was about a new news item. With feedback (the harness then shows the
        gloss) a restudy event (k = 0.55) is added at the same pre-event R --
        feedback roughly doubles the testing effect (Rowland 2014: g 0.73 vs
        0.39). A success on a term with no trace (only possible live, when the
        persona answers from outside the model) forms a studied trace.
        """
        term = term.strip().lower()
        self._expose(term, at)
        tr = self.traces.get(term)
        if tr is None:
            tr = self._seed_from_row(term, STUDIED_QUALITY, at)
            tr.n_ret = 1
            self._note("retrieval_success", term, f"new trace S0={tr.S:.3f}")
            return
        R = self.retrievability_of(term, at)
        g = self._grow(tr, K_RETRIEVAL, R)
        if feedback:
            tr.S = tr.S * (1.0 + K_RESTUDY * g)
        tr.n_ret += 1
        if new_context:
            tr.n_ctx += 1
        tr.t_last = at
        self._note(
            "retrieval_success",
            term,
            f"R={R:.2f} g={g:.2f} feedback={feedback} -> S={tr.S:.3f}",
        )

    def retrieval_failure(self, term: str, at: datetime, feedback: bool) -> None:
        """Rule 5: a failed retrieval (probe wrong or "don't know").

        The FSRS lapse rule, then a restudy event if feedback is given. Without
        feedback a failed attempt has no reliable benefit (Rowland 2014:
        initial success <= 50% => g ~ 0.03), so it is recorded and nothing
        else. A failure on a term with no trace followed by feedback is a first
        glossed encounter at `read` quality.
        """
        term = term.strip().lower()
        tr = self.traces.get(term)
        if tr is None:
            if feedback:
                self.first_gloss(term, FEEDBACK_QUALITY, at)
            else:
                self._note("retrieval_failure", term, "no trace, no feedback")
            return
        R = self.retrievability_of(term, at)
        before = tr.S
        tr.S = lapse_stability(R, tr.D, tr.S)
        if feedback:
            self._expose(term, at)
            g = self._grow(tr, K_RESTUDY, R)
        else:
            g = 0.0
        tr.t_last = at
        self._note(
            "retrieval_failure",
            term,
            f"R={R:.2f} S {before:.3f} -> {tr.S:.3f} (feedback={feedback}, g={g:.2f})",
        )

    # Rule 6 (time elapsed) is `retrievability_of`, computed on demand.
    # Rule 7 (same vs new context) is `n_ctx`, gating rung 3 via `use_factor`.

    def glossed(
        self,
        term: str,
        read_quality: str,
        at: datetime,
        context_id: str,
        system_label: str | None = None,
    ) -> bool:
        """The runner's dispatcher for a glossed encounter in a briefing:
        rule 1 if the term has no trace, otherwise rule 2 in a new context.
        `system_label` is the system's own subdomain label for the term, if
        its ledger already carries one (used only to place a term that is not
        in the vocabulary)."""
        term = term.strip().lower()
        fresh = self.new_context(term, context_id)
        if term in self.traces:
            return self.reread(term, read_quality, at, new_context=fresh)
        return self.first_gloss(term, read_quality, at, system_label=system_label)
