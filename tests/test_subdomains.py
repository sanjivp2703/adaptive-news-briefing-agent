"""Subdomains: where inside a group the user is at home.

Human direction (2026-09-14): "the glossary shouldn't be an exact word to word
matching they should cover the subjects subdomains within the group that the
user is familiar with and use that among other factors to decide how in depth
the harness needs to explain a certain topic for the user to understand."

The read-through behind it: Pilar (a wine professional) was given the same
inline definition of `hectolitres per hectare` as Sam (a beginner). The
per-term ledger had never seen her use that exact string, and the per-term
ledger alone cannot know that an expert in viticulture will hold a viticulture
term she has never been observed using. The subdomain readout can: it is the
same evidence, grouped by the slice of the group each term belongs to.

What these tests pin down:

  * a label is a fact about the TERM -- stored, canonicalised, never lost to a
    null, and settled once the user has done anything about the term;
  * the per-subdomain counts are the group band's own arithmetic over a
    subset, and unlabelled terms count toward no subdomain;
  * the readout reaches the briefing, the router and the reply, and the
    extractor gets the existing labels (names only) -- while reading
    behaviour reaches none of them;
  * a verdict without the new field is fine, which is what keeps the offline
    stub (not ours) working.
"""

from __future__ import annotations

import json

import pytest

from conversational_agent import config, judgments
from conversational_agent.assessor import Assessor
from conversational_agent.store import normalize_subdomain

READING_FIELDS = (
    "dwell_ms",
    "scroll_fraction",
    "read_quality",
    "reading_source",
    "attention",
    "skip_kind",
)
READ_BANDS = ("skipped", "skimmed", "studied")


def _assert_no_reading(ctx: dict) -> None:
    """The same two scans the harness's reading_regression runs: the field
    names (a field carried structurally) and the band strings (the numbers
    folded into prose)."""
    blob = json.dumps(ctx)
    for field in READING_FIELDS:
        assert f'"{field}"' not in blob, f"{field} leaked into a judgment context"
    for band in READ_BANDS:
        assert f'"{band}"' not in blob, f"read-quality band {band!r} leaked"


def _contexts(stub, point: str) -> list[dict]:
    return [c["context"]["context"] for c in stub.calls if c["point"] == point]


@pytest.fixture
def world(store):
    user = store.create_user("pilar", kind="real")
    scope = store.scope(user.id)
    group = scope.create_group("Champagne & Burgundy", description="wine trade")
    return scope, group


def _confirm(scope, group_id, terms, subdomains=None):
    for _ in range(config.CORRECT_USES_BEFORE_CONFIRMED):
        scope.mark_correct_use(group_id, terms, subdomains=subdomains)


def _by_term(scope, group_id):
    return {c.term: c for c in scope.ledger(group_id)}


# ---------------------------------------------------------------------------
# Labels on terms
# ---------------------------------------------------------------------------


# Prevents: labels forking on spelling. The label is a key in the familiarity
# readout, so `Appellation-Rules` and `appellation rules` must be one slice,
# and a model wrapping its example in backticks must not create a new one.
def test_labels_are_stored_and_canonicalised(world):
    scope, group = world

    scope.mark_explained(
        group.id, ["derogation"], subdomains={"Derogation": " Appellation-Rules "}
    )
    scope.note_exposure(
        group.id, ["hectolitres per hectare"], subdomains={"hectolitres per hectare": "`viticulture & harvest`"}
    )
    scope.mark_correct_use(group.id, ["budburst"], subdomains={"budburst": "Viticulture &   Harvest"})

    by_term = _by_term(scope, group.id)
    assert by_term["derogation"].subdomain == "appellation rules"
    assert by_term["hectolitres per hectare"].subdomain == "viticulture & harvest"
    assert by_term["budburst"].subdomain == "viticulture & harvest"
    assert scope.subdomain_labels(group.id) == ["appellation rules", "viticulture & harvest"]
    assert normalize_subdomain("") is None and normalize_subdomain(None) is None
    # `Concept.subdomain` is what `scope.ledger()` returns -- the contract the
    # harness reads.
    assert all(hasattr(c, "subdomain") for c in scope.ledger(group.id))


