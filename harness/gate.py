"""`python -m harness.gate` -- the regression gate over a live-run artifact.

    PYTHONPATH=src:. .venv/bin/python -m harness.gate CANDIDATE.json \
        [--baseline BASELINE.json] [--db CANDIDATE.db] [--baseline-db BASELINE.db] \
        [--json out.json] [--max-cost 5.00]

Reads the artifact `harness.run` wrote (and the SQLite store the run wrote its
`judgment_log`, `exchanges`, `turns` and `concepts` into) and decides, in three
tiers, whether the candidate run may replace the baseline:

  Tier 1  hard gates, binary. Any failure fails the run.
  Tier 2  scored, paired against the baseline persona-by-persona with tolerance
          bands; a drop beyond the band fails. Without a baseline only the
          absolute thresholds apply.
  Tier 3  reported, never gated.

Exit status 0 is PASS, 1 is FAIL (or INCOMPLETE -- a Tier 1 check that could
not be evaluated because no database was available is not a pass).

The gate is read-only. The database is opened `mode=ro`; nothing here writes
to the artifact, the store, or `src/`.

Where a result already exists in the artifact -- `no_questions_to_user`,
`reading_regression`, `silence_regression`, per-state accuracy, ledger
agreement, materiality -- it is reused rather than recomputed, so that this
file cannot quietly disagree with `metrics.py` about the same number. What the
artifact does not hold (event ids, near-duplicate briefings, figures in
replies, judgment errors, cost, the re-ask rate) is computed from the store.

## The figure-normalisation rule (Tier 1, check 4)

A *checkable figure* in a reply is a number carrying a currency prefix
($ £ € USD GBP EUR), a scale word (k, m, bn, million, billion ...) or a unit
(%, kg, hl, ha, kg/ha, hl/ha, g/L, tonnes, points, bps). Bare numbers -- dates,
"6 August", "three clubs" -- are not checked, and neither are years 1900-2099
or ordinals (1st, 4th). A range ("13-15 hl/ha") contributes both ends.

A figure is *normalised* to its significant digits: commas, decimal points,
currency, scale and unit are dropped and leading and trailing zeros stripped,
so `1,285`, `1.285bn`, `$1.285 billion` and `1285` all become `1285`; `8,800`
becomes `88`; `0.5%` becomes `5`. It *matches* when that key equals the key of
any number anywhere in the call's own `input_json` (source_event, the briefing,
lookup / supporting_source, and the thread so far). This is deliberately
forgiving in the direction of not flagging a reply that restated a held figure
in a different unit; the cost is that a short key ("8") will match trivially.
Misses are what matter, and every miss is printed verbatim.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sqlite3
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from conversational_agent import config
from conversational_agent.store import _content_tokens, normalize_term

from .config import HarnessRouting
from .materiality_cache import CACHE_MODEL_LABEL
from .plainness import BUNDLE_FIELDS, format_summary, plainness, summarise

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Prices ------------------------------------------------------------------
# The system's own table (`config.PRICES_PER_MTOK`, USD per million tokens,
# (input, output)) plus the two extra labels that appear in a harness run's
# `judgment_log`. The log stores only `input_tokens` / `output_tokens` (no
# cache-read or cache-creation columns), so the estimate is the uncached price
# of what was logged and will UNDERSTATE a run that wrote cache and OVERSTATE
# one that read it.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    **config.PRICES_PER_MTOK,
    # The undated alias of the Haiku model id.
    "claude-haiku-4-5": config.PRICES_PER_MTOK[config.HAIKU],
    # A materiality verdict replayed from `--materiality-cache`: the row is
    # kept for tracing and stamped with this label; it cost nothing.
    CACHE_MODEL_LABEL: (0.0, 0.0),
}
DEFAULT_MAX_COST_USD = 5.00

# --- Thresholds ---------------------------------------------------------------
NEAR_DUPLICATE_JACCARD = 0.6
MATERIALITY_MIN_F1 = 0.90
LEDGER_MAX_DROP = 0.15
UNKNOWN_MIN_PRECISION = 0.90
CONFIRMED_MIN_PRECISION = 0.65
REASK_MAX_RATE = 0.09  # 3 of 34 briefings
REASK_MAX_RISE = 0.06
MARGINAL_BAND = 0.05
# Learning trend, paired per persona against the baseline (Tier 2).
LEARNING_KNOWN_SLOPE_MAX_DROP = 0.15  # terms per session
LEARNING_EFFICIENCY_MAX_REL_DROP = 0.20  # relative, on terms taught per briefing
LEARNING_RETAINED_BUCKET = "2-7d"
LEARNING_RETAINED_MIN_N = 3  # on both sides, or the retained check is not gated
LEARNING_RETAINED_MAX_DROP = 0.4  # rungs
# Definitions ceiling (Tier 2). `briefing` v7 asks for plain language over
# inline definitions -- "it shouldn't have too many literal definitions ... which
# can occasionally include a definition" -- so the regression being guarded
# against is gloss-stacking: a briefing that defines its way through the
# story. The gloss measure is the bundle's `asides_per_100w` -- parenthetical
# asides, paired dashes and "X, meaning Y" / "X, that is, Y" cues -- NOT
# `explained_terms`, which under v7 also counts terms made clear by plain
# explanation. A briefing is over the ceiling at more than 2.0 glosses per 100
# words; the check fails when more than 20% of briefings are over it, or when
# that share rose more than 0.10 on the baseline. Measured from the artifact's
# per-thread plainness bundle (`harness/plainness.py`), recomputed from the
# briefing text for artifacts written before the bundle existed.
DEFINITIONS_CEILING_PER_100W = 2.0
DEFINITIONS_CEILING_MAX_SHARE = 0.20
DEFINITIONS_CEILING_MAX_RISE = 0.10
# Tier 3 briefing-form thresholds are reported only; the proposer reads them.
LONG_BRIEFING_WORDS = 220
MANY_DEFINITIONS = 4
_BAND_ORDER = (config.BEGINNER, config.DEVELOPING, config.CONVERSANT, config.FLUENT)


# --- Report records -----------------------------------------------------------


@dataclass
class Check:
    """One line of a tier. `passed` is None when the check could not be run."""

    id: str
    passed: bool | None
    detail: str
    items: list[str] = field(default_factory=list)
    value: float | None = None
    baseline: float | None = None
    threshold: float | None = None
    persona: str | None = None
    marginal: bool = False
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateReport:
    candidate: str
    baseline: str | None
    db: str | None
    baseline_db: str | None
    run_id: str | None
    tier1: list[Check]
    tier2: list[Check]
    tier3: dict[str, Any]
    notes: list[str]

    @property
    def tier1_passed(self) -> bool | None:
        if any(c.passed is None for c in self.tier1):
            return None
        return all(c.passed for c in self.tier1)

    @property
    def tier2_passed(self) -> bool:
        return all(c.passed is not False for c in self.tier2)

    @property
    def status(self) -> str:
        if self.tier1_passed is None:
            return "INCOMPLETE"
        return "PASS" if (self.tier1_passed and self.tier2_passed) else "FAIL"

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    @property
    def rerun_advice(self) -> str:
        marginal = [c for c in self.tier2 if c.passed is False and c.marginal]
        hard = [c for c in self.tier2 if c.passed is False and not c.marginal]
        if any(c.id == "paired_comparison" and c.passed is None for c in self.tier1):
            return "baseline refused -- pair against a run with the same fixtures, rounds, horizon and harness routing."
        if self.tier1_passed is False:
            return "Tier 1 failed -- a rerun will not help; fix the cause first."
        if marginal and not hard:
            names = ", ".join(f"{c.id}[{c.persona}]" if c.persona else c.id for c in marginal)
            return f"marginal -- rerun once before deciding ({names} within {MARGINAL_BAND} of the band)"
        if hard:
            return "Tier 2 failed outside the marginal band -- do not rerun to make it pass."
        if self.tier1_passed is None:
            return "incomplete -- supply --db so the Tier 1 database checks can run."
        return "none needed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "baseline": self.baseline,
            "db": self.db,
            "baseline_db": self.baseline_db,
            "run_id": self.run_id,
            "status": self.status,
            "passed": self.passed,
            "tier1": {"passed": self.tier1_passed, "checks": [asdict(c) for c in self.tier1]},
            "tier2": {"passed": self.tier2_passed, "checks": [asdict(c) for c in self.tier2]},
            "tier3": self.tier3,
            "rerun_advice": self.rerun_advice,
            "notes": self.notes,
            "thresholds": {
                "near_duplicate_jaccard": NEAR_DUPLICATE_JACCARD,
                "materiality_min_f1": MATERIALITY_MIN_F1,
                "ledger_max_drop": LEDGER_MAX_DROP,
                "unknown_min_precision": UNKNOWN_MIN_PRECISION,
                "confirmed_min_precision": CONFIRMED_MIN_PRECISION,
                "reask_max_rate": REASK_MAX_RATE,
                "reask_max_rise": REASK_MAX_RISE,
                "definitions_ceiling_per_100w": DEFINITIONS_CEILING_PER_100W,
                "definitions_ceiling_max_share": DEFINITIONS_CEILING_MAX_SHARE,
                "definitions_ceiling_max_rise": DEFINITIONS_CEILING_MAX_RISE,
                "marginal_band": MARGINAL_BAND,
                "learning_known_slope_max_drop": LEARNING_KNOWN_SLOPE_MAX_DROP,
                "learning_efficiency_max_rel_drop": LEARNING_EFFICIENCY_MAX_REL_DROP,
                "learning_retained_bucket": LEARNING_RETAINED_BUCKET,
                "learning_retained_min_n": LEARNING_RETAINED_MIN_N,
                "learning_retained_max_drop": LEARNING_RETAINED_MAX_DROP,
            },
        }


# --- Pairing: may these two artifacts be compared at all? ---------------------


def _default_fixture_dir() -> str:
    from .personas import FIXTURE_DIR

    try:
        return str(Path(FIXTURE_DIR).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(FIXTURE_DIR)


def pairing_signature(artifact: dict[str, Any]) -> dict[str, Any]:
    """What a paired comparison requires to be equal on both sides.

    Read from the artifact's `run_settings` header when it has one; for an
    artifact written before the header existed, derived from the body
    (persona ids and rounds from `personas[]`) and defaulted to what every
    such run had (checked-in fixtures, default clock, Sonnet voice and
    grader, remediation search on). `header_recorded` says which.
    """
    rs = artifact.get("run_settings") or {}
    personas = artifact.get("personas") or []
    rounds_run = {
        p["persona_id"]: sum(1 for r in (p.get("rounds") or []) if not r.get("probe_only"))
        for p in personas
    }
    routing = HarnessRouting.from_dict(rs.get("harness_routing"))
    return {
        "mode": artifact.get("mode"),
        "fixture_dir": rs.get("fixture_dir") or _default_fixture_dir(),
        "persona_ids": sorted(p["persona_id"] for p in personas),
        "rounds_run": rounds_run,
        "rounds_cap": rs.get("rounds_cap") if rs else None,
        "horizon_days": rs.get("horizon_days"),
        "retention_probes": int(rs.get("retention_probes") or 0),
        "harness_routing": {
            "persona_model": routing.persona_model,
            "grader_model": routing.grader_model,
            "remediation_search": routing.remediation_search,
        },
        "header_recorded": bool(rs),
    }


def pairing_mismatches(cand: dict[str, Any], base: dict[str, Any]) -> list[str]:
    """Reasons the two artifacts may not be paired; empty when they may."""
    a, b = pairing_signature(cand), pairing_signature(base)
    out: list[str] = []
    if a["mode"] != b["mode"]:
        out.append(f"mode: candidate {a['mode']} vs baseline {b['mode']}")
    if a["fixture_dir"] != b["fixture_dir"]:
        out.append(f"fixture dir: candidate {a['fixture_dir']} vs baseline {b['fixture_dir']}")
    if a["persona_ids"] != b["persona_ids"]:
        out.append(f"persona ids: candidate {a['persona_ids']} vs baseline {b['persona_ids']}")
    for pid in sorted(set(a["rounds_run"]) & set(b["rounds_run"])):
        if a["rounds_run"][pid] != b["rounds_run"][pid]:
            out.append(f"rounds run for {pid}: candidate {a['rounds_run'][pid]} vs baseline {b['rounds_run'][pid]}")
    if a["header_recorded"] and b["header_recorded"] and a["rounds_cap"] != b["rounds_cap"]:
        out.append(f"--rounds: candidate {a['rounds_cap']} vs baseline {b['rounds_cap']}")
    if a["horizon_days"] != b["horizon_days"]:
        out.append(f"--horizon-days: candidate {a['horizon_days']} vs baseline {b['horizon_days']}")
    if a["retention_probes"] != b["retention_probes"]:
        out.append(f"--retention-probes: candidate {a['retention_probes']} vs baseline {b['retention_probes']}")
    for key in ("persona_model", "grader_model", "remediation_search"):
        if a["harness_routing"][key] != b["harness_routing"][key]:
            out.append(
                f"harness routing {key}: candidate {a['harness_routing'][key]} vs baseline {b['harness_routing'][key]}"
            )
    return out


# --- Database access (read-only) --------------------------------------------


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


class RunDb:
    """Read-only view of one run's store, scoped to the artifact's personas.

    Everything the harness created that is *not* one of the artifact's
    personas -- the silence / reading / familiar-contract probes, the
    interrupt-case pseudo-user -- is a probe, and probes are excluded from every
    per-user check. Persona users are matched to artifact personas by
    `display_name`, which is the only identifier the artifact carries.
    """

    def __init__(self, path: Path, artifact: dict[str, Any]):
        self.path = path
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.run_id: str | None = (artifact.get("volatile") or {}).get("run_id")
        if not self.run_id:
            row = self.conn.execute(
                "SELECT id FROM eval_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            self.run_id = row["id"] if row else None
        names = {p["display_name"]: p["persona_id"] for p in artifact.get("personas", [])}
        self.user_to_persona: dict[str, str] = {}
        self.persona_to_user: dict[str, str] = {}
        for row in self.conn.execute("SELECT id, display_name FROM users").fetchall():
            pid = names.get(row["display_name"])
            if pid:
                self.user_to_persona[row["id"]] = pid
                self.persona_to_user[pid] = row["id"]

    def close(self) -> None:
        self.conn.close()

    # -- exchanges / turns -----------------------------------------------------

    def exchanges(self) -> list[dict[str, Any]]:
        """Persona exchanges only, oldest first, with JSON columns decoded."""
        rows = self.conn.execute(
            "SELECT * FROM exchanges ORDER BY raised_at, created_at"
        ).fetchall()
        out = []
        for r in rows:
            if r["user_id"] not in self.user_to_persona:
                continue
            d = dict(r)
            d["persona_id"] = self.user_to_persona[r["user_id"]]
            for col in ("explained_terms", "asked_about", "understood", "not_understood"):
                d[col] = _json_list(r[col])
            out.append(d)
        return out

    def turns(self, exchange_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT seq, speaker, text FROM turns WHERE exchange_id = ? ORDER BY seq",
                (exchange_id,),
            ).fetchall()
        ]

    def events(self, user_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, headline, detail, is_material FROM monitor_events WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        ]

    def concepts(self, user_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT term, state, exposure_count, correct_uses, misunderstandings,"
                " read_explanations, evidence FROM concepts WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        ]

    # -- judgment log ------------------------------------------------------------

    def judgments(self, point: str | None = None, include_probes: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM judgment_log WHERE run_id IS ?"
        params: list[Any] = [self.run_id]
        if point:
            sql += " AND judgment_point = ?"
            params.append(point)
        sql += " ORDER BY created_at"
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        if include_probes:
            return rows
        return [r for r in rows if r["user_id"] in self.user_to_persona]

    def is_probe(self, user_id: str | None) -> bool:
        return user_id not in self.user_to_persona


# --- Tier 1 helpers -----------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")


def token_jaccard(a: str, b: str) -> float:
    """Jaccard over lowercased alphanumeric token sets."""
    ta, tb = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


def never_repeat(exchanges: list[dict[str, Any]]) -> Check:
    """Every briefing names its event, no event twice, no two briefings alike."""
    items: list[str] = []
    by_user: dict[str, list[dict[str, Any]]] = {}
    for e in exchanges:
        by_user.setdefault(e["persona_id"], []).append(e)
    max_pair: tuple[float, str, str] = (0.0, "", "")
    for persona, rows in by_user.items():
        seen: dict[str, str] = {}
        for e in rows:
            if not e["event_id"]:
                items.append(f"{persona}: exchange {e['id']} has no event_id")
            elif e["event_id"] in seen:
                items.append(
                    f"{persona}: event {e['event_id']} briefed twice "
                    f"({seen[e['event_id']]} and {e['id']})"
                )
            else:
                seen[e["event_id"]] = e["id"]
        for a, b in itertools.combinations(rows, 2):
            j = token_jaccard(a["briefing"] or "", b["briefing"] or "")
            if j > max_pair[0]:
                max_pair = (j, a["id"], b["id"])
            if j > NEAR_DUPLICATE_JACCARD:
                items.append(
                    f"{persona}: near-duplicate briefings (jaccard {j:.2f})\n"
                    f"    [{a['id']}] {a['briefing']}\n"
                    f"    [{b['id']}] {b['briefing']}"
                )
    return Check(
        id="never_repeat",
        passed=not items,
        detail=(
            f"{len(exchanges)} briefings, {sum(1 for e in exchanges if e['event_id'])} with event_id, "
            f"{len({(e['persona_id'], e['event_id']) for e in exchanges if e['event_id']})} distinct; "
            f"max pairwise jaccard {max_pair[0]:.2f}"
        ),
        items=items,
        value=max_pair[0],
        threshold=NEAR_DUPLICATE_JACCARD,
        data={"briefings": len(exchanges), "max_pair": list(max_pair)},
    )


# -- figures ----------------------------------------------------------------------

_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_CURRENCY = r"(?:US\$|A\$|C\$|\$|£|€|USD|GBP|EUR)"
_SCALE = r"(?:thousand|million|billion|trillion|bn|mn|mm|tn|k|m|b)"
_UNIT = (
    r"(?:%|percent|per cent|kg/ha|hl/ha|kg|hl|ha|g/l|mg/l|tonnes?|tons?|litres?|liters?|"
    r"hectares?|hectolitres?|points?|pts|bps|pp)"
)
_FIGURE = re.compile(
    rf"(?P<cur>{_CURRENCY})?\s?(?P<num>{_NUMBER})"
    rf"(?:\s?(?P<scale>{_SCALE})\b)?"
    rf"(?:\s?(?P<unit>{_UNIT})(?![a-z]))?",
    re.IGNORECASE,
)
_RANGE = re.compile(
    rf"(?P<cur>{_CURRENCY})?\s?(?P<lo>{_NUMBER})\s?(?:-|–|—|to)\s?(?P<hi>{_NUMBER})"
    rf"(?:\s?(?P<scale>{_SCALE})\b)?(?:\s?(?P<unit>{_UNIT})(?![a-z]))?",
    re.IGNORECASE,
)
_ORDINAL = re.compile(r"^\d+(st|nd|rd|th)$", re.IGNORECASE)
_ANY_NUMBER = re.compile(_NUMBER)


def figure_key(number: str) -> str:
    """Significant digits of a number string: `1,285` -> `1285`, `8,800` -> `88`."""
    digits = re.sub(r"[^0-9]", "", number)
    digits = digits.lstrip("0").rstrip("0")
    return digits or "0"


def _is_year(num: str, cur: str | None, scale: str | None, unit: str | None) -> bool:
    if cur or scale or unit:
        return False
    return bool(re.fullmatch(r"(19|20)\d\d", num))


def extract_figures(text: str) -> list[dict[str, str]]:
    """Checkable figures in `text`: those with a currency, scale or unit."""
    out: list[dict[str, str]] = []
    spans: set[tuple[int, int]] = set()
    for m in _RANGE.finditer(text):
        cur, scale, unit = m.group("cur"), m.group("scale"), m.group("unit")
        if not (cur or scale or unit):
            continue
        for grp in ("lo", "hi"):
            num = m.group(grp)
            if _is_year(num, cur, scale, unit):
                continue
            out.append({"text": m.group(0).strip(), "number": num, "key": figure_key(num), "pos": str(m.start())})
        spans.add(m.span())
    for m in _FIGURE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        cur, num, scale, unit = m.group("cur"), m.group("num"), m.group("scale"), m.group("unit")
        if not (cur or scale or unit):
            continue
        tail = text[m.end(): m.end() + 2]
        if not (scale or unit) and _ORDINAL.match(num + tail):
            continue
        if _is_year(num, cur, scale, unit):
            continue
        out.append({"text": m.group(0).strip(), "number": num, "key": figure_key(num), "pos": str(m.start())})
    return out


_LITERAL_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def literal_escapes(text: str) -> list[str]:
    """Literal `\\uXXXX` sequences left in text a user (or a later call) saw.

    A model that double-escapes a pound sign returns the six characters
    `\\u00a3`; `json.loads` faithfully keeps them, so the user reads
    `\\u00a33.5m`. That is a real output defect, and it also breaks any number
    check that runs on the raw text (`a3` + `3.5m` reads as `33.5m`). Reported
    separately; decoded before figures are extracted.
    """
    return [m.group(0) for m in _LITERAL_ESCAPE.finditer(text or "")]


def decode_literal_escapes(text: str) -> str:
    return _LITERAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text or "")


def source_keys(source_text: str) -> set[str]:
    """Every number in the call's input, normalised the same way."""
    return {figure_key(m.group(0)) for m in _ANY_NUMBER.finditer(decode_literal_escapes(source_text))}


