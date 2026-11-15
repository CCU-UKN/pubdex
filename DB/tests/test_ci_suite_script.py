"""Orchestration of run_ci_suite.sh, offline.

The real script runs from a scratch copy of the repository layout in which
every stage is a stub that records its name, arguments and working directory,
and fails or runs slowly when asked to. Stub `docker` and `git` executables
stand in for the real ones; the stub `docker` reports an Engine and a Compose
version, so that the version requirements can be checked, and a stub file
stands where the pinned helm belongs. This pins, without Docker, the stage order, that
the first failure stops the run with its own status, how --base/--head reach
the transform-version guard and the check of the range's commits (the
all-zero and empty first-push forms included), the prerequisite diagnostics,
and what an interrupt or a timeout does. The last tests run the shell steps of
the GitHub and Forgejo workflows with each forge's event context, and pin both
workflows as thin adapters around this one command. That stage 1 cannot
inherit database settings is proven with the real run_checks.sh in
test_offline_isolation.py.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "run_ci_suite.sh"
BASH = shutil.which("bash") or "/bin/bash"

# stage name -> path of the component it stands in for, in the order the
# suite has to run them
STAGES = {
    "offline": "run_checks.sh",
    "secrets": "check_secrets_and_local_config.py",
    "migration": "DB/run_migration_smoke.sh",
    "demo": "DB/run_task_1a_demo.sh",
    "integration": "DB/run_disposable_integration.sh",
    "stack": "run_stack_acceptance.sh",
    "operator": "run_operator_acceptance.sh",
    "charts": "run_chart_checks.sh",
    "cluster": "run_cluster_acceptance.sh",
}
ORDER = list(STAGES)
COUNT = len(STAGES)
# Where the pinned tools belong (cluster_tool_path in DB/lib/cluster_tools.sh).
TOOL_VERSIONS = {tool: re.search(rf"^readonly PINNED_{tool.upper()}_VERSION=(\S+)$",
                                 (ROOT / "DB" / "lib" / "cluster_tools.sh").read_text(encoding="utf-8"), re.M).group(1)
                 for tool in ("helm", "kubectl", "kind")}
TOOLS = {tool: Path(".tools") / f"{tool}-{version}" / tool for tool, version in TOOL_VERSIONS.items()}
HELM_VERSION = TOOL_VERSIONS["helm"]
HELM = TOOLS["helm"]

STUB_STAGE = r'''#!/usr/bin/env bash
name="@NAME@"
{
  printf '%s' "$name"
  for arg in "$@"; do printf '\037%s' "$arg"; done
  printf '\n'
} >> "$STUB_LOG"
printf '%s\t%s\n' "$name" "$PWD" >> "$STUB_LOG.context"
if [ "${STUB_SLOW:-}" = "$name" ]; then
  trap 'echo "$name cleaned up after INT" >> "$STUB_LOG.cleanup"; exit 130' INT
  trap 'echo "$name cleaned up after TERM" >> "$STUB_LOG.cleanup"; exit 143' TERM
  for _ in $(seq "${STUB_SLOW_TICKS:-300}"); do sleep 0.1; done
  echo "$name finished" >> "$STUB_LOG.cleanup"
fi
if [ "${STUB_FAIL:-}" = "$name" ]; then
  exit "${STUB_FAIL_STATUS:-7}"
fi
exit 0
'''

STUB_DOCKER = r'''#!/bin/sh
echo "$*" >> "$STUB_LOG.docker"
[ "${STUB_DOCKER:-up}" = down ] && exit 1
case "$*" in
  "version --format {{.Server.Version}}") echo "${STUB_ENGINE:-29.9.1}" ;;
  "compose version --short") [ "${STUB_COMPOSE:-5.6.0}" = none ] && exit 1; echo "${STUB_COMPOSE:-5.6.0}" ;;
esac
exit 0
'''
STUB_GIT = '#!/bin/sh\n[ "${STUB_CHECKOUT:-yes}" = no ] && exit 128\necho true\n'
# Stands in for .venv/bin/python: answers the requirements probe and runs the
# stub checker, which is a shell script like the other stages.
# -c: the import check and the version report; -m: the pip version report,
# answered here so that no real program of that name runs; anything else is
# a stage script, run by Bash.
STUB_PYTHON = (f'#!{BASH}\nif [ "$1" = -c ]; then exit "${{STUB_IMPORTS_FAIL:-0}}"; fi\n'
               f'if [ "$1" = -m ]; then echo "stub 0.0"; exit 0; fi\nexec {BASH} "$@"\n')


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class Fake:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "checkout"
        self.root.mkdir()
        self.script = self.root / SUITE.name
        shutil.copy2(SUITE, self.script)
        (self.root / "DB" / "lib").mkdir(parents=True)
        shutil.copy2(ROOT / "DB" / "lib" / "docker_versions.sh", self.root / "DB" / "lib")
        shutil.copy2(ROOT / "DB" / "lib" / "cluster_tools.sh", self.root / "DB" / "lib")
        # The suite only looks for them; stages 8 and 9 check their hashes and versions.
        for relative in TOOLS.values():
            _executable(self.root / relative, "#!/bin/sh\nexit 1\n")
        for name, relative in STAGES.items():
            _executable(self.root / relative, STUB_STAGE.replace("@NAME@", name))
        self.python = self.root / ".venv" / "bin" / "python"
        _executable(self.python, STUB_PYTHON)
        self.bindir = tmp_path / "bin"
        _executable(self.bindir / "docker", STUB_DOCKER)
        _executable(self.bindir / "git", STUB_GIT)
        self.log = tmp_path / "stages.log"
        self.env = dict(os.environ, PATH=f"{self.bindir}{os.pathsep}{os.environ['PATH']}", STUB_LOG=str(self.log))

    def run(self, *args: str, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(self.script), *args], cwd=cwd or self.tmp, env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=120)

    def popen(self, command: list, **kwargs) -> subprocess.Popen:
        return subprocess.Popen(command, cwd=self.tmp, env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, **kwargs)

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return [line.split("\x1f") for line in self.log.read_text(encoding="utf-8").splitlines()]

    def names(self) -> list:
        return [call[0] for call in self.calls()]

    def args_of(self, name: str) -> list:
        return next(call[1:] for call in self.calls() if call[0] == name)

    def lines(self, suffix: str) -> list:
        path = Path(str(self.log) + suffix)
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def wait_for_stage(self, name: str, proc: subprocess.Popen) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if name in self.names():
                return
            time.sleep(0.05)
        proc.kill()
        pytest.fail(f"the suite never started stage {name}")


def test_help_lists_every_stage_and_runs_nothing(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run("--help")
    assert proc.returncode == 0
    assert proc.stdout.startswith("usage: run_ci_suite.sh")
    for relative in STAGES.values():
        assert relative in proc.stdout
    assert fake.calls() == [] and fake.lines(".docker") == []


def test_stages_run_in_order_from_any_directory(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = fake.run(cwd=elsewhere)
    assert proc.returncode == 0, proc.stderr
    assert fake.names() == ORDER
    assert all(call[1:] == [] for call in fake.calls()), "no stage gets arguments without a range"
    headers = [line for line in proc.stdout.splitlines() if line.startswith("==> Stage ") and "passed" not in line]
    assert [h.split(":")[0] for h in headers] == [f"==> Stage {n}/{COUNT}" for n in range(1, COUNT + 1)]
    assert COUNT == 9 and f"PASS: all {COUNT} stages" in proc.stdout
    for context in fake.lines(".context"):
        _name, cwd = context.split("\t")
        assert Path(cwd).resolve() == fake.root.resolve(), "stages run from the repository root"
    assert fake.lines(".docker") == ["info", "version --format {{.Server.Version}}", "compose version --short"], (
        "the daemon and the Compose plugin are probed, and their versions reported, before any stage"
    )
    assert "Environment:" in proc.stdout and "  bash    " in proc.stdout
    assert "  docker  29.9.1" in proc.stdout and "  compose 5.6.0" in proc.stdout


@pytest.mark.parametrize("failing", ORDER)
def test_the_first_failure_stops_the_run_with_its_status(tmp_path: Path, failing: str) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(STUB_FAIL=failing, STUB_FAIL_STATUS="7")
    assert proc.returncode == 7
    position = ORDER.index(failing)
    assert fake.names() == ORDER[: position + 1], "no later stage starts"
    assert f"stage {position + 1}/{COUNT}" in proc.stderr and "failed with exit status 7" in proc.stderr
    assert "later stages were not run" in proc.stderr
    assert "PASS" not in proc.stdout


ZERO = "0" * 40


@pytest.mark.parametrize(
    "args, forwarded",
    [
        ([], []),
        (["--base", "abc123"], ["--base", "abc123"]),
        (["--base", "abc123", "--head", "def456"], ["--base", "abc123", "--head", "def456"]),
        (["--head", "def456", "--base", "abc123"], ["--base", "abc123", "--head", "def456"]),
        (["--base", ZERO, "--head", "def456"], ["--base", ZERO, "--head", "def456"]),
        (["--base", "", "--head", "def456"], ["--base", "", "--head", "def456"]),
    ],
    ids=["no-range", "base-only", "base-and-head", "any-order", "all-zero-base", "empty-base"],
)
def test_the_range_reaches_the_offline_and_secrets_stages_unchanged(tmp_path: Path, args: list, forwarded: list) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(*args)
    assert proc.returncode == 0, proc.stderr
    # The transform-version guard and the check of the range's commits.
    assert fake.args_of("offline") == forwarded
    assert fake.args_of("secrets") == forwarded
    for name in ORDER[2:]:
        assert fake.args_of(name) == [], f"{name} gets no range"


@pytest.mark.parametrize(
    "args, message",
    [
        (["--head", "def456"], "--head requires --base"),
        (["--base"], "--base needs a value"),
        (["--base", "abc123", "--head"], "--head needs a value"),
        (["--staged"], "pre-commit mode of ./run_checks.sh"),
        (["--bogus"], "unknown argument: --bogus"),
    ],
)
def test_invalid_arguments_are_usage_errors(tmp_path: Path, args: list, message: str) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(*args)
    assert proc.returncode == 2
    assert message in proc.stderr and "usage: run_ci_suite.sh" in proc.stderr
    assert fake.calls() == [] and fake.lines(".docker") == []


def _restricted_path(tmp_path: Path, *extra: Path) -> str:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    os.symlink(shutil.which("dirname"), tools / "dirname")
    for item in extra:
        os.symlink(item, tools / item.name)
    return str(tools)


def test_prerequisite_diagnostics(tmp_path: Path) -> None:
    fake = Fake(tmp_path)

    proc = fake.run(STUB_DOCKER="down")
    assert proc.returncode == 1 and "cannot reach the Docker daemon" in proc.stderr

    proc = fake.run(STUB_CHECKOUT="no")
    assert proc.returncode == 1 and "not a Git checkout" in proc.stderr

    # Below 28.0.1 other hosts can reach ports published on loopback, and in
    # 28.2.0 to 28.3.2 again after a firewalld reload: refused although they
    # start the stack.
    for refused in ("24.0.7", "25.0.5", "26.1.5+dfsg1-9+deb13u1", "27.5.1", "28.0.0"):
        proc = fake.run(STUB_ENGINE=refused)
        assert proc.returncode == 1 and f"Docker Engine {refused} is too old" in proc.stderr, refused
        assert "need Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2" in proc.stderr
        assert "can let other hosts reach the ports it publishes on 127.0.0.1" in " ".join(proc.stderr.split())
    for refused in ("28.2.0", "28.3.2"):
        proc = fake.run(STUB_ENGINE=refused)
        assert proc.returncode == 1 and "a firewalld reload opens ports published on 127.0.0.1" in proc.stderr
    assert fake.calls() == [], "no stage runs on an unsupported Engine"
    # The first supported release, GitHub's ubuntu-24.04 runner, the fix of
    # CVE-2025-54388 and a current release.
    for accepted in ("28.0.1", "28.0.4", "28.3.3", "29.9.2"):
        assert fake.run(STUB_ENGINE=accepted, STUB_FAIL="offline").returncode == 7, accepted
    fake.log.unlink()

    proc = fake.run(STUB_ENGINE="dev")
    assert proc.returncode == 1 and "Docker Engine reports the version 'dev'" in proc.stderr

    proc = fake.run(STUB_COMPOSE="none")
    assert proc.returncode == 1 and "the Docker Compose plugin was not found" in proc.stderr
    assert "Install the Compose plugin, version 2.24.0 or later" in proc.stderr

    # 2.20.1 and older reject the start_interval of compose.yaml; 2.20.2 to
    # 2.23.x leave it out of the containers.
    for old in ("2.19.1", "2.20.0", "2.20.1", "2.20.2", "2.23.3", "2.24.0-rc.1", "v1.29.2"):
        proc = fake.run(STUB_COMPOSE=old)
        assert proc.returncode == 1 and f"Docker Compose {old} is too old" in proc.stderr, old
        assert "stages 6 and 7 need the Compose plugin 2.24.0 or later" in proc.stderr
        assert "Upgrade the Compose plugin" in proc.stderr
    for unknown in ("dev", "2.24"):
        proc = fake.run(STUB_COMPOSE=unknown)
        assert proc.returncode == 1 and f"reports the version '{unknown}', which is not a release number" in proc.stderr
    assert fake.calls() == [], "no stage runs while a prerequisite is missing"

    for accepted in ("2.24.0", "v2.24.0", "2.24.6+ds1-0ubuntu1~22.04.1", "2.24.1-rc.1", "v2.39.1", "5.6.0"):
        assert fake.run(STUB_COMPOSE=accepted, STUB_FAIL="offline").returncode == 7, accepted
    fake.log.unlink()

    tools = _restricted_path(tmp_path, fake.bindir / "git")
    proc = fake.run(PATH=tools)
    assert proc.returncode == 1 and "docker was not found on PATH" in proc.stderr
    assert "./run_checks.sh" in proc.stderr, "the offline alternative is named"

    (tmp_path / "tools" / "git").unlink()
    proc = fake.run(PATH=tools)
    assert proc.returncode == 1 and "git was not found on PATH" in proc.stderr

    # An existing .venv that lacks packages is created anew: no pip of its own
    # installs anything.
    proc = fake.run(STUB_IMPORTS_FAIL="1")
    assert proc.returncode == 1 and "lacks the development requirements" in proc.stderr
    assert "Remove .venv and create it again with:" in proc.stderr and "./setup_venv.sh" in proc.stderr
    assert "pip install" not in proc.stderr and "install_pip.py" not in proc.stderr

    # The pinned tools are looked for before any stage runs, not once seven
    # or eight stages have passed.
    for tool in ("kind", "kubectl"):
        (fake.root / TOOLS[tool]).unlink()
        proc = fake.run()
        assert proc.returncode == 1 and f"the pinned {tool} {TOOL_VERSIONS[tool]} is not installed" in proc.stderr
        assert "stage 9 tests the Helm chart on a disposable kind cluster with it" in proc.stderr
        assert _hint(proc.stderr) == ["./setup_cluster_tools.sh"]
        _executable(fake.root / TOOLS[tool], "#!/bin/sh\nexit 1\n")
    (fake.root / HELM).unlink()
    proc = fake.run()
    assert proc.returncode == 1 and f"the pinned helm {HELM_VERSION} is not installed" in proc.stderr
    assert "stage 8 checks the Helm chart with it" in proc.stderr
    assert _hint(proc.stderr) == ["./setup_cluster_tools.sh"]

    fake.python.unlink()
    proc = fake.run()
    assert proc.returncode == 1 and "./setup_venv.sh" in proc.stderr
    assert _hint(proc.stderr) == FRESH_SETUP

    assert fake.calls() == [], "no stage runs while a prerequisite is missing"


# Every .venv is set up by one command, ./setup_venv.sh: create it without
# pip, fetch the locked files over HTTPS with DB/fetch_verified.py, install
# the pip of its own lock from them with DB/install_pip.py, which runs no
# other pip, then the development lock with DB/install_locked.py, from those
# files alone -- the files, pip and steps that CI and the image build use.
# The scripts that need .venv say so when it is missing, and the
# documentation gives the same command.
FRESH_SETUP = ["./setup_venv.sh"]
VENV_HINTS = ["run_checks.sh", "DB/run_task_1a_demo.sh", "DB/run_disposable_integration.sh", "run_chart_checks.sh"]
SETUP_DOCUMENTS = ["README.md", "QUICKSTART.md", "DB/README.md", "DB/TESTING_STRATEGY.md",
                   ".github/workflows/db-tests.yml", ".forgejo/workflows/db-tests.yml",
                   "run_ci_suite.sh", *VENV_HINTS]
# The records of tasks 1a and 1b keep the commands their results were
# verified with, before pip had a lock of its own; they are not checked here.
OTHER_SETUP = re.compile(r"pip install\b|-m venv\b|python\S* \S*(?:install_pip|install_locked|fetch_verified)\.py")


def _run_setup_script(tmp_path: Path, *args: str, interpreter: str = "python3.11") -> list:
    """Runs setup_venv.sh in a scratch checkout whose interpreters only record
    how they are called, and returns those calls in order, with the
    checkout's path as <root> and the temporary wheelhouse as <wheelhouse>."""
    checkout, log = tmp_path / "setup-checkout", tmp_path / "setup-calls.log"
    checkout.mkdir()
    shutil.copy2(ROOT / "setup_venv.sh", checkout / "setup_venv.sh")
    venv_python = tmp_path / "setup-venv-python"
    _executable(venv_python, f'#!/bin/sh\necho "venv-python $*" >> "{log}"\n')
    # The interpreter answers the version check, and creates .venv where
    # -m venv asks.
    _executable(tmp_path / "setup-bin" / interpreter,
                f'#!/bin/sh\necho "{interpreter} $*" >> "{log}"\n'
                f'if [ "$1" = -m ]; then mkdir -p "$4/bin" && cp "{venv_python}" "$4/bin/python"; fi\n')
    environment = dict(os.environ, PATH=f"{tmp_path / 'setup-bin'}{os.pathsep}{os.environ['PATH']}",
                       TMPDIR=str(tmp_path))
    proc = subprocess.run([BASH, str(checkout / "setup_venv.sh"), *args], cwd=tmp_path, env=environment,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text(encoding="utf-8").replace(str(checkout), "<root>").splitlines()
    return [re.sub(re.escape(str(tmp_path)) + r"/tmp\.\w+", "<wheelhouse>", call) for call in calls]


def test_setup_venv_creates_venv_without_pip_and_installs_only_from_the_verified_files(tmp_path: Path) -> None:
    calls = _run_setup_script(tmp_path)
    assert [call for call in calls if " -c " not in call] == [
        "python3.11 -m venv --without-pip <root>/.venv",
        "venv-python -I <root>/DB/fetch_verified.py wheels --dest <wheelhouse> --allow-source psycopg-c "
        "<root>/DB/requirements-pip.txt <root>/DB/requirements-build.txt <root>/DB/requirements-dev.txt",
        "venv-python <root>/DB/install_pip.py --wheelhouse <wheelhouse> --network-isolation preferred",
        "venv-python <root>/DB/install_locked.py --wheelhouse <wheelhouse> --psycopg c --network-isolation preferred "
        "<root>/DB/requirements-dev.txt",
        "venv-python -I -m pip check",
    ]
    assert not list(tmp_path.glob("tmp.*")), "the wheelhouse is removed"


def test_setup_venv_passes_its_choices_on_and_refuses_an_existing_venv(tmp_path: Path) -> None:
    calls = _run_setup_script(tmp_path, "--python", "python3", "--psycopg", "python",
                              "--network-isolation", "required", interpreter="python3")
    assert "python3 -m venv --without-pip <root>/.venv" in calls
    assert any(call.endswith("--wheelhouse <wheelhouse> --network-isolation required") for call in calls)
    assert any("--psycopg python --network-isolation required" in call for call in calls)
    again = subprocess.run([BASH, str(tmp_path / "setup-checkout" / "setup_venv.sh"), "--python", "python3"],
                           env=dict(os.environ, PATH=f"{tmp_path / 'setup-bin'}{os.pathsep}{os.environ['PATH']}"),
                           capture_output=True, text=True, timeout=60)
    assert again.returncode == 1 and ".venv exists already" in again.stderr
    for args in (["--psycopg", "binary"], ["--network-isolation", "none"], ["--bogus"]):
        usage = subprocess.run([BASH, str(ROOT / "setup_venv.sh"), *args], capture_output=True, text=True, timeout=60)
        assert usage.returncode == 2 and "usage: setup_venv.sh" in usage.stderr, args


def _hint(stderr: str) -> list:
    """The commands a message for a missing prerequisite gives: the indented
    lines after the one that says how to create or install it."""
    lines = stderr.splitlines()
    start = next(index for index, line in enumerate(lines) if line.endswith(" it from the repository root with:"))
    commands = []
    for line in lines[start + 1:]:
        if not line.startswith("  "):
            break
        commands.append(line.strip())
    return commands


@pytest.mark.parametrize("relative", VENV_HINTS)
def test_each_script_that_needs_the_venv_says_how_to_create_it_with_the_pinned_pip(
    tmp_path: Path, relative: str,
) -> None:
    # The script alone in a checkout without .venv: each looks for the
    # interpreter before it reads anything else of the checkout.
    script = tmp_path / "checkout" / relative
    script.parent.mkdir(parents=True)
    shutil.copy2(ROOT / relative, script)
    proc = subprocess.run([BASH, str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1 and "no interpreter at .venv/bin/python" in proc.stderr, proc.stderr
    assert _hint(proc.stderr) == FRESH_SETUP


@pytest.mark.parametrize("relative", SETUP_DOCUMENTS)
def test_every_setup_is_setup_venv_and_no_other_pip_or_download(relative: str) -> None:
    # No setup creates .venv, runs pip or fetches anything but through
    # setup_venv.sh: so no pip from ensurepip, no pip that downloads, and no
    # source build but psycopg-c's. Commands in prose count as well.
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert "setup_venv.sh" in text, f"{relative} gives no setup command"
    assert OTHER_SETUP.findall(text) == [], relative


def test_an_interrupt_from_the_terminal_stops_the_running_stage_and_the_suite(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.env["STUB_SLOW"] = "migration"
    proc = fake.popen([BASH, str(fake.script)], start_new_session=True)
    fake.wait_for_stage("migration", proc)
    os.killpg(proc.pid, signal.SIGINT)  # what Ctrl-C does: the whole foreground group
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    assert fake.lines(".cleanup") == ["migration cleaned up after INT"]
    assert fake.names() == ORDER[:3]


@pytest.mark.skipif(shutil.which("timeout") is None, reason="needs the timeout utility")
def test_an_external_timeout_stops_the_running_stage_and_the_suite(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.env["STUB_SLOW"] = "demo"
    proc = fake.popen(["timeout", "-s", "TERM", "1", BASH, str(fake.script)])
    proc.communicate(timeout=60)
    assert proc.returncode == 124
    assert fake.lines(".cleanup") == ["demo cleaned up after TERM"]
    assert fake.names() == ORDER[:4]


def test_a_signal_to_the_suite_alone_lets_the_stage_finish_and_starts_no_other(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.env.update(STUB_SLOW="migration", STUB_SLOW_TICKS="8")
    proc = fake.popen([BASH, str(fake.script)])
    fake.wait_for_stage("migration", proc)
    proc.send_signal(signal.SIGTERM)
    _out, err = proc.communicate(timeout=30)
    assert proc.returncode == 143
    assert fake.lines(".cleanup") == ["migration finished"]
    assert fake.names() == ORDER[:3]
    assert "no further stage runs" in err


def test_the_stage_components_exist_in_this_repository() -> None:
    for relative in STAGES.values():
        path = ROOT / relative
        assert path.is_file(), relative
        if relative.endswith(".sh"):
            assert os.access(path, os.X_OK), f"{relative} is executable"
    assert os.access(SUITE, os.X_OK)


def _gitlab_example() -> tuple:
    """The GitLab CI example in DB/TESTING_STRATEGY.md: its text and the
    commands of its script."""
    text = (ROOT / "DB" / "TESTING_STRATEGY.md").read_text(encoding="utf-8")
    block = re.search(r"```yaml\n(verify:[\n].*?)```", text, re.S)
    assert block, "the example is where the documentation says"
    commands = [line.split("- ", 1)[1] for line in block.group(1).splitlines() if line.startswith("    - ")]
    return block.group(1), commands


HEAD_SHA, PREVIOUS_SHA, MERGE_BASE_SHA = "b" * 40, "a" * 40, "c" * 40


@pytest.mark.parametrize(
    "variables, base",
    [
        ({"CI_COMMIT_BEFORE_SHA": PREVIOUS_SHA}, PREVIOUS_SHA),
        ({"CI_COMMIT_BEFORE_SHA": ZERO}, ZERO),
        ({"CI_MERGE_REQUEST_DIFF_BASE_SHA": MERGE_BASE_SHA, "CI_COMMIT_BEFORE_SHA": ZERO}, MERGE_BASE_SHA),
    ],
    ids=["push", "first-push", "merge-request"],
)
def test_the_gitlab_example_installs_the_requirements_and_hands_the_suite_its_range(
    tmp_path: Path, variables: dict, base: str,
) -> None:
    block, commands = _gitlab_example()
    assert 'GIT_DEPTH: "0"' in block, "the commit-range checks need the complete history"
    # A checkout whose setup and suite only record how they are called;
    # setup_venv.sh's own steps are tested above.
    checkout, log = tmp_path / "checkout", tmp_path / "calls.log"
    _executable(checkout / "run_ci_suite.sh", f'#!/bin/sh\necho "suite $*" >> "{log}"\n')
    _executable(checkout / "setup_venv.sh", f'#!/bin/sh\necho "setup $*" >> "{log}"\n')
    _executable(checkout / "setup_cluster_tools.sh", f'#!/bin/sh\necho "tools $*" >> "{log}"\n')
    environment = {key: value for key, value in os.environ.items() if not key.startswith("CI_")}
    environment.update(variables, CI_COMMIT_SHA=HEAD_SHA)
    # As a shell executor runs them: Bash, stopping at the first failure.
    job = "set -eo pipefail\n" + "\n".join(commands) + "\n"
    proc = subprocess.run([BASH, "-c", job], cwd=checkout, env=environment, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "setup --python python3.11",
        "tools ",
        f"suite --base {base} --head {HEAD_SHA}",
    ]


def test_public_ci_is_only_an_adapter_around_the_suite() -> None:
    workflow = (ROOT / ".github" / "workflows" / "db-tests.yml").read_text(encoding="utf-8")
    code = [line.split("#", 1)[0] for line in workflow.splitlines()]
    steps = "\n".join(code)
    assert './run_ci_suite.sh --base "$BASE" --head "$HEAD"' in steps
    assert "./setup_venv.sh --python python" in steps
    assert "run: ./setup_cluster_tools.sh\n" in steps
    for forbidden in ("pytest", "docker", "psql", "postgres", "services:", "secrets.", "pip install", "-m venv",
                      "install_pip", "install_locked", "fetch_verified",
                      "run_checks.sh", "run_integration_tests", "run_migration_smoke", "run_task_1a_demo",
                      "run_stack_acceptance", "build_image", "create_secrets", "helm", "kubectl", "run_chart_checks",
                      "run_cluster_acceptance"):
        assert forbidden not in steps, f"{forbidden!r} belongs in a tracked script, not in the workflow"
    assert "contents: read" in steps
    assert "fetch-depth: 0" in steps
    assert "persist-credentials: false" in steps
    assert "timeout-minutes:" in steps
    # Inputs that would otherwise move are fixed: actions by commit, a runner
    # image release, an exact Python version, and pip from its hashed lock.
    uses = [line.split("uses:", 1)[1].strip() for line in code if "uses:" in line]
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action) for action in uses), uses
    assert "runs-on: ubuntu-latest" not in steps and re.search(r"runs-on: ubuntu-\d\d\.\d\d", steps)
    assert re.search(r'python-version: "3\.11\.\d+"', steps)


# The CI adapters: forge -> (its workflow, the context names its expressions
# may use, the interpreter its install step calls).
ADAPTERS = {
    "github": (ROOT / ".github" / "workflows" / "db-tests.yml", ("github",), "python"),
    "forgejo": (ROOT / ".forgejo" / "workflows" / "db-tests.yml", ("forgejo", "forge", "github"), "python3.11"),
}
BASE_TIP, MERGE_COMMIT = "d" * 40, "e" * 40


def _adapter_job(workflow: Path) -> tuple:
    """The workflow's `env:` expressions and the commands of its `run:` steps
    in order, for the two forms the adapters use: one-line and `|` values."""
    text = workflow.read_text(encoding="utf-8")
    expressions = dict(re.findall(r"^ +([A-Z_]+): (\$\{\{.*\}\})$", text, re.M))
    commands = []
    for _indent, block, line in re.findall(r"^( *)run: (?:\|\n((?:\1 +\S.*\n)+)|(\S.*)\n)", text, re.M):
        if block:
            lines = block.splitlines()
            width = min(len(entry) - len(entry.lstrip()) for entry in lines)
            commands += [entry[width:] for entry in lines]
        else:
            commands.append(line)
    return expressions, commands


def _evaluate(expression: str, contexts: dict) -> str:
    """A `${{ }}` value of the form the adapters use -- context properties
    joined by `||` -- as GitHub and Forgejo evaluate it: the first operand
    that is set and not empty, else empty."""
    inner = re.fullmatch(r"\$\{\{ *(.+?) *\}\}", expression)
    assert inner, expression
    for operand in (part.strip() for part in inner.group(1).split("||")):
        assert re.fullmatch(r"[a-z]+(\.[a-z_]+)+", operand), f"unsupported expression {operand!r}; extend this test"
        value = contexts
        for key in operand.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value:
            return str(value)
    return ""


PUSH = {"sha": HEAD_SHA, "event": {"before": PREVIOUS_SHA, "after": HEAD_SHA}}
FIRST_PUSH = {"sha": HEAD_SHA, "event": {"before": ZERO, "after": HEAD_SHA}}
# (forge, event) -> (that event's context, the base and head the suite has to get)
EVENT_RANGES = {
    ("github", "push"): (PUSH, PREVIOUS_SHA, HEAD_SHA),
    ("github", "first-push"): (FIRST_PUSH, ZERO, HEAD_SHA),
    # GitHub runs a pull request on the merge commit it creates on top of the
    # base branch's current tip.
    ("github", "pull-request"): (
        {"sha": MERGE_COMMIT, "event": {"pull_request": {"base": {"sha": BASE_TIP}, "head": {"sha": HEAD_SHA}}}},
        BASE_TIP, MERGE_COMMIT,
    ),
    ("forgejo", "push"): (PUSH, PREVIOUS_SHA, HEAD_SHA),
    ("forgejo", "first-push"): (FIRST_PUSH, ZERO, HEAD_SHA),
    # Forgejo runs a pull request on its head commit, so the range starts at
    # the merge base; the base branch's tip may have moved on since.
    ("forgejo", "pull-request"): (
        {"sha": HEAD_SHA, "event": {"pull_request": {
            "merge_base": MERGE_BASE_SHA, "base": {"sha": BASE_TIP}, "head": {"sha": HEAD_SHA}}}},
        MERGE_BASE_SHA, HEAD_SHA,
    ),
    # Without a merge base the whole history is read, never a range that
    # starts at the base branch's tip.
    ("forgejo", "pull-request-without-merge-base"): (
        {"sha": HEAD_SHA, "event": {"pull_request": {"base": {"sha": BASE_TIP}, "head": {"sha": HEAD_SHA}}}},
        "", HEAD_SHA,
    ),
}


@pytest.mark.parametrize("forge, event", list(EVENT_RANGES), ids=["-".join(key) for key in EVENT_RANGES])
def test_each_ci_adapter_installs_the_requirements_and_hands_the_suite_its_events_range(
    tmp_path: Path, forge: str, event: str,
) -> None:
    workflow, names, interpreter = ADAPTERS[forge]
    context, base, head = EVENT_RANGES[(forge, event)]
    expressions, commands = _adapter_job(workflow)
    variables = {name: _evaluate(expression, dict.fromkeys(names, context)) for name, expression in expressions.items()}
    # A checkout whose setup and suite only record how they are called;
    # setup_venv.sh's own steps are tested above.
    checkout, log = tmp_path / "checkout", tmp_path / "calls.log"
    _executable(checkout / "run_ci_suite.sh", f'#!/bin/sh\necho "suite $*" >> "{log}"\n')
    _executable(checkout / "setup_venv.sh", f'#!/bin/sh\necho "setup $*" >> "{log}"\n')
    _executable(checkout / "setup_cluster_tools.sh", f'#!/bin/sh\necho "tools $*" >> "{log}"\n')
    environment = {key: value for key, value in os.environ.items() if key not in variables}
    environment.update(variables)
    # As both runners execute run steps: Bash, stopping at the first failure.
    proc = subprocess.run([BASH, "-e", "-c", "\n".join(commands) + "\n"], cwd=checkout, env=environment,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls == [f"setup --python {interpreter}", "tools ", f"suite --base {base} --head {head}"]


def test_the_forgejo_adapter_runs_on_a_host_runner_with_forgejo_hosted_actions() -> None:
    github, forgejo = (ADAPTERS[forge][0].read_text(encoding="utf-8") for forge in ("github", "forgejo"))
    code = "\n".join(line.split("#", 1)[0] for line in forgejo.splitlines())
    # The same thin adapter as on GitHub ...
    assert './run_ci_suite.sh --base "$BASE" --head "$HEAD"' in code
    assert "./setup_venv.sh --python python3.11" in code
    assert "run: ./setup_cluster_tools.sh\n" in code
    for forbidden in ("pytest", "docker", "psql", "postgres", "services:", "secrets.", "pip install", "-m venv",
                      "install_pip", "install_locked", "fetch_verified",
                      "run_checks.sh", "run_integration_tests", "run_migration_smoke", "run_task_1a_demo",
                      "run_stack_acceptance", "build_image", "create_secrets", "helm", "kubectl", "run_chart_checks",
                      "run_cluster_acceptance"):
        assert forbidden not in code, f"{forbidden!r} belongs in a tracked script, not in the workflow"
    assert "fetch-depth: 0" in code and "persist-credentials: false" in code and "timeout-minutes:" in code
    # ... for a runner that runs the job on its host, under the label the
    # runner contract names ...
    label = re.search(r"runs-on: ([\w.-]+)\n", code).group(1)
    assert f"`{label}:host`" in (ROOT / "DB" / "TESTING_STRATEGY.md").read_text(encoding="utf-8")
    # ... with actions from Forgejo's own hosting alone, named by absolute URL
    # and pinned by commit, and no action that fetches an interpreter.
    uses = re.findall(r"uses: (\S+)", code)
    assert uses and all(re.fullmatch(r"https://data\.forgejo\.org/[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses), uses
    assert "setup-python" not in code and "github.com" not in code
    # Both forges run the same checkout code and the same setup, from the locks.
    def pins(text: str) -> tuple:
        checkout = re.search(r"checkout@([0-9a-f]{40})", text)
        setup = re.search(r"run: (\./setup_venv\.sh) --python \S+\n", text)
        assert checkout and setup, "each adapter pins the checkout commit and sets up with setup_venv.sh"
        return checkout.group(1), setup.group(1)

    assert pins(github) == pins(forgejo)

    # Both give the job the same time limit, set for the nine-stage suite, and
    # the description of the Forgejo job states that limit.
    def limit(text: str) -> str:
        limits = re.findall(r"^    timeout-minutes: (\d+)$", text, re.M)
        assert len(limits) == 1, limits
        return limits[0]

    assert limit(github) == limit(forgejo)
    strategy = " ".join((ROOT / "DB" / "TESTING_STRATEGY.md").read_text(encoding="utf-8").split())
    assert f"under a {limit(github)}-minute limit: the same three steps and the same limit as on GitHub" in strategy
