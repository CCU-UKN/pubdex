"""RELEASING.md against the tree and stage 10, offline.

The maintainer's procedure ends with a table, *What ran these commands*: one
row for every block of shell commands of the page, in order, with a piece of
the block, what ran it and notes. What ran it is one of: steps of stage 10
(run_release_rehearsal.sh, the rehearsal of a PubDex release); stage 1
(run_checks.sh); the suite itself; not run by the suite, for the suite's own
prerequisites; or release only, for what only a real PubDex release runs.
These tests keep that table checkable without Docker:

- each row matches its block;
- a row of stage 10 names steps that exist -- the steps
  DB/TESTING_STRATEGY.md numbers in *The rehearsal of a PubDex release* and
  the script marks with section comments of the same number, "# --- <n>.
  ..." -- and each of them runs the block's release.sh command; an option
  the block gives and the step does not, or the step gives and the block
  does not, is named in the row's notes;
- a row of stage 1 names commands that run_checks.sh runs, or that a test
  module the notes name runs;
- a release-only row that publishes names the tests that cover publication;
- every script and file a block names exists, and every option it gives to
  one of the repository's scripts is one that script knows.

Whether the commands work is what stage 10, stage 1 and a real PubDex
release show by running them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_deployment_guide import ANY, PLACEHOLDER, _blocks, _joined, _lines, _script_steps
from tests.test_kubernetes_guide import _steps_named

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "RELEASING.md"
SCRIPT = ROOT / "run_release_rehearsal.sh"
STRATEGY = ROOT / "DB" / "TESTING_STRATEGY.md"
TABLE = "\n## What ran these commands\n"
STRATEGY_SECTION = "\n## The rehearsal of a PubDex release\n"
PUBLISH_TESTS = "DB/tests/test_release_script.py"
RELEASE_COMMANDS = ("setup", "build", "verify", "publish")
OPTION = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")


# ---------------------------------------------------------------------------
# The guide
# ---------------------------------------------------------------------------

def _parts(text: str) -> tuple:
    """(the page before the table, the table's section)."""
    assert TABLE in text, "RELEASING.md has no section What ran these commands"
    before, table = text.split(TABLE, 1)
    return before, table.split("\n## ", 1)[0]


def _rows(table: str) -> list:
    """The table's rows as {piece, ran_by, notes}."""
    rows = []
    for line in table.splitlines():
        if not line.startswith("| ") or re.match(r"^\|[-| ]+\|$", line) or line.startswith("| Block |"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip()[1:-1])]
        assert len(cells) == 3, f"a row without three cells: {line}"
        piece = re.fullmatch(r"`([^`]+)`", cells[0])
        assert piece, f"a row without a piece of its block: {line}"
        rows.append({"piece": piece.group(1), "ran_by": cells[1], "notes": cells[2]})
    return rows


def _kind(ran_by: str) -> str:
    if re.match(r"stage 10, steps? [0-9]", ran_by):
        return "stage 10"
    if ran_by == "stage 1":
        return "stage 1"
    if ran_by in ("the suite", "not run by the suite", "release only"):
        return ran_by
    return ""


def _program(line: str) -> tuple:
    """(the program a command line runs, its arguments): leading variable
    assignments skipped, and an interpreter's script taken as the program."""
    line = re.sub(r'^(?:[A-Z_]+=(?:"[^"]*"|\S+)\s+)+', "", line)
    words = line.split()
    if words and words[0] in ("python3", ".venv/bin/python") and len(words) > 1:
        return words[1], words[2:]
    return (words[0], words[1:]) if words else ("", [])


def _repository_file(program: str) -> Path | None:
    """The repository's file a program names, or None for a system command."""
    if program.startswith("./"):
        return ROOT / program[2:]
    if program.startswith("DB/"):
        return ROOT / program
    return None


def _named(notes: str, option: str) -> bool:
    return f"`{option}`" in notes


# ---------------------------------------------------------------------------
# Stage 10 and stage 1
# ---------------------------------------------------------------------------

def _steps() -> dict:
    """{step: code} of stage 10, with its wrapper `release ...` read as
    ./release.sh, as the maintainer types it."""
    steps = _script_steps(SCRIPT.read_text(encoding="utf-8"))
    return {number: re.sub(r"(?<![\w$./-])release (setup|build|verify|publish)\b", r"./release.sh \1", code)
            for number, code in steps.items()}


def _runs(code: str, command: str) -> list:
    """The lines of a step's code that run ./release.sh <command>, each up to
    its first || or &&."""
    found = []
    for line in code.splitlines():
        match = re.search(rf"(?<![\w$/.-])\./release\.sh {command}\b(.*)$", line)
        if match:
            found.append(re.split(r"\|\||&&", match.group(1))[0])
    return found


