"""Persona generator: real events through the system's own search, then fixtures.

    PYTHONPATH=src:. .venv/bin/python -m harness.persona_gen events    # stage A
    PYTHONPATH=src:. .venv/bin/python -m harness.persona_gen personas  # stage B
    PYTHONPATH=src:. .venv/bin/python -m harness.persona_gen review    # REVIEW.md
    PYTHONPATH=src:. .venv/bin/python -m harness.persona_gen all
    PYTHONPATH=src:. .venv/bin/python -m harness.persona_gen subdomains  # stage C: backfill
                                     # group.subdomains / familiar_subdomains (one call per group)

Both stages are idempotent and cached under `personas/generated/_cache/`: a
re-run does no API work for anything already on disk unless `--force`.

**Stage A -- events.** Candidate events come from a directed research call
(`directed_search`: one `web_search` call per pass asking for twelve dated,
single-article reports for the group inside the window). Each candidate set is
then passed through the system's own `judge_source_selection`, each survivor's
URL is fetched (plain GET, falling back to a domain-restricted web search for
sites that wall scripts), the page text goes to a verification call that
rewrites the headline and detail *from the page only* and dates the event, and
anything the page does not support, that cannot be dated, or that falls
outside 1 August - 13 September 2026 is rejected. Survivors are judged by the
system's own `materiality` call with the group description as the only
context, exactly as `Monitor.poll` does on a first poll. Output:
`events_<slug>.json`, each event stamped with `discovered_by`.

Why not `search.discover_events` end to end: run five times on the Premier
League group it produced two verified events for about $2. Two causes, both
findings about the system rather than about this script: (1) `execute_search`
hands source selection `page_age` as each hit's `snippet`, while the
source-selection prompt reads `snippet` as content, so it discarded 29-35 of
~35 hits per pass; (2) the space-covering queries the query-formulation prompt
asks for return index pages -- Wikipedia season articles, fixtures hubs, other
countries' "Premier Leagues" -- that a credibility judge is right to reject.
Query formulation is therefore bypassed for candidate finding; source
selection and materiality are the system's.

**Stage B -- personas.** Four fixtures per group (one per archetype), each
written by a Sonnet call that sees the group, the verified events, the
archetype and the fixture rules, and returns the creative fields only. Every
structural decision -- ids, numbering, group name, event `concepts`, the
`learning` block (K from the term split, m from a seeded draw), noise flags,
lowercase register, seeds -- is made here in code, and every output is validated by constructing the real `Persona` (plus
the same range checks the prompt states) before it is written. On failure the
call is retried once with the errors appended, then skipped and logged.

Every model call goes through the project's `AnthropicClient`; the two
system judgments (source selection, materiality) go through
the real `Judge` and land in `judgment_log` under their own judgment points,
and the two generator-specific calls are logged to the same table under
`persona_gen.verify_event` / `persona_gen.persona`, all with
`run_id = persona_gen_<timestamp>` so the LLMOps console can find them.

Cost is metered exactly -- every SDK response's usage (input, output, cache
writes/reads, web searches) is recorded to `_cache/cost_ledger.jsonl` -- and a
running line is printed. If the projection for stage A exceeds `--budget`
(default $12) the run stops and says so rather than continuing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html as htmllib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conversational_agent import config, judgments, search
from conversational_agent.judgment import AnthropicClient, Judge, JudgmentError
from conversational_agent.judgment import schema as json_schema
from conversational_agent.store import Store, new_id

from .learning import (
    SUBDOMAIN_COUNT_RANGE,
    SUBDOMAIN_DNK_MAX_WEIGHT,
    SUBDOMAIN_KNOWS_MIN_WEIGHT,
    SubdomainMap,
)
from .personas import FixtureError, Persona, _parse, _validate
from .reading import PROFILES as READING_PROFILES
from .responders import BriefingView, PersonaState, ScriptedResponder, p_first_turn, terms_in

# --- Layout ------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "personas" / "generated"
CACHE_DIR = OUT_DIR / "_cache"
REVIEW_PATH = OUT_DIR / "REVIEW.md"
COST_LEDGER = CACHE_DIR / "cost_ledger.jsonl"

WINDOW_START = dt.date(2026, 8, 1)
WINDOW_END = dt.date(2026, 9, 13)

TARGET_MIN = 8
TARGET_MAX = 8
MAX_PASSES = 1  # one pass per group for the half-minimal run (human, 2026-09-14)
MAX_SEARCH_FALLBACKS_PER_GROUP = 2
# The directed research call gets a larger search allowance than a live
# monitor poll: it is asked for a whole window's worth of events at once, and
# the first run hit the poll-sized cap after one useful query and returned prose.
DIRECTED_MAX_USES = 12
FIRST_FIXTURE_NUMBER = 6
USER_ID = "persona_gen"

# Rough stage-B reservation used in the stage-A projection, in dollars.
STAGE_B_ESTIMATE = 1.5

# $/MTok from the system's price table, keyed on the model family so a dated
# or aliased id still resolves. Web search is $10 per 1,000.
PRICES = {
    "opus": config.PRICES_PER_MTOK[config.OPUS],
    "sonnet": config.PRICES_PER_MTOK[config.SONNET],
    "haiku": config.PRICES_PER_MTOK[config.HAIKU],
}
SEARCH_PRICE = 0.01

# --- Groups ------------------------------------------------------------------
# The descriptions are what the materiality judge sees. Keep them verbatim.


@dataclass(frozen=True)
class GroupDef:
    slug: str
    name: str
    description: str
    poll_interval_minutes: int


GROUPS: tuple[GroupDef, ...] = (
    GroupDef("premier_league", "Premier League",
             "English top-flight football: results, squads, injuries, refereeing and the transfer market.", 240),
    GroupDef("nfl", "NFL",
             "American football's NFL: results, injuries, trades, coaching changes and the start of the 2026 season.", 240),
    GroupDef("formula_1", "Formula 1",
             "F1 grands prix, driver and constructor standings, technical regulations, team and driver moves.", 360),
    GroupDef("startups_vc", "Startups and VC",
             "Funding rounds, exits and market conditions in venture-backed technology companies.", 180),
    GroupDef("crypto", "Crypto",
             "Bitcoin, Ethereum and major tokens: prices, ETF flows, exchange and protocol events, regulation.", 120),
    GroupDef("ai_trends", "AI industry",
             "Model releases, AI company funding and deals, compute and chips, AI policy.", 180),
    GroupDef("public_markets", "Stocks and markets",
             "US equities, major earnings, the Fed and rates, notable single-stock moves.", 120),
    GroupDef("wine_trade", "Wine trade",
             "Harvest, vintage conditions, appellation rules and the commercial state of the wine industry.", 720),
    GroupDef("film_tv", "Film and TV",
             "Box office, streaming, awards season, major releases and industry deals.", 360),
    GroupDef("music", "Music",
             "Charts, tours, releases, label and streaming business, awards.", 360),
)
GROUP_BY_SLUG = {g.slug: g for g in GROUPS}

ARCHETYPE_ORDER = ("well_informed", "partially_informed", "barely_informed", "disengaged")

# Plausible, diverse, not public figures. Index = group_index * 4 + archetype
# index, so every first name -- and therefore every persona id -- is unique.
NAMES: tuple[str, ...] = (
    "Amara Okonkwo", "Tomasz Wierzbicki", "Priya Raghunathan", "Callum Beattie",
    "Yuki Tanabe", "Rosa Delgado", "Kwame Mensah", "Ingrid Solheim",
    "Rafael Ortega", "Meilin Chao", "Dmitri Volkov", "Leila Haddad",
    "Sean Gallagher", "Aditi Bhattacharya", "Noor Al-Sayed", "Jonas Lindqvist",
    "Farida Nazarova", "Malik Thompson", "Hana Kovacs", "Bruno Ferreira",
    "Sipho Dlamini", "Elena Marchetti", "Tariq Rahman", "Chloe Whitfield",
    "Ravi Menon", "Astrid Nystrom", "Femi Adebayo", "Marta Kowalczyk",
    "Kenji Morimoto", "Lucia Sandoval", "Oskar Brandt", "Nadia Petrova",
    "Idris Bello", "Saoirse Byrne", "Hugo Lefevre", "Zainab Chaudhry",
    "Mateo Rojas", "Anya Fedorova", "Devraj Singh", "Greta Hoffmann",
)

# Exactly six noisy personas, in the two shapes the hand-built set uses:
# `03_ade` (sheepish about unknown terms, asks about known ones) on the
# partially-informed persona of three groups, and `05_theo` (`never_engages`)
# on the disengaged persona of three others. The remaining disengaged personas
# carry the same zero turn rates without the label -- the suite-level
# zero-turn check in `personas._validate` reads the rates, not the flag.
NOISY_PARTIAL_GROUPS = (0, 3, 6)
NOISY_DISENGAGED_GROUPS = (1, 4, 7)
ADE_NOISE = {"silent_when_ignorant_rate": 0.6, "asks_about_known_rate": 0.25}
THEO_NOISE = {"never_engages": True}

# 37.5% of measured turns are lowercase-initial and the trait clusters by
# person (assistant_reply_style_profile.md section 3). 15 of 40.
LOWERCASE_SLOTS = (1, 4, 6)  # global persona index mod 8

# --- Fixture rules, as code -------------------------------------------------
# (knows_lo, knows_hi), (does_not_know_lo, does_not_know_hi)
TERM_COUNTS = {
    "well_informed": ((8, 12), (2, 4)),
    "partially_informed": ((4, 6), (4, 6)),
    "barely_informed": ((1, 3), (8, 12)),
    "disengaged": ((1, 3), (8, 12)),
}

# Ranges sit inside what assistant_reply_style_profile.md section 6 lists
# (P(any turn) 0.20-0.30 with nothing unfamiliar, 0.40-0.55 with an unfamiliar
# term, react 0.05-0.10, continue 0.30-0.45, context ~0.3, terminal
# punctuation dropped ~0.5) and reply_style_profile.md section 8.2 (medians
# 16 / 13 / 8 / 5). They are narrowed so that p(first turn) = ask + (1-ask) *
# react lands in [0.25, 0.5] for both ask rates, which the fixture brief
# requires and which is checked with the real `p_first_turn`.
STYLE_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "well_informed": {
        "median_words": (14, 18), "drop_terminal_punct_rate": (0.40, 0.60),
        "ask_rate_default": (0.22, 0.30), "ask_rate_unknown_present": (0.38, 0.45),
        "react_rate": (0.05, 0.09), "continue_rate": (0.30, 0.45), "max_turns": (3, 3),
        "clarify_share": (0.40, 0.55), "check_belief_share": (0.28, 0.40),
        "remark_share": (0.40, 0.60), "context_rate": (0.22, 0.32),
        "gap_admit_rate": (0.03, 0.08), "redirect_rate": (0.08, 0.14),
    },
    "partially_informed": {
        "median_words": (11, 15), "drop_terminal_punct_rate": (0.40, 0.60),
        "ask_rate_default": (0.22, 0.30), "ask_rate_unknown_present": (0.38, 0.45),
        "react_rate": (0.05, 0.09), "continue_rate": (0.30, 0.45), "max_turns": (3, 3),
        "clarify_share": (0.55, 0.70), "check_belief_share": (0.15, 0.25),
        "remark_share": (0.40, 0.60), "context_rate": (0.25, 0.35),
        "gap_admit_rate": (0.10, 0.20), "redirect_rate": (0.08, 0.14),
    },
    "barely_informed": {
        "median_words": (6, 10), "drop_terminal_punct_rate": (0.40, 0.60),
        "ask_rate_default": (0.22, 0.30), "ask_rate_unknown_present": (0.40, 0.45),
        "react_rate": (0.05, 0.08), "continue_rate": (0.35, 0.45), "max_turns": (3, 3),
        "clarify_share": (0.80, 0.95), "check_belief_share": (0.05, 0.15),
        "remark_share": (0.35, 0.50), "context_rate": (0.28, 0.38),
        "gap_admit_rate": (0.20, 0.30), "redirect_rate": (0.08, 0.14),
    },
}
DISENGAGED_STYLE = {
    "median_words": 5, "variance": 1.0, "drop_terminal_punct_rate": 0.6,
    "ask_rate_default": 0.0, "ask_rate_unknown_present": 0.0, "react_rate": 0.0,
    "continue_rate": 0.0, "max_turns": 0, "clarify_share": 0.0,
    "check_belief_share": 0.0, "remark_share": 0.0, "context_rate": 0.0,
    "gap_admit_rate": 0.0, "redirect_rate": 0.0,
}
STYLE_FIELDS = tuple(STYLE_RANGES["well_informed"].keys())
INT_STYLE_FIELDS = ("median_words", "max_turns")
P_FIRST_TURN_RANGE = (0.25, 0.50)

# --- Learning block (schema v1) --------------------------------------------
# Two traits, no more: the review rejected age, working memory, learning style
# and interest. `prior_knowledge` (K) is the archetype's prior, and it must
# agree with the term split it sits next to: K ~= |knows| / (|knows| +
# |does_not_know|) within 0.1, clamped into the archetype's band.
# `memory_rate` (m) is drawn uniformly in [0.7, 1.4] INDEPENDENTLY of
# archetype -- forgetting rate is not predicted by ability or knowledge -- from
# a fixed seed per slot, with four slots pinned to the fast-forgetter tail
# (<= 0.8) and four to the slow tail (>= 1.3), spread across groups.
class BudgetExceeded(RuntimeError):
    pass


class GenError(RuntimeError):
    pass


LEARNING_SCHEMA = "v1"
PRIOR_KNOWLEDGE_BANDS = {
    "well_informed": (0.75, 0.90),
    "partially_informed": (0.40, 0.60),
    "barely_informed": (0.05, 0.20),
    "disengaged": (0.05, 0.20),
}
MEMORY_RATE_RANGE = (0.70, 1.40)
FAST_FORGETTER_SLOTS = (0, 11, 22, 33)   # groups 0, 2, 5, 8; one per archetype
SLOW_FORGETTER_SLOTS = (5, 16, 27, 38)   # groups 1, 4, 6, 9; one per archetype


def _memory_rates() -> tuple[float, ...]:
    import random

    rng = random.Random("persona_gen:learning:v1")
    out = []
    for i in range(len(NAMES)):
        if i in FAST_FORGETTER_SLOTS:
            lo, hi = MEMORY_RATE_RANGE[0], 0.80
        elif i in SLOW_FORGETTER_SLOTS:
            lo, hi = 1.30, MEMORY_RATE_RANGE[1]
        else:
            lo, hi = MEMORY_RATE_RANGE
        out.append(round(rng.uniform(lo, hi), 2))
    return tuple(out)


MEMORY_RATES = _memory_rates()


# --- Subdomain weights by archetype -----------------------------------------
# `familiar_subdomains` is a weight 0-1 per subdomain. The bands below are what
# the persona prompt asks for and `check_output` enforces. One exception in
# each band: the subdomain that holds a `knows` term may not be weighted at or
# below SUBDOMAIN_KNOWS_MIN_WEIGHT (0.2) -- a person does not hold a term from
# a field they know nothing of -- so for the two low archetypes that one
# subdomain may rise to KNOWS_SUBDOMAIN_MAX. Likewise no subdomain holding a
# `does_not_know` term may reach SUBDOMAIN_DNK_MAX_WEIGHT (0.8).
FAMILIAR_BANDS: dict[str, tuple[float, float]] = {
    "well_informed": (0.70, 0.95),      # across most subdomains, one weaker (<= WEAK_MAX)
    "partially_informed": (0.20, 0.80),
    "barely_informed": (0.00, 0.20),
    "disengaged": (0.00, 0.15),
}
# The one weaker subdomain of a well-informed persona: anything below the
# band's floor (the first backfill run rejected a 0.65 sitting between a 0.6
# cap and the 0.7 floor, which was a dead zone of this file's making).
WELL_INFORMED_WEAK_MAX = FAMILIAR_BANDS["well_informed"][0]
SUBDOMAIN_NAME_MAX_WORDS = 5    # "&" not counted
KNOWS_SUBDOMAIN_MAX = 0.35      # the knows-holding subdomain of a low archetype
K_FROM_WEIGHTS_TOLERANCE = 0.10


def weights_from_pairs(raw: Any) -> dict[str, float]:
    """{name: weight} from the wire form (a list of {name, weight}) or a dict."""
    out: dict[str, float] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = ((e.get("name"), e.get("weight")) for e in raw if isinstance(e, dict))
    else:
        return out
    for name, w in items:
        if isinstance(name, str) and name.strip():
            try:
                out[SubdomainMap.norm(name)] = round(float(w), 2)
            except (TypeError, ValueError):
                continue
    return out


def taxonomy_from_pairs(raw: Any) -> dict[str, list[str]]:
    """{name: [terms]} from the wire form (a list of {name, terms}) or a dict."""
    out: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = ((e.get("name"), e.get("terms")) for e in raw if isinstance(e, dict))
    else:
        return out
    for name, terms in items:
        if isinstance(name, str) and name.strip():
            out[SubdomainMap.norm(name)] = _norm_terms(terms)
    return out


def check_subdomains(archetype: str, subdomains: dict[str, list[str]], weights: dict[str, float],
                     knows: list[str], dnk: list[str]) -> list[str]:
    """The subdomain rules, as code. Returns problems; empty means clean."""
    problems: list[str] = []
    lo_n, hi_n = SUBDOMAIN_COUNT_RANGE
    if not lo_n <= len(subdomains) <= hi_n:
        problems.append(f"{len(subdomains)} subdomains; need {lo_n}-{hi_n}")
    owner: dict[str, str] = {}
    for name, terms in subdomains.items():
        if len([w for w in name.split() if w != "&"]) > SUBDOMAIN_NAME_MAX_WORDS:
            problems.append(f"subdomain name longer than {SUBDOMAIN_NAME_MAX_WORDS} words: {name!r}")
        for t in terms:
            if t in owner:
                problems.append(f"term {t!r} is in both {owner[t]!r} and {name!r}")
            owner[t] = name
    for t in knows + dnk:
        if t not in owner:
            problems.append(f"vocabulary term {t!r} is in no subdomain")
    missing = sorted(set(subdomains) - set(weights))
    extra = sorted(set(weights) - set(subdomains))
    if missing:
        problems.append(f"familiar_subdomains gives no weight for {missing}")
    if extra:
        problems.append(f"familiar_subdomains names unknown subdomains {extra}")
    lo, hi = FAMILIAR_BANDS[archetype]
    knows_holding = {owner[t] for t in knows if t in owner}
    weak_seen = 0
    for name, w in weights.items():
        if not 0.0 <= w <= 1.0:
            problems.append(f"weight for {name!r} is {w}, not in [0, 1]")
            continue
        if name in knows_holding and w <= SUBDOMAIN_KNOWS_MIN_WEIGHT:
            problems.append(
                f"{name!r} holds a `knows` term but is weighted {w} (<= {SUBDOMAIN_KNOWS_MIN_WEIGHT})"
            )
        if any(owner.get(t) == name for t in dnk) and w >= SUBDOMAIN_DNK_MAX_WEIGHT:
            problems.append(
                f"{name!r} holds a `does_not_know` term but is weighted {w} (>= {SUBDOMAIN_DNK_MAX_WEIGHT})"
            )
        in_band = lo <= w <= hi
        if archetype == "well_informed" and not in_band and w < WELL_INFORMED_WEAK_MAX:
            weak_seen += 1
            continue
        if archetype in ("barely_informed", "disengaged") and not in_band \
                and name in knows_holding and w <= KNOWS_SUBDOMAIN_MAX:
            continue
        if not in_band:
            problems.append(f"weight for {name!r} is {w}, outside the {archetype} band [{lo}, {hi}]")
    if archetype == "well_informed" and weak_seen > 1:
        problems.append(f"well_informed may have at most one weaker subdomain; got {weak_seen}")
    return problems


def learning_block(archetype: str, n_knows: int, n_dnk: int, global_index: int, *,
                   vocabulary: list[str] | None = None,
                   subdomains: dict[str, list[str]] | None = None,
                   familiar: dict[str, float] | None = None) -> dict[str, Any]:
    """The `learning` block. K is the vocabulary-weighted mean of the subdomain
    weights when the fixture carries them (clamped into the archetype's band),
    else the pre-subdomain term-split ratio."""
    lo, hi = PRIOR_KNOWLEDGE_BANDS[archetype]
    if subdomains and familiar and vocabulary:
        smap = SubdomainMap(terms={k: tuple(v) for k, v in subdomains.items()}, weights=dict(familiar))
        ratio = smap.vocabulary_weighted_mean(vocabulary)
        source = "vocabulary-weighted mean of familiar_subdomains"
    else:
        ratio = n_knows / max(1, n_knows + n_dnk)
        source = "term-split ratio"
    k = round(min(hi, max(lo, ratio)), 2)
    if abs(k - ratio) > K_FROM_WEIGHTS_TOLERANCE:
        raise GenError(
            f"prior_knowledge {k} is more than {K_FROM_WEIGHTS_TOLERANCE} from the {source} "
            f"{ratio:.2f} ({n_knows} knows / {n_dnk} does_not_know) for {archetype}"
        )
    return {"schema": LEARNING_SCHEMA, "prior_knowledge": k, "memory_rate": MEMORY_RATES[global_index]}


def forgetter_label(m: float) -> str:
    if m <= 0.80:
        return "fast forgetter"
    if m >= 1.30:
        return "slow forgetter"
    return ""


# --- Cost metering -----------------------------------------------------------


def _price(model: str) -> tuple[float, float]:
    for key, prices in PRICES.items():
        if key in (model or ""):
            return prices
    return PRICES["opus"]


@dataclass
class CostMeter:
    run_id: str
    stage: str = ""
    spent: float = 0.0
    calls: int = 0
    searches: int = 0
    budget: float = 12.0
    spent_before: float = 0.0

    def record(self, response: Any, label: str) -> float:
        usage = getattr(response, "usage", None)
        model = getattr(response, "model", "") or ""
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        stu = getattr(usage, "server_tool_use", None)
        ws = int(getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        pin, pout = _price(model)
        cost = (inp * pin + cw * pin * 1.25 + cr * pin * 0.10 + out * pout) / 1e6 + ws * SEARCH_PRICE
        self.spent += cost
        self.calls += 1
        self.searches += ws
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with COST_LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                "run_id": self.run_id, "stage": self.stage, "label": label,
                "model": model, "input_tokens": inp, "output_tokens": out,
                "cache_write_tokens": cw, "cache_read_tokens": cr,
                "web_searches": ws, "cost_usd": round(cost, 6),
            }) + "\n")
        if self.spent > self.budget:
            raise BudgetExceeded(
                f"spent ${self.spent:.2f} which exceeds the ${self.budget:.2f} budget"
            )
        return cost

    def line(self, projected: float | None = None) -> str:
        proj = f" | projected ${projected:.2f}" if projected is not None else ""
        return (f"[cost] ${self.spent:.2f} spent (all runs; ${self.spent - self.spent_before:.2f} this run)"
                f"{proj} | budget ${self.budget:.2f} | {self.calls} calls, {self.searches} searches this run")


def ledger_totals() -> dict[str, Any]:
    """Sum the on-disk ledger, across every run that contributed to the cache."""
    totals: dict[str, Any] = {"total_usd": 0.0, "by_stage": {}, "by_run": {}, "rows": 0,
                              "web_searches": 0, "input_tokens": 0, "output_tokens": 0}
    if not COST_LEDGER.exists():
        return totals
    for line in COST_LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        c = float(row.get("cost_usd", 0.0))
        totals["total_usd"] += c
        totals["rows"] += 1
        totals["web_searches"] += int(row.get("web_searches", 0))
        totals["input_tokens"] += int(row.get("input_tokens", 0)) + int(row.get("cache_write_tokens", 0)) + int(row.get("cache_read_tokens", 0))
        totals["output_tokens"] += int(row.get("output_tokens", 0))
        totals["by_stage"][row.get("stage", "")] = totals["by_stage"].get(row.get("stage", ""), 0.0) + c
        totals["by_run"][row.get("run_id", "")] = totals["by_run"].get(row.get("run_id", ""), 0.0) + c
    return totals


class _MeteredMessages:
    def __init__(self, outer: MeteredClient):
        self._outer = outer

    def create(self, **kwargs: Any) -> Any:
        response = self._outer.inner.messages.create(**kwargs)
        tools = kwargs.get("tools") or []
        label = "json"
        if any(t.get("name") == "web_search" for t in tools):
            label = "fallback_search" if any(t.get("allowed_domains") for t in tools) else "search"
        self._outer.meter.record(response, label)
        return response


class MeteredClient:
    """The SDK client with every `messages.create` metered. Nothing else changes."""

    def __init__(self, inner: Any, meter: CostMeter):
        self.inner = inner
        self.meter = meter
        self.messages = _MeteredMessages(self)


# --- Generator context -------------------------------------------------------


@dataclass
class Gen:
    run_id: str
    store: Store
    judge: Judge
    llm: AnthropicClient
    raw: MeteredClient
    meter: CostMeter
    force: bool = False
    max_events: int = TARGET_MAX


def build_gen(db_path: Path | None, budget: float, force: bool, max_events: int) -> Gen:
    import os

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise SystemExit(
            "No Anthropic credentials in the environment. Export "
            "ANTHROPIC_API_KEY first (see .env.example)."
        )
    import anthropic

    run_id = "persona_gen_" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    meter = CostMeter(run_id=run_id, budget=budget, spent=ledger_totals()["total_usd"])
    meter.spent_before = meter.spent
    raw = MeteredClient(anthropic.Anthropic(timeout=240.0, max_retries=3), meter)
    llm = AnthropicClient(raw)
    store = Store(db_path)
    judge = Judge(client=llm, store=store, run_id=run_id)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return Gen(run_id=run_id, store=store, judge=judge, llm=llm, raw=raw, meter=meter,
               force=force, max_events=max_events)


# --- Cache helpers -----------------------------------------------------------


def _cache_get(name: str) -> Any:
    path = CACHE_DIR / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _cache_put(name: str, obj: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{name}.json").write_text(
        json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


# --- A logged, structured call outside the six judgment points --------------


def logged_call(
    gen: Gen,
    point: str,
    *,
    system: str,
    context: dict[str, Any],
    schema: dict[str, Any],
    model: str,
    effort: str,
    group_id: str,
    max_tokens: int = 8000,
) -> dict[str, Any]:
    """Mirror of `Judge.__call__` for a prompt that has no `prompts/<point>.md`.

    Same wire shape, same log row, so the console shows these next to the
    system's own calls. The prompt text is versioned by the constant below.
    """
    payload = json.dumps({"_judgment_point": point, "context": context},
                         indent=2, sort_keys=True, default=str)
    started = time.perf_counter()
    error: str | None = None
    raw = None
    data: dict[str, Any] = {}
    try:
        raw = gen.llm.complete_json(model=model, system=system, user_content=payload,
                                    schema=schema, effort=effort, max_tokens=max_tokens)
        data = json.loads(raw.text)
    except BudgetExceeded:
        raise
    except JudgmentError as exc:
        error = str(exc)
    except json.JSONDecodeError as exc:
        error = f"Model returned unparseable JSON: {exc}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = int((time.perf_counter() - started) * 1000)
    gen.store.log_judgment({
        "id": new_id("jdg"),
        "judgment_point": point,
        "user_id": USER_ID,
        "group_id": group_id,
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "effort": effort,
        "input_json": payload,
        "verdict_json": json.dumps(data) if data else None,
        "reasoning": str(data.get("reasoning", "")) if data else "",
        "input_tokens": raw.input_tokens if raw else None,
        "output_tokens": raw.output_tokens if raw else None,
        "latency_ms": latency_ms,
        "error": error,
        "run_id": gen.run_id,
    })
    if error:
        raise GenError(f"[{point}] {error}")
    return data


PROMPT_VERSION = "persona_gen-v1"

# =============================================================================
# Stage A -- events
# =============================================================================

VERIFY_SYSTEM = """\
You verify a candidate news event against the text of the page that was cited
for it. The events go into an evaluation fixture that must contain only real,
dated, checkable developments, so the standard is: supported by this page, or
out.

You receive JSON with the group, the candidate (a headline and short detail as
a reader of search results described it), and the cited page (title, any
publication-date hint found in its markup, and its extracted body text).

Answer from the page text only. Do not add facts from your own memory: a
figure that is not on the page does not go in `detail`.

- `supported` -- true only if the page genuinely reports the event in the
  candidate headline: not a related topic, not a preview of it, not a
  listicle, not an undated explainer. A paywall stub, login wall, bot-check
  page, error page, or unrelated page is false.
- `headline` -- one sentence stating what happened, plainly, with the key
  figure in it where there is one. Correct the candidate's headline where the
  page contradicts it.
- `detail` -- two to four sentences of substance from the page, carrying the
  concrete figures: amounts, scores, dates, percentages, the parties involved.
  No speculation, no framing, no "according to the article".
- `occurred_at` -- the date the event itself happened, YYYY-MM-DD, from the
  page. If the page gives only a publication date and this is clearly
  same-day news, use that. If no date can be established, return "".
- `source_name` -- the publication's name as its readers know it.
- `reasoning` -- one sentence on what in the page supports or fails the
  headline.
"""

VERIFY_SCHEMA = json_schema(
    {
        "supported": {"type": "boolean"},
        "headline": {"type": "string"},
        "detail": {"type": "string"},
        "occurred_at": {"type": "string", "description": "YYYY-MM-DD, or empty string."},
        "source_name": {"type": "string"},
    },
    ["supported", "headline", "detail", "occurred_at", "source_name"],
)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
_DATE_META_RE = re.compile(
    r'(?:article:published_time|datePublished|publishdate|pubdate|date)["\']?\s*content=["\']([^"\']+)',
    re.IGNORECASE,
)
_JSONLD_DATE_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _html_to_text(raw_html: str) -> tuple[str, str, str]:
    """(title, published hint, body text)."""
    title = ""
    m = _TITLE_RE.search(raw_html)
    if m:
        title = htmllib.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    hint = ""
    m = _DATE_META_RE.search(raw_html) or _JSONLD_DATE_RE.search(raw_html)
    if m:
        hint = m.group(1).strip()
    body = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", raw_html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = htmllib.unescape(body)
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    body = re.sub(r"\s*\n\s*", "\n", body)
    body = re.sub(r"\n{2,}", "\n", body).strip()
    return title, hint, body


def fetch_page(url: str) -> dict[str, Any]:
    """Plain GET. `ok` means a 2xx with enough text to be an article."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            status = int(resp.status)
            final_url = resp.geturl()
            raw = resp.read(900_000).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": int(exc.code), "final_url": url, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "status": None, "final_url": url, "error": f"{type(exc).__name__}: {exc}"}
    title, hint, text = _html_to_text(raw)
    ok = 200 <= status < 300 and len(text) >= 1500
    return {"ok": ok, "status": status, "final_url": final_url, "title": title,
            "published_hint": hint, "text": text, "error": None if ok else "thin or non-2xx body"}


def fetch_via_search(gen: Gen, url: str, headline: str, group_id: str) -> str | None:
    """Sites that refuse scripts (and Anthropic's fetcher): one domain-restricted
    web search, whose index copy of the page the model returns as text.

    `web_fetch` was tried first and is the wrong tool here: the sites that wall
    a plain GET wall it too, and a failed attempt still costs ~15k tokens.
    """
    domain = urllib.parse.urlsplit(url).netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    instruction = (
        f"Using web search restricted to {domain}, find this specific report and return its "
        "content as fully as the results allow: headline, date, and the body facts and figures, "
        "verbatim where possible, with no commentary of your own. If the results do not contain "
        "this report, reply with exactly FETCH_FAILED.\n\n"
        f"Headline: {headline}\nURL: {url}"
    )
    started = time.perf_counter()
    error = None
    text = ""
    try:
        response = gen.raw.messages.create(
            model=config.SONNET, max_tokens=4000,
            messages=[{"role": "user", "content": instruction}],
            tools=[{**config.WEB_SEARCH_TOOL, "max_uses": 1, "allowed_domains": [domain]}],
        )
        text = "\n".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
    except BudgetExceeded:
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    gen.store.log_judgment({
        "id": new_id("jdg"), "judgment_point": "persona_gen.fetch_page", "user_id": USER_ID,
        "group_id": group_id, "prompt_version": PROMPT_VERSION, "model": config.SONNET,
        "effort": None, "input_json": json.dumps({"url": url, "method": "web_search", "domain": domain}),
        "verdict_json": json.dumps({"chars": len(text)}), "reasoning": "",
        "input_tokens": None, "output_tokens": None,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "error": error, "run_id": gen.run_id,
    })
    if error or not text or "FETCH_FAILED" in text[:300] or len(text) < 600:
        return None
    return text


def _norm_url(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{p.path.rstrip('/').lower()}"


_STOP = frozenset("a an the of to in on for and or as at by with from its is are was were be has have "
                  "after over into out up down about than that this it his her their new first".split())


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 1}


def _similar(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.5


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", text.strip()) if s.strip()])


def _parse_date(text: str) -> dt.date | None:
    text = (text or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def verify_candidate(gen: Gen, group: GroupDef, cand: dict[str, Any],
                     fallbacks: dict[str, int]) -> dict[str, Any]:
    """Resolve the URL, read the page, verify and date the event from it."""
    url = str(cand.get("source_url", "")).strip()
    key = "verify_" + _key(url)
    cached = _cache_get(key)
    if cached is not None and not gen.force:
        return cached

    group_id = f"gen_{group.slug}"

    def reject(reason: str, **extra: Any) -> dict[str, Any]:
        out = {"accepted": False, "reason": reason, "candidate": cand, "url": url, "group": group.slug, **extra}
        _cache_put(key, out)
        return out

    if not url.startswith("http"):
        return reject("no http(s) source URL")

    page = fetch_page(url)
    via = "get"
    if not page["ok"]:
        if page.get("status") in (404, 410):
            return reject(f"URL does not resolve (HTTP {page['status']})", status=page["status"])
        if fallbacks.get("used", 0) >= MAX_SEARCH_FALLBACKS_PER_GROUP:
            return reject(f"page unreadable ({page.get('error')}); search-fallback cap reached for group",
                          status=page.get("status"))
        fallbacks["used"] = fallbacks.get("used", 0) + 1
        text = fetch_via_search(gen, url, str(cand.get("headline", "")), group_id)
        if text is None:
            return reject(f"page unreadable ({page.get('error')}) and search fallback found no copy",
                          status=page.get("status"))
        page = {"ok": True, "status": page.get("status"), "final_url": url, "title": "",
                "published_hint": "", "text": text}
        via = "web_search"

    context = {
        "group": {"name": group.name, "description": group.description},
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "candidate": {
            "headline": cand.get("headline", ""), "detail": cand.get("detail", ""),
            "source_name": cand.get("source_name", ""), "source_url": url,
        },
        "page": {
            "retrieved_via": ("direct GET of the URL" if via == "get" else
                              "a domain-restricted web search, because the site walls scripts; "
                              "the text is the search index's copy of the same report"),
            "title": page.get("title", ""), "published_hint": page.get("published_hint", ""),
            "text": page["text"][:14000],
        },
    }
    problems: list[str] = []
    verdict: dict[str, Any] = {}
    for _attempt in range(2):
        ctx = dict(context)
        if problems:
            ctx["previous_attempt_problems"] = problems
            ctx["previous_attempt"] = verdict
        try:
            verdict = logged_call(gen, "persona_gen.verify_event", system=VERIFY_SYSTEM,
                                  context=ctx, schema=VERIFY_SCHEMA, model=config.SONNET,
                                  effort="medium", group_id=group_id)
        except GenError as exc:
            return reject(f"verification call failed: {exc}")
        if not verdict.get("supported"):
            return reject(f"page does not support the event: {verdict.get('reasoning', '')}",
                          verdict=verdict, fetched_via=via)
        problems = []
        date = _parse_date(verdict.get("occurred_at", ""))
        if date is None:
            problems.append("occurred_at must be YYYY-MM-DD; none was established")
        elif not (WINDOW_START <= date <= WINDOW_END):
            return reject(f"event dated {date.isoformat()}, outside {WINDOW_START}..{WINDOW_END}",
                          verdict=verdict, fetched_via=via)
        n = _sentences(verdict.get("detail", ""))
        if not 2 <= n <= 4:
            problems.append(f"detail must be 2-4 sentences with the concrete figures; got {n}")
        if not str(verdict.get("headline", "")).strip():
            problems.append("headline is empty")
        if not str(verdict.get("source_name", "")).strip():
            problems.append("source_name is empty")
        if not problems:
            break
    if problems:
        # One retry has been spent. Tolerate a slightly long detail; nothing else.
        hard = [p for p in problems if not p.startswith("detail must") or not 2 <= _sentences(verdict.get("detail", "")) <= 5]
        if hard:
            return reject("verification incomplete after retry: " + "; ".join(hard),
                          verdict=verdict, fetched_via=via)

    event = {
        "headline": str(verdict["headline"]).strip(),
        "detail": str(verdict["detail"]).strip(),
        "source_name": str(verdict["source_name"]).strip(),
        "source_url": url,
        "occurred_at": _parse_date(verdict["occurred_at"]).isoformat(),
        "verified_via": via,
        "http_status": page.get("status"),
        "candidate_confidence": cand.get("confidence"),
        "verify_reasoning": verdict.get("reasoning", ""),
    }
    out = {"accepted": True, "event": event, "candidate": cand, "url": url, "group": group.slug,
           "discovered_by": "directed_search"}
    _cache_put(key, out)
    return out


def judge_event(gen: Gen, group: GroupDef, event: dict[str, Any]) -> dict[str, Any]:
    """The system's own materiality call, group description as the only context."""
    key = "materiality_" + _key(event["source_url"])
    cached = _cache_get(key)
    if cached is not None and not gen.force:
        return cached
    candidate = {k: event[k] for k in ("headline", "detail", "source_url", "source_name", "occurred_at")}
    last_error = ""
    for _ in range(2):
        try:
            verdict = judgments.judge_materiality(
                gen.judge,
                group={"name": group.name, "description": group.description},
                candidate_event=candidate,
                recent_events=[],
                user_id=USER_ID,
                group_id=f"gen_{group.slug}",
            )
        except JudgmentError as exc:
            last_error = str(exc)
            continue
        score = float(verdict.get("materiality_score", 0) or 0)
        out = {
            "ok": True,
            "is_material": bool(verdict.get("is_material")),
            "score": score,
            "reasoning": verdict.reasoning,
            "significance": "major" if score >= 76 else ("borderline" if score >= 40 else "minor"),
            "model": verdict.model,
            "prompt_version": verdict.prompt_version,
        }
        _cache_put(key, out)
        return out
    out = {"ok": False, "error": last_error}
    _cache_put(key, out)
    return out



DIRECTED_INSTRUCTION = """\
You are compiling real, dated news reports for a reader who follows this group:

  {name} -- {description}

Using web search (up to {max_uses} searches, different angles), find {want} DISTINCT significant
developments that happened between {start} and {end}, spread across those weeks, that this group
would still be talking about. Each must be reported by an established outlet -- national press,
a major sports, business or trade publication, or an official body. Prefer a specific article
page; a tracker, results or transactions page is ACCEPTABLE as a candidate when it names a dated
development with a figure -- every URL is fetched and checked afterwards, so include it rather than
withhold it. Use one broad search per week of the window plus targeted follow-ups.{avoid}

ALWAYS return the JSON array, even if it holds only two or three items and even if your searches
were cut short. Never reply with prose instead of the array.

Return ONLY a JSON array inside a ```json fence, one object per item:
  {{"headline": "as the outlet ran it", "source_url": "the article URL exactly as shown in the
  results", "source_name": "outlet", "occurred_at": "YYYY-MM-DD", "summary": "one sentence of what
  happened, with the key figure"}}

Only include items that appeared in your search results. Never invent or reconstruct a URL.
"""

_JSON_FENCE_RE = re.compile(r"```json\s*(\[.*?\])\s*```", re.DOTALL)


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    m = _JSON_FENCE_RE.search(text or "")
    blob = m.group(1) if m else None
    if blob is None:
        i, j = (text or "").find("["), (text or "").rfind("]")
        if i < 0 or j <= i:
            return []
        blob = text[i:j + 1]
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def directed_search(gen: Gen, group: GroupDef, pass_no: int, avoid: list[str]) -> dict[str, Any]:
    """One research call finds candidates; the system's source selection judges them."""
    key = f"directed_{group.slug}_p{pass_no}"
    cached = _cache_get(key)
    if cached is not None and not gen.force:
        return cached
    group_id = f"gen_{group.slug}"
    avoid_text = ""
    if avoid:
        avoid_text = " Do NOT repeat these already-recorded developments:\n" + "\n".join(f"  - {h}" for h in avoid)
    instruction = DIRECTED_INSTRUCTION.format(
        name=group.name, description=group.description, max_uses=DIRECTED_MAX_USES,
        want=12, start=f"{WINDOW_START:%-d %B %Y}", end=f"{WINDOW_END:%-d %B %Y}", avoid=avoid_text,
    )
    found: dict[str, Any] = {"pass": pass_no, "run_id": gen.run_id, "events": [], "candidates": [],
                             "method": "directed_search"}
    started = time.perf_counter()
    error = None
    text = ""
    try:
        response = gen.raw.messages.create(
            model=config.SEARCH_EXECUTION_MODEL, max_tokens=8192,
            messages=[{"role": "user", "content": instruction}],
            tools=[{**config.WEB_SEARCH_TOOL, "max_uses": DIRECTED_MAX_USES}],
        )
        text = "\n".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    except BudgetExceeded:
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    candidates = _extract_json_array(text)
    gen.store.log_judgment({
        "id": new_id("jdg"), "judgment_point": "persona_gen.directed_search", "user_id": USER_ID,
        "group_id": group_id, "prompt_version": PROMPT_VERSION, "model": config.SEARCH_EXECUTION_MODEL,
        "effort": None, "input_json": json.dumps({"group": group.name, "pass": pass_no, "avoid": avoid}),
        "verdict_json": json.dumps({"candidates": len(candidates)}), "reasoning": "",
        "input_tokens": None, "output_tokens": None,
        "latency_ms": int((time.perf_counter() - started) * 1000), "error": error, "run_id": gen.run_id,
    })
    found["candidates"] = candidates
    found["raw_text"] = text[:6000]
    if error or not candidates:
        found["note"] = error or "research call returned no parseable candidates"
        _cache_put(key, found)
        return found
    results = [
        {"title": str(c.get("headline", "")), "url": str(c.get("source_url", "")),
         "snippet": f"{c.get('occurred_at', '')}: {c.get('summary', '')}".strip(": "),
         "source_name": str(c.get("source_name", "")) or search._domain_of(str(c.get("source_url", "")))}
        for c in candidates if str(c.get("source_url", "")).startswith("http")
    ]
    try:
        sel = judgments.judge_source_selection(
            gen.judge, group={"name": group.name, "description": group.description},
            expected_signals=("A dated, single development this group would discuss: a result, a deal "
                              "with a figure, an appointment, a ruling, a release, a filing, a price move."),
            domain_confidence="medium", results=results, user_id=USER_ID, group_id=group_id,
        )
    except JudgmentError as exc:
        found["note"] = f"Source judgment failed: {exc}"
        _cache_put(key, found)
        return found
    found["events"] = sel.get("events", [])
    found["excluded_count"] = sel.get("excluded_count", 0)
    found["note"] = sel.get("exclusion_notes", "")
    found["queries"] = []
    _cache_put(key, found)
    return found


def events_path(slug: str) -> Path:
    return OUT_DIR / f"events_{slug}.json"


def stage_events_for_group(gen: Gen, group: GroupDef) -> dict[str, Any]:
    path = events_path(group.slug)
    if path.exists() and not gen.force:
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"  {group.slug}: cached ({len(data['events'])} events)")
        return data

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    passes: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    fallbacks: dict[str, int] = {"used": 0}

    # Anything already verified for this group by an earlier run (the system-
    # pipeline calibration passes included) is free: reuse it.
    for vpath in sorted(CACHE_DIR.glob("verify_*.json")):
        ver = json.loads(vpath.read_text(encoding="utf-8"))
        if not ver.get("accepted"):
            continue
        if ver.get("group", "premier_league") != group.slug:
            continue
        ev = dict(ver["event"])
        mat = judge_event(gen, group, ev)
        if not mat.get("ok"):
            continue
        ev.update({"should_be_material": mat["is_material"], "materiality_score": mat["score"],
                   "materiality_reasoning": mat["reasoning"], "significance": mat["significance"],
                   "discovered_by": ver.get("discovered_by", "system_pipeline")})
        accepted.append(ev)
        seen_urls.add(_norm_url(ev["source_url"]))

    for pass_no in range(1, MAX_PASSES + 1):
        if len(accepted) >= TARGET_MIN:
            break
        found = directed_search(gen, group, pass_no, [e["headline"] for e in accepted])
        candidates = list(found.get("events") or [])
        passes.append({
            "pass": pass_no, "method": found.get("method", "directed_search"),
            "candidates_from_search": len(found.get("candidates", [])),
            "candidates": len(candidates),
            "excluded_by_source_selection": found.get("excluded_count", 0), "note": found.get("note", ""),
        })
        print(f"  {group.slug} pass {pass_no}: {len(found.get('candidates', []))} candidates found, "
              f"{len(candidates)} passed source selection. {gen.meter.line()}", flush=True)
        # Highest-confidence first, so the cap keeps the best-supported items.
        candidates.sort(key=lambda c: -float(c.get("confidence", 0) or 0))
        for cand in candidates:
            if len(accepted) >= gen.max_events:
                break
            url = str(cand.get("source_url", "")).strip()
            nu = _norm_url(url) if url else ""
            if nu and nu in seen_urls:
                rejected.append({"headline": cand.get("headline"), "url": url, "reason": "duplicate URL"})
                continue
            if any(_similar(cand.get("headline", ""), e["headline"]) for e in accepted):
                rejected.append({"headline": cand.get("headline"), "url": url, "reason": "duplicate story"})
                continue
            seen_urls.add(nu)
            ver = verify_candidate(gen, group, cand, fallbacks)
            if ver["accepted"]:
                ver["event"]["discovered_by"] = "directed_search"
            if not ver["accepted"]:
                rejected.append({"headline": cand.get("headline"), "url": url, "reason": ver["reason"]})
                print(f"    - rejected: {str(cand.get('headline', ''))[:70]} -- {ver['reason'][:80]}", flush=True)
                continue
            ev = dict(ver["event"])
            if any(_similar(ev["headline"], e["headline"]) for e in accepted):
                rejected.append({"headline": ev["headline"], "url": url, "reason": "duplicate story (after verification)"})
                continue
            mat = judge_event(gen, group, ev)
            if not mat.get("ok"):
                rejected.append({"headline": ev["headline"], "url": url, "reason": f"materiality failed: {mat.get('error')}"})
                continue
            ev.update({
                "should_be_material": mat["is_material"], "materiality_score": mat["score"],
                "materiality_reasoning": mat["reasoning"], "significance": mat["significance"],
            })
            accepted.append(ev)
            print(f"    + {ev['occurred_at']} {'M' if ev['should_be_material'] else '-'} "
                  f"{ev['headline'][:80]}  {gen.meter.line()}", flush=True)

    accepted.sort(key=lambda e: e["occurred_at"])
    data = {
        "group": {"slug": group.slug, "name": group.name, "description": group.description,
                  "poll_interval_minutes": group.poll_interval_minutes},
        "generated_at": _now(), "run_id": gen.run_id,
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "passes": passes, "rejected": rejected, "events": accepted,
        "short_of_target": len(accepted) < TARGET_MIN,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  {group.slug}: {len(accepted)} verified events "
          f"({sum(1 for e in accepted if e['should_be_material'])} material), {len(rejected)} rejected"
          + ("  ** SHORT OF 8 **" if data["short_of_target"] else ""))
    return data


def run_stage_events(gen: Gen, only: str | None) -> int:
    gen.meter.stage = "A"
    groups = [g for g in GROUPS if only is None or g.slug == only]
    pending = [g for g in groups if gen.force or not events_path(g.slug).exists()]
    print(f"Stage A: {len(groups)} group(s), {len(pending)} to search. "
          f"Estimate before running: ~{len(pending)} x (up to {MAX_PASSES} directed-search passes at ~$0.30 + "
          f"~{gen.max_events} verify at ~$0.015 + ~{MAX_SEARCH_FALLBACKS_PER_GROUP} search fallbacks at ~$0.05 "
          f"+ ~{gen.max_events} materiality at ~$0.03) ~= "
          f"${len(pending) * (MAX_PASSES * 0.30 + gen.max_events * 0.045 + MAX_SEARCH_FALLBACKS_PER_GROUP * 0.05):.2f}, "
          f"plus ~${STAGE_B_ESTIMATE:.2f} for stage B; ${gen.meter.spent:.2f} already spent by earlier runs.")
    done_cost = 0.0
    done = 0
    try:
        for group in groups:
            was_pending = group in pending
            before = gen.meter.spent
            if was_pending and done:
                avg = done_cost / done
                remaining = len([g for g in pending if g is not group and not events_path(g.slug).exists()])
                projected = gen.meter.spent + avg * (remaining + 1) + STAGE_B_ESTIMATE
                print(gen.meter.line(projected))
                if projected > gen.meter.budget:
                    print(f"STOP: projected total ${projected:.2f} exceeds the ${gen.meter.budget:.2f} budget "
                          f"after {done} group(s). Nothing further was run.")
                    return 3
            stage_events_for_group(gen, group)
            if was_pending:
                done_cost += gen.meter.spent - before
                done += 1
    except BudgetExceeded as exc:
        print(f"STOP: {exc}. Partial results are cached; re-run to resume with a higher --budget.")
        return 3
    print(gen.meter.line())
    return 0


# =============================================================================
# Stage B -- personas
# =============================================================================

PERSONA_SYSTEM = """\
You write ONE synthetic user fixture for an evaluation harness. The harness
replays real news events to a simulated reader and scores whether the system
under test correctly learns which domain terms this reader does and does not
understand. You are given the group, the verified events the reader will be
briefed on, the archetype, the reader's assigned name and register, and the
rules. You return the creative fields only; ids, events, numbering and noise
flags are set by code.

Every rule below is checked by code. A violation is sent back to you once with
the exact problem; a second violation discards the fixture.

1. `knows` and `does_not_know` are concrete domain terms -- jargon, mechanisms,
   measures, named rules or roles -- and EVERY term must appear VERBATIM
   (case-insensitive, as a whole word or phrase) in the headline or detail of
   at least one of the events given. Do not paraphrase, do not change the
   plural, do not invent. Lowercase, one to three words. No proper nouns: a
   club, company, driver, ticker or person is not a concept. Terms must be
   distinct and no term may be contained inside another term you list.
2. Counts by archetype -- well_informed: 8-12 knows / 2-4 does_not_know, the
   unknowns being the rare or deep terms; partially_informed: 4-6 / 4-6;
   barely_informed: 1-3 / 8-12; disengaged: 1-3 / 8-12.
3. `quantities` is a subset of knows + does_not_know holding ONLY terms that
   are numeric measures: a fee, a yield, a valuation, a lap time, a rate, a
   price, a count, a percentage. A concept that is not a number never goes
   here. An empty list is fine.
4. `style` values must fall inside the ranges given for the archetype
   (`style_ranges` in the input; a two-element list is [min, max]). p(first
   turn) is computed as ask + (1 - ask) * react_rate and must land in
   [0.25, 0.5] for BOTH ask rates. Beginners clarify more; experts check
   beliefs more. For the disengaged archetype the style block is overridden
   by code; return the ranges' minima.
5. `reading` is one of careful / skimmer / bouncer. Prefer a profile the
   group's other personas (`sibling_reading_profiles`) have not used.
6. `bio`: one or two sentences, plausible, present tense, no real public
   figures, consistent with the assigned name. `notes`: two to four sentences
   on why this person holds these terms and lacks those -- a coherent shape,
   like a match-goer who knows on-pitch language but not transfer-market
   mechanics. Plain register, as in the example given.
7. `subdomains`: partition knows + does_not_know into 3-6 subdomains -- short
   lowercase noun phrases a specialist would file the terms under (`viticulture
   & harvest`, `appellation rules`, `funding mechanics`). Every term in exactly
   one subdomain; you may add up to three anchor terms per subdomain that are
   real domain vocabulary not in your lists. `familiar_subdomains`: a weight
   0-1 per subdomain for how at home THIS person is there. Bands by archetype
   (`familiar_bands` in the input): well_informed 0.7-0.95 across most
   subdomains with at most one weaker (below 0.7); partially_informed a mix in
   0.2-0.8; barely_informed <= 0.2; disengaged <= 0.15. Two consistency rules
   override the bands: a subdomain holding a `knows` term must be weighted
   above 0.2 (for the two low archetypes, up to 0.35), and a subdomain holding
   a `does_not_know` term must be weighted below 0.8. `prior_knowledge` is
   computed by code as the vocabulary-weighted mean of these weights.
8. `reasoning`: one sentence on how you chose the term split.
"""

PERSONA_SCHEMA = json_schema(
    {
        "bio": {"type": "string"},
        "notes": {"type": "string"},
        "knows": {"type": "array", "items": {"type": "string"}},
        "does_not_know": {"type": "array", "items": {"type": "string"}},
        "quantities": {"type": "array", "items": {"type": "string"}},
        "subdomains": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "terms": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "terms"],
                "additionalProperties": False,
            },
        },
        "familiar_subdomains": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "weight": {"type": "number"}},
                "required": ["name", "weight"],
                "additionalProperties": False,
            },
        },
        "reading": {"type": "string", "enum": ["careful", "skimmer", "bouncer"]},
        "style": {
            "type": "object",
            "properties": {
                "median_words": {"type": "integer"},
                "drop_terminal_punct_rate": {"type": "number"},
                "ask_rate_default": {"type": "number"},
                "ask_rate_unknown_present": {"type": "number"},
                "react_rate": {"type": "number"},
                "continue_rate": {"type": "number"},
                "max_turns": {"type": "integer"},
                "clarify_share": {"type": "number"},
                "check_belief_share": {"type": "number"},
                "remark_share": {"type": "number"},
                "context_rate": {"type": "number"},
                "gap_admit_rate": {"type": "number"},
                "redirect_rate": {"type": "number"},
            },
            "required": list(STYLE_FIELDS),
            "additionalProperties": False,
        },
    },
    ["bio", "notes", "knows", "does_not_know", "quantities", "subdomains",
     "familiar_subdomains", "reading", "style"],
)

