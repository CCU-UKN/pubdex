#!/usr/bin/env python3
"""
people_pubs.sync.attribution_rules

The one operation for institutional attribution: change the rules, recompute
the cached flags, and refresh the canonical snapshots — in that order.

Which publications belong to your institution is installation-specific, so the
rule is data rather than code. `app.institution_attribution_rules` holds the
affiliation terms and funding identifiers to look for. It ships EMPTY, and
while it is empty nothing is attributed automatically: publications are still
ingested and stored, but `biblio.publications_canon` will contain only the rows
a curator explicitly included.

Matching contract (also stated on the table in DB/init/010_...sql):
- Evidence is the publication's stored provider payloads, i.e. the text of
  `biblio.publications.raw_json`. `source_of_truth` is NOT evidence: tokens
  like `crossref-funding` record how a record was discovered, not whose it is.
- A rule matches when its pattern occurs as a case-insensitive SUBSTRING of
  that payload text. `affiliation` and `funding` rules match identically; the
  kind only keeps the two lists manageable.
- Patterns are literal: `%` and regex metacharacters are not special.
- A publication is attributed when at least one active rule matches, so an
  empty rule set attributes nothing. Blank patterns cannot be stored.
- Patterns must be at least two characters. Short or common patterns
  over-match; prefer a distinctive acronym, the full centre name, or a grant
  number.

Attribution is stored per publication row; canon membership is per duplicate
group. `biblio.publications_canon` attributes a group when ANY member is
attributed, so evidence on one version of a paper covers the whole group.

Why one command: `biblio.publications.institution_attributed` is a cache of
`biblio.institution_attribution_signal(raw_json)`. Changing the rules without
recomputing would leave that cache — and the canon snapshots built from it —
describing the previous rules.

Commands and options:
  list        show the configured rules; read-only, takes no other option
  set         replace the whole rule set with the given --affiliation/--funding
  add         add the given rules to the existing set (re-activates duplicates)
  remove      remove the given rules (--note is not accepted)
  recompute   re-sync the cached flags and refresh the snapshots without
              touching the rules; takes no --affiliation/--funding/--note
  --dry-run       run the rule change and recompute inside a transaction that
                  is rolled back; nothing is written and no snapshot is
                  refreshed. Not accepted with --refresh-only.
  --no-refresh    commit rules and flags but leave the snapshots stale.
  --refresh-only  only refresh the snapshots (the retry after a failed
                  refresh). Accepted with `recompute` alone, and not together
                  with --dry-run or --no-refresh.
Incompatible combinations are rejected before any connection is opened.

Transaction boundaries and coordination:
- Transaction 1 — the rule change and the flag recompute. Both run under the
  exclusive attribution lock that the database takes itself: a statement
  trigger on the rules table and `biblio.recompute_institution_attribution()`
  acquire it, and the trigger that computes the flag on every publication
  write holds it shared until that writer commits. The command therefore
  waits for in-flight ingestion transactions to finish, recomputes under a
  snapshot that includes everything they committed, and writers that arrive
  meanwhile wait for the commit and then evaluate the new rules. Two rule
  commands serialise the same way. If either step fails, both roll back. On
  the rare deadlock with a writer that already held row locks the command
  retries the transaction a few times.
- Transaction 2 — the snapshot refresh. `publication_dedup_map_web` and
  `publications_canon_web` are refreshed inside ONE transaction, under the
  same advisory lock as DB/refresh_publications_canon_web.sh, so readers
  switch from the old pair to the new pair atomically. It is a separate
  transaction so the attribution lock is not held for the seconds a snapshot
  rebuild takes on a large database.
- If the refresh fails, both snapshots keep their previous state and the
  committed rules and flags are consistent; only the served snapshots are
  stale. Re-run `recompute --refresh-only`, or
  `DB/refresh_publications_canon_web.sh`, until it succeeds. A refresh is
  idempotent and safe to retry.

Examples:
  # Show the current rules
  python -m people_pubs.sync.attribution_rules list

  # Replace the whole rule set, recompute flags, refresh snapshots
  python -m people_pubs.sync.attribution_rules set \
    --affiliation "Example Institute" \
    --affiliation "Institute for Example Studies" \
    --funding "EX-12345678"

  # Add one funding identifier to the existing rules
  python -m people_pubs.sync.attribution_rules add --funding "EX-99999999"

  # Remove one term
  python -m people_pubs.sync.attribution_rules remove --affiliation "Example Institute"

  # Preview a change without writing anything (no snapshot refresh either)
  python -m people_pubs.sync.attribution_rules set --affiliation "Example Institute" --dry-run

  # Re-sync flags and snapshots without changing rules (e.g. after a restore)
  python -m people_pubs.sync.attribution_rules recompute

  # Retry only the snapshot refresh after it failed
  python -m people_pubs.sync.attribution_rules recompute --refresh-only
"""

