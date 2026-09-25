#!/usr/bin/env python3
"""
people_pubs.sync.openalex_search

Search OpenAlex works by grant/award number and/or affiliation and ingest into
biblio.publications/authorships, mirroring the other searchers.

Examples:
  python -m people_pubs.sync.openalex_search \
    --award 12345678 \
    --affiliation "Example University" \
    --from-year 2019 --to-year 2025 \
    --count 100 --min-interval 0.2 \
    --source-token openalex-funding \
    --debug

If OpenAlex rejects the grant filter (HTTP 400), try:
  --award-mode search
or specify a different field:
  --award-field grants.award_number
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

from people_pubs.config import (
    DEFAULT_DSN,
    USER_AGENT,
    CROSSREF_MAILTO,
    has_more_trusted_source,
)
from people_pubs.db.connection import db_conn
from people_pubs.db.publications import upsert_publication, merge_raw_json
from people_pubs.db.authorships import compute_order_tag, upsert_authorship
from people_pubs.db.people import (
    lookup_internal_person_for_author,
    lookup_orcid_for_person_id,
    name_similarity_score,
    upsert_person_name_alias,
)
from people_pubs.services.crossref_client import normalize_doi
from people_pubs.sync.crossref_backfill import merge_source_of_truth
from people_pubs.utils.identifiers import normalize_orcid
from people_pubs.sync.scaffold import SearchStats, _lookup_existing_publication, _strip_orcid


LOGGER = logging.getLogger("people_pubs.openalex_search")
OPENALEX_BASE = "https://api.openalex.org"
OPENALEX_RAW_DISPLAY_MIN_SCORE = 0.8


def _iter_authors_from_openalex(
    work: Dict[str, Any],
) -> Iterable[Tuple[str, Optional[str], List[str], bool]]:
    for auth in work.get("authorships") or []:
        if not isinstance(auth, dict):
            continue
        author = auth.get("author") or {}
        raw_name = str(auth.get("raw_author_name") or "").strip()
        display_name = str(author.get("display_name") or "").strip()
        name = raw_name or display_name
        if raw_name and display_name:
            score = name_similarity_score(raw_name, display_name)
            if score < OPENALEX_RAW_DISPLAY_MIN_SCORE:
                LOGGER.debug(
                    "OpenAlex raw/display name mismatch (score=%.2f) raw=%r display=%r",
                    score,
                    raw_name,
                    display_name,
                )
        if not name:
            continue
        orcid = _strip_orcid(author.get("orcid"))
        affs: List[str] = []
        raw_affs = auth.get("raw_affiliation_strings") or []
        for item in raw_affs:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    affs.append(s)
        if not affs:
            for inst in auth.get("institutions") or []:
                if isinstance(inst, dict):
                    s = str(inst.get("display_name") or "").strip()
                    if s:
                        affs.append(s)
        is_corr = bool(auth.get("is_corresponding")) if "is_corresponding" in auth else False
        yield name, orcid, affs, is_corr


def _openalex_publication_fields(
    existing: Optional[Dict[str, Any]],
    *,
    title: Optional[str],
    year: Optional[int],
    venue: Optional[str],
    is_preprint: bool,
) -> Dict[str, Any]:
    """
    Return publication fields OpenAlex is allowed to send into upsert_publication.

    Existing rows with a more trusted source keep their metadata; OpenAlex only
    fills blanks. New or OpenAlex-only rows can use the OpenAlex values.
    """
    if not existing:
        return {
            "title": title,
            "year": year,
            "venue": venue,
            "is_preprint": bool(is_preprint),
        }
    trusted_existing = has_more_trusted_source(
        str(existing.get("source_of_truth") or ""),
        "openalex",
    )
    return {
        "title": title if title and (not trusted_existing or not existing.get("title")) else None,
        "year": year if year is not None and (not trusted_existing or existing.get("year") is None) else None,
        "venue": venue if venue and (not trusted_existing or not existing.get("venue")) else None,
        "is_preprint": bool(is_preprint) if not trusted_existing else None,
    }


def _allow_openalex_authorships(existing: Optional[Dict[str, Any]]) -> bool:
    return not existing or not has_more_trusted_source(
        str(existing.get("source_of_truth") or ""),
        "openalex",
    )


def _build_filter(
    *,
    award: Optional[str],
    affiliation: Optional[str],
    from_year: Optional[int],
    to_year: Optional[int],
    award_field: str,
) -> str:
    filters: List[str] = []
    if award:
        filters.append(f"{award_field}:{award}")
    if affiliation:
        filters.append(f"authorships.institutions.display_name.search:{affiliation}")
    if isinstance(from_year, int):
        filters.append(f"from_publication_date:{from_year}-01-01")
    if isinstance(to_year, int):
        filters.append(f"to_publication_date:{to_year}-12-31")
    return ",".join(filters)


def _iter_query_specs(
    awards: Sequence[str],
    affiliations: Sequence[str],
    award_mode: str,
    award_field: str,
    *,
    from_year: Optional[int],
    to_year: Optional[int],
) -> Iterable[Tuple[str, Optional[str], Dict[str, Any]]]:
    awards_list = list(awards) or [None]
    affiliations_list = list(affiliations) or [None]

    for award in awards_list:
        for affiliation in affiliations_list:
            filt = _build_filter(
                award=award if award_mode in {"filter", "both"} else None,
                affiliation=affiliation,
                from_year=from_year,
                to_year=to_year,
                award_field=award_field,
            )
            search = str(award) if (award and award_mode in {"query", "both"}) else None
            context = {
                "award": award,
                "affiliation": affiliation,
                "from_year": from_year,
                "to_year": to_year,
                "award_mode": award_mode,
                "award_field": award_field,
            }
            yield filt, search, context


def _iter_openalex_items(
    client: httpx.Client,
    filt: str,
    search: Optional[str],
    *,
    per_page: int,
    max_records: Optional[int],
    min_interval: float,
    debug_json: bool,
    mailto: Optional[str],
) -> Iterable[Dict[str, Any]]:
    fetched = 0
    cursor = "*"
    last_request = 0.0
    first_page = True

    while True:
        if min_interval > 0:
            elapsed = time.time() - last_request
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)

        params = {
            "filter": filt,
            "per-page": per_page,
            "cursor": cursor,
        }
        if search:
            params["search"] = search
        if mailto:
            params["mailto"] = mailto

        last_request = time.time()
        resp = client.get(f"{OPENALEX_BASE}/works", params=params)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = None
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(
                f"OpenAlex request failed (status={resp.status_code} filter={filt!r} search={search!r}): {detail}"
            ) from exc
        payload = resp.json() or {}
        items = payload.get("results") or []

        if debug_json and first_page:
            LOGGER.info("OpenAlex response JSON (first page):\n%s", json.dumps(payload, indent=2)[:20000])
        first_page = False

        for item in items:
            yield item
            fetched += 1
            if max_records is not None and fetched >= max_records:
                return

        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor or not items:
            return


def run_openalex_search(
    *,
    dsn: Optional[str],
    awards: Sequence[str],
    affiliations: Sequence[str],
    award_mode: str,
    award_field: str,
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

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=30.0, http2=True) as client:
        with db_conn(dsn) if not no_db else nullcontext() as conn:
            for filt, search, context in _iter_query_specs(
                awards,
                affiliations,
                award_mode=award_mode,
                award_field=award_field,
                from_year=from_year,
                to_year=to_year,
            ):
                if max_records is not None and stats.processed >= max_records:
                    break
                remaining = None
                if max_records is not None:
                    remaining = max_records - stats.processed
                    if remaining <= 0:
                        break
                LOGGER.info("OpenAlex filter=%s search=%s", filt, search)
                for item in _iter_openalex_items(
                    client,
                    filt,
                    search,
                    per_page=count,
                    max_records=remaining,
                    min_interval=min_interval,
                    debug_json=debug_json,
                    mailto=CROSSREF_MAILTO,
                ):
                    stats.processed += 1
                    try:
                        doi_raw = item.get("doi") or (item.get("ids") or {}).get("doi") or ""
                        doi = normalize_doi(doi_raw)
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

                        title = str(item.get("title") or "").strip()
                        venue = None
                        host = item.get("host_venue") or {}
                        if isinstance(host, dict):
                            venue = (host.get("display_name") or "").strip() or None
                        if not venue:
                            primary = (item.get("primary_location") or {}).get("source") or {}
                            if isinstance(primary, dict):
                                venue = (primary.get("display_name") or "").strip() or None
                        year = item.get("publication_year")
                        is_preprint = str(item.get("type") or "").lower() == "preprint"
                        publication_fields = _openalex_publication_fields(
                            existing,
                            title=title or None,
                            year=year,
                            venue=venue,
                            is_preprint=is_preprint,
                        )

                        existing_sot = existing.get("source_of_truth") if existing else ""
                        existing_raw = existing.get("raw_json") if existing else None
                        if rewrite_sot:
                            sot = rewrite_sot
                        else:
                            sot = merge_source_of_truth(existing_sot, source_token)

                        search_ctx = {
                            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "filter": filt,
                            "context": context,
                        }
                        merged_raw = merge_raw_json(
                            existing_raw,
                            str(existing_sot or ""),
                            {"openalex": item, "openalex_search": search_ctx},
                        )

                        pub_dict = {
                            "doi": doi,
                            "raw_crossref_json": None,
                            "raw_orcid_json": None,
                            "raw_scholar_json": None,
                            "raw_openalex_json": merged_raw,
                            "source_of_truth": sot,
                            "replace_source_of_truth": bool(rewrite_sot),
                        }
                        for field, value in publication_fields.items():
                            if value is not None:
                                pub_dict[field] = value

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

                        if publication_id and _allow_openalex_authorships(existing):
                            authors = list(_iter_authors_from_openalex(item))
                            n_authors = len(authors)
                            for pos, (full_name, auth_orcid, affiliations, is_corr) in enumerate(
                                authors, start=1
                            ):
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
                                        source="openalex.author_name",
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
                                    is_corresponding=bool(is_corr),
                                    order_tag=order_tag,
                                    equal_contrib_tag=None,
                                    raw_author_json={
                                        "source": "openalex",
                                        "author": {
                                            "full_name": full_name,
                                            "orcid": paper_orcid,
                                            "affiliations": affiliations,
                                        },
                                    },
                                    dry_run=dry_run,
                                    paper_orcid=paper_orcid,
                                )
                        elif publication_id and refresh_authorships:
                            LOGGER.info(
                                "Skipping OpenAlex authorship refresh for pub_id=%s because a more trusted source is present",
                                publication_id,
                            )

                        if not no_db and not dry_run and commit_every and stats.processed % commit_every == 0:
                            conn.commit()

                    except Exception:
                        stats.errors += 1
                        LOGGER.exception("Failed processing OpenAlex record (doi=%s)", item.get("doi"))
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
        description="Search OpenAlex by grant/affiliation and ingest into biblio.* tables."
    )
    ap.add_argument("--dsn", help="Optional PostgreSQL DSN (otherwise PG* env vars).")
    ap.add_argument("--award", action="append", default=[], help="Award/grant number (repeatable).")
    ap.add_argument("--affiliation", action="append", default=[], help="Affiliation search term (repeatable).")
    ap.add_argument(
        "--award-mode",
        choices=["filter", "query", "both"],
        default="filter",
        help="How to apply award numbers in OpenAlex (default: filter).",
    )
    ap.add_argument(
        "--award-field",
        default="grants.award_number",
        help="OpenAlex filter field for award numbers (default: grants.award_number).",
    )
    ap.add_argument("--from-year", type=int, help="Start year (inclusive) for publication date filter.")
    ap.add_argument("--to-year", type=int, help="End year (inclusive) for publication date filter.")
    ap.add_argument("--count", type=int, default=100, help="Rows per request (OpenAlex per-page).")
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
        default="openalex",
        help="Token to merge into source_of_truth (default: openalex).",
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

    if not (args.award or args.affiliation):
        raise SystemExit("Provide at least one of --award or --affiliation.")

    stats = run_openalex_search(
        dsn=args.dsn or DEFAULT_DSN,
        awards=args.award,
        affiliations=args.affiliation,
        award_mode=str(args.award_mode or "filter"),
        award_field=str(args.award_field or "grants.award_number"),
        from_year=args.from_year,
        to_year=args.to_year,
        count=args.count,
        max_records=args.max_records,
        min_interval=args.min_interval,
        no_db=bool(args.no_db),
        dry_run=bool(args.dry_run),
        debug=bool(args.debug),
        debug_json=bool(args.debug_json),
        source_token=str(args.source_token or "openalex"),
        rewrite_sot=args.rewrite_sot or None,
        skip_existing=bool(args.skip_existing),
        refresh_authorships=bool(args.refresh_authorships),
        commit_every=int(args.commit_every or 0),
    )

    LOGGER.info(
        "openalex_search: processed=%s inserted=%s updated=%s skipped_no_doi=%s skipped_existing=%s errors=%s",
        stats.processed,
        stats.inserted,
        stats.updated,
        stats.skipped_no_doi,
        stats.skipped_existing,
        stats.errors,
    )


if __name__ == "__main__":
    main()
