"""The chart as a package file, as release.sh builds it with the pinned helm.

helm package writes the chart's files into pubdex-<version>.tgz, with Chart.yaml
written afresh from its fields. With SOURCE_DATE_EPOCH set, helm 4.3 gives
every member that time instead of the file's, so the package is the same,
byte for byte, whatever times the files have: two packages of the same
chart, one from a copy whose files have other times, are compared here. A
test release's package carries its version as version and appVersion.

The package has to stand on its own once it leaves the repository: it
carries the licence, which is the repository's, no Markdown link of it
points outside it, the chart's own files are named relative to the chart,
and every other file it names -- documents and create_secrets.sh -- is one
its README says where to find. It lints and renders from the file.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Dict

import pytest

from chart import CHART, ROOT

#: A test release's version, which the package then carries.
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
TEST_VERSION = f"{VERSION}-test.checks"
#: A commit time, as release.sh passes it.
EPOCH = "1700000000"
#: Files that a chart directory may hold and .helmignore keeps out of the package.
IGNORED = re.compile(r"(^|/)(__pycache__|\.vscode|\.idea|\.git)(/|$)|\.(swp|bak|tmp|orig|pyc)$|~$|\.DS_Store$")


def _package(helm, source: Path, destination: Path, *args: str) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([str(helm.path), "package", str(source), "--destination", str(destination), *args],
                          cwd=ROOT, env={**helm.env, "SOURCE_DATE_EPOCH": EPOCH}, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    packages = list(destination.glob("*.tgz"))
    assert len(packages) == 1, packages
    return packages[0]


@pytest.fixture(scope="module")
def package(helm, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _package(helm, CHART, tmp_path_factory.mktemp("package"), "--version", TEST_VERSION,
                    "--app-version", TEST_VERSION)


@pytest.fixture(scope="module")
def members(package: Path) -> Dict[str, bytes]:
    """The package's files, by their path inside the chart."""
    with tarfile.open(package) as archive:
        found = {}
        for member in archive.getmembers():
            assert member.isfile(), f"{member.name}: files only"
            assert member.name.startswith("pubdex/"), member.name
            handle = archive.extractfile(member)
            assert handle is not None
            found[member.name[len("pubdex/"):]] = handle.read()
    return found


def _chart_files() -> Dict[str, bytes]:
    return {path.relative_to(CHART).as_posix(): path.read_bytes() for path in sorted(CHART.rglob("*"))
            if path.is_file() and not IGNORED.search(path.relative_to(CHART).as_posix())}


def test_the_package_is_named_and_versioned_for_the_test_release(package: Path, members: Dict[str, bytes]) -> None:
    assert package.name == f"pubdex-{TEST_VERSION}.tgz"
    chart = members["Chart.yaml"].decode("utf-8")
    assert re.search(rf"^version: {re.escape(TEST_VERSION)}$", chart, re.M), chart
    assert re.search(rf"^appVersion: {re.escape(TEST_VERSION)}$", chart, re.M), chart


