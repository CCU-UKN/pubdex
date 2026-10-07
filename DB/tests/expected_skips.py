"""Which skipped tests a run may contain; any other skip fails the run.

A skip can hide a test that silently stopped running, so every skip the suite
expects is named below: which tests may skip, for which reason, and under
which condition. tests/conftest.py applies this policy to every pytest run
under tests/: a skip that no rule names, an expected failure (xfail), or a
module skipped while it is collected is listed at the end of the run and
makes it fail even when every other test passed. The rules name tests and
reasons, not counts, so adding tests does not make the policy stale; a new
reason to skip needs a new rule here, which is the point.

Collection-time skips -- pytest.skip(..., allow_module_level=True),
pytest.importorskip or unittest.SkipTest at module level -- take every test of
the module out of the run without a trace in the test count, so none is
expected: COLLECTION_RULES is empty, and a rule may be added there only for a
module whose skip is narrowly justified.
"""
from __future__ import annotations

import os
import shutil
from collections import Counter
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import pytest

NO_DATABASE_REASON = "set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests"
INTEGRATION_DIRECTORY = "tests/integration/"
GUARDRAILS = "tests/test_refresh_jobs_guardrails.py"
DISCOVERY_GUARDRAILS = (
    f"{GUARDRAILS}::test_discovery_job_caps_new_records",
    f"{GUARDRAILS}::test_discovery_job_skips_existing",
    f"{GUARDRAILS}::test_affiliation_job_has_post_filter",
)
AFFILIATION_GUARDRAIL = f"{GUARDRAILS}::test_affiliation_job_has_post_filter"
UNREADABLE_FILE_TEST = "tests/test_secrets_and_local_config.py::test_unreadable_file_is_an_error_not_a_pass"
EXTERNAL_TIMEOUT_TESTS = (
    "tests/test_ci_suite_script.py::test_an_external_timeout_stops_the_running_stage_and_the_suite",
    "tests/test_disposable_integration_script.py::test_an_external_timeout_cleans_up",
)


@dataclass(frozen=True)
class Skip:
    """One skipped test, as the policy sees it."""

    nodeid: str
    reason: str
    integration: bool
    database_configured: bool

    @property
    def module(self) -> str:
        return self.nodeid.split("::", 1)[0]

    @property
    def function(self) -> str:
        """The test's id without its parameters: path::name."""
        return self.nodeid.split("[", 1)[0]


@dataclass(frozen=True)
class Rule:
    name: str
    why: str
    applies: Callable[[Skip], bool]


def database_configured() -> bool:
    return any(os.environ.get(name) for name in ("PEOPLE_PUBS_INTEGRATION_DSN", "PEOPLE_DB_TEST_DSN"))


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def timeout_missing() -> bool:
    return shutil.which("timeout") is None


RULES: Tuple[Rule, ...] = (
    Rule(
        "integration test without a disposable database",
        "integration tests need a disposable PostgreSQL; "
        "./run_disposable_integration.sh, stage 5 of run_ci_suite.sh, runs all "
        "of them and fails if any is skipped",
        lambda skip: (
            skip.module.startswith(INTEGRATION_DIRECTORY)
            and skip.integration
            and skip.reason == NO_DATABASE_REASON
            and not skip.database_configured
        ),
    ),
    Rule(
        "no discovery job in refresh_jobs.json",
        "the shipped job list schedules no broad discovery search, so the "
        "guardrails parametrized over such jobs have none to check; synthetic "
        "probes in the same module run the same checks",
        lambda skip: skip.function in DISCOVERY_GUARDRAILS and skip.reason.startswith("got empty parameter set"),
    ),
    Rule(
        "discovery job without an affiliation search",
        "the post-filter guardrail concerns affiliation searches only",
        lambda skip: skip.function == AFFILIATION_GUARDRAIL and skip.reason == "not an affiliation job",
    ),
    Rule(
        "running as root",
        "root can read a file whose permissions forbid it, so the unreadable-file "
        "case cannot be produced",
        lambda skip: (
            skip.function == UNREADABLE_FILE_TEST
            and skip.reason == "root can read any file"
            and running_as_root()
        ),
    ),
    Rule(
        "no timeout utility",
        "the external-timeout cases need timeout(1), which not every platform "
        "ships (macOS without GNU coreutils, for one)",
        lambda skip: (
            skip.function in EXTERNAL_TIMEOUT_TESTS
            and skip.reason == "needs the timeout utility"
            and timeout_missing()
        ),
    ),
)

# Skips raised while a module is collected. None is expected; see the module
# docstring before adding one.
COLLECTION_RULES: Tuple[Rule, ...] = ()


def classify(skip: Skip) -> Optional[Rule]:
    """The rule that expects this skip, or None."""
    return next((rule for rule in RULES if rule.applies(skip)), None)


def classify_collection(skip: Skip) -> Optional[Rule]:
    """The rule that expects this collection-time skip, or None."""
    return next((rule for rule in COLLECTION_RULES if rule.applies(skip)), None)


def _reason(report) -> str:
    longrepr = report.longrepr
    reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr or "")
    return reason[len("Skipped: "):] if reason.startswith("Skipped: ") else reason


class SkipPolicy:
    """Collects every skip of the run and fails the run on unexpected ones."""

    def __init__(self, database: bool) -> None:
        self.database = database
        self.expected: Counter = Counter()
        self.unexpected: List[Tuple[str, str]] = []

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if not report.skipped:
            return
        skip = Skip(report.nodeid, _reason(report), False, self.database)
        rule = classify_collection(skip)
        if rule is None:
            self.unexpected.append((skip.nodeid, f"skipped while collected, with all its tests: {skip.reason}"))
        else:
            self.expected[rule.name] += 1

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if not report.skipped:
            return
        if hasattr(report, "wasxfail"):
            self.unexpected.append((report.nodeid, f"expected failure: {report.wasxfail or 'no reason'}"))
            return
        skip = Skip(report.nodeid, _reason(report), "integration" in report.keywords, self.database)
        rule = classify(skip)
        if rule is None:
            self.unexpected.append((skip.nodeid, skip.reason))
        else:
            self.expected[rule.name] += 1

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if self.expected:
            terminalreporter.section("expected skips (tests/expected_skips.py)")
            for rule in RULES + COLLECTION_RULES:
                if self.expected[rule.name]:
                    terminalreporter.line(f"{self.expected[rule.name]:5d}  {rule.name}: {rule.why}")
        if self.unexpected:
            terminalreporter.section("unexpected skips", red=True)
            for nodeid, reason in self.unexpected:
                terminalreporter.line(f"{nodeid}: {reason}", red=True)
            terminalreporter.line(
                "A skip that tests/expected_skips.py does not name fails the run; "
                "make the test run, or add a rule saying why the skip is expected.",
                red=True,
            )

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        # Only a passing run is turned into a failing one; a failure status
        # pytest set itself (failed tests, an interrupted collection, no tests
        # at all) stays as it is.
        if self.unexpected and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
