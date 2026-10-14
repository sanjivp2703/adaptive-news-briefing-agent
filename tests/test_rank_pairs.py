"""Preference pairs are decided by the hard rules first, then a consistent judge.

Offline throughout: candidates are built here and the judge is a function.
"""

from __future__ import annotations

import json
import random

from training import rank_pairs as rp

PACKET = {
    "group": {"name": "Premier League", "description": "English football"},
    "proficiency": "beginner",
    "concept_ledger": {"unknown": [], "familiar": ["var"], "explained": [], "confirmed": [], "provisional": []},
    "events": [{"headline": "City win the derby 2-1 after a VAR review", "detail": "Haaland scored twice; the winner came in the 88th minute."}],
}
GOOD = {
    "briefing": "City beat United 2-1. Haaland scored twice, the second in the 88th minute. "
    "Offside, meaning an attacker was beyond the last defender when the ball was played, was checked by VAR.",
    "topic": "Manchester derby", "explained_terms": ["offside"], "terms_used": ["offside", "var"], "reasoning": "x",
}
SHORT = dict(GOOD, briefing="City beat United 2-1. Haaland scored twice, the second in the 88th minute.", explained_terms=[], terms_used=["var"])
BAD_NUMBER = dict(GOOD, briefing=GOOD["briefing"] + " The fee was 300 million pounds.")
REGLOSS = dict(GOOD, explained_terms=["offside", "var"])


def _row(rid: str, profile: str = "beginner") -> dict:
    return {
        "messages": [
            {"role": "system", "content": "Write the briefing."},
            {"role": "user", "content": json.dumps({"_judgment_point": "briefing", "context": PACKET})},
            {"role": "assistant", "content": json.dumps(GOOD)},
        ],
        "meta": {"id": rid, "profile": profile, "group_family": "Premier League"},
    }


def _judge_prefers(text_contains: str):
    """A judge that always prefers the candidate containing a substring, in either order."""

    def judge(packet, a, b):
        return {"winner": "A" if text_contains in a else "B", "margin": "clear", "reasoning": "stub"}

    return judge


def test_a_rule_breaker_loses_without_consulting_the_judge():
    calls = []

    def judge(packet, a, b):
        calls.append(1)
        return {"winner": "A", "margin": "clear", "reasoning": "x"}

    pair = rp.decide(PACKET, [json.dumps(BAD_NUMBER), json.dumps(GOOD)], [], judge, random.Random(1))
    assert pair["decided_by"] == "rules"
    assert json.loads(pair["chosen"]) == GOOD
    assert "numbers_supported" in pair["reasoning"]
    assert calls == []

    pair = rp.decide(PACKET, [json.dumps(GOOD), json.dumps(REGLOSS)], [], judge, random.Random(1))
    assert json.loads(pair["chosen"]) == GOOD and "no_regloss" in pair["reasoning"]


def test_two_rule_breakers_make_no_pair():
    assert rp.decide(PACKET, [json.dumps(BAD_NUMBER), json.dumps(REGLOSS)], [], None, random.Random(1)) is None
    assert rp.decide(PACKET, ["not json", "also not json"], [], None, random.Random(1)) is None


def test_the_judge_decides_only_when_both_pass_and_agrees_with_itself():
    pair = rp.decide(PACKET, [json.dumps(SHORT), json.dumps(GOOD)], [], _judge_prefers("Offside"), random.Random(1))
    assert pair["decided_by"] == "judge" and json.loads(pair["chosen"]) == GOOD and pair["margin"] == "clear"
    # Same candidates the other way round: the same briefing must still win.
    pair = rp.decide(PACKET, [json.dumps(GOOD), json.dumps(SHORT)], [], _judge_prefers("Offside"), random.Random(1))
    assert json.loads(pair["chosen"]) == GOOD


# Prevents: a judge whose verdict flips with the order of presentation being
# trusted. Position bias is the classic failure of pairwise LLM judging.
def test_an_order_dependent_judge_is_thrown_out():
    always_a = lambda packet, a, b: {"winner": "A", "margin": "clear", "reasoning": "first one"}
    assert rp.decide(PACKET, [json.dumps(SHORT), json.dumps(GOOD)], [], always_a, random.Random(1)) is None


def test_the_run_writes_pairs_resumes_and_reports(tmp_path):
    rows = [_row("p1"), _row("p2"), _row("p3", "expert"), _row("p4")]
    candidates = [
        {"id": "p1", "candidates": [json.dumps(BAD_NUMBER), json.dumps(GOOD)]},   # rules
        {"id": "p2", "candidates": [json.dumps(SHORT), json.dumps(GOOD)]},        # judge
        {"id": "p3", "candidates": [json.dumps(BAD_NUMBER), json.dumps(REGLOSS)]},  # both fail
        {"id": "p4", "candidates": ["garbage", json.dumps(GOOD)]},               # rules (parse)
    ]
    out = tmp_path / "pairs.jsonl"
    stats = rp.rank(rows, candidates, _judge_prefers("Offside"), out_path=out, progress=lambda _: None)
    assert stats["pairs"] == 3 and stats["rules"] == 2 and stats["judge"] == 1 and stats["dropped_both_fail"] == 1
    pairs = rp.read_jsonl(out)
    assert {p["id"] for p in pairs} == {"p1", "p2", "p4"}
    assert pairs[0]["prompt"] == rows[0]["messages"][:2]
    assert all(json.loads(p["chosen"]) == GOOD for p in pairs)

    # A second run skips what is already decided and adds nothing twice.
    stats = rp.rank(rows, candidates, _judge_prefers("Offside"), out_path=out, progress=lambda _: None)
    assert stats["skipped_done"] == 3 and len(rp.read_jsonl(out)) == 3


def test_the_budget_stops_the_run_before_overspending(tmp_path):
    rows = [_row("p1"), _row("p2")]
    candidates = [{"id": "p1", "candidates": [json.dumps(SHORT), json.dumps(GOOD)]},
                  {"id": "p2", "candidates": [json.dumps(SHORT), json.dumps(GOOD)]}]
    spend = {"usd": 0.0, "calls": 0}

    def judge(packet, a, b):
        spend["usd"] += 0.6
        return {"winner": "A" if "Offside" in a else "B", "margin": "slight", "reasoning": "x"}

    stats = rp.rank(rows, candidates, judge, out_path=tmp_path / "p.jsonl", budget_usd=1.0, spend=spend,
                    progress=lambda _: None)
    assert stats["pairs"] == 1 and stats["stopped_for_budget"] is True


def test_the_offline_judge_runs_the_whole_cli_path(tmp_path):
    train = tmp_path / "train.jsonl"
    train.write_text("\n".join(json.dumps(_row(f"p{i}")) for i in range(3)) + "\n")
    cands = tmp_path / "c.jsonl"
    cands.write_text("\n".join(json.dumps({"id": f"p{i}", "candidates": [json.dumps(SHORT), json.dumps(GOOD)]}) for i in range(3)) + "\n")
    out = tmp_path / "pairs.jsonl"
    assert rp.main([str(cands), "--train", str(train), "--out", str(out), "--offline"]) == 0
    assert len(rp.read_jsonl(out)) == 3
