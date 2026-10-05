"""The Knowledge-State Store: all persistence, with user isolation enforced.

The spec names cross-user data leakage as the system's top risk. Rather than
relying on every future query remembering to filter by user, that guarantee is
structural here:

  * `Store` holds only operations that are legitimately cross-user
    (creating a user, listing users, the scheduler's due-poll sweep).
  * Every operation that touches a user's own data lives on `UserScope`,
    which is constructed with a user_id and injects it into every statement.

There is no API on `UserScope` that lets a caller name a different user, so a
component holding one scope cannot read or write another user's rows even by
mistake.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config, db
from .models import (
    Concept,
    Exchange,
    Goal,
    Group,
    MonitorEvent,
    Turn,
    User,
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _loads_terms(raw: Any) -> list[str]:
    """JSON list column -> list[str]; tolerant of NULL and junk."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(t) for t in data if str(t).strip()] if isinstance(data, list) else []


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --- Reading behaviour -----------------------------------------------------
#
# How the briefing was read is ATTENTION, NEVER COMPREHENSION. There is no path
# from anything below into the concept ledger, and there must never be one:
# every module-level function here is deliberately deterministic and has no
# access to a scope, so it cannot write one.
#
# The industry has had to learn this repeatedly. Apple's Mail Privacy
# Protection pre-fetches images and inflates roughly half of all reported email
# opens; the IAB had to invent "viewability" because "served" had stopped
# meaning anything; most shared links are never clicked at all. A display
# metric tells you a surface was in front of someone. It does not tell you they
# read it, and it certainly does not tell you they understood it.
#
# What this IS good for: closing an item out of a "new" count, and reporting
# engagement honestly.

# Reading speed used to turn dwell into a plausible fraction-read. 200 wpm is a
# common adult silent-reading figure; at ~5.5 characters per word that is about
# 18 characters per second. Deliberately one constant rather than a model --
# precision here would be false, because the input is already a proxy.
_CHARS_PER_SECOND = 18.0

# Below this, the briefing cannot have been read at any speed.
_SKIPPED_MAX_FRACTION = 0.15
# Read most of it at a plausible pace.
_READ_MIN_FRACTION = 0.75
# Lingered well past the time the text alone would take.
_STUDIED_MIN_RATIO = 1.6


def read_quality(
    dwell_ms: int | None,
    scroll_fraction: float | None,
    briefing_chars: int,
) -> str:
    """Bucket reading behaviour into skipped | skimmed | read | studied.

    Deterministic and LLM-free on purpose. This is a coarse readout of two weak
    proxies, and asking a model to interpret them would dress a guess up as a
    judgment -- while also creating a call whose output could plausibly be
    routed somewhere it must never go.

    Missing signals degrade rather than fail: a caller that can observe neither
    dwell nor scroll (the CLI cannot see either -- text simply prints) gets
    `skimmed`, the honest "something was in front of them" answer, rather than
    a fabricated one in either direction.
    """
    chars = max(int(briefing_chars or 0), 1)
    seconds = None if dwell_ms is None else max(float(dwell_ms), 0.0) / 1000.0
    fraction = None if scroll_fraction is None else min(max(float(scroll_fraction), 0.0), 1.0)

    if seconds is None and fraction is None:
        return "skimmed"

    # Time the text would take to read at a plausible pace.
    expected_seconds = chars / _CHARS_PER_SECOND

    # Scroll alone: the fraction is all we have.
    if seconds is None:
        if fraction <= _SKIPPED_MAX_FRACTION:
            return "skipped"
        if fraction >= _READ_MIN_FRACTION:
            return "read"
        return "skimmed"

    ratio = seconds / expected_seconds if expected_seconds > 0 else 0.0

    # Dwell alone: how long they stayed, against how long it would take.
    if fraction is None:
        if ratio < 0.15:
            return "skipped"
        if ratio >= _STUDIED_MIN_RATIO:
            return "studied"
        if ratio >= 0.6:
            return "read"
        return "skimmed"

    # Both. They have to agree before the strong buckets are claimed: a long
    # dwell on an unscrolled page is a tab left open, and a fast scroll to the
    # bottom is a scroll to the bottom.
    if fraction <= _SKIPPED_MAX_FRACTION or ratio < 0.15:
        return "skipped"
    if fraction >= _READ_MIN_FRACTION and ratio >= _STUDIED_MIN_RATIO:
        return "studied"
    if fraction >= _READ_MIN_FRACTION and ratio >= 0.6:
        return "read"
    return "skimmed"

def attention_score(
    dwell_ms: int | None,
    scroll_fraction: float | None,
    briefing_chars: int,
) -> float | None:
    """0-10 readout of the same two proxies `read_quality` buckets.

    Deterministic, LLM-free, and subject to the same wall: it describes how
    much attention the text got, never what was understood. Dwell is compared
    with the time the text would take to read; scroll is taken as is; when
    both exist they are averaged, dwell weighted slightly more because scroll
    saturates on short briefings. None when neither signal exists.
    """
    chars = max(int(briefing_chars or 0), 1)
    seconds = None if dwell_ms is None else max(float(dwell_ms), 0.0) / 1000.0
    fraction = None if scroll_fraction is None else min(max(float(scroll_fraction), 0.0), 1.0)
    if seconds is None and fraction is None:
        return None
    expected = chars / _CHARS_PER_SECOND
    dwell_part = None if seconds is None else min(seconds / expected, 1.5) / 1.5
    if dwell_part is None:
        score = fraction
    elif fraction is None:
        score = dwell_part
    else:
        score = 0.6 * dwell_part + 0.4 * fraction
    return round(10.0 * float(score), 2)


# Words that carry no concept identity on their own. Kept deliberately short --
# over-stripping merges genuinely distinct concepts, which is worse than the
# duplication it is trying to fix.
_TERM_NOISE = frozenset(
    {"the", "a", "an", "of", "in", "on", "for", "and", "to", "s"}
)


def normalize_term(term: str) -> str:
    """Lowercase, de-hyphenate, collapse whitespace. Cheap and total."""
    cleaned = term.strip().lower().replace("-", " ").replace("_", " ")
    return " ".join(cleaned.split())


def normalize_subdomain(label: Any) -> str | None:
    """Canonical form of a subdomain label, or None when there is nothing there.

    Same treatment as a term -- lowercase, de-hyphenated, whitespace
    collapsed -- plus stripping the backticks and quotes a model tends to
    wrap an example in. The labels are keys in the familiarity readout, so
    `Appellation Rules` and `appellation rules` must be one slice.
    """
    if label is None:
        return None
    cleaned = normalize_term(str(label).strip().strip("`'\""))
    return cleaned or None


def _subdomain_lookup(subdomains: Mapping[str, Any] | None) -> dict[str, str]:
    """{raw term: raw label} -> {normalised term: canonical label}, dropping junk."""
    out: dict[str, str] = {}
    for raw_term, raw_label in (subdomains or {}).items():
        term = normalize_term(str(raw_term)) if raw_term is not None else ""
        label = normalize_subdomain(raw_label)
        if term and label:
            out[term] = label
    return out


