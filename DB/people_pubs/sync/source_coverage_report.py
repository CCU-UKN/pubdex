"""
people_pubs.sync.source_coverage_report

Deterministic, public-safe source-coverage report over the canonical
publication set (biblio.publications_canon): which metadata providers
contributed to each canonical publication, and how broad the provenance is
overall. Read-only.

Per-publication columns (in order):
  pub_id             canonical publication id
  doi                normalized DOI or empty
  title              publication title
  year               publication year or empty
  n_source_families  number of distinct source families in source_of_truth
  source_families    '+'-joined sorted family names, e.g. "crossref+orcid"
  source_tokens      '+'-joined sorted raw tokens, e.g. "crossref+crossref-funding+orcid"

Summary rows (in order):
  metric,value pairs: total canonical publications, publications per source
  family (family=<name>), single-source publication count, and the provenance
  breadth histogram (families=<n>).

Rows are sorted by pub_id so repeated exports of the same data are
byte-identical. Family/token semantics come from people_pubs.config
(source_tokens / source_family) — the same helpers the dedup policy uses.

Privacy: reads only biblio.publications_canon; no contact PII exists there.

Examples:
  python -m people_pubs.sync.source_coverage_report --summary
  python -m people_pubs.sync.source_coverage_report --output coverage.csv
  python -m people_pubs.sync.source_coverage_report --output coverage.csv --summary-output coverage_summary.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, TextIO, Tuple

from people_pubs.config import source_family, source_tokens
from people_pubs.db.connection import db_conn
from people_pubs.sync.scaffold import _row_get

COVERAGE_COLUMNS = (
    "pub_id",
    "doi",
    "title",
    "year",
    "n_source_families",
    "source_families",
    "source_tokens",
)

SUMMARY_COLUMNS = ("metric", "value")

_COVERAGE_SQL = """
SELECT pub_id, doi, title, year, source_of_truth
FROM biblio.publications_canon
ORDER BY pub_id
"""


def coverage_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Map one canonical publication row to its coverage row."""
    tokens = sorted(set(source_tokens(str(row.get("source_of_truth") or ""))))
    families = sorted({source_family(tok) for tok in tokens})
    return {
        "pub_id": row.get("pub_id"),
        "doi": row.get("doi"),
        "title": row.get("title"),
        "year": row.get("year"),
        "n_source_families": len(families),
        "source_families": "+".join(families),
        "source_tokens": "+".join(tokens),
    }


def build_coverage_rows(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Coverage rows for every canonical publication, sorted by pub_id."""
    out = [coverage_row(row) for row in rows]
    out.sort(key=lambda r: r.get("pub_id") or 0)
    return out


def summarize_coverage(coverage_rows: Sequence[Mapping[str, Any]]) -> List[Tuple[str, Any]]:
    """Aggregate metrics over coverage rows; deterministic order."""
    family_counts: Counter[str] = Counter()
    breadth_counts: Counter[int] = Counter()
    for row in coverage_rows:
        families = [f for f in str(row.get("source_families") or "").split("+") if f]
        breadth_counts[len(families)] += 1
        for family in families:
            family_counts[family] += 1

    summary: List[Tuple[str, Any]] = [
        ("total_canonical_publications", len(coverage_rows)),
    ]
    for family in sorted(family_counts):
        summary.append((f"publications_with_family={family}", family_counts[family]))
    summary.append(("single_source_publications", breadth_counts.get(1, 0)))
    for breadth in sorted(breadth_counts):
        summary.append((f"publications_with_n_families={breadth}", breadth_counts[breadth]))
    return summary


def write_coverage_csv(rows: Iterable[Mapping[str, Any]], stream: TextIO) -> int:
    """Write coverage rows (already sorted) with the documented header."""
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(COVERAGE_COLUMNS)
    count = 0
    for row in rows:
        writer.writerow(
            ["" if row.get(col) is None else str(row.get(col)) for col in COVERAGE_COLUMNS]
        )
        count += 1
    return count


def write_summary_csv(summary: Iterable[Tuple[str, Any]], stream: TextIO) -> int:
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(SUMMARY_COLUMNS)
    count = 0
    for metric, value in summary:
        writer.writerow([metric, "" if value is None else str(value)])
        count += 1
    return count


def fetch_canon_rows(dsn: Optional[str]) -> List[Dict[str, Any]]:
    with db_conn(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(_COVERAGE_SQL)
            fetched = cur.fetchall()
    rows: List[Dict[str, Any]] = []
    for raw in fetched:
        rows.append(
            {
                "pub_id": _row_get(raw, "pub_id", 0),
                "doi": _row_get(raw, "doi", 1),
                "title": _row_get(raw, "title", 2),
                "year": _row_get(raw, "year", 3),
                "source_of_truth": _row_get(raw, "source_of_truth", 4),
            }
        )
    return rows


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Report which metadata providers contributed to each canonical "
            "publication (read-only)."
        )
    )
    parser.add_argument("--dsn", help="Postgres DSN (else PEOPLE_DB_DSN / PG* env)")
    parser.add_argument("--output", help="Per-publication coverage CSV path (default: stdout)")
    parser.add_argument("--summary-output", help="Summary CSV path")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print the summary (instead of per-publication rows) to stdout",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    rows = build_coverage_rows(fetch_canon_rows(args.dsn))
    summary = summarize_coverage(rows)

    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as fh:
            count = write_coverage_csv(rows, fh)
        logging.info("Wrote %d coverage rows to %s", count, args.output)
    elif not args.summary:
        write_coverage_csv(rows, sys.stdout)

    if args.summary_output:
        with open(args.summary_output, "w", encoding="utf-8", newline="") as fh:
            write_summary_csv(summary, fh)
        logging.info("Wrote summary to %s", args.summary_output)
    if args.summary:
        write_summary_csv(summary, sys.stdout)


if __name__ == "__main__":
    main()
