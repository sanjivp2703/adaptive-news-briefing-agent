# Reply style profile — empirical

> **Status (2026-09-12): this is the *forum* register, and it is superseded for persona speech.**
> Every measurement below was taken from Hacker News comments — people performing opinions
> for an audience. Personas in this harness are people who asked an assistant for information
> and got it, and that population does not assert, correct or explain; it asks. The register,
> the speech-act rates (§8.3) and the concrete forms (§8.4) are **no longer used by
> `harness/responders.py`**. They are replaced by
> [`assistant_reply_style_profile.md`](assistant_reply_style_profile.md), measured on real
> human→assistant turns (WildChat-1M).
>
> What is retained from this file, and the only reason it stays: **the length-variance
> finding** (§6, §8.2) — that length is a distribution with real spread and that a persona
> whose length variance is too low is the most likely single failure of the harness.
> `metrics.style_fidelity` still gates on it. Everything else here is history.


**Purpose.** Replace invented assumptions about how partially-informed people react to news with measured
distributions, so that synthetic personas in the eval harness are calibrated against something real.

**The question that prompted this:** the harness currently asserts that *"a well-informed user replies in four
lowercase words."* That was a guess. This document tests it.

**Short answer up front:** the guess is wrong in its specific form and roughly half-right in spirit. Comments
that are both ≤10 words and lowercase-initial are **0.4%** of the corpus (5 of 1286) — essentially absent.
But short-and-knowledgeable *is* real and common: about **42%** of comments under 25 words carry a specific
factual claim, and comments under 25 words are 32% of everything. Terse-but-correct exists; it is a
substantial minority mode, not the default, and it is not lowercase.

---

## 1. Method

| | |
|---|---|
| Source | Hacker News Firebase API (`topstories.json` + `beststories.json`, then `item/<id>.json`) |
| Collected | 2026-09-07 01:24 UTC |
| Story dates | 2026-09-01 → 2026-09-06 |
| Stories sampled | **70** (69 contributed comments) |
| Comments fetched | **1440** raw |
| Comments analysed | **1286** after filters |
| Unique authors | 1094 (so ~1.18 comments per author — near-independent samples) |
| Median comments per story | 21 |
| Story score range | 16 – 2265 |

**Sampling rule.** From the merged top/best story lists, in rank order, I kept stories that were `type:
story`, had an outbound `url` (text-only posts on HN are overwhelmingly solicited-opinion threads), had ≥5
comments, and whose title did not begin with `Ask HN` / `Show HN` / `Tell HN` / `Launch HN` / `Poll:` — i.e.
reactions to news, not solicited opinion. Per story I took up to the first 14 top-level comments plus up to 14
of their direct replies, giving a near-even split of 637 top-level and 649 depth-2 comments.

**Cleaning.** `<p>` → paragraph break, `<a href=X>` → the bare URL, remaining tags stripped, HTML entities
unescaped.

**Filters applied (and what they cost):**

| Filter | Dropped |
|---|---|
| >150 words (essays, not reply-shaped turns) | 127 (8.8% of raw) |
| Link-dump / <8 words of non-URL text | 27 |
| Empty or quote-only | 0 |

The >150-word cut is deliberate and it **truncates the right tail on purpose**. Every length number below is
"length of a reply-shaped HN comment", not "length of an HN comment". The true HN mean is higher than what
I report.

**Fetching was polite:** strictly sequential, ~0.12 s inter-request delay, and every id cached to
`research/_hn_cache/item_<id>.json` so re-runs never re-hit the API. Scripts: `_harvest.py`, `_analyze.py`.
Corpus: `_hn_cache/comments_clean.json`. Hand labels: `_hn_cache/handlabels_60.json`.

