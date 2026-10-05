# Prompt changelog

One entry per prompt version. The version is stamped on every `judgment_log`
row, so a metric change can be traced to the exact revision that caused it —
but the *reason* for a revision lives here, and nowhere else.

This exists because a constraint with no recorded motivation looks arbitrary,
and arbitrary-looking constraints get "simplified" back out by whoever reads
the prompt next. Every rule below was bought with a real failure.

Format: what failed → what changed → what it cost or bought.

---

## `briefing` v6, `concept_evidence` v7, `gap_routing` v5, `thread_reply` v3 + subdomain familiarity — 2026-09-14

**Decided by the human, verbatim:** *"the glossary shouldn't be an exact word
to word matching they should cover the subjects subdomains within the group
that the user is familiar with and use that among other factors to decide how
in depth the harness needs to explain a certain topic for the user to
understand."*

**Failed:** in the checkpoint-5 read-through, Pilar (`pilar_wine`, a wine
professional) was given the same inline definition of `hectolitres per
hectare` as Sam (`sam_beginner`). The per-term ledger was doing exactly what it
says: the string `hectolitres per hectare` had never been observed in anything
she said, so it sat at `unknown`, and `unknown` is glossed at every band. But
her ledger already held `potential alcohol`, `budburst` and `yields` as
correctly used — three viticulture terms — and the per-term ledger has no way
to say what those three imply about a fourth. **The per-term ledger alone
cannot know that an expert in viticulture will hold a viticulture term she has
never been observed using.** The group band cannot say it either: it is one
number for a person who is not one number — Pilar is fluent in viticulture and
may be a beginner in the commercial side of the same group, and the band
averages the two into `developing`.

**What was NOT done, and why:** no exact-match glossary was replaced by a
fuzzy one, and no silence-based state was added. Term identity is unchanged
(`canonical_term` still merges only on containment). Nothing is inferred from
what the user did not say. The change is an *aggregation* of evidence the
ledger already holds, along a new axis.

**Changed (`src/`, 107/107 tests — 94 existing + 13 new in `tests/test_subdomains.py` — offline suite PASS):**

- `concepts.subdomain` (additive migration) and `Concept.subdomain`: each
  term carries the slice of the group it belongs to — a short heading a
  specialist would file it under (`appellation rules`, `viticulture &
  harvest`). A fact about the term, not the person; changes no state, moves
  no band by itself.
- The two calls that already name terms now label them. `concept_evidence`
  v7 and `briefing` v6 both return `subdomains` — on the wire a list of
  `{term, subdomain}` pairs, because the structured-output API requires
  `additionalProperties: false` on every object and a free-form map cannot be
  expressed; in process it is a `{term: subdomain}` dict
  (`judgments.subdomain_map`). Both prompts carry the same tagging rule
  (2–4 word lowercase noun phrase; reuse an existing label where it fits;
  invent only when nothing fits) and are handed the labels already in use so
  labels converge. A verdict without the field is treated as no labels — the
  offline stub never emits it and is unaffected.
- Store: `mark_*` and `note_exposure` take an optional `subdomains` map.
  Labels are canonicalised with `normalize_term`. Churn rule: a label is
  never overwritten with nothing; a *different* label wins only while the
  term has no attested evidence (nothing the user did about it yet). So the
  exposure-era label from a briefing can be corrected by the first call that
  reads what the user did with the term, and is then fixed.
- `UserScope.subdomain_familiarity(group)` → `{subdomain: {known, attested,
  band}}`. `known` and `attested` are the **same SQL predicates as
  `proficiency`** (hoisted to module level so the two cannot drift): confirmed
  always; familiar/explained at `read_explanations >= 2`; provisional never;
  attested = asked / used / got wrong / read twice. Unlabelled terms are
  reported under `(unlabelled)` and count toward no other slice. `band` is
  `config.subdomain_band` — floors of 2 / 4 / 8 known at ratios 0.5 / 0.7 /
  0.8, against the group's 3 / 12 / 25 at 0.35 / 0.6 / 0.8. Lower absolute
  floors because a slice the size of `appellation rules` may only ever
  surface eight or ten distinct terms and would otherwise never read as
  `conversant` however completely it was held — which is exactly the case
  the readout exists for; higher ratio floors because with few terms in the
  denominator a lower ratio would let two lucky confirmations dominate.
