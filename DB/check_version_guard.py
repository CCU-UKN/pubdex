#!/usr/bin/env python3
"""A published version never gets different content.

    python3 DB/check_version_guard.py [--root DIR] [--version VERSION]

When VERSION names a published PubDex release -- one the release record,
releases/, holds -- the tree has to ship exactly what that release shipped:
the files that enter its images and its Helm chart's package. The check
compares them, read-only, with the commit the release record names for it:
the differences Git reports between that commit and the working tree, and the
untracked files it does not ignore. Any difference in a shipped file fails
the check. A change to what a published release ships needs a new version:
VERSION, and with it the chart's version and appVersion and the image
defaults of compose.yaml, moves to the next version, which gets its own
section in CHANGELOG.md.

Shipped are both Dockerfiles and their allowlists, .dockerignore and
postgres.Dockerfile.dockerignore; every file an allowlist lets into a build
context, read from the allowlists themselves at every run; build_image.sh and
DB/lib/release_identity.sh, which write the images' labels; and everything
under charts/pubdex/. Not shipped are the release record, the documents, the
tests, CI and the suite's scripts, so the record commit, which adds a
release's entry to releases/ while VERSION still names that release, passes.

The allowlists are read as Docker reads a .dockerignore file, for the rules
they hold: comment lines (#) and blank lines are skipped, the first rule, *,
excludes everything, and each later rule !<pattern> lets in the paths that
<pattern> matches, or that lie under a directory it matches; * matches within
one path segment, ** any number of segments and ? one character. A rule of
another form stops the check, so that it never watches less than a build
lets in.

When VERSION names no published PubDex release, there is nothing to compare,
and the check passes without Git. Otherwise it needs a Git checkout that has
the release's commit: in a shallow clone that lacks it, the check fails and
names the commit.

It runs in stage 1 of the complete suite (run_checks.sh). --root names another
repository root, for tests; --version checks as if VERSION held that version.
Exit status: 0 when nothing differs or nothing is published under VERSION, 1
when a shipped file differs or the comparison cannot be made, 2 for a usage
error.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, TextIO, Tuple

from release_record import Record, RecordError, is_version

ROOT = Path(__file__).resolve().parents[1]
ALLOWLISTS = (".dockerignore", "postgres.Dockerfile.dockerignore")
BUILD_FILES = ("Dockerfile", "postgres.Dockerfile", *ALLOWLISTS, "build_image.sh", "DB/lib/release_identity.sh")
CHART = "charts/pubdex/"

#: Runs git with the arguments in the repository root: (exit status, standard output, standard error).
Git = Callable[[Path, Sequence[str]], Tuple[int, str, str]]


class GuardError(Exception):
    """The comparison cannot be made."""


def pattern_regex(pattern: str, source: str = "an allowlist") -> "re.Pattern[str]":
    """The regular expression Docker compiles from a .dockerignore pattern, for *, ** and ?."""
    if any(character in pattern for character in "[]\\"):
        raise GuardError(f"{source}: the pattern {pattern!r} uses a character class or an escape, which this "
                         "check does not read; extend DB/check_version_guard.py before using one")
    pattern = pattern.strip()
    while pattern.startswith("/"):
        pattern = pattern[1:]
    if not pattern:
        raise GuardError(f"{source}: a rule lets in an empty pattern")
    regex = ""
    position = 0
    while position < len(pattern):
        character = pattern[position]
        if character == "*" and pattern[position + 1:position + 2] == "*":
            position += 1
            if pattern[position + 1:position + 2] == "/":
                position += 1
            regex += ".*" if position + 1 == len(pattern) else "(.*/)?"
        elif character == "*":
            regex += "[^/]*"
        elif character == "?":
            regex += "[^/]"
        else:
            regex += re.escape(character)
        position += 1
    return re.compile(regex)


def allowlist_patterns(root: Path, name: str) -> List[str]:
    """The patterns of an allowlist's !<pattern> rules, after its first rule, *."""
    try:
        lines = (root / name).read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise GuardError(f"cannot read {name}: {error}") from error
    rules = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    if not rules or rules[0] != "*":
        raise GuardError(f"{name}: its first rule has to be *, which excludes everything; this check reads "
                         "allowlists only")
    patterns = []
    for rule in rules[1:]:
        if not rule.startswith("!"):
            raise GuardError(f"{name}: the rule {rule!r} is not !<pattern>; this check reads allowlists of * and "
                             "!<pattern> rules only; extend DB/check_version_guard.py before adding another rule")
        patterns.append(rule[1:].strip())
    return patterns


