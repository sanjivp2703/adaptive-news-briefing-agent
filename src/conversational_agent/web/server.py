"""Transport for the web surface: routing, JSON, static files, the poller.

Run it with:

    PYTHONPATH=src:. .venv/bin/python -m conversational_agent.web

and open http://127.0.0.1:8787. Stdlib only.

Shape of a request. Each one opens its own SQLite connection (the store is
WAL-mode and connections are per-thread), does its work through the same
objects the CLI uses, and closes it. Endpoints that make live calls build the
full system per request; read-only ones open just the store and therefore work
with no credentials at all, exactly as the CLI's offline commands do.

This is a local, single-machine surface with no login. It binds to loopback
by default, and three cheap checks keep another website open in the same
browser from driving it (and spending API credit) behind the user's back:
the Host header must be a loopback name, an Origin header must match it, and
every POST must be `application/json`, which a cross-site page cannot send
without a preflight this server never approves.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import config
from ..store import Store, now_iso
from . import ApiError, console, reader

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEMO_STORE_SQL = Path(__file__).resolve().parent / "demo_store.sql"
ENV_FILE = Path.home() / ".config" / "news_agent" / "env"
_CREDENTIAL_NAMES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
MAX_BODY_BYTES = 64 * 1024


def load_env_file(path: Path = ENV_FILE) -> bool:
    """Pick up the API key from the project's documented key file.

    The key lives in `~/.config/news_agent/env` so that it is never pasted
    into a terminal, a script or a commit. The CLI gets it via `source`; the
    server reads the same file so that starting the app is one command. Only
    the two credential names are read, nothing already set is overridden, and
    the value is never printed or logged.
    """
    if any(os.environ.get(name) for name in _CREDENTIAL_NAMES):
        return True
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    found = False
    for line in lines:
        text = line.strip()
        if text.startswith("export "):
            text = text[len("export "):].strip()
        name, sep, value = text.partition("=")
        if not sep or name.strip() not in _CREDENTIAL_NAMES:
            continue
        value = value.strip().strip("'\"")
        if value:
            os.environ[name.strip()] = value
            found = True
    return found


def credentials_present() -> bool:
    return any(os.environ.get(name) for name in _CREDENTIAL_NAMES)


# --- The app: what the handlers share --------------------------------------


class Poller(threading.Thread):
    """The background poller (`cli poll --forever`), hosted by the server.

    Off unless switched on, from `--poll` or from the page: left running it
    makes real, billed calls every time a group's interval elapses, and that
    should be something the person at the keyboard chose.
    """

    def __init__(self, app: App, tick_seconds: int = 300, enabled: bool = False):
        super().__init__(daemon=True, name="poller")
        self.app = app
        self.tick_seconds = max(10, int(tick_seconds))
        self.enabled = enabled
        self.running = False
        self.last_run_at: str | None = None
        self.last_result: dict[str, Any] | None = None
        self._last_monotonic: float | None = None
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "tick_seconds": self.tick_seconds,
            "last_run_at": self.last_run_at,
            "last_result": self.last_result,
        }

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if self.enabled:
            self._last_monotonic = None  # sweep on the next beat, not a tick from now

    def run(self) -> None:
        while not self._halt.wait(1.0):
            if not self.enabled:
                continue
            due = (
                self._last_monotonic is None
                or time.monotonic() - self._last_monotonic >= self.tick_seconds
            )
            if not due:
                continue
            self._last_monotonic = time.monotonic()
            self.running = True
            try:
                system = self.app.system_factory()
                try:
                    self.last_result = reader.sweep(system)
                finally:
                    system.close()
            except Exception as exc:
                self.last_result = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            finally:
                self.running = False
                self.last_run_at = now_iso()


@dataclass
class App:
    """Everything a request needs that outlives the request."""

    db_path: Path | None = None
    # Builds the full live system. Tests substitute one built offline with a
    # stub client and a scripted feed; everything else is the real thing.
    system_factory: Callable[[], Any] | None = None
    live_ready: Callable[[], bool] = credentials_present
    poller: Poller | None = None
    # True when serving the recorded demo store: no key, no live calls.
    demo: bool = False
    # One live operation per user per endpoint at a time. A double-clicked
    # "ask" must not become two questions, and two concurrent closes of one
    # thread would both run the evidence extractor.
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _locks_guard: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.system_factory is None:
            self.system_factory = self._build_live

    def _build_live(self) -> Any:
        from ..app import MissingCredentials, build

        try:
            return build(db_path=self.db_path)
        except MissingCredentials:
            raise ApiError(
                503,
                "No Anthropic credentials found, so nothing that needs a live "
                "call can run. Set ANTHROPIC_API_KEY (or put it in "
                "~/.config/news_agent/env) and restart the server. Groups, "
                "goals and the whole console work without it.",
            ) from None

    def lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def resolved_db_path(self) -> Path:
        return Path(self.db_path) if self.db_path else config.db_path()


class Context:
    """One request's view of the app: its query, body, user and connections."""

    def __init__(self, app: App, query: dict[str, str], body: dict[str, Any], user_id: str | None):
        self.app = app
        self.query = query
        self.body = body
        self.user_id = user_id
        self._store: Store | None = None
        self._system: Any = None

    @property
    def store(self) -> Store:
        if self._system is not None:
            return self._system.store
        if self._store is None:
            self._store = Store(self.app.db_path)
        return self._store

    def system(self) -> Any:
        if self._system is None:
            self._system = self.app.system_factory()  # type: ignore[misc]
        return self._system

    def user_optional(self):
        if not self.user_id:
            return None
        return self.store.get_user(self.user_id)

    def user(self):
        found = self.user_optional()
        if found is None:
            raise ApiError(401, "Choose who you are first.")
        return found

    def scope(self):
        return self.store.scope(self.user().id)

    def flag(self, name: str) -> bool:
        return str(self.query.get(name, "")).lower() in ("1", "true", "yes")

    def close(self) -> None:
        if self._system is not None:
            self._system.close()
        if self._store is not None:
            self._store.close()


