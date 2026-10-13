# Backlog — news_agent

Single queue for feedback, QA findings, retro items, and open questions. Format and lanes: `iteration-guide.md` at the `agent_pipeline/` root. Rows are never deleted; `/triage` assigns Lane and Size and recommends the next item. Seeded 2026-10-12 from existing status notes.

| # | Item | Source | Lane | Size | Status |
|---|------|--------|------|------|--------|
| 1 | docs/spec.md predates the web app; web-app row says the 'no web UI' non-goal is superseded but the spec is not updated | pipeline-status.md open item 5 | | | open |
| 2 | eval-automation: scored live baseline over all 21 personas on current prompts still open; thresholds for materiality and evidence accuracy not agreed | pipeline-status.md eval-automation row | | | open |
| 3 | Defects that change model input, held for the next evaluated prompt revision | docs/architecture.md 'Known limitations'; pipeline-status.md open item 1 | | | open |
| 4 | Concept-label fragmentation in the ledger | pipeline-status.md open item 2 | | | open |
| 5 | Presupposition confusion in the evidence extractor (measured live only) | pipeline-status.md open item 3 | | | open |
| 6 | Live thresholds for materiality and concept-evidence accuracy are reported, not gated | pipeline-status.md open item 4 | | | open |
| 7 | qa-tester has never run on this project; web app verified live by the builder only | pipeline-status.md QA line | | | open |
