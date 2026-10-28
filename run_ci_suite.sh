#!/usr/bin/env bash
# The complete verification suite, in one command: what public CI runs, and
# what anyone can run locally from a clean checkout.
#
# Stages, in this order; the first stage that fails stops the run with that
# stage's exit status:
#   1. the default offline check suite (run_checks.sh): compilation, Markdown
#      consistency, the offline unit and source-fixture tests and, given
#      --base, the transform-version guard over that commit range;
#   2. committed secrets and local configuration
#      (check_secrets_and_local_config.py) over the publishable tree and,
#      given --base, the files each commit of that range adds or changes;
#   3. schema bootstrap from an empty database and re-application of the
#      runtime patch (DB/run_migration_smoke.sh);
#   4. the task-1a clean-clone demonstration on the bundled fixtures
#      (DB/run_task_1a_demo.sh);
#   5. the complete integration suite on its own disposable PostgreSQL
#      (DB/run_disposable_integration.sh);
#   6. the acceptance test of the Compose deployment (run_stack_acceptance.sh):
#      the application image built from this checkout, compose.yaml started
#      in a Compose project of its own, the synthetic demonstration ingested
#      inside it and read back over HTTP, readiness, persistence and secrets;
#   7. the operator's workflow on that deployment (run_operator_acceptance.sh):
#      the stack configured by an .env file on the ordinary network, a private
#      roster on standard input, ingestion of the bundled payloads, refresh,
#      read service and export, and both documented ways of changing the
#      application roles' passwords.
#
# Only synthetic fixtures and disposable databases are used. Stages 3-5 each
# start their own uniquely named and labelled PostgreSQL container and volume,
# published on loopback at most, and remove exactly those again when the stage
# ends, also after a failure or an interrupt; a stage whose cleanup cannot be
# confirmed says how to find the leftovers and fails. Stages 6 and 7 do the
# same for their Compose projects -- containers, networks and volume, found by
# the project's name of the run's own and, in stage 6, by its run label --
# the image tag each built and their secret files, with the read service on
# a loopback port. Stage 6 puts PostgreSQL and ingestion on a network without
# a route off the host; stage 7 keeps the ordinary network of compose.yaml
# and runs only commands that contact no metadata provider. Only stages 6 and
# 7 use Docker Compose: stage 6 with its project's explicit name, files and
# settings, stage 7 with a synthetic .env file of its own in a directory of
# its own. No stage connects to an existing database or a metadata provider,
# and in
# the pytest runs of stages 1 and 5 a network guard refuses any other network
# access by the tests and the Python processes they start (a guard inside
# Python, not network isolation; see DB/TESTING_STRATEGY.md for its
# boundaries). No stage reads DB/.env: the stage-1 tests run in a
# cleared environment (DB/run_tests.sh), and they and every Python step of
# stages 4 and 5 run with PEOPLE_PUBS_SKIP_DOTENV=1; the image of stages 6
# and 7 reads no .env file at all. This script creates
# nothing itself, so it has nothing of its own to clean up. An interrupt from
# the terminal or an external timeout reaches the running stage directly,
# which cleans up at once; a signal sent to this script alone takes effect when
# the running stage returns, and no later stage starts.
#
# Needs Bash, a Git checkout, .venv with DB/requirements-dev.txt installed and,
# for stages 3-7, a Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2, with
# the Compose plugin 2.24.0 or later (DB/lib/docker_versions.sh says why), which
# this user can reach and which runs on the same machine: stages 3-5
# bind-mount DB/init/ from this checkout, stages 6 and 7 files from the
# temporary directory, and they publish PostgreSQL and the read service on 127.0.0.1
# (DB/TESTING_STRATEGY.md, "Running the suite elsewhere"). Stages 6 and 7
# need network access to build the image unless Docker has its inputs cached.
# ./run_checks.sh remains the fast offline command for pre-commit and everyday
# use.
#
#   ./run_ci_suite.sh                              # local run
#   ./run_ci_suite.sh --base <sha> [--head <sha>]  # CI: a pushed or proposed range
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PYTHON="$ROOT/.venv/bin/python"
STAGES=7
# shellcheck source=DB/lib/docker_versions.sh
source "$ROOT/DB/lib/docker_versions.sh"

