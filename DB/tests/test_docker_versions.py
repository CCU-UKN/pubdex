"""The Docker version requirements of DB/lib/docker_versions.sh, offline.

run_ci_suite.sh, run_stack_acceptance.sh and run_operator_acceptance.sh
check the Engine and the Compose plugin with this library before they start
anything. These tests
source it in Bash and pin how it reads the versions Docker and Compose
report -- v-prefixed, with a distribution's build metadata, pre-releases,
later major versions and forms that are no version at all -- that the
Compose minimum is the one compose.yaml needs, that the Engine requirement
refuses every release without Docker's filter for ports published on
loopback, and that the documentation states the same requirements.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "DB" / "lib" / "docker_versions.sh"
BASH = shutil.which("bash") or "/bin/bash"


def _bash(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, "-c", f'set -euo pipefail; source "$0"; {script}', str(LIBRARY), *args],
                          capture_output=True, text=True, timeout=30)


def _status(version: str, minimum: str) -> int:
    proc = _bash('status=0; version_at_least "$1" "$2" || status=$?; echo "$status"', version, minimum)
    assert proc.returncode == 0, proc.stderr
    return int(proc.stdout)


def _setting(name: str) -> str:
    proc = _bash(f'echo "${name}"')
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.parametrize(
    "version, status",
    [
        # The first release that passes start_interval on to the container.
        ("2.24.0", 0),
        ("v2.24.0", 0),
        ("2.24.1", 0),
        ("2.39.1", 0),
        # Compose went from 2.x to 5.0.
        ("5.6.0", 0),
        ("v5.6.0", 0),
        ("10.0.0", 0),
        # A distribution's build metadata is not part of the version.
        ("2.24.6+ds1-0ubuntu1~22.04.1", 0),
        # Debian 13's docker-compose reports its package revision, which
        # ranks as a pre-release of 2.26.1: still later than 2.24.0.
        ("2.26.1-4", 0),
        # Ubuntu 24.04's docker-compose-v2, as released and in its updates.
        ("2.24.6", 0),
        ("2.40.3", 0),
        # A pre-release ranks just below its own release.
        ("2.24.1-rc.1", 0),
        ("2.24.6-desktop.1", 0),
        ("2.24.0-rc.1", 1),
        ("2.24.0-desktop.1", 1),
        # Accept start_interval but leave it out of the containers.
        ("2.23.3", 1),
        ("2.20.2", 1),
        # Reject the start_interval of compose.yaml.
        ("2.20.1", 1),
        ("2.20.0", 1),
        ("2.19.1", 1),
        ("1.99.99", 1),
        ("v1.29.2", 1),
        # Not a version that can be compared.
        ("", 2),
        ("dev", 2),
        ("v", 2),
        ("2.24", 2),
        ("2.24.0.1", 2),
        ("2.24.0rc1", 2),
        ("2.24.0-", 2),
        (" 2.24.0", 2),
    ],
)
def test_compose_versions_against_the_minimum(version: str, status: int) -> None:
    assert _status(version, _setting("COMPOSE_MINIMUM")) == status


def _engine_problem(version: str) -> str:
    proc = _bash('engine_problem "$1"', version)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


TOO_OLD = "is too old"
EXCLUDED = "is one of the releases in which a firewalld reload opens ports published on 127.0.0.1 to other hosts"


@pytest.mark.parametrize(
    "version, problem",
    [
        # Before 28.0.0 hosts on the same layer-2 network reach ports
        # published on loopback. The last releases of each line: the 27.x
        # line, moby's 25.0 branch, which Debian 13 takes fixes from, and
        # 25.0, the first Engine to run start_interval.
        ("27.5.1", TOO_OLD),
        ("v25.0.18", TOO_OLD),
        ("25.0.5", TOO_OLD),
        ("25.0.0", TOO_OLD),
        # Debian 13's docker.io, Ubuntu 24.04's as released, Debian 12's.
        ("26.1.5+dfsg1-9+deb13u1", TOO_OLD),
        ("24.0.7", TOO_OLD),
        ("20.10.24+dfsg1", TOO_OLD),
        # 28.0.0 has the filter, but its firewall rules needed the kernel's
        # ipset modules until 28.0.1.
        ("28.0.0", TOO_OLD),
        ("28.0.1-rc.1", TOO_OLD),
        # The first supported release, and GitHub's ubuntu-24.04 runner.
        ("28.0.1", ""),
        ("v28.0.1", ""),
        ("28.0.4", ""),
        ("28.1.1", ""),
        # A firewalld reload removes the filter (CVE-2025-54388).
        ("28.2.0-rc.1", ""),
        ("28.2.0", EXCLUDED),
        ("28.2.2", EXCLUDED),
        ("28.3.2", EXCLUDED),
        ("28.3.3-rc.1", EXCLUDED),
        # Fixed in 28.3.3; Debian's testing, Ubuntu 24.04's updates, current
        # Docker releases and a later major version.
        ("28.3.3", ""),
        ("28.5.2+dfsg4-5", ""),
        ("29.1.3", ""),
        ("29.9.2", ""),
        ("30.0.0", ""),
        # Not a version that can be compared.
        ("28.3", "Docker Engine reports the version '28.3', which is not a release number that can be compared"),
        ("", "Docker Engine does not report its version"),
    ],
)
def test_engine_versions_against_the_requirement(version: str, problem: str) -> None:
    found = _engine_problem(version)
    if problem in (TOO_OLD, EXCLUDED):
        assert found.startswith(f"Docker Engine {version} {problem}"), found
    else:
        assert found == problem


def test_the_minimums() -> None:
    assert _setting("COMPOSE_MINIMUM") == "2.24.0"
    assert _setting("ENGINE_MINIMUM") == "28.0.1", "the first release after 28.0.0, which filters loopback ports"
    assert (_setting("ENGINE_EXCLUDED_FROM"), _setting("ENGINE_EXCLUDED_UNTIL")) == ("28.2.0", "28.3.3"), \
        "CVE-2025-54388: affected from 28.2.0, fixed in 28.3.3"
    assert _setting("ENGINE_SUPPORTED") == "28.0.1 or later, except 28.2.0 to 28.3.2"
    assert "BUILD_MINIMUM" not in LIBRARY.read_text(encoding="utf-8"), "building needs no other Engine than running"


def test_the_compose_minimum_is_what_compose_yaml_needs() -> None:
    # While a health check sets start_interval, Compose has to be a release
    # that applies it: 2.24.0 or later.
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert re.search(r"^\s+start_interval:", compose, re.M), "compose.yaml no longer sets start_interval"
    assert _status(_setting("COMPOSE_MINIMUM"), "2.24.0") == 0


@pytest.mark.parametrize(
    "program, version, message",
    [
        ("Docker Compose", "2.23.3", "Docker Compose 2.23.3 is too old"),
        ("Docker Compose", "dev", "Docker Compose reports the version 'dev', which is not a release number "
                                  "that can be compared"),
        ("Docker Engine", "", "Docker Engine does not report its version"),
    ],
)
def test_the_problem_says_what_is_wrong(program: str, version: str, message: str) -> None:
    proc = _bash('version_problem "$1" "$2" "$COMPOSE_MINIMUM"', program, version)
    assert proc.returncode == 0 and proc.stdout.strip() == message


def test_no_problem_prints_nothing() -> None:
    proc = _bash('version_problem "Docker Compose" 5.6.0 "$COMPOSE_MINIMUM"')
    assert proc.returncode == 0 and proc.stdout == ""


# Where the requirement is stated for readers. The changelog is left out: it
# records what each release needed at the time.
DOCUMENTS = ["README.md", "DEPLOYMENT.md", "TASK_2A.md", "DB/TESTING_STRATEGY.md", "run_ci_suite.sh",
             "run_stack_acceptance.sh", "run_operator_acceptance.sh", "build_image.sh", "Dockerfile"]
STATED = re.compile(r"Compose(?: plugin)?,? (?:version )?(\d+\.\d+(?:\.\d+)?) or later|"
                    r"Compose(?: plugin)?[^.|]{0,40}?older than (\d+\.\d+(?:\.\d+)?)")


def test_the_documentation_states_the_same_compose_minimum() -> None:
    minimum = _setting("COMPOSE_MINIMUM")
    found = {}
    for relative in DOCUMENTS:
        text = " ".join((ROOT / relative).read_text(encoding="utf-8").split())
        for match in STATED.finditer(text):
            found.setdefault(match.group(1) or match.group(2), []).append(relative)
    assert minimum in found, "no document states the Compose minimum"
    assert set(found) == {minimum}, f"other Compose minimums stated: {found}"


# A statement of the Engine requirement, with whatever follows its minimum up
# to the end of the clause, where the exclusion has to follow; and the
# statements of the minimum alone, as "older than".
ENGINE_STATED = re.compile(r"Docker Engine,? (?:version )?(\d+(?:\.\d+)*),? (?:or later|or newer)"
                           r"((?:[^;:.|()]|\.(?=\d))*)")
ENGINE_OLDER = re.compile(r"Docker Engine[^.|]{0,40}?older than (\d+(?:\.\d+)*)")


def test_the_documentation_states_the_same_engine_requirement() -> None:
    # One Engine requirement for building the image and for running the
    # deployment, with the excluded releases wherever it is stated.
    minimum = _setting("ENGINE_MINIMUM")
    exclusion = _setting("ENGINE_SUPPORTED").removeprefix(minimum + " or later")
    assert exclusion == ", except 28.2.0 to 28.3.2"
    found, older = {}, {}
    for relative in DOCUMENTS:
        text = " ".join((ROOT / relative).read_text(encoding="utf-8").split())
        for match in ENGINE_STATED.finditer(text):
            stated = match.group(1) + (exclusion if match.group(2).startswith(exclusion) else "")
            found.setdefault(stated, []).append(relative)
        for match in ENGINE_OLDER.finditer(text):
            older.setdefault(match.group(1), []).append(relative)
    assert minimum + exclusion in found, "no document states the Engine requirement"
    assert set(found) == {minimum + exclusion}, f"other Engine requirements stated: {found}"
    assert set(older) <= {minimum}, f"other Engine minimums stated: {older}"


def test_the_acceptance_tests_report_the_binding_not_a_connection_from_elsewhere() -> None:
    # Both acceptance tests check the port bindings Docker reports. That is
    # configuration; neither connects from another host, and their output
    # says so rather than claiming that other hosts are refused.
    for name in ("run_stack_acceptance.sh", "run_operator_acceptance.sh"):
        lines = [line for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
                 if line.lstrip().startswith("ok ") and "127.0.0.1" in line and re.search(r"bind|publish", line)]
        assert lines, name
        for line in lines:
            assert "the configured binding; no connection from another host is tried" in line, (name, line)
