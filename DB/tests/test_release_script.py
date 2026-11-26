"""release.sh, offline: building, verifying and publishing a PubDex release.

The real script runs in a scratch checkout -- the script, the identity rules
and tool pins it sources, VERSION, a small chart, the module holding the
transform version and a few other files -- against stub git, docker, helm and
build_image.sh, which record every call and keep the images and the registry
in a state file. tar, gzip and sha256sum are the real ones, so the source
archive and the checksum file are real.

That proves, without Docker, Git or a registry: the usage and every refusal
-- a PubDex release from a checkout with changes, without its annotated tag
or at another commit, a reserved version, a chart of another version, an
output directory in the way -- each before anything is built; the names of
the artifacts and the checksum file's format; a source archive that is the
same on a second build whatever the files' times, with the checkout's files
under pubdex-<version>/, owner 0 and the modes 644 and 755; a chart package
made with SOURCE_DATE_EPOCH the commit's time; images that a PubDex release
builds without Docker's build cache and a test release with it; that build
and verify never push, tag, log in or ask a registry; what verify refuses;
and that publish refuses a test release, a published or unverified
directory, a checkout that is not the tagged commit, a version either
repository holds as another image or as one whose digest the verified image
does not record, a version whose absence the registry does not explicitly
report or whose digest it does not give, and a local tag in the way, each
before any push; then pushes the PostgreSQL image before the application
image and writes both digests, and never logs in; and that a second publish
after a failed one pushes only what the registry does not hold yet. The
stubbed registry keeps what a push left from one run to the next.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "release.sh"
PINS = ROOT / "DB" / "lib" / "cluster_tools.sh"
SBOM_PINS = dict(re.findall(r"^readonly (PINNED_SYFT_\w+)=(\S+)$",
                            (ROOT / "DB" / "lib" / "sbom_tool.sh").read_text(encoding="utf-8"), re.M))
SYFT_IMAGE = SBOM_PINS["PINNED_SYFT_IMAGE"]
SYFT_ID = SBOM_PINS["PINNED_SYFT_IMAGE_ID"]
#: The files of the checkout the provenance record reads, copied as they are.
RECORD_INPUTS = ("Dockerfile", "postgres.Dockerfile", ".dockerignore", "postgres.Dockerfile.dockerignore",
                 "DB/requirements.txt", "DB/requirements-pip.txt", "DB/requirements-build.txt",
                 "DB/fetch_verified.py", "DB/install_pip.py", "DB/install_locked.py", "DB/locked_files.py",
                 "DB/release_provenance.py", "DB/verify_release.py", "DB/people_pubs/api/database.py",
                 "DB/lib/sbom_tool.sh", "third_party/docker-library-postgres/LICENSE")
OVERLAY = ROOT / "DB" / "tests" / "release_overlay"
OVERLAY_FILES = ("compatible.sql", "incompatible.sql", "transform-version-suffix", "application.py")
COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
EPOCH = "1763452800"
VERSION = "0.2.0"
TEST_VERSION = "0.2.0-test.a"
PREFIX = "registry.example.org/team"

STUB_PY = r'''
import gzip, io, json, os, sys, tarfile, uuid
from pathlib import Path

STATE = Path(os.environ["STUB_STATE"])


def record(program, args):
    with open(STATE / "events", "a") as events:
        events.write("\x1f".join([program, *args]) + "\n")


def modes():
    # A file, not the environment: helm runs with a cleared one.
    path = STATE / "mode"
    return set(filter(None, path.read_text().split(","))) if path.exists() else set()


def key(reference):
    return reference.replace("/", "_S_").replace(":", "_C_")


def arg(dockerfile, name):
    """An ARG default of the scratch checkout's Dockerfile."""
    text = (Path(os.environ["STUB_ROOT"]) / dockerfile).read_text()
    return next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith(f"ARG {name}="))


def pins():
    """The runtime lock's pins in the scratch checkout."""
    lock = (Path(os.environ["STUB_ROOT"]) / "DB/requirements.txt").read_text()
    return dict(line.split()[0].split("==") for line in lock.splitlines() if line[:1].isalnum() and "==" in line)


LIBPQ5 = "17.11-0+deb13u1"


def spdx(repository, version, role):
    """An SPDX 2.3 document shaped as Syft writes one for an image."""
    def package(name, version, purl):
        return {"name": name, "SPDXID": f"SPDXRef-Package-{name}", "versionInfo": version,
                "externalRefs": [{"referenceType": "purl", "referenceLocator": purl}]}
    root = {"name": repository, "SPDXID": "SPDXRef-DocumentRoot-Image", "versionInfo": version,
            "primaryPackagePurpose": "CONTAINER"}
    packages = [root]
    if role == "application":
        packages.append(package("libpq5", LIBPQ5, f"pkg:deb/debian/libpq5@{LIBPQ5}"))
        packages += [package(name, pinned, f"pkg:pypi/{name}@{pinned}") for name, pinned in pins().items()]
    else:
        postgresql = "16.14" if "sbom-old-postgresql" in modes() else arg("postgres.Dockerfile", "PG_VERSION")
        packages.append(package("postgresql", postgresql, f"pkg:generic/postgresql@{postgresql}"))
    return {"spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT", "name": repository,
            "creationInfo": {"creators": ["Tool: syft-1.57.2"], "created": "2026-11-18T03:01:40Z"},
            "packages": packages, "relationships": [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType":
                                                     "DESCRIBES", "relatedSpdxElement": root["SPDXID"]}]}


def syft_scan(args):
    """The pinned Syft on a saved image: an SBOM into the directory mounted at /sbom."""
    mount = Path(os.environ["STUB_MOUNT"].split(",")[1].split("=", 1)[1])
    value = lambda flag: args[args.index(flag) + 1]
    with tarfile.open(mount / args[0].split("/sbom/")[1]) as archive:
        saved = json.load(archive.extractfile("manifest.json"))[0]["RepoTags"][0]
    role = "application" if saved.startswith("pubdex-release:") else "postgres"
    document = spdx(value("--source-name"), value("--source-version"), role)
    (mount / value("--output").split("=/sbom/")[1]).write_text(json.dumps(document))
    return 0


def save_image(args):
    destination, reference = args
    labels = (STATE / "images" / key(reference) / "labels.json").read_bytes()
    with tarfile.open(destination, "w") as archive:
        for name, content in (("config.json", labels), ("manifest.json", json.dumps([
                {"Config": "config.json", "RepoTags": [reference], "Layers": []}]).encode())):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return 0


def build_image(args):
    record("build_image", args)
    if "build-fails" in modes():
        print("stub: build failed", file=sys.stderr)
        return 1
    # The tree whose build_image.sh runs: the checkout, or a copy of it.
    root = Path(os.environ["STUB_BUILD_ROOT"])
    value = lambda flag: args[args.index(flag) + 1] if flag in args else None
    version = value("--version") or (root / "VERSION").read_text().strip()
    revision = value("--revision") or os.environ["STUB_COMMIT"] + ("-dirty" if os.environ.get("STUB_STATUS") else "")
    transform = (root / "DB/people_pubs/__init__.py").read_text().split('"')[1]
    with open(STATE / "build_roots.jsonl", "a") as log:
        log.write(json.dumps({
            "root": str(root), "ceiling": os.environ.get("GIT_CEILING_DIRECTORIES"),
            "modes": {path.relative_to(root).as_posix() or ".": oct(path.stat().st_mode)
                      for path in [root, *root.rglob("*")] if ".tools" not in path.parts and not path.is_symlink()},
            "schema": sorted(path.name for path in (root / "DB/schema").iterdir()),
        }) + "\n")
    for tag, role in ((value("--tag"), "application"), (value("--postgres-tag"), "postgres")):
        image = STATE / "images" / key(tag)
        image.mkdir(parents=True, exist_ok=True)
        base = arg("Dockerfile" if role == "application" else "postgres.Dockerfile",
                   "PYTHON_BASE" if role == "application" else "DEBIAN_BASE")
        labels = {"org.opencontainers.image.version": version, "org.opencontainers.image.revision": revision,
                  "org.opencontainers.image.created": "2026-11-18T03:01:33Z",
                  "org.opencontainers.image.base.name": "docker.io/library/" + base.split("@")[0],
                  "org.opencontainers.image.base.digest": base.split("@")[1]}
        if role == "application":
            labels["pubdex.transform-version"] = transform
        else:
            labels["pubdex.postgresql-version"] = arg("postgres.Dockerfile", "PG_VERSION")
        (image / "id").write_text("sha256:" + uuid.uuid4().hex * 2 + "\n")
        (image / "platform").write_text("linux/amd64\n")
        (image / "digests").write_text("")
        for name, label in labels.items():
            (image / f"label.{name}").write_text(label + "\n")
        (image / "labels.json").write_text(json.dumps(labels) + "\n")
    return 0


