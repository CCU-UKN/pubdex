"""
people_pubs.sync.merge_people

Safely merge a duplicate person into a canonical person.

The script rewires references from --drop-person-id to --keep-person-id across
the live tables that carry person_id, merges refresh state / aliases / PII, and
then deletes the losing app.people row.

ORCID handling is explicit: if multiple ORCIDs are present across the two
people, choose which ORCID(s) to keep with --keep-orcid or by answering the
interactive prompt. Unselected ORCIDs are removed instead of being merged by
default.

Safety defaults:
  - preview by default: without --execute the merge runs and is rolled back,
    so nothing is committed (there is no separate --dry-run option)
  - aborts if both people are attached to the same publication in
    biblio.authorships, because that case needs manual review
  - intended to be run as a write-capable role with access to pii, typically
    postgres

Examples:
  Preview (omit --execute):
  python -m people_pubs.sync.merge_people \
    --keep-person-id 62 \
    --drop-person-id 281 \
    --keep-orcid 0000-0000-0000-0002 \
    --debug

  Apply the merge (include --execute):
  python -m people_pubs.sync.merge_people \
    --keep-person-id 62 \
    --drop-person-id 281 \
    --keep-orcid 0000-0000-0000-0002 \
    --execute --debug
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

import psycopg
from psycopg.rows import dict_row

from people_pubs.db.connection import db_conn
from people_pubs.db.people import merge_emails, pick_primary_email
from people_pubs.utils.identifiers import normalize_orcid


@dataclass
class MergeStats:
    authorships_moved: int = 0
    authorship_overrides_moved: int = 0
    refresh_logs_moved: int = 0
    fetch_jobs_moved: int = 0
    employments_moved: int = 0
    orcid_rows_moved: int = 0
    orcid_rows_deleted: int = 0
    scholar_rows_moved: int = 0
    person_keys_inserted: int = 0
    person_keys_deleted: int = 0
    alias_rows_inserted: int = 0
    alias_rows_updated: int = 0
    alias_rows_deleted: int = 0
    source_refresh_rows_moved: int = 0
    source_refresh_rows_merged: int = 0
    person_refresh_rows_moved: int = 0
    person_refresh_rows_merged: int = 0
    pii_rows_merged: int = 0
    pii_email_rows_moved: int = 0
    pii_pub_email_rows_moved: int = 0
    pii_payload_rows_moved: int = 0
    pii_email_log_rows_moved: int = 0
    drop_person_deleted: bool = False
    notes: List[str] = field(default_factory=list)




def _max_ts(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _min_ts(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b


def _row(
    conn: psycopg.Connection,
    sql: str,
    params: Sequence[Any] | None = None,
) -> Optional[Dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()


def _rows(
    conn: psycopg.Connection,
    sql: str,
    params: Sequence[Any] | None = None,
) -> List[Dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params or ())
        return list(cur.fetchall())


def _scalar(
    conn: psycopg.Connection,
    sql: str,
    params: Sequence[Any] | None = None,
) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def _as_email_list(*values: Any) -> List[str]:
    out: List[str] = []
    for value in values:
        if value is None:
            continue
        for item in _normalize_text_array(value):
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
    return out


def _normalize_text_array(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if not isinstance(value, str):
        s = str(value).strip()
        return [s] if s else []

    s = value.strip()
    if not s or s in {"{}", "[]"}:
        return []
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        parts = [p.strip().strip('"') for p in inner.split(",")]
        return [p for p in parts if p]
    return [s]


def _merge_text_arrays(*values: Any) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        if value is None:
            continue
        for item in _normalize_text_array(value):
            if item is None:
                continue
            text = str(item).strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
    return out


def _normalize_orcid(value: Optional[str]) -> Optional[str]:
    return normalize_orcid(value)


def _resolve_person_ids(
    keep_person_id: Optional[int],
    drop_person_id: Optional[int],
) -> tuple[int, int]:
    keep_id = keep_person_id
    drop_id = drop_person_id
    if keep_id is None:
        if not sys.stdin.isatty():
            raise RuntimeError("Missing --keep-person-id")
        keep_raw = input("Keep which person_id? ").strip()
        keep_id = int(keep_raw)
    if drop_id is None:
        if not sys.stdin.isatty():
            raise RuntimeError("Missing --drop-person-id")
        drop_raw = input("Drop which person_id? ").strip()
        drop_id = int(drop_raw)
    return int(keep_id), int(drop_id)


def _choose_keep_orcids(
    conn: psycopg.Connection,
    keep_id: int,
    drop_id: int,
    keep_orcids: Optional[Sequence[str]],
) -> List[str]:
    rows = _rows(
        conn,
        """
        SELECT person_id, orcid, orcid_display_name
        FROM app.identities_orcid
        WHERE person_id IN (%s, %s)
        ORDER BY person_id, orcid
        """,
        (keep_id, drop_id),
    )
    available = []
    seen = set()
    for row in rows:
        normalized = _normalize_orcid(row.get("orcid"))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        available.append(
            {
                "person_id": int(row["person_id"]),
                "orcid": normalized,
                "label": row.get("orcid_display_name"),
            }
        )

    if not available:
        return []

    if keep_orcids:
        wanted = []
        seen_wanted = set()
        for raw in keep_orcids:
            normalized = _normalize_orcid(raw)
            if not normalized:
                raise RuntimeError(f"Invalid --keep-orcid value: {raw!r}")
            if normalized not in seen_wanted:
                seen_wanted.add(normalized)
                wanted.append(normalized)
        available_orcids = {row["orcid"] for row in available}
        unknown = [o for o in wanted if o not in available_orcids]
        if unknown:
            raise RuntimeError(
                "Selected ORCID(s) not found on the two people being merged: "
                + ", ".join(unknown)
            )
        return wanted

    if len(available) == 1:
        return [available[0]["orcid"]]

    if not sys.stdin.isatty():
        options = ", ".join(f"{row['orcid']} (person_id={row['person_id']})" for row in available)
        raise RuntimeError(
            "Multiple ORCIDs found. Re-run with one or more --keep-orcid values. "
            f"Available: {options}"
        )

    print(f"Multiple ORCIDs found for person_id {keep_id} and {drop_id}:")
    for idx, row in enumerate(available, start=1):
        label = f" [{row['label']}]" if row.get("label") else ""
        print(f"  {idx}. {row['orcid']} (currently on person_id={row['person_id']}){label}")
    print("Enter the number(s) or ORCID value(s) to keep, comma-separated.")
    raw = input("Keep ORCID(s): ").strip()
    if not raw:
        raise RuntimeError("No ORCID choice provided.")

    picked: List[str] = []
    by_index = {str(i): row["orcid"] for i, row in enumerate(available, start=1)}
    by_orcid = {row["orcid"]: row["orcid"] for row in available}
    for token in [part.strip() for part in raw.split(",") if part.strip()]:
        normalized = by_index.get(token)
        if normalized is None:
            normalized = _normalize_orcid(token)
            if normalized is not None:
                normalized = by_orcid.get(normalized)
        if normalized is None:
            raise RuntimeError(f"Invalid ORCID selection: {token!r}")
        if normalized not in picked:
            picked.append(normalized)
    if not picked:
        raise RuntimeError("No ORCIDs selected to keep.")
    return picked


def _merge_people_pii(
    conn: psycopg.Connection,
    keep_id: int,
    drop_id: int,
    *,
    dry_run: bool,
    stats: MergeStats,
) -> None:
    keep_row = _row(conn, "SELECT * FROM pii.people_pii WHERE person_id = %s", (keep_id,))
    drop_row = _row(conn, "SELECT * FROM pii.people_pii WHERE person_id = %s", (drop_id,))
    if drop_row is None:
        return

    if keep_row is None:
        logging.info("  moving pii.people_pii row %s -> %s", drop_id, keep_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE pii.people_pii SET person_id = %s WHERE person_id = %s",
                    (keep_id, drop_id),
                )
        stats.pii_rows_merged += 1
        return

    merged_emails = merge_emails(
        _normalize_text_array(keep_row.get("emails")),
        _as_email_list(
            keep_row.get("primary_email"),
            keep_row.get("email"),
            drop_row.get("primary_email"),
            drop_row.get("email"),
            drop_row.get("emails"),
        ),
    )
    merged_primary = pick_primary_email(keep_row.get("primary_email"), merged_emails)
    merged_email = keep_row.get("email") or drop_row.get("email") or merged_primary
    merged_phone = keep_row.get("phone") or drop_row.get("phone")
    merged_room = keep_row.get("room") or drop_row.get("room")
    merged_postal = keep_row.get("postal_address") or drop_row.get("postal_address")
    merged_websites = keep_row.get("websites") or drop_row.get("websites")
    merged_consent = keep_row.get("consent_flags") or drop_row.get("consent_flags")

    logging.info(
        "  merging pii.people_pii: keep=%s drop=%s primary_email=%r merged_emails=%r",
        keep_id,
        drop_id,
        merged_primary,
        merged_emails,
    )
    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pii.people_pii
                SET primary_email = %s,
                    emails = %s,
                    email = %s,
                    phone = %s,
                    room = %s,
                    postal_address = %s,
                    websites = %s,
                    consent_flags = %s,
                    updated_at = now()
                WHERE person_id = %s
                """,
                (
                    merged_primary,
                    merged_emails,
                    merged_email,
                    merged_phone,
                    merged_room,
                    merged_postal,
                    merged_websites,
                    merged_consent,
                    keep_id,
                ),
            )
            cur.execute("DELETE FROM pii.people_pii WHERE person_id = %s", (drop_id,))
    stats.pii_rows_merged += 1


