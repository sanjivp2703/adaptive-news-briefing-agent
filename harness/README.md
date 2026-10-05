# Persona evaluation harness

```bash
PYTHONPATH=src:. .venv/bin/python -m harness.run              # offline by default: no network, free
PYTHONPATH=src:. .venv/bin/python -m harness.verify_offline   # the structural check; must pass
PYTHONPATH=src:. .venv/bin/python -m harness.run --show-thread pilar_wine:3   # one thread, verbatim
PYTHONPATH=src:. .venv/bin/python -m harness.run --live       # real models, costs money
PYTHONPATH=src:. .venv/bin/python -m harness.run --live --parallel 5   # same, ~5x faster
```

`harness.run` is offline unless `--live` is given; there is no `--offline`
flag. `--live` needs `ANTHROPIC_API_KEY` exported (see `.env.example`).

**The default offline run prints `RESULT: FAIL` and exits 1. That is expected.**
Two personas, `pilar_wine` and `sam_beginner`, do not reach the ledger gate
offline: against the scripted responder and the stub extractor their threads
produce no scored ledger evidence (elicitation 0.00), so precision and recall
are `n/a` and the threshold is never held. That is the ledger metric meeting a
stub that exercises no judgment, not a regression in the system; every
structural gate in the same report is clean. The check that must pass offline
is `python -m harness.verify_offline` (zero network, reproducibility, no
ground-truth or reading leakage, the structural refusal of judgment-quality
numbers, and the regression gates). Whether the ledger tracks what a persona
knows is a `--live` question; recorded live results are in
`docs/checkpoints.md`.

`--parallel N` replays up to N personas concurrently. The personas are already
independent — each has its own user, its own group, and a clock that restarts at
`EPOCH` — so this changes wall-clock time and nothing else; the artifact is
byte-identical to a sequential run and `--compare` shows it. It is worth using
under `--live`, where a run is almost entirely API latency. The store stays
shared, on one SQLite file in WAL with one connection per thread:
`check_isolation` asks whether persona A's rows are visible from persona B's
scope, which is not a question that can be asked of five separate databases.
`clock.installed` dispatches through a thread-local so parallel personas cannot
stamp each other's rows.

The system under test is the real one, assembled by `app.build`. Three things
are substituted — the clock, the LLM client and the event feed — and one thing
is added: the persona, which stands where a human reads and types, and reaches
the system only through a reading measurement and a sequence of message strings.

---

## The interaction model: the personas ask the questions

The system never tests the user. A briefing that ends in a question encodes
what its author assumes the reader already knows, and a beginner, who is the
primary case, cannot answer it. So:

1. **The system never asks the user a question.** A briefing states the
   substance and stops.
2. **The user asks, or does not.** An exchange is a multi-turn thread: briefing
   → the user may ask → the system answers → they may ask again → until they
   stop. The system never decides the conversation is over.
3. **Evidence is extracted from the whole thread**, once, when it goes quiet.
4. **Reading behaviour is observed and recorded**, and never touches the concept
   ledger. It is attention, not comprehension.

The history of how the design got here is in `docs/checkpoints.md`.

### Why a persona's question is the signal

A persona's *answer* to a question the system chose is, at best, a reaction to
the system's framing. A persona's *question* is not. What someone chooses to
ask is a far closer analogue of real usage, and it locates their knowledge
precisely, in their own words, on a subject they chose.

So question generation is the most diagnostic thing this harness produces,
and it is built to be faithful rather than plausible. Each form in
`responders.QUESTION_FORMS` declares what it needs from the persona's concept
sets, and no form is emitted whose preconditions the persona fails:

| tier | form | speech act | needs | example |
|---|---|---|---|---|
| 0 | `definition` | clarify | one term it lacks | *"what's a derogation?"* |
| 1 | `bridge` | clarify | one it lacks, one it holds | *"what's a derogation -- is that related to yields or separate?"* |
| 2 | `extend` | extend | one it holds | *"why does yields matter here?"* |
| 3 | `check_belief` | check-belief | one it holds | *"so does that mean yields is affected too?"* |
| 3 | `relation` | check-belief | two it holds | *"so is budburst the reason for the yields here, or is that separate?"* |

Every form is a **request**. The forms are the speech acts real people perform
after an informational message, at the rates they perform them
(`personas/research/assistant_reply_style_profile.md`, 80 hand-classified
WildChat turns): asking for more on something they hold is the mode (58% of
turns that engage with the reply), checking a belief *as a question* is 14%,
and asking what a term means is 6%. Asserting a fact ("yields must be down")
and explaining the topic to the assistant were **1 and 0 of 80**, so there is
no form for either. Knowledge is still detectable: *"so does that mean yields are
down?"* presupposes `yields` exactly as *"yields must be down"* did, and the
extractor reads the presupposition (`stub_judgments.extract_concept_evidence`
tells "what does X mean" from "so does that mean X..." by shape alone).

Selection is weighted 2^tier toward the highest tier a persona can satisfy, not
locked to it. Locking produces runs where most questions are the same form: a
well-informed persona holds nearly every term in front of it, so tier 3 is
always available and nothing else is ever drawn. Someone who *could* ask a probing question often asks a plain one instead, and a
harness whose most diagnostic output is one template is not diagnosing anything.

**`question_faithfulness` is a hard gate**, and it is the harness auditing
itself. It checks per turn that no question leans on a term the persona did not
hold *at the moment it spoke* — the concept sets are snapshotted per turn, since
a persona that asks what a term means in round 1 holds it in round 2 — and that
no term it held was asked about with a definition-class form. If that stops
being true, every number downstream is decoration.

