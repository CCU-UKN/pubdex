#!/usr/bin/env python3
"""The provenance record and the SBOMs of a PubDex release, written and checked.

    python3 DB/release_provenance.py write --dir <directory> --facts <directory> IDENTITY
                                           --started <time> --finished <time>
    python3 DB/release_provenance.py check --dir <directory> --facts <directory> IDENTITY

    IDENTITY: --version <version> --commit <commit> --revision <revision> --epoch <seconds>

release.sh build calls write once the release's directory holds the source
archive, the chart package, IMAGES and both SBOMs; write adds the provenance
record pubdex-<version>-provenance.json. release.sh verify calls check. This
script runs neither Docker nor Git and uses no network: release.sh passes the
checkout's identity -- the version, the commit, the revision its images'
labels record and the commit's time (SOURCE_DATE_EPOCH) -- and gathers into
the facts directory what only Docker and the images can tell (FACTS below).
The checkout's own files it reads itself, under --root, by default the
repository this file belongs to.

The provenance record, format "pubdex-provenance/2", is plain JSON. Version 2
adds images.<role>.config_digest, the SHA-256 of saved image config bytes,
independent of the builder's image store. Version 1 records remain accepted
by check with their original fields; no old record is rewritten:

  release   the version, whether it is a test release, the commit, the
            revision, whether the tree had changes, the tag (v<version>, or
            null for a test release), SOURCE_DATE_EPOCH, the transform
            version and the schema changes the release ships (number, name,
            SHA-256), as releases/README.md records them;
  build     the start and end of the build (UTC); the platform, Docker
            Engine, buildx, BuildKit, the builder's driver and the image
            store; Git, tar, gzip and the Python that wrote the record; helm
            (version and SHA-256) and the SBOM tool (version, image reference
            and image id);
  inputs    the SHA-256 of both Dockerfiles, of the .dockerignore file each
            build context uses, of the dependency locks and of the scripts
            the builds run -- the files of the application image's stage
            build-files, build_image.sh and DB/lib/release_identity.sh; the
            base images, every ARG of the Dockerfiles that names an image by
            digest; PostgreSQL's source (version, URL, SHA-256) and the
            entry point's licence (file and SHA-256);
  images    for each image (application, postgres): the local tag and image
            id IMAGES records, the config digest, the platform, every label, and the packages of
            the stage that compiled its C code -- psycopg-c, PostgreSQL --
            with their versions, from build-packages.txt in the image; the
            application image's client libraries (psycopg, psycopg-c and
            psycopg-pool, psycopg's implementation, the libpq it was built
            with and loads, Debian's libpq5), the PostgreSQL image's
            PostgreSQL release, as the image itself reports them;
  files     the SHA-256 of every other file of the directory: the source
            archive, the chart package, IMAGES and both SBOMs.

Every value comes from the checkout's files, the built images or the tools
themselves; none is kept by hand. The record names no path of the machine
that built it. The registry digests of a published release are not in it:
publish writes them to DIGESTS, next to it.

The SBOMs, pubdex-<version>-image.spdx.json and
pubdex-<version>-postgres-image.spdx.json, are SPDX 2.3 JSON documents that
the pinned Syft wrote (DB/lib/sbom_tool.sh).

check passes when both SBOMs and the record parse and agree with the
checkout, the directory and the images:
  - each SBOM is SPDX 2.3 JSON, made by the pinned Syft, describing one
    container image named as IMAGES names the image, of the version; the
    application image's names every package of DB/requirements.txt in its
    pinned version and Debian's libpq5 in the version the image holds; the
    PostgreSQL image's names PostgreSQL in the version postgres.Dockerfile
    builds, which the image reports;
  - the record has exactly the fields above; release equals the identity
    passed and the checkout's transform version and schema changes; inputs
    equal what the checkout's files give now; images equal the facts of the
    images now and IMAGES; the labels name the version, the revision, the
    base image each Dockerfile builds on, its digest, the transform version
    and the PostgreSQL release the checkout gives; the client libraries are
    the runtime lock's versions with psycopg's C implementation; helm and
    the SBOM tool are the pinned ones; files equals the directory's files
    and their SHA-256.
The facts of the machine that built it -- the tools' versions, the times --
are checked for their form only.

FACTS: the files release.sh writes into the facts directory. For each role,
application and postgres: <role>.config-digest (saved config SHA-256),
<role>.platform (os/architecture), <role>.labels.json
(the image's labels as Docker prints them in JSON), <role>.build-packages.txt
(build-packages.txt from the image); application.client.json (what the
probe in release.sh prints inside the image) and application.libpq5.txt
(libpq5's version in the image); postgres.version.txt (postgres --version
inside the image). For write also: builder.docker.txt (Engine version and
platform), builder.buildx.txt, builder.buildkit.txt (docker buildx inspect),
builder.store.txt (storage driver and driver type), builder.git.txt,
builder.tar.txt, builder.gzip.txt, builder.helm.txt (reported version and
SHA-256) and builder.syft.txt (the SBOM tool's image id, then its version
output).

Exit status: 0 on success, 1 when a file or fact is missing, malformed or
disagrees, 2 for a usage error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "pubdex-provenance/2"
LEGACY_FORMAT = "pubdex-provenance/1"
ROLES = ("application", "postgres")
DOCKERFILES = {"application": "Dockerfile", "postgres": "postgres.Dockerfile"}
IDENTITY_FILES = ("build_image.sh", "DB/lib/release_identity.sh")
BUILD_FILES_STAGE = "build-files"
RUNTIME_LOCK = "DB/requirements.txt"
CLIENT_PACKAGES = ("psycopg", "psycopg-c", "psycopg-pool")
LICENCE_FILE = "third_party/docker-library-postgres/LICENSE"
SBOM_TOOL = "syft"
PLATFORM = "linux/amd64"
SPDX_VERSION = "SPDX-2.3"

VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?")
COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
PINNED_IMAGE_RE = re.compile(r"(?P<name>[a-z0-9._/:-]+?)(?::(?P<tag>[A-Za-z0-9._-]+))?@(?P<digest>sha256:[0-9a-f]{64})")
ARG_RE = re.compile(r"ARG ([A-Za-z_][A-Za-z0-9_]*)=(\S+)")
FROM_RE = re.compile(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.I)
PIN_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)")
SHELL_PIN_RE = re.compile(r"readonly ([A-Z_][A-Z0-9_]*)=(\S+)")
SCHEMA_CHANGE_RE = re.compile(r"([0-9]{4})_[a-z0-9_]+")

FIELDS = {
    "": ("format", "release", "build", "inputs", "images", "files"),
    "release": ("version", "test_release", "commit", "revision", "tree_changes", "tag", "source_date_epoch",
                "transform_version", "schema_changes"),
    "build": ("started", "finished", "platform", "docker_engine", "buildx", "buildkit", "builder_driver",
              "image_store", "storage_driver", "git", "tar", "gzip", "python", "helm", "sbom_tool"),
    "build.helm": ("version", "sha256"),
    "build.sbom_tool": ("name", "version", "image", "image_id"),
    "inputs": ("dockerfiles", "build_contexts", "locks", "build_scripts", "base_images", "postgresql_source",
               "entrypoint_licence"),
    "inputs.postgresql_source": ("version", "url", "sha256"),
    "inputs.entrypoint_licence": ("path", "sha256"),
    "images": ROLES,
    "images.application": ("tag", "id", "config_digest", "platform", "labels", "build_packages", "client_libraries"),
    "images.postgres": ("tag", "id", "config_digest", "platform", "labels", "build_packages", "postgresql"),
    "images.application.client_libraries": ("psycopg", "psycopg-c", "psycopg-pool", "implementation", "libpq",
                                            "libpq_build", "libpq5"),
}
BUILD_TEXT_FIELDS = ("platform", "docker_engine", "buildx", "buildkit", "builder_driver", "image_store",
                     "storage_driver", "git", "tar", "gzip", "python")


class ProvenanceError(Exception):
    """A file or fact is missing or malformed, or the record disagrees."""


# --- reading ----------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError as error:
        raise ProvenanceError(f"cannot read {path.name}: {error.strerror}") from None
    return digest.hexdigest()


def read_text(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ProvenanceError(f"{what} is missing or unreadable ({path.name})") from None


def read_json(path: Path, what: str) -> object:
    try:
        return json.loads(read_text(path, what))
    except json.JSONDecodeError as error:
        raise ProvenanceError(f"{what} is not JSON: {error.msg} at line {error.lineno}") from None


def normalise(name: str) -> str:
    """A Python distribution's name as PEP 503 compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def is_test_version(version: str) -> bool:
    """The rule of DB/lib/release_identity.sh: a pre-release part beginning with "test"."""
    match = VERSION_RE.fullmatch(version)
    return bool(match and match.group(4) and match.group(4).startswith("test"))


