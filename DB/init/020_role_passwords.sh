#!/usr/bin/env bash
# Gives the application roles their passwords from secret files, once, when
# a new database is initialised; and again on request.
#
# The PostgreSQL image runs the files in /docker-entrypoint-initdb.d/ once,
# in name order, when it initialises an empty data directory: this script
# runs after the SQL files, and 000_init.sql has created the login roles
# without a password. The Compose deployment (compose.yaml in the repository
# root) mounts two secret files into the database container and names them
# in
#
#   PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE  the read service's role, app_readonly
#   PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE    the ingestion role, app_writer
#
# With both set, app_readonly and app_writer get the passwords the files
# hold, and maintenance, which that deployment does not use, loses its
# login. With neither set -- the setup in DB/compose.yaml and the test
# databases -- nothing changes: the roles keep no password until an operator
# sets one (DB/README.md, section 2.5). Setting only one of the two is an
# error, and so are two roles with the same password or a role with the
# superuser's: each container receives only the credential of its own role.
#
# A file holds one line, the password exactly as written: one trailing line
# break is removed, and any character is allowed, spaces and punctuation
# included. An empty file, a second line, a NUL byte, more than 4096 bytes or
# text that is not UTF-8 is refused before any role changes, with a message
# that names the variable, never the content.
#
# The password never reaches the server as text: psql's \password computes
# the SCRAM verifier on the client side and sends only the verifier, so the
# password is in no SQL statement and no server log. It reaches psql through
# a pipe from shell builtins, never on a command line, and this script
# prints it nowhere. psql runs without a controlling terminal (setsid), so it
# reads the password from that pipe even where a terminal is attached.
#
# On a running server it reads no file. A running container keeps the file
# it was started with: once the file on the host is replaced -- a new file
# moved into its place, as mv and many editors save -- the container still
# reads the old one, and setting the passwords from it would set the old
# password again. set_passwords.sh (repository root, and in the application
# image) reads the host's files instead and gives them to this script on its
# standard input, as the database's operating-system user:
#
#   docker compose exec -T -u postgres postgres /docker-entrypoint-initdb.d/020_role_passwords.sh --stdin
#
# one line for each of postgres, app_readonly and app_writer: the role's
# name, a tab and its password. All three are checked first, as the files
# are, and must differ; then each is set as above, and each is tried with a
# new connection over TCP to this container's own address, which only the
# new password may authenticate: on loopback the image trusts every
# connection, so SYSTEM_USER has to show scram-sha-256 for that role.
# maintenance stays as it is. Exit status 0 on success, 1 on any failure.

ROLE_PASSWORDS_MAX_BYTES=4096
ROLE_PASSWORDS_NAME="020_role_passwords.sh"

ROLE_PASSWORDS_STDIN_MAX=16384  # three lines of a name, a tab and 4096 bytes fit

role_passwords_usage() {
  cat <<'USAGE'
usage: 020_role_passwords.sh [--stdin]

Without an argument, as when a new database is initialised: sets the
passwords of app_readonly and app_writer from the files named in
PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE and PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE
and takes the login away from maintenance; with neither variable set it
changes nothing.

--stdin: on a running server, sets the passwords of postgres, app_readonly
and app_writer from standard input, one line "<role><TAB><password>" each,
as set_passwords.sh sends them, and tries each with a new connection that
only that password authenticates.
USAGE
}

role_passwords_fail() {
  echo "$ROLE_PASSWORDS_NAME: $*" >&2
  exit 1
}

# psql on the local socket as the bootstrap superuser, as the image's own
# initialisation runs it.
role_passwords_psql() {
  PGHOST= PGHOSTADDR= psql -X -q -v ON_ERROR_STOP=1 --no-password \
    --username "$ROLE_PASSWORDS_DB_USER" --dbname "$ROLE_PASSWORDS_DB_NAME" "$@"
}

