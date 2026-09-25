#!/usr/bin/env python3
"""
people_pubs.sync.alias_backfill

Backfill person name aliases from existing authorship rows, read through
biblio.authorships_curated so manual overrides (force_person/force_null/
ignore_row) are honored — matching the app.person_name_aliases view.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

# Allow running as a standalone script from repo root or DB/ without installing.
_PKG_ROOT = Path(__file__).resolve().parents[2]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from people_pubs.db.connection import db_conn
from people_pubs.db.people import upsert_person_name_alias
from people_pubs.sync.scaffold import _row_get

log = logging.getLogger(__name__)


def iter_alias_rows(
    *,
    conn,
    only_person_id: Optional[int],
    limit: Optional[int],
) -> Iterable[Tuple[int, str]]:
    sql = """
    WITH alias_rows AS (
      SELECT person_id, btrim(author_name) AS alias_name
      FROM biblio.authorships_curated
      WHERE person_id IS NOT NULL
        AND author_name IS NOT NULL
        AND btrim(author_name) <> ''
      UNION
      SELECT person_id, btrim(display_name_at_pub) AS alias_name
      FROM biblio.authorships_curated
      WHERE person_id IS NOT NULL
        AND display_name_at_pub IS NOT NULL
        AND btrim(display_name_at_pub) <> ''
    )
    SELECT person_id, alias_name
    FROM alias_rows
    """
    params = {}
    if only_person_id is not None:
        sql += " WHERE person_id = %(person_id)s"
        params["person_id"] = only_person_id
    sql += " ORDER BY person_id, alias_name"
    if limit:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit

    with conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            person_id = int(_row_get(row, "person_id", 0))
            alias_name = str(_row_get(row, "alias_name", 1)).strip()
            if not alias_name:
                continue
            yield person_id, alias_name


def run_alias_backfill(
    *,
    dsn: Optional[str],
    only_person_id: Optional[int],
    limit: Optional[int],
    commit_every: int,
    dry_run: bool,
    debug: bool,
) -> None:
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    processed = 0
    with db_conn(dsn) as conn:
        for person_id, alias_name in iter_alias_rows(
            conn=conn,
            only_person_id=only_person_id,
            limit=limit,
        ):
            upsert_person_name_alias(
                conn,
                person_id,
                alias_name,
                source="authorships.db_only",
                dry_run=dry_run,
            )
            processed += 1
            if not dry_run and commit_every and processed % commit_every == 0:
                conn.commit()

        if not dry_run:
            conn.commit()

    log.info("alias_backfill: processed=%s dry_run=%s", processed, dry_run)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Backfill person name aliases from biblio.authorships_curated.",
    )
    p.add_argument(
        "--dsn",
        default=None,
        help="Optional PostgreSQL DSN (otherwise use PEOPLE_DB_DSN or PG* env vars).",
    )
    p.add_argument(
        "--only-person-id",
        type=int,
        default=None,
        help="Only backfill aliases for one person_id.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of (person_id, alias_name) pairs to process.",
    )
    p.add_argument(
        "--commit-every",
        type=int,
        default=1000,
        help="Commit every N rows (0 disables periodic commits).",
    )
    p.add_argument("--dry-run", action="store_true", help="Do not write to DB.")
    p.add_argument("--debug", action="store_true", help="Verbose logging.")
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
    )
    run_alias_backfill(
        dsn=args.dsn,
        only_person_id=args.only_person_id,
        limit=args.limit,
        commit_every=args.commit_every,
        dry_run=bool(args.dry_run),
        debug=bool(args.debug),
    )


if __name__ == "__main__":
    main()