**A methodological note that matters for reading section 2.** The keyword/regex rates in section 2 are
**floors, not estimates**. Regex catches a marker only when a person uses one of the phrasings in my list.
Hand-reading 60 comments (section 4) found corrections at 16.7% where regex found 5.7% — a 3× undercount,
because most corrections don't contain the word "actually". Where a hand number and a regex number disagree,
trust the hand number and treat the regex number as a lower bound.

---

## 2. Length distribution — the headline number

n = 1286 reply-shaped comments, word counts:

| min | p10 | p25 | **median** | p75 | p90 | max | mean | sd |
|---|---|---|---|---|---|---|---|---|
| 2 | 13 | 21 | **39** | 69 | 100.5 | 150 (capped) | 49.0 | 35.0 |

Distribution by band:

| words | count | share | |
|---|---|---|---|
| 1–5 | 21 | 1.6% | ▏ |
| 6–10 | 66 | 5.1% | ▍ |
| 11–20 | 216 | 16.8% | █▎ |
| 21–40 | 364 | 28.3% | ██▎ |
| 41–80 | 374 | 29.1% | ██▎ |
| 81–150 | 245 | 19.1% | █▌ |

Top-level comments and replies are close to identical in length, which is mildly surprising and worth
knowing — a reply to another person is not meaningfully shorter than a reaction to the article:

| | n | p10 | p25 | median | p75 | p90 | mean |
|---|---|---|---|---|---|---|---|
| top-level (depth 1) | 637 | 12 | 21 | 42 | 72 | 101 | 50.5 |
| replies (depth 2) | 649 | 14 | 21 | 37 | 66 | 99.2 | 47.6 |

**Read:** the modal HN reply is a short paragraph — roughly 20–70 words, two to four sentences. One-liners
under 10 words are only 6.7% of the corpus. Nothing here supports a four-word default.

Structural companions to length:

- **48.2%** of comments contain a paragraph break (they are multi-paragraph).
- **8.6%** open with a `>` quote-block of what they're responding to.
- **14.1%** contain a link.

---

## 3. Hedging and partial knowledge

Rates per 100 comments. **Keyword-matched — read these as floors.**

| Behaviour | per 100 |
|---|---|
| Uncertainty markers ("I think", "IIRC", "afaik", "pretty sure", "my understanding is") | **13.1** |
| Explicit non-knowledge ("I don't know", "no idea", "TIL", "wasn't aware", "hadn't seen") | **2.3** |
| Correction / contradiction of the article or another commenter | **5.7** (hand: ~16.7 — see §4) |
| A question back instead of an answer | **13.8** |
| Bare reaction / opinion with no factual content | **20.2** (hand: ~30 — see §4) |

### 3.1 Uncertainty markers — 13.1 per 100

The dominant public form is a *hedged assertion*: the commenter states something substantive and attaches an
epistemic disclaimer to it. The hedge is protective armour against being corrected in front of an audience.

> "afaik argon2 should make the GPU less helpful" — *(8 words; the entire comment)*

> "As far as I know, no real progress has been made on alignment, only on convincing humans that the model is
> aligned. We can't even formally define what 'aligned' means."

> "You can opt to build it yourself and exclude anything that is not 2-clause BSD licensed, as some drivers and
> such are **if I recall correctly**."

> "I am wondering how much the spec changes per M-chip iteration? **I think** that after some iterations, the
> spec stays the same, no?"

> "**I believe** this is the planned gallery app. I've already been using it: github.com/IacobIonut01/ReFra"

> "In the post it says 'I'll be making posts as TALA and D2 Studio are released.' so **I assume/hope** so?"

Note the shape of the first one. `afaik argon2 should make the GPU less helpful` is the archetype the harness
was groping toward: eight words, a hedge, a real technical claim, no capital letter. It exists. It's just
rare — see §5.

### 3.2 Explicit non-knowledge — 2.3 per 100

**This is the most important number in the document and it is almost certainly the most distorted.** Saying
"I don't know" on HN buys you nothing; people who don't know a thing mostly just don't comment. The 2.3%
measures *what a public forum rewards*, not *how often humans lack knowledge*. See §6.

