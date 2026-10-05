"""The concept ledger: what the system believes, and what it took to believe it.

This replaces the old 0-100 level and its decay model. The whole redesign
turns on four claims, and each one has a specific way of going wrong:

  * only something the USER did is evidence -- our own output never is;
  * weak evidence must never overwrite strong evidence;
  * strong evidence must still be revocable, or a wrong belief calcifies;
  * proficiency is derived on read, so it can never drift from the ledger.

Falling behind is the fifth: it is a count of unshown events, not a decayed
number, which is what makes it checkable rather than modelled.

The first claim used to be weaker -- three unquestioned exposures promoted a
term to `assumed`. The persona harness measured that inference against ground
truth and it was anti-predictive at every threshold tried (lift -0.34 at one
exposure, -1.00 at three and five); split by provenance, `assumed` from silence
scored 0/2 while `assumed` from one correct use scored 9/9. So the state was
deleted rather than re-tuned, and `provisional` -- reachable only from a
correct use -- took its place.
"""

from __future__ import annotations

import pytest
from conftest import expose, known_terms

from conversational_agent import config


@pytest.fixture
def group_scope(store):
    user = store.create_user("ledger-user", kind="real")
    scope = store.scope(user.id)
    return scope, scope.create_group("wine tasting")


# Prevents: promotion-from-silence coming back under a new threshold. Not
# asking may mean they knew it, skimmed, inferred it, or didn't want to
# interrupt -- and question-asking runs ~0.11/student/hour regardless of
# understanding (Graesser & Person 1994), so silence is the likely outcome
# under both hypotheses and separates neither. No number of exposures may buy
# anything, which is why this asserts at a count far past any old threshold.
def test_note_exposure_never_promotes_a_term_however_many_times_it_fires(group_scope):
    scope, group = group_scope

    promoted = expose(scope, group.id, "malolactic", 10)

    assert promoted == [], "note_exposure must return [] -- it promotes nothing"
    assert scope.concept_state(group.id, "malolactic") == config.CONCEPT_UNKNOWN
    assert known_terms(scope, group.id) == set()
    assert scope.ledger(group.id)[0].is_known is False
    # The state that exposure used to reach is gone from the model too, so the
    # old rule cannot be resurrected by writing the state directly.
    assert set(config.CONCEPT_STATES) == {
        config.CONCEPT_UNKNOWN,
        config.CONCEPT_PROVISIONAL,
        config.CONCEPT_FAMILIAR,
        config.CONCEPT_EXPLAINED,
        config.CONCEPT_CONFIRMED,
    }
    # `familiar` is NOT `assumed` under another name: it is reached only by
    # `mark_read_explanation`, which needs an explanation to have been given
    # and the text to have been read. Exposure alone still cannot get there.
    assert scope.concept_state(group.id, "malolactic") != config.CONCEPT_FAMILIAR


# Prevents: the exposure counter being ripped out along with the inference it
# used to feed. The count has a second, still-valid job -- it tells the briefing
# whether a term has been put in front of this user before, which is a fact
# about our own output and safe to use for phrasing. Only its inferential
# weight was removed.
def test_note_exposure_still_counts_exposures_it_only_stops_inferring_from_them(
    group_scope,
):
    scope, group = group_scope

    expose(scope, group.id, "brett", 3)
    entry = scope.ledger(group.id)[0]

    assert entry.term == "brett"
    assert entry.exposure_count == 3, "the count itself must survive the redesign"
    assert entry.state == config.CONCEPT_UNKNOWN

    scope.note_exposure(group.id, ["brett", "phylloxera"])
    counts = {c.term: c.exposure_count for c in scope.ledger(group.id)}
    assert counts == {"brett": 4, "phylloxera": 1}


# Prevents: one correct use being treated as mastery. A single unprompted use
# sits near a 0.43 posterior under standard guess/slip assumptions, and there
# is a specific confound here -- the briefing just used the term, so echoing it
# back may be parroting rather than knowing. Two independent uses required.
def test_one_correct_use_lands_provisional_and_a_second_confirms(group_scope):
    scope, group = group_scope

    first = scope.mark_correct_use(group.id, ["terroir"])

    assert first == [], "one use confirms nothing"
    assert scope.concept_state(group.id, "terroir") == config.CONCEPT_PROVISIONAL

    confirmed = scope.mark_correct_use(group.id, ["terroir"])

    assert confirmed == ["terroir"]
    assert scope.concept_state(group.id, "terroir") == config.CONCEPT_CONFIRMED


