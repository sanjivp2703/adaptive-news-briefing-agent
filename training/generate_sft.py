"""Generate briefing SFT data by asking the teacher to brief synthetic users.

`extract_sft.py` harvests briefings the system actually wrote in live runs;
this script makes more of them on purpose. It takes every verified real event
in the persona fixtures, invents a few hundred plausible *user states* (what
the ledger would hold for a beginner, a partial learner, an expert, an expert
in one corner of the group, a returning reader who has read many glosses),
assembles the exact packet `Assessor.raise_topic` would hand to
`judgments.write_briefing`, and sends it through the project's real `Judge`
so the production prompt, schema, effort and tracing all apply. Each answer
is written in `extract_sft.py`'s row format, so the two files concatenate.

Usage (from the project root; the live run needs ANTHROPIC_API_KEY in the environment):

    PYTHONPATH=src:. .venv/bin/python training/generate_sft.py --dry-run
    PYTHONPATH=src:. .venv/bin/python training/generate_sft.py --offline
    PYTHONPATH=src:. .venv/bin/python training/generate_sft.py

Writes `training/data/sft_generated.jsonl` (every row, flagged) and
`training/data/sft_generated.clean.jsonl` (the rows that pass every hard
check). Resumable: a packet whose id is already in the output is skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from conversational_agent import config, judgments
from conversational_agent.judgment import (
    AnthropicClient,
    Judge,
    JudgmentError,
    RawCompletion,
    StubClient,
    load_prompt,
)

# Reporting only: the plainness bundle (grade level, sentence length,
# domain-term density, asides, definitions per 100 words) that the harness
# reads depth from since `briefing` v7. A pure text module with no harness
# state behind it; the packet synthesis and the teacher call do not use it.
from harness.plainness import BUNDLE_FIELDS, plainness, summarise

PERSONA_DIR = PROJECT_ROOT / "harness" / "personas"
DEFAULT_OUT = HERE / "data" / "sft_generated.jsonl"
DEFAULT_DB = HERE / "data" / "generated.db"

PROFILES = (
    "beginner",
    "partial",
    "expert",
    "expert_one_subdomain",
    "returning_reader",
)

# Planning figures for the cost estimate, from the eight real v5/v6 examples
# in `sft_briefings.clean_nogate.jsonl` (rounded up: v6 packets carry
# `subdomain_familiarity`, which the older rows did not). The running meter
# uses the real token counts once calls are made.
EST_INPUT_TOKENS = 900
EST_OUTPUT_TOKENS = 770
PRICES_PER_MTOK = config.PRICES_PER_MTOK
TEACHERS = {"sonnet": config.SONNET, "opus": config.OPUS}

# A ledger bucket in the packet is capped exactly like production's.
LEDGER_TERMS_PER_STATE = config.LEDGER_TERMS_PER_STATE
FALLBACK_OCCURRED_AT = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
GOAL_TEMPLATES = (
    "Dinner with friends who follow {group} closely",
    "Catch-up call with my brother, who talks about {group} nonstop",
    "Work drinks where a colleague always brings up {group}",
    "Weekend with my partner's family, who are all {group} people",
    "Podcast recording where {group} is on the agenda",
)


# --- Fixtures ----------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One persona's view of the group an event belongs to."""

    persona_id: str
    source_file: str
    group_name: str
    description: str
    taxonomy: dict[str, tuple[str, ...]]  # normalised label -> normalised terms
    vocabulary: tuple[str, ...]


@dataclass
class EventFixture:
    key: str
    headline: str
    detail: str
    source_name: str | None
    source_url: str | None
    occurred_at: str
    concepts: tuple[str, ...]
    family: str
    variants: list[Variant] = field(default_factory=list)

    @property
    def event_id(self) -> str:
        return "evt_" + self.key[:12]


def normalize_term(term: str) -> str:
    """Mirror of `store.normalize_term`: lowercase, de-hyphenate, collapse."""
    cleaned = str(term).strip().lower().replace("-", " ").replace("_", " ")
    return " ".join(cleaned.split())


def normalize_label(label: str) -> str:
    return normalize_term(str(label).strip().strip("`'\""))


_SUFFIX = re.compile(r"\s*\([A-Z][a-z]+\)\s*$")
_URL_DATE = re.compile(r"/((?:19|20)\d\d)/(\d\d)/(\d\d)/")


def canonical_group_name(name: str) -> str:
    """`Crypto (Farida)` -> `Crypto`. The suffix is a fixture-loader artefact
    (group names must be unique per suite); no real user names a group so."""
    return _SUFFIX.sub("", name).strip()


def _event_key(headline: str) -> str:
    return hashlib.sha1(headline.strip().encode("utf-8")).hexdigest()


