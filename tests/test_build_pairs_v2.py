"""The second pair set anchors rule breakers and under-definers to the teacher."""

from __future__ import annotations

import json

from training import build_pairs_v2 as v2

PACKET = {
    "proficiency": "beginner",
    "concept_ledger": {"unknown": ["offside", "var"], "familiar": [], "explained": [], "confirmed": [], "provisional": []},
    "events": [{"headline": "City win the derby 2-1", "detail": "Haaland scored twice; the winner came in the 88th minute."}],
}
TEACHER = {
    "briefing": "City beat United 2-1. Haaland scored twice, the second in the 88th minute. "
    "Offside, meaning an attacker was beyond the last defender when the ball was played, was checked by VAR, the video review system.",
    "topic": "derby", "explained_terms": ["offside", "var"], "terms_used": ["offside", "var"], "reasoning": "x",
}
SILENT = dict(TEACHER, briefing="City beat United 2-1. Haaland scored twice, the second in the 88th minute.", explained_terms=[], terms_used=["offside", "var"])
BAD_NUMBER = dict(TEACHER, briefing=TEACHER["briefing"] + " The fee was 300 million pounds.")


def _row(rid: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": json.dumps({"_judgment_point": "briefing", "context": PACKET})},
            {"role": "assistant", "content": json.dumps(TEACHER)},
        ],
        "meta": {"id": rid, "profile": "beginner", "group_family": "Premier League"},
    }


def test_rule_breakers_and_under_definers_are_paired_against_the_teacher():
    rows = [_row("p1")]
    candidates = [{"id": "p1", "candidates": [json.dumps(BAD_NUMBER), json.dumps(SILENT)]}]
    pairs, summary = v2.build([], candidates, rows, draws=20)
    sources = sorted(p["source"] for p in pairs)
    assert sources == ["learner", "rules"]
    assert all(json.loads(p["chosen"]) == TEACHER for p in pairs)
    assert summary["mean_definitions_chosen"] > summary["mean_definitions_rejected"]


def test_judge_pairs_are_kept_and_rules_pairs_from_the_first_set_are_not():
    rows = [_row("p1")]
    v1 = [
        {"id": "p1", "decided_by": "judge", "prompt": rows[0]["messages"][:2], "chosen": json.dumps(TEACHER), "rejected": json.dumps(SILENT)},
        {"id": "p1", "decided_by": "rules", "prompt": rows[0]["messages"][:2], "chosen": json.dumps(SILENT), "rejected": json.dumps(BAD_NUMBER)},
    ]
    pairs, summary = v2.build(v1, [], rows, draws=5)
    assert [p["source"] for p in pairs] == ["judge"] and summary["from_judge"] == 1


def test_a_candidate_as_good_as_the_teacher_makes_no_learner_pair():
    rows = [_row("p1")]
    candidates = [{"id": "p1", "candidates": [json.dumps(TEACHER), json.dumps(TEACHER)]}]
    pairs, summary = v2.build([], candidates, rows, draws=10)
    assert pairs == [] and summary["pairs"] == 0
