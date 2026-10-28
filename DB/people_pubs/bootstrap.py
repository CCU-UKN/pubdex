"""
people_pubs.bootstrap

`python -m people_pubs.bootstrap <directory>`: makes <directory> hold exactly
the schema bootstrap this package is shipped with -- the files of DB/init/
next to it, which the application image holds in /opt/pubdex/init.

The Compose deployment (compose.yaml) runs it as its one-shot `bootstrap`
service before PostgreSQL starts. <directory> is a volume that PostgreSQL
mounts read-only at /docker-entrypoint-initdb.d and whose files it runs once,
in name order, when it initialises an empty data directory. The schema is
therefore the one of the image in use, and no file of a checkout of the
repository takes part; a database that is initialised already is not
affected by what the volume holds.

Each file is written under a temporary name that starts with a dot, which
PostgreSQL's entrypoint does not run, and then renamed into place, so nothing
ever reads a partial file. Shell scripts get mode 755 -- set_passwords.sh
runs 020_role_passwords.sh --stdin as a program -- and every other
file 644, whatever the modes of the source. Everything else in <directory>
is removed: a file of an earlier image, or a temporary file of an
interrupted copy. A subdirectory there is an error, and so is a source that
holds anything but regular files, or no .sql file.

It prints the SHA-256 of each file, which names exactly what PostgreSQL will
run, and uses only the standard library. Exit status 0 on success, 1 when
the directory cannot be made to hold the files, 2 for a usage error.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

#: DB/init/ next to the package: /opt/pubdex/init in the image.
SOURCE = Path(__file__).resolve().parents[1] / "init"
TEMPORARY_PREFIX = "."
SCRIPT_SUFFIX = ".sh"


class BootstrapError(Exception):
    """Why the directory does not hold the files; names paths, never contents."""


def _reason(exc: OSError) -> str:
    return exc.strerror or str(exc)


def source_files(source: Path) -> List[Path]:
    """The files of the schema bootstrap in source, in name order."""
    try:
        entries = sorted(source.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        raise BootstrapError(f"cannot list the schema bootstrap {source}: {_reason(exc)}") from None
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise BootstrapError(f"{entry} is not a regular file")
    if not any(entry.suffix == ".sql" for entry in entries):
        raise BootstrapError(f"{source} holds no .sql file to bootstrap from")
    return entries


def _write(target: Path, name: str, data: bytes, mode: int) -> None:
    temporary = target / f"{TEMPORARY_PREFIX}{name}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)  # whatever the umask
        os.replace(temporary, target / name)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        hint = ""
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            hint = ("; the directory has to be writable for this user (uid 10001 in the image): Docker gives a new "
                    "volume the owner of the directory it is first mounted on, which compose.yaml makes this "
                    "service's mount point")
        raise BootstrapError(f"cannot write {target / name}: {_reason(exc)}{hint}") from None


def install(source: Path, target: Path) -> List[Tuple[str, str]]:
    """Makes target hold exactly the files of source; (name, SHA-256) of each."""
    files = source_files(source)
    if not target.is_dir():
        raise BootstrapError(f"{target} is not a directory")
    installed = []
    for file in files:
        try:
            data = file.read_bytes()
        except OSError as exc:
            raise BootstrapError(f"cannot read {file}: {_reason(exc)}") from None
        _write(target, file.name, data, 0o755 if file.suffix == SCRIPT_SUFFIX else 0o644)
        installed.append((file.name, hashlib.sha256(data).hexdigest()))
    names = {name for name, _digest in installed}
    try:
        for entry in sorted(target.iterdir(), key=lambda entry: entry.name):
            if entry.name in names:
                continue
            if entry.is_dir() and not entry.is_symlink():
                raise BootstrapError(f"{entry} is a directory, which the schema bootstrap never holds; remove it")
            entry.unlink()
        descriptor = os.open(target, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BootstrapError(f"cannot clear {target} of other files: {_reason(exc)}") from None
    return installed


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m people_pubs.bootstrap",
        description="Makes a directory hold exactly the schema bootstrap of this image, the files PostgreSQL runs "
                    "when it initialises an empty database.",
    )
    parser.add_argument("directory", type=Path,
                        help="the directory to fill: the volume PostgreSQL mounts at /docker-entrypoint-initdb.d")
    args = parser.parse_args(argv)
    try:
        installed = install(SOURCE, args.directory)
    except BootstrapError as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        return 1
    for name, digest in installed:
        print(f"bootstrap: {name} sha256:{digest}")
    print(f"bootstrap: {args.directory} holds the {len(installed)} files of this image's schema bootstrap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