# Prevents: `provisional` being re-glossed in every briefing forever, or --
# the opposite and worse failure -- counting toward the band on the strength of
# a single use we may have prompted ourselves. It is deliberately known-enough
# to stop explaining and not known-enough to vote.
def test_provisional_suppresses_re_glossing_but_does_not_move_the_band(group_scope):
    scope, group = group_scope

    for term in ("tannin", "vintage", "terroir", "brett"):
        scope.mark_correct_use(group.id, [term])

    assert all(
        scope.concept_state(group.id, t) == config.CONCEPT_PROVISIONAL
        for t in ("tannin", "vintage", "terroir", "brett")
    )
    assert known_terms(scope, group.id) == {"tannin", "vintage", "terroir", "brett"}
    assert config.CONCEPT_PROVISIONAL in config.KNOWN_STATES
    assert config.CONCEPT_PROVISIONAL not in config.BAND_STATES
    assert scope.proficiency(group.id) == config.BEGINNER, (
        "four single uses are four 0.43 posteriors, not a developing speaker"
    )

    # The states that DO carry the band, over the same four attested terms.
    # `confirmed` counts at once. `explained` counts only after the second
    # explanation they read (checkpoint 3: familiarity "over time grows into
    # more confidence"), so one ask leaves the band where it was ...
    scope.mark_explained(group.id, ["tannin", "vintage"])
    scope.mark_correct_use(group.id, ["terroir"])

    assert scope.concept_state(group.id, "terroir") == config.CONCEPT_CONFIRMED
    assert scope.proficiency(group.id) == config.proficiency_band(1, 4)
    assert scope.proficiency(group.id) == config.BEGINNER

    # ... and a later read gloss of the same two terms is what moves it.
    scope.mark_read_explanation(group.id, ["tannin", "vintage"])
    assert scope.proficiency(group.id) == config.proficiency_band(3, 4)
    assert scope.proficiency(group.id) == config.DEVELOPING


# Prevents: the weakest signal in the system silently overwriting the strongest
# ones. Mentioning a term again after explaining it -- which is exactly what a
# follow-up briefing does -- must not demote "we explained this to them" or
# "they used it correctly" back down the ladder.
def test_exposure_never_downgrades_a_term_with_stronger_evidence(group_scope):
    scope, group = group_scope
    scope.mark_explained(group.id, ["tannin"], evidence="they asked what it meant")
    for _ in range(config.CORRECT_USES_BEFORE_CONFIRMED):
        scope.mark_correct_use(group.id, ["vintage"], evidence="used it correctly")
    scope.mark_correct_use(group.id, ["brett"], evidence="used it correctly once")

    for term in ("tannin", "vintage", "brett"):
        assert expose(scope, group.id, term, 5) == []

    assert scope.concept_state(group.id, "tannin") == config.CONCEPT_EXPLAINED
    assert scope.concept_state(group.id, "vintage") == config.CONCEPT_CONFIRMED
    assert scope.concept_state(group.id, "brett") == config.CONCEPT_PROVISIONAL
    # The exposures are still counted -- they are real observations of our own
    # output -- they just cannot move the state or rewrite the evidence.
    by_term = {c.term: c for c in scope.ledger(group.id)}
    assert by_term["tannin"].exposure_count == 5
    assert by_term["tannin"].evidence == "they asked what it meant"