# --- Handlers --------------------------------------------------------------
# Each takes a Context plus the route's captured groups and returns JSON-able
# data. Live ones are marked in ROUTES so the transport can serialise them per
# user and attach the judgment calls they made.


def h_meta(ctx: Context) -> Any:
    app = ctx.app
    local = None
    if config.local_model_enabled():
        local = {
            "name": config.local_model_name(),
            "points": sorted(config.local_model_points()),
        }
    return {
        "db_path": str(app.resolved_db_path()),
        "demo": app.demo,
        "live_ready": bool(app.live_ready()),
        "local_model": local,
        "poller": app.poller.status() if app.poller else None,
        "default_poll_interval_minutes": config.DEFAULT_POLL_INTERVAL_MINUTES,
    }


def h_poller(ctx: Context) -> Any:
    if ctx.app.poller is None:
        raise ApiError(404, "This server was started without a poller.")
    enabled = bool(ctx.body.get("enabled"))
    if enabled and not ctx.app.live_ready():
        raise ApiError(503, "Background checking needs Anthropic credentials.")
    ctx.app.poller.set_enabled(enabled)
    return ctx.app.poller.status()


def h_users(ctx: Context) -> Any:
    return reader.list_users(ctx.store, include_personas=ctx.flag("all"))


def h_user_add(ctx: Context) -> Any:
    return reader.create_user(ctx.store, ctx.body.get("name"))


def h_groups(ctx: Context) -> Any:
    return reader.list_groups(ctx.scope())


def h_group_add(ctx: Context) -> Any:
    return reader.create_group(
        ctx.scope(),
        ctx.body.get("name"),
        ctx.body.get("description"),
        ctx.body.get("poll_interval_minutes"),
    )


def h_group(ctx: Context, group_id: str) -> Any:
    scope = ctx.scope()
    return reader.group_detail(scope, reader.require_group(scope, group_id))


