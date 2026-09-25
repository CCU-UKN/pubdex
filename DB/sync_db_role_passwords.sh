#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env}"
CONTAINER="${PG_CONTAINER:-peopledb-postgres}"
MODE="check"

usage() {
  cat <<'EOF'
Sync PostgreSQL role passwords from DB/.env.

Usage:
  ./sync_db_role_passwords.sh --check
  ./sync_db_role_passwords.sh --apply

Modes:
  --check   Verify that env-backed role passwords currently work (default)
  --apply   Apply env-backed role passwords with ALTER ROLE, then verify
  -h, --help  Show this help

Env vars read from DB/.env if present:
  APP_READONLY_PASSWORD
  APP_WRITER_PASSWORD
  MAINTENANCE_PASSWORD

Connection/admin vars:
  PGUSER        default: postgres
  PGDATABASE    default: people_db
  PG_CONTAINER  default: peopledb-postgres
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      MODE="check"
      shift
      ;;
    --apply)
      MODE="apply"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

[[ -f "$ENV_FILE" ]] || die "Env file not found: $ENV_FILE"
set -a
. "$ENV_FILE"
set +a

DB_NAME="${PGDATABASE:-people_db}"
ADMIN_USER="${PGUSER:-postgres}"

command -v docker >/dev/null 2>&1 || die "docker is not installed or not in PATH"
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  die "Container '$CONTAINER' is not running."
fi

roles=(
  "app_readonly:APP_READONLY_PASSWORD"
  "app_writer:APP_WRITER_PASSWORD"
  "maintenance:MAINTENANCE_PASSWORD"
)

set_count=0
failed=0

for spec in "${roles[@]}"; do
  role="${spec%%:*}"
  var_name="${spec##*:}"
  pw="${!var_name-}"

  if [[ -z "$pw" ]]; then
    echo "skip: $role ($var_name is unset)"
    continue
  fi

  set_count=$((set_count + 1))

  if [[ "$MODE" == "apply" ]]; then
    echo "apply: $role from $var_name"
    docker exec -e ROLE_PASSWORD="$pw" "$CONTAINER"       psql -v ON_ERROR_STOP=1 -U "$ADMIN_USER" -d "$DB_NAME" -v pw="$pw"       -c "ALTER ROLE ${role} WITH PASSWORD :'pw';" >/dev/null
  else
    echo "check: $role using $var_name"
  fi

  current_user="$({
    docker exec       -e ROLE_PASSWORD="$pw"       -e ROLE_NAME="$role"       -e DB_NAME="$DB_NAME"       "$CONTAINER"       sh -lc 'PGPASSWORD="$ROLE_PASSWORD" psql -h 127.0.0.1 -U "$ROLE_NAME" -d "$DB_NAME" -Atqc "select current_user;"'
  } 2>/dev/null || true)"

  if [[ "$current_user" == "$role" ]]; then
    echo "ok: $role login works with $var_name"
  else
    echo "FAIL: $role login does not work with $var_name" >&2
    failed=1
  fi
done

if [[ "$set_count" -eq 0 ]]; then
  echo "No APP_*_PASSWORD variables are set in $ENV_FILE." >&2
  exit 1
fi

exit "$failed"
