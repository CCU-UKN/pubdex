#!/usr/bin/env bash
# Prepares a database on a PostgreSQL 16 server that the chart does not run
# -- its external mode -- for PubDex, as a role that need not be a
# superuser: the schema the application image carries, the passwords of the
# two application roles, and who may connect to the database.
#
# The chart runs it in its setup Job before each helm install and helm
# upgrade (templates/external-setup-job.yaml). With
# externalDatabase.setup.enabled=false an operator runs it by hand instead,
# the same way (KUBERNETES.md). It runs in the release's PostgreSQL
# image, which has psql and setsid, as any user.
#
# It reads everything from its environment:
#
#   PGHOST, PGPORT, PGDATABASE   the server and the database, which has to
#                                exist; PGPORT defaults to 5432
#   PGUSER                       the role that prepares the database
#   PGSSLMODE, PGSSLROOTCERT     TLS, as libpq reads them. PGSSLMODE
#                                defaults to verify-full here, the chart's
#                                default, not to libpq's prefer: TLS with
#                                the server's certificate checked against
#                                the CA in PGSSLROOTCERT and its name
#                                against PGHOST. A weaker mode has to be set
#                                explicitly
#   PGREQUIREAUTH                unset, empty or scram-sha-256: every
#                                connection requires SCRAM, whatever the
#                                sslmode. Another value stops the run before
#                                the server is contacted
#   PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE    a file with PGUSER's password
#   PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE   a file with app_readonly's password
#   PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE     a file with app_writer's password
#   PEOPLE_PUBS_SETUP_INIT_DIR   the schema bootstrap as python -m
#                                people_pubs.bootstrap writes it
#
# The role PGUSER has to own the database and have CREATEROLE, or be a
# superuser. The extensions citext, pgcrypto, unaccent and pg_trgm have to be
# available; PostgreSQL 16 marks all four trusted, so the database's owner
# creates them. Where one is not, someone allowed to has to create it in the
# database's schema public first.
#
# Steps, in this order; the first that fails ends the run with status 1:
#
#   1. The three password files are checked, as the application checks its
#      own: one line of UTF-8 text, 4096 bytes at most. The three passwords
#      have to differ.
#   2. One query, which changes nothing, collects what the next steps need.
#      The run stops here, before any change, and names every reason, when
#      - the server is not PostgreSQL 16;
#      - PGUSER lacks CREATEROLE or does not own the database, and is no
#        superuser;
#      - the database holds some, but not all, of the PubDex schema;
#      - app_readonly, app_writer or maintenance exists while the database
#        holds no PubDex schema. Roles belong to the whole server, not to
#        one database, so these are another installation's, or left over:
#        one PubDex installation per PostgreSQL server;
#      - the database holds the PubDex schema, but a role is missing, or
#        PGUSER lacks the ADMIN OPTION on app_readonly or app_writer that
#        setting their passwords needs;
#      - an extension cannot be created, or lies outside schema public.
#   3. On a database without the PubDex schema, the schema's SQL files run
#      in one transaction, so a failure leaves nothing behind; maintenance,
#      which the chart does not use, loses its login in the same
#      transaction. Three lines of 000_init.sql are left out: they need a
#      superuser or a role named postgres, and change nothing that this
#      setup depends on (EXTERNAL_SETUP_LEFT_OUT below). On a database that
#      holds the schema this step is skipped: that is what makes a second
#      run, for example at the next helm upgrade, safe.
#   4. In one transaction, every time: app_readonly and app_writer get the
#      passwords of their files, and only they, besides the owner, may
#      connect to the database or create temporary objects in it: CONNECT
#      and TEMPORARY are revoked from PUBLIC.
#   5. Each of the two roles has to log in with a new connection, which the
#      server has to authenticate by SCRAM.
#
# No password reaches a command line, an SQL statement, the environment of
# a program or this script's output. PGUSER's password reaches libpq through
# a password file (PGPASSFILE) in a private directory under TMPDIR, which the
# script removes when it ends. libpq uses it only in answer to the server's
# SCRAM request (PGREQUIREAUTH=scram-sha-256), for the first connection as
# for every later one: a server that asks for the password in clear text or
# as an MD5 hash ends the run before anything is sent in answer, with
# libpq's message. The roles' passwords reach psql through a pipe
# from shell builtins: \password computes each SCRAM verifier on the client,
# so the server receives only the verifier. psql runs without a controlling
# terminal (setsid), so it reads them from that pipe even where a terminal
# is attached. Messages name variables, files and roles, never a password.
#
# Exit status 0 when the database is prepared, 1 when it is not, 2 for a
# usage error.

