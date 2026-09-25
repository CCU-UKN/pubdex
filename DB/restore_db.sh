#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BACKUP_DIR="${SCRIPT_DIR}/backups"
COMPOSE_FILE="${SCRIPT_DIR}/compose.yaml"
SERVICE="postgres"
CONTAINER="peopledb-postgres"
DB_USER="postgres"
DB_NAME="people_db"
PYTHON_BIN="${SCRIPT_DIR}/../.venv/bin/python"
RUNTIME_SCHEMA_PATCH="${SCRIPT_DIR}/init/010_ingestion_runtime_schema.sql"

DUMP_FILE=""
GLOBALS_FILE=""
CURATION_FILE=""
USE_GLOBALS=1
USE_CURATION=1
START_POSTGRES=1
APPLY_RUNTIME_SCHEMA_PATCH=1
ASSUME_YES=0

usage() {
  cat <<'EOF'
Restore people_db from backup dumps.

Usage:
  ./restore_db.sh [options]

Options:
  --dump PATH            Path to .dump file (default: latest backups/people_db_*.dump)
  --globals PATH         Path to globals .sql file (default: latest backups/people_globals_*.sql)
  --curation PATH        Path to curation JSON (default: timestamp-matched backups/people_curation_*.json)
  --backup-dir PATH      Backup directory (default: ./backups relative to this script)
  --compose-file PATH    Docker compose file (default: ./compose.yaml relative to this script)
  --service NAME         Compose service to start/check (default: postgres)
  --container NAME       Container name for postgres (default: peopledb-postgres)
  --db-user USER         DB user (default: postgres)
  --db-name NAME         DB name (default: people_db)
  --no-globals           Skip globals restore
  --no-curation          Skip curation JSON import
  --no-start             Do not run "docker compose up -d <service>"
  --no-runtime-schema-patch
                         Skip applying init/010_ingestion_runtime_schema.sql after restore
  -y, --yes              Do not ask for confirmation
  -h, --help             Show this help

Examples:
  ./restore_db.sh
  ./restore_db.sh --dump backups/people_db_20260220T064236Z.dump --no-globals -y
  ./restore_db.sh --backup-dir /path/to/backups --container peopledb-postgres -y
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

latest_file() {
  local pattern="$1"
  local found
  found="$(ls -1t ${pattern} 2>/dev/null | head -n1 || true)"
  echo "${found}"
}

matching_companion_file() {
  local dump_file="$1"
  local prefix="$2"
  local suffix="$3"
  local base
  local stamp
  local candidate

  base="$(basename "${dump_file}")"
  if [[ "${base}" =~ ^people_db_(.+)\.dump$ ]]; then
    stamp="${BASH_REMATCH[1]}"
    candidate="${BACKUP_DIR}/${prefix}_${stamp}${suffix}"
    if [[ -f "${candidate}" ]]; then
      echo "${candidate}"
      return
    fi
  fi

  latest_file "${BACKUP_DIR}/${prefix}_*${suffix}"
}

load_db_env() {
  if [[ -f "${SCRIPT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "${SCRIPT_DIR}/.env"
    set +a
  fi

  export PGHOST="${PGHOST:-127.0.0.1}"
  export PGPORT="${PGPORT:-${POSTGRES_PORT:-5432}}"
  export PGDATABASE="${PGDATABASE:-${DB_NAME}}"
  export PGUSER="${PGUSER:-${DB_USER}}"

  if [[ -z "${PGPASSWORD:-}" ]]; then
    case "${PGUSER}" in
      postgres)
        export PGPASSWORD="${POSTGRES_PASSWORD:-}"
        ;;
      app_writer)
        export PGPASSWORD="${APP_WRITER_PASSWORD:-}"
        ;;
      app_readonly)
        export PGPASSWORD="${APP_READONLY_PASSWORD:-}"
        ;;
      maintenance)
        export PGPASSWORD="${MAINTENANCE_PASSWORD:-}"
        ;;
    esac
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump)
      DUMP_FILE="${2:-}"
      shift 2
      ;;
    --globals)
      GLOBALS_FILE="${2:-}"
      shift 2
      ;;
    --curation)
      CURATION_FILE="${2:-}"
      shift 2
      ;;
    --backup-dir)
      BACKUP_DIR="${2:-}"
      shift 2
      ;;
    --compose-file)
      COMPOSE_FILE="${2:-}"
      shift 2
      ;;
    --service)
      SERVICE="${2:-}"
      shift 2
      ;;
    --container)
      CONTAINER="${2:-}"
      shift 2
      ;;
    --db-user)
      DB_USER="${2:-}"
      shift 2
      ;;
    --db-name)
      DB_NAME="${2:-}"
      shift 2
      ;;
    --no-globals)
      USE_GLOBALS=0
      shift
      ;;
    --no-curation)
      USE_CURATION=0
      shift
      ;;
    --no-start)
      START_POSTGRES=0
      shift
      ;;
    --no-runtime-schema-patch)
      APPLY_RUNTIME_SCHEMA_PATCH=0
      shift
      ;;
    -y|--yes)
      ASSUME_YES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1 (use --help)"
      ;;
  esac
done

command -v docker >/dev/null 2>&1 || die "docker is not installed or not in PATH"

if [[ -z "${DUMP_FILE}" ]]; then
  DUMP_FILE="$(latest_file "${BACKUP_DIR}/people_db_*.dump")"