A persona also learns inside a thread: ask about a term, get an answer, hold it
from the next turn on. That is what produces arcs where turn 1 asks what
`equity value` is and turn 2 asks what that means for `valuation`.

### Zero user turns is a normal outcome

It is what most briefings get. Nothing downstream may treat it as an error, a
failure, or a zero to divide by, and `personas._validate` **refuses a fixture
set in which it cannot happen** — the suite must always contain a persona
configured to stay silent, or that path breaks without anyone noticing until a
real user ignores a briefing.

`theo_silent` says nothing at all, which is what a disengaged reader actually
does, and it is the hardest input in the suite. The system is left holding a
briefing it wrote, terms it chose to use, and a reading measurement saying the
reader skimmed it. Every input to its band is the system's own output plus an
attention signal.

---

## Reading behaviour is attention, not knowledge

Each persona has a reading profile (`reading.py`) producing `dwell_ms` and
`scroll_fraction` for a briefing of a given length, fed through
`record_reading(..., source="simulated")`:

| profile | who | what it is for |
|---|---|---|
| `careful` | `pilar_wine` | reads the whole thing, sometimes twice. The control. |
| `skimmer` | `dana_startup`, `ade_premier_league` | scans most of it fast. The common case. |
| `bouncer` | `sam_beginner`, `theo_silent` | fine until the briefing gets long, then bails. |

The numbers are not invented. Silent reading of English prose runs about **238
words per minute** (Brysbaert 2019, meta-analysis of 190 studies), at 5.1
characters per word — so "did they spend as long on this as reading it would
take" is a computable question rather than a threshold someone picked. The
`bouncer` matters most: its output is a function of a length *the system chose*,
which is what lets read quality measure the briefing rather than the reader.

**`reading_regression` is a hard gate.** Attention signals keep trying to
become knowledge claims, and this project has measured three:

1. An earlier design promoted a term to an `assumed` state after three
   unquestioned exposures. Measured against persona ground truth it was
   anti-predictive at every bar tried (lift −0.34 at one exposure, −1.00 at
   three and five), so the state was deleted; `silence_regression` keeps it
   deleted.
2. A live `concept_evidence` extractor credited `theo_silent`, who knows
   nothing and says nothing, with understanding `yields` and `trading down`,
   off topically-adjacent paraphrase that never used either word. The prompt
   was fixed and the gate stays.
3. Reading behaviour. The most dangerous of the three, because it arrives as a
   number and a number looks like a measurement.

What is actually known about display metrics as comprehension proxies is not
ambiguous: Apple Mail Privacy Protection inflates roughly half of reported email
opens, the IAB had to invent "viewability" because "served" had stopped meaning
anything, and around 59% of shared links are never clicked by the sharer.
Time-on-page and scroll depth are in the same family.

Four independent checks, none sharing an implementation with what it audits:

* no concept's evidence string mentions dwell, scroll, or how attentively
  anything was read;
* no term reached a known state out of a thread with no user turns;
* `record_reading` is exercised against a live scope — five minutes and a full
  scroll, eight times — and the concept's state must be byte-identical
  afterwards;
* no reading field appears in any judgment context, scanned in `judgment_log`.

`verify_offline` scans for it a second time with independent SQL, for the field
names **and** for the band strings, because the shape this leak would actually
take is someone folding the numbers into a prose summary rather than passing a
field along.

### Read quality: two derivations, compared rather than assumed

The harness derives its own band from the dwell and scroll it simulated;
`store.read_quality` derives one from the same two numbers. They agree on
**31/34** in the current offline run, and all three disagreements are boundary
cases — e.g. 3,594 ms over 412 chars at 0.43 scroll, which the harness calls
`skipped` and the store calls `skimmed`. The store's is authoritative; the
harness's (`reading.reference_read_quality`) is a second opinion, and the value
of keeping it is that it is a second *implementation*.

---

## Engagement is reported as deviation, not as a rate

A raw follow-up rate, per persona or aggregated, says nothing about briefing
quality: it is a readout of each persona's configured ask rates, and it prints
the same number against briefings of any quality whatsoever. **A metric that
cannot move in response to the thing it claims to measure is not a metric.**

So every rate is printed next to what that persona's own configuration
predicts *for the briefings this run actually produced*, and the residual is the
column to read. The aggregate row prints deltas only; an aggregate raw rate
across five differently-configured personas is not printed, so it cannot be
misread as a finding.

The behaviour "if there is a word in front of them they do not know, they ask"
is a deliberate departure from §8.3's measured 13.8-per-100, and it lives in
configuration so that it can be netted out of the observation: two configured
rates, `ask_rate_default` and `ask_rate_unknown_present`, with the expected ask
rate for a run computed per briefing from whichever applies.

`react_rate`, `continue_rate` and `max_turns` make the expected number of user
turns per thread exactly computable (a truncated geometric), which is what
`turns_per_thread_delta` is measured against.

Reading folds in the same way. `read_score` is the mean band — 0 skipped, 1
skimmed, 2 read, 3 studied — and the expectation is what the same profile would
produce against briefings pitched at that persona's own comfortable length
(`patience_chars`). A system that writes long gets a negative residual, and that
residual is entirely the system's doing. The mean band is used rather than a
binary "read or better" because a skimmer cannot reach `read` at *any* length
(its dwell-to-expected ratio is a constant of its profile), so a binary measure
is structurally zero on both sides and its residual can never move.

