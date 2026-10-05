"""The web surface: the same system the CLI drives, in a browser.

Nothing here adds a judgment point, a prompt, or a rule. Every endpoint is a
thin wrapper over the calls `cli.py` already makes -- `Orchestrator.open_session`,
`Assessor.raise_topic` / `respond` / `close_thread`, `Monitor.poll`, and the
store's read paths -- so the browser and the terminal are two views of one
product and cannot drift apart in behaviour.

The CLI's two audiences are kept as two modules, and the split is load-bearing:

  * `reader.py`  -- what the person using the product sees. It never reads a
    proficiency band, a score, or a before/after; `tests/test_web_no_score.py`
    enforces that structurally, the same way the CLI's session path is guarded.
  * `console.py` -- the builder-facing LLMOps console: ledger, traces, stats,
    models, prompts, eval runs.

`server.py` is transport only: routing, JSON, static files, the optional
background poller. Stdlib `http.server`, in keeping with a project whose only
third-party import is the Anthropic SDK.
"""


class ApiError(Exception):
    """A user-facing failure, carried to the browser as `{error: message}`."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message