# Prevents: a later call that names the term without a label wiping the one
# it has. Most writes carry no label (a reply's exposure, a read-gloss credit)
# and none of them may cost the term its slice.
def test_a_null_or_empty_label_never_overwrites_a_real_one(world):
    scope, group = world
    scope.note_exposure(group.id, ["derogation"], subdomains={"derogation": "appellation rules"})

    scope.note_exposure(group.id, ["derogation"])
    scope.mark_read_explanation(group.id, ["derogation"], subdomains=None)
    scope.mark_correct_use(group.id, ["derogation"], subdomains={"derogation": ""})
    scope.mark_explained(group.id, ["derogation"], subdomains={"derogation": None})
    scope.mark_unknown(group.id, ["derogation"], subdomains={})

    assert _by_term(scope, group.id)["derogation"].subdomain == "appellation rules"


# Prevents: churn. An exposure-era label (from the briefing that first used
# the term) may be corrected by the first call that reads what the user did
# with the term; after that the label is fixed, however many later calls file
# it elsewhere. Attested = asked / used / got wrong / read twice -- the same
# predicate the band uses.
def test_a_differing_label_wins_only_until_the_term_is_attested(world):
    scope, group = world
    term = "hectolitres per hectare"

    scope.note_exposure(group.id, [term], subdomains={term: "viticulture & harvest"})
    scope.note_exposure(group.id, [term], subdomains={term: "yields & quotas"})
    assert _by_term(scope, group.id)[term].subdomain == "yields & quotas", (
        "exposure only: the term is unattested, so a new label still wins"
    )

    # The first attesting write carries its own label, and that label wins:
    # before this write the term had no attested evidence.
    scope.mark_explained(group.id, [term], subdomains={term: "appellation rules"})
    assert _by_term(scope, group.id)[term].subdomain == "appellation rules"

    # Now attested. Nothing moves it.
    scope.mark_correct_use(group.id, [term], subdomains={term: "viticulture & harvest"})
    scope.mark_unknown(group.id, [term], subdomains={term: "commercial & market"})
    scope.note_exposure(group.id, [term], subdomains={term: "yields & quotas"})
    assert _by_term(scope, group.id)[term].subdomain == "appellation rules"

    # The read-explanation path attests at READ_EXPLANATIONS_BEFORE_BAND, not
    # before: one read gloss leaves the label open, the second closes it.
    scope.mark_read_explanation(group.id, ["lees"], subdomains={"lees": "winemaking"})
    scope.mark_read_explanation(group.id, ["lees"], subdomains={"lees": "cellar work"})
    assert _by_term(scope, group.id)["lees"].subdomain == "cellar work"
    assert _by_term(scope, group.id)["lees"].read_explanations == 2
    scope.mark_read_explanation(group.id, ["lees"], subdomains={"lees": "winemaking"})
    assert _by_term(scope, group.id)["lees"].subdomain == "cellar work"


# Prevents: a misunderstanding relabelling a term. Getting `derogation` wrong
# does not move it out of `appellation rules`; it makes the user one term less
# known inside it. The label survives the revert that resets the counters.
def test_a_misunderstanding_resets_counts_but_keeps_the_label(world):
    scope, group = world
    _confirm(scope, group.id, ["derogation"], {"derogation": "appellation rules"})
    scope.mark_unknown(group.id, ["derogation"])
    c = _by_term(scope, group.id)["derogation"]
    assert c.state == config.CONCEPT_UNKNOWN and c.correct_uses == 0
    assert c.subdomain == "appellation rules"


# ---------------------------------------------------------------------------
# Per-subdomain familiarity
# ---------------------------------------------------------------------------


