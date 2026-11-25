#!/usr/bin/env python3
"""The release notes of the checkout's version, checked against the tree.

    .venv/bin/python DB/check_release_notes.py [--root DIR]

CHANGELOG.md holds one section per PubDex release, headed `## PubDex
<version>`; the date of publication may follow on the same line. Its first
three subsections are, in this order, `### Schema changes`, `### Ingestion
changes` and `### Upgrade and rollback`, each present even where nothing
changed: "None." is then its whole text. Other subsections may follow them.

This check reads the section of the version VERSION holds and compares it
with the tree and with the preceding PubDex release, the newest one the
release record (releases/, DB/release_record.py) holds below VERSION. It
needs no Git history and no network. It fails when

  - VERSION holds a test release's version (-test.<name>), which only the
    release script gives a build;
  - the section, or one of its three headings, is missing, or the headings
    are not its first three subsections in that order;
  - a schema change the preceding release shipped is missing from DB/schema/
    or differs from the file the release record names;
  - Schema changes does not name a schema change of DB/schema/ that is new
    since the preceding release -- by its number as a code span, `0002`, or
    as its file's name, `0002_<name>` -- in a list item of its own that says
    "backward compatible" or "not backward compatible" as the file declares;
    names, as such a code span, a schema change that does not exist; or is
    not "None." although no schema change is new;
  - the transform version, people_pubs.__version__, differs from the
    preceding release's and Ingestion changes is "None." or does not name
    the new one; or it equals the preceding release's and Ingestion changes
    is not "None.";
  - Upgrade and rollback does not name the floor, which releases/FLOOR holds,
    and the preceding release; does not say, in one sentence with "floor"
    and "raised", that the floor was raised when FLOOR lies above the
    preceding release's floor; or, when a new schema change is not backward
    compatible, does not say, in one sentence with "rollback" and "is a
    restore", that the rollback is a restore of the backup;
  - VERSION is a patch release of the preceding release -- the same major and
    minor version, a later patch -- although a schema change is new or the
    transform version differs: a patch release changes neither the schema nor
    ingestion, and a minor release may change either.

It runs in stage 1 of the complete suite (run_checks.sh). --root names another
repository root, for tests. Exit status: 0 when the notes agree with the tree,
1 when they do not or a file cannot be read, 2 for a usage error.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, TextIO, Tuple

import release_record
from people_pubs.schema import Change, ChangeFileError, load_changes
from release_record import Record, RecordError

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = "CHANGELOG.md"
SCHEMA_DIR = "DB/schema"
VERSION_FILE = "DB/people_pubs/__init__.py"
HEADINGS = ("Schema changes", "Ingestion changes", "Upgrade and rollback")
NONE = "None."

SECTION_RE = re.compile(r"## PubDex (?P<version>[^ \t]+)(?:[ \t].*)?")
SUBSECTION_RE = re.compile(r"### (?P<title>.*?)[ \t]*")
LEVEL_RE = re.compile(r"(?P<hashes>#{1,6})[ \t]")
FENCE_RE = re.compile(r"[ \t]{0,3}(?P<fence>`{3,}|~{3,})")
ITEM_RE = re.compile(r"[ \t]*(?:[-*+]|[0-9]+[.)])[ \t]")
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
CHANGE_SPAN_RE = re.compile(r"(?:DB/schema/)?(?P<number>[0-9]{4})(?P<name>_[a-z0-9_]+)?(?:\.sql)?")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])[ \t]+")
TRANSFORM_VERSION_RE = re.compile(r'^__version__[ \t]*=[ \t]*"([^"]+)"', re.MULTILINE)
NOT_COMPATIBLE_RE = re.compile(r"\bnot[ \t]+backward[- ]compatible\b", re.IGNORECASE)
COMPATIBLE_RE = re.compile(r"\bbackward[- ]compatible\b", re.IGNORECASE)


@dataclass
class Notes:
    """The three fixed subsections of one PubDex release's section, as text."""
    schema: str
    ingestion: str
    upgrade: str
    schema_items: List[str] = field(default_factory=list)