# Prevents: a wrong belief calcifying. `confirmed` can still come from a misread
# reply, so if the strongest state cannot be revoked the system keeps using a
# term the user does not follow and has no way back. Resetting the use count
# matters as much as the state: leaving it at 2 would let the very next use
# snap the term back to confirmed on evidence we just learned to distrust.
def test_a_revealed_misunderstanding_reverts_confirmed_and_costs_two_fresh_uses(
    group_scope,
):
    scope, group = group_scope
    for _ in range(config.CORRECT_USES_BEFORE_CONFIRMED):
        scope.mark_correct_use(group.id, ["chaptalisation"])
    scope.mark_explained(group.id, ["sulphites"])
    expose(scope, group.id, "brett", 4)
    assert known_terms(scope, group.id) == {"chaptalisation", "sulphites"}

    scope.mark_unknown(
        group.id,
        ["chaptalisation", "sulphites", "brett"],
        evidence="called them all grape varieties",
    )

    assert known_terms(scope, group.id) == set()
    for term in ("chaptalisation", "sulphites", "brett"):
        assert scope.concept_state(group.id, term) == config.CONCEPT_UNKNOWN
    entries = {c.term: c for c in scope.ledger(group.id)}
    assert entries["chaptalisation"].evidence == "called them all grape varieties"

    # The climb back is the full two uses, not one -- which is the observable
    # proof that `correct_uses` was reset and not merely the state.
    assert scope.mark_correct_use(group.id, ["chaptalisation"]) == []
    assert scope.concept_state(group.id, "chaptalisation") == config.CONCEPT_PROVISIONAL
    assert scope.mark_correct_use(group.id, ["chaptalisation"]) == ["chaptalisation"]
    assert scope.concept_state(group.id, "chaptalisation") == config.CONCEPT_CONFIRMED


# Prevents: proficiency drifting away from the evidence -- the failure the old
# stored-and-decayed level had built in. It must be recomputed from the ledger
# every time, and it must need BOTH an absolute floor and a ratio: three terms
# met and all three known is a beginner who has been told three things, not a
# fluent speaker, and calling them fluent is how the system starts skipping
# explanations they need.
def test_proficiency_is_derived_and_needs_both_a_floor_and_a_ratio(group_scope):
    scope, group = group_scope

    assert scope.proficiency(group.id) == config.BEGINNER, "empty ledger is beginner"

    for _ in range(config.CORRECT_USES_BEFORE_CONFIRMED):
        scope.mark_correct_use(group.id, ["tannin", "vintage", "terroir"])

    assert len(scope.ledger(group.id)) == 3
    assert known_terms(scope, group.id) == {"tannin", "vintage", "terroir"}
    # Three known of three encountered is a perfect ratio, and must NOT read
    # as fluent -- the absolute floor is what stops a three-term ledger from
    # convincing the system it can stop explaining things.
    assert scope.proficiency(group.id) == config.DEVELOPING
    assert config.proficiency_band(known=3, encountered=3) == config.DEVELOPING
    assert config.proficiency_band(known=2, encountered=2) == config.BEGINNER
    assert config.proficiency_band(known=30, encountered=30) == config.FLUENT
    # And the ratio bites independently: plenty known, but mostly not.
    assert config.proficiency_band(known=30, encountered=200) == config.BEGINNER
    assert config.proficiency_band(known=0, encountered=0) == config.BEGINNER

    # Nothing was stored: reverting the evidence moves the band straight back.
    scope.mark_unknown(group.id, ["tannin", "vintage", "terroir"])
    assert scope.proficiency(group.id) == config.BEGINNER


# Prevents: "how far behind are you" going back to a modelled number. It is a
# count of specific material events this user has not been shown -- so an
# immaterial event, an already-surfaced one, and another group's event must all
# be excluded, and the count must drop the moment something is actually shown.
def test_behind_count_counts_exactly_the_material_unsurfaced_events(group_scope):
    scope, group = group_scope
    other = scope.create_group("NBA fans")

    assert scope.behind_count(group.id) == 0

    unjudged = scope.record_event(group.id, headline="not yet judged")
    immaterial = scope.record_event(group.id, headline="a barrel was cleaned")
    scope.apply_materiality(immaterial, is_material=False, score=2.0, reason="noise")
    first = scope.record_event(group.id, headline="2024 Bordeaux declared")
    scope.apply_materiality(first, is_material=True, score=91.0, reason="big")
    second = scope.record_event(group.id, headline="famous estate sold")
    scope.apply_materiality(second, is_material=True, score=88.0, reason="big")
    elsewhere = scope.record_event(other.id, headline="star guard traded")
    scope.apply_materiality(elsewhere, is_material=True, score=95.0, reason="big")

    assert scope.behind_count(group.id) == 2, "only material, unsurfaced, this group"
    assert scope.behind_count(other.id) == 1
    assert scope.get_event(unjudged).is_material is None

    scope.mark_surfaced([first])

    assert scope.behind_count(group.id) == 1
    assert [e.id for e in scope.pending_events(group.id)] == [second]

    scope.mark_surfaced([second])
    assert scope.behind_count(group.id) == 0


