"""people_pubs.sync.manual_csv_dois

Ingest/scan a manually curated CSV (e.g., Zotero export) containing DOIs.

For each row with a DOI:
1) Normalize the DOI
2) Fetch Crossref metadata (best-effort; title fallback if DOI lookup fails)
3) Resolve internal members by matching Crossref (or CSV) authors against the internal people table
4) Optionally upsert publication + internal authorships into biblio.* tables
5) Write a report CSV (missing-DOI rows use source_of_truth=manual-no-doi)

Usage:
  python -m people_pubs.sync.manual_csv_dois --csv my_pubs.csv --report manual_csv_report.csv --dry-run
  python DB/people_pubs/sync/manual_csv_dois.py --csv my_pubs.csv --debug

Notes:
  - Run from `DB/` or set `PYTHONPATH=DB` when using `python -m ...`.
  - Required CSV column: DOI (override with --doi-col).
  - The report CSV is always written, even in --dry-run.

Required CSV columns:
  - DOI (or the column passed via --doi-col)

Common optional columns (defaults shown):
  - Key (via --key-col)
  - Title (via --title-col)
  - Publication Title (via --venue-col)
  - Publication Year (via --year-col)
  - Author (used only when Crossref has no author list)
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
from pathlib import Path
import sys
from dataclasses import dataclass, field
import inspect
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

# Allow running as a standalone script from the repo root or DB/ without installing.
_PKG_ROOT = Path(__file__).resolve().parents[2]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from people_pubs.db.connection import db_conn
from people_pubs.db.people import lookup_internal_person_for_author, upsert_person_name_alias
from people_pubs.db.publications import upsert_publication, merge_raw_json
from people_pubs.db.authorships import upsert_authorship, compute_order_tag
from people_pubs.utils.identifiers import normalize_orcid
from people_pubs.services.crossref_client import (
    CrossrefClient,
    guess_is_preprint,
    normalize_doi,
    pick_publication_date,
)
from people_pubs.sync.crossref_backfill import (
    merge_source_of_truth,
    title_similarity,
    parse_crossref_year,
)
from people_pubs.sync.scaffold import _lookup_existing_publication, _strip_orcid

log = logging.getLogger(__name__)


@dataclass
class RowResult:
    csv_key: str
    doi: str
    title: str
    venue: str
    source_of_truth: str
    pub_year: Optional[int]
    internal_person_ids: List[int]
    crossref_found: bool
    authors_total: int = 0
    authors_internal_matched: int = 0
    already_in_db: bool = False
    pub_id_before: Optional[int] = None
    pub_id_after: Optional[int] = None
    ingest_action: str = ""
    crossref_http_status: Optional[int] = None
    registration_agency_id: str = ""
    registration_agency_label: str = ""
    error: str = ""



def _parse_int(s: Any) -> Optional[int]:
    if s is None:
        return None
    try:
        s2 = str(s).strip()
        if not s2:
            return None
        return int(float(s2))
    except Exception:
        return None


def _iter_csv_rows(path: str) -> Iterable[Dict[str, str]]:
    # Zotero exports commonly include a UTF-8 BOM; utf-8-sig handles it.
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalize keys: DictReader keeps header names as-is (including quotes sometimes).
            yield {k.strip().strip('"'): (v or "").strip() for k, v in row.items() if k is not None}


def _best_title(cr_msg: Optional[Mapping[str, Any]], csv_title: str) -> str:
    if cr_msg:
        t = cr_msg.get("title")
        if isinstance(t, list) and t and t[0]:
            return str(t[0]).strip()
        if isinstance(t, str) and t.strip():
            return t.strip()
    return (csv_title or "").strip()


def _best_venue(cr_msg: Optional[Mapping[str, Any]], csv_venue: str) -> str:
    if cr_msg:
        ct = cr_msg.get("container-title")
        if isinstance(ct, list) and ct and ct[0]:
            return str(ct[0]).strip()
        if isinstance(ct, str) and ct.strip():
            return ct.strip()
    return (csv_venue or "").strip()

def _pick_best_title_match(
    items: List[Dict[str, Any]],
    title: str,
    year: Optional[int],
) -> Optional[Dict[str, Any]]:
    if not items or not title:
        return None
    best: Optional[Dict[str, Any]] = None
    best_score = -1.0
    for it in items:
        cand_titles = it.get("title") or []
        cand_title = cand_titles[0] if isinstance(cand_titles, list) and cand_titles else ""
        score = title_similarity(title, cand_title)
        if isinstance(year, int) and year > 0:
            cy = parse_crossref_year(it) or 0
            if cy == year:
                score += 0.10
            elif abs(cy - year) == 1:
                score += 0.03
        if score > best_score:
            best_score = score
            best = it
    if best_score < 0.70:
        return None
    return best


def _iter_authors_from_crossref(cr_msg: Mapping[str, Any]) -> Iterable[Tuple[str, Optional[str], List[str]]]:
    for a in cr_msg.get("author", []) or []:
        if not isinstance(a, Mapping):
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
                if isinstance(item, Mapping):
                    n = str(item.get("name") or "").strip()
                    if n:
                        affs.append(n)
                elif isinstance(item, str):
                    s = item.strip()
                    if s:
                        affs.append(s)
        yield name, _strip_orcid(a.get("ORCID")), affs


def _iter_authors_from_csv(author_field: str) -> Iterable[str]:
    # Zotero 'Author' column may be: "Last, First; Last, First" or multiple lines.
    raw = (author_field or "").replace("\n", ";")
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    for p in parts:
        yield p


def run_manual_csv_doi_scan(
    *,
    csv_path: str,
    dsn: Optional[str],
    doi_col: str,
    key_col: str,
    title_col: str,
    venue_col: str,
    year_col: str,
    report_path: str,
    dry_run: bool,
    debug: bool,
    limit: Optional[int],
    source_of_truth: str,
) -> List[RowResult]:
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
    log.info(
        "manual_csv_dois: start csv=%s report=%s dry_run=%s limit=%r",
        csv_path,
        report_path,
        dry_run,
        limit,
    )

    results: List[RowResult] = []
    seen: set[str] = set()

    with db_conn(dsn) as conn, CrossrefClient() as cr:
        for idx, row in enumerate(_iter_csv_rows(csv_path), start=1):
            if limit and len(results) >= limit:
                break

            csv_key = row.get(key_col, "") or f"row_{idx}"
            doi_raw = row.get(doi_col, "")
            doi = normalize_doi(doi_raw)
            csv_year = _parse_int(row.get(year_col))
            csv_title = (row.get(title_col, "") or "").strip()
            csv_venue = (row.get(venue_col, "") or "").strip()
            no_doi_in_csv = not doi
            source_token = "manual-no-doi" if no_doi_in_csv else "manual"
            report_sot = "manual-no-doi" if no_doi_in_csv else source_of_truth
            manual_blob_source = "manual-no-doi" if no_doi_in_csv else source_of_truth

            log.debug(
                "row=%d csv_key=%r doi_raw=%r doi_norm=%r", idx, csv_key, doi_raw, doi
            )

            cr_msg: Optional[Dict[str, Any]] = None
            cr_status: Optional[int] = None
            crossref_found = False
            title_fallback_used = False
            reg_agency_id = ""
            reg_agency_label = ""

            if no_doi_in_csv:
                if not csv_title:
                    results.append(
                        RowResult(
                            source_of_truth=report_sot,
                            csv_key=csv_key,
                            doi="",
                            title=csv_title,
                            venue=csv_venue,
                            pub_year=csv_year,
                            internal_person_ids=[],
                            crossref_found=False,
                            already_in_db=False,
                            pub_id_before=None,
                            pub_id_after=None,
                            ingest_action="no_doi_title_lookup_failed",
                            crossref_http_status=None,
                            registration_agency_id="",
                            registration_agency_label="",
                            error="missing DOI; title lookup failed (missing title)",
                        )
                    )
                    continue

                items = cr.lookup_by_title(csv_title, rows=5)
                cr_msg = _pick_best_title_match(items, csv_title, csv_year)
                if not cr_msg:
                    results.append(
                        RowResult(
                            source_of_truth=report_sot,
                            csv_key=csv_key,
                            doi="",
                            title=csv_title,
                            venue=csv_venue,
                            pub_year=csv_year,
                            internal_person_ids=[],
                            crossref_found=False,
                            already_in_db=False,
                            pub_id_before=None,
                            pub_id_after=None,
                            ingest_action="no_doi_title_lookup_failed",
                            crossref_http_status=None,
                            registration_agency_id="",
                            registration_agency_label="",
                            error="title lookup failed",
                        )
                    )
                    continue

                cr_doi = normalize_doi(cr_msg.get("DOI") or "")
                if not cr_doi:
                    results.append(
                        RowResult(
                            source_of_truth=report_sot,
                            csv_key=csv_key,
                            doi="",
                            title=csv_title,
                            venue=csv_venue,
                            pub_year=csv_year,
                            internal_person_ids=[],
                            crossref_found=False,
                            already_in_db=False,
                            pub_id_before=None,
                            pub_id_after=None,
                            ingest_action="no_doi_title_lookup_failed",
                            crossref_http_status=None,
                            registration_agency_id="",
                            registration_agency_label="",
                            error="title lookup matched but DOI missing",
                        )
                    )
                    continue

                doi = cr_doi
                crossref_found = True
                title_fallback_used = True
                log.debug(
                    "row=%d csv_key=%r title lookup resolved doi=%r",
                    idx,
                    csv_key,
                    doi,
                )

            if doi in seen:
                continue
            seen.add(doi)

            existing = _lookup_existing_publication(conn, doi)
            already_in_db = existing is not None
            pub_id_before: Optional[int] = None
            if existing and existing.get("pub_id") is not None:
                try:
                    pub_id_before = int(existing.get("pub_id"))
                except Exception:
                    pub_id_before = None
            if existing:
                log.debug(
                    "doi=%s already_in_db pub_id=%s sot=%r year=%r venue=%r",
                    doi,
                    existing.get("pub_id"),
                    existing.get("source_of_truth"),
                    existing.get("year"),
                    existing.get("venue"),
                )
            else:
                log.debug("doi=%s not_in_db", doi)

            try:
                if not crossref_found:
                    cr_msg, cr_status = cr.lookup_by_doi_with_status(doi)
                    crossref_found = cr_msg is not None
                if not crossref_found:
                    if csv_title:
                        items = cr.lookup_by_title(csv_title, rows=5)
                        cr_msg = _pick_best_title_match(items, csv_title, csv_year)
                        if cr_msg:
                            crossref_found = True
                            title_fallback_used = True
                            log.debug("doi=%s no Crossref DOI match; using title fallback", doi)
                    if not crossref_found and not no_doi_in_csv:
                        agency, agency_status = cr.lookup_registration_agency_with_status(doi)
                        if agency:
                            reg_agency_id = str(agency.get("id") or "")
                            reg_agency_label = str(agency.get("label") or "")
                        log.debug(
                            "doi=%s crossref_found=%s crossref_http_status=%s registration_agency=%s/%s agency_http_status=%s",
                            doi,
                            crossref_found,
                            cr_status,
                            reg_agency_id or "?",
                            reg_agency_label or "?",
                            agency_status,
                        )
                else:
                    log.debug("doi=%s crossref_found=%s crossref_http_status=%s", doi, crossref_found, cr_status)


                title = _best_title(cr_msg, csv_title)
                venue = _best_venue(cr_msg, csv_venue)

                pub_date = pick_publication_date(cr_msg or {}, fallback_year=csv_year)
                pub_year = pub_date.year if pub_date else csv_year

                is_preprint = guess_is_preprint(
                    cr_msg or {},
                    doi=doi,
                    venue=venue,
                    title=title,
                )
                # Optional publication upsert (do this before authorships so we have pub_id).
                publication_id: Optional[int] = None
                pub_id_after: Optional[int] = pub_id_before
                ingest_action = ("would_update" if already_in_db else "would_insert") if dry_run else ("update" if already_in_db else "insert")
                if not dry_run:
                    existing_sot = existing.get("source_of_truth") if existing else ""
                    existing_raw = existing.get("raw_json") if existing else None
                    sot = merge_source_of_truth(existing_sot, source_token)
                    if cr_msg:
                        sot = merge_source_of_truth(sot, "crossref")
                    manual_blob = {
                        "source_of_truth": manual_blob_source,
                        "ingested_at": datetime.utcnow().isoformat() + "Z",
                        "csv": {
                            "key": csv_key,
                            "title": csv_title,
                            "venue": csv_venue,
                            "year": csv_year,
                            "doi_raw": doi_raw,
                        },
                    }
                    updates = {"manual_csv": manual_blob}
                    if cr_msg:
                        updates["crossref"] = cr_msg
                    merged_raw = merge_raw_json(existing_raw, str(existing_sot or ""), updates)

                    raw_crossref = merged_raw if cr_msg else None
                    raw_manual = merged_raw if not cr_msg else None

                    publication_id = upsert_publication(
                        conn,
                        {
                            "doi": doi,
                            "title": title,
                            "year": pub_year,
                            "venue": venue,
                            "raw_crossref_json": raw_crossref,
                            "raw_orcid_json": raw_manual,
                            "raw_scholar_json": None,
                            "is_preprint": bool(is_preprint),
                            "source_of_truth": sot,
                        },
                        dry_run=False,
                    )
                    if publication_id is not None:
                        pub_id_after = int(publication_id)


                # Resolve internal members
                internal_person_ids: List[int] = []
                matched: set[int] = set()
                authors_total = 0

                # If publication upsert returned None for some reason, fall back to known pub_id.
                pub_id_for_authorships: Optional[int] = int(pub_id_after) if pub_id_after else (int(pub_id_before) if pub_id_before else None)

                if cr_msg and cr_msg.get("author"):
                    authors = list(_iter_authors_from_crossref(cr_msg))
                    n_authors = len(authors)
                    authors_total = n_authors
                    for pos, (full_name, auth_orcid, affiliations) in enumerate(authors, start=1):
                        internal_id = lookup_internal_person_for_author(conn, full_name, auth_orcid)
                        if internal_id and int(internal_id) not in matched:
                            matched.add(int(internal_id))
                            internal_person_ids.append(int(internal_id))
                            upsert_person_name_alias(
                                conn,
                                int(internal_id),
                                full_name,
                                source="manual_csv.crossref_author_name",
                                dry_run=dry_run,
                            )

                        # Always materialize authorships (person_id may be NULL for external authors).
                        if not dry_run:
                            if not pub_id_for_authorships:
                                log.warning("No publication_id for doi=%s; skipping authorships", doi)
                            else:
                                order_tag = compute_order_tag(pos, n_authors)
                                upsert_authorship(
                                    conn,
                                    publication_id=int(pub_id_for_authorships),
                                    author_position=pos,
                                    author_name=full_name,
                                    author_orcid=auth_orcid,
                                    person_id=(int(internal_id) if internal_id else None),
                                    affiliations=affiliations,
                                    is_corresponding=False,
                                    order_tag=order_tag,
                                    equal_contrib_tag=None,
                                    raw_author_json={
                                        "source_of_truth": source_of_truth,
                                        "source": "crossref",
                                        "crossref_author": {
                                            "full_name": full_name,
                                            "orcid": auth_orcid,
                                            "affiliations": affiliations,
                                        },
                                    },
                                    dry_run=False,
                                )

                else:
                    # Fall back to CSV 'Author' field (names only)
                    csv_authors = list(_iter_authors_from_csv(row.get("Author", "")))
                    n_authors = len(csv_authors)
                    authors_total = n_authors
                    for pos, full_name in enumerate(csv_authors, start=1):
                        internal_id = lookup_internal_person_for_author(conn, full_name, None)
                        if internal_id and int(internal_id) not in matched:
                            matched.add(int(internal_id))
                            internal_person_ids.append(int(internal_id))
                            upsert_person_name_alias(
                                conn,
                                int(internal_id),
                                full_name,
                                source="manual_csv.csv_author_name",
                                dry_run=dry_run,
                            )
                        if not dry_run:
                            if not pub_id_for_authorships:
                                log.warning("No publication_id for doi=%s; skipping authorships", doi)
                            else:
                                order_tag = compute_order_tag(pos, n_authors)
                                upsert_authorship(
                                    conn,
                                    publication_id=int(pub_id_for_authorships),
                                    author_position=pos,
                                    author_name=full_name,
                                    author_orcid=None,
                                    person_id=(int(internal_id) if internal_id else None),
                                    affiliations=[],
                                    is_corresponding=False,
                                    order_tag=order_tag,
                                    equal_contrib_tag=None,
                                    raw_author_json={
                                        "source_of_truth": source_of_truth,
                                        "source": "csv",
                                        "author_name": full_name,
                                    },
                                    dry_run=False,
                                )

                authors_internal_matched = len(internal_person_ids)
                results.append(
                    RowResult(
                        source_of_truth=report_sot,
                        csv_key=csv_key,
                        doi=doi,
                        title=title,
                        venue=venue,
                        pub_year=pub_year,
                        internal_person_ids=internal_person_ids,
                        authors_total=authors_total,
                        authors_internal_matched=authors_internal_matched,
                        crossref_found=crossref_found,
                        already_in_db=already_in_db,
                        pub_id_before=pub_id_before,
                        pub_id_after=pub_id_after,
                        ingest_action=ingest_action,
                        crossref_http_status=cr_status if not title_fallback_used else None,
                        registration_agency_id=reg_agency_id,
                        registration_agency_label=reg_agency_label,
                    )
                )
                if not dry_run:
                    conn.commit()

            except Exception as e:
                log.exception("Failed processing DOI %s (csv_key=%s)", doi, csv_key)
                try:
                    conn.rollback()
                except Exception:
                    pass
                results.append(
                    RowResult(
                        source_of_truth=report_sot,
                        csv_key=csv_key,
                        doi=doi,
                        title=csv_title,
                        venue=csv_venue,
                        pub_year=csv_year,
                        internal_person_ids=[],
                        crossref_found=False,
                        already_in_db=already_in_db,
                        pub_id_before=pub_id_before,
                        pub_id_after=pub_id_before,
                        ingest_action="error",
                        crossref_http_status=None,
                        registration_agency_id="",
                        registration_agency_label="",
                        error=str(e),
                    )
                )

    # Always write a report
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "source_of_truth",
                "csv_key",
                "doi",
                "title",
                "venue",
                "pub_year",
                "crossref_found",
                "authors_total",
                "authors_internal_matched",
                "already_in_db",
                "is_new",
                "pub_id_before",
                "pub_id_after",
                "ingest_action",
                "crossref_http_status",
                "registration_agency_id",
                "registration_agency_label",
                "internal_person_ids",
                "error",
            ],
        )
        w.writeheader()
        for r in results:
            w.writerow(
                {
                    "source_of_truth": r.source_of_truth or source_of_truth,
                    "csv_key": r.csv_key,
                    "doi": r.doi,
                    "title": r.title,
                    "venue": r.venue,
                    "pub_year": r.pub_year or "",
                    "crossref_found": "1" if r.crossref_found else "0",
                    "authors_total": r.authors_total,
                    "authors_internal_matched": r.authors_internal_matched,
                    "already_in_db": "1" if r.already_in_db else "0",
                    "is_new": "0" if r.already_in_db else "1",
                    "pub_id_before": r.pub_id_before or "",
                    "pub_id_after": r.pub_id_after or "",
                    "ingest_action": r.ingest_action,
                    "crossref_http_status": r.crossref_http_status if r.crossref_http_status is not None else "",
                    "registration_agency_id": r.registration_agency_id,
                    "registration_agency_label": r.registration_agency_label,
                    "internal_person_ids": ";".join(str(x) for x in r.internal_person_ids),
                    "error": r.error,
                }
            )
    log.info("manual_csv_dois: wrote report=%s rows=%d", report_path, len(results))
    action_counts: Dict[str, int] = {}
    for r in results:
        action_counts[r.ingest_action or "unknown"] = action_counts.get(r.ingest_action or "unknown", 0) + 1
    log.info(
        "manual_csv_dois summary: processed=%d inserted=%d updated=%d would_insert=%d would_update=%d "
        "skipped=%d errors=%d crossref_found=%d internal_matches=%d report=%s dry_run=%s",
        len(results),
        action_counts.get("insert", 0),
        action_counts.get("update", 0),
        action_counts.get("would_insert", 0),
        action_counts.get("would_update", 0),
        sum(v for k, v in action_counts.items() if k.startswith("no_doi") or k in {"unknown"}),
        action_counts.get("error", 0),
        sum(1 for r in results if r.crossref_found),
        sum(r.authors_internal_matched for r in results),
        report_path,
        dry_run,
    )
    return results


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scan/ingest a manually curated publications CSV by DOI, resolve internal members, and write a report."
    )
    p.add_argument("--csv", dest="csv_path", required=True, help="Path to the CSV file (e.g., Zotero export).")

    p.add_argument(
        "--dsn",
        default=None,
        help="Optional PostgreSQL DSN (otherwise use PEOPLE_DB_DSN or PG* env vars). Example: postgresql://postgres:<password>@127.0.0.1:5432/people_db",
    )

    p.add_argument("--doi-col", default="DOI", help="CSV column name that holds the DOI. Default: DOI")
    p.add_argument("--key-col", default="Key", help="CSV column name used as a stable row identifier. Default: Key")
    p.add_argument("--title-col", default="Title", help="CSV column name for title fallback. Default: Title")
    p.add_argument("--venue-col", default="Publication Title", help="CSV column name for venue/journal fallback. Default: Publication Title")
    p.add_argument("--year-col", default="Publication Year", help="CSV column name for year fallback. Default: Publication Year")

    p.add_argument("--report", default="manual_csv_doi_report.csv", help="Where to write the report CSV. Default: manual_csv_doi_report.csv")

    p.add_argument("--limit", type=int, default=None, help="Maximum number of unique DOIs to process (for testing).")

    p.add_argument(
        "--source-of-truth",
        default="manual csv",
        help='Value written into the report and provenance JSON. Default: "manual csv"',
    )

    p.add_argument("--dry-run", action="store_true", help="Do not write to the DB (still writes the report).")

    p.add_argument("--debug", action="store_true", help="Verbose logging.")

    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
    )
    run_manual_csv_doi_scan(
        csv_path=args.csv_path,
        dsn=args.dsn,
        doi_col=args.doi_col,
        key_col=args.key_col,
        title_col=args.title_col,
        venue_col=args.venue_col,
        year_col=args.year_col,
        report_path=args.report,
        dry_run=bool(args.dry_run),
        debug=bool(args.debug),
        limit=args.limit,
        source_of_truth=args.source_of_truth,
    )


if __name__ == "__main__":
    main()
