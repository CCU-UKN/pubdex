"""Disposable-DB coverage for the biblio.publications_canon_web materialized
snapshot (TESTING_STRATEGY priority scenario): CONCURRENTLY refresh publishes
new canon rows, and a failed refresh leaves the prior populated snapshot
readable.
"""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.db.publications import upsert_publication


pytestmark = pytest.mark.integration

MATVIEW = "biblio.publications_canon_web"
_ATTRIBUTION_TERM = "Institute for Example Studies"


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _connect() -> psycopg.Connection:
    return psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True)


def _refresh(conn: psycopg.Connection) -> None:
    conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {MATVIEW}")


def _snapshot_dois(conn: psycopg.Connection, dois: list[str]) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT doi FROM {MATVIEW} WHERE doi = ANY(%s) ORDER BY doi", (dois,))
        return [r["doi"] for r in cur.fetchall()]


def test_concurrent_refresh_publishes_and_failed_refresh_keeps_prior_snapshot() -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/canonweb-{slug}"

    with _connect() as conn:
        # publications_canon only counts rows the attribution signal accepts,
        # and the signal is configured per installation. Configure one
        # fictional term for the duration of this test so the fixture is
        # attributed, then recompute the cached flag.
        with conn.transaction():
            conn.execute(
                "INSERT INTO app.institution_attribution_rules (rule_kind, pattern) "
                "VALUES ('affiliation', %s) ON CONFLICT DO NOTHING",
                (_ATTRIBUTION_TERM,),
            )
        upsert_publication(
            conn,
            {
                "doi": doi,
                "title": f"canon web fixture {slug}",
                "year": 2024,
                "venue": "Journal of Synthetic Metadata",
                "raw_crossref_json": {"funder": [{"name": _ATTRIBUTION_TERM}]},
                "is_preprint": False,
            },
        )

    try:
        with _connect() as conn:
            # Not visible until the snapshot is refreshed.
            assert _snapshot_dois(conn, [doi]) == []
            _refresh(conn)
            assert _snapshot_dois(conn, [doi]) == [doi], (
                "refresh must publish canon rows into the web snapshot"
            )

            # Break the refresh deterministically (CONCURRENTLY requires the
            # unique index) and verify the prior snapshot stays readable.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT indexname, indexdef FROM pg_indexes
                    WHERE schemaname = 'biblio' AND tablename = 'publications_canon_web'
                      AND indexdef ILIKE 'CREATE UNIQUE INDEX%'
                    """
                )
                idx = cur.fetchone()
            assert idx, "canon_web must carry a unique index for CONCURRENTLY"

            conn.execute(f"DROP INDEX biblio.{idx['indexname']}")
            try:
                with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                    _refresh(conn)
                assert _snapshot_dois(conn, [doi]) == [doi], (
                    "a failed refresh must leave the prior populated snapshot readable"
                )
            finally:
                conn.execute(idx["indexdef"])

            # Refresh works again after the index is restored.
            _refresh(conn)
            assert _snapshot_dois(conn, [doi]) == [doi]
    finally:
        with _connect() as conn:
            conn.execute("DELETE FROM biblio.publications WHERE doi = %s", (doi,))
            conn.execute(
                "DELETE FROM app.institution_attribution_rules WHERE pattern = %s",
                (_ATTRIBUTION_TERM,),
            )
            try:
                _refresh(conn)
            except Exception:
                pass