# Prevents THE SILENCE SPIRAL, the highest-risk failure the literature review
# identified. Terms we merely said in front of the user are excluded from BOTH
# sides of the band's ratio, and the symmetry is the point: counting them as
# known lets the system talk its way into believing it can stop explaining;
# counting them as encountered-but-unknown lets it talk its way into the
# opposite. Either way the band would move for reasons that have nothing to do
# with the user.
def test_terms_the_user_never_responded_to_do_not_move_the_band_either_way(store):
    scope = store.scope(store.create_user("Ada").id)
    group = scope.create_group("Wine")

    for term in [f"term{i}" for i in range(14)]:
        expose(scope, group.id, term, 3)

    assert all(c.state == config.CONCEPT_UNKNOWN for c in scope.ledger(group.id))
    assert scope.proficiency(group.id) == config.BEGINNER, (
        "talking at someone must never raise their band"
    )

    # And the mirror: silence must not DEPRESS the band either. (Confirmed
    # uses rather than asks: one ask no longer moves the band by itself.)
    for _ in range(config.CORRECT_USES_BEFORE_CONFIRMED):
        scope.mark_correct_use(group.id, ["a", "b", "c"])
    with_silence = scope.proficiency(group.id)

    other = store.scope(store.create_user("Bo").id)
    other_group = other.create_group("Wine")
    for _ in range(config.CORRECT_USES_BEFORE_CONFIRMED):
        other.mark_correct_use(other_group.id, ["a", "b", "c"])

    assert with_silence == other.proficiency(other_group.id), (
        "14 unanswered mentions changed the band; silence must get no vote"
    )
    assert with_silence == config.DEVELOPING


# ---------------------------------------------------------------------------
# `familiar`: the one door from reading behaviour into the ledger.
#
# Human decision, checkpoint 3: "we explained and they read or skimmed should
# count as they have familiarity; over time this will grow into more
# confidence. One correct use should stop from re-explaining a term."
# ---------------------------------------------------------------------------


# Prevents: reading alone moving anything. `record_reading` still writes the
# exchange and nowhere else; the credit happens at close, and only for terms
# the briefing defined.
def test_record_reading_alone_moves_nothing(group_scope):
    scope, group = group_scope
    eid = scope.open_exchange(
        group.id, "Malolactic is the softening step.", explained_terms=["malolactic"]
    )
    scope.record_reading(eid, dwell_ms=60_000, scroll_fraction=1.0, source="simulated")

    assert scope.get_exchange(eid).read_quality in ("read", "studied")
    assert scope.ledger(group.id) == []
    assert scope.concept_state(group.id, "malolactic") == config.CONCEPT_UNKNOWN


# Prevents: a read explanation counting as more than it is. One lands the term
# at `familiar` (no more re-glossing) with read_explanations == 1, and the band
# does not move -- it needs READ_EXPLANATIONS_BEFORE_BAND.
def test_one_read_explanation_lands_familiar_and_stops_glossing_only(group_scope):
    scope, group = group_scope

    promoted = scope.mark_read_explanation(group.id, ["malolactic"])

    assert promoted == ["malolactic"]
    concept = scope.ledger(group.id)[0]
    assert concept.state == config.CONCEPT_FAMILIAR
    assert concept.read_explanations == 1
    assert concept.is_known is True, "one read definition stops re-glossing"
    assert concept.counts_toward_band is False
    assert config.READ_EXPLANATIONS_BEFORE_BAND == 2
    assert scope.proficiency(group.id) == config.BEGINNER