def _merge_aliases_manual(
    conn: psycopg.Connection,
    keep_id: int,
    drop_id: int,
    keep_person: Dict[str, Any],
    drop_person: Dict[str, Any],
    *,
    dry_run: bool,
    stats: MergeStats,
) -> None:
    source_aliases = _rows(
        conn,
        """
        SELECT person_id, alias_name, alias_sources
        FROM app.person_name_aliases_manual
        WHERE person_id = %s
        ORDER BY alias_name
        """,
        (drop_id,),
    )
    drop_display_name = (drop_person.get("display_name") or "").strip()
    keep_display_name = (keep_person.get("display_name") or "").strip()
    if drop_display_name and drop_display_name.lower() != keep_display_name.lower():
        source_aliases.append(
            {
                "person_id": drop_id,
                "alias_name": drop_display_name,
                "alias_sources": ["app.people.display_name"],
            }
        )

    seen = set()
    for alias_row in source_aliases:
        alias_name = str(alias_row.get("alias_name") or "").strip()
        if not alias_name:
            continue
        key = alias_name.lower()
        if key in seen:
            continue
        seen.add(key)

        dest_row = _row(
            conn,
            """
            SELECT alias_name, alias_sources
            FROM app.person_name_aliases_manual
            WHERE person_id = %s AND lower(alias_name) = lower(%s)
            """,
            (keep_id, alias_name),
        )
        merged_sources = _merge_text_arrays(
            (dest_row or {}).get("alias_sources"),
            alias_row.get("alias_sources"),
        )
        if dest_row is None:
            logging.info("  adding alias %r to person_id=%s", alias_name, keep_id)
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app.person_name_aliases_manual
                          (person_id, alias_name, alias_sources, created_at, updated_at)
                        VALUES (%s, %s, %s, now(), now())
                        """,
                        (keep_id, alias_name, merged_sources),
                    )
            stats.alias_rows_inserted += 1
        elif merged_sources != (dest_row.get("alias_sources") or []):
            logging.info("  merging alias sources for %r on person_id=%s", alias_name, keep_id)
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE app.person_name_aliases_manual
                        SET alias_sources = %s,
                            updated_at = now()
                        WHERE person_id = %s AND lower(alias_name) = lower(%s)
                        """,
                        (merged_sources, keep_id, alias_name),
                    )
            stats.alias_rows_updated += 1

    source_count = _scalar(
        conn,
        "SELECT COUNT(*) FROM app.person_name_aliases_manual WHERE person_id = %s",
        (drop_id,),
    ) or 0
    if source_count:
        logging.info("  deleting %s manual alias row(s) from person_id=%s", source_count, drop_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.person_name_aliases_manual WHERE person_id = %s",
                    (drop_id,),
                )
        stats.alias_rows_deleted += int(source_count)