def helm(args):
    record("helm", args)
    with open(STATE / "helm.jsonl", "a") as log:
        log.write(json.dumps({"args": args, "epoch": os.environ.get("SOURCE_DATE_EPOCH"),
                              "kubeconfig": os.environ.get("KUBECONFIG"), "environment": sorted(os.environ)}) + "\n")
    if args[0] == "version":
        print("v4.3.1")
        return 0
    if args[:2] == ["show", "chart"]:
        path = Path(args[2])
        if path.is_dir():
            sys.stdout.write((path / "Chart.yaml").read_text())
        else:
            with tarfile.open(path) as archive:
                sys.stdout.write(archive.extractfile("pubdex/Chart.yaml").read().decode())
        return 0
    if args[0] == "package":
        source, destination = Path(args[1]), Path(args[args.index("--destination") + 1])
        chart = (source / "Chart.yaml").read_text()
        version = args[args.index("--version") + 1] if "--version" in args else \
            next(line.split(": ")[1].strip('"') for line in chart.splitlines() if line.startswith("version: "))
        if "--version" in args:
            chart = "".join(f"version: {version}\n" if line.startswith("version: ") else
                            f"appVersion: {version}\n" if line.startswith("appVersion: ") else line + "\n"
                            for line in chart.splitlines())
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    content = chart.encode() if path.name == "Chart.yaml" else path.read_bytes()
                    info = tarfile.TarInfo("pubdex/" + path.relative_to(source).as_posix())
                    info.size, info.mtime, info.mode = len(content), int(os.environ["SOURCE_DATE_EPOCH"]), 0o644
                    archive.addfile(info, io.BytesIO(content))
        (destination / f"pubdex-{version}.tgz").write_bytes(gzip.compress(data.getvalue(), mtime=0))
        return 0
    if args[0] == "lint":
        return 1 if "lint-fails" in modes() else 0
    if args[0] == "template":
        print("kind: Deployment")
        return 0
    print(f"stub helm: unexpected {args}", file=sys.stderr)
    return 2


program = sys.argv[1]
sys.exit({"helm": helm, "build_image": build_image, "syft_scan": syft_scan, "save_image": save_image}[program](sys.argv[2:]))
'''

STUB_GIT = r'''#!/usr/bin/env bash
# Answers only the read-only questions release.sh and the identity rules ask.
{ printf 'git'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "$STUB_STATE/events"
case " $* " in
  " --version ") echo "git version 2.43.0"; exit 0 ;;
  *" rev-parse --verify --quiet HEAD "*) echo "$STUB_COMMIT"; exit 0 ;;
  *" status --porcelain "*) printf '%s' "${STUB_STATUS:-}"; exit 0 ;;
  *" ls-files --others --ignored "*) exit 0 ;;
  *" cat-file -t refs/tags/"*)
    [ "$STUB_TAG_TYPE" = missing ] && exit 128
    echo "$STUB_TAG_TYPE"; exit 0 ;;
  *" rev-parse --verify --quiet refs/tags/"*)
    [ -n "$STUB_TAG_COMMIT" ] || exit 1
    echo "$STUB_TAG_COMMIT"; exit 0 ;;
  *" show --no-patch --format=%ct HEAD "*) echo "$STUB_EPOCH"; exit 0 ;;
  *" ls-files -z "*)
    for f in $STUB_FILES; do printf '%s\0' "$f"; done
    case " $* " in *" --others "*) for f in $STUB_UNTRACKED; do printf '%s\0' "$f"; done ;; esac
    exit 0 ;;
