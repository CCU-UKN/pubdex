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
#      (DB/run_disposable_integration.sh).
#
# Only synthetic fixtures and disposable databases are used. Stages 3-5 each
# start their own uniquely named and labelled PostgreSQL container and volume,
# published on loopback at most, and remove exactly those again when the stage
# ends, also after a failure or an interrupt; a stage whose cleanup cannot be
# confirmed says how to find the leftovers and fails. None of them uses Docker
# Compose or connects to an existing database or a metadata provider, and in
# the pytest runs of stages 1 and 5 a network guard refuses any other network
# access by the tests and the Python processes they start (a guard inside
# Python, not network isolation; see DB/TESTING_STRATEGY.md for its
# boundaries). No stage reads DB/.env: the stage-1 tests run in a
# cleared environment (DB/run_tests.sh), and they and every Python step of
# stages 4 and 5 run with PEOPLE_PUBS_SKIP_DOTENV=1. This script creates
# nothing itself, so it has nothing of its own to clean up. An interrupt from
# the terminal or an external timeout reaches the running stage directly,
# which cleans up at once; a signal sent to this script alone takes effect when
# the running stage returns, and no later stage starts.
#
# Needs Bash, a Git checkout, .venv with DB/requirements-dev.txt installed and,
# for stages 3-5, a Docker Engine this user can reach that runs on the same
# machine: it bind-mounts DB/init/ from this checkout and publishes PostgreSQL
# on 127.0.0.1 (DB/TESTING_STRATEGY.md, "Running the suite elsewhere").
# ./run_checks.sh remains the fast offline command for pre-commit and everyday
# use.
#
#   ./run_ci_suite.sh                              # local run
#   ./run_ci_suite.sh --base <sha> [--head <sha>]  # CI: a pushed or proposed range
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PYTHON="$ROOT/.venv/bin/python"
STAGES=5

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
reachable Docker Engine. ./run_checks.sh alone runs the offline part.
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
    "python3 -m venv .venv && .venv/bin/python -m pip install -r DB/requirements-dev.txt"
fi
if ! "$PYTHON" -c 'import psycopg, pytest' >/dev/null 2>&1; then
  missing ".venv/bin/python lacks the development requirements. Install them with:" \
    ".venv/bin/python -m pip install -r DB/requirements-dev.txt"
fi
if ! command -v git >/dev/null 2>&1; then
  missing "git was not found on PATH; the checks list the publishable files from a Git checkout."
fi
if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  missing "this directory is not a Git checkout; run the suite in a clone of the repository."
fi
if ! command -v docker >/dev/null 2>&1; then
  missing "docker was not found on PATH. Stages 3-5 start disposable PostgreSQL containers." \
    "Install Docker Engine, or run ./run_checks.sh for the offline part alone."
fi
if ! docker info >/dev/null 2>&1; then
  missing "cannot reach the Docker daemon. Start it, or check that this user may use it" \
    "('docker info' shows the reason)."
fi

# What this run uses that the repository does not pin, so that its log
# records it; each Docker-backed stage adds the PostgreSQL image digest and
# server version (DB/TESTING_STRATEGY.md, "Reproducing a verified run").
echo "Environment:"
echo "  system  $(uname -srm 2>/dev/null || echo unknown)"
echo "  bash    $BASH_VERSION"
echo "  python  $("$PYTHON" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo unknown)"
echo "  pip     $("$PYTHON" -m pip --version 2>/dev/null | cut -d' ' -f2 || echo unknown)"
echo "  git     $(git --version 2>/dev/null | cut -d' ' -f3 || echo unknown)"
echo "  docker  $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"

# No stage takes database settings from the caller: stage 1 runs its tests in
# a cleared environment (DB/run_tests.sh), and stages 3-5 bind their own.

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

echo
echo "PASS: all $STAGES stages of the complete verification suite passed in $((SECONDS - suite_started))s."
