"""The reward for a briefing, with no reference to the teacher's text.

Stage 3's rule: Claude writes no more targets. A briefing is scored on what
can be measured about it for the reader the packet describes, and Claude may
only ever judge between two student briefings (see `rank_pairs.claude_judge`).
The components, all computed by code:

1. **Hard rules** (`generate_sft.run_checks`). A briefing that breaks one
   scores zero overall. Everything else is conditional on passing.
2. **Reader learning**, from the harness's memory model. The reader is seeded
   in the packet's state: ledger-known terms held, ledger-unknown terms not
   held, and every other term the briefing uses held with probability equal
   to the reader's familiarity with that term's subdomain (beginner 0.1,
   developing 0.4, conversant 0.7, fluent 0.9). Each defined term is a glossed
   encounter at `read` quality. Gain is the expected number of terms the
   reader can define a day later that they could not before, averaged over
   draws, divided by the number of terms the reader lacked, so it is a
   coverage in 0..1. The terms are a universe shared by every briefing being
   compared for the packet (see `event_vocabulary`), so a briefing cannot
   score well by naming fewer terms, and a defined term only counts if the
   briefing text contains it. Defining a term the draw seeded as held costs: that is
   the expert being talked down to, which no rule catches because the term
   was not in the ledger.
3. **Fit**, from readability and length targets by band. Flesch-Kincaid
   grade: beginner 7-10, developing 8-12, conversant 9-13, fluent 9-14,
   losing 0.15 per grade outside the range. Length: 50-140 words, losing
   0.01 per word outside. The targets come from plain-language guidance and
   the reading-speed research the harness already cites, not from the
   teacher's averages.

Composite = 0 if any rule fails, else 0.6 * learning + 0.4 * fit, where
learning = coverage - 0.5 * overgloss_fraction, clipped to 0..1.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from conversational_agent import config
from harness.learning import LearningSpec, MemoryModel
from harness.plainness import plainness
from training.generate_sft import normalize_term, run_checks

BAND_WEIGHT = {"beginner": 0.1, "developing": 0.4, "conversant": 0.7, "fluent": 0.9}
GRADE_RANGE = {"beginner": (7.0, 10.0), "developing": (8.0, 12.0), "conversant": (9.0, 13.0), "fluent": (9.0, 14.0)}
WORDS_RANGE = (50, 140)
DRAWS = 24
HORIZON = timedelta(days=1)
LEARNING_WEIGHT = 0.6
FIT_WEIGHT = 0.4
OVERGLOSS_PENALTY = 0.5


@dataclass
class Score:
    rules_passed: bool
    rule_failures: list[str]
    coverage: float          # 0..1: lacking terms the reader can now define
    overgloss: float         # 0..1: share of defined terms the reader probably held
    learning: float          # coverage - penalty * overgloss, clipped
    fit: float               # 0..1
    composite: float         # 0 if rules failed
    details: dict[str, Any] = field(default_factory=dict)


# --- Reading the packet ---------------------------------------------------------


def _terms(output: dict[str, Any] | None, key: str) -> list[str]:
    seen: list[str] = []
    for t in (output or {}).get(key) or []:
        if isinstance(t, str) and t.strip():
            n = normalize_term(t)
            if n not in seen:
                seen.append(n)
    return seen


def _labels(output: dict[str, Any] | None) -> dict[str, str]:
    """term -> subdomain label, from the briefing's own `subdomains` list."""
    out: dict[str, str] = {}
    for item in (output or {}).get("subdomains") or []:
        if isinstance(item, dict) and item.get("term") and item.get("subdomain"):
            out[normalize_term(str(item["term"]))] = str(item["subdomain"]).strip().lower()
    return out


def reader_state(packet: dict[str, Any]) -> tuple[set[str], set[str], str, dict[str, float]]:
    """(known, unknown, band, subdomain weights) as the packet describes them."""
    ledger = packet.get("concept_ledger") or {}
    known = {normalize_term(t) for state in config.KNOWN_STATES for t in ledger.get(state, [])}
    unknown = {normalize_term(t) for t in ledger.get(config.CONCEPT_UNKNOWN, [])} - known
    band = str(packet.get("proficiency") or "beginner")
    weights = {
        str(name).strip().lower(): BAND_WEIGHT.get(str((info or {}).get("band")), BAND_WEIGHT.get(band, 0.4))
        for name, info in (packet.get("subdomain_familiarity") or {}).items()
    }
    return known, unknown, band, weights


def hold_probability(term: str, labels: dict[str, str], weights: dict[str, float], band: str) -> float:
    """How likely the reader already holds a term the ledger does not mention."""
    label = labels.get(term)
    if label is not None and label in weights:
        return weights[label]
    return BAND_WEIGHT.get(band, 0.4)


# --- Components -------------------------------------------------------------------


def term_in_text(term: str, text: str) -> bool:
    """A defined term counts only if the briefing actually contains it."""
    t = normalize_term(term)
    body = normalize_term(text)
    return bool(t) and t in body


