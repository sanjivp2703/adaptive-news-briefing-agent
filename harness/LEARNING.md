# The persona learning model and the post-session probe

`harness/learning.py` gives every persona a memory: what it can say about each
briefing term, how that grows with reading, asking and being tested, and how it
fades with time. `harness/probe.py` tests that memory after every session with
two to four items graded on a four-rung ladder. Together they are both the
*driver of persona behaviour* and the *ground truth the ledger is scored
against*; the fixture's `knows` / `does_not_know` lists only seed the memory at
t = 0.

The design is `personas/research/learning_model.md` §0. This document says how
it was implemented, what was deliberately left out, how to read the report, and
where the constants most likely to need tuning live.

## What is modelled

Two traits per persona, from the fixture's required `learning` block (frozen
contract, shared with the persona generator; unknown keys are refused):

```json
"learning": {"schema": "v1", "prior_knowledge": 0.0-1.0, "memory_rate": 0.7-1.4}
```

| trait | what it does |
|---|---|
| `prior_knowledge` K | `knows` is the dominant effect (the terms seeded as strong traces); K additionally multiplies P(encode) on a first glossed encounter by `1 + 0.4·(K − 0.5)`, i.e. 0.8–1.2, capped at 0.9. It never compounds. |
| `memory_rate` m | multiplies stability at read time: `R(t, S·m)`. 1.0 is the population default; 0.7 forgets fastest, 1.4 slowest. Age and verbal ability are expressed through this one number. |

Five numbers per `(persona, term)` (`TermTrace`): stability `S` (days; the time
for retrievability to fall to 0.90), difficulty `D` (1–10; 5 by default, 6 for
a term the fixture lists under `quantities`), `n_ctx` (distinct contexts the
term was met or used in), `n_ret` (successful retrievals), `t_last`. A term with
no entry has no trace. Each term in `knows` starts at `S = 365`, `n_ctx = 3`.

Retrievability is the FSRS-4.5 power law, `R = (1 + 19/81 · t/S)^−0.5`. The
knowledge ladder is `P(recognises) = R`,
`P(can define) = R · (0.65 + 0.35·min(1, (n_ret + n_seed)/3))`,
`P(can use in a new context) = P(can define) · (0.5 + 0.25·min(2, n_ctx − 1))`.

The define rung **consolidates**. The 0.65 recall/recognition ratio is a fact
about newly learned words; applied to an expert's working vocabulary it gave
Pilar (live read-through `eval-runs/live/probe4_pilar.json`) a 35 % chance of
failing to define `potential alcohol` twelve hours after using it fluently
three times. Each successful retrieval or correct use (`n_ret`) closes a third
of the gap to 1.0, and a term seeded as known carries `n_seed = 3` from the
start, so a seeded expert term sits at ≈1.0 (test: p_define ≥ 0.95 at 12 h)
and a term retrieved three times reaches it; two retrievals give 0.88.
Recognition still decays with time; what stops decaying is the ability to
define a word you still recognise and have used repeatedly. Constants and
justification are in one block in `learning.py` (`RECALL_GIVEN_RECOGNITION`,
`DEFINE_CONSOLIDATION_*`, `KNOWN_SEED_STRENGTH`).

### The seven events

Seven events (`MemoryModel`), with the gain kernel
`g = e^1.49 · (11 − D) · S^−0.14 · (e^0.94(1−R) − 1)` and `S' = S·(1 + k·g)`:

| # | event | k | also |
|---|---|---|---|
| 1 | `first_gloss(term, read_quality, at)` | — | draw P(encode) from the table below; set S0 |
| 2 | `reread(term, read_quality, at, new_context)` | 0.55 | `n_ctx += 1` if new context; `skipped` is no event |
| 3 | `asked_and_answered(term, at)` | 0.75 | `n_ctx += 1`, `D −= 1`; floored at the `studied` S0 |
| 4 | `retrieval_success(term, at, new_context, feedback)` | 1.0 | `n_ret += 1`; with feedback an extra restudy (0.55) at the same pre-event R |
| 5 | `retrieval_failure(term, at, feedback)` | — | FSRS lapse rule, capped at the prior S; then a restudy if feedback |
| 6 | time | — | nothing stored; `R(t, S·m)` on demand |
| 7 | context | — | `n_ctx` gates rung 3; S unchanged |

