# Checkpoints — human review log

The rhythm: **after each live eval run**, the results come back with at most
three decisions attached. Ten minutes. Between checkpoints, bug fixing and
prompt iteration happen without interruption.

The point of writing them down is that open questions get *closed* instead of
accumulating. Anything still open after two checkpoints is either genuinely
blocked or should be decided by default and marked as such.

## What only the human can decide

Three categories. Everything else — bugs, prompt wording, metric plumbing —
gets done and reported, not asked about.

1. **Ground truth.** Whether an event really is something a member of this
   group would know. Currently one researcher's judgment plus Claude's; if
   those labels are wrong, every score built on them is confidently wrong.
2. **Whether the output is any good.** No metric answers "is this briefing
   worth reading". The cold-start design flaw was found this way — by a human
   trying to answer seven openers and failing.
3. **Product shape.** Should it re-raise something you may have missed? Should
   it ever say "you're behind"? Not measurable; these are decisions about what
   the product is.

---

## Checkpoint 1 — 2026-09-08 (first live run, $1.80, 163 calls)

**Brought:** materiality P 0.97 / R 0.83 / F1 0.89 against real Aug 2026
events. Ledger precision 0.00–0.33, contaminated. Two `src/` bugs: unbounded
concept vocabulary fragmenting the ledger (80% of entries unscoreable), and
`concept_evidence` crediting understanding for terms the user never used.

**Decided:**
- Scoring denominator stays over all ledger keys — the unscoreable share *is*
  the finding; narrowing the metric would hide the bug.
- Canonical vocabulary: merge-on-write, not a pre-built per-domain list, which
  would break the "works for an unanticipated group" property.
- `confirmed` stays at two uses. Three makes it unreachable in practice —
  terms rarely repeat that often — so the band would never rise.
- Briefing quality to be judged by what the reply does (substantive? follow-up
  question? term reused correctly?) rather than by asking the human each time.

**Still open — carried to checkpoint 2:**
- [ ] **Materiality labels reviewed by the human** for one domain they follow
      (~12 events, ~10 min). Blocks trusting the 0.97 figure.
- [ ] **Is the briefing actually useful to read?** Asked three times now,
      still unanswered. The engagement metric will inform it but not settle it.
- [ ] **Does `provisional` predict knowing?** Unresolved at n=4 on a
      contaminated denominator. The next run should answer it properly.

---

## Checkpoint 2 — 2026-09-08 (second live run, $1.58, 166 calls)

**Brought:** the first before/after. Canonicalization + `concept_evidence` v3
landed; the noisy persona ran for the first time; briefing engagement measured.

| | before | after |
|---|---|---|
| ledger entries | 118 | 90 |
| scoreable (have ground truth) | 24 (20%) | 22 (24%) |
| spurious `understood` | 42 | 22 |
| false positives, all personas | 35 | 17 |
| `theo_silent` false claims | 2 | **0** |
| materiality P / R / F1 | 0.97 / 0.83 / 0.89 | **0.97 / 0.94 / 0.96** |
| `P(known \| provisional)` | 0.06 (n=31) | **0.27 (n=15)** |
| `P(known \| unknown)` | 0.08 | 0.06 |
| lift | −0.01 | **+0.21** |

**`provisional` survives.** On the cleaner denominator it predicts knowing at
0.27 against a 0.06 base — lift +0.21, where `assumed` scored −0.34 to −1.00.
It is not the same finding and the state should stay. n=15 is still small.

**Decided (harness-side, reported not asked):** noise is now decided
deterministically in the harness and *instructed* to the live persona, with
compliance read back out of the reply text — so the report shows what was
written, not what was requested.

**Needs the human:**
- [ ] **Carried, still unanswered (3rd time): is the briefing actually useful
      to read?** Engagement says 90% substantive but only 17% ask a follow-up
      and 7% reuse a term. That pattern is consistent with briefings that are
      pleasant and inert. A metric cannot settle it.
