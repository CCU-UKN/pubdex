#!/usr/bin/env python3
"""
people_pubs.sync.dblp_search

Search DBLP by query/series/venue/author/title and ingest into
biblio.publications/authorships, similar to the Crossref/OpenAlex searchers.

Notes:
- DBLP does not expose structured funding/affiliation/address fields.
  Any --award/--affiliation/--address terms are added as plain-text query terms.
- Year filtering is applied client-side after results are fetched.

Examples:
  # LNI series (DBLP stream query)
  python -m people_pubs.sync.dblp_search \
    --series lni --series-mode stream \
    --from-year 2019 --to-year 2025 \
    --count 100 --min-interval 0.2 \
    --source-token dblp-lni \
    --debug

  # Free-text query
  python -m people_pubs.sync.dblp_search \
    --query "Neural Texture Puppeteer" \
    --count 50 --min-interval 0.2 \
    --source-token dblp \
    --debug
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

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
from people_pubs.sync.scaffold import SearchStats


LOGGER = logging.getLogger("people_pubs.dblp_search")
DBLP_BASE = "https://dblp.org"
DBLP_MAX_RETRIES = 6
DBLP_BACKOFF_BASE = 1.0
DBLP_BACKOFF_MAX = 16.0


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _clean_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    t = _strip_html(str(title))
    if t.endswith("."):
        t = t[:-1].rstrip()
    return t or None


def _quote_term(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    v = v.replace('"', "'")
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


def _format_terms(values: Sequence[str], *, prefix: Optional[str] = None) -> Optional[str]:
    terms: List[str] = []
    for v in values:
        t = _quote_term(v)
        if not t:
            continue
        if prefix:
            terms.append(f"{prefix}:{t}")
        else:
            terms.append(t)
    return _or_group(terms)


def _format_series(values: Sequence[str], mode: str) -> Optional[str]:
    terms: List[str] = []
    for v in values:
        raw = (v or "").strip()
        if not raw:
            continue
        if mode == "stream":
            if raw.startswith("stream:"):
                term = raw
            else:
                if raw.startswith("series/") or "/" in raw:
                    term = f"stream:{raw}"
                else:
                    term = f"stream:series/{raw}"
        elif mode == "field":
            term = f"series:{_quote_term(raw)}"
        else:
            term = _quote_term(raw)
        terms.append(term)
    return _or_group(terms)


def _build_query(
    *,
    query: Optional[str],
    titles: Sequence[str],
    authors: Sequence[str],
    venues: Sequence[str],
    series: Sequence[str],
    series_mode: str,
    dois: Sequence[str],
    awards: Sequence[str],
    affiliations: Sequence[str],
    addresses: Sequence[str],
) -> str:
    parts: List[str] = []
    if query:
        parts.append(f"({query})")
    title_term = _format_terms(titles, prefix="title")
    if title_term:
        parts.append(title_term)
    author_term = _format_terms(authors, prefix="author")
    if author_term:
        parts.append(author_term)
    venue_term = _format_terms(venues, prefix="venue")
    if venue_term:
        parts.append(venue_term)
    series_term = _format_series(series, series_mode)
    if series_term:
        parts.append(series_term)
    doi_term = _format_terms(dois, prefix="doi")
    if doi_term:
        parts.append(doi_term)
    free_terms = list(awards) + list(affiliations) + list(addresses)
    free_term = _format_terms(free_terms, prefix=None)
    if free_term:
        parts.append(free_term)
    if not parts:
        raise SystemExit("DBLP search requires at least one query/term.")
    return " AND ".join(parts)


def _parse_year(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.search(r"\d{4}", value)
        if m:
            try:
                return int(m.group(0))
            except Exception:
                return None
    return None


def _extract_doi(info: Dict[str, Any]) -> Optional[str]:
    doi = info.get("doi")
    if isinstance(doi, str) and doi.strip():
        return normalize_doi(doi) or doi.strip()
    ee = info.get("ee")
    if isinstance(ee, list):
        candidates = ee
    elif isinstance(ee, str):
        candidates = [ee]
    else:
        candidates = []
    for url in candidates:
        if not isinstance(url, str):
            continue
        if "doi.org/" in url:
            return normalize_doi(url) or url
        if url.lower().startswith("doi:"):
            return normalize_doi(url) or url
    return None


def _iter_authors_from_dblp(info: Dict[str, Any]) -> Iterable[Tuple[str, Optional[str], List[str]]]:
    authors = (info.get("authors") or {}).get("author")
    if not authors:
        return
    items = authors if isinstance(authors, list) else [authors]
    for a in items:
        name = None
        if isinstance(a, dict):
            name = a.get("text") or a.get("name")
        elif isinstance(a, str):
            name = a
        if not name:
            continue
        yield str(name).strip(), None, []


def _info_title(info: Dict[str, Any]) -> Optional[str]:
    return _clean_title(info.get("title"))


def _info_venue(info: Dict[str, Any]) -> Optional[str]:
    for key in ("venue", "booktitle", "journal", "series"):
        raw = info.get(key)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        elif isinstance(raw, str):
            if raw.strip():
                return raw.strip()
        elif raw:
            try:
                text = str(raw).strip()
            except Exception:
                text = ""
            if text:
                return text
    return None


def _iter_dblp_hits(
    client: httpx.Client,
    query: str,
    *,
    rows: int,
    max_records: Optional[int],
    min_interval: float,
    debug_json: bool,
    last_request: Optional[List[float]] = None,
) -> Iterable[Dict[str, Any]]:
    fetched = 0
    start = 0
    last_request_ts = last_request
    if last_request_ts is None:
        last_request_ts = [0.0]
    first_page = True

    while True:
        params = {"q": query, "format": "json", "h": rows, "f": start}
        attempt = 0
        while True:
            if min_interval > 0:
                elapsed = time.time() - last_request_ts[0]
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)

            last_request_ts[0] = time.time()
            resp = client.get(f"{DBLP_BASE}/search/publ/api", params=params)
            if resp.status_code == 429:
                attempt += 1
                if attempt > DBLP_MAX_RETRIES:
                    resp.raise_for_status()
                retry_after = resp.headers.get("Retry-After")
                wait = None
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = None
                if wait is None:
                    wait = min(DBLP_BACKOFF_BASE * (2 ** (attempt - 1)), DBLP_BACKOFF_MAX)
                LOGGER.warning(
                    "DBLP rate limited (429). Sleeping %.1fs before retry %s/%s.",
                    wait,
                    attempt,
                    DBLP_MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                attempt += 1
                if attempt > DBLP_MAX_RETRIES:
                    resp.raise_for_status()
                wait = min(DBLP_BACKOFF_BASE * (2 ** (attempt - 1)), DBLP_BACKOFF_MAX)
                LOGGER.warning(
                    "DBLP error %s. Sleeping %.1fs before retry %s/%s.",
                    resp.status_code,
                    wait,
                    attempt,
                    DBLP_MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            payload = resp.json() or {}
            break
        if debug_json and first_page:
            LOGGER.info("DBLP response JSON (first page):\n%s", json.dumps(payload, indent=2)[:20000])
        first_page = False

        hits = ((payload.get("result") or {}).get("hits") or {})
        hit_list = hits.get("hit") or []
        if isinstance(hit_list, dict):
            hit_list = [hit_list]
        sent = int(hits.get("@sent", len(hit_list)) or len(hit_list))
        total = int(hits.get("@total", 0) or 0)

        for hit in hit_list:
            info = hit.get("info") if isinstance(hit, dict) else None
            if not isinstance(info, dict):
                continue
            yield info
            fetched += 1
            if max_records is not None and fetched >= max_records:
                return

        if not hit_list:
            return
        start += sent
        if start >= total:
            return


def run_dblp_search(
    *,
    dsn: Optional[str],
    query: Optional[str],
    titles: Sequence[str],
    authors: Sequence[str],
    venues: Sequence[str],
    series: Sequence[str],
    series_mode: str,
    dois: Sequence[str],
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

    if awards or affiliations or addresses:
        LOGGER.warning(
            "DBLP does not support structured award/affiliation/address filters; using plain text terms."
        )

    stats = SearchStats()
    seen_dois: set[str] = set()
    built_query = _build_query(
        query=query,
        titles=titles,
        authors=authors,
        venues=venues,
        series=series,
        series_mode=series_mode,
        dois=dois,
        awards=awards,
        affiliations=affiliations,
        addresses=addresses,
    )

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=30.0, http2=True) as client:
        with db_conn(dsn) if not no_db else nullcontext() as conn:
            throttle = [0.0]
            for info in _iter_dblp_hits(
                client,
                built_query,
                rows=count,
                max_records=max_records,
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
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                SELECT pub_id, doi, title, year, venue, source_of_truth, raw_json
                                FROM biblio.publications
                                WHERE doi = %s
                                """,
                                (doi,),
                            )
                            row = cur.fetchone()
                        if row:
                            try:
                                existing = dict(row)
                            except Exception:
                                existing = {
                                    "pub_id": row[0],
                                    "doi": row[1],
                                    "title": row[2],
                                    "year": row[3],
                                    "venue": row[4],
                                    "source_of_truth": row[5],
                                    "raw_json": row[6],
                                }

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
                        "query": built_query,
                        "filters": {
                            "from_year": from_year,
                            "to_year": to_year,
                        },
                    }
                    merged_raw = merge_raw_json(
                        existing_raw,
                        str(existing_sot or ""),
                        {"dblp": info, "dblp_search": search_ctx},
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
                    LOGGER.exception("Failed processing DBLP record")
                    if not no_db and not dry_run:
                        try:
                            conn.rollback()
                        except Exception:
                            pass

            if not no_db and not dry_run:
                conn.commit()

    return stats


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Search DBLP and ingest into biblio.* tables."
    )
    ap.add_argument("--dsn", help="Optional PostgreSQL DSN (otherwise PG* env vars).")
    ap.add_argument("--query", help="Raw DBLP query string (combined with other terms).")
    ap.add_argument("--title", action="append", default=[], help="Title term (repeatable).")
    ap.add_argument("--author", action="append", default=[], help="Author term (repeatable).")
    ap.add_argument("--venue", action="append", default=[], help="Venue term (repeatable).")
    ap.add_argument("--series", action="append", default=[], help="Series code (e.g., lni).")
    ap.add_argument(
        "--series-mode",
        choices=["stream", "field", "text"],
        default="stream",
        help="How to apply --series (default: stream).",
    )
    ap.add_argument("--doi", action="append", default=[], help="DOI term (repeatable).")
    ap.add_argument("--award", action="append", default=[], help="Grant/award term (repeatable).")
    ap.add_argument("--affiliation", action="append", default=[], help="Affiliation term (repeatable).")
    ap.add_argument("--address", action="append", default=[], help="Address term (repeatable).")
    ap.add_argument("--from-year", type=int, help="Start year (inclusive) for client-side filter.")
    ap.add_argument("--to-year", type=int, help="End year (inclusive) for client-side filter.")
    ap.add_argument("--count", type=int, default=100, help="Rows per request (DBLP 'h').")
    ap.add_argument("--max-records", type=int, help="Maximum total records to process.")
    ap.add_argument("--min-interval", type=float, default=1.0, help="Minimum seconds between API requests.")
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
        default="dblp",
        help="Token to merge into source_of_truth (default: dblp).",
    )
    ap.add_argument(
        "--rewrite-sot",
        help="Replace source_of_truth instead of merging.",
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip DOIs that already exist in biblio.publications.",
    )
    ap.add_argument(
        "--commit-every",
        type=int,
        default=50,
        help="Commit every N records (0 to disable).",
    )
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")

    stats = run_dblp_search(
        dsn=args.dsn or DEFAULT_DSN,
        query=args.query,
        titles=args.title,
        authors=args.author,
        venues=args.venue,
        series=args.series,
        series_mode=args.series_mode,
        dois=args.doi,
        awards=args.award,
        affiliations=args.affiliation,
        addresses=args.address,
        from_year=args.from_year,
        to_year=args.to_year,
        count=args.count,
        max_records=args.max_records,
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
    )

    LOGGER.info(
        "dblp_search: processed=%s inserted=%s updated=%s skipped_no_doi=%s skipped_existing=%s errors=%s",
        stats.processed,
        stats.inserted,
        stats.updated,
        stats.skipped_no_doi,
        stats.skipped_existing,
        stats.errors,
    )


if __name__ == "__main__":
    main()
