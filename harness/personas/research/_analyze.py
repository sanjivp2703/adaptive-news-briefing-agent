#!/usr/bin/env python3
"""Clean + measure HN comment corpus. Stdlib only.

Reads `_hn_cache/harvest_raw.json` (written by `_harvest.py`) and writes the
cleaned corpus, summary and example files back into `_hn_cache/`.

    .venv/bin/python harness/personas/research/_analyze.py
"""
import json, os, re, html, statistics, random, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "_hn_cache")


def main():
    raw = json.load(open(os.path.join(CACHE, "harvest_raw.json")))

    TAG = re.compile(r"<[^>]+>")


    def clean(t):
        t = t.replace("<p>", "\n\n").replace("</p>", "")
        t = re.sub(r"<a[^>]*href=\"([^\"]*)\"[^>]*>.*?</a>", r" \1 ", t, flags=re.S)
        t = TAG.sub(" ", t)
        t = html.unescape(t)
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()


    URLRE = re.compile(r"https?://\S+")


    def words(t):
        return re.findall(r"[A-Za-z0-9''\-]+", t)


    kept, dropped = [], {"long": 0, "linkdump": 0, "empty": 0, "quote_only": 0}
    seen_ids = set()
    for c in raw["comments"]:
        if c["id"] in seen_ids:
            continue
        seen_ids.add(c["id"])
        txt = clean(c["text"])
        if not txt:
            dropped["empty"] += 1
            continue
        nolink = URLRE.sub(" ", txt).strip()
        wl = words(nolink)
        if len(wl) < 2:
            dropped["linkdump"] += 1
            continue
        # link dump: majority of the content is urls
        if len(URLRE.findall(txt)) >= 1 and len(wl) < 8:
            dropped["linkdump"] += 1
            continue
        # quote-only (all lines start with > and nothing else)
        lines = [l for l in txt.split("\n") if l.strip()]
        if lines and all(l.strip().startswith(">") for l in lines):
            dropped["quote_only"] += 1
            continue
        n = len(words(txt))
        if n > 150:
            dropped["long"] += 1
            continue
        c2 = dict(c)
        c2["clean"] = txt
        c2["wc"] = n
        kept.append(c2)

    print("kept", len(kept), "dropped", dropped, file=sys.stderr)


    def pct(vals, p):
        v = sorted(vals)
        if not v:
            return 0
        k = (len(v) - 1) * p / 100.0
        f = int(k)
        c = min(f + 1, len(v) - 1)
        return v[f] + (v[c] - v[f]) * (k - f)


    wcs = [c["wc"] for c in kept]
    length = {
        "n": len(wcs), "min": min(wcs), "p10": pct(wcs, 10), "p25": pct(wcs, 25),
        "median": pct(wcs, 50), "p75": pct(wcs, 75), "p90": pct(wcs, 90),
        "max": max(wcs), "mean": round(statistics.mean(wcs), 1),
        "stdev": round(statistics.pstdev(wcs), 1),
    }

    # ---------------- pattern families ----------------
    P = {
     "uncertainty": [
       r"\bI think\b", r"\bI'?d think\b", r"\bIIRC\b", r"\bif I recall\b", r"\bpretty sure\b",
       r"\bfairly sure\b", r"\bAFAIK\b", r"\bas far as I know\b", r"\bmy understanding is\b",
       r"\bI believe\b", r"\bcorrect me if I'?m wrong\b", r"\blast I checked\b", r"\bI assume\b",
       r"\bI suspect\b", r"\bI'?m not sure\b", r"\bnot entirely sure\b", r"\bmight be wrong\b",
       r"\bseems? like\b", r"\bI guess\b", r"\bpresumably\b", r"\bI vaguely (recall|remember)\b",
     ],
     "nonknowledge": [
       r"\bI (don'?t|do not) know\b", r"\bno idea\b", r"\bhadn'?t (seen|heard|noticed)\b",
       r"\bdidn'?t (realise|realize|know)\b", r"\bwasn'?t aware\b", r"\bTIL\b",
       r"\bnever heard of\b", r"\bI'?m not familiar\b", r"\bnot familiar with\b",
       r"\bcan'?t tell\b", r"\bhaven'?t (seen|heard|read|used|tried)\b", r"\bwho knows\b",
       r"\bI have no\b.{0,12}\b(clue|idea)\b",
     ],
     "correction": [
       r"\bthis is (wrong|incorrect|false|misleading)\b", r"\bthat'?s (wrong|not right|incorrect|false|misleading)\b",
       r"\bnot (quite )?(true|correct|right)\b", r"\bactually,?\b", r"\bin fact,?\b",
       r"\bno,? (it|that|this|they|you)\b", r"\bnope\b", r"\bthe article (is|says|claims|gets)\b",
       r"\byou'?re (wrong|confusing|conflating|mistaken)\b", r"\bmisleading\b",
       r"\bthat'?s not (how|what|why)\b", r"\bcitation needed\b", r"\bthis is (a )?misread",
       r"\bconflating\b", r"\bto be clear,?\b", r"\bdisagree\b",
     ],
     "question_back": [],  # handled structurally
     "bare_reaction": [],  # handled structurally
    }

    REG = {
     "lc_start": None, "no_terminal": None, "contraction": r"\b\w+'(s|t|re|ve|ll|d|m)\b",
     "profanity": r"\b(fuck\w*|shit\w*|damn|crap|bullshit|hell|ass|sucks?|dumb|stupid|garbage|insane|wtf|lol|lmao|meh|yeah|yep|nah|gonna|wanna|kinda|sorta|dude|whatever)\b",
     "first_person_open": r"^\s*(I|I'?(m|ve|d|ll)|My|Me\b|We\b|IMO|IME|Personally)\b",
    }


    def count(pats, t):
        return [p for p in pats if re.search(p, t, re.I)]


    def first_sentence(t):
        s = t.strip().lstrip(">").strip()
        return s


    def is_question_back(c):
        t = c["clean"]
        if "?" not in t:
            return False
        # question dominant: >=1 question and question marks make up a big share of terminals
        qs = t.count("?")
        terminals = len(re.findall(r"[.!?]", t))
        if terminals == 0:
            return False
        # short and mostly question, or ends with a question
        return (qs / terminals >= 0.5) or (c["wc"] <= 40 and qs >= 1 and t.rstrip().endswith("?"))


    NUMFACT = re.compile(r"\b(\d{4}|\d+(\.\d+)?\s?(%|percent|x|k|m|bn|billion|million|thousand|gb|mb|tb|kb|ms|hz|ghz|mhz|nm|kw|mw|gw|w|v|km|mi|years?|months?|days?|hours?)\b)", re.I)
    PROPER = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z]{2,}(?:[A-Z]\w*)?)\b")


    def has_specificity(c):
        t = c["clean"]
        if NUMFACT.search(t):
            return True
        # proper nouns not at sentence start
        caps = PROPER.findall(t)
        return len(set(caps)) >= 2


    def is_bare_reaction(c):
        t = c["clean"]
        if c["wc"] > 30:
            return False
        if "?" in t:
            return False
        if has_specificity(c):
            return False
        return True


    res = {}
    for fam in ("uncertainty", "nonknowledge", "correction"):
        hits = [c for c in kept if count(P[fam], c["clean"])]
        res[fam] = hits

    res["question_back"] = [c for c in kept if is_question_back(c)]
    res["bare_reaction"] = [c for c in kept if is_bare_reaction(c)]

    reg = {}
    lc = []
    for c in kept:
        s = first_sentence(c["clean"])
        if s and s[0].isalpha() and s[0].islower():
            lc.append(c)
    reg["lc_start"] = lc
    reg["no_terminal"] = [c for c in kept if not c["clean"].rstrip().endswith((".", "!", "?", ":", ";", ")", '"'))]
    reg["contraction"] = [c for c in kept if re.search(REG["contraction"], c["clean"], re.I)]
    reg["profanity_slang"] = [c for c in kept if re.search(REG["profanity"], c["clean"], re.I)]
    reg["first_person_open"] = [c for c in kept if re.search(REG["first_person_open"], c["clean"])]

    summary = {
        "collected_utc": raw["collected_utc"],
        "n_stories": raw["n_stories"],
        "n_comments_raw": raw["n_comments_raw"],
        "n_kept": len(kept),
        "dropped": dropped,
        "length": length,
        "rates_per_100": {k: round(100.0 * len(v) / len(kept), 1) for k, v in res.items()},
        "register_per_100": {k: round(100.0 * len(v) / len(kept), 1) for k, v in reg.items()},
    }

    # length by band
    bands = [(1, 5), (6, 10), (11, 20), (21, 40), (41, 80), (81, 150)]
    summary["length_bands"] = {f"{a}-{b}": sum(1 for w in wcs if a <= w <= b) for a, b in bands}
    summary["length_bands_pct"] = {f"{a}-{b}": round(100.0 * sum(1 for w in wcs if a <= w <= b) / len(wcs), 1) for a, b in bands}

    # length of "specific knowledge" (regex proxy) comments
    spec = [c for c in kept if has_specificity(c)]
    sw = [c["wc"] for c in spec]
    summary["specificity_proxy"] = {
        "n": len(spec), "pct": round(100.0 * len(spec) / len(kept), 1),
        "min": min(sw), "p10": pct(sw, 10), "p25": pct(sw, 25), "median": pct(sw, 50),
        "p75": pct(sw, 75), "p90": pct(sw, 90), "max": max(sw), "mean": round(statistics.mean(sw), 1),
        "under_20w": sum(1 for w in sw if w < 20), "under_20w_pct": round(100.0*sum(1 for w in sw if w < 20)/len(sw), 1),
        "under_40w": sum(1 for w in sw if w < 40), "under_40w_pct": round(100.0*sum(1 for w in sw if w < 40)/len(sw), 1),
    }

    json.dump(summary, open(os.path.join(CACHE, "summary.json"), "w"), indent=1)
    json.dump(kept, open(os.path.join(CACHE, "comments_clean.json"), "w"), indent=1)

    # dump examples per family
    ex = {}
    for k, v in list(res.items()) + list(reg.items()):
        rnd = random.Random(7)
        pool = sorted(v, key=lambda c: c["wc"])
        picks = rnd.sample(v, min(14, len(v)))
        ex[k] = [{"id": c["id"], "wc": c["wc"], "text": c["clean"][:600], "story": c["story_title"]} for c in picks]
    json.dump(ex, open(os.path.join(CACHE, "examples.json"), "w"), indent=1)

    # hand-classification sample of 60
    rnd = random.Random(20260906)
    sample = rnd.sample(kept, 60)
    json.dump([{"id": c["id"], "wc": c["wc"], "story": c["story_title"], "text": c["clean"]} for c in sample],
              open(os.path.join(CACHE, "handsample_60.json"), "w"), indent=1)

    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