NOTES_EXAMPLE = (
    "Well-informed persona in a fast-moving domain. Reads funding news daily, so she carries "
    "deal structure and revenue vocabulary; the gaps are on the LP/fund side of the table, which "
    "she sees written about but has never had to use."
)
BIO_EXAMPLE = (
    "Seed-stage founder, second company. Reads TechCrunch and Crunchbase News every morning, "
    "sits on one other cap table as an angel."
)


@dataclass
class Slot:
    group_index: int
    group: GroupDef
    archetype_index: int
    archetype: str

    @property
    def global_index(self) -> int:
        return self.group_index * 4 + self.archetype_index

    @property
    def number(self) -> int:
        return FIRST_FIXTURE_NUMBER + self.global_index

    @property
    def display_name(self) -> str:
        return NAMES[self.global_index]

    @property
    def first(self) -> str:
        return self.display_name.split()[0].lower()

    @property
    def persona_id(self) -> str:
        return f"{self.first}_{self.group.slug}"

    @property
    def filename(self) -> str:
        return f"{self.number:02d}_{self.first}_{self.group.slug}.json"

    @property
    def lowercase(self) -> bool:
        return self.global_index % 8 in LOWERCASE_SLOTS

    @property
    def noise(self) -> dict[str, Any] | None:
        if self.archetype == "partially_informed" and self.group_index in NOISY_PARTIAL_GROUPS:
            return dict(ADE_NOISE)
        if self.archetype == "disengaged" and self.group_index in NOISY_DISENGAGED_GROUPS:
            return dict(THEO_NOISE)
        return None

    @property
    def seed(self) -> int:
        return 20260906 + self.global_index


