# Knowledge-inference validity: literature review

> **Status: historical.** This review assessed an earlier four-state ledger design (`unknown` / `assumed` / `explained` / `confirmed`).
> Its findings led to deleting the `assumed` state; the current states are defined in `docs/spec.md`.

**Scope.** Two design questions for the Conversational Competence Agent:
(1) whether `surfaced_at` should discharge the "you're behind on N things" count, and
(2) whether the four-state concept ledger (`unknown` / `assumed` / `explained` / `confirmed`)
plus derived proficiency band is defensible against the learning-science and
learner-modelling literature.

**How to read the confidence labels.** *Supported* = multiple converging peer-reviewed
findings bear directly on the claim. *Weakly supported* = the direction is right but the
literature does not license the specific operationalisation (threshold, count, binarity).
*Contradicted* = the literature points the other way. Where I extrapolate past what a
source actually claims, it is marked **[my inference]**.

**Blunt caveat up front.** Almost none of this literature is about *this* problem. It is
about classroom learning, L2 vocabulary acquisition, and intelligent tutoring systems with
graded item responses. This system has none of those: no items, no grades, no
ground truth, and a loss function where being wrong costs one redundant sentence. So the
literature can tell you reliably which inferences are *unsound*, and it can tell you what
better-instrumented systems do instead — but it cannot tell you what threshold to use here,
and anyone who claims otherwise is extrapolating. I have tried to be explicit about which
of the two I am doing at every point.

---

# Question 1 — Does "delivered" count as "known"?

## 1.1 The literature is unambiguous: exposure is not encoding

This is the least contested part of the whole review.

