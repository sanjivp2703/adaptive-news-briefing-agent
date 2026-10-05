#!/usr/bin/env python3
"""Filter cached WildChat pages down to post-information user turns. Stdlib only.

The shape we want is *our* shape: the assistant has just delivered information
(an explanation, a factual answer, a summary) in reply to a request for
information, and we keep what the user typed next. That turn is the analogue of
a persona's reply to a briefing.

Everything about the filter is here, in one place, so the acceptance rate in
`assistant_reply_style_profile.md` can be traced to specific rules.

    .venv/bin/python harness/personas/research/_wildchat_filter.py

Writes `_wildchat_cache/qualifying_turns.json` and prints the funnel.
"""
from __future__ import annotations

import glob
import json
import os
import random
import re
import statistics
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "_wildchat_cache")

# --- Rule bank -------------------------------------------------------------
#
# R1  the user turn we keep must be English (row-level language and turn-level
#     language both 'English'), and so must the assistant message before it.
# R2  the turn must be preceded by exactly: user request -> assistant reply.
# R3  the *request* must look like an information request, not a task. It must
#     contain a question mark or open with an interrogative / explain-shaped
#     verb, and must not match TASK_WORDS (write/generate/rewrite/translate/
#     code/story/essay/prompt...), nor HOMEWORK_SHAPE words (question N,
#     options:, calculate, marks, extended essay, 'with reference'), nor
#     enumerated-list requests ('10 episodes', 'top 5'), nor fiction
#     hypotheticals ('what if', 'who would win', 'vs').
# R4  the assistant reply must look informational: 120-6000 chars of prose, no
#     code fence, no > 3 lines that look like code, not a refusal or an
#     apology/correction, not an enumerated list ('Here are 10...', '1. ...'),
#     not fiction
#     (no 'Once upon', 'Title:', 'Chapter', stage-direction asterisks), not a
#     letter/email, not a list of image prompts.
# R5  the kept user turn must be non-empty, <= 120 words, no code fence, no
#     pasted URL-heavy block, not a verbatim repeat of the previous request.
#     (Long turns are overwhelmingly a new pasted task, and anything we would
#     call a *reply* to information is short.)
# R6  no explicit/sexual content in the request, the reply or the kept turn
#     (local keyword screen -- the slim page cache does not carry WildChat's
#     own `toxic` flag, so this is a stand-in for it, stated as such).
# R7  at most 3 qualifying turns per conversation, so one long chat does not
#     dominate the sample.

TASK_WORDS = re.compile(
    r"\b(write|writing|rewrite|re-write|reword|paraphrase|generate|compose|draft|"
    r"create|make me|build|code|coding|script|program|function|python|javascript|"
    r"java\b|c\+\+|html|css|sql|regex|bash|excel formula|vba|"
    r"story|stories|poem|poetry|lyrics|song|rap|essay|article about|blog post|"
    r"cover letter|email|letter to|resume|cv\b|bio for|caption|slogan|tagline|"
    r"prompt for|midjourney|stable diffusion|dall-?e|"
    r"translate|translation|in (french|spanish|german|italian|russian|chinese|"
    r"japanese|arabic|hindi|portuguese)\b|"
    r"roleplay|role-play|role play|act as|pretend|you are (a|an|my)\b|"
    r"continue|next chapter|chapter \d|scene|dialogue|"
    r"multiple choice|choose the correct|which of the following|true or false|"
    r"fill in the blank|solve|homework|assignment|worksheet|"
    r"summarize this|summarise this|proofread|fix (this|my|the)|correct (this|my|the)|"
    r"improve (this|my|the)|shorten|expand this|outline for|table of contents|"
    r"list of \d+|give me \d+|top \d+|\d+ (episodes|examples|ways|ideas|names|"
    r"reasons|tips|facts|questions|scenes|characters)|"
    r"what if\b|who would win|vs\.?\b|versus|fight|battle|"
    r"question \d|options:|calculate|find the (value|probability|smallest|"
    r"largest)|p\[|extended essay|with (a |on )?reference|cite|citation|"
    r"in \d+ words|word essay|marks?\)|\(\d+ marks|ad campaign|print ad|"
    r"advert|marketing copy)\b",
    re.I,
)

INFO_OPENERS = re.compile(
    r"^\s*(what|what's|whats|why|how|who|whom|whose|when|where|which|is|are|was|"
    r"were|do|does|did|can|could|would|should|will|has|have|had|explain|tell me|"
    r"describe|define|summarize|summarise|compare|difference between|"
    r"i want to know|i'd like to know|i need to know|any idea|so\b|and\b|but\b)",
    re.I,
)

ASSISTANT_FICTION = re.compile(
    r"^\s*(title:|once upon|chapter|dear\b|subject:|to whom|hi\b|hello\b|\*|\"|“|"
    r"scene|int\.|ext\.|verse|\[)",
    re.I,
)
ASSISTANT_REFUSAL = re.compile(
    r"^\s*(i'?m sorry|sorry|as an ai|i cannot|i can'?t|unfortunately, i|"
    r"i apologi[sz]e|apologies|ah, i apologi[sz]e|my apologies|i am unable|"
    r"i'm unable|i do not have|i don'?t have (access|the ability)|"
    r"here are \d+|here'?s a list|sure! here|certainly! here|"
    r"\d+\.\s)",
    re.I,
)
CODE_LINE = re.compile(
    r"^\s*(def |class |import |from \S+ import|#include|<\w+|\{|\}|\$|//|/\*|"
    r"function\b|var |let |const |return\b|print\(|console\.|SELECT |INSERT |"
    r"public |private |if \(|for \(|while \(|end$|fi$)"
)
CODE_FENCE = re.compile(r"```")
EXPLICIT = re.compile(
    r"\b(sex|sexual|sexy|nude|naked|erotic|fetish|kinky|orgasm|porn|nsfw|"
    r"hypnoti[sz]e|tights|bondage|breasts?|penis|vagina|slut|fuck|cum\b|"
    r"seduc|lingerie|feet worship|foot fetish|arous)",
    re.I,
)
URL = re.compile(r"https?://\S+")


