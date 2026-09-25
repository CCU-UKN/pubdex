"""
people_pubs.db.authorships

Helpers to insert/update rows in biblio.authorships.
Used by ingestion scripts (ORCID, Crossref, OpenAlex and the other providers)
to keep author mappings consistent and idempotent.

Important ORCID semantics:
- paper_orcid must reflect only the source-provided ORCID from the paper/search
  payload.
- author_orcid may additionally be backfilled from a matched internal person.
"""

# people_pubs/db/authorships.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from people_pubs.db.people import person_is_internal
from people_pubs.utils.identifiers import normalize_orcid


def compute_order_tag(position: int, n_authors: int) -> str:
    """
    Classify author position into a small set of tags:
      - single  : only author
      - first   : first among >= 2
      - last    : last among >= 2
      - middle  : everyone else
    """
    if n_authors <= 1:
        return "single"
    if position == 1:
        return "first"
    if position == n_authors:
        return "last"
    return "middle"


def infer_equal_contrib_tag(author_obj: Dict[str, Any], order_tag: str) -> Optional[str]:
    """
    Very light-weight heuristic: look for 'equal' in various role/note fields.
    If found, classify as co-first / co-last / equal-contrib (generic).
    """
    texts: List[str] = []
    for key in ("note", "notes", "role", "roles", "contribution", "contributor-role"):
        v = author_obj.get(key)
        if isinstance(v, str):
            texts.append(v)
        elif isinstance(v, list):
            texts.extend(str(x) for x in v)
    joined = " ".join(texts).lower()
    if "equal" not in joined:
        return None
    if order_tag == "first" and "first" in joined:
        return "co-first"
    if order_tag == "last" and "last" in joined:
        return "co-last"
    return "equal-contrib"


def _normalize_orcid_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("path", "value", "uri", "url"):
            nested = _normalize_orcid_value(value.get(key))
            if nested:
                return nested
        return None
    if not isinstance(value, str):
        return None
    return normalize_orcid(value)


def _extract_source_orcid(raw_author_json: Dict[str, Any]) -> Optional[str]:
    if not isinstance(raw_author_json, dict):
        return None
    for key in ("paper_orcid", "orcid", "ORCID", "contributor-orcid"):
        extracted = _normalize_orcid_value(raw_author_json.get(key))
        if extracted:
            return extracted
    for value in raw_author_json.values():
        if isinstance(value, dict):
            extracted = _extract_source_orcid(value)
            if extracted:
                return extracted
    return None


