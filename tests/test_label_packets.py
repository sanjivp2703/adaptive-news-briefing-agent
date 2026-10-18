"""Packet labels describe the source story, are cached by row id, and survive a bad reply."""

from __future__ import annotations

import json

from training import label_packets as lp

PACKET = {
    "group": {"name": "Premier League"},
    "concept_ledger": {"unknown": ["offside", "var"], "familiar": [], "explained": [], "confirmed": [], "provisional": []},
    "events": [{"headline": "City win the derby 2-1", "detail": "Haaland scored twice. The winner came in the 88th minute after a VAR check."}],
}


def _row(rid):
    return {"messages": [{"role": "system", "content": "s"},
                         {"role": "user", "content": json.dumps({"_judgment_point": "briefing", "context": PACKET})},
                         {"role": "assistant", "content": "x"}],
            "meta": {"id": rid, "profile": "beginner"}}


def test_clean_label_normalises_terms_and_drops_facts_without_anchors():
    raw = {"terms": [{"term": "VAR", "central": True, "subdomain": "Officiating"}, {"term": "var", "central": False, "subdomain": "x"}, {"term": ""}],
           "facts": [{"fact": "City won 2-1", "anchors": ["City", "2-1"], "terms": ["VAR"]}, {"fact": "no anchors", "anchors": []}]}
    label = lp.clean_label(raw)
    assert label["terms"] == [{"term": "var", "central": True, "subdomain": "officiating"}]
    assert label["facts"] == [{"fact": "City won 2-1", "anchors": ["City", "2-1"], "terms": ["var"]}]
    assert lp.clean_label({"terms": [], "facts": []}) is None
    assert lp.clean_label("nonsense") is None


def test_offline_label_uses_the_ledger_and_the_source_only():
    label = lp.offline_label(PACKET)
    assert [t["term"] for t in label["terms"]] == ["var"]  # "offside" is unknown but not in the story
    assert label["facts"] and all(f["anchors"] for f in label["facts"])
    assert "var" in label["facts"][-1]["terms"]


def test_label_rows_is_resumable_and_keeps_going_past_a_bad_reply(tmp_path):
    out = tmp_path / "labels.json"
    calls = []

    def labeler(packet):
        calls.append(1)
        return None if len(calls) == 1 else lp.offline_label(packet)

    rows = [_row("a"), _row("b"), _row("c")]
    labels = lp.label_rows(rows, labeler, {"a": {"terms": [], "facts": [{"fact": "f", "anchors": ["x"], "terms": []}]}},
                           out=out, budget=10.0, spend={"usd": 0.0, "calls": 0}, progress=lambda _: None)
    assert len(calls) == 2 and set(labels) == {"a", "c"}            # "b" got the bad reply, "a" was cached
    assert json.loads(out.read_text())["c"]["terms"][0]["term"] == "var"
    again = lp.label_rows(rows, labeler, lp.load_labels(out), out=out, budget=10.0, spend={"usd": 0.0, "calls": 0}, progress=lambda _: None)
    assert len(calls) == 3 and set(again) == {"a", "b", "c"}
