"""Persona-based evaluation harness.

A co-equal first-class deliverable, not QA scaffolding: it is the instrument
that says whether the system's concept ledger actually matches what a synthetic
user knows, and it is the primary way the multi-user architecture is exercised
before a second real user exists.

Everything under test is the real system, assembled by `app.build`. Three things
are substituted -- the clock, the LLM client and the event feed -- and one thing
is added, the persona's responder, which stands where a human sits and reaches
the system only through what a user can do: how long it dwells on a briefing
and how far it scrolls, and the turns it types into the thread.

The headline metric is concept-set agreement: precision and recall of the
system's ledger against the persona's true concept sets. A failure is a
sentence about a specific word.
"""
