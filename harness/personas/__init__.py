"""Persona fixtures: synthetic users defined by what they know and how they write.

The system briefs and then answers whatever the user chooses to ask; it never
asks the user anything, and each briefing is generated from the event and the
ledger. A fixture therefore cannot script replies in advance. A persona is
declarative:

    knows / does_not_know   -- GROUND TRUTH at t = 0: the LABELLED seed of the
                               persona's memory model (`harness/learning.py`),
                               which from then on decides both how the persona
                               behaves and what the ledger is scored against.
    group.subdomains /      -- the group's vocabulary partitioned into 3-6
    familiar_subdomains        subdomains, and this persona's 0-1 weight for
                               each. Prior knowledge as a SHAPE: every unlabelled
                               vocabulary term is seeded as known with its
                               subdomain's weight, and a glossed term in a
                               familiar subdomain encodes faster. Ground truth,
                               like the lists; the system's own subdomain labels
                               and familiarity are scored against them.
    learning                -- the two learning traits (prior_knowledge,
                               memory_rate) that model runs on. Required.
    style                   -- how this person writes, in the parameters
                               `research/assistant_reply_style_profile.md`
                               actually measured (length variance from
                               `research/reply_style_profile.md` section 8.2).
    events                  -- the real news items its feed replays, each with
                               a GROUND TRUTH `should_be_material` label.

A `Responder` (see `harness/responders.py`) turns those three things into a
reply to whatever it is actually asked. Offline that is deterministic template
composition; under `--live` it is a small persona agent.

**Ground truth never travels with the system.** `knows`, `does_not_know` and
`should_be_material` are handed to the responder and the scorer directly and
are never placed in a dict that reaches `judge(...)`. If they rode along in a
judgment context they would be written verbatim into
`judgment_log.input_json`, and under `--live` a real model would simply read
the answer off the page. `Persona.system_visible_group()` is the only view of a
persona that is safe to hand to the system, and it is the one the runner uses.

`group.vocabulary` is deliberately *not* ground truth. It is the domain's
glossary -- the jargon a news item in this group contains -- which a real model
reads straight out of the headline it was given. It is what the offline stubs
use to decide which terms a briefing covers, and it says nothing about which of
them this particular person understands.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..learning import LearningSpec, SubdomainMap
from ..reading import PROFILES as READING_PROFILES
from ..reading import ReadingProfile, profile_for

FIXTURE_DIR = Path(__file__).resolve().parent

# Archetype names. `research/reply_style_profile.md` §8.2 (length) is keyed on
# these, so they are a closed set rather than free text.
ARCHETYPES = ("well_informed", "partially_informed", "barely_informed", "disengaged")


@dataclass(frozen=True)
class StyleSpec:
    """How this persona writes, and how often it speaks at all.

    Register comes from `research/assistant_reply_style_profile.md` (real
    human->assistant turns after an informational message). Length variance
    alone is retained from the forum profile, `research/reply_style_profile.md`
    section 8.2.

    * `median_words` / `variance` -- length is a *distribution*, not a
      constant. Section 8.2's hard rule (>=20% of replies under 10 words,
      >=15% over 35) is checked against the configured distribution in
      `metrics.style_fidelity`.
    * `lowercase` -- a fixed per-persona trait, never a per-reply coin flip.
      37.5% of measured turns are lowercase-initial, and it clusters by person.
    * `drop_terminal_punct_rate` -- 51% of measured turns carry terminal
      punctuation; ~0.5.

    **Turn rates** -- whether a turn happens. The profile's section 4 puts the
    zero-reply outcome at 45% of informational messages and the "replies about
    the information" outcome at about 25%, so `p_first_turn` for a briefing
    with nothing unfamiliar in it should sit around 0.25-0.35, not the old
    0.6-0.8.

    * `ask_rate_default` -- P(asks something) when the briefing contains
      nothing this persona fails to hold.
    * `ask_rate_unknown_present` -- the same when it does. Higher, because an
      unknown word is the thing a clarify needs; a modelling decision on top of
      the data, which has no knowledge labels.
    * `react_rate` -- P(says something without asking | did not ask). Small:
      acknowledge + remark are 12% of the turns that engage with a reply.
    * `continue_rate` -- P(takes another turn | the system just answered). 56%
      of engaged turns in the data were followed by another; kept at 0.3-0.45.
    * `max_turns`.

    Together these make the expected number of user turns per thread exactly
    computable, which is what `metrics.engagement_deviation` needs.

    **Act mix** -- what a turn is, given that one happens. Measured shares over
    the turns that engage with the reply: extend 58%, check-belief 14%,
    redirect 11%, clarify 6%, remark 9%, acknowledge 3%.

    * `clarify_share` -- P(asks about the unfamiliar term | a question, and an
      unfamiliar term is present). The archetype tilt lives here and only
      here: a beginner clarifies, an expert asks for more on what it holds.
      Not measured -- WildChat says nothing about what the user knew -- and
      stated as a fixture decision.
    * `check_belief_share` -- P(check-belief | a question about a held term);
      the rest are `extend`. Measured 5/26 = 0.19 overall.
    * `remark_share` -- P(remark | a reaction); the rest are acknowledgements.
    * `context_rate` -- P(a question carries one clause of purpose or
      self-disclosure). Measured 14/46 = 0.30 of questions.
    * `gap_admit_rate` -- P(a clarify carries a short "lost me there" tail).
    * `redirect_rate` -- P(a continuation turn repairs the earlier question).
      Measured 4/36 of engaged turns.

    The forum-register parameters (`hedge_rate`, `correct_rate`,
    `bare_reaction_rate`, `specific_fact_rate`, `first_person_rate`) are gone
    with the behaviours they drove. A fixture that still declares one is
    refused rather than silently ignored, so dead configuration cannot look
    live.
    """

    median_words: int
    variance: float
    lowercase: bool
    drop_terminal_punct_rate: float = 0.50
    # Turn rates.
    ask_rate_default: float = 0.25
    ask_rate_unknown_present: float = 0.45
    react_rate: float = 0.08
    continue_rate: float = 0.35
    max_turns: int = 3
    # Act mix.
    clarify_share: float = 0.60
    check_belief_share: float = 0.20
    remark_share: float = 0.50
    context_rate: float = 0.30
    gap_admit_rate: float = 0.10
    redirect_rate: float = 0.12

    # Retired forum-register parameters. Declaring one is a fixture error.
    RETIRED = (
        "hedge_rate",
        "correct_rate",
        "bare_reaction_rate",
        "specific_fact_rate",
        "first_person_rate",
        "ask_back_rate",
    )

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> StyleSpec:
        stale = [k for k in cls.RETIRED if k in raw]
        if stale:
            raise FixtureError(
                f"style declares retired forum-register parameter(s) {stale}. "
                "Persona speech is calibrated from "
                "research/assistant_reply_style_profile.md; these fields drove "
                "behaviours (asserting facts, correcting, hedged assertions) that "
                "people do not perform toward an assistant, and nothing reads "
                "them any more."
            )
        return cls(
            median_words=int(raw["median_words"]),
            variance=float(raw["variance"]),
            lowercase=bool(raw["lowercase"]),
            drop_terminal_punct_rate=float(raw.get("drop_terminal_punct_rate", 0.50)),
            ask_rate_default=float(raw.get("ask_rate_default", 0.25)),
            ask_rate_unknown_present=float(raw.get("ask_rate_unknown_present", 0.45)),
            react_rate=float(raw.get("react_rate", 0.08)),
            continue_rate=float(raw.get("continue_rate", 0.35)),
            max_turns=int(raw.get("max_turns", 3)),
            clarify_share=float(raw.get("clarify_share", 0.60)),
            check_belief_share=float(raw.get("check_belief_share", 0.20)),
            remark_share=float(raw.get("remark_share", 0.50)),
            context_rate=float(raw.get("context_rate", 0.30)),
            gap_admit_rate=float(raw.get("gap_admit_rate", 0.10)),
            redirect_rate=float(raw.get("redirect_rate", 0.12)),
        )


@dataclass(frozen=True)
class NoiseSpec:
    """The noisy persona's two specific, targeted behaviours.

    Both behaviours were built to attack `assumed`, the weakest evidence in the
    model. `assumed` is gone; the behaviours are kept because they are now
    regression witnesses rather than attacks:

    * `silent_when_ignorant_rate` -- it meets a term it does not understand and
      says nothing about it. Silence used to promote a term after enough
      unquestioned exposures; it now promotes nothing, so this behaviour can no
      longer move the ledger at all, and that is the finding.
    * `asks_about_known_rate` -- it asks about a term it does understand. That
      costs a turn and pushes a term that could have reached `confirmed` into
      `explained`. Harmless to precision, costly to interactions-to-threshold.

    Both are drawn from a per-term seeded RNG, so a noisy persona is noisy
    identically on every run and is sheepish about the same specific words.

    `never_engages` is a **label, not a branch.** The disengaged persona's
    silence is configured -- its three turn rates are zero in the fixture -- so
    that its expected turn count is computed by the same arithmetic as everyone
    else's and its deviation from configuration is a real zero rather than a
    special case that cannot deviate. `_validate` enforces the agreement
    between the flag and the rates, so the two cannot drift apart.
    """

    silent_when_ignorant_rate: float = 0.0
    asks_about_known_rate: float = 0.0
    never_engages: bool = False

    @property
    def is_noisy(self) -> bool:
        return (
            self.silent_when_ignorant_rate > 0
            or self.asks_about_known_rate > 0
            or self.never_engages
        )

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> NoiseSpec:
        if not raw:
            return cls()
        return cls(
            silent_when_ignorant_rate=float(raw.get("silent_when_ignorant_rate", 0.0)),
            asks_about_known_rate=float(raw.get("asks_about_known_rate", 0.0)),
            never_engages=bool(raw.get("never_engages", False)),
        )


@dataclass(frozen=True)
class GroupSpec:
    """The persona's one group.

    Falling behind is counted from the event queue by `scope.behind_count`,
    not modelled here; ledger concepts do not go stale.
    """

    name: str
    description: str
    poll_interval_minutes: int
    # The domain glossary. System-visible: it is the jargon that appears in the
    # group's news items, not a claim about this person.
    vocabulary: tuple[str, ...] = ()
    # The glossary partitioned into subdomains: {name: (terms...)}. Each
    # vocabulary term in exactly one; a subdomain may also carry anchor terms
    # outside the vocabulary. Harness-only: it exists so a persona can hold a
    # weight per subdomain, and it never enters `system_visible_group`.
    subdomains: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class PersonaEvent:
    """One real, verified news item from `personas/research/*.md`.

    `should_be_material` is the answer key for the materiality metric and is
    held here only so the feed can put it in its side registry. It is never
    included in the dict handed to the Monitor.

    `concepts` is not an answer key: it is which glossary terms this item's
    text contains, which is exactly what a model reading the headline sees.
    """

    headline: str
    detail: str
    source_url: str
    source_name: str
    should_be_material: bool
    concepts: tuple[str, ...] = ()
    occurred_hours_ago: float = 2.0
    significance: str = ""  # 'major' | 'borderline' | 'minor', from the research file


@dataclass(frozen=True)
class GoalSpec:
    description: str
    deadline_hours_from_start: float


@dataclass(frozen=True)
class Persona:
    id: str
    display_name: str
    archetype: str
    profile: dict[str, Any]
    group: GroupSpec
    knows: tuple[str, ...]
    # Concepts that are numbers -- an amount, a rate, a valuation. Only
    # these may take a template that talks about the term being higher,
    # lower or wrong. Undeclared means not a quantity, so an unmaintained
    # fixture degrades to blander questions rather than nonsense.
    quantities: frozenset[str]
    does_not_know: tuple[str, ...]
    style: StyleSpec
    events: tuple[PersonaEvent, ...]
    noise: NoiseSpec
    seed: int
    interactions: int
    # How this person reads a briefing. Attention, never comprehension -- see
    # `harness/reading.py`. Held on the persona because it is a property of the
    # reader, and fed to `record_reading(..., source="simulated")`.
    reading: ReadingProfile = field(default_factory=lambda: profile_for("careful"))
    # The two learning traits (`harness/learning.py`). REQUIRED in the fixture:
    # `knows` / `does_not_know` are now only the t = 0 seed of a memory model,
    # and a persona without a memory rate has no forgetting curve to be scored
    # against. The dataclass default exists only so `verify_offline` can
    # rebuild a Persona from `__dict__`; `_parse` never uses it.
    learning: LearningSpec = field(
        default_factory=lambda: LearningSpec(prior_knowledge=0.5, memory_rate=1.0)
    )
    # Plausible domain terms that were never shown, for the post-session
    # probe's confabulation check (`harness/probe.py`). Optional; the probe
    # carries a default list per hand-built fixture and a generic fallback.
    pseudo_terms: tuple[str, ...] = ()
    goal: GoalSpec | None = None
    notes: str = ""
    # How at home this person is in each of the group's subdomains, 0.0-1.0,
    # keyed by the same names as `group.subdomains`. Ground truth. Empty for a
    # legacy fixture, in which case `knows` / `does_not_know` decide everything.
    familiar_subdomains: dict[str, float] = field(default_factory=dict)

    @property
    def is_noisy(self) -> bool:
        return self.noise.is_noisy

    @property
    def subdomain_map(self) -> SubdomainMap | None:
        """The taxonomy plus this persona's weights, or None for a legacy fixture."""
        if not self.group.subdomains or not self.familiar_subdomains:
            return None
        return SubdomainMap(
            terms=dict(self.group.subdomains), weights=dict(self.familiar_subdomains)
        )

    @property
    def concept_universe(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.knows) | set(self.does_not_know)))

    def system_visible_group(self) -> dict[str, Any]:
        """The only projection of a persona that may reach the system.

        Deliberately a whitelist rather than a copy-with-deletions: a new
        ground-truth field added to the fixture cannot leak through this by
        being forgotten.
        """
        return {
            "name": self.group.name,
            "description": self.group.description,
            "poll_interval_minutes": self.group.poll_interval_minutes,
        }


