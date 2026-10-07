# shellcheck shell=bash
# The lifecycle of one disposable PostgreSQL container, shared by
# run_migration_smoke.sh, run_task_1a_demo.sh and run_disposable_integration.sh.
# Sourced, not run.
#
# The sourcing script sets DB_DIR (the directory holding init/), LABEL_KEY
# (the ownership label), RESOURCE_PREFIX (the name prefix of its container
# and volume), PASSWORD_PREFIX, IMAGE and WAIT_SECONDS, then calls
# pg_begin_run before it creates anything:
#
#   - pg_begin_run picks a unique run id, container and volume name and a
#     random superuser password, registers the cleanup for every ordinary exit
#     and for SIGINT, SIGTERM and SIGHUP, and refuses to go on if either name
#     is taken already -- an existing resource is never taken over;
#   - pg_make_workdir creates the run's private temporary directory, which
#     the cleanup removes as well;
#   - pg_start creates the volume and starts the container with init/ mounted
#     read-only; `pg_start loopback` also publishes PostgreSQL on a loopback
#     port Docker picks, which pg_loopback_port then reads into PORT;
#   - pg_wait_for_bootstrap waits until every init script has run and the
#     server answers, for WAIT_SECONDS at most, and stops at once when the
#     container exits, an init script fails, or the container initialised
#     without the init scripts -- which happens when the Docker daemon cannot
#     see this checkout's directory;
#   - pg_report_server prints the image digest and server version in use;
#   - pg_fail reports an error with the container's state and log tail.
#
# The password reaches Docker by variable name through the environment, never
# on a command line, and every log or error output printed here goes through
# mask_password. The cleanup removes only what carries this run's label --
# never anything else -- and the run's private directory, then confirms that
# nothing is left; if something is, or the daemon cannot say, it prints the
# commands that find it and fails: with status 1 after an otherwise successful
# run, and with an earlier failure's or a signal's own status otherwise.
# Further signals are ignored while it runs. SIGKILL skips it.

