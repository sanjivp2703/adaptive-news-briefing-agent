#!/usr/bin/env python3
"""Harvest HN comments for empirical reply-style profiling. Stdlib only."""
import json, os, re, sys, time, urllib.request, urllib.error, html

BASE = "https://hacker-news.firebaseio.com/v0"
ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "_hn_cache")
os.makedirs(CACHE, exist_ok=True)
DELAY = 0.12


def fetch(path, key):
    cp = os.path.join(CACHE, key + ".json")
    if os.path.exists(cp):
        try:
            with open(cp) as f:
                return json.load(f)
        except Exception:
            pass
    url = f"{BASE}/{path}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "style-research/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            with open(cp, "w") as f:
                json.dump(data, f)
            time.sleep(DELAY)
            return data
        except Exception as e:
            if attempt == 2:
                sys.stderr.write(f"FAIL {url}: {e}\n")
                return None
            time.sleep(1.0 + attempt)
    return None


def item(i):
    return fetch(f"item/{i}.json", f"item_{i}")


SOLICITED = re.compile(r"^\s*(ask hn|show hn|tell hn|launch hn|poll:)", re.I)


def main():
    top = fetch("topstories.json", "list_topstories") or []
    best = fetch("beststories.json", "list_beststories") or []
    seen, order = set(), []
    for i in top[:250] + best[:250]:
        if i not in seen:
            seen.add(i)
            order.append(i)

    stories = []
    for sid in order:
        if len(stories) >= 70:
            break
        s = item(sid)
        if not s or s.get("type") != "story" or s.get("dead") or s.get("deleted"):
            continue
        title = s.get("title", "")
        if SOLICITED.match(title):
            continue
        if not s.get("url"):  # text posts are usually solicited-opinion shaped
            continue
        kids = s.get("kids") or []
        if len(kids) < 5:
            continue
        stories.append(s)

    print(f"stories selected: {len(stories)}", file=sys.stderr)

    comments = []
    # breadth-first per story: take top-level kids, then one level of replies
    for s in stories:
        got = 0
        frontier = list((s.get("kids") or [])[:14])
        depth2 = []
        for cid in frontier:
            c = item(cid)
            if not c:
                continue
            if c.get("type") == "comment" and not c.get("deleted") and not c.get("dead") and c.get("text"):
                comments.append({
                    "id": c["id"], "story_id": s["id"], "story_title": s.get("title"),
                    "by": c.get("by"), "time": c.get("time"), "parent": c.get("parent"),
                    "depth": 1, "text": c["text"],
                })
                got += 1
                depth2.extend((c.get("kids") or [])[:3])
        for cid in depth2[:14]:
            c = item(cid)
            if not c:
                continue
            if c.get("type") == "comment" and not c.get("deleted") and not c.get("dead") and c.get("text"):
                comments.append({
                    "id": c["id"], "story_id": s["id"], "story_title": s.get("title"),
                    "by": c.get("by"), "time": c.get("time"), "parent": c.get("parent"),
                    "depth": 2, "text": c["text"],
                })
                got += 1
        print(f"  {s['id']} {s.get('title','')[:60]}: {got}", file=sys.stderr)

    out = {
        "collected_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_stories": len(stories),
        "n_comments_raw": len(comments),
        "stories": [{"id": s["id"], "title": s.get("title"), "score": s.get("score"),
                     "url": s.get("url"), "descendants": s.get("descendants"),
                     "time": s.get("time")} for s in stories],
        "comments": comments,
    }
    with open(os.path.join(CACHE, "harvest_raw.json"), "w") as f:
        json.dump(out, f, indent=1)
    print(f"raw comments: {len(comments)}", file=sys.stderr)


if __name__ == "__main__":
    main()
