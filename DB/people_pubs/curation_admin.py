"""Helpers for the curation editor surface.

These functions keep curator-driven edits narrow, auditable, and aligned with
the existing DB curation model. They are intentionally designed around explicit
capabilities and queue records so a future login/group layer can reuse the same
operations without changing the underlying write paths.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

import psycopg
from psycopg.rows import dict_row

from people_pubs.db.people import upsert_person_name_alias
from people_pubs.sync.attach_authorship import (
    AttachStats,
    _attach_authorship_with_conn,
    _fetch_authorship_row,
    _load_authorship_columns,
    _log_manual_correction as _auth_log_manual_correction,
    _normalize_orcid as _normalize_authorship_orcid,
    _table_exists as _auth_table_exists,
)
from people_pubs.sync.publication_dedup_overrides import (
    _active_decisions_for_snapshot,
    _fetch_override_state,
    _insert_durable_decision,
    _log_manual_correction as _dedup_log_manual_correction,
    _pub_exists,
    _pub_snapshot,
    _set_keep_separate,
    _set_merge_one,
    _table_exists as _dedup_table_exists,
)


LOG = logging.getLogger("people_pubs.curation_admin")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class QueueActor:
    name: str
    role: str


def _jsonb(value: Optional[Dict[str, Any]]) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _row(
    conn: psycopg.Connection,
    sql: str,
    params: Sequence[Any] | None = None,
) -> Optional[Dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params or ())
        rec = cur.fetchone()
    return dict(rec) if rec else None


def _rows(
    conn: psycopg.Connection,
    sql: str,
    params: Sequence[Any] | None = None,
) -> List[Dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params or ())
        return [dict(row) for row in cur.fetchall()]


def _view_exists(conn: psycopg.Connection, qualified_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified_name,))
        row = cur.fetchone()
    if not row:
        return False
    return bool(row[0] if not isinstance(row, dict) else next(iter(row.values())))


def _insert_queue_entry(
    conn: psycopg.Connection,
    *,
    review_type: str,
    subject_type: str,
    actor: QueueActor,
    subject_pub_id: Optional[int] = None,
    subject_person_id: Optional[int] = None,
    status: str = "applied",
    note: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    resolution: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    if not _view_exists(conn, "biblio.curation_review_queue"):
        return None

    approved_by = actor.name if status in {"approved", "applied"} else None
    approved_role = actor.role if status in {"approved", "applied"} else None
    approved_at_sql = "NOW()" if status in {"approved", "applied"} else "NULL"
    applied_at_sql = "NOW()" if status == "applied" else "NULL"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            INSERT INTO biblio.curation_review_queue (
              review_type,
              subject_type,
              subject_pub_id,
              subject_person_id,
              status,
              requested_by,
              requested_role,
              approved_by,
              approved_role,
              requested_at,
              approved_at,
              applied_at,
              note,
              payload,
              resolution
            )
            VALUES (
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s,
              NOW(), {approved_at_sql}, {applied_at_sql},
              %s, %s::jsonb, %s::jsonb
            )
            RETURNING queue_id
            """,
            (
                review_type,
                subject_type,
                subject_pub_id,
                subject_person_id,
                status,
                actor.name,
                actor.role,
                approved_by,
                approved_role,
                note,
                _jsonb(payload or {}),
                _jsonb(resolution or {}),
            ),
        )
        row = cur.fetchone()
    return int(row["queue_id"]) if row else None


def _fetch_alias_row(
    conn: psycopg.Connection,
    *,
    person_id: int,
    alias_name: str,
) -> Optional[Dict[str, Any]]:
    return _row(
        conn,
        """
        SELECT person_id, alias_name, alias_sources
        FROM app.person_name_aliases_manual
        WHERE person_id = %s
          AND lower(alias_name) = lower(%s)
        """,
        (person_id, alias_name),
    )