- `briefing` v6 receives `subdomain_familiarity` and the rule: *"Depth is
  decided per topic, not per person. Locate the event in the group's
  subdomains. If the user's familiarity in that subdomain is `conversant` or
  `fluent`, write as to a peer in that subdomain: fewer definitions, lead
  with what they would not have predicted, one level more specific. If it is
  `beginner`, define as for the group band. If the event straddles
  subdomains, pitch each part to its own subdomain. `reading_pattern` and
  the per-term ledger still apply on top: a term the ledger marks known is
  never glossed whatever the subdomain says; a `p_informed` prior still
  shifts the overall pitch."* The event's subdomain is not pre-computed; the
  prompt locates it. Every existing rule is intact: one thing only, never
  repeat, `unknown` glossed once, no question, no recap.
- `gap_routing` v5 and `thread_reply` v3 receive the same map, with one
  sentence each: answer at the depth of the user's familiarity in the
  subdomain the question falls in, the per-term ledger still winning on any
  specific term.
- What the extractor gets: the existing label *names* only. No counts, no
  bands. It is the one call that writes to the ledger and is given nothing
  derived from the ledger beyond term states, as before.
- Reading behaviour reaches none of the four contexts. The familiarity map
  is built from ledger evidence; the tests scan every context for the
  reading field names and band strings.

**What it costs / what to watch:** a new label vocabulary that can fork the
way term strings once did (`concept_evidence` v2). The convergence rule and
canonicalisation are the same defences; the harness should report the
number of distinct labels per group and flag near-duplicates. The slice
band is a prior with lower floors than the group band, so it will be wrong
more often in absolute terms — the cost of an error remains one skipped or
one extra definition, and the per-term ledger still overrides it on any term
it has evidence about. If a live run shows `conversant` slices producing
briefings that beginners in that slice cannot follow, raise
`SUBDOMAIN_CONVERSANT_MIN_KNOWN` before touching the prompt.

---

## `concept_evidence` v6 — 2026-09-14 (PROPOSED, not yet measured)

**Failed:** jdg_a0b5e56707ac (concept_evidence v5) read sam_beginner's turn 5 — "so a series c would just be the round after this one?" — as a term deployed in a question and emitted `understood: ['series c']`. The user was checking a guess at the meaning, built from our own turn-6-style gloss in the same thread, immediately after asking what 'valuation' and 'venture funding' mean. That credit is the gated false positive "the system believes they know 'series c'" (sam ledger precision 0.667) and feeds `explained`/`provisional` precision of 0.36/0.22. The v5 asymmetry table and comparison rule cover "is X the same as Y?" but leave self-answering definition checks on the `understood` side.

Evidence: E01: sam_beginner: 'series b' is familiar on 'defined in a briefing they skimmed' but no read-or-skimmed exchange of theirs glossed it; E03: sam_beginner: 'series b' reached 'familiar' but the persona never said anything the system read as evidence about it and no read exchange glossed it.

**Changed:** Definition-checking questions ('so a series c would just be the round after this one?') are `asked_about`, not `understood` (see the diff in `proposals/`).

**What it costs or bought:** expected — sam_beginner ledger precision should rise from 0.667 toward 0.8–1.0 (removing the 'series c' false positive) and Tier 3 `provisional`/`explained` precision should improve by roughly one to three entries pooled; `unknown` recall/precision should be unaffected. Risk — Over-application could push genuine deployments ("is that X or just Y?") into `asked_about`, lowering ledger recall (currently 1.00 on all gated personas) and causing re-glossing of terms the user holds. Tier 2 ledger_recall and the Tier 3 re-ask/question_faithfulness checks would catch that.

**Touches:** concept_evidence v5 changelog rule: a comparison question presupposes both its terms — credit the terms, put nothing in asked_about. Why this is not a reversal: The v5 rule is preserved verbatim and still governs genuine comparisons ("is a Series C the same as a growth round?"), where the unknown is the relationship. The addition carves out the narrower case the rule never contemplated: a question whose own proposed answer is the term's definition, so nothing about the world remains once the word is known. It tightens the boundary rather than reversing it, and the worked example is the live failure.

