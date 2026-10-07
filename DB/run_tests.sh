#!/usr/bin/env bash
# The offline unit and fixture tests; also step 3 of ../run_checks.sh.
#
# They run in a cleared environment that keeps only PATH, HOME, TMPDIR and
# LANG, so no database setting, test DSN, PYTEST_ADDOPTS value or other
# variable the caller exports can connect them to a database or narrow the
# run. PEOPLE_PUBS_SKIP_DOTENV=1 stops people_pubs from filling settings in
# from DB/.env as well. The integration tests therefore always skip here;
# run_disposable_integration.sh and run_integration_tests.sh run them.
# tests/conftest.py refuses network access from the tests and the Python
# processes they start, and fails the run on a skip that
# tests/expected_skips.py does not name.
# Arguments are passed to pytest.
set -euo pipefail

cd "$(dirname "$0")"

exec env -i \
  PATH="$PATH" \
  ${HOME:+"HOME=$HOME"} \
  ${TMPDIR:+"TMPDIR=$TMPDIR"} \
  LANG="${LANG:-C.UTF-8}" \
  PYTHONPATH=. \
  PEOPLE_PUBS_SKIP_DOTENV=1 \
  ../.venv/bin/python -m pytest "$@"
