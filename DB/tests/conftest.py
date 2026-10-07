"""Guards that apply to every test under this directory.

- The network guard (tests/network_guard.py) is installed here, before any test
  module is imported: offline tests may not use the network at all, and
  integration tests may reach only the disposable database that
  PEOPLE_PUBS_INTEGRATION_DSN or PEOPLE_DB_TEST_DSN names. The environment
  carries the same policy into every Python process a test starts, through
  tests/subprocess_guard/. A test during which anything was refused fails,
  even when the code under test caught the exception, and a refusal outside
  a test body -- during collection or in a fixture -- fails the run.
- The skip policy (tests/expected_skips.py) fails a run that contains a skip
  it does not name.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from tests import expected_skips, network_guard

_SUBPROCESS_GUARD = Path(__file__).resolve().parent / "subprocess_guard"
_REFUSAL_LOG_DIR = tempfile.mkdtemp(prefix="pubdex-network-guard-")

os.environ[network_guard.LOG_VARIABLE] = os.path.join(_REFUSAL_LOG_DIR, "refusals.log")
_POLICY = network_guard.install(network_guard.endpoints_from_environment())
os.environ[network_guard.POLICY_VARIABLE] = _POLICY.to_environment()
os.environ["PYTHONPATH"] = os.pathsep.join(
    entry for entry in (str(_SUBPROCESS_GUARD), os.environ.get("PYTHONPATH")) if entry
)


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(
        expected_skips.SkipPolicy(expected_skips.database_configured()), "expected-skips"
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item):
    start = network_guard.refusal_mark()
    try:
        result = yield
    except BaseException:
        network_guard.unexpected_refusals(start)  # the test fails anyway
        raise
    refused = network_guard.unexpected_refusals(start)
    if refused:
        pytest.fail(
            "the test network guard refused network access during this test, or could not "
            "guard a process it started (" + "; ".join(refused) + "): " + _POLICY.describe(),
            pytrace=False,
        )
    return result


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    refused = network_guard.unexpected_refusals(0)
    if refused:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                "the test network guard refused network access outside any test: " + "; ".join(refused),
                red=True,
            )
        if session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_unconfigure(config: pytest.Config) -> None:
    shutil.rmtree(_REFUSAL_LOG_DIR, ignore_errors=True)
