#!/usr/bin/env python3
"""
people_pubs.sync.backfill_all

Run the standard Crossref, DataCite and Semantic Scholar backfills in
sequence, with OpenAlex and DBLP available as opt-in extras.

Role:
- Orchestrates the backfill scripts with shared flags.
- Useful for periodic metadata refreshes across sources.

Typical usage:
  export PEOPLE_PUBS_CROSSREF_MAILTO="you@example.org"
  python -m people_pubs.sync.backfill_all --max 200 --merge-mode doi --debug

Common cases:
- Run the standard set in order: Crossref -> DataCite -> Semantic Scholar.
- Include an extra source only with --with-openalex / --with-dblp.
- Skip a standard source with --skip-crossref / --skip-datacite / --skip-semantic.
- Target a single record with --only-pub-id.
- Target canonical publications with too few provenance sources using
  --only-low-source-canon.
- Dry run with --dry-run (no DB writes).
- When --only-pub-id is a CSV, the CSV order is preserved across all backfills.

Examples:
  # Full run of the standard set
  python -m people_pubs.sync.backfill_all --max 200 --merge-mode doi --debug

  # Standard set plus OpenAlex + DBLP
  python -m people_pubs.sync.backfill_all --with-openalex --with-dblp --max 200 --debug

  # Single pub_id, no DB writes
  python -m people_pubs.sync.backfill_all --only-pub-id 1210 --dry-run --debug

Notes:
- DataCite uses the public REST API; set DATACITE_API_TOKEN if you have one.
- OpenAlex and Crossref politely use mailto from PEOPLE_PUBS_CROSSREF_MAILTO.
- Semantic Scholar requires an API key; set SEMANTIC_SCHOLAR_API_KEY.
- OpenAlex is excluded from the default set because it currently introduces too many metadata mistakes to be a standard source.
- DBLP is excluded from the default set because title-based fallback/backfill is comparatively slow.
- Each backfill uses its own missing-data filter unless --force is set.
- DBLP does not provide structured affiliations; authorship refresh may be skipped.
"""

from __future__ import annotations

import argparse
import atexit
import csv
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from people_pubs.config import (
    CROSSREF_MAILTO,
    DEFAULT_DSN,
    DATACITE_API_TOKEN,
    SEMANTIC_SCHOLAR_API_KEY,
)
from people_pubs.db.connection import db_conn
from people_pubs.sync.crossref_backfill import run_crossref_backfill
from people_pubs.sync.datacite_backfill import run_datacite_backfill
from people_pubs.sync.semantic_scholar_backfill import run_semantic_backfill
from people_pubs.sync.openalex_backfill import run_openalex_backfill
from people_pubs.sync.dblp_backfill import run_dblp_backfill
from people_pubs.sync.scaffold import _parse_pub_id_list


LOGGER = logging.getLogger("people_pubs.backfill_all")


def count_source_tokens(source_of_truth: Optional[str]) -> int:
    tokens = [
        part.strip()
        for part in (source_of_truth or "").split("+")
        if part.strip() and part.strip().lower() != "unknown"
    ]
    return len(tokens)


# Payload keys the attribution backfill cares about. A row is a candidate while
# any of these is absent from raw_json. Crossref is the only source this selector
# pulls: DataCite/Semantic are deliberately out of scope (no yield on
# non-preprint journal/conference output), and the bookkeeping below does not
# depend on which keys are listed.
ATTRIBUTION_PAYLOAD_KEYS = ("crossref",)


