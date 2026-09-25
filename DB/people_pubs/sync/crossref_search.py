#!/usr/bin/env python3
"""
people_pubs.sync.crossref_search

Search Crossref works by grant/award number and/or affiliation and ingest into
biblio.publications/authorships, mirroring the other searchers.

Examples:
  export PEOPLE_PUBS_CROSSREF_MAILTO="you@example.org"
  python -m people_pubs.sync.crossref_search \
    --award 12345678 \
    --affiliation "Example University" \
    --from-year 2019 --to-year 2025 \
    --count 100 --min-interval 0.2 \
    --source-token crossref-funding \
    --debug

If Crossref rejects award filters (HTTP 400), retry with:
  --award-mode query
"""

from __future__ import annotations

import argparse
import csv
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

from people_pubs.config import DEFAULT_DSN, USER_AGENT, CROSSREF_MAILTO
from people_pubs.db.connection import db_conn
from people_pubs.db.publications import upsert_publication, merge_raw_json
from people_pubs.db.authorships import compute_order_tag, infer_equal_contrib_tag, upsert_authorship
from people_pubs.db.people import (
    lookup_internal_person_for_author,
    lookup_orcid_for_person_id,
    upsert_person_name_alias,
)
from people_pubs.services.crossref_client import (
    normalize_doi,
    pick_publication_date,
    guess_is_preprint,
)
from people_pubs.sync.crossref_backfill import merge_source_of_truth
from people_pubs.utils.identifiers import normalize_orcid
from people_pubs.sync.scaffold import SearchStats, _lookup_existing_publication, _parse_person_id_list, _strip_orcid


LOGGER = logging.getLogger("people_pubs.crossref_search")

_CROSSREF_MAX_PAGE_RETRIES = 3
_CROSSREF_RETRY_BACKOFF_SECONDS = 2.0