EXTERNAL_SETUP_NAME="external-setup.sh"
EXTERNAL_SETUP_MAX_BYTES=4096
EXTERNAL_SETUP_ROLES=(app_readonly app_writer maintenance)
EXTERNAL_SETUP_SCHEMAS=(app biblio activity pii staging_raw)
EXTERNAL_SETUP_EXTENSIONS=(citext pgcrypto unaccent pg_trgm)
# Objects the first, the second and the last of the schema's files create:
# a database holds the PubDex schema when it holds these and its schemas.
EXTERNAL_SETUP_MARKERS=(
  "table biblio.publications"
  "function biblio.refresh_serving_snapshots"
  "table app.member_info_manual"
)
# The lines of 000_init.sql this setup leaves out, each matched as a whole
# line. ALTER SYSTEM and pg_reload_conf() need a superuser, and ALTER SYSTEM
# cannot run in a transaction; they set the server's password_encryption to
# scram-sha-256, which is PostgreSQL 16's default, and step 4 sets it for its
# own transaction. The GRANT needs a role named postgres, which a managed
# server need not have; that role, where it is the superuser, needs no grant.
# A line that is absent is fine; one that occurs twice stops the run.
EXTERNAL_SETUP_LEFT_OUT=(
  "ALTER SYSTEM SET password_encryption = 'scram-sha-256';"
  "SELECT pg_reload_conf();"
  "GRANT USAGE ON SCHEMA public TO postgres;"
)

external_setup_usage() {
  cat <<'USAGE'
usage: external-setup.sh

Prepares the database PGDATABASE on the PostgreSQL 16 server PGHOST:PGPORT
for PubDex, as the role PGUSER: the schema of PEOPLE_PUBS_SETUP_INIT_DIR
once, then on every run the passwords of app_readonly and app_writer from
PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE and PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE
and the database's CONNECT privilege. PGUSER's password is read from
PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE. The header of this script lists every
check and step.
USAGE
}

external_setup_fail() {
  echo "$EXTERNAL_SETUP_NAME: $*" >&2
  exit 1
}

external_setup_say() {
  echo "$EXTERNAL_SETUP_NAME: $*"
}

# Reads the password in the file the named variable gives into
# EXTERNAL_SETUP_VALUE, or fails naming the variable, never the content.
external_setup_read_password() {
  local variable="$1" file size content
  file="${!variable:-}"
  if [ -z "$file" ]; then
    external_setup_fail "$variable is not set"
  fi
  if [ ! -e "$file" ]; then
    external_setup_fail "$variable names a file that does not exist: $file. In the chart's setup Job, the Secret that holds it is missing or lacks the key"
  fi
  if [ ! -f "$file" ]; then
    external_setup_fail "$variable names something that is not a file: $file"
  fi
  if [ ! -r "$file" ]; then
    external_setup_fail "$variable names a file this user may not read: $file"
  fi
  size="$(wc -c < "$file")" || external_setup_fail "$variable names a file that cannot be read: $file"
  size="${size//[[:space:]]/}"
  if [ "$size" -gt "$EXTERNAL_SETUP_MAX_BYTES" ]; then
    external_setup_fail "the file $variable names is larger than $EXTERNAL_SETUP_MAX_BYTES bytes"
  fi
  if [ "$(tr -d '\000' < "$file" | wc -c | tr -d '[:space:]')" != "$size" ]; then
    external_setup_fail "the file $variable names holds more than one line"
  fi
  content=""
  IFS= read -r -d '' content < "$file" || true
  if [[ "$content" == *$'\r\n' ]]; then
    content="${content%$'\r\n'}"
  elif [[ "$content" == *$'\n' ]]; then
    content="${content%$'\n'}"
  fi
  if [ -z "$content" ]; then
    external_setup_fail "the file $variable names is empty"
  fi
  if [[ "$content" == *[$'\r\n']* ]]; then
    external_setup_fail "the file $variable names holds more than one line"
  fi
  if ! printf '%s' "$content" | iconv -f UTF-8 -t UTF-8 > /dev/null 2>&1; then
    external_setup_fail "the file $variable names is not UTF-8 text"
  fi
  EXTERNAL_SETUP_VALUE="$content"
}

