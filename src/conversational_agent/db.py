"""SQLite schema and connection handling.

Every table holding user data carries a `user_id` column. The isolation
guarantee the spec calls its top risk is enforced one layer up, in `store.py`,
where every read and write is required to bind a user_id -- but the schema is
shaped to make that enforceable and to make a violation visible.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('real', 'persona')),
    display_name TEXT NOT NULL,
    profile_json TEXT,              -- persona behaviour parameters; NULL for real users
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id                    TEXT PRIMARY KEY,
    user_id               TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                  TEXT NOT NULL,
    description           TEXT,
    poll_interval_minutes INTEGER NOT NULL,
    last_polled_at        TEXT,
    created_at            TEXT NOT NULL,
    UNIQUE (user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_groups_user ON groups(user_id);

CREATE TABLE IF NOT EXISTS goals (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id    TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    deadline    TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('active', 'completed', 'expired')),
    created_at  TEXT NOT NULL,
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_goals_user_group ON goals(user_id, group_id);
-- At most one active goal per group (confirmed with the human; the spec asked
-- for this assumption to be flagged). Partial unique index: only 'active' rows
-- participate, so historical completed/expired goals are unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS idx_goals_one_active
    ON goals(group_id) WHERE status = 'active';

-- The concept ledger: what the user has demonstrated about each term, and the
-- evidence behind it. Deliberately NOT a score -- "did they ask what a vintage
-- is" is an observed fact; "they are at 47/100" is an inference that then has
-- to be validated. No decay: concepts do not go stale once known.
CREATE TABLE IF NOT EXISTS concepts (
    user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id       TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    term           TEXT NOT NULL,
    state          TEXT NOT NULL
                   CHECK (state IN ('unknown', 'provisional', 'familiar',
                                    'explained', 'confirmed')),
    -- Times the term has been used in front of them. Recorded so the briefing
    -- knows whether a term is new to the conversation -- NOT as evidence of
    -- understanding. Measured against ground truth, silence was anti-predictive.
    exposure_count INTEGER NOT NULL DEFAULT 0,
    -- Times the USER used the term correctly, unprompted. Distinct from
    -- exposure_count, which counts times WE used it in front of them.
    correct_uses   INTEGER NOT NULL DEFAULT 0,
    -- Times they revealed a misunderstanding. Negative user evidence, and it
    -- must be counted: it is what keeps the band honest rather than merely
    -- optimistic.
    misunderstandings INTEGER NOT NULL DEFAULT 0,
    -- Times an explanation of the term was put in front of them AND read:
    -- a gloss inside a briefing they read or skimmed, or an answer to their
    -- own question. This is the one place reading behaviour reaches the
    -- ledger, and only ever together with an explanation having been given.
    -- One makes the term `familiar` (no more re-glossing); two count toward
    -- the band. Reset, like correct_uses, by a revealed misunderstanding.
    read_explanations INTEGER NOT NULL DEFAULT 0,
    -- Which slice of the group this term belongs to: a short heading a
    -- specialist would file it under ('appellation rules', 'transfer
    -- market'). A fact about the TERM, assigned by the same calls that name
    -- it; it never changes a state and never moves the group band. It exists
    -- so the ledger can be read per slice: the per-term ledger cannot know
    -- that a viticulturist holds a viticulture term she has never been seen
    -- using, but her known/attested counts inside that slice can.
    subdomain      TEXT,
    evidence       TEXT,           -- why the state is what it is
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    explained_at   TEXT,
    confirmed_at   TEXT,
    PRIMARY KEY (user_id, group_id, term)
);
CREATE INDEX IF NOT EXISTS idx_concepts_state ON concepts(user_id, group_id, state);

CREATE TABLE IF NOT EXISTS monitor_events (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id            TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    occurred_at         TEXT NOT NULL,
    headline            TEXT NOT NULL,
    detail              TEXT,
    source_url          TEXT,
    source_name         TEXT,
    is_material         INTEGER,      -- 0/1, NULL until judged
    materiality_score   REAL,
    materiality_reason  TEXT,
    -- Where this event came from. Persona users are fed a simulated feed by
    -- the eval harness rather than live search, and the two must stay
    -- distinguishable: a metric computed over a mix of real and injected
    -- events without knowing which is which is not measuring anything.
    origin              TEXT NOT NULL DEFAULT 'live_search'
                        CHECK (origin IN ('live_search', 'simulated_feed')),
    -- Reporting to the Orchestrator is a row state, not a call. The Monitor
    -- never invokes the Orchestrator and never touches the user.
    reported_as_trigger INTEGER NOT NULL DEFAULT 0,
    surfaced_at         TEXT,         -- when it was PUT IN FRONT OF THEM (pull-only)
    -- When the user actually replied about it. `surfaced_at` is delivery, not
    -- comprehension: Apple MPP inflates ~half of reported email opens, the IAB
    -- had to invent "viewability" because "served" meant nothing, and 59% of
    -- shared links are never clicked. This column is the real signal, and it
    -- is the only one of the two that may ever inform the concept ledger.
    engaged_at          TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user_group ON monitor_events(user_id, group_id);
CREATE INDEX IF NOT EXISTS idx_events_pending
    ON monitor_events(user_id, group_id, surfaced_at);

-- One briefing and the conversation that followed it.
--
-- The system does not ask the user questions. It states what happened and
-- stops; the user asks whatever they want, or nothing. What someone chooses to
-- ask locates them far more precisely than an answer to a question we picked --
-- "what's a derogation?" and "was that a chaptalisation year?" come from very
-- different people, and neither is a reaction to our framing.
CREATE TABLE IF NOT EXISTS exchanges (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id        TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    raised_at       TEXT NOT NULL,
    topic           TEXT,
    briefing        TEXT NOT NULL,  -- the substance; ends when the substance ends

    -- How the briefing was read. ATTENTION, NEVER COMPREHENSION -- this must
    -- not reach the concept ledger. Display metrics are famously worthless as
    -- comprehension proxies (Apple MPP inflates ~half of reported email opens;
    -- the IAB had to invent "viewability" because "served" meant nothing).
    -- Good enough to close an item out of the "new" count and to report
    -- engagement. Nothing more.
    dwell_ms        INTEGER,
    scroll_fraction REAL,           -- 0.0-1.0 of the briefing actually scrolled past
    read_quality    TEXT CHECK (read_quality IS NULL OR read_quality IN
                        ('skipped', 'skimmed', 'read', 'studied')),
    -- Where the reading signal came from. The CLI cannot see scroll position or
    -- dwell -- text simply prints -- so it records time-to-respond and says so,
    -- rather than passing a weak proxy off as the real measurement.
    reading_source  TEXT CHECK (reading_source IS NULL OR reading_source IN
                        ('observed', 'response_time_proxy', 'simulated')),

    closed_at       TEXT,           -- when the thread went quiet
    -- Evidence extracted across the WHOLE thread, not from a single reply.
    understood      TEXT,
    not_understood  TEXT,
    asked_about     TEXT,
    already_knew    INTEGER,
    created_at      TEXT NOT NULL,
    -- 0-10 continuous readout of the same two proxies as read_quality.
    attention       REAL,
    -- What a skip meant, resolved later from evidence: informed | lazy |
    -- unresolved. NULL when the briefing was not skipped.
    skip_kind       TEXT CHECK (skip_kind IS NULL OR skip_kind IN
                        ('informed', 'lazy', 'unresolved')),
    -- JSON list: the terms the briefing defined inline. Stored so that, at
    -- close, the ledger can credit a READ explanation to exactly those terms
    -- and nothing else the briefing merely mentioned.
    explained_terms TEXT,
    -- The event this briefing was written from, when it was written from one.
    -- Nullable: a briefing can be composed over several queued events or over
    -- "whatever is ongoing", and then there is no single source to name. When
    -- set, it is what lets a thread reply see the source material rather than
    -- only the briefing's paraphrase of it -- the first live run had the reply
    -- call refuse a figure that was sitting in the event detail all along.
    event_id        TEXT REFERENCES monitor_events(id) ON DELETE SET NULL
);

-- The turns of one conversation. The user speaks first after the briefing, if
-- they speak at all; a thread with no user turns is a perfectly normal outcome.
CREATE TABLE IF NOT EXISTS turns (
    id          TEXT PRIMARY KEY,
    exchange_id TEXT NOT NULL REFERENCES exchanges(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    speaker     TEXT NOT NULL CHECK (speaker IN ('user', 'system')),
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (exchange_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_turns_exchange ON turns(exchange_id, seq);
CREATE INDEX IF NOT EXISTS idx_exchanges_user_group ON exchanges(user_id, group_id);

-- Observability. One row per judgment-point LLM call, ever. This is what
-- turns "the metric got worse" into "here is the exact call that got it
-- wrong, what it saw, what it decided, and which prompt version produced it."
CREATE TABLE IF NOT EXISTS judgment_log (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    judgment_point  TEXT NOT NULL,
    user_id         TEXT,           -- NULL only for system-level calls
    group_id        TEXT,
    prompt_version  TEXT NOT NULL,
    model           TEXT NOT NULL,
    effort          TEXT,
    input_json      TEXT NOT NULL,  -- exactly what the judgment call was given
    verdict_json    TEXT,           -- the structured verdict it returned
    reasoning       TEXT,           -- the model's own stated reasoning
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    latency_ms      INTEGER,
    error           TEXT,           -- populated instead of verdict on failure
    run_id          TEXT            -- set when called from an eval run
);
CREATE INDEX IF NOT EXISTS idx_judgment_point ON judgment_log(judgment_point, created_at);
CREATE INDEX IF NOT EXISTS idx_judgment_run ON judgment_log(run_id);
CREATE INDEX IF NOT EXISTS idx_judgment_user ON judgment_log(user_id, group_id);

-- Eval-harness run records, so a regression is a diff between two runs
-- rather than a remembered number.
CREATE TABLE IF NOT EXISTS eval_runs (
    id           TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    suite        TEXT NOT NULL,      -- 'personas' | 'live_search'
    config_json  TEXT NOT NULL,      -- model routing + prompt versions in force
    metrics_json TEXT,
    passed       INTEGER
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the store, creating the schema if needed."""
    target = Path(path) if path is not None else config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None)  # autocommit; we manage txns
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL keeps the background poller writing while a CLI session reads,
    # which is the concurrency shape this system actually has.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` is a
