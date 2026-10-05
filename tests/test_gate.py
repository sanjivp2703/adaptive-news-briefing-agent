"""Unit tests for `harness.gate`: the never-repeat check, the figure normaliser,
and the re-ask metric, each against a small synthetic store.

No network, no credentials. The synthetic databases are built through the real
`Store`, so the schema under test is the one the run writes.
"""

from __future__ import annotations

import json

import pytest

from conversational_agent.store import Store, now_iso
from harness import gate

# --- fixtures ------------------------------------------------------------------


def _artifact(personas: dict[str, str]) -> dict:
    """Minimal artifact: only what `RunDb` reads to map users to personas."""
    return {
        "mode": "live",
        "volatile": {"run_id": "run_test"},
        "personas": [{"persona_id": pid, "display_name": name} for pid, name in personas.items()],
    }


def _exchange(store: Store, *, user_id: str, group_id: str, briefing: str, event_id: str | None,
              explained: list[str], asked: list[str], exch_id: str, read_quality: str = "skimmed") -> None:
    store.conn.execute(
        "INSERT INTO exchanges (id, user_id, group_id, raised_at, topic, briefing, read_quality,"
        " closed_at, asked_about, created_at, explained_terms, event_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (exch_id, user_id, group_id, now_iso(), "t", briefing, read_quality, now_iso(),
         json.dumps(asked), now_iso(), json.dumps(explained), event_id),
    )


def _event(store: Store, *, event_id: str, user_id: str, group_id: str, headline: str) -> None:
    store.conn.execute(
        "INSERT INTO monitor_events (id, user_id, group_id, occurred_at, headline, is_material,"
        " origin, created_at) VALUES (?,?,?,?,?,1,'simulated_feed',?)",
        (event_id, user_id, group_id, now_iso(), headline, now_iso()),
    )


@pytest.fixture
def run(db_path):
    """A store with one persona user, one probe user, and a run row."""
    store = Store(db_path)
    user = store.create_user("Sam Ilori", kind="persona", user_id="usr_sam")
    probe = store.create_user("reading-regression-probe", kind="persona", user_id="usr_probe")
    for uid, gid in ((user.id, "grp_sam"), (probe.id, "grp_probe")):
        store.conn.execute(
            "INSERT INTO groups (id, user_id, name, poll_interval_minutes, created_at) VALUES (?,?,?,?,?)",
            (gid, uid, f"group {gid}", 60, now_iso()),
        )
    store.conn.execute(
        "INSERT INTO eval_runs (id, started_at, suite, config_json) VALUES ('run_test', ?, 'personas', '{}')",
        (now_iso(),),
    )
    for i in range(4):
        _event(store, event_id=f"evt_{i}", user_id="usr_sam", group_id="grp_sam", headline=f"Event {i}")
    yield store, db_path
    store.close()


def _rundb(db_path):
    return gate.RunDb(db_path, _artifact({"sam_beginner": "Sam Ilori"}))


# --- never repeat ----------------------------------------------------------------


def test_never_repeat_passes_on_distinct_events_and_texts(run):
    store, db_path = run
    _exchange(store, user_id="usr_sam", group_id="grp_sam", exch_id="e1", event_id="evt_0",
              briefing="Databricks raised five billion dollars at a huge valuation this week.",
              explained=[], asked=[])
    _exchange(store, user_id="usr_sam", group_id="grp_sam", exch_id="e2", event_id="evt_1",
              briefing="Arsenal signed a midfielder from Newcastle on deadline day for a record fee.",
              explained=[], asked=[])
    # A probe exchange with no event_id must not count against the persona.
    _exchange(store, user_id="usr_probe", group_id="grp_probe", exch_id="p1", event_id=None,
              briefing="probe", explained=[], asked=[])
    db = _rundb(db_path)
    check = gate.never_repeat(db.exchanges())
    db.close()
    assert check.passed, check.items
    assert check.data["briefings"] == 2


def test_never_repeat_fails_on_missing_event_id(run):
    store, db_path = run
    _exchange(store, user_id="usr_sam", group_id="grp_sam", exch_id="e1", event_id=None,
              briefing="Something happened.", explained=[], asked=[])
    db = _rundb(db_path)
    check = gate.never_repeat(db.exchanges())
    db.close()
    assert not check.passed
    assert any("no event_id" in i for i in check.items)