def unsupported_figures(reply: str, input_json: str) -> list[dict[str, str]]:
    keys = source_keys(input_json)
    reply = decode_literal_escapes(reply)
    misses = []
    for fig in extract_figures(reply):
        if fig["key"] not in keys:
            pos = int(fig["pos"])
            lo, hi = max(0, pos - 80), min(len(reply), pos + len(fig["text"]) + 80)
            fig = dict(fig)
            fig["excerpt"] = ("..." if lo else "") + reply[lo:hi].replace("\n", " ") + ("..." if hi < len(reply) else "")
            misses.append(fig)
    return misses


def fabricated_figures(db: RunDb) -> Check:
    rows = db.judgments(config.THREAD_REPLY)
    items: list[str] = []
    hygiene: list[str] = []
    checked = figures = 0
    for r in rows:
        if not r["verdict_json"]:
            continue
        try:
            answer = json.loads(r["verdict_json"]).get("answer", "")
        except ValueError:
            continue
        checked += 1
        escapes = literal_escapes(answer or "")
        if escapes:
            hygiene.append(
                f"{db.user_to_persona.get(r['user_id'], r['user_id'])} [{r['id']}] reply shown to the user "
                f"contains literal {sorted(set(escapes))} -- a double-escaped character, not a figure"
            )
        figs = extract_figures(decode_literal_escapes(answer or ""))
        figures += len(figs)
        for miss in unsupported_figures(answer or "", r["input_json"] or ""):
            items.append(
                f"{db.user_to_persona.get(r['user_id'], r['user_id'])} [{r['id']}] figure "
                f"{miss['text']!r} (key {miss['key']}) not in the call's input\n"
                f"    reply: {miss['excerpt']}"
            )
    return Check(
        id="no_fabricated_figure",
        passed=not items,
        detail=f"{checked} replies, {figures} checkable figures, {len(items)} unsupported"
        + (f"; {len(hygiene)} reply/replies with literal \\u escapes (reported under Tier 3)" if hygiene else ""),
        items=items,
        value=float(len(items)),
        data={"replies": checked, "figures": figures, "reply_hygiene": hygiene},
    )


