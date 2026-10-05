"""`python -m harness.propose` -- the prompt proposer.

    PYTHONPATH=src:. .venv/bin/python -m harness.propose CANDIDATE.json --db CANDIDATE.db \
        [--baseline BASELINE.json] [--baseline-db BASELINE.db] [--max 3] [--out DIR] [--no-llm]
    PYTHONPATH=src:. .venv/bin/python -m harness.propose --apply proposals/<run_id>/proposal-02.patch

Reads a run through `harness.gate`, gathers verbatim evidence for everything
the gate failed or flagged, assembles the constraint set (human decisions in
`docs/checkpoints.md`, every "Changed" rule in `docs/prompt-changelog.md`, the spec's
non-goals, and the seven product rules), and makes ONE traced call to Opus 5
asking for at most `--max` minimal, evidence-cited changes. It writes
`proposals/<run_id>/` -- one markdown file and one unified-diff `.patch` per
proposal, plus `index.md` -- and stops.

It proposes; a human applies. Nothing in the pipeline edits `src/`,
`harness/` or a prompt. The single writing path is `--apply PATCH`, which
applies exactly one patch (validated with `patch --dry-run` first) and appends
the proposal's ready-made `docs/prompt-changelog.md` entry, then tells the operator
to run the gate. Even that path never runs unless the flag is given.

The LLM call is logged like every other judgment call -- `judgment_log` row,
`judgment_point = 'propose'`, `run_id = propose_<timestamp>` -- but into
`proposals/propose.db`, a store this tool owns, rather than into the run's own
database, which is an artifact of the run and is left untouched.

Evidence is gathered deterministically, without a model: for each Tier 1
failure, Tier 2 drop and Tier 3 anomaly, the briefing text, the user and
system turns, the `judgment_log` row (prompt version, model, reasoning) and the
exchange's `explained_terms` / `read_quality` are pulled from the store. A
Tier 3 anomaly is: a re-ask event; `familiar` / `explained` precision below
0.15 at fewer than two read explanations with n >= 10; a persona with recall
below 0.7; a briefing over 220 words or with more than four inline
definitions; a reply that carried a literal `\\u` escape to the user.
Evidence is capped at ~40 items, Tier 1 first.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from conversational_agent import config
from conversational_agent.judgment import JudgmentError, load_prompt
from conversational_agent.judgment import schema as judgment_schema
from conversational_agent.store import Store, new_id, normalize_term

from . import gate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROPOSALS_DIR = PROJECT_ROOT / "proposals"
PROPOSER_DB = PROPOSALS_DIR / "propose.db"
PROMPTS_REL = Path("src/conversational_agent/prompts")
CONFIG_REL = Path("src/conversational_agent/config.py")
CHANGELOG = PROJECT_ROOT / "docs" / "prompt-changelog.md"
CHECKPOINTS = PROJECT_ROOT / "docs" / "checkpoints.md"
SPEC = PROJECT_ROOT / "docs" / "spec.md"

PROPOSER_MODEL = config.OPUS  # claude-opus-5
PROPOSER_EFFORT = "high"
PROPOSER_POINT = "propose"
PROPOSER_PROMPT_VERSION = "propose-v1"
EVIDENCE_CAP = 40
KINDS = ("prompt", "config", "fixture_label", "harness_metric")
CONFIDENCE = ("low", "med", "high")

TUNABLE_CONSTANTS = (
    "READ_EXPLANATIONS_BEFORE_BAND",
    "CORRECT_USES_BEFORE_CONFIRMED",
    "MAX_SEARCH_USES",
    "LEDGER_MIN_PRECISION",
    "LEDGER_MIN_RECALL",
    "LEDGER_MAX_INTERACTIONS",
)

# Product rules a proposal may not reverse, independent of what the parsers
# find. Each is a decision recorded in docs/spec.md / docs/checkpoints.md / the changelog.
HARD_PRODUCT_RULES = (
    "Brief, then stop: the briefing states the substance and ends when the substance ends.",
    "Never ask the user a question -- no closing question, no invitation line, no offered thread, no 'hope that helps'.",
    "Knowledge is a ledger of concepts with evidence, never a score, level or grade; nothing numeric is shown or stored as proficiency.",
    "No promotion from silence or exposure: exposure counts are bookkeeping and promote nothing (the `assumed` state was deleted on measured evidence).",
    "Reading behaviour reaches the ledger only through the `familiar` door (a defined term in a non-skipped briefing, at thread close); dwell/scroll magnitude never touches a concept state.",
    "Never repeat a story; when there is nothing new, say nothing -- one untold event per briefing, a told event is never re-raised.",
    "Gloss a term once, then only on request: `provisional`, `familiar`, `explained` and `confirmed` are all no-gloss states.",
)

# Anomaly thresholds (Tier 3 is not gated; these decide what becomes evidence).
LOW_PRECISION_SPLIT = 0.15
LOW_PRECISION_MIN_N = 10
LOW_RECALL = 0.7


# --- Evidence -------------------------------------------------------------------


@dataclass
class Evidence:
    id: str
    tier: int
    kind: str
    title: str
    persona: str | None = None
    exchange_id: str | None = None
    briefing: str | None = None
    read_quality: str | None = None
    explained_terms: list[str] = field(default_factory=list)
    asked_about: list[str] = field(default_factory=list)
    understood: list[str] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    judgments: list[dict[str, Any]] = field(default_factory=list)
    concept: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _parse(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class Collector:
    """Deterministic evidence gathering over the run's store and gate report."""

    def __init__(self, db: gate.RunDb, report: dict[str, Any], artifact: dict[str, Any]):
        self.db = db
        self.report = report
        self.artifact = artifact
        self.items: list[Evidence] = []
        self._seen: set[tuple[str, str]] = set()
        self.exchanges = db.exchanges()
        self.by_id = {e["id"]: e for e in self.exchanges}
        self.by_persona: dict[str, list[dict[str, Any]]] = {}
        for e in self.exchanges:
            self.by_persona.setdefault(e["persona_id"], []).append(e)
        # Judgment rows keyed by the briefing they were about; the log has no
        # exchange id, but every generative/extractive call carries the
        # briefing text, which is unique per exchange.
        self.rows: list[dict[str, Any]] = []
        self.rows_by_briefing: dict[str, list[dict[str, Any]]] = {}
        self.rows_by_id: dict[str, dict[str, Any]] = {}
        for r in db.judgments(include_probes=True):
            ctx = (_parse(r["input_json"]) or {}).get("context", {}) if r["input_json"] else {}
            verdict = _parse(r["verdict_json"]) or {}
            row = {
                "id": r["id"],
                "point": r["judgment_point"],
                "prompt_version": r["prompt_version"],
                "model": r["model"],
                "user_id": r["user_id"],
                "reasoning": r["reasoning"],
                "error": r["error"],
                "verdict": verdict,
                "context": ctx,
            }
            self.rows.append(row)
            self.rows_by_id[r["id"]] = row
            key = None
            if r["judgment_point"] == config.BRIEFING:
                key = verdict.get("briefing")
            else:
                key = ctx.get("briefing_we_gave") or ctx.get("briefing")
            if key:
                self.rows_by_briefing.setdefault(key, []).append(row)
        self.concepts: dict[str, dict[str, dict[str, Any]]] = {}
        for pid, uid in db.persona_to_user.items():
            self.concepts[pid] = {normalize_term(c["term"]): c for c in db.concepts(uid)}

    # -- helpers -------------------------------------------------------------------

    def _judgment_view(self, row: dict[str, Any], *, verdict_chars: int = 700) -> dict[str, Any]:
        verdict = row["verdict"]
        view = {
            "id": row["id"],
            "point": row["point"],
            "prompt_version": row["prompt_version"],
            "model": row["model"],
            "reasoning": row["reasoning"],
        }
        if row["error"]:
            view["error"] = row["error"][:300]
        if verdict:
            slim = {k: v for k, v in verdict.items() if k not in ("reasoning",)}
            text = json.dumps(slim, ensure_ascii=False)
            view["verdict"] = text if len(text) <= verdict_chars else text[:verdict_chars] + "..."
        return view

    def rows_for(self, exch: dict[str, Any], points: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        rows = self.rows_by_briefing.get(exch["briefing"] or "", [])
        if points:
            rows = [r for r in rows if r["point"] in points]
        return rows

    def exchange_evidence(self, exch: dict[str, Any], *, tier: int, kind: str, title: str,
                          points: tuple[str, ...] = (config.BRIEFING, config.CONCEPT_EVIDENCE),
                          term: str | None = None, extra: dict[str, Any] | None = None,
                          include_turns: bool = True) -> Evidence | None:
        key = (kind, exch["id"] + (f":{term}" if term else ""))
        if key in self._seen or len(self.items) >= EVIDENCE_CAP:
            return None
        self._seen.add(key)
        ev = Evidence(
            id=f"E{len(self.items) + 1:02d}",
            tier=tier,
            kind=kind,
            title=title,
            persona=exch["persona_id"],
            exchange_id=exch["id"],
            briefing=exch["briefing"],
            read_quality=exch["read_quality"],
            explained_terms=list(exch["explained_terms"]),
            asked_about=list(exch["asked_about"]),
            understood=list(exch["understood"]),
            turns=self.db.turns(exch["id"]) if include_turns else [],
            judgments=[self._judgment_view(r) for r in self.rows_for(exch, points)],
            concept=(self.concepts.get(exch["persona_id"], {}).get(normalize_term(term)) if term else None),
            extra=extra or {},
        )
        self.items.append(ev)
        return ev

    def plain_evidence(self, *, tier: int, kind: str, title: str, persona: str | None = None,
                       judgments: list[dict[str, Any]] | None = None, concept: dict[str, Any] | None = None,
                       extra: dict[str, Any] | None = None) -> Evidence | None:
        key = (kind, title)
        if key in self._seen or len(self.items) >= EVIDENCE_CAP:
            return None
        self._seen.add(key)
        ev = Evidence(id=f"E{len(self.items) + 1:02d}", tier=tier, kind=kind, title=title, persona=persona,
                      judgments=[self._judgment_view(r) for r in (judgments or [])], concept=concept, extra=extra or {})
        self.items.append(ev)
        return ev

    def exchanges_glossing(self, persona: str, term: str) -> list[dict[str, Any]]:
        t = normalize_term(term)
        out = [e for e in self.by_persona.get(persona, []) if any(gate._asks_about(x, [t]) for x in e["explained_terms"])]
        if not out:
            out = [e for e in self.by_persona.get(persona, []) if t in normalize_term(e["briefing"] or "")]
        return out

    def exchanges_evidencing(self, persona: str, term: str) -> list[dict[str, Any]]:
        t = normalize_term(term)
        return [
            e for e in self.by_persona.get(persona, [])
            if any(gate._asks_about(x, [t]) for x in e["understood"] + e["asked_about"] + e["not_understood"])
        ]

    def exchange_for_judgment(self, row: dict[str, Any]) -> dict[str, Any] | None:
        key = row["context"].get("briefing_we_gave") or row["verdict"].get("briefing")
        return next((e for e in self.exchanges if e["briefing"] == key), None)

    # -- tiers -----------------------------------------------------------------------

    def gather(self) -> list[Evidence]:
        self._tier1()
        self._tier2()
        self._tier3()
        return self.items

    def _tier1(self) -> None:
        for check in self.report["tier1"]["checks"]:
            if check["passed"] is not False:
                continue
            cid = check["id"]
            if cid in ("reading_regression", "silence_regression"):
                for item in check["items"]:
                    m = re.match(r"^(\w+): '([^']+)'", item)
                    if not m:
                        continue
                    persona, term = m.group(1), m.group(2)
                    for e in self.exchanges_glossing(persona, term)[:2]:
                        self.exchange_evidence(e, tier=1, kind=cid, title=item, term=term)
                    if not self.exchanges_glossing(persona, term):
                        self.plain_evidence(tier=1, kind=cid, title=item, persona=persona,
                                            concept=self.concepts.get(persona, {}).get(normalize_term(term)))
            elif cid == "no_fabricated_figure":
                for item in check["items"]:
                    m = re.search(r"\[(jdg_[0-9a-f]+)\]", item)
                    row = self.rows_by_id.get(m.group(1)) if m else None
                    if row is None:
                        continue
                    exch = self.exchange_for_judgment(row)
                    extra = {"flag": item, "question": row["context"].get("question"), "lookup": row["context"].get("lookup"),
                             "source_event_detail": (row["context"].get("source_event") or {}).get("detail")}
                    if exch:
                        self.exchange_evidence(exch, tier=1, kind=cid, title=item.split("\n")[0], points=(config.THREAD_REPLY, config.GAP_ROUTING), extra=extra)
                    else:
                        self.plain_evidence(tier=1, kind=cid, title=item.split("\n")[0], judgments=[row], extra=extra)
            elif cid == "never_repeat":
                for item in check["items"]:
                    for eid in re.findall(r"\[(exch_[0-9a-f]+)\]|exchange (exch_[0-9a-f]+)", item):
                        eid = eid[0] or eid[1]
                        if eid in self.by_id:
                            self.exchange_evidence(self.by_id[eid], tier=1, kind=cid, title=item.split("\n")[0], points=(config.BRIEFING, config.INTERRUPT_TIMING), include_turns=False)
            elif cid == "zero_judgment_errors":
                rows = [r for r in self.rows if r["error"] and not self.db.is_probe(r["user_id"])]
                by_point: dict[str, list[dict[str, Any]]] = {}
                for r in rows:
                    by_point.setdefault(r["point"], []).append(r)
                for point, group in by_point.items():
                    self.plain_evidence(tier=1, kind=cid, title=f"{len(group)} errored {point} call(s); first: {group[0]['error'][:200]}", judgments=group[:2])
            elif cid == "cost_within_budget":
                self.plain_evidence(tier=1, kind=cid, title=check["detail"], extra={"by_point": self.report["tier3"].get("cost_and_latency")})
            elif cid == "no_questions_to_user":
                for item in check["items"]:
                    m = re.match(r"^(\w+) r(\d+): the briefing asks the reader '(.+)'$", item)
                    if not m:
                        continue
                    persona, sentence = m.group(1), m.group(3)
                    for e in self.by_persona.get(persona, []):
                        if sentence[:40] in (e["briefing"] or ""):
                            self.exchange_evidence(e, tier=1, kind=cid, title=item, points=(config.BRIEFING,), include_turns=False)

    def _tier2(self) -> None:
        per_persona = {p["persona_id"]: p for p in self.report["tier3"].get("per_persona", [])}
        for check in self.report["tier2"]["checks"]:
            if check["passed"] is not False:
                continue
            cid, persona = check["id"], check.get("persona")
            if cid in ("ledger_precision", "ledger_recall") and persona:
                p = per_persona.get(persona, {})
                sentences = p.get("false_positives" if cid == "ledger_precision" else "false_negatives", [])
                for s in sentences:
                    m = re.search(r"'([^']+)'", s)
                    if not m:
                        continue
                    term = m.group(1)
                    for e in self.exchanges_evidencing(persona, term)[:2]:
                        self.exchange_evidence(e, tier=2, kind=cid, title=f"{persona}: {s} (vs baseline {check.get('baseline')})", term=term)
                    if not self.exchanges_evidencing(persona, term):
                        self.plain_evidence(tier=2, kind=cid, title=f"{persona}: {s}", persona=persona, concept=self.concepts.get(persona, {}).get(normalize_term(term)))
            elif cid == "materiality_f1":
                mq = (self.artifact.get("judgment_quality") or {}).get("materiality") or {}
                for label in ("false_negative_examples", "false_positive_examples"):
                    for s in mq.get(label, []):
                        rows = [r for r in self.rows if r["point"] == config.MATERIALITY and s.split(": ", 1)[-1][:60] in json.dumps(r["context"], ensure_ascii=False)]
                        self.plain_evidence(tier=2, kind=cid, title=f"{label}: {s}", judgments=rows[:1])
            elif cid in ("unknown_precision", "confirmed_precision"):
                state = cid.split("_")[0]
                sp = self.report["tier3"].get("state_precision", {}).get(state, {})
                for s in sp.get("wrong_examples", []):
                    m = re.match(r"^(\w+): '([^']+)'", s)
                    if not m:
                        continue
                    persona, term = m.group(1), m.group(2)
                    for e in self.exchanges_evidencing(persona, term)[:1]:
                        self.exchange_evidence(e, tier=2, kind=cid, title=s, term=term)
            elif cid == "reask_rate":
                for ev in check.get("data", {}).get("events", []):
                    exch = self.by_id.get(ev["exchange_id"])
                    if exch:
                        self.exchange_evidence(exch, tier=2, kind=cid, title=f"{ev['persona_id']} re-asked {ev['terms']} after the briefing defined them", points=(config.BRIEFING, config.THREAD_REPLY, config.CONCEPT_EVIDENCE), term=ev["terms"][0])

    def _tier3(self) -> None:
        t3 = self.report["tier3"]
        for ev in t3.get("reask_events", []):
            exch = self.by_id.get(ev["exchange_id"])
            if exch:
                self.exchange_evidence(exch, tier=3, kind="reask_event", title=f"{ev['persona_id']} asked about {ev['terms']} which this briefing had just defined", points=(config.BRIEFING, config.THREAD_REPLY, config.CONCEPT_EVIDENCE), term=ev["terms"][0])
        for state, row in t3.get("state_precision", {}).items():
            if "read_explanations <" in state and row.get("precision") is not None and row["precision"] < LOW_PRECISION_SPLIT and row.get("n", 0) >= LOW_PRECISION_MIN_N:
                for s in row.get("wrong_examples", [])[:6]:
                    m = re.match(r"^(\w+): '([^']+)'", s)
                    if not m:
                        continue
                    persona, term = m.group(1), m.group(2)
                    for e in self.exchanges_glossing(persona, term)[:1]:
                        self.exchange_evidence(e, tier=3, kind="low_precision_state", title=f"{state} precision {row['precision']:.2f} (n={row['n']}): {s}", points=(config.BRIEFING,), term=term)
        for p in t3.get("per_persona", []):
            rec = p.get("ledger_recall")
            if p.get("gated", True) and rec is not None and rec < LOW_RECALL:
                for s in p.get("false_negatives", []):
                    m = re.search(r"'([^']+)'", s)
                    if not m:
                        continue
                    term = m.group(1)
                    for e in self.exchanges_evidencing(p["persona_id"], term)[:1]:
                        self.exchange_evidence(e, tier=3, kind="low_recall", title=f"{p['persona_id']} recall {rec:.2f}: {s}", term=term)
        bf = t3.get("briefing_form") or {}
        for eid in bf.get("long_briefings", []) + bf.get("many_definition_briefings", []):
            exch = self.by_id.get(eid)
            if exch:
                stats = next((b for b in bf["per_briefing"] if b["exchange_id"] == eid), {})
                self.exchange_evidence(exch, tier=3, kind="briefing_form", title=f"{exch['persona_id']} briefing {stats.get('words')} words, {stats.get('definitions')} inline definitions", points=(config.BRIEFING,), include_turns=False, extra=stats)
        for h in t3.get("reply_hygiene", []):
            m = re.search(r"\[(jdg_[0-9a-f]+)\]", h)
            row = self.rows_by_id.get(m.group(1)) if m else None
            if row:
                self.plain_evidence(tier=3, kind="reply_hygiene", title=h, persona=self.db.user_to_persona.get(row["user_id"]), judgments=[row])


# --- Constraints ----------------------------------------------------------------


def _bullets_after(lines: list[str], start: int) -> list[str]:
    """Collect one markdown list item (with continuation lines) starting at `start`."""
    item = [lines[start].strip()]
    i = start + 1
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("  ") and ln.strip() and not ln.strip().startswith("- ["):
            item.append(ln.strip())
            i += 1
            continue
        break
    return [" ".join(item)]


def human_decisions() -> list[str]:
    if not CHECKPOINTS.exists():
        return []
    lines = CHECKPOINTS.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for i, ln in enumerate(lines):
        if ln.strip().startswith("- [x]"):
            text = _bullets_after(lines, i)[0]
            out.append(re.sub(r"\*\*", "", text)[6:].strip())
    return out


def changelog_rules() -> list[dict[str, str]]:
    if not CHANGELOG.exists():
        return []
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, str]] = []
    heading = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("## "):
            heading = ln[3:].strip()
        elif heading and ln.startswith("**Changed"):
            block = [ln]
            j = i + 1
            while j < len(lines) and not lines[j].startswith("**") and not lines[j].startswith("## ") and lines[j].strip() != "---":
                block.append(lines[j])
                j += 1
            text = "\n".join(block).strip()
            out.append({"heading": heading, "changed": text[:1500] + ("..." if len(text) > 1500 else "")})
            i = j
            continue
        i += 1
    return out


