# Assistant-register reply style profile — empirical

**Purpose.** The personas in this harness are people who receive an informational briefing from a
service and decide what, if anything, to type back. The previous style profile
(`reply_style_profile.md`) was measured on Hacker News comments — people performing opinions for an
audience — and it produced personas that said things like *"yields must be down too if it's ripening
that fast, heat usually shrivels the bunches."* The human's objection, verbatim:

> "People use agents to get the information they want, they don't offer their own opinions to an
> agent... people don't explain things about the topic they're trying to learn to the agent."

This document tests that claim against real human→assistant conversations, and replaces the forum
profile as the source for how personas *speak*. The forum profile is retained only for its
length-variance finding.

**Short answer up front.** The claim holds. In 80 hand-read user turns that follow an informational
assistant message, **1 offers an opinion** and **0 explain the topic to the assistant** (2 more tell
the assistant something — one a personal disclosure, one a quote — and are counted against the claim
anyway). That is **3 of 80 (3.75%)** for `assert-opinion` + `explain-to-assistant` at the most
generous reading. The mode, by a wide margin, is *asking for more*: `extend` is 21 of 80 and 58% of
the turns that engage with the reply at all. The second most common thing people do after an
informational message is **nothing** — 45.5% of such messages end the conversation.

---

## 1. Source and method

| | |
|---|---|
| Source | WildChat-1M (`allenai/WildChat-1M`, ODC-BY), via the Hugging Face datasets-server rows API |
| Collected | 2026-09-12 |
| Sampling | 80 pages × 100 rows, offsets 0, 10 000, 20 000 … 790 000 (stride 10 000 across the 837 989-row train split, so no single week of 2023 dominates) |
| Rows fetched | **8 000** conversations |
| Conversation dates | 2023-04 → 2024-04 |
| Models the humans were talking to | gpt-3.5-turbo (80%), gpt-4 (20%) |
| Scripts | `_wildchat_harvest.py` (fetch, cache to `_wildchat_cache/rows_*.json`), `_wildchat_filter.py` (rules below, writes `qualifying_turns.json`) |
| Hand sample | `_wildchat_cache/hand_sample_80.json` (80 turns, one per conversation, seed 20260912); labels in `hand_labels_80.json` |

Stdlib only; sequential; 0.6 s delay between pages; every page cached so re-runs cost nothing.

### 1.1 Filter rules

The filter keeps the shape we care about — **a request for information → an informational reply →
what the user typed next** — and drops everything else. The rules, numbered as in
`_wildchat_filter.py`:

- **R1** English at row level and turn level (the kept turn and the assistant message before it).
- **R2** The kept turn must be preceded by exactly `user request → assistant reply`.
- **R3** The *request* must look like an information request: it contains `?` or opens with an
  interrogative / explain-shaped verb (*what, how, why, is, does, explain, tell me, describe,
  compare…*), and it must **not** match the task bank — write/generate/rewrite/paraphrase/translate,
  code/script/python/sql/regex, story/poem/essay/lyrics, email/cover letter/resume, roleplay/act
  as/pretend/you are a, midjourney/prompt for, multiple choice/which of the following/solve/homework,
  enumerated-list requests (*top 10, 5 examples*), fiction hypotheticals (*what if, who would win,
  vs*), quiz shapes (*question 4, options:, calculate, (5 marks)*), *extended essay*, *with
  reference*.
- **R4** The *assistant reply* must look informational: 120–6 000 chars of prose; no code fence and
  no more than three code-shaped lines; not a refusal or an apology/correction; not an enumerated
  list that opens with *Here are 10…* or `1.`; not fiction (*Once upon, Title:, Chapter*, stage
  directions, heavy quoted dialogue); not a letter/email.
- **R5** The *kept turn* must be non-empty, ≤ 120 words, with no code fence and at most one URL, and
  not a verbatim repeat of the request. (Long turns are overwhelmingly a freshly pasted task.)
