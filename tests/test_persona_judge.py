"""The persona judge picks the right persona for a packet and keeps the packet's record in charge."""

from __future__ import annotations

from training import persona_judge as pj


def test_every_topic_maps_and_every_persona_has_a_topic():
    personas = pj.load_personas()
    assert len(personas) == 21 and all(p["_topic"] for p in personas)
    assert pj.topic_of("Startup funding, new to it") == "startups" and pj.topic_of("Premier League (Amara)") == "premier league"


def test_the_persona_matches_the_reader_level_or_the_nearest_one():
    personas = pj.load_personas()
    assert pj.pick_persona(personas, "nfl", "beginner", "x")["archetype"] == "barely_informed"
    assert pj.pick_persona(personas, "nfl", "expert", "x")["archetype"] == "well_informed"
    # Wine has a well-informed and a disengaged persona only: a beginner gets the nearest, disengaged.
    assert pj.pick_persona(personas, "wine", "beginner", "x")["archetype"] == "disengaged"
    assert pj.pick_persona(personas, None, "beginner", "x") is None
    # The choice between two equally fitting personas is stable per packet.
    a = pj.pick_persona(personas, "premier league", "partial", "row-7")
    assert a is pj.pick_persona(personas, "premier league", "partial", "row-7")


def test_the_judge_sees_the_packet_record_and_the_expanded_word_list():
    personas = pj.load_personas()
    p = pj.pick_persona(personas, "nfl", "beginner", "x")
    seen = {}

    def call(system, user, schema_, max_tokens=0):
        seen.update(user)
        return {"not_understood": ["MCL"], "needlessly_explained": [], "comment": "lost me"}

    packet = {"proficiency": "beginner", "events": [{"headline": "h"}],
              "concept_ledger": {"unknown": ["mcl"], "familiar": ["touchdown"], "explained": [], "confirmed": [], "provisional": []}}
    out = pj.judge_one(call, p, {p["id"]: {"knows": ["touchdown"], "does_not_know": ["preseason"]}}, packet, {"briefing": "He tore his MCL."})
    assert out["not_understood"] == ["MCL"]
    assert seen["record"]["known"] == ["touchdown"] and seen["record"]["unknown"] == ["mcl"]
    assert "preseason" in seen["reader"]["does_not_know"] and seen["briefing"] == "He tore his MCL."