def _normalize_emails(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for raw in values:
        email = str(raw or "").strip()
        if not email:
            continue
        key = email.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(email)
    return out


def _fetch_people_pii_row(
    conn: psycopg.Connection,
    *,
    person_id: int,
) -> Optional[Dict[str, Any]]:
    if not _auth_table_exists(conn, "pii.people_pii"):
        return None
    return _row(
        conn,
        """
        SELECT person_id, primary_email, emails
        FROM pii.people_pii
        WHERE person_id = %s
        """,
        (person_id,),
    )


def fetch_person_editor_profile(
    conn: psycopg.Connection,
    *,
    person_id: int,
) -> Dict[str, Any]:
    aliases = _rows(
        conn,
        """
        SELECT alias_name, alias_sources
        FROM app.person_name_aliases_manual
        WHERE person_id = %s
        ORDER BY lower(alias_name), alias_name
        """,
        (person_id,),
    ) if _auth_table_exists(conn, "app.person_name_aliases_manual") else []
    pii_row = _fetch_people_pii_row(conn, person_id=person_id) or {}
    return {
        "aliases": [
            {
                "alias_name": row.get("alias_name"),
                "alias_sources": row.get("alias_sources") or [],
            }
            for row in aliases
        ],
        "primary_email": pii_row.get("primary_email"),
        "emails": _normalize_emails(pii_row.get("emails") or []),
    }


def add_person_alias(
    conn: psycopg.Connection,
    *,
    person_id: int,
    alias_name: str,
    actor: QueueActor,
    source: str = "curation_admin.person_alias",
    note: Optional[str] = None,
) -> Dict[str, Any]:
    before_state = _fetch_alias_row(conn, person_id=person_id, alias_name=alias_name)
    upsert_person_name_alias(conn, person_id, alias_name, source=source, dry_run=False)
    after_state = _fetch_alias_row(conn, person_id=person_id, alias_name=alias_name)
    queue_id = _insert_queue_entry(
        conn,
        review_type="person_alias",
        subject_type="person",
        subject_person_id=person_id,
        actor=actor,
        note=note,
        payload={
            "alias_name": alias_name,
            "source": source,
        },
        resolution={"updated": bool(after_state)},
    )
    if _auth_table_exists(conn, "biblio.manual_corrections_log"):
        _auth_log_manual_correction(
            conn,
            has_log_table=True,
            correction_type="person_alias.add",
            target={"person_id": person_id, "alias_name": alias_name},
            before_state=before_state,
            after_state=after_state,
            applied_by=actor.name,
            note=note or f"alias add via {source}",
            dry_run=False,
        )
    return {
        "person_id": person_id,
        "alias_name": alias_name,
        "updated": bool(after_state),
        "queue_id": queue_id,
    }


def set_person_contact_email(
    conn: psycopg.Connection,
    *,
    person_id: int,
    primary_email: str,
    actor: QueueActor,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    email = str(primary_email or "").strip()
    if not email:
        raise ValueError("Email is required.")
    if not _EMAIL_RE.match(email):
        raise ValueError("Email does not look valid.")
    before_state = _fetch_people_pii_row(conn, person_id=person_id)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT action, primary_email, emails
            FROM pii.ensure_people_pii(%s, %s::citext, %s::citext[], TRUE)
            """,
            (person_id, email, [email]),
        )
        row = cur.fetchone()
    after_state = dict(row) if row else None
    queue_id = _insert_queue_entry(
        conn,
        review_type="person.email",
        subject_type="person",
        subject_person_id=person_id,
        actor=actor,
        note=note,
        payload={"primary_email": email},
        resolution=after_state or {"updated": False},
    )
    if _auth_table_exists(conn, "biblio.manual_corrections_log"):
        _auth_log_manual_correction(
            conn,
            has_log_table=True,
            correction_type="person.email.set",
            target={"person_id": person_id},
            before_state=before_state,
            after_state=after_state,
            applied_by=actor.name,
            note=note or "curation email update",
            dry_run=False,
        )
    return {
        "person_id": person_id,
        "primary_email": (after_state or {}).get("primary_email") or email,
        "emails": _normalize_emails((after_state or {}).get("emails") or [email]),
        "queue_id": queue_id,
    }


def _upsert_force_null_override(
    conn: psycopg.Connection,
    *,
    pub_id: int,
    author_position: Optional[int],
    display_name_at_pub: Optional[str],
    paper_orcid: Optional[str],
    actor: QueueActor,
    reason: Optional[str],
    evidence: Optional[Dict[str, Any]],
) -> None:
    if not _auth_table_exists(conn, "biblio.authorship_manual_overrides"):
        return

    name_val = (display_name_at_pub or "").strip() or None
    orcid_val = _normalize_authorship_orcid(paper_orcid)
    if author_position is None and not name_val and not orcid_val:
        return

    existing = _row(
        conn,
        """
        SELECT override_id
        FROM biblio.authorship_manual_overrides
        WHERE active = TRUE
          AND mode = 'force_null'
          AND pub_id = %s
          AND (
                (author_position IS NULL AND %s IS NULL)
             OR author_position = %s
          )
          AND lower(COALESCE(display_name_at_pub, '')) = lower(COALESCE(%s, ''))
          AND lower(COALESCE(paper_orcid, '')) = lower(COALESCE(%s, ''))
        ORDER BY override_id DESC
        LIMIT 1
        """,
        (pub_id, author_position, author_position, name_val, orcid_val),
    )
    if existing:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE biblio.authorship_manual_overrides
                SET lock_row = TRUE,
                    active = TRUE,
                    person_id = NULL,
                    reason = %s,
                    evidence = %s::jsonb,
                    source_file = 'curation_admin',
                    source_row = NULL,
                    created_by = COALESCE(%s, created_by),
                    updated_at = now()
                WHERE override_id = %s
                """,
                (
                    reason,
                    _jsonb(evidence or {}),
                    actor.name,
                    int(existing["override_id"]),
                ),
            )
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO biblio.authorship_manual_overrides
              (pub_id, author_position, display_name_at_pub, paper_orcid, mode,
               person_id, lock_row, active, reason, evidence, source_file, source_row, created_by)
            VALUES
              (%s, %s, %s, %s, 'force_null',
               NULL, TRUE, TRUE, %s, %s::jsonb, 'curation_admin', NULL, %s)
            """,
            (
                pub_id,
                author_position,
                name_val,
                orcid_val,
                reason,
                _jsonb(evidence or {}),
                actor.name,
            ),
        )


def detach_authorship(
    conn: psycopg.Connection,
    *,
    pub_id: int,
    author_position: Optional[int],
    author_name: Optional[str],
    actor: QueueActor,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    cols = _load_authorship_columns(conn)
    row = _fetch_authorship_row(
        conn,
        pub_id=pub_id,
        author_position=author_position,
        author_name=author_name,
        cols=cols,
    )
    if not row:
        raise ValueError("No authorship row matched the selection.")
    if row.get("person_id") is None:
        return {
            "pub_id": pub_id,
            "author_position": row.get("author_position"),
            "updated": False,
            "queue_id": None,
        }

    before_state = {k: v for k, v in row.items() if k != "ctid"}
    set_parts = ["person_id = NULL"]
    if "is_internal_at_ingest" in cols:
        set_parts.append("is_internal_at_ingest = FALSE")
    if "updated_at" in cols:
        set_parts.append("updated_at = now()")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE biblio.authorships
            SET {", ".join(set_parts)}
            WHERE ctid = %s
            """,
            (row["ctid"],),
        )

    author_label = row.get("display_name_at_pub") or row.get("author_name") or author_name
    reason = note or "manual detach via curation admin"
    evidence = {
        "author_position": row.get("author_position"),
        "author_name": row.get("author_name"),
        "display_name_at_pub": row.get("display_name_at_pub"),
        "paper_orcid": row.get("paper_orcid") or row.get("author_orcid"),
    }
    _upsert_force_null_override(
        conn,
        pub_id=pub_id,
        author_position=row.get("author_position"),
        display_name_at_pub=author_label,
        paper_orcid=row.get("paper_orcid") or row.get("author_orcid"),
        actor=actor,
        reason=reason,
        evidence=evidence,
    )
    after_state = {
        **before_state,
        "person_id": None,
        "is_internal_at_ingest": False if "is_internal_at_ingest" in cols else before_state.get("is_internal_at_ingest"),
    }
    if _auth_table_exists(conn, "biblio.manual_corrections_log"):
        _auth_log_manual_correction(
            conn,
            has_log_table=True,
            correction_type="authorship.detach_person",
            target={
                "pub_id": pub_id,
                "author_position": row.get("author_position"),
                "display_name_at_pub": author_label,
                "paper_orcid": _normalize_authorship_orcid(row.get("paper_orcid") or row.get("author_orcid")),
            },
            before_state=before_state,
            after_state=after_state,
            applied_by=actor.name,
            note=reason,
            dry_run=False,
        )
    queue_id = _insert_queue_entry(
        conn,
        review_type="authorship.detach",
        subject_type="authorship",
        subject_pub_id=pub_id,
        subject_person_id=int(before_state["person_id"]),
        actor=actor,
        note=note,
        payload={
            "pub_id": pub_id,
            "author_position": row.get("author_position"),
            "author_name": row.get("author_name"),
            "display_name_at_pub": row.get("display_name_at_pub"),
        },
        resolution={"updated": True, "mode": "force_null"},
    )
    return {
        "pub_id": pub_id,
        "author_position": row.get("author_position"),
        "updated": True,
        "queue_id": queue_id,
    }