def all_slots(only: str | None = None) -> list[Slot]:
    out = []
    for gi, g in enumerate(GROUPS):
        if only and g.slug != only:
            continue
        for ai, a in enumerate(ARCHETYPE_ORDER):
            out.append(Slot(gi, g, ai, a))
    return out


_SENTINEL_RAW = {
    "id": "__sentinel__", "display_name": "sentinel", "archetype": "disengaged",
    "group": {"name": "__sentinel__", "description": "", "vocabulary": ["x"]},
    "knows": [], "does_not_know": ["x"],
    "style": {"median_words": 5, "variance": 1.0, "lowercase": True, "ask_rate_default": 0,
              "ask_rate_unknown_present": 0, "react_rate": 0, "continue_rate": 0, "max_turns": 0},
    "events": [{"headline": "s", "detail": "s", "source_url": "http://sentinel.invalid",
                "source_name": "s", "should_be_material": False, "concepts": ["x"]}],
    "learning": {"schema": "v1", "prior_knowledge": 0.1, "memory_rate": 1.0},
}


def _norm_terms(terms: Any) -> list[str]:
    out: list[str] = []
    for t in terms or []:
        c = re.sub(r"\s+", " ", str(t)).strip().lower()
        if c and c not in out:
            out.append(c)
    return out