`never_engages` is a **label, not a branch.** `theo_silent`'s three turn
rates are zero in the fixture, so its expected turn count comes from the same
arithmetic as everyone else's and its deviation is a real zero rather than a
special case that cannot deviate. `_validate` enforces that the flag and the
rates agree.

---

## `harness/contract.py`: the adapter over the system's thread API

The runner drives a round through three calls on the real `Assessor`
(`raise_topic`, `respond`, `close_thread`) and the store's `record_reading`.
`contract.SystemContract` is a thin adapter over those calls. Every call lands
in `src/`; the adapter holds no implementation of system behaviour. It adds
three things:

* it normalises what comes back into frozen, lower-cased projections, so the
  metrics compare terms without caring how the system cased or ordered them;
* it reads the stored reading columns back (`stored_reading`), so the band the
  store derived can be compared with the band the harness derives;
* it checks the turn count around `respond`.

**Who records the user's turn.** `UserScope.add_turn` is public, so a caller
could reasonably record the user's turn itself before handing the text to
`respond`. The Assessor records it itself, first, deliberately — *"if the
answering call fails, their question is still on the record and still reaches
the evidence extractor"* — so the harness does **not** call `add_turn` around
`respond`, and `contract.respond` verifies the turn count moved by exactly two
in the order `['user', 'system']`. Both sides recording it would double every
turn count in the report, silently; a mismatch is printed under "contract
notes".

---

## The five personas

Every event is a **real, verified August 2026 item** from
`personas/research/*.md`, with its real headline, detail and source URL, and the
significance rating that research assigned it.

| Persona | Domain | Archetype | Reading | What it is for |
|---|---|---|---|---|
| `dana_startup` | startup/VC | well-informed | skimmer | Well-informed user in a fast-moving domain. The control. |
| `pilar_wine` | wine | well-informed | careful | Well-informed user in a slow domain, and the **unanticipated-domain check** — nothing in this system was designed around wine. Also the one lowercase persona. |
| `ade_premier_league` | Premier League | partially informed, **noisy** | skimmer | The required noisy persona: sometimes silent about a term it lacks, sometimes asks about one it holds. A regression witness that silence promotes nothing. |
| `sam_beginner` | startup funding | barely informed | bouncer | **The primary case.** Asks the most, and bounces off long briefings. |
| `theo_silent` | wine | disengaged, **adversarial** | bouncer | Reads and says nothing, ever. The zero-turn case, and the instrument for both the silence and reading gates. |

### The noisy persona's behaviours are decided per term, not per turn

`silent_when_ignorant_rate` and `asks_about_known_rate` are drawn from a
term-keyed seed, so a persona is consistently sheepish about particular words
rather than flipping a coin each time it meets the same one. They are
decided **in the runner, from the briefing text, before any turn happens** —
because a persona that stayed silent about a term and then said nothing at all
has done exactly what the noise fixture exists to produce, and deriving the
behaviour from emitted turns would under-report it precisely when it matters.

---

## Reply style comes from the research, not from invention

Two studies: one for length, one for register.

`personas/research/reply_style_profile.md` measured 1286 Hacker News comments.
Its length finding stands — length is a **distribution** with real spread, and
low length variance is the likeliest single failure of the harness — and
`metrics.style_fidelity` gates on it. Its *register* is not used: HN
commenters perform opinions for an audience, and personas built on them say
things like *"yields must be down too if it's ripening that fast."* The human's
objection, verbatim: *"People use agents to get the information they want, they
don't offer their own opinions to an agent... people don't explain things about
the topic they're trying to learn to the agent."*

`personas/research/assistant_reply_style_profile.md` tested that on real
human→assistant turns (WildChat-1M; 8,000 conversations, 507 turns that follow
an informational reply to an information request, 80 hand-classified). The
claim holds: `assert-opinion` + `explain-to-assistant` are **3 of 80**, and 0 of
80 explain the subject. What people do instead is ask for more (`extend`, 58%
of engaged turns), check a belief as a question (14%), or clarify a term (6%)
— and **45% of informational messages get no reply at all**. The fixtures
encode exactly that: turn rates around 0.25–0.5; an act mix
of clarify / extend / check-belief / acknowledge / remark; questions median 9
words, a third carrying one clause of purpose or self-disclosure and never a
fact; lowercase as a fixed per-persona trait (37.5% of measured turns);
terminal punctuation dropped about half the time. Every phrase bank in
`responders.py` cites the measured example it is modelled on.

### Deviations, stated rather than buried

1. **§8.2's hard rule is not applicable to the short archetypes.** It demands
   ≥15% of replies over 35 words for *every* persona, but the same table gives
   `barely_informed` a p90 of 28 and `disengaged` a p90 of 14. Those cannot both
   hold. The long-tail half is enforced only where §8.2's own p90 clears 35; the
   report prints `n/a` for the others rather than silently passing them.
2. **The unknown-term ask rate is a documented departure**, held in
   configuration — see the engagement section above.
3. **Observed length undershoots configured length**: under the assistant
   register almost every turn is a question, and questions
   that engage with an informational message are median 9 words. That is a
   property of the register rather than a miscalibrated fixture, which is one
   more reason the gate stays on the *configured* distribution and the
   observed column stays reported-only.

---

## What the report measures

**Ledger precision and recall**, per persona, per interaction, with
interactions-to-threshold. Recall is scored over the terms the run actually
produced **evidence** about — used correctly, asked about, or revealed as
misunderstood. Neither exposure nor reading is on that list. Scoring recall over
everything the briefings merely *mentioned* would be wrong: the
Assessor deliberately does not promote a term on the strength of having defined
it unprompted, so that denominator marks the system wrong for declining to
guess. `elicitation` reports how much of the concept set the run got evidence
about at all, so a strong recall over three terms is not mistaken for a strong
result.

