# Persona learning model — literature review

Purpose: decide what a synthetic persona in the eight-session harness should carry as *learning* state and traits, so that its knowledge of briefing terms grows and decays the way a real adult reader's would, and so that a harness-only post-session probe can measure "can converse about it" rather than "can recite a definition".

Scope note: the source literatures are (a) laboratory memory research on students, (b) L2 vocabulary acquisition from reading, and (c) a thinner literature on adults acquiring real-world domain knowledge. Our users are adult professionals reading L1 text about a domain they follow, where the *form* of a term ("liquidation preference", "en primeur", "expected goals") is usually made of known words and the burden is conceptual. Every number below should be read with that gap in mind (see Caveats).

---

## 0. What to build

### 0.1 Per-persona traits (two numeric parameters, one derived set)

| Trait | Range | What it drives | Why this and not more |
|---|---|---|---|
| `prior_knowledge` K | 0–1 | (i) the set of briefing terms the persona already knows at session 1 (the dominant effect); (ii) a modest multiplier on initial encoding, `1 + 0.4·(K − 0.5)` i.e. 0.8–1.2; (iii) the persona's propensity to ask follow-up questions (interest and knowledge co-vary) | Pre-test knowledge is by far the strongest predictor of post-test knowledge (r ≈ .53 across 8,776 effects; Simonsmeier et al. 2022) and of new learning in real domains (Hambrick 2003; Witherby & Carpenter 2022). But the same meta-analysis finds prior knowledge does *not* predict normalised gain on average (r ≈ −.06), so the "rich get richer" multiplier must be modest, not compounding. |
| `memory_rate` m | 0.7–1.4 (multiplier on stability S; 1.0 = population default) | how fast every trace decays | An individual's rate of forgetting is stable within a person across weeks (Sense et al. 2016); in the Groningen ACT-R implementation it ranges about 0.19–0.44 around a default of 0.30 (Sense & van Rijn 2018), i.e. roughly ±35 %. It is *not* predicted by working-memory capacity or general ability in that work, so it has to be its own parameter. Age, working memory and verbal ability should be *expressed through this one number* when you build a persona (e.g. a 68-year-old persona gets m ≈ 0.8), not carried separately. |
| derived: `known_terms` | set | which glossary terms start at stage "can define / can use" | Sample a fraction of the domain glossary proportional to K, weighted toward high-frequency terms. |

Do **not** add: working-memory span, need for cognition, age, general verbal ability, or a "learning style". Reasons in §6.

Interest/motivation is already represented in the harness through read quality and question-asking; the evidence says interest works mainly through exposure and depth of processing (Hambrick 2003; Yanagisawa & Webb 2021), so keep it there.

### 0.2 Per-term memory state

Base it on the DSR (difficulty–stability–retrievability) family used by FSRS, with the ACT-R/HLR insight that stability grows multiplicatively with each successful event and that the growth is larger the more the item had been forgotten at the time (this is what produces the spacing effect without a separate rule). Per `(persona, term)` keep:

- `S` — stability in days (time for retrievability to fall to 0.90). `S = 0` means no trace.
- `D` — difficulty, 1–10 (term-level: abstract, polysemous, or numeric terms are harder; set from the glossary, default 5).
- `n_ctx` — number of *distinct* contexts (news items) in which the term has been met or used.
- `n_ret` — number of successful retrievals (answered a probe / used it correctly).
- `t_last` — time of last event.

Retrievability at elapsed time `t` (days), FSRS-4.5 form (power law, calibrated so that R(S) = 0.9):

```
R(t, S) = (1 + (19/81) · t / S) ^ (−0.5)
```

A power law, not an exponential, is the right shape: the Ebbinghaus replication (Murre & Dros 2015) and the FSRS benchmarks on ~350 M real reviews both favour it, and it is what makes early forgetting fast and late forgetting slow.

