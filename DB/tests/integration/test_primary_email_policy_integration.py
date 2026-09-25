"""Disposable-DB coverage for the SQL half of the primary-email rule.

pii.ensure_people_pii and pii.merge_people_verified_emails must make the same
domain-neutral choice as people_pubs.db.people.pick_primary_email: keep an
explicitly set primary, honour an explicitly supplied one where the function
accepts it, and otherwise take the first address in stable input order. No
organisation, domain or top-level domain may be preferred, and the fallback
must not silently become lexical order.
"""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

pytestmark = pytest.mark.integration


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _person(conn: psycopg.Connection) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO app.people (person_kind, display_name) "
            "VALUES ('internal', %s) RETURNING person_id",
            (f"Email Policy {uuid4().hex[:8]}",),
        )
        return cur.fetchone()["person_id"]


def _ensure(conn: psycopg.Connection, person_id: int, primary, emails) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        # CITEXT[] has no psycopg array loader, so it would come back as a raw
        # array literal; cast to TEXT[] to compare real values.
        cur.execute(
            "SELECT action, primary_email::TEXT AS primary_email, emails::TEXT[] AS emails "
            "FROM pii.ensure_people_pii(%s, %s::CITEXT, %s::CITEXT[])",
            (person_id, primary, emails),
        )
        return cur.fetchone()


def _merge_verified(conn: psycopg.Connection, person_id: int, emails) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT row_found, primary_email::TEXT AS primary_email, "
            "emails::TEXT[] AS emails "
            "FROM pii.merge_people_verified_emails(%s, %s::CITEXT[])",
            (person_id, emails),
        )
        return cur.fetchone()


def test_explicitly_supplied_primary_is_honoured_on_insert() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            person_id = _person(conn)
            row = _ensure(
                conn,
                person_id,
                "chosen@example.org",
                ["first@example.org", "second@example.net"],
            )
            assert row["action"] == "inserted"
            assert row["primary_email"] == "chosen@example.org"
        finally:
            conn.rollback()


def test_first_address_wins_when_no_primary_is_supplied() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            person_id = _person(conn)
            row = _ensure(conn, person_id, None, ["first@example.org", "second@example.net"])
            assert row["primary_email"] == "first@example.org"
        finally:
            conn.rollback()


def test_fallback_is_input_order_not_lexical_order() -> None:
    """A later-sorting address supplied first must still win."""
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            person_id = _person(conn)
            row = _ensure(conn, person_id, None, ["zeta@example.org", "alpha@example.net"])
            assert row["primary_email"] == "zeta@example.org"
            assert list(row["emails"]) == ["zeta@example.org", "alpha@example.net"]
        finally:
            conn.rollback()


@pytest.mark.parametrize(
    "emails",
    [
        ["a@example.org", "b@example.net", "c@example.com"],
        ["a@example.net", "b@example.com", "c@example.org"],
        ["a@example.com", "b@example.org", "c@example.net"],
    ],
)
def test_no_domain_or_tld_receives_special_treatment(emails: list) -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            person_id = _person(conn)
            row = _ensure(conn, person_id, None, emails)
            assert row["primary_email"] == emails[0]
        finally:
            conn.rollback()


def test_reversing_the_input_reverses_the_choice() -> None:
    pair = ["one@example.org", "two@example.net"]
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            forward = _ensure(conn, _person(conn), None, pair)
            backward = _ensure(conn, _person(conn), None, list(reversed(pair)))
            assert forward["primary_email"] == "one@example.org"
            assert backward["primary_email"] == "two@example.net"
        finally:
            conn.rollback()


def test_existing_primary_survives_an_update() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            person_id = _person(conn)
            _ensure(conn, person_id, "kept@example.org", ["kept@example.org"])
            row = _ensure(conn, person_id, "ignored@example.net", ["later@example.com"])
            assert row["action"] == "updated"
            assert row["primary_email"] == "kept@example.org"
            # The supplied address is still recorded as one of the person's
            # addresses -- it simply does not take the primary role away from
            # the one already chosen. Existing addresses keep their position,
            # incoming ones follow in the order they were supplied.
            assert list(row["emails"]) == [
                "kept@example.org",
                "later@example.com",
                "ignored@example.net",
            ]
        finally:
            conn.rollback()


def test_supplied_primary_is_used_when_no_primary_is_stored_yet() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            person_id = _person(conn)
            _ensure(conn, person_id, None, [])
            row = _ensure(conn, person_id, "supplied@example.org", ["other@example.net"])
            assert row["action"] == "updated"
            assert row["primary_email"] == "supplied@example.org"
        finally:
            conn.rollback()


@pytest.mark.parametrize("placeholder", ["primary_email", "email", "none", "n/a", "  "])
def test_placeholder_primary_is_ignored(placeholder: str) -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            person_id = _person(conn)
            row = _ensure(
                conn, person_id, placeholder, ["first@example.org", "second@example.net"]
            )
            assert row["primary_email"] == "first@example.org"
        finally:
            conn.rollback()


def test_case_variants_do_not_alter_the_result() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            person_id = _person(conn)
            row = _ensure(
                conn,
                person_id,
                None,
                ["First@Example.org", "first@example.org", "b@example.net"],
            )
            assert list(row["emails"]) == ["First@Example.org", "b@example.net"]
            assert row["primary_email"] == "First@Example.org"
        finally:
            conn.rollback()


def test_merge_verified_emails_keeps_an_existing_primary() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            person_id = _person(conn)
            _ensure(conn, person_id, "kept@example.org", ["kept@example.org"])
            row = _merge_verified(conn, person_id, ["verified@example.net"])
            assert row["row_found"] is True
            assert row["primary_email"] == "kept@example.org"
            assert list(row["emails"]) == ["kept@example.org", "verified@example.net"]
        finally:
            conn.rollback()


def test_merge_verified_emails_falls_back_to_the_first_merged_address() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            person_id = _person(conn)
            _ensure(conn, person_id, None, [])
            row = _merge_verified(
                conn, person_id, ["zeta@example.org", "alpha@example.net"]
            )
            assert row["primary_email"] == "zeta@example.org"
        finally:
            conn.rollback()


def test_merge_verified_emails_reports_a_missing_row() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            row = _merge_verified(conn, _person(conn), ["nobody@example.org"])
            assert row["row_found"] is False
            assert row["primary_email"] is None
        finally:
            conn.rollback()