class Checkout:
    """The checkout's files that the record and its checks read."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def text(self, relative: str) -> str:
        return read_text(self.root / relative, relative)

    def sha256(self, relative: str) -> str:
        path = self.root / relative
        if not path.is_file():
            raise ProvenanceError(f"the checkout has no file {relative}")
        return sha256_file(path)

    def instructions(self, dockerfile: str) -> List[str]:
        """The Dockerfile's instructions, continuation lines joined, comments dropped."""
        lines: List[str] = []
        current = ""
        for raw in self.text(dockerfile).splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.endswith("\\"):
                current += stripped[:-1].rstrip() + " "
                continue
            lines.append(current + stripped)
            current = ""
        if current:
            lines.append(current.strip())
        return lines

    def global_args(self, dockerfile: str) -> Dict[str, str]:
        """The ARG defaults before the first FROM."""
        args: Dict[str, str] = {}
        for line in self.instructions(dockerfile):
            if FROM_RE.match(line):
                break
            match = ARG_RE.fullmatch(line)
            if match:
                args[match.group(1)] = match.group(2)
        return args

    def base_images(self) -> Dict[str, str]:
        images: Dict[str, str] = {}
        for dockerfile in DOCKERFILES.values():
            for name, value in self.global_args(dockerfile).items():
                if not PINNED_IMAGE_RE.fullmatch(value):
                    continue
                if images.get(name, value) != value:
                    raise ProvenanceError(f"the Dockerfiles give ARG {name} two values: {images[name]} and {value}")
                images[name] = value
        if not images:
            raise ProvenanceError("the Dockerfiles name no base image by digest")
        return dict(sorted(images.items()))

    def final_base(self, dockerfile: str) -> str:
        """The image the Dockerfile's last stage builds on, by digest."""
        froms = [FROM_RE.match(line) for line in self.instructions(dockerfile)]
        froms = [match for match in froms if match]
        if not froms:
            raise ProvenanceError(f"{dockerfile} has no FROM")
        reference = froms[-1].group(1)
        match = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", reference)
        if match:
            reference = self.global_args(dockerfile).get(match.group(1), "")
        if not PINNED_IMAGE_RE.fullmatch(reference):
            raise ProvenanceError(f"the last stage of {dockerfile} builds on {reference or 'nothing'}, not an image by digest")
        return reference

    def build_files(self) -> List[str]:
        """The sources the application Dockerfile copies into its stage build-files."""
        stage = None
        for line in self.instructions(DOCKERFILES["application"]):
            match = FROM_RE.match(line)
            if match:
                stage = match.group(2)
                continue
            if stage == BUILD_FILES_STAGE and line.upper().startswith("COPY "):
                words = line.split()[1:]
                sources = [word for word in words[:-1] if not word.startswith("--")]
                if sources:
                    return sorted(sources)
        raise ProvenanceError(f"{DOCKERFILES['application']} has no stage {BUILD_FILES_STAGE} with a COPY")

    def ignore_file(self, dockerfile: str) -> str:
        """The .dockerignore file Docker uses with this Dockerfile: its own, else the context's."""
        own = f"{dockerfile}.dockerignore"
        return own if (self.root / own).is_file() else ".dockerignore"

    def postgresql_source(self) -> Dict[str, str]:
        dockerfile = DOCKERFILES["postgres"]
        args = self.global_args(dockerfile)
        version, sha256 = args.get("PG_VERSION", ""), args.get("PG_SHA256", "")
        if not re.fullmatch(r"[0-9]+\.[0-9]+", version) or not SHA256_RE.fullmatch(sha256):
            raise ProvenanceError(f"{dockerfile} has no PG_VERSION and PG_SHA256")
        urls = re.findall(r'"(https://[^"\s]*\$\{PG_VERSION\}[^"\s]*)"', self.text(dockerfile))
        if not urls:
            raise ProvenanceError(f"{dockerfile} downloads no PostgreSQL source of PG_VERSION")
        return {"version": version, "url": urls[0].replace("${PG_VERSION}", version), "sha256": sha256}

    def entrypoint_licence(self) -> Dict[str, str]:
        pinned = self.global_args(DOCKERFILES["postgres"]).get("ENTRYPOINT_LICENSE_SHA256", "")
        actual = self.sha256(LICENCE_FILE)
        if pinned != actual:
            raise ProvenanceError(f"{LICENCE_FILE} has the SHA-256 {actual}, but postgres.Dockerfile pins {pinned or 'none'}")
        return {"path": LICENCE_FILE, "sha256": actual}

    def inputs(self) -> Dict[str, object]:
        build_files = self.build_files()
        locks = [path for path in build_files if re.fullmatch(r"(.*/)?requirements[^/]*\.txt", path)]
        scripts = [path for path in build_files if path not in locks] + list(IDENTITY_FILES)
        if not locks:
            raise ProvenanceError(f"the stage {BUILD_FILES_STAGE} copies no dependency lock")
        hashes = lambda paths: {path: self.sha256(path) for path in sorted(paths)}  # noqa: E731
        return {
            "dockerfiles": hashes(DOCKERFILES.values()),
            "build_contexts": hashes(self.ignore_file(dockerfile) for dockerfile in DOCKERFILES.values()),
            "locks": hashes(locks),
            "build_scripts": hashes(scripts),
            "base_images": self.base_images(),
            "postgresql_source": self.postgresql_source(),
            "entrypoint_licence": self.entrypoint_licence(),
        }

    def transform_version(self) -> str:
        match = re.search(r'^__version__ = "([^"]+)"$', self.text("DB/people_pubs/__init__.py"), re.M)
        if not match:
            raise ProvenanceError("DB/people_pubs/__init__.py holds no __version__")
        return match.group(1)

    def schema_changes(self) -> List[Dict[str, object]]:
        changes = []
        directory = self.root / "DB" / "schema"
        for path in sorted(directory.glob("*.sql")) if directory.is_dir() else []:
            match = SCHEMA_CHANGE_RE.fullmatch(path.stem)
            if not match:
                raise ProvenanceError(f"DB/schema/{path.name} is not named NNNN_<name>.sql")
            changes.append({"number": int(match.group(1)), "name": path.stem, "sha256": sha256_file(path)})
        return changes

    def lock_pins(self, relative: str) -> Dict[str, str]:
        pins = {}
        for line in self.text(relative).splitlines():
            match = PIN_RE.match(line.strip())
            if match:
                pins[normalise(match.group(1))] = match.group(2)
        if not pins:
            raise ProvenanceError(f"{relative} pins no package")
        return pins

    def shell_pins(self, relative: str) -> Dict[str, str]:
        return {match.group(1): match.group(2) for match in
                (SHELL_PIN_RE.fullmatch(line.strip()) for line in self.text(relative).splitlines()) if match}