pg_begin_run() {
  RUN_ID="$(date +%Y%m%d%H%M%S)-$$-$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"
  CONTAINER="${RESOURCE_PREFIX}${RUN_ID}"
  VOLUME="${RESOURCE_PREFIX}${RUN_ID}"
  WORKDIR=""
  # Test-only superuser password of a container that lives for this run
  # alone: random, and independent of the printed run id.
  PGPASS="${PASSWORD_PREFIX}-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  PG_INIT_SCRIPTS=()
  local script
  for script in "$DB_DIR"/init/*.sql; do
    if [ -e "$script" ]; then
      PG_INIT_SCRIPTS+=("$(basename "$script")")
    fi
  done
  if [ "${#PG_INIT_SCRIPTS[@]}" -eq 0 ]; then
    echo "ERROR: DB/init/ holds no .sql file to bootstrap from." >&2
    exit 1
  fi
  trap pg_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  # Never take over a resource that already exists, however unlikely a collision.
  if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "ERROR: a container named $CONTAINER already exists; not touching it." >&2
    exit 1
  fi
  if docker volume inspect "$VOLUME" >/dev/null 2>&1; then
    echo "ERROR: a volume named $VOLUME already exists; not touching it." >&2
    exit 1
  fi
}

pg_make_workdir() {
  WORKDIR="$(mktemp -d)"
  chmod 700 "$WORKDIR"
}

# Copies its input with the run's password replaced, line by line. The
# password reaches awk through its environment, not its command line.
mask_password() {
  SECRET_TO_MASK="$PGPASS" awk '
    BEGIN { secret = ENVIRON["SECRET_TO_MASK"]; n = length(secret) }
    {
      line = $0; out = ""
      while (n > 0 && (i = index(line, secret)) > 0) {
        out = out substr(line, 1, i - 1) "<password>"
        line = substr(line, i + n)
      }
      print out line
      fflush()
    }'
}

pg_cleanup() {
  local rc=$? id name containers volumes
  trap '' INT TERM HUP  # a repeated interrupt must not cut the cleanup short
  trap - EXIT
  for id in $(docker ps -aq --filter "label=$LABEL_KEY=$RUN_ID" 2>/dev/null); do
    docker rm -f -v "$id" >/dev/null 2>&1 || true
  done
  for name in $(docker volume ls -q --filter "label=$LABEL_KEY=$RUN_ID" 2>/dev/null); do
    docker volume rm "$name" >/dev/null 2>&1 || true
  done
  if ! containers="$(docker ps -aq --filter "label=$LABEL_KEY=$RUN_ID" 2>/dev/null)" \
     || ! volumes="$(docker volume ls -q --filter "label=$LABEL_KEY=$RUN_ID" 2>/dev/null)" \
     || [ -n "$containers$volumes" ]; then
    echo "WARNING: could not confirm that run $RUN_ID left nothing behind. Check with" >&2
    echo "  docker ps -a --filter label=$LABEL_KEY=$RUN_ID" >&2
    echo "  docker volume ls --filter label=$LABEL_KEY=$RUN_ID" >&2
    [ "$rc" -ne 0 ] || rc=1  # an earlier failure keeps its own status
  fi
  if [ -n "$WORKDIR" ]; then
    rm -rf "$WORKDIR" 2>/dev/null || true
    if [ -e "$WORKDIR" ]; then
      echo "WARNING: could not remove the run's temporary directory $WORKDIR." >&2
      [ "$rc" -ne 0 ] || rc=1
    fi
  fi
  exit "$rc"
}

pg_diagnose() {  # the container's state and log tail, the password masked
  echo "--- diagnostics for run $RUN_ID" >&2
  docker ps -a --filter "label=$LABEL_KEY=$RUN_ID" --format 'container {{.Names}}: {{.Status}}' 2>&1 \
    | mask_password >&2 || true
  docker logs --tail 30 "$CONTAINER" 2>&1 | mask_password >&2 || true
}

pg_fail() {
  echo "ERROR: $*" >&2
  pg_diagnose
  exit 1
}

pg_start() {  # pg_start [loopback]
  local publish=()
  if [ "${1:-}" = loopback ]; then
    publish=(-p 127.0.0.1::5432)
  fi
  docker volume create --label "$LABEL_KEY=$RUN_ID" "$VOLUME" >/dev/null
  POSTGRES_PASSWORD="$PGPASS" docker run -d --name "$CONTAINER" --label "$LABEL_KEY=$RUN_ID" \
    -e POSTGRES_PASSWORD -e POSTGRES_DB=people_db \
    ${publish[@]+"${publish[@]}"} \
    -v "$VOLUME:/var/lib/postgresql/data" \
    -v "$DB_DIR/init:/docker-entrypoint-initdb.d:ro" \
    "$IMAGE" >/dev/null
}

# The entrypoint runs the init scripts against a socket-only temporary server
# and only then starts the real one. Ready means: its completion message,
# evidence that every init/*.sql ran, and TCP readiness. "unseen" means the
# entrypoint completed without running them: the mounted directory was empty.
pg_bootstrap_state() {
  local logs script
  if [ "$(docker container inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
    echo exited
    return
  fi
  if ! logs="$(docker logs "$CONTAINER" 2>&1)"; then
    echo waiting
    return
  fi
  if grep -Eq '^psql:/docker-entrypoint-initdb\.d/[^:]+:[0-9]+: (ERROR|FATAL):' <<<"$logs"; then
    echo failed
    return
  fi
  if ! grep -qF 'PostgreSQL init process complete' <<<"$logs"; then
    echo waiting
    return
  fi
  for script in "${PG_INIT_SCRIPTS[@]}"; do
    if ! grep -qF "running /docker-entrypoint-initdb.d/$script" <<<"$logs"; then
      echo unseen
      return
    fi
  done
  if docker exec "$CONTAINER" pg_isready -h 127.0.0.1 -U postgres -d people_db >/dev/null 2>&1; then
    echo ready
  else
    echo waiting
  fi
}

pg_wait_for_bootstrap() {
  local deadline=$((SECONDS + WAIT_SECONDS))
  while :; do
    case "$(pg_bootstrap_state)" in
      ready) return 0 ;;
      exited) pg_fail "the database container stopped during bootstrap" ;;
      failed) pg_fail "an init script failed during bootstrap" ;;
      unseen)
        pg_fail "the database initialised without running DB/init/: the Docker daemon could not" \
          "see $DB_DIR/init. It has to run on the same machine as this checkout" \
          "(DB/TESTING_STRATEGY.md, \"Running the suite elsewhere\")."
        ;;
    esac
    if [ "$SECONDS" -ge "$deadline" ]; then
      pg_fail "the schema did not bootstrap within ${WAIT_SECONDS}s"
    fi
    sleep 1
  done
}

pg_loopback_port() {  # sets PORT; exactly one binding, on loopback, or the run stops
  local binding
  binding="$(docker port "$CONTAINER" 5432/tcp 2>/dev/null)" || pg_fail "Docker did not report the published port"
  if ! [[ "$binding" =~ ^127\.0\.0\.1:([0-9]+)$ ]]; then
    pg_fail "PostgreSQL is not published on a single loopback port"
  fi
  PORT="${BASH_REMATCH[1]}"
}

# What this run actually uses, so that a log records it: the image's registry
# digest (none for an image that was built locally) and the server version.
pg_report_server() {
  local digest version
  digest="$(docker image inspect --format '{{join .RepoDigests " "}}' "$IMAGE" 2>/dev/null)" || digest=""
  version="$(docker exec "$CONTAINER" postgres --version 2>/dev/null)" || version=""
  echo "      image $IMAGE, digest ${digest:-unknown}; ${version:-server version unknown}"
}

# Unsets every exported variable and function, with shell builtins only; call
# it only in a subshell. Entries whose names are not valid shell identifiers
# cannot be unset this way and pass through, but no setting read by the
# programs started here has such a name.
clear_exports() {
  IFS=$' \t\n'
  for _cleared in $(compgen -e); do
    unset "$_cleared" 2>/dev/null || true
  done
  while read -r _declare _flags _cleared; do
    if [ -n "$_cleared" ]; then unset -f "$_cleared"; fi
  done <<<"$(declare -Fx)"
}