def spec_non_goals() -> list[str]:
    if not SPEC.exists():
        return []
    lines = SPEC.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    inside = False
    current: list[str] = []
    for ln in lines:
        if ln.startswith("## "):
            if inside:
                break
            inside = "does NOT do" in ln
            continue
        if not inside:
            continue
        if ln.startswith("- "):
            if current:
                out.append(" ".join(current))
            current = [ln[2:].strip()]
        elif ln.startswith("  ") and current:
            current.append(ln.strip())
        elif not ln.strip() and current:
            out.append(" ".join(current))
            current = []
    if current:
        out.append(" ".join(current))
    return [re.sub(r"\*\*", "", x) for x in out]


def constraint_set() -> dict[str, Any]:
    return {
        "product_rules": list(HARD_PRODUCT_RULES),
        "human_decisions_checkpoints": human_decisions(),
        "changelog_rules": changelog_rules(),
        "spec_non_goals": spec_non_goals(),
    }


# --- Prompts and config -----------------------------------------------------------


def current_prompts() -> dict[str, dict[str, str]]:
    out = {}
    for point in sorted(config.ROUTED_CALLS):
        path = config.PROMPTS_DIR / f"{point}.md"
        out[point] = {"path": str(path.relative_to(PROJECT_ROOT)), "version": load_prompt(point).version, "text": path.read_text(encoding="utf-8")}
    return out