- [ ] **Carried: materiality labels reviewed for one domain.** Recall moved
      0.83 → 0.94 with no prompt change, which is either run variance on a
      high-effort call or label noise. The 0.97 figure is still unaudited.
- [ ] **The extractor still invents 68 out-of-vocabulary concepts.**
      Canonicalization merges duplicates but nothing constrains what may enter
      the ledger in the first place. This is now the single biggest source of
      error and the fix is a product decision, not a prompt tweak.

**`src/` bugs found this run — both now FIXED:**
1. ~~`canonical_term` does not handle plurals~~ — added naive singularization
   to `_content_tokens`. `billion dollar round`/`rounds` now merge; `gross
   margin`/`operating margin` still correctly don't.
2. ~~`judgment.py` `max_tokens=4096`~~ — raised to 16000. Thinking is billed
   against `max_tokens` on these models, so one `interrupt_timing` call spent
   its whole budget reasoning and returned truncated JSON: a silently degraded
   judgment, not an error. Verdicts run 550-750 tokens, so the headroom is free
   on a normal call.

---

## Checkpoint 3 — 2026-09-12 (first live run on the thread model)

**Brought:** live transcripts for a beginner (`sam_beginner`) and an expert
(`pilar_wine`) on the new no-question thread model, plus two metric bugs found
and fixed, plus the `explained` measurement.

**What works.** Briefings end when the substance ends — no question anywhere.
Sam asks beginner questions in beginner language and learns across the thread
(asked about shares, used "diluted" unprompted in the next turn). Pilar asks
genuinely expert questions and extraction credits her correctly
(`potential alcohol`, `budburst`, `yields`, none of them prompted by us).
The no-fabrication rule holds hard under pressure.

**Two metric bugs, both fixed.** Precision counted terms with *no ground truth*
as wrong beliefs — so the system was penalised for reading concepts the fixture
author never anticipated. `sam_beginner` produced `dilution` and
`oversubscribed` correctly and unprompted, and both scored as errors. In an
open-vocabulary product, unanticipated concepts are the normal case, so absence
of a label now means unscored, not negative. Fixed in both the trajectory
precision denominator and the false-positive list; unlabelled beliefs are
reported separately as coverage.

**The `explained` measurement (the question spec-writer flagged).** Sam asked
what ARR and IPO meant, we explained, and the ledger then counted both as
known — while ground truth (which *does* model learning: it recorded Sam
genuinely learning `valuation` and `venture funding`) says he did not take them
up.

| band | believed known | correct | precision |
|---|---|---|---|
| `explained` + `provisional` + `confirmed` | arr, ipo, valuation, venture funding | 2 | **0.50** |
| `provisional` + `confirmed` only | valuation | 1 | **1.00** |

Small n, and dropping `explained` shrinks the believed set from 4 to 1, so
recall pays for it. But the direction matches the literature (the *asking* is
strong evidence of prior ignorance; the *explaining* is unverified) and matches
the principle applied three times already: our own output must not become their
competence.

**Needs the human:**
- [x] **Does "we explained it" count as "they know it"?** Recommendation was
      no. **Human decided otherwise, 2026-09-12:** *"We explained and they read
      or skimmed should count as they have familiarity; over time this will
      grow into more confidence."* Implemented 2026-09-13 as the `familiar`
      state + `read_explanations` counter: one read explanation stops
      re-glossing, two count toward the band. Explained-by-asking follows the
      same two-explanation rule (it was 0.50 precision counting immediately).
      See `prompt-changelog.md`, `briefing` v3 entry.
- [x] **Should one correct use suppress re-glossing?** **Human: yes** — *"One
      correct use should stop from re-explaining a term."* `briefing` v2.
- [ ] **Carried (4th time): are the briefings actually good to read?** Both
      transcripts are now available to judge rather than described.
