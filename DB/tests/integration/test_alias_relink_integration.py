"""Disposable-DB fixture tests for alias backfill and authorship relinking
Synthetic people/publications/authorships only, no live
network access, no real people data.

Covers:
- alias_backfill derives manual aliases (with Last-comma-First variants) from
  linked authorship rows, reads through biblio.authorships_curated so
  force_null overrides are honored, and is rerun-idempotent;
- relink_authorships links NULL-person authorship rows by ORCID and by unique
  name/alias match, writes relink aliases, leaves non-matches reviewable, and
  is rerun-idempotent.
"""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.db.publications import upsert_publication
from people_pubs.sync.alias_backfill import run_alias_backfill
from people_pubs.sync.relink_authorships import run_relink_authorships


pytestmark = pytest.mark.integration

# Fictional but checksum-valid ORCID (ISO 7064 11-2), reserved for this test.
_RELINK_ORCID = "0000-0000-0000-0028"


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _connect() -> psycopg.Connection:
    return psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True)


def _insert_person(cur, display_name: str) -> int:
    cur.execute(
        "INSERT INTO app.people (display_name, person_kind) VALUES (%s, 'internal') RETURNING person_id",
        (display_name,),
    )
    return int(cur.fetchone()["person_id"])


def _insert_authorship(
    cur,
    *,
    pub_id: int,
    position: int,
    author_name: str,
    display_name_at_pub: str | None = None,
    person_id: int | None = None,
    paper_orcid: str | None = None,
) -> None:
    cur.execute(
        """
        INSERT INTO biblio.authorships
          (pub_id, author_position, author_name, display_name_at_pub, person_id, paper_orcid)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (pub_id, position, author_name, display_name_at_pub, person_id, paper_orcid),
    )


def _aliases_for(conn, person_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT alias_name, alias_sources
            FROM app.person_name_aliases_manual
            WHERE person_id = %s
            ORDER BY lower(alias_name)
            """,
            (person_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def _cleanup(dois: list[str], person_ids: list[int]) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM biblio.authorship_manual_overrides WHERE pub_id IN "
            "(SELECT pub_id FROM biblio.publications WHERE doi = ANY(%s))",
            (dois,),
        )
        cur.execute(
            "DELETE FROM biblio.authorships WHERE pub_id IN "
            "(SELECT pub_id FROM biblio.publications WHERE doi = ANY(%s))",
            (dois,),
        )
        cur.execute("DELETE FROM biblio.publications WHERE doi = ANY(%s)", (dois,))
        if person_ids:
            cur.execute(
                "DELETE FROM app.person_name_aliases_manual WHERE person_id = ANY(%s)",
                (person_ids,),
            )
            cur.execute(
                "DELETE FROM app.identities_orcid WHERE person_id = ANY(%s)", (person_ids,)
            )
            cur.execute("DELETE FROM app.people WHERE person_id = ANY(%s)", (person_ids,))


def test_alias_backfill_reads_curated_rows_and_reruns_idempotently() -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/aliasbf-{slug}"
    doi_overridden = f"10.5555/aliasbf-ovr-{slug}"
    dsn = _dsn()

    with _connect() as conn, conn.cursor() as cur:
        person_id = _insert_person(cur, f"Alias Fixture {slug}")
        pub_id = upsert_publication(
            conn,
            {"doi": doi, "title": f"alias backfill fixture {slug}", "year": 2024,
             "raw_orcid_json": {"put-code": 5}},
        )
        # A person may appear only once per publication, so the curator-detached
        # case lives on a second publication.
        pub_id_overridden = upsert_publication(
            conn,
            {"doi": doi_overridden, "title": f"alias backfill override fixture {slug}",
             "year": 2024, "raw_orcid_json": {"put-code": 6}},
        )
        # Linked row: both name spellings should become aliases.
        _insert_authorship(
            cur,
            pub_id=pub_id,
            position=1,
            author_name=f"Fixture, A. {slug}",
            display_name_at_pub=f"Alias Fixture {slug}",
            person_id=person_id,
        )
        # Unlinked row: person_id IS NULL rows contribute no aliases.
        _insert_authorship(
            cur,
            pub_id=pub_id,
            position=2,
            author_name=f"Detached Author {slug}",
            person_id=None,
        )
        # Linked row that a curator detached via force_null: authorships_curated
        # reads person_id NULL, so no alias may be derived from it.
        _insert_authorship(
            cur,
            pub_id=pub_id_overridden,
            position=1,
            author_name=f"Overridden Author {slug}",
            person_id=person_id,
        )
        cur.execute(
            """
            INSERT INTO biblio.authorship_manual_overrides
              (pub_id, author_position, display_name_at_pub, paper_orcid, mode,
               person_id, lock_row, active, reason, evidence, source_file, source_row, created_by)
            VALUES (%s, %s, %s, NULL, 'force_null', NULL, TRUE, TRUE,
                    'pytest force_null', '{}'::jsonb, 'pytest', NULL, 'pytest')
            """,
            (pub_id_overridden, 1, f"Overridden Author {slug}"),
        )

    def _run() -> None:
        run_alias_backfill(
            dsn=dsn,
            only_person_id=person_id,
            limit=None,
            commit_every=0,
            dry_run=False,
            debug=False,
        )

    try:
        _run()
        with _connect() as conn:
            first = _aliases_for(conn, person_id)
        names = {a["alias_name"] for a in first}
        assert f"Alias Fixture {slug}" in names
        assert f"Fixture, A. {slug}" in names
        assert f"A. {slug} Fixture" in names, (
            "'Last, First' authorship names must also produce the swapped variant"
        )
        assert not any(f"Overridden Author {slug}" in n for n in names), (
            "alias backfill must read authorships_curated: force_null rows contribute nothing"
        )
        assert all("authorships.db_only" in (a["alias_sources"] or []) for a in first)

        _run()
        with _connect() as conn:
            second = _aliases_for(conn, person_id)
        assert second == first, "second identical run must not add or change aliases"
    finally:
        _cleanup([doi, doi_overridden], [person_id])


def test_relink_authorships_links_orcid_and_unique_names_and_reruns_stable() -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/relink-{slug}"
    dsn = _dsn()

    with _connect() as conn, conn.cursor() as cur:
        person_orcid = _insert_person(cur, f"Orcid Person {slug}")
        cur.execute(
            "INSERT INTO app.identities_orcid (person_id, orcid) VALUES (%s, %s)",
            (person_orcid, _RELINK_ORCID),
        )
        person_name = _insert_person(cur, f"Uniquename Relinktarget{slug}")
        pub_id = upsert_publication(
            conn,
            {"doi": doi, "title": f"relink fixture {slug}", "year": 2024,
             "raw_orcid_json": {"put-code": 6}},
        )
        # ORCID beats a non-matching name.
        _insert_authorship(
            cur,
            pub_id=pub_id,
            position=1,
            author_name="Somebody Else Entirely",
            paper_orcid=_RELINK_ORCID,
        )
        # Unique display-name match above the score threshold.
        _insert_authorship(
            cur,
            pub_id=pub_id,
            position=2,
            author_name=f"Uniquename Relinktarget{slug}",
        )
        # No match: must stay NULL (reviewable) rather than being guessed.
        _insert_authorship(
            cur,
            pub_id=pub_id,
            position=3,
            author_name=f"Nomatch Anywhere{slug}",
        )

    def _run():
        return run_relink_authorships(
            dsn=dsn,
            only_pub_id=pub_id,
            max_n=100,
            overwrite=False,
            write_aliases=True,
            dry_run=False,
        )

    def _links(conn) -> list:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT author_position, person_id
                FROM biblio.authorships
                WHERE pub_id = %s
                ORDER BY author_position
                """,
                (pub_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    try:
        stats = _run()
        assert stats.total == 3
        assert stats.matched == 2
        assert stats.updated == 2
        assert stats.skipped_no_match == 1, "low-confidence rows stay unlinked for review"

        with _connect() as conn:
            first = _links(conn)
        assert first == [
            {"author_position": 1, "person_id": person_orcid},
            {"author_position": 2, "person_id": person_name},
            {"author_position": 3, "person_id": None},
        ]

        with _connect() as conn:
            orcid_aliases = {a["alias_name"]: a["alias_sources"] for a in _aliases_for(conn, person_orcid)}
        assert "Somebody Else Entirely" in orcid_aliases, (
            "relink must record the paper spelling as an alias"
        )
        assert "authorships.relink" in orcid_aliases["Somebody Else Entirely"]

        stats2 = _run()
        assert stats2.total == 1, "already-linked rows must not be reprocessed"
        assert stats2.updated == 0
        assert stats2.skipped_no_match == 1
        with _connect() as conn:
            second = _links(conn)
        assert second == first, "rerun must not change any person link"
    finally:
        _cleanup([doi], [person_orcid, person_name])
