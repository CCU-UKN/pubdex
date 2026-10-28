#!/usr/bin/env bash
# Gives the database roles of the running Compose deployment (compose.yaml)
# the passwords their secret files hold now: postgres, app_readonly and
# app_writer. Run it after changing a secret file -- overwritten in place or
# replaced, by any editor -- and then restart the read service, which keeps
# the password it read when it started:
#
#   ./set_passwords.sh
#   bash set_passwords.sh     # the copy the image carries, which is not executable
#   docker compose restart api
#   docker compose up --wait
#
# docker compose runs in the directory of this script, the directory of
# compose.yaml, so it reads COMPOSE_PROJECT_NAME and the other settings from
# the environment or the .env file there, as every docker compose command in
# that directory does. Arguments are options for those docker compose
# commands: -p <name> (--project-name), -f <file> (--file) and --env-file
# <file>, as you give them to your other commands for this project. The
# secret files are found as create_secrets.sh finds them: in secrets/ next to
# compose.yaml, or in the directory PEOPLE_PUBS_SECRETS_DIR names in the
# environment, never in .env.
#
# What one run does, stopping at the first problem:
#
#   1. checks the directory and the three files as `create_secrets.sh --check`
#      does, which has to lie next to this script: a directory only you can
#      enter, one usable line in each file, the role files readable for the
#      containers' users, three different passwords;
#   2. checks that the project's PostgreSQL runs;
#   3. checks that a new container of the read service, and one of ingestion,
#      reads at /run/secrets/ exactly the bytes of the host file it is about
#      to send: `docker compose run --rm --no-deps` mounts the file Compose
#      gives those services now, as it will after the restart. This fails
#      when Compose takes the files from another directory -- such as one
#      that PEOPLE_PUBS_SECRETS_DIR names in .env only -- or a container's
#      user cannot read its file;
#   4. reads the three passwords from the host's files and sends them to
#      /docker-entrypoint-initdb.d/020_role_passwords.sh --stdin in the
#      PostgreSQL container, on the standard input of `docker compose exec
#      -T`. That script checks them again, sets each with psql's \password on
#      PostgreSQL's socket -- the server receives the SCRAM verifier psql
#      computes, not the password -- and then has to open a new TCP
#      connection as each role, authenticated by its new password.
#
# Nothing changes before step 4. The files are read on the host, never in a
# running container: a container keeps the file it was started with, and
# after a replacement it would read the old password. No password is printed
# or passed on a command line; the passwords travel on standard input only,
# from shell builtins.
#
# Exit status: 0 when the database has the files' passwords and accepted a new
# connection with each; 1 when a check failed -- in steps 1 to 3 nothing was
# changed; 2 for a usage error.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPERUSER=postgres
INIT_SCRIPT=/docker-entrypoint-initdb.d/020_role_passwords.sh
CONFIRMATION="020_role_passwords.sh: $SUPERUSER, app_readonly and app_writer have the passwords from standard input, and each authenticated a new connection."
# Each application role's file, and the service that reads it.
CONSUMERS=("api app_readonly_password" "ingest app_writer_password")

usage() {
  cat <<'USAGE'
usage: set_passwords.sh [-p <name>] [-f <file>]... [--env-file <file>]

Gives the database roles postgres, app_readonly and app_writer of the running
Compose deployment the passwords that their files in secrets/ next to
compose.yaml, or in the directory PEOPLE_PUBS_SECRETS_DIR names, hold now,
and checks each with a new connection. The options go to docker compose.
Afterwards restart the read service: docker compose restart api, then
docker compose up --wait.
USAGE
}