- [ ] **Carried: materiality labels unaudited.**
- [ ] **New, from the same review:** persona register was wrong (forum
      assertions, not assistant-user requests). Rebuild from WildChat-1M in
      progress; the measured claim holds — 3 of 80 real post-briefing turns
      assert or explain, the mode is asking for more, ~45% say nothing.

**New bug found, fixed 2026-09-12:** `thread_reply` refused a question it had
the answer to. Pilar asked what potential alcohol they picked at; the reply was
"I don't have a real figure". The figure (over 11%) was in the source event and
appeared in the *next* briefing. The reply call saw only the briefing text, not
the event behind it — so the no-fabrication rule fired on information the system
actually held. Fixed three ways: exchanges now carry the `event_id` they were
written from (`raise_topic(event=...)` records it); `respond` loads that event
and hands it to both gap routing (v4) and the reply (`thread_reply` v2, which
answers from the source material first and says where the figure came from);
and when neither the source nor the briefing holds the answer, `respond` now
searches (`reply_search`, default on, off for cheap offline runs) before saying
so — and says plainly when the search found nothing. Reproduced live on the
exact event and question; the reply now gives the figure. See
`prompt-changelog.md`.

## Checkpoint 4 — 2026-09-13 (all five personas live, rebuilt register + `familiar`)

**Brought:** first live run on (a) personas rebuilt from real human→assistant
data and (b) the `familiar` state. Run `eval-runs/live/all5.*`; full transcripts
in `eval-runs/live/all5_transcripts/`. **The run was cut short: the Anthropic
account ran out of API credits partway through.** Dana lost rounds 4–8, Pilar
round 3 onward, Ade round 5, and 9 of 34 interrupt-timing calls. Every
"I can't get you a reliable answer to that right now" ending and every
`understood=None` close in the transcripts is that failure, not a judgment.
Everything before it is real.

**What the rebuilt personas sound like.** *"what's arr, is that the same as the
run-rate thing?"* · *"ok, no worries"* · *"does 13-15 hl/ha count as low even
by burgundy standards"* · *"Did VAR check either of those goals?"* No
opinions offered, no explaining the topic to the agent. 47 user turns across
25 threads; Theo said nothing eight times.

**The `familiar` measurement, first look (small n, truncated run):**

| persona | familiar terms | at 2+ reads | ground truth on those |
|---|---|---|---|
| theo_silent (knows nothing) | 6 | 2 (`alcohol by volume`, `derogation`) | 0/6 known |
| sam_beginner | 4 | 1 (`funding round`) | `market capitalization` believed, not known |
| dana / pilar / ade (experts) | 7 / 3 / 7 | 0 / 0 / 1 | mostly genuinely known |

Theo is the silent-reader path in the flesh: eight skimmed briefings, zero
words, and two terms already at the band bar. He knows none of them.

**Decided by the human, 2026-09-13:**
- [x] **Credits** added. Full five-persona rerun launched on the corrected
      system (`eval-runs/live/all5b.*`).
- [x] **Theo / the `familiar` threshold:** human took the recommendation —
      leave `READ_EXPLANATIONS_BEFORE_BAND = 2` until a complete run has been
      measured.
- [x] **Repetition:** *"It should never repeat the story; if there's nothing
      new to say, don't say anything."* Implemented structurally: an event is
      briefed at most once (`untold_events` is the only queue; every exchange
      records its event), `raise_topic` returns nothing when nothing is
      untold, and the orchestrator raises no topic for a group with nothing
      untold however overdue it looks. `briefing` v4, `interrupt_timing` v4.
      Side effect worth knowing: offline, the rounds that used to be filled
      with a rerun are now quiet, so `sam_beginner`'s scripted evidence over 8
      rounds no longer reaches the ledger gate's bar (reads n/a → FAIL offline).
      That is the metric meeting a shorter run, not a system regression;
      the live run is the evaluation that matters and the offline structural
      suite (`verify_offline`, 11 checks) passes.

**Carried:** materiality labels unaudited; briefing readability (transcripts
now exist for all five); 8,800 kg/ha Comité Champagne figure unverified.