def check_output(out: dict[str, Any], slot: Slot, events: list[dict[str, Any]],
                 sibling_readings: list[str]) -> list[str]:
    """The fixture rules, as code. Returns problems; empty means clean."""
    problems: list[str] = []
    knows = _norm_terms(out.get("knows"))
    dnk = _norm_terms(out.get("does_not_know"))
    quantities = _norm_terms(out.get("quantities"))
    (klo, khi), (dlo, dhi) = TERM_COUNTS[slot.archetype]
    if not klo <= len(knows) <= khi:
        problems.append(f"{slot.archetype} needs {klo}-{khi} knows; got {len(knows)}")
    if not dlo <= len(dnk) <= dhi:
        problems.append(f"{slot.archetype} needs {dlo}-{dhi} does_not_know; got {len(dnk)}")
    overlap = sorted(set(knows) & set(dnk))
    if overlap:
        problems.append(f"in both knows and does_not_know: {overlap}")
    universe = knows + dnk
    texts = [f"{e['headline']} {e['detail']}" for e in events]
    for t in universe:
        if len(t.split()) > 3:
            problems.append(f"term longer than three words: {t!r}")
        if not any(terms_in(text, (t,)) for text in texts):
            problems.append(f"term {t!r} does not appear verbatim in any event's headline or detail")
    for a in universe:
        for b in universe:
            if a != b and terms_in(b, (a,)):
                problems.append(f"term {a!r} is contained inside term {b!r}")
    stray = sorted(set(quantities) - set(universe))
    if stray:
        problems.append(f"quantities not in knows/does_not_know: {stray}")
    problems += check_subdomains(
        slot.archetype,
        taxonomy_from_pairs(out.get("subdomains")),
        weights_from_pairs(out.get("familiar_subdomains")),
        knows, dnk,
    )
    if out.get("reading") not in READING_PROFILES:
        problems.append(f"reading must be one of {sorted(READING_PROFILES)}")
    if slot.archetype != "disengaged":
        style = out.get("style") or {}
        ranges = STYLE_RANGES[slot.archetype]
        for name, (lo, hi) in ranges.items():
            v = style.get(name)
            if v is None:
                problems.append(f"style.{name} missing")
                continue
            if not lo <= float(v) <= hi:
                problems.append(f"style.{name}={v} outside [{lo}, {hi}]")
        if not problems:
            spec = _StyleView(style)
            for unknown_present in (False, True):
                p = p_first_turn(spec, unknown_present)
                if not P_FIRST_TURN_RANGE[0] <= p <= P_FIRST_TURN_RANGE[1]:
                    problems.append(
                        f"p(first turn | unknown_present={unknown_present}) = {p:.3f}, "
                        f"must be within {P_FIRST_TURN_RANGE}"
                    )
    if not str(out.get("bio", "")).strip() or not str(out.get("notes", "")).strip():
        problems.append("bio and notes must be non-empty")
    return problems