from __future__ import annotations

import argparse
import logging
from typing import List, Optional, Sequence, Tuple

import psycopg
from psycopg import IsolationLevel
from psycopg.rows import dict_row

from people_pubs.config import DEFAULT_DSN
from people_pubs.db.connection import db_conn

LOGGER = logging.getLogger("people_pubs.attribution_rules")

MIN_PATTERN_LENGTH = 2
RULE_KINDS = ("affiliation", "funding")
COMMANDS = ("list", "set", "add", "remove", "recompute")
RULE_COMMANDS = ("set", "add", "remove")
# Attempts for transaction 1 when PostgreSQL resolves a deadlock against a
# concurrent writer by aborting this side.
TRANSACTION_ATTEMPTS = 3

# Both run inside the rule-change transaction; the lock they take is the one
# the publication-write trigger holds shared (DB/init/010_...sql).
LOCK_SQL = "SELECT biblio.institution_attribution_lock(true)"
RECOMPUTE_SQL = "SELECT biblio.recompute_institution_attribution() AS changed"

# Both run inside one transaction, under the lock shared with
# DB/refresh_publications_canon_web.sh.
REFRESH_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('biblio.publications_canon_web.refresh'))"
REFRESH_SQL = (
    "REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publication_dedup_map_web",
    "REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publications_canon_web",
)


def check_options(
    *,
    mode: str,
    affiliations: Sequence[str],
    fundings: Sequence[str],
    note: Optional[str],
    dry_run: bool,
    refresh: bool,
    refresh_only: bool,
) -> Optional[str]:
    """Return the reason a command/option combination is invalid, else None.

    Shared by the argument parser and run(), so a combination is refused the
    same way whether it comes from the command line or from a caller.
    """
    if mode not in COMMANDS:
        return f"unknown command {mode!r}"
    has_rules = bool(affiliations or fundings)
    if mode in RULE_COMMANDS and not has_rules:
        return f"{mode} needs at least one --affiliation or --funding"
    if mode not in RULE_COMMANDS and has_rules:
        return f"{mode} takes no --affiliation/--funding; it never changes the rules"
    if mode not in ("set", "add") and note is not None:
        return f"{mode} takes no --note; notes are stored with added rules only"
    if mode == "list" and (dry_run or not refresh or refresh_only):
        return "list is read-only and takes no --dry-run/--no-refresh/--refresh-only"
    if refresh_only and mode != "recompute":
        return "--refresh-only is only meaningful with recompute"
    if refresh_only and dry_run:
        return "--refresh-only cannot be combined with --dry-run: a dry run never refreshes snapshots"
    if refresh_only and not refresh:
        return "--refresh-only cannot be combined with --no-refresh"
    return None


def _validate(patterns: Sequence[str], kind: str) -> List[str]:
    cleaned: List[str] = []
    for raw in patterns:
        pattern = (raw or "").strip()
        if len(pattern) < MIN_PATTERN_LENGTH:
            raise SystemExit(
                f"{kind} pattern {raw!r} is shorter than {MIN_PATTERN_LENGTH} characters. "
                "An empty or one-character pattern would match almost everything."
            )
        cleaned.append(pattern)
    return cleaned