# no-op on an existing database, so every additive column also needs an ALTER
# here or a live store silently keeps the old shape. Each entry is idempotent:
# it is skipped when the column is already present.
_ADDITIVE_COLUMNS = (
    (
        "exchanges",
        "event_id",
        "ALTER TABLE exchanges ADD COLUMN event_id TEXT"
        " REFERENCES monitor_events(id) ON DELETE SET NULL",
    ),
    (
        "exchanges",
        "explained_terms",
        "ALTER TABLE exchanges ADD COLUMN explained_terms TEXT",
    ),
    (
        "concepts",
        "read_explanations",
        "ALTER TABLE concepts ADD COLUMN read_explanations INTEGER NOT NULL DEFAULT 0",
    ),
    # Continuous attention readout (0-10) next to the coarse bucket. Same
    # inputs, same wall: attention, never comprehension.
    ("exchanges", "attention", "ALTER TABLE exchanges ADD COLUMN attention REAL"),
    # What a SKIP turned out to mean, decided later from ledger evidence about
    # the terms the skipped briefing defined: 'informed' (they already held
    # them), 'lazy' (they later showed they did not), 'unresolved' (no evidence
    # yet). Written by the system from evidence, never from the skip itself.
    ("exchanges", "skip_kind", "ALTER TABLE exchanges ADD COLUMN skip_kind TEXT"),
    # Subdomain label per term. Nullable: rows written before the label
    # existed stay unlabelled until the next call that names the term, and are
    # reported under `(unlabelled)` rather than guessed.
    ("concepts", "subdomain", "ALTER TABLE concepts ADD COLUMN subdomain TEXT"),
)

