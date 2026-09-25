"""Shared scaffolding for the sync backfill/search CLIs.

These helpers were copy-pasted across the seven *_backfill.py and seven
*_search.py modules (~1,500 duplicated lines). They are
extracted verbatim from the majority variant of each helper; the few modules
whose copies had deliberately different behavior keep a thin local wrapper
that passes the matching parameters (see the git history of this file for the
variant inventory). Behavior contract: identical CLI flags, identical
end-of-run summary output, identical DB writes.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from people_pubs.utils.identifiers import normalize_orcid

LOGGER = logging.getLogger(__name__)

# Sentinel: _row_get re-raises the tuple-index failure by default (the
# majority behavior); callers that historically returned None instead pass
# default=None.
_ROW_GET_RAISE = object()


def _row_get(row: Any, key: str, idx: int, *, default: Any = _ROW_GET_RAISE) -> Any:
    """
    psycopg can return rows as tuples (tuple_row) or dict-like mappings
    (dict_row), depending on row_factory. This helper makes the code resilient.
    """
    try:
        return row[key]
    except Exception:
        if default is _ROW_GET_RAISE:
            return row[idx]
        try:
            return row[idx]
        except Exception:
            return default


def _strip_orcid(orcid: Optional[str]) -> Optional[str]:
    return normalize_orcid(orcid)


def _parse_pub_id_list(
    value: Optional[str], *, detect_header_column: bool = True
) -> Optional[List[int]]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    path = Path(raw)
    if path.exists() and path.is_file():
        pub_ids: List[int] = []
        with path.open(newline="") as f:
            reader = csv.reader(f)
            header_seen = not detect_header_column
            pub_col = 0
            for row in reader:
                if not row:
                    continue
                if not header_seen:
                    normalized = []
                    for cell in row:
                        cell_norm = str(cell or "").strip().strip('"').lower()
                        cell_norm = cell_norm.replace("-", "_").replace(" ", "_")
                        normalized.append(cell_norm)
                    if "pub_id" in normalized:
                        pub_col = normalized.index("pub_id")
                        header_seen = True
                        continue
                    header_seen = True
                cell = (row[pub_col] if pub_col < len(row) else "").strip()
                if not cell:
                    continue
                try:
                    pub_ids.append(int(cell))
                except ValueError:
                    continue
        deduped: List[int] = []
        seen: set[int] = set()
        for pid in pub_ids:
            if pid in seen:
                continue
            seen.add(pid)
            deduped.append(pid)
        return deduped or None
    if "," in raw:
        pub_ids: List[int] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                pub_ids.append(int(part))
            except ValueError:
                continue
        deduped: List[int] = []
        seen: set[int] = set()
        for pid in pub_ids:
            if pid in seen:
                continue
            seen.add(pid)
            deduped.append(pid)
        return deduped or None
    try:
        return [int(raw)]
    except ValueError as exc:
        raise SystemExit("--only-pub-id must be an integer or path to a CSV file.") from exc


def _parse_person_id_list(value: Optional[str]) -> Optional[List[int]]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    path = Path(raw)
    if path.exists() and path.is_file():
        person_ids: List[int] = []
        with path.open(newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                cell = (row[0] or "").strip()
                if not cell:
                    continue
                try:
                    person_ids.append(int(cell))
                except ValueError:
                    continue
        deduped: List[int] = []
        seen: set[int] = set()
        for pid in person_ids:
            if pid in seen:
                continue
            seen.add(pid)
            deduped.append(pid)
        return deduped or None
    if "," in raw:
        person_ids = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                person_ids.append(int(part))
            except ValueError:
                continue
        deduped = []
        seen: set[int] = set()
        for pid in person_ids:
            if pid in seen:
                continue
            seen.add(pid)
            deduped.append(pid)
        return deduped or None
    try:
        return [int(raw)]
    except ValueError as exc:
        raise SystemExit("--only-person-id must be an integer, CSV file, or comma-separated list.") from exc


@dataclass
class BackfillStats:
    processed: int = 0
    matched: int = 0
    updated: int = 0
    merged: int = 0
    skipped_no_match: int = 0
    errors: int = 0
    failed_pub_ids: set[int] = field(default_factory=set)
    changed_doi: int = 0
    changed_title: int = 0
    changed_year: int = 0
    changed_venue: int = 0
    changed_sot: int = 0
    changed_raw: int = 0
    authorship_refreshed: int = 0
    authors_added: Dict[int, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class SearchStats:
    processed: int = 0
    inserted: int = 0
    updated: int = 0
    skipped_no_doi: int = 0
    skipped_existing: int = 0
    errors: int = 0
    # Used by crossref_search only:
    skipped_affiliation_mismatch: int = 0
    # Used by dblp_search_people only:
    people: int = 0
    names: int = 0


def _record_pub_changes(
    stats: BackfillStats,
    # Duck-typed: each backfill passes its own PubRow-like object exposing
    # .doi/.title/.year/.venue/.source_of_truth attributes.
    existing: Any,
    *,
    doi: Optional[str],
    title: Optional[str],
    year: Optional[int],
    venue: Optional[str],
    source_of_truth: Optional[str],
    raw_patch: bool,
) -> None:
    if doi and (existing.doi or None) != doi:
        stats.changed_doi += 1
    if title and (existing.title or "") != title:
        stats.changed_title += 1
    if year is not None and existing.year != year:
        stats.changed_year += 1
    if venue and (existing.venue or "") != venue:
        stats.changed_venue += 1
    if source_of_truth and (existing.source_of_truth or "") != source_of_truth:
        stats.changed_sot += 1
    if raw_patch:
        stats.changed_raw += 1


def _note_author_added(
    stats: BackfillStats,
    *,
    person_id: int,
    author_name: str,
    pub_id: int,
) -> None:
    entry = stats.authors_added.get(person_id)
    if not entry:
        entry = {"name": author_name, "pub_ids": set()}
        stats.authors_added[person_id] = entry
    entry["pub_ids"].add(pub_id)


def _lookup_existing_publication(conn, doi: str) -> Optional[Dict[str, Any]]:
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
    if not row:
        return None
    try:
        return dict(row)
    except Exception:
        return {
            "pub_id": row[0],
            "doi": row[1],
            "title": row[2],
            "year": row[3],
            "venue": row[4],
            "source_of_truth": row[5],
            "raw_json": row[6],
        }


def _debug_dump(
    label: str, payload: Any, *, max_chars: int = 4000, level: int = logging.DEBUG
) -> None:
    try:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    except Exception:
        text = str(payload)
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    LOGGER.log(level, "%s:\n%s", label, text)