def test_never_repeat_fails_on_reused_event_id(run):
    store, db_path = run
    for n in (1, 2):
        _exchange(store, user_id="usr_sam", group_id="grp_sam", exch_id=f"e{n}", event_id="evt_0",
                  briefing=f"Completely different text number {n} about {'wine' if n == 1 else 'football'}.",
                  explained=[], asked=[])
    db = _rundb(db_path)
    check = gate.never_repeat(db.exchanges())
    db.close()
    assert not check.passed
    assert any("briefed twice" in i for i in check.items)


def test_never_repeat_fails_on_reworded_duplicate_and_prints_both(run):
    store, db_path = run
    a = "The Rioja harvest started early this year after a hot summer pushed ripening forward by weeks."
    b = "The Rioja harvest started early this year after a hot summer pushed ripening forward by three weeks."
    _exchange(store, user_id="usr_sam", group_id="grp_sam", exch_id="e1", event_id="evt_0", briefing=a, explained=[], asked=[])
    _exchange(store, user_id="usr_sam", group_id="grp_sam", exch_id="e2", event_id="evt_1", briefing=b, explained=[], asked=[])
    assert gate.token_jaccard(a, b) > gate.NEAR_DUPLICATE_JACCARD
    db = _rundb(db_path)
    check = gate.never_repeat(db.exchanges())
    db.close()
    assert not check.passed
    dup = next(i for i in check.items if "near-duplicate" in i)
    assert a in dup and b in dup, "both briefings must be printed verbatim"


# --- figure normaliser -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,key",
    [
        ("1,285", "1285"),
        ("1.285", "1285"),
        ("1285", "1285"),
        ("8,800", "88"),
        ("0.5", "5"),
        ("100", "1"),
        ("11", "11"),
    ],
)
def test_figure_key_is_significant_digits(raw, key):
    assert gate.figure_key(raw) == key


def test_forgiving_match_across_units_and_scales():
    reply = "They raised $1.285 billion, roughly 1.285bn, on revenue of 1,285 million."
    source = json.dumps({"detail": "The round was 1,285 million dollars."})
    assert gate.unsupported_figures(reply, source) == []


def test_extracts_only_unit_bearing_numbers_and_skips_years_and_ordinals():
    text = "On 6 August 2026 the 4th cut came in at 11% potential alcohol, about 13-15 hl/ha, for £45m."
    figs = gate.extract_figures(text)
    numbers = sorted(f["number"] for f in figs)
    assert numbers == ["11", "13", "15", "45"], figs
    # a bare year is not a figure even though it is a number
    assert not any(f["number"] == "2026" for f in figs)


def test_unsupported_figure_is_reported_with_excerpt():
    reply = "Newcastle sold him for £75m, which takes their summer sales past £240m."
    source = json.dumps({"source_event": {"detail": "Guimaraes has joined Arsenal; fee undisclosed."}})
    misses = gate.unsupported_figures(reply, source)
    assert [m["text"] for m in misses] == ["£75m", "£240m"]
    assert "£75m" in misses[0]["excerpt"]


def test_figure_in_lookup_summary_counts_as_supported():
    reply = "The Comité Champagne set the marketable yield at 8,800 kg/ha for 2026."
    source = json.dumps(
        {
            "source_event": {"detail": "picking began on 6 August"},
            "lookup": {"found": True, "summary": "marketable yield fixed at 8,800 kg of grapes per hectare"},
        }
    )
    assert gate.unsupported_figures(reply, source) == []


def test_literal_unicode_escape_in_reply_is_decoded_before_checking():
    # A double-escaped pound sign is stored as the six characters backslash-u-0-0-a-3;
    # built by concatenation so no editor or tool decodes it on the way in.
    escape = "\\u00a3"
    reply = "It's a season-long loan for a " + escape + "3.5m fee."
    assert "\\" in reply and len(escape) == 6
    source = json.dumps({"source_event": {"detail": "a loan with a \u00a33.5m fee"}})
    assert gate.unsupported_figures(reply, source) == []
    assert gate.literal_escapes(reply) == [escape]
    assert gate.decode_literal_escapes(reply) == "It's a season-long loan for a \u00a33.5m fee."


# --- re-ask metric -----------------------------------------------------------------