class FixtureError(ValueError):
    """A fixture that would silently produce a meaningless number."""


def _norm(terms: Iterable[str]) -> tuple[str, ...]:
    """Normalise concept terms the way the store does.

    `store._touch_concept` and `note_exposure` both `.strip().lower()` the term
    before writing it, so a fixture that says "Down Round" and a ledger row that
    says "down round" are the same concept. Normalising here rather than at
    comparison time means every downstream set operation is a plain set
    operation.
    """
    out: list[str] = []
    for term in terms:
        cleaned = str(term).strip().lower()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return tuple(out)


def _parse(raw: dict[str, Any]) -> Persona:
    group = raw["group"]
    learning = LearningSpec.from_raw(raw.get("learning"), persona_id=raw["id"])
    # Parsed and validated together: the taxonomy, the weights, the labelled
    # lists and K must all agree, and `SubdomainMap.from_raw` says how they
    # disagree when they do. None for a fixture that declares neither.
    smap = SubdomainMap.from_raw(
        group.get("subdomains"),
        raw.get("familiar_subdomains"),
        persona_id=raw["id"],
        vocabulary=_norm(group.get("vocabulary", [])),
        knows=_norm(raw.get("knows", [])),
        does_not_know=_norm(raw.get("does_not_know", [])),
        prior_knowledge=learning.prior_knowledge,
    )
    return Persona(
        id=raw["id"],
        display_name=raw["display_name"],
        archetype=raw["archetype"],
        profile=raw.get("profile", {}),
        group=GroupSpec(
            name=group["name"],
            description=group["description"],
            poll_interval_minutes=int(group.get("poll_interval_minutes", 360)),
            vocabulary=_norm(group.get("vocabulary", [])),
            subdomains=dict(smap.terms) if smap is not None else {},
        ),
        familiar_subdomains=dict(smap.weights) if smap is not None else {},
        knows=_norm(raw.get("knows", [])),
        quantities=frozenset(
            t.strip().lower() for t in raw.get('quantities', [])
        ),
        does_not_know=_norm(raw.get("does_not_know", [])),
        style=StyleSpec.from_raw(raw["style"]),
        events=tuple(
            PersonaEvent(
                headline=e["headline"],
                detail=e["detail"],
                source_url=e["source_url"],
                source_name=e["source_name"],
                should_be_material=bool(e["should_be_material"]),
                concepts=_norm(e.get("concepts", [])),
                occurred_hours_ago=float(e.get("occurred_hours_ago", 2.0)),
                significance=e.get("significance", ""),
            )
            for e in raw["events"]
        ),
        noise=NoiseSpec.from_raw(raw.get("noise")),
        seed=int(raw.get("seed", 20260901)),
        interactions=int(raw.get("interactions", 8)),
        reading=profile_for(str(raw.get("reading", "careful"))),
        learning=learning,
        pseudo_terms=_norm(raw.get("pseudo_terms", [])),
        goal=(
            GoalSpec(
                description=raw["goal"]["description"],
                deadline_hours_from_start=float(
                    raw["goal"]["deadline_hours_from_start"]
                ),
            )
            if raw.get("goal")
            else None
        ),
        notes=raw.get("notes", ""),
    )


