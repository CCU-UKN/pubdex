#!/usr/bin/env python3
"""
people_pubs.sync.dblp_search_people

Search DBLP for each person in app.people (optionally including aliases)
and ingest results into biblio.publications/authorships.

Notes:
- DBLP does not expose structured funding/affiliation/address fields.
  Any --award/--affiliation/--address terms are added as plain-text query terms.
- Expect false positives for common names; consider limiting with --series/--venue
  or running --only-person-id for targeted runs.

Examples:
  # All people, limit per person
  python -m people_pubs.sync.dblp_search_people --limit-people 200 --max-per-person 25 --debug

  # One person, include aliases, and restrict to series
  python -m people_pubs.sync.dblp_search_people \
    --only-person-id 16 \
    --include-aliases \
    --series lni --series-mode stream \
    --from-year 2019 --to-year 2025 \
    --debug
"""

from __future__ import annotations

import argparse
import csv
import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx
import psycopg

# Allow running as a standalone script from the repo root or DB/ without installing.
_PKG_ROOT = Path(__file__).resolve().parents[2]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from people_pubs.config import DEFAULT_DSN, USER_AGENT
from people_pubs.db.connection import db_conn
from people_pubs.db.publications import upsert_publication, merge_raw_json
from people_pubs.db.authorships import compute_order_tag, upsert_authorship
from people_pubs.db.people import (
    lookup_internal_person_for_author,
    lookup_orcid_for_person_id,
    upsert_person_name_alias,
)
from people_pubs.services.crossref_client import normalize_doi, guess_is_preprint
from people_pubs.sync.crossref_backfill import merge_source_of_truth
from people_pubs.sync.dblp_search import (
    _build_query,
    _extract_doi,
    _info_title,
    _info_venue,
    _iter_authors_from_dblp,
    _iter_dblp_hits,
    _parse_year,
)
from people_pubs.sync.scaffold import SearchStats, _lookup_existing_publication, _parse_person_id_list, _row_get


LOGGER = logging.getLogger("people_pubs.dblp_search_people")


def _load_people(
    conn: psycopg.Connection,
    *,
    only_person_ids: Optional[Sequence[int]],
    only_orcid: Optional[str],
    limit_people: Optional[int],
) -> List[Dict[str, Any]]:
    sql = """
        SELECT
          p.person_id,
          p.display_name,
          io.orcid,
          io.orcid_display_name
        FROM app.people p
        LEFT JOIN app.identities_orcid io
          ON io.person_id = p.person_id
        WHERE (%(only_person_ids)s::bigint[] IS NULL OR p.person_id = ANY(%(only_person_ids)s))
          AND (%(only_orcid)s::text IS NULL OR io.orcid = %(only_orcid)s)
        ORDER BY p.person_id
    """
    params = {
        "only_person_ids": list(only_person_ids) if only_person_ids else None,
        "only_orcid": only_orcid,
    }
    if isinstance(limit_people, int) and limit_people > 0:
        sql += " LIMIT %(limit_people)s"
        params["limit_people"] = limit_people
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    people: List[Dict[str, Any]] = []
    for row in rows:
        people.append(
            {
                "person_id": int(_row_get(row, "person_id", 0)),
                "display_name": _row_get(row, "display_name", 1),
                "orcid": _row_get(row, "orcid", 2),
                "orcid_display_name": _row_get(row, "orcid_display_name", 3),
            }
        )
    return people