**Levels of processing.** Craik & Lockhart's (1972) framework holds that retention is a
function of the *depth* of processing at encoding, not of exposure. Shallow processing
(orthographic, phonemic — i.e. the words went past your eyes) produces a fragile trace that
decays rapidly; semantic/elaborative processing produces a durable one
([Craik & Lockhart 1972, PDF](http://wixtedlab.ucsd.edu/publications/Psych%20218/Craik_Lockhart_1972.pdf)).
Craik & Tulving (1975) demonstrated the effect experimentally: the same stimulus, presented
identically, produced very different recall depending only on the orienting task
([summary](https://www.themantic-education.com/ibpsych/2021/10/04/key-study-levels-of-processing-craik-and-tulving-1975/)).
The framework has been criticised (depth is hard to define independently of the retention it
predicts), but the core empirical claim — *presentation alone does not determine retention* —
has never been seriously challenged.

**The testing effect.** Roediger & Karpicke (2006) had students either reread a passage or
practise recalling it. After one week, the retrieval group retained ~61% versus ~40% for the
rereading group — *and the rereading group was more confident*
([Test-Enhanced Learning, Psychological Science](https://journals.sagepub.com/doi/10.1111/j.1467-9280.2006.01693.x);
[Karpicke & Roediger 2007, JML PDF](https://learninglab.psych.purdue.edu/downloads/2007/2007_Karpicke_Roediger_JML.pdf)).
Dunlosky et al.'s (2013) large review for *Psychological Science in the Public Interest* rated
practice testing and distributed practice as **high utility** and rereading and highlighting —
the two techniques students actually rely on most — as **low utility**
([Dunlosky et al. 2013](https://journals.sagepub.com/doi/abs/10.1177/1529100612453266);
[PDF](https://gwern.net/doc/psychology/spaced-repetition/2013-dunlosky.pdf)).

A brief that a user reads is, at best, a reread. It is the *low-utility* condition.

**Mere exposure does not deliver comprehension.** Zajonc's (1968) mere-exposure effect is
frequently misread as "exposure produces familiarity produces knowledge." What it actually
shows is that repeated exposure increases *liking* — an affective, not epistemic, outcome —
and Zajonc's later subliminal work deliberately established the effect *without* conscious
recognition of the stimulus
([Zajonc 1968, PDF](https://www.psy.lmu.de/allg2/download/audriemmo/ws1011/mere_exposure_effect.pdf)).
**[my inference]** For this design, mere exposure is a *hazard*, not a support: it predicts
that repeatedly surfacing a term will make the user feel warmer toward it without
understanding it any better — exactly the failure mode the `assumed` state is exposed to.

## 1.2 Seeing something makes people believe they know it

**Illusion of explanatory depth.** Rozenblit & Keil (2002) showed that people rate their
understanding of everyday mechanisms far above what they can actually produce, and that the
illusion is *specific to explanatory knowledge* — it is much weaker for facts, procedures and
narratives ([Rozenblit & Keil 2002, Cognitive Science](https://onlinelibrary.wiley.com/doi/abs/10.1207/s15516709cog2605_1);
[PDF](https://time.com/wp-content/uploads/2015/02/ioed_proofs.pdf_1.pdf)).
The mechanism is confusing fluent recall of surface features with grasp of mechanism.

**Fluency masquerades as learning.** Carpenter et al. (2013) had participants watch the same
content delivered fluently or haltingly. Fluency raised *judgments of learning* substantially
and actual test performance not at all
([Carpenter et al. 2013, PBR](https://pubmed.ncbi.nlm.nih.gov/23645413/); replicated by
[Toftness et al. / Carpenter et al. 2016](https://pubmed.ncbi.nlm.nih.gov/26844368/)).
A well-written LLM brief is the fluent condition by construction.

**People cannot judge their own comprehension.** Meta-analytic work puts *relative*
metacomprehension accuracy — a reader's ability to tell which passages they understood well
versus badly — at around **r ≈ +.27**, i.e. weak
([Dunlosky & Lipko 2007, *Current Directions*](https://journals.sagepub.com/doi/abs/10.1111/j.1467-8721.2007.00509.x);
[Prinz et al. 2019 meta-analysis, *Educational Research Review*](https://www.sciencedirect.com/science/article/abs/pii/S1747938X19304270)).
Kruger & Dunning (1999) add the asymmetry: the least competent are the least able to detect
their own incompetence, because the same knowledge is needed to perform and to self-assess
([Kruger & Dunning 1999](https://sites.lsa.umich.edu/sasi/wp-content/uploads/sites/275/2015/11/krugerdunning02.pdf)).
Since this product's stated primary case is *cold start* — the user knows nothing — it is
operating precisely in the region where self-assessment and self-report are least reliable.

## 1.3 Product precedent: "displayed" is a famously bad proxy for "consumed"

There is unusually good documented data here, and it is all in one direction.

| System | What it measures | Documented failure |
|---|---|---|
| Email open pixel | Image fetched | Apple Mail Privacy Protection (iOS 15, Sept 2021) pre-fetches pixels on delivery. Apple Mail is ~half of consumer opens; roughly **half of all reported opens in a typical programme are now machine-generated**, and per-recipient open rates for Apple Mail users approach 100% regardless of behaviour. ESPs now advise abandoning open rate as an engagement metric in favour of clicks ([Postmark](https://postmarkapp.com/blog/how-apples-mail-privacy-changes-affect-email-open-tracking), [Validity](https://www.validity.com/blog/case-closed-the-mystery-of-declining-email-open-rates/)) |
| Display advertising | Ad served | The industry found "served" so meaningless it built a whole second metric. The IAB/MRC viewable-impression standard requires ≥50% of pixels in view for ≥1 second — and is explicit that this measures *opportunity to see*, not seeing ([MRC Viewable Ad Impression Measurement Guidelines, PDF](https://www.iab.com/wp-content/uploads/2015/06/MRC-Viewable-Ad-Impression-Measurement-Guideline.pdf)) |
| Social link sharing | Link posted / shared | Gabielkov et al. (2016), 2.8M shares across BBC/CNN/NYT/Fox/HuffPo: **59% of shared links were never clicked by anyone**. People propagate content they have not opened ([ACM SIGMETRICS 2016](https://dl.acm.org/doi/abs/10.1145/2896377.2901462)). Replicated at scale on Facebook by [Sundar et al. 2024, *Nature Human Behaviour*](https://www.nature.com/articles/s41562-024-02067-4) |
| Web pages | Page view | Nielsen (2008): users have time to read **at most ~28% of words on an average page visit, ~20% more likely**; earlier eye-tracking shows F-pattern scanning rather than reading ([NN/g, How Little Do Users Read?](https://www.nngroup.com/articles/how-little-do-users-read/)) |
| Messaging read receipts | Message rendered | Hoyle et al. (CHI 2017): **over two-thirds of recipients reported deliberately avoiding opening messages** to suppress the receipt, some feigning never having seen them. "Seen" is actively gamed by users because it carries obligation ([CHI 2017](https://dl.acm.org/doi/abs/10.1145/3025453.3025925); [PDF](https://www.cs.oberlin.edu/~rhoyle/papers/hoyle-chi17.pdf)) |
| LMS / e-learning | Activity viewed | The standards themselves refuse to conflate these. ADL's xAPI vocabulary keeps `experienced`, `attempted`, `completed`, `passed`, and `mastered` as **distinct verbs**, and SCORM keeps `completion_status` separate from `success_status` ([xAPI SCORM Profile](https://github.com/adlnet/xAPI-SCORM-Profile/blob/master/xapi-scorm-profile.md); [xAPI verbs](https://xapi.com/blog/deep-dive-verb/)) |

The xAPI row is the most directly instructive precedent: an entire industry standard exists
whose central design decision is that *experienced ≠ completed ≠ mastered*, and that a system
must record which one it actually observed.

## 1.4 Recommendation for Question 1

**The question as posed conflates two different ledgers, and the fix is mostly semantic, not
mechanical.**

`surfaced_at` answers *"has the system discharged its obligation to tell you?"* The concept
ledger answers *"do you know this?"* These are different questions with different truth
conditions, and the spec is already right to keep them in separate records
(`MonitorEvent.surfaced_at` vs `ConceptLedgerEntry.state`). The literature above does **not**
say that a delivery counter is invalid — it says that a delivery counter must never be read as
a knowledge counter.

Concretely:

1. **Keep `surfaced_at` as the discharge condition for the backlog count. Do not gate it on
   evidence of reading.** Requiring proof of consumption to clear the queue means the count
   never goes down for a skimming user, which produces a permanently growing "you're behind on
   47 things" — the exact anxiety/obligation dynamic Hoyle et al. documented around read
   receipts, and a strong driver of disengagement. The spec's own framing ("the user simply
   hasn't been told about them yet") is the correct semantics for this field. *Strength: this
   is a product-design judgement, weakly supported by the read-receipt literature, not a
   finding.*

2. **Rename the user-facing count so it claims delivery, not knowledge.** "3 new things since
   you were last here" is true. "You're behind on 3 things" asserts a fact about the user's
   head that `surfaced_at` cannot support. This is free and removes the entire class of error.
   *Strength: follows directly from §1.1–1.3.*

3. **Enforce a hard one-way wall: `surfaced_at` must never write to the concept ledger.** This
   is the load-bearing recommendation. If surfacing a brief containing a term can increment
   that term's observation count toward `assumed`, then §1.1–1.2 apply in full and the system
   is inferring knowledge from an ad-impression-grade signal. See Question 2 / `assumed`, which
   is where this actually bites.

4. **Add a second, cheap timestamp: `engaged_at`.** This system has an advantage that feeds,
   emails and ads do not — it is *conversational*. A user reply that references or responds to
   an item is real positive evidence and costs nothing extra to capture, because the Assessor
   already parses replies. In xAPI's terms, distinguish `experienced` (surfaced) from
   `interacted` (replied about). Use `surfaced_at` for the backlog count and `engaged_at` for
   anything that touches knowledge. **[my inference]** — no source recommends this specific
   two-timestamp split; it is the obvious application of the xAPI/viewability precedent to this
   architecture.

**Evidence strength for the overall recommendation: strong for the negative claim** (surfacing
is not knowledge — this is about as settled as applied cognitive psychology gets),
**moderate for the positive claim** (that `surfaced_at` is nevertheless the right discharge
condition for a *delivery* counter — that rests on product reasoning and one CHI paper about
obligation dynamics, not on a body of evidence).

---

# Question 2 — Is the knowledge-inference scheme defensible?

## 2.0 The one framework that fits this problem directly

Before the per-state assessment: most of the learning-science literature is a poor fit here
because it assumes graded item responses. **Clark & Brennan's (1991) grounding theory is a
much better fit**, because it is about exactly this — how a speaker infers that a listener has
understood, from conversational behaviour alone
([Grounding in Communication](https://philpapers.org/rec/CLAGIC);
[overview](https://en.wikipedia.org/wiki/Grounding_in_communication)).

Clark & Brennan's key claim is that participants look for **positive evidence of
understanding**, and that such evidence comes in a rough strength ordering:

1. **Continued attention** (weakest) — the addressee stays in the conversation
2. **Initiation of the relevant next turn** — they respond appropriately
3. **Acknowledgement** — "right", "got it"
4. **Demonstration** — they paraphrase or reformulate
5. **Display** (strongest) — they produce the content themselves

This maps onto the four states almost cleanly, and it is the strongest available *support* for
the design's basic shape: the ledger's ordering (`assumed` < `explained` < `confirmed`) is a
recognisable, principled ordering of grounding evidence. The problems are all in the
*thresholds* and in the *collapse of a graded ordering into discrete "known" categories.*

Note what Clark & Brennan do **not** say: they do not say silence is evidence. Their weakest
level is *continued attention plus a relevant next turn* — participation, not absence of
objection.

---

## 2.1 `unknown` (default state, and reversion on revealed misunderstanding)

**Verdict: default = supported. Reversion-as-implemented = weakly supported.**

Starting every concept at not-known is standard and correct. BKT's prior parameter `P(L0)` is
routinely estimated low for new skills, and starting a cold-start user at `beginner` matches
both the spec's stated product premise and normal learner-modelling practice
([Corbett & Anderson 1995](https://link.springer.com/article/10.1007/BF01099821)).

The **reversion** rule is where it gets thin. Reverting all the way to `unknown` on a single
revealed misunderstanding treats one negative observation as conclusive. BKT explicitly does
not do this: its **slip** parameter — `P(S) = 0.10` in Corbett & Anderson's defaults — exists
precisely because a learner who *has* mastered a skill still errs about 10% of the time
([BKT parameter overview](https://www.cs.williams.edu/~iris/res/bkt/)). A hard revert also
creates a ratchet in the wrong direction: it is trivially easy to fall back to `unknown` and
comparatively hard to climb back to `confirmed`.

That said, **asymmetry here is cheap and probably right for this product [my inference]**: the
cost of wrongly reverting is one redundant explanation; the cost of wrongly retaining
`confirmed` is a brief the user cannot follow, which is the stated core failure mode. So the
crude rule is *defensible on decision-theoretic grounds even though it is not what the models
do* — but it should be recorded as a deliberate asymmetric-loss choice, not as an inference.

**Better-supported alternative:** demote by one level rather than reverting to the floor
(`confirmed` → `explained` → `unknown`), and keep the evidence trail so a second contradiction
completes the fall. This approximates a slip-tolerant update without introducing probabilities.

---

## 2.2 `assumed` — term used ≥3 times, never questioned

**Verdict: weakly supported as a *provisional prior*. Contradicted if treated as "known" —
including by counting it in the ratio that produces the proficiency band.**

This is the state the spec itself flags as the weakest, and the literature agrees emphatically.

### Why people don't ask, even when lost

- **They almost never ask, full stop.** Graesser & Person (1994) measured question-asking rates
  directly: in classrooms, students asked **1.3–4 questions per hour in total**, which works
  out to roughly **0.11 questions per student per hour** in an average class of ~27. In
  one-to-one tutoring the rate is about **240× higher**
  ([Graesser & Person 1994, AERJ](https://journals.sagepub.com/doi/10.3102/00028312031001104);
  [PDF](https://gwern.net/doc/psychology/spaced-repetition/1994-graesser.pdf)).
  This is the single most damaging finding for the `assumed` state: the base rate of asking is
  so low that *absence of a question carries almost no information about comprehension* — it is
  the overwhelmingly likely outcome under both hypotheses.
  The 240× tutoring multiplier is genuinely good news for this product's *form factor* (a 1:1
  chat is the high-question-rate condition), but it does not rescue the inference, because the
  rate is still low in absolute terms and the tutoring number includes plenty of
  non-comprehension questions.

- **Social inhibition suppresses the question.** Miller & McFarland (1987) formalised
  pluralistic ignorance with the classroom case as their canonical example: students who don't
  understand look around, see composed faces, conclude they alone are lost, and stay silent for
  fear of embarrassment. Their experiments support the underlying asymmetry — people believe
  fear of embarrassment explains *their own* silence but not others'
  ([Miller & McFarland 1987, JPSP](https://www.researchgate.net/publication/232428926_Pluralistic_Ignorance_When_Similarity_is_Interpreted_as_Dissimilarity);
  [overview](https://en.wikipedia.org/wiki/Pluralistic_ignorance)).
  **[my inference]** This mechanism is *weaker* with an AI than with human peers — there is no
  audience — but the whole premise of this product is a user who is anxious about looking
  ignorant in front of a social group, i.e. exactly the personality/context in which
  status-protective silence is most likely to generalise. That is a hypothesis, not a finding.

- **They don't know what they don't know.** Kruger & Dunning (1999) — the metacognitive deficit
  is largest at low competence, which is this product's default user
  ([Kruger & Dunning](https://sites.lsa.umich.edu/sasi/wp-content/uploads/sites/275/2015/11/krugerdunning02.pdf)).
  Metacomprehension accuracy at r ≈ .27 means a user often cannot tell that a term went past
  them un-understood, so no question is *generated to be suppressed*
  ([Prinz et al. 2019](https://www.sciencedirect.com/science/article/abs/pii/S1747938X19304270)).

- **They're skimming.** Nielsen's ~20–28%-of-words finding and the Gabielkov 59%-never-clicked
  finding (§1.3) both say the modal interaction with delivered text is not reading. A term in a
  brief may not have been *perceived*, let alone processed.

### Is 3 the right number?

**There is no empirical basis in the literature for 3 — or for any number — as a threshold for
comprehension-from-silence, because no literature studies that inference.** The nearest
empirical anchor is L2 incidental vocabulary acquisition, which studies exactly the shape
"how many encounters in context before the word is known":

- Webb (2007) manipulated encounters at **1, 3, 7 and 10** and found significant gains in word
  knowledge *at each step up*, with the 10-encounter group still significantly ahead of the
  7-encounter group on 4 of 10 measures. Crucially, he also found that number of encounters
  drives **knowledge of form** more than **knowledge of meaning**, which is driven more by
  context quality
  ([Webb 2007, *Reading in a Foreign Language*](http://www2.hawaii.edu/~readfl/rfl/October2008/webb/webb.html);
  [PDF](https://files.eric.ed.gov/fulltext/EJ815123.pdf)).
- The general finding in that literature is that meaningful acquisition needs on the order of
  **8–20+ encounters**, and even then acquisition is partial
  ([Cambridge meta-analysis of incidental vocabulary learning](https://www.cambridge.org/core/journals/language-teaching/article/how-effective-is-second-language-incidental-vocabulary-learning-a-metaanalysis/E38E3468FD2090B1FA3051051DE8E70C)).

**[my inference]** Read against Webb, "3 exposures" lands roughly where a learner has begun to
recognise the *form* of a term and has not reliably acquired its *meaning* — which is arguably
the correct calibration for what "assumed" should mean in a *conversational fluency* product
(recognising a term when a founder says it, and not visibly flinching, is genuinely most of the
value), but is emphatically not calibration for "knows what it means." Note also that these
studies used deliberate reading of a controlled text — a much stronger exposure condition than
a term appearing in a skimmed brief.

### The partial defence

Clark & Brennan (§2.0) do license *something* here: if the user, after 3 exposures, is still
participating and producing relevant next turns, that is level-1/level-2 positive evidence of
grounding. It is real, and it is the weakest kind. That justifies `assumed` as a **weak
provisional prior that changes framing** ("as you know, the pre-seed round…") — it does not
justify counting it as a known concept.

**Better-supported alternative:**

1. **Do not count `assumed` in the numerator of the proficiency band**, or weight it heavily
   down (e.g. `confirmed` = 1.0, `explained` = 0.5, `assumed` = 0.15). Otherwise a user who
   never speaks is promoted to `fluent` purely by being talked at — which is a live and
   self-reinforcing failure (see §5).
2. **Raise the bar from "3 exposures" to "3 exposures spanning ≥2 sessions,"** so it is at least
   distributed exposure ([Cepeda et al. 2006](https://augmentingcognition.com/assets/Cepeda2006.pdf)).
3. **Prefer *cheap positive evidence* over *counted silence* wherever it is available.** The
   Assessor already reads every reply; a term the user *responded around* is worth more than
   three the user was shown. **[my inference]**
4. **Consider making the ledger visible.** The open-learner-model literature (Bull & Kay's SMILI
   framework; Kay's scrutable models) finds that exposing the model to the learner and letting
   them inspect or contest it both improves model accuracy and supports metacognition
   ([Bull & Kay, SMILI, IJAIED 2007](https://www.researchgate.net/publication/228738851_STUDENT_MODELS_THAT_INVITE_THE_LEARNER_IN_THE_SMILI_OPEN_LEARNER_MODELLING_FRAMEWORK_TECHNICAL_REPORT_580);
   [Bull 2020, *There are Open Learner Models About!*](https://dl.acm.org/doi/abs/10.1109/TLT.2020.2978473)).
   A one-line "I've been assuming you know *cap table* — correct me if not" converts the
   weakest inference in the system into direct evidence at near-zero cost. The spec currently
   says the ledger is internal; this is the cheapest single change with literature behind it.

---

## 2.3 `explained` — the user asked, and it was explained

**Verdict: weakly supported. The *asking* is excellent evidence; the *explaining* is not.**

Split the two halves, because they have very different evidential value.

**The question is strong evidence — of prior ignorance.** Given Graesser & Person's base rates,
a user who actually asks has done something rare and informative. It reliably tells you they
*did not* know the term. It tells you nothing about the after-state.

**The explanation is the low-utility condition.** Being told something is comprehension-time
processing, not retrieval. Roediger & Karpicke's rereading arm (~40% at one week vs ~61%)
and Dunlosky et al.'s low-utility rating for rereading both apply
([Roediger & Karpicke 2006](https://journals.sagepub.com/doi/10.1111/j.1467-9280.2006.01693.x);
[Dunlosky et al. 2013](https://journals.sagepub.com/doi/abs/10.1177/1529100612453266)).
Carpenter et al. (2013) adds the sting specific to an LLM: a *fluent* explanation raises
confidence without raising learning ([PBR 2013](https://pubmed.ncbi.nlm.nih.gov/23645413/)).
An LLM's explanations are maximally fluent by construction, so this system is structurally
biased toward producing the illusion of understanding in both parties at once.

There is also **no verification step at all** in the current design — the state transitions on
the system's own action, not on any observation of the user. That makes `explained` the only
state in the ledger set by something the *system* did rather than something the *user* did.

**Better-supported alternative:** treat `explained` as a **pending** state, not a known one, and
promote it only on subsequent evidence. The cheapest version costs one clause: after
explaining, ask the user to apply it once ("does that match how you'd read the Anthropic round
then?"). A correct application is Clark & Brennan level 4–5 evidence and converts
`explained` → `confirmed` in the same turn. This is retrieval practice, it is free in a
conversational medium, and it is the single highest-value-per-token change available.
*Strength: the underlying testing-effect literature is strong; the specific application is
**[my inference]**.*

---

## 2.4 `confirmed` — one correct unprompted use

**Verdict: weakly-to-moderately supported. It is the strongest signal available, but one
observation is not enough by the standards of every model that quantifies this.**

**What supports it.** Productive use is genuinely the top of validated knowledge scales. The
Vocabulary Knowledge Scale (Wesche & Paribakht 1996) is a 5-level instrument whose **level 5 is
"I can use this word in a sentence"** — above recognition, above stated meaning — with reported
test-retest reliability r = .89
([VKS description](https://jalt-publications.org/tlt/departments/myshare/articles/628-using-modified-version-vocabulary-knowledge-scale-aid-vocabular);
[critical analysis, Language Assessment Quarterly 2009](https://eric.ed.gov/?id=EJ867001)).
Clark & Brennan rank *display* (producing the content yourself) as the strongest grounding
evidence. So the design's *ordering* is right and its choice of unprompted productive use as
the gold standard is well-founded.

**What undercuts it — the guess parameter.** BKT's `P(G)` exists precisely to model "correct
response without knowledge." Corbett & Anderson's defaults are **P(guess) = 0.30, P(slip) =
0.10**, and their operational mastery threshold is **P(mastery) ≥ 0.95**
([Corbett & Anderson 1995](https://link.springer.com/article/10.1007/BF01099821);
[parameter defaults](https://www.cs.williams.edu/~iris/res/bkt/)).

**[my inference — this is my arithmetic, not a published result]** Applying the standard BKT
update from a low prior `P(L₀) = 0.20`, with no learning transition (we are *assessing*
pre-existing knowledge, not teaching), a run of correct observations gives:

| Correct observations | P(knows) |
|---|---|
| 0 | 0.20 |
| 1 | 0.43 |
| 2 | 0.69 |
| 3 | 0.87 |
| 4 | **0.95** ✓ |

So under the field's own default parameters, **one correct use gets you to ~43% confidence, and
it takes about four to reach the conventional mastery bar.** Calling it `confirmed` after one is
roughly a 2× overstatement of confidence. (Change the priors and this moves — that is the
point: the number is parameter-dependent, and "1" is a choice, not a derivation.)

**And the guess rate here is plausibly *worse* than 0.30.** In a tutoring system the guess
parameter covers multiple-choice luck. In this system the user has just been handed a brief
containing the term, in context, with correct usage modelled for them. Immediate mimicry of
freshly-supplied phrasing is not just possible, it is the expected behaviour — it is what
"conversationally competent" users of the product are being *trained* to do. That is the
parroting concern in the task description, and it is well-founded. **[my inference]**

**Better-supported alternative — the novel-context criterion.** Rather than counting to 2 or 4,
require that the confirming use be **decoupled from the supply**: a correct unprompted use
either (a) in a session *after* the one in which the term was supplied, or (b) applied to an
event or referent the system did not just provide. This is the transfer criterion, and it
distinguishes retrieval from echo. Webb's finding that context quality drives *meaning*
knowledge while repetition drives *form* knowledge supports treating a novel context as worth
more than a repetition ([Webb 2007](https://files.eric.ed.gov/fulltext/EJ815123.pdf)).
One use in a novel context is defensibly `confirmed`; one use in the same turn as the
explanation is not. This adds one boolean to `ConceptLedgerEntry`, not a probability model.

---

## 2.5 No decay

**Verdict: split. Defensible for `confirmed`; weakly supported to contradicted for `assumed`
and `explained`. The spec's *reasoning* for deleting decay is sound even where the *claim* is
not.**

**What supports "no decay."** Bahrick's permastore work is the strongest evidence that
"once known, known" is not naïve. Tracking Spanish learned in school across 50 years and 733
subjects, he found retention **declined exponentially for the first 3–6 years and then remained
essentially unchanged for up to 30 years** before a final age-related decline — and that the
size of the permastore fraction was predicted by *level of original training*, with negligible
measurable rehearsal effects
([Bahrick 1984, *JEP: General*](https://www.semanticscholar.org/paper/Semantic-memory-content-in-permastore:-fifty-years-Bahrick/57f7bca4dbd92caba99c660b58f6d5013760ac35);
[Bahrick & Phelps 1987, PDF](https://gwern.net/doc/psychology/spaced-repetition/1987-bahrick.pdf)).
So: *well-learned* material genuinely does plateau. A concept a user has used correctly,
unprompted, in their own life is a decent candidate for that regime.

**What contradicts it.** The permastore result is conditional on the level of original
learning — which is exactly what `assumed` and `explained` lack. For shallowly encoded material
the standard forgetting curve applies, and the distributed-practice meta-analysis (839 effects
across 317 experiments) shows retention is jointly determined by study spacing and retention
interval, not fixed
([Cepeda et al. 2006, *Psychological Bulletin*](https://augmentingcognition.com/assets/Cepeda2006.pdf)).
Modern scheduling models make decay the central object: FSRS represents each item by
**Difficulty, Stability, Retrievability** with `R = exp(−t/S)`, and is the default scheduler in
Anki since v23.10, benchmarked on 500M+ reviews as needing 20–30% fewer reviews than SM-2 for
equal retention ([Anki FAQ](https://faqs.ankiweb.net/what-spaced-repetition-algorithm);
[open-spaced-repetition](https://github.com/open-spaced-repetition)). Bahrick's own curve has a
3–6 year *decline* phase before the plateau; "no decay" is wrong for the first several years of
any concept's life.

**Where I think the spec is actually right.** The spec's argument is not "memory doesn't fade,"
it is "we deleted decay because we were using it as a proxy for *staleness*, and staleness is
now directly countable from unsurfaced events." That is a good decomposition and the literature
does not object to it. **[my inference]** In a current-events domain there is also a third thing
neither memory model covers: a concept can become *wrong* rather than forgotten (the referent of
"the Series B" changes; a protocol is deprecated). Neither Bahrick nor FSRS speaks to that, and
a decay model would not have caught it either.

**Better-supported alternative — do the cheapest possible thing:** don't build a decay model;
just **stop treating age as irrelevant for the weak states.** A `last_observed` timestamp
already exists in the schema. A rule as crude as "an `assumed` entry not re-observed in N
sessions is re-treated as `unknown` for framing purposes" captures most of the value at zero
modelling cost, and applies decay exactly where the evidence for it is strongest (shallow
encoding) and not where it is weakest (`confirmed`).

---

## 2.6 The derived proficiency band

**Verdict: supported — this is the best-justified decision in the design, *conditional on* the
constraint that it is only a fallback prior. But it inherits whatever error is in `assumed`.**

Three reasons the coarse band is fine:

1. **The loss function is tiny and symmetric-ish.** The spec's own argument — "the cost of an
   error is one unnecessary explanation or one clarifying question" — is a correct
   decision-theoretic argument, and it is the argument that licenses coarseness. Precision has
   value only in proportion to the cost of imprecision.
2. **Continuous estimates require data this system doesn't have.** Elo/IRT-based learner models
   get their precision from many graded, difficulty-calibrated responses; Pelánek's review of
   Elo in adaptive educational systems highlights its self-correcting behaviour *given a stream
   of scored attempts* ([Pelánek 2016, *Computers in Human Behavior*](https://www.fi.muni.cz/~xpelanek/publications/CAE-elo.pdf)).
   This system has no items, no difficulty calibration, and a handful of noisy observations per
   concept. A continuous score computed from that would be **false precision** — it would look
   more defensible while being no better informed.
3. **Deriving rather than storing is correct.** Keeping the band computed-on-read means it can
   never drift from its evidence, which removes a whole class of bug and is what open-learner-
   model work recommends for inspectable models (Bull & Kay).

**The one real objection:** the band is derived from a ratio over the ledger, and if `assumed`
entries count fully in that ratio, the band's accuracy is bounded by the accuracy of the
system's weakest inference — and the errors correlate rather than cancel, because a user who is
silent is silent about *everything*. Weighting states (§2.2) fixes this. **[my inference]**

---

# 3. Comparison to established knowledge-tracing approaches

| Approach | Core idea | What it gets right that a 4-state ledger misses | Is it worth adopting here? |
|---|---|---|---|
| **BKT** (Corbett & Anderson 1995) — [paper](https://link.springer.com/article/10.1007/BF01099821) | 2-state HMM; params `P(L₀), P(T), P(G), P(S)`; mastery at P ≥ 0.95 | **Explicit noise modelling.** Guess and slip say out loud that observations are unreliable in *both* directions, and the mastery threshold forces multiple observations. This is the single biggest gap in the four-state design. | **Concepts yes, machinery no.** Adopting P(guess)/P(slip) reasoning as *design discipline* (don't confirm on one observation; don't revert on one error) captures nearly all the value. Fitting real parameters needs labelled data this system won't have. |
| **DKT** (Piech et al. 2015) — [paper](https://stanford.edu/~cpiech/bio/papers/deepKnowledgeTracing.pdf) | LSTM over interaction sequences | Cross-concept structure; no hand-specified skill map | **No.** And notably, Khajah, Lindsey & Mozer's [*How Deep is Knowledge Tracing?* (2016)](https://arxiv.org/abs/1604.02416) showed that a suitably extended BKT matches DKT, concluding the gains don't come from learned representations — and Xiong et al. found duplicate rows inflating the original benchmark ([EDM 2016](https://www.educationaldatamining.org/EDM2016/proceedings/paper_133.pdf)). The field's own verdict is that depth wasn't the win. Interpretability matters more here anyway. |
| **Elo / IRT** — [Pelánek 2016](https://www.fi.muni.cz/~xpelanek/publications/CAE-elo.pdf) | Continuous ability updated per response; self-correcting; jointly estimates item difficulty | **Concept difficulty.** Nothing in the current design distinguishes "cap table" from "liquidation preference overhang" — every concept is one unit in the ratio. That is probably a bigger accuracy loss than the state granularity. | **Partially.** A crude 2–3 tier difficulty tag per concept (assigned once by the Monitor when it introduces the term) would improve the band more cheaply than any change to the states. **[my inference]** |
| **Open Learner Models** (Bull & Kay) — [SMILI](https://www.researchgate.net/publication/228738851_STUDENT_MODELS_THAT_INVITE_THE_LEARNER_IN_THE_SMILI_OPEN_LEARNER_MODELLING_FRAMEWORK_TECHNICAL_REPORT_580), [Bull 2020](https://dl.acm.org/doi/abs/10.1109/TLT.2020.2978473) | Show the model to the learner; let them inspect, contest, edit | **Cheap ground truth, and metacognitive benefit as a bonus.** Turns the system's weakest inference into an answerable question. | **Yes — highest value-to-cost ratio in this table.** Directly attacks the `assumed` problem. Requires only a UI/phrasing decision, and this product's medium is conversation, where "correct me if I'm wrong about that" is one clause. |
| **SM-2 / FSRS** — [Anki FAQ](https://faqs.ankiweb.net/what-spaced-repetition-algorithm) | Per-item memory state (D/S/R); schedules review at predicted forgetting | **Time is a first-class variable.** Knowledge has an age and a stability. | **No, and correctly so.** These optimise *scheduled review*, which this product explicitly isn't. But borrow the one-bit version: `last_observed` should influence trust in the weak states (§2.5). |

**Where the simple model is genuinely adequate.** The four-state ledger is not a naïve
simplification of BKT — it is a reasonable design for a regime BKT was never built for: no
graded items, ~1–5 noisy observations per concept, an LLM (not a solver) as the consumer of the
state, and an error cost of one redundant sentence. Discrete, named, evidence-linked states are
*more* useful to an LLM at prompt time than a float would be, and they are auditable by a human
in a way a fitted HMM is not. **The design's problems are not that it's too simple. They are
(a) the state names overclaim relative to the evidence that sets them, and (b) all four states
collapse to "known" when the band is computed.** Both are fixable without adding a model.

---

# 4. Overall verdict

| State | Verdict | One-line reason |
|---|---|---|
| `unknown` (default) | **Supported** | Cold-start non-mastery prior is standard practice |
| `unknown` (reversion on misunderstanding) | **Weakly supported** | Ignores slip; but the asymmetry is a defensible deliberate choice given the loss function |
| `assumed` (≥3 exposures, no question) | **Weakly supported as a prior; contradicted as "known"** | Base rate of question-asking is ~0.11/student/hour — silence is near-uninformative; 3 has no empirical basis and Webb suggests it's ~form-recognition, not meaning |
| `explained` (asked → explained) | **Weakly supported** | The asking is strong evidence of *prior ignorance*; the explaining is the low-utility rereading condition with no verification |
| `confirmed` (one correct unprompted use) | **Weakly-to-moderately supported** | Right signal (VKS level 5, Clark & Brennan "display"), but ~1 observation ≈ 0.43 posterior under standard guess/slip; vulnerable to same-turn mimicry |
| No decay | **Split: defensible for `confirmed`, weakly supported for `explained`, contradicted for `assumed`** | Bahrick's permastore is real but conditional on level of original learning — which the weak states lack |
| Derived coarse band | **Supported**, conditional on fallback-prior-only use | Coarseness is correct given the loss function and the absence of graded data; but it inherits `assumed`'s error unless states are weighted |

**Net:** the *architecture* is sound — separating a delivery ledger from a knowledge ledger,
ordering evidence by strength, deriving rather than storing the band, and refusing false
precision are all good calls with support behind them. The *calibration* overclaims at three
points: silence counts as evidence, one system action (`explained`) sets a knowledge state, and
one possibly-echoed use sets the top state. All three are fixable with rules, not models.

---

# 5. Highest-risk assumption, and the cheapest test

## The assumption

**That `assumed` — silence across 3 exposures — is positive evidence of understanding at all,
*combined with* the fact that it counts toward the ratio that derives the proficiency band.**

Neither half is fatal alone. Together they create a **self-reinforcing silence spiral**:

1. The system uses terms in briefs.
2. A quiet, skimming, or intimidated user asks nothing — the overwhelmingly likely behaviour
   given Graesser & Person's base rates.
3. Terms flip to `assumed` purely as a function of how much the system talked.
4. `assumed` entries inflate the known-ratio; the band rises to `conversant`/`fluent`.
5. The band, as a fallback prior, tells the system to *stop explaining terms inline*.
6. Briefs get harder. The user understands less, and is now *less* likely to ask, not more —
   because admitting confusion after apparent agreement is more costly than admitting it up
   front (pluralistic ignorance, Miller & McFarland).
7. Return to 2, faster.

This is worth flagging as the top risk for four reasons: it is the only inference driven by
system output rather than user input, so it can run away without any user signal; it is
positively correlated with the failure it causes (more talking → more `assumed` → more
unexplained terms); its errors correlate across concepts rather than cancelling, because a
silent user is silent about everything; and it produces **exactly the failure mode the spec
names as the one that prompted the redesign** — a brief the user cannot follow. The spec already
flags weak-evidence inference as a risk and mandates repetition; what it doesn't guard is the
*compounding path from `assumed` into the band and back into brief difficulty.*

**[my inference]** — the spiral is my synthesis, not a finding in any cited source. The
individual links are each supported; the loop is a hypothesis about this system.

## Cheapest test — offline, today

**The persona harness already computes exactly the needed quantity.** Each persona has a
ground-truth concept set. So:

For each persona run, partition ledger entries by state and compute **precision against ground
truth**:

- `P(actually known | assumed)` — the number that matters
- `P(actually known | confirmed)`, `P(actually known | explained)` — for comparison
- **`P(actually known | exposed ≥3 times but NOT marked assumed)`** — the base rate, and the
  only comparison that makes the first number interpretable

**The decision rule:** if `P(known | assumed)` is not meaningfully above the base rate for
exposed-but-unmarked concepts, **the `assumed` state carries no information and should be
deleted or excluded from the band**, not merely down-weighted. If it is above base rate but
below `explained`, weight it accordingly (§2.2).

Cost: a scoring function over data the harness already produces. No new instrumentation, no
users, no model.

**Two cheap extensions worth adding to the same run:**

- **Sweep the threshold.** Recompute precision at 1, 2, 3, 5, 8 exposures. This is free once the
  scorer exists and turns "3" from an assertion into a measured choice — and gives you the
  precision/recall curve to pick a point on. Webb's 1/3/7/10 design is a reasonable set of
  breakpoints to mirror.
- **Include a deliberately adversarial persona.** A persona that *never asks questions* and
  genuinely knows nothing is the direct falsification test for the spiral: run it 20 sessions
  and check whether its derived band climbs. If a persona that knows nothing and says nothing
  reaches `conversant`, the loop is confirmed and the band weighting is not optional. The spec
  already calls for personas that "resist over-trusting the weak `assumed` path" — this makes
  that concrete and measurable.

**The one thing the harness cannot tell you** is whether real users behave like the personas —
personas are authored, so their silence patterns are an assumption, not evidence. The cheapest
real-world check is the open-learner-model move (§2.2/§3): surface the `assumed` inference to
the user occasionally ("I've been assuming *liquidation preference* is familiar — is it?") and
log the correction rate. That is simultaneously a product feature, a metacognitive prompt with
literature behind it, and a live measurement of the riskiest assumption in the system.

---

# 6. Sources

**Encoding, retention, and the exposure/knowledge gap**

- Craik, F. I. M., & Lockhart, R. S. (1972). Levels of processing: A framework for memory research. *JVLVB*, 11(6), 671–684. [PDF](http://wixtedlab.ucsd.edu/publications/Psych%20218/Craik_Lockhart_1972.pdf)
- Craik, F. I. M., & Tulving, E. (1975). Depth of processing and the retention of words in episodic memory. [summary](https://www.themantic-education.com/ibpsych/2021/10/04/key-study-levels-of-processing-craik-and-tulving-1975/)
- Roediger, H. L., & Karpicke, J. D. (2006). Test-enhanced learning. *Psychological Science*, 17(3), 249–255. [journal](https://journals.sagepub.com/doi/10.1111/j.1467-9280.2006.01693.x)
- Karpicke, J. D., & Roediger, H. L. (2007). Repeated retrieval during learning is the key to long-term retention. *JML*. [PDF](https://learninglab.psych.purdue.edu/downloads/2007/2007_Karpicke_Roediger_JML.pdf)
- Dunlosky, J., Rawson, K. A., Marsh, E. J., Nathan, M. J., & Willingham, D. T. (2013). Improving students' learning with effective learning techniques. *PSPI*, 14(1), 4–58. [journal](https://journals.sagepub.com/doi/abs/10.1177/1529100612453266) · [PDF](https://gwern.net/doc/psychology/spaced-repetition/2013-dunlosky.pdf)
- Zajonc, R. B. (1968). Attitudinal effects of mere exposure. *JPSP Monograph*. [PDF](https://www.psy.lmu.de/allg2/download/audriemmo/ws1011/mere_exposure_effect.pdf)
- Cepeda, N. J., Pashler, H., Vul, E., Wixted, J. T., & Rohrer, D. (2006). Distributed practice in verbal recall tasks: A review and quantitative synthesis. *Psychological Bulletin*, 132(3), 354–380. [PDF](https://augmentingcognition.com/assets/Cepeda2006.pdf)
- Bahrick, H. P. (1984). Semantic memory content in permastore: Fifty years of memory for Spanish learned in school. *JEP: General*. [record](https://www.semanticscholar.org/paper/Semantic-memory-content-in-permastore:-fifty-years-Bahrick/57f7bca4dbd92caba99c660b58f6d5013760ac35)
- Bahrick, H. P., & Phelps, E. (1987). Retention of Spanish vocabulary over 8 years. *JEP: LMC*. [PDF](https://gwern.net/doc/psychology/spaced-repetition/1987-bahrick.pdf)

**Metacognition, fluency, and illusions of knowing**

- Rozenblit, L., & Keil, F. (2002). The misunderstood limits of folk science: An illusion of explanatory depth. *Cognitive Science*, 26(5), 521–562. [journal](https://onlinelibrary.wiley.com/doi/abs/10.1207/s15516709cog2605_1) · [PDF](https://time.com/wp-content/uploads/2015/02/ioed_proofs.pdf_1.pdf)
- Carpenter, S. K., Wilford, M. M., Kornell, N., & Mullaney, K. M. (2013). Appearances can be deceiving: Instructor fluency increases perceptions of learning without increasing actual learning. *PB&R*. [PubMed](https://pubmed.ncbi.nlm.nih.gov/23645413/)
- Carpenter, S. K., et al. (2016). The effect of instructor fluency on students' perceptions of instructors, confidence in learning, and actual learning. [PubMed](https://pubmed.ncbi.nlm.nih.gov/26844368/)
- Dunlosky, J., & Lipko, A. R. (2007). Metacomprehension: A brief history and how to improve its accuracy. *Current Directions*, 16(4). [journal](https://journals.sagepub.com/doi/abs/10.1111/j.1467-8721.2007.00509.x)
- Prinz, A., Golke, S., & Wittwer, J. (2019). How accurately can learners discriminate their comprehension of texts? A meta-analysis. *Educational Research Review*. [journal](https://www.sciencedirect.com/science/article/abs/pii/S1747938X19304270)
- Kruger, J., & Dunning, D. (1999). Unskilled and unaware of it. *JPSP*, 77(6). [PDF](https://sites.lsa.umich.edu/sasi/wp-content/uploads/sites/275/2015/11/krugerdunning02.pdf)

**Question-asking, silence, and conversational grounding**

- Graesser, A. C., & Person, N. K. (1994). Question asking during tutoring. *AERJ*, 31(1), 104–137. [journal](https://journals.sagepub.com/doi/10.3102/00028312031001104) · [PDF](https://gwern.net/doc/psychology/spaced-repetition/1994-graesser.pdf)
- Miller, D. T., & McFarland, C. (1987). Pluralistic ignorance: When similarity is interpreted as dissimilarity. *JPSP*, 53(2). [record](https://www.researchgate.net/publication/232428926_Pluralistic_Ignorance_When_Similarity_is_Interpreted_as_Dissimilarity) · [overview](https://en.wikipedia.org/wiki/Pluralistic_ignorance)
- Clark, H. H., & Brennan, S. E. (1991). Grounding in communication. In *Perspectives on Socially Shared Cognition*. APA. [record](https://philpapers.org/rec/CLAGIC) · [overview](https://en.wikipedia.org/wiki/Grounding_in_communication)

**Vocabulary acquisition and graded knowledge scales**

- Webb, S. (2007). The effects of repetition on vocabulary knowledge. / The effects of context on incidental vocabulary learning. *RFL*. [article](http://www2.hawaii.edu/~readfl/rfl/October2008/webb/webb.html) · [PDF](https://files.eric.ed.gov/fulltext/EJ815123.pdf)
- Uchihara, T., Webb, S., & Yanagisawa, A. How effective is second language incidental vocabulary learning? A meta-analysis. *Language Teaching*. [Cambridge](https://www.cambridge.org/core/journals/language-teaching/article/how-effective-is-second-language-incidental-vocabulary-learning-a-metaanalysis/E38E3468FD2090B1FA3051051DE8E70C)
- Wesche, M., & Paribakht, T. S. (1996). Assessing second language vocabulary knowledge: Depth versus breadth (the Vocabulary Knowledge Scale). [description](https://jalt-publications.org/tlt/departments/myshare/articles/628-using-modified-version-vocabulary-knowledge-scale-aid-vocabular) · [critical analysis](https://eric.ed.gov/?id=EJ867001)

**Learner modelling and knowledge tracing**

- Corbett, A. T., & Anderson, J. R. (1995). Knowledge tracing: Modeling the acquisition of procedural knowledge. *UMUAI*, 4, 253–278. [journal](https://link.springer.com/article/10.1007/BF01099821) · [parameter overview](https://www.cs.williams.edu/~iris/res/bkt/)
- Piech, C., et al. (2015). Deep knowledge tracing. *NIPS*. [PDF](https://stanford.edu/~cpiech/bio/papers/deepKnowledgeTracing.pdf)
- Khajah, M., Lindsey, R. V., & Mozer, M. C. (2016). How deep is knowledge tracing? *EDM*. [arXiv](https://arxiv.org/abs/1604.02416)
- Xiong, X., Zhao, S., Van Inwegen, E., & Beck, J. (2016). Going deeper with deep knowledge tracing. *EDM*. [PDF](https://www.educationaldatamining.org/EDM2016/proceedings/paper_133.pdf)
- Pelánek, R. (2016). Applications of the Elo rating system in adaptive educational systems. *Computers & Education*. [PDF](https://www.fi.muni.cz/~xpelanek/publications/CAE-elo.pdf)
- Bull, S., & Kay, J. (2007). Student models that invite the learner in: The SMILI open learner modelling framework. *IJAIED*, 17(2), 89–120. [PDF/record](https://www.researchgate.net/publication/228738851_STUDENT_MODELS_THAT_INVITE_THE_LEARNER_IN_THE_SMILI_OPEN_LEARNER_MODELLING_FRAMEWORK_TECHNICAL_REPORT_580)
- Bull, S. (2020). There are open learner models about! *IEEE TLT*. [journal](https://dl.acm.org/doi/abs/10.1109/TLT.2020.2978473)
- Abdelrahman, G., Wang, Q., & Nunes, B. (2023). Knowledge tracing: A survey. *ACM Computing Surveys*. [journal](https://dl.acm.org/doi/10.1145/3569576)
- FSRS / DSR model and Anki scheduler. [Anki FAQ](https://faqs.ankiweb.net/what-spaced-repetition-algorithm) · [open-spaced-repetition](https://github.com/open-spaced-repetition)

**Product and industry precedent on "displayed" vs "consumed"**

- Gabielkov, M., Ramachandran, A., Chaintreau, A., & Legout, A. (2016). Social clicks: What and who gets read on Twitter? *ACM SIGMETRICS*. [ACM](https://dl.acm.org/doi/abs/10.1145/2896377.2901462)
- Sundar, S. S., et al. (2024). Sharing without clicking on news in social media. *Nature Human Behaviour*. [journal](https://www.nature.com/articles/s41562-024-02067-4)
- Hoyle, R., et al. (2017). Was my message read? Privacy and signaling on Facebook Messenger. *CHI 2017*. [ACM](https://dl.acm.org/doi/abs/10.1145/3025453.3025925) · [PDF](https://www.cs.oberlin.edu/~rhoyle/papers/hoyle-chi17.pdf)
- Nielsen, J. (2008). How little do users read? Nielsen Norman Group. [article](https://www.nngroup.com/articles/how-little-do-users-read/) · [F-shaped pattern](https://www.nngroup.com/articles/f-shaped-pattern-reading-web-content/)
- MRC/IAB. Viewable Ad Impression Measurement Guidelines. [PDF](https://www.iab.com/wp-content/uploads/2015/06/MRC-Viewable-Ad-Impression-Measurement-Guideline.pdf)
- Apple Mail Privacy Protection impact on open-rate measurement. [Postmark](https://postmarkapp.com/blog/how-apples-mail-privacy-changes-affect-email-open-tracking) · [Validity](https://www.validity.com/blog/case-closed-the-mystery-of-declining-email-open-rates/)
- ADL. xAPI SCORM Profile and verb vocabulary (`experienced` / `attempted` / `completed` / `passed` / `mastered`). [profile](https://github.com/adlnet/xAPI-SCORM-Profile/blob/master/xapi-scorm-profile.md) · [Deep dive: verbs](https://xapi.com/blog/deep-dive-verb/)
