"""Disposable-DB results of the publication read views.

Pins down what the views return for synthetic records, not how the SQL is
written:

- type exclusion in biblio.publication_dedup_memberships (and therefore
  biblio.publications_clean): Crossref type evidence decides when present,
  OpenAlex type evidence only when Crossref carries no type;
- publication dates in biblio.publications_with_pub_date: source precedence,
  day/month/year precision and the fallbacks for missing dates;
- the identifier kinds of app.person_key_type on a fresh bootstrap.

Every test inserts its own uniquely titled rows inside a transaction and rolls
back, so the views see each row as its own canonical publication.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, Optional
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Json


pytestmark = pytest.mark.integration


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _insert(
    conn: psycopg.Connection,
    raw_json: dict[str, Any],
    *,
    year: Optional[int] = 2024,
) -> int:
    slug = uuid4().hex[:12]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO biblio.publications
              (doi, title, year, source_of_truth, raw_json, is_preprint)
            VALUES (%s, %s, %s, %s, %s, false)
            RETURNING pub_id
            """,
            (
                f"10.5555/views-{slug}",
                f"publication views integration {slug}",
                year,
                "+".join(sorted(raw_json)) or "manual",
                Json(raw_json),
            ),
        )
        return int(cur.fetchone()["pub_id"])


def _present(conn: psycopg.Connection, relation: str, pub_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {relation} WHERE pub_id = %s", (pub_id,))
        return int(cur.fetchone()["n"]) == 1


# (raw_json, retained)
TYPE_CASES = {
    "crossref-excluded-type": ({"crossref": {"type": "dataset"}}, False),
    "crossref-ordinary-article": ({"crossref": {"type": "journal-article"}}, True),
    "openalex-excluded-type-without-crossref": ({"openalex": {"type": "paratext"}}, False),
    "openalex-excluded-type-crossref-without-type": (
        {"crossref": {"DOI": "10.5555/untyped"}, "openalex": {"type": "paratext"}},
        False,
    ),
    "openalex-ordinary-article": ({"openalex": {"type": "article"}}, True),
    "crossref-article-overrides-openalex-exclusion": (
        {"crossref": {"type": "journal-article"}, "openalex": {"type": "paratext"}},
        True,
    ),
    "crossref-exclusion-overrides-openalex-article": (
        {"crossref": {"type": "dataset"}, "openalex": {"type": "article"}},
        False,
    ),
    "no-type-evidence": ({"orcid": {"put-code": 1}}, True),
}


@pytest.mark.parametrize("case", sorted(TYPE_CASES))
def test_type_exclusion(case: str) -> None:
    raw_json, retained = TYPE_CASES[case]
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            pub_id = _insert(conn, raw_json)
            assert _present(conn, "biblio.publication_dedup_memberships", pub_id) is retained
            assert _present(conn, "biblio.publications_clean", pub_id) is retained
        finally:
            conn.rollback()


_ORCID_MARCH_9 = {"publication-date": {"month": {"value": "03"}, "day": {"value": "09"}}}
_OPENALEX_NOV_23 = {"publication_date": "2024-11-23"}

# (year, raw_json, expected pub_date, expected precision)
DATE_CASES = {
    "crossref-issued-beats-orcid-and-openalex": (
        2024,
        {
            "crossref": {"issued": {"date-parts": [[2024, 5, 17]]}},
            "orcid": _ORCID_MARCH_9,
            "openalex": _OPENALEX_NOV_23,
        },
        date(2024, 5, 17),
        "day",
    ),
    "crossref-published-when-issued-is-absent": (
        2024,
        {
            "crossref": {
                "published": {"date-parts": [[2024, 6, 2]]},
                "published-print": {"date-parts": [[2024, 8, 30]]},
            },
            "openalex": _OPENALEX_NOV_23,
        },
        date(2024, 6, 2),
        "day",
    ),
    "crossref-published-print-beats-orcid": (
        2024,
        {
            "crossref": {"published-print": {"date-parts": [[2024, 8, 30]]}},
            "orcid": _ORCID_MARCH_9,
        },
        date(2024, 8, 30),
        "day",
    ),
    "orcid-beats-openalex-without-crossref-dates": (
        2024,
        {"crossref": {"type": "journal-article"}, "orcid": _ORCID_MARCH_9, "openalex": _OPENALEX_NOV_23},
        date(2024, 3, 9),
        "day",
    ),
    "openalex-alone": (2024, {"openalex": _OPENALEX_NOV_23}, date(2024, 11, 23), "day"),
    # Month and day are resolved independently, each in the same source order.
    "month-and-day-resolved-independently": (
        2024,
        {"crossref": {"issued": {"date-parts": [[2024, 5]]}}, "openalex": _OPENALEX_NOV_23},
        date(2024, 5, 23),
        "day",
    ),
    "month-precision-from-crossref": (
        2024,
        {"crossref": {"issued": {"date-parts": [[2024, 5]]}}},
        date(2024, 5, 1),
        "month",
    ),
    "month-precision-from-orcid": (
        2024,
        {"orcid": {"publication-date": {"month": {"value": "07"}}}},
        date(2024, 7, 1),
        "month",
    ),
    "year-precision-when-no-payload-has-a-month": (
        2021,
        {"crossref": {"issued": {"date-parts": [[2021]]}}},
        date(2021, 1, 1),
        "year",
    ),
    "year-precision-without-payload-dates": (
        2021,
        {"orcid": {"put-code": 1}},
        date(2021, 1, 1),
        "year",
    ),
    "day-clamped-to-end-of-month": (
        2023,
        {"crossref": {"issued": {"date-parts": [[2023, 2, 31]]}}},
        date(2023, 2, 28),
        "day",
    ),
    "no-year-no-date": (
        None,
        {"openalex": _OPENALEX_NOV_23},
        None,
        None,
    ),
}


@pytest.mark.parametrize("case", sorted(DATE_CASES))
def test_publication_date(case: str) -> None:
    year, raw_json, expected_date, expected_precision = DATE_CASES[case]
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.execute("BEGIN")
        try:
            pub_id = _insert(conn, raw_json, year=year)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pub_date, pub_date_precision
                    FROM biblio.publications_with_pub_date
                    WHERE pub_id = %s
                    """,
                    (pub_id,),
                )
                rows = cur.fetchall()
            assert len(rows) == 1, f"expected the publication in the view, got {len(rows)} row(s)"
            assert rows[0]["pub_date"] == expected_date
            assert rows[0]["pub_date_precision"] == expected_precision
        finally:
            conn.rollback()


def test_person_key_type_values() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.enumlabel
                FROM pg_enum e
                JOIN pg_type t ON t.oid = e.enumtypid
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname = 'app' AND t.typname = 'person_key_type'
                ORDER BY e.enumsortorder
                """
            )
            labels = [row["enumlabel"] for row in cur.fetchall()]
        conn.rollback()
    assert labels == ["orcid", "email", "gs_profile", "crossref", "name_affil_key"]