# Prevents: the per-slice readout drifting from the group band. Same
# predicates -- confirmed always; familiar/explained at the read threshold;
# provisional never; attested = asked / used / wrong / read twice -- so the
# slices sum to the group and each slice agrees with `counts_toward_band`.
# The constructed user is Pilar: conversant in `appellation rules`, a
# beginner in `commercial & market`, with a few terms nobody has labelled.
def test_subdomain_familiarity_uses_the_proficiency_predicates(world):
    scope, group = world
    rules = {t: "appellation rules" for t in ("derogation", "aoc", "cru", "yield cap", "chaptalisation", "dosage")}
    market = {t: "commercial & market" for t in ("en primeur", "negociant", "allocation")}

    # appellation rules: five confirmed (known, attested), one asked once
    # (attested, not known) -> 5 / 6.
    _confirm(scope, group.id, ["derogation", "aoc", "cru", "yield cap", "chaptalisation"], rules)
    scope.mark_explained(group.id, ["dosage"], subdomains=rules)

    # commercial & market: one asked once (attested, not known), one used
    # once (provisional: attested -- they used it -- but never known), one
    # read once (familiar at one read: neither) -> 0 / 2.
    scope.mark_explained(group.id, ["en primeur"], subdomains=market)
    scope.mark_correct_use(group.id, ["negociant"], subdomains=market)
    scope.mark_read_explanation(group.id, ["allocation"], subdomains=market)

    # Unlabelled: exposure only, plus one confirmed term nobody labelled.
    scope.note_exposure(group.id, ["vintage", "terroir"])
    _confirm(scope, group.id, ["brett"])

    fam = scope.subdomain_familiarity(group.id)

    assert fam["appellation rules"] == {"known": 5, "attested": 6, "band": config.CONVERSANT}
    assert fam["commercial & market"] == {"known": 0, "attested": 2, "band": config.BEGINNER}
    assert fam[config.UNLABELLED_SUBDOMAIN] == {"known": 1, "attested": 1, "band": config.BEGINNER}
    assert set(fam) == {"appellation rules", "commercial & market", config.UNLABELLED_SUBDOMAIN}
    for entry in fam.values():
        assert set(entry) == {"known", "attested", "band"}

    # Same arithmetic as the group band: the slices sum to it exactly.
    known = sum(e["known"] for e in fam.values())
    attested = sum(e["attested"] for e in fam.values())
    assert scope.proficiency(group.id) == config.proficiency_band(known, attested)
    # And each slice's `known` is the ledger's own per-term verdict.
    for label, entry in fam.items():
        in_slice = [
            c for c in scope.ledger(group.id)
            if (c.subdomain or config.UNLABELLED_SUBDOMAIN) == label
        ]
        assert entry["known"] == sum(1 for c in in_slice if c.counts_toward_band)


# Prevents: an unlabelled term propping up (or dragging down) a slice it was
# never assigned to. Pilar's confirmed-but-unlabelled term must not make
# `commercial & market` look known.
def test_unlabelled_terms_count_toward_no_other_subdomain(world):
    scope, group = world
    scope.mark_explained(group.id, ["en primeur"], subdomains={"en primeur": "commercial & market"})
    _confirm(scope, group.id, ["brett", "lees", "tannin", "vintage"])

    fam = scope.subdomain_familiarity(group.id)
    assert fam["commercial & market"]["known"] == 0
    assert fam[config.UNLABELLED_SUBDOMAIN]["known"] == 4
    assert fam[config.UNLABELLED_SUBDOMAIN] not in ({}, None)
    assert config.UNLABELLED_SUBDOMAIN not in scope.subdomain_labels(group.id)


# Prevents: the slice floors quietly becoming the group floors (under which a
# ten-term subdomain could never read as conversant), or losing the ratio
# guard that stops two lucky confirmations dominating a tiny slice.
def test_subdomain_band_floors():
    band = config.subdomain_band
    assert band(0, 0) == config.BEGINNER
    assert band(1, 1) == config.BEGINNER, "one known term is not a slice you are at home in"
    assert band(2, 2) == config.DEVELOPING
    assert band(2, 5) == config.BEGINNER, "ratio 0.4 < 0.5"
    assert band(4, 5) == config.CONVERSANT, "4 known, ratio 0.8"
    assert band(4, 6) == config.DEVELOPING, "ratio 0.67 < 0.7"
    assert band(8, 10) == config.FLUENT
    assert band(8, 11) == config.CONVERSANT, "ratio 0.73 < 0.8"
    assert band(30, 200) == config.BEGINNER
    # Lower than the group's absolute floors, higher on ratio, by design.
    assert config.SUBDOMAIN_DEVELOPING_MIN_KNOWN < 3
    assert config.SUBDOMAIN_CONVERSANT_MIN_KNOWN < 12
    assert config.SUBDOMAIN_FLUENT_MIN_KNOWN < 25
    assert config.SUBDOMAIN_CONVERSANT_MIN_RATIO > 0.6
    # A slice that would be `conversant` under the slice floors is still
    # `beginner` at group level -- the two readouts answer different questions.
    assert config.proficiency_band(5, 6) == config.DEVELOPING
    assert config.subdomain_band(5, 6) == config.CONVERSANT


