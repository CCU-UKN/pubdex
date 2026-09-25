"""
people_pubs.sync.relink_authorships

Relink authorships with missing person_id by matching against app.people.
Uses ORCID if present, otherwise falls back to display_name/aliases.

Examples:
  python -m people_pubs.sync.relink_authorships --only-pub-id 275 --debug
  python -m people_pubs.sync.relink_authorships --max 500 --dry-run
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import psycopg
from psycopg.rows import dict_row

from people_pubs.db.connection import db_conn
from people_pubs.db.people import (
    lookup_internal_person_for_author,
    person_is_internal,
    upsert_person_name_alias,
)


@dataclass
class RelinkStats:
    total: int = 0
    matched: int = 0
    updated: int = 0
    skipped_no_name: int = 0
    skipped_no_match: int = 0
    skipped_existing: int = 0
    errors: int = 0


def _load_authorship_columns(conn: psycopg.Connection) -> Set[str]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'biblio' AND table_name = 'authorships'
            """
        )
        rows = cur.fetchall()
    return {r["column_name"] for r in rows}


def _select_authorship_rows(
    conn: psycopg.Connection,
    *,
    only_pub_id: Optional[int],
    max_n: int,
    overwrite: bool,
) -> Iterable[Dict[str, Any]]:
    cols = _load_authorship_columns(conn)

    select_parts: List[str] = ["ctid", "pub_id", "person_id"]
    select_parts.append(
        "author_position" if "author_position" in cols else "NULL::int AS author_position"
    )
    select_parts.append(
        "display_name_at_pub" if "display_name_at_pub" in cols else "NULL::text AS display_name_at_pub"
    )
    select_parts.append(
        "author_name" if "author_name" in cols else "NULL::text AS author_name"
    )
    select_parts.append(
        "paper_orcid" if "paper_orcid" in cols else "NULL::text AS paper_orcid"
    )
    select_parts.append(
        "author_orcid" if "author_orcid" in cols else "NULL::text AS author_orcid"
    )

    sql = f"""
    SELECT {", ".join(select_parts)}
    FROM biblio.authorships
    WHERE
      (%(only_pub_id)s::bigint IS NULL OR pub_id = %(only_pub_id)s::bigint)
      AND (%(overwrite)s OR person_id IS NULL)
    ORDER BY pub_id ASC, author_position ASC NULLS LAST
    LIMIT %(max_n)s
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            sql,
            {
                "only_pub_id": only_pub_id,
                "max_n": max_n,
                "overwrite": bool(overwrite),
            },
        )
        for row in cur.fetchall():
            yield dict(row)


def _authorship_person_exists(
    conn: psycopg.Connection, pub_id: int, person_id: int
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM biblio.authorships
            WHERE pub_id = %s AND person_id = %s
            """,
            (pub_id, person_id),
        )
        return cur.fetchone() is not None


def _update_authorship_person_id(
    conn: psycopg.Connection,
    *,
    ctid: str,
    person_id: int,
    set_internal: bool,
    has_internal_flag: bool,
    has_updated_at: bool,
    dry_run: bool,
) -> None:
    if dry_run:
        logging.info("  [dry-run] would set person_id=%s for authorship ctid=%s", person_id, ctid)
        return

    set_parts = ["person_id = %s"]
    params: List[Any] = [person_id]

    if has_internal_flag:
        set_parts.append("is_internal_at_ingest = %s")
        params.append(bool(set_internal))

    if has_updated_at:
        set_parts.append("updated_at = now()")

    params.append(ctid)

    sql = f"""
    UPDATE biblio.authorships
    SET {", ".join(set_parts)}
    WHERE ctid = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)


def run_relink_authorships(
    *,
    dsn: Optional[str],
    only_pub_id: Optional[int],
    max_n: int,
    overwrite: bool,
    write_aliases: bool,
    dry_run: bool,
) -> RelinkStats:
    stats = RelinkStats()
    with db_conn(dsn) as conn:
        cols = _load_authorship_columns(conn)
        has_internal_flag = "is_internal_at_ingest" in cols
        has_updated_at = "updated_at" in cols

        rows = list(
            _select_authorship_rows(
                conn, only_pub_id=only_pub_id, max_n=max_n, overwrite=overwrite
            )
        )
        if not rows:
            logging.info("No authorships matched the relink criteria.")
            return stats

        for row in rows:
            stats.total += 1
            try:
                full_name = (
                    (row.get("display_name_at_pub") or row.get("author_name") or "").strip()
                )
                if not full_name:
                    stats.skipped_no_name += 1
                    continue

                author_orcid = (
                    (row.get("paper_orcid") or row.get("author_orcid") or "").strip()
                ) or None

                person_id = lookup_internal_person_for_author(
                    conn, full_name, author_orcid
                )
                if not person_id:
                    stats.skipped_no_match += 1
                    continue

                stats.matched += 1
                pub_id = int(row["pub_id"])

                if _authorship_person_exists(conn, pub_id, int(person_id)):
                    stats.skipped_existing += 1
                    continue

                _update_authorship_person_id(
                    conn,
                    ctid=row["ctid"],
                    person_id=int(person_id),
                    set_internal=person_is_internal(conn, int(person_id)),
                    has_internal_flag=has_internal_flag,
                    has_updated_at=has_updated_at,
                    dry_run=dry_run,
                )

                if write_aliases:
                    upsert_person_name_alias(
                        conn,
                        int(person_id),
                        full_name,
                        source="authorships.relink",
                        dry_run=dry_run,
                    )

                stats.updated += 1
            except Exception:
                stats.errors += 1
                logging.exception("Failed relinking authorship row: %r", row)
                if not dry_run:
                    conn.rollback()

        if not dry_run:
            conn.commit()

    return stats


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Relink biblio.authorships rows to internal people (ORCID/alias matching).",
    )
    p.add_argument(
        "--dsn",
        default=None,
        help=(
            "Optional PostgreSQL DSN (otherwise use PEOPLE_DB_DSN or PG* env vars). "
            "Example: postgresql://postgres:<password>@127.0.0.1:5432/people_db"
        ),
    )
    p.add_argument(
        "--only-pub-id",
        type=int,
        default=None,
        help="If set, only relink authorships for this pub_id.",
    )
    p.add_argument(
        "--max",
        type=int,
        default=500,
        help="Maximum authorship rows to process.",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Consider rows even when person_id is already set (still only updates when match is safe).",
    )
    p.add_argument(
        "--no-alias",
        action="store_true",
        help="Do not write aliases for matched authors.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write anything to the DB; just log what would happen.",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging.",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
    )

    stats = run_relink_authorships(
        dsn=args.dsn,
        only_pub_id=args.only_pub_id,
        max_n=args.max,
        overwrite=args.overwrite,
        write_aliases=not args.no_alias,
        dry_run=args.dry_run,
    )

    logging.info(
        "relink_authorships: total=%s matched=%s updated=%s skipped_no_name=%s "
        "skipped_no_match=%s skipped_existing=%s errors=%s",
        stats.total,
        stats.matched,
        stats.updated,
        stats.skipped_no_name,
        stats.skipped_no_match,
        stats.skipped_existing,
        stats.errors,
    )


if __name__ == "__main__":
    main()
