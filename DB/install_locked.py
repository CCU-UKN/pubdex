#!/usr/bin/env python3
"""Install a hashed lock of DB/ from a wheelhouse, with psycopg's C implementation built against the system's libpq.

    python3 -m venv --without-pip .venv
    python3 DB/fetch_verified.py wheels --dest <wheelhouse> --allow-source psycopg-c \\
        DB/requirements-pip.txt DB/requirements-build.txt DB/requirements-dev.txt
    .venv/bin/python DB/install_pip.py --wheelhouse <wheelhouse>
    .venv/bin/python DB/install_locked.py --wheelhouse <wheelhouse> DB/requirements-dev.txt

installs every package of the lock into the environment of the Python that
runs this script, with the pip DB/install_pip.py installed there, and
downloads nothing:

1. It checks the wheelhouse: exactly one file for every pin of the pip lock,
   the build lock and the given lock, each matching its version and one of
   its hashes; a wheel, but for psycopg-c, whose source archive it holds.
   A missing, extra or changed file stops it before anything is installed.
2. With --psycopg c, the default, it builds psycopg-c from that verified
   source archive into a wheel, in a temporary environment of its own: pip,
   then the build tools of the build lock, each from its hashed wheel, then
   `pip wheel` without build isolation, with the build dependencies checked,
   compiled against the libpq whose pg_config comes first on PATH (or
   --pg-config names) -- the system's libpq, in the image Debian's. The
   build sees no HOME of the caller's and no PYTHON* variable.
   --build-only <directory> stops there, leaving the wheel and a record of
   it -- the source archive's SHA-256, the wheel's and the pg_config used --
   in that new directory; --built <directory> installs such a wheel instead
   of building one. That is how the image builds psycopg-c in a stage with a
   compiler and installs it in one without (Dockerfile).
3. It installs the lock from the wheelhouse alone (--no-index), in
   hash-checking mode, from binary wheels only and without dependencies; the
   locally built psycopg-c wheel is installed by the SHA-256 recorded when it
   was built from the verified archive, the narrow exception to "every file
   by a hash the lock lists". With --psycopg python it leaves psycopg-c out:
   psycopg then uses its pure-Python implementation, which loads the
   system's libpq at run time.
4. It checks that this Python imports every package of the lock at its
   locked version from its own site-packages, that no psycopg-binary is
   installed, and that psycopg uses the requested implementation; it prints
   the libpq version psycopg loads.

Every pip and build step runs with pip's environment cleaned as in
DB/install_pip.py -- no PIP_* variable, PIP_CONFIG_FILE=os.devnull,
--isolated -- and without network access where the system allows it (see
--network-isolation there). Paths given are taken from the caller's
directory, wherever pip and the build then run, and the build environment's
pip is the one --pip-lock fixes. Needs nothing outside the standard library;
a C compiler, Python's headers and libpq's headers with pg_config for
--psycopg c. Exits with 1, naming the step, if any of them fails.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from locked_files import (Isolation, LockError, Pin, find_isolation, merge_locks,  # noqa: E402
                          check_wheelhouse, pip_environment, read_lock, run_offline, sha256_of)

HERE = Path(__file__).resolve().parent
PSYCOPG_C = "psycopg-c"
RECORD = "psycopg-c.json"
PIP_OFFLINE = ["--isolated", "--no-index", "--no-cache-dir", "--disable-pip-version-check"]
INSTALL = ["install", *PIP_OFFLINE, "--root-user-action=ignore"]


class InstallError(Exception):
    """The lock cannot be installed as it requires."""


class Offline:
    """Runs commands as run_offline does, with one decision about isolation."""

    def __init__(self, required: bool) -> None:
        self.required = required
        self.isolation: Isolation = find_isolation()
        print(f"install_locked.py: {self.isolation.description}", flush=True)

    def run(self, command: List[str], what: str, **kwargs) -> subprocess.CompletedProcess:
        try:
            done = run_offline(command, required=self.required, isolation=self.isolation, **kwargs)
        except LockError as exc:
            raise InstallError(str(exc)) from None
        if done.returncode != 0:
            raise InstallError(f"{what} failed (exit status {done.returncode})")
        return done


def build_environment(home: Path, pg_config: Optional[Path]) -> Dict[str, str]:
    """pip's clean environment for the build of psycopg-c: no PYTHON* variable,
    an empty HOME, and the directory of the chosen pg_config first on PATH."""
    environment = {name: value for name, value in pip_environment().items() if not name.startswith("PYTHON")}
    environment["HOME"] = str(home)
    if pg_config is not None:
        environment["PATH"] = os.pathsep.join([str(pg_config.parent), environment.get("PATH", os.defpath)])
    return environment


def describe_pg_config(environment: Dict[str, str], chosen: Optional[Path] = None) -> Dict[str, str]:
    """The pg_config the build finds first on the environment's PATH, which
    has to be the chosen one if --pg-config names one: psycopg-c's build
    looks it up by that name, so a file called otherwise would leave the build
    with whichever pg_config comes next on PATH."""
    pg_config = shutil.which("pg_config", path=environment.get("PATH"))
    if chosen is not None and (pg_config is None or not os.path.samefile(pg_config, chosen)):
        raise InstallError(f"--pg-config {chosen} is not the pg_config the build finds first on its PATH "
                           f"({pg_config or 'none'}); name an executable file called pg_config")
    if pg_config is None:
        raise InstallError("no pg_config on PATH: building psycopg-c needs libpq's headers and pg_config "
                           "(libpq-dev on Debian and Ubuntu); --pg-config names one, and --psycopg python "
                           "installs psycopg without its C implementation")
    found = {"pg_config": pg_config}
    for option in ("version", "includedir", "libdir"):
        done = subprocess.run([pg_config, f"--{option}"], capture_output=True, text=True, env=environment)
        if done.returncode != 0:
            raise InstallError(f"{pg_config} --{option} failed")
        found[option] = done.stdout.strip()
    return found


def build_psycopg_c(wheelhouse: Path, sdist: Path, pin: Pin, pip_lock: Path, build_lock: Path, out: Path,
                    offline: Offline, pg_config: Optional[Path]) -> Dict[str, str]:
    """Builds psycopg-c from the verified source archive into out; its record."""
    with tempfile.TemporaryDirectory(prefix="install-locked-build-") as directory:
        work = Path(directory)
        home, built = work / "home", work / "wheel"
        home.mkdir()
        built.mkdir()
        environment = build_environment(home, pg_config)
        config = describe_pg_config(environment, pg_config)
        print(f"install_locked.py: building {sdist.name} against {config['version']} "
              f"({config['pg_config']}, libraries in {config['libdir']})", flush=True)
        if subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(work / "env")]).returncode != 0:
            raise InstallError("cannot create the build environment")
        python = str(work / "env" / "bin" / "python")
        offline.run([python, str(HERE / "install_pip.py"), "--wheelhouse", str(wheelhouse), "--lock", str(pip_lock),
                     "--network-isolation", "required" if offline.required else "preferred"],
                    "installing pip into the build environment")
        offline.run([python, "-I", "-m", "pip", *INSTALL, "--find-links", str(wheelhouse),
                     "--require-hashes", "--only-binary=:all:", "--no-deps", "--requirement", str(build_lock)],
                    "installing the build tools of " + build_lock.name, env=pip_environment())
        requirement = work / "psycopg-c.txt"
        requirement.write_text(f"{PSYCOPG_C}=={pin.version} "
                               + " ".join(f"--hash=sha256:{h}" for h in sorted(pin.hashes)) + "\n",
                               encoding="utf-8")
        offline.run([python, "-I", "-m", "pip", "wheel", *PIP_OFFLINE, "--find-links", str(wheelhouse),
                     "--require-hashes", f"--no-binary={PSYCOPG_C}", "--no-deps", "--no-build-isolation",
                     "--check-build-dependencies", "--wheel-dir", str(built), "--requirement", str(requirement)],
                    f"building {sdist.name}", env=environment, cwd=str(home))
        wheels = sorted(built.glob("psycopg_c-*.whl"))
        if len(wheels) != 1 or len(list(built.iterdir())) != 1:
            raise InstallError(f"the build left {sorted(p.name for p in built.iterdir())}, not one psycopg-c wheel")
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(wheels[0], out / wheels[0].name)
    record = {"version": pin.version, "sdist": sdist.name, "sdist_sha256": sha256_of(sdist),
              "wheel": wheels[0].name, "wheel_sha256": sha256_of(out / wheels[0].name),
              "pg_config_version": config["version"], "pg_config_libdir": config["libdir"]}
    (out / RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"install_locked.py: built {record['wheel']}, SHA-256 {record['wheel_sha256']}, from {sdist.name}, "
          f"SHA-256 {record['sdist_sha256']}", flush=True)
    return record


def read_built(directory: Path, sdist: Path, pin: Pin) -> Dict[str, str]:
    """The record of a psycopg-c wheel built from the verified archive, checked."""
    try:
        names = sorted(p.name for p in directory.iterdir())
        record = json.loads((directory / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"{directory} holds no readable {RECORD}: {exc}") from None
    if names != sorted([RECORD, str(record.get("wheel"))]):
        raise InstallError(f"{directory} has to hold {RECORD} and the wheel it names, nothing else: {names}")
    if record.get("version") != pin.version or record.get("sdist_sha256") not in pin.hashes \
            or record.get("sdist_sha256") != sha256_of(sdist):
        raise InstallError(f"{directory / RECORD} does not describe a build of the locked {sdist.name}")
    if sha256_of(directory / record["wheel"]) != record.get("wheel_sha256"):
        raise InstallError(f"{record['wheel']} in {directory} is not the wheel its record names")
    return record


def requirements_for(target: Dict[str, Pin], implementation: str,
                     record: Dict[str, str]) -> Tuple[List[str], Dict[str, str]]:
    """The requirements pip installs, every one with its version and hashes,
    and the versions they install: the lock's, but psycopg-c, which is
    installed by the SHA-256 of the wheel built from its verified archive
    with --psycopg c, and left out with --psycopg python. Extras go: pip
    installs without dependencies, and every package an extra needs is in the
    lock already."""
    requirements, locked = [], {}
    for name, pin in sorted(target.items()):
        if name == PSYCOPG_C:
            if implementation != "c":
                continue
            hashes = [record["wheel_sha256"]]
        else:
            hashes = sorted(pin.hashes)
        requirements.append(f"{name}=={pin.version} " + " ".join(f"--hash=sha256:{h}" for h in hashes))
        locked[name] = pin.version
    return requirements, locked


_CHECK = """\
import importlib.metadata, json, os, sys, sysconfig
locked, implementation = json.loads(sys.argv[1]), sys.argv[2]
sites = {os.path.realpath(sysconfig.get_path(p)) for p in ("purelib", "platlib")}
problems = []
for name, version in sorted(locked.items()):
    try:
        found = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        problems.append(f"{name}: not installed"); continue
    where = os.path.realpath(str(found.locate_file("")))
    if found.version != version or where not in sites:
        problems.append(f"{name}: {found.version} in {where}, locked {version} in {' or '.join(sorted(sites))}")