esac
echo "stub git: unexpected $*" >&2
exit 2
'''

STUB_DOCKER = r'''#!/usr/bin/env bash
# Images are directories under $STUB_STATE/images. The registry holds what
# docker push left in $STUB_STATE/registry, "<reference> <digest>" per line,
# which persists from one run to the next; $STUB_STATE/manifest, a run's own,
# overrides its answers per reference: "exists" is another image, held under
# OTHER_DIGEST. A reference in neither is "manifest unknown".
state="$STUB_STATE"
{ printf 'docker'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "$state/events"
mode=",$(cat "$state/mode" 2>/dev/null),"
key() { local k="${1//\//_S_}"; printf '%s' "${k//:/_C_}"; }
OTHER_DIGEST="sha256:$(printf 'f%.0s' {1..64})"
answer() { awk -v r="$1" '$1 == r { print $2 }' "$state/manifest" 2>/dev/null; }
held() { awk -v r="$1" '$1 == r { print $2 }' "$state/registry" 2>/dev/null | tail -n 1; }
case "$1" in
  buildx)
    if [ "$2" = imagetools ] && [ "$3" = inspect ]; then
      case "$mode" in *,imagetools-fails,*) echo "ERROR: failed to authorize: 503 Service Unavailable" >&2; exit 1 ;; esac
      digest="$(held "$4")"
      [ "$(answer "$4")" = exists ] && digest="$OTHER_DIGEST"
      [ -n "$digest" ] || { echo "ERROR: $4: not found" >&2; exit 1; }
      printf 'Name:      %s\nMediaType: application/vnd.docker.distribution.manifest.v2+json\nDigest:    %s\n' "$4" "$digest"
      exit 0
    fi
    if [ "$2" = version ]; then
      case "$mode" in *,no-buildx,*) echo "docker: 'buildx' is not a docker command." >&2; exit 1 ;; esac
      echo "github.com/docker/buildx v0.39.0"; exit 0
    fi
    if [ "$2" = inspect ]; then
      printf '%s\n' 'Name: default' 'Driver: docker' '' 'Nodes' 'BuildKit version: v0.35.0' 'Labels' \
        ' host-gateway-ip: 192.0.2.17'
      exit 0
    fi ;;
  version) echo "29.10.0 linux/amd64"; exit 0 ;;
  info) echo "overlay2 "; exit 0 ;;
  run)
    # The pinned Syft (its version, or an SBOM, written by the Python stub),
    # or a probe of a release image, answered here.
    shift
    image="" entry="" mount=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --rm|--read-only) shift ;;
        --entrypoint) entry="$2"; shift 2 ;;
        --mount) mount="$2"; shift 2 ;;
        --*) shift 2 ;;
        *) image="$1"; shift; break ;;
      esac
    done
    [ -d "$state/images/$(key "$image")" ] || { echo "docker: Error response from daemon: No such image: $image" >&2; exit 125; }
    case "$image" in
      *anchore/syft*)
        if [ "$1" = version ]; then
          case "$mode" in *,syft-old,*) v=1.57.1 ;; *) v=1.57.2 ;; esac
          printf 'Application:   syft\nVersion:       %s\n' "$v"
          exit 0
        fi
        STUB_MOUNT="$mount" exec "$STUB_PYTHON" -I -S "$STUB_SCRIPT" syft_scan "$@" ;;
    esac
    case "$entry" in
      cat)
        case "$mode" in *,no-build-packages,*) echo "cat: $1: No such file or directory" >&2; exit 1 ;; esac
        printf 'gcc 4:14.2.0-1\nlibc6-dev 2.41-12\nlibpq-dev %s\nmake 4.4.1-2\n' "$(cat "$state/libpq5")" ;;
      python) cat "$state/client.json" ;;
      dpkg-query) cat "$state/libpq5" ;;
      postgres)
        case "$mode" in *,old-postgresql,*) echo "postgres (PostgreSQL) 16.14" ;; *) echo "postgres (PostgreSQL) $(cat "$state/pg_version")" ;; esac ;;
      *) echo "stub docker run: unexpected $entry $*" >&2; exit 2 ;;
    esac
    exit 0 ;;
  pull)
    # The pinned image, by digest: the pinned id unless the mode says otherwise.
    dir="$state/images/$(key "$2")"
    mkdir -p "$dir"
    case "$mode" in *,pull-other,*) echo "sha256:$(printf '9%.0s' {1..64})" > "$dir/id" ;; *) echo "$STUB_SYFT_ID" > "$dir/id" ;; esac
    echo linux/amd64 > "$dir/platform"; : > "$dir/digests"
    exit 0 ;;
  image)
    if [ "$2" = save ]; then
      dir="$state/images/$(key "$5")"
      [ -d "$dir" ] || { echo "Error: No such image: $5" >&2; exit 1; }
      exec "$STUB_PYTHON" -I -S "$STUB_SCRIPT" save_image "$4" "$5"
    fi
    if [ "$2" = inspect ]; then
      dir="$state/images/$(key "$5")"
      [ -d "$dir" ] || { echo "Error: No such image: $5" >&2; exit 1; }
      case "$4" in
        '{{json .Config.Labels}}') cat "$dir/labels.json" 2>/dev/null || echo '{}' ;;
        '{{.Id}}') cat "$dir/id" ;;
        '{{.Os}}/{{.Architecture}}') cat "$dir/platform" ;;
        '{{range .RepoDigests}}{{println .}}{{end}}')
          # They belong to the image: every tag of one image id shows them all.
          id="$(cat "$dir/id")"
          for other in "$state"/images/*/; do
            [ "$(cat "$other/id")" = "$id" ] && cat "$other/digests"
          done | sort -u ;;
        '{{index .Config.Labels "'*)
          name="${4#*\"}"; name="${name%%\"*}"
          cat "$dir/label.$name" 2>/dev/null || echo ;;
        *) echo "stub docker: unexpected format $4" >&2; exit 2 ;;
      esac
      exit 0
    fi ;;
  tag)
    dst="$state/images/$(key "$3")"
    rm -rf "$dst"; cp -r "$state/images/$(key "$2")" "$dst"; : > "$dst/digests"
    exit 0 ;;
  manifest)
    reply="$(answer "$3")"
    [ -z "$reply" ] && [ -n "$(held "$3")" ] && reply=exists
    case "${reply:-unknown}" in
      exists) echo '{"schemaVersion": 2}'; exit 0 ;;
      unknown) echo "manifest unknown" >&2 ;;
      denied) echo "unauthorized: authentication required" >&2 ;;
      denied-unknown) echo "manifest unknown; unauthorized: authentication required" >&2 ;;
      name-unknown) echo "errors: name unknown: repository name not known to registry" >&2 ;;
      odd) echo "error: something else went wrong" >&2 ;;
    esac
    exit 1 ;;
  push)
    case "$mode" in *",push-fails:$2,"*) echo "denied: requested access to the resource is denied" >&2; exit 1 ;; esac
    digest="sha256:$(printf '%s' "$2" | sha256sum | cut -d' ' -f1)"
    echo "$2 $digest" >> "$state/registry"
    # The manifest landed, but the client never learnt it: nothing recorded.
    case "$mode" in *",push-fails-after-manifest:$2,"*) echo "error: unexpected EOF" >&2; exit 1 ;; esac
    echo "${2%:*}@$digest" >> "$state/images/$(key "$2")/digests"
    echo "${2##*:}: digest: $digest size: 1234"
    exit 0 ;;
  login) exit 0 ;;
esac
echo "stub docker: unexpected $*" >&2
exit 2
'''

CHART_YAML = 'apiVersion: v2\nname: pubdex\n# One version.\nversion: 0.2.0\nappVersion: "0.2.0"\n'


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class Checkout:
    """A scratch repository root with the script and stubs."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "checkout"
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        self.state.mkdir()
        stubs = tmp_path / "stubs.py"
        stubs.write_text(STUB_PY, encoding="utf-8")
        files = {
            "VERSION": f"{VERSION}\n",
            "LICENSE": "MIT License\n",
            "README.md": "# PubDex\n",
            "DB/people_pubs/__init__.py": '__version__ = "1.5.4"\n',
            "DB/schema/0001_schema_change_record.sql": "-- backward-compatible: yes\n",
            "charts/pubdex/Chart.yaml": CHART_YAML,
            "charts/pubdex/values.yaml": "secrets: {name: \"\"}\n",
            "charts/pubdex/LICENSE": "MIT License\n",
            "charts/pubdex/templates/deployment.yaml": "kind: Deployment\n",
        }
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            path.chmod(0o664)
        _executable(self.root / "run_something.sh", "#!/bin/sh\necho run\n")
        shutil.copy2(SCRIPT, self.root / "release.sh")
        (self.root / "DB" / "lib").mkdir(parents=True)
        for name in ("release_identity.sh", "cluster_tools.sh"):
            shutil.copy2(ROOT / "DB" / "lib" / name, self.root / "DB" / "lib" / name)
        for name in RECORD_INPUTS:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, self.root / name)
        self.stubs = stubs
        wrapper = ('#!/bin/sh\nSTUB_BUILD_ROOT="$(cd "$(dirname "$0")" && pwd)" exec "{python}" -I -S "{stubs}" '
                   '{name} "$@"\n')
        _executable(self.root / "build_image.sh", wrapper.format(python=sys.executable, stubs=stubs, name="build_image"))
        # The test-only overlay, as the repository holds it.
        for name in OVERLAY_FILES:
            target = self.root / "DB" / "tests" / "release_overlay" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(OVERLAY / name, target)
            target.chmod(0o664)
        # release.sh runs helm with a cleared environment, so the wrapper
        # names the state directory itself.
        helm = self.root / ".tools" / "helm-4.3.1" / "helm"
        _executable(helm, f'#!/bin/sh\nSTUB_STATE="{self.state}" exec "{sys.executable}" -I -S "{stubs}" helm "$@"\n')
        pins = PINS.read_text(encoding="utf-8")
        digest = hashlib.sha256(helm.read_bytes()).hexdigest()
        pins = re.sub(r"^readonly PINNED_HELM_MEMBER_SHA256=\S+$", f"readonly PINNED_HELM_MEMBER_SHA256={digest}",
                      pins, flags=re.M)
        (self.root / "DB" / "lib" / "cluster_tools.sh").write_text(pins, encoding="utf-8")
        _executable(self.bin / "git", STUB_GIT)
        _executable(self.bin / "docker", STUB_DOCKER)
        _executable(self.bin / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        # The pinned SBOM tool, as ./release.sh setup leaves it.
        self.set_image(SYFT_IMAGE, SYFT_ID)
        # What the images report inside a container, for the stub docker run.
        lock = dict(re.findall(r"^([a-z0-9-]+)==(\S+)", (ROOT / "DB/requirements.txt").read_text(), re.M))
        (self.state / "client.json").write_text(json.dumps({
            "psycopg": lock["psycopg"], "psycopg-c": lock["psycopg-c"], "psycopg-pool": lock["psycopg-pool"],
            "implementation": "c", "libpq": 170011, "libpq_build": 170011}) + "\n")
        (self.state / "libpq5").write_text("17.11-0+deb13u1\n")
        (self.state / "pg_version").write_text(
            re.search(r"^ARG PG_VERSION=(\S+)$", (ROOT / "postgres.Dockerfile").read_text(), re.M).group(1) + "\n")
        # What git ls-files lists: every file but the pinned tools, as in a
        # real checkout, where .tools/ is ignored.
        self.tracked = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*")
                              if path.is_file() and ".tools" not in path.parts)

    def run(self, *args: str, mode: str = "", status: str = "", tag_type: str = "tag",
            tag_commit: Optional[str] = None, untracked: str = "", manifest: Optional[Dict[str, str]] = None,
            **extra: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "LANG": "C.UTF-8",
            "HOME": str(self.tmp),
            "TMPDIR": str(self.tmp),
            "STUB_STATE": str(self.state),
            "STUB_ROOT": str(self.root),
            "STUB_STATUS": status,
            "STUB_COMMIT": COMMIT,
            "STUB_EPOCH": EPOCH,
            "STUB_TAG_TYPE": tag_type,
            "STUB_TAG_COMMIT": COMMIT if tag_commit is None else tag_commit,
            "STUB_FILES": "\n".join(self.tracked),
            "STUB_UNTRACKED": untracked,
            "STUB_PYTHON": sys.executable,
            "STUB_SCRIPT": str(self.stubs),
            "STUB_SYFT_ID": SYFT_ID,
        }
        env.update(extra)
        (self.state / "mode").write_text(mode)
        (self.state / "manifest").write_text("".join(f"{ref} {answer}\n" for ref, answer in (manifest or {}).items()))
        return subprocess.run([str(self.root / "release.sh"), *args], cwd=self.tmp, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def events(self) -> List[List[str]]:
        """Every stub call, as [program, argument...]."""
        path = self.state / "events"
        return [line.split("\x1f") for line in path.read_text().splitlines()] if path.exists() else []

    def calls(self, program: str) -> List[List[str]]:
        return [event[1:] for event in self.events() if event[0] == program]

    def helm_calls(self) -> List[dict]:
        """Each helm call: its arguments, SOURCE_DATE_EPOCH, KUBECONFIG and environment."""
        path = self.state / "helm.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def reset_calls(self) -> None:
        for name in ("events", "helm.jsonl"):
            (self.state / name).unlink(missing_ok=True)

    def image(self, reference: str) -> Path:
        return self.state / "images" / reference.replace("/", "_S_").replace(":", "_C_")

    def image_id(self, reference: str) -> str:
        return (self.image(reference) / "id").read_text().strip()

    def set_image(self, reference: str, image_id: str) -> None:
        image = self.image(reference)
        image.mkdir(parents=True, exist_ok=True)
        (image / "id").write_text(image_id + "\n")
        (image / "platform").write_text("linux/amd64\n")
        (image / "digests").write_text("")

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")


@pytest.fixture
def checkout(tmp_path: Path) -> Checkout:
    return Checkout(tmp_path)


def _registry_calls(checkout: Checkout) -> List[List[str]]:
    """The docker calls that would change or ask a registry, or log in."""
    return [call for call in checkout.calls("docker")
            if call[0] in ("push", "tag", "login", "manifest", "pull") or call[:2] == ["buildx", "imagetools"]]


def _record_files(version: str) -> List[str]:
    """The two SBOMs and the provenance record."""
    return [f"pubdex-{version}-image.spdx.json", f"pubdex-{version}-postgres-image.spdx.json",
            f"pubdex-{version}-provenance.json"]


def _build_test(checkout: Checkout, name: str = "a", **kwargs) -> Path:
    out = checkout.tmp / f"out-{name}-{len(list(checkout.tmp.glob('out-*')))}"
    proc = checkout.run("build", "--test", name, "--out", str(out), **kwargs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return out


def _build_release(checkout: Checkout) -> Path:
    proc = checkout.run("build")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return checkout.root / "dist" / f"pubdex-{VERSION}"


# --- usage ----------------------------------------------------------------------


def test_it_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.parametrize("args", [
    [], ["deploy"], ["build", "--test"], ["build", "--test", "B1"], ["build", "--test", "b-1"], ["build", "--out"],
    ["build", "--source"], ["build", "--push"], ["verify"], ["verify", "a", "b"], ["publish"], ["publish", "dir"],
    ["publish", "--repository-prefix", PREFIX], ["publish", "dir", "--repository-prefix"],
    ["publish", "dir", "--repository-prefix", "registry.example.org/team/"],
    ["publish", "dir", "--repository-prefix", "https://registry.example.org/team"],
    ["publish", "dir", "--repository-prefix", "registry.example.org"],
    ["publish", "dir", "--repository-prefix", "registry.example.org/Team"],
    ["publish", "dir", "--repository-prefix", "registry.example.org/team/pubdex:0.2.0"],
    ["publish", "a", "b", "--repository-prefix", PREFIX], ["--help", "extra"], ["setup", "extra"],
    ["build", "--overlay", "b1"], ["build", "--test", "b1", "--overlay"], ["build", "--test", "b1", "--overlay", "b3"],
    ["build", "--test", "b1", "--overlay", "b1", "--overlay", "b2"], ["verify", "--overlay", "b1"],
    ["verify", "dir", "--overlay", "c"], ["verify", "dir", "--other"],
], ids=lambda args: " ".join(args) or "nothing")
def test_usage_errors_exit_2_before_anything_runs(checkout: Checkout, args: List[str]) -> None:
    proc = checkout.run(*args)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "usage: release.sh setup\n       release.sh build" in proc.stderr
    assert checkout.events() == []


def test_help(checkout: Checkout) -> None:
    proc = checkout.run("--help")
    assert proc.returncode == 0 and "release.sh build [--test <name> [--overlay b1|b2]]" in proc.stdout
    assert "publish" in proc.stdout and "Log in first; it never does." in proc.stdout


# --- build ----------------------------------------------------------------------


def test_a_test_release_builds_its_artifact_set_from_the_checkout_as_it_is(checkout: Checkout) -> None:
    out = checkout.tmp / "out"
    proc = checkout.run("build", "--test", "a", "--out", str(out), status=" M README.md\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"PASS: PubDex {TEST_VERSION} built and verified in {out}" in proc.stdout
    assert f"revision {COMMIT}-dirty, a test release" in proc.stdout
    archive, package = f"pubdex-{TEST_VERSION}-source.tar.gz", f"pubdex-{TEST_VERSION}.tgz"
    produced = [archive, package, "IMAGES", *_record_files(TEST_VERSION)]
    assert sorted(path.name for path in out.iterdir()) == sorted([*produced, "SHA256SUMS"])
    # The checksum file: coreutils format, sorted by name, every other file.
    lines = (out / "SHA256SUMS").read_text().splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == sorted(produced)
    for line in lines:
        digest, name = line.split("  ", 1)
        assert re.fullmatch(r"[0-9a-f]{64}", digest) and hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    # The images: local names only, the test release's version, through build_image.sh.
    assert checkout.calls("build_image") == [["--tag", f"pubdex-release:{TEST_VERSION}", "--postgres-tag",
                                              f"pubdex-postgres-release:{TEST_VERSION}", "--version", TEST_VERSION]]
    assert (out / "IMAGES").read_text() == (
        f"application pubdex-release:{TEST_VERSION} {checkout.image_id(f'pubdex-release:{TEST_VERSION}')}\n"
        f"postgres pubdex-postgres-release:{TEST_VERSION} "
        f"{checkout.image_id(f'pubdex-postgres-release:{TEST_VERSION}')}\n")
    # The chart package: the commit's time, the test release's version.
    packages = [call for call in checkout.helm_calls() if call["args"][0] == "package"]
    assert len(packages) == 1 and packages[0]["epoch"] == EPOCH
    assert packages[0]["args"][-4:] == ["--version", TEST_VERSION, "--app-version", TEST_VERSION]
    helm_events = checkout.helm_calls()
    allowed = {"PATH", "LC_ALL", "HOME", "TMPDIR", "KUBECONFIG", "DOCKER_CONFIG", "HELM_CONFIG_HOME",
               "HELM_CACHE_HOME", "HELM_DATA_HOME", "SOURCE_DATE_EPOCH", "STUB_STATE", "PWD", "SHLVL", "_"}
    for event in helm_events:
        assert set(event["environment"]) <= allowed, event["environment"]
        assert event["kubeconfig"].endswith("/no-kubeconfig"), "no kubeconfig of the caller's"
    assert [event["epoch"] for event in helm_events if event["args"][0] != "package"] == \
        [None] * (len(helm_events) - 1), "SOURCE_DATE_EPOCH for the package only"
    with tarfile.open(out / package) as chart:
        assert "version: 0.2.0-test.a" in chart.extractfile("pubdex/Chart.yaml").read().decode()
        assert chart.extractfile("pubdex/LICENSE") is not None
    # Nothing reached a registry, and nothing was written into the checkout.
    assert _registry_calls(checkout) == []
    assert not (checkout.root / "dist").exists()


def test_the_source_archive_holds_the_checkouts_files_reproducibly(checkout: Checkout) -> None:
    first = _build_test(checkout)
    archive = f"pubdex-{TEST_VERSION}-source.tar.gz"
    raw = (first / archive).read_bytes()
    assert raw[:2] == b"\x1f\x8b" and raw[4:8] == b"\0\0\0\0" and not raw[3] & 0x08, "gzip -n: no name, no time"
    with tarfile.open(first / archive) as source:
        members = source.getmembers()
    assert [member.name for member in members] == [f"pubdex-{TEST_VERSION}/{name}" for name in checkout.tracked]
    for member in members:
        assert member.isfile() and member.mtime == int(EPOCH) and (member.uid, member.gid) == (0, 0)
        assert member.uname == member.gname == ""
        executable = os.access(checkout.root / member.name.split("/", 1)[1], os.X_OK)
        assert member.mode == (0o755 if executable else 0o644), member.name
    # Other file times, the same bytes.
    for number, path in enumerate(sorted(checkout.root.rglob("*"))):
        os.utime(path, (1_000_000 + number, 1_000_000 + number))
    second = _build_test(checkout)
    assert (second / archive).read_bytes() == raw
    assert (second / f"pubdex-{TEST_VERSION}.tgz").read_bytes() == (first / f"pubdex-{TEST_VERSION}.tgz").read_bytes()


def test_a_test_release_takes_the_untracked_files_git_does_not_ignore(checkout: Checkout) -> None:
    checkout.write("notes.txt", "local notes\n")
    out = _build_test(checkout, status="?? notes.txt\n", untracked="notes.txt missing.txt")
    with tarfile.open(out / f"pubdex-{TEST_VERSION}-source.tar.gz") as source:
        names = source.getnames()
    assert f"pubdex-{TEST_VERSION}/notes.txt" in names
    assert f"pubdex-{TEST_VERSION}/missing.txt" not in names, "only files that exist"


def test_a_real_release_builds_from_the_clean_checkout_at_its_annotated_tag(checkout: Checkout) -> None:
    out = _build_release(checkout)
    assert sorted(path.name for path in out.iterdir()) == sorted(
        [f"pubdex-{VERSION}-source.tar.gz", f"pubdex-{VERSION}.tgz", "IMAGES", "SHA256SUMS", *_record_files(VERSION)])
    assert checkout.calls("build_image") == [["--tag", f"pubdex-release:{VERSION}", "--postgres-tag",
                                              f"pubdex-postgres-release:{VERSION}", "--no-cache"]]
    package = [call for call in checkout.calls("helm") if call[0] == "package"][0]
    assert "--version" not in package, "the chart's own version"
    git = checkout.calls("git")
    assert ["-C", str(checkout.root), "cat-file", "-t", "refs/tags/v0.2.0"] in git
    assert ["-C", str(checkout.root), "rev-parse", "--verify", "--quiet", "refs/tags/v0.2.0^{commit}"] in git
    # Questions only: nothing that writes the repository; its version for the
    # provenance record.
    for call in git:
        assert call == ["--version"] or call[2] in ("rev-parse", "--no-optional-locks", "ls-files", "cat-file",
                                                    "show"), call
    assert _registry_calls(checkout) == []


def test_a_real_release_builds_without_the_cache_and_a_test_release_never_does(checkout: Checkout) -> None:
    _build_release(checkout)
    _build_test(checkout)
    _build_overlay(checkout, "b1", "b1")
    release, test, overlay = checkout.calls("build_image")
    assert release.count("--no-cache") == 1, release
    assert "--no-cache" not in test, test
    assert "--no-cache" not in overlay, overlay


@pytest.mark.parametrize("settings, message", [
    ({"status": " M DB/people_pubs/__init__.py\n"}, "is built only from a Git checkout without changes"),
    ({"tag_type": "missing"}, "the annotated tag v0.2.0, which does not exist here"),
    ({"tag_type": "commit"}, "which is a lightweight tag here"),
    ({"tag_commit": OTHER_COMMIT}, f"the tag v0.2.0 names commit {OTHER_COMMIT}, not this checkout's commit"),
], ids=["changes", "no-tag", "lightweight-tag", "tag-elsewhere"])
def test_a_real_release_needs_a_clean_checkout_at_its_annotated_tag(checkout: Checkout, settings: dict,
                                                                    message: str) -> None:
    proc = checkout.run("build", **settings)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert checkout.calls("build_image") == [] and not (checkout.root / "dist").exists()
    assert not [call for call in checkout.calls("helm") if call[0] == "package"]


@pytest.mark.parametrize("version, args, message", [
    ("0.2.0-test.a", ["build", "--test", "b"], "a version reserved for test releases"),
    ("0.2.0-testing", ["build"], "a version reserved for test releases"),
    ("0.2.0-rc.1", ["build", "--test", "a"], "for a VERSION without a pre-release part"),
    ("0.2", ["build", "--test", "a"], "VERSION must hold a version"),
])
def test_versions_a_build_refuses(checkout: Checkout, version: str, args: List[str], message: str) -> None:
    checkout.write("VERSION", version + "\n")
    proc = checkout.run(*args)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert checkout.calls("build_image") == []


def test_the_chart_has_to_carry_the_version(checkout: Checkout) -> None:
    checkout.write("charts/pubdex/Chart.yaml", CHART_YAML.replace('appVersion: "0.2.0"', 'appVersion: "0.1.9"'))
    proc = checkout.run("build", "--test", "a")
    assert proc.returncode == 1
    assert "charts/pubdex/Chart.yaml has appVersion '0.1.9', not VERSION's 0.2.0" in proc.stderr
    assert checkout.calls("build_image") == []


def test_an_output_directory_in_the_way_is_refused(checkout: Checkout) -> None:
    taken = checkout.tmp / "taken"
    taken.mkdir()
    (taken / "old").write_text("x")
    proc = checkout.run("build", "--test", "a", "--out", str(taken))
    assert proc.returncode == 1 and "is not an empty directory" in proc.stderr
    inside = checkout.run("build", "--test", "a", "--out", str(checkout.root / "out"))
    assert inside.returncode == 1 and "lies in the checkout outside dist/" in inside.stderr
    assert checkout.calls("build_image") == []
    empty = checkout.tmp / "empty"
    empty.mkdir()
    assert checkout.run("build", "--test", "a", "--out", str(empty)).returncode == 0
    assert (empty / "SHA256SUMS").is_file()


def test_a_failed_build_leaves_no_output_directory(checkout: Checkout) -> None:
    out = checkout.tmp / "out"
    proc = checkout.run("build", "--test", "a", "--out", str(out), mode="build-fails")
    assert proc.returncode == 1 and "build_image.sh failed" in proc.stderr
    assert not out.exists()


def test_the_pinned_helm_and_the_buildx_plugin_are_required(checkout: Checkout) -> None:
    proc = checkout.run("build", "--test", "a", mode="no-buildx")
    assert proc.returncode == 1 and "buildx plugin was not found" in proc.stderr
    helm = checkout.root / ".tools" / "helm-4.3.1" / "helm"
    helm.write_text(helm.read_text() + "# changed\n")
    proc = checkout.run("build", "--test", "a")
    assert proc.returncode == 1 and "not the pinned" in proc.stderr and "./setup_cluster_tools.sh" in proc.stderr
    assert checkout.calls("build_image") == []


# --- the SBOM tool, the SBOMs and the provenance record ---------------------------


def _nothing_built(checkout: Checkout) -> None:
    assert checkout.calls("build_image") == []
    assert not [call for call in checkout.helm_calls() if call["args"][0] == "package"]
    assert not [call for call in checkout.calls("docker") if call[:2] == ["image", "save"]]
    assert not [call for call in checkout.calls("docker") if call[:1] == ["pull"]], "build never pulls the tool"


def test_a_missing_sbom_tool_is_refused_before_anything_is_built(checkout: Checkout) -> None:
    shutil.rmtree(checkout.image(SYFT_IMAGE))
    proc = checkout.run("build", "--test", "a", "--out", str(checkout.tmp / "out"))
    assert proc.returncode == 1
    assert "the pinned SBOM tool, Syft 1.57.2, is not on this machine" in proc.stderr
    assert "./release.sh setup" in proc.stderr
    _nothing_built(checkout)
    assert not (checkout.tmp / "out").exists()


@pytest.mark.parametrize("image_id, mode, message", [
    ("sha256:" + "9" * 64, "", "is not the pinned SBOM tool"),
    (SYFT_ID, "syft-old", "reports the version '1.57.1', not the pinned 1.57.2"),
], ids=["other-image", "other-version"])
def test_an_altered_sbom_tool_is_refused_before_anything_is_built(checkout: Checkout, image_id: str, mode: str,
                                                                   message: str) -> None:
    checkout.set_image(SYFT_IMAGE, image_id)
    proc = checkout.run("build", "--test", "a", mode=mode)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    _nothing_built(checkout)


def test_the_index_digest_is_the_tools_id_in_the_containerd_image_store(checkout: Checkout) -> None:
    checkout.set_image(SYFT_IMAGE, SBOM_PINS["PINNED_SYFT_INDEX_DIGEST"])
    _build_test(checkout)


def test_setup_pulls_the_pinned_image_by_digest_and_checks_it(checkout: Checkout) -> None:
    shutil.rmtree(checkout.image(SYFT_IMAGE))
    proc = checkout.run("setup")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"PASS: the pinned SBOM tool is in place: {SYFT_IMAGE}, image {SYFT_ID}." in proc.stdout
    assert [call for call in checkout.calls("docker") if call[0] == "pull"] == [["pull", SYFT_IMAGE]]
    assert [call for call in checkout.calls("docker") if call[0] in ("push", "login", "tag")] == []
    assert "@sha256:" in SYFT_IMAGE and SYFT_IMAGE.startswith("docker.io/anchore/syft:v1.57.2@")


def test_setup_refuses_an_image_that_is_not_the_pinned_one(checkout: Checkout) -> None:
    shutil.rmtree(checkout.image(SYFT_IMAGE))
    proc = checkout.run("setup", mode="pull-other")
    assert proc.returncode == 1 and "is not the pinned SBOM tool" in proc.stderr


def _runs(checkout: Checkout) -> List[List[str]]:
    return [call[1:] for call in checkout.calls("docker") if call[0] == "run"]


def test_the_sboms_come_from_the_pinned_tool_offline_on_the_saved_images(checkout: Checkout) -> None:
    out = _build_test(checkout)
    syft = [run for run in _runs(checkout) if SYFT_IMAGE in run]
    scans = [run for run in syft if any(word.startswith("docker-archive:") for word in run)]
    assert len(scans) == 2 * 1 and len(syft) == 3, "the version check, then one run per image"
    for run, role, name in zip(scans, ("application", "postgres"), _record_files(TEST_VERSION)):
        options = " ".join(run[:run.index(SYFT_IMAGE)])
        for option in ("--rm --pull never --network none --read-only", "--tmpfs /tmp:rw,mode=1777",
                       f"--user {os.getuid()}:{os.getgid()}", "--cap-drop ALL", "--security-opt no-new-privileges",
                       "--env SYFT_CHECK_FOR_APP_UPDATE=false", ",target=/sbom"):
            assert option in options, (option, options)
        command = run[run.index(SYFT_IMAGE) + 1:]
        repository = "pubdex-release" if role == "application" else "pubdex-postgres-release"
        assert command == [f"docker-archive:/sbom/{role}.tar", "--source-name", repository, "--source-version",
                           TEST_VERSION, "--quiet", "--output", f"spdx-json@2.3=/sbom/{name}"]
        assert json.loads((out / name).read_text())["spdxVersion"] == "SPDX-2.3"
    saves = [call for call in checkout.calls("docker") if call[:2] == ["image", "save"]]
    assert [call[-1] for call in saves] == [f"pubdex-release:{TEST_VERSION}", f"pubdex-postgres-release:{TEST_VERSION}"]
    # Every other container is a probe of a release image: no network, an
    # unprivileged user, a read-only root file system; no other image runs.
    for run in _runs(checkout):
        if SYFT_IMAGE in run:
            continue
        options = " ".join(run)
        assert "--rm --pull never --network none --read-only --cap-drop ALL" in options
        assert "--user 65534:65534" in options
        image = run[run.index("--entrypoint") + 2]
        assert image in (f"pubdex-release:{TEST_VERSION}", f"pubdex-postgres-release:{TEST_VERSION}")
    assert _registry_calls(checkout) == []


def test_the_provenance_record_is_written_from_the_build_and_covered_by_the_checksums(checkout: Checkout) -> None:
    out = _build_test(checkout, status=" M README.md\n")
    record = json.loads((out / f"pubdex-{TEST_VERSION}-provenance.json").read_text())
    assert record["format"] == "pubdex-provenance/2"
    assert record["release"]["version"] == TEST_VERSION and record["release"]["tag"] is None
    assert record["release"]["revision"] == COMMIT + "-dirty" and record["release"]["tree_changes"] is True
    assert record["release"]["source_date_epoch"] == int(EPOCH)
    assert record["images"]["application"]["id"] == checkout.image_id(f"pubdex-release:{TEST_VERSION}")
    assert record["images"]["postgres"]["postgresql"] == "16.16"
    assert record["build"]["sbom_tool"] == {"name": "syft", "version": "1.57.2", "image": SYFT_IMAGE,
                                            "image_id": SYFT_ID}
    assert sorted(record["files"]) == sorted([f"pubdex-{TEST_VERSION}-source.tar.gz", f"pubdex-{TEST_VERSION}.tgz",
                                              "IMAGES", *_record_files(TEST_VERSION)[:2]])
    assert f"pubdex-{TEST_VERSION}-provenance.json" in (out / "SHA256SUMS").read_text()
    text = (out / f"pubdex-{TEST_VERSION}-provenance.json").read_text()
    assert str(checkout.tmp) not in text and "192.0.2.17" not in text, "nothing of the machine that built it"


def test_a_build_whose_images_lack_their_build_packages_fails(checkout: Checkout) -> None:
    proc = checkout.run("build", "--test", "a", "--out", str(checkout.tmp / "out"), mode="no-build-packages")
    assert proc.returncode == 1 and "could not read a fact for the provenance record" in proc.stderr
    assert not (checkout.tmp / "out").exists()


def test_no_step_scans_for_vulnerabilities() -> None:
    code = "\n".join(line for line in SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#"))
    for scanner in ("trivy", "grype", "vulnerab", "cve", "syft scan", "attest"):
        assert scanner not in code.lower(), scanner


# --- verify ---------------------------------------------------------------------


def _tamper_checksummed(out: Path) -> None:
    archive = next(out.glob("*-source.tar.gz"))
    archive.write_bytes(archive.read_bytes() + b"\0")


def _tamper_extra(out: Path) -> None:
    (out / "extra.txt").write_text("x")


def _tamper_unlisted(out: Path) -> None:
    lines = (out / "SHA256SUMS").read_text().splitlines()
    (out / "SHA256SUMS").write_text("\n".join(line for line in lines if not line.endswith("IMAGES")) + "\n")


def _rechecksum(out: Path) -> None:
    names = sorted(path.name for path in out.iterdir() if path.name not in ("SHA256SUMS", "DIGESTS"))
    (out / "SHA256SUMS").write_text("".join(f"{hashlib.sha256((out / name).read_bytes()).hexdigest()}  {name}\n"
                                            for name in names))


def _tamper_archive_content(out: Path) -> None:
    # A consistent checksum file, but the archive is not the checkout's.
    archive = next(out.glob("*-source.tar.gz"))
    with tarfile.open(archive) as source:
        members = [(member, source.extractfile(member).read()) for member in source.getmembers()]
    data = __import__("io").BytesIO()
    with tarfile.open(fileobj=data, mode="w") as target:
        for member, content in members:
            if member.name.endswith("README.md"):
                content = b"# Changed\n"
                member.size = len(content)
            target.addfile(member, __import__("io").BytesIO(content))
    archive.write_bytes(gzip.compress(data.getvalue()))
    _rechecksum(out)


@pytest.mark.parametrize("tamper, message", [
    (_tamper_checksummed, "does not match its SHA-256"),
    (_tamper_extra, "but the directory holds"),
    (_tamper_unlisted, "but the directory holds"),
    (_tamper_archive_content, "files differ from the checkout's"),
], ids=["hash", "extra-file", "unlisted-file", "archive-content"])
def test_verify_refuses_a_directory_that_is_not_what_build_wrote(checkout: Checkout, tamper, message: str) -> None:
    out = _build_test(checkout)
    tamper(out)
    checkout.reset_calls()
    proc = checkout.run("verify", str(out))
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert _registry_calls(checkout) == [] and checkout.calls("build_image") == []


def test_verify_refuses_a_moved_tag_another_revision_and_a_changed_checkout(checkout: Checkout) -> None:
    out = _build_test(checkout)
    assert checkout.run("verify", str(out)).returncode == 0
    # The checkout changed since the build: the labels no longer name its revision.
    proc = checkout.run("verify", str(out), status=" M README.md\n")
    assert proc.returncode == 1 and "but the checkout is at " + COMMIT + "-dirty" in proc.stderr
    # A file of the checkout changed: the archive no longer holds it.
    checkout.write("README.md", "# Changed\n")
    proc = checkout.run("verify", str(out))
    assert proc.returncode == 1 and "differs: README.md" in proc.stderr
    checkout.write("README.md", "# PubDex\n")
    # The local tag moved to another image.
    (checkout.image(f"pubdex-release:{TEST_VERSION}") / "id").write_text("sha256:" + "f" * 64 + "\n")
    proc = checkout.run("verify", str(out))
    assert proc.returncode == 1 and "the tag has moved since the build" in proc.stderr


def test_verify_refuses_a_chart_package_of_another_version(checkout: Checkout) -> None:
    out = _build_test(checkout)
    other = _build_test(checkout, name="b")
    shutil.copy2(other / f"pubdex-0.2.0-test.b.tgz", out / f"pubdex-{TEST_VERSION}.tgz")
    _rechecksum(out)
    proc = checkout.run("verify", str(out))
    assert proc.returncode == 1 and f"has version '0.2.0-test.b', not {TEST_VERSION}" in proc.stderr


def test_verify_refuses_a_failing_lint(checkout: Checkout) -> None:
    out = _build_test(checkout)
    proc = checkout.run("verify", str(out), mode="lint-fails")
    assert proc.returncode == 1 and "helm lint --strict fails" in proc.stderr


def _tamper_record(out: Path) -> None:
    # A consistent checksum file, but the record names another base image.
    path = next(out.glob("*-provenance.json"))
    record = json.loads(path.read_text())
    record["inputs"]["base_images"]["DEBIAN_BASE"] = "debian:trixie-slim@sha256:" + "0" * 64
    path.write_text(json.dumps(record, indent=2) + "\n")
    _rechecksum(out)


def _drop_sbom(out: Path) -> None:
    next(out.glob("*-postgres-image.spdx.json")).unlink()
    _rechecksum(out)


def _tamper_sbom(out: Path) -> None:
    path = out / f"pubdex-{TEST_VERSION}-image.spdx.json"
    document = json.loads(path.read_text())
    document["packages"] = [package for package in document["packages"] if package["name"] != "psycopg-c"]
    path.write_text(json.dumps(document))
    _rechecksum(out)


@pytest.mark.parametrize("tamper, mode, message", [
    (_tamper_record, "", "inputs.base_images.DEBIAN_BASE: recorded"),
    (_drop_sbom, "", "has no SBOM pubdex-0.2.0-test.a-postgres-image.spdx.json"),
    (_tamper_sbom, "", "names the Python package psycopg-c as nothing"),
    (None, "old-postgresql", "the image reports 16.14"),
], ids=["record", "sbom-missing", "sbom-package", "image-postgresql"])
def test_verify_refuses_sboms_or_a_record_that_disagree(checkout: Checkout, tamper, mode: str, message: str) -> None:
    out = _build_test(checkout)
    if tamper:
        tamper(out)
    checkout.reset_calls()
    proc = checkout.run("verify", str(out), mode=mode)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert _registry_calls(checkout) == [] and checkout.calls("build_image") == []
    assert not [run for run in _runs(checkout) if SYFT_IMAGE in run], "verify never runs the SBOM tool"


# --- publish --------------------------------------------------------------------


def _pushes(checkout: Checkout) -> List[str]:
    return [call[1] for call in checkout.calls("docker") if call[0] == "push"]


def test_publish_refuses_a_test_release_before_anything_else(checkout: Checkout) -> None:
    out = _build_test(checkout)
    checkout.reset_calls()
    proc = checkout.run("publish", str(out), "--repository-prefix", PREFIX)
    assert proc.returncode == 1 and "a test release is never published" in proc.stderr
    assert checkout.events() == []


def test_publish_pushes_the_postgresql_image_first_and_records_both_digests(checkout: Checkout) -> None:
    out = _build_release(checkout)
    checkout.reset_calls()
    proc = checkout.run("publish", str(out), "--repository-prefix", PREFIX)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    app, postgres = f"{PREFIX}/pubdex:{VERSION}", f"{PREFIX}/pubdex-postgres:{VERSION}"
    assert _registry_calls(checkout) == [
        ["manifest", "inspect", postgres], ["manifest", "inspect", app],
        ["tag", f"pubdex-postgres-release:{VERSION}", postgres], ["tag", f"pubdex-release:{VERSION}", app],
        ["push", postgres], ["push", app],
    ]
    digest = lambda ref: "sha256:" + hashlib.sha256(ref.encode()).hexdigest()
    assert (out / "DIGESTS").read_text() == f"{app}@{digest(app)}\n{postgres}@{digest(postgres)}\n"
    assert f"published {postgres}@{digest(postgres)}" in proc.stdout and f"published {app}@{digest(app)}" in proc.stdout
    assert proc.stdout.index(f"Pushing {postgres}") < proc.stdout.index(f"Pushing {app}")
    # Verify still passes on the published directory: DIGESTS is outside SHA256SUMS.
    assert checkout.run("verify", str(out)).returncode == 0
    # A second publish refuses: the directory is published.
    checkout.reset_calls()
    again = checkout.run("publish", str(out), "--repository-prefix", PREFIX)
    assert again.returncode == 1 and "it has been published already" in again.stderr
    assert _registry_calls(checkout) == []


@pytest.mark.parametrize("answers, message", [
    ({f"{PREFIX}/pubdex:{VERSION}": "exists"}, f"{PREFIX}/pubdex:{VERSION} already exists"),
    ({f"{PREFIX}/pubdex-postgres:{VERSION}": "exists"}, f"{PREFIX}/pubdex-postgres:{VERSION} already exists"),
    ({f"{PREFIX}/pubdex-postgres:{VERSION}": "denied"}, "its absence is not established"),
    ({f"{PREFIX}/pubdex:{VERSION}": "denied-unknown"}, "its absence is not established"),
    ({f"{PREFIX}/pubdex:{VERSION}": "name-unknown"}, "its absence is not established"),
    ({f"{PREFIX}/pubdex:{VERSION}": "odd"}, "did not explicitly report"),
], ids=["application-exists", "postgres-exists", "denied", "denied-with-unknown", "repository-unknown", "odd"])
def test_publish_requires_the_registrys_explicit_manifest_unknown(checkout: Checkout, answers: Dict[str, str],
                                                                  message: str) -> None:
    out = _build_release(checkout)
    checkout.reset_calls()
    proc = checkout.run("publish", str(out), "--repository-prefix", PREFIX, manifest=answers)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert _pushes(checkout) == [] and not [call for call in checkout.calls("docker") if call[0] == "tag"]
    assert not (out / "DIGESTS").exists()


@pytest.mark.parametrize("settings, message", [
    ({"status": " M README.md\n"}, "is built only from a Git checkout without changes"),
    ({"tag_commit": OTHER_COMMIT}, f"the tag v0.2.0 names commit {OTHER_COMMIT}"),
    ({"tag_type": "missing"}, "which does not exist here"),
], ids=["changes", "tag-elsewhere", "no-tag"])
def test_publish_refuses_a_checkout_that_is_not_the_tagged_commit(checkout: Checkout, settings: dict,
                                                                   message: str) -> None:
    out = _build_release(checkout)
    checkout.reset_calls()
    proc = checkout.run("publish", str(out), "--repository-prefix", PREFIX, **settings)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert _registry_calls(checkout) == []


def test_publish_refuses_an_unverified_directory(checkout: Checkout) -> None:
    out = _build_release(checkout)
    _tamper_checksummed(out)
    checkout.reset_calls()
    proc = checkout.run("publish", str(out), "--repository-prefix", PREFIX)
    assert proc.returncode == 1 and "does not match its SHA-256" in proc.stderr
    assert _registry_calls(checkout) == []


def test_publish_never_moves_a_local_tag(checkout: Checkout) -> None:
    out = _build_release(checkout)
    checkout.set_image(f"{PREFIX}/pubdex:{VERSION}", "sha256:" + "e" * 64)
    checkout.reset_calls()
    proc = checkout.run("publish", str(out), "--repository-prefix", PREFIX)
    assert proc.returncode == 1 and "it is never moved" in proc.stderr
    assert _pushes(checkout) == []


def test_a_failed_push_stops_before_the_application_image(checkout: Checkout) -> None:
    out = _build_release(checkout)
    checkout.reset_calls()
    proc = checkout.run("publish", str(out), "--repository-prefix", PREFIX,
                        mode=f"push-fails:{PREFIX}/pubdex-postgres:{VERSION}")
    assert proc.returncode == 1 and f"docker push {PREFIX}/pubdex-postgres:{VERSION} failed" in proc.stderr
    assert _pushes(checkout) == [f"{PREFIX}/pubdex-postgres:{VERSION}"]
    assert not (out / "DIGESTS").exists()


APP_REF, POSTGRES_REF = f"{PREFIX}/pubdex:{VERSION}", f"{PREFIX}/pubdex-postgres:{VERSION}"
#: What the stubbed registry holds under a reference it answers "exists" for.
OTHER_DIGEST = "sha256:" + "f" * 64


def _digest(reference: str) -> str:
    """The digest the stubbed docker push reports for a reference."""
    return "sha256:" + hashlib.sha256(reference.encode()).hexdigest()


def _publish(checkout: Checkout, out: Path, **kwargs) -> subprocess.CompletedProcess:
    return checkout.run("publish", str(out), "--repository-prefix", PREFIX, **kwargs)


def test_a_second_publish_completes_one_whose_application_push_failed(checkout: Checkout) -> None:
    out = _build_release(checkout)
    checkout.reset_calls()
    first = _publish(checkout, out, mode=f"push-fails:{APP_REF}")
    assert first.returncode == 1 and f"docker push {APP_REF} failed" in first.stderr
    assert _pushes(checkout) == [POSTGRES_REF, APP_REF] and not (out / "DIGESTS").exists()
    checkout.reset_calls()
    again = _publish(checkout, out)
    assert again.returncode == 0, again.stdout + again.stderr
    assert _registry_calls(checkout) == [
        ["manifest", "inspect", POSTGRES_REF], ["buildx", "imagetools", "inspect", POSTGRES_REF],
        ["manifest", "inspect", APP_REF], ["push", APP_REF],
    ]
    assert f"the registry holds {_digest(POSTGRES_REF)}, which the verified image records" in again.stdout
    assert "it is not pushed again" in again.stdout
    assert (out / "DIGESTS").read_text() == \
        f"{APP_REF}@{_digest(APP_REF)}\n{POSTGRES_REF}@{_digest(POSTGRES_REF)}\n"


def test_completion_write_failure_cleans_up_and_retry_does_not_push(checkout: Checkout) -> None:
    out = _build_release(checkout)
    real_mktemp = shutil.which("mktemp")
    _executable(checkout.bin / "mktemp", f'''#!/bin/bash
case "$1" in
  */.DIGESTS.XXXXXX)
    temporary="${{1%XXXXXX}}failed"
    ln -s /dev/full "$temporary" || exit
    printf '%s\\n' "$temporary" ;;
  *) exec "{real_mktemp}" "$@" ;;