def h_group_poll(ctx: Context, group_id: str) -> Any:
    system = ctx.system()
    scope = ctx.scope()
    group = reader.require_group(scope, group_id)
    result = reader.poll_group(system, scope, group)
    # Re-read the group: the poll stamped `last_polled_at` after `group` was loaded.
    result["group"] = reader.group_payload(scope, reader.require_group(scope, group_id))
    return result


def h_sweep(ctx: Context) -> Any:
    return reader.sweep(ctx.system())


def h_goals(ctx: Context) -> Any:
    return reader.list_goals(ctx.scope())


def h_goal_add(ctx: Context, group_id: str) -> Any:
    scope = ctx.scope()
    group = reader.require_group(scope, group_id)
    return reader.add_goal(scope, group, ctx.body.get("description"), ctx.body.get("deadline"))


def h_goal_close(ctx: Context, goal_id: str) -> Any:
    return reader.close_goal(ctx.scope(), goal_id, expired=bool(ctx.body.get("expired")))


def h_session(ctx: Context) -> Any:
    system = ctx.system()
    return reader.open_session(system, ctx.scope())


def h_brief(ctx: Context, group_id: str) -> Any:
    system = ctx.system()
    scope = ctx.scope()
    group = reader.require_group(scope, group_id)
    return {"exchange": reader.brief(system, scope, group)}


def h_open_threads(ctx: Context) -> Any:
    return reader.open_threads(ctx.scope())


def h_exchange(ctx: Context, exchange_id: str) -> Any:
    scope = ctx.scope()
    return reader.exchange_payload(scope, reader.require_exchange(scope, exchange_id))


def h_reading(ctx: Context, exchange_id: str) -> Any:
    scope = ctx.scope()
    exchange = reader.require_exchange(scope, exchange_id)
    return {"recorded": reader.record_reading(scope, exchange, ctx.body)}


def h_ask(ctx: Context, exchange_id: str) -> Any:
    system = ctx.system()
    scope = ctx.scope()
    answer = reader.ask(
        system, scope, exchange_id, ctx.body.get("text"), ctx.body.get("reading")
    )
    return {"answer": answer}


def h_close(ctx: Context, exchange_id: str) -> Any:
    system = ctx.system()
    scope = ctx.scope()
    result = reader.close(system, scope, exchange_id, ctx.body.get("reading"))
    result["groups"] = reader.list_groups(scope)
    return result


def h_console_group(ctx: Context, group_id: str) -> Any:
    scope = ctx.scope()
    return console.group_console(scope, reader.require_group(scope, group_id))


def h_models(ctx: Context) -> Any:
    return console.models()


def h_prompts(ctx: Context) -> Any:
    return console.prompts()


def h_prompt(ctx: Context, point: str) -> Any:
    return console.prompt(point)


def _limit(ctx: Context, default: int, ceiling: int) -> int:
    try:
        return max(1, min(int(ctx.query.get("limit", default)), ceiling))
    except (TypeError, ValueError):
        return default


def h_traces(ctx: Context) -> Any:
    return console.traces(
        ctx.store,
        ctx.user_optional(),
        point=ctx.query.get("point"),
        run_id=ctx.query.get("run_id"),
        all_users=ctx.flag("all_users"),
        limit=_limit(ctx, 100, 500),
    )


def h_trace(ctx: Context, log_id: str) -> Any:
    return console.trace(ctx.store, ctx.user_optional(), log_id, all_users=ctx.flag("all_users"))


def h_stats(ctx: Context) -> Any:
    return console.stats(
        ctx.store,
        ctx.user_optional(),
        point=ctx.query.get("point"),
        run_id=ctx.query.get("run_id"),
        all_users=ctx.flag("all_users"),
    )


def h_runs(ctx: Context) -> Any:
    return console.runs(ctx.store, limit=_limit(ctx, 30, 200))


_ID = r"([A-Za-z0-9_\-]+)"

