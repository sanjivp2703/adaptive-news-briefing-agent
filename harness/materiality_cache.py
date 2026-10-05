"""`--materiality-cache PATH`: replay materiality verdicts across runs.

A per-change regression run re-judges the same fixture events every time, and
materiality is routed to Opus. The verdict for a given event in a given group
does not depend on anything the change under test touched unless the change
was to the materiality prompt itself -- so it can be cached, keyed on what the
prompt actually sees about the event:

    sha256(headline \\x1f detail \\x1f group description)

The cache is a plain JSON file. `CachingClient` wraps the harness's own
`RecordingClient` (never anything in `src/`): a `materiality` call whose key is
in the file is answered from it, anything else -- a miss, or any other
judgment point -- goes through to the wrapped client unchanged, and new
verdicts are written back.

Two things keep the accounting honest:

* `Judge` still logs a `judgment_log` row for a cached verdict (it cannot tell
  the difference; the wrapper returns a `RawCompletion` like any client). The
  row carries zero tokens because the wrapper reports zero, and after the
  monitor stage the runner calls `mark_logged`, which stamps those rows
  `model = "cache"` so the gate's cost table prices them at nothing and a
  reader of the log can see which verdicts were replayed. Tracing stays
  complete: every verdict the system acted on is still in the log.
* The `RecordingClient`'s call list gets an entry for a hit too (flagged
  `cached`), so call-order attribution of verdicts to rounds is unchanged.

The key deliberately excludes `recent_events`: the prompt sees them, and a
verdict could in principle depend on them ("saturating similar events"). For
the fixture suites every event is the first of its kind in its group, so the
approximation holds there; `stats()` reports the hit rate and the runner
prints it, and a cache should be deleted after a change to the materiality
prompt (`prompt_version` is stored next to each verdict and mismatches are
treated as misses).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conversational_agent import config
from conversational_agent.judgment import RawCompletion

CACHE_MODEL_LABEL = "cache"


def materiality_key(context: dict[str, Any]) -> str | None:
    """The cache key for one materiality context, or None if it lacks the parts."""
    event = context.get("candidate_event") or {}
    group = context.get("group") or {}
    headline = str(event.get("headline") or "").strip()
    if not headline:
        return None
    detail = str(event.get("detail") or "").strip()
    description = str(group.get("description") or "").strip()
    blob = "\x1f".join((headline, detail, description)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class MaterialityCache:
    """The JSON file plus hit/miss bookkeeping. Thread-safe (parallel personas)."""

    def __init__(self, path: Path, prompt_version: str | None = None):
        self.path = Path(path)
        self.prompt_version = prompt_version
        self._lock = threading.Lock()
        self.entries: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        self.writes = 0
        # `input_json` payloads answered from the cache and not yet stamped
        # in the judgment log.
        self._served: list[str] = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.entries = {
                    k: v for k, v in (data.get("entries") or {}).items() if isinstance(v, dict)
                }

    # --- lookups -----------------------------------------------------------

    def lookup(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self.entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if self.prompt_version and entry.get("prompt_version") not in (None, self.prompt_version):
                self.misses += 1
                return None
            self.hits += 1
            return entry.get("verdict")

    def store(self, key: str, verdict: dict[str, Any], *, headline: str) -> None:
        with self._lock:
            self.entries[key] = {
                "headline": headline,
                "prompt_version": self.prompt_version,
                "verdict": verdict,
            }
            self.writes += 1
        self.save()

    def note_served(self, input_json: str) -> None:
        with self._lock:
            self._served.append(input_json)

    def save(self) -> None:
        with self._lock:
            body = {"kind": "materiality_cache", "entries": self.entries}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "path": str(self.path),
                "entries": len(self.entries),
                "hits": self.hits,
                "misses": self.misses,
                "written": self.writes,
                "hit_rate": (round(self.hits / total, 4) if total else None),
            }

    # --- honest logging -----------------------------------------------------

    def mark_logged(self, conn: sqlite3.Connection, run_id: str | None = None) -> int:
        """Stamp the judgment-log rows for verdicts served from the cache.

        `Judge` wrote each of them with the configured model name and the zero
        token counts the wrapper reported. Matching is on the exact
        `input_json` the wrapper served (plus the run id when known), and only
        rows still carrying zero tokens are touched, so a genuine call for the
        same payload in the same run is never mislabelled.
        """
        with self._lock:
            pending, self._served = self._served, []
        stamped = 0
        for payload in pending:
            # Exactly one row per served verdict: the newest unmarked match.
            # (The offline stub also logs zero tokens, so "zero tokens" alone
            # would not separate a genuine call from its replay.)
            where = (
                " WHERE judgment_point = ? AND input_json = ? AND model != ?"
                " AND COALESCE(input_tokens, 0) = 0 AND COALESCE(output_tokens, 0) = 0"
            )
            params: list[Any] = [config.MATERIALITY, payload, CACHE_MODEL_LABEL]
            if run_id is not None:
                where += " AND run_id IS ?"
                params.append(run_id)
            cur = conn.execute(
                "UPDATE judgment_log SET model = ?, input_tokens = 0, output_tokens = 0"
                " WHERE id = (SELECT id FROM judgment_log" + where + " ORDER BY rowid DESC LIMIT 1)",
                [CACHE_MODEL_LABEL] + params,
            )
            stamped += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return stamped


@dataclass
class CachingClient:
    """`LLMClient` wrapper: answers materiality from the cache, passes the rest on."""

    inner: Any  # the harness's RecordingClient (or any LLMClient)
    cache: MaterialityCache
    served: list[dict[str, Any]] = field(default_factory=list)

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        schema: dict[str, Any],
        effort: str,
        max_tokens: int = 16000,
    ) -> RawCompletion:
        payload = json.loads(user_content)
        point = payload.get("_judgment_point")
        context = payload.get("context", {}) or {}
        key = materiality_key(context) if point == config.MATERIALITY else None
        if key is not None:
            verdict = self.cache.lookup(key)
            if verdict is not None:
                self.cache.note_served(user_content)
                self.served.append({"key": key, "headline": (context.get("candidate_event") or {}).get("headline")})
                calls = getattr(self.inner, "calls", None)
                if isinstance(calls, list):
                    calls.append(
                        {"point": point, "context": context, "verdict": verdict, "error": None, "cached": True}
                    )
                return RawCompletion(
                    text=json.dumps(verdict), input_tokens=0, output_tokens=0, model=CACHE_MODEL_LABEL
                )
        raw = self.inner.complete_json(
            model=model,
            system=system,
            user_content=user_content,
            schema=schema,
            effort=effort,
            max_tokens=max_tokens,
        )
        if key is not None:
            try:
                verdict = json.loads(raw.text)
            except json.JSONDecodeError:
                verdict = None
            if isinstance(verdict, dict):
                self.cache.store(
                    key, verdict, headline=str((context.get("candidate_event") or {}).get("headline") or "")
                )
        return raw