def _blocks(lines: Sequence[str]) -> List[str]:
    """The list items and paragraphs of lines, each joined into one line."""
    blocks: List[str] = []
    current: List[str] = []
    for line in lines:
        if not line.strip():
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        if ITEM_RE.match(line) and current:
            blocks.append(" ".join(current))
            current = []
        current.append(line.strip())
    if current:
        blocks.append(" ".join(current))
    return blocks


def _headings(text: str) -> List[Tuple[int, int, str]]:
    """(line index, level, line) of every Markdown heading outside fenced code."""
    found = []
    fence: Optional[str] = None
    for index, line in enumerate(text.splitlines()):
        match = FENCE_RE.match(line)
        if match:
            marker = match.group("fence")
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not line.strip()[len(marker):].strip():
                fence = None
            continue
        if fence is None:
            level = LEVEL_RE.match(line)
            if level:
                found.append((index, len(level.group("hashes")), line.rstrip()))
    return found


def release_notes(text: str, version: str) -> Tuple[Optional[Notes], List[str]]:
    """The notes of version in the changelog text, or None and what is wrong."""
    lines = text.splitlines()
    headings = _headings(text)
    starts = []
    for position, (index, level, line) in enumerate(headings):
        match = SECTION_RE.fullmatch(line) if level == 2 else None
        if match is not None and match.group("version") == version:
            starts.append((position, index))
    if not starts:
        return None, [f"{CHANGELOG} has no section for PubDex {version}: add one headed '## PubDex {version}', "
                      f"whose first three subsections are {_titles()}"]
    if len(starts) > 1:
        return None, [f"{CHANGELOG} has {len(starts)} sections headed '## PubDex {version}'; it may have one"]
    position, start = starts[0]
    end = len(lines)
    subsections: List[Tuple[str, int]] = []
    for index, level, line in headings[position + 1:]:
        if level <= 2:
            end = index
            break
        if level == 3:
            subsections.append((SUBSECTION_RE.fullmatch(line).group("title"), index))
    titles = [title for title, _ in subsections[:3]]
    if titles != list(HEADINGS):
        missing = [title for title in HEADINGS if title not in titles]
        what = (f"lacks {', '.join(repr('### ' + title) for title in missing)}" if missing
                else f"has them in the order {', '.join(titles)}")
        return None, [f"the section of PubDex {version} in {CHANGELOG} has to begin with the subsections "
                      f"{_titles()}, in this order; it {what}"]
    bodies = []
    for number, (_, index) in enumerate(subsections[:3]):
        stop = subsections[number + 1][1] if number + 1 < len(subsections) else end
        bodies.append(lines[index + 1:stop])
    schema, ingestion, upgrade = ("\n".join(body).strip() for body in bodies)
    return Notes(schema, ingestion, upgrade, _blocks(bodies[0])), []


def _titles() -> str:
    return ", ".join(f"'### {title}'" for title in HEADINGS)


def _is_none(text: str) -> bool:
    return text.strip().rstrip(".").strip().lower() == "none"


def _names(text: str) -> List[Tuple[int, Optional[str], str]]:
    """The schema changes a text names as code spans: (number, name or None, the span)."""
    found = []
    for span in CODE_SPAN_RE.findall(text):
        match = CHANGE_SPAN_RE.fullmatch(span.strip())
        if match:
            name = match.group("name")
            found.append((int(match.group("number")), match.group("number") + name if name else None, span))
    return found


def _sentences(text: str) -> List[str]:
    return [sentence for sentence in SENTENCE_END_RE.split(" ".join(text.split())) if sentence]


def _says(text: str, *words: str) -> bool:
    """Whether one sentence of text holds every one of words (any case)."""
    return any(all(word.lower() in sentence.lower() for word in words) for sentence in _sentences(text))


