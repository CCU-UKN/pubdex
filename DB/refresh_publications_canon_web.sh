#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Surface a clear, greppable alert to stderr on any failure so the cron MAILTO
# message is actionable instead of a raw psql traceback. The non-zero exit
# (set -e) still propagates, so cron treats the run as failed.
trap 'rc=$?; echo "ALERT: biblio.publications_canon_web refresh FAILED (exit ${rc}); readers keep the last good snapshot pair until this succeeds." >&2' ERR

./wait_for_postgres.sh "${DB_WAIT_TIMEOUT_SECONDS:-300}"

docker exec -i peopledb-postgres \
  psql -U postgres -d people_db -v ON_ERROR_STOP=1 \
  -f - < refresh_publications_canon_web.sql
