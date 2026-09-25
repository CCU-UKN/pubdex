"""Failure isolation of the tracked operator bundles, offline.

RunAllDayly.sh and RunAllWeekly.sh promise that one failing job does not stop
the later ones and that the bundle still exits non-zero. Each bundle is
copied next to stub run_refresh_job.sh / run_backup.sh scripts that record
their arguments and fail on demand; no database, network or real job runs.
The retained job lists are pinned as well.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

DB_DIR = Path(__file__).resolve().parents[1]

WEEKLY_JOBS = [
    "crossref_backfill_query",
    "semantic_backfill_query",
    "dblp_backfill_query",
    "low_source_canon_backfill_query",
]
DAILY_JOBS = [
    "orcid_profiles_person",
    "orcid_works_person",
    "crossref_search_orcid_person",
]

STUB_RUNNER = """#!/usr/bin/env bash
printf 'run_refresh_job %s\\n' "$*" >> "$BUNDLE_STUB_LOG"
for arg in "$@"; do
  if [ "$arg" = "$BUNDLE_STUB_FAIL_JOB" ]; then
    echo "stub failure for $arg" >&2
    exit 1
  fi
done
"""

STUB_BACKUP = """#!/usr/bin/env bash
printf 'run_backup\\n' >> "$BUNDLE_STUB_LOG"
if [ "$BUNDLE_STUB_FAIL_JOB" = "run_backup" ]; then
  echo "stub backup failure" >&2
  exit 1
fi
"""


def _install(tmp_path: Path, bundle: str) -> Path:
    script = tmp_path / bundle
    shutil.copy(DB_DIR / bundle, script)
    for name, body in (("run_refresh_job.sh", STUB_RUNNER), ("run_backup.sh", STUB_BACKUP)):
        stub = tmp_path / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _run(script: Path, tmp_path: Path, fail_job: str) -> tuple[int, list[str]]:
    log = tmp_path / "invocations.log"
    env = dict(os.environ, BUNDLE_STUB_LOG=str(log), BUNDLE_STUB_FAIL_JOB=fail_job)
    proc = subprocess.run(["bash", str(script)], env=env, cwd=tmp_path, capture_output=True, text=True)
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc.returncode, lines


def _runner_jobs(lines: list[str]) -> list[str]:
    jobs = []
    for line in lines:
        if not line.startswith("run_refresh_job "):
            continue
        args = line.split()[1:]
        onlys = [args[i + 1] for i, a in enumerate(args) if a == "--only"]
        assert len(onlys) == 1, f"one job per runner invocation, got {line!r}"
        assert "--stop-on-error" in args, f"per-job stop-on-error must be kept: {line!r}"
        jobs.append(onlys[0])
    return jobs


@pytest.mark.parametrize("fail_job", WEEKLY_JOBS[:1] + WEEKLY_JOBS[-1:], ids=["first-fails", "last-fails"])
def test_weekly_runs_every_later_job_and_exits_nonzero(tmp_path: Path, fail_job: str) -> None:
    rc, lines = _run(_install(tmp_path, "RunAllWeekly.sh"), tmp_path, fail_job)
    assert rc != 0, "a failed job must make the bundle exit non-zero"
    assert _runner_jobs(lines) == WEEKLY_JOBS, "every retained weekly job runs, in order, even after a failure"


def test_weekly_exits_zero_when_every_job_succeeds(tmp_path: Path) -> None:
    rc, lines = _run(_install(tmp_path, "RunAllWeekly.sh"), tmp_path, "nothing-fails")
    assert rc == 0
    assert _runner_jobs(lines) == WEEKLY_JOBS


def test_weekly_logs_each_invocation(tmp_path: Path) -> None:
    _run(_install(tmp_path, "RunAllWeekly.sh"), tmp_path, WEEKLY_JOBS[1])
    err = (tmp_path / "refresh_backfills.err.log").read_text(encoding="utf-8")
    assert "stub failure for semantic_backfill_query" in err, "job stderr lands in the bundle's error log"


@pytest.mark.parametrize("fail_job", ["run_backup", DAILY_JOBS[0]], ids=["backup-fails", "first-job-fails"])
def test_daily_runs_every_later_job_and_exits_nonzero(tmp_path: Path, fail_job: str) -> None:
    rc, lines = _run(_install(tmp_path, "RunAllDayly.sh"), tmp_path, fail_job)
    assert rc != 0
    assert lines[0] == "run_backup", "backup runs first"
    assert _runner_jobs(lines) == DAILY_JOBS, "every retained daily job runs after a failure"


def test_bundle_comments_describe_cron_accurately() -> None:
    cron = (DB_DIR / "cron.example").read_text(encoding="utf-8")
    for bundle in ("RunAllDayly.sh", "RunAllWeekly.sh"):
        assert f"./{bundle}" in cron, "cron.example invokes the bundle, not individual jobs"
        # Join the comment header so a line wrap cannot hide a phrase.
        header = " ".join(
            line.lstrip("# ").strip()
            for line in (DB_DIR / bundle).read_text(encoding="utf-8").splitlines()
            if line.startswith("#")
        )
        assert "schedules the same jobs individually" not in header
        assert "cron.example invokes this script" in header
        assert "one job's failure does not stop the later ones" in header