---

## `briefing` v7 — plain language over inline definitions — 2026-09-16

**Decided by the human, verbatim:** *"it shouldn't have too many literal
definitions it should just talk in simpler language and explain topics more
which can occasionally include a definition."*

**Failed:** the distillation set showed the teacher's style plainly: beginners
got 1.98 inline definitions per hundred words, experts 0.59. The gradient was
right, the instrument was wrong. A beginner briefing built from stacked
"X, meaning Y" glosses is harder to read, not easier, and the learning-model
review found in-text glosses among the least effective gloss formats (§3.3 of
`learning_model.md`). The v3–v6 rule "any unknown term gets defined inline the
first time you use it" was producing exactly that.

**Changed:** the band guidance and the unknown-term rule are rewritten. An
unknown term is still never left as a hole, but the order of remedies is now:
(1) say it in plain words and do not use the term; (2) use it and make it clear
from the surrounding explanation of what is happening; (3) a formal gloss, only
when the reader will need the word by name. Beginner pitch = plainer words,
shorter sentences, more *what and why*; expert pitch = the trade's own words
and no explanations. `explained_terms` now means "terms a reader who lacked
them would now have from your text", by either route, so the ledger's
`familiar` mechanism and gloss-once rule are unchanged. The gloss-once,
never-repeat, one-event and no-question rules are all intact.

**What it changes downstream:** definitions-per-100-words is no longer the
depth measure — it should be low everywhere now. The harness moves to a
plainness bundle (reading grade level, sentence length, domain-term density,
with definition count kept only as a ceiling). The 449-example distillation set
was generated under v6 and embodies the gloss-heavy style; it is regenerated
under v7 before any training.

---

## `briefing` v5 + skip classification and the reading pattern — 2026-09-14

**Decided by the human:** personas should sometimes skip, skim or read, and
the system should try to tell what a skip meant. *"Skipping can often mean the
user already knew the information. But skipping and then indicating later down
the line that they don't know about the subject discussed would mean they just
lazily skipped it and don't have that info."*

**What was wrong before:** a skip was a bucket (`skipped`) and nothing more.
The reading wall was doing its job -- a skip wrote nothing to the ledger -- but
the system also learned nothing from the pattern of a person's skips, so a
reader who skips everything they already know was pitched exactly like one who
skips out of laziness.

**Changed (`src/`, 92/92 tests, offline suite PASS):**

- `exchanges.attention`: a 0-10 continuous readout of the same two proxies as
  `read_quality`. Deterministic, same wall.
- `exchanges.skip_kind`: `unresolved` when a briefing is skipped; resolved
  LATER, at a thread close that produces evidence about the terms that
  briefing defined -- `informed` if the user turned out to hold them (used
  correctly), `lazy` if they turned out not to (asked about them, or got them
  wrong). Negative evidence wins on the same skip: skip-then-ask is lazy
  whatever else they knew. A skip of terms the ledger ALREADY attests
  (provisional/explained/confirmed -- never `familiar`, which came from
  reading) is classified informed at once. No later evidence: stays
  unresolved. **A skip still writes nothing to the ledger.**
- `UserScope.reading_pattern(group)`: counts of informed/lazy/unresolved skips
  and a Beta(1,1)-smoothed `p_informed`. A prior about the person, not a fact
  about any term.
- `briefing` v5: `reading_pattern` enters the briefing context (counts and the
  smoothed share only -- never dwell, scroll or read_quality). Usable only at
  3+ resolved skips: `p_informed` >= 0.7 pitches one band up and defines less;
  <= 0.3 holds the band and keeps definitions; otherwise ignored.

**What this is not:** it is not silence becoming evidence. Every label comes
from something the user said in a later thread (or had already said). It is
the *reading act* being interpreted in the light of that evidence, which is
the inference the human asked for.

**Harness side (in progress, same change):** personas now choose skip / skim /
read from their memory state -- an informed skip when they hold the story's
terms, a lazy skip at a profile-set rate -- and record the hidden intent; the
harness scores the system's attention readout against true depth, the
informed/lazy classification against true intent once evidence exists, and
whether `p_informed` converges to the persona's true informed-skip rate.

---

