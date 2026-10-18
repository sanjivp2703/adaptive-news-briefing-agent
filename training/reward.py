"""The reward for a briefing, decided from the story and the reader, not from the briefing's own claims.

Stage 3's rule: Claude writes no more targets. A briefing is scored on what
can be measured about it for the reader the packet describes, and Claude may
only ever judge between two student briefings (`rank_pairs.claude_judge`) or
label the *source* story once (`label_packets.py`). The components, all
computed by code:

1. **Hard rules.** The generator's rules (`generate_sft.run_checks`: no
   question, only the packet's event, every number supported, no re-gloss of
   a known term, explained terms are used terms) plus the **definition cap**:
   at most one definition per `DEFINITION_CHARS` characters of briefing and
   never more than `MAX_DEFINITIONS`. A briefing that breaks a rule scores
   zero. A definition is a term the briefing declares in `explained_terms`
   and actually contains, or a term the prose glosses ("MCL (medial
   collateral ligament)", "offside, meaning ...", "known as a hat-trick"),
   whether declared or not.

2. **Substance**: the share of the story's key facts (from the packet label)
   whose anchors appear in the briefing. Without a label it is 1.0 and the
   score falls back to the other components.

3. **Gap handling.** The gap is the set of the story's central terms (label)
   that this reader lacks: ledger-unknown, or not in the ledger and in a
   subdomain the reader is beginner/developing in. Each gap term has one of
   three outcomes: *taught* (present and glossed, 1.0), *said plainly*
   (absent, but a fact it belongs to is present: 1.0, or 0.8 when the term
   recurs in the group's other stories and so was worth teaching), *stranded
   or dropped* (present unglossed, or absent with its fact: 0). Any other
   term the briefing uses that the reader lacks, unglossed, is stranded too.
   The gap score is the mean over those outcomes; 1.0 when there is nothing
   to handle. A reader who lacks nothing is judged on substance alone.

4. **Over-gloss**: the share of definitions spent on terms the reader holds
   (ledger-known is caught by the hard rule; this catches un-ledgered terms
   in a subdomain the reader is conversant/fluent in). learning = gap -
   0.5 * overgloss, clipped to 0..1.

5. **Fit**: 50-140 words, losing 0.01 per word outside.

Composite = 0 if any rule fails, else 0.45 * substance + 0.35 * learning +
0.2 * fit. Defining nothing fails as soon as the story has a gap. Stacking
glosses breaks the cap or crowds out the facts. The only way up is to tell
the whole story and make each gap term land, by teaching it or by plain words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from conversational_agent import config
from harness.plainness import plainness
from training.generate_sft import normalize_term, numbers_in, run_checks

BAND_WEIGHT = {"beginner": 0.1, "developing": 0.4, "conversant": 0.7, "fluent": 0.9}
LACKS_BELOW = 0.5           # hold probability under which the reader is taken to lack a term
DEFINITION_CHARS = 250      # one definition allowed per this many characters of briefing
MAX_DEFINITIONS = 2
WORDS_RANGE = (50, 140)
SUBSTANCE_WEIGHT = 0.45
LEARNING_WEIGHT = 0.35
FIT_WEIGHT = 0.2
OVERGLOSS_PENALTY = 0.5
PLAIN_WHEN_RECURRING = 0.8
UNVERIFIABLE_PLAIN = 0.5    # term absent, no fact to check it against


@dataclass
class Score:
    rules_passed: bool
    rule_failures: list[str]
    substance: float         # 0..1: key facts carried
    gap: float               # 0..1: gap terms taught or said plainly
    overgloss: float         # 0..1: share of definitions the reader did not need
    learning: float          # gap - penalty * overgloss, clipped
    fit: float               # 0..1
    composite: float         # 0 if rules failed
    definitions: int = 0
    details: dict[str, Any] = field(default_factory=dict)


# --- Reading the packet and the label ---------------------------------------------


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


def reader_lacks(term: str, packet: dict[str, Any], labels: dict[str, str]) -> bool:
    known, unknown, band, weights = reader_state(packet)
    if term in known:
        return False
    if term in unknown:
        return True
    return hold_probability(term, labels, weights, band) < LACKS_BELOW


_TERM_END = r"(?:s|es|'s)?(?![a-z0-9])"   # plurals and possessives count as the term


def term_in_text(term: str, text: str) -> bool:
    t = normalize_term(term)
    return bool(t) and re.search(rf"(?<![a-z0-9]){re.escape(t)}{_TERM_END}", normalize_term(text)) is not None


# --- Definitions ----------------------------------------------------------------------

_GLOSS_AFTER = r"\s*(?:\(|,\s*(?:meaning|which|that is|i\.e\.|or |a |an |the )|\s+(?:means|refers to|is (?:a |an |the |when )))"
_GLOSS_BEFORE = r"(?:called|known as|so called|termed|dubbed)\s+(?:a |an |the )?"


def prose_glosses(text: str, terms: list[str]) -> list[str]:
    """Terms the prose glosses, declared or not: "T (…)", "(T)" after an
    expansion, "T, meaning …", "T, which …", "T means …", "called T"."""
    body = normalize_term(text)
    out = []
    for term in terms:
        t = re.escape(normalize_term(term))
        if not t:
            continue
        if (re.search(rf"(?<![a-z0-9]){t}(?:s|es|'s)?{_GLOSS_AFTER}", body)
                or re.search(rf"\(\s*{t}(?:s|es)?\s*\)", body)
                or re.search(rf"{_GLOSS_BEFORE}{t}{_TERM_END}", body)):
            out.append(normalize_term(term))
    return out


def definitions(output: dict[str, Any] | None, candidate_terms: list[str] = ()) -> list[str]:
    """Every term the briefing defines: declared and present, or glossed in prose."""
    text = str((output or {}).get("briefing") or "")
    declared = [t for t in _terms(output, "explained_terms") if term_in_text(t, text)]
    pool = list(dict.fromkeys(_terms(output, "terms_used") + [normalize_term(t) for t in candidate_terms]))
    return list(dict.fromkeys(declared + prose_glosses(text, pool)))


def definition_cap(text: str) -> int:
    return min(MAX_DEFINITIONS, max(1, len(text) // DEFINITION_CHARS))


# --- Components -------------------------------------------------------------------


def _anchor_present(anchor: str, body: str) -> bool:
    nums = numbers_in(anchor)
    if nums:
        body_keys = {n["key"] for n in numbers_in(body)}
        return all(n["key"] in body_keys for n in nums)
    return term_in_text(anchor, body)


def substance_score(output: dict[str, Any] | None, label: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    facts = (label or {}).get("facts") or []
    if not facts:
        return 1.0, {"facts": 0, "facts_present": 0}
    text = str((output or {}).get("briefing") or "")
    present = 0
    for fact in facts:
        anchors = fact.get("anchors") or []
        hits = sum(1 for a in anchors if _anchor_present(a, text))
        if anchors and hits * 2 >= len(anchors):
            present += 1
    return round(present / len(facts), 3), {"facts": len(facts), "facts_present": present}


def gap_terms(packet: dict[str, Any], label: dict[str, Any] | None) -> list[str]:
    """Central story terms this reader lacks. Without a label: ledger-unknown
    terms that the source mentions."""
    subdomains = {t["term"]: t.get("subdomain") or "" for t in (label or {}).get("terms") or []}
    if label and label.get("terms"):
        central = [t["term"] for t in label["terms"] if t.get("central")]
    else:
        event = (packet.get("events") or [{}])[0]
        source = f"{event.get('headline') or ''} {event.get('detail') or ''}"
        _, unknown, _, _ = reader_state(packet)
        central = [t for t in sorted(unknown) if term_in_text(t, source)]
    return [t for t in central if reader_lacks(t, packet, subdomains)]


def gap_components(
    packet: dict[str, Any], output: dict[str, Any] | None, label: dict[str, Any] | None, other_events: list[str]
) -> tuple[float, float, dict[str, Any]]:
    """(gap score, overgloss fraction, details)."""
    text = str((output or {}).get("briefing") or "")
    label_terms = [t["term"] for t in (label or {}).get("terms") or []]
    subdomains = {**_labels(output), **{t["term"]: t.get("subdomain") or "" for t in (label or {}).get("terms") or []}}
    defined = set(definitions(output, label_terms))
    gap = gap_terms(packet, label)
    facts_by_term: dict[str, list[dict[str, Any]]] = {}
    for fact in (label or {}).get("facts") or []:
        for t in fact.get("terms") or []:
            facts_by_term.setdefault(normalize_term(t), []).append(fact)
    outcomes: dict[str, str] = {}
    values: list[float] = []
    for term in gap:
        if term_in_text(term, text):
            taught = term in defined
            outcomes[term] = "taught" if taught else "stranded"
            values.append(1.0 if taught else 0.0)
            continue
        facts = facts_by_term.get(term, [])
        if not facts:
            outcomes[term] = "absent"
            values.append(UNVERIFIABLE_PLAIN)
            continue
        conveyed = any(_fact_present(f, text) for f in facts)
        if not conveyed:
            outcomes[term] = "dropped"
            values.append(0.0)
            continue
        recurring = any(term_in_text(term, o) for o in other_events)
        outcomes[term] = "said plainly"
        values.append(PLAIN_WHEN_RECURRING if recurring else 1.0)
    # Terms the briefing brings in itself that the reader lacks, unglossed.
    for term in _terms(output, "terms_used"):
        if term in outcomes or term in defined or not term_in_text(term, text):
            continue
        if reader_lacks(term, packet, subdomains):
            outcomes[term] = "stranded"
            values.append(0.0)
    score = round(sum(values) / len(values), 3) if values else 1.0
    needless = [t for t in defined if not reader_lacks(t, packet, subdomains)]
    overgloss = round(len(needless) / len(defined), 3) if defined else 0.0
    return score, overgloss, {"gap": gap, "outcomes": outcomes, "defined": sorted(defined), "needless": needless}


def _fact_present(fact: dict[str, Any], text: str) -> bool:
    anchors = fact.get("anchors") or []
    hits = sum(1 for a in anchors if _anchor_present(a, text))
    return bool(anchors) and hits * 2 >= len(anchors)


def fit_score(packet: dict[str, Any], output: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    briefing = str((output or {}).get("briefing") or "")
    p = plainness(briefing)
    words = int(p["words"])
    lo_w, hi_w = WORDS_RANGE
    words_fit = 1.0 if lo_w <= words <= hi_w else max(0.0, 1.0 - 0.01 * (lo_w - words if words < lo_w else words - hi_w))
    return round(words_fit, 3), {"words": words, "grade": float(p["fk_grade"]), "band": str(packet.get("proficiency") or "beginner")}


def score_briefing(
    packet: dict[str, Any],
    output: dict[str, Any] | None,
    other_events: list[str],
    *,
    label: dict[str, Any] | None = None,
) -> Score:
    event = (packet.get("events") or [{}])[0]
    own = f"{event.get('headline') or ''} {event.get('detail') or ''}"
    others = [o for o in other_events if o != own]
    checks = run_checks(packet, output, others)
    failures = [k for k, v in checks.items() if k not in ("details", "passed") and not v]
    if output is None:
        failures = ["not valid JSON", *failures]
    text = str((output or {}).get("briefing") or "")
    defined = definitions(output, [t["term"] for t in (label or {}).get("terms") or []])
    cap = definition_cap(text)
    if output is not None and len(defined) > cap:
        failures.append("definition_cap")
    passed = output is not None and bool(checks["passed"]) and len(defined) <= cap
    substance, sdetails = substance_score(output, label)
    gap, overgloss, gdetails = gap_components(packet, output, label, others)
    learning = max(0.0, min(1.0, gap - OVERGLOSS_PENALTY * overgloss))
    fit, fdetails = fit_score(packet, output)
    composite = 0.0 if not passed else round(SUBSTANCE_WEIGHT * substance + LEARNING_WEIGHT * learning + FIT_WEIGHT * fit, 4)
    return Score(
        rules_passed=passed, rule_failures=failures, substance=substance, gap=gap, overgloss=overgloss,
        learning=round(learning, 3), fit=fit, composite=composite, definitions=len(defined),
        details={**sdetails, **gdetails, **fdetails, "definition_cap": cap,
                 "checks": {k: v for k, v in checks.items() if k != "details"}},
    )
