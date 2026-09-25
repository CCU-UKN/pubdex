#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

load_db_env() {
  if [[ -f ./.env ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
  fi

  export PGHOST="${PGHOST:-127.0.0.1}"
  export PGPORT="${PGPORT:-${POSTGRES_PORT:-5432}}"
  export PGDATABASE="${PGDATABASE:-${POSTGRES_DB:-people_db}}"
  export PGUSER="${PGUSER:-${POSTGRES_USER:-postgres}}"
  export PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"
}

./wait_for_postgres.sh "${DB_WAIT_TIMEOUT_SECONDS:-300}"
mkdir -p backups
load_db_env

ts="$(date -u +%Y%m%dT%H%M%SZ)"
dump_tmp="backups/.people_db_${ts}.dump.tmp"
globals_tmp="backups/.people_globals_${ts}.sql.tmp"
curation_tmp="backups/.people_curation_${ts}.json.tmp"
trap 'rm -f "${dump_tmp}" "${globals_tmp}" "${curation_tmp}"' EXIT

# Dump to temp files first so a mid-dump failure never leaves a truncated
# file under a final name (a 0-byte dump would silently age out of retention).
docker exec peopledb-postgres pg_dump -U postgres -d people_db -Fc > "${dump_tmp}"
docker exec peopledb-postgres pg_dumpall -U postgres --globals-only > "${globals_tmp}"

# Verify the custom-format dump is a readable archive before publishing it.
docker exec -i peopledb-postgres pg_restore --list < "${dump_tmp}" > /dev/null

mv "${dump_tmp}" "backups/people_db_${ts}.dump"
mv "${globals_tmp}" "backups/people_globals_${ts}.sql"

# Curation overlay export must not block the SQL dumps above (they are already
# safely on disk), but a broken venv still fails the run loudly for cron.
rc=0
python_bin="../.venv/bin/python"
if [[ -x "${python_bin}" ]]; then
  if "${python_bin}" -m people_pubs.sync.curation_backup export --output "${curation_tmp}"; then
    mv "${curation_tmp}" "backups/people_curation_${ts}.json"
  else
    echo "ERROR: curation overlay export failed; SQL dumps for ${ts} are intact." >&2
    rc=1
  fi
else
  echo "ERROR: missing ${python_bin}; skipped curation overlay export (SQL dumps for ${ts} are intact)." >&2
  rc=1
fi

# Trailing slash: backups/ is a symlink in the cron worktree, and find(1) does
# not descend into a symlinked starting point without it, which would silently
# retain every dump forever.
find backups/ -name 'people_db_*.dump' -mtime +30 -delete
find backups/ -name 'people_globals_*.sql' -mtime +30 -delete
find backups/ -name 'people_curation_*.json' -mtime +30 -delete
find backups/ -name '.people_*.tmp' -mtime +1 -delete

exit "${rc}"