def _load_aliases(
    conn: psycopg.Connection,
    person_ids: Sequence[int],
) -> Dict[int, List[str]]:
    if not person_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT person_id, alias_name
            FROM app.person_name_aliases
            WHERE person_id = ANY(%s)
            """,
            (list(person_ids),),
        )
        rows = cur.fetchall()
    out: Dict[int, List[str]] = {}
    for row in rows:
        pid = int(_row_get(row, "person_id", 0))
        alias = (_row_get(row, "alias_name", 1) or "").strip()
        if not alias:
            continue
        out.setdefault(pid, []).append(alias)
    return out


def _name_ok(name: str, min_words: int, min_len: int) -> bool:
    if not name:
        return False
    if len(name) < min_len:
        return False
    if min_words > 1:
        if len([p for p in name.split() if p]) < min_words:
            return False
    return True


def _build_author_query(
    name: str,
    *,
    author_mode: str,
    query: Optional[str],
    venues: Sequence[str],
    series: Sequence[str],
    series_mode: str,
    awards: Sequence[str],
    affiliations: Sequence[str],
    addresses: Sequence[str],
) -> str:
    if author_mode == "text":
        safe = name.replace('"', '\\"')
        name_query = f"\"{safe}\""
        combined_query = f"{name_query} AND ({query})" if query else name_query
        return _build_query(
            query=combined_query,
            titles=[],
            authors=[],
            venues=venues,
            series=series,
            series_mode=series_mode,
            dois=[],
            awards=awards,
            affiliations=affiliations,
            addresses=addresses,
        )
    return _build_query(
        query=query,
        titles=[],
        authors=[name],
        venues=venues,
        series=series,
        series_mode=series_mode,
        dois=[],
        awards=awards,
        affiliations=affiliations,
        addresses=addresses,
    )


def run_dblp_search_people(
    *,
    dsn: Optional[str],
    query: Optional[str],
    author_mode: str,
    venues: Sequence[str],
    series: Sequence[str],
    series_mode: str,
    awards: Sequence[str],
    affiliations: Sequence[str],
    addresses: Sequence[str],
    from_year: Optional[int],
    to_year: Optional[int],
    count: int,
    max_records: Optional[int],
    max_per_person: Optional[int],
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
    only_person_ids: Optional[Sequence[int]],
    only_orcid: Optional[str],
    limit_people: Optional[int],
    include_aliases: bool,
    min_name_words: int,
    min_name_length: int,
) -> SearchStats:
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if no_db:
        raise SystemExit(
            "--no-db is not supported for dblp_search_people: "
            "this command must read app.people from the database. "
            "Use --dry-run for a no-write test."
        )

    if no_db and not dry_run:
        LOGGER.warning("No DB connection requested; forcing dry-run (no writes).")
        dry_run = True

    if no_db and skip_existing:
        LOGGER.warning("--skip-existing ignored because --no-db is set.")
        skip_existing = False
    if no_db and refresh_authorships:
        LOGGER.warning("--refresh-authorships ignored because --no-db is set.")
        refresh_authorships = False

    if awards or affiliations or addresses:
        LOGGER.warning(
            "DBLP does not support structured award/affiliation/address filters; using plain text terms."
        )

    stats = SearchStats()
    seen_dois: set[str] = set()

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=30.0, http2=True) as client:
        with db_conn(dsn) if not no_db else nullcontext() as conn:
            people = _load_people(
                conn,
                only_person_ids=only_person_ids,
                only_orcid=only_orcid,
                limit_people=limit_people,
            )
            stats.people = len(people)
            alias_map = {}
            if include_aliases and people:
                alias_map = _load_aliases(conn, [p["person_id"] for p in people])

            throttle = [0.0]
            for person in people:
                person_id = int(person["person_id"])
                display_name = (person.get("display_name") or "").strip()
                orcid_display = (person.get("orcid_display_name") or "").strip()
                names: List[str] = []
                for n in (display_name, orcid_display):
                    if n and n not in names:
                        names.append(n)
                if include_aliases:
                    for alias in alias_map.get(person_id, []):
                        if alias not in names:
                            names.append(alias)

                filtered_names = [
                    n for n in names if _name_ok(n, min_name_words, min_name_length)
                ]
                if not filtered_names:
                    continue

                LOGGER.info(
                    "DBLP person search: person_id=%s display_name=%r names=%s",
                    person_id,
                    display_name,
                    filtered_names,
                )

                stop_all = False
                for name in filtered_names:
                    stats.names += 1
                    person_query = _build_author_query(
                        name,
                        author_mode=author_mode,
                        query=query,
                        venues=venues,
                        series=series,
                        series_mode=series_mode,
                        awards=awards,
                        affiliations=affiliations,
                        addresses=addresses,
                    )
                    LOGGER.debug(
                        "DBLP person query: person_id=%s name=%r query=%r",
                        person_id,
                        name,
                        person_query,
                    )

                    remaining = None
                    if max_records is not None:
                        remaining = max_records - stats.processed
                        if remaining <= 0:
                            return stats
                    per_person = max_per_person
                    if per_person is not None and remaining is not None:
                        per_person = min(per_person, remaining)
                    elif remaining is not None:
                        per_person = remaining

                    try:
                        got_hits = False
                        for info in _iter_dblp_hits(
                            client,
                            person_query,
                            rows=count,
                            max_records=per_person,
                            min_interval=min_interval,
                            debug_json=debug_json,
                            last_request=throttle,
                        ):
                            got_hits = True
                            stats.processed += 1
                            try:
                                year = _parse_year(info.get("year"))
                                if isinstance(from_year, int) and year is not None and year < from_year:
                                    continue
                                if isinstance(to_year, int) and year is not None and year > to_year:
                                    continue

                                doi = _extract_doi(info)
                                if not doi:
                                    stats.skipped_no_doi += 1
                                    continue
                                if doi in seen_dois:
                                    continue
                                seen_dois.add(doi)

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

                                title = _info_title(info)
                                venue = _info_venue(info)
                                is_preprint = guess_is_preprint({}, doi=doi, venue=venue, title=title)

                                existing_sot = existing.get("source_of_truth") if existing else ""
                                existing_raw = existing.get("raw_json") if existing else None
                                if rewrite_sot:
                                    sot = rewrite_sot
                                else:
                                    sot = merge_source_of_truth(existing_sot, source_token)

                                search_ctx = {
                                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "person_id": person_id,
                                    "person_name": name,
                                    "query": person_query,
                                }
                                merged_raw = merge_raw_json(
                                    existing_raw,
                                    str(existing_sot or ""),
                                    {"dblp": info, "dblp_search_people": search_ctx},
                                )

                                pub_dict = {
                                    "doi": doi,
                                    "title": title,
                                    "year": year,
                                    "venue": venue,
                                    "raw_crossref_json": None,
                                    "raw_orcid_json": None,
                                    "raw_scholar_json": None,
                                    "raw_openalex_json": None,
                                    "raw_dblp_json": merged_raw,
                                    "is_preprint": bool(is_preprint),
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
                                    authors = list(_iter_authors_from_dblp(info))
                                    n_authors = len(authors)
                                    for pos, (full_name, auth_orcid, affiliations) in enumerate(authors, start=1):
                                        paper_orcid = auth_orcid
                                        resolved_author_orcid = paper_orcid
                                        internal_id = lookup_internal_person_for_author(
                                            conn, full_name, paper_orcid
                                        )
                                        if internal_id:
                                            if not resolved_author_orcid:
                                                resolved_author_orcid = lookup_orcid_for_person_id(conn, internal_id)
                                            upsert_person_name_alias(
                                                conn,
                                                int(internal_id),
                                                full_name,
                                                source="dblp.author_name",
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
                                            is_corresponding=False,
                                            order_tag=order_tag,
                                            equal_contrib_tag=None,
                                            raw_author_json={
                                                "source": "dblp",
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
                                LOGGER.exception(
                                    "Failed processing DBLP record for person_id=%s name=%r",
                                    person_id,
                                    name,
                                )
                                if not no_db and not dry_run:
                                    try:
                                        conn.rollback()
                                    except Exception:
                                        pass
                        if not got_hits and author_mode == "auto":
                            fallback_query = _build_author_query(
                                name,
                                author_mode="text",
                                query=query,
                                venues=venues,
                                series=series,
                                series_mode=series_mode,
                                awards=awards,
                                affiliations=affiliations,
                                addresses=addresses,
                            )
                            LOGGER.debug(
                                "DBLP author query returned no hits; retrying as text query=%r",
                                fallback_query,
                            )
                            for info in _iter_dblp_hits(
                                client,
                                fallback_query,
                                rows=count,
                                max_records=per_person,
                                min_interval=min_interval,
                                debug_json=debug_json,
                                last_request=throttle,
                            ):
                                stats.processed += 1
                                try:
                                    year = _parse_year(info.get("year"))
                                    if isinstance(from_year, int) and year is not None and year < from_year:
                                        continue
                                    if isinstance(to_year, int) and year is not None and year > to_year:
                                        continue

                                    doi = _extract_doi(info)
                                    if not doi:
                                        stats.skipped_no_doi += 1
                                        continue
                                    if doi in seen_dois:
                                        continue
                                    seen_dois.add(doi)

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

                                    title = _info_title(info)
                                    venue = _info_venue(info)
                                    is_preprint = guess_is_preprint({}, doi=doi, venue=venue, title=title)

                                    existing_sot = existing.get("source_of_truth") if existing else ""
                                    existing_raw = existing.get("raw_json") if existing else None
                                    if rewrite_sot:
                                        sot = rewrite_sot
                                    else:
                                        sot = merge_source_of_truth(existing_sot, source_token)

                                    search_ctx = {
                                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                        "person_id": person_id,
                                        "person_name": name,
                                        "query": fallback_query,
                                    }
                                    merged_raw = merge_raw_json(
                                        existing_raw,
                                        str(existing_sot or ""),
                                        {"dblp": info, "dblp_search_people": search_ctx},
                                    )

                                    pub_dict = {
                                        "doi": doi,
                                        "title": title,
                                        "year": year,
                                        "venue": venue,
                                        "raw_crossref_json": None,
                                        "raw_orcid_json": None,
                                        "raw_scholar_json": None,
                                        "raw_openalex_json": None,
                                        "raw_dblp_json": merged_raw,
                                        "is_preprint": bool(is_preprint),
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
                                        authors = list(_iter_authors_from_dblp(info))
                                        n_authors = len(authors)
                                        for pos, (full_name, auth_orcid, affiliations) in enumerate(authors, start=1):
                                            paper_orcid = auth_orcid
                                            resolved_author_orcid = paper_orcid
                                            internal_id = lookup_internal_person_for_author(
                                                conn, full_name, paper_orcid
                                            )
                                            if internal_id:
                                                if not resolved_author_orcid:
                                                    resolved_author_orcid = lookup_orcid_for_person_id(conn, internal_id)
                                                upsert_person_name_alias(
                                                    conn,
                                                    int(internal_id),
                                                    full_name,
                                                    source="dblp.author_name",
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
                                                is_corresponding=False,
                                                order_tag=order_tag,
                                                equal_contrib_tag=None,
                                                raw_author_json={
                                                    "source": "dblp",
                                                    "author": {
                                                        "full_name": full_name,
                                                        "orcid": paper_orcid,
                                                        "affiliations": affiliations,
                                                    },
                                                },
                                                dry_run=dry_run,
                                                paper_orcid=paper_orcid,
                                            )
                                except Exception:
                                    stats.errors += 1
                                    LOGGER.exception(
                                        "Failed processing DBLP record for person_id=%s name=%r",
                                        person_id,
                                        name,
                                    )
                                    if not no_db and not dry_run:
                                        try:
                                            conn.rollback()
                                        except Exception:
                                            pass
                    except httpx.HTTPStatusError as exc:
                        stats.errors += 1
                        LOGGER.warning(
                            "DBLP request failed for person_id=%s name=%r (status=%s). Skipping.",
                            person_id,
                            name,
                            exc.response.status_code if exc.response else "n/a",
                        )

                    if max_records is not None and stats.processed >= max_records:
                        stop_all = True
                        break

                if stop_all:
                    break

                if not no_db and not dry_run:
                    conn.commit()

    return stats


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Search DBLP by author names from app.people and ingest into biblio.* tables."
    )
    ap.add_argument("--dsn", help="Optional PostgreSQL DSN (otherwise PG* env vars).")
    ap.add_argument("--query", help="Extra query terms applied to every person.")
    ap.add_argument(
        "--author-mode",
        choices=["author", "text", "auto"],
        default="auto",
        help="How to search author names (default: auto = author: then text fallback).",
    )
    ap.add_argument("--venue", action="append", default=[], help="Venue term (repeatable).")
    ap.add_argument("--series", action="append", default=[], help="Series code (e.g., lni).")
    ap.add_argument(
        "--series-mode",
        choices=["stream", "field", "text"],
        default="stream",
        help="How to apply --series (default: stream).",
    )
    ap.add_argument("--award", action="append", default=[], help="Grant/award term (repeatable).")
    ap.add_argument("--affiliation", action="append", default=[], help="Affiliation term (repeatable).")
    ap.add_argument("--address", action="append", default=[], help="Address term (repeatable).")
    ap.add_argument("--from-year", type=int, help="Start year (inclusive) for client-side filter.")
    ap.add_argument("--to-year", type=int, help="End year (inclusive) for client-side filter.")
    ap.add_argument("--count", type=int, default=100, help="Rows per request (DBLP 'h').")
    ap.add_argument("--max-records", type=int, help="Maximum total records to process.")
    ap.add_argument("--max-per-person", type=int, default=50, help="Maximum records per person.")
    ap.add_argument("--min-interval", type=float, default=1.0, help="Minimum seconds between API requests.")
    ap.add_argument(
        "--no-db",
        action="store_true",
        help="Not supported for this command (people are loaded from DB).",
    )
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
        default="dblp",
        help="Token to merge into source_of_truth (default: dblp).",
    )
    ap.add_argument("--rewrite-sot", help="Replace source_of_truth instead of merging.")
    ap.add_argument("--skip-existing", action="store_true", help="Skip DOIs that already exist.")
    ap.add_argument("--commit-every", type=int, default=50, help="Commit every N records (0 to disable).")
    ap.add_argument(
        "--only-person-id",
        help="Restrict to one or more person_ids (comma-separated or CSV file).",
    )
    ap.add_argument("--only-orcid", help="Restrict to a single ORCID.")
    ap.add_argument("--limit-people", type=int, help="Limit number of people processed.")
    ap.add_argument("--include-aliases", action="store_true", help="Include app.person_name_aliases.")
    ap.add_argument("--min-name-words", type=int, default=2, help="Skip names with fewer words.")
    ap.add_argument("--min-name-length", type=int, default=6, help="Skip very short names.")
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")

    only_person_ids = _parse_person_id_list(args.only_person_id)
    stats = run_dblp_search_people(
        dsn=args.dsn or DEFAULT_DSN,
        query=args.query,
        author_mode=args.author_mode,
        venues=args.venue,
        series=args.series,
        series_mode=args.series_mode,
        awards=args.award,
        affiliations=args.affiliation,
        addresses=args.address,
        from_year=args.from_year,
        to_year=args.to_year,
        count=args.count,
        max_records=args.max_records,
        max_per_person=args.max_per_person,
        min_interval=args.min_interval,
        no_db=args.no_db,
        dry_run=args.dry_run,
        debug=args.debug,
        debug_json=args.debug_json,
        source_token=str(args.source_token or "dblp"),
        rewrite_sot=args.rewrite_sot,
        skip_existing=args.skip_existing,
        refresh_authorships=args.refresh_authorships,
        commit_every=args.commit_every,
        only_person_ids=only_person_ids,
        only_orcid=args.only_orcid,
        limit_people=args.limit_people,
        include_aliases=args.include_aliases,
        min_name_words=args.min_name_words,
        min_name_length=args.min_name_length,
    )

    LOGGER.info(
        "dblp_search_people: people=%s names=%s processed=%s inserted=%s updated=%s skipped_no_doi=%s skipped_existing=%s errors=%s",
        stats.people,
        stats.names,
        stats.processed,
        stats.inserted,
        stats.updated,
        stats.skipped_no_doi,
        stats.skipped_existing,
        stats.errors,
    )


if __name__ == "__main__":
    main()