def _merge_person_refresh_state(
    conn: psycopg.Connection,
    keep_id: int,
    drop_id: int,
    *,
    dry_run: bool,
    stats: MergeStats,
) -> None:
    keep_row = _row(
        conn,
        "SELECT * FROM activity.person_refresh_state WHERE person_id = %s",
        (keep_id,),
    )
    drop_row = _row(
        conn,
        "SELECT * FROM activity.person_refresh_state WHERE person_id = %s",
        (drop_id,),
    )
    if drop_row is None:
        return

    if keep_row is None:
        logging.info("  moving activity.person_refresh_state %s -> %s", drop_id, keep_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE activity.person_refresh_state
                    SET person_id = %s,
                        updated_at = now()
                    WHERE person_id = %s
                    """,
                    (keep_id, drop_id),
                )
        stats.person_refresh_rows_moved += 1
        return

    merged_profile = _max_ts(
        keep_row.get("last_orcid_profile_at"),
        drop_row.get("last_orcid_profile_at"),
    )
    merged_works = _max_ts(
        keep_row.get("last_orcid_pub_refresh_at"),
        drop_row.get("last_orcid_pub_refresh_at"),
    )
    logging.info("  merging activity.person_refresh_state for keep=%s drop=%s", keep_id, drop_id)
    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE activity.person_refresh_state
                SET last_orcid_profile_at = %s,
                    last_orcid_pub_refresh_at = %s,
                    updated_at = now()
                WHERE person_id = %s
                """,
                (merged_profile, merged_works, keep_id),
            )
            cur.execute(
                "DELETE FROM activity.person_refresh_state WHERE person_id = %s",
                (drop_id,),
            )
    stats.person_refresh_rows_merged += 1