**Per-state accuracy**, over `provisional` / `explained` / `confirmed`, and
**`provisional_signal`** — `P(known | provisional)` against `P(known | left at
unknown)`, the counterfactual, since deleting the state puts every one of those
terms there instead. It is the test that showed exposure-based promotion to be
anti-predictive, pointed at `provisional`.

**The always-on exposure table**, `P(known | exposed >= N)` for N = 1..5 against
the base rate, computed from the run's own exposure counts. Exposure promotes
nothing; the table is kept so that introducing a silence-based rule requires
arguing with a number.

**Six hard gates, both modes** (the ledger thresholds and the plumbing checks
below also decide `RESULT`):

| gate | what it holds |
|---|---|
| `silence_regression` | silence promotes nothing: no term reaches a known state on exposure alone, and the deleted `assumed` state stays deleted. Three checks, including `note_exposure` exercised against a live scope. |
| `reading_regression` | attention moves no concept. Four checks. See above. |
| `no_questions_to_user` | the system asks the user nothing. Detection is narrow — a question mark in a sentence addressed to the reader — because a briefing may legitimately quote a question, and a gate that fired on that would be turned off within a week. |
| `question_faithfulness` | what a persona asks follows from its concept sets. The harness auditing itself. |
| `band_containment` | `theo_silent` never leaves `beginner`. |
| `cold_start` | the beginner's unknown terms actually get glossed. |

**Live only:** materiality precision/recall/F1 by significance band, and
evidence-extraction accuracy, including `presupposition_confusion`,
described below.

**Plumbing**: cross-user isolation through `UserScope` only, ground-truth
leakage, reading-behaviour leakage, unexpected judgment calls, per-judgment-point
cost and latency.

### `theo_silent` is reported but not gated on the ledger

`theo_silent`'s ledger numbers and its cold-start row are printed in full and
gate nothing. It supplies no evidence at all, so there is nothing for the ledger
to be right or wrong *about*. The properties it *does* gate are band
containment, the silence regression and the reading regression. Counting the
same finding three times would make the build permanently red for something
already under measurement.

---

## Offline mode refuses to print judgment-quality numbers

Structurally, not as a caption. `compute()` only ever *calls*
`_judgment_quality()` inside `if live:`; offline the field holds a sentinel
whose every attribute access raises `OfflineMetricError`. There is no branch in
which an offline report holds a judgment-quality float.

