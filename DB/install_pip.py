#!/usr/bin/env python3
"""Install the pip that requirements-pip.txt pins, from a wheelhouse, without running another pip.

    python3 -m venv --without-pip .venv
    python3 DB/fetch_verified.py wheels --dest <wheelhouse> DB/requirements-pip.txt ...
    .venv/bin/python DB/install_pip.py --wheelhouse <wheelhouse>

installs pip into the environment of the Python that runs this script: a new
virtual environment, as above, or the application image's Python
(Dockerfile). This script runs the locked pip directly instead of the pip
bundled with ensurepip or the base image. It downloads nothing:
DB/fetch_verified.py has fetched the wheel before.

1. It reads the lock: exactly one requirement, pip==<version>, with the
   SHA-256 hashes of its files on the package index.
2. It takes pip-<version>-py3-none-any.whl from the wheelhouse and compares
   the file's SHA-256 with the lock's hashes. Nothing from the file runs
   unless one of them matches.
3. It copies the wheel into a new temporary directory and runs pip from it
   to install the wheel itself, from that directory alone (--no-index), in
   hash-checking mode, from binary wheels only and without dependencies, and
   removes the directory. pip runs without any of its environment variables
   and with PIP_CONFIG_FILE set to os.devnull, so it reads no configuration
   file either: none of them can send the installation elsewhere or add an
   index. It runs without network access where the system allows it
   (DB/locked_files.py, run_offline): in a network namespace with nothing
   but loopback. --network-isolation required refuses to install where that
   is not possible; preferred, the default, says so and installs with
   --no-index alone, which is no network sandbox.
4. It checks that this Python now imports pip of the locked version from its
   own site-packages, and fails otherwise.

Needs nothing outside the standard library; exits with 1, naming the step,
if any of them fails.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from locked_files import (LockError, find_isolation, pip_environment, read_lock,  # noqa: E402
                          run_offline, sha256_of)

DEFAULT_LOCK = Path(__file__).resolve().with_name("requirements-pip.txt")


class BootstrapError(Exception):
    """pip cannot be installed as the lock requires."""


def read_pip_lock(path: Path) -> Tuple[str, Set[str]]:
    """The version of pip the lock pins and the SHA-256 hashes it lists."""
    try:
        pins = read_lock(path)
    except LockError as exc:
        raise BootstrapError(str(exc)) from None
    if list(pins) != ["pip"] or pins["pip"].extras:
        raise BootstrapError(f"the lock {path} has to hold exactly one requirement, pip==<version>, "
                             "followed by its --hash=sha256:<hash> options and nothing else")
    return pins["pip"].version, set(pins["pip"].hashes)


def verified_wheel(wheelhouse: Path, filename: str, hashes: Set[str]) -> str:
    """The SHA-256 of the wheelhouse's pip wheel, if the lock lists it."""
    wheel = wheelhouse / filename
    if wheel.is_symlink() or not wheel.is_file():
        raise BootstrapError(f"the wheelhouse {wheelhouse} holds no {filename}; DB/fetch_verified.py fetches it")
    found = sha256_of(wheel)
    if found not in hashes:
        raise BootstrapError(f"{filename} in {wheelhouse} has the SHA-256 {found}, which the lock does not "
                             "list; nothing from it was run")
    if not zipfile.is_zipfile(wheel) or "pip/__main__.py" not in zipfile.ZipFile(wheel).namelist():
        raise BootstrapError(f"{filename} in {wheelhouse} matches the lock but is not pip's wheel")
    return found


def install(wheel: Path, lock: Path, version: str, required: bool) -> None:
    """Runs pip from the wheel to install the wheel, and nothing else, after
    checking that the pip which runs is the wheel's."""
    pip = [sys.executable, "-I", str(wheel / "pip")]
    environment = pip_environment()
    isolation = find_isolation()
    print(f"install_pip.py: {isolation.description}", flush=True)
    try:
        running = run_offline([*pip, "--version"], required=required, isolation=isolation,
                              capture_output=True, text=True, env=environment)
    except LockError as exc:
        raise BootstrapError(str(exc)) from None
    if running.returncode != 0 or not running.stdout.startswith(f"pip {version} from {wheel / 'pip'} "):
        raise BootstrapError(f"the wheel does not run as pip {version}: {running.stdout.strip()}")
    print(f"install_pip.py: running {running.stdout.strip()}", flush=True)
    command = [
        *pip, "install",
        "--isolated", "--no-index", "--find-links", str(wheel.parent),
        "--require-hashes", "--only-binary=:all:", "--no-deps",
        "--no-cache-dir", "--disable-pip-version-check", "--root-user-action=ignore",
        "--requirement", str(lock),
    ]
    status = run_offline(command, required=required, isolation=isolation, env=environment).returncode
    if status != 0:
        raise BootstrapError(f"the pip of the wheel could not install itself (exit status {status})")


_IMPORTED = """\
import importlib.metadata, json, sysconfig
import pip
print(json.dumps([pip.__version__, importlib.metadata.version("pip"), pip.__file__,
                  [sysconfig.get_path("purelib"), sysconfig.get_path("platlib")]]))
"""


def installed(version: str) -> Path:
    """The directory this Python imports pip from, as a new process of it
    finds it; an error unless that is pip of the given version, installed in
    the Python's own site-packages."""
    found = subprocess.run([sys.executable, "-I", "-c", _IMPORTED], capture_output=True, text=True,
                           env=pip_environment())
    if found.returncode != 0:
        reason = (found.stderr.strip().splitlines() or ["no reason given"])[-1]
        raise BootstrapError(f"pip reported success, but {sys.executable} does not find it: {reason}")
    try:
        module_version, distribution_version, module, site = json.loads(found.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise BootstrapError(f"cannot tell where {sys.executable} imports pip from: {found.stdout.strip()!r}") from None
    package = Path(module).resolve().parent
    sites = sorted({str(Path(directory).resolve()) for directory in site})
    if module_version != version or distribution_version != version or str(package.parent) not in sites:
        raise BootstrapError(
            f"pip reported success, but {sys.executable} imports pip {module_version} (installed as "
            f"{distribution_version}) from {package}, not pip {version} from its site-packages, "
            f"{' or '.join(sites)}"
        )
    return package


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0], formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n\n".join(__doc__.split("\n\n")[1:]),
    )
    parser.add_argument("--wheelhouse", type=Path, required=True,
                        help="the directory DB/fetch_verified.py fetched the locked files into")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK,
                        help="the lock of pip (default: requirements-pip.txt next to this script)")
    parser.add_argument("--network-isolation", choices=("preferred", "required"), default="preferred",
                        help="whether pip may run where no network isolation is possible (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        version, hashes = read_pip_lock(args.lock)
        filename = f"pip-{version}-py3-none-any.whl"
        found = verified_wheel(args.wheelhouse, filename, hashes)
        print(f"install_pip.py: {filename} has the SHA-256 {found}, which the lock lists; "
              "installing it with the pip it contains", flush=True)
        with tempfile.TemporaryDirectory(prefix="install-pip-") as directory:
            wheel = Path(directory) / filename
            shutil.copyfile(args.wheelhouse / filename, wheel)
            if sha256_of(wheel) != found:
                raise BootstrapError(f"{filename} changed while it was copied")
            install(wheel, args.lock.resolve(), version, args.network_isolation == "required")
        # Checked once the copied wheel is gone, so that the pip found cannot
        # be the wheel's.
        package = installed(version)
    except BootstrapError as exc:
        print(f"install_pip.py: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"install_pip.py: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"install_pip.py: pip {version} installed for {sys.executable}, which imports it from {package}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
