"""DB/release_provenance.py, offline: the provenance record and the SBOMs.

A fixture checkout -- two small Dockerfiles with their ARG pins and the stage
build-files, the locks, the build scripts, the entry point's licence, the
tool pins, the transform version and a schema change -- a release directory
with IMAGES, two artifacts and two SPDX documents shaped as Syft writes
them, and the facts release.sh gathers from Docker and the images. Neither
Docker nor Git runs.

That proves: every field of the record comes from the checkout's files, the
directory or the facts -- the base images and the PostgreSQL source from the
Dockerfiles, the scripts from the stage build-files, the tag and whether the
tree had changes from the identity -- and the record names nothing of the
machine that wrote it; check passes on what write wrote and fails, naming
the field, for a record changed in any section, a field added or missing, a
checkout or an image that changed since, labels that disagree with the
Dockerfiles, client libraries that are not the runtime lock's, and for an
SBOM of another format, tool, image or version, or one that misses a locked
package, Debian's libpq5 or PostgreSQL. The real checkout's Dockerfiles give
the inputs the record of a real build names.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

import release_provenance
from release_provenance import Checkout, Facts, Identity, ProvenanceError

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "DB" / "release_provenance.py"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
VERSION = "0.2.0"
TEST_VERSION = "0.2.0-test.a"
EPOCH = "1763452800"
PY_DIGEST = "sha256:" + "a" * 64
DEB_DIGEST = "sha256:" + "b" * 64
ENTRY_DIGEST = "sha256:" + "c" * 64
PG_SHA256 = "d" * 64
APP_ID = "sha256:" + "1" * 64
PG_ID = "sha256:" + "2" * 64
SYFT_ID = "sha256:" + "3" * 64
SYFT_INDEX = "sha256:" + "4" * 64
SYFT_IMAGE = f"docker.io/anchore/syft:v1.57.2@{SYFT_INDEX}"
HELM_SHA256 = "5" * 64
LICENCE = "MIT License\n\nThe entry point's licence.\n"
TRANSFORM = "1.5.4"
LIBPQ5 = "17.11-0+deb13u1"
LOCK = {"psycopg": "3.3.6", "psycopg-c": "3.3.6", "psycopg-pool": "3.3.3", "typing_extensions": "4.15.0"}

APP_DOCKERFILE = f"""\
# The application image.
ARG PYTHON_BASE=python:3.11.18-slim-trixie@{PY_DIGEST}

FROM scratch AS build-files
COPY DB/requirements-pip.txt DB/requirements.txt \\
     DB/fetch_verified.py DB/install_locked.py \\
     /

FROM ${{PYTHON_BASE}} AS psycopg-c
RUN dpkg-query -W gcc

FROM psycopg-c AS build-packages
RUN dpkg-query -W -f '${{Package}} ${{Version}}\\n' | LC_ALL=C sort > /build-packages.txt

FROM ${{PYTHON_BASE}}
COPY --from=build-packages /build-packages.txt /usr/share/doc/pubdex/build-packages.txt
LABEL org.opencontainers.image.base.name="docker.io/library/python:3.11.18-slim-trixie"
"""

PG_DOCKERFILE = f"""\
ARG PYTHON_BASE=python:3.11.18-slim-trixie@{PY_DIGEST}
ARG DEBIAN_BASE=debian:trixie-slim@{DEB_DIGEST}
ARG POSTGRES_ENTRYPOINT_IMAGE=postgres:16.16-trixie@{ENTRY_DIGEST}
ARG PG_MAJOR=16
ARG PG_VERSION=16.16
ARG PG_SHA256={PG_SHA256}
ARG ENTRYPOINT_LICENSE_SHA256={hashlib.sha256(LICENCE.encode()).hexdigest()}

FROM ${{PYTHON_BASE}} AS source
ARG PG_VERSION
RUN python -I /opt/pubdex-build/fetch_verified.py url --sha256 "${{PG_SHA256}}" \\
      "https://ftp.postgresql.org/pub/source/v${{PG_VERSION}}/postgresql-${{PG_VERSION}}.tar.bz2" \\
      "/src/postgresql-${{PG_VERSION}}.tar.bz2"

FROM ${{POSTGRES_ENTRYPOINT_IMAGE}} AS entrypoint
FROM ${{DEBIAN_BASE}} AS build