Refused offline: materiality accuracy (the stub is handed `should_be_material`)
and evidence-extraction accuracy (scoring the stub's reading against the
persona's intent is scoring the answer key against itself). Serialisation drops
those answer-key columns too, so the artifact cannot be used to recompute in a
spreadsheet what the report declined to state.

`held_at_the_time` / `lacked_at_the_time` — the persona's live concept sets, per
turn — are the strongest answer key in the run and are **never** written to the
artifact, not even live. `question_faithfulness` consumes them in memory and
reports counts.

Measured in **both** modes and honest in both: ledger agreement, per-state
accuracy, cold start, band containment, isolation, reproducibility, and all six
hard gates. The offline extractor works from the user's turns and never sees
`knows`/`does_not_know`, so the ledger genuinely can be — and is — wrong about a
persona.

---

## Verification

`python -m harness.verify_offline` checks properties that would otherwise be
claims made on trust. Current output:

```
  ok   1. zero network: a full offline run completed with sockets blocked
  ok   2. reproducibility: two runs byte-identical over 286,219 chars of canonical body (run ids run_3f90b3656a38 vs run_e846b2ad7f10 differ)
  ok   2b. the reproducibility check is not vacuous: dropping one concept from 'dana_startup''s ground truth changed the canonical body
  ok   3. no ground truth in judgment_log: 131 input_json rows scanned by independent SQL for 7 key names plus each persona's verbatim does_not_know list
  ok   3b. no reading behaviour in judgment_log: 131 input_json rows scanned by independent SQL for 8 field names plus the read-quality band strings. Attention may be stored; it may not be shown to a call that decides what somebody knows.
  ok   3c. no probe string in the system's store: every table except eval_runs scanned for 12 pseudo-term(s) (bridge multiple, cane weighting, carry basis, cru drift...) and the word 'rung'; 77 probe items were recorded, so the scan had something to find
  ok   4. offline refusal is structural: reading a judgment-quality metric raises OfflineMetricError
  ok   4b. the offline artifact omits the answer-key columns (should_be_material, intent_*), so the refused numbers cannot be recomputed from it
  ok   5. question_faithfulness: clean
  ok   5. quantity_phrasing: clean
  ok   5. reading_regression: clean
  ok   5. silence_regression: clean
RESULT: PASS
```

(The run ids and the character count vary from checkout to checkout; the
verdicts should not.)

Check 2b matters: a canonicaliser that stripped too much would pass every diff
including the ones that count, so the check is required to *fail* on a real
change before its passing is worth anything. Check 3 uses independent SQL rather
than reusing `metrics.check_answer_key_leakage` — a leak check sharing an
implementation with the thing it checks can agree with itself while both are
wrong. 3b is separate from 3 on purpose: `dwell_ms` is not ground truth, and the
system is entitled to store it. It is a leak because of *where* it would be.

Artifacts are diffable: `--compare A B` ignores the `volatile` subtree (ids,
latency, tokens) and diffs the rest.

---

## Presupposition: a question is not always a gap

Most of what a user writes in a thread *is* a question, and **a question
presupposes terms as often as it asks about them**, so the rule "a term inside
a question is `asked_about`" is wrong:

> *"Is the enterprise value number the same story as the ARR one, or different?"*

That asks about neither term. It demonstrates both. Read as a gap, it lands
`enterprise value` and `arr` in `explained`, and the system will re-explain them
to somebody who has just proved they do not need it. The offline extractor
tells the two apart by shape (`stub_judgments.extract_concept_evidence`), and
`presupposition_confusion` in the live evidence block
measures it: of terms the persona held and leaned on, how many came back as gaps
to be closed. It is refused offline, because the persona's intent is the answer
key.

---

## Files

| file | what it is |
|---|---|
| `run.py` | CLI. Main flags: `--live`, `--persona`, `--rounds`, `--parallel`, `--compare`, `--show-thread`; see `python -m harness.run --help` for the rest |
| `runner.py` | The replay loop: brief → read → thread → close → probe |
| `contract.py` | Thin adapter over the Assessor's thread API; normalises results and checks the turn count around `respond` |
| `config.py` | Harness-side model routing: which model voices the persona and which grades the probe |
| `reading.py` | Reading profiles and the reference `read_quality` derivation |
| `responders.py` | `ScriptedResponder`, `LLMResponder`, the question forms, and the §8.2 length distribution |
| `learning.py` / `probe.py` | The persona memory model and the post-session probe; see `LEARNING.md` |
| `stub_judgments.py` | Deterministic offline stub for every routed call |
| `metrics.py` | Scoring, the hard gates, and the structural offline/live split |
| `plainness.py` | Deterministic plainness measures of a briefing (grade, term density, glosses) |
| `feed.py` / `clock.py` | The event-feed and clock substitutions |
| `materiality_cache.py` | Optional replay cache for materiality verdicts (`--materiality-cache`) |
| `interrupt_cases.py` | Interrupt-timing multi-group cases |
| `live_search_eval.py` | Query formulation and source selection against the real web, which persona runs never reach by design |
| `verify_offline.py` | The checks above |
| `gate.py` / `propose.py` | The regression gate over a live-run artifact, and the prompt proposer; see below |
| `persona_gen.py` | Generator for the larger fixture set in `personas/generated/` (live calls; not needed to run the suite) |
| `personas/` | The five hand-built fixtures (`NN_*.json`) and the loader/validator (`__init__.py`) |
| `personas/generated/` | Generated fixtures and their `REVIEW.md`; run with `--fixture-dir harness/personas/generated` |
| `personas/research/` | Research notes behind the fixtures (event files, the two reply-style studies, the learning-model and knowledge-inference reviews) and the `_*.py` scripts that built the style profiles; see its `README.md` |

## Fixture invariants

`personas/__init__.py` refuses a fixture that would produce a plausible number
rather than an exception — that being the only kind of fixture bug that costs
anything:

* a term in both `knows` and `does_not_know`;
* a scored term missing from `group.vocabulary` (unreachable, deflates recall);
* a glossary term in **neither** concept set — worse, because it is silent;
* a scored term that appears in no event's `concepts`;
* a duplicate group name or headline (both key the side channel);
* an event with no real source URL;
* a turn rate outside [0, 1], or an unknown reading profile;
* a persona declaring `never_engages` whose turn rates are not all zero — its
  silence has to be in its configuration, or its deviation from configuration is
  a special case pretending to be a measurement;
* **a suite in which no persona can produce a thread with zero user turns.**

---

## The regression gate and the prompt proposer

Two tools that sit downstream of a live run. Neither edits `src/`, the prompts,
or the harness; the proposer's `--apply` is the single, explicit exception.

The commands below read `eval-runs/live/all5c.json`, the artifact of a local
live run. `eval-runs/` is gitignored and not checked in; substitute the
artifact of your own `harness.run --live --json PATH` run.

```bash
PYTHONPATH=src:. .venv/bin/python -m harness.gate eval-runs/live/all5c.json                                   # absolute thresholds only
PYTHONPATH=src:. .venv/bin/python -m harness.gate eval-runs/live/all5c.json --baseline eval-runs/live/all5.json --json gate.json
PYTHONPATH=src:. .venv/bin/python -m harness.propose eval-runs/live/all5c.json --db eval-runs/live/all5c.db --max 3
PYTHONPATH=src:. .venv/bin/python -m harness.propose --apply proposals/<run_id>/proposal-02.patch          # the only writing path
```

### `harness/gate.py`

Reads the artifact `harness.run` wrote and the run's SQLite store (opened
read-only; default is the artifact's sibling `.db`) and decides in three tiers.
Exit 0 is PASS; anything else is 1 -- including INCOMPLETE, which is what a
Tier 1 check that could not run because no database was supplied reports. An
unevaluated hard gate is not a pass.

**Tier 1 -- hard, binary.** `no_questions_to_user`, `reading_regression` and
`silence_regression` are reused from the artifact rather than recomputed, so
the gate cannot disagree with `metrics.py` about them. Computed from the store:
`never_repeat` (every persona exchange has an `event_id`, no event twice per
user, no two briefings for one user with token Jaccard > 0.6 -- both printed
when it fires); `no_fabricated_figure` (every currency-, scale- or unit-bearing
number in a `thread_reply` answer must appear, after normalisation to
significant digits, somewhere in that call's own `input_json`; the rule and its
deliberate forgiveness are documented at the top of the file, and every miss is
printed with the reply excerpt); `zero_judgment_errors` (rows with `error` for
this run, the harness's own probe users excluded); `cost_within_budget`
(tokens × per-model price, default ceiling `--max-cost 5.00`). Prices come
from `conversational_agent.config.PRICES_PER_MTOK`, which
`gate.PRICES_PER_MTOK` extends with the undated Haiku alias and the zero-cost
`cache` label; `judgment_log` stores no cache-read/creation tokens, so the
figure is the uncached price of what was logged.

**Tier 2 -- scored, paired per persona against `--baseline`.** Materiality F1
≥ 0.90 (pooled); ledger precision and recall per persona may not drop more than
0.15 against that persona's baseline value; `unknown` precision ≥ 0.90;
`confirmed` precision ≥ 0.65; and the **re-ask rate** -- the share of
briefings whose thread asked about a term that briefing's `explained_terms`
contains, "asked about" meaning the canonical term appears in the exchange's
`asked_about` (exact after `normalize_term`, or content-token containment, the
store's own merge rule) -- at most 0.09 and not up more than 0.06 on the
baseline. Without a baseline only the absolute thresholds apply. The
disengaged persona's ledger numbers are reported and not gated, as in the
artifact. A Tier 2 failure within 0.05 of its band prints `MARG` and the
rerun-advice line says *marginal -- rerun once before deciding*; the exit code
is still 1.

**Tier 3 -- reported, never gated.** Engagement deviation, `familiar` /
`explained` precision split by `read_explanations` (< 2 / ≥ 2), `provisional`
precision, per-persona turn counts, cost and latency by judgment point, a
briefing-form line (word count, sentence count, inline definitions, and a
crude check for briefings that touch more than one of the user's events),
reply hygiene (replies that carried a literal `\u` escape to the user -- a
real defect that also confuses any figure check run on raw text), and the
harness's own `question_faithfulness` self-audit.

`--json out.json` writes the machine-readable report the proposer consumes.
`tests/test_gate.py` covers the never-repeat check, the figure normaliser and
the re-ask metric against small synthetic stores.

### `harness/propose.py`

Runs the gate, then gathers evidence **deterministically, without a model**:
for each Tier 1 failure, Tier 2 drop and Tier 3 anomaly (re-ask events;
`familiar`/`explained` precision below 0.15 at fewer than two reads with
n ≥ 10; a persona with recall below 0.7; a briefing over 220 words or with more
than four inline definitions; a reply carrying a literal escape) it pulls the
briefing, the turns, the matching `judgment_log` rows (prompt version, model,
reasoning, verdict) and the exchange's `explained_terms` / `read_quality` --
capped at 40 items, Tier 1 first. Judgment rows are matched to exchanges by
briefing text, since the log carries no exchange id.

The constraint set is parsed from `docs/checkpoints.md` (`[x]` items), every
`**Changed**` block in `docs/prompt-changelog.md`, the spec's non-goals section,
and seven hard-coded product rules (brief-then-stop; never ask; ledger not
score; no silence promotion; reading only through `familiar`; never repeat;
gloss once). A proposal that touches one of these must argue why it is not a
reversal or it is dropped.

Then **one** call to `claude-opus-5` through the project's `AnthropicClient`
with a structured-output schema, logged like every other judgment call
(`judgment_point = propose`, `run_id = propose_<timestamp>`) into
`proposals/propose.db` -- its own store, so the run's database stays an
untouched artifact. The model sees the gate report, the evidence, the
constraints, the current text of every prompt with its version header, and the
tunable constants (`READ_EXPLANATIONS_BEFORE_BAND`,
`CORRECT_USES_BEFORE_CONFIRMED`, `MAX_SEARCH_USES`, `LEDGER_*`). It returns at
most `--max` proposals, each with kind, target, diagnosis, evidence ids, the
exact old/new text (or constant and value), expected effect, risk,
constraints touched and confidence. Proposals citing no supplied evidence are
rejected.

Output goes to `proposals/<run_id>/`: `proposal-NN.md` with the evidence
quoted verbatim underneath, `proposal-NN.patch` (a unified diff generated by
applying old→new to a copy of the target, version header bumped for prompts,
validated with `patch --dry-run`), `proposal-NN.changelog.md` (a ready-to-paste
entry in the changelog's what-failed → what-changed → what-it-cost format),
and `index.md`. See `proposals/README.md`.

`--apply PATCH` applies exactly one patch after a dry run, inserts the sibling
changelog entry at the top of `docs/prompt-changelog.md`, prints what changed, and
tells the operator to run a fresh live run through the gate. `--no-llm`
gathers evidence and constraints without a model call.

---

## Learning trend, horizon mode and the cheap regression

Four parts of the learning side of the harness. All of it is harness-only:
the one thing it uses from `src/` is the `config.HAIKU` model id, which
`--cheap` voices the persona with. The memory model and probe themselves are
described in `LEARNING.md`.

### Learning-trend metrics (`metrics.PersonaLearning`)

Per persona, alongside the probe numbers:

| field | what it is |
|---|---|
| `known_slope` | least-squares slope of \|{terms with P(can define) ≥ 0.5}\| against session ordinal. A session is a round that ran a thread or a probe-only day; a quiet round does not stretch the axis. The count comes from `rounds[].known_count` (the persona's memory, never the ledger). |
| `retained_slope` | slope of the mean retained rung per session against the same ordinal, over sessions that probed a retained item. |
| `teaching_efficiency` | terms **taught** — first probe graded rung ≥ 2, not known at t = 0 (`knows` or seeded by subdomain weight), attributed to the briefing that *first glossed* them (`explained_terms`), and only when that briefing was read or skimmed — per briefing read or skimmed, and per 100 words of such briefings. A term first glossed in a skipped briefing, or learned only by asking, is not credited: the denominator is briefings looked at, so only what those glossed can be in the numerator. |
| `retained_by_interval` | four buckets: `<=1d`, `2-7d`, `>7d` (7–30 days, label kept for continuity) and `>30d`. Edges are `probe.RETENTION_BUCKET_EDGES`. |

Slopes need at least `MIN_SLOPE_POINTS = 3` points, otherwise `None`; two
points always fit a line. The run-level **learning summary table**
(persona | K | m | known start→end | known_slope | retained_slope |
efficiency terms/briefing, terms/100w | calibration gap | confab) is printed
in the Learning section and stored under `learning_summary` (`rows`,
`columns`, the rendered `table`, and one-line `lines`).
`python -m harness.run --learning-summary ART [ART...]` renders it from
existing artifacts; artifacts written before `rounds[].known_count` existed
show `known_slope` as n/a, since nothing in them recovers the per-session
known set.

### The artifact header: `run_settings`

Non-volatile, written on every run: `fixture_dir` (project-relative),
`persona_ids`, `rounds_cap` (`--rounds`), `horizon_days`,
`retention_probes`, `harness_routing` (persona model, grader model, `cheap`,
`remediation_search`), `ledger_truth` and whether a materiality cache was in
use. `--parallel` is recorded under `volatile` instead, so the artifact stays
byte-identical across worker counts. The report opens with a
`persona voice:` line from the header.

### Paired comparison in the gate

`harness.gate --baseline` **refuses** a baseline that was not produced
under the same fixture set, persona ids, rounds (per persona, probe-only days
excluded), `--rounds` cap, horizon settings and harness routing. Refusal is a
Tier 1 `paired_comparison` line with the mismatches listed, status
`INCOMPLETE`, exit 1, and Tier 2 then runs without a baseline. An artifact
without the header is read as what every such run was — checked-in
fixtures, default clock, Sonnet voice and grader, remediation search on — so
two legacy artifacts pair with each other and with a full Sonnet run, and
never with a cheap one.

Tier 2 has three paired learning checks per persona, rebuilt from both artifacts
with `metrics.learning_report_from_artifact` so an older baseline still
pairs: `learning_known_slope` (fail if it drops by more than 0.15
terms/session), `learning_efficiency` (terms per briefing; fail if it drops
more than 20 % relative; a zero baseline is not gated) and
`learning_retained_2_7d` (mean retained rung in the 2–7 day bucket, gated
only with n ≥ 3 on both sides; fail if it drops more than 0.4). Tier 3 prints
`learning vs baseline` — every one of those plus retained slope, calibration
gap and confabulation rate, candidate / baseline — whether gated or not,
and the candidate's learning summary table.

### Horizon mode

```bash
PYTHONPATH=src:. .venv/bin/python -m harness.run --horizon-days 30 --retention-probes 3
```

`--horizon-days N` spreads the same events over N simulated days: the clock
advances `N / (rounds + retention probes)` days between rounds instead of the
group's poll interval. `--retention-probes K` (horizon mode only) appends K
probe-only days after the last briefing round — no event, no monitor, no
briefing, no thread; the probe asks about earlier terms with `glosses` taken
from everything the system said during the run — spaced so the last one
lands on day N. They are recorded as rounds with `probe_only: true`,
`thread_ran: false`, `quiet: false`, and count as neither a briefing nor a
quiet round anywhere (`thread_engagement`, the gate's per-persona table,
`rounds_run` in the pairing signature). With both flags absent the clock
advances by the group's poll interval between rounds and no probe-only days
are added.

### Cheap regression mode

```bash
PYTHONPATH=src:. .venv/bin/python -m harness.run --live --cheap --rounds 4 \
    --persona sam_beginner --materiality-cache eval-runs/cache/materiality.json
```

`harness/config.py` routes the three harness-side calls that never go
through `Judge`: the live persona's replies (`LLMResponder`), the probe's
persona-answer call, and the probe's grader. Environment overrides are
`HARNESS_PERSONA_MODEL` and `HARNESS_GRADER_MODEL`; defaults are Sonnet for
both. `--cheap` voices the persona with Haiku (`config.HAIKU`) and drops the
remediation search; **the grader stays on Sonnet** — it is the reward signal
and must not get noisier. `--persona-model M` / `--grader-model M` are the
explicit overrides (precedence: flag > `--cheap` > env > default), so a
same-seed A/B of a Sonnet-voiced against a Haiku-voiced persona is one
command per side, the two artifacts carry different `persona voice` lines,
and the gate refuses to pair them.

`--materiality-cache PATH` (`harness/materiality_cache.py`) wraps the
harness's `RecordingClient`: a `materiality` call whose
`sha256(headline, detail, group description)` is in the JSON file is
answered from it; misses go through and are written back; the hit rate is
printed and stored under `volatile.materiality_cache`. `Judge` still logs a
row for a replayed verdict, and after each monitor stage the runner stamps
those rows `model = "cache"` with zero tokens (`mark_logged`), so tracing
stays complete and the gate's cost table prices them at nothing. Entries
carry the materiality prompt version and are misses after it changes; the
key ignores `recent_events`, which is exact for the fixture suites (every
event is the first of its kind in its group) and an approximation elsewhere.

Token usage of the harness-side calls is accumulated
(`LLMResponder.usage`, `ProbeRunner.usage`) and written to
`volatile.harness_usage`, so a live run shows what the voice and the grader
cost.

## Plainness: how briefing depth is read

**Human direction, verbatim:** *"it shouldn't have too many literal definitions
it should just talk in simpler language and explain topics more which can
occasionally include a definition."*

From `briefing` prompt v7 on, inline definitions should be low **everywhere**,
so a count of them cannot tell a beginner briefing from an expert one; depth is
read from how plainly the text is written. (The prompt history is in
`docs/prompt-changelog.md`.) `harness/plainness.py` is that
measure -- deterministic, dependency-free, one function:

```python
plainness(text, vocabulary, explained_terms) -> {
    words, sentences, mean_sentence_length, syllables_per_word,
    fk_grade,                # Flesch-Kincaid grade: 0.39*(w/s) + 11.8*(syl/w) - 15.59
    domain_term_hits, domain_term_density,   # group-vocabulary terms per 100 words
    definitions, definitions_per_100w,       # len(explained_terms): "terms made clear" (see below)
    gloss_cues,                              # "X, meaning Y" / "X -- meaning Y" / "X, that is, Y" / "X, i.e. Y"
    parenthetical_asides, asides_per_100w,   # THE GLOSS MEASURE: "(...)", " -- ... -- " pairs and the cues
}
```

Under v7 `explained_terms` means "terms a reader who lacked them would now
have from the text", whether by a formal gloss or by plain explanation -- a
69-word beginner briefing that says "paid with its own shares rather than
cash" lists two explained terms and no gloss. So `definitions_per_100w` does
not measure glossing; it is reported as **made clear/100w** and kept in
the bundle for the ledger's sake. Glossing is read from `asides_per_100w`:
parenthetical insertions, paired dashes and the formal-gloss cue phrases
(`, meaning`, `-- meaning`, `, that is,`, `, i.e.`, `, which means`, `, in
other words,`), a cue that opens with a dash counted once.

The syllable counter is the vowel-group heuristic (runs of `aeiouy`, minus a
trailing silent `e` except after a consonant + `l`, floor 1). It is wrong on
some words in both directions and is used only to *compare* briefings with
each other; the same error applies to every briefing it runs on. Vocabulary
terms are matched case-insensitively on word boundaries with a plural allowed,
longest term first, each character span counted once. `vocabulary` is the
fixture's glossary (`group.vocabulary`), not the reader's knowledge.

**Where it is read:**

* **Report, Subdomains (c) depth fit** (`metrics.subdomain_report`): the same
  held / middle / not-held bucket by the persona's true subdomain weight, and
  each bucket carries mean grade, words per sentence, term density, asides
  and definitions per 100 words. Two verdicts, both reported and neither
  gated: `plainer for beginners: YES/NO` (grade AND density lower in not-held
  than in held) and `definitions occasional: YES/NO` (glosses --
  `asides_per_100w` -- <= 1.0 in every occupied bucket). Raw per-briefing rows
  are kept under `depth_fit.rows`.
* **Artifact**: every thread carries its bundle under `plainness`, so the
  gate summarises it without re-reading briefings. Nothing about the persona
  enters it; it is a function of the briefing text and `explained_terms`,
  which were already in the artifact.
* **Gate, Tier 3 briefing form**: p50/p90 of the bundle split by the reader's
  band *at the time of the briefing* (`proficiency_before`), with the
  baseline's rows printed underneath when one is paired. For artifacts written
  before the bundle existed the gate recomputes it from the briefing text
  (against the fixture set the artifact's `run_settings.fixture_dir` names,
  else the checked-in one; a persona not in the fixtures falls back to its own
  `terms_used`) and says how many it recomputed.
* **Gate, Tier 2 `definitions_ceiling`**: the gloss-stacking regression v7
  guards against. A briefing is over the ceiling at more than **2.0**
  glosses (`asides_per_100w`) per 100 words; the check fails when more than **20%** of
  briefings are over it, or when that share rose more than **0.10** on the
  baseline (marginal within 0.05, as for the other Tier 2 checks). Every
  offending briefing is listed.
* **`training/generate_sft.py`**: each row's `quality.plainness` carries the
  bundle and the run summary prints grade / density / definitions by profile
  (beginner ... expert), so a regeneration under v7 shows at a glance whether
  the teacher writes plainer for beginners rather than defining more.

**The v6 baseline** (`eval-runs/live/all5c.json`, a local live run that is not
checked in: 34 live briefings written under `briefing` v4-v6, all recomputed),
p50/p90:

```
all          n=34  grade 14.7/16.9; w/sent 31.5/38.3; terms/100w 1.2/2.9; glosses/100w 0.9/2.1; made clear/100w 1.5/2.6
beginner     n=28  grade 14.7/16.1; w/sent 31.8/38.0; terms/100w 1.1/2.6; glosses/100w 1.1/2.0; made clear/100w 1.6/2.6
developing   n=6   grade 12.9/14.8; w/sent 31.5/32.5; terms/100w 2.1/2.3; glosses/100w 0.7/0.8; made clear/100w 0.0/0.8
```

That is the v6 shape: the band gradient was carried by what was explained
(made clear 1.6 vs 0.0 per 100 words) and by glossing (1.1 vs 0.7), not by
plainness -- beginner briefings were no plainer than developing ones (grade
14.7 vs 12.9, the same sentence length). 4 of 34 (0.118) sat over the 2.0
glosses/100w ceiling, so `definitions_ceiling` passes on it with room to
spare. The v7 regeneration is compared against these numbers; the expected
movement is grade and sentence length down for beginners, glosses staying
around or below 1/100w in every band, and "made clear" free to stay where it
is or rise, since plain explanation now counts toward it.