## `briefing` v4, `interrupt_timing` v4 + the untold-events queue — 2026-09-13

**Decided by the human (checkpoint 4), verbatim:** *"It should never repeat the
story; if there's nothing new to say, don't say anything."*

**Failed:** in the first full five-persona live run, the silent persona was
briefed on the same Rioja harvest story five sessions running (sessions 4–8),
reworded each time. Mechanism: his group stayed flagged `never_engaged` (he
never speaks), so every session raised a topic; the harness had already marked
each new event *surfaced* before the briefing step, so `raise_topic` found
nothing pending and fell back to "recent material events", and the briefing
prompt had an explicit "if nothing material has happened, brief them on
something ongoing" mode. Three permissions to talk with nothing to say.

**Changed:**

- New store query `untold_events`: material events with no exchange written
  from them. It is the ONLY source `raise_topic` draws on. "Told" means an
  exchange records the event as `event_id`, which is now always set (one event
  per briefing, no composing over several). A told event cannot come back.
- `raise_topic` returns `None` when nothing is untold. No fallback to recent
  events, no "ongoing" mode. A pinned event that was already told is refused.
- `Orchestrator.open_session` raises a topic only for a group that has
  something untold, whatever the judgment said about `raise_topic` and however
  overdue the group looks.
- `briefing` v4: "What to raise" rewritten — you receive exactly one untold
  event, you are never called when there is nothing new, there is no recap
  mode. Overlap with `previously_raised` means brief only what is new.