# Reads the password from the file the named variable gives into
# ROLE_PASSWORDS_VALUE, or fails naming the variable.
role_passwords_read() {
  local variable="$1" file size content
  file="${!variable}"
  if [ ! -e "$file" ]; then
    role_passwords_fail "$variable names a file that does not exist"
  fi
  if [ ! -f "$file" ]; then
    role_passwords_fail "$variable names something that is not a file"
  fi
  if [ ! -r "$file" ]; then
    role_passwords_fail "$variable names a file this user may not read; a secret file has to be readable for the database's user (the secrets section of DEPLOYMENT.md)"
  fi
  size="$(wc -c < "$file")" || role_passwords_fail "$variable names a file that cannot be read"
  size="${size//[[:space:]]/}"
  if [ "$size" -gt "$ROLE_PASSWORDS_MAX_BYTES" ]; then
    role_passwords_fail "the file $variable names is larger than $ROLE_PASSWORDS_MAX_BYTES bytes"
  fi
  if [ "$(tr -d '\000' < "$file" | wc -c | tr -d '[:space:]')" != "$size" ]; then
    role_passwords_fail "the file $variable names holds more than one line"
  fi
  content=""
  IFS= read -r -d '' content < "$file" || true
  if [[ "$content" == *$'\r\n' ]]; then
    content="${content%$'\r\n'}"
  elif [[ "$content" == *$'\n' ]]; then
    content="${content%$'\n'}"
  fi
  if [ -z "$content" ]; then
    role_passwords_fail "the file $variable names is empty"
  fi
  if [[ "$content" == *[$'\r\n']* ]]; then
    role_passwords_fail "the file $variable names holds more than one line"
  fi
  if ! printf '%s' "$content" | iconv -f UTF-8 -t UTF-8 > /dev/null 2>&1; then
    role_passwords_fail "the file $variable names is not UTF-8 text"
  fi
  ROLE_PASSWORDS_VALUE="$content"
}

# The superuser's password, where this process can see it: the image exports
# it during the first initialisation, read from its file.
role_passwords_superuser() {
  if [ -n "${POSTGRES_PASSWORD:-}" ]; then
    printf '%s' "$POSTGRES_PASSWORD"
  elif [ -n "${POSTGRES_PASSWORD_FILE:-}" ] && [ -r "$POSTGRES_PASSWORD_FILE" ]; then
    # Read as the image reads it: every trailing line break removed.
    printf '%s' "$(< "$POSTGRES_PASSWORD_FILE")"
  fi
}

role_passwords_set() {  # role_passwords_set <role> <password>
  local role="$1" password="$2" output
  if ! output="$(printf '%s\n%s\n' "$password" "$password" \
       | role_passwords_psql_detached -c "\\password $role" 2>&1)"; then
    # psql's prompts end without a line break; what follows them is its error.
    output="${output//"$password"/<password>}"
    output="${output//"Enter new password for user \"$role\": "/}"
    output="${output//"Enter it again: "/}"
    role_passwords_fail "could not set the password of $role: ${output:-psql failed}"
  fi
}

role_passwords_psql_detached() {
  PGHOST= PGHOSTADDR= setsid --wait psql -X -q -v ON_ERROR_STOP=1 --no-password \
    --username "$ROLE_PASSWORDS_DB_USER" --dbname "$ROLE_PASSWORDS_DB_NAME" "$@"
}

# Fails unless app_readonly and app_writer can log in with a SCRAM password,
# as the health check of compose.yaml requires.
role_passwords_provisioned() {
  local provisioned
  provisioned="$(role_passwords_psql -tA -c "SELECT count(*) FROM pg_catalog.pg_authid
    WHERE rolname IN ('app_readonly', 'app_writer') AND rolcanlogin
      AND rolpassword LIKE 'SCRAM-SHA-256\$%'")" \
    || role_passwords_fail "could not check the passwords that were set"
  if [ "$provisioned" != 2 ]; then
    role_passwords_fail "app_readonly and app_writer do not both have a SCRAM password after setting it"
  fi
}