try:
    importlib.metadata.distribution("psycopg-binary")
    problems.append("psycopg-binary is installed")
except importlib.metadata.PackageNotFoundError:
    pass
if "psycopg" in locked:
    import psycopg
    if psycopg.pq.__impl__ != implementation:
        problems.append(f"psycopg uses its {psycopg.pq.__impl__} implementation, not {implementation}")
    if psycopg.pq.version() < 160000:
        problems.append(f"psycopg loads libpq {psycopg.pq.version()}, older than 16, which lacks the "
                        "require_auth setting every PubDex connection makes")
    print(f"psycopg {psycopg.__version__}, {psycopg.pq.__impl__} implementation, libpq {psycopg.pq.version()}"
          + (f", built against libpq {psycopg.pq.__build_version__}" if implementation == "c" else ""))
print(*problems, sep="\\n")
sys.exit(1 if problems else 0)
"""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    parser.add_argument("--wheelhouse", type=Path, required=True,
                        help="the directory DB/fetch_verified.py fetched the locked files into")
    parser.add_argument("--pip-lock", type=Path, default=HERE / "requirements-pip.txt")
    parser.add_argument("--build-lock", type=Path, default=HERE / "requirements-build.txt")
    parser.add_argument("--psycopg", choices=("c", "python"), default="c",
                        help="psycopg's implementation: c, built against the system's libpq (default), "
                             "or python, which loads the system's libpq at run time")
    parser.add_argument("--pg-config", type=Path, help="the pg_config of the libpq to build against")
    parser.add_argument("--network-isolation", choices=("preferred", "required"), default="preferred",
                        help="whether pip may run where no network isolation is possible (default: %(default)s)")
    stage = parser.add_mutually_exclusive_group()
    stage.add_argument("--build-only", type=Path, metavar="DIRECTORY",
                       help="build psycopg-c into this new directory and install nothing")
    stage.add_argument("--built", type=Path, metavar="DIRECTORY",
                       help="install the psycopg-c wheel --build-only left in this directory")
    parser.add_argument("lock", type=Path)
    args = parser.parse_args(argv)
    # Every path given is the caller's, relative to the caller's directory;
    # pip and the build run in directories of their own.
    for name in ("wheelhouse", "pip_lock", "build_lock", "pg_config", "build_only", "built", "lock"):
        if getattr(args, name) is not None:
            setattr(args, name, getattr(args, name).absolute())
    try:
        locks = {path: read_lock(path) for path in (args.pip_lock, args.build_lock, args.lock)}
        target = locks[args.lock]
        pins = merge_locks(locks)
        files = check_wheelhouse(args.wheelhouse, pins, [PSYCOPG_C] if PSYCOPG_C in pins else [])
        print(f"install_locked.py: {args.wheelhouse} holds exactly the {len(files)} files the locks fix", flush=True)
        if (args.build_only or args.built) and (PSYCOPG_C not in target or args.psycopg != "c"):
            raise InstallError("--build-only and --built are for a lock that pins psycopg-c, with --psycopg c")
        if args.build_only and args.build_only.exists() and any(args.build_only.iterdir()):
            raise InstallError(f"{args.build_only} has to be a new or empty directory")
        offline = Offline(args.network_isolation == "required")
        with tempfile.TemporaryDirectory(prefix="install-locked-") as directory:
            work = Path(directory)
            built: Optional[Path] = None
            record: Dict[str, str] = {}
            if PSYCOPG_C in target and args.psycopg == "c":
                if args.built:
                    built, record = args.built, read_built(args.built, files[PSYCOPG_C], target[PSYCOPG_C])
                else:
                    built = args.build_only or work / "built"
                    record = build_psycopg_c(args.wheelhouse, files[PSYCOPG_C], target[PSYCOPG_C], args.pip_lock,
                                             args.build_lock, built, offline, args.pg_config)
                if args.build_only:
                    return 0
            requirements, locked = requirements_for(target, args.psycopg, record)
            derived = work / ("installed-" + args.lock.name)
            derived.write_text("\n".join(requirements) + "\n", encoding="utf-8")
            command = [sys.executable, "-I", "-m", "pip", *INSTALL,
                       "--find-links", str(args.wheelhouse)]
            if built is not None:
                command += ["--find-links", str(built)]
            command += ["--require-hashes", "--only-binary=:all:", "--no-deps", "--requirement", str(derived)]
            offline.run(command, f"installing {args.lock.name}", env=pip_environment())
        environment = pip_environment()
        environment["PSYCOPG_IMPL"] = args.psycopg
        check = subprocess.run([sys.executable, "-I", "-c", _CHECK, json.dumps(locked), args.psycopg],
                               capture_output=True, text=True, env=environment)
        if check.returncode != 0:
            raise InstallError(f"pip reported success, but {sys.executable} does not import the lock as "
                               f"installed: {(check.stdout + check.stderr).strip()}")
        print(f"install_locked.py: {len(locked)} packages of {args.lock.name} installed for {sys.executable}; "
              + check.stdout.strip())
    except (InstallError, LockError) as exc:
        print(f"install_locked.py: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"install_locked.py: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
