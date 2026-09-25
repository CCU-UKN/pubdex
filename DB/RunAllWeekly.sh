#!/usr/bin/env bash
# Weekly sequence (metadata backfills). cron.example invokes this script, so
# the job list below is the single source of truth for what runs weekly; it is
# also the manual entry point.
# Each job is its own runner invocation, so one job's failure does not stop the
# later ones; the script still exits non-zero if any job failed.
# --stop-on-error applies within a job (stop at its first failing item).
set -uo pipefail

cd "$(dirname "$0")"

rc=0
./run_refresh_job.sh --only crossref_backfill_query --stop-on-error >> refresh_backfills.log 2>> refresh_backfills.err.log || rc=1
./run_refresh_job.sh --only semantic_backfill_query --stop-on-error >> refresh_backfills.log 2>> refresh_backfills.err.log || rc=1
./run_refresh_job.sh --only dblp_backfill_query --stop-on-error >> refresh_backfills.log 2>> refresh_backfills.err.log || rc=1
./run_refresh_job.sh --only low_source_canon_backfill_query --stop-on-error >> refresh_backfills.log 2>> refresh_backfills.err.log || rc=1

exit "${rc}"
