#!/usr/bin/env bash
# The complete integration suite against its own disposable PostgreSQL.
#
# Starts a PostgreSQL container with DB/init/ mounted, waits until the schema
# has bootstrapped, and runs ./run_integration_tests.sh against it. It then
# re-applies init/010_ingestion_runtime_schema.sql to the same database and
# reruns the publication-view and role tests, so the read views and the
# grants of app_writer, app_readonly and maintenance are also proven on a
# database the runtime patch has been applied to twice. Every integration test
# has to run: a skipped test fails the run, so a broken database binding can
# never pass as an empty green run.
#
# What it touches, and what it leaves behind:
#   - one container and one data volume, both with a unique per-run name and
#     the ownership label pubdex.integration.run=<run id>. If either name is
#     already taken, the run stops instead of taking the resource over.
#   - Cleanup is registered before anything is created. It removes only the
#     resources carrying this run's label, plus the run's private temporary
#     directory, after success, a failure, the readiness timeout, SIGINT,
#     SIGTERM or SIGHUP. Further signals are ignored while it runs, so a
#     repeated interrupt cannot cut it short; it then confirms that nothing
#     is left. SIGKILL skips it, and a failed removal or a daemon that stops
#     answering can leave something behind too; in those two cases the run
#     says so and fails. Find leftovers with
#     docker ps -a --filter label=pubdex.integration.run
#   - PostgreSQL is published only on a loopback port Docker picks, the init
#     scripts are mounted read-only, and Docker Compose is never used.
#   - The superuser password is generated for this run. Docker receives it by
#     variable name and the test command only through its environment, which
#     the shell exports itself, so neither the password nor the connection
#     string appears on any command line. It is never printed and never
#     written to a file, and the test output and every container diagnostic
#     are filtered, so that a test or a log line echoing it cannot show it.
#   - The tests run in an environment from which every exported variable and
#     function has been removed; in it the test DSNs, the application DSN and
#     the libpq connection settings all name the disposable database, so no
#     inherited variable can point a test at any other database, and
#     PEOPLE_PUBS_SKIP_DOTENV=1 keeps people_pubs from reading DB/.env at all.
#   - The container lifecycle is shared with the other Docker-backed scripts
#     through lib/disposable_postgres.sh.
#
#   ./run_disposable_integration.sh                  # the complete suite
#   ./run_disposable_integration.sh -k attribution   # pytest arguments for the main run
#   INTEGRATION_PG_IMAGE=postgres:16@sha256:<digest> INTEGRATION_WAIT_SECONDS=180 ./run_disposable_integration.sh
#   (DISPOSABLE_PG_IMAGE sets the image of all three Docker-backed scripts at once)
set -euo pipefail

DB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DB_DIR/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"

IMAGE="${INTEGRATION_PG_IMAGE:-${DISPOSABLE_PG_IMAGE:-postgres:16}}"
WAIT_SECONDS="${INTEGRATION_WAIT_SECONDS:-120}"
LABEL_KEY="pubdex.integration.run"
# Rerun after the runtime patch has been re-applied: it re-creates views and
# re-issues grants.
RERUN_TESTS=(
  tests/integration/test_publication_views_integration.py
  tests/integration/test_application_roles_integration.py
)