def select_unattributed_missing_payload_pub_ids(
    *,
    dsn: Optional[str],
    max_n: int,
    cooldown_days: int,
) -> List[int]:
    """Publications that should be attributed but are not, and that we have not
    already exhausted the providers for.

    Selection is by *which payload is missing*, not by age or token count:
    attribution evidence lives in the provider payloads, so a row whose payloads
    do not mention the institution can never gain the flag by being re-fetched
    from a source we already hold. The only lever is pulling a payload we do not
    hold yet and letting the `institution_attributed` trigger re-evaluate
    raw_json against the configured rules.

    With no rules configured (app.institution_attribution_rules empty) nothing
    is attributed, so this selector offers every eligible publication.

    `cooldown_days` skips rows attempted recently, so publications the providers
    genuinely do not hold stop blocking the head of the queue.
    """
    if max_n is not None and max_n <= 0:
        raise ValueError("max_n must be > 0")
    if cooldown_days < 0:
        raise ValueError("cooldown_days must be >= 0")

    missing_clause = " OR ".join(
        f"NOT (p.raw_json ? '{key}')" for key in ATTRIBUTION_PAYLOAD_KEYS
    )

    sql = f"""
    SELECT p.pub_id
    FROM biblio.publications p
    LEFT JOIN activity.attribution_backfill_attempts a ON a.pub_id = p.pub_id
    WHERE p.institution_attributed IS NOT TRUE
      AND p.is_preprint IS NOT TRUE
      AND EXISTS (
            SELECT 1 FROM biblio.authorships au
            WHERE au.pub_id = p.pub_id AND au.person_id IS NOT NULL
          )
      AND ({missing_clause})
      AND (
            a.pub_id IS NULL
            OR a.last_outcome = 'error'
            OR a.last_attempt_at < now() - make_interval(days => %(cooldown_days)s)
          )
    ORDER BY
      -- never-tried first, then longest-untried
      (a.pub_id IS NOT NULL),
      a.last_attempt_at ASC NULLS FIRST,
      -- a missing Crossref record is the cheapest, highest-yield fetch
      (p.raw_json ? 'crossref'),
      p.year DESC NULLS LAST,
      p.pub_id ASC
    """
    params: dict = {"cooldown_days": cooldown_days}
    if max_n and max_n > 0:
        sql += "\nLIMIT %(max_n)s"
        params["max_n"] = max_n

    with db_conn(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    out: List[int] = []
    for row in rows:
        try:
            out.append(int(row["pub_id"]))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _payload_state(dsn: Optional[str], pub_ids: List[int]) -> dict:
    """Map pub_id -> (missing_payload_keys, institution_attributed) for a bounded set."""
    if not pub_ids:
        return {}
    checks = ", ".join(
        f"(NOT (p.raw_json ? '{key}')) AS missing_{key}" for key in ATTRIBUTION_PAYLOAD_KEYS
    )
    sql = f"""
    SELECT p.pub_id, p.institution_attributed, {checks}
    FROM biblio.publications p
    WHERE p.pub_id = ANY(%(ids)s)
    """
    state: dict = {}
    with db_conn(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"ids": pub_ids})
            for row in cur.fetchall():
                pub_id = int(row["pub_id"])
                missing = [
                    key for key in ATTRIBUTION_PAYLOAD_KEYS if row[f"missing_{key}"]
                ]
                state[pub_id] = (missing, bool(row["institution_attributed"]))
    return state


def record_attribution_attempts(
    *,
    dsn: Optional[str],
    before: dict,
    after: dict,
    provider_errors: Optional[Dict[str, set[int]]] = None,
) -> dict:
    """Upsert one attempt row per publication and return a summary counter.

    Called after the provider backfills have run over the selected set, so the
    outcome reflects what actually changed rather than what was requested.
    """
    counts = {
        "enriched": 0,
        "no_new_payload": 0,
        "error": 0,
        "became_attributed": 0,
        "merged_away": 0,
    }
    provider_errors = provider_errors or {}
    rows = []
    for pub_id, (missing_before, _) in before.items():
        if pub_id not in after:
            # --merge-mode doi can delete a row mid-run when a DOI collision makes
            # it the loser of a merge. Its content now lives on the winner, which
            # the selector will pick up on its own merits; there is no row left to
            # attach an attempt to (and the FK would reject it).
            counts["merged_away"] += 1
            continue
        missing_after, attributed_after = after[pub_id]
        gained = set(missing_before) - set(missing_after)
        unresolved_provider_errors = {
            source
            for source in missing_after
            if pub_id in provider_errors.get(source, set())
        }
        if unresolved_provider_errors:
            outcome = "error"
        elif gained:
            outcome = "enriched"
        else:
            outcome = "no_new_payload"
        counts[outcome] += 1
        if attributed_after:
            counts["became_attributed"] += 1
        rows.append((pub_id, outcome, missing_before, missing_after, attributed_after))

    if not rows:
        return counts

    sql = """
    INSERT INTO activity.attribution_backfill_attempts AS t
      (pub_id, attempt_count, first_attempt_at, last_attempt_at,
       last_outcome, missing_before, missing_after, became_attributed)
    VALUES (%s, 1, now(), now(), %s, %s, %s, %s)
    ON CONFLICT (pub_id) DO UPDATE SET
      attempt_count     = t.attempt_count + 1,
      last_attempt_at   = now(),
      last_outcome      = EXCLUDED.last_outcome,
      missing_before    = EXCLUDED.missing_before,
      missing_after     = EXCLUDED.missing_after,
      became_attributed = EXCLUDED.became_attributed
    """
    with db_conn(dsn) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
    return counts