Where it does appear, it's usually a preamble that licenses a question or an opinion, not a standalone
admission:

> "TIL: bardcore is a music genre. Thank you." — *(8 words)*

> "Could someone point me to some background on this whole incident or write a quick one? **I don't know who
> they are or what's going on** but it seems important."

> "**I wasn't aware of this group**; en.wikipedia.org/wiki/Autistici/Inventati is their wiki article, but it is
> a little light on details."

> "**I am not familiar with** the standards of publishing in machine learning, but as someone trained in a
> mathematics background, this paper seems relatively light on details and heavy on exposition. Is that
> typical? […] Not trying to be snarky, just trying to understand."

> "**I don't know much about robotics engineering**, so I just wanted to give kudos to the Robocurve team on
> this write-up."

Observe: in four of these five, the admission of ignorance is immediately *repaired* — with a question, a
link the person went and found, or a compliment. A bare, unrepaired "no idea" essentially does not occur in
this corpus. That is a property of the venue, not of people.

### 3.3 Correction / contradiction — 5.7 per 100 by keyword, ~16.7% by hand

Corrections are the highest-status move on HN and they are everywhere once you read rather than grep. They
range from seven words to a paragraph:

> "Albania and Bosnia share a small border" — *(7 words, flatly contradicting the map in the article)*

> "Title is misspelled -- 'ue' for 'eu'. In a neologism, some nuisance." — *(12 words)*

> "Is it? **Pangram disagrees** and human-made photos imply at least some human effort." — *(13 words)*

> "Purported white-hat hackers. The implication is that these people are presumably describing themselves as
> such, but it may not actually be true."

> "**Actually**, in the Wiki incident OpenAI tried to cover up, the agents tried to socially-engineer the humans
> of that forum by impersonating their forum's mod. (From collusion.wiki: 'They use some tricks […] they make
> an account that appears to be the same as the administrator's username, except it uses a nearly identical
> Cyrillic е character […]')"

> "Now I'm really curious if they're actually plasma. Many of the stations were built long after LCDs became
> the norm (the first parts of the Thomson-East Coast Line only opened in **2021**), so it's likely
> mislabelling on the website's part."

The corrective register has a distinctive tell: it very often cites a *specific* countervailing item — a
date, a named tool, a URL, a Cyrillic homoglyph. Corrections are where the specificity lives.

### 3.4 Question back instead of an answer — 13.8 per 100

Questions skew short. In the hand-classified sample, question-back comments had a median of 31 words against
a corpus median of 39.

> "Time for bitcoin classic++?" — *(4 words)*

> "Why do they say it's white hat hackers?" — *(8 words)*

> "Does USB work already or is that a 2027 item?" — *(10 words)*

> "I couldn't find any minimum order information. Where did you see that?" — *(12 words)*

> "Does anyone know of good alternatives that also block ads? Seems Quad9 doesn't." — *(13 words)*

> "how is it a rug pull? if it's an inside job, doesn't that just make it theft?" — *(17 words, lowercase throughout)*