def test_reask_rate_counts_briefings_whose_thread_asked_about_a_defined_term(run):
    store, db_path = run
    # defined `head coach`, thread asked about `head coach` -> re-ask
    _exchange(store, user_id="usr_sam", group_id="grp_sam", exch_id="e1", event_id="evt_0",
              briefing="b1", explained=["head coach"], asked=["head coach", "add-ons"])
    # defined `annualized revenue run-rate`, asked about the canonical spelling -> re-ask
    _exchange(store, user_id="usr_sam", group_id="grp_sam", exch_id="e2", event_id="evt_1",
              briefing="b2", explained=["annualized revenue run-rate"], asked=["annualized revenue run rate"])
    # defined `promoted`, asked about `promotion` -> not the same canonical term
    _exchange(store, user_id="usr_sam", group_id="grp_sam", exch_id="e3", event_id="evt_2",
              briefing="b3", explained=["promoted"], asked=["promotion"])
    # nothing asked
    _exchange(store, user_id="usr_sam", group_id="grp_sam", exch_id="e4", event_id="evt_3",
              briefing="b4", explained=["valuation"], asked=[])
    db = _rundb(db_path)
    rate, events, n = gate.reask_rate(db.exchanges())
    db.close()
    assert n == 4
    assert rate == pytest.approx(0.5)
    assert [e["exchange_id"] for e in events] == ["e1", "e2"]
    assert events[0]["terms"] == ["head coach"]


def test_reask_rate_is_none_with_no_briefings(run):
    _, db_path = run
    db = _rundb(db_path)
    rate, events, n = gate.reask_rate(db.exchanges())
    db.close()
    assert (rate, events, n) == (None, [], 0)


# --- the whole thing, end to end on a synthetic run --------------------------------


def test_evaluate_marks_tier1_incomplete_without_a_database(tmp_path):
    artifact = tmp_path / "cand.json"
    artifact.write_text(json.dumps(_artifact({"sam_beginner": "Sam Ilori"}) | {
        "no_questions_to_user": {"clean": True, "briefings_checked": 0, "offending": []},
        "reading_regression": {"clean": True, "violations": []},
        "silence_regression": {"clean": True, "violations": []},
    }), encoding="utf-8")
    report = gate.evaluate(artifact)
    assert report.status == "INCOMPLETE"
    assert report.tier1_passed is None
    assert not report.passed


# --- pairing refusal and the learning-trend checks --------------------------------
# (appended with the horizon / cheap-regression work)


def _run_settings(**over):
    base = {
        "schema": "run_settings/v1", "fixture_dir": "harness/personas", "persona_ids": ["sam_beginner"],
        "rounds_cap": 4, "horizon_days": None, "retention_probes": 0,
        "harness_routing": {"persona_model": "claude-sonnet-5", "grader_model": "claude-sonnet-5", "cheap": False, "remediation_search": True},
        "ledger_truth": "dynamic", "materiality_cache": False, "parallel": 1,
    }
    base.update(over)
    return base


def _art(rounds=4, probe_only=0, settings=None, personas=("sam_beginner",)):
    art = {
        "mode": "live",
        "volatile": {"run_id": "run_test"},
        "no_questions_to_user": {"clean": True, "briefings_checked": 0, "offending": []},
        "reading_regression": {"clean": True, "violations": []},
        "silence_regression": {"clean": True, "violations": []},
        "personas": [
            {
                "persona_id": pid, "display_name": pid,
                "rounds": [{"index": i, "thread_ran": True, "quiet": False} for i in range(1, rounds + 1)]
                + [{"index": rounds + k, "thread_ran": False, "quiet": False, "probe_only": True} for k in range(1, probe_only + 1)],
                "threads": [], "probes": [], "true_concepts": {"initial_known": [], "final_known": [], "ever_known": []},
            }
            for pid in personas
        ],
    }
    if settings is not None:
        art["run_settings"] = settings
    return art