def select_low_source_canon_pub_ids(
    *,
    dsn: Optional[str],
    source_threshold: int,
    max_n: int,
) -> List[int]:
    if source_threshold <= 0:
        raise ValueError("source_threshold must be > 0")

    sql = """
    WITH source_counts AS (
      SELECT
        pc.pub_id,
        pc.year,
        CASE
          WHEN btrim(COALESCE(pc.source_of_truth, '')) = '' THEN 0
          WHEN lower(btrim(COALESCE(pc.source_of_truth, ''))) = 'unknown' THEN 0
          ELSE
            length(COALESCE(pc.source_of_truth, ''))
            - length(replace(COALESCE(pc.source_of_truth, ''), '+', ''))
            + 1
        END AS source_count
      FROM biblio.publications_canon pc
    )
    SELECT pub_id
    FROM source_counts
    WHERE source_count < %(source_threshold)s
    ORDER BY
      source_count ASC,
      year DESC NULLS LAST,
      pub_id ASC
    """
    params = {"source_threshold": source_threshold}
    if max_n and max_n > 0:
        sql += "\nLIMIT %(max_n)s"
        params["max_n"] = max_n

    with db_conn(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    out: List[int] = []
    for row in rows:
        try:
            out.append(int(row["pub_id"]))
        except Exception:
            out.append(int(row[0]))
    return out


def _pub_id_arg_for_string_parser(pub_ids: Optional[List[int]]) -> Optional[str]:
    if not pub_ids:
        return None
    raw = ",".join(str(x) for x in pub_ids)
    if len(raw) < 200:
        return raw

    fd, path = tempfile.mkstemp(
        prefix="people_pubs_pub_ids_",
        suffix=".csv",
        text=True,
    )
    with os.fdopen(fd, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["pub_id"])
        for pub_id in pub_ids:
            writer.writerow([pub_id])

    def _cleanup(path: str = path) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_cleanup)
    return path


