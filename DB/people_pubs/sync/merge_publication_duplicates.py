"""
people_pubs/sync/merge_publication_duplicates.py
Utilities to merge duplicate publications.

Primary use-case:
- During Crossref backfill, a publication that originally came from ORCID or
  Scholar may be assigned a DOI that already exists in biblio.publications.
  Because biblio.publications has a UNIQUE (doi) constraint, we need a
  deterministic merge routine.

This module focuses on *safe* automatic merges:
- Merge when DOI keys match (after normalization).
- Do NOT automatically merge title duplicates with multiple distinct DOIs unless
  explicitly enabled.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from people_pubs.config import PEOPLE_DB_DSN
from people_pubs.dedupe_policy import choose_canonical_publication, publication_candidate_from
from people_pubs.db.connection import db_conn
from people_pubs.db.publications import (
    merge_publication_provenance,
    merge_publication_source_provenance,
)
from people_pubs.services.crossref_client import normalize_doi

log = logging.getLogger(__name__)


def _doi_key_sql(col: str = "doi") -> str:
    """SQL DOI key expression that roughly matches normalize_doi()."""
    return (
        "lower("
        "regexp_replace("
        "regexp_replace(trim({col}), '^\\\\s*doi:\\\\s*', '', 'i'),"
        "'^\\\\s*https?://(?:dx\\\\.)?doi\\\\.org/', '', 'i'"
        ")"
        ")"
    ).format(col=col)


@dataclass
class PubRow:
    pub_id: int
    doi: Optional[str]
    year: Optional[int]
    venue: Optional[str]
    source_of_truth: str
    is_preprint: bool
    raw_json: Any


def _choose_canonical(rows: list[PubRow]) -> int:
    choice = choose_canonical_publication(publication_candidate_from(r) for r in rows)
    log.info(
        "canonical choice: winner=%s losers=%s reason=%s score=%s",
        choice.winner_pub_id,
        ",".join(str(pid) for pid in choice.loser_pub_ids),
        choice.reason,
        choice.winner_score,
    )
    return choice.winner_pub_id


def _load_pub_rows_by_ids(conn: psycopg.Connection, pub_ids: list[int]) -> list[PubRow]:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT pub_id, doi, year, venue, source_of_truth, is_preprint, raw_json
        FROM biblio.publications
        WHERE pub_id = ANY(%s)
        """,
        (pub_ids,),
    )
    out: list[PubRow] = []
    for r in cur.fetchall():
        out.append(
            PubRow(
                pub_id=int(r["pub_id"]),
                doi=r.get("doi"),
                year=r.get("year"),
                venue=r.get("venue"),
                source_of_truth=r.get("source_of_truth") or "orcid",
                is_preprint=bool(r.get("is_preprint")),
                raw_json=r.get("raw_json"),
            )
        )
    return out


