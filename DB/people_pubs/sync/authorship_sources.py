#!/usr/bin/env python3
"""
people_pubs.sync.authorship_sources

Inspect which source JSON contains author/affiliation data for a publication.

Typical usage:
  python -m people_pubs.sync.authorship_sources --pub-id 5184
  python -m people_pubs.sync.authorship_sources --doi 10.1038/s41598-021-01441-w

What it does:
- Loads the publication raw_json (crossref/openalex/datacite/semantic/dblp).
- Counts how many authors/affiliations are present per source.
- Summarizes existing biblio.authorships rows.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg

from people_pubs.config import DEFAULT_DSN
from people_pubs.db.connection import db_conn
from people_pubs.services.crossref_client import normalize_doi
from people_pubs.sync.crossref_backfill import find_pub_by_doi, PubRow
from people_pubs.services.datacite_client import count_authors_with_affiliations as datacite_count
from people_pubs.services.semantic_scholar_client import count_authors_with_affiliations as semantic_count
from people_pubs.sync.scaffold import _row_get


LOGGER = logging.getLogger("people_pubs.authorship_sources")


def _load_pub_by_id(conn: psycopg.Connection, pub_id: int) -> Optional[PubRow]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pub_id, doi, title, year, venue, source_of_truth, raw_json, is_preprint
            FROM biblio.publications
            WHERE pub_id = %s
            """,
            (pub_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return PubRow(
        pub_id=int(_row_get(row, "pub_id", 0)),
        doi=_row_get(row, "doi", 1),
        title=_row_get(row, "title", 2),
        year=_row_get(row, "year", 3),
        venue=_row_get(row, "venue", 4),
        source_of_truth=_row_get(row, "source_of_truth", 5),
        raw_json=_row_get(row, "raw_json", 6),
        is_preprint=bool(_row_get(row, "is_preprint", 7)),
    )


def _summarize_crossref(raw_json: Any) -> Tuple[int, int]:
    if not isinstance(raw_json, dict):
        return 0, 0
    cr = raw_json.get("crossref")
    if not isinstance(cr, dict):
        return 0, 0
    authors = cr.get("author") or []
    if not isinstance(authors, list):
        return 0, 0
    author_count = 0
    with_aff = 0
    for a in authors:
        if not isinstance(a, dict):
            continue
        author_count += 1
        aff_raw = a.get("affiliation") or []
        if isinstance(aff_raw, list) and any(
            (isinstance(x, dict) and (x.get("name") or "").strip())
            or (isinstance(x, str) and x.strip())
            for x in aff_raw
        ):
            with_aff += 1
    return author_count, with_aff


def _summarize_openalex(raw_json: Any) -> Tuple[int, int]:
    if not isinstance(raw_json, dict):
        return 0, 0
    oa = raw_json.get("openalex")
    if not isinstance(oa, dict):
        return 0, 0
    authorships = oa.get("authorships") or []
    if not isinstance(authorships, list):
        return 0, 0
    author_count = 0
    with_aff = 0
    for auth in authorships:
        if not isinstance(auth, dict):
            continue
        author_count += 1
        raw_affs = auth.get("raw_affiliation_strings") or []
        if isinstance(raw_affs, list) and any(isinstance(x, str) and x.strip() for x in raw_affs):
            with_aff += 1
            continue
        insts = auth.get("institutions") or []
        if isinstance(insts, list) and any(
            isinstance(i, dict) and (i.get("display_name") or "").strip() for i in insts
        ):
            with_aff += 1
    return author_count, with_aff


def _summarize_dblp(raw_json: Any) -> Tuple[int, int]:
    if not isinstance(raw_json, dict):
        return 0, 0
    dblp = raw_json.get("dblp")
    if not isinstance(dblp, dict):
        return 0, 0
    authors = (dblp.get("authors") or {}).get("author")
    if not authors:
        return 0, 0
    if isinstance(authors, list):
        author_count = len(authors)
    else:
        author_count = 1
    return author_count, 0


def _summarize_datacite(raw_json: Any) -> Tuple[int, int]:
    if not isinstance(raw_json, dict):
        return 0, 0
    payload = raw_json.get("datacite")
    if not isinstance(payload, dict):
        return 0, 0
    return datacite_count(payload)


def _summarize_semantic(raw_json: Any) -> Tuple[int, int]:
    if not isinstance(raw_json, dict):
        return 0, 0
    payload = raw_json.get("semantic")
    if not isinstance(payload, dict):
        return 0, 0
    return semantic_count(payload)


def _summarize_authorship_rows(
    conn: psycopg.Connection,
    pub_id: int,
) -> Tuple[int, int, int]:
    with conn.cursor() as cur:
        # Deliberately raw (not authorships_curated): this diagnostic compares
        # stored ingest rows against source payloads, so overrides must stay visible.
        cur.execute(
            """
            SELECT author_name, person_id, affiliations
            FROM biblio.authorships
            WHERE pub_id = %s
            """,
            (pub_id,),
        )
        rows = cur.fetchall()
    total = 0
    with_person = 0
    with_aff = 0
    for row in rows:
        total += 1
        person_id = _row_get(row, "person_id", 1)
        if person_id is not None:
            with_person += 1
        affiliations = _row_get(row, "affiliations", 2)
        if isinstance(affiliations, list) and affiliations:
            with_aff += 1
        elif isinstance(affiliations, str) and affiliations.strip():
            with_aff += 1
    return total, with_person, with_aff


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Inspect author/affiliation data by source for a publication.")
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (defaults to people_pubs.config.DEFAULT_DSN)")
    p.add_argument("--pub-id", type=int, default=None, help="Publication ID to inspect.")
    p.add_argument("--doi", default=None, help="DOI to inspect.")
    p.add_argument("--debug", action="store_true", help="Enable debug logging.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")

    if not args.pub_id and not args.doi:
        raise SystemExit("Provide --pub-id or --doi.")

    with db_conn(args.dsn) as conn:
        pub = None
        if args.pub_id:
            pub = _load_pub_by_id(conn, int(args.pub_id))
        elif args.doi:
            pub = find_pub_by_doi(conn, normalize_doi(args.doi) or args.doi)

        if not pub:
            raise SystemExit("Publication not found.")

        total_auth, with_person, with_aff = _summarize_authorship_rows(conn, pub.pub_id)
        cr_auth, cr_aff = _summarize_crossref(pub.raw_json)
        oa_auth, oa_aff = _summarize_openalex(pub.raw_json)
        dc_auth, dc_aff = _summarize_datacite(pub.raw_json)
        ss_auth, ss_aff = _summarize_semantic(pub.raw_json)
        dblp_auth, dblp_aff = _summarize_dblp(pub.raw_json)

        LOGGER.info("Publication: pub_id=%s doi=%r title=%r", pub.pub_id, pub.doi, pub.title)
        LOGGER.info(
            "Authorships table: rows=%s with_person_id=%s with_affiliations=%s",
            total_auth,
            with_person,
            with_aff,
        )
        LOGGER.info("Crossref raw_json: authors=%s with_affiliations=%s", cr_auth, cr_aff)
        LOGGER.info("OpenAlex raw_json: authors=%s with_affiliations=%s", oa_auth, oa_aff)
        LOGGER.info("DataCite raw_json: authors=%s with_affiliations=%s", dc_auth, dc_aff)
        LOGGER.info("Semantic Scholar raw_json: authors=%s with_affiliations=%s", ss_auth, ss_aff)
        LOGGER.info("DBLP raw_json: authors=%s with_affiliations=%s", dblp_auth, dblp_aff)


if __name__ == "__main__":
    main()
