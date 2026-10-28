#!/usr/bin/env bash
# Creates .venv, the development environment the checks and tests run in,
# with the packages of DB/requirements-dev.txt at their locked versions:
# what every documented setup and both CI workflows run.
#
#   ./setup_venv.sh                         # with python3.11 and psycopg's C implementation
#   ./setup_venv.sh --python <interpreter>  # another CPython 3.11
#   ./setup_venv.sh --psycopg python        # psycopg's pure-Python implementation
#
# Downloading and installing are separate steps, and pip never downloads:
#
#   1. <interpreter> -m venv --without-pip .venv, so that no pip but the
#      locked one ever runs in it;
#   2. DB/fetch_verified.py fetches exactly the files of DB/requirements-pip.txt,
#      DB/requirements-build.txt and DB/requirements-dev.txt into a new
#      temporary directory -- a wheel per pin, psycopg-c's source archive --
#      over HTTPS with Python's own client, each kept only when its SHA-256 is
#      one its lock lists;
#   3. DB/install_pip.py installs the locked pip from that directory, and
#      DB/install_locked.py the development lock, building psycopg-c against
#      the libpq whose pg_config comes first on PATH: both run pip with
#      --no-index from that directory alone, without pip's configuration, and
#      without network access where the system permits a network namespace
#      (DB/locked_files.py), which they report;
#   4. pip check, and the temporary directory is removed.
#
# --psycopg c, the default, needs a C compiler, the interpreter's headers and
# libpq's headers with pg_config (libpq-dev on Debian and Ubuntu); --psycopg
# python needs none of them, and loads the system's libpq at run time. Either
# way the libpq psycopg loads has to be 16 or later (DB/install_locked.py
# checks it). --network-isolation required refuses to install without a
# network namespace. An existing .venv is refused: remove it first. Exit
# status: 0 on success, 1 when a step fails, 2 for a usage error.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="python3.11"
psycopg="c"
isolation="preferred"

usage() {
  cat <<'USAGE'
usage: setup_venv.sh [--python <interpreter>] [--psycopg c|python]
                     [--network-isolation preferred|required]

Creates .venv with DB/requirements-dev.txt installed: the locked files are
fetched over HTTPS by DB/fetch_verified.py and checked against their hashes,
then installed by pip from those files alone (DB/install_pip.py,
DB/install_locked.py).

  --python <interpreter>        CPython 3.11 to create .venv with (python3.11)
  --psycopg c|python            psycopg's C implementation, built against the
                                system's libpq (c, the default), or its
                                pure-Python one, which loads it at run time
  --network-isolation <mode>    preferred (default): install without network
                                access where the system permits it; required:
                                refuse to install otherwise
  -h, --help                    show this help
USAGE
}

die_usage() {
  echo "setup_venv.sh: $1" >&2
  usage >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --python)
      if [ $# -lt 2 ] || [ -z "$2" ]; then die_usage "--python needs a value"; fi
      python="$2"
      shift 2
      ;;
    --psycopg)
      case "${2:-}" in c|python) psycopg="$2" ;; *) die_usage "--psycopg needs c or python" ;; esac
      shift 2
      ;;
    --network-isolation)
      case "${2:-}" in preferred|required) isolation="$2" ;; *) die_usage "--network-isolation needs preferred or required" ;; esac
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die_usage "unknown argument: $1"
      ;;
  esac
done

fail() {
  echo "setup_venv.sh: $*" >&2
  exit 1
}

command -v "$python" >/dev/null 2>&1 || fail "$python was not found; name a CPython 3.11 with --python"
"$python" -I -c 'import sys; sys.exit(sys.version_info[:2] != (3, 11) or sys.implementation.name != "cpython")' \
  || fail "$python is not CPython 3.11"
[ ! -e "$ROOT/.venv" ] || fail "$ROOT/.venv exists already; remove it first, or keep using it"

WHEELHOUSE="$(mktemp -d)"
trap 'rm -rf -- "$WHEELHOUSE"' EXIT

echo "==> 1/4 creating .venv without pip ($("$python" -I -c 'import sys; print(sys.executable, sys.version.split()[0])'))"
"$python" -m venv --without-pip "$ROOT/.venv"
echo "==> 2/4 fetching the locked files"
"$ROOT/.venv/bin/python" -I "$ROOT/DB/fetch_verified.py" wheels --dest "$WHEELHOUSE" --allow-source psycopg-c \
  "$ROOT/DB/requirements-pip.txt" "$ROOT/DB/requirements-build.txt" "$ROOT/DB/requirements-dev.txt"
echo "==> 3/4 installing pip and DB/requirements-dev.txt from them"
"$ROOT/.venv/bin/python" "$ROOT/DB/install_pip.py" --wheelhouse "$WHEELHOUSE" --network-isolation "$isolation"
"$ROOT/.venv/bin/python" "$ROOT/DB/install_locked.py" --wheelhouse "$WHEELHOUSE" --psycopg "$psycopg" \
  --network-isolation "$isolation" "$ROOT/DB/requirements-dev.txt"
echo "==> 4/4 pip check"
"$ROOT/.venv/bin/python" -I -m pip check
echo "setup_venv.sh: .venv is ready."
