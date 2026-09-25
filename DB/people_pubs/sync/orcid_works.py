"""
people_pubs.sync.orcid_works

Fetch ORCID works for internal people, enrich with Crossref, and upsert
publications/authorships.

Example:
  python -m people_pubs.sync.orcid_works --since 2019-01-01 --debug
"""

# people_pubs/sync/orcid_works.py
from __future__ import annotations

import argparse
import logging
import os
import re
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.rows import dict_row  # noqa: F401

from ..orcidkit import OrcidClient, works_summary_records, extract_contributors

from people_pubs.config import CROSSREF_MAILTO as DEFAULT_CROSSREF_MAILTO
from people_pubs.db.connection import db_conn
from people_pubs.db.people import (
    load_orcid_people,
    lookup_internal_person_for_author,
    lookup_orcid_for_person_id,
    mark_person_orcid_refreshed,
    names_equivalent,
)
from people_pubs.db.publications import upsert_publication, merge_raw_json
from people_pubs.db.authorships import (
    compute_order_tag,
    infer_equal_contrib_tag,
    upsert_authorship,
    upsert_author_email,
)
from people_pubs.services.crossref_client import (
    CrossrefClient,
    normalize_doi,
    pick_publication_date,
    guess_is_preprint,
)
from people_pubs.sync.crossref_backfill import merge_source_of_truth
from people_pubs.utils.identifiers import normalize_orcid
from people_pubs.sync.scaffold import _lookup_existing_publication


@dataclass
class OrcidWorksStats:
    people_total: int = 0
    people_failed: int = 0
    works_total: int = 0
    works_with_doi: int = 0
    processed: int = 0
    inserted: int = 0
    updated: int = 0
    skipped_no_doi: int = 0
    skipped_before_since: int = 0


def _normalize_orcid_value(orcid: Optional[str]) -> Optional[str]:
    """
    Normalize ORCID to canonical 0000-0000-0000-000X form or return None.

    - Accepts full URLs (https://orcid.org/0000-...) and strips the prefix.
    - Treats empty / whitespace / malformed values as None.
    - Does NOT raise; just drops bad values, so DB constraints are happy.
    """
    normalized = normalize_orcid(orcid)
    if orcid and normalized is None:
        logging.debug("Dropping invalid ORCID value %r", orcid)
        return None
    return normalized


def _has_internal_authorship_for_person(
    conn: psycopg.Connection,
    publication_id: int,
    person_id: int,
) -> bool:
    """
    Return True when publication already has an authorship linked to person_id.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM biblio.authorships a
                WHERE a.pub_id = %s
                  AND a.person_id = %s
            )
            """,
            (publication_id, person_id),
        )
        row = cur.fetchone()
    if not row:
        return False
    try:
        return bool(row[0])
    except Exception:
        return bool(row["exists"])


def _merge_sot(existing_sot: str, pub_dict: Dict[str, Any]) -> str:
    sot = existing_sot or ""
    if pub_dict.get("raw_crossref_json") is not None:
        sot = merge_source_of_truth(sot, "crossref")
    if pub_dict.get("raw_orcid_json") is not None:
        sot = merge_source_of_truth(sot, "orcid")
    if pub_dict.get("raw_scholar_json") is not None:
        sot = merge_source_of_truth(sot, "scholar")
    return sot or "orcid"


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def parse_since_date(s: str) -> date:
    """
    Parse --since / --refreshed-before as YYYY-MM-DD, YYYY-MM, or YYYY.
    - YYYY       -> YYYY-01-01
    - YYYY-MM    -> YYYY-MM-01
    - YYYY-MM-DD -> as-is
    """
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s)
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return date.fromisoformat(s + "-01")
    if re.fullmatch(r"\d{4}", s):
        return date.fromisoformat(s + "-01-01")
    raise ValueError(f"Invalid date value {s!r}; expected YYYY or YYYY-MM or YYYY-MM-DD")


# ---------------------------------------------------------------------------
# Mapping ORCID + Crossref metadata into publication dicts
# ---------------------------------------------------------------------------