def tunable_config() -> dict[str, Any]:
    return {name: getattr(config, name) for name in TUNABLE_CONSTANTS if hasattr(config, name)}


# --- The one LLM call ----------------------------------------------------------------

PROPOSER_SYSTEM = """You are the prompt proposer for a conversational news-briefing agent. You are handed the output of a regression gate over one evaluation run, verbatim evidence pulled from the run's database, a constraint set (human decisions and rules bought with past failures), the current text of every prompt, and the tunable configuration constants. You propose at most N minimal changes. A human applies them; you never do.

What a proposal is:
- kind `prompt`: a minimal edit to ONE prompt file. `change.old_text` must be an exact, contiguous, verbatim excerpt of the prompt text you were given (copy it; do not paraphrase, do not reflow, keep the original line breaks). `change.new_text` is the replacement. Prefer adding or sharpening one rule with one worked example drawn from the evidence over rewriting sections. Leave `constant` and `new_value` empty.
- kind `config`: change ONE tunable constant. Fill `change.constant` (a name from the supplied list) and `change.new_value`; leave old_text/new_text empty.
- kind `fixture_label`: the evidence shows the persona fixture, not the system, is wrong (a mislabelled concept or event). `target` is the fixture path; describe the label to change in old_text/new_text.
- kind `harness_metric`: the evidence shows the measurement is wrong or misleading, not the system. Target `harness/metrics.py` or `harness/gate.py`; describe the change in old_text/new_text.

Rules:
1. Every proposal cites at least one supplied evidence id and the diagnosis must follow from that evidence. Do not propose from general taste.
2. Never reverse a constraint. The constraint set contains human decisions (marked [x] in CHECKPOINTS), every 'Changed' rule from the changelog, the spec's non-goals and seven product rules. If a proposal touches any of them, list it in `conflicts_with` and argue in `conflict_argument` why the change is not a reversal (e.g. it tightens the rule, or adds a guard the rule anticipated). If you cannot make that argument, do not propose it.
3. Never relax a gate or a threshold to make a failure pass. A `harness_metric` proposal is for a measurement that is demonstrably wrong about what happened, and must say what the evidence shows the metric got wrong.
4. Diagnose the mechanism, not the symptom, in at most 120 words: which call, which prompt version, what it saw, what it decided, why.
5. `expected_effect`: name the gate or metric that should move, the direction, and a rough magnitude. `risk`: what could get worse and which Tier 1 gate would catch it.
6. Prefer proposals with distinct mechanisms over three variants of one idea. Fewer, better proposals beat the maximum.
7. Bug fixes belong in `src/` code, not prompts; if the evidence points at a code bug (e.g. an encoding defect), say so as a harness_metric or note it in the diagnosis of a related proposal rather than trying to prompt around it.

Return only the structured object requested."""


