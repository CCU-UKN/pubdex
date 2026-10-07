"""Orchestration of run_ci_suite.sh, offline.

The real script runs from a scratch copy of the repository layout in which
every stage is a stub that records its name, arguments and working directory,
and fails or runs slowly when asked to. Stub `docker` and `git` executables
stand in for the real ones. This pins, without Docker, the stage order, that
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
}
ORDER = list(STAGES)

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

STUB_DOCKER = '#!/bin/sh\necho "$*" >> "$STUB_LOG.docker"\n[ "${STUB_DOCKER:-up}" = down ] && exit 1\nexit 0\n'
STUB_GIT = '#!/bin/sh\n[ "${STUB_CHECKOUT:-yes}" = no ] && exit 128\necho true\n'
# Stands in for .venv/bin/python: answers the requirements probe and runs the
# stub checker, which is a shell script like the other stages.
STUB_PYTHON = f'#!{BASH}\nif [ "$1" = -c ]; then exit "${{STUB_IMPORTS_FAIL:-0}}"; fi\nexec {BASH} "$@"\n'


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
    assert [h.split(":")[0] for h in headers] == [f"==> Stage {n}/5" for n in range(1, 6)]
    assert "PASS: all 5 stages" in proc.stdout
    for context in fake.lines(".context"):
        _name, cwd = context.split("\t")
        assert Path(cwd).resolve() == fake.root.resolve(), "stages run from the repository root"
    assert fake.lines(".docker") == ["info", "version --format {{.Server.Version}}"], (
        "the daemon is probed, and its version reported, before any stage"
    )
    assert "Environment:" in proc.stdout and "  bash    " in proc.stdout


@pytest.mark.parametrize("failing", ORDER)
def test_the_first_failure_stops_the_run_with_its_status(tmp_path: Path, failing: str) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(STUB_FAIL=failing, STUB_FAIL_STATUS="7")
    assert proc.returncode == 7
    position = ORDER.index(failing)
    assert fake.names() == ORDER[: position + 1], "no later stage starts"
    assert f"stage {position + 1}/5" in proc.stderr and "failed with exit status 7" in proc.stderr
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

    tools = _restricted_path(tmp_path, fake.bindir / "git")
    proc = fake.run(PATH=tools)
    assert proc.returncode == 1 and "docker was not found on PATH" in proc.stderr
    assert "./run_checks.sh" in proc.stderr, "the offline alternative is named"

    (tmp_path / "tools" / "git").unlink()
    proc = fake.run(PATH=tools)
    assert proc.returncode == 1 and "git was not found on PATH" in proc.stderr

    proc = fake.run(STUB_IMPORTS_FAIL="1")
    assert proc.returncode == 1 and "lacks the development requirements" in proc.stderr
    assert "pip install -r DB/requirements-dev.txt" in proc.stderr

    fake.python.unlink()
    proc = fake.run()
    assert proc.returncode == 1 and "python3 -m venv .venv" in proc.stderr

    assert fake.calls() == [], "no stage runs while a prerequisite is missing"


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
    # A checkout whose suite and interpreters only record how they are called.
    checkout, log = tmp_path / "checkout", tmp_path / "calls.log"
    _executable(checkout / "run_ci_suite.sh", f'#!/bin/sh\necho "suite $*" >> "{log}"\n')
    venv_python = tmp_path / "venv-python"
    _executable(venv_python, f'#!/bin/sh\necho "venv-python $*" >> "{log}"\n')
    _executable(tmp_path / "bin" / "python3.11",
                f'#!/bin/sh\necho "python3.11 $*" >> "{log}"\nmkdir -p .venv/bin && cp "{venv_python}" .venv/bin/python\n')
    environment = {key: value for key, value in os.environ.items() if not key.startswith("CI_")}
    environment.update(variables, CI_COMMIT_SHA=HEAD_SHA, PATH=f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    # As a shell executor runs them: Bash, stopping at the first failure.
    job = "set -eo pipefail\n" + "\n".join(commands) + "\n"
    proc = subprocess.run([BASH, "-c", job], cwd=checkout, env=environment, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "python3.11 -m venv .venv",
        "venv-python -m pip install -r DB/requirements-dev.txt",
        f"suite --base {base} --head {HEAD_SHA}",
    ]


def test_public_ci_is_only_an_adapter_around_the_suite() -> None:
    workflow = (ROOT / ".github" / "workflows" / "db-tests.yml").read_text(encoding="utf-8")
    code = [line.split("#", 1)[0] for line in workflow.splitlines()]
    steps = "\n".join(code)
    assert './run_ci_suite.sh --base "$BASE" --head "$HEAD"' in steps
    assert "pip install -r DB/requirements-dev.txt" in steps
    for forbidden in ("pytest", "docker", "psql", "postgres", "services:", "secrets.",
                      "run_checks.sh", "run_integration_tests", "run_migration_smoke", "run_task_1a_demo"):
        assert forbidden not in steps, f"{forbidden!r} belongs in a tracked script, not in the workflow"
    assert "contents: read" in steps
    assert "fetch-depth: 0" in steps
    assert "persist-credentials: false" in steps
    assert "timeout-minutes:" in steps
    # Inputs that would otherwise move are fixed: actions by commit, a runner
    # image release, exact Python and pip versions.
    uses = [line.split("uses:", 1)[1].strip() for line in code if "uses:" in line]
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action) for action in uses), uses
    assert "runs-on: ubuntu-latest" not in steps and re.search(r"runs-on: ubuntu-\d\d\.\d\d", steps)
    assert re.search(r'python-version: "3\.11\.\d+"', steps)
    assert re.search(r"pip install pip==\d+\.\d+(\.\d+)?\n", steps)


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
    # A checkout whose suite and interpreters only record how they are called.
    checkout, log = tmp_path / "checkout", tmp_path / "calls.log"
    _executable(checkout / "run_ci_suite.sh", f'#!/bin/sh\necho "suite $*" >> "{log}"\n')
    venv_python = tmp_path / "venv-python"
    _executable(venv_python, f'#!/bin/sh\necho "venv-python $*" >> "{log}"\n')
    _executable(tmp_path / "bin" / interpreter,
                f'#!/bin/sh\necho "{interpreter} $*" >> "{log}"\nmkdir -p .venv/bin && cp "{venv_python}" .venv/bin/python\n')
    environment = {key: value for key, value in os.environ.items() if key not in variables}
    environment.update(variables, PATH=f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    # As both runners execute run steps: Bash, stopping at the first failure.
    proc = subprocess.run([BASH, "-e", "-c", "\n".join(commands) + "\n"], cwd=checkout, env=environment,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == f"{interpreter} -m venv .venv"
    assert re.fullmatch(r"venv-python -m pip install pip==\d+\.\d+(\.\d+)?", calls[1])
    assert calls[2:] == ["venv-python -m pip install -r DB/requirements-dev.txt", f"suite --base {base} --head {head}"]


def test_the_forgejo_adapter_runs_on_a_host_runner_with_forgejo_hosted_actions() -> None:
    github, forgejo = (ADAPTERS[forge][0].read_text(encoding="utf-8") for forge in ("github", "forgejo"))
    code = "\n".join(line.split("#", 1)[0] for line in forgejo.splitlines())
    # The same thin adapter as on GitHub ...
    assert './run_ci_suite.sh --base "$BASE" --head "$HEAD"' in code
    assert "pip install -r DB/requirements-dev.txt" in code
    for forbidden in ("pytest", "docker", "psql", "postgres", "services:", "secrets.",
                      "run_checks.sh", "run_integration_tests", "run_migration_smoke", "run_task_1a_demo"):
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
    # Both forges run the same checkout code and the same pip.
    def pins(text: str) -> tuple:
        checkout = re.search(r"checkout@([0-9a-f]{40})", text)
        pip = re.search(r"pip install pip==(\S+)\n", text)
        assert checkout and pip, "each adapter pins the checkout commit and pip"
        return checkout.group(1), pip.group(1)

    assert pins(github) == pins(forgejo)