def _stage_1_runs(path: str, notes: str) -> bool:
    """Whether stage 1 runs the program: run_checks.sh itself, a Python
    script run_checks.sh runs, or one that a test module the notes name runs."""
    if path == "./run_checks.sh":
        return True
    checks = (ROOT / "run_checks.sh").read_text(encoding="utf-8")
    if re.search(rf'(?m)^[^#\n]*"\$PYTHON"\s+{re.escape(path)}(?:\s|$)', checks):
        return True
    for module in re.findall(r"`(DB/tests/test_[a-z0-9_]+\.py)`", notes):
        test = ROOT / module
        if test.is_file() and Path(path).name in test.read_text(encoding="utf-8"):
            return True
    return False


# ---------------------------------------------------------------------------
# The table against the blocks, the tree and stage 10
# ---------------------------------------------------------------------------

def _command_problems(where: str, line: str) -> list:
    """A command line's own problems: a missing script or file, an unknown
    release.sh command, an option its script does not know."""
    problems = []
    program, arguments = _program(line)
    path = _repository_file(program)
    if path is None:
        return problems
    if not path.is_file():
        return [f"{where}: no such file in the repository: {program}"]
    text = path.read_text(encoding="utf-8")
    if path.name == "release.sh":
        command = arguments[0] if arguments else ""
        if command not in RELEASE_COMMANDS or f"release.sh {command}" not in text:
            problems.append(f"{where}: release.sh has no command {command!r}")
    for word in arguments:
        option = OPTION.fullmatch(word)
        if option and option.group(1) not in text:
            problems.append(f"{where}: {program} has no option {option.group(1)}")
        named = re.fullmatch(r"(DB/\S+)", word)
        if named and ANY not in word and "*" not in word and not (ROOT / named.group(1)).exists():
            problems.append(f"{where}: no such file in the repository: {word}")
    return problems


