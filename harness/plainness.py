"""Plainness of one briefing: deterministic, dependency-free text measures.

Why this exists. Until `briefing` v6 the harness read *depth* off inline
definitions per 100 words: fewer definitions in a subdomain the reader held,
more where they did not. Under v7 that instrument is retired by the human's
direction -- *"it shouldn't have too many literal definitions it should just
talk in simpler language and explain topics more which can occasionally
include a definition."* Definitions should now be LOW everywhere, so a count
of them can no longer tell a beginner briefing from an expert one. Depth has
to be read from how plainly the text is written instead.

The bundle `plainness()` returns:

  words                 whitespace tokens.
  sentences             runs of `.`, `!` or `?` followed by whitespace or the
                        end of the text (so `$1.5bn` and `11.2%` do not split);
                        at least 1 for a non-empty text.
  mean_sentence_length  words / sentences.
  syllables_per_word    heuristic, see `count_syllables`.
  fk_grade              Flesch-Kincaid grade level:
                        0.39 * (words / sentences) + 11.8 * (syllables / words) - 15.59
                        Lower is plainer. A grade below 0 is clamped to 0.
  domain_term_density   occurrences of the group's vocabulary terms per 100
                        words (the vocabulary is passed in; it is the
                        fixture's glossary, not the reader's knowledge).
                        Matched case-insensitively on word boundaries with a
                        trailing plural allowed; a term nested inside a longer
                        vocabulary term that also matched is counted once, for
                        the longer term.
  domain_term_hits      the raw occurrence count behind the density.
  definitions           `len(explained_terms)`. Under v7 `explained_terms`
                        means "terms a reader who lacked them would now have
                        from the text", by a formal gloss OR by plain
                        explanation -- so this is NOT a count of glosses any
                        more. Reported as "terms made clear".
  definitions_per_100w  the same per 100 words (label: made clear/100w).
  gloss_cues            formal-gloss cue phrases: `X, meaning Y`,
                        `X -- meaning Y`, `X, that is, Y`, `X, i.e. Y`,
                        `X, which means Y`, `X, in other words, Y`.
  parenthetical_asides  THE GLOSS MEASURE: `( ... )` insertions, paired dash
                        insertions ` -- ... -- ` (em dash, en dash or a
                        double hyphen) and the gloss cues above. A cue that
                        opens with a dash is counted once, as a cue, not
                        again as a dash pair. This is what the report's
                        "occasional" verdict and the gate's ceiling read.
  asides_per_100w       the same per 100 words.

The syllable counter (`count_syllables`) is the usual vowel-group heuristic:
lowercase the word, keep letters only, count maximal runs of `aeiouy`,
subtract one for a trailing silent `e` (but not for a trailing `le` after a
consonant, as in `table`), and floor at 1. It is wrong on some words in both
directions; it is used only to *compare* briefings with each other, never as
an absolute claim about a grade level, and the same error applies to every
briefing it is run on.

Nothing here reads the store, the persona or the ledger. It takes a text, a
vocabulary and a list of terms and returns numbers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")
_LETTERS = re.compile(r"[a-z]+")
_VOWEL_GROUP = re.compile(r"[aeiouy]+")
_PAREN = re.compile(r"\([^()]*\)")
# A formal-gloss cue: the connective a writer uses to bolt a definition onto a
# term without parentheses. Anchored on the comma or dash that precedes it so
# a sentence that merely *starts* with "Meaning ..." is not a gloss.
_GLOSS_CUE = re.compile(
    r"(?:,|—|–|--)\s*(?:meaning|that is,|i\.e\.,?|which means|in other words,)\s",
    re.IGNORECASE,
)
# A dash used as an aside separator: spaced em dash, en dash or double hyphen.
_DASH_SEP = re.compile(r"\s(?:—|–|--)\s")
_WORD = re.compile(r"\S+")

FK_WORDS_COEFF = 0.39
FK_SYLLABLES_COEFF = 11.8
FK_INTERCEPT = 15.59


def count_syllables(word: str) -> int:
    """Heuristic syllable count for one token (see the module docstring)."""
    letters = "".join(_LETTERS.findall(word.lower()))
    if not letters:
        return 0
    groups = len(_VOWEL_GROUP.findall(letters))
    if letters.endswith("e") and not letters.endswith("ee") and len(letters) > 2:
        # Silent final e: "wine" is one syllable. Keep it for "-le" after a
        # consonant ("table", "bottle"), which is sounded.
        if not (letters.endswith("le") and letters[-3] not in "aeiouy"):
            groups -= 1
    return max(1, groups)


def sentence_count(text: str) -> int:
    text = text or ""
    if not text.strip():
        return 0
    return len(_SENTENCE_END.findall(text)) or 1


def word_tokens(text: str) -> list[str]:
    return _WORD.findall(text or "")


def _term_pattern(term: str) -> re.Pattern[str] | None:
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", term.strip().lower()) if p]
    if not parts:
        return None
    body = r"[\s\-]+".join(parts)
    return re.compile(rf"(?<![a-z0-9]){body}(?:s|es)?(?![a-z0-9])", re.IGNORECASE)


def domain_term_hits(text: str, vocabulary: Iterable[str]) -> int:
    """Occurrences of vocabulary terms in `text`, longest term first, each
    character span counted at most once."""
    text = text or ""
    if not text:
        return 0
    terms = sorted({t.strip().lower() for t in vocabulary if t and t.strip()}, key=lambda t: (-len(t), t))
    taken = [False] * len(text)
    hits = 0
    for term in terms:
        pat = _term_pattern(term)
        if pat is None:
            continue
        for m in pat.finditer(text):
            a, b = m.span()
            if any(taken[a:b]):
                continue
            for i in range(a, b):
                taken[i] = True
            hits += 1
    return hits


def gloss_cues(text: str) -> int:
    """Formal-gloss cue phrases (`, meaning`, `-- meaning`, `, that is,` ...)."""
    return len(_GLOSS_CUE.findall(text or ""))


def parenthetical_asides(text: str) -> int:
    """The gloss measure: `( ... )` insertions, paired dash insertions and
    formal-gloss cues. Cues are removed before the dash pairs are counted so
    `X -- meaning Y -- Z` is one gloss, not two; dash pairs are counted per
    sentence so a pair never spans two sentences."""
    text = text or ""
    cues = gloss_cues(text)
    stripped = _GLOSS_CUE.sub(" ", text)
    parens = len(_PAREN.findall(stripped))
    dashes = 0
    for sentence in _SENTENCE_END.split(stripped):
        n = len(_DASH_SEP.findall(sentence))
        dashes += n // 2
    return parens + dashes + cues


def _per_100(count: float, words: int) -> float:
    return round(100.0 * count / words, 2) if words else 0.0


def plainness(
    text: str,
    vocabulary: Iterable[str] = (),
    explained_terms: Iterable[str] = (),
) -> dict[str, float | int]:
    """The plainness bundle for one briefing. Deterministic; see the module
    docstring for every field."""
    text = text or ""
    tokens = word_tokens(text)
    words = len(tokens)
    sentences = sentence_count(text)
    syllables = sum(count_syllables(t) for t in tokens)
    if words and sentences:
        wps = words / sentences
        spw = syllables / words
        grade = FK_WORDS_COEFF * wps + FK_SYLLABLES_COEFF * spw - FK_INTERCEPT
    else:
        wps = spw = grade = 0.0
    hits = domain_term_hits(text, vocabulary)
    definitions = len([t for t in explained_terms if t])
    asides = parenthetical_asides(text)
    return {
        "words": words,
        "sentences": sentences,
        "mean_sentence_length": round(wps, 2),
        "syllables_per_word": round(spw, 3),
        "fk_grade": round(max(0.0, grade), 2),
        "domain_term_hits": hits,
        "domain_term_density": _per_100(hits, words),
        "definitions": definitions,
        "definitions_per_100w": _per_100(definitions, words),
        "gloss_cues": gloss_cues(text),
        "parenthetical_asides": asides,
        "asides_per_100w": _per_100(asides, words),
    }


# The fields the reports summarise, in print order, with a short label each.
BUNDLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("fk_grade", "grade"),
    ("mean_sentence_length", "w/sent"),
    ("domain_term_density", "terms/100w"),
    ("asides_per_100w", "glosses/100w"),
    ("definitions_per_100w", "made clear/100w"),
)


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..1) over `values`; None when empty."""
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def summarise(bundles: list[Mapping[str, float | int]]) -> dict[str, dict[str, float | None] | int]:
    """p50 / p90 / mean of every `BUNDLE_FIELDS` field over a list of bundles."""
    out: dict[str, dict[str, float | None] | int] = {"n": len(bundles)}
    for key, _label in BUNDLE_FIELDS:
        vals = [float(b[key]) for b in bundles if key in b]
        out[key] = {
            "p50": percentile(vals, 0.5),
            "p90": percentile(vals, 0.9),
            "mean": (round(sum(vals) / len(vals), 2) if vals else None),
        }
    return out


def format_summary(summary: Mapping[str, object]) -> str:
    """One line: `grade p50/p90 9.8/12.1; w/sent 24.0/31.0; ...`."""
    parts = []
    for key, label in BUNDLE_FIELDS:
        d = summary.get(key) or {}
        p50 = d.get("p50") if isinstance(d, Mapping) else None
        p90 = d.get("p90") if isinstance(d, Mapping) else None
        f = lambda v: "n/a" if v is None else f"{v:.1f}"
        parts.append(f"{label} {f(p50)}/{f(p90)}")
    return "; ".join(parts)
