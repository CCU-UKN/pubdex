"""Disposable-DB coverage for the clean-clone acceptance path end to end.

This is the database half of `people_pubs.sync.fixture_demo`: the bundled
ORCID payload is ingested, the same work is enriched from the bundled Crossref
payload, fictional attribution is configured, and the real export
implementation has to produce a nonempty CSV containing the expected synthetic
publication. `DB/run_task_1a_demo.sh` performs the same sequence from a clean
clone against its own throwaway container; this test pins it inside the suite.

Like the other institutional-attribution tests, this one owns
app.institution_attribution_rules for the length of the run, so it needs a
disposable database rather than a shared one.
"""
from __future__ import annotations

import io
import os

import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.sync.export_publications import export_publications
from people_pubs.sync.fixture_demo import (
    CROSSREF_FIXTURE,
    DEMO_AFFILIATION,
    DEMO_DOI,
    DEMO_ORCID,
    DEMO_TITLE,
    ORCID_FIXTURE,
    ingest_fixtures,
    load_fixture,
)

#: The bundled roster's external co-author. The Crossref payload carries no
#: ORCID for him, so ingestion links him by name -- which is exactly the case
#: that used to mislabel an external person as an internal author.
EXTERNAL_NAME = "Bert Example"
EXTERNAL_ORCID = "0000-0000-0000-001X"

pytestmark = pytest.mark.integration


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _reset(conn: psycopg.Connection) -> None:
    """Remove everything this test creates, in both directions of the run."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM biblio.publications WHERE doi = %s", (DEMO_DOI,))
        cur.execute(
            "DELETE FROM app.people WHERE person_id IN "
            "(SELECT person_id FROM app.identities_orcid WHERE orcid = ANY(%s))",
            ([DEMO_ORCID, EXTERNAL_ORCID],),
        )
        cur.execute("DELETE FROM app.institution_attribution_rules")
        cur.execute("SELECT biblio.recompute_institution_attribution()")
    conn.commit()


def _create_roster(conn: psycopg.Connection) -> int:
    """The two roster people the payloads mention: one internal, one external."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO app.people (person_kind, display_name) "
            "VALUES ('internal', 'Ada Example') RETURNING person_id"
        )
        person_id = cur.fetchone()["person_id"]
        cur.execute(
            "INSERT INTO app.identities_orcid (person_id, orcid) VALUES (%s, %s)",
            (person_id, DEMO_ORCID),
        )
        cur.execute(
            "INSERT INTO app.people (person_kind, display_name) "
            "VALUES ('external', %s) RETURNING person_id",
            (EXTERNAL_NAME,),
        )
        external_id = cur.fetchone()["person_id"]
        cur.execute(
            "INSERT INTO app.identities_orcid (person_id, orcid) VALUES (%s, %s)",
            (external_id, EXTERNAL_ORCID),
        )
    conn.commit()
    return person_id


def _configure_attribution(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.institution_attribution_rules (rule_kind, pattern) "
            "VALUES ('affiliation', %s)",
            (DEMO_AFFILIATION,),
        )
        cur.execute("SELECT biblio.recompute_institution_attribution()")
    conn.commit()


def test_fixture_demo_ingests_queries_and_exports_end_to_end() -> None:
    dsn = _dsn()
    orcid_work = load_fixture(ORCID_FIXTURE)
    crossref_work = load_fixture(CROSSREF_FIXTURE)

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        _reset(conn)
        try:
            person_id = _create_roster(conn)

            results = ingest_fixtures(
                conn, orcid_work=orcid_work, crossref_work=crossref_work
            )
            assert [r.stage for r in results] == ["orcid", "crossref"]
            # The first stage stores the work; the second updates the same row.
            assert results[0].inserted == 1
            assert results[1].inserted == 0
            assert results[1].updated == 1

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT pub_id, title, year, venue, source_of_truth, raw_json "
                    "FROM biblio.publications WHERE doi = %s",
                    (DEMO_DOI,),
                )
                rows = cur.fetchall()
            assert len(rows) == 1, "the two stages must share one publication row"
            stored = rows[0]
            assert stored["title"] == DEMO_TITLE
            assert stored["year"] == 2024
            assert stored["venue"] == "Journal of Synthetic Metadata"
            # Provenance is cumulative, and both payloads are kept source-keyed.
            assert "orcid" in stored["source_of_truth"]
            assert "crossref" in stored["source_of_truth"]
            assert {"orcid", "crossref"} <= set(stored["raw_json"])

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT author_position, author_name, person_id, "
                    "is_internal_at_ingest "
                    "FROM biblio.authorships_curated WHERE pub_id = %s "
                    "ORDER BY author_position",
                    (stored["pub_id"],),
                )
                authors = cur.fetchall()
            assert [a["author_name"] for a in authors] == ["Ada Example", EXTERNAL_NAME]
            assert authors[0]["person_id"] == person_id
            # Both authors are linked; only the roster's internal person counts
            # as one of the organisation's own authors.
            assert authors[0]["is_internal_at_ingest"] is True
            assert authors[1]["person_id"] is not None, "the external co-author stays linked"
            assert authors[1]["is_internal_at_ingest"] is False

            # Nothing is canonical until attribution is configured.
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM biblio.publications_canon WHERE doi = %s",
                    (DEMO_DOI,),
                )
                assert cur.fetchone()["n"] == 0
            conn.rollback()

            _configure_attribution(conn)

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT doi, title, source_of_truth, internal_authors "
                    "FROM biblio.publications_canon WHERE doi = %s",
                    (DEMO_DOI,),
                )
                canon = cur.fetchall()
            assert len(canon) == 1, "the configured rule must make the work canonical"
            assert canon[0]["title"] == DEMO_TITLE
            assert canon[0]["internal_authors"] == "Ada Example"
            conn.rollback()

            # The real export implementation, against the committed state.
            buffer = io.StringIO()
            written = export_publications(dsn, buffer)
            assert written >= 1, "the acceptance export must not be empty"
            text = buffer.getvalue()
            assert DEMO_DOI in text
            assert DEMO_TITLE in text
            assert "Ada Example" in text
            assert EXTERNAL_NAME not in text, "an external co-author is not an internal author"
        finally:
            conn.rollback()
            _reset(conn)