Derived knowledge ladder for a term (the probe in §0.4 measures this ladder directly; the simulator uses these to *decide the persona's answer*):

```
P(recognises)                     = R
P(can define | recognises)        = 0.65            # meaning-recall ≈ 0.6–0.7 × meaning-recognition at equal exposure
P(can use in a NEW news context   = 0.5 + 0.25·min(2, n_ctx − 1)   # 0.5 after one context, 1.0 after three
   | can define)
```

The 0.65 comes from the ratio of meaning-recall to meaning-recognition pick-up rates in the incidental-learning meta-analysis (9 %/15 % immediate, 12 %/17 % delayed; Webb, Uchihara & Yanagisawa 2023) and from Pellicer-Sánchez & Schmitt (2010) (recall 55 %/63 % vs recognition 84 %/76 % after 10+ exposures). The context term is from the contextual-diversity studies (§3.4): words met in one context transfer poorly to new sentences.

### 0.3 Update rules

Notation: `R` is retrievability at the moment of the event; `g(R, D, S)` is the FSRS-style stability-gain kernel (FSRS-4 default weights, fit on Anki review data):

```
g(R, D, S) = e^{1.49} · (11 − D) · S^{−0.14} · (e^{0.94·(1 − R)} − 1)
S' = S · (1 + k_event · g)          # for an existing trace
```

`k_event` scales the kernel by event type. FSRS's kernel is fit to *successful retrievals with feedback*, so that event has `k = 1`; the others are set from the ratios in the literature (§2.3), and are the main things to tune against future real-user data.

**1. First glossed encounter (S = 0).** Draw whether any trace is formed, then set S0:

| read quality | P(encode) | S0 (days) |
|---|---|---|
| skipped | 0 | — |
| skimmed | 0.25 | 0.15 |
| read | 0.50 | 0.40 |
| studied | 0.70 | 0.80 |

Multiply P(encode) by the prior-knowledge multiplier, cap at 0.9; multiply S0 by `m`. Rationale: single-encounter pick-up in L2 reading is ~15 % unglossed (Swanborn & de Glopper 1999; Nagy et al. 1985 report 0.15–0.22 immediately, Nagy et al. 1987 0.05 after six days), and glossing raises immediate learning from 26.6 % to 45.3 % and delayed from 19.8 % to 33.4 % (Yanagisawa, Webb & Uchihara 2020). Adult L1 readers of their own domain should sit above L2 learners, hence 0.5 for a normal read. With S0 = 0.4 d the formula gives R ≈ 0.79 after 1 day, 0.33 after 1 week, 0.16 after a month — consistent with the "once-read" delayed rates in §2. **Note that in-text glosses ("X, meaning Y") were among the *least* effective gloss formats in the meta-regression**; the recommended product change this implies is in §3.3.

**2. Re-read of the gloss (restudy) after gap t.** `k = 0.55`. Same context: `n_ctx` unchanged; new news item: `n_ctx += 1`. Massed re-exposure within the same briefing (R ≈ 1) yields almost nothing because `e^{0.94(1−R)} − 1 → 0`; a re-read one week later (R ≈ 0.33) yields about 12× the gain of a re-read after five minutes. This reproduces the spacing effect quantitatively without a separate parameter (see §2.2 for the check against Cepeda et al. 2008).

**3. Asked a follow-up question about the term and got an answer.** Treat as a *studied* re-exposure with elaboration: `k = 0.75`, `n_ctx += 1` (the answer is a new context), and the term's `D` drops by 1 (min 1) because the persona now has an elaborated representation. This is a generation/elaboration event, not retrieval: the generation effect is d ≈ 0.40 over reading (Bertsch et al. 2007) and the "evaluation" component of task involvement is the strongest predictor of incidental gain (Yanagisawa & Webb 2021), but neither is as strong as successful retrieval.

**4. Successful retrieval (answered a probe item correctly, or used the term correctly in an answer).** `k = 1.0` (full FSRS kernel), `n_ret += 1`, `n_ctx += 1` if the sentence was about a new news item. If the harness then shows the correct gloss (feedback), add a restudy event with `k = 0.55` — feedback roughly doubles the testing effect (g 0.73 vs 0.39; Rowland 2014).

**5. Failed retrieval (probe answered wrongly or "don't know").** FSRS lapse rule:

```
S' = 2.18 · D^{−0.05} · ((S + 1)^{0.34} − 1) · e^{1.26·(1 − R)}
```

then, if feedback is given, a restudy event. Without feedback a failed retrieval attempt has no reliable benefit (initial success ≤ 50 % ⇒ g ≈ 0.03; Rowland 2014).

**6. Time elapsed.** Nothing to update; `R(t, S·m)` is computed on demand. Ignore the small 24-hour sleep "bump" reported by Murre & Dros (2015); it is within the noise of everything else here.

**7. Same context vs new context.** Handled by `n_ctx`, which gates the top rung of the ladder. It does not change S: in the diversity studies, diverse contexts *slowed* acquisition slightly but improved transfer (Pagán & Nation 2019; Norman et al. 2022), so the cleanest representation is "same strength, better generalisation".

### 0.4 Probe format and rubric (harness-only, 2–4 items, after each session)

Use a **performance-based four-rung ladder** modelled on Laufer & Goldstein's (2004) strength hierarchy and the Vocabulary Knowledge Scale (Wesche & Paribakht 1996), but with every rung scored from what the persona *produces*, never from its self-rating (the VKS's self-report rungs are its weakest part; Bruton 2009).

Item selection per session: 1 term introduced this session (immediate), 1–2 terms from earlier sessions chosen by the harness to span the retention interval (1 day, ~1 week, ~1 month if the schedule allows), and, every other session, 1 **plausible pseudo-term** from the domain that was never shown. The pseudo-term calibrates confabulation, which matters more for an LLM persona than for a human.

Each item is one prompt with three parts:

