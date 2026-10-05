# Conversational Competence Agent — Spec (v1)

## What it does

A multi-agent system that helps users become conversationally competent in
groups they define (e.g., "NBA fans," "startup founders") — able to follow
and contribute to that group's conversations without embarrassing knowledge
gaps.

**Cold start is the primary case, not an edge case.** The product's purpose
is helping someone *become* conversant, so the default assumption is that a
user knows nothing about a newly created group. Any interaction that only
works for a user who is already conversant and merely has gaps is a design
error. (This premise supersedes the earlier quiz-based interaction model —
see "The system briefs; the user asks," below.)

This project has **two first-class deliverables, built together, not
sequentially bolted on**:

1. **The agentic orchestration loop** — an Orchestrator coordinating
   per-group Monitor and Assessor sub-agents to brief the user on what's
   happening in a group, answer whatever they ask about it, track which
   concepts they actually know, and decide what to surface and when.
2. **A persona-based simulation harness** — a set of personas simulating
   distinct users/groups, run through the same loop, used to measure whether
   the system actually *teaches*: each persona carries a memory model of
   what it knows, and a harness-only post-session probe (never shown to a
   real user) tests that memory after every session. Whether the system's
   concept ledger agrees with what the persona knows is still reported, as
   a diagnostic. This is not secondary QA scaffolding; it is as central to
   what this project is proving out as the orchestration loop itself,
   because the product is architected for multiple real users and personas
   are the primary way that is exercised and validated pre-launch. It has
   already earned its place: it is what caught and killed the silence-based
   inference described in the ledger section.

The system is designed for **multiple users from the start** — one real
human user plus N persona-driven synthetic users, each with their own fully
isolated set of groups and concept ledgers. Auth *mechanics* can be minimal
for v1 (see Non-functional constraints), but the data model and access
patterns are real multi-user architecture, not a single-user shortcut.

### Groups (persistent, user-created)
- A **group** is a target social/professional circle a user wants to be
  conversant with.
- Groups are created only by explicit user action — the system never
  autonomously spawns a group.
- Once created, a group persists indefinitely (no auto-archive in v1).
- Each group has its own concept ledger and dedicated Monitor + Assessor
  instances — state and tuning isolated per group, and per user.

