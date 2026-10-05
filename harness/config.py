"""Harness-side model routing: which model plays the *person*, and which grades.

`src/conversational_agent/config.py` routes the system's judgment points. This
module routes the three calls that belong to the harness and never go through
`Judge`: the live persona's replies (`responders.LLMResponder`), the probe's
persona-answer call and the probe's grader (`probe.ProbeRunner`).

Precedence, highest first:

    explicit flag (--persona-model / --grader-model)
    > --cheap                       (persona voice only; see below)
    > HARNESS_PERSONA_MODEL / HARNESS_GRADER_MODEL environment overrides
    > the defaults (Sonnet for both)

`--cheap` moves ONLY the persona voice to Haiku. The grader is the reward
signal every learning metric is scored from, so it stays on Sonnet in cheap
mode too; `HARNESS_GRADER_MODEL` / `--grader-model` remain as explicit
overrides for experiments that want to measure the grader itself.

The resolved routing is written into the artifact's non-volatile header
(`run_settings.harness_routing`) so `harness.gate` can refuse to pair a run
voiced by one model against a run voiced by another: a Haiku-voiced persona and
a Sonnet-voiced persona are different subjects, and a paired comparison across
them would attribute the voice change to the system under test.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

from conversational_agent import config as _cfg

PERSONA_MODEL_ENV = "HARNESS_PERSONA_MODEL"
GRADER_MODEL_ENV = "HARNESS_GRADER_MODEL"

DEFAULT_PERSONA_MODEL = _cfg.SONNET
DEFAULT_GRADER_MODEL = _cfg.SONNET
CHEAP_PERSONA_MODEL = _cfg.HAIKU


def persona_model(override: str | None = None, *, cheap: bool = False) -> str:
    """The model that voices the persona (replies and probe answers)."""
    if override:
        return override
    if cheap:
        return CHEAP_PERSONA_MODEL
    return os.environ.get(PERSONA_MODEL_ENV) or DEFAULT_PERSONA_MODEL


def grader_model(override: str | None = None) -> str:
    """The model that grades probe answers. Unaffected by `--cheap`."""
    if override:
        return override
    return os.environ.get(GRADER_MODEL_ENV) or DEFAULT_GRADER_MODEL


@dataclass(frozen=True)
class HarnessRouting:
    """What voiced and graded the personas in one run. Written to the artifact."""

    persona_model: str
    grader_model: str
    cheap: bool = False
    remediation_search: bool = True

    @classmethod
    def resolve(
        cls,
        *,
        cheap: bool = False,
        persona_model_override: str | None = None,
        grader_model_override: str | None = None,
        remediation_search: bool = True,
    ) -> HarnessRouting:
        return cls(
            persona_model=persona_model(persona_model_override, cheap=cheap),
            grader_model=grader_model(grader_model_override),
            cheap=cheap,
            # A cheap run also drops the Assessor's large-gap web search.
            remediation_search=remediation_search and not cheap,
        )

    @classmethod
    def legacy(cls) -> HarnessRouting:
        """The routing every artifact written before this header existed had:
        Sonnet voice, Sonnet grader, remediation search on."""
        return cls(
            persona_model=DEFAULT_PERSONA_MODEL,
            grader_model=DEFAULT_GRADER_MODEL,
            cheap=False,
            remediation_search=True,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> HarnessRouting:
        if not data:
            return cls.legacy()
        return cls(
            persona_model=str(data.get("persona_model") or DEFAULT_PERSONA_MODEL),
            grader_model=str(data.get("grader_model") or DEFAULT_GRADER_MODEL),
            cheap=bool(data.get("cheap", False)),
            remediation_search=bool(data.get("remediation_search", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def voice_line(self) -> str:
        """The report's 'persona voice' line."""
        return (
            f"persona voice: {self.persona_model}"
            + ("  (--cheap)" if self.cheap else "")
            + f"; probe grader: {self.grader_model}"
            + ("" if self.remediation_search else "; remediation search off")
        )