FROM ${{DEBIAN_BASE}}
"""

FILES = {
    "Dockerfile": APP_DOCKERFILE,
    "postgres.Dockerfile": PG_DOCKERFILE,
    ".dockerignore": "*\n!DB/requirements.txt\n",
    "postgres.Dockerfile.dockerignore": "*\n!DB/fetch_verified.py\n",
    "DB/requirements-pip.txt": "pip==26.2.2 \\\n    --hash=sha256:" + "e" * 64 + "\n",
    "DB/requirements.txt": "".join(f"{name}=={version} \\\n    --hash=sha256:{'f' * 64}\n"
                                   for name, version in LOCK.items()),
    "DB/fetch_verified.py": "# downloads\n",
    "DB/install_locked.py": "# installs\n",
    "build_image.sh": "#!/bin/sh\n",
    "DB/lib/release_identity.sh": "# identity\n",
    "DB/lib/cluster_tools.sh": f"readonly PINNED_HELM_VERSION=4.3.1\nreadonly PINNED_HELM_MEMBER_SHA256={HELM_SHA256}\n",
    "DB/lib/sbom_tool.sh": (f"readonly PINNED_SYFT_VERSION=1.57.2\nreadonly PINNED_SYFT_IMAGE={SYFT_IMAGE}\n"
                            f"readonly PINNED_SYFT_INDEX_DIGEST={SYFT_INDEX}\nreadonly PINNED_SYFT_IMAGE_ID={SYFT_ID}\n"),
    "third_party/docker-library-postgres/LICENSE": LICENCE,
    "DB/people_pubs/__init__.py": f'__version__ = "{TRANSFORM}"\n',
    "DB/schema/0001_schema_change_record.sql": "-- backward-compatible: yes\n",
}


def _labels(role: str, version: str, revision: str) -> Dict[str, str]:
    labels = {"org.opencontainers.image.version": version, "org.opencontainers.image.revision": revision,
              "org.opencontainers.image.created": "2026-11-18T03:01:33Z"}
    if role == "application":
        labels.update({"org.opencontainers.image.base.name": "docker.io/library/python:3.11.18-slim-trixie",
                       "org.opencontainers.image.base.digest": PY_DIGEST, "pubdex.transform-version": TRANSFORM})
    else:
        labels.update({"org.opencontainers.image.base.name": "docker.io/library/debian:trixie-slim",
                       "org.opencontainers.image.base.digest": DEB_DIGEST, "pubdex.postgresql-version": "16.16"})
    return labels


def _package(name: str, version: str, purl: str, spdx_id: str) -> dict:
    return {"name": name, "SPDXID": spdx_id, "versionInfo": version,
            "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl",
                              "referenceLocator": purl}]}


def _sbom(role: str, version: str) -> dict:
    repository = "pubdex-release" if role == "application" else "pubdex-postgres-release"
    root = {"name": repository, "SPDXID": f"SPDXRef-DocumentRoot-Image-{repository}", "versionInfo": version,
            "primaryPackagePurpose": "CONTAINER"}
    packages = [root, _package("libc6", "2.41-12", "pkg:deb/debian/libc6@2.41-12", "SPDXRef-Package-deb-libc6")]
    if role == "application":
        packages.append(_package("libpq5", LIBPQ5, f"pkg:deb/debian/libpq5@{LIBPQ5}", "SPDXRef-Package-deb-libpq5"))
        for name, pinned in LOCK.items():
            packages.append(_package(name, pinned, f"pkg:pypi/{name}@{pinned}", f"SPDXRef-Package-python-{name}"))
        packages.append(_package("wheel", "0.46.3", "pkg:pypi/wheel@0.46.3", "SPDXRef-Package-python-wheel"))
    else:
        packages.append(_package("postgresql", "16.16", "pkg:generic/postgresql@16.16",
                                 "SPDXRef-Package-binary-postgresql"))
    return {
        "spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT", "name": repository,
        "documentNamespace": f"https://example.org/{repository}-0000",
        "creationInfo": {"creators": ["Organization: Anchore, Inc", "Tool: syft-1.57.2"],
                         "created": "2026-11-18T03:01:40Z"},
        "packages": packages,
        "relationships": [{"spdxElementId": "SPDXRef-DOCUMENT", "relatedSpdxElement": root["SPDXID"],
                           "relationshipType": "DESCRIBES"}],
    }


BUILDKIT = """Name:          default
Driver:        docker
Last Activity: 2026-11-18 02:42:00 +0000 UTC