def run_backfill_all(
    *,
    dsn: str,
    mailto: Optional[str],
    max_n: int,
    only_pub_ids: Optional[List[int]],
    merge_mode: str,
    dry_run: bool,
    force: bool,
    include_department: bool,
    crossref_sleep: float,
    crossref_source_token: str,
    datacite_token: Optional[str],
    datacite_sleep: float,
    datacite_source_token: str,
    datacite_rewrite_sot: Optional[str],
    semantic_api_key: Optional[str],
    semantic_sleep: float,
    semantic_source_token: str,
    semantic_rewrite_sot: Optional[str],
    openalex_sleep: float,
    openalex_source_token: str,
    openalex_rewrite_sot: Optional[str],
    dblp_sleep: float,
    dblp_source_token: str,
    dblp_rewrite_sot: Optional[str],
    dblp_title_fallback: bool,
    refresh_authorships: bool,
    skip_crossref: bool,
    skip_datacite: bool,
    skip_semantic: bool,
    skip_openalex: bool,
    skip_dblp: bool,
) -> Dict[str, set[int]]:
    provider_errors: Dict[str, set[int]] = {}
    ordered_pub_ids = only_pub_ids
    effective_max = max_n
    if only_pub_ids:
        if max_n and max_n > 0:
            ordered_pub_ids = only_pub_ids[:max_n]
            effective_max = len(ordered_pub_ids)
        else:
            effective_max = len(only_pub_ids)
    string_only_pub_id = _pub_id_arg_for_string_parser(ordered_pub_ids)

    if not skip_crossref:
        LOGGER.info("Running Crossref backfill.")
        stats = run_crossref_backfill(
            dsn=dsn,
            mailto=mailto,
            max_n=effective_max,
            only_pub_ids=ordered_pub_ids,
            merge_mode=merge_mode,
            sleep_s=crossref_sleep,
            dry_run=dry_run,
            force=force,
            refresh_authorships=refresh_authorships,
            source_token=crossref_source_token,
            include_department=include_department,
        )
        provider_errors["crossref"] = set(stats.failed_pub_ids)

    if not skip_datacite:
        LOGGER.info("Running DataCite backfill.")
        run_datacite_backfill(
            dsn=dsn,
            token=datacite_token,
            max_n=effective_max,
            only_pub_id=string_only_pub_id,
            merge_mode=merge_mode,
            sleep_s=datacite_sleep,
            dry_run=dry_run,
            force=force,
            source_token=datacite_source_token,
            rewrite_sot=datacite_rewrite_sot,
            refresh_authorships=refresh_authorships,
            include_department=include_department,
            search_count=5,
            debug_json=False,
            debug_json_max_chars=0,
        )

    if not skip_semantic:
        if not semantic_api_key:
            raise SystemExit(
                "Missing Semantic Scholar API key (set SEMANTIC_SCHOLAR_API_KEY or pass --semantic-api-key)."
            )
        LOGGER.info("Running Semantic Scholar backfill.")
        run_semantic_backfill(
            dsn=dsn,
            api_key=semantic_api_key,
            max_n=effective_max,
            only_pub_id=string_only_pub_id,
            merge_mode=merge_mode,
            sleep_s=semantic_sleep,
            dry_run=dry_run,
            force=force,
            source_token=semantic_source_token,
            rewrite_sot=semantic_rewrite_sot,
            refresh_authorships=refresh_authorships,
            include_department=include_department,
            search_count=5,
            debug_json=False,
            debug_json_max_chars=0,
        )

    if skip_openalex:
        LOGGER.info("Skipping OpenAlex backfill by default; use --with-openalex to opt in.")
    else:
        LOGGER.info("Running OpenAlex backfill.")
        run_openalex_backfill(
            dsn=dsn,
            mailto=mailto,
            max_n=effective_max,
            only_pub_ids=ordered_pub_ids,
            merge_mode=merge_mode,
            sleep_s=openalex_sleep,
            dry_run=dry_run,
            force=force,
            source_token=openalex_source_token,
            rewrite_sot=openalex_rewrite_sot,
            refresh_authorships=refresh_authorships,
            include_department=include_department,
        )

    if skip_dblp:
        LOGGER.info("Skipping DBLP backfill by default; use --with-dblp to opt in.")
    else:
        LOGGER.info("Running DBLP backfill.")
        run_dblp_backfill(
            dsn=dsn,
            max_n=effective_max,
            only_pub_ids=ordered_pub_ids,
            merge_mode=merge_mode,
            sleep_s=dblp_sleep,
            dry_run=dry_run,
            force=force,
            source_token=dblp_source_token,
            rewrite_sot=dblp_rewrite_sot,
            refresh_authorships=refresh_authorships,
            include_department=include_department,
            title_fallback=dblp_title_fallback,
        )

    return provider_errors


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Run the standard Crossref, DataCite and Semantic Scholar backfills in "
            "sequence. OpenAlex and DBLP are opt-in extras."
        )
    )
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (defaults to people_pubs.config.DEFAULT_DSN)")
    p.add_argument("--mailto", default=CROSSREF_MAILTO, help="Email for Crossref/OpenAlex polite usage.")
    p.add_argument("--max", type=int, default=200, help="Max publications to process per backfill.")
    p.add_argument(
        "--only-pub-id",
        default=None,
        help="Process one pub_id or a CSV file (single column of pub_ids).",
    )
    p.add_argument(
        "--only-low-source-canon",
        action="store_true",
        help="Process canonical publications whose source_of_truth has fewer than --source-threshold non-unknown source tokens.",
    )
    p.add_argument(
        "--source-threshold",
        type=int,
        default=4,
        help="Minimum non-unknown source_of_truth tokens before a canon row is skipped by --only-low-source-canon.",
    )
    p.add_argument(
        "--only-unattributed-missing-payload",
        action="store_true",
        help=(
            "Process publications that carry an internal author but no institutional attribution "
            "signal and are still missing a Crossref payload. Records each "
            "attempt in activity.attribution_backfill_attempts so exhausted rows stop "
            "blocking the queue."
        ),
    )
    p.add_argument(
        "--attempt-cooldown-days",
        type=int,
        default=30,
        help=(
            "Skip publications whose last attribution-backfill attempt is newer than this "
            "many days. Default: 30."
        ),
    )
    p.add_argument(
        "--merge-mode",
        default="doi",
        choices=["none", "doi", "doi+title"],
        help="Duplicate merge strategy. Default: doi.",
    )
    p.add_argument("--dry-run", action="store_true", help="Do not write to DB.")
    p.add_argument("--force", action="store_true", help="Ignore missing-data filters.")
    p.add_argument(
        "--department",
        action="store_true",
        help="Include department fields in affiliation strings when present.",
    )
    p.add_argument(
        "--refresh-authorships",
        "--refresh_authorships",
        dest="refresh_authorships",
        action="store_true",
        help="Refresh authorships from each source during backfill.",
    )
    p.add_argument("--skip-crossref", action="store_true", help="Skip Crossref backfill.")
    p.add_argument("--skip-datacite", action="store_true", help="Skip DataCite backfill.")
    p.add_argument("--skip-semantic", action="store_true", help="Skip Semantic Scholar backfill.")
    p.add_argument(
        "--with-openalex",
        action="store_true",
        help="Include OpenAlex in backfill_all. Excluded by default because it currently introduces too many metadata mistakes.",
    )
    p.add_argument(
        "--with-dblp",
        action="store_true",
        help="Include DBLP in backfill_all. Excluded by default because DBLP backfill is comparatively slow.",
    )

    p.add_argument("--crossref-sleep", type=float, default=0.0, help="Seconds to sleep between Crossref requests.")
    p.add_argument(
        "--crossref-source-token",
        default="crossref",
        help="Token to merge into source_of_truth for Crossref.",
    )
    p.add_argument("--datacite-token", default=DATACITE_API_TOKEN, help="DataCite API token (optional).")
    p.add_argument("--datacite-sleep", type=float, default=0.0, help="Seconds to sleep between DataCite requests.")
    p.add_argument(
        "--datacite-source-token",
        default="datacite",
        help="Token to merge into source_of_truth for DataCite.",
    )
    p.add_argument("--datacite-rewrite-sot", help="Replace source_of_truth for DataCite.")
    p.add_argument(
        "--semantic-api-key",
        default=SEMANTIC_SCHOLAR_API_KEY,
        help="Semantic Scholar API key (or env SEMANTIC_SCHOLAR_API_KEY).",
    )
    p.add_argument("--semantic-sleep", type=float, default=1.0, help="Seconds to sleep between Semantic Scholar requests.")
    p.add_argument(
        "--semantic-source-token",
        default="semantic",
        help="Token to merge into source_of_truth for Semantic Scholar.",
    )
    p.add_argument("--semantic-rewrite-sot", help="Replace source_of_truth for Semantic Scholar.")
    p.add_argument("--openalex-sleep", type=float, default=0.0, help="Seconds to sleep between OpenAlex requests.")
    p.add_argument(
        "--openalex-source-token",
        default="openalex",
        help="Token to merge into source_of_truth for OpenAlex.",
    )
    p.add_argument("--openalex-rewrite-sot", help="Replace source_of_truth for OpenAlex.")
    p.add_argument("--dblp-sleep", type=float, default=0.0, help="Seconds to sleep between DBLP requests.")
    p.add_argument(
        "--dblp-source-token",
        default="dblp",
        help="Token to merge into source_of_truth for DBLP.",
    )
    p.add_argument("--dblp-rewrite-sot", help="Replace source_of_truth for DBLP.")
    p.add_argument(
        "--dblp-title-fallback",
        action="store_true",
        help="Fallback to title search if DBLP DOI lookup fails.",
    )
    p.add_argument("--debug", action="store_true", help="Enable debug logging.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
    )

    only_pub_ids = _parse_pub_id_list(args.only_pub_id)
    if args.only_low_source_canon:
        if only_pub_ids:
            raise SystemExit("--only-low-source-canon cannot be combined with --only-pub-id.")
        only_pub_ids = select_low_source_canon_pub_ids(
            dsn=args.dsn,
            source_threshold=args.source_threshold,
            max_n=args.max,
        )
        LOGGER.info(
            "Low-source canon selector queued %s publications with fewer than %s source tokens.",
            len(only_pub_ids),
            args.source_threshold,
        )
        if not only_pub_ids:
            return

    attribution_before: dict = {}
    if args.only_unattributed_missing_payload:
        if only_pub_ids:
            raise SystemExit(
                "--only-unattributed-missing-payload cannot be combined with "
                "--only-pub-id or --only-low-source-canon."
            )
        only_pub_ids = select_unattributed_missing_payload_pub_ids(
            dsn=args.dsn,
            max_n=args.max,
            cooldown_days=args.attempt_cooldown_days,
        )
        LOGGER.info(
            "Attribution selector queued %s publications missing a %s payload "
            "(cooldown %s days).",
            len(only_pub_ids),
            "/".join(ATTRIBUTION_PAYLOAD_KEYS),
            args.attempt_cooldown_days,
        )
        if not only_pub_ids:
            LOGGER.info(
                "attribution_backfill: nothing to do — every candidate was attempted "
                "within the cooldown window."
            )
            return
        if not args.dry_run:
            attribution_before = _payload_state(args.dsn, only_pub_ids)

    provider_errors = run_backfill_all(
        dsn=args.dsn,
        mailto=args.mailto,
        max_n=args.max,
        only_pub_ids=only_pub_ids,
        merge_mode=args.merge_mode,
        dry_run=args.dry_run,
        force=args.force,
        include_department=args.department,
        crossref_sleep=args.crossref_sleep,
        crossref_source_token=args.crossref_source_token,
        datacite_token=args.datacite_token or None,
        datacite_sleep=args.datacite_sleep,
        datacite_source_token=args.datacite_source_token,
        datacite_rewrite_sot=args.datacite_rewrite_sot,
        semantic_api_key=args.semantic_api_key,
        semantic_sleep=args.semantic_sleep,
        semantic_source_token=args.semantic_source_token,
        semantic_rewrite_sot=args.semantic_rewrite_sot,
        openalex_sleep=args.openalex_sleep,
        openalex_source_token=args.openalex_source_token,
        openalex_rewrite_sot=args.openalex_rewrite_sot,
        dblp_sleep=args.dblp_sleep,
        dblp_source_token=args.dblp_source_token,
        dblp_rewrite_sot=args.dblp_rewrite_sot,
        dblp_title_fallback=args.dblp_title_fallback,
        refresh_authorships=args.refresh_authorships,
        skip_crossref=args.skip_crossref,
        skip_datacite=args.skip_datacite,
        skip_semantic=args.skip_semantic,
        skip_openalex=not args.with_openalex,
        skip_dblp=not args.with_dblp,
    )

    if attribution_before:
        after = _payload_state(args.dsn, list(attribution_before))
        counts = record_attribution_attempts(
            dsn=args.dsn,
            before=attribution_before,
            after=after,
            provider_errors=provider_errors,
        )
        LOGGER.info(
            "attribution_backfill summary: processed=%s enriched=%s no_new_payload=%s errors=%s "
            "merged_away=%s became_attributed=%s cooldown_days=%s",
            len(attribution_before),
            counts["enriched"],
            counts["no_new_payload"],
            counts["error"],
            counts["merged_away"],
            counts["became_attributed"],
            args.attempt_cooldown_days,
        )


if __name__ == "__main__":
    main()