Two sub-types are worth separating for persona purposes. There is the **genuine information request** ("Where
did you see that?"), and there is the **rhetorical challenge** dressed as a question ("Why wouldn't bringing
up an OS on new hardware be necessary? […] Do you think Apple should be funding this work out of revenue?").
The second is a correction wearing a question's clothes and behaves like a correction — longer, more
confident, more specific.

### 3.5 Bare reaction with no factual content — 20.2 per 100 by keyword, ~30% by hand

The largest single non-knowledge category. These are short: median 19 words in the hand sample.

> "Some big caveats but still amazing progress!" — *(7 words)*

> "Get this guy some VC funding stat!" — *(7 words)*

> "This is such a breath of fresh air" — *(8 words)*

> "Some real copium lmao ;)" — *(4 words)*

> "These people write in gibberish. They are high on their own supply." — *(12 words)*

> "how is anyone going to afford robots if no one can work" — *(12 words)*

> "$320m, not a bad haul. More than enough to buy yourself a pardon if you get caught." — *(17 words)*

This is the closest HN gets to the "four lowercase words" intuition — and notably, when people *do* write
very short, this is what they're doing. **Short correlates with content-free more than it correlates with
expertise.** That is the opposite of the assumption the harness encoded.

---

## 4. Register

Rate per 100 comments:

| Feature | per 100 |
|---|---|
| Contractions ("don't", "it's", "I'm") | **47.7** |
| First-person opening ("I…", "My…", "IMO…") | **20.2** |
| Missing terminal punctuation | **18.0** |
| Profanity / slang / interjection ("lol", "meh", "gonna", "sucks", "crap") | **7.2** |
| Sentence-initial lowercase | **5.8** |
| — of which ≤10 words | 5 comments total (0.4% of corpus) |

**Read:** HN's register is *informal-but-typed-properly*. People use contractions freely (nearly half of
comments) and open in first person, but they capitalise and punctuate. Lowercase-initial writing is a
minority dialect used by a small set of authors, and it is not correlated with brevity — of 75
lowercase-initial comments, only 5 are under 10 words.

Missing terminal punctuation (18%) is mostly a specific artefact: comments that end on a bare URL, or on a
quoted block, rather than genuine sloppiness.

> "There is a bit more mature project that does the same: https://selfprivacy.org" — *(no terminal period; ends on the link)*

> "The Netherlands and France share a border on the island of Saint Martin" — *(genuinely unpunctuated)*

Lowercase examples, for calibration on what the dialect actually looks like:

> "simply using the installer from https://asahilinux.org — the devs strongly discourage single-booting"

> "we need to be supporting asahi if we want these things. their funding has fallen off."

> "i love the idea of recurse center, love the idea of the hub"

> "swiss tables were invented by engineers working at google's zurich office, hence the name / im surprised
> that go, a programming language also from google, wasn't using them!"

That last one is instructive: 62 words, entirely lowercase, missing an apostrophe in "im", and it delivers a
precise piece of institutional history. Lowercase does not imply terse and does not imply uninformed.

Slang/profanity examples:

> "lmao, PR description says: > Fixes a number of small issues picked up during LLM scans"

> "Eh, honestly, 'entity gets nasty legal letter, entity stops doing thing, entity resumes doing thing after
> its own lawyers say, "yeah, nah"' is a fairly common pattern."

> "*Privatized profit. The ~210mil € of public funding wasn't private. It is now I guess, lol. EDIT: Title has
> been updated so my comment seems out of context."

---

## 5. Knowledge display shape — hand-classified

60 comments drawn at random (`random.Random(20260906)`), read individually and given one primary label. Full
labels in `_hn_cache/handlabels_60.json`.

| Class | n | share | median words | mean words |
|---|---|---|---|---|
| **demonstrates-specific-knowledge** | 24 | **40.0%** | 39.5 | 47.3 |
| **reacts-without-content** | 18 | **30.0%** | 19 | 28.8 |
| **corrects** | 10 | **16.7%** | 66.5 | 68.0 |
| **asks-question** | 7 | **11.7%** | 31 | 27.3 |
| **admits-gap** | 1 | **1.7%** | 29 | 29 |

Sampling error on n=60 is large — roughly ±12 points on the 40% figure at 95% confidence. Treat these as
"about 40 / 30 / 17 / 12 / 2", not as precise values. The ordering is robust; the exact percentages are not.

Illustrations of each class, chosen to span the length range:

**demonstrates-specific-knowledge**
> "Many of them, especially the smaller hard-case versions, have a space to write date of first-use." — *(16 words)*

> "CarPlay Ultra adds information such as speed, fuel/charge level, climate control, and tire pressure. Hard to
> imagine legal issues around that." — *(36 words with its second paragraph)*

