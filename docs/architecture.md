# Architecture

This document describes how the system is built and why. The
[README](../README.md) covers what the product is; the
[decision log](checkpoints.md) and [prompt changelog](prompt-changelog.md)
hold the evidence behind the choices summarised here.

## 1. Design principles

1. **The model judges; code does everything else.** A model is called only
   where a judgment is needed: is this event material, which sources are
   trustworthy, what is worth interrupting for, what did this thread reveal.
   Polling, queueing, counting, deadlines and the ledger's state machine are
   deterministic. There is no agent framework and no tool-use loop; control
   flow is ordinary Python.
2. **Every judgment is a pure, structured, logged function.** Context in,
   schema-valid JSON out, one row written to a log. That is what makes each
   call testable with a fixed input, replaceable by a stub, and replaceable
   by a different model.
3. **Observed facts, not inferred scores.** "They asked what a vintage is" is
   a fact. "They are at 47/100" is an inference that then has to be
   validated. The store records the first kind.
4. **User scoping is structural.** Isolation between users is a property of
   the API shape, not of each query remembering a `WHERE` clause.
5. **Nothing the system says to itself counts as evidence about the user.**
   Using a term in front of someone, or defining it unprompted, changes
   nothing until the user does something.

## 2. Components and data flow

```
                    background, per (user, group)
  scheduler ──► Monitor ──► query_formulation ──► web search ──► source_selection
                   │                                                   │
                   └────────────── materiality ◄── candidate events ◄──┘
                                       │
                                       ▼
                               monitor_events  (material + not yet shown = the queue)

                    on demand, when the user opens a session
  Orchestrator ──► interrupt_timing ──► events surfaced, topic group chosen
        │
        ▼
     Assessor.raise_topic ──► briefing ──► exchange opened (one event, linked)
     Assessor.respond     ──► gap_routing ──► [search] ──► thread_reply
     Assessor.close_thread ─► concept_evidence ──► ledger writes, skip resolution
```

**Monitor** (`monitor.py`, `search.py`). One poll: formulate two to four
queries for the group, run them through Anthropic's server-side web search,
ask a second call which results are real, recent and credible, record each
surviving candidate as an event, and judge its materiality. Materiality is
given the group and recent events, and deliberately not the reader: an event
does not become more material because one reader is behind.

