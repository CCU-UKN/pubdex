#!/usr/bin/env python3
"""
Crossref backfill + duplicate merge for people_pubs.

Typical usage:
  python -m people_pubs.sync.crossref_backfill --max 200 --debug
  python -m people_pubs.sync.crossref_backfill --only-pub-id 1210 --debug
  python -m people_pubs.sync.crossref_backfill --only-pub-id 1210 --force --debug
  python -m people_pubs.sync.crossref_backfill --merge-mode doi --dry-run

What it does:
- Finds publications that appear not Crossref-enriched (raw_json missing 'crossref')
  and/or missing doi/year/venue, unless --force is used.
- Queries Crossref:
    - by DOI if present
    - otherwise by title (and optional year window)
- Updates biblio.publications in-place (optionally refreshes authorships with --refresh-authorships).
- If Crossref returns a DOI that already exists under a different pub_id,
  merges (moves) child rows and deletes the loser.

Assumptions:
- psycopg v3 is used (as in your project).
- Your schema matches what you pasted (biblio.publications/authorships, pii.publication_author_emails).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from people_pubs.utils.identifiers import normalize_orcid




import httpx
import psycopg
from psycopg.types.json import Jsonb

from people_pubs.config import DEFAULT_DSN, USER_AGENT
from people_pubs.dedupe_policy import choose_canonical_publication, publication_candidate_from
from people_pubs.db.connection import db_conn
from people_pubs.db.authorships import compute_order_tag, infer_equal_contrib_tag, upsert_authorship
from people_pubs.db.people import (
    lookup_internal_person_for_author,
    lookup_orcid_for_person_id,
    upsert_person_name_alias,
)
from people_pubs.db.publications import (
    build_source_provenance,
    merge_publication_provenance,
    merge_publication_source_provenance,
    merge_raw_json,
    merge_source_of_truth,
    merge_source_of_truth_replace_family,
)
from people_pubs.sync.scaffold import BackfillStats, _note_author_added, _parse_pub_id_list, _record_pub_changes, _row_get, _strip_orcid

# Try to reuse project helpers when present.
try:
    from people_pubs.services.crossref_client import (
        normalize_doi as _normalize_doi,
        pick_publication_date as _pick_publication_date,
        guess_is_preprint as _guess_is_preprint,
    )
except Exception:  # pragma: no cover
    _normalize_doi = None
    _pick_publication_date = None
    _guess_is_preprint = None


LOGGER = logging.getLogger("people_pubs.crossref_backfill")


def _coerce_aff_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _coerce_aff_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, dict):
        for key in ("name", "value", "text"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in value.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _format_crossref_affiliation(item: Any, include_department: bool) -> Optional[str]:
    if isinstance(item, str):
        s = item.strip()
        return s or None
    if not isinstance(item, dict):
        return None
    parts: List[str] = []
    name = _coerce_aff_text(item.get("name"))
    if name:
        parts.append(name)
    if include_department:
        for dept in _coerce_aff_list(item.get("department")):
            s = _coerce_aff_text(dept)
            if s:
                parts.append(s)
    if not parts:
        return None
    deduped: List[str] = []
    seen: set[str] = set()
    for part in parts:
        if part in seen:
            continue
        seen.add(part)
        deduped.append(part)
    return "; ".join(deduped)


def _iter_authors_from_crossref(
    cr_msg: Dict[str, Any],
    include_department: bool = False,
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
        for item in _coerce_aff_list(aff_raw):
            formatted = _format_crossref_affiliation(item, include_department)
            if formatted:
                affs.append(formatted)
        yield name, _strip_orcid(a.get("ORCID")), affs, a


def _refresh_authorships_from_crossref(
    conn: psycopg.Connection,
    publication_id: int,
    cr_msg: Dict[str, Any],
    *,
    include_department: bool,
    dry_run: bool,
    stats: BackfillStats,
) -> None:
    authors = list(_iter_authors_from_crossref(cr_msg, include_department=include_department))
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
    for pos, (full_name, auth_orcid, affiliations, raw_author) in enumerate(authors, start=1):
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
                source="crossref.author_name",
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
    if not processed_any:
        LOGGER.info(
            "Skipping authorship refresh for pub_id=%s (no affiliations in Crossref author data)",
            publication_id,
        )
        return
    stats.authorship_refreshed += 1


# ----------------------------
# Models / normalization
# ----------------------------

@dataclass(frozen=True)
class PubRow:
    pub_id: int
    doi: Optional[str]
    title: str
    year: Optional[int]
    venue: Optional[str]
    source_of_truth: str
    raw_json: Optional[dict]
    is_preprint: Optional[bool]


def normalize_doi(doi: Optional[str]) -> Optional[str]:
    if not doi:
        return None
    doi = doi.strip()
    if not doi:
        return None
    if _normalize_doi:
        return _normalize_doi(doi)
    # Minimal fallback
    doi = doi.lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi).strip()
    return doi or None


_TAG_RE = re.compile(r"<[^>]+>")


def norm_title(s: str) -> str:
    # Remove HTML tags (<b> etc.), normalize whitespace/punct.
    s = _TAG_RE.sub("", s or "")
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    # Keep letters/digits/spaces; remove most punctuation for robust matching.
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def title_similarity(a: str, b: str) -> float:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def parse_crossref_year(msg: Dict[str, Any]) -> Optional[int]:
    # Crossref date fields: issued / published-online / published-print / created
    for key in ("issued", "published-online", "published-print", "created"):
        obj = msg.get(key) or {}
        parts = obj.get("date-parts") or []
        if parts and isinstance(parts, list) and parts[0] and isinstance(parts[0], list):
            y = parts[0][0]
            if isinstance(y, int):
                return y
    return None


def pick_publication_date(msg: Dict[str, Any], fallback_year: Optional[int]) -> Optional[dt.date]:
    if _pick_publication_date:
        try:
            return _pick_publication_date(msg, fallback_year)
        except TypeError:
            # Some older helper signatures may differ; fall through to local.
            pass

    # Local fallback: choose best available year/month/day, else fallback_year-01-01.
    for key in ("issued", "published-online", "published-print", "created"):
        obj = msg.get(key) or {}
        parts = obj.get("date-parts") or []
        if parts and isinstance(parts, list) and parts[0] and isinstance(parts[0], list):
            p = parts[0]
            y = p[0] if len(p) > 0 else None
            m = p[1] if len(p) > 1 else 1
            d = p[2] if len(p) > 2 else 1
            if isinstance(y, int):
                try:
                    return dt.date(y, int(m), int(d))
                except Exception:
                    return dt.date(y, 1, 1)
    if isinstance(fallback_year, int):
        return dt.date(fallback_year, 1, 1)
    return None


def guess_is_preprint(msg: Dict[str, Any], doi: Optional[str], venue: Optional[str], title: Optional[str]) -> bool:
    if _guess_is_preprint:
        try:
            return bool(_guess_is_preprint(msg or {}, doi, venue, title))
        except Exception:
            pass

    t = (msg.get("type") or "").lower()
    st = (msg.get("subtype") or "").lower()
    title_l = (title or "").lower()
    venue_l = (venue or "").lower()

    if "preprint" in st:
        return True
    if t in {"posted-content", "posted_content"}:
        return True
    if "(preprint)" in title_l:
        return True
    if "biorxiv" in venue_l or "medrxiv" in venue_l:
        return True
    return False


# ----------------------------
# Crossref HTTP client
# ----------------------------

class CrossrefHTTP:
    def __init__(self, mailto: Optional[str], timeout: float = 20.0, sleep_s: float = 0.0):
        self.mailto = mailto
        self.sleep_s = sleep_s
        ua = USER_AGENT
        if mailto and "mailto:" not in ua:
            # Keep UA stable; Crossref prefers a contact in UA or mailto param.
            ua = f"{ua} (+mailto:{mailto})"

        self.client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": ua, "Accept": "application/json"},
            http2=True,
        )

    def close(self) -> None:
        self.client.close()

    def _maybe_sleep(self) -> None:
        if self.sleep_s and self.sleep_s > 0:
            time.sleep(self.sleep_s)

    def get_work(self, doi: str) -> Optional[Dict[str, Any]]:
        doi = normalize_doi(doi) or doi
        url = f"https://api.crossref.org/works/{doi}"
        self._maybe_sleep()
        r = self.client.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        return data.get("message")

    def search_best_by_title(
        self,
        title: str,
        year: Optional[int],
        rows: int = 5,
    ) -> Optional[Dict[str, Any]]:
        q_title = (title or "").strip()
        if not q_title:
            return None

        params: Dict[str, Any] = {
            "query.title": q_title,
            "rows": rows,
        }

        # Narrow by year window (year-1 .. year+1) if provided.
        if isinstance(year, int) and year > 0:
            y0 = max(year - 1, 1000)
            y1 = year + 1
            params["filter"] = f"from-pub-date:{y0}-01-01,until-pub-date:{y1}-12-31"

        self._maybe_sleep()
        r = self.client.get("https://api.crossref.org/works", params=params)
        r.raise_for_status()
        items = (r.json().get("message") or {}).get("items") or []
        if not items:
            return None

        best: Optional[Dict[str, Any]] = None
        best_score = -1.0

        for it in items:
            cand_titles = it.get("title") or []
            cand_title = cand_titles[0] if isinstance(cand_titles, list) and cand_titles else ""
            score = title_similarity(title, cand_title)

            # Soft preference for year match.
            if isinstance(year, int) and year > 0:
                cy = parse_crossref_year(it) or 0
                if cy == year:
                    score += 0.10
                elif abs(cy - year) == 1:
                    score += 0.03

            if score > best_score:
                best_score = score
                best = it

        # Be conservative: require at least “some” similarity.
        if best_score < 0.70:
            LOGGER.debug("Title match too weak (%.3f) for %r", best_score, title)
            return None
        return best


# ----------------------------
# DB: selection + updates
# ----------------------------

def iter_publications_to_backfill(
    conn: psycopg.Connection,
    only_pub_ids: Optional[List[int]],
    max_n: int,
    force: bool,
) -> Iterable[PubRow]:
    """
    Heuristic selection:
      - missing doi/year/venue
      OR
      - raw_json is NULL OR does not contain key 'crossref'
    """
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
        OR NOT COALESCE(raw_json ? 'crossref', FALSE)
      )
    """
    if only_pub_ids is None:
        sql += """
    ORDER BY
      updated_at ASC,
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
        rows = cur.fetchall()
    pub_rows: List[PubRow] = [
        PubRow(
            pub_id=int(_row_get(row, "pub_id", 0)),
            doi=_row_get(row, "doi", 1),
            title=_row_get(row, "title", 2),
            year=_row_get(row, "year", 3),
            venue=_row_get(row, "venue", 4),
            source_of_truth=_row_get(row, "source_of_truth", 5),
            raw_json=_row_get(row, "raw_json", 6),
            is_preprint=bool(_row_get(row, "is_preprint", 7)),
        )
        for row in rows
    ]

    if only_pub_ids:
        order = {pid: idx for idx, pid in enumerate(only_pub_ids)}
        pub_rows.sort(key=lambda r: order.get(r.pub_id, len(order)))
        if max_n is not None and max_n > 0:
            pub_rows = pub_rows[:max_n]

    for row in pub_rows:
        yield row


def find_pub_by_doi(conn: psycopg.Connection, doi: str) -> Optional[PubRow]:
    doi_norm = normalize_doi(doi) or doi
    sql = """
    SELECT pub_id, doi, title, year, venue, source_of_truth, raw_json, is_preprint
    FROM biblio.publications
    WHERE lower(
        regexp_replace(
            regexp_replace(trim(doi), '^\\s*doi:\\s*', '', 'i'),
            '^\\s*https?://(?:dx\\.)?doi\\.org/', '', 'i'
        )
    ) = lower(%(doi)s);
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"doi": doi_norm})
        row = cur.fetchone()
        if not row:
            return None
        return PubRow(
            pub_id=int(_row_get(row, "pub_id", 0)),
            doi=_row_get(row, "doi", 1),
            title=_row_get(row, "title", 2),
            year=_row_get(row, "year", 3),
            venue=_row_get(row, "venue", 4),
            source_of_truth=_row_get(row, "source_of_truth", 5),
            raw_json=_row_get(row, "raw_json", 6),
            is_preprint=bool(_row_get(row, "is_preprint", 7)),
        )