def proposal_schema(max_n: int) -> dict[str, Any]:
    proposal = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "target": {"type": "string", "description": "File path relative to the project root."},
            "title": {"type": "string"},
            "diagnosis": {"type": "string", "description": "What failed and the mechanism, at most 120 words."},
            "evidence_ids": {"type": "array", "items": {"type": "string"}, "description": "Ids of supplied evidence items this rests on. At least one."},
            "change": {
                "type": "object",
                "properties": {
                    "old_text": {"type": "string", "description": "prompt: exact verbatim excerpt to replace. Empty for config."},
                    "new_text": {"type": "string", "description": "prompt: replacement text. Empty for config."},
                    "constant": {"type": "string", "description": "config: constant name. Empty otherwise."},
                    "new_value": {"type": "string", "description": "config: new value as Python literal. Empty otherwise."},
                },
                "required": ["old_text", "new_text", "constant", "new_value"],
                "additionalProperties": False,
            },
            "expected_effect": {"type": "string"},
            "risk": {"type": "string"},
            "conflicts_with": {"type": "array", "items": {"type": "string"}},
            "conflict_argument": {"type": "string", "description": "Why the change is not a reversal of the listed constraints. Empty when conflicts_with is empty."},
            "confidence": {"type": "string", "enum": list(CONFIDENCE)},
        },
        "required": ["kind", "target", "title", "diagnosis", "evidence_ids", "change", "expected_effect", "risk", "conflicts_with", "conflict_argument", "confidence"],
        "additionalProperties": False,
    }
    return judgment_schema(
        {
            "proposals": {"type": "array", "items": proposal, "description": f"At most {max_n} proposals."},
        },
        ["proposals"],
    )


