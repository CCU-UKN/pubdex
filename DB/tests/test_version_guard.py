"""DB/check_version_guard.py, offline.

What the guard counts as shipped is read from the checkout's two allowlists
at every run: every file the Dockerfiles copy from the build context, every
file each allowlist rule matches (found here independently, with pathlib's
glob), both Dockerfiles, both allowlists, build_image.sh,
DB/lib/release_identity.sh and charts/pubdex/ are shipped; the release
record, documents, tests and the suite's scripts are not. The patterns are
read as Docker reads them, and an allowlist rule the guard cannot read stops
it.

The comparison runs on fixture trees with a fake Git, which records each
call and answers it: a VERSION that names no published PubDex release passes
without Git; a published one passes when only unshipped files differ from its
commit -- as on the record commit -- and fails, listing them, when a shipped
file differs or a shipped file is untracked; a missing commit, a directory
that is not the top of a checkout and a failing git diff stop it with a
message. The command line is run with a stub git on PATH, and without git.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import pytest

import check_version_guard
from check_version_guard import ALLOWLISTS, BUILD_FILES, GuardError, Shipped, check, pattern_regex

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "DB" / "check_version_guard.py"
COMMIT_010 = "1" * 40
COMMIT_020 = "3" * 40


# --- What is shipped ------------------------------------------------------------------------------------------


def _copy_sources(dockerfile: Path) -> List[str]:
    """The build-context sources of every COPY without --from."""
    text = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
    sources = []
    for line in text.splitlines():
        words = line.split()
        if words[:1] != ["COPY"] or any(word.startswith("--from=") for word in words):
            continue
        sources += [word for word in words[1:-1] if not word.startswith("--")]
    return sources


def _files(path: Path) -> List[str]:
    if path.is_file():
        return [path.relative_to(ROOT).as_posix()]
    return sorted(item.relative_to(ROOT).as_posix() for item in path.rglob("*")
                  if item.is_file() and "__pycache__" not in item.parts)


def test_every_file_the_dockerfiles_copy_from_the_build_context_is_shipped() -> None:
    shipped = Shipped(ROOT)
    sources = _copy_sources(ROOT / "Dockerfile") + _copy_sources(ROOT / "postgres.Dockerfile")
    assert {"DB/people_pubs", "DB/schema", "DB/init/000_init.sql", "compose.yaml",
            "third_party/docker-library-postgres/LICENSE"} <= set(sources)
    files = [name for source in sources for name in _files(ROOT / source)]
    assert len(files) > 50
    assert [name for name in files if not shipped(name)] == []


def test_every_file_an_allowlist_rule_matches_is_shipped() -> None:
    shipped = Shipped(ROOT)
    for allowlist in ALLOWLISTS:
        for pattern in check_version_guard.allowlist_patterns(ROOT, allowlist):
            matched = [path.relative_to(ROOT).as_posix() for path in ROOT.glob(pattern) if path.is_file()]
            assert matched, f"{allowlist}: !{pattern} matches no file of the checkout"
            assert [name for name in matched if not shipped(name)] == [], pattern


@pytest.mark.parametrize("path", [*BUILD_FILES, "charts/pubdex/Chart.yaml", "charts/pubdex/templates/new.yaml",
                                  "charts/pubdex/.helmignore", "DB/people_pubs/schema.py",
                                  "DB/people_pubs/api/app.py", "DB/people_pubs/new/module.py",
                                  "DB/schema/0001_schema_change_record.sql", "DB/schema/0002_new.sql",
                                  "DB/init/010_ingestion_runtime_schema.sql", "DB/requirements.txt",
                                  "DB/fetch_verified.py", "DB/tests/fixtures/golden/demo_roster_minimal.csv",
                                  "compose.yaml", "create_secrets.sh", "set_passwords.sh", "LICENSE",
                                  "LICENSE-CC-BY-4.0", "third_party/docker-library-postgres/LICENSE"])
def test_shipped(path: str) -> None:
    assert Shipped(ROOT)(path)


@pytest.mark.parametrize("path", ["CHANGELOG.md", "README.md", "KUBERNETES.md", "releases/README.md",
                                  "releases/FLOOR", "releases/0.1.0/release.json",
                                  "releases/0.2.0/SHA256SUMS", "VERSION", "release.sh", "run_ci_suite.sh",
                                  "run_release_rehearsal.sh", "DB/check_version_guard.py",
                                  "DB/check_release_notes.py", "DB/release_record.py",
                                  "DB/tests/test_version_guard.py", "DB/tests/fixtures/golden/other.json",
                                  "DB/people_pubs/__pycache__/schema.cpython-311.pyc", "DB/people_pubs/notes.txt",
                                  "DB/schema/README.md", "DB/init/README.md", "DB/lib/cluster_tools.sh",
                                  "charts/tests/test_package.py", "charts/pubdexx/Chart.yaml",
                                  ".github/workflows/db-tests.yml", "third_party/docker-library-postgres/NOTES"])
def test_not_shipped(path: str) -> None:
    assert not Shipped(ROOT)(path)


@pytest.mark.parametrize("pattern, path, matches", [
    ("DB/people_pubs/**/*.py", "DB/people_pubs/__init__.py", True),
    ("DB/people_pubs/**/*.py", "DB/people_pubs/api/app.py", True),
    ("DB/people_pubs/**/*.py", "DB/people_pubs/a/b/c.py", True),
    ("DB/people_pubs/**/*.py", "DB/people_pubs/x.pyc", False),
    ("DB/people_pubs/**/*.py", "DB/people_pubsX/a.py", False),
    ("DB/schema/*.sql", "DB/schema/0001_a.sql", True),
    ("DB/schema/*.sql", "DB/schema/sub/0001_a.sql", False),
    ("third_party/**", "third_party/a/b", True),
    ("**/*.md", "README.md", True),
    ("**/*.md", "docs/a/b.md", True),
    ("DB/?.txt", "DB/a.txt", True),
    ("DB/?.txt", "DB/ab.txt", False),
    ("/LICENSE", "LICENSE", True),
    ("a.b", "aXb", False),
])
def test_patterns_match_as_docker_reads_them(pattern: str, path: str, matches: bool) -> None:
    assert (pattern_regex(pattern).fullmatch(path) is not None) is matches


def test_a_directory_an_allowlist_lets_in_ships_everything_under_it(tmp_path: Path) -> None:
    (tmp_path / ".dockerignore").write_text("# comment\n\n*\n!DB/init\n", encoding="utf-8")
    (tmp_path / "postgres.Dockerfile.dockerignore").write_text("*\n", encoding="utf-8")
    shipped = Shipped(tmp_path)
    assert shipped("DB/init/new.sql") and shipped("DB/init") and not shipped("DB/initial.sql")


@pytest.mark.parametrize("text, message", [
    ("!compose.yaml\n", "its first rule has to be *, which excludes everything"),
    ("*\n!compose.yaml\nDB/people_pubs/secret.py\n", "the rule 'DB/people_pubs/secret.py' is not !<pattern>"),
    ("*\n![ab].txt\n", "uses a character class or an escape"),
    ("*\n!a\\*b\n", "uses a character class or an escape"),
    ("*\n!/\n", "a rule lets in an empty pattern"),
])
def test_an_allowlist_the_guard_cannot_read_stops_it(tmp_path: Path, text: str, message: str) -> None:
    (tmp_path / ".dockerignore").write_text(text, encoding="utf-8")
    (tmp_path / "postgres.Dockerfile.dockerignore").write_text("*\n", encoding="utf-8")
    with pytest.raises(GuardError, match=re.escape(message)):
        Shipped(tmp_path)


# --- The comparison -------------------------------------------------------------------------------------------


def _entry(version: str, commit: str) -> dict:
    return {
        "version": version, "commit": commit, "tag": f"v{version}", "published": "2026-11-03",
        "platform": "linux/amd64",
        "images": {"application": f"registry.example.org/pubdex:{version}@sha256:{'a' * 64}",
                   "postgres": f"registry.example.org/pubdex-postgres:{version}@sha256:{'b' * 64}"},
        "schema_changes": [], "transform_version": "1.5.3", "floor": None,
        "chart": {"version": version, "commit": commit, "package": None},
    }


def tree(root: Path, version: str = "0.2.0") -> Path:
    for name, commit in (("0.1.0", COMMIT_010), ("0.2.0", COMMIT_020)):
        (root / "releases" / name).mkdir(parents=True)
        (root / "releases" / name / "release.json").write_text(json.dumps(_entry(name, commit)), encoding="utf-8")
    (root / "releases" / "FLOOR").write_text("0.1.0\n", encoding="utf-8")
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    for allowlist in ALLOWLISTS:
        (root / allowlist).write_text((ROOT / allowlist).read_text(encoding="utf-8"), encoding="utf-8")
    return root


class FakeGit:
    """Answers the guard's git calls and records them."""

    def __init__(self, changed: Sequence[str] = (), untracked: Sequence[str] = (), has_commit: bool = True,
                 toplevel: Optional[Path] = None, diff_fails: bool = False, checkout: bool = True) -> None:
        self.changed, self.untracked, self.has_commit = changed, untracked, has_commit
        self.toplevel, self.diff_fails, self.checkout = toplevel, diff_fails, checkout
        self.calls: List[List[str]] = []

    def __call__(self, root: Path, arguments: Sequence[str]) -> Tuple[int, str, str]:
        arguments = list(arguments)
        self.calls.append(arguments)
        if arguments[:2] == ["rev-parse", "--show-toplevel"]:
            if not self.checkout:
                return 128, "", "fatal: not a git repository"
            return 0, f"{self.toplevel or root}\n", ""
        if arguments[0] == "cat-file":
            return (0 if self.has_commit else 1), "", ""
        if arguments[0] == "diff":
            if self.diff_fails:
                return 128, "", "fatal: bad object"
            return 0, "".join(f"{path}\0" for path in self.changed), ""
        if arguments[0] == "ls-files":
            return 0, "".join(f"{path}\0" for path in self.untracked), ""
        raise AssertionError(f"unexpected git call: {arguments}")