# Output of a program with each password replaced, for a message.
external_setup_mask() {
  local text="$1" secret
  for secret in "${EXTERNAL_SETUP_ADMIN_PASSWORD:-}" "${EXTERNAL_SETUP_READONLY_PASSWORD:-}" \
                "${EXTERNAL_SETUP_WRITER_PASSWORD:-}"; do
    if [ -n "$secret" ]; then
      text="${text//"$secret"/<password>}"
    fi
  done
  printf '%s' "$text"
}

# One field of a libpq password file: backslash and colon escaped.
external_setup_pgpass_field() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//:/\\:}"
  printf '%s' "$value"
}

# Copies 000_init.sql without the lines of EXTERNAL_SETUP_LEFT_OUT, each
# matched as a whole line; prints how many it left out. Fails when one of
# them occurs more than once.
external_setup_filter_init() {  # external_setup_filter_init <source> <target>
  local source="$1" target="$2" line count left_out=0 status=0 before after patterns=()
  if [ ! -f "$source" ] || [ ! -r "$source" ]; then
    external_setup_fail "cannot read $source"
  fi
  for line in "${EXTERNAL_SETUP_LEFT_OUT[@]}"; do
    count="$(grep -cxF -e "$line" "$source")" || [ "$count" = 0 ] \
      || external_setup_fail "cannot read $source"
    if [ "$count" -gt 1 ]; then
      external_setup_fail "$(basename "$source") holds the line \"$line\" $count times; this setup leaves it out only where it occurs once"
    fi
    left_out=$((left_out + count))
    patterns+=(-e "$line")
  done
  grep -vxF "${patterns[@]}" "$source" > "$target" || status=$?
  if [ "$status" -gt 1 ]; then
    external_setup_fail "cannot copy $source"
  fi
  before="$(wc -l < "$source")"
  after="$(wc -l < "$target")"
  if [ "$((before - after))" -ne "$left_out" ]; then
    external_setup_fail "the copy of $(basename "$source") lacks other lines than the $left_out it leaves out"
  fi
  echo "$left_out"
}

# Its arguments, separated by commas.
external_setup_join() {
  local IFS=$'\037' joined
  joined="$*"
  printf '%s' "${joined//$'\037'/, }"
}

# psql as PGUSER, whose password libpq reads from PGPASSFILE and sends only
# by SCRAM (PGREQUIREAUTH, exported by external_setup_main for every psql).
external_setup_psql() {
  psql -X -q -v ON_ERROR_STOP=1 --no-password "$@"
}

# What the decision needs, as "key<TAB>value" lines; changes nothing.
EXTERNAL_SETUP_FACTS_QUERY="
WITH me AS (
  SELECT oid, rolsuper, rolcreaterole FROM pg_catalog.pg_roles WHERE rolname = current_user
)
SELECT 'version', current_setting('server_version_num')
UNION ALL SELECT 'superuser', CASE WHEN me.rolsuper THEN 'yes' ELSE 'no' END FROM me
UNION ALL SELECT 'createrole', CASE WHEN me.rolcreaterole THEN 'yes' ELSE 'no' END FROM me
UNION ALL SELECT 'owner', CASE WHEN d.datdba = me.oid THEN 'yes' ELSE 'no' END
  FROM me, pg_catalog.pg_database d WHERE d.datname = current_database()
UNION ALL SELECT 'tls', coalesce((SELECT CASE WHEN s.ssl THEN s.version END
  FROM pg_catalog.pg_stat_ssl s WHERE s.pid = pg_catalog.pg_backend_pid()), 'none')
UNION ALL SELECT 'schema:' || s, CASE WHEN n.oid IS NULL THEN 'no' ELSE 'yes' END
  FROM unnest(ARRAY['app', 'biblio', 'activity', 'pii', 'staging_raw']) AS s
  LEFT JOIN pg_catalog.pg_namespace n ON n.nspname = s
UNION ALL SELECT 'marker:table biblio.publications', CASE WHEN EXISTS (
  SELECT FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'biblio' AND c.relname = 'publications') THEN 'yes' ELSE 'no' END