def test_pairing_signature_agrees_for_same_settings_and_treats_legacy_as_sonnet_full():
    assert gate.pairing_mismatches(_art(settings=_run_settings()), _art(settings=_run_settings())) == []
    # Two pre-header artifacts with the same rounds pair (legacy defaults assumed on both sides).
    assert gate.pairing_mismatches(_art(), _art()) == []
    # A legacy artifact pairs with a full Sonnet run of the same shape...
    assert gate.pairing_mismatches(_art(settings=_run_settings()), _art()) == []
    # ...and not with a cheap one.
    cheap = _run_settings(harness_routing={"persona_model": "claude-haiku-4-5-20251001", "grader_model": "claude-sonnet-5", "cheap": True, "remediation_search": False})
    reasons = gate.pairing_mismatches(_art(settings=cheap), _art())
    assert any("persona_model" in r for r in reasons) and any("remediation_search" in r for r in reasons)


@pytest.mark.parametrize(
    "cand, base, fragment",
    [
        (_art(rounds=4), _art(rounds=8), "rounds run"),
        (_art(settings=_run_settings(rounds_cap=4)), _art(settings=_run_settings(rounds_cap=8)), "--rounds"),
        (_art(settings=_run_settings(horizon_days=30.0)), _art(settings=_run_settings()), "--horizon-days"),
        (_art(probe_only=2, settings=_run_settings(horizon_days=30.0, retention_probes=2)), _art(settings=_run_settings(horizon_days=30.0)), "--retention-probes"),
        (_art(settings=_run_settings(fixture_dir="eval-runs/fixtures-b")), _art(settings=_run_settings()), "fixture dir"),
        (_art(personas=("sam_beginner", "pilar_wine")), _art(), "persona ids"),
        (_art(settings=_run_settings(harness_routing={"persona_model": "claude-sonnet-5", "grader_model": "claude-opus-5", "cheap": False, "remediation_search": True})), _art(settings=_run_settings()), "grader_model"),
    ],
)
def test_gate_refuses_to_pair_mismatched_headers(tmp_path, cand, base, fragment):
    assert any(fragment in r for r in gate.pairing_mismatches(cand, base))
    c, b = tmp_path / "cand.json", tmp_path / "base.json"
    c.write_text(json.dumps(cand), encoding="utf-8")
    b.write_text(json.dumps(base), encoding="utf-8")
    report = gate.evaluate(c, b)
    check = next(x for x in report.tier1 if x.id == "paired_comparison")
    assert check.passed is None and any(fragment in item for item in check.items)
    assert report.status == "INCOMPLETE" and not report.passed
    assert "baseline refused" in report.rerun_advice
    # No paired Tier 2 value survives a refusal.
    assert all(x.baseline is None for x in report.tier2 if x.id.startswith("ledger_"))
    assert gate.main([str(c), "--baseline", str(b)]) == 1


def test_probe_only_rounds_do_not_count_toward_rounds_run():
    a = _art(rounds=4, probe_only=3, settings=_run_settings(horizon_days=30.0, retention_probes=3))
    assert gate.pairing_signature(a)["rounds_run"] == {"sam_beginner": 4}


def _entry(known_slope=None, eff=None, retained=(None, 0), calibration=0.0, confab=(0, 0), pid="sam_beginner"):
    from harness.metrics import PersonaLearning, TeachingEfficiency

    return PersonaLearning(
        persona_id=pid, prior_knowledge=0.1, memory_rate=0.75, sessions=[], immediate_mean=None, immediate_n=0,
        retained_by_bucket={"<=1d": (None, 0), "2-7d": retained, ">7d": (None, 0), ">30d": (None, 0)},
        confabulated=confab[0], pseudo_n=confab[1], calibration_gap=calibration, calibration_n=1,
        initial_known=1, final_known=3, ever_known=3, learned=[], learned_by_asking=[], forgotten=[],
        known_slope=known_slope,
        efficiency=None if eff is None else TeachingEfficiency(taught_terms=["t"] * eff, briefings_read=1, words_read=100, first_probe_rungs={}),
    )


def _by_id(check_list, check_id):
    return next(c for c in check_list if c.id == check_id)