fi
[[ -n "${DUMP_FILE}" ]] || die "No dump file found. Use --dump or place backups in ${BACKUP_DIR}"
[[ -f "${DUMP_FILE}" ]] || die "Dump file not found: ${DUMP_FILE}"

if [[ "${USE_GLOBALS}" -eq 1 ]]; then
  if [[ -z "${GLOBALS_FILE}" ]]; then
    GLOBALS_FILE="$(latest_file "${BACKUP_DIR}/people_globals_*.sql")"
  fi
  if [[ -z "${GLOBALS_FILE}" || ! -f "${GLOBALS_FILE}" ]]; then
    echo "WARN: globals SQL not found; continuing without globals restore."
    USE_GLOBALS=0
  fi
fi

if [[ "${USE_CURATION}" -eq 1 ]]; then
  if [[ -z "${CURATION_FILE}" ]]; then
    CURATION_FILE="$(matching_companion_file "${DUMP_FILE}" "people_curation" ".json")"
  fi
  if [[ -z "${CURATION_FILE}" || ! -f "${CURATION_FILE}" ]]; then
    echo "WARN: curation JSON not found; continuing without curation import."
    USE_CURATION=0
  fi
fi

if [[ "${START_POSTGRES}" -eq 1 ]]; then
  [[ -f "${COMPOSE_FILE}" ]] || die "Compose file not found: ${COMPOSE_FILE}"
  echo "Ensuring postgres service is up: ${SERVICE}"
  docker compose -f "${COMPOSE_FILE}" up -d "${SERVICE}"
fi

running="$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || true)"
[[ "${running}" == "true" ]] || die "Container is not running: ${CONTAINER}"

echo "Will restore:"
echo "  container : ${CONTAINER}"
echo "  db        : ${DB_NAME}"
echo "  db user   : ${DB_USER}"
echo "  dump      : ${DUMP_FILE}"
if [[ "${USE_GLOBALS}" -eq 1 ]]; then
  echo "  globals   : ${GLOBALS_FILE}"
else
  echo "  globals   : (skipped)"
fi
if [[ "${USE_CURATION}" -eq 1 ]]; then
  echo "  curation  : ${CURATION_FILE}"
else
  echo "  curation  : (skipped)"
fi
if [[ "${APPLY_RUNTIME_SCHEMA_PATCH}" -eq 1 ]]; then
  echo "  patch     : ${RUNTIME_SCHEMA_PATCH}"
else
  echo "  patch     : (skipped)"
fi

if [[ "${ASSUME_YES}" -ne 1 ]]; then
  read -r -p "This will REPLACE database '${DB_NAME}'. Continue? [y/N] " reply
  if [[ ! "${reply}" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
  fi
fi

if [[ "${USE_GLOBALS}" -eq 1 ]]; then
  echo "Applying globals (role-exists errors are expected and safe)..."
  cat "${GLOBALS_FILE}" | docker exec -i "${CONTAINER}" psql -v ON_ERROR_STOP=0 -U "${DB_USER}" -d postgres >/dev/null || true
fi

echo "Terminating active sessions on ${DB_NAME}..."
docker exec "${CONTAINER}" psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d postgres \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${DB_NAME}' AND pid <> pg_backend_pid();" >/dev/null

echo "Dropping and recreating ${DB_NAME}..."
docker exec "${CONTAINER}" dropdb -U "${DB_USER}" --if-exists "${DB_NAME}"
docker exec "${CONTAINER}" createdb -U "${DB_USER}" "${DB_NAME}"

echo "Restoring dump into ${DB_NAME}..."
# --single-transaction + --exit-on-error: a partial restore rolls back and
# fails loudly instead of leaving a silently incomplete database.
cat "${DUMP_FILE}" | docker exec -i "${CONTAINER}" pg_restore \
  -U "${DB_USER}" \
  -d "${DB_NAME}" \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  --single-transaction \
  --exit-on-error

if [[ "${APPLY_RUNTIME_SCHEMA_PATCH}" -eq 1 ]]; then
  [[ -f "${RUNTIME_SCHEMA_PATCH}" ]] || die "Runtime schema patch not found: ${RUNTIME_SCHEMA_PATCH}"
  echo "Applying runtime schema patch..."
  cat "${RUNTIME_SCHEMA_PATCH}" | docker exec -i "${CONTAINER}" psql \
    -v ON_ERROR_STOP=1 \
    -U "${DB_USER}" \
    -d "${DB_NAME}" \
    -f -
else
  echo "WARNING: runtime schema patch SKIPPED (--no-runtime-schema-patch)." >&2
  echo "WARNING: this database will lack runtime-only objects the current code" >&2
  echo "         expects (e.g. biblio.authorships_curated, publications_canon)." >&2
fi

if [[ "${USE_CURATION}" -eq 1 ]]; then
  [[ -x "${PYTHON_BIN}" ]] || die "Python env not found for curation import: ${PYTHON_BIN}"
  echo "Importing curation backup..."
  load_db_env
  "${PYTHON_BIN}" -m people_pubs.sync.curation_backup import --input "${CURATION_FILE}"
fi

echo "Restore completed."

pub_count="$(docker exec "${CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -Atqc "SELECT COUNT(*) FROM biblio.publications;" 2>/dev/null || true)"
auth_count="$(docker exec "${CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -Atqc "SELECT COUNT(*) FROM biblio.authorships;" 2>/dev/null || true)"
if [[ -n "${pub_count}" || -n "${auth_count}" ]]; then
  echo "Quick check: publications=${pub_count:-n/a} authorships=${auth_count:-n/a}"
fi
