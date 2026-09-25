from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.sync import publication_dedup_overrides as dedup


pytestmark = pytest.mark.integration


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def test_durable_dedup_decision_survives_publication_id_reset() -> None:
    slug = uuid4().hex[:12]
    title = f"durable dedup integration {slug}"
    final_doi = f"10.5555/integration-{slug}"
    chosen_doi = f"10.5555/integration-{slug}.v2"

    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            dedup._ensure_table(conn, dry_run=False)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO biblio.publications
                      (doi, title, year, venue, source_of_truth, raw_json, is_preprint)
                    VALUES (%s, %s, 2026, 'Journal', 'manual', '{}'::jsonb, false)
                    RETURNING pub_id
                    """,
                    (final_doi, title),
                )
                final_id = int(cur.fetchone()["pub_id"])
                cur.execute(
                    """
                    INSERT INTO biblio.publications
                      (doi, title, year, venue, source_of_truth, raw_json, is_preprint)
                    VALUES (%s, %s, 2026, 'Preprint', 'manual', '{}'::jsonb, true)
                    RETURNING pub_id
                    """,
                    (chosen_doi, title),
                )
                chosen_id = int(cur.fetchone()["pub_id"])

            dedup._set_merge_one(
                conn,
                loser_pub_id=final_id,
                winner_pub_id=chosen_id,
                note="integration durable choice",
                has_log_table=True,
                applied_by="pytest",
                dry_run=False,
            )

            with conn.cursor() as cur:
                cur.execute("DELETE FROM biblio.publications WHERE pub_id IN (%s, %s)", (final_id, chosen_id))
                cur.execute(
                    """
                    INSERT INTO biblio.publications
                      (doi, title, year, venue, source_of_truth, raw_json, is_preprint)
                    VALUES (%s, %s, 2026, 'Journal', 'manual', '{}'::jsonb, false)
                    RETURNING pub_id
                    """,
                    (final_doi, title),
                )
                new_final_id = int(cur.fetchone()["pub_id"])
                cur.execute(
                    """
                    INSERT INTO biblio.publications
                      (doi, title, year, venue, source_of_truth, raw_json, is_preprint)
                    VALUES (%s, %s, 2026, 'Preprint', 'manual', '{}'::jsonb, true)
                    RETURNING pub_id
                    """,
                    (chosen_doi, title),
                )
                new_chosen_id = int(cur.fetchone()["pub_id"])
                cur.execute(
                    """
                    SELECT pub_id, doi
                    FROM biblio.publications_clean
                    WHERE doi IN (%s, %s)
                    ORDER BY pub_id
                    """,
                    (final_doi, chosen_doi),
                )
                rows = cur.fetchall()

            assert new_final_id != final_id
            assert new_chosen_id != chosen_id
            assert rows == [{"pub_id": new_chosen_id, "doi": chosen_doi}]
        finally:
            conn.rollback()