def _problems(text: str, steps: dict) -> list:
    """Every disagreement between the table, the page's blocks, the tree and stage 10."""
    before, table = _parts(text)
    blocks, rows = _blocks(before), _rows(table)
    problems = []
    if len(rows) != len(blocks):
        problems.append(f"{len(blocks)} blocks of commands, {len(rows)} rows in the table")
    for mention in re.findall(r"stage 10, steps? [0-9][0-9, and to]*", table):
        missing = [step for step in _steps_named(mention) if step not in steps]
        if missing:
            problems.append(f"the table names steps {missing} of stage 10, which it does not have")
    for number, (block, row) in enumerate(zip(blocks, rows), 1):
        where = f"row {number} (`{row['piece']}`)"
        if row["piece"] not in _joined(block):
            problems.append(f"{where}: not in its block, which begins {block.splitlines()[0]!r}")
        lines = _lines(block)
        for line in lines:
            problems.extend(_command_problems(where, line))
        kind = _kind(row["ran_by"])
        if not kind:
            problems.append(f"{where}: says none of the known kinds: {row['ran_by']!r}")
        elif not row["notes"]:
            problems.append(f"{where}: has no notes")
        if kind == "release only" and any("release.sh publish" in line for line in lines) \
                and PUBLISH_TESTS not in row["notes"]:
            problems.append(f"{where}: publishes, and does not name {PUBLISH_TESTS}, which covers it")
        if kind == "the suite":
            for line in lines:
                if _program(line)[0] != "./run_ci_suite.sh":
                    problems.append(f"{where}: not the suite's command: {line.replace(ANY, '<…>')}")
        if kind == "stage 1":
            for line in lines:
                program = _program(line)[0]
                if not _stage_1_runs(program, row["notes"]):
                    problems.append(f"{where}: stage 1 does not run {program}")
        if kind != "stage 10":
            continue
        named = _steps_named(row["ran_by"])
        missing = [step for step in named if step not in steps]
        if not named or missing:
            problems.append(f"{where}: stage 10 has no step {missing or row['ran_by']}")
            continue
        for line in lines:
            program, arguments = _program(line)
            if program != "./release.sh" or not arguments:
                problems.append(f"{where}: not a release.sh command: {line.replace(ANY, '<…>')}")
                continue
            command = arguments[0]
            given = {word for word in arguments if OPTION.fullmatch(word)}
            for step in named:
                runs = _runs(steps[step], command)
                if not runs:
                    problems.append(f"{where}: step {step} does not run ./release.sh {command}")
                    continue
                used = set(OPTION.findall(" ".join(runs)))
                for option in sorted(given - used):
                    if not _named(row["notes"], option):
                        problems.append(f"{where}: step {step} runs ./release.sh {command} without {option}, "
                                        "which the notes do not name")
                for option in sorted(used - given):
                    if not _named(row["notes"], option):
                        problems.append(f"{where}: step {step} runs ./release.sh {command} with {option}, "
                                        "which the notes do not name")
    return problems


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _replace(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def test_every_block_has_its_row_and_each_row_says_truly_what_ran_it() -> None:
    problems = _problems(_guide(), _steps())
    assert not problems, "\n".join(problems)
    before, table = _parts(_guide())
    rows = _rows(table)
    kinds = {_kind(row["ran_by"]) for row in rows}
    assert {"stage 10", "stage 1", "the suite", "not run by the suite", "release only"} <= kinds, kinds
    assert len(rows) == len(_blocks(before)) >= 12


def test_the_steps_are_the_ones_the_testing_strategy_numbers() -> None:
    strategy = STRATEGY.read_text(encoding="utf-8")
    assert STRATEGY_SECTION in strategy
    section = strategy.split(STRATEGY_SECTION, 1)[1].split("\n## ", 1)[0]
    numbered = [int(n) for n in re.findall(r"(?m)^([0-9]+)\. \*\*", section)]
    assert numbered == list(range(1, len(numbered) + 1)) and len(numbered) >= 6, numbered
    assert sorted(_steps()) == numbered


def test_the_step_parser_reads_the_script() -> None:
    steps = _steps()
    assert [_runs(steps[2], "build")] == [[' --test "$name" --out "$RUN_DIR/first" ']]
    assert _runs(steps[3], "verify") == [' "$RUN_DIR/first" '] and not _runs(steps[3], "build")
    assert _runs(steps[4], "build") == [' --test "$name" --out "$RUN_DIR/second" ']
    assert not any(_runs(code, "publish") for code in steps.values())
    # A message names a command without running it.
    assert not _runs(_script_steps('# --- 1. x\nstep "./release.sh build --test a"\n')[1], "build")


@pytest.mark.parametrize(
    ("change", "found"),
    [
        (("| stage 10, step 2 |", "| stage 10, step 3 |"), "step 3 does not run ./release.sh build"),
        (("| stage 10, step 3 |", "| stage 10, step 9 |"), "stage 10 has no step [9]"),
        (("| stage 10, step 3 |", "| stage 10, steps 3 and 4 |"), "step 4 does not run ./release.sh verify"),
        (("without `--source`", "without the source label"), "without --source, which the notes do not name"),
        (("with `--test` and `--out`", "with `--test`"), "with --out, which the notes do not name"),
        (("./release.sh build --source <public repository URL>", "./release.sh build --sign --source <public repository URL>"),
         "release.sh has no option --sign"),
        (("./release.sh setup               #", "./release.sh install             #"), "release.sh has no command 'install'"),
        (("| `./setup_venv.sh` | not run by the suite |", "| `./setup_venv.sh` | stage 1 |"), "stage 1 does not run ./setup_venv.sh"),
        (("| `./run_ci_suite.sh --base` | the suite |", "| `./run_ci_suite.sh --base` | tested |"), "none of the known kinds"),
        (("`./release.sh publish` | release only | [`DB/tests/test_release_script.py`](DB/tests/test_release_script.py), in stage 1, runs",
          "`./release.sh publish` | release only | stage 1 runs"), "publishes, and does not name"),
        (("| `git clone --branch` |", "| `git clone --depth` |"), "not in its block"),
        (("python3 DB/release_record.py entry <version>", "python3 DB/release_records.py entry <version>"),
         "no such file in the repository: DB/release_records.py"),
        (("stage 10, step 6, shows", "stage 10, step 8, shows"), "the table names steps [8] of stage 10"),
    ],
)
def test_a_wrong_row_or_block_would_be_found(change: tuple, found: str) -> None:
    problems = _problems(_replace(_guide(), *change), _steps())
    assert any(found in problem for problem in problems), problems


def test_a_block_without_its_row_would_be_found() -> None:
    text = _replace(_guide(), TABLE, "\n```bash\n./release.sh verify dist/pubdex-<version>\n```\n" + TABLE)
    problems = _problems(text, _steps())
    assert any("blocks of commands" in problem for problem in problems), problems


def test_placeholders_stand_for_one_word() -> None:
    line = _lines("./release.sh verify dist/pubdex-<version>\n")[0]
    assert ANY in line and not PLACEHOLDER.search(line)
    assert _program('DOCKER_CONFIG="$(mktemp -d)" ./run_stack_acceptance.sh --release')[0] == "./run_stack_acceptance.sh"
    assert _program(".venv/bin/python DB/check_release_notes.py") == ("DB/check_release_notes.py", [])