def test_the_package_is_the_same_whatever_the_files_times(helm, package: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy" / "pubdex"
    shutil.copytree(CHART, copy, ignore=shutil.ignore_patterns("__pycache__"))
    for number, path in enumerate(sorted(copy.rglob("*"))):
        os.utime(path, (1_000_000 + number, 1_000_000 + number))
    again = _package(helm, copy, tmp_path / "again", "--version", TEST_VERSION, "--app-version", TEST_VERSION)
    assert again.read_bytes() == package.read_bytes()


def test_the_package_holds_every_file_of_the_chart_and_the_licence(members: Dict[str, bytes]) -> None:
    files = _chart_files()
    assert set(members) == set(files)
    assert {name: content for name, content in members.items() if name != "Chart.yaml"} == \
        {name: content for name, content in files.items() if name != "Chart.yaml"}
    assert members["LICENSE"] == (ROOT / "LICENSE").read_bytes(), "the repository's licence, byte for byte"


def _texts(members: Dict[str, bytes]) -> Dict[str, str]:
    return {name: content.decode("utf-8") for name, content in members.items()}


def _flat(text: str) -> str:
    """The words of a text, comment marks left out, one space apart."""
    return " ".join(" ".join(re.sub(r"^\s*#", "", line) for line in text.splitlines()).split())


def test_no_markdown_link_leaves_the_package(members: Dict[str, bytes]) -> None:
    names = set(members)
    for name, text in _texts(members).items():
        if not name.endswith(".md"):
            continue
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if re.match(r"[a-z]+://|#|mailto:", target):
                continue
            path = PurePosixPath(name).parent / target.split("#", 1)[0]
            parts = []
            for part in path.parts:
                if part == "..":
                    assert parts, f"{name}: the link {target} leaves the package"
                    parts.pop()
                elif part != ".":
                    parts.append(part)
            resolved = "/".join(parts)
            assert resolved in names or any(other.startswith(resolved.rstrip("/") + "/") for other in names), \
                f"{name}: the link {target} names nothing in the package"


def test_the_charts_own_files_are_named_relative_to_the_chart(members: Dict[str, bytes]) -> None:
    # Only the examples' commands, which run from the root of the PubDex
    # source, name the chart by its place there.
    for name, text in _texts(members).items():
        for line in text.splitlines():
            if "charts/pubdex" not in line:
                continue
            assert name.startswith("examples/") and re.match(r"#\s+helm (install|upgrade) ", line), (name, line)
        if name.startswith("examples/") and "charts/pubdex" in text:
            assert "from the root of the PubDex source of this chart's version" in _flat(text), name


def test_every_file_the_package_names_and_lacks_is_one_its_readme_locates(members: Dict[str, bytes]) -> None:
    texts = _texts(members)
    readme = _flat(texts["README.md"])
    located = re.search(r"the other files the chart names but does not hold — (.*?) — are in the PubDex source "
                        r"of the chart's version", readme)
    assert located, "README.md says where the files the chart names come from"
    outside = set(re.findall(r"`([^`]+)`", located.group(1))) | {"KUBERNETES.md"}
    named = set()
    for name, text in texts.items():
        named |= set(re.findall(r"(?<![\w./-])((?:[\w-]+/)*[\w-]+\.md)\b", text))
    assert named - set(members) <= outside, sorted(named - set(members) - outside)
    # Scripts: the chart's own, those the images carry at a stated path or as
    # part of their schema bootstrap, and create_secrets.sh, which is never
    # invoked as if it lay next to the chart.
    scripts = set()
    for name, text in texts.items():
        scripts |= set(re.findall(r"(?<![\w./-])((?:[\w./-]*/)?[\w-]+\.sh)\b", text.replace("helm.sh/", "")))
    image_paths = {script for script in scripts if script.startswith("/opt/pubdex/")}
    assert scripts - image_paths - set(members) <= {"external-setup.sh", "020_role_passwords.sh",
                                                    "create_secrets.sh"}, sorted(scripts)
    assert "create_secrets.sh" in outside
    for name, text in texts.items():
        assert "./create_secrets.sh" not in text, name
        if "create_secrets.sh" in text and name != "README.md":
            flat = _flat(text)
            assert "PubDex source of this chart's version" in flat, name
            assert "application image" in flat, name
    assert "/opt/pubdex/create_secrets.sh" in readme


def test_the_package_lints_and_renders_from_the_file(helm, package: Path) -> None:
    lint = helm.run("lint", "--strict", package)
    assert lint.returncode == 0, lint.stdout + lint.stderr
    rendered = helm.run("template", "pubdex", package, "--namespace", "package-checks",
                        "--set", "secrets.name=pubdex-credentials")
    assert rendered.returncode == 0, rendered.stderr
    assert re.search(r"^kind: Deployment$", rendered.stdout, re.M)
    assert f'app.kubernetes.io/version: "{TEST_VERSION}"' in rendered.stdout
    shown = helm.run("show", "chart", package)
    assert shown.returncode == 0 and f"version: {TEST_VERSION}" in shown.stdout


def test_the_licence_of_the_chart_is_the_repositorys() -> None:
    # Helm writes Chart.yaml afresh and drops its comments, so the licence
    # annotation needs the file beside it.
    assert (CHART / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    assert "licenses: MIT" in (CHART / "Chart.yaml").read_text(encoding="utf-8")