def slim_report(report: dict[str, Any]) -> dict[str, Any]:
    """The gate report without the per-briefing table (it is long and the flagged rows are in the evidence)."""
    out = json.loads(json.dumps(report))
    bf = out.get("tier3", {}).get("briefing_form") or {}
    bf.pop("per_briefing", None)
    for check in out.get("tier2", {}).get("checks", []):
        check.pop("data", None)
    for check in out.get("tier1", {}).get("checks", []):
        check.get("data", {}).pop("reply_hygiene", None)
    return out


class _StreamingMessages:
    """`messages.create(...)` that streams underneath and returns the final message.

    The proposer's output is a few thousand tokens of JSON, but Opus 5 thinks
    adaptively and thinking is billed against `max_tokens`. The first live
    call hit a 20,000 budget mid-string (checkpoint 2 recorded the same
    failure on `interrupt_timing`), and the SDK refuses non-streaming calls
    with a budget much larger than that. Streaming lifts the ceiling; the
    project's `AnthropicClient` is kept as the code path so the call is built
    and logged exactly like every other judgment call.
    """

    def __init__(self, inner: Any):
        self._inner = inner

    def create(self, **kwargs: Any) -> Any:
        with self._inner.messages.stream(**kwargs) as stream:
            return stream.get_final_message()


class _StreamingClient:
    def __init__(self, inner: Any):
        self.messages = _StreamingMessages(inner)


PROPOSER_MAX_TOKENS = 64000


