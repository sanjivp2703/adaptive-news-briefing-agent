"""Goals are an intensity spike on top of persistent state, never a reset.

The spec: completing or expiring a goal never resets the underlying group
knowledge. Under the redesign that state is the concept ledger, so the thing
that must survive is every term and its evidence -- there is no level left to
preserve, and losing a ledger would be worse than losing a number because the
evidence behind each entry cannot be recomputed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conversational_agent import config


@pytest.fixture
def scope_with_ledger(store):
    user = store.create_user("goal-user", kind="real")
    scope = store.scope(user.id)
    group = scope.create_group("startup founders")
    for _ in range(config.CORRECT_USES_BEFORE_CONFIRMED):
        scope.mark_correct_use(group.id, ["seed round"], evidence="used it correctly")
    scope.mark_explained(group.id, ["safe note"], evidence="asked, explained")
    scope.note_exposure(group.id, ["cap table"])
    return scope, group


# Prevents: a user finishing "dinner with 3 founders Thursday" and silently
# losing the accumulated ledger for that group -- every term, and the evidence
# that put it in its state.
def test_closing_or_expiring_a_goal_leaves_the_concept_ledger_untouched(
    scope_with_ledger,
):
    scope, group = scope_with_ledger
    before = {c.term: (c.state, c.evidence, c.exposure_count) for c in scope.ledger(group.id)}
    assert len(before) == 3

    completed = scope.create_goal(
        group.id, "dinner Thursday", "2999-01-01T00:00:00+00:00"
    )
    scope.close_goal(completed.id, "completed")

    after_close = {
        c.term: (c.state, c.evidence, c.exposure_count) for c in scope.ledger(group.id)
    }
    assert after_close == before
    # Two attested terms, not three: "cap table" was only ever exposed, and
    # exposure-only terms sit out of both sides of the band's ratio.
    assert scope.proficiency(group.id) == config.proficiency_band(2, 2)

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    scope.create_goal(group.id, "conference last week", past)
    expired = scope.expire_due_goals(datetime.now(UTC))

    assert [g.description for g in expired] == ["conference last week"]
    after_expiry = {
        c.term: (c.state, c.evidence, c.exposure_count) for c in scope.ledger(group.id)
    }
    assert after_expiry == before
    # And the group itself keeps running -- no auto-archive.
    assert scope.get_group(group.id) is not None
    assert scope.active_goal(group.id) is None


# Prevents: two overlapping goals on one group, which would make "the active
# goal" ambiguous everywhere it is read (briefing generation, interrupt framing).
def test_a_second_active_goal_on_the_same_group_is_rejected(scope_with_ledger):
    scope, group = scope_with_ledger
    first = scope.create_goal(group.id, "dinner Thursday", "2999-01-01T00:00:00+00:00")

    with pytest.raises(ValueError):
        scope.create_goal(group.id, "panel Friday", "2999-02-01T00:00:00+00:00")

    assert scope.active_goal(group.id).id == first.id

    # Closing the first frees the slot; history is not constrained.
    scope.close_goal(first.id, "completed")
    second = scope.create_goal(group.id, "panel Friday", "2999-02-01T00:00:00+00:00")
    assert scope.active_goal(group.id).id == second.id
    assert len(scope.list_goals(group.id)) == 2
