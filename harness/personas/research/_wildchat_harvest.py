#!/usr/bin/env python3
"""Harvest WildChat-1M rows via the HF datasets-server rows API. Stdlib only.

Pages are cached to `_wildchat_cache/rows_<offset>.json` so re-runs cost nothing.
Sequential, polite (small delay). Filtering is done in `_wildchat_filter.py`.

    .venv/bin/python harness/personas/research/_wildchat_harvest.py [start_offset] [n_pages]
"""
import json, os, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "_wildchat_cache")
os.makedirs(CACHE, exist_ok=True)
URL = ("https://datasets-server.huggingface.co/rows?dataset=allenai%2FWildChat-1M"
       "&config=default&split=train&offset={offset}&length=100")
DELAY = 0.6


def fetch_page(offset):
    cp = os.path.join(CACHE, f"rows_{offset:08d}.json")
    if os.path.exists(cp):
        try:
            with open(cp) as f:
                return json.load(f), True
        except Exception:
            pass
    url = URL.format(offset=offset)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "persona-style-research/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
            # keep only what we need to keep the cache small
            slim = {"rows": []}
            for row in data.get("rows", []):
                r_ = row.get("row", {})
                conv = r_.get("conversation") or []
                slim["rows"].append({
                    "conversation_hash": r_.get("conversation_hash"),
                    "model": r_.get("model"),
                    "timestamp": str(r_.get("timestamp")),
                    "language": r_.get("language"),
                    "turn": r_.get("turn"),
                    "conversation": [
                        {"role": m.get("role"), "content": m.get("content"),
                         "language": m.get("language"), "turn_identifier": m.get("turn_identifier")}
                        for m in conv
                    ],
                })
            with open(cp, "w") as f:
                json.dump(slim, f)
            time.sleep(DELAY)
            return slim, False
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5.0 * (attempt + 1))
                continue
            sys.stderr.write(f"HTTP {e.code} at offset {offset}\n")
            time.sleep(2.0 + attempt)
        except Exception as e:
            sys.stderr.write(f"ERR at offset {offset}: {e}\n")
            time.sleep(2.0 + attempt)
    return None, False


def main():
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    n_pages = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    stride = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    fetched = cached = failed = 0
    for i in range(n_pages):
        offset = start + i * stride
        data, was_cached = fetch_page(offset)
        if data is None:
            failed += 1
        elif was_cached:
            cached += 1
        else:
            fetched += 1
        if (i + 1) % 10 == 0:
            print(f"page {i+1}/{n_pages} offset={offset} fetched={fetched} cached={cached} failed={failed}", flush=True)
    print(f"done fetched={fetched} cached={cached} failed={failed}")


if __name__ == "__main__":
    main()