> **(a)** Have you come across the term "*X*"? (yes / not sure / no)
> **(b)** In one or two sentences, what does it mean?
> **(c)** Use it in a sentence about [this week's story on ___].

Grader rubric (an LLM grader compares against the canonical gloss and the story summary; paraphrase is fine — Rawson & Dunlosky's participants self-scored sentence-length definitions for meaning, not wording):

| Rung | Label | Criterion |
|---|---|---|
| 0 | never seen | (a) = no *and* (b) empty or wrong; or a confident answer for a pseudo-term (flag `confabulated`) |
| 1 | recognises | (a) = yes/not sure, (b) is related-but-wrong or only the surface domain ("something about wine pricing") |
| 2 | can define | (b) captures the core meaning; no false components. Minor omissions allowed |
| 3 | can use | rung 2 *and* (c) applies the term correctly to the story: correct referent, correct polarity/direction, idiomatic collocation (e.g. "raised a Series A *at* a $40 m pre-money", not "*of*"). A grammatical but generic sentence that could have been written without knowing the story scores 2 |

Record per item: rung, `confabulated` flag, elapsed time since the term's last exposure, and the persona's simulated R at that moment — so the harness can check that observed rungs track the model.

Reactivity warning: the probe itself is a retrieval event and will strengthen the probed terms (that is the testing effect, g ≈ 0.5). Either model it (apply rule 4/5 to probed terms) or restrict probing to a random subset so most terms follow the pure read-only trajectory. Do both: apply the update *and* keep the probed subset small.

---

## 1. Which individual differences predict acquisition and forgetting

### 1.1 Prior domain knowledge — strong for *level*, weak-to-mixed for *rate*

- Simonsmeier, Flaig, Deiglmayr, Schalk & Schneider (2022), *Educational Psychologist* 57(1), 31–54, meta-analysis of 8,776 effect sizes: pre-test → post-test knowledge r = .534; pre-test → *normalised gain* r = −.059 with a 95 % prediction interval of [−.69, .62]. Their conclusion is that both "knowledge is power" and "prior knowledge is negligible" are falsified as general statements. https://doi.org/10.1080/00461520.2021.1939700
- Witherby & Carpenter (2022), *JEP: Learning, Memory & Cognition* 48, 483–498: across three experiments, prior knowledge of cooking or American football predicted learning of *new* (deliberately false) facts in that domain only, with curiosity mediating the effect in the last experiment. https://doi.org/10.1037/xlm0000996
- Hambrick (2003), *Memory & Cognition* 31, 902–917: adults followed a college-basketball season for ~2.5 months. Prior knowledge had a strong direct effect on new knowledge; interest acted *indirectly* through exposure; fluid intelligence had no effect. This is the closest analogue to our product (adults, real domain, news-paced acquisition). https://doi.org/10.3758/BF03196444
- Hambrick & Engle (2002), *Cognitive Psychology* 44, 339–384 (baseball broadcasts): large effect of domain knowledge on memory for game-relevant information; working memory also mattered but *additively* — high knowledge did not change the WM–performance relation, which the authors read as inconsistent with rich-get-richer and compensation hypotheses. https://doi.org/10.1006/cogp.2001.0769
- In the L2 incidental-learning meta-analysis, learners beyond basic proficiency showed twice the effect size of basic learners (g 1.40 vs 0.70; Webb, Uchihara & Yanagisawa 2023).
- Stanovich's (1986) Matthew-effect argument is about reciprocal causation through *volume of reading*; it is not evidence for a per-encounter learning-rate multiplier.

Verdict: carry K. Use it chiefly to set what the persona already knows and how much it engages; keep the per-encounter multiplier small (±20 %). Do not let it compound.

### 1.2 Working memory capacity — real but small, and already absorbed

- Linck, Osthus, Koeth & Bunting (2014), *Psychonomic Bulletin & Review* 21, 861–883: 79 samples, 3,707 participants, ρ = .255 between WM and L2 processing/proficiency outcomes. https://doi.org/10.3758/s13423-013-0565-2
- Daneman & Merikle (1996), *Psychonomic Bulletin & Review* 3, 422–433: WM span correlates with comprehension in the .3–.4 range for process-plus-storage measures. Daneman & Green (1986), *JML* 25, 1–18, showed span predicts learning word meanings from context in adults. https://doi.org/10.3758/BF03214546 ; https://doi.org/10.1016/0749-596X(86)90018-5
- Cain, Oakhill & Lemmon (2004), *J. Educational Psychology* 96, 671–681: in 9–10-year-olds, WM mattered most when inferring word meanings across large text units. https://doi.org/10.1037/0022-0663.96.4.671
- Sense & van Rijn (2018), *Frontiers in Education* 3:112: in 126 adults learning 35 Swahili–Dutch pairs, the model-estimated rate of forgetting correlated r = −.13 with WM capacity (Bayes factor ~1.4, i.e. no evidence) and not at all with general cognitive ability, while it correlated ≈ −.79 with delayed recall. https://doi.org/10.3389/feduc.2018.00112

Verdict: WM predicts *comprehension* more than it predicts *retention*. In a 150-word briefing with inline glosses the comprehension load is small. Do not carry WM; let `memory_rate` and read quality absorb it.

### 1.3 A stable individual forgetting rate — the trait the evidence actually supports

- Sense, Behrens, Meijer & van Rijn (2016), *Topics in Cognitive Science* 8, 305–321: estimates of an individual's rate of forgetting (the ACT-R decay parameter fitted from accuracy and RT) are stable across sessions weeks apart but differ across materials (vocabulary vs flags vs maps). https://doi.org/10.1111/tops.12183
- Sense & van Rijn (2018): default decay 0.30; observed per-participant range 0.186–0.442, mean 0.296 (SD 0.046).

Verdict: carry `memory_rate`. Because the trait is material-specific, treat domain (startups vs wine vs football) as part of the item difficulty `D`, not the persona.

### 1.4 Interest, need for cognition, motivation — small, and act through behaviour

- Liu & Nesbit (2024), *Review of Educational Research* 94: need for cognition ↔ academic achievement r = .20 [.18, .22], 136 effects, N = 53,258. https://doi.org/10.3102/00346543231160474
- Schiefele, Krapp & Winteler (1992): interest ↔ achievement correlations of small-to-medium size (~.16–.32 across subjects).
- Hambrick (2003): interest → exposure → knowledge; no direct path.
- Witherby & Carpenter (2022): curiosity mediates the prior-knowledge effect.
- Yanagisawa & Webb (2021), *Language Learning* 71, meta-analysis of 398 effects: task involvement explains 15.0 % (immediate) and 5.1 % (delayed) of variance in incidental gains; the *evaluation* component (working out how the word fits) drives it; mere *search* does not. https://doi.org/10.1111/lang.12444

Verdict: do not add a parameter. Make the persona's read-quality distribution and question-asking propensity correlate with K (which already stands in for interest), because interest expresses itself as dwell and questions — both already simulated.

### 1.5 Reading depth — already simulated; its effect size

Swanborn & de Glopper (1999) found reading purpose and grade among the moderators of incidental pick-up; Hulstijn & Laufer (2001) found retention ordered reading < reading+fill-in < composition. The `P(encode)` and `S0` table in §0.3 encodes this as a ~3× range from skimmed to studied, which is the same order as the gap between "reading for comprehension" and "using the word in writing" in that literature.

### 1.6 Age — modest and mostly offset

- Verhaeghen (2003), *Psychology and Aging* 18, 332–339: older adults score *higher* on vocabulary, +0.80 SD (production 0.68, multiple choice 0.93). https://doi.org/10.1037/0882-7974.18.2.332
- Verhaeghen, Marcoen & Goossens (1993), *J. Gerontology* 48, P157–P171: older adults roughly 0.5–1.0 SD lower on episodic recall tasks, with paired-associate recall among the more affected. https://doi.org/10.1093/geronj/48.4.P157
- Studies of novel-word learning find older adults equally accurate but slower, relying more on existing vocabulary and less on WM (see Kavé 2024 for a review of adult vocabulary change, https://doi.org/10.1111/1460-6984.12820).

Verdict: no separate parameter. For a persona over ~65 set `memory_rate` ≈ 0.8 and K a notch higher; for 25–55 (most of our users) age is noise.

### 1.7 General verbal ability

Correlated with prior knowledge, reading volume and WM; no study isolates its effect on adult acquisition of domain concepts from glossed text. Omit.

---

## 2. The shape of forgetting and relearning

### 2.1 Forgetting curves

Murre & Dros (2015), *PLoS ONE* 10(7): e0120644, replicated Ebbinghaus with one subject over 70 hours of nonsense-syllable learning. Savings (proportion of relearning time saved), Ebbinghaus vs replication: 20 min .58/.44; 1 h .44/.33; 9 h .36/.27; 1 day .34/.27; 2 days .28/.29; 6 days .25/.21; 31 days .21/.04. A double-exponential (Memory Chain Model) fit marginally better than Ebbinghaus's own power and log functions, with no meaningful AIC difference; there is a small upward "bump" around 24 h plausibly due to sleep. https://doi.org/10.1371/journal.pone.0120644

For material more like ours, the delayed rates in the L2 literature are the better anchor: once-met words drop from 45 % to 33 % (meaning translation) after a month (Rott 1999, as tabulated in Uchihara et al. 2019) and from 42 % to 6 % after three months (Waring & Takaki 2003, same source); across 29 samples the delayed pick-up (mean RI 34 days) was 6 % for form recognition but 17 % for meaning recognition and 12 % for meaning recall — the last two *not* lower than immediate, which the authors attribute to spaced treatments and pseudoword designs (Webb, Uchihara & Yanagisawa 2023, *Language Teaching*; https://doi.org/10.1017/S0261444822000507).

Parameter implication: a single-session trace should have R in the 0.2–0.35 range after a week and ~0.1–0.15 after a month; S0 ≈ 0.4 d in the FSRS form gives 0.33 and 0.16.

### 2.2 The spacing effect — how much, at what gaps

- Cepeda, Pashler, Vul, Wixted & Rohrer (2006), *Psychological Bulletin* 132, 354–380: 839 assessments, 317 experiments, 184 articles. Spaced beats massed across the literature; the interstudy interval (ISI) that maximises retention grows with the retention interval (RI), and the field under-samples RIs > 1 month. https://doi.org/10.1037/0033-2909.132.3.354
- Cepeda, Vul, Rohrer, Wixted & Pashler (2008), *Psychological Science* 19, 1095–1102: 1,354 people, fact learning, gaps up to 3.5 months, RIs of 7, 35, 70 and 350 days. Optimal gaps were 1, 11, 21 and 21 days respectively; recall improvement at the optimal vs zero-day gap was 10 %, 59 %, 111 % and 77 %; pooling, the optimal gap gave a 64 % increase in recall (d = 1.1) and 26 % in recognition (d = 1.5) for the same study time. Optimal gap ≈ 20–40 % of a one-week RI, falling to 5–10 % of a one-year RI. https://doi.org/10.1111/j.1467-9280.2008.02209.x
- Bahrick & Phelps (1987), *JEP: LMC* 13, 344–349: 50 Spanish–English pairs, tested after 8 years; recall 15 % (30-day inter-session interval) vs 8 % (1 day) vs 6 % (0 days); recognition of non-recalled words 83/80/71 %. https://doi.org/10.1037/0278-7393.13.2.344
- In the incidental-vocabulary meta-analysis, spaced treatments g = 1.51 vs massed 0.97 (Webb et al. 2023).

Check of the §0.3 mechanism: a second read after one day (R ≈ 0.79, S0 = 0.4) gives a kernel factor `e^{0.94·0.21} − 1 = 0.22`; after seven days (R ≈ 0.33) it is 0.88 — a 4× larger stability increment, offset by the lower R at the moment of re-reading. Integrated over a 35-day test delay this yields the same qualitative "ridgeline" as Cepeda et al. 2008. Tune `w10` (0.94) if the ridgeline peak needs moving.

### 2.3 The testing / retrieval effect — how much more than re-reading

- Roediger & Karpicke (2006), *Psychological Science* 17, 249–255, prose passages. Exp. 1 (study–study vs study–test): 5 min 81 % vs 75 %; 2 days 54 % vs 68 %; 1 week 42 % vs 56 % (d = 0.83). Exp. 2: SSSS/SSST/STTT recalled 83/78/71 % at 5 min but 40/56/61 % at one week, even though STTT subjects read the passage 3.4 times vs 14.2. https://doi.org/10.1111/j.1467-9280.2006.01693.x
- Karpicke & Roediger (2008), *Science* 319, 966–968, 40 Swahili–English pairs: conditions with repeated retrieval recalled ~80 % after one week; conditions that dropped items from testing once recalled fell to 36 % and 33 %; repeated *studying* after a first success added nothing. All groups predicted ~50 %. https://doi.org/10.1126/science.1152408
- Rowland (2014), *Psychological Bulletin* 140, 1432–1463, meta-analysis: overall g = 0.50 [0.42, 0.58]; RI ≥ 1 day g = 0.69 vs < 1 day 0.41; with feedback 0.73 vs without 0.39; initial recall tests g ≈ 0.7–0.8 vs recognition tests 0.32–0.36; single-word stimuli g = 0.33, paired associates 0.59, prose 0.58; without feedback, initial success ≤ 50 % gives g = 0.03 (no effect), 51–75 % 0.29, > 75 % 0.56. https://doi.org/10.1037/a0037559
- Adesope, Trevisan & Sundararajan (2017), *Review of Educational Research* 87, 659–701: practice tests beat restudy and all other comparison conditions across 272 effects. https://doi.org/10.3102/0034654316689306

Converting Roediger & Karpicke's one-week figures (42 % vs 56 %) into the FSRS retrievability form gives a stability ratio of ≈ 1.8 for retrieval over restudy, which is where `k_restudy = 0.55` comes from.

### 2.4 Relearning and successive relearning

- Rawson & Dunlosky (2011), *JEP: General* 140, 283–302, 533 students learning concept definitions: practise to 3 correct recalls, then relearn (to 1 correct recall) in 3 spaced sessions. Interim recall after 2 days was 73 % (criterion 3) vs 58 % (criterion 1); after a couple of relearning sessions the criterion difference largely vanished — the *number of spaced relearning sessions*, not initial over-learning, drove 1- and 4-month retention. https://doi.org/10.1037/a0023956 (summary in Rawson & Dunlosky 2012, *Educ. Psychol. Rev.* 24, 419–435)
- Janes, Dunlosky, Rawson & Jasnow (2020), *Applied Cognitive Psychology*: successive relearning in a real course raised exam performance by ≥ 10 points, d = .54–1.10. https://doi.org/10.1002/acp.3699

Implication: once a term has been retrieved once, the marginal value of further same-session exposure is small; the value is in retrieving it again in a later session. This is what the DSR kernel's `S^{−0.14}` and `(1 − R)` terms produce.

### 2.5 The generation effect

Bertsch, Pesta, Wiscott & McDaniel (2007), *Memory & Cognition* 35, 201–210: 86 studies, 445 effects, d = 0.40 for generating over reading, with large moderator variance. https://doi.org/10.3758/BF03193441 — this sets the "asked-and-answered" event between restudy and retrieval.

---

## 3. Incidental vocabulary acquisition from reading

### 3.1 Probability of learning from one encounter

- Nagy, Herman & Anderson (1985), *Reading Research Quarterly* 20, 233–253: eighth-graders reading natural text; small but reliable gains at all ability levels; probability of learning a word from one encounter 0.15–0.22 on an immediate multiple-choice test, depending on item difficulty. https://doi.org/10.2307/747758
- Nagy, Anderson & Herman (1987), *American Educational Research Journal* 24, 237–270: with a six-day delay the probability fell to ~0.05 (report: https://files.eric.ed.gov/fulltext/ED264546.pdf).
- Swanborn & de Glopper (1999), *Review of Educational Research* 69, 261–285, meta-analysis of 20 experiments: ~15 % of unknown words learned; grade level and test sensitivity to partial knowledge explain 66 % of the variance in effects. https://doi.org/10.3102/00346543069003261
- L2 reading: 17 % immediate, 15 % delayed pick-up (Webb, Uchihara & Yanagisawa 2023).

### 3.2 Growth with number of encounters — what the "8–12 encounters" claim rests on

- Uchihara, Webb & Yanagisawa (2019), *Language Learning* 69, 559–599: 45 effects, 26 studies, N = 1,918; correlation between frequency of encounters and learning r = .34, moderated by spacing (massed single-day treatments show higher correlations, e.g. .54, than spaced ones, e.g. .18), test format, and engagement. https://doi.org/10.1111/lang.12343
- Webb (2007), *Applied Linguistics* 28, 46–65: 1/3/7/10 encounters in glossed sentences; at least one of ten knowledge aspects improved at every step; form recognition ~67 % after one encounter but meaning recall only ~29 % after ten. https://doi.org/10.1093/applin/aml048
- Pellicer-Sánchez & Schmitt (2010), *Reading in a Foreign Language* 22, 31–55: reading *Things Fall Apart*; gains became noticeable at 5–8 exposures and accelerated at 10–17; after 10+ exposures meaning recognition 84 %, form recognition 76 %, meaning recall 55 %, word-class recall 63 %. https://nflrc.hawaii.edu/rfl/item/207
- Horst, Cobb & Meara (1998), *Reading in a Foreign Language* 11, 207–223: graded reader, mean pick-up five words; ~8 encounters suggested. Nation (2014), *Reading in a Foreign Language* 26, 1–16, *assumes* 12 repetitions when computing how much reading is needed and is explicit that this is a working assumption, noting Vidal (2011) found the largest jump between two and three repetitions. https://files.eric.ed.gov/fulltext/EJ1044345.pdf

So: the 8–12 figure is a convention derived from a few L2 extensive-reading studies with *unglossed* text and receptive tests; the underlying relationship is a continuous, moderately strong correlation with diminishing returns, heavily dependent on what "learned" means. In the model, "number of encounters" is not a parameter at all — it falls out of repeated S updates. A glossed term met once per session for four spaced sessions, with one retrieval, ends with S in the tens of days, which matches the "10+ exposures ⇒ ~55 % recall" band.

### 3.3 Inline gloss vs inference from context

- Yanagisawa, Webb & Uchihara (2020), *Studies in Second Language Acquisition* 42, 411–438: 42 studies, 359 effects, 3,802 participants. Glossed reading 45.3 % (immediate) / 33.4 % (delayed) vs non-glossed 26.6 % / 19.8 % — a 1.7× advantage that persists. Multiple-choice glosses were most effective; **in-text glosses and glossaries were the least effective formats**; L1 glosses beat L2 glosses, especially for beginners. https://doi.org/10.1017/S0272263119000688
- Bolger, Balass, Landen & Perfetti (2008), *Discourse Processes* 45, 122–159, adults: definition accuracy 0.66 with a definition provided vs 0.36 without; four varied contexts 0.59 vs one repeated context 0.52; in Exp. 2, meaning generation 0.43 (4 contexts) vs 0.29 (1 context) vs 0.04 (form only). https://doi.org/10.1080/01638530701792826

Product implication (not a persona-model parameter): the current "X, meaning Y" in-text gloss is the format with the weakest evidence; a brief *marginal/tap-to-reveal* gloss, or a gloss that makes the reader choose between two meanings, is where the effect sizes are. Worth an A/B once the persona model exists.

### 3.4 New context vs same context

- Johns, Dye & Jones (2016), *Psychonomic Bulletin & Review* 23, 1214–1220; Pagán & Nation (2019), *Cognitive Science* 43:e12705; Norman et al. (2022), *QJEP* 76, n = 239. Consistent picture: diverse contexts slow initial acquisition slightly, do not change form learning, and produce meanings that transfer to *unfamiliar* sentences; a single repeated context produces knowledge that works only in that context. https://doi.org/10.1111/cogs.12705 ; https://doi.org/10.1177/17470218221126976

Hence `n_ctx` gating the "can use in a new context" rung — which is exactly the product's target behaviour.

---

## 4. Measuring "can converse about it"

- Nation's framework (2001/2013) splits word knowledge into form, meaning and use, each receptive and productive. Read (1993) operationalised depth with the Word Associates Test (collocational and paradigmatic associates). Laufer & Nation (1999), *Language Testing* 16, 33–51, built a *controlled productive* test (sentence completion with the first letters given). https://doi.org/10.1177/026553229901600103
- Laufer & Goldstein (2004), *Language Learning* 54, 399–436: four strengths form a hierarchy at every frequency band — passive recognition (easiest) < active recognition < passive recall < active recall (hardest). https://doi.org/10.1111/j.0023-8333.2004.00260.x
- Wesche & Paribakht (1996), *Canadian Modern Language Review* 53, 13–40: the VKS — five rungs from "never seen" through "seen but don't know", "I think it means…", "I know it means…", to "I can use it in a sentence: …"; test–retest r = .89; sensitive to short-term change. Bruton (2009), *Language Assessment Quarterly* 6, 288–297, criticises the mixing of self-report and performance, the non-interval scale, and the weak link between the sentence rung and real use. https://doi.org/10.1080/15434300902801909
- Kremmel & Schmitt (2016), *Language Assessment Quarterly* 13, 377–392: form–meaning item formats (matching, MC, cloze) say little about learners' ability to *employ* words or about derivative/collocational knowledge. https://doi.org/10.1080/15434303.2016.1237516
- Webb et al. (2023) and Uchihara et al. (2019) show why the test format matters for our numbers: the same treatment yields 18 % (form recognition), 15 % (meaning recognition) or 9 % (meaning recall).

The probe in §0.4 follows from this: performance-based rungs; the top rung is *active recall in a new context with correct application to the current story* (Laufer & Goldstein's "active recall" plus a use criterion), and a pseudo-term item controls for guessing/confabulation. Two to four items keep the probe short enough that reactivity (§2.3) stays a modelled nuisance rather than the main effect.

---

## 5. Simulated learners: what to borrow

| Model | State per item | Update | Fit to human data? | Notes for us |
|---|---|---|---|---|
| **BKT** (Corbett & Anderson 1995, *UMUAI* 4, 253–278) | P(known) | 4 params: P(L0), P(T), P(G), P(S); HMM update on each response | Yes, on tutor data; classic values P(S)=.10, P(G)=.30 | No forgetting, no time; skills not items. Khajah, Lindsey & Mozer (2016) show adding forgetting lifts BKT's AUC from .73 to .83 on Assistments vs DKT's .86 — most of DKT's edge is forgetting + ability. https://arxiv.org/abs/1604.02416 |
| **DKT** (Piech et al. 2015, NeurIPS) | RNN hidden state | learned | Yes, large tutor logs | No interpretable parameters; nothing to set from the literature; overkill for 8 sessions. |
| **ACT-R declarative memory / Pavlik & Anderson 2005** (*Cognitive Science* 29, 559–586) | list of presentation times with per-trace decay | activation m = ln Σ t_i^{−d_i}; d_i = c·e^{m_{i−1}} + a (decay of each new trace grows with activation at the time — this is the spacing mechanism); P(recall) = logistic((m − τ)/s) | Yes: Japanese–English vocabulary, spacings and 1–7-day RIs; used in Groningen's adaptive systems with default decay 0.30, per-learner 0.19–0.44 | Best-motivated psychologically; needs the full presentation history per item and a threshold/noise fit. https://doi.org/10.1207/s15516709cog0000_14 |
| **Half-life regression** (Settles & Meeder 2016, ACL) | half-life h | p = 2^{−Δ/h}; h = 2^{Θ·x}, x = (#correct, #incorrect, lexeme tags); L2-regularised squared loss | Yes: 13 M Duolingo traces; MAE 0.128 vs Leitner 0.235, Pimsleur 0.445, logistic 0.211; AUC only .54 | Simplest; exponential forgetting; treats all exposures as retrieval attempts. https://aclanthology.org/P16-1174/ |
| **DASH** (Lindsey, Shroyer, Pashler & Mozer 2014, *Psychological Science* 25, 639–647; equations in Choffin et al. 2019) | counts of correct/total attempts in windows {1 h, 1 d, 7 d, 30 d, ∞} | logistic(a_s − d_i + Σ_w θ_{2w+1}·log(1+c_w) − θ_{2w+2}·log(1+n_w)) | Yes: semester-long middle-school Spanish; personalised review +16.5 % over massed, +10 % over generic spacing | Has an explicit student-ability term — a direct precedent for `memory_rate`. https://doi.org/10.1177/0956797613504302 ; https://arxiv.org/abs/1905.06873 |
| **Leitner / SM-2** | box or ease factor + interval | fixed heuristics | No | Baselines only. |
| **FSRS** (open-spaced-repetition; Anki ≥ 23.10) | D, S, (R computed) | power-law R; multiplicative S gain with (11−D), S^{−w9}, e^{w10(1−R)}; separate lapse rule | Yes: benchmark on ~350 M reviews from 9,999 Anki users; log-loss .439/AUC .72 for FSRS-6 vs HLR .497/.63, DASH .531/.61, ACT-R .539/.60, SM-2 .569/.57 | Best-calibrated retrievability function available; 17 public parameters. Fit to *flashcard retrieval*, so our read/ask events need the `k_event` scaling. https://github.com/open-spaced-repetition/fsrs4anki/wiki ; https://expertium.github.io/Benchmark.html |

Recommendation: FSRS's state and equations (§0.2–0.3) for the memory core, with (i) DASH's per-student ability term as `memory_rate`, (ii) ACT-R's insight that all events, not only retrievals, add to the trace — implemented as `k_event`, and (iii) an explicit encoding-probability front end from the incidental-learning literature, because none of the spaced-repetition models has a notion of "read past it and nothing stuck".

If a simpler first cut is wanted: HLR with `h ← h · f_event` (f_retrieval ≈ 2, f_restudy ≈ 1.5, f_ask ≈ 1.7, first read h0 ≈ 0.3–0.6 d × m) reproduces the same ordering with three lines of code, at the cost of exponential forgetting and no spacing effect unless you add it by hand.

---

## 6. What the evidence does not support

1. **Working memory as a persona parameter.** ρ ≈ .26 with L2 outcomes, ~ −.13 with fitted forgetting rate (no evidence), additive rather than interactive with domain knowledge. Absorb into `memory_rate`.
2. **Need for cognition / interest as separate parameters.** r ≈ .20 with achievement, mediated by exposure and depth of processing, which the harness already simulates.
3. **Age as a separate parameter for 25–55-year-olds.** Vocabulary rises with age (+0.8 SD); new-word accuracy is similar; episodic decline appears mainly past ~65 and is captured by `memory_rate`.
4. **General verbal ability.** No study isolates it for adults learning glossed domain terms.
5. **A compounding "rich get richer" learning-rate multiplier.** Prior knowledge predicts post-test *level* (r = .53) but not normalised *gain* (r = −.06, huge heterogeneity). Represent K as starting knowledge plus a small encoding bonus, not as a rate that grows with knowledge.
6. **A fixed "N encounters to learn" threshold (8, 10, 12).** The relation is a continuous r ≈ .34 with diminishing returns; thresholds came from unglossed L2 novels and receptive tests. Let repetitions accumulate through S.
7. **Any benefit from repeating a term within the same briefing.** Massed re-exposure yields little (spacing literature; Karpicke & Roediger 2008's repeated *study* condition added nothing). The (1−R) kernel gives near-zero gain at R ≈ 1; do not add a per-mention bonus.
8. **Exponential forgetting.** Use a power law.
9. **Expanding vs uniform spacing schedules.** Evidence is mixed; the harness does not control schedule anyway.
10. **The 24-hour sleep bump.** Real but small relative to everything else.
11. **Self-reported knowledge rungs (VKS style) as ground truth.** Score from production.
12. **Equating "defined inline" with "learned".** In-text glosses are the weakest gloss format in the meta-regression; one glossed read gives ~0.3 recall a week later, not mastery.
13. **Retrieval without success or feedback as a learning event.** Initial success ≤ 50 % without feedback: g ≈ 0.

---

## 7. Caveats

- **Lab vs field.** Most effect sizes come from single-session lab studies with deliberate learning of lists or short passages; the two field studies closest to us (Hambrick 2003; Lindsey et al. 2014) show smaller but same-signed effects. Expect real users to sit at the low end of every range above.
- **L2 vocabulary vs L1 domain concepts.** Pick-up rates (15–45 %), encounter counts and gloss effects are from L2 learners meeting unknown *word forms*. Our users usually know the words and must learn a *concept and its relations*. Form learning is essentially free for them; concept learning is closer to Bolger et al.'s adult definition studies and to Witherby & Carpenter's fact learning. This is why the §0.3 encoding probabilities are set above the L2 figures, and why they are the first thing to recalibrate on real users.
- **Adults vs students.** Rowland, Cepeda and the L2 meta-analyses are dominated by undergraduates and school pupils. Adult professionals bring higher K and lower time-on-task; Bahrick's and Hambrick's adult samples behave as the model predicts, but the calibration is thin.
- **Measurement reactivity.** Every probe is a retrieval event with roughly g = 0.5. Whatever the harness measures, it changes; keep probed subsets small and model the update.
- **Simulated persona ≠ human.** The model tells the persona *what it can answer*; an LLM persona can still confabulate a plausible definition for a term at R = 0.1. The pseudo-term item and the `confabulated` flag exist to detect exactly this, and the register work already done should be extended to "hedged, partially-right" answers at rungs 1–2.
- **Unverified constant.** The fitted values of the ACT-R decay constants c and a in Pavlik & Anderson (2005) could not be checked against the paper for this review (the PDF was unreachable); the *form* of the equation and the Groningen default/range (0.30; 0.19–0.44) are verified.
- **Mixed evidence, flagged as such:** prior knowledge → learning *rate* (Simonsmeier vs Witherby & Carpenter/Hambrick); WM's role (Linck vs Sense & van Rijn); delayed vs immediate gloss advantage by gloss language; contextual diversity's effect on acquisition speed (slower) vs transfer (better).

---

## Sources (checkable)

- Adesope, Trevisan & Sundararajan (2017). *Rev. Educ. Res.* 87, 659–701. https://doi.org/10.3102/0034654316689306
- Bahrick & Phelps (1987). *JEP: LMC* 13, 344–349. https://gwern.net/doc/psychology/spaced-repetition/1987-bahrick.pdf
- Bertsch, Pesta, Wiscott & McDaniel (2007). *Memory & Cognition* 35, 201–210. https://doi.org/10.3758/BF03193441
- Bolger, Balass, Landen & Perfetti (2008). *Discourse Processes* 45, 122–159. https://doi.org/10.1080/01638530701792826
- Bruton (2009). *Language Assessment Quarterly* 6, 288–297. https://doi.org/10.1080/15434300902801909
- Cain, Oakhill & Lemmon (2004). *J. Educ. Psychol.* 96, 671–681. https://doi.org/10.1037/0022-0663.96.4.671
- Cepeda, Pashler, Vul, Wixted & Rohrer (2006). *Psychol. Bull.* 132, 354–380. https://doi.org/10.1037/0033-2909.132.3.354
- Cepeda, Vul, Rohrer, Wixted & Pashler (2008). *Psychol. Sci.* 19, 1095–1102. https://doi.org/10.1111/j.1467-9280.2008.02209.x
- Choffin, Popineau, Bourda & Vie (2019). DAS3H. *EDM 2019.* https://arxiv.org/abs/1905.06873
- Corbett & Anderson (1995). *UMUAI* 4, 253–278. https://doi.org/10.1007/BF01099821
- Daneman & Green (1986). *JML* 25, 1–18. https://doi.org/10.1016/0749-596X(86)90018-5
- Daneman & Merikle (1996). *Psychon. Bull. Rev.* 3, 422–433. https://doi.org/10.3758/BF03214546
- FSRS algorithm wiki and benchmark. https://github.com/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm ; https://expertium.github.io/Benchmark.html
- Hambrick (2003). *Memory & Cognition* 31, 902–917. https://doi.org/10.3758/BF03196444
- Hambrick & Engle (2002). *Cognitive Psychology* 44, 339–384. https://doi.org/10.1006/cogp.2001.0769
- Horst, Cobb & Meara (1998). *Reading in a Foreign Language* 11, 207–223. https://nflrc.hawaii.edu/rfl/item/31
- Hulstijn & Laufer (2001). *Language Learning* 51, 539–558. https://doi.org/10.1111/0023-8333.00164
- Janes, Dunlosky, Rawson & Jasnow (2020). *Applied Cognitive Psychology* 34, 667–678. https://doi.org/10.1002/acp.3699
- Johns, Dye & Jones (2016). *Psychon. Bull. Rev.* 23, 1214–1220. https://doi.org/10.3758/s13423-015-0980-7
- Karpicke & Roediger (2008). *Science* 319, 966–968. https://doi.org/10.1126/science.1152408
- Kavé (2024). *Int. J. Lang. Comm. Disord.* https://doi.org/10.1111/1460-6984.12820
- Khajah, Lindsey & Mozer (2016). *EDM 2016.* https://arxiv.org/abs/1604.02416
- Kremmel & Schmitt (2016). *Language Assessment Quarterly* 13, 377–392. https://doi.org/10.1080/15434303.2016.1237516
- Laufer & Goldstein (2004). *Language Learning* 54, 399–436. https://doi.org/10.1111/j.0023-8333.2004.00260.x
- Laufer & Nation (1999). *Language Testing* 16, 33–51. https://doi.org/10.1177/026553229901600103
- Linck, Osthus, Koeth & Bunting (2014). *Psychon. Bull. Rev.* 21, 861–883. https://doi.org/10.3758/s13423-013-0565-2
- Lindsey, Shroyer, Pashler & Mozer (2014). *Psychol. Sci.* 25, 639–647. https://doi.org/10.1177/0956797613504302
- Liu & Nesbit (2024). *Rev. Educ. Res.* 94, 155–192. https://doi.org/10.3102/00346543231160474
- Murre & Dros (2015). *PLoS ONE* 10(7): e0120644. https://doi.org/10.1371/journal.pone.0120644
- Nagy, Herman & Anderson (1985). *Reading Research Quarterly* 20, 233–253. https://doi.org/10.2307/747758
- Nagy, Anderson & Herman (1987). *AERJ* 24, 237–270. https://doi.org/10.3102/00028312024002237
- Nation (2014). *Reading in a Foreign Language* 26, 1–16. https://files.eric.ed.gov/fulltext/EJ1044345.pdf
- Norman, Hulme, Sarantopoulos, Chandran, Shen, Rodd, Joseph & Taylor (2022). *QJEP* 76, 1658–1671. https://doi.org/10.1177/17470218221126976
- Pagán & Nation (2019). *Cognitive Science* 43:e12705. https://doi.org/10.1111/cogs.12705
- Pavlik & Anderson (2005). *Cognitive Science* 29, 559–586. https://doi.org/10.1207/s15516709cog0000_14
- Pellicer-Sánchez & Schmitt (2010). *Reading in a Foreign Language* 22, 31–55. https://nflrc.hawaii.edu/rfl/item/207
- Piech et al. (2015). Deep Knowledge Tracing. *NeurIPS 28.* https://papers.nips.cc/paper/5654-deep-knowledge-tracing
- Rawson & Dunlosky (2011). *JEP: General* 140, 283–302. https://doi.org/10.1037/a0023956 ; Rawson & Dunlosky (2012). *Educ. Psychol. Rev.* 24, 419–435.
- Read (1993). *Language Testing* 10, 355–371. https://doi.org/10.1177/026553229301000308
- Roediger & Karpicke (2006). *Psychol. Sci.* 17, 249–255. https://doi.org/10.1111/j.1467-9280.2006.01693.x
- Rowland (2014). *Psychol. Bull.* 140, 1432–1463. https://doi.org/10.1037/a0037559
- Schiefele, Krapp & Winteler (1992). In Renninger, Hidi & Krapp (eds), *The Role of Interest in Learning and Development*, 183–212.
- Sense, Behrens, Meijer & van Rijn (2016). *Topics in Cognitive Science* 8, 305–321. https://doi.org/10.1111/tops.12183
- Sense & van Rijn (2018). *Frontiers in Education* 3:112. https://doi.org/10.3389/feduc.2018.00112
- Settles & Meeder (2016). *ACL 2016*, 1848–1858. https://aclanthology.org/P16-1174/ ; data/code https://github.com/duolingo/halflife-regression
- Simonsmeier, Flaig, Deiglmayr, Schalk & Schneider (2022). *Educational Psychologist* 57, 31–54. https://doi.org/10.1080/00461520.2021.1939700
- Stanovich (1986). *Reading Research Quarterly* 21, 360–407. https://doi.org/10.1598/RRQ.21.4.1
- Swanborn & de Glopper (1999). *Rev. Educ. Res.* 69, 261–285. https://doi.org/10.3102/00346543069003261
- Uchihara, Webb & Yanagisawa (2019). *Language Learning* 69, 559–599. https://doi.org/10.1111/lang.12343
- Verhaeghen (2003). *Psychology and Aging* 18, 332–339. https://doi.org/10.1037/0882-7974.18.2.332
- Verhaeghen, Marcoen & Goossens (1993). *J. Gerontology* 48, P157–P171. https://doi.org/10.1093/geronj/48.4.P157
- Webb (2007). *Applied Linguistics* 28, 46–65. https://doi.org/10.1093/applin/aml048
- Webb, Uchihara & Yanagisawa (2023). *Language Teaching* 56, 161–180. https://doi.org/10.1017/S0261444822000507
- Wesche & Paribakht (1996). *Canadian Modern Language Review* 53, 13–40. https://doi.org/10.3138/cmlr.53.1.13
- Witherby & Carpenter (2022). *JEP: LMC* 48, 483–498. https://doi.org/10.1037/xlm0000996
- Yanagisawa & Webb (2021). *Language Learning* 71, 487–536. https://doi.org/10.1111/lang.12444
- Yanagisawa, Webb & Uchihara (2020). *Studies in Second Language Acquisition* 42, 411–438. https://doi.org/10.1017/S0272263119000688
