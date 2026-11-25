"""build_image.sh, offline: the documented build of the deployment's two images.

The real script runs from a scratch directory standing in for the repository
root, against stub docker and git executables that record their calls; the
stub git answers only the three read-only questions the script asks. That
proves, without Docker, what each build receives -- the PostgreSQL image's
postgres.Dockerfile, then the application image's Dockerfile, both with the
checkout as context, the labels' values as build arguments, no attestation
and no Git metadata of buildx's own, BuildKit and never the legacy builder --
that the images are reported through their tags, as every image store
accepts them, that the revision label says "-dirty" for a working tree with
changes, or with an ignored Python module of the package or schema change
that the build context would take in, and "unknown" outside a Git checkout,
that the release version cannot become the tag of either image of such a
build, that a test release's version (--version <VERSION>-test.<name>) labels
both images and keeps the release version off their tags, that only a copy
that is no Git checkout states its revision (--revision <commit>-dirty), and
that a failed build fails the script.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "build_image.sh"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
IMAGE_ID = "sha256:" + "ab" * 32

STUB_DOCKER = r'''#!/usr/bin/env bash
{ printf 'docker'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "$STUB_STATE/events"
case "$1" in
  build)
    printf 'BUILDX_GIT_INFO=%s BUILDX_GIT_LABELS=%s DOCKER_BUILDKIT=%s\n' "${BUILDX_GIT_INFO-unset}" \
      "${BUILDX_GIT_LABELS-unset}" "${DOCKER_BUILDKIT-unset}" > "$STUB_STATE/build-environment"
    case ",$STUB_MODE," in *",build-fails,"*) echo "stub: build failed" >&2; exit 1 ;; esac
    exit 0 ;;
  image)
    # docker image inspect --format <template> <reference>
    case "$4" in
      "{{.Id}}") echo "$STUB_IMAGE_ID" ;;
      *postgresql-version*) echo "16.15" ;;
      *) echo "linux/amd64" ;;
    esac
    printf '%s\n' "${!#}" >> "$STUB_STATE/inspected"
    exit 0 ;;
  buildx)
    # Ubuntu's docker.io without the docker-buildx package it only suggests.
    case ",$STUB_MODE," in *",no-buildx,"*) echo "docker: 'buildx' is not a docker command." >&2; exit 1 ;; esac
    [ "$2" = version ] && { echo "github.com/docker/buildx v0.30.1"; exit 0; } ;;
esac
echo "stub docker: unexpected $*" >&2
exit 2
'''

STUB_GIT = r'''#!/usr/bin/env bash
{ printf 'git'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "$STUB_STATE/events"
case " $* " in
  *" rev-parse --verify --quiet HEAD "*)
    case ",$STUB_MODE," in *",no-checkout,"*) exit 128 ;; esac
    echo "$STUB_COMMIT"; exit 0 ;;
  *" status --porcelain "*)
    case ",$STUB_MODE," in *",status-fails,"*) exit 128 ;; esac
    printf '%s' "${STUB_STATUS:-}"; exit 0 ;;
  *" ls-files --others --ignored --exclude-standard -- DB/people_pubs/*.py DB/schema/*.sql "*)
    case ",$STUB_MODE," in *",ignored-fails,"*) exit 128 ;; esac
    printf '%s' "${STUB_IGNORED:-}"; exit 0 ;;
esac
echo "stub git: unexpected $*" >&2
exit 2
'''


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class Checkout:
    """A scratch repository root: the script and the identity rules it
    sources, VERSION, both Dockerfiles and the module holding the transform
    version, plus the stubs."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "checkout"
        (self.root / "DB" / "people_pubs").mkdir(parents=True)
        (self.root / "DB" / "lib").mkdir()
        self.script = self.root / SCRIPT.name
        shutil.copy2(SCRIPT, self.script)
        shutil.copy2(ROOT / "DB" / "lib" / "release_identity.sh", self.root / "DB" / "lib")
        (self.root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        (self.root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (self.root / "postgres.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (self.root / "DB" / "people_pubs" / "__init__.py").write_text('__version__ = "1.5.2"\n', encoding="utf-8")
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.bin.mkdir()
        self.state.mkdir()
        _executable(self.bin / "docker", STUB_DOCKER)
        _executable(self.bin / "git", STUB_GIT)

    def run(self, *args: str, mode: str = "", status: str = "", **extra: str) -> subprocess.CompletedProcess:
        env: Dict[str, str] = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "STUB_STATE": str(self.state),
            "STUB_MODE": mode,
            "STUB_STATUS": status,
            "STUB_COMMIT": COMMIT,
            "STUB_IMAGE_ID": IMAGE_ID,
        }
        env.update(extra)
        return subprocess.run([str(self.script), *args], cwd=self.root.parent, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def calls(self, program: str) -> List[List[str]]:
        events = self.state / "events"
        lines = events.read_text(encoding="utf-8").splitlines() if events.exists() else []
        return [line.split("\x1f")[1:] for line in lines if line.split("\x1f")[0] == program]

    def builds(self) -> List[List[str]]:
        return [call for call in self.calls("docker") if call[:1] == ["build"]]

    def build(self, dockerfile: str = "Dockerfile") -> List[str]:
        """The one build of dockerfile; the PostgreSQL image's comes first."""
        builds = self.builds()
        assert [_values(call, "--file") for call in builds] == [
            [str(self.root / "postgres.Dockerfile")], [str(self.root / "Dockerfile")]], builds
        return builds[0 if dockerfile == "postgres.Dockerfile" else 1]


def _values(call: List[str], flag: str) -> List[str]:
    return [call[i + 1] for i, arg in enumerate(call[:-1]) if arg == flag]


def _build_args(call: List[str]) -> Dict[str, str]:
    return dict(value.split("=", 1) for value in _values(call, "--build-arg"))


@pytest.fixture
def checkout(tmp_path: Path) -> Checkout:
    return Checkout(tmp_path)


def test_it_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


def test_a_clean_checkout_builds_with_its_commit_as_revision(checkout: Checkout) -> None:
    proc = checkout.run()
    assert proc.returncode == 0, proc.stderr
    call, postgres = checkout.build(), checkout.build("postgres.Dockerfile")
    assert call[-1] == postgres[-1] == str(checkout.root), "the checkout is the build context"
    assert _values(call, "--tag") == ["pubdex:local"]
    assert _values(postgres, "--tag") == ["pubdex-postgres:local"]
    assert "--provenance=false" in call and "--provenance=false" in postgres
    args = _build_args(call)
    assert args["PEOPLE_PUBS_VERSION"] == "0.1.0"
    assert args["PEOPLE_PUBS_REVISION"] == COMMIT
    assert args["PEOPLE_PUBS_TRANSFORM_VERSION"] == "1.5.2"
    assert args["PEOPLE_PUBS_SOURCE"] == ""
    assert args["PEOPLE_PUBS_CREATED"].endswith("Z") and len(args["PEOPLE_PUBS_CREATED"]) == 20
    assert set(args) == {"PEOPLE_PUBS_VERSION", "PEOPLE_PUBS_REVISION", "PEOPLE_PUBS_CREATED",
                         "PEOPLE_PUBS_TRANSFORM_VERSION", "PEOPLE_PUBS_SOURCE"}, "metadata only, no secret"
    # The two images of one build carry the same identity.
    assert _build_args(postgres) == {name: value for name, value in args.items()
                                     if name != "PEOPLE_PUBS_TRANSFORM_VERSION"}
    assert (checkout.state / "build-environment").read_text() == \
        "BUILDX_GIT_INFO=0 BUILDX_GIT_LABELS=0 DOCKER_BUILDKIT=1\n"
    git_calls = checkout.calls("git")
    assert all(call[:2] == ["-C", str(checkout.root)] for call in git_calls)
    # Questions only, and git status without its optional index refresh, so
    # nothing under .git/ is written.
    assert [call[2:4] for call in git_calls] == [["rev-parse", "--verify"], ["--no-optional-locks", "status"],
                                                 ["ls-files", "--others"]], git_calls
    assert IMAGE_ID in proc.stdout and f"revision           {COMMIT}" in proc.stdout
    # The id and platform are read through the tag, which every image store
    # accepts, not through an --iidfile id, which the containerd store does not.
    assert "--iidfile" not in call
    assert (checkout.state / "inspected").read_text().split() == [
        "pubdex:local", "pubdex:local", "pubdex-postgres:local", "pubdex-postgres:local", "pubdex-postgres:local"]
    assert "linux/amd64" in proc.stdout and "PostgreSQL         16.15" in proc.stdout
    assert "not a release build" not in proc.stdout


def test_the_legacy_builder_is_never_used(checkout: Checkout) -> None:
    # Without the buildx plugin Docker falls back to its deprecated legacy
    # builder, which knows no --provenance; the script says what to install
    # instead, before it reads the checkout or builds anything. A caller's
    # DOCKER_BUILDKIT=0 does not select that builder either.
    proc = checkout.run(mode="no-buildx")
    assert proc.returncode == 1
    assert "the Docker buildx plugin was not found ('docker buildx version' fails)" in proc.stderr
    assert "docker-buildx on Debian or Ubuntu, docker-buildx-plugin from Docker's own packages" in proc.stderr
    assert [call[:1] for call in checkout.calls("docker")] == [["buildx"]] and checkout.calls("git") == []
    proc = checkout.run(DOCKER_BUILDKIT="0")
    assert proc.returncode == 0, proc.stderr
    assert "DOCKER_BUILDKIT=1" in (checkout.state / "build-environment").read_text()


def test_a_clean_checkout_may_carry_the_release_tag_and_its_source(checkout: Checkout) -> None:
    proc = checkout.run("--tag", "registry.example.org/team/pubdex:0.1.0", "--tag", "pubdex:candidate",
                        "--postgres-tag", "registry.example.org/team/pubdex-postgres:0.1.0",
                        "--source", "https://forge.example.org/team/pubdex", "--no-cache")
    assert proc.returncode == 0, proc.stderr
    call, postgres = checkout.build(), checkout.build("postgres.Dockerfile")
    assert _values(call, "--tag") == ["registry.example.org/team/pubdex:0.1.0", "pubdex:candidate"]
    assert _values(postgres, "--tag") == ["registry.example.org/team/pubdex-postgres:0.1.0"]
    for build in (call, postgres):
        assert _build_args(build)["PEOPLE_PUBS_SOURCE"] == "https://forge.example.org/team/pubdex"
        assert "--no-cache" in build


@pytest.mark.parametrize("mode, status, ignored", [
    ("", " M DB/people_pubs/config.py\n", ""),
    ("", "?? notes.txt\n", ""),
    ("status-fails", "", ""),
    # Git status shows no ignored file, but the build context takes these in.
    ("", "", "DB/people_pubs/__pycache__/local_settings.py\n"),
    ("", "", "DB/schema/0002_local_trial.sql\n"),
    ("ignored-fails", "", ""),
], ids=["modified", "untracked", "status-unreadable", "ignored-module", "ignored-schema-change",
        "ignored-unreadable"])
def test_changes_make_the_revision_dirty_and_refuse_the_release_tag(checkout: Checkout, mode: str,
                                                                    status: str, ignored: str) -> None:
    local = checkout.run(mode=mode, status=status, STUB_IGNORED=ignored)
    assert local.returncode == 0, local.stderr
    assert _build_args(checkout.build())["PEOPLE_PUBS_REVISION"] == f"{COMMIT}-dirty"
    assert _build_args(checkout.build("postgres.Dockerfile"))["PEOPLE_PUBS_REVISION"] == f"{COMMIT}-dirty"
    assert "these are local builds, not release builds" in local.stdout

    (checkout.state / "events").unlink()
    for option in ("--tag", "--postgres-tag"):
        for tag in ("pubdex:0.1.0", "registry.example.org:5000/team/pubdex:0.1.0", "pubdex:0.1.0@sha256:" + "0" * 64):
            refused = checkout.run(option, tag, mode=mode, status=status, STUB_IGNORED=ignored)
            assert refused.returncode == 1, (option, tag)
            assert "refusing to tag" in refused.stderr and f"{COMMIT}-dirty" in refused.stderr
            assert checkout.builds() == [], "nothing was built"


def test_outside_a_git_checkout_the_revision_is_unknown(checkout: Checkout) -> None:
    proc = checkout.run(mode="no-checkout")
    assert proc.returncode == 0, proc.stderr
    assert _build_args(checkout.build())["PEOPLE_PUBS_REVISION"] == "unknown"
    refused = checkout.run("--tag", "pubdex:0.1.0", mode="no-checkout")
    assert refused.returncode == 1 and "revision unknown" in refused.stderr


def test_only_the_release_version_as_tag_needs_a_clean_checkout(checkout: Checkout) -> None:
    # A registry port is not a tag, and other tags are a local build's own.
    proc = checkout.run("--tag", "registry.example.org:5000/team/pubdex", "--tag", "pubdex:0.1.0-local",
                        status="?? notes.txt\n")
    assert proc.returncode == 0, proc.stderr
    assert _values(checkout.build(), "--tag") == ["registry.example.org:5000/team/pubdex", "pubdex:0.1.0-local"]


@pytest.mark.parametrize("version", ["0.1", "v0.1.0", "0.1.0+build", "01.2.3", ""])
def test_an_unusable_version_file_stops_the_build(checkout: Checkout, version: str) -> None:
    (checkout.root / "VERSION").write_text(version + "\n", encoding="utf-8")
    proc = checkout.run()
    assert proc.returncode == 1 and "VERSION must hold a version" in proc.stderr
    assert checkout.calls("docker") == []


def test_a_failed_build_fails_the_script(checkout: Checkout) -> None:
    proc = checkout.run(mode="build-fails")
    assert proc.returncode == 1 and "the PostgreSQL image build failed" in proc.stderr
    assert len(checkout.builds()) == 1, "nothing is built after a failed build"


@pytest.mark.parametrize("args, status", [(["--help"], 0), (["--tag"], 2), (["--tag", ""], 2),
                                          (["--postgres-tag"], 2), (["--postgres-tag", ""], 2),
                                          (["--source"], 2), (["--push"], 2), (["--version"], 2),
                                          (["--version", ""], 2), (["--revision"], 2), (["--revision", ""], 2)])
def test_arguments(checkout: Checkout, args, status: int) -> None:
    proc = checkout.run(*args)
    assert proc.returncode == status
    assert "usage: build_image.sh" in (proc.stdout if status == 0 else proc.stderr)
    assert checkout.calls("docker") == []


def test_a_test_release_labels_its_version_and_never_carries_the_release_tag(checkout: Checkout) -> None:
    proc = checkout.run("--version", "0.1.0-test.a", "--tag", "pubdex-release:0.1.0-test.a",
                        "--postgres-tag", "pubdex-postgres-release:0.1.0-test.a", status=" M README.md\n")
    assert proc.returncode == 0, proc.stderr
    for dockerfile in ("Dockerfile", "postgres.Dockerfile"):
        args = _build_args(checkout.build(dockerfile))
        assert args["PEOPLE_PUBS_VERSION"] == "0.1.0-test.a"
        assert args["PEOPLE_PUBS_REVISION"] == f"{COMMIT}-dirty"
    assert "release version    0.1.0-test.a" in proc.stdout
    assert "A test release's version: these are local builds, never published." in proc.stdout
    # Not even a checkout without changes gives a test release's image the
    # release version as tag.
    (checkout.state / "events").unlink()
    for option in ("--tag", "--postgres-tag"):
        refused = checkout.run("--version", "0.1.0-test.a", option, "registry.example.org/team/pubdex:0.1.0")
        assert refused.returncode == 1
        assert "never carry the release version 0.1.0 as tag" in refused.stderr
    assert checkout.builds() == []


@pytest.mark.parametrize("version", ["0.1.0", "0.1.0-rc.1", "0.1.0-test.A", "0.1.0-test.", "0.1.0-test.a.b",
                                     "0.1.0-test.a-b", "v0.1.0-test.a", "0.1.0-testing"])
def test_version_takes_only_a_test_releases_version(checkout: Checkout, version: str) -> None:
    proc = checkout.run("--version", version)
    assert proc.returncode == 2 and "--version takes only a test release's version" in proc.stderr
    assert checkout.calls("docker") == []


def test_a_test_releases_version_belongs_to_the_version_file(checkout: Checkout) -> None:
    proc = checkout.run("--version", "0.2.0-test.a")
    assert proc.returncode == 1 and "does not name a test release of VERSION 0.1.0" in proc.stderr
    (checkout.root / "VERSION").write_text("0.1.0-rc.1\n", encoding="utf-8")
    proc = checkout.run("--version", "0.1.0-test.a")
    assert proc.returncode == 1 and "for a VERSION without a pre-release part" in proc.stderr
    assert checkout.builds() == []


def test_only_a_copy_outside_a_git_checkout_states_its_revision(checkout: Checkout) -> None:
    stated = f"{COMMIT}-dirty"
    proc = checkout.run("--version", "0.1.0-test.b1", "--revision", stated, mode="no-checkout")
    assert proc.returncode == 0, proc.stderr
    assert _build_args(checkout.build())["PEOPLE_PUBS_REVISION"] == stated
    assert _build_args(checkout.build("postgres.Dockerfile"))["PEOPLE_PUBS_REVISION"] == stated
    (checkout.state / "events").unlink()
    inside = checkout.run("--version", "0.1.0-test.b1", "--revision", stated)
    assert inside.returncode == 1 and f"is one, at revision {COMMIT}, which the label records" in inside.stderr
    assert checkout.builds() == []
    alone = checkout.run("--revision", stated, mode="no-checkout")
    assert alone.returncode == 2 and "--revision needs --version" in alone.stderr
    for revision in (COMMIT, COMMIT[:12] + "-dirty", "unknown", f"{COMMIT}-modified"):
        refused = checkout.run("--version", "0.1.0-test.b1", "--revision", revision, mode="no-checkout")
        assert refused.returncode == 2 and "--revision takes only <commit>-dirty" in refused.stderr, revision
    assert checkout.builds() == []
