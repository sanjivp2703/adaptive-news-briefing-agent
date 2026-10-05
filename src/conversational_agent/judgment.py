"""The judgment-call harness: the one path every agentic decision goes through.

Design rules this file exists to enforce:

1. **Every judgment call is a pure function.** Context in, structured verdict
   out, no side effects and no tool use. That is what makes each of the
   judgment points independently testable with a fixed input, which is what
   the eval harness needs.

2. **Every call is structured.** The model must return schema-valid JSON with
   a `reasoning` field. The verdict is what gets scored; the reasoning is what
   gets read when diagnosing a failure.

3. **Every call is traced.** One row in `judgment_log` per call: inputs,
   verdict, reasoning, model, prompt version, tokens, latency. Without this you
   can detect that a metric moved but not why.

4. **Every prompt is versioned.** The prompt text lives in `prompts/<point>.md`
   with a version header, and that version is stamped on every logged call, so
   a metric change can be attributed to a specific prompt revision.

5. **The model is swappable per judgment point** (see `config.model_for`), so
   "should this call use a stronger model?" is an experiment, not a rewrite.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from functools import cache
from typing import Any, Protocol

from . import config


class JudgmentError(RuntimeError):
    """A judgment call could not produce a usable verdict."""


# --- Prompt loading --------------------------------------------------------

_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Prompt:
    point: str
    version: str
    text: str


@cache
def load_prompt(point: str) -> Prompt:
    """Read `prompts/<point>.md`, split its `--- version: ... ---` header."""
    path = config.PROMPTS_DIR / f"{point}.md"
    if not path.exists():
        raise JudgmentError(f"No prompt file for judgment point {point!r} at {path}")
    raw = path.read_text(encoding="utf-8")
    version = "unversioned"
    body = raw
    if raw.startswith("---"):
        _, _, rest = raw.partition("---")
        header, sep, remainder = rest.partition("---")
        if sep:
            match = _VERSION_RE.search(header)
            if match:
                version = match.group(1)
            body = remainder
    return Prompt(point=point, version=version, text=body.strip())


# --- Client seam -----------------------------------------------------------
# Everything that talks to the API goes through this protocol, so the eval
# harness and unit tests can run the entire system with a stub and zero
# network calls or spend.


@dataclass
class RawCompletion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class LLMClient(Protocol):
    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        schema: dict[str, Any],
        effort: str,
        max_tokens: int = 16000,
    ) -> RawCompletion: ...


class AnthropicClient:
    """Real client. Structured output via `output_config.format`."""

    def __init__(self, client: Any = None):
        if client is None:
            import anthropic  # imported lazily so tests need no key

            client = anthropic.Anthropic()
        self._client = client

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
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            # The judgment prompt is stable across calls and goes in `system`
            # so it sits at the front of the cache prefix; the volatile
            # per-call context goes in `messages` behind it.
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            category = getattr(detail, "category", None)
            raise JudgmentError(f"Model declined the request (category={category})")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return RawCompletion(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
        )


class StubClient:
    """Deterministic stand-in used by tests and by `--offline` eval runs.

    Callers register a handler per judgment point; each receives the parsed
    context dict and returns the verdict dict. No network, no spend, and
    fully reproducible -- the same properties that made scripted personas the
    right v1 choice.
    """

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []

    def register(self, point: str, handler) -> None:
        self.handlers[point] = handler

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
        self.calls.append({"point": point, "context": payload})
        handler = self.handlers.get(point)
        if handler is None:
            raise JudgmentError(
                f"StubClient has no handler registered for {point!r}. "
                "Register one before running this path offline."
            )
        verdict = handler(payload.get("context", {}))
        return RawCompletion(
            text=json.dumps(verdict), input_tokens=0, output_tokens=0, model=model
        )


# --- The judge -------------------------------------------------------------


@dataclass
class Verdict:
    """A judgment call's structured result plus its trace metadata."""

    point: str
    data: dict[str, Any]
    reasoning: str
    model: str
    prompt_version: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    log_id: str | None = None

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


_LITERAL_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _decode_literal_escapes(value: Any) -> Any:
    """Turn a literal six-character `\\u2014` inside model text back into `—`.

    Belt to the braces of `ensure_ascii=False` above: even with clean input a
    model can emit the escape as text, and json.loads then hands us the
    literal backslash sequence rather than the character. Applied to every
    string in a verdict, recursively; anything that is not such a sequence is
    left exactly as it was.
    """
    if isinstance(value, str):
        if "\\u" not in value:
            return value
        return _LITERAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), value)
    if isinstance(value, list):
        return [_decode_literal_escapes(v) for v in value]
    if isinstance(value, dict):
        return {k: _decode_literal_escapes(v) for k, v in value.items()}
    return value