def _build_publication_dict(
    w: Dict[str, Any],
    cr_msg: Optional[Dict[str, Any]],
    raw_orcid_work: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Map ORCID + Crossref metadata into the generic pub dict expected by
    people_pubs.db.publications.upsert_publication.
    """
    doi = w.get("doi")
    year = w.get("year")

    # Title: prefer Crossref, then ORCID summary.
    title = None
    if cr_msg:
        titles = cr_msg.get("title") or []
        if isinstance(titles, list) and titles:
            title = titles[0]
    if not title:
        title = w.get("title")

    journal_name = None
    if cr_msg:
        containers = cr_msg.get("container-title") or []
        if isinstance(containers, list) and containers:
            journal_name = containers[0]

    volume = cr_msg.get("volume") if cr_msg else None
    issue = cr_msg.get("issue") if cr_msg else None
    pages = None
    if cr_msg:
        page_str = cr_msg.get("page") or ""
        if isinstance(page_str, str) and page_str.strip():
            pages = page_str.strip()

    # Prefer Crossref's view of publication date, with ORCID year as fallback.
    fallback_year = None
    if isinstance(year, int):
        fallback_year = year
    elif isinstance(year, str) and year.isdigit():
        fallback_year = int(year)

    pub_date = pick_publication_date(cr_msg or {}, fallback_year)

    # Guess preprint status from Crossref + DOI + title.
    is_preprint = guess_is_preprint(cr_msg or {}, doi, journal_name, title)

    return {
        "doi": doi,
        "title": title,
        "year": pub_date.year if pub_date else fallback_year,
        "journal_name": journal_name,
        "venue": None,  # scholar/openalex flows can put a more generic venue here
        "raw_crossref_json": cr_msg,
        "raw_orcid_json": raw_orcid_work,
        "raw_scholar_json": None,
        "is_preprint": is_preprint,
    }


# ---------------------------------------------------------------------------
# Authorship extraction (Crossref first, ORCID fallback)
# ---------------------------------------------------------------------------

def _iter_authorship_rows(
    cr_msg: Optional[Dict[str, Any]],
    raw_orcid_work: Optional[Dict[str, Any]],
) -> List[Tuple[int, Dict[str, Any]]]:
    """
    Build a list of (position, author_json) from Crossref or ORCID.
    """
    authorship_rows: List[Tuple[int, Dict[str, Any]]] = []

    authors_from_crossref = (cr_msg.get("author") if cr_msg else None) or []
    if authors_from_crossref:
        for idx, a in enumerate(authors_from_crossref, start=1):
            authorship_rows.append((idx, a))
    else:
        if raw_orcid_work:
            contribs = extract_contributors(raw_orcid_work)  # [{name,orcid},...]
            for idx, c in enumerate(contribs, start=1):
                authorship_rows.append((idx, c))

    return authorship_rows


# ---------------------------------------------------------------------------
# Per-person ORCID → publications/authorships
# ---------------------------------------------------------------------------

def process_person(
    conn: psycopg.Connection,
    oc: OrcidClient,
    cr: CrossrefClient,
    person_row: Dict[str, Any],
    since: date,
    dry_run: bool = False,
    stats: Optional[OrcidWorksStats] = None,
) -> None:
    """
    For one person_id + ORCID:
      - list works
      - filter by date >= since
      - upsert publications + authorships
    """
    person_id = person_row["person_id"]
    display_name = person_row["display_name"]
    orcid = person_row["orcid"]

    logging.info("Processing person_id=%s orcid=%s (%s)", person_id, orcid, display_name)

    # 1) Get ORCID works summary
    try:
        works_obj = oc.list_works(orcid)
    except Exception as e:
        logging.error("  failed to list ORCID works for %s: %s", orcid, e)
        return

    works = works_summary_records(works_obj)  # [{put_code, title, year, doi, type}, ...]
    if stats is not None:
        stats.works_total += len(works)

    for w in works:
        doi = normalize_doi(w.get("doi") or "")
        if not doi:
            if stats is not None:
                stats.skipped_no_doi += 1
            continue
        if stats is not None:
            stats.works_with_doi += 1

        year = w.get("year")
        if since and year:
            try:
                y_int = int(year)
                if y_int < since.year - 1:  # rough early filter
                    logging.debug(
                        "  skipping DOI %s by ORCID year=%s < %s", doi, y_int, since.year
                    )
                    if stats is not None:
                        stats.skipped_before_since += 1
                    continue
            except Exception:
                pass

        # 2) Crossref metadata (may be missing)
        cr_msg: Optional[Dict[str, Any]] = None
        try:
            cr_msg = cr.get_work(doi) or None
        except Exception as e:
            logging.debug("  failed Crossref lookup for DOI %s: %s", doi, e)

        if not cr_msg:
            logging.debug(
                "  no Crossref metadata for DOI %s; will still store minimal record", doi
            )

        # 3) ORCID full work (for raw JSON, fallback metadata)
        raw_orcid_work: Optional[Dict[str, Any]] = None
        put_code = w.get("put_code")
        if put_code is not None:
            try:
                raw_orcid_work = oc.get_work(orcid, put_code)
            except Exception as e:
                logging.debug(
                    "  failed to fetch ORCID work %s for DOI %s: %s", put_code, doi, e
                )

        # 4) Build publication dict and filter by --since
        pub_dict = _build_publication_dict(w, cr_msg, raw_orcid_work)

        pub_year = pub_dict.get("year")
        if since and pub_year:
            try:
                if int(pub_year) < since.year:
                    logging.debug(
                        "  skipping DOI %s by publication year=%s < %s",
                        doi,
                        pub_year,
                        since.year,
                    )
                    if stats is not None:
                        stats.skipped_before_since += 1
                    continue
            except Exception:
                pass

        # 5) Upsert publication (merge source_of_truth with existing)
        existing = _lookup_existing_publication(conn, doi)
        existing_sot = str(existing.get("source_of_truth") or "") if existing else ""
        existing_raw = existing.get("raw_json") if existing else None

        updates = {}
        if raw_orcid_work is not None:
            updates["orcid"] = raw_orcid_work
        if cr_msg is not None:
            updates["crossref"] = cr_msg
        merged_raw = merge_raw_json(existing_raw, existing_sot, updates)

        if cr_msg is not None:
            pub_dict["raw_crossref_json"] = merged_raw
            pub_dict["raw_orcid_json"] = None
        else:
            pub_dict["raw_orcid_json"] = merged_raw
            pub_dict["raw_crossref_json"] = None

        pub_dict["source_of_truth"] = _merge_sot(existing_sot, pub_dict)
        if raw_orcid_work is not None:
            pub_dict["source_of_truth"] = merge_source_of_truth(
                pub_dict["source_of_truth"], "orcid"
            )

        if stats is not None:
            stats.processed += 1

        publication_id = upsert_publication(conn, pub_dict, dry_run=dry_run)
        if stats is not None:
            if dry_run:
                if existing:
                    stats.updated += 1
                else:
                    stats.inserted += 1
            elif publication_id:
                if existing:
                    stats.updated += 1
                else:
                    stats.inserted += 1
        if not publication_id:
            logging.warning(
                "  no publication_id returned for DOI %s; skipping authorships", doi
            )
            continue

        # 6) Build authorships
        authorship_rows = _iter_authorship_rows(cr_msg, raw_orcid_work)
        n_authors = len(authorship_rows)
        # Keep warnings accurate across reruns: if a prior run already linked
        # this person to the publication, do not emit a false "no internal author"
        # warning just because the current payload has weaker author metadata.
        inserted_internal_for_this_pub = _has_internal_authorship_for_person(
            conn,
            publication_id=publication_id,
            person_id=person_id,
        )

        for pos, a in authorship_rows:
            order_tag = compute_order_tag(pos, n_authors)

            if cr_msg and (cr_msg.get("author") or []):
                given = a.get("given") or ""
                family = a.get("family") or ""
                full_name = (given + " " + family).strip() or a.get("name") or ""

                raw_auth_orcid = a.get("ORCID") or ""
                auth_orcid = _normalize_orcid_value(raw_auth_orcid)

                affiliations = []
                for aff in a.get("affiliation") or []:
                    name = aff.get("name")
                    if isinstance(name, str) and name.strip():
                        affiliations.append(name.strip())
                email = a.get("email") or None
            else:
                # ORCID-style contributor record (ORCID record "contributors")
                full_name = a.get("name") or ""

                raw_auth_orcid = a.get("orcid") or ""
                auth_orcid = _normalize_orcid_value(raw_auth_orcid)

                affiliations = []
                email = None

            equal_contrib_tag = infer_equal_contrib_tag(a, order_tag)

            # Resolve internal person_id
            internal_person_id: Optional[int] = None
            # First: special-case focal person
            if auth_orcid and auth_orcid == orcid:
                internal_person_id = person_id
            elif names_equivalent(full_name, display_name):
                internal_person_id = person_id
            else:
                internal_person_id = lookup_internal_person_for_author(
                    conn, full_name, auth_orcid
                )

            if internal_person_id:
                inserted_internal_for_this_pub = True
                # Backfill author_orcid for internal authors, if missing.
                # paper_orcid must continue to reflect only the ORCID payload
                # that came from the ORCID work record itself.
                if not auth_orcid:
                    auth_orcid = _normalize_orcid_value(
                        lookup_orcid_for_person_id(conn, internal_person_id)
                    )

            # Upsert authorship row
            upsert_authorship(
                conn=conn,
                publication_id=publication_id,
                author_position=pos,
                author_name=full_name,
                author_orcid=auth_orcid,
                person_id=internal_person_id,
                affiliations=affiliations,
                is_corresponding=bool(a.get("corresponding", False)),
                order_tag=order_tag,
                equal_contrib_tag=equal_contrib_tag,
                raw_author_json=a,
                dry_run=dry_run,
            )

            # Optional: store author e-mails in pii.publication_author_emails
            if email:
                upsert_author_email(
                    conn=conn,
                    publication_id=publication_id,
                    person_id=internal_person_id,
                    author_position=pos,
                    email=email,
                    dry_run=dry_run,
                )

        if not inserted_internal_for_this_pub:
            logging.warning(
                "  WARNING: publication doi=%s has no internal author for person_id=%s",
                doi,
                person_id,
            )

    if not dry_run:
        mark_person_orcid_refreshed(conn, person_id)
        conn.commit()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_orcid_works_sync(
    *,
    dsn: Optional[str],
    since: date,
    limit_people: Optional[int],
    refreshed_before: Optional[date],
    max_age_days: Optional[int],
    only_person_id: Optional[int],
    only_orcid: Optional[str],
    crossref_mailto: Optional[str],
    dry_run: bool,
) -> None:
    """
    Core orchestration for ORCID works → publications/authorships.

    This is the central entry point that other code should call instead of
    re-implementing the same loop.
    """
    # If max_age_days is provided, derive refreshed_before from it (unless already set).
    refreshed_before_date: Optional[date] = refreshed_before
    if refreshed_before_date is None and max_age_days is not None:
        refreshed_before_date = date.today() - timedelta(days=max_age_days)

    mailto = (
        crossref_mailto
        or os.getenv("PEOPLE_PUBS_CROSSREF_MAILTO")
        or os.getenv("CROSSREF_MAILTO")
        or DEFAULT_CROSSREF_MAILTO
    )
    if not mailto:
        raise SystemExit(
            "Crossref mailto not set. Export PEOPLE_PUBS_CROSSREF_MAILTO (preferred) "
            "or pass --crossref-mailto."
        )

    with db_conn(dsn) as conn, OrcidClient() as oc, CrossrefClient(mailto=mailto) as cr:
        people = load_orcid_people(
            conn=conn,
            only_person_id=only_person_id,
            only_orcid=only_orcid,
            limit_people=limit_people,
            refreshed_before=refreshed_before_date,
        )

        if not people:
            logging.info("No people to process for ORCID works.")
            return

        stats = OrcidWorksStats(people_total=len(people))
        logging.info("Processing ORCID works for %d people...", len(people))

        for row in people:
            try:
                process_person(
                    conn=conn,
                    oc=oc,
                    cr=cr,
                    person_row=row,
                    since=since,
                    dry_run=dry_run,
                    stats=stats,
                )
            except Exception as e:  # defensive logging per person
                conn.rollback()
                stats.people_failed += 1
                logging.exception(
                    "Error processing person_id=%s orcid=%s: %s",
                    row.get("person_id"),
                    row.get("orcid"),
                    e,
                )

        logging.info(
            "orcid_works: people=%s failed=%s works=%s with_doi=%s processed=%s inserted=%s updated=%s skipped_no_doi=%s skipped_before_since=%s",
            stats.people_total,
            stats.people_failed,
            stats.works_total,
            stats.works_with_doi,
            stats.processed,
            stats.inserted,
            stats.updated,
            stats.skipped_no_doi,
            stats.skipped_before_since,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Sync ORCID works into biblio.publications and biblio.authorships.",
    )
    ap.add_argument(
        "--dsn",
        help=(
            "Optional PostgreSQL DSN (otherwise use PEOPLE_DB_DSN or PG* env vars). "
            "Example: postgresql://postgres:<password>@127.0.0.1:5432/people_db"
        ),
    )

    ap.add_argument(
        "--crossref-mailto",
        default=(
            os.getenv("PEOPLE_PUBS_CROSSREF_MAILTO")
            or os.getenv("CROSSREF_MAILTO")
            or DEFAULT_CROSSREF_MAILTO
        ),
        help=(
            "Email address used for Crossref polite pool (mailto=...). "
            "Defaults to $PEOPLE_PUBS_CROSSREF_MAILTO or $CROSSREF_MAILTO."
        ),
    )

    ap.add_argument(
        "--since",
        default="2019-01-01",
        help=(
            "Only keep publications with pub_date >= this date "
            "(YYYY or YYYY-MM or YYYY-MM-DD). Default: 2019-01-01"
        ),
    )
    ap.add_argument(
        "--limit-people",
        type=int,
        default=None,
        help="Maximum number of people to process (for testing).",
    )
    ap.add_argument(
        "--refreshed-before",
        default=None,
        help=(
            "Only process people whose ORCID publications were last refreshed "
            "before this date (YYYY, YYYY-MM, or YYYY-MM-DD) or never. "
            "Ignored if --only-person-id/--only-orcid is given."
        ),
    )
    ap.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help=(
            "Alternative to --refreshed-before: only process people whose ORCID "
            "publications were last refreshed more than N days ago or never. "
            "Ignored if --only-person-id/--only-orcid is given."
        ),
    )
    ap.add_argument(
        "--only-person-id",
        type=int,
        default=None,
        help="If set, only process this person_id.",
    )
    ap.add_argument(
        "--only-orcid",
        default=None,
        help="If set, only process this ORCID.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write anything to the DB; just log what would happen.",
    )
    ap.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging.",
    )
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    since = parse_since_date(args.since)

    refreshed_before_date: Optional[date] = None
    max_age_days: Optional[int] = args.max_age_days

    if args.refreshed_before:
        refreshed_before_date = parse_since_date(args.refreshed_before)
        if args.max_age_days is not None:
            logging.warning(
                "Both --refreshed-before and --max-age-days provided; "
                "honouring --refreshed-before and ignoring --max-age-days."
            )
            max_age_days = None

    run_orcid_works_sync(
        dsn=args.dsn,
        since=since,
        limit_people=args.limit_people,
        refreshed_before=refreshed_before_date,
        max_age_days=max_age_days,
        only_person_id=args.only_person_id,
        only_orcid=args.only_orcid,
        crossref_mailto=args.crossref_mailto,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
