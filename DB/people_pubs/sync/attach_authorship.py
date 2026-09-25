"""
people_pubs.sync.attach_authorship

Manually attach an authorship row to a person_id.
Updates biblio.authorships and writes a name alias for the person.

The script computes a name similarity score against the person display_name.
If the score is below --min-score (default 0.80) it will refuse to attach
unless --force is provided.

Examples:
  python -m people_pubs.sync.attach_authorship --pub-id 9507 --author-position 2 --person-id 16 --debug
  python -m people_pubs.sync.attach_authorship --pub-id 9507 --author-name "Shauhin E. Alavi" --person-id 16
  python -m people_pubs.sync.attach_authorship --csv attach.csv --debug
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import psycopg
from psycopg.rows import dict_row

from people_pubs.db.connection import db_conn
from people_pubs.db.people import person_is_internal, upsert_person_name_alias
from people_pubs.orcidkit import ascii_fold, strip_titles
from people_pubs.utils.identifiers import normalize_orcid


@dataclass
class AttachStats:
    processed: int = 0
    updated: int = 0
    skipped_existing: int = 0
    skipped_conflict: int = 0
    skipped_low_score: int = 0
    skipped_missing_person: int = 0
    skipped_bad_row: int = 0
    errors: int = 0


_UNICODE_DASH_RE = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u00ad]")


def _load_authorship_columns(conn: psycopg.Connection) -> Set[str]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'biblio' AND table_name = 'authorships'
            """
        )
        rows = cur.fetchall()
    return {r["column_name"] for r in rows}


def _table_exists(conn: psycopg.Connection, qualified_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified_name,))
        row = cur.fetchone()
    if not row:
        return False
    try:
        return bool(row[0])
    except Exception:
        pass
    if isinstance(row, dict):
        for v in row.values():
            return bool(v)
    return False


def _jsonb(value: Optional[Dict[str, Any]]) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

def _normalize_name(name: str) -> str:
    s = strip_titles(name or "")
    s = ascii_fold(s)
    s = _UNICODE_DASH_RE.sub("-", s)
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _name_variants(name: str) -> List[str]:
    raw = (name or "").strip()
    if not raw:
        return []
    out: List[str] = []
    seen: set[str] = set()

    def add(val: str) -> None:
        val = (val or "").strip()
        if not val:
            return
        key = val.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(val)

    add(raw)
    add(_normalize_name(raw))
    if "," in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if len(parts) >= 2:
            swapped = f"{parts[1]} {parts[0]}".strip()
            add(swapped)
            add(_normalize_name(swapped))

    return out


def _best_name_similarity(author_name: str, person_name: str) -> float:
    best = 0.0
    for a in _name_variants(author_name):
        for b in _name_variants(person_name):
            if not a or not b:
                continue
            score = difflib.SequenceMatcher(None, _normalize_name(a), _normalize_name(b)).ratio()
            if score > best:
                best = score
    return best


def _load_person_identity(conn: psycopg.Connection, person_id: int) -> Tuple[Optional[str], Optional[str]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT p.display_name, io.orcid
            FROM app.people p
            LEFT JOIN app.identities_orcid io ON io.person_id = p.person_id
            WHERE p.person_id = %s
            """,
            (person_id,),
        )
        row = cur.fetchone()
    if not row:
        return None, None
    return row.get("display_name"), row.get("orcid")


def _normalize_orcid(orcid: Optional[str]) -> Optional[str]:
    return normalize_orcid(orcid)


def _lookup_person_id_by_orcid(
    conn: psycopg.Connection, paper_orcid: str
) -> Tuple[Optional[int], bool]:
    norm = _normalize_orcid(paper_orcid)
    if not norm:
        return None, False
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT DISTINCT io.person_id
            FROM app.identities_orcid io
            WHERE lower(io.orcid) = lower(%s)
               OR lower(io.orcid) = lower('https://orcid.org/' || %s)
               OR lower(io.orcid) = lower('http://orcid.org/' || %s)
            ORDER BY io.person_id
            """,
            (norm, norm, norm),
        )
        rows = cur.fetchall()
    person_ids = [int(r["person_id"]) for r in rows]
    if not person_ids:
        return None, False
    if len(person_ids) > 1:
        return None, True
    return person_ids[0], False


