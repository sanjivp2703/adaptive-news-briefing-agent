"""A guided rewrite is kept only when it fixed the flagged words, broke no rule and kept the story."""

from __future__ import annotations

import json

from training import build_pairs_guided as g

PACKET = {"proficiency": "beginner", "subdomain_familiarity": {},
          "concept_ledger": {"unknown": ["mcl"], "familiar": [], "explained": [], "confirmed": [], "provisional": []},
          "events": [{"headline": "Bisontis out for the season", "detail": "Cardinals rookie Chase Bisontis tore his MCL in the preseason opener on Aug. 14."}]}
LABEL = {"terms": [{"term": "mcl", "central": True, "subdomain": "injuries"}],
         "facts": [{"fact": "Bisontis is out for the season.", "anchors": ["Bisontis", "season"], "terms": ["mcl"]}]}
TAIL = " The Cardinals had counted on him to start at guard, and the team now has to find a replacement before the regular season opens next month."


def _b(text, used=(), explained=()):
    return json.dumps({"briefing": text + TAIL, "topic": "injury", "terms_used": list(used), "explained_terms": list(explained), "subdomains": []})


DRAFT = _b("Cardinals rookie Chase Bisontis tore his MCL in the preseason opener on Aug. 14 and will miss the season.", ["mcl"])
PLAIN = _b("Cardinals rookie Chase Bisontis tore a ligament in his knee in a warm-up game on Aug. 14 and will miss the season.")
STILL_LOST = _b("Cardinals rookie Chase Bisontis badly tore his MCL in the opener on Aug. 14 and will miss the season.", ["mcl"])
MADE_UP = _b("Cardinals rookie Chase Bisontis tore a ligament in his knee on Aug. 14 and will miss 12 games this season.")


def test_only_a_rewrite_that_fixes_the_flagged_word_and_keeps_the_rules_wins():
    rec = {"id": "p1", "draft": DRAFT, "revisions": [STILL_LOST, MADE_UP, PLAIN], "not_understood": ["MCL"]}
    best, draft_score, why = g.choose(rec, PACKET, [], LABEL)
    assert best[0] == PLAIN
    assert why["left a flagged word unexplained"] == 1 and why["broke a rule"] == 1


def test_no_pair_when_nothing_qualifies():
    rec = {"id": "p1", "draft": DRAFT, "revisions": [STILL_LOST, DRAFT], "not_understood": ["MCL"]}
    assert g.choose(rec, PACKET, [], LABEL)[0] is None
