"""
people_pubs.sync.enqueue_provisional

Queue DOI-less (provisional) publications for curation review.

Normalized DOI is the only automatic identity key, so publications without a
DOI are provisional: they cannot be safely deduplicated or Crossref-enriched
and stay out of biblio.publications_canon unless a curator recovers a DOI
(crossref_search / manual_csv_dois) or manually includes them via a canon
scope decision. This command inserts one *pending*
biblio.curation_review_queue entry (review_type
'publication.provisional_no_doi') per DOI-less publication that does not
already have one, so these records surface for review instead of silently
disappearing from every consumer.

Idempotent: a publication with an existing entry of this review_type is
skipped regardless of that entry's status, so curator resolutions (rejected,
applied, cancelled) are never re-opened by reruns.

Usage:
  python -m people_pubs.sync.enqueue_provisional --dry-run
  python -m people_pubs.sync.enqueue_provisional
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Optional

from psycopg.rows import dict_row

from people_pubs.config import PEOPLE_DB_DSN
from people_pubs.curation_admin import QueueActor, _insert_queue_entry
from people_pubs.db.connection import db_conn

log = logging.getLogger(__name__)

REVIEW_TYPE = "publication.provisional_no_doi"
ACTOR = QueueActor(name="enqueue_provisional", role="system")


@dataclass
class EnqueueStats:
    candidates: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    errors: int = 0


def enqueue_provisional_publications(conn, *, dry_run: bool = False) -> EnqueueStats:
    stats = EnqueueStats()

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM biblio.publications p
            JOIN biblio.curation_review_queue q
              ON q.review_type = %s AND q.subject_pub_id = p.pub_id
            WHERE p.doi IS NULL
            """,
            (REVIEW_TYPE,),
        )
        stats.skipped_existing = int(cur.fetchone()["n"])

        cur.execute(
            """
            SELECT p.pub_id, p.title, p.year, p.source_of_truth
            FROM biblio.publications p
            WHERE p.doi IS NULL
              AND NOT EXISTS (
                SELECT 1
                FROM biblio.curation_review_queue q
                WHERE q.review_type = %s
                  AND q.subject_pub_id = p.pub_id
              )
            ORDER BY p.pub_id
            """,
            (REVIEW_TYPE,),
        )
        candidates = cur.fetchall()

    stats.candidates = len(candidates)

    for row in candidates:
        pub_id = int(row["pub_id"])
        if dry_run:
            log.info(
                "  [dry-run] would enqueue pub_id=%s year=%r title=%r",
                pub_id,
                row.get("year"),
                (row.get("title") or "")[:70],
            )
            stats.inserted += 1
            continue
        try:
            queue_id = _insert_queue_entry(
                conn,
                review_type=REVIEW_TYPE,
                subject_type="publication",
                actor=ACTOR,
                subject_pub_id=pub_id,
                status="pending",
                note="DOI-less publication is provisional: recover a DOI or decide canon inclusion.",
                payload={
                    "reason": "no_doi",
                    "title": row.get("title"),
                    "year": row.get("year"),
                    "source_of_truth": row.get("source_of_truth"),
                },
            )
            if queue_id is None:
                stats.errors += 1
                log.warning("  queue insert returned no id for pub_id=%s", pub_id)
                continue
            stats.inserted += 1
            log.info("  enqueued pub_id=%s (queue_id=%s)", pub_id, queue_id)
        except Exception:
            stats.errors += 1
            log.exception("  failed to enqueue pub_id=%s", pub_id)

    return stats


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=PEOPLE_DB_DSN)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")

    with db_conn(args.dsn) as conn:
        stats = enqueue_provisional_publications(conn, dry_run=args.dry_run)
        if not args.dry_run:
            conn.commit()

    log.info(
        "enqueue_provisional summary: candidates=%s inserted=%s skipped_existing=%s errors=%s dry_run=%s",
        stats.candidates,
        stats.inserted,
        stats.skipped_existing,
        stats.errors,
        args.dry_run,
    )


if __name__ == "__main__":
    main()
