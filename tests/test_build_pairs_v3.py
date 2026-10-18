"""Stage 3 pairs come from the student's own candidates, best against worst, and never teach "define less"."""

from __future__ import annotations

import json

from training import build_pairs_v3 as v3
from training.reward import score_briefing

PACKET = {
    "proficiency": "beginner",
    "concept_ledger": {"unknown": ["offside", "var"], "familiar": ["derby"], "explained": [], "confirmed": [], "provisional": []},
    "subdomain_familiarity": {},
    "events": [{"headline": "City win the derby 2-1", "detail": "Haaland scored twice; the winner came in the 88th minute after a VAR check for offside."}],
}
LABEL = {
    "terms": [{"term": "offside", "central": True, "subdomain": "rules"}, {"term": "var", "central": True, "subdomain": "rules"}],
    "facts": [{"fact": "City beat United 2-1.", "anchors": ["City", "2-1"], "terms": []},
              {"fact": "The winner came in the 88th minute after a VAR check for offside.", "anchors": ["88th", "winner"], "terms": ["var", "offside"]}],
}
TEXT = ("City beat United 2-1 in the derby. Haaland scored twice, the second in the 88th minute. "
        "Offside, meaning an attacker was beyond the last defender when the ball was played, was checked by VAR, the video review system. "
        "United's manager said the call was wrong and the away fans were furious at full time, but the result stands. "
             "The referee took nearly four minutes over the review while both benches argued, and the home crowd whistled throughout. City now sit second in the table, two points behind the leaders with ten games left to play.")
SILENT_TEXT = "City beat United 2-1 in the derby. Haaland scored twice, the winner in the 88th minute after a VAR check for offside."


def _b(explained, used=("offside", "var", "derby"), text=TEXT):
    return json.dumps({"briefing": text, "topic": "derby", "terms_used": list(used), "explained_terms": list(explained), "subdomains": []})


FULL = _b(["offside", "var"])
HALF = _b(["offside"], text="City beat United 2-1 in the derby. Haaland scored twice, the second in the 88th minute. "
          "Offside, meaning an attacker was beyond the last defender when the ball was played, was checked by VAR. "
          "United's manager said the call was wrong and the away fans were furious at full time, but the result stands. "
             "The referee took nearly four minutes over the review while both benches argued, and the home crowd whistled throughout. City now sit second in the table, two points behind the leaders with ten games left to play.")
SILENT = _b([], text=SILENT_TEXT)                                   # strands both gap terms
MUTE = _b([], used=(), text="City beat United 2-1. Haaland scored twice.")  # names nothing, drops the gap
REGLOSS = _b(["offside", "var", "derby"])                           # defines a known term
BAD_NUMBER = _b(["offside", "var"], text=TEXT + " The fee was 300 million pounds.")


def _row(rid="p1"):
    return {"messages": [{"role": "system", "content": "s"},
                         {"role": "user", "content": json.dumps({"_judgment_point": "briefing", "context": PACKET})},
                         {"role": "assistant", "content": "unused"}],
            "meta": {"id": rid, "profile": "beginner", "group_family": "Premier League"}}


def _scores(texts):
    return v3.score_candidates(PACKET, texts, [], label=LABEL)


def test_best_against_worst_when_the_gap_is_real():
    texts = [SILENT, FULL, HALF]
    pair = v3.choose_pair(texts, _scores(texts), margin=0.1, judge=None, packet=PACKET)
    assert pair["chosen"] == FULL and pair["rejected"] == SILENT and pair["decided_by"] == "reward"


def test_a_re_glosser_is_not_rejected_in_favour_of_a_briefing_that_teaches_less():
    # Worst by reward is the re-glosser (rules fail), but it handles the gap
    # and the silent briefing does not. The silent briefing must not win.
    texts = [SILENT, REGLOSS]
    assert v3.choose_pair(texts, _scores(texts), margin=0.1, judge=None, packet=PACKET) is None


# Prevents: the round-1 pair set, where "names nothing" beat "strands everything"
# and the model learned to define nothing.
def test_two_briefings_that_both_leave_the_reader_stranded_make_no_pair():
    texts = [SILENT, MUTE]
    assert v3.choose_pair(texts, _scores(texts), margin=0.0, judge=None, packet=PACKET) is None


def test_an_invented_number_is_rejected_even_when_it_defined_more():
    texts = [HALF, BAD_NUMBER]
    pair = v3.choose_pair(texts, _scores(texts), margin=0.1, judge=None, packet=PACKET)
    assert pair["chosen"] == HALF and pair["rejected"] == BAD_NUMBER


def test_the_chosen_side_always_passes_the_rules():
    texts = [BAD_NUMBER, REGLOSS]
    assert v3.choose_pair(texts, _scores(texts), margin=0.1, judge=None, packet=PACKET) is None


def test_a_reader_with_no_gap_makes_no_pair():
    expert = dict(PACKET, proficiency="fluent", concept_ledger={"unknown": [], "familiar": ["offside", "var", "derby"], "explained": [], "confirmed": [], "provisional": []})
    texts = [SILENT, FULL]
    scores = v3.score_candidates(expert, texts, [], label=LABEL)
    assert v3.choose_pair(texts, scores, margin=0.0, judge=None, packet=expert) is None


# Prevents: round 1b, where the chosen side could strand as many gap terms as the rejected side.
def test_the_chosen_side_never_strands_more_than_the_rejected_side():
    texts = [HALF, SILENT]
    pair = v3.choose_pair(texts, _scores(texts), margin=0.0, judge=None, packet=PACKET)
    assert pair["chosen"] == HALF and pair["rejected"] == SILENT


def test_build_reports_pairs_skips_and_the_definition_lean():
    rows = [_row("p1"), _row("p2")]
    cands = [{"id": "p1", "round": 1, "candidates": [SILENT, FULL, HALF]},
             {"id": "p2", "round": 1, "candidates": [SILENT, REGLOSS]}]
    pairs, summary = v3.build(rows, cands, margin=0.1, labels={"p1": LABEL, "p2": LABEL})
    assert summary["pairs"] == 1 and summary["skipped"] == 1 and pairs[0]["id"] == "p1"
    assert summary["lean"] == {"chosen defines more": 1}
    assert summary["mean_gap_chosen"] > summary["mean_gap_rejected"]
    assert score_briefing(PACKET, json.loads(pairs[0]["chosen"]), [], label=LABEL).rules_passed