def test_learning_checks_gate_known_slope_efficiency_and_retained_rung():
    cand = {"sam_beginner": _entry(known_slope=0.32, eff=2, retained=(2.0, 3))}
    base = {"sam_beginner": _entry(known_slope=0.50, eff=3, retained=(2.5, 4))}
    checks, rows = gate.learning_checks(cand, base)
    slope = _by_id(checks, "learning_known_slope")
    assert slope.passed is False and slope.value == 0.32 and slope.baseline == 0.50  # drop 0.18 > 0.15
    assert slope.marginal is True  # 0.03 over the band: within the 0.05 marginal band
    far, _ = gate.learning_checks({"sam_beginner": _entry(known_slope=0.10)}, base)
    assert _by_id(far, "learning_known_slope").passed is False and _by_id(far, "learning_known_slope").marginal is False
    eff = _by_id(checks, "learning_efficiency")
    assert eff.passed is False and "relative drop +33.3%" in eff.detail  # 3 -> 2 per briefing
    ret = _by_id(checks, "learning_retained_2_7d")
    assert ret.passed is False and ret.value == 2.0 and ret.baseline == 2.5  # drop 0.5 > 0.4
    assert rows[0]["known_slope"] == 0.32 and rows[0]["baseline_efficiency_per_briefing"] == 3.0

    # Within the bands: all pass.
    ok, _ = gate.learning_checks({"sam_beginner": _entry(known_slope=0.40, eff=3, retained=(2.2, 3))}, base)
    assert all(c.passed for c in ok)
    # Retained is not gated below n = 3 on either side; a zero baseline efficiency is not gated.
    small, _ = gate.learning_checks({"sam_beginner": _entry(known_slope=0.5, eff=0, retained=(0.0, 2))}, {"sam_beginner": _entry(known_slope=0.5, eff=0, retained=(3.0, 5))})
    assert _by_id(small, "learning_retained_2_7d").passed is True and "not gated" in _by_id(small, "learning_retained_2_7d").detail
    assert _by_id(small, "learning_efficiency").passed is True and "baseline is zero" in _by_id(small, "learning_efficiency").detail
    # Slope unrecorded on both sides (pre-header artifacts) passes with a note.
    legacy, _ = gate.learning_checks({"sam_beginner": _entry()}, {"sam_beginner": _entry()})
    assert _by_id(legacy, "learning_known_slope").passed is True and "not recorded" in _by_id(legacy, "learning_known_slope").detail
    # Without a baseline nothing is gated, and the values still print.
    solo, solo_rows = gate.learning_checks(cand, None)
    assert all(c.passed for c in solo) and solo_rows[0]["baseline_known_slope"] is None


# --- plainness (briefing v7: plain language over inline definitions) ------------
# (appended with the plainness bundle; `harness/plainness.py`, the Tier 3
# plainness-by-band line and the Tier 2 `definitions_ceiling` check)

from harness import plainness as pl

_VOCAB = ("harvest", "potential alcohol", "derogation", "cru", "yields", "appellation",
          "chaptalisation", "reserve wines", "reference minimum", "vintage")
PLAIN = ("Grape growers in Champagne started picking on 6 August. That is the earliest start anyone "
         "can remember. A hot summer ripened the fruit weeks early. Growers had to ask for special "
         "permission to pick this soon, and they got it.")
DENSE = ("Champagne's harvest opened 6 August under a derogation, with Chardonnay already past 11% "
         "potential alcohol and yields tracking below the appellation's reference minimum; "
         "chaptalisation is off the table and houses are leaning on reserve wines to hold the cuvée.")
GLOSSED = ("The harvest (the picking of the grapes) opened on 6 August under a derogation — a special "
           "permission from the regulator — with Chardonnay past 11% potential alcohol (a measure of grape "
           "sugar) and yields — the crop per hectare — running low, so the cru (the village ranking) "
           "mattered less than usual this vintage (the year's crop).")


