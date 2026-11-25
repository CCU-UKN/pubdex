#!/usr/bin/env python3
"""The release record, releases/ at the repository root, read and checked.

    python3 DB/release_record.py entry <version>           that PubDex release's entry, as JSON
    python3 DB/release_record.py field <version> <name>...  fields of it, one a line
    python3 DB/release_record.py preceding [--version <v>]  the PubDex release this checkout upgrades from
    python3 DB/release_record.py floor                      the oldest PubDex release an upgrade to this checkout may start from

Each published PubDex release has a directory releases/<version>/ with a
file release.json (releases/README.md describes every field), and
releases/FLOOR names the floor: one line, a recorded version. This script
reads them and nothing else: no network, no Git, no write.

- entry checks the release's file against the rules below and prints it.
- field prints the named fields, one a line, by their names in the file,
  with a dot between levels (images.application, chart.commit). A text field
  prints as it is, any other as JSON (null, a list).
- preceding prints the newest recorded PubDex release below the checkout's
  version, which VERSION holds, or --version gives; the pre-release part of
  a test release's version (-test.<name>) is left out before comparing.
  Versions compare as Semantic Versioning 2.0.0 orders them.
- floor prints the version releases/FLOOR names, once that release's entry
  passes the checks.

The checks of an entry: exactly the fields README.md lists; version the
directory's name, a release version (MAJOR.MINOR.PATCH, optionally
-<pre-release>), never a test release's; commit a full commit id; tag
v<version>; published a date (YYYY-MM-DD); platform linux/amd64; images.application
and images.postgres each <registry>/<repository>:<version>@sha256:<digest>;
schema_changes a list of {number, name, sha256} numbered from 1 without a
gap, as the files of DB/schema/ are; transform_version a version; floor null
or a version not above the release's own; chart a version, a full commit id
and a package file name or null.

--root names another repository root, for tests. Exit status: 0, 1 when the
record is missing, malformed or holds no such release, 2 for a usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, TextIO, Tuple

ROOT = Path(__file__).resolve().parents[1]
RECORD = "releases"
FLOOR = "FLOOR"
ENTRY = "release.json"
PLATFORM = "linux/amd64"
# The same rule as RELEASE_VERSION_RE in DB/lib/release_identity.sh.
VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?")
COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
DATE_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
CHANGE_NAME_RE = re.compile(r"([0-9]{4})_[a-z0-9_]+")
IMAGE_RE = re.compile(r"(?P<registry>[a-z0-9.-]+(?::[0-9]+)?)/(?P<repository>[a-z0-9._/-]+):(?P<tag>[0-9A-Za-z._-]+)"
                      r"@(?P<digest>sha256:[0-9a-f]{64})")
PACKAGE_RE = re.compile(r"[a-z0-9-]+-[0-9A-Za-z.+-]+\.tgz")
FIELDS = ("version", "commit", "tag", "published", "platform", "images", "schema_changes", "transform_version",
          "floor", "chart")
IMAGES = ("application", "postgres")
CHANGE_FIELDS = ("number", "name", "sha256")
CHART_FIELDS = ("version", "commit", "package")


class RecordError(Exception):
    """The release record is missing, malformed or holds no such release."""


def _parts(version: str) -> Optional[Tuple[int, int, int, Optional[str]]]:
    match = VERSION_RE.fullmatch(version) if isinstance(version, str) else None
    if not match:
        return None
    pre = match.group(4)
    if pre is not None and "" in pre.split("."):
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), pre


def is_version(version: object) -> bool:
    """A release version: MAJOR.MINOR.PATCH, optionally -<pre-release>."""
    return isinstance(version, str) and _parts(version) is not None


def is_test_version(version: str) -> bool:
    """A test release's version: its pre-release part begins with test."""
    parts = _parts(version)
    return parts is not None and parts[3] is not None and parts[3].startswith("test")


def without_test(version: str) -> str:
    """The version without a test release's pre-release part."""
    return version.split("-", 1)[0] if is_test_version(version) else version


