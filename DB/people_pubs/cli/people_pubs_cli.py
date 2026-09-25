#!/usr/bin/env python3
"""
people_pubs/cli/people_pubs_cli.py

Thin convenience CLI that wraps the existing standalone scripts:

    - update_orcid_db.py
    - update_publications_from_orcid.py
    - backfill_publications_from_crossref.py

This lets you run:

    python -m people_pubs.cli.people_pubs_cli orcid:profiles ...
    python -m people_pubs.cli.people_pubs_cli orcid:works ...
    python -m people_pubs.cli.people_pubs_cli crossref:backfill ...

while still reusing the mature logic already implemented in those scripts.

Once the refactor is complete, the sub-commands can be switched to call
people_pubs.sync.* modules instead of spawning subprocesses.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import List


def _run(cmd: List[str]) -> int:
    """Run a child process, streaming output, and return its exit code."""
    proc = subprocess.run(cmd)
    return proc.returncode


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="people-pubs",
        description="Unified CLI for people/publications maintenance tasks.",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    # ------------------------------------------------------------------
    # ORCID profiles (update_orcid_db.py)
    # ------------------------------------------------------------------
    p_profiles = sub.add_parser(
        "orcid:profiles",
        help="Refresh ORCID profiles (emails + employments) into people_db.",
    )
    p_profiles.add_argument(
        "--dsn",
        help="PostgreSQL DSN (optional; if omitted, use PG* environment vars).",
    )
    p_profiles.add_argument(
        "--max-age-days",
        type=int,
        default=180,
        help=(
            "Only refresh people whose last_orcid_profile_at is older than this "
            "many days (or NULL). Ignored if --only-person-id/--only-orcid is given."
        ),
    )
    p_profiles.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of people to refresh in one run.",
    )
    p_profiles.add_argument(
        "--only-person-id",
        type=int,
        default=None,
        help="If set, refresh only this person_id (ignores max-age-days).",
    )
    p_profiles.add_argument(
        "--only-orcid",
        default=None,
        help="If set, refresh only this ORCID (ignores max-age-days).",
    )
    p_profiles.add_argument(
        "--org-match",
        default="",
        help=(
            "Semicolon- or comma-separated patterns for your org(s); "
            "passed through to fetch_orcid_profile_enrichment. Defaults to "
            "PEOPLE_PUBS_ORG_PATTERNS; with neither set, no employment is matched."
        ),
    )
    p_profiles.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write anything to the DB; just log what would happen.",
    )
    p_profiles.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging.",
    )

    # ------------------------------------------------------------------
    # ORCID works (update_publications_from_orcid.py)
    # ------------------------------------------------------------------
    p_works = sub.add_parser(
        "orcid:works",
        help="Fetch ORCID works for people and update biblio.publications/authorships.",
    )
    p_works.add_argument(
        "--dsn",
        help="PostgreSQL DSN (optional; if omitted, use PG* environment vars).",
    )
    p_works.add_argument(
        "--since",
        default="2019-01-01",
        help=(
            "Only keep publications with pub_date >= this date "
            "(YYYY or YYYY-MM or YYYY-MM-DD). Default: 2019-01-01"
        ),
    )
    p_works.add_argument(
        "--limit-people",
        type=int,
        default=None,
        help="Maximum number of people to process (for testing).",
    )
    p_works.add_argument(
        "--refreshed-before",
        default=None,
        help=(
            "Only process people whose ORCID publications were last refreshed "
            "before this date (YYYY, YYYY-MM, or YYYY-MM-DD) or never. "
            "Ignored if --only-person-id/--only-orcid is given."
        ),
    )
    p_works.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help=(
            "Alternative to --refreshed-before: only process people whose ORCID "
            "publications were last refreshed more than N days ago or never. "
            "Ignored if --only-person-id/--only-orcid is given."
        ),
    )
    p_works.add_argument(
        "--only-person-id",
        type=int,
        default=None,
        help="If set, only process this person_id.",
    )
    p_works.add_argument(
        "--only-orcid",
        default=None,
        help="If set, only process this ORCID.",
    )
    p_works.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write anything to the DB; just log what would happen.",
    )
    p_works.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging.",
    )

    # ------------------------------------------------------------------
    # Crossref backfill (backfill_publications_from_crossref.py)
    # ------------------------------------------------------------------
    p_backfill = sub.add_parser(
        "crossref:backfill",
        help="Backfill missing publication metadata from Crossref.",
    )
    p_backfill.add_argument(
        "--dsn",
        help="PostgreSQL DSN (optional; if omitted, use PG* environment vars).",
    )
    p_backfill.add_argument(
        "--mailto",
        help=(
            "Contact email for Crossref User-Agent; overrides the default "
            "configured in people_pubs.config.CROSSREF_MAILTO."
        ),
    )
    p_backfill.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of publications to backfill in one run.",
    )
    p_backfill.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging.",
    )

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    # Map sub-commands to the legacy scripts.
    cmd: list[str]

    if args.command == "orcid:profiles":
        cmd = [sys.executable, "-m", "people_pubs.sync.orcid_profiles"]
        if args.dsn:
            cmd += ["--dsn", args.dsn]
        if args.max_age_days is not None:
            cmd += ["--max-age-days", str(args.max_age_days)]
        if args.limit is not None:
            cmd += ["--limit", str(args.limit)]
        if args.only_person_id is not None:
            cmd += ["--only-person-id", str(args.only_person_id)]
        if args.only_orcid is not None:
            cmd += ["--only-orcid", args.only_orcid]
        if args.org_match:
            cmd += ["--org-match", args.org_match]
        if args.dry_run:
            cmd += ["--dry-run"]
        if args.debug:
            cmd += ["--debug"]

    elif args.command == "orcid:works":
        cmd = [sys.executable, "-m", "people_pubs.sync.orcid_works"]
        if args.dsn:
            cmd += ["--dsn", args.dsn]
        if args.since:
            cmd += ["--since", args.since]
        if args.limit_people is not None:
            cmd += ["--limit-people", str(args.limit_people)]
        if args.refreshed_before:
            cmd += ["--refreshed-before", args.refreshed_before]
        if args.max_age_days is not None:
            cmd += ["--max-age-days", str(args.max_age_days)]
        if args.only_person_id is not None:
            cmd += ["--only-person-id", str(args.only_person_id)]
        if args.only_orcid is not None:
            cmd += ["--only-orcid", args.only_orcid]
        if args.dry_run:
            cmd += ["--dry-run"]
        if args.debug:
            cmd += ["--debug"]

    elif args.command == "crossref:backfill":
        cmd = [sys.executable, "-m", "people_pubs.sync.crossref_backfill"]
        if args.dsn:
            cmd += ["--dsn", args.dsn]
        if args.mailto:
            cmd += ["--mailto", args.mailto]
        if args.limit is not None:
            cmd += ["--max", str(args.limit)]
        if args.debug:
            cmd += ["--debug"]

    else:
        ap.error(f"Unknown command {args.command!r}")
        return 2

    return _run(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