class _StyleView:
    def __init__(self, raw: dict[str, Any]):
        for k, v in raw.items():
            setattr(self, k, v)


def build_fixture(out: dict[str, Any], slot: Slot, events: list[dict[str, Any]]) -> dict[str, Any]:
    knows = _norm_terms(out["knows"])
    dnk = _norm_terms(out["does_not_know"])
    quantities = sorted(set(_norm_terms(out.get("quantities"))) & set(knows + dnk))
    vocabulary = knows + dnk
    subdomains = taxonomy_from_pairs(out.get("subdomains"))
    familiar = weights_from_pairs(out.get("familiar_subdomains"))
    if slot.archetype == "disengaged":
        style: dict[str, Any] = dict(DISENGAGED_STYLE)
    else:
        raw_style = out["style"]
        style = {}
        for name in STYLE_FIELDS:
            v = raw_style[name]
            style[name] = int(v) if name in INT_STYLE_FIELDS else round(float(v), 3)
        style["variance"] = 1.0
    style["lowercase"] = slot.lowercase
    ordered_style = {
        "median_words": style["median_words"], "variance": style["variance"],
        "lowercase": style["lowercase"],
        "drop_terminal_punct_rate": style["drop_terminal_punct_rate"],
        "ask_rate_default": style["ask_rate_default"],
        "ask_rate_unknown_present": style["ask_rate_unknown_present"],
        "react_rate": style["react_rate"], "continue_rate": style["continue_rate"],
        "max_turns": style["max_turns"], "clarify_share": style["clarify_share"],
        "check_belief_share": style["check_belief_share"], "remark_share": style["remark_share"],
        "context_rate": style["context_rate"], "gap_admit_rate": style["gap_admit_rate"],
        "redirect_rate": style["redirect_rate"],
    }
    fixture_events = []
    for i, e in enumerate(events):
        fixture_events.append({
            "headline": e["headline"], "detail": e["detail"], "source_url": e["source_url"],
            "source_name": e["source_name"], "should_be_material": bool(e["should_be_material"]),
            "significance": e["significance"],
            "concepts": terms_in(f"{e['headline']} {e['detail']}", tuple(vocabulary)),
            "occurred_hours_ago": round(2.0 + 1.5 * i, 1),
        })
    notes = (
        str(out["notes"]).strip()
        + f" Generated by harness/persona_gen.py from events_{slot.group.slug}.json "
        f"(real, verified {WINDOW_START:%-d %B}-{WINDOW_END:%-d %B %Y} items found by the system's own "
        "search and judged by its own materiality call); the group name carries the persona's first "
        "name only because the fixture loader requires group names to be unique per suite."
    )
    fixture: dict[str, Any] = {
        "id": slot.persona_id,
        "display_name": slot.display_name,
        "archetype": slot.archetype,
        "notes": notes,
        "profile": {"bio": str(out["bio"]).strip()},
        "seed": slot.seed,
        "interactions": 8,
        "group": {
            "name": f"{slot.group.name} ({slot.display_name.split()[0]})",
            "description": slot.group.description,
            "poll_interval_minutes": slot.group.poll_interval_minutes,
            "vocabulary": vocabulary,
            "subdomains": subdomains,
        },
        "knows": knows,
        "does_not_know": dnk,
        "familiar_subdomains": familiar,
    }
    if slot.noise:
        fixture["noise"] = slot.noise
    fixture["style"] = ordered_style
    fixture["events"] = fixture_events
    fixture["reading"] = out["reading"]
    fixture["quantities"] = quantities
    fixture["learning"] = learning_block(
        slot.archetype, len(knows), len(dnk), slot.global_index,
        vocabulary=vocabulary, subdomains=subdomains, familiar=familiar,
    )
    return fixture


