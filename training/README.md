# training/ — moving the briefing writer to a small local model

The product writes its briefings with a frontier API model. This track asks
how much of that one job a small open-weight model can take over, using the
evaluation harness as the fixed yardstick. It changes nothing about how the
product works today.

**Where it stands (2026-10-05)**

| Step | State |
|---|---|
| Distillation data: 500 teacher briefings generated, 452 pass the automated checks | done |
| Packaging: 415 train / 45 validation, stratified by reader profile | done |
| Local-model seat in the product (`LOCAL_MODEL_*`) | done, tested |
| Stage 1: LoRA fine-tune of Qwen2.5-3B-Instruct (`notebooks/01_sft_colab.ipynb`) | written, **not yet run** |
| Stage 2: DPO on preference pairs | planned, no code |
| Stage 3: RLAIF (reward model + GRPO, the regression gate as guardrail) | planned, no code |

No trained-model result exists yet, so none is reported here.

The rest of this page is written for someone new to fine-tuning. Technical
names are in brackets the first time they appear. The data files it describes
are generated locally and are not checked in; `data/sample_rows.jsonl` shows
the row format.

## What a training example is

Every time the system writes a briefing, it logs two things:

- **the packet** it handed to the model — the group, the ledger of which words
  you already know, the news event, what was raised before. In the data files
  this is the `input` field. It is exactly what the model saw, byte for byte.
- **the answer** the model wrote back — the briefing text, the topic label, the
  list of terms it used and the ones it defined. This is the `output` field.

One packet plus one answer is one **training example**. A model learns by
being shown many of these pairs and being nudged, each time, toward producing
that answer when given that packet. That is all "fine-tuning" (specifically
*supervised fine-tuning*, SFT) means: show it the packet, show it the answer,
repeat.

The `system` field is the instructions the model was given (the briefing
prompt). It is included only on rows written under the *current* prompt
version, because older rows were written under older instructions we no
longer have the exact text of.

## What distillation is

We are not inventing answers. Claude already wrote good briefings, and the
log kept them. **Distillation** means training a small model to copy what a
larger model did — the big model is the teacher, the log is the lesson book.
The small model will not be as good, but it can be good *enough* for one
narrow job, and it runs for free, on your machine, with nothing sent anywhere.

## The files in `data/`

Run `PYTHONPATH=src:. .venv/bin/python training/extract_sft.py` to (re)build:

- `sft_briefings.jsonl` — **everything**: every briefing call from every live
  run, one per line, including the ones that failed. Each row carries a
  `quality` block: did the call error, does the briefing contain a question
  mark (it never should), how many words, how many terms it defined, and
  `reask` — whether the person later asked about a term the briefing had
  just defined (a sign the definition did not land).
- `sft_briefings.clean.jsonl` — the **clean split**: the rows worth training
  on. No errors, no question marks, only recent prompt versions
  (v5 to v7), duplicates removed (same event, same ledger), and the run they
  came from must not have failed its automated checks.
- `sft_briefings.clean_nogate.jsonl` — the same, except the last rule is
  dropped. As of now every live run has failed its checks, but on the
  *ledger* measurement, not on the briefings themselves — so the strict clean
  file is empty and this one has the 8 usable rows. Whether to train on them
  is your call; the script writes both so the choice is visible.

The honest summary: 94 briefing calls logged, 82 with an answer, all written
by `claude-sonnet-5`; only 9 under v5/v6. That is far too few to train on. The
extractor exists so the pile grows automatically as more runs happen.

## Serving a small model locally, later