def learning_components(
    packet: dict[str, Any],
    output: dict[str, Any] | None,
    *,
    draws: int = DRAWS,
    seed: str = "reward",
    universe_terms: list[str] | None = None,
) -> tuple[float, float, dict[str, Any]]:
    """(coverage, overgloss fraction, details) for one briefing.

    `universe_terms` is the set of domain terms the reader might need for this
    event, shared by every briefing being compared for the same packet. Without
    it a briefing could shrink its own `terms_used` list and claim full
    coverage of nothing; with it, coverage is measured over the same terms for
    every candidate. Callers build it as the union of terms across candidates
    plus the ledger's unknown terms that the event text mentions.
    """
    known, unknown, band, weights = reader_state(packet)
    briefing_text = str((output or {}).get("briefing") or "")
    explained = [t for t in _terms(output, "explained_terms") if term_in_text(t, briefing_text)]
    own_used = list(dict.fromkeys(_terms(output, "terms_used") + explained))
    used = list(dict.fromkeys([normalize_term(t) for t in (universe_terms or [])] + own_used))
    labels = _labels(output)
    if not used:
        return 0.0, 0.0, {"used": 0, "lacking": 0}
    at = datetime(2026, 9, 1, tzinfo=UTC)
    spec = LearningSpec(prior_knowledge=BAND_WEIGHT.get(band, 0.4), memory_rate=1.0)
    universe = sorted(known | unknown | set(used))
    event_key = str((packet.get("events") or [{}])[0].get("headline") or "event")
    coverages: list[float] = []
    overglosses: list[float] = []
    had_lacking: list[bool] = []
    for draw in range(draws):
        rng = random.Random(f"{seed}:{draw}")
        held = set(known)
        for term in used:
            if term in known or term in unknown:
                continue
            if rng.random() < hold_probability(term, labels, weights, band):
                held.add(term)
        lacking = [t for t in used if t not in held]
        model = MemoryModel.seeded(
            learning=spec, seed=f"{seed}:{draw}", persona_id="reader", universe=universe,
            knows=held, does_not_know=[t for t in used if t not in held], at=at,
        )
        before = sum(model.ladder(t, at + HORIZON)[1] for t in lacking)
        for term in explained:
            model.glossed(term, "read", at, context_id=event_key)
        after = sum(model.ladder(t, at + HORIZON)[1] for t in lacking)
        coverages.append((after - before) / len(lacking) if lacking else 1.0)
        had_lacking.append(bool(lacking))
        overglosses.append(sum(1 for t in explained if t in held) / len(explained) if explained else 0.0)
    coverage = max(0.0, min(1.0, statistics.mean(coverages)))
    overgloss = statistics.mean(overglosses)
    return coverage, overgloss, {"used": len(used), "explained": len(explained),
                                 "candidates_lacking": len([t for t in used if t not in known]),
                                 # Share of draws in which the reader lacked something: when 0,
                                 # coverage is 1.0 by definition (nothing to teach, nothing missed).
                                 "share_with_lacking": round(sum(had_lacking) / len(had_lacking), 2)}


def fit_score(packet: dict[str, Any], output: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    band = str(packet.get("proficiency") or "beginner")
    briefing = str((output or {}).get("briefing") or "")
    p = plainness(briefing)
    low, high = GRADE_RANGE.get(band, GRADE_RANGE["developing"])
    grade = float(p["fk_grade"])
    grade_fit = 1.0 if low <= grade <= high else max(0.0, 1.0 - 0.15 * (low - grade if grade < low else grade - high))
    words = int(p["words"])
    lo_w, hi_w = WORDS_RANGE
    words_fit = 1.0 if lo_w <= words <= hi_w else max(0.0, 1.0 - 0.01 * (lo_w - words if words < lo_w else words - hi_w))
    return round(grade_fit * words_fit, 3), {"grade": grade, "words": words, "band": band}


def event_vocabulary(packet: dict[str, Any], outputs: list[dict[str, Any] | None]) -> list[str]:
    """The shared term universe for one packet: every term any compared
    briefing used or defined, plus the ledger's unknown terms that appear in
    the event text."""
    event = (packet.get("events") or [{}])[0]
    event_text = normalize_term(f"{event.get('headline') or ''} {event.get('detail') or ''}")
    _, unknown, _, _ = reader_state(packet)
    terms: list[str] = [t for t in sorted(unknown) if t and t in event_text]
    for out in outputs:
        for t in _terms(out, "terms_used") + _terms(out, "explained_terms"):
            if t not in terms:
                terms.append(t)
    return terms


def score_briefing(
    packet: dict[str, Any],
    output: dict[str, Any] | None,
    other_events: list[str],
    *,
    draws: int = DRAWS,
    universe_terms: list[str] | None = None,
) -> Score:
    event = (packet.get("events") or [{}])[0]
    own = f"{event.get('headline') or ''} {event.get('detail') or ''}"
    checks = run_checks(packet, output, [o for o in other_events if o != own])
    failures = [k for k, v in checks.items() if k not in ("details", "passed") and not v]
    if output is None:
        failures = ["not valid JSON", *failures]
    passed = output is not None and bool(checks["passed"])
    coverage, overgloss, ldetails = learning_components(packet, output, draws=draws, universe_terms=universe_terms)
    learning = max(0.0, min(1.0, coverage - OVERGLOSS_PENALTY * overgloss))
    fit, fdetails = fit_score(packet, output)
    composite = 0.0 if not passed else round(LEARNING_WEIGHT * learning + FIT_WEIGHT * fit, 4)
    return Score(
        rules_passed=passed, rule_failures=failures, coverage=round(coverage, 3), overgloss=round(overgloss, 3),
        learning=round(learning, 3), fit=fit, composite=composite,
        details={**ldetails, **fdetails, "checks": {k: v for k, v in checks.items() if k != "details"}},
    )
