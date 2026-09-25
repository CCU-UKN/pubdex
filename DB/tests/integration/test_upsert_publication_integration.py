"""Disposable-DB coverage for the actual INSERT…ON CONFLICT path of
upsert_publication(): provenance merge, idempotent rerun, deliberate SOT
repair (TESTING_STRATEGY 'Future DB-container idempotency tests')."""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.db.publications import upsert_publication


pytestmark = pytest.mark.integration


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _fetch(conn: psycopg.Connection, doi: str) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT pub_id, doi, title, source_of_truth, raw_json FROM biblio.publications WHERE doi = %s",
            (doi,),
        )
        return cur.fetchall()


def test_upsert_publication_merges_provenance_and_is_idempotent() -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/upsert-{slug}"

    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            # 1) fresh insert from a Crossref-shaped payload
            pub_id = upsert_publication(
                conn,
                {
                    "doi": doi,
                    "title": f"Upsert Fixture {slug}",
                    "year": 2024,
                    "venue": "Journal of Synthetic Metadata",
                    "raw_crossref_json": {"DOI": doi, "title": ["Upsert Fixture"]},
                },
            )
            rows = _fetch(conn, doi)
            assert len(rows) == 1
            assert rows[0]["pub_id"] == pub_id
            assert "crossref" in rows[0]["source_of_truth"]
            assert set(rows[0]["raw_json"]) == {"crossref"}

            # 2) second source for the same DOI: cumulative provenance,
            #    source-keyed raw_json, still exactly one row
            second_id = upsert_publication(
                conn,
                {
                    "doi": doi,
                    "title": f"Upsert Fixture {slug}",
                    "year": 2024,
                    "raw_datacite_json": {"id": f"dc-{slug}"},
                },
            )
            rows = _fetch(conn, doi)
            assert len(rows) == 1
            assert second_id == pub_id
            sot = rows[0]["source_of_truth"]
            assert "crossref" in sot and "datacite" in sot
            assert set(rows[0]["raw_json"]) == {"crossref", "datacite"}

            # 3) exact rerun of (2): fully idempotent
            before = rows[0]
            upsert_publication(
                conn,
                {
                    "doi": doi,
                    "title": f"Upsert Fixture {slug}",
                    "year": 2024,
                    "raw_datacite_json": {"id": f"dc-{slug}"},
                },
            )
            rows = _fetch(conn, doi)
            assert len(rows) == 1
            assert rows[0]["source_of_truth"] == before["source_of_truth"]
            assert rows[0]["raw_json"] == before["raw_json"]

            # 4) deliberate repair replaces instead of merging
            upsert_publication(
                conn,
                {
                    "doi": doi,
                    "title": f"Upsert Fixture {slug}",
                    "year": 2024,
                    "source_of_truth": "manual",
                    "replace_source_of_truth": True,
                },
            )
            rows = _fetch(conn, doi)
            assert len(rows) == 1
            assert rows[0]["source_of_truth"] == "manual"
        finally:
            conn.rollback()


def test_upsert_publication_requires_doi_and_respects_dry_run() -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/upsert-dry-{slug}"

    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            assert upsert_publication(conn, {"title": "no doi"}) is None

            upsert_publication(conn, {"doi": doi, "title": "dry", "year": 2024}, dry_run=True)
            assert _fetch(conn, doi) == []
        finally:
            conn.rollback()
