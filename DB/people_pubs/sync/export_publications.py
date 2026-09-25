"""
people_pubs.sync.export_publications

Deterministic, public-safe CSV export of the canonical publication list
(biblio.publications_canon). Read-only; intended for annual reporting and
external integrations.

Columns (in order):
  pub_id           canonical publication id (may change after curation resets)
  doi              normalized DOI or empty
  title            publication title
  year             publication year or empty
  venue            venue/journal string as stored on the canonical row
  source_of_truth  cumulative provenance tokens, e.g. "crossref+orcid"
  internal_authors comma-separated display names of matched internal authors

Rows are sorted newest-first: year descending (rows without a year last),
then case-insensitive title, then pub_id as the final tiebreaker, so repeated
exports of the same data are byte-identical.

Privacy: reads only biblio.publications_canon. No contact PII (emails,
phones, addresses) exists in that view, so none can appear in the export.

Examples:
  python -m people_pubs.sync.export_publications --output publications.csv
  python -m people_pubs.sync.export_publications            # writes to stdout
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, TextIO

from people_pubs.db.connection import db_conn

EXPORT_COLUMNS = (
    "pub_id",
    "doi",
    "title",
    "year",
    "venue",
    "source_of_truth",
    "internal_authors",
)

_EXPORT_SQL = """
SELECT pub_id, doi, title, year, venue, source_of_truth, internal_authors
FROM biblio.publications_canon
"""


def sort_publications(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deterministic reporting order: year desc (unknown last), title, pub_id."""
    return sorted(
        rows,
        key=lambda r: (
            -(r.get("year") if isinstance(r.get("year"), int) else -1),
            (r.get("title") or "").lower(),
            r.get("pub_id") or 0,
        ),
    )


def write_publications_csv(rows: Iterable[Dict[str, Any]], stream: TextIO) -> int:
    """Write rows (already sorted) with the documented header; returns count."""
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    count = 0
    for row in rows:
        writer.writerow(
            ["" if row.get(col) is None else str(row.get(col)) for col in EXPORT_COLUMNS]
        )
        count += 1
    return count


def export_publications(dsn: Optional[str], stream: TextIO) -> int:
    with db_conn(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(_EXPORT_SQL)
            rows = cur.fetchall()
    return write_publications_csv(sort_publications(rows), stream)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the canonical publication list as deterministic CSV."
    )
    parser.add_argument("--dsn", help="Postgres DSN (else PEOPLE_DB_DSN / PG* env)")
    parser.add_argument(
        "--output",
        help="Output CSV path (default: stdout)",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as fh:
            count = export_publications(args.dsn, fh)
        logging.info("Exported %d publications to %s", count, args.output)
    else:
        count = export_publications(args.dsn, sys.stdout)
        logging.info("Exported %d publications to stdout", count)


if __name__ == "__main__":
    main()
