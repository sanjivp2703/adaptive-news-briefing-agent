"""Judgment point names are a three-way contract.

The same string is a prompt filename, a model/effort routing key, and the
`judgment_point` value in every historical log row. Renaming one in a single
place orphans the other two, and the failure is silent until a live call.

The ledger redesign renamed one point and deleted another, which is exactly
the change this file exists to catch half-applied.
"""

from __future__ import annotations

import re

from conversational_agent import config
from conversational_agent.judgment import load_prompt

_VERSION = re.compile(r"^v?\d+(\.\d+)*$")


# Prevents: the routed-call set drifting from six scored judgment points plus
# two generative calls; a newly added point with no model route or a nonsense
# effort value; a renamed point whose prompt file did not follow it (breaks
# only at call time); an unversioned prompt (makes a metric change
# unattributable to a revision); and an orphaned prompt file left behind by a
# half-applied rename.
def test_the_routed_call_set_is_six_judgment_points_plus_two_generative_calls():
    assert len(config.ALL_JUDGMENT_POINTS) == 6
    assert len(config.ROUTED_CALLS) == 8
    for generative in (config.BRIEFING, config.THREAD_REPLY):
        assert generative in config.ROUTED_CALLS
        assert generative not in config.ALL_JUDGMENT_POINTS
    assert set(config.ALL_JUDGMENT_POINTS) < set(config.ROUTED_CALLS)

    for point in config.ROUTED_CALLS:
        assert config.model_for(point), f"{point} has no model route"
        assert config.effort_for(point) in {"low", "medium", "high"}

        prompt = load_prompt(point)
        assert prompt.version != "unversioned", f"{point} prompt has no version header"
        assert _VERSION.match(
            prompt.version
        ), f"{point} version {prompt.version!r} is not a version"
        assert prompt.text.strip(), f"{point} prompt body is empty"

    on_disk = {p.stem for p in config.PROMPTS_DIR.glob("*.md")}
    assert on_disk == set(config.ROUTED_CALLS)
