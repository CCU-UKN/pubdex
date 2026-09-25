#!/usr/bin/env python3
"""
DBLP backfill for people_pubs.

Typical usage:
  python -m people_pubs.sync.dblp_backfill --max 200 --debug
  python -m people_pubs.sync.dblp_backfill --only-pub-id 1210 --debug
  python -m people_pubs.sync.dblp_backfill --only-pub-id 1210 --force --debug
  python -m people_pubs.sync.dblp_backfill --merge-mode doi --dry-run

What it does:
- Finds publications that appear not DBLP-enriched (raw_json missing 'dblp')
  and/or missing doi/year/venue, unless --force is used.
- Queries DBLP:
    - by DOI if present
    - otherwise by title (and optional year window)
- Updates biblio.publications in-place (optionally refreshes authorships with --refresh-authorships).
- If DBLP returns a DOI that already exists under a different pub_id,
  merges (moves) child rows and deletes the loser (same merge flow as Crossref).

Notes:
- DBLP does not provide structured affiliations; authorship refresh will
  be skipped when no affiliations are present.
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

from people_pubs.config import DEFAULT_DSN, USER_AGENT
from people_pubs.db.connection import db_conn
from people_pubs.db.authorships import compute_order_tag, upsert_authorship
from people_pubs.db.people import (
    lookup_internal_person_for_author,
    lookup_orcid_for_person_id,
    upsert_person_name_alias,
)
from people_pubs.db.publications import merge_raw_json
from people_pubs.services.crossref_client import normalize_doi, guess_is_preprint
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
from people_pubs.sync.scaffold import BackfillStats, _note_author_added, _parse_pub_id_list, _record_pub_changes, _row_get


LOGGER = logging.getLogger("people_pubs.dblp_backfill")
DBLP_BASE = "https://dblp.org"
DBLP_MAX_RETRIES = 6
DBLP_BACKOFF_BASE = 2.0
DBLP_BACKOFF_MAX = 32.0


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _clean_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    t = _strip_html(str(title))
    if t.endswith("."):
        t = t[:-1].rstrip()
    return t or None


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


def _info_title(info: Dict[str, Any]) -> Optional[str]:
    return _clean_title(info.get("title"))


def _info_venue(info: Dict[str, Any]) -> Optional[str]:
    for key in ("venue", "booktitle", "journal", "series"):
        value = (info.get(key) or "").strip()
        if value:
            return value
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


def _refresh_authorships_from_dblp(
    conn: psycopg.Connection,
    publication_id: int,
    info: Dict[str, Any],
    *,
    include_department: bool,
    dry_run: bool,
    stats: BackfillStats,
) -> None:
    authors = list(_iter_authors_from_dblp(info))
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
    for pos, (full_name, auth_orcid, affiliations) in enumerate(authors, start=1):
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
                source="dblp.author_name",
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
    if not processed_any:
        LOGGER.info(
            "Skipping authorship refresh for pub_id=%s (no affiliations in DBLP author data)",
            publication_id,
        )
        return
    stats.authorship_refreshed += 1


@dataclass(frozen=True)
class DblpConfig:
    sleep_s: float = 1.0
    timeout: float = 20.0
    max_retries: int = DBLP_MAX_RETRIES
    backoff_base: float = DBLP_BACKOFF_BASE
    backoff_max: float = DBLP_BACKOFF_MAX


class DblpHTTP:
    def __init__(self, config: DblpConfig):
        self.config = config
        self.client = httpx.Client(
            timeout=config.timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            http2=True,
        )

    def close(self) -> None:
        self.client.close()

    def _maybe_sleep(self) -> None:
        if self.config.sleep_s and self.config.sleep_s > 0:
            time.sleep(self.config.sleep_s)

    @staticmethod
    def _retry_after_seconds(resp: httpx.Response) -> Optional[float]:
        val = resp.headers.get("Retry-After")
        if not val:
            return None
        try:
            return float(val)
        except ValueError:
            return None

    def _search(self, query: str, *, rows: int = 5, start: int = 0) -> List[Dict[str, Any]]:
        params = {"q": query, "format": "json", "h": rows, "f": start}
        attempt = 0
        while True:
            self._maybe_sleep()
            r = self.client.get(f"{DBLP_BASE}/search/publ/api", params=params)
            if r.status_code == 429:
                attempt += 1
                if attempt > self.config.max_retries:
                    r.raise_for_status()
                wait = self._retry_after_seconds(r)
                if wait is None:
                    wait = min(
                        self.config.backoff_base * (2 ** (attempt - 1)),
                        self.config.backoff_max,
                    )
                LOGGER.warning(
                    "DBLP rate limited (429). Sleeping %.1fs before retry %s/%s.",
                    wait,
                    attempt,
                    self.config.max_retries,
                )
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                attempt += 1
                if attempt > self.config.max_retries:
                    r.raise_for_status()
                wait = min(
                    self.config.backoff_base * (2 ** (attempt - 1)),
                    self.config.backoff_max,
                )
                LOGGER.warning(
                    "DBLP error %s. Sleeping %.1fs before retry %s/%s.",
                    r.status_code,
                    wait,
                    attempt,
                    self.config.max_retries,
                )
                time.sleep(wait)
                continue
            r.raise_for_status()
            payload = r.json() or {}
            break
        hits = ((payload.get("result") or {}).get("hits") or {})
        hit_list = hits.get("hit") or []
        if isinstance(hit_list, dict):
            hit_list = [hit_list]
        out: List[Dict[str, Any]] = []
        for hit in hit_list:
            info = hit.get("info") if isinstance(hit, dict) else None
            if isinstance(info, dict):
                out.append(info)
        return out

    def search_by_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        doi = normalize_doi(doi) or doi
        hits = self._search(f"doi:{doi}", rows=5)
        if not hits:
            hits = self._search(doi, rows=5)
        for info in hits:
            hit_doi = _extract_doi(info)
            if hit_doi and normalize_doi(hit_doi) == normalize_doi(doi):
                return info
        return hits[0] if hits else None

    def search_best_by_title(
        self,
        title: str,
        year: Optional[int],
        rows: int = 5,
    ) -> Optional[Dict[str, Any]]:
        q = (title or "").strip()
        if not q:
            return None
        hits = self._search(_clean_title(q) or q, rows=rows)
        if not hits:
            return None

        best: Optional[Dict[str, Any]] = None
        best_score = -1.0
        for info in hits:
            cand_title = _info_title(info) or ""
            score = title_similarity(title, cand_title)
            if isinstance(year, int) and year > 0:
                cy = _parse_year(info.get("year")) or 0
                if cy == year:
                    score += 0.10
                elif abs(cy - year) == 1:
                    score += 0.03
            if score > best_score:
                best_score = score
                best = info

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
        OR NOT COALESCE(raw_json ? 'dblp', FALSE)
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
    dblp: DblpHTTP,
    pub: PubRow,
    *,
    merge_mode: str,
    dry_run: bool,
    source_token: str,
    rewrite_sot: Optional[str],
    refresh_authorships: bool,
    include_department: bool,
    title_fallback: bool,
    stats: BackfillStats,
) -> None:
    info: Optional[Dict[str, Any]] = None

    doi_norm = normalize_doi(pub.doi or "")
    if doi_norm:
        info = dblp.search_by_doi(doi_norm)

    if info is None and (not doi_norm or title_fallback):
        info = dblp.search_best_by_title(pub.title, pub.year)

    if info is None:
        LOGGER.info("No DBLP match for pub_id=%s title=%r", pub.pub_id, pub.title)
        stats.skipped_no_match += 1
        return
    stats.matched += 1

    dblp_doi = _extract_doi(info) or doi_norm or pub.doi
    title = _info_title(info) or pub.title
    venue = _info_venue(info) or pub.venue
    year = _parse_year(info.get("year")) or pub.year
    is_preprint = guess_is_preprint({}, doi=dblp_doi, venue=venue, title=title)

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
            "dblp": info,
            "dblp_backfill": {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        },
    )

    if merge_mode in {"doi", "doi+title"} and dblp_doi:
        other = find_pub_by_doi(conn, dblp_doi)
        if other and other.pub_id != pub.pub_id:
            winner, loser = choose_canonical_pub(pub, other)
            if loser.doi:
                LOGGER.info(
                    "Clearing DOI on loser pub_id=%s to avoid conflict before merge",
                    loser.pub_id,
                )
                _clear_publication_doi(conn, loser.pub_id, dry_run=dry_run)
            update_publication_in_place(
                conn,
                winner.pub_id,
                doi=dblp_doi,
                title=title,
                year=year,
                venue=venue,
                source_of_truth=sot,
                raw_json_patch=raw_patch,
                is_preprint=is_preprint,
                dry_run=dry_run,
            )
            stats.updated += 1
            stats.merged += 1
            _record_pub_changes(
                stats,
                winner,
                doi=dblp_doi,
                title=title,
                year=year,
                venue=venue,
                source_of_truth=sot,
                raw_patch=True,
            )
            merge_publications(conn, winner_pub_id=winner.pub_id, loser_pub_id=loser.pub_id, dry_run=dry_run)
            if refresh_authorships:
                _refresh_authorships_from_dblp(
                    conn,
                    winner.pub_id,
                    info,
                    include_department=include_department,
                    dry_run=dry_run,
                    stats=stats,
                )
            return

    update_publication_in_place(
        conn,
        pub.pub_id,
        doi=dblp_doi if (pub.doi is None and dblp_doi) else None,
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
        doi=dblp_doi if (pub.doi is None and dblp_doi) else pub.doi,
        title=title,
        year=year,
        venue=venue,
        source_of_truth=sot,
        raw_patch=True,
    )

    if refresh_authorships:
        _refresh_authorships_from_dblp(
            conn,
            pub.pub_id,
            info,
            include_department=include_department,
            dry_run=dry_run,
            stats=stats,
        )


def run_dblp_backfill(
    *,
    dsn: str,
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
    title_fallback: bool,
) -> None:
    if merge_mode not in {"none", "doi", "doi+title"}:
        raise ValueError("merge_mode must be one of: none, doi, doi+title")

    with db_conn(dsn) as conn:
        conn.autocommit = False

        if force:
            LOGGER.info("DBLP backfill: force enabled (ignoring missing-data filter).")

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
            LOGGER.info("DBLP backfill: %s publications queued.", len(pubs))

            dblp = DblpHTTP(DblpConfig(sleep_s=sleep_s))
            try:
                for p in pubs:
                    try:
                        stats.processed += 1
                        backfill_one(
                            conn,
                            dblp,
                            p,
                            merge_mode=merge_mode,
                            dry_run=dry_run,
                            source_token=source_token,
                            rewrite_sot=rewrite_sot,
                            refresh_authorships=refresh_authorships,
                            include_department=include_department,
                            title_fallback=title_fallback,
                            stats=stats,
                        )
                        if not dry_run:
                            conn.commit()
                    except Exception:
                        conn.rollback()
                        LOGGER.exception("Failed backfilling pub_id=%s", p.pub_id)
            finally:
                dblp.close()

    LOGGER.info(
        "dblp_backfill summary: processed=%s matched=%s updated=%s merged=%s skipped_no_match=%s",
        stats.processed,
        stats.matched,
        stats.updated,
        stats.merged,
        stats.skipped_no_match,
    )
    LOGGER.info(
        "dblp_backfill changes: doi=%s title=%s year=%s venue=%s source_of_truth=%s raw_json=%s",
        stats.changed_doi,
        stats.changed_title,
        stats.changed_year,
        stats.changed_venue,
        stats.changed_sot,
        stats.changed_raw,
    )
    if refresh_authorships:
        LOGGER.info("dblp_backfill authorships_refreshed=%s", stats.authorship_refreshed)
        if stats.authors_added:
            added = []
            for pid, entry in sorted(stats.authors_added.items()):
                name = entry.get("name") or ""
                pubs = sorted(entry.get("pub_ids") or [])
                added.append(f"person_id={pid} name='{name}' pubs={pubs}")
            LOGGER.info("dblp_backfill authors_added: %s", "; ".join(added))


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill publications from DBLP and merge duplicates by DOI.")
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (defaults to people_pubs.config.DEFAULT_DSN)")
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
        help="Refresh authorships from DBLP for processed publications.",
    )
    p.add_argument(
        "--department",
        action="store_true",
        help="Include department fields in affiliation strings when available.",
    )
    p.add_argument("--sleep", type=float, default=1.0, help="Seconds to sleep between DBLP requests.")
    p.add_argument("--dry-run", action="store_true", help="Do not write to DB.")
    p.add_argument(
        "--source-token",
        default="dblp",
        help="Token to merge into source_of_truth (default: dblp).",
    )
    p.add_argument("--rewrite-sot", help="Replace source_of_truth for DBLP.")
    p.add_argument(
        "--title-fallback",
        action="store_true",
        help="Fallback to title search if DOI lookup fails.",
    )
    p.add_argument("--debug", action="store_true", help="Enable debug logging.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    run_dblp_backfill(
        dsn=args.dsn or DEFAULT_DSN,
        max_n=args.max,
        only_pub_ids=_parse_pub_id_list(args.only_pub_id),
        merge_mode=args.merge_mode,
        sleep_s=args.sleep,
        dry_run=args.dry_run,
        force=args.force,
        source_token=str(args.source_token or "dblp"),
        rewrite_sot=args.rewrite_sot,
        refresh_authorships=args.refresh_authorships,
        include_department=args.department,
        title_fallback=args.title_fallback,
    )


if __name__ == "__main__":
    main()