Encoding table (rule 1), `read quality → (P(encode), S0 days)`:
`skipped (0, —)`, `skimmed (0.25, 0.15)`, `read (0.50, 0.40)`, `studied (0.70, 0.80)`.

Time is always the harness `SimClock`; every draw comes from a `Random` seeded
from the persona seed, so two runs are byte-identical.

### Constants and their citations

All constants are in one labelled table at the top of `harness/learning.py`,
each with the citation the review gave next to it. In brief:

| constant | value | source (learning_model.md) |
|---|---|---|
| retrievability form | `(1 + 19/81·t/S)^−0.5` | §0.2; Murre & Dros 2015; FSRS benchmarks |
| P(define \| recognise) | 0.65 | §0.2; Webb, Uchihara & Yanagisawa 2023; Pellicer-Sánchez & Schmitt 2010 |
| use factor | `0.5 + 0.25·min(2, n_ctx−1)` | §0.2, §3.4; Pagán & Nation 2019; Norman et al. 2022; Bolger et al. 2008 |
| gain kernel weights | `e^1.49, 11−D, S^−0.14, 0.94` | §0.3; FSRS-4 defaults |
| k restudy / ask / retrieval | 0.55 / 0.75 / 1.0 | §0.3, §2.3; Roediger & Karpicke 2006 (1.8 ratio); Bertsch et al. 2007 (d ≈ 0.40); FSRS fit |
| feedback doubles testing effect | extra 0.55 restudy | §0.3 rule 4; Rowland 2014 (g 0.73 vs 0.39) |
| lapse rule | `2.18·D^−0.05·((S+1)^0.34−1)·e^1.26(1−R)` | §0.3 rule 5; FSRS |
| encoding table | above | §0.3 rule 1; Swanborn & de Glopper 1999; Nagy et al. 1985/87; Yanagisawa, Webb & Uchihara 2020 |
| K multiplier | `1 + 0.4(K−0.5)`, cap 0.9 | §0.1; Simonsmeier et al. 2022 |
| m range | 0.7–1.4 | §0.1; Sense & van Rijn 2018 |
| known seed | S = 365 d, n_ctx = 3 | brief |
| D default / quantity | 5 / 6 | §0.2 ("numeric terms are harder") |
| thresholds | askable p_define < 0.5; presupposable p_use ≥ 0.5; known p_define ≥ 0.5 | brief |

## Subdomains: prior knowledge as a shape, not a list

Human direction: "the glossary shouldn't be an exact word to word matching
they should cover the subjects subdomains within the group that the user is
familiar with and use that among other factors to decide how in depth the
harness needs to explain a certain topic for the user to understand."

Each fixture's `group` carries `subdomains` — the vocabulary partitioned
into 3–6 short lowercase noun phrases (`viticulture & harvest`, `appellation
rules`, …), every vocabulary term in exactly one, optionally plus *anchor*
terms outside the vocabulary that give a runtime term something to match — and
each persona carries `familiar_subdomains`: `{name: weight 0.0–1.0}`, how at
home they are in each. Both are ground truth: never in `system_visible_group`,
never in a judgment context (`familiar_subdomains` is in `ANSWER_KEY_FIELDS`
and the `verify_offline` scan). `knows` / `does_not_know` stay the *labelled*
subset the ledger is scored on and must agree with the weights: a `knows` term
in a subdomain weighted ≤ 0.2, a `does_not_know` term in one weighted ≥ 0.8, a
vocabulary term in no subdomain or in two, a weight for a subdomain that does
not exist, or `prior_knowledge` more than 0.15 from the vocabulary-weighted
mean of the weights are all `FixtureError`s. A fixture that declares neither
block has no subdomain effects: the labelled lists alone decide what is
seeded.

What the weights do:

1. **Seeding (`MemoryModel.seeded`).** Every vocabulary term in subdomain *d*
   with weight *w* is seeded as known (`S = 365`, `n_ctx = 3`, `n_seed = 3`)
   with probability *w*, drawn from its own `Random(seed:persona:seed-subdomains)`
   in vocabulary order so a change to one weight moves only that term. `knows`
   is always seeded, `does_not_know` never. The terms seeded by weight are
   reported (`seeded by weight (beyond knows)`) and count as known at *t* = 0
   under both answer keys.
2. **In-domain encoding.** A first glossed encounter has P(encode) multiplied
   by `(0.7 + 0.6·w)` for the term's subdomain, after the K multiplier and
   before the 0.9 cap — 0.7 in a subdomain the reader knows nothing of, 1.3 in
   one they live in. This is the "prior knowledge helps new learning
   *in-domain*" finding (Witherby & Carpenter 2022; Hambrick 2003), kept
   modest and never compounding. The brief specifies it for terms outside the
   vocabulary; it is applied to every first gloss, because an in-vocabulary
   `does_not_know` term has a subdomain too and there is no reason the effect
   should stop at the fixture author's list.
3. **Placing a term the fixture never named.** A briefing at runtime glosses
   terms outside the vocabulary (`hectolitres per hectare`, `glut`, `cremant`
   in the live wine run). Such a term is assigned, once, to the subdomain
   given by — in order — the system's own `concepts.subdomain` label for it
   (exact, else best token overlap with a subdomain name), token overlap with
   the subdomain's terms or name, else the group's least-weighted subdomain
   (the reader most likely met it nowhere). The assignment and how it was made
   are in the artifact under `subdomains.runtime_assignments`. The system's
   label is used only as a placement hint for an unscored term; it never
   touches a labelled term's truth.
4. **K.** `prior_knowledge` stays the global encoding multiplier trait; the
   generator (`persona_gen.learning_block`) computes it as the
   vocabulary-weighted mean of `familiar_subdomains`, clamped to the archetype
   band, and the loader checks the two agree within 0.15.

The report's *Subdomains* section scores the system against this (reported,
not gated): (a) `concepts.subdomain` labels vs the fixture
taxonomy, exact after `normalize_term` and "near" on token overlap; (b)
`UserScope.subdomain_familiarity` vs the true weights — Spearman over
subdomains with ≥ 1 attested term and the mean |known/attested − weight|; (c)
depth fit, read as *plainness* (`harness/plainness.py`) rather than as
definitions per 100 words: Flesch–Kincaid grade, words per sentence,
group-vocabulary terms per 100 words, and glosses per 100 words (parenthetical
asides, paired dashes and "X, meaning Y" / "X, that is, Y" cues), split by
whether the briefing's event fell mostly in a subdomain the persona holds
(weight ≥ 0.7) or does not (≤ 0.3). The expectation the human stated is that
the briefing is *plainer* where the reader is a beginner — lower grade and
lower term density in not-held than in held — while glosses stay occasional
(≤ 1 per 100 words) everywhere. `explained_terms` per 100 words is in
the bundle as "made clear"; it counts terms explained in plain
words as well as glossed ones, so it is not a gloss measure. Offline,
the stub labels each term with the group-description phrase it shares most
tokens with (it is never handed the taxonomy), so (a) and (b) exercise the
plumbing only; (c) is real because the stub's text is what it is.

## A skip means they did not read it

The live read-through had Sam skip 2 % of a briefing (`scroll 0.024`) and then
ask three sharp questions about its contents. A skip is a glance at the top of
the page and a close, and a glance reaches the first sentence. So after a
`skip` reading act (`BriefingView.reading_act`, set by the runner after the
reading draw) the responders multiply the first-turn probability by
`SKIP_FIRST_TURN_FACTOR = 0.15` (`skip_adjusted_rates` keeps the
question/reaction split) and restrict every term the persona can ask about,
presuppose or mention to the briefing's first sentence plus whatever the thread
added after the briefing (answers it did read). After a `skim` nothing
changes. The live responder applies the same deterministic gate before calling
the model and shows it only the glanced text. `metrics.thread_engagement`
scales its expected first-turn and turn counts by the same factor for skipped
briefings, so a skip does not read as under-engagement. Register and templates
are untouched.