def validate_fixture(fixture: dict[str, Any]) -> str | None:
    """Construct the real Persona and run the loader's checks. None if clean."""
    try:
        persona = _parse(fixture)
        _validate([persona, _parse(_SENTINEL_RAW)])
    except FixtureError as exc:
        return str(exc)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _load_failures() -> dict[str, Any]:
    return _cache_get("failures") or {}


def _record_failure(slot: Slot, problems: list[str]) -> None:
    failures = _load_failures()
    failures[slot.persona_id] = {"file": slot.filename, "archetype": slot.archetype,
                                 "group": slot.group.slug, "problems": problems, "at": _now()}
    _cache_put("failures", failures)


def _clear_failure(slot: Slot) -> None:
    failures = _load_failures()
    if slot.persona_id in failures:
        del failures[slot.persona_id]
        _cache_put("failures", failures)


def generate_persona(gen: Gen, slot: Slot, events: list[dict[str, Any]],
                     sibling_readings: list[str]) -> dict[str, Any] | None:
    path = OUT_DIR / slot.filename
    if path.exists() and not gen.force:
        print(f"  {slot.filename}: cached")
        return json.loads(path.read_text(encoding="utf-8"))

    event_view = [
        {"headline": e["headline"], "detail": e["detail"], "source_name": e["source_name"],
         "occurred_at": e["occurred_at"]}
        for e in events
    ]
    context: dict[str, Any] = {
        "group": {"name": slot.group.name, "description": slot.group.description},
        "archetype": slot.archetype,
        "assigned_name": slot.display_name,
        "register": {"lowercase_initial_always": slot.lowercase},
        "noise": (
            "This persona is the group's noisy one: it stays silent about some terms it does not "
            "understand and sometimes asks about terms it does. Mention that in notes."
            if slot.noise and "silent_when_ignorant_rate" in slot.noise else
            "This persona reads and never says anything at all. Mention that in notes."
            if slot.noise else None
        ),
        "term_counts": {"knows": TERM_COUNTS[slot.archetype][0], "does_not_know": TERM_COUNTS[slot.archetype][1]},
        "style_ranges": STYLE_RANGES.get(slot.archetype) or STYLE_RANGES["barely_informed"],
        "p_first_turn_range": P_FIRST_TURN_RANGE,
        "familiar_bands": FAMILIAR_BANDS[slot.archetype],
        "subdomain_count_range": SUBDOMAIN_COUNT_RANGE,
        "sibling_reading_profiles": sibling_readings,
        "example_bio": BIO_EXAMPLE,
        "example_notes": NOTES_EXAMPLE,
        "events": event_view,
    }
    problems: list[str] = []
    out: dict[str, Any] = {}
    fixture: dict[str, Any] | None = None
    for attempt in range(2):
        ctx = dict(context)
        if problems:
            ctx["previous_attempt"] = out
            ctx["previous_attempt_problems"] = problems
        try:
            out = logged_call(gen, "persona_gen.persona", system=PERSONA_SYSTEM, context=ctx,
                              schema=PERSONA_SCHEMA, model=config.SONNET, effort="medium",
                              group_id=f"gen_{slot.group.slug}", max_tokens=16000)
        except GenError as exc:
            problems = [str(exc)]
            continue
        _cache_put(f"persona_{slot.persona_id}_attempt{attempt + 1}", out)
        problems = check_output(out, slot, events, sibling_readings)
        if not problems:
            try:
                candidate = build_fixture(out, slot, events)
            except GenError as exc:
                problems = [str(exc)]
                continue
            err = validate_fixture(candidate)
            if err:
                problems = [f"Persona construction failed: {err}"]
            else:
                fixture = candidate
                break
        print(f"  {slot.filename}: attempt {attempt + 1} rejected -- " + "; ".join(problems)[:200])
    if fixture is None:
        _record_failure(slot, problems)
        print(f"  {slot.filename}: SKIPPED after retry")
        return None
    _clear_failure(slot)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  {slot.filename}: ok ({len(fixture['knows'])} knows / {len(fixture['does_not_know'])} "
          f"does_not_know, reading={fixture['reading']}"
          + (", NOISY" if fixture.get("noise") else "") + f")  {gen.meter.line()}")
    return fixture


def run_stage_personas(gen: Gen, only: str | None) -> int:
    gen.meter.stage = "B"
    slots = all_slots(only)
    missing_groups = sorted({s.group.slug for s in slots if not events_path(s.group.slug).exists()})
    if missing_groups:
        print(f"Stage B: no events file for {missing_groups}; run `events` first.")
        return 2
    print(f"Stage B: {len(slots)} persona slot(s).")
    try:
        by_group: dict[str, list[str]] = {}
        for slot in slots:
            data = json.loads(events_path(slot.group.slug).read_text(encoding="utf-8"))
            events = data["events"]
            if not events:
                print(f"  {slot.filename}: no events for group; skipped")
                _record_failure(slot, ["group has no verified events"])
                continue
            fixture = generate_persona(gen, slot, events, by_group.get(slot.group.slug, []))
            if fixture is not None:
                by_group.setdefault(slot.group.slug, []).append(fixture["reading"])
    except BudgetExceeded as exc:
        print(f"STOP: {exc}")
        return 3
    print(gen.meter.line())
    return suite_check()


def rewrite_learning(only: str | None) -> int:
    """Rewrite the `learning` block in place on fixtures already on disk. Nothing else changes."""
    by_id = {slot.persona_id: slot for slot in all_slots(only)}
    n = 0
    for path in sorted(OUT_DIR.glob("*.json")):
        if path.name.startswith("events_"):
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        slot = by_id.get(raw.get("id"))
        if slot is None:
            continue
        block = learning_block(
            slot.archetype, len(raw["knows"]), len(raw["does_not_know"]), slot.global_index,
            vocabulary=list(raw["group"].get("vocabulary", [])),
            subdomains=raw["group"].get("subdomains"),
            familiar=raw.get("familiar_subdomains"),
        )
        if raw.get("learning") != block:
            raw["learning"] = block
            path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            n += 1
    print(f"learning: rewrote {n} fixture(s).")
    return suite_check()


def load_generated() -> list[Persona]:
    return [_parse(json.loads(p.read_text(encoding="utf-8"))) for p in sorted(OUT_DIR.glob("*.json"))
            if not p.name.startswith("events_")]


def suite_check() -> int:
    """Whole-suite validation, the way `personas.load` would see the directory."""
    try:
        personas = load_generated()
        _validate(personas)
    except FixtureError as exc:
        print(f"Suite validation FAILED: {exc}")
        return 1
    print(f"Suite validation ok: {len(personas)} fixtures, "
          f"{sum(1 for p in personas if p.is_noisy)} noisy.")
    return 0


# =============================================================================
# Stage C -- subdomain backfill for fixtures already on disk (one-off)
# =============================================================================
#
# The 16 generated fixtures predate `group.subdomains` / `familiar_subdomains`.
# Rather than a term-matching heuristic (the human's direction is that the
# glossary is NOT a word-to-word match), one Sonnet call per group proposes
# the taxonomy over the union of the group's four vocabularies and a weight
# per persona per subdomain from the existing knows / does_not_know, bios and
# notes. Four calls, cached in `_cache/subdomains_<slug>.json`; re-running
# costs nothing. `prior_knowledge` is then recomputed from the weights.

SUBDOMAIN_SYSTEM = """\
You partition one news group's glossary into subdomains and say how at home
each of several synthetic readers is in each subdomain. The output backfills
evaluation fixtures whose readers were previously described only by two
lists, `knows` and `does_not_know`; you are turning those lists into a shape.

Input: the group (name, description), the union vocabulary (every term any of
the readers is scored on), and each reader with archetype, bio, notes, and
their `knows` / `does_not_know` lists.

Rules, all checked by code (a violation is sent back once with the problem):

1. `subdomains`: 3-6 entries, each a short lowercase noun phrase (at most four
   words, "&" not counted) a specialist would file the terms under -- e.g. for a wine group
   `viticulture & harvest`, `appellation rules`, `winemaking & chemistry`,
   `commercial & market`. Every vocabulary term in EXACTLY one subdomain, spelt
   exactly as given. You may add up to three anchor terms per subdomain that
   are real domain vocabulary not in the union list. Some union terms are
   plainly off-topic for the group (an NFL term in a Premier League group);
   give them a subdomain anyway -- a fifth or sixth one named for what they
   are is fine.
2. `personas`: for every reader, one weight 0-1 per subdomain (every subdomain
   named), using the reader ids exactly as given. Bands by archetype:
   well_informed 0.70-0.95 across most subdomains with at most one weaker
   (below 0.70); partially_informed a mix, each in
   0.20-0.80; barely_informed <= 0.20; disengaged <= 0.15.
3. Two consistency rules override the bands: a subdomain that holds one of a
   reader's `knows` terms must be weighted ABOVE 0.20 for that reader (for
   barely_informed / disengaged readers, up to 0.35); a subdomain that holds
   one of a reader's `does_not_know` terms must be weighted BELOW 0.80.
   Where a reader's lists put a knows term and a does_not_know term in the
   same subdomain, the weight has to sit in (0.20, 0.80) -- choose the split
   of terms so this is rare and coherent.
4. Weights should follow the bio and notes: a reader described as strong on
   the pitch and weak on transfer mechanics gets a high weight on the on-pitch
   subdomain and a low one on deal mechanics.
5. `reasoning`: two or three sentences on the taxonomy.
"""