def call_proposer(payload: dict[str, Any], max_n: int, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """One traced Opus 5 call. Returns (verdict, trace)."""
    import anthropic

    from conversational_agent.judgment import AnthropicClient

    client = AnthropicClient(client=_StreamingClient(anthropic.Anthropic()))
    schema = proposal_schema(max_n)
    user_content = json.dumps({"_judgment_point": PROPOSER_POINT, "context": payload}, indent=1, sort_keys=True, default=str, ensure_ascii=False)
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    store = Store(PROPOSER_DB)
    started = time.perf_counter()
    error = None
    raw = None
    data: dict[str, Any] = {}
    try:
        raw = client.complete_json(model=PROPOSER_MODEL, system=PROPOSER_SYSTEM, user_content=user_content, schema=schema, effort=PROPOSER_EFFORT, max_tokens=PROPOSER_MAX_TOKENS)
        data = json.loads(raw.text)
    except JudgmentError as exc:
        error = str(exc)
    except json.JSONDecodeError as exc:
        error = f"Model returned unparseable JSON: {exc}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = int((time.perf_counter() - started) * 1000)
    log_id = new_id("jdg")
    store.log_judgment(
        {
            "id": log_id,
            "judgment_point": PROPOSER_POINT,
            "prompt_version": PROPOSER_PROMPT_VERSION,
            "model": PROPOSER_MODEL,
            "effort": PROPOSER_EFFORT,
            "input_json": user_content,
            "verdict_json": json.dumps(data) if data else None,
            "reasoning": str(data.get("reasoning", "")) if data else "",
            "input_tokens": raw.input_tokens if raw else None,
            "output_tokens": raw.output_tokens if raw else None,
            "latency_ms": latency_ms,
            "error": error,
            "run_id": run_id,
        }
    )
    store.close()
    prices = gate.PRICES_PER_MTOK.get(PROPOSER_MODEL, (0.0, 0.0))
    trace = {
        "log_id": log_id,
        "log_db": str(PROPOSER_DB.relative_to(PROJECT_ROOT)),
        "run_id": run_id,
        "model": PROPOSER_MODEL,
        "effort": PROPOSER_EFFORT,
        "input_tokens": raw.input_tokens if raw else None,
        "output_tokens": raw.output_tokens if raw else None,
        "latency_ms": latency_ms,
        "cost_usd": round(((raw.input_tokens * prices[0] + raw.output_tokens * prices[1]) / 1_000_000), 4) if raw else None,
        "error": error,
    }
    if error:
        raise JudgmentError(f"[{PROPOSER_POINT}] {error} (logged as {log_id})")
    return data, trace


# --- Rendering: patches, markdown, index ---------------------------------------------


def locate(haystack: str, needle: str) -> tuple[int, int] | None:
    """Find `needle` in `haystack`, exactly or with whitespace runs collapsed."""
    if not needle:
        return None
    pos = haystack.find(needle)
    if pos >= 0:
        return pos, pos + len(needle)
    # whitespace-tolerant: map normalised offsets back to the original
    norm_chars: list[str] = []
    index: list[int] = []
    prev_space = False
    for i, ch in enumerate(haystack):
        if ch.isspace():
            if prev_space:
                continue
            prev_space = True
            norm_chars.append(" ")
            index.append(i)
        else:
            prev_space = False
            norm_chars.append(ch)
            index.append(i)
    norm = "".join(norm_chars)
    n_needle = " ".join(needle.split())
    pos = norm.find(n_needle)
    if pos < 0:
        return None
    start = index[pos]
    end = index[pos + len(n_needle) - 1] + 1
    return start, end


_VERSION_LINE = re.compile(r"^(version:\s*v?)(\d+)(\s*)$", re.MULTILINE)


def bump_version(text: str) -> tuple[str, str, str]:
    """Bump `version: vN` in the front-matter; returns (new_text, old_version, new_version)."""
    m = _VERSION_LINE.search(text)
    if not m:
        return text, "unversioned", "unversioned"
    old = f"v{m.group(2)}"
    new = f"v{int(m.group(2)) + 1}"
    return text[: m.start()] + f"{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}" + text[m.end():], old, new


def unified_patch(rel_path: str, old: str, new: str) -> str:
    diff = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=rel_path, tofile=rel_path)
    text = "".join(diff)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def patch_applies(patch_path: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["patch", "-p0", "--dry-run", "--forward", "-d", str(PROJECT_ROOT), "-i", str(patch_path)],
        capture_output=True, text=True,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def build_patch(proposal: dict[str, Any]) -> dict[str, Any]:
    """Apply old->new to a copy of the target; return patch text and version info."""
    kind = proposal["kind"]
    target = Path(proposal["target"])
    abs_target = PROJECT_ROOT / target
    result: dict[str, Any] = {"patch": None, "note": None, "old_version": None, "new_version": None}
    if kind == "config":
        const, value = proposal["change"]["constant"].strip(), proposal["change"]["new_value"].strip()
        if const not in TUNABLE_CONSTANTS:
            result["note"] = f"constant {const!r} is not tunable ({', '.join(TUNABLE_CONSTANTS)})"
            return result
        text = (PROJECT_ROOT / CONFIG_REL).read_text(encoding="utf-8")
        m = re.search(rf"^{re.escape(const)}\s*=\s*.+$", text, re.MULTILINE)
        if not m:
            result["note"] = f"could not find `{const} = ...` in {CONFIG_REL}"
            return result
        new = text[: m.start()] + f"{const} = {value}" + text[m.end():]
        proposal["target"] = str(CONFIG_REL)
        result["patch"] = unified_patch(str(CONFIG_REL), text, new)
        return result
    if not abs_target.exists() or not abs_target.is_file():
        result["note"] = f"target {target} does not exist; no patch generated"
        return result
    old_text, new_text = proposal["change"]["old_text"], proposal["change"]["new_text"]
    text = abs_target.read_text(encoding="utf-8")
    span = locate(text, old_text)
    if span is None:
        result["note"] = "old_text not found in target (even whitespace-tolerantly); no patch generated"
        return result
    new = text[: span[0]] + new_text + text[span[1]:]
    if kind == "prompt" and target.parent == PROMPTS_REL:
        new, result["old_version"], result["new_version"] = bump_version(new)
    result["patch"] = unified_patch(str(target), text, new)
    return result


def changelog_entry(proposal: dict[str, Any], built: dict[str, Any], evidence_by_id: dict[str, Evidence], date: str) -> str:
    point = Path(proposal["target"]).stem if proposal["kind"] == "prompt" else proposal["target"]
    version = f" {built['new_version']}" if built.get("new_version") else ""
    lines = [f"## `{point}`{version} — {date} (PROPOSED, not yet measured)", ""]
    ev_titles = [f"{eid}: {evidence_by_id[eid].title}" for eid in proposal["evidence_ids"] if eid in evidence_by_id]
    lines.append(f"**Failed:** {proposal['diagnosis'].strip()}")
    if ev_titles:
        lines.append("")
        lines.append("Evidence: " + "; ".join(ev_titles) + ".")
    lines.append("")
    if proposal["kind"] == "config":
        lines.append(f"**Changed:** `{proposal['change']['constant']}` set to `{proposal['change']['new_value']}`. {proposal['title']}")
    else:
        lines.append(f"**Changed:** {proposal['title']} (see the diff in `proposals/`).")
    lines.append("")
    lines.append(f"**What it costs or bought:** expected — {proposal['expected_effect'].strip()} Risk — {proposal['risk'].strip()}")
    if proposal.get("conflicts_with"):
        lines.append("")
        lines.append("**Touches:** " + "; ".join(proposal["conflicts_with"]) + ". Why this is not a reversal: " + proposal.get("conflict_argument", "").strip())
    lines.append("")
    return "\n".join(lines)


def _quote(text: str | None) -> str:
    if not text:
        return "> (empty)"
    return "\n".join("> " + ln for ln in text.strip().splitlines())


def render_evidence(ev: Evidence) -> str:
    out = [f"#### {ev.id} — {ev.title}", ""]
    meta = [f"tier {ev.tier}", ev.kind]
    if ev.persona:
        meta.append(f"persona `{ev.persona}`")
    if ev.exchange_id:
        meta.append(f"exchange `{ev.exchange_id}`")
    if ev.read_quality:
        meta.append(f"read_quality `{ev.read_quality}`")
    out.append("*" + " · ".join(meta) + "*")
    out.append("")
    if ev.briefing:
        out.append("**Briefing:**")
        out.append(_quote(ev.briefing))
        out.append("")
    if ev.explained_terms or ev.asked_about or ev.understood:
        out.append(f"`explained_terms` {ev.explained_terms} · `asked_about` {ev.asked_about} · `understood` {ev.understood}")
        out.append("")
    if ev.turns:
        out.append("**Thread:**")
        for t in ev.turns:
            out.append(f"- **{t['speaker']}** ({t['seq']}): {t['text']}")
        out.append("")
    if ev.concept:
        out.append(f"**Ledger row:** `{ev.concept['term']}` state `{ev.concept['state']}`, exposure {ev.concept['exposure_count']}, correct_uses {ev.concept['correct_uses']}, misunderstandings {ev.concept['misunderstandings']}, read_explanations {ev.concept['read_explanations']}, evidence: *{ev.concept['evidence']}*")
        out.append("")
    for j in ev.judgments:
        out.append(f"**Judgment `{j['id']}`** — `{j['point']}` {j['prompt_version']} on `{j['model']}`")
        if j.get("error"):
            out.append(f"- error: `{j['error']}`")
        if j.get("reasoning"):
            out.append(f"- reasoning: {j['reasoning']}")
        if j.get("verdict"):
            out.append(f"- verdict: `{j['verdict']}`")
        out.append("")
    if ev.extra:
        out.append("**Extra:** `" + json.dumps(ev.extra, ensure_ascii=False, default=str)[:1500] + "`")
        out.append("")
    return "\n".join(out)


def render_proposal(n: int, p: dict[str, Any], built: dict[str, Any], applies: tuple[bool, str] | None,
                    evidence_by_id: dict[str, Evidence], entry: str, patch_name: str | None) -> str:
    out = [f"# Proposal {n:02d} — {p['title']}", ""]
    out.append(f"- **kind:** `{p['kind']}`")
    out.append(f"- **target:** `{p['target']}`")
    out.append(f"- **confidence:** {p['confidence']}")
    out.append(f"- **evidence:** {', '.join(p['evidence_ids'])}")
    if built.get("old_version"):
        out.append(f"- **version:** {built['old_version']} → {built['new_version']}")
    if patch_name:
        status = "applies cleanly (`patch --dry-run`)" if applies and applies[0] else f"DOES NOT APPLY: {applies[1] if applies else ''}"
        out.append(f"- **patch:** `{patch_name}` — {status}")
    else:
        out.append(f"- **patch:** none — {built.get('note')}")
    out.append("")
    out.append("## Diagnosis")
    out.append("")
    out.append(p["diagnosis"].strip())
    out.append("")
    out.append("## Change")
    out.append("")
    if p["kind"] == "config":
        out.append(f"`{p['change']['constant']}` → `{p['change']['new_value']}`")
    else:
        out.append("**Old:**")
        out.append("")
        out.append("```")
        out.append(p["change"]["old_text"].rstrip("\n"))
        out.append("```")
        out.append("")
        out.append("**New:**")
        out.append("")
        out.append("```")
        out.append(p["change"]["new_text"].rstrip("\n"))
        out.append("```")
    out.append("")
    out.append("## Expected effect")
    out.append("")
    out.append(p["expected_effect"].strip())
    out.append("")
    out.append("## Risk")
    out.append("")
    out.append(p["risk"].strip())
    out.append("")
    if p.get("conflicts_with"):
        out.append("## Constraints touched, and why this is not a reversal")
        out.append("")
        for c in p["conflicts_with"]:
            out.append(f"- {c}")
        out.append("")
        out.append(p.get("conflict_argument", "").strip())
        out.append("")
    out.append("## Ready-to-paste `docs/prompt-changelog.md` entry")
    out.append("")
    out.append("```markdown")
    out.append(entry.rstrip())
    out.append("```")
    out.append("")
    out.append("## Evidence (verbatim)")
    out.append("")
    for eid in p["evidence_ids"]:
        ev = evidence_by_id.get(eid)
        if ev:
            out.append(render_evidence(ev))
    return "\n".join(out) + "\n"


def validate(proposals: list[dict[str, Any]], evidence_ids: set[str], max_n: int) -> tuple[list[dict[str, Any]], list[str]]:
    kept: list[dict[str, Any]] = []
    rejected: list[str] = []
    for p in proposals:
        title = p.get("title", "(untitled)")
        ids = [e for e in p.get("evidence_ids", []) if e in evidence_ids]
        if not ids:
            rejected.append(f"{title}: cites no supplied evidence item ({p.get('evidence_ids')})")
            continue
        p["evidence_ids"] = ids
        if p.get("conflicts_with") and not p.get("conflict_argument", "").strip():
            rejected.append(f"{title}: touches {p['conflicts_with']} without arguing why it is not a reversal")
            continue
        if p.get("kind") not in KINDS:
            rejected.append(f"{title}: unknown kind {p.get('kind')!r}")
            continue
        if p["kind"] == "prompt" and not (p["change"].get("old_text") and p["change"].get("new_text")):
            rejected.append(f"{title}: prompt proposal without old_text/new_text")
            continue
        if p["kind"] == "prompt" and p["change"]["old_text"].strip() == p["change"]["new_text"].strip():
            rejected.append(f"{title}: old and new text are identical")
            continue
        kept.append(p)
    if len(kept) > max_n:
        rejected.extend(f"{p['title']}: over --max {max_n}, dropped" for p in kept[max_n:])
        kept = kept[:max_n]
    return kept, rejected


# --- Apply ---------------------------------------------------------------------------


def apply_patch(patch_path: Path) -> int:
    patch_path = patch_path.resolve()
    if not patch_path.exists():
        print(f"No such patch: {patch_path}")
        return 2
    ok, msg = patch_applies(patch_path)
    if not ok:
        print(f"Refusing: the patch does not apply cleanly.\n{msg}")
        return 1
    proc = subprocess.run(
        ["patch", "-p0", "--forward", "--no-backup-if-mismatch", "-d", str(PROJECT_ROOT), "-i", str(patch_path)],
        capture_output=True, text=True,
    )
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip())
        return 1
    targets = re.findall(r"^\+\+\+ (\S+)", patch_path.read_text(encoding="utf-8"), re.MULTILINE)
    entry_path = patch_path.with_suffix(".changelog.md")
    if entry_path.exists():
        entry = entry_path.read_text(encoding="utf-8").rstrip() + "\n\n---\n\n"
        text = CHANGELOG.read_text(encoding="utf-8")
        # Insert after the intro separator (the first `---` line after "Format:").
        anchor = text.find("\n---\n", text.find("Format:"))
        if anchor < 0:
            CHANGELOG.write_text(text.rstrip() + "\n\n---\n\n" + entry, encoding="utf-8")
        else:
            cut = anchor + len("\n---\n")
            CHANGELOG.write_text(text[:cut] + "\n" + entry + text[cut:].lstrip("\n"), encoding="utf-8")
        print(f"Appended changelog entry from {entry_path.name} to {CHANGELOG.relative_to(PROJECT_ROOT)}")
    else:
        print("No sibling .changelog.md found; docs/prompt-changelog.md not touched.")
    print("Changed: " + ", ".join(targets))
    print(
        "Now run the gate on a fresh live run before trusting it:\n"
        "  PYTHONPATH=src:. .venv/bin/python -m harness.run --live --parallel 5 --json eval-runs/live/<name>.json --db eval-runs/live/<name>.db\n"
        "  PYTHONPATH=src:. .venv/bin/python -m harness.gate eval-runs/live/<name>.json --baseline eval-runs/live/all5c.json"
    )
    return 0


