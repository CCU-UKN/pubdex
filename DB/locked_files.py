"""What the hashed locks of DB/ fix, for the scripts that fetch and install them.

DB/fetch_verified.py downloads the files the locks name into a wheelhouse,
a directory of its own; DB/install_pip.py and DB/install_locked.py install
from that directory alone, with the network out of reach where the system
allows it. This module holds what the three share, with nothing outside the
standard library:

- read_lock(): a lock as pip-compile writes it with --generate-hashes --
  every requirement name==version, optionally with extras, followed by the
  SHA-256 hashes of its files -- and nothing else: an option, an environment
  marker, a URL or a requirement without hashes is refused;
- the names of distribution files, and whether a file suits the Python that
  will install it (Target): a pure-Python wheel, a wheel for this CPython and
  a glibc-based Linux no newer than the target's, or -- only for the names a
  caller allows -- a source archive;
- pip_environment(): the environment pip runs in, without any of pip's own
  variables and with PIP_CONFIG_FILE naming os.devnull, so that no
  configuration file or variable can send an installation elsewhere or add
  an index;
- network isolation: run_offline() runs a command where it cannot reach the
  network -- in this process's network namespace when that has no interface
  but loopback (BuildKit's RUN --network=none), else in a new user and
  network namespace (unshare) when the system permits one, which is checked
  before use. Where neither is possible it says so, and with
  required=True refuses to run: pip's --no-index keeps pip from asking an
  index, but is no sandbox.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple


class LockError(Exception):
    """A lock, a wheelhouse or a file is not what the locks fix."""


def canonical(name: str) -> str:
    """The normalised project name of PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Pin:
    name: str             # canonical
    version: str
    hashes: FrozenSet[str]
    extras: Tuple[str, ...] = ()