RECORD_COMMIT = ["releases/0.2.0/release.json", "releases/0.2.0/SHA256SUMS", "releases/0.2.0/DIGESTS",
                 "CHANGELOG.md", "DEPLOYMENT.md", "KUBERNETES.md", "README.md", "DB/TESTING_STRATEGY.md"]


def test_a_version_that_is_not_published_passes_without_git(tmp_path: Path) -> None:
    git = FakeGit(changed=["Dockerfile"])
    result = check(tree(tmp_path, "0.3.0"), git=git)
    assert result.passed and git.calls == []
    assert result.message == ("VERSION holds 0.3.0, which is no published PubDex release (the release record holds "
                              "0.1.0, 0.2.0); nothing to compare")


def test_the_record_commit_of_a_published_release_passes(tmp_path: Path) -> None:
    git = FakeGit(changed=RECORD_COMMIT, untracked=["releases/0.2.0/notes.txt"])
    result = check(tree(tmp_path), git=git)
    assert result.passed and result.differing == ()
    assert result.message == f"0.2.0 is published, from commit {COMMIT_020}; the tree ships what it shipped"
    assert git.calls == [["rev-parse", "--show-toplevel"], ["cat-file", "-e", f"{COMMIT_020}^{{commit}}"],
                         ["diff", "--name-only", "--no-renames", "--no-ext-diff", "-z", COMMIT_020, "--"],
                         ["ls-files", "--others", "--exclude-standard", "-z"]]


