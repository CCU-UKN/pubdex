#!/usr/bin/env python3
"""
people_pubs.sync.rebuild_authorships

Rebuild biblio.authorships from stored publications.raw_json (DB-only).

Role:
- Restores authorship rows after accidental delete/truncate.
- Uses existing raw_json from Crossref/OpenAlex/DataCite/Semantic Scholar/DBLP (no external API calls).
- Optionally links internal people using name/ORCID matching.

Typical usage:
  # Rebuild everything from stored raw_json
  python -m people_pubs.sync.rebuild_authorships --debug

  # Rebuild a subset from a CSV of pub_ids
  python -m people_pubs.sync.rebuild_authorships --only-pub-id refresh.csv --debug

  # Replace existing authorships for each pub_id
  python -m people_pubs.sync.rebuild_authorships --replace-existing --debug

  # Rebuild while ignoring OpenAlex author data
  python -m people_pubs.sync.rebuild_authorships \
    --only-pub-id 834,2803 \
    --replace-existing \
    --exclude-source openalex \
    --debug

Notes:
- Manual attachments are only restored if the names/ORCIDs still match.
- If a publication has no author data in raw_json, it will be skipped.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg
from psycopg.rows import dict_row

from people_pubs.config import DEFAULT_DSN, source_rank
from people_pubs.db.authorships import compute_order_tag, infer_equal_contrib_tag, upsert_authorship
from people_pubs.db.connection import db_conn
from people_pubs.db.people import (
    lookup_internal_person_for_author,
    lookup_orcid_for_person_id,
    name_similarity_score,
    upsert_person_name_alias,
)
from people_pubs.db.publications import normalize_raw_json
from people_pubs.services.datacite_client import iter_authors_from_datacite
from people_pubs.services.semantic_scholar_client import iter_authors_from_semantic
from people_pubs.utils.identifiers import normalize_orcid
from people_pubs.sync.scaffold import _parse_pub_id_list as _scaffold_parse_pub_id_list, _strip_orcid


def _parse_pub_id_list(value):
    # Historical variant: always reads CSV column 0 (no pub_id header-column
    # detection).
    return _scaffold_parse_pub_id_list(value, detect_header_column=False)


LOGGER = logging.getLogger("people_pubs.rebuild_authorships")

OPENALEX_RAW_DISPLAY_MIN_SCORE = 0.8


@dataclass
class AuthorCandidate:
    name: str
    orcid: Optional[str]
    affiliations: List[str]
    is_corresponding: bool
    raw: Any


@dataclass
class RebuildStats:
    publications: int = 0
    authorships_upserted: int = 0
    authors_matched: int = 0
    publications_no_raw: int = 0
    publications_no_authors: int = 0
    sources_used: Dict[str, int] = field(default_factory=dict)
    errors: int = 0


def _iter_authors_from_crossref(cr_msg: Dict[str, Any]) -> Iterable[AuthorCandidate]:
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
        yield AuthorCandidate(
            name=name,
            orcid=_strip_orcid(a.get("ORCID")),
            affiliations=affs,
            is_corresponding=False,
            raw=a,
        )


def _iter_authors_from_openalex(oa: Dict[str, Any]) -> Iterable[AuthorCandidate]:
    for auth in oa.get("authorships") or []:
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
        yield AuthorCandidate(
            name=name,
            orcid=orcid,
            affiliations=affs,
            is_corresponding=is_corr,
            raw=auth,
        )


def _iter_authors_from_dblp(dblp: Dict[str, Any]) -> Iterable[AuthorCandidate]:
    authors = (dblp.get("authors") or {}).get("author")
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
        yield AuthorCandidate(
            name=str(name).strip(),
            orcid=None,
            affiliations=[],
            is_corresponding=False,
            raw=a,
        )


def _iter_authors_from_datacite(dc: Dict[str, Any]) -> Iterable[AuthorCandidate]:
    for name, orcid, affs, is_corr in iter_authors_from_datacite(dc):
        if not name:
            continue
        yield AuthorCandidate(
            name=name,
            orcid=_strip_orcid(orcid),
            affiliations=affs,
            is_corresponding=is_corr,
            raw={"author": {"full_name": name, "orcid": orcid, "affiliations": affs}, "source": "datacite"},
        )


def _iter_authors_from_semantic(ss: Dict[str, Any]) -> Iterable[AuthorCandidate]:
    for name, orcid, affs, is_corr in iter_authors_from_semantic(ss):
        if not name:
            continue
        yield AuthorCandidate(
            name=name,
            orcid=_strip_orcid(orcid),
            affiliations=affs,
            is_corresponding=is_corr,
            raw={"author": {"full_name": name, "orcid": orcid, "affiliations": affs}, "source": "semantic"},
        )


def _source_candidates(
    raw_json: Dict[str, Any],
    *,
    exclude_sources: Optional[Sequence[str]] = None,
) -> Dict[str, List[AuthorCandidate]]:
    out: Dict[str, List[AuthorCandidate]] = {}
    excluded = {str(s).strip().lower() for s in (exclude_sources or []) if str(s).strip()}
    cr = raw_json.get("crossref")
    if isinstance(cr, dict) and "crossref" not in excluded:
        out["crossref"] = list(_iter_authors_from_crossref(cr))
    oa = raw_json.get("openalex")
    if isinstance(oa, dict) and "openalex" not in excluded:
        out["openalex"] = list(_iter_authors_from_openalex(oa))
    dc = raw_json.get("datacite")
    if isinstance(dc, dict) and "datacite" not in excluded:
        out["datacite"] = list(_iter_authors_from_datacite(dc))
    ss = raw_json.get("semantic")
    if isinstance(ss, dict) and "semantic" not in excluded:
        out["semantic"] = list(_iter_authors_from_semantic(ss))
    dblp = raw_json.get("dblp")
    if isinstance(dblp, dict) and "dblp" not in excluded:
        out["dblp"] = list(_iter_authors_from_dblp(dblp))
    return out


def _choose_best_source(
    candidates: Dict[str, List[AuthorCandidate]],
    *,
    require_affiliations: bool,
    exclude_sources: Optional[Sequence[str]] = None,
) -> Tuple[Optional[str], List[AuthorCandidate]]:
    if not candidates:
        return None, []
    excluded = {str(s).strip().lower() for s in (exclude_sources or []) if str(s).strip()}
    scored: List[Tuple[int, int, int, str, List[AuthorCandidate]]] = []
    for source, authors in candidates.items():
        if source.lower() in excluded:
            continue
        if not authors:
            continue
        if require_affiliations:
            authors = [a for a in authors if a.affiliations]
            if not authors:
                continue
        aff_count = sum(1 for a in authors if a.affiliations)
        scored.append((source_rank(source), -aff_count, -len(authors), source, authors))
    if not scored:
        return None, []
    scored.sort()
    _, _, _, source, authors = scored[0]
    return source, authors


def _load_publication(
    conn: psycopg.Connection,
    pub_id: int,
) -> Optional[Dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT pub_id, source_of_truth, raw_json
            FROM biblio.publications
            WHERE pub_id = %s
            """,
            (pub_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _iter_publications(
    conn: psycopg.Connection,
    *,
    only_pub_ids: Optional[List[int]],
    max_n: int,
) -> Iterable[Dict[str, Any]]:
    if only_pub_ids:
        for pid in only_pub_ids:
            row = _load_publication(conn, int(pid))
            if row:
                yield row
        return

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT pub_id, source_of_truth, raw_json
            FROM biblio.publications
            ORDER BY pub_id
            LIMIT %s
            """,
            (max_n,),
        )
        for row in cur.fetchall():
            yield dict(row)


def run_rebuild_authorships(
    *,
    dsn: Optional[str],
    only_pub_ids: Optional[List[int]],
    max_n: int,
    require_affiliations: bool,
    replace_existing: bool,
    write_aliases: bool,
    exclude_sources: Optional[Sequence[str]],
    dry_run: bool,
) -> RebuildStats:
    stats = RebuildStats()
    with db_conn(dsn) as conn:
        for row in _iter_publications(conn, only_pub_ids=only_pub_ids, max_n=max_n):
            stats.publications += 1
            try:
                pub_id = int(row["pub_id"])
                raw_json = row.get("raw_json")
                if not raw_json:
                    stats.publications_no_raw += 1
                    continue

                merged = normalize_raw_json(raw_json, row.get("source_of_truth") or "")
                if not merged:
                    stats.publications_no_raw += 1
                    continue

                candidates = _source_candidates(
                    merged,
                    exclude_sources=exclude_sources,
                )
                source, authors = _choose_best_source(
                    candidates,
                    require_affiliations=require_affiliations,
                    exclude_sources=exclude_sources,
                )
                if not source or not authors:
                    stats.publications_no_authors += 1
                    continue

                if replace_existing and not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM biblio.authorships WHERE pub_id = %s",
                            (pub_id,),
                        )

                stats.sources_used[source] = stats.sources_used.get(source, 0) + 1
                n_authors = len(authors)
                for pos, author in enumerate(authors, start=1):
                    paper_orcid = author.orcid
                    resolved_author_orcid = paper_orcid
                    internal_id = lookup_internal_person_for_author(
                        conn, author.name, paper_orcid
                    )
                    if internal_id:
                        stats.authors_matched += 1
                        if not resolved_author_orcid:
                            resolved_author_orcid = lookup_orcid_for_person_id(conn, internal_id)
                        if write_aliases:
                            upsert_person_name_alias(
                                conn,
                                int(internal_id),
                                author.name,
                                source=f"{source}.author_name",
                                dry_run=dry_run,
                            )

                    order_tag = compute_order_tag(pos, n_authors)
                    equal_contrib_tag = infer_equal_contrib_tag(author.raw, order_tag) if source == "crossref" else None

                    if source == "crossref" and isinstance(author.raw, dict):
                        raw_author_json = {"source": source, "author": author.raw}
                    else:
                        raw_author_json = {
                            "source": source,
                            "author": {
                                "full_name": author.name,
                                "orcid": paper_orcid,
                                "affiliations": author.affiliations,
                            },
                        }

                    upsert_authorship(
                        conn=conn,
                        publication_id=pub_id,
                        author_position=pos,
                        author_name=author.name,
                        author_orcid=resolved_author_orcid,
                        person_id=(int(internal_id) if internal_id else None),
                        affiliations=author.affiliations,
                        is_corresponding=author.is_corresponding,
                        order_tag=order_tag,
                        equal_contrib_tag=equal_contrib_tag,
                        raw_author_json=raw_author_json,
                        dry_run=dry_run,
                        paper_orcid=paper_orcid,
                    )
                    stats.authorships_upserted += 1
            except Exception:
                stats.errors += 1
                LOGGER.exception("Failed rebuilding authorships for row=%r", row)
                if not dry_run:
                    conn.rollback()

        if not dry_run:
            conn.commit()

    return stats


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Rebuild biblio.authorships from stored publications.raw_json (DB-only).",
    )
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (defaults to people_pubs.config.DEFAULT_DSN)")
    p.add_argument("--max", type=int, default=1000, help="Max publications to process.")
    p.add_argument(
        "--only-pub-id",
        default=None,
        help="Process one pub_id or a CSV file (single column of pub_ids).",
    )
    p.add_argument(
        "--require-affiliations",
        action="store_true",
        help="Only rebuild authorships when affiliations are present in the source data.",
    )
    p.add_argument(
        "--exclude-source",
        action="append",
        default=None,
        help="Exclude a stored source when choosing author data (repeatable, e.g. --exclude-source openalex).",
    )
    p.add_argument(
        "--replace-existing",
        action="store_true",
        help="Delete existing authorships for a publication before inserting new rows.",
    )
    p.add_argument(
        "--no-aliases",
        dest="write_aliases",
        action="store_false",
        help="Do not add author names to alias table during rebuild.",
    )
    p.add_argument("--dry-run", action="store_true", help="Do not write to DB.")
    p.add_argument("--debug", action="store_true", help="Enable debug logging.")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")
    only_pub_ids = _parse_pub_id_list(args.only_pub_id)
    stats = run_rebuild_authorships(
        dsn=args.dsn,
        only_pub_ids=only_pub_ids,
        max_n=args.max,
        require_affiliations=args.require_affiliations,
        replace_existing=args.replace_existing,
        write_aliases=args.write_aliases,
        exclude_sources=args.exclude_source,
        dry_run=args.dry_run,
    )
    LOGGER.info(
        "rebuild_authorships: pubs=%s upserted=%s matched=%s no_raw=%s no_authors=%s errors=%s",
        stats.publications,
        stats.authorships_upserted,
        stats.authors_matched,
        stats.publications_no_raw,
        stats.publications_no_authors,
        stats.errors,
    )
    if stats.sources_used:
        used = ", ".join(f"{k}={v}" for k, v in sorted(stats.sources_used.items()))
        LOGGER.info("rebuild_authorships sources_used: %s", used)


if __name__ == "__main__":
    main()
