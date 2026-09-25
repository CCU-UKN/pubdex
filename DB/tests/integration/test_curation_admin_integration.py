"""End-to-end curation-admin writes against a disposable DB.

Drives people_pubs.curation_admin with a real psycopg connection pointed at
the disposable database — no fakes on the write path. Closes the scenario
DB/TESTING_STRATEGY.md left open: a curator correction must survive in the
biblio.authorships_curated overlay even after ingestion relinks the base row.

Synthetic fixture data only.
"""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

pytestmark = pytest.mark.integration


def _actor():
    from people_pubs.curation_admin import QueueActor

    return QueueActor(name="pytest-curation-admin", role="admin")


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _connect() -> psycopg.Connection:
    return psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True)


def _seed_person_pub(slug: str) -> tuple[int, int, str]:
    from people_pubs.db.publications import upsert_publication

    doi = f"10.5555/curadm-{slug}"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.people (display_name, person_kind) VALUES (%s, 'internal') RETURNING person_id",
            (f"Curation Fixture {slug}",),
        )
        person_id = int(cur.fetchone()["person_id"])
        pub_id = upsert_publication(
            conn,
            {"doi": doi, "title": f"curation fixture {slug}", "year": 2024,
             "raw_orcid_json": {"put-code": 7}},
        )
        cur.execute(
            """
            INSERT INTO biblio.authorships
              (pub_id, author_position, author_name, display_name_at_pub, person_id)
            VALUES (%s, 1, %s, %s, NULL)
            """,
            (pub_id, f"Curation Fixture {slug}", f"Curation Fixture {slug}"),
        )
    return person_id, pub_id, doi


def _table_exists(cur, qualified: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (qualified,))
    row = cur.fetchone()
    return bool(row and row["ok"])


def _cleanup(slug: str, person_ids: list[int], dois: list[str]) -> None:
    # publication_dedup_overrides and authorships cascade from publications;
    # queue/log/decision rows are selector-keyed and need explicit deletes.
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM biblio.curation_review_queue WHERE note LIKE %s",
            (f"pytest-{slug}%",),
        )
        cur.execute(
            "DELETE FROM biblio.manual_corrections_log WHERE note LIKE %s",
            (f"pytest-{slug}%",),
        )
        cur.execute(
            "DELETE FROM biblio.publication_dedup_decisions "
            "WHERE subject_doi_key = ANY(%s) OR winner_doi_key = ANY(%s)",
            (dois, dois),
        )
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
            if _table_exists(cur, "pii.people_pii"):
                cur.execute(
                    "DELETE FROM pii.people_pii WHERE person_id = ANY(%s)", (person_ids,)
                )
            cur.execute("DELETE FROM app.people WHERE person_id = ANY(%s)", (person_ids,))


def test_alias_and_email_writes_persist_with_audit_trail() -> None:
    from people_pubs import curation_admin

    slug = uuid4().hex[:12]
    person_id, _pub_id, doi = _seed_person_pub(slug)
    try:
        with _connect() as conn:
            result = curation_admin.add_person_alias(
                conn,
                person_id=person_id,
                alias_name=f"C. Fixture {slug}",
                actor=_actor(),
                note=f"pytest-{slug} alias",
            )
            assert result["updated"] is True
            profile = curation_admin.fetch_person_editor_profile(conn, person_id=person_id)
            assert any(a["alias_name"] == f"C. Fixture {slug}" for a in profile["aliases"])

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT alias_sources FROM app.person_name_aliases_manual "
                "WHERE person_id = %s AND alias_name = %s",
                (person_id, f"C. Fixture {slug}"),
            )
            alias_row = cur.fetchone()
            assert alias_row and "curation_admin.person_alias" in alias_row["alias_sources"]

            cur.execute(
                "SELECT status, review_type FROM biblio.curation_review_queue WHERE note = %s",
                (f"pytest-{slug} alias",),
            )
            queue_row = cur.fetchone()
            assert queue_row and queue_row["status"] == "applied"
            assert queue_row["review_type"] == "person_alias"

            cur.execute(
                "SELECT 1 FROM biblio.manual_corrections_log "
                "WHERE correction_type = 'person_alias.add' AND note = %s",
                (f"pytest-{slug} alias",),
            )
            assert cur.fetchone(), "alias write must land in the manual corrections log"

        with _connect() as conn:
            email_result = curation_admin.set_person_contact_email(
                conn,
                person_id=person_id,
                primary_email=f"fixture-{slug}@example.org",
                actor=_actor(),
                note=f"pytest-{slug} email",
            )
            assert email_result["primary_email"] == f"fixture-{slug}@example.org"

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT primary_email FROM pii.people_pii WHERE person_id = %s",
                (person_id,),
            )
            pii_row = cur.fetchone()
            assert pii_row and pii_row["primary_email"] == f"fixture-{slug}@example.org", (
                "email must be stored via the narrow pii.ensure_people_pii function"
            )
    finally:
        _cleanup(slug, [person_id], [doi])