def attach_authorship(
    conn: psycopg.Connection,
    *,
    pub_id: int,
    author_position: Optional[int],
    author_name: Optional[str],
    person_id: int,
    actor: QueueActor,
    overwrite: bool = True,
    alias_source: str = "curation_admin.authorship_attach",
    min_score: float = 0.80,
    force: bool = False,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    cols = _load_authorship_columns(conn)
    has_internal_flag = "is_internal_at_ingest" in cols
    has_updated_at = "updated_at" in cols
    has_manual_override_table = _auth_table_exists(conn, "biblio.authorship_manual_overrides")
    has_manual_log_table = _auth_table_exists(conn, "biblio.manual_corrections_log")
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
        dry_run=False,
        cols=cols,
        has_internal_flag=has_internal_flag,
        has_updated_at=has_updated_at,
        has_manual_override_table=has_manual_override_table,
        has_manual_log_table=has_manual_log_table,
        source_file="curation_admin",
        source_row=None,
        applied_by=actor.name,
    )
    queue_id = None
    if stats.updated:
        queue_id = _insert_queue_entry(
            conn,
            review_type="authorship.attach",
            subject_type="authorship",
            subject_pub_id=pub_id,
            subject_person_id=person_id,
            actor=actor,
            note=note,
            payload={
                "pub_id": pub_id,
                "author_position": author_position,
                "author_name": author_name,
                "person_id": person_id,
                "overwrite": overwrite,
            },
            resolution={
                "updated": stats.updated,
                "skipped_existing": stats.skipped_existing,
                "skipped_conflict": stats.skipped_conflict,
                "skipped_low_score": stats.skipped_low_score,
            },
        )
    return {
        "updated": stats.updated,
        "skipped_existing": stats.skipped_existing,
        "skipped_conflict": stats.skipped_conflict,
        "skipped_low_score": stats.skipped_low_score,
        "queue_id": queue_id,
    }


