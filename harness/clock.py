"""A simulated clock, so elapsed time is a controlled variable rather than weather.

The foundation stamps rows with `store.now_iso()` and accepts an optional `now`
on the functions where elapsed time matters (`session_need`, `open_session`).
There is no injectable clock seam in `src/`, and this harness must not add one,
so the clock is installed by rebinding `store.now_iso` for the duration of a run.

This is one of the substitutions the harness makes. Every row written during a
run carries a timestamp, the persona memory model forgets as simulated days
pass, and `harness.verify_offline` requires two runs to be byte-identical.
Without a fixed clock the artifacts differ on wall-clock noise and the
reproducibility check becomes unmeasurable.

The epoch is fixed rather than `now`, so even the ISO strings in the scratch DB
are byte-identical between runs, which makes a stray timestamp leaking into a
metric immediately visible instead of silently nondeterministic.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from conversational_agent import store as _store

# A fixed, arbitrary Monday morning. Any constant would do; what matters is
# that it is a constant.
EPOCH = datetime(2026, 1, 5, 9, 0, 0, tzinfo=UTC)


@dataclass
class SimClock:
    """Monotonic simulated time. Only the harness ever advances it."""

    moment: datetime = field(default=EPOCH)

    def now(self) -> datetime:
        return self.moment

    def iso(self) -> str:
        return self.moment.isoformat()

    def advance(self, hours: float) -> datetime:
        if hours < 0:
            raise ValueError("the simulated clock only moves forward")
        self.moment = self.moment + timedelta(hours=hours)
        return self.moment

    def reset(self, moment: datetime = EPOCH) -> datetime:
        """Restart the timeline.

        Personas run sequentially against one store, and the runner resets
        between them so that each persona's timeline begins at the same instant.
        Without this, persona 3's timestamps would depend on how many rounds
        personas 1 and 2 happened to have, and `--persona X` would report
        different numbers than the same persona inside a full run.

        This is the one operation that moves the clock backwards, which is why
        it is a separate, explicitly-named method rather than a negative
        `advance`.
        """
        self.moment = moment
        return self.moment

    def offset_iso(self, hours: float) -> str:
        """An ISO stamp relative to now -- used to date injected events."""
        return (self.moment + timedelta(hours=hours)).isoformat()


# --- Installing the clock, one thread at a time ----------------------------
#
# `store.now_iso` is a module global, so rebinding it is a process-wide act.
# That was fine while personas ran strictly sequentially. `--parallel` replays
# several at once, each with its own timeline, and two threads assigning to the
# same global would give persona A rows stamped with persona B's simulated
# time -- reintroducing exactly the cross-persona coupling that `SimClock.reset`
# exists to prevent, and breaking reproducibility in a way that would look like
# a flaky metric rather than a clock bug.
#
# So the global is rebound exactly once, to a dispatcher that reads a
# thread-local clock. Each thread installs its own; a thread with none falls
# through to the real clock, which is what any non-harness code in the process
# should still get.

_local = threading.local()
_lock = threading.Lock()
_depth = 0
_real_now_iso = _store.now_iso


def _dispatch() -> str:
    clock = getattr(_local, "clock", None)
    return clock.iso() if clock is not None else _real_now_iso()


@contextmanager
def installed(clock: SimClock) -> Iterator[SimClock]:
    """Rebind `store.now_iso` to `clock` for the duration, in this thread only.

    Scoped rather than global so importing this module has no effect on a
    process that merely wants the fixtures, and thread-local so that parallel
    persona replays cannot stamp each other's rows.
    """
    global _depth
    previous = getattr(_local, "clock", None)
    _local.clock = clock
    with _lock:
        _depth += 1
        if _depth == 1:
            _store.now_iso = _dispatch  # type: ignore[assignment]
    try:
        yield clock
    finally:
        _local.clock = previous
        with _lock:
            _depth -= 1
            if _depth == 0:
                _store.now_iso = _real_now_iso  # type: ignore[assignment]
