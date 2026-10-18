"""The reward decides what a briefing needs from the story and the reader, never from the briefing's own claims."""

from __future__ import annotations

from training import reward as rw

EVENT = {"headline": "City win the derby 2-1", "detail": "Haaland scored twice; the winner came in the 88th minute after a VAR check for offside."}
LABEL = {
    "terms": [{"term": "offside", "central": True, "subdomain": "rules"},
              {"term": "var", "central": True, "subdomain": "rules"},
              {"term": "derby", "central": False, "subdomain": "fixtures"}],
    "facts": [{"fact": "City beat United 2-1.", "anchors": ["City", "2-1"], "terms": []},
              {"fact": "Haaland scored twice.", "anchors": ["Haaland", "twice"], "terms": []},
              {"fact": "The winner came in the 88th minute after a VAR check for offside.", "anchors": ["88th", "winner"], "terms": ["var", "offside"]}],
}
OTHERS = ["Arsenal beat Spurs after a late VAR review for offside."]


def _packet(band="beginner", known=(), unknown=("offside", "var"), familiarity=None):
    return {
        "proficiency": band,
        "concept_ledger": {"unknown": list(unknown), "familiar": list(known), "explained": [], "confirmed": [], "provisional": []},
        "subdomain_familiarity": familiarity or {},
        "events": [EVENT],
    }


FULL_TEXT = ("City beat United 2-1. Haaland scored twice, the winner in the 88th minute. "
             "Offside, meaning an attacker was beyond the last defender when the ball was played, was checked by VAR, the video referee. "
             "United's manager said the call was wrong and the away fans were furious at full time, but the result stands. "
             "The referee took nearly four minutes over the review while both benches argued, and the home crowd whistled throughout. City now sit second in the table, two points behind the leaders with ten games left to play.")
PLAIN_TEXT = ("City beat United 2-1. Haaland scored twice, and his winner in the 88th minute stood after the video officials "
              "checked that no attacker had strayed too far forward.")
STRANDED_TEXT = "City beat United 2-1. Haaland scored twice, the winner in the 88th minute after a VAR check for offside."
THIN_TEXT = "City beat United. Haaland scored twice."


def _briefing(text, explained=(), used=("offside", "var"), labels=None):
    return {"briefing": text, "topic": "derby", "terms_used": list(used), "explained_terms": list(explained),
            "subdomains": [{"term": t, "subdomain": labels.get(t, "rules")} for t in used] if labels else []}


def test_a_rule_breaker_scores_zero_overall():
    bad = _briefing(FULL_TEXT + " The fee was 300 million pounds.", ["offside", "var"])
    s = rw.score_briefing(_packet(), bad, OTHERS, label=LABEL)
    assert s.rules_passed is False and s.composite == 0.0 and "numbers_supported" in s.rule_failures


def test_the_gap_comes_from_the_story_and_the_reader_not_the_briefing():
    assert rw.gap_terms(_packet(), LABEL) == ["offside", "var"]
    fam = {"rules": {"band": "fluent"}}
    assert rw.gap_terms(_packet(band="fluent", unknown=(), familiarity=fam), LABEL) == []
    # A term the ledger marks known is never in the gap.
    assert rw.gap_terms(_packet(known=("var",), unknown=("offside",)), LABEL) == ["offside"]


def test_teaching_and_saying_plainly_both_handle_the_gap_but_stranding_and_dropping_do_not():
    packet = _packet()
    taught = rw.score_briefing(packet, _briefing(FULL_TEXT, ["offside", "var"]), OTHERS, label=LABEL)
    plain = rw.score_briefing(packet, _briefing(PLAIN_TEXT, used=()), OTHERS, label=LABEL)
    stranded = rw.score_briefing(packet, _briefing(STRANDED_TEXT), OTHERS, label=LABEL)
    dropped = rw.score_briefing(packet, _briefing(THIN_TEXT, used=()), OTHERS, label=LABEL)
    assert taught.gap == 1.0
    assert taught.details["outcomes"] == {"offside": "taught", "var": "taught"}
    assert plain.details["outcomes"] == {"offside": "said plainly", "var": "said plainly"}
    assert plain.gap == rw.PLAIN_WHEN_RECURRING  # both terms recur in the group's other stories
    assert stranded.gap == 0.0 and stranded.details["outcomes"] == {"offside": "stranded", "var": "stranded"}
    assert dropped.gap == 0.0 and dropped.details["outcomes"] == {"offside": "dropped", "var": "dropped"}
    assert taught.composite > plain.composite > stranded.composite >= dropped.composite


