#!/usr/bin/env python3
"""
OpenAlex backfill for people_pubs.

Typical usage:
  python -m people_pubs.sync.openalex_backfill --max 200 --debug
  python -m people_pubs.sync.openalex_backfill --only-pub-id 1210 --debug
  python -m people_pubs.sync.openalex_backfill --only-pub-id 1210 --force --debug
  python -m people_pubs.sync.openalex_backfill --merge-mode doi --dry-run

What it does:
- Finds publications that appear not OpenAlex-enriched (raw_json missing 'openalex')
  and/or missing doi/year/venue, unless --force is used.
- Queries OpenAlex:
    - by DOI if present
    - otherwise by title (and optional year window)
- Updates biblio.publications in-place (optionally refreshes authorships with --refresh-authorships).
- If OpenAlex returns a DOI that already exists under a different pub_id,
  merges (moves) child rows and deletes the loser (same merge flow as Crossref).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx
import psycopg

from people_pubs.config import (
    DEFAULT_DSN,
    USER_AGENT,
    CROSSREF_MAILTO,
    has_more_trusted_source,
)
from people_pubs.db.connection import db_conn
from people_pubs.db.authorships import compute_order_tag, upsert_authorship
from people_pubs.db.people import (
    lookup_internal_person_for_author,
    lookup_orcid_for_person_id,
    name_similarity_score,
    upsert_person_name_alias,
)
from people_pubs.db.publications import merge_raw_json
from people_pubs.services.crossref_client import normalize_doi
from people_pubs.utils.identifiers import normalize_orcid
from people_pubs.sync.crossref_backfill import (
    PubRow,
    choose_canonical_pub,
    find_pub_by_doi,
    merge_publications,
    merge_source_of_truth,
    title_similarity,
    update_publication_in_place,
    _clear_publication_doi,
)
from people_pubs.sync.scaffold import BackfillStats, _note_author_added, _parse_pub_id_list, _record_pub_changes, _row_get, _strip_orcid


LOGGER = logging.getLogger("people_pubs.openalex_backfill")
OPENALEX_BASE = "https://api.openalex.org"
OPENALEX_RAW_DISPLAY_MIN_SCORE = 0.8


@dataclass(frozen=True)
class OpenAlexConfig:
    mailto: Optional[str]
    sleep_s: float = 0.0
    timeout: float = 20.0


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


def _openalex_year(work: Dict[str, Any], fallback_year: Optional[int] = None) -> Optional[int]:
    year = _parse_year(work.get("publication_year"))
    if year:
        return year
    year = _parse_year(work.get("publication_date"))
    if year:
        return year
    return fallback_year


def _openalex_venue(work: Dict[str, Any]) -> Optional[str]:
    host = work.get("host_venue") or {}
    if isinstance(host, dict):
        name = (host.get("display_name") or "").strip()
        if name:
            return name
    primary = (work.get("primary_location") or {}).get("source") or {}
    if isinstance(primary, dict):
        name = (primary.get("display_name") or "").strip()
        if name:
            return name
    return None


def _is_preprint(work: Dict[str, Any]) -> bool:
    return str(work.get("type") or "").lower() == "preprint"


def _openalex_update_fields(
    existing: PubRow,
    *,
    title: Optional[str],
    year: Optional[int],
    venue: Optional[str],
    is_preprint: bool,
) -> Dict[str, Any]:
    """
    Return publication fields OpenAlex is allowed to update.

    OpenAlex is last-resort enrichment: it may fill missing fields, but it must
    not replace metadata already backed by a more trusted source family.
    """
    trusted_existing = has_more_trusted_source(existing.source_of_truth or "", "openalex")
    return {
        "title": title if title and (not trusted_existing or not existing.title) else None,
        "year": year if year is not None and (not trusted_existing or existing.year is None) else None,
        "venue": venue if venue and (not trusted_existing or not existing.venue) else None,
        "is_preprint": is_preprint if not trusted_existing else None,
    }


def _iter_authors_from_openalex(
    work: Dict[str, Any],
    include_department: bool = False,
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


def _refresh_authorships_from_openalex(
    conn: psycopg.Connection,
    publication_id: int,
    work: Dict[str, Any],
    *,
    include_department: bool,
    dry_run: bool,
    stats: BackfillStats,
) -> None:
    authors = list(_iter_authors_from_openalex(work, include_department=include_department))
    if not authors:
        return
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
    processed_any = False
    n_authors = len(authors)
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
                source="openalex.author_name",
                dry_run=dry_run,
            )
            if int(internal_id) not in existing_person_ids:
                _note_author_added(
                    stats,
                    person_id=int(internal_id),
                    author_name=full_name,
                    pub_id=publication_id,
                )
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
    if not processed_any:
        LOGGER.info(
            "Skipping authorship refresh for pub_id=%s (no affiliations in OpenAlex author data)",
            publication_id,
        )
        return
    stats.authorship_refreshed += 1


class OpenAlexHTTP:
    def __init__(self, cfg: OpenAlexConfig):
        self.cfg = cfg
        self.client = httpx.Client(
            timeout=cfg.timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            http2=True,
        )

    def close(self) -> None:
        self.client.close()

    def _maybe_sleep(self) -> None:
        if self.cfg.sleep_s and self.cfg.sleep_s > 0:
            time.sleep(self.cfg.sleep_s)

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        self._maybe_sleep()
        params = dict(params or {})
        if self.cfg.mailto:
            params.setdefault("mailto", self.cfg.mailto)
        r = self.client.get(url, params=params)
        r.raise_for_status()
        return r

    def get_work_by_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        doi_norm = normalize_doi(doi) or doi
        if not doi_norm:
            return None
        url = f"{OPENALEX_BASE}/works/https://doi.org/{doi_norm}"
        self._maybe_sleep()
        r = self.client.get(url, params={"mailto": self.cfg.mailto} if self.cfg.mailto else None)
        if r.status_code == 404:
            return None
        if r.status_code == 400:
            # Fallback to filter-based query
            try:
                r = self._get(
                    f"{OPENALEX_BASE}/works",
                    params={"filter": f"doi:{doi_norm}", "per-page": 1},
                )
            except Exception:
                return None
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = None
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(
                f"OpenAlex DOI lookup failed (status={r.status_code} doi={doi_norm}): {detail}"
            ) from exc
        data = r.json()
        # /works/{id} returns a work object; /works?filter returns list
        if isinstance(data, dict) and "results" in data:
            results = data.get("results") or []
            return results[0] if results else None
        return data if isinstance(data, dict) else None

    def search_best_by_title(
        self,
        title: str,
        year: Optional[int],
        rows: int = 5,
    ) -> Optional[Dict[str, Any]]:
        q = (title or "").strip()
        if not q:
            return None

        params: Dict[str, Any] = {"search": q, "per-page": rows}
        if isinstance(year, int) and year > 0:
            y0 = max(year - 1, 1000)
            y1 = year + 1
            params["filter"] = f"from_publication_date:{y0}-01-01,to_publication_date:{y1}-12-31"

        r = self._get(f"{OPENALEX_BASE}/works", params=params)
        payload = r.json() or {}
        items = payload.get("results") or []
        if not items:
            return None

        best: Optional[Dict[str, Any]] = None
        best_score = -1.0
        for it in items:
            cand_title = str(it.get("title") or "").strip()
            score = title_similarity(title, cand_title)
            if isinstance(year, int) and year > 0:
                cy = _parse_year(it.get("publication_year")) or 0
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
        OR
        doi IS NULL
        OR year IS NULL
        OR venue IS NULL
        OR raw_json IS NULL
        OR NOT COALESCE(raw_json ? 'openalex', FALSE)
      )
    ORDER BY
      CASE WHEN %(pub_ids)s::bigint[] IS NULL THEN updated_at END ASC,
      CASE WHEN %(pub_ids)s::bigint[] IS NOT NULL THEN array_position(%(pub_ids)s::bigint[], pub_id) END ASC,
      pub_id ASC
    LIMIT %(max_n)s;
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            {
                "pub_ids": only_pub_ids,
                "max_n": max_n,
                "force": bool(force),
            },
        )
        for row in cur.fetchall():
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


def backfill_one(
    conn: psycopg.Connection,
    oa: OpenAlexHTTP,
    pub: PubRow,
    *,
    merge_mode: str,
    dry_run: bool,
    source_token: str,
    rewrite_sot: Optional[str],
    refresh_authorships: bool,
    include_department: bool,
    stats: BackfillStats,
) -> None:
    work: Optional[Dict[str, Any]] = None

    doi_norm = normalize_doi(pub.doi or "")
    if doi_norm:
        work = oa.get_work_by_doi(doi_norm)

    if work is None:
        work = oa.search_best_by_title(pub.title, pub.year)

    if work is None:
        LOGGER.info("No OpenAlex match for pub_id=%s title=%r", pub.pub_id, pub.title)
        stats.skipped_no_match += 1
        return
    stats.matched += 1

    oa_doi = normalize_doi(work.get("doi") or (work.get("ids") or {}).get("doi") or "")
    oa_title = str(work.get("title") or "").strip() or None
    oa_venue = _openalex_venue(work)
    oa_year = _openalex_year(work)
    is_preprint = _is_preprint(work)

    existing_sot = pub.source_of_truth or ""
    existing_raw = pub.raw_json
    if rewrite_sot:
        sot = rewrite_sot
    else:
        sot = merge_source_of_truth(existing_sot, source_token)

    raw_patch = merge_raw_json(
        existing_raw,
        str(existing_sot or ""),
        {
            "openalex": work,
            "openalex_backfill": {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        },
    )

    if merge_mode in {"doi", "doi+title"} and oa_doi:
        other = find_pub_by_doi(conn, oa_doi)
        if other and other.pub_id != pub.pub_id:
            winner, loser = choose_canonical_pub(pub, other)
            if loser.doi:
                LOGGER.info(
                    "Clearing DOI on loser pub_id=%s to avoid conflict before merge",
                    loser.pub_id,
                )
                _clear_publication_doi(conn, loser.pub_id, dry_run=dry_run)
            updates = _openalex_update_fields(
                winner,
                title=oa_title,
                year=oa_year,
                venue=oa_venue,
                is_preprint=is_preprint,
            )
            winner_sot = rewrite_sot if rewrite_sot else merge_source_of_truth(
                winner.source_of_truth,
                source_token,
            )
            update_publication_in_place(
                conn,
                winner.pub_id,
                doi=oa_doi,
                title=updates["title"],
                year=updates["year"],
                venue=updates["venue"],
                source_of_truth=winner_sot,
                raw_json_patch=raw_patch,
                is_preprint=updates["is_preprint"],
                dry_run=dry_run,
            )
            stats.updated += 1
            stats.merged += 1
            _record_pub_changes(
                stats,
                winner,
                doi=oa_doi,
                title=updates["title"],
                year=updates["year"],
                venue=updates["venue"],
                source_of_truth=winner_sot,
                raw_patch=True,
            )
            merge_publications(conn, winner_pub_id=winner.pub_id, loser_pub_id=loser.pub_id, dry_run=dry_run)
            if refresh_authorships and not has_more_trusted_source(
                winner.source_of_truth or "", "openalex"
            ):
                _refresh_authorships_from_openalex(
                    conn,
                    winner.pub_id,
                    work,
                    include_department=include_department,
                    dry_run=dry_run,
                    stats=stats,
                )
            elif refresh_authorships:
                LOGGER.info(
                    "Skipping OpenAlex authorship refresh for pub_id=%s because a more trusted source is present",
                    winner.pub_id,
                )
            return

    updates = _openalex_update_fields(
        pub,
        title=oa_title,
        year=oa_year,
        venue=oa_venue,
        is_preprint=is_preprint,
    )
    update_publication_in_place(
        conn,
        pub.pub_id,
        doi=oa_doi if (pub.doi is None and oa_doi) else None,
        title=updates["title"],
        year=updates["year"],
        venue=updates["venue"],
        source_of_truth=sot,
        raw_json_patch=raw_patch,
        is_preprint=updates["is_preprint"],
        dry_run=dry_run,
    )
    stats.updated += 1
    _record_pub_changes(
        stats,
        pub,
        doi=oa_doi if (pub.doi is None and oa_doi) else pub.doi,
        title=updates["title"],
        year=updates["year"],
        venue=updates["venue"],
        source_of_truth=sot,
        raw_patch=True,
    )
    if refresh_authorships and not has_more_trusted_source(pub.source_of_truth or "", "openalex"):
        _refresh_authorships_from_openalex(
            conn,
            pub.pub_id,
            work,
            include_department=include_department,
            dry_run=dry_run,
            stats=stats,
        )
    elif refresh_authorships:
        LOGGER.info(
            "Skipping OpenAlex authorship refresh for pub_id=%s because a more trusted source is present",
            pub.pub_id,
        )

    LOGGER.info(
        "Backfilled pub_id=%s doi=%r year=%r venue=%r",
        pub.pub_id,
        oa_doi,
        updates["year"] if updates["year"] is not None else pub.year,
        updates["venue"] if updates["venue"] is not None else pub.venue,
    )


def run_openalex_backfill(
    *,
    dsn: str,
    mailto: Optional[str],
    max_n: int,
    only_pub_ids: Optional[List[int]],
    merge_mode: str,
    sleep_s: float,
    dry_run: bool,
    force: bool,
    source_token: str,
    rewrite_sot: Optional[str],
    refresh_authorships: bool,
    include_department: bool,
) -> None:
    if merge_mode not in {"none", "doi", "doi+title"}:
        raise ValueError("merge_mode must be one of: none, doi, doi+title")

    with db_conn(dsn) as conn:
        conn.autocommit = False

        if force:
            LOGGER.info("OpenAlex backfill: force enabled (ignoring missing-data filter).")

        stats = BackfillStats()
        pubs = list(
            iter_publications_to_backfill(
                conn,
                only_pub_ids=only_pub_ids,
                max_n=max_n,
                force=force,
            )
        )
        if not pubs:
            LOGGER.info("No publications matched the backfill criteria.")
        else:
            LOGGER.info("OpenAlex backfill: %s publications queued.", len(pubs))

            oa = OpenAlexHTTP(OpenAlexConfig(mailto=mailto, sleep_s=sleep_s))
            try:
                for p in pubs:
                    try:
                        stats.processed += 1
                        backfill_one(
                            conn,
                            oa,
                            p,
                            merge_mode=merge_mode,
                            dry_run=dry_run,
                            source_token=source_token,
                            rewrite_sot=rewrite_sot,
                            refresh_authorships=refresh_authorships,
                            include_department=include_department,
                            stats=stats,
                        )
                        if not dry_run:
                            conn.commit()
                    except Exception:
                        conn.rollback()
                        LOGGER.exception("Failed backfilling pub_id=%s", p.pub_id)
            finally:
                oa.close()

    LOGGER.info(
        "openalex_backfill summary: processed=%s matched=%s updated=%s merged=%s skipped_no_match=%s",
        stats.processed,
        stats.matched,
        stats.updated,
        stats.merged,
        stats.skipped_no_match,
    )
    LOGGER.info(
        "openalex_backfill changes: doi=%s title=%s year=%s venue=%s source_of_truth=%s raw_json=%s",
        stats.changed_doi,
        stats.changed_title,
        stats.changed_year,
        stats.changed_venue,
        stats.changed_sot,
        stats.changed_raw,
    )
    if refresh_authorships:
        LOGGER.info("openalex_backfill authorships_refreshed=%s", stats.authorship_refreshed)
        if stats.authors_added:
            added = []
            for pid, entry in sorted(stats.authors_added.items()):
                name = entry.get("name") or ""
                pubs = sorted(entry.get("pub_ids") or [])
                added.append(f"person_id={pid} name='{name}' pubs={pubs}")
            LOGGER.info("openalex_backfill authors_added: %s", "; ".join(added))


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill publications from OpenAlex and merge duplicates by DOI.")
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (defaults to people_pubs.config.DEFAULT_DSN)")
    p.add_argument("--mailto", default=CROSSREF_MAILTO, help="Email for OpenAlex polite usage (recommended).")
    p.add_argument("--max", type=int, default=200, help="Max publications to process.")
    p.add_argument(
        "--only-pub-id",
        default=None,
        help="Process one pub_id or a CSV file (single column of pub_ids).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Ignore the missing-data filter and backfill anyway.",
    )
    p.add_argument(
        "--merge-mode",
        default="doi",
        choices=["none", "doi", "doi+title"],
        help="Duplicate merge strategy. Default: doi (merge only on exact DOI collision).",
    )
    p.add_argument(
        "--refresh-authorships",
        "--refresh_authorships",
        dest="refresh_authorships",
        action="store_true",
        help="Refresh authorships from OpenAlex for processed publications.",
    )
    p.add_argument(
        "--department",
        action="store_true",
        help="Include department fields in affiliation strings when available.",
    )
    p.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between OpenAlex requests.")
    p.add_argument("--dry-run", action="store_true", help="Do not write to DB.")
    p.add_argument("--debug", action="store_true", help="Enable debug logging.")
    p.add_argument(
        "--source-token",
        default="openalex",
        help="Token to merge into source_of_truth (default: openalex).",
    )
    p.add_argument(
        "--rewrite-sot",
        help="Replace source_of_truth instead of merging.",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
    )

    only_pub_ids = _parse_pub_id_list(args.only_pub_id)
    run_openalex_backfill(
        dsn=args.dsn,
        mailto=args.mailto,
        max_n=args.max,
        only_pub_ids=only_pub_ids,
        merge_mode=args.merge_mode,
        sleep_s=args.sleep,
        dry_run=args.dry_run,
        force=args.force,
        source_token=args.source_token,
        rewrite_sot=args.rewrite_sot,
        refresh_authorships=args.refresh_authorships,
        include_department=args.department,
    )


if __name__ == "__main__":
    main()