UNION ALL SELECT 'marker:function biblio.refresh_serving_snapshots', CASE WHEN EXISTS (
  SELECT FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'biblio' AND p.proname = 'refresh_serving_snapshots') THEN 'yes' ELSE 'no' END
UNION ALL SELECT 'marker:table app.member_info_manual', CASE WHEN EXISTS (
  SELECT FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'app' AND c.relname = 'member_info_manual') THEN 'yes' ELSE 'no' END
UNION ALL SELECT 'role:' || r, CASE WHEN ro.oid IS NULL THEN 'absent'
    WHEN pg_catalog.pg_has_role(current_user, ro.oid, 'MEMBER WITH ADMIN OPTION') THEN 'admin'
    ELSE 'present' END
  FROM unnest(ARRAY['app_readonly', 'app_writer', 'maintenance']) AS r
  LEFT JOIN pg_catalog.pg_roles ro ON ro.rolname = r
UNION ALL SELECT 'extension:' || e, coalesce(
    (SELECT 'installed:' || n.nspname FROM pg_catalog.pg_extension x
       JOIN pg_catalog.pg_namespace n ON n.oid = x.extnamespace WHERE x.extname = e),
    (SELECT CASE WHEN v.trusted THEN 'trusted' ELSE 'untrusted' END
       FROM pg_catalog.pg_available_extensions a
       JOIN pg_catalog.pg_available_extension_versions v ON v.name = a.name AND v.version = a.default_version
      WHERE a.name = e),
    'unavailable')
  FROM unnest(ARRAY['citext', 'pgcrypto', 'unaccent', 'pg_trgm']) AS e
"

# Fills EXTERNAL_SETUP_FACT from "key<TAB>value" lines.
declare -gA EXTERNAL_SETUP_FACT=()
external_setup_read_facts() {
  local key value
  EXTERNAL_SETUP_FACT=()
  while IFS=$'\t' read -r key value; do
    if [ -n "$key" ]; then
      EXTERNAL_SETUP_FACT["$key"]="$value"
    fi
  done <<< "$1"
}

