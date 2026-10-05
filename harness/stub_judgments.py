"""Offline judgment stubs -- deterministic stand-ins for every routed call.

`--live` swaps this whole module out for the real `AnthropicClient`; everything
else about the run is identical, which is what makes an offline number and a
live number comparable where they are comparable at all.

What offline mode does and does not measure, stated plainly because it changes
how the report must be read:

* **Materiality offline is 1.0 by construction.** The stub reads the event's
  ground-truth label out of the feed's side registry. Offline the materiality
  figures would test the measurement *path*, not the judgment -- so
  `metrics.py` refuses to print them at all rather than printing a 1.0 that
  reads like evidence. See the module docstring there.

* **Concept evidence is extracted from the reply text, not from the answer
  key.** The stub matches glossary terms against what the persona actually
  wrote and reads their grammatical position -- a term whose meaning is asked
  for is an ask, a term a question presupposes or a declarative uses is a use.
  It never consults
  `knows`/`does_not_know`. That is deliberate: it means the ledger can and does
  end up wrong about a persona, which is the entire point of the precision and
  recall numbers. A stub that read the answer key would drive both to 1.0 and
  measure nothing.

* **The briefing stub explains what the ledger does not already record as
  known.** That is the cold-start behaviour in its simplest form, and it is
  what the beginner persona exists to check.

One structural note. The stubs read the system's own state out of the judgment
*context*, the way a real model would: the ledger summary and the proficiency
band the Assessor puts in front of the call. That is the system's belief
state, never the persona's ground truth -- there is no route from a fixture's
answer key into a stub verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from conversational_agent import config
from conversational_agent.judgment import StubClient

from .feed import ScriptedEventFeed
from .personas import Persona
from .responders import definition_asked, term_pattern

# Phrases that mean "I don't understand this", regardless of what else is in
# the sentence. A hedged reply is a gap even when it happens to echo a term.
_GAP_MARKERS = (
    "no idea",
    "hadn't seen this",
    "hadnt seen this",
    "first i'm hearing",
    "first im hearing",
    "wasn't aware",
    "wasnt aware",
    "missed that one",
    "not sure what",
    "don't know",
    "dont know",
    "no clue",
)

_ALREADY_KNEW = ("called it", "saw that", "yeah saw", "old news", "knew that")


@dataclass
class StubRegistry:
    """Everything the stubs need that the judgment context deliberately omits.

    Note what is *not* here: no persona concept sets, no `should_be_material`
    except inside the feed's own side registry, no reply script. The stubs
    cannot reach a persona's ground truth because they are never given it.
    """

    feed: ScriptedEventFeed
    # group name -> the domain glossary for that group (system-visible)
    vocabulary: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # group name -> the group's description (system-visible: the system is
    # handed it at group creation). The offline stand-in for subdomain labels
    # is cut from it -- see `subdomain_labels_for`.
    descriptions: dict[str, str] = field(default_factory=dict)
    # group name -> the events queued for that group, by headline
    event_concepts: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    # Judgment points that should never fire on the persona path.
    unexpected_calls: list[str] = field(default_factory=list)
    # Places where a call's context lacked something the stub needs.
    # Reported, never swallowed.
    contract_notes: list[str] = field(default_factory=list)

    def register(self, persona: Persona) -> None:
        self.vocabulary[persona.group.name] = persona.group.vocabulary
        self.descriptions[persona.group.name] = persona.group.description
        for event in persona.events:
            self.event_concepts[(persona.group.name, event.headline)] = event.concepts

    def note(self, message: str) -> None:
        if message not in self.contract_notes:
            self.contract_notes.append(message)


def believed_known(ctx: dict[str, Any]) -> set[str]:
    """Terms the system currently records as known, read from the briefing context.

    `concept_ledger` is the Assessor's ledger summary, `{state: [terms]}`. This
    is the system's belief, not the truth -- reading it is exactly what a real
    model does with the ledger summary it is handed.
    """
    ledger = ctx.get("concept_ledger") or {}
    return {
        str(term).strip().lower()
        for state in config.KNOWN_STATES
        for term in (ledger.get(state) or [])
    }


def _group_name(ctx: dict[str, Any]) -> str:
    group = ctx.get("group") or {}
    if isinstance(group, dict):
        return str(group.get("name", ""))
    return str(group)


_LABEL_STOP = frozenset({"the", "a", "an", "of", "in", "on", "for", "and", "to", "its", "major"})


def _description_phrases(description: str) -> list[str]:
    """The comma / 'and' separated phrases of a group description, lowercased.

    "Harvest, vintage conditions, appellation rules and the commercial state of
    the wine industry." -> ["harvest", "vintage conditions", "appellation
    rules", "commercial state of the wine industry"].
    """
    text = (description or "").lower().split(":")[-1]
    parts = re.split(r",|;|\band\b|\bplus\b", text)
    out: list[str] = []
    for part in parts:
        words = [w for w in re.findall(r"[a-z0-9&'-]+", part) if w not in _LABEL_STOP]
        phrase = " ".join(words).strip()
        if phrase and phrase not in out:
            out.append(phrase)
    return out


def subdomain_labels_for(terms: list[str], description: str) -> list[dict[str, str]]:
    """The offline stand-in for the system's subdomain labels.

    Live, the labelling calls file each term under a short heading of their
    own choosing. Offline there is no model, and the stub may NOT be handed
    the fixture's taxonomy (that is the answer key the labels are scored
    against). So the label is cut from the one system-visible description of
    the group: the description phrase sharing most tokens with the term, else
    the first phrase. Crude by design -- it exercises the label plumbing and
    the agreement arithmetic; the numbers it produces say nothing about the
    system and the report says so.
    """
    phrases = _description_phrases(description)
    if not phrases:
        return []
    out: list[dict[str, str]] = []
    for term in terms:
        tokens = set(re.findall(r"[a-z0-9&'-]+", str(term).lower()))
        best, score = phrases[0], 0
        for phrase in phrases:
            overlap = len(tokens & set(phrase.split()))
            if overlap > score:
                best, score = phrase, overlap
        out.append({"term": str(term), "subdomain": best})
    return out


def _terms_present(text: str, vocabulary: tuple[str, ...]) -> list[str]:
    """Word-boundary matching -- see `responders.term_pattern` for why."""
    return [t for t in vocabulary if term_pattern(t).search(text or "")]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+|\n+", text or "") if s.strip()]


def extract_concept_evidence(
    reply: str, briefing_text: str, vocabulary: tuple[str, ...]
) -> dict[str, Any]:
    """The offline concept-evidence extractor. Pure function of the text.

    `reply` is the concatenation of the **user's** turns across the whole
    thread, and that word matters. The system's own turns are excluded before
    this function is ever called: an extractor allowed to read them will credit
    understanding for terms the system itself introduced (a live run once
    credited the silent persona with `yields` off a paraphrase it never
    wrote). Only what the user actually said is evidence.

    Rules, in order:

    * A term the sentence **asks the meaning of** -- "what's X", "what does X
      mean", "what do you mean by X", a bare "X?" -- is `asked_about`. The
      strongest and least ambiguous signal there is.
    * A term in a clause that also carries a gap marker is `not_understood`.
    * A term that a question **presupposes** -- "so does that mean yields are
      down?", "why does yields matter here?" -- is `understood`. Under the
      assistant register almost every turn is a question, and the knowledge in
      it lives in what the question takes for granted; an extractor that
      treated every term inside a question mark as an ask would read a
      check-belief as ignorance and could never see a knowledgeable reader.
      This is a text-only rule: "what does X mean" and "so does that mean X is
      affected" are told apart by shape, never by the answer key.
    * A term used in a plain declarative clause is `understood`.
    * A term that appears nowhere in the user's turns produces no evidence at
      all. A thread with no user turns therefore produces nothing, which is
      correct and is the whole point.
    """
    understood: list[str] = []
    not_understood: list[str] = []
    asked_about: list[str] = []

    for sentence in _sentences(reply):
        lowered = sentence.lower()
        present = _terms_present(sentence, vocabulary)
        if not present:
            continue
        has_gap = any(marker in lowered for marker in _GAP_MARKERS)
        for term in present:
            if definition_asked(sentence, term):
                if term not in asked_about:
                    asked_about.append(term)
            elif has_gap:
                if term not in not_understood:
                    not_understood.append(term)
            elif term not in understood:
                understood.append(term)

    # A bare gap admission with no term attached still says something: it is
    # about whatever the briefing just raised.
    lowered_reply = (reply or "").lower()
    if any(marker in lowered_reply for marker in _GAP_MARKERS) and not (
        not_understood or asked_about
    ):
        for term in _terms_present(briefing_text, vocabulary):
            if term not in understood and term not in not_understood:
                not_understood.append(term)
                break

    # Precedence: an explicit question outranks incidental declarative use of
    # the same term in the same reply.
    understood = [t for t in understood if t not in asked_about and t not in not_understood]

    return {
        "understood": understood,
        "not_understood": not_understood,
        "asked_about": asked_about,
        "already_knew": any(marker in lowered_reply for marker in _ALREADY_KNEW),
        "newly_known": [],
        "reasoning": (
            f"Reply used {len(understood)} glossary term(s) in declarative "
            f"clauses, asked about {len(asked_about)}, and showed a gap on "
            f"{len(not_understood)}."
        ),
    }


def interrupt_verdict(ctx: dict[str, Any]) -> dict[str, Any]:
    """The interrupt-timing stub. A flat materiality bar, nothing more.

    Module-level because `interrupt_cases.py` runs the same handler standalone.
    Note what it deliberately does not do: it does not weigh goal deadlines,
    dormancy, or how far behind a group has fallen. That is exactly the
    reasoning the interrupt cases exist to score, and scoring it needs `--live`.
    """
    pending = ctx.get("pending_events", []) or []
    due = ctx.get("groups_needing_attention") or []
    surface = [e for e in pending if float(e.get("materiality_score", 0) or 0) >= 60.0]
    held = len(pending) - len(surface)
    return {
        "should_surface": bool(surface),
        "event_ids": [e["id"] for e in surface],
        "raise_topic": bool(due),
        "framing": (
            f"{len(surface)} update(s) worth knowing about"
            if surface or due
            else "Nothing worth interrupting you with."
        ),
        "held_back_count": max(0, held),
        "reasoning": (
            f"{len(surface)} of {len(pending)} pending events cleared the "
            f"surfacing bar; {len(due)} group(s) want a session."
        ),
    }


def build_stub_client(registry: StubRegistry) -> StubClient:
    """Register a handler for every routed call, so nothing falls through."""
    client = StubClient()

    # --- Materiality -------------------------------------------------------
    def materiality(ctx: dict[str, Any]) -> dict[str, Any]:
        event = ctx.get("candidate_event", {}) or {}
        headline = event.get("headline", "")
        truth = registry.feed.truth_for(_group_name(ctx), headline)
        if truth is None:
            registry.unexpected_calls.append(f"materiality:unlabelled:{headline}")
            truth = False
        return {
            "is_material": bool(truth),
            "materiality_score": 82.0 if truth else 18.0,
            "reasoning": (
                "Scripted feed label: someone conversant in this group would "
                + ("be expected to know this." if truth else "not need this.")
            ),
        }

    # --- Routed call: the briefing, and nothing but the briefing -----------
    def briefing(ctx: dict[str, Any]) -> dict[str, Any]:
        """State the substance and stop.

        The system does not ask the user anything: the briefing schema has no
        field for a question, and `metrics.no_questions_to_user` gates the
        prose against carrying one.

        The briefing glosses any term the ledger does not already record as
        known, pitched by the derived band. That is the cold-start behaviour
        in its simplest form, and it is what the beginner persona exists to
        check.
        """
        group_name = _group_name(ctx)
        vocab = registry.vocabulary.get(group_name, ())
        events = ctx.get("events") or []
        event = events[0] if events else {}
        if not events:
            registry.note(
                "briefing context carried no event; the stub wrote a generic "
                "briefing. raise_topic(event=...) should pass one through."
            )
        headline = str(event.get("headline", "")).strip()
        detail = str(event.get("detail", "")).strip()

        concepts = registry.event_concepts.get((group_name, headline))
        if concepts is None:
            concepts = tuple(_terms_present(f"{headline} {detail}", vocab))

        known = believed_known(ctx)
        band = str(ctx.get("proficiency") or config.BEGINNER)
        # The proficiency prior, used exactly as `config.proficiency_band`
        # describes: only where a term has no evidence either way. Someone the
        # ledger already reads as conversant gets the term used in front of
        # them; someone it reads as a beginner gets it glossed.
        gloss_everything = band in (config.BEGINNER, config.DEVELOPING)
        to_explain = [t for t in concepts if t not in known] if gloss_everything else []

        body = headline or f"Something came up in {group_name}."
        if detail:
            body = f"{body} {detail}"
        glosses = "".join(
            f" ({term}: the term for what's being described here.)"
            for term in to_explain
        )
        return {
            "briefing": f"{body}{glosses}".strip(),
            "topic": headline or group_name,
            "explained_terms": to_explain,
            # `terms_used` is what feeds `note_exposure`: every domain term the
            # briefing put in front of them, glossed or not. It is a record of
            # what was said, and it promotes nothing.
            "terms_used": list(concepts),
            "subdomains": subdomain_labels_for(
                list(concepts), registry.descriptions.get(group_name, "")
            ),
            "reasoning": (
                f"Stated the item plainly; band {band!r} so glossed "
                f"{len(to_explain)} of {len(concepts)} term(s). Asked nothing."
            ),
        }

    # --- Concept evidence, over the whole thread ----------------------------
    def concept_evidence(ctx: dict[str, Any]) -> dict[str, Any]:
        group_name = _group_name(ctx)
        vocab = registry.vocabulary.get(group_name, ())

        # ONLY the user's turns. The system's own words are not evidence
        # about the user, and an extractor that reads them will eventually
        # credit the reader for the writer's vocabulary.
        user_text = "\n".join(
            str(t.get("text", ""))
            for t in ctx.get("thread") or []
            if isinstance(t, dict) and t.get("speaker") == "user"
        )
        briefing_text = str(ctx.get("briefing_we_gave") or "")
        verdict = extract_concept_evidence(user_text, briefing_text, vocab)
        named = list(
            dict.fromkeys(
                verdict["understood"] + verdict["not_understood"] + verdict["asked_about"]
            )
        )
        verdict["subdomains"] = subdomain_labels_for(
            named, registry.descriptions.get(group_name, "")
        )
        return verdict

    # --- Gap routing: can we answer this ourselves, or is a source needed? --
    def gap_routing(ctx: dict[str, Any]) -> dict[str, Any]:
        """Route one user question. Offline, never to the resource path.

        `large` would send the Assessor to live web search, which offline is
        both impossible and the one thing this suite promises not to do. So
        this stub always answers `small`, and the resource path is exercised
        under `--live` only. Reported here rather than left implicit: the
        offline run is not evidence that the large-gap branch works.
        """
        thread = ctx.get("thread") or []
        question = ""
        for turn in reversed(thread):
            if isinstance(turn, dict) and turn.get("speaker") == "user":
                question = str(turn.get("text", ""))
                break
        if not question:
            return {
                "gap_size": "none",
                "explanation": "",
                "search_focus": "",
                "reasoning": "No user question in the thread to route.",
            }
        return {
            "gap_size": "small",
            "explanation": "",
            "search_focus": "",
            "reasoning": (
                "Offline the stub never routes to the resource path, so the "
                "large-gap branch is exercised under --live only."
            ),
        }

    # --- Generative: answering what the user actually asked ----------------
    def thread_reply(ctx: dict[str, Any]) -> dict[str, Any]:
        """The system's half of a turn. Answers, and does not hand back.

        **No question in the answer.** `thread_reply.md` is emphatic that
        turning the question around is "a small betrayal of the trust that
        took", and an offline stub that closed with "does that make sense?"
        would train the harness to tolerate exactly that. Gated by
        `metrics.no_questions_to_user`.

        The answer uses only the term(s) the question itself named; it does
        not introduce adjacent vocabulary.
        """
        group_name = _group_name(ctx)
        vocab = registry.vocabulary.get(group_name, ())
        question = str(ctx.get("question") or "")

        targets = _terms_present(question, vocab)
        if not targets:
            return {
                "answer": "Noted -- nothing much turns on that one either way.",
                "source_url": None,
                "terms_used": [],
                "explained_terms": [],
                "reasoning": "A reaction rather than a question; acknowledged it.",
            }

        # Answer the term whose meaning was asked for, if any. A question that
        # only *presupposes* a term ("so does that mean yields are down?") gets
        # an answer about the situation, not a definition -- defining a word
        # the asker plainly holds is re-explaining, and it would land an
        # `explained` on a term that should be heading for `confirmed`.
        asked = [t for t in targets if definition_asked(question, t)]
        if asked:
            focus = asked[0]
            return {
                "answer": (
                    f"{focus}: in this context it's the measure everyone in the "
                    "trade quotes."
                ),
                "source_url": None,
                "terms_used": [focus],
                "explained_terms": [focus],
                "subdomains": subdomain_labels_for(
                    [focus], registry.descriptions.get(group_name, "")
                ),
                "reasoning": f"Defined {focus!r} inline; {len(targets)} term(s) in play.",
            }

        focus = targets[0]
        return {
            "answer": (
                f"On {focus}: it is the part of this that moves, though not by "
                "much on its own. The effect shows up over the next cycle rather "
                "than immediately."
            ),
            "source_url": None,
            "terms_used": [focus],
            "explained_terms": [],
            "subdomains": subdomain_labels_for(
                [focus], registry.descriptions.get(group_name, "")
            ),
            "reasoning": (
                f"Answered a question that presupposed {focus!r}; no definition "
                "given, none asked for."
            ),
        }

    # --- Query formulation and source selection: never reached on the
    # --- persona path --------------------------------------------------------
    def query_formulation(ctx: dict[str, Any]) -> dict[str, Any]:
        registry.unexpected_calls.append(config.QUERY_FORMULATION)
        return {
            "queries": [],
            "expected_signals": "",
            "domain_confidence": "low",
            "reasoning": "Not exercised by the persona harness; see live_search_eval.",
        }

    def source_selection(ctx: dict[str, Any]) -> dict[str, Any]:
        registry.unexpected_calls.append(config.SOURCE_SELECTION)
        return {
            "events": [],
            "excluded_count": 0,
            "exclusion_notes": "",
            "reasoning": "Not exercised by the persona harness; see live_search_eval.",
        }

    client.register(config.MATERIALITY, materiality)
    client.register(config.CONCEPT_EVIDENCE, concept_evidence)
    client.register(config.GAP_ROUTING, gap_routing)
    client.register(config.INTERRUPT_TIMING, interrupt_verdict)
    client.register(config.BRIEFING, briefing)
    client.register(config.THREAD_REPLY, thread_reply)
    client.register(config.QUERY_FORMULATION, query_formulation)
    client.register(config.SOURCE_SELECTION, source_selection)
    return client
