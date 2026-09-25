#!/usr/bin/env bash
set -euo pipefail

timeout_seconds="${1:-300}"
interval_seconds="${DB_WAIT_INTERVAL_SECONDS:-5}"

if ! [[ "$timeout_seconds" =~ ^[0-9]+$ ]]; then
  echo "wait_for_postgres: timeout must be an integer number of seconds" >&2
  exit 2
fi

deadline=$((SECONDS + timeout_seconds))

while (( SECONDS <= deadline )); do
  if docker exec peopledb-postgres pg_isready -U postgres -d people_db >/dev/null 2>&1; then
    exit 0
  fi
  sleep "$interval_seconds"
done

echo "wait_for_postgres: peopledb-postgres did not become ready within ${timeout_seconds}s" >&2
docker compose ps >&2 || true
exit 1