def confirm_publication_winner(
    conn: psycopg.Connection,
    *,
    winner_pub_id: int,
    actor: QueueActor,
    note: Optional[str] = None,
    enqueue: bool = True,
) -> Dict[str, Any]:
    if not _pub_exists(conn, winner_pub_id):
        raise ValueError(f"Publication not found: {winner_pub_id}")
    winner_snapshot = _pub_snapshot(conn, winner_pub_id)
    has_log_table = _dedup_table_exists(conn, "biblio.manual_corrections_log")
    before_state = {"active_decisions": _active_decisions_for_snapshot(conn, winner_snapshot)}
    after_state = _insert_durable_decision(
        conn,
        mode="confirm_winner",
        subject=winner_snapshot,
        winner=winner_snapshot,
        note=note,
        applied_by=actor.name,
        dry_run=False,
    )
    if has_log_table:
        _dedup_log_manual_correction(
            conn,
            has_log_table=True,
            correction_type="dedup_decision.confirm_winner",
            target={
                "subject": {
                    "pub_id": winner_snapshot.get("pub_id"),
                    "doi_key": winner_snapshot.get("doi_key"),
                    "title_key": winner_snapshot.get("title_key"),
                    "year": winner_snapshot.get("year"),
                },
                "winner": {
                    "pub_id": winner_snapshot.get("pub_id"),
                    "doi_key": winner_snapshot.get("doi_key"),
                    "title_key": winner_snapshot.get("title_key"),
                    "year": winner_snapshot.get("year"),
                },
            },
            before_state=before_state,
            after_state=after_state,
            applied_by=actor.name,
            note=note,
            dry_run=False,
        )
    queue_id = None
    if enqueue:
        queue_id = _insert_queue_entry(
            conn,
            review_type="dedup.confirm_winner",
            subject_type="publication",
            subject_pub_id=winner_pub_id,
            actor=actor,
            note=note,
            payload={
                "winner_pub_id": winner_pub_id,
            },
            resolution={
                "decision_id": (after_state or {}).get("decision_id"),
                "mode": "confirm_winner",
            },
        )
    return {
        "winner_pub_id": winner_pub_id,
        "decision_id": (after_state or {}).get("decision_id"),
        "queue_id": queue_id,
    }