## Checkpoint 5 — 2026-09-13 (first complete five-persona run on the corrected system)

**Brought:** all five personas, eight rounds each, uninterrupted (`eval-runs/live/all5c.*`,
transcripts in `eval-runs/live/all5c_transcripts/`). System under test: rebuilt
persona register, `familiar` state, never-repeat rule. Run cost: 318 judgment
calls, roughly $3.

**The never-repeat rule held.** 34 briefings, 34 distinct events, zero
re-tellings. Six rounds were quiet (the event was judged immaterial, so nothing
was said). Theo, who last run got the same Rioja story five times, got seven
different stories and one quiet session.

**Materiality:** P 0.97, R 0.94, F1 0.96 against fixture labels. Three
disagreements, all defensible on the judge's side and listed for the human
audit: Ade's VAR non-call (fixture material, judge 35), Sam's monthly funding
aggregate (fixture material, judge 42 for a newcomer while 58 for Dana), Sam's
Emerald AI Series A (fixture not material, judge 56).

**Per-state precision against labelled ground truth:**

| state | correct | precision |
|---|---|---|
| unknown | 22/23 | 0.96 |
| confirmed | 6/8 | 0.75 |
| explained, 2+ explanations read | 2/3 | 0.67 |
| familiar, 2+ explanations read | 2/3 | 0.67 |
| explained, 1 read | 2/8 | 0.25 |
| provisional | 2/9 | 0.22 |
| familiar, 1 read | 2/32 | **0.06** |

The 2-read threshold for the band is doing what it was meant to: below it,
`familiar` and `explained` are mostly not-yet-known; above it (tiny n) they
mostly are. But note what `familiar` at one read means in practice: the terms
we gloss are, by construction, the ones the reader lacks, and one skimmed gloss
did not change that in 30 of 32 labelled cases. Under the current rule that
single skimmed gloss also stops the term being defined again. Ade asked
*"What's a head coach exactly?"* one sentence after the briefing defined it.

**Bugs found, fixed:** `series a` was merged into `series b` by term
canonicalisation (the letter was treated as noise). Fixed with a regression
test; 43/43.

**Persona register:** holds. 38 user turns, all requests or check-beliefs;
no volunteered opinions, no explaining the topic to the agent. One fidelity
miss: Ade asked what a head coach is although the fixture lists it as held.
Sam's `series c` was credited from a correct presupposition in a check-belief
question; ground truth says he does not hold it.

**Needs the human (≤3):**
- [x] **Does one *skimmed* gloss stop re-glossing?** Human, 2026-09-14:
      keep it — a term defined once in a briefing the user looked at is not
      defined again unless the user asks. That is the shipped behaviour
      (`familiar` is a no-gloss state; asking routes through `thread_reply`
      and lands `explained`). Measured cost accepted with eyes open: 30 of 32
      labelled terms were still unknown after one skimmed gloss. The decision was
      "explained and read or skimmed counts as familiarity." Measured: after
      one skimmed gloss the reader still lacks the term 94% of the time, and
      we then never define it again unless they ask. Options: (a) keep as is —
      asking is cheap and the transcripts show people do ask; (b) require
      `read`/`studied`, not `skimmed`, for the no-gloss effect while keeping
      `skimmed` for the counter; (c) no-gloss only at 2 reads. Recommendation:
      (b). It matches the intent (they actually looked at the definition) and
      costs one extra gloss for skimmers.
- [x] **Materiality label audit:** human, 2026-09-14: *"Event selection
      looks good."* The judge's calls stand; the three fixture labels were
      corrected to match (Ade VAR → not material; Sam monthly aggregate → not
      material; Sam Emerald AI → material). First human audit of materiality
      since checkpoint 1; carried item closed.
- [ ] **Carried:** briefing readability (human said "these look good" on
      Dana; four more transcripts to judge); 8,800 kg/ha figure unverified.