def merge_publications(conn: psycopg.Connection, keep_pub_id: int, drop_pub_id: int, *, dry_run: bool) -> None:
    """Move child rows from drop_pub_id to keep_pub_id, then delete drop."""
    if keep_pub_id == drop_pub_id:
        return

    log.info("Merging pub_id=%s <- pub_id=%s", keep_pub_id, drop_pub_id)

    cur = conn.cursor(row_factory=dict_row)

    # publication_author_emails: handled by a SECURITY DEFINER function so
    # routine app_writer jobs do not need direct table privileges on pii.*.
    if not dry_run:
        cur.execute("SELECT pii.move_publication_author_emails(%s, %s)", (keep_pub_id, drop_pub_id))

        # authorships: insert then delete
        cur.execute(
            """
            INSERT INTO biblio.authorships (
                pub_id, person_id, author_position, display_name_at_pub,
                paper_affiliation, paper_orcid, is_corresponding,
                is_internal_at_ingest, order_tag, equal_contrib_tag,
                author_name, author_orcid, affiliations,
                raw_crossref_author_json, created_at, updated_at
            )
            SELECT
                %s, person_id, author_position, display_name_at_pub,
                paper_affiliation,
                NULLIF(trim(paper_orcid), '')::text,
                is_corresponding,
                is_internal_at_ingest, order_tag, equal_contrib_tag,
                author_name, author_orcid, affiliations,
                raw_crossref_author_json, created_at, now()
            FROM biblio.authorships
            WHERE pub_id = %s
            ON CONFLICT (pub_id, author_position) DO NOTHING
            """,
            (keep_pub_id, drop_pub_id),
        )
        cur.execute("DELETE FROM biblio.authorships WHERE pub_id = %s", (drop_pub_id,))

        # publication metadata merge (conservative)
        cur.execute(
            """
            SELECT pub_id, doi, year, venue, source_of_truth, raw_json,
                   source_provenance, is_preprint
            FROM biblio.publications
            WHERE pub_id IN (%s, %s)
            """,
            (keep_pub_id, drop_pub_id),
        )
        pubs = {int(r["pub_id"]): r for r in cur.fetchall()}
        keep = pubs[keep_pub_id]
        drop = pubs[drop_pub_id]

        keep_doi = normalize_doi(keep.get("doi"))
        drop_doi = normalize_doi(drop.get("doi"))
        merged_doi = keep_doi or drop_doi

        merged_sot, merged_raw = merge_publication_provenance(
            keep.get("source_of_truth"),
            keep.get("raw_json"),
            drop.get("source_of_truth"),
            drop.get("raw_json"),
        )
        merged_prov = merge_publication_source_provenance(
            keep.get("raw_json"),
            keep.get("source_provenance"),
            drop.get("raw_json"),
            drop.get("source_provenance"),
            keep.get("source_of_truth") or "",
            drop.get("source_of_truth") or "",
        )
        merged_year = keep.get("year") if keep.get("year") is not None else drop.get("year")
        merged_venue = keep.get("venue") or drop.get("venue")

        # final (non-preprint) should dominate
        merged_is_preprint = bool(keep.get("is_preprint")) and bool(drop.get("is_preprint"))

        cur.execute(
            """
            UPDATE biblio.publications
            SET doi = %s,
                year = %s,
                venue = %s,
                source_of_truth = %s,
                raw_json = %s,
                source_provenance = %s,
                is_preprint = %s,
                updated_at = now()
            WHERE pub_id = %s
            """,
            (
                merged_doi,
                merged_year,
                merged_venue,
                merged_sot,
                Jsonb(merged_raw) if merged_raw is not None else None,
                Jsonb(merged_prov) if merged_prov else None,
                merged_is_preprint,
                keep_pub_id,
            ),
        )

        # finally delete drop
        cur.execute("DELETE FROM biblio.publications WHERE pub_id = %s", (drop_pub_id,))


def merge_duplicates_by_doi(
    conn: psycopg.Connection,
    doi: str,
    *,
    incoming_pub_id: Optional[int] = None,
    dry_run: bool = False,
) -> int:
    """Merge all rows matching DOI key into a canonical row.

    Returns canonical pub_id (which may not equal incoming_pub_id).
    """
    doi_key = normalize_doi(doi)
    if not doi_key:
        return incoming_pub_id or -1

    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        f"""
        SELECT pub_id
        FROM biblio.publications
        WHERE doi IS NOT NULL
          AND {_doi_key_sql('doi')} = %s
        ORDER BY pub_id
        """,
        (doi_key,),
    )
    pub_ids = [int(r["pub_id"]) for r in cur.fetchall()]
    if incoming_pub_id is not None and incoming_pub_id not in pub_ids:
        pub_ids.append(int(incoming_pub_id))

    pub_ids = sorted(set(pub_ids))
    if len(pub_ids) <= 1:
        # Normalize DOI if possible
        if incoming_pub_id is not None and not dry_run:
            cur.execute(
                "UPDATE biblio.publications SET doi = %s, updated_at = now() WHERE pub_id = %s",
                (doi_key, incoming_pub_id),
            )
        return incoming_pub_id or pub_ids[0]

    rows = _load_pub_rows_by_ids(conn, pub_ids)
    canonical = _choose_canonical(rows)

    for pid in pub_ids:
        if pid == canonical:
            continue
        merge_publications(conn, canonical, pid, dry_run=dry_run)

    # Ensure canonical DOI is normalized
    if not dry_run:
        cur.execute(
            "UPDATE biblio.publications SET doi = %s, updated_at = now() WHERE pub_id = %s",
            (doi_key, canonical),
        )

    return canonical


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=PEOPLE_DB_DSN)
    parser.add_argument("--doi", required=True)
    parser.add_argument("--incoming-pub-id", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")

    with db_conn(args.dsn) as conn:
        canonical = merge_duplicates_by_doi(
            conn,
            args.doi,
            incoming_pub_id=args.incoming_pub_id,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            conn.commit()
        log.info(
            "merge_publication_duplicates summary: processed=1 canonical_pub_id=%s dry_run=%s errors=0",
            canonical,
            args.dry_run,
        )


if __name__ == "__main__":
    main()