Nodes:
Name:             default
Endpoint:         default
Status:           running
BuildKit version: v0.35.0
Labels:
 org.mobyproject.buildkit.worker.moby.host-gateway-ip: 192.0.2.17
"""


class Fixture:
    def __init__(self, tmp_path: Path, version: str = TEST_VERSION, revision: str = COMMIT + "-dirty") -> None:
        self.root = tmp_path / "checkout"
        self.dir = tmp_path / "release"
        self.facts = tmp_path / "facts"
        self.version, self.revision = version, revision
        for name, text in FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.dir.mkdir()
        (self.dir / "IMAGES").write_text(f"application pubdex-release:{version} {APP_ID}\n"
                                         f"postgres pubdex-postgres-release:{version} {PG_ID}\n")
        (self.dir / f"pubdex-{version}-source.tar.gz").write_bytes(b"source archive")
        (self.dir / f"pubdex-{version}.tgz").write_bytes(b"chart package")
        (self.dir / "SHA256SUMS").write_text("not covered by the record\n")
        for role in ("application", "postgres"):
            self.write_sbom(role, _sbom(role, version))
        self.facts.mkdir()
        facts = {
            "application.config-digest": APP_ID,
            "postgres.config-digest": PG_ID,
            "application.platform": "linux/amd64\n",
            "postgres.platform": "linux/amd64\n",
            "application.labels.json": json.dumps(_labels("application", version, revision)) + "\n",
            "postgres.labels.json": json.dumps(_labels("postgres", version, revision)) + "\n",
            "application.build-packages.txt": f"gcc 4:14.2.0-1\ngcc-14 14.2.0-19\nlibc6-dev 2.41-12\nlibpq-dev {LIBPQ5}\n",
            "postgres.build-packages.txt": "gcc 4:14.2.0-1\nlibc6-dev 2.41-12\nmake 4.4.1-2\n",
            "application.client.json": json.dumps({"psycopg": "3.3.6", "psycopg-c": "3.3.6", "psycopg-pool": "3.3.3",
                                                   "implementation": "c", "libpq": 170011, "libpq_build": 170011}),
            "application.libpq5.txt": LIBPQ5 + "\n",
            "postgres.version.txt": "postgres (PostgreSQL) 16.16\n",
            "builder.docker.txt": "29.10.0 linux/amd64\n",
            "builder.buildx.txt": "github.com/docker/buildx v0.39.0 360040a78d6113dc84e5d4dcdf0f438e1be7a41f\n",
            "builder.buildkit.txt": BUILDKIT,
            "builder.store.txt": "overlay2 \n",
            "builder.git.txt": "git version 2.43.0\n",
            "builder.tar.txt": "tar (GNU tar) 1.35\n",
            "builder.gzip.txt": "gzip 1.12\n",
            "builder.helm.txt": f"v4.3.1 {HELM_SHA256}\n",
            "builder.syft.txt": f"{SYFT_ID}\nApplication:   syft\nVersion:       1.57.2\nBuildDate:     2026-11-16\n",
        }
        for name, text in facts.items():
            (self.facts / name).write_text(text, encoding="utf-8")

    @property
    def record_path(self) -> Path:
        return self.dir / f"pubdex-{self.version}-provenance.json"

    def sbom_path(self, role: str) -> Path:
        middle = "image" if role == "application" else "postgres-image"
        return self.dir / f"pubdex-{self.version}-{middle}.spdx.json"

    def write_sbom(self, role: str, document: dict) -> None:
        self.sbom_path(role).write_text(json.dumps(document), encoding="utf-8")

    def identity(self) -> Identity:
        return Identity(self.version, COMMIT, self.revision, EPOCH)

    def write(self) -> dict:
        release_provenance.write_record(Checkout(self.root), self.dir, Facts(self.facts), self.identity(),
                                        "2026-11-18T03:01:32Z", "2026-11-18T03:01:45Z")
        return self.record()

    def record(self) -> dict:
        return json.loads(self.record_path.read_text(encoding="utf-8"))

    def save(self, record: dict) -> None:
        self.record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    def check(self) -> List[str]:
        return release_provenance.check_all(Checkout(self.root), self.dir, Facts(self.facts), self.identity())


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- write ---------------------------------------------------------------------


def test_the_record_takes_every_field_from_the_checkout_the_directory_and_the_facts(fixture: Fixture) -> None:
    record = fixture.write()
    assert record["format"] == "pubdex-provenance/2"
    assert record["release"] == {
        "version": TEST_VERSION, "test_release": True, "commit": COMMIT, "revision": COMMIT + "-dirty",
        "tree_changes": True, "tag": None, "source_date_epoch": int(EPOCH), "transform_version": TRANSFORM,
        "schema_changes": [{"number": 1, "name": "0001_schema_change_record",
                            "sha256": _sha(FILES["DB/schema/0001_schema_change_record.sql"])}],
    }
    inputs = record["inputs"]
    assert inputs["dockerfiles"] == {"Dockerfile": _sha(APP_DOCKERFILE), "postgres.Dockerfile": _sha(PG_DOCKERFILE)}
    assert inputs["build_contexts"] == {".dockerignore": _sha(FILES[".dockerignore"]),
                                        "postgres.Dockerfile.dockerignore": _sha(FILES["postgres.Dockerfile.dockerignore"])}
    # The locks and the scripts are the files of the stage build-files, with
    # the two scripts that label the images.
    assert inputs["locks"] == {name: _sha(FILES[name]) for name in ("DB/requirements-pip.txt", "DB/requirements.txt")}
    assert sorted(inputs["build_scripts"]) == ["DB/fetch_verified.py", "DB/install_locked.py",
                                               "DB/lib/release_identity.sh", "build_image.sh"]
    assert inputs["base_images"] == {"DEBIAN_BASE": f"debian:trixie-slim@{DEB_DIGEST}",
                                     "POSTGRES_ENTRYPOINT_IMAGE": f"postgres:16.16-trixie@{ENTRY_DIGEST}",
                                     "PYTHON_BASE": f"python:3.11.18-slim-trixie@{PY_DIGEST}"}
    assert inputs["postgresql_source"] == {
        "version": "16.16", "url": "https://ftp.postgresql.org/pub/source/v16.16/postgresql-16.16.tar.bz2",
        "sha256": PG_SHA256}
    assert inputs["entrypoint_licence"] == {"path": "third_party/docker-library-postgres/LICENSE",
                                            "sha256": _sha(LICENCE)}
    app, pg = record["images"]["application"], record["images"]["postgres"]
    assert (app["tag"], app["id"], app["platform"]) == (f"pubdex-release:{TEST_VERSION}", APP_ID, "linux/amd64")
    assert app["labels"] == _labels("application", TEST_VERSION, COMMIT + "-dirty")
    assert app["build_packages"] == {"gcc": "4:14.2.0-1", "gcc-14": "14.2.0-19", "libc6-dev": "2.41-12",
                                     "libpq-dev": LIBPQ5}
    assert app["client_libraries"] == {"psycopg": "3.3.6", "psycopg-c": "3.3.6", "psycopg-pool": "3.3.3",
                                       "implementation": "c", "libpq": "17.11", "libpq_build": "17.11",
                                       "libpq5": LIBPQ5}
    assert (pg["id"], pg["postgresql"], pg["build_packages"]["make"]) == (PG_ID, "16.16", "4.4.1-2")
    assert record["files"] == {
        name: hashlib.sha256((fixture.dir / name).read_bytes()).hexdigest()
        for name in ("IMAGES", f"pubdex-{TEST_VERSION}-image.spdx.json",
                     f"pubdex-{TEST_VERSION}-postgres-image.spdx.json", f"pubdex-{TEST_VERSION}-source.tar.gz",
                     f"pubdex-{TEST_VERSION}.tgz")}
    build = record["build"]
    assert {key: build[key] for key in ("platform", "docker_engine", "buildx", "buildkit", "builder_driver",
                                        "image_store", "storage_driver", "git", "tar", "gzip")} == {
        "platform": "linux/amd64", "docker_engine": "29.10.0", "buildx": "v0.39.0", "buildkit": "v0.35.0",
        "builder_driver": "docker", "image_store": "classic", "storage_driver": "overlay2", "git": "2.43.0",
        "tar": "tar (GNU tar) 1.35", "gzip": "1.12"}
    assert build["helm"] == {"version": "4.3.1", "sha256": HELM_SHA256}
    assert build["sbom_tool"] == {"name": "syft", "version": "1.57.2", "image": SYFT_IMAGE, "image_id": SYFT_ID}
    assert build["python"] == ".".join(map(str, sys.version_info[:3]))
    assert (build["started"], build["finished"]) == ("2026-11-18T03:01:32Z", "2026-11-18T03:01:45Z")


def test_a_release_records_its_tag_and_a_tree_without_changes(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, version=VERSION, revision=COMMIT)
    release = fixture.write()["release"]
    assert (release["test_release"], release["tag"], release["tree_changes"]) == (False, f"v{VERSION}", False)
    assert fixture.check() == []


def test_the_record_names_nothing_of_the_machine_that_wrote_it(fixture: Fixture) -> None:
    text = fixture.write() and fixture.record_path.read_text()
    for private in ("192.0.2.17", "host-gateway", str(fixture.root), str(fixture.dir), str(fixture.facts), "Endpoint"):
        assert private not in text


def test_the_containerd_image_store_is_named(fixture: Fixture) -> None:
    (fixture.facts / "builder.store.txt").write_text("overlayfs io.containerd.snapshotter.v1\n")
    assert fixture.write()["build"]["image_store"] == "containerd"


@pytest.mark.parametrize("store", ["classic", "containerd"])
def test_config_digest_is_separate_from_the_store_dependent_id(fixture: Fixture, store: str) -> None:
    if store == "containerd":
        (fixture.facts / "builder.store.txt").write_text("overlayfs io.containerd.snapshotter.v1\n")
        images = fixture.dir / "IMAGES"
        images.write_text(images.read_text().replace(APP_ID, "sha256:" + "9" * 64))
    record = fixture.write()
    assert record["images"]["application"]["config_digest"] == APP_ID
    assert fixture.check() == []
    record["images"]["application"]["config_digest"] = "sha256:" + "8" * 64
    fixture.save(record)
    assert any("images.application.config_digest" in item for item in fixture.check())


@pytest.mark.parametrize("store", ["classic", "containerd"])
def test_legacy_records_still_validate(fixture: Fixture, store: str) -> None:
    if store == "containerd":
        (fixture.facts / "builder.store.txt").write_text("overlayfs io.containerd.snapshotter.v1\n")
    record = fixture.write()
    record["format"] = "pubdex-provenance/1"
    for role in record["images"]:
        del record["images"][role]["config_digest"]
        (fixture.facts / f"{role}.config-digest").unlink()
    fixture.save(record)
    assert fixture.check() == []


def test_write_refuses_an_existing_record(fixture: Fixture) -> None:
    fixture.write()
    with pytest.raises(ProvenanceError, match="exists already"):
        fixture.write()


@pytest.mark.parametrize("name, text, message", [
    ("builder.buildkit.txt", "Driver: docker\n", "no BuildKit version"),
    ("builder.helm.txt", "v4.3.1\n", "builder.helm.txt"),
    ("builder.syft.txt", "Version: 1.57.2\n", "builder.syft.txt"),
    ("application.build-packages.txt", "gcc\n", "build-packages.txt of the application image"),
    ("application.client.json", '{"psycopg": "3.3.6"}', "reports no psycopg-c"),
    ("postgres.version.txt", "postgres 16\n", "names no release"),
    ("application.labels.json", "[]", "not a map of texts"),
])
def test_write_refuses_a_fact_it_cannot_read(fixture: Fixture, name: str, text: str, message: str) -> None:
    (fixture.facts / name).write_text(text)
    with pytest.raises(ProvenanceError, match=message):
        fixture.write()
    assert not fixture.record_path.exists()


def test_a_missing_fact_is_named(fixture: Fixture) -> None:
    (fixture.facts / "builder.git.txt").unlink()
    with pytest.raises(ProvenanceError, match="builder.git.txt"):
        fixture.write()


def test_the_entry_points_licence_has_to_be_the_pinned_one(fixture: Fixture) -> None:
    (fixture.root / "third_party/docker-library-postgres/LICENSE").write_text("another licence\n")
    with pytest.raises(ProvenanceError, match="pins"):
        fixture.write()


# --- check ---------------------------------------------------------------------


def test_check_passes_on_what_write_wrote(fixture: Fixture) -> None:
    fixture.write()
    assert fixture.check() == []


def _node(record: object, path: object) -> tuple:
    """The container of the last key of path -- a dotted text, or a tuple
    for keys that hold dots -- and that key; list positions are numbers."""
    parts = path.split(".") if isinstance(path, str) else list(path)
    node = record
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    last = parts[-1]
    return node, int(last) if isinstance(node, list) else last


def _set(path: object, value: object) -> Callable[[dict], None]:
    def change(record: dict) -> None:
        node, key = _node(record, path)
        node[key] = value
    return change


def _drop(path: object) -> Callable[[dict], None]:
    def change(record: dict) -> None:
        node, key = _node(record, path)
        del node[key]
    return change


TAMPERING = {
    "format": (_set("format", "pubdex-provenance/99"), "format"),
    "commit": (_set("release.commit", "f" * 40), "release.commit"),
    "tree changes": (_set("release.tree_changes", False), "release.tree_changes"),
    "tag": (_set("release.tag", f"v{TEST_VERSION}"), "release.tag"),
    "epoch": (_set("release.source_date_epoch", 1), "release.source_date_epoch"),
    "transform": (_set("release.transform_version", "1.5.3"), "release.transform_version"),
    "schema change": (_set("release.schema_changes", []), "release.schema_changes"),
    "base image": (_set("inputs.base_images.DEBIAN_BASE", f"debian:trixie-slim@sha256:{'0' * 64}"),
                   "inputs.base_images"),
    "lock": (_set(("inputs", "locks", "DB/requirements.txt"), "0" * 64), "inputs.locks"),
    "script": (_drop(("inputs", "build_scripts", "build_image.sh")), "inputs.build_scripts"),
    "postgresql source": (_set("inputs.postgresql_source.sha256", "0" * 64), "inputs.postgresql_source"),
    "image id": (_set("images.application.id", "sha256:" + "9" * 64), "images.application.id"),
    "label": (_set(("images", "postgres", "labels", "org.opencontainers.image.revision"), COMMIT),
              "images.postgres.labels"),
    "compiler": (_set("images.application.build_packages.gcc-14", "14.1.0-1"), "images.application.build_packages"),
    "client library": (_set("images.application.client_libraries.libpq5", "17.6-0+deb13u1"),
                       "images.application.client_libraries"),
    "postgresql": (_set("images.postgres.postgresql", "16.14"), "images.postgres.postgresql"),
    "file": (_set(("files", f"pubdex-{TEST_VERSION}.tgz"), "0" * 64), "files.pubdex-"),
    "helm": (_set("build.helm.sha256", "0" * 64), "build.helm.sha256"),
    "sbom tool": (_set("build.sbom_tool.image_id", "sha256:" + "0" * 64), "build.sbom_tool.image_id"),
    "sbom tool version": (_set("build.sbom_tool.version", "1.57.1"), "build.sbom_tool.version"),
    "times": (_set("build.started", "2026-11-18T04:00:00Z"), "later than build.finished"),
    "time format": (_set("build.finished", "yesterday"), "build.finished is not a UTC time"),
    "platform": (_set("build.platform", "linux/arm64"), "build.platform"),
    "field added": (_set("build.host", "builder.example.org"), "build has the fields"),
    "field missing": (_drop("release.tag"), "release has the fields"),
    "section missing": (_drop("inputs"), "the record has the fields"),
}


@pytest.mark.parametrize("change, message", TAMPERING.values(), ids=TAMPERING.keys())
def test_a_tampered_record_fails_the_check_naming_the_field(fixture: Fixture, change: Callable[[dict], None],
                                                            message: str) -> None:
    record = fixture.write()
    change(record)
    fixture.save(record)
    problems = fixture.check()
    assert problems and any(message in problem for problem in problems), problems


def test_a_record_that_is_no_json_fails(fixture: Fixture) -> None:
    fixture.write()
    fixture.record_path.write_text("{")
    assert any("is not JSON" in problem for problem in fixture.check())


def test_a_checkout_that_changed_since_the_build_fails(fixture: Fixture) -> None:
    fixture.write()
    (fixture.root / "DB/requirements-pip.txt").write_text("pip==26.2.3 \\\n    --hash=sha256:" + "e" * 64 + "\n")
    assert any(problem.startswith("inputs.locks.DB/requirements-pip.txt") for problem in fixture.check())


def test_an_image_that_changed_since_the_build_fails(fixture: Fixture) -> None:
    fixture.write()
    labels = _labels("application", TEST_VERSION, COMMIT + "-dirty")
    labels["org.opencontainers.image.created"] = "2026-11-18T05:00:00Z"
    (fixture.facts / "application.labels.json").write_text(json.dumps(labels))
    assert any("images.application.labels" in problem for problem in fixture.check())


def test_a_file_of_the_directory_that_changed_since_the_build_fails(fixture: Fixture) -> None:
    fixture.write()
    (fixture.dir / f"pubdex-{TEST_VERSION}-source.tar.gz").write_bytes(b"other")
    assert any(problem.startswith(f"files.pubdex-{TEST_VERSION}-source.tar.gz") for problem in fixture.check())


def test_labels_that_disagree_with_the_dockerfiles_fail(fixture: Fixture) -> None:
    labels = _labels("postgres", TEST_VERSION, COMMIT + "-dirty")
    labels["org.opencontainers.image.base.digest"] = PY_DIGEST
    labels["pubdex.postgresql-version"] = "16.14"
    (fixture.facts / "postgres.labels.json").write_text(json.dumps(labels))
    fixture.write()
    problems = fixture.check()
    assert any("org.opencontainers.image.base.digest" in problem and DEB_DIGEST in problem for problem in problems)
    assert any("pubdex.postgresql-version is \"16.14\"" in problem for problem in problems)


def test_client_libraries_other_than_the_runtime_locks_fail(fixture: Fixture) -> None:
    (fixture.facts / "application.client.json").write_text(json.dumps({
        "psycopg": "3.3.5", "psycopg-c": "3.3.6", "psycopg-pool": "3.3.3", "implementation": "python",
        "libpq": 170011, "libpq_build": 170011}))
    fixture.write()
    problems = fixture.check()
    assert any("client_libraries.psycopg is \"3.3.5\"" in problem for problem in problems)
    assert any("C implementation" in problem for problem in problems)


def test_build_packages_without_the_compiler_fail(fixture: Fixture) -> None:
    (fixture.facts / "postgres.build-packages.txt").write_text("make 4.4.1-2\n")
    fixture.write()
    assert any("images.postgres.build_packages does not name gcc" in problem for problem in fixture.check())


# --- the SBOMs -----------------------------------------------------------------


def _edit_sbom(role: str, edit: Callable[[dict], None]) -> Callable[[Fixture], None]:
    def apply(fixture: Fixture) -> None:
        document = _sbom(role, fixture.version)
        edit(document)
        fixture.write_sbom(role, document)
    return apply


def _package_version(name: str, version: str) -> Callable[[dict], None]:
    def edit(document: dict) -> None:
        for package in document["packages"]:
            if package["name"] == name:
                package["versionInfo"] = version
    return edit


def _without(name: str) -> Callable[[dict], None]:
    def edit(document: dict) -> None:
        document["packages"] = [package for package in document["packages"] if package["name"] != name]
    return edit


SBOM_DEFECTS = {
    "other format": (_edit_sbom("application", _set("spdxVersion", "SPDX-2.2")), "spdxVersion"),
    "other tool": (_edit_sbom("postgres", _set("creationInfo.creators", ["Tool: syft-1.57.1"])), "pinned SBOM tool"),
    "other image": (_edit_sbom("application", _set("packages.0.name", "pubdex")), "does not describe one container"),
    "other version": (_edit_sbom("postgres", _set("packages.0.versionInfo", "0.1.0")), "does not describe one"),
    "no relationship": (_edit_sbom("postgres", _set("relationships", [])), "does not describe one"),
    "locked package missing": (_edit_sbom("application", _without("typing_extensions")),
                               "Python package typing-extensions as nothing"),
    "locked package version": (_edit_sbom("application", _package_version("psycopg-c", "3.3.5")),
                               "psycopg-c as ['3.3.5']"),
    "libpq5": (_edit_sbom("application", _package_version("libpq5", "17.6-0+deb13u1")), "libpq5"),
    "postgresql missing": (_edit_sbom("postgres", _without("postgresql")), "names PostgreSQL as nothing"),
    "postgresql version": (_edit_sbom("postgres", _package_version("postgresql", "16.14")), "['16.14']"),
}


@pytest.mark.parametrize("defect, message", SBOM_DEFECTS.values(), ids=SBOM_DEFECTS.keys())
def test_a_defective_sbom_fails_the_check(fixture: Fixture, defect: Callable[[Fixture], None], message: str) -> None:
    defect(fixture)
    fixture.write()
    problems = fixture.check()
    assert any(message in problem for problem in problems), problems


def test_an_sbom_that_is_no_json_fails(fixture: Fixture) -> None:
    fixture.sbom_path("postgres").write_text("<spdx/>")
    fixture.write()
    assert any("postgres-image.spdx.json is not JSON" in problem for problem in fixture.check())


def test_packages_outside_the_lock_are_named_but_not_refused(fixture: Fixture) -> None:
    # The base image's own packages -- wheel, say -- are in the SBOM too.
    fixture.write()
    assert fixture.check() == []
    assert "wheel" in fixture.sbom_path("application").read_text()


# --- the command line ----------------------------------------------------------


def _cli(fixture: Fixture, step: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), step, "--root", str(fixture.root), "--dir",
                           str(fixture.dir), "--facts", str(fixture.facts), "--version", fixture.version, "--commit",
                           COMMIT, "--revision", fixture.revision, "--epoch", EPOCH, *extra],
                          capture_output=True, text=True, timeout=60)


def test_the_command_line_writes_and_checks(fixture: Fixture) -> None:
    proc = _cli(fixture, "write", "--started", "2026-11-18T03:01:32Z", "--finished", "2026-11-18T03:01:45Z")
    assert proc.returncode == 0, proc.stderr
    assert f"wrote pubdex-{TEST_VERSION}-provenance.json" in proc.stdout
    assert _cli(fixture, "check").returncode == 0
    record = fixture.record()
    record["inputs"]["locks"]["DB/requirements.txt"] = "0" * 64
    fixture.save(record)
    proc = _cli(fixture, "check")
    assert proc.returncode == 1
    assert "release_provenance.py: inputs.locks.DB/requirements.txt: recorded" in proc.stderr


@pytest.mark.parametrize("extra, status, message", [
    ((), 2, "write needs --started and --finished"),
    (("--started", "now", "--finished", "later"), 1, "is not a UTC time"),
])
def test_the_command_line_refuses_a_write_without_times(fixture: Fixture, extra: tuple, status: int,
                                                        message: str) -> None:
    proc = _cli(fixture, "write", *extra)
    assert proc.returncode == status and message in proc.stderr
    assert not fixture.record_path.exists()


def test_an_identity_whose_revision_is_not_the_commit_is_refused(fixture: Fixture) -> None:
    fixture.revision = "f" * 40
    proc = _cli(fixture, "check")
    assert proc.returncode == 1 and "is not the commit" in proc.stderr


# --- the real checkout ---------------------------------------------------------


def test_the_real_dockerfiles_give_the_inputs_the_record_names() -> None:
    checkout = Checkout(ROOT)
    inputs = checkout.inputs()
    assert sorted(inputs["locks"]) == ["DB/requirements-build.txt", "DB/requirements-pip.txt", "DB/requirements.txt"]
    assert sorted(inputs["build_scripts"]) == ["DB/fetch_verified.py", "DB/install_locked.py", "DB/install_pip.py",
                                               "DB/lib/release_identity.sh", "DB/locked_files.py", "build_image.sh"]
    assert sorted(inputs["build_contexts"]) == [".dockerignore", "postgres.Dockerfile.dockerignore"]
    assert sorted(inputs["base_images"]) == ["DEBIAN_BASE", "POSTGRES_ENTRYPOINT_IMAGE", "PYTHON_BASE"]
    assert inputs["postgresql_source"]["url"].startswith("https://ftp.postgresql.org/pub/source/v16.")
    assert checkout.final_base("Dockerfile") == inputs["base_images"]["PYTHON_BASE"]
    assert checkout.final_base("postgres.Dockerfile") == inputs["base_images"]["DEBIAN_BASE"]
    for name in ("psycopg", "psycopg-c", "psycopg-pool"):
        assert name in checkout.lock_pins("DB/requirements.txt")


def test_the_pins_the_check_reads_are_the_ones_the_scripts_source() -> None:
    checkout = Checkout(ROOT)
    syft = checkout.shell_pins("DB/lib/sbom_tool.sh")
    assert set(syft) == {"PINNED_SYFT_VERSION", "PINNED_SYFT_IMAGE", "PINNED_SYFT_INDEX_DIGEST", "PINNED_SYFT_IMAGE_ID"}
    assert syft["PINNED_SYFT_IMAGE"].endswith("@" + syft["PINNED_SYFT_INDEX_DIGEST"])
    assert f":v{syft['PINNED_SYFT_VERSION']}@" in syft["PINNED_SYFT_IMAGE"]
    assert syft["PINNED_SYFT_IMAGE"].startswith("docker.io/anchore/syft:")
    helm = checkout.shell_pins("DB/lib/cluster_tools.sh")
    assert {"PINNED_HELM_VERSION", "PINNED_HELM_MEMBER_SHA256"} <= set(helm)