# Checks a password from standard input as role_passwords_read checks a
# file, with a message that names the role, never the content.
role_passwords_check() {  # role_passwords_check <role> <password>
  local role="$1" password="$2" size
  if [ -z "$password" ]; then
    role_passwords_fail "the password for $role on standard input is empty"
  fi
  if [[ "$password" == *$'\r'* ]]; then
    role_passwords_fail "the password for $role on standard input holds more than one line"
  fi
  size="$(printf '%s' "$password" | wc -c)"
  size="${size//[[:space:]]/}"
  if [ "$size" -gt "$ROLE_PASSWORDS_MAX_BYTES" ]; then
    role_passwords_fail "the password for $role on standard input is larger than $ROLE_PASSWORDS_MAX_BYTES bytes"
  fi
  if ! printf '%s' "$password" | iconv -f UTF-8 -t UTF-8 > /dev/null 2>&1; then
    role_passwords_fail "the password for $role on standard input is not UTF-8 text"
  fi
}

# A new connection as <role> over TCP to this container's own address, with
# <password> as psql reads it from its input (setsid, as above); fails unless
# the server authenticated that role by SCRAM. Loopback would prove nothing:
# the image trusts every connection there, and SYSTEM_USER, the method and
# identity the server authenticated, is then NULL.
role_passwords_login() {  # role_passwords_login <role> <password>
  local role="$1" password="$2" address output
  address="$(hostname -i 2>/dev/null)" || address=""
  address="${address%% *}"
  if [ -z "$address" ]; then
    role_passwords_fail "cannot find this container's address to try the new password of $role"
  fi
  if ! output="$(printf '%s\n' "$password" \
       | PGHOST= PGHOSTADDR= setsid --wait psql -X -q -tA -W --host "$address" --port 5432 \
           --username "$role" --dbname "$ROLE_PASSWORDS_DB_NAME" \
           -c "SELECT current_user || ' ' || coalesce(system_user, 'without a password')" 2>&1)"; then
    output="${output//"$password"/<password>}"
    output="${output//"Password: "/}"
    role_passwords_fail "a new connection as $role with its new password failed: ${output:-psql failed}"
  fi
  # psql's prompt, without a line break, comes first.
  if [[ "$output" != *"$role scram-sha-256:$role" ]]; then
    output="${output//"$password"/<password>}"
    role_passwords_fail "a new connection as $role was not authenticated by its password: ${output//"Password: "/}"
  fi
}

# --stdin: the three passwords from standard input, checked before any role
# changes, set, and tried.
role_passwords_stdin() {
  local superuser="$ROLE_PASSWORDS_DB_USER" input="" status=0 rest line number=0 role password seen=" "
  local readonly_password="" writer_password="" superuser_password=""
  if [ -t 0 ]; then
    role_passwords_fail "--stdin reads the passwords from standard input, which is a terminal here; set_passwords.sh gives them (DEPLOYMENT.md, \"Changing a password\")"
  fi
  # Up to a NUL byte, which ends the read early, or the limit.
  IFS= read -r -d '' -n "$ROLE_PASSWORDS_STDIN_MAX" input || status=$?
  if [ "$status" -eq 0 ]; then
    if [ "${#input}" -ge "$ROLE_PASSWORDS_STDIN_MAX" ]; then
      role_passwords_fail "standard input is larger than three passwords of $ROLE_PASSWORDS_MAX_BYTES bytes"
    fi
    role_passwords_fail "standard input holds a NUL byte"
  fi
  if [ -z "$input" ]; then
    role_passwords_fail "standard input is empty; it needs one line for each of $superuser, app_readonly and app_writer"
  fi
  if [[ "$input" != *$'\n' ]]; then
    role_passwords_fail "the last line of standard input has no line break"
  fi
  rest="$input"
  input=""
  while [ -n "$rest" ]; do
    number=$((number + 1))
    line="${rest%%$'\n'*}"
    rest="${rest#*$'\n'}"
    if [[ "$line" != *$'\t'* ]]; then
      role_passwords_fail "line $number of standard input is not a role's name, a tab and its password"
    fi
    role="${line%%$'\t'*}"
    password="${line#*$'\t'}"
    case "$role" in
      "$superuser"|app_readonly|app_writer) ;;
      *) role_passwords_fail "line $number of standard input names no role this script sets ($superuser, app_readonly, app_writer)" ;;
    esac
    case "$seen" in
      *" $role "*) role_passwords_fail "standard input names $role more than once" ;;
    esac
    seen+="$role "
    role_passwords_check "$role" "$password"
    case "$role" in
      app_readonly) readonly_password="$password" ;;
      app_writer) writer_password="$password" ;;
      *) superuser_password="$password" ;;
    esac
  done
  line=""
  password=""
  for role in "$superuser" app_readonly app_writer; do
    case "$seen" in
      *" $role "*) ;;
      *) role_passwords_fail "standard input gives no password for $role; it needs one for each of $superuser, app_readonly and app_writer" ;;
    esac
  done
  if [ "$readonly_password" = "$writer_password" ]; then
    role_passwords_fail "app_readonly and app_writer must not share a password: the read service would hold the ingestion role's credential"
  fi
  if [ "$superuser_password" = "$readonly_password" ] || [ "$superuser_password" = "$writer_password" ]; then
    role_passwords_fail "an application role must not have the superuser's password"
  fi

  role_passwords_set app_readonly "$readonly_password"
  role_passwords_set app_writer "$writer_password"
  role_passwords_set "$superuser" "$superuser_password"
  role_passwords_provisioned
  role_passwords_login app_readonly "$readonly_password"
  role_passwords_login app_writer "$writer_password"
  role_passwords_login "$superuser" "$superuser_password"
  readonly_password=""
  writer_password=""
  superuser_password=""
  echo "$ROLE_PASSWORDS_NAME: $superuser, app_readonly and app_writer have the passwords from standard input, and each authenticated a new connection."
}