def _words(text: str) -> int:
    return len([w for w in re.split(r"\s+", (text or "").strip()) if w])


def _looks_like_code(text: str) -> bool:
    if CODE_FENCE.search(text):
        return True
    lines = [l for l in text.split("\n") if l.strip()]
    codey = sum(1 for l in lines if CODE_LINE.match(l))
    return codey > 3 or (lines and codey / len(lines) > 0.25)


def is_info_request(text: str) -> bool:
    if not text or _words(text) > 150:
        return False
    if CODE_FENCE.search(text):
        return False
    if TASK_WORDS.search(text):
        return False
    return ("?" in text) or bool(INFO_OPENERS.match(text))


def is_informational_reply(text: str) -> bool:
    if not text:
        return False
    n = len(text)
    if n < 120 or n > 6000:
        return False
    if _looks_like_code(text):
        return False
    if ASSISTANT_FICTION.match(text) or ASSISTANT_REFUSAL.match(text):
        return False
    # Stage directions / heavy quoting mark roleplay and fiction.
    if text.count("*") >= 4 or text.count("\n\"") >= 3:
        return False
    return True


def is_keepable_user_turn(text: str, previous_request: str) -> bool:
    if not text or not text.strip():
        return False
    if _words(text) > 120:
        return False
    if CODE_FENCE.search(text):
        return False
    if len(URL.findall(text)) > 1:
        return False
    if text.strip().lower() == (previous_request or "").strip().lower():
        return False
    return True


def main() -> int:
    pages = sorted(glob.glob(os.path.join(CACHE, "rows_*.json")))
    funnel = {
        "pages": len(pages), "rows": 0, "rows_english": 0, "rows_not_toxic": 0,
        "assistant_messages_after_info_request": 0,
        "info_replies_after_info_request": 0,
        "info_replies_with_a_next_user_turn": 0,
        "kept_turns": 0, "kept_conversations": 0,
    }
    kept = []
    for page in pages:
        with open(page) as f:
            data = json.load(f)
        for row in data.get("rows", []):
            funnel["rows"] += 1
            if row.get("language") != "English":
                continue
            funnel["rows_english"] += 1
            conv = row.get("conversation") or []
            if any(EXPLICIT.search(m.get("content") or "") for m in conv):
                continue
            funnel["rows_not_toxic"] += 1
            per_conv = 0
            for i in range(1, len(conv)):
                a = conv[i]
                if a.get("role") != "assistant":
                    continue
                req = conv[i - 1]
                if req.get("role") != "user":
                    continue
                if (req.get("language") or "English") != "English":
                    continue
                if not is_info_request(req.get("content") or ""):
                    continue
                funnel["assistant_messages_after_info_request"] += 1
                if not is_informational_reply(a.get("content") or ""):
                    continue
                funnel["info_replies_after_info_request"] += 1
                nxt = conv[i + 1] if i + 1 < len(conv) else None
                if nxt is None or nxt.get("role") != "user":
                    # Informational reply that ended the conversation: this is
                    # the "no reply" outcome and is counted, not dropped.
                    kept_none = {
                        "conversation_hash": row.get("conversation_hash"),
                        "model": row.get("model"), "timestamp": row.get("timestamp"),
                        "position": i, "ended_here": True,
                        "request": req.get("content"), "assistant": a.get("content"),
                        "assistant_words": _words(a.get("content")),
                        "user_turn": None,
                    }
                    kept.append(kept_none)
                    continue
                funnel["info_replies_with_a_next_user_turn"] += 1
                if (nxt.get("language") or "English") != "English":
                    continue
                if not is_keepable_user_turn(nxt.get("content") or "", req.get("content") or ""):
                    continue
                if per_conv >= 3:
                    continue
                per_conv += 1
                further_user = any(
                    m.get("role") == "user" for m in conv[i + 2:]
                )
                kept.append({
                    "conversation_hash": row.get("conversation_hash"),
                    "model": row.get("model"), "timestamp": row.get("timestamp"),
                    "position": i, "ended_here": False,
                    "request": req.get("content"), "assistant": a.get("content"),
                    "assistant_words": _words(a.get("content")),
                    "user_turn": nxt.get("content"),
                    "user_words": _words(nxt.get("content")),
                    "is_last_user_turn": not further_user,
                })
                funnel["kept_turns"] += 1
    funnel["kept_conversations"] = len({k["conversation_hash"] for k in kept if k["user_turn"]})
    out = os.path.join(CACHE, "qualifying_turns.json")
    with open(out, "w") as f:
        json.dump({"funnel": funnel, "turns": kept}, f, indent=1)
    for k, v in funnel.items():
        print(f"{k:45s} {v}")
    with_turn = [k for k in kept if k["user_turn"]]
    if with_turn:
        ws = [k["user_words"] for k in with_turn]
        print(f"user_words median={statistics.median(ws)} p25={sorted(ws)[len(ws)//4]} p75={sorted(ws)[3*len(ws)//4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
