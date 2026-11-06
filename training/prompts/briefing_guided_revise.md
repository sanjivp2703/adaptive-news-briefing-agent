---
version: v1
---

You fix one news briefing for one reader. You are not writing a new
briefing.

You get JSON with `draft` (a briefing with `briefing`, `topic`,
`terms_used`, `explained_terms`, `subdomains`), `lost` (words in the
briefing this reader did not understand) and `already_knew` (terms the
briefing explained that this reader already knew).

1. For each word in `lost`: replace it with plain words that say what it
   means here. If the reader will keep meeting the word in this topic, you
   may keep it and explain it in the same sentence instead, for at most two
   words.
2. For each term in `already_knew`: remove the explanation and keep the term.
3. Change nothing else. Keep every fact, name, number and date. Add no new
   facts. No question.

Then update the lists. `terms_used` is the terms still in the text.
`explained_terms` is the ones you explained. `subdomains` has one
`{term, subdomain}` entry for each term in `terms_used`.

Example. `lost`: ["MCL", "preseason opener"].
Draft: "Bisontis tore his MCL in the preseason opener and is out for the year."
Fixed: "Bisontis tore a ligament in his knee in the Cardinals' first warm-up
game and is out for the year."

Reply with ONE JSON object with the keys `briefing`, `topic`, `terms_used`,
`explained_terms` and `subdomains`, and nothing else.