def _fetch_authorship_row(
    conn: psycopg.Connection,
    *,
    pub_id: int,
    author_position: Optional[int],
    author_name: Optional[str],
    cols: Set[str],
) -> Optional[Dict[str, Any]]:
    select_parts = ["ctid", "pub_id", "person_id"]
    select_parts.append(
        "author_position" if "author_position" in cols else "NULL::int AS author_position"
    )
    select_parts.append(
        "author_name" if "author_name" in cols else "NULL::text AS author_name"
    )
    select_parts.append(
        "display_name_at_pub" if "display_name_at_pub" in cols else "NULL::text AS display_name_at_pub"
    )
    select_parts.append(
        "paper_orcid" if "paper_orcid" in cols else "NULL::text AS paper_orcid"
    )
    select_parts.append(
        "author_orcid" if "author_orcid" in cols else "NULL::text AS author_orcid"
    )

    where = ["pub_id = %(pub_id)s"]
    params: Dict[str, Any] = {"pub_id": pub_id}

    if author_position is not None:
        where.append("author_position = %(author_position)s")
        params["author_position"] = author_position
    elif author_name:
        where.append(
            "(lower(author_name) = lower(%(author_name)s) OR "
            "lower(display_name_at_pub) = lower(%(author_name)s))"
        )
        params["author_name"] = author_name
    else:
        raise ValueError("Provide --author-position or --author-name to select the row.")

    sql = f"""
    SELECT {", ".join(select_parts)}
    FROM biblio.authorships
    WHERE {" AND ".join(where)}
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    if not rows:
        return None
    if len(rows) > 1:
        raise ValueError("Multiple authorships matched; provide --author-position.")
    return dict(rows[0])


def _authorship_person_exists(
    conn: psycopg.Connection, pub_id: int, person_id: int
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM biblio.authorships
            WHERE pub_id = %s AND person_id = %s
            """,
            (pub_id, person_id),
        )
        return cur.fetchone() is not None


def _fetch_authorship_state(
    conn: psycopg.Connection,
    *,
    pub_id: int,
    person_id: int,
    cols: Set[str],
) -> Optional[Dict[str, Any]]:
    select_parts = ["pub_id", "person_id"]
    select_parts.append(
        "author_position" if "author_position" in cols else "NULL::int AS author_position"
    )
    select_parts.append(
        "author_name" if "author_name" in cols else "NULL::text AS author_name"
    )
    select_parts.append(
        "display_name_at_pub" if "display_name_at_pub" in cols else "NULL::text AS display_name_at_pub"
    )
    select_parts.append(
        "paper_orcid" if "paper_orcid" in cols else "NULL::text AS paper_orcid"
    )
    select_parts.append(
        "author_orcid" if "author_orcid" in cols else "NULL::text AS author_orcid"
    )

    sql = f"""
    SELECT {", ".join(select_parts)}
    FROM biblio.authorships
    WHERE pub_id = %s AND person_id = %s
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (pub_id, person_id))
        row = cur.fetchone()
    return dict(row) if row else None


def _upsert_authorship_manual_override(
    conn: psycopg.Connection,
    *,
    has_override_table: bool,
    pub_id: int,
    author_position: Optional[int],
    display_name_at_pub: Optional[str],
    paper_orcid: Optional[str],
    person_id: int,
    reason: Optional[str],
    evidence: Optional[Dict[str, Any]],
    source_file: Optional[str],
    source_row: Optional[int],
    applied_by: Optional[str],
    dry_run: bool,
) -> None:
    if not has_override_table:
        return

    name_val = (display_name_at_pub or "").strip() or None
    orcid_val = _normalize_orcid(paper_orcid)
    if author_position is None and not name_val and not orcid_val:
        logging.debug("Skipping authorship_manual_overrides write: no selector fields.")
        return

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT override_id
            FROM biblio.authorship_manual_overrides
            WHERE active = TRUE
              AND mode = 'force_person'
              AND pub_id = %s
              AND person_id = %s
              AND (
                    (author_position IS NULL AND %s IS NULL)
                 OR author_position = %s
              )
              AND lower(COALESCE(display_name_at_pub, '')) = lower(COALESCE(%s, ''))
              AND lower(COALESCE(paper_orcid, '')) = lower(COALESCE(%s, ''))
            ORDER BY override_id DESC
            LIMIT 1
            """,
            (pub_id, person_id, author_position, author_position, name_val, orcid_val),
        )
        row = cur.fetchone()

    if dry_run:
        action = "update" if row else "insert"
        logging.info(
            "  [dry-run] would %s biblio.authorship_manual_overrides for pub_id=%s person_id=%s",
            action,
            pub_id,
            person_id,
        )
        return

    if row:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE biblio.authorship_manual_overrides
                SET lock_row = TRUE,
                    active = TRUE,
                    reason = %s,
                    evidence = %s::jsonb,
                    source_file = %s,
                    source_row = %s,
                    created_by = COALESCE(%s, created_by),
                    updated_at = now()
                WHERE override_id = %s
                """,
                (
                    reason,
                    _jsonb(evidence or {}),
                    source_file,
                    source_row,
                    applied_by,
                    int(row["override_id"]),
                ),
            )
    else:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biblio.authorship_manual_overrides
                  (pub_id, author_position, display_name_at_pub, paper_orcid, mode, person_id,
                   lock_row, active, reason, evidence, source_file, source_row, created_by)
                VALUES
                  (%s, %s, %s, %s, 'force_person', %s,
                   TRUE, TRUE, %s, %s::jsonb, %s, %s, %s)
                """,
                (
                    pub_id,
                    author_position,
                    name_val,
                    orcid_val,
                    person_id,
                    reason,
                    _jsonb(evidence or {}),
                    source_file,
                    source_row,
                    applied_by,
                ),
            )


