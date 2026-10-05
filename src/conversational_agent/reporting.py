"""Read-side helpers shared by the two interfaces (CLI and web).

Pure functions over the store: cost arithmetic, trace scoping, call-stat
aggregation, and two small pieces of input/output logic both surfaces need.
Nothing here makes a model call or prints; each interface formats the result
its own way, which is what keeps the terminal and the browser reporting the
same numbers under the same scoping rules.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from . import config
from .models import User
from .store import Store, UserScope

# "No limit" for aggregate views, which want the whole history, not one page.
ALL_ROWS = 1_000_000


def cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """USD for one call, or None for a model with no price entry.

    Never a guessed price: a wrong cost is worse than no cost when it drives
    model choice.
    """
    price = config.PRICES_PER_MTOK.get(model)
    if price is None:
        return None
    return (input_tokens / 1_000_000.0) * price[0] + (output_tokens / 1_000_000.0) * price[1]


def p95(values: list[float]) -> float:
    """Nearest-rank p95; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


def last_engaged(scope: UserScope, group_id: str) -> str | None:
    """When the user last actually said something in this group.

    The last reply, not the last briefing: a briefing nobody answered is not
    engagement, and the honest answer is the one that shows a group has gone
    quiet.
    """
    for exchange in scope.exchange_history(group_id, limit=20):
        if any(t.is_user for t in scope.thread_turns(exchange.id)):
            return exchange.raised_at
    return None


def parse_deadline(raw: str) -> str:
    """`YYYY-MM-DD` or a full ISO timestamp -> UTC-aware ISO string.

    A bare date means the end of that day. Raises ValueError on anything else.
    Normalising here matters because the store compares deadlines against a
    timezone-aware clock.
    """
    text = (raw or "").strip()
    if len(text) == 10:
        moment = datetime.fromisoformat(text).replace(
            hour=23, minute=59, second=59, tzinfo=UTC
        )
    else:
        moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def trace_rows(
    store: Store,
    *,
    user: User | None,
    point: str | None = None,
    run_id: str | None = None,
    all_users: bool = False,
    limit: int = 100,
) -> tuple[list[Any], str]:
    """Judgment-log rows plus a one-line description of the scope applied.

    Default scope is one user's rows plus system-level rows (no user id).
    `run_id` narrows to one eval run instead, and `all_users` is the explicit
    opt-in to the whole log, so no view spans users by accident. Raises
    ValueError for an unknown judgment point.
    """
    if point and point not in config.ROUTED_CALLS:
        raise ValueError(
            f"Unknown judgment point {point!r}. Known: {', '.join(config.ROUTED_CALLS)}"
        )
    # Over-fetch, then filter here: the store's query is the cross-user
    # analysis surface and deliberately has no user predicate.
    fetch = min(max(limit * 20, 200), ALL_ROWS)
    rows = store.judgments(judgment_point=point or None, run_id=run_id or None, limit=fetch)

    if run_id:
        return list(rows)[:limit], f"eval run {run_id}"
    if all_users:
        return list(rows)[:limit], "all users"
    if user is None:
        kept = [r for r in rows if r["user_id"] is None]
        return kept[:limit], "system-level calls only (no active user)"
    kept = [r for r in rows if r["user_id"] in (user.id, None)]
    return kept[:limit], f"{user.display_name} ({user.id})"


def aggregate_stats(rows: list[Any]) -> dict[str, Any]:
    """Per-call-type and total counts, latency, tokens and cost.

    Returns `{"rows": [...], "total": {...} | None}`; each entry has `point`,
    `calls`, `errors`, `mean_ms`, `p95_ms`, `input_tokens`, `output_tokens`,
    `cost`, `cost_known` and `models`. `cost_known` is False when any call in
    the bucket used a model with no price entry.
    """

    def empty() -> dict[str, Any]:
        return {"calls": 0, "errors": 0, "lat": [], "in": 0, "out": 0, "cost": 0.0,
                "cost_known": True, "models": set()}

    buckets: dict[str, dict[str, Any]] = {}
    total = empty()
    for row in rows:
        in_tok, out_tok = row["input_tokens"] or 0, row["output_tokens"] or 0
        call_cost = cost(row["model"], in_tok, out_tok)
        for b in (buckets.setdefault(row["judgment_point"], empty()), total):
            b["calls"] += 1
            b["errors"] += 1 if row["error"] else 0
            if row["latency_ms"] is not None:
                b["lat"].append(float(row["latency_ms"]))
            b["in"] += in_tok
            b["out"] += out_tok
            b["models"].add(row["model"])
            if call_cost is None:
                b["cost_known"] = False
            else:
                b["cost"] += call_cost

    def shape(name: str, b: dict[str, Any]) -> dict[str, Any]:
        lat = b["lat"]
        return {
            "point": name,
            "calls": b["calls"],
            "errors": b["errors"],
            "mean_ms": round(sum(lat) / len(lat)) if lat else 0,
            "p95_ms": round(p95(lat)),
            "input_tokens": b["in"],
            "output_tokens": b["out"],
            "cost": round(b["cost"], 6),
            "cost_known": b["cost_known"],
            "models": sorted(b["models"]),
        }

    ordered = sorted(buckets, key=lambda name: -buckets[name]["calls"])
    return {
        "rows": [shape(name, buckets[name]) for name in ordered],
        "total": shape("TOTAL", total) if rows else None,
    }