Once there is a trained model (or just to try an off-the-shelf one), run it
with [Ollama](https://ollama.com) — it gives any open model a web address the
system can talk to:

```bash
ollama run qwen2.5:7b        # downloads and serves the model on port 11434
```

Then point the system at it with two environment variables:

```bash
export LOCAL_MODEL_BASE_URL=http://localhost:11434/v1
export LOCAL_MODEL_NAME=qwen2.5:7b
```

That is the whole switch. With those set, the **briefing** call goes to the
local model and every other call still goes to Claude. Unset them and nothing
is different from before. Optional extras: `LOCAL_MODEL_API_KEY` if the server
wants a password, and `LOCAL_MODEL_POINTS=briefing,thread_reply` to send more
than one kind of call local (default is `briefing` only).

The switch is honest about itself. `cli models` shows the local name in the
briefing row, and every briefing row in the judgment log records the local
model as its author, so you can always tell who wrote what:

```bash
PYTHONPATH=src:. .venv/bin/python -m conversational_agent.cli models
PYTHONPATH=src:. .venv/bin/python -m conversational_agent.cli traces --point briefing
```

Any server that speaks the common "chat completions" format works the same
way — vLLM, LM Studio, TGI, and most hosted open-model providers — just change
the URL and name.

## Running the cheap regression with the local model in the briefing seat

The harness's cheap mode replays a scripted persona against the real system,
voicing the persona with a cheap model. With the two variables above set, the
briefings in that run come from your local model, and the run's report shows
it. One command per persona; the three that have been run most are:

```bash
export LOCAL_MODEL_BASE_URL=http://localhost:11434/v1
export LOCAL_MODEL_NAME=qwen2.5:7b
for p in sam_beginner pilar_wine dana_startup; do
  PYTHONPATH=src:. .venv/bin/python -m harness.run --live --cheap --rounds 4 \
      --persona $p --db eval-runs/local/$p.db --json eval-runs/local/$p.json \
      --materiality-cache eval-runs/cache/materiality.json
done
```

Claude credentials are still needed for the non-briefing calls. Afterwards,
`extract_sft.py --db eval-runs/local/sam_beginner.db` pulls that run's
briefings into the same format, tagged with the local model's name, so the
teacher's and the student's briefings sit side by side in one file.

## Making more examples on purpose: `generate_sft.py`

Eight real examples is not a lesson book. `generate_sft.py` makes a few hundred
more by asking the teacher (Claude, the same model and the same briefing prompt
the product uses today) to brief invented users about real events.

**What it does.** It takes every verified real event in the persona fixtures
(`harness/personas/*.json` and `generated/*.json` — 56 distinct events across
eight groups), and for each one invents several *user states*: what the ledger
would hold for a complete beginner, someone partway in, an expert, an expert in
one corner of the group only, and a returning reader who has read a lot of our
definitions. From each state it assembles the exact packet the product would
hand to the briefing call — the same keys, the same proficiency band and
per-subdomain readout computed with the same arithmetic the store uses — and
sends it through the project's real `Judge`, so the production prompt, output
schema, effort setting and tracing all apply. Every call lands in a
`judgment_log` (`training/data/generated.db`) exactly like a live run's would.
The answers are written to `training/data/sft_generated.jsonl` in the same row
format as `extract_sft.py`, so the two files simply concatenate.

**Why invent user states instead of only using recorded ones.** The recorded
ones are nearly all the same state: a new user with an empty ledger, told about
the first event. A model trained on that would learn to define everything for
everyone. The briefing prompt's hard parts — do not gloss a term this person has
already used, pitch one subdomain as to a peer and another as to a beginner,
lean on a reading pattern only once it has enough behind it — never appear in
the recorded data because no real user has been around long enough yet. The
synthetic states put those cases in front of the teacher on purpose, and the
`meta` block on each row says which profile produced it, so the mix can be
rebalanced later without regenerating.

**The one command to run it live** (it reads `ANTHROPIC_API_KEY` from the
environment; never put the key in a script or a commit):

```bash
export ANTHROPIC_API_KEY=...   # see .env.example
PYTHONPATH=src:. .venv/bin/python training/generate_sft.py
```

Add `--dry-run` first to see the coverage report and three sample packets
without spending anything; `--offline` runs the whole pipeline with a stub
teacher (what the tests do). `--teacher opus` uses the larger model; `--budget`
(default $6) stops the run before the call that would cross it; `--limit`
(default 500) and `--states-per-event` (default 12) size the run; `--seed`
makes the same packets again. Interrupted runs resume: a packet already in the
output file is skipped.

**What $5 buys.** About 500 briefings at Sonnet prices (roughly 900 tokens in
and 770 out per call, $0.0095 each, $4.75 for 500 — the running total printed
during the run uses the real token counts, which exclude the cached prompt, so
it slightly understates the bill). Each row is also checked against the hard
rules the eval gate applies to briefings: no question mark, only the packet's
event referenced, every number in the briefing present in the event, no term
defined that the ledger already marks known, `explained_terms` a subset of
`terms_used`. Rows that fail are kept and flagged (`quality.checks`); the ones
that pass everything go to `sft_generated.clean.jsonl`, which is the file to
train on.

**Reading the coverage report.** It is printed before any call is made:

- *per group* — packets per group; proportional to how many distinct events
  each group has (Premier League has the most), not a judgement of importance;
- *per profile* — the five user-state profiles, equal by construction;
- *per proficiency band* — how the synthesised ledgers came out under the real
  band rule; `fluent` is rare because it needs 25 known terms and few group
  vocabularies are that large;
- *with a conversant/fluent subdomain* — how many packets exercise the
  "depth per topic" rule, the one the recorded data never reaches;
- *with a usable reading_pattern* — packets where the skip prior is allowed
  to shift the pitch (three or more resolved skips);
- *with an empty ledger* — brand-new users, the production common case.

## Stage 1: training the small model on Colab (`package_sft.py` + `notebooks/01_sft_colab.ipynb`)

This is the first real training step. It happens on Google Colab, because
training needs a graphics card that a typical laptop does not have; the
free tier gives you a T4 for a few hours at a time, and this is sized to fit
in one sitting.

### The two data files

`package_sft.py` turns the clean rows into the shape a chat model is trained
on: for every row, the instructions (the *current* briefing prompt, plus the
same "Output format" tail the local switch appends at serve time, byte for
byte), then the packet exactly as the system sends it, then Claude's answer
as one JSON object. It reads every count from the files, so it works the same
when the rows are regenerated under a new prompt version:

```bash
PYTHONPATH=src:. .venv/bin/python training/package_sft.py        # add --seed N for a different shuffle
```

It writes two files into `training/data/`:

- `sft_train.jsonl` — about 90% of the examples. The model learns from these.
- `sft_val.jsonl` — the other 10%, held back and never trained on. They are
  only used to measure, so the loss on them is the honest number.

The split keeps every reader profile (beginner, partial, expert, and so on)
in the same 90/10 ratio, and the handful of real recorded rows always go to
train. The script prints the counts per split and per profile, and a rough
token count per example (the longest one decides how much memory training
needs; the notebook re-measures this with the model's own tokenizer). Two
things it puts back that the clean files had dropped: the one-sentence
`reasoning` field the schema requires (restored from the judgment log the row
came from — a row whose reasoning cannot be found is dropped, never invented),
and an empty `subdomains` list for the few rows written under the older v5
prompt, which never had one; `meta.backfilled` says which.

### Step by step on Colab

1. Open [colab.research.google.com](https://colab.research.google.com),
   choose **Upload**, and pick `training/notebooks/01_sft_colab.ipynb`.
2. **Runtime → Change runtime type → T4 GPU → Save.** Without this step
   nothing works; the first cell checks and tells you.
3. Run the cells in order, top to bottom (Shift+Enter on each, or
   **Runtime → Run all**). Every cell has a note above it saying what it does
   and what you should see.
4. When the upload cell asks, choose both `sft_train.jsonl` and
   `sft_val.jsonl` from `training/data/`. It also asks to connect your Google
   Drive; say yes — see the next section for why.
5. The training cell is the long one, roughly an hour. Watch the two loss
   numbers: `loss` (on the training examples) and `eval_loss` (on the
   held-back ones). Both should fall and then flatten. The plot cell after it
   draws them so the pattern is easy to see.
6. The "Try it" cell shows two held-back packets with the model's briefing
   next to Claude's, and runs the same hard rules the data generator used.
   This is your first look at what the training bought.
7. The save cell converts the model to a GGUF file (the format the local
   server reads, about 2 GB) and puts it in your Drive under
   `MyDrive/news-briefer/`, or downloads it directly if you chose that.
8. The last cell prints the exact commands for running it locally with
   Ollama and switching the briefing seat over (`LOCAL_MODEL_BASE_URL` and
   `LOCAL_MODEL_NAME`, as described above).

### If the GPU is not available, or the session disconnects

Colab's free tier sometimes has no T4 to give ("Cannot connect to GPU
backend"). Wait an hour or two and try again, ideally outside US daytime;
or under **Runtime → Change runtime type** try whichever GPU it does offer.
Do not run it on a CPU runtime — it would take days.

Colab also disconnects free sessions after long inactivity or roughly twelve
hours, and when it does its disk is wiped. The notebook guards against this in
two ways: with Drive connected, it saves a checkpoint of the trained part to
your Drive at the end of every epoch, and the training cell resumes from the
newest checkpoint it finds there. So a disconnect costs you at most one
epoch. To recover: reconnect, run the cells from the top again (the install
and upload steps repeat; the model download is quick), and the training cell
picks up where it left off. Keep the browser tab open and the laptop awake
while it trains.

### What to expect, honestly

A three-billion-parameter model trained on a few hundred examples will
produce briefings that look like Claude's: the right shape, the right
register, valid JSON, no closing question most of the time. It will be worse
in the ways that are hard to check with a rule — it will sometimes gloss a
term the ledger says the reader already knows, occasionally invent a number,
and it has a thinner sense of which detail matters. The hard-rule pass rate in
the "Try it" cell and the three-persona regression run are the measure; if it
passes the rules on most packets, stage 1 has done its job, which is to give
the later stages (preference training) something worth improving rather than
a finished replacement for the teacher. Every briefing the local model writes
is logged with its name as the author, so nothing it writes can be mistaken
for Claude's.

## Plan as decided on 2026-10-02

Three stages, then stop: **SFT (stage 1, this notebook) -> DPO (stage 2) ->
one small RLAIF run (stage 3, GRPO against a Claude-labelled reward model,
capped at about $40)**. RLHF is dropped: it differs from RLAIF only in who
writes the preference labels, so it teaches no new method and would cost
hours of hand-labelling. If the difference human labels make is ever of
interest, label ~100 pairs by hand and compare the two reward models; that
is an afternoon, not a stage. The product does not depend on any of this;
the demo app runs on Claude, with the local model as an optional toggle in
the briefing seat once stage 1 has produced a GGUF.