# --- Main ----------------------------------------------------------------------------


def propose(candidate: Path, db_path: Path | None, baseline: Path | None, baseline_db: Path | None,
            max_n: int, out_dir: Path | None, use_llm: bool = True) -> Path:
    report_obj = gate.evaluate(candidate, baseline, db_path, baseline_db)
    report = report_obj.to_dict()
    artifact = json.loads(candidate.read_text(encoding="utf-8"))
    db_path = Path(report["db"]) if report.get("db") else None
    if db_path is None:
        raise SystemExit("propose needs the run database (--db); the evidence is in it")
    run_id = report.get("run_id") or candidate.stem
    out = out_dir or (PROPOSALS_DIR / run_id)
    out.mkdir(parents=True, exist_ok=True)
    (out / "gate-report.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    db = gate.RunDb(db_path, artifact)
    collector = Collector(db, report, artifact)
    evidence = collector.gather()
    db.close()
    evidence_by_id = {e.id: e for e in evidence}
    (out / "evidence.json").write_text(json.dumps([asdict(e) for e in evidence], indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8")
    constraints = constraint_set()
    (out / "constraints.json").write_text(json.dumps(constraints, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    payload = {
        "task": f"Propose at most {max_n} changes. Run {run_id}; gate status {report['status']}.",
        "gate_report": slim_report(report),
        "evidence": [asdict(e) for e in evidence],
        "constraints": constraints,
        "prompts": current_prompts(),
        "tunable_config": tunable_config(),
        "max_proposals": max_n,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    propose_run_id = f"propose_{stamp}"
    print(f"gate: {report['status']}; evidence items: {len(evidence)} "
          f"(tier1 {sum(e.tier == 1 for e in evidence)}, tier2 {sum(e.tier == 2 for e in evidence)}, tier3 {sum(e.tier == 3 for e in evidence)}); "
          f"constraints: {len(constraints['human_decisions_checkpoints'])} human decisions, {len(constraints['changelog_rules'])} changelog rules, "
          f"{len(constraints['spec_non_goals'])} spec non-goals, {len(constraints['product_rules'])} product rules")

    if not use_llm:
        (out / "index.md").write_text(f"# Proposals for `{run_id}` — evidence only (--no-llm)\n\nSee `evidence.json`, `constraints.json`, `gate-report.json`.\n", encoding="utf-8")
        print(f"--no-llm: wrote evidence and constraints to {out}")
        return out

    verdict, trace = call_proposer(payload, max_n, propose_run_id)
    (out / "llm-output.json").write_text(json.dumps({"trace": trace, "verdict": verdict}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    kept, rejected = validate(list(verdict.get("proposals", [])), set(evidence_by_id), max_n)

    date = datetime.now(UTC).strftime("%Y-%m-%d")
    index_rows = []
    for n, p in enumerate(kept, start=1):
        built = build_patch(p)
        patch_name = None
        applies = None
        if built["patch"]:
            patch_name = f"proposal-{n:02d}.patch"
            (out / patch_name).write_text(built["patch"], encoding="utf-8")
            applies = patch_applies(out / patch_name)
        entry = changelog_entry(p, built, evidence_by_id, date)
        (out / f"proposal-{n:02d}.changelog.md").write_text(entry, encoding="utf-8")
        (out / f"proposal-{n:02d}.md").write_text(render_proposal(n, p, built, applies, evidence_by_id, entry, patch_name), encoding="utf-8")
        index_rows.append(
            {
                "n": n,
                "kind": p["kind"],
                "target": p["target"],
                "confidence": p["confidence"],
                "title": p["title"],
                "diagnosis": p["diagnosis"].strip().split(". ")[0][:160],
                "evidence": len(p["evidence_ids"]),
                "conflicts": len(p.get("conflicts_with") or []),
                "patch": (patch_name + (" ok" if applies and applies[0] else " BROKEN")) if patch_name else "none",
            }
        )

    lines = [f"# Proposals for `{run_id}`", ""]
    lines.append(f"Gate: **{report['status']}** ({candidate}" + (f" vs {baseline}" if baseline else "") + ").")
    lines.append(f"Proposer call: `{trace['log_id']}` in `{trace['log_db']}`, run `{trace['run_id']}`, {trace['model']} effort {trace['effort']}, "
                 f"{trace['input_tokens']} in / {trace['output_tokens']} out, {trace['latency_ms']} ms, est. ${trace['cost_usd']:.3f}.")
    lines.append(f"Evidence: {len(evidence)} items; constraints: {len(constraints['human_decisions_checkpoints'])} human decisions, {len(constraints['changelog_rules'])} changelog rules, {len(constraints['spec_non_goals'])} spec non-goals.")
    lines.append("")
    lines.append("Nothing here has been applied. To apply ONE proposal:")
    lines.append("")
    lines.append(f"    PYTHONPATH=src:. .venv/bin/python -m harness.propose --apply proposals/{out.name}/proposal-01.patch")
    lines.append("")
    lines.append("| # | kind | target | conf | evid | conflicts | patch | title — diagnosis |")
    lines.append("|---|------|--------|------|------|-----------|-------|-------------------|")
    for r in index_rows:
        lines.append(f"| {r['n']:02d} | {r['kind']} | `{r['target']}` | {r['confidence']} | {r['evidence']} | {r['conflicts']} | {r['patch']} | **{r['title']}** — {r['diagnosis']} |")
    if rejected:
        lines.append("")
        lines.append("Rejected by validation:")
        for r in rejected:
            lines.append(f"- {r}")
    if verdict.get("reasoning"):
        lines.append("")
        lines.append(f"Proposer's own summary: {verdict['reasoning']}")
    (out / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness.propose", description="Propose minimal prompt/config changes from a gated run. Proposes only; --apply is the single writing path.")
    parser.add_argument("candidate", type=Path, nargs="?", help="candidate artifact JSON")
    parser.add_argument("--db", type=Path, default=None, help="candidate run database (default: sibling .db)")
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--baseline-db", type=Path, default=None)
    parser.add_argument("--max", type=int, default=3, help="maximum proposals (default 3)")
    parser.add_argument("--out", type=Path, default=None, help="output directory (default proposals/<run_id>)")
    parser.add_argument("--no-llm", action="store_true", help="gather evidence and constraints only; make no model call")
    parser.add_argument("--apply", type=Path, default=None, metavar="PATCH", help="apply exactly one proposal patch and append its changelog entry")
    args = parser.parse_args(argv)

    if args.apply:
        return apply_patch(args.apply)
    if not args.candidate:
        parser.error("candidate artifact is required unless --apply is given")
    propose(args.candidate, args.db, args.baseline, args.baseline_db, args.max, args.out, use_llm=not args.no_llm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