> "The Asahi installer does this automatically, it guides you through sizing. You run it in a macOS terminal. By
> default, Asahi will be the default boot option, and to get to macOS you hold the power button on boot until
> it says 'Loading startup options…' I have two M1 pro asahi macbooks, and I only use macOS for VirtualDJ or
> AirPlay." — *(61 words)*

> "A good historical nugget is that the Americans got a huge leg up in the space race by importing the Germans
> who worked on the V-2 program. […] https://en.wikipedia.org/wiki/Operation_Paperclip"

**reacts-without-content**
> "Time for bitcoin classic++?" — *(4 words)*

> "I enjoy hearing that they were ran on medium effort." — *(10 words)*

> "Reverse-engineering Apple's custom silicon is basically the modern equivalent of repairing a spaceship while
> it's actively launching. Incredible work by the Asahi team!"

**corrects**
> "Albania and Bosnia share a small border" — *(7 words)*

> "This is what I came to the comments for. The post didn't mention price a single time. It's not that
> expensive to retrofit CarPlay into any vehicle, and a 166% delta is hard to explain away with a single
> feature that can be retrofitted later."

**asks-question**
> "Tangential question: do quantum researchers benefit from LLMs in any way?" — *(11 words)*

> "Codeberg seems to focus on OSS whereas pushin seems to focus on enterprise/commercial uses?" — *(15 words)*

**admits-gap** — the single instance in 60, and note that even it is bundled with knowledge:
> "Pleasantly surprised to see dpreview still running, **I thought they had shut down**. It was a fantastic site
> back in the day. **Not sure how it fares these days**."

One clean admission of a stale mental model in sixty comments. In a private conversation this would be one of
the most common moves there is. This gap between 1.7% here and what a private register would produce is the
single largest correction to apply — see §6.

---

## 6. The terseness question, answered directly

**Q: is terse-but-correct actually how informed people write, or did I invent that?**

**A: you invented the specific form; the general phenomenon is real but is a minority mode, not the default.**

Three measurements:

**(a) The literal claim — "four lowercase words" — is false.**

| | count | share of corpus |
|---|---|---|
| ≤5 words | 21 | 1.6% |
| ≤10 words | 87 | 6.7% |
| ≤10 words **and** lowercase-initial | **5** | **0.4%** |

Five comments in 1286. If personas default to this, roughly 99.6% of generated replies will be off-distribution.

**(b) Comments that clearly demonstrate specific knowledge are, on average, normal-length or slightly longer
than average.** From the 24 hand-classified knowledge-demonstrating comments:

| min | p10 | p25 | median | p75 | p90 | max | mean |
|---|---|---|---|---|---|---|---|
| 14 | 18 | 26 | **39.5** | 59 | 86 | 142 | 47.3 |

Against a corpus median of 39, knowledge-demonstrating comments sit at 39.5 — statistically
indistinguishable. **Demonstrating knowledge does not make people shorter, and it does not make them
longer either.** 4 of 24 (17%) were under 20 words; 9 of 24 (37.5%) were under 30.

The regex-based specificity proxy run over the whole corpus points the other way (median 56 words, only 8.8%
under 20 words) — but that proxy is biased, because detecting specificity by looking for proper nouns and
figures mechanically favours long text. The hand number is the one to trust.

**(c) Read from the other direction: among *short* comments, how many are knowledgeable?** I hand-read a
second sample of 40 comments of ≤25 words:

- **17 of 40 (42.5%)** carried a specific factual claim — a named product, a figure, a prior event, a
  correction of fact.
- Comments ≤25 words are **32.0%** of the corpus.
- So roughly **13–14% of all comments are both short (≤25 words) and specifically knowledgeable.**

Specimens, all real, all under 25 words:

> "afaik argon2 should make the GPU less helpful" — *(8 words)*

