"""The Orchestrator: the only component that talks to the user.

Owns the interrupt-timing judgment -- what actually reaches the user this session, and how it's
framed. Monitors and Assessors report; only this decides to interrupt.

Delivery is pull-only in v1: nothing here runs unless the user opens a
session. The detection work behind it (the Monitor's background polling) runs
regardless -- what is session-gated is the surfacing, not the finding.

There is no decay pass on the read path: knowledge does not decay. Deciding
which groups warrant attention is now `assessor.session_need()` -- three
countable facts, no LLM -- because the old call spent a model round-trip
estimating how far a number had drifted, and there is no number.

What this component does NOT do is decide when a conversation is over. It opens
a session, surfaces what is worth surfacing, and hands the chosen group to
`Assessor.raise_topic`. From there the user asks what they want, or nothing,
and the thread ends when they stop. There is no judgment point for "are we
done" because there is nothing for it to be right about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import judgments
from .assessor import Assessor, session_need, warrants_attention
from .judgment import Judge, JudgmentError
from .models import Group, MonitorEvent
from .store import UserScope


@dataclass
class SessionBriefing:
    """What the Orchestrator decided to put in front of the user."""

    framing: str
    events: list[MonitorEvent] = field(default_factory=list)
    held_back_count: int = 0
    topic_group: Group | None = None
    topic_reason: str = ""
    reasoning: str = ""
    quiet: bool = False


def _need_reason(need: dict[str, Any], group: Group) -> str:
    """Say plainly why a group is on the shortlist. No score, no estimate."""
    parts: list[str] = []
    if need.get("behind"):
        n = need["behind"]
        parts.append(f"{n} unseen development{'s' if n != 1 else ''}")
    if need.get("never_engaged"):
        parts.append("never talked about this group yet")
    if need.get("goal_soon"):
        parts.append("goal deadline is close")
    return f"{group.name}: " + ", ".join(parts) if parts else ""


class Orchestrator:
    def __init__(self, judge: Judge, assessor: Assessor):
        self.judge = judge
        self.assessor = assessor

    def open_session(
        self, scope: UserScope, now: datetime | None = None
    ) -> SessionBriefing:
        """Decide what this user should see right now."""
        moment = now or datetime.now(UTC)
        groups = scope.list_groups()
        if not groups:
            return SessionBriefing(
                framing="No groups yet. Add one to start tracking it.", quiet=True
            )

        # Housekeeping first. Expiring a goal is the only bookkeeping left on
        # this path.
        scope.expire_due_goals(moment)

        by_id = {g.id: g for g in groups}
        pending = scope.pending_events()

        # Which groups warrant attention. Plain counting, one dict per group,
        # zero LLM calls.
        needs: dict[str, dict[str, Any]] = {
            g.id: session_need(scope, g, now=moment) for g in groups
        }
        attention: list[dict[str, Any]] = []
        for group in groups:
            need = needs[group.id]
            if not warrants_attention(need):
                continue
            attention.append(
                {
                    "group_id": group.id,
                    "group_name": group.name,
                    "behind": need["behind"],
                    "never_engaged": need["never_engaged"],
                    "goal_soon": need["goal_soon"],
                    "reason": _need_reason(need, group),
                }
            )

        if not pending and not attention:
            return SessionBriefing(
                framing="Nothing new since last time — you're current.",
                quiet=True,
                reasoning="No unseen material events and no group needing attention.",
            )

        groups_summary = []
        for group in groups:
            goal = scope.active_goal(group.id)
            ledger = scope.ledger(group.id)
            groups_summary.append(
                {
                    "id": group.id,
                    "name": group.name,
                    # A coarse band, not a number, and never shown to the user.
                    # It is here so the framing can be pitched right, not so
                    # anything can be ranked by it.
                    "proficiency": scope.proficiency(group.id),
                    "concepts_known": sum(1 for c in ledger if c.is_known),
                    "concepts_seen": len(ledger),
                    "unseen_events": needs[group.id]["behind"],
                    "active_goal": (
                        {"description": goal.description, "deadline": goal.deadline}
                        if goal
                        else None
                    ),
                }
            )

        # Newest first across all groups, then truncated: truncating in group
        # order could drop the most recent surfacing and misstate how long it
        # has been since the last session.
        recently_surfaced = sorted(
            (
                {"headline": e.headline, "surfaced_at": e.surfaced_at}
                for g in groups
                for e in scope.recent_events(g.id, limit=5)
                if e.surfaced_at
            ),
            key=lambda item: item["surfaced_at"],
            reverse=True,
        )[:10]

        last_seen = max(
            (e["surfaced_at"] for e in recently_surfaced if e["surfaced_at"]),
            default=None,
        )
        hours_since_session = None
        if last_seen:
            hours_since_session = (
                moment - datetime.fromisoformat(last_seen)
            ).total_seconds() / 3600.0

        # The interrupt-timing judgment.
        try:
            verdict = judgments.judge_interrupt_timing(
                self.judge,
                groups_summary=groups_summary,
                pending_events=[
                    {
                        "id": e.id,
                        "group_id": e.group_id,
                        "group_name": by_id[e.group_id].name if e.group_id in by_id else "",
                        "headline": e.headline,
                        "detail": e.detail,
                        "occurred_at": e.occurred_at,
                        "materiality_score": e.materiality_score,
                    }
                    for e in pending
                ],
                groups_needing_attention=attention,
                hours_since_last_session=hours_since_session,
                recently_surfaced=recently_surfaced,
                user_id=scope.user_id,
            )
        except JudgmentError as exc:
            # Degrade to showing nothing rather than dumping everything: the
            # spec's whole point is that "record it" and "interrupt with it"
            # are different bars, and a failed judgment must not collapse them.
            return SessionBriefing(
                framing="Couldn't assess what's worth surfacing right now.",
                quiet=True,
                reasoning=f"interrupt_timing judgment failed: {exc}",
            )

        if not verdict.get("should_surface") and not verdict.get("raise_topic"):
            return SessionBriefing(
                framing=verdict.get("framing")
                or "Nothing worth interrupting you with right now.",
                held_back_count=int(verdict.get("held_back_count", 0)),
                quiet=True,
                reasoning=verdict.reasoning,
            )

        # Events are surfaced only on an explicit `should_surface`; a verdict
        # that merely raises a topic must not mark anything as shown.
        chosen_ids = set(verdict.get("event_ids", [])) if verdict.get("should_surface") else set()
        chosen = [e for e in pending if e.id in chosen_ids]
        # Only what actually reached the user is marked surfaced; anything held
        # back stays queued for a future session -- and stays counted in
        # `behind_count`, which is what makes falling behind observable rather
        # than modelled.
        scope.mark_surfaced([e.id for e in chosen])

        # A topic is raised only for a group that has something UNTOLD. The
        # judgment may say `raise_topic`, and a group may be flagged for
        # attention (never engaged, behind, deadline near) -- but if every
        # material event in it has already been briefed, there is nothing new
        # to say, and nothing is said. Human rule, checkpoint 4: never repeat
        # the story; if there's nothing new, don't say anything.
        topic_group = None
        topic_reason = ""
        if verdict.get("raise_topic"):
            for target in attention:
                group = by_id.get(target["group_id"])
                if group is not None and scope.untold_events(group.id):
                    topic_group = group
                    topic_reason = target["reason"]
                    break

        return SessionBriefing(
            framing=verdict.get("framing", ""),
            events=chosen,
            # Counted, not taken from the model: what stayed queued is a fact.
            held_back_count=len(pending) - len(chosen),
            topic_group=topic_group,
            topic_reason=topic_reason,
            reasoning=verdict.reasoning,
        )
