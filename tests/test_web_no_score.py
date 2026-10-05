"""No score, grade, level or proficiency band reaches the reader in a browser.

The same rule `test_cli_shows_no_score.py` holds the terminal to, held the same
two ways: the reader module must not even *read* the scoring fields (not
showing a number today is one edit away from showing it), and the payloads a
reader's pages are built from must not *contain* score-shaped keys or text.

The console is exempt by design -- it is the builder's diagnostic view -- which
is exactly why the two live in separate modules: the boundary this test draws
is a file, not a convention.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from conversational_agent.web import reader
from conversational_agent.web.server import ROUTES, dispatch
from tests.conftest import ok
from tests.test_cli_shows_no_score import FORBIDDEN_READS, SCORE_SHAPED, _body_without_docstring

FORBIDDEN_KEYS = FORBIDDEN_READS | {
    "materiality_score",
    "attention",
    "read_quality",
    "skip_kind",
    "understood",
    "not_understood",
    "asked_about",
    "already_knew",
    "known",
    "counts_toward_band",
}


def test_the_reader_module_never_reads_a_score_or_band_field():
    tree = ast.parse(Path(reader.__file__).read_text(encoding="utf-8"))
    offenders: list[str] = []
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert len(functions) > 15, "reader.py looks empty; this guard is scanning the wrong file."
    for func in functions:
        for statement in _body_without_docstring(func):
            for node in ast.walk(statement):
                if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_READS:
                    offenders.append(f"{func.name}(): reads .{node.attr}")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if SCORE_SHAPED.search(node.value):
                        offenders.append(f"{func.name}(): literal {node.value!r}")
    assert not offenders, "Reader payloads touch scoring:\n" + "\n".join(offenders)


def test_reader_handlers_do_not_borrow_from_the_console():
    """Every non-console route's handler must stay out of `console.*`, so a
    band cannot arrive on a reader page by way of a helpful import."""
    from conversational_agent.web import server

    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    handlers = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    reader_handlers = {
        fn.__name__ for _, pattern, fn, _ in ROUTES if "/api/console/" not in pattern.pattern
    }
    assert len(reader_handlers) > 10
    offenders = []
    for name in sorted(reader_handlers):
        for node in ast.walk(handlers[name]):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "console"
            ):
                offenders.append(f"{name}(): console.{node.attr}")
    assert not offenders, "\n".join(offenders)


def _keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _keys(item)


def test_no_reader_payload_carries_a_score_shaped_key_or_word(web, sam, league):
    """Walk the whole reader journey and inspect every payload it returns."""
    seen: list[tuple[str, object]] = []

    def step(method, path, body=None):
        payload = ok(web, method, path, body, sam)
        seen.append((f"{method} {path}", payload))
        return payload

    step("GET", "/api/meta")
    step("GET", "/api/users")
    step("POST", f"/api/groups/{league}/goals", {"description": "derby day", "deadline": "2999-01-01"})
    step("GET", "/api/goals")
    step("POST", f"/api/groups/{league}/poll")
    step("GET", "/api/groups")
    step("POST", "/api/session")
    exchange = step("POST", f"/api/groups/{league}/brief")["exchange"]
    step("GET", "/api/open-exchanges")
    step("POST", f"/api/exchanges/{exchange['id']}/ask", {"text": "What is a release clause?",
                                                           "reading": {"dwell_ms": 9000, "scroll_fraction": 1}})
    step("GET", f"/api/exchanges/{exchange['id']}")
    step("POST", f"/api/exchanges/{exchange['id']}/close")
    step("GET", f"/api/groups/{league}")

    for label, payload in seen:
        # `calls` is the behind-the-scenes strip: call name, model, time, cost.
        # It is checked for keys like everything else, but the model id and
        # judgment-point names in it are not prose shown as a verdict.
        bad_keys = sorted(set(_keys(payload)) & FORBIDDEN_KEYS)
        assert not bad_keys, f"{label} carries scoring keys: {bad_keys}"
        prose = dict(payload) if isinstance(payload, dict) else {"rows": payload}
        prose.pop("calls", None)
        hits = SCORE_SHAPED.findall(json.dumps(prose))
        assert not hits, f"{label} carries score-shaped text: {hits}"


def test_closing_a_thread_tells_the_reader_only_that_it_is_closed(web, sam, league):
    ok(web, "POST", f"/api/groups/{league}/poll", user=sam)
    xid = ok(web, "POST", f"/api/groups/{league}/brief", user=sam)["exchange"]["id"]
    ok(web, "POST", f"/api/exchanges/{xid}/ask", {"text": "What is a release clause?"}, sam)
    status, payload = dispatch(web, "POST", f"/api/exchanges/{xid}/close", {}, {}, sam)
    assert status == 200
    assert set(payload) == {"closed", "groups", "calls"}
