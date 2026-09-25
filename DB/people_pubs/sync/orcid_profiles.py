"""
people_pubs.sync.orcid_profiles

Refresh ORCID profile metadata (names, emails, employments) into the DB.

Example:
  python -m people_pubs.sync.orcid_profiles --max-age-months 3 --limit 20 --debug
"""

# people_pubs/sync/orcid_profiles.py
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

import psycopg
from psycopg.rows import dict_row  # noqa: F401

from ..orcidkit import OrcidClient, fetch_orcid_profile_enrichment

from people_pubs.config import DEFAULT_ORCID_ORG_PATTERNS
from people_pubs.db.connection import db_conn
from people_pubs.db.people import (
    load_people_due_for_orcid_profile_refresh,
    sync_orcid_names_for_person,
    update_people_pii_with_verified_emails,
    upsert_employment,
    update_orcid_profile_refresh_state,
)


def _parse_orcid_last_updated_date(value: Any) -> Optional[dt.date]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # ORCID may return date-only or full ISO timestamp.
        try:
            if "T" not in s and " " not in s:
                return dt.date.fromisoformat(s)
        except Exception:
            pass
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except Exception:
            return None
    return None


def _update_orcid_last_updated_date(
    conn: psycopg.Connection,
    person_id: int,
    orcid_last_updated: Optional[dt.date],
) -> None:
    if orcid_last_updated is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.identities_orcid
               SET orcid_last_updated = CASE
                     WHEN orcid_last_updated IS NULL OR orcid_last_updated < %s
                       THEN %s
                     ELSE orcid_last_updated
                   END,
                   updated_at = now()
             WHERE person_id = %s
            """,
            (orcid_last_updated, orcid_last_updated, person_id),
        )


def refresh_orcid_profile_for_person(
    conn: psycopg.Connection,
    oc: OrcidClient,
    row: Dict[str, Any],
    org_patterns: Sequence[str],
    dry_run: bool = False,
) -> None:
    """
    Refresh one person:
    - fetch ORCID profile + employments
    - merge verified emails into pii.people_pii
    - upsert employments into app.employments
    - update activity.person_refresh_state
    """
    person_id = row["person_id"]
    display_name = row.get("display_name") or ""
    orcid = row["orcid"]

    logging.info("Refreshing person_id=%s orcid=%s (%s)", person_id, orcid, display_name)

    # Keep ORCID name fields in app.identities_orcid in sync for this person
    sync_orcid_names_for_person(conn, oc, person_id, orcid, dry_run=dry_run)

    prof = fetch_orcid_profile_enrichment(
        oc,
        orcid,
        display_name_for_scoring=display_name,
        org_patterns=list(org_patterns),
    )

    verified_emails = [e.strip() for e in (prof.get("verified_emails") or []) if e]
    matched_emps = prof.get("matched_employments") or []
    orcid_last_updated = prof.get("last_updated")  # ORCID-side date, mostly for inspection
    orcid_last_updated_date = _parse_orcid_last_updated_date(orcid_last_updated)

    logging.debug(
        "  ORCID last_updated=%r, verified_emails=%r, matched_employments=%d",
        orcid_last_updated,
        verified_emails,
        len(matched_emps),
    )

    if dry_run:
        logging.info(
            "  [dry-run] would merge %d verified emails and %d employments",
            len(verified_emails),
            len(matched_emps),
        )
        # names already logged above; skip email/employment writes
        return

    # 1) Merge verified emails into pii.people_pii
    update_people_pii_with_verified_emails(conn, person_id, verified_emails)

    # 2) Upsert employments into app.employments
    for emp in matched_emps:
        upsert_employment(conn, person_id, emp)

    # 3) Update activity.person_refresh_state
    update_orcid_profile_refresh_state(conn, person_id)

    # 4) Persist ORCID-side last_updated date for automation state propagation.
    _update_orcid_last_updated_date(conn, person_id, orcid_last_updated_date)

    conn.commit()


def run_orcid_profile_sync(
    *,
    dsn: Optional[str],
    max_age_days: Optional[int],
    max_age_months: Optional[int],
    limit: int,
    only_person_id: Optional[int],
    only_orcid: Optional[str],
    org_patterns: Sequence[str],
    dry_run: bool,
) -> None:
    """
    Core orchestration for ORCID profile refresh.

    This is the central entry point that other code should call instead of
    re-implementing the same loop.
    """
    pattern_list: List[str] = [p for p in org_patterns if p]

    with db_conn(dsn) as conn, OrcidClient() as oc:
        due = load_people_due_for_orcid_profile_refresh(
            conn,
            max_age_days=max_age_days,
            max_age_months=max_age_months,
            limit=limit,
            only_person_id=only_person_id,
            only_orcid=only_orcid,
        )

        if not due:
            if max_age_months is not None:
                logging.info("No people due for ORCID refresh (max_age_months=%d).", max_age_months)
            else:
                logging.info("No people due for ORCID refresh (max_age_days=%s).", max_age_days)
            logging.info(
                "orcid_profiles summary: processed=0 succeeded=0 skipped=0 errors=0 dry_run=%s",
                dry_run,
            )
            return

        logging.info("Refreshing %d people...", len(due))
        processed = 0
        succeeded = 0
        errors = 0
        for row in due:
            processed += 1
            try:
                refresh_orcid_profile_for_person(conn, oc, row, pattern_list, dry_run=dry_run)
                succeeded += 1
            except Exception as e:  # defensive logging
                # If one person fails, the transaction is in an aborted state.
                # Roll it back so we can continue with the next person_id.
                conn.rollback()
                errors += 1
                logging.exception(
                    "Error refreshing person_id=%s orcid=%s: %s",
                    row.get("person_id"),
                    row.get("orcid"),
                    e,
                )
        logging.info(
            "orcid_profiles summary: processed=%d succeeded=%d skipped=0 errors=%d dry_run=%s",
            processed,
            succeeded,
            errors,
            dry_run,
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Refresh ORCID profile/employment + emails into people_db.",
    )
    ap.add_argument(
        "--dsn",
        help=(
            "Optional PostgreSQL DSN (otherwise use PEOPLE_DB_DSN or PG* env vars). "
            "Example: postgresql://postgres:<password>@127.0.0.1:5432/people_db"
        ),
    )
    age_group = ap.add_mutually_exclusive_group()
    age_group.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help=(
            "Only refresh people whose last_orcid_profile_at is older than this "
            "many days (or NULL). Ignored if --only-person-id/--only-orcid is given."
        ),
    )
    age_group.add_argument(
        "--max-age-months",
        type=int,
        default=3,
        help=(
            "Only refresh people whose last_orcid_profile_at is older than this "
            "many months (or NULL). Default: 3. Ignored if --only-person-id/--only-orcid is given."
        ),
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of people to refresh in one run.",
    )
    ap.add_argument(
        "--only-person-id",
        type=int,
        default=None,
        help="If set, refresh only this person_id (ignores max-age-days/max-age-months).",
    )
    ap.add_argument(
        "--only-orcid",
        default=None,
        help="If set, refresh only this ORCID (ignores max-age-days/max-age-months).",
    )
    ap.add_argument(
        "--org-match",
        default="; ".join(DEFAULT_ORCID_ORG_PATTERNS),
        help=(
            "Semicolon- or comma-separated patterns for your org(s), "
            "passed through to fetch_orcid_profile_enrichment."
        ),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write anything to the DB; just log what would happen.",
    )
    ap.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging.",
    )
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)

    if args.max_age_days is not None and args.max_age_days <= 0:
        raise SystemExit("--max-age-days must be > 0")
    if args.max_age_months is not None and args.max_age_months <= 0:
        raise SystemExit("--max-age-months must be > 0")
    if args.limit <= 0:
        raise SystemExit("--limit must be > 0")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    patterns = [p.strip() for p in re.split(r"[;,]", args.org_match) if p.strip()]

    run_orcid_profile_sync(
        dsn=args.dsn,
        max_age_days=args.max_age_days,
        max_age_months=args.max_age_months,
        limit=args.limit,
        only_person_id=args.only_person_id,
        only_orcid=args.only_orcid,
        org_patterns=patterns,
        dry_run=args.dry_run,
    )

if __name__ == "__main__":
    main()