def _merge_source_refresh_state(
    conn: psycopg.Connection,
    keep_id: int,
    drop_id: int,
    *,
    dry_run: bool,
    stats: MergeStats,
) -> None:
    drop_rows = _rows(
        conn,
        """
        SELECT *
        FROM activity.source_refresh_state
        WHERE person_id = %s
        ORDER BY source
        """,
        (drop_id,),
    )
    for src_row in drop_rows:
        dest_row = _row(
            conn,
            """
            SELECT *
            FROM activity.source_refresh_state
            WHERE scope_type = 'person' AND person_id = %s AND source = %s
            """,
            (keep_id, src_row["source"]),
        )
        if dest_row is None:
            logging.info(
                "  moving activity.source_refresh_state source=%s %s -> %s",
                src_row["source"],
                drop_id,
                keep_id,
            )
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE activity.source_refresh_state
                        SET person_id = %s,
                            updated_at = now()
                        WHERE source = %s AND scope_type = 'person' AND person_id = %s
                        """,
                        (keep_id, src_row["source"], drop_id),
                    )
            stats.source_refresh_rows_moved += 1
            continue

        merged_last_error_at = _max_ts(
            dest_row.get("last_error_at"),
            src_row.get("last_error_at"),
        )
        if src_row.get("last_error_at") and (
            dest_row.get("last_error_at") is None
            or src_row["last_error_at"] >= dest_row["last_error_at"]
        ):
            merged_last_error_msg = src_row.get("last_error_msg") or dest_row.get("last_error_msg")
        else:
            merged_last_error_msg = dest_row.get("last_error_msg") or src_row.get("last_error_msg")

        logging.info(
            "  merging activity.source_refresh_state source=%s into person_id=%s",
            src_row["source"],
            keep_id,
        )
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE activity.source_refresh_state
                    SET last_attempt_at = %s,
                        last_success_at = %s,
                        external_updated_at = %s,
                        last_error_at = %s,
                        last_error_msg = %s,
                        updated_at = now()
                    WHERE source = %s AND scope_type = 'person' AND person_id = %s
                    """,
                    (
                        _max_ts(dest_row.get("last_attempt_at"), src_row.get("last_attempt_at")),
                        _max_ts(dest_row.get("last_success_at"), src_row.get("last_success_at")),
                        _max_ts(dest_row.get("external_updated_at"), src_row.get("external_updated_at")),
                        merged_last_error_at,
                        merged_last_error_msg,
                        src_row["source"],
                        keep_id,
                    ),
                )
                cur.execute(
                    """
                    DELETE FROM activity.source_refresh_state
                    WHERE source = %s AND scope_type = 'person' AND person_id = %s
                    """,
                    (src_row["source"], drop_id),
                )
        stats.source_refresh_rows_merged += 1