# --- the facts ----------------------------------------------------------------------


class Facts:
    """What release.sh found out from Docker, the images and the tools."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def text(self, name: str) -> str:
        return read_text(self.directory / name, f"the fact {name}").strip()

    def first_line(self, name: str) -> str:
        text = self.text(name)
        return text.splitlines()[0].strip() if text else ""

    def platform(self, role: str) -> str:
        return self.first_line(f"{role}.platform")

    def config_digest(self, role: str) -> str:
        digest = self.text(f"{role}.config-digest")
        if not IMAGE_ID_RE.fullmatch(digest):
            raise ProvenanceError(f"invalid config digest for {role}")
        return digest

    def labels(self, role: str) -> Dict[str, str]:
        labels = read_json(self.directory / f"{role}.labels.json", f"the labels of the {role} image")
        if not isinstance(labels, dict) or not all(isinstance(v, str) for v in labels.values()):
            raise ProvenanceError(f"the labels of the {role} image are not a map of texts")
        return dict(sorted(labels.items()))

    def build_packages(self, role: str) -> Dict[str, str]:
        packages: Dict[str, str] = {}
        for line in self.text(f"{role}.build-packages.txt").splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[0] in packages:
                raise ProvenanceError(f"build-packages.txt of the {role} image has a line that is not "
                                      f"'<package> <version>' once: {line!r}")
            packages[parts[0]] = parts[1]
        if not packages:
            raise ProvenanceError(f"build-packages.txt of the {role} image lists no package")
        return dict(sorted(packages.items()))

    def client_libraries(self) -> Dict[str, str]:
        probe = read_json(self.directory / "application.client.json", "the application image's client libraries")
        if not isinstance(probe, dict):
            raise ProvenanceError("the application image's client libraries are not a JSON object")
        result: Dict[str, str] = {}
        for name in CLIENT_PACKAGES + ("implementation",):
            value = probe.get(name)
            if not isinstance(value, str) or not value:
                raise ProvenanceError(f"the application image reports no {name}")
            result[name] = value
        for name in ("libpq", "libpq_build"):
            value = probe.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 100000:
                raise ProvenanceError(f"the application image reports no {name} version")
            result[name] = f"{value // 10000}.{value % 10000}"
        libpq5 = self.first_line("application.libpq5.txt")
        if not libpq5 or " " in libpq5:
            raise ProvenanceError("the application image reports no version of Debian's libpq5")
        result["libpq5"] = libpq5
        return result

    def postgresql(self) -> str:
        match = re.fullmatch(r"postgres \(PostgreSQL\) ([0-9]+\.[0-9]+)", self.first_line("postgres.version.txt"))
        if not match:
            raise ProvenanceError("the PostgreSQL image's postgres --version names no release")
        return match.group(1)

    def builder(self) -> Dict[str, object]:
        def word(name: str, pattern: str, what: str) -> str:
            match = re.search(pattern, self.text(name), re.M)
            if not match:
                raise ProvenanceError(f"no {what} in the fact {name}")
            return match.group(1)

        engine = word("builder.docker.txt", r"^(\S+) \S+$", "Docker Engine version")
        platform = word("builder.docker.txt", r"^\S+ (\S+)$", "platform")
        store = self.first_line("builder.store.txt").split()
        if not store:
            raise ProvenanceError("no storage driver in the fact builder.store.txt")
        helm = self.first_line("builder.helm.txt").split()
        if len(helm) != 2 or not SHA256_RE.fullmatch(helm[1]):
            raise ProvenanceError("the fact builder.helm.txt is not '<version> <SHA-256>'")
        syft = self.text("builder.syft.txt").splitlines()
        syft_version = re.search(r"^Version:[ \t]*(\S+)$", "\n".join(syft), re.M)
        if not syft or not IMAGE_ID_RE.fullmatch(syft[0].strip()) or not syft_version:
            raise ProvenanceError("the fact builder.syft.txt is not the image id followed by the version output")
        return {
            "platform": platform,
            "docker_engine": engine,
            "buildx": word("builder.buildx.txt", r"\s(v?[0-9][^\s]*)", "buildx version"),
            "buildkit": word("builder.buildkit.txt", r"^BuildKit version:[ \t]*(\S+)", "BuildKit version"),
            "builder_driver": word("builder.buildkit.txt", r"^Driver:[ \t]*(\S+)", "builder driver"),
            "image_store": "containerd" if store[1:] == ["io.containerd.snapshotter.v1"] else "classic",
            "storage_driver": store[0],
            "git": word("builder.git.txt", r"^git version (\S+)", "Git version"),
            "tar": word("builder.tar.txt", r"^(.*\S)$", "tar version"),
            "gzip": word("builder.gzip.txt", r"^gzip (\S+)", "gzip version"),
            "helm": {"version": helm[0].lstrip("v"), "sha256": helm[1]},
            "sbom_tool": {"name": SBOM_TOOL, "version": syft_version.group(1), "image_id": syft[0].strip()},
        }


# --- the release's directory ----------------------------------------------------------


def images_file(directory: Path) -> Dict[str, Tuple[str, str]]:
    """IMAGES: role -> (local tag, image id)."""
    images: Dict[str, Tuple[str, str]] = {}
    for line in read_text(directory / "IMAGES", "IMAGES").splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[0] not in ROLES or parts[0] in images or not IMAGE_ID_RE.fullmatch(parts[2]):
            raise ProvenanceError(f"IMAGES has a line that is not '<role> <tag> <image id>': {line!r}")
        images[parts[0]] = (parts[1], parts[2])
    if tuple(images) != ROLES:
        raise ProvenanceError("IMAGES has to name the application image, then the PostgreSQL image")
    return images


def record_name(version: str) -> str:
    return f"pubdex-{version}-provenance.json"


def sbom_name(version: str, role: str) -> str:
    return f"pubdex-{version}-image.spdx.json" if role == "application" else f"pubdex-{version}-postgres-image.spdx.json"


def artifact_hashes(directory: Path, version: str) -> Dict[str, str]:
    """Every file of the directory but the record itself, SHA256SUMS and DIGESTS."""
    skip = {record_name(version), "SHA256SUMS", "DIGESTS"}
    return {path.name: sha256_file(path) for path in sorted(directory.iterdir())
            if path.name not in skip and path.is_file()}


# --- the record ---------------------------------------------------------------------


class Identity:
    def __init__(self, version: str, commit: str, revision: str, epoch: str) -> None:
        if not VERSION_RE.fullmatch(version):
            raise ProvenanceError(f"not a release version: {version}")
        if not COMMIT_RE.fullmatch(commit):
            raise ProvenanceError(f"not a full commit id: {commit}")
        if revision not in (commit, commit + "-dirty"):
            raise ProvenanceError(f"the revision {revision} is not the commit {commit}, with or without -dirty")
        if not epoch.isdigit():
            raise ProvenanceError(f"SOURCE_DATE_EPOCH is not a number of seconds: {epoch}")
        self.version, self.commit, self.revision, self.epoch = version, commit, revision, int(epoch)
        self.test = is_test_version(version)

    def release(self, checkout: Checkout) -> Dict[str, object]:
        return {
            "version": self.version,
            "test_release": self.test,
            "commit": self.commit,
            "revision": self.revision,
            "tree_changes": self.revision.endswith("-dirty"),
            "tag": None if self.test else f"v{self.version}",
            "source_date_epoch": self.epoch,
            "transform_version": checkout.transform_version(),
            "schema_changes": checkout.schema_changes(),
        }


def images_section(directory: Path, facts: Facts, format: str = FORMAT) -> Dict[str, object]:
    listed = images_file(directory)
    images: Dict[str, object] = {}
    for role in ROLES:
        tag, image_id = listed[role]
        entry: Dict[str, object] = {"tag": tag, "id": image_id, "platform": facts.platform(role),
                                    "labels": facts.labels(role), "build_packages": facts.build_packages(role)}
        if format == FORMAT:
            entry["config_digest"] = facts.config_digest(role)
        if role == "application":
            entry["client_libraries"] = facts.client_libraries()
        else:
            entry["postgresql"] = facts.postgresql()
        images[role] = entry
    return images


def build_record(checkout: Checkout, directory: Path, facts: Facts, identity: Identity,
                 started: str, finished: str) -> Dict[str, object]:
    for name, value in (("--started", started), ("--finished", finished)):
        if not TIME_RE.fullmatch(value):
            raise ProvenanceError(f"{name} is not a UTC time YYYY-MM-DDThh:mm:ssZ: {value}")
    builder = facts.builder()
    tool = builder["sbom_tool"]
    assert isinstance(tool, dict)
    tool["image"] = checkout.shell_pins("DB/lib/sbom_tool.sh").get("PINNED_SYFT_IMAGE", "")
    builder["sbom_tool"] = {field: tool[field] for field in FIELDS["build.sbom_tool"]}
    builder["python"] = ".".join(str(part) for part in sys.version_info[:3])
    build: Dict[str, object] = {"started": started, "finished": finished}
    build.update((field, builder[field]) for field in FIELDS["build"][2:])
    return {
        "format": FORMAT,
        "release": identity.release(checkout),
        "build": build,
        "inputs": checkout.inputs(),
        "images": images_section(directory, facts),
        "files": artifact_hashes(directory, identity.version),
    }


def write_record(checkout: Checkout, directory: Path, facts: Facts, identity: Identity,
                 started: str, finished: str) -> Path:
    record = build_record(checkout, directory, facts, identity, started, finished)
    path = directory / record_name(identity.version)
    if path.exists():
        raise ProvenanceError(f"{path.name} exists already")
    path.write_text(json.dumps(record, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return path


# --- checks -------------------------------------------------------------------------


class Problems:
    def __init__(self) -> None:
        self.items: List[str] = []

    def add(self, message: str) -> None:
        self.items.append(message)

    def equal(self, where: str, recorded: object, expected: object) -> None:
        """Each differing value, by the path of its field."""
        if recorded == expected:
            return
        if isinstance(recorded, dict) and isinstance(expected, dict):
            for key in sorted(set(recorded) | set(expected)):
                self.equal(f"{where}.{key}", recorded.get(key, "(missing)"), expected.get(key, "(missing)"))
        else:
            self.add(f"{where}: recorded {_short(recorded)}, expected {_short(expected)}")


def _short(value: object) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= 160 else text[:157] + "..."


def check_shape(record: object, problems: Problems) -> bool:
    """Exactly the fields of the format, at every level FIELDS names."""
    if not isinstance(record, dict):
        problems.add("the record is not a JSON object")
        return False
    good = True
    for where, fields in FIELDS.items():
        if record.get("format") == LEGACY_FORMAT:
            fields = tuple(field for field in fields if field != "config_digest")
        node: object = record
        for part in filter(None, where.split(".")):
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            problems.add(f"{where or 'the record'} is not an object")
            good = False
            continue
        if sorted(node) != sorted(fields):
            problems.add(f"{where or 'the record'} has the fields {sorted(node)}, not {sorted(fields)}")
            good = False
    return good


def check_build(build: Dict[str, object], checkout: Checkout, problems: Problems) -> None:
    times = []
    for field in ("started", "finished"):
        value = build[field]
        if not isinstance(value, str) or not TIME_RE.fullmatch(value):
            problems.add(f"build.{field} is not a UTC time: {_short(value)}")
        else:
            times.append(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    if len(times) == 2 and times[0] > times[1]:
        problems.add("build.started is later than build.finished")
    for field in BUILD_TEXT_FIELDS:
        if not isinstance(build[field], str) or not build[field]:
            problems.add(f"build.{field} is not a text")
    if build["platform"] != PLATFORM:
        problems.add(f"build.platform is {_short(build['platform'])}, not {PLATFORM}")
    helm_pins = checkout.shell_pins("DB/lib/cluster_tools.sh")
    problems.equal("build.helm", build["helm"], {"version": helm_pins.get("PINNED_HELM_VERSION"),
                                                 "sha256": helm_pins.get("PINNED_HELM_MEMBER_SHA256")})
    pins = checkout.shell_pins("DB/lib/sbom_tool.sh")
    tool = build["sbom_tool"]
    assert isinstance(tool, dict)
    problems.equal("build.sbom_tool", {key: tool[key] for key in ("name", "version", "image")},
                   {"name": SBOM_TOOL, "version": pins.get("PINNED_SYFT_VERSION"), "image": pins.get("PINNED_SYFT_IMAGE")})
    if tool["image_id"] not in (pins.get("PINNED_SYFT_IMAGE_ID"), pins.get("PINNED_SYFT_INDEX_DIGEST")):
        problems.add(f"build.sbom_tool.image_id {_short(tool['image_id'])} is not the pinned image's id")


def check_labels(record: Dict[str, object], checkout: Checkout, identity: Identity, problems: Problems) -> None:
    """The images' labels against the identity and the checkout's pins."""
    images = record["images"]
    assert isinstance(images, dict)
    pg_version = checkout.postgresql_source()["version"]
    for role in ROLES:
        image = images[role]
        labels = image["labels"] if isinstance(image, dict) else None
        if not isinstance(labels, dict):
            continue
        base = PINNED_IMAGE_RE.fullmatch(checkout.final_base(DOCKERFILES[role]))
        assert base is not None
        base_name = base.group("name") + (f":{base.group('tag')}" if base.group("tag") else "")
        if "/" not in base_name.split(":")[0]:
            base_name = "docker.io/library/" + base_name
        expected = {
            "org.opencontainers.image.version": identity.version,
            "org.opencontainers.image.revision": identity.revision,
            "org.opencontainers.image.base.name": base_name,
            "org.opencontainers.image.base.digest": base.group("digest"),
        }
        if role == "application":
            expected["pubdex.transform-version"] = checkout.transform_version()
        else:
            expected["pubdex.postgresql-version"] = pg_version
        for name, value in expected.items():
            if labels.get(name) != value:
                problems.add(f"images.{role}.labels: {name} is {_short(labels.get(name))}, but the checkout gives {value}")
        if role == "postgres" and image.get("postgresql") != pg_version:
            problems.add(f"images.postgres.postgresql is {_short(image.get('postgresql'))}, not PG_VERSION {pg_version}")
        packages = image.get("build_packages")
        wanted = ("gcc", "libc6-dev", "libpq-dev") if role == "application" else ("gcc", "libc6-dev", "make")
        if not isinstance(packages, dict) or any(name not in packages for name in wanted):
            problems.add(f"images.{role}.build_packages does not name {', '.join(wanted)}")
    client = images["application"].get("client_libraries") if isinstance(images["application"], dict) else None
    if isinstance(client, dict):
        pins = checkout.lock_pins(RUNTIME_LOCK)
        for name in CLIENT_PACKAGES:
            if client.get(name) != pins.get(name):
                problems.add(f"images.application.client_libraries.{name} is {_short(client.get(name))}, "
                             f"but {RUNTIME_LOCK} pins {pins.get(name)}")
        if client.get("implementation") != "c":
            problems.add("images.application.client_libraries: psycopg does not use its C implementation")


def check_record(checkout: Checkout, directory: Path, facts: Facts, identity: Identity, problems: Problems) -> None:
    path = directory / record_name(identity.version)
    try:
        record = read_json(path, f"the provenance record {path.name}")
    except ProvenanceError as error:
        problems.add(str(error))
        return
    if not check_shape(record, problems):
        return
    assert isinstance(record, dict)
    if record["format"] not in (FORMAT, LEGACY_FORMAT):
        problems.add(f"unsupported format {_short(record['format'])}")
    problems.equal("release", record["release"], identity.release(checkout))
    check_build(record["build"], checkout, problems)
    problems.equal("inputs", record["inputs"], checkout.inputs())
    current = images_section(directory, facts, record["format"])
    for role in ROLES:
        problems.equal(f"images.{role}", record["images"][role], current[role])
    check_labels(record, checkout, identity, problems)
    problems.equal("files", record["files"], artifact_hashes(directory, identity.version))


def check_sbom(path: Path, role: str, tag: str, identity: Identity, checkout: Checkout, facts: Facts,
               problems: Problems) -> None:
    what = f"the SBOM {path.name}"
    try:
        document = read_json(path, what)
    except ProvenanceError as error:
        problems.add(str(error))
        return
    if not isinstance(document, dict):
        problems.add(f"{what} is not a JSON object")
        return
    for field, value in (("spdxVersion", SPDX_VERSION), ("dataLicense", "CC0-1.0"), ("SPDXID", "SPDXRef-DOCUMENT")):
        if document.get(field) != value:
            problems.add(f"{what}: {field} is {_short(document.get(field))}, not {value}")
    creators = document.get("creationInfo", {}).get("creators") if isinstance(document.get("creationInfo"), dict) else None
    tool = f"Tool: {SBOM_TOOL}-{checkout.shell_pins('DB/lib/sbom_tool.sh').get('PINNED_SYFT_VERSION')}"
    if not isinstance(creators, list) or tool not in creators:
        problems.add(f"{what} was not made by the pinned SBOM tool ({tool})")
    packages = document.get("packages")
    relationships = document.get("relationships")
    if not isinstance(packages, list) or not all(isinstance(p, dict) for p in packages) or \
            not isinstance(relationships, list):
        problems.add(f"{what} has no list of packages and relationships")
        return
    by_id = {package.get("SPDXID"): package for package in packages}
    described = [by_id.get(rel.get("relatedSpdxElement")) for rel in relationships if isinstance(rel, dict)
                 and rel.get("spdxElementId") == "SPDXRef-DOCUMENT" and rel.get("relationshipType") == "DESCRIBES"]
    repository = tag.rsplit(":", 1)[0]
    if len(described) != 1 or not isinstance(described[0], dict) or \
            described[0].get("primaryPackagePurpose") != "CONTAINER" or described[0].get("name") != repository or \
            described[0].get("versionInfo") != identity.version:
        problems.add(f"{what} does not describe one container image {repository} of version {identity.version}")

    def versions(purl_prefix: str, name: str, normalised: bool = False) -> List[str]:
        found = []
        for package in packages:
            refs = package.get("externalRefs") or []
            purls = [ref.get("referenceLocator", "") for ref in refs if isinstance(ref, dict)
                     and ref.get("referenceType") == "purl"]
            own = normalise(str(package.get("name", ""))) if normalised else package.get("name")
            if own == name and any(purl.startswith(purl_prefix) for purl in purls):
                found.append(str(package.get("versionInfo")))
        return found

    if role == "application":
        for name, version in sorted(checkout.lock_pins(RUNTIME_LOCK).items()):
            found = versions("pkg:pypi/", name, normalised=True)
            if found != [version]:
                problems.add(f"{what} names the Python package {name} as {found or 'nothing'}, "
                             f"but {RUNTIME_LOCK} installs {version}")
        try:
            libpq5 = facts.client_libraries()["libpq5"]
        except ProvenanceError as error:
            problems.add(str(error))
        else:
            found = versions("pkg:deb/", "libpq5")
            if found != [libpq5]:
                problems.add(f"{what} names Debian's libpq5 as {found or 'nothing'}, but the image holds {libpq5}")
    else:
        expected = checkout.postgresql_source()["version"]
        try:
            reported = facts.postgresql()
        except ProvenanceError as error:
            problems.add(str(error))
            reported = expected
        found = versions("pkg:generic/postgresql", "postgresql")
        if found != [expected] or reported != expected:
            problems.add(f"{what} names PostgreSQL as {found or 'nothing'}; postgres.Dockerfile builds {expected}, "
                         f"the image reports {reported}")


def check_all(checkout: Checkout, directory: Path, facts: Facts, identity: Identity) -> List[str]:
    problems = Problems()
    try:
        listed = images_file(directory)
        for role in ROLES:
            check_sbom(directory / sbom_name(identity.version, role), role, listed[role][0], identity, checkout,
                       facts, problems)
        check_record(checkout, directory, facts, identity, problems)
    except ProvenanceError as error:
        problems.add(str(error))
    return problems.items


# --- command line -------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None, stdout=sys.stdout, stderr=sys.stderr) -> int:
    parser = argparse.ArgumentParser(prog="release_provenance.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=("write", "check"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--started")
    parser.add_argument("--finished")
    args = parser.parse_args(argv)
    if args.step == "write" and not (args.started and args.finished):
        parser.error("write needs --started and --finished")
    checkout, facts = Checkout(args.root), Facts(args.facts)
    try:
        identity = Identity(args.version, args.commit, args.revision, args.epoch)
        if args.step == "write":
            path = write_record(checkout, args.dir, facts, identity, args.started, args.finished)
            print(f"wrote {path.name}", file=stdout)
            problems = check_all(checkout, args.dir, facts, identity)
        else:
            problems = check_all(checkout, args.dir, facts, identity)
    except ProvenanceError as error:
        problems = [str(error)]
    if problems:
        for problem in problems:
            print(f"release_provenance.py: {problem}", file=stderr)
        return 1
    print(f"ok: both SBOMs and {record_name(identity.version)} agree with the checkout, the directory and the images",
          file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
