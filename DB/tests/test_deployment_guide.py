"""DEPLOYMENT.md, "Upgrading to a new release", against stage 11, offline.

The section ends with a table, *What ran these commands*: one row for every
block of shell commands in the section, in order, with a piece of the block,
the steps of stage 11 (run_upgrade_acceptance.sh) that ran it, or that
nothing did, and what the run did differently. DB/TESTING_STRATEGY.md
numbers stage 11's steps, and the script marks each step with a section
comment of the same number, "# --- <n>. ...". These tests keep that table
checkable without Docker: each row matches its block; each step a row names
exists; each named step runs one of the block's commands; and each docker
command of the block is one those steps run -- its words, in their order, on
one command line of the steps' own code or of the script's functions they
call, which may add options of the run's own between them. A placeholder
such as <version> stands for any one word. Whether the commands work is
what stage 11 shows by running them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_kubernetes_guide import _functions, _steps_named

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "DEPLOYMENT.md"
SCRIPT = ROOT / "run_upgrade_acceptance.sh"
STRATEGY = ROOT / "DB" / "TESTING_STRATEGY.md"
SECTION = "\n## Upgrading to a new release\n"
TABLE = "\n### What ran these commands\n"
STRATEGY_SECTION = "\n## Upgrades between PubDex releases on Compose\n"

# What the table's second column may say, by its first words.
RAN_BY = ("stage 11, step", "outside the suite", "nothing")
# A section comment of the script; numbered ones begin a step.
MARK = re.compile(r"^# --- (?:([0-9]+)\. )?")
# The run's own messages, which name commands without running them.
MESSAGE = re.compile(r'(?<![\w-])(?:step|ok|fail|prerequisite) "(?:[^"\\]|\\.)*"')
PLACEHOLDER = re.compile(r"<[^<>\s][^<>]*>")
ANY = "\0"


# ---------------------------------------------------------------------------
# The guide
# ---------------------------------------------------------------------------

def _section(text: str) -> str:
    assert SECTION in text, "DEPLOYMENT.md has no section Upgrading to a new release"
    return text.split(SECTION, 1)[1].split("\n## ", 1)[0]


def _blocks(section: str) -> list:
    """The text of each ```bash or ```sh block of the section, in order."""
    blocks, fence, lines = [], None, []
    for line in section.splitlines():
        if fence is None:
            if line.startswith("```"):
                fence, lines = line[3:].strip(), []
        elif line.startswith("```"):
            if fence in ("bash", "sh"):
                blocks.append("\n".join(lines) + "\n")
            fence = None
        else:
            lines.append(line)
    return blocks


def _rows(section: str) -> list:
    """The table's rows as {piece, ran_by, notes}."""
    assert TABLE in section, "the section has no table of what ran its commands"
    rows = []
    for line in section.split(TABLE, 1)[1].splitlines():
        if not line.startswith("| ") or re.match(r"^\|[-| ]+\|$", line) or line.startswith("| Block |"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip()[1:-1])]
        assert len(cells) == 3, f"a row without three cells: {line}"
        piece = re.fullmatch(r"`([^`]+)`", cells[0])
        assert piece, f"a row without a piece of its block: {line}"
        rows.append({"piece": piece.group(1), "ran_by": cells[1], "notes": cells[2]})
    return rows


