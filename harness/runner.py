"""The replay runner: drives one persona through its timeline.

The system under test is the real one, assembled by `app.build`. Exactly three
things are substituted, each at a seam the foundation already provides:

  1. the clock      -- `clock.installed()` rebinds `store.now_iso`
  2. the LLM client -- `judgment.LLMClient`, the protocol `app.build` accepts
  3. the event feed -- `monitor.EventFeed`, likewise

A fourth thing is *added* rather than substituted: the persona, which stands
where a human reads and types, and reaches the system only through a reading
measurement and a sequence of message strings.

**The loop is now a thread, not a turn.** The system briefs and stops. The
persona reads it -- observably, and that observation is recorded. Then it may
ask something, or react, or say nothing; if it asks, the system answers, and it
may ask again. Nothing in this loop lets the system decide the conversation is
over: the thread ends when `plan_turn` returns `None`, which is the persona's
decision and only the persona's.

    advance clock -> stage the event -> Monitor.poll -> Orchestrator.open_session
    -> contract.raise_topic          (the briefing; no question in it)
    -> persona reads it              (dwell + scroll -> record_reading)
    -> while the persona has something to say:
           add_turn('user', ...) -> contract.respond -> add_turn('system', ...)
    -> contract.close_thread         (evidence from the WHOLE thread)
    -> snapshot the ledger against the persona's true concept set

Four structural choices worth stating.

*   **Zero user turns is a normal outcome.** It is what most briefings get.
    Nothing downstream may treat it as an error, a failure, or a zero to divide
    by, and the fixture loader refuses a suite in which it cannot happen.

*   **Reading behaviour goes to `record_reading` and nowhere else.** It is
    never passed to a judgment call, never handed to the responder as a reason
    to learn something, and never consulted when writing the ledger.
    `metrics.reading_regression` is the gate; this loop is the thing it gates.

*   **Each persona's clock restarts at EPOCH.** A shared advancing clock would
    make persona 3's timeline depend on how many rounds personas 1 and 2
    happened to have. Resetting per persona makes each timeline a pure function
    of its own fixture, so `--persona X` reports the same numbers alone as in a
    full run.

*   **Ground truth reaches the persona and the scorer directly, never the
    system.** `Persona.system_visible_group()` is a whitelist and the only
    persona-derived dict that enters a component call. `ANSWER_KEY_FIELDS` and
    `READING_FIELDS` below name what must never appear in
    `judgment_log.input_json`, and both are asserted with independent scans.
"""

from __future__ import annotations

import json
import random
import threading
from dataclasses import dataclass, field, replace
from typing import Any

from conversational_agent import app, config
from conversational_agent.assessor import session_need
from conversational_agent.judgment import LLMClient, RawCompletion
from conversational_agent.store import Store, normalize_term

from .clock import EPOCH, SimClock
from .config import HarnessRouting
from .contract import SystemContract
from .feed import ScriptedEventFeed
from .learning import MemoryModel
from .materiality_cache import CachingClient, MaterialityCache
from .personas import Persona
from .probe import ProbeRunner, ProbeSession, gloss_for
from .responders import (
    BriefingView,
    LLMResponder,
    PersonaState,
    Responder,
    ScriptedResponder,
    TurnIntent,
    noise_targets,
    term_pattern,
)
from .stub_judgments import StubRegistry, build_stub_client

# Keys that are answer key, not context. None may ever appear in a judgment's
# `input_json`. `knows` / `does_not_know` are the important ones: they are the
# ledger metric's ground truth, and under `--live` a model handed them is not
# being measured, it is being told.
ANSWER_KEY_FIELDS = (
    "should_be_material",
    "knows",
    "does_not_know",
    "ground_truth",
    "answer_key",
    "true_concepts",
    # The persona's per-subdomain weights: prior knowledge as a shape. The
    # system is measured on recovering it and must never be handed it.
    "familiar_subdomains",
)

# Keys that are ATTENTION, not comprehension, and must never reach a judgment
# call either. Different reason, same enforcement.
#
# A model told "they spent four minutes on this and scrolled to the bottom" and
# asked what they understood will answer differently, and it will answer more
# confidently, and it will be wrong in the direction the whole product is built
# to avoid. Reading behaviour is good enough to close an item out of the "new"
# count and to report engagement. It is not evidence about a concept, and the
# cheapest way to keep it from becoming one is for the calls that write the
# ledger never to see it.
READING_FIELDS = (
    "dwell_ms",
    "scroll_fraction",
    "read_quality",
    "reading_source",
    "time_on_page",
    "read_seconds",
    "attention_score",
    "engagement_score",
)


def _canon(terms: Any) -> tuple[str, ...]:
    """Put answer-key terms in the shape the store writes ledger keys in.

    The store normalises on write -- lowercase, de-hyphenated, whitespace
    collapsed -- and it also merges a term
    into an existing label when one contains the other. The harness compares
    ledger keys against fixture ground truth, so the fixture side has to go
    through the same normalisation or the comparison is between two spellings
    of the same word.

    Deliberately only at the scoring boundary. The persona's own vocabulary
    keeps its hyphens for matching against briefing prose, where "run-rate" is
    what is actually written.
    """
    if not terms:
        return ()
    out: list[str] = []
    for item in terms:
        term = normalize_term(str(item))
        if term and term not in out:
            out.append(term)
    return tuple(out)