def test_attach_detach_and_curated_overlay_survive_relink() -> None:
    from people_pubs import curation_admin

    slug = uuid4().hex[:12]
    person_id, pub_id, doi = _seed_person_pub(slug)
    try:
        with _connect() as conn:
            attach = curation_admin.attach_authorship(
                conn,
                pub_id=pub_id,
                author_position=1,
                author_name=None,
                person_id=person_id,
                actor=_actor(),
                note=f"pytest-{slug} attach",
            )
            assert attach["updated"] == 1

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT person_id FROM biblio.authorships WHERE pub_id = %s AND author_position = 1",
                (pub_id,),
            )
            assert cur.fetchone()["person_id"] == person_id
            cur.execute(
                "SELECT person_id FROM biblio.authorships_curated WHERE pub_id = %s AND author_position = 1",
                (pub_id,),
            )
            assert cur.fetchone()["person_id"] == person_id

        with _connect() as conn:
            detach = curation_admin.detach_authorship(
                conn,
                pub_id=pub_id,
                author_position=1,
                author_name=None,
                actor=_actor(),
                note=f"pytest-{slug} detach",
            )
            assert detach["updated"] is True

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT person_id FROM biblio.authorships WHERE pub_id = %s AND author_position = 1",
                (pub_id,),
            )
            assert cur.fetchone()["person_id"] is None
            cur.execute(
                """
                SELECT mode, active FROM biblio.authorship_manual_overrides
                WHERE pub_id = %s AND author_position = 1
                ORDER BY override_id DESC LIMIT 1
                """,
                (pub_id,),
            )
            override = cur.fetchone()
            assert override and override["mode"] == "force_null" and override["active"]

            # Reingest simulation: an importer relinks the base row. The
            # curated overlay must keep the curator's detach decision.
            cur.execute(
                "UPDATE biblio.authorships SET person_id = %s WHERE pub_id = %s AND author_position = 1",
                (person_id, pub_id),
            )
            cur.execute(
                "SELECT person_id, manual_override_mode FROM biblio.authorships_curated "
                "WHERE pub_id = %s AND author_position = 1",
                (pub_id,),
            )
            curated = cur.fetchone()
            assert curated["person_id"] is None, (
                "a curator detach must survive relinking via the authorships_curated overlay"
            )
            assert curated["manual_override_mode"] == "force_null"
    finally:
        _cleanup(slug, [person_id], [doi])


def test_dedup_confirm_and_keep_separate_write_durable_decisions() -> None:
    from people_pubs import curation_admin

    slug = uuid4().hex[:12]
    person_id, pub_id, doi = _seed_person_pub(slug)
    try:
        with _connect() as conn:
            confirm = curation_admin.confirm_publication_winner(
                conn,
                winner_pub_id=pub_id,
                actor=_actor(),
                note=f"pytest-{slug} confirm",
            )
            decision_id = confirm["decision_id"]
            assert decision_id, "confirm must create a durable dedup decision"

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT mode, subject_doi_key FROM biblio.publication_dedup_decisions "
                "WHERE decision_id = %s",
                (decision_id,),
            )
            decision = cur.fetchone()
            assert decision["mode"] == "confirm_winner"
            assert decision["subject_doi_key"] == doi

        with _connect() as conn:
            keep = curation_admin.keep_publications_separate(
                conn,
                pub_ids=[pub_id],
                actor=_actor(),
                note=f"pytest-{slug} keep",
            )
            queue_id = keep["queue_id"]

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT review_type, status FROM biblio.curation_review_queue WHERE queue_id = %s",
                (queue_id,),
            )
            queue_row = cur.fetchone()
            assert queue_row["review_type"] == "dedup.keep_separate"
            assert queue_row["status"] == "applied"
            cur.execute(
                "SELECT 1 FROM biblio.publication_dedup_decisions "
                "WHERE subject_doi_key = %s AND mode = 'keep_separate' AND active",
                (doi,),
            )
            assert cur.fetchone(), "keep-separate must write a durable selector-keyed decision"
    finally:
        _cleanup(slug, [person_id], [doi])


def test_explicit_caller_attribution_is_preserved() -> None:
    """A caller-supplied source/alias_source overrides the generic default.

    The defaults are `curation_admin.person_alias` and
    `curation_admin.authorship_attach`; an explicit value must reach the stored
    provenance unchanged, so an integration that has a real requester can still
    say who it was.
    """
    from people_pubs import curation_admin

    slug = uuid4().hex[:12]
    person_id, pub_id, doi = _seed_person_pub(slug)
    explicit_alias_source = f"roster_import.{slug}"
    explicit_attach_source = f"orcid_reconciliation.{slug}"
    try:
        with _connect() as conn:
            curation_admin.add_person_alias(
                conn,
                person_id=person_id,
                alias_name=f"E. Explicit {slug}",
                actor=_actor(),
                source=explicit_alias_source,
                note=f"pytest-{slug} explicit alias",
            )
            curation_admin.attach_authorship(
                conn,
                pub_id=pub_id,
                author_position=1,
                author_name=None,
                person_id=person_id,
                actor=_actor(),
                alias_source=explicit_attach_source,
                note=f"pytest-{slug} explicit attach",
            )

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT alias_sources FROM app.person_name_aliases_manual "
                "WHERE person_id = %s AND alias_name = %s",
                (person_id, f"E. Explicit {slug}"),
            )
            row = cur.fetchone()
            assert row and explicit_alias_source in row["alias_sources"], (
                "an explicit source must be stored, not replaced by the default"
            )
            assert "curation_admin.person_alias" not in row["alias_sources"]

            cur.execute(
                """
                SELECT source_file, evidence FROM biblio.authorship_manual_overrides
                WHERE pub_id = %s AND author_position = 1
                ORDER BY override_id DESC LIMIT 1
                """,
                (pub_id,),
            )
            override = cur.fetchone()
            assert override is not None, "the attach must record a manual override"
            assert override["evidence"].get("alias_source") == explicit_attach_source, (
                "an explicit alias_source must reach the stored provenance unchanged"
            )
            # source_file stays the generic operation token: it names the
            # surface that performed the write, not the caller's requester.
            assert override["source_file"] == "curation_admin"
    finally:
        _cleanup(slug, [person_id], [doi])