_REQUIREMENT = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[(?P<extras>[A-Za-z0-9._,-]*)\])?==(?P<version>[0-9A-Za-z.!+_-]+)")
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def read_lock(path: Path) -> Dict[str, Pin]:
    """The pins of a hashed lock, by canonical name."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise LockError(f"cannot read the lock {path}: {exc.strerror or exc}") from None
    pins: Dict[str, Pin] = {}
    for line in re.sub(r"\\\n", " ", text).splitlines():
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        match = _REQUIREMENT.fullmatch(words[0])
        hashes = [_HASH.fullmatch(word) for word in words[1:]]
        if match is None or not hashes or not all(hashes):
            raise LockError(f"the lock {path} has a line that is not name==version followed by "
                            f"--hash=sha256:<hash> options alone: {line.strip()!r}")
        name = canonical(match["name"])
        if name in pins:
            raise LockError(f"the lock {path} pins {name} twice")
        extras = tuple(sorted(canonical(e) for e in (match["extras"] or "").split(",") if e))
        pins[name] = Pin(name, match["version"], frozenset(m.group(1) for m in hashes if m), extras)
    if not pins:
        raise LockError(f"the lock {path} pins nothing")
    return pins


def merge_locks(locks: Dict[Path, Dict[str, Pin]]) -> Dict[str, Pin]:
    """One pin per name over several locks: the same version everywhere, and
    the hashes every lock that names it lists, so that a file chosen by them
    satisfies each of those locks."""
    merged: Dict[str, Pin] = {}
    for path, pins in locks.items():
        for name, pin in pins.items():
            seen = merged.get(name)
            if seen is None:
                merged[name] = Pin(name, pin.version, pin.hashes)
                continue
            if seen.version != pin.version:
                raise LockError(f"{path} pins {name}=={pin.version}, another lock {name}=={seen.version}")
            common = seen.hashes & pin.hashes
            if not common:
                raise LockError(f"the locks list no common hash for {name}=={pin.version}")
            merged[name] = Pin(name, pin.version, common)
    return merged


# --- distribution files --------------------------------------------------------------------


@dataclass(frozen=True)
class FileName:
    name: str        # canonical
    version: str
    kind: str        # "wheel" or "sdist"
    tags: FrozenSet[Tuple[str, str, str]] = frozenset()


def parse_filename(filename: str) -> Optional[FileName]:
    """The project, version and kind of a distribution file's name, with the
    tags of a wheel; None for anything else."""
    if filename.endswith(".whl"):
        parts = filename[:-4].split("-")
        if len(parts) not in (5, 6):
            return None
        name, version, python, abi, plat = parts[0], parts[1], parts[-3], parts[-2], parts[-1]
        tags = frozenset((p, a, q) for p in python.split(".") for a in abi.split(".") for q in plat.split("."))
        return FileName(canonical(name), version, "wheel", tags)
    if filename.endswith(".tar.gz"):
        name, _, version = filename[:-7].rpartition("-")
        if not name or not version:
            return None
        return FileName(canonical(name), version, "sdist")
    return None


_MANYLINUX_ALIASES = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}


@dataclass(frozen=True)
class Target:
    """The Python that will install the files: CPython major.minor, the
    machine and the glibc version."""
    python: Tuple[int, int]
    machine: str
    glibc: Tuple[int, int]

    @classmethod
    def current(cls) -> "Target":
        libc = (os.confstr("CS_GNU_LIBC_VERSION") if hasattr(os, "confstr") else "") or ""
        found = re.fullmatch(r"glibc (\d+)\.(\d+)", libc)
        return cls(sys.version_info[:2], platform.machine(),
                   (int(found[1]), int(found[2])) if found else (0, 0))

    def describe(self) -> str:
        glibc = f"glibc {self.glibc[0]}.{self.glibc[1]}" if self.glibc != (0, 0) else "no glibc"
        return f"CPython {self.python[0]}.{self.python[1]}, {self.machine}, {glibc}"

    def _platform_ok(self, plat: str) -> bool:
        if plat == "any":
            return True
        alias = re.fullmatch(r"(manylinux1|manylinux2010|manylinux2014)_(.+)", plat)
        if alias:
            needed, machine = _MANYLINUX_ALIASES[alias[1]], alias[2]
        else:
            found = re.fullmatch(r"manylinux_(\d+)_(\d+)_(.+)", plat)
            if not found:
                return False  # musllinux, linux_*, macOS, Windows ...
            needed, machine = (int(found[1]), int(found[2])), found[3]
        return machine == self.machine and self.glibc != (0, 0) and needed <= self.glibc

    def accepts(self, tags: Iterable[Tuple[str, str, str]]) -> bool:
        major, minor = self.python
        cpython = f"cp{major}{minor}"
        for python, abi, plat in tags:
            pure = (python in (f"py{major}", f"py{major}{minor}")
                    or any(python == f"py{major}{m}" for m in range(minor + 1)))
            if pure and abi == "none" and plat == "any":
                return True
            if not self._platform_ok(plat):
                continue
            if python == cpython and abi in (cpython, "abi3", "none"):
                return True
            found = re.fullmatch(rf"cp{major}(\d+)", python)
            if found and int(found[1]) <= minor and abi == "abi3":
                return True
        return False


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_wheelhouse(directory: Path, pins: Dict[str, Pin], sources: Iterable[str] = ()) -> Dict[str, Path]:
    """The one file of each pin in directory, by canonical name. Every file
    there has to belong to exactly one pin, match its version and one of its
    hashes, and be a wheel -- a source archive only for the names in sources;
    a missing, extra, unreadable or changed file is an error, and so is
    anything in the directory that is not a regular file."""
    sources = {canonical(name) for name in sources}
    try:
        entries = sorted(Path(directory).iterdir())
    except OSError as exc:
        raise LockError(f"cannot read the wheelhouse {directory}: {exc.strerror or exc}") from None
    found: Dict[str, Path] = {}
    problems: List[str] = []
    for entry in entries:
        parsed = parse_filename(entry.name)
        pin = pins.get(parsed.name) if parsed else None
        if entry.is_symlink() or not entry.is_file():
            problems.append(f"{entry.name}: not a regular file")
        elif pin is None:
            problems.append(f"{entry.name}: no lock pins it")
        elif parsed.version != pin.version:
            problems.append(f"{entry.name}: version {parsed.version}, locked {pin.version}")
        elif parsed.kind == "sdist" and pin.name not in sources:
            problems.append(f"{entry.name}: a source archive, and {pin.name} is installed from a wheel only")
        elif pin.name in found:
            problems.append(f"{entry.name}: a second file for {pin.name} ({found[pin.name].name})")
        elif sha256_of(entry) not in pin.hashes:
            problems.append(f"{entry.name}: its SHA-256 {sha256_of(entry)} is not one the locks list")
        else:
            found[pin.name] = entry
    problems += [f"{name}=={pin.version}: no file in the wheelhouse"
                 for name, pin in sorted(pins.items()) if name not in found]
    if problems:
        listed = "".join(f"\n  {problem}" for problem in problems)
        raise LockError(f"the wheelhouse {directory} is not exactly the files the locks fix, as follows.{listed}")
    return found


# --- running pip ---------------------------------------------------------------------------


def pip_environment(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment pip runs in: base (this one by default) without pip's
    own variables -- PIP_*, the _PIP_* it reads internally and
    ENSUREPIP_OPTIONS -- and with PIP_CONFIG_FILE naming os.devnull, which
    keeps pip from reading any configuration file. pip's --isolated alone
    would not: it leaves the global and site files and PIP_CONFIG_FILE's in
    effect."""
    environment = {name: value for name, value in (os.environ if base is None else base).items()
                   if not name.startswith(("PIP_", "_PIP_")) and name != "ENSUREPIP_OPTIONS"}
    environment["PIP_CONFIG_FILE"] = os.devnull
    return environment


