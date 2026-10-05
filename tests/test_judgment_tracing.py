"""Every judgment call leaves exactly one diagnosable trace row.

Without this, "the materiality metric got worse" is unattributable: you can
see the number moved but not which call, which prompt version, or what input
produced it. A failed call is the case most worth tracing and the easiest to
drop on the floor.

Materiality is the judgment point used here because it is the one the Monitor
owns and the one whose contract did not move in the ledger redesign.
"""

from __future__ import annotations

import json

import pytest

from conversational_agent import config, judgments
from conversational_agent.judgment import JudgmentError, load_prompt

CANDIDATE = {"headline": "Star guard traded to Denver", "detail": "three-team deal"}


def _call(judge, scope, group):
    return judgments.judge_materiality(
        judge,
        group={"name": group.name, "description": group.description},
        candidate_event=CANDIDATE,
        recent_events=[],
        user_id=scope.user_id,
        group_id=group.id,
    )


@pytest.fixture
def traced(store):
    user = store.create_user("traced", kind="real")
    scope = store.scope(user.id)
    return scope, scope.create_group("NBA fans")


# Prevents: judgment calls succeeding but leaving no trace, turning any later
# quality regression into guesswork about which call and prompt caused it.
def test_a_judgment_call_writes_one_log_row_with_prompt_version_input_and_verdict(
    judge, stub, store, traced
):
    scope, group = traced
    stub.register(
        config.MATERIALITY,
        lambda ctx: {
            "is_material": True,
            "materiality_score": 88.0,
            "reasoning": "A starter changing teams is table stakes for this group.",
        },
    )

    verdict = _call(judge, scope, group)

    rows = store.judgments(judgment_point=config.MATERIALITY)
    assert len(rows) == 1
    row = rows[0]

    assert row["prompt_version"] == load_prompt(config.MATERIALITY).version
    assert row["model"] == config.model_for(config.MATERIALITY)
    assert row["effort"] == config.effort_for(config.MATERIALITY)
    assert row["user_id"] == scope.user_id and row["group_id"] == group.id
    assert row["error"] is None

    # The input actually given to the model, not a summary of it.
    logged_input = json.loads(row["input_json"])
    assert logged_input["_judgment_point"] == config.MATERIALITY
    assert logged_input["context"]["candidate_event"] == CANDIDATE

    assert json.loads(row["verdict_json"])["materiality_score"] == 88.0
    assert row["reasoning"] == verdict.reasoning
    assert "table stakes" in row["reasoning"]


# Prevents: a failing judgment raising with no record of what it was asked --
# the exact calls you most need to read back when diagnosing a bad run.
def test_a_failing_judgment_call_is_logged_with_its_error_before_raising(
    judge, stub, store, traced
):
    scope, group = traced

    # No handler registered: StubClient raises, standing in for a model failure.
    with pytest.raises(JudgmentError):
        _call(judge, scope, group)

    rows = store.judgments(judgment_point=config.MATERIALITY)
    assert len(rows) == 1
    row = rows[0]
    assert row["error"] and "no handler registered" in row["error"]
    assert row["verdict_json"] is None
    assert json.loads(row["input_json"])["context"]["candidate_event"] == CANDIDATE
    assert row["prompt_version"] == load_prompt(config.MATERIALITY).version


# Prevents: the live defect where two replies reached the user containing a
# literal `\\u2014`. Non-ASCII in the context must go to the model as the
# character, and a literal escape in the model's output must come back as the
# character.
def test_non_ascii_survives_the_round_trip_and_literal_escapes_are_decoded():
    from conversational_agent.judgment import _decode_literal_escapes

    assert _decode_literal_escapes("a \\u2014 b \\u00a375m") == "a — b £75m"
    assert _decode_literal_escapes({"x": ["\\u00e9", 3]}) == {"x": ["é", 3]}
    assert _decode_literal_escapes("no escapes here") == "no escapes here"
    import json
    payload = json.dumps({"t": "£ — é"}, ensure_ascii=False)
    assert "\\u" not in payload and "£ — é" in payload
