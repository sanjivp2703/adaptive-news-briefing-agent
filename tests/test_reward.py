"""The reward scores a briefing for its reader without any reference to the teacher."""

from __future__ import annotations

from training import reward as rw

EVENT = {"headline": "City win the derby 2-1", "detail": "Haaland scored twice; the winner came in the 88th minute."}


def _packet(band="beginner", known=(), unknown=("offside", "var"), familiarity=None):
    return {
        "proficiency": band,
        "concept_ledger": {"unknown": list(unknown), "familiar": list(known), "explained": [], "confirmed": [], "provisional": []},
        "subdomain_familiarity": familiarity or {},
        "events": [EVENT],
    }


def _briefing(explained, used=("offside", "var"), text=None, labels=None):
    text = text or ("City beat United 2-1. Haaland scored twice, the second in the 88th minute. "
                    "Offside, meaning an attacker was beyond the last defender when the ball was played, was checked by VAR.")
    return {"briefing": text, "topic": "derby", "terms_used": list(used), "explained_terms": list(explained),
            "subdomains": [{"term": t, "subdomain": labels.get(t, "rules")} for t in used] if labels else []}


def test_a_rule_breaker_scores_zero_overall():
    bad = _briefing(["offside"], text="City beat United 2-1. The fee was 300 million pounds.")
    s = rw.score_briefing(_packet(), bad, [], draws=8)
    assert s.rules_passed is False and s.composite == 0.0 and "numbers_supported" in s.rule_failures


def test_defining_what_a_beginner_lacks_beats_defining_nothing():
    full = rw.score_briefing(_packet(), _briefing(["offside", "var"]), [], draws=16)
    none = rw.score_briefing(_packet(), _briefing([]), [], draws=16)
    assert full.rules_passed and none.rules_passed
    assert full.coverage > none.coverage and full.composite > none.composite
    assert none.coverage == 0.0


def test_an_expert_is_charged_for_being_told_what_they_hold():
    # Terms not in the ledger, in a subdomain the reader is fluent in: probably held.
    fam = {"rules": {"known": 9, "attested": 10, "band": "fluent"}}
    expert = _packet(band="fluent", unknown=(), familiarity=fam)
    glossy = rw.score_briefing(expert, _briefing(["offside", "var"], labels={"offside": "rules", "var": "rules"}), [], draws=24)
    quiet = rw.score_briefing(expert, _briefing([], labels={"offside": "rules", "var": "rules"}), [], draws=24)
    assert glossy.overgloss > 0.7
    assert quiet.overgloss == 0.0
    assert quiet.learning >= glossy.learning


def test_fit_rewards_the_band_and_a_sane_length():
    simple = "City beat United. Haaland scored twice. The second goal came late. United's manager was angry."
    dense = ("Notwithstanding the officiating controversy precipitated by the video assistant referee's non-intervention, "
             "Manchester City's subsequent consolidation of a two-one advantage reflected a sustained territorial superiority.")
    fit_simple, _ = rw.fit_score(_packet("beginner"), {"briefing": simple * 3})
    fit_dense, _ = rw.fit_score(_packet("beginner"), {"briefing": dense * 3})
    assert fit_simple > fit_dense
    short, _ = rw.fit_score(_packet("beginner"), {"briefing": "City won."})
    assert short < 1.0


# Prevents: a briefing scoring full coverage by naming no terms. The universe
# is shared across the briefings being compared, and a defined term only
# counts when the text contains it.
def test_coverage_is_measured_over_a_shared_universe_and_only_for_terms_in_the_text():
    packet = _packet()
    full = _briefing(["offside", "var"])
    silent = {"briefing": "City beat United 2-1. Haaland scored twice.", "topic": "derby", "terms_used": [], "explained_terms": [], "subdomains": []}
    universe = rw.event_vocabulary(packet, [full, silent])
    assert set(universe) >= {"offside", "var"}
    s_full = rw.score_briefing(packet, full, [], draws=12, universe_terms=universe)
    s_silent = rw.score_briefing(packet, silent, [], draws=12, universe_terms=universe)
    assert s_silent.coverage == 0.0 and s_full.coverage > 0.15
    claims = dict(full, briefing="City beat United 2-1. Haaland scored twice.")  # claims to define terms it never mentions
    s_claims = rw.score_briefing(packet, claims, [], draws=12, universe_terms=universe)
    assert s_claims.coverage == 0.0