- **R6** No explicit/sexual content anywhere in the conversation (local keyword screen — the slim
  page cache does not carry WildChat's own `toxic` flag, so this stands in for it).
- **R7** At most three kept turns per conversation.

### 1.2 Funnel and acceptance rate

| Stage | n |
|---|---|
| Rows fetched | 8 000 |
| English | 4 483 |
| After explicit-content screen | 4 240 |
| Assistant messages that follow an information request (R3) | 1 926 |
| … of which the reply is informational (R4) | **1 145** |
| … of which a user turn follows at all | 722 (63%) |
| … of which the turn passes R5/R7 | **507 kept turns** from 315 conversations |

**Acceptance rate: 507 / 8 000 rows = 6.3%**, or 44% of informational replies. The 423
informational replies that ended the conversation are kept as a separate count, not dropped — they
are the zero-turn outcome (§4).

### 1.3 What leaked through anyway

Regex filters cannot read. In the hand sample, 18 of 80 kept turns were still task-shaped (pasted
quiz questions, *"write 3 rivalries…"*, the next heading of a document being drafted, pasted code).
They are classified as `new-task` below and reported, not silently removed; the classes the harness
actually models are computed on the 36 turns that engage with the reply (§2.2).

---

## 2. Speech acts — the hand classification

80 kept turns, one per conversation, drawn at random and read with the request and the reply in
front of me. Classes as specified, plus one I could not honestly fold into the others:

| class | definition |
|---|---|
| `clarify` | asks what a term/part of the reply means |
| `extend` | asks for more on something in the reply (why, how, what about X, what does that mean for Y) — includes imperative forms (*"give more details on…"*) and reformat requests (*"state in one paragraph"*) |
| `check-belief` | states a belief *as a question* for confirmation |
| `acknowledge` | ok / thanks / got it |
| `assert-opinion` | offers an opinion or evaluation, unprompted |
| `explain-to-assistant` | tells the assistant something (about the topic, or otherwise) without asking |
| `new-task` | changes subject or issues a request not anchored to the reply |
| `redirect` *(added)* | repairs their own question — *"no i mean…"*, *"i mean rear windows not the ones near the driver"*, re-asks with a changed word |

### 2.1 Distribution over all 80

| class | n | % of 80 | median words | range | < 10 words | contains `?` | bare single-sentence question | question + context | reuses ≥1 content word from the reply | is the last user turn |
|---|---|---|---|---|---|---|---|---|---|---|
| `new-task` | **44** | 55.0 | 12 | 3–91 | 41% | 27% | 16% | 11% | 41% | 39% |
| `extend` | **21** | 26.2 | 9 | 4–36 | 57% | 48% | 33% | 14% | 43% | 57% |
| `check-belief` | **5** | 6.2 | 11 | 8–13 | 40% | 60% | 40% | 20% | 80% | 20% |
| `redirect` | 4 | 5.0 | 9 | 5–17 | 50% | 50% | 50% | 0% | 0% | 25% |
| `clarify` | 2 | 2.5 | 12 | 7–18 | 50% | 50% | 0% | 50% | 50% | 0% |
| `explain-to-assistant` | 2 | 2.5 | 19 | 8–30 | 50% | 0% | — | — | 50% | 50% |
| `assert-opinion` | **1** | 1.2 | 16 | 16 | 0% | 0% | — | — | 100% | 100% |
| `acknowledge` | 1 | 1.2 | 2 | 2 | 100% | 0% | — | — | 0% | 0% |

(*reuses ≥1 content word* is computed after removing words that were already in the user's own
request, so it measures picking up the *assistant's* words specifically.)

**The human's claim, by number: `assert-opinion` + `explain-to-assistant` = 3 / 80 = 3.75%.** And
the 3 are weaker than they look: the one opinion is an evaluation of the reply (*"Weirds me out a bit
that the vdevs do not have the same number of disks."*), not a claim about the domain; of the two
"explain" turns one is a personal disclosure in a mental-health conversation (*"I have no one to
share my feelings"*) and the other offers a quote (*"This quote sums this up '…'"*). **Zero turns in
80 explain the subject matter to the assistant.** The claim is confirmed.

### 2.2 The 36 turns that engage with the reply

`new-task` is 55% of everything, and that is a real finding about how people use a chat assistant:
they ask the next thing. But the harness persona has no channel for "write me a limerick"; the
question for the responders is *what does someone do when they do respond to the information*. Over
the 36 anchored, non-task turns:

| class | n | share |
|---|---|---|
| `extend` | 21 | **58%** |
| `check-belief` | 5 | 14% |
| `redirect` | 4 | 11% |
| `clarify` | 2 | 6% |
| `explain-to-assistant` | 2 | 6% |
| `assert-opinion` | 1 | 3% |
| `acknowledge` | 1 | 3% |

Anchored turns are **shorter** than the corpus as a whole: median 9 words, 53% under 10 words, 6%
over 20. Of the anchored turns, 44% were the last thing the user said in the conversation.

Of the 44 `new-task` turns, 34 stay in the same broad subject (*"what is batch os"* after *"what is
real time os"*; *"how long do surgical technologist programs take"* after the same question about
medical assistants), 12 of those are genuine adjacent questions rather than template repeats or
leaked tasks. Adjacent questions are the one `new-task` shape a briefing persona could plausibly
produce.

### 2.3 Verbatim examples per class

Sample numbers refer to `hand_sample_80.json`. Turns are reproduced as typed.

**`extend` (21)** — the mode. Nearly always picks up a specific thing from the reply.

- #6 after a list whose point 3 was "Social Stigma": *"Would social stigma persist if everone wore diapers?"*
- #7 after "most are from the Cretaceous": *"Were there any dinosaurs featured in Jurassic Park (1993) from the Triassic period?"*
- #8 after a numbered list of philosophers: *"Kant main ideas 100 words"*
- #10 after "this is the US customary cup": *"In British, how much it is?"*
- #29 after formulas for absolute/relative error: *"Who came up with these formulas?"*
- #33 after "hacked clients": *"What are some of the clients? I want to protect my users when they are reading"*
- #36 after a description of Sisense: *"Do they provide service in China mainland"*
- #37: *"Give more details on why it's still relevant today"*
- #50 after a comparison of feudalisms: *"How were peasants in Imperial China working the land different from serfs under feudalism in Europe?"*
- #61: *"Where could kids find those five Golden tickets . Chapter 5"*
- #63 after kuru/prion disease: *"Why is human diff from other animals (in terms of sickness)"*
- #17 (the long one, 36 words, and the only one carrying a preference): *"Does imgui provide any simlar functionality becouse I really like the way imgui looks? If not how hard do you think it would be to add a small console to it? I know very little C"*
- #67 reformat: *"state in just one short paragraph"*; #72: *"give me the steps"*; #77: *"Explain above skills with examples."*

Regex-found further examples from the other 427 turns (not in the hand sample, not counted above):
*"Why did Hurricane Florence stall"*, *"Why to choose connecting is series then?"*, *"What about
peasants in Japan during the Sengoku Jidai?"*, *"what about pork liver as beef liver is hard to get
hold of for some reason here"*, *"Why is there no largest infinity"*.

**`check-belief` (5)** — the only place people volunteer knowledge, and it arrives as a question.

- #79 after laws on impersonation: *"Basically, if you impersonate people in order to decieve, it's wrong?"*
- #64 after "the heavier wrestler may have an advantage": *"Do you high weight could be also disadvantage?"*
- #59 after item 3 was "The Equilibrium of Planes": *"Could .3 be used to prodict garvitaional waves"*
- #42 pushback: *"Who relatives-languages of language of Cucuteni-Trypillia culture? But isn't Indo-European languages!"*
- #45 (borderline — no `?`; the previous turn had the identical shape and got "Yes, you are correct", so it is a confirmation request by conversational pattern): *"and in modern doo-wop music it still have a 1940s and. 1950s feel"*

Regex-found: *"So the same concept can be applied to NASCAR,right?"*, *"Are you sure it can be
plugged in?"*, *"are you sure this is correct"*.

**`clarify` (2)** — rarer than expected, and note *what* is clarified: the core term of the reply, or
a distinction the reply drew.

- #39 after a reply that used "chronography technique" in every paragraph: *"What is chronography technique in environmental monitoring"*
- #53: *"What does it mean 2ds xl vs 3ds when both have the hinges. What is the 3d functionality?"*

Regex-found: *"what do you mean by explicitly mapping?"*, *"What does 64-bit mean?"*, *"What is her?"*

**`redirect` (4)** — the user fixes their own question. Not in the spec, real, and not modelled by the
responders (a persona's first turn has no prior question to repair).

- #20: *"no i mean how can i put the game into a website or app form with illustrations"*
- #62: *"i mean rear windows not the ones near to drivers"*
- #23: *"What is a respectful dictatorship?"* (after asking about a *respectable* one)
- #71: *"What is the world like 50 years later?"* (re-asks the previous question)

**`acknowledge` (1)**

- #69: *"Than you."*

Regex-found: *"thanks"*, *"thank you"*, *"Thanks man, see you!"* — 4 of 507 by regex, so under 1%
of kept turns. People who are done mostly just stop (§4).

**`assert-opinion` (1)**

- #55 after a pool layout with uneven vdevs: *"Weirds me out a bit that the vdevs do not have the same number of disks."*

**`explain-to-assistant` (2)** — neither is about the subject matter.

- #43: *"I have no one to share my feelings"*
- #66: *"This quote sums this up "You may find fun, glamour, opportunity and excitement on the outside, but when you look at the inside, you find exploitation, dehumanization, danger and misery""*

**`new-task` (44)** — a few, to show the range:

- adjacent: #19 *"What is batch os"*; #41 *"how long do surgical technologist certification programs take"*; #74 *"whats a good diet for weightloss"*
- template repeat: #51 *"Who was the head of state of Russia on November 28th, 1992?"*; #65 *"What is the most popular cat name ever"*
- unrelated: #3 *"questions on datorama vs bi tool"*; #28 *"do you know who is Yuntian Deng"*
- leaked tasks: #13 *"Write 3 rivalries between female, who live in New York…"*; #15 *"so next section Literature Review:"*

---

## 3. Register — the measurable properties, over all 507 kept turns

| property | value |
|---|---|
| words: median / p10 / p25 / p75 / p90 / mean | **11** / 5 / 7 / 18 / 37 / 16.8 |
| under 10 words | **44.4%** |
| under 5 words | 9.1% |
| over 20 words | 21.9% |
| over 35 words | 10.5% |
| contains `?` | 46.2% |
| ends with `?` | 37.9% |
| bare single-sentence question | **32.1%** |
| question + at least one other sentence | 14.0% |
| no `?` at all | 53.8% (84% of those are a single sentence — overwhelmingly imperatives: *describe…*, *explain…*, *give…*) |
| lowercase initial | **37.5%** |
| terminal punctuation present | 51.3% |
| pure acknowledgement (regex) | 0.4% |
| content-word overlap with the assistant's message | median 25%; 70% of turns reuse ≥1 of the reply's content words; 29% reuse half or more |
| … excluding words already in the user's own request | 41.5% of turns reuse ≥1 of the *assistant's* words |
| kept turn is the last user turn of the conversation | 31.8% |

Top openers: *what* (108), *how* (49), *can* (19), *describe* (15), *i* (13), *who* (13), *why* (12),
*explain* (12), *is* (11), *give* (11).

**Own terms or paraphrase?** People reuse the assistant's words. In the anchored classes the reuse
rate is higher than the corpus figure: 80% of `check-belief` turns and 43% of `extend` turns carry at
least one content word that the assistant introduced and the user had not used before (*social
stigma*, *these formulas*, *clients*, *serfs*, *Golden tickets*, *3d functionality*). Paraphrase is
the exception; when it happens it is a garbled repeat (*"prodict garvitaional waves"*), not a
rewording. For the harness this matters twice: a persona should echo the briefing's term rather than
a synonym, and an extractor that matches glossary terms literally is matching what people actually do.

**Bare question vs question + context.** Two thirds of questions are bare (32.1% vs 14.0%). Where
context appears it is a *purpose or a self-disclosure*, never a domain claim: *"I want to protect my
users when they are reading"*, *"I know very little C"*, *"i can't find it anywhere"*, *"because I
really like the way imgui looks"*.

---

## 4. Zero turns

| | n | share |
|---|---|---|
| Informational replies to an information request (R3 ∧ R4) | 1 145 | |
| … conversation ends there — no further user message | **423** | **45.5%** (of the 930 with a resolvable next position) |
| … a next user message exists but fails R5 (long pasted task, code) | 215 | 23% |
| … a kept turn follows | 507 | 55% |
| … a kept turn that *engages with the reply* (§2.2 share applied) | ≈ 228 | **≈ 25%** |

So after an informational message: roughly **45% nothing, 30% an unrelated next request, 25% a
reply about the information**. For a briefing product there is no channel for the middle bucket, so
the honest prior for "does the persona say anything after this briefing" sits between 0.25 and 0.4,
and the previous fixtures (0.62–0.80 for the well-informed personas) were more than twice too chatty.

Continuation: 68% of kept turns were followed by at least one more user turn, but among the
*anchored* turns only 56% were (44% were the user's last message). The earlier fixture
`continue_rate` of 0.30–0.45 is in range and is left alone.

Split by the length of the assistant's message (the briefing analogue):

| assistant message | informational replies | ended the conversation | user median words | user turn contains `?` |
|---|---|---|---|---|
| ≤ 120 words | 298 | 41% | 11.5 | 51% |
| 121–250 words | 349 | 46% | 10 | 44% |
| > 250 words | 283 | **49%** | 12 | 43% |

Longer messages get slightly fewer replies and fewer questions. In the hand sample, replies to long
messages (> 150 words) were *more* often anchored (49% vs 37%) — the long message gives you
something specific to pick up on — but the sample is 27 vs 53 and this should be read as a
direction, not a rate.

---

## 5. Caveats

1. **Population.** These are 2023–24 ChatGPT users, mostly on gpt-3.5, typing into a chat box they
   opened themselves. Our users receive a push briefing they did not ask for in that moment, on a
   subject they subscribed to. The direction of every finding (asking dominates; asserting is rare;
   silence is common) is likely to transfer; the exact rates are not guaranteed to.
2. **Solicited vs delivered information.** Every reply here follows information the user *requested*
   one turn earlier. A briefing is unrequested; the zero-turn rate for it is plausibly higher than
   45%, and the `new-task` bucket plausibly collapses into silence.
3. **Message length.** The assistant messages here are median 181 words (p25 98, p75 276). A
   briefing is shorter. §4's split suggests shorter messages draw slightly more questions; it does
   not suggest they change the *kind* of question.
4. **Filter leakage and filter loss.** 18 of 80 kept turns were task-shaped despite the filter
   (reported as `new-task`); conversely R3's task bank will have dropped some genuine information
   requests that happened to contain *create* or *fight*. Neither error moves the anchored-class
   shares much, because those are computed on the 36 turns that were read and confirmed anchored.
5. **Sample size for the small classes.** `clarify` = 2, `acknowledge` = 1, `assert-opinion` = 1.
   The regex sweep over all 507 puts each of them under 2%, which agrees, but the per-class word
   counts for these three are anecdotes.
6. **No knowledge labels.** WildChat says nothing about what the user already knew. The per-archetype
   tilt in the fixtures (a beginner clarifies more, an expert check-beliefs more) is therefore a
   modelling decision on top of this data, not a measurement, and is stated as such in
   `personas/__init__.py`.

---

## 6. What the responders take from this

Stated here so the fixture numbers can be traced.

| fixture parameter | value | from |
|---|---|---|
| act mix given a turn happens, over {`clarify`, `extend`, `check_belief`, `acknowledge`, `remark`} | ≈ 0.06 / 0.66 / 0.16 / 0.03 / 0.09 (before archetype tilt) | §2.2 with `redirect` removed and the two declarative classes merged into `remark` |
| `remark` never explains the topic | evaluation only (*"the X part is a bit surprising"*) | §2.1: 0 of 80 explain the subject |
| P(any turn) after a briefing with nothing unfamiliar in it | 0.20–0.30 | §4: ≈ 25% engage with the information |
| P(any turn) when an unfamiliar term is present | 0.40–0.55 | modelling decision: an unknown word is the thing a `clarify` needs; kept above the default and below the old 0.75 |
| `react_rate` (non-question turn given no question) | 0.05–0.10 | §2.2: acknowledge + remark ≈ 12% of anchored turns |
| `continue_rate` | unchanged (0.30–0.45) | §4: 56% of anchored turns get a further turn |
| question + context clause | ≈ 0.3 of questions; context is purpose/self-disclosure only | §3 |
| terminal punctuation dropped | ≈ 0.5 | §3: 51% present |
| lowercase as a per-persona trait | ≈ 38% of personas | §3: 37.5% lowercase-initial |
| the persona echoes the briefing's term, never a synonym | always | §3 term reuse |
| questions are short | median 9 words anchored; 53% under 10 | §2.2 |