def _load_author_orcids_for_people(conn, person_ids: Sequence[int]) -> List[str]:
    if not person_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT io.orcid
            FROM app.identities_orcid io
            WHERE io.person_id = ANY(%s)
              AND io.orcid IS NOT NULL
              AND io.orcid <> ''
            ORDER BY io.person_id
            """,
            (list(person_ids),),
        )
        rows = cur.fetchall()
    out: List[str] = []
    seen: set[str] = set()
    for row in rows:
        try:
            raw_orcid = row["orcid"]  # dict_row
        except Exception:
            raw_orcid = row[0]
        norm = _strip_orcid(str(raw_orcid or ""))
        if not norm:
            continue
        norm_l = norm.lower()
        if norm_l in seen:
            continue
        seen.add(norm_l)
        out.append(norm)
    return out


def _iter_authors_from_crossref(
    cr_msg: Dict[str, Any],
) -> Iterable[Tuple[str, Optional[str], List[str], Dict[str, Any]]]:
    for a in cr_msg.get("author", []) or []:
        if not isinstance(a, dict):
            continue
        given = str(a.get("given") or "").strip()
        family = str(a.get("family") or "").strip()
        name = " ".join([x for x in [given, family] if x]).strip()
        if not name:
            name = str(a.get("name") or "").strip()
        if not name:
            continue
        affs: List[str] = []
        aff_raw = a.get("affiliation") or []
        if isinstance(aff_raw, list):
            for item in aff_raw:
                if isinstance(item, dict):
                    n = str(item.get("name") or "").strip()
                    if n:
                        affs.append(n)
                elif isinstance(item, str):
                    s = item.strip()
                    if s:
                        affs.append(s)
        yield name, _strip_orcid(a.get("ORCID")), affs, a


def _normalize_affiliation_text(value: str) -> str:
    s = str(value or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _affiliation_terms_match(candidate_affiliations: Sequence[str], expected_terms: Sequence[str]) -> bool:
    terms = [_normalize_affiliation_text(t) for t in expected_terms if str(t or "").strip()]
    terms = [t for t in terms if t]
    if not terms:
        return True
    for aff in candidate_affiliations:
        aff_norm = _normalize_affiliation_text(aff)
        if not aff_norm:
            continue
        for term in terms:
            if term in aff_norm:
                return True
    return False


def _crossref_item_has_expected_affiliation(item: Dict[str, Any], expected_terms: Sequence[str]) -> bool:
    all_affs: List[str] = []
    for _name, _orcid, affs, _raw_author in _iter_authors_from_crossref(item):
        all_affs.extend(affs)
    return _affiliation_terms_match(all_affs, expected_terms)


def _build_filter(
    *,
    award: Optional[str],
    funder: Optional[str],
    author_orcid: Optional[str],
    from_year: Optional[int],
    to_year: Optional[int],
) -> Optional[str]:
    filters: List[str] = []
    if award:
        # Crossref works route expects award.number (not legacy award).
        filters.append(f"award.number:{award}")
    if funder:
        filters.append(f"funder:{funder}")
    if author_orcid:
        filters.append(f"orcid:{author_orcid}")
    if isinstance(from_year, int):
        filters.append(f"from-pub-date:{from_year}-01-01")
    if isinstance(to_year, int):
        filters.append(f"until-pub-date:{to_year}-12-31")
    return ",".join(filters) if filters else None


def _iter_query_specs(
    awards: Sequence[str],
    affiliations: Sequence[str],
    funders: Sequence[str],
    author_orcids: Sequence[str],
    award_mode: str,
    *,
    from_year: Optional[int],
    to_year: Optional[int],
) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any]]]:
    awards_list = list(awards) or [None]
    affiliations_list = list(affiliations) or [None]
    funders_list = list(funders) or [None]
    author_orcids_list = list(author_orcids) or [None]

    for award in awards_list:
        for affiliation in affiliations_list:
            for funder in funders_list:
                for author_orcid in author_orcids_list:
                    params: Dict[str, Any] = {}
                    filt = _build_filter(
                        award=award if award_mode in {"filter", "both"} else None,
                        funder=funder,
                        author_orcid=author_orcid,
                        from_year=from_year,
                        to_year=to_year,
                    )
                    if filt:
                        params["filter"] = filt
                    if affiliation:
                        params["query.affiliation"] = affiliation
                    if award and award_mode in {"query", "both"}:
                        params["query"] = str(award)

                    context = {
                        "award": award,
                        "affiliation": affiliation,
                        "funder": funder,
                        "author_orcid": author_orcid,
                        "from_year": from_year,
                        "to_year": to_year,
                        "award_mode": award_mode,
                    }
                    yield params, context


def _iter_crossref_items(
    client: httpx.Client,
    params: Dict[str, Any],
    *,
    rows: int,
    max_records: Optional[int],
    min_interval: float,
    debug_json: bool,
    mailto: Optional[str],
) -> Iterable[Dict[str, Any]]:
    fetched = 0
    cursor = "*"
    last_request = 0.0
    first_page = True
    seen_cursors: set[str] = set()

    while True:
        if cursor in seen_cursors:
            LOGGER.warning(
                "Crossref returned a repeated cursor; stopping pagination. params=%s cursor=%s",
                params,
                cursor[:80],
            )
            return
        seen_cursors.add(cursor)

        if min_interval > 0:
            elapsed = time.time() - last_request
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)

        page_params = dict(params)
        page_params["rows"] = rows
        page_params["cursor"] = cursor
        if mailto:
            page_params["mailto"] = mailto

        resp = None
        for attempt in range(1, _CROSSREF_MAX_PAGE_RETRIES + 1):
            try:
                last_request = time.time()
                resp = client.get("https://api.crossref.org/works", params=page_params)
                break
            except httpx.TransportError as exc:
                if attempt >= _CROSSREF_MAX_PAGE_RETRIES:
                    raise RuntimeError(
                        f"Crossref request failed after {_CROSSREF_MAX_PAGE_RETRIES} transport attempts: {exc}"
                    ) from exc
                delay = min(8.0, _CROSSREF_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                LOGGER.warning(
                    "Crossref transport error. Sleeping %.1fs before retry %s/%s. params=%s error=%s",
                    delay,
                    attempt,
                    _CROSSREF_MAX_PAGE_RETRIES,
                    params,
                    exc,
                )
                time.sleep(delay)

        assert resp is not None
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = None
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(
                f"Crossref request failed (status={resp.status_code}): {detail}"
            ) from exc
        payload = resp.json() or {}
        message = payload.get("message") or {}
        items = message.get("items") or []

        if debug_json and first_page:
            LOGGER.info("Crossref response JSON (first page):\n%s", json.dumps(payload, indent=2)[:20000])
        first_page = False

        for item in items:
            yield item
            fetched += 1
            if max_records is not None and fetched >= max_records:
                return

        cursor = message.get("next-cursor")
        if not cursor or not items:
            return


def _estimate_label(context: Dict[str, Any]) -> str:
    """Short human label for one query spec, for --estimate output."""
    parts: List[str] = []
    for key in ("award", "affiliation", "funder", "author_orcid"):
        val = context.get(key)
        if val:
            parts.append(f"{key}={val!r}")
    return ", ".join(parts) or "(all)"


def _crossref_estimate_total(
    client: httpx.Client,
    params: Dict[str, Any],
    *,
    mailto: Optional[str],
) -> Optional[int]:
    """Fetch only Crossref's total-results for a query spec (rows=0, no items)."""
    page_params = dict(params)
    page_params["rows"] = 0
    if mailto:
        page_params["mailto"] = mailto
    resp = client.get("https://api.crossref.org/works", params=page_params)
    resp.raise_for_status()
    payload = resp.json() or {}
    message = payload.get("message") or {}
    total = message.get("total-results")
    try:
        return int(total)
    except (TypeError, ValueError):
        return None