esac
''')
    failed = checkout.run("publish", str(out), "--repository-prefix", PREFIX)
    assert failed.returncode == 1 and "cannot write the completion record" in failed.stderr, failed.stderr
    assert _pushes(checkout) == [POSTGRES_REF, APP_REF]
    assert not (out / "DIGESTS").exists() and not list(out.glob(".DIGESTS.*"))
    (checkout.bin / "mktemp").unlink()
    checkout.reset_calls()
    retry = checkout.run("publish", str(out), "--repository-prefix", PREFIX)
    assert retry.returncode == 0, retry.stderr
    assert _pushes(checkout) == [] and retry.stdout.count("it is not pushed again") == 2
    assert (out / "DIGESTS").read_text() == f"{APP_REF}@{_digest(APP_REF)}\n{POSTGRES_REF}@{_digest(POSTGRES_REF)}\n"
    assert not list(out.glob(".DIGESTS.*"))


def test_a_second_publish_of_a_complete_publication_pushes_nothing_and_writes_the_digests(
        checkout: Checkout) -> None:
    out = _build_release(checkout)
    assert _publish(checkout, out).returncode == 0
    digests = (out / "DIGESTS").read_text()
    # Interrupted after both pushes, before DIGESTS was written.
    (out / "DIGESTS").unlink()
    checkout.reset_calls()
    again = _publish(checkout, out)
    assert again.returncode == 0, again.stdout + again.stderr
    assert _pushes(checkout) == [] and not [call for call in checkout.calls("docker") if call[0] == "tag"]
    assert again.stdout.count("it is not pushed again") == 2
    assert (out / "DIGESTS").read_text() == digests


def test_publish_refuses_a_version_the_registry_holds_as_another_image(checkout: Checkout) -> None:
    out = _build_release(checkout)
    assert _publish(checkout, out, mode=f"push-fails:{APP_REF}").returncode == 1
    checkout.reset_calls()
    proc = _publish(checkout, out, manifest={POSTGRES_REF: "exists"})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"{POSTGRES_REF} already exists, as {OTHER_DIGEST}, which is another image" in proc.stderr
    assert f"records {_digest(POSTGRES_REF)} for {PREFIX}/pubdex-postgres" in proc.stderr
    assert f"Version {VERSION} is spent" in proc.stderr
    assert _pushes(checkout) == [] and not (out / "DIGESTS").exists()


def test_publish_refuses_a_version_whose_image_records_no_digest_for_it(checkout: Checkout) -> None:
    out = _build_release(checkout)
    first = _publish(checkout, out, mode=f"push-fails-after-manifest:{POSTGRES_REF}")
    assert first.returncode == 1 and f"docker push {POSTGRES_REF} failed" in first.stderr
    checkout.reset_calls()
    proc = _publish(checkout, out)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"{POSTGRES_REF} already exists, as {_digest(POSTGRES_REF)}, and the verified image " \
           f"pubdex-postgres-release:{VERSION} records no digest for {PREFIX}/pubdex-postgres" in proc.stderr
    assert f"Version {VERSION} is spent" in proc.stderr
    assert _pushes(checkout) == [] and not (out / "DIGESTS").exists()


def test_publish_stops_when_the_registrys_digest_cannot_be_read(checkout: Checkout) -> None:
    out = _build_release(checkout)
    checkout.reset_calls()
    proc = _publish(checkout, out, manifest={APP_REF: "exists"}, mode="imagetools-fails")
    assert proc.returncode == 1 and f"the registry's digest of {APP_REF} could not be read" in proc.stderr
    assert "is spent" not in proc.stderr
    assert _pushes(checkout) == [] and not [call for call in checkout.calls("docker") if call[0] == "tag"]


def test_no_step_logs_in_or_pushes_a_chart() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "docker login" not in code and "helm push" not in code and "helm registry" not in code
    assert code.count('docker push "') == 1 and "docker manifest inspect" in code
    assert "github.com" not in text and "gh " not in code


# --- the test-only overlay --------------------------------------------------------


def _build_overlay(checkout: Checkout, name: str, overlay: str, **kwargs) -> Path:
    out = checkout.tmp / f"out-{name}"
    proc = checkout.run("build", "--test", name, "--overlay", overlay, "--out", str(out), **kwargs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return out


def _members(archive: Path) -> Dict[str, bytes]:
    """The source archive's files by their path in the tree."""
    with tarfile.open(archive) as source:
        return {member.name.split("/", 1)[1]: source.extractfile(member).read() for member in source.getmembers()}


def _tree(root: Path) -> Dict[str, object]:
    """Each file's content and each path's mode under root, the pinned tools aside."""
    return {path.relative_to(root).as_posix(): (path.read_bytes() if path.is_file() else None, path.stat().st_mode)
            for path in root.rglob("*") if ".tools" not in path.parts}


@pytest.mark.parametrize("overlay, added", [
    ("b1", ["0002_test_overlay_compatible.sql"]),
    ("b2", ["0002_test_overlay_compatible.sql", "0003_test_overlay_incompatible.sql"]),
])
def test_an_overlay_build_is_a_copy_of_the_checkout_with_the_overlay_applied(checkout: Checkout, overlay: str,
                                                                              added: List[str]) -> None:
    before = _tree(checkout.root)
    out = _build_overlay(checkout, overlay, overlay)
    version = f"{VERSION}-test.{overlay}"
    # The source archive: the checkout's files, the overlay's schema changes
    # numbered after the last one, and the new transform version.
    members = _members(out / f"pubdex-{version}-source.tar.gz")
    assert sorted(members) == sorted([*checkout.tracked, *(f"DB/schema/{name}" for name in added)])
    for name, template in zip(added, ("compatible.sql", "incompatible.sql")):
        assert members[f"DB/schema/{name}"] == (OVERLAY / template).read_bytes()
    assert members["DB/people_pubs/__init__.py"] == b'__version__ = "1.5.4+test"\n'
    # The images: the copy's build_image.sh, given the revision, finding no
    # Git checkout above the temporary directory.
    [call] = checkout.calls("build_image")
    assert call == ["--tag", f"pubdex-release:{version}", "--postgres-tag", f"pubdex-postgres-release:{version}",
                    "--version", version, "--revision", f"{COMMIT}-dirty"]
    [build] = [json.loads(line) for line in (checkout.state / "build_roots.jsonl").read_text().splitlines()]
    copy = Path(build["root"])
    assert checkout.root not in copy.parents and Path(build["ceiling"]) in copy.parents
    assert build["schema"] == ["0001_schema_change_record.sql", *added]
    # Every path the copy shares with the checkout has the checkout's mode,
    # directories included, so BuildKit's cache serves what is unchanged.
    modes = {path: oct(mode) for path, (_content, mode) in before.items()}
    modes["."] = oct(checkout.root.stat().st_mode)
    shared = {path: mode for path, mode in build["modes"].items() if path in modes}
    assert shared == {path: modes[path] for path in shared} and len(shared) == len(modes)
    for name in added:
        assert build["modes"][f"DB/schema/{name}"] == modes["DB/schema/0001_schema_change_record.sql"]
    image = checkout.image(f"pubdex-release:{version}")
    assert (image / "label.pubdex.transform-version").read_text() == "1.5.4+test\n"
    assert (image / "label.org.opencontainers.image.revision").read_text() == f"{COMMIT}-dirty\n"
    # The provenance record describes the copy.
    release = json.loads((out / f"pubdex-{version}-provenance.json").read_text())["release"]
    assert release["transform_version"] == "1.5.4+test" and release["revision"] == f"{COMMIT}-dirty"
    assert [change["name"] for change in release["schema_changes"]] == [
        "0001_schema_change_record", *(name[:-len(".sql")] for name in added)]
    # The checkout is untouched, and the copy is gone with the temporary directory.
    assert _tree(checkout.root) == before
    assert not copy.exists()
    assert _registry_calls(checkout) == []


def test_an_overlay_build_takes_the_checkouts_uncommitted_and_untracked_files(checkout: Checkout) -> None:
    checkout.write("notes.txt", "local notes\n")
    checkout.write("README.md", "# PubDex, changed\n")
    out = _build_overlay(checkout, "b1", "b1", status=" M README.md\n?? notes.txt\n", untracked="notes.txt")
    members = _members(out / f"pubdex-{VERSION}-test.b1-source.tar.gz")
    assert members["notes.txt"] == b"local notes\n" and members["README.md"] == b"# PubDex, changed\n"


def test_verify_checks_an_overlay_build_against_the_same_copy(checkout: Checkout) -> None:
    out = _build_overlay(checkout, "b2", "b2")
    checkout.reset_calls()
    proc = checkout.run("verify", str(out), "--overlay", "b2")
    assert proc.returncode == 0 and f"PASS: {out} verified" in proc.stdout, proc.stdout + proc.stderr
    assert checkout.calls("build_image") == []
    # Without the overlay the images' revision is not the checkout's; with
    # the other one the archive holds other files.
    for args, message in (([], f"is labelled revision '{COMMIT}-dirty', but the checkout is at {COMMIT}"),
                          (["--overlay", "b1"], "does not hold exactly the checkout's files")):
        proc = checkout.run("verify", str(out), *args)
        assert proc.returncode == 1 and message in proc.stderr, (args, proc.stderr)
    # A PubDex release is never built or checked with the overlay.
    release = _build_release(checkout)
    proc = checkout.run("verify", str(release), "--overlay", "b1")
    assert proc.returncode == 1 and "only a test release is built with the test-only overlay" in proc.stderr


@pytest.mark.parametrize("change, message", [
    (lambda root: (root / "DB/tests/release_overlay/incompatible.sql").unlink(),
     "the test-only overlay lacks DB/tests/release_overlay/incompatible.sql"),
    (lambda root: (root / "DB/tests/release_overlay/transform-version-suffix").write_text("test\n"),
     "has to hold one suffix such as +test"),
    (lambda root: (root / "DB/schema/0001_schema_change_record.sql").rename(root / "DB/schema/notes.txt"),
     "no schema change under DB/schema/ to number the overlay's after"),
], ids=["template missing", "suffix", "no schema change"])
def test_an_overlay_that_cannot_be_applied_stops_before_anything_is_built(checkout: Checkout, change,
                                                                         message: str) -> None:
    change(checkout.root)
    checkout.tracked = sorted(path.relative_to(checkout.root).as_posix() for path in checkout.root.rglob("*")
                              if path.is_file() and ".tools" not in path.parts)
    proc = checkout.run("build", "--test", "b2", "--overlay", "b2", "--out", str(checkout.tmp / "out"))
    assert proc.returncode == 1 and message in proc.stderr, proc.stdout + proc.stderr
    assert checkout.calls("build_image") == [] and not (checkout.tmp / "out").exists()


def test_the_overlays_schema_changes_follow_the_rules_of_a_schema_change(tmp_path: Path) -> None:
    from people_pubs import schema

    for number, (template, compatible) in enumerate((("compatible.sql", True), ("incompatible.sql", False)), 2):
        path = tmp_path / f"{number:04d}_test_overlay_{template}"
        shutil.copy2(OVERLAY / template, path)
        change = schema.read_change(path)
        assert change.backward_compatible is compatible
        findings = schema.compatibility_findings(change.text)
        # The backward-compatible change only adds; the other renames a serving column.
        assert findings == [] if compatible else findings and all("renames" in finding for finding in findings)
    assert (OVERLAY / "transform-version-suffix").read_text() == "+test\n"
    assert sorted(path.name for path in OVERLAY.iterdir() if path.is_file()) == sorted(OVERLAY_FILES)