def _joined(block: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\\\n\s*", " ", block))


def _lines(block: str) -> list:
    """The command lines of a block: continuations joined, comments and the
    parentheses of a subshell taken off, placeholders as one word each."""
    lines = []
    for line in re.sub(r"\\\n\s*", " ", block).splitlines():
        line = re.sub(r"(?:^|\s+)#.*$", "", line).strip()
        line = re.sub(r"^\((.*)\)$", r"\1", line)
        if line:
            lines.append(PLACEHOLDER.sub(ANY, line))
    return lines


def _is_docker(line: str) -> bool:
    return re.match(r"(?:umask [0-7]+ && )?docker\s", line) is not None


def _pattern(line: str) -> re.Pattern:
    """A command line's words, in order, on one line of code, with any other
    words between them; a word holding a placeholder matches any one word."""
    words = []
    for word in line.split():
        if ANY in word:
            words.append(r"\S+")
        else:
            words.append(rf"(?<![^\s\"'(;]){re.escape(word)}(?![^\s\"');])")
    return re.compile(r"[ \t]+(?:\S+[ \t]+)*?".join(words))


# ---------------------------------------------------------------------------
# The stage 11 script, step by step
# ---------------------------------------------------------------------------

def _clean(code: str) -> str:
    """Continuations joined, comment lines and the run's messages dropped,
    and the run's wrappers for docker compose as the operator types it: dc,
    and down <log> [options]."""
    code = re.sub(r"\\\n\s*", " ", code)
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
    code = MESSAGE.sub('""', code)
    code = re.sub(r"(?m)^(\s*)down \S+", r"\1docker compose down", code)
    return re.sub(r"(?<![\w$-])dc ", "docker compose ", code)


def _script_steps(text: str) -> dict:
    """{step number: the code that step runs}: the lines of its numbered
    section, without the functions defined among them, and the bodies of the
    script's functions it calls, and of those they call."""
    lines = text.splitlines()
    functions, inside = _functions(lines)
    marks = [(number, MARK.match(line)) for number, line in enumerate(lines) if line.startswith("# --- ")]

    def called(code: str, seen: set) -> str:
        parts = [code]
        for name, body in functions.items():
            if name not in seen and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", code):
                seen.add(name)
                parts.append(called(_clean(body), seen))
        return "\n".join(parts)

    steps = {}
    for index, (start, mark) in enumerate(marks):
        if not mark.group(1):
            continue
        end = marks[index + 1][0] if index + 1 < len(marks) else len(lines)
        own = [lines[number] for number in range(start, end) if number not in inside]
        steps[int(mark.group(1))] = called(_clean("\n".join(own)), set())
    return steps


# ---------------------------------------------------------------------------
# The table against the blocks and the script
# ---------------------------------------------------------------------------

def _problems(text: str, steps: dict) -> list:
    """Every disagreement between the table, the section's blocks and stage 11."""
    section = _section(text)
    blocks, rows = _blocks(section), _rows(section)
    problems = []
    if len(rows) != len(blocks):
        problems.append(f"{len(blocks)} blocks of commands, {len(rows)} rows in the table")
    for number, (block, row) in enumerate(zip(blocks, rows), 1):
        where = f"row {number} (`{row['piece']}`)"
        if row["piece"] not in _joined(block):
            problems.append(f"{where}: not in its block, which begins {block.splitlines()[0]!r}")
        commands = []
        for line in _lines(block):
            if _is_docker(line):
                commands.append(line)
            else:
                problems.append(f"{where}: a command the check does not know: {line.replace(ANY, '<…>')!r}")
        if not row["ran_by"].startswith(RAN_BY):
            problems.append(f"{where}: says neither of {RAN_BY}: {row['ran_by']!r}")
        if not row["ran_by"].startswith("stage 11"):
            continue
        named = _steps_named(row["ran_by"])
        missing = [step for step in named if step not in steps]
        if not named or missing:
            problems.append(f"{where}: stage 11 has no step {missing or row['ran_by']}")
            continue
        patterns = [(line, _pattern(line)) for line in commands]
        for step in named:
            if not any(pattern.search(steps[step]) for _, pattern in patterns):
                problems.append(f"{where}: step {step} runs none of its commands")
        code = "\n".join(steps[step] for step in named)
        for line, pattern in patterns:
            if not pattern.search(code):
                problems.append(f"{where}: steps {named} do not run {line.replace(ANY, '<…>')}")
    return problems


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _in_section(text: str, old: str, new: str) -> str:
    """The guide with the first occurrence of old in the upgrade section
    replaced; the same commands occur elsewhere in the guide."""
    before, rest = text.split(SECTION, 1)
    assert old in rest, old
    return before + SECTION + rest.replace(old, new, 1)


def _steps() -> dict:
    return _script_steps(SCRIPT.read_text(encoding="utf-8"))


def test_every_block_of_the_upgrade_section_has_its_row_and_stage_11_runs_what_a_row_says() -> None:
    problems = _problems(_guide(), _steps())
    assert not problems, "\n".join(problems)
    section = _section(_guide())
    rows, blocks = _rows(section), _blocks(section)
    assert len(rows) >= 5, "the section's blocks of commands were not found"
    ran = [block for block, row in zip(blocks, rows) if row["ran_by"].startswith("stage 11")]
    commands = sum(len(_lines(block)) for block in ran)
    assert len(ran) >= 5 and commands >= 15, f"only {len(ran)} rows and {commands} commands of stage 11"


def test_the_steps_are_the_ones_the_testing_strategy_numbers() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    functions, _ = _functions(text.splitlines())
    assert len(functions) == len(re.findall(r"^[a-z_][a-z0-9_]*\(\) \{", text, re.M)) >= 40
    strategy = STRATEGY.read_text(encoding="utf-8")
    assert STRATEGY_SECTION in strategy
    section = strategy.split(STRATEGY_SECTION, 1)[1].split("\n## ", 1)[0]
    numbered = [int(n) for n in re.findall(r"(?m)^([0-9]+)\. \*\*", section)]
    assert numbered == list(range(1, len(numbered) + 1)) and len(numbered) >= 6, numbered
    steps = _steps()
    assert sorted(steps) == numbered


def test_the_step_parser_reads_the_script() -> None:
    steps = _steps()
    finds = {name: _pattern(PLACEHOLDER.sub(ANY, command)) for name, command in [
        ("counts", "docker compose exec -T postgres psql -U postgres -d people_db -XAtc "
         "'SELECT (SELECT count(*) FROM app.people), (SELECT count(*) FROM biblio.publications), "
         "(SELECT count(*) FROM biblio.publications_canon_web)'"),
        ("backup", "umask 077 && docker compose exec -T postgres pg_dump -U postgres -Fc people_db > people_db.dump"),
        ("files", "docker run --rm <image> tar -cC /opt/pubdex compose.yaml create_secrets.sh set_passwords.sh | tar -x"),
        ("down", "docker compose down"),
        ("volumes", "docker compose down --volumes"),
        ("up", "docker compose up --wait"),
        ("ps", "docker compose ps --all"),
        ("restore", "docker compose exec -T postgres pg_restore -U postgres -d people_db --single-transaction"),
    ]}
    runs = {step: {name for name, pattern in finds.items() if pattern.search(code)} for step, code in steps.items()}
    # Through new_stack, use_images and up; backup; switch_to, down and up.
    assert {"files", "up", "counts", "backup", "down"} <= runs[1] and "volumes" not in runs[1] and "restore" not in runs[1]
    assert {"files", "up", "down"} <= runs[2] and "backup" not in runs[2]
    assert "ps" in runs[4] and "ps" not in runs[3]
    assert {"volumes", "restore", "counts", "up"} <= runs[5] and "files" not in runs[5]
    assert {"files", "up", "backup", "down"} <= runs[6] and "counts" not in runs[6]
    # A message names a command without running it.
    message = _clean('ok "docker compose up --detach --no-deps api"')
    assert not _pattern("docker compose up --detach --no-deps api").search(message)


def test_every_word_of_every_command_is_checked() -> None:
    # Each word of each command, altered in its block alone, is found.
    text, steps = _guide(), _steps()
    altered = 0
    for block in _blocks(_section(text)):
        lines = block.splitlines()
        for index, line in enumerate(lines):
            holders = PLACEHOLDER.findall(line)
            masked = PLACEHOLDER.sub(ANY, line)
            command = re.sub(r"(?:^|\s+)#.*$", "", masked).strip()
            for word in command.split():
                word = word.strip("()")
                if ANY in word or not word:
                    continue
                changed = re.sub(rf"(?<![^\s(]){re.escape(word)}(?=\)?(?:\s|$))", word + "q", masked, count=1)
                assert changed != masked, (line, word)
                for holder in holders:
                    changed = changed.replace(ANY, holder, 1)
                block_changed = "\n".join(lines[:index] + [changed] + lines[index + 1:]) + "\n"
                assert _problems(_in_section(text, block, block_changed), steps), (line, word)
                altered += 1
    assert altered >= 100, altered


@pytest.mark.parametrize(
    ("change", "found"),
    [
        (("| stage 11, steps 4 and 5 |", "| stage 11, step 5 |"), "steps [5] do not run docker run"),
        (("| stage 11, steps 4 and 5 |", "| stage 11, steps 1 and 4 |"), "steps [1, 4] do not run docker compose stop api"),
        (("| stage 11, steps 1 and 6 |", "| stage 11, steps 1 and 7 |"), "stage 11 has no step [7]"),
        (("| stage 11, steps 1 to 6 |", "| stage 11, steps 1 to 3 |"), "do not run docker compose ps --all"),
        (("| stage 11, steps 1 and 6 |", "| tested |"), "says neither of"),
        (("| stage 11, steps 2 and 6 |", "| stage 11, steps 2, 5 and 6 |"), None),
        (("| `pubdex:<previous version>@sha256` |", "| `pubdex:<earlier version>@sha256` |"), "not in its block"),
        (("docker compose ps --all\n", "docker compose ps --all\ncurl -s http://127.0.0.1:8000/readyz\n"),
         "a command the check does not know"),
    ],
)
def test_a_wrong_row_would_be_found(change: tuple, found: str | None) -> None:
    text = _guide()
    problems = _problems(_in_section(text, change[0], change[1]), _steps())
    if found is None:
        assert not problems, problems
    else:
        assert any(found in problem for problem in problems), problems


def test_a_block_without_its_row_would_be_found() -> None:
    text = _guide().replace(TABLE, "\n```bash\ndocker compose ps\n```\n" + TABLE, 1)
    problems = _problems(text, _steps())
    assert any("blocks of commands" in problem for problem in problems), problems