def precedence(version: str) -> tuple:
    """A key that orders versions as Semantic Versioning 2.0.0 does."""
    parts = _parts(version)
    if parts is None:
        raise ValueError(f"not a version: {version!r}")
    major, minor, patch, pre = parts
    if pre is None:
        return (major, minor, patch), 1, ()
    identifiers = tuple((0, int(item), "") if item.isdigit() else (1, 0, item) for item in pre.split("."))
    return (major, minor, patch), 0, identifiers


def _check(condition: bool, version: str, message: str) -> None:
    if not condition:
        raise RecordError(f"{RECORD}/{version}/{ENTRY}: {message}")


def check_entry(version: str, entry: object) -> Dict:
    """The entry of releases/<version>/release.json, or RecordError naming what is wrong."""
    _check(isinstance(entry, dict), version, "is not a JSON object")
    assert isinstance(entry, dict)
    _check(set(entry) == set(FIELDS), version, f"has the fields {', '.join(entry)}, not {', '.join(FIELDS)}")
    _check(entry["version"] == version, version, f"names the version {entry['version']!r}, not its directory's {version}")
    _check(is_version(version) and not is_test_version(version), version,
           "the directory's name is no release version, or a test release's, which the record never holds")
    _check(isinstance(entry["commit"], str) and bool(COMMIT_RE.fullmatch(entry["commit"])), version,
           "commit is not a full commit id")
    _check(entry["tag"] == f"v{version}", version, f"tag is {entry['tag']!r}, not v{version}")
    _check(isinstance(entry["published"], str) and bool(DATE_RE.fullmatch(entry["published"])), version,
           "published is not a date YYYY-MM-DD")
    _check(entry["platform"] == PLATFORM, version, f"platform is {entry['platform']!r}, not {PLATFORM}")
    images = entry["images"]
    _check(isinstance(images, dict) and set(images) == set(IMAGES), version,
           f"images has to name exactly {' and '.join(IMAGES)}")
    for name in IMAGES:
        match = IMAGE_RE.fullmatch(images[name]) if isinstance(images[name], str) else None
        _check(match is not None, version, f"images.{name} is not <registry>/<repository>:<tag>@sha256:<digest>")
        assert match is not None
        _check(match.group("tag") == version, version, f"images.{name} has the tag {match.group('tag')}, not {version}")
    changes = entry["schema_changes"]
    _check(isinstance(changes, list), version, "schema_changes is not a list")
    for position, change in enumerate(changes, start=1):
        _check(isinstance(change, dict) and set(change) == set(CHANGE_FIELDS), version,
               f"schema change {position} has to have exactly the fields {', '.join(CHANGE_FIELDS)}")
        name_match = CHANGE_NAME_RE.fullmatch(change["name"]) if isinstance(change["name"], str) else None
        _check(change["number"] == position and type(change["number"]) is int, version,
               f"schema change {position} has the number {change['number']!r}: the numbers run from 1 without a gap")
        _check(name_match is not None and int(name_match.group(1)) == position, version,
               f"schema change {position} has the name {change['name']!r}, not NNNN_<name> with its number")
        _check(isinstance(change["sha256"], str) and bool(SHA256_RE.fullmatch(change["sha256"])), version,
               f"schema change {position} has no SHA-256")
    _check(is_version(entry["transform_version"]), version, "transform_version is not a version")
    floor = entry["floor"]
    _check(floor is None or (is_version(floor) and precedence(floor) <= precedence(version)), version,
           f"floor is {floor!r}: null or a version not above {version}")
    chart = entry["chart"]
    _check(isinstance(chart, dict) and set(chart) == set(CHART_FIELDS), version,
           f"chart has to have exactly the fields {', '.join(CHART_FIELDS)}")
    _check(is_version(chart["version"]), version, "chart.version is not a version")
    _check(isinstance(chart["commit"], str) and bool(COMMIT_RE.fullmatch(chart["commit"])), version,
           "chart.commit is not a full commit id")
    _check(chart["package"] is None or (isinstance(chart["package"], str) and bool(PACKAGE_RE.fullmatch(chart["package"]))),
           version, "chart.package is neither null nor the file name of a chart package")
    return entry