usage() {
  cat <<'USAGE'
usage: run_ci_suite.sh [--base <sha> [--head <sha>]]

Runs the complete verification suite, stopping at the first failing stage
with that stage's exit status:

  1. offline check suite                          run_checks.sh
  2. committed secrets and local configuration    check_secrets_and_local_config.py
  3. schema bootstrap from an empty database      DB/run_migration_smoke.sh
  4. task-1a clean-clone demonstration            DB/run_task_1a_demo.sh
  5. integration suite on disposable PostgreSQL   DB/run_disposable_integration.sh
  6. Compose deployment acceptance                run_stack_acceptance.sh
  7. operator workflow on the deployment          run_operator_acceptance.sh

  --base <sha> [--head <sha>]  also check this commit range (--head defaults
                               to HEAD): the transform-version guard in stage 1
                               and, in stage 2, the files each commit adds or
                               changes. Both are handed to run_checks.sh and to
                               the secrets check unchanged. An empty or all-zero
                               <sha> -- the first push of a branch -- has no
                               range for the guard, and makes the secrets check
                               read every commit reachable from the head.
  -h, --help                   show this help

Needs a Git checkout, .venv with DB/requirements-dev.txt installed, and a
reachable Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2, with the
Compose plugin 2.24.0 or later. ./run_checks.sh alone runs the offline part.
USAGE
}

die_usage() {
  echo "run_ci_suite.sh: $1" >&2
  usage >&2
  exit 2
}

base_given=0
base=""
head_given=0
head_ref=""
while [ $# -gt 0 ]; do
  case "$1" in
    --base)
      if [ $# -lt 2 ]; then die_usage "--base needs a value"; fi
      base_given=1
      base="$2"
      shift 2
      ;;
    --head)
      if [ $# -lt 2 ]; then die_usage "--head needs a value"; fi
      head_given=1
      head_ref="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --staged)
      die_usage "--staged is the pre-commit mode of ./run_checks.sh, not of the complete suite"
      ;;
    *)
      die_usage "unknown argument: $1"
      ;;
  esac
done
if [ "$head_given" = 1 ] && [ "$base_given" = 0 ]; then
  die_usage "--head requires --base"
fi

# Handed to run_checks.sh and the secrets check exactly as given, empty values
# included.
range_args=()
if [ "$base_given" = 1 ]; then
  range_args+=(--base "$base")
  if [ "$head_given" = 1 ]; then
    range_args+=(--head "$head_ref")
  fi
fi

missing() {
  echo "run_ci_suite.sh: $1" >&2
  shift
  local hint
  for hint in "$@"; do
    echo "  $hint" >&2
  done
  exit 1
}

echo "Checking prerequisites..."
if [ ! -x "$PYTHON" ]; then
  missing "no interpreter at .venv/bin/python. Create it from the repository root with:" \
    "./setup_venv.sh"
fi
if ! "$PYTHON" -c 'import psycopg, psycopg_pool, pytest, starlette, uvicorn' >/dev/null 2>&1; then
  missing ".venv/bin/python lacks the development requirements. Remove .venv and create it again with:" \
    "./setup_venv.sh"
fi
if ! command -v git >/dev/null 2>&1; then
  missing "git was not found on PATH; the checks list the publishable files from a Git checkout."
fi
if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  missing "this directory is not a Git checkout; run the suite in a clone of the repository."
fi
if ! command -v docker >/dev/null 2>&1; then
  missing "docker was not found on PATH. Stages 3-7 start disposable containers." \
    "Install Docker Engine, or run ./run_checks.sh for the offline part alone."
fi
if ! docker info >/dev/null 2>&1; then
  missing "cannot reach the Docker daemon. Start it, or check that this user may use it" \
    "('docker info' shows the reason)."
fi
engine_version="$(docker version --format '{{.Server.Version}}' 2>/dev/null)" || engine_version=""
problem="$(engine_problem "$engine_version")"
if [ -n "$problem" ]; then
  missing "$problem: stages 6 and 7 build the application image and start the deployment," \
    "which need Docker Engine $ENGINE_SUPPORTED: other Engines start it too, but can let other hosts" \
    "reach the ports it publishes on 127.0.0.1 (DB/lib/docker_versions.sh says why)." \
    "Upgrade the engine, or run the stages before them on their own."