# Decides from EXTERNAL_SETUP_FACT whether the database can be prepared:
# sets EXTERNAL_SETUP_STATE to absent (no PubDex schema yet) or present, or
# fails with every reason that stops it.
external_setup_decide() {
  local user="$1" database="$2" reasons=() version superuser item state have=0 missing=() found=()
  local role extension value total=$(( ${#EXTERNAL_SETUP_SCHEMAS[@]} + ${#EXTERNAL_SETUP_MARKERS[@]} ))
  version="${EXTERNAL_SETUP_FACT[version]:-}"
  if ! [[ "$version" =~ ^[0-9]+$ ]]; then
    external_setup_fail "the server reported no version; is $database a PostgreSQL database?"
  fi
  if [ "$((version / 10000))" -ne 16 ]; then
    reasons+=("the server runs PostgreSQL $((version / 10000)).$((version % 10000)); PubDex supports PostgreSQL 16 only")
  fi
  superuser="${EXTERNAL_SETUP_FACT[superuser]:-no}"
  if [ "$superuser" != yes ]; then
    if [ "${EXTERNAL_SETUP_FACT[createrole]:-no}" != yes ]; then
      reasons+=("the role $user lacks CREATEROLE, which creating the application roles and setting their passwords needs")
    fi
    if [ "${EXTERNAL_SETUP_FACT[owner]:-no}" != yes ]; then
      reasons+=("the role $user does not own the database $database; the schema and its privileges need the owner")
    fi
  fi

  for item in "${EXTERNAL_SETUP_SCHEMAS[@]/#/schema:}" "${EXTERNAL_SETUP_MARKERS[@]/#/marker:}"; do
    if [ "${EXTERNAL_SETUP_FACT[$item]:-no}" = yes ]; then
      have=$((have + 1))
      found+=("${item#*:}")
    else
      missing+=("${item#*:}")
    fi
  done
  if [ "$have" -eq 0 ]; then
    state=absent
  elif [ "$have" -eq "$total" ]; then
    state=present
  else
    state=partial
    reasons+=("the database $database holds part of the PubDex schema, or objects of other software with its names: it has $(external_setup_join "${found[@]}") but not $(external_setup_join "${missing[@]}"). PubDex needs a database of its own")
  fi

  if [ "$state" = absent ]; then
    found=()
    for role in "${EXTERNAL_SETUP_ROLES[@]}"; do
      if [ "${EXTERNAL_SETUP_FACT[role:$role]:-absent}" != absent ]; then
        found+=("$role")
      fi
    done
    if [ "${#found[@]}" -gt 0 ]; then
      reasons+=("the role(s) $(external_setup_join "${found[@]}") exist on this server, but the database $database holds no PubDex schema. Roles belong to the whole server: these belong to another PubDex installation on it, or were left over. One PubDex installation per PostgreSQL server; drop the roles only if nothing uses them")
    fi
    for extension in "${EXTERNAL_SETUP_EXTENSIONS[@]}"; do
      value="${EXTERNAL_SETUP_FACT[extension:$extension]:-unavailable}"
      case "$value" in
        installed:public|trusted) ;;
        installed:*) reasons+=("the extension $extension is installed in schema ${value#installed:} of $database; PubDex needs it in schema public") ;;
        untrusted)
          if [ "$superuser" != yes ]; then
            reasons+=("the extension $extension is not trusted on this server, so only a superuser can create it: have one run CREATE EXTENSION $extension; in the database $database first")
          fi ;;
        *) reasons+=("the extension $extension is not available on this server") ;;
      esac
    done
  elif [ "$state" = present ]; then
    for role in "${EXTERNAL_SETUP_ROLES[@]}"; do
      value="${EXTERNAL_SETUP_FACT[role:$role]:-absent}"
      if [ "$value" = absent ]; then
        reasons+=("the database $database holds the PubDex schema, but the role $role does not exist on this server")
      elif [ "$value" != admin ] && [ "$superuser" != yes ] && [ "$role" != maintenance ]; then
        reasons+=("the role $user lacks the ADMIN OPTION on $role, which setting its password needs: run this as the role that created the PubDex schema, or have that role run GRANT $role TO $user WITH ADMIN OPTION")
      fi
    done
  fi

  if [ "${#reasons[@]}" -gt 0 ]; then
    echo "$EXTERNAL_SETUP_NAME: the database $database cannot be prepared; nothing was changed:" >&2
    for item in "${reasons[@]}"; do
      echo "  - $item" >&2
    done
    exit 1
  fi
  EXTERNAL_SETUP_STATE="$state"
}