# ---------------------------------------------------------------------------
# What reaches each call
# ---------------------------------------------------------------------------


def _briefing_verdict(ctx):
    return {
        "briefing": "Yields came in at 9,000 kg/ha — hectolitres per hectare being the usual measure.",
        "topic": "2026 yields",
        "terms_used": ["Hectolitres per hectare", "yields", "appellation"],
        "explained_terms": ["hectolitres per hectare"],
        # Wire shape: a list of pairs, since a strict schema cannot carry a map.
        "subdomains": [
            {"term": "hectolitres per hectare", "subdomain": "Viticulture & Harvest"},
            {"term": "yields", "subdomain": "viticulture & harvest"},
            {"term": "appellation", "subdomain": "appellation rules"},
        ],
        "reasoning": "stub",
    }


def _seed_event(scope, group):
    eid = scope.record_event(
        group.id, headline="Comité fixes 2026 yield", detail="9,000 kg/ha agreed."
    )
    scope.apply_materiality(eid, is_material=True, score=90.0, reason="t")
    return eid


# Prevents: the briefing being written without the per-slice readout, or with
# anything about reading in it. Also pins that the briefing's own labels land
# on the exposed terms (canonicalised) and that the event's subdomain is NOT
# pre-computed -- the prompt decides that.
def test_briefing_context_carries_familiarity_and_no_reading_field(judge, stub, world):
    scope, group = world
    stub.register(config.BRIEFING, _briefing_verdict)
    assessor = Assessor(judge, reply_search=False)
    _confirm(scope, group.id, ["derogation", "aoc"], {"derogation": "appellation rules", "aoc": "appellation rules"})

    # An earlier, read briefing, so the DB holds reading data to leak.
    _seed_event(scope, group)
    first = assessor.raise_topic(scope, group)
    scope.record_reading(first.exchange_id, dwell_ms=90_000, scroll_fraction=1.0, source="simulated")
    assert scope.get_exchange(first.exchange_id).read_quality in ("read", "studied")

    _seed_event(scope, group)
    assessor.raise_topic(scope, group)

    ctx = _contexts(stub, config.BRIEFING)[-1]
    assert ctx["subdomain_familiarity"]["appellation rules"] == {
        "known": 2, "attested": 2, "band": config.DEVELOPING
    }
    assert "viticulture & harvest" in ctx["subdomain_familiarity"], (
        "labels from the first briefing's exposure are already slices"
    )
    assert "subdomain" not in ctx["events"][0], "the event's subdomain is not pre-computed"
    assert "event_subdomain" not in ctx
    _assert_no_reading(ctx)

    by_term = _by_term(scope, group.id)
    assert by_term["hectolitres per hectare"].subdomain == "viticulture & harvest"
    assert by_term["appellation"].subdomain == "appellation rules"
    assert by_term["hectolitres per hectare"].state == config.CONCEPT_UNKNOWN, (
        "a label is not evidence: exposure still promotes nothing"
    )