def _merge_orcid_payloads(
    conn: psycopg.Connection,
    keep_id: int,
    drop_id: int,
    *,
    dry_run: bool,
    stats: MergeStats,
) -> None:
    keep_row = _row(conn, "SELECT * FROM pii.orcid_payloads WHERE person_id = %s", (keep_id,))
    drop_row = _row(conn, "SELECT * FROM pii.orcid_payloads WHERE person_id = %s", (drop_id,))
    if drop_row is None:
        return

    if keep_row is None:
        logging.info("  moving pii.orcid_payloads %s -> %s", drop_id, keep_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE pii.orcid_payloads SET person_id = %s WHERE person_id = %s",
                    (keep_id, drop_id),
                )
        stats.pii_payload_rows_moved += 1
        return

    logging.info("  merging pii.orcid_payloads for keep=%s drop=%s", keep_id, drop_id)
    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pii.orcid_payloads
                SET person_json = COALESCE(person_json, %s),
                    record_json = COALESCE(record_json, %s),
                    fetched_at = %s
                WHERE person_id = %s
                """,
                (
                    drop_row.get("person_json"),
                    drop_row.get("record_json"),
                    _max_ts(keep_row.get("fetched_at"), drop_row.get("fetched_at")),
                    keep_id,
                ),
            )
            cur.execute("DELETE FROM pii.orcid_payloads WHERE person_id = %s", (drop_id,))
    stats.pii_payload_rows_moved += 1


def _merge_single_row_identity(
    conn: psycopg.Connection,
    table_name: str,
    keep_id: int,
    drop_id: int,
    *,
    key_field: str,
    dry_run: bool,
    stats_attr: str,
    stats: MergeStats,
) -> None:
    keep_row = _row(conn, f"SELECT * FROM {table_name} WHERE person_id = %s", (keep_id,))
    drop_row = _row(conn, f"SELECT * FROM {table_name} WHERE person_id = %s", (drop_id,))
    if drop_row is None:
        return
    if keep_row is None:
        logging.info("  moving %s %s -> %s", table_name, drop_id, keep_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {table_name} SET person_id = %s WHERE person_id = %s",
                    (keep_id, drop_id),
                )
        setattr(stats, stats_attr, getattr(stats, stats_attr) + 1)
        return

    if keep_row.get(key_field) != drop_row.get(key_field):
        raise RuntimeError(
            f"{table_name} has conflicting rows for keep={keep_id} and drop={drop_id}: "
            f"{keep_row.get(key_field)!r} vs {drop_row.get(key_field)!r}"
        )

    logging.info("  deleting duplicate %s row for person_id=%s", table_name, drop_id)
    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table_name} WHERE person_id = %s", (drop_id,))
    stats.notes.append(f"Deleted duplicate {table_name} row for person_id={drop_id}")


def run_merge_people(
    conn: psycopg.Connection,
    keep_id: int,
    drop_id: int,
    *,
    dry_run: bool,
    keep_orcids: Sequence[str],
) -> MergeStats:
    if keep_id == drop_id:
        raise ValueError("--keep-person-id and --drop-person-id must differ")

    keep_person = _row(conn, "SELECT * FROM app.people WHERE person_id = %s", (keep_id,))
    drop_person = _row(conn, "SELECT * FROM app.people WHERE person_id = %s", (drop_id,))
    if keep_person is None:
        raise RuntimeError(f"keep person_id={keep_id} not found")
    if drop_person is None:
        raise RuntimeError(f"drop person_id={drop_id} not found")

    overlap_pub_ids = [
        int(r["pub_id"])
        for r in _rows(
            conn,
            """
            SELECT pub_id
            FROM biblio.authorships
            WHERE person_id = %s
            INTERSECT
            SELECT pub_id
            FROM biblio.authorships
            WHERE person_id = %s
            ORDER BY pub_id
            """,
            (keep_id, drop_id),
        )
    ]
    if overlap_pub_ids:
        raise RuntimeError(
            "Refusing to merge because both people already appear on the same "
            f"publication(s): {overlap_pub_ids}"
        )

    stats = MergeStats()
    keep_orcid_set = {o for o in (_normalize_orcid(v) for v in keep_orcids) if o}

    authorships_to_move = _scalar(
        conn,
        "SELECT COUNT(*) FROM biblio.authorships WHERE person_id = %s",
        (drop_id,),
    ) or 0
    if authorships_to_move:
        logging.info("  moving %s authorship row(s) %s -> %s", authorships_to_move, drop_id, keep_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE biblio.authorships SET person_id = %s WHERE person_id = %s",
                    (keep_id, drop_id),
                )
        stats.authorships_moved += int(authorships_to_move)

    override_rows = _scalar(
        conn,
        "SELECT COUNT(*) FROM biblio.authorship_manual_overrides WHERE person_id = %s",
        (drop_id,),
    ) or 0
    if override_rows:
        logging.info(
            "  moving %s authorship override row(s) %s -> %s",
            override_rows,
            drop_id,
            keep_id,
        )
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE biblio.authorship_manual_overrides
                    SET person_id = %s,
                        updated_at = now()
                    WHERE person_id = %s
                    """,
                    (keep_id, drop_id),
                )
        stats.authorship_overrides_moved += int(override_rows)

    refresh_log_rows = _scalar(
        conn,
        "SELECT COUNT(*) FROM activity.refresh_run_log WHERE person_id = %s",
        (drop_id,),
    ) or 0
    if refresh_log_rows:
        logging.info("  moving %s refresh log row(s) %s -> %s", refresh_log_rows, drop_id, keep_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE activity.refresh_run_log SET person_id = %s WHERE person_id = %s",
                    (keep_id, drop_id),
                )
        stats.refresh_logs_moved += int(refresh_log_rows)

    fetch_job_rows = _scalar(
        conn,
        "SELECT COUNT(*) FROM activity.fetch_jobs WHERE person_id = %s",
        (drop_id,),
    ) or 0
    if fetch_job_rows:
        logging.info("  moving %s fetch job row(s) %s -> %s", fetch_job_rows, drop_id, keep_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE activity.fetch_jobs SET person_id = %s WHERE person_id = %s",
                    (keep_id, drop_id),
                )
        stats.fetch_jobs_moved += int(fetch_job_rows)

    employment_rows = _scalar(
        conn,
        "SELECT COUNT(*) FROM app.employments WHERE person_id = %s",
        (drop_id,),
    ) or 0
    if employment_rows:
        logging.info("  moving %s employment row(s) %s -> %s", employment_rows, drop_id, keep_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.employments SET person_id = %s WHERE person_id = %s",
                    (keep_id, drop_id),
                )
        stats.employments_moved += int(employment_rows)

    existing_keep_orcid_rows = _rows(
        conn,
        """
        SELECT orcid
        FROM app.identities_orcid
        WHERE person_id = %s
        ORDER BY orcid
        """,
        (keep_id,),
    )
    for row in existing_keep_orcid_rows:
        normalized = _normalize_orcid(row.get("orcid"))
        if normalized and normalized not in keep_orcid_set:
            logging.info("  deleting unselected ORCID %s from person_id=%s", normalized, keep_id)
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM app.identities_orcid WHERE person_id = %s AND orcid = %s",
                        (keep_id, normalized),
                    )
            stats.orcid_rows_deleted += 1

    drop_orcid_rows = _rows(
        conn,
        """
        SELECT orcid
        FROM app.identities_orcid
        WHERE person_id = %s
        ORDER BY orcid
        """,
        (drop_id,),
    )
    for row in drop_orcid_rows:
        normalized = _normalize_orcid(row.get("orcid"))
        if not normalized:
            continue
        if normalized not in keep_orcid_set:
            logging.info("  deleting unselected ORCID %s from person_id=%s", normalized, drop_id)
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM app.identities_orcid WHERE person_id = %s AND orcid = %s",
                        (drop_id, normalized),
                    )
            stats.orcid_rows_deleted += 1
            continue

        existing = _scalar(
            conn,
            "SELECT COUNT(*) FROM app.identities_orcid WHERE person_id = %s AND orcid = %s",
            (keep_id, normalized),
        ) or 0
        if existing:
            logging.info(
                "  deleting duplicate selected ORCID %s from person_id=%s",
                normalized,
                drop_id,
            )
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM app.identities_orcid WHERE person_id = %s AND orcid = %s",
                        (drop_id, normalized),
                    )
            stats.orcid_rows_deleted += 1
            continue

        logging.info("  moving ORCID identity %s %s -> %s", normalized, drop_id, keep_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app.identities_orcid
                    SET person_id = %s
                    WHERE person_id = %s AND orcid = %s
                    """,
                    (keep_id, drop_id, normalized),
                )
        stats.orcid_rows_moved += 1

    scholar_drop_rows = _scalar(
        conn,
        "SELECT COUNT(*) FROM app.identities_scholar WHERE person_id = %s",
        (drop_id,),
    ) or 0
    if scholar_drop_rows:
        _merge_single_row_identity(
            conn,
            "app.identities_scholar",
            keep_id,
            drop_id,
            key_field="scholar_id",
            dry_run=dry_run,
            stats_attr="scholar_rows_moved",
            stats=stats,
        )

    key_rows = _rows(
        conn,
        """
        SELECT person_id, key_type, key_value, is_verified, source
        FROM app.person_keys
        WHERE person_id = %s
        ORDER BY key_type, key_value
        """,
        (drop_id,),
    )

    keep_orcid_key_rows = _rows(
        conn,
        """
        SELECT key_value
        FROM app.person_keys
        WHERE person_id = %s AND key_type = 'orcid'
        ORDER BY key_value
        """,
        (keep_id,),
    )
    for row in keep_orcid_key_rows:
        normalized = _normalize_orcid(row.get("key_value"))
        if normalized and normalized not in keep_orcid_set:
            logging.info(
                "  deleting unselected ORCID person_key %s from person_id=%s",
                normalized,
                keep_id,
            )
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM app.person_keys
                        WHERE person_id = %s AND key_type = 'orcid' AND key_value = %s
                        """,
                        (keep_id, normalized),
                    )
            stats.person_keys_deleted += 1

    for key_row in key_rows:
        normalized_key_value = (
            _normalize_orcid(key_row["key_value"])
            if key_row["key_type"] == "orcid"
            else key_row["key_value"]
        )
        if key_row["key_type"] == "orcid" and normalized_key_value not in keep_orcid_set:
            logging.info(
                "  deleting unselected ORCID person_key %s from person_id=%s",
                normalized_key_value,
                drop_id,
            )
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM app.person_keys
                        WHERE person_id = %s AND key_type = %s AND key_value = %s
                        """,
                        (drop_id, key_row["key_type"], key_row["key_value"]),
                    )
            stats.person_keys_deleted += 1
            continue

        existing = _row(
            conn,
            """
            SELECT person_id, is_verified, source
            FROM app.person_keys
            WHERE person_id = %s AND key_type = %s AND key_value = %s
            """,
            (keep_id, key_row["key_type"], key_row["key_value"]),
        )
        if existing is not None:
            if key_row["is_verified"] and not existing.get("is_verified"):
                logging.info(
                    "  marking existing person_key verified for person_id=%s %s=%r",
                    keep_id,
                    key_row["key_type"],
                    key_row["key_value"],
                )
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE app.person_keys
                            SET is_verified = TRUE,
                                source = COALESCE(source, %s)
                            WHERE person_id = %s AND key_type = %s AND key_value = %s
                            """,
                            (
                                key_row["source"],
                                keep_id,
                                key_row["key_type"],
                                key_row["key_value"],
                            ),
                        )
                logging.info(
                    "  deleting duplicate person_key row from person_id=%s for %s=%r",
                    drop_id,
                    key_row["key_type"],
                    key_row["key_value"],
                )
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            DELETE FROM app.person_keys
                            WHERE person_id = %s AND key_type = %s AND key_value = %s
                            """,
                            (drop_id, key_row["key_type"], key_row["key_value"]),
                        )
                stats.person_keys_deleted += 1
            else:
                stats.notes.append(
                    f"person_key already present on keep person: {key_row['key_type']}={key_row['key_value']!r}"
                )
            continue

        logging.info(
            "  moving person_key %s=%r %s -> %s",
            key_row["key_type"],
            key_row["key_value"],
            drop_id,
            keep_id,
        )
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app.person_keys
                    SET person_id = %s
                    WHERE person_id = %s AND key_type = %s AND key_value = %s
                    """,
                    (keep_id, drop_id, key_row["key_type"], key_row["key_value"]),
                )
        stats.person_keys_inserted += 1

    _merge_aliases_manual(conn, keep_id, drop_id, keep_person, drop_person, dry_run=dry_run, stats=stats)
    _merge_person_refresh_state(conn, keep_id, drop_id, dry_run=dry_run, stats=stats)
    _merge_source_refresh_state(conn, keep_id, drop_id, dry_run=dry_run, stats=stats)
    _merge_people_pii(conn, keep_id, drop_id, dry_run=dry_run, stats=stats)
    _merge_orcid_payloads(conn, keep_id, drop_id, dry_run=dry_run, stats=stats)

    pii_email_rows = _scalar(
        conn,
        "SELECT COUNT(*) FROM pii.orcid_emails WHERE person_id = %s",
        (drop_id,),
    ) or 0
    if pii_email_rows:
        logging.info("  moving %s ORCID email row(s) %s -> %s", pii_email_rows, drop_id, keep_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pii.orcid_emails (person_id, email, verified, visibility, source)
                    SELECT %s, email, verified, visibility, source
                    FROM pii.orcid_emails
                    WHERE person_id = %s
                    ON CONFLICT (person_id, email) DO NOTHING
                    """,
                    (keep_id, drop_id),
                )
                cur.execute("DELETE FROM pii.orcid_emails WHERE person_id = %s", (drop_id,))
        stats.pii_email_rows_moved += int(pii_email_rows)

    pii_pub_email_rows = _scalar(
        conn,
        "SELECT COUNT(*) FROM pii.publication_author_emails WHERE person_id = %s",
        (drop_id,),
    ) or 0
    if pii_pub_email_rows:
        logging.info(
            "  moving %s publication author email row(s) %s -> %s",
            pii_pub_email_rows,
            drop_id,
            keep_id,
        )
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pii.publication_author_emails
                      (pub_id, person_id, email, is_corresponding)
                    SELECT pub_id, %s, email, is_corresponding
                    FROM pii.publication_author_emails
                    WHERE person_id = %s
                    ON CONFLICT (pub_id, person_id, email) DO NOTHING
                    """,
                    (keep_id, drop_id),
                )
                cur.execute(
                    "DELETE FROM pii.publication_author_emails WHERE person_id = %s",
                    (drop_id,),
                )
        stats.pii_pub_email_rows_moved += int(pii_pub_email_rows)

    email_log_rows = _scalar(
        conn,
        "SELECT COUNT(*) FROM pii.email_log WHERE person_id = %s",
        (drop_id,),
    ) or 0
    if email_log_rows:
        logging.info("  moving %s email log row(s) %s -> %s", email_log_rows, drop_id, keep_id)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE pii.email_log SET person_id = %s WHERE person_id = %s",
                    (keep_id, drop_id),
                )
        stats.pii_email_log_rows_moved += int(email_log_rows)

    merged_last_profile_refresh = _max_ts(
        keep_person.get("last_profile_refresh_at"),
        drop_person.get("last_profile_refresh_at"),
    )
    merged_last_change = _max_ts(
        keep_person.get("last_change_detected_at"),
        drop_person.get("last_change_detected_at"),
    )
    merged_created_at = _min_ts(keep_person.get("created_at"), drop_person.get("created_at"))
    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.people
                SET last_profile_refresh_at = %s,
                    last_change_detected_at = %s,
                    created_at = %s,
                    updated_at = now()
                WHERE person_id = %s
                """,
                (
                    merged_last_profile_refresh,
                    merged_last_change,
                    merged_created_at,
                    keep_id,
                ),
            )
            cur.execute("DELETE FROM app.people WHERE person_id = %s", (drop_id,))
    logging.info("  deleting app.people row person_id=%s", drop_id)
    stats.drop_person_deleted = True
    return stats


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Safely merge one person into another.")
    p.add_argument("--dsn", default=None, help="Optional DSN override.")
    p.add_argument("--keep-person-id", type=int, help="Canonical person_id to keep.")
    p.add_argument("--drop-person-id", type=int, help="Duplicate person_id to merge away.")
    p.add_argument(
        "--keep-orcid",
        action="append",
        default=None,
        help="ORCID to keep on the canonical person after merge. Repeat to keep multiple ORCIDs.",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Apply changes. Without this flag the script is a dry-run.",
    )
    p.add_argument("--debug", action="store_true", help="Verbose logging.")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    keep_id, drop_id = _resolve_person_ids(args.keep_person_id, args.drop_person_id)
    dry_run = not args.execute
    with db_conn(args.dsn) as conn:
        conn.autocommit = False
        try:
            keep_orcids = _choose_keep_orcids(conn, keep_id, drop_id, args.keep_orcid)
            logging.info(
                "Selected ORCID(s) to keep on person_id=%s: %s",
                keep_id,
                ", ".join(keep_orcids) if keep_orcids else "(none)",
            )
            stats = run_merge_people(
                conn,
                keep_id,
                drop_id,
                dry_run=dry_run,
                keep_orcids=keep_orcids,
            )
            if dry_run:
                conn.rollback()
                logging.info("Dry-run complete; rolled back.")
            else:
                conn.commit()
                logging.info("Merge committed.")
        except Exception:
            conn.rollback()
            raise

    logging.info(
        "merge_people summary: keep=%s drop=%s dry_run=%s authorships_moved=%s "
        "employments_moved=%s orcid_rows_moved=%s orcid_rows_deleted=%s person_keys_inserted=%s "
        "alias_rows_inserted=%s alias_rows_updated=%s source_refresh_rows_moved=%s "
        "source_refresh_rows_merged=%s person_refresh_rows_moved=%s "
        "person_refresh_rows_merged=%s pii_rows_merged=%s drop_person_deleted=%s",
        keep_id,
        drop_id,
        dry_run,
        stats.authorships_moved,
        stats.employments_moved,
        stats.orcid_rows_moved,
        stats.orcid_rows_deleted,
        stats.person_keys_inserted,
        stats.alias_rows_inserted,
        stats.alias_rows_updated,
        stats.source_refresh_rows_moved,
        stats.source_refresh_rows_merged,
        stats.person_refresh_rows_moved,
        stats.person_refresh_rows_merged,
        stats.pii_rows_merged,
        stats.drop_person_deleted,
    )
    for note in stats.notes:
        logging.info("note: %s", note)


if __name__ == "__main__":
    main()