role_passwords_main() {
  set -euo pipefail
  local mode=init readonly_password writer_password superuser_password
  case "${1:-}" in
    "") ;;
    --stdin) mode=stdin ;;
    -h|--help) role_passwords_usage; return 0 ;;
    *) role_passwords_usage >&2; role_passwords_fail "unknown argument: $1" ;;
  esac
  if [ "$#" -gt 1 ]; then
    role_passwords_usage >&2
    role_passwords_fail "too many arguments"
  fi
  ROLE_PASSWORDS_DB_USER="${POSTGRES_USER:-postgres}"
  ROLE_PASSWORDS_DB_NAME="${POSTGRES_DB:-$ROLE_PASSWORDS_DB_USER}"
  if [ "$mode" = stdin ]; then
    role_passwords_stdin
    return 0
  fi

  if [ -z "${PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE:-}" ] && [ -z "${PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE:-}" ]; then
    echo "$ROLE_PASSWORDS_NAME: no role password files are configured; app_readonly, app_writer and maintenance keep no password."
    return 0
  fi
  if [ -z "${PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE:-}" ]; then
    role_passwords_fail "PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE is set but PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE is not; set both"
  fi
  if [ -z "${PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE:-}" ]; then
    role_passwords_fail "PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE is set but PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE is not; set both"
  fi

  # Both files are checked before any role changes.
  role_passwords_read PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE
  readonly_password="$ROLE_PASSWORDS_VALUE"
  role_passwords_read PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE
  writer_password="$ROLE_PASSWORDS_VALUE"
  ROLE_PASSWORDS_VALUE=""
  if [ "$readonly_password" = "$writer_password" ]; then
    role_passwords_fail "app_readonly and app_writer must not share a password: the read service would hold the ingestion role's credential"
  fi
  superuser_password="$(role_passwords_superuser)"
  if [ -n "$superuser_password" ] && { [ "$superuser_password" = "$readonly_password" ] || [ "$superuser_password" = "$writer_password" ]; }; then
    role_passwords_fail "an application role must not have the superuser's password"
  fi
  superuser_password=""

  role_passwords_set app_readonly "$readonly_password"
  role_passwords_set app_writer "$writer_password"
  readonly_password=""
  writer_password=""
  role_passwords_psql -c "DO \$\$ BEGIN
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'maintenance') THEN
      ALTER ROLE maintenance NOLOGIN PASSWORD NULL;
    END IF;
  END \$\$;" || role_passwords_fail "could not take the login away from maintenance"
  role_passwords_provisioned
  echo "$ROLE_PASSWORDS_NAME: app_readonly and app_writer have the passwords from their files; maintenance cannot log in."
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  role_passwords_main "$@"
else
  # The image sources a script it cannot execute, for example from a
  # checkout without file modes: a subshell keeps the options and exits
  # here from ending its initialisation early, and a failure still fails it.
  ( role_passwords_main )
fi
