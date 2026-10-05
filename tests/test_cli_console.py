"""The CLI's builder-facing views render every ledger state.

`ledger` and `group show` had no test, which is how two crashes shipped: the
CLI kept its own list of concept states that had lost `familiar`, and
`group show` read a field the exchange record no longer has.
"""

from __future__ import annotations

from conversational_agent import cli, config
from conversational_agent.store import Store


def _seed(db: str) -> None:
    store = Store(db)
    user = store.create_user("Sam")
    scope = store.scope(user.id)
    group = scope.create_group("wine", description="natural wine")
    scope.note_exposure(group.id, ["vintage", "tannin", "lees"])
    scope.mark_read_explanation(group.id, ["vintage"], evidence="defined in a briefing they read")
    scope.mark_explained(group.id, ["tannin"], evidence="asked about it; we explained")
    exchange_id = scope.open_exchange(group.id, briefing="Harvest began early.", topic="harvest")
    scope.add_turn(exchange_id, "user", "What is a vintage?")
    scope.add_turn(exchange_id, "system", "The year the grapes were picked.")
    scope.close_exchange(exchange_id, [], [], ["vintage"], None)
    store.close()


# Prevents: `ledger` raising ValueError on a state its own ordering forgot.
def test_ledger_lists_every_state_including_familiar(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    _seed(db)
    assert cli.main(["ledger", "--db", db, "wine"]) == 0
    out = capsys.readouterr().out
    for state in (config.CONCEPT_FAMILIAR, config.CONCEPT_EXPLAINED, config.CONCEPT_UNKNOWN):
        assert state in out
    assert f"{config.CONCEPT_FAMILIAR}=1" in out
    assert cli.main(["ledger", "--db", db, "wine", "--state", config.CONCEPT_FAMILIAR]) == 0
    assert "vintage" in capsys.readouterr().out


# Prevents: `group show` crashing on a group that has a thread with a reply,
# and its state counts silently leaving a state out.
def test_group_show_renders_a_group_with_a_thread(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    _seed(db)
    assert cli.main(["group", "show", "--db", db, "wine"]) == 0
    out = capsys.readouterr().out
    assert "What is a vintage?" in out
    assert f"{config.CONCEPT_FAMILIAR} (1)" in out
    assert "total=3" in out


def test_the_cli_and_the_config_agree_on_the_states():
    assert set(cli._STATE_ORDER) == set(config.CONCEPT_STATES)
    assert set(cli._STATE_GLOSS) == set(config.CONCEPT_STATES)
