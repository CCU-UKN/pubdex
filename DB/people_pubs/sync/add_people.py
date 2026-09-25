"""
people_pubs.sync.add_people

Add or update people in app.people + app.identities_orcid (ORCID required).
Optionally ensures pii.people_pii exists and stores ORCID in app.person_keys.
Optionally inserts one employment row into app.employments.
By default, fetches ORCID profile names to populate orcid_* fields
(use --no-orcid-lookup to skip).

Use cases:
  - Seed internal people so ORCID profile/work syncs can run.
  - Bulk import a cohort from CSV.
  - Update existing records by ORCID with --update-existing.

Examples:
  python -m people_pubs.sync.add_people \
    --orcid 0000-0002-1825-0097 \
    --display-name "Jane Doe" \
    --person-kind internal \
    --primary-email jane@example.org \
    --employment-org-name "Example University" \
    --employment-start-date 2024-10-01 \
    --employment-role-title "Postdoc"

  python -m people_pubs.sync.add_people --csv people.csv --person-kind internal --debug

CSV columns:
  - orcid (required)
  - display_name, first_name, last_name
  - person_kind (internal|external)
  - primary_email
  - emails or email (comma/semicolon separated)
  - employment_org_name (required if providing employment)
  - employment_department
  - employment_role_title
  - employment_start_date
  - employment_end_date
  - employment_is_current (true/false)
  - employment_source (default: manual)
"""

# people_pubs/sync/add_people.py
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

import psycopg
from psycopg.rows import dict_row

from people_pubs.db.connection import db_conn
from people_pubs.db.people import (
    pick_primary_email,
    sync_orcid_names_for_person,
    upsert_employment,
)
from people_pubs.orcidkit import OrcidClient
from people_pubs.utils.identifiers import normalize_orcid, orcid_checksum_is_valid


_VALID_PERSON_KINDS = {"internal", "external"}


@dataclass
class AddPeopleStats:
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped_existing: int = 0
    skipped_invalid: int = 0
    errors: int = 0


def _normalize_orcid_value(orcid: Optional[str]) -> Optional[str]:
    return normalize_orcid(orcid)


def _normalize_person_kind(value: Optional[str], default: str) -> str:
    if value is None or not str(value).strip():
        return default
    v = str(value).strip().lower()
    if v not in _VALID_PERSON_KINDS:
        raise ValueError(f"Invalid person_kind {value!r}; expected internal|external")
    return v