# Prevents: `familiar` never reaching the band ("over time this will grow into
# more confidence" is the decision), and reaching it too early. Three terms
# each explained-and-read twice clear the band floor; the same three read once
# do not.
def test_repeated_read_explanations_grow_into_the_band(group_scope):
    scope, group = group_scope
    terms = ["malolactic", "lees", "tannin"]

    scope.mark_read_explanation(group.id, terms)
    assert scope.proficiency(group.id) == config.BEGINNER
    assert all(not c.counts_toward_band for c in scope.ledger(group.id))

    scope.mark_read_explanation(group.id, terms)
    assert all(c.read_explanations == 2 for c in scope.ledger(group.id))
    assert all(c.counts_toward_band for c in scope.ledger(group.id))
    assert scope.proficiency(group.id) != config.BEGINNER


# Prevents: asking about a term not counting as reading its explanation. The
# person who asked wanted the answer -- that is the strongest attention there
# is -- so an ask plus one later read gloss reaches the band threshold.
def test_asking_counts_as_a_read_explanation(group_scope):
    scope, group = group_scope

    scope.mark_explained(group.id, ["derogation"])
    concept = scope.ledger(group.id)[0]
    assert concept.state == config.CONCEPT_EXPLAINED
    assert concept.read_explanations == 1
    assert concept.counts_toward_band is False

    scope.mark_read_explanation(group.id, ["derogation"])
    concept = scope.ledger(group.id)[0]
    assert concept.state == config.CONCEPT_EXPLAINED, "stronger state is kept"
    assert concept.read_explanations == 2
    assert concept.counts_toward_band is True


# Prevents: a read explanation downgrading stronger evidence, or a
# misunderstanding leaving the read count behind. Provisional stays
# provisional under a read gloss; a revealed miss zeroes read_explanations.
def test_read_explanation_never_downgrades_and_a_miss_resets_it(group_scope):
    scope, group = group_scope

    scope.mark_correct_use(group.id, ["yields"])
    scope.mark_read_explanation(group.id, ["yields"])
    concept = scope.ledger(group.id)[0]
    assert concept.state == config.CONCEPT_PROVISIONAL
    assert concept.read_explanations == 1

    scope.mark_read_explanation(group.id, ["yields"])
    scope.mark_unknown(group.id, ["yields"])
    concept = scope.ledger(group.id)[0]
    assert concept.state == config.CONCEPT_UNKNOWN
    assert concept.read_explanations == 0
    assert concept.correct_uses == 0


# Prevents: the band moving on a single familiar term in EITHER direction. A
# term read once is neither attested nor known; it votes for nothing.
def test_a_single_familiar_term_votes_neither_way(group_scope):
    scope, group = group_scope
    # Three confirmed terms out of three attested: developing at least.
    for _ in range(config.CORRECT_USES_BEFORE_CONFIRMED):
        scope.mark_correct_use(group.id, ["a", "b", "c"])
    before = scope.proficiency(group.id)
    assert before != config.BEGINNER

    scope.mark_read_explanation(group.id, ["d", "e", "f", "g", "h", "i"])
    assert scope.proficiency(group.id) == before, (
        "six familiar-once terms must not dilute the ratio"
    )


