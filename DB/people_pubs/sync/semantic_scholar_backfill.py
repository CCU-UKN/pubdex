#!/usr/bin/env python3
"""
Semantic Scholar backfill for people_pubs.

Typical usage:
  export SEMANTIC_SCHOLAR_API_KEY="..."
  python -m people_pubs.sync.semantic_scholar_backfill --max 200 --debug
  python -m people_pubs.sync.semantic_scholar_backfill --only-pub-id 1210 --debug
  python -m people_pubs.sync.semantic_scholar_backfill --only-pub-id 1210 --force --debug
  python -m people_pubs.sync.semantic_scholar_backfill --merge-mode doi --dry-run

What it does:
- Finds publications that appear not Semantic Scholar–enriched (raw_json missing 'semantic')
  and/or missing doi/year/venue, unless --force is used.
- Queries Semantic Scholar:
    - by DOI if present
    - otherwise by title (and optional year window)
- Updates biblio.publications in-place (optionally refreshes authorships with --refresh-authorships).
- If Semantic Scholar returns a DOI that already exists under a different pub_id,
  merges (moves) child rows and deletes the loser (same merge flow as Crossref/OpenAlex).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg

from people_pubs.config import DEFAULT_DSN, SEMANTIC_SCHOLAR_API_KEY
from people_pubs.db.authorships import compute_order_tag, upsert_authorship
from people_pubs.db.connection import db_conn
from people_pubs.db.people import (
    lookup_internal_person_for_author,
    lookup_orcid_for_person_id,
    upsert_person_name_alias,
)
from people_pubs.db.publications import merge_raw_json
from people_pubs.services.crossref_client import normalize_doi
from people_pubs.services.semantic_scholar_client import (
    SemanticScholarClient,
    iter_authors_from_semantic,
    semantic_doi,
    semantic_is_preprint,
    semantic_title,
    semantic_venue,
    semantic_year,
)
from people_pubs.sync.crossref_backfill import (
    PubRow,
    choose_canonical_pub,
    find_pub_by_doi,
    guess_is_preprint,
    merge_publications,
    merge_source_of_truth,
    title_similarity,
    update_publication_in_place,
    _clear_publication_doi,
)
from people_pubs.sync.scaffold import BackfillStats, _debug_dump as _scaffold_debug_dump, _note_author_added, _record_pub_changes, _row_get as _scaffold_row_get


def _row_get(row: Any, key: str, idx: int) -> Any:
    # Historical variant: returns None (instead of raising) when both dict and
    # tuple access fail.
    return _scaffold_row_get(row, key, idx, default=None)


def _debug_dump(label: str, payload: Any, max_chars: int = 4000) -> None:
    # Historical variant: dumps at INFO so payloads show without --debug.
    _scaffold_debug_dump(label, payload, max_chars=max_chars, level=logging.INFO)


LOGGER = logging.getLogger("people_pubs.semantic_scholar_backfill")


def _parse_pub_id_list(value: Optional[str]) -> Optional[List[int]]:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if "," in raw:
        pub_ids: List[int] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                pub_ids.append(int(part))
            except ValueError:
                continue
        deduped: List[int] = []
        seen: set[int] = set()
        for pid in pub_ids:
            if pid in seen:
                continue
            seen.add(pid)
            deduped.append(pid)
        return deduped or None
    path = Path(raw)
    if path.exists() and path.is_file():
        with path.open("r", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
        if not rows:
            return None
        header = [c.strip().lower() for c in rows[0] if c is not None]
        use_header = "pub_id" in header
        pub_ids: List[int] = []
        for idx, row in enumerate(rows):
            if idx == 0 and use_header:
                continue
            if not row:
                continue
            cell = row[0].strip()
            if not cell:
                continue
            try:
                pub_ids.append(int(float(cell)))
            except ValueError:
                continue
        deduped: List[int] = []
        seen: set[int] = set()
        for pid in pub_ids:
            if pid in seen:
                continue
            seen.add(pid)
            deduped.append(pid)
        return deduped or None
    try:
        return [int(raw)]
    except ValueError as exc:
        raise SystemExit("--only-pub-id must be an integer or path to a CSV file.") from exc


def iter_publications_to_backfill(
    conn: psycopg.Connection,
    only_pub_ids: Optional[List[int]],
    max_n: int,
    force: bool,
) -> Iterable[PubRow]:
    sql = """
    SELECT
      pub_id,
      doi,
      title,
      year,
      venue,
      source_of_truth,
      raw_json,
      is_preprint
    FROM biblio.publications
    WHERE
      (%(pub_ids)s::bigint[] IS NULL OR pub_id = ANY(%(pub_ids)s::bigint[]))
      AND (
        %(force)s
        OR raw_json IS NULL
        OR NOT COALESCE(raw_json ? 'semantic', FALSE)
        OR doi IS NULL
        OR year IS NULL
        OR venue IS NULL
      )
    ORDER BY
      CASE WHEN %(pub_ids)s::bigint[] IS NULL THEN updated_at END ASC,
      CASE WHEN %(pub_ids)s::bigint[] IS NOT NULL THEN array_position(%(pub_ids)s::bigint[], pub_id) END ASC,
      pub_id ASC
    """
    if max_n and max_n > 0:
        sql += " LIMIT %(max_n)s"

    with conn.cursor() as cur:
        cur.execute(
            sql,
            {
                "pub_ids": only_pub_ids,
                "max_n": max_n,
                "force": bool(force),
            },
        )
        rows = cur.fetchall()

    for row in rows:
        yield PubRow(
            pub_id=int(_row_get(row, "pub_id", 0)),
            doi=_row_get(row, "doi", 1),
            title=_row_get(row, "title", 2),
            year=_row_get(row, "year", 3),
            venue=_row_get(row, "venue", 4),
            source_of_truth=_row_get(row, "source_of_truth", 5),
            raw_json=_row_get(row, "raw_json", 6),
            is_preprint=bool(_row_get(row, "is_preprint", 7)),
        )


def _search_best_by_title(
    client: SemanticScholarClient,
    title: str,
    year: Optional[int],
    *,
    count: int,
    debug_json: bool,
    debug_max_chars: int,
) -> Optional[Dict[str, Any]]:
    q_title = re.sub(r"\s+", " ", title or "").strip()
    if not q_title:
        return None
    payload = client.search(q_title, limit=count, offset=0)
    if debug_json:
        _debug_dump("Semantic Scholar title search JSON", payload, max_chars=debug_max_chars)
    items = payload.get("data") or []
    if not isinstance(items, list):
        return None
    best: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for it in items:
        cand_title = semantic_title(it) or ""
        score = title_similarity(q_title, cand_title)
        if year is not None:
            cy = semantic_year(it) or 0
            if cy == year:
                score += 0.10
            elif abs(cy - year) == 1:
                score += 0.03
        if score > best_score:
            best_score = score
            best = it
    if best_score < 0.70:
        LOGGER.debug("Title match too weak (%.3f) for %r", best_score, title)
        return None
    return best


def _refresh_authorships_from_semantic(
    conn: psycopg.Connection,
    publication_id: int,
    entry: Dict[str, Any],
    *,
    include_department: bool,
    dry_run: bool,
    stats: BackfillStats,
) -> None:
    authors = list(iter_authors_from_semantic(entry, include_department=include_department))
    if not authors:
        return
    processed_any = False
    n_authors = len(authors)
    existing_person_ids: set[int] = set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT person_id FROM biblio.authorships WHERE pub_id = %s AND person_id IS NOT NULL",
            (publication_id,),
        )
        existing_person_ids = {
            int(_row_get(r, "person_id", 0))
            for r in cur.fetchall()
            if r and _row_get(r, "person_id", 0) is not None
        }

    for pos, (full_name, auth_orcid, affiliations, is_corr) in enumerate(authors, start=1):
        if not affiliations:
            continue
        processed_any = True
        paper_orcid = auth_orcid
        resolved_author_orcid = paper_orcid
        internal_id = lookup_internal_person_for_author(conn, full_name, paper_orcid)
        if internal_id:
            if not resolved_author_orcid:
                resolved_author_orcid = lookup_orcid_for_person_id(conn, internal_id)
            upsert_person_name_alias(
                conn,
                int(internal_id),
                full_name,
                source="semantic.author_name",
                dry_run=dry_run,
            )
            if int(internal_id) not in existing_person_ids:
                _note_author_added(stats, person_id=int(internal_id), author_name=full_name, pub_id=publication_id)
                existing_person_ids.add(int(internal_id))
        order_tag = compute_order_tag(pos, n_authors)
        upsert_authorship(
            conn=conn,
            publication_id=int(publication_id),
            author_position=pos,
            author_name=full_name,
            author_orcid=resolved_author_orcid,
            person_id=(int(internal_id) if internal_id else None),
            affiliations=affiliations,
            is_corresponding=is_corr,
            order_tag=order_tag,
            equal_contrib_tag=None,
            raw_author_json={
                "source": "semantic",
                "author": {
                    "full_name": full_name,
                    "orcid": paper_orcid,
                    "affiliations": affiliations,
                },
            },
            dry_run=dry_run,
            paper_orcid=paper_orcid,
        )

    if processed_any:
        stats.authorship_refreshed += 1
    else:
        LOGGER.info(
            "Skipping authorship refresh for pub_id=%s (no affiliations in Semantic Scholar author data)",
            publication_id,
        )


def backfill_one(
    conn: psycopg.Connection,
    client: SemanticScholarClient,
    pub: PubRow,
    *,
    merge_mode: str,
    source_token: str,
    rewrite_sot: Optional[str],
    dry_run: bool,
    force: bool,
    refresh_authorships: bool,
    include_department: bool,
    search_count: int,
    debug_json: bool,
    debug_json_max_chars: int,
    stats: BackfillStats,
) -> None:
    stats.processed += 1

    entry: Optional[Dict[str, Any]] = None
    doi_norm = normalize_doi(pub.doi)
    if doi_norm:
        entry = client.get_paper_by_doi(doi_norm)
        if debug_json and entry:
            _debug_dump("Semantic Scholar DOI JSON", entry, max_chars=debug_json_max_chars)

    if entry is None and pub.title:
        entry = _search_best_by_title(
            client,
            pub.title,
            pub.year,
            count=search_count,
            debug_json=debug_json,
            debug_max_chars=debug_json_max_chars,
        )

    if entry is None:
        LOGGER.info("No Semantic Scholar match for pub_id=%s title=%r", pub.pub_id, pub.title)
        stats.skipped_no_match += 1
        return

    stats.matched += 1
    doi = semantic_doi(entry) or pub.doi
    title = semantic_title(entry) or pub.title
    year = semantic_year(entry) or pub.year
    venue = semantic_venue(entry) or pub.venue

    is_preprint = semantic_is_preprint(entry)
    if is_preprint is None:
        is_preprint = guess_is_preprint(entry or {}, doi=doi, venue=venue, title=title)

    raw_patch = merge_raw_json(
        pub.raw_json,
        pub.source_of_truth,
        {
            "semantic": entry,
            "semantic_backfill": {"at": dt.datetime.now(dt.timezone.utc).isoformat()},
        },
    )

    if merge_mode in {"doi", "doi+title"} and doi:
        other = find_pub_by_doi(conn, doi)
        if other and other.pub_id != pub.pub_id:
            a = pub
            b = other
            winner, loser = choose_canonical_pub(a, b)
            if loser.doi:
                LOGGER.info(
                    "Clearing DOI on loser pub_id=%s to avoid conflict before merge",
                    loser.pub_id,
                )
                _clear_publication_doi(conn, loser.pub_id, dry_run=dry_run)
            winner_sot = merge_source_of_truth(winner.source_of_truth, source_token)
            update_publication_in_place(
                conn,
                winner.pub_id,
                doi=doi,
                title=title,
                year=year,
                venue=venue,
                source_of_truth=winner_sot,
                raw_json_patch=raw_patch,
                is_preprint=is_preprint,
                dry_run=dry_run,
            )
            stats.updated += 1
            stats.merged += 1
            _record_pub_changes(
                stats,
                winner,
                doi=doi,
                title=title,
                year=year,
                venue=venue,
                source_of_truth=winner_sot,
                raw_patch=True,
            )
            merge_publications(conn, winner_pub_id=winner.pub_id, loser_pub_id=loser.pub_id, dry_run=dry_run)
            if refresh_authorships:
                _refresh_authorships_from_semantic(
                    conn,
                    winner.pub_id,
                    entry,
                    include_department=include_department,
                    dry_run=dry_run,
                    stats=stats,
                )
            return

    sot = merge_source_of_truth(pub.source_of_truth, source_token) if not rewrite_sot else rewrite_sot
    update_publication_in_place(
        conn,
        pub.pub_id,
        doi=doi if (pub.doi is None and doi) else None,
        title=title,
        year=year,
        venue=venue,
        source_of_truth=sot,
        raw_json_patch=raw_patch,
        is_preprint=is_preprint,
        dry_run=dry_run,
    )
    stats.updated += 1
    _record_pub_changes(
        stats,
        pub,
        doi=doi if (pub.doi is None and doi) else pub.doi,
        title=title,
        year=year,
        venue=venue,
        source_of_truth=sot,
        raw_patch=True,
    )
    if refresh_authorships:
        _refresh_authorships_from_semantic(
            conn,
            pub.pub_id,
            entry,
            include_department=include_department,
            dry_run=dry_run,
            stats=stats,
        )


def run_semantic_backfill(
    dsn: Optional[str],
    api_key: Optional[str],
    max_n: int,
    only_pub_id: Optional[str],
    merge_mode: str,
    sleep_s: float,
    dry_run: bool,
    force: bool,
    source_token: str,
    rewrite_sot: Optional[str],
    refresh_authorships: bool,
    include_department: bool,
    search_count: int,
    debug_json: bool = False,
    debug_json_max_chars: int = 4000,
) -> BackfillStats:
    if merge_mode not in {"none", "doi", "doi+title"}:
        raise ValueError("merge_mode must be one of: none, doi, doi+title")

    stats = BackfillStats()
    only_pub_ids = _parse_pub_id_list(only_pub_id)
    client = SemanticScholarClient(api_key=api_key or None, min_interval=sleep_s)
    try:
        with db_conn(dsn) as conn:
            pubs = list(iter_publications_to_backfill(conn, only_pub_ids, max_n, force))
            if not pubs:
                logging.info("No publications matched the backfill criteria.")
                return stats
            logging.info("Semantic Scholar backfill: %s publications queued.", len(pubs))
            if force:
                logging.info("Semantic Scholar backfill: force enabled (ignoring missing-data filter).")

            for pub in pubs:
                try:
                    backfill_one(
                        conn,
                        client,
                        pub,
                        merge_mode=merge_mode,
                        source_token=source_token,
                        rewrite_sot=rewrite_sot,
                        dry_run=dry_run,
                        force=force,
                        refresh_authorships=refresh_authorships,
                        include_department=include_department,
                        search_count=search_count,
                        debug_json=debug_json,
                        debug_json_max_chars=debug_json_max_chars,
                        stats=stats,
                    )
                except Exception:
                    stats.errors += 1
                    LOGGER.exception("Failed backfilling pub_id=%s", pub.pub_id)
                    if not dry_run:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
            if not dry_run:
                conn.commit()
    finally:
        client.close()

    logging.info(
        "semantic_scholar_backfill summary: processed=%s matched=%s updated=%s merged=%s skipped_no_match=%s errors=%s",
        stats.processed,
        stats.matched,
        stats.updated,
        stats.merged,
        stats.skipped_no_match,
        stats.errors,
    )
    logging.info(
        "semantic_scholar_backfill changes: doi=%s title=%s year=%s venue=%s source_of_truth=%s raw_json=%s",
        stats.changed_doi,
        stats.changed_title,
        stats.changed_year,
        stats.changed_venue,
        stats.changed_sot,
        stats.changed_raw,
    )
    logging.info("semantic_scholar_backfill authorships_refreshed=%s", stats.authorship_refreshed)
    if stats.authors_added:
        added: List[str] = []
        for pid, entry in sorted(stats.authors_added.items()):
            pub_ids = ",".join(str(x) for x in sorted(entry["pub_ids"]))
            added.append(f"{pid}:{entry['name']}@{pub_ids}")
        logging.info("semantic_scholar_backfill authors_added: %s", "; ".join(added))
    return stats


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill publications from Semantic Scholar.")
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (defaults to people_pubs.config.DEFAULT_DSN)")
    p.add_argument(
        "--api-key",
        default=SEMANTIC_SCHOLAR_API_KEY,
        help="Semantic Scholar API key (or set SEMANTIC_SCHOLAR_API_KEY).",
    )
    p.add_argument("--max", type=int, default=200, help="Max publications to backfill.")
    p.add_argument("--only-pub-id", help="Process one pub_id or a CSV file (single column of pub_ids).")
    p.add_argument(
        "--merge-mode",
        default="doi",
        choices=["none", "doi", "doi+title"],
        help="Duplicate merge strategy. Default: doi (merge only on exact DOI collision).",
    )
    p.add_argument(
        "--count",
        type=int,
        default=5,
        help="Rows per title-based search (default: 5).",
    )
    p.add_argument("--sleep", type=float, default=1.0, help="Seconds to sleep between API calls.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--source-token", default="semantic", help="Token to merge into source_of_truth.")
    p.add_argument("--rewrite-sot", help="Replace source_of_truth instead of merging.")
    p.add_argument(
        "--refresh-authorships",
        "--refresh_authorships",
        dest="refresh_authorships",
        action="store_true",
        help="Refresh authorships from Semantic Scholar author data.",
    )
    p.add_argument(
        "--department",
        action="store_true",
        help="Include department fields in affiliation strings when available.",
    )
    p.add_argument("--debug", action="store_true", help="Enable debug logging.")
    p.add_argument("--debug-json", action="store_true", help="Print Semantic Scholar JSON payloads.")
    p.add_argument(
        "--debug-json-max-chars",
        type=int,
        default=4000,
        help="Limit debug JSON output size.",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    run_semantic_backfill(
        args.dsn,
        api_key=args.api_key,
        max_n=args.max,
        only_pub_id=args.only_pub_id,
        merge_mode=args.merge_mode,
        sleep_s=args.sleep,
        dry_run=args.dry_run,
        force=args.force,
        source_token=args.source_token,
        rewrite_sot=args.rewrite_sot,
        refresh_authorships=args.refresh_authorships,
        include_department=args.department,
        search_count=args.count,
        debug_json=args.debug_json,
        debug_json_max_chars=args.debug_json_max_chars,
    )


if __name__ == "__main__":
    main()
