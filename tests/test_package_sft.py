"""`training/package_sft.py`: the chat-format packaging for stage-1 SFT.

Offline. The load-bearing tests push a packet through the real `Judge` and the
real `LocalModelClient` (with a fake wire) and assert the packaged system and
user messages are byte-identical to what went over the wire -- that is the
whole point of the packager.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from conversational_agent import config
from conversational_agent.judgment import Judge, load_prompt
from conversational_agent.judgments import BRIEFING_SCHEMA
from conversational_agent.local_client import LocalModelClient
from training import package_sft as pkg

ROOT = Path(__file__).resolve().parent.parent
PROMPT_TEXT = load_prompt("briefing").text


def _context(i: int) -> dict:
    return {
        "active_goal": None,
        "concept_ledger": {
            "confirmed": [],
            "explained": [],
            "familiar": ["harvest"] if i % 2 else [],
            "provisional": [],
            "unknown": ["véraison", "malolactic fermentation"],
        },
        "events": [
            {
                "detail": f"Picking began on {i + 1} August, three weeks early — 40% of the crop is in.",
                "headline": f"Harvest {i} starts early",
                "id": f"evt_{i:04d}",
                "occurred_at": "2026-08-10",
                "source_name": "Decanter",
                "source_url": "https://example.com",
            }
        ],
        "events_not_yet_seen": 0,
        "group": {"description": "Wine.", "name": "Wine"},
        "previously_raised": [],
        "proficiency": "beginner",
        "reading_pattern": {"informed_skips": 0, "lazy_skips": 0, "p_informed": 0.5, "resolved": 0, "unresolved_skips": 0},
        "subdomain_familiarity": {"viticulture & harvest": {"attested": 1, "band": "beginner", "known": 0}},
    }


def _output(i: int, *, with_reasoning: bool = True, with_subdomains: bool = True) -> dict:
    out = {
        "briefing": f"Picking began on {i + 1} August — véraison, the moment grapes change colour, came early.",
        "topic": "harvest",
        "explained_terms": ["véraison"],
        "terms_used": ["véraison", "harvest"],
    }
    if with_subdomains:
        out["subdomains"] = [
            {"term": "véraison", "subdomain": "viticulture & harvest"},
            {"term": "harvest", "subdomain": "viticulture & harvest"},
        ]
    if with_reasoning:
        out["reasoning"] = "the one unseen event, glossing only the unknown term"
    return out


def _generated_row(i: int, profile: str) -> dict:
    return {
        "id": f"sftgen_{i:03d}",
        "source_db": "generated",
        "group_id": "grp_wine",
        "prompt_version": "v6",
        "model": "claude-sonnet-5",
        "system": PROMPT_TEXT,
        "input": {"_judgment_point": "briefing", "context": _context(i)},
        "output": _output(i),
        "quality": {"checks": {"passed": True}},
        "meta": {"profile": profile, "persona_id": "pilar_wine", "group_family": "Wine", "log_id": f"jdg_{i:03d}"},
    }


def _real_row(i: int, *, version: str = "v5") -> dict:
    ctx = _context(i)
    if version == "v5":
        ctx.pop("subdomain_familiarity")
    row = {
        "id": f"jdg_real_{i}",
        "source_db": "probe4_pilar",
        "group_id": "grp_live",
        "prompt_version": version,
        "model": "claude-sonnet-5",
        "input": {"_judgment_point": "briefing", "context": ctx},
        "output": _output(i, with_subdomains=(version != "v5")),
        "quality": {},
    }
    return row


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture
def small_corpus(tmp_path):
    profiles = ["beginner"] * 20 + ["expert"] * 12 + ["partial"] * 3
    generated = [_generated_row(i, p) for i, p in enumerate(profiles)]
    real = [_real_row(0), _real_row(1), _real_row(2, version="v6")]
    return {
        "generated": _write(tmp_path / "gen.jsonl", generated),
        "real": _write(tmp_path / "real.jsonl", real),
        "generated_db": tmp_path / "missing.db",
        "live_db_dir": tmp_path / "no-live-dbs",
    }


def _package(corpus, **kw):
    return pkg.package(
        generated_path=corpus["generated"],
        real_path=corpus["real"],
        generated_db=corpus["generated_db"],
        live_db_dir=corpus["live_db_dir"],
        **kw,
    )


# --- Shape -------------------------------------------------------------------


def test_messages_shape(small_corpus):
    train, val, stats = _package(small_corpus)
    assert stats["dropped"] == 0
    for ex in train + val:
        roles = [m["role"] for m in ex["messages"]]
        assert roles == ["system", "user", "assistant"]
        assert all(isinstance(m["content"], str) and m["content"] for m in ex["messages"])
        assert {"id", "source", "profile", "backfilled"} <= set(ex["meta"])
        assert ex["meta"]["source"] in {"real", "generated"}


# --- Byte-identity with the serving path ---------------------------------------


class _CaptureWire:
    def __init__(self, reply: dict):
        self.reply = reply
        self.bodies: list[dict] = []

    def __call__(self, url, body, headers, timeout):
        self.bodies.append(body)
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps(self.reply)}}], "usage": {}})


def test_system_and_user_messages_match_what_judge_sends_through_local_client(monkeypatch):
    monkeypatch.setenv(config.LOCAL_MODEL_BASE_URL_ENV, "http://localhost:11434/v1")
    monkeypatch.setenv(config.LOCAL_MODEL_NAME_ENV, "news-briefer")
    wire = _CaptureWire(_output(7))
    judge = Judge(client=LocalModelClient(post=wire), store=None)
    context = _context(7)
    judge("briefing", context=context, schema=BRIEFING_SCHEMA)
    sent = wire.bodies[0]["messages"]

    row = _generated_row(7, "beginner")
    row["input"] = {"_judgment_point": "briefing", "context": context}
    assert pkg.complete_output(row, generated_db=Path("/nonexistent"), live_db_dir=Path("/nonexistent"))
    ex = pkg.build_example(row, PROMPT_TEXT)

    assert ex["messages"][0]["content"] == sent[0]["content"]  # system: prompt + schema tail
    assert ex["messages"][1]["content"] == sent[1]["content"]  # user: the packet, as serialised
    assert ex["messages"][0]["content"].endswith("\n```")
    assert "## Output format" in ex["messages"][0]["content"]
    assert ex["messages"][0]["content"].startswith(PROMPT_TEXT)


def test_user_message_reproduces_logged_wire_payload_for_a_real_generated_row():
    """The clean file's `input` is `json.loads` of the wire string; dumping it
    back must give the wire string. Uses the real generated log if present."""
    if not pkg.GENERATED_DB.exists() or not pkg.GENERATED_CLEAN.exists():
        pytest.skip("generated data not present")
    row = json.loads(pkg.GENERATED_CLEAN.open(encoding="utf-8").readline())
    conn = sqlite3.connect(pkg.GENERATED_DB)
    try:
        (input_json,) = conn.execute(
            "SELECT input_json FROM judgment_log WHERE id = ?", (row["meta"]["log_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert pkg.user_message(row["input"]) == input_json


# --- Assistant content -----------------------------------------------------------


def test_assistant_content_parses_back_and_has_every_required_key(small_corpus):
    train, val, _ = _package(small_corpus)
    for ex in train + val:
        parsed = json.loads(ex["messages"][2]["content"])
        assert set(parsed) == set(BRIEFING_SCHEMA["required"])  # exactly the schema fields, no extras
        assert list(parsed) == list(BRIEFING_SCHEMA["properties"])  # schema order
        assert isinstance(parsed["briefing"], str) and parsed["briefing"]
        assert isinstance(parsed["terms_used"], list)
        assert set(parsed["explained_terms"]) <= set(parsed["terms_used"])
        for pair in parsed["subdomains"]:
            assert set(pair) == {"term", "subdomain"}
        # ensure_ascii=False: accents survive as characters, not \u escapes
        assert "\\u00e9" not in ex["messages"][2]["content"]
        assert "véraison" in ex["messages"][2]["content"]


def test_assistant_content_round_trips_the_original_output(small_corpus):
    train, val, _ = _package(small_corpus)
    by_id = {ex["meta"]["id"]: ex for ex in train + val}
    ex = by_id["sftgen_003"]
    assert json.loads(ex["messages"][2]["content"]) == _output(3)


# --- Backfill / drop -------------------------------------------------------------


def test_v5_real_rows_get_empty_subdomains_and_are_flagged(small_corpus):
    train, _, stats = _package(small_corpus)
    real = [ex for ex in train if ex["meta"]["source"] == "real"]
    assert len(real) == 3
    v5 = [ex for ex in real if ex["meta"]["prompt_version"] == "v5"]
    assert len(v5) == 2
    for ex in v5:
        assert "subdomains" in ex["meta"]["backfilled"]
        assert json.loads(ex["messages"][2]["content"])["subdomains"] == []
    v6 = [ex for ex in real if ex["meta"]["prompt_version"] == "v6"][0]
    assert "subdomains" not in v6["meta"]["backfilled"]
    assert stats["backfilled"] == {"subdomains": 2}


def test_row_without_recoverable_reasoning_is_dropped_not_invented(tmp_path):
    bad = _generated_row(1, "beginner")
    bad["output"] = _output(1, with_reasoning=False)
    good = _generated_row(2, "beginner")
    corpus = {
        "generated": _write(tmp_path / "gen.jsonl", [bad, good]),
        "real": tmp_path / "absent.jsonl",
        "generated_db": tmp_path / "missing.db",
        "live_db_dir": tmp_path / "no-live-dbs",
    }
    train, val, stats = _package(corpus)
    assert stats["dropped"] == 1
    assert {ex["meta"]["id"] for ex in train + val} == {"sftgen_002"}


def test_reasoning_is_restored_from_the_judgment_log(tmp_path):
    db = tmp_path / "generated.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE judgment_log (id TEXT PRIMARY KEY, verdict_json TEXT, reasoning TEXT)")
    verdict = dict(_output(5), reasoning="restored from the log")
    conn.execute("INSERT INTO judgment_log VALUES (?, ?, ?)", ("jdg_005", json.dumps(verdict), "restored from the log"))
    conn.commit()
    conn.close()
    row = _generated_row(5, "expert")
    row["output"] = _output(5, with_reasoning=False)
    assert pkg.complete_output(row, generated_db=db, live_db_dir=tmp_path)
    assert row["output"]["reasoning"] == "restored from the log"
    assert row["_backfilled"] == ["reasoning"]


# --- Split -------------------------------------------------------------------------


def test_split_is_stratified_and_real_rows_train(small_corpus):
    train, val, stats = _package(small_corpus, val_fraction=0.1)
    assert stats["val_by_profile"] == {"beginner": 2, "expert": 1, "partial": 1}  # round(2.0), round(1.2), max(1, round(0.3))
    assert stats["train_by_profile"] == {"beginner": 18, "expert": 11, "partial": 2, "real": 3}
    assert all(ex["meta"]["source"] == "generated" for ex in val)
    ids_train = {ex["meta"]["id"] for ex in train}
    ids_val = {ex["meta"]["id"] for ex in val}
    assert not ids_train & ids_val
    assert len(ids_train) + len(ids_val) == 38


def test_split_is_deterministic_by_seed_and_independent_of_file_order(small_corpus, tmp_path):
    train_a, val_a, _ = _package(small_corpus, seed=11)
    train_b, val_b, _ = _package(small_corpus, seed=11)
    assert [ex["meta"]["id"] for ex in train_a] == [ex["meta"]["id"] for ex in train_b]
    assert [ex["meta"]["id"] for ex in val_a] == [ex["meta"]["id"] for ex in val_b]

    _, val_c, _ = _package(small_corpus, seed=12)
    assert {ex["meta"]["id"] for ex in val_c} != {ex["meta"]["id"] for ex in val_a}

    # Reverse the generated file: same seed must give the same split.
    rows = [json.loads(l) for l in small_corpus["generated"].open(encoding="utf-8")]
    reversed_corpus = dict(small_corpus, generated=_write(tmp_path / "gen_rev.jsonl", rows[::-1]))
    train_d, val_d, _ = _package(reversed_corpus, seed=11)
    assert [ex["meta"]["id"] for ex in val_d] == [ex["meta"]["id"] for ex in val_a]
    assert [ex["meta"]["id"] for ex in train_d] == [ex["meta"]["id"] for ex in train_a]


# --- Stats + CLI -----------------------------------------------------------------------


def test_token_estimate_is_chars_over_four_and_summary_reports_max(small_corpus):
    train, val, stats = _package(small_corpus)
    ex = train[0]
    chars = sum(len(m["content"]) for m in ex["messages"])
    assert pkg.token_estimate(ex) == -(-chars // 4)
    assert stats["tokens_est"]["max"] >= stats["tokens_est"]["p95"] >= stats["tokens_est"]["mean"] > 0
    assert stats["tokens_est"]["system_only"] > 2000  # the ~10k-char prompt plus schema


def test_cli_writes_both_files(small_corpus, tmp_path, capsys):
    out_train, out_val = tmp_path / "t.jsonl", tmp_path / "v.jsonl"
    rc = pkg.main(
        [
            "--generated", str(small_corpus["generated"]),
            "--real", str(small_corpus["real"]),
            "--generated-db", str(small_corpus["generated_db"]),
            "--live-db-dir", str(small_corpus["live_db_dir"]),
            "--train-out", str(out_train),
            "--val-out", str(out_val),
            "--seed", "3",
        ]
    )
    assert rc == 0
    assert len(out_train.read_text(encoding="utf-8").splitlines()) == 34
    assert len(out_val.read_text(encoding="utf-8").splitlines()) == 4
    printed = capsys.readouterr().out
    assert "train: 34" in printed and "val: 4" in printed
    assert "token estimate" in printed