def run_crossref_search(
    *,
    dsn: Optional[str],
    awards: Sequence[str],
    affiliations: Sequence[str],
    funders: Sequence[str],
    author_orcids: Sequence[str],
    only_person_ids: Optional[Sequence[int]],
    from_year: Optional[int],
    to_year: Optional[int],
    count: int,
    max_records: Optional[int],
    max_new_records: Optional[int],
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
    award_mode: str,
    require_affiliation_match: bool,
    estimate: bool = False,
) -> SearchStats:
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # --estimate is a read-only count probe; it never touches the DB unless it
    # must resolve ORCIDs for a --only-person-id scope.
    if estimate and not only_person_ids:
        no_db = True

    if no_db and not dry_run:
        LOGGER.warning("No DB connection requested; forcing dry-run (no writes).")
        dry_run = True

    if no_db and skip_existing:
        LOGGER.warning("--skip-existing ignored because --no-db is set.")
        skip_existing = False
    if no_db and refresh_authorships:
        LOGGER.warning("--refresh-authorships ignored because --no-db is set.")
        refresh_authorships = False
    if no_db and max_new_records is not None:
        LOGGER.warning("--max-new-records ignored because --no-db is set.")
        max_new_records = None

    stats = SearchStats()
    new_applied = 0
    seen_dois: set[str] = set()

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=30.0, http2=True) as client:
        with db_conn(dsn) if not no_db else nullcontext() as conn:
            resolved_orcids: List[str] = []
            seen_orcids: set[str] = set()

            for raw in author_orcids or []:
                norm = _strip_orcid(raw)
                if not norm:
                    continue
                norm_l = norm.lower()
                if norm_l in seen_orcids:
                    continue
                seen_orcids.add(norm_l)
                resolved_orcids.append(norm)

            if only_person_ids:
                if no_db:
                    raise SystemExit("--only-person-id requires DB access (remove --no-db or pass --author-orcid).")
                db_orcids = _load_author_orcids_for_people(conn, only_person_ids)
                for norm in db_orcids:
                    norm_l = norm.lower()
                    if norm_l in seen_orcids:
                        continue
                    seen_orcids.add(norm_l)
                    resolved_orcids.append(norm)
                if not db_orcids:
                    LOGGER.warning("No ORCID values found for --only-person-id selection; no queries will run.")

            if not (awards or affiliations or funders or resolved_orcids):
                LOGGER.warning("No effective Crossref query constraints resolved; nothing to do.")
                return stats

            if estimate:
                grand_total = 0
                n_queries = 0
                for params, context in _iter_query_specs(
                    awards,
                    affiliations,
                    funders,
                    resolved_orcids,
                    award_mode=award_mode,
                    from_year=from_year,
                    to_year=to_year,
                ):
                    n_queries += 1
                    if min_interval > 0 and n_queries > 1:
                        time.sleep(min_interval)
                    total = _crossref_estimate_total(client, params, mailto=CROSSREF_MAILTO)
                    LOGGER.info(
                        "ESTIMATE crossref %s: %s matching records (NOT ingested; raw API match before "
                        "--require-affiliation-match / --max-new-records)",
                        _estimate_label(context),
                        total if total is not None else "unknown",
                    )
                    if total:
                        grand_total += total
                LOGGER.info(
                    "ESTIMATE crossref TOTAL (sum over %s queries): %s matching records (NOT ingested)",
                    n_queries,
                    grand_total,
                )
                return stats

            for params, context in _iter_query_specs(
                awards,
                affiliations,
                funders,
                resolved_orcids,
                award_mode=award_mode,
                from_year=from_year,
                to_year=to_year,
            ):
                if max_records is not None and stats.processed >= max_records:
                    break
                if max_new_records is not None and new_applied >= max_new_records:
                    break
                remaining = None
                if max_records is not None:
                    remaining = max_records - stats.processed
                    if remaining <= 0:
                        break
                LOGGER.info("Crossref search params=%s", params)
                stop_now = False
                for item in _iter_crossref_items(
                    client,
                    params,
                    rows=count,
                    max_records=remaining,
                    min_interval=min_interval,
                    debug_json=debug_json,
                    mailto=CROSSREF_MAILTO,
                ):
                    stats.processed += 1
                    try:
                        if max_new_records is not None and new_applied >= max_new_records:
                            stop_now = True
                            break
                        if require_affiliation_match and affiliations:
                            if not _crossref_item_has_expected_affiliation(item, affiliations):
                                stats.skipped_affiliation_mismatch += 1
                                continue

                        doi = normalize_doi(item.get("DOI") or "")
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

                        title_list = item.get("title") or []
                        title = (
                            title_list[0].strip()
                            if isinstance(title_list, list) and title_list
                            else (item.get("title") or "")
                        )
                        venue_list = item.get("container-title") or []
                        venue = (
                            venue_list[0].strip()
                            if isinstance(venue_list, list) and venue_list
                            else (item.get("container-title") or None)
                        )
                        pub_date = pick_publication_date(item, fallback_year=None)
                        year = pub_date.year if pub_date else None
                        is_preprint = guess_is_preprint(item, doi=doi, venue=venue, title=title)

                        existing_sot = existing.get("source_of_truth") if existing else ""
                        existing_raw = existing.get("raw_json") if existing else None
                        if rewrite_sot:
                            sot = rewrite_sot
                        else:
                            sot = merge_source_of_truth(existing_sot, source_token)

                        search_ctx = {
                            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "params": params,
                            "context": context,
                        }
                        merged_raw = merge_raw_json(
                            existing_raw,
                            str(existing_sot or ""),
                            {"crossref": item, "crossref_search": search_ctx},
                        )

                        pub_dict = {
                            "doi": doi,
                            "title": title,
                            "year": year,
                            "venue": venue,
                            "raw_crossref_json": merged_raw,
                            "raw_orcid_json": None,
                            "raw_scholar_json": None,
                            "is_preprint": bool(is_preprint),
                            "source_of_truth": sot,
                            "replace_source_of_truth": bool(rewrite_sot),
                        }

                        if no_db:
                            LOGGER.debug("No DB: extracted doi=%s title=%r year=%r", doi, title, year)
                            continue

                        publication_id = None
                        publication_changed = False
                        if skip_publication_update:
                            publication_id = existing.get("pub_id") if existing else None
                        elif dry_run:
                            if existing:
                                stats.updated += 1
                            else:
                                stats.inserted += 1
                            publication_changed = True
                        else:
                            publication_id = upsert_publication(conn, pub_dict, dry_run=dry_run)
                            if publication_id:
                                if existing:
                                    stats.updated += 1
                                else:
                                    stats.inserted += 1
                                publication_changed = True

                        if publication_id:
                            authors = list(_iter_authors_from_crossref(item))
                            n_authors = len(authors)
                            for pos, (full_name, auth_orcid, affiliations, raw_author) in enumerate(authors, start=1):
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
                                        source="crossref.author_name",
                                        dry_run=dry_run,
                                    )
                                order_tag = compute_order_tag(pos, n_authors)
                                equal_contrib_tag = infer_equal_contrib_tag(raw_author, order_tag)
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
                                    equal_contrib_tag=equal_contrib_tag,
                                    raw_author_json={
                                        "source": "crossref",
                                        "author": raw_author,
                                    },
                                    dry_run=dry_run,
                                    paper_orcid=paper_orcid,
                                )

                        if not no_db and not dry_run and commit_every and stats.processed % commit_every == 0:
                            conn.commit()

                        if publication_changed:
                            new_applied += 1
                            if max_new_records is not None and new_applied >= max_new_records:
                                LOGGER.info("Reached --max-new-records=%s; stopping.", max_new_records)
                                stop_now = True
                                break

                    except Exception:
                        stats.errors += 1
                        LOGGER.exception("Failed processing Crossref record (doi=%s)", item.get("DOI"))
                        if not no_db and not dry_run:
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                if stop_now:
                    break

            if not no_db and not dry_run:
                conn.commit()

    return stats


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Search Crossref by award/funder/affiliation/ORCID and ingest into biblio.* tables."
    )
    ap.add_argument("--dsn", help="Optional PostgreSQL DSN (otherwise PG* env vars).")
    ap.add_argument("--award", action="append", default=[], help="Award/grant number (repeatable).")
    ap.add_argument("--funder", action="append", default=[], help="Funder DOI (repeatable).")
    ap.add_argument("--affiliation", action="append", default=[], help="Affiliation search term (repeatable).")
    ap.add_argument(
        "--author-orcid",
        action="append",
        default=[],
        help="Author ORCID to filter in Crossref (repeatable).",
    )
    ap.add_argument(
        "--only-person-id",
        help="Restrict ORCID filters to person_id(s): single id, CSV file, or comma-separated list.",
    )
    ap.add_argument(
        "--award-mode",
        choices=["filter", "query", "both"],
        default="filter",
        help="How to apply award numbers in Crossref (default: filter).",
    )
    ap.add_argument("--from-year", type=int, help="Start year (inclusive) for publication date filter.")
    ap.add_argument("--to-year", type=int, help="End year (inclusive) for publication date filter.")
    ap.add_argument("--count", type=int, default=100, help="Rows per request (Crossref rows param).")
    ap.add_argument("--max-records", type=int, help="Maximum total records to process.")
    ap.add_argument(
        "--max-new-records",
        type=int,
        help="Stop after this many inserted/updated publications.",
    )
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
        "--estimate",
        action="store_true",
        help=(
            "Preflight only: report how many records each query would match "
            "(Crossref total-results) and exit WITHOUT ingesting or touching the DB."
        ),
    )
    ap.add_argument(
        "--require-affiliation-match",
        action="store_true",
        help=(
            "When --affiliation is used, require the returned Crossref author affiliations "
            "to contain at least one provided affiliation term."
        ),
    )
    ap.add_argument(
        "--source-token",
        default="crossref",
        help="Token to merge into source_of_truth (default: crossref).",
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
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    only_person_ids = _parse_person_id_list(args.only_person_id)

    if args.no_db and only_person_ids and not args.author_orcid:
        raise SystemExit("--only-person-id cannot be used with --no-db unless --author-orcid is provided.")

    if not (args.award or args.affiliation or args.funder or args.author_orcid or only_person_ids):
        raise SystemExit(
            "Provide at least one of --award, --affiliation, --funder, --author-orcid, or --only-person-id."
        )
    if args.max_new_records is not None and args.max_new_records <= 0:
        raise SystemExit("--max-new-records must be > 0.")

    stats = run_crossref_search(
        dsn=args.dsn or DEFAULT_DSN,
        awards=args.award,
        affiliations=args.affiliation,
        funders=args.funder,
        author_orcids=args.author_orcid,
        only_person_ids=only_person_ids,
        from_year=args.from_year,
        to_year=args.to_year,
        count=args.count,
        max_records=args.max_records,
        max_new_records=args.max_new_records,
        min_interval=args.min_interval,
        no_db=bool(args.no_db),
        dry_run=bool(args.dry_run),
        debug=bool(args.debug),
        debug_json=bool(args.debug_json),
        source_token=str(args.source_token or "crossref"),
        rewrite_sot=args.rewrite_sot or None,
        skip_existing=bool(args.skip_existing),
        refresh_authorships=bool(args.refresh_authorships),
        commit_every=int(args.commit_every or 0),
        award_mode=str(args.award_mode or "filter"),
        require_affiliation_match=bool(args.require_affiliation_match),
        estimate=bool(args.estimate),
    )

    if args.estimate:
        return

    LOGGER.info(
        "crossref_search: processed=%s inserted=%s updated=%s new_applied=%s skipped_no_doi=%s skipped_existing=%s skipped_affiliation_mismatch=%s errors=%s",
        stats.processed,
        stats.inserted,
        stats.updated,
        stats.inserted + stats.updated,
        stats.skipped_no_doi,
        stats.skipped_existing,
        stats.skipped_affiliation_mismatch,
        stats.errors,
    )


if __name__ == "__main__":
    main()
