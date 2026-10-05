"""Query formulation and source selection against the real web.

    PYTHONPATH=src:. .venv/bin/python -m harness.live_search_eval

This is the coverage the persona suite structurally cannot give. Persona
Monitor input is a scripted feed by design -- that is what makes persona runs
cheap and reproducible -- and the direct consequence is that search is
never exercised there at all. The persona harness registers stub handlers for
both halves purely so a stray call is *reported* rather than silently billing a
real search. Actually measuring them has to happen here, against the real tool.

Two things this file is built around:

**At least one group well outside the persona domains.** The spec's Risks
section asks for this explicitly, and it is the only check that separates "the
query prompt works" from "the query prompt has been tuned until it works on
sports and AI". A judgment that formulates good queries for the NBA and
flounders on biodynamic viticulture has not generalised -- it has memorised.
Three of the five cases are deliberately unfamiliar.

**An empty list is a correct answer, and the most important one to verify.**
The failure mode that actually hurts a user is not a thin result set; it is a
confident, plausible, fabricated one. Case 5 names a group that does not exist
anywhere on the web. The only passing behaviour is returning nothing. A run
that invents two credible-looking sources for the Thursday night intramural
curling ladder at a polytechnic that isn't real has failed in the exact way the
no-hallucination rule exists to prevent, no matter how good its other numbers.

Needs credentials. Without them it says so and exits 0 -- a skipped live suite
is not a failed one, and making CI red for a missing key trains people to
ignore it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conversational_agent import app, config, search
from conversational_agent.judgment import AnthropicClient, Judge
from conversational_agent.store import Store

# Domains that are never an acceptable primary source for a factual claim.
# Deliberately short and uncontroversial: user-generated Q&A, pin boards and
# free blog hosts. The point is to catch obvious junk, not to adjudicate
# publication quality -- that judgement stays with the human reading the report.
JUNK_DOMAIN_MARKERS = (
    "pinterest.",
    "quora.com",
    "answers.com",
    "blogspot.",
    "wordpress.com",
    "medium.com/@",
    "facebook.com",
    "reddit.com/r/",
    "tiktok.com",
    "ehow.com",
)


@dataclass
class SearchCase:
    id: str
    group_name: str
    description: str
    familiar: bool
    # Distinctive tokens at least one query should contain, lowercased.
    expect_query_terms: list[str]
    expect_empty: bool = False
    rationale: str = ""


def build_cases() -> list[SearchCase]:
    return [
        SearchCase(
            id="nba_familiar",
            group_name="NBA Western Conference",
            description=(
                "Western Conference standings, rotations, trades and injuries."
            ),
            familiar=True,
            expect_query_terms=["nba", "western conference"],
            rationale="Baseline: a domain the prompts were written with in mind.",
        ),
        SearchCase(
            id="frontier_ai_familiar",
            group_name="Frontier AI model releases",
            description=(
                "New frontier model launches, capability evaluations, and the "
                "regulatory response to them."
            ),
            familiar=True,
            expect_query_terms=["model", "ai"],
            rationale="Baseline: the other well-trodden domain.",
        ),
        SearchCase(
            id="natural_wine_unfamiliar",
            group_name="Natural wine importers",
            description=(
                "Low-intervention and biodynamic wine: importer portfolios, "
                "vintage reports, and the certification arguments around them."
            ),
            familiar=False,
            expect_query_terms=["wine"],
            rationale=(
                "Outside the persona domains. Tests whether query formulation "
                "generalises or has been tuned to sport and technology."
            ),
        ),
        SearchCase(
            id="bread_baking_unfamiliar",
            group_name="Competitive bread baking",
            description=(
                "Competitive baking: the Coupe du Monde de la Boulangerie, "
                "national qualifiers, and championship sourdough technique."
            ),
            familiar=False,
            expect_query_terms=["baking", "bread"],
            rationale=(
                "Outside the persona domains, and a niche where the obvious "
                "search results are recipe content farms rather than reporting "
                "-- so source selection has real work to do."
            ),
        ),
        SearchCase(
            id="nonexistent_anti_fabrication",
            group_name=(
                "Thursday night intramural curling ladder at Kestrelmoor "
                "Polytechnic"
            ),
            description=(
                "Results, standings and rink assignments for the Thursday "
                "night intramural curling ladder at Kestrelmoor Polytechnic."
            ),
            familiar=False,
            expect_query_terms=["curling"],
            expect_empty=True,
            rationale=(
                "Kestrelmoor Polytechnic does not exist. The only correct "
                "output is an empty event list. Anything else is fabrication."
            ),
        ),
    ]


# --- Scoring ---------------------------------------------------------------


@dataclass
class CaseScore:
    case_id: str
    familiar: bool
    queries: list[str] = field(default_factory=list)
    domain_confidence: str = ""
    event_count: int = 0
    excluded_count: int = 0
    note: str = ""
    sources: list[str] = field(default_factory=list)
    query_findings: list[str] = field(default_factory=list)
    source_findings: list[str] = field(default_factory=list)
    fabrication: str | None = None  # 'clean' | 'fabricated' | 'inconclusive'
    error: str | None = None

    @property
    def failed(self) -> bool:
        return bool(
            self.error
            or self.query_findings
            or self.source_findings
            or self.fabrication == "fabricated"
        )


def _domain(url: str) -> str:
    if "://" not in url:
        return url
    return url.split("://", 1)[1].split("/", 1)[0].lower()


def score_queries(case: SearchCase, queries: list[str]) -> list[str]:
    """Well-formed and domain-appropriate?"""
    findings: list[str] = []
    if not queries:
        findings.append("no queries were produced at all")
        return findings
    joined = " ".join(queries).lower()
    for term in case.expect_query_terms:
        if term.lower() not in joined:
            findings.append(
                f"no query mentions {term!r}; the queries may have drifted off "
                f"the group's actual subject (got {queries})"
            )
    for query in queries:
        if len(query.split()) < 2:
            findings.append(f"query {query!r} is a bare token, not a search")
    if len(set(q.strip().lower() for q in queries)) != len(queries):
        findings.append(f"duplicate queries issued: {queries}")
    return findings


def score_sources(events: list[dict[str, Any]]) -> list[str]:
    """Did selection exclude junk and stay inside what search returned?"""
    findings: list[str] = []
    seen: set[str] = set()
    for event in events:
        url = str(event.get("source_url", "") or "")
        if not url.startswith(("http://", "https://")):
            findings.append(f"source_url is not a real URL: {url!r}")
            continue
        domain = _domain(url)
        if url in seen:
            findings.append(f"the same source was selected twice: {url}")
        seen.add(url)
        for marker in JUNK_DOMAIN_MARKERS:
            if marker in url.lower():
                findings.append(f"junk source selected ({marker}): {url}")
        if not str(event.get("headline", "")).strip():
            findings.append(f"event from {domain} has an empty headline")
        confidence = event.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 100:
            findings.append(f"confidence {confidence!r} out of range for {domain}")
    return findings


def run_case(judge: Judge, client: Any, scope, case: SearchCase) -> CaseScore:
    group = scope.create_group(
        name=case.group_name,
        description=case.description,
        
        
    )
    score = CaseScore(case_id=case.id, familiar=case.familiar)
    try:
        found = search.discover_events(judge, client, group, recent_events=[])
    except Exception as exc:
        score.error = f"{type(exc).__name__}: {exc}"
        return score

    events = found.get("events", []) or []
    score.queries = list(found.get("queries", []) or [])
    score.domain_confidence = str(found.get("domain_confidence", ""))
    score.event_count = len(events)
    score.excluded_count = int(found.get("excluded_count", 0) or 0)
    score.note = str(found.get("note", ""))
    score.sources = sorted({_domain(str(e.get("source_url", ""))) for e in events})
    score.query_findings = score_queries(case, score.queries)
    score.source_findings = score_sources(events)

    if case.expect_empty:
        call_failed = "failed" in score.note.lower() or "unavailable" in score.note.lower()
        if call_failed:
            # The search never actually ran, so this proves nothing either way.
            # Reported as inconclusive rather than quietly counted as a pass.
            score.fabrication = "inconclusive"
        elif events:
            score.fabrication = "fabricated"
        else:
            score.fabrication = "clean"
    return score


# --- Entry point -----------------------------------------------------------


def has_credentials() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )


def render(scores: list[CaseScore]) -> str:
    lines = ["=" * 74, "Live search eval -- query formulation + source selection", "=" * 74]
    for score in scores:
        status = "FAIL" if score.failed else "ok"
        if score.error:
            status = "ERROR"
        lines.append(f"\n[{status:>5}] {score.case_id}" + ("" if score.familiar else "   (OUT OF DOMAIN)"))
        if score.error:
            lines.append(f"         error: {score.error}")
            continue
        lines.append(f"         queries ({score.domain_confidence} confidence):")
        for query in score.queries:
            lines.append(f"           - {query}")
        lines.append(
            f"         selected {score.event_count} event(s), "
            f"excluded {score.excluded_count}"
        )
        if score.sources:
            lines.append(f"         sources: {', '.join(score.sources)}")
        if score.note:
            lines.append(f"         note: {score.note}")
        if score.fabrication:
            label = {
                "clean": "returned EMPTY rather than fabricating -- correct",
                "fabricated": "FABRICATED sources for a group that does not exist",
                "inconclusive": "inconclusive: the search call itself did not run",
            }[score.fabrication]
            lines.append(f"         anti-fabrication: {label}")
        for finding in score.query_findings:
            lines.append(f"         ! query: {finding}")
        for finding in score.source_findings:
            lines.append(f"         ! source: {finding}")

    familiar = [s for s in scores if s.familiar and not s.error]
    unfamiliar = [s for s in scores if not s.familiar and not s.error]
    lines.append("\n" + "-" * 74)
    lines.append("Generalisation (the reason the unfamiliar groups are here)")
    for label, bucket in (("familiar", familiar), ("out of domain", unfamiliar)):
        if not bucket:
            continue
        clean = sum(1 for s in bucket if not s.query_findings)
        lines.append(
            f"  {label:<14} {clean}/{len(bucket)} produced well-formed, "
            f"on-subject queries"
        )
    failures = [s.case_id for s in scores if s.failed]
    lines.append("-" * 74)
    lines.append(f"RESULT: {'FAIL -- ' + ', '.join(failures) if failures else 'PASS'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live search eval (query formulation + source selection).")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--case", default=None, help="run a single case by id")
    args = parser.parse_args(argv)

    if not has_credentials():
        print(
            "Live search eval SKIPPED: no Anthropic credentials found.\n"
            "This suite makes real web-search calls and cannot be stubbed -- "
            "stubbed search would be measuring the stub.\n"
            "Set ANTHROPIC_API_KEY and re-run to exercise the search judgments."
        )
        return 0

    cases = build_cases()
    if args.case:
        cases = [c for c in cases if c.id == args.case]
        if not cases:
            print(f"No such case: {args.case}")
            return 1

    tmp = None
    db_path = args.db
    if db_path is None:
        tmp = tempfile.TemporaryDirectory(prefix="live-search-eval-")
        db_path = Path(tmp.name) / "live_search.db"

    store = Store(db_path)
    run_id = store.start_eval_run(
        "live_search",
        json.dumps(
            {
                "cases": [c.id for c in cases],
                "models": {
                    p: config.model_for(p)
                    for p in (config.QUERY_FORMULATION, config.SOURCE_SELECTION)
                },
                "search_execution_model": config.SEARCH_EXECUTION_MODEL,
                "max_search_uses": config.MAX_SEARCH_USES,
            },
            indent=2,
            sort_keys=True,
        ),
    )

    raw = app._anthropic_raw_client()
    judge = Judge(client=AnthropicClient(raw), store=store, run_id=run_id)
    user = store.create_user(
        f"live-search-eval {run_id}", kind="persona", profile={"suite": "live_search"}
    )
    scope = store.scope(user.id)

    scores = [run_case(judge, raw, scope, case) for case in cases]
    report = render(scores)
    print(report)

    payload = {
        "suite": "live_search",
        "run_id": run_id,
        "cases": [
            {
                "case_id": s.case_id,
                "familiar": s.familiar,
                "queries": s.queries,
                "domain_confidence": s.domain_confidence,
                "event_count": s.event_count,
                "excluded_count": s.excluded_count,
                "sources": s.sources,
                "note": s.note,
                "query_findings": s.query_findings,
                "source_findings": s.source_findings,
                "fabrication": s.fabrication,
                "error": s.error,
                "failed": s.failed,
            }
            for s in scores
        ],
    }
    passed = not any(s.failed for s in scores)
    store.finish_eval_run(run_id, json.dumps(payload, sort_keys=True), passed)
    store.close()
    if tmp is not None:
        tmp.cleanup()

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nArtifact: {args.json}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
