"""Background polling: real, continuous, and independent of user sessions.

The spec is explicit that detection is not computed retroactively when a
session opens -- the Monitor runs on its own interval whether or not anyone is
looking. This module is that runner.

It is deliberately plain: sweep for due groups, poll each, sleep, repeat. No
LLM decides when to poll (adaptive intervals are explicitly out of scope for
v1); the only judgment inside a cycle is the Monitor's own.

Real users only. Persona users have no live feed by design -- their events are
injected by the eval harness -- so the sweep skips them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .app import System


@dataclass
class SweepResult:
    polled: int = 0
    material_found: int = 0
    skipped_personas: int = 0
    errors: list[str] = field(default_factory=list)
    # What a poll said about its own search (queries run, results discarded).
    # Information, not failure.
    notes: list[str] = field(default_factory=list)


def sweep_once(system: System, now: datetime | None = None) -> SweepResult:
    """Poll every group whose interval has elapsed. One pass, then return."""
    moment = now or datetime.now(UTC)
    result = SweepResult()

    for group in system.store.groups_due_for_poll(moment):
        user = system.store.get_user(group.user_id)
        if user is None:
            continue
        if user.is_persona:
            # Persona event feeds are injected by the harness, never polled.
            result.skipped_personas += 1
            continue

        scope = system.store.scope(group.user_id)
        try:
            poll = system.monitor.poll(scope, group)
        except Exception as exc:
            result.errors.append(f"{group.name}: {type(exc).__name__}: {exc}")
            continue
        result.polled += 1
        result.material_found += poll.material_count
        if poll.note:
            result.notes.append(f"{group.name}: {poll.note}")

    return result


def run_forever(system: System, tick_seconds: int = 300) -> None:
    """Long-running poller. Ctrl-C to stop."""
    print(f"Poller started (checking for due groups every {tick_seconds}s). Ctrl-C to stop.")
    while True:
        started = datetime.now(UTC)
        result = sweep_once(system, started)
        stamp = started.strftime("%H:%M:%S")
        if result.polled or result.errors:
            print(
                f"[{stamp}] polled {result.polled} group(s), "
                f"{result.material_found} material finding(s)"
            )
            for note in result.notes:
                print(f"[{stamp}]   - {note}")
            for err in result.errors:
                print(f"[{stamp}]   ! {err}")
        time.sleep(tick_seconds)
