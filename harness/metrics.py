"""Scoring, and the structural offline/live split.

The headline number is **concept-set agreement**: precision and recall between
the system's ledger and what the persona actually understands. The knowledge
model is a ledger of concepts, not a score, so a failure reads as a sentence
rather than a delta --

    the system believes they know 'vintage'; they don't.

The single most important property of this module: **an offline run cannot
produce a judgment-quality number.** Not "produces one and labels it
untrustworthy" -- cannot produce one. The enforcement is that `compute()` only
ever *calls* `_judgment_quality()` inside `if live:`. Offline the field holds
`NOT_MEASURED`, a sentinel whose every attribute access raises
`OfflineMetricError`, so code that tries to read a number that was never
measured crashes loudly instead of printing something evidence-shaped.
Serialisation drops the answer-key half of the affected observations too, so
the artifact does not carry the ingredients to recompute in a spreadsheet what
the report declined to state.

Where the offline/live line falls, and why it falls there:

* **Materiality is refused offline.** The stub is handed `should_be_material`
  out of the feed's side registry, so an offline F1 of 1.00 says the wire is
  connected and nothing else.

* **Evidence-extraction accuracy is refused offline.** Comparing what the
  system read out of a reply against what the persona *meant* by it scores the
  stub extractor against the answer key. Meaningless offline; the real question
  under `--live`.

* **Ledger agreement is measured in both modes, and it is honest in both.**
  The offline extractor works from the reply text and never sees
  `knows`/`does_not_know`, so the ledger genuinely can be -- and is -- wrong
  about a persona. Offline that number is about *mechanics*: does evidence flow
  into the right state, does a second correct use move a term from
  `provisional` to `confirmed`, does a revealed misunderstanding revert it all
  the way to `unknown`. Live, the same number is about the model's
  reading. The report says which every time it prints it.
"""

from __future__ import annotations

import json
import random
import re
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from conversational_agent import config
from conversational_agent.store import Store, normalize_term

from .learning import SubdomainMap, token_overlap
from .plainness import plainness as _plainness
from .responders import (
    HOLDING_FORMS,
    LACKING_FORMS,
    SKIP_FIRST_TURN_FACTOR,
    expected_turns_after,
    p_first_turn_after,
    p_first_turn_is_question,
    target_words,
    term_pattern,
)
from .runner import (
    ANSWER_KEY_FIELDS,
    READING_FIELDS,
    PersonaResult,
    ThreadRecord,
    _canon,
    band_bar,
)

REFUSAL = "not measured offline (stubs read the answer key)"

# reply_style_profile.md §8.2: "at least 20% of every persona's replies should
# be under 10 words, and at least 15% should be over 35. A persona whose length
# variance is too low is the most likely single failure of this harness."
STYLE_MIN_SHORT_SHARE = 0.20
STYLE_MIN_LONG_SHARE = 0.15
STYLE_SHORT_WORDS = 10
STYLE_LONG_WORDS = 35

# §8.2's p90 column, kept as data because the long-tail half of the hard rule
# is not applicable to every archetype -- and the document is internally
# inconsistent on this point, so the resolution needs to be visible rather than
# buried in a threshold.
#
# The rule says ">=15% over 35 words" for every persona, but the same table
# gives the barely-informed archetype a p90 of 28 and the disengaged archetype
# a p90 of 14. A distribution whose 90th percentile is 28 cannot put 15% of its
# mass above 35 without abandoning the median the table also specifies. The two
# halves of §8.2 cannot both hold for the short archetypes.
#
# Resolution: the long-tail requirement is enforced only where §8.2's own p90
# clears 35 -- the two archetypes the study actually measured at length. For
# the short archetypes the report says the rule is not applicable rather than
# silently passing them or silently failing them. The short-tail requirement is
# enforced for all four; every archetype's median is low enough to meet it.
ARCHETYPE_P90 = {
    "well_informed": 60,
    "partially_informed": 45,
    "barely_informed": 28,
    "disengaged": 14,
}


class OfflineMetricError(RuntimeError):
    """Raised when something tries to read a judgment-quality number offline."""