> "They were just bought for $7B and rely on Discord?" — *(10 words)*

> "Title is misspelled -- 'ue' for 'eu'." — *(12 words)*

> "The announcement only mentions DoH and not DoT... I guess both are going away?" — *(14 words)*

> "> overlapping circles and rectangles / Just hull() two circles. It's a cheap operation in 2D." — *(14 words)*

> "I like the mermaid sequence diagrams more. But D2 for everything else. No GitHub integration yet though." — *(17 words)*

> "This is what the 'Allow Chrome sign-in' setting is for and is turned off by the post author." — *(18 words)*

> "There are many of us who don't need public DNS records and rely on local-only DNS records accessible from
> outside via Tailscale/WireGuard." — *(23 words)*

**My read.** Short-and-knowledgeable is common — roughly one comment in seven — and it has a recognisable
shape: it is a *single* claim, usually one sentence, frequently a pointer ("this is what X is for", "only
mentions DoH not DoT"), frequently hedged with a two-letter marker, and almost always properly capitalised.
What it is *not* is the default: the modal informed reply is 20–70 words and carries two or three
propositions.

The failure mode your original assumption would produce is real but subtle. Personas written to "reply in
four lowercase words" would not sound like informed people being efficient; they would sound like the
**reacts-without-content** class, which is where the genuinely short comments actually live (median 19 words,
and the shortest things in the corpus). Brevity in this corpus is a much stronger signal of *having nothing
specific to say* than of expertise.

---

## 7. Caveats — read before using any number above

### 7.1 HN comments are performative; private replies are not

This is the load-bearing caveat. Every comment here was written for an audience of thousands of strangers who
can downvote it, and for a permanent public archive attached to a pseudonym the author cares about. The
persona harness models something different: a person replying privately when a system raises a news item
with them. That is a different speech act with a different cost structure, and several measurements above are
distorted in *predictable directions*.

| Measurement | HN value | Direction of bias vs. private reply | My estimate of size |
|---|---|---|---|
| Median length | 39 words | **Inflated** | Large. Private median plausibly 10–18 words; call it a 2–3× deflation. |
| Multi-paragraph rate | 48.2% | **Inflated** | Very large. Private replies are rarely multi-paragraph; I'd guess under 10%. |
| Explicit "I don't know" | 2.3 / 100 | **Deflated, severely** | Very large. Admitting ignorance costs status publicly and costs nothing privately. Private rate plausibly 10–20×, i.e. 15–30 per 100. |
| Corrections | ~16.7% | **Inflated** | Moderate. Correcting a stranger publicly is a status move; correcting a friend is mildly rude. Halve it. |
| Hedging *rate* | ≥13 / 100 | Roughly comparable, maybe slightly deflated | Small. |
| Hedging *form* | elaborate ("correct me if I'm wrong", "my understanding is") | **Inflated in length** | Large. Public hedges are defensive armour written out in full; private hedges compress to "think so", "iirc", "pretty sure?", or a bare question mark. |
| Sentence-initial lowercase | 5.8% | **Deflated, severely** | Very large. In private messaging lowercase is the majority register for many people. Do not port 5.8% into a persona. |
| Missing terminal punctuation | 18% | **Deflated** | Large, for the same reason. |
| Question back | 13.8 / 100 | Slightly deflated | Small–moderate. Asking is cheaper privately than publicly. |
| Bare reaction | ~30% | **Deflated** | Moderate. Privately, "huh" and "ugh, figures" are complete and adequate replies; publicly they get downvoted, so they're suppressed here. |

**Do not present the numbers in §2–§5 as private-message numbers.** They are public-forum numbers. §8 is
where I apply these corrections; §8 is what should be dropped into a persona prompt, not §2.

There is also a category HN *structurally cannot measure*: the non-reply. People who saw the story and had no
reaction, or didn't read it, leave no trace in a comment corpus. If a persona archetype should sometimes
respond with disengagement, no number in this document constrains how often — it must be set by judgement.

### 7.2 Upvote survivorship inflates everything

I took the first 14 top-level comments per story, which on HN are the *highest-ranked* ones. Ranking rewards
articulateness, specificity, and length. The genuinely low-effort comments are disproportionately below my
cut. This compounds the performativity bias in the same direction: real HN is shorter and dumber than this
corpus, and private conversation is shorter and dumber again.

### 7.3 The >150-word filter changes the shape by design

127 comments (8.8% of raw) were dropped as essays. This is correct for the purpose — we want reply-shaped
turns — but it means the reported mean (49.0) and p90 (100.5) are artefacts of a truncated distribution, not
properties of HN. Do not compare these figures to unfiltered HN statistics elsewhere.

### 7.4 Domain skew

The 70 stories are overwhelmingly tech: operating systems (Asahi Linux on M3, NetBSD), security (Chromium
sandbox RCE, GrapheneOS), crypto (a $320M Liquid Federation theft), AI/LLM discourse, retro computing (6502,
Commodore 64), developer tooling (Vidact, D2, OCaml). There is a thin seam of adjacent material — a Babylonian
lamb stew recipe, Singapore MRT displays, a German rocket launch, US Census Bureau politics — but essentially
nothing on sport, celebrity, local news, health, or personal finance. **How people react to a football
transfer or a drug approval is not measured here.** If persona archetypes cover non-tech domains, these
distributions are being extrapolated, not observed.

### 7.5 HN commenters are not a general population

Self-selected, technically literate, heavily English-native or English-fluent, skewed US/EU, skewed male,
skewed toward software professionals, and — crucially — **selected for having an opinion worth typing**. The
1094 unique authors are people who chose to write. A general population contains many people who would read
the same story and say nothing at all, or say something confidently wrong with no hedging whatsoever. The
hedging rate in §3.1 in particular should be read as "how often *people who write for a critical technical
audience* hedge", which is likely an upper bound for the general public.

---

## 8. Recommended persona style parameters

These are the HN measurements with the §7 corrections applied. Where a value is extrapolated rather than
measured I mark it **[est]**; where it comes straight from the corpus I mark it **[measured]**.

### 8.1 Global register rules (all archetypes)

Drop these into a persona system prompt as-is:

- Write 1–3 sentences by default. Reach for a paragraph break only when you have two genuinely separate
  points — under 10% of replies. **[est; HN measures 48% multi-paragraph, which is a public-writing artefact]**
- Use contractions. Roughly half of all replies should contain one. **[measured: 47.7%]**
- Open in first person about a fifth of the time ("I think…", "I saw…", "My read is…"). **[measured: 20.2%]**
- Capitalisation is a **per-persona trait, not a per-reply coin flip.** Assign each persona a fixed register
  at creation: about 25% of personas write lowercase-initial always, the rest capitalise always.
  Never mix within one persona. **[est; HN's 5.8% is a public-forum floor, and the corpus shows the trait
  clusters by author]**
- Terminal punctuation may be dropped, especially when the reply ends on a link, a name, or a fragment —
  around 30% of replies. **[est; HN measures 18%]**
- Light slang and interjections ("eh", "huh", "lol", "meh", "figures", "gonna") in ~15% of replies. Profanity
  in under 5%. **[est; HN measures 7.2% for both combined]**
- Never write a preamble ("Great question!", "Thanks for sharing"). It appears essentially nowhere in the
  corpus outside of thread-author replies.
- When citing something specific, cite it *bare* — "Thomson-East Coast Line opened 2021", not "According to
  the Wikipedia article on the Thomson-East Coast Line, it opened in 2021."

### 8.2 Word-count targets by archetype

Target the **median and the interquartile range**, not a fixed length. A persona that always writes 15 words
is as wrong as one that always writes 60.

| Archetype | median | p25–p75 | p90 | Notes |
|---|---|---|---|---|
| **Well-informed / domain expert** | **16** | 8–34 | 60 | HN's knowledge-demonstrating median is 39.5 **[measured]**; deflated ~2.5× for private register **[est]**. Crucially, keep the *spread*: this persona should sometimes fire off 8 words and sometimes write 50. |
| **Partially informed** | **13** | 6–26 | 45 | The core archetype. Shorter than the expert because hedging privately is short, not because they write less overall. |
| **Barely informed / reacting only** | **8** | 4–16 | 28 | HN's reacts-without-content median is 19 **[measured]**, deflated **[est]**. This is where genuinely tiny replies belong. |
| **Disengaged** | **5** | 2–9 | 14 | Not measurable from HN (§7.1); set by judgement. |

**Hard rule:** at least 20% of every persona's replies should be under 10 words, and at least 15% should be
over 35 words. A persona whose length variance is too low is the most likely single failure of this harness.

### 8.3 Behaviour rates by archetype

Per 100 replies. HN's measured rate is given for traceability; the recommended value applies §7's corrections.

| Behaviour | HN measured | **Well-informed** | **Partially informed** | **Barely informed** |
|---|---|---|---|---|
| Hedged assertion ("iirc", "pretty sure", "think so") | ≥13 | **20** | **35** | 15 |
| Outright gap admission ("no idea", "hadn't seen this") | 2.3 | **8** | **20** | **30** |
| Corrects the item or the sender | ~17 | **20** | **8** | 2 |
| Asks a question back instead of answering | 13.8 | **15** | **25** | 15 |
| Bare reaction, no factual content | ~30 | **15** | **30** | **60** |
| States a specific fact, figure, or prior event | ~40 | **55** | **25** | 5 |

Rows do not sum to 100 — a single reply routinely does two of these (a hedged assertion that is also a
correction; a gap admission followed by a question).

**The gap-admission row is the most important change from the status quo.** HN measures 2.3 per 100. The
recommendation for a partially-informed persona is 20 per 100 — nearly ten times higher — because the 2.3
is a measurement of what a public forum punishes, not of how often people don't know things. If the harness
ships personas that admit ignorance at HN rates, the eval will be measuring a population of show-offs.

### 8.4 Concrete forms to use

**Hedges — prefer the short forms.** The corpus is full of "As far as I know" and "correct me if I'm wrong";
those are public-forum armour. Privately, use: `iirc`, `afaik`, `think so`, `pretty sure`, `wasn't that…?`,
`something like that`, a trailing `?`, or `— though I might be off`.

**Gap admissions — allow them bare.** HN never leaves one unrepaired; a private persona should, often:
`no idea`, `hadn't seen this`, `nope, missed that one`, `first I'm hearing of it`, `wasn't aware`.
Repair it (with a question or a guess) only about half the time.

**Corrections — one clause, one specific.** Model them on `Albania and Bosnia share a small border` and
`Title is misspelled -- "ue" for "eu"`, not on the 90-word rebuttals. A private correction that runs three
paragraphs is out of register.

**Questions back — keep them under 15 words.** `where'd you see that?`, `is that the same one as last year?`,
`does that actually change anything?`

**Bare reactions — these are complete replies.** `finally`, `called it`, `ugh`, `well that was inevitable`,
`huh`, `figures`. Do not append an explanation.

### 8.5 What to hold as an open question

Two things this harvest does not settle, and which should be flagged rather than guessed at again:

1. **The non-reply rate.** How often a real person simply doesn't engage with a raised news item is
   unmeasurable from a comment corpus (§7.1). It needs a different data source or an explicit product
   decision.
2. **Non-tech domains.** Every distribution here comes from tech/startup news read by software people
   (§7.4–7.5). If a persona covers sport, health, or local news, the §8.2 and §8.3 tables are extrapolation.
   The register rules in §8.1 are likelier to transfer than the rates are.
