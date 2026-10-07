"""The policy for expected skips (tests/expected_skips.py), offline.

Each rule is checked on the tests it names and on the condition it states,
with unrelated tests and false conditions as counterparts. Whole test runs,
started from a temporary directory with the same tests/conftest.py loaded as
a plugin, then show that a skip the policy does not name -- also a module
skipped while it is collected, next to a module whose test passes -- fails an
otherwise green run, that pytest's own failure statuses stay as they are, and
that the expected skips keep a run green.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests import expected_skips
from tests.expected_skips import (
    AFFILIATION_GUARDRAIL,
    DISCOVERY_GUARDRAILS,
    EXTERNAL_TIMEOUT_TESTS,
    NO_DATABASE_REASON,
    UNREADABLE_FILE_TEST,
    Skip,
)

DB_DIR = Path(__file__).resolve().parents[1]
EMPTY = "got empty parameter set for (job)"
REAL_WHICH = shutil.which


def rule_for(nodeid: str, reason: str, *, integration: bool = False, database: bool = False):
    rule = expected_skips.classify(Skip(nodeid, reason, integration, database))
    return rule.name if rule else None


@pytest.fixture
def as_root(monkeypatch: pytest.MonkeyPatch):
    def become(root: bool) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 0 if root else 1000, raising=False)
    return become


@pytest.fixture
def timeout_available(monkeypatch: pytest.MonkeyPatch):
    def provide(available: bool) -> None:
        def which(name, *args, **kwargs):
            if name == "timeout":
                return "/usr/bin/timeout" if available else None
            return REAL_WHICH(name, *args, **kwargs)
        monkeypatch.setattr(shutil, "which", which)
    return provide


# --------------------------------------------------------------------------
# The rules, one by one
# --------------------------------------------------------------------------
def test_integration_tests_may_skip_only_without_a_database() -> None:
    nodeid = "tests/integration/test_x_integration.py::test_y"
    expected = "integration test without a disposable database"
    assert rule_for(nodeid, NO_DATABASE_REASON, integration=True) == expected
    assert rule_for(nodeid + "[case]", NO_DATABASE_REASON, integration=True) == expected
    assert rule_for(nodeid, NO_DATABASE_REASON, integration=True, database=True) is None
    assert rule_for(nodeid, NO_DATABASE_REASON, integration=False) is None, "the marker is required"
    assert rule_for(nodeid, "temporarily disabled", integration=True) is None, "and the reason"
    assert rule_for("tests/test_x.py::test_y", NO_DATABASE_REASON, integration=True) is None, (
        "and the integration directory"
    )


@pytest.mark.parametrize("function", DISCOVERY_GUARDRAILS)
def test_the_empty_parameter_rule_names_the_three_guardrails(function: str) -> None:
    assert rule_for(f"{function}[NOTSET]", EMPTY) == "no discovery job in refresh_jobs.json"


def test_the_guardrail_rules_reach_no_other_test() -> None:
    module = DISCOVERY_GUARDRAILS[0].split("::")[0]
    assert rule_for(f"{module}::test_config_parses", EMPTY) is None, "another test of the same module"
    assert rule_for(f"{module}::test_backfill_job_is_bounded[NOTSET]", EMPTY) is None
    assert rule_for("tests/test_other.py::test_discovery_job_caps_new_records[NOTSET]", EMPTY) is None
    assert rule_for(f"{AFFILIATION_GUARDRAIL}[job]", "not an affiliation job") == (
        "discovery job without an affiliation search"
    )
    assert rule_for(f"{DISCOVERY_GUARDRAILS[0]}[job]", "not an affiliation job") is None
    assert rule_for(f"{AFFILIATION_GUARDRAIL}[job]", "temporarily disabled") is None


def test_the_root_rule_needs_root_and_the_unreadable_file_case(as_root) -> None:
    as_root(True)
    assert rule_for(UNREADABLE_FILE_TEST, "root can read any file") == "running as root"
    assert rule_for("tests/test_secrets_and_local_config.py::test_clean_files_exit_zero",
                    "root can read any file") is None, "an unrelated test"
    assert rule_for("tests/test_other.py::test_unreadable_file_is_an_error_not_a_pass",
                    "root can read any file") is None, "the same name in another module"
    as_root(False)
    assert rule_for(UNREADABLE_FILE_TEST, "root can read any file") is None, "a false condition"


def test_the_timeout_rule_needs_a_missing_timeout_and_the_timeout_cases(timeout_available) -> None:
    timeout_available(False)
    for nodeid in EXTERNAL_TIMEOUT_TESTS:
        assert rule_for(nodeid, "needs the timeout utility") == "no timeout utility"
    assert rule_for("tests/test_ci_suite_script.py::test_help_lists_every_stage_and_runs_nothing",
                    "needs the timeout utility") is None, "an unrelated test"
    timeout_available(True)
    for nodeid in EXTERNAL_TIMEOUT_TESTS:
        assert rule_for(nodeid, "needs the timeout utility") is None, "a false condition"


def test_no_collection_time_skip_is_expected() -> None:
    assert expected_skips.COLLECTION_RULES == ()
    skip = Skip("tests/integration/test_x_integration.py", NO_DATABASE_REASON, True, False)
    assert expected_skips.classify_collection(skip) is None


def test_every_named_test_exists() -> None:
    # A renamed test would otherwise leave a rule that can no longer apply.
    named = (*DISCOVERY_GUARDRAILS, UNREADABLE_FILE_TEST, *EXTERNAL_TIMEOUT_TESTS)
    for nodeid in named:
        path, function = nodeid.split("::")
        source = (DB_DIR / path).read_text(encoding="utf-8")
        assert re.search(rf"^def {function}\(", source, re.MULTILINE), nodeid
    assert (DB_DIR / expected_skips.INTEGRATION_DIRECTORY).is_dir()


def test_this_run_applies_the_policy(request: pytest.FixtureRequest) -> None:
    plugin = request.config.pluginmanager.get_plugin("expected-skips")
    assert isinstance(plugin, expected_skips.SkipPolicy)
    assert plugin.database == expected_skips.database_configured()


# --------------------------------------------------------------------------
# Whole runs
# --------------------------------------------------------------------------
PASSING = "def test_passes():\n    pass\n"

COLLECTION_SKIPS = {
    "module-level-skip": (
        'import pytest\n\npytest.skip("temporarily disabled", allow_module_level=True)\n',
        "temporarily disabled",
    ),
    "importorskip": (
        'import pytest\n\npytest.importorskip("module_that_does_not_exist_anywhere")\n',
        "could not import 'module_that_does_not_exist_anywhere'",
    ),
    "unittest-skip": (
        'import unittest\n\nraise unittest.SkipTest("temporarily disabled")\n',
        "unittest.case.SkipTest: temporarily disabled",
    ),
}
HIDDEN = "\n\ndef test_hidden():\n    pass\n"

INTEGRATION_SKIP = f'''
import pytest

pytestmark = pytest.mark.integration


def test_needs_a_database():
    pytest.skip({NO_DATABASE_REASON!r})
'''

TIMEOUT_SKIP = '''
import shutil

import pytest


@pytest.mark.skipif(shutil.which("timeout") is None, reason="needs the timeout utility")
def test_an_external_timeout_stops_the_running_stage_and_the_suite():
    pass
'''

TIMEOUT_SKIP_WITHOUT_CAUSE = '''
import pytest


@pytest.mark.skipif(True, reason="needs the timeout utility")
def test_an_external_timeout_stops_the_running_stage_and_the_suite():
    pass


@pytest.mark.skipif(True, reason="needs the timeout utility")
def test_unrelated_with_the_same_reason():
    pass
'''


def write(root: Path, files: dict) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")


def run(root: Path, *, policy: bool = True, path: str | None = None, **environment: str):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(DB_DIR), os.environ["PYTHONPATH"]]))
    for name in ("PEOPLE_PUBS_INTEGRATION_DSN", "PEOPLE_DB_TEST_DSN"):
        env.pop(name, None)
    env.update(environment)
    if path is not None:
        env["PATH"] = path
    plugin = ["-p", "tests.conftest"] if policy else []
    return subprocess.run(
        [sys.executable, "-m", "pytest", *plugin, "-p", "no:cacheprovider",
         "-W", "ignore::pytest.PytestUnknownMarkWarning",
         "--rootdir", str(root), "-q", "-rs", str(root)],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("kind", list(COLLECTION_SKIPS))
def test_a_module_skipped_while_collected_fails_the_run(tmp_path: Path, kind: str) -> None:
    source, reason = COLLECTION_SKIPS[kind]
    write(tmp_path, {"test_passing.py": PASSING, "test_skipped_module.py": source + HIDDEN})
    unguarded = run(tmp_path, policy=False)
    assert unguarded.returncode == 0, "without the policy, the run passes and the module is gone"
    assert "1 passed, 1 skipped" in unguarded.stdout
    proc = run(tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "1 passed, 1 skipped" in proc.stdout, "nothing failed, and still the run does"
    assert "unexpected skips" in proc.stdout
    assert f"test_skipped_module.py: skipped while collected, with all its tests: {reason}" in proc.stdout


def test_pytest_failure_statuses_stay_as_they_are(tmp_path: Path) -> None:
    skipped_module = COLLECTION_SKIPS["module-level-skip"][0] + HIDDEN
    failing = tmp_path / "failing"
    write(failing, {"test_fails.py": "def test_fails():\n    assert False\n", "test_skipped_module.py": skipped_module})
    assert run(failing).returncode == pytest.ExitCode.TESTS_FAILED
    broken = tmp_path / "broken"
    write(broken, {"test_passing.py": PASSING, "test_broken.py": "def test_broken(:\n",
                   "test_skipped_module.py": skipped_module})
    assert run(broken).returncode == pytest.ExitCode.INTERRUPTED, "a collection error"
    alone = tmp_path / "alone"
    write(alone, {"test_skipped_module.py": skipped_module})
    proc = run(alone)
    assert proc.returncode == pytest.ExitCode.NO_TESTS_COLLECTED
    assert "test_skipped_module.py: skipped while collected" in proc.stdout


def test_expected_skips_keep_a_run_green(tmp_path: Path) -> None:
    write(tmp_path, {
        "test_passing.py": PASSING,
        "tests/integration/test_inner_integration.py": INTEGRATION_SKIP,
        "tests/test_ci_suite_script.py": TIMEOUT_SKIP,
    })
    no_timeout = tmp_path / "empty-bin"
    no_timeout.mkdir()
    proc = run(tmp_path, path=str(no_timeout))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed, 2 skipped" in proc.stdout
    assert "1  integration test without a disposable database" in proc.stdout
    assert "1  no timeout utility" in proc.stdout
    assert "unexpected skips" not in proc.stdout


def test_unrelated_tests_and_false_conditions_cannot_use_an_exception(tmp_path: Path) -> None:
    root = tmp_path / "run"
    write(root, {"test_passing.py": PASSING, "tests/test_ci_suite_script.py": TIMEOUT_SKIP_WITHOUT_CAUSE})
    # The run's PATH holds a stand-in timeout executable and nothing else, so
    # the utility is present there whether or not this machine has one. The
    # run only looks it up; it never starts it.
    tools = tmp_path / "bin-with-timeout"
    tools.mkdir()
    stand_in = tools / "timeout"
    stand_in.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stand_in.chmod(0o755)
    assert shutil.which("timeout", path=str(tools)) == str(stand_in)
    proc = run(root, path=str(tools))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    named = "tests/test_ci_suite_script.py::test_an_external_timeout_stops_the_running_stage_and_the_suite"
    assert f"{named}: needs the timeout utility" in proc.stdout, "the condition is false here"
    assert "test_unrelated_with_the_same_reason: needs the timeout utility" in proc.stdout


def test_an_integration_skip_fails_once_a_database_is_configured(tmp_path: Path) -> None:
    write(tmp_path, {"test_passing.py": PASSING, "tests/integration/test_inner_integration.py": INTEGRATION_SKIP})
    proc = run(tmp_path, PEOPLE_PUBS_INTEGRATION_DSN="postgresql://postgres@127.0.0.1:1/people_db")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "test_inner_integration.py::test_needs_a_database: set PEOPLE_PUBS_INTEGRATION_DSN" in proc.stdout


def test_an_integration_skip_outside_the_integration_directory_fails(tmp_path: Path) -> None:
    write(tmp_path, {"test_passing.py": PASSING, "tests/test_misplaced.py": INTEGRATION_SKIP})
    proc = run(tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "tests/test_misplaced.py::test_needs_a_database" in proc.stdout


XFAIL = '''
import pytest


@pytest.mark.xfail(reason="known to fail")
def test_known_failure():
    assert False
'''


def test_an_expected_failure_is_not_an_expected_skip(tmp_path: Path) -> None:
    write(tmp_path, {"test_passing.py": PASSING, "test_xfail.py": XFAIL})
    proc = run(tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "test_xfail.py::test_known_failure: expected failure: known to fail" in proc.stdout
