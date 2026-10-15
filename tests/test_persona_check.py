"""The simulated learner rewards defining what the reader lacks, and nothing else."""

from __future__ import annotations

import json

from training import persona_check as pc

PACKET = {
    "proficiency": "beginner",
    "concept_ledger": {"unknown": ["offside"], "familiar": ["var"], "explained": [], "confirmed": [], "provisional": []},
    "events": [{"headline": "City win the derby", "detail": "Haaland scored twice."}],
}
DEFINES_OFFSIDE = {"briefing": "x", "topic": "t", "terms_used": ["offside", "var"], "explained_terms": ["offside"]}
DEFINES_NOTHING = {"briefing": "x", "topic": "t", "terms_used": ["offside", "var"], "explained_terms": []}
DEFINES_KNOWN = {"briefing": "x", "topic": "t", "terms_used": ["offside", "var"], "explained_terms": ["var"]}


def _pair(chosen, rejected, decided_by="judge"):
    return {
        "id": "p1", "profile": "beginner", "decided_by": decided_by, "margin": "clear",
        "prompt": [{"role": "system", "content": "s"},
                   {"role": "user", "content": json.dumps({"_judgment_point": "briefing", "context": PACKET})}],
        "chosen": json.dumps(chosen), "rejected": json.dumps(rejected),
    }


def test_defining_a_lacking_term_teaches_the_reader_more_than_not_defining_it():
    gain_defined = pc.learning_gain(PACKET, DEFINES_OFFSIDE, draws=40)
    gain_silent = pc.learning_gain(PACKET, DEFINES_NOTHING, draws=40)
    assert gain_defined > gain_silent + 0.1
    assert gain_silent == 0.0


def test_defining_a_term_the_reader_already_has_teaches_almost_nothing_new():
    gain_known = pc.learning_gain(PACKET, DEFINES_KNOWN, draws=40)
    gain_lacking = pc.learning_gain(PACKET, DEFINES_OFFSIDE, draws=40)
    assert gain_lacking > gain_known


def test_verdicts_follow_the_learner_and_ties_are_called_ties():
    assert pc.check_pair(_pair(DEFINES_OFFSIDE, DEFINES_NOTHING), draws=40)["verdict"] == "agree"
    assert pc.check_pair(_pair(DEFINES_NOTHING, DEFINES_OFFSIDE), draws=40)["verdict"] == "disagree"
    assert pc.check_pair(_pair(DEFINES_OFFSIDE, DEFINES_OFFSIDE), draws=40)["verdict"] == "tie"


def test_the_summary_splits_by_who_decided():
    results = [
        pc.check_pair(_pair(DEFINES_OFFSIDE, DEFINES_NOTHING, "judge"), draws=20),
        pc.check_pair(_pair(DEFINES_NOTHING, DEFINES_OFFSIDE, "judge"), draws=20),
        pc.check_pair(_pair(DEFINES_OFFSIDE, DEFINES_OFFSIDE, "rules"), draws=20),
    ]
    summary = pc.summarise(results)
    assert summary["judge"] == {"pairs": 2, "agree": 1, "disagree": 1, "tie": 0, "agreement_when_decided": 0.5}
    assert summary["rules"]["tie"] == 1 and summary["rules"]["agreement_when_decided"] is None
    assert "agreement" in pc.format_summary(summary)