def _mentions(text: str, version: str) -> bool:
    return re.search(r"(?<![0-9A-Za-z.])" + re.escape(version) + r"(?![0-9A-Za-z]|\.[0-9])", text) is not None


def _compatibility(change: Change) -> str:
    return "backward compatible" if change.backward_compatible else "not backward compatible"


def transform_version(root: Path) -> str:
    try:
        text = (root / VERSION_FILE).read_text(encoding="utf-8")
    except OSError as error:
        raise RecordError(f"cannot read {VERSION_FILE}: {error}") from error
    match = TRANSFORM_VERSION_RE.search(text)
    if not match:
        raise RecordError(f"{VERSION_FILE} sets no __version__")
    return match.group(1)


def _core(version: str) -> Tuple[int, int, int]:
    match = release_record.VERSION_RE.fullmatch(version)
    assert match is not None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


@dataclass
class Result:
    version: str
    problems: List[str]
    summary: str = ""


def check(root: Path = ROOT) -> Result:
    """Every way the notes of VERSION disagree with the tree, and a summary."""
    record = Record(root)
    try:
        version = record.checkout_version()
    except RecordError as error:
        return Result("?", [str(error)])
    problems: List[str] = []
    if release_record.is_test_version(version):
        problems.append(f"VERSION holds {version}, a test release's version: only the release script gives "
                        "one to a build, and VERSION never holds one")
    try:
        preceding, _ = record.preceding(version)
        previous = record.entry(preceding)
        floor = record.floor()
        previous_floor = previous["floor"]
        tree_transform = transform_version(root)
        changes = load_changes(root / SCHEMA_DIR)
    except (RecordError, ChangeFileError) as error:
        return Result(version, problems + [str(error)])
    try:
        text = (root / CHANGELOG).read_text(encoding="utf-8")
    except OSError as error:
        return Result(version, problems + [f"cannot read {CHANGELOG}: {error}"])

    by_number: Dict[int, Change] = {change.number: change for change in changes}
    shipped = {item["number"]: item for item in previous["schema_changes"]}
    for number, item in sorted(shipped.items()):
        change = by_number.get(number)
        if change is None or change.name != item["name"]:
            problems.append(f"schema change {item['name']} of PubDex {preceding} is missing from {SCHEMA_DIR}/: "
                            "a schema change, once released, stays as it is")
        elif change.sha256 != item["sha256"]:
            problems.append(f"{SCHEMA_DIR}/{change.name}.sql differs from the file PubDex {preceding} shipped "
                            f"(SHA-256 {item['sha256']} in the release record): a schema change, once released, "
                            "stays as it is")
    new = [change for change in changes if change.number not in shipped]

    core, before = _core(version), _core(preceding)
    patch_release = core[:2] == before[:2] and core[2] > before[2]
    next_minor = f"{core[0]}.{core[1] + 1}.0"
    if patch_release and new:
        problems.append(f"{version} is a patch release of {preceding}, which changes no schema, but "
                        f"{', '.join(change.name for change in new)} {'is' if len(new) == 1 else 'are'} new: "
                        f"a schema change needs a minor release, such as {next_minor}")
    if patch_release and tree_transform != previous["transform_version"]:
        problems.append(f"{version} is a patch release of {preceding}, which changes no ingestion, but the "
                        f"transform version is {tree_transform}, not {previous['transform_version']}: an "
                        f"ingestion change needs a minor release, such as {next_minor}")

    notes, missing = release_notes(text, version)
    if notes is None:
        return Result(version, problems + missing)

    # Schema changes
    named = _names(notes.schema)
    for number, name, span in named:
        change = by_number.get(number)
        if change is None:
            problems.append(f"Schema changes names `{span}`, but {SCHEMA_DIR}/ holds no schema change {number:04d}")
        elif name is not None and name != change.name:
            problems.append(f"Schema changes names `{span}`, but schema change {number:04d} is {change.name}")
    for change in new:
        own_items = [item for item in notes.schema_items
                     if {number for number, _, _ in _names(item)} == {change.number}]
        if not own_items:
            problems.append(f"Schema changes does not name {change.name}, new since PubDex {preceding}, in a list "
                            f"item of its own: name it as `{change.number:04d}` and say that it is "
                            f"{_compatibility(change)}")
            continue
        right = [item for item in own_items
                 if (NOT_COMPATIBLE_RE.search(item) is not None) == (not change.backward_compatible)
                 and COMPATIBLE_RE.search(item) is not None]
        if not right:
            problems.append(f"Schema changes does not say that {change.name} is {_compatibility(change)}, as its "
                            f"file declares, in the list item that names `{change.number:04d}`")
    if not new and not _is_none(notes.schema):
        problems.append(f"Schema changes has to be '{NONE}': no schema change is new since PubDex {preceding}")

    # Ingestion changes
    old_transform = previous["transform_version"]
    if tree_transform == old_transform:
        if not _is_none(notes.ingestion):
            problems.append(f"Ingestion changes has to be '{NONE}': the transform version is {tree_transform}, "
                            f"as in PubDex {preceding}")
    elif _is_none(notes.ingestion):
        problems.append(f"Ingestion changes says '{NONE}', but the transform version is {tree_transform}, "
                        f"not {old_transform} as in PubDex {preceding}: say what changed")
    elif not _mentions(notes.ingestion, tree_transform):
        problems.append(f"Ingestion changes does not name the transform version {tree_transform} "
                        f"(PubDex {preceding}: {old_transform})")

    # Upgrade and rollback
    if not _mentions(notes.upgrade, floor):
        problems.append(f"Upgrade and rollback does not name the floor, {floor} (releases/FLOOR)")
    if not _mentions(notes.upgrade, preceding):
        problems.append(f"Upgrade and rollback does not name the preceding PubDex release, {preceding}")
    raised = (previous_floor is not None
              and release_record.precedence(floor) > release_record.precedence(previous_floor))
    if raised and not _says(notes.upgrade, "floor", "raised"):
        problems.append(f"releases/FLOOR raises the floor from {previous_floor}, the floor of PubDex "
                        f"{preceding}, to {floor}, but Upgrade and rollback does not say so: say in one "
                        "sentence that the floor was raised")
    incompatible = [change.name for change in new if not change.backward_compatible]
    if incompatible and not _says(notes.upgrade, "rollback", "is a restore"):
        problems.append(f"{', '.join(incompatible)} {'is' if len(incompatible) == 1 else 'are'} not backward "
                        "compatible, but Upgrade and rollback does not say that the rollback is a restore: say "
                        f"in one sentence that the rollback to PubDex {preceding} is a restore of the backup "
                        "taken before the upgrade")

    new_text = ", ".join(f"{change.name} ({_compatibility(change)})" for change in new) or "none"
    transform_text = (f"{old_transform} -> {tree_transform}" if tree_transform != old_transform
                      else f"{tree_transform}, unchanged")
    summary = (f"PubDex {version} against the preceding PubDex release {preceding}: new schema changes {new_text}; "
               f"transform version {transform_text}; floor {floor}")
    return Result(version, problems, summary)


def main(argv: Optional[Sequence[str]] = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    parser = argparse.ArgumentParser(prog="check_release_notes.py",
                                     description="Checks the release notes of VERSION in CHANGELOG.md against the tree.")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root (default: this checkout)")
    arguments = parser.parse_args(argv)
    result = check(arguments.root)
    if result.problems:
        print(f"release notes: PubDex {result.version}: {len(result.problems)} problem(s):", file=err)
        for problem in result.problems:
            print(f"  - {problem}", file=err)
        return 1
    print(f"release notes: {result.summary}: {CHANGELOG} agrees.", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