fi
compose_version="$(docker compose version --short 2>/dev/null)" || compose_version=""
if [ -z "$compose_version" ]; then
  missing "the Docker Compose plugin was not found ('docker compose version' fails); stages 6 and 7 need it." \
    "Install the Compose plugin, version $COMPOSE_MINIMUM or later (docker-compose-plugin of Docker's packages, docker-compose-v2 on Ubuntu, docker-compose on Debian 13, or Docker's" \
    "installation guide), so that 'docker compose' works for this user."
fi
problem="$(version_problem "Docker Compose" "$compose_version" "$COMPOSE_MINIMUM")"
if [ -n "$problem" ]; then
  missing "$problem; stages 6 and 7 need the Compose plugin $COMPOSE_MINIMUM or later," \
    "the first release that applies the health checks of compose.yaml as written." \
    "Upgrade the Compose plugin (docker-compose-plugin of Docker's packages, docker-compose-v2 on Ubuntu, docker-compose on Debian 13, or Docker's installation guide)."
fi

# What this run uses that the repository does not pin, so that its log
# records it; each Docker-backed stage adds the PostgreSQL image digest and
# server version (DB/TESTING_STRATEGY.md, "Reproducing a verified run").
echo "Environment:"
echo "  system  $(uname -srm 2>/dev/null || echo unknown)"
echo "  bash    $BASH_VERSION"
echo "  python  $("$PYTHON" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo unknown)"
echo "  pip     $("$PYTHON" -m pip --version 2>/dev/null | cut -d' ' -f2 || echo unknown)"
echo "  psycopg $("$PYTHON" -c 'import psycopg; print(psycopg.__version__, psycopg.pq.__impl__, "implementation, libpq", psycopg.pq.version())' 2>/dev/null || echo unknown)"
echo "  git     $(git --version 2>/dev/null | cut -d' ' -f3 || echo unknown)"
echo "  docker  $engine_version"
echo "  compose $compose_version"

# No stage takes database settings from the caller: stage 1 runs its tests in
# a cleared environment (DB/run_tests.sh), stages 3-5 bind their own, and
# stages 6 and 7 hand Compose only their run's own files and settings.

# A signal that reaches this script alone takes effect once the running stage
# has returned (and cleaned up); no later stage starts.
trap 'echo "run_ci_suite.sh: interrupted; no further stage runs." >&2; exit 130' INT
trap 'echo "run_ci_suite.sh: terminated; no further stage runs." >&2; exit 143' TERM
trap 'exit 129' HUP

suite_started=$SECONDS
run_stage() {  # run_stage <number> <title> <command> [arguments]
  local number="$1" title="$2" stage_started=$SECONDS status=0
  shift 2
  echo
  echo "==> Stage $number/$STAGES: $title"
  "$@" || status=$?
  if [ "$status" -ne 0 ]; then
    echo >&2
    echo "run_ci_suite.sh: stage $number/$STAGES ($title) failed with exit status $status;" \
      "later stages were not run." >&2
    exit "$status"
  fi
  echo "==> Stage $number/$STAGES passed in $((SECONDS - stage_started))s: $title"
}

run_stage 1 "offline check suite (run_checks.sh)" \
  bash "$ROOT/run_checks.sh" ${range_args[@]+"${range_args[@]}"}
run_stage 2 "committed secrets and local configuration (check_secrets_and_local_config.py)" \
  "$PYTHON" "$ROOT/check_secrets_and_local_config.py" ${range_args[@]+"${range_args[@]}"}
run_stage 3 "schema bootstrap from an empty database (DB/run_migration_smoke.sh)" \
  bash "$ROOT/DB/run_migration_smoke.sh"
run_stage 4 "task-1a clean-clone demonstration (DB/run_task_1a_demo.sh)" \
  bash "$ROOT/DB/run_task_1a_demo.sh"
run_stage 5 "integration suite on a disposable PostgreSQL (DB/run_disposable_integration.sh)" \
  bash "$ROOT/DB/run_disposable_integration.sh"
run_stage 6 "Compose deployment acceptance (run_stack_acceptance.sh)" \
  bash "$ROOT/run_stack_acceptance.sh"
run_stage 7 "operator workflow on the Compose deployment (run_operator_acceptance.sh)" \
  bash "$ROOT/run_operator_acceptance.sh"

echo
echo "PASS: all $STAGES stages of the complete verification suite passed in $((SECONDS - suite_started))s."
