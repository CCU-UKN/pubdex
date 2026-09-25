"""Disposable-DB orchestration tests for refresh_state_runner in non-dry-run
mode (TESTING_STRATEGY priority scenario): state rows update once per item
with no duplicate inserts, one failing job does not stop the others, and the
per-job subprocess timeout kills hung children.

Children are the local tests.integration.runner_stub module — no network, no
real ingestion modules.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.sync import refresh_state_runner


pytestmark = pytest.mark.integration

DB_DIR = str(Path(__file__).resolve().parents[2])


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _connect() -> psycopg.Connection:
    return psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True)


def _seed_people(slug: str, count: int) -> list[int]:
    ids = []
    with _connect() as conn, conn.cursor() as cur:
        for i in range(count):
            cur.execute(
                "INSERT INTO app.people (display_name, person_kind) VALUES (%s, 'internal') RETURNING person_id",
                (f"runner-stub-{slug}-{i}",),
            )
            ids.append(int(cur.fetchone()["person_id"]))
    return ids


def _job(slug: str, name: str, *, behavior: str, timeout_seconds: int | None = None) -> dict:
    job = {
        "name": name,
        "enabled": True,
        "source": f"{name}_{slug}",
        "scope_type": "person",
        # NB: no '%' in this SQL — the runner interpolates it into a query
        # that still goes through psycopg placeholder parsing.
        "person_selector_sql": (
            "SELECT person_id FROM app.people "
            f"WHERE position('runner-stub-{slug}-' in display_name) = 1 ORDER BY person_id"
        ),
        "stale_after_days": 1,
        "limit": 10,
        "module": "tests.integration.runner_stub",
        "base_args": [],
        "per_person_args": ["--only-person-id", "{person_id}"],
        "dry_run_arg": "--dry-run",
        "cwd": DB_DIR,
        "env": {"RUNNER_STUB_BEHAVIOR": behavior},
    }
    if timeout_seconds is not None:
        job["timeout_seconds"] = timeout_seconds
    return job


def _write_config(tmp_path: Path, jobs: list[dict]) -> Path:
    config = {"state_table": "activity.source_refresh_state", "env": {}, "jobs": jobs}
    path = tmp_path / "refresh_jobs.json"
    path.write_text(json.dumps(config))
    return path


def _run_main(config: Path) -> None:
    refresh_state_runner.main(
        [
            "--config", str(config),
            "--dsn", _dsn(),
            "--python", sys.executable,
            "--no-lock",
        ]
    )


def _state_rows(sources: list[str]) -> list[dict]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT source, person_id, last_attempt_at, last_success_at, last_error_at
            FROM activity.source_refresh_state
            WHERE source = ANY(%s)
            ORDER BY source, person_id
            """,
            (sources,),
        )
        return cur.fetchall()


def _cleanup(slug: str, sources: list[str]) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM activity.source_refresh_state WHERE source = ANY(%s)", (sources,))
        cur.execute("DELETE FROM app.people WHERE display_name LIKE %s", (f"runner-stub-{slug}-%",))


def test_runner_updates_state_once_per_item_and_skips_when_fresh(tmp_path: Path) -> None:
    slug = uuid4().hex[:12]
    source = f"ok_{slug}"
    _seed_people(slug, 2)
    config = _write_config(tmp_path, [_job(slug, "ok", behavior="ok")])

    try:
        _run_main(config)
        rows = _state_rows([source])
        assert len(rows) == 2, "one state row per processed person, no duplicates"
        assert all(r["last_success_at"] is not None for r in rows)
        assert all(r["last_error_at"] is None for r in rows)
        first_success = {r["person_id"]: r["last_success_at"] for r in rows}

        # Second run inside the staleness window: nothing is due, state
        # rows are neither duplicated nor touched.
        _run_main(config)
        rows = _state_rows([source])
        assert len(rows) == 2
        assert {r["person_id"]: r["last_success_at"] for r in rows} == first_success
    finally:
        _cleanup(slug, [source])


def test_runner_continues_past_failing_job_and_exits_nonzero(tmp_path: Path) -> None:
    slug = uuid4().hex[:12]
    fail_source = f"boom_{slug}"
    ok_source = f"after_{slug}"
    _seed_people(slug, 1)
    config = _write_config(
        tmp_path,
        [_job(slug, "boom", behavior="fail"), _job(slug, "after", behavior="ok")],
    )

    try:
        with pytest.raises(SystemExit):
            _run_main(config)
        fail_rows = _state_rows([fail_source])
        ok_rows = _state_rows([ok_source])
        assert len(fail_rows) == 1 and fail_rows[0]["last_error_at"] is not None
        assert len(ok_rows) == 1 and ok_rows[0]["last_success_at"] is not None, (
            "a failing job must not prevent later jobs from running"
        )
    finally:
        _cleanup(slug, [fail_source, ok_source])


def test_runner_kills_hung_child_after_timeout(tmp_path: Path) -> None:
    slug = uuid4().hex[:12]
    source = f"hang_{slug}"
    _seed_people(slug, 1)
    job = _job(slug, "hang", behavior="sleep", timeout_seconds=2)
    job["env"]["RUNNER_STUB_SLEEP"] = "60"
    config = _write_config(tmp_path, [job])

    try:
        started = time.monotonic()
        with pytest.raises(SystemExit):
            _run_main(config)
        elapsed = time.monotonic() - started
        assert elapsed < 40, f"hung child must be killed by the timeout (took {elapsed:.0f}s)"
        rows = _state_rows([source])
        assert len(rows) == 1 and rows[0]["last_error_at"] is not None
    finally:
        _cleanup(slug, [source])
