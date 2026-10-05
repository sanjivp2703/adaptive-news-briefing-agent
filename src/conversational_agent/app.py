"""Assembly. One place that wires the pieces together.

Kept separate from the components themselves so the eval harness can build the
identical system with only two things substituted -- the LLM client and the
event feed -- and everything else genuinely under test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .assessor import Assessor
from .judgment import AnthropicClient, Judge, LLMClient
from .monitor import EventFeed, LiveSearchFeed, Monitor
from .orchestrator import Orchestrator
from .store import Store


class MissingCredentials(RuntimeError):
    pass


def _anthropic_raw_client() -> Any:
    """The underlying SDK client, used for server-side web search."""
    import anthropic

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise MissingCredentials(
            "No Anthropic credentials found. Set ANTHROPIC_API_KEY in your "
            "environment before "
            "running anything that makes live calls. Offline commands and the "
            "persona harness with --offline do not need this."
        )
    return anthropic.Anthropic()


@dataclass
class System:
    store: Store
    judge: Judge
    monitor: Monitor
    assessor: Assessor
    orchestrator: Orchestrator

    def close(self) -> None:
        self.store.close()


def build(
    *,
    db_path: Path | None = None,
    llm_client: LLMClient | None = None,
    raw_client: Any = None,
    feed: EventFeed | None = None,
    run_id: str | None = None,
    offline: bool = False,
    reply_search: bool = True,
) -> System:
    """Construct the system.

    offline=True builds it with no credentials and no network: callers must
    supply `llm_client` and `feed`. That is the mode the persona harness and
    the unit tests run in.

    reply_search=False stops `Assessor.respond` from searching when a question
    is not answered by the source event or the briefing; the reply then says
    plainly that it could not look. Default on -- see `Assessor.__init__`.
    """
    store = Store(db_path)

    if offline:
        if llm_client is None:
            raise ValueError("offline=True requires an llm_client (e.g. StubClient)")
        raw = raw_client
    else:
        raw = raw_client or _anthropic_raw_client()
        llm_client = llm_client or AnthropicClient(raw)
        if config.local_model_enabled():
            # The v2 training track's seat: the points in LOCAL_MODEL_POINTS
            # (default: briefing) go to an OpenAI-compatible local endpoint,
            # everything else to whatever client was resolved above. Wrapping
            # a caller-supplied client too is deliberate -- the harness's live
            # mode supplies its own, and the regression has to be able to put
            # the local model in the briefing seat without touching harness/.
            from .local_client import LocalModelClient, RoutingClient

            llm_client = RoutingClient(
                local=LocalModelClient(),
                default=llm_client,
                points=config.local_model_points(),
            )

    judge = Judge(client=llm_client, store=store, run_id=run_id)
    assessor = Assessor(judge, client=raw, reply_search=reply_search)
    resolved_feed = feed or LiveSearchFeed(judge=judge, client=raw)
    monitor = Monitor(judge, resolved_feed)
    orchestrator = Orchestrator(judge, assessor)

    return System(
        store=store,
        judge=judge,
        monitor=monitor,
        assessor=assessor,
        orchestrator=orchestrator,
    )