# The two predicates the proficiency band is built from, written ONCE so that
# the whole-group band (`UserScope.proficiency`) and the per-subdomain readout
# (`UserScope.subdomain_familiarity`) cannot drift apart. Both take
# `_band_predicate_params()` in this order.
#
#   attested -- the user gave us a signal about the term: asked about it
#               (explained_at), used it correctly, got it wrong, or has read
#               its explanation READ_EXPLANATIONS_BEFORE_BAND times. Terms we
#               merely said are on neither side.
#   known    -- `confirmed` always; `familiar` / `explained` only at the read
#               threshold; `provisional` never.
_ATTESTED_PREDICATE = (
    "(explained_at IS NOT NULL OR correct_uses > 0"
    " OR misunderstandings > 0 OR read_explanations >= ?)"
)


def _known_predicate() -> str:
    placeholders = ",".join("?" for _ in config.BAND_STATES)
    return f"(state = ? OR (state IN ({placeholders}) AND read_explanations >= ?))"


def _band_predicate_params() -> tuple[Any, ...]:
    return (
        config.READ_EXPLANATIONS_BEFORE_BAND,
        config.CONCEPT_CONFIRMED,
        *config.BAND_STATES,
        config.READ_EXPLANATIONS_BEFORE_BAND,
    )


def _band_count_columns() -> str:
    return (
        f" SUM(CASE WHEN {_ATTESTED_PREDICATE} THEN 1 ELSE 0 END) AS attested,"
        f" SUM(CASE WHEN {_known_predicate()} THEN 1 ELSE 0 END) AS known"
    )