def test_plainness_orders_plain_dense_and_glossed_paragraphs():
    plain = pl.plainness(PLAIN, _VOCAB, [])
    dense = pl.plainness(DENSE, _VOCAB, [])
    glossed = pl.plainness(GLOSSED, _VOCAB, ["harvest", "derogation", "potential alcohol", "yields", "cru", "vintage"])
    # Grade level and sentence length: the beginner paragraph is plainest.
    assert plain["fk_grade"] < dense["fk_grade"] and plain["fk_grade"] < glossed["fk_grade"]
    assert plain["mean_sentence_length"] < dense["mean_sentence_length"]
    assert plain["sentences"] == 4 and dense["sentences"] == 1
    # Domain-term density: the expert paragraph is densest; the plain one uses no term at all.
    assert plain["domain_term_density"] == 0.0
    assert dense["domain_term_density"] > glossed["domain_term_density"] > plain["domain_term_density"]
    # Asides and definitions: only the gloss-stacked paragraph carries them.
    assert glossed["parenthetical_asides"] == 6 and plain["parenthetical_asides"] == 0 and dense["parenthetical_asides"] == 0
    assert glossed["asides_per_100w"] > dense["asides_per_100w"] == plain["asides_per_100w"] == 0.0
    assert glossed["definitions_per_100w"] > 2.0 and plain["definitions_per_100w"] == dense["definitions_per_100w"] == 0.0
    # Formal glosses without parentheses count as glosses; a plain-words explanation does not.
    assert pl.parenthetical_asides("The transfer window, meaning the fixed period when clubs may buy players, shut on Monday.") == 1
    assert pl.parenthetical_asides("The transfer window — meaning the fixed period when clubs may buy players — shut on Monday.") == 1
    assert pl.parenthetical_asides("Growers needed a derogation, that is, special permission, to pick early.") == 1
    assert pl.parenthetical_asides("Valuation, i.e. what the whole company is deemed worth, doubled.") == 1
    assert pl.parenthetical_asides("Yields (the crop per hectare) were low; a cru — a ranked village — mattered less.") == 2
    assert pl.parenthetical_asides("Meaning is not a gloss here. SpaceX paid with its own shares rather than cash.") == 0
    v7 = pl.plainness("SpaceX paid with its own shares rather than cash, so Anysphere's owners now hold SpaceX stock.", (), ["all-stock deal", "stock"])
    assert v7["definitions_per_100w"] > 2.0 and v7["asides_per_100w"] == 0.0  # made clear, not glossed
    # Deterministic, and every bundle field is present.
    assert pl.plainness(GLOSSED, _VOCAB, ["cru"]) == pl.plainness(GLOSSED, _VOCAB, ["cru"])
    assert {k for k, _ in pl.BUNDLE_FIELDS} <= set(plain)


def test_syllable_heuristic_and_term_matching_edge_cases():
    assert pl.count_syllables("wine") == 1 and pl.count_syllables("table") == 2
    assert pl.count_syllables("derogation") == 4 and pl.count_syllables("the") == 1
    assert pl.count_syllables("$60") == 0 and pl.plainness("", _VOCAB, []) == pl.plainness(None, _VOCAB, [])
    # A nested term counts once, for the longer term; plurals and case match; `$1.5bn` does not split a sentence.
    assert pl.domain_term_hits("Reserve wines and the reserve are different; yields fell.", ("reserve wines", "reserve", "yield")) == 3
    assert pl.sentence_count("It raised $1.5bn at 11.2% and stopped. Then nothing.") == 2 and pl.sentence_count("No full stop") == 1
    assert pl.summarise([]) == {"n": 0} | {k: {"p50": None, "p90": None, "mean": None} for k, _ in pl.BUNDLE_FIELDS}


def _thread(band, briefing, explained=(), terms_used=(), bundle=None, rnd=1):
    t = {"round": rnd, "proficiency_before": band, "briefing": briefing,
         "explained_terms": list(explained), "terms_used": list(terms_used)}
    if bundle is not None:
        t["plainness"] = bundle
    return t


def test_thread_bundles_take_the_artifact_bundle_or_recompute_and_group_by_band():
    ready = pl.plainness(PLAIN, _VOCAB, [])
    art = _art(personas=("sam_beginner", "not_a_fixture"))
    art["personas"][0]["threads"] = [
        _thread("beginner", PLAIN, bundle=ready, rnd=1),
        _thread("developing", DENSE, terms_used=["derogation"], rnd=2),
        _thread("beginner", "   ", rnd=3),  # empty briefing: not a row
    ]
    art["personas"][1]["threads"] = [_thread("beginner", GLOSSED, explained=["cru", "harvest"], terms_used=["cru", "harvest"], rnd=1)]
    rows = gate.thread_bundles(art)
    assert [(r["persona_id"], r["round"], r["band"], r["recomputed"]) for r in rows] == [
        ("sam_beginner", 1, "beginner", False), ("sam_beginner", 2, "developing", True), ("not_a_fixture", 1, "beginner", True),
    ]
    assert rows[0]["bundle"] is ready
    # The unknown persona's recompute fell back to its own `terms_used` as the vocabulary.
    assert rows[2]["bundle"]["domain_term_hits"] == 2 and rows[2]["bundle"]["definitions"] == 2
    by = gate.plainness_by_band(rows)
    assert by["all"]["n"] == 3 and list(by["by_band"]) == ["beginner", "developing"] and by["recomputed"] == 2
    assert by["by_band"]["developing"]["fk_grade"]["p50"] == rows[1]["bundle"]["fk_grade"]
    assert gate.thread_bundles(None) == [] and gate.thread_bundles({}) == []