def upsert_authorship(
    conn: psycopg.Connection,
    publication_id: int,
    author_position: int,
    author_name: str,
    author_orcid: Optional[str],
    person_id: Optional[int],
    affiliations: List[str],
    is_corresponding: bool,
    order_tag: str,
    equal_contrib_tag: Optional[str],
    raw_author_json: Dict[str, Any],
    dry_run: bool = False,
    paper_orcid: Optional[str] = None,
) -> None:
    """
    Insert/update one row in biblio.authorships.

    Behaviour mirrors update_publications_from_orcid.upsert_authorship:
    - If person_id is present, we prefer to key on (pub_id, person_id) to keep
      a stable mapping regardless of position changes.
    - Otherwise, key on (pub_id, author_position).
    - `paper_orcid` is sourced only from the paper/search payload.
    - `author_orcid` can carry a locally resolved/backfilled ORCID.
    - `is_internal_at_ingest` reflects the linked person's `person_kind`, so
      linking an external co-author does not make them an internal author.
    """
    if dry_run:
        logging.info(
            "  [dry-run] would upsert authorship: pub_id=%s pos=%s name=%r person_id=%r paper_orcid=%r author_orcid=%r",
            publication_id,
            author_position,
            author_name,
            person_id,
            paper_orcid,
            author_orcid,
        )
        return

    # A matched person is not necessarily one of ours: external co-authors
    # are linked too. The flag mirrors app.people.person_kind, which is what
    # the column documents and what the canonical author list reads.
    is_internal = person_is_internal(conn, person_id)
    author_name_clean = author_name.strip() if isinstance(author_name, str) else None
    if author_name_clean == "":
        author_name_clean = None
    author_orcid_clean = author_orcid.strip() if isinstance(author_orcid, str) else None
    if author_orcid_clean == "":
        author_orcid_clean = None
    paper_orcid_clean = _normalize_orcid_value(paper_orcid)
    if paper_orcid_clean is None:
        paper_orcid_clean = _extract_source_orcid(raw_author_json)
    affiliations_param = affiliations if affiliations else None
    paper_affiliation_str = "; ".join(affiliations) if affiliations else None
    raw_author_param = raw_author_json if raw_author_json else None

    with conn.cursor(row_factory=dict_row) as cur:
        existing_row = None
        if person_id is not None:
            cur.execute(
                """
                SELECT *
                FROM biblio.authorships
                WHERE pub_id = %s AND person_id = %s
                """,
                (publication_id, person_id),
            )
            existing_row = cur.fetchone()

        if existing_row:
            # Keep the existing author_position as canonical to avoid shuffling
            existing_pos = existing_row.get("author_position") or author_position

            cur.execute(
                """
                UPDATE biblio.authorships
                SET
                    author_position          = %s,
                    display_name_at_pub      = COALESCE(%s, display_name_at_pub),
                    paper_affiliation        = COALESCE(%s, paper_affiliation),
                    paper_orcid              = COALESCE(%s, paper_orcid),
                    is_corresponding         = %s,
                    is_internal_at_ingest    = %s,
                    order_tag                = %s,
                    equal_contrib_tag        = %s,
                    author_name              = COALESCE(%s, author_name),
                    author_orcid             = COALESCE(%s, author_orcid),
                    affiliations             = COALESCE(%s, affiliations),
                    raw_crossref_author_json = COALESCE(%s::jsonb, raw_crossref_author_json),
                    updated_at               = now()
                WHERE pub_id = %s AND person_id = %s
                """,
                (
                    existing_pos,
                    author_name_clean,
                    paper_affiliation_str,
                    paper_orcid_clean,
                    is_corresponding,
                    is_internal,
                    order_tag,
                    equal_contrib_tag,
                    author_name_clean,
                    author_orcid_clean,
                    affiliations_param,
                    Jsonb(raw_author_param) if raw_author_param is not None else None,
                    publication_id,
                    person_id,
                ),
            )
        else:
            if person_id is not None:
                cur.execute(
                    """
                    SELECT person_id, author_position
                    FROM biblio.authorships
                    WHERE pub_id = %s AND author_position = %s
                    """,
                    (publication_id, author_position),
                )
                row_by_pos = cur.fetchone()
                if row_by_pos and row_by_pos.get("person_id") is None:
                    existing_pos = row_by_pos.get("author_position") or author_position
                    cur.execute(
                        """
                        UPDATE biblio.authorships
                        SET
                            person_id               = %s,
                            author_position          = %s,
                            display_name_at_pub      = COALESCE(%s, display_name_at_pub),
                            paper_affiliation        = COALESCE(%s, paper_affiliation),
                            paper_orcid              = COALESCE(%s, paper_orcid),
                            is_corresponding         = %s,
                            is_internal_at_ingest    = %s,
                            order_tag                = %s,
                            equal_contrib_tag        = %s,
                            author_name              = COALESCE(%s, author_name),
                            author_orcid             = COALESCE(%s, author_orcid),
                            affiliations             = COALESCE(%s, affiliations),
                            raw_crossref_author_json = COALESCE(%s::jsonb, raw_crossref_author_json),
                            updated_at               = now()
                        WHERE pub_id = %s AND author_position = %s AND person_id IS NULL
                        """,
                        (
                            person_id,
                            existing_pos,
                            author_name_clean,
                            paper_affiliation_str,
                            paper_orcid_clean,
                            is_corresponding,
                            is_internal,
                            order_tag,
                            equal_contrib_tag,
                            author_name_clean,
                            author_orcid_clean,
                            affiliations_param,
                            Jsonb(raw_author_param) if raw_author_param is not None else None,
                            publication_id,
                            author_position,
                        ),
                    )
                    return

            # No existing (pub_id, person_id) row; safe to insert. We still
            # guard on (pub_id, author_position) so re-runs update in place.
            cur.execute(
                """
                INSERT INTO biblio.authorships (
                    pub_id,
                    person_id,
                    author_position,
                    display_name_at_pub,
                    paper_affiliation,
                    paper_orcid,
                    is_corresponding,
                    is_internal_at_ingest,
                    order_tag,
                    equal_contrib_tag,
                    author_name,
                    author_orcid,
                    affiliations,
                    raw_crossref_author_json
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (pub_id, author_position) DO UPDATE
                SET
                    person_id               = COALESCE(biblio.authorships.person_id, EXCLUDED.person_id),
                    author_position          = EXCLUDED.author_position,
                    display_name_at_pub      = COALESCE(EXCLUDED.display_name_at_pub, biblio.authorships.display_name_at_pub),
                    paper_affiliation        = COALESCE(EXCLUDED.paper_affiliation, biblio.authorships.paper_affiliation),
                    paper_orcid              = COALESCE(EXCLUDED.paper_orcid, biblio.authorships.paper_orcid),
                    is_corresponding         = EXCLUDED.is_corresponding,
                    is_internal_at_ingest    = EXCLUDED.is_internal_at_ingest,
                    order_tag                = EXCLUDED.order_tag,
                    equal_contrib_tag        = EXCLUDED.equal_contrib_tag,
                    author_name              = COALESCE(EXCLUDED.author_name, biblio.authorships.author_name),
                    author_orcid             = COALESCE(EXCLUDED.author_orcid, biblio.authorships.author_orcid),
                    affiliations             = COALESCE(EXCLUDED.affiliations, biblio.authorships.affiliations),
                    raw_crossref_author_json = COALESCE(EXCLUDED.raw_crossref_author_json, biblio.authorships.raw_crossref_author_json),
                    updated_at               = now()
                """,
                (
                    publication_id,
                    person_id,
                    author_position,
                    author_name_clean,
                    paper_affiliation_str,
                    paper_orcid_clean,
                    is_corresponding,
                    is_internal,
                    order_tag,
                    equal_contrib_tag,
                    author_name_clean,
                    author_orcid_clean,
                    affiliations_param,
                    Jsonb(raw_author_param) if raw_author_param is not None else None,
                ),
            )


def upsert_author_email(
    conn: psycopg.Connection,
    publication_id: int,
    person_id: Optional[int],
    author_position: int,
    email: str,
    dry_run: bool = False,
) -> None:
    """
    Optional helper to populate pii.publication_author_emails when Crossref has e-mails.
    You can safely noop this if you don't have that table.
    """
    if not email:
        return
    if person_id is None:
        logging.debug(
            "  skipping author email without matched person_id: pub_id=%s pos=%s email=%r",
            publication_id,
            author_position,
            email,
        )
        return
    if dry_run:
        logging.info(
            "  [dry-run] would upsert author email: pub_id=%s person_id=%r pos=%s email=%r",
            publication_id,
            person_id,
            author_position,
            email,
        )
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pii.upsert_publication_author_email(%s, %s, %s::citext, FALSE)
            """,
            (publication_id, person_id, email),
        )
