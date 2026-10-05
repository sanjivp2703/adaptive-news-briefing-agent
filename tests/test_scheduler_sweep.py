"""The background sweep: who gets polled.

Persona users have no live feed by design -- their events are injected by the
harness -- so polling them would both spend real API budget and pollute the
persona's deterministic event stream with live data. Since the eval harness
now scores set agreement between the system's ledger and the persona's true
concept set, a persona whose event stream is half live is not measuring
anything at all.
"""

from __future__ import annotations

from conftest import ScriptedFeed, material_verdict

from conversational_agent import config
from conversational_agent.judgment import StubClient

EVENT = [{"headline": "Star guard traded", "occurred_at": "2026-09-01T10:00:00+00:00"}]


# Prevents: the live poller hitting the web on behalf of synthetic personas --
# real API spend per persona, and injected-vs-live events mixed in one stream,
# which makes every persona ledger-agreement metric meaningless.
def test_the_sweep_polls_real_users_and_skips_personas(build_system):
    stub = StubClient()
    stub.register(config.MATERIALITY, material_verdict())
    feed = ScriptedFeed(events=EVENT)
    # build_system skips the test if the wired system cannot be imported, so
    # the scheduler import below is only reached once that holds.
    system = build_system(stub, feed=feed)
    from conversational_agent.scheduler import sweep_once

    try:
        real = system.store.create_user("human", kind="real")
        persona = system.store.create_user("noisy-persona", kind="persona")
        real_group = system.store.scope(real.id).create_group("NBA fans")
        persona_group = system.store.scope(persona.id).create_group("NBA fans")

        result = sweep_once(system)

        assert result.polled == 1
        assert result.skipped_personas == 1
        assert result.material_found == 1
        assert result.errors == []
        # The feed was consulted for the real user's group only.
        assert feed.fetch_calls == [real_group.id]
        assert system.store.scope(persona.id).recent_events(persona_group.id) == []
        assert len(system.store.scope(real.id).recent_events(real_group.id)) == 1
    finally:
        system.close()