## Where §0 left something implicit (and what was done)

These are the only places the implementation had to choose; none changes a
number in §0.

1. **`asked_and_answered` on a term with no trace.** Rule 3 presumes a trace.
   A persona that generated the question and read the answer is given a trace
   with certainty at the `studied` S0 (0.8 d), `D − 1`. An existing trace is
   also *floored* at that S0, so a skim followed by an elaborated answer is
   never weaker than a studied read. Without this a beginner who skimmed a
   gloss (S0 = 0.15) and then asked would re-ask the same term a day later.
2. **Feedback after a success.** Rule 4 says "add a restudy event". Applied at
   the *pre-event* R (a restudy at the post-update instant has R = 1 and gains
   nothing), which is what makes feedback roughly double the effect as the rule
   intends.
3. **Lapse cap.** The lapse formula alone can leave S *larger* than before for
   a small S; FSRS caps post-lapse S at the prior S and so does this.
4. **Retrieval events on a term with no trace** (only reachable live, or via
   probe feedback): a success forms a `studied` trace with `n_ret = 1`; a
   failure with feedback is a first glossed encounter at `read` quality.
5. **Quality on a re-read.** Rule 2's k does not depend on read quality; a
   `skipped` re-read is no event, everything else is a k = 0.55 restudy.
6. **A prose check in §0.3 does not match its own formula.** The review says
   S0 = 0.4 d gives R ≈ 0.79 / 0.33 / 0.16 after 1 day / 1 week / 1 month.
   Under the stated formula the values are 0.79 / 0.44 / 0.23. The formula is
   the spec and is what is implemented (and asserted in the tests); the
   consequence is that a once-read gloss is more durable here than the
   review's target band of "0.2–0.35 after a week", so S0 is the first
   candidate for downward tuning (see the last section).

## What is deliberately NOT modelled

- **Terms used but not glossed.** A briefing that uses a term without
  explaining it produces no memory event. The review's encoding numbers are
  for *glossed* encounters; unglossed single-encounter pick-up is ~15 % in L2
  and there is no adult-L1-domain figure to put against it. A known
  simplification: a well-informed reader meeting an unglossed term does gain
  something from it.
- **In-thread use as retrieval is taken from the persona's own intent, not
  from the system.** `TurnIntent.presupposes` / `.mentions` name the held
  terms the responder deliberately used, so rule 4 is applied to those with
  `feedback=False`. Nothing is inferred from the system's `understood` list;
  that would let the system's belief train the truth it is scored against.
  Likewise rule 3 is applied to the persona's own `asks_about` when the answer
  was non-empty, not to the system's `asked_about` read-out.
- **Probe reactivity mitigations beyond the two the review names.** The probe
  is a learning event (testing effect) and is modelled as one (rule 4/5 with
  feedback), and the probed subset is kept to 2–4 items with immediate items
  spread over terms not yet probed. Nothing further: no held-out control
  terms, no probe-free personas. The cost is visible in the report —
  `theo_silent`, who never asks anything, still ends the run "knowing" two or
  three terms, and almost all of that is probe feedback plus repeated glossed
  reads. Treat a disengaged persona's `known` count as an upper bound on what
  the *product* taught it.
- **Pseudo-terms never enter the model.** A confabulated definition for one is
  flagged and graded 0; no trace is formed.
- **The L2 → L1-domain caveat.** Every pick-up rate above comes from L2
  learners meeting unknown *word forms*. Our readers usually know the words
  and must learn a *concept and its relations*; form learning is free for
  them and concept learning is closer to Bolger et al.'s adult definition
  studies. The §0.3 encoding probabilities were set above the L2 figures for
  this reason and are the first thing to recalibrate on real users. Domain
  difficulty (wine vs. VC) is not a persona trait; it lives in `D`.
- **Working memory, need for cognition, age, learning style, interest.**
  Excluded by the review (§6); the `learning` block rejects them as keys.
- **The 24-hour sleep bump, expanding-vs-uniform schedules, exponential
  forgetting.** All excluded by §6.

## Where persona behaviour reads memory instead of the static lists