# --- network isolation ---------------------------------------------------------------------

_INTERFACES = """\
with open("/proc/self/net/dev", encoding="ascii") as dev:
    print(" ".join(sorted(line.split(":", 1)[0].strip() for line in dev.readlines()[2:])))
"""


def interfaces() -> Optional[List[str]]:
    """The network interfaces of this process's network namespace, or None
    where Linux's /proc does not say."""
    try:
        with open("/proc/self/net/dev", encoding="ascii") as dev:
            return sorted(line.split(":", 1)[0].strip() for line in dev.readlines()[2:])
    except OSError:
        return None


@dataclass(frozen=True)
class Isolation:
    prefix: Tuple[str, ...]  # what to put in front of a command
    isolated: bool
    description: str


def find_isolation(unshare: Optional[str] = None) -> Isolation:
    """How a command can run without network access here."""
    if interfaces() == ["lo"]:
        return Isolation((), True, "network isolation: this process's network namespace has no "
                                   "interface but loopback")
    unshare = unshare or shutil.which("unshare")
    if unshare:
        for flags in (("--user", "--map-current-user", "--net"), ("--user", "--map-root-user", "--net")):
            try:
                probe = subprocess.run([unshare, *flags, "--", sys.executable, "-I", "-c", _INTERFACES],
                                       capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                continue
            if probe.returncode == 0 and probe.stdout.split() == ["lo"]:
                return Isolation((unshare, *flags, "--"), True,
                                 "network isolation: a new network namespace with no interface but loopback "
                                 f"({os.path.basename(unshare)} {' '.join(flags)})")
    return Isolation((), False, "NO network isolation: this system permits no network namespace for this "
                                "user; pip runs with --no-index, which keeps it from asking an index, but "
                                "that is not a network sandbox")


def run_offline(command: Sequence[str], *, required: bool, isolation: Optional[Isolation] = None,
                **kwargs) -> subprocess.CompletedProcess:
    """Runs command without network access where possible (find_isolation);
    with required=True only then."""
    isolation = isolation or find_isolation()
    if not isolation.isolated and required:
        raise LockError(isolation.description + "; refusing to install (--network-isolation required)")
    return subprocess.run([*isolation.prefix, *command], **kwargs)
