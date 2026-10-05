"""No score, grade, level or proficiency band ever reaches the user.

This is the redesign's central rule and the reason the previous build failed
its first real user: the CLI printed `Grade: partial (55/100)` and
`Level: 44.0 -> 55.0`, which is the single most study-like thing a product can
do to someone who is a beginner in every domain.

It is guarded two ways, because either alone is easy to defeat: the session
output path must not even *read* the scoring fields the Assessor returns, and
the user-facing commands must not *print* score-shaped text.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from conversational_agent import cli

# Fields the Assessor's ThreadOutcome carries for the ledger's benefit that the
# session must never surface, plus the vocabulary of the deleted level model.
FORBIDDEN_READS = {
    "proficiency",
    "proficiency_before",
    "proficiency_after",
    "newly_known",
    "score",
    "grade",
    "grade_score",
    "level",
    "level_before",
    "level_after",
    "difficulty",
}

# The functions that produce session output -- the only path a user sees when
# something is raised with them.
SESSION_OUTPUT_FUNCTIONS = {"cmd_session", "_raise_topic"}

SCORE_SHAPED = re.compile(
    r"/100|\bgrade\b|\bscore[ds]?\b|\blevel\b|\bbeginner\b|\bdeveloping\b"
    r"|\bconversant\b|\bfluent\b|\bproficien",
    re.I,
)


def _body_without_docstring(func: ast.AST) -> list[ast.AST]:
    body = list(func.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


# Prevents: the session path reading `outcome.proficiency_after` or a score
# field at all. Not printing it today is one edit away from printing it; not
# reading it is the property that holds.
def test_the_session_output_path_never_reads_a_score_or_band_field():
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = SESSION_OUTPUT_FUNCTIONS - set(functions)
    assert not missing, f"cli.py no longer defines {sorted(missing)}; update this guard."

    offenders: list[str] = []
    for name in sorted(SESSION_OUTPUT_FUNCTIONS):
        for statement in _body_without_docstring(functions[name]):
            for node in ast.walk(statement):
                if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_READS:
                    offenders.append(f"{name}(): reads .{node.attr}")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if SCORE_SHAPED.search(node.value):
                        offenders.append(f"{name}(): literal {node.value!r}")
    assert not offenders, "Session output touches scoring:\n" + "\n".join(offenders)


# Prevents: a score-shaped string reaching the user from any command they
# actually run day to day -- the regression that would reintroduce
# "Grade: partial (55/100)" under a different name.
def test_no_user_facing_command_prints_score_shaped_output(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    runs = [
        ["init", "--db", db, "--name", "Sam"],
        ["user", "list", "--db", db],
        ["group", "add", "--db", db, "wine tasting", "--description", "natural wine"],
        ["group", "list", "--db", db],
        ["goal", "add", "--db", db, "wine tasting", "tasting Thursday",
         "--deadline", "2999-01-01"],
        ["goal", "list", "--db", db],
    ]
    output = ""
    for argv in runs:
        assert cli.main(argv) == 0, f"`{' '.join(argv)}` failed"
        output += capsys.readouterr().out

    goal_id = re.search(r"\(id (goal_\w+)\)", output).group(1)
    assert cli.main(["goal", "close", "--db", db, goal_id]) == 0
    output += capsys.readouterr().out

    hits = SCORE_SHAPED.findall(output)
    assert not hits, f"Score-shaped output from a user-facing command: {hits}\n{output}"