if ! [[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: INTEGRATION_WAIT_SECONDS must be a positive whole number of seconds." >&2
  exit 2
fi
if [ ! -x "$PYTHON" ]; then
  echo "ERROR: no interpreter at .venv/bin/python" >&2
  echo "Create it from the repository root with:" >&2
  echo "  python3 -m venv .venv && .venv/bin/python -m pip install -r DB/requirements-dev.txt" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker was not found on PATH; this runner needs Docker to start a disposable PostgreSQL." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: cannot reach the Docker daemon. Start it, or check that this user may use it" >&2
  echo "       ('docker info' shows the reason)." >&2
  exit 1
fi
RESOURCE_PREFIX="pubdex-integration-"
PASSWORD_PREFIX="it"
# shellcheck source=lib/disposable_postgres.sh
source "$DB_DIR/lib/disposable_postgres.sh"
pg_begin_run
pg_make_workdir

echo "[1/5] Starting disposable PostgreSQL ($IMAGE) with DB/init/ mounted read-only (run $RUN_ID)..."
pg_start loopback

echo "[2/5] Waiting for the schema to bootstrap (${PG_INIT_SCRIPTS[*]}), up to ${WAIT_SECONDS}s..."
pg_wait_for_bootstrap
pg_report_server
pg_loopback_port
DSN="postgresql://postgres:${PGPASS}@127.0.0.1:${PORT}/people_db"

# Runs a command in a subshell whose environment holds only what the tests
# need, every connection setting bound to the disposable database. The service
# and password files point nowhere, so no libpq default can add another host
# either, and PEOPLE_PUBS_SKIP_DOTENV stops people_pubs from reading DB/.env.
# The shell exports these values itself and then execs the command, so the
# connection string and the password appear on no command line -- not even
# briefly, as they would in the arguments of `env -i NAME=value ...`.
run_isolated() (
  home="${HOME:-$WORKDIR}"
  path="$PATH"
  lang="${LANG:-C.UTF-8}"
  clear_exports
  export HOME="$home" PATH="$path" LANG="$lang" TMPDIR="$WORKDIR" \
    PYTHONDONTWRITEBYTECODE=1 PEOPLE_PUBS_SKIP_DOTENV=1 \
    PEOPLE_PUBS_INTEGRATION_DSN="$DSN" PEOPLE_DB_TEST_DSN="$DSN" PEOPLE_DB_DSN="$DSN" \
    PGHOST=127.0.0.1 PGHOSTADDR=127.0.0.1 PGPORT="$PORT" PGUSER=postgres \
    PGPASSWORD="$PGPASS" PGDATABASE=people_db PGSSLMODE=disable \
    PGSERVICEFILE=/dev/null PGSYSCONFDIR="$WORKDIR" PGPASSFILE=/dev/null
  exec "$@"
)

junit_totals() {  # "tests failures errors skipped" from a JUnit XML report
  "$PYTHON" - "$1" <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
keys = ("tests", "failures", "errors", "skipped")
print(*(sum(int(suite.get(key, 0)) for suite in suites) for key in keys))
PY
}

# run_tests <report> [pytest arguments]: the tests' own exit status; 1 if
# they passed but a test was skipped or none ran at all.
run_tests() {
  local report="$1" status totals tests failures errors skipped
  shift
  set +e
  run_isolated bash "$DB_DIR/run_integration_tests.sh" -rs -p no:cacheprovider \
    --junitxml="$report" "$@" 2>&1 | mask_password
  status="${PIPESTATUS[0]}"
  set -e
  if [ ! -s "$report" ]; then
    echo "      no test report was written (exit status $status)" >&2
    [ "$status" -ne 0 ] || status=1
    return "$status"
  fi
  if ! totals="$(junit_totals "$report")"; then
    echo "ERROR: could not read the test report." >&2
    [ "$status" -ne 0 ] || status=1
    return "$status"
  fi
  read -r tests failures errors skipped <<<"$totals"
  echo "      $tests collected: $((tests - failures - errors - skipped)) passed," \
    "$failures failed, $errors errors, $skipped skipped"
  if [ "$status" -ne 0 ]; then
    return "$status"
  fi
  if [ "$tests" -eq 0 ]; then
    echo "ERROR: no integration test ran." >&2
    return 1
  fi
  if [ "$skipped" -ne 0 ]; then
    echo "ERROR: $skipped integration test(s) were skipped; with a disposable database bound, every test has to run." >&2
    return 1
  fi
  return 0
}

echo "[3/5] Running the integration suite (run_integration_tests.sh${*:+ $*})..."
status=0
run_tests "$WORKDIR/suite.xml" "$@" || status=$?
if [ "$status" -ne 0 ]; then
  echo "FAIL: the integration suite exited with status $status." >&2
  pg_diagnose
  exit "$status"
fi

echo "[4/5] Re-applying init/010_ingestion_runtime_schema.sql to the same database..."
if ! docker exec "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -U postgres -d people_db \
     -f /docker-entrypoint-initdb.d/010_ingestion_runtime_schema.sql \
     >/dev/null 2>"$WORKDIR/reapply.log"; then
  echo "ERROR: re-applying 010_ingestion_runtime_schema.sql failed; last lines:" >&2
  grep -v 'NOTICE:' "$WORKDIR/reapply.log" | tail -20 | mask_password >&2 || true
  pg_diagnose
  exit 1
fi
echo "      re-applied cleanly"

echo "[5/5] Rerunning the publication-view and role tests after the re-application..."
status=0
run_tests "$WORKDIR/rerun.xml" "${RERUN_TESTS[@]}" || status=$?
if [ "$status" -ne 0 ]; then
  echo "FAIL: the rerun tests exited with status $status after the re-application." >&2
  pg_diagnose
  exit "$status"
fi

echo "PASS: integration suite, runtime-patch re-application and view and role rerun on a disposable database."