def update_publication_in_place(
    conn: psycopg.Connection,
    pub_id: int,
    *,
    doi: Optional[str],
    title: Optional[str],
    year: Optional[int],
    venue: Optional[str],
    source_of_truth: Optional[str],
    raw_json_patch: Optional[Dict[str, Any]],
    is_preprint: Optional[bool],
    dry_run: bool,
) -> None:
    """
    Update only provided non-None fields; raw_json is merged with raw_json_patch.
    """
    def _clean_str(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    sets: List[str] = []
    params: Dict[str, Any] = {"pub_id": pub_id}

    doi = _clean_str(doi)
    title = _clean_str(title)
    venue = _clean_str(venue)
    source_of_truth = _clean_str(source_of_truth)

    if doi is not None:
        sets.append("doi = %(doi)s")
        params["doi"] = doi
    if title is not None:
        sets.append("title = %(title)s")
        params["title"] = title
    if year is not None:
        sets.append("year = %(year)s")
        params["year"] = year
    if venue is not None:
        sets.append("venue = %(venue)s")
        params["venue"] = venue
    if source_of_truth is not None:
        sets.append("source_of_truth = %(source_of_truth)s")
        params["source_of_truth"] = source_of_truth
    if is_preprint is not None:
        sets.append("is_preprint = %(is_preprint)s")
        params["is_preprint"] = is_preprint

    if raw_json_patch is not None:
        # COALESCE(raw_json,'{}') || patch
        sets.append("raw_json = COALESCE(raw_json, '{}'::jsonb) || %(raw_json_patch)s")
        params["raw_json_patch"] = Jsonb(raw_json_patch)
        # Envelope entries track the payloads written by this patch.
        sets.append(
            "source_provenance = COALESCE(source_provenance, '{}'::jsonb) || %(source_provenance_patch)s"
        )
        params["source_provenance_patch"] = Jsonb(build_source_provenance(raw_json_patch))

    sets.append("updated_at = NOW()")

    if not sets:
        return

    sql = f"""
    UPDATE biblio.publications
    SET {", ".join(sets)}
    WHERE pub_id = %(pub_id)s;
    """

    if dry_run:
        LOGGER.info("[dry-run] would UPDATE pub_id=%s (fields=%s)", pub_id, ", ".join(sets))
        return

    with conn.cursor() as cur:
        cur.execute(sql, params)


def _clear_publication_doi(conn: psycopg.Connection, pub_id: int, *, dry_run: bool) -> None:
    """
    Clear DOI for a publication row (used to avoid unique conflicts during merges).
    """
    if dry_run:
        LOGGER.info("[dry-run] would clear DOI for pub_id=%s", pub_id)
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE biblio.publications
            SET doi = NULL,
                updated_at = NOW()
            WHERE pub_id = %s
            """,
            (pub_id,),
        )


def choose_canonical_pub(dest: PubRow, other: PubRow) -> Tuple[PubRow, PubRow]:
    """
    Decide which pub to keep when merging.

    Delegates to the shared publication canonical policy so backfill and manual
    DOI merge paths do not drift.
    """
    by_id = {dest.pub_id: dest, other.pub_id: other}
    choice = choose_canonical_publication(
        [publication_candidate_from(dest), publication_candidate_from(other)]
    )
    LOGGER.info(
        "canonical choice: winner=%s losers=%s reason=%s score=%s",
        choice.winner_pub_id,
        ",".join(str(pid) for pid in choice.loser_pub_ids),
        choice.reason,
        choice.winner_score,
    )
    winner = by_id[choice.winner_pub_id]
    loser = by_id[choice.loser_pub_ids[0]]
    return winner, loser


def merge_publications(
    conn: psycopg.Connection,
    *,
    winner_pub_id: int,
    loser_pub_id: int,
    dry_run: bool,
) -> None:
    """
    Merge loser into winner:
      - loser source_of_truth tokens + source-keyed raw_json absorbed into the
        winner (merge_publication_provenance) so neither is lost on merge
      - biblio.authorships moved with conflict-safe upsert semantics
      - pii.publication_author_emails moved (ON CONFLICT DO NOTHING)
      - loser publication deleted
    """
    if winner_pub_id == loser_pub_id:
        return

    LOGGER.info("Merging publications: winner=%s loser=%s", winner_pub_id, loser_pub_id)

    if dry_run:
        LOGGER.info(
            "[dry-run] would union source_of_truth + raw_json, move authorships + emails, and DELETE pub_id=%s",
            loser_pub_id,
        )
        return

    with conn.cursor() as cur:
        # Absorb the loser's provenance into the winner before deleting it, so a
        # DOI merge never drops source tokens or source-keyed raw_json payloads
        # regardless of which row trust order picked (see
        # merge_publication_provenance).
        cur.execute(
            "SELECT source_of_truth, raw_json, source_provenance FROM biblio.publications WHERE pub_id = %s",
            (winner_pub_id,),
        )
        winner_row = cur.fetchone()
        cur.execute(
            "SELECT source_of_truth, raw_json, source_provenance FROM biblio.publications WHERE pub_id = %s",
            (loser_pub_id,),
        )
        loser_row = cur.fetchone()
        if winner_row and loser_row:
            merged_sot, merged_raw = merge_publication_provenance(
                _row_get(winner_row, "source_of_truth", 0),
                _row_get(winner_row, "raw_json", 1),
                _row_get(loser_row, "source_of_truth", 0),
                _row_get(loser_row, "raw_json", 1),
            )
            merged_prov = merge_publication_source_provenance(
                _row_get(winner_row, "raw_json", 1),
                _row_get(winner_row, "source_provenance", 2),
                _row_get(loser_row, "raw_json", 1),
                _row_get(loser_row, "source_provenance", 2),
                _row_get(winner_row, "source_of_truth", 0) or "",
                _row_get(loser_row, "source_of_truth", 0) or "",
            )
            cur.execute(
                """
                UPDATE biblio.publications
                SET source_of_truth = %(sot)s,
                    raw_json = %(raw)s,
                    source_provenance = %(prov)s,
                    updated_at = NOW()
                WHERE pub_id = %(winner)s
                """,
                {
                    "sot": merged_sot,
                    "raw": Jsonb(merged_raw),
                    "prov": Jsonb(merged_prov) if merged_prov else None,
                    "winner": winner_pub_id,
                },
            )
            LOGGER.info(
                "  absorbed loser=%s provenance into winner=%s (source_of_truth=%r)",
                loser_pub_id,
                winner_pub_id,
                merged_sot,
            )

        # Move authorships:
        # - avoid paper_orcid_format violations by NULLIF('','')
        # - avoid (pub_id, person_id) partial unique violations by dropping person_id if already present
        move_authorships_sql = """
        INSERT INTO biblio.authorships (
          pub_id,
          person_id,
          author_position,
          display_name_at_pub,
          paper_affiliation,
          paper_orcid,
          is_corresponding,
          is_internal_at_ingest,
          order_tag,
          equal_contrib_tag,
          author_name,
          author_orcid,
          affiliations,
          raw_crossref_author_json
        )
        SELECT
          %(winner)s AS pub_id,
          CASE
            WHEN a.person_id IS NULL THEN NULL
            WHEN EXISTS (
              SELECT 1
              FROM biblio.authorships d
              WHERE d.pub_id = %(winner)s
                AND d.person_id = a.person_id
            ) THEN NULL
            ELSE a.person_id
          END AS person_id,
          a.author_position,
          a.display_name_at_pub,
          a.paper_affiliation,
          NULLIF(a.paper_orcid, '') AS paper_orcid,
          a.is_corresponding,
          a.is_internal_at_ingest,
          a.order_tag,
          a.equal_contrib_tag,
          a.author_name,
          NULLIF(a.author_orcid, '') AS author_orcid,
          a.affiliations,
          a.raw_crossref_author_json
        FROM biblio.authorships a
        WHERE a.pub_id = %(loser)s
        ON CONFLICT (pub_id, author_position) DO UPDATE
        SET
          person_id = COALESCE(biblio.authorships.person_id, EXCLUDED.person_id),
          display_name_at_pub = COALESCE(biblio.authorships.display_name_at_pub, EXCLUDED.display_name_at_pub),
          paper_affiliation = COALESCE(biblio.authorships.paper_affiliation, EXCLUDED.paper_affiliation),
          paper_orcid = COALESCE(biblio.authorships.paper_orcid, EXCLUDED.paper_orcid),
          is_corresponding = COALESCE(biblio.authorships.is_corresponding, EXCLUDED.is_corresponding),
          is_internal_at_ingest = biblio.authorships.is_internal_at_ingest OR EXCLUDED.is_internal_at_ingest,
          order_tag = COALESCE(biblio.authorships.order_tag, EXCLUDED.order_tag),
          equal_contrib_tag = COALESCE(biblio.authorships.equal_contrib_tag, EXCLUDED.equal_contrib_tag),
          author_name = COALESCE(biblio.authorships.author_name, EXCLUDED.author_name),
          author_orcid = COALESCE(biblio.authorships.author_orcid, EXCLUDED.author_orcid),
          affiliations = COALESCE(biblio.authorships.affiliations, EXCLUDED.affiliations),
          raw_crossref_author_json = COALESCE(biblio.authorships.raw_crossref_author_json, EXCLUDED.raw_crossref_author_json),
          updated_at = NOW();
        """
        cur.execute(move_authorships_sql, {"winner": winner_pub_id, "loser": loser_pub_id})

        # Move author emails through a SECURITY DEFINER function; routine
        # app_writer jobs should not need direct table privileges on pii.*.
        cur.execute("SELECT pii.move_publication_author_emails(%s, %s)", (winner_pub_id, loser_pub_id))

        # Delete loser publication (CASCADE removes any leftover child rows)
        cur.execute("DELETE FROM biblio.publications WHERE pub_id = %(loser)s;", {"loser": loser_pub_id})


# ----------------------------
# Backfill orchestration
# ----------------------------

def backfill_one(
    conn: psycopg.Connection,
    cr: CrossrefHTTP,
    pub: PubRow,
    *,
    source_token: str,
    merge_mode: str,
    dry_run: bool,
    refresh_authorships: bool,
    include_department: bool,
    stats: BackfillStats,
) -> None:
    """
    Backfill a single publication row.
    If Crossref resolves a DOI that already exists on another pub_id, merge duplicates.
    """
    # 1) Resolve Crossref record
    cr_msg: Optional[Dict[str, Any]] = None

    doi_norm = normalize_doi(pub.doi)
    if doi_norm:
        cr_msg = cr.get_work(doi_norm)

    if cr_msg is None:
        cr_msg = cr.search_best_by_title(pub.title, pub.year)

    if cr_msg is None:
        LOGGER.info("No Crossref match for pub_id=%s title=%r", pub.pub_id, pub.title)
        stats.skipped_no_match += 1
        return
    stats.matched += 1

    cr_doi = normalize_doi(cr_msg.get("DOI"))
    cr_titles = cr_msg.get("title") or []
    cr_title = cr_titles[0] if isinstance(cr_titles, list) and cr_titles else None
    containers = cr_msg.get("container-title") or []
    cr_venue = containers[0] if isinstance(containers, list) and containers else None
    if isinstance(cr_title, str):
        cr_title = cr_title.strip() or None
    if isinstance(cr_venue, str):
        cr_venue = cr_venue.strip() or None

    fallback_year = pub.year
    pub_date = pick_publication_date(cr_msg, fallback_year)
    cr_year = pub_date.year if pub_date else (fallback_year if isinstance(fallback_year, int) else None)

    is_preprint = guess_is_preprint(cr_msg, cr_doi, cr_venue, cr_title or pub.title)

    raw_patch = merge_raw_json(
        pub.raw_json,
        pub.source_of_truth,
        {
            "crossref": cr_msg,
            "crossref_backfill": {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        },
    )

    # 2) If Crossref DOI collides with another publication row, merge
    if merge_mode in {"doi", "doi+title"} and cr_doi:
        other = find_pub_by_doi(conn, cr_doi)
        if other and other.pub_id != pub.pub_id:
            # Decide canonical (winner) and merge
            a = pub
            b = other
            winner, loser = choose_canonical_pub(a, b)

            # Ensure winner has the DOI (it might be the ORCID row)
            # We update winner in-place, then merge loser into it.
            if loser.doi:
                LOGGER.info(
                    "Clearing DOI on loser pub_id=%s to avoid conflict before merge",
                    loser.pub_id,
                )
                _clear_publication_doi(conn, loser.pub_id, dry_run=dry_run)
            winner_sot = merge_source_of_truth_replace_family(
                winner.source_of_truth,
                source_token,
                "crossref",
            )
            update_publication_in_place(
                conn,
                winner.pub_id,
                doi=cr_doi,
                title=cr_title,  # OK to override with Crossref's title if provided
                year=cr_year,
                venue=cr_venue,
                source_of_truth=winner_sot,
                raw_json_patch=raw_patch,
                is_preprint=is_preprint,
                dry_run=dry_run,
            )
            stats.updated += 1
            stats.merged += 1
            _record_pub_changes(
                stats,
                winner,
                doi=cr_doi,
                title=cr_title,
                year=cr_year,
                venue=cr_venue,
                source_of_truth=winner_sot,
                raw_patch=True,
            )

            merge_publications(conn, winner_pub_id=winner.pub_id, loser_pub_id=loser.pub_id, dry_run=dry_run)
            if refresh_authorships:
                _refresh_authorships_from_crossref(
                    conn,
                    winner.pub_id,
                    cr_msg,
                    include_department=include_department,
                    dry_run=dry_run,
                    stats=stats,
                )
            return

    # 3) No DOI collision merge; update this row in-place.
    # - If doi was NULL and Crossref provides a DOI, set it (may still fail if unique constraint hits later).
    # - Title/year/venue are updated if Crossref has them.
    sot = merge_source_of_truth_replace_family(
        pub.source_of_truth,
        source_token,
        "crossref",
    )
    update_publication_in_place(
        conn,
        pub.pub_id,
        doi=cr_doi if (pub.doi is None and cr_doi) else None,
        title=cr_title,
        year=cr_year,
        venue=cr_venue,
        source_of_truth=sot,
        raw_json_patch=raw_patch,
        is_preprint=is_preprint,
        dry_run=dry_run,
    )
    stats.updated += 1
    _record_pub_changes(
        stats,
        pub,
        doi=cr_doi if (pub.doi is None and cr_doi) else pub.doi,
        title=cr_title,
        year=cr_year,
        venue=cr_venue,
        source_of_truth=sot,
        raw_patch=True,
    )
    if refresh_authorships:
        _refresh_authorships_from_crossref(
            conn,
            pub.pub_id,
            cr_msg,
            include_department=include_department,
            dry_run=dry_run,
            stats=stats,
        )

    LOGGER.info(
        "Backfilled pub_id=%s doi=%r year=%r venue=%r",
        pub.pub_id,
        cr_doi,
        cr_year,
        cr_venue,
    )


def run_crossref_backfill(
    *,
    dsn: str,
    mailto: Optional[str],
    max_n: int,
    only_pub_ids: Optional[List[int]],
    merge_mode: str,
    sleep_s: float,
    dry_run: bool,
    force: bool,
    refresh_authorships: bool,
    source_token: str,
    include_department: bool,
) -> BackfillStats:
    if merge_mode not in {"none", "doi", "doi+title"}:
        raise ValueError("merge_mode must be one of: none, doi, doi+title")

    with db_conn(dsn) as conn:
        conn.autocommit = False

        if force:
            LOGGER.info("Crossref backfill: force enabled (ignoring missing-data filter).")

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
            LOGGER.info("Crossref backfill: %s publications queued.", len(pubs))

            cr = CrossrefHTTP(mailto=mailto, sleep_s=sleep_s)
            try:
                for p in pubs:
                    try:
                        stats.processed += 1
                        backfill_one(
                            conn,
                            cr,
                            p,
                            source_token=source_token,
                            merge_mode=merge_mode,
                            dry_run=dry_run,
                            refresh_authorships=refresh_authorships,
                            include_department=include_department,
                            stats=stats,
                        )
                        if not dry_run:
                            conn.commit()
                    except Exception:
                        conn.rollback()
                        stats.errors += 1
                        stats.failed_pub_ids.add(p.pub_id)
                        LOGGER.exception("Failed backfilling pub_id=%s", p.pub_id)
            finally:
                cr.close()

    LOGGER.info(
        "crossref_backfill summary: processed=%s matched=%s updated=%s merged=%s skipped_no_match=%s errors=%s",
        stats.processed,
        stats.matched,
        stats.updated,
        stats.merged,
        stats.skipped_no_match,
        stats.errors,
    )
    LOGGER.info(
        "crossref_backfill changes: doi=%s title=%s year=%s venue=%s source_of_truth=%s raw_json=%s",
        stats.changed_doi,
        stats.changed_title,
        stats.changed_year,
        stats.changed_venue,
        stats.changed_sot,
        stats.changed_raw,
    )
    if refresh_authorships:
        LOGGER.info("crossref_backfill authorships_refreshed=%s", stats.authorship_refreshed)
        if stats.authors_added:
            added = []
            for pid, entry in sorted(stats.authors_added.items()):
                name = entry.get("name") or ""
                pubs = sorted(entry.get("pub_ids") or [])
                added.append(f"person_id={pid} name='{name}' pubs={pubs}")
            LOGGER.info("crossref_backfill authors_added: %s", "; ".join(added))
    return stats


# ----------------------------
# CLI
# ----------------------------

def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill publications from Crossref and merge duplicates by DOI.")
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (defaults to people_pubs.config.DEFAULT_DSN)")
    p.add_argument("--mailto", default=None, help="Email for Crossref polite usage (recommended).")
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
        "--source-token",
        default="crossref",
        help="Token to merge into source_of_truth (default: crossref).",
    )
    p.add_argument(
        "--refresh-authorships",
        "--refresh_authorships",
        dest="refresh_authorships",
        action="store_true",
        help="Refresh authorships from Crossref for processed publications.",
    )
    p.add_argument(
        "--department",
        action="store_true",
        help="Include Crossref department fields in affiliation strings when present.",
    )
    p.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between Crossref requests.")
    p.add_argument("--dry-run", action="store_true", help="Do not write to DB.")
    p.add_argument("--debug", action="store_true", help="Enable debug logging.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    only_pub_ids = _parse_pub_id_list(args.only_pub_id)
    run_crossref_backfill(
        dsn=args.dsn,
        mailto=args.mailto,
        max_n=args.max,
        only_pub_ids=only_pub_ids,
        merge_mode=args.merge_mode,
        sleep_s=args.sleep,
        dry_run=args.dry_run,
        force=args.force,
        refresh_authorships=args.refresh_authorships,
        source_token=args.source_token,
        include_department=args.department,
    )


if __name__ == "__main__":
    main()