def _log_manual_correction(
    conn: psycopg.Connection,
    *,
    has_log_table: bool,
    correction_type: str,
    target: Dict[str, Any],
    before_state: Optional[Dict[str, Any]],
    after_state: Optional[Dict[str, Any]],
    applied_by: Optional[str],
    note: Optional[str],
    dry_run: bool,
) -> None:
    if not has_log_table:
        return
    if dry_run:
        logging.info(
            "  [dry-run] would log correction type=%s target=%s",
            correction_type,
            target,
        )
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO biblio.manual_corrections_log
              (correction_type, target, before_state, after_state, applied_by, note)
            VALUES
              (%s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)
            """,
            (
                correction_type,
                _jsonb(target),
                _jsonb(before_state),
                _jsonb(after_state),
                applied_by,
                note,
            ),
        )


def _update_authorship_person_id(
    conn: psycopg.Connection,
    *,
    ctid: str,
    person_id: int,
    has_internal_flag: bool,
    has_updated_at: bool,
    dry_run: bool,
) -> None:
    if dry_run:
        logging.info("  [dry-run] would set person_id=%s for authorship ctid=%s", person_id, ctid)
        return

    set_parts = ["person_id = %s"]
    params: List[Any] = [person_id]

    if has_internal_flag:
        # Attaching an author records who they are, not that they are ours.
        set_parts.append("is_internal_at_ingest = %s")
        params.append(person_is_internal(conn, person_id))

    if has_updated_at:
        set_parts.append("updated_at = now()")

    params.append(ctid)

    sql = f"""
    UPDATE biblio.authorships
    SET {", ".join(set_parts)}
    WHERE ctid = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _attach_authorship_with_conn(
    conn: psycopg.Connection,
    *,
    pub_id: int,
    author_position: Optional[int],
    author_name: Optional[str],
    person_id: int,
    overwrite: bool,
    alias_source: str,
    min_score: float,
    force: bool,
    dry_run: bool,
    cols: Set[str],
    has_internal_flag: bool,
    has_updated_at: bool,
    has_manual_override_table: bool,
    has_manual_log_table: bool,
    source_file: Optional[str],
    source_row: Optional[int],
    applied_by: Optional[str],
) -> AttachStats:
    stats = AttachStats(processed=1)
    row = _fetch_authorship_row(
        conn,
        pub_id=pub_id,
        author_position=author_position,
        author_name=author_name,
        cols=cols,
    )
    if not row:
        raise ValueError("No authorship row matched the selection.")
    before_state = {k: v for k, v in row.items() if k != "ctid"}

    person_name, person_orcid = _load_person_identity(conn, person_id)
    if not person_name:
        raise ValueError(f"No app.people row found for person_id={person_id}.")

    author_label = author_name or row.get("author_name") or row.get("display_name_at_pub") or ""
    author_label = author_label.strip()
    paper_orcid = (row.get("paper_orcid") or row.get("author_orcid") or "").strip() or None
    orcid_match = bool(person_orcid and paper_orcid and person_orcid == paper_orcid)
    name_score = _best_name_similarity(author_label, person_name) if author_label else 0.0
    score = 1.0 if orcid_match else name_score

    logging.info(
        "match score=%.3f (orcid_match=%s) author_name=%r person_display_name=%r",
        score,
        orcid_match,
        author_label,
        person_name,
    )

    if score < min_score and not force:
        stats.skipped_low_score += 1
        logging.warning(
            "Score %.3f below min_score=%.2f; use --force to attach anyway.",
            score,
            min_score,
        )
        return stats

    existing_person = row.get("person_id")
    if existing_person:
        if int(existing_person) == person_id:
            stats.skipped_existing += 1
            logging.info("Authorship already linked to person_id=%s", person_id)
        elif not overwrite:
            stats.skipped_existing += 1
            logging.info(
                "Authorship already linked to person_id=%s (use --overwrite to replace).",
                existing_person,
            )
            return stats

    if _authorship_person_exists(conn, pub_id, person_id):
        stats.skipped_conflict += 1
        logging.warning(
            "Pub %s already has person_id=%s attached; refusing to overwrite.",
            pub_id,
            person_id,
        )
        return stats

    _update_authorship_person_id(
        conn,
        ctid=row["ctid"],
        person_id=person_id,
        has_internal_flag=has_internal_flag,
        has_updated_at=has_updated_at,
        dry_run=dry_run,
    )

    alias_name = (
        author_name
        or row.get("author_name")
        or row.get("display_name_at_pub")
        or ""
    ).strip()
    if alias_name:
        upsert_person_name_alias(
            conn,
            person_id,
            alias_name,
            source=alias_source,
            dry_run=dry_run,
        )

    reason = (
        f"manual attach via attach_authorship; score={score:.3f}; "
        f"force={bool(force)}; overwrite={bool(overwrite)}"
    )
    evidence = {
        "match_score": round(score, 6),
        "orcid_match": orcid_match,
        "author_label": author_label or None,
        "person_display_name": person_name,
        "alias_source": alias_source,
    }
    selector_name = (
        row.get("display_name_at_pub")
        or row.get("author_name")
        or author_name
        or None
    )
    selector_orcid = row.get("paper_orcid") or row.get("author_orcid") or None
    _upsert_authorship_manual_override(
        conn,
        has_override_table=has_manual_override_table,
        pub_id=pub_id,
        author_position=row.get("author_position"),
        display_name_at_pub=selector_name,
        paper_orcid=selector_orcid,
        person_id=person_id,
        reason=reason,
        evidence=evidence,
        source_file=source_file,
        source_row=source_row,
        applied_by=applied_by,
        dry_run=dry_run,
    )
    after_state = _fetch_authorship_state(
        conn,
        pub_id=pub_id,
        person_id=person_id,
        cols=cols,
    )
    _log_manual_correction(
        conn,
        has_log_table=has_manual_log_table,
        correction_type="authorship.attach_person",
        target={
            "pub_id": pub_id,
            "person_id": person_id,
            "author_position": row.get("author_position"),
            "display_name_at_pub": selector_name,
            "paper_orcid": _normalize_orcid(selector_orcid),
        },
        before_state=before_state,
        after_state=after_state,
        applied_by=applied_by,
        note=reason,
        dry_run=dry_run,
    )

    stats.updated += 1
    return stats