def set_publication_group_winner(
    conn: psycopg.Connection,
    *,
    winner_pub_id: int,
    candidate_pub_ids: Sequence[int],
    actor: QueueActor,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    unique_pub_ids = []
    seen: set[int] = set()
    for pub_id in candidate_pub_ids:
        pid = int(pub_id)
        if pid in seen:
            continue
        seen.add(pid)
        unique_pub_ids.append(pid)
    if winner_pub_id not in seen:
        unique_pub_ids.append(winner_pub_id)
    losers = [pid for pid in unique_pub_ids if pid != winner_pub_id]
    has_log_table = _dedup_table_exists(conn, "biblio.manual_corrections_log")
    for loser_pub_id in losers:
        _set_merge_one(
            conn,
            loser_pub_id=loser_pub_id,
            winner_pub_id=winner_pub_id,
            note=note,
            has_log_table=has_log_table,
            applied_by=actor.name,
            dry_run=False,
        )
    confirmation = confirm_publication_winner(
        conn,
        winner_pub_id=winner_pub_id,
        actor=actor,
        note=note,
        enqueue=False,
    )
    queue_id = _insert_queue_entry(
        conn,
        review_type="dedup.set_winner",
        subject_type="publication",
        subject_pub_id=winner_pub_id,
        actor=actor,
        note=note,
        payload={
            "winner_pub_id": winner_pub_id,
            "candidate_pub_ids": unique_pub_ids,
        },
        resolution={
            "loser_pub_ids": losers,
            "confirm_decision_id": confirmation.get("decision_id"),
        },
    )
    return {
        "winner_pub_id": winner_pub_id,
        "loser_pub_ids": losers,
        "queue_id": queue_id,
        "confirm_decision_id": confirmation.get("decision_id"),
    }


def keep_publications_separate(
    conn: psycopg.Connection,
    *,
    pub_ids: Sequence[int],
    actor: QueueActor,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    clean_pub_ids = [int(pub_id) for pub_id in pub_ids]
    has_log_table = _dedup_table_exists(conn, "biblio.manual_corrections_log")
    _set_keep_separate(
        conn,
        pub_ids=clean_pub_ids,
        note=note,
        has_log_table=has_log_table,
        applied_by=actor.name,
        dry_run=False,
    )
    queue_id = _insert_queue_entry(
        conn,
        review_type="dedup.keep_separate",
        subject_type="publication",
        subject_pub_id=clean_pub_ids[0] if clean_pub_ids else None,
        actor=actor,
        note=note,
        payload={"pub_ids": clean_pub_ids},
        resolution={"mode": "keep_separate", "count": len(clean_pub_ids)},
    )
    return {
        "pub_ids": clean_pub_ids,
        "queue_id": queue_id,
    }


def _deactivate_scope_decisions_for_snapshot(
    conn: psycopg.Connection,
    *,
    scope_type: str,
    scope_person_id: Optional[int],
    snapshot: Dict[str, Any],
) -> int:
    selectors: List[str] = ["active IS TRUE", "scope_type = %s"]
    params: List[Any] = [scope_type]
    if scope_type == "person":
        selectors.append("scope_person_id = %s")
        params.append(scope_person_id)
    else:
        selectors.append("scope_person_id IS NULL")
    if snapshot.get("doi_key"):
        selectors.append("subject_doi_key = %s")
        params.append(snapshot["doi_key"])
    elif snapshot.get("title_key"):
        selectors.append("(subject_title_key = %s AND (subject_year IS NULL OR subject_year = %s))")
        params.extend([snapshot["title_key"], snapshot.get("year")])
    else:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE biblio.canon_scope_decisions
            SET active = FALSE,
                updated_at = now()
            WHERE {' AND '.join(selectors)}
            """,
            tuple(params),
        )
        return int(cur.rowcount or 0)


def set_canon_scope_decision(
    conn: psycopg.Connection,
    *,
    pub_id: int,
    mode: str,
    actor: QueueActor,
    note: Optional[str] = None,
    scope_type: str = "global",
    scope_person_id: Optional[int] = None,
) -> Dict[str, Any]:
    if mode not in {"include", "exclude"}:
        raise ValueError(f"Unsupported canon scope mode: {mode}")
    snapshot = _pub_snapshot(conn, pub_id)
    _deactivate_scope_decisions_for_snapshot(
        conn,
        scope_type=scope_type,
        scope_person_id=scope_person_id,
        snapshot=snapshot,
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO biblio.canon_scope_decisions (
              scope_type,
              scope_person_id,
              mode,
              subject_pub_id,
              subject_doi,
              subject_doi_key,
              subject_title,
              subject_title_key,
              subject_year,
              subject_snapshot,
              note,
              evidence,
              created_by,
              created_role
            )
            VALUES (
              %s, %s, %s,
              %s, %s, %s,
              %s, %s, %s,
              %s::jsonb,
              %s,
              %s::jsonb,
              %s,
              %s
            )
            RETURNING decision_id
            """,
            (
                scope_type,
                scope_person_id,
                mode,
                snapshot.get("pub_id"),
                snapshot.get("doi"),
                snapshot.get("doi_key"),
                snapshot.get("title"),
                snapshot.get("title_key"),
                snapshot.get("year"),
                _jsonb(snapshot),
                note,
                _jsonb({"requested_via": "curation_admin"}),
                actor.name,
                actor.role,
            ),
        )
        row = cur.fetchone()
    queue_id = _insert_queue_entry(
        conn,
        review_type="canon_scope.set",
        subject_type="publication",
        subject_pub_id=pub_id,
        subject_person_id=scope_person_id,
        actor=actor,
        note=note,
        payload={
            "scope_type": scope_type,
            "scope_person_id": scope_person_id,
            "mode": mode,
        },
        resolution={"decision_id": row["decision_id"] if row else None},
    )
    if _auth_table_exists(conn, "biblio.manual_corrections_log"):
        _auth_log_manual_correction(
            conn,
            has_log_table=True,
            correction_type="canon_scope.set",
            target={
                "pub_id": pub_id,
                "scope_type": scope_type,
                "scope_person_id": scope_person_id,
                "mode": mode,
            },
            before_state=None,
            after_state={
                "decision_id": row["decision_id"] if row else None,
                "mode": mode,
                "scope_type": scope_type,
                "scope_person_id": scope_person_id,
            },
            applied_by=actor.name,
            note=note,
            dry_run=False,
        )
    return {
        "decision_id": int(row["decision_id"]) if row else None,
        "mode": mode,
        "queue_id": queue_id,
    }


def fetch_publication_dedup_family(
    conn: psycopg.Connection,
    *,
    pub_id: int,
) -> Optional[Dict[str, Any]]:
    if not _view_exists(conn, "biblio.publication_dedup_memberships"):
        return None

    family_rows = _rows(
        conn,
        """
        WITH family AS (
          SELECT canonical_pub_id
          FROM biblio.publication_dedup_memberships
          WHERE pub_id = %s
          LIMIT 1
        )
        SELECT
          m.canonical_pub_id,
          m.pub_id,
          m.is_canonical,
          m.candidate_count,
          m.canonical_locked,
          m.confirm_winner_decision_id,
          m.dedup_override_mode,
          p.doi,
          p.title,
          p.year,
          p.venue,
          p.source_of_truth,
          p.is_preprint
        FROM biblio.publication_dedup_memberships m
        JOIN family f ON f.canonical_pub_id = m.canonical_pub_id
        JOIN biblio.publications p ON p.pub_id = m.pub_id
        ORDER BY
          m.is_canonical DESC,
          CASE
            WHEN p.is_preprint IS FALSE THEN 0
            WHEN p.is_preprint IS NULL THEN 1
            ELSE 2
          END,
          p.year DESC NULLS LAST,
          p.pub_id DESC
        """,
        (pub_id,),
    )
    if not family_rows:
        return None

    scope_row = None
    if _view_exists(conn, "biblio.canon_scope_matches"):
        scope_row = _row(
            conn,
            """
            SELECT pub_id, mode, decision_id
            FROM biblio.canon_scope_matches
            WHERE pub_id = %s
              AND scope_type = 'global'
              AND scope_person_id IS NULL
            """,
            (int(family_rows[0]["canonical_pub_id"]),),
        )

    canonical_pub_id = int(family_rows[0]["canonical_pub_id"])
    candidates = []
    for row in family_rows:
        candidates.append(
            {
                "pub_id": int(row["pub_id"]),
                "doi": row.get("doi"),
                "title": row.get("title"),
                "year": row.get("year"),
                "venue": row.get("venue"),
                "source_of_truth": row.get("source_of_truth"),
                "is_preprint": row.get("is_preprint"),
                "source_tokens": [
                    token
                    for token in str(row.get("source_of_truth") or "").replace("+", " ").split()
                    if token
                ],
                "is_canonical": bool(row.get("is_canonical")),
                "dedup_override_mode": row.get("dedup_override_mode"),
            }
        )
    return {
        "canonical_pub_id": canonical_pub_id,
        "candidate_count": int(family_rows[0]["candidate_count"] or 0),
        "canonical_locked": bool(family_rows[0].get("canonical_locked")),
        "confirm_winner_decision_id": family_rows[0].get("confirm_winner_decision_id"),
        "canon_scope_mode": scope_row.get("mode") if scope_row else None,
        "canon_scope_decision_id": scope_row.get("decision_id") if scope_row else None,
        "candidates": candidates,
    }


def fetch_person_dedup_reviews(
    conn: psycopg.Connection,
    *,
    person_id: int,
) -> List[Dict[str, Any]]:
    if not _view_exists(conn, "biblio.publication_dedup_memberships"):
        return []

    rows = _rows(
        conn,
        """
        WITH person_families AS (
          SELECT DISTINCT m.canonical_pub_id
          FROM biblio.publication_dedup_memberships m
          JOIN biblio.authorships_curated a ON a.pub_id = m.pub_id
          WHERE a.person_id = %s
        )
        SELECT
          m.canonical_pub_id,
          m.pub_id,
          m.is_canonical,
          m.candidate_count,
          m.canonical_locked,
          m.confirm_winner_decision_id,
          m.dedup_override_mode,
          p.doi,
          p.title,
          p.year,
          p.venue,
          p.source_of_truth,
          p.is_preprint
        FROM biblio.publication_dedup_memberships m
        JOIN person_families pc ON pc.canonical_pub_id = m.canonical_pub_id
        JOIN biblio.publications p ON p.pub_id = m.pub_id
        WHERE m.candidate_count > 1
           OR m.dedup_override_mode IS NOT NULL
           OR m.confirm_winner_decision_id IS NOT NULL
        ORDER BY
          m.canonical_pub_id DESC,
          m.is_canonical DESC,
          CASE
            WHEN p.is_preprint IS FALSE THEN 0
            WHEN p.is_preprint IS NULL THEN 1
            ELSE 2
          END,
          p.year DESC NULLS LAST,
          p.pub_id DESC
        """,
        (person_id,),
    )
    grouped: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        canonical_pub_id = int(row["canonical_pub_id"])
        entry = grouped.setdefault(
            canonical_pub_id,
            {
                "canonical_pub_id": canonical_pub_id,
                "candidate_count": int(row["candidate_count"] or 0),
                "canonical_locked": bool(row.get("canonical_locked")),
                "confirm_winner_decision_id": row.get("confirm_winner_decision_id"),
                "candidates": [],
            },
        )
        entry["candidates"].append(
            {
                "pub_id": int(row["pub_id"]),
                "doi": row.get("doi"),
                "title": row.get("title"),
                "year": row.get("year"),
                "venue": row.get("venue"),
                "source_of_truth": row.get("source_of_truth"),
                "is_preprint": row.get("is_preprint"),
                "source_tokens": [
                    token
                    for token in str(row.get("source_of_truth") or "").replace("+", " ").split()
                    if token
                ],
                "is_canonical": bool(row.get("is_canonical")),
                "dedup_override_mode": row.get("dedup_override_mode"),
            }
        )
    return list(grouped.values())