# (method, path pattern, handler, live). `live` = makes model calls.
ROUTES: list[tuple[str, re.Pattern[str], Callable[..., Any], bool]] = [
    (m, re.compile(f"^{p}$"), fn, live)
    for m, p, fn, live in [
        ("GET", r"/api/meta", h_meta, False),
        ("POST", r"/api/poller", h_poller, False),
        ("GET", r"/api/users", h_users, False),
        ("POST", r"/api/users", h_user_add, False),
        ("GET", r"/api/groups", h_groups, False),
        ("POST", r"/api/groups", h_group_add, False),
        ("GET", rf"/api/groups/{_ID}", h_group, False),
        ("POST", rf"/api/groups/{_ID}/poll", h_group_poll, True),
        ("POST", rf"/api/groups/{_ID}/brief", h_brief, True),
        ("POST", rf"/api/groups/{_ID}/goals", h_goal_add, False),
        ("POST", r"/api/poll", h_sweep, True),
        ("GET", r"/api/goals", h_goals, False),
        ("POST", rf"/api/goals/{_ID}/close", h_goal_close, False),
        ("POST", r"/api/session", h_session, True),
        ("GET", r"/api/open-exchanges", h_open_threads, False),
        ("GET", rf"/api/exchanges/{_ID}", h_exchange, False),
        ("POST", rf"/api/exchanges/{_ID}/reading", h_reading, False),
        ("POST", rf"/api/exchanges/{_ID}/ask", h_ask, True),
        ("POST", rf"/api/exchanges/{_ID}/close", h_close, True),
        ("GET", rf"/api/console/groups/{_ID}", h_console_group, False),
        ("GET", r"/api/console/models", h_models, False),
        ("GET", r"/api/console/prompts", h_prompts, False),
        ("GET", rf"/api/console/prompts/{_ID}", h_prompt, False),
        ("GET", r"/api/console/traces", h_traces, False),
        ("GET", rf"/api/console/traces/{_ID}", h_trace, False),
        ("GET", r"/api/console/stats", h_stats, False),
        ("GET", r"/api/console/runs", h_runs, False),
    ]
]


def dispatch(
    app: App,
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> tuple[int, Any]:
    """Route one API call. Returns (status, JSON-able payload).

    Separate from the HTTP handler so tests can exercise every endpoint
    without a socket.
    """
    known_path = False
    for route_method, pattern, handler, live in ROUTES:
        match = pattern.match(path)
        if not match:
            continue
        known_path = True
        if route_method != method:
            continue
        ctx = Context(app, query or {}, body or {}, user_id)
        # Keyed by user AND path: a double-clicked "ask" must not become two
        # questions, but a minute-long news check must not block reading.
        lock = app.lock_for(f"{user_id or '-'}:{path}") if live else None
        if lock is not None:
            lock.acquire()
        since = now_iso()
        try:
            payload = handler(ctx, *match.groups())
            if live and isinstance(payload, dict):
                payload["calls"] = console.recent_calls(ctx.store, user_id, since)
            return 200, payload
        except ApiError as exc:
            return exc.status, {"error": exc.message}
        except KeyError as exc:
            return 404, {"error": str(exc).strip("'\"") or "Not found."}
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            detail = f"{type(exc).__name__}: {exc}"[:400]
            if live:
                return 502, {"error": f"That needed a live call and it failed. {detail}"}
            return 500, {"error": detail}
        finally:
            if lock is not None:
                lock.release()
            ctx.close()
    if known_path:
        return 405, {"error": "Method not allowed."}
    return 404, {"error": "No such endpoint."}


# --- HTTP ------------------------------------------------------------------

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class Handler(BaseHTTPRequestHandler):
    server_version = "news-agent-web"
    protocol_version = "HTTP/1.1"
    app: App  # set on the subclass by make_server
    enforce_loopback: bool = True

    # -- plumbing

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/api/"):
            stamp = datetime.now(UTC).strftime("%H:%M:%S")
            sys.stderr.write(f"{stamp} {fmt % args}\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        if not self.enforce_loopback:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
        if name not in _LOOPBACK_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != host:
            return False
        return True

    # -- verbs

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "This server only answers on localhost."})
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self._api("GET", parsed, {})
        return self._static(parsed.path)

    do_HEAD = do_GET

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "This server only answers on localhost."})
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return self._json(404, {"error": "No such endpoint."})
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return self._json(415, {"error": "POST bodies must be application/json."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return self._json(413, {"error": "Request body too large."})
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "Request body is not valid JSON."})
        if not isinstance(body, dict):
            return self._json(400, {"error": "Request body must be a JSON object."})
        return self._api("POST", parsed, body)

    def _api(self, method: str, parsed: Any, body: dict[str, Any]) -> None:
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        user_id = self.headers.get("X-User-Id") or query.get("user") or None
        status, payload = dispatch(self.app, method, parsed.path, query, body, user_id)
        self._json(status, payload)

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        if name.startswith("static/"):
            name = name[len("static/"):]
        # Only files that actually sit in the static directory, by exact name:
        # no traversal is possible because the request never becomes a path.
        allowed = {p.name: p for p in STATIC_DIR.iterdir() if p.is_file()}
        target = allowed.get(name)
        if target is None:
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)