# Prevents: the router and the reply pitching at the group band only, or
# seeing reading behaviour.
def test_gap_routing_and_thread_reply_contexts_carry_familiarity(judge, stub, world):
    scope, group = world
    stub.register(config.BRIEFING, _briefing_verdict)
    stub.register(config.GAP_ROUTING, lambda ctx: {
        "gap_size": "small", "explanation": "x", "search_focus": "", "reasoning": "stub"
    })
    stub.register(config.THREAD_REPLY, lambda ctx: {
        "answer": "It is the volume-per-area measure.", "source_url": None,
        "terms_used": ["hectolitres per hectare"], "explained_terms": [], "reasoning": "stub",
    })
    assessor = Assessor(judge, reply_search=False)
    _seed_event(scope, group)
    briefing = assessor.raise_topic(scope, group)
    scope.record_reading(briefing.exchange_id, dwell_ms=30_000, scroll_fraction=0.9, source="simulated")

    assessor.respond(scope, group, briefing.exchange_id, "what's hl/ha?")

    (routing,) = _contexts(stub, config.GAP_ROUTING)
    (reply,) = _contexts(stub, config.THREAD_REPLY)
    for ctx in (routing, reply):
        assert isinstance(ctx["subdomain_familiarity"], dict)
        assert "viticulture & harvest" in ctx["subdomain_familiarity"]
        _assert_no_reading(ctx)


# Prevents: the extractor -- the one call that writes to the ledger -- being
# handed anything derived beyond term states, or anything about reading. It
# gets the existing labels (names only) so its labels converge; its labels
# then land on the terms it named, under the churn rule.
def test_close_thread_gives_the_extractor_labels_only_and_applies_its_subdomains(
    judge, stub, world
):
    scope, group = world
    stub.register(config.BRIEFING, _briefing_verdict)
    stub.register(config.CONCEPT_EVIDENCE, lambda ctx: {
        "understood": ["yields"],
        "not_understood": [],
        "asked_about": ["dosage"],
        "already_knew": None,
        "subdomains": [
            {"term": "yields", "subdomain": "yields & quotas"},
            {"term": "dosage", "subdomain": "winemaking"},
        ],
        "reasoning": "stub",
    })
    assessor = Assessor(judge, reply_search=False)
    _seed_event(scope, group)
    briefing = assessor.raise_topic(scope, group)
    scope.record_reading(briefing.exchange_id, dwell_ms=60_000, scroll_fraction=1.0, source="simulated")
    scope.add_turn(briefing.exchange_id, "user", "so yields are up — what's dosage?")
    scope.add_turn(briefing.exchange_id, "system", "Dosage is the sugar added at disgorgement.")

    assessor.close_thread(scope, group, briefing.exchange_id)

    (ctx,) = _contexts(stub, config.CONCEPT_EVIDENCE)
    assert ctx["subdomain_labels"] == ["appellation rules", "viticulture & harvest"]
    assert "subdomain_familiarity" not in ctx, "no counts or bands to the writer call"
    _assert_no_reading(ctx)

    by_term = _by_term(scope, group.id)
    assert by_term["dosage"].state == config.CONCEPT_EXPLAINED
    assert by_term["dosage"].subdomain == "winemaking"
    # `yields` was labelled at exposure and unattested until this thread, so
    # the extractor's label -- riding with the first attesting evidence -- wins.
    assert by_term["yields"].state == config.CONCEPT_PROVISIONAL
    assert by_term["yields"].subdomain == "yields & quotas"


