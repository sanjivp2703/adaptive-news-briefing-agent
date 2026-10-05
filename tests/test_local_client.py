"""The local / open-model seat: JSON extraction, the failure path, routing,
and the `app.build` hook. All offline: the wire is a fake `post` callable.
"""

from __future__ import annotations

import json

import pytest
from conftest import ScriptedFeed

from conversational_agent import app, config
from conversational_agent.judgment import Judge, JudgmentError, RawCompletion
from conversational_agent.judgments import BRIEFING_SCHEMA, MATERIALITY_SCHEMA
from conversational_agent.local_client import (
    LocalModelClient,
    RoutingClient,
    extract_json_object,
    judgment_point_of,
)

VERDICT = {
    "briefing": "The harvest — the picking of the grapes — started on 6 August.",
    "topic": "early harvest",
    "terms_used": ["harvest"],
    "explained_terms": ["harvest"],
    "subdomains": [{"term": "harvest", "subdomain": "viticulture & harvest"}],
    "reasoning": "one unseen event",
}


def _chat_reply(content, model="qwen-test", prompt_tokens=120, completion_tokens=80):
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }
    )


class FakeWire:
    """Stands in for `_urllib_post`. Scripted (status, body) per call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, body, headers, timeout):
        self.requests.append({"url": url, "body": body, "headers": headers})
        status, text = self.responses.pop(0)
        return status, text


@pytest.fixture
def local_env(monkeypatch):
    monkeypatch.setenv(config.LOCAL_MODEL_BASE_URL_ENV, "http://localhost:11434/v1")
    monkeypatch.setenv(config.LOCAL_MODEL_NAME_ENV, "qwen-test")
    monkeypatch.delenv(config.LOCAL_MODEL_POINTS_ENV, raising=False)
    monkeypatch.delenv(config.LOCAL_MODEL_API_KEY_ENV, raising=False)


@pytest.fixture
def no_local_env(monkeypatch):
    for key in (
        config.LOCAL_MODEL_BASE_URL_ENV,
        config.LOCAL_MODEL_NAME_ENV,
        config.LOCAL_MODEL_POINTS_ENV,
        config.LOCAL_MODEL_API_KEY_ENV,
    ):
        monkeypatch.delenv(key, raising=False)


# --- JSON extraction -------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        json.dumps(VERDICT),
        "```json\n" + json.dumps(VERDICT) + "\n```",
        "Here is the JSON you asked for:\n\n" + json.dumps(VERDICT) + "\n\nLet me know!",
        "Sure.\n```\n" + json.dumps(VERDICT, indent=2) + "\n```\nDone.",
    ],
    ids=["bare", "fenced", "prefixed", "fenced-indented-with-chatter"],
)
def test_extracts_json_from_fenced_and_prefixed_replies(reply):
    assert json.loads(extract_json_object(reply)) == VERDICT


def test_extraction_survives_braces_inside_strings():
    tricky = dict(VERDICT, briefing="Yields fell {sharply} — a brace } in prose.")
    text = "Answer: " + json.dumps(tricky) + " }"
    assert json.loads(extract_json_object(text)) == tricky


def test_extraction_returns_none_for_no_object():
    assert extract_json_object("I cannot produce that.") is None
    assert extract_json_object("{not json") is None
    assert extract_json_object("") is None


# --- The client over a fake wire ------------------------------------------


def test_client_sends_system_with_schema_tail_and_returns_completion(local_env):
    wire = FakeWire((200, _chat_reply("```json\n" + json.dumps(VERDICT) + "\n```")))
    client = LocalModelClient(post=wire)

    raw = client.complete_json(
        model=config.model_for(config.BRIEFING),
        system="You brief people.",
        user_content=json.dumps({"_judgment_point": "briefing", "context": {}}),
        schema=BRIEFING_SCHEMA,
        effort="medium",
        max_tokens=2000,
    )

    assert isinstance(raw, RawCompletion)
    assert json.loads(raw.text) == VERDICT
    assert raw.model == "qwen-test"
    assert (raw.input_tokens, raw.output_tokens) == (120, 80)

    sent = wire.requests[0]
    assert sent["url"] == "http://localhost:11434/v1/chat/completions"
    assert sent["body"]["model"] == "qwen-test"
    assert sent["body"]["response_format"] == {"type": "json_object"}
    system_msg, user_msg = sent["body"]["messages"]
    assert system_msg["role"] == "system"
    assert system_msg["content"].startswith("You brief people.")
    assert '"additionalProperties": false' in system_msg["content"]
    assert user_msg == {
        "role": "user",
        "content": json.dumps({"_judgment_point": "briefing", "context": {}}),
    }
    assert "Authorization" not in sent["headers"]


def test_client_retries_without_response_format_when_rejected(local_env):
    wire = FakeWire(
        (400, '{"error": "response_format is not supported"}'),
        (200, _chat_reply(json.dumps(VERDICT))),
    )
    client = LocalModelClient(post=wire)
    raw = client.complete_json(
        model="x", system="s", user_content="{}", schema=BRIEFING_SCHEMA, effort="low"
    )
    assert json.loads(raw.text) == VERDICT
    assert "response_format" in wire.requests[0]["body"]
    assert "response_format" not in wire.requests[1]["body"]

    # Remembered: the next call skips the doomed first attempt.
    wire.responses.append((200, _chat_reply(json.dumps(VERDICT))))
    client.complete_json(
        model="x", system="s", user_content="{}", schema=BRIEFING_SCHEMA, effort="low"
    )
    assert len(wire.requests) == 3
    assert "response_format" not in wire.requests[2]["body"]


def test_api_key_goes_in_bearer_header_only(local_env, monkeypatch):
    monkeypatch.setenv(config.LOCAL_MODEL_API_KEY_ENV, "sk-local-secret")
    wire = FakeWire((200, _chat_reply(json.dumps(VERDICT))))
    LocalModelClient(post=wire).complete_json(
        model="x", system="s", user_content="{}", schema=BRIEFING_SCHEMA, effort="low"
    )
    assert wire.requests[0]["headers"]["Authorization"] == "Bearer sk-local-secret"
    assert "sk-local-secret" not in json.dumps(wire.requests[0]["body"])


def test_unparseable_reply_becomes_judge_error_not_exception(local_env, store):
    wire = FakeWire((200, _chat_reply("I'd rather not write JSON today.")))
    judge = Judge(client=LocalModelClient(post=wire), store=store)

    with pytest.raises(JudgmentError) as excinfo:
        judge(config.BRIEFING, context={"events": []}, schema=BRIEFING_SCHEMA)
    assert "unparseable JSON" in str(excinfo.value)

    rows = store.conn.execute(
        "SELECT judgment_point, model, error, verdict_json FROM judgment_log"
    ).fetchall()
    assert len(rows) == 1
    point, model, error, verdict = rows[0]
    assert point == config.BRIEFING
    assert model == "qwen-test"  # the log names who actually answered
    assert "unparseable JSON" in error
    assert verdict is None


def test_http_error_is_recorded_as_judgment_error(local_env, store):
    wire = FakeWire((500, "boom"), (500, "boom"))
    judge = Judge(client=LocalModelClient(post=wire), store=store)
    with pytest.raises(JudgmentError, match="HTTP 500"):
        judge(config.BRIEFING, context={}, schema=BRIEFING_SCHEMA)
    (error,) = store.conn.execute("SELECT error FROM judgment_log").fetchone()
    assert "HTTP 500" in error


def test_client_requires_base_url(no_local_env):
    with pytest.raises(JudgmentError, match=config.LOCAL_MODEL_BASE_URL_ENV):
        LocalModelClient()


# --- Routing ---------------------------------------------------------------


class Recorder:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return RawCompletion(text=json.dumps({"reasoning": self.name}), model=self.name)


def test_routing_sends_briefing_local_and_materiality_default():
    local, default = Recorder("local"), Recorder("default")
    router = RoutingClient(local=local, default=default, points=frozenset({"briefing"}))

    def call(point, schema):
        return router.complete_json(
            model="m",
            system="s",
            user_content=json.dumps({"_judgment_point": point, "context": {"k": 1}}, indent=2),
            schema=schema,
            effort="low",
        )

    assert call("briefing", BRIEFING_SCHEMA).model == "local"
    assert call("materiality", MATERIALITY_SCHEMA).model == "default"
    assert call("thread_reply", BRIEFING_SCHEMA).model == "default"
    assert len(local.calls) == 1 and len(default.calls) == 2
    # Arguments pass through untouched.
    assert local.calls[0]["schema"] is BRIEFING_SCHEMA
    assert local.calls[0]["max_tokens"] == 16000


def test_judgment_point_of_reads_judge_payload():
    payload = json.dumps({"_judgment_point": "gap_routing", "context": {"x": "y"}}, indent=2)
    assert judgment_point_of(payload) == "gap_routing"
    assert judgment_point_of("not json") is None


def test_points_come_from_env(local_env, monkeypatch):
    assert config.local_model_points() == frozenset({config.BRIEFING})
    monkeypatch.setenv(config.LOCAL_MODEL_POINTS_ENV, "briefing, thread_reply")
    assert config.local_model_points() == frozenset({"briefing", "thread_reply"})
    monkeypatch.setenv(config.LOCAL_MODEL_POINTS_ENV, "")
    assert config.local_model_points() == frozenset()


def test_model_for_reports_local_name_only_for_routed_points(local_env, monkeypatch):
    assert config.model_for(config.BRIEFING) == "qwen-test"
    assert config.model_for(config.MATERIALITY) == config.OPUS
    assert config.model_for(config.THREAD_REPLY) == config.SONNET
    # A MODEL_<POINT> override still applies to points that are not routed.
    monkeypatch.setenv("MODEL_THREAD_REPLY", "claude-opus-5")
    assert config.model_for(config.THREAD_REPLY) == "claude-opus-5"


def test_model_for_unchanged_without_env(no_local_env):
    assert config.model_for(config.BRIEFING) == config.SONNET
    assert config.local_model_points() == frozenset()


# --- The app.build hook ----------------------------------------------------


def test_offline_build_never_routes(local_env, db_path, stub):
    system = app.build(db_path=db_path, llm_client=stub, feed=ScriptedFeed(), offline=True)
    try:
        assert system.judge.client is stub
    finally:
        system.close()


def test_live_build_without_env_has_no_router(no_local_env, db_path):
    supplied = Recorder("anthropic-stand-in")
    system = app.build(
        db_path=db_path,
        llm_client=supplied,
        raw_client=object(),
        feed=ScriptedFeed(),
        offline=False,
    )
    try:
        assert system.judge.client is supplied
    finally:
        system.close()


def test_live_build_with_env_wraps_supplied_client(local_env, db_path):
    supplied = Recorder("anthropic-stand-in")
    system = app.build(
        db_path=db_path,
        llm_client=supplied,
        raw_client=object(),
        feed=ScriptedFeed(),
        offline=False,
    )
    try:
        router = system.judge.client
        assert isinstance(router, RoutingClient)
        assert router.default is supplied
        assert isinstance(router.local, LocalModelClient)
        assert router.points == frozenset({config.BRIEFING})
    finally:
        system.close()
