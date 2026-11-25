"""run_release_rehearsal.sh, offline: the rehearsal of a PubDex release.

The real script runs in a scratch checkout against stub release.sh, docker,
helm and git, which record every call; the stub docker keeps the images and
containers in a state directory, and the stub release.sh writes the files a
build writes, gives each build's images new ids under the same local tags,
and changes, on request, what a second build gives. python3, tar, sha256sum
and cmp are the real ones.

That proves, without Docker: the usage; every refusal before anything is
built -- an unreachable daemon, an Engine or a platform the deployment does
not support, no buildx, no or another SBOM tool, no, another or a
misreporting pinned helm, a Docker context with TLS files, a VERSION with a
pre-release part, no Git checkout -- each leaving nothing behind; the order
of the steps: test release A built under a version of the run's own, that
directory verified, a second build, the comparison, the chart linted for the
oldest and the newest supported Kubernetes release and rendered from the
package file with the test release's images; that every Docker command after
the first runs with a client configuration of the run's own, empty or
naming only the caller's plugin directory, without the caller's context or
credentials; the comparison failing for a changed
byte of the source archive or the chart package, for other files, and for
SBOMs, a provenance record or IMAGES that differ beyond what a build has to
change; a failed build or verify stopping the run; the cleanup removing
every container and image of the run and nothing else, after success,
failure and SIGINT, SIGTERM or SIGHUP, and failing with the commands that
find what is left when it cannot confirm the removal; a credential in the
run's client configuration failing the run; and that the script never
publishes, pushes, tags or logs in.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import time
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "run_release_rehearsal.sh"
BASH = shutil.which("bash") or "/bin/bash"
PINS = ROOT / "DB" / "lib" / "cluster_tools.sh"
SBOM_PINS = dict(re.findall(r"^readonly (PINNED_SYFT_\w+)=(\S+)$",
                            (ROOT / "DB" / "lib" / "sbom_tool.sh").read_text(encoding="utf-8"), re.M))
SYFT_IMAGE = SBOM_PINS["PINNED_SYFT_IMAGE"]
SYFT_ID = SBOM_PINS["PINNED_SYFT_IMAGE_ID"]
HELM_VERSION = re.search(r"^readonly PINNED_HELM_VERSION=(\S+)$", PINS.read_text(encoding="utf-8"), re.M).group(1)
NEWEST = re.search(r"^readonly PINNED_KIND_NODE_IMAGE=kindest/node:v(\d+\.\d+\.\d+)@",
                   PINS.read_text(encoding="utf-8"), re.M).group(1)
OLDEST = "1.35.0"
VERSION = "0.2.0"
LABEL = "org.opencontainers.image.version"
ENDPOINT = "unix:///run/stub-docker.sock"
EXAMPLES = ["values-a.yaml", "values-b.yaml"]

STUB_RECORD = r'''{ printf '%s' "$PROGRAM"; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "$S/events"
'''

# The images and containers live in $S/images/<hex>/ (label, tags) and
# $S/containers/<id>/ (label, ancestor, mounts).
STUB_DOCKER = r'''#!/usr/bin/env bash
S="$STUB_STATE"
PROGRAM=docker
''' + STUB_RECORD + r'''
printf '%s\t%s\n' "$1" "${DOCKER_CONFIG-unset}" >> "$S/docker-config"
if [ "${STUB_DOCKER:-up}" = down ] && [ "$1" != context ]; then
  echo "Cannot connect to the Docker daemon" >&2
  exit 1
fi
last="${!#}"
image_of_tag() {
  local d
  for d in "$S"/images/*/; do
    [ -d "$d" ] || continue
    if grep -qxF -- "$1" "$d/tags"; then basename "$d"; return 0; fi
  done
  return 1
}
case "$1" in
  context) printf 'default\t%s\t%s\n' "@ENDPOINT@" "${STUB_CONTEXT_TLS:-0}" ;;
  info) ;;
  version)
    case "$3" in
      *Server.Version*) echo "${STUB_ENGINE:-29.9.1}" ;;
      *Server.Os*) echo "${STUB_PLATFORM:-linux/amd64}" ;;
    esac ;;
  buildx) [ "${STUB_BUILDX:-yes}" = none ] && exit 1; echo "buildx v0.0.0-stub" ;;
  image)
    case "$2" in
      inspect)
        if [ "$last" = "@SYFT@" ]; then
          [ "${STUB_SBOM:-yes}" = none ] && exit 1
          echo "${STUB_SBOM_ID:-@SYFT_ID@}"
          exit 0
        fi
        if [[ "$last" == sha256:* ]]; then
          hex="${last#sha256:}"
          [ -d "$S/images/$hex" ] || exit 1
        else
          hex="$(image_of_tag "$last")" || exit 1
        fi
        case "$*" in
          *RepoTags*) cat "$S/images/$hex/tags" ;;
          *'{{.Id}}'*) echo "sha256:$hex" ;;
        esac ;;
      ls)
        [ "${STUB_LS:-ok}" = fails ] && exit 1
        want="${last#label=@LABEL@=}"
        for d in "$S"/images/*/; do
          [ -d "$d" ] && [ "$(cat "$d/label")" = "$want" ] && echo "sha256:$(basename "$d")"
        done
        exit 0 ;;
      rm)
        [ "${STUB_RM:-ok}" = fails ] && exit 1
        if [[ "$last" == sha256:* ]]; then
          hex="${last#sha256:}"
          [ -d "$S/images/$hex" ] || exit 1
          [ -s "$S/images/$hex/tags" ] && { echo "conflict: image is tagged" >&2; exit 1; }
          rm -r "$S/images/$hex"
        else
          hex="$(image_of_tag "$last")" || exit 1
          grep -vxF -- "$last" "$S/images/$hex/tags" > "$S/tags.tmp" || true
          mv "$S/tags.tmp" "$S/images/$hex/tags"
          [ -s "$S/images/$hex/tags" ] || rm -r "$S/images/$hex"
        fi ;;
    esac ;;
  ps)
    for d in "$S"/containers/*/; do
      [ -d "$d" ] || continue
      case "$last" in
        label=@LABEL@=*) [ "$(cat "$d/label")" = "${last#label=@LABEL@=}" ] && echo "$(basename "$d")" ;;
        ancestor=*) [ "$(cat "$d/ancestor")" = "${last#ancestor=}" ] && echo "$(basename "$d")" ;;
      esac
    done
    exit 0 ;;
  inspect) [ -d "$S/containers/$last" ] || exit 1; cat "$S/containers/$last/mounts" ;;
  rm) [ "${STUB_RM:-ok}" = fails ] && exit 1; rm -rf "$S/containers/$last" ;;
esac
exit 0
'''

# build --test <name> --out <dir> and verify <dir>, as release.sh takes them.
STUB_RELEASE = r'''#!/usr/bin/env bash
S="$STUB_STATE"
PROGRAM=release.sh
''' + STUB_RECORD + r'''
{
  printf 'DOCKER_CONFIG=%s\n' "${DOCKER_CONFIG-unset}"
  printf 'DOCKER_HOST=%s\n' "${DOCKER_HOST-unset}"
  printf 'DOCKER_CONTEXT=%s\n' "${DOCKER_CONTEXT-unset}"
  printf 'DOCKER_AUTH_CONFIG=%s\n' "${DOCKER_AUTH_CONFIG-unset}"
  printf 'TMPDIR=%s\n' "${TMPDIR-unset}"
  printf 'CONFIG_JSON=%s\n' "$(tr -d '\n' < "${DOCKER_CONFIG:-/nonexistent}/config.json" 2>/dev/null)"
  printf 'CONFIG_FILES=%s\n' "$(ls -A "${DOCKER_CONFIG:-/nonexistent}" 2>/dev/null | tr '\n' ' ')"
} >> "$S/release-env"
label=@LABEL@
new_image() {  # new_image <hex> <version> [tag]: the tag moves to it
  local d
  mkdir -p "$S/images/$1"
  echo "$2" > "$S/images/$1/label"
  : > "$S/images/$1/tags"
  if [ -n "${3:-}" ]; then
    for d in "$S"/images/*/; do
      grep -vxF -- "$3" "$d/tags" > "$S/tags.tmp" || true
      mv "$S/tags.tmp" "$d/tags"
    done
    echo "$3" >> "$S/images/$1/tags"
  fi
}
new_container() {  # new_container <id> <label> <ancestor> <mount source>
  mkdir -p "$S/containers/$1"
  echo "$2" > "$S/containers/$1/label"
  echo "$3" > "$S/containers/$1/ancestor"
  echo "$4" > "$S/containers/$1/mounts"
}
case "$1" in
  build)
    name="$3" out="$5"
    n=$(( $(cat "$S/builds" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$S/builds"
    v="$(cat VERSION)-test.$name"
    differ=""
    [ "$n" = 2 ] && differ="${STUB_DIFFER:-}"
    if [ "${STUB_SLOW:-}" = "build$n" ]; then
      new_image "$(printf 'c%063d' "$n")" "$v"  # what an interrupted build can leave
      trap 'echo "build$n interrupted" >> "$S/signals"; exit 130' INT
      trap 'echo "build$n terminated" >> "$S/signals"; exit 143' TERM
      trap 'echo "build$n hung up" >> "$S/signals"; exit 129' HUP
      echo "build$n started" >> "$S/signals"
      for _ in $(seq "${STUB_SLOW_TICKS:-300}"); do sleep 0.1; done
      echo "build$n finished" >> "$S/signals"
    fi
    if [ "${STUB_FAIL:-}" = "build$n" ]; then
      echo "release.sh: stub build $n fails" >&2
      exit 1
    fi
    app="$(printf 'a%063d' "$n")" postgres="$(printf 'b%063d' "$n")"
    new_image "$app" "$v" "pubdex-release:$v"
    new_image "$postgres" "$v" "pubdex-postgres-release:$v"
    if [ "${STUB_LEAVE:-}" = containers ]; then
      new_container "probe$n" "$v" pubdex-release ""
      new_container "syft$n" "syft" "@SYFT@" "$TMPDIR/pubdex-release.stub/sbom"
      new_container "other$n" "syft" "@SYFT@" "/elsewhere/pubdex-release.other/sbom"
    fi
    if [ "${STUB_LOGIN:-}" = yes ]; then
      printf '{"credsStore": "stub"}\n' > "$DOCKER_CONFIG/config.json"
    fi
    mkdir -p "$out"
    printf 'the source archive of %s\n' "$v" > "$out/pubdex-$v-source.tar.gz"
    [ "$differ" = archive ] && printf 'one more byte' >> "$out/pubdex-$v-source.tar.gz"
    cp "$S/chart.tgz" "$out/pubdex-$v.tgz"
    [ "$differ" = package ] && printf '!' >> "$out/pubdex-$v.tgz"
    [ "$differ" = files ] && echo extra > "$out/EXTRA"
    role=postgres
    [ "$differ" = images ] && role=database
    printf 'application pubdex-release:%s sha256:%s\n%s pubdex-postgres-release:%s sha256:%s\n' \
      "$v" "$app" "$role" "$v" "$postgres" > "$out/IMAGES"
    psycopg=3.3.6
    [ "$differ" = sbom ] && psycopg=9.9.9
    for image in image postgres-image; do
      printf '{"spdxVersion": "SPDX-2.3", "documentNamespace": "https://example.org/spdx/%s",
 "creationInfo": {"created": "2026-11-19T00:00:0%sZ", "creators": ["Tool: syft-1.57.2"]},
 "packages": [{"name": "%s", "primaryPackagePurpose": "CONTAINER", "versionInfo": "%s",
               "checksums": [{"algorithm": "SHA256", "checksumValue": "%s"}],
               "externalRefs": [{"referenceLocator": "pkg:oci/pubdex@sha256:%s"}]},
              {"name": "psycopg", "versionInfo": "%s"}]}\n' \
        "$n" "$n" "$image" "$v" "$app" "$app" "$psycopg" > "$out/pubdex-$v-$image.spdx.json"
    done
    lock=0123
    [ "$differ" = record ] && lock=4567
    printf '{"format": "pubdex-provenance/2",
 "build": {"started": "%s", "finished": "%s", "docker_engine": "29.9.1"},
 "inputs": {"locks": {"DB/requirements.txt": "%s"}},
 "images": {"application": {"id": "sha256:%s", "config_digest": "sha256:%s", "labels": {"org.opencontainers.image.created": "%s", "%s": "%s"}},
            "postgres": {"id": "sha256:%s", "config_digest": "sha256:%s", "labels": {"org.opencontainers.image.created": "%s", "%s": "%s"}}},
 "files": {"IMAGES": "%s", "pubdex-%s-image.spdx.json": "%s", "pubdex-%s-postgres-image.spdx.json": "%s",
           "pubdex-%s-source.tar.gz": "same", "pubdex-%s.tgz": "same"}}\n' \
      "$n" "$n" "$lock" "$app" "$app" "$n" "$label" "$v" "$postgres" "$postgres" "$n" "$label" "$v" \
      "$n" "$v" "$n" "$v" "$n" "$v" "$v" > "$out/pubdex-$v-provenance.json"
    echo "stub checksums" > "$out/SHA256SUMS"
    echo "PASS: stub build $n of $v" ;;
  verify)
    if [ "${STUB_FAIL:-}" = verify ]; then
      echo "release.sh: stub verify refuses" >&2
      exit 1
    fi
    echo "PASS: stub verify" ;;
esac
exit 0
'''

# helm runs with a cleared environment, so the stub names the state
# directory itself.
STUB_HELM = r'''#!/usr/bin/env bash
S="@STATE@"
PROGRAM=helm
''' + STUB_RECORD + r'''
mode="$(cat "$S/helm-mode" 2>/dev/null)"
case "$1" in
  version) if [ "$mode" = old ]; then echo v4.0.0; else echo "v@HELM_VERSION@"; fi ;;
  show) printf '%s\n' "apiVersion: v2" "name: pubdex" "kubeVersion: '>=@OLDEST@-0'" ;;
  lint)
    if [ "$mode" = lint-fails ]; then echo "[ERROR] stub"; exit 1; fi
    echo "1 chart(s) linted, 0 chart(s) failed" ;;
  template)
    package="$3"
    version="${package##*/pubdex-}"
    version="${version%.tgz}"
    prev=""
    for a in "$@"; do
      if [ "$prev" = --set ]; then
        case "$a" in
          image.repository=*) app_repo="${a#*=}" ;;
          image.tag=*) app_tag="${a#*=}" ;;
          postgresImage.repository=*) pg_repo="${a#*=}" ;;
          postgresImage.tag=*) pg_tag="${a#*=}" ;;
        esac
      fi
      prev="$a"
    done
    chart="pubdex-$version"
    [ "$mode" = no-label ] && chart="pubdex-0.0.0"
    printf '%s\n' "kind: Deployment" "metadata:" "  labels:" "    helm.sh/chart: $chart" "spec:" \
      "  template:" "    spec:" "      containers:" "        - name: api" "          image: \"$app_repo:$app_tag\"" \
      "---" "kind: StatefulSet" "spec:" "  template:" "    spec:" "      containers:" \
      "        - image: $pg_repo:$pg_tag"
    if [ "$mode" = foreign-image ]; then
      printf '%s\n' "---" "kind: Job" "spec:" "  containers:" "    - image: docker.io/library/busybox:1"
    fi ;;
esac
exit 0
'''

STUB_GIT = '#!/bin/sh\n[ "${STUB_CHECKOUT:-yes}" = no ] && exit 128\necho true\n'


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _chart_package() -> bytes:
    """A small chart package with two examples, as the rehearsal extracts it."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in ["Chart.yaml", "values.yaml", *(f"examples/{example}" for example in EXAMPLES)]:
            data = f"# {name}\n".encode()
            info = tarfile.TarInfo(f"pubdex/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Checkout:
    """A scratch repository root with the script and stubs."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "checkout"
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        self.temp = tmp_path / "tmp"
        for path in (self.state / "images", self.state / "containers", self.temp):
            path.mkdir(parents=True)
        (self.state / "chart.tgz").write_bytes(_chart_package())
        self.root.mkdir()
        shutil.copy2(SCRIPT, self.root / SCRIPT.name)
        (self.root / "VERSION").write_text(f"{VERSION}\n", encoding="utf-8")
        (self.root / "DB" / "lib").mkdir(parents=True)
        for name in ("docker_versions.sh", "sbom_tool.sh"):
            shutil.copy2(ROOT / "DB" / "lib" / name, self.root / "DB" / "lib" / name)
        replace = {"@STATE@": str(self.state), "@SYFT@": SYFT_IMAGE, "@SYFT_ID@": SYFT_ID, "@LABEL@": LABEL,
                   "@ENDPOINT@": ENDPOINT, "@HELM_VERSION@": HELM_VERSION, "@OLDEST@": OLDEST}

        def fill(text: str) -> str:
            for key, value in replace.items():
                text = text.replace(key, value)
            return text

        _executable(self.root / "release.sh", fill(STUB_RELEASE))
        self.helm = self.root / ".tools" / f"helm-{HELM_VERSION}" / "helm"
        _executable(self.helm, fill(STUB_HELM))
        self.pin_helm()
        _executable(self.bin / "docker", fill(STUB_DOCKER))
        _executable(self.bin / "git", STUB_GIT)
        # An image of another test release, which the run must leave alone.
        self.add_image("f" * 64, "0.2.0-test.other", "pubdex-release:0.2.0-test.other")
        # What the caller has: a Docker configuration with a credential, a
        # context and a credential in the environment, none of which may reach the run.
        caller = tmp_path / "caller-docker"
        caller.mkdir()
        (caller / "config.json").write_text('{"credsStore": "caller"}\n', encoding="utf-8")
        self.env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "LANG": "C.UTF-8",
            "HOME": str(tmp_path),
            "TMPDIR": str(self.temp),
            "STUB_STATE": str(self.state),
            "DOCKER_CONFIG": str(caller),
            "DOCKER_CONTEXT": "caller-context",
            "DOCKER_AUTH_CONFIG": '{"auths": {}}',
        }

    def pin_helm(self) -> None:
        pins = PINS.read_text(encoding="utf-8")
        digest = hashlib.sha256(self.helm.read_bytes()).hexdigest()
        pins = re.sub(r"^readonly PINNED_HELM_MEMBER_SHA256=\S+$", f"readonly PINNED_HELM_MEMBER_SHA256={digest}",
                      pins, flags=re.M)
        (self.root / "DB" / "lib" / "cluster_tools.sh").write_text(pins, encoding="utf-8")

    def add_image(self, hex_id: str, version: str, tag: str = "") -> None:
        image = self.state / "images" / hex_id
        image.mkdir()
        (image / "label").write_text(version + "\n", encoding="utf-8")
        (image / "tags").write_text(tag + "\n" if tag else "", encoding="utf-8")

    def command(self) -> List[str]:
        return [BASH, str(self.root / SCRIPT.name)]

    def run(self, *args: str, helm: str = "", **extra: str) -> subprocess.CompletedProcess:
        (self.state / "helm-mode").write_text(helm, encoding="utf-8")
        return subprocess.run([*self.command(), *args], cwd=self.tmp, env=dict(self.env, **extra),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def popen(self, **extra: str) -> subprocess.Popen:
        (self.state / "helm-mode").write_text("", encoding="utf-8")
        return subprocess.Popen(self.command(), cwd=self.tmp, env=dict(self.env, **extra),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)

    def events(self) -> List[List[str]]:
        path = self.state / "events"
        return [line.split("\x1f") for line in path.read_text().splitlines()] if path.exists() else []

    def calls(self, program: str) -> List[List[str]]:
        return [event[1:] for event in self.events() if event[0] == program]

    def release_env(self) -> List[dict]:
        path = self.state / "release-env"
        if not path.exists():
            return []
        blocks, current = [], {}
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            current[key] = value
            if key == "CONFIG_FILES":
                blocks.append(current)
                current = {}
        return blocks

    def images(self) -> dict:
        return {path.name: (path / "label").read_text().strip() for path in (self.state / "images").iterdir()}

    def containers(self) -> List[str]:
        return sorted(path.name for path in (self.state / "containers").iterdir())

    def signals(self) -> List[str]:
        path = self.state / "signals"
        return path.read_text().splitlines() if path.exists() else []

    def leftover_directories(self) -> List[str]:
        return sorted(path.name for path in self.temp.iterdir())

    def wait_for(self, line: str, proc: subprocess.Popen) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if line in self.signals():
                return
            time.sleep(0.05)
        proc.kill()
        pytest.fail(f"never saw {line!r}")


def assert_nothing_of_the_run_left(fake: Checkout) -> None:
    assert fake.images() == {"f" * 64: "0.2.0-test.other"}, "every image of the run goes, and only those"
    assert fake.leftover_directories() == [], "the run's temporary directory goes"


def test_help_and_usage_errors(tmp_path: Path) -> None:
    fake = Checkout(tmp_path)
    proc = fake.run("--help")
    assert proc.returncode == 0 and proc.stdout.startswith("usage: run_release_rehearsal.sh")
    for args in (["--bogus"], ["--help", "extra"]):
        proc = fake.run(*args)
        assert proc.returncode == 2 and "usage: run_release_rehearsal.sh" in proc.stderr
    assert fake.events() == [] and fake.leftover_directories() == []


def test_a_rehearsal_builds_verifies_compares_and_renders_in_order_and_cleans_up(tmp_path: Path) -> None:
    fake = Checkout(tmp_path)
    proc = fake.run()
    assert proc.returncode == 0, proc.stderr
    release = fake.calls("release.sh")
    assert [call[0] for call in release] == ["build", "verify", "build"]
    first, verify, second = release
    assert first[1] == "--test" and re.fullmatch(r"a[0-9a-f]{10}", first[2]), "a name of the run's own"
    name = first[2]
    version = f"{VERSION}-test.{name}"
    run_dir = Path(first[4]).parent
    assert run_dir.parent == fake.temp and run_dir.name.startswith("pubdex-rehearsal.")
    assert first == ["build", "--test", name, "--out", str(run_dir / "first")]
    assert verify == ["verify", str(run_dir / "first")]
    assert second == ["build", "--test", name, "--out", str(run_dir / "second")]
    assert f"PASS: the rehearsal of test release A, {version}," in proc.stdout
    assert "nothing was published" in proc.stdout

    # The run's own, empty Docker configuration; the caller's context and
    # credentials reach neither release.sh nor any later docker command.
    for env in fake.release_env():
        assert env["DOCKER_CONFIG"] == str(run_dir / "docker")
        assert env["DOCKER_CONTEXT"] == "unset" and env["DOCKER_AUTH_CONFIG"] == "unset"
        assert env["DOCKER_HOST"] == ENDPOINT and env["TMPDIR"] == str(run_dir / "tmp")
    assert fake.release_env()[0]["CONFIG_FILES"] == "", "the configuration starts empty"
    configs = (fake.state / "docker-config").read_text().splitlines()
    assert configs[0].split("\t")[0] == "context", "only the daemon's address is read from the caller's configuration"
    assert {line.split("\t")[1] for line in configs[1:]} == {str(run_dir / "docker")}

    # The comparison and the chart from its package file.
    for line in (f"pubdex-{version}-source.tar.gz: the same bytes in both builds",
                 f"pubdex-{version}.tgz: the same bytes in both builds",
                 "IMAGES: the same roles and tags, other image ids",
                 f"pubdex-{version}-image.spdx.json: the same but for the build time and the image identities",
                 f"pubdex-{version}-provenance.json: the same but for the build time and the image identities"):
        assert line in proc.stdout
    helm = fake.calls("helm")
    package = str(run_dir / "first" / f"pubdex-{version}.tgz")
    lints = [call for call in helm if call[0] == "lint"]
    expected = []
    for kube in (OLDEST, NEWEST):
        for example in [None, *EXAMPLES]:
            values = ["--values", str(run_dir / "chart" / "pubdex" / "examples" / example)] if example else []
            expected.append(["lint", "--strict", package, "--kube-version", kube, *values])
    assert lints == expected
    templates = [call for call in helm if call[0] == "template"]
    assert len(templates) == 1 + len(EXAMPLES)
    for call in templates:
        assert call[1:3] == ["rehearsal", package]
        for setting in ("image.registry=", "image.repository=pubdex-release", f"image.tag={version}",
                        "postgresImage.registry=", "postgresImage.repository=pubdex-postgres-release",
                        f"postgresImage.tag={version}"):
            assert setting in call, setting
    assert f"the images pubdex-release:{version} and pubdex-postgres-release:{version}" in proc.stdout

    # Order: both builds and the verify, then helm, then the cleanup.
    events = fake.events()
    last_release = max(i for i, event in enumerate(events) if event[0] == "release.sh")
    first_helm_lint = min(i for i, event in enumerate(events) if event[0] == "helm" and event[1] == "lint")
    first_removal = min(i for i, event in enumerate(events) if event[0] == "docker" and event[1:3] == ["image", "rm"])
    assert last_release < first_helm_lint < first_removal
    assert "the run's images carry only the local tags" in proc.stdout
    assert "holds no registry credential" in proc.stdout
    assert_nothing_of_the_run_left(fake)


def test_the_callers_plugin_directory_and_nothing_else_of_its_configuration_reaches_the_run(tmp_path: Path) -> None:
    fake = Checkout(tmp_path)
    plugins = Path(fake.env["DOCKER_CONFIG"]) / "cli-plugins"
    plugins.mkdir()
    proc = fake.run()
    assert proc.returncode == 0, proc.stderr
    for env in fake.release_env():
        assert env["CONFIG_FILES"].split() == ["config.json"]
        assert json.loads(env["CONFIG_JSON"]) == {"cliPluginsExtraDirs": [str(plugins)]}
    # Without DOCKER_CONFIG, the caller's configuration directory is ~/.docker.
    fake = Checkout(tmp_path / "home-default")
    (Path(fake.env["HOME"]) / ".docker" / "cli-plugins").mkdir(parents=True)
    env = dict(fake.env)
    del env["DOCKER_CONFIG"]
    fake.env = env
    assert fake.run().returncode == 0
    assert json.loads(fake.release_env()[0]["CONFIG_JSON"]) == {
        "cliPluginsExtraDirs": [str(Path(fake.env["HOME"]) / ".docker" / "cli-plugins")]}


def test_the_script_never_publishes_pushes_tags_or_logs_in() -> None:
    code = "\n".join(line for line in SCRIPT.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    # release.sh runs only through the function release(), with build or verify.
    assert re.findall(r"^.*release\.sh.*\"\$@\".*$", code, re.M) == ['  TMPDIR="$RUN_DIR/tmp" "$ROOT/release.sh" "$@"']
    assert len(re.findall(r"release\.sh\"", code)) == 1
    assert set(re.findall(r"^\s*release (\S+)", code, re.M)) == {"build", "verify"}
    assert re.search(r"docker\s+(push|login|logout|tag|manifest|buildx\s+imagetools)\b|--push\b", code) is None


def test_no_call_of_a_run_publishes_pushes_tags_or_logs_in(tmp_path: Path) -> None:
    fake = Checkout(tmp_path)
    assert fake.run().returncode == 0
    assert all(call[0] in ("build", "verify") for call in fake.calls("release.sh"))
    for call in fake.calls("docker"):
        assert call[0] not in ("push", "login", "tag", "manifest", "build", "run", "pull"), call
        assert call[:2] != ["image", "tag"] and call[:2] != ["buildx", "build"], call


@pytest.mark.parametrize(
    "differ, message",
    [
        ("archive", "pubdex-{v}-source.tar.gz differs between the two builds"),
        ("package", "pubdex-{v}.tgz differs between the two builds"),
        ("files", "the two builds wrote different files"),
        ("sbom", "pubdex-{v}-image.spdx.json differs beyond the build time and the image identities, at .packages[1].versionInfo"),
        ("record", "pubdex-{v}-provenance.json differs beyond the build time and the image identities, at "
                   ".inputs.locks.DB/requirements.txt"),
        ("images", "IMAGES names other roles or tags in the two builds"),
    ],
)
def test_a_second_build_that_differs_fails_the_run_and_still_cleans_up(tmp_path: Path, differ: str,
                                                                       message: str) -> None:
    fake = Checkout(tmp_path)
    proc = fake.run(STUB_DIFFER=differ)
    assert proc.returncode == 1
    version = f"{VERSION}-test.{fake.calls('release.sh')[0][2]}"
    assert message.format(v=version) in proc.stderr
    assert "PASS: the rehearsal" not in proc.stdout
    assert [call for call in fake.calls("helm") if call[0] in ("lint", "template")] == [], "no later step runs"
    assert f"no image, tag or container of {version} is left" in proc.stdout
    assert_nothing_of_the_run_left(fake)


@pytest.mark.parametrize(
    "failing, calls, message",
    [
        ("build1", ["build"], "release.sh build failed for test release A"),
        ("verify", ["build", "verify"], "release.sh verify refused the directory its build wrote"),
        ("build2", ["build", "verify", "build"], "release.sh build failed for the second build"),
    ],
)
def test_a_failed_build_or_verify_stops_the_run_and_cleans_up(tmp_path: Path, failing: str, calls: list,
                                                              message: str) -> None:
    fake = Checkout(tmp_path)
    proc = fake.run(STUB_FAIL=failing)
    assert proc.returncode == 1 and message in proc.stderr
    assert [call[0] for call in fake.calls("release.sh")] == calls
    assert fake.calls("helm") == [["version", "--template", "{{.Version}}"]]
    assert_nothing_of_the_run_left(fake)


@pytest.mark.parametrize(
    "mode, message",
    [
        ("lint-fails", f"helm lint --strict fails for pubdex-{{v}}.tgz, Kubernetes {OLDEST}, the chart's own values"),
        ("foreign-image", "names the images"),
        ("no-label", "renders no label helm.sh/chart: pubdex-{v}"),
    ],
)
def test_a_chart_package_that_does_not_lint_or_render_the_test_release_fails(tmp_path: Path, mode: str,
                                                                             message: str) -> None:
    fake = Checkout(tmp_path)
    proc = fake.run(helm=mode)
    assert proc.returncode == 1
    version = f"{VERSION}-test.{fake.calls('release.sh')[0][2]}"
    assert message.format(v=version) in proc.stderr
    assert_nothing_of_the_run_left(fake)


REFUSALS = [
    ({"STUB_DOCKER": "down"}, "cannot reach the Docker daemon"),
    ({"STUB_ENGINE": "27.5.1"}, "Docker Engine 27.5.1 is too old"),
    ({"STUB_ENGINE": "28.2.0"}, "a firewalld reload opens ports"),
    ({"STUB_PLATFORM": "linux/arm64"}, "the Docker daemon runs on 'linux/arm64'"),
    ({"STUB_BUILDX": "none"}, "the Docker buildx plugin was not found"),
    ({"STUB_SBOM": "none"}, "the pinned SBOM tool, Syft"),
    ({"STUB_SBOM_ID": "sha256:" + "0" * 64}, "not the pinned sha256:"),
    ({"STUB_CONTEXT_TLS": "3"}, "uses TLS files"),
    ({"STUB_CHECKOUT": "no"}, "this directory is not a Git checkout"),
]


@pytest.mark.parametrize("env, message", REFUSALS, ids=[next(iter(env.values())) for env, _ in REFUSALS])
def test_a_missing_prerequisite_is_refused_before_anything_is_built(tmp_path: Path, env: dict,
                                                                    message: str) -> None:
    fake = Checkout(tmp_path)
    proc = fake.run(**env)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    if env.get("STUB_SBOM") == "none":
        assert proc.stderr.rstrip().splitlines()[-1].strip() == "./release.sh setup"
    assert fake.calls("release.sh") == [], "nothing is built"
    assert not [call for call in fake.calls("docker") if call[0] == "rm" or call[:2] == ["image", "rm"]]
    assert fake.leftover_directories() == []


def test_the_pinned_helm_and_the_version_are_checked_before_anything_is_built(tmp_path: Path) -> None:
    fake = Checkout(tmp_path)
    proc = fake.run(helm="old")
    assert proc.returncode == 1 and "reports the version 'v4.0.0'" in proc.stderr
    stub = fake.helm.read_text(encoding="utf-8")
    fake.helm.write_text(stub + "# changed\n", encoding="utf-8")
    proc = fake.run()
    assert proc.returncode == 1 and "has the SHA-256" in proc.stderr and "./setup_cluster_tools.sh" in proc.stderr
    fake.helm.unlink()
    proc = fake.run()
    assert proc.returncode == 1 and f"the pinned helm {HELM_VERSION} is not installed" in proc.stderr
    fake.helm.write_text(stub, encoding="utf-8")
    fake.helm.chmod(0o755)
    (fake.root / "VERSION").write_text("0.2.0-rc.1\n", encoding="utf-8")
    proc = fake.run()
    assert proc.returncode == 1 and "VERSION holds '0.2.0-rc.1'" in proc.stderr
    assert fake.calls("release.sh") == [] and fake.leftover_directories() == []


def test_the_cleanup_removes_every_container_and_image_of_the_run_and_nothing_else(tmp_path: Path) -> None:
    fake = Checkout(tmp_path)
    proc = fake.run(STUB_LEAVE="containers")
    assert proc.returncode == 0, proc.stderr
    # The probes carry the run's label, the SBOM tool's containers mount its
    # directory; another run's SBOM tool container stays.
    assert fake.containers() == ["other1", "other2"]
    removed = [call[2] for call in fake.calls("docker") if call[:2] == ["rm", "-f"]]
    assert sorted(removed) == ["probe1", "probe2", "syft1", "syft2"]
    assert_nothing_of_the_run_left(fake)


def test_a_cleanup_that_cannot_be_confirmed_fails_with_the_commands_that_find_what_is_left(tmp_path: Path) -> None:
    fake = Checkout(tmp_path)
    proc = fake.run(STUB_RM="fails")
    assert proc.returncode == 1
    version = f"{VERSION}-test.{fake.calls('release.sh')[0][2]}"
    assert f"WARNING: could not confirm that the rehearsal of {version} left nothing behind" in proc.stderr
    assert f"docker image ls -a --filter label={LABEL}={version}" in proc.stderr
    assert f"docker ps -a --filter label={LABEL}={version}" in proc.stderr
    assert f"docker ps -a --filter ancestor={SYFT_IMAGE}" in proc.stderr
    assert "PASS: the rehearsal" not in proc.stdout
    assert fake.leftover_directories() == [], "the directory goes even so"


def test_a_credential_in_the_runs_docker_configuration_fails_the_run(tmp_path: Path) -> None:
    fake = Checkout(tmp_path)
    proc = fake.run(STUB_LOGIN="yes")
    assert proc.returncode == 1
    assert "the run's Docker client configuration holds registry credentials (credsStore)" in proc.stderr
    assert_nothing_of_the_run_left(fake)


@pytest.mark.parametrize("sig, status, seen", [
    (signal.SIGINT, 130, "build1 interrupted"),
    (signal.SIGTERM, 143, "build1 terminated"),
    (signal.SIGHUP, 129, "build1 hung up"),
])
def test_a_signal_to_the_process_group_during_a_build_stops_it_and_cleans_up(tmp_path: Path, sig: int, status: int,
                                                                            seen: str) -> None:
    fake = Checkout(tmp_path)
    proc = fake.popen(STUB_SLOW="build1")
    fake.wait_for("build1 started", proc)
    os.killpg(proc.pid, sig)  # Ctrl-C, a timeout or a hangup: the whole group
    out, _err = proc.communicate(timeout=60)
    assert proc.returncode == status
    assert fake.signals() == ["build1 started", seen]
    assert [call[0] for call in fake.calls("release.sh")] == ["build"], "no later step runs"
    assert "PASS: the rehearsal" not in out
    # The image the interrupted build left goes too.
    assert_nothing_of_the_run_left(fake)


def test_a_signal_to_the_script_alone_takes_effect_when_the_build_returns(tmp_path: Path) -> None:
    fake = Checkout(tmp_path)
    proc = fake.popen(STUB_SLOW="build1", STUB_SLOW_TICKS="10")
    fake.wait_for("build1 started", proc)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=60)
    assert proc.returncode == 143
    assert fake.signals() == ["build1 started", "build1 finished"]
    assert [call[0] for call in fake.calls("release.sh")] == ["build"]
    assert_nothing_of_the_run_left(fake)


def test_the_script_is_executable_and_names_its_stage() -> None:
    assert os.access(SCRIPT, os.X_OK)
    header = SCRIPT.read_text(encoding="utf-8").split("set -euo pipefail")[0]
    assert "stage 10 of run_ci_suite.sh" in header