compose_options=()
while [ $# -gt 0 ]; do
  case "$1" in
    -p|--project-name|-f|--file|--env-file)
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "set_passwords.sh: $1 needs a value" >&2
        usage >&2
        exit 2
      fi
      compose_options+=("$1" "$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "set_passwords.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

fail() {
  echo "set_passwords.sh: $*" >&2
  exit 1
}

compose() {
  ( cd "$ROOT" && docker compose ${compose_options[@]+"${compose_options[@]}"} "$@" )
}

# The commands the operator runs next, with the same options.
shown_options=""
if [ "${#compose_options[@]}" -gt 0 ]; then
  shown_options="$(printf ' %q' "${compose_options[@]}")"
fi

setting="${PEOPLE_PUBS_SECRETS_DIR:-secrets}"
case "$setting" in
  /*) dir="$setting" ;;
  *) dir="$ROOT/$setting" ;;
esac

# --- 1. the files -------------------------------------------------------------
[ -f "$ROOT/create_secrets.sh" ] \
  || fail "create_secrets.sh, which checks the secret files, is not next to this script in $ROOT"
if ! check="$(bash "$ROOT/create_secrets.sh" --check 2>&1)"; then
  printf '%s\n' "$check" >&2
  fail "the secret files in $dir are not ready; nothing was changed"
fi
echo "The secret files in $dir are usable."

# --- 2. the database ----------------------------------------------------------
if ! running="$(compose ps --status running --quiet postgres 2>&1)" || [ -z "$running" ]; then
  [ -z "$running" ] || printf '%s\n' "$running" >&2
  fail "PostgreSQL of this Compose project is not running (start it with docker compose up --wait); nothing was changed"
fi

# --- 3. what the services read -------------------------------------------------
for consumer in "${CONSUMERS[@]}"; do
  service="${consumer%% *}"
  name="${consumer#* }"
  # The comparison's status comes back as a line of its own, so that a
  # failure of docker compose itself is not taken for a difference.
  output="$(compose run --rm -T --no-deps "$service" \
              sh -c 'cmp -s "$1" -; echo "comparison status $?"' sh "/run/secrets/$name" \
              < "$dir/$name" 2>&1)" || true
  case "$output" in
    *"comparison status 0"*)
      echo "  same      $name: a new $service container reads the host's file"
      ;;
    *"comparison status 1"*)
      fail "a new $service container reads another password at /run/secrets/$name than $dir/$name holds: Compose takes the secret files from another directory. If PEOPLE_PUBS_SECRETS_DIR is set in .env, give this script the same value, PEOPLE_PUBS_SECRETS_DIR=<directory> bash set_passwords.sh; nothing was changed"
      ;;
    *)
      printf '%s\n' "$output" | tail -n 5 >&2
      fail "cannot compare /run/secrets/$name of a new $service container with $dir/$name; nothing was changed"
      ;;
  esac
done

# --- 4. the passwords -----------------------------------------------------------
# Read as the containers read the files: one trailing line break removed.
read_password() {  # read_password <file name> <variable>
  local content=""
  IFS= read -r -d '' content < "$dir/$1" || true
  if [[ "$content" == *$'\r\n' ]]; then
    content="${content%$'\r\n'}"
  elif [[ "$content" == *$'\n' ]]; then
    content="${content%$'\n'}"
  fi
  printf -v "$2" '%s' "$content"
}
read_password postgres_password superuser_password
read_password app_readonly_password readonly_password
read_password app_writer_password writer_password

status=0
output="$(printf '%s\t%s\n%s\t%s\n%s\t%s\n' "$SUPERUSER" "$superuser_password" app_readonly "$readonly_password" \
            app_writer "$writer_password" \
          | compose exec -T -u postgres postgres "$INIT_SCRIPT" --stdin 2>&1)" || status=$?
for value in "$superuser_password" "$readonly_password" "$writer_password"; do
  output="${output//"$value"/<password>}"
done
superuser_password=""
readonly_password=""
writer_password=""
case "$status:$output" in
  0:*"$CONFIRMATION"*) ;;
  *)
    printf '%s\n' "$output" >&2
    fail "the database did not confirm the new passwords (exit status $status). If 020_role_passwords.sh refused them before setting any, nothing was changed; otherwise run this script again once the cause is fixed: it sets all three each time"
    ;;
esac
printf '%s\n' "$output"
echo "The database has the passwords of the files in $dir, and a new connection with each succeeded."
echo "Next, restart the read service, which keeps the password it read when it started, and wait until it is healthy:"
echo "  docker compose$shown_options restart api"
echo "  docker compose$shown_options up --wait"