def _validate(personas: list[Persona]) -> None:
    """Catch the fixture mistakes that corrupt a metric instead of crashing.

    Every check here exists because its absence would produce a plausible
    number rather than an exception -- which is the only kind of fixture bug
    that actually costs anything.
    """
    seen_groups: dict[str, str] = {}
    seen_headlines: dict[str, str] = {}

    for persona in personas:
        # --- Turn rates and reading profile -------------------------------
        style = persona.style
        rates = {
            "ask_rate_default": style.ask_rate_default,
            "ask_rate_unknown_present": style.ask_rate_unknown_present,
            "react_rate": style.react_rate,
            "continue_rate": style.continue_rate,
            "clarify_share": style.clarify_share,
            "check_belief_share": style.check_belief_share,
            "remark_share": style.remark_share,
            "context_rate": style.context_rate,
            "gap_admit_rate": style.gap_admit_rate,
            "redirect_rate": style.redirect_rate,
        }
        for name, value in rates.items():
            if not 0.0 <= value <= 1.0:
                raise FixtureError(
                    f"{persona.id!r}: {name}={value} is not a probability. "
                    "`metrics.engagement_deviation` computes an expected turn "
                    "count from these; a value outside [0,1] makes the "
                    "expectation meaningless rather than merely wrong."
                )
        if style.max_turns < 0:
            raise FixtureError(f"{persona.id!r}: max_turns must be >= 0.")

        if persona.reading.name not in READING_PROFILES:
            raise FixtureError(
                f"{persona.id!r}: reading profile {persona.reading.name!r} is "
                f"not one of {sorted(READING_PROFILES)}."
            )

        # The disengaged persona's silence must be CONFIGURED, not special-
        # cased in the responder. If the flag and the rates disagree, the
        # deviation metric would report a persona deviating from a
        # configuration the code was overriding anyway.
        if persona.noise.never_engages:
            live = {
                k: v
                for k, v in rates.items()
                if k in ("ask_rate_default", "ask_rate_unknown_present", "react_rate")
                and v > 0.0
            }
            if live:
                raise FixtureError(
                    f"{persona.id!r} declares never_engages but has non-zero "
                    f"{sorted(live)}. The disengaged persona's silence has to be "
                    "in its configuration, or its deviation from configuration "
                    "is a special case pretending to be a measurement."
                )

        if persona.archetype not in ARCHETYPES:
            raise FixtureError(
                f"{persona.id!r}: archetype {persona.archetype!r} is not one of "
                f"{ARCHETYPES}. The length table in reply_style_profile.md §8.2 "
                "and metrics.ARCHETYPE_P90 are keyed on these names."
            )

        if persona.group.name in seen_groups:
            raise FixtureError(
                f"Group name {persona.group.name!r} is used by both "
                f"{seen_groups[persona.group.name]!r} and {persona.id!r}. Group "
                "names key the offline stubs' side channel; they must be unique."
            )
        seen_groups[persona.group.name] = persona.id

        # The whole headline metric is a set comparison. If a term were in both
        # sets the comparison would still run and would still produce a number,
        # and the number would be meaningless.
        overlap = set(persona.knows) & set(persona.does_not_know)
        if overlap:
            raise FixtureError(
                f"{persona.id!r}: {sorted(overlap)} appear in both `knows` and "
                "`does_not_know`. A concept cannot be simultaneously ground "
                "truth for and against."
            )

        if not persona.knows and not persona.does_not_know:
            raise FixtureError(
                f"{persona.id!r} declares no concepts at all; ledger precision "
                "and recall would both be undefined."
            )

        vocab = set(persona.group.vocabulary)
        # The glossary and the scored concept set must be the same set, in both
        # directions, and each direction fails differently:
        #
        #   scored but not in the glossary -- unreachable. The system can only
        #     raise a term the briefing contains, so these deflate recall for a
        #     reason that has nothing to do with the system.
        #
        #   in the glossary but not scored -- worse, because it is silent. Such
        #     a term can still be written to the ledger in a known state (the
        #     briefing can gloss it, the persona can use it), at which point it
        #     counts as believed-known with no ground truth to check it
        #     against, and it lands in the precision denominator as an
        #     automatic false positive.
        stray = set(persona.concept_universe) - vocab
        if stray:
            raise FixtureError(
                f"{persona.id!r}: {sorted(stray)} are in the persona's concept "
                "sets but not in `group.vocabulary`, so no briefing can raise them."
            )
        unscored = vocab - set(persona.concept_universe)
        if unscored:
            raise FixtureError(
                f"{persona.id!r}: {sorted(unscored)} are in `group.vocabulary` "
                "but in neither `knows` nor `does_not_know`. Every glossary term "
                "can reach the ledger and be believed known, so an unscored one "
                "is a guaranteed false positive with no ground truth behind it."
            )

        # A pseudo-term that is really in the glossary is not a pseudo-term,
        # and the confabulation flag it produces would be scored the wrong way.
        fake_real = set(persona.pseudo_terms) & vocab
        if fake_real:
            raise FixtureError(
                f"{persona.id!r}: pseudo_terms {sorted(fake_real)} are in "
                "`group.vocabulary`. A pseudo-term must be a plausible domain "
                "term that was never shown; the probe's confabulation check "
                "depends on it not existing."
            )

        for event in persona.events:
            key = f"{persona.group.name}::{event.headline}"
            if key in seen_headlines:
                raise FixtureError(
                    f"Duplicate event headline {event.headline!r} in "
                    f"{persona.id!r}; headlines key the materiality answer key."
                )
            seen_headlines[key] = persona.id

            missing = set(event.concepts) - vocab
            if missing:
                raise FixtureError(
                    f"{persona.id!r}: event {event.headline!r} names concepts "
                    f"{sorted(missing)} that are not in `group.vocabulary`."
                )
            if not event.source_url.startswith("http"):
                raise FixtureError(
                    f"{persona.id!r}: event {event.headline!r} has no real "
                    f"source URL ({event.source_url!r}). These fixtures are "
                    "built from verified events; an invented citation defeats "
                    "the point of using them."
                )

        # A persona whose events collectively never mention a concept it is
        # scored on cannot possibly have that concept land in the ledger.
        raised = set().union(*(set(e.concepts) for e in persona.events)) if persona.events else set()
        unreachable = set(persona.concept_universe) - raised
        if unreachable:
            raise FixtureError(
                f"{persona.id!r}: {sorted(unreachable)} are scored but appear "
                "in no event's `concepts`, so no briefing can ever raise them. "
                "Either add them to an event or drop them from the concept sets."
            )

    # --- Suite-level: the zero-turn path must always be exercised ---------
    #
    # A thread where the user says nothing is a normal outcome and the metrics
    # have to survive it -- no division by zero, no scoring it as a failure. A
    # suite in which every persona always speaks would let that break without
    # anyone noticing until a real user ignored a briefing, which is the single
    # most likely thing a real user does. So the suite is required to contain a
    # persona that is configured to stay silent.
    if personas and not any(
        p.style.ask_rate_default == 0.0
        and p.style.ask_rate_unknown_present == 0.0
        and p.style.react_rate == 0.0
        for p in personas
    ):
        raise FixtureError(
            "No persona in this suite can produce a thread with zero user "
            "turns. Zero turns is a normal outcome, not an edge case, and the "
            "metrics must be exercised against it on every run."
        )


def load(only: str | None = None) -> list[Persona]:
    """Load every fixture, in filename order so runs are ordered identically."""
    # A fixture directory may also hold the generator's `events_<group>.json`
    # and other support files; a fixture is a JSON object with an `id`.
    personas = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        if path.name.startswith(("events_", "_")):
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        personas.append(_parse(raw))
    _validate(personas)
    if only:
        personas = [p for p in personas if p.id == only]
    return personas