The static `knows` / `does_not_know` are used exactly once: to seed the
`MemoryModel` at t = 0 (`runner.run_persona`). After that:

1. **`responders.PersonaState.refresh(now)`** — the single derivation point.
   `state.unknown` = every glossary term with P(can define) < 0.5 (askable
   with a lacking-class form); `state.known` = every term with P(can use) ≥ 0.5
   (presupposable with a holding-class form). A term can be in neither
   (recognised or definable but met in too few contexts to lean on); such a
   term is neither asked about nor presupposed. When a memory is attached,
   `PersonaState.learn` does not move terms between the sets; it only records
   `learned` (what was learned *by asking*).
2. **`runner._run_thread`** calls `state.refresh(now)` before every
   `plan_turn`, and again after each answer once the memory events for that
   turn are applied. Both `ScriptedResponder` and `LLMResponder` read
   `state.known` / `state.unknown`; the register, templates and the live
   persona prompt do not depend on the memory model. (The live prompt lists only the two
   sets; the middle "half-known" class is shown to the model as neither.)
3. **`runner.run_persona`, after reading**: rule 1 / rule 2 for every term in
   the briefing's `explained_terms` at the harness's own `expected_quality`
   for that reading, before the thread starts; then `refresh`. So a glossed
   term the persona *just read and encoded* is not asked about in the same
   thread (P(encode) is 0.5 for a normal read, so about half of glossed
   unknowns still draw a clarify).
4. **`unknown_term_present`** (drives `metrics.engagement_deviation`) is
   decided from memory at the moment reading starts, not from the fixture's
   `does_not_know`.
5. **`responders.noise_targets`** — its `state.unknown` / `state.known`
   inputs are the memory-derived sets.
6. **`runner._snapshot`** — the ledger's ground truth per snapshot is
   `memory.known_terms(now)` (P(can define) ≥ 0.5). `final_truly_known`,
   `learned` (known at end − known at start, by any route) and `ever_known`
   are derived the same way; `learned_by_asking` and `final_truly_known_static`
   carry the static-key notions alongside.
7. **`probe.ProbeRunner`** samples every rung from `memory.sample_rung`; the
   persona LLM (live) is instructed to that rung and never decides it.

Everything the memory model touches stays on the harness side: it is never in
a dict handed to `judge(...)`, never written to the store, and never passed to
a contract member. `verify_offline` check 3c dumps every system-owned table and
searches it for the probe's pseudo-terms and the word "rung".

## The ledger's ground truth: dynamic by default, static for comparison

A term counts as **known at time t** when P(can define) ≥ 0.5 at t. So a term
learned by asking can be forgotten (and the ledger's `explained` row for it
becomes a false positive when it is), and a term glossed on three spaced
occasions can be learned without a question (and a ledger that still has it
`familiar` is scored against a reader who now knows it).

The static key — `knows` plus asked-and-answered, forever — is also
computed on every snapshot (`truly_known_static`) and reported next to the
dynamic result for every persona (`under static truth precision … recall …`).
`python -m harness.run --ledger-truth static` makes it the gating key for a
run. Only the ledger agreement switches; every other metric that reads
`final_truly_known` reads the dynamic key, and `cold_start` reads `ever_known`
(the question there is whether the system claimed a term it never taught; a
term taught and then forgotten was still taught).

## How to read the report's Learning section

```
knowledge trajectory  (mean graded rung: immediate / retained)
  persona                    s1       s2       s3 ...
  sam_beginner            0.0/-       --    1.0/-    -/3.0 ...
```

One column per session that ran a thread. Each cell is `immediate / retained`:
the mean graded rung (0–3) over items introduced *this* session, and over
items introduced in an *earlier* session and not shown again this one. `-`
means no item of that kind was probed that session (a session whose briefing
glossed nothing has no immediate item; the first session has nothing to
retain); `--` means the session had no probe at all (a quiet round).

Below the table, one line per persona:

```
sam_beginner (K=0.10, m=0.75): immediate 1.0 -> retained@<=1d 1.2; confab 0/3; calibration |graded-sampled| 0.00 over 8
```

