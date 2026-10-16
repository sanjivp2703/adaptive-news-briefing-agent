"""Stage 3 pairs come from the student's own candidates, best against worst, and never teach "define less"."""

from __future__ import annotations

import json

from training import build_pairs_v3 as v3
from training.reward import score_briefing

PACKET = {
    "proficiency": "beginner",
    "concept_ledger": {"unknown": ["offside", "var"], "familiar": ["derby"], "explained": [], "confirmed": [], "provisional": []},
    "subdomain_familiarity": {},
    "events": [{"headline": "City win the derby 2-1", "detail": "Haaland scored twice; the winner came in the 88th minute."}],
}
TEXT = ("City beat United 2-1 in the derby. Haaland scored twice, the second in the 88th minute. "
        "Offside, meaning an attacker was beyond the last defender when the ball was played, was checked by VAR, the video review system.")


def _b(explained, used=("offside", "var", "derby"), text=TEXT):
    return json.dumps({"briefing": text, "topic": "derby", "terms_used": list(used), "explained_terms": list(explained), "subdomains": []})


FULL = _b(["offside", "var"])
HALF = _b(["offside"])
NONE = _b([])
REGLOSS = _b(["offside", "var", "derby"])          # defines a known term
BAD_NUMBER = _b(["offside", "var"], text=TEXT + " The fee was 300 million pounds.")


def _row(rid="p1"):
    return {"messages": [{"role": "system", "content": "s"},
                         {"role": "user", "content": json.dumps({"_judgment_point": "briefing", "context": PACKET})},
                         {"role": "assistant", "content": "unused"}],
            "meta": {"id": rid, "profile": "beginner", "group_family": "Premier League"}}


def _scores(texts):
    return v3.score_candidates(PACKET, texts, [], draws=12)


def test_best_against_worst_when_the_gap_is_real():
    texts = [NONE, FULL, HALF]
    pair = v3.choose_pair(texts, _scores(texts), margin=0.1, judge=None, packet=PACKET)
    assert pair["chosen"] == FULL and pair["rejected"] == NONE and pair["decided_by"] == "reward"


def test_a_re_glosser_is_not_rejected_in_favour_of_a_briefing_that_teaches_less():
    # Worst by reward is the re-glosser (rules fail), but it covers more than
    # the silent briefing. The silent briefing must not be the winner.
    texts = [NONE, REGLOSS]
    assert v3.choose_pair(texts, _scores(texts), margin=0.1, judge=None, packet=PACKET) is None


def test_an_invented_number_is_rejected_even_when_it_defined_more():
    texts = [HALF, BAD_NUMBER]
    pair = v3.choose_pair(texts, _scores(texts), margin=0.1, judge=None, packet=PACKET)
    assert pair["chosen"] == HALF and pair["rejected"] == BAD_NUMBER


def test_the_chosen_side_always_passes_the_rules():
    texts = [BAD_NUMBER, REGLOSS]
    assert v3.choose_pair(texts, _scores(texts), margin=0.1, judge=None, packet=PACKET) is None


def test_build_reports_pairs_and_skips():
    rows = [_row("p1"), _row("p2")]
    cands = [{"id": "p1", "round": 1, "candidates": [NONE, FULL, HALF, BAD_NUMBER]},
             {"id": "p2", "round": 1, "candidates": [NONE, REGLOSS]}]
    pairs, summary = v3.build(rows, cands, margin=0.1, draws=12)
    assert summary["pairs"] == 1 and summary["skipped"] == 1 and pairs[0]["id"] == "p1"
    assert score_briefing(PACKET, json.loads(pairs[0]["chosen"]), [], draws=4).rules_passed