def _singular(token: str) -> str:
    """Naive singularization, enough to stop plurals forking a concept.

    `billion dollar round` and `billion dollar rounds` were landing as two
    concepts because containment compares tokens exactly. Deliberately crude --
    an aggressive stemmer would merge genuinely distinct terms, which is the
    worse error here.
    """
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("es") and not token.endswith(("ses", "zes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _content_tokens(term: str) -> frozenset[str]:
    return frozenset(
        _singular(t) for t in normalize_term(term).split() if t not in _TERM_NOISE
    )


class Store:
    """Owns the connection. Cross-user operations only."""

    def __init__(self, path: Path | None = None):
        self.conn: sqlite3.Connection = db.connect(path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- Users (inherently cross-user) ------------------------------------

    def create_user(
        self,
        display_name: str,
        kind: str = "real",
        profile: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> User:
        if kind not in ("real", "persona"):
            raise ValueError(f"kind must be 'real' or 'persona', got {kind!r}")
        uid = user_id or new_id("usr")
        self.conn.execute(
            "INSERT INTO users (id, kind, display_name, profile_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, kind, display_name, json.dumps(profile or {}), now_iso()),
        )
        return User(id=uid, kind=kind, display_name=display_name, profile=profile or {})

    def get_user(self, user_id: str) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return User.from_row(row) if row else None

    def find_user_by_name(self, display_name: str) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE display_name = ?", (display_name,)
        ).fetchone()
        return User.from_row(row) if row else None

    def list_users(self, kind: str | None = None) -> list[User]:
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM users WHERE kind = ? ORDER BY created_at", (kind,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM users ORDER BY created_at"
            ).fetchall()
        return [User.from_row(r) for r in rows]

    # --- Scheduler sweep (deliberately cross-user) ------------------------

    def groups_due_for_poll(self, as_of: datetime | None = None) -> list[Group]:
        """Every group whose Monitor is due, across all users.

        This is the one legitimately cross-user read of group data: the
        background poller sweeps all users. It returns whole Group records so
        the caller can immediately construct the right UserScope; it never
        returns another user's knowledge state or interactions.
        """
        moment = as_of or datetime.now(UTC)
        rows = self.conn.execute("SELECT * FROM groups").fetchall()
        due: list[Group] = []
        for row in rows:
            group = Group.from_row(row)
            if group.last_polled_at is None:
                due.append(group)
                continue
            last = datetime.fromisoformat(group.last_polled_at)
            elapsed_minutes = (moment - last).total_seconds() / 60.0
            if elapsed_minutes >= group.poll_interval_minutes:
                due.append(group)
        return due

    def scope(self, user_id: str) -> UserScope:
        if self.get_user(user_id) is None:
            raise KeyError(f"No such user: {user_id}")
        return UserScope(self.conn, user_id)

    # --- Judgment log (analysis surface; cross-user by design) ------------

    def log_judgment(self, record: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO judgment_log ("
            " id, created_at, judgment_point, user_id, group_id, prompt_version,"
            " model, effort, input_json, verdict_json, reasoning, input_tokens,"
            " output_tokens, latency_ms, error, run_id"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.get("id") or new_id("jdg"),
                record.get("created_at") or now_iso(),
                record["judgment_point"],
                record.get("user_id"),
                record.get("group_id"),
                record["prompt_version"],
                record["model"],
                record.get("effort"),
                record["input_json"],
                record.get("verdict_json"),
                record.get("reasoning"),
                record.get("input_tokens"),
                record.get("output_tokens"),
                record.get("latency_ms"),
                record.get("error"),
                record.get("run_id"),
            ),
        )

    def judgments(
        self,
        judgment_point: str | None = None,
        run_id: str | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        if judgment_point:
            clauses.append("judgment_point = ?")
            params.append(judgment_point)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.conn.execute(
            f"SELECT * FROM judgment_log {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()

    # --- Eval runs --------------------------------------------------------

    def start_eval_run(self, suite: str, config_json: str) -> str:
        run_id = new_id("run")
        self.conn.execute(
            "INSERT INTO eval_runs (id, started_at, suite, config_json) VALUES (?,?,?,?)",
            (run_id, now_iso(), suite, config_json),
        )
        return run_id

    def finish_eval_run(self, run_id: str, metrics_json: str, passed: bool) -> None:
        self.conn.execute(
            "UPDATE eval_runs SET finished_at = ?, metrics_json = ?, passed = ? "
            "WHERE id = ?",
            (now_iso(), metrics_json, 1 if passed else 0, run_id),
        )

    def eval_runs(self, suite: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        if suite:
            return self.conn.execute(
                "SELECT * FROM eval_runs WHERE suite = ? ORDER BY started_at DESC LIMIT ?",
                (suite, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM eval_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()


class UserScope:
    """All per-user data access. Every statement binds this scope's user_id.

    Constructed via `Store.scope(user_id)`. Note there is no method here that
    accepts a *different* user id -- that absence is the isolation guarantee.
    """

    def __init__(self, conn: sqlite3.Connection, user_id: str):
        self._conn = conn
        self._user_id = user_id
        self._kind: str | None = None

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def user_kind(self) -> str:
        """'real' or 'persona'. Cached -- it cannot change for a given user."""
        if self._kind is None:
            row = self._conn.execute(
                "SELECT kind FROM users WHERE id = ?", (self._user_id,)
            ).fetchone()
            self._kind = row["kind"] if row else "real"
        return self._kind

    @contextmanager
    def transaction(self):
        """Serialize a read-modify-write against concurrent writers.

        The spec names this risk directly: the background poller and an open
        session run at the same time, and a read-compute-write on knowledge
        state can otherwise interleave so that one
        writer's update is computed from state the other has already replaced.

        `BEGIN IMMEDIATE` takes the write lock up front rather than on first
        write, so a second writer blocks (up to `busy_timeout`) instead of
        racing. Nested use is a no-op so callers can compose safely.
        """
        if self._conn.in_transaction:
            yield
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # --- Groups -----------------------------------------------------------

    def create_group(
        self,
        name: str,
        description: str | None = None,
        poll_interval_minutes: int | None = None,
    ) -> Group:
        gid = new_id("grp")
        created = now_iso()
        interval = poll_interval_minutes or config.DEFAULT_POLL_INTERVAL_MINUTES
        self._conn.execute(
            "INSERT INTO groups (id, user_id, name, description,"
            " poll_interval_minutes, last_polled_at, created_at)"
            " VALUES (?,?,?,?,?,NULL,?)",
            (gid, self._user_id, name, description, interval, created),
        )
        # No knowledge row to seed: the ledger starts empty by construction,
        # which is exactly right -- a new group means we know nothing yet.
        return self.get_group(gid)  # type: ignore[return-value]

    def get_group(self, group_id: str) -> Group | None:
        row = self._conn.execute(
            "SELECT * FROM groups WHERE id = ? AND user_id = ?",
            (group_id, self._user_id),
        ).fetchone()
        return Group.from_row(row) if row else None

    def find_group_by_name(self, name: str) -> Group | None:
        row = self._conn.execute(
            "SELECT * FROM groups WHERE name = ? AND user_id = ?",
            (name, self._user_id),
        ).fetchone()
        return Group.from_row(row) if row else None

    def list_groups(self) -> list[Group]:
        rows = self._conn.execute(
            "SELECT * FROM groups WHERE user_id = ? ORDER BY created_at",
            (self._user_id,),
        ).fetchall()
        return [Group.from_row(r) for r in rows]

    def mark_polled(self, group_id: str, when: str | None = None) -> None:
        self._conn.execute(
            "UPDATE groups SET last_polled_at = ? WHERE id = ? AND user_id = ?",
            (when or now_iso(), group_id, self._user_id),
        )

    # --- Goals ------------------------------------------------------------

    def create_goal(self, group_id: str, description: str, deadline: str) -> Goal:
        if self.get_group(group_id) is None:
            raise KeyError(f"No such group for this user: {group_id}")
        gid = new_id("goal")
        try:
            self._conn.execute(
                "INSERT INTO goals (id, user_id, group_id, description, deadline,"
                " status, created_at) VALUES (?,?,?,?,?,'active',?)",
                (gid, self._user_id, group_id, description, deadline, now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "This group already has an active goal. Close it before "
                "creating another (at most one active goal per group)."
            ) from exc
        return self.get_goal(gid)  # type: ignore[return-value]

    def get_goal(self, goal_id: str) -> Goal | None:
        row = self._conn.execute(
            "SELECT * FROM goals WHERE id = ? AND user_id = ?",
            (goal_id, self._user_id),
        ).fetchone()
        return Goal.from_row(row) if row else None

    def active_goal(self, group_id: str) -> Goal | None:
        row = self._conn.execute(
            "SELECT * FROM goals WHERE group_id = ? AND user_id = ? AND status = 'active'",
            (group_id, self._user_id),
        ).fetchone()
        return Goal.from_row(row) if row else None

    def list_goals(self, group_id: str | None = None) -> list[Goal]:
        if group_id:
            rows = self._conn.execute(
                "SELECT * FROM goals WHERE user_id = ? AND group_id = ? ORDER BY created_at DESC",
                (self._user_id, group_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM goals WHERE user_id = ? ORDER BY created_at DESC",
                (self._user_id,),
            ).fetchall()
        return [Goal.from_row(r) for r in rows]

    def close_goal(self, goal_id: str, status: str = "completed") -> None:
        """Close a goal WITHOUT touching knowledge state.

        The spec is explicit that a goal is a temporary intensity spike on top
        of persistent state -- completing or expiring it must never reset what
        the user knows.
        """
        if status not in ("completed", "expired"):
            raise ValueError("status must be 'completed' or 'expired'")
        self._conn.execute(
            "UPDATE goals SET status = ?, closed_at = ? WHERE id = ? AND user_id = ?",
            (status, now_iso(), goal_id, self._user_id),
        )

    def expire_due_goals(self, as_of: datetime | None = None) -> list[Goal]:
        moment = as_of or datetime.now(UTC)
        expired = []
        for goal in self.list_goals():
            if goal.status != "active":
                continue
            if goal.deadline_dt <= moment:
                self.close_goal(goal.id, "expired")
                expired.append(goal)
        return expired

    # --- Concept ledger ---------------------------------------------------


    def canonical_term(self, group_id: str, term: str) -> str:
        """Resolve an incoming term against what the ledger already holds.

        If the incoming term's content words are a subset or superset of an
        existing term's, they are the same concept and the EXISTING label wins
        -- it already carries the evidence, and switching labels would strand
        it. This is what stops `run-rate`, `annualized revenue` and
        `annualized revenue run-rate` becoming three concepts.

        Merges only on containment, never on partial overlap: "gross margin"
        and "operating margin" share a token but neither contains the other, so
        they stay distinct. Single-character and very short tokens are ignored
        as anchors, since merging on those over-collapses.
        """
        incoming = normalize_term(term)
        if not incoming:
            return incoming
        rows = self._conn.execute(
            "SELECT term FROM concepts WHERE user_id = ? AND group_id = ?",
            (self._user_id, group_id),
        ).fetchall()
        existing_terms = [r["term"] for r in rows]
        if incoming in existing_terms:
            return incoming

        tokens = _content_tokens(incoming)
        if not tokens:
            return incoming
        incoming_full = set(normalize_term(incoming).split())
        for candidate in existing_terms:
            other = _content_tokens(candidate)
            if not other:
                continue
            if not (tokens <= other or other <= tokens):
                continue
            # A short designator token that only one side has makes them
            # different things, not a longer form of the same thing: `series a`,
            # `series b` and `series c` all reduce to {series} once noise
            # tokens are dropped, and a live run merged Sam's `series a` into
            # `series b`. The letter is the whole distinction.
            differing = incoming_full ^ set(normalize_term(candidate).split())
            if any(len(tok) <= 2 for tok in differing):
                continue
            # Require the shared anchor to be substantial; merging on a stray
            # short token collapses unrelated concepts.
            shared = tokens & other
            if not any(len(tok) >= 4 for tok in shared):
                continue
            return candidate
        return incoming

    def _touch_concept(
        self, group_id: str, term: str, state: str, evidence: str | None
    ) -> None:
        stamp = now_iso()
        self._conn.execute(
            "INSERT INTO concepts (user_id, group_id, term, state, exposure_count,"
            " evidence, first_seen_at, last_seen_at,"
            " explained_at, confirmed_at) VALUES (?,?,?,?,0,?,?,?,?,?)"
            " ON CONFLICT(user_id, group_id, term) DO UPDATE SET"
            "   state = excluded.state,"
            "   evidence = COALESCE(excluded.evidence, concepts.evidence),"
            "   last_seen_at = excluded.last_seen_at,"
            "   explained_at = COALESCE(concepts.explained_at, excluded.explained_at),"
            "   confirmed_at = COALESCE(excluded.confirmed_at, concepts.confirmed_at)",
            (
                self._user_id,
                group_id,
                self.canonical_term(group_id, term),
                state,
                evidence,
                stamp,
                stamp,
                stamp if state == config.CONCEPT_EXPLAINED else None,
                stamp if state == config.CONCEPT_CONFIRMED else None,
            ),
        )

    # --- Subdomain labels --------------------------------------------------
    #
    # A label is a fact about the TERM ("this belongs to `appellation rules`"),
    # written by the same calls that name the term. It changes no state and
    # moves no band by itself; its only consumer is `subdomain_familiarity`,
    # which aggregates the existing evidence per slice.
    #
    # Churn rule, so labels settle early and stop moving:
    #   * a label is never overwritten with nothing;
    #   * a DIFFERENT label wins only while the term has no attested evidence
    #     (nothing the user did about it yet) -- once they have asked about it,
    #     used it, got it wrong, or read it twice, the label is fixed.
    # The exposure-era label (from the briefing that first used the term) is
    # therefore the weakest and is the one that gets corrected, by the first
    # call that reads what the user actually did with the term.

    def _term_is_attested(self, group_id: str, term: str) -> bool:
        row = self._conn.execute(
            f"SELECT {_ATTESTED_PREDICATE} AS attested FROM concepts"
            " WHERE user_id = ? AND group_id = ? AND term = ?",
            (config.READ_EXPLANATIONS_BEFORE_BAND, self._user_id, group_id, term),
        ).fetchone()
        return bool(row) and bool(row["attested"])

    def _apply_subdomain(
        self, group_id: str, term: str, label: str | None, *, settled: bool
    ) -> None:
        """Write `label` on `term` under the churn rule.

        `settled` is whether the term had attested evidence BEFORE the write
        this call is making -- captured by the caller, because the write
        itself may be what attests the term, and the label riding along with
        the first attesting evidence is exactly the one that should win.
        """
        if not label:
            return
        self._conn.execute(
            "UPDATE concepts SET subdomain = ?"
            " WHERE user_id = ? AND group_id = ? AND term = ?"
            " AND (subdomain IS NULL OR subdomain = '' OR (subdomain != ? AND ? = 0))",
            (label, self._user_id, group_id, term, label, 1 if settled else 0),
        )

    def _label_for(
        self, labels: dict[str, str], raw: str, canonical: str
    ) -> str | None:
        """The label a caller supplied for this term, under either spelling."""
        return labels.get(normalize_term(raw)) or labels.get(canonical)

    def note_exposure(
        self,
        group_id: str,
        terms: Iterable[str],
        subdomains: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Record that these terms were put in front of the user.

        This COUNTS exposure and promotes nothing. Silence was measured against
        persona ground truth and found anti-predictive at every threshold, so
        no amount of talking at someone is evidence that they understood.

        The count is still useful -- it tells the briefing whether a term is new
        to this conversation -- so it is kept, just stripped of any inferential
        weight. Returns [] always; the return type is retained so callers that
        logged promotions keep working.

        `subdomains` ({term: label}) labels the slice each term belongs to. A
        label is a fact about the term, not evidence about the user, so it is
        stored here without breaking the promotes-nothing rule.
        """
        stamp = now_iso()
        labels = _subdomain_lookup(subdomains)
        for raw in terms:
            term = self.canonical_term(group_id, raw)
            if not term:
                continue
            label = self._label_for(labels, raw, term)
            settled = label is not None and self._term_is_attested(group_id, term)
            self._conn.execute(
                "INSERT INTO concepts (user_id, group_id, term, state, exposure_count,"
                " first_seen_at, last_seen_at) VALUES (?,?,?,?,1,?,?)"
                " ON CONFLICT(user_id, group_id, term) DO UPDATE SET"
                "   exposure_count = concepts.exposure_count + 1,"
                "   last_seen_at = excluded.last_seen_at",
                (self._user_id, group_id, term, config.CONCEPT_UNKNOWN, stamp, stamp),
            )
            self._apply_subdomain(group_id, term, label, settled=settled)
        return []

    def mark_explained(
        self,
        group_id: str,
        terms: Iterable[str],
        evidence: str | None = None,
        subdomains: Mapping[str, Any] | None = None,
    ) -> None:
        """They asked about these; we explained them.

        Asking is a read explanation with the strongest attention there is --
        they wanted the answer -- so it counts toward `read_explanations`
        like a gloss in a read briefing does. One ask stops re-glossing; the
        second explanation they read, by either route, moves the band.
        """
        labels = _subdomain_lookup(subdomains)
        for term in terms:
            if not term.strip():
                continue
            canonical = self.canonical_term(group_id, term)
            label = self._label_for(labels, term, canonical)
            settled = label is not None and self._term_is_attested(group_id, canonical)
            self._touch_concept(
                group_id, term, config.CONCEPT_EXPLAINED, evidence or "asked, explained"
            )
            self._conn.execute(
                "UPDATE concepts SET read_explanations = read_explanations + 1"
                " WHERE user_id = ? AND group_id = ? AND term = ?",
                (self._user_id, group_id, canonical),
            )
            self._apply_subdomain(group_id, canonical, label, settled=settled)

    def mark_read_explanation(
        self,
        group_id: str,
        terms: Iterable[str],
        evidence: str | None = None,
        subdomains: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """We defined these terms in a briefing, and the user read that briefing.

        The one sanctioned path from reading behaviour into the ledger, and it
        is narrow on purpose. The caller (`Assessor.close_thread`) is
        responsible for two preconditions: the terms were actually defined in
        the briefing (`exchanges.explained_terms`, not merely mentioned), and
        the briefing's `read_quality` was something other than `skipped`.
        `record_reading` itself still touches nothing here.

        Effect: `read_explanations` += 1 for every term. A term at `unknown`
        becomes `familiar`; any stronger state is kept. Nothing here can reach
        `confirmed`, and nothing here moves the band on its own -- the band
        looks at the count, and needs READ_EXPLANATIONS_BEFORE_BAND.

        Returns the terms that newly became `familiar` on this call.
        """
        promoted: list[str] = []
        stamp = now_iso()
        labels = _subdomain_lookup(subdomains)
        for raw in terms:
            term = self.canonical_term(group_id, raw)
            if not term:
                continue
            label = self._label_for(labels, raw, term)
            settled = label is not None and self._term_is_attested(group_id, term)
            existed = self._conn.execute(
                "SELECT 1 FROM concepts WHERE user_id = ? AND group_id = ? AND term = ?",
                (self._user_id, group_id, term),
            ).fetchone() is not None
            self._conn.execute(
                "INSERT INTO concepts (user_id, group_id, term, state, exposure_count,"
                " read_explanations, evidence, first_seen_at, last_seen_at)"
                " VALUES (?,?,?,?,0,1,?,?,?)"
                " ON CONFLICT(user_id, group_id, term) DO UPDATE SET"
                "   read_explanations = concepts.read_explanations + 1,"
                "   last_seen_at = excluded.last_seen_at",
                (
                    self._user_id,
                    group_id,
                    term,
                    config.CONCEPT_FAMILIAR,
                    evidence or "defined in a briefing they read",
                    stamp,
                    stamp,
                ),
            )
            row = self._conn.execute(
                "SELECT state, read_explanations FROM concepts"
                " WHERE user_id = ? AND group_id = ? AND term = ?",
                (self._user_id, group_id, term),
            ).fetchone()
            if row["state"] == config.CONCEPT_UNKNOWN:
                self._conn.execute(
                    "UPDATE concepts SET state = ?, evidence = ?"
                    " WHERE user_id = ? AND group_id = ? AND term = ?",
                    (
                        config.CONCEPT_FAMILIAR,
                        evidence or "defined in a briefing they read",
                        self._user_id,
                        group_id,
                        term,
                    ),
                )
                promoted.append(term)
            elif not existed:
                # Fresh row: the INSERT landed it at familiar directly.
                promoted.append(term)
            self._apply_subdomain(group_id, term, label, settled=settled)
        return promoted

    def mark_correct_use(
        self,
        group_id: str,
        terms: Iterable[str],
        evidence: str | None = None,
        subdomains: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """They used these correctly, unprompted -- the strongest signal there is.

        One use is not enough to call it confirmed. It sits near a 0.43
        posterior under standard guess/slip assumptions, and there is a
        specific confound here: the briefing just used the term, so echoing it
        back may be parroting. Two independent uses are required; the first
        lands the term at `provisional` unless it already holds something
        stronger.

        Returns the terms that reached `confirmed` on this call.
        """
        confirmed: list[str] = []
        stamp = now_iso()
        labels = _subdomain_lookup(subdomains)
        for raw in terms:
            term = self.canonical_term(group_id, raw)
            if not term:
                continue
            label = self._label_for(labels, raw, term)
            settled = label is not None and self._term_is_attested(group_id, term)
            self._conn.execute(
                "INSERT INTO concepts (user_id, group_id, term, state, exposure_count,"
                " correct_uses, evidence, first_seen_at, last_seen_at)"
                " VALUES (?,?,?,?,0,1,?,?,?)"
                " ON CONFLICT(user_id, group_id, term) DO UPDATE SET"
                "   correct_uses = concepts.correct_uses + 1,"
                "   last_seen_at = excluded.last_seen_at",
                (
                    self._user_id,
                    group_id,
                    term,
                    config.CONCEPT_PROVISIONAL,
                    evidence or "used correctly once",
                    stamp,
                    stamp,
                ),
            )
            row = self._conn.execute(
                "SELECT state, correct_uses FROM concepts"
                " WHERE user_id = ? AND group_id = ? AND term = ?",
                (self._user_id, group_id, term),
            ).fetchone()
            uses = row["correct_uses"]
            if uses >= config.CORRECT_USES_BEFORE_CONFIRMED:
                self._conn.execute(
                    "UPDATE concepts SET state = ?, evidence = ?, confirmed_at ="
                    " COALESCE(confirmed_at, ?)"
                    " WHERE user_id = ? AND group_id = ? AND term = ?",
                    (
                        config.CONCEPT_CONFIRMED,
                        f"used correctly {uses}x, unprompted",
                        stamp,
                        self._user_id,
                        group_id,
                        term,
                    ),
                )
                confirmed.append(term)
            elif row["state"] == config.CONCEPT_UNKNOWN:
                # First correct use: real evidence, but only weak-tier.
                self._conn.execute(
                    "UPDATE concepts SET state = ?, evidence = ?"
                    " WHERE user_id = ? AND group_id = ? AND term = ?",
                    (
                        config.CONCEPT_PROVISIONAL,
                        "used correctly once (needs a second use to confirm)",
                        self._user_id,
                        group_id,
                        term,
                    ),
                )
            self._apply_subdomain(group_id, term, label, settled=settled)
        return confirmed


    def mark_unknown(
        self,
        group_id: str,
        terms: Iterable[str],
        evidence: str | None = None,
        subdomains: Mapping[str, Any] | None = None,
        *,
        misunderstood: bool = True,
    ) -> None:
        """They revealed a misunderstanding -- revert, however strong the prior state.

        `misunderstood=False` sets the state without counting a miss or
        resetting the counters. It is for a term they asked about that we then
        failed to answer: asking is not getting it wrong.

        This is what stops "didn't ask, so they must know it" from calcifying
        into a wrong belief the system never revisits. It also resets
        `correct_uses`: whatever those uses looked like, this one shows the
        concept was not actually held. `read_explanations` resets for the same
        reason: whatever they read, this shows it did not land.

        The subdomain label is NOT reset: it is a fact about the term, and a
        misunderstanding of `derogation` does not move it out of `appellation
        rules` -- it makes the user one term less known inside it.
        """
        labels = _subdomain_lookup(subdomains)
        for term in terms:
            if not term.strip():
                continue
            canonical = self.canonical_term(group_id, term)
            label = self._label_for(labels, term, canonical)
            settled = label is not None and self._term_is_attested(group_id, canonical)
            self._touch_concept(
                group_id, term, config.CONCEPT_UNKNOWN, evidence or "misunderstood"
            )
            if misunderstood:
                self._conn.execute(
                    "UPDATE concepts SET misunderstandings = misunderstandings + 1,"
                    " correct_uses = 0, read_explanations = 0"
                    " WHERE user_id = ? AND group_id = ? AND term = ?",
                    (self._user_id, group_id, canonical),
                )
            self._apply_subdomain(group_id, canonical, label, settled=settled)

    def ledger(self, group_id: str) -> list[Concept]:
        rows = self._conn.execute(
            "SELECT * FROM concepts WHERE user_id = ? AND group_id = ?"
            " ORDER BY last_seen_at DESC",
            (self._user_id, group_id),
        ).fetchall()
        return [Concept.from_row(r) for r in rows]

    def concept_state(self, group_id: str, term: str) -> str:
        row = self._conn.execute(
            "SELECT state FROM concepts WHERE user_id = ? AND group_id = ? AND term = ?",
            (self._user_id, group_id, self.canonical_term(group_id, term)),
        ).fetchone()
        return row["state"] if row else config.CONCEPT_UNKNOWN

    def proficiency(self, group_id: str) -> str:
        """Coarse band derived from the ledger. Never stored, never decayed."""
        # The band is computed ONLY over terms the user gave us a signal about
        # -- ones they asked about, used correctly, or got wrong.
        #
        # Terms we merely said in front of them are excluded from BOTH sides of
        # the ratio, and that symmetry is the point. Counting them as known
        # lets the system talk its way into thinking it can stop explaining;
        # counting them as encountered-but-unknown lets it talk its way into
        # the opposite. Either way the band would move for reasons that have
        # nothing to do with the user. Silence is not evidence in either
        # direction, so it gets no vote.
        # Derived from config rather than hardcoded: this is the exact rule the
        # redesign changed, so it is the likeliest thing to go stale.
        #
        # The one reading-derived input is `read_explanations`, and it enters
        # both sides at the same threshold: a term explained-and-read at least
        # READ_EXPLANATIONS_BEFORE_BAND times is attested AND known; below
        # that, a `familiar` term votes neither way. `explained` (they asked)
        # is attested from the first ask -- asking is direct evidence they did
        # not hold it then -- and becomes known at the same threshold, so the
        # band grows as explanations accumulate rather than on one exchange.
        # `confirmed` counts unconditionally.
        #
        # The predicates live at module level (`_band_count_columns`) and are
        # shared verbatim with `subdomain_familiarity`, so the per-slice
        # readout is by construction the same arithmetic over a subset.
        row = self._conn.execute(
            f"SELECT {_band_count_columns()}"
            " FROM concepts WHERE user_id = ? AND group_id = ?",
            (*_band_predicate_params(), self._user_id, group_id),
        ).fetchone()
        return config.proficiency_band(
            int(row["known"] or 0), int(row["attested"] or 0)
        )

    def subdomain_familiarity(self, group_id: str) -> dict[str, dict[str, Any]]:
        """The same known/attested arithmetic as `proficiency`, per subdomain.

        Returns {subdomain: {"known": int, "attested": int, "band": str}} for
        every label present in this user's ledger for the group, plus
        `config.UNLABELLED_SUBDOMAIN` for terms no call has labelled yet. The
        band comes from `config.subdomain_band` (lower floors than the group
        band -- a slice is smaller than the whole; see the rationale there).

        This is the human's ask -- "the subjects/subdomains within the group
        that the user is familiar with" -- and note what it is made of: only
        the evidence the ledger already holds. No new inference about the
        user is made here; the same terms count, under the same predicates,
        grouped by the label they were given. Unlabelled terms are reported
        but count toward no other slice, so they can neither inflate nor
        dilute a subdomain they were never assigned to.

        Like the group band, this is a prior for pitch. The per-term ledger
        always wins where it has something to say about the term at hand.
        """
        rows = self._conn.execute(
            f"SELECT COALESCE(NULLIF(subdomain, ''), ?) AS subdomain,"
            f" {_band_count_columns()}"
            " FROM concepts WHERE user_id = ? AND group_id = ?"
            " GROUP BY COALESCE(NULLIF(subdomain, ''), ?)"
            " ORDER BY subdomain",
            (
                config.UNLABELLED_SUBDOMAIN,
                *_band_predicate_params(),
                self._user_id,
                group_id,
                config.UNLABELLED_SUBDOMAIN,
            ),
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            known = int(row["known"] or 0)
            attested = int(row["attested"] or 0)
            out[row["subdomain"]] = {
                "known": known,
                "attested": attested,
                "band": config.subdomain_band(known, attested),
            }
        return out

    def subdomain_labels(self, group_id: str) -> list[str]:
        """The distinct labels already in use for this user and group.

        Handed to the labelling calls so that labels converge: the prompt is
        told to reuse one of these where it fits and invent only when nothing
        does. Excludes the unlabelled bucket, which is not a label.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT subdomain FROM concepts"
            " WHERE user_id = ? AND group_id = ?"
            " AND subdomain IS NOT NULL AND subdomain != ''"
            " ORDER BY subdomain",
            (self._user_id, group_id),
        ).fetchall()
        return [r["subdomain"] for r in rows]


    def behind_count(self, group_id: str) -> int:
        """How many material events the user has not been shown.

        Falling behind is countable, not modelled -- this replaces the decay
        estimate entirely.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM monitor_events WHERE user_id = ? AND group_id = ?"
            " AND reported_as_trigger = 1 AND surfaced_at IS NULL",
            (self._user_id, group_id),
        ).fetchone()
        return int(row["n"] or 0)

    # --- Monitor events ---------------------------------------------------

    def record_event(
        self,
        group_id: str,
        headline: str,
        occurred_at: str | None = None,
        detail: str | None = None,
        source_url: str | None = None,
        source_name: str | None = None,
        origin: str = "live_search",
    ) -> str:
        eid = new_id("evt")
        self._conn.execute(
            "INSERT INTO monitor_events (id, user_id, group_id, occurred_at, headline,"
            " detail, source_url, source_name, origin, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                eid,
                self._user_id,
                group_id,
                occurred_at or now_iso(),
                headline,
                detail,
                source_url,
                source_name,
                origin,
                now_iso(),
            ),
        )
        return eid

    def apply_materiality(
        self,
        event_id: str,
        is_material: bool,
        score: float,
        reason: str,
    ) -> None:
        self._conn.execute(
            "UPDATE monitor_events SET is_material = ?, materiality_score = ?,"
            " materiality_reason = ?, reported_as_trigger = ?"
            " WHERE id = ? AND user_id = ?",
            (
                1 if is_material else 0,
                score,
                reason,
                1 if is_material else 0,
                event_id,
                self._user_id,
            ),
        )

    def get_event(self, event_id: str) -> MonitorEvent | None:
        row = self._conn.execute(
            "SELECT * FROM monitor_events WHERE id = ? AND user_id = ?",
            (event_id, self._user_id),
        ).fetchone()
        return MonitorEvent.from_row(row) if row else None

    def pending_events(self, group_id: str | None = None) -> list[MonitorEvent]:
        """Material events detected but not yet shown to the user.

        This queue is what makes delivery pull-only: the background poller
        fills it whether or not a session is open; the Orchestrator drains it
        when the user actually shows up.
        """
        sql = (
            "SELECT * FROM monitor_events WHERE user_id = ? AND reported_as_trigger = 1"
            " AND surfaced_at IS NULL"
        )
        params: list[Any] = [self._user_id]
        if group_id:
            sql += " AND group_id = ?"
            params.append(group_id)
        sql += " ORDER BY occurred_at"
        rows = self._conn.execute(sql, params).fetchall()
        return [MonitorEvent.from_row(r) for r in rows]

    def untold_events(self, group_id: str) -> list[MonitorEvent]:
        """Material events no briefing has yet been written from.

        This is the ONLY queue `raise_topic` draws on, and it is what makes
        "never repeat a story" structural rather than a prompt request: an
        event is told when an exchange records it as `event_id`, and a told
        event never comes back through here. Ordered by materiality, so the
        one that matters most is told first. (Human decision, checkpoint 4:
        "it should never repeat the story; if there's nothing new to say,
        don't say anything.")
        """
        rows = self._conn.execute(
            "SELECT e.* FROM monitor_events e"
            " WHERE e.user_id = ? AND e.group_id = ? AND e.reported_as_trigger = 1"
            " AND NOT EXISTS (SELECT 1 FROM exchanges x"
            "                 WHERE x.event_id = e.id AND x.user_id = e.user_id)"
            " ORDER BY COALESCE(e.materiality_score, 0) DESC, e.occurred_at DESC",
            (self._user_id, group_id),
        ).fetchall()
        return [MonitorEvent.from_row(r) for r in rows]

    def event_told(self, event_id: str) -> bool:
        """Has a briefing already been written from this event (for this user)?"""
        row = self._conn.execute(
            "SELECT 1 FROM exchanges WHERE event_id = ? AND user_id = ? LIMIT 1",
            (event_id, self._user_id),
        ).fetchone()
        return row is not None

    def recent_events(self, group_id: str, limit: int = 20) -> list[MonitorEvent]:
        rows = self._conn.execute(
            "SELECT * FROM monitor_events WHERE user_id = ? AND group_id = ?"
            " ORDER BY occurred_at DESC LIMIT ?",
            (self._user_id, group_id, limit),
        ).fetchall()
        return [MonitorEvent.from_row(r) for r in rows]

    def mark_engaged(self, event_ids: Iterable[str]) -> None:
        """The user actually replied about these -- distinct from being shown them."""
        stamp = now_iso()
        for eid in event_ids:
            self._conn.execute(
                "UPDATE monitor_events SET engaged_at = COALESCE(engaged_at, ?)"
                " WHERE id = ? AND user_id = ?",
                (stamp, eid, self._user_id),
            )

    def mark_surfaced(self, event_ids: Iterable[str]) -> None:
        stamp = now_iso()
        for eid in event_ids:
            self._conn.execute(
                "UPDATE monitor_events SET surfaced_at = ? WHERE id = ? AND user_id = ?",
                (stamp, eid, self._user_id),
            )

    # --- Exchanges and their threads --------------------------------------
    #
    # An exchange is a briefing plus the conversation that followed it. The
    # system states the substance and stops; the user asks whatever they want,
    # or nothing. So an exchange is opened, then optionally appended to, then
    # closed once quiet -- rather than written once with a question in it and
    # updated once with the answer.

    def open_exchange(
        self,
        group_id: str,
        briefing: str,
        topic: str | None = None,
        event_id: str | None = None,
        explained_terms: Iterable[str] = (),
    ) -> str:
        """Record a briefing we have just put in front of the user.

        `explained_terms` are the terms the briefing DEFINED inline (not every
        term it used). They are stored on the exchange so that, at close, a
        read briefing credits a read explanation to exactly those.

        `event_id` names the monitor event the briefing was written from, when
        there was exactly one. It must be this user's event: linking a thread
        to another user's source material would let their event detail surface
        in this user's replies, so a foreign id refuses loudly rather than
        being stored.
        """
        if event_id is not None:
            owned = self._conn.execute(
                "SELECT 1 FROM monitor_events WHERE id = ? AND user_id = ?",
                (event_id, self._user_id),
            ).fetchone()
            if owned is None:
                raise KeyError(f"No such event for this user: {event_id}")
        eid = new_id("exch")
        stamp = now_iso()
        self._conn.execute(
            "INSERT INTO exchanges (id, user_id, group_id, raised_at, topic, briefing,"
            " event_id, explained_terms, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                eid,
                self._user_id,
                group_id,
                stamp,
                topic,
                briefing,
                event_id,
                json.dumps([t for t in explained_terms if str(t).strip()]),
                stamp,
            ),
        )
        return eid

    def record_reading(
        self,
        exchange_id: str,
        dwell_ms: int | None = None,
        scroll_fraction: float | None = None,
        source: str = "observed",
    ) -> None:
        """Record how the briefing was read. Attention, never comprehension.

        This writes to the exchange row and NOWHERE ELSE. In particular it does
        not touch `concepts`, and it deliberately holds no code path that
        could: the derived `read_quality` comes from a module-level function
        with no scope, so there is nothing here that could promote a term.

        Someone staring at a briefing for four minutes may have understood
        every word or may have left the tab open and gone to lunch. The two are
        indistinguishable from this signal, and the ledger is the one place in
        the system where that ambiguity is not acceptable.

        `source` records how the signal was obtained -- `observed` for a real
        client that can see dwell and scroll, `response_time_proxy` for the CLI
        (which cannot: text simply prints), `simulated` for the eval harness.
        Recording which is which is the difference between a metric and a
        number: a mix of measured and proxied values, averaged without knowing
        the split, is not measuring anything.
        """
        if source not in ("observed", "response_time_proxy", "simulated"):
            raise ValueError(
                "reading_source must be 'observed', 'response_time_proxy' or "
                f"'simulated', got {source!r}"
            )
        row = self._conn.execute(
            "SELECT briefing, group_id, explained_terms FROM exchanges"
            " WHERE id = ? AND user_id = ?",
            (exchange_id, self._user_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"No such exchange for this user: {exchange_id}")
        chars = len(row["briefing"] or "")
        quality = read_quality(dwell_ms, scroll_fraction, chars)
        attention = attention_score(dwell_ms, scroll_fraction, chars)
        # A skip starts out unresolved. If the ledger ALREADY holds evidence
        # that this user knew every term the briefing defined (asked-and-
        # explained, used correctly, or confirmed -- never merely `familiar`
        # or exposed), the skip is classified informed at once: that is prior
        # evidence about them, not an inference from the skip. Otherwise it
        # waits for a later thread to say what it meant.
        skip_kind = None
        if quality == "skipped":
            skip_kind = "unresolved"
            glossed = _loads_terms(row["explained_terms"]) if "explained_terms" in row.keys() else []
            if glossed and all(
                self._attested_known(row["group_id"], t) for t in glossed
            ):
                skip_kind = "informed"
        self._conn.execute(
            "UPDATE exchanges SET dwell_ms = ?, scroll_fraction = ?, read_quality = ?,"
            " attention = ?, skip_kind = ?, reading_source = ?"
            " WHERE id = ? AND user_id = ?",
            (
                dwell_ms,
                scroll_fraction,
                quality,
                attention,
                skip_kind,
                source,
                exchange_id,
                self._user_id,
            ),
        )

    def _attested_known(self, group_id: str, term: str) -> bool:
        """Held on the user's OWN evidence: asked-and-explained, used correctly
        (provisional/confirmed). `familiar` does not count -- it came from
        reading, and reading must not vouch for reading."""
        row = self._conn.execute(
            "SELECT state FROM concepts WHERE user_id = ? AND group_id = ? AND term = ?",
            (self._user_id, group_id, self.canonical_term(group_id, term)),
        ).fetchone()
        return bool(row) and row["state"] in (
            config.CONCEPT_PROVISIONAL,
            config.CONCEPT_EXPLAINED,
            config.CONCEPT_CONFIRMED,
        )

    def resolve_skips(
        self,
        group_id: str,
        known_now: Iterable[str],
        lacking_now: Iterable[str],
    ) -> dict[str, str]:
        """Say what earlier skips meant, from evidence that has just arrived.

        Called at thread close with the terms this thread showed the user
        holds (`known_now`: used correctly) and lacks (`lacking_now`: asked
        about, or got wrong). Every still-unresolved skipped briefing in the
        group whose defined terms overlap that evidence is classified:
        `informed` if the evidence about its terms is all positive, `lazy` if
        any of it is negative -- a person who skipped a definition and later
        had to ask for it did not know it, whatever else they knew. Skips with
        no overlapping evidence stay unresolved; silence resolves nothing.

        Returns {exchange_id: kind} for what changed. Writes nothing to the
        ledger: the ledger already recorded the evidence; this labels the
        reading act in the light of it.
        """
        known = {self.canonical_term(group_id, t) for t in known_now if t.strip()}
        lacking = {self.canonical_term(group_id, t) for t in lacking_now if t.strip()}
        if not known and not lacking:
            return {}
        rows = self._conn.execute(
            "SELECT id, explained_terms FROM exchanges WHERE user_id = ? AND group_id = ?"
            " AND read_quality = 'skipped' AND skip_kind = 'unresolved'",
            (self._user_id, group_id),
        ).fetchall()
        resolved: dict[str, str] = {}
        for row in rows:
            glossed = {
                self.canonical_term(group_id, t) for t in _loads_terms(row["explained_terms"])
            }
            if not glossed:
                continue
            if glossed & lacking:
                kind = "lazy"
            elif glossed & known:
                kind = "informed"
            else:
                continue
            self._conn.execute(
                "UPDATE exchanges SET skip_kind = ? WHERE id = ? AND user_id = ?",
                (kind, row["id"], self._user_id),
            )
            resolved[row["id"]] = kind
        return resolved

    def reading_pattern(self, group_id: str) -> dict[str, Any]:
        """How this user's skips have tended to resolve. A prior, not a fact.

        `p_informed` is a Beta(1,1)-smoothed share of RESOLVED skips that were
        informed, so with no evidence it sits at 0.5 and moves only as skips
        get labelled. Consumers (the briefing) may lean on it only once
        `resolved` is at least 3 -- below that it is noise with a decimal
        point.
        """
        row = self._conn.execute(
            "SELECT"
            " SUM(CASE WHEN skip_kind = 'informed' THEN 1 ELSE 0 END) AS informed,"
            " SUM(CASE WHEN skip_kind = 'lazy' THEN 1 ELSE 0 END) AS lazy,"
            " SUM(CASE WHEN skip_kind = 'unresolved' THEN 1 ELSE 0 END) AS unresolved"
            " FROM exchanges WHERE user_id = ? AND group_id = ?",
            (self._user_id, group_id),
        ).fetchone()
        informed = int(row["informed"] or 0)
        lazy = int(row["lazy"] or 0)
        unresolved = int(row["unresolved"] or 0)
        return {
            "informed_skips": informed,
            "lazy_skips": lazy,
            "unresolved_skips": unresolved,
            "resolved": informed + lazy,
            "p_informed": round((informed + 1) / (informed + lazy + 2), 3),
        }

    def add_turn(self, exchange_id: str, speaker: str, text: str) -> str:
        """Append one utterance to a thread. Returns the turn id.

        `seq` is assigned here rather than by the caller, under the exchange's
        write lock, so two writers appending at once cannot collide on the
        unique (exchange_id, seq) index.
        """
        if speaker not in ("user", "system"):
            raise ValueError(f"speaker must be 'user' or 'system', got {speaker!r}")
        with self.transaction():
            owned = self._conn.execute(
                "SELECT 1 FROM exchanges WHERE id = ? AND user_id = ?",
                (exchange_id, self._user_id),
            ).fetchone()
            if owned is None:
                raise KeyError(f"No such exchange for this user: {exchange_id}")
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS top FROM turns"
                " WHERE exchange_id = ? AND user_id = ?",
                (exchange_id, self._user_id),
            ).fetchone()
            tid = new_id("turn")
            self._conn.execute(
                "INSERT INTO turns (id, exchange_id, user_id, seq, speaker, text,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    tid,
                    exchange_id,
                    self._user_id,
                    int(row["top"]) + 1,
                    speaker,
                    text,
                    now_iso(),
                ),
            )
        return tid

    def thread_turns(self, exchange_id: str) -> list[Turn]:
        """Every turn of one thread, in order. Empty is a normal result."""
        rows = self._conn.execute(
            "SELECT * FROM turns WHERE exchange_id = ? AND user_id = ? ORDER BY seq",
            (exchange_id, self._user_id),
        ).fetchall()
        return [Turn.from_row(r) for r in rows]

    def close_exchange(
        self,
        exchange_id: str,
        understood: list[str],
        not_understood: list[str],
        asked_about: list[str],
        already_knew: bool | None,
    ) -> None:
        """The thread went quiet; store what the whole of it revealed.

        The system never decides a conversation is finished -- the user stops,
        and this records the evidence over everything they said before they
        did. Closing twice is allowed and simply overwrites: a user who comes
        back to the same thread produces more evidence, not a second exchange.

        Refuses loudly on another user's exchange, matching `add_turn` and
        `record_reading`. The older writes silently affected zero rows, which
        is safe but hides a caller bug; the thread API is consistently loud.
        """
        owned = self._conn.execute(
            "SELECT 1 FROM exchanges WHERE id = ? AND user_id = ?",
            (exchange_id, self._user_id),
        ).fetchone()
        if owned is None:
            raise KeyError(f"No such exchange for this user: {exchange_id}")
        self._conn.execute(
            "UPDATE exchanges SET closed_at = ?, understood = ?, not_understood = ?,"
            " asked_about = ?, already_knew = ? WHERE id = ? AND user_id = ?",
            (
                now_iso(),
                json.dumps(understood),
                json.dumps(not_understood),
                json.dumps(asked_about),
                None if already_knew is None else int(already_knew),
                exchange_id,
                self._user_id,
            ),
        )

    def exchange_history(self, group_id: str, limit: int = 20) -> list[Exchange]:
        rows = self._conn.execute(
            "SELECT * FROM exchanges WHERE user_id = ? AND group_id = ?"
            " ORDER BY raised_at DESC LIMIT ?",
            (self._user_id, group_id, limit),
        ).fetchall()
        return [Exchange.from_row(r) for r in rows]

    def get_exchange(self, exchange_id: str) -> Exchange | None:
        row = self._conn.execute(
            "SELECT * FROM exchanges WHERE id = ? AND user_id = ?",
            (exchange_id, self._user_id),
        ).fetchone()
        return Exchange.from_row(row) if row else None

    def user_turn_count(self, group_id: str) -> int:
        """How many times this user has actually spoken in this group.

        The measure of engagement is that they said something -- not that a
        briefing was delivered, and not that it was read. Reading behaviour is
        recorded on the exchange and deliberately plays no part here.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM turns t JOIN exchanges e ON e.id = t.exchange_id"
            " WHERE t.user_id = ? AND e.group_id = ? AND t.speaker = 'user'",
            (self._user_id, group_id),
        ).fetchone()
        return int(row["n"] or 0)

    def open_exchanges(self, group_id: str | None = None) -> list[Exchange]:
        """Threads raised but not yet closed."""
        sql = "SELECT * FROM exchanges WHERE user_id = ? AND closed_at IS NULL"
        params: list[Any] = [self._user_id]
        if group_id:
            sql += " AND group_id = ?"
            params.append(group_id)
        sql += " ORDER BY raised_at"
        return [Exchange.from_row(r) for r in self._conn.execute(sql, params).fetchall()]
