"""The distillation data generator (training/generate_sft.py), fully offline.

No network, no credentials: the teacher is the project's StubClient. The one
place a real `Store` is used is to check that the generator's mirrored ledger
arithmetic agrees with the store's own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conversational_agent import config, judgments
from conversational_agent.judgment import Judge, StubClient
from conversational_agent.store import Store
from training import generate_sft as gen

EXTRACTOR_KEYS = {
    "id", "source_db", "run_id", "user_id", "group_id", "prompt_version", "model",
    "created_at", "system_text_available", "system", "input", "output", "quality",
}
EXTRACTOR_QUALITY_KEYS = {
    "gate_run_passed", "errored", "error", "question_mark_in_briefing", "words",
    "definitions", "defs_per_100w", "reask", "input_tokens", "output_tokens", "latency_ms",
}


@pytest.fixture(scope="module")
def fixtures():
    return gen.load_fixtures()


@pytest.fixture(scope="module")
def items(fixtures):
    return gen.build_packets(fixtures, seed=7, states_per_event=12, limit=500)


# --- Packets ------------------------------------------------------------------


def test_packet_keys_match_write_briefing_context(items):
    """The teacher must see exactly what production sees: the packet's keys
    are the keys `write_briefing` builds, no more and no fewer."""
    stub = StubClient()
    seen: dict = {}

    def capture(ctx):
        seen.update(ctx)
        return {"briefing": "x.", "topic": "t", "terms_used": [], "explained_terms": [],
                "subdomains": [], "reasoning": "r"}

    stub.register(config.BRIEFING, capture)
    judgments.write_briefing(
        Judge(client=stub), group={}, ledger={}, proficiency="beginner", events=[],
        previously_raised=[], active_goal=None, behind_count=0, user_id="u", group_id="g",
        reading_pattern={}, subdomain_familiarity={},
    )
    for it in items[:25]:
        assert set(it["packet"]) == set(seen), it["id"]
        event = it["packet"]["events"][0]
        assert set(event) == {"id", "headline", "detail", "occurred_at", "source_name", "source_url"}
        assert set(it["packet"]["concept_ledger"]) == set(config.CONCEPT_STATES)


def test_familiarity_predicates_agree_with_store(store):
    """Build a ledger through the real Store's mark_* methods, then compare
    the generator's dict arithmetic with the store's readouts."""
    user = store.create_user("Test", kind="persona")
    scope = store.scope(user.id)
    gid = scope.create_group("Wine", "wine").id
    labels = {
        "budburst": "viticulture & harvest", "yields": "viticulture & harvest",
        "veraison": "viticulture & harvest", "picking": "viticulture & harvest",
        "cru": "appellation rules", "ebits": "commercial & market",
    }
    scope.note_exposure(gid, ["budburst", "yields", "veraison", "picking", "cru", "ebits", "glut"], subdomains=labels)
    for _ in range(2):
        scope.mark_correct_use(gid, ["budburst", "yields"], subdomains=labels)  # confirmed
    scope.mark_correct_use(gid, ["veraison"], subdomains=labels)  # provisional
    for _ in range(2):
        scope.mark_read_explanation(gid, ["picking"], subdomains=labels)  # familiar, read twice
    scope.mark_read_explanation(gid, ["cru"], subdomains=labels)  # familiar, read once
    scope.mark_explained(gid, ["ebits"], subdomains=labels)  # explained, read once
    scope.mark_unknown(gid, ["glut"])  # misunderstood, unlabelled

    rows = [
        {"term": c.term, "state": c.state, "correct_uses": c.correct_uses,
         "read_explanations": c.read_explanations, "misunderstandings": c.misunderstandings,
         "explained_at": c.explained_at, "subdomain": c.subdomain}
        for c in scope.ledger(gid)
    ]
    assert gen.familiarity_from_rows(rows) == scope.subdomain_familiarity(gid)
    assert gen.proficiency_from_rows(rows) == scope.proficiency(gid)
    # The interesting slice really is decided by the shared predicates.
    # 3 known of 4 attested: ratio 0.75 clears the conversant ratio floor but
    # the count floor is 4 known, so it is `developing` -- in both.
    assert scope.subdomain_familiarity(gid)["viticulture & harvest"] == {"known": 3, "attested": 4, "band": config.DEVELOPING}
    assert gen.familiarity_from_rows(rows)[config.UNLABELLED_SUBDOMAIN] == {"known": 0, "attested": 1, "band": config.BEGINNER}


def test_synthetic_rows_are_store_consistent(items):
    """Every packet's proficiency and familiarity are recomputable from its
    own ledger buckets: a term in a known bucket never gets glossed, and no
    band claims more known terms than the buckets can carry."""
    for it in items:
        p = it["packet"]
        known_terms = sum(len(p["concept_ledger"][s]) for s in config.BAND_STATES)
        assert it["meta"]["known"] <= known_terms
        assert p["proficiency"] == config.proficiency_band(it["meta"]["known"], it["meta"]["attested"])
        for fam in p["subdomain_familiarity"].values():
            assert fam["band"] == config.subdomain_band(fam["known"], fam["attested"])
            assert fam["known"] <= fam["attested"]


def test_deterministic_by_seed(fixtures):
    a = gen.build_packets(fixtures, seed=11, states_per_event=3, limit=60)
    b = gen.build_packets(fixtures, seed=11, states_per_event=3, limit=60)
    c = gen.build_packets(fixtures, seed=12, states_per_event=3, limit=60)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert [x["id"] for x in a] != [x["id"] for x in c]
    assert json.dumps([x["packet"] for x in a], sort_keys=True) != json.dumps([x["packet"] for x in c], sort_keys=True)


def test_coverage_spans_every_profile_and_group(fixtures, items):
    cov = gen.coverage(items)
    assert set(cov["per_profile"]) == set(gen.PROFILES)
    # Every group name any persona gives an event appears in some packet
    # (an event shared by two personas rotates through both their groups).
    assert set(cov["per_group"]) == {v.group_name for ev in fixtures for v in ev.variants}
    assert {ev.family for ev in fixtures} <= set(cov["per_group"])
    assert cov["events"] == len(fixtures)
    assert cov["with_deep_subdomain"] > 0
    assert {config.BEGINNER, config.DEVELOPING, config.CONVERSANT} <= set(cov["per_band"])
    assert len({ev.headline for ev in fixtures}) == len(fixtures)  # deduped by headline


# --- Offline end to end -----------------------------------------------------------


def _rows(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_offline_end_to_end_rows_match_extractor_shape(tmp_path):
    out = tmp_path / "gen.jsonl"
    rc = gen.main(["--offline", "--seed", "3", "--limit", "6", "--out", str(out), "--db", str(tmp_path / "g.db")])
    assert rc == 0
    rows = _rows(out)
    assert len(rows) == 6
    for r in rows:
        assert set(r) == EXTRACTOR_KEYS | {"meta"}
        assert EXTRACTOR_QUALITY_KEYS | {"checks", "plainness"} == set(r["quality"])
        assert r["quality"]["gate_run_passed"] is None and r["quality"]["reask"] is None
        from conversational_agent.judgment import load_prompt
        assert r["prompt_version"] == load_prompt("briefing").version
        assert r["system_text_available"] is True
        assert r["input"]["_judgment_point"] == config.BRIEFING
        assert set(r["output"]) == {"briefing", "topic", "explained_terms", "terms_used", "subdomains"}
        assert r["quality"]["checks"]["passed"] is True
        assert r["quality"]["words"] > 0 and r["quality"]["input_tokens"] > 0
    clean = _rows(tmp_path / "gen.clean.jsonl")
    assert [r["id"] for r in clean] == [r["id"] for r in rows]
    # Every call went through the real Judge and landed in the log.
    with Store(tmp_path / "g.db") as store:
        n = store.conn.execute("SELECT COUNT(*) FROM judgment_log WHERE judgment_point = 'briefing'").fetchone()[0]
    assert n == 6


def test_checks_flag_planted_question_mark_and_regloss(tmp_path):
    """A stub teacher that breaks the two rules on purpose: the rows are still
    written, flagged, and kept out of the clean file."""
    client = gen.OfflineClient()

    def bad(ctx):
        good = gen.offline_briefing(ctx)
        known = [t for s in config.KNOWN_STATES for t in ctx["concept_ledger"].get(s, [])]
        if known:  # re-gloss a term the ledger already marks known
            good["explained_terms"] = good["explained_terms"] + [known[0]]
            good["terms_used"] = good["terms_used"] + [known[0]]
            good["reglossed"] = known[0]
        else:  # or end with a question
            good["briefing"] += " Does that make sense?"
        return good

    client.register(config.BRIEFING, bad)
    out = tmp_path / "bad.jsonl"
    gen.main(["--offline", "--seed", "3", "--limit", "10", "--out", str(out), "--db", str(tmp_path / "b.db")], client=client)
    rows = _rows(out)
    assert len(rows) == 10
    flagged = [r["quality"]["checks"] for r in rows]
    assert all(not c["passed"] for c in flagged)
    assert any(not c["no_question_mark"] for c in flagged)
    assert any(not c["no_regloss"] and c["details"]["reglossed_terms"] for c in flagged)
    assert _rows(tmp_path / "bad.clean.jsonl") == []


def test_number_and_event_checks():
    packet = {
        "events": [{"headline": "Databricks raised $5 billion at a $190 billion valuation.", "detail": "Revenue run-rate of $7 billion, growing 80%."}],
        "concept_ledger": {s: [] for s in config.CONCEPT_STATES},
    }
    ok = {"briefing": "Databricks raised $5bn at a $190bn valuation, on a $7bn run-rate growing 80%.", "explained_terms": ["run-rate"], "terms_used": ["run rate", "valuation"]}
    checks = gen.run_checks(packet, ok, ["Coatue led a $2 billion fund close."])
    assert checks["numbers_supported"] and checks["references_only_packet_event"] and checks["explained_subset_of_used"]
    made_up = dict(ok, briefing="Databricks raised $5bn from 12 investors at a $190bn valuation.")
    assert gen.run_checks(packet, made_up, [])["details"]["unsupported_numbers"] == ["12"]
    other = dict(ok, briefing="Coatue led a $2 billion fund close this week.")
    assert not gen.run_checks(packet, other, ["Coatue led a $2 billion fund close."])["references_only_packet_event"]
    assert not gen.run_checks(packet, dict(ok, explained_terms=["exit"]), [])["explained_subset_of_used"]


def test_budget_stops_before_the_call_that_would_cross_it(tmp_path):
    out = tmp_path / "b.jsonl"
    gen.main(["--offline", "--seed", "3", "--limit", "50", "--budget", "0.03", "--out", str(out), "--db", str(tmp_path / "b.db")])
    rows = _rows(out)
    assert 0 < len(rows) < 50
    spent = sum(gen._cost(r["model"], r["quality"]["input_tokens"], r["quality"]["output_tokens"]) for r in rows)
    assert spent <= 0.03
    assert spent + gen._cost(config.SONNET, gen.EST_INPUT_TOKENS, gen.EST_OUTPUT_TOKENS) > 0.03
    # A budget of nothing makes no call at all.
    none = tmp_path / "none.jsonl"
    gen.main(["--offline", "--seed", "3", "--limit", "5", "--budget", "0", "--out", str(none), "--db", str(tmp_path / "n.db")])
    assert not none.exists() or _rows(none) == []


def test_resume_skips_ids_already_written(tmp_path):
    out = tmp_path / "r.jsonl"
    db = tmp_path / "r.db"
    gen.main(["--offline", "--seed", "5", "--limit", "4", "--out", str(out), "--db", str(db)])
    first = _rows(out)
    gen.main(["--offline", "--seed", "5", "--limit", "9", "--out", str(out), "--db", str(db)])
    rows = _rows(out)
    assert len(rows) == 9
    assert [r["id"] for r in rows[:4]] == [r["id"] for r in first]
    assert rows[:4] == first  # untouched, not regenerated
    assert len({r["id"] for r in rows}) == 9
    with Store(db) as store:
        n = store.conn.execute("SELECT COUNT(*) FROM judgment_log").fetchone()[0]
    assert n == 9  # the second run made only the five new calls


def test_dry_run_makes_no_calls_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "dry.jsonl"
    rc = gen.main(["--dry-run", "--limit", "30", "--out", str(out), "--db", str(tmp_path / "d.db")])
    assert rc == 0
    assert not out.exists() and not (tmp_path / "d.db").exists()
    text = capsys.readouterr().out
    assert "Coverage: 30 packets" in text and "Estimated live cost" in text and "Sample packets" in text


# --- plainness in the quality stats (briefing v7) ---------------------------------
# (appended: reporting only -- `quality.plainness` per row and the by-profile summary)


def test_rows_carry_the_plainness_bundle_and_the_summary_prints_by_profile(tmp_path, capsys):
    out = tmp_path / "gen.jsonl"
    rc = gen.main(["--offline", "--seed", "5", "--limit", "10", "--out", str(out), "--db", str(tmp_path / "g.db")])
    assert rc == 0
    rows = _rows(out)
    from harness.plainness import BUNDLE_FIELDS, plainness
    for r in rows:
        b = r["quality"]["plainness"]
        assert {k for k, _ in BUNDLE_FIELDS} <= set(b)
        assert b["words"] == r["quality"]["words"] and b["definitions"] == r["quality"]["definitions"]
        assert b["definitions_per_100w"] == r["quality"]["defs_per_100w"]
        assert b["fk_grade"] >= 0 and b["sentences"] >= 1
    # The bundle was computed against the variant's group vocabulary, not the briefing's own terms.
    fixtures = gen.load_fixtures()
    vocab = {(ev.key, v.persona_id): v.vocabulary for ev in fixtures for v in ev.variants}
    r0 = rows[0]
    expected = plainness(r0["output"]["briefing"], vocab[(r0["meta"]["event_key"], r0["meta"]["persona_id"])], sorted(gen._norm_terms(r0["output"]["explained_terms"])))
    assert r0["quality"]["plainness"] == expected
    text = capsys.readouterr().out
    assert "Plainness by profile" in text
    profiles = {r["meta"]["profile"] for r in rows}
    for profile in profiles:
        assert f"  {profile:<22}" in text


def test_plainness_by_profile_orders_profiles_and_summarises():
    rows = {
        "expert": [{"fk_grade": 18.0, "mean_sentence_length": 40.0, "domain_term_density": 6.0, "asides_per_100w": 0.0, "definitions_per_100w": 0.0}],
        "beginner": [
            {"fk_grade": 8.0, "mean_sentence_length": 15.0, "domain_term_density": 1.0, "asides_per_100w": 1.0, "definitions_per_100w": 1.0},
            {"fk_grade": 10.0, "mean_sentence_length": 17.0, "domain_term_density": 2.0, "asides_per_100w": 0.0, "definitions_per_100w": 0.5},
        ],
        "made_up": [{"fk_grade": 1.0, "mean_sentence_length": 1.0, "domain_term_density": 1.0, "asides_per_100w": 1.0, "definitions_per_100w": 1.0}],
    }
    table = gen.plainness_by_profile(rows)
    assert list(table) == ["beginner", "expert", "made_up"]
    assert table["beginner"]["n"] == 2 and table["beginner"]["fk_grade"]["mean"] == 9.0 and table["beginner"]["fk_grade"]["p90"] == 10.0
    assert table["expert"]["domain_term_density"]["p50"] == 6.0
    gen.print_plainness_by_profile({})  # nothing to print, no error
