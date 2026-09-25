#!/usr/bin/env bash
# Alert when the PubDex canonical web snapshot is stale or missing.
#
# biblio.publications_canon_web is refreshed on a scheduled (cron) cadence
# (refresh_publications_canon_web.sh). A hard refresh *failure* already exits
# non-zero from that script; this companion check catches the case that script
# cannot: a refresh that never ran at all (host down, cron disabled), which
# would let readers be served a silently stale snapshot.
#
# Run it on its own cron schedule with MAILTO set, e.g. hourly:
#   MAILTO=ops@example.org
#   17 * * * * /path/to/DB/check_canon_web_freshness.sh
# It prints an ALERT line to stderr and exits non-zero when the freshest
# snapshot row is older than CANON_WEB_MAX_AGE_HOURS (default 24), so cron
# delivers the email.
set -euo pipefail

cd "$(dirname "$0")"

max_age_hours="${CANON_WEB_MAX_AGE_HOURS:-24}"

./wait_for_postgres.sh "${DB_WAIT_TIMEOUT_SECONDS:-300}"

# Age of the freshest snapshot row in hours. An empty view (no rows) or NULL
# timestamp is reported as effectively infinite so it trips the alert.
age_hours="$(docker exec -i peopledb-postgres \
  psql -U postgres -d people_db -tA -v ON_ERROR_STOP=1 \
  -c "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - max(snapshot_refreshed_at))) / 3600.0, 1e9) FROM biblio.publications_canon_web;")"

if awk -v a="$age_hours" -v m="$max_age_hours" 'BEGIN { exit !(a + 0 > m + 0) }'; then
  echo "ALERT: biblio.publications_canon_web snapshot is stale — age=${age_hours}h exceeds ${max_age_hours}h; check the scheduled refresh job." >&2
  exit 1
fi

echo "publications_canon_web freshness OK — age=${age_hours}h (limit ${max_age_hours}h)."