# The `familiar` state was added after concepts first shipped. SQLite cannot
# alter a CHECK constraint in place, so a store created before it has to have
# the table rebuilt: copy out, recreate from the current SCHEMA, copy back.
# Detected from the stored DDL rather than a version number, so a database
# that already has it is left alone.
_CONCEPTS_REBUILD_MARKER = "'familiar'"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _rebuild_concepts_if_stale(conn: sqlite3.Connection) -> None:
    """Rebuild `concepts` under the current CHECK constraint, resumably.

    The old table is renamed aside, the current schema recreates `concepts`,
    and the rows are copied back. If a previous attempt stopped between those
    steps, `concepts_old` is still there and this picks up from the copy:
    the rebuild is finished only when `concepts_old` is gone, so an
    interrupted run can never leave the ledger stranded in a table nothing
    reads. A row in a state the current schema no longer has is carried over
    as `unknown` -- the honest reading of evidence we can no longer interpret
    -- rather than failing the whole copy.
    """
    if not _table_exists(conn, "concepts_old"):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'concepts'"
        ).fetchone()
        if row is None or _CONCEPTS_REBUILD_MARKER in (row["sql"] or ""):
            return
        # The index would follow the renamed table and then die with it, and
        # IF NOT EXISTS would skip recreating it by name. Drop it first.
        conn.execute("DROP INDEX IF EXISTS idx_concepts_state")
        conn.execute("ALTER TABLE concepts RENAME TO concepts_old")

    # Recreates `concepts` (and its index) in the current shape; every other
    # statement in the schema is IF NOT EXISTS and a no-op.
    conn.executescript(SCHEMA)
    new_cols = {r["name"] for r in conn.execute("PRAGMA table_info(concepts)").fetchall()}
    cols = [
        r["name"]
        for r in conn.execute("PRAGMA table_info(concepts_old)").fetchall()
        if r["name"] in new_cols
    ]
    valid_states = ", ".join(f"'{state}'" for state in config.CONCEPT_STATES)
    select = ", ".join(
        f"CASE WHEN state IN ({valid_states}) THEN state ELSE '{config.CONCEPT_UNKNOWN}' END"
        if col == "state"
        else col
        for col in cols
    )
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(
            f"INSERT OR IGNORE INTO concepts ({', '.join(cols)}) SELECT {select} FROM concepts_old"
        )
        conn.execute("DROP TABLE concepts_old")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _ADDITIVE_COLUMNS:
        present = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in present:
            conn.execute(ddl)
    _rebuild_concepts_if_stale(conn)
