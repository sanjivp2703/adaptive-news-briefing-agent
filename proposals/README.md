# proposals/

Output of `python -m harness.propose`. One directory per evaluated run, named
by the run id (`run_...`). **Nothing in here has been applied.** A proposal is
a suggestion with its evidence attached; a human reads it, decides, and applies
it -- or does not.

```
proposals/
  README.md            this file
  propose.db           judgment_log for every proposer call (run_id propose_<ts>)
  <run_id>/
    index.md           the list: kind, target, confidence, evidence count, one-line diagnosis
    gate-report.json   the gate's machine-readable report the proposer worked from
    evidence.json      the verbatim evidence items (briefings, turns, judgment rows)
    constraints.json   what a proposal may not reverse (checkpoints, changelog, spec, product rules)
    llm-output.json    the model's structured output plus the call trace (tokens, cost, log id)
    proposal-NN.md     one proposal: diagnosis, exact change, effect, risk, evidence quoted verbatim
    proposal-NN.patch  unified diff against the target, validated with `patch --dry-run`
    proposal-NN.changelog.md   ready-to-paste `docs/prompt-changelog.md` entry
```

## Applying one

```bash
PYTHONPATH=src:. .venv/bin/python -m harness.propose --apply proposals/<run_id>/proposal-02.patch
```

That applies exactly one patch (refusing if it no longer applies cleanly),
inserts the sibling changelog entry at the top of `docs/prompt-changelog.md`, and
prints a reminder to run a fresh live run through the gate. It is the only
path in the tool that writes to `src/` or the changelog, and it never runs
without the flag.

After applying: the changelog entry is marked `PROPOSED, not yet measured`.
Replace that with what the next live run actually measured, in the format the
file uses -- what failed → what changed → what it cost or bought.

## Reading a proposal

- **Diagnosis** is the mechanism the proposer inferred from the evidence. Check
  it against the quoted judgment rows before believing it.
- **Constraints touched** is the proposer's own list of human decisions and
  changelog rules the change comes near, with its argument for why it is not a
  reversal. If the argument is weak, the proposal is wrong, however plausible
  the diff looks.
- **Expected effect** names the gate or metric that should move. If the next
  run does not move it, the entry in the changelog should say so.

`kind` is one of `prompt` (a minimal edit to one prompt file, version bumped in
the patch), `config` (one tunable constant), `fixture_label` (the persona
fixture is wrong, not the system), `harness_metric` (the measurement is
wrong, not the system).

Proposer output is generated locally and is not checked in; only this file is.