class Shipped:
    """Whether a path, relative to the repository root, is shipped."""

    def __init__(self, root: Path) -> None:
        self.patterns = [pattern_regex(pattern, name)
                         for name in ALLOWLISTS for pattern in allowlist_patterns(root, name)]

    def __call__(self, path: str) -> bool:
        if path in BUILD_FILES or path.startswith(CHART):
            return True
        parts = path.split("/")
        candidates = ["/".join(parts[:end]) for end in range(1, len(parts) + 1)]
        return any(regex.fullmatch(candidate) for regex in self.patterns for candidate in candidates)


def run_git(root: Path, arguments: Sequence[str]) -> Tuple[int, str, str]:
    try:
        process = subprocess.run(["git", "--no-optional-locks", *arguments], cwd=root, capture_output=True,
                                 text=True, check=False)
    except OSError as error:
        raise GuardError(f"cannot run git ({error}); the check compares the tree with a published release's "
                         "commit in a Git checkout") from error
    return process.returncode, process.stdout, process.stderr


@dataclass
class Result:
    message: str
    passed: bool
    commit: Optional[str] = None
    differing: Tuple[str, ...] = ()


def check(root: Path = ROOT, version: Optional[str] = None, git: Git = run_git) -> Result:
    """Compare the tree's shipped files with the published release VERSION (or version) names."""
    record = Record(root)
    version = version or record.checkout_version()
    published = record.versions()
    if version not in published:
        return Result(f"VERSION holds {version}, which is no published PubDex release (the release record holds "
                      f"{', '.join(published) or 'none'}); nothing to compare", True)
    commit = record.entry(version)["commit"]
    shipped = Shipped(root)

    status, top, _ = git(root, ["rev-parse", "--show-toplevel"])
    if status != 0 or Path(top.strip()).resolve() != root.resolve():
        raise GuardError(f"{root} is not the top directory of a Git checkout; {version} is published, and the "
                         f"check compares the tree with its commit {commit}")
    status, _, _ = git(root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    if status != 0:
        raise GuardError(f"{version} is published from commit {commit}, which this checkout does not have, so "
                         "what it ships cannot be compared; a shallow clone lacks it: fetch the complete history "
                         "and run the check again")
    status, changed, error = git(root, ["diff", "--name-only", "--no-renames", "--no-ext-diff", "-z", commit, "--"])
    if status != 0:
        raise GuardError(f"git diff against {commit} failed: {error.strip()}")
    status, untracked, error = git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    if status != 0:
        raise GuardError(f"git ls-files failed: {error.strip()}")
    paths = {path for path in (changed + untracked).split("\0") if path}
    differing = tuple(sorted(path for path in paths if shipped(path)))
    if differing:
        return Result(f"{version} is published; a change to what it ships needs a new version", False, commit,
                      differing)
    return Result(f"{version} is published, from commit {commit}; the tree ships what it shipped", True, commit)


def main(argv: Optional[Sequence[str]] = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    parser = argparse.ArgumentParser(prog="check_version_guard.py",
                                     description="Refuses a tree that carries a published version but ships "
                                                 "other files.")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root (default: this checkout)")
    parser.add_argument("--version", help="check as if VERSION held this version")
    arguments = parser.parse_args(argv)
    if arguments.version is not None and not is_version(arguments.version):
        parser.error(f"--version needs a release version such as 0.2.0, not {arguments.version!r}")
    try:
        result = check(arguments.root, arguments.version)
    except (GuardError, RecordError) as error:
        print(f"version guard: {error}", file=err)
        return 1
    if result.passed:
        print(f"version guard: {result.message}.", file=out)
        return 0
    print(f"version guard: {result.message}.", file=err)
    print(f"These {len(result.differing)} shipped file(s) differ from its commit {result.commit}:", file=err)
    for path in result.differing:
        print(f"  {path}", file=err)
    print("Move VERSION to the next version, with the chart's version and appVersion and the image defaults of "
          "compose.yaml, and give it a section in CHANGELOG.md.", file=err)
    return 1


if __name__ == "__main__":
    sys.exit(main())
