"""Builder-facing payloads: the LLMOps console, as data.

Everything the CLI's operator commands print -- `ledger`, `group show`'s
diagnostics, `models`, `prompts`, `traces`, `trace`, `stats`, `runs` -- with
the same scoping rules: trace views are the active user's by default (plus
system-level rows), `all_users` is the explicit opt-in, and another user's row
reports as not found rather than revealing that it exists.

Nothing here makes an LLM call or needs credentials, so a failed live run can
always be diagnosed afterwards. This is the one place in the web surface that
shows the derived band; it is a diagnostic for the builder and the reader
module never touches it.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from .. import config, judgment, reporting
from ..models import Group, User
from ..store import Store, UserScope
from . import ApiError

STATE_ORDER = config.STATES_STRONGEST_FIRST
STATE_GLOSS = config.STATE_GLOSS


def _json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


# --- Ledger ----------------------------------------------------------------


def group_console(scope: UserScope, group: Group) -> dict[str, Any]:
    """What the system believes about this user in this group, and why."""
    ledger = scope.ledger(group.id)
    counts = {state: 0 for state in STATE_ORDER}
    for concept in ledger:
        counts[concept.state] = counts.get(concept.state, 0) + 1

    told = {e.event_id for e in scope.exchange_history(group.id, limit=500) if e.event_id}
    events = [
        {
            "id": e.id,
            "occurred_at": e.occurred_at,
            "headline": e.headline,
            "source_name": e.source_name,
            "source_url": e.source_url,
            "is_material": e.is_material,
            "materiality_score": e.materiality_score,
            "materiality_reason": e.materiality_reason,
            "surfaced": bool(e.surfaced_at),
            "told": e.id in told,
            "origin": e.origin,
        }
        for e in scope.recent_events(group.id, limit=50)
    ]

    exchanges = []
    for x in scope.exchange_history(group.id, limit=50):
        turns = scope.thread_turns(x.id)
        exchanges.append(
            {
                "id": x.id,
                "raised_at": x.raised_at,
                "closed_at": x.closed_at,
                "topic": x.topic,
                "briefing": x.briefing,
                "explained_terms": x.explained_terms,
                "user_turns": sum(1 for t in turns if t.is_user),
                "read_quality": x.read_quality,
                "attention": x.attention,
                "skip_kind": x.skip_kind,
                "dwell_ms": x.dwell_ms,
                "scroll_fraction": x.scroll_fraction,
                "reading_source": x.reading_source,
                "understood": x.understood,
                "not_understood": x.not_understood,
                "asked_about": x.asked_about,
                "already_knew": x.already_knew,
            }
        )

    return {
        "group": {"id": group.id, "name": group.name, "description": group.description},
        "proficiency": scope.proficiency(group.id),
        "known": sum(1 for c in ledger if c.is_known),
        "total": len(ledger),
        "state_counts": [
            {"state": s, "count": counts.get(s, 0), "gloss": STATE_GLOSS.get(s, "")}
            for s in STATE_ORDER
        ],
        "known_states": list(config.KNOWN_STATES),
        "subdomains": [
            {"subdomain": name, **values}
            for name, values in scope.subdomain_familiarity(group.id).items()
        ],
        "reading_pattern": scope.reading_pattern(group.id),
        "new": scope.behind_count(group.id),
        "ledger": [
            {
                "term": c.term,
                "state": c.state,
                "subdomain": c.subdomain,
                "exposure_count": c.exposure_count,
                "correct_uses": c.correct_uses,
                "misunderstandings": c.misunderstandings,
                "read_explanations": c.read_explanations,
                "counts_toward_band": c.counts_toward_band,
                "first_seen_at": c.first_seen_at,
                "last_seen_at": c.last_seen_at,
                "evidence": c.evidence,
            }
            for c in sorted(
                ledger,
                key=lambda c: (
                    STATE_ORDER.index(c.state) if c.state in STATE_ORDER else 99,
                    c.term,
                ),
            )
        ],
        "events": events,
        "exchanges": exchanges,
    }


# --- Models and prompts ----------------------------------------------------


def models() -> dict[str, Any]:
    rows = []
    for point in config.ROUTED_CALLS:
        try:
            version = judgment.load_prompt(point).version
        except judgment.JudgmentError:
            version = "MISSING"
        overrides = [
            label
            for label, env in (("model", "MODEL"), ("effort", "EFFORT"))
            if os.environ.get(f"{env}_{point.upper()}")
        ]
        local = config.local_model_enabled() and point in config.local_model_points()
        rows.append(
            {
                "point": point,
                "kind": "judgment" if point in config.ALL_JUDGMENT_POINTS else "generative",
                "model": config.model_for(point),
                "local": local,
                "effort": config.effort_for(point),
                "prompt_version": version,
                "overrides": overrides,
            }
        )
    return {
        "rows": rows,
        "judgment_points": len(config.ALL_JUDGMENT_POINTS),
        "routed_calls": len(config.ROUTED_CALLS),
    }


def prompts() -> list[dict[str, Any]]:
    rows = []
    for point in config.ROUTED_CALLS:
        path = config.PROMPTS_DIR / f"{point}.md"
        if not path.exists():
            rows.append({"point": point, "version": "MISSING", "file": path.name})
            continue
        prompt = judgment.load_prompt(point)
        rows.append(
            {
                "point": point,
                "version": prompt.version,
                "chars": len(prompt.text),
                "modified": datetime.fromtimestamp(
                    path.stat().st_mtime, UTC
                ).isoformat(),
                "file": path.name,
            }
        )
    return rows


def prompt(point: str) -> dict[str, Any]:
    if point not in config.ROUTED_CALLS:
        raise ApiError(404, f"Unknown judgment point {point!r}.")
    try:
        loaded = judgment.load_prompt(point)
    except judgment.JudgmentError as exc:
        raise ApiError(404, str(exc)) from None
    return {
        "point": loaded.point,
        "version": loaded.version,
        "text": loaded.text,
        "file": f"{point}.md",
    }


# --- Traces ----------------------------------------------------------------


def _trace_summary(row: Any) -> dict[str, Any]:
    in_tok = row["input_tokens"] or 0
    out_tok = row["output_tokens"] or 0
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "point": row["judgment_point"],
        "model": row["model"],
        "effort": row["effort"],
        "prompt_version": row["prompt_version"],
        "latency_ms": row["latency_ms"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cost": reporting.cost(row["model"], in_tok, out_tok),
        "error": row["error"],
        "reasoning": row["reasoning"],
        "user_id": row["user_id"],
        "group_id": row["group_id"],
        "run_id": row["run_id"],
    }


def traces(
    store: Store,
    user: User | None,
    point: str | None = None,
    run_id: str | None = None,
    all_users: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    try:
        rows, scope_label = reporting.trace_rows(
            store, user=user, point=point, run_id=run_id, all_users=all_users, limit=limit
        )
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    out = []
    for row in rows:
        item = _trace_summary(row)
        verdict = row["verdict_json"] or ""
        item["verdict_preview"] = " ".join(verdict.split())[:160]
        out.append(item)
    return {"scope": scope_label, "rows": out, "points": list(config.ROUTED_CALLS)}


def trace(store: Store, user: User | None, log_id: str, all_users: bool = False) -> dict[str, Any]:
    row = store.conn.execute(
        "SELECT * FROM judgment_log WHERE id = ?", (log_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "No such judgment call.")
    if not all_users and row["user_id"] is not None:
        if user is None or row["user_id"] != user.id:
            raise ApiError(404, "No such judgment call for this user.")
    item = _trace_summary(row)
    item["input"] = _json(row["input_json"])
    item["verdict"] = _json(row["verdict_json"])
    return item


def recent_calls(store: Store, user_id: str | None, since_iso: str) -> list[dict[str, Any]]:
    """The judgment calls a single request made, for the 'behind the scenes'
    strip. Summary fields only: point, model, time, cost, whether it failed."""
    rows = store.conn.execute(
        "SELECT * FROM judgment_log WHERE created_at >= ?"
        " AND (user_id = ? OR user_id IS NULL) ORDER BY created_at",
        (since_iso, user_id),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "point": r["judgment_point"],
            "model": r["model"],
            "latency_ms": r["latency_ms"],
            "cost": reporting.cost(r["model"], r["input_tokens"] or 0, r["output_tokens"] or 0),
            "error": bool(r["error"]),
        }
        for r in rows
    ]


def stats(
    store: Store,
    user: User | None,
    point: str | None = None,
    run_id: str | None = None,
    all_users: bool = False,
) -> dict[str, Any]:
    try:
        rows, scope_label = reporting.trace_rows(
            store, user=user, point=point, run_id=run_id, all_users=all_users,
            limit=reporting.ALL_ROWS,
        )
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    return {
        "scope": scope_label,
        **reporting.aggregate_stats(rows),
        "pricing": [
            {"model": model, "input_per_million": price[0], "output_per_million": price[1]}
            for model, price in sorted(config.PRICES_PER_MTOK.items())
        ],
    }


def runs(store: Store, limit: int = 30) -> list[dict[str, Any]]:
    out = []
    for row in store.eval_runs(limit=limit):
        metrics = _json(row["metrics_json"])
        scalars = (
            {k: v for k, v in metrics.items() if isinstance(v, (int, float, str, bool))}
            if isinstance(metrics, dict)
            else {}
        )
        out.append(
            {
                "id": row["id"],
                "suite": row["suite"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "passed": None if row["passed"] is None else bool(row["passed"]),
                "metrics": scalars,
            }
        )
    return out
