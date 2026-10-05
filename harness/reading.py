"""Reading behaviour, simulated per persona -- attention, never comprehension.

The interaction model now records how a briefing was *read*: `dwell_ms` and
`scroll_fraction`, from which the store derives a coarse `read_quality` band.
The harness has to produce that signal, because a persona that never generates
it leaves the whole path untested, and the path has a specific hazard.

**This is the third time an attention signal has tried to become a knowledge
claim in this project.** First `assumed` promoted a term after three
unquestioned exposures -- deleted, after measuring anti-predictive at every
bar. Then the live `concept_evidence` extractor credited `theo_silent` with
understanding `yields` off topically-adjacent paraphrase that never used the
word. Reading behaviour is the same shape and a more tempting one, because it
arrives as a number and numbers look like measurements. So it is simulated
here, recorded through the contract, and `metrics.reading_regression` gates
hard that it moves no concept's state.

The numbers are not invented. A briefing is prose, and silent reading of
English prose runs about 238 words per minute (Brysbaert 2019, meta-analysis of
190 studies) -- that is `BASELINE_WPM` below, and it is what makes "did they
spend as long on this as reading it would take" a computable question rather
than a threshold someone picked. Word length is taken at 5.1 characters
including the following space, the standard convention behind every
words-per-minute figure of this kind.

What each profile is *for*:

    careful   -- reads the whole thing, sometimes twice. The control.
    skimmer   -- scans most of it fast. The common case, and the one that
                 produces `skimmed` at every briefing length.
    bouncer   -- fine until the briefing gets long, then bails. This is the
                 profile that makes read quality a measurement of the BRIEFING
                 rather than of the person: its output is a function of a
                 length the system chose.

Every draw is seeded from the persona and the round, so two runs read
identically.

**Reading is now state-dependent, and every reading act records a hidden
intent.** A reader who already holds a briefing's terms may skip it *because*
they hold them (an informed skip); anyone may skip it out of laziness; and
between those, how far they read shifts with how much of it is new to them.
`ReadingProfile.read_with_intent` takes the persona's `known_share` and
`story_known` from the memory model and returns both the observable reading
(dwell, scroll) and a `ReadingIntent` that says what the act *was*. The intent
is ground truth and harness-only: `record_reading` still receives dwell, scroll
and source and nothing else, and an informed skip and a lazy skip are drawn from
the same dwell/scroll distribution, so the system cannot tell them apart from
the reading alone -- it has to work it out later from evidence about the
skipped briefing's terms, which is exactly the capability the
`skip_classification` metric scores.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# Brysbaert, M. (2019), "How many words do we read per minute? A review and
# meta-analysis of reading rate", Journal of Memory and Language 109.
BASELINE_WPM = 238.0

# Characters per word including the trailing space. The convention behind every
# wpm figure; using it here means `expected_ms` is comparable to the literature
# rather than to an arbitrary character count.
CHARS_PER_WORD = 5.1


def expected_read_ms(briefing_chars: int, wpm: float = BASELINE_WPM) -> float:
    """How long reading this briefing properly would take, in milliseconds."""
    words = max(1.0, briefing_chars / CHARS_PER_WORD)
    return words / max(1.0, wpm) * 60_000.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def reference_read_quality(
    dwell_ms: int | None,
    scroll_fraction: float | None,
    briefing_chars: int,
) -> str:
    """The harness's own derivation of the four-band read quality.

    Kept as an independent implementation on purpose. The store exports its
    own `read_quality`; the harness derives the band from the same inputs and
    `metrics.read_quality_agreement` checks the two agree -- which is only a
    check while the two are separate pieces of code.

    Two inputs, and both have to clear their bar:

      * **ratio** -- dwell against the time reading it would actually take. A
        long dwell on a short briefing is not attention, it is a tab left open,
        so the top band needs the ratio to be high *and* the scroll to be
        complete rather than either alone.
      * **scroll** -- how much of it went past. Scrolling nothing is decisive:
        no amount of dwell on an unscrolled briefing means it was read.

    The bands are coarse by design. This is the same reasoning the concept
    ledger uses for its own coarseness: a four-way split that is roughly right
    costs one wrong engagement figure, where a continuous "attention score"
    would invite exactly the promotion this signal must never make.
    """
    ratio = (max(0, dwell_ms or 0)) / expected_read_ms(briefing_chars)
    scroll = _clamp(float(scroll_fraction or 0.0), 0.0, 1.0)

    if scroll < 0.15 or ratio < 0.20:
        return "skipped"
    if scroll < 0.55 or ratio < 0.65:
        return "skimmed"
    if ratio < 1.60:
        return "read"
    return "studied"


# The reading acts, in depth order. The ordinal is what `attention_agreement`
# correlates the system's `attention` score against.
READING_ACTS: tuple[str, ...] = ("skip", "skim", "read", "study")
DEPTH_ORDINAL: dict[str, int] = {act: i for i, act in enumerate(READING_ACTS)}
_ACT_FOR_BAND = {"skipped": "skip", "skimmed": "skim", "read": "read", "studied": "study"}

# How strongly `known_share` shifts a non-skip reading: at known_share = 1 the
# reader's attention and thoroughness fall by this fraction (toward a skim), at
# 0 they rise by it (toward a read).
KNOWN_SHARE_SHIFT = 0.30


@dataclass(frozen=True)
class ReadingIntent:
    """What one reading act WAS, as opposed to what it looked like.

    Ground truth, harness-only. `act` is one of `READING_ACTS`; `informed` is
    True for an informed skip, False for a lazy skip (or a length-induced
    bail that the derivation bands as `skipped`), None for any non-skip.
    `known_share` and `story_known` are the memory-model inputs the decision
    was made from, kept so the decision can be audited.
    """

    act: str
    informed: bool | None
    known_share: float
    story_known: bool

    @property
    def depth(self) -> int:
        return DEPTH_ORDINAL[self.act]


@dataclass(frozen=True)
class ReadingProfile:
    """How one persona reads. A fixture field, not a behaviour of the system.

    `patience_chars` is the field that matters. Below it the persona reads the
    way it normally reads; above it, attention decays in proportion to the
    overrun. That is what turns briefing length into an observable consequence
    instead of a style preference, and it is why the engagement report can say
    something about the briefing rather than only about the reader.
    """

    name: str
    # Reading speed. A skimmer's eyes move faster over the same prose.
    wpm: float = BASELINE_WPM
    # How much of a comfortable-length briefing they intend to get through.
    attention: float = 1.0
    # Dwell multiplier on top of what the scrolled portion would take. Above 1
    # means re-reading; below 1 means skipping words while scrolling past them.
    thoroughness: float = 1.0
    # Length past which they start bailing, in characters.
    patience_chars: int = 1400
    # Spread on both draws, as a fraction. 0.0 makes a persona a metronome,
    # which is the same mistake `reply_style_profile.md` section 8.2 names for
    # reply length.
    jitter: float = 0.25
    # P(informed skip) = skip_when_known * known_share: the more of a briefing
    # this reader already holds, the likelier they are to skip it on purpose.
    skip_when_known: float = 0.3
    # P(lazy skip), regardless of what is in it. A disengaged fixture may set
    # this higher via a `reading` object.
    lazy_skip: float = 0.03

    @classmethod
    def from_raw(cls, raw: dict | None) -> ReadingProfile:
        if not raw:
            return cls(name="careful")
        return cls(
            name=str(raw.get("name", "careful")),
            wpm=float(raw.get("wpm", BASELINE_WPM)),
            attention=float(raw.get("attention", 1.0)),
            thoroughness=float(raw.get("thoroughness", 1.0)),
            patience_chars=int(raw.get("patience_chars", 1400)),
            jitter=float(raw.get("jitter", 0.25)),
            skip_when_known=float(raw.get("skip_when_known", 0.3)),
            lazy_skip=float(raw.get("lazy_skip", 0.03)),
        )

    def read(self, briefing_chars: int, rng: random.Random) -> Reading:
        """Draw one reading of a briefing of this length."""
        chars = max(1, int(briefing_chars))

        # Bail factor: 1.0 while the briefing is within patience, then decaying
        # as it overruns. Not a cliff -- someone handed twice their patience
        # gets through roughly half, which is the behaviour, rather than
        # stopping dead at a threshold.
        overrun = chars / max(1, self.patience_chars)
        bail = 1.0 if overrun <= 1.0 else 1.0 / overrun

        jitter_scroll = 1.0 + self.jitter * (rng.random() * 2.0 - 1.0)
        jitter_dwell = 1.0 + self.jitter * (rng.random() * 2.0 - 1.0)

        scroll = _clamp(self.attention * bail * jitter_scroll, 0.02, 1.0)
        # Dwell is the time to read the portion they actually scrolled past, at
        # their own speed, times how thoroughly they read it.
        dwell = expected_read_ms(chars, self.wpm) * scroll * self.thoroughness
        dwell_ms = max(200, int(round(dwell * jitter_dwell)))

        return Reading(
            dwell_ms=dwell_ms,
            scroll_fraction=round(scroll, 4),
            briefing_chars=chars,
            expected_quality=reference_read_quality(dwell_ms, scroll, chars),
            profile=self.name,
        )

    def _skip(self, briefing_chars: int, rng: random.Random) -> Reading:
        """The observable trace of a skip: a glance and a close.

        ONE distribution for both kinds of skip. The scroll is a fraction of
        the top of the page and the dwell is a fraction of the time reading
        that much would take, both below the `skipped` band's bars, and
        neither depends on why the reader skipped.
        """
        chars = max(1, int(briefing_chars))
        scroll = 0.02 + 0.10 * rng.random()  # < 0.15
        ratio = 0.03 + 0.14 * rng.random()  # < 0.20 of a full read
        dwell_ms = max(200, int(round(expected_read_ms(chars, self.wpm) * ratio)))
        return Reading(
            dwell_ms=dwell_ms,
            scroll_fraction=round(scroll, 4),
            briefing_chars=chars,
            expected_quality=reference_read_quality(dwell_ms, scroll, chars),
            profile=self.name,
        )

    def read_with_intent(
        self,
        briefing_chars: int,
        rng: random.Random,
        *,
        known_share: float,
        story_known: bool,
    ) -> tuple[Reading, ReadingIntent]:
        """Draw one reading of a briefing, given what the reader already holds.

        In order, and every roll drawn unconditionally so later draws do not
        move when an earlier branch is taken:

        1. informed skip with P = `skip_when_known * known_share`;
        2. otherwise lazy skip with P = `lazy_skip`;
        3. otherwise the length/patience reading as before, with attention and
           thoroughness shifted down when `known_share` is high (toward a
           skim) and up when it is low (toward a read).

        The returned `Reading` is what the system sees; the `ReadingIntent` is
        what happened, and goes nowhere near it.
        """
        share = _clamp(float(known_share), 0.0, 1.0)
        roll_informed = rng.random()
        roll_lazy = rng.random()

        if roll_informed < self.skip_when_known * share:
            reading = self._skip(briefing_chars, rng)
            return reading, ReadingIntent("skip", True, share, story_known)
        if roll_lazy < self.lazy_skip:
            reading = self._skip(briefing_chars, rng)
            return reading, ReadingIntent("skip", False, share, story_known)

        shift = 1.0 - KNOWN_SHARE_SHIFT * (share - 0.5) * 2.0  # 1.3 at 0, 0.7 at 1
        from dataclasses import replace

        shifted = replace(
            self,
            attention=_clamp(self.attention * shift, 0.02, 1.0),
            thoroughness=max(0.05, self.thoroughness * shift),
        )
        reading = shifted.read(briefing_chars, rng)
        act = _ACT_FOR_BAND[reading.expected_quality]
        # A bail on an over-long briefing bands as `skipped`; it was not an
        # informed choice, so it is recorded on the lazy side.
        informed = False if act == "skip" else None
        return reading, ReadingIntent(act, informed, share, story_known)


@dataclass(frozen=True)
class Reading:
    """One simulated reading, and the band the harness expects from it."""

    dwell_ms: int
    scroll_fraction: float
    briefing_chars: int
    expected_quality: str
    profile: str


PROFILES: dict[str, ReadingProfile] = {
    "careful": ReadingProfile(
        name="careful",
        wpm=210.0,
        attention=1.0,
        thoroughness=1.35,
        patience_chars=2600,
        jitter=0.18,
        skip_when_known=0.3,
        lazy_skip=0.03,
    ),
    "skimmer": ReadingProfile(
        name="skimmer",
        wpm=340.0,
        attention=0.75,
        thoroughness=0.7,
        patience_chars=1600,
        jitter=0.28,
        skip_when_known=0.5,
        lazy_skip=0.10,
    ),
    "bouncer": ReadingProfile(
        name="bouncer",
        wpm=300.0,
        attention=0.55,
        thoroughness=0.6,
        patience_chars=520,
        jitter=0.3,
        skip_when_known=0.6,
        lazy_skip=0.20,
    ),
}


def profile_for(name: str) -> ReadingProfile:
    return PROFILES.get(name, PROFILES["careful"])