class _NotMeasured:
    """A hole where a judgment-quality metric would be, that bites.

    Deliberately not `None` and not `0.0`: both of those quietly flow into a
    format string. This raises.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        raise OfflineMetricError(
            f"{name!r} is a judgment-quality metric and was {REFUSAL}. "
            "Re-run with --live to measure it."
        )

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return REFUSAL


NOT_MEASURED = _NotMeasured()


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if not precision or not recall:
        return None
    return round(2 * precision * recall / (precision + recall), 4)


# --- The headline metric: ledger agreement ---------------------------------


@dataclass
class LedgerPoint:
    """Precision and recall at one interaction."""

    interaction: int
    round_index: int
    believed: int
    true_overlap: int
    precision: float | None
    recall: float | None
    f1: float | None
    meets_threshold: bool


@dataclass
class LedgerAgreement:
    persona_id: str
    archetype: str
    is_noisy: bool
    precision: float | None
    recall: float | None
    f1: float | None
    trajectory: list[LedgerPoint]
    first_met_at: int | None
    held_from: int | None
    interactions_run: int
    coverage: float | None
    false_positives: list[str]
    false_negatives: list[str]
    passed: bool
    # Whether this persona's ledger numbers gate the build.
    #
    # The adversarial persona supplies no evidence whatsoever, so there is
    # nothing for the ledger to be right or wrong *about*: whatever it holds
    # comes from whatever weak evidence the model thought it saw, and how well
    # that holds up is the question `provisional_signal` and
    # `silence_regression` exist to answer, not a bar the system can be asked
    # to clear. Its numbers are reported in full and gate nothing; the
    # properties it does gate are `band_containment` and `silence_regression`.
    gated: bool = True
    # Terms the ledger holds that the fixture never labelled either way.
    # Not errors -- coverage gaps. Reported so they cannot hide.
    unlabelled_beliefs: list[str] = field(default_factory=list)
    # Which answer key this was scored against: `dynamic` (the memory model's
    # P(can define) >= 0.5 at each snapshot; the default) or `static` (the
    # fixture's `knows` plus asked-and-answered, never forgotten; kept for
    # comparison). See harness/LEARNING.md.
    ground_truth: str = "dynamic"


LEDGER_TRUTH_MODES = ("dynamic", "static")


def _truth_of(snapshot, ground_truth: str) -> set[str]:
    if ground_truth == "static":
        return set(getattr(snapshot, "truly_known_static", ()) or ())
    return set(snapshot.truly_known)


def _agreement_at(
    snapshot, labelled: set[str] | None = None, ground_truth: str = "dynamic"
) -> LedgerPoint:
    # Precision is scored only over terms the fixture labelled. A belief about
    # a term with no ground truth is unscored, not wrong -- see the note on
    # `false_positives`. Without this, the system is penalised for reading a
    # concept the fixture author never anticipated, which in an open-vocabulary
    # domain is the normal case rather than the exception.
    believed = set(snapshot.believed_known)
    if labelled is not None:
        believed &= labelled
    truly = _truth_of(snapshot, ground_truth)
    evidenced = set(snapshot.evidenced)
    # Recall is measured over terms the run actually produced evidence about,
    # not over every term the briefings happened to contain.
    #
    # The alternative -- scoring recall over everything the system has ever
    # said in front of the persona -- was tried first and is wrong, for a
    # reason worth recording. It penalises the system for being conservative in
    # exactly the way the design tells it to be. The Assessor deliberately does
    # not promote a term to `explained` on the strength of having defined it
    # unprompted ("we do not know they read the definition, let alone absorbed
    # it"). A denominator that counts those terms therefore marks the system
    # wrong for declining to guess, and the measured effect was large: it put
    # recall below threshold for every persona regardless of whether the ledger
    # made a single incorrect entry.
    #
    # It also collides with `LEDGER_MAX_INTERACTIONS`. Eight short replies
    # cannot demonstrate a dozen concepts -- a persona would have to name every
    # term it knows in every reply, which is precisely the register
    # `reply_style_profile.md` rules out. Recall would then be measuring how
    # chatty the fixture is.
    #
    # So the denominator is the evidence-bearing subset, and `elicitation`
    # below reports how much of the concept set that subset covers, so a run
    # that elicits almost nothing is visible rather than flattered by a high
    # recall over three terms.
    reachable = truly & evidenced
    overlap = believed & truly
    precision = _ratio(len(overlap), len(believed))
    recall = _ratio(len(overlap), len(reachable))
    return LedgerPoint(
        interaction=snapshot.interaction_index,
        round_index=snapshot.round_index,
        believed=len(believed),
        true_overlap=len(overlap),
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
        meets_threshold=(
            precision is not None
            and recall is not None
            and precision >= config.LEDGER_MIN_PRECISION
            and recall >= config.LEDGER_MIN_RECALL
        ),
    )


def ledger_agreement(
    result: PersonaResult, ground_truth: str = "dynamic"
) -> LedgerAgreement:
    """Ledger vs. what the persona actually knows.

    `ground_truth` picks the answer key. `dynamic` (default) is the memory
    model's -- a term is known at time t when P(can define) >= 0.5 at t, so a
    term learned by asking can be forgotten and a term glossed three times in
    three contexts can be learned without a question. `static` is the
    pre-model key -- `knows` plus asked-and-answered, forever -- kept so one
    run can report both side by side.
    """
    if ground_truth not in LEDGER_TRUTH_MODES:
        raise ValueError(f"ground_truth must be one of {LEDGER_TRUTH_MODES}")
    labelled_terms = set(result.initial_knows) | set(result.initial_does_not_know)
    trajectory = [
        _agreement_at(s, labelled_terms, ground_truth) for s in result.snapshots
    ]
    met = [p.meets_threshold for p in trajectory]

    first_met = next(
        (trajectory[i].interaction for i, ok in enumerate(met) if ok), None
    )
    # "Reached and held": the first interaction from which every later one also
    # meets the bar. Touching the threshold once and falling back out is not
    # the system knowing who it is talking to.
    held_from = next(
        (trajectory[i].interaction for i in range(len(met)) if all(met[i:]) and met[i]),
        None,
    )

    final = trajectory[-1] if trajectory else None
    last = result.snapshots[-1] if result.snapshots else None
    believed = set(last.believed_known) if last else set()
    truly = (
        set(result.final_truly_known_static)
        if ground_truth == "static"
        else set(result.final_truly_known)
    )
    evidenced = set(last.evidenced) if last else set()

    # The readable failure. This is the whole point of the redesign: a wrong
    # belief is stated as a sentence about a specific word, not as a distance.
    # Scored only against terms the fixture actually labelled. A term in
    # neither `knows` nor `does_not_know` has no ground truth, and treating
    # its absence as "they are ignorant of it" penalises the system for
    # reading concepts the fixture author did not anticipate.
    #
    # This was a live false failure: `sam_beginner` wrote "so more shares just
    # means existing ones get diluted right" and "that's the oversubscription
    # thing?", producing both terms unprompted and correctly. Neither was in
    # the fixture, so both scored as wrong beliefs. In an open-vocabulary
    # domain -- which is the whole premise -- unanticipated concepts are the
    # norm, so absence of a label must mean unscored, not negative.
    labelled = set(result.initial_knows) | set(result.initial_does_not_know)
    false_positives = [
        f"the system believes they know {term!r}; they don't"
        for term in sorted((believed - truly) & labelled)
    ]
    unlabelled_beliefs = sorted(believed - truly - labelled)
    false_negatives = [
        f"they know {term!r}; the system has it as "
        f"{result.final_ledger.get(term, 'unrecorded')!r}"
        for term in sorted((truly & evidenced) - believed)
    ]

    # Elicitation, not coverage-of-briefings: how much of this persona's
    # concept set the run actually got evidence about. It bounds recall from
    # above, so a strong recall over a tiny evidenced set is not a strong
    # result, and this is the number that says so.
    universe = set(result.initial_knows) | set(result.initial_does_not_know)
    coverage = _ratio(len(evidenced & universe), len(universe))

    return LedgerAgreement(
        persona_id=result.persona_id,
        archetype=result.archetype,
        is_noisy=result.is_noisy,
        precision=final.precision if final else None,
        recall=final.recall if final else None,
        f1=final.f1 if final else None,
        trajectory=trajectory,
        first_met_at=first_met,
        held_from=held_from,
        interactions_run=trajectory[-1].interaction if trajectory else 0,
        coverage=coverage,
        false_positives=false_positives,
        false_negatives=false_negatives,
        unlabelled_beliefs=unlabelled_beliefs,
        passed=(
            held_from is not None and held_from <= config.LEDGER_MAX_INTERACTIONS
        ),
        gated=result.archetype != "disengaged",
        ground_truth=ground_truth,
    )


# --- Learning: the persona memory model and the post-session probe ---------
#
# `harness/learning.py` gives every persona a forgetting curve; `harness/probe.py`
# tests it after each session with 2-4 items graded on a four-rung ladder. These
# metrics summarise the probe: how much of what was just taught can the persona
# say (immediate), how much of what was taught earlier (retained, by interval),
# how often it confabulates a definition for a term that does not exist, and
# whether the graded rung tracks the rung the simulator sampled (calibration;
# ~0 offline by construction, and the number to watch live).


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


@dataclass
class SessionLearning:
    round_index: int
    items: int
    immediate: float | None
    immediate_n: int
    retained: float | None
    retained_n: int
    by_bucket: dict[str, tuple[float | None, int]]
    confabulated: int
    pseudo_n: int
    calibration_gap: float | None


@dataclass
class TeachingEfficiency:
    """Terms the system taught per briefing the persona actually looked at.

    A term counts as *taught* when its FIRST probe graded rung >= 2 and it was
    not known at t = 0 (neither in `knows` nor seeded by subdomain weight).
    It is attributed to the briefing that first glossed it (the exchange's
    `explained_terms`); a term whose first gloss sat in a skipped briefing, or
    that was never glossed at all (learned by asking), is not credited -- the
    denominator is briefings read or skimmed, so only what those briefings
    glossed can be in the numerator.
    """

    taught_terms: list[str]
    briefings_read: int
    words_read: int
    # First-probe outcomes for every non-pseudo term, for the read-through.
    first_probe_rungs: dict[str, int]

    @property
    def taught(self) -> int:
        return len(self.taught_terms)

    @property
    def per_briefing(self) -> float | None:
        return None if not self.briefings_read else round(self.taught / self.briefings_read, 4)

    @property
    def per_100_words(self) -> float | None:
        return None if not self.words_read else round(self.taught * 100.0 / self.words_read, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "taught_terms": list(self.taught_terms),
            "taught": self.taught,
            "briefings_read": self.briefings_read,
            "words_read": self.words_read,
            "per_briefing": self.per_briefing,
            "per_100_words": self.per_100_words,
            "first_probe_rungs": dict(self.first_probe_rungs),
        }


# A least-squares slope over fewer points than this is reported as None: two
# points always fit a line, and a "trend" read off two sessions is noise.
MIN_SLOPE_POINTS = 3


def least_squares_slope(points: list[tuple[float, float]]) -> float | None:
    """Ordinary least-squares slope of y on x; None below `MIN_SLOPE_POINTS`
    or when every x is the same."""
    if len(points) < MIN_SLOPE_POINTS:
        return None
    xs = [float(x) for x, _ in points]
    ys = [float(y) for _, y in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(sxy / sxx, 4)


@dataclass
class PersonaLearning:
    persona_id: str
    prior_knowledge: float
    memory_rate: float
    sessions: list[SessionLearning]
    immediate_mean: float | None
    immediate_n: int
    retained_by_bucket: dict[str, tuple[float | None, int]]
    confabulated: int
    pseudo_n: int
    calibration_gap: float | None
    calibration_n: int
    initial_known: int
    final_known: int
    ever_known: int
    learned: list[str]
    learned_by_asking: list[str]
    forgotten: list[str]
    # --- Learning trend (added with horizon mode) ------------------------------
    # (session ordinal, |known|) at the end of every session that ran -- a
    # thread round or a probe-only day. None-valued counts (artifacts written
    # before `rounds[].known_count` existed) are left out.
    known_by_session: list[tuple[int, int]] = field(default_factory=list)
    known_slope: float | None = None  # terms per session
    # (session ordinal, mean retained rung) over sessions with a retained item.
    retained_by_session: list[tuple[int, float]] = field(default_factory=list)
    retained_slope: float | None = None  # rungs per session
    efficiency: TeachingEfficiency | None = None
    probe_only_sessions: int = 0

    @property
    def confabulation_rate(self) -> float | None:
        return _ratio(self.confabulated, self.pseudo_n)

    @property
    def retained_headline(self) -> tuple[str, float | None]:
        """The longest retention bucket with data, for the one-line summary."""
        from .probe import RETENTION_BUCKET_LABELS

        for label in reversed(RETENTION_BUCKET_LABELS):
            mean, n = self.retained_by_bucket.get(label, (None, 0))
            if n:
                return label, mean
        return "n/a", None

    def retained_at(self, label: str) -> tuple[float | None, int]:
        return self.retained_by_bucket.get(label, (None, 0))

    @property
    def summary_row(self) -> dict[str, Any]:
        """One row of the run-level learning summary table."""
        eff = self.efficiency
        return {
            "persona_id": self.persona_id,
            "prior_knowledge": self.prior_knowledge,
            "memory_rate": self.memory_rate,
            "known_initial": self.initial_known,
            "known_final": self.final_known,
            "known_slope": self.known_slope,
            "retained_slope": self.retained_slope,
            "efficiency_per_briefing": eff.per_briefing if eff else None,
            "efficiency_per_100_words": eff.per_100_words if eff else None,
            "taught": eff.taught if eff else None,
            "briefings_read": eff.briefings_read if eff else None,
            "words_read": eff.words_read if eff else None,
            "calibration_gap": self.calibration_gap,
            "calibration_n": self.calibration_n,
            "confabulated": self.confabulated,
            "pseudo_n": self.pseudo_n,
            "sessions": len(self.sessions),
            "probe_only_sessions": self.probe_only_sessions,
        }

    @property
    def summary_line(self) -> str:
        label, retained = self.retained_headline
        imm = "n/a" if self.immediate_mean is None else f"{self.immediate_mean:.1f}"
        ret = "n/a" if retained is None else f"{retained:.1f}"
        cal = (
            "n/a"
            if self.calibration_gap is None
            else f"{self.calibration_gap:.2f} over {self.calibration_n}"
        )
        return (
            f"{self.persona_id} (K={self.prior_knowledge:.2f}, m={self.memory_rate:.2f}): "
            f"immediate {imm} -> retained@{label} {ret}; "
            f"confab {self.confabulated}/{self.pseudo_n}; "
            f"calibration |graded-sampled| {cal}"
        )


def _bucket_means(items) -> dict[str, tuple[float | None, int]]:
    from .probe import RETENTION_BUCKETS

    out: dict[str, tuple[float | None, int]] = {}
    for label, _, _ in RETENTION_BUCKETS:
        got = [float(i.graded_rung) for i in items if i.bucket == label]
        out[label] = (_mean(got), len(got))
    return out


def learning_report(results: list[PersonaResult]) -> list[PersonaLearning]:
    out: list[PersonaLearning] = []
    for result in results:
        spec = result.learning or {}
        sessions: list[SessionLearning] = []
        all_immediate: list[float] = []
        all_retained = []
        confab = pseudo_n = 0
        gaps: list[float] = []
        for session in result.probes:
            imm = [float(i.graded_rung) for i in session.immediate]
            ret = list(session.retained)
            ps = list(session.pseudo)
            real = [i for i in session.items if not i.is_pseudo]
            session_gaps = [float(i.calibration_gap) for i in real]
            sessions.append(
                SessionLearning(
                    round_index=session.round_index,
                    items=len(session.items),
                    immediate=_mean(imm),
                    immediate_n=len(imm),
                    retained=_mean([float(i.graded_rung) for i in ret]),
                    retained_n=len(ret),
                    by_bucket=_bucket_means(ret),
                    confabulated=sum(1 for i in ps if i.confabulated),
                    pseudo_n=len(ps),
                    calibration_gap=_mean(session_gaps),
                )
            )
            all_immediate += imm
            all_retained += ret
            confab += sum(1 for i in ps if i.confabulated)
            pseudo_n += len(ps)
            gaps += session_gaps
        ever = set(result.ever_known) | set(result.initial_knows)
        final = set(result.final_truly_known)

        # --- trend: |known| per session ---------------------------------------
        # A session is a round that ran a thread or a probe-only day; the
        # ordinal is its position among those, so a quiet round between two
        # sessions does not stretch the x axis.
        rounds = list(getattr(result, "rounds", []) or [])
        session_rounds = [
            r for r in rounds if getattr(r, "thread_ran", False) or getattr(r, "probe_only", False)
        ]
        known_points: list[tuple[int, int]] = []
        snapshots_by_round = {
            s.round_index: len(s.truly_known) for s in (getattr(result, "snapshots", []) or [])
        }
        for ordinal, r in enumerate(session_rounds):
            count = getattr(r, "known_count", None)
            if count is None and not getattr(r, "probe_only", False):
                count = snapshots_by_round.get(r.index)
            if count is not None:
                known_points.append((ordinal, int(count)))
        session_ordinal = {r.index: i for i, r in enumerate(session_rounds)}
        retained_points: list[tuple[int, float]] = [
            (session_ordinal.get(s.round_index, i), float(s.retained))
            for i, s in enumerate(sessions)
            if s.retained is not None and s.retained_n > 0
        ]

        out.append(
            PersonaLearning(
                persona_id=result.persona_id,
                prior_knowledge=float(spec.get("prior_knowledge", 0.0)),
                memory_rate=float(spec.get("memory_rate", 1.0)),
                sessions=sessions,
                immediate_mean=_mean(all_immediate),
                immediate_n=len(all_immediate),
                retained_by_bucket=_bucket_means(all_retained),
                confabulated=confab,
                pseudo_n=pseudo_n,
                calibration_gap=_mean(gaps),
                calibration_n=len(gaps),
                initial_known=len(result.initial_knows),
                final_known=len(final),
                ever_known=len(ever),
                learned=sorted(result.learned),
                learned_by_asking=sorted(result.learned_by_asking),
                forgotten=sorted(ever - final),
                known_by_session=known_points,
                known_slope=least_squares_slope(known_points),
                retained_by_session=retained_points,
                retained_slope=least_squares_slope(retained_points),
                efficiency=teaching_efficiency(result),
                probe_only_sessions=sum(1 for r in rounds if getattr(r, "probe_only", False)),
            )
        )
    return out


def teaching_efficiency(result: Any) -> TeachingEfficiency:
    """See `TeachingEfficiency`. Works on a `PersonaResult` or the artifact
    adapter below; needs `threads`, `probes`, `initial_knows`, `seeded_by_weight`."""
    known_at_start = {
        normalize_term(t) for t in list(getattr(result, "initial_knows", ()) or [])
    } | {normalize_term(t) for t in list(getattr(result, "seeded_by_weight", ()) or [])}

    # First gloss per term, in thread order, with whether that briefing was read.
    first_gloss_read: dict[str, bool] = {}
    briefings_read = 0
    words_read = 0
    for thread in getattr(result, "threads", []) or []:
        reading = getattr(thread, "reading", None)
        act = str(getattr(reading, "intent_act", "") or "") if reading is not None else ""
        if not act:
            # Fall back on the system's band when the harness act is absent.
            quality = str(getattr(reading, "system_quality", "") or "") if reading is not None else ""
            act = "skip" if quality == "skipped" else "read"
        was_read = act != "skip"
        if was_read:
            briefings_read += 1
            words_read += len(str(getattr(thread, "briefing", "") or "").split())
        for term in getattr(thread, "explained_terms", ()) or ():
            key = normalize_term(str(term))
            if key and key not in first_gloss_read:
                first_gloss_read[key] = was_read

    first_probe: dict[str, int] = {}
    for session in getattr(result, "probes", []) or []:
        for item in session.items:
            if item.is_pseudo:
                continue
            key = normalize_term(item.term)
            if key not in first_probe:
                first_probe[key] = int(item.graded_rung)

    taught = sorted(
        term
        for term, rung in first_probe.items()
        if rung >= 2 and term not in known_at_start and first_gloss_read.get(term) is True
    )
    return TeachingEfficiency(
        taught_terms=taught,
        briefings_read=briefings_read,
        words_read=words_read,
        first_probe_rungs=dict(sorted(first_probe.items())),
    )


def learning_report_from_artifact(payload: dict[str, Any]) -> list[PersonaLearning]:
    """The same report, rebuilt from an artifact `harness.run` wrote.

    Used by `harness.gate` (so a baseline written before the trend fields
    existed can still be paired) and by `harness.run --learning-summary`.
    Artifacts written before `rounds[].known_count` existed get no
    `known_slope`; everything else is recoverable from `probes`, `threads`,
    `rounds` and `true_concepts`.
    """
    from types import SimpleNamespace

    from .probe import ProbeItem, ProbeSession

    results = []
    for p in payload.get("personas", []) or []:
        probes = []
        for session in p.get("probes") or []:
            items = tuple(
                ProbeItem(
                    term=str(i.get("term", "")),
                    is_pseudo=bool(i.get("is_pseudo", False)),
                    kind=str(i.get("kind", "")),
                    sampled_rung=int(i.get("sampled_rung", 0) or 0),
                    graded_rung=int(i.get("graded_rung", 0) or 0),
                    confabulated=bool(i.get("confabulated", False)),
                    days_since_last_exposure=i.get("days_since_last_exposure"),
                    r_at_probe=float(i.get("r_at_probe", 0.0) or 0.0),
                    p_define_at_probe=float(i.get("p_define_at_probe", 0.0) or 0.0),
                )
                for i in session.get("items", [])
            )
            probes.append(
                ProbeSession(round_index=int(session.get("round", 0)), at=str(session.get("at", "")), items=items)
            )
        threads = [
            SimpleNamespace(
                round_index=t.get("round"),
                explained_terms=tuple(t.get("explained_terms") or ()),
                briefing=t.get("briefing") or "",
                reading=SimpleNamespace(
                    intent_act=(t.get("reading") or {}).get("intent_act", ""),
                    system_quality=(t.get("reading") or {}).get("system_read_quality", ""),
                ),
            )
            for t in p.get("threads") or []
        ]
        rounds = [
            SimpleNamespace(
                index=r.get("index"),
                thread_ran=bool(r.get("thread_ran", False)),
                probe_only=bool(r.get("probe_only", False)),
                known_count=r.get("known_count"),
                quiet=bool(r.get("quiet", False)),
            )
            for r in p.get("rounds") or []
        ]
        truth = p.get("true_concepts") or {}
        learning = p.get("learning") or {}
        results.append(
            SimpleNamespace(
                persona_id=p.get("persona_id"),
                learning={
                    "prior_knowledge": learning.get("prior_knowledge", 0.0),
                    "memory_rate": learning.get("memory_rate", 1.0),
                },
                probes=probes,
                threads=threads,
                rounds=rounds,
                snapshots=[],
                initial_knows=tuple(truth.get("initial_known") or ()),
                seeded_by_weight=tuple((p.get("subdomains") or {}).get("seeded_by_weight") or ()),
                final_truly_known=tuple(truth.get("final_known") or ()),
                ever_known=tuple(truth.get("ever_known") or ()),
                learned=tuple(truth.get("learned_during_run") or ()),
                learned_by_asking=tuple(truth.get("learned_by_asking") or ()),
            )
        )
    return learning_report(results)  # type: ignore[arg-type]


def _fmt_opt(value: float | None, spec: str = ".2f", width: int = 6) -> str:
    """Right-align an optional number; `spec` is `.2f` or `+.2f`."""
    if value is None:
        return f"{'n/a':>{width}}"
    sign = "+" if spec.startswith("+") else ""
    return format(value, f">{sign}{width}{spec.lstrip('+')}")


LEARNING_SUMMARY_COLUMNS = (
    "persona",
    "K",
    "m",
    "known start->end",
    "known_slope",
    "retained_slope",
    "efficiency (terms/briefing, terms/100w)",
    "calibration gap",
    "confab",
)


def render_learning_summary(entries: list[PersonaLearning]) -> str:
    """The run-level learning summary table, one row per persona."""
    lines = [
        f"    {'persona':<20} {'K':>5} {'m':>5} {'known':>9} {'k_slope':>8} {'r_slope':>8} "
        f"{'eff/brf':>8} {'eff/100w':>9} {'calib':>12} {'confab':>7}"
    ]
    for e in entries:
        eff = e.efficiency
        known = f"{e.initial_known}->{e.final_known}"
        calib = "n/a" if e.calibration_gap is None else f"{e.calibration_gap:.2f} (n={e.calibration_n})"
        eff_b = "n/a" if eff is None or eff.per_briefing is None else f"{eff.per_briefing:.2f}"
        eff_w = "n/a" if eff is None or eff.per_100_words is None else f"{eff.per_100_words:.2f}"
        lines.append(
            f"    {e.persona_id:<20} {e.prior_knowledge:>5.2f} {e.memory_rate:>5.2f} {known:>9} "
            f"{_fmt_opt(e.known_slope, '+.2f', 8)} {_fmt_opt(e.retained_slope, '+.2f', 8)} "
            f"{eff_b:>8} {eff_w:>9} {calib:>12} {e.confabulated:>3}/{e.pseudo_n:<3}"
        )
    lines.append(
        "    k_slope: terms known (p_define >= 0.5) per session, least squares; r_slope: mean retained rung per\n"
        "    session; eff: terms taught (first probe >= rung 2, not known at t=0, glossed by a read/skimmed\n"
        f"    briefing) per such briefing and per 100 words read; slopes need >= {MIN_SLOPE_POINTS} points, else n/a."
    )
    return "\n".join(lines)


# --- Subdomains: does the system see the SHAPE of what the reader knows? -----
#
# The human's direction: the glossary is not a word-to-word match; it should
# cover the subdomains within the group the user is familiar with, and the
# system should use that, among other things, to decide how deeply to explain.
# Three questions, all reported and none gated on this first pass:
#
#   (a) labels  -- does the system's `concepts.subdomain` for a term agree with
#       the fixture's taxonomy? Exact after `normalize_term`, and a looser
#       "near" match on token overlap (`transfer market` ~ `transfer calendar`).
#   (b) familiarity -- does `UserScope.subdomain_familiarity` rank the
#       subdomains the way the persona's true weights do? Spearman over the
#       subdomains the system has attested at least one term in, plus the mean
#       absolute gap between its known/attested ratio and the true weight.
#   (c) depth fit -- how PLAINLY each briefing is written, split by whether
#       the briefing's event fell mostly in a subdomain the persona holds
#       (weight >= 0.7) or does not (<= 0.3). Under `briefing` v7 the human's
#       direction is plain language over inline definitions ("it shouldn't
#       have too many literal definitions it should just talk in simpler
#       language and explain topics more which can occasionally include a
#       definition"), so definitions per 100 words is no longer the depth
#       measure -- it should be low everywhere -- and depth is read from the
#       plainness bundle (`harness/plainness.py`): Flesch-Kincaid grade,
#       sentence length, domain-term density and glosses (parenthetical
#       asides, dash pairs and "X, meaning Y" cues) per 100 words. Under v7
#       `explained_terms` also counts terms made clear by plain explanation,
#       so `definitions_per_100w` is reported as "made clear/100w" and is no
#       longer a gloss count. Two verdicts, both reported and neither gated:
#       `plainer for beginners` (grade AND density lower in not-held than in
#       held) and `definitions occasional` (glosses -- asides/100w -- <=
#       GLOSSES_OCCASIONAL_MAX in every bucket).
#
# Everything on the fixture side is answer key and stays here; the system's
# side is read back from the store and never written.

HELD_WEIGHT = 0.7
NOT_HELD_WEIGHT = 0.3
# "Occasionally include a definition": at most one formal gloss (parenthetical
# aside, dash pair or "X, meaning Y" cue) per hundred words, as a bucket mean.
# Report-only.
GLOSSES_OCCASIONAL_MAX = 1.0


@dataclass
class DepthBucket:
    label: str
    briefings: int
    mean_fk_grade: float | None
    mean_sentence_length: float | None
    mean_domain_term_density: float | None
    mean_asides_per_100w: float | None
    mean_definitions_per_100_words: float | None
    mean_definitions: float | None
    mean_words: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "briefings": self.briefings,
            "mean_fk_grade": self.mean_fk_grade,
            "mean_sentence_length": self.mean_sentence_length,
            "mean_domain_term_density": self.mean_domain_term_density,
            "mean_asides_per_100w": self.mean_asides_per_100w,
            "mean_definitions_per_100_words": self.mean_definitions_per_100_words,
            "mean_definitions": self.mean_definitions,
            "mean_words": self.mean_words,
        }


@dataclass
class PersonaSubdomain:
    persona_id: str
    archetype: str
    taxonomy: dict[str, list[str]]
    true_weights: dict[str, float]
    seeded_by_weight: list[str]
    # (a) labels
    labels_exported: bool
    labelled_terms: int
    exact_matches: int
    near_matches: int
    label_examples: list[str]
    unlabelled_ledger_terms: int
    # runtime placement of glossed terms outside the vocabulary
    runtime_assignments: dict[str, str]  # term -> "subdomain (how)"
    # (b) familiarity
    familiarity_exported: bool
    system_familiarity: dict[str, dict[str, Any]]
    compared_subdomains: int
    familiarity_rho: float | None
    familiarity_mean_gap: float | None
    familiarity_rows: list[str]
    unmatched_system_subdomains: list[str]
    # attested terms the system has not labelled at all (its own bucket)
    unlabelled_attested: int
    # (c) depth fit (plainness; see harness/plainness.py)
    depth: dict[str, DepthBucket]
    plainer_for_beginners: bool | None
    definitions_occasional: bool | None
    depth_rows: list[str]

    @property
    def exact_rate(self) -> float | None:
        return _ratio(self.exact_matches, self.labelled_terms)

    @property
    def near_rate(self) -> float | None:
        return _ratio(self.near_matches, self.labelled_terms)

    @property
    def summary_line(self) -> str:
        if not self.taxonomy:
            return f"{self.persona_id}: fixture carries no subdomain taxonomy"
        weights = ", ".join(f"{k} {v:.2f}" for k, v in self.true_weights.items())
        if not self.labels_exported:
            labels = "labels: system exports no `concepts.subdomain` yet"
        elif not self.labelled_terms:
            labels = f"labels: none on the {self.unlabelled_ledger_terms} scored ledger term(s)"
        else:
            labels = (
                f"labels: exact {_pct(self.exact_rate)} near {_pct(self.near_rate)} "
                f"over {self.labelled_terms}"
            )
        if not self.familiarity_exported:
            fam = "familiarity: not exported"
        elif not self.compared_subdomains:
            fam = "familiarity: no subdomain attested yet"
        else:
            rho = "n/a" if self.familiarity_rho is None else f"{self.familiarity_rho:.2f}"
            gap = "n/a" if self.familiarity_mean_gap is None else f"{self.familiarity_mean_gap:.2f}"
            fam = f"familiarity: ρ={rho} |gap| {gap} over {self.compared_subdomains}"
        held = self.depth.get("held")
        not_held = self.depth.get("not held")

        def _pair(attr: str, spec: str = ".1f") -> str:
            def one(b: DepthBucket | None) -> str:
                if b is None or not b.briefings or getattr(b, attr) is None:
                    return "n/a"
                return format(getattr(b, attr), spec)
            return f"held {one(held)} vs not held {one(not_held)}"

        def _yn(v: bool | None) -> str:
            return "n/a" if v is None else ("YES" if v else "NO")

        n_held = held.briefings if held else 0
        n_not = not_held.briefings if not_held else 0
        return (
            f"{self.persona_id} [{weights}]: {labels}; {fam}; "
            f"plainness (n held {n_held} / not held {n_not}): grade {_pair('mean_fk_grade')}, "
            f"terms/100w {_pair('mean_domain_term_density')}, glosses/100w {_pair('mean_asides_per_100w', '.2f')} -- "
            f"plainer for beginners: {_yn(self.plainer_for_beginners)}; "
            f"definitions occasional (glosses/100w <= {GLOSSES_OCCASIONAL_MAX:.1f}): {_yn(self.definitions_occasional)}"
        )


def _familiarity_ratio(entry: Mapping[str, Any]) -> float | None:
    try:
        known = float(entry.get("known", 0) or 0)
        attested = float(entry.get("attested", 0) or 0)
    except (TypeError, ValueError):
        return None
    if attested <= 0:
        return None
    return max(0.0, min(1.0, known / attested))


def subdomain_report(results: list[PersonaResult], personas: list[Any]) -> list[PersonaSubdomain]:
    by_id = {p.id: p for p in personas}
    out: list[PersonaSubdomain] = []
    for result in results:
        persona = by_id.get(result.persona_id)
        smap = persona.subdomain_map if persona is not None else None
        if smap is None and result.fixture_subdomains and result.familiar_subdomains:
            smap = SubdomainMap(
                terms=dict(result.fixture_subdomains), weights=dict(result.familiar_subdomains)
            )
        taxonomy = {k: list(v) for k, v in (smap.terms.items() if smap else {})}
        weights = dict(smap.weights) if smap else {}
        # A scored term's fixture subdomain, keyed the way the ledger keys it.
        truth: dict[str, str] = {}
        if smap is not None:
            vocab = tuple(result.vocabulary)
            for term in vocab:
                name = smap.of(term)
                if name is not None:
                    truth[normalize_term(term)] = name

        # --- (a) labels ----------------------------------------------------
        labels_exported = any(v is not None for v in result.ledger_subdomains.values())
        labelled = exact = near = 0
        examples: list[str] = []
        unlabelled = 0
        for term, label in result.ledger_subdomains.items():
            key = normalize_term(term)
            if key not in truth:
                continue  # unscored: the fixture never placed it
            if not label:
                unlabelled += 1
                continue
            labelled += 1
            matched, how = smap.match_label(label) if smap else (None, "none")
            if matched == truth[key] and how == "exact":
                exact += 1
                near += 1
            elif matched == truth[key]:
                near += 1
            elif token_overlap(label, truth[key]) > 0:
                near += 1
            else:
                if len(examples) < 6:
                    examples.append(f"{term!r}: system {label!r} vs fixture {truth[key]!r}")
        runtime = {
            term: f"{name} ({how})"
            for term, (name, how) in sorted(result.subdomain_assignments.items())
            if how != "declared"
        }

        # --- (b) familiarity ------------------------------------------------
        fam_exported = bool(result.subdomain_familiarity_exported)
        xs: list[float] = []
        ys: list[float] = []
        gaps: list[float] = []
        fam_rows: list[str] = []
        unmatched: list[str] = []
        unlabelled_bucket = config.UNLABELLED_SUBDOMAIN
        unlabelled_attested = 0
        for sys_name, entry in sorted(result.system_subdomain_familiarity.items()):
            ratio = _familiarity_ratio(entry)
            if ratio is None:
                continue  # nothing attested there yet
            if sys_name == unlabelled_bucket:
                # The system's own "not labelled yet" bucket: not a label, so
                # not compared; its size is reported instead.
                unlabelled_attested = int(entry.get("attested", 0) or 0)
                continue
            matched, how = smap.match_label(sys_name) if smap else (None, "none")
            if matched is None:
                unmatched.append(sys_name)
                continue
            truth_w = weights.get(matched, 0.0)
            xs.append(ratio)
            ys.append(truth_w)
            gaps.append(abs(ratio - truth_w))
            fam_rows.append(
                f"{sys_name!r} -> {matched!r} ({how}): system {ratio:.2f} "
                f"({entry.get('known', 0)}/{entry.get('attested', 0)}, "
                f"band {entry.get('band', '?')}) vs true {truth_w:.2f}"
            )

        # --- (c) depth fit: plainness by bucket ---------------------------
        buckets: dict[str, list[dict[str, Any]]] = {
            "held": [], "middle": [], "not held": [], "unplaced": []
        }
        depth_rows: list[str] = []
        for thread in result.threads:
            bundle = _plainness(thread.briefing, result.vocabulary, thread.explained_terms)
            dominant, dom_w = _dominant_subdomain(thread.event_concepts, smap)
            if dominant is None:
                bucket = "unplaced"
            elif dom_w >= HELD_WEIGHT:
                bucket = "held"
            elif dom_w <= NOT_HELD_WEIGHT:
                bucket = "not held"
            else:
                bucket = "middle"
            buckets[bucket].append(bundle)
            where = (
                f"event in {dominant!r} (w={dom_w:.2f}) -> {bucket}"
                if dominant is not None
                else "event names no fixture concept -> unplaced"
            )
            depth_rows.append(
                f"r{thread.round_index}: grade {bundle['fk_grade']:.1f}, "
                f"{bundle['mean_sentence_length']:.1f} w/sent, "
                f"terms {bundle['domain_term_density']:.1f}/100w, "
                f"glosses {bundle['asides_per_100w']:.2f}/100w, "
                f"made clear {bundle['definitions_per_100w']:.2f}/100w "
                f"({bundle['parenthetical_asides']} gloss(es), {bundle['definitions']} term(s) / {bundle['words']} words); {where}"
            )
        depth: dict[str, DepthBucket] = {}
        for label, rows in buckets.items():
            depth[label] = DepthBucket(
                label=label,
                briefings=len(rows),
                mean_fk_grade=_mean([float(r["fk_grade"]) for r in rows]),
                mean_sentence_length=_mean([float(r["mean_sentence_length"]) for r in rows]),
                mean_domain_term_density=_mean([float(r["domain_term_density"]) for r in rows]),
                mean_asides_per_100w=_mean([float(r["asides_per_100w"]) for r in rows]),
                mean_definitions_per_100_words=_mean([float(r["definitions_per_100w"]) for r in rows]),
                mean_definitions=_mean([float(r["definitions"]) for r in rows]),
                mean_words=_mean([float(r["words"]) for r in rows]),
            )
        held_b, not_b = depth["held"], depth["not held"]
        plainer: bool | None
        if held_b.briefings and not_b.briefings:
            plainer = bool(
                not_b.mean_fk_grade < held_b.mean_fk_grade
                and not_b.mean_domain_term_density < held_b.mean_domain_term_density
            )
        else:
            plainer = None
        occupied = [b for b in depth.values() if b.briefings]
        occasional: bool | None = (
            all(b.mean_asides_per_100w <= GLOSSES_OCCASIONAL_MAX for b in occupied)
            if occupied
            else None
        )

        out.append(
            PersonaSubdomain(
                persona_id=result.persona_id,
                archetype=result.archetype,
                taxonomy=taxonomy,
                true_weights=weights,
                seeded_by_weight=sorted(result.seeded_by_weight),
                labels_exported=labels_exported,
                labelled_terms=labelled,
                exact_matches=exact,
                near_matches=near,
                label_examples=examples,
                unlabelled_ledger_terms=unlabelled,
                runtime_assignments=runtime,
                familiarity_exported=fam_exported,
                system_familiarity=dict(result.system_subdomain_familiarity),
                compared_subdomains=len(xs),
                familiarity_rho=_spearman(xs, ys),
                familiarity_mean_gap=_mean(gaps),
                familiarity_rows=fam_rows,
                unmatched_system_subdomains=unmatched,
                unlabelled_attested=unlabelled_attested,
                depth=depth,
                plainer_for_beginners=plainer,
                definitions_occasional=occasional,
                depth_rows=depth_rows,
            )
        )
    return out


def _dominant_subdomain(
    concepts: tuple[str, ...], smap: SubdomainMap | None
) -> tuple[str | None, float]:
    """The subdomain most of an event's concepts fall in, and its weight.
    Ties go to the heavier subdomain (the one the reader is likelier to hold)."""
    if smap is None or not concepts:
        return None, 0.0
    counts: dict[str, int] = {}
    for term in concepts:
        name = smap.of(term)
        if name is not None:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return None, 0.0
    best = max(counts, key=lambda n: (counts[n], smap.weight(n), n))
    return best, smap.weight(best)



# --- Per-state accuracy: the weak-evidence path ----------------------------


@dataclass
class StateAccuracy:
    state: str
    n: int
    correct: int
    accuracy: float | None
    wrong_examples: list[str]


@dataclass
class PersonaReadingIntent:
    """How one persona read, what its skips meant, and whether the system saw it.

    Everything on the persona side is harness ground truth (`ReadingIntent`);
    everything on the system side was read back from the store. The three
    numbers that matter, in order: attention agreement (rank correlation of
    the system's 0-10 readout with the true depth ordinal), skip
    classification (precision/recall for `informed` over the skips the system
    resolved), and prior convergence (|system p_informed - true informed-skip
    rate| at the last session).
    """

    persona_id: str
    profile: str
    acts: dict[str, int]                 # "informed skip" / "lazy skip" / "skim" / "read" / "study"
    attention_rho: float | None
    attention_n: int
    skips: int
    resolved: int
    unresolved: int
    informed_precision: float | None
    informed_recall: float | None
    misclassified: list[str]
    true_informed_rate: float | None
    p_informed_trajectory: list[float]
    final_gap: float | None

    @property
    def summary_line(self) -> str:
        acts = ", ".join(f"{n} {k}" for k, n in self.acts.items() if n) or "no reading"
        rho = "n/a" if self.attention_rho is None else f"{self.attention_rho:.2f}"
        cls = (
            f"skips classified {self.resolved}/{self.skips} ({self.unresolved} unresolved)"
            if self.skips
            else "no skips"
        )
        pr = (
            f", informed P {_pct(self.informed_precision)} R {_pct(self.informed_recall)}"
            if self.resolved
            else ""
        )
        prior = (
            f"p_informed {self.p_informed_trajectory[-1]:.2f} vs true "
            f"{'n/a' if self.true_informed_rate is None else f'{self.true_informed_rate:.2f}'}"
            if self.p_informed_trajectory
            else "p_informed n/a"
        )
        return (
            f"{self.persona_id} ({self.profile}): {acts}; system: attention ρ={rho} "
            f"(n={self.attention_n}), {cls}{pr}, {prior}"
        )


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation with average ranks for ties; None below n=3 or if flat."""
    n = len(xs)
    if n < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None

    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return None if den == 0 else round(num / den, 3)


def reading_intent_report(results: list[PersonaResult]) -> list[PersonaReadingIntent]:
    out: list[PersonaReadingIntent] = []
    for result in results:
        readings = [t for t in result.threads if t.reading is not None]
        if not readings:
            continue
        acts = {"informed skip": 0, "lazy skip": 0, "skim": 0, "read": 0, "study": 0}
        att_x: list[float] = []
        att_y: list[float] = []
        skips = resolved = unresolved = 0
        tp = fp = fn = 0
        bad: list[str] = []
        for t in readings:
            r = t.reading
            if r.intent_act == "skip":
                acts["informed skip" if r.intent_informed else "lazy skip"] += 1
                skips += 1
                kind = result.skip_kinds.get(t.exchange_id, "unresolved")
                if kind == "unresolved":
                    unresolved += 1
                else:
                    resolved += 1
                    truth = bool(r.intent_informed)
                    said = kind == "informed"
                    if said and truth:
                        tp += 1
                    elif said and not truth:
                        fp += 1
                        bad.append(
                            f"r{t.round_index}: lazy skip called informed"
                        )
                    elif truth and not said:
                        fn += 1
                        bad.append(
                            f"r{t.round_index}: informed skip called lazy"
                        )
            elif r.intent_act in acts:
                acts[r.intent_act] += 1
            if r.system_attention is not None:
                att_x.append(float(r.system_attention))
                att_y.append(float(r.depth))
        true_rate = (acts["informed skip"] / skips) if skips else None
        traj = [
            float(rd.reading_pattern.get("p_informed"))
            for rd in result.rounds
            if rd.reading_pattern and rd.reading_pattern.get("p_informed") is not None
        ]
        final_gap = (
            round(abs(traj[-1] - true_rate), 3) if traj and true_rate is not None else None
        )
        out.append(
            PersonaReadingIntent(
                persona_id=result.persona_id,
                profile=readings[0].reading.profile,
                acts=acts,
                attention_rho=_spearman(att_x, att_y),
                attention_n=len(att_x),
                skips=skips,
                resolved=resolved,
                unresolved=unresolved,
                informed_precision=(tp / (tp + fp)) if (tp + fp) else None,
                informed_recall=(tp / (tp + fn)) if (tp + fn) else None,
                misclassified=bad,
                true_informed_rate=true_rate,
                p_informed_trajectory=traj,
                final_gap=final_gap,
            )
        )
    return out


@dataclass
class PerStateReport:
    by_state: list[StateAccuracy]
    provisional_wrong_total: int
    provisional_total: int

    @property
    def provisional_accuracy(self) -> float | None:
        return _ratio(
            self.provisional_total - self.provisional_wrong_total,
            self.provisional_total,
        )


# The evidence string `assumed` wrote when it was promoting a term on silence
# alone. `assumed` is gone -- `note_exposure` promotes nothing -- so nothing
# should ever write this again. It is kept here as a fingerprint rather than
# deleted: `silence_regression` greps the ledger for it, which is what turns
# "we removed the state" into a check that stays true.
SILENCE_EVIDENCE_MARKER = "without being questioned"


def per_state_accuracy(results: list[PersonaResult]) -> PerStateReport:
    """Of the terms the system marked known, at each tier, how many were?

    Reported per state rather than pooled, because the states carry very
    different evidence and the whole point of the ledger is that the strength
    of a claim is visible. `confirmed` means the person used the word correctly
    twice, unprompted. `explained` means they asked and we answered.
    `provisional` -- the surviving weak tier -- means exactly one correct
    unprompted use, held below `confirmed` because the briefing had just used
    the term and echoing it back may be parroting.

    `provisional` is the state this report exists to interrogate. It is the
    same question that killed `assumed`: does the weakest evidence in the model
    actually predict knowing, or is it a state that fires without carrying
    information? `provisional_signal` answers it with a lift; this function
    supplies the raw accuracy and the named counterexamples.
    """
    buckets: dict[str, dict[str, Any]] = {
        state: {"n": 0, "correct": 0, "wrong": []} for state in config.KNOWN_STATES
    }
    buckets[config.CONCEPT_UNKNOWN] = {"n": 0, "correct": 0, "wrong": []}

    bar = band_bar()
    for result in results:
        truly = set(result.final_truly_known)
        for term, state in result.final_ledger.items():
            bucket = buckets.setdefault(state, {"n": 0, "correct": 0, "wrong": []})
            bucket["n"] += 1
            is_known = term in truly
            # For `unknown`, "correct" means the person genuinely does not know
            # it; for every other state it means they do.
            correct = (not is_known) if state == config.CONCEPT_UNKNOWN else is_known
            state_label = state
            # `familiar` / `explained` are split by read_explanations bucket --
            # P(known | familiar) at one exposure and at the band bar. The same
            # test that killed `assumed` and vindicated `provisional`; the
            # human chose `familiar` having seen `explained` at 0.50, on the
            # rationale that familiarity strengthens over time, so measuring it
            # at both counts is the deal.
            if state in (config.CONCEPT_FAMILIAR, config.CONCEPT_EXPLAINED):
                reads = int(result.ledger_read_explanations.get(term, 0))
                sub_label = f"{state} (read_explanations {'>= ' + str(bar) if reads >= bar else '< ' + str(bar)})"
                sub = buckets.setdefault(sub_label, {"n": 0, "correct": 0, "wrong": []})
                sub["n"] += 1
                if correct:
                    sub["correct"] += 1
                elif len(sub["wrong"]) < 8:
                    sub["wrong"].append(
                        f"{result.persona_id}: {term!r} marked {sub_label!r} but they "
                        + ("do know it" if is_known else "don't know it")
                    )
            if correct:
                bucket["correct"] += 1
            elif len(bucket["wrong"]) < 8:
                bucket["wrong"].append(
                    f"{result.persona_id}: {term!r} marked {state_label!r} but they "
                    + ("do know it" if is_known else "don't know it")
                )

    by_state = [
        StateAccuracy(
            state=state,
            n=bucket["n"],
            correct=bucket["correct"],
            accuracy=_ratio(bucket["correct"], bucket["n"]),
            wrong_examples=bucket["wrong"],
        )
        for state, bucket in sorted(buckets.items())
    ]
    prov = buckets.get(config.CONCEPT_PROVISIONAL, {"n": 0, "correct": 0})
    return PerStateReport(
        by_state=by_state,
        provisional_total=prov["n"],
        provisional_wrong_total=prov["n"] - prov["correct"],
    )


# --- Does `provisional` carry any information at all? ----------------------


@dataclass
class ProvisionalSignal:
    """`P(known | provisional)` against the base rate. The test that killed `assumed`.

    `assumed` was deleted on this measurement, not on an argument: it inferred
    knowledge from silence, scored 0/2 against persona ground truth, and came
    out anti-predictive at every exposure bar tried (lift -0.34 at one, -1.00
    at three and five). Raising the bar made it fire less often and never made
    it informative.

    `provisional` is the weakest evidence still standing, and it deserves the
    same treatment rather than a pass for being next in line. Its claim is
    stronger on its face -- someone used the term correctly, unprompted, which
    is a positive act rather than an absence -- but it has a specific confound
    the design already admits to: the briefing used the term moments earlier,
    so one echo may be parroting. Whether that confound eats the whole signal
    is an empirical question and this is the number that answers it.

    Two controls are reported, because the honest comparison depends on what
    the alternative to `provisional` actually is:

      * `control` -- terms the persona has been exposed to at least once that
        are not `provisional`. Contaminated in both directions: it holds
        `explained` and `confirmed` terms, which carry real positive evidence
        and inflate it, and `unknown` terms that got there by a revealed
        misunderstanding, which deflate it.
      * `unknown_rate` -- terms the ledger left at `unknown`. This is the
        cleaner counterfactual and the one to read: delete `provisional` and
        every term in it lands here instead. If `P(known | provisional)` is no
        better than `P(known | unknown)`, the state is decorating a coin flip.
    """

    provisional_n: int
    provisional_known: int
    provisional_rate: float | None
    control_n: int
    control_known: int
    control_rate: float | None
    unknown_n: int
    unknown_known: int
    unknown_rate: float | None
    overall_n: int
    overall_known: int
    overall_rate: float | None

    @property
    def lift(self) -> float | None:
        if self.provisional_rate is None or self.control_rate is None:
            return None
        return round(self.provisional_rate - self.control_rate, 4)

    @property
    def lift_over_unknown(self) -> float | None:
        """The counterfactual lift: `provisional` against leaving it `unknown`."""
        if self.provisional_rate is None or self.unknown_rate is None:
            return None
        return round(self.provisional_rate - self.unknown_rate, 4)

    @property
    def verdict(self) -> str:
        if self.provisional_n == 0:
            return "no `provisional` entries in this run -- nothing to score"
        lift = self.lift_over_unknown
        if lift is None:
            return "no comparable control group in this run"
        if lift >= 0.15:
            return (
                "`provisional` predicts knowledge above the state it would "
                "otherwise sit in -- one correct use carries signal"
            )
        if lift <= -0.15:
            return (
                "`provisional` predicts knowledge WORSE than leaving the term "
                "`unknown` -- the same finding that retired `assumed`"
            )
        return (
            "`provisional` is indistinguishable from leaving the term "
            "`unknown` -- on this evidence one correct use carries no "
            "information and the state should be deleted, not down-weighted"
        )


def provisional_signal(results: list[PersonaResult]) -> ProvisionalSignal:
    """Score `provisional` against the states a term would otherwise be in."""
    p_n = p_known = c_n = c_known = u_n = u_known = o_n = o_known = 0

    for result in results:
        truly = set(result.final_truly_known)
        for term, state in result.final_ledger.items():
            known = term in truly
            o_n += 1
            o_known += int(known)
            if state == config.CONCEPT_PROVISIONAL:
                p_n += 1
                p_known += int(known)
            elif result.exposure_counts.get(term, 0) >= 1:
                c_n += 1
                c_known += int(known)
            if state == config.CONCEPT_UNKNOWN:
                u_n += 1
                u_known += int(known)

    return ProvisionalSignal(
        provisional_n=p_n,
        provisional_known=p_known,
        provisional_rate=_ratio(p_known, p_n),
        control_n=c_n,
        control_known=c_known,
        control_rate=_ratio(c_known, c_n),
        unknown_n=u_n,
        unknown_known=u_known,
        unknown_rate=_ratio(u_known, u_n),
        overall_n=o_n,
        overall_known=o_known,
        overall_rate=_ratio(o_known, o_n),
    )


# --- Exposure: counted, never inferential ----------------------------------


@dataclass
class ExposureBucket:
    at_least: int
    n: int
    known: int
    rate: float | None


@dataclass
class ExposureReport:
    """What repetition alone predicts, reported without ever acting on it.

    The question is "does having seen a term N times predict knowing it?",
    answered directly from the run's own exposure counts and ground truth.

    Read it as the standing evidence for why exposure promotes nothing: if
    `P(known | exposed >= N)` sits flat near the base rate as N climbs, there
    is no bar at which a repetition-based rule would work.
    """

    buckets: list[ExposureBucket]
    base_rate: float | None
    max_exposures: int


def exposure_report(results: list[PersonaResult], max_bar: int = 5) -> ExposureReport:
    total_n = total_known = 0
    observed_max = 0
    for result in results:
        truly = set(result.final_truly_known)
        for term in result.final_ledger:
            total_n += 1
            total_known += int(term in truly)
            observed_max = max(observed_max, result.exposure_counts.get(term, 0))

    buckets: list[ExposureBucket] = []
    for bar in range(1, max_bar + 1):
        n = known = 0
        for result in results:
            truly = set(result.final_truly_known)
            for term, count in result.exposure_counts.items():
                if count >= bar:
                    n += 1
                    known += int(term in truly)
        buckets.append(
            ExposureBucket(at_least=bar, n=n, known=known, rate=_ratio(known, n))
        )

    return ExposureReport(
        buckets=buckets,
        base_rate=_ratio(total_known, total_n),
        max_exposures=observed_max,
    )


# --- The one sanctioned path from reading into the ledger ------------------
#
# `familiar` (human decision, checkpoint 3): a term the system DEFINED in a
# briefing (`exchanges.explained_terms`, not merely mentioned) which the user
# then read or skimmed becomes `familiar`. One such exposure stops the
# re-glossing; it counts toward the band only at
# `config.READ_EXPLANATIONS_BEFORE_BAND`. It is reached without a user turn by
# construction, so every gate below that used to say "no known state without a
# turn behind it" now says "...or `familiar`, traceable to a read exchange that
# glossed it". The trace requirement is what keeps this narrow: a `familiar`
# term that no read exchange glossed is a violation, exactly as before.

SANCTIONED_FAMILIAR_EVIDENCE = re.compile(
    r"^defined in a briefing they (read|skimmed|studied)$"
)


def glossed_in_read_exchange(result: PersonaResult) -> set[str]:
    """Terms some closed, non-skipped exchange of this persona DEFINED inline."""
    out: set[str] = set()
    for thread in result.threads:
        band = thread.reading.system_quality if thread.reading else ""
        if band and band != "skipped":
            out |= set(_canon(thread.explained_terms))
    return out


def glossed_anywhere(result: PersonaResult) -> set[str]:
    out: set[str] = set()
    for thread in result.threads:
        out |= set(_canon(thread.explained_terms))
    return out


# --- Regression: silence-based inference must never come back --------------


@dataclass
class SilenceRegression:
    """A hard gate that `assumed`, or anything shaped like it, stays deleted.

    `assumed` was a state the system could enter with no input from the user at
    all. It is gone, and `theo_silent` -- a persona that replies to everything
    and supplies no evidence in any reply -- is the instrument that proves it
    stays gone. With promotion removed this should be trivially safe, which is
    exactly what makes it a good regression test: it costs nothing to hold and
    it fails loudly the moment anyone reintroduces inference from an absence.

    Three independent things are checked, deliberately not sharing an
    implementation with the mechanism they audit:

      1. **No silence fingerprint.** The retired state wrote a recognisable
         evidence string. Nothing may write it now.
      2. **No unevidenced promotion.** Every term in a known state must appear
         in the terms the persona's replies actually produced evidence about,
         which the runner records from the Assessor's read of each reply rather
         than from the ledger.
      3. **`note_exposure` promotes nothing.** Asserted against a live scope,
         not read off the source, because a comment saying it returns `[]` is
         not the same as it returning `[]`.
    """

    violations: list[str]
    known_state_terms: int
    silent_persona_known_terms: list[str]
    note_exposure_promotions: int | None
    # The disengaged persona's `familiar` terms: how many, how many crossed the
    # band bar, and where its band ended up. Reported, and printed loudly when
    # the bar is crossed -- that is the silent-reader path the human chose
    # knowingly, so it must be visible rather than either hidden or failed.
    silent_reader_familiar: int = 0
    silent_reader_over_bar: int = 0
    silent_reader_band: str = ""
    familiar_traced: int = 0

    @property
    def clean(self) -> bool:
        return not self.violations


def silence_regression(
    results: list[PersonaResult], store: Store | None = None
) -> SilenceRegression:
    violations: list[str] = []
    known_terms = 0
    silent_known: list[str] = []
    familiar_traced = 0
    sr_familiar = sr_over = 0
    sr_band = ""
    bar = band_bar()

    for result in results:
        reported = set(result.reported_terms)
        glossed_read = glossed_in_read_exchange(result)
        reads = result.ledger_read_explanations
        for term, state in result.final_ledger.items():
            evidence = result.ledger_evidence.get(term, "")
            if SILENCE_EVIDENCE_MARKER in evidence.lower():
                violations.append(
                    f"{result.persona_id}: {term!r} carries silence-derived "
                    f"evidence {evidence!r} -- inference from an absence is back"
                )
            if state not in config.KNOWN_STATES:
                continue
            known_terms += 1
            # No known state without EITHER a persona turn behind it OR
            # (`familiar`, traceable to a read-or-skimmed exchange that glossed
            # the term). The second arm is the sanctioned path and nothing else
            # is.
            if term not in reported:
                if state == config.CONCEPT_FAMILIAR and term in glossed_read:
                    familiar_traced += 1
                else:
                    violations.append(
                        f"{result.persona_id}: {term!r} reached {state!r} but the "
                        "persona never said anything the system read as evidence "
                        "about it"
                        + (
                            " and no read exchange glossed it"
                            if state == config.CONCEPT_FAMILIAR
                            else ""
                        )
                    )
            if result.archetype == "disengaged":
                # A bouncer reads short briefings, so `familiar` is allowed;
                # nothing above it is, and every familiar term must trace.
                if state != config.CONCEPT_FAMILIAR:
                    silent_known.append(f"{result.persona_id}: {term} -> {state}")
                else:
                    sr_familiar += 1
                    if int(reads.get(term, 0)) >= bar:
                        sr_over += 1
                    if term not in glossed_read:
                        violations.append(
                            f"{result.persona_id}: {term!r} is familiar but no "
                            "read-or-skimmed exchange glossed it"
                        )
        if result.archetype == "disengaged":
            sr_band = result.rounds[-1].proficiency if result.rounds else ""

    if silent_known:
        violations.append(
            "the disengaged persona supplies no evidence at all, so its ledger "
            "must hold nothing above `familiar`; it holds "
            + ", ".join(sorted(silent_known))
        )

    # 3. The store's own behaviour, exercised rather than assumed.
    promotions: int | None = None
    if store is not None:
        try:
            probe = store.create_user("silence-regression-probe", kind="persona")
            scope = store.scope(probe.id)
            group = scope.create_group(
                name="silence regression probe",
                description="asserts note_exposure promotes nothing",
                poll_interval_minutes=360,
            )
            promotions = 0
            for _ in range(8):
                promotions += len(scope.note_exposure(group.id, ["probe term"]))
            if promotions:
                violations.append(
                    f"store.note_exposure promoted {promotions} term(s) after "
                    "repeated exposure -- silence is being treated as evidence"
                )
            states = {c.term: c.state for c in scope.ledger(group.id)}
            if states.get("probe term") != config.CONCEPT_UNKNOWN:
                violations.append(
                    "a term exposed 8x with no reply left `unknown`: it is "
                    f"{states.get('probe term')!r}"
                )
        except Exception as exc:
            violations.append(f"note_exposure probe failed: {type(exc).__name__}: {exc}")

    for attr in ("CONCEPT_ASSUMED", "EXPOSURES_BEFORE_ASSUMED"):
        if hasattr(config, attr):
            violations.append(
                f"config.{attr} exists again -- the deleted state is being "
                "reintroduced"
            )

    return SilenceRegression(
        violations=violations,
        known_state_terms=known_terms,
        silent_persona_known_terms=sorted(silent_known),
        note_exposure_promotions=promotions,
        silent_reader_familiar=sr_familiar,
        silent_reader_over_bar=sr_over,
        silent_reader_band=sr_band,
        familiar_traced=familiar_traced,
    )


# --- Regression: reading behaviour must never become a knowledge claim -----
#
# Same shape as `silence_regression`, and for the same reason. This is the
# THIRD attention signal in this project to try to become a comprehension
# claim:
#
#   1. `assumed` promoted a term after three unquestioned exposures. Measured
#      against persona ground truth it was anti-predictive at every bar tried
#      (lift -0.34 at one exposure, -1.00 at three and five). Deleted.
#   2. The live `concept_evidence` extractor credited `theo_silent` -- who
#      knows nothing and says nothing -- with understanding `yields` and
#      `trading down`, off topically-adjacent paraphrase that never used either
#      word. Fixed in the prompt; the gate stayed.
#   3. Reading behaviour, now. It is the most dangerous of the three because it
#      arrives as a number, and a number looks like a measurement.
#
# What is actually known about display metrics as comprehension proxies is not
# ambiguous. Apple Mail Privacy Protection inflates roughly half of all
# reported email opens by fetching images for users who never looked; the IAB
# had to invent "viewability" as a separate standard because "served" had
# stopped meaning anything; and around 59% of links shared on social media are
# never clicked by the person sharing them. Time-on-page and scroll depth are
# in the same family. They are good enough to close an item out of a "new"
# count and to report engagement, and they are not evidence that anybody
# understood anything.

# Words that would appear in a ledger evidence string if a reading signal had
# been allowed to write one. Kept as fingerprints for the same reason
# `SILENCE_EVIDENCE_MARKER` is: it turns "we designed it not to" into a check
# that stays true after the people who designed it have moved on.
READING_EVIDENCE_MARKERS = (
    "dwell",
    "scroll",
    "read quality",
    "time on page",
    "time-on-page",
    "skimmed",
    "studied it",
    "read it thoroughly",
    "read it carefully",
    "attention",
)


@dataclass
class ReadingRegression:
    """A hard gate that reading behaviour moves no concept, in either mode.

    Four independent checks, deliberately not sharing an implementation with
    the mechanism they audit:

      1. **No reading fingerprint in the ledger.** No concept's evidence string
         may mention dwell, scroll, or how attentively anything was read.
      2. **No promotion out of a silent thread.** Every term in a known state
         must appear in the terms the persona's own turns produced evidence
         about, which the runner records from the system's read of the thread
         rather than from the ledger. A term that got there off a thread with
         no user turns has been promoted on reading alone.
      3. **The store, exercised directly.** A live scope, a term, a maximal
         reading -- five minutes and a full scroll, repeatedly -- and the
         concept's state must be byte-identical afterwards. A comment saying
         `record_reading` does not touch concepts is not the same as it not
         touching them.
      4. **No reading field in any judgment context.** Scanned in the log. A
         model told how long someone looked at a briefing, and then asked what
         they understood, will answer differently and more confidently, and it
         will be wrong in the exact direction the product exists to avoid.
    """

    violations: list[str]
    silent_threads: int
    threads_checked: int
    probe_state_before: str | None
    probe_state_after: str | None
    judgment_rows_scanned: int
    # The close_thread probes (the `familiar` contract, exercised directly).
    close_probe_notes: list[str] = field(default_factory=list)
    # Personas that read and never spoke: "<id>: N familiar, M at the bar".
    silent_readers: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.violations


def reading_regression(
    results: list[PersonaResult],
    store: Store | None = None,
    run_id: str | None = None,
    contract: Any | None = None,
) -> ReadingRegression:
    violations: list[str] = []
    silent_threads = 0
    threads_checked = 0
    silent_readers: list[str] = []
    bar = band_bar()

    for result in results:
        reported = set(result.reported_terms)
        glossed_read = glossed_in_read_exchange(result)
        total_user_turns = sum(t.user_turn_count for t in result.threads)
        if result.threads and total_user_turns == 0:
            fam = [
                t for t, st in result.final_ledger.items()
                if st == config.CONCEPT_FAMILIAR
            ]
            over = [
                t for t in fam
                if int(result.ledger_read_explanations.get(t, 0)) >= bar
            ]
            silent_readers.append(
                f"{result.persona_id}: read {len(result.threads)} briefings, said "
                f"nothing; {len(fam)} term(s) reached familiar, {len(over)} crossed "
                f"read_explanations >= {bar}"
                + (f" ({', '.join(sorted(over))})" if over else "")
            )
        for thread in result.threads:
            threads_checked += 1
            if thread.user_turn_count == 0:
                silent_threads += 1
                # A thread nobody spoke in may produce no evidence whatsoever.
                # Reading it -- however thoroughly -- is not speaking.
                for bucket, label in (
                    (thread.understood, "understood"),
                    (thread.asked_about, "asked_about"),
                ):
                    if bucket:
                        violations.append(
                            f"{result.persona_id} r{thread.round_index}: nobody said "
                            f"anything in this thread, yet it reported "
                            f"{label}={list(bucket)}. The only user signal it "
                            f"carried was a {thread.reading.system_quality if thread.reading else '?'} "
                            "read of the briefing."
                        )

        for term, state in result.final_ledger.items():
            evidence = (result.ledger_evidence.get(term) or "").lower()
            if SANCTIONED_FAMILIAR_EVIDENCE.match(evidence.strip()):
                # The one string a reading is allowed to write, and only onto
                # `familiar` (or `explained`, which may sit on top of it without
                # rewriting the evidence). On `provisional` / `confirmed` it
                # means a reading was credited as a use.
                if state in (config.CONCEPT_PROVISIONAL, config.CONCEPT_CONFIRMED):
                    violations.append(
                        f"{result.persona_id}: {term!r} is {state!r} on the evidence "
                        f"{evidence!r} -- a reading has been credited as a use"
                    )
                elif state == config.CONCEPT_FAMILIAR and term not in glossed_read:
                    violations.append(
                        f"{result.persona_id}: {term!r} is familiar on {evidence!r} "
                        "but no read-or-skimmed exchange of theirs glossed it"
                    )
            else:
                for marker in READING_EVIDENCE_MARKERS + ("dwell_ms", " ms",):
                    if marker in evidence:
                        violations.append(
                            f"{result.persona_id}: {term!r} carries reading-derived "
                            f"evidence {evidence!r} -- attention is being recorded as "
                            "comprehension"
                        )
                        break
            if state in config.KNOWN_STATES and term not in reported:
                if not (state == config.CONCEPT_FAMILIAR and term in glossed_read):
                    violations.append(
                        f"{result.persona_id}: {term!r} reached {state!r} with no turn "
                        "from the persona behind it"
                    )

    # 3. The store's own behaviour, exercised rather than assumed.
    before = after = None
    if store is not None:
        try:
            probe = store.create_user("reading-regression-probe", kind="persona")
            scope = store.scope(probe.id)
            group = scope.create_group(
                name="reading regression probe",
                description="asserts record_reading moves no concept",
                poll_interval_minutes=360,
            )
            scope.note_exposure(group.id, ["probe term"])
            before = scope.concept_state(group.id, "probe term")

            exchange_id = scope.open_exchange(
                group.id, "probe term is a thing that happened.", topic="probe"
            )
            for _ in range(8):
                scope.record_reading(
                    exchange_id,
                    dwell_ms=300_000,
                    scroll_fraction=1.0,
                    source="simulated",
                )
            after = scope.concept_state(group.id, "probe term")
            if before != after:
                violations.append(
                    f"eight maximal readings moved 'probe term' from {before!r} to "
                    f"{after!r} -- record_reading is writing to the ledger"
                )
            if after in config.KNOWN_STATES:
                violations.append(
                    f"a term nobody ever mentioned is {after!r} after being read "
                    "attentively eight times"
                )
        except Exception as exc:
            violations.append(
                f"record_reading probe failed: {type(exc).__name__}: {exc}"
            )

    # 3b. `close_thread`, exercised directly against the `familiar` contract.
    #
    #   P2  closing a READ thread whose briefing glossed nothing moves nothing.
    #   P3  closing a SKIPPED thread that glossed X moves nothing.
    #   P4  closing a READ thread that glossed X moves ONLY X, to `familiar`
    #       with read_explanations == 1 -- never band-eligible from one
    #       exposure, and the band stays `beginner`.
    #
    # Needs the real Assessor (the contract's `close_thread`), so it runs only
    # when the caller hands one over; the note says so when it did not.
    close_notes: list[str] = []
    if store is not None and contract is not None:
        try:
            probe2 = store.create_user("familiar-contract-probe", kind="persona")
            scope2 = store.scope(probe2.id)
            group2 = scope2.create_group(
                name="familiar contract probe",
                description="asserts close_thread moves only what it may",
                poll_interval_minutes=360,
            )

            def _states() -> dict[str, tuple[str, int]]:
                return {
                    c.term: (c.state, int(getattr(c, "read_explanations", 0) or 0))
                    for c in scope2.ledger(group2.id)
                }

            # P2 -- read, glossed nothing.
            eid = scope2.open_exchange(
                group2.id, "probe alpha is a thing that happened.", topic="probe",
                explained_terms=[],
            )
            scope2.note_exposure(group2.id, ["probe alpha"])
            contract.record_reading(
                scope2, eid, dwell_ms=120_000, scroll_fraction=1.0, source="simulated"
            )
            contract.close_thread(scope2, group2, eid)
            st = _states()
            moved = {t: v for t, v in st.items() if v[0] != config.CONCEPT_UNKNOWN}
            if moved:
                violations.append(
                    f"P2: closing a read thread that glossed nothing moved {moved}"
                )
            else:
                close_notes.append("P2 read thread, no gloss: nothing moved")

            # P3 -- skipped, glossed 'probe beta'.
            eid = scope2.open_exchange(
                group2.id, "probe beta is a thing that happened.", topic="probe",
                explained_terms=["probe beta"],
            )
            contract.record_reading(
                scope2, eid, dwell_ms=0, scroll_fraction=0.0, source="simulated"
            )
            quality = str(contract.stored_reading(scope2, eid).get("read_quality") or "")
            contract.close_thread(scope2, group2, eid)
            st = _states()
            beta = st.get("probe beta", (config.CONCEPT_UNKNOWN, 0))
            if quality != "skipped":
                close_notes.append(
                    f"P3 could not produce a 'skipped' reading (store said {quality!r}); "
                    "assertion not run"
                )
            elif beta[0] != config.CONCEPT_UNKNOWN or beta[1] != 0:
                violations.append(
                    f"P3: closing a SKIPPED thread that glossed 'probe beta' left it "
                    f"{beta[0]!r} with read_explanations={beta[1]}"
                )
            else:
                close_notes.append("P3 skipped thread, glossed X: X unmoved")

            # P4 -- read, glossed 'probe gamma', mentioned 'probe delta'.
            eid = scope2.open_exchange(
                group2.id,
                "probe gamma and probe delta both happened.",
                topic="probe",
                explained_terms=["probe gamma"],
            )
            scope2.note_exposure(group2.id, ["probe gamma", "probe delta"])
            contract.record_reading(
                scope2, eid, dwell_ms=120_000, scroll_fraction=1.0, source="simulated"
            )
            band_before = scope2.proficiency(group2.id)
            contract.close_thread(scope2, group2, eid)
            st = _states()
            gamma = st.get("probe gamma", (config.CONCEPT_UNKNOWN, 0))
            others = {
                t: v for t, v in st.items()
                if t != "probe gamma" and v[0] != config.CONCEPT_UNKNOWN
            }
            band_after = scope2.proficiency(group2.id)
            if gamma != (config.CONCEPT_FAMILIAR, 1):
                violations.append(
                    "P4: closing a READ thread that glossed 'probe gamma' should "
                    f"leave it ('familiar', 1); it is {gamma}"
                )
            if others:
                violations.append(
                    f"P4: closing a read thread that glossed only 'probe gamma' also moved {others}"
                )
            if band_after != band_before or band_after != "beginner":
                violations.append(
                    f"P4: one read explanation moved the band {band_before!r} -> "
                    f"{band_after!r}; it must stay 'beginner'"
                )
            if not any(v.startswith("P4") for v in violations):
                close_notes.append(
                    "P4 read thread, glossed X: only X moved, to familiar with "
                    f"read_explanations=1; band {band_after!r}"
                )
        except Exception as exc:
            violations.append(
                f"close_thread probe failed: {type(exc).__name__}: {exc}"
            )
    else:
        close_notes.append("close_thread probes not run (no contract handed to metrics)")

    # 4. No reading field in any judgment context.
    rows_scanned = 0
    if store is not None and run_id is not None:
        try:
            rows = store.judgments(run_id=run_id, limit=1_000_000)
            rows_scanned = len(rows)
            seen: set[str] = set()
            for row in rows:
                blob = row["input_json"] or ""
                for field_name in READING_FIELDS:
                    if f'"{field_name}"' in blob:
                        seen.add(f"{row['judgment_point']} was shown {field_name!r}")
            violations.extend(
                f"reading behaviour in a judgment context: {entry}"
                for entry in sorted(seen)
            )
        except Exception as exc:
            violations.append(f"reading leak scan failed: {type(exc).__name__}: {exc}")

    return ReadingRegression(
        violations=violations,
        silent_threads=silent_threads,
        threads_checked=threads_checked,
        probe_state_before=before,
        probe_state_after=after,
        judgment_rows_scanned=rows_scanned,
        close_probe_notes=close_notes,
        silent_readers=silent_readers,
    )


# --- Did the store derive the same read quality the harness did? -----------


@dataclass
class ReadQualityAgreement:
    """Two derivations of the same four-band label, compared rather than assumed.

    The harness simulates `dwell_ms` and `scroll_fraction` and derives its own
    expected band; the store derives one from the same two numbers. They should
    agree, and the only way to know is to check. The two are separate pieces of
    code (`reading.reference_read_quality` and `store.read_quality`), which is
    what makes the agreement rate a check rather than a tautology.
    """

    n: int
    agreed: int
    rate: float | None
    distribution: dict[str, int]
    disagreements: list[str]


def read_quality_agreement(results: list[PersonaResult]) -> ReadQualityAgreement:
    n = agreed = 0
    dist: dict[str, int] = {}
    bad: list[str] = []
    for result in results:
        for thread in result.threads:
            if thread.reading is None:
                continue
            n += 1
            dist[thread.reading.system_quality or "(none)"] = (
                dist.get(thread.reading.system_quality or "(none)", 0) + 1
            )
            if thread.reading.agrees:
                agreed += 1
            elif len(bad) < 10:
                bad.append(
                    f"{result.persona_id} r{thread.round_index}: harness said "
                    f"{thread.reading.expected_quality!r}, store said "
                    f"{thread.reading.system_quality!r} "
                    f"({thread.reading.dwell_ms}ms, "
                    f"{thread.reading.scroll_fraction:.2f} scroll, "
                    f"{thread.reading.briefing_chars} chars)"
                )
    return ReadQualityAgreement(
        n=n,
        agreed=agreed,
        rate=_ratio(agreed, n),
        distribution=dict(sorted(dist.items())),
        disagreements=bad,
    )


# --- Does the question follow from what the persona knows? -----------------


@dataclass
class QuestionFaithfulness:
    """The harness's own honesty check, and a hard gate.

    This is the property that makes the redesign worth anything to the harness:
    a persona no longer answers a question somebody else wrote, it asks one of
    its own, and what it asks is supposed to be a readout of its concept sets.
    If it is not -- if a persona that has never met `derogation` asks a question
    that presupposes it, or one that holds `vintage` asks what a vintage is --
    then the most diagnostic thing this harness produces is a template bank
    talking to itself.

    Three checks, and all three gate:

      * **No bluffing.** A term the persona did not hold at the moment it spoke
        may not appear in that turn's `presupposes`.
      * **No feigned ignorance.** A term the persona did hold may not be asked
        about with a definition-class form -- unless the noise fixture told it
        to, which is a configured behaviour and is counted separately.
      * **The contingency holds.** Across the run, every question about a term
        the asker lacked used a lacking-class form, and every question that
        leaned only on held terms used a holding-class form.

    The contingency table is reported in full, because when it stops being
    perfect the interesting thing is *which* cell filled up.
    """

    questions: int
    lacking_form_when_lacking: int
    holding_form_when_holding: int
    violations: list[str]
    by_form: dict[str, int]
    by_tier: dict[int, int]
    # Questions the noise fixture deliberately made unfaithful, excluded from
    # the gate and counted here so they cannot be mistaken for a clean run.
    configured_noise_questions: int

    @property
    def clean(self) -> bool:
        return not self.violations


def question_faithfulness(results: list[PersonaResult]) -> QuestionFaithfulness:
    violations: list[str] = []
    questions = 0
    lacking_ok = 0
    holding_ok = 0
    noise_questions = 0
    by_form: dict[str, int] = {}
    by_tier: dict[int, int] = {}

    for result in results:
        for thread in result.threads:
            noisy_here = set(thread.asked_about_known)
            for turn in thread.questions:
                questions += 1
                by_form[turn.form] = by_form.get(turn.form, 0) + 1
                by_tier[turn.tier] = by_tier.get(turn.tier, 0) + 1
                held = set(turn.held_at_the_time)
                lacked = set(turn.lacked_at_the_time)

                bluffed = [t for t in turn.presupposes if t not in held]
                if bluffed:
                    violations.append(
                        f"{result.persona_id} r{thread.round_index}: asked "
                        f"{turn.text!r}, which leans on {sorted(bluffed)} -- terms "
                        "it did not hold at the time"
                    )

                asked_but_held = [
                    t for t in turn.asks_about if t in held and t not in noisy_here
                ]
                if asked_but_held and turn.form in LACKING_FORMS:
                    violations.append(
                        f"{result.persona_id} r{thread.round_index}: asked what "
                        f"{sorted(asked_but_held)} means, but held it"
                    )
                if set(turn.asks_about) & noisy_here:
                    noise_questions += 1

                if turn.asks_about and not (set(turn.asks_about) & noisy_here):
                    if turn.form in LACKING_FORMS and all(
                        t in lacked for t in turn.asks_about
                    ):
                        lacking_ok += 1
                    else:
                        violations.append(
                            f"{result.persona_id} r{thread.round_index}: question "
                            f"{turn.text!r} used form {turn.form!r} about "
                            f"{list(turn.asks_about)}, which it lacked"
                        )
                elif turn.presupposes:
                    if turn.form in HOLDING_FORMS:
                        holding_ok += 1
                    else:
                        violations.append(
                            f"{result.persona_id} r{thread.round_index}: question "
                            f"{turn.text!r} presupposed {list(turn.presupposes)} but "
                            f"used lacking-class form {turn.form!r}"
                        )

    return QuestionFaithfulness(
        questions=questions,
        lacking_form_when_lacking=lacking_ok,
        holding_form_when_holding=holding_ok,
        violations=violations,
        by_form=dict(sorted(by_form.items())),
        by_tier=dict(sorted(by_tier.items())),
        configured_noise_questions=noise_questions,
    )


# --- The system must not ask the user anything -----------------------------


@dataclass
class NoQuestionsToUser:
    """The redesign's first rule, gated rather than described.

    Every briefing in the live transcript that prompted this rebuild ended in a
    question the system had chosen, and every one of those questions
    presupposed knowledge the reader might not have. *"Does a harvest running
    this far ahead of normal sound like good news for the wine, or more like a
    warning sign to you?"* is unanswerable by the person the product exists to
    help, and the system was still, in effect, testing them.

    So: a briefing states the substance and stops. Detection is deliberately
    narrow -- a question mark in a sentence addressed to the reader -- because a
    briefing may legitimately quote a question someone else asked, and a gate
    that fired on that would be turned off within a week.
    """

    briefings: int
    offending: list[str]

    @property
    def clean(self) -> bool:
        return not self.offending


_ADDRESSES_READER = re.compile(
    r"\b(you|your|yours|thoughts|curious what|what do you|had you|does that "
    r"sound|how do you)\b",
    re.IGNORECASE,
)


def no_questions_to_user(results: list[PersonaResult]) -> NoQuestionsToUser:
    offending: list[str] = []
    briefings = 0
    for result in results:
        for thread in result.threads:
            briefings += 1
            for sentence in re.split(r"(?<=[.?!])\s+|\n+", thread.briefing or ""):
                sentence = sentence.strip()
                if not sentence.endswith("?"):
                    continue
                if _ADDRESSES_READER.search(sentence):
                    offending.append(
                        f"{result.persona_id} r{thread.round_index}: the briefing "
                        f"asks the reader {sentence!r}"
                    )
    return NoQuestionsToUser(briefings=briefings, offending=offending)


# --- The silence spiral: can the band be talked upward? --------------------


@dataclass
class BandContainment:
    persona_id: str
    bands_seen: list[str]
    highest: str
    briefings: int
    user_turns: int
    reads: dict[str, int]
    evidence_given: int
    passed: bool


_BAND_ORDER = (config.BEGINNER, config.DEVELOPING, config.CONVERSANT, config.FLUENT)


def band_containment(results: list[PersonaResult]) -> list[BandContainment]:
    """A user who never engages must never be promoted out of `beginner`.

    A structural property, not a judgment-quality measurement, so it is checked
    and gated in both modes.

    **The adversarial persona got sharper in the new model, and this is the
    metric that shows it.** It used to reply to every briefing with "ok" -- a
    turn that supplied no evidence, but a turn. It now says nothing at all,
    which is what someone like that actually does, and it is a strictly harder
    input: the system is left holding a briefing it wrote, a set of terms it
    chose to use, and a reading measurement saying the reader skimmed it. Every
    input to the band is now the system's own output plus an attention signal.
    If the band rises from that, the silence spiral is real and the failure
    compounds -- a higher band means fewer terms get explained, which means even
    less engagement.

    `reads` is reported alongside, and it is the point: this persona produced
    eight read-quality observations and zero turns, so a band that moves has
    moved on reading.
    """
    out: list[BandContainment] = []
    for result in results:
        if result.archetype != "disengaged":
            continue
        bands = [r.proficiency for r in result.rounds if r.proficiency]
        highest = config.BEGINNER
        for band in bands:
            if band in _BAND_ORDER and _BAND_ORDER.index(band) > _BAND_ORDER.index(
                highest
            ):
                highest = band
        evidence = sum(
            len(t.understood) + len(t.asked_about) + len(t.not_understood)
            for t in result.threads
        )
        reads: dict[str, int] = {}
        for thread in result.threads:
            if thread.reading is not None:
                key = thread.reading.system_quality or "(none)"
                reads[key] = reads.get(key, 0) + 1
        out.append(
            BandContainment(
                persona_id=result.persona_id,
                bands_seen=sorted(set(bands)),
                highest=highest,
                briefings=len(result.threads),
                user_turns=sum(t.user_turn_count for t in result.threads),
                reads=dict(sorted(reads.items())),
                evidence_given=evidence,
                passed=highest == config.BEGINNER,
            )
        )
    return out


# --- Cold start: the case that broke the old design ------------------------


@dataclass
class ColdStart:
    persona_id: str
    unknown_terms: int
    explained_terms: int
    explained_share: float | None
    claimed_without_explaining: list[str]
    first_briefing_explained: int
    first_briefing_terms: int
    briefed_before_asked: bool
    passed: bool
    # The disengaged persona is reported here but does not gate on
    # `claimed_without_explaining`. For it, a term the ledger claims it knows
    # is not a cold-start bug -- it is the exact phenomenon
    # `silence_regression` and `band_containment` exist to measure, and failing
    # it here as well would count one finding three times and make the build
    # permanently red for something already under measurement.
    gated: bool = True


def cold_start(results: list[PersonaResult]) -> list[ColdStart]:
    """Did the system brief a beginner, or interrogate one?

    Two things are checked, and they are different questions.

    1. Of the terms this person does not understand and has been shown, how
       many did the system actually gloss? A system that uses jargon at a
       beginner and waits to be asked has failed them.
    2. Did every thread put substance in front of them at all, i.e. is the
       briefing non-empty? Whether the system asked the user anything is a
       separate check, `no_questions_to_user`, which reads the prose itself.
    """
    out: list[ColdStart] = []
    for result in results:
        if result.archetype not in ("barely_informed", "disengaged"):
            continue
        unknown = set(result.initial_does_not_know)
        explained: set[str] = set()
        for thread in result.threads:
            explained |= set(thread.explained_terms)
        shown = (
            unknown
            & set().union(*(set(t.explained_terms) for t in result.threads), set())
            if result.threads
            else set()
        )
        # Terms they don't know that the system decided it already knew they
        # knew, without ever explaining them: the cold-start failure mode.
        # Scored over every known state now rather than over `assumed` alone.
        # When `assumed` existed it was the only state a beginner could be
        # wrongly credited with without saying anything; with it gone the way
        # to get here is a misread of a reply, which is a different bug with
        # the same consequence for the person -- jargon they cannot follow.
        # A term the persona does not know, which the ledger nonetheless calls
        # known -- EXCEPT via `explained`, which is reached only by the user
        # asking and us answering. That is a legitimate way to stop not knowing
        # something, and the persona's ground truth is a fixed starting state,
        # not a claim that they can never learn. Flagging it made the harness
        # mark the system wrong for successfully teaching someone.
        #
        # `provisional`/`confirmed` on an unknown term is still a real error:
        # it means a reply was misread as demonstrating a concept the persona
        # does not hold.
        #
        # The same correction has to be made once more, for the same reason.
        # Excluding `explained` is not enough: a persona who asked about a term
        # in round 1 genuinely knows it from round 2 on, and may then use it
        # correctly twice and reach `confirmed` on its own merits. Scoring
        # against the *starting* set marked that a cold-start failure -- the
        # live run flagged `sam_beginner`'s 'valuation' exactly this way, a
        # term Sam asked about, had explained, and then used correctly twice.
        # So the test is against what the persona knows at the END of the run,
        # which is what `final_truly_known` is for; the starting set only says
        # which terms are in scope for the cold-start question at all.
        # With the learning model the persona can FORGET a term it learned,
        # and the cold-start question is whether the system claimed a term it
        # never taught -- so the exclusion is "ever truly known during the
        # run", not "still known at the end".
        truly_now = set(result.ever_known) | set(result.final_truly_known)
        # `familiar` is by construction a term the system explained -- it can
        # only come from `exchanges.explained_terms` -- so it is excluded on
        # the same grounds as `explained`. The stronger claim is asserted
        # instead: every familiar term must appear in some exchange's
        # explained_terms, or the state was reached some other way.
        glossed = glossed_anywhere(result)
        claimed_bad = sorted(
            term
            for term, state in result.final_ledger.items()
            if state in config.KNOWN_STATES
            and state not in (config.CONCEPT_EXPLAINED, config.CONCEPT_FAMILIAR)
            and term in unknown
            and term not in truly_now
        )
        claimed_bad += sorted(
            f"{term} (familiar, never glossed)"
            for term, state in result.final_ledger.items()
            if state == config.CONCEPT_FAMILIAR and term not in glossed
        )
        first = result.threads[0] if result.threads else None
        briefed_first = all(bool(t.briefing.strip()) for t in result.threads)
        target = unknown & set(result.final_ledger)
        out.append(
            ColdStart(
                persona_id=result.persona_id,
                unknown_terms=len(target),
                explained_terms=len(explained & target),
                explained_share=_ratio(len(explained & target), len(target)),
                claimed_without_explaining=claimed_bad,
                first_briefing_explained=len(first.explained_terms) if first else 0,
                first_briefing_terms=len(shown),
                briefed_before_asked=briefed_first,
                passed=bool(result.threads) and briefed_first and not claimed_bad,
                gated=result.archetype == "barely_informed",
            )
        )
    return out


# --- Style fidelity --------------------------------------------------------


@dataclass
class StyleFidelity:
    persona_id: str
    archetype: str
    replies: int
    observed_median: float | None
    observed_short_share: float | None
    observed_long_share: float | None
    configured_median: float
    configured_short_share: float
    configured_long_share: float
    lowercase: bool
    long_rule_applies: bool
    configured_ok: bool


def style_fidelity(results: list[PersonaResult], personas: list[Any]) -> list[StyleFidelity]:
    """Is the persona's length distribution the one the study prescribes?

    Two figures, because they answer different questions and only one of them
    is trustworthy at this sample size:

    * `configured_*` -- 20,000 draws from the persona's own parameters. This is
      a property of the fixture and is the one that is gated: it says whether
      the persona *can* produce §8.2's shape.
    * `observed_*` -- the replies this run actually produced. Eight replies per
      persona cannot resolve a 20% tail, so this is reported for eyeballing and
      never gated. A persona that passes `configured` and looks wild in
      `observed` is small-sample noise; one that fails `configured` is a
      miscalibrated fixture.

    §8.2 is blunt that this is worth checking: "A persona whose length variance
    is too low is the most likely single failure of this harness."
    """
    by_id = {p.id: p for p in personas}
    out: list[StyleFidelity] = []
    for result in results:
        persona = by_id.get(result.persona_id)
        if persona is None:
            continue
        style = persona.style
        rng = random.Random(f"style-check:{persona.id}")
        draws = [
            target_words(rng, style.median_words, style.variance) for _ in range(20_000)
        ]
        cfg_short = sum(1 for d in draws if d < STYLE_SHORT_WORDS) / len(draws)
        cfg_long = sum(1 for d in draws if d > STYLE_LONG_WORDS) / len(draws)
        long_applies = ARCHETYPE_P90.get(result.archetype, 0) > STYLE_LONG_WORDS

        # Observed lengths are over the persona's own turns, questions
        # included. A question is a message the person typed, so it belongs in
        # the distribution -- but section 8.4 caps questions at 15 words, so a
        # persona that mostly asks will sit lower than one that mostly reacts.
        # That is a real property of the new interaction model rather than a
        # miscalibrated fixture, which is one more reason the gate stays on the
        # configured distribution and this column stays reported-only.
        lengths = [
            turn.words for t in result.threads for turn in t.turns if turn.speaker == "user"
        ]
        out.append(
            StyleFidelity(
                persona_id=result.persona_id,
                archetype=result.archetype,
                replies=len(lengths),
                observed_median=(
                    round(statistics.median(lengths), 1) if lengths else None
                ),
                observed_short_share=(
                    round(sum(1 for n in lengths if n < STYLE_SHORT_WORDS) / len(lengths), 3)
                    if lengths
                    else None
                ),
                observed_long_share=(
                    round(sum(1 for n in lengths if n > STYLE_LONG_WORDS) / len(lengths), 3)
                    if lengths
                    else None
                ),
                configured_median=round(statistics.median(draws), 1),
                configured_short_share=round(cfg_short, 3),
                configured_long_share=round(cfg_long, 3),
                lowercase=style.lowercase,
                long_rule_applies=long_applies,
                configured_ok=(
                    cfg_short >= STYLE_MIN_SHORT_SHARE
                    and (not long_applies or cfg_long >= STYLE_MIN_LONG_SHARE)
                ),
            )
        )
    return out


# --- Briefing engagement ---------------------------------------------------
#
# Judge a briefing by what the reply DID, not by whether it was displayed.
#
# Every other measure of briefing quality here is really a measure of the
# ledger. This one asks the question a reader would ask: did putting that in
# front of them actually land? It is built entirely from data the run already
# collects, so it costs nothing to compute and adds no calls.

# Replies that are a polite acknowledgement and nothing else. Matched whole,
# after stripping punctuation, so "ok" is a brush-off and "ok so does that mean
# the whole category is repriced" is not.
_BRUSH_OFF = frozenset(
    {
        "ok", "okay", "k", "huh", "sure", "right", "got it", "makes sense",
        "fair enough", "i see", "interesting", "wow", "nice", "cool", "yeah",
        "yep", "mm", "mmm", "hm", "hmm", "noted", "fine", "alright",
        "sounds good", "good to know", "thanks", "ta",
    }
)

# The ladder. Ordinal and strongest-wins: a thread that asks a question is
# engaged whether or not it was also substantive.
ENGAGEMENT_LEVELS = (
    "not read",
    "read, said nothing",
    "brush-off",
    "substantive",
    "asked something",
    "kept the thread going",
    "reused a term later",
)


@dataclass
class ThreadEngagement:
    """What one persona's briefings actually drew out of it.

    **The absolute numbers here measure the persona, and the deltas measure the
    briefing.** That distinction is the whole reason this metric was rebuilt.

    The previous version reported raw follow-up rates per persona and an
    aggregate across all five. The aggregate was read as a finding about
    briefing quality -- "only 17% ask a follow-up" -- and it was nothing of the
    kind: it was the mean of five configured `ask_back_rate` values, and it
    would have printed the same number against briefings of any quality
    whatsoever. A metric that cannot move in response to the thing it claims to
    measure is not a metric.

    So every rate is reported next to what this persona's own configuration
    predicts for the briefings this run actually produced -- `expected_*`,
    computed per briefing from the configured rates and from whether that
    briefing contained a term the persona lacked. The residual is the only part
    that can be about the briefing, and it is the column to read.

    Reading is folded in for the same reason. `read_score` is the mean band
    this reader gave the briefings -- 0 skipped, 1 skimmed, 2 read, 3 studied --
    and the expectation is what the same reading profile would have produced
    against briefings pitched at the persona's own comfortable length. A system
    that writes long gets a negative residual there, and that residual is
    entirely the system's doing.
    """

    persona_id: str
    briefings: int
    # Observed
    threads_with_a_turn: int
    threads_with_a_question: int
    multi_turn_threads: int
    total_user_turns: int
    substantive: int
    reused_term_later: int
    read_score_total: float
    mean_level: float | None
    per_briefing: list[int]
    read_bands: dict[str, int]
    # Configured expectation, over these same briefings
    expected_threads_with_a_turn: float
    expected_threads_with_a_question: float
    expected_user_turns: float
    expected_read_score_total: float

    @property
    def turn_rate(self) -> float | None:
        return _ratio(self.threads_with_a_turn, self.briefings)

    @property
    def question_rate(self) -> float | None:
        return _ratio(self.threads_with_a_question, self.briefings)

    @property
    def turns_per_thread(self) -> float | None:
        return (
            round(self.total_user_turns / self.briefings, 3) if self.briefings else None
        )

    @property
    def read_score(self) -> float | None:
        """Mean read band, 0 skipped / 1 skimmed / 2 read / 3 studied."""
        return (
            round(self.read_score_total / self.briefings, 3)
            if self.briefings
            else None
        )

    @property
    def reuse_rate(self) -> float | None:
        return _ratio(self.reused_term_later, self.briefings)

    def _delta(self, observed: float, expected: float) -> float | None:
        if not self.briefings:
            return None
        return round((observed - expected) / self.briefings, 3)

    @property
    def turn_rate_delta(self) -> float | None:
        return self._delta(self.threads_with_a_turn, self.expected_threads_with_a_turn)

    @property
    def question_rate_delta(self) -> float | None:
        return self._delta(
            self.threads_with_a_question, self.expected_threads_with_a_question
        )

    @property
    def turns_per_thread_delta(self) -> float | None:
        return self._delta(self.total_user_turns, self.expected_user_turns)

    @property
    def read_score_delta(self) -> float | None:
        return self._delta(self.read_score_total, self.expected_read_score_total)


def _is_brush_off(text: str, words: int) -> bool:
    stripped = "".join(c for c in text.lower() if c.isalnum() or c.isspace()).strip()
    if stripped in _BRUSH_OFF:
        return True
    # A very short message carrying no specifics at all. Four words is the line
    # `reply_style_profile.md` puts on a bare reaction; a short message with a
    # digit in it is reacting to something specific and is not a brush-off.
    return words <= 4 and not any(c.isdigit() for c in text)


# The four bands are ordinal, so they get an index and the metric compares
# means. A binary "read or better" was tried first and is wrong here: a skimmer
# cannot reach `read` at ANY briefing length, because its dwell-to-expected
# ratio is a constant of its own profile -- so both the observed share and the
# expected share are structurally zero and the residual can never move. That is
# a metric that cannot respond to the thing it claims to measure, which is the
# exact fault this whole section was rebuilt to remove. The mean band index
# moves: a skimmer handed a long briefing slides from `skimmed` to `skipped`,
# and that slide is entirely the system's doing.
READ_BAND_SCORE = {"skipped": 0.0, "skimmed": 1.0, "read": 2.0, "studied": 3.0}


def _expected_read_score(persona: Any) -> float:
    """This reader's mean band index at their own comfortable length.

    `patience_chars` is the counterfactual length -- the point at which this
    profile stops bailing -- so the expectation is "what this reader does with a
    briefing pitched at their own tolerance", and the residual is what the
    system's actual length cost.

    Computed by simulation rather than derived, because `read_quality` has
    thresholds on two interacting inputs and a closed form for the joint
    distribution would be a second implementation to keep in sync with the
    first. 4,000 draws from a fixed seed, so it is deterministic and
    contributes nothing to run-to-run variance.
    """
    profile = persona.reading
    rng = random.Random(f"read-expectation:{persona.id}")
    chars = profile.patience_chars
    total = 0.0
    trials = 4_000
    for _ in range(trials):
        reading = profile.read(chars, rng)
        total += READ_BAND_SCORE.get(reading.expected_quality, 0.0)
    return total / trials


def thread_engagement(
    results: list[PersonaResult], personas: list[Any]
) -> list[ThreadEngagement]:
    """Per briefing: was it read, did it draw anything out, and how much?

    Six signals, weakest to strongest:

    1. **Read at all.** New, and the floor is now genuinely below "replied":
       a briefing can be skipped without a single word being typed, and until
       this model there was no way to see the difference between skipped and
       read-then-ignored.
    2. **Read but silent.** The most common outcome for a real briefing, and a
       normal one. Not a failure.
    3. **Substantive rather than a brush-off.** "makes sense" is a turn and is
       not engagement.
    4. **Asked something.** The strongest single-turn signal, because you only
       ask about what you are actually engaging with, and it costs the reader
       effort.
    5. **Kept the thread going.** Two or more user turns. Stronger than one
       question, because the second one is a response to the answer rather than
       to the briefing.
    6. **Used a term from that briefing correctly in a LATER thread.** The only
       signal that survives the mood objection, because it is separated from the
       briefing by at least one whole exchange. Matched against the raw text of
       later turns rather than against the system's own `understood` read: the
       extractor is the thing this is supposed to be independent of.
    """
    by_id = {p.id: p for p in personas}
    out: list[ThreadEngagement] = []

    for result in results:
        persona = by_id.get(result.persona_id)
        levels: list[int] = []
        read_bands: dict[str, int] = {}
        exp_turn = exp_question = exp_turns = 0.0
        expected_band = _expected_read_score(persona) if persona is not None else 0.0

        for i, thread in enumerate(result.threads):
            band = thread.reading.system_quality if thread.reading else ""
            read_bands[band or "(none)"] = read_bands.get(band or "(none)", 0) + 1

            if persona is not None:
                style = persona.style
                # A skipped briefing opens a thread at SKIP_FIRST_TURN_FACTOR
                # of the configured rate (responders.py): the expectation has
                # to say so, or every skip would read as under-engagement.
                act = thread.reading.intent_act if thread.reading else ""
                exp_turn += p_first_turn_after(style, thread.unknown_term_present, act)
                exp_question += p_first_turn_is_question(
                    style, thread.unknown_term_present
                ) * (SKIP_FIRST_TURN_FACTOR if act == "skip" else 1.0)
                exp_turns += expected_turns_after(style, thread.unknown_term_present, act)

            user_turns = thread.user_turns
            if not user_turns:
                levels.append(0 if band == "skipped" else 1)
                continue

            text = " ".join(t.text for t in user_turns)
            words = sum(t.words for t in user_turns)
            level = 2 if _is_brush_off(text, words) else 3
            if thread.questions or thread.asked_about:
                level = 4
            if thread.user_turn_count >= 2:
                level = 5
            later = " ".join(
                turn.text
                for later_thread in result.threads[i + 1 :]
                for turn in later_thread.user_turns
            )
            if later and any(
                term_pattern(t).search(later) for t in thread.explained_terms
            ):
                level = 6
            levels.append(level)

        n = len(result.threads)
        out.append(
            ThreadEngagement(
                persona_id=result.persona_id,
                briefings=n,
                threads_with_a_turn=sum(1 for t in result.threads if t.user_turn_count),
                threads_with_a_question=sum(1 for t in result.threads if t.questions),
                multi_turn_threads=sum(
                    1 for t in result.threads if t.user_turn_count >= 2
                ),
                total_user_turns=sum(t.user_turn_count for t in result.threads),
                substantive=sum(1 for v in levels if v >= 3),
                reused_term_later=sum(1 for v in levels if v >= 6),
                read_score_total=sum(
                    READ_BAND_SCORE.get(t.reading.system_quality, 0.0)
                    for t in result.threads
                    if t.reading
                ),
                mean_level=round(sum(levels) / len(levels), 2) if levels else None,
                per_briefing=levels,
                read_bands=dict(sorted(read_bands.items())),
                expected_threads_with_a_turn=round(exp_turn, 3),
                expected_threads_with_a_question=round(exp_question, 3),
                expected_user_turns=round(exp_turns, 3),
                expected_read_score_total=round(expected_band * n, 3),
            )
        )
    return out


# --- Noise report ----------------------------------------------------------


@dataclass
class NoiseReport:
    persona_id: str
    silent_terms: list[str]
    silence_promoted: list[str]
    asked_about_known: list[str]
    interactions_spent_on_known: int


def noise_report(results: list[PersonaResult]) -> list[NoiseReport]:
    """What the noisy persona's two behaviours actually cost.

    Its silence was aimed at `assumed`, and `assumed` is gone, so
    `silence_promoted` should now be empty by construction. It is kept and
    reported rather than dropped: it is the persona-level counterpart to
    `silence_regression`, and an empty list here every run is the evidence that
    the deletion held. The second behaviour -- asking about a term it already
    knows -- still costs a real interaction and is still worth counting.
    """
    out: list[NoiseReport] = []
    for result in results:
        if not result.is_noisy:
            continue
        silent: list[str] = []
        asked_known: list[str] = []
        for thread in result.threads:
            silent.extend(thread.stayed_silent_on)
            asked_known.extend(thread.asked_about_known)
        truly = set(result.final_truly_known)
        landed = sorted(
            {
                term
                for term in silent
                if result.final_ledger.get(term) in config.KNOWN_STATES
                and term not in truly
            }
        )
        out.append(
            NoiseReport(
                persona_id=result.persona_id,
                silent_terms=sorted(set(silent)),
                silence_promoted=landed,
                asked_about_known=sorted(set(asked_known)),
                interactions_spent_on_known=len(asked_known),
            )
        )
    return out


# --- Plumbing --------------------------------------------------------------


@dataclass
class Isolation:
    checks_run: int
    violations: list[str]

    @property
    def clean(self) -> bool:
        return not self.violations


def check_isolation(store: Store, results: list[PersonaResult]) -> Isolation:
    """Every persona's rows must be invisible from every other persona's scope.

    Exercised through `UserScope` only -- the same API the components use. A
    check that reached around it with raw SQL would be testing the schema, not
    the guarantee.
    """
    violations: list[str] = []
    checks = 0
    scopes = {r.persona_id: store.scope(r.user_id) for r in results}

    for viewer in results:
        scope = scopes[viewer.persona_id]
        own_groups = {g.id for g in scope.list_groups()}
        checks += 1
        if own_groups != {viewer.group_id}:
            violations.append(
                f"{viewer.persona_id}: list_groups() returned {sorted(own_groups)}, "
                f"expected only {viewer.group_id}"
            )

        for other in results:
            if other.persona_id == viewer.persona_id:
                continue
            checks += 5
            if scope.get_group(other.group_id) is not None:
                violations.append(
                    f"{viewer.persona_id} can read {other.persona_id}'s group"
                )
            if scope.find_group_by_name(other.group_name) is not None:
                violations.append(
                    f"{viewer.persona_id} can find {other.persona_id}'s group by name"
                )
            if scope.ledger(other.group_id):
                violations.append(
                    f"{viewer.persona_id} can read {other.persona_id}'s concept ledger"
                )
            if scope.exchange_history(other.group_id):
                violations.append(
                    f"{viewer.persona_id} can read {other.persona_id}'s exchanges"
                )
            if scope.pending_events(other.group_id):
                violations.append(
                    f"{viewer.persona_id} can read {other.persona_id}'s pending events"
                )

    return Isolation(checks_run=checks, violations=violations)


@dataclass
class LeakCheck:
    rows_scanned: int
    leaks: list[str]

    @property
    def clean(self) -> bool:
        return not self.leaks


def check_answer_key_leakage(store: Store, run_id: str) -> LeakCheck:
    """No judgment call may have been shown the fixture's answer key.

    `judgment_log.input_json` is byte-for-byte what the call was given. Under
    `--live` that string is in the model's context window, so a leak here does
    not merely dirty the log -- it invalidates every live number in the report.
    The two that matter most are `knows` and `does_not_know`: a model handed
    those is not being measured, it is being told.
    """
    rows = store.judgments(run_id=run_id, limit=1_000_000)
    leaks: list[str] = []
    for row in rows:
        blob = row["input_json"] or ""
        for field_name in ANSWER_KEY_FIELDS:
            if f'"{field_name}"' in blob:
                leaks.append(
                    f"{row['judgment_point']} (log {row['id']}) contains {field_name!r}"
                )
    return LeakCheck(rows_scanned=len(rows), leaks=sorted(set(leaks)))


@dataclass
class PointCost:
    point: str
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    mean_latency_ms: float
    max_latency_ms: int


def cost_and_latency(store: Store, run_id: str) -> list[PointCost]:
    """Per-judgment-point rollup, read from this run's log rows only."""
    rows = store.judgments(run_id=run_id, limit=1_000_000)
    buckets: dict[str, list[Any]] = {}
    for row in rows:
        buckets.setdefault(row["judgment_point"], []).append(row)

    out: list[PointCost] = []
    for point in sorted(buckets):
        group = buckets[point]
        latencies = [r["latency_ms"] or 0 for r in group]
        out.append(
            PointCost(
                point=point,
                calls=len(group),
                errors=sum(1 for r in group if r["error"]),
                input_tokens=sum(r["input_tokens"] or 0 for r in group),
                output_tokens=sum(r["output_tokens"] or 0 for r in group),
                mean_latency_ms=(
                    round(statistics.fmean(latencies), 1) if latencies else 0.0
                ),
                max_latency_ms=max(latencies) if latencies else 0,
            )
        )
    return out


# --- Judgment-quality metrics (live only) ----------------------------------


@dataclass
class MaterialityQuality:
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    precision: float | None
    recall: float | None
    f1: float | None
    by_significance: dict[str, dict[str, Any]]
    false_positive_examples: list[str]
    false_negative_examples: list[str]


@dataclass
class EvidenceQuality:
    """Did the system read the reply the way the persona meant it?

    Scored against the responder's own `TurnIntent`s, which are the persona's
    intent and therefore answer key. Refused offline for exactly that reason.
    """

    mentions_total: int
    mentions_caught: int
    mention_recall: float | None
    asks_total: int
    asks_caught: int
    ask_recall: float | None
    spurious_understood: int
    # New hazard, and specific to the new interaction model.
    #
    # Under the old model most of what a user wrote was declarative, so "a term
    # in a question is a gap" was a cheap and usually-right rule. Now most user
    # text IS a question, and a question presupposes terms as often as it asks
    # about them: *"is the enterprise value number the same story as the ARR
    # one?"* asks about neither -- it demonstrates both. A rule that reads every
    # term inside a question mark as a gap will mark them `explained`, and the
    # system will then re-explain to somebody who has just proved they do not
    # need it, which is the specific insult the redesign was meant to end.
    #
    # This counts terms the persona held and leaned on, which came back as
    # `asked_about`. Refused offline like the rest of this block: the persona's
    # intent is the answer key.
    presupposed_read_as_asked: int
    presupposed_total: int
    examples: list[str]

    @property
    def presupposition_confusion(self) -> float | None:
        return _ratio(self.presupposed_read_as_asked, self.presupposed_total)


@dataclass
class JudgmentQuality:
    materiality: MaterialityQuality
    evidence: EvidenceQuality


def _materiality(results: list[PersonaResult]) -> MaterialityQuality:
    tp = fp = tn = fn = 0
    fp_examples: list[str] = []
    fn_examples: list[str] = []
    by_sig: dict[str, dict[str, Any]] = {}
    for result in results:
        for obs in result.materiality:
            bucket = by_sig.setdefault(
                obs.significance or "unclassified",
                {"n": 0, "judged_material": 0, "should_be_material": 0, "agreed": 0},
            )
            bucket["n"] += 1
            bucket["judged_material"] += int(obs.judged_material)
            bucket["should_be_material"] += int(obs.should_be_material)
            bucket["agreed"] += int(obs.judged_material == obs.should_be_material)
            if obs.judged_material and obs.should_be_material:
                tp += 1
            elif obs.judged_material and not obs.should_be_material:
                fp += 1
                fp_examples.append(f"{result.persona_id}: {obs.headline}")
            elif not obs.judged_material and obs.should_be_material:
                fn += 1
                fn_examples.append(f"{result.persona_id}: {obs.headline}")
            else:
                tn += 1
    for bucket in by_sig.values():
        bucket["agreement"] = _ratio(bucket["agreed"], bucket["n"])
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    return MaterialityQuality(
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
        by_significance=dict(sorted(by_sig.items())),
        false_positive_examples=sorted(fp_examples)[:10],
        false_negative_examples=sorted(fn_examples)[:10],
    )


def _evidence(results: list[PersonaResult]) -> EvidenceQuality:
    mentions = caught = asks = asks_caught = spurious = 0
    presupposed = presupposed_confused = 0
    examples: list[str] = []
    for result in results:
        for thread in result.threads:
            for term in thread.intent_presupposed:
                if term in thread.intent_asked:
                    continue
                presupposed += 1
                if term in thread.asked_about:
                    presupposed_confused += 1
                    if len(examples) < 14:
                        examples.append(
                            f"{result.persona_id} r{thread.round_index}: leaned on "
                            f"{term!r} to frame a question and it came back as a "
                            "gap to be explained"
                        )
            for term in thread.intent_mentioned:
                mentions += 1
                if term in thread.understood:
                    caught += 1
                elif len(examples) < 10:
                    examples.append(
                        f"{result.persona_id} r{thread.round_index}: used "
                        f"{term!r} correctly, not read as understood"
                    )
            for term in thread.intent_asked:
                asks += 1
                if term in thread.asked_about:
                    asks_caught += 1
                elif len(examples) < 10:
                    examples.append(
                        f"{result.persona_id} r{thread.round_index}: asked "
                        f"about {term!r}, not read as an ask"
                    )
            spurious += len(
                set(thread.understood)
                - set(thread.intent_mentioned)
                - set(thread.intent_presupposed)
                - set(result.final_truly_known)
            )
    return EvidenceQuality(
        mentions_total=mentions,
        mentions_caught=caught,
        mention_recall=_ratio(caught, mentions),
        asks_total=asks,
        asks_caught=asks_caught,
        ask_recall=_ratio(asks_caught, asks),
        spurious_understood=spurious,
        presupposed_read_as_asked=presupposed_confused,
        presupposed_total=presupposed,
        examples=examples,
    )


def _judgment_quality(results: list[PersonaResult]) -> JudgmentQuality:
    return JudgmentQuality(
        materiality=_materiality(results), evidence=_evidence(results)
    )


# --- The report ------------------------------------------------------------


@dataclass
class Report:
    live: bool
    run_id: str
    suite: str
    ledger: list[LedgerAgreement]
    per_state: PerStateReport
    provisional: ProvisionalSignal
    exposure: ExposureReport
    silence: SilenceRegression
    reading: ReadingRegression
    read_quality: ReadQualityAgreement
    faithfulness: QuestionFaithfulness
    no_questions: NoQuestionsToUser
    band: list[BandContainment]
    cold_start: list[ColdStart]
    engagement: list[ThreadEngagement]
    style: list[StyleFidelity]
    noise: list[NoiseReport]
    isolation: Isolation
    leakage: LeakCheck
    costs: list[PointCost]
    unexpected_judgment_calls: list[str]
    contract_notes: list[str]
    round_errors: list[str]
    quality: JudgmentQuality | _NotMeasured
    interrupt: dict[str, Any] = field(default_factory=dict)
    personas_run: int = 0
    # `quantity_phrasing_check`: a template that talks about a figure being
    # "higher than normal" applied to a concept that is not a number. Gated,
    # because the harness feeds these sentences to the extractor as if they
    # were realistic input.
    quantity_offences: list[str] = field(default_factory=list)
    # The persona memory model and the post-session probe (harness/LEARNING.md).
    learning: list[PersonaLearning] = field(default_factory=list)
    # Reading intent: what each skip meant vs what the system decided it meant.
    reading_intent: list[PersonaReadingIntent] = field(default_factory=list)
    # Subdomains: the system's labels and familiarity vs the fixture's shape,
    # and whether briefing depth follows it. Reported, not gated.
    subdomains: list[PersonaSubdomain] = field(default_factory=list)
    # Which answer key `ledger` was scored against, and the same agreement
    # under the other key, reported alongside for one run's comparison.
    ledger_truth: str = "dynamic"
    ledger_comparison: list[LedgerAgreement] = field(default_factory=list)
    # The run's non-volatile header: fixture dir, persona ids, rounds cap,
    # horizon settings and harness model routing. `harness.gate` pairs two
    # artifacts only when these agree. Empty when the caller supplied none
    # (older callers, `verify_offline`).
    run_settings: dict[str, Any] = field(default_factory=dict)
    # Token usage of the harness-side calls (persona voice, probe, grader).
    # Volatile: it is cost, not behaviour.
    harness_usage: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Pass/fail is decided on the ledger metric plus plumbing, both modes.

        Live judgment-quality numbers are reported, not gated: no thresholds
        for them have been agreed with the human, and inventing one here would
        turn an unreviewed guess into a build-breaking rule. The ledger
        thresholds *are* agreed -- they are in `config` -- so those gate.

        Three of the gates are structural rather than judgment-quality, so
        they hold in both modes:

        * `reading` -- attention may not move a concept.
        * `no_questions` -- the system may not ask the user anything.
        * `faithfulness` -- the harness's own honesty check. A persona whose
          questions do not follow from its concept sets makes every number
          downstream of it meaningless, so it fails the build rather than
          appearing as a footnote.
        """
        interrupt_ok = not self.interrupt.get("structural_failures")
        return (
            self.personas_run > 0
            and all(a.passed for a in self.ledger if a.gated)
            and not self.quantity_offences
            # Structural, so gated in both modes: a persona that supplies no
            # evidence at all must not be promoted out of `beginner`, and no
            # term may reach a known state without the person having said
            # something the system read as evidence.
            and all(b.passed for b in self.band)
            and self.silence.clean
            and self.reading.clean
            and self.no_questions.clean
            and self.faithfulness.clean
            and all(c.passed for c in self.cold_start if c.gated)
            and all(s.configured_ok for s in self.style)
            and self.isolation.clean
            and self.leakage.clean
            and not self.unexpected_judgment_calls
            and not self.round_errors
            and interrupt_ok
        )


def compute(
    results: list[PersonaResult],
    personas: list[Any],
    *,
    store: Store,
    run_id: str,
    live: bool,
    unexpected_calls: list[str] | None = None,
    contract_notes: list[str] | None = None,
    interrupt: dict[str, Any] | None = None,
    contract: Any | None = None,
    ledger_truth: str = "dynamic",
    run_settings: dict[str, Any] | None = None,
    harness_usage: dict[str, dict[str, int]] | None = None,
) -> Report:
    if ledger_truth not in LEDGER_TRUTH_MODES:
        raise ValueError(f"ledger_truth must be one of {LEDGER_TRUTH_MODES}")
    other_truth = "static" if ledger_truth == "dynamic" else "dynamic"
    round_errors = [f"{r.persona_id}: {e}" for r in results for e in r.errors]
    gaps = [f"{r.persona_id}: {g}" for r in results for g in r.contract_gaps]

    # The one branch that matters. There is no `else` that produces numbers.
    quality: JudgmentQuality | _NotMeasured
    if live:
        quality = _judgment_quality(results)
    else:
        quality = NOT_MEASURED

    return Report(
        live=live,
        run_id=run_id,
        suite="personas",
        ledger=[ledger_agreement(r, ledger_truth) for r in results],
        ledger_truth=ledger_truth,
        ledger_comparison=[ledger_agreement(r, other_truth) for r in results],
        learning=learning_report(results),
        reading_intent=reading_intent_report(results),
        subdomains=subdomain_report(results, personas),
        per_state=per_state_accuracy(results),
        provisional=provisional_signal(results),
        exposure=exposure_report(results),
        silence=silence_regression(results, store),
        reading=reading_regression(results, store, run_id, contract=contract),
        quantity_offences=quantity_phrasing_check(results, personas),
        read_quality=read_quality_agreement(results),
        faithfulness=question_faithfulness(results),
        no_questions=no_questions_to_user(results),
        band=band_containment(results),
        cold_start=cold_start(results),
        engagement=thread_engagement(results, personas),
        style=style_fidelity(results, personas),
        noise=noise_report(results),
        isolation=check_isolation(store, results),
        leakage=check_answer_key_leakage(store, run_id),
        costs=cost_and_latency(store, run_id),
        unexpected_judgment_calls=sorted(set(unexpected_calls or [])),
        contract_notes=sorted(set(list(contract_notes or []) + gaps)),
        round_errors=round_errors,
        quality=quality,
        interrupt=interrupt or {},
        personas_run=len(results),
        run_settings=dict(run_settings or {}),
        harness_usage={k: dict(v) for k, v in (harness_usage or {}).items()},
    )


# --- Serialisation ---------------------------------------------------------
#
# The artifact splits into a deterministic body and a `volatile` subtree
# holding everything that legitimately differs between two identical runs --
# ids, wall-clock latency, tokens. Reproducibility is then a plain diff of the
# body, with no per-field exclusion list to keep in sync.


def _round_dict(record) -> dict[str, Any]:
    return {
        "index": record.index,
        "sim_time": record.sim_time,
        "advance_hours": record.advance_hours,
        "events_injected": record.events_injected,
        "candidates_seen": record.candidates_seen,
        "material_count": record.material_count,
        "thread_ran": record.thread_ran,
        "quiet": record.quiet,
        "surfaced_count": record.surfaced_count,
        "held_back_count": record.held_back_count,
        "behind_count": record.behind_count,
        "proficiency": record.proficiency,
        "topic_group": record.topic_group,
        "session_need": record.session_need,
        "errors": list(record.errors),
        "probe_only": bool(getattr(record, "probe_only", False)),
        "known_count": getattr(record, "known_count", None),
    }


def _thread_dict(thread: ThreadRecord, live: bool, vocabulary: tuple[str, ...] = ()) -> dict[str, Any]:
    """One thread, verbatim.

    `plainness` is the briefing's plainness bundle (`harness/plainness.py`),
    computed against the group's vocabulary so the gate can summarise it by
    band without re-reading briefings. It is a function of the briefing text
    and `explained_terms`, both of which are already in the artifact; nothing
    about the persona enters it.

    Two things are held back offline, for the same reason as before.

    The persona's *intent* (`asks_about` / `presupposes` / `mentions`, and the
    thread-level roll-ups) is the answer key for evidence extraction, which the
    report refuses to score offline. Leaving it in the artifact would let a
    reader recompute in a spreadsheet precisely the number the report declined
    to give, which would make the refusal decorative.

    `held_at_the_time` / `lacked_at_the_time` are the persona's live concept
    sets, which is the ledger metric's ground truth restated per turn. They are
    the strongest answer key in the file and are never written at all -- not
    even live. `question_faithfulness` consumes them in memory and reports
    counts.
    """
    body: dict[str, Any] = {
        "round": thread.round_index,
        "topic": thread.topic,
        "briefing": thread.briefing,
        "briefing_chars": thread.briefing_chars,
        "explained_terms": list(thread.explained_terms),
        "terms_used": list(thread.terms_used),
        "plainness": _plainness(thread.briefing, vocabulary, thread.explained_terms),
        "reading": (
            {
                "profile": thread.reading.profile,
                "dwell_ms": thread.reading.dwell_ms,
                "scroll_fraction": thread.reading.scroll_fraction,
                "harness_expected_quality": thread.reading.expected_quality,
                "system_read_quality": thread.reading.system_quality,
                "source": thread.reading.source,
                "agrees": thread.reading.agrees,
                "intent_act": thread.reading.intent_act,
                "intent_informed": thread.reading.intent_informed,
                "known_share": thread.reading.known_share,
                "story_known": thread.reading.story_known,
                "system_attention": thread.reading.system_attention,
            }
            if thread.reading
            else None
        ),
        "user_turn_count": thread.user_turn_count,
        "system_turn_count": thread.system_turn_count,
        "turns": [
            {
                "seq": t.seq,
                "speaker": t.speaker,
                "text": t.text,
                "words": t.words,
                **(
                    {"kind": t.kind, "form": t.form, "tier": t.tier}
                    if t.speaker == "user"
                    else {}
                ),
            }
            for t in thread.turns
        ],
        "understood": list(thread.understood),
        "not_understood": list(thread.not_understood),
        "asked_about": list(thread.asked_about),
        "already_knew": thread.already_knew,
        "proficiency_before": thread.proficiency_before,
        "proficiency_after": thread.proficiency_after,
        "newly_known": list(thread.newly_known),
        "learned_this_thread": list(thread.learned_this_thread),
        "stayed_silent_on": list(thread.stayed_silent_on),
        "asked_about_known": list(thread.asked_about_known),
    }
    body["event_headline"] = thread.event_headline
    if live:
        body["intent_asked"] = list(thread.intent_asked)
        body["intent_presupposed"] = list(thread.intent_presupposed)
        body["intent_mentioned"] = list(thread.intent_mentioned)
        body["unknown_term_present"] = thread.unknown_term_present
        body["event_concepts"] = list(thread.event_concepts)
    return body


def to_dict(report: Report, results: list[PersonaResult]) -> dict[str, Any]:
    live = report.live
    by_id = {r.persona_id: r for r in results}

    comparison_by_id = {a.persona_id: a for a in report.ledger_comparison}
    learning_by_id = {l.persona_id: l for l in report.learning}
    reading_intent_by_id = {r.persona_id: r for r in report.reading_intent}
    subdomain_by_id = {r.persona_id: r for r in report.subdomains}

    def _subdomain_dict(entry: PersonaSubdomain | None) -> dict[str, Any] | None:
        if entry is None:
            return None
        return {
            "taxonomy": entry.taxonomy,
            "true_weights": entry.true_weights,
            "seeded_by_weight": entry.seeded_by_weight,
            "labels": {
                "exported": entry.labels_exported,
                "labelled_terms": entry.labelled_terms,
                "exact_matches": entry.exact_matches,
                "near_matches": entry.near_matches,
                "exact_rate": entry.exact_rate,
                "near_rate": entry.near_rate,
                "unlabelled_scored_terms": entry.unlabelled_ledger_terms,
                "mismatch_examples": entry.label_examples,
            },
            "runtime_assignments": entry.runtime_assignments,
            "familiarity": {
                "exported": entry.familiarity_exported,
                "system": entry.system_familiarity,
                "compared_subdomains": entry.compared_subdomains,
                "spearman_rho": entry.familiarity_rho,
                "mean_abs_gap": entry.familiarity_mean_gap,
                "rows": entry.familiarity_rows,
                "unmatched_system_subdomains": entry.unmatched_system_subdomains,
                "unlabelled_attested": entry.unlabelled_attested,
            },
            "depth_fit": {
                "measure": "plainness",
                "buckets": {k: b.as_dict() for k, b in entry.depth.items()},
                "plainer_for_beginners": entry.plainer_for_beginners,
                "definitions_occasional": entry.definitions_occasional,
                "definitions_occasional_measure": "asides_per_100w",
                "glosses_occasional_max": GLOSSES_OCCASIONAL_MAX,
                "rows": entry.depth_rows,
            },
            "summary": entry.summary_line,
        }

    def _learning_dict(entry: PersonaLearning | None) -> dict[str, Any]:
        if entry is None:
            return {}
        return {
            "prior_knowledge": entry.prior_knowledge,
            "memory_rate": entry.memory_rate,
            "immediate_mean_rung": entry.immediate_mean,
            "immediate_n": entry.immediate_n,
            "retained_by_interval": {
                k: {"mean_rung": v[0], "n": v[1]}
                for k, v in entry.retained_by_bucket.items()
            },
            "confabulated": entry.confabulated,
            "pseudo_terms_probed": entry.pseudo_n,
            "confabulation_rate": entry.confabulation_rate,
            "calibration_gap_mean": entry.calibration_gap,
            "calibration_n": entry.calibration_n,
            "known_initial": entry.initial_known,
            "known_final": entry.final_known,
            "known_ever": entry.ever_known,
            "learned": entry.learned,
            "learned_by_asking": entry.learned_by_asking,
            "forgotten": entry.forgotten,
            "summary": entry.summary_line,
            "known_by_session": [list(p) for p in entry.known_by_session],
            "known_slope": entry.known_slope,
            "retained_by_session": [list(p) for p in entry.retained_by_session],
            "retained_slope": entry.retained_slope,
            "teaching_efficiency": entry.efficiency.to_dict() if entry.efficiency else None,
            "probe_only_sessions": entry.probe_only_sessions,
            "sessions": [
                {
                    "round": s.round_index,
                    "items": s.items,
                    "immediate_mean_rung": s.immediate,
                    "immediate_n": s.immediate_n,
                    "retained_mean_rung": s.retained,
                    "retained_n": s.retained_n,
                    "retained_by_interval": {
                        k: {"mean_rung": v[0], "n": v[1]} for k, v in s.by_bucket.items()
                    },
                    "confabulated": s.confabulated,
                    "pseudo_n": s.pseudo_n,
                    "calibration_gap_mean": s.calibration_gap,
                }
                for s in entry.sessions
            ],
        }

    personas_out = []
    for agreement in report.ledger:
        result = by_id[agreement.persona_id]
        other = comparison_by_id.get(agreement.persona_id)
        personas_out.append(
            {
                "persona_id": agreement.persona_id,
                "display_name": result.display_name,
                "archetype": result.archetype,
                "is_noisy": agreement.is_noisy,
                "group_name": result.group_name,
                "ledger_agreement": {
                    "precision": agreement.precision,
                    "recall": agreement.recall,
                    "f1": agreement.f1,
                    "first_met_at_interaction": agreement.first_met_at,
                    "held_from_interaction": agreement.held_from,
                    "interactions_run": agreement.interactions_run,
                    "elicitation_of_concept_set": agreement.coverage,
                    "passed": agreement.passed,
                    "gated": agreement.gated,
                    "trajectory": [
                        {
                            "interaction": p.interaction,
                            "believed_known": p.believed,
                            "precision": p.precision,
                            "recall": p.recall,
                            "f1": p.f1,
                            "meets_threshold": p.meets_threshold,
                        }
                        for p in agreement.trajectory
                    ],
                    "false_positives": agreement.false_positives,
                    "false_negatives": agreement.false_negatives,
                    "ground_truth": agreement.ground_truth,
                    "under_other_ground_truth": (
                        {
                            "ground_truth": other.ground_truth,
                            "precision": other.precision,
                            "recall": other.recall,
                            "f1": other.f1,
                            "held_from_interaction": other.held_from,
                            "passed": other.passed,
                        }
                        if other is not None
                        else None
                    ),
                },
                "learning": _learning_dict(learning_by_id.get(agreement.persona_id)),
                "reading_intent": (
                    {
                        "profile": ri.profile,
                        "acts": ri.acts,
                        "attention_rho": ri.attention_rho,
                        "attention_n": ri.attention_n,
                        "skips": ri.skips,
                        "resolved": ri.resolved,
                        "unresolved": ri.unresolved,
                        "informed_precision": ri.informed_precision,
                        "informed_recall": ri.informed_recall,
                        "true_informed_rate": ri.true_informed_rate,
                        "p_informed_trajectory": ri.p_informed_trajectory,
                        "final_gap": ri.final_gap,
                    }
                    if (ri := reading_intent_by_id.get(agreement.persona_id)) is not None
                    else None
                ),
                "subdomains": _subdomain_dict(subdomain_by_id.get(agreement.persona_id)),
                # Probe items: term, rungs, timing. The persona's answer text
                # and the grader's reason are deliberately NOT written -- they
                # are harness-side prose that has no business in an artifact
                # that is also stored in the system's eval_runs table.
                "probes": [
                    {
                        "round": s.round_index,
                        "items": [
                            {
                                "term": i.term,
                                "kind": i.kind,
                                "is_pseudo": i.is_pseudo,
                                "sampled_rung": i.sampled_rung,
                                "graded_rung": i.graded_rung,
                                "confabulated": i.confabulated,
                                "days_since_last_exposure": i.days_since_last_exposure,
                                "r_at_probe": i.r_at_probe,
                                "p_define_at_probe": i.p_define_at_probe,
                            }
                            for i in s.items
                        ],
                    }
                    for s in result.probes
                ],
                "memory_final": result.memory_final,
                "final_ledger": result.final_ledger,
                "exposure_counts": result.exposure_counts,
                "true_concepts": {
                    "initial_known": list(result.initial_knows),
                    "initial_unknown": list(result.initial_does_not_know),
                    "learned_during_run": list(result.learned),
                    "learned_by_asking": list(result.learned_by_asking),
                    "final_known": list(result.final_truly_known),
                    "final_known_static": list(result.final_truly_known_static),
                    "ever_known": list(result.ever_known),
                },
                "reading_profile": result.reading_profile,
                "rounds": [_round_dict(r) for r in result.rounds],
                "threads": [_thread_dict(t, live, result.vocabulary) for t in result.threads],
                "materiality_observations": [
                    {
                        "round": o.round_index,
                        "headline": o.headline,
                        "judged_material": o.judged_material,
                        "materiality_score": o.materiality_score,
                        **(
                            {
                                "should_be_material": o.should_be_material,
                                "significance": o.significance,
                            }
                            if live
                            else {}
                        ),
                    }
                    for o in result.materiality
                ],
                "errors": result.errors,
            }
        )

    quality: Any
    if live:
        q = report.quality
        quality = {
            "materiality": {
                "true_positives": q.materiality.true_positives,
                "false_positives": q.materiality.false_positives,
                "true_negatives": q.materiality.true_negatives,
                "false_negatives": q.materiality.false_negatives,
                "precision": q.materiality.precision,
                "recall": q.materiality.recall,
                "f1": q.materiality.f1,
                "by_significance": q.materiality.by_significance,
                "false_positive_examples": q.materiality.false_positive_examples,
                "false_negative_examples": q.materiality.false_negative_examples,
            },
            "evidence_extraction": {
                "mentions_total": q.evidence.mentions_total,
                "mentions_caught": q.evidence.mentions_caught,
                "mention_recall": q.evidence.mention_recall,
                "asks_total": q.evidence.asks_total,
                "asks_caught": q.evidence.asks_caught,
                "ask_recall": q.evidence.ask_recall,
                "spurious_understood": q.evidence.spurious_understood,
                "presupposed_read_as_asked": q.evidence.presupposed_read_as_asked,
                "presupposed_total": q.evidence.presupposed_total,
                "presupposition_confusion": q.evidence.presupposition_confusion,
                "examples": q.evidence.examples,
            },
        }
    else:
        quality = REFUSAL

    noisy = [a for a in report.ledger if a.is_noisy]
    clean = [a for a in report.ledger if not a.is_noisy]

    def _mean_precision(bucket: list[LedgerAgreement]) -> float | None:
        values = [a.precision for a in bucket if a.precision is not None]
        return round(statistics.fmean(values), 4) if values else None

    body: dict[str, Any] = {
        "suite": report.suite,
        "mode": "live" if live else "offline",
        "passed": report.passed,
        "personas_run": report.personas_run,
        "config": {
            "ledger_min_precision": config.LEDGER_MIN_PRECISION,
            "ledger_min_recall": config.LEDGER_MIN_RECALL,
            "ledger_max_interactions": config.LEDGER_MAX_INTERACTIONS,
            "correct_uses_before_confirmed": config.CORRECT_USES_BEFORE_CONFIRMED,
            "known_states": list(config.KNOWN_STATES),
            "band_states": list(config.BAND_STATES),
            "models": {p: config.model_for(p) for p in sorted(config.ROUTED_CALLS)},
            "effort": {p: config.effort_for(p) for p in sorted(config.ROUTED_CALLS)},
            "prompt_versions": prompt_versions(),
        },
        "ledger_ground_truth": report.ledger_truth,
        # Non-volatile header: what the gate checks before pairing two runs.
        "run_settings": report.run_settings,
        "learning_summary": {
            "columns": list(LEARNING_SUMMARY_COLUMNS),
            "rows": [l.summary_row for l in report.learning],
            "lines": [l.summary_line for l in report.learning],
            "table": render_learning_summary(report.learning) if report.learning else "",
        },
        "subdomain_summary": [e.summary_line for e in report.subdomains],
        "ledger_summary": {
            "all_passed": all(a.passed for a in report.ledger),
            "mean_precision": _mean_precision(report.ledger),
            "clean_mean_precision": _mean_precision(clean),
            "noisy_mean_precision": _mean_precision(noisy),
            "noisy_personas": [a.persona_id for a in noisy],
        },
        "per_state_accuracy": {
            "provisional_accuracy": report.per_state.provisional_accuracy,
            "by_state": [
                {
                    "state": s.state,
                    "n": s.n,
                    "correct": s.correct,
                    "accuracy": s.accuracy,
                    "wrong_examples": s.wrong_examples,
                }
                for s in report.per_state.by_state
            ],
        },
        "provisional_state_signal": {
            "p_known_given_provisional": report.provisional.provisional_rate,
            "provisional_n": report.provisional.provisional_n,
            "p_known_given_exposed_not_provisional": report.provisional.control_rate,
            "control_n": report.provisional.control_n,
            "p_known_given_unknown": report.provisional.unknown_rate,
            "unknown_n": report.provisional.unknown_n,
            "p_known_overall": report.provisional.overall_rate,
            "overall_n": report.provisional.overall_n,
            "lift_over_exposed_control": report.provisional.lift,
            "lift_over_unknown": report.provisional.lift_over_unknown,
            "verdict": report.provisional.verdict,
        },
        "exposure_signal": {
            "note": (
                "exposure is counted and never inferred from; these rows say "
                "what repetition alone would have predicted, and are why "
                "silence-based promotion is gone"
            ),
            "base_rate": report.exposure.base_rate,
            "max_exposures_observed": report.exposure.max_exposures,
            "buckets": [
                {
                    "exposed_at_least": b.at_least,
                    "n": b.n,
                    "known": b.known,
                    "p_known": b.rate,
                }
                for b in report.exposure.buckets
            ],
        },
        "silence_regression": {
            "clean": report.silence.clean,
            "violations": report.silence.violations,
            "known_state_terms_checked": report.silence.known_state_terms,
            "disengaged_persona_known_terms": report.silence.silent_persona_known_terms,
            "note_exposure_promotions": report.silence.note_exposure_promotions,
            "familiar_terms_traced_to_a_read_gloss": report.silence.familiar_traced,
            "disengaged_persona_familiar": report.silence.silent_reader_familiar,
            "disengaged_persona_familiar_at_band_bar": report.silence.silent_reader_over_bar,
            "disengaged_persona_band": report.silence.silent_reader_band,
            "band_bar_read_explanations": band_bar(),
        },
        "reading_regression": {
            "note": (
                "reading behaviour is attention, never comprehension. This is "
                "the third attention signal in this project to try to become a "
                "knowledge claim; the first two were `assumed` (deleted, "
                "anti-predictive at every bar) and model paraphrase in the "
                "evidence extractor (fixed, gate kept)."
            ),
            "clean": report.reading.clean,
            "violations": report.reading.violations,
            "threads_checked": report.reading.threads_checked,
            "threads_with_no_user_turn": report.reading.silent_threads,
            "probe_state_before": report.reading.probe_state_before,
            "probe_state_after": report.reading.probe_state_after,
            "close_thread_probes": report.reading.close_probe_notes,
            "silent_readers": report.reading.silent_readers,
            "reading_fields_scanned": list(READING_FIELDS),
            "judgment_rows_scanned": report.reading.judgment_rows_scanned,
        },
        "read_quality_agreement": {
            "note": (
                "the harness derives a band from the dwell and scroll it "
                "simulated; the store derives one from the same two numbers "
                "with its own implementation."
            ),
            "n": report.read_quality.n,
            "agreed": report.read_quality.agreed,
            "agreement_rate": report.read_quality.rate,
            "band_distribution": report.read_quality.distribution,
            "disagreements": report.read_quality.disagreements,
        },
        "question_faithfulness": {
            "note": (
                "what a persona asks must follow from its concept sets, or the "
                "most diagnostic thing this harness produces is a template "
                "bank talking to itself. Hard gate, both modes."
            ),
            "clean": report.faithfulness.clean,
            "questions": report.faithfulness.questions,
            "lacking_form_when_lacking": report.faithfulness.lacking_form_when_lacking,
            "holding_form_when_holding": report.faithfulness.holding_form_when_holding,
            "configured_noise_questions": report.faithfulness.configured_noise_questions,
            "by_form": report.faithfulness.by_form,
            "by_tier": {str(k): v for k, v in report.faithfulness.by_tier.items()},
            "violations": report.faithfulness.violations,
        },
        "no_questions_to_user": {
            "note": (
                "the system states the substance and stops. Every briefing in "
                "the transcript that prompted this rebuild ended in a question "
                "the system chose, and every one presupposed knowledge the "
                "reader might not have."
            ),
            "clean": report.no_questions.clean,
            "briefings_checked": report.no_questions.briefings,
            "offending": report.no_questions.offending,
        },
        "band_containment": [
            {
                "persona_id": b.persona_id,
                "bands_seen": b.bands_seen,
                "highest_band": b.highest,
                "briefings": b.briefings,
                "user_turns": b.user_turns,
                "read_quality_seen": b.reads,
                "evidence_given": b.evidence_given,
                "passed": b.passed,
            }
            for b in report.band
        ],
        "quantity_phrasing": {
            "clean": not report.quantity_offences,
            "offences": report.quantity_offences,
        },
        "cold_start": [
            {
                "persona_id": c.persona_id,
                "unknown_terms_encountered": c.unknown_terms,
                "explained": c.explained_terms,
                "explained_share": c.explained_share,
                "claimed_without_explaining": c.claimed_without_explaining,
                "briefed_before_asked": c.briefed_before_asked,
                "passed": c.passed,
                "gated": c.gated,
            }
            for c in report.cold_start
        ],
        "thread_engagement": {
            "caveat": (
                "READ THE DELTAS, NOT THE RATES. An absolute rate here is the "
                "persona's configuration -- its ask rates, its reading profile "
                "-- and would print the same number against briefings of any "
                "quality. The `*_delta` fields are observed minus what that "
                "persona's own configuration predicts for the briefings this "
                "run actually produced, and are the only part that can be "
                "about the briefing. The previous version of this metric "
                "reported the rates alone and the aggregate was read as a "
                "finding about briefing quality when it was the mean of five "
                "configured ask_back_rate values. Reported, never gated."
            ),
            "levels": list(ENGAGEMENT_LEVELS),
            "per_persona": [
                {
                    "persona_id": e.persona_id,
                    "briefings": e.briefings,
                    "read_bands": e.read_bands,
                    "mean_read_band": e.read_score,
                    "mean_read_band_delta": e.read_score_delta,
                    "turn_rate": e.turn_rate,
                    "turn_rate_delta": e.turn_rate_delta,
                    "question_rate": e.question_rate,
                    "question_rate_delta": e.question_rate_delta,
                    "turns_per_thread": e.turns_per_thread,
                    "turns_per_thread_delta": e.turns_per_thread_delta,
                    "multi_turn_threads": e.multi_turn_threads,
                    "later_reuse_rate": e.reuse_rate,
                    "mean_level": e.mean_level,
                    "per_briefing_level": e.per_briefing,
                    "expected": {
                        "threads_with_a_turn": e.expected_threads_with_a_turn,
                        "threads_with_a_question": e.expected_threads_with_a_question,
                        "user_turns": e.expected_user_turns,
                        "read_band_total": e.expected_read_score_total,
                    },
                }
                for e in report.engagement
            ],
        },
        "style_fidelity": [
            {
                "persona_id": s.persona_id,
                "archetype": s.archetype,
                "lowercase": s.lowercase,
                "replies": s.replies,
                "observed_median_words": s.observed_median,
                "observed_share_under_10": s.observed_short_share,
                "observed_share_over_35": s.observed_long_share,
                "configured_median_words": s.configured_median,
                "configured_share_under_10": s.configured_short_share,
                "configured_share_over_35": s.configured_long_share,
                "long_tail_rule_applies": s.long_rule_applies,
                "configured_ok": s.configured_ok,
            }
            for s in report.style
        ],
        "noise": [
            {
                "persona_id": n.persona_id,
                "stayed_silent_on": n.silent_terms,
                "silence_promoted_terms": n.silence_promoted,
                "asked_about_terms_it_knew": n.asked_about_known,
                "interactions_spent_on_known_terms": n.interactions_spent_on_known,
            }
            for n in report.noise
        ],
        "plumbing": {
            "isolation": {
                "checks_run": report.isolation.checks_run,
                "violations": report.isolation.violations,
            },
            "answer_key_leakage": {
                "rows_scanned": report.leakage.rows_scanned,
                "fields_scanned": list(ANSWER_KEY_FIELDS),
                "leaks": report.leakage.leaks,
            },
            "unexpected_judgment_calls": report.unexpected_judgment_calls,
            "contract_notes": report.contract_notes,
            "round_errors": report.round_errors,
            "judgment_errors": {c.point: c.errors for c in report.costs if c.errors},
            "call_counts": {c.point: c.calls for c in report.costs},
        },
        "judgment_quality": quality,
        "interrupt_cases": report.interrupt,
        "personas": personas_out,
        "volatile": {
            "run_id": report.run_id,
            "latency_ms": {
                c.point: {"mean": c.mean_latency_ms, "max": c.max_latency_ms}
                for c in report.costs
            },
            "tokens": {
                c.point: {"input": c.input_tokens, "output": c.output_tokens}
                for c in report.costs
            },
            # The harness's own calls (persona voice, probe voice, grader),
            # which are deliberately not in `judgment_log`.
            "harness_usage": report.harness_usage,
        },
    }
    return body


def prompt_versions() -> dict[str, str]:
    """Prompt version in force for every routed call, for the run record."""
    from conversational_agent.judgment import JudgmentError, load_prompt

    out = {}
    for point in sorted(config.ROUTED_CALLS):
        try:
            out[point] = load_prompt(point).version
        except JudgmentError:
            out[point] = "missing"
    return out


def canonical(payload: dict[str, Any]) -> str:
    """The reproducibility surface: everything but `volatile`, sorted."""
    body = {k: v for k, v in payload.items() if k != "volatile"}
    return json.dumps(body, indent=2, sort_keys=True, default=str)


# --- Human-readable report -------------------------------------------------


def _bar(label: str) -> str:
    return f"\n{label}\n{'-' * len(label)}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def render(report: Report, results: list[PersonaResult]) -> str:
    by_id = {r.persona_id: r for r in results}
    lines: list[str] = []
    mode = "LIVE" if report.live else "OFFLINE"
    lines.append("=" * 74)
    lines.append(f"Persona evaluation harness -- suite 'personas' -- {mode}")
    lines.append("=" * 74)

    if not report.personas_run:
        lines.append("\nNo personas defined. Nothing to run.")
        return "\n".join(lines)

    rs = report.run_settings or {}
    if rs:
        from .config import HarnessRouting

        routing = HarnessRouting.from_dict(rs.get("harness_routing"))
        horizon = rs.get("horizon_days")
        lines.append(
            f"  {routing.voice_line}"
            + ("" if report.live else "  [offline: scripted persona, stub grader; routing recorded only]")
        )
        lines.append(
            f"  fixtures {rs.get('fixture_dir', '?')}; rounds cap {rs.get('rounds_cap')}; "
            + (
                f"horizon {horizon} days, {rs.get('retention_probes', 0)} probe-only day(s)"
                if horizon is not None
                else "default clock (poll interval between rounds)"
            )
        )

    if not report.live:
        lines.append(
            "\nOffline mode. The materiality stub is handed the answer key, so\n"
            "judgment-quality numbers are refused outright rather than printed\n"
            "and captioned. What this run measures is LEDGER MECHANICS,\n"
            "isolation, cold-start behaviour and reproducibility. It is not\n"
            "evidence that any prompt works."
        )

    lines.append(_bar("Contract status"))
    lines.append("  every member of the frozen contract is bound to src/.")

    lines.append(_bar("Ledger agreement  (system's ledger vs. what they actually know)"))
    lines.append(
        f"  thresholds: precision >= {config.LEDGER_MIN_PRECISION}, "
        f"recall >= {config.LEDGER_MIN_RECALL}, "
        f"held by interaction {config.LEDGER_MAX_INTERACTIONS}"
    )
    lines.append(
        f"  ground truth: {report.ledger_truth.upper()} -- "
        + (
            "a term is known at time t when the persona memory model's\n"
            "  P(can define) >= 0.5 at t (harness/LEARNING.md). The fixture's `knows`\n"
            "  seeds t = 0 only; the static key is shown per persona for comparison."
            if report.ledger_truth == "dynamic"
            else "the fixture's `knows` plus asked-and-answered, never forgotten\n"
            "  (the pre-model key; the dynamic key is shown per persona for comparison)."
        )
    )
    comparison_by_id = {a.persona_id: a for a in report.ledger_comparison}
    for agreement in report.ledger:
        result = by_id[agreement.persona_id]
        flag = ("PASS" if agreement.passed else "FAIL") if agreement.gated else "----"
        at = (
            f"interaction {agreement.held_from}"
            if agreement.held_from is not None
            else "never held"
        )
        lines.append(
            f"\n  [{flag}] {agreement.persona_id}"
            + ("  (NOISY)" if agreement.is_noisy else "")
            + f"  -- {result.archetype}"
            + ("" if agreement.gated else "   [reported, not gated]")
        )
        lines.append(f"         group          {result.group_name}")
        lines.append(
            f"         precision      {_pct(agreement.precision)}   "
            f"recall {_pct(agreement.recall)}   F1 {_pct(agreement.f1)}"
        )
        lines.append(f"         threshold met  {at}")
        lines.append(
            f"         concept set    {len(result.final_truly_known)} known, "
            f"elicitation {_pct(agreement.coverage)}"
        )
        lines.append(
            "         precision by interaction  "
            + " -> ".join(_pct(p.precision) for p in agreement.trajectory)
        )
        lines.append(
            "         recall by interaction     "
            + " -> ".join(_pct(p.recall) for p in agreement.trajectory)
        )
        other = comparison_by_id.get(agreement.persona_id)
        if other is not None:
            other_at = (
                f"held from {other.held_from}"
                if other.held_from is not None
                else "never held"
            )
            lines.append(
                f"         under {other.ground_truth} truth  precision {_pct(other.precision)}"
                f"   recall {_pct(other.recall)}   {other_at}"
            )
        for line in agreement.false_positives[:6]:
            lines.append(f"           ! {line}")
        for line in agreement.false_negatives[:6]:
            lines.append(f"           ? {line}")

    lines.append(_bar("Learning  (persona memory model + post-session probe; harness-only)"))
    lines.append(
        "  Each persona carries a forgetting curve (FSRS core, learning_model.md §0)\n"
        "  and is probed after every session on 2-4 terms, graded 0-3:\n"
        "  0 never seen, 1 recognises, 2 can define, 3 can use for this story.\n"
        "  Offline the persona answers at exactly the rung the model sampled, so\n"
        "  the calibration gap is 0 by construction; live it is the number to watch."
    )
    if report.learning:
        rounds_seen = sorted(
            {s.round_index for entry in report.learning for s in entry.sessions}
        )
        lines.append("\n  knowledge trajectory  (mean graded rung: immediate / retained)")
        header = f"    {'persona':<20}" + "".join(f"{'s' + str(r):>9}" for r in rounds_seen)
        lines.append(header)
        for entry in report.learning:
            by_round = {s.round_index: s for s in entry.sessions}
            cells = []
            for r in rounds_seen:
                s = by_round.get(r)
                if s is None:
                    cells.append(f"{'--':>9}")
                    continue
                imm = "-" if s.immediate is None else f"{s.immediate:.1f}"
                ret = "-" if s.retained is None else f"{s.retained:.1f}"
                cells.append(f"{imm + '/' + ret:>9}")
            lines.append(f"    {entry.persona_id:<20}" + "".join(cells))
        lines.append("\n  learning summary  (trend and teaching efficiency per persona)")
        lines.append(render_learning_summary(report.learning))
        lines.append("")
        for entry in report.learning:
            lines.append(f"  {entry.summary_line}")
            detail = "; ".join(
                f"{k}: {('n/a' if v[0] is None else f'{v[0]:.2f}')} (n={v[1]})"
                for k, v in entry.retained_by_bucket.items()
            )
            lines.append(
                f"      retained by interval  {detail}\n"
                f"      known {entry.initial_known} -> {entry.final_known} "
                f"(ever {entry.ever_known}); learned {entry.learned or '[]'}"
                f" (by asking {entry.learned_by_asking or '[]'}); "
                f"forgotten {entry.forgotten or '[]'}"
            )

    lines.append(_bar("Reading intent  (what a skip meant, and whether the system saw it)"))
    lines.append(
        "  Personas now read from what they hold: an informed skip when they already\n"
        "  have the story's terms, a lazy skip at their profile's rate, otherwise a\n"
        "  skim-to-read shifted by how much is new. The system sees only dwell and\n"
        "  scroll. It classifies a skip informed/lazy only from LATER evidence about\n"
        "  the terms that briefing defined; unresolved is a legitimate outcome and\n"
        "  is not penalised. Three numbers: attention ρ (system 0-10 vs true depth),\n"
        "  informed-skip precision/recall over resolved skips, and how close the\n"
        "  system's p_informed prior ends to the persona's true informed-skip rate."
    )
    for entry in report.reading_intent:
        lines.append(f"  {entry.summary_line}")
        if entry.p_informed_trajectory:
            lines.append(
                "      p_informed by session  "
                + " -> ".join(f"{v:.2f}" for v in entry.p_informed_trajectory)
                + (f"   final gap {entry.final_gap:.2f}" if entry.final_gap is not None else "")
            )
        for line in entry.misclassified[:4]:
            lines.append(f"      ! {line}")

    lines.append(_bar("Subdomains  (does the system see the SHAPE of what they know?)"))
    lines.append(
        "  Each fixture partitions its glossary into subdomains and gives the persona\n"
        "  a 0-1 weight per subdomain; unlabelled terms are seeded as known at that\n"
        "  weight and a gloss in a familiar subdomain encodes faster. Three readouts,\n"
        "  reported and not gated: (a) the system's `concepts.subdomain` labels vs the\n"
        "  fixture taxonomy (exact / near); (b) `subdomain_familiarity` vs the true\n"
        "  weights (Spearman, mean |gap|); (c) depth fit, read as PLAINNESS since\n"
        "  `briefing` v7 (plain language over inline definitions): Flesch-Kincaid\n"
        "  grade, words per sentence, domain-term density and glosses (parenthetical\n"
        "  asides, dash pairs, 'X, meaning Y' cues) per 100 words, split by whether\n"
        "  the story fell in a held (w>=0.7) or not-held (w<=0.3) subdomain.\n"
        "  Expected: PLAINER where the reader is a beginner (lower grade and density\n"
        "  in not-held than held), and glosses OCCASIONAL everywhere (<= "
        f"{GLOSSES_OCCASIONAL_MAX:.1f}/100w in every bucket). 'made clear' is\n"
        "  `explained_terms` per 100 words -- under v7 that includes terms explained\n"
        "  in plain words, so it is no longer a gloss count."
    )
    if not report.live:
        lines.append(
            "  OFFLINE: the stub labels each term with the group-description phrase\n"
            "  it shares most tokens with (it is never shown the fixture taxonomy), so\n"
            "  (a) and (b) exercise the plumbing only; the numbers say nothing about\n"
            "  the system. (c) is real: the stub's gloss decision is the system's own\n"
            "  band rule applied to the ledger."
        )
    for entry in report.subdomains:
        lines.append(f"  {entry.summary_line}")
        if entry.seeded_by_weight:
            lines.append(f"      seeded by weight (beyond `knows`): {entry.seeded_by_weight}")
        if entry.runtime_assignments:
            placed = ", ".join(f"{t} -> {v}" for t, v in list(entry.runtime_assignments.items())[:6])
            lines.append(f"      runtime placement of out-of-vocabulary glosses: {placed}")
        for line in entry.label_examples[:4]:
            lines.append(f"      ! label mismatch {line}")
        for line in entry.familiarity_rows[:6]:
            lines.append(f"      familiarity {line}")
        if entry.unmatched_system_subdomains:
            lines.append(
                f"      system subdomains matching nothing in the fixture: "
                f"{entry.unmatched_system_subdomains}"
            )
        if entry.unlabelled_attested:
            lines.append(
                f"      {entry.unlabelled_attested} attested term(s) sit in the system's "
                "unlabelled bucket (no label yet; compared with nothing)"
            )
        for label in ("held", "middle", "not held", "unplaced"):
            b = entry.depth.get(label)
            if b is None or not b.briefings:
                continue
            lines.append(
                f"      plainness {label:<9} {b.briefings} briefing(s): "
                f"grade {b.mean_fk_grade:.1f}, {b.mean_sentence_length:.1f} w/sent, "
                f"terms {b.mean_domain_term_density:.1f}/100w, glosses {b.mean_asides_per_100w:.2f}/100w, "
                f"made clear {b.mean_definitions_per_100_words:.2f}/100w "
                f"(mean {b.mean_definitions:.1f} terms over {b.mean_words:.0f} words)"
            )

    lines.append(_bar("Per-state accuracy  (which evidence actually holds up)"))
    lines.append(
        "  `provisional` is now the weakest tier in the model: exactly one\n"
        "  correct unprompted use, held below `confirmed` pending a second\n"
        f"  ({config.CORRECT_USES_BEFORE_CONFIRMED} required) because the briefing\n"
        "  had just used the term and one echo may be parroting. `assumed` --\n"
        "  which inferred knowledge from silence -- was deleted after measuring\n"
        "  0/2 here and a negative lift at every exposure bar."
    )
    for state in report.per_state.by_state:
        if not state.n:
            continue
        lines.append(
            f"    {state.state:<26} {state.correct:>3}/{state.n:<3} correct   "
            f"accuracy {_pct(state.accuracy)}"
        )
    for state in report.per_state.by_state:
        if state.state == config.CONCEPT_PROVISIONAL:
            for example in state.wrong_examples:
                lines.append(f"      ! {example}")

    p = report.provisional
    lines.append(_bar("Does `provisional` carry any information?"))
    lines.append(
        "  The same test that killed `assumed`, applied to the weakest state\n"
        "  still standing. One correct use is a positive act rather than an\n"
        "  absence, so it starts in a better position -- but the briefing used\n"
        "  the term moments earlier, so an echo may be parroting. The number to\n"
        "  read is the lift over `unknown`: that is the counterfactual, since\n"
        "  deleting the state puts every one of these terms there instead."
    )
    lines.append(
        f"    P(known | provisional)                 {_pct(p.provisional_rate)}"
        f"   (n={p.provisional_n})"
    )
    lines.append(
        f"    P(known | exposed, not provisional)    {_pct(p.control_rate)}"
        f"   (n={p.control_n})"
    )
    lines.append(
        f"    P(known | left at `unknown`)           {_pct(p.unknown_rate)}"
        f"   (n={p.unknown_n})"
    )
    lines.append(
        f"    P(known | any ledger entry)            {_pct(p.overall_rate)}"
        f"   (n={p.overall_n})"
    )
    lines.append(
        "    lift over `unknown` (counterfactual)   "
        + (
            "n/a"
            if p.lift_over_unknown is None
            else f"{p.lift_over_unknown:+.2f}"
        )
    )
    lines.append(
        "    lift over exposed control              "
        + ("n/a" if p.lift is None else f"{p.lift:+.2f}")
    )
    lines.append(f"    -> {p.verdict}")

    e = report.exposure
    lines.append(_bar("Exposure  (counted, never inferred from)"))
    lines.append(
        "  Repetition is recorded so the briefing knows whether a term is new to\n"
        "  the conversation. It promotes nothing. These rows are what a\n"
        "  silence-based rule would have been betting on, kept in the report so\n"
        "  that reintroducing one requires arguing with a number."
    )
    lines.append(
        f"    base rate P(known | any ledger entry)  {_pct(e.base_rate)}"
        f"   (max exposures seen: {e.max_exposures})"
    )
    for bucket in e.buckets:
        if not bucket.n:
            continue
        lines.append(
            f"    P(known | exposed >= {bucket.at_least}x)             "
            f"{_pct(bucket.rate)}   (n={bucket.n})"
        )

    s = report.silence
    lines.append(_bar("Silence regression  (`assumed` must stay deleted)"))
    lines.append(
        "  Hard gate, both modes. Three independent checks: no term carries the\n"
        "  retired silence evidence string; no term reached a known state\n"
        "  without the persona saying something the system read as evidence;\n"
        "  and `store.note_exposure` is exercised directly and must promote\n"
        "  nothing after repeated exposure."
    )
    lines.append(
        "  `familiar` is the one exception, and it is narrow: a term the\n"
        "  briefing DEFINED which the person then read or skimmed. Allowed\n"
        "  only when it traces to such an exchange; one exposure votes\n"
        f"  neither way until read_explanations reaches {band_bar()}."
    )
    lines.append(
        f"  [{'PASS' if s.clean else 'FAIL'}] "
        f"{s.known_state_terms} known-state term(s) checked "
        f"({s.familiar_traced} familiar, each traced to a read exchange that glossed it), "
        f"note_exposure promoted {s.note_exposure_promotions} after 8 exposures"
    )
    for violation in s.violations:
        lines.append(f"      ! {violation}")
    if s.silent_reader_familiar or s.silent_reader_band:
        loud = s.silent_reader_over_bar >= 3
        lines.append(
            f"  {'!!! ' if loud else ''}disengaged persona: {s.silent_reader_familiar} term(s) "
            f"familiar, {s.silent_reader_over_bar} at read_explanations >= {band_bar()}; "
            f"band {s.silent_reader_band!r}"
            + (
                " -- THE SILENT-READER PATH IS LIVE: three or more terms reached the "
                "bar without a word from them. Chosen knowingly (checkpoint 3); "
                "here so it is seen."
                if loud
                else ""
            )
        )

    r = report.reading
    lines.append(_bar("Reading regression  (attention is not comprehension)"))
    lines.append(
        "  Hard gate, both modes, and the third time an attention signal in\n"
        "  this project has tried to become a knowledge claim -- after\n"
        "  `assumed` (deleted; anti-predictive at every exposure bar) and the\n"
        "  evidence extractor crediting understanding for model paraphrase.\n"
        "  Four checks: no reading fingerprint in any concept's evidence\n"
        "  (except the sanctioned `familiar` string, and only on `familiar`);\n"
        "  no term in a known state without a turn behind it, `familiar`\n"
        "  traced to a read exchange that glossed it excepted; `record_reading`\n"
        "  and `close_thread` exercised against a live scope; and no reading\n"
        "  field in any judgment context."
    )
    lines.append(
        f"  [{'PASS' if r.clean else 'FAIL'}] {r.threads_checked} thread(s), "
        f"{r.silent_threads} with no user turn at all; probe term "
        f"{r.probe_state_before!r} -> {r.probe_state_after!r} after 8 maximal "
        f"readings; {r.judgment_rows_scanned} judgment rows scanned for "
        f"{', '.join(READING_FIELDS[:4])}..."
    )
    for violation in r.violations:
        lines.append(f"      ! {violation}")
    for note in r.close_probe_notes:
        lines.append(f"         probe: {note}")
    for line in r.silent_readers:
        lines.append(f"         silent reader: {line}")

    rq = report.read_quality
    lines.append(_bar("Read quality  (does the store derive what the harness does?)"))
    lines.append(
        f"  {rq.agreed}/{rq.n} agreed ({_pct(rq.rate)})   bands seen: "
        + (", ".join(f"{k} {v}" for k, v in rq.distribution.items()) or "none")
    )
    for line in rq.disagreements:
        lines.append(f"      ! {line}")

    q = report.no_questions
    lines.append(_bar("The system asks the user nothing  (the redesign, gated)"))
    lines.append(
        "  A briefing states the substance and stops. Every briefing in the\n"
        "  transcript that prompted this rebuild ended in a question the system\n"
        "  chose, and every one of those presupposed knowledge the reader might\n"
        "  not have -- which is a test wearing a conversation's clothes."
    )
    lines.append(
        f"  [{'PASS' if q.clean else 'FAIL'}] {q.briefings} briefing(s) checked"
    )
    for line in q.offending:
        lines.append(f"      ! {line}")

    f = report.faithfulness
    lines.append(_bar("Question faithfulness  (does the ask follow from the ledger?)"))
    lines.append(
        "  The harness's own honesty check, and a hard gate. A persona that\n"
        "  does not hold `derogation` may only ask what one is; a persona that\n"
        "  holds it asks something further on. If that stops being true, the\n"
        "  most diagnostic thing this harness produces is a template bank\n"
        "  talking to itself, and every number downstream is decoration."
    )
    lines.append(
        f"  [{'PASS' if f.clean else 'FAIL'}] {f.questions} question(s): "
        f"{f.lacking_form_when_lacking} asked about a term they lacked, "
        f"{f.holding_form_when_holding} leaned on terms they held, "
        f"{f.configured_noise_questions} were configured noise"
    )
    if f.by_form:
        lines.append(
            "         forms drawn out   "
            + ", ".join(f"{k} {v}" for k, v in f.by_form.items())
        )
    if f.by_tier:
        lines.append(
            "         by tier (0=knows nothing about it, 3=probing)   "
            + ", ".join(f"t{k} {v}" for k, v in f.by_tier.items())
        )
    for violation in f.violations[:10]:
        lines.append(f"      ! {violation}")

    lines.append(_bar("Quantity phrasing  (a figure is only ever a figure)"))
    lines.append(
        "  A template about a figure being higher or lower than normal may\n"
        "  only be applied to a concept the fixture declares as a quantity.\n"
        "  Gated, because these sentences reach the extractor as input."
    )
    lines.append(
        f"  [{'PASS' if not report.quantity_offences else 'FAIL'}] "
        f"{len(report.quantity_offences)} offence(s)"
    )
    for offence in report.quantity_offences:
        lines.append(f"      ! {offence}")

    if report.band:
        lines.append(_bar("Silence spiral  (can the band be talked upward?)"))
        lines.append(
            "  A persona that reads its briefings and says nothing at all must\n"
            "  never leave `beginner`. Every input to its band is the system's\n"
            "  own output plus a reading measurement, so a band that climbs has\n"
            "  climbed on one of those two."
        )
        for b in report.band:
            flag = "PASS" if b.passed else "FAIL"
            lines.append(
                f"  [{flag}] {b.persona_id}: {b.briefings} briefings, "
                f"{b.user_turns} user turns, {b.evidence_given} pieces of "
                f"evidence, highest band {b.highest!r} (saw {b.bands_seen})"
            )
            lines.append(
                "         read as: "
                + (", ".join(f"{k} x{v}" for k, v in b.reads.items()) or "n/a")
            )

    if report.noise:
        lines.append(_bar("Noise  (what the noisy persona's behaviour actually cost)"))
        for noise in report.noise:
            lines.append(f"  {noise.persona_id}")
            lines.append(
                f"    stayed silent on          {noise.silent_terms or 'nothing'}"
            )
            lines.append(
                f"    silence promoted          {noise.silence_promoted or 'none'}"
            )
            lines.append(
                f"    asked about known terms   {noise.asked_about_known or 'none'} "
                f"({noise.interactions_spent_on_known} interaction(s) spent)"
            )

    if report.cold_start:
        lines.append(_bar("Cold start  (does it brief a beginner, or interrogate one?)"))
        for cold in report.cold_start:
            flag = ("PASS" if cold.passed else "FAIL") if cold.gated else "----"
            lines.append(
                f"  [{flag}] {cold.persona_id}"
                + ("" if cold.gated else "   [reported, not gated]")
            )
            lines.append(
                f"         briefed before asking, every exchange: "
                f"{cold.briefed_before_asked}"
            )
            lines.append(
                f"         unknown terms explained: {cold.explained_terms}"
                f"/{cold.unknown_terms} ({_pct(cold.explained_share)})"
            )
            if cold.claimed_without_explaining:
                lines.append(
                    "         ! claimed as known without ever explaining: "
                    + ", ".join(cold.claimed_without_explaining)
                )

    if report.engagement:
        lines.append(
            _bar("Briefing engagement  (deviation from configuration, not raw rates)")
        )
        lines.append(
            "  READ THE DELTA COLUMNS. An absolute rate here is a property of\n"
            "  the persona -- its configured ask rates, its reading profile --\n"
            "  and would print the same number against briefings of any quality\n"
            "  whatsoever. The previous version of this metric reported the raw\n"
            "  rates and the aggregate was read as a finding about briefing\n"
            "  quality when it was the mean of five configured ask_back_rate\n"
            "  values. Each delta is observed minus what that persona's own\n"
            "  configuration predicts for the briefings THIS RUN produced, so\n"
            "  it is the only part that can be about the briefing.\n"
            "\n"
            "  read     mean read band (0 skipped, 1 skimmed, 2 read, 3 studied)\n"
            "  turn     share of briefings that drew any user turn at all\n"
            "  ask      share that drew a question\n"
            "  t/br     user turns per briefing\n"
            "  reuse    a term from that briefing used correctly in a LATER one\n"
            "  Reported, never gated."
        )
        lines.append(
            f"    {'persona':<20}{'n':>3}{'read':>7}{'d':>7}{'turn':>7}{'d':>7}"
            f"{'ask':>7}{'d':>7}{'t/br':>7}{'d':>7}{'reuse':>7}{'lvl':>6}"
        )
        for e in report.engagement:
            def _d(value: float | None) -> str:
                return "  n/a" if value is None else f"{value:+.2f}"

            lines.append(
                f"    {e.persona_id:<20}{e.briefings:>3}"
                + (
                    f"{e.read_score:>7.2f}" if e.read_score is not None else f"{'n/a':>7}"
                )
                + f"{_d(e.read_score_delta):>7}"
                f"{_pct(e.turn_rate):>7}{_d(e.turn_rate_delta):>7}"
                f"{_pct(e.question_rate):>7}{_d(e.question_rate_delta):>7}"
                + (
                    f"{e.turns_per_thread:>7.2f}"
                    if e.turns_per_thread is not None
                    else f"{'n/a':>7}"
                )
                + f"{_d(e.turns_per_thread_delta):>7}"
                + f"{_pct(e.reuse_rate):>7}"
                + (
                    f"{e.mean_level:>6.2f}"
                    if e.mean_level is not None
                    else f"{'n/a':>6}"
                )
            )
        agg = [e for e in report.engagement if e.briefings]
        if agg:
            n = sum(e.briefings for e in agg)
            obs_turns = sum(e.threads_with_a_turn for e in agg)
            exp_turns_n = sum(e.expected_threads_with_a_turn for e in agg)
            obs_q = sum(e.threads_with_a_question for e in agg)
            exp_q = sum(e.expected_threads_with_a_question for e in agg)
            lines.append(
                f"    {'ALL (delta only)':<20}{n:>3}"
                f"{'':>7}{'':>7}"
                f"{'':>7}{(obs_turns - exp_turns_n) / n:>+7.2f}"
                f"{'':>7}{(obs_q - exp_q) / n:>+7.2f}"
            )
            lines.append(
                "    The aggregate is printed as a delta only, deliberately. "
                "An aggregate\n    raw rate across five differently-configured "
                "personas is the number\n    that was misread last time, and it "
                "should not be available to misread."
            )

    lines.append(_bar("Reply style  (reply_style_profile.md section 8.2)"))
    lines.append(
        f"  hard rule: >= {STYLE_MIN_SHORT_SHARE:.0%} of replies under "
        f"{STYLE_SHORT_WORDS} words, >= {STYLE_MIN_LONG_SHARE:.0%} over "
        f"{STYLE_LONG_WORDS}. Gated on the configured distribution; the\n"
        "  observed column is 8 replies per persona and is for eyeballing only.\n"
        "  The long-tail half is n/a for archetypes whose own p90 in that table\n"
        "  is under 35 words (barely_informed 28, disengaged 14) -- the two\n"
        "  halves of section 8.2 cannot both hold for those."
    )
    for style in report.style:
        flag = "ok " if style.configured_ok else "BAD"
        long_col = (
            f"{style.configured_long_share:.0%}" if style.long_rule_applies else "n/a"
        )
        lines.append(
            f"    [{flag}] {style.persona_id:<18} median {style.configured_median:>5} "
            f"  <10w {style.configured_short_share:.0%}   >35w {long_col:>4}"
            f"   {'lowercase' if style.lowercase else 'capitalised'}"
        )
        lines.append(
            f"           observed: median {style.observed_median}, "
            f"<10w {_pct(style.observed_short_share)}, "
            f">35w {_pct(style.observed_long_share)} over {style.replies} replies"
        )

    lines.append(_bar("Plumbing"))
    iso = report.isolation
    lines.append(
        f"  cross-user isolation   {'OK' if iso.clean else 'VIOLATED'} "
        f"({iso.checks_run} scope checks)"
    )
    for violation in iso.violations:
        lines.append(f"      ! {violation}")
    leak = report.leakage
    lines.append(
        f"  ground-truth leakage   {'OK' if leak.clean else 'LEAKED'} "
        f"({leak.rows_scanned} judgment_log rows scanned for "
        f"{', '.join(ANSWER_KEY_FIELDS)})"
    )
    for entry in leak.leaks:
        lines.append(f"      ! {entry}")
    lines.append(
        "  unexpected judgments   "
        + (
            "none"
            if not report.unexpected_judgment_calls
            else ", ".join(report.unexpected_judgment_calls)
        )
    )
    lines.append(
        "  round errors           "
        + ("none" if not report.round_errors else str(len(report.round_errors)))
    )
    for err in report.round_errors[:10]:
        lines.append(f"      ! {err}")
    if report.contract_notes:
        lines.append("  contract notes")
        for note in report.contract_notes:
            lines.append(f"      - {note}")

    lines.append(_bar("Judgment quality"))
    if not report.live:
        lines.append(f"  materiality precision / recall / F1 ....... {REFUSAL}")
        lines.append(f"  materiality by significance band .......... {REFUSAL}")
        lines.append(f"  evidence-extraction accuracy ............. {REFUSAL}")
        lines.append("\n  Run with --live to measure these against the real models.")
    else:
        q = report.quality
        m = q.materiality
        lines.append("  Materiality (judgment #1)")
        lines.append(
            f"    TP {m.true_positives}  FP {m.false_positives}  "
            f"FN {m.false_negatives}  TN {m.true_negatives}"
        )
        lines.append(f"    precision {m.precision}   recall {m.recall}   F1 {m.f1}")
        for band, bucket in m.by_significance.items():
            lines.append(
                f"    {band:<14} {bucket['agreed']}/{bucket['n']} agreed "
                f"({bucket['agreement']})"
            )
        for example in m.false_negative_examples:
            lines.append(f"    missed: {example}")
        for example in m.false_positive_examples:
            lines.append(f"    spurious: {example}")

        e = q.evidence
        lines.append("\n  Concept evidence (judgment #6)")
        lines.append(
            f"    terms the persona used correctly and we caught: "
            f"{e.mentions_caught}/{e.mentions_total} ({_pct(e.mention_recall)})"
        )
        lines.append(
            f"    terms the persona asked about and we caught:    "
            f"{e.asks_caught}/{e.asks_total} ({_pct(e.ask_recall)})"
        )
        lines.append(f"    read as understood without warrant:  {e.spurious_understood}")
        lines.append(
            f"    terms they LEANED ON, read back as a gap:      "
            f"{e.presupposed_read_as_asked}/{e.presupposed_total} "
            f"({_pct(e.presupposition_confusion)})"
        )
        lines.append(
            "      A question presupposes terms as often as it asks about them.\n"
            "      Reading every term inside a question mark as a gap makes the\n"
            "      system re-explain to someone who just demonstrated they know it."
        )
        for example in e.examples:
            lines.append(f"      - {example}")

    if report.interrupt:
        lines.append(_bar("Interrupt-timing cases (judgment #4, multi-group)"))
        lines.append(f"  {report.interrupt.get('summary', '')}")
        for line in report.interrupt.get("lines", []):
            lines.append(f"  {line}")

    lines.append(_bar("Cost and latency (this run)"))
    for cost in report.costs:
        lines.append(
            f"  {cost.point:<20} {cost.calls:>4} calls  "
            f"{cost.input_tokens:>7} in  {cost.output_tokens:>7} out  "
            f"{cost.mean_latency_ms:>8.1f} ms mean  {cost.errors} errors"
        )
    if not report.live:
        lines.append("  (offline: token counts are zero by construction)")

    lines.append("")
    lines.append("=" * 74)
    verdict = "PASS" if report.passed else "FAIL"
    lines.append(f"RESULT: {verdict}")
    lines.append("=" * 74)
    return "\n".join(lines)


def render_thread(result: PersonaResult, round_index: int | None = None) -> str:
    """One thread verbatim -- briefing, questions, answers -- for reading.

    The report is a wall of aggregates and the thread is the thing the
    aggregates are about. `--show-thread` prints this so a human can see the
    interaction shape rather than infer it from a follow-up rate.
    """
    lines: list[str] = []
    threads = [
        t
        for t in result.threads
        if round_index is None or t.round_index == round_index
    ]
    for thread in threads:
        lines.append("=" * 74)
        lines.append(
            f"{result.persona_id}  --  round {thread.round_index}  --  "
            f"{result.group_name}"
        )
        lines.append("=" * 74)
        lines.append("")
        lines.append("SYSTEM (briefing):")
        for para in (thread.briefing or "").split("\n"):
            lines.append(f"  {para}")
        if thread.explained_terms:
            lines.append(f"  [glossed: {', '.join(thread.explained_terms)}]")
        lines.append("")
        if thread.reading:
            rd = thread.reading
            lines.append(
                f"[reading: {rd.profile} profile, {rd.briefing_chars} chars, "
                f"{rd.dwell_ms} ms, {rd.scroll_fraction:.2f} scrolled "
                f"-> {rd.system_quality!r} ({rd.source})]"
            )
            lines.append("")
        if not thread.user_turn_count:
            lines.append("(no user turns -- they read it and said nothing)")
            lines.append("")
        for turn in thread.turns:
            label = "USER" if turn.speaker == "user" else "SYSTEM"
            if turn.speaker == "user":
                label = f"USER ({turn.form}, tier {turn.tier})"
            lines.append(f"{label}:")
            for para in (turn.text or "").split("\n"):
                lines.append(f"  {para}")
            lines.append("")
        lines.append(
            f"[thread closed: understood={list(thread.understood)} "
            f"asked_about={list(thread.asked_about)} "
            f"not_understood={list(thread.not_understood)} "
            f"band {thread.proficiency_before!r} -> {thread.proficiency_after!r}]"
        )
        lines.append("")
    return "\n".join(lines)


# Phrasings that assert the concept is a number. Applying one to a concept that
# is not -- "that's higher than the relegation I remember" -- produces a
# sentence nobody would say, and the harness then feeds it to the extractor as
# if it were realistic input.
#
# This exists because `question_faithfulness` did not catch it: that gate scores
# whether a question is right about the persona's concept sets, never whether
# the sentence means anything. Scoring the instruction rather than the artifact
# is a mistake this project has now made three times.
_QUANTITY_PHRASINGS = (
    # "figure" as a noun only: "trying to figure out" is a purpose clause the
    # rebuilt responders use, and the bare word flagged it on non-quantities.
    "the figure",
    "that figure",
    "a figure",
    "figure is",
    "higher than",
    "lower than",
    "number is the same story",
    "number the same story",
    "how far below normal",
    "at that level",
    "high or low",
    "compare to normal",
    "comes in lower",
    "comes in higher",
)


def quantity_phrasing_check(
    results: list[PersonaResult], personas: list[Any]
) -> list[str]:
    """Every quantity phrasing must be attached to a declared quantity.

    Runs over the user turns the run actually produced: a turn that carries a
    quantity phrasing must name only terms the persona's fixture declares in
    `quantities`. Gated in `Report.passed` and in `verify_offline`.
    """
    by_id = {p.id: p for p in personas}
    offences: list[str] = []
    for result in results:
        persona = by_id.get(result.persona_id)
        quantities = {q.lower() for q in (persona.quantities if persona else ())}
        for thread in result.threads:
            for turn in thread.user_turns:
                low = (turn.text or "").lower()
                if not any(phrase in low for phrase in _QUANTITY_PHRASINGS):
                    continue
                named = set(turn.asks_about) | set(turn.presupposes) | set(turn.mentions)
                for term in sorted(named):
                    if term.lower() not in quantities:
                        offences.append(
                            f"{result.persona_id} r{thread.round_index}: quantity "
                            f"phrasing applied to non-quantity {term!r} -- {turn.text!r}"
                        )
    return offences
