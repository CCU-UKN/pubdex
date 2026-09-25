#!/usr/bin/env bash
# The default offline check suite, in one command.
#
# Covers: Python compilation, Markdown link and path consistency, the offline
# unit and fixture tests, and the transform-version guard when a commit range
# or the staged index is supplied. It needs no database and no network.
#
# Local runs and CI run the same steps: CI checks out the repository, installs
# the development requirements and calls this script. No check is defined in
# workflow YAML, so any Git forge can produce the same result.
#
#   ./run_checks.sh                              # ordinary local run
#   ./run_checks.sh --staged                     # pre-commit: index vs HEAD
#   ./run_checks.sh --base <sha> --head <sha>    # CI: a pushed or proposed range
#
# This is deliberately NOT the whole verification story. These are separate
# commands, each needing Docker:
#
#   DB/run_migration_smoke.sh      schema bootstrap from an empty database
#   DB/run_task_1a_demo.sh         the clean-clone acceptance path, offline
#   DB/run_integration_tests.sh    the disposable-database integration suite
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

usage() {
  cat >&2 <<'USAGE'
usage: run_checks.sh [--staged | --base <sha> [--head <sha>]]

  (no arguments)               run the checks without a transform-version range
  --staged                     compare the staged index against HEAD
  --base <sha> [--head <sha>]  compare a commit range (--head defaults to HEAD)

An empty or all-zero <sha> means there is no range to compare -- the first
push of a root commit -- which is reported and is not an error.
USAGE
}

die() {
  echo "run_checks.sh: $1" >&2
  usage
  exit 2
}

mode=""          # "" (none), "staged" or "range"
base=""
head_ref="HEAD"
head_given=0

while [ $# -gt 0 ]; do
  case "$1" in
    --staged)
      if [ -n "$mode" ]; then die "--staged cannot be combined with --base/--head"; fi
      mode="staged"
      shift
      ;;
    --base)
      if [ "$mode" = "staged" ]; then die "--base cannot be combined with --staged"; fi
      if [ $# -lt 2 ]; then die "--base needs a value"; fi
      mode="range"
      base="$2"
      shift 2
      ;;
    --head)
      if [ "$mode" = "staged" ]; then die "--head cannot be combined with --staged"; fi
      if [ $# -lt 2 ]; then die "--head needs a value"; fi
      head_ref="$2"
      head_given=1
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

if [ "$head_given" = 1 ] && [ "$mode" != "range" ]; then
  die "--head requires --base"
fi

PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "run_checks.sh: no interpreter at .venv/bin/python" >&2
  echo "Create it from the repository root with:" >&2
  echo "  python3 -m venv .venv && .venv/bin/python -m pip install -r DB/requirements-dev.txt" >&2
  exit 1
fi

steps=3
if [ -n "$mode" ]; then
  steps=4
fi

echo "[1/$steps] Compiling people_pubs and its tests..."
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -m compileall -q DB/people_pubs DB/tests

echo "[2/$steps] Checking Markdown links and paths..."
"$PYTHON" check_markdown_links.py

echo "[3/$steps] Running the offline test suite..."
DB/run_tests.sh

case "$mode" in
  staged)
    echo "[4/$steps] Transform-version guard (staged index)..."
    "$PYTHON" DB/check_transform_version.py --staged
    ;;
  range)
    echo "[4/$steps] Transform-version guard ($base..$head_ref)..."
    # A first push of a root commit reports an empty or all-zero base SHA:
    # there is no range to compare, which is expected, not a failure.
    if [ -z "$base" ] || [ -z "${base//0/}" ]; then
      echo "        No base commit; there is no range to check."
    else
      "$PYTHON" DB/check_transform_version.py --base "$base" --head "$head_ref"
    fi
    ;;
esac

echo "Default offline check suite passed."
