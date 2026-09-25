#!/usr/bin/env python3
"""
people_pubs.sync.datacite_search

Search DataCite and ingest into biblio.publications/authorships.

Notes:
- DataCite search is full-text. Award/affiliation/address terms are appended to
  the query string; use --query for raw DataCite query syntax if needed.
- If you pass only --doi (repeatable), the script will fetch records directly
  via the DOI endpoint instead of using search.

Examples:
  # Simple query (full-text)
  python -m people_pubs.sync.datacite_search \
    --query "\"Example University\"" \
    --from-year 2019 --to-year 2025 \
    --count 100 --min-interval 0.2 \
    --source-token datacite \
    --debug

  # Award/affiliation terms
  python -m people_pubs.sync.datacite_search \
    --award 12345678 \
    --affiliation "Example University" \
    --from-year 2019 --to-year 2025 \
    --count 100 --min-interval 0.2 \
    --source-token datacite-funding \
    --debug
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg

from people_pubs.config import DEFAULT_DSN, DATACITE_API_TOKEN
from people_pubs.db.connection import db_conn
from people_pubs.db.publications import upsert_publication, merge_raw_json
from people_pubs.db.authorships import compute_order_tag, upsert_authorship
from people_pubs.db.people import (
    lookup_internal_person_for_author,
    lookup_orcid_for_person_id,
    upsert_person_name_alias,
)
from people_pubs.services.crossref_client import normalize_doi, guess_is_preprint
from people_pubs.services.datacite_client import (
    DataCiteClient,
    iter_authors_from_datacite,
    datacite_doi,
    datacite_is_preprint,
    datacite_title,
    datacite_venue,
    datacite_year,
)
from people_pubs.sync.crossref_backfill import merge_source_of_truth
from people_pubs.sync.scaffold import SearchStats, _lookup_existing_publication


LOGGER = logging.getLogger("people_pubs.datacite_search")


def _quote_term(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    v = v.replace('"', '\\"')
    if any(ch.isspace() for ch in v):
        return f"\"{v}\""
    return v


def _or_group(items: List[str]) -> Optional[str]:
    items = [i for i in items if i]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return "(" + " OR ".join(items) + ")"


def _build_query(
    *,
    query: Optional[str],
    dois: Sequence[str],
    titles: Sequence[str],
    awards: Sequence[str],
    affiliations: Sequence[str],
    addresses: Sequence[str],
) -> str:
    parts: List[str] = []
    if query:
        parts.append(f"({query})")

    doi_terms = [_quote_term(d) for d in dois if d]
    doi_clause = _or_group(doi_terms)
    if doi_clause:
        parts.append(doi_clause)

    title_terms = [_quote_term(t) for t in titles if t]
    title_clause = _or_group(title_terms)
    if title_clause:
        parts.append(title_clause)

    award_terms = [_quote_term(a) for a in awards if a]
    award_clause = _or_group(award_terms)
    if award_clause:
        parts.append(award_clause)

    aff_terms = [_quote_term(a) for a in affiliations if a]
    aff_clause = _or_group(aff_terms)
    if aff_clause:
        parts.append(aff_clause)

    addr_terms = [_quote_term(a) for a in addresses if a]
    addr_clause = _or_group(addr_terms)
    if addr_clause:
        parts.append(addr_clause)

    if not parts:
        raise SystemExit("DataCite search requires at least one query/term or DOI.")
    return " AND ".join(parts)


def _iter_datacite_entries(
    client: DataCiteClient,
    query: str,
    *,
    count: int,
    max_records: Optional[int],
    debug_json: bool,
) -> Iterable[Dict[str, Any]]:
    fetched = 0
    page = 1
    first_page = True
    while True:
        payload = client.search(query, size=count, page=page)
        if first_page and debug_json:
            print("DataCite Search JSON (first page):")
            print(json.dumps(payload, indent=2, ensure_ascii=False)[:4000])
        first_page = False
        entries = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(entries, list):
            entries = [entries] if entries else []
        if not entries:
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            yield entry
            fetched += 1
            if max_records is not None and fetched >= max_records:
                return
        meta = payload.get("meta") if isinstance(payload, dict) else {}
        total = None
        if isinstance(meta, dict):
            try:
                total = int(meta.get("total") or 0)
            except Exception:
                total = None
        if total is not None and fetched >= total:
            return
        page += 1


def _year_in_range(year: Optional[int], from_year: Optional[int], to_year: Optional[int]) -> bool:
    if year is None:
        return from_year is None and to_year is None
    if from_year is not None and year < from_year:
        return False
    if to_year is not None and year > to_year:
        return False
    return True


def run_datacite_search(
    *,
    dsn: str,
    token: Optional[str],
    query: Optional[str],
    dois: Sequence[str],
    titles: Sequence[str],
    awards: Sequence[str],
    affiliations: Sequence[str],
    addresses: Sequence[str],
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
    stats = SearchStats()
    seen_dois: set[str] = set()

    if no_db and skip_existing:
        LOGGER.warning("--no-db disables --skip-existing (no DB lookup).")
        skip_existing = False

    client = DataCiteClient(token=token, min_interval=min_interval)
    try:
        query_used: Optional[str] = None
        if dois and not (query or titles or awards or affiliations or addresses):
            entries: Iterable[Dict[str, Any]] = []
            direct_entries: List[Dict[str, Any]] = []
            for doi_raw in dois:
                doi_norm = normalize_doi(doi_raw or "")
                if not doi_norm:
                    continue
                entry = client.get_doi(doi_norm)
                if entry:
                    direct_entries.append(entry)
            entries = direct_entries
        else:
            query_str = _build_query(
                query=query,
                dois=dois,
                titles=titles,
                awards=awards,
                affiliations=affiliations,
                addresses=addresses,
            )
            query_used = query_str
            LOGGER.info("DataCite search query=%s", query_str)
            entries = _iter_datacite_entries(
                client,
                query_str,
                count=count,
                max_records=max_records,
                debug_json=debug_json,
            )

        ctx = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "query": query_used or query,
            "awards": list(awards),
            "affiliations": list(affiliations),
            "addresses": list(addresses),
            "titles": list(titles),
            "dois": list(dois),
            "from_year": from_year,
            "to_year": to_year,
        }

        with (db_conn(dsn) if not no_db else nullcontext()) as conn:
            for entry in entries:
                if max_records is not None and stats.processed >= max_records:
                    break
                stats.processed += 1
                try:
                    doi_raw = datacite_doi(entry) or ""
                    doi = normalize_doi(doi_raw)
                    if not doi:
                        stats.skipped_no_doi += 1
                        continue
                    if doi in seen_dois:
                        continue
                    seen_dois.add(doi)

                    year = datacite_year(entry)
                    if not _year_in_range(year, from_year, to_year):
                        continue

                    title = (datacite_title(entry) or "").strip()
                    venue = datacite_venue(entry)
                    is_preprint = datacite_is_preprint(entry)
                    if is_preprint is None:
                        is_preprint = guess_is_preprint({}, doi=doi, venue=venue, title=title)

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

                    existing_sot = existing.get("source_of_truth") if existing else ""
                    existing_raw = existing.get("raw_json") if existing else None
                    sot = rewrite_sot if rewrite_sot else merge_source_of_truth(existing_sot, source_token)
                    merged_raw = merge_raw_json(
                        existing_raw,
                        str(existing_sot or ""),
                        {
                            "datacite": entry,
                            "datacite_search": ctx,
                        },
                    )

                    pub_dict = {
                        "doi": doi,
                        "title": title,
                        "year": year,
                        "venue": venue,
                        "raw_datacite_json": merged_raw,
                        "is_preprint": bool(is_preprint) if is_preprint is not None else None,
                        "source_of_truth": sot,
                        "replace_source_of_truth": bool(rewrite_sot),
                    }

                    if no_db:
                        LOGGER.debug("No DB: extracted doi=%s title=%r year=%r", doi, title, year)
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
                        authors = list(iter_authors_from_datacite(entry))
                        n_authors = len(authors)
                        for pos, (full_name, auth_orcid, affiliations, is_corr) in enumerate(
                            authors, start=1
                        ):
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
                                    source="datacite.author_name",
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
                                    "source": "datacite",
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
                    LOGGER.exception("Failed processing DataCite entry")
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
    ap = argparse.ArgumentParser(description="Search DataCite and ingest into biblio.*.")
    ap.add_argument("--dsn", help="Optional PostgreSQL DSN (otherwise PG* env vars).")
    ap.add_argument("--token", default=DATACITE_API_TOKEN, help="DataCite API token (optional).")
    ap.add_argument("--query", help="Raw DataCite query string to execute.")
    ap.add_argument("--doi", action="append", default=[], help="DOI term (repeatable).")
    ap.add_argument("--title", action="append", default=[], help="Title term (repeatable).")
    ap.add_argument("--award", action="append", default=[], help="Award/grant number (repeatable).")
    ap.add_argument("--affiliation", action="append", default=[], help="Affiliation search term (repeatable).")
    ap.add_argument("--address", action="append", default=[], help="Address search term (repeatable).")
    ap.add_argument("--from-year", type=int, help="Start year (inclusive) for publication date filter.")
    ap.add_argument("--to-year", type=int, help="End year (inclusive) for publication date filter.")
    ap.add_argument("--count", type=int, default=100, help="Rows per request (DataCite page size).")
    ap.add_argument("--max-records", type=int, help="Maximum total records to process.")
    ap.add_argument("--min-interval", type=float, default=0.2, help="Minimum seconds between API requests.")
    ap.add_argument("--no-db", action="store_true", help="Do not connect/write to the DB.")
    ap.add_argument("--dry-run", action="store_true", help="Read/parse but do not write.")
    ap.add_argument(
        "--refresh-authorships",
        "--refresh_authorships",
        dest="refresh_authorships",
        action="store_true",
        help="Refresh authorships even when publications already exist.",
    )
    ap.add_argument("--debug", action="store_true", help="Verbose logging.")
    ap.add_argument("--debug-json", action="store_true", help="Print first-page JSON response.")
    ap.add_argument(
        "--source-token",
        default="datacite",
        help="Token to merge into source_of_truth (default: datacite).",
    )
    ap.add_argument("--rewrite-sot", help="Replace source_of_truth instead of merging.")
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip DOIs that already exist in biblio.publications.",
    )
    ap.add_argument("--commit-every", type=int, default=50, help="Commit every N records (0 to disable).")
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")

    if not (args.query or args.doi or args.title or args.award or args.affiliation or args.address):
        raise SystemExit("Provide at least one of --query/--doi/--title/--award/--affiliation/--address.")

    stats = run_datacite_search(
        dsn=args.dsn or DEFAULT_DSN,
        token=args.token or None,
        query=args.query,
        dois=args.doi,
        titles=args.title,
        awards=args.award,
        affiliations=args.affiliation,
        addresses=args.address,
        from_year=args.from_year,
        to_year=args.to_year,
        count=args.count,
        max_records=args.max_records,
        min_interval=args.min_interval,
        no_db=bool(args.no_db),
        dry_run=bool(args.dry_run),
        debug=bool(args.debug),
        debug_json=bool(args.debug_json),
        source_token=str(args.source_token or "datacite"),
        rewrite_sot=args.rewrite_sot or None,
        skip_existing=bool(args.skip_existing),
        refresh_authorships=bool(args.refresh_authorships),
        commit_every=int(args.commit_every or 0),
    )

    LOGGER.info(
        "datacite_search: processed=%s inserted=%s updated=%s skipped_no_doi=%s skipped_existing=%s errors=%s",
        stats.processed,
        stats.inserted,
        stats.updated,
        stats.skipped_no_doi,
        stats.skipped_existing,
        stats.errors,
    )


if __name__ == "__main__":
    main()
