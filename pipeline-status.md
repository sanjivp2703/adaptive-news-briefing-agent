# Pipeline status — Adaptive News Briefing Agent (news_agent)

Build state per feature slice. The project was specified first
(`docs/spec.md`), decomposed into slices, built, then put through repeated
cycles of live evaluation and human decision (`docs/checkpoints.md`).

Stack: Python 3.11+ · SQLite (single file, user-scoped throughout) · direct
`anthropic` SDK Messages calls, one per judgment point (no agent framework) ·
Anthropic server-side web search · standard-library CLI and web server ·
per-call model routing, overridable by environment variable.

| Slice | Brief | Status | Notes |
|-------|-------|--------|-------|
| foundation | (removed; superseded by docs/spec.md) | done | Store with structural user scoping, the traced judgment wrapper (structured output, versioned prompts), search pipeline, config with per-call routing. Rebuilt for the concept ledger on 2026-09-06. |
| groups-and-goals | (removed) | done | Group and goal lifecycle. One active goal per group, enforced by a partial unique index. Closing a goal leaves the ledger untouched. |
| monitor-and-background-polling | (removed) | done | Scheduled polling per (user, group), independent of sessions. Materiality judged per group, not per reader. Persona users are structurally blocked from live search. |
| assessor | (removed) | done | Rebuilt 2026-09-06: brief, then stop; no quiz, no score, no decay. Owns briefing, thread replies, gap routing and concept-evidence extraction. |
| orchestrator-session | (removed) | done | Sole owner of what reaches the user. Pull-only delivery; held-back events stay queued. Group shortlisting is deterministic counting. |
| persona-eval-harness | (removed) | done | Rebuilt 2026-09-08 for the thread model: personas ask questions. Hand-built and generated personas, memory model, hidden probe, reading simulation, leak checks. Offline mode is byte-reproducible. |
| llmops-console | (removed) | done | `ledger`, `models`, `prompts`, `traces`, `trace`, `stats`, `runs`. One log row per model call. User-scoped by default. |
| eval-automation | (no brief; human direction 2026-09-14) | in progress | Regression gate and prompt proposer are built. Open: a scored live baseline over all 21 personas on current prompts; agreed thresholds for materiality and evidence accuracy. |
| web-app | (no brief; human direction 2026-10-05) | done | Browser surface over the same objects the CLI drives (`src/conversational_agent/web/`). Reader pages and builder console are separate modules, enforced by test. Observed dwell and scroll. Background poller, off by default. Loopback only, no login. `--demo` serves a recorded session with no key. Supersedes the spec's "no web UI" non-goal; the spec is not yet updated. No design-agent pass. |
| repo-hardening | (no brief; human direction 2026-10-05) | done | Audit and cleanup for external review: two CLI crashes fixed with regression tests, shared reporting module, resumable migration, stale quiz-era names and comments removed, lint and CI added, docs restructured under `docs/`. Slice briefs removed as stale. |
| training-track | (personal learning track; not in spec) | in progress | Distillation data generated and packaged; local-model seat built. Stage 1 fine-tune notebook written, not yet run. Stages 2 (DPO) and 3 (RLAIF) planned. See `training/README.md`. |

The slice briefs described the pre-redesign quiz model and were removed on
2026-10-05; `docs/spec.md` is authoritative.

## Open items

1. Defects that change model input, held for the next evaluated prompt
   revision: see "Known limitations" in `docs/architecture.md`.
2. Concept-label fragmentation in the ledger.
3. Presupposition confusion in the evidence extractor (measured live only).
4. Live thresholds for materiality and concept-evidence accuracy are
   reported and not gated.
5. `docs/spec.md` predates the web app.

QA: not run (qa-tester has not been run on this project; the web app was verified live in a browser by the builder on 2026-10-05)
Deploy: not deployed (local only; the web surface has no authentication)