def _rows(asides_per_100w):
    return [{"persona_id": "p", "round": i, "exchange_id": None, "band": "beginner",
             "bundle": {"asides_per_100w": d, "parenthetical_asides": 1, "definitions_per_100w": 0.0, "words": 100}, "recomputed": False}
            for i, d in enumerate(asides_per_100w, 1)]


def test_definitions_ceiling_is_gated_absolutely_and_against_the_baseline():
    over = gate.definitions_ceiling(_rows([0.5, 2.5, 3.0, 2.1] + [1.0] * 6), None)  # 3/10 over 2.0
    assert over.passed is False and over.value == 0.3 and over.marginal is False and len(over.items) == 3
    assert over.items[0].startswith("p r3 (beginner): 3.00 glosses/100w")
    edge = gate.definitions_ceiling(_rows([2.5, 2.5] + [0.0] * 8), None)  # exactly 0.20 passes
    assert edge.passed is True and edge.value == 0.2
    marg = gate.definitions_ceiling(_rows([2.5] * 6 + [0.0] * 20), None)  # 6/26 = 0.231: within 0.05 of the band
    assert marg.passed is False and marg.marginal is True
    # Within the absolute band but up more than 0.10 on the baseline: fails, with the baseline recorded.
    rise = gate.definitions_ceiling(_rows([2.5, 2.5] + [0.0] * 8), _rows([0.0] * 10))
    assert rise.passed is False and rise.baseline == 0.0 and "rose +0.200" in rise.detail
    same = gate.definitions_ceiling(_rows([2.5, 2.5] + [0.0] * 8), _rows([2.5, 2.5] + [0.0] * 8))
    assert same.passed is True and same.baseline == 0.2
    assert gate.definitions_ceiling([], None).passed is None
    # A briefing exactly at the ceiling is not over it.
    assert gate.definitions_ceiling(_rows([2.0] * 10), None).value == 0.0


def test_evaluate_reports_the_ceiling_and_plainness_by_band_without_a_database(tmp_path):
    art = _art()
    art["personas"][0]["threads"] = [
        _thread("beginner", PLAIN, rnd=1),
        _thread("beginner", GLOSSED, explained=["cru", "harvest", "yields"], terms_used=["cru", "harvest", "yields"], rnd=2),
        _thread("developing", DENSE, rnd=3),
    ]
    path = tmp_path / "cand.json"
    path.write_text(json.dumps(art), encoding="utf-8")
    report = gate.evaluate(path)
    ceiling = _by_id(report.tier2, "definitions_ceiling")
    assert ceiling.passed is False and ceiling.value == round(1 / 3, 4) and "no baseline" in ceiling.detail
    pb = report.tier3["plainness_by_band"]
    assert pb["all"]["n"] == 3 and set(pb["by_band"]) == {"beginner", "developing"} and pb["recomputed"] == 3
    text = gate.render(report)
    assert "definitions_ceiling" in text and "plainness by band at the time of the briefing" in text
    assert "    beginner     n=2 " in text and "    developing   n=1 " in text
    # A baseline artifact with the same shape pairs, and its rows print under the candidate's.
    base = tmp_path / "base.json"
    base_art = _art()
    base_art["personas"][0]["threads"] = [_thread("beginner", PLAIN, rnd=1), _thread("beginner", PLAIN, rnd=2), _thread("developing", DENSE, rnd=3)]
    base.write_text(json.dumps(base_art), encoding="utf-8")
    paired = gate.evaluate(path, base)
    check = _by_id(paired.tier2, "definitions_ceiling")
    assert check.baseline == 0.0 and "rose" in check.detail
    assert "  baseline" in gate.render(paired)
    # The thresholds travel in the JSON report.
    th = paired.to_dict()["thresholds"]
    assert th["definitions_ceiling_per_100w"] == 2.0 and th["definitions_ceiling_max_share"] == 0.2
