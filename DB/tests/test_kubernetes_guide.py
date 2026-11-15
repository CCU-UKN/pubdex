"""KUBERNETES.md, the operator guide of the Helm chart, offline.

The guide's appendix, *What ran these commands*, has one row for every block
of shell commands, in the guide's order: the block's section, a piece of its
text, and what ran it -- steps of stage 9 (run_cluster_acceptance.sh), stage
8's tests only, one run outside the suite, or nothing. These tests keep that
table checkable without a cluster: each row matches its block; each step a
row names exists; each kubectl and helm command of a block that stage 9 ran
is a subcommand those steps run, in their own code or through the script's
functions they call, and each of its long options occurs in that code; each
stage 8 test a row names exists; and the guide's table of values names every
key of the chart's values.yaml, so that every value can be found from the
guide. Whether the commands work is what stage 9 shows by running them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "KUBERNETES.md"
SCRIPT = ROOT / "run_cluster_acceptance.sh"
VALUES = ROOT / "charts" / "pubdex" / "values.yaml"
APPENDIX = "\n## Appendix: what ran these commands\n"

# The old form, a note under each block, is replaced by the appendix.
MARK = re.compile(r"\*(Tested|Tested outside the suite|Untested):\*")
# What the appendix's third column may say, by its first words.
RAN_BY = ("stage 9, step", "stage 8 only", "outside the suite", "nothing")
# The kubectl subcommands that take a second word naming what they act on.
TWO_WORDS = {"create", "rollout", "label"}
# Values whose content is free-form or a list, documented as one key.
LEAF_KEYS = {"resources", "annotations", "tasks"}


# ---------------------------------------------------------------------------
# The guide
# ---------------------------------------------------------------------------

def _slug(heading: str) -> str:
    """The anchor a forge gives a heading: lower case, punctuation dropped,
    spaces as hyphens."""
    return re.sub(r"[^a-z0-9 _-]", "", heading.strip().lower()).replace(" ", "-")


def _blocks(text: str) -> list:
    """(the commands of each ```sh block, the heading of its section), in order."""
    blocks, heading, fence, lines = [], "", None, []
    for line in text.splitlines():
        if fence is None:
            if line.startswith("```"):
                fence, lines = line[3:].strip(), []
            elif re.match(r"^#{2,3} ", line):
                heading = line.lstrip("#").strip()
        elif line.startswith("```"):
            if fence == "sh":
                blocks.append(("\n".join(lines) + "\n", heading))
            fence = None
        else:
            lines.append(line)
    return blocks


def _rows(text: str) -> list:
    """The appendix's rows as {section, anchor, piece, ran_by, notes}."""
    assert APPENDIX in text, "the guide has no appendix of what ran its commands"
    table = text.split(APPENDIX, 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in table.splitlines():
        if not line.startswith("| ") or re.match(r"^\|[-| ]+\|$", line) or line.startswith("| Section |"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip()[1:-1])]
        assert len(cells) == 4, f"an appendix row without four cells: {line}"
        link = re.fullmatch(r"\[([^]]+)\]\(#([a-z0-9_-]+)\)(?:,.*)?", cells[0])
        piece = re.match(r"`([^`]+)`", cells[1])
        assert link and piece, f"an appendix row without a section link or a piece of the block: {line}"
        rows.append({"section": link.group(1), "anchor": link.group(2), "piece": piece.group(1),
                     "ran_by": cells[2], "notes": cells[3]})
    return rows


def _joined(block: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\\\n\s*", " ", block))


def _commands(block: str) -> list:
    """Each kubectl or helm command of a block, as its words, with
    continuation lines joined and comments dropped. A command ends at a
    pipe, a semicolon or the end of a command substitution."""
    lines = re.sub(r"\\\n\s*", " ", block).splitlines()
    commands = []
    for line in lines:
        line = re.sub(r"\s+#.*$", "", line)
        for match in re.finditer(r"(?:^|[\s|;(])(kubectl|helm)\s+([^|;)]*)", line):
            commands.append([match.group(1)] + match.group(2).split())
    return commands


def _subcommand(words: list) -> str:
    tool, rest = words[0], words[1:]
    if rest[:1] == ["--namespace"]:
        rest = rest[2:]
    if tool == "kubectl" and rest[0] in TWO_WORDS:
        return " ".join(rest[:2])
    return rest[0]


def _steps_named(ran_by: str) -> list:
    """The step numbers of "stage 9, step 4", "steps 13 and 20", "steps 25 to 29"."""
    words = ran_by.split(",", 1)[1]
    numbers = []
    for first, last in re.findall(r"(\d+)(?: to (\d+))?", words):
        numbers.extend(range(int(first), int(last or first) + 1))
    return numbers


# ---------------------------------------------------------------------------
# The stage 9 script, step by step
# ---------------------------------------------------------------------------

def _functions(lines: list) -> tuple:
    """({name: body}, the line numbers of all function definitions)."""
    functions, inside, number = {}, set(), 0
    while number < len(lines):
        match = re.match(r"^([a-z_][a-z0-9_]*)\(\) \{(.*)$", lines[number])
        if not match:
            number += 1
            continue
        rest = match.group(2).rstrip()
        if rest.endswith("}") and not rest.lstrip().startswith("#"):
            functions[match.group(1)], end = rest, number
        else:
            end = lines.index("}", number + 1)
            functions[match.group(1)] = "\n".join(lines[number + 1:end])
        inside.update(range(number, end + 1))
        number = end + 1
    return functions, inside


def _script_steps(text: str) -> dict:
    """{step number: the code that step runs}: its own lines, without the
    functions defined among them and without comments, and the bodies of the
    script's functions it calls, and of those they call."""
    lines = text.splitlines()
    functions, inside = _functions(lines)
    starts = [number for number, line in enumerate(lines) if re.match(r'^step [0-9]+ "', line)]

    def called(code: str, seen: set) -> str:
        parts = [code]
        for name, body in functions.items():
            if name not in seen and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", code):
                seen.add(name)
                parts.append(called(body, seen))
        return "\n".join(parts)

    steps = {}
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        own = [lines[number] for number in range(start, end)
               if number not in inside and not lines[number].lstrip().startswith("#")]
        steps[index + 1] = called("\n".join(own), set())
    return steps


def _runs(code: str) -> set:
    """The kubectl and helm subcommands a piece of the script runs, through
    its wrappers: helm_ and helm_run for helm; k, kn <namespace>,
    kube "<what>" <namespace> and kubectl_ for kubectl."""
    code = re.sub(r"\\\n\s*", " ", code)
    found = set()
    for verb in re.findall(r"\bhelm_(?:run)? ([a-z]+)\b", code):
        found.add(("helm", verb))
    kubectl = (r"(?:\bk |\bkn \S+ |\bkube \"[^\"]*\" \S+ |\bkubectl_ (?:--namespace \S+ )?)"
               r"([a-z][a-z-]*)(?: ([a-z][a-z-]*))?")
    for verb, second in re.findall(kubectl, code):
        found.add(("kubectl", f"{verb} {second}" if verb in TWO_WORDS else verb))
    return found


# ---------------------------------------------------------------------------
# The appendix against the blocks and the script
# ---------------------------------------------------------------------------

def _problems(text: str, script: str) -> list:
    """Every disagreement between the appendix, the guide's blocks and stage 9."""
    blocks, rows, steps = _blocks(text), _rows(text), _script_steps(script)
    problems = []
    if len(rows) != len(blocks):
        problems.append(f"{len(blocks)} blocks of commands, {len(rows)} rows in the appendix")
    for number, ((block, heading), row) in enumerate(zip(blocks, rows), 1):
        where = f"row {number} ({row['section']}, `{row['piece']}`)"
        if row["anchor"] != _slug(heading) or row["section"] != heading:
            problems.append(f"{where}: its block is in the section {heading!r}")
        if row["piece"] not in _joined(block):
            problems.append(f"{where}: not in its block, which begins {block.splitlines()[0]!r}")
        if not row["ran_by"].startswith(RAN_BY):
            problems.append(f"{where}: says neither of {RAN_BY}: {row['ran_by']!r}")
        if row["ran_by"].startswith("stage 8 only"):
            for target in re.findall(r"\]\(([^)]+)\)", row["ran_by"]) or ["(no link)"]:
                if not re.fullmatch(r"charts/tests/test_[a-z_]+\.py", target) or not (ROOT / target).is_file():
                    problems.append(f"{where}: names no stage 8 test file: {target}")
        if not row["ran_by"].startswith("stage 9"):
            continue
        named = _steps_named(row["ran_by"])
        missing = [step for step in named if step not in steps]
        if not named or missing:
            problems.append(f"{where}: stage 9 has no step {missing or row['ran_by']}")
            continue
        code = "\n".join(steps[step] for step in named)
        runs = _runs(code)
        block_runs = {(words[0], _subcommand(words)) for words in _commands(block)}
        for step in named:
            if not _runs(steps[step]) & block_runs:
                problems.append(f"{where}: step {step} runs none of its commands")
        for words in _commands(block):
            if (words[0], _subcommand(words)) not in runs:
                problems.append(f"{where}: steps {named} do not run {words[0]} {_subcommand(words)}")
            for option in sorted({word.split("=", 1)[0] for word in words if re.match(r"--[a-z]", word)}):
                if not re.search(rf"(?<![\w-]){re.escape(option)}(?![\w-])", code):
                    problems.append(f"{where}: steps {named} never use {option}: {' '.join(words)}")
    return problems


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_every_block_of_commands_has_its_row_and_stage_9_runs_what_a_row_says() -> None:
    problems = _problems(_guide(), _script())
    assert not problems, "\n".join(problems)
    rows = _rows(_guide())
    assert len(rows) >= 25, "the guide's blocks of commands were not found"
    ran = [row for row in rows if row["ran_by"].startswith("stage 9")]
    commands = sum(len(_commands(block)) for (block, _), row in zip(_blocks(_guide()), rows) if row in ran)
    assert len(ran) >= 15 and commands >= 30, f"only {len(ran)} rows and {commands} commands of stage 9"


def test_the_appendix_replaces_the_notes_under_the_blocks() -> None:
    assert not MARK.search(_guide()), "a note under a block: the appendix holds what ran each block"


def test_the_step_parser_reads_the_script() -> None:
    text = _script()
    functions, _ = _functions(text.splitlines())
    assert len(functions) == len(re.findall(r"^[a-z_][a-z0-9_]*\(\) \{", text, re.M)) >= 60
    steps = _script_steps(text)
    count = int(re.search(r"^STEPS=([0-9]+)$", text, re.M).group(1))
    assert sorted(steps) == list(range(1, count + 1))
    assert {("kubectl", "create namespace"), ("kubectl", "label namespace"),
            ("kubectl", "create secret")} <= _runs(steps[4]), "through restricted_namespace"
    assert ("helm", "install") in _runs(steps[6]) and ("helm", "install") not in _runs(steps[4])
    assert {("kubectl", "rollout restart"), ("kubectl", "rollout status")} <= _runs(steps[17]), "through restart_api"
    assert ("helm", "rollback") in _runs(steps[19]) and ("helm", "rollback") not in _runs(steps[18])
    assert "--namespace" in steps[6], "through helm_run"
    for expected in [("helm", "upgrade"), ("helm", "uninstall"), ("helm", "test"), ("helm", "history"),
                     ("kubectl", "exec"), ("kubectl", "run"), ("kubectl", "port-forward"), ("kubectl", "create job")]:
        assert any(expected in _runs(code) for code in steps.values()), expected
    assert not any({("helm", "lint"), ("kubectl", "apply")} & _runs(code) for code in steps.values())


@pytest.mark.parametrize(
    ("change", "found"),
    [
        (("| stage 9, steps 6 and 26 |", "| stage 9, step 5 |"), "steps [5] do not run helm install"),
        (("| stage 9, steps 6 and 26 |", "| stage 9, steps 5 and 26 |"), "step 5 runs none of its commands"),
        (("| stage 9, steps 6 and 26 |", "| stage 9, steps 6 and 30 |"), "stage 9 has no step [30]"),
        (("| stage 9, steps 6 and 26 |", "| tested |"), "says neither of"),
        (("--wait` | stage 9, step 18 |", "--wait` | stage 9, step 16 |"), "never use --set"),
        (("serviceaccount` | stage 9, step 18 |", "serviceaccount` | stage 9, step 19 |"),
         "steps [19] do not run kubectl delete"),
        (("--values values-demo.yaml --wait", "--values values-demo.yaml --atomic --wait"), "never use --atomic"),
        (("020_role_passwords.sh --stdin\n", "020_role_passwords.sh --file\n"), "not in its block"),
        (("[`test_render_network.py`](charts/tests/test_render_network.py)",
          "[`test_render_ingress.py`](charts/tests/test_render_ingress.py)"), "names no stage 8 test file"),
        (("\n## Ingress and TLS\n", "\n## Ingress\n"), "its block is in the section 'Ingress'"),
        (("| stage 9, steps 13, 20 and 29 |", "| stage 9, steps 13 to 29 |"), "runs none of its commands"),
    ],
)
def test_a_wrong_row_would_be_found(change: tuple, found: str | None) -> None:
    text = _guide()
    assert change[0] in text, change[0]
    problems = _problems(text.replace(change[0], change[1], 1), _script())
    if found is None:
        assert not problems, problems
    else:
        assert any(found in problem for problem in problems), problems


def test_a_block_without_its_row_would_be_found() -> None:
    text = _guide().replace("\n## Contents\n", "\n```sh\nhelm list\n```\n\n## Contents\n", 1)
    problems = _problems(text, _script())
    assert any("blocks of commands" in problem for problem in problems), problems


def test_the_guide_names_every_value() -> None:
    values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    keys = _leaf_keys(values)
    assert len(keys) >= 40 and "externalDatabase.setup.adminSecret.key" in keys
    table = _guide().split("\n## Values\n", 1)[1].split("\n## ", 1)[0]
    missing = [key for key in keys if f"`{key}`" not in table]
    assert not missing, f"the table of values in KUBERNETES.md lacks {missing}"


def _leaf_keys(node: dict, prefix: str = "") -> list:
    keys = []
    for key, value in node.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value and key not in LEAF_KEYS:
            keys.extend(_leaf_keys(value, f"{name}."))
        else:
            keys.append(name)
    return keys
