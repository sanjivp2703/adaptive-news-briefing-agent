"""A local / open-model seat for any routed call, plus the switch that fills it.

Two small classes, both speaking the `LLMClient` protocol in `judgment.py`:

* `LocalModelClient` -- talks to an OpenAI-compatible chat-completions
  endpoint (vLLM, Ollama, LM Studio, TGI, most hosted open-model providers).
  Configured by three environment variables and nothing else:

      LOCAL_MODEL_BASE_URL   e.g. http://localhost:11434/v1
      LOCAL_MODEL_NAME       e.g. qwen2.5:7b  (what the server calls the model)
      LOCAL_MODEL_API_KEY    optional bearer token; never logged

  It uses `urllib` so the project gains no dependency. The system prompt goes
  in the system message with the JSON schema appended to its tail (a local
  model has no structured-output mode to lean on); the payload goes in the
  user message; `response_format={"type": "json_object"}` is sent and quietly
  dropped if the server rejects it. The reply is searched for its first JSON
  object -- fences stripped, outermost braces -- and if none can be found the
  raw text is returned as-is, so `Judge` records its usual "unparseable JSON"
  error row instead of this class raising.

* `RoutingClient` -- holds two clients and dispatches each call by its
  judgment point (read off the `_judgment_point` field that `Judge` already
  puts in every payload). `LOCAL_MODEL_POINTS=briefing` names the points that
  go local; everything else goes to the default client unchanged.

Why this exists: the v2 training track distils the briefing call into a small
model. A distilled model is only worth anything if it can sit in the real
system's briefing seat, under the real prompt, with the real ledger in front of
it, and be measured by the same harness as the model it replaces. This file is
that seat. `app.build` installs the router when `LOCAL_MODEL_BASE_URL` is set
and does nothing at all when it is not.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import config
from .judgment import JudgmentError, LLMClient, RawCompletion

# --- JSON extraction -------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> str | None:
    """Return the first complete JSON object inside `text`, or None.

    Tolerates a code fence, a chatty prefix ("Here is the JSON:"), and a
    trailing sign-off. Works on the outermost brace pair, tracking string
    literals so a `}` inside a briefing sentence does not end the object early.
    The candidate must actually parse -- a stray brace is not an object.
    """
    if not text:
        return None
    candidates: list[str] = [m.group(1) for m in _FENCE_RE.finditer(text)]
    candidates.append(text)
    for chunk in candidates:
        found = _first_balanced_object(chunk)
        if found is not None:
            return found
    return None


def _first_balanced_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # malformed from this start; try the next `{`
                    if isinstance(parsed, dict):
                        return candidate
                    break
        start = text.find("{", start + 1)
    return None


# --- Transport -------------------------------------------------------------

Poster = Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, str]]
"""(url, json_body, headers, timeout) -> (status, response_text).

Injectable so tests can fake the wire without a server. The default is
`_urllib_post`; an HTTP error status is returned, not raised, so the caller
can decide what to retry.
"""


def _urllib_post(
    url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, payload
    except urllib.error.URLError as exc:
        raise JudgmentError(
            f"Local model endpoint unreachable at {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise JudgmentError(f"Local model endpoint timed out at {url}") from exc


# --- The local seat --------------------------------------------------------


SCHEMA_TAIL = (
    "\n\n## Output format\n\n"
    "Reply with ONE JSON object and nothing else -- no prose before or after "
    "it, no code fence. It must validate against this JSON schema:\n\n"
)


@dataclass
class LocalModelClient:
    """`LLMClient` over an OpenAI-compatible `/chat/completions` endpoint."""

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    timeout: float = 180.0
    post: Poster = field(default=_urllib_post, repr=False)
    # Set to False after the server rejects `response_format` once, so every
    # later call skips the doomed first attempt.
    _send_response_format: bool = field(default=True, repr=False)

    def __post_init__(self) -> None:
        base = self.base_url or os.environ.get(config.LOCAL_MODEL_BASE_URL_ENV) or ""
        if not base:
            raise JudgmentError(
                f"LocalModelClient needs a base URL: set {config.LOCAL_MODEL_BASE_URL_ENV} "
                "(e.g. http://localhost:11434/v1)."
            )
        self.base_url = base.rstrip("/")
        self.model = self.model or config.local_model_name()
        if self.api_key is None:
            self.api_key = os.environ.get(config.LOCAL_MODEL_API_KEY_ENV)

    @property
    def endpoint(self) -> str:
        assert self.base_url is not None
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

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
        # `model` arrives from `config.model_for(point)`, which already reports
        # the local name for routed points; the env-configured name is the
        # one the server knows, so it wins when the two differ.
        served_model = self.model or model
        system_text = system + SCHEMA_TAIL + "```json\n" + json.dumps(schema, indent=2) + "\n```"
        body: dict[str, Any] = {
            "model": served_model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        if self._send_response_format:
            body["response_format"] = {"type": "json_object"}

        status, text = self.post(self.endpoint, body, self._headers(), self.timeout)
        if status >= 400 and "response_format" in body:
            # Many servers 400 on response_format; the prompt tail carries the
            # schema anyway, so retry bare and remember not to bother again.
            self._send_response_format = False
            body = {k: v for k, v in body.items() if k != "response_format"}
            status, text = self.post(self.endpoint, body, self._headers(), self.timeout)
        if status >= 400:
            raise JudgmentError(
                f"Local model endpoint returned HTTP {status}: {text[:300]}"
            )

        try:
            reply = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JudgmentError(
                f"Local model endpoint returned a non-JSON body: {exc}"
            ) from exc

        content = _message_content(reply)
        usage = reply.get("usage") or {}
        extracted = extract_json_object(content)
        return RawCompletion(
            # On a parse failure the raw text goes back untouched, so `Judge`'s
            # own json.loads fails and logs "unparseable JSON" -- one error
            # path for every client, not a special one for this seat.
            text=extracted if extracted is not None else content,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=str(reply.get("model") or served_model),
        )


def _message_content(reply: dict[str, Any]) -> str:
    """`choices[0].message.content`, tolerating the list-of-parts form and
    the `text` field some completion-style servers use instead."""
    choices = reply.get("choices") or []
    if not choices:
        raise JudgmentError("Local model endpoint returned no choices")
    first = choices[0] or {}
    message = first.get("message") or {}
    content = message.get("content")
    if content is None:
        content = first.get("text", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


# --- The switch ------------------------------------------------------------

_POINT_RE = re.compile(r'"_judgment_point"\s*:\s*"([^"]+)"')


def judgment_point_of(user_content: str) -> str | None:
    """The point name `Judge` stamps into every payload."""
    match = _POINT_RE.search(user_content)
    if match:
        return match.group(1)
    try:
        return json.loads(user_content).get("_judgment_point")
    except (json.JSONDecodeError, AttributeError):
        return None


@dataclass
class RoutingClient:
    """Send the points in `points` to `local`, everything else to `default`."""

    local: LLMClient
    default: LLMClient
    points: frozenset[str] = field(default_factory=config.local_model_points)

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
        point = judgment_point_of(user_content)
        target = self.local if point in self.points else self.default
        return target.complete_json(
            model=model,
            system=system,
            user_content=user_content,
            schema=schema,
            effort=effort,
            max_tokens=max_tokens,
        )
