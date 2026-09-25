"""Disposable-DB coverage for internal-versus-external author classification.

PubDex links every author it can resolve, including external co-authors. Being
linked is not the same as being one of the organisation's own authors:
`biblio.authorships.is_internal_at_ingest` is documented as a snapshot of the
linked person's `app.people.person_kind`, and `biblio.publications_clean`
builds `internal_authors` from exactly that flag.

Ingestion used to set the flag from "a person was matched at all", so a linked
external co-author was reported as an internal author. These tests pin the
corrected behaviour end to end: the link survives, the classification does not
leak, and neither a rerun nor a curator overlay can promote an external person.
"""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.db.authorships import upsert_authorship
from people_pubs.db.people import person_is_internal

pytestmark = pytest.mark.integration


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _person(conn: psycopg.Connection, kind: str, name: str) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO app.people (person_kind, display_name) "
            "VALUES (%s, %s) RETURNING person_id",
            (kind, name),
        )
        return cur.fetchone()["person_id"]


def _publication(conn: psycopg.Connection, doi: str, title: str) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO biblio.publications (doi, title, year, venue, source_of_truth) "
            "VALUES (%s, %s, 2024, 'Journal of Synthetic Metadata', 'crossref') "
            "RETURNING pub_id",
            (doi, title),
        )
        return cur.fetchone()["pub_id"]


def _add_author(conn: psycopg.Connection, pub_id: int, position: int, name: str, person_id: int) -> None:
    upsert_authorship(
        conn=conn,
        publication_id=pub_id,
        author_position=position,
        author_name=name,
        author_orcid=None,
        person_id=person_id,
        affiliations=["Synthetic Institute for Collective Behaviour"],
        is_corresponding=False,
        order_tag="first" if position == 1 else "last",
        equal_contrib_tag=None,
        raw_author_json={"given": name.split()[0], "family": name.split()[-1]},
    )


def _authorship_rows(conn: psycopg.Connection, pub_id: int) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT author_position, author_name, person_id, is_internal_at_ingest "
            "FROM biblio.authorships_curated WHERE pub_id = %s ORDER BY author_position",
            (pub_id,),
        )
        return cur.fetchall()


def _internal_authors(conn: psycopg.Connection, pub_id: int) -> str | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT internal_authors FROM biblio.publications_clean WHERE pub_id = %s",
            (pub_id,),
        )
        row = cur.fetchone()
    return row["internal_authors"] if row else None


class _Fixture:
    """One publication with an internal and an external author."""

    def __init__(self, conn: psycopg.Connection) -> None:
        slug = uuid4().hex[:12]
        self.internal_name = f"Ada Example {slug}"
        self.external_name = f"Bert Example {slug}"
        self.internal_id = _person(conn, "internal", self.internal_name)
        self.external_id = _person(conn, "external", self.external_name)
        self.pub_id = _publication(
            conn, f"10.5555/internal-class-{slug}", f"Classification Fixture {slug}"
        )
        _add_author(conn, self.pub_id, 1, self.internal_name, self.internal_id)
        _add_author(conn, self.pub_id, 2, self.external_name, self.external_id)


def test_person_is_internal_reads_person_kind() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            internal_id = _person(conn, "internal", f"Internal {uuid4().hex[:8]}")
            external_id = _person(conn, "external", f"External {uuid4().hex[:8]}")
            assert person_is_internal(conn, internal_id) is True
            assert person_is_internal(conn, external_id) is False
            assert person_is_internal(conn, None) is False
            # An id that does not exist is not internal, and does not raise.
            assert person_is_internal(conn, 2_147_483_600) is False
        finally:
            conn.rollback()


def test_internal_author_is_linked_and_reported() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            fx = _Fixture(conn)
            rows = _authorship_rows(conn, fx.pub_id)
            internal_row = next(r for r in rows if r["author_name"] == fx.internal_name)
            assert internal_row["person_id"] == fx.internal_id
            assert internal_row["is_internal_at_ingest"] is True
            assert _internal_authors(conn, fx.pub_id) == fx.internal_name
        finally:
            conn.rollback()


def test_external_author_stays_linked_and_visible() -> None:
    """Classification must not cost us the authorship record itself."""
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            fx = _Fixture(conn)
            rows = _authorship_rows(conn, fx.pub_id)
            assert [r["author_name"] for r in rows] == [fx.internal_name, fx.external_name]
            external_row = next(r for r in rows if r["author_name"] == fx.external_name)
            assert external_row["person_id"] == fx.external_id, "the link must survive"
            assert external_row["author_position"] == 2, "source order must survive"
        finally:
            conn.rollback()


def test_matched_external_person_is_not_marked_internal() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            fx = _Fixture(conn)
            rows = _authorship_rows(conn, fx.pub_id)
            external_row = next(r for r in rows if r["author_name"] == fx.external_name)
            assert external_row["is_internal_at_ingest"] is False
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT is_internal_at_ingest FROM biblio.authorships "
                    "WHERE pub_id = %s AND person_id = %s",
                    (fx.pub_id, fx.external_id),
                )
                assert cur.fetchone()["is_internal_at_ingest"] is False
        finally:
            conn.rollback()


def test_external_author_is_absent_from_internal_authors() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            fx = _Fixture(conn)
            internal_authors = _internal_authors(conn, fx.pub_id)
            assert internal_authors == fx.internal_name
            assert fx.external_name not in (internal_authors or "")
        finally:
            conn.rollback()


def test_rerunning_ingestion_is_stable() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            fx = _Fixture(conn)
            before = _authorship_rows(conn, fx.pub_id)
            # Same payload again, as a rerun of the importer would send it.
            _add_author(conn, fx.pub_id, 1, fx.internal_name, fx.internal_id)
            _add_author(conn, fx.pub_id, 2, fx.external_name, fx.external_id)
            after = _authorship_rows(conn, fx.pub_id)
            assert after == before
            assert _internal_authors(conn, fx.pub_id) == fx.internal_name
        finally:
            conn.rollback()


def test_manual_override_does_not_promote_an_external_person() -> None:
    """A curator forcing a person records who they are, not that they are ours."""
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            fx = _Fixture(conn)
            # Force position 1 -- the internal author's row -- onto the external
            # person. The overlay must follow that person's classification.
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO biblio.authorship_manual_overrides "
                    "(pub_id, author_position, mode, person_id, reason) "
                    "VALUES (%s, 1, 'force_person', %s, 'classification regression')",
                    (fx.pub_id, fx.external_id),
                )
            rows = _authorship_rows(conn, fx.pub_id)
            forced = next(r for r in rows if r["author_position"] == 1)
            assert forced["person_id"] == fx.external_id, "the override must still apply"
            assert forced["is_internal_at_ingest"] is False
            # With the only internal row overridden away, nobody is internal.
            assert _internal_authors(conn, fx.pub_id) is None
        finally:
            conn.rollback()


def test_manual_override_onto_an_internal_person_still_counts() -> None:
    """The overlay follows person_kind in both directions, not just downwards."""
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            fx = _Fixture(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO biblio.authorship_manual_overrides "
                    "(pub_id, author_position, mode, person_id, reason) "
                    "VALUES (%s, 2, 'force_person', %s, 'classification regression')",
                    (fx.pub_id, fx.internal_id),
                )
            rows = _authorship_rows(conn, fx.pub_id)
            forced = next(r for r in rows if r["author_position"] == 2)
            assert forced["person_id"] == fx.internal_id
            assert forced["is_internal_at_ingest"] is True
        finally:
            conn.rollback()