def _parse_emails(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    out: List[str] = []
    for part in re.split(r"[;,]", str(raw)):
        val = part.strip()
        if val:
            out.append(val)
    return out


def _parse_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if not s:
        return None
    if s in {"1", "true", "t", "yes", "y"}:
        return True
    if s in {"0", "false", "f", "no", "n"}:
        return False
    return None


def _get_str(raw: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        val = raw.get(key)
        if val is None:
            continue
        s = str(val).strip()
        if s:
            return s
    return None


def _prepare_employment(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    org_name = _get_str(raw, "employment_org_name", "employment_org")
    department = _get_str(raw, "employment_department")
    role_title = _get_str(raw, "employment_role_title")
    start_date = _get_str(raw, "employment_start_date")
    end_date = _get_str(raw, "employment_end_date")
    source_raw = _get_str(raw, "employment_source")
    is_current_raw = raw.get("employment_is_current")
    is_current = _parse_bool(is_current_raw)

    fields_present = any(
        value is not None
        for value in [
            org_name,
            department,
            role_title,
            start_date,
            end_date,
            source_raw,
            is_current_raw,
        ]
    )
    if not fields_present:
        return None

    source = source_raw or "manual"

    return {
        "org_name": org_name,
        "department": department,
        "role_title": role_title,
        "start_date": start_date,
        "end_date": end_date,
        "is_current": is_current,
        "source": source,
    }


def _prepare_record(raw: Dict[str, Any], default_kind: str) -> Dict[str, Any]:
    orcid = _normalize_orcid_value(raw.get("orcid"))
    if not orcid:
        raise ValueError("Missing or invalid ORCID")
    if not orcid_checksum_is_valid(orcid):
        raise ValueError("ORCID checksum invalid (ISO 7064 MOD 11-2) — mistyped iD?")

    display_name = (raw.get("display_name") or "").strip()
    first_name = (raw.get("first_name") or "").strip() or None
    last_name = (raw.get("last_name") or "").strip() or None

    if not display_name:
        display_name = " ".join([p for p in [first_name, last_name] if p]).strip()
    if not display_name:
        raise ValueError("Missing display_name (or first_name/last_name)")

    person_kind = _normalize_person_kind(raw.get("person_kind"), default_kind)

    primary_email = (raw.get("primary_email") or "").strip() or None
    emails = _parse_emails(raw.get("emails")) or []
    if "email" in raw and raw.get("email"):
        emails.extend(_parse_emails(raw.get("email")))
    if primary_email and primary_email not in emails:
        emails.append(primary_email)

    employment = _prepare_employment(raw)

    return {
        "orcid": orcid,
        "display_name": display_name,
        "first_name": first_name,
        "last_name": last_name,
        "person_kind": person_kind,
        "primary_email": primary_email,
        "emails": emails,
        "employment": employment,
    }


def _lookup_by_orcid(conn: psycopg.Connection, orcid: str) -> Optional[Dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT p.person_id, p.display_name, p.first_name, p.last_name, p.person_kind
            FROM app.identities_orcid io
            JOIN app.people p ON p.person_id = io.person_id
            WHERE io.orcid = %s
            """,
            (orcid,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _insert_person(
    conn: psycopg.Connection, record: Dict[str, Any], dry_run: bool
) -> Optional[int]:
    if dry_run:
        logging.info(
            "  [dry-run] would insert person display_name=%r orcid=%s",
            record["display_name"],
            record["orcid"],
        )
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO app.people (person_kind, display_name, first_name, last_name)
            VALUES (%s, %s, %s, %s)
            RETURNING person_id
            """,
            (
                record["person_kind"],
                record["display_name"],
                record["first_name"],
                record["last_name"],
            ),
        )
        row = cur.fetchone()
        return row["person_id"]


def _ensure_orcid_identity(
    conn: psycopg.Connection, person_id: int, orcid: str, dry_run: bool
) -> None:
    if dry_run:
        logging.info(
            "  [dry-run] would insert identities_orcid person_id=%s orcid=%s",
            person_id,
            orcid,
        )
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.identities_orcid (person_id, orcid)
            VALUES (%s, %s)
            ON CONFLICT (orcid) DO NOTHING
            """,
            (person_id, orcid),
        )


def _ensure_orcid_key(
    conn: psycopg.Connection, person_id: int, orcid: str, dry_run: bool
) -> None:
    if dry_run:
        logging.info(
            "  [dry-run] would insert person_keys orcid person_id=%s orcid=%s",
            person_id,
            orcid,
        )
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.person_keys (person_id, key_type, key_value, is_verified, source)
            VALUES (%s, 'orcid', %s, true, 'manual')
            ON CONFLICT (key_type, key_value) DO NOTHING
            """,
            (person_id, orcid),
        )


def _ensure_people_pii(
    conn: psycopg.Connection,
    person_id: int,
    primary_email: Optional[str],
    emails: List[str],
    update_existing: bool,
    dry_run: bool,
) -> None:
    incoming_emails = [e for e in emails if e]
    if primary_email and primary_email not in incoming_emails:
        incoming_emails.append(primary_email)

    if dry_run:
        chosen_primary = pick_primary_email(primary_email, incoming_emails) if incoming_emails else primary_email
        logging.info(
            "  [dry-run] would ensure pii.people_pii person_id=%s emails=%r primary=%r update_existing=%s",
            person_id,
            incoming_emails or None,
            chosen_primary,
            update_existing,
        )
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT action, primary_email, emails
            FROM pii.ensure_people_pii(%s, %s::citext, %s::citext[], %s)
            """,
            (person_id, primary_email, incoming_emails, update_existing),
        )
        row = cur.fetchone()
    if row:
        action = row.get("action") if isinstance(row, dict) else row[0]
        logging.debug("  pii.people_pii ensure action=%s person_id=%s", action, person_id)


def _ensure_employment(
    conn: psycopg.Connection,
    person_id: int,
    employment: Optional[Dict[str, Any]],
    dry_run: bool,
) -> None:
    if not employment:
        return

    org_name = employment.get("org_name")
    if not org_name:
        logging.warning(
            "Skipping employment for person_id=%s: missing employment_org_name",
            person_id,
        )
        return

    if dry_run:
        logging.info(
            "  [dry-run] would upsert employment person_id=%s org=%r start_date=%r",
            person_id,
            org_name,
            employment.get("start_date"),
        )
        return

    upsert_employment(conn, person_id, employment)


def _needs_orcid_name_sync(conn: psycopg.Connection, person_id: int) -> bool:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT orcid_first_name, orcid_last_name, orcid_display_name
            FROM app.identities_orcid
            WHERE person_id = %s
            """,
            (person_id,),
        )
        row = cur.fetchone()

    if not row:
        return True

    first = (row.get("orcid_first_name") or "").strip()
    last = (row.get("orcid_last_name") or "").strip()
    display = (row.get("orcid_display_name") or "").strip()
    return not (first and last and display)


def _maybe_sync_orcid_names(
    conn: psycopg.Connection,
    oc: Optional[OrcidClient],
    person_id: int,
    orcid: str,
    dry_run: bool,
) -> None:
    if dry_run or oc is None:
        return
    if not _needs_orcid_name_sync(conn, person_id):
        return
    sync_orcid_names_for_person(conn, oc, person_id, orcid, dry_run=dry_run)


def _update_person(
    conn: psycopg.Connection,
    person_id: int,
    record: Dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        logging.info(
            "  [dry-run] would update person_id=%s display_name=%r first_name=%r last_name=%r kind=%r",
            person_id,
            record.get("display_name"),
            record.get("first_name"),
            record.get("last_name"),
            record.get("person_kind"),
        )
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.people
            SET display_name = COALESCE(%s, display_name),
                first_name = COALESCE(%s, first_name),
                last_name = COALESCE(%s, last_name),
                person_kind = COALESCE(%s, person_kind),
                updated_at = now()
            WHERE person_id = %s
            """,
            (
                record.get("display_name"),
                record.get("first_name"),
                record.get("last_name"),
                record.get("person_kind"),
                person_id,
            ),
        )


def _iter_csv_records(path: str, delimiter: str) -> Iterable[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            yield row


def _ensure_person(
    conn: psycopg.Connection,
    record: Dict[str, Any],
    update_existing: bool,
    dry_run: bool,
    stats: AddPeopleStats,
    oc: Optional[OrcidClient],
) -> None:
    stats.total += 1
    orcid = record["orcid"]
    existing = _lookup_by_orcid(conn, orcid)

    # Per-record savepoint: the whole run shares one transaction, so a plain
    # rollback here would discard *earlier* successful rows while their
    # inserted/updated counters survive into the end-of-run summary. Rolling
    # back to a savepoint discards only this record's writes and recovers the
    # aborted transaction state a PostgreSQL error leaves behind.
    use_savepoint = not dry_run
    if use_savepoint:
        conn.execute("SAVEPOINT add_people_record")
    try:
        if existing:
            if not update_existing:
                stats.skipped_existing += 1
                logging.info("Skipping existing ORCID %s (person_id=%s)", orcid, existing["person_id"])
                return
            _update_person(conn, existing["person_id"], record, dry_run=dry_run)
            _ensure_orcid_identity(conn, existing["person_id"], orcid, dry_run=dry_run)
            _ensure_orcid_key(conn, existing["person_id"], orcid, dry_run=dry_run)
            _ensure_people_pii(
                conn,
                existing["person_id"],
                record.get("primary_email"),
                record.get("emails") or [],
                update_existing=True,
                dry_run=dry_run,
            )
            _ensure_employment(
                conn,
                existing["person_id"],
                record.get("employment"),
                dry_run=dry_run,
            )
            _maybe_sync_orcid_names(
                conn,
                oc,
                existing["person_id"],
                orcid,
                dry_run=dry_run,
            )
            stats.updated += 1
            return

        person_id = _insert_person(conn, record, dry_run=dry_run)
        if person_id is None and dry_run:
            stats.inserted += 1
            return

        if person_id is None:
            raise RuntimeError("Insert failed; no person_id returned")

        _ensure_orcid_identity(conn, person_id, orcid, dry_run=dry_run)
        _ensure_orcid_key(conn, person_id, orcid, dry_run=dry_run)
        _ensure_people_pii(
            conn,
            person_id,
            record.get("primary_email"),
            record.get("emails") or [],
            update_existing=False,
            dry_run=dry_run,
        )
        _ensure_employment(
            conn,
            person_id,
            record.get("employment"),
            dry_run=dry_run,
        )
        _maybe_sync_orcid_names(
            conn,
            oc,
            person_id,
            orcid,
            dry_run=dry_run,
        )
        logging.info(
            "Added person_id=%s orcid=%s display_name=%r",
            person_id,
            orcid,
            record.get("display_name"),
        )
        stats.inserted += 1
    except Exception:
        stats.errors += 1
        logging.exception("Failed to add person for ORCID %s", orcid)
        if use_savepoint:
            conn.execute("ROLLBACK TO SAVEPOINT add_people_record")
    finally:
        if use_savepoint:
            conn.execute("RELEASE SAVEPOINT add_people_record")


def run_add_people(
    *,
    dsn: Optional[str],
    records: Iterable[Dict[str, Any]],
    default_person_kind: str,
    update_existing: bool,
    dry_run: bool,
    lookup_orcid: bool,
) -> AddPeopleStats:
    stats = AddPeopleStats()
    ctx = OrcidClient() if lookup_orcid and not dry_run else None
    if ctx is None:
        with db_conn(dsn) as conn:
            for raw in records:
                try:
                    record = _prepare_record(raw, default_person_kind)
                except Exception as exc:
                    stats.skipped_invalid += 1
                    logging.warning("Skipping invalid row: %s (row=%r)", exc, raw)
                    continue
                _ensure_person(
                    conn,
                    record,
                    update_existing=update_existing,
                    dry_run=dry_run,
                    stats=stats,
                    oc=None,
                )

            if not dry_run:
                conn.commit()
        return stats

    with db_conn(dsn) as conn, ctx as oc:
        for raw in records:
            try:
                record = _prepare_record(raw, default_person_kind)
            except Exception as exc:
                stats.skipped_invalid += 1
                logging.warning("Skipping invalid row: %s (row=%r)", exc, raw)
                continue
            _ensure_person(
                conn,
                record,
                update_existing=update_existing,
                dry_run=dry_run,
                stats=stats,
                oc=oc,
            )

        if not dry_run:
            conn.commit()

    return stats


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Add people to app.people + app.identities_orcid (ORCID required).",
    )
    ap.add_argument(
        "--dsn",
        help=(
            "Optional PostgreSQL DSN (otherwise use PEOPLE_DB_DSN or PG* env vars). "
            "Example: postgresql://postgres:<password>@127.0.0.1:5432/people_db"
        ),
    )
    ap.add_argument("--csv", help="Path to CSV with people (requires an orcid column).")
    ap.add_argument(
        "--delimiter",
        default=",",
        help="CSV delimiter (default: ',').",
    )
    ap.add_argument("--orcid", help="ORCID for a single person.")
    ap.add_argument("--display-name", help="Display name for a single person.")
    ap.add_argument("--first-name", help="First name for a single person.")
    ap.add_argument("--last-name", help="Last name for a single person.")
    ap.add_argument(
        "--person-kind",
        default="internal",
        choices=sorted(_VALID_PERSON_KINDS),
        help="Person kind for new rows (default: internal).",
    )
    ap.add_argument(
        "--primary-email",
        help="Primary email for a single person.",
    )
    ap.add_argument(
        "--email",
        action="append",
        default=[],
        help="Additional email(s) for a single person (repeatable).",
    )
    ap.add_argument(
        "--employment-org-name",
        help="Employment org_name for app.employments (optional).",
    )
    ap.add_argument(
        "--employment-department",
        help="Employment department for app.employments (optional).",
    )
    ap.add_argument(
        "--employment-role-title",
        help="Employment role_title for app.employments (optional).",
    )
    ap.add_argument(
        "--employment-start-date",
        help="Employment start date (YYYY or YYYY-MM or YYYY-MM-DD).",
    )
    ap.add_argument(
        "--employment-end-date",
        help="Employment end date (YYYY or YYYY-MM or YYYY-MM-DD).",
    )
    ap.add_argument(
        "--employment-current",
        dest="employment_is_current",
        action="store_true",
        default=None,
        help="Mark employment as current.",
    )
    ap.add_argument(
        "--employment-source",
        default=None,
        help="Employment source label (default: manual when employment is provided).",
    )
    ap.add_argument(
        "--update-existing",
        action="store_true",
        help="If ORCID already exists, update names/emails instead of skipping.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write anything to the DB; just log what would happen.",
    )
    ap.add_argument(
        "--no-orcid-lookup",
        action="store_true",
        help="Skip live ORCID lookup for name fields after insert/update.",
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
        format="%(message)s",
    )

    if args.csv and args.orcid:
        raise SystemExit("Use either --csv or --orcid (not both).")

    if args.csv:
        if not os.path.exists(args.csv):
            raise SystemExit(f"CSV not found: {args.csv}")
        records = _iter_csv_records(args.csv, args.delimiter)
    else:
        if not args.orcid:
            raise SystemExit("Missing --orcid for single-person insert.")
        records = [
            {
                "orcid": args.orcid,
                "display_name": args.display_name,
                "first_name": args.first_name,
                "last_name": args.last_name,
                "person_kind": args.person_kind,
                "primary_email": args.primary_email,
                "emails": args.email,
                "employment_org_name": args.employment_org_name,
                "employment_department": args.employment_department,
                "employment_role_title": args.employment_role_title,
                "employment_start_date": args.employment_start_date,
                "employment_end_date": args.employment_end_date,
                "employment_is_current": args.employment_is_current,
                "employment_source": args.employment_source,
            }
        ]

    stats = run_add_people(
        dsn=args.dsn,
        records=records,
        default_person_kind=args.person_kind,
        update_existing=args.update_existing,
        dry_run=args.dry_run,
        lookup_orcid=not args.no_orcid_lookup,
    )

    logging.info(
        "add_people: total=%s inserted=%s updated=%s skipped_existing=%s skipped_invalid=%s errors=%s",
        stats.total,
        stats.inserted,
        stats.updated,
        stats.skipped_existing,
        stats.skipped_invalid,
        stats.errors,
    )


if __name__ == "__main__":
    main()