@pytest.mark.parametrize("path", [*BUILD_FILES, "charts/pubdex/values.yaml", "DB/people_pubs/schema.py",
                                  "DB/schema/0002_new.sql", "compose.yaml"])
def test_a_changed_shipped_file_fails(tmp_path: Path, path: str) -> None:
    result = check(tree(tmp_path), git=FakeGit(changed=RECORD_COMMIT + [path]))
    assert not result.passed
    assert result.message == "0.2.0 is published; a change to what it ships needs a new version"
    assert result.differing == (path,) and result.commit == COMMIT_020


def test_an_untracked_shipped_file_fails(tmp_path: Path) -> None:
    result = check(tree(tmp_path), git=FakeGit(untracked=["DB/schema/0002_new.sql", "notes.md"]))
    assert not result.passed and result.differing == ("DB/schema/0002_new.sql",)


def test_another_version_compares_with_its_own_commit(tmp_path: Path) -> None:
    git = FakeGit(changed=["Dockerfile"])
    result = check(tree(tmp_path), "0.1.0", git=git)
    assert not result.passed and result.commit == COMMIT_010
    assert ["diff", "--name-only", "--no-renames", "--no-ext-diff", "-z", COMMIT_010, "--"] in git.calls


def test_a_missing_commit_stops_the_guard_and_names_it(tmp_path: Path) -> None:
    with pytest.raises(GuardError, match=f"0.2.0 is published from commit {COMMIT_020}, which this checkout does "
                                         "not have, so what it ships cannot be compared; a shallow clone lacks it"):
        check(tree(tmp_path), git=FakeGit(has_commit=False))