# Prevents: an old database (CHECK constraint without 'familiar') silently
# rejecting the new state, or the table rebuild losing rows or the index.
def test_migration_rebuilds_a_pre_familiar_concepts_table(tmp_path):
    import sqlite3

    from conversational_agent import db as dbmod
    from conversational_agent.store import Store

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        dbmod.SCHEMA.replace(
            "CHECK (state IN ('unknown', 'provisional', 'familiar',\n"
            "                                    'explained', 'confirmed'))",
            "CHECK (state IN ('unknown', 'provisional', 'explained', 'confirmed'))",
        )
    )
    old.execute(
        "INSERT INTO users (id, display_name, kind, created_at)"
        " VALUES ('u1', 'x', 'real', 't')"
    )
    old.execute(
        "INSERT INTO groups (id, user_id, name, description, created_at,"
        " poll_interval_minutes) VALUES ('g1', 'u1', 'wine', 'd', 't', 60)"
    )
    old.execute(
        "INSERT INTO concepts (user_id, group_id, term, state, exposure_count,"
        " correct_uses, misunderstandings, first_seen_at, last_seen_at)"
        " VALUES ('u1', 'g1', 'lees', 'confirmed', 3, 2, 0, 't', 't')"
    )
    old.commit()
    old.close()
    ddl = sqlite3.connect(path).execute(
        "SELECT sql FROM sqlite_master WHERE name = 'concepts'"
    ).fetchone()[0]
    assert "'familiar'" not in ddl

    store = Store(path)
    scope = store.scope("u1")
    assert [c.term for c in scope.ledger("g1")] == ["lees"]
    assert scope.ledger("g1")[0].state == config.CONCEPT_CONFIRMED
    scope.mark_read_explanation("g1", ["tannin"])  # would raise on the old CHECK
    assert scope.concept_state("g1", "tannin") == config.CONCEPT_FAMILIAR
    names = {
        r["name"]
        for r in store.conn.execute(
            "PRAGMA index_list(concepts)"
        ).fetchall()
    }
    assert "idx_concepts_state" in names
    store.close()


# Prevents: the live merge of `series a` into `series b`. Containment on
# content tokens treats the round letter as noise, so every lettered round
# collapsed into whichever one the ledger saw first. A short token present on
# only one side is a distinction, not a longer form.
def test_lettered_rounds_stay_distinct_concepts(group_scope):
    scope, group = group_scope
    scope.mark_read_explanation(group.id, ["series b"])
    scope.mark_explained(group.id, ["series c"])
    scope.mark_correct_use(group.id, ["series a"])

    terms = sorted(c.term for c in scope.ledger(group.id))
    assert terms == ["series a", "series b", "series c"]
    # The rule that made this necessary still holds for real longer forms.
    scope.mark_correct_use(group.id, ["run rate"])
    assert scope.canonical_term(group.id, "annualized revenue run rate") == "run rate"


# ---------------------------------------------------------------------------
# Reading acts: attention, and what a skip meant.
# ---------------------------------------------------------------------------

from conversational_agent.store import attention_score


# Prevents: the continuous readout disagreeing with the buckets it sits beside.
def test_attention_score_is_monotone_and_bounded():
    chars = 800
    assert attention_score(None, None, chars) is None
    skipped = attention_score(2_000, 0.2, chars)
    skimmed = attention_score(8_000, 0.5, chars)
    read = attention_score(40_000, 0.9, chars)
    studied = attention_score(120_000, 1.0, chars)
    assert 0 <= skipped < skimmed < read <= studied <= 10
    assert attention_score(10_000, None, chars) is not None
    assert attention_score(None, 1.0, chars) == 10.0


# Prevents: a skip writing anything to the ledger, or being classified from
# nothing. A skip is `unresolved` until evidence arrives; evidence about the
# terms the briefing defined labels it; unrelated evidence leaves it alone.
def test_a_skip_is_unresolved_until_evidence_labels_it(group_scope):
    scope, group = group_scope
    eid = scope.open_exchange(
        group.id, "Malolactic is the softening step.", explained_terms=["malolactic"]
    )
    scope.record_reading(eid, dwell_ms=1_500, scroll_fraction=0.1, source="simulated")
    x = scope.get_exchange(eid)
    assert x.read_quality == "skipped" and x.skip_kind == "unresolved"
    assert scope.ledger(group.id) == [], "a skip writes nothing to the ledger"
    assert scope.reading_pattern(group.id)["p_informed"] == 0.5

    # Evidence about a different term: still unresolved.
    assert scope.resolve_skips(group.id, known_now=["tannin"], lacking_now=[]) == {}
    assert scope.get_exchange(eid).skip_kind == "unresolved"

    # They later used the term correctly: the skip was informed.
    assert scope.resolve_skips(group.id, known_now=["malolactic"], lacking_now=[]) == {
        eid: "informed"
    }
    assert scope.reading_pattern(group.id)["informed_skips"] == 1


