"""run_chart_checks.sh, stage 8 of run_ci_suite.sh, offline.

The real script runs from a scratch copy of the files it uses. helm is a stub
at the pinned path, and the copy of DB/lib/cluster_tools.sh pins the stub's
SHA-256. The stub records its arguments and environment, and reports a
version or fails a lint as a control file asks. .venv/bin/python is a stub
that records the pytest call. Both find their log by an absolute path
written into them, because the script clears their environment.

This pins, without the real helm: the checks of helm's hash and version
before anything runs; the lint of the chart with its own values and with
every values file and example, for both ends of the supported Kubernetes
range; the cleared environment of every helm and pytest call; that nothing is
written outside the temporary directory, which is removed after a pass, a
failure and an interrupt; and the exit statuses. The chart tests themselves
(charts/tests) run against the real helm in stage 8. Here their conftest is
only shown to fail, and never to skip, without that helm.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "run_chart_checks.sh"
PINS = ROOT / "DB" / "lib" / "cluster_tools.sh"
BASH = shutil.which("bash") or "/bin/bash"
HELM_VERSION = re.search(r"^readonly PINNED_HELM_VERSION=(\S+)$", PINS.read_text(encoding="utf-8"), re.M).group(1)
HELM_SHA256 = re.search(r"^readonly PINNED_HELM_MEMBER_SHA256=(\S+)$", PINS.read_text(encoding="utf-8"),
                        re.M).group(1)
HELM = Path(".tools") / f"helm-{HELM_VERSION}" / "helm"
KUBE = ["1.35.0", "1.37.0"]
VALUES_FILES = ["charts/tests/values/default.yaml", "charts/tests/values/evaluation.yaml",
                "charts/pubdex/examples/values-example.yaml"]

STUB_HELM = r'''#!@BASH@
log="@LOG@"
control="@CONTROL@"
{
  printf 'helm'
  for arg in "$@"; do printf '\037%s' "$arg"; done
  printf '\n'
} >> "$log/calls"
{ env; printf '\036\n'; } >> "$log/helm-env"
case "$1" in
  version) cat "$control/version" ;;
  lint)
    for arg in "$@"; do
      if [ -e "$control/lint-fails" ] && [ "$arg" = "$(cat "$control/lint-fails")" ]; then
        echo "[ERROR] values.yaml: a lint finding in $arg"
        exit 1
      fi
    done
    echo "1 chart(s) linted, 0 chart(s) failed" ;;
  *) exit 3 ;;
esac
'''

STUB_PYTHON = r'''#!@BASH@
log="@LOG@"
control="@CONTROL@"
if [ "$1" = -c ]; then exit "$(cat "$control/imports" 2>/dev/null || echo 0)"; fi
{
  printf 'python'
  for arg in "$@"; do printf '\037%s' "$arg"; done
  printf '\n'
} >> "$log/calls"
{ env; printf '\036\n'; } >> "$log/python-env"
printf '%s\n' "$PWD" >> "$log/python-cwd"
if [ -e "$control/slow" ]; then
  while :; do sleep 0.1; done
fi
exit "$(cat "$control/pytest-status" 2>/dev/null || echo 0)"
'''

# What env -i keeps for helm, and what the shell adds on its own.
HELM_ENV = {"PATH", "LC_ALL", "HOME", "TMPDIR", "KUBECONFIG", "DOCKER_CONFIG", "HELM_CONFIG_HOME",
            "HELM_CACHE_HOME", "HELM_DATA_HOME"}
PYTEST_ENV = {"PATH", "HOME", "TMPDIR", "LANG", "PYTHONDONTWRITEBYTECODE", "PEOPLE_PUBS_SKIP_DOTENV"}
SHELL_VARIABLES = {"PWD", "OLDPWD", "SHLVL", "_"}
POLLUTION = {
    "KUBECONFIG": "/nonexistent/kubeconfig",
    "HELM_CONFIG_HOME": "/nonexistent/helm-config",
    "HELM_CACHE_HOME": "/nonexistent/helm-cache",
    "HELM_DATA_HOME": "/nonexistent/helm-data",
    "HELM_KUBECONTEXT": "elsewhere",
    "HELM_DRIVER": "sql",
    "DOCKER_CONFIG": "/nonexistent/docker",
    "PYTEST_ADDOPTS": "-k nothing_at_all",
    "PYTHONPATH": "/nonexistent",
    "PEOPLE_PUBS_INTEGRATION_DSN": "postgresql://db.invalid/people_db",
}


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class Fake:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "checkout"
        self.log = tmp_path / "log"
        self.control = tmp_path / "control"
        self.tmpdir = tmp_path / "tmpdir"
        for directory in (self.log, self.control, self.tmpdir):
            directory.mkdir()
        self.script = self.root / SCRIPT.name
        self.script.parent.mkdir(parents=True)
        shutil.copy2(SCRIPT, self.script)
        self.helm = self.root / HELM
        _executable(self.helm, self._stub(STUB_HELM))
        digest = hashlib.sha256(self.helm.read_bytes()).hexdigest()
        pins = self.root / "DB" / "lib" / "cluster_tools.sh"
        pins.parent.mkdir(parents=True)
        pins.write_text(PINS.read_text(encoding="utf-8").replace(HELM_SHA256, digest), encoding="utf-8")
        self.python = self.root / ".venv" / "bin" / "python"
        _executable(self.python, self._stub(STUB_PYTHON))
        for relative in VALUES_FILES:
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.root / relative).write_text("mode: evaluation\n", encoding="utf-8")
        (self.control / "version").write_text(f"v{HELM_VERSION}", encoding="utf-8")
        self.env = dict(os.environ, TMPDIR=str(self.tmpdir))
        self.env.pop("KUBECONFIG", None)

    def _stub(self, text: str) -> str:
        return text.replace("@BASH@", BASH).replace("@LOG@", str(self.log)).replace("@CONTROL@", str(self.control))

    def set(self, name: str, value: str) -> None:
        (self.control / name).write_text(value, encoding="utf-8")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(self.script), *args], cwd=self.tmpdir.parent, env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def calls(self) -> list:
        path = self.log / "calls"
        if not path.exists():
            return []
        return [line.split("\x1f") for line in path.read_text(encoding="utf-8").splitlines()]

    def environments(self, program: str) -> list:
        path = self.log / f"{program}-env"
        if not path.exists():
            return []
        blocks = path.read_text(encoding="utf-8").split("\x1e\n")
        return [dict(line.split("=", 1) for line in block.splitlines() if "=" in line) for block in blocks if block]

    def checkout_state(self) -> dict:
        return {str(path.relative_to(self.root)): path.read_bytes() for path in sorted(self.root.rglob("*"))
                if path.is_file()}


def _lint_calls(files: list) -> list:
    expected = []
    for kube in KUBE:
        for values in [None, *files]:
            call = ["helm", "lint", "--strict", "charts/pubdex", "--kube-version", kube]
            expected.append(call + (["--values", values] if values else []))
    return expected


def test_help_and_usage_errors_run_nothing(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run("--help")
    assert proc.returncode == 0 and proc.stdout.startswith("usage: run_chart_checks.sh")
    for args in (["--bogus"], ["--help", "extra"], ["charts/pubdex"]):
        proc = fake.run(*args)
        assert proc.returncode == 2 and "unexpected argument" in proc.stderr, args
    assert fake.calls() == []


def test_a_passing_run_lints_every_values_file_for_both_ends_then_runs_the_chart_tests(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    before = fake.checkout_state()
    proc = fake.run()
    assert proc.returncode == 0, proc.stderr
    calls = fake.calls()
    assert calls[0] == ["helm", "version", "--template", "{{.Version}}"], "the version is checked first"
    # The chart's own values, then the values files of the checks and the
    # examples, in that order, for the oldest and the newest release.
    assert calls[1:-1] == _lint_calls(VALUES_FILES)
    assert calls[-1] == ["python", "-m", "pytest", "-p", "no:cacheprovider", "charts/tests"]
    (cwd,) = (fake.log / "python-cwd").read_text(encoding="utf-8").splitlines()
    assert Path(cwd).resolve() == fake.root.resolve(), "the tests run from the repository root"
    assert f"helm v{HELM_VERSION}" in proc.stdout and "SHA-256 matches the pin" in proc.stdout
    assert "PASS: the Helm chart's offline checks passed." in proc.stdout
    assert fake.checkout_state() == before, "nothing is written into the checkout"
    assert list(fake.tmpdir.iterdir()) == [], "the temporary directory is removed"


def test_without_examples_the_chart_and_the_checks_values_are_linted(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    shutil.rmtree(fake.root / "charts" / "pubdex" / "examples")
    proc = fake.run()
    assert proc.returncode == 0, proc.stderr
    assert fake.calls()[1:-1] == _lint_calls(VALUES_FILES[:2])


def test_helm_and_the_tests_run_with_cleared_environments_in_one_temporary_directory(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(**POLLUTION)
    assert proc.returncode == 0, proc.stderr
    helm_envs = fake.environments("helm")
    assert len(helm_envs) == 1 + 2 * (1 + len(VALUES_FILES))
    work = None
    for env in helm_envs:
        assert set(env) - SHELL_VARIABLES <= HELM_ENV, sorted(set(env) - SHELL_VARIABLES - HELM_ENV)
        assert set(env) >= HELM_ENV
        home = Path(env["HOME"]).parent
        work = work or home
        assert home == work, "one temporary directory for every call"
        assert work.parent == fake.tmpdir, "under the caller's TMPDIR"
        for name in ("TMPDIR", "KUBECONFIG", "DOCKER_CONFIG", "HELM_CONFIG_HOME", "HELM_CACHE_HOME", "HELM_DATA_HOME"):
            assert Path(env[name]).is_relative_to(work), name
        assert env["LC_ALL"] == "C"
    (pytest_env,) = fake.environments("python")
    assert set(pytest_env) - SHELL_VARIABLES <= PYTEST_ENV, sorted(set(pytest_env) - SHELL_VARIABLES - PYTEST_ENV)
    assert Path(pytest_env["HOME"]).parent == work and Path(pytest_env["TMPDIR"]).parent == work
    assert pytest_env["PYTHONDONTWRITEBYTECODE"] == "1" and pytest_env["PEOPLE_PUBS_SKIP_DOTENV"] == "1"
    assert not work.exists(), "and it is removed afterwards"
    assert not os.path.exists(helm_envs[0]["KUBECONFIG"]), "no kubeconfig was there to read"


def test_a_missing_interpreter_or_requirements_stop_the_run(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.set("imports", "1")
    proc = fake.run()
    assert proc.returncode == 1 and "lacks the development requirements (pytest, PyYAML)" in proc.stderr
    assert "./setup_venv.sh" in proc.stderr
    fake.python.unlink()
    proc = fake.run()
    assert proc.returncode == 1 and "no interpreter at .venv/bin/python" in proc.stderr
    assert fake.calls() == [] and list(fake.tmpdir.iterdir()) == []


def test_helm_is_checked_against_its_pin_before_it_runs(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.set("version", "v3.22.1")
    proc = fake.run()
    assert proc.returncode == 1 and f"reports the version 'v3.22.1', not the pinned v{HELM_VERSION}" in proc.stderr
    assert [call[1] for call in fake.calls()] == ["version"], "nothing is linted"
    assert list(fake.tmpdir.iterdir()) == []

    fake.helm.write_text(fake.helm.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    calls = len(fake.calls())
    proc = fake.run()
    assert proc.returncode == 1 and "not the pinned" in proc.stderr and "SHA-256" in proc.stderr
    assert "./setup_cluster_tools.sh" in proc.stderr
    assert len(fake.calls()) == calls, "a helm of another hash is not run at all"

    fake.helm.unlink()
    proc = fake.run()
    assert proc.returncode == 1 and f"the pinned helm {HELM_VERSION} is not installed at {HELM}" in proc.stderr
    assert "./setup_cluster_tools.sh" in proc.stderr
    assert len(fake.calls()) == calls and list(fake.tmpdir.iterdir()) == []


def test_a_failing_lint_stops_the_run_before_the_tests(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.set("lint-fails", "charts/tests/values/evaluation.yaml")
    proc = fake.run()
    assert proc.returncode == 1
    assert "a lint finding in charts/tests/values/evaluation.yaml" in proc.stderr, "helm's output is shown"
    assert "helm lint --strict failed for Kubernetes 1.35.0 with charts/tests/values/evaluation.yaml" in proc.stderr
    assert fake.calls()[-1][:2] == ["helm", "lint"] and not (fake.log / "python-env").exists()
    assert list(fake.tmpdir.iterdir()) == []


def test_failing_chart_tests_fail_the_run(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.set("pytest-status", "1")
    proc = fake.run()
    assert proc.returncode == 1 and "the chart tests failed (pytest exit status 1)" in proc.stderr
    assert "PASS" not in proc.stdout and list(fake.tmpdir.iterdir()) == []


def test_an_interrupt_removes_the_temporary_directory(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.set("slow", "")
    proc = subprocess.Popen([BASH, str(fake.script)], cwd=tmp_path, env=fake.env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    deadline = time.monotonic() + 30
    while not (fake.log / "python-env").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert list(fake.tmpdir.iterdir()), "the tests run inside the temporary directory"
    os.killpg(proc.pid, signal.SIGINT)  # what Ctrl-C does: the whole foreground group
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    assert list(fake.tmpdir.iterdir()) == []


def test_the_kubernetes_range_is_the_one_the_chart_supports() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^KUBE_VERSIONS=\((\S+) (\S+)\)$", script, re.M).groups() == tuple(KUBE)
    chart = (ROOT / "charts" / "pubdex" / "Chart.yaml").read_text(encoding="utf-8")
    assert re.search(r'^kubeVersion: ">=(\d+\.\d+\.\d+)-0"$', chart, re.M).group(1) == KUBE[0]
    node = re.search(r"^readonly PINNED_KIND_NODE_IMAGE=kindest/node:v(\d+\.\d+\.\d+)@", PINS.read_text(
        encoding="utf-8"), re.M).group(1)
    assert node == KUBE[1], "the newest is the release stage 9 installs the chart on"
    helpers = (ROOT / "charts" / "tests" / "chart.py").read_text(encoding="utf-8")
    assert re.search(r'^KUBE_OLDEST = "(\S+)"$', helpers, re.M).group(1) == KUBE[0]
    assert re.search(r'^KUBE_NEWEST = "(\S+)"$', helpers, re.M).group(1) == KUBE[1]


def test_stage_one_does_not_collect_the_chart_tests() -> None:
    # Stage 1 collects DB/tests only, and its skip policy would refuse a test
    # that skips without helm; the chart tests are a run of their own.
    assert re.search(r"^testpaths = tests$", (ROOT / "DB" / "pytest.ini").read_text(encoding="utf-8"), re.M)
    assert (ROOT / "charts" / "tests" / "pytest.ini").is_file()
    assert not (ROOT / "charts" / "tests" / "__init__.py").exists()
    assert os.access(SCRIPT, os.X_OK)


def _chart_tests_checkout(tmp_path: Path) -> Path:
    """The chart tests, the chart and what their conftest loads, without
    .tools/."""
    root = tmp_path / "checkout"
    shutil.copytree(ROOT / "charts", root / "charts", ignore=shutil.ignore_patterns("__pycache__"))
    for relative in ("DB/lib/cluster_tools.sh", "DB/tests/__init__.py", "DB/tests/conftest.py",
                     "DB/tests/network_guard.py", "DB/tests/expected_skips.py",
                     "DB/tests/subprocess_guard/sitecustomize.py"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, root / relative)
    return root


def _run_chart_tests(root: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PEOPLE_PUBS_SKIP_DOTENV": "1"}
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "charts/tests"], cwd=root,
                          env=env, capture_output=True, text=True, timeout=120)


def test_the_chart_tests_fail_and_never_skip_without_the_pinned_helm(tmp_path: Path) -> None:
    # The same interpreter as these tests, with PyYAML from the development
    # lock; only the pinned helm is missing, then of another hash.
    root = _chart_tests_checkout(tmp_path)
    proc = _run_chart_tests(root, tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, output
    assert f"the pinned helm {HELM_VERSION} is not installed at {HELM}" in output
    assert "./setup_cluster_tools.sh" in output
    assert "skipped" not in output and "passed" not in output

    _executable(root / HELM, "#!/bin/sh\nexit 0\n")
    proc = _run_chart_tests(root, tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1 and f"not the pinned {HELM_SHA256}" in output, output
    assert "skipped" not in output and "passed" not in output