- `immediate` — mean rung on this-session items over the run. For a beginner
  this is mostly "did the gloss stick" (rung 0 = it did not).
- `retained@<bucket>` — mean rung on earlier-session items in the *longest*
  retention bucket that has data (`<=1d`, `2-7d`, `>7d` meaning 7–30 days,
  `>30d`). The line under it gives all four buckets with their n. With the
  fixtures' poll intervals (3–12 h) an eight-round run spans one to four
  simulated days, so `>7d` and `>30d` are empty in a default run; they fill
  under `--horizon-days`.
- `confab k/n` — confabulations over pseudo-term items. Offline the stub
  confabulates on every fourth pseudo-term deliberately, so `1/4` is the
  expected offline value and exercises the flag; live it is a measurement.
- `calibration` — mean |graded − sampled| over real items. 0 offline by
  construction (the stub answers at the sampled rung). Live it is the number
  that says whether the LLM persona is voicing the model's knowledge or its
  own: a gap that grows with the term's obscurity means the persona model
  answers from what *it* knows about wine, not what the memory says.
- `known a -> b (ever c); learned [...] (by asking [...]); forgotten [...]` —
  known-set sizes at start and end, the peak, and the terms that moved. A
  term in `learned` but not in `by asking` was learned from repeated glossed
  reads and/or probe feedback.

Per-persona detail (sessions, items with `sampled_rung`, `graded_rung`,
`days_since_last_exposure`, `r_at_probe`, `p_define_at_probe`) is in the JSON
artifact under `personas[].probes` and `personas[].learning`. The persona's
answer text and the grader's reason are not in the artifact; `harness.run`
writes them next to it as a `<artifact>.probe_answers.jsonl` sidecar, one line
per probe item.

## Offline vs live

Offline there is no LLM anywhere in the loop: the memory model samples a rung,
the stub persona "answers" at exactly that rung, the stub grader returns it.
The pseudo-term confabulates on every fourth pseudo probe. This exercises every
path and keeps the artifact byte-reproducible; it says nothing about whether a
real model can be held to a rung.

Live (`--live`), the persona is voiced by `claude-sonnet-5` by default
(`harness/config.py`; `--cheap`, `--persona-model` and `HARNESS_PERSONA_MODEL`
change it) and is given the three-part prompt
with an instruction pinning it to the sampled rung (rung 0 on a pseudo-term
confabulates with probability 0.15); the canonical gloss and the story are
shown to it only at rungs ≥ 2. The canonical gloss is the sentence in the
system's own briefing or answer that carried the term — the harness has no
hidden glossary, so it grades against what the system actually said. A
separate grader call (`claude-sonnet-5` by default; `--grader-model`) grades against the §0.4 rubric and sets
`confabulated`. Neither call goes through `Judge` (they are the human side of
the conversation), and neither is logged to the store.

## Constants most likely to need tuning against real-user data

1. **`S0` for a normal read (0.40 d) — and the whole encoding table.** Set
   above the L2 figures on an argument, not a measurement, and (item 6 above)
   the review's own week/month check values do not follow from its formula:
   S0 = 0.4 gives 0.44 after a week, not 0.33. If real users retain a
   once-glossed term less well than that, S0 ≈ 0.25 d hits the review's
   0.2–0.35 target band. This is the first thing to fit.
2. **`k_ask` (0.75) and the `studied` floor on asked-and-answered.** Together
   they decide how long an answer to one's own question lasts — for the
   beginner persona this is the single biggest driver of whether the same
   term gets asked twice, and it is set from a d ≈ 0.40 generation effect on
   lab material, not from users of this product.
3. **`RECALL_GIVEN_RECOGNITION` (0.65) with the 0.5 define threshold.** The
   two together mean "known" requires R ≥ 0.77, and the rung-3 gate requires
   three contexts. A small change in either moves every persona's known-set
   size and the ledger gate with it; the 0.65 is an L2 recall/recognition
   ratio and the 0.5 threshold is a convention.

Honourable mention: `w10 = 0.94` in the gain kernel, which is the knob that
moves the spacing ridgeline if real users' optimal re-exposure gap turns out
to differ from Cepeda et al.'s.
