"""The web surface drives the same system the CLI does, end to end.

Everything here is offline: the system is the real wired one with only the LLM
client and the event feed stubbed (the same substitution the rest of the suite
makes), and most tests go through `dispatch`, the router the HTTP handler
calls. A few go over a real socket, because the transport's own rules -- the
loopback, origin and content-type checks that stop another website from
spending API credit through this server -- only exist there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from unittest import mock

import pytest

from conversational_agent import config
from conversational_agent.web import server
from conversational_agent.web.server import App, Poller, dispatch, make_server
from tests.conftest import WEB_EVENT, ok

EVENT = WEB_EVENT


# Prevents: the browser being a second product. One pass through every step a
# reader takes -- follow a group, check for news, open a session, be briefed,
# ask, stop -- must leave the store in the state the CLI path would.
def test_a_reader_can_go_from_nothing_to_a_closed_thread(web, sam, league):
    assert ok(web, "GET", "/api/groups", user=sam)[0]["last_polled_at"] is None

    polled = ok(web, "POST", f"/api/groups/{league}/poll", user=sam)
    assert polled["material_found"] == 1
    assert polled["group"]["new"] == 1 and polled["group"]["untold"] == 1
    assert polled["group"]["last_polled_at"] is not None  # the page shows "checked just now"
    assert [c["point"] for c in polled["calls"]] == [config.MATERIALITY]

    session = ok(web, "POST", "/api/session", user=sam)
    assert session["quiet"] is False
    assert [e["headline"] for e in session["events"]] == [EVENT["headline"]]
    assert session["topic"]["group_id"] == league
    assert ok(web, "GET", "/api/groups", user=sam)[0]["new"] == 0  # surfaced

    exchange = ok(web, "POST", f"/api/groups/{league}/brief", user=sam)["exchange"]
    assert exchange["briefing"].startswith("Arsenal bought a striker")
    assert exchange["source"]["source_url"] == EVENT["source_url"]
    assert exchange["closed"] is False and exchange["turns"] == []

    asked = ok(
        web,
        "POST",
        f"/api/exchanges/{exchange['id']}/ask",
        {"text": "What is a release clause?", "reading": {"dwell_ms": 9000, "scroll_fraction": 1.0}},
        sam,
    )
    assert "release clause" in asked["answer"]["text"]
    assert {c["point"] for c in asked["calls"]} == {config.GAP_ROUTING, config.THREAD_REPLY}

    closed = ok(web, "POST", f"/api/exchanges/{exchange['id']}/close", user=sam)
    assert closed["closed"] is True
    assert [c["point"] for c in closed["calls"]] == [config.CONCEPT_EVIDENCE]

    detail = ok(web, "GET", f"/api/groups/{league}", user=sam)
    told = detail["history"][0]
    assert told["closed"] is True
    assert [t["speaker"] for t in told["turns"]] == ["user", "system"]
    assert detail["last_engaged"] is not None

    ledger = {c["term"]: c for c in ok(web, "GET", f"/api/console/groups/{league}", user=sam)["ledger"]}
    assert ledger["release clause"]["state"] == config.CONCEPT_EXPLAINED
    # Defined in a briefing the browser saw them read: the one sanctioned path
    # (the ledger stores terms normalised, so "add-ons" is held as "add ons")
    # from reading behaviour into the ledger.
    assert ledger["add ons"]["state"] == config.CONCEPT_FAMILIAR


# Prevents: the web surface becoming the place a story gets told twice. The
# never-repeat rule is structural in the store; a "tell me" button must hit it.
def test_a_story_is_never_briefed_twice_from_the_browser(web, sam, league):
    ok(web, "POST", f"/api/groups/{league}/poll", user=sam)
    first = ok(web, "POST", f"/api/groups/{league}/brief", user=sam)["exchange"]
    assert first is not None
    again = ok(web, "POST", f"/api/groups/{league}/brief", user=sam)
    assert again["exchange"] is None
    assert ok(web, "GET", "/api/groups", user=sam)[0]["untold"] == 0


# Prevents: a tab closed mid-thread losing the briefing. It stays open, is
# listed for pick-up, and can still be asked about and closed later.
def test_an_unfinished_thread_can_be_picked_back_up(web, sam, league):
    ok(web, "POST", f"/api/groups/{league}/poll", user=sam)
    exchange = ok(web, "POST", f"/api/groups/{league}/brief", user=sam)["exchange"]
    open_threads = ok(web, "GET", "/api/open-exchanges", user=sam)
    assert [x["id"] for x in open_threads] == [exchange["id"]]
    assert ok(web, "GET", "/api/groups", user=sam)[0]["open_exchange_id"] == exchange["id"]
    ok(web, "POST", f"/api/exchanges/{exchange['id']}/close", user=sam)
    assert ok(web, "GET", "/api/open-exchanges", user=sam) == []


# Prevents: the browser's reading signal being recorded twice, or under the
# CLI's proxy label. It is the first surface that observes dwell and scroll;
# it is measured once, before the first question, and labelled `observed`.
def test_reading_is_recorded_once_and_labelled_observed(web, sam, league):
    ok(web, "POST", f"/api/groups/{league}/poll", user=sam)
    xid = ok(web, "POST", f"/api/groups/{league}/brief", user=sam)["exchange"]["id"]
    first = ok(web, "POST", f"/api/exchanges/{xid}/reading", {"dwell_ms": 300, "scroll_fraction": 0.05}, sam)
    second = ok(web, "POST", f"/api/exchanges/{xid}/reading", {"dwell_ms": 60000, "scroll_fraction": 1.0}, sam)
    assert first == {"recorded": True} and second == {"recorded": False}
    assert ok(web, "GET", f"/api/exchanges/{xid}", user=sam)["reading_recorded"] is True

    row = ok(web, "GET", f"/api/console/groups/{league}", user=sam)["exchanges"][0]
    assert row["reading_source"] == "observed"
    assert row["read_quality"] == "skipped" and row["dwell_ms"] == 300

    # A skipped briefing earns no reading credit at close: nothing was read.
    ok(web, "POST", f"/api/exchanges/{xid}/close", user=sam)
    states = {c["term"]: c["state"] for c in ok(web, "GET", f"/api/console/groups/{league}", user=sam)["ledger"]}
    assert states.get("add ons") == config.CONCEPT_UNKNOWN


def test_a_finished_thread_refuses_more_questions_and_blank_ones_are_rejected(web, sam, league):
    ok(web, "POST", f"/api/groups/{league}/poll", user=sam)
    xid = ok(web, "POST", f"/api/groups/{league}/brief", user=sam)["exchange"]["id"]
    assert dispatch(web, "POST", f"/api/exchanges/{xid}/ask", {}, {"text": "   "}, sam)[0] == 400
    ok(web, "POST", f"/api/exchanges/{xid}/close", user=sam)
    assert dispatch(web, "POST", f"/api/exchanges/{xid}/ask", {}, {"text": "one more?"}, sam)[0] == 409
    # Closing twice is harmless and makes no second evidence call.
    again = ok(web, "POST", f"/api/exchanges/{xid}/close", user=sam)
    assert again["closed"] is True and again["calls"] == []


# Prevents: one person's groups, threads or traces being reachable with
# another person's id. Every reader path goes through a UserScope, so the
# other user's rows must report as absent, not as forbidden.
def test_one_user_cannot_reach_another_users_data(web, sam, league):
    dana = ok(web, "POST", "/api/users", {"name": "Dana"})["id"]
    ok(web, "POST", f"/api/groups/{league}/poll", user=sam)
    xid = ok(web, "POST", f"/api/groups/{league}/brief", user=sam)["exchange"]["id"]

    assert ok(web, "GET", "/api/groups", user=dana) == []
    for method, path in [
        ("GET", f"/api/groups/{league}"),
        ("POST", f"/api/groups/{league}/poll"),
        ("POST", f"/api/groups/{league}/brief"),
        ("GET", f"/api/exchanges/{xid}"),
        ("POST", f"/api/exchanges/{xid}/ask"),
        ("POST", f"/api/exchanges/{xid}/close"),
        ("GET", f"/api/console/groups/{league}"),
    ]:
        status, _ = dispatch(web, method, path, {}, {"text": "hello?"}, dana)
        assert status == 404, f"{method} {path} leaked to another user ({status})"

    sams = ok(web, "GET", "/api/console/traces", user=sam)["rows"]
    danas = ok(web, "GET", "/api/console/traces", user=dana)["rows"]
    assert sams and not danas
    assert dispatch(web, "GET", f"/api/console/traces/{sams[0]['id']}", {}, {}, dana)[0] == 404
    everyone = ok(web, "GET", "/api/console/traces", user=dana, query={"all_users": "1"})["rows"]
    assert len(everyone) == len(sams)


def test_requests_without_a_known_person_are_turned_away(web):
    assert dispatch(web, "GET", "/api/groups", {}, {}, None)[0] == 401
    assert dispatch(web, "GET", "/api/groups", {}, {}, "usr_nobody")[0] == 401
    # The console's own pages need nobody: a failed run must stay diagnosable.
    assert dispatch(web, "GET", "/api/console/models", {}, {}, None)[0] == 200
    assert dispatch(web, "GET", "/api/console/traces", {}, {}, None)[0] == 200


def test_groups_and_goals_follow_the_cli_rules(web, sam, league):
    assert dispatch(web, "POST", "/api/groups", {}, {"name": "Premier League"}, sam)[0] == 409
    assert dispatch(web, "POST", "/api/groups", {}, {"name": "  "}, sam)[0] == 400
    assert dispatch(web, "POST", "/api/users", {}, {"name": "Sam"}, None)[0] == 409

    past = dispatch(web, "POST", f"/api/groups/{league}/goals", {}, {"description": "derby", "deadline": "2001-01-01"}, sam)
    assert past[0] == 400
    goal = ok(web, "POST", f"/api/groups/{league}/goals", {"description": "derby", "deadline": "2999-01-01"}, sam)
    assert goal["status"] == "active"
    second = dispatch(web, "POST", f"/api/groups/{league}/goals", {}, {"description": "cup", "deadline": "2999-02-01"}, sam)
    assert second[0] == 409  # one active goal per group
    assert ok(web, "GET", "/api/groups", user=sam)[0]["goal"]["id"] == goal["id"]
    closed = ok(web, "POST", f"/api/goals/{goal['id']}/close", {}, sam)
    assert closed["status"] == "completed"
    assert ok(web, "GET", "/api/groups", user=sam)[0]["goal"] is None


def test_the_console_reports_models_prompts_stats_and_traces(web, sam, league):
    ok(web, "POST", f"/api/groups/{league}/poll", user=sam)
    ok(web, "POST", "/api/session", user=sam)

    models = ok(web, "GET", "/api/console/models")
    assert [r["point"] for r in models["rows"]] == list(config.ROUTED_CALLS)
    prompts = ok(web, "GET", "/api/console/prompts")
    assert {r["point"] for r in prompts} == set(config.ROUTED_CALLS)
    text = ok(web, "GET", f"/api/console/prompts/{config.BRIEFING}")
    assert text["version"] and len(text["text"]) > 100
    assert dispatch(web, "GET", "/api/console/prompts/nonsense", {}, {}, None)[0] == 404

    traces = ok(web, "GET", "/api/console/traces", user=sam)
    assert {r["point"] for r in traces["rows"]} == {config.MATERIALITY, config.INTERRUPT_TIMING}
    one = ok(web, "GET", f"/api/console/traces/{traces['rows'][0]['id']}", user=sam)
    assert isinstance(one["input"], dict) and isinstance(one["verdict"], dict)
    only = ok(web, "GET", "/api/console/traces", user=sam, query={"point": config.MATERIALITY})
    assert {r["point"] for r in only["rows"]} == {config.MATERIALITY}

    stats = ok(web, "GET", "/api/console/stats", user=sam)
    assert stats["total"]["calls"] == 2 and stats["total"]["errors"] == 0
    assert ok(web, "GET", "/api/console/runs") == []

    console = ok(web, "GET", f"/api/console/groups/{league}", user=sam)
    assert console["events"][0]["is_material"] is True and console["events"][0]["surfaced"] is True
    # Every state in config is represented, so none can be silently dropped
    # from the ledger view (the CLI's own state list once lost `familiar`).
    assert {s["state"] for s in console["state_counts"]} == set(config.CONCEPT_STATES)


# Prevents: a missing key turning the whole app into an error page. Only the
# calls that need a model fail, and they say why; everything else works.
def test_without_credentials_only_the_live_calls_fail(db_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    web = App(db_path=db_path)
    sam = ok(web, "POST", "/api/users", {"name": "Sam"})["id"]
    group = ok(web, "POST", "/api/groups", {"name": "wine"}, sam)["id"]
    assert ok(web, "GET", "/api/meta")["live_ready"] is False
    for path in ("/api/session", f"/api/groups/{group}/poll", f"/api/groups/{group}/brief"):
        status, payload = dispatch(web, "POST", path, {}, {}, sam)
        assert status == 503 and "credentials" in payload["error"].lower()
    assert ok(web, "GET", f"/api/groups/{group}", user=sam)["name"] == "wine"
    assert ok(web, "GET", "/api/console/stats", user=sam)["rows"] == []


def test_the_env_file_supplies_the_key_without_overriding_or_printing_it(tmp_path, capsys):
    env = tmp_path / "env"
    env.write_text("# comment\nexport OTHER=1\nexport ANTHROPIC_API_KEY='sk-test-123'\n")
    # `load_env_file` writes os.environ directly, so the whole test runs on a
    # copy that is restored afterwards; otherwise the fake key would outlive
    # the test and every later one would believe credentials exist.
    with mock.patch.dict(os.environ):
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OTHER"):
            os.environ.pop(name, None)
        assert server.load_env_file(env) is True
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-test-123"
        assert "OTHER" not in os.environ  # only the credential names are read
        os.environ["ANTHROPIC_API_KEY"] = "already-set"
        assert server.load_env_file(env) is True
        assert os.environ["ANTHROPIC_API_KEY"] == "already-set"
        del os.environ["ANTHROPIC_API_KEY"]
        assert server.load_env_file(tmp_path / "missing") is False
    captured = capsys.readouterr()
    assert "sk-test-123" not in captured.out + captured.err


# Prevents: background checking running without being switched on, and a
# sweep that fails taking the poller thread down with it.
def test_the_poller_sweeps_only_when_switched_on(web, sam, league, feed):
    poller = Poller(web, tick_seconds=10, enabled=False)
    web.poller = poller
    poller.start()
    try:
        time.sleep(1.3)
        assert feed.fetch_calls == [] and poller.last_run_at is None
        assert ok(web, "POST", "/api/poller", {"enabled": True})["enabled"] is True
        deadline = time.time() + 5
        while poller.last_run_at is None and time.time() < deadline:
            time.sleep(0.05)
        assert poller.last_run_at is not None, "the poller never swept after being switched on"
        assert poller.last_result == {
            "polled": 1, "material_found": 1, "skipped_personas": 0, "notes": [], "errors": [],
        }
        assert ok(web, "GET", "/api/groups", user=sam)[0]["new"] == 1
        assert ok(web, "POST", "/api/poller", {"enabled": False})["enabled"] is False
    finally:
        poller.stop()
        poller.join(timeout=3)


# --- Over a real socket ----------------------------------------------------


@pytest.fixture
def http(web):
    srv = make_server(web, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]

    def request(method, path, body=None, headers=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, res.headers.get("Content-Type", ""), res.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read()

    yield request
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=3)


JSON = {"Content-Type": "application/json"}


def test_the_page_and_its_assets_are_served(http):
    status, ctype, body = http("GET", "/")
    assert status == 200 and ctype.startswith("text/html") and b'id="view"' in body
    for name, kind in (("app.js", "javascript"), ("app.css", "text/css")):
        status, ctype, body = http("GET", f"/static/{name}")
        assert status == 200 and kind in ctype and len(body) > 1000
    for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/server.py", "/static/nope.js"):
        assert http("GET", path)[0] == 404


def test_the_api_round_trips_over_http(http):
    status, _, body = http("POST", "/api/users", {"name": "Sam"}, JSON)
    user = json.loads(body)["id"]
    assert status == 200
    status, _, body = http("POST", "/api/groups", {"name": "wine"}, {**JSON, "X-User-Id": user})
    assert status == 200 and json.loads(body)["name"] == "wine"
    # The beacon cannot set headers, so the person may also travel in the query.
    status, _, body = http("GET", f"/api/groups?user={user}")
    assert status == 200 and [g["name"] for g in json.loads(body)] == ["wine"]
    assert http("GET", "/api/nope")[0] == 404
    assert http("POST", "/api/meta", {}, JSON)[0] == 405


# Prevents: a web page open in another tab driving this server. It has no
# login, and its live endpoints spend real API credit, so the transport must
# refuse anything a cross-site page could send.
def test_requests_another_website_could_forge_are_refused(http):
    # A form post: the only cross-site POST a browser sends without preflight.
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    assert http("POST", "/api/users", b"name=Mallory", form)[0] == 415
    assert http("POST", "/api/users", b'{"name":"Mallory"}', {"Content-Type": "text/plain"})[0] == 415
    # A page that somehow sent JSON still carries its own Origin.
    assert http("POST", "/api/users", {"name": "Mallory"}, {**JSON, "Origin": "https://evil.example"})[0] == 403
    # DNS rebinding: the right address under somebody else's name.
    assert http("GET", "/api/users", headers={"Host": "evil.example"})[0] == 403
    assert http("POST", "/api/users", b"{not json", JSON)[0] == 400
    status, _, body = http("GET", "/api/users")
    assert status == 200 and json.loads(body) == []


# Prevents: a script no test imports shipping with a syntax error. Nothing
# executes app.js here, so without a parse gate a broken file passes a green
# suite.
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_browser_script_parses():
    script = server.STATIC_DIR / "app.js"
    result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# Prevents: the no-key demo rotting. It must load, show the recorded session,
# and refuse -- not fake -- anything that would need a model.
def test_the_demo_serves_a_recorded_session_and_refuses_live_calls(tmp_path):
    demo = server.demo_app(tmp_path)
    meta = ok(demo, "GET", "/api/meta")
    assert meta["demo"] is True and meta["live_ready"] is False and meta["poller"] is None
    users = ok(demo, "GET", "/api/users")
    assert len(users) == 1
    reader_id = users[0]["id"]
    groups = ok(demo, "GET", "/api/groups", user=reader_id)
    assert len(groups) >= 2
    told = [g for g in groups if ok(demo, "GET", f"/api/groups/{g['id']}", user=reader_id)["history"]]
    assert told, "the demo store should contain at least one briefing with its thread"
    traces = ok(demo, "GET", "/api/console/traces", user=reader_id)["rows"]
    assert {r["point"] for r in traces} >= {config.MATERIALITY, config.BRIEFING, config.THREAD_REPLY}
    status, payload = dispatch(demo, "POST", "/api/session", {}, {}, reader_id)
    assert status == 503 and "demo" in payload["error"].lower()
    text = server.DEMO_STORE_SQL.read_text(encoding="utf-8")
    assert "sk-ant" not in text and "/Users/" not in text