def test_a_directory_that_is_no_checkout_stops_the_guard(tmp_path: Path) -> None:
    with pytest.raises(GuardError, match="is not the top directory of a Git checkout"):
        check(tree(tmp_path), git=FakeGit(checkout=False))
    with pytest.raises(GuardError, match="is not the top directory of a Git checkout"):
        check(tree(tmp_path / "inner"), git=FakeGit(toplevel=tmp_path))


def test_a_failing_diff_stops_the_guard(tmp_path: Path) -> None:
    with pytest.raises(GuardError, match=f"git diff against {COMMIT_020} failed: fatal: bad object"):
        check(tree(tmp_path), git=FakeGit(diff_fails=True))


def test_the_checkout_passes() -> None:
    assert check(ROOT).passed


# --- The command line -----------------------------------------------------------------------------------------

STUB_GIT = r'''#!/usr/bin/env bash
case "$*" in
  "--no-optional-locks rev-parse --show-toplevel") pwd -P ;;
  "--no-optional-locks cat-file -e "*) [ -e "$STUB_DIR/has-commit" ] ;;
  "--no-optional-locks diff "*) tr '\n' '\0' < "$STUB_DIR/changed" ;;
  "--no-optional-locks ls-files "*) tr '\n' '\0' < "$STUB_DIR/untracked" ;;
  *) echo "unexpected git call: $*" >&2; exit 99 ;;
esac
'''


def _run(root: Path, stub: Optional[Path], *arguments: str) -> subprocess.CompletedProcess:
    path = f"{stub}{os.pathsep}{os.environ['PATH']}" if stub else str(root / "no-tools")
    env = dict(os.environ, PATH=path, STUB_DIR=str(stub or ""))
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *arguments], env=env,
                          capture_output=True, text=True, timeout=60)


def _stub(tmp_path: Path, changed: Sequence[str] = (), has_commit: bool = True) -> Path:
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "git").write_text(STUB_GIT, encoding="utf-8")
    (stub / "git").chmod(0o755)
    (stub / "changed").write_text("".join(f"{path}\n" for path in changed), encoding="utf-8")
    (stub / "untracked").write_text("", encoding="utf-8")
    if has_commit:
        (stub / "has-commit").write_text("", encoding="utf-8")
    return stub


def test_the_command_line_lists_the_shipped_files_that_differ(tmp_path: Path) -> None:
    root = tree(tmp_path / "root")
    proc = _run(root, _stub(tmp_path, RECORD_COMMIT + ["DB/people_pubs/schema.py", "Dockerfile"]))
    assert proc.returncode == 1 and proc.stdout == ""
    assert proc.stderr.splitlines() == [
        "version guard: 0.2.0 is published; a change to what it ships needs a new version.",
        f"These 2 shipped file(s) differ from its commit {COMMIT_020}:",
        "  DB/people_pubs/schema.py",
        "  Dockerfile",
        "Move VERSION to the next version, with the chart's version and appVersion and the image defaults of "
        "compose.yaml, and give it a section in CHANGELOG.md.",
    ]


def test_the_command_line_passes_on_the_record_commit(tmp_path: Path) -> None:
    root = tree(tmp_path / "root")
    proc = _run(root, _stub(tmp_path, RECORD_COMMIT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (f"version guard: 0.2.0 is published, from commit {COMMIT_020}; the tree ships what "
                           "it shipped.\n")


def test_the_command_line_fails_without_the_commit_or_without_git(tmp_path: Path) -> None:
    root = tree(tmp_path / "root")
    proc = _run(root, _stub(tmp_path, has_commit=False))
    assert proc.returncode == 1
    assert proc.stderr.startswith(f"version guard: 0.2.0 is published from commit {COMMIT_020}, which this checkout")
    proc = _run(root, None)
    assert proc.returncode == 1 and proc.stderr.startswith("version guard: cannot run git")


def test_the_command_line_refuses_a_malformed_version(tmp_path: Path) -> None:
    proc = _run(tree(tmp_path / "root"), None, "--version", "0.2")
    assert proc.returncode == 2 and "--version needs a release version such as 0.2.0" in proc.stderr


def test_main_reports_a_record_error(tmp_path: Path) -> None:
    root = tree(tmp_path)
    (root / "releases" / "0.2.0" / "release.json").write_text("{}", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    assert check_version_guard.main(["--root", str(root)], out, err) == 1
    assert err.getvalue().startswith("version guard: releases/0.2.0/release.json: has the fields")
