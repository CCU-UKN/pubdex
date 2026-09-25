#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${PEOPLE_PUBS_INTEGRATION_DSN:-}${PEOPLE_DB_TEST_DSN:-}" ]]; then
  echo "Set PEOPLE_PUBS_INTEGRATION_DSN or PEOPLE_DB_TEST_DSN to a disposable PostgreSQL database." >&2
  exit 2
fi

PYTHONPATH=. exec ../.venv/bin/python -m pytest -m integration "$@"
