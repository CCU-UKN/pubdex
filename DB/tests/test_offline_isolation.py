"""The offline suite cannot be pointed at a database or narrowed from outside.

DB/run_tests.sh -- step 3 of run_checks.sh, and so part of stage 1 of
run_ci_suite.sh -- starts pytest in a cleared environment with
PEOPLE_PUBS_SKIP_DOTENV=1. These tests run the real run_tests.sh,
run_checks.sh and run_ci_suite.sh from a scratch copy of the checkout in which
.venv/bin/python is a recorder: every call records its arguments, working
directory and environment, and succeeds. Started from an environment full of
database settings, test DSNs, pytest options and an attempt to switch the
DB/.env opt-out off, the pytest call sees none of them. Stages 2-5 of the
complete suite, Docker and Git are stubs here.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash") or "/bin/bash"
# What a shell adds on its own when it starts a program.
SHELL_VARIABLES = {"PWD", "OLDPWD", "SHLVL", "_"}
KEPT = {"PATH", "HOME", "TMPDIR", "LANG", "PYTHONPATH", "PEOPLE_PUBS_SKIP_DOTENV"}
ELSEWHERE = "postgresql://db.invalid/people_db"
# What a caller may have exported. None of it may reach the tests.
POLLUTION = {
    "PEOPLE_PUBS_INTEGRATION_DSN": ELSEWHERE,
    "PEOPLE_DB_TEST_DSN": ELSEWHERE,
    "PEOPLE_DB_DSN": ELSEWHERE,
    "PGHOST": "db.invalid",
    "PGHOSTADDR": "db.invalid",
    "PGPORT": "1",
    "PGUSER": "someone",
    "PGPASSWORD": "placeholder-not-a-real-one",
    "PGDATABASE": "elsewhere",
    "PGSERVICE": "production",
    "PGSERVICEFILE": "/nonexistent",
    "PGPASSFILE": "/nonexistent",
    "PGSSLMODE": "require",
    "POSTGRES_PASSWORD": "placeholder-not-a-real-one",
    "POSTGRES_USER": "someone",
    "POSTGRES_DB": "elsewhere",
    "PYTEST_ADDOPTS": "-k nothing_at_all",
    "PYTEST_PLUGINS": "unwanted_plugin",
    "PYTHONPATH": "/nonexistent",
    "PEOPLE_PUBS_SKIP_DOTENV": "0",
    "PEOPLE_PUBS_ORG_PATTERNS": "Example University",
}

RECORDER = r'''
import json, os, sys
from pathlib import Path
with open(Path(__file__).resolve().parent / "calls.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}) + "\n")
'''


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def checkout(tmp_path: Path) -> Path:
    """The real entry points, a recording interpreter, and stub stages 2-5."""
    root = tmp_path / "checkout"
    for relative in ("run_checks.sh", "run_ci_suite.sh", "DB/run_tests.sh"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, root / relative)
    for relative in ("DB/run_migration_smoke.sh", "DB/run_task_1a_demo.sh", "DB/run_disposable_integration.sh"):
        _executable(root / relative, "#!/bin/sh\nexit 0\n")
    bindir = root / ".venv" / "bin"
    _executable(bindir / "recorder.py", RECORDER)
    _executable(bindir / "python", f'#!/bin/sh\nexec "{sys.executable}" "{bindir / "recorder.py"}" "$@"\n')
    _executable(tmp_path / "tools" / "docker", "#!/bin/sh\nexit 0\n")
    _executable(tmp_path / "tools" / "git", "#!/bin/sh\necho true\n")
    return root


def polluted(tmp_path: Path) -> dict:
    tools = tmp_path / "tools"
    return dict(os.environ, **POLLUTION, PATH=f"{tools}{os.pathsep}{os.environ['PATH']}")


def run(command: list, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True, timeout=120)


def calls(root: Path) -> list:
    log = root / ".venv" / "bin" / "calls.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def pytest_calls(root: Path) -> list:
    return [call for call in calls(root) if call["argv"][:2] == ["-m", "pytest"]]


def assert_isolated(env: dict) -> None:
    leaked = set(env) - SHELL_VARIABLES - KEPT
    # Names only, never values, and only the ones this test planted by name.
    planted = sorted(leaked & set(POLLUTION))
    assert not leaked, f"inherited by the tests: {planted} and {len(leaked) - len(planted)} other variable(s)"
    assert env["PEOPLE_PUBS_SKIP_DOTENV"] == "1", "people_pubs does not read DB/.env"
    assert env["PYTHONPATH"] == ".", "only the package itself is importable from outside"
    assert "PATH" in env and "LANG" in env


def test_run_tests_starts_pytest_in_a_cleared_environment(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    proc = run([BASH, str(root / "DB" / "run_tests.sh"), "-k", "focused"], polluted(tmp_path), tmp_path)
    assert proc.returncode == 0, proc.stderr
    (call,) = pytest_calls(root)
    assert call["argv"] == ["-m", "pytest", "-k", "focused"], "explicit arguments still reach pytest"
    assert Path(call["cwd"]).resolve() == (root / "DB").resolve()
    assert_isolated(call["env"])


def test_run_checks_keeps_its_tests_offline_and_unnarrowed(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    proc = run([BASH, str(root / "run_checks.sh")], polluted(tmp_path), tmp_path)
    assert proc.returncode == 0, proc.stderr
    (call,) = pytest_calls(root)
    assert call["argv"] == ["-m", "pytest"], "nothing narrows the offline suite"
    assert_isolated(call["env"])


def test_stage_one_of_the_complete_suite_is_isolated(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    proc = run([BASH, str(root / "run_ci_suite.sh"), "--base", "abc123", "--head", "def456"],
               polluted(tmp_path), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "PASS: all 5 stages" in proc.stdout
    (call,) = pytest_calls(root)
    assert call["argv"] == ["-m", "pytest"]
    assert_isolated(call["env"])
    guard = [call["argv"] for call in calls(root) if call["argv"][:1] == ["DB/check_transform_version.py"]]
    assert guard == [["DB/check_transform_version.py", "--base", "abc123", "--head", "def456"]]


def test_an_all_zero_base_leaves_the_transform_guard_nothing_to_check(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    proc = run([BASH, str(root / "run_checks.sh"), "--base", "0" * 40, "--head", "def456"],
               polluted(tmp_path), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "No base commit; there is no range to check." in proc.stdout
    assert not any(call["argv"][:1] == ["DB/check_transform_version.py"] for call in calls(root))


def test_nothing_is_invented_for_a_minimal_caller(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    proc = run([BASH, str(root / "DB" / "run_tests.sh")], {"PATH": os.environ["PATH"]}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    (call,) = pytest_calls(root)
    assert "HOME" not in call["env"] and "TMPDIR" not in call["env"]
    assert call["env"]["LANG"] == "C.UTF-8"
    assert_isolated(call["env"])
