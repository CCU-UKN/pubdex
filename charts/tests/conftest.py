"""Set-up of the Helm chart's checks, stage 8 of run_ci_suite.sh.

run_chart_checks.sh runs these tests once it has linted the chart. They also
run on their own from the repository root:

    .venv/bin/python -m pytest charts/tests

They live here, not under DB/tests, because they need the pinned helm, which
the offline suite of stage 1 does not have to have. Without that helm they
fail; they never skip.

- helm is the one ./setup_cluster_tools.sh installs, at the path, of the
  version and with the SHA-256 that DB/lib/cluster_tools.sh pins. When it is
  missing or differs, the run stops with a failure before the first test.
- Every helm call runs with a cleared environment. HOME, TMPDIR, helm's
  configuration, cache and data directories and DOCKER_CONFIG lie in a
  temporary directory of the run, and KUBECONFIG names a file that does not
  exist. The tests run only helm template and helm install --dry-run=client,
  which render on the client, so no kubeconfig is read and no cluster is
  contacted.
- DB/tests/conftest.py is loaded as a plugin, so the rules of stage 1 apply
  here as well. Its network guard refuses network access by these tests and
  by the Python processes they start; it does not reach helm, which is not
  Python. A skip that DB/tests/expected_skips.py does not name fails the run,
  and that file names none of these tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Tuple

import pytest

from chart import CHART, KUBE_NEWEST, NAMESPACE, RELEASE, ROOT, Rendered, parse

sys.path.insert(0, str(ROOT / "DB"))
pytest_plugins = ("tests.conftest",)

PINS = ROOT / "DB" / "lib" / "cluster_tools.sh"


def _pins() -> Tuple[Path, str, str]:
    """The pinned helm: its path in this checkout, version and SHA-256."""
    script = 'source "$1" && cluster_tool_path "$2" helm && echo "$PINNED_HELM_VERSION" "$PINNED_HELM_MEMBER_SHA256"'
    proc = subprocess.run(["bash", "-c", script, "pins", str(PINS), str(ROOT)], capture_output=True, text=True,
                          timeout=30, env={"PATH": os.environ.get("PATH", os.defpath)})
    lines = proc.stdout.split("\n")
    if proc.returncode != 0 or len(lines) < 2 or len(lines[1].split()) != 2:
        raise RuntimeError(f"cannot read the pinned helm from {PINS.relative_to(ROOT)}: {proc.stderr.strip()}")
    version, sha256 = lines[1].split()
    return Path(lines[0]), version, sha256


def _helm_problem(work: Path) -> str:
    """Why the pinned helm cannot be used, or an empty string."""
    path, version, sha256 = _pins()
    where = path.relative_to(ROOT)
    if not (path.is_file() and os.access(path, os.X_OK)):
        return f"the pinned helm {version} is not installed at {where}; install it with ./setup_cluster_tools.sh"
    have = hashlib.sha256(path.read_bytes()).hexdigest()
    if have != sha256:
        return (f"{where} has the SHA-256 {have}, not the pinned {sha256}; remove its directory and install it "
                f"again with ./setup_cluster_tools.sh")
    reported = Helm(path, work).run("version", "--template", "{{.Version}}").stdout.strip()
    if reported != f"v{version}":
        return f"{where} reports the version {reported!r}, not the pinned v{version}"
    return ""


class Helm:
    """The pinned helm, run with a cleared environment of its own."""

    def __init__(self, path: Path, work: Path) -> None:
        self.path = path
        for directory in ("home", "tmp", "docker", "helm/config", "helm/cache", "helm/data"):
            (work / directory).mkdir(parents=True, exist_ok=True)
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LC_ALL": "C",
            "HOME": str(work / "home"),
            "TMPDIR": str(work / "tmp"),
            "KUBECONFIG": str(work / "no-kubeconfig"),
            "DOCKER_CONFIG": str(work / "docker"),
            "HELM_CONFIG_HOME": str(work / "helm/config"),
            "HELM_CACHE_HOME": str(work / "helm/cache"),
            "HELM_DATA_HOME": str(work / "helm/data"),
        }
        self._rendered: Dict[tuple, Rendered] = {}

    def run(self, *args: object) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.path), *map(str, args)], cwd=ROOT, env=self.env, capture_output=True,
                              text=True, timeout=120)

    def template(self, *args: object, release: str = RELEASE, namespace: str = NAMESPACE,
                 kube_version: str = KUBE_NEWEST) -> Rendered:
        """The chart rendered with these helm arguments; a failure fails the
        test with helm's message."""
        key = (tuple(map(str, args)), release, namespace, kube_version)
        if key not in self._rendered:
            proc = self.run("template", release, CHART, "--namespace", namespace, "--kube-version", kube_version,
                            *args)
            if proc.returncode != 0:
                pytest.fail(f"helm template {' '.join(key[0])} failed\n{proc.stderr}", pytrace=False)
            self._rendered[key] = parse(proc.stdout)
        return self._rendered[key]

    def refusal(self, *args: object, release: str = RELEASE, kube_version: str = KUBE_NEWEST) -> str:
        """helm's message for arguments the chart has to refuse; rendering
        them fails the test."""
        proc = self.run("template", release, CHART, "--namespace", NAMESPACE, "--kube-version", kube_version,
                        *args)
        if proc.returncode == 0:
            pytest.fail(f"helm template {' '.join(map(str, args))} rendered, but has to be refused", pytrace=False)
        return proc.stderr

    def notes(self, *args: object, release: str = RELEASE, namespace: str = NAMESPACE) -> str:
        """NOTES.txt as helm install shows it, from a client-side dry run."""
        proc = self.run("install", release, CHART, "--namespace", namespace, "--dry-run=client", "--output", "json",
                        *args)
        if proc.returncode != 0:
            pytest.fail(f"helm install --dry-run=client failed\n{proc.stderr}", pytrace=False)
        return json.loads(proc.stdout)["info"]["notes"]


@pytest.fixture(scope="session")
def helm(tmp_path_factory: pytest.TempPathFactory) -> Helm:
    path, _version, _sha256 = _pins()
    return Helm(path, tmp_path_factory.mktemp("helm"))


def pytest_sessionstart(session: pytest.Session) -> None:
    with tempfile.TemporaryDirectory(prefix="pubdex-helm-check-") as probe:
        problem = _helm_problem(Path(probe))
    if problem:
        pytest.exit(f"the chart checks need the pinned helm: {problem}", returncode=pytest.ExitCode.TESTS_FAILED)
