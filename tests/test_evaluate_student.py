"""The student evaluation scores a local model the way the teacher was scored.

Offline throughout: rows are built here, the "server" is a fake poster.
"""

from __future__ import annotations

import json

from training import evaluate_student as ev

PACKET = {
    "group": {"name": "Premier League", "description": "English football"},
    "concept_ledger": {"unknown": ["offside"], "familiar": ["var"], "explained": [], "confirmed": [], "provisional": []},
    "events": [{"headline": "City win the derby 2-1 after a VAR review", "detail": "Haaland scored twice; the winner came in the 88th minute."}],
}
TEACHER = {
    "briefing": "City beat United 2-1. Haaland scored twice, the second in the 88th minute. "
    "Offside, meaning an attacker was beyond the last defender when the ball was played, was checked by VAR.",
    "topic": "Manchester derby",
    "explained_terms": ["offside"],
    "terms_used": ["offside", "var"],
    "reasoning": "stub",
}


def _row(profile: str, headline: str | None = None) -> dict:
    packet = json.loads(json.dumps(PACKET))
    if headline:
        packet["events"][0]["headline"] = headline
    return {
        "messages": [
            {"role": "system", "content": "Write the briefing."},
            {"role": "user", "content": json.dumps({"_judgment_point": "briefing", "context": packet})},
            {"role": "assistant", "content": json.dumps(TEACHER)},
        ],
        "meta": {"id": f"row-{profile}", "profile": profile, "group_family": "Premier League"},
    }


def test_replaying_the_teacher_scores_identically_to_the_teacher():
    rows = [_row("beginner"), _row("expert")]
    report = ev.evaluate(rows, ev.replay_student(rows), progress=lambda _: None)
    summary = report["summary"]
    assert summary["rules"]["student"] == summary["rules"]["teacher"]
    assert summary["rules"]["teacher"]["all"] == 2
    for entry in summary["plainness_by_profile"].values():
        assert entry["student"] == entry["teacher"]


# Prevents: a student that returns prose, a question, or an invented number
# being scored as if it had passed.
def test_a_bad_student_fails_the_rules_it_breaks():
    rows = [_row("beginner")]
    answers = iter([
        "Sure! Here is the briefing: City won 2-1?",  # no JSON, and a question mark
    ])
    report = ev.evaluate(rows, lambda messages: next(answers), progress=lambda _: None)
    result = report["results"][0]
    assert result["student"]["parsed"] is False
    assert result["student"]["checks"]["passed"] is False
    assert result["teacher"]["checks"]["passed"] is True

    invented = dict(TEACHER, briefing=TEACHER["briefing"] + " The fee was 300 million pounds.")
    report = ev.evaluate(rows, lambda messages: json.dumps(invented), progress=lambda _: None)
    checks = report["results"][0]["student"]["checks"]
    assert checks["numbers_supported"] is False
    assert "300" in " ".join(checks["details"]["unsupported_numbers"])

    reglossed = dict(TEACHER, explained_terms=["offside", "var"])
    report = ev.evaluate(rows, lambda messages: json.dumps(reglossed), progress=lambda _: None)
    assert report["results"][0]["student"]["checks"]["no_regloss"] is False


def test_the_local_student_sends_the_rows_own_messages_unchanged(monkeypatch):
    from conversational_agent.local_client import LocalModelClient

    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["url"] = url
        seen["body"] = body
        reply = {"choices": [{"message": {"role": "assistant", "content": json.dumps(TEACHER)}}]}
        return 200, json.dumps(reply)

    client = LocalModelClient(base_url="http://localhost:11434/v1", model="news-briefer", post=fake_post)
    student = ev.local_student(client)
    row = _row("beginner")
    answer = student(row["messages"][:2])
    assert json.loads(answer)["topic"] == "Manchester derby"
    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["model"] == "news-briefer"
    assert seen["body"]["messages"] == row["messages"][:2]  # no second schema tail
    assert "response_format" not in seen["body"]


def test_the_summary_prints_both_columns():
    rows = [_row("beginner")]
    report = ev.evaluate(rows, ev.replay_student(rows), progress=lambda _: None)
    text = ev.format_summary(report["summary"], 1)
    assert "student | teacher" in text and "beginner" in text and "no_regloss" in text
