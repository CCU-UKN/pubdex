#!/usr/bin/env bash
# Daily sequence (backup + per-person refresh jobs). cron.example invokes this
# script, so the job list below is the single source of truth for what runs
# daily; it is also the manual entry point.
# Each job is its own runner invocation, so one job's failure does not stop the
# later ones; the script still exits non-zero if any job failed.
# --stop-on-error applies within a job (stop at its first failing item).
set -uo pipefail

cd "$(dirname "$0")"

rc=0
./run_backup.sh >> refresh_backup.log 2>> refresh_backup.err.log || rc=1
./run_refresh_job.sh --only orcid_profiles_person --stop-on-error >> refresh_orcid_profiles.log 2>> refresh_orcid_profiles.err.log || rc=1
./run_refresh_job.sh --only orcid_works_person --stop-on-error >> refresh_orcid_works.log 2>> refresh_orcid_works.err.log || rc=1
./run_refresh_job.sh --only crossref_search_orcid_person --stop-on-error >> refresh_crossref_orcid.log 2>> refresh_crossref_orcid.err.log || rc=1

exit "${rc}"