def demo_app(directory: Path) -> App:
    """An App over a fresh copy of the recorded demo store.

    The demo exists so the product can be explored with no API key: the store
    is a real session captured from live runs (real search results, real model
    verdicts, real costs), loaded into a throwaway SQLite file. Everything
    that only reads works -- history, ledger, every trace. Anything that would
    need a model call is refused with a message saying so, rather than faked:
    a canned answer presented as a live one would misrepresent the system.
    """
    import sqlite3

    target = Path(directory) / "demo.db"
    conn = sqlite3.connect(target)
    try:
        conn.executescript(DEMO_STORE_SQL.read_text(encoding="utf-8"))
    finally:
        conn.close()

    def no_live_calls() -> Any:
        raise ApiError(
            503,
            "This is the recorded demo, so nothing new is searched for or "
            "written. Start the server without --demo and with an Anthropic "
            "API key to run it live.",
        )

    return App(db_path=target, system_factory=no_live_calls, live_ready=lambda: False, demo=True)


def make_server(app: App, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (Handler,),
        {"app": app, "enforce_loopback": host in ("127.0.0.1", "localhost", "::1")},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m conversational_agent.web",
        description="The conversational competence agent, in a browser.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="SQLite store (default: $CONVAGENT_DB, else the packaged data dir).")
    parser.add_argument("--poll", action="store_true",
                        help="Start with background checking switched on.")
    parser.add_argument("--tick", type=int, default=300,
                        help="Seconds between background sweeps for due groups.")
    parser.add_argument("--open", action="store_true", help="Open the page in a browser.")
    parser.add_argument("--demo", action="store_true",
                        help="Serve a recorded real session. Needs no API key; makes no live calls.")
    args = parser.parse_args(argv)

    if args.demo:
        import tempfile

        app = demo_app(Path(tempfile.mkdtemp(prefix="news-agent-demo-")))
    else:
        load_env_file()
        app = App(db_path=Path(args.db) if args.db else None)
        app.poller = Poller(app, tick_seconds=args.tick, enabled=args.poll and credentials_present())
        app.poller.start()

    try:
        server = make_server(app, args.host, args.port)
    except OSError as exc:
        print(f"Could not start on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"News agent running at {url}")
    print(f"  store        {app.resolved_db_path()}")
    if app.demo:
        print("  mode         DEMO: a recorded session, no API key needed, no live calls")
    else:
        print(f"  live calls   {'ready' if credentials_present() else 'NO CREDENTIALS (read-only pages still work)'}")
        print(f"  background   {'on' if app.poller.enabled else 'off'} (toggle on the page, or start with --poll)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  WARNING: not bound to loopback. There is no login on this server.")
    print("Ctrl-C to stop.")
    if args.open:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if app.poller is not None:
            app.poller.stop()
        server.server_close()
    return 0