def _terms(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    for item in value:
        term = str(item).strip().lower()
        if term and term not in out:
            out.append(term)
    return tuple(out)


# --- Observation records ---------------------------------------------------


@dataclass(frozen=True)
class MaterialityObservation:
    round_index: int
    headline: str
    judged_material: bool
    materiality_score: float
    should_be_material: bool  # answer key
    significance: str = ""  # answer key ('major' | 'borderline' | 'minor')


@dataclass(frozen=True)
class TurnRecord:
    """One turn of a thread, from either side."""

    seq: int
    speaker: str
    text: str
    words: int
    # Persona intent, present only on user turns. Answer key for evidence
    # extraction and for the faithfulness check.
    kind: str = ""
    form: str = ""
    tier: int = 0
    asks_about: tuple[str, ...] = ()
    presupposes: tuple[str, ...] = ()
    mentions: tuple[str, ...] = ()
    # What the persona held at the moment it spoke. The faithfulness check has
    # to score the question against the concept sets AS THEY WERE, not as they
    # ended up: a persona that asks what a term means in round 1 and is told
    # holds it in round 2, and scoring the round-1 question against the round-8
    # concept set would mark a perfectly honest question as a bluff.
    held_at_the_time: tuple[str, ...] = ()
    lacked_at_the_time: tuple[str, ...] = ()
    source_url: str | None = None


@dataclass(frozen=True)
class ReadingRecord:
    """How the persona read one briefing, and whether the system agreed.

    `expected_quality` is the harness's own derivation from the same two
    numbers; `system_quality` is what the store recorded. They are compared, not
    assumed equal -- see `metrics.read_quality_agreement`.
    """

    profile: str
    briefing_chars: int
    dwell_ms: int
    scroll_fraction: float
    expected_quality: str
    system_quality: str
    source: str
    # --- Reading intent (harness-only ground truth) and the system's readout.
    # `intent_act` is skip/skim/read/study as it WAS; `intent_informed` is True
    # for an informed skip, False for a lazy one, None for a non-skip.
    # `system_attention` is the store's 0-10 readout of the same two numbers.
    intent_act: str = ""
    intent_informed: bool | None = None
    known_share: float = 0.0
    story_known: bool = False
    system_attention: float | None = None

    @property
    def agrees(self) -> bool:
        return self.expected_quality == self.system_quality

    @property
    def depth(self) -> int:
        from .reading import DEPTH_ORDINAL

        return DEPTH_ORDINAL.get(self.intent_act, 0)


@dataclass(frozen=True)
class ThreadRecord:
    """One briefing and the whole conversation that did or did not follow it."""

    round_index: int
    exchange_id: str
    topic: str
    briefing: str
    briefing_chars: int
    explained_terms: tuple[str, ...]
    terms_used: tuple[str, ...]
    reading: ReadingRecord | None
    turns: tuple[TurnRecord, ...]
    user_turn_count: int
    system_turn_count: int
    # What the system read out of the whole thread.
    understood: tuple[str, ...]
    not_understood: tuple[str, ...]
    asked_about: tuple[str, ...]
    already_knew: bool | None
    proficiency_before: str
    proficiency_after: str
    newly_known: tuple[str, ...]
    learned_this_thread: tuple[str, ...]
    # Persona intent, rolled up across the thread (answer key).
    intent_asked: tuple[str, ...]
    intent_presupposed: tuple[str, ...]
    intent_mentioned: tuple[str, ...]
    stayed_silent_on: tuple[str, ...]
    asked_about_known: tuple[str, ...]
    # Was there an unknown-to-this-persona term in the briefing? Needed by
    # `engagement_deviation`, which picks the configured ask rate per briefing.
    unknown_term_present: bool = False
    # The fixture event this briefing was about (matched through the
    # exchange's event_id), and its glossary concepts -- so the subdomain
    # report can say which subdomain the story fell in. Answer key.
    event_headline: str = ""
    event_concepts: tuple[str, ...] = ()

    @property
    def user_turns(self) -> tuple[TurnRecord, ...]:
        return tuple(t for t in self.turns if t.speaker == "user")

    @property
    def questions(self) -> tuple[TurnRecord, ...]:
        return tuple(t for t in self.user_turns if t.kind == "question")


@dataclass(frozen=True)
class LedgerSnapshot:
    """The ledger against the persona's true concept set, after one thread.

    Holds sets, not ratios. Precision, recall and F1 are computed in
    `metrics.py` so that the runner records observations and exactly one module
    scores them.
    """

    round_index: int
    interaction_index: int
    by_state: dict[str, tuple[str, ...]]
    believed_known: tuple[str, ...]
    # `explained` / `familiar` terms held below `READ_EXPLANATIONS_BEFORE_BAND`:
    # not yet a belief, not yet in any denominator. Reported.
    held_pending: tuple[str, ...]
    encountered: tuple[str, ...]
    # Terms the run has actually produced evidence about: the persona used one,
    # asked about one, or revealed a misunderstanding. Exposure is not on that
    # list and neither is reading. This is the recall denominator -- see
    # `metrics._agreement_at`.
    evidenced: tuple[str, ...]
    # Answer key, as it stands at this moment. DYNAMIC: a term is known at time
    # t when the memory model's P(can define) >= 0.5 at t. The fixture's
    # `knows` only seeds t = 0; a term learned by asking can be forgotten.
    truly_known: tuple[str, ...]
    # The pre-learning-model answer key -- `knows` plus every term asked about
    # and answered, never forgotten -- kept for one run's comparison under
    # `--ledger-truth static`.
    truly_known_static: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoundRecord:
    index: int
    sim_time: str
    advance_hours: float
    events_injected: int
    candidates_seen: int
    material_count: int
    thread_ran: bool
    quiet: bool
    surfaced_count: int
    held_back_count: int
    behind_count: int
    proficiency: str
    topic_group: str = ""
    session_need: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    # The system's per-user reading prior at the end of this round
    # (`UserScope.reading_pattern`): informed/lazy/unresolved counts and the
    # smoothed p_informed. Read from the scope; the harness never writes it.
    reading_pattern: dict[str, Any] = field(default_factory=dict)
    # Horizon mode (`--retention-probes K`): a probe-only day. No event, no
    # monitor, no briefing, no thread -- just the probe on earlier terms. Such
    # a round is neither a briefing nor a quiet round in any metric.
    probe_only: bool = False
    # |{terms with P(can define) >= 0.5}| at the end of the round, from the
    # persona's memory model. The learning-trend metrics (`known_slope`) read
    # it; it is the ground truth's size, never the ledger's.
    known_count: int | None = None


@dataclass
class PersonaResult:
    persona_id: str
    display_name: str
    archetype: str
    is_noisy: bool
    user_id: str
    group_id: str
    group_name: str
    vocabulary: tuple[str, ...]
    reading_profile: str
    initial_knows: tuple[str, ...]
    initial_does_not_know: tuple[str, ...]
    final_truly_known: tuple[str, ...] = ()
    learned: tuple[str, ...] = ()
    final_ledger: dict[str, str] = field(default_factory=dict)
    # `concepts.read_explanations` per term. `familiar` and `explained` count
    # toward the band only at `config.READ_EXPLANATIONS_BEFORE_BAND`; the
    # per-state report splits them on this so P(known | familiar) can be read
    # at one exposure and at two, which is the deal the human made when
    # choosing the state.
    ledger_read_explanations: dict[str, int] = field(default_factory=dict)
    ledger_correct_uses: dict[str, int] = field(default_factory=dict)
    exposure_counts: dict[str, int] = field(default_factory=dict)
    ledger_evidence: dict[str, str] = field(default_factory=dict)
    # Terms the persona's own turns produced evidence about. Recorded separately
    # from the ledger because the silence and reading regression checks need a
    # source of evidence independent of the ledger they audit: "no term reached
    # a known state without the persona saying anything about it" is only a
    # check if the two sides come from different places.
    reported_terms: tuple[str, ...] = ()
    rounds: list[RoundRecord] = field(default_factory=list)
    threads: list[ThreadRecord] = field(default_factory=list)
    materiality: list[MaterialityObservation] = field(default_factory=list)
    snapshots: list[LedgerSnapshot] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    contract_gaps: list[str] = field(default_factory=list)
    # --- The learning model and the probe (harness/learning.py, harness/probe.py)
    learning: dict[str, Any] = field(default_factory=dict)
    probes: list[ProbeSession] = field(default_factory=list)
    # The static answer key at the end of the run (`knows` + asked-and-
    # answered), for the one-run comparison with the dynamic one.
    final_truly_known_static: tuple[str, ...] = ()
    # Every term that was truly known at ANY snapshot (or at t = 0). The
    # cold-start check asks whether the system claimed a term it never taught,
    # and a term the persona learned and then forgot was still taught.
    ever_known: tuple[str, ...] = ()
    learned_by_asking: tuple[str, ...] = ()
    memory_final: dict[str, dict[str, Any]] = field(default_factory=dict)
    # What the SYSTEM eventually decided each skipped briefing meant
    # (exchange_id -> informed | lazy | unresolved), read back at the end of
    # the run so late resolutions are captured.
    skip_kinds: dict[str, str] = field(default_factory=dict)
    # --- Subdomains (harness/LEARNING.md, 'Subdomains') -----------------------
    # The fixture's taxonomy and this persona's true weights (answer key), the
    # memory model's placement of every term it met, and the vocabulary terms
    # the weights seeded as known beyond `knows`.
    fixture_subdomains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    familiar_subdomains: dict[str, float] = field(default_factory=dict)
    subdomain_assignments: dict[str, tuple[str, str]] = field(default_factory=dict)
    seeded_by_weight: tuple[str, ...] = ()
    # The system's side, read back from the store at the end of the run:
    # `concepts.subdomain` per ledger term (None where the column is absent or
    # unlabelled) and `UserScope.subdomain_familiarity(group_id)` (empty when
    # the member is not exported). Never written by the harness.
    ledger_subdomains: dict[str, str | None] = field(default_factory=dict)
    system_subdomain_familiarity: dict[str, dict[str, Any]] = field(default_factory=dict)
    subdomain_familiarity_exported: bool = False


# --- The recording client seam ---------------------------------------------


@dataclass
class RecordingClient:
    """Pass-through observer around any `LLMClient`.

    Records `(point, context, verdict)` in call order. In offline mode it wraps
    the stub; in live mode it wraps `AnthropicClient` and changes nothing about
    the call. It exists because call *order* within a simulated instant is the
    only way to attribute a verdict to the round that produced it -- every row a
    round writes carries the same simulated timestamp, so `ORDER BY created_at`
    cannot separate two judgments made inside one round.
    """

    inner: LLMClient
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        schema: dict[str, Any],
        effort: str,
        max_tokens: int = 16000,
    ) -> RawCompletion:
        payload = json.loads(user_content)
        point = payload.get("_judgment_point")
        context = payload.get("context", {})
        try:
            raw = self.inner.complete_json(
                model=model,
                system=system,
                user_content=user_content,
                schema=schema,
                effort=effort,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            self.calls.append(
                {
                    "point": point,
                    "context": context,
                    "verdict": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise
        try:
            verdict = json.loads(raw.text)
        except json.JSONDecodeError:
            verdict = None
        self.calls.append(
            {"point": point, "context": context, "verdict": verdict, "error": None}
        )
        return raw


# --- Harness assembly ------------------------------------------------------


@dataclass
class Harness:
    """The assembled system plus the substituted pieces."""

    system: app.System
    contract: SystemContract
    clock: SimClock
    feed: ScriptedEventFeed
    recorder: RecordingClient
    registry: StubRegistry | None  # None under --live: there are no stubs
    responder: Responder
    live: bool
    # The post-session probe. Harness-only: it is handed the persona's own
    # client (live) or nothing (offline), never the system or its store.
    probe: ProbeRunner = field(default_factory=ProbeRunner)
    # Which models voice and grade the personas (harness/config.py), and the
    # optional materiality verdict cache (`--materiality-cache`).
    routing: HarnessRouting | None = None
    materiality_cache: MaterialityCache | None = None

    @property
    def store(self) -> Store:
        return self.system.store

    def close(self) -> None:
        self.system.close()

    def harness_usage(self) -> dict[str, dict[str, int]]:
        """Token usage of the harness-side calls (persona voice, probe voice,
        grader). Zero offline; never part of `judgment_log`."""
        out: dict[str, dict[str, int]] = {}
        voice = getattr(self.responder, "usage", None)
        if isinstance(voice, dict):
            out["persona_voice"] = dict(voice)
        for role, bucket in (getattr(self.probe, "usage", None) or {}).items():
            out[role] = dict(bucket)
        return out


class _QuietRound(Exception):
    """Control flow only: the system had nothing new to say this round."""


def build_harness(
    *,
    db_path,
    run_id: str,
    personas: list[Persona],
    live: bool = False,
    remediation_search: bool = True,
    routing: HarnessRouting | None = None,
    materiality_cache: MaterialityCache | None = None,
) -> Harness:
    """Assemble the real system with the harness's substitutions in place.

    `personas` is needed up front only so the offline stub registry can hold
    each group's glossary -- the side channel that keeps ground truth out of the
    judgment context. Note what the registry is *not* given: no concept sets, no
    turn script, no materiality labels except inside the feed.

    `routing` names the models that voice and grade the personas (default:
    `harness.config` -- Sonnet for both). `materiality_cache` wraps the
    recording client so materiality verdicts are replayed from a file.
    """
    clock = SimClock()
    feed = ScriptedEventFeed(clock=clock)
    if routing is None:
        routing = HarnessRouting.resolve(remediation_search=remediation_search)
    remediation_search = remediation_search and routing.remediation_search

    registry: StubRegistry | None = None
    responder: Responder
    if live:
        from conversational_agent.judgment import AnthropicClient

        raw = app._anthropic_raw_client()
        recorder = RecordingClient(inner=AnthropicClient(raw))
        llm_client: Any = recorder
        if materiality_cache is not None:
            llm_client = CachingClient(inner=recorder, cache=materiality_cache)
        system = app.build(
            db_path=db_path,
            llm_client=llm_client,
            raw_client=raw,
            feed=feed,
            run_id=run_id,
            offline=False,
        )
        if not remediation_search:
            system.assessor.client = None
        # The persona's own model calls deliberately do NOT go through `Judge`:
        # they are the human side of the conversation, and logging them would
        # corrupt every cost, latency and call-count figure in the report.
        responder = LLMResponder(client=raw, model=routing.persona_model)
        probe = ProbeRunner(
            client=raw,
            persona_model=routing.persona_model,
            grader_model=routing.grader_model,
        )
    else:
        probe = ProbeRunner(
            persona_model=routing.persona_model, grader_model=routing.grader_model
        )
        registry = StubRegistry(feed=feed)
        for persona in personas:
            registry.register(persona)
        recorder = RecordingClient(inner=build_stub_client(registry))
        llm_client = recorder
        if materiality_cache is not None:
            llm_client = CachingClient(inner=recorder, cache=materiality_cache)
        system = app.build(
            db_path=db_path,
            llm_client=llm_client,
            raw_client=None,
            feed=feed,
            run_id=run_id,
            offline=True,
        )
        responder = ScriptedResponder()

    return Harness(
        system=system,
        contract=SystemContract(system),
        clock=clock,
        feed=feed,
        recorder=recorder,
        registry=registry,
        responder=responder,
        live=live,
        probe=probe,
        routing=routing,
        materiality_cache=materiality_cache,
    )


# --- The replay ------------------------------------------------------------


def _vocab_key(term: str, vocabulary: tuple[str, ...]) -> str:
    """Map a term the contract returned onto the fixture's spelling of it.

    The contract lowercases; the store also de-hyphenates. The memory model is
    keyed on the fixture's spelling ("run-rate"), because that is what the
    responders match against briefing prose, so a glossed "run rate" has to
    land on the same trace.
    """
    term = term.strip().lower()
    if term in vocabulary:
        return term
    wanted = normalize_term(term)
    for candidate in vocabulary:
        if normalize_term(candidate) == wanted:
            return candidate
    return term


def band_bar() -> int:
    """How many read explanations `familiar`/`explained` need before they count.

    Read from config so the harness cannot drift from the store's own rule.
    """
    return int(config.READ_EXPLANATIONS_BEFORE_BAND)


def _snapshot(
    scope,
    group_id: str,
    state: PersonaState,
    round_index: int,
    interaction: int,
    acted: set[str],
    *,
    memory: MemoryModel,
    now,
    static_known: set[str],
) -> LedgerSnapshot:
    by_state: dict[str, list[str]] = {s: [] for s in config.CONCEPT_STATES}
    encountered: list[str] = []
    evidenced: set[str] = set(_canon(acted))
    believed: list[str] = []
    pending: list[str] = []
    bar = band_bar()
    for concept in scope.ledger(group_id):
        by_state.setdefault(concept.state, []).append(concept.term)
        encountered.append(concept.term)
        reads = int(getattr(concept, "read_explanations", 0) or 0)
        uses = int(getattr(concept, "correct_uses", 0) or 0)
        misses = int(getattr(concept, "misunderstandings", 0) or 0)
        # What counts as a belief the system is prepared to act on mirrors the
        # band's own rule, because that is the claim being scored:
        #   * `confirmed` and `provisional` -- the persona used the term.
        #   * any row with a correct use -- a presupposed term in a question is
        #     a use, and the store keeps such a row at `familiar`/`explained`
        #     if it already held that (it does not demote to `provisional`),
        #     so the use has to be read off `correct_uses` rather than the state.
        #   * `explained` / `familiar` on reading alone -- only at
        #     `READ_EXPLANATIONS_BEFORE_BAND`. One explanation they read (or one
        #     ask) stops the re-glossing; it votes neither way until the second.
        # A term held below the bar with no use behind it is `pending`:
        # reported, so the run can show how much of the ledger is sitting one
        # exposure short, and in no denominator.
        if concept.state in (config.CONCEPT_CONFIRMED, config.CONCEPT_PROVISIONAL) or uses >= 1:
            believed.append(concept.term)
            evidenced.add(concept.term)
        elif concept.state in (config.CONCEPT_EXPLAINED, config.CONCEPT_FAMILIAR):
            if reads >= bar:
                believed.append(concept.term)
                evidenced.add(concept.term)
            else:
                pending.append(concept.term)
        if misses >= 1:
            evidenced.add(concept.term)
        # Neither exposure nor a single reading is evidence, so neither is in
        # the recall denominator. A term the system only ever said in front of
        # the persona -- however attentively they read it -- is one the system
        # has no mechanism to score, and counting it would mark the system
        # wrong for a capability it deliberately does not have.
    return LedgerSnapshot(
        round_index=round_index,
        interaction_index=interaction,
        by_state={k: tuple(sorted(v)) for k, v in sorted(by_state.items())},
        believed_known=tuple(sorted(set(believed))),
        held_pending=tuple(sorted(set(pending))),
        encountered=tuple(sorted(set(encountered))),
        evidenced=tuple(sorted(evidenced)),
        # Dynamic ground truth: what the memory model says they can define
        # right now. `state.known` is the narrower presupposable set and is
        # not the scoring key.
        truly_known=tuple(sorted(_canon(memory.known_terms(now)))),
        truly_known_static=tuple(sorted(_canon(static_known))),
    )


def _run_thread(
    harness: Harness,
    persona: Persona,
    state: PersonaState,
    scope,
    group,
    view: BriefingView,
    index: int,
    errors: list[str],
) -> tuple[list[TurnRecord], list[str]]:
    """The conversation. Ends when the persona stops, and only then."""
    contract = harness.contract
    turns: list[TurnRecord] = []
    learned: list[str] = []
    visible = view.briefing
    seq = 0
    turn_no = 0
    memory: MemoryModel | None = state.memory
    now = harness.clock.now()

    while True:
        # The persona decides from its memory as it stands right now, not
        # from the fixture's lists. A term it asked about last session and
        # still remembers is not asked about again; one it has forgotten is.
        state.refresh(now)
        intent: TurnIntent | None = harness.responder.plan_turn(
            persona=persona,
            state=state,
            briefing=view,
            index=index,
            turn_no=turn_no,
            visible_text=visible,
        )
        if intent is None:
            break

        # Snapshot the concept sets before the answer can change them -- the
        # faithfulness check scores a question against what its asker held when
        # it was asked.
        held_now = tuple(sorted(state.known))
        lacked_now = tuple(sorted(state.unknown))

        # The user's turn is written by `respond`, not here. `add_turn` is
        # exported separately and it would be natural to call it -- but the
        # Assessor records the question itself, first, on purpose: if the
        # answering call fails, the question is still on the record and still
        # reaches the evidence extractor, because what they chose to ask is
        # true whether or not we managed to answer it. Recording it here as
        # well would double every turn count in the report. `contract.respond`
        # verifies the count moved by exactly two and notes it if it did not.
        answer = contract.respond(scope, group, view.exchange_id, intent.text)

        seq += 1
        turns.append(
            TurnRecord(
                seq=seq,
                speaker="user",
                text=intent.text,
                words=intent.words,
                kind=intent.kind,
                form=intent.form,
                tier=intent.tier,
                asks_about=intent.asks_about,
                presupposes=intent.presupposes,
                mentions=intent.mentions,
                held_at_the_time=held_now,
                lacked_at_the_time=lacked_now,
            )
        )
        visible = f"{visible}\n\n{intent.text}"

        seq += 1
        turns.append(
            TurnRecord(
                seq=seq,
                speaker="system",
                text=answer.text,
                words=len(answer.text.split()),
                source_url=answer.source_url,
            )
        )
        visible = f"{visible}\n\n{answer.text}"

        # They asked, we answered: that is the `explained` transition, and the
        # persona genuinely holds the term afterwards. Note what does NOT cause
        # this -- a term glossed in a briefing they never asked about, however
        # attentively they read it. The Assessor is explicit that an unprompted
        # definition is exposure and not knowledge, and the persona simulation
        # has to agree with it or the ledger would be scored against a truth it
        # had no way to observe.
        if answer.text:
            learned += state.learn(intent.asks_about)
            if memory is not None:
                # Rule 3: they asked and read the answer. Applied to the
                # persona's OWN asks, not to the system's `asked_about`
                # read-out -- the system's belief must not train the truth
                # it is scored against.
                for term in intent.asks_about:
                    memory.asked_and_answered(term, now)
        if memory is not None:
            # Rule 4, in-thread: the persona deliberately used a term it holds
            # (a presupposition in a question, or a mention in a remark). The
            # responder's intent says exactly which, so no judgement about
            # correctness is borrowed from the system. No feedback: the
            # system does not gloss a term the asker plainly holds.
            for term in tuple(intent.presupposes) + tuple(intent.mentions):
                memory.retrieval_success(
                    term,
                    now,
                    new_context=memory.new_context(term, view.exchange_id),
                    feedback=False,
                )
            state.refresh(now)

        turn_no += 1

    return turns, sorted(set(learned))


def horizon_advance_hours(
    default_hours: float,
    *,
    rounds: int,
    horizon_days: float | None,
    retention_probes: int,
) -> float:
    """Hours between rounds.

    Default: the group's poll interval (at least an hour). Horizon mode
    (`--horizon-days N`): the N days are divided into equal steps over the
    briefing rounds *and* the probe-only days that follow, so the last
    probe-only day lands on day N (and with no probe-only days the last
    briefing does).
    """
    if horizon_days is None:
        return default_hours
    steps = max(1, rounds + max(0, retention_probes))
    return float(horizon_days) * 24.0 / steps


def run_persona(
    harness: Harness,
    persona: Persona,
    *,
    max_rounds: int | None = None,
    horizon_days: float | None = None,
    retention_probes: int = 0,
) -> PersonaResult:
    """Create the persona in the real store and replay its timeline.

    `horizon_days` spreads the same events over that many simulated days;
    `retention_probes` appends that many probe-only days after the last
    briefing round (horizon mode only). Both absent: today's behaviour.
    """
    system = harness.system
    contract = harness.contract
    clock = harness.clock
    clock.reset(EPOCH)

    store = system.store
    user = store.create_user(
        persona.display_name, kind="persona", profile=dict(persona.profile)
    )
    scope = store.scope(user.id)
    visible_group = persona.system_visible_group()
    group = scope.create_group(
        name=visible_group["name"],
        description=visible_group["description"],
        poll_interval_minutes=visible_group["poll_interval_minutes"],
    )
    if persona.goal is not None:
        scope.create_goal(
            group.id,
            persona.goal.description,
            clock.offset_iso(persona.goal.deadline_hours_from_start),
        )

    # The persona's memory, seeded from the fixture at t = 0. From here on the
    # static lists are not consulted for behaviour or for ground truth.
    memory = MemoryModel.seeded(
        learning=persona.learning,
        seed=persona.seed,
        persona_id=persona.id,
        universe=persona.group.vocabulary,
        knows=persona.knows,
        at=clock.now(),
        quantities=persona.quantities,
        does_not_know=persona.does_not_know,
        subdomains=persona.subdomain_map,
    )
    state = PersonaState.for_persona(persona, memory=memory, now=clock.now())
    # `knows` + asked-and-answered, never forgotten: the pre-model answer key.
    # With a subdomain taxonomy the seed is wider than `knows`: the terms the
    # weights seeded are known at t = 0 too, under both keys.
    seeded_known = set(memory.known_terms(clock.now()))
    static_known: set[str] = set(persona.knows) | seeded_known
    ever_known: set[str] = set(persona.knows) | seeded_known
    result = PersonaResult(
        persona_id=persona.id,
        display_name=persona.display_name,
        archetype=persona.archetype,
        is_noisy=persona.is_noisy,
        user_id=user.id,
        group_id=group.id,
        group_name=group.name,
        vocabulary=persona.group.vocabulary,
        reading_profile=persona.reading.name,
        initial_knows=_canon(persona.knows),
        initial_does_not_know=_canon(persona.does_not_know),
        learning=persona.learning.to_dict(),
        fixture_subdomains=dict(persona.group.subdomains),
        familiar_subdomains=dict(persona.familiar_subdomains),
        seeded_by_weight=tuple(memory.seeded_by_weight),
    )

    events = list(persona.events)
    rounds = (
        persona.interactions
        if max_rounds is None
        else min(persona.interactions, max_rounds)
    )
    advance = horizon_advance_hours(
        max(1.0, persona.group.poll_interval_minutes / 60.0),
        rounds=rounds,
        horizon_days=horizon_days,
        retention_probes=retention_probes,
    )
    interaction = 0
    # Every piece of system prose the persona saw, for the probe-only days'
    # glosses (the probe grades against what the system actually said).
    system_texts_all: list[str] = []
    last_story = ""
    reported_on: set[str] = set()
    # The subset of `reported_on` that is evidence FOR or AGAINST -- a correct
    # use or a revealed misunderstanding. Asking is on the record (it is a
    # persona turn, and the regressions need to see it) but under the band
    # rule a single ask-and-explain votes neither way, so it is not in the
    # recall denominator on its own.
    acted_on: set[str] = set()

    for index in range(1, rounds + 1):
        errors: list[str] = []
        clock.advance(advance)

        staged = [events[index - 1]] if index - 1 < len(events) else []
        harness.feed.stage(group, staged)
        group = scope.get_group(group.id) or group

        # --- Monitor -------------------------------------------------------
        candidates_seen = 0
        material_count = 0
        try:
            poll = system.monitor.poll(scope, group)
            candidates_seen = poll.candidates_seen
            material_count = poll.material_count
            if poll.note:
                errors.append(f"monitor: {poll.note}")
            for event_id in poll.event_ids:
                event = scope.get_event(event_id)
                if event is None:
                    errors.append(f"monitor: recorded event {event_id} not readable")
                    continue
                truth = harness.feed.truth_for(group.name, event.headline)
                if truth is None:
                    errors.append(
                        f"monitor: no ground-truth label for {event.headline!r}"
                    )
                    continue
                significance = next(
                    (e.significance for e in staged if e.headline == event.headline), ""
                )
                result.materiality.append(
                    MaterialityObservation(
                        round_index=index,
                        headline=event.headline,
                        judged_material=bool(event.is_material),
                        materiality_score=float(event.materiality_score or 0.0),
                        should_be_material=truth,
                        significance=significance,
                    )
                )
        except Exception as exc:
            errors.append(f"monitor: {type(exc).__name__}: {exc}")
        if harness.materiality_cache is not None:
            # Verdicts served from the cache were logged by `Judge` with the
            # configured model name; stamp them `cache` so cost accounting is
            # honest while the trace stays complete.
            try:
                harness.materiality_cache.mark_logged(
                    store.conn, getattr(system.judge, "run_id", None)
                )
            except Exception as exc:
                errors.append(f"materiality cache: {type(exc).__name__}: {exc}")

        # --- Orchestrator --------------------------------------------------
        quiet = True
        surfaced = 0
        held_back = 0
        topic_group_name = ""
        try:
            session = system.orchestrator.open_session(scope, now=clock.now())
            quiet = session.quiet
            surfaced = len(session.events)
            held_back = session.held_back_count
            topic_group_name = session.topic_group.name if session.topic_group else ""
        except Exception as exc:
            errors.append(f"orchestrator: {type(exc).__name__}: {exc}")

        # `session_need` is the Assessor's no-LLM read. Exercised because it is
        # part of the surface this harness drives, and cheap enough to call
        # every round.
        need: dict[str, Any] = {}
        try:
            need = dict(session_need(scope, group) or {})
        except Exception as exc:
            errors.append(f"session_need: {type(exc).__name__}: {exc}")

        # --- The thread ----------------------------------------------------
        thread_ran = False
        untold = []
        try:
            untold = scope.untold_events(group.id)
        except Exception as exc:
            errors.append(f"untold_events: {type(exc).__name__}: {exc}")

        try:
            # `raise_topic` picks the most material untold event itself; the
            # harness passes nothing so the system's own choice is what is
            # under test. A round with nothing untold produces NO briefing --
            # that is the human's rule (never repeat a story, say nothing when
            # there is nothing new), and a quiet round is a normal outcome
            # here, not a gap.
            briefing = contract.raise_topic(scope, group, event=None)
            if briefing is None:
                if untold:
                    _gap(
                        result,
                        f"raise_topic returned None with {len(untold)} untold "
                        "event(s) queued",
                    )
                raise _QuietRound()
            view = BriefingView(
                exchange_id=briefing.exchange_id,
                topic=briefing.topic,
                briefing=briefing.briefing,
                explained_terms=briefing.explained_terms,
                terms_used=briefing.terms_used,
            )
            if not view.exchange_id:
                _gap(result, "Briefing.exchange_id was empty")
            if not view.briefing:
                _gap(
                    result,
                    "Briefing.briefing was empty -- the system's only job here "
                    "is to state the substance",
                )

            # --- Reading, before a single word is typed --------------------
            # What the reader already holds decides how they read: an informed
            # skip when they hold the story's terms, a lazy skip at the
            # profile's rate, otherwise a skim-to-read shifted by how much of
            # it is new. `known_share` is computed BEFORE the gloss events
            # below -- the decision is made at the moment they start reading.
            session_start = clock.now()
            glossed_now = tuple(
                _vocab_key(t, persona.group.vocabulary) for t in view.explained_terms
            )
            in_text = tuple(
                t for t in persona.group.vocabulary if term_pattern(t).search(view.briefing)
            )
            decision_terms = tuple(dict.fromkeys(list(glossed_now) + list(in_text)))
            if decision_terms:
                known_share = sum(
                    1 for t in decision_terms if memory.is_known(t, session_start)
                ) / len(decision_terms)
                with_context = sum(
                    1
                    for t in decision_terms
                    if (memory.trace(t) is not None and memory.trace(t).n_ctx >= 1)
                )
                story_known = with_context * 2 > len(decision_terms)
            else:
                known_share, story_known = 0.0, False
            reading_rng = random.Random(f"{persona.seed}:{persona.id}:read:{index}")
            reading, intent = persona.reading.read_with_intent(
                view.chars, reading_rng, known_share=known_share, story_known=story_known
            )
            contract.record_reading(
                scope,
                view.exchange_id,
                dwell_ms=reading.dwell_ms,
                scroll_fraction=reading.scroll_fraction,
                source="simulated",
            )
            stored = contract.stored_reading(scope, view.exchange_id)
            stored_exchange = scope.get_exchange(view.exchange_id)
            reading_record = ReadingRecord(
                profile=reading.profile,
                briefing_chars=view.chars,
                dwell_ms=reading.dwell_ms,
                scroll_fraction=reading.scroll_fraction,
                expected_quality=reading.expected_quality,
                system_quality=str(stored.get("read_quality") or ""),
                source=str(stored.get("reading_source") or ""),
                intent_act=intent.act,
                intent_informed=intent.informed,
                known_share=round(intent.known_share, 3),
                story_known=intent.story_known,
                system_attention=(
                    getattr(stored_exchange, "attention", None) if stored_exchange else None
                ),
            )
            if stored.get("reading_source") not in (None, "", "simulated"):
                _gap(
                    result,
                    "record_reading(source='simulated') was stored as "
                    f"{stored.get('reading_source')!r}; a simulated reading must "
                    "stay distinguishable from an observed one",
                )

            # --- Memory: the glossed terms they just read -------------------
            # Rule 1 (first glossed encounter) or rule 2 (re-read in a new
            # context) for every term the briefing glossed, at the quality
            # they actually read it. Terms merely USED but not glossed get no
            # event -- the review's encoding numbers are for glossed
            # encounters (a known simplification; see LEARNING.md). A glossed
            # term outside the vocabulary is placed in a subdomain first; the
            # system's own label for it (if its ledger already has one) is the
            # first hint, and is the ONLY thing read from the system here.
            system_labels = _system_subdomain_labels(scope, group.id)
            for term in glossed_now:
                memory.glossed(
                    term,
                    reading.expected_quality,
                    clock.now(),
                    view.exchange_id,
                    system_label=system_labels.get(normalize_term(term)),
                )
            state.refresh(clock.now())
            # The responders decide what the persona can have seen from how it
            # read: a skipped briefing was glanced at, not read.
            view = replace(view, reading_act=intent.act)

            # Whether an unknown-to-this-persona term is in front of them,
            # decided from memory at the moment they start reading -- the
            # same view the responder's first turn takes.
            unknown_present = bool(
                {
                    t
                    for t in persona.group.vocabulary
                    if t in state.unknown and term_pattern(t).search(view.briefing)
                }
            )

            # The noisy persona's two behaviours are decided per term, from the
            # briefing text, BEFORE any turn happens -- and recorded whether or
            # not one does. Deriving them from emitted turns would under-report
            # them exactly when they matter most: a persona that stayed silent
            # about a term and then said nothing at all has done the thing the
            # noise fixture exists to produce, and a thread with no turns is now
            # the common case rather than the exception.
            silent_targets, ask_known_targets = noise_targets(
                persona, state, view.briefing
            )

            # --- The conversation ------------------------------------------
            turns, learned = _run_thread(
                harness, persona, state, scope, group, view, index, errors
            )
            outcome = contract.close_thread(scope, group, view.exchange_id)
            thread_ran = True
            interaction += 1
            static_known.update(learned)

            user_turns = [t for t in turns if t.speaker == "user"]

            # --- The probe (harness-only; nothing below reaches src/) -------
            # After the thread is closed and the system has written whatever
            # it will write about this session. The probe reads the persona's
            # memory and the system's own prose (as the closest thing to a
            # canonical gloss), and its results go into `result.probes` only.
            try:
                system_texts = [view.briefing] + [
                    t.text for t in turns if t.speaker == "system"
                ]
                system_texts_all.extend(system_texts)
                last_story = view.briefing[:600]
                candidate_terms = tuple(
                    dict.fromkeys(list(glossed_now) + list(memory.exposed_terms()))
                )
                result.probes.append(
                    harness.probe.run_session(
                        persona=persona,
                        memory=memory,
                        round_index=index,
                        session_start=session_start,
                        now=clock.now(),
                        this_session_terms=glossed_now,
                        story=view.briefing[:600],
                        glosses={t: gloss_for(t, system_texts) for t in candidate_terms},
                    )
                )
            except Exception as exc:
                errors.append(f"probe: {type(exc).__name__}: {exc}")
            state.refresh(clock.now())

            # Engagement is a fact about the user, so the harness -- which is
            # playing the user -- is what records it. Deliberately distinct
            # from `mark_surfaced`: being shown an item is not the same as
            # having engaged with it, and only the latter may ever inform the
            # ledger. Reading it does not count either; the item was put in
            # front of them whether or not they looked at it.
            told = scope.get_exchange(view.exchange_id)
            if told is not None and told.event_id and user_turns:
                scope.mark_engaged([told.event_id])
            event_headline, event_concepts = _event_for_exchange(scope, told, persona)

            reported_on.update(_terms(outcome.asked_about))
            reported_on.update(_terms(outcome.understood))
            reported_on.update(_terms(outcome.not_understood))
            acted_on.update(_terms(outcome.understood))
            acted_on.update(_terms(outcome.not_understood))

            result.threads.append(
                ThreadRecord(
                    round_index=index,
                    exchange_id=view.exchange_id,
                    topic=view.topic,
                    briefing=view.briefing,
                    briefing_chars=view.chars,
                    explained_terms=view.explained_terms,
                    terms_used=view.terms_used,
                    reading=reading_record,
                    turns=tuple(turns),
                    user_turn_count=len(user_turns),
                    system_turn_count=len(turns) - len(user_turns),
                    understood=_terms(outcome.understood),
                    not_understood=_terms(outcome.not_understood),
                    asked_about=_terms(outcome.asked_about),
                    already_knew=outcome.already_knew,
                    proficiency_before=outcome.proficiency_before,
                    proficiency_after=outcome.proficiency_after,
                    newly_known=_terms(outcome.newly_known),
                    learned_this_thread=tuple(learned),
                    intent_asked=tuple(
                        sorted({t for turn in user_turns for t in turn.asks_about})
                    ),
                    intent_presupposed=tuple(
                        sorted({t for turn in user_turns for t in turn.presupposes})
                    ),
                    intent_mentioned=tuple(
                        sorted({t for turn in user_turns for t in turn.mentions})
                    ),
                    stayed_silent_on=silent_targets,
                    asked_about_known=tuple(
                        sorted(
                            set(ask_known_targets)
                            & {t for turn in user_turns for t in turn.asks_about}
                        )
                    ),
                    unknown_term_present=unknown_present,
                    event_headline=event_headline,
                    event_concepts=event_concepts,
                )
            )
        except _QuietRound:
            pass
        except Exception as exc:
            errors.append(f"thread: {type(exc).__name__}: {exc}")

        # --- Snapshot ------------------------------------------------------
        try:
            snap = _snapshot(
                scope,
                group.id,
                state,
                index,
                interaction,
                acted_on,
                memory=memory,
                now=clock.now(),
                static_known=static_known,
            )
            result.snapshots.append(snap)
            ever_known.update(memory.known_terms(clock.now()))
            behind = scope.behind_count(group.id)
            band = scope.proficiency(group.id)
            pattern = dict(scope.reading_pattern(group.id))
        except Exception as exc:
            errors.append(f"ledger: {type(exc).__name__}: {exc}")
            behind, band, pattern = 0, "", {}

        result.rounds.append(
            RoundRecord(
                index=index,
                sim_time=clock.iso(),
                advance_hours=advance,
                events_injected=len(staged),
                candidates_seen=candidates_seen,
                material_count=material_count,
                thread_ran=thread_ran,
                quiet=quiet,
                surfaced_count=surfaced,
                held_back_count=held_back,
                behind_count=behind,
                proficiency=band,
                topic_group=topic_group_name,
                session_need=need,
                errors=tuple(errors),
                reading_pattern=pattern,
                known_count=len(memory.known_terms(clock.now())),
            )
        )
        result.errors.extend(f"round {index}: {e}" for e in errors)

    # --- Probe-only days (horizon mode) --------------------------------------
    # No event, no monitor, no briefing, no thread: the clock moves, the
    # persona's memory decays, and the probe asks about terms met earlier.
    # Recorded as rounds with `probe_only=True` so the timeline is complete,
    # and with `thread_ran=False`, `quiet=False` so nothing counts them as a
    # briefing or as a quiet round.
    if horizon_days is not None and retention_probes > 0:
        last_band = result.rounds[-1].proficiency if result.rounds else ""
        last_behind = result.rounds[-1].behind_count if result.rounds else 0
        for k in range(1, retention_probes + 1):
            index = rounds + k
            errors = []
            clock.advance(advance)
            try:
                probe_start = clock.now()
                candidate_terms = tuple(memory.exposed_terms())
                result.probes.append(
                    harness.probe.run_session(
                        persona=persona,
                        memory=memory,
                        round_index=index,
                        session_start=probe_start,
                        now=clock.now(),
                        this_session_terms=(),
                        story=last_story,
                        glosses={t: gloss_for(t, system_texts_all) for t in candidate_terms},
                    )
                )
            except Exception as exc:
                errors.append(f"probe: {type(exc).__name__}: {exc}")
            state.refresh(clock.now())
            ever_known.update(memory.known_terms(clock.now()))
            result.rounds.append(
                RoundRecord(
                    index=index,
                    sim_time=clock.iso(),
                    advance_hours=advance,
                    events_injected=0,
                    candidates_seen=0,
                    material_count=0,
                    thread_ran=False,
                    quiet=False,
                    surfaced_count=0,
                    held_back_count=0,
                    behind_count=last_behind,
                    proficiency=last_band,
                    errors=tuple(errors),
                    probe_only=True,
                    known_count=len(memory.known_terms(clock.now())),
                )
            )
            result.errors.extend(f"round {index}: {e}" for e in errors)

    try:
        # What the system finally decided each skipped briefing meant. Read
        # back at the end so a skip resolved by a LATER thread is captured.
        for thread in result.threads:
            if thread.reading is not None and thread.reading.intent_act == "skip":
                x = scope.get_exchange(thread.exchange_id)
                result.skip_kinds[thread.exchange_id] = str(
                    getattr(x, "skip_kind", None) or "unresolved"
                )
    except Exception as exc:
        result.errors.append(f"skip_kinds: {type(exc).__name__}: {exc}")

    try:
        ledger = sorted(scope.ledger(group.id), key=lambda c: c.term)
        result.final_ledger = {c.term: c.state for c in ledger}
        result.exposure_counts = {c.term: c.exposure_count for c in ledger}
        result.ledger_read_explanations = {
            c.term: int(getattr(c, "read_explanations", 0) or 0) for c in ledger
        }
        result.ledger_correct_uses = {
            c.term: int(getattr(c, "correct_uses", 0) or 0) for c in ledger
        }
        # The evidence string is captured because it is how a silence-derived or
        # reading-derived promotion would announce itself. Nothing should
        # write "used Nx without being questioned" or anything about dwell
        # time, and `metrics.silence_regression` and
        # `metrics.reading_regression` assert both.
        result.ledger_evidence = {c.term: (c.evidence or "") for c in ledger}
        # The system's own subdomain labels.
        result.ledger_subdomains = {c.term: (c.subdomain or None) for c in ledger}
    except Exception as exc:
        result.errors.append(f"final ledger: {type(exc).__name__}: {exc}")

    try:
        fam = getattr(scope, "subdomain_familiarity", None)
        if callable(fam):
            raw_fam = fam(group.id) or {}
            result.system_subdomain_familiarity = {
                str(k): dict(v) for k, v in dict(raw_fam).items()
            }
            result.subdomain_familiarity_exported = True
        else:
            _gap(result, "scope.subdomain_familiarity is not exported")
    except Exception as exc:
        result.errors.append(f"subdomain_familiarity: {type(exc).__name__}: {exc}")
    result.subdomain_assignments = dict(memory.assignments)

    final_known = _canon(memory.known_terms(clock.now()))
    result.final_truly_known = tuple(sorted(final_known))
    # Learned = known at the end and not known at the start, by any route
    # (asking, repeated glossed reads, probe feedback). Learned-by-asking is
    # the narrower, pre-model notion and is kept alongside it.
    result.learned = tuple(sorted(set(final_known) - set(result.initial_knows)))
    result.learned_by_asking = tuple(sorted(_canon(state.learned)))
    result.final_truly_known_static = tuple(sorted(_canon(static_known)))
    result.ever_known = tuple(sorted(_canon(ever_known)))
    result.memory_final = memory.snapshot()
    result.reported_terms = tuple(sorted(_canon(reported_on)))
    return result


def _gap(result: PersonaResult, message: str) -> None:
    if message not in result.contract_gaps:
        result.contract_gaps.append(message)


def _system_subdomain_labels(scope, group_id: str) -> dict[str, str]:
    """`concepts.subdomain` per normalised term. Read-only, and used only to
    place an out-of-vocabulary term."""
    try:
        out: dict[str, str] = {}
        for concept in scope.ledger(group_id):
            label = concept.subdomain
            if label:
                out[normalize_term(concept.term)] = str(label)
        return out
    except Exception:
        return {}


def _event_for_exchange(scope, exchange, persona: Persona) -> tuple[str, tuple[str, ...]]:
    """The fixture event a briefing was about, via the exchange's event_id."""
    try:
        if exchange is None or not getattr(exchange, "event_id", None):
            return "", ()
        event = scope.get_event(exchange.event_id)
        if event is None:
            return "", ()
        for fixture_event in persona.events:
            if fixture_event.headline == event.headline:
                return fixture_event.headline, tuple(fixture_event.concepts)
        return str(event.headline), ()
    except Exception:
        return "", ()


def run_all(
    harness: Harness,
    personas: list[Persona],
    *,
    max_rounds: int | None = None,
    horizon_days: float | None = None,
    retention_probes: int = 0,
) -> list[PersonaResult]:
    return [
        run_persona(
            harness,
            p,
            max_rounds=max_rounds,
            horizon_days=horizon_days,
            retention_probes=retention_probes,
        )
        for p in personas
    ]


def merge_usage(
    into: dict[str, dict[str, int]], more: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    for role, bucket in more.items():
        target = into.setdefault(role, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        for key, value in bucket.items():
            target[key] = target.get(key, 0) + int(value or 0)
    return into


@dataclass
class ParallelOutcome:
    """What a parallel replay produced, beyond the results themselves."""

    results: list[PersonaResult]
    unexpected_calls: list[str]
    contract_notes: list[str]
    harness_usage: dict[str, dict[str, int]] = field(default_factory=dict)


def run_all_parallel(
    *,
    db_path,
    run_id: str,
    personas: list[Persona],
    live: bool,
    remediation_search: bool = True,
    max_rounds: int | None = None,
    workers: int = 4,
    routing: HarnessRouting | None = None,
    materiality_cache: MaterialityCache | None = None,
    horizon_days: float | None = None,
    retention_probes: int = 0,
) -> ParallelOutcome:
    """Replay personas concurrently, each on its own system, one shared store.

    Personas are already independent: each has its own user, its own group, and
    a clock that restarts at `EPOCH` so `--persona X` reports the same numbers
    alone as inside a full run. That independence is what makes this safe --
    nothing here changes what is measured, only how long it takes to measure it,
    which under `--live` is almost entirely API latency.

    Three things make it work rather than merely start:

    * **One store file, one connection per thread.** SQLite is opened in WAL
      with a busy timeout, and a connection is never shared across threads. The
      store has to stay shared: `check_isolation` asks whether persona A's rows
      are visible from persona B's scope, which is not a question that can be
      asked of five separate databases.
    * **A thread-local clock.** `clock.installed` rebinds a module global, so
      per-thread dispatch is what stops persona A's rows being stamped with
      persona B's simulated time. See `clock.py`.
    * **Per-thread stub registries and contracts, merged afterwards.** Each
      worker carries its own, so a gap found in one would otherwise vanish from
      the report.
    """
    from concurrent.futures import ThreadPoolExecutor

    from .clock import installed as _installed

    results: dict[str, PersonaResult] = {}
    unexpected: list[str] = []
    notes: list[str] = []
    usage: dict[str, dict[str, int]] = {}
    lock = threading.Lock()

    def one(persona: Persona) -> None:
        local = build_harness(
            db_path=db_path,
            run_id=run_id,
            personas=[persona],
            live=live,
            remediation_search=remediation_search,
            routing=routing,
            materiality_cache=materiality_cache,
        )
        try:
            with _installed(local.clock):
                result = run_persona(
                    local,
                    persona,
                    max_rounds=max_rounds,
                    horizon_days=horizon_days,
                    retention_probes=retention_probes,
                )
            with lock:
                results[persona.id] = result
                notes.extend(local.contract.notes)
                merge_usage(usage, local.harness_usage())
                if local.registry is not None:
                    unexpected.extend(local.registry.unexpected_calls)
                    notes.extend(local.registry.contract_notes)
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, personas))

    # Fixture order, not completion order: the artifact has to be reproducible
    # and a race must not reorder the report.
    return ParallelOutcome(
        results=[results[p.id] for p in personas if p.id in results],
        unexpected_calls=unexpected,
        contract_notes=notes,
        harness_usage=usage,
    )