# -- errors and cost ------------------------------------------------------------


def row_cost_usd(row: dict[str, Any]) -> float | None:
    prices = PRICES_PER_MTOK.get(row.get("model") or "")
    if prices is None:
        return None
    return ((row.get("input_tokens") or 0) * prices[0] + (row.get("output_tokens") or 0) * prices[1]) / 1_000_000


def errors_and_cost(db: RunDb, max_cost: float, artifact: dict[str, Any]) -> tuple[Check, Check, dict[str, Any]]:
    all_rows = db.judgments(include_probes=True)
    persona_rows = [r for r in all_rows if not db.is_probe(r["user_id"])]
    errors = [r for r in persona_rows if r["error"]]
    items = [
        f"{db.user_to_persona.get(r['user_id'], r['user_id'])} {r['judgment_point']} "
        f"[{r['id']}]: {(r['error'] or '')[:160]}"
        for r in errors
    ]
    probe_errors = sum(1 for r in all_rows if r["error"] and db.is_probe(r["user_id"]))
    err_check = Check(
        id="zero_judgment_errors",
        passed=not errors,
        detail=f"{len(errors)} errored calls of {len(persona_rows)} (probes excluded: {probe_errors} errored of {len(all_rows) - len(persona_rows)})",
        items=items,
        value=float(len(errors)),
        data={"artifact_judgment_errors": (artifact.get("plumbing") or {}).get("judgment_errors")},
    )

    by_point: dict[str, dict[str, Any]] = {}
    unknown_models: set[str] = set()
    total = 0.0
    probe_cost = 0.0
    for r in all_rows:
        c = row_cost_usd(r)
        if c is None:
            unknown_models.add(r.get("model") or "?")
            c = 0.0
        total += c
        if db.is_probe(r["user_id"]):
            probe_cost += c
        b = by_point.setdefault(
            r["judgment_point"],
            {"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "latencies": [], "model": r["model"]},
        )
        b["calls"] += 1
        b["errors"] += 1 if r["error"] else 0
        b["input_tokens"] += r["input_tokens"] or 0
        b["output_tokens"] += r["output_tokens"] or 0
        b["cost_usd"] += c
        b["latencies"].append(r["latency_ms"] or 0)
    cost_table = {}
    for point, b in sorted(by_point.items()):
        lat = b.pop("latencies")
        b["cost_usd"] = round(b["cost_usd"], 4)
        b["mean_latency_ms"] = round(statistics.fmean(lat), 1) if lat else 0.0
        b["p95_latency_ms"] = sorted(lat)[max(0, int(len(lat) * 0.95) - 1)] if lat else 0
        b["max_latency_ms"] = max(lat) if lat else 0
        cost_table[point] = b
    cost_check = Check(
        id="cost_within_budget",
        passed=total <= max_cost,
        detail=(
            f"estimated ${total:.2f} of ${max_cost:.2f} over {len(all_rows)} calls "
            f"(probes ${probe_cost:.2f}; uncached input+output only)"
            + (f"; unpriced models: {sorted(unknown_models)}" if unknown_models else "")
        ),
        value=round(total, 4),
        threshold=max_cost,
        data={"probe_cost_usd": round(probe_cost, 4), "calls": len(all_rows)},
    )
    return err_check, cost_check, cost_table


# --- Tier 2 helpers -----------------------------------------------------------


def _persona_map(artifact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["persona_id"]: p for p in artifact.get("personas", [])}


def _state_precision(artifact: dict[str, Any], state: str) -> tuple[float | None, int]:
    for row in (artifact.get("per_state_accuracy") or {}).get("by_state", []):
        if row["state"] == state:
            return row.get("accuracy"), row.get("n", 0)
    return None, 0


def _absolute(check_id: str, value: float | None, minimum: float, detail: str, baseline: float | None = None, persona: str | None = None) -> Check:
    if value is None:
        return Check(id=check_id, passed=None, detail=f"{detail}: not measured", persona=persona, threshold=minimum, baseline=baseline)
    passed = value >= minimum
    return Check(
        id=check_id,
        passed=passed,
        detail=f"{detail}: {value:.3f} (min {minimum:.2f})" + (f", baseline {baseline:.3f}" if baseline is not None else ""),
        value=value,
        baseline=baseline,
        threshold=minimum,
        persona=persona,
        marginal=(not passed) and (minimum - value) <= MARGINAL_BAND,
    )


def _paired_drop(check_id: str, persona: str, value: float | None, baseline: float | None, max_drop: float, label: str) -> Check:
    if value is None:
        return Check(id=check_id, passed=None if baseline is not None else True, persona=persona, detail=f"{label}: n/a in candidate", baseline=baseline, threshold=max_drop)
    if baseline is None:
        return Check(id=check_id, passed=True, persona=persona, value=value, threshold=max_drop, detail=f"{label}: {value:.3f} (no baseline value; absolute only)")
    drop = baseline - value
    passed = drop <= max_drop
    return Check(
        id=check_id,
        passed=passed,
        persona=persona,
        value=value,
        baseline=baseline,
        threshold=max_drop,
        detail=f"{label}: {value:.3f} vs baseline {baseline:.3f} (drop {drop:+.3f}, band {max_drop:.2f})",
        marginal=(not passed) and (drop - max_drop) <= MARGINAL_BAND,
    )


def _paired_relative_drop(
    check_id: str, persona: str, value: float | None, baseline: float | None, max_rel_drop: float, label: str
) -> Check:
    """Fail when `value` is more than `max_rel_drop` below `baseline`, relatively.

    A zero baseline cannot drop, so it is reported and not gated."""
    if value is None:
        return Check(id=check_id, passed=None if baseline is not None else True, persona=persona, detail=f"{label}: n/a in candidate", baseline=baseline, threshold=max_rel_drop)
    if baseline is None:
        return Check(id=check_id, passed=True, persona=persona, value=value, threshold=max_rel_drop, detail=f"{label}: {value:.3f} (no baseline value)")
    if baseline <= 0:
        return Check(id=check_id, passed=True, persona=persona, value=value, baseline=baseline, threshold=max_rel_drop, detail=f"{label}: {value:.3f} vs baseline {baseline:.3f} (baseline is zero; not gated)")
    rel = (baseline - value) / baseline
    passed = rel <= max_rel_drop
    return Check(
        id=check_id,
        passed=passed,
        persona=persona,
        value=value,
        baseline=baseline,
        threshold=max_rel_drop,
        detail=f"{label}: {value:.3f} vs baseline {baseline:.3f} (relative drop {rel:+.1%}, band {max_rel_drop:.0%})",
        marginal=(not passed) and (rel - max_rel_drop) <= MARGINAL_BAND,
    )


def learning_checks(
    cand_entries: dict[str, Any], base_entries: dict[str, Any] | None
) -> tuple[list[Check], list[dict[str, Any]]]:
    """Tier 2 learning-trend checks per persona, and the Tier 3 rows.

    `cand_entries` / `base_entries` map persona id -> `metrics.PersonaLearning`
    (rebuilt from the artifact, so a baseline written before the trend fields
    existed still pairs; its `known_slope` is then n/a on that side).
    """
    checks: list[Check] = []
    rows: list[dict[str, Any]] = []
    for pid, c in cand_entries.items():
        b = (base_entries or {}).get(pid)
        has_base = base_entries is not None
        c_eff = c.efficiency
        b_eff = b.efficiency if b else None
        c_ret, c_n = c.retained_at(LEARNING_RETAINED_BUCKET)
        b_ret, b_n = b.retained_at(LEARNING_RETAINED_BUCKET) if b else (None, 0)

        slope = _paired_drop(
            "learning_known_slope", pid, c.known_slope, (b.known_slope if b else None) if has_base else None,
            LEARNING_KNOWN_SLOPE_MAX_DROP, "known slope (terms/session)",
        )
        if c.known_slope is None and (b is None or b.known_slope is None):
            slope.passed = True
            slope.detail = "known slope: not recorded on either side (needs rounds[].known_count, written from this version on)"
        checks.append(slope)

        checks.append(
            _paired_relative_drop(
                "learning_efficiency", pid,
                c_eff.per_briefing if c_eff else None,
                (b_eff.per_briefing if b_eff else None) if has_base else None,
                LEARNING_EFFICIENCY_MAX_REL_DROP, "teaching efficiency (terms/briefing)",
            )
        )

        if has_base and b is not None and c_n >= LEARNING_RETAINED_MIN_N and b_n >= LEARNING_RETAINED_MIN_N:
            checks.append(
                _paired_drop(
                    "learning_retained_2_7d", pid, c_ret, b_ret, LEARNING_RETAINED_MAX_DROP,
                    f"retained rung @{LEARNING_RETAINED_BUCKET} (n {c_n} vs {b_n})",
                )
            )
        else:
            checks.append(
                Check(
                    id="learning_retained_2_7d", passed=True, persona=pid, value=c_ret, baseline=b_ret,
                    threshold=LEARNING_RETAINED_MAX_DROP,
                    detail=f"retained rung @{LEARNING_RETAINED_BUCKET}: "
                    + ("n/a" if c_ret is None else f"{c_ret:.2f}") + f" (n={c_n})"
                    + (" vs baseline " + ("n/a" if b_ret is None else f"{b_ret:.2f}") + f" (n={b_n})" if has_base else "")
                    + f"; not gated (needs n >= {LEARNING_RETAINED_MIN_N} on both sides)",
                )
            )

        rows.append(
            {
                "persona_id": pid,
                "known_slope": c.known_slope,
                "baseline_known_slope": b.known_slope if b else None,
                "known_initial": c.initial_known,
                "known_final": c.final_known,
                "efficiency_per_briefing": c_eff.per_briefing if c_eff else None,
                "baseline_efficiency_per_briefing": b_eff.per_briefing if b_eff else None,
                "efficiency_per_100_words": c_eff.per_100_words if c_eff else None,
                "baseline_efficiency_per_100_words": b_eff.per_100_words if b_eff else None,
                "retained_2_7d": c_ret,
                "retained_2_7d_n": c_n,
                "baseline_retained_2_7d": b_ret,
                "baseline_retained_2_7d_n": b_n,
                "retained_slope": c.retained_slope,
                "baseline_retained_slope": b.retained_slope if b else None,
                "calibration_gap": c.calibration_gap,
                "baseline_calibration_gap": b.calibration_gap if b else None,
                "confabulation_rate": c.confabulation_rate,
                "baseline_confabulation_rate": b.confabulation_rate if b else None,
            }
        )
    return checks, rows


def _asks_about(term: str, asked: list[str]) -> bool:
    """`term` (an explained term) was asked about in this thread.

    Exact match after `normalize_term`, or containment on content tokens --
    the same merge the store applies on write, so `annualized revenue
    run-rate` in `explained_terms` meets `annualized revenue run rate` in
    `asked_about`.
    """
    t = normalize_term(term)
    for a in asked:
        a_n = normalize_term(a)
        if t == a_n:
            return True
        # The store's own containment tokens, so "asks about" merges the way
        # the ledger does.
        ta, tb = _content_tokens(t), _content_tokens(a_n)
        if ta and tb and (ta <= tb or tb <= ta):
            return True
    return False


def reask_rate(exchanges: list[dict[str, Any]]) -> tuple[float | None, list[dict[str, Any]], int]:
    """Share of briefings whose thread asked about a term that briefing defined."""
    events = []
    for e in exchanges:
        hits = [t for t in e["explained_terms"] if _asks_about(t, e["asked_about"])]
        if hits:
            events.append({"exchange_id": e["id"], "persona_id": e["persona_id"], "terms": hits, "explained_terms": e["explained_terms"], "asked_about": e["asked_about"]})
    n = len(exchanges)
    return ((len(events) / n) if n else None), events, n


# --- Plainness: the per-thread bundle, by band ---------------------------------


def _vocabularies(artifact: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """persona_id -> the group's vocabulary, from the fixture set the artifact
    names (`run_settings.fixture_dir`, else the checked-in one). Only needed
    for artifacts written before the per-thread bundle existed; an empty map
    makes the fallback use each thread's own `terms_used`."""
    try:
        from . import personas as _personas
    except Exception:  # pragma: no cover - defensive
        return {}
    wanted = (artifact.get("run_settings") or {}).get("fixture_dir")
    original = _personas.FIXTURE_DIR
    try:
        if wanted:
            candidate = (PROJECT_ROOT / wanted) if not Path(wanted).is_absolute() else Path(wanted)
            if candidate.is_dir():
                _personas.FIXTURE_DIR = candidate  # type: ignore[assignment]
        return {p.id: tuple(p.group.vocabulary) for p in _personas.load()}
    except Exception:
        return {}
    finally:
        _personas.FIXTURE_DIR = original  # type: ignore[assignment]


def thread_bundles(artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One row per briefing in the artifact: persona, round, the reader's
    band at the time (`proficiency_before`) and the plainness bundle -- taken
    from the thread when the run wrote one, recomputed from the briefing text
    otherwise (`recomputed` says which)."""
    if not artifact:
        return []
    vocab: dict[str, tuple[str, ...]] | None = None
    rows: list[dict[str, Any]] = []
    for p in artifact.get("personas", []):
        pid = p.get("persona_id")
        for t in p.get("threads", []):
            text = t.get("briefing") or ""
            if not text.strip():
                continue
            bundle = t.get("plainness")
            recomputed = False
            if not isinstance(bundle, dict) or "fk_grade" not in bundle:
                if vocab is None:
                    vocab = _vocabularies(artifact)
                words = vocab.get(pid) or tuple(t.get("terms_used") or ())
                bundle = plainness(text, words, t.get("explained_terms") or [])
                recomputed = True
            rows.append(
                {
                    "persona_id": pid,
                    "round": t.get("round"),
                    "exchange_id": t.get("exchange_id"),
                    "band": t.get("proficiency_before") or "?",
                    "bundle": bundle,
                    "recomputed": recomputed,
                }
            )
    return rows


def plainness_by_band(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """p50 / p90 / mean of every bundle field, for all briefings and per band
    at the time of the briefing. Bands print in proficiency order; anything
    unexpected is appended."""
    bands = sorted({r["band"] for r in rows}, key=lambda b: (_BAND_ORDER.index(b) if b in _BAND_ORDER else len(_BAND_ORDER), b))
    out: dict[str, Any] = {
        "all": summarise([r["bundle"] for r in rows]),
        "by_band": {b: summarise([r["bundle"] for r in rows if r["band"] == b]) for b in bands},
        "recomputed": sum(1 for r in rows if r["recomputed"]),
        "fields": [k for k, _ in BUNDLE_FIELDS],
    }
    return out


def definitions_ceiling(rows: list[dict[str, Any]], base_rows: list[dict[str, Any]] | None) -> Check:
    """Tier 2: the share of briefings over `DEFINITIONS_CEILING_PER_100W`
    glosses (`asides_per_100w`) per 100 words, at most
    `DEFINITIONS_CEILING_MAX_SHARE` and not up more than
    `DEFINITIONS_CEILING_MAX_RISE` on the baseline."""

    def share(rs: list[dict[str, Any]]) -> tuple[float | None, list[dict[str, Any]]]:
        over = [r for r in rs if float(r["bundle"].get("asides_per_100w", 0.0)) > DEFINITIONS_CEILING_PER_100W]
        return ((len(over) / len(rs)) if rs else None), over

    rate, over = share(rows)
    b_rate = share(base_rows)[0] if base_rows is not None else None
    if rate is None:
        return Check(id="definitions_ceiling", passed=None, detail="no briefings in the artifact", threshold=DEFINITIONS_CEILING_MAX_SHARE)
    fails = []
    if rate > DEFINITIONS_CEILING_MAX_SHARE:
        fails.append(f"above {DEFINITIONS_CEILING_MAX_SHARE:.2f}")
    if b_rate is not None and (rate - b_rate) > DEFINITIONS_CEILING_MAX_RISE:
        fails.append(f"rose {rate - b_rate:+.3f} vs baseline {b_rate:.3f} (band {DEFINITIONS_CEILING_MAX_RISE:.2f})")
    excess = max(rate - DEFINITIONS_CEILING_MAX_SHARE, (rate - b_rate - DEFINITIONS_CEILING_MAX_RISE) if b_rate is not None else -1)
    return Check(
        id="definitions_ceiling",
        passed=not fails,
        value=round(rate, 4),
        baseline=None if b_rate is None else round(b_rate, 4),
        threshold=DEFINITIONS_CEILING_MAX_SHARE,
        detail=f"briefings over {DEFINITIONS_CEILING_PER_100W:.1f} glosses/100w (asides, dash pairs, 'meaning' cues): {len(over)}/{len(rows)} = {rate:.3f} (max {DEFINITIONS_CEILING_MAX_SHARE:.2f}"
        + (f", baseline {b_rate:.3f}, max rise {DEFINITIONS_CEILING_MAX_RISE:.2f}" if b_rate is not None else ", no baseline")
        + ")"
        + ("; " + "; ".join(fails) if fails else ""),
        items=[
            f"{r['persona_id']} r{r['round']} ({r['band']}): {float(r['bundle']['asides_per_100w']):.2f} glosses/100w "
            f"({r['bundle'].get('parenthetical_asides')} glosses / {r['bundle'].get('words')} words; "
            f"made clear {float(r['bundle'].get('definitions_per_100w', 0.0)):.2f}/100w)"
            for r in sorted(over, key=lambda r: -float(r["bundle"]["asides_per_100w"]))
        ],
        marginal=bool(fails) and excess <= MARGINAL_BAND,
        data={"over": [{k: r[k] for k in ("persona_id", "round", "exchange_id", "band")} | {"asides_per_100w": r["bundle"]["asides_per_100w"], "definitions_per_100w": r["bundle"].get("definitions_per_100w")} for r in over]},
    )


# --- Tier 3 helpers -----------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")
_CAP_TOKEN = re.compile(r"\b[A-Z][a-zA-Z0-9'’-]{2,}\b")
_STOP_CAPS = frozenset(
    "The This That These Those There Their They Them Then When Where While What Which Who Why How "
    "But And For Nor Yet Also Just Now New One Two Three Four Five Six Seven Eight Nine Ten "
    "January February March April May June July August September October November December "
    "Monday Tuesday Wednesday Thursday Friday Saturday Sunday After Before Since Until Over Under "
    "Normally Instead Meanwhile However Although Because Its His Her".split()
)


def key_phrases(text: str) -> set[str]:
    """Crude event fingerprint: proper-noun-ish tokens plus scaled figures."""
    caps = {t for t in _CAP_TOKEN.findall(text or "") if t not in _STOP_CAPS}
    figs = {f["text"].lower() for f in extract_figures(text or "")}
    return {c.lower() for c in caps} | figs


def briefing_form(db: RunDb, exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    events_by_user = {uid: db.events(uid) for uid in db.persona_to_user.values()}
    per: list[dict[str, Any]] = []
    for e in exchanges:
        text = e["briefing"] or ""
        words = len(text.split())
        sentences = len(_SENTENCE_END.findall(text)) or (1 if text.strip() else 0)
        own = next((ev for ev in events_by_user.get(e["user_id"], []) if ev["id"] == e["event_id"]), None)
        own_keys = key_phrases((own or {}).get("headline", "") + " " + ((own or {}).get("detail") or ""))
        brief_keys = key_phrases(text)
        others = []
        for ev in events_by_user.get(e["user_id"], []):
            if ev["id"] == e["event_id"]:
                continue
            ev_keys = key_phrases(ev["headline"]) - own_keys
            overlap = ev_keys & brief_keys
            if len(overlap) >= 2:
                others.append({"event_id": ev["id"], "headline": ev["headline"], "overlap": sorted(overlap)})
        per.append(
            {
                "exchange_id": e["id"],
                "persona_id": e["persona_id"],
                "topic": e["topic"],
                "words": words,
                "sentences": sentences,
                "definitions": len(e["explained_terms"]),
                "read_quality": e["read_quality"],
                "other_events_mentioned": len(others),
                "other_events": others,
                "long": words > LONG_BRIEFING_WORDS,
                "many_definitions": len(e["explained_terms"]) > MANY_DEFINITIONS,
            }
        )

    def dist(key: str) -> dict[str, Any]:
        vals = [p[key] for p in per]
        if not vals:
            return {}
        s = sorted(vals)
        return {"min": s[0], "median": s[len(s) // 2], "mean": round(statistics.fmean(s), 1), "max": s[-1]}

    return {
        "per_briefing": per,
        "words": dist("words"),
        "sentences": dist("sentences"),
        "definitions": dist("definitions"),
        "multi_event_briefings": sum(1 for p in per if p["other_events_mentioned"]),
        "long_briefings": [p["exchange_id"] for p in per if p["long"]],
        "many_definition_briefings": [p["exchange_id"] for p in per if p["many_definitions"]],
        "thresholds": {"long_words": LONG_BRIEFING_WORDS, "many_definitions": MANY_DEFINITIONS},
    }


# --- Assembly -----------------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sibling_db(artifact_path: Path) -> Path | None:
    candidate = artifact_path.with_suffix(".db")
    return candidate if candidate.exists() else None


def evaluate(
    candidate_path: Path,
    baseline_path: Path | None = None,
    db_path: Path | None = None,
    baseline_db_path: Path | None = None,
    max_cost: float = DEFAULT_MAX_COST_USD,
) -> GateReport:
    cand = _load(candidate_path)
    base = _load(baseline_path) if baseline_path else None
    notes: list[str] = []
    if cand.get("mode") != "live":
        notes.append("candidate is not a live artifact; judgment-quality numbers are refused offline and the gate will report them as not measured")

    # ---- Pairing ----------------------------------------------------------------
    # A paired comparison is only meaningful between runs of the same fixture
    # set, the same number of rounds on the same horizon, and the same
    # harness routing (a Haiku-voiced persona is a different subject from a
    # Sonnet-voiced one). Otherwise the baseline is refused: the run is
    # INCOMPLETE, not FAIL, and the absolute checks still print.
    pairing_check: Check | None = None
    if base is not None:
        mismatches = pairing_mismatches(cand, base)
        sig = pairing_signature(cand)
        if mismatches:
            pairing_check = Check(
                id="paired_comparison",
                passed=None,
                detail=f"REFUSED: baseline {baseline_path} is not comparable ({len(mismatches)} mismatch(es)); Tier 2 ran without a baseline",
                items=mismatches,
                data={"candidate": sig, "baseline": pairing_signature(base)},
            )
            notes.append("baseline refused: the artifacts were not produced under the same fixture set / rounds / horizon / harness routing")
            base = None
            baseline_db_path = None
        else:
            legacy = [name for name, art in (("candidate", cand), ("baseline", _load(baseline_path))) if not (art.get("run_settings") or {})]
            pairing_check = Check(
                id="paired_comparison",
                passed=True,
                detail="fixture set, persona ids, rounds, horizon and harness routing agree"
                + (f" ({', '.join(legacy)} without a run_settings header: legacy defaults assumed)" if legacy else ""),
                data={"signature": sig},
            )

    db_path = db_path or _sibling_db(candidate_path)
    baseline_db_path = baseline_db_path or (_sibling_db(baseline_path) if (baseline_path and base) else None)
    db = RunDb(db_path, cand) if db_path else None
    bdb = RunDb(baseline_db_path, base) if (baseline_db_path and base) else None
    if db is None:
        notes.append("no database: Tier 1 checks 2, 4 and 5 and the re-ask rate could not be evaluated (pass --db)")
    elif not db.persona_to_user:
        notes.append(f"{db_path}: none of the artifact's personas matched a user by display_name; database checks see no exchanges")

    # ---- Tier 1 ---------------------------------------------------------------
    tier1: list[Check] = []
    if pairing_check is not None:
        tier1.append(pairing_check)
    nq = cand.get("no_questions_to_user") or {}
    tier1.append(
        Check(
            id="no_questions_to_user",
            passed=bool(nq.get("clean")) if nq else None,
            detail=f"{nq.get('briefings_checked', '?')} briefings checked, {len(nq.get('offending', []))} offending (from artifact)",
            items=list(nq.get("offending", [])),
        )
    )
    exchanges = db.exchanges() if db else []
    if db:
        tier1.append(never_repeat(exchanges))
    else:
        tier1.append(Check(id="never_repeat", passed=None, detail="needs --db"))
    for key in ("reading_regression", "silence_regression"):
        g = cand.get(key) or {}
        tier1.append(
            Check(
                id=key,
                passed=bool(g.get("clean")) if g else None,
                detail=f"{len(g.get('violations', []))} violation(s) (from artifact)",
                items=list(g.get("violations", [])),
            )
        )
    if db:
        tier1.append(fabricated_figures(db))
        err_check, cost_check, cost_table = errors_and_cost(db, max_cost, cand)
        tier1.extend([err_check, cost_check])
    else:
        tier1.append(Check(id="no_fabricated_figure", passed=None, detail="needs --db"))
        tier1.append(Check(id="zero_judgment_errors", passed=None, detail="needs --db"))
        tier1.append(Check(id="cost_within_budget", passed=None, detail="needs --db"))
        cost_table = {}

    # ---- Tier 2 ---------------------------------------------------------------
    tier2: list[Check] = []
    cq = (cand.get("judgment_quality") or {})
    bq = (base or {}).get("judgment_quality") or {}
    c_f1 = (cq.get("materiality") or {}).get("f1") if isinstance(cq, dict) else None
    b_f1 = (bq.get("materiality") or {}).get("f1") if isinstance(bq, dict) else None
    tier2.append(_absolute("materiality_f1", c_f1, MATERIALITY_MIN_F1, "materiality F1 (pooled)", baseline=b_f1))

    c_personas = _persona_map(cand)
    b_personas = _persona_map(base) if base else {}
    for pid, p in c_personas.items():
        la = p.get("ledger_agreement") or {}
        bla = (b_personas.get(pid) or {}).get("ledger_agreement") or {}
        gated = la.get("gated", True)
        for metric in ("precision", "recall"):
            check = _paired_drop(
                f"ledger_{metric}", pid, la.get(metric), bla.get(metric) if base else None, LEDGER_MAX_DROP, f"ledger {metric}"
            )
            if not gated:
                check.passed = True if check.passed is not False else True
                check.detail += " [ungated persona: reported, not gated]"
                check.marginal = False
            tier2.append(check)

    u_val, u_n = _state_precision(cand, config.CONCEPT_UNKNOWN)
    b_u, _ = _state_precision(base, config.CONCEPT_UNKNOWN) if base else (None, 0)
    tier2.append(_absolute("unknown_precision", u_val, UNKNOWN_MIN_PRECISION, f"`unknown` precision (n={u_n})", baseline=b_u))
    c_val, c_n = _state_precision(cand, config.CONCEPT_CONFIRMED)
    b_c, _ = _state_precision(base, config.CONCEPT_CONFIRMED) if base else (None, 0)
    tier2.append(_absolute("confirmed_precision", c_val, CONFIRMED_MIN_PRECISION, f"`confirmed` precision (n={c_n})", baseline=b_c))

    reask_events: list[dict[str, Any]] = []
    if db:
        rate, reask_events, n = reask_rate(exchanges)
        b_rate = None
        if bdb:
            b_rate, _, _ = reask_rate(bdb.exchanges())
        if rate is None:
            tier2.append(Check(id="reask_rate", passed=None, detail="no briefings"))
        else:
            fails = []
            if rate > REASK_MAX_RATE:
                fails.append(f"above {REASK_MAX_RATE:.2f}")
            if b_rate is not None and (rate - b_rate) > REASK_MAX_RISE:
                fails.append(f"rose {rate - b_rate:+.3f} vs baseline {b_rate:.3f} (band {REASK_MAX_RISE:.2f})")
            over = max(rate - REASK_MAX_RATE, (rate - b_rate - REASK_MAX_RISE) if b_rate is not None else -1)
            tier2.append(
                Check(
                    id="reask_rate",
                    passed=not fails,
                    value=round(rate, 4),
                    baseline=None if b_rate is None else round(b_rate, 4),
                    threshold=REASK_MAX_RATE,
                    detail=f"re-ask rate {len(reask_events)}/{n} = {rate:.3f} (max {REASK_MAX_RATE:.2f}"
                    + (f", baseline {b_rate:.3f}, max rise {REASK_MAX_RISE:.2f}" if b_rate is not None else ", no baseline db")
                    + ")"
                    + ("; " + "; ".join(fails) if fails else ""),
                    items=[f"{ev['persona_id']} {ev['exchange_id']}: asked about {ev['terms']} which the briefing defined" for ev in reask_events],
                    marginal=bool(fails) and over <= MARGINAL_BAND,
                    data={"events": reask_events},
                )
            )
    else:
        tier2.append(Check(id="reask_rate", passed=None, detail="needs --db"))

    # Definitions ceiling, from the artifact's per-thread plainness bundle
    # (recomputed from the briefing text for artifacts that predate it).
    c_rows = thread_bundles(cand)
    b_rows = thread_bundles(base) if base else None
    tier2.append(definitions_ceiling(c_rows, b_rows))

    # Learning trend, paired per persona. Rebuilt from the artifact on both
    # sides so a baseline that predates the trend fields still pairs.
    from . import metrics as _metrics

    c_learning = {e.persona_id: e for e in _metrics.learning_report_from_artifact(cand)}
    b_learning = {e.persona_id: e for e in _metrics.learning_report_from_artifact(base)} if base else None
    learning_tier2, learning_rows = learning_checks(c_learning, b_learning)
    tier2.extend(learning_tier2)

    # ---- Tier 3 ---------------------------------------------------------------
    eng = (cand.get("thread_engagement") or {}).get("per_persona", [])
    engagement = [
        {
            "persona_id": e["persona_id"],
            "briefings": e.get("briefings"),
            "turn_rate_delta": e.get("turn_rate_delta"),
            "question_rate_delta": e.get("question_rate_delta"),
            "turns_per_thread_delta": e.get("turns_per_thread_delta"),
            "read_band_delta": e.get("mean_read_band_delta"),
            "later_reuse_rate": e.get("later_reuse_rate"),
        }
        for e in eng
    ]
    state_rows = (cand.get("per_state_accuracy") or {}).get("by_state", [])
    state_precision = {r["state"]: {"precision": r.get("accuracy"), "n": r.get("n"), "correct": r.get("correct"), "wrong_examples": r.get("wrong_examples", [])} for r in state_rows}
    b_state_rows = ((base or {}).get("per_state_accuracy") or {}).get("by_state", [])
    b_state_precision = {r["state"]: {"precision": r.get("accuracy"), "n": r.get("n")} for r in b_state_rows}
    turn_counts = [
        {
            "persona_id": pid,
            "threads": len(p.get("threads", [])),
            "user_turns": sum(t.get("user_turn_count", 0) for t in p.get("threads", [])),
            "system_turns": sum(t.get("system_turn_count", 0) for t in p.get("threads", [])),
            "quiet_rounds": sum(1 for r in p.get("rounds", []) if r.get("quiet")),
            "ledger_precision": (p.get("ledger_agreement") or {}).get("precision"),
            "ledger_recall": (p.get("ledger_agreement") or {}).get("recall"),
            "false_positives": (p.get("ledger_agreement") or {}).get("false_positives", []),
            "false_negatives": (p.get("ledger_agreement") or {}).get("false_negatives", []),
            "gated": (p.get("ledger_agreement") or {}).get("gated", True),
        }
        for pid, p in c_personas.items()
    ]
    tier3: dict[str, Any] = {
        "engagement_deviation": engagement,
        "state_precision": state_precision,
        "baseline_state_precision": b_state_precision,
        "provisional_precision": (cand.get("per_state_accuracy") or {}).get("provisional_accuracy"),
        "per_persona": turn_counts,
        "cost_and_latency": cost_table,
        "briefing_form": briefing_form(db, exchanges) if db else {},
        "plainness_by_band": plainness_by_band(c_rows) if c_rows else {},
        "baseline_plainness_by_band": plainness_by_band(b_rows) if b_rows else {},
        "reask_events": reask_events,
        "reply_hygiene": next((c.data.get("reply_hygiene", []) for c in tier1 if c.id == "no_fabricated_figure"), []),
        "question_faithfulness": {
            "clean": (cand.get("question_faithfulness") or {}).get("clean"),
            "violations": (cand.get("question_faithfulness") or {}).get("violations", []),
        },
        "interrupt_cases": (cand.get("interrupt_cases") or {}).get("summary"),
        "learning_vs_baseline": learning_rows,
        "learning_summary": _metrics.render_learning_summary(list(c_learning.values())) if c_learning else "",
        "pairing": pairing_signature(cand),
        "harness_routing": HarnessRouting.from_dict((cand.get("run_settings") or {}).get("harness_routing")).to_dict(),
    }

    report = GateReport(
        candidate=str(candidate_path),
        baseline=str(baseline_path) if baseline_path else None,
        db=str(db_path) if db_path else None,
        baseline_db=str(baseline_db_path) if baseline_db_path else None,
        run_id=db.run_id if db else (cand.get("volatile") or {}).get("run_id"),
        tier1=tier1,
        tier2=tier2,
        tier3=tier3,
        notes=notes,
    )
    if db:
        db.close()
    if bdb:
        bdb.close()
    return report


# --- Rendering -----------------------------------------------------------------


def _mark(passed: bool | None, marginal: bool = False) -> str:
    if passed is None:
        return "----"
    if passed:
        return "ok  "
    return "MARG" if marginal else "FAIL"


def _fmt(v: float | None) -> str:
    return "  n/a" if v is None else f"{v:5.3f}"


def render(report: GateReport, show_items: int = 12) -> str:
    lines: list[str] = []
    lines.append(f"REGRESSION GATE  {report.status}")
    lines.append(f"  candidate {report.candidate}" + (f"  (run {report.run_id})" if report.run_id else ""))
    if report.baseline:
        lines.append(f"  baseline  {report.baseline}")
    if report.db:
        lines.append(f"  db        {report.db}" + (f"   baseline db {report.baseline_db}" if report.baseline_db else ""))
    for n in report.notes:
        lines.append(f"  note: {n}")

    t1 = report.tier1_passed
    lines.append("")
    lines.append(f"TIER 1  hard gates  -- {'PASS' if t1 else ('INCOMPLETE' if t1 is None else 'FAIL')}")
    for c in report.tier1:
        lines.append(f"  {_mark(c.passed)}  {c.id:<24} {c.detail}")
        for item in c.items[:show_items]:
            for i, ln in enumerate(item.split("\n")):
                lines.append(("        - " if i == 0 else "          ") + ln)
        if len(c.items) > show_items:
            lines.append(f"        ... {len(c.items) - show_items} more")

    lines.append("")
    lines.append(f"TIER 2  scored vs baseline  -- {'PASS' if report.tier2_passed else 'FAIL'}" + ("" if report.baseline else "  (no baseline: absolute thresholds only)"))
    lines.append(f"  {'':4}  {'check':<22} {'persona':<20} {'value':>6} {'base':>6} {'band':>6}  detail")
    for c in report.tier2:
        lines.append(
            f"  {_mark(c.passed, c.marginal)}  {c.id:<22} {(c.persona or '-'):<20} {_fmt(c.value):>6} {_fmt(c.baseline):>6} {_fmt(c.threshold):>6}  {c.detail}"
        )
        for item in c.items[:show_items]:
            lines.append(f"        - {item}")

    t3 = report.tier3
    lines.append("")
    lines.append("TIER 3  reported, not gated")
    if t3.get("engagement_deviation"):
        lines.append("  engagement deviation (observed - configured expectation)")
        lines.append(f"    {'persona':<20} {'turn':>6} {'quest':>6} {'t/thr':>6} {'read':>6} {'reuse':>6}")
        for e in t3["engagement_deviation"]:
            lines.append(
                f"    {e['persona_id']:<20} {_fmt(e['turn_rate_delta']):>6} {_fmt(e['question_rate_delta']):>6} "
                f"{_fmt(e['turns_per_thread_delta']):>6} {_fmt(e['read_band_delta']):>6} {_fmt(e['later_reuse_rate']):>6}"
            )
    sp = t3.get("state_precision", {})
    if sp:
        lines.append("  per-state precision (split by read_explanations)")
        for state in sorted(sp):
            row = sp[state]
            b = (t3.get("baseline_state_precision") or {}).get(state)
            lines.append(
                f"    {state:<40} {_fmt(row['precision'])}  n={row['n']:<3}"
                + (f"  baseline {_fmt(b['precision'])} n={b['n']}" if b else "")
            )
    lines.append(f"  provisional precision: {_fmt(t3.get('provisional_precision'))}")
    if t3.get("per_persona"):
        lines.append("  per-persona turns")
        lines.append(f"    {'persona':<20} {'thr':>4} {'user':>5} {'sys':>4} {'quiet':>5} {'prec':>6} {'rec':>6}")
        for p in t3["per_persona"]:
            lines.append(
                f"    {p['persona_id']:<20} {p['threads']:>4} {p['user_turns']:>5} {p['system_turns']:>4} {p['quiet_rounds']:>5} "
                f"{_fmt(p['ledger_precision']):>6} {_fmt(p['ledger_recall']):>6}" + ("" if p["gated"] else "  (ungated)")
            )
    if t3.get("cost_and_latency"):
        lines.append("  cost and latency by judgment point")
        lines.append(f"    {'point':<20} {'calls':>5} {'err':>4} {'in_tok':>8} {'out_tok':>8} {'usd':>7} {'mean_ms':>8} {'p95_ms':>8}")
        total = 0.0
        for point, b in t3["cost_and_latency"].items():
            total += b["cost_usd"]
            lines.append(
                f"    {point:<20} {b['calls']:>5} {b['errors']:>4} {b['input_tokens']:>8} {b['output_tokens']:>8} "
                f"{b['cost_usd']:>7.3f} {b['mean_latency_ms']:>8.0f} {b['p95_latency_ms']:>8}"
            )
        lines.append(f"    {'total':<20} {'':>5} {'':>4} {'':>8} {'':>8} {total:>7.3f}")
    bf = t3.get("briefing_form") or {}
    if bf.get("per_briefing"):
        w, s, d = bf["words"], bf["sentences"], bf["definitions"]
        lines.append(
            f"  briefing form over {len(bf['per_briefing'])} briefings: words min/med/mean/max "
            f"{w['min']}/{w['median']}/{w['mean']}/{w['max']}; sentences {s['min']}/{s['median']}/{s['mean']}/{s['max']}; "
            f"inline definitions {d['min']}/{d['median']}/{d['mean']}/{d['max']}; "
            f">{LONG_BRIEFING_WORDS} words: {len(bf['long_briefings'])}; >{MANY_DEFINITIONS} definitions: {len(bf['many_definition_briefings'])}; "
            f"mention >1 event: {bf['multi_event_briefings']}"
        )
        for p in bf["per_briefing"]:
            if p["other_events_mentioned"]:
                lines.append(f"    {p['persona_id']} {p['exchange_id']} ({p['topic']}): also touches " + "; ".join(o["headline"][:70] for o in p["other_events"]))
    pb = t3.get("plainness_by_band") or {}
    if pb.get("all"):
        src = "from the artifact's per-thread bundle" + (f"; {pb['recomputed']} recomputed from the briefing text" if pb.get("recomputed") else "")
        lines.append(
            f"  briefing form, plainness by band at the time of the briefing (p50/p90; {src}).\n"
            f"    Expected under briefing v7: lower grade, shorter sentences and lower term density for beginners;\n"
            f"    glosses/100w (asides, dash pairs, 'meaning' cues) low in every band (ceiling {DEFINITIONS_CEILING_PER_100W:.1f}, gated in Tier 2 as definitions_ceiling).\n"
            f"    'made clear' is explained_terms/100w -- under v7 it includes plain-words explanations, so it is not a gloss count."
        )
        bpb = t3.get("baseline_plainness_by_band") or {}
        rows_out = [("all", pb["all"])] + list((pb.get("by_band") or {}).items())
        for band, summ in rows_out:
            lines.append(f"    {band:<12} n={summ['n']:<3} {format_summary(summ)}")
            b_summ = bpb["all"] if (band == "all" and bpb.get("all")) else (bpb.get("by_band") or {}).get(band)
            if b_summ:
                lines.append(f"    {'  baseline':<12} n={b_summ['n']:<3} {format_summary(b_summ)}")
    if t3.get("reply_hygiene"):
        lines.append(f"  reply hygiene: {len(t3['reply_hygiene'])} reply/replies carried a literal \\u escape to the user")
        for h in t3["reply_hygiene"][:show_items]:
            lines.append(f"    - {h}")
    qf = t3.get("question_faithfulness") or {}
    if qf.get("violations"):
        lines.append(f"  harness self-audit (question_faithfulness): {len(qf['violations'])} violation(s) -- about the personas, not the system")
        for v in qf["violations"][:show_items]:
            lines.append(f"    - {v}")
    if t3.get("interrupt_cases"):
        lines.append(f"  interrupt cases: {t3['interrupt_cases']}")
    hr = t3.get("harness_routing") or {}
    if hr:
        lines.append(f"  {HarnessRouting.from_dict(hr).voice_line}" + ("" if (report.tier3.get('pairing') or {}).get('header_recorded') else "  (not recorded in artifact; legacy defaults assumed)"))
    if t3.get("learning_vs_baseline"):
        lines.append("  learning vs baseline  (candidate / baseline; gated rows are in Tier 2)")
        lines.append(
            f"    {'persona':<20} {'k_slope':>15} {'eff/brf':>15} {'eff/100w':>15} {'ret@2-7d':>21} {'r_slope':>15} {'calib':>13} {'confab':>13}"
        )

        def pair(c: float | None, b: float | None, spec: str = ".2f") -> str:
            fc = "n/a" if c is None else format(c, spec)
            fb = "n/a" if b is None else format(b, spec)
            return f"{fc} / {fb}"

        for r in t3["learning_vs_baseline"]:
            ret = (
                f"{pair(r['retained_2_7d'], r['baseline_retained_2_7d'])} (n {r['retained_2_7d_n']}/{r['baseline_retained_2_7d_n']})"
            )
            lines.append(
                f"    {r['persona_id']:<20} {pair(r['known_slope'], r['baseline_known_slope'], '+.2f'):>15} "
                f"{pair(r['efficiency_per_briefing'], r['baseline_efficiency_per_briefing']):>15} "
                f"{pair(r['efficiency_per_100_words'], r['baseline_efficiency_per_100_words']):>15} "
                f"{ret:>21} {pair(r['retained_slope'], r['baseline_retained_slope'], '+.2f'):>15} "
                f"{pair(r['calibration_gap'], r['baseline_calibration_gap']):>13} "
                f"{pair(r['confabulation_rate'], r['baseline_confabulation_rate']):>13}"
            )
    if t3.get("learning_summary"):
        lines.append("  learning summary (candidate)")
        lines.append(t3["learning_summary"])

    lines.append("")
    lines.append(f"rerun advice: {report.rerun_advice}")
    lines.append(f"RESULT: {report.status}")
    return "\n".join(lines)


# --- CLI -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness.gate", description="Three-tier regression gate over a live-run artifact.")
    parser.add_argument("candidate", type=Path, help="candidate artifact (JSON written by harness.run)")
    parser.add_argument("--baseline", type=Path, default=None, help="baseline artifact for the paired Tier 2 comparison")
    parser.add_argument("--db", type=Path, default=None, help="candidate run database (default: sibling .db of the artifact)")
    parser.add_argument("--baseline-db", type=Path, default=None, help="baseline run database (default: sibling .db of the baseline)")
    parser.add_argument("--json", type=Path, default=None, help="write the machine-readable report here")
    parser.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST_USD, help="Tier 1 cost ceiling in USD (default 5.00)")
    parser.add_argument("--no-rerun-advice", dest="rerun_advice", action="store_false", help="omit the rerun-advice line from the terminal report")
    parser.add_argument("--show-items", type=int, default=12, help="max evidence lines per check in the terminal report")
    args = parser.parse_args(argv)

    report = evaluate(args.candidate, args.baseline, args.db, args.baseline_db, args.max_cost)
    text = render(report, show_items=args.show_items)
    if not args.rerun_advice:
        text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("rerun advice:"))
    print(text)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        print(f"\nJSON: {args.json}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