**Scheduler** (`scheduler.py`). Sweeps for groups whose interval has elapsed
and polls them. Detection is independent of sessions; what is session-gated
is surfacing. Persona users (the evaluation harness's simulated users) are
structurally refused live search, at the scheduler and again inside the
Monitor, so a synthetic user can never bill real calls.

**Orchestrator** (`orchestrator.py`). Which groups warrant attention is plain
counting: unseen material events, never engaged, goal deadline near. The one
judgment is `interrupt_timing`: of everything pending, what is surfaced now
and what stays queued. Only events the verdict explicitly surfaces are marked
shown; the held-back count is computed from the queue, not taken from the
model.

**Assessor** (`assessor.py`). Three operations:

- `raise_topic` picks the single most material *untold* event, writes a
  briefing for this reader, and opens an exchange linked to that event. The
  link is what retires the event from the untold queue, which makes "never
  tell a story twice" a property of the schema rather than a request in a
  prompt.
- `respond` answers one question. It records the user's turn first, so a
  failed answer never loses what they asked. `gap_routing` decides whether
  the answer is held (source event, briefing, general knowledge) or must be
  looked up; a lookup runs the same search pipeline. If nothing reliable is
  found it says so.
- `close_thread` runs once, when the user stops. It reads the whole thread
  for evidence and applies it to the ledger. It never runs per turn: what a
  thread reveals is visible only when it is whole, and there is no judgment
  about whether the user is "done" because a model deciding that would be
  wrong in the one direction that cannot be recovered.

## 3. The judgment wrapper

`judgment.py` is the single path every model call takes.

- **Prompts are files** (`prompts/<point>.md`) with a `version:` header.
  The version is logged with every call, so any row can be tied to the
  exact instructions that produced it.
- **Schemas are explicit.** Each call declares a JSON schema; a `reasoning`
  field is injected into every one. The verdict is what gets scored; the
  reasoning is what gets read when diagnosing a failure.
- **Clients are swappable.** `LLMClient` is a one-method protocol.
  `AnthropicClient` makes the real call. `StubClient` dispatches to
  registered Python handlers, which is how the test suite and the offline
  harness run with no network. `RoutingClient` sends named calls to a local
  OpenAI-compatible endpoint and the rest to Claude.
- **Every call is logged** to `judgment_log`: exact input JSON, verdict JSON,
  reasoning, model, effort, prompt version, tokens, latency, error, and an
  eval run id when there is one. A failed call logs its error in place of a
  verdict.

Model and effort are resolved per call from `config.py` and can be overridden
by `MODEL_<POINT>` / `EFFORT_<POINT>` environment variables, which is how
A/B comparisons run without a code change.

## 4. The store

One SQLite file in WAL mode (`db.py`, `store.py`), so the poller can write
while a session reads.

| Table | Holds |
|---|---|
| `users`, `groups`, `goals` | Identity and configuration. At most one active goal per group, enforced by a partial unique index. |
| `monitor_events` | Every candidate event, its materiality verdict, and when it was surfaced. |
| `exchanges`, `turns` | A briefing and the thread under it; the reading signal; evidence fields filled once at close. |
| `concepts` | The ledger: one row per (user, group, term). |
| `judgment_log`, `eval_runs` | The trace of every model call; evaluation run records. |

**Scoping.** `Store` owns the connection and exposes only inherently
cross-user operations (users, the scheduler sweep, the log). Everything else
is on `UserScope`, obtained with `store.scope(user_id)`. A `UserScope` has no
method that accepts another user's id, and every query it issues carries its
own. `tests/test_user_isolation.py` exercises this, and the web API's
equivalent test confirms another user's rows report as absent.

**Migrations.** Additive columns are applied idempotently on open. One
change needed a table rebuild (a new state in a `CHECK` constraint); that
rebuild is resumable, so an interrupted run cannot strand the ledger.

## 5. The concept ledger

States, weakest to strongest evidence: `unknown`, `provisional`, `familiar`,
`explained`, `confirmed`.

Writes happen in exactly these places:

| Trigger | Write |
|---|---|
| We used a term in a briefing or answer | `exposure_count += 1`. No state change, ever. |
| Thread closed; briefing was read; term was glossed and not asked about | `read_explanations += 1`; `unknown` becomes `familiar`. |
| User asked about the term and we answered | `explained`. |
| User used the term correctly | `provisional`, then `confirmed` on a second independent use. |
| User revealed a misunderstanding | `unknown`, from any state; counters reset. |

When one thread yields overlapping evidence, precedence is explicit: asked
about wins, then misunderstood, then understood. Being wrong in that
direction costs one extra explanation, which is the cheap direction.

**Why these rules.** Three came from measurement and a literature review of
knowledge-inference validity (about 40 sources):

- An `assumed` state, inferred from terms seen and not questioned, was
  measured against simulated users with known ground truth. It was
  anti-predictive at exposure thresholds 1, 2, 3, 5 and 8, and was deleted.
- `confirmed` requires two uses because one correct use, moments after the
  briefing supplied the term, may be parroting.
- The proficiency band is computed only over terms the user gave a signal
  about, so silence votes neither way. It is derived on read, never stored.

**Term identity.** Terms are normalised and merged on write
(`canonical_term`) so "ETF flows" and "spot ETF flows" resolve to one row.
This is partial; see Known limitations.

**Subdomains.** The calls that name a term also label the slice of the group
it belongs to ("transfer market", "officiating & rules"). Familiarity is the
same known/attested arithmetic per label, which lets a briefing be pitched
as to a peer in one slice and as to a beginner in another.

**Reading behaviour.** `read_quality` buckets dwell and scroll into
skipped, skimmed, read or studied with a deterministic function that has no
access to a scope, so it cannot write to the ledger. The evidence-extraction
call is never shown the reading signal. A skipped briefing is labelled
informed or lazy only when a later thread produces evidence about the terms
it defined.

## 6. Interfaces

Both interfaces call the same objects; neither contains product logic.

**CLI** (`cli.py`). User commands (`init`, `user`, `group`, `goal`, `poll`,
`session`) and operator commands (`ledger`, `models`, `prompts`, `traces`,
`trace`, `stats`, `runs`). Operator commands make no model calls and need no
credentials.

**Web** (`web/`). A standard-library HTTP server and a browser app with no
framework and no build step.

- `reader.py` builds what the user sees. `console.py` builds the builder's
  view. The split is enforced: `tests/test_web_no_score.py` parses
  `reader.py` and fails if it reads a band, a score, or a thread outcome
  field, and walks a whole reader journey checking every payload.
- Each request opens its own SQLite connection. Read-only endpoints open
  only the store and work with no credentials; live endpoints build the full
  system.
- The browser measures how long a briefing was on screen in a visible tab
  and how far it was scrolled, once, before the reader's first action, and
  reports it as `observed`. The CLI can only proxy this from response time.
- Live endpoints return the judgment calls they made, so the page can show
  what happened behind each action and link to the trace.
- No login. The server binds to loopback, requires a loopback `Host`, a
  matching `Origin` when one is sent, and `application/json` on every POST,
  so a page in another tab cannot drive it or spend credit through it.
- `--demo` serves a recorded store and refuses live calls rather than
  faking them.

Shared read-side logic (cost, trace scoping, stat aggregation) lives in
`reporting.py`, so the terminal and the browser cannot disagree.

## 7. Evaluation

Covered in [harness/README.md](../harness/README.md). In outline:

- **Substitution points.** `app.build(..., offline=True)` takes an LLM client
  and an event feed and wires everything else for real. Tests, the offline
  harness and the live harness differ only in those two arguments.
- **Ground truth.** Personas carry concept sets; a per-term memory model
  updates what they hold as they are briefed; a hidden probe grades each
  term 0 to 3 after the session. Ledger precision and recall are computed
  against that.
- **Leak checks.** The harness verifies that persona ground truth and
  simulated reading behaviour never reach the system under test.
- **Gate and proposer.** The gate compares a run with a baseline in three
  tiers and hard-fails on a repeated story or an unsupported figure. The
  proposer reads failures and proposes prompt diffs that may not reverse any
  recorded decision.

## 8. Local-model seat and training

`local_client.py` adapts an OpenAI-compatible endpoint to the `LLMClient`
protocol. With `LOCAL_MODEL_BASE_URL` set, the calls named in
`LOCAL_MODEL_POINTS` (default: `briefing`) go to that endpoint; the log
records the local model as the author. `training/` produces the data for
that seat: it rebuilds the exact packets the product sends the briefing
call, has the production prompt and model answer them, checks each answer
against the gate's hard rules, and packages a stratified split. See
[training/README.md](../training/README.md).

## 9. Known limitations

**Held for the next evaluated prompt revision.** Each of these changes what
a model is sent, so fixing it invalidates the recorded evaluation numbers.
They are listed rather than patched silently, and should be fixed together
with a prompt version bump and a fresh scored run.

- `source_selection` receives a search hit's page age in the field its
  prompt reads as a content snippet, so it judges on title, URL and age
  alone. It discards most hits as a result; recall of the monitor is lower
  than it should be.
- `gap_routing` is sent the proficiency band under a different key from the
  one its prompt names, and an evidence field that is always empty.
- Three prompts miscount their own lists ("three failure modes" followed by
  four), and two list an input they are not given or omit one they are.

**Open.**

- Concept labels still fragment across near-synonyms.
- The evidence extractor can confuse terms a question asks about with terms
  it correctly presupposes.
- "Answered" is judged per thread, not per question.
- Cost figures use uncached token prices and so understate cache writes and
  overstate cache reads.
- `engaged_at` on events is written only by the harness.
- The web surface has no authentication and is single-machine.
