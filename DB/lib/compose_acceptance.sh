# shellcheck shell=bash
# Helpers shared by the acceptance tests of the Compose deployment,
# run_stack_acceptance.sh and run_operator_acceptance.sh: commands run in a
# cleared environment and within a time limit, output with the run's
# passwords masked, the check of the run's own records for them, plain HTTP
# requests to the read service, and the check that a stack mounts nothing
# but its own volumes and secret files. Sourced, not run.
#
# The sourcing script calls save_caller_environment before anything else
# needs the caller's environment, and sets
#   SECRET_VALUES  the run's passwords, as it learns them (empty at first)
#   PY             the Python 3 of the HTTP checks
#   PORT           the read service's host port on 127.0.0.1, for http()
# isolated() needs clear_exports from DB/lib/disposable_postgres.sh.

# The caller's environment, kept for the commands that need it; isolated()
# hands them nothing else.
save_caller_environment() {
  local name
  SAVED_PATH="$PATH"
  SAVED_HOME="${HOME:-/nonexistent}"
  DOCKER_SELECTION=()
  for name in DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH DOCKER_TLS_VERIFY; do
    if [ -n "${!name+x}" ]; then
      DOCKER_SELECTION+=("$name=${!name}")
    fi
  done
}

isolated() {  # isolated <command> [arguments]: PATH, HOME and the daemon selection only
  if [ -n "${ISOLATED_IN_PLACE:-}" ]; then
    # bounded()'s background job exists for this command alone: the command
    # replaces it, so that stopping the job stops the command itself.
    isolated_exec "$@"
  fi
  ( isolated_exec "$@" )
}

isolated_exec() {
  clear_exports
  export PATH="$SAVED_PATH" HOME="$SAVED_HOME" LC_ALL=C
  for setting in ${DOCKER_SELECTION[@]+"${DOCKER_SELECTION[@]}"}; do
    export "${setting?}"
  done
  exec "$@"
}

# Copies its input with every secret of the run replaced, line by line. The
# values reach awk through its environment, exported by a builtin, never on
# a command line.
mask() {
  ( index=0
    for value in ${SECRET_VALUES[@]+"${SECRET_VALUES[@]}"}; do
      index=$((index + 1))
      export "MASK_$index=$value"
    done
    export MASK_COUNT="$index"
    exec awk '
      BEGIN { count = ENVIRON["MASK_COUNT"] + 0; for (i = 1; i <= count; i++) secret[i] = ENVIRON["MASK_" i] }
      {
        line = $0
        for (i = 1; i <= count; i++) {
          n = length(secret[i]); out = ""
          while (n > 0 && (j = index(line, secret[i])) > 0) {
            out = out substr(line, 1, j - 1) "<password>"
            line = substr(line, j + n)
          }
          line = out line
        }
        print line
        fflush()
      }' )
}

leaks_in() {  # leaks_in <file>...: true when a secret of the run occurs in one of them
  [ "${#SECRET_VALUES[@]}" -gt 0 ] || return 1
  grep -qsF -f <(printf '%s\n' "${SECRET_VALUES[@]}") "$@"
}

# Runs a command for at most <seconds>; status 124 when it had to be stopped.
# The command runs through isolated(), which makes it the background job's
# own process: SIGTERM reaches the command -- the Docker client, which ends
# its Compose plugin with it -- and SIGKILL follows if it is still there ten
# seconds later.
bounded() {  # bounded <seconds> <command> [arguments]
  local limit="$1"
  shift
  ( ISOLATED_IN_PLACE=1; "$@" ) &
  bounded_wait "$limit" "$!" "$1"
}

# As bounded, with this shell's standard input -- the file or pipe the caller
# redirected into it -- as the command's, rather than the empty input a job
# started with & receives.
bounded_input() {  # bounded_input <seconds> <command> [arguments]
  local limit="$1"
  shift
  ( ISOLATED_IN_PLACE=1; "$@" ) <&0 &
  bounded_wait "$limit" "$!" "$1"
}

bounded_wait() {  # bounded_wait <seconds> <job's pid> <command's name>
  local limit="$1" pid="$2" name="$3" status=0 deadline grace
  deadline=$((SECONDS + limit))
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      kill -TERM "$pid" 2>/dev/null || true
      grace=$((SECONDS + 10))
      while kill -0 "$pid" 2>/dev/null && [ "$SECONDS" -lt "$grace" ]; do
        sleep 0.2
      done
      kill -KILL "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      echo "${0##*/}: '$name ...' did not finish within ${limit}s" >&2
      return 124
    fi
    sleep 0.2
  done
  wait "$pid" || status=$?
  return "$status"
}

wait_until() {  # wait_until <seconds> <command> [arguments]: polls once a second
  local deadline=$((SECONDS + $1))
  shift
  until "$@"; do
    [ "$SECONDS" -lt "$deadline" ] || return 1
    sleep 1
  done
}

# GET http://127.0.0.1:<port><path>, the body into <file>; prints the status,
# 000 when nothing answered.
HTTP_GET='
import sys, urllib.error, urllib.request
url, out = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(url, timeout=10) as response:
        status, body = response.status, response.read()
except urllib.error.HTTPError as error:
    status, body = error.code, error.read()
except OSError:
    status, body = 0, b""
open(out, "wb").write(body)
print(f"{status:03d}")
'
http() {  # http <path> <file>
  isolated "$PY" -I -c "$HTTP_GET" "http://127.0.0.1:$PORT$1" "$2" 2>/dev/null || echo 000
}

# What the containers of Compose project <project> mount besides the
# project's two volumes, postgres-data and initdb, and the secret files in
# <secrets directory>: one line per mount, nothing when there is none. A stack
# started from the image and compose.yaml alone mounts nothing else -- no
# file of a checkout or of the project directory. Fails when Docker cannot
# say.
foreign_mounts() {  # foreign_mounts <project> <secrets directory>
  local project="$1" secrets ids id listing type name source
  secrets="$(cd "$2" && pwd -P)" || return 1
  ids="$(docker ps -aq --filter "label=com.docker.compose.project=$project")" && [ -n "$ids" ] || return 1
  for id in $ids; do
    # A bind has no name: it comes last, as read would merge an empty field
    # between two tabs into the next.
    listing="$(docker inspect --format '{{range .Mounts}}{{.Type}}{{"\t"}}{{.Source}}{{"\t"}}{{.Name}}{{"\n"}}{{end}}' "$id")" \
      || return 1
    while IFS=$'\t' read -r type source name; do
      case "$type" in
        "") ;;
        volume)
          [ "$name" = "${project}_postgres-data" ] || [ "$name" = "${project}_initdb" ] || echo "$id: volume $name"
          ;;
        bind)
          case "$(cd "$(dirname "$source")" 2>/dev/null && pwd -P)/$(basename "$source")" in
            "$secrets/postgres_password" | "$secrets/app_readonly_password" | "$secrets/app_writer_password") ;;
            *) echo "$id: bind $source" ;;
          esac
          ;;
        *) echo "$id: $type $source" ;;
      esac
    done <<<"$listing"
  done
}