@dataclass
class Judge:
    """Runs judgment calls and records every one of them.

    `store` is optional so a judgment point can be exercised in isolation
    (e.g. a unit test) without a database; when present, every call is logged.
    """

    client: LLMClient
    store: Any = None
    run_id: str | None = None

    def __call__(
        self,
        point: str,
        *,
        context: dict[str, Any],
        schema: dict[str, Any],
        user_id: str | None = None,
        group_id: str | None = None,
        # These models think adaptively, and thinking is billed against
        # max_tokens. A live interrupt_timing call spent its entire 4096
        # budget reasoning and returned truncated JSON -- a silently degraded
        # judgment, not an error. Verdicts themselves run 550-750 tokens, so
        # the headroom costs nothing on a normal call.
        max_tokens: int = 16000,
    ) -> Verdict:
        prompt = load_prompt(point)
        model = config.model_for(point)
        effort = config.effort_for(point)

        # The wire payload carries the point name so StubClient can dispatch,
        # and so a logged input is self-describing when read back later.
        # ensure_ascii=False matters: with the default, every en dash, pound
        # sign or accented name in a source article reaches the model as a
        # literal `\u2014` / `\u00a3` escape, and the model sometimes copies
        # that six-character sequence back into prose the user then reads.
        # Two replies in the first complete live run did exactly that.
        payload = json.dumps(
            {"_judgment_point": point, "context": context},
            indent=2,
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )

        started = time.perf_counter()
        error: str | None = None
        raw: RawCompletion | None = None
        data: dict[str, Any] = {}
        try:
            raw = self.client.complete_json(
                model=model,
                system=prompt.text,
                user_content=payload,
                schema=schema,
                effort=effort,
                max_tokens=max_tokens,
            )
            data = _decode_literal_escapes(json.loads(raw.text))
        except JudgmentError as exc:
            error = str(exc)
        except json.JSONDecodeError as exc:
            error = f"Model returned unparseable JSON: {exc}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.perf_counter() - started) * 1000)

        reasoning = str(data.get("reasoning", "")) if data else ""
        log_id = None
        if self.store is not None:
            from .store import new_id

            log_id = new_id("jdg")
            self.store.log_judgment(
                {
                    "id": log_id,
                    "judgment_point": point,
                    "user_id": user_id,
                    "group_id": group_id,
                    "prompt_version": prompt.version,
                    "model": model,
                    "effort": effort,
                    "input_json": payload,
                    "verdict_json": json.dumps(data) if data else None,
                    "reasoning": reasoning,
                    "input_tokens": raw.input_tokens if raw else None,
                    "output_tokens": raw.output_tokens if raw else None,
                    "latency_ms": latency_ms,
                    "error": error,
                    "run_id": self.run_id,
                }
            )

        if error:
            raise JudgmentError(f"[{point}] {error}")

        return Verdict(
            point=point,
            data=data,
            reasoning=reasoning,
            model=model,
            prompt_version=prompt.version,
            latency_ms=latency_ms,
            input_tokens=raw.input_tokens if raw else 0,
            output_tokens=raw.output_tokens if raw else 0,
            log_id=log_id,
        )


def _strip_bounds(node: Any) -> Any:
    """Remove numeric `minimum`/`maximum`, folding them into the description.

    The structured-output API rejects those keywords on numeric types
    (400: "For 'number' type, properties maximum, minimum are not supported").
    They are still worth stating, so they move into the description where the
    model will read them -- and callers clamp on the way into the store, since
    a described range is guidance rather than a guarantee.
    """
    if isinstance(node, list):
        return [_strip_bounds(n) for n in node]
    if not isinstance(node, dict):
        return node

    out = {k: _strip_bounds(v) for k, v in node.items() if k not in ("minimum", "maximum")}
    lo, hi = node.get("minimum"), node.get("maximum")
    if node.get("type") in ("number", "integer") and (lo is not None or hi is not None):
        if lo is not None and hi is not None:
            hint = f"A number from {lo} to {hi}."
        elif lo is not None:
            hint = f"A number of at least {lo}."
        else:
            hint = f"A number of at most {hi}."
        existing = out.get("description", "")
        out["description"] = f"{existing} {hint}".strip()
    return out


def schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Build a strict JSON schema.

    `additionalProperties: false` plus an explicit `required` list is what the
    API needs to guarantee the response validates.
    """
    props = _strip_bounds(dict(properties))
    props.setdefault(
        "reasoning",
        {
            "type": "string",
            "description": "Brief explanation of why you reached this verdict.",
        },
    )
    req = list(required)
    if "reasoning" not in req:
        req.append("reasoning")
    return {
        "type": "object",
        "properties": props,
        "required": req,
        "additionalProperties": False,
    }