class Record:
    """The release record of a repository root."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.directory = root / RECORD

    def versions(self) -> List[str]:
        """Every recorded version, oldest first; a directory that is no release version is an error."""
        if not self.directory.is_dir():
            raise RecordError(f"there is no release record: {RECORD}/ is missing")
        found = sorted(path.name for path in self.directory.iterdir() if path.is_dir())
        for name in found:
            if not is_version(name) or is_test_version(name):
                raise RecordError(f"{RECORD}/{name}/ is not named by a release version; a test release is never recorded")
        return sorted(found, key=precedence)

    def entry(self, version: str) -> Dict:
        path = self.directory / version / ENTRY
        if not is_version(version) or not path.is_file():
            known = ", ".join(self.versions()) or "none"
            raise RecordError(f"no PubDex release {version} is recorded (recorded: {known})")
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise RecordError(f"{RECORD}/{version}/{ENTRY} cannot be read as JSON: {error}") from error
        return check_entry(version, entry)

    def checkout_version(self) -> str:
        path = self.root / "VERSION"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise RecordError(f"cannot read VERSION: {error}") from error
        version = lines[0].strip() if lines else ""
        if not is_version(version):
            raise RecordError(f"VERSION holds {version!r}, not a release version")
        return version

    def preceding(self, version: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """The preceding release of that version (VERSION's by default), and no note."""
        version = without_test(version or self.checkout_version())
        versions = self.versions()
        below = [item for item in versions if precedence(item) < precedence(version)]
        if below:
            self.entry(below[-1])
            return below[-1], None
        raise RecordError(f"no PubDex release below {version} is recorded (recorded: {', '.join(versions) or 'none'})")

    def floor(self) -> str:
        path = self.directory / FLOOR
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise RecordError(f"cannot read {RECORD}/{FLOOR}: {error}") from error
        if len(lines) != 1 or not is_version(lines[0]):
            raise RecordError(f"{RECORD}/{FLOOR} has to hold one line, a release version")
        self.entry(lines[0])
        return lines[0]


def field(entry: Dict, name: str) -> str:
    value: object = entry
    for key in name.split("."):
        if not isinstance(value, dict) or key not in value:
            raise RecordError(f"the entry of {entry['version']} has no field {name}")
        value = value[key]
    return value if isinstance(value, str) else json.dumps(value)


def main(argv: Optional[Sequence[str]] = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    parser = argparse.ArgumentParser(prog="release_record.py", description="Reads the release record, releases/.")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root (default: this checkout)")
    commands = parser.add_subparsers(dest="command", required=True)
    entry_command = commands.add_parser("entry", help="a PubDex release's entry, as JSON")
    entry_command.add_argument("version")
    field_command = commands.add_parser("field", help="fields of a PubDex release's entry, one a line")
    field_command.add_argument("version")
    field_command.add_argument("names", nargs="+")
    preceding_command = commands.add_parser("preceding", help="the PubDex release this checkout upgrades from")
    preceding_command.add_argument("--version", help="instead of VERSION")
    commands.add_parser("floor", help="the version releases/FLOOR names")
    arguments = parser.parse_args(argv)
    record = Record(arguments.root)
    try:
        if arguments.command == "entry":
            print(json.dumps(record.entry(arguments.version), indent=2), file=out)
        elif arguments.command == "field":
            entry = record.entry(arguments.version)
            for name in arguments.names:
                print(field(entry, name), file=out)
        elif arguments.command == "preceding":
            if arguments.version is not None and not is_version(arguments.version):
                parser.error(f"--version needs a release version such as 0.2.0, not {arguments.version!r}")
            version, note = record.preceding(arguments.version)
            if note:
                print(f"release_record.py: {note}", file=err)
            print(version, file=out)
        else:
            print(record.floor(), file=out)
    except RecordError as error:
        print(f"release_record.py: {error}", file=err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