def _occurred_at(raw: dict[str, Any], dated: dict[str, str]) -> str:
    key = _event_key(raw["headline"])
    if key in dated:
        return dated[key]
    if isinstance(raw.get("occurred_at"), str) and raw["occurred_at"]:
        return _iso_day(raw["occurred_at"])
    m = _URL_DATE.search(raw.get("source_url") or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T09:00:00+00:00"
    hours = float(raw.get("occurred_hours_ago", 2.0))
    return (FALLBACK_OCCURRED_AT - timedelta(hours=hours)).isoformat()


def _iso_day(value: str) -> str:
    return value if "T" in value else f"{value}T09:00:00+00:00"


def load_fixtures(persona_dir: Path = PERSONA_DIR) -> list[EventFixture]:
    """Every verified event, once per headline, with every persona's taxonomy
    for its group attached as a variant. Deterministic order (sorted files)."""
    files = sorted(persona_dir.glob("*.json")) + sorted(
        p for p in (persona_dir / "generated").glob("*.json") if p.name[0].isdigit()
    )
    dated: dict[str, str] = {}
    for path in sorted((persona_dir / "generated").glob("events_*.json")):
        for ev in json.loads(path.read_text(encoding="utf-8")).get("events", []):
            if ev.get("occurred_at"):
                dated[_event_key(ev["headline"])] = _iso_day(ev["occurred_at"])

    events: dict[str, EventFixture] = {}
    for path in files:
        persona = json.loads(path.read_text(encoding="utf-8"))
        group = persona.get("group") or {}
        taxonomy = {
            normalize_label(name): tuple(
                dict.fromkeys(normalize_term(t) for t in terms if normalize_term(t))
            )
            for name, terms in (group.get("subdomains") or {}).items()
        }
        variant = Variant(
            persona_id=persona.get("id", path.stem),
            source_file=str(path.relative_to(PROJECT_ROOT)),
            group_name=canonical_group_name(group.get("name", path.stem)),
            description=group.get("description", ""),
            taxonomy=taxonomy,
            vocabulary=tuple(normalize_term(t) for t in group.get("vocabulary", [])),
        )
        for raw in persona.get("events", []):
            key = _event_key(raw["headline"])
            fixture = events.get(key)
            if fixture is None:
                fixture = EventFixture(
                    key=key,
                    headline=raw["headline"].strip(),
                    detail=(raw.get("detail") or "").strip(),
                    source_name=raw.get("source_name"),
                    source_url=raw.get("source_url"),
                    occurred_at=_occurred_at(raw, dated),
                    concepts=tuple(normalize_term(c) for c in raw.get("concepts", [])),
                    family=variant.group_name,
                )
                events[key] = fixture
            fixture.variants.append(variant)
    return list(events.values())


# --- The ledger arithmetic, mirrored from store.py --------------------------
# These are the store's `_ATTESTED_PREDICATE` / `_known_predicate` rewritten
# over dicts. tests/test_generate_sft.py checks them against a real Store.


def is_attested(row: dict[str, Any]) -> bool:
    return (
        row.get("explained_at") is not None
        or int(row.get("correct_uses", 0)) > 0
        or int(row.get("misunderstandings", 0)) > 0
        or int(row.get("read_explanations", 0)) >= config.READ_EXPLANATIONS_BEFORE_BAND
    )


def is_known(row: dict[str, Any]) -> bool:
    state = row.get("state")
    if state == config.CONCEPT_CONFIRMED:
        return True
    return (
        state in config.BAND_STATES
        and int(row.get("read_explanations", 0)) >= config.READ_EXPLANATIONS_BEFORE_BAND
    )


def proficiency_from_rows(rows: list[dict[str, Any]]) -> str:
    known = sum(1 for r in rows if is_known(r))
    attested = sum(1 for r in rows if is_attested(r))
    return config.proficiency_band(known, attested)


def familiarity_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    counts: dict[str, list[int]] = {}
    for r in rows:
        label = r.get("subdomain") or config.UNLABELLED_SUBDOMAIN
        bucket = counts.setdefault(label, [0, 0])
        bucket[0] += 1 if is_known(r) else 0
        bucket[1] += 1 if is_attested(r) else 0
    return {
        label: {"known": k, "attested": a, "band": config.subdomain_band(k, a)}
        for label, (k, a) in sorted(counts.items())
    }


def ledger_summary(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Terms grouped by state, capped per bucket, in the order given (the
    caller has already put them most-recently-seen first)."""
    buckets: dict[str, list[str]] = {state: [] for state in config.CONCEPT_STATES}
    for r in rows:
        bucket = buckets.setdefault(r["state"], [])
        if len(bucket) < LEDGER_TERMS_PER_STATE:
            bucket.append(r["term"])
    return buckets


# --- Synthesising one user state ---------------------------------------------

_U, _P, _F, _E, _C = (
    config.CONCEPT_UNKNOWN,
    config.CONCEPT_PROVISIONAL,
    config.CONCEPT_FAMILIAR,
    config.CONCEPT_EXPLAINED,
    config.CONCEPT_CONFIRMED,
)

# Per profile: how likely a vocabulary term is in the ledger at all, how the
# states are distributed over the ones that are, and how many explanations a
# familiar/explained term has been read (2+ is what moves the band).
_RULES: dict[str, dict[str, Any]] = {
    "beginner": {"include": 0.35, "empty": 0.3, "re": (1, 1),
                 "weights": {_U: 0.8, _F: 0.15, _P: 0.05}},
    "partial": {"include": 0.7, "empty": 0.0, "re": (1, 3),
                "weights": {_U: 0.35, _P: 0.1, _F: 0.25, _E: 0.15, _C: 0.15}},
    "expert": {"include": 0.95, "empty": 0.0, "re": (2, 4),
               "weights": {_U: 0.06, _P: 0.06, _F: 0.14, _E: 0.14, _C: 0.6}},
    "returning_reader": {"include": 0.85, "empty": 0.0, "re": (1, 4),
                         "weights": {_F: 0.55, _E: 0.15, _U: 0.15, _P: 0.1, _C: 0.05}},
}
_FOCUS = {"include": 0.95, "re": (2, 3), "weights": {_C: 0.6, _E: 0.25, _F: 0.15}}
_OFF_FOCUS = {"include": 0.4, "re": (1, 1), "weights": {_U: 0.7, _F: 0.2, _P: 0.1}}
_STAMP = "2026-08-01T00:00:00+00:00"


def _draw_state(rng: random.Random, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _row(rng: random.Random, term: str, state: str, label: str | None, re_range: tuple[int, int]) -> dict[str, Any]:
    """One concept row whose counters are consistent with how the store's
    mark_* methods reach that state (see store.py)."""
    lo, hi = re_range
    row = {"term": term, "state": state, "correct_uses": 0, "read_explanations": 0,
           "misunderstandings": 0, "explained_at": None, "subdomain": label}
    if state == _U:
        row["misunderstandings"] = 1 if rng.random() < 0.15 else 0
    elif state == _P:
        row["correct_uses"] = 1
        row["read_explanations"] = rng.choice([0, 0, 0, 1])
    elif state == _F:
        row["correct_uses"] = rng.choice([0, 0, 1])
        row["read_explanations"] = max(1, rng.randint(lo, hi))
    elif state == _E:
        row["explained_at"] = _STAMP
        row["correct_uses"] = rng.choice([0, 0, 1])
        row["read_explanations"] = max(1, rng.randint(lo, hi))
    elif state == _C:
        row["correct_uses"] = rng.randint(2, 4)
        row["read_explanations"] = rng.randint(0, 2)
        row["explained_at"] = _STAMP if rng.random() < 0.3 else None
    return row


def _term_pool(variant: Variant, siblings: list[Variant], *, widen: bool) -> list[tuple[str, str]]:
    """(term, label) pairs; a term appears once. `widen` adds sibling personas'
    taxonomies for the same group, so an expert ledger can be as large as a
    real long-lived one (the fluent floor is 25 known terms)."""
    seen: dict[str, str] = {}
    for label, terms in variant.taxonomy.items():
        for t in terms:
            seen.setdefault(t, label)
    if widen:
        for sib in siblings:
            for label, terms in sib.taxonomy.items():
                for t in terms:
                    seen.setdefault(t, label)
    return list(seen.items())


def _reading_pattern(rng: random.Random, profile: str) -> dict[str, Any]:
    p_resolved = 0.6 if profile == "returning_reader" else 0.15
    informed = lazy = 0
    unresolved = rng.choice([0, 0, 0, 1, 2])
    if rng.random() < p_resolved:
        resolved = rng.randint(3, 8)
        high = rng.random() < 0.5
        share = rng.uniform(0.75, 1.0) if high else rng.uniform(0.0, 0.25)
        informed = round(resolved * share)
        lazy = resolved - informed
    return {
        "informed_skips": informed,
        "lazy_skips": lazy,
        "unresolved_skips": unresolved,
        "resolved": informed + lazy,
        "p_informed": round((informed + 1) / (informed + lazy + 2), 3),
    }


def topic_label(headline: str) -> str:
    head = re.split(r"[,:;—–]\s|\s-\s", headline)[0]
    words = re.findall(r"[A-Za-z0-9'’$%.-]+", head)[:4]
    return " ".join(w.strip(".") for w in words)


def synthesise_state(
    event: EventFixture,
    variant: Variant,
    siblings: list[Variant],
    others: list[EventFixture],
    profile: str,
    rng: random.Random,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One packet (exactly the keys `write_briefing` builds) and its meta."""
    focus: str | None = None
    if profile == "expert_one_subdomain":
        big = [l for l, t in variant.taxonomy.items() if len(t) >= config.SUBDOMAIN_CONVERSANT_MIN_KNOWN]
        focus = rng.choice(sorted(big) if big else sorted(variant.taxonomy))
    pool = _term_pool(variant, siblings, widen=(profile == "expert"))
    rng.shuffle(pool)

    rows: list[dict[str, Any]] = []
    rules = _RULES.get(profile)
    if rules is None or rng.random() >= rules["empty"]:
        for term, label in pool:
            rule = rules if rules else (_FOCUS if label == focus else _OFF_FOCUS)
            if rng.random() >= rule["include"]:
                continue
            state = _draw_state(rng, rule["weights"])
            # Most rows carry the label the briefing gave the term; a few are
            # older rows from before labels existed, which production reports
            # under `(unlabelled)`.
            shown = label if rng.random() < 0.9 else None
            rows.append(_row(rng, term, state, shown, rule["re"]))
    rng.shuffle(rows)  # "most recently seen first" is arbitrary here

    n_prev = rng.choices([0, 1, 2, 3], weights=[0.1, 0.2, 0.3, 0.4] if profile == "returning_reader" else [0.35, 0.3, 0.2, 0.15])[0]
    prev = [{"topic": topic_label(o.headline)} for o in rng.sample(others, min(n_prev, len(others)))]

    goal = None
    if rng.random() < 0.3:
        day = datetime.fromisoformat(event.occurred_at) + timedelta(days=rng.randint(2, 21))
        goal = {
            "description": rng.choice(GOAL_TEMPLATES).format(group=variant.group_name),
            "deadline": day.date().isoformat(),
        }

    packet = {
        "group": {"name": variant.group_name, "description": variant.description},
        "concept_ledger": ledger_summary(rows),
        "proficiency": proficiency_from_rows(rows),
        "subdomain_familiarity": familiarity_from_rows(rows),
        "events": [
            {
                "id": event.event_id,
                "headline": event.headline,
                "detail": event.detail,
                "occurred_at": event.occurred_at,
                "source_name": event.source_name,
                "source_url": event.source_url,
            }
        ],
        "previously_raised": prev,
        "active_goal": goal,
        "events_not_yet_seen": rng.choices([0, 1, 2, 3, 4], weights=[0.45, 0.25, 0.15, 0.1, 0.05])[0],
        "reading_pattern": _reading_pattern(rng, profile),
    }
    meta = {
        "profile": profile,
        "persona_source": variant.source_file,
        "persona_id": variant.persona_id,
        "group_family": event.family,
        "focus_subdomain": focus,
        "ledger_rows": len(rows),
        "known": sum(1 for r in rows if is_known(r)),
        "attested": sum(1 for r in rows if is_attested(r)),
        "labelled": sum(1 for r in rows if r["subdomain"]),
        "pool_terms": len(pool),
    }
    return packet, meta


def build_packets(
    fixtures: list[EventFixture], *, seed: int, states_per_event: int, limit: int
) -> list[dict[str, Any]]:
    """All packets, round-robin over events (state 0 for every event, then
    state 1, ...) so a `limit` keeps every event and every profile in play.
    Everything is drawn from `seed`, the event and the state index only."""
    by_family: dict[str, list[EventFixture]] = {}
    for ev in fixtures:
        by_family.setdefault(ev.family, []).append(ev)
    out: list[dict[str, Any]] = []
    for k in range(states_per_event):
        for i, ev in enumerate(fixtures):
            if len(out) >= limit:
                return out
            variant = ev.variants[k % len(ev.variants)]
            siblings = [v for v in ev.variants if v is not variant]
            others = [o for o in by_family[ev.family] if o.key != ev.key]
            profile = PROFILES[(k + i) % len(PROFILES)]
            rng = random.Random(f"{seed}:{ev.key}:{k}")
            packet, meta = synthesise_state(ev, variant, siblings, others, profile, rng)
            meta.update({"seed": seed, "event_key": ev.key, "state_index": k})
            out.append({"id": f"sftgen_{seed}_{ev.key[:8]}_{k:02d}", "packet": packet, "meta": meta})
    return out


# --- Coverage ----------------------------------------------------------------


def coverage(items: list[dict[str, Any]]) -> dict[str, Any]:
    deep = 0
    for it in items:
        bands = {v["band"] for v in it["packet"]["subdomain_familiarity"].values()}
        if bands & {config.CONVERSANT, config.FLUENT}:
            deep += 1
    return {
        "packets": len(items),
        "events": len({it["meta"]["event_key"] for it in items}),
        "per_group": Counter(it["packet"]["group"]["name"] for it in items),
        "per_profile": Counter(it["meta"]["profile"] for it in items),
        "per_band": Counter(it["packet"]["proficiency"] for it in items),
        "with_deep_subdomain": deep,
        "with_reading_prior": sum(1 for it in items if it["packet"]["reading_pattern"]["resolved"] >= 3),
        "with_goal": sum(1 for it in items if it["packet"]["active_goal"]),
        "empty_ledger": sum(1 for it in items if it["meta"]["ledger_rows"] == 0),
    }


def print_coverage(cov: dict[str, Any]) -> None:
    print(f"\nCoverage: {cov['packets']} packets over {cov['events']} events")
    for title, key in (("per group", "per_group"), ("per profile", "per_profile"), ("per proficiency band", "per_band")):
        print(f"  {title}")
        for name, n in sorted(cov[key].items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {name:<32} {n:>4}")
    print(f"  with a conversant/fluent subdomain   {cov['with_deep_subdomain']:>4}  (depth rule exercised)")
    print(f"  with a usable reading_pattern (>=3)  {cov['with_reading_prior']:>4}")
    print(f"  with an active goal                  {cov['with_goal']:>4}")
    print(f"  with an empty ledger                 {cov['empty_ledger']:>4}")


# --- Hard-rule checks (the gate's briefing rules, reimplemented minimally) ---

_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_ORDINAL_TAIL = re.compile(r"^(st|nd|rd|th)\b", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the a an and or of to in on at for with by from as is are was were be been this that "
    "it its into than then over under after before about their his her they them he she who "
    "which what when where while have has had not no but so if up out more most also just "
    "one two three first new said says say will would could should can may might did do does "
    "s t".split()
)


def figure_key(number: str) -> str:
    digits = re.sub(r"[^0-9]", "", number).lstrip("0").rstrip("0")
    return digits or "0"


def numbers_in(text: str) -> list[dict[str, str]]:
    """Every number in `text` except years and ordinals. Stricter than the
    gate (which only checks figures carrying a currency, scale or unit): a
    training row should not teach a bare count the source does not hold."""
    out = []
    for m in _NUMBER.finditer(text or ""):
        num = m.group(0)
        if re.fullmatch(r"(19|20)\d\d", num):
            continue
        if _ORDINAL_TAIL.match(text[m.end(): m.end() + 2]):
            continue
        out.append({"text": num, "key": figure_key(num)})
    return out


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall((text or "").lower()) if t not in _STOP and len(t) > 1}


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def run_checks(packet: dict[str, Any], output: dict[str, Any] | None, other_events: list[str]) -> dict[str, Any]:
    """The hard rules the gate applies to briefings, on one row.

    * no question mark anywhere in the briefing;
    * the briefing refers to the packet's event and no other: its content
      overlap with that event beats its overlap with every other event of
      the group;
    * every number in the briefing (years and ordinals aside) appears in the
      event's headline or detail, compared on significant digits;
    * no term in `explained_terms` is one the ledger already marks known
      (provisional/familiar/explained/confirmed) -- the gloss-once rule;
    * `explained_terms` is a subset of `terms_used`.
    """
    briefing = str((output or {}).get("briefing") or "")
    event = packet["events"][0]
    source = f"{event.get('headline') or ''} {event.get('detail') or ''}"
    own = _overlap(_tokens(briefing), _tokens(source))
    best_other = max((_overlap(_tokens(briefing), _tokens(o)) for o in other_events), default=0.0)
    source_keys = {n["key"] for n in numbers_in(source)}
    unsupported = [n["text"] for n in numbers_in(briefing) if n["key"] not in source_keys]
    known = {
        normalize_term(t)
        for state in config.KNOWN_STATES
        for t in packet["concept_ledger"].get(state, [])
    }
    explained = [normalize_term(t) for t in (output or {}).get("explained_terms") or [] if isinstance(t, str)]
    used = {normalize_term(t) for t in (output or {}).get("terms_used") or [] if isinstance(t, str)}
    reglossed = sorted({t for t in explained if t in known})
    not_in_used = sorted({t for t in explained if t not in used})
    checks = {
        "no_question_mark": "?" not in briefing,
        "references_only_packet_event": bool(briefing) and own > 0 and own > best_other,
        "numbers_supported": not unsupported,
        "no_regloss": not reglossed,
        "explained_subset_of_used": not not_in_used,
        "details": {
            "own_event_overlap": round(own, 3),
            "best_other_event_overlap": round(best_other, 3),
            "unsupported_numbers": unsupported,
            "reglossed_terms": reglossed,
            "explained_not_in_used": not_in_used,
        },
    }
    checks["passed"] = output is not None and all(v for k, v in checks.items() if k != "details")
    return checks


# --- Offline teacher -----------------------------------------------------------


def offline_briefing(context: dict[str, Any]) -> dict[str, Any]:
    """A plausible, rule-abiding briefing built from the packet alone, so the
    whole pipeline (Judge, log, row format, checks) runs with no network."""
    event = context["events"][0]
    detail = event.get("detail") or ""
    first = re.split(r"(?<=[.!])\s+", detail.strip())[0] if detail.strip() else ""
    ledger = context.get("concept_ledger", {})
    unknown = [t for t in ledger.get(config.CONCEPT_UNKNOWN, [])][:2]
    known = [t for s in config.KNOWN_STATES for t in ledger.get(s, [])][:1]
    sentences = [event["headline"].rstrip(".?!") + ".", first]
    for t in unknown:
        sentences.append(f"Here {t} is the piece of vocabulary that matters, meaning the thing this development turns on.")
    if known:
        sentences.append(f"For anyone who follows {known[0]} closely, this is the bigger deal.")
    briefing = " ".join(s for s in sentences if s).replace("?", ".")
    used = unknown + known
    slices = list(context.get("subdomain_familiarity", {})) or ["general"]
    return {
        "briefing": briefing,
        "topic": topic_label(event["headline"]),
        "terms_used": used,
        "explained_terms": unknown,
        "subdomains": [{"term": t, "subdomain": slices[i % len(slices)]} for i, t in enumerate(used)],
        "reasoning": "offline stub: the one untold event, glossing only unknown terms",
    }


class OfflineClient(StubClient):
    """StubClient that also reports rough token counts, so the cost meter and
    the budget stop can be exercised without spending anything."""

    def complete_json(self, **kwargs) -> RawCompletion:  # type: ignore[override]
        raw = super().complete_json(**kwargs)
        raw.input_tokens = max(1, len(kwargs["user_content"]) // 4)
        raw.output_tokens = max(1, len(raw.text) // 4)
        return raw


# --- Generation -----------------------------------------------------------------


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES_PER_MTOK.get(model, PRICES_PER_MTOK[config.SONNET])
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def estimate_cost(model: str, n: int) -> float:
    return n * _cost(model, EST_INPUT_TOKENS, EST_OUTPUT_TOKENS)


def _existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    ids.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return ids


def _norm_terms(raw: Any) -> set[str]:
    if not isinstance(raw, list):
        return set()
    return {t.strip().lower() for t in raw if isinstance(t, str) and t.strip()}


def make_row(
    item: dict[str, Any],
    log_row: Any,
    *,
    source_db: str,
    run_id: str,
    prompt_version: str,
    prompt_text: str,
    other_events: list[str],
    vocabulary: tuple[str, ...] = (),
) -> dict[str, Any]:
    """One output row in `extract_sft.py`'s shape, built from the judgment_log
    row the Judge wrote -- so `input` is the packet exactly as logged.
    `quality.plainness` is additive to that shape: the bundle the harness
    reads depth from (against the variant's group vocabulary)."""
    payload = json.loads(log_row["input_json"])
    verdict = json.loads(log_row["verdict_json"]) if log_row["verdict_json"] and log_row["error"] is None else None
    output = None
    if isinstance(verdict, dict):
        output = {k: verdict[k] for k in ("briefing", "topic", "explained_terms", "terms_used", "subdomains") if k in verdict}
    briefing = (output or {}).get("briefing") if output else None
    explained = _norm_terms((output or {}).get("explained_terms")) if output else set()
    words = len(briefing.split()) if isinstance(briefing, str) else 0
    definitions = len(explained)
    record: dict[str, Any] = {
        "id": item["id"],
        "source_db": source_db,
        "run_id": run_id,
        "user_id": log_row["user_id"],
        "group_id": log_row["group_id"],
        "prompt_version": log_row["prompt_version"],
        "model": log_row["model"],
        "created_at": log_row["created_at"],
        "system_text_available": log_row["prompt_version"] == prompt_version,
    }
    if record["system_text_available"]:
        record["system"] = prompt_text
    record["input"] = payload
    record["output"] = output
    record["quality"] = {
        "gate_run_passed": None,
        "errored": log_row["error"] is not None,
        "error": (log_row["error"][:200] if log_row["error"] else None),
        "question_mark_in_briefing": ("?" in briefing) if isinstance(briefing, str) else False,
        "words": words,
        "definitions": definitions,
        "defs_per_100w": round(100.0 * definitions / words, 2) if words else 0.0,
        "plainness": plainness(briefing if isinstance(briefing, str) else "", vocabulary, sorted(explained)),
        "reask": None,
        "input_tokens": log_row["input_tokens"],
        "output_tokens": log_row["output_tokens"],
        "latency_ms": log_row["latency_ms"],
        "checks": run_checks(payload["context"], output, other_events),
    }
    record["meta"] = dict(item["meta"], log_id=log_row["id"])
    return record


def generate(
    items: list[dict[str, Any]],
    fixtures: list[EventFixture],
    *,
    client: Any,
    db_path: Path,
    out_path: Path,
    run_id: str,
    budget: float,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Call the teacher for every packet not already in `out_path`, appending
    rows as they land. Stops when the next call would cross `budget`."""
    from conversational_agent.store import Store

    prompt = load_prompt(config.BRIEFING)
    model = config.model_for(config.BRIEFING)
    others_by_family: dict[str, dict[str, list[str]]] = {}
    for ev in fixtures:
        others_by_family.setdefault(ev.family, {})[ev.key] = []
    for ev in fixtures:
        for other in fixtures:
            if other.family == ev.family and other.key != ev.key:
                others_by_family[ev.family][ev.key].append(f"{other.headline} {other.detail}")

    vocab_by_variant = {(ev.key, v.persona_id): v.vocabulary for ev in fixtures for v in ev.variants}
    plainness_rows: dict[str, list[dict[str, Any]]] = {}

    done = _existing_ids(out_path)
    todo = [it for it in items if it["id"] not in done]
    log(f"{len(items)} packets; {len(done)} already in {out_path.name}; {len(todo)} to generate")
    per_call = _cost(model, EST_INPUT_TOKENS, EST_OUTPUT_TOKENS)
    log(f"teacher {model}; estimated ${per_call:.4f}/call, ${per_call * len(todo):.2f} for the remainder; budget ${budget:.2f}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    spent = 0.0
    written = errored = failed_checks = 0
    stopped = None
    store = Store(db_path)
    try:
        judge = Judge(client=client, store=store, run_id=run_id)
        with out_path.open("a", encoding="utf-8") as fh:
            for n, item in enumerate(todo, 1):
                if spent + per_call > budget:
                    stopped = f"budget: ${spent:.2f} spent, next call ~${per_call:.4f}, budget ${budget:.2f}"
                    break
                packet = item["packet"]
                meta = item["meta"]
                try:
                    judgments.write_briefing(
                        judge,
                        group=packet["group"],
                        ledger=packet["concept_ledger"],
                        proficiency=packet["proficiency"],
                        subdomain_familiarity=packet["subdomain_familiarity"],
                        events=packet["events"],
                        previously_raised=packet["previously_raised"],
                        active_goal=packet["active_goal"],
                        behind_count=packet["events_not_yet_seen"],
                        reading_pattern=packet["reading_pattern"],
                        user_id=f"sftgen_{meta['profile']}",
                        group_id="grp_" + re.sub(r"[^a-z0-9]+", "_", meta["group_family"].lower()).strip("_"),
                    )
                except JudgmentError:
                    pass  # logged by the Judge; the row records the error
                store.conn.commit()
                log_row = store.conn.execute(
                    "SELECT * FROM judgment_log WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if log_row is None:
                    raise RuntimeError("the Judge logged nothing for this call")
                row = make_row(
                    item,
                    log_row,
                    source_db=db_path.stem,
                    run_id=run_id,
                    prompt_version=prompt.version,
                    prompt_text=prompt.text,
                    other_events=others_by_family[meta["group_family"]][meta["event_key"]],
                    vocabulary=vocab_by_variant.get((meta["event_key"], meta["persona_id"]), ()),
                )
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                written += 1
                if row["output"] and not row["quality"]["errored"]:
                    plainness_rows.setdefault(meta["profile"], []).append(row["quality"]["plainness"])
                errored += 1 if row["quality"]["errored"] else 0
                failed_checks += 0 if row["quality"]["checks"]["passed"] else 1
                spent += _cost(model, log_row["input_tokens"] or 0, log_row["output_tokens"] or 0)
                if n % 10 == 0 or n == len(todo):
                    log(f"  {n}/{len(todo)}  spent ${spent:.2f}  errors {errored}  failed checks {failed_checks}")
    finally:
        store.close()

    clean = write_clean(out_path)
    return {
        "written": written,
        "skipped": len(done),
        "errored": errored,
        "failed_checks": failed_checks,
        "spent_usd": round(spent, 4),
        "stopped": stopped,
        "clean_rows": clean,
        "model": model,
        "plainness_by_profile": plainness_by_profile(plainness_rows),
    }


def plainness_by_profile(rows_by_profile: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """`summarise` per profile, in PROFILES order (beginner ... expert ...)."""
    order = [p for p in PROFILES if p in rows_by_profile] + sorted(p for p in rows_by_profile if p not in PROFILES)
    return {profile: summarise(rows_by_profile[profile]) for profile in order}


def print_plainness_by_profile(table: dict[str, dict[str, Any]]) -> None:
    """The run summary's at-a-glance readout: does the teacher write PLAINER
    for beginners (lower grade, lower term density) rather than defining more
    (higher defs/100w)? Means, with p50 in brackets."""
    if not table:
        return
    print("\nPlainness by profile (mean [p50]) -- briefing v7 expects grade and terms/100w to RISE from beginner to expert while glosses/100w stays low everywhere"
          " ('made clear' is explained_terms/100w, which under v7 includes plain-words explanations and is not a gloss count):")
    labels = {k: v for k, v in BUNDLE_FIELDS}
    header = f"  {'profile':<22} {'n':>4}  " + "  ".join(f"{labels[k]:>14}" for k, _ in BUNDLE_FIELDS)
    print(header)
    for profile, summ in table.items():
        cells = []
        for key, _ in BUNDLE_FIELDS:
            d = summ.get(key) or {}
            mean, p50 = d.get("mean"), d.get("p50")
            cells.append(f"{'n/a':>14}" if mean is None else f"{mean:6.2f} [{p50:5.2f}]".rjust(14))
        print(f"  {profile:<22} {summ['n']:>4}  " + "  ".join(cells))


def write_clean(out_path: Path) -> int:
    clean_path = out_path.with_name(out_path.stem + ".clean.jsonl")
    kept = 0
    with out_path.open(encoding="utf-8") as src, clean_path.open("w", encoding="utf-8") as dst:
        for line in src:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("output") and row["quality"]["checks"]["passed"]:
                dst.write(line + "\n")
                kept += 1
    return kept


# --- Main ---------------------------------------------------------------------


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", type=int, default=20260915)
    parser.add_argument("--states-per-event", type=int, default=12)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--teacher", choices=sorted(TEACHERS), default=None,
                        help="sonnet (production default) or opus; sets MODEL_BRIEFING for this run")
    parser.add_argument("--budget", type=float, default=6.00, help="USD; stop before crossing it")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="store for the judgment_log rows")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--dry-run", action="store_true", help="coverage report and 3 sample packets; no calls")
    parser.add_argument("--offline", action="store_true", help="run the whole pipeline through the stub teacher")
    args = parser.parse_args(argv)

    if args.teacher:
        os.environ["MODEL_BRIEFING"] = TEACHERS[args.teacher]
    if config.local_model_enabled() and not (args.offline or args.dry_run):
        print("LOCAL_MODEL_BASE_URL is set; the teacher must be Claude. Unset it and rerun.", file=sys.stderr)
        return 2

    fixtures = load_fixtures()
    items = build_packets(fixtures, seed=args.seed, states_per_event=args.states_per_event, limit=args.limit)
    cov = coverage(items)
    model = config.model_for(config.BRIEFING)
    prompt = load_prompt(config.BRIEFING)
    print(f"Briefing prompt {prompt.version}; teacher {model}; {len(fixtures)} distinct events from {PERSONA_DIR.relative_to(PROJECT_ROOT)}")
    print_coverage(cov)
    print(f"\nEstimated live cost for {len(items)} calls at {model} prices: ${estimate_cost(model, len(items)):.2f}"
          f" (~{EST_INPUT_TOKENS} in / ~{EST_OUTPUT_TOKENS} out per call; cache reads not counted)")

    if args.dry_run:
        print("\nSample packets (one per profile, first three):")
        shown: set[str] = set()
        for it in items:
            if it["meta"]["profile"] in shown:
                continue
            shown.add(it["meta"]["profile"])
            print(f"\n--- {it['id']}  profile={it['meta']['profile']}  known={it['meta']['known']} attested={it['meta']['attested']}")
            print(json.dumps(it["packet"], indent=2, sort_keys=True, ensure_ascii=False))
            if len(shown) == 3:
                break
        return 0

    if client is None:
        if args.offline:
            client = OfflineClient()
            client.register(config.BRIEFING, offline_briefing)
        else:
            client = AnthropicClient()  # reads the key from the environment; never from here
    run_id = f"sft_gen_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    summary = generate(items, fixtures, client=client, db_path=args.db, out_path=args.out, run_id=run_id, budget=args.budget)
    print(f"\nrun {run_id}: wrote {summary['written']} rows (skipped {summary['skipped']} already present),"
          f" {summary['errored']} errored, {summary['failed_checks']} failed checks, spent ${summary['spent_usd']:.2f}")
    if summary["stopped"]:
        print(f"stopped early -- {summary['stopped']}")
    print_plainness_by_profile(summary["plainness_by_profile"])
    print(f"{args.out} (all rows) and {args.out.with_name(args.out.stem + '.clean.jsonl')} ({summary['clean_rows']} clean rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
