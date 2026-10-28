"""DB/install_locked.py and DB/locked_files.py, offline: installing a lock
from the wheelhouse alone.

The real script runs, as a separate process, in a new virtual environment
without pip, into which DB/install_pip.py first installs a wheel rebuilt from
the pip installed with these tests; the locks and wheels are synthetic. What
is shown: a wheelhouse that is not exactly the files of the locks -- a file
missing, an extra or changed one, a link, a source archive where a wheel is
locked, two files for one pin -- stops the run before pip runs; the real pip
then installs the lock into the environment from those files alone, without
network where the system permits a network namespace, and whatever pip's
configuration and variables say about indexes, extra file locations or the
destination; --network-isolation required refuses to install where no
namespace is possible; success is reported only when the environment imports
every package at its locked version. The C build runs too, on a synthetic
psycopg-c source archive whose build backend records what it found, from
paths given relative to the caller's directory: they reach pip and the build
backend, which run elsewhere, and the chosen pg_config wins over another on
PATH. Unit tests cover the pieces the image
build relies on: the requirements derived for each psycopg implementation,
the record of a psycopg-c wheel built from the verified archive, the build's
environment, the final check -- psycopg's implementation, its libpq of 16 or
later, no psycopg-binary -- and the detection of a namespace that does not
isolate.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import io
import json
import os
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path
from typing import Tuple

import pytest

import install_locked
import locked_files
from locked_files import LockError, Pin

DB = Path(__file__).resolve().parents[1]
SCRIPT = DB / "install_locked.py"
OTHER_HASH = "0" * 64
ISOLATED = locked_files.find_isolation().isolated


def wheel(name: str, version: str, module: str = "") -> bytes:
    buffer = io.BytesIO()
    distribution = name.replace("-", "_")
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{distribution}/__init__.py", module or f"__version__ = {version!r}\n")
        archive.writestr(f"{distribution}-{version}.dist-info/METADATA",
                         f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        archive.writestr(f"{distribution}-{version}.dist-info/WHEEL",
                         "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{distribution}-{version}.dist-info/RECORD", "")
    return buffer.getvalue()


def write_lock(path: Path, *pins: tuple) -> Path:
    text = "#\n# A synthetic lock.\n#\n"
    for name, version, *hashes in pins:
        text += f"{name}=={version}" + "".join(f" \\\n    --hash=sha256:{h}" for h in hashes) + "\n    # via test\n"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def real_pip() -> Tuple[str, bytes]:
    """The version and a wheel of the pip installed with these tests, as in
    tests/test_install_pip.py."""
    distribution = importlib.metadata.distribution("pip")
    dist_info = f"pip-{distribution.version}.dist-info"
    left_out = {f"{dist_info}/{name}" for name in ("INSTALLER", "RECORD", "REQUESTED", "direct_url.json")}
    buffer, record = io.BytesIO(), []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in distribution.files or []:
            name = file.as_posix()
            if name.startswith("../") or "/__pycache__/" in name or name in left_out:
                continue
            content = file.read_binary()
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            archive.writestr(name, content)
            record.append(f"{name},sha256={digest},{len(content)}")
        archive.writestr(f"{dist_info}/RECORD", "\n".join([*record, f"{dist_info}/RECORD,,"]) + "\n")
    return distribution.version, buffer.getvalue()


class Setup:
    """A wheelhouse with pip, a build tool and the lock's package; the three
    locks; and a new environment with that pip installed."""

    def __init__(self, root: Path, real_pip: Tuple[str, bytes]) -> None:
        self.root = root
        self.house = root / "wheelhouse"
        self.house.mkdir(parents=True)
        version, content = real_pip
        self.pip_lock = write_lock(root / "requirements-pip.txt",
                                   ("pip", version, self.put(f"pip-{version}-py3-none-any.whl", content)))
        self.build_lock = write_lock(root / "requirements-build.txt",
                                     ("buildtool", "1.0", self.put("buildtool-1.0-py3-none-any.whl",
                                                                   wheel("buildtool", "1.0"))))
        self.alpha = self.put("alpha-1.0-py3-none-any.whl", wheel("alpha", "1.0"))
        self.lock = write_lock(root / "requirements.txt", ("alpha", "1.0", self.alpha, OTHER_HASH))
        venv.create(root / "venv", with_pip=False)
        self.python = str(root / "venv" / "bin" / "python")
        done = subprocess.run([self.python, str(DB / "install_pip.py"), "--wheelhouse", str(self.house),
                               "--lock", str(self.pip_lock)], capture_output=True, text=True, timeout=180,
                              env=clean_environment())
        assert done.returncode == 0, done.stdout + done.stderr

    def put(self, name: str, content: bytes) -> str:
        (self.house / name).write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def run(self, *extra: str, **environment: str) -> subprocess.CompletedProcess:
        return subprocess.run([self.python, str(SCRIPT), "--wheelhouse", str(self.house), "--pip-lock",
                               str(self.pip_lock), "--build-lock", str(self.build_lock), *extra, str(self.lock)],
                              capture_output=True, text=True, timeout=180,
                              env=dict(clean_environment(), **environment))

    def installed(self) -> dict:
        found = subprocess.run([self.python, "-I", "-c", "import importlib.metadata as m, json; print(json.dumps("
                                "{d.metadata['Name']: d.version for d in m.distributions()}))"],
                               capture_output=True, text=True, timeout=60)
        return json.loads(found.stdout)


def clean_environment() -> dict:
    return {name: value for name, value in os.environ.items() if not name.startswith(("PIP_", "_PIP_"))}


@pytest.fixture
def setup(tmp_path: Path, real_pip) -> Setup:
    return Setup(tmp_path, real_pip)


def test_the_lock_is_installed_from_the_wheelhouse_alone_into_the_environment(setup: Setup) -> None:
    proc = setup.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"install_locked.py: {setup.house} holds exactly the 3 files the locks fix" in proc.stdout
    assert "install_locked.py: 1 packages of requirements.txt installed for" in proc.stdout
    installed = setup.installed()
    assert installed["alpha"] == "1.0" and "buildtool" not in installed, "the build tools stay out"
    if ISOLATED:
        assert "install_locked.py: network isolation:" in proc.stdout
    else:
        assert "install_locked.py: NO network isolation:" in proc.stdout


def test_without_a_network_namespace_required_refuses_before_pip_runs(setup: Setup, tmp_path: Path) -> None:
    empty = tmp_path / "no-unshare"
    empty.mkdir()
    proc = setup.run("--network-isolation", "required", PATH=str(empty))
    assert proc.returncode == 1 and "refusing to install (--network-isolation required)" in proc.stderr
    assert "alpha" not in setup.installed()


def test_pips_configuration_neither_adds_inputs_nor_moves_the_destination(setup: Setup, tmp_path: Path) -> None:
    # Another alpha, which pip would take from an extra location or index if
    # it read any configuration, and a destination elsewhere.
    elsewhere, extra = tmp_path / "elsewhere", tmp_path / "extra"
    extra.mkdir()
    (extra / "alpha-2.0-py3-none-any.whl").write_bytes(wheel("alpha", "2.0"))
    configuration = tmp_path / "pip.conf"
    configuration.write_text(f"[global]\nfind-links = {extra}\n[install]\ntarget = {elsewhere}\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".config" / "pip").mkdir(parents=True)
    (home / ".config" / "pip" / "pip.conf").write_text(f"[install]\nprefix = {elsewhere}\n", encoding="utf-8")
    proc = setup.run(PIP_CONFIG_FILE=str(configuration), PIP_FIND_LINKS=str(extra), PIP_TARGET=str(elsewhere),
                     PIP_USER="1", PIP_INDEX_URL="https://index.invalid/simple/", PIP_NO_INDEX="0",
                     PIP_REQUIRE_HASHES="0", HOME=str(home), PYTHONUSERBASE=str(elsewhere))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert setup.installed()["alpha"] == "1.0" and not elsewhere.exists()


@pytest.mark.parametrize("change, message", [
    ("missing", "alpha==1.0: no file in the wheelhouse"),
    ("extra", "stray-1.0-py3-none-any.whl: no lock pins it"),
    ("corrupted", "alpha-1.0-py3-none-any.whl: its SHA-256"),
    ("link", "alpha-1.0-py3-none-any.whl: not a regular file"),
    ("source", "alpha-1.0.tar.gz: a source archive, and alpha is installed from a wheel only"),
    ("second", "a second file for alpha"),
    ("other version", "alpha-2.0-py3-none-any.whl: version 2.0, locked 1.0"),
])
def test_a_wheelhouse_that_is_not_exactly_the_locked_files_stops_the_run_before_pip(
        setup: Setup, tmp_path: Path, change: str, message: str) -> None:
    target = setup.house / "alpha-1.0-py3-none-any.whl"
    if change == "missing":
        target.unlink()
    elif change == "extra":
        (setup.house / "stray-1.0-py3-none-any.whl").write_bytes(wheel("stray", "1.0"))
    elif change == "corrupted":
        target.write_bytes(target.read_bytes() + b"\0")
    elif change == "link":
        moved = tmp_path / "moved.whl"
        target.rename(moved)
        target.symlink_to(moved)
    elif change == "source":
        content = b"alpha source"
        (setup.house / "alpha-1.0.tar.gz").write_bytes(content)
        target.unlink()
        write_lock(setup.lock, ("alpha", "1.0", hashlib.sha256(content).hexdigest()))
    elif change == "second":
        content = wheel("alpha", "1.0", "second = True\n")
        (setup.house / "alpha-1.0-py2.py3-none-any.whl").write_bytes(content)
        write_lock(setup.lock, ("alpha", "1.0", setup.alpha, hashlib.sha256(content).hexdigest()))
    else:
        (setup.house / "alpha-2.0-py3-none-any.whl").write_bytes(wheel("alpha", "2.0"))
    proc = setup.run()
    assert proc.returncode == 1 and "is not exactly the files the locks fix" in proc.stderr, proc.stderr
    assert message in proc.stderr, proc.stderr
    assert "alpha" not in setup.installed()


def test_a_lock_that_pins_something_unlocked_or_carries_options_is_refused(setup: Setup) -> None:
    setup.lock.write_text("alpha>=1.0 --hash=sha256:" + setup.alpha + "\n", encoding="utf-8")
    proc = setup.run()
    assert proc.returncode == 1 and "is not name==version followed by --hash" in proc.stderr


def test_build_only_and_built_are_for_a_lock_with_psycopg_c(setup: Setup, tmp_path: Path) -> None:
    for option in (["--build-only", str(tmp_path / "out")], ["--built", str(tmp_path / "out")]):
        proc = setup.run(*option)
        assert proc.returncode == 1 and "are for a lock that pins psycopg-c" in proc.stderr


# --- the C build, from paths relative to the caller's directory -------------------------------

#: The build backend of a synthetic psycopg-c source archive: what the real
#: build relies on -- pg_config found on PATH -- is recorded in the wheel,
#: with the directory the build ran in and the PATH it saw.
BACKEND = '''\
import json, os, shutil, subprocess, zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    pg_config = shutil.which("pg_config")
    seen = {"cwd": os.getcwd(), "path": os.environ["PATH"].split(os.pathsep), "pg_config": pg_config,
            "version": subprocess.run([pg_config, "--version"], capture_output=True, text=True).stdout.strip()
            if pg_config else None}
    name = "psycopg_c-3.3.6-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as wheel:
        wheel.writestr("psycopg_c/__init__.py", "")
        wheel.writestr("psycopg_c/build.json", json.dumps(seen))
        wheel.writestr("psycopg_c-3.3.6.dist-info/METADATA", "Metadata-Version: 2.1\\nName: psycopg-c\\nVersion: 3.3.6\\n")
        wheel.writestr("psycopg_c-3.3.6.dist-info/WHEEL",
                       "Wheel-Version: 1.0\\nGenerator: test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n")
        wheel.writestr("psycopg_c-3.3.6.dist-info/RECORD", "")
    return name
'''


def psycopg_c_sdist() -> bytes:
    buffer = io.BytesIO()
    files = {"pyproject.toml": '[build-system]\nrequires = ["buildtool"]\nbuild-backend = "backend"\n'
                               'backend-path = ["."]\n',
             "backend.py": BACKEND,
             "PKG-INFO": "Metadata-Version: 2.1\nName: psycopg-c\nVersion: 3.3.6\n"}
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            content = text.encode()
            info = tarfile.TarInfo(f"psycopg_c-3.3.6/{name}")
            info.size, info.mode = len(content), 0o644
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def fake_pg_config(directory: Path, version: str) -> Path:
    directory.mkdir(parents=True)
    script = directory / "pg_config"
    script.write_text(f'#!/bin/sh\ncase "$1" in\n  --version) echo "PostgreSQL {version}" ;;\n'
                      f'  --includedir) echo "{directory}/include" ;;\n  --libdir) echo "{directory}/lib" ;;\n'
                      '  *) exit 1 ;;\nesac\n', encoding="utf-8")
    script.chmod(0o755)
    return script


def test_relative_paths_reach_the_c_build_from_the_directory_it_runs_in(setup: Setup) -> None:
    # The build of psycopg-c runs pip in a temporary directory of its own, and
    # pip runs the source's build backend in yet another. Every path given
    # relative to the caller's directory -- the wheelhouse, the three locks,
    # the chosen pg_config, --build-only and --built -- has to reach both, and
    # the chosen pg_config has to win over another one on PATH.
    (setup.house / "alpha-1.0-py3-none-any.whl").unlink()
    setup.lock = write_lock(setup.root / "requirements.txt",
                            ("psycopg-c", "3.3.6", setup.put("psycopg_c-3.3.6.tar.gz", psycopg_c_sdist())))
    fake_pg_config(setup.root / "libpq" / "bin", "99.1 (chosen)")
    decoy = fake_pg_config(setup.root / "elsewhere", "0.1 (another on PATH)").parent
    environment = dict(clean_environment(), PATH=os.pathsep.join([str(decoy), os.environ["PATH"]]))
    relative = ["--wheelhouse", "wheelhouse", "--pip-lock", "requirements-pip.txt",
                "--build-lock", "requirements-build.txt", "--pg-config", "libpq/bin/pg_config"]

    def run(*extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([setup.python, str(SCRIPT), *relative, *extra, "requirements.txt"],
                              cwd=setup.root, capture_output=True, text=True, timeout=300, env=environment)

    built = run("--build-only", "built")
    assert built.returncode == 0, built.stdout + built.stderr
    assert f"building psycopg_c-3.3.6.tar.gz against PostgreSQL 99.1 (chosen) ({setup.root}/libpq/bin/pg_config" \
        in built.stdout
    record = json.loads((setup.root / "built" / install_locked.RECORD).read_text(encoding="utf-8"))
    with zipfile.ZipFile(setup.root / "built" / record["wheel"]) as wheel:
        seen = json.loads(wheel.read("psycopg_c/build.json"))
    # The backend ran elsewhere than the caller, with no relative entry on
    # PATH, and found the chosen pg_config first.
    assert Path(seen["cwd"]).resolve() != setup.root.resolve()
    assert all(os.path.isabs(entry) for entry in seen["path"] if entry), seen["path"]
    assert seen["path"][0] == str(setup.root / "libpq" / "bin")
    assert seen["version"] == "PostgreSQL 99.1 (chosen)" and seen["pg_config"] == str(
        setup.root / "libpq" / "bin" / "pg_config")
    assert "psycopg-c" not in setup.installed(), "--build-only installs nothing"

    installed = run("--built", "built")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert "install_locked.py: 1 packages of requirements.txt installed for" in installed.stdout
    assert setup.installed()["psycopg-c"] == "3.3.6"


def test_a_pg_config_that_is_not_the_one_found_first_is_refused(tmp_path: Path) -> None:
    # A file named otherwise would leave the build with whichever pg_config
    # comes first on PATH.
    chosen = fake_pg_config(tmp_path / "libpq", "99.1")
    renamed = chosen.with_name("pg_config-99")
    chosen.rename(renamed)
    environment = install_locked.build_environment(tmp_path / "home", renamed)
    with pytest.raises(install_locked.InstallError, match="is not the pg_config the build finds first"):
        install_locked.describe_pg_config(environment, renamed)
    renamed.rename(chosen)
    assert install_locked.describe_pg_config(environment, chosen)["version"] == "PostgreSQL 99.1"


# --- the pieces of the image build ------------------------------------------------------------

TARGET = {
    "psycopg": Pin("psycopg", "3.3.6", frozenset({"1" * 64, "2" * 64}), ("c",)),
    "psycopg-c": Pin("psycopg-c", "3.3.6", frozenset({"3" * 64})),
    "typing-extensions": Pin("typing-extensions", "4.15.0", frozenset({"4" * 64})),
}


def test_the_requirements_install_psycopg_c_by_its_build_or_not_at_all() -> None:
    record = {"wheel_sha256": "5" * 64}
    requirements, locked = install_locked.requirements_for(TARGET, "c", record)
    assert requirements == [f"psycopg==3.3.6 --hash=sha256:{'1' * 64} --hash=sha256:{'2' * 64}",
                            f"psycopg-c==3.3.6 --hash=sha256:{'5' * 64}",
                            f"typing-extensions==4.15.0 --hash=sha256:{'4' * 64}"]
    assert locked == {"psycopg": "3.3.6", "psycopg-c": "3.3.6", "typing-extensions": "4.15.0"}
    requirements, locked = install_locked.requirements_for(TARGET, "python", {})
    assert [line.split("==")[0] for line in requirements] == ["psycopg", "typing-extensions"]
    assert "psycopg-c" not in locked


def test_a_built_wheel_is_installed_only_as_its_record_from_the_verified_archive_says(tmp_path: Path) -> None:
    sdist = tmp_path / "psycopg_c-3.3.6.tar.gz"
    sdist.write_bytes(b"source")
    pin = Pin("psycopg-c", "3.3.6", frozenset({hashlib.sha256(b"source").hexdigest()}))
    built = tmp_path / "built"
    built.mkdir()
    name = "psycopg_c-3.3.6-cp311-cp311-linux_x86_64.whl"
    (built / name).write_bytes(b"built wheel")
    record = {"version": "3.3.6", "sdist": sdist.name, "sdist_sha256": hashlib.sha256(b"source").hexdigest(),
              "wheel": name, "wheel_sha256": hashlib.sha256(b"built wheel").hexdigest()}
    (built / install_locked.RECORD).write_text(json.dumps(record), encoding="utf-8")
    assert install_locked.read_built(built, sdist, pin)["wheel_sha256"] == record["wheel_sha256"]
    (built / name).write_bytes(b"another wheel")
    with pytest.raises(install_locked.InstallError, match="is not the wheel its record names"):
        install_locked.read_built(built, sdist, pin)
    (built / name).write_bytes(b"built wheel")
    sdist.write_bytes(b"other source")
    with pytest.raises(install_locked.InstallError, match="does not describe a build of the locked"):
        install_locked.read_built(built, sdist, pin)
    sdist.write_bytes(b"source")
    (built / "stray.whl").write_bytes(b"stray")
    with pytest.raises(install_locked.InstallError, match="nothing else"):
        install_locked.read_built(built, sdist, pin)


def test_the_build_sees_no_python_variable_no_home_of_the_callers_and_the_chosen_pg_config(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    monkeypatch.setenv("PYTHONSTARTUP", "/elsewhere/startup.py")
    monkeypatch.setenv("PIP_INDEX_URL", "https://index.invalid/simple/")
    environment = install_locked.build_environment(tmp_path / "home", Path("/opt/libpq/bin/pg_config"))
    assert not any(name.startswith("PYTHON") for name in environment)
    assert environment["HOME"] == str(tmp_path / "home") and environment["PIP_CONFIG_FILE"] == os.devnull
    assert "PIP_INDEX_URL" not in environment
    assert environment["PATH"].split(os.pathsep)[0] == "/opt/libpq/bin"


def fake_psycopg(tmp_path: Path, implementation: str, libpq: int, binary: bool = False) -> str:
    """An environment whose psycopg reports the given implementation and libpq."""
    venv.create(tmp_path / "env", with_pip=False)
    site = next((tmp_path / "env" / "lib").glob("python3*/site-packages"))
    (site / "psycopg").mkdir()
    module = ["__version__ = '3.3.6'", "class pq:", f"    __impl__ = {implementation!r}",
              f"    __build_version__ = {libpq}", "    @staticmethod", "    def version():", f"        return {libpq}"]
    (site / "psycopg" / "__init__.py").write_text("\n".join(module) + "\n", encoding="utf-8")
    for name in ["psycopg"] + (["psycopg_binary"] if binary else []):
        (site / f"{name}-3.3.6.dist-info").mkdir()
        (site / f"{name}-3.3.6.dist-info" / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name.replace('_', '-')}\nVersion: 3.3.6\n", encoding="utf-8")
    return str(tmp_path / "env" / "bin" / "python")


@pytest.mark.parametrize("implementation, libpq, binary, problem", [
    ("c", 170011, False, None),
    ("c", 120015, False, "psycopg loads libpq 120015, older than 16, which lacks the require_auth setting"),
    ("python", 170011, False, "psycopg uses its python implementation, not c"),
    ("c", 170011, True, "psycopg-binary is installed"),
])
def test_the_final_check_requires_the_implementation_a_libpq_of_16_or_later_and_no_psycopg_binary(
        tmp_path: Path, implementation: str, libpq: int, binary: bool, problem) -> None:
    python = fake_psycopg(tmp_path, implementation, libpq, binary)
    done = subprocess.run([python, "-I", "-c", install_locked._CHECK, json.dumps({"psycopg": "3.3.6"}), "c"],
                          capture_output=True, text=True, timeout=60)
    assert f"psycopg 3.3.6, {implementation} implementation, libpq {libpq}" in done.stdout
    if problem is None:
        assert done.returncode == 0, done.stdout + done.stderr
    else:
        assert done.returncode == 1 and problem in done.stdout


def test_a_namespace_that_does_not_isolate_is_not_taken_for_isolation(tmp_path: Path) -> None:
    # A stand-in for unshare that runs the command in this network
    # namespace: the probe sees this namespace's interfaces, not loopback alone.
    fake = tmp_path / "unshare"
    fake.write_text('#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n', encoding="utf-8")
    fake.chmod(0o755)
    found = locked_files.find_isolation(str(fake))
    if locked_files.interfaces() == ["lo"]:
        assert found.isolated and found.prefix == ()
    else:
        assert not found.isolated and "NO network isolation" in found.description
    with pytest.raises(LockError, match="refusing to install"):
        locked_files.run_offline(["true"], required=True, isolation=locked_files.Isolation((), False, "none"))


def test_pip_runs_with_none_of_its_variables_and_no_configuration_file(monkeypatch) -> None:
    monkeypatch.setenv("PIP_TARGET", "/elsewhere")
    monkeypatch.setenv("_PIP_USE_IMPORTLIB_METADATA", "0")
    monkeypatch.setenv("ENSUREPIP_OPTIONS", "altinstall")
    environment = locked_files.pip_environment()
    assert environment["PIP_CONFIG_FILE"] == os.devnull
    assert not any(name.startswith(("PIP_", "_PIP_")) and name != "PIP_CONFIG_FILE" for name in environment)
    assert "ENSUREPIP_OPTIONS" not in environment


def test_the_script_needs_nothing_but_the_standard_library() -> None:
    proc = subprocess.run([sys.executable, "-I", "-S", str(SCRIPT), "--help"], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "--psycopg" in proc.stdout and "--build-only" in proc.stdout and "--built" in proc.stdout