SUBDOMAIN_SCHEMA = json_schema(
    {
        "subdomains": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "terms": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "terms"],
                "additionalProperties": False,
            },
        },
        "personas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "familiar_subdomains": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "weight": {"type": "number"},
                            },
                            "required": ["name", "weight"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "familiar_subdomains"],
                "additionalProperties": False,
            },
        },
    },
    ["subdomains", "personas"],
)

BACKFILL_LOG = "subdomains_backfill"


def _fixtures_for_group(slug: str) -> list[tuple[Path, dict[str, Any]]]:
    out = []
    for path in sorted(OUT_DIR.glob("*.json")):
        if path.name.startswith("events_"):
            continue
        if path.stem.split("_", 2)[2] == slug:
            out.append((path, json.loads(path.read_text(encoding="utf-8"))))
    return out


def check_group_subdomains(group: GroupDef, fixtures: list[tuple[Path, dict[str, Any]]],
                           out: dict[str, Any]) -> tuple[list[str], dict[str, list[str]], dict[str, dict[str, float]]]:
    """Validate one group's proposal against every persona. Returns
    (problems, taxonomy, {persona_id: weights})."""
    problems: list[str] = []
    taxonomy = taxonomy_from_pairs(out.get("subdomains"))
    by_id = {str(p.get("id")): weights_from_pairs(p.get("familiar_subdomains"))
             for p in out.get("personas", []) if isinstance(p, dict)}
    weights: dict[str, dict[str, float]] = {}
    slots = {s.persona_id: s for s in all_slots()}
    for _, raw in fixtures:
        pid = raw["id"]
        if pid not in by_id:
            problems.append(f"no familiar_subdomains for persona {pid!r}")
            continue
        w = by_id[pid]
        knows, dnk = _norm_terms(raw["knows"]), _norm_terms(raw["does_not_know"])
        for prob in check_subdomains(raw["archetype"], taxonomy, w, knows, dnk):
            problems.append(f"{pid}: {prob}")
        if not problems:
            try:
                SubdomainMap.from_raw(taxonomy, w, persona_id=pid,
                                      vocabulary=knows + dnk, knows=knows, does_not_know=dnk)
                slot = slots.get(pid)
                if slot is not None:
                    learning_block(raw["archetype"], len(knows), len(dnk), slot.global_index,
                                   vocabulary=knows + dnk, subdomains=taxonomy, familiar=w)
            except (FixtureError, GenError) as exc:
                problems.append(f"{pid}: {exc}")
        weights[pid] = w
    return problems, taxonomy, weights


