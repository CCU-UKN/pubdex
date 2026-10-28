"""DB/install_pip.py, offline: the pip bootstrap of every new .venv and of the
application image, from a wheelhouse DB/fetch_verified.py filled.

Most tests run the real script, as a separate process, against a wheelhouse
that holds a stand-in for pip's wheel: a zip whose pip/__main__.py answers
--version as pip would and otherwise records its arguments, the pip settings
of its environment and the network interfaces it sees, and then places
itself in the site-packages of the Python that runs it, as an installation
would. The interpreter is a new virtual environment without pip, as the
documented setup creates it. What is shown: the wheel runs only when its
SHA-256 is one the lock lists and it is a wheel of the locked pip; a missing
or corrupted wheel installs nothing; the wheel then installs itself from a
copy in a directory of its own and nothing else, with every option that
keeps the index, pip's configuration and source builds out, in an
environment without any of pip's variables and with PIP_CONFIG_FILE naming
os.devnull; it runs without network where the system permits a network
namespace, and with --network-isolation required not at all where it does
not; success is reported only when the interpreter then imports pip of the
locked version from its own site-packages; a lock that is not one hashed pip
requirement is refused before anything else.

The real pip is tested too: a wheel rebuilt from the pip installed with
these tests -- the locked pip, each file checked against its RECORD -- is
put in the wheelhouse and installs itself under configuration that sends
pip's installations elsewhere, from each place pip reads it: PIP_CONFIG_FILE,
PIP_* variables, and the global, site and user files, all of them temporary.
Each case first shows that pip itself follows that configuration. That the
locked wheel installs itself this way is shown by every CI run and by the
image build.
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
import venv
import zipfile
from pathlib import Path
from typing import Tuple

import pytest

import install_pip
import locked_files

SCRIPT = Path(__file__).resolve().parents[1] / "install_pip.py"
VERSION = "99.0"
WHEEL = f"pip-{VERSION}-py3-none-any.whl"
OTHER_HASH = "0" * 64
STAND_IN_MAIN = '''
import json, os, sys, sysconfig
pip_settings = {name: value for name, value in os.environ.items()
                if name.startswith(("PIP_", "_PIP_")) or name == "ENSUREPIP_OPTIONS"}
with open("/proc/self/net/dev", encoding="ascii") as dev:
    interfaces = sorted(line.split(":", 1)[0].strip() for line in dev.readlines()[2:])
with open(os.environ["STAND_IN_PIP_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "flags": [sys.flags.isolated, sys.flags.ignore_environment],
                          "pip_settings": pip_settings, "interfaces": interfaces}) + "\\n")
if sys.argv[1:] == ["--version"]:
    shown = os.environ.get("STAND_IN_PIP_VERSION", "99.0")
    print(f"pip {shown} from {os.path.dirname(__file__)} (python {sys.version_info[0]}.{sys.version_info[1]})")
    sys.exit(0)
status = int(os.environ.get("STAND_IN_PIP_STATUS", "0"))
if status == 0:
    # What an installation leaves: the package and its metadata, in the
    # site-packages of this Python unless the test names another directory.
    site = os.environ.get("STAND_IN_PIP_INSTALL_TO") or sysconfig.get_path("purelib")
    version = os.environ.get("STAND_IN_PIP_INSTALLS", "99.0")
    recorded = os.environ.get("STAND_IN_PIP_METADATA", version)
    os.makedirs(os.path.join(site, "pip"), exist_ok=True)
    os.makedirs(os.path.join(site, f"pip-{recorded}.dist-info"), exist_ok=True)
    with open(os.path.join(site, "pip", "__init__.py"), "w", encoding="utf-8") as module:
        module.write(f"__version__ = {version!r}\\n")
    with open(os.path.join(site, f"pip-{recorded}.dist-info", "METADATA"), "w", encoding="utf-8") as metadata:
        metadata.write(f"Metadata-Version: 2.1\\nName: pip\\nVersion: {recorded}\\n")
    if os.environ.get("STAND_IN_PIP_PATH_FILE"):
        with open(os.path.join(sysconfig.get_path("purelib"), "stand-in.pth"), "w", encoding="utf-8") as path:
            path.write(site + "\\n")
sys.exit(status)
'''
#: pip's own variables, which no run of the script may hand to pip.
PIP_SETTINGS = ("PIP_", "_PIP_", "ENSUREPIP_OPTIONS")
#: Whether this system permits the script a network namespace; each test
#: that depends on it checks what the script does either way.
ISOLATED = locked_files.find_isolation().isolated


def new_pipless_python(directory: Path) -> str:
    """The interpreter of a new virtual environment without pip."""
    venv.create(directory, with_pip=False)
    python = directory / "bin" / "python"
    assert subprocess.run([str(python), "-I", "-c", "import pip"], capture_output=True).returncode != 0
    return str(python)


@pytest.fixture
def pipless_python(tmp_path: Path) -> str:
    return new_pipless_python(tmp_path / "venv")


def imports(python: str) -> Tuple[str, Path, Path]:
    """The pip version python imports, the directory it imports it from and
    python's site-packages."""
    found = subprocess.run(
        [python, "-I", "-c", "import pip, sysconfig; print(pip.__version__); print(pip.__file__); "
                             "print(sysconfig.get_path('purelib'))"],
        capture_output=True, text=True, timeout=60)
    assert found.returncode == 0, found.stderr
    version, module, site = found.stdout.splitlines()
    return version, Path(module).resolve().parent, Path(site).resolve()