def _add_stats(total: AttachStats, part: AttachStats) -> None:
    total.processed += part.processed
    total.updated += part.updated
    total.skipped_existing += part.skipped_existing
    total.skipped_conflict += part.skipped_conflict
    total.skipped_low_score += part.skipped_low_score
    total.skipped_missing_person += part.skipped_missing_person
    total.skipped_bad_row += part.skipped_bad_row
    total.errors += part.errors


def run_attach_authorship(
    *,
    dsn: Optional[str],
    pub_id: int,
    author_position: Optional[int],
    author_name: Optional[str],
    person_id: int,
    overwrite: bool,
    alias_source: str,
    min_score: float,
    force: bool,
    applied_by: Optional[str],
    dry_run: bool,
) -> AttachStats:
    stats = AttachStats()
    with db_conn(dsn) as conn:
        cols = _load_authorship_columns(conn)
        has_internal_flag = "is_internal_at_ingest" in cols
        has_updated_at = "updated_at" in cols
        has_manual_override_table = _table_exists(conn, "biblio.authorship_manual_overrides")
        has_manual_log_table = _table_exists(conn, "biblio.manual_corrections_log")
        if not has_manual_override_table:
            logging.debug("biblio.authorship_manual_overrides not found; skipping override writes.")
        if not has_manual_log_table:
            logging.debug("biblio.manual_corrections_log not found; skipping correction logs.")
        try:
            stats = _attach_authorship_with_conn(
                conn,
                pub_id=pub_id,
                author_position=author_position,
                author_name=author_name,
                person_id=person_id,
                overwrite=overwrite,
                alias_source=alias_source,
                min_score=min_score,
                force=force,
                dry_run=dry_run,
                cols=cols,
                has_internal_flag=has_internal_flag,
                has_updated_at=has_updated_at,
                has_manual_override_table=has_manual_override_table,
                has_manual_log_table=has_manual_log_table,
                source_file=None,
                source_row=None,
                applied_by=applied_by,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if not dry_run and stats.updated:
            conn.commit()
    return stats


def run_attach_authorship_csv(
    *,
    dsn: Optional[str],
    csv_path: str,
    delimiter: str,
    overwrite: bool,
    alias_source: str,
    min_score: float,
    force: bool,
    applied_by: Optional[str],
    dry_run: bool,
) -> AttachStats:
    stats = AttachStats()
    path = Path(csv_path)
    if not path.exists():
        raise SystemExit(f"CSV file not found: {path}")

    with db_conn(dsn) as conn:
        cols = _load_authorship_columns(conn)
        has_internal_flag = "is_internal_at_ingest" in cols
        has_updated_at = "updated_at" in cols
        has_manual_override_table = _table_exists(conn, "biblio.authorship_manual_overrides")
        has_manual_log_table = _table_exists(conn, "biblio.manual_corrections_log")
        if not has_manual_override_table:
            logging.debug("biblio.authorship_manual_overrides not found; skipping override writes.")
        if not has_manual_log_table:
            logging.debug("biblio.manual_corrections_log not found; skipping correction logs.")

        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            if not reader.fieldnames:
                raise SystemExit("CSV has no header row.")
            required = {"pub_id", "paper_orcid"}
            missing = sorted(required - set(reader.fieldnames))
            if missing:
                raise SystemExit(f"CSV missing required columns: {', '.join(missing)}")

            for line_no, raw in enumerate(reader, start=2):
                stats.processed += 1
                pub_id_raw = (raw.get("pub_id") or "").strip()
                paper_orcid_raw = (raw.get("paper_orcid") or "").strip()
                author_pos_raw = (raw.get("author_position") or "").strip()
                display_name_at_pub = (raw.get("display_name_at_pub") or "").strip()

                if not pub_id_raw or not paper_orcid_raw:
                    stats.skipped_bad_row += 1
                    logging.warning(
                        "line %s: missing pub_id or paper_orcid; skipping row",
                        line_no,
                    )
                    continue

                try:
                    pub_id = int(pub_id_raw)
                except ValueError:
                    stats.skipped_bad_row += 1
                    logging.warning(
                        "line %s: invalid pub_id=%r; skipping row",
                        line_no,
                        pub_id_raw,
                    )
                    continue

                author_position: Optional[int] = None
                if author_pos_raw:
                    try:
                        author_position = int(author_pos_raw)
                    except ValueError:
                        stats.skipped_bad_row += 1
                        logging.warning(
                            "line %s: invalid author_position=%r; skipping row",
                            line_no,
                            author_pos_raw,
                        )
                        continue

                if author_position is None and not display_name_at_pub:
                    stats.skipped_bad_row += 1
                    logging.warning(
                        "line %s: provide author_position or display_name_at_pub; skipping row",
                        line_no,
                    )
                    continue

                person_id, ambiguous = _lookup_person_id_by_orcid(conn, paper_orcid_raw)
                if ambiguous:
                    stats.skipped_conflict += 1
                    logging.warning(
                        "line %s: ORCID %r maps to multiple people; skipping row",
                        line_no,
                        paper_orcid_raw,
                    )
                    continue
                if person_id is None:
                    stats.skipped_missing_person += 1
                    logging.warning(
                        "line %s: ORCID %r not found in app.identities_orcid; skipping row",
                        line_no,
                        paper_orcid_raw,
                    )
                    continue

                logging.info(
                    "line %s: pub_id=%s person_id=%s author_position=%s display_name_at_pub=%r",
                    line_no,
                    pub_id,
                    person_id,
                    author_position,
                    display_name_at_pub or None,
                )

                try:
                    one = _attach_authorship_with_conn(
                        conn,
                        pub_id=pub_id,
                        author_position=author_position,
                        author_name=(display_name_at_pub or None),
                        person_id=person_id,
                        overwrite=overwrite,
                        alias_source=alias_source,
                        min_score=min_score,
                        force=force,
                        dry_run=dry_run,
                        cols=cols,
                        has_internal_flag=has_internal_flag,
                        has_updated_at=has_updated_at,
                        has_manual_override_table=has_manual_override_table,
                        has_manual_log_table=has_manual_log_table,
                        source_file=str(path),
                        source_row=line_no,
                        applied_by=applied_by,
                    )
                    _add_stats(stats, one)
                    if not dry_run and one.updated:
                        conn.commit()
                except Exception as exc:
                    stats.errors += 1
                    logging.error("line %s: failed to attach authorship: %s", line_no, exc)
                    if not dry_run:
                        conn.rollback()

    return stats


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Attach a biblio.authorships row to a person_id.",
    )
    p.add_argument(
        "--dsn",
        default=None,
        help=(
            "Optional PostgreSQL DSN (otherwise use PEOPLE_DB_DSN or PG* env vars). "
            "Example: postgresql://postgres:<password>@127.0.0.1:5432/people_db"
        ),
    )
    p.add_argument("--pub-id", type=int, required=False, help="Publication ID to update (single mode).")
    p.add_argument("--person-id", type=int, required=False, help="Person ID to attach (single mode).")
    p.add_argument(
        "--csv",
        default=None,
        help=(
            "CSV mode: ingest rows with columns paper_orcid,pub_id,author_position,display_name_at_pub. "
            "Person is resolved by existing ORCID; rows with unknown ORCID are skipped."
        ),
    )
    p.add_argument(
        "--delimiter",
        default=",",
        help="CSV delimiter for --csv mode (default: ',').",
    )
    p.add_argument(
        "--author-position",
        type=int,
        default=None,
        help="Author position to select the row (recommended).",
    )
    p.add_argument(
        "--author-name",
        default=None,
        help="Author name to select the row (used only if --author-position is not set).",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing person_id on the authorship row.",
    )
    p.add_argument(
        "--alias-source",
        default="authorships.manual_attach",
        help="Alias source tag written into app.person_name_aliases_manual.",
    )
    p.add_argument(
        "--applied-by",
        default=os.getenv("USER"),
        help="Provenance user for biblio.authorship_manual_overrides/manual_corrections_log (default: $USER).",
    )
    p.add_argument(
        "--min-score",
        type=float,
        default=0.80,
        help="Minimum name similarity score required to attach (default: 0.80).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Attach even when the similarity score is below --min-score.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write anything to the DB; just log what would happen.",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging.",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
    )

    if args.csv:
        if args.pub_id is not None or args.person_id is not None:
            raise SystemExit("Use either --csv mode OR single mode args (--pub-id/--person-id), not both.")
        stats = run_attach_authorship_csv(
            dsn=args.dsn,
            csv_path=args.csv,
            delimiter=args.delimiter,
            overwrite=args.overwrite,
            alias_source=args.alias_source,
            min_score=args.min_score,
            force=args.force,
            applied_by=args.applied_by,
            dry_run=args.dry_run,
        )
    else:
        if args.pub_id is None or args.person_id is None:
            raise SystemExit("Single mode requires --pub-id and --person-id.")
        stats = run_attach_authorship(
            dsn=args.dsn,
            pub_id=args.pub_id,
            author_position=args.author_position,
            author_name=args.author_name,
            person_id=args.person_id,
            overwrite=args.overwrite,
            alias_source=args.alias_source,
            min_score=args.min_score,
            force=args.force,
            applied_by=args.applied_by,
            dry_run=args.dry_run,
        )

    logging.info(
        "attach_authorship: processed=%s updated=%s skipped_existing=%s skipped_conflict=%s skipped_low_score=%s skipped_missing_person=%s skipped_bad_row=%s errors=%s",
        stats.processed,
        stats.updated,
        stats.skipped_existing,
        stats.skipped_conflict,
        stats.skipped_low_score,
        stats.skipped_missing_person,
        stats.skipped_bad_row,
        stats.errors,
    )


if __name__ == "__main__":
    main()