### Goals (ephemeral, layered on a group)
- A **goal** is a time-bound objective attached to a group (e.g., "dinner
  with 3 founders Thursday"), with a deadline and a completion/expiry
  condition.
- An active goal raises the priority and frequency of briefings for that
  group and biases which events and concepts get covered first. It never
  resets the underlying concept ledger — the goal is a temporary intensity
  spike on top of persistent state.
- Without an active goal, a group still receives lower-intensity ongoing
  briefing (see "Staying current," below), not full dormancy.
- A goal raises priority *among groups that have something untold*. It does
  not make the system speak when there is nothing new to say (see
  Orchestrator, below).

### Orchestrator
- The only component that interacts with a user directly.
- On every cycle, decides — via runtime judgment, not a fixed rule —
  whether an active goal needs attention, whether a Monitor has flagged
  something material, and whether/when/in what form any of this should reach
  the user. (Note: *whether* a briefing is warranted is now fully
  deterministic — see "Staying current." What remains a judgment call is
  timing and framing.)
- Sole owner of the "interrupt the user or wait" decision. Monitors report
  findings; they never alert the user directly. This keeps
  materiality-vs-user-context weighing centralized rather than duplicated
  per group.
- **It never repeats a story.** Human decision, checkpoint 4 (2026-09-13),
  verbatim: *"It should never repeat the story; if there's nothing new to
  say, don't say anything."* Concretely: raising a topic (`raise_topic`)
  means briefing the user on exactly one **untold** event — a material event
  with no exchange yet recorded against it. A group with nothing untold gets
  nothing raised, regardless of the interrupt-timing verdict and regardless
  of its `never_engaged` / behind / goal-soon flags. There is no fallback to
  "recent material events", no "ongoing" or recap mode, and an already-told
  event cannot be pinned or re-queued. The live failure this fixes: the
  first full five-persona run briefed the silent persona on the same Rioja
  harvest story five sessions running, reworded each time, because his
  group stayed flagged never-engaged and the briefing step fell back to
  something ongoing when nothing was pending.
- **Session need is computed without an LLM** and does two separate jobs
  that must not be conflated. First, *whether* a group has anything to say:
  purely the untold queue — empty means silent. Second, *ordering* among
  the groups that do have something untold: `never_engaged` (this group has
  never had an exchange — cold start), behind (several untold events piled
  up), and goal-soon (active goal deadline approaching) raise a group's
  priority. Cold start is still a strong reason to *prioritise* a group's
  first untold event; it is never a licence to speak with nothing. The
  flags decide who goes first among groups with news, never whether to speak
  with none.
- **Sole writer of `surfaced_at`.** The Orchestrator sets it at the moment it
  puts an item in front of the user. Reading behaviour never writes it and
  never removes an item from the unsurfaced count — it may only enrich an
  item that is already surfaced.
- **v1 delivery to the user is pull, not push**: the Orchestrator surfaces
  queued material updates whenever the user opens a session, on whatever
  platform/surface that session happens to be on. The user receives nothing
  out-of-band while no session is open — but the underlying detection work
  is not gated on a session being open (see Monitor, below, and
  Non-functional constraints). Push/out-of-band delivery *to the user* is an
  explicit v2 idea, deferred rather than rejected — see Non-goals.
- **A session may legitimately show nothing at all.** If no group has an
  untold event when the user opens a session, the session prints nothing for
  any group. Empty is the correct output, not an error and not a prompt to
  find something to say.

### Monitor (one persistent instance per group, per user)
- Watches for external signals relevant to its group (e.g., NBA trades,
  startup funding rounds) using a single general-purpose web/news search
  tool — not a fixed per-domain curated source list, and not dynamic
  discovery/onboarding of new specialized third-party APIs at runtime (that
  approach was considered and rejected as infeasible for v1: credential
  provisioning for arbitrary new APIs isn't something an agent can reliably
  self-serve).
- Runs as a **real, continuous scheduled background process** per (user,
  group) pair, on its fixed interval, independent of whether a user session
  is open — it is not computed retroactively only when a session starts.
  Findings are written to the event queue on every poll. Fixed-interval only
  in v1 (not adaptive/volatility-based).
- On each poll, judges materiality as a contextual judgment call — is this
  significant enough that someone conversant in this group would be
  expected to know it — not a keyword match.
- A second, related judgment call — **what to query for and which returned
  sources to trust/prioritize as relevant and credible for this specific
  group** — is what lets the Monitor work for a group nobody anticipated in
  advance (e.g., "wine tasting"), since it queries the live web at runtime
  rather than relying on a pre-built per-domain source map. See
  Non-functional constraints for this as a formal judgment point.
- Material findings are reported to the Orchestrator; non-material polls
  still report on schedule. This happens continuously in the background
  regardless of session state; only the user-facing surfacing of it is
  pull-gated by the Orchestrator.
- Does not talk to the user directly and does not decide whether to
  interrupt.
- **For persona-driven synthetic users**, Monitor input is a simulated/
  injected event feed rather than live external polling — personas exercise
  the same materiality-judgment and reporting logic as a real Monitor
  instance, without generating extra live external-API load per synthetic
  user.

### Assessor (one persistent instance per group, per user)
*(The name is inherited from an earlier quiz-based design and no longer
describes testing the user. Its actual job is: brief plainly, answer what is
asked, and maintain the concept ledger. Renaming is an open cosmetic
question, not a behavioral one.)*

#### The system briefs; the user asks
- **The system does not ask the user questions.** A briefing ends when the
  substance ends. No closing question, no "let me know if you have
  questions," no invitation line, no offered threads or suggested topics.
- The expectation is that the user will typically ask a question or two of
  their own. **What they choose to ask is far better evidence of where they
  stand than an answer to a question we picked** — "what's a derogation?"
  and "was that a chaptalisation year?" locate someone precisely, and
  neither is a reaction to our framing.
- **This supersedes both the earlier quiz model and its "brief first, then
  ask" replacement.** A closing question, however politely phrased, is still
  a test: it presupposes knowledge the user may not have. The live example
  that prompted this change — *"Does a harvest running this far ahead of
  normal sound like good news for the wine, or more like a warning sign to
  you?"* — is unanswerable unless you already know what fast ripening does
  to wine.
- **No score, level, grade, or progress metric is ever shown to the user.**
  The ledger and derived band are internal.
- **Cold start is the default.** A new group starts with an empty ledger and
  a `beginner` prior. Because there is no closing question to carry the
  explanatory load, the *briefing itself* must not presuppose: terms are
  explained inline as they are used, subject to the glossing policy below.

#### What gets raised: exactly one untold event
- **A briefing is written from exactly one untold event.** The Assessor's
  `raise_topic` receives one event that has never had an exchange recorded
  against it, and briefs that. It returns nothing when the group has nothing
  untold — it does not fall back to recent events, does not recap, and does
  not summarise "where things stand."
- The briefing generative call (`briefing` prompt, now v6) is told plainly:
  you receive exactly one untold event; you are never called when there is
  nothing new; where the event overlaps with material already raised
  (`previously_raised`), brief only what is new. Reworded repetition of an
  already-told story is the failure this rule exists to prevent.
- Told is told. Once an exchange is recorded against an event, that event
  never re-enters the queue, and pinning it is refused.

#### An exchange is a multi-turn conversation
- Briefing → the user may ask something → the system answers → they may ask
  again → and so on.
- Every exchange is anchored to the single untold event it was opened on
  (`event_id`, always recorded). Follow-up answers within the thread may
  draw on anything relevant; it is the *opening* that is constrained to one
  new event.
- **The conversation ends when the user stops. The system never judges that
  it is finished** and never forces closure. It answers what is asked and
  stops there.
- This is not a new judgment point. The system does not decide when the
  exchange is over; it simply stops having anything to respond to. For
  bookkeeping, a thread is treated as quiet at session end or after a fixed
  idle threshold — a deterministic condition, not a model decision.
- **Closing a thread must be idempotent**: extraction runs once per thread
  regardless of how many times the quiet condition fires.

#### Evidence comes from the whole thread
- **Concept-evidence extraction runs over the entire thread**, at the point
  the conversation goes quiet — not per-reply. The evidence is: the
  questions the user asked, their reactions, and any terms they used
  correctly across all turns.
- Reading behaviour is **not** an input to this extraction, and neither is
  exposure. Only what the user said counts. (Two deterministic steps that do
  consult a reading record run at the same thread-close moment, and neither
  is part of extraction: the `unknown` → `familiar` transition, described in
  the ledger section, which sees only whether the briefing was skipped; and
  skip classification, described under Reading behaviour, which sees whether
  a briefing was skipped and what the user's own evidence says about the
  terms it defined, and writes only to the exchange row, never the ledger.)
- The extractor (`concept_evidence` v7) also **labels each term it names
  with a subdomain** — the slice of the group a specialist would file it
  under (`viticulture & harvest`, `appellation rules`). It is handed the
  label names already in use for the group so labels converge, and nothing
  else derived from the ledger: no counts, no bands, no reading fields. It
  remains the one call that writes to the ledger and is given nothing
  derived from the ledger beyond term states. See the ledger section for
  what the labels are for.

#### Glossing and depth: a decision about us, not a claim about them
- "Should I gloss this term again?" is deliberately **separated from the
  knowledge claim**. It is a fact about our own behaviour, not theirs.
- Concretely: `unknown` is the only state that gets glossed. `provisional`,
  `familiar`, `explained`, and `confirmed` are all no-gloss. **One correct
  use, or one read definition, stops re-explaining** (human decision,
  checkpoint 3: *"one correct use should stop from re-explaining a term"*).
  **Stopping asserts nothing about their understanding.** If they later
  reveal they didn't follow it, the term reverts to `unknown` and gets
  explained again as normal.
- **Gloss once, then only on request.** Human decision, checkpoint 5
  (2026-09-14): a term defined in a briefing the user read *or skimmed* is
  never defined again unless the user asks. The decision was taken with the
  cost measured and in view: in the first complete five-persona run, one
  skimmed gloss left **30 of 32** labelled terms still unknown, and under
  this rule that single skimmed gloss is also the last unprompted one — Ade
  asked *"What's a head coach exactly?"* one sentence after the briefing
  defined it. The recommendation was to require `read`/`studied` (not
  `skimmed`) for the no-gloss effect; the human kept the rule as shipped, on
  the grounds that asking is cheap and the transcripts show people do ask.
  Asking routes through `thread_reply` and lands `explained`. The accepted
  cost is one clarifying question per term for skimmers; the re-ask rate in
  the regression gate (see the harness section) is the number that watches
  it.
- **Depth is decided per topic, not per person** (`briefing` v6). The
  briefing locates the event in the group's subdomains and reads the user's
  `subdomain_familiarity` for each (see the ledger section). In a subdomain
  where the user is `conversant` or `fluent`, it writes as to a peer in that
  subdomain: fewer definitions, lead with what they would not have
  predicted, one level more specific. In a subdomain where they are
  `beginner`, it defines as for the group band. An event that straddles
  subdomains has each part pitched to its own. The per-term ledger still
  wins on any specific term — a term the ledger marks known is never glossed
  whatever the subdomain says, and an `unknown` term is still glossed once —
  and the reading pattern (next bullet) still shifts the overall pitch. The
  live failure this fixes: a wine professional whose ledger held `potential
  alcohol`, `budburst` and `yields` as correctly used was given the same
  inline definition of `hectolitres per hectare` as the beginner, because
  the per-term ledger has no way to say what three viticulture terms imply
  about a fourth, and the group band averaged her viticulture fluency and
  her commercial ignorance into `developing`.
- **The reading pattern is a prior, usable only once earned** (`briefing`
  v5). Once a user has **three or more resolved skips** (see Reading
  behaviour), a `p_informed` of 0.7 or above pitches the briefing one band
  up and defines less; 0.3 or below holds the band and keeps definitions;
  anything between is ignored. Below three resolved skips it is ignored
  entirely. It enters the briefing context as counts and the smoothed share
  only — never dwell, scroll or read quality.
- This is the mitigation for two opposite failures. It fixes the quiet-user
  trap — a user who reads and asks nothing would otherwise be glossed at
  forever. And it fixes real over-explaining: in a live transcript, a wine
  professional was told what Champagne and Bordeaux are, three times,
  because those terms sat at `unknown` after three unquestioned exposures.
  (Under the current rule the first of those glosses, once read, would have
  been the last.)
- The glossing decision is **deterministic** — read straight off ledger
  state — and feeds the briefing generative call (`briefing` prompt, now
  v6), together with the deterministic `subdomain_familiarity` map and
  `reading_pattern`. None of these is a sixth judgment point, and none
  writes to the ledger itself; the state they read is written at thread
  close, by the rules in the ledger section. (Locating the event's
  subdomain happens inside the briefing call — it is not pre-computed —
  which is the one generative act in this list.)

#### Gap remediation
- Owns gap remediation, routed by judgment call, not a fixed rule:
  - Small gaps → inline explanation, no external search.
  - Larger/deeper gaps → real resource search using the same
    general-purpose search tool as the Monitor, candidate sources evaluated
    for relevance/depth, one surfaced — never a guessed/hallucinated
    resource. Formulating the query and judging which sources are
    trustworthy/appropriate-depth for this specific group is itself a
    genuine judgment call (see Non-functional constraints), not a
    per-domain lookup table.
- Gap routing (`gap_routing` v5) and the reply (`thread_reply` v3) both
  receive the user's `subdomain_familiarity` map and **answer at the depth
  of the user's familiarity in the subdomain the question falls in**, the
  per-term ledger still winning on any specific term.
- Resource search is a capability inside the Assessor, not a separate
  sub-agent.
- **For persona-driven synthetic users**, Assessor input is the persona's
  own questions and reactions to briefings — the same signal type real usage
  produces, per the constraint that synthetic and real signal must match or
  the harness tests a different problem than the one that ships.

### Concept ledger and derived proficiency (persistent memory layer, not an agent)

Per (user, group), the system tracks **each concept/term** with a state and
its supporting evidence, **grouped into subdomains the system infers**.
**Every state except `familiar` is reached only through something the user
said. `familiar` is reached through a definition we put in front of them
and a briefing they did not skip. No state is reached through silence after
mere usage.**

- `unknown` — the default. Also where a concept reverts, **from any state**,
  if the user reveals a misunderstanding of it; the correct-use count and
  the read-explanation count both reset with it.
- `provisional` — the user used the term correctly once.
- `confirmed` — the user used the term correctly on **two independent
  occasions**.
- `explained` — they asked, and we explained it. Strong, timestamped.
  Parallel to the ladder rather than a rung on it.
- `familiar` — a briefing **defined the term inline** (per the model's own
  list of what it glossed, `explained_terms` — not every term it used) AND
  the reading record for that briefing is anything other than `skipped`
  (`skimmed` / `read` / `studied`). Applied at thread close (`close_thread`),
  never by the code path that records reading (`record_reading` writes the
  exchange row and nothing else). From `unknown` the term becomes
  `familiar`; any stronger state is kept. A missing reading signal is
  treated as `skipped`. Human decision, checkpoint 3 (2026-09-12),
  verbatim: *"We explained and they read or skimmed should count as they
  have familiarity; over time this will grow into more confidence. One
  correct use should stop from re-explaining a term."*

Alongside the state, each concept carries **`read_explanations`**: +1 for
each explanation of the term the user actually read — a gloss in a
non-skipped briefing, or an answer to their own question (asking counts,
and still lands `explained`). A gloss the user then asked about in the same
thread, or on a term they got wrong in the same thread, earns no read
credit: the definition evidently did not land. Reset to 0, together with
`correct_uses`, by a revealed misunderstanding.

Each concept also carries a **`subdomain`** — the slice of the group a
specialist would file it under (`appellation rules`, `viticulture &
harvest`): a short, lowercase, free-text label, canonicalised with the same
normaliser as terms, written by the two calls that already name terms
(`concept_evidence` v7 and `briefing` v6), both of which are handed the
labels already in use so they converge. It is **a fact about the term, not
the person**: it changes no state and moves no band by itself. Churn rule:
a label is never overwritten with nothing, and a *different* label wins
only while the term has no attested evidence — so the exposure-era label
from a briefing can be corrected by the first call that reads what the user
did with the term, and is then fixed. Human decision, 2026-09-14, verbatim:
*"the glossary shouldn't be an exact word to word matching they should cover
the subjects subdomains within the group that the user is familiar with and
use that among other factors to decide how in depth the harness needs to
explain a certain topic for the user to understand."* What was **not**
done: term identity is unchanged (`canonical_term` still merges only on
containment), no fuzzy glossary replaced the exact one, and nothing is
inferred from what the user did not say. Subdomains are an *aggregation* of
evidence the ledger already holds, along a new axis. The system never sees
the persona harness's own taxonomy; it infers its labels from the
conversation.

**Two correct uses, not one**, because a single correct use sits at only
~0.43 posterior under standard guess/slip assumptions, and there is a
specific confound in this product: the briefing hands the user the term
moments before they would echo it.

**Exposure promotes nothing, ever.** `note_exposure` records that a term was
used in front of the user. It is useful for knowing whether a term is new to
the conversation — but it carries **zero inferential weight**. Using a term
is not explaining it: only a definition the briefing reports having given,
in a briefing the user did not skip, reaches `familiar`. Nothing in this
system infers knowledge from silence.

> **Do not reintroduce a silence-based state.** An earlier design had an
> `assumed` state: a term used three or more times without the user
> questioning it. The persona harness measured it directly against ground
> truth and it was **anti-predictive at every threshold tried** — lift −0.34
> at one exposure, −1.00 at three and at five. Splitting by provenance was
> decisive: `assumed` reached via silence scored **0/2**, while `assumed`
> reached via one correct use scored **9/9**. The state was conflating two
> different things, and only one of them carried signal. The literature
> predicts exactly this: student question-asking runs about 0.11 questions
> per student per hour regardless of understanding (Graesser & Person,
> 1994), so silence is the likely outcome under both hypotheses and
> separates neither. This state was deleted on measured evidence, not on
> taste. It will look like an obvious improvement again; anything of this
> shape must be measured against ground truth before it ships.
>
> **`familiar` is not that state.** `assumed` was silence after mere usage:
> no explanation given, no evidence the text was looked at. `familiar`
> requires two positive acts — a definition was put in front of them, and
> the text was read or skimmed — and requires that twice before it touches
> the band. It is the one sanctioned path from reading behaviour into the
> ledger, and it is deliberately narrow. It was a human decision taken with
> the `explained` 0.50 measurement in view (see the band, below), on the
> rationale that familiarity strengthens with repetition. It ships under
> the same test that killed `assumed`: the harness measures
> `P(known | familiar)` split by `read_explanations` bucket (1 vs 2+). If
> the 2+ bucket does not beat the 1 bucket on the corrected personas,
> `READ_EXPLANATIONS_BEFORE_BAND` rises. (Checkpoint 5 measured the split
> on the first complete run: 0.06 at one read, 0.67 at two or more, tiny n
> above the bar. The threshold stays at 2.)

**General proficiency** is a coarse band — `beginner` / `developing` /
`conversant` / `fluent` — **derived from the ledger on read. It is never
stored as an independently mutable value and never decays.** Which terms
count as known:

- `confirmed` counts unconditionally.
- `familiar` and `explained` count only once
  `read_explanations >= READ_EXPLANATIONS_BEFORE_BAND` (currently 2,
  mirroring the two-use rule for `confirmed`).
- `provisional` never counts.

The denominator is the set of **attested** terms: those the user asked
about, used correctly, got wrong, or has `read_explanations >= 2` on. Terms
we merely said are excluded from both sides, so silence votes in neither
direction. A term read once votes neither way. A term asked about once is
attested but not known — asking is direct evidence they did not hold it
then — and becomes known on the second explanation they read. Note the
consequence: **a single ask no longer moves the band by itself.**
Previously `explained` counted immediately; the checkpoint-3 measurement
had `explained` at 0.50 precision in the band, which is why it now needs
the second read. The band is consulted **only as a prior when there is no
specific evidence about the term at hand.** Everyone starts at `beginner`.

**Subdomain familiarity** (`UserScope.subdomain_familiarity`) is the same
computation per label: `{subdomain: {known, attested, band}}`, using the
**identical known/attested predicates as the group band** (hoisted to one
place so the two cannot drift), with unlabelled terms reported under
`(unlabelled)` and counting toward no other slice. The slice band uses
lower absolute floors and higher ratio floors than the group band
(`config.subdomain_band`: `developing` at ≥ 2 known and ratio ≥ 0.5,
`conversant` at ≥ 4 and ≥ 0.7, `fluent` at ≥ 8 and ≥ 0.8, against the
group's 3 / 12 / 25 at 0.35 / 0.6 / 0.8). Lower absolute floors because a
slice the size of `appellation rules` may only ever surface eight or ten
distinct terms and would otherwise never read as `conversant` however
completely it was held — exactly the case the readout exists for; higher
ratio floors because with few terms in the denominator two lucky
confirmations would otherwise dominate. It has the same standing as the
band — a prior, consulted for depth when there is no specific evidence
about the term at hand, overridden by the per-term ledger whenever there
is — and it will be wrong more often in absolute terms than the group band,
at the same cost: one skipped or one extra definition. If a live run shows
`conversant` slices producing briefings that beginners in that slice
cannot follow, raise `SUBDOMAIN_CONVERSANT_MIN_KNOWN` before touching the
prompt.

Being slightly wrong about the band, or a slice band, is acceptable by
design: the cost of an error is one unnecessary explanation or one
clarifying question, not a broken experience. Do not build machinery to
make this precise.

All sub-agents read/write the ledger rather than passing state solely
through conversation context, so state survives across sessions and across
the ephemeral lifecycle of individual goals. Scoped by user identity
throughout — no cross-user reads or writes.

### Reading behaviour (recorded separately; one narrow door into knowledge, one evidence-gated reading of the act)
- **There is no read receipt or acknowledgement button.** Instead, the
  system observes *how* a briefing was read — dwell time and scroll depth —
  and derives a coarse reading quality (`skipped` / `skimmed` / `read` /
  `studied`) and, from the same two proxies, a continuous **`attention`**
  readout (0–10). Both are recorded as their own facts against the briefing
  (`exchanges.read_quality`, `exchanges.attention`), behind the same wall.
- **Reading behaviour reaches the concept ledger through exactly one door.**
  It is evidence about attention, not comprehension. Display metrics are
  near-worthless as comprehension proxies: Apple's Mail Privacy Protection
  inflates roughly half of reported email opens, and the IAB had to invent
  "viewability" precisely because "served" meant nothing. The one door is
  the `unknown` → `familiar` transition in the ledger section: a term the
  briefing defined inline, in a briefing whose reading record is anything
  other than `skipped`. Magnitude is irrelevant to it — `skimmed` and
  `studied` open the door equally — and it takes two such readings before
  the band notices.
- **What a skip meant is resolved from the user's own evidence, never from
  the skip.** Human decision, 2026-09-14, verbatim: *"Skipping can often
  mean the user already knew the information. But skipping and then
  indicating later down the line that they don't know about the subject
  discussed would mean they just lazily skipped it and don't have that
  info."* Implemented as `exchanges.skip_kind` ∈ {`unresolved`, `informed`,
  `lazy`}. A skipped briefing starts `unresolved`. It is resolved **only**
  by evidence the user produces about the terms that briefing defined — at
  a later thread close, or from speech evidence already on the ledger:
  `informed` if they turned out to hold them (used correctly), `lazy` if
  they turned out not to (asked about them, or got them wrong). **Negative
  evidence wins** on the same skip: skip-then-ask is `lazy` whatever else
  they knew. A skip of terms the ledger already attests through speech
  (`provisional` / `explained` / `confirmed`) is `informed` at once;
  **`familiar` never vouches for a skip**, because `familiar` came from
  reading. No evidence: it stays `unresolved`. **A skip still writes
  nothing to the ledger.** Every label comes from something the user said;
  this is the reading act being interpreted in the light of that evidence,
  which is the inference the human asked for — not silence becoming
  evidence.
- **The reading pattern** (`UserScope.reading_pattern`): counts of informed
  / lazy / unresolved skips and a Beta(1,1)-smoothed `p_informed`, the
  share of *resolved* skips that were informed. **A prior about the person,
  not a fact about any term.** The briefing may consult it only at three or
  more resolved skips (see Glossing and depth). **This is the one place
  reading behaviour informs anything beyond the `familiar` door, and it
  does so only after the user's own evidence has labelled the act.**
- **Permitted uses, exhaustively:** (a) engagement reporting; (b) enriching
  an item that the Orchestrator has already surfaced; (c) the `familiar`
  door above, applied at thread close and nowhere else — which is also how
  glossing tapers off for a quiet reader, since glossing reads ledger
  state; (d) skip classification and the reading pattern above — a label on
  the exchange and a prior on the person, both gated on the user's own
  evidence, neither a ledger write. Nothing else. It is not an input to
  concept-evidence extraction, not a tiebreaker on any term, and it **never
  writes `surfaced_at`** or otherwise changes the unsurfaced count.
  `record_reading` writes the exchange row and nothing else; the ledger
  consequence and the skip label are drawn only when a thread closes.
  Dwell, scroll, `read_quality` and `attention` reach none of the judgment
  contexts (the tests scan every context for the field names and band
  strings); the reading pattern reaches the briefing as counts and the
  smoothed share only.
- **Known surface gap — state honestly, do not paper over.** v1's interaction
  surface is a CLI inside Claude Code, which has no scroll position and no
  dwell measurement: text simply prints. Therefore the **data model and the
  persona simulation carry full reading behaviour**, while **the CLI records
  only time-to-respond**, stored with an explicit marker that it is a
  degraded proxy rather than an equivalent measurement. Real collection
  waits for a surface that can actually measure it. This is a known gap, not
  a solved problem, and nothing downstream may treat CLI-derived reading
  quality as if it were instrumented. It matters more now that reading has
  a ledger consequence and a classification: on the CLI, "not skipped" is a
  time-to-respond judgement, so `familiar` minted there — and any
  `attention` or `skip_kind` recorded there — is exactly as trustworthy as
  that proxy.

### Staying current (no active goal)
- Background polling keeps writing material events to the event queue
  whether or not the user shows up.
- **Whether a briefing is warranted is deterministic**: the group's
  **untold queue** — material events for that (user, group) with no
  exchange recorded against them (`untold_events`). Non-empty means there is
  something to say; empty means there is nothing, and nothing is said. It is
  not an LLM judgment call. Only the Orchestrator writes `surfaced_at`, at
  the moment it puts the item in front of the user, and every exchange
  records the `event_id` it was opened on, so an event leaves the untold
  queue exactly once and for good.
- **An empty untold queue is not a problem to solve.** Earlier behaviour
  fell back to "recent material events" or an "ongoing" recap when nothing
  was pending, which is how the same harvest story got told five times. That
  fallback is gone. A slow-moving group with nothing new produces no
  briefing, for as many sessions as it takes for something new to happen.
- **There is no decay model.** Concepts and terminology do not decay — once
  known, known. Current events don't decay either; the user simply hasn't
  been told about them yet, which is directly countable from the event
  queue. The old design fused these two things; this spec separates them and
  deletes decay as a concept. (The persona harness's memory model *does*
  forget, because real readers do — that is ground truth about the persona,
  on the harness side, and it is precisely what lets the harness catch a
  ledger that still says `explained` for a term the reader has lost. It is
  not a system-side decay model and must not become one.)
- The Orchestrator still decides timing and framing (judgment) for events
  that are untold; it no longer reasons about whether knowledge has faded,
  and it never reasons its way into speaking when there is nothing untold.

### Persona-based evaluation harness (central deliverable)
- **The harness's primary objective is the persona's actual knowledge
  growth**, not ledger agreement. Each persona carries a memory of what it
  can say about each briefing term; a **harness-only post-session probe**
  tests that memory after every session; and the harness scores whether the
  system taught anything and whether that stuck. Ledger precision/recall —
  the previous headline — are demoted to diagnostics. Nothing the probe
  does is ever shown to a real user: it is the harness's instrument, not a
  product feature, and the no-questions rule is untouched.
- Personas represent distinct user/group behavior patterns: e.g., a
  consistent quick-check-in user vs. a deep-engagement user; a user new to a
  fast-moving domain vs. one new to a slow-moving one; and at least one
  deliberately noisy persona (sometimes silent about a term they don't know,
  sometimes asking about one they do) to keep pressure on inference that
  leans on silence.
- Each persona is defined by: a **group taxonomy** (the fixture's
  `group.subdomains`: the vocabulary partitioned into 3–6 short lowercase
  labels, every vocabulary term in exactly one, optionally plus anchor terms
  outside the vocabulary that give a runtime term something to match);
  per-persona **`familiar_subdomains`** weights (0–1 per label: how at home
  they are in each slice); two evidence-based **learning traits**
  (`prior_knowledge` K, 0–1, and `memory_rate` m, 0.7–1.4 — the two
  individual differences the literature actually supports, with age,
  working memory and verbal ability expressed through m rather than carried
  separately; the fixture loader refuses any other key); a question-asking
  disposition; and a reading-behaviour profile. `knows` / `does_not_know`
  remain the **labelled subset** the ledger is scored against, and must
  agree with the weights (a `knows` term in a slice weighted ≤ 0.2, a
  `does_not_know` term in one weighted ≥ 0.8, or `prior_knowledge` more
  than 0.15 from the vocabulary-weighted mean of the weights is a fixture
  error). All of it is ground truth: never in the group description the
  system sees, never in a judgment context, never in the store.
- **Knowledge is a memory, not a list.** Per (persona, term) the harness
  keeps an FSRS-style trace — stability, difficulty, distinct contexts,
  successful retrievals, last event — with power-law forgetting and a
  ladder derived from it (recognises → can define → can use in a new
  context). At t = 0 every `knows` term is seeded strong, `does_not_know`
  never, and every other vocabulary term is seeded with probability equal
  to its subdomain's weight. Reading a gloss, re-reading it later, asking
  and being answered, and retrieving under probe each update the trace by
  rules and constants taken from the literature; a term first glossed in a
  subdomain the persona is at home in encodes faster (P(encode) ×
  (0.7 + 0.6·w)) — the "prior knowledge helps new learning in-domain"
  finding, kept modest and never compounding. A runtime term the fixture
  never named is placed into a subdomain once, using the system's own label
  only as a placement hint for an unscored term; it never touches a
  labelled term's truth. The static lists are used exactly once, to seed;
  after that persona behaviour reads memory. Design:
  `harness/personas/research/learning_model.md` §0; implementation,
  deliberate omissions, and the constants most likely to need tuning:
  `harness/LEARNING.md`.
- **Reading is state-dependent, and its intent is recorded.** A persona
  chooses an **informed skip** (it holds the story's terms), a **lazy skip**
  (at a profile-set rate), a **skim** or a **read** from what it currently
  holds, and the hidden intent is written to the harness artifact, never to
  the store. A skip means they did not read it: after a skip the persona
  can only ask about, presuppose or mention what the briefing's first
  sentence and the thread's later answers contained, and its first-turn
  probability is scaled down to match, so a skip does not read as
  under-engagement. This is the persona side of the skip-semantics decision
  under Reading behaviour.
- **The probe**: after each session, two to four items — one term
  introduced this session, one or two from earlier sessions chosen to span
  the retention interval, and, every other session, a **plausible
  pseudo-term** from the domain that was never shown — each asked as (a)
  have you come across it, (b) what does it mean, (c) use it in a sentence
  about this week's story. An LLM grader scores a **0–3 rung** (never seen
  / recognises / can define / can use) against the canonical gloss — the
  sentence in the system's own briefing or answer that carried the term,
  since the harness has no hidden glossary — and flags a confident answer
  to a pseudo-term as `confabulated`. The persona is instructed to the rung
  its memory sampled and never decides it. The probe is itself a learning
  event (the testing effect) and is modelled as one; the probed subset is
  kept small. Neither call goes through `Judge`; neither is logged to the
  store; `verify_offline` dumps every system-owned table and searches it
  for the pseudo-terms and the word "rung". Offline, no LLM is in the loop
  at all — the stub answers at the sampled rung — which exercises every
  path, keeps the artifact byte-reproducible, and proves nothing about
  whether a real model can be held to a rung.
- **Personas ask their own questions rather than answering ours**, and
  **simulate reading behaviour** (dwell, scroll depth) against each
  briefing. This makes the harness's signal *closer* to real usage than
  before, because it no longer depends on questions we authored — the same
  property that makes user-initiated questions better evidence in production
  makes persona-initiated questions a better test.
- **Persona speech must match the speech *situation*, not merely the
  topic.** The situation is a human talking to an assistant. An earlier
  style profile was derived from Hacker News comments — people performing
  opinions for an audience — and produced persona turns like *"6 august for
  chardonnay is wild"* and *"yields must be down too if it's ripening that
  fast, heat usually shrivels the bunches"*. These were rejected as
  unrealistic: people use agents to get the information they want; they do
  not offer opinions to an agent, and they do not explain the topic they are
  trying to learn *to* the agent. Forum posters assert; assistant users ask.
  This was flagged as a register bias at the time and mis-corrected — the
  fix only shortened replies and kept the speech acts. Grounding on a forum
  register is how it got in; do not do it again.
- **Realistic persona turns are requests**: clarify a term, ask for the
  implication, check a belief phrased as a question, acknowledge. Not
  opinions or explanations directed at the agent. Persona style is grounded
  on WildChat-1M (real human→assistant conversations), filtered to turns
  that follow an informational assistant message — i.e. the same position
  in a conversation a persona occupies after a briefing. Checkpoint 5
  measured the rebuilt register holding: 38 user turns, all requests or
  check-beliefs.
- **Knowledge remains detectable under this register from what a question
  presupposes.** *"Does that mean yields are down?"* reveals that the asker
  already holds `yields` — which is why concept-evidence extraction reads
  presuppositions, not just assertions. But note the design consequence: if
  real users rarely volunteer knowledge, the `confirmed` path (two correct
  unprompted uses) will fire **rarely** in production, and asking →
  `explained` becomes the dominant real signal. **Any conclusion previously
  drawn from persona runs that assumed volunteered knowledge is provisional
  until re-measured on the corrected personas.** That includes the numbers
  in the ledger section's do-not-reintroduce note: the *direction* of that
  finding is expected to survive (silence still separates nothing), but the
  magnitudes were measured on personas that asserted more than real users
  do.
- Each persona runs as its own synthetic user in the same multi-user
  architecture the real human uses — own groups, own isolated ledger, own
  simulated Monitor input stream.
- **What the harness scores, in order of standing:**
  1. **Knowledge trajectory** — mean graded rung on items introduced this
     session (immediate) and on items from earlier sessions not shown again
     (retained, by retention bucket ≤ 1 d / 2–7 d / > 7 d), per persona per
     session; plus confabulation count, and the known-set size at start,
     end and peak with the terms that moved (learned by asking, learned
     from repeated glossed reads, forgotten).
  2. **Calibration** — mean |graded − sampled rung| over real items. Live,
     this is the number that says whether the LLM persona is voicing the
     memory model's knowledge or its own; a gap that grows with a term's
     obscurity means the persona answers from what *it* knows about wine.
  3. **Attention agreement** — the system's `attention` readout against the
     persona's true reading depth.
  4. **Skip classification** — precision/recall of the system's `informed`
     / `lazy` labels against the persona's recorded intent, once evidence
     exists; and **reading-prior convergence** — whether `p_informed`
     converges to the persona's true informed-skip rate.
  5. **Subdomains** (reported, not gated, on this first pass) — the
     system's `concepts.subdomain` labels against the fixture taxonomy
     (exact after normalisation, "near" on token overlap, plus the count of
     distinct labels per group with near-duplicates flagged);
     `subdomain_familiarity` against the true weights (Spearman over slices
     with ≥ 1 attested term, and mean |known/attested − weight|); and
     **depth fit** — inline definitions per 100 words of briefing, split by
     whether the event fell mostly in a slice the persona holds (weight
     ≥ 0.7) or does not (≤ 0.3). Fewer definitions in held slices is the
     goal. Offline, the stub labels each term with the group-description
     phrase it shares most tokens with (it is never handed the taxonomy),
     so label and familiarity agreement exercise the plumbing only; depth
     fit is real because the stub's gloss decision is the system's own rule.
  6. **Ledger agreement, now a diagnostic** — precision/recall between the
     ledger and the persona's known set, scored against **dynamic ground
     truth**: a term is known at time *t* when the memory model's P(can
     define) ≥ 0.5 at *t*. So a term learned by asking can be forgotten
     (and the ledger's `explained` row becomes a false positive when it is),
     and a term glossed on three spaced occasions can be learned without a
     question (and a ledger that still has it `familiar` is scored against
     a reader who now knows it). The pre-model static key (`knows` plus
     asked-and-answered, forever) is computed on every snapshot and reported
     alongside; `--ledger-truth static` makes it the gating key for a run.
     Per-state precision, `P(known | familiar)` split by read count, the
     exposure table, elicitation and the walls below are all still
     reported. Absence of a label still means unscored, not negative; there
     is still no numeric knowledge level on the system side to converge on.
- **Measured, first live probe run:** 9/10 rung agreement between persona
  and grader, 0 confabulations, and 2/2 skips classified correctly. Small
  n: the mechanism works; the magnitudes are early.
- **A quiet round is not a gap.** A harness round in which a persona's group
  has nothing untold is expected to produce no briefing, and the harness
  scores that as correct silence, not as a missed surfacing. The
  `topic_without_events` interrupt case expects silence. Conversely, the
  harness must catch the rerun: the same `event_id` opening two exchanges
  for the same (user, group) is a defect.
- The harness must measure inference rules against ground truth, not just
  exercise them. It has already earned this: it is what established that
  silence-based promotion was anti-predictive, by splitting a state's
  provenance and scoring each path separately. Any new promotion rule gets
  the same treatment before it ships. **`familiar` is the current case**:
  the harness reports `P(known | familiar)` split by `read_explanations`
  bucket (1 vs 2+), and `READ_EXPLANATIONS_BEFORE_BAND` rises if the 2+
  bucket does not beat the 1 bucket on the corrected personas. Skip
  classification and subdomain familiarity are the next two, and each ships
  with its own scoring (items 4 and 5 above).
- It must also verify the walls hold: a persona that studies every briefing
  but asks nothing must accumulate **no** `provisional`, `explained`, or
  `confirmed` knowledge — not from reading behaviour, not from exposure.
  What it may accumulate is `familiar`, and only on terms a briefing
  actually defined inline. The harness **reports (does not gate)** how many
  of a silent persona's terms cross `READ_EXPLANATIONS_BEFORE_BAND`, so the
  size of the silent-reader path is always visible. Glossing should still
  taper off for that persona — now because its terms sit at `familiar`.
  Under the probe, the disengaged persona's `known` count is an **upper
  bound** on what the product taught it: `theo_silent` ends a run "knowing"
  two or three terms, almost all of it probe feedback plus repeated glossed
  reads.
- **Persona execution model — settled.** Personas must generate their own
  questions live, because a pre-authored question cannot react to briefing
  content the persona has not seen. **Scripted persona *responses* are no
  longer viable on the live path.** A deterministic offline responder is
  retained for reproducibility (regression runs where identical input must
  produce identical output), but it is not the mechanism under test.
- The real human's actual usage is a separate, single live data point used
  as a qualitative sanity check (does this seem to be working) after the
  harness looks solid — not a statistical validation and not a substitute
  for the harness.

### Regression gate and prompt proposer (eval / LLMOps)
Two tools sit downstream of a live run. Neither edits `src/`, the prompts or
the harness; the proposer's `--apply` is the single, explicit, human-invoked
exception. Full detail: the last section of `harness/README.md`.

- **`harness/gate.py`** reads the run artifact and its SQLite store
  (read-only) and decides in three tiers. Exit 0 is PASS; anything else —
  including INCOMPLETE, a Tier 1 check that could not run because no
  database was supplied — is 1. An unevaluated hard gate is not a pass.
  - **Tier 1, hard and binary:** `no_questions_to_user`,
    `reading_regression` and `silence_regression` (reused from the artifact
    rather than recomputed, so the gate cannot disagree with `metrics.py`
    about them); `never_repeat` (every persona exchange has an `event_id`,
    no event twice per user, no two briefings for one user with token
    Jaccard > 0.6); `no_fabricated_figure` (every currency-, scale- or
    unit-bearing number in a `thread_reply` answer must appear, after
    normalisation to significant digits, somewhere in that call's own
    input); `zero_judgment_errors` (the harness's own probe users excluded);
    `cost_within_budget` (default ceiling $4.00).
  - **Tier 2, scored, paired per persona against `--baseline`:** materiality
    F1 ≥ 0.90 pooled; ledger precision and recall per persona may not drop
    more than 0.15 against that persona's baseline; `unknown` precision
    ≥ 0.90; `confirmed` precision ≥ 0.65; and the **re-ask rate** — the
    share of briefings whose thread asked about a term that briefing's
    `explained_terms` contains — at most 0.09 and not up more than 0.06 on
    the baseline. Without a baseline only the absolute thresholds apply. A
    failure within 0.05 of its band prints `MARG` with advice to rerun once
    before deciding; the exit code is still 1. The disengaged persona's
    ledger numbers are reported and not gated.
  - **Tier 3, reported, never gated:** engagement deviation; `familiar` /
    `explained` precision split by `read_explanations` (< 2 / ≥ 2);
    `provisional` precision; per-persona turn counts; cost and latency by
    judgment point; a briefing-form line (word count, sentence count, inline
    definitions, and a check for briefings that touch more than one of the
    user's events); reply hygiene (replies that carried a literal `\u`
    escape to the user); and the harness's own `question_faithfulness`
    self-audit.
- **`harness/propose.py`** runs the gate, then gathers evidence
  **deterministically, without a model** — for each Tier 1 failure, Tier 2
  drop and Tier 3 anomaly, the briefing, the turns, the matching
  `judgment_log` rows (prompt version, model, reasoning, verdict) and the
  exchange's `explained_terms` / `read_quality`, capped at 40 items, Tier 1
  first. It parses a **constraint set** from every `[x]` decision in
  `CHECKPOINTS.md`, every `**Changed**` block in `prompt-changelog.md`, this
  spec's non-goals section, and seven hard-coded product rules
  (brief-then-stop; never ask; ledger not score; no silence promotion;
  reading only through `familiar`; never repeat; gloss once). It then makes
  one structured call and writes at most `--max` proposals to
  `proposals/<run_id>/` — each a unified diff with the exact old/new text
  (version header bumped for prompts, validated with `patch --dry-run`),
  the **evidence quoted verbatim** underneath, a ready-to-paste changelog
  entry, expected effect, risk, constraints touched and confidence. A
  proposal citing no supplied evidence is rejected; one that touches a
  human decision must argue why it is not a reversal or it is dropped. **It
  never applies anything.** `--apply PATCH` applies exactly one patch after
  a dry run, inserts the sibling changelog entry, and tells the operator to
  run a fresh live run through the gate. The proposer's first output,
  `concept_evidence` v6 (definition-checking questions such as *"so a
  series c would just be the round after this one?"* land in `asked_about`,
  not `understood`), was reviewed by the human and applied on 2026-09-14; its
  first measured run through the gate is still to come.

## What it explicitly does NOT do (v1)

- No agent-initiated group creation — groups are created only by explicit
  user action.
- No quizzes, tests, exams, or check-ins framed as such — and no score,
  level, grade, or progress metric shown to the user at any point. (The
  harness's post-session probe is not this: it is harness-only, runs
  against synthetic personas, and is never shown to a real user.)
- **No system-initiated questions at all** — no closing question, no "let me
  know if you have questions," no invitation lines, no suggested threads. A
  briefing ends when the substance ends.
- **No repeating a story, and no speaking when there is nothing new.** No
  recaps, no digests, no "here's where this stands" filler, no "ongoing"
  briefings, no fallback to recent-but-already-told events. A briefing is
  opened on exactly one untold event or not at all; a told event is never
  re-raised, even reworded, even for a group that has never engaged, is
  behind, or has a goal due. An empty session is a valid session.
- **No re-defining a term the user has already read defined.** A term
  glossed in a briefing the user read or skimmed is defined again only if
  the user asks (checkpoint 5). No second unprompted gloss, no "reminder"
  definition, no re-gloss because the reading was only a skim.
- No system-judged end of conversation — the exchange ends when the user
  stops, and the system never forces closure.
- No withholding of substance to probe the user: the system briefs, and the
  user asks whatever they want.
- **No inference of knowledge from silence or exposure.** Exposure counts
  are bookkeeping; they promote nothing. The `assumed` state that once did
  this was deleted on measured evidence — see the ledger section before
  proposing anything of that shape. (`familiar` is not this: it needs a
  definition to have been given and the briefing not to have been skipped.)
- No read receipts or acknowledgement buttons — reading behaviour is
  observed, not requested.
- **No use of reading-behaviour magnitude as knowledge evidence** — dwell,
  scroll depth, `attention`, and the `skimmed`/`read`/`studied` distinction
  never touch the concept ledger. Two sanctioned exceptions, both narrow.
  The first is binary: a non-skipped reading of a briefing that defined a
  term moves that term `unknown` → `familiar` at thread close (see the
  ledger section). The second is evidence-gated: a *skipped* briefing is
  later labelled `informed` or `lazy` solely from what the user themselves
  said about the terms it defined, and the share of informed skips becomes
  a prior on the person that the briefing may consult only after three
  resolved skips — a label on the exchange and a pitch on the briefing,
  never a ledger write and never a claim about any term. The other
  permitted uses are engagement reporting and enriching already-surfaced
  items.
- **No fuzzy term matching, and no persona taxonomy on the system side.**
  Subdomain familiarity aggregates ledger evidence by a label the system
  infers; term identity and merge rules are unchanged. The harness's group
  taxonomy and `familiar_subdomains` weights are ground truth the system
  never sees.
- No decay model and no time-based knowledge expiry — concepts don't decay,
  and "falling behind" is counted from untold events rather than modeled.
  (The persona memory model forgets; that is harness-side ground truth,
  not a system feature.)
- No adaptive/volatility-based polling interval — fixed interval only.
- No group auto-archiving or retirement — groups persist indefinitely
  regardless of dormancy.
- No inference of *knowledge* from any behavioural or telemetry signal
  beyond the `familiar` door. (Reading behaviour is collected in v1 as an
  attention fact — this replaces the earlier blanket non-goal on
  telemetry. Every ledger state other than `familiar` is fed exclusively by
  conversational evidence from the thread, and `familiar` needs a
  definition to have been given, not just a screen to have been looked at.
  The reading pattern is not a knowledge inference: it says nothing about
  any term, and its labels come from speech.)
- No separate resource-finding sub-agent — resource search is a capability
  inside the Assessor.
- No dynamic discovery/onboarding of new specialized third-party search
  APIs at runtime — the system uses one general-purpose web/news search
  tool for every group; considered and rejected as infeasible for v1
  (arbitrary API credential provisioning isn't something an agent can
  reliably self-serve).
- No real dwell/scroll instrumentation in v1 — the CLI cannot measure it;
  only a marked-degraded time-to-respond proxy is recorded. Real collection
  waits for a capable surface.
- No scripted persona responses on the live harness path — personas generate
  their own questions; the deterministic responder is for offline
  reproducibility only.
- No statistical validation from the real-user sanity check — that phase is
  explicitly qualitative (N=1), not proof.
- No push notifications or other out-of-band alerts sent **to the user** in
  v1 — delivery to the user is pull-only, surfaced at the user's next opened
  session. (This is a user-facing delivery restriction only; background
  polling/detection infrastructure is real and required — see
  Non-functional constraints. Push delivery to the user is deferred to v2,
  not rejected.)
- No standalone app or web UI — v1's interaction surface is CLI/chat within
  Claude Code (see Non-functional constraints).
- No full account/auth system — v1 needs only enough identity mechanism to
  keep one real human account and N persona-driven synthetic accounts
  cleanly separated (see Non-functional constraints). Building
  password/OAuth/session-management infrastructure is out of scope.
- No automatic application of prompt or constant changes — the proposer
  proposes; a human applies, then re-runs through the gate.

## Data model basics

- **User**: id, kind (`real` | `persona`), display name; if `persona`,
  includes a profile description, behavior-pattern parameters, a
  question-asking disposition, a reading-behaviour profile, a `learning`
  block (`prior_knowledge`, `memory_rate`), a group taxonomy
  (`group.subdomains`) with per-persona `familiar_subdomains` weights, a
  labelled concept set per group (`knows` / `does_not_know`), and — at
  run time, harness-only — a per-term memory trace and the hidden intent
  behind each reading act. Everything after the display name is
  harness-only and never readable by the system under test.
- **Group**: id, user_id, name, created_at. Belongs to exactly one user.
  (No decay-rate model — deleted.)
- **Goal**: id, group_id, description, deadline, completion/expiry
  condition, status (active / completed / expired). Assumption: at most one
  active goal per group at a time — flag if that's wrong.
- **ConceptLedgerEntry** (per user, per group, per concept): concept/term,
  state (`unknown` | `provisional` | `familiar` | `explained` |
  `confirmed`), **`subdomain`** (nullable free-text label, canonicalised;
  written by the extractor and the briefing; a fact about the term, not the
  person; never overwritten with nothing, and replaced only while the term
  has no attested evidence), **correct_use_count** (independent correct
  uses by the user: one → `provisional`, two → `confirmed`; reset to zero
  on revealed misunderstanding), **exposure_count** (from `note_exposure`;
  bookkeeping only, **never** a promotion trigger), **read_explanations**
  (explanations of the term the user actually read: a gloss in a
  non-skipped briefing, or an answer to their own question; no credit for a
  gloss they then asked about or got wrong in the same thread; reset to
  zero on revealed misunderstanding; gates whether `familiar`/`explained`
  count toward the band, at `READ_EXPLANATIONS_BEFORE_BAND` = 2) and
  last-explained timestamp, evidence reference (which thread produced the
  current state), and first-/last-observed timestamps. There is no stored
  level, no topic scores, and no decay parameters. The state column's CHECK
  constraint enumerates the five states; existing databases are migrated
  by rebuilding it, and `subdomain` is an additive migration.
- **Derived proficiency band**: `beginner` / `developing` / `conversant` /
  `fluent`, computed from the ledger at read time. Numerator: `confirmed`
  terms, plus `familiar` and `explained` terms with `read_explanations >=
  2`. Denominator: attested terms — asked about, used correctly, got wrong,
  or `read_explanations >= 2`. Not a stored, independently-mutable field.
- **Derived subdomain familiarity** (`UserScope.subdomain_familiarity`):
  `{subdomain: {known, attested, band}}`, the identical predicates per
  label, with `(unlabelled)` as its own bucket and the lower floors in
  `config.subdomain_band`. Computed at read time; never stored.
- **Derived reading pattern** (`UserScope.reading_pattern`): counts of
  `informed` / `lazy` / `unresolved` skips per (user, group) and a
  Beta(1,1)-smoothed `p_informed`. Computed at read time from exchange
  rows; never stored; a prior about the person.
- **BriefingThread** (per user, per group; the exchange record): id,
  started_at, **`event_id`** (the single untold MonitorEvent this exchange
  was opened on — always present, never null; an exchange without an event
  cannot exist, because there is nothing to brief), the briefing content,
  **`explained_terms`** (JSON list: the model's own declaration of which
  terms the briefing defined inline — the input to the `familiar`
  transition and to skip classification, and deliberately not derived from
  exposure), `surfaced_at` (written by the Orchestrator only),
  `went_quiet_at`, the reading-behaviour record including **`attention`**
  (0–10, from the same two proxies as `read_quality`) and **`skip_kind`**
  (`unresolved` | `informed` | `lazy`; null unless the briefing was
  skipped; resolved only by the user's own evidence about `explained_terms`,
  negative evidence winning, `familiar` never vouching), an
  extraction-completed marker (so closing is idempotent), and the ledger
  updates derived when the thread went quiet.
- **Turn** (per thread, ordered): ordinal, role (`system` | `user`),
  content, timestamp. The briefing is the first system turn; subsequent
  turns are user questions and system answers. A thread may legitimately
  have exactly one turn — the user asking nothing is normal, not a failure.
- **ReadingBehaviour** (one per briefing): dwell time, scroll depth, derived
  quality (`skipped` | `skimmed` | `read` | `studied`), the `attention`
  readout, and **`measurement_source`** (`instrumented` |
  `cli_time_to_respond_proxy`) so degraded data can never be silently
  treated as equivalent to real measurement. Enters ledger updates only as
  the binary skipped/not-skipped test for the `familiar` transition at
  thread close; enters skip classification only as "was it skipped"; dwell,
  scroll, `attention` and the finer quality grades never reach the ledger or
  any judgment context.
- **MonitorEvent** (event queue, per user, per group): timestamp,
  description, materiality judgment, whether it was reported to the
  Orchestrator, and `surfaced_at` (null until the Orchestrator puts it in
  front of the user). **The untold queue** is the derived set of material
  events with no BriefingThread carrying their `event_id` (`untold_events`
  store query); it is what determines whether a briefing is due, and a
  briefing consumes exactly one entry from it. Told is permanent: once an
  exchange references an event, that event is never briefed again. For
  persona users, sourced from a simulated/injected event feed rather than
  live external polling.

Relationships: one User → many Groups (fully isolated from every other
user's Groups) → zero-or-one active Goal (plus historical goal records) →
one Monitor instance, one Assessor instance, one concept ledger, one event
queue, and many BriefingThreads (each anchored to exactly one MonitorEvent,
with many Turns and one ReadingBehaviour), all scoped to that (user, group)
pair. A MonitorEvent has at most one BriefingThread. All sub-agents share
the ledger as the single source of truth rather than passing state through
conversation context alone. Everything harness-side (taxonomy, weights,
memory traces, reading intent, probe items and grades) lives in the harness
artifact, never in these tables.

## Non-functional constraints

- **Genuinely agentic at five specific judgment points**:
  1. **Materiality assessment** (Monitor) — is this event significant enough
     that someone conversant would be expected to know it.
  2. **Dynamic source/query selection** (Monitor and Assessor) — what to
     query for and which returned sources to trust/prioritize as relevant
     and credible for a given group. This is what makes the system work for
     groups nobody anticipated in advance, since it uses one general-purpose
     search tool over the live web rather than a pre-built per-domain source
     map.
  3. **Concept-evidence extraction** (Assessor) — run over a whole thread
     once it goes quiet: what did the user's questions, reactions, and term
     usage show they know, use correctly, misunderstand, or don't know.
     Since v7 it also labels each term it names with a subdomain, handed
     only the label names already in use. Reading behaviour and exposure
     are excluded from this input by construction.
  4. **Gap-size routing** (Assessor) — inline explanation vs. real resource
     search. Since v5 it receives `subdomain_familiarity` and answers at
     that depth, the per-term ledger still winning on any specific term.
  5. **Interrupt-vs-wait timing/framing** (Orchestrator) — for a group that
     has something untold. This judgment is about *when and how* to raise a
     new event; it is never consulted on whether to speak when there is
     nothing new, and its verdict cannot produce a briefing for a group
     with an empty untold queue. The `interrupt_timing` prompt (v4) names
     "the rerun" as a failure mode and states that `raise_topic` is only
     ever about something new.

  These five require runtime reasoning given ambiguous, evolving signal, not
  hardcoded rules or keyword matching. Separately, a **briefing generative
  call** produces the briefing text — generative rather than a judgment
  classification. (It replaces the earlier `conversation_opener` call, which
  replaced quiz generation; there is no opener or question field any more,
  because the system no longer asks anything.) The briefing call (v6)
  receives the per-term glossing states, the `subdomain_familiarity` map,
  and `reading_pattern` (counts and smoothed share only, usable at three or
  more resolved skips); it locates the event's subdomain itself; and it
  returns `explained_terms`, its own list of which terms it defined inline,
  plus `subdomains` labels for the terms it named. It is only ever invoked
  with exactly one untold event, and it is never invoked when there is
  nothing new. The reply call (`thread_reply` v3) receives the same
  familiarity map.

  **Explicitly not judgment points:** whether a group has anything to say
  (deterministic: the untold queue is non-empty or it is not); ordering
  among groups that do (`never_engaged` / behind / goal-soon flags, plain
  code); decay-due assessment (deleted); deciding when a conversation is
  over (the user decides by stopping; a thread is treated as quiet at
  session end or after a fixed idle threshold); the glossing decision
  (deterministic from ledger state, feeding the briefing call); the
  `unknown` → `familiar` transition (deterministic from `explained_terms`
  plus a non-skipped reading record, at thread close); the `attention`
  readout (arithmetic on the same two proxies as `read_quality`); skip
  classification (deterministic from `explained_terms` and ledger evidence
  about those terms, at thread close); the reading pattern and subdomain
  familiarity (SQL over exchange and ledger rows at read time). Everything
  else — polling on a timer, reading/writing the ledger, checking goal
  deadlines — is deterministic plumbing and should be built as plain code,
  not routed through an LLM.
- **Ledger promotion is conversational-evidence-only, with one named
  exception.** No code path may promote a concept from exposure, silence,
  dwell, scroll, or elapsed time. The exception is `unknown` → `familiar`
  at thread close, which requires a definition the briefing reports having
  given (`explained_terms`) and a reading record other than `skipped`; it
  lives in `close_thread`, not in `record_reading` and not in extraction.
  Skip classification also runs at thread close and also reads the reading
  record, but it writes only `exchanges.skip_kind` — never a ledger row —
  and the labels it writes are derived from ledger evidence, not the other
  way round. This remains a hard architectural boundary, not a convention:
  the extraction input still does not carry reading fields, and the
  `familiar` step is the only place a reading record is consulted for a
  ledger write. Enforce by construction rather than by reviewer vigilance.
- **Harness ground truth never crosses the wall.** The group taxonomy,
  `familiar_subdomains`, the memory model, reading intent, and the probe's
  items, pseudo-terms and grades are never in `system_visible_group`, never
  in a dict handed to `judge(...)`, never written to the store, and never
  passed to a contract member. `verify_offline` scans every system-owned
  table for the pseudo-terms and the word "rung", and the answer-key fields
  for leakage, with SQL independent of the code it audits.
- **Thread closing is idempotent** — extraction runs exactly once per thread
  no matter how many times the quiet condition fires.
- **One event, one exchange, enforced by construction.** Every exchange
  records its `event_id`; the untold queue is derived from that record; and
  `raise_topic` returns nothing for an empty queue. There is no code path
  that opens an exchange without an event, no fallback selection of
  already-told events, and no pin of a told event. Enforce this the same way
  as the ledger boundary — structurally, not by prompt wording alone.
- **Background polling is real, continuous infrastructure, required in
  v1**: each (user, group) Monitor instance runs as a scheduled background
  job on its fixed interval, independent of whether a user session is
  open — it is not computed retroactively at session start. Findings are
  written to the event queue on every poll, whether or not a session is
  open. This is distinct from, and does not imply, push delivery to the user
  (see below).
- **Delivery to the user is pull/display-only in v1**: the Orchestrator
  surfaces due material only when the user opens a session, on whatever
  platform that session is on. No push notifications, no out-of-band
  alerting is sent to the user. (v2 may add real push delivery to the
  user — noted here only so it isn't mistaken for a rejected idea. The
  background polling infrastructure itself, above, is separate and already
  required in v1.)
- **v1 interaction surface is CLI/chat within Claude Code** — sessions run
  inside Claude Code itself; no standalone app or web UI is being built for
  v1. This surface has **no scroll position and no dwell measurement**; text
  simply prints. Reading behaviour is therefore modelled and simulated in
  full, but on the CLI only time-to-respond is recorded, marked as a
  degraded proxy via `measurement_source`. Do not present the proxy as
  equivalent to instrumented measurement anywhere in code, output, or
  reporting. The CLI prints nothing for a group with nothing untold, and a
  session with nothing untold anywhere prints nothing at all; do not add
  placeholder text ("nothing new today") to fill the silence — that is a
  recap by another name. Two repairs to this surface, recorded so they are
  not undone: the CLI `session` path was still calling the pre-redesign
  `submit_reply`/`opener` API and would have crashed on first use; it now
  follows the thread model — print the briefing, answer questions until a
  blank line, then close the thread. And replies no longer carry literal
  unicode escape sequences to the user (`ensure_ascii=False` when building
  the context, plus a decoder on output); the gate's Tier 3 reply-hygiene
  line watches for a regression, because a literal `\u` in a reply is both
  a user-facing defect and a confounder for the figure check.
- **Multi-user data isolation is real architecture, required from v1**, not
  a deferred concern: every read/write path must be scoped by user identity,
  and one user's (real or persona) group/goal/ledger data must never be
  visible to or mutated by another user's sub-agent instances.
- **Auth mechanics can be minimal/stubbed for v1** — a static record for the
  one real human account plus N persona-driven synthetic accounts is
  sufficient; no password/OAuth/session-management system is required. The
  requirement is architectural (user-scoped data access throughout), not
  operational (a real login flow).
- **Personas generate their own questions on the live harness path**;
  scripted responses are not viable there. A deterministic offline responder
  is retained for reproducible regression runs only. Build the harness so
  the persona-response source is swappable between the two. The same holds
  for the probe: offline the stub answers at the sampled rung and the stub
  grader returns it; live both sides are real model calls that never go
  through `Judge`.
- Requires a single general-purpose web/news search tool, shared by the
  Monitor (external event detection, real users only) and the Assessor
  (gap-remediation resource search) — not a per-domain curated source list,
  and not runtime discovery/onboarding of new specialized third-party APIs
  (rejected as infeasible for v1). Exact provider is not prescribed here.
- Per-(user, group) isolation is mandatory for Monitor/Assessor tuning and
  ledger state.
- Persistent storage required; group and ledger records must survive
  indefinitely across sessions (no v1 expiry).
- Online-dependent for real users (Monitor/Assessor rely on external
  search); persona users do not need live external connectivity since their
  Monitor input is simulated.
- No accessibility or latency targets specified by the source intent — none
  invented here.

## Risks

**Overall risk level: Medium.** No money or third-party credentials are
handled, but the system holds multiple users' personal ledger data side by
side in the same running system, depends on external data sources, and its
usefulness hinges on judgment quality rather than pure correctness.

- **Cross-user data isolation bugs** (ledger, Monitor, Assessor): because
  the real human account and N persona accounts run concurrently in the same
  system, a scoping bug could leak one user's group data, thread history, or
  ledger into another's session. This is the most consequential risk
  introduced by building real multi-user architecture in v1, and should be
  tested explicitly (not just assumed correct because "it's just personas").
- **Persona register mismatch**: synthetic users who speak in the wrong
  register produce metrics that look healthy and measure nothing about real
  usage. This has already happened once — a forum-derived style profile
  produced personas that asserted and explained where real assistant users
  ask, and every metric built on those runs is now provisional. Mitigation:
  style parameters are derived **only** from human→assistant data, and the
  **speech-act distribution is reported alongside length** so the register
  itself is checkable, not just the word count. A persona set whose turns
  are mostly assertions is a harness defect regardless of what the agreement
  numbers say.
- **Silence-based inference creeping back in**: the deleted `assumed` state
  was intuitive, cheap, and measurably anti-predictive (lift −1.00 at three
  exposures; 0/2 when reached via silence versus 9/9 via one correct use).
  Any future rule that promotes a concept because the user *didn't* object
  is the same mistake wearing a different name. `familiar` is not that rule
  — it needs a definition given and a briefing not skipped — but it is the
  nearest thing to it that exists, so it ships under the same
  provenance-split measurement, and any proposal to loosen it (count usage
  as explanation, count `skipped` as read, drop the two-read gate) is
  `assumed` returning. Subdomain familiarity is not it either — it
  aggregates speech evidence by label and infers nothing from what was not
  said — but "she's fluent in viticulture, so mark `hectolitres per
  hectare` known" would be, and the system deliberately does not do it:
  the slice band changes depth, never a term's state. The defence is
  procedural: measure any new or loosened promotion rule against persona
  ground truth, split by provenance, before shipping it.
- **The rerun creeping back in** (Orchestrator, Assessor `raise_topic`,
  briefing prompt): the pressure to say *something* to a group that looks
  neglected — never engaged, behind, goal due — is exactly what produced the
  five-session Rioja repeat, and it will present itself again as a
  reasonable-sounding feature ("a quick recap for users who've been away",
  "a digest when the queue is empty", "re-surface the most important story
  if they didn't respond"). Each of these is the fallback that was removed.
  The defence is structural: `event_id` on every exchange, the untold queue
  as the sole source of things to raise, `raise_topic` returning nothing on
  empty, and told events refusing to be pinned. Test the rerun directly: the
  same `event_id` must never open two exchanges for the same (user, group),
  and a group with an empty untold queue must produce no output no matter
  which priority flags are set on it. The gate's Tier 1 `never_repeat` check
  is that test.
- **Quiet is the chosen behaviour, and it has a cost** (accepted knowingly):
  a slow-moving group can yield an empty session, and a user who checks in
  regularly on a quiet group will see nothing, repeatedly, until something
  new happens. This was previously listed as a concern ("a silent or
  never-engaged group goes quiet"); it is now the intended outcome, per the
  checkpoint-4 decision that not speaking beats repeating. The real residual
  risk is upstream: if the Monitor under-detects for a slow domain, silence
  and "nothing is happening" look identical from the user's side, and the
  system has no way to tell them apart either. Mitigation is a Monitor-side
  observability question (poll counts and materiality verdicts should be
  visible per group so a quiet group can be distinguished from a broken
  one), not a licence to fill the gap with filler.
- **Gloss-once has a measured cost** (accepted knowingly, checkpoint 5): one
  skimmed gloss left 30 of 32 labelled terms still unknown, and that gloss
  is now the last unprompted one. The design leans entirely on the user
  asking, which the transcripts support and the reply path makes cheap — but
  a user who skims and does not ask is left holding a term they saw defined
  once and did not take in. The re-ask rate (Tier 2, ≤ 0.09) is the
  canary: if users routinely ask about a term one sentence after it was
  defined, the gloss is not landing and the human's options (b) and (c) —
  require `read`/`studied`, or two reads, for the no-gloss effect — are the
  documented next moves. Do not re-add an unprompted second gloss without
  reopening the decision.
- **`explained_terms` over-reporting** (briefing prompt → `familiar`,
  skip classification, re-ask rate): `familiar` is exactly as narrow as the
  model's honesty about what it defined. If the briefing lists terms it
  merely used, `familiar` collapses into exposure-plus-reading — `assumed`
  under another name — the read_explanations counter inflates with it, and
  now skip classification and the re-ask rate are computed over the wrong
  term set too. This is the integration seam three mechanisms hang on. Test
  that every term in `explained_terms` has an inline definition in the
  briefing text, and that a term used but not defined never reaches
  `familiar`.
- **Subdomain label churn or over-splitting fragments familiarity**
  (`concepts.subdomain`, `concept_evidence` v7, `briefing` v6): a new label
  vocabulary can fork the way term strings once did (`concept_evidence` v2:
  `picks-and-shovels investment thesis` vs `AI infrastructure investment`).
  If `viticulture`, `viticulture & harvest` and `harvest` coexist, a
  persona fluent across all three reads as `beginner` in each, and the
  briefing defines everything at group-band depth — the Pilar failure back
  again, now with a mechanism that looks like it should have fixed it. The
  defences are the same as for terms: the convergence rule (callers are
  handed the labels in use), canonicalisation, and the churn rule that
  freezes a label at first attesting evidence. The harness reports distinct
  labels per group and flags near-duplicates; label agreement against the
  fixture taxonomy and the familiarity gap are the numbers to read. The
  cost of a fork is under-explaining nothing and over-explaining
  everything, i.e. a return to the pre-subdomain behaviour, not a new
  failure.
- **Peer-depth briefing in a mislabelled or thinly-evidenced subdomain
  under-explains** (`briefing` v6, `gap_routing` v5, `thread_reply` v3): the
  slice band has lower floors than the group band by design — four known
  terms at 0.7 make a slice `conversant` — so a term filed under the wrong
  label, or a slice that four lucky confirmations lifted, can pitch a whole
  briefing as to a peer for a reader who is not one. The event's subdomain
  is also located by the briefing call itself, not pre-computed, so a
  mislocated event is pitched to the wrong slice. Both show up in the same
  two places: the re-ask rate (the user asks about a term the peer-depth
  briefing skipped, or about the briefing's own undefined terms) and the
  probe (immediate rung 0 on a term the briefing assumed). The per-term
  ledger still glosses every `unknown` term once whatever the slice says,
  which bounds the damage to depth of framing rather than missing
  definitions. First response to a live signal: raise
  `SUBDOMAIN_CONVERSANT_MIN_KNOWN`, not the prompt.
- **The glossing carve-out becoming a back channel**: glossing now reads
  ledger state, the slice band and the reading pattern rather than its own
  attention inputs, which keeps this surface small — but the reasoning "we
  stopped glossing it, so they must know it" is still wrong, because
  `provisional` and `familiar` are both no-gloss and neither counts toward
  the band on its own, and "we pitched this slice as to a peer" is a
  statement about our depth choice, not their knowledge. Stopping glossing
  is a statement about our behaviour only; test that no promotion beyond
  the rules in the ledger section can originate from a gloss or depth
  decision.
- **Briefing quality / cold-start risk** (Assessor): with no closing
  question, the briefing carries the entire explanatory load. If it uses
  terms it doesn't explain, a not-yet-conversant user gets nothing and has
  nothing to ask about. This is the same failure that has now bitten the
  design twice — first as openers that withheld substance ("what's
  vintage?"), then as closing questions that presupposed it ("does a harvest
  running this far ahead of normal sound like good news for the wine?").
  Downstream QA should evaluate briefings against a cold-start user who
  knows nothing about the group, and check that no briefing ends in a
  question or invitation line. The opposite failure — glossing Champagne to
  a wine professional for the third time — is now handled by the glossing
  policy and subdomain depth and should be regression-tested there. Note
  the interaction with the one-untold-event rule: a cold-start user's
  *first* briefing on a group is also their only chance to get that event's
  context, since it will not be raised again. The briefing must be complete
  enough on its own; the `previously_raised` overlap instruction ("brief
  only what is new") applies to later events, not to the first. The probe's
  immediate rung is now the direct measure of whether a briefing's glosses
  landed for a beginner.
- **Reading behaviour mistaken for comprehension**: dwell and scroll say
  something about attention and nothing reliable about understanding, and
  the pressure to use them as a knowledge signal — especially for a quiet
  user who asks nothing — will be constant. There are now exactly two
  sanctioned uses beyond reporting: the `familiar` door (`unknown` →
  `familiar` at thread close, binary skipped/not-skipped, only on defined
  terms) and skip classification (a label on the exchange, resolved only
  from the user's speech about the defined terms, feeding a prior on the
  person). The specific new failure shapes: `skip_kind` resolved from
  anything other than user evidence about those terms; `familiar` vouching
  for a skip as `informed`; `reading_pattern` consulted below three
  resolved skips; or `attention` magnitude reaching any judgment context or
  ledger write. Each is a defect, and each should be tested for directly,
  not just avoided by convention. The harness scores skip classification
  against the persona's recorded intent precisely so that a
  reading-behaviour signal cannot go live without a ground-truth number
  against it.
- **Degraded CLI proxy treated as real measurement**: v1 cannot measure
  dwell or scroll at all, yet the model, the personas, and the reporting all
  carry full reading behaviour. The risk is that time-to-respond gets
  quietly consumed as if it were instrumented data, producing engagement
  numbers that look precise and are not — and, now, minting `familiar`,
  `attention` and `skip_kind` from a proxy. `measurement_source` must be
  respected at every read site, and harness reporting on `familiar` and on
  skip classification should be split by it.
- **Silent-reader path (accepted knowingly; reported, not gated)**: a user
  who reads every briefing and never speaks can now, over many briefings,
  accumulate `familiar` terms with `read_explanations >= 2` and move off
  `beginner` without ever having said a word. The human chose this at
  checkpoint 3 with the `explained` 0.50 measurement in view, on the
  rationale that familiarity strengthens with repetition. It is bounded:
  only terms a briefing defined inline qualify, each needs two non-skipped
  readings before the band counts it, and a revealed misunderstanding
  resets both counters and reverts the term. The harness reports how many
  of a silent persona's terms cross the threshold, and measures
  `P(known | familiar)` split by read_explanations bucket (1 vs 2+); if
  the 2+ bucket does not beat the 1 bucket on the corrected personas,
  `READ_EXPLANATIONS_BEFORE_BAND` rises. The residual risk is unchanged in
  kind: band-driven behaviour other than glossing is where a wrong band
  would show, so keep that surface small. One consequence of the
  no-repeat rule: a silent reader in a slow group now accumulates
  `read_explanations` only as fast as genuinely new events arrive, since
  the same gloss is no longer re-delivered in reworded reruns. That is
  correct — the earlier count was partly inflated by repetition. Under the
  probe, the silent persona's measured knowledge is an upper bound on what
  the product taught it (probe feedback is itself a learning event); read
  its `known` count accordingly.
- **Materiality miscalibration** (Monitor + Orchestrator): false positives
  surface low-value updates; false negatives mean a real gap goes uncaught.
  A high-value target for the persona harness. Under the no-repeat rule,
  false negatives also cost more: a missed event is not caught later by a
  recap, because there is no recap. The first human audit (checkpoint 5)
  sided with the judge on all three disagreements and corrected the fixture
  labels; the 0.97 / 0.94 figures are no longer unaudited.
- **External dependency**: Monitor's event detection and Assessor's resource
  search depend on an external search/news tool for real users — its
  availability, rate limits, and result quality bound system reliability.
  The Assessor's no-hallucination fallback (when the tool returns nothing
  useful) should be explicitly tested, not just assumed to hold; the gate's
  `no_fabricated_figure` check is the live test of the reply side.
- **Shared-state race conditions**: a Monitor write arriving mid-session, or
  concurrent updates to the same (user, group) ledger, could produce
  inconsistent records — including double-surfacing an event or losing a
  `surfaced_at` write. Thread extraction firing twice is a real race; the
  fix is that closing a thread is idempotent. Opening two exchanges on the
  same `event_id` from concurrent sessions is the same shape of race on the
  untold queue; the exchange-records-event_id invariant should be enforced
  at the store, not only checked by the caller. Skip resolution is a third
  instance: a later thread close labels an earlier exchange, so two closes
  resolving the same skip must converge (negative evidence wins, and a
  resolved label is not downgraded to `unresolved`).
- **Data durability**: the ledger accumulates indefinitely per (user, group)
  with no reset; losing it loses all cumulative progress. Whatever storage
  is chosen needs a real persistence guarantee, not best-effort/
  in-memory-only. The exchange records are now load-bearing for a second
  reason: they are what marks an event as told. Losing them would resurrect
  every past story as untold.
- **Persona-harness validity risk**: if personas stop producing the signal
  type real usage produces (self-initiated questions and reading behaviour),
  the harness validates a different problem than the one that ships — this
  is how the previous two interaction designs went wrong. With live
  question-generation now settled as the mechanism, the residual risks are:
  personas being unrealistically curious and articulate, which would flatter
  every promotion rule the harness is supposed to police; the **LLM persona
  answering the probe from its own knowledge rather than the memory
  model's** (the calibration gap is the number for this, and it is 0
  offline by construction, so only live runs test it); and the ground truth
  itself now being a model whose constants — the encoding table's S0, the
  ask kernel, the 0.65 recall/recognition ratio and 0.5 define threshold —
  were set from L2 and laboratory literatures, not from users of this
  product. Dynamic truth makes the ledger scored against a simulated reader
  who forgets on a schedule nobody has fit; that is why the static key is
  kept alongside and can be made the gating key. `harness/LEARNING.md`
  lists the constants to fit first when real-user data exists.
- **Proposer eroding human decisions**: a tool that proposes prompt diffs
  from failure evidence will, sooner or later, propose relaxing a rule that
  was bought with a failure it cannot see in this run's evidence. It is
  constrained by every `[x]` checkpoint decision, every changelog
  `**Changed**` block, this spec's non-goals and seven hard rules, and it
  never applies anything — but the constraint set is parsed text, and a
  proposal that argues its way past a constraint is still just an argument.
  Every applied proposal goes through a fresh live run and the gate; a
  proposal that touches a human decision should be read by the human who
  made it.
- **Query/source-judgment risk**: since the Monitor and Assessor rely on
  runtime query formulation and source-trust judgment rather than a curated
  per-domain source list, a poorly-formed query or a low-credibility source
  accepted as trustworthy could quietly degrade materiality assessment or
  gap remediation for a given group. Worth testing on at least one group
  outside the initial synthetic-persona domains to confirm the judgment
  generalizes rather than being implicitly tuned to a handful of
  well-known domains.