def run_stage_subdomains(gen: Gen, only: str | None) -> int:
    gen.meter.stage = "C"
    log = _cache_get(BACKFILL_LOG) or {}
    slugs = [g.slug for g in GROUPS if (only is None or g.slug == only) and _fixtures_for_group(g.slug)]
    print(f"Stage C: subdomain backfill for {len(slugs)} group(s) with fixtures on disk.")
    slots = {s.persona_id: s for s in all_slots()}
    for slug in slugs:
        group = GROUP_BY_SLUG[slug]
        fixtures = _fixtures_for_group(slug)
        cache_name = f"subdomains_{slug}"
        cached = None if gen.force else _cache_get(cache_name)
        union = list(dict.fromkeys(t for _, raw in fixtures for t in raw["group"]["vocabulary"]))
        context: dict[str, Any] = {
            "group": {"name": group.name, "description": group.description},
            "union_vocabulary": union,
            "readers": [
                {
                    "id": raw["id"], "archetype": raw["archetype"],
                    "bio": raw.get("profile", {}).get("bio", ""),
                    "notes": raw.get("notes", "").split(" Generated by harness/persona_gen.py")[0],
                    "knows": raw["knows"], "does_not_know": raw["does_not_know"],
                }
                for _, raw in fixtures
            ],
            "familiar_bands": FAMILIAR_BANDS,
            "subdomain_count_range": SUBDOMAIN_COUNT_RANGE,
        }
        problems: list[str] = []
        out: dict[str, Any] = {}
        accepted = None
        calls = 0
        cached_run = gen.run_id
        if cached and cached.get("out"):
            # Re-validated under the CURRENT rules: a proposal rejected by an
            # over-strict earlier rule is accepted without another call.
            out = cached["out"]
            problems, taxonomy, weights = check_group_subdomains(group, fixtures, out)
            if not problems:
                accepted = (taxonomy, weights)
                # The call that produced it is the one to record, not this
                # re-validation.
                calls = int(cached.get("attempt", 1) or 1)
                cached_run = str(cached.get("run_id", gen.run_id))
                print(f"  {slug}: cached proposal ({len(taxonomy)} subdomains, "
                      f"{calls} call(s) in {cached_run})")
        if accepted is None:
            for attempt in range(2):
                ctx = dict(context)
                if problems:
                    ctx["previous_attempt"] = out
                    ctx["previous_attempt_problems"] = problems
                try:
                    out = logged_call(gen, "persona_gen.subdomains", system=SUBDOMAIN_SYSTEM, context=ctx,
                                      schema=SUBDOMAIN_SCHEMA, model=config.SONNET, effort="medium",
                                      group_id=f"gen_{slug}", max_tokens=8000)
                except GenError as exc:
                    problems = [str(exc)]
                    calls += 1
                    continue
                calls += 1
                problems, taxonomy, weights = check_group_subdomains(group, fixtures, out)
                _cache_put(cache_name, {"out": out, "problems": problems, "at": _now(),
                                        "attempt": attempt + 1, "run_id": gen.run_id})
                if not problems:
                    accepted = (taxonomy, weights)
                    break
                print(f"  {slug}: attempt {attempt + 1} rejected -- " + "; ".join(problems)[:300])
        if accepted is None:
            log[slug] = {"status": "failed", "problems": problems, "at": _now(), "calls": calls,
                         "run_id": gen.run_id}
            _cache_put(BACKFILL_LOG, log)
            print(f"  {slug}: FAILED after retry; fixtures unchanged")
            continue
        taxonomy, weights = accepted
        changed_k: list[str] = []
        for path, raw in fixtures:
            pid = raw["id"]
            knows, dnk = _norm_terms(raw["knows"]), _norm_terms(raw["does_not_know"])
            group_block = {}
            for k, v in raw["group"].items():
                group_block[k] = v
                if k == "vocabulary":
                    group_block["subdomains"] = {n: list(t) for n, t in taxonomy.items()}
            group_block.setdefault("subdomains", {n: list(t) for n, t in taxonomy.items()})
            rebuilt: dict[str, Any] = {}
            for k, v in raw.items():
                if k == "group":
                    rebuilt[k] = group_block
                elif k == "familiar_subdomains":
                    continue
                else:
                    rebuilt[k] = v
                if k == "does_not_know":
                    rebuilt["familiar_subdomains"] = dict(weights[pid])
            rebuilt.setdefault("familiar_subdomains", dict(weights[pid]))
            old_k = raw["learning"]["prior_knowledge"]
            rebuilt["learning"] = learning_block(
                raw["archetype"], len(knows), len(dnk), slots[pid].global_index,
                vocabulary=knows + dnk, subdomains=taxonomy, familiar=weights[pid],
            )
            if rebuilt["learning"]["prior_knowledge"] != old_k:
                changed_k.append(f"{pid} K {old_k} -> {rebuilt['learning']['prior_knowledge']}")
            err = validate_fixture(rebuilt)
            if err:
                print(f"  {slug}: {path.name} would not validate after backfill: {err}")
                log[slug] = {"status": "failed", "problems": [err], "at": _now(), "calls": calls,
                             "run_id": gen.run_id}
                _cache_put(BACKFILL_LOG, log)
                break
            path.write_text(json.dumps(rebuilt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        else:
            # A re-run over fixtures already backfilled sees no K change; keep
            # the record of the change the first pass made.
            previous = (log.get(slug) or {}).get("k_changes") or []
            log[slug] = {"status": "ok", "at": _now(), "calls": calls, "run_id": cached_run,
                         "subdomains": {n: list(t) for n, t in taxonomy.items()},
                         "k_changes": changed_k or previous,
                         "reasoning": str(out.get("reasoning", ""))}
            _cache_put(BACKFILL_LOG, log)
            print(f"  {slug}: ok -- {len(taxonomy)} subdomains {sorted(taxonomy)}; "
                  f"K changed on {len(changed_k)} fixture(s)  {gen.meter.line()}")
    print(gen.meter.line())
    return suite_check()


# =============================================================================
# REVIEW.md
# =============================================================================


def sample_question(persona: Persona) -> tuple[str, str]:
    responder = ScriptedResponder()
    for ev in persona.events[:4]:
        text = f"{ev.headline} {ev.detail}"
        view = BriefingView(exchange_id="review", topic=persona.group.name, briefing=text)
        for index in range(40):
            state = PersonaState.for_persona(persona)
            intent = responder.plan_turn(persona=persona, state=state, briefing=view, index=index,
                                         turn_no=0, visible_text=text)
            if intent is not None and intent.kind == "question":
                return intent.text, intent.form
    if persona.style.max_turns == 0:
        return "(no turn -- configured to stay silent)", "-"
    return "(no question drawn in 160 tries)", "-"


def _md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def write_review() -> int:
    fixtures_by_group: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for p in sorted(OUT_DIR.glob("*.json")):
        if p.name.startswith("events_"):
            continue
        raw = json.loads(p.read_text(encoding="utf-8"))
        slug = p.stem.split("_", 2)[2]
        fixtures_by_group.setdefault(slug, []).append((p, raw))
    failures = _load_failures()
    totals = ledger_totals()

    lines: list[str] = []
    lines.append("# Generated persona set -- human review sheet")
    lines.append("")
    lines.append(f"Generated {_now()} by `harness/persona_gen.py`. Events window "
                 f"{WINDOW_START.isoformat()} to {WINDOW_END.isoformat()}. Every event below was "
                 "passed through the system's own `source_selection` judgment, re-read from its cited "
                 "page, dated from that page, and judged by the system's own `materiality` call with "
                 "the group description as the only context. **Candidate finding is NOT the system's "
                 "query-formulation pipeline** except where `Found by` says `system_pipeline`: that "
                 "pipeline was run five times on Premier League and yielded two verified events for "
                 "about $2 (see the module docstring for the two causes, both findings about the "
                 "system); the rest come from a directed web-search research call per group. "
                 "`should_be_material` in the fixtures is the judge's verdict, not a human's -- "
                 "fill the **Human verdict** column and correct the fixture where you disagree.")
    lines.append("")
    lines.append("Run the set: `PYTHONPATH=src:. .venv/bin/python -m harness.run --fixture-dir "
                 "harness/personas/generated --rounds 2`")
    lines.append("")
    lines.append("Significance is derived from the judge's score (>=76 major, 40-75 borderline, "
                 "<40 minor). The `learning` block on every fixture is schema v1: `prior_knowledge` "
                 "(K) is the archetype's prior, clamped to its band and within 0.1 of "
                 "|knows| / (|knows| + |does_not_know|); `memory_rate` (m) is a seeded uniform draw "
                 "in [0.7, 1.4] independent of archetype, with four fast forgetters (m <= 0.8) and "
                 "four slow forgetters (m >= 1.3) pinned across groups -- see the learning table in "
                 "the summary.")
    lines.append("")

    per_group_counts: list[tuple[str, int, int, int, bool]] = []
    learning_rows: list[tuple[Any, ...]] = []
    total_events = 0
    persona_count = 0
    noisy: list[str] = []
    archetype_counts: dict[str, int] = {}

    for gi, group in enumerate(GROUPS):
        path = events_path(group.slug)
        lines.append(f"## {gi + 1}. {group.name} (`{group.slug}`)")
        lines.append("")
        lines.append(f"> {group.description}")
        lines.append("")
        if not path.exists():
            lines.append("_No events file -- stage A did not reach this group._")
            lines.append("")
            per_group_counts.append((group.slug, 0, 0, 0, True))
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        events = data["events"]
        total_events += len(events)
        material = sum(1 for e in events if e["should_be_material"])
        per_group_counts.append((group.slug, len(events), material, len(data.get("rejected", [])),
                                 data.get("short_of_target", False)))
        lines.append(f"### Events ({len(events)} verified, {material} judged material, "
                     f"{len(data.get('rejected', []))} candidates rejected)"
                     + ("  -- **short of 8**" if data.get("short_of_target") else ""))
        lines.append("")
        lines.append("| # | Date | Headline | Source | Found by | Judge | Judge's reason | Human verdict |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for i, e in enumerate(events, 1):
            verdict = f"{'material' if e['should_be_material'] else 'not material'} ({e['materiality_score']:.0f})"
            reason = _md(e.get("materiality_reasoning", ""))
            if len(reason) > 220:
                reason = reason[:217] + "..."
            lines.append(f"| {i} | {e['occurred_at']} | {_md(e['headline'])} | "
                         f"[{_md(e['source_name'])}]({e['source_url']}) | {e.get('discovered_by', '?')} | "
                         f"{verdict} | {reason} |  |")
        lines.append("")
        rejected = data.get("rejected", [])
        if rejected:
            lines.append("<details><summary>Rejected candidates</summary>")
            lines.append("")
            for r in rejected:
                lines.append(f"- {_md(r.get('headline') or '(no headline)')} -- {_md(r.get('reason'))}"
                             + (f" -- <{r['url']}>" if r.get("url") else ""))
            lines.append("")
            lines.append("</details>")
            lines.append("")
        queries = [q for p in data.get("passes", []) for q in p.get("queries", [])]
        if queries:
            lines.append("<details><summary>Search queries the system wrote</summary>")
            lines.append("")
            for q in queries:
                lines.append(f"- {_md(q)}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        group_fixtures = fixtures_by_group.get(group.slug, [])
        taxonomy = next((f[1]["group"].get("subdomains") for f in group_fixtures
                         if f[1]["group"].get("subdomains")), None)
        if taxonomy:
            lines.append("### Subdomains (backfilled, one Sonnet call per group -- see the summary)")
            lines.append("")
            for name, terms in taxonomy.items():
                lines.append(f"- `{name}`: " + ", ".join(f"`{t}`" for t in terms))
            lines.append("")
        lines.append("### Personas")
        lines.append("")
        for path_p, raw in group_fixtures:
            persona = _parse(raw)
            persona_count += 1
            archetype_counts[persona.archetype] = archetype_counts.get(persona.archetype, 0) + 1
            if persona.is_noisy:
                noisy.append(persona.id)
            question, form = sample_question(persona)
            noise_desc = "no"
            if raw.get("noise"):
                noise_desc = "yes -- " + ", ".join(f"{k}={v}" for k, v in raw["noise"].items())
            lines.append(f"#### `{path_p.name}` -- {persona.display_name} -- {persona.archetype}")
            lines.append("")
            lines.append(f"- reading: `{persona.reading.name}`; lowercase: {str(persona.style.lowercase).lower()}; "
                         f"noisy: {noise_desc}")
            lines.append(f"- knows ({len(persona.knows)}): " + ", ".join(f"`{t}`" for t in persona.knows))
            lines.append(f"- does_not_know ({len(persona.does_not_know)}): "
                         + ", ".join(f"`{t}`" for t in persona.does_not_know))
            lines.append("- quantities: " + (", ".join(f"`{t}`" for t in sorted(persona.quantities)) or "(none)"))
            lb = raw.get("learning", {})
            tag = forgetter_label(float(lb.get("memory_rate", 1.0)))
            lines.append(f"- learning: K={lb.get('prior_knowledge')}, m={lb.get('memory_rate')}"
                         + (f" (**{tag}**)" if tag else ""))
            fam = raw.get("familiar_subdomains") or {}
            if fam:
                lines.append("- familiar_subdomains: "
                             + ", ".join(f"`{n}` {w:.2f}" for n, w in fam.items())
                             + f" (K = vocabulary-weighted mean = "
                             f"{persona.subdomain_map.vocabulary_weighted_mean(persona.group.vocabulary):.2f})")
            else:
                lines.append("- familiar_subdomains: (none -- legacy fixture, K from the term split)")
            learning_rows.append((path_p.name, persona.display_name, persona.archetype, group.slug,
                                  lb.get("prior_knowledge"), lb.get("memory_rate"), tag))
            lines.append(f"- sample question (form `{form}`): _{_md(question)}_")
            lines.append(f"- bio: {_md(raw.get('profile', {}).get('bio', ''))}")
            lines.append(f"- notes: {_md(raw.get('notes', '').split(' Generated by harness/persona_gen.py')[0])}")
            lines.append("")
        group_failures = {k: v for k, v in failures.items() if v.get("group") == group.slug}
        for f in group_failures.values():
            lines.append(f"#### `{f['file']}` -- FAILED VALIDATION ({f['archetype']})")
            lines.append("")
            for p in f.get("problems", []):
                lines.append(f"- {_md(p)}")
            lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Groups: {len(GROUPS)}; events files present: "
                 f"{sum(1 for g in GROUPS if events_path(g.slug).exists())}; verified events: {total_events}.")
    lines.append("")
    lines.append("| Group | Events | Material | Rejected | Short of 8 |")
    lines.append("|---|---|---|---|---|")
    for slug, n, m, r, short in per_group_counts:
        lines.append(f"| `{slug}` | {n} | {m} | {r} | {'**yes**' if short else 'no'} |")
    lines.append("")
    short = [slug for slug, n, m, r, s in per_group_counts if s]
    lines.append(f"- Groups short of 8 events: {', '.join(f'`{s}`' for s in short) if short else 'none'}.")
    lines.append(f"- Personas written: {persona_count} "
                 + "(" + ", ".join(f"{k} {v}" for k, v in sorted(archetype_counts.items())) + ")."
                 if archetype_counts else f"- Personas written: {persona_count}.")
    lines.append(f"- Noisy personas ({len(noisy)}): " + (", ".join(f"`{p}`" for p in noisy) or "none") + ".")
    if failures:
        lines.append(f"- Personas that failed validation ({len(failures)}): "
                     + ", ".join(f"`{f['file']}`" for f in failures.values()) + " (details above).")
    else:
        lines.append("- Personas that failed validation: none.")
    lines.append("")
    lines.append("### Learning traits (schema v1)")
    lines.append("")
    fast = [r for r in learning_rows if r[6] == "fast forgetter"]
    slow = [r for r in learning_rows if r[6] == "slow forgetter"]
    lines.append(f"- Fast forgetters (m <= 0.8), {len(fast)}: "
                 + (", ".join(f"`{r[0]}` (m={r[5]})" for r in fast) or "none") + ".")
    lines.append(f"- Slow forgetters (m >= 1.3), {len(slow)}: "
                 + (", ".join(f"`{r[0]}` (m={r[5]})" for r in slow) or "none") + ".")
    lines.append("")
    lines.append("| File | Persona | Archetype | Group | K (prior_knowledge) | m (memory_rate) | |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in learning_rows:
        lines.append(f"| `{r[0]}` | {r[1]} | {r[2]} | `{r[3]}` | {r[4]} | {r[5]} | {r[6]} |")
    lines.append("")
    backfill = _cache_get(BACKFILL_LOG) or {}
    lines.append("### Subdomain backfill (stage C)")
    lines.append("")
    lines.append("`group.subdomains` / `familiar_subdomains` were added to fixtures already on disk by "
                 "`python -m harness.persona_gen subdomains`: ONE `claude-sonnet-5` call per group "
                 "(retried once on a rule violation) proposing the taxonomy over the union of the "
                 "group's four vocabularies and a weight per persona from the existing knows / "
                 "does_not_know, bio and notes. No term-matching heuristic was used. `prior_knowledge` "
                 "was then recomputed as the vocabulary-weighted mean of the weights (clamped to the "
                 "archetype band); changes are listed. The calls are in the cost ledger under stage C.")
    lines.append("")
    if backfill:
        lines.append("| Group | Status | Calls | Run | Subdomains | K changes |")
        lines.append("|---|---|---|---|---|---|")
        for slug, entry in sorted(backfill.items()):
            subs = ", ".join(f"`{n}`" for n in (entry.get("subdomains") or {}))
            kc = "; ".join(entry.get("k_changes") or []) or "none"
            probs = "; ".join(entry.get("problems") or [])
            lines.append(f"| `{slug}` | {entry.get('status')}{(' -- ' + _md(probs)) if probs else ''} | "
                         f"{entry.get('calls', '?')} | `{entry.get('run_id', '?')}` | {subs} | {_md(kc)} |")
        lines.append("")
        for slug, entry in sorted(backfill.items()):
            if entry.get("reasoning"):
                lines.append(f"- `{slug}`: {_md(entry['reasoning'])}")
        lines.append("")
    else:
        lines.append("_Not run yet._")
        lines.append("")
    lines.append("### Cost")
    lines.append("")
    lines.append(f"- Total API cost of generation (metered from every SDK response's usage, all runs that "
                 f"contributed to the cache): **${totals['total_usd']:.2f}** over {totals['rows']} calls, "
                 f"{totals['web_searches']} web searches, {totals['input_tokens']:,} input tokens "
                 f"(cache reads/writes included), {totals['output_tokens']:,} output tokens.")
    for stage, cost in sorted(totals["by_stage"].items()):
        label = {"A": "stage A (search, verification, materiality)", "B": "stage B (personas)",
                 "C": "stage C (subdomain backfill)"}.get(stage, stage or "?")
        lines.append(f"  - {label}: ${cost:.2f}")
    for run, cost in sorted(totals["by_run"].items()):
        lines.append(f"  - run `{run}`: ${cost:.2f} (rows in `judgment_log` carry this `run_id`)")
    lines.append("")
    lines.append("### Things to look at first")
    lines.append("")
    lines.append("1. **The judge's materiality verdicts.** They are the fixture's answer key for the live "
                 "materiality metric and nobody has checked them; borderline scores (40-60) are where a "
                 "human will most often disagree. The score and reason are in the tables above.")
    lines.append("2. **The term splits.** A `does_not_know` term the archetype would plainly hold (or a "
                 "`knows` term a beginner would not) skews ledger precision/recall for that persona. "
                 "Also check that `quantities` are genuinely numeric measures.")
    lines.append("3. **Group names carry the persona's first name** (`Premier League (Amara)`), because "
                 "the fixture loader requires group names to be unique per suite. Under `--live` that "
                 "string reaches the briefing prompt. It leaks no ground truth, but it is a visible "
                 "artefact of the generator.")
    lines.append("")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REVIEW_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {REVIEW_PATH} ({persona_count} personas, {total_events} events, "
          f"${totals['total_usd']:.2f} metered).")
    return 0


# =============================================================================
# CLI
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["events", "personas", "review", "all", "check", "learning",
                                          "subdomains"])
    parser.add_argument("--group", default=None, help="one group slug only")
    parser.add_argument("--force", action="store_true", help="ignore the cache and regenerate")
    parser.add_argument("--budget", type=float, default=12.0, help="hard dollar cap for this run")
    parser.add_argument("--max-events", type=int, default=TARGET_MAX, help="cap on verified events per group")
    parser.add_argument("--db", type=Path, default=None,
                        help="SQLite store for the judgment log (default: the app's own, so the console sees it)")
    args = parser.parse_args(argv)

    if args.group and args.group not in GROUP_BY_SLUG:
        print(f"Unknown group {args.group!r}; one of {sorted(GROUP_BY_SLUG)}")
        return 2
    if args.stage == "review":
        return write_review()
    if args.stage == "check":
        return suite_check()
    if args.stage == "learning":
        return rewrite_learning(args.group)

    gen = build_gen(args.db, args.budget, args.force, args.max_events)
    print(f"run_id {gen.run_id}; judgment log at {gen.store.conn.execute('PRAGMA database_list').fetchone()[2]}")
    try:
        rc = 0
        if args.stage in ("events", "all"):
            rc = run_stage_events(gen, args.group)
            if rc:
                return rc
        if args.stage in ("personas", "all"):
            rc = run_stage_personas(gen, args.group)
            if rc and rc != 1:
                return rc
        if args.stage == "subdomains":
            rc = run_stage_subdomains(gen, args.group)
            write_review()
            return rc
        if args.stage == "all":
            write_review()
        return rc
    finally:
        print(gen.meter.line())
        gen.store.close()


if __name__ == "__main__":
    sys.exit(main())