# Prevents: the new field becoming load-bearing. The offline stub extractor
# and briefing (not ours) omit `subdomains`; an older logged verdict lacks it.
# All of that must behave exactly as before: no labels, nothing else changed.
def test_a_verdict_without_subdomains_is_treated_as_no_labels(judge, stub, world):
    scope, group = world
    stub.register(config.BRIEFING, lambda ctx: {
        "briefing": "Plain.", "topic": "t", "terms_used": ["yields"],
        "explained_terms": ["yields"], "reasoning": "stub",
    })
    stub.register(config.CONCEPT_EVIDENCE, lambda ctx: {
        "understood": ["yields"], "not_understood": [], "asked_about": [],
        "already_knew": None, "reasoning": "stub",
    })
    assessor = Assessor(judge, reply_search=False)
    _seed_event(scope, group)
    briefing = assessor.raise_topic(scope, group)
    scope.add_turn(briefing.exchange_id, "user", "yields look fine to me")
    outcome = assessor.close_thread(scope, group, briefing.exchange_id)

    assert outcome.understood == ["yields"]
    assert _by_term(scope, group.id)["yields"].subdomain is None
    assert scope.subdomain_familiarity(group.id) == {
        config.UNLABELLED_SUBDOMAIN: {"known": 0, "attested": 1, "band": config.BEGINNER}
    }
    assert scope.subdomain_labels(group.id) == []

    # The boundary converter accepts every shape and drops junk.
    assert judgments.subdomain_map(None) == {}
    assert judgments.subdomain_map([]) == {}
    assert judgments.subdomain_map("nonsense") == {}
    assert judgments.subdomain_map({"Yields": "viticulture"}) == {"yields": "viticulture"}
    assert judgments.subdomain_map(
        [{"term": " Yields ", "subdomain": "viticulture"}, {"term": "", "subdomain": "x"}, 7]
    ) == {"yields": "viticulture"}


# Prevents: the schema silently carrying a shape the structured-output API
# rejects (a free-form map), or the field being dropped from either call.
def test_subdomains_is_on_the_wire_for_both_labelling_calls():
    for sch in (judgments.CONCEPT_EVIDENCE_SCHEMA, judgments.BRIEFING_SCHEMA):
        field = sch["properties"]["subdomains"]
        assert "subdomains" in sch["required"]
        assert field["type"] == "array"
        item = field["items"]
        assert item["additionalProperties"] is False
        assert set(item["properties"]) == {"term", "subdomain"}
        assert sorted(item["required"]) == ["subdomain", "term"]
    # Nothing in any schema is an open map: strict mode allows only `false`.
    def walk(node):
        if isinstance(node, dict):
            if "additionalProperties" in node:
                assert node["additionalProperties"] is False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    for sch in (
        judgments.CONCEPT_EVIDENCE_SCHEMA,
        judgments.BRIEFING_SCHEMA,
        judgments.THREAD_REPLY_SCHEMA,
        judgments.GAP_ROUTING_SCHEMA,
    ):
        walk(sch)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


# Prevents: a live store created before the column existed silently rejecting
# labels (the INSERT ... DO UPDATE would fail on the UPDATE). The additive
# migration must add it, existing rows read back unlabelled, and labelling
# then works.
def test_migration_adds_the_subdomain_column_to_an_old_db(tmp_path):
    import sqlite3

    from conversational_agent import db as dbmod
    from conversational_agent.store import Store

    old_schema = dbmod.SCHEMA.replace("    subdomain      TEXT,\n", "")
    assert "subdomain" not in old_schema.split("CREATE TABLE IF NOT EXISTS concepts")[1].split(");")[0]

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(old_schema)
    old.execute("INSERT INTO users (id, display_name, kind, created_at) VALUES ('u1', 'x', 'real', 't')")
    old.execute(
        "INSERT INTO groups (id, user_id, name, description, created_at, poll_interval_minutes)"
        " VALUES ('g1', 'u1', 'wine', 'd', 't', 60)"
    )
    old.execute(
        "INSERT INTO concepts (user_id, group_id, term, state, exposure_count, correct_uses,"
        " misunderstandings, first_seen_at, last_seen_at) VALUES ('u1', 'g1', 'lees', 'confirmed', 3, 2, 0, 't', 't')"
    )
    old.commit()
    cols = {r[1] for r in old.execute("PRAGMA table_info(concepts)").fetchall()}
    assert "subdomain" not in cols
    old.close()

    store = Store(path)
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(concepts)").fetchall()}
    assert "subdomain" in cols
    scope = store.scope("u1")
    (lees,) = scope.ledger("g1")
    assert lees.subdomain is None
    assert scope.subdomain_familiarity("g1") == {
        config.UNLABELLED_SUBDOMAIN: {"known": 1, "attested": 1, "band": config.BEGINNER}
    }
    scope.mark_explained("g1", ["dosage"], subdomains={"dosage": "winemaking"})
    assert _by_term(scope, "g1")["dosage"].subdomain == "winemaking"
    store.close()