# Prevents: the round-1 failure, where defining nothing scored level with Claude.
def test_defining_nothing_loses_whenever_the_story_has_a_gap():
    packet = _packet()
    taught = rw.score_briefing(packet, _briefing(FULL_TEXT, ["offside", "var"]), OTHERS, label=LABEL)
    silent = rw.score_briefing(packet, _briefing(STRANDED_TEXT, used=()), OTHERS, label=LABEL)
    assert silent.definitions == 0 and silent.gap == 0.0
    assert taught.composite - silent.composite > 0.3


def test_a_reader_who_lacks_nothing_is_judged_on_substance_alone():
    fam = {"rules": {"band": "fluent"}, "fixtures": {"band": "fluent"}}
    expert = _packet(band="fluent", unknown=(), familiarity=fam)
    quiet = rw.score_briefing(expert, _briefing(STRANDED_TEXT), OTHERS, label=LABEL)
    glossy = rw.score_briefing(expert, _briefing(FULL_TEXT, ["offside", "var"]), OTHERS, label=LABEL)
    assert quiet.gap == 1.0 and quiet.overgloss == 0.0 and quiet.substance == 1.0
    assert glossy.overgloss == 1.0 and glossy.learning < quiet.learning
    assert quiet.composite > glossy.composite


def test_prose_glosses_count_as_definitions_whether_declared_or_not():
    text = "Bisontis tore his MCL (medial collateral ligament) in the preseason, the warm-up games before the season starts."
    assert rw.prose_glosses(text, ["mcl", "preseason", "draft"]) == ["mcl", "preseason"]
    assert rw.prose_glosses("A hat-trick, meaning three goals by one player, sealed it.", ["hat trick"]) == ["hat trick"]
    assert rw.prose_glosses("The medial collateral ligament (MCL) tore.", ["mcl"]) == ["mcl"]
    assert rw.prose_glosses("It is known as the derby.", ["derby"]) == ["derby"]
    assert rw.prose_glosses("VAR is reviewing it now.", ["var"]) == []
    out = {"briefing": text, "terms_used": ["mcl"], "explained_terms": []}
    assert rw.definitions(out, ["preseason"]) == ["mcl", "preseason"]
    # A declared term that the text never contains is not a definition.
    assert rw.definitions({"briefing": "City won.", "terms_used": ["var"], "explained_terms": ["var"]}) == []


def test_the_definition_cap_is_a_hard_rule_tied_to_length():
    assert rw.definition_cap("x" * 100) == 1
    assert rw.definition_cap("x" * 520) == 2
    assert rw.definition_cap("x" * 2000) == rw.MAX_DEFINITIONS
    stacked = ("City beat United 2-1 in the derby (the match between the two Manchester clubs). Haaland scored twice, "
               "the winner in the 88th minute. Offside (an attacker beyond the last defender) was checked by VAR (the video referee).")
    s = rw.score_briefing(_packet(unknown=("offside", "var", "derby")), _briefing(stacked, ["offside", "var", "derby"], used=("offside", "var", "derby")), OTHERS, label=LABEL)
    assert s.definitions == 3 and "definition_cap" in s.rule_failures and s.composite == 0.0


def test_substance_counts_the_key_facts_carried():
    full, _ = rw.substance_score({"briefing": FULL_TEXT}, LABEL)
    thin, _ = rw.substance_score({"briefing": THIN_TEXT}, LABEL)
    none, _ = rw.substance_score({"briefing": THIN_TEXT}, None)
    assert full == 1.0 and thin < full and none == 1.0


def test_without_a_label_the_gap_falls_back_to_the_ledger():
    s = rw.score_briefing(_packet(), _briefing(FULL_TEXT, ["offside", "var"]), OTHERS)
    assert s.details["gap"] == ["offside", "var"] and s.gap == 1.0 and s.substance == 1.0
