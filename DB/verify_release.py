#!/usr/bin/env python3
"""Verify release downloads or a saved image's config using only the standard library."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile


def required_downloads(version: str) -> tuple[str, ...]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("invalid release version")
    return (f"pubdex-{version}-source.tar.gz", f"pubdex-{version}.tgz",
            f"pubdex-{version}-image.spdx.json", f"pubdex-{version}-postgres-image.spdx.json",
            f"pubdex-{version}-provenance.json")


def verify_downloads(directory: Path, checksums: Path, version: str) -> None:
    entries = {}
    for line in checksums.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *]([^/\\]+)", line)
        if not match or match[2] in entries:
            raise ValueError("malformed or duplicate checksum entry")
        entries[match[2]] = match[1]
    for name in required_downloads(version):
        if name not in entries:
            raise ValueError(f"checksum file has no {name}")
        digest = hashlib.sha256()
        with (directory / name).open("rb") as source:
            for block in iter(lambda: source.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != entries[name]:
            raise ValueError(f"{name}: checksum mismatch")
        print(f"{name}: OK")


def config_digest(archive: Path) -> str:
    """Hash the actual config bytes, independent of the saving Engine's store.

    Docker save supplies manifest.json for both classic and containerd stores;
    Config may name a legacy config.json or an OCI blobs/sha256 member.
    Nothing is extracted to disk and image layers are never read.
    """
    with tarfile.open(archive) as saved:
        manifest_file = saved.extractfile("manifest.json")
        if manifest_file is None:
            raise ValueError("saved image has no manifest.json")
        manifests = json.load(manifest_file)
        if not isinstance(manifests, list) or len(manifests) != 1:
            raise ValueError("expected exactly one saved image")
        config = saved.extractfile(manifests[0]["Config"])
        if config is None:
            raise ValueError("saved image has no config bytes")
        return "sha256:" + hashlib.sha256(config.read()).hexdigest()


def verify_config(archive: Path, provenance: Path, role: str) -> str:
    record = json.loads(provenance.read_text(encoding="utf-8"))
    if record["format"] == "pubdex-provenance/2":
        expected = record["images"][role]["config_digest"]
    elif record["format"] == "pubdex-provenance/1" and record["build"]["image_store"] == "classic":
        expected = record["images"][role]["id"]
    else:
        raise ValueError("record has no verifiable config digest (legacy containerd or unknown format)")
    if not isinstance(expected, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise ValueError(f"{role}: invalid recorded config digest")
    actual = config_digest(archive)
    if actual != expected:
        raise ValueError(f"{role}: {actual} != {expected}")
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    files = commands.add_parser("files", help="require and hash all five downloads; IMAGES is optional")
    files.add_argument("--version", required=True)
    files.add_argument("--checksums", type=Path, required=True)
    files.add_argument("--directory", type=Path, default=Path.cwd())
    config = commands.add_parser("image-config", help="hash a Docker saved image, optionally checking provenance")
    config.add_argument("archive", type=Path)
    config.add_argument("--provenance", type=Path)
    config.add_argument("--role", choices=("application", "postgres"))
    args = parser.parse_args()
    if bool(getattr(args, "provenance", None)) != bool(getattr(args, "role", None)):
        parser.error("--provenance and --role must be supplied together")
    try:
        if args.command == "files":
            verify_downloads(args.directory, args.checksums, args.version)
        elif args.provenance:
            print(f"{args.role}: {verify_config(args.archive, args.provenance, args.role)} OK")
        else:
            print(config_digest(args.archive))
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        print(f"verify_release.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
