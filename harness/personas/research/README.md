# Persona research notes

Background research behind the persona fixtures and the persona memory model.
Nothing here is imported by the harness at run time; `ruff` skips this folder.

## Notes

| file | what it is |
|---|---|
| `startup_founders.md`, `wine.md`, `premier_league.md` | The real, dated news items (headline, detail, source URL, significance) that the five hand-built fixtures replay as their event feeds. |
| `reply_style_profile.md` | Reply-length and style measurements over 1,286 Hacker News comments. The fixtures use its length distribution (§8.2); its register is not used, because forum commenters perform opinions for an audience. |
| `assistant_reply_style_profile.md` | The register study the fixtures do use: what people type after an assistant gives them information, from WildChat-1M human-to-assistant turns (507 qualifying turns, 80 hand-classified). |
| `learning_model.md` | Literature review behind the persona memory model in `harness/learning.py`; implementation notes are in `harness/LEARNING.md`. |
| `knowledge_inference_validity.md` | Literature review of an earlier four-state concept ledger. It led to deleting the `assumed` state; see the banner at its top. |

## Scripts

The `_*.py` scripts built the two style profiles from third-party data. They
are stdlib-only, are not part of the harness or the test suite, and are kept so
the numbers in the profiles can be traced to the rules that produced them.

| script | what it does |
|---|---|
| `_harvest.py` | Fetches Hacker News comments through the public HN API into `_hn_cache/`. |
| `_analyze.py` | Cleans and measures that corpus; the output feeds `reply_style_profile.md`. |
| `_wildchat_harvest.py` | Fetches pages of the `allenai/WildChat-1M` dataset through the Hugging Face datasets-server API into `_wildchat_cache/`. |
| `_wildchat_filter.py` | Filters the cached pages down to user turns that follow an informational reply; the output feeds `assistant_reply_style_profile.md`. |

The harvest scripts make network requests. The fetched text in `_hn_cache/`
and `_wildchat_cache/` belongs to its authors and is not redistributed: both
directories are gitignored, so a fresh clone has the notes and scripts but not
the caches, and the analysis scripts need a harvest run first.