- `interrupt_timing` v4: `raise_topic` is only ever about something new; added
  failure mode 4, *the rerun* ("saying something because a session is open
  rather than because something happened").
- Harness: the runner no longer pre-picks a pending event; the system's own
  choice is under test, and a round with nothing untold is a quiet round, not
  a gap. The `topic_without_events` interrupt case flipped: with nothing
  queued, `should_surface` and `raise_topic` must both be false (previously it
  required `raise_topic` true — that expectation was the bug, written down).
- CLI `session`: the thread loop was still calling the pre-redesign
  `submit_reply`/`opener` API and would have crashed on first use. Now: print
  the briefing, answer questions until a blank line, then close the thread.
  Nothing is printed for a group with nothing untold.

**What it costs:** a user who follows a slow group can open a session and be
told nothing. That is the decision. Offline, this also means fewer threads per
run (the fixture rounds whose event is immaterial are now genuinely quiet
instead of being filled with a rerun), which shortened the scripted personas'
evidence enough that `sam_beginner`'s ledger gate reads n/a offline — noted
under checkpoint 4, not papered over.

---

## `briefing` v3 + the `familiar` ledger state — 2026-09-13

**Decided by the human (checkpoint 3), verbatim:** *"We explained and they read
or skimmed should count as they have familiarity; over time this will grow into
more confidence. One correct use should stop from re-explaining a term."*

**What was wrong before:** two things, one per sentence of the decision.
(1) A term the briefing defined inline was recorded as *exposure only* — zero
inferential weight — even when the reading record said the user had read the
paragraph containing the definition. So the next briefing defined it again, and
the one after that. (2) Pilar used "potential alcohol" fluently in an expert
question and the very next briefing explained it back to her; fixed for
`provisional` in v2, but a term we had *defined and she had read* still got
re-glossed because there was no state to hold that.

**Changed (`src/`, 41/41 tests):**

- New state `familiar` = the briefing **defined** the term (`explained_terms`,
  the model's own list of what it glossed, now stored on the exchange) **and**
  the briefing's `read_quality` was anything but `skipped`. Applied at
  `close_thread`, never by `record_reading`. From `unknown` only; stronger
  states are kept.
- New counter `read_explanations`: +1 per explanation they read, by either
  route (read gloss, or an answer to their own question). Reset with
  `correct_uses` on a revealed misunderstanding.
- **Glossing** (`briefing` v3): `provisional`, `familiar`, `explained`,
  `confirmed` are all no-gloss. One correct use or one read definition ends the
  re-explaining.
- **Band** ("over time"): `confirmed` counts at once; `familiar` and
  `explained` count only at `read_explanations >= 2`
  (`READ_EXPLANATIONS_BEFORE_BAND`, mirroring the two-use rule); `provisional`
  never. A term read once votes neither way. **A single ask no longer moves
  the band by itself** — checkpoint 3 measured `explained` at 0.50 precision
  inside the band, so it now needs the second explanation read.
- A gloss the user then asked about, or on a term they got wrong in the same
  thread, earns no read credit.
- Existing databases migrate (concepts CHECK constraint rebuilt; two columns
  added).

**Why this is not `assumed` coming back:** `assumed` was silence after mere
*usage* — no definition given, no evidence the text was looked at — and it
measured anti-predictive at every threshold. `familiar` needs two positive
acts and needs them twice before it touches the band. It is the one sanctioned
door from reading behaviour into the ledger, and it is narrow on purpose.

**What it costs / what to watch:** a reader who never speaks can now, over
many read briefings, leave `beginner`. The human chose that knowingly. The
harness measures `P(known | familiar)` split by read count (1 vs 2+) — the
exact test that killed `assumed` — and reports the silent-reader crossings. If
the 2+ bucket does not beat the 1 bucket on the rebuilt personas, the
threshold rises.

---

## `thread_reply` v2, `gap_routing` v4 — 2026-09-12

**Failed:** the reply refused a question it had the answer to. Source event
detail: *"obtained a derogation to pick Chardonnay on 6 August at **over 11%
potential alcohol**"*. The briefing written from it omitted the percentage. The
user asked *"what potential alcohol were they picking at that early — still
under 11%?"* and got *"I don't have a real figure for that pick, so I won't
guess at a percentage."* The next briefing, built from the same event, stated
"over 11% potential alcohol" unprompted. Across five user turns in that run the
system said some form of "I don't have that" five times.

**Why it happened:** the no-fabrication rule was working exactly as written on
the wrong inputs. `respond` built the reply context from the briefing text
only; it never saw the `MonitorEvent` the briefing was paraphrased from, and
could not, because exchanges did not record which event they came from. And the
run had search off (`--no-remediation-search`) while `respond` had no search
path of its own, so when a fact genuinely was not held there was no way to go
and get it. A rule that says "never invent" with nothing to draw on collapses
into "never answer".

**Changed:**

- Exchanges carry a nullable `event_id` (single-source briefings only; a
  briefing composed over several events stays unlinked rather than being pinned
  to one at random). `respond` loads the linked event and passes it as
  `source_event`, distinct from `briefing_we_gave`, to both calls.
- `thread_reply` v2: a new *answer from the source material first* section with
  an explicit order of authority — source event, briefing, retrieved source,
  then and only then "I don't have that" — with the live case as the worked
  example, and an instruction to re-check `source_event.detail` before writing
  a refusal. Added: say where a figure came from; general stable knowledge (how
  ripening works) needs no source, a specific figure for a specific pick does;
  and a `lookup` record so the reply distinguishes *I looked and found nothing*
  from *I could not look* from *I don't have that*, and never claims a search
  it did not make.
- `gap_routing` v4: receives `source_event` and routes `small` when the last
  question is answered by the event detail, even when numeric — "anything
  numeric routes large" was about figures we would otherwise assert from
  memory, and a figure in the source detail is not from memory. Without this
  the router would have sent the 11% question to search anyway.
- `respond` searches on a `large` routing via the existing
  `search.discover_events(remediation_focus=...)` path, behind
  `Assessor(reply_search=True)` / `app.build(reply_search=True)`. Default on;
  the harness turns it off for cheap offline runs.

**What it bought:** reproduced live on the exact event and question — the reply
now gives the figure and names the source. On a question the material does not
cover (*"yields must be down too?"*), it searches and either cites what it found
or says plainly that it looked and found nothing. The no-fabrication rule is
unchanged; what changed is what it has to work with.

**Watch:** the router now has a reason to route `small` on numeric questions.
If it starts routing `small` on figures that are *near* the source detail but
not actually in it, the reply will be tempted to reconstruct. The v2 prompt's
re-check instruction is the guard; check live replies that cite a figure
against the event detail they claim it came from.

## `briefing` v1 — 2026-09-08 (replaces `conversation_opener`)

**Failed:** every briefing ended in a question we had chosen, and choosing a
question means encoding what we assume the user already knows. From a live
transcript: *"Does a harvest running this far ahead of normal sound like good
news for the wine, or more like a warning sign to you?"* — unanswerable unless
you already know what fast ripening does to wine.

**Why it mattered:** this is the cold-start failure again, one level up. v2
fixed *withholding the substance*; it did not fix *testing the user*. The
briefing was now honest and the question was still an exam, so the interaction
still put the user in the position of being assessed by a system that had
decided what the interesting question was.

**Changed:** the system asks nothing. `opener` and `reference_answer` are
deleted from the schema — not just discouraged in the prose, because a prompt
rule can be softened by the next editor and a schema field that does not exist
cannot quietly grow back. Added an explicit list of questions-in-disguise (the
invitation line, the offered thread, "hope that helps") and the delete-your-last-
sentence test. Carried over from v2 unchanged: pitch to the band, define
`unknown` terms inline mid-sentence, never invent specifics, never reference
history or any score.

**What it bought:** better evidence, not just a kinder interaction. What someone
chooses to ask locates their knowledge more precisely than an answer to a
question we picked — *"what's a derogation?"* and *"was that a chaptalisation
year?"* come from very different people, and neither is a reaction to our
framing. The cost is that some threads will be silent; silence was already
worthless as evidence, so nothing is lost.

**Watch:** inline definitions now carry weight the closing question used to
hide. An unexplained term at the end of a briefing was previously salvaged by
"let me know if you have questions"; now it is simply a hole. Check live
briefings for terms introduced and left undefined.

---

## `thread_reply` v1 — 2026-09-08 (new)

**Why it exists:** the system stopped asking questions, so the user's own
question became the thing being answered. There was no call for that — the old
design routed a gap to a follow-up paragraph appended to an assessment, which
is remediation, not an answer.

**Rules and what each is for:** answer what was actually asked, not the adjacent
topic (a system that answers the question it prefers is not listening). Never
invent specifics; say plainly when you do not know and prefer a real source for
anything current — inherited from `gap_routing` v2, where the reasoning was that
a fabricated explanation is worse than none because the user cannot know to
distrust it. Do not quiz back, do not append a question, do not hand the
conversation back: the same delete-your-last-sentence test as the briefing.
Never make asking feel expensive — no "as I mentioned", no "good question", no
reference to what they have or have not understood before.

**The bet:** a user asking a question is the best signal in the system, and it
is a signal that can be trained away. If asking ever costs the user something —
a check, a quiz-back, a whiff of being marked — they stop, and the system loses
both its evidence and its point.

---

## `gap_routing` v3 — 2026-09-08

**Failed:** v2 assumed a single reply to a question we had asked. It received
`opener` and `user_reply`, and the concept evidence extracted from that one
reply. Neither input exists now.

**Changed:** takes the whole `thread` and routes on the last user turn. Scope
narrowed to the routing decision itself — `small` vs `large`, where the answer
should come from — because a separate call (`thread_reply`) now writes what the
user actually sees; `explanation` is retained as a sketch for the trace and as a
fallback. Added: a repeated question means our first answer failed, so route
`large` — what we produced from memory has already been tested once and lost.

**Unchanged:** the bias to `large` when torn, and the rule that anything recent
or numeric routes `large` regardless.

---

## harness question templates — 2026-09-12

Not a prompt change, but it belongs in the same log: it is a constraint bought
with a real failure, and it will look arbitrary to whoever reads the template
bank next.

**Failed:** templates slotted any concept into phrasings that presuppose a
number. `arr` and `relegation` both went through *"that's higher than the {b} I
remember -- am I misremembering?"*. It reads fine for ARR and is nonsense for
relegation. Same for *"the {term} figure there is off"* and *"how far below
normal is that on {b}?"*. Two smaller variants: *"does that make it a {c} year
for {b}?"* only parses when `{c}` can characterise a year, and *"last season"* /
*"the rest of the market"* leaked football and finance idiom into wine and
startup threads.

**Why the gate missed it:** `question_faithfulness` scores whether a question is
right about the persona's concept sets — does it hold the term or not — and
never whether the sentence means anything. That is the same error as scoring an
instruction rather than the artifact it produced, which this project has now
made three times.

**Changed:** fixtures declare which of their concepts are `quantities`;
templates that presuppose a number moved into a separate bank drawn only when
the term actually is one. Undeclared means not a quantity, so an unmaintained
fixture degrades to a blander question rather than a nonsensical one. Added
`quantity_phrasing_check` so the class is caught mechanically instead of by
reading transcripts.

**After:** *"Thought relegation worked differently — has that changed?"* and
*"Wasn't the transfer window the other way round"* for non-quantities, while
*"That's higher than the valuation I remember"* and *"Is the enterprise value
number the same story as the arr one?"* still reach the quantities they suit.

---

## `briefing` v2 — 2026-09-13

**Failed:** a term the user had just used correctly was still being defined
back at them. Pilar wrote *"what potential alcohol were they picking at that
early - still under 11%?"*; the next briefing said *"potential alcohol, a
measure of how much alcohol the sugar in the grapes will eventually produce"*.
The v1 rule hedged on `provisional` (one correct use) and glossed it "in three
words" if load-bearing.

**Human decision (checkpoint 3):** one correct use is enough to stop
re-explaining. `provisional` joins `explained`/`confirmed` in the no-gloss set.

**Changed:** the hedge is gone; only `unknown` terms are defined inline. The
safety net is unchanged -- a revealed misunderstanding reverts the term to
`unknown` and it gets defined again.

**What it costs:** a user who parroted a term once without holding it will not
be re-glossed until they slip. Accepted -- the cost is one clarifying question,
versus patronising every fluent user on every briefing.

---

## `concept_evidence` v5 — 2026-09-08

**Failed:** now that every user turn is a question, comparison questions were
being read as ignorance of both their terms. *"Is a Series C the same as a
growth round, or different?"* scored `asked_about: ['series c', 'growth
round']` — so the system would have explained Series C to someone who had just
used it fluently.

**Why it mattered:** the harness rebuild made questions the dominant input
shape, which turned an edge case into the main path. A question presupposes
terms at least as often as it asks about them.

**Changed:** added the rule that a comparison question presupposes both its
terms — what is unknown is the *relationship*, which is usually not a ledger
concept at all. With the test: strip the question to what the user does not
know; if the answer is "how these relate" rather than "what this word means",
credit the terms and put nothing in `asked_about`.

**After (measured on four live cases, 2/4 → 4/4):** the comparison case flips
to `understood: ['series c', 'growth round']`, and a second case improved from
crediting one term to crediting both. Critically it did **not** over-correct —
*"what's a derogation — is it like a chaptalisation exemption?"* still splits
correctly into `asked_about: derogation` / `understood: chaptalisation`, which
is the case that distinguishes a real fix from a blanket rule.

---

## `concept_evidence` v4 — 2026-09-08

**Failed:** v3 read one reply to one question we asked. Both halves of that are
gone — there is no question and no single reply, there is a thread that ends
when the user stops.

**Changed:** reads the whole thread, turns marked `user` / `system`, and
extracts once over all of it. Added explicitly: our own `system` turns are not
evidence about the user (a multi-turn thread leaves far more of our material
lying around to be echoed than a single briefing did); later turns can retire or
create doubt about earlier ones; a long thread of "ok" is not more evidence than
a short one.

**The substantive addition — question asymmetry.** A question tells you where
knowledge stops, and which direction it points depends on whether the term is
what is being asked about or what is being asked *with*:

| Asked | Reading |
|---|---|
| *"what's a derogation?"* | does **not** hold `derogation` → `asked_about` |
| *"was that a chaptalisation year?"* | very likely **does** hold `chaptalisation` → `understood` |

v3 had two sentences on this; it is now a table, a rule, and a worked
both-at-once case, because with no question from us the user's questions are the
primary evidence stream rather than an occasional bonus. The failure mode being
targeted is defaulting every question mark into `asked_about`, which would
systematically under-credit the users who know most.

**Kept intact:** the v3 citation test (quote the words or leave it out — it cut
spurious `understood` credits by 48%), now explicitly scoped to *user* turns;
and the v2 term-form rule (one to three words, lowercase, canonical).

---

## `concept_evidence` v3 — 2026-09-08

**Failed:** the extractor credited understanding for concepts the user never
demonstrated. A test user who knows nothing said *"probably the money's in the
bigger crop tbh"* and was credited with `yields`; *"sounds more like cheaper,
given the price drop too"* was credited with `trading down`. Across the live
run: **42 spurious `understood` entries**.

**Why it mattered:** this is the `assumed` failure returning through a
different door. We deleted "they didn't ask, so they must know it"; this was
"they said something adjacent, so they must know it". Same wrong instinct, new
location — and it corrupts the ledger at the source rather than downstream.

**Changed:** added a citation test. Before writing anything into `understood`,
find the specific words in the reply that demonstrate it; if you cannot quote
them, leave it out. Both live failures included as worked examples, with the
sceptic test: *could you show someone the user's own words and have them agree
the concept was demonstrated?*

**After:** both regression cases credit nothing. Genuine demonstration still
lands — *"picks and shovels… the real constraint is power not chips"* still
yields `picks and shovels` and `power constraints`. Confidently-wrong still
routes to `not_understood`; a question back still routes to `asked_about`.

**Watch:** a broad disclaimer (*"I don't really follow the funding side"*) now
credits nothing where it previously marked a specific concept not-understood.
Probably right — a vague disclaimer is not evidence about a particular
concept — but it is the same inference running in reverse, so worth checking on
the next live run.

---

## `concept_evidence` v2 — 2026-09-08

**Failed:** live extraction emitted descriptive phrases rather than stable
labels — `picks-and-shovels investment thesis`, `AI compute infrastructure
buildout`, `power constraint on datacentre capacity`, `AI infrastructure
investment`. Two of those are the same concept written two ways.

**Why it mattered:** the ledger keys on the term string. Different strings for
one concept means evidence never accumulates and **nothing can ever reach
`confirmed`**. The ledger doesn't look broken — it looks conservative.

**Changed:** added a hard form rule (one to three words, lowercase, canonical
name), using the actual observed failures as the examples, plus the test "would
I emit this exact string again next month, unprompted?"

**After:** `picks and shovels`, `ai infrastructure`, `power constraints`,
`gross margins`, `fluidstack`. Zero labels over three words, zero uppercase,
across a full live run. The verdicts themselves were unchanged — only the
labels.

**Limit found later:** form was fixed, identity was not. `run-rate` and
`annualized revenue` still arrived as separate concepts. Prompt wording can't
fix that; it needed merge-on-write canonicalization in the store.

---

## `interrupt_timing` v3 — 2026-09-07

**Failed:** not a behavioural failure. The verdict field was named
`run_check_in`, left over from the quiz design, and the prompt had grown a
parenthetical apologising for its own field name: *"the field name is
historical; nothing is checked and nothing is tested"*.

**Changed:** field renamed to `raise_topic`; the apology deleted.

**Why it's worth an entry:** stale naming is how a retired design creeps back.
The prompt was actively teaching the next reader a concept the product had
deliberately removed.

---

## `interrupt_timing` v2, `gap_routing` v2, `conversation_opener` v2 — 2026-09-06

**Failed:** the whole quiz-and-score model. A real user was given seven openers
written in the withhold-the-substance style the design implied and could answer
**none** of them — *"What's vintage"*, *"What fee?"*, *"I don't know what spot
ETF flows are"*. The design assumed an already-conversant user with gaps; the
product exists to help someone *become* conversant, so they start out knowing
nothing.

**Changed:** brief before asking. State the news plainly — the briefing *is*
the product — then ask a question about what you just said. Never withhold to
test. Never show a score, level or grade.

**Also bought:** a better signal, not just a better experience. A blank reply to
a withheld opener only tells you the person couldn't reconstruct a fact from a
hint. A reply to a briefing tells you what they *do* with the fact.

---

## `materiality` v1 (amended) — 2026-09-07

**Changed:** removed the reader's knowledge level from the inputs.

**Why:** it was passed and unused, and it was conceptually wrong — an event
does not become more material because the reader happens to be behind on it.
Materiality is a property of the group; being behind is a property of the
person, and is now counted separately.

**Measured after:** P 0.97 / R 0.83 / F1 0.89 on real August 2026 events,
consistent across major (0.83), borderline (0.82) and minor (0.80) — so it
isn't only getting the easy calls.