# Prevents: the human's rule being lost -- skipping and then asking about the
# subject means the skip was lazy, whatever else they knew. Negative evidence
# outranks positive on the same skip.
def test_skip_then_asking_about_it_is_a_lazy_skip(group_scope):
    scope, group = group_scope
    eid = scope.open_exchange(
        group.id, "text", explained_terms=["potential alcohol", "derogation"]
    )
    scope.record_reading(eid, dwell_ms=1_000, scroll_fraction=0.1, source="simulated")
    out = scope.resolve_skips(
        group.id, known_now=["derogation"], lacking_now=["potential alcohol"]
    )
    assert out == {eid: "lazy"}
    pattern = scope.reading_pattern(group.id)
    assert pattern["lazy_skips"] == 1 and pattern["p_informed"] < 0.5


# Prevents: a skip of material the user had ALREADY shown they hold waiting
# for evidence that already exists -- and, the other way, `familiar` (which
# came from reading) vouching for a skip.
def test_prior_attested_evidence_resolves_a_skip_at_once_but_familiar_does_not(
    group_scope,
):
    scope, group = group_scope
    scope.mark_correct_use(group.id, ["yields"])
    e1 = scope.open_exchange(group.id, "t", explained_terms=["yields"])
    scope.record_reading(e1, dwell_ms=1_000, scroll_fraction=0.1, source="simulated")
    assert scope.get_exchange(e1).skip_kind == "informed"

    scope.mark_read_explanation(group.id, ["budburst"])
    e2 = scope.open_exchange(group.id, "t", explained_terms=["budburst"])
    scope.record_reading(e2, dwell_ms=1_000, scroll_fraction=0.1, source="simulated")
    assert scope.get_exchange(e2).skip_kind == "unresolved"


# Prevents: a rebuild that stopped half way stranding the ledger. If the old
# table was renamed aside and the copy never finished, the next open must
# finish it -- and a row in a state the schema no longer has (the deleted
# `assumed`) must come across as `unknown`, not abort the copy.
def test_an_interrupted_concepts_rebuild_is_finished_on_the_next_open(tmp_path):
    import sqlite3

    from conversational_agent import db as dbmod
    from conversational_agent.store import Store

    path = tmp_path / "stranded.db"
    conn = sqlite3.connect(path)
    conn.executescript(dbmod.SCHEMA)
    conn.execute(
        "INSERT INTO users (id, display_name, kind, created_at) VALUES ('u1', 'x', 'real', 't')"
    )
    conn.execute(
        "INSERT INTO groups (id, user_id, name, description, created_at,"
        " poll_interval_minutes) VALUES ('g1', 'u1', 'wine', 'd', 't', 60)"
    )
    # The state a failed rebuild leaves behind: the current `concepts` exists
    # and is empty, and the data sits in `concepts_old` under the old rules.
    conn.execute(
        "CREATE TABLE concepts_old (user_id TEXT, group_id TEXT, term TEXT, state TEXT,"
        " exposure_count INTEGER, correct_uses INTEGER, misunderstandings INTEGER,"
        " evidence TEXT, first_seen_at TEXT, last_seen_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO concepts_old VALUES ('u1', 'g1', ?, ?, 1, 0, 0, NULL, 't', 't')",
        [("lees", "confirmed"), ("tannin", "assumed")],
    )
    conn.commit()
    conn.close()

    store = Store(path)
    states = {c.term: c.state for c in store.scope("u1").ledger("g1")}
    assert states == {"lees": config.CONCEPT_CONFIRMED, "tannin": config.CONCEPT_UNKNOWN}
    leftover = store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'concepts_old'"
    ).fetchone()
    assert leftover is None
    store.close()


# Prevents: promotion-from-silence returning as a re-tuned threshold. It was
# measured anti-predictive at every exposure threshold tried and deleted, so
# neither the state nor a threshold constant may reappear; and one correct use
# (with its parroting confound) must not move the proficiency band.
def test_the_deleted_silence_inference_has_not_come_back():
    assert not hasattr(config, "CONCEPT_ASSUMED")
    assert not hasattr(config, "EXPOSURES_BEFORE_ASSUMED")
    assert config.CONCEPT_PROVISIONAL not in config.BAND_STATES
