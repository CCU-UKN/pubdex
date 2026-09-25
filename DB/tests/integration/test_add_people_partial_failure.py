"""Disposable-DB test for add_people row-level failure isolation.

A row-level PostgreSQL error mid-batch must roll back only that record's
writes (per-record savepoint), keep earlier and later rows, recover the
shared transaction from its aborted state, and report end-of-run counters
that match what the final commit actually persisted. Synthetic people only,
no live network access.
"""
from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.sync import add_people
from people_pubs.sync.add_people import run_add_people


pytestmark = pytest.mark.integration

# Fictional but checksum-valid ORCIDs (ISO 7064 11-2), reserved for this test.
_ORCID_FIRST = "0000-0000-0000-0036"
_ORCID_FAILING = "0000-0000-0000-0044"
_ORCID_LAST = "0000-0000-0000-0052"
_ALL_ORCIDS = (_ORCID_FIRST, _ORCID_FAILING, _ORCID_LAST)


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _records() -> list[dict]:
    return [
        {"orcid": _ORCID_FIRST, "display_name": "Savepoint First", "person_kind": "external"},
        {"orcid": _ORCID_FAILING, "display_name": "Savepoint Failing", "person_kind": "external"},
        {"orcid": _ORCID_LAST, "display_name": "Savepoint Last", "person_kind": "external"},
    ]


def _persisted_orcids(dsn: str) -> set[str]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT orcid FROM app.identities_orcid WHERE orcid = ANY(%s)",
            (list(_ALL_ORCIDS),),
        ).fetchall()
    return {row["orcid"] for row in rows}


def _cleanup(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            DELETE FROM app.people
            WHERE person_id IN (
                SELECT person_id FROM app.identities_orcid WHERE orcid = ANY(%s)
            )
            """,
            (list(_ALL_ORCIDS),),
        )


def test_row_db_error_keeps_other_rows_and_truthful_counters(monkeypatch):
    dsn = _dsn()
    original_ensure_key = add_people._ensure_orcid_key

    def flaky_ensure_orcid_key(conn, person_id, orcid, dry_run):
        if orcid == _ORCID_FAILING:
            # Genuine PostgreSQL error: puts the transaction into the aborted
            # state the savepoint recovery has to handle.
            conn.execute("SELECT 1/0")
        return original_ensure_key(conn, person_id, orcid, dry_run=dry_run)

    monkeypatch.setattr(add_people, "_ensure_orcid_key", flaky_ensure_orcid_key)
    try:
        stats = run_add_people(
            dsn=dsn,
            records=_records(),
            default_person_kind="external",
            update_existing=False,
            dry_run=False,
            lookup_orcid=False,
        )

        assert stats.total == 3
        assert stats.errors == 1
        assert stats.inserted == 2
        assert stats.updated == 0
        assert stats.skipped_existing == 0
        assert stats.skipped_invalid == 0

        # The summary must match what was committed: rows before and after
        # the failing record survive, the failing record leaves nothing.
        assert _persisted_orcids(dsn) == {_ORCID_FIRST, _ORCID_LAST}

        # Documented recovery path: rerunning after the cause is fixed adds
        # only the failed row (idempotent re-import).
        monkeypatch.setattr(add_people, "_ensure_orcid_key", original_ensure_key)
        stats_rerun = run_add_people(
            dsn=dsn,
            records=_records(),
            default_person_kind="external",
            update_existing=False,
            dry_run=False,
            lookup_orcid=False,
        )
        assert stats_rerun.inserted == 1
        assert stats_rerun.skipped_existing == 2
        assert stats_rerun.errors == 0
        assert _persisted_orcids(dsn) == set(_ALL_ORCIDS)
    finally:
        _cleanup(dsn)