# Step 3: the schema in one transaction, and maintenance without a login.
external_setup_apply_schema() {
  local init_dir="$1" work="$2" file left_out output files=() names=()
  left_out="$(external_setup_filter_init "$init_dir/000_init.sql" "$work/000_init.sql")" || exit 1
  for file in "$init_dir"/*.sql; do
    if [ "$(basename "$file")" = 000_init.sql ]; then
      files+=(-f "$work/000_init.sql")
    else
      files+=(-f "$file")
    fi
    names+=("$(basename "$file")")
  done
  # Only warnings and errors, on the error output: the files' notices and
  # query results would bury the one line that says what failed.
  if ! output="$(PGOPTIONS="${PGOPTIONS:+$PGOPTIONS }-c client_min_messages=warning" \
       external_setup_psql --single-transaction "${files[@]}" \
       -c "ALTER ROLE maintenance NOLOGIN PASSWORD NULL" 2>&1 >/dev/null)"; then
    external_setup_fail "the schema could not be applied, and nothing of it was kept: $(external_setup_mask "$output")"
  fi
  external_setup_say "applied the schema in one transaction: ${names[*]} ($left_out line(s) of 000_init.sql left out); maintenance cannot log in"
}

# Step 4: both passwords and the database's CONNECT privilege, in one
# transaction. \password asks for each password twice, on its input.
external_setup_set_passwords() {
  local output
  if ! output="$(printf '%s\n%s\n%s\n%s\n' \
         "$EXTERNAL_SETUP_READONLY_PASSWORD" "$EXTERNAL_SETUP_READONLY_PASSWORD" \
         "$EXTERNAL_SETUP_WRITER_PASSWORD" "$EXTERNAL_SETUP_WRITER_PASSWORD" \
       | setsid --wait psql -X -q -v ON_ERROR_STOP=1 --no-password --single-transaction \
           -c "SET LOCAL password_encryption = 'scram-sha-256'" \
           -c "\\password app_readonly" \
           -c "\\password app_writer" \
           -c "DO \$\$ BEGIN
                 EXECUTE format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
                 EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_readonly, app_writer', current_database());
               END \$\$" 2>&1)"; then
    output="${output//"Enter new password for user \"app_readonly\": "/}"
    output="${output//"Enter new password for user \"app_writer\": "/}"
    output="${output//"Enter it again: "/}"
    external_setup_fail "the passwords could not be set, and none was changed: $(external_setup_mask "${output:-psql failed}")"
  fi
  external_setup_say "set the passwords of app_readonly and app_writer (SCRAM, computed here); only they and the owner may connect to $PGDATABASE"
}

# Step 5: a new connection as <role> with its password, which libpq allows
# only when the server authenticates it by SCRAM (require_auth). SYSTEM_USER
# shows the method of the server's pg_hba.conf line and the authenticated
# role: scram-sha-256, or md5, which runs SCRAM for a SCRAM verifier.
external_setup_login() {  # external_setup_login <role> <password>
  local role="$1" password="$2" output errors="$EXTERNAL_SETUP_WORK/login.err"
  # psql's prompt, "Password: ", goes to the error output.
  if ! output="$(printf '%s\n' "$password" \
       | PGUSER="$role" PGPASSFILE="$EXTERNAL_SETUP_WORK/no-password-file" PGREQUIREAUTH=scram-sha-256 \
         setsid --wait psql -X -q -tA -W -v ON_ERROR_STOP=1 \
           -c "SELECT current_user || ' ' || coalesce(system_user, 'without authentication')" 2>"$errors")"; then
    output="$(cat "$errors" 2>/dev/null)" || output=""
    output="${output//"Password: "/}"
    external_setup_fail "a new connection as $role with its new password failed: $(external_setup_mask "${output:-psql failed}")"
  fi
  case "$output" in
    "$role scram-sha-256:$role"|"$role md5:$role") ;;
    *) external_setup_fail "a new connection as $role was not authenticated by its password: $(external_setup_mask "$output")" ;;
  esac
}

external_setup_cleanup() {
  if [ -n "${EXTERNAL_SETUP_WORK:-}" ]; then
    rm -rf "$EXTERNAL_SETUP_WORK"
  fi
}

external_setup_main() {
  set -euo pipefail
  local variable role facts version
  case "${1:-}" in
    "") ;;
    -h|--help) external_setup_usage; return 0 ;;
    *) external_setup_usage >&2; echo "$EXTERNAL_SETUP_NAME: unknown argument: $1" >&2; exit 2 ;;
  esac
  for variable in PGHOST PGDATABASE PGUSER PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE \
                  PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE \
                  PEOPLE_PUBS_SETUP_INIT_DIR; do
    if [ -z "${!variable:-}" ]; then
      external_setup_usage >&2
      echo "$EXTERNAL_SETUP_NAME: $variable is not set" >&2
      exit 2
    fi
  done
  for role in "${EXTERNAL_SETUP_ROLES[@]}"; do
    if [ "$PGUSER" = "$role" ]; then
      external_setup_fail "PGUSER is $role, one of the roles this setup creates; prepare the database as its owner"
    fi
  done
  if [ ! -f "$PEOPLE_PUBS_SETUP_INIT_DIR/000_init.sql" ]; then
    external_setup_fail "PEOPLE_PUBS_SETUP_INIT_DIR holds no 000_init.sql: $PEOPLE_PUBS_SETUP_INIT_DIR"
  fi
  export LC_ALL=C PGCLIENTENCODING=UTF8 PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
  export PGAPPNAME="${PGAPPNAME:-pubdex-external-setup}" PGGSSENCMODE="${PGGSSENCMODE:-disable}"
  export PGSSLMODE="${PGSSLMODE:-verify-full}"
  # Every psql this script starts, the admin's as well as the two roles',
  # answers the server only if it authenticates by SCRAM (libpq's
  # require_auth): a server, or a host that poses as it, that asks for a
  # password in clear text or as an MD5 hash gets none, whatever the sslmode.
  # No value from the environment may weaken that.
  case "${PGREQUIREAUTH:-}" in
    ""|scram-sha-256) ;;
    *) external_setup_fail "PGREQUIREAUTH is \"$PGREQUIREAUTH\", but this setup lets the server authenticate its connections by SCRAM only: unset PGREQUIREAUTH or set it to scram-sha-256" ;;
  esac
  export PGREQUIREAUTH=scram-sha-256
  # The password file below is the admin's only password source.
  unset PGPASSWORD PGSERVICE PGSERVICEFILE

  # Step 1: every password file before the server is contacted.
  external_setup_read_password PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE
  EXTERNAL_SETUP_ADMIN_PASSWORD="$EXTERNAL_SETUP_VALUE"
  external_setup_read_password PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE
  EXTERNAL_SETUP_READONLY_PASSWORD="$EXTERNAL_SETUP_VALUE"
  external_setup_read_password PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE
  EXTERNAL_SETUP_WRITER_PASSWORD="$EXTERNAL_SETUP_VALUE"
  EXTERNAL_SETUP_VALUE=""
  if [ "$EXTERNAL_SETUP_READONLY_PASSWORD" = "$EXTERNAL_SETUP_WRITER_PASSWORD" ]; then
    external_setup_fail "app_readonly and app_writer must not share a password: the read service would hold the ingestion role's credential"
  fi
  if [ "$EXTERNAL_SETUP_ADMIN_PASSWORD" = "$EXTERNAL_SETUP_READONLY_PASSWORD" ] \
     || [ "$EXTERNAL_SETUP_ADMIN_PASSWORD" = "$EXTERNAL_SETUP_WRITER_PASSWORD" ]; then
    external_setup_fail "an application role must not have the password of $PGUSER"
  fi

  EXTERNAL_SETUP_WORK="$(mktemp -d "${TMPDIR:-/tmp}/external-setup.XXXXXX")" \
    || external_setup_fail "cannot create a private directory under ${TMPDIR:-/tmp}"
  trap external_setup_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  export PGPASSFILE="$EXTERNAL_SETUP_WORK/admin.pgpass"
  ( umask 077
    printf '*:*:*:%s:%s\n' "$(external_setup_pgpass_field "$PGUSER")" \
      "$(external_setup_pgpass_field "$EXTERNAL_SETUP_ADMIN_PASSWORD")" > "$PGPASSFILE" ) \
    || external_setup_fail "cannot write the password file under ${TMPDIR:-/tmp}"

  # Step 2: what the server and the database are, before any change.
  if ! facts="$(external_setup_psql -tA -F $'\t' -c "$EXTERNAL_SETUP_FACTS_QUERY" 2>&1)"; then
    external_setup_fail "cannot connect to $PGHOST:${PGPORT:-5432}, database $PGDATABASE, as $PGUSER: $(external_setup_mask "$facts")"
  fi
  external_setup_read_facts "$facts"
  version="${EXTERNAL_SETUP_FACT[version]:-}"
  if [[ "$version" =~ ^[0-9]+$ ]]; then
    version="$((version / 10000)).$((version % 10000))"
  fi
  external_setup_say "connected to $PGHOST:${PGPORT:-5432}, database $PGDATABASE, as $PGUSER: PostgreSQL ${version:-of unknown version}, sslmode $PGSSLMODE, TLS ${EXTERNAL_SETUP_FACT[tls]:-none}"
  external_setup_decide "$PGUSER" "$PGDATABASE"

  # Step 3.
  if [ "$EXTERNAL_SETUP_STATE" = absent ]; then
    external_setup_apply_schema "$PEOPLE_PUBS_SETUP_INIT_DIR" "$EXTERNAL_SETUP_WORK"
  else
    external_setup_say "the database holds the PubDex schema already; the schema step is skipped"
  fi
  # Steps 4 and 5.
  external_setup_set_passwords
  external_setup_login app_readonly "$EXTERNAL_SETUP_READONLY_PASSWORD"
  external_setup_login app_writer "$EXTERNAL_SETUP_WRITER_PASSWORD"
  EXTERNAL_SETUP_ADMIN_PASSWORD=""
  EXTERNAL_SETUP_READONLY_PASSWORD=""
  EXTERNAL_SETUP_WRITER_PASSWORD=""
  external_setup_say "app_readonly and app_writer each authenticated a new connection by SCRAM; the database $PGDATABASE is ready for PubDex"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  external_setup_main "$@"
fi