class Wheelhouse:
    """A wheelhouse, as DB/fetch_verified.py leaves one, and a pip lock."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.directory = root / "wheelhouse"
        self.directory.mkdir(parents=True)
        self.lock = root / "requirements-pip.txt"
        self.log = root / "stand-in-pip.log"

    def put(self, content: bytes, name: str = WHEEL) -> str:
        """Puts content in the wheelhouse as name; returns its SHA-256."""
        (self.directory / name).write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def write_lock(self, *hashes: str, requirement: str = f"pip=={VERSION}") -> None:
        options = "".join(f" \\\n    --hash=sha256:{digest}" for digest in hashes)
        self.lock.write_text(f"#\n# The pip of the test.\n#\n{requirement}{options}\n    # via -r -\n",
                             encoding="utf-8")

    def calls(self) -> list:
        """Every run of the stand-in, --version included."""
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def installs(self) -> list:
        return [call for call in self.calls() if call["argv"][:1] == ["install"]]


def stand_in_wheel() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        wheel.writestr("pip/__init__.py", f'__version__ = "{VERSION}"\n')
        wheel.writestr("pip/__main__.py", STAND_IN_MAIN)
        wheel.writestr(f"pip-{VERSION}.dist-info/WHEEL", "Wheel-Version: 1.0\n")
    return buffer.getvalue()


@pytest.fixture
def house(tmp_path: Path) -> Wheelhouse:
    return Wheelhouse(tmp_path / "files")


def environment(house: Wheelhouse, **extra: str) -> dict:
    """The test process's environment without pip's settings, with extra."""
    env = {name: value for name, value in os.environ.items() if not name.startswith(PIP_SETTINGS)}
    env.update(STAND_IN_PIP_LOG=str(house.log), **extra)
    return env


def run(python: str, house: Wheelhouse, *args: str, **extra: str) -> subprocess.CompletedProcess:
    arguments = args or ("--wheelhouse", str(house.directory), "--lock", str(house.lock))
    return subprocess.run([python, str(SCRIPT), *arguments], env=environment(house, **extra),
                          capture_output=True, text=True, timeout=120, cwd=house.root)


def test_the_locked_wheel_installs_itself_from_a_copy_of_its_own_and_nothing_else(pipless_python, house) -> None:
    digest = house.put(stand_in_wheel())
    house.write_lock(OTHER_HASH, digest)
    proc = run(pipless_python, house)
    assert proc.returncode == 0, proc.stderr
    (call,) = house.installs()
    argv = call["argv"]
    find_links = argv[argv.index("--find-links") + 1]
    assert argv == ["install", "--isolated", "--no-index", "--find-links", find_links,
                    "--require-hashes", "--only-binary=:all:", "--no-deps", "--no-cache-dir",
                    "--disable-pip-version-check", "--root-user-action=ignore",
                    "--requirement", str(house.lock.resolve())]
    assert Path(find_links) != house.directory, "pip sees the pip wheel alone, not the wheelhouse"
    for call in house.calls():
        assert call["flags"] == [1, 1], "python -I: no PYTHON* variable, user site or working directory reaches pip"
        assert call["pip_settings"] == {"PIP_CONFIG_FILE": os.devnull}, "pip reads no configuration file"
    assert not Path(find_links).exists(), "the copy of the wheel is removed again"
    assert (house.directory / WHEEL).exists(), "the wheelhouse is left as it was"
    assert f"has the SHA-256 {digest}, which the lock lists" in proc.stdout
    assert f"running pip {VERSION} from {find_links}/{WHEEL}/pip (python" in proc.stdout
    version, package, site = imports(pipless_python)
    assert (version, package) == (VERSION, site / "pip")
    assert f"pip {VERSION} installed for {pipless_python}, which imports it from {package}" in proc.stdout


def test_pip_runs_without_network_where_the_system_permits_it(pipless_python, house) -> None:
    house.write_lock(house.put(stand_in_wheel()))
    proc = run(pipless_python, house)
    assert proc.returncode == 0, proc.stderr
    seen = {tuple(call["interfaces"]) for call in house.calls()}
    if ISOLATED:
        assert seen == {("lo",)}, "pip saw no network interface but loopback"
        assert "install_pip.py: network isolation:" in proc.stdout
    else:
        assert "install_pip.py: NO network isolation:" in proc.stdout
        assert "that is not a network sandbox" in proc.stdout


@pytest.mark.parametrize("mode", ["preferred", "required"])
def test_without_a_network_namespace_required_refuses_and_preferred_says_so(pipless_python, house, tmp_path,
                                                                            mode: str) -> None:
    # A system that permits no network namespace: no unshare on PATH, as on
    # a host whose kernel or policy refuses one.
    house.write_lock(house.put(stand_in_wheel()))
    empty = tmp_path / "no-unshare"
    empty.mkdir()
    proc = run(pipless_python, house, "--wheelhouse", str(house.directory), "--lock", str(house.lock),
               "--network-isolation", mode, PATH=str(empty))
    if mode == "required":
        assert proc.returncode == 1
        assert "refusing to install (--network-isolation required)" in proc.stderr
        assert house.calls() == [], "nothing from the wheel ran"
    else:
        assert proc.returncode == 0, proc.stderr
        assert "NO network isolation" in proc.stdout and len(house.installs()) == 1


def test_no_pip_setting_reaches_pip(pipless_python, house, tmp_path) -> None:
    house.write_lock(house.put(stand_in_wheel()))
    elsewhere = tmp_path / "elsewhere"
    hostile = tmp_path / "pip.conf"
    hostile.write_text(f"[install]\ntarget = {elsewhere}\nfind-links = {tmp_path}\n", encoding="utf-8")
    proc = run(pipless_python, house, PIP_CONFIG_FILE=str(hostile), PIP_TARGET=str(elsewhere), PIP_NO_INDEX="0",
               PIP_INDEX_URL="https://index.invalid/simple/", PIP_FIND_LINKS=str(tmp_path),
               _PIP_USE_IMPORTLIB_METADATA="0", ENSUREPIP_OPTIONS="altinstall")
    assert proc.returncode == 0, proc.stderr
    assert [call["pip_settings"] for call in house.calls()] == [{"PIP_CONFIG_FILE": os.devnull}] * 2
    assert imports(pipless_python)[0] == VERSION and not elsewhere.exists()


@pytest.mark.parametrize("where, error", [
    ("elsewhere", "does not find it: ModuleNotFoundError: No module named 'pip'"),
    ("another version", "imports pip 98.0 (installed as 98.0) from {site}/pip, not pip 99.0 from its site-packages"),
    ("other metadata", "imports pip 99.0 (installed as 98.0) from {site}/pip, not pip 99.0 from its site-packages"),
    ("other module", "imports pip 98.0 (installed as 99.0) from {site}/pip, not pip 99.0 from its site-packages"),
    ("on a path file", "imports pip 99.0 (installed as 99.0) from {elsewhere}/pip, not pip 99.0 from its "
                       "site-packages, {site}"),
])
def test_success_needs_the_locked_pip_in_the_interpreters_own_site_packages(
    pipless_python, house, tmp_path, where: str, error: str,
) -> None:
    house.write_lock(house.put(stand_in_wheel()))
    elsewhere = tmp_path / "elsewhere"
    settings = {
        "elsewhere": {"STAND_IN_PIP_INSTALL_TO": str(elsewhere)},
        "another version": {"STAND_IN_PIP_INSTALLS": "98.0"},
        "other metadata": {"STAND_IN_PIP_METADATA": "98.0"},
        "other module": {"STAND_IN_PIP_INSTALLS": "98.0", "STAND_IN_PIP_METADATA": "99.0"},
        "on a path file": {"STAND_IN_PIP_INSTALL_TO": str(elsewhere), "STAND_IN_PIP_PATH_FILE": "1"},
    }[where]
    proc = run(pipless_python, house, **settings)
    site = subprocess.run([pipless_python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                          capture_output=True, text=True).stdout.strip()
    assert proc.returncode == 1 and len(house.installs()) == 1
    assert "install_pip.py: pip reported success, but " in proc.stderr
    assert error.format(site=site, elsewhere=elsewhere) in proc.stderr
    assert "installed for" not in proc.stdout


def test_a_corrupted_wheel_is_never_run(pipless_python, house) -> None:
    digest = house.put(stand_in_wheel())
    house.write_lock(digest)
    content = bytearray((house.directory / WHEEL).read_bytes())
    content[-1] ^= 0xFF
    (house.directory / WHEEL).write_bytes(bytes(content))
    proc = run(pipless_python, house)
    assert proc.returncode == 1
    assert f"{WHEEL} in {house.directory} has the SHA-256" in proc.stderr
    assert "which the lock does not list; nothing from it was run" in proc.stderr
    assert house.calls() == [] and "running pip" not in proc.stdout


def test_a_missing_wheel_installs_nothing(pipless_python, house) -> None:
    house.write_lock(OTHER_HASH)
    house.put(stand_in_wheel(), name="pip-98.0-py3-none-any.whl")
    proc = run(pipless_python, house)
    assert proc.returncode == 1
    assert f"the wheelhouse {house.directory} holds no {WHEEL}; DB/fetch_verified.py fetches it" in proc.stderr
    assert house.calls() == []


def test_a_link_in_place_of_the_wheel_is_refused(pipless_python, house, tmp_path) -> None:
    digest = house.put(stand_in_wheel(), name="elsewhere.whl")
    (house.directory / WHEEL).symlink_to(house.directory / "elsewhere.whl")
    house.write_lock(digest)
    proc = run(pipless_python, house)
    assert proc.returncode == 1 and f"holds no {WHEEL}" in proc.stderr
    assert house.calls() == []


def test_a_file_the_lock_lists_that_is_not_the_wheel_is_never_run(pipless_python, house) -> None:
    house.write_lock(house.put(b"\x1f\x8b not a zip archive"))
    proc = run(pipless_python, house)
    assert proc.returncode == 1 and "matches the lock but is not pip's wheel" in proc.stderr
    assert house.calls() == []


def test_a_wheel_that_does_not_run_as_the_locked_pip_installs_nothing(pipless_python, house) -> None:
    house.write_lock(house.put(stand_in_wheel()))
    proc = run(pipless_python, house, STAND_IN_PIP_VERSION="24.0")
    assert proc.returncode == 1 and f"the wheel does not run as pip {VERSION}: pip 24.0" in proc.stderr
    assert house.installs() == []


def test_a_failed_installation_is_reported(pipless_python, house) -> None:
    house.write_lock(house.put(stand_in_wheel()))
    proc = run(pipless_python, house, STAND_IN_PIP_STATUS="3")
    assert proc.returncode == 1 and "the pip of the wheel could not install itself (exit status 3)" in proc.stderr
    assert len(house.installs()) == 1


@pytest.mark.parametrize("requirement, hashed", [
    ("pip>=99.0", True),
    (f"pip=={VERSION}", False),
    ("setuptools==99.0", True),
    (f"pip=={VERSION} --index-url https://mirror.example.org/simple/", True),
    (f"pip=={VERSION} \\\n    --hash=sha256:{OTHER_HASH}\nwheel==0.1", True),
])
def test_a_lock_that_is_not_one_hashed_pip_requirement_is_refused_first(
    pipless_python, house, requirement: str, hashed: bool,
) -> None:
    house.put(stand_in_wheel())
    house.write_lock(*([OTHER_HASH] if hashed else []), requirement=requirement)
    proc = run(pipless_python, house)
    assert proc.returncode == 1
    assert ("has to hold exactly one requirement, pip==<version>" in proc.stderr
            or "is not name==version followed by --hash=sha256:<hash> options alone" in proc.stderr), proc.stderr
    assert house.calls() == []


def test_the_repository_lock_is_read_as_one_hashed_pip() -> None:
    version, hashes = install_pip.read_pip_lock(install_pip.DEFAULT_LOCK)
    assert install_pip.DEFAULT_LOCK == SCRIPT.with_name("requirements-pip.txt")
    assert version == "26.2.2" and len(hashes) == 2


def test_the_script_needs_nothing_but_the_standard_library() -> None:
    # -S: no site-packages at all; -I: not even the script's directory on the
    # path, which the script adds for DB/locked_files.py itself.
    proc = subprocess.run([sys.executable, "-I", "-S", str(SCRIPT), "--help"], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "Install the pip that requirements-pip.txt pins" in proc.stdout
    assert "--wheelhouse" in proc.stdout and "--network-isolation" in proc.stdout


# --- The real pip, under configuration that sends its installations elsewhere


@pytest.fixture(scope="module")
def real_pip() -> Tuple[str, bytes]:
    """The version and a wheel of the pip installed with these tests: its
    installed files, each checked against the hash its RECORD lists, with a
    RECORD of their own. The wheel's hash is not the lock's, so each test
    writes a lock of its own."""
    distribution = importlib.metadata.distribution("pip")
    dist_info = f"pip-{distribution.version}.dist-info"
    left_out = {f"{dist_info}/{name}" for name in ("INSTALLER", "RECORD", "REQUESTED", "direct_url.json")}
    buffer, record = io.BytesIO(), []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as wheel:
        for file in distribution.files or []:
            name = file.as_posix()
            if name.startswith("../") or "/__pycache__/" in name or name in left_out:
                continue
            content = file.read_binary()
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            assert file.hash is not None and (file.hash.mode, file.hash.value) == ("sha256", digest), name
            wheel.writestr(name, content)
            record.append(f"{name},sha256={digest},{len(content)}")
        wheel.writestr(f"{dist_info}/RECORD", "\n".join([*record, f"{dist_info}/RECORD,,"]) + "\n")
    assert len(record) > 100, "the installed pip is complete"
    return distribution.version, buffer.getvalue()


@pytest.mark.parametrize("source", ["PIP_CONFIG_FILE", "environment", "global", "site", "user"])
def test_the_real_pip_installs_itself_into_the_environment_whatever_pips_configuration_says(
    real_pip, house, tmp_path, source: str,
) -> None:
    version, content = real_pip
    name = f"pip-{version}-py3-none-any.whl"
    house.write_lock(house.put(content, name=name), requirement=f"pip=={version}")
    python = new_pipless_python(tmp_path / "venv")
    elsewhere, home = tmp_path / "elsewhere", tmp_path / "home"
    # No configuration of this machine is read or written: every place pip
    # looks for its files is under tmp_path, but /etc/pip.conf.
    settings = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CONFIG_DIRS": str(tmp_path / "xdg")}
    configuration = {
        "PIP_CONFIG_FILE": tmp_path / "pip-config-file.conf",
        "global": tmp_path / "xdg" / "pip" / "pip.conf",
        "site": tmp_path / "venv" / "pip.conf",
        "user": home / ".config" / "pip" / "pip.conf",
    }.get(source)
    if configuration is not None:
        configuration.parent.mkdir(parents=True, exist_ok=True)
        configuration.write_text(f"[install]\ntarget = {elsewhere}\n", encoding="utf-8")
    if source == "PIP_CONFIG_FILE":
        settings["PIP_CONFIG_FILE"] = str(configuration)
    if source == "environment":
        settings.update(PIP_TARGET=str(elsewhere), PIP_USER="1", PIP_PREFIX=str(elsewhere))
    # pip, run as the bootstrap does but without its settings, would install
    # into elsewhere.
    followed = subprocess.run([python, "-I", str(house.directory / name / "pip"), "config", "list"],
                              env=environment(house, **settings), capture_output=True, text=True, timeout=120)
    assert f"target='{elsewhere}'" in followed.stdout, (followed.stdout, followed.stderr)

    proc = run(python, house, **settings)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    found, package, site = imports(python)
    assert (found, package) == (version, site / "pip")
    assert site.is_relative_to((tmp_path / "venv").resolve())
    assert f"pip {version} installed for {python}, which imports it from {package}" in proc.stdout
    assert not elsewhere.exists()
