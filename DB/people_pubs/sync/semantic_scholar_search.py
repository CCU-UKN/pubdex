#!/usr/bin/env python3
"""
people_pubs.sync.semantic_scholar_search

Search Semantic Scholar and ingest results into biblio.*.

Examples:
  python -m people_pubs.sync.semantic_scholar_search \
    --query "collective behaviour Example University" \
    --from-year 2019 --to-year 2025 \
    --count 100 --min-interval 1.0 \
    --source-token semantic \
    --debug

  python -m people_pubs.sync.semantic_scholar_search \
    --doi 10.1038/s41598-021-01441-w \
    --debug-json --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from contextlib import nullcontext
import psycopg

from people_pubs.config import DEFAULT_DSN, SEMANTIC_SCHOLAR_API_KEY
from people_pubs.db.authorships import compute_order_tag, upsert_authorship
from people_pubs.db.connection import db_conn
from people_pubs.db.people import (
    lookup_internal_person_for_author,
    lookup_orcid_for_person_id,
    upsert_person_name_alias,
)
from people_pubs.db.publications import merge_raw_json, upsert_publication
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
from people_pubs.sync.crossref_backfill import guess_is_preprint, merge_source_of_truth
from people_pubs.sync.scaffold import SearchStats, _lookup_existing_publication


LOGGER = logging.getLogger("people_pubs.semantic_scholar_search")


def _build_query(
    *,
    query: Optional[str],
    titles: Sequence[str],
    awards: Sequence[str],
    affiliations: Sequence[str],
    addresses: Sequence[str],
    award_mode: str,
) -> str:
    parts: List[str] = []
    if query:
        parts.append(f"({query})")
    for title in titles:
        title = title.strip()
        if title:
            parts.append(f"\"{title}\"")
    # Semantic Scholar search uses full-text query; treat awards/affiliations/addresses as terms.
    if award_mode != "query":
        LOGGER.debug("Semantic Scholar award-mode=%s treated as free-text terms.", award_mode)
    for award in awards:
        award = award.strip()
        if award:
            parts.append(award)
    for affiliation in affiliations:
        affiliation = affiliation.strip()
        if affiliation:
            parts.append(affiliation)
    for address in addresses:
        address = address.strip()
        if address:
            parts.append(address)
    return " ".join(parts).strip()


def _iter_semantic_entries(
    client: SemanticScholarClient,
    query: str,
    *,
    count: int,
    max_records: Optional[int],
    debug_json: bool,
) -> Iterable[Dict[str, Any]]:
    offset = 0
    fetched = 0
    first_page = True
    while True:
        limit = count
        if max_records is not None:
            remaining = max_records - fetched
            if remaining <= 0:
                return
            limit = min(limit, remaining)
        payload = client.search(query, limit=limit, offset=offset)
        if debug_json and first_page:
            LOGGER.info("Semantic Scholar response JSON (first page):\n%s", json.dumps(payload, indent=2)[:20000])
        first_page = False
        items = payload.get("data") or []
        if not isinstance(items, list) or not items:
            return
        for item in items:
            yield item
            fetched += 1
            if max_records is not None and fetched >= max_records:
                return
        offset += len(items)
        if len(items) < limit:
            return


def run_semantic_search(
    *,
    dsn: Optional[str],
    api_key: Optional[str],
    query: Optional[str],
    dois: Sequence[str],
    titles: Sequence[str],
    awards: Sequence[str],
    affiliations: Sequence[str],
    addresses: Sequence[str],
    award_mode: str,
    from_year: Optional[int],
    to_year: Optional[int],
    count: int,
    max_records: Optional[int],
    min_interval: float,
    no_db: bool,
    dry_run: bool,
    debug: bool,
    debug_json: bool,
    source_token: str,
    rewrite_sot: Optional[str],
    skip_existing: bool,
    refresh_authorships: bool,
    commit_every: int,
) -> SearchStats:
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if no_db and not dry_run:
        LOGGER.warning("No DB connection requested; forcing dry-run (no writes).")
        dry_run = True

    if no_db and skip_existing:
        LOGGER.warning("--skip-existing ignored because --no-db is set.")
        skip_existing = False
    if no_db and refresh_authorships:
        LOGGER.warning("--refresh-authorships ignored because --no-db is set.")
        refresh_authorships = False

    stats = SearchStats()
    seen_dois: set[str] = set()

    client = SemanticScholarClient(api_key=api_key or None, min_interval=min_interval)
    try:
        with db_conn(dsn) if not no_db else nullcontext() as conn:
            entries: Iterable[Dict[str, Any]]
            if dois and not (query or titles or awards or affiliations or addresses):
                direct_entries: List[Dict[str, Any]] = []
                for doi_raw in dois:
                    doi_norm = normalize_doi(doi_raw or "")
                    if not doi_norm:
                        continue
                    entry = client.get_paper_by_doi(doi_norm)
                    if entry:
                        direct_entries.append(entry)
                        if debug_json:
                            LOGGER.info(
                                "Semantic Scholar DOI JSON:\n%s",
                                json.dumps(entry, indent=2)[:20000],
                            )
                entries = direct_entries
            else:
                built_query = _build_query(
                    query=query,
                    titles=titles,
                    awards=awards,
                    affiliations=affiliations,
                    addresses=addresses,
                    award_mode=award_mode,
                )
                if not built_query:
                    raise SystemExit("No search terms provided. Use --query/--title/--award/--affiliation/--address or --doi.")
                LOGGER.info("Semantic Scholar query=%s", built_query)
                entries = _iter_semantic_entries(
                    client,
                    built_query,
                    count=count,
                    max_records=max_records,
                    debug_json=debug_json,
                )

            for entry in entries:
                stats.processed += 1
                try:
                    doi = semantic_doi(entry)
                    if not doi:
                        stats.skipped_no_doi += 1
                        continue
                    if doi in seen_dois:
                        continue
                    seen_dois.add(doi)

                    year = semantic_year(entry)
                    if from_year is not None and (year is None or year < from_year):
                        continue
                    if to_year is not None and (year is None or year > to_year):
                        continue

                    existing = None
                    if not no_db:
                        existing = _lookup_existing_publication(conn, doi)

                    skip_publication_update = False
                    if skip_existing and existing:
                        if refresh_authorships:
                            skip_publication_update = True
                        else:
                            stats.skipped_existing += 1
                            continue

                    title = semantic_title(entry)
                    venue = semantic_venue(entry)
                    is_preprint = semantic_is_preprint(entry)
                    if is_preprint is None:
                        is_preprint = guess_is_preprint(entry or {}, doi=doi, venue=venue, title=title)

                    existing_sot = (existing or {}).get("source_of_truth") if existing else ""
                    existing_raw = (existing or {}).get("raw_json") if existing else None
                    if rewrite_sot:
                        sot = rewrite_sot
                    else:
                        sot = merge_source_of_truth(existing_sot, source_token)

                    search_ctx = {
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "query": query or None,
                    }
                    merged_raw = merge_raw_json(
                        existing_raw,
                        str(existing_sot or ""),
                        {"semantic": entry, "semantic_search": search_ctx},
                    )

                    pub_dict = {
                        "doi": doi,
                        "title": title,
                        "year": year,
                        "venue": venue,
                        "raw_semantic_json": merged_raw,
                        "raw_crossref_json": None,
                        "raw_orcid_json": None,
                        "raw_scholar_json": None,
                        "raw_openalex_json": None,
                        "raw_dblp_json": None,
                        "raw_datacite_json": None,
                        "is_preprint": bool(is_preprint),
                        "source_of_truth": sot,
                        "replace_source_of_truth": bool(rewrite_sot),
                    }

                    if no_db:
                        LOGGER.debug("No DB: extracted doi=%s title=%r year=%r venue=%r", doi, title, year, venue)
                        continue

                    publication_id = None
                    if skip_publication_update:
                        publication_id = existing.get("pub_id") if existing else None
                    elif dry_run:
                        if existing:
                            stats.updated += 1
                        else:
                            stats.inserted += 1
                    else:
                        publication_id = upsert_publication(conn, pub_dict, dry_run=dry_run)
                        if publication_id:
                            if existing:
                                stats.updated += 1
                            else:
                                stats.inserted += 1

                    if publication_id:
                        authors = list(iter_authors_from_semantic(entry))
                        n_authors = len(authors)
                        for pos, (full_name, auth_orcid, affiliations, is_corr) in enumerate(authors, start=1):
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

                    if not no_db and not dry_run and commit_every and stats.processed % commit_every == 0:
                        conn.commit()

                except Exception:
                    stats.errors += 1
                    LOGGER.exception("Failed processing Semantic Scholar entry")
                    if not no_db and not dry_run:
                        try:
                            conn.rollback()
                        except Exception:
                            pass

            if not no_db and not dry_run:
                conn.commit()
    finally:
        client.close()

    return stats


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Search Semantic Scholar and ingest into biblio.*.")
    ap.add_argument("--dsn", help="Optional PostgreSQL DSN (otherwise PG* env vars).")
    ap.add_argument(
        "--api-key",
        default=SEMANTIC_SCHOLAR_API_KEY,
        help="Semantic Scholar API key (or set SEMANTIC_SCHOLAR_API_KEY).",
    )
    ap.add_argument("--query", help="Raw Semantic Scholar query string to execute.")
    ap.add_argument("--doi", action="append", default=[], help="DOI term (repeatable).")
    ap.add_argument("--title", action="append", default=[], help="Title term (repeatable).")
    ap.add_argument("--affiliation", action="append", default=[], help="Affiliation term (repeatable).")
    ap.add_argument("--award", action="append", default=[], help="Award term (repeatable).")
    ap.add_argument("--address", action="append", default=[], help="Address term (repeatable).")
    ap.add_argument(
        "--award-mode",
        choices=["field", "query", "both"],
        default="query",
        help="How to apply awards (treated as free-text for Semantic Scholar).",
    )
    ap.add_argument("--from-year", type=int, help="Start year (inclusive) for client-side filter.")
    ap.add_argument("--to-year", type=int, help="End year (inclusive) for client-side filter.")
    ap.add_argument("--count", type=int, default=100, help="Rows per request.")
    ap.add_argument("--max-records", type=int, help="Maximum total records to process.")
    ap.add_argument("--min-interval", type=float, default=1.0, help="Minimum seconds between API requests.")
    ap.add_argument("--no-db", action="store_true", help="Do not connect/write to the DB.")
    ap.add_argument("--dry-run", action="store_true", help="Read/parse but do not write.")
    ap.add_argument("--debug", action="store_true", help="Verbose logging.")
    ap.add_argument("--debug-json", action="store_true", help="Print first-page JSON response.")
    ap.add_argument(
        "--source-token",
        default="semantic",
        help="Token to merge into source_of_truth (default: semantic).",
    )
    ap.add_argument("--rewrite-sot", help="Replace source_of_truth instead of merging.")
    ap.add_argument("--skip-existing", action="store_true", help="Skip DOIs that already exist.")
    ap.add_argument(
        "--refresh-authorships",
        "--refresh_authorships",
        dest="refresh_authorships",
        action="store_true",
        help="Refresh authorships even when publications already exist.",
    )
    ap.add_argument("--commit-every", type=int, default=50, help="Commit every N records (0 to disable).")
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")

    stats = run_semantic_search(
        dsn=args.dsn or DEFAULT_DSN,
        api_key=args.api_key,
        query=args.query,
        dois=args.doi,
        titles=args.title,
        awards=args.award,
        affiliations=args.affiliation,
        addresses=args.address,
        award_mode=args.award_mode,
        from_year=args.from_year,
        to_year=args.to_year,
        count=args.count,
        max_records=args.max_records,
        min_interval=args.min_interval,
        no_db=args.no_db,
        dry_run=args.dry_run,
        debug=args.debug,
        debug_json=args.debug_json,
        source_token=args.source_token,
        rewrite_sot=args.rewrite_sot,
        skip_existing=args.skip_existing,
        refresh_authorships=args.refresh_authorships,
        commit_every=args.commit_every,
    )

    LOGGER.info(
        "semantic_scholar_search: processed=%s inserted=%s updated=%s skipped_no_doi=%s skipped_existing=%s errors=%s",
        stats.processed,
        stats.inserted,
        stats.updated,
        stats.skipped_no_doi,
        stats.skipped_existing,
        stats.errors,
    )


if __name__ == "__main__":
    main()