def _fetch_rules(conn: psycopg.Connection) -> List[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT rule_id, rule_kind, pattern, active, note
            FROM app.institution_attribution_rules
            ORDER BY rule_kind, lower(pattern)
            """
        )
        return list(cur.fetchall())


def _print_rules(rules: Sequence[dict]) -> None:
    if not rules:
        LOGGER.info(
            "No attribution rules configured. Nothing is attributed automatically; "
            "biblio.publications_canon contains only curator-included rows."
        )
        return
    LOGGER.info("%-6s %-12s %-7s %s", "id", "kind", "active", "pattern")
    for rule in rules:
        LOGGER.info(
            "%-6s %-12s %-7s %s",
            rule["rule_id"],
            rule["rule_kind"],
            "yes" if rule["active"] else "no",
            rule["pattern"],
        )


def _apply_rule_changes(
    conn: psycopg.Connection,
    *,
    mode: str,
    affiliations: Sequence[str],
    fundings: Sequence[str],
    note: Optional[str],
) -> Tuple[int, int]:
    """Insert/remove rules for set/add/remove. Returns (added, removed).

    Caller owns the transaction. Only the rule commands reach this function;
    `recompute` and `list` never change the rules.
    """
    if mode not in RULE_COMMANDS:
        raise ValueError(f"{mode} does not change rules")
    added = removed = 0
    wanted: List[Tuple[str, str]] = (
        [("affiliation", p) for p in affiliations] + [("funding", p) for p in fundings]
    )
    with conn.cursor() as cur:
        if mode == "set":
            cur.execute("DELETE FROM app.institution_attribution_rules")
            removed = cur.rowcount or 0
        if mode == "remove":
            for kind, pattern in wanted:
                cur.execute(
                    """
                    DELETE FROM app.institution_attribution_rules
                    WHERE rule_kind = %s AND lower(btrim(pattern)) = lower(btrim(%s))
                    """,
                    (kind, pattern),
                )
                removed += cur.rowcount or 0
            return added, removed
        for kind, pattern in wanted:
            cur.execute(
                """
                INSERT INTO app.institution_attribution_rules (rule_kind, pattern, note)
                VALUES (%s, %s, %s)
                ON CONFLICT (rule_kind, lower(btrim(pattern)))
                DO UPDATE SET active = TRUE,
                              note = COALESCE(EXCLUDED.note, app.institution_attribution_rules.note),
                              updated_at = now()
                """,
                (kind, pattern, note),
            )
            added += cur.rowcount or 0
    return added, removed


def _change_rules_and_recompute(
    conn: psycopg.Connection,
    *,
    mode: str,
    affiliations: Sequence[str],
    fundings: Sequence[str],
    note: Optional[str],
    dry_run: bool,
) -> None:
    """Transaction 1: lock, rule change (rule commands only), recompute.

    Commits unless dry_run, in which case everything is rolled back. Raises
    after rolling back on any error.
    """
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            LOGGER.info(
                "acquiring the attribution lock (waits for in-flight publication "
                "writers and other rule changes to commit)..."
            )
            cur.execute(LOCK_SQL)
            added = removed = 0
            if mode in RULE_COMMANDS:
                added, removed = _apply_rule_changes(
                    conn, mode=mode, affiliations=affiliations, fundings=fundings, note=note
                )
            # Same transaction as the rule change: new rules and recomputed
            # flags commit together or not at all.
            cur.execute(RECOMPUTE_SQL)
            row = cur.fetchone() or {}
            changed = int(row.get("changed") or 0)

        LOGGER.info(
            "rules added/updated=%d removed=%d; publications whose flag changed=%d",
            added,
            removed,
            changed,
        )
        _print_rules(_fetch_rules(conn))

        if dry_run:
            conn.rollback()
            LOGGER.info("dry-run: rolled back, nothing written and no snapshot refreshed.")
            return
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _refresh_snapshots(dsn: Optional[str]) -> None:
    """Transaction 2: refresh both snapshots atomically, map and canon.

    Takes the same advisory lock as DB/refresh_publications_canon_web.sh, so
    the two never overlap. A failure rolls the whole transaction back: both
    snapshots keep their previous state, the committed rules and flags stay
    consistent, and the refresh can simply be retried.
    """
    with db_conn(dsn) as conn:
        conn.autocommit = False
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(REFRESH_LOCK_SQL)
                for statement in REFRESH_SQL:
                    LOGGER.info("%s", statement)
                    cur.execute(statement)
                cur.execute(
                    "SELECT count(*) AS publications, max(snapshot_refreshed_at) AS refreshed_at "
                    "FROM biblio.publications_canon_web"
                )
                row = cur.fetchone() or {}
            conn.commit()
        except Exception:
            conn.rollback()
            LOGGER.error(
                "snapshot refresh failed and was rolled back: both snapshots keep their "
                "previous state; rules and flags are committed and consistent. Retry with "
                "`attribution_rules recompute --refresh-only` or "
                "DB/refresh_publications_canon_web.sh."
            )
            raise
    LOGGER.info(
        "snapshots refreshed together: %s canonical publications at %s",
        row.get("publications"),
        row.get("refreshed_at"),
    )


def run(
    *,
    mode: str,
    dsn: Optional[str],
    affiliations: Sequence[str],
    fundings: Sequence[str],
    note: Optional[str],
    dry_run: bool,
    refresh: bool,
    refresh_only: bool,
) -> int:
    problem = check_options(
        mode=mode,
        affiliations=affiliations,
        fundings=fundings,
        note=note,
        dry_run=dry_run,
        refresh=refresh,
        refresh_only=refresh_only,
    )
    if problem:
        raise SystemExit(problem)
    affiliations = _validate(affiliations, "affiliation")
    fundings = _validate(fundings, "funding")

    if refresh_only:
        _refresh_snapshots(dsn)
        return 0

    with db_conn(dsn) as conn:
        conn.autocommit = False
        # The coordination with publication writers needs READ COMMITTED (see
        # biblio.institution_attribution_lock); do not inherit a stricter
        # server default.
        conn.isolation_level = IsolationLevel.READ_COMMITTED

        if mode == "list":
            _print_rules(_fetch_rules(conn))
            conn.rollback()
            return 0

        for attempt in range(1, TRANSACTION_ATTEMPTS + 1):
            try:
                _change_rules_and_recompute(
                    conn,
                    mode=mode,
                    affiliations=affiliations,
                    fundings=fundings,
                    note=note,
                    dry_run=dry_run,
                )
                break
            except psycopg.errors.DeadlockDetected:
                if attempt == TRANSACTION_ATTEMPTS:
                    LOGGER.error(
                        "rule change rolled back after %d deadlocks with concurrent "
                        "publication writers; rules and flags are unchanged. Retry.",
                        attempt,
                    )
                    raise
                LOGGER.warning(
                    "deadlock with a concurrent publication writer; rolled back, "
                    "retrying (%d/%d)",
                    attempt,
                    TRANSACTION_ATTEMPTS,
                )
            except Exception:
                LOGGER.error("rule change failed and was rolled back; rules and flags are unchanged.")
                raise

    if dry_run:
        return 0
    if refresh:
        _refresh_snapshots(dsn)
    else:
        LOGGER.warning(
            "Snapshots NOT refreshed (--no-refresh). biblio.publications_canon_web still "
            "reflects the previous rules; run `attribution_rules recompute --refresh-only` "
            "or DB/refresh_publications_canon_web.sh before serving."
        )
    return 0


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m people_pubs.sync.attribution_rules",
        description=(
            "Manage institutional attribution rules, recompute the cached flags, "
            "and refresh the canonical snapshots."
        ),
        epilog=(
            "list is read-only and takes no other option. set/add/remove need at least "
            "one --affiliation/--funding; --note is stored with set/add only. recompute "
            "takes no rule option and is the only command that accepts --refresh-only, "
            "which excludes --dry-run and --no-refresh. A --dry-run never writes and "
            "never refreshes a snapshot."
        ),
    )
    p.add_argument("command", choices=COMMANDS)
    p.add_argument(
        "--affiliation",
        action="append",
        default=[],
        metavar="TERM",
        help="Affiliation term to look for in provider payloads (repeatable; set/add/remove).",
    )
    p.add_argument(
        "--funding",
        action="append",
        default=[],
        metavar="ID",
        help="Funding identifier to look for in provider payloads (repeatable; set/add/remove).",
    )
    p.add_argument("--note", default=None, help="Optional note stored with added rules (set/add).")
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (or use PEOPLE_DB_DSN / PG* env vars).")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Roll back instead of committing; nothing is written and no snapshot is refreshed.",
    )
    p.add_argument(
        "--no-refresh",
        action="store_true",
        help="Skip the snapshot refresh. Leaves the served snapshots stale on purpose.",
    )
    p.add_argument(
        "--refresh-only",
        action="store_true",
        help="recompute only: refresh the snapshots and nothing else (retry after a failed refresh).",
    )
    p.add_argument("--debug", action="store_true", help="Verbose logging.")
    args = p.parse_args(argv)
    problem = check_options(
        mode=args.command,
        affiliations=args.affiliation,
        fundings=args.funding,
        note=args.note,
        dry_run=args.dry_run,
        refresh=not args.no_refresh,
        refresh_only=args.refresh_only,
    )
    if problem:
        p.error(problem)
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
    )
    return run(
        mode=args.command,
        dsn=args.dsn,
        affiliations=args.affiliation,
        fundings=args.funding,
        note=args.note,
        dry_run=args.dry_run,
        refresh=not args.no_refresh,
        refresh_only=args.refresh_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
