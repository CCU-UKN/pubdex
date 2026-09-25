#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

./wait_for_postgres.sh "${DB_WAIT_TIMEOUT_SECONDS:-300}"

exec ../.venv/bin/python -m people_pubs.sync.refresh_state_runner \
  --config refresh_jobs.json \
  "$@"
