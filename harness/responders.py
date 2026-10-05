"""How a persona behaves in a thread -- and what it chooses to ask.

The system briefs and stops. The persona decides what to do with that: **ask
something, react without asking, or say nothing at all.** What someone chooses
to ask locates them -- "what's a derogation?" and "so does that mean yields are
down?" come from two very different readers looking at the same paragraph -- so
question generation is the most diagnostic thing this file produces, and it is
built to be faithful rather than plausible:

* A term the persona does **not** hold can only be asked about with a form that
  admits not holding it -- a definition, or a definition bridged to something
  it does hold.
* A term the persona **does** hold can only be asked about with a form that
  presupposes it -- asking for more on it, or checking a belief about it.
* A form is never emitted whose preconditions the persona fails.

`metrics.question_faithfulness` gates all of that against the persona's concept
sets, so "the question follows from what it knows" is a checked property.

**Register comes from `personas/research/assistant_reply_style_profile.md`**,
measured on real human->assistant turns that follow an informational message
(WildChat-1M, 507 kept turns, 80 hand-classified). The previous source was
Hacker News comments -- people performing opinions for an audience -- and it
produced personas that *asserted* ("yields must be down too", "the arr angle is
the real story"). People using an assistant do not do that. In 80 hand-read
turns, one offered an opinion and none explained the topic to the assistant.
What they do, in order, is:

| act | share of turns that engage with the reply | what it is here |
|---|---|---|
| `extend` | 58% | asks for more on a term it holds -- the mode |
| `check_belief` | 14% | states a belief *as a question* -- the only place knowledge is volunteered, and it arrives as a request |
| `redirect` | 11% | repairs its own earlier question -- continuation turns only |
| `clarify` | 6% | asks what a term means |
| `acknowledge` / `remark` | 3% / 9% | "thanks" / a one-clause evaluation of the reply, never a claim about the domain |

and, before any of that, **45% of informational messages get no reply at all**.
The turn rates in the fixtures sit on that number, not on the forum's.

Two implementations behind one interface:

| | |
|---|---|
| `ScriptedResponder` (default) | Deterministic composition. No API calls, no network, byte-identical across runs: every draw comes from a `Random` seeded from the fixture, the round and the turn number. |
| `LLMResponder` (`--live`) | The persona as a small agent, told what it holds and what it does not, and asked what it would say -- including that it may say nothing. |
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .personas import Persona


def _harness_persona_model() -> str:
    # Imported lazily: `harness.config` is tiny, but keeping the responder's
    # import surface to the fixtures matters for the offline tests.
    from .config import persona_model

    return persona_model()

# --- Phrase banks (assistant_reply_style_profile.md sections 2.3 and 3) -----
#
# Nothing in any bank below asserts a fact about the domain. The remark bank
# evaluates the *reply* ("the X part is a bit surprising"); the context bank is
# purpose or self-disclosure ("just trying to work out if this matters for
# me"), which is the only kind of context the data shows people attaching to a
# question. None of the context clauses contains a glossary term or a gap
# marker, so they cannot move the ledger by themselves.

# Acknowledgement and nothing else. 1 of 80 hand-read turns; people who are
# done mostly just stop. Nothing here contains a domain term or a question
# mark, so a persona restricted to this bank cannot supply evidence.
_ACKNOWLEDGEMENTS = (
    "ok",
    "thanks",
    "got it",
    "ok thanks",
    "makes sense",
    "thank you",
    "noted",
)

# A one-clause evaluation of the reply, anchored to a term the persona holds.
# Modelled on the single measured instance ("Weirds me out a bit that the
# vdevs do not have the same number of disks."). Evaluation only -- no claim,
# no explanation, no prediction.
_REMARKS_WITH_TERM = (
    "the {b} part is a bit surprising honestly",
    "didn't expect the {b} bit",
    "huh, the {b} part is odd",
    "the {b} side of it is the bit I wasn't expecting",
)
_REMARKS_BARE = (
    "huh",
    "interesting",
    "that's a bit surprising",
    "huh, ok",
)

# Purpose / self-disclosure clauses that follow a question. Measured examples:
# "I want to protect my users when they are reading", "I know very little C",
# "i can't find it anywhere", "because I really like the way imgui looks".
_CONTEXT_PURPOSE = (
    "just trying to work out if this matters for me",
    "trying to figure out whether this is a big deal or not",
    "asking because a friend mentioned it",
    "I only skimmed it",
)
# Self-disclosure of not following: real ("I know very little C"), and not
# something a person who holds most of the vocabulary says. Drawn only for the
# archetypes that do not.
_CONTEXT_SELF = (
    "asking because I only half follow this",
    "for context I'm pretty new to this",
    "I haven't been following this closely",
)

# The gap admission people actually attach to a clarify question, when they
# attach one: short, and about *following*, not about the world.
_GAP_TAILS = (
    "not sure I follow that bit",
    "lost me there",
    "that part went past me",
)

# Repairing an earlier question. Continuation turns only: "no i mean ...",
# "i mean rear windows not the ones near to drivers".
_REDIRECT_PREFIXES = (
    "sorry, i meant the {t} part --",
    "no i mean {t} --",
    "actually i meant {t}:",
)


# --- Question forms --------------------------------------------------------
#
# Each form declares what it needs from the persona's concept sets, and the
# selector will not emit one whose needs are unmet. `lacks` is the term being
# asked about and must be in `state.unknown`; `holds` are terms the question
# leans on and must all be in `state.known`. That is the whole rule, and it is
# what makes the question a readout of the persona rather than of the template
# bank.
#
# `tier` orders the forms by how much the asker is assumed to know. It is
# recorded on the turn so the report can say what shape of question a briefing
# actually drew out of each reader.
#
# **Every template is a request.** A definition-class template names the
# unknown term inside a "what's X" / "what does X mean" clause, which is the
# shape a text-only extractor can recognise as an ask (see
# `stub_judgments.extract_concept_evidence`). A holding-class template
# presupposes its term the way "so does that mean yields are down?" does: the
# knowledge is detectable from what the question takes for granted, and it is
# still a question.


@dataclass(frozen=True)
class QuestionForm:
    id: str
    tier: int  # 0 = knows nothing about it, 3 = knows enough to probe
    needs_lacking: int
    needs_held: int
    # Work for any concept, whatever kind of thing it is.
    templates: tuple[str, ...]
    # Presuppose the term is a number -- only drawn when it actually is one.
    quantity_templates: tuple[str, ...] = ()

    def satisfiable(self, lacking: list[str], held: list[str]) -> bool:
        return len(lacking) >= self.needs_lacking and len(held) >= self.needs_held

    def usable(self, term: str, quantities: frozenset[str]) -> tuple[str, ...]:
        """Templates that make sense for this term."""
        if term and term.lower() in quantities:
            return self.templates + self.quantity_templates
        return self.templates


QUESTION_FORMS: tuple[QuestionForm, ...] = (
    # Tier 0 -- `clarify`. They do not hold the word and ask what it is.
    # Measured: "What is chronography technique in environmental monitoring",
    # "what do you mean by explicitly mapping?", "What does 64-bit mean?"
    QuestionForm(
        "definition",
        0,
        1,
        0,
        (
            "what's {a}?",
            "what is {a}?",
            "what does {a} mean here?",
            "what do you mean by {a}?",
            "{a}?",
            "sorry, what's {a} in this?",
        ),
    ),
    # Tier 1 -- `clarify` with somewhere to put it. Still a definition ask
    # for {a}; the held term {b} is the thing they are trying to attach it to.
    QuestionForm(
        "bridge",
        1,
        1,
        1,
        (
            "what's {a} -- is that related to {b} or separate?",
            "what does {a} mean here, and does it change the {b} side of it?",
            "what is {a}, is it a kind of {b}?",
        ),
    ),
    # Tier 2 -- `extend`. Holds the term and asks for more. The mode of the
    # data (58% of engaged turns). Measured: "Who came up with these
    # formulas?", "Do they provide service in China mainland", "Why is human
    # diff from other animals (in terms of sickness)", "give me the steps".
    QuestionForm(
        "extend",
        2,
        0,
        1,
        (
            "why does {b} matter here?",
            "what does that mean for {b}?",
            "what happens to {b} after this?",
            "does this change anything for {b}?",
            "how does this affect {b}?",
            "what about {b}, does that move too?",
            "can you say more about the {b} side of it?",
            "more on the {b} part?",
        ),
        quantity_templates=(
            "is that {b} figure high or low for them?",
            "how does that {b} figure compare to normal?",
        ),
    ),
    # Tier 3 -- `check_belief`. States a belief about a held term, as a
    # question. This is the presupposition channel: the ledger can read that
    # the asker holds {b} from what the question takes for granted, exactly as
    # it could from "yields must be down" -- but this is a request, which is
    # what people actually type. Measured: "Basically, if you impersonate
    # people in order to decieve, it's wrong?", "Do you high weight could be
    # also disadvantage?", "So the same concept can be applied to NASCAR,right?"
    QuestionForm(
        "check_belief",
        3,
        0,
        1,
        (
            "so does that mean {b} is affected too?",
            "so is this basically a {b} thing?",
            "i thought {b} worked the other way -- is that wrong?",
            "does that mean {b} changes as well, or no?",
            "so {b} is the reason for this, right?",
            "is {b} the same thing here, or am I mixing it up?",
        ),
        quantity_templates=(
            "so is {b} lower than usual then?",
            "so that {b} figure is higher than normal, right?",
            "does that mean {b} comes in lower than last time?",
        ),
    ),
    # Tier 3 -- `relation`. A check-belief that puts two held terms in
    # relation. The one that separates a well-informed reader from a competent
    # one, and still a question.
    QuestionForm(
        "relation",
        3,
        0,
        2,
        (
            "so is {c} the reason for the {b} here, or is that separate?",
            "do {b} and {c} move together in this, or not?",
            "is the {b} part the same story as {c}, or different?",
        ),
        quantity_templates=(
            "is the {b} number the same story as the {c} one, or different?",
        ),
    ),
)

FORM_BY_ID = {f.id: f for f in QUESTION_FORMS}

# Forms whose asker is admitting they do not hold the term, versus forms whose
# asker is presupposing it. `metrics.question_faithfulness` is a contingency
# table over exactly this split.
LACKING_FORMS = frozenset(f.id for f in QUESTION_FORMS if f.needs_lacking)
HOLDING_FORMS = frozenset(f.id for f in QUESTION_FORMS if not f.needs_lacking)

# The two holding-class families, so the check-belief share can be a
# configured rate rather than a weight buried in the selector.
_EXTEND_FORMS = ("extend",)
_CHECK_BELIEF_FORMS = ("check_belief", "relation")

# --- A skip means they did not read it ----------------------------------------
#
# The live read-through (`eval-runs/live/probe4_sam.json`) had a persona skip
# 2% of a briefing and then ask three sharp questions about its contents. A
# skip is a glance at the top of the page and a close (`reading._skip`: scroll
# < 0.15, dwell < 0.2 of a read); what a glance reaches is the first sentence.
# So after a `skip` act the first-turn probability is multiplied by this factor
# and any question may target only terms in the briefing's first sentence (and
# whatever the thread added after the briefing, which they did read). A `skim`
# leaves the rates alone: a skimmer's eyes pass over the whole thing.
SKIP_FIRST_TURN_FACTOR = 0.15

_SENTENCE_END = re.compile(r"(?<=[.?!])\s+")


def first_sentence(text: str) -> str:
    """The part of a briefing a glance reaches."""
    parts = _SENTENCE_END.split((text or "").strip(), maxsplit=1)
    return parts[0] if parts else ""


def glanced_text(briefing: str, visible_text: str) -> str:
    """What a reader who SKIPPED the briefing has actually seen of the thread:
    the briefing's first sentence, plus everything the thread added after the
    briefing (the persona's own turns and the answers it read)."""
    head = first_sentence(briefing)
    tail = ""
    if briefing and visible_text.startswith(briefing):
        tail = visible_text[len(briefing):]
    return f"{head}{tail}"


# --- Persona live state ----------------------------------------------------


@dataclass
class PersonaState:
    """A persona's live concept state. Ground truth -- never shown the system.

    `known` and `unknown` are the two sets every decision in this file reads:
    a term in `unknown` can be asked about with a lacking-class form, a term in
    `known` can be leaned on with a holding-class form. Where they come from
    depends on whether a memory model is attached:

    * **With `memory`** (the runner always attaches one) the sets are derived
      from `harness/learning.py` at the simulated moment by `refresh(now)`:
      `unknown` is every glossary term with P(can define) < 0.5 and `known` is
      every term with P(can use in a new context) >= 0.5. The fixture's lists
      are only the t = 0 seed. A term can be in neither set -- recognised or
      definable but not yet met in enough contexts to lean on -- and such a
      term is neither asked about nor presupposed. The runner calls
      `refresh` before every `plan_turn` and after every memory event, so the
      responders never see a stale set.
    * **Without one** (unit tests of the templates) the sets are the static
      lists and evolve in exactly one way: `learn` moves an asked-and-answered
      term from unknown to known.

    Note what still does not count as learning on the static path: a term
    glossed in a briefing the persona never asked about. The Assessor is
    explicit that an unprompted definition is exposure and not knowledge. On
    the memory path a glossed read *may* encode a short-lived trace (~0.3
    recall after a week), which is the literature's number rather than the
    Assessor's rule; `metrics.reading_regression` still gates that reading
    moves nothing in the *ledger*.
    """

    known: set[str]
    unknown: set[str]
    learned: set[str] = field(default_factory=set)
    # Terms this persona has already used or asked about in an earlier thread.
    # People do not ask the same question twice, so a spent term is
    # deprioritised.
    mentioned: set[str] = field(default_factory=set)
    asked_ever: set[str] = field(default_factory=set)
    # The memory model (`learning.MemoryModel`), when attached. Typed loosely
    # so this module does not import the model.
    memory: Any = None

    @classmethod
    def for_persona(
        cls, persona: Persona, memory: Any = None, now: Any = None
    ) -> PersonaState:
        state = cls(
            known=set(persona.knows), unknown=set(persona.does_not_know), memory=memory
        )
        if memory is not None and now is not None:
            state.refresh(now)
        return state

    def refresh(self, now: Any) -> None:
        """Re-derive `known` / `unknown` from the memory model at `now`.

        The one place persona behaviour reads memory instead of the static
        lists. A no-op without a memory model.
        """
        if self.memory is None:
            return
        self.unknown = set(self.memory.askable_terms(now))
        self.known = set(self.memory.presupposable_terms(now))

    def learn(self, terms: Any) -> list[str]:
        """The system answered a question about these; record that.

        On the static path the term moves from unknown to known. On the memory
        path the runner applies `asked_and_answered` to the model and then
        `refresh`es; this only records which terms were learned by asking.
        """
        newly: list[str] = []
        for raw in terms or ():
            term = str(raw).strip().lower()
            if term and term in self.unknown:
                if self.memory is None:
                    self.unknown.discard(term)
                    self.known.add(term)
                self.learned.add(term)
                newly.append(term)
        return sorted(newly)


@dataclass(frozen=True)
class BriefingView:
    """What the system actually put in front of the persona.

    A plain projection of the contract's `Briefing`: the substance only. The
    system does not ask the user anything, so there is no question field.
    """

    exchange_id: str
    topic: str
    briefing: str
    explained_terms: tuple[str, ...] = ()
    terms_used: tuple[str, ...] = ()
    # How the persona READ it -- one of `reading.READING_ACTS` (skip / skim /
    # read / study), or "" when unknown (unit tests of the templates). The
    # runner sets it after the reading act and before the thread; the
    # responders use it to decide what the persona can have seen.
    reading_act: str = ""

    @property
    def text(self) -> str:
        return self.briefing

    @property
    def skipped(self) -> bool:
        return self.reading_act == "skip"

    def seen_text(self, visible_text: str) -> str:
        """The part of the thread this reader has actually taken in."""
        if self.skipped:
            return glanced_text(self.briefing, visible_text)
        return visible_text

    @property
    def chars(self) -> int:
        return len(self.briefing)


@dataclass(frozen=True)
class TurnIntent:
    """One user turn, and what the persona meant by it.

    Answer key for evidence extraction: `asks_about`, `presupposes` and
    `mentions` are what the persona was actually doing, against which the
    system's read of the thread is scored. Refused offline for that reason.
    """

    text: str
    kind: str  # 'question' | 'reaction'
    form: str  # a QuestionForm id, or a reaction form ('acknowledge' / 'remark')
    tier: int
    words: int
    target_words: int
    # Terms the persona does NOT hold and is asking about.
    asks_about: tuple[str, ...] = ()
    # Terms the persona DOES hold and the question leans on. A form that
    # presupposes a term the persona lacks is a faithfulness violation.
    presupposes: tuple[str, ...] = ()
    # Terms used declaratively in a remark.
    mentions: tuple[str, ...] = ()
    admitted_gap: bool = False
    carried_context: bool = False
    redirected: bool = False
    # Noise bookkeeping.
    stayed_silent_on: tuple[str, ...] = ()
    asked_about_known: tuple[str, ...] = ()


class Responder(Protocol):
    def plan_turn(
        self,
        *,
        persona: Persona,
        state: PersonaState,
        briefing: BriefingView,
        index: int,
        turn_no: int,
        visible_text: str,
    ) -> TurnIntent | None: ...


# --- Length distribution (reply_style_profile.md section 8.2) --------------
#
# The one thing kept from the forum profile: length is a distribution with real
# spread, and a persona whose length variance is too low is the most likely
# single failure of this harness. `metrics.style_fidelity` gates on the
# configured distribution. Under the assistant register almost every turn is a
# question and questions are short (median 9 words for turns that engage with
# the reply), so the observed column sits well under the configured one; that
# is a property of the register and is reported, not gated.

_QUANTILES: tuple[tuple[float, float], ...] = (
    (0.00, 0.20),
    (0.10, 0.32),
    (0.25, 0.50),
    (0.50, 1.00),
    (0.75, 2.00),
    (0.90, 3.50),
    (1.00, 5.00),
)


def target_words(rng: random.Random, median: int, variance: float) -> int:
    """Draw a length from the archetype's distribution by inverse-CDF."""
    u = rng.random()
    lo_p, lo_r = _QUANTILES[0]
    for hi_p, hi_r in _QUANTILES[1:]:
        if u <= hi_p:
            span = hi_p - lo_p
            frac = 0.0 if span <= 0 else (u - lo_p) / span
            ratio = lo_r + frac * (hi_r - lo_r)
            break
        lo_p, lo_r = hi_p, hi_r
    else:  # pragma: no cover
        ratio = _QUANTILES[-1][1]
    scaled = 1.0 + variance * (ratio - 1.0)
    return max(2, int(round(median * scaled)))


def _words(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def term_pattern(term: str) -> re.Pattern[str]:
    """Word-boundary matcher for one glossary term.

    Plain substring matching is wrong here and quietly so: `arr` matches
    "Barrett", `cru` matches "crushed", `var` matches "variety". Every one of
    those would land a spurious concept in the ledger.
    """
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


def terms_in(text: str, terms: tuple[str, ...]) -> list[str]:
    """Which glossary terms actually appear in what the persona was shown."""
    return [t for t in terms if term_pattern(t).search(text or "")]


def noise_targets(
    persona: Persona, state: PersonaState, text: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Terms this persona goes quiet on, and terms it asks about anyway.

    Module-level, deterministic, and keyed on the *term* rather than on the
    round: people are consistently sheepish about particular words rather than
    flipping a coin each time they meet the same one, and the runner has to be
    able to record what a persona went quiet on even when it took no turns at
    all -- which is the common case.

    Both responders call this, so the scripted and live personas make the same
    decision and only their prose differs.
    """
    noise = persona.noise
    present = terms_in(text, persona.group.vocabulary)
    silent = tuple(
        sorted(
            t
            for t in present
            if t in state.unknown
            and random.Random(f"{persona.seed}:{persona.id}:silent:{t}").random()
            < noise.silent_when_ignorant_rate
        )
    )
    ask_known = tuple(
        sorted(
            t
            for t in present
            if t in state.known
            and random.Random(f"{persona.seed}:{persona.id}:askknown:{t}").random()
            < noise.asks_about_known_rate
        )
    )
    return silent, ask_known


# --- Turn-rate arithmetic --------------------------------------------------
#
# Kept as plain functions because `metrics.engagement_deviation` has to be able
# to compute what a persona's configuration *predicts* for the briefings this
# run actually produced. Absolute levels measure the persona. Deviation at
# equal configuration measures the briefing.


def p_first_turn(style: Any, unknown_present: bool) -> float:
    """P(the persona says anything at all after this briefing)."""
    ask = style.ask_rate_unknown_present if unknown_present else style.ask_rate_default
    return ask + (1.0 - ask) * style.react_rate


def p_first_turn_is_question(style: Any, unknown_present: bool) -> float:
    return style.ask_rate_unknown_present if unknown_present else style.ask_rate_default


def skip_adjusted_rates(ask_rate: float, react_rate: float) -> tuple[float, float]:
    """The (ask, react) rates that put P(first turn) at SKIP_FIRST_TURN_FACTOR
    times its configured value after a skip, keeping the question share.

    P(first turn) = a + (1 - a) r. Scaling a by f keeps the question share at
    f of what it was; r' is then solved so the reaction share scales by the
    same f: (1 - f a) r' = f (1 - a) r.
    """
    f = SKIP_FIRST_TURN_FACTOR
    a2 = f * ask_rate
    denom = 1.0 - a2
    r2 = (f * (1.0 - ask_rate) * react_rate / denom) if denom > 0 else 0.0
    return a2, min(1.0, max(0.0, r2))


def p_first_turn_after(style: Any, unknown_present: bool, reading_act: str) -> float:
    """`p_first_turn`, with the skip factor applied when the briefing was skipped."""
    p = p_first_turn(style, unknown_present)
    return p * SKIP_FIRST_TURN_FACTOR if reading_act == "skip" else p


def expected_turns_after(style: Any, unknown_present: bool, reading_act: str) -> float:
    """`expected_turns`, with the skip factor applied to the opening turn."""
    e = expected_turns(style, unknown_present)
    return e * SKIP_FIRST_TURN_FACTOR if reading_act == "skip" else e


def expected_turns(style: Any, unknown_present: bool) -> float:
    """Expected user turns in a thread, from configuration alone.

    A truncated geometric: the persona opens with probability `p_first_turn`,
    then continues after each answer with probability `continue_rate`, capped at
    `max_turns`.
    """
    p0 = p_first_turn(style, unknown_present)
    c = max(0.0, min(1.0, style.continue_rate))
    total = 0.0
    for n in range(int(style.max_turns)):
        total += c**n
    return p0 * total


# --- The scripted responder ------------------------------------------------


class ScriptedResponder:
    """Deterministic, offline, no API calls.

    `plan_turn` is called once per user turn and returns `None` to end the
    thread. **Returning `None` on the first call is a normal outcome, not an
    error**: 45% of informational messages in the data get no reply, and a
    harness that cannot represent that is measuring a world where every message
    gets one.
    """

    name = "scripted"

    def plan_turn(
        self,
        *,
        persona: Persona,
        state: PersonaState,
        briefing: BriefingView,
        index: int,
        turn_no: int,
        visible_text: str,
    ) -> TurnIntent | None:
        rng = random.Random(f"{persona.seed}:{persona.id}:{index}:{turn_no}")
        style = persona.style
        vocab = persona.group.vocabulary
        # A reader who skipped the briefing saw its first sentence and nothing
        # else of it; every term below comes from what they actually saw.
        seen = briefing.seen_text(visible_text)
        present = terms_in(seen, vocab)

        lacking = [t for t in present if t in state.unknown]
        held = [t for t in present if t in state.known]

        # --- Noise, decided per term and before anything else -------------
        silent_targets, ask_known_targets = noise_targets(persona, state, seen)
        stayed_silent = list(silent_targets)
        asked_about_known = list(ask_known_targets)
        lacking = [t for t in lacking if t not in silent_targets]

        # Do not ask the same thing twice.
        fresh_lacking = [t for t in lacking if t not in state.asked_ever]

        # --- Does a turn happen at all? -----------------------------------
        unknown_present = bool(fresh_lacking)
        roll = rng.random()
        if turn_no == 0:
            ask_rate = (
                style.ask_rate_unknown_present
                if unknown_present
                else style.ask_rate_default
            )
            react_rate = style.react_rate
            if briefing.skipped:
                ask_rate, react_rate = skip_adjusted_rates(ask_rate, react_rate)
            wants_question = roll < ask_rate
            react_roll = rng.random()
            wants_reaction = (not wants_question) and react_roll < react_rate
            if not (wants_question or wants_reaction):
                return None
        else:
            if turn_no >= style.max_turns:
                return None
            if roll >= style.continue_rate:
                return None
            # A continuation is a question when there is anything left to ask
            # about; otherwise it is an acknowledgement of the answer.
            wants_question = bool(fresh_lacking or held or asked_about_known)

        # --- Compose ------------------------------------------------------
        if wants_question:
            intent = self._question(
                rng, persona, state, fresh_lacking, held, asked_about_known,
                turn_no=turn_no,
            )
            if intent is not None:
                for term in intent.asks_about:
                    state.asked_ever.add(term)
                return _decorate(
                    intent,
                    style,
                    rng,
                    stayed_silent=stayed_silent,
                    asked_about_known=asked_about_known,
                )
            # No form was satisfiable -- nothing present that this persona
            # could ask a faithful question about. Falls through to a reaction
            # rather than inventing a question it has no basis for.

        intent = self._reaction(rng, persona, state, held, turn_no=turn_no)
        return _decorate(
            intent,
            style,
            rng,
            stayed_silent=stayed_silent,
            asked_about_known=asked_about_known,
        )

    # --- question ------------------------------------------------------

    def _question(
        self,
        rng: random.Random,
        persona: Persona,
        state: PersonaState,
        lacking: list[str],
        held: list[str],
        asked_about_known: list[str],
        *,
        turn_no: int,
    ) -> TurnIntent | None:
        """Pick the question this persona would honestly ask.

        Selection is by satisfiable preconditions and then by the configured
        act mix, in this order:

        1. If a fresh unknown term is in front of them, `clarify_share` decides
           whether they ask about *it* (definition / bridge) or ask for more on
           something they hold. This is the one place the archetype tilt lives
           -- WildChat has no knowledge labels, so how much more a beginner
           clarifies than an expert is a fixture decision, stated as such in
           `personas/__init__.py`.
        2. Within the holding-class forms, `check_belief_share` decides between
           asking for more (`extend`, the measured mode) and checking a belief
           (`check_belief` / `relation`).
        3. Within a family, the highest-tier satisfiable form is weighted
           2^tier but not locked in, so a well-informed persona does not draw
           the same form every time.
        """
        style = persona.style

        # The noisy behaviour: asks about a term it actually holds. A real
        # question, so it produces a real ask -- and it costs a turn.
        if asked_about_known and rng.random() < 0.5:
            term = asked_about_known[rng.randrange(len(asked_about_known))]
            form = FORM_BY_ID["definition"]
            text = rng.choice(form.templates).format(a=term)
            return TurnIntent(
                text=text,
                kind="question",
                form=form.id,
                tier=form.tier,
                words=_words(text),
                target_words=_words(text),
                asks_about=(term,),
                presupposes=(),
            )

        candidates = [f for f in QUESTION_FORMS if f.satisfiable(lacking, held)]
        if not candidates:
            return None

        lacking_forms = [f for f in candidates if f.needs_lacking]
        holding_forms = [f for f in candidates if not f.needs_lacking]

        # Every roll is drawn unconditionally and only then gated, so the
        # position of later draws does not depend on what happened to be in
        # the briefing.
        roll_clarify = rng.random()
        roll_belief = rng.random()
        roll_context = rng.random()
        roll_gap = rng.random()
        roll_redirect = rng.random()

        if lacking_forms and (not holding_forms or roll_clarify < style.clarify_share):
            pool = lacking_forms
        elif holding_forms:
            belief = [f for f in holding_forms if f.id in _CHECK_BELIEF_FORMS]
            extend = [f for f in holding_forms if f.id in _EXTEND_FORMS]
            if belief and (not extend or roll_belief < style.check_belief_share):
                pool = belief
            else:
                pool = extend or belief
        else:
            pool = lacking_forms

        weights = [2 ** f.tier for f in pool]
        pick = rng.random() * sum(weights)
        running = 0.0
        form = pool[-1]
        for candidate, weight in zip(pool, weights):
            running += weight
            if pick <= running:
                form = candidate
                break

        # Prefer terms this persona has not already spent.
        lack_pool = [t for t in lacking if t not in state.mentioned] or lacking
        held_pool = [t for t in held if t not in state.mentioned] or held

        a = lack_pool[rng.randrange(len(lack_pool))] if form.needs_lacking else ""
        picks: list[str] = []
        remaining = list(held_pool)
        for _ in range(form.needs_held):
            if not remaining:
                return None
            picks.append(remaining.pop(rng.randrange(len(remaining))))

        # Only draw a template whose presuppositions the term can carry: a
        # phrasing about a figure being "higher than normal" is nonsense for a
        # concept like `relegation`. `metrics.quantity_phrasing_check` gates it.
        usable = form.usable(picks[0] if picks else a, persona.quantities)
        text = rng.choice(usable).format(
            a=a,
            b=picks[0] if picks else "",
            c=picks[1] if len(picks) > 1 else "",
        )
        for term in picks:
            state.mentioned.add(term)

        # A clarify may carry the short gap tail people actually attach
        # ("lost me there"); any question may carry a purpose clause.
        admitted_gap = False
        if form.needs_lacking and roll_gap < style.gap_admit_rate:
            text = f"{text} {rng.choice(_GAP_TAILS)}"
            admitted_gap = True
        carried_context = False
        if roll_context < style.context_rate:
            bank = (
                _CONTEXT_PURPOSE
                if persona.archetype == "well_informed"
                else _CONTEXT_PURPOSE + _CONTEXT_SELF
            )
            text = f"{text} {rng.choice(bank)}"
            carried_context = True

        # A continuation turn may be a repair of the earlier question rather
        # than a fresh one: "sorry, i meant the X part -- what's that?"
        redirected = False
        if turn_no > 0 and roll_redirect < style.redirect_rate:
            focus = a or (picks[0] if picks else "")
            if focus:
                text = f"{rng.choice(_REDIRECT_PREFIXES).format(t=focus)} {text}"
                redirected = True

        return TurnIntent(
            text=text,
            kind="question",
            form=form.id,
            tier=form.tier,
            words=_words(text),
            target_words=_words(text),
            asks_about=((a,) if a else ()),
            presupposes=tuple(picks),
            admitted_gap=admitted_gap,
            carried_context=carried_context,
            redirected=redirected,
        )

    # --- reaction ------------------------------------------------------

    def _reaction(
        self,
        rng: random.Random,
        persona: Persona,
        state: PersonaState,
        held: list[str],
        *,
        turn_no: int,
    ) -> TurnIntent:
        """A turn that says something without asking anything.

        Two shapes, and only two, because the data has only two: an
        acknowledgement ("thanks") or a one-clause evaluation of the reply
        ("the yields part is a bit surprising honestly"). Neither states a
        fact about the domain. The old fact-stating and correcting paths --
        "the arr angle is the real story", "wasn't the vintage the other way
        round" -- were forum behaviour and are gone.
        """
        style = persona.style
        roll_remark = rng.random()
        target = target_words(rng, style.median_words, style.variance)

        # After the system has answered, a non-question is almost always an
        # acknowledgement; a remark is a first-turn reaction to the briefing.
        do_remark = turn_no == 0 and roll_remark < style.remark_share

        mentions: list[str] = []
        if do_remark:
            fresh = [t for t in held if t not in state.mentioned]
            pool = fresh or held
            if pool and rng.random() < 0.7:
                term = pool[rng.randrange(len(pool))]
                mentions.append(term)
                state.mentioned.add(term)
                text = rng.choice(_REMARKS_WITH_TERM).format(b=term)
            else:
                text = rng.choice(_REMARKS_BARE)
            form = "remark"
        else:
            text = rng.choice(_ACKNOWLEDGEMENTS)
            form = "acknowledge"

        return TurnIntent(
            text=text,
            kind="reaction",
            form=form,
            tier=0,
            words=_words(text),
            target_words=target,
            mentions=tuple(mentions),
        )


def _decorate(
    intent: TurnIntent,
    style: Any,
    rng: random.Random,
    *,
    stayed_silent: list[str],
    asked_about_known: list[str],
) -> TurnIntent:
    """Apply the fixed per-persona register traits and attach noise bookkeeping.

    Capitalisation is a fixed per-persona trait, never a per-reply coin flip
    (37.5% of measured turns are lowercase-initial, and it clusters by
    person). Terminal punctuation is dropped about half the time (51% of
    measured turns carry it).
    """
    text = intent.text
    if style.lowercase:
        text = text.lower()
    elif text:
        text = text[0].upper() + text[1:]

    if text and not text.endswith("?"):
        if rng.random() < style.drop_terminal_punct_rate:
            text = text.rstrip(".")
        elif not text.endswith("."):
            text = text + "."

    return TurnIntent(
        text=text,
        kind=intent.kind,
        form=intent.form,
        tier=intent.tier,
        words=_words(text),
        target_words=intent.target_words,
        asks_about=intent.asks_about,
        presupposes=intent.presupposes,
        mentions=intent.mentions,
        admitted_gap=intent.admitted_gap,
        carried_context=intent.carried_context,
        redirected=intent.redirected,
        stayed_silent_on=tuple(sorted(set(stayed_silent))),
        asked_about_known=tuple(sorted(set(asked_about_known))),
    )


# --- The live responder ----------------------------------------------------

PERSONA_SYSTEM_PROMPT = """\
You are role-playing one person who subscribed to a briefing service and has \
just been sent a short news briefing by it. You are NOT an assistant. Never \
offer help, never explain what you are doing, never write a preamble.

Who you are: {display_name}. {profile}

Terms you genuinely understand and can use correctly:
{known}

Terms you do NOT understand:
{unknown}

The briefing does not ask you anything. Nobody is waiting for an answer. You \
decide what to do with it, and these are the options, in the order real people \
choose them:

1. Say nothing at all. This is the most common outcome -- about half of \
informational messages get no reply. If you would not actually type anything, \
reply with exactly: <silence>
2. Ask for more on something in it that you understand. This is what people do \
most when they do reply: "why does that matter here?", "what happens to X after \
this?", "does this change anything for X?", "more on the X part?"
3. Check a belief, as a question. "so does that mean X is affected too?", "i \
thought X worked the other way -- is that wrong?", "so X is the reason for this, \
right?" This is the ONLY way you ever volunteer something you know: framed as a \
question for the service to confirm or correct.
4. Ask what a term means, if one you do not understand is in front of you: \
"what's X?", "what do you mean by X?", "X?"
5. Rarely, a bare acknowledgement ("thanks", "ok") or a one-clause reaction to \
the briefing ("the X part is a bit surprising honestly").

Things you never do, because people using a service like this do not do them:
- You never state a fact, prediction or explanation about the topic. Not "yields \
must be down", not "heat usually shrivels the bunches", not "the arr angle is \
the real story". If you know something, it comes out as a question (option 3) \
or it stays in your head.
- You never correct the briefing with a claim. If you think it is wrong you ask: \
"wasn't it the other way round?" -- and even that is rare.
- You never offer an opinion about the news itself. At most you evaluate the \
briefing ("that bit is surprising"), and rarely.

Rules about what you may ask, and they are strict:
- If you ask about a term from the "do NOT understand" list, ask what it means, \
or ask what it means and how it bears on something you DO understand. Never \
write a question that implies you already know it.
- If you ask about a term you DO understand, ask for more on it or check a \
belief about it. Never ask what it means -- you know what it means.
- Never write a question that leans on a term from the "do NOT understand" list \
as though you held it.
- Use the briefing's own word for a thing. People echo the term they were given; \
they do not paraphrase it.

How you write (measured from real replies to informational messages):
- Short. Median 9 words for a reply that engages with what it was sent; more \
than half are under 10 words; almost none are over 20.
- Usually a bare question, one sentence. About a third of the time the question \
carries one short clause of context, and that clause is a purpose or something \
about you ("just trying to work out if this matters for me", "I only skimmed \
it", "for context I'm pretty new to this") -- never a fact about the topic.
- {case_rule}
- Terminal punctuation is optional; drop it about half the time on anything that \
is not a question.
- Never write a preamble like "Great question" or "Thanks for the briefing".

Reply with the message text only, or <silence>. No quotes, no formatting.\
{noise_directive}"""

_NOISE_SILENT = """

IMPORTANT -- this overrides the rule about asking when you don't understand
something. These terms appear and you do NOT understand them:
{terms}
Say nothing about them. Do not use them, do not ask what they mean, do not
admit you are unsure. React to the rest as though you followed all of it. This
is the ordinary thing people do when they don't want to look uninformed, and it
must read as natural, not evasive."""

_NOISE_ASK_KNOWN = """

Also: ask what {terms} means, in a short question, even though you do actually
know it. People check terms they already know when a briefing uses one in an
unfamiliar way. Ask plainly, do not signal that you know it."""

SILENCE_TOKEN = "<silence>"

# Text-only recognisers used to label what a live persona actually wrote. The
# same shapes the offline stub extractor keys on, so a live persona's turns are
# labelled the way the offline ones are.
_DEFINITION_SHAPES = (
    r"what(?:'s| is| are|s)\s+(?:a |an |the )?{t}\b",
    r"what does\s+(?:a |an |the )?{t}\s+mean",
    r"what do you mean by\s+(?:a |an |the )?{t}\b",
    r"^\W*(?:sorry,?\s*)?(?:the\s+)?{t}\W*\??\s*$",
    r"not sure what\s+(?:a |an |the )?{t}\b",
)
_CHECK_BELIEF_OPENERS = re.compile(
    r"^\W*(so\b|basically|i thought|isn'?t (it|that|this)|wasn'?t (it|that|this)|"
    r"does (that|this|it) mean|is that (right|correct)|are you sure|right\?)",
    re.IGNORECASE,
)


def definition_asked(text: str, term: str) -> bool:
    """Does this text ask what `term` means, as opposed to presupposing it?

    Checked sentence by sentence, so "series c? I haven't been following this
    closely" is still a definition ask for `series c`.
    """
    t = re.escape(term)
    for sentence in re.split(r"(?<=[.?!])\s+", (text or "").strip()):
        if any(
            re.search(shape.format(t=t), sentence, re.IGNORECASE)
            for shape in _DEFINITION_SHAPES
        ):
            return True
    return False


@dataclass
class LLMResponder:
    """`--live`: the persona as a small agent deciding what to say, if anything.

    Three properties matter more than the prose quality:

    1. **It never goes through `Judge`.** The persona's own calls have nothing
       to do with the system under test; routing them through the judgment
       harness would write persona turns into `judgment_log` and corrupt every
       cost, latency and call-count figure in the report.
    2. **The concept sets stay here.** They reach the model playing the
       *person*, never the model playing the *system*.
    3. **It is allowed to say nothing.** `<silence>` is a first-class output,
       and under the measured register it is the modal one.
    """

    client: Any
    # Resolved by `harness.config` (env override, `--cheap`, `--persona-model`).
    model: str = field(default_factory=lambda: _harness_persona_model())
    max_tokens: int = 512
    name: str = "llm"
    transcript: list[dict[str, str]] = field(default_factory=list)
    # Token usage of the persona-voice calls. Never in `judgment_log` (they are
    # the human side of the conversation); surfaced through the artifact's
    # volatile `harness_usage` block so the cost of the voice is visible.
    usage: dict[str, int] = field(
        default_factory=lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    )

    def _system(
        self,
        persona: Persona,
        state: PersonaState,
        silent: tuple[str, ...],
        ask_known: tuple[str, ...],
    ) -> str:
        style = persona.style
        directive = ""
        if silent:
            directive += _NOISE_SILENT.format(terms=", ".join(sorted(silent)))
        if ask_known:
            directive += _NOISE_ASK_KNOWN.format(
                terms=" and ".join(f"'{t}'" for t in sorted(ask_known))
            )
        return PERSONA_SYSTEM_PROMPT.format(
            noise_directive=directive,
            display_name=persona.display_name,
            profile=persona.profile.get("bio", ""),
            known=", ".join(sorted(state.known)) or "(none)",
            unknown=", ".join(sorted(state.unknown)) or "(none)",
            case_rule=(
                "Write entirely in lowercase, always. This is how you type; "
                "never capitalise a sentence."
                if style.lowercase
                else "Capitalise and punctuate normally, always."
            ),
        )

    def plan_turn(
        self,
        *,
        persona: Persona,
        state: PersonaState,
        briefing: BriefingView,
        index: int,
        turn_no: int,
        visible_text: str,
    ) -> TurnIntent | None:
        if turn_no >= persona.style.max_turns:
            return None

        # A skipped briefing was glanced at, not read: the model is shown only
        # what a glance reaches, and the first turn happens at the skip-scaled
        # rate on top of whatever silence the model chooses itself. The gate
        # is drawn deterministically from the fixture, as the scripted
        # responder's rolls are.
        if briefing.skipped and turn_no == 0:
            gate = random.Random(f"{persona.seed}:{persona.id}:{index}:skip-gate")
            if gate.random() >= SKIP_FIRST_TURN_FACTOR:
                self.transcript.append(
                    {"persona": persona.id, "turn": "0", "text": SILENCE_TOKEN + " (skipped)"}
                )
                return None
        visible_text = briefing.seen_text(visible_text)

        # The noise *decision* is deterministic and shared with
        # `ScriptedResponder`, so the two responders stay comparable. Whether
        # the model complied is read back out of the text below.
        silent_targets, ask_known_targets = noise_targets(persona, state, visible_text)
        user = visible_text if turn_no == 0 else (
            f"{visible_text}\n\n(That is the thread so far. Say something else, "
            f"or {SILENCE_TOKEN} if you are done.)"
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self._system(persona, state, silent_targets, ask_known_targets),
            messages=[{"role": "user", "content": user}],
        )
        self.usage["calls"] += 1
        _usage = getattr(response, "usage", None)
        self.usage["input_tokens"] += int(getattr(_usage, "input_tokens", 0) or 0)
        self.usage["output_tokens"] += int(getattr(_usage, "output_tokens", 0) or 0)
        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        ).strip()
        self.transcript.append(
            {"persona": persona.id, "turn": str(turn_no), "text": text}
        )

        if not text or SILENCE_TOKEN in text.lower():
            return None

        # What the turn did is read back out of the text rather than decided up
        # front. A live persona may ignore the instruction, and the metric
        # should reflect what it actually wrote.
        vocab = persona.group.vocabulary
        present = terms_in(visible_text, vocab)
        in_text = set(terms_in(text, vocab))
        lowered = text.lower()
        is_question = "?" in text

        # A term is *asked about* when the text asks what it means; a term is
        # *presupposed* when it appears in a question that does not. That is
        # the split the ledger has to make from text alone, so it is made the
        # same way here.
        asked_shape = {t for t in in_text if definition_asked(text, t)}
        asks = tuple(
            t for t in present if t in state.unknown and t in in_text and is_question
        )
        presupposes = tuple(
            t
            for t in present
            if t in state.known and t in in_text and is_question and t not in asked_shape
        )
        mentions = tuple(
            t for t in present if t in state.known and t in in_text and not is_question
        )
        stayed_silent = tuple(sorted(t for t in silent_targets if t not in in_text))
        asked_known = tuple(
            sorted(t for t in ask_known_targets if t in in_text and is_question)
        )

        for term in asks:
            state.asked_ever.add(term)

        # Form is inferred, not chosen, so a live persona that bluffs shows up
        # as a violation rather than as a form label nobody assigned.
        if not is_question:
            form, tier = ("acknowledge" if _words(text) <= 3 else "remark"), 0
        elif asks or asked_known:
            form, tier = ("bridge", 1) if presupposes else ("definition", 0)
        elif len(presupposes) >= 2:
            form, tier = "relation", 3
        elif presupposes:
            form, tier = (
                ("check_belief", 3)
                if _CHECK_BELIEF_OPENERS.match(text)
                else ("extend", 2)
            )
        else:
            form, tier = "unattributed", 0

        return TurnIntent(
            text=text,
            kind="question" if is_question else "reaction",
            form=form,
            tier=tier,
            words=_words(text),
            target_words=persona.style.median_words,
            asks_about=tuple(sorted(set(asks) | set(asked_known))),
            presupposes=presupposes,
            mentions=mentions,
            admitted_gap=any(g in lowered for g in _GAP_TAILS),
            carried_context=len(re.split(r"(?<=[.?!])\s+", text.strip())) > 1,
            redirected=bool(re.match(r"^\W*(no,? i mean|sorry,? i meant|actually i meant)", lowered)),
            stayed_silent_on=stayed_silent,
            asked_about_known=asked_known,
        )
