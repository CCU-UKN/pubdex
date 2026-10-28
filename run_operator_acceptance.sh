#!/usr/bin/env bash
# The operator's side of the Compose deployment, tested against Docker: the
# stack configured by an .env file, on the ordinary network of compose.yaml,
# and run with the commands DEPLOYMENT.md gives, typed as an operator types
# them, in a Compose project that this run creates, owns and removes again.
#
#   ./run_operator_acceptance.sh                       # builds both images from this checkout
#   ./run_operator_acceptance.sh --image <reference> [--postgres-image <reference>]
#                                                      # tests images Docker already has
#
# run_stack_acceptance.sh tests the deployment itself, with every setting
# fixed by the test and ingestion on a network without a route off the host.
# This test is the workflow around it. What one run does, stopping at the
# first failed check:
#
#   1. selects the images: without --image it builds this checkout's
#      application and PostgreSQL images with build_image.sh under tags of
#      its own; with --image it uses that application image and the
#      PostgreSQL image --postgres-image names, or else the one this
#      checkout's compose.yaml names by default, which Docker has to have, and
#      never builds or pulls one;
#   2. prepares an operator's directory -- a copy of this checkout's
#      compose.yaml, create_secrets.sh and set_passwords.sh, byte for byte,
#      and nothing else of the checkout: the schema comes from the image --
#      with an .env file
#      that names the project, the image, a secrets directory outside it by a
#      relative path, a host port of the run's own for the read service and
#      that service's log level; runs create_secrets.sh with that directory,
#      as DEPLOYMENT.md says, and checks the configuration Compose renders
#      from the .env file;
#   3. starts the stack with `docker compose up --wait` and checks the first
#      start from the schema bootstrap the image carries, that the containers
#      mount the project's volumes and the secret files and nothing else, the
#      read service bound to its configured port on 127.0.0.1 alone and
#      PostgreSQL to no host port, as Docker reports the bindings -- no check
#      connects from another host -- and the ordinary network of compose.yaml,
#      on which an ingestion container has a route off the host;
#   4. imports a synthetic roster from a file of the invoking user's with mode
#      0600, with the documented command -- on standard input, as app_writer,
#      without ORCID lookups -- in a container whose user is 10001; checks the
#      records it wrote, that a second import changes nothing, that a bind
#      mount of the file fails with the documented `permission denied`, and
#      that the file keeps its owner, mode and content;
#   5. configures the attribution rule, ingests the bundled ORCID and Crossref
#      payloads for the roster's person they belong to, and checks that the
#      read service serves the publication only after the documented refresh,
#      with that person as its internal author, and that the documented export
#      writes the same row to standard output;
#   6. overwrites the three password files in place and runs the commands of
#      DEPLOYMENT.md, "Changing a password": set_passwords.sh, given the .env
#      file's secrets directory, then the restart of the read service. New
#      connections with the new passwords succeed and with the old ones fail,
#      the read service is healthy again, and the records are kept;
#   7. replaces the three files instead, by rename, as many editors save. The
#      running database container still reads the old files, and the former
#      in-container resync, which read them, is refused. set_passwords.sh run
#      without the .env file's secrets directory, next to a decoy secrets/,
#      refuses before changing anything; run as documented, it gives the
#      roles the new passwords without a restart of PostgreSQL. The same
#      checks, against every earlier password;
#   8. backs the database up and restores it into a new stack with the
#      commands of DEPLOYMENT.md, "Backup and restore": pg_dump into a file
#      of mode 0600, `docker compose down --volumes`, a first start of
#      PostgreSQL alone on a new volume, the read service stopped, the
#      database dropped, created again and restored by pg_restore in one
#      transaction, then `docker compose up --wait`. The verification query
#      has to count what it counted before the dump, the read service has to
#      serve the same publication, the database to hold the roster's
#      records, and app_writer to refresh the snapshot again;
#   9. checks the environments of the database server and the read service,
#      that the read service's log level took effect, and the role model of
#      DEPLOYMENT.md, on the restored database: maintenance has no login;
#      app_writer may use no pii table and executes only the definer
#      functions of that schema, and app_readonly may not use it; the
#      superuser on PostgreSQL's socket, after SET ROLE maintenance, reads
#      the pii rows of the roster and is refused the application schemas,
#      and app_writer is refused the pii tables themselves; the documented
#      command gives maintenance a password it logs in with, until its login
#      is taken away again. Then that no password of any generation occurs
#      in the containers' logs (those of the first stack included), the
#      commands' output, the rendered configuration, docker inspect output or
#      those environments, nor a name or address of the roster in the
#      imports' output or the containers' logs.
#
# No metadata provider is contacted, by the choice of commands alone: the
# import runs with --no-orcid-lookup, the ingestion is
# people_pubs.sync.fixture_demo, whose only sources are the bundled payloads,
# and no other ingestion command runs. The network has a route off the host,
# as an operator's does; run_stack_acceptance.sh is the test that shows the
# demonstration needs none.
#
# Isolation. The project is named pubdex-operator-<run id> by the .env file,
# and the operator's directory has that name too, which Compose would
# otherwise take. A project of that name that exists already stops the run
# before anything is created, and is left as it is. Every operator command
# runs in that directory with every exported variable removed except PATH,
# HOME and Docker's daemon selection (DOCKER_HOST, DOCKER_CONTEXT,
# DOCKER_CONFIG, DOCKER_CERT_PATH, DOCKER_TLS_VERIFY): the .env file is what
# configures it, and no COMPOSE_* or PEOPLE_PUBS_* setting of the caller's,
# and neither this checkout's .env, compose.override.yaml nor secrets/, can
# reach it.
#
# Secrets. The passwords are random: create_secrets.sh makes the first ones,
# this run the later ones, which it writes into the files with shell
# builtins. They reach the containers as Compose file secrets, the database
# through set_passwords.sh on standard input, and a connection check
# receives the passwords it tries on its standard input.
# None is printed or put on a command line; output that the run prints from
# Docker or the containers is masked, and its own records of that output are
# checked for every one of them. No set -x.
#
# Cleanup. Registered before anything is created, it runs on every exit and
# after SIGINT, SIGTERM or SIGHUP, ignoring further signals while it works:
# `docker compose down --volumes` in the operator's directory, then the
# removal of any container, network or volume still carrying the project's
# name, of the image tags the run built, and of the run's temporary directory
# with the operator's directory, the secret files, the roster and the
# database dump. It then
# confirms that none of them is left; if one is, or Docker cannot say, it
# prints the commands that find it and fails -- with status 1 after an
# otherwise successful run, and with the earlier failure's or the signal's
# status otherwise. It removes nothing else: an image it was given, the
# PostgreSQL image and build cache stay, and nothing is pruned. SIGKILL skips
# it; the project's name finds what is left.
#
# Needs Bash, Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2, to build
# the image as to run it, with the Compose plugin 2.24.0 or later
# (DB/lib/docker_versions.sh says why), a daemon on this machine that can
# bind-mount files of the temporary directory, a free TCP port on 127.0.0.1
# that it publishes reachable from here, and Python 3 for the HTTP checks (.venv/bin/python, else python3).
# Git is not used.
# Exit status: 0 when every check passed and the cleanup was confirmed, 1 for
# a failed check or cleanup, 2 for a usage error, 130, 143 or 129 after a
# signal.
set -euo pipefail

# A shell function the caller exported, or one a BASH_ENV file defined, would
# stand in for docker or any other command below. None of this script's own
# exists yet, so every function defined now goes.
while read -r _declare _flags _function; do
  if [ -n "$_function" ]; then unset -f "$_function"; fi
done <<<"$(declare -F)"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_LABEL="com.docker.compose.project"
DEMO_DOI="10.5555/pubdex-fixture-001"
DEMO_AFFILIATION="Synthetic Institute for Collective Behaviour"
WAIT_SECONDS="${STACK_WAIT_SECONDS:-300}"
# shellcheck source=DB/lib/disposable_postgres.sh
source "$ROOT/DB/lib/disposable_postgres.sh"  # for clear_exports
# shellcheck source=DB/lib/docker_versions.sh
source "$ROOT/DB/lib/docker_versions.sh"
# shellcheck source=DB/lib/compose_acceptance.sh
source "$ROOT/DB/lib/compose_acceptance.sh"  # isolated, bounded, mask, leaks_in, http, foreign_mounts
# shellcheck source=DB/lib/release_identity.sh
source "$ROOT/DB/lib/release_identity.sh"  # for compose_default_image

usage() {
  cat <<'USAGE'
usage: run_operator_acceptance.sh [--image <reference> [--postgres-image <reference>]]

Runs the operator's workflow of DEPLOYMENT.md on the Compose deployment: an
.env file with a project name, image, secrets directory, port and log level
of its own, the ordinary network, a private roster on standard input, an
ingestion of the bundled payloads, the refresh, the read service and the
export, both ways of changing the passwords with set_passwords.sh, and a
backup restored into a new stack. It runs
in a Compose project of its own, contacts no metadata provider, and removes
everything it created.

  (no option)           build both images from this checkout (build_image.sh)
                        under tags of the run's own, removed afterwards
  --image <reference>   test this application image instead; Docker has to
                        have it, and nothing is built or pulled
  --postgres-image <reference>
                        with --image: test this PostgreSQL image, not the one
                        compose.yaml names by default
  -h, --help            show this help

STACK_WAIT_SECONDS bounds each wait for the stack to become healthy (300).
USAGE
}

die_usage() {
  echo "run_operator_acceptance.sh: $1" >&2
  usage >&2
  exit 2
}

image_ref=""
postgres_ref=""
while [ $# -gt 0 ]; do
  case "$1" in
    --image)
      if [ $# -lt 2 ] || [ -z "$2" ]; then die_usage "--image needs a value"; fi
      image_ref="$2"
      shift 2
      ;;
    --postgres-image)
      if [ $# -lt 2 ] || [ -z "$2" ]; then die_usage "--postgres-image needs a value"; fi
      postgres_ref="$2"
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
if [ -n "$postgres_ref" ] && [ -z "$image_ref" ]; then
  die_usage "--postgres-image needs --image; without --image both images are built from this checkout"
fi
if ! [[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  die_usage "STACK_WAIT_SECONDS must be a positive number of seconds"
fi

prerequisite() {
  echo "run_operator_acceptance.sh: $1" >&2
  shift
  local hint
  for hint in "$@"; do
    echo "  $hint" >&2
  done
  exit 1
}

command -v docker >/dev/null 2>&1 || prerequisite "docker was not found on PATH."
docker info >/dev/null 2>&1 || prerequisite "cannot reach the Docker daemon ('docker info' shows the reason)."
engine_version="$(docker version --format '{{.Server.Version}}' 2>/dev/null)" || engine_version=""
compose_version="$(docker compose version --short 2>/dev/null)" || compose_version=""
if [ -z "$compose_version" ]; then
  prerequisite "the Docker Compose plugin was not found: 'docker compose version' fails." \
    "Install the Compose plugin, version $COMPOSE_MINIMUM or later (docker-compose-plugin of Docker's packages, docker-compose-v2 on Ubuntu, docker-compose on Debian 13, or Docker's" \
    "installation guide), so that 'docker compose' works for this user."
fi
problem="$(version_problem "Docker Compose" "$compose_version" "$COMPOSE_MINIMUM")"
if [ -n "$problem" ]; then
  prerequisite "$problem; the deployment needs $COMPOSE_MINIMUM or later, the first to apply its health checks as written." \
    "Upgrade the Compose plugin (docker-compose-plugin of Docker's packages, docker-compose-v2 on Ubuntu, docker-compose on Debian 13, or Docker's installation guide)."
fi
problem="$(engine_problem "$engine_version")"
if [ -n "$problem" ]; then
  prerequisite "$problem; the deployment needs Docker Engine $ENGINE_SUPPORTED, to build its image as to run it." \
    "Other Engines start the stack too, but can let other hosts reach the ports it publishes on 127.0.0.1 (DB/lib/docker_versions.sh says why)." \
    "Upgrade the Engine (docker-ce of Docker's packages, or a distribution's docker.io of a supported release)."
fi
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 2>/dev/null)" || prerequisite "neither .venv/bin/python nor python3 was found; the HTTP checks need Python 3."
fi

# The caller's environment, kept for the commands that need it; isolated()
# hands them nothing else.
save_caller_environment

# --- the run's identity, cleanup and private directory ------------------------------

RUN_ID="$(date +%Y%m%d%H%M%S)-$$-$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"
PROJECT="pubdex-operator-$RUN_ID"
BUILT_TAG=""
BUILT_POSTGRES_TAG=""
CLAIMED=0  # set once no resource of the project existed before it
WORK=""
OPDIR=""     # the operator's directory: compose.yaml, create_secrets.sh, the .env file
SECRETS=""   # the secrets directory the .env file names
ROSTER=""
SECRETS_SETTING="../operator-secrets"  # relative to the directory of compose.yaml
SECRET_VALUES=()
ROSTER_VALUES=()

owned() {  # owned <ps|volume|network>: ids, one per line; fails when Docker cannot say
  local filter="label=$PROJECT_LABEL=$PROJECT"
  case "$1" in
    ps) docker ps -aq --filter "$filter" ;;
    volume) docker volume ls -q --filter "$filter" ;;
    network) docker network ls -q --filter "$filter" ;;
  esac
}

# docker compose as the operator runs it: in the operator's directory, which
# its .env file configures, with no option naming the project, files or
# settings.
dc() {
  if [ -n "${ISOLATED_IN_PLACE:-}" ]; then
    # bounded()'s job: the directory changes for this job alone, which the
    # command then replaces.
    cd "$OPDIR" || return 1
    isolated docker compose "$@"
  fi
  ( cd "$OPDIR" && isolated docker compose "$@" )
}

remove_owned() {  # everything of the project, and the image tags it built
  local id
  if [ -n "$OPDIR" ] && [ -f "$OPDIR/.env" ]; then
    dc --profile ingest --profile demo down --volumes --remove-orphans --timeout 5 >/dev/null 2>&1 || true
  fi
  for id in $(owned ps 2>/dev/null | sort -u); do
    docker rm -f -v "$id" >/dev/null 2>&1 || true
  done
  for id in $(owned volume 2>/dev/null | sort -u); do
    docker volume rm "$id" >/dev/null 2>&1 || true
  done
  for id in $(owned network 2>/dev/null | sort -u); do
    docker network rm "$id" >/dev/null 2>&1 || true
  done
  for id in "$BUILT_TAG" "$BUILT_POSTGRES_TAG"; do
    if [ -n "$id" ]; then
      docker image rm "$id" >/dev/null 2>&1 || true
    fi
  done
}

confirm_removed() {  # nothing of it left, or a warning with the commands that find it
  local containers volumes networks tag tag_left=""
  for tag in "$BUILT_TAG" "$BUILT_POSTGRES_TAG"; do
    if [ -n "$tag" ] && docker image inspect "$tag" >/dev/null 2>&1; then
      tag_left+="$tag "
    fi
  done
  if ! containers="$(owned ps 2>/dev/null)" || ! volumes="$(owned volume 2>/dev/null)" \
     || ! networks="$(owned network 2>/dev/null)" || [ -n "$containers$volumes$networks$tag_left" ]; then
    echo "WARNING: could not confirm that run $RUN_ID left nothing behind. Check with" >&2
    echo "  docker ps -a --filter label=$PROJECT_LABEL=$PROJECT" >&2
    echo "  docker volume ls --filter label=$PROJECT_LABEL=$PROJECT" >&2
    echo "  docker network ls --filter label=$PROJECT_LABEL=$PROJECT" >&2
    for tag in "$BUILT_TAG" "$BUILT_POSTGRES_TAG"; do
      if [ -n "$tag" ]; then
        echo "  docker image ls $tag" >&2
      fi
    done
    return 1
  fi
}

cleanup() {
  local rc=$?
  trap '' INT TERM HUP  # a repeated interrupt must not cut the cleanup short
  trap - EXIT
  # A signal ends the run inside whatever command was running, with that
  # command's output redirection still in place; report to the terminal.
  exec 1>&3 2>&4
  if [ "$CLAIMED" = 1 ]; then
    remove_owned
    if ! confirm_removed; then
      [ "$rc" -ne 0 ] || rc=1  # an earlier failure keeps its own status
    fi
  fi
  if [ -n "$WORK" ]; then
    rm -rf -- "$WORK" 2>/dev/null || true
    if [ -e "$WORK" ]; then
      echo "WARNING: could not remove the run's temporary directory $WORK, which holds its secret files, roster and database dump." >&2
      [ "$rc" -ne 0 ] || rc=1
    fi
  fi
  if [ "$rc" -eq 0 ]; then
    echo "      cleanup: the project's containers, networks and volumes, the run's image tags, the operator's directory, secret files, roster and dump are gone"
  fi
  exit "$rc"
}

exec 3>&1 4>&2  # the run's own output, for the cleanup
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

diagnose() {  # the project's state and log tails, masked
  [ -n "$OPDIR" ] && [ -f "$OPDIR/.env" ] || return 0
  echo "--- diagnostics for run $RUN_ID" >&2
  dc ps -a 2>&1 | mask >&2 || true
  dc logs --no-color --tail 30 2>&1 | mask >&2 || true
}

fail() {
  echo "FAIL: $*" >&2
  diagnose
  exit 1
}

ok() { echo "  ok  $*"; }

PORT=""
ready() { [ "$(http /readyz "$WORK/readyz.json")" = 200 ]; }
container_of() { dc ps -q "$1" 2>/dev/null; }
started_at() { docker inspect --format '{{.State.StartedAt}}' "$(container_of "$1")"; }

# SQL from standard input, as the database's superuser through PostgreSQL's
# socket in its container, as DEPLOYMENT.md, "Reaching the database", does.
superuser_sql() {
  bounded_input 60 dc exec -T -u postgres postgres psql -XqtA -v ON_ERROR_STOP=1 -d people_db -f -
}

# login_check <role> <password> ...: for each pair, one new TCP connection
# from a new ingestion container, which reads the pairs from its standard
# input; prints "accepted <current_user>" or "refused" per pair.
LOGIN_CHECK='
import sys, psycopg
server = {"host": "postgres", "port": 5432, "dbname": "people_db", "connect_timeout": 10,
          "application_name": "pubdex-login-check"}
for line in sys.stdin:
    role, secret = line.rstrip("\n").split("\t", 1)
    try:
        with psycopg.connect(**server, **{"user": role, "password": secret}) as connection:
            print("accepted", connection.execute("SELECT current_user").fetchone()[0])
    except psycopg.OperationalError as error:
        print("refused" if "password authentication failed" in str(error) else "error " + type(error).__name__)
'
login_check() {
  local lines=""
  while [ $# -ge 2 ]; do
    lines+="$1"$'\t'"$2"$'\n'
    shift 2
  done
  printf '%s' "$lines" | bounded_input 120 dc run --rm -T ingest python -I -c "$LOGIN_CHECK"
}

new_password() { od -An -N32 -tx1 /dev/urandom | tr -d ' \n'; }
inode() { ls -i "$SECRETS/$1" | awk '{ print $1 }'; }
SECRET_NAMES=(postgres_password app_readonly_password app_writer_password)
inodes() { local name; for name in "${SECRET_NAMES[@]}"; do printf '%s ' "$(inode "$name")"; done; }

# same_content <service> <secret> [user]: whether the container reads the
# host file's present content at /run/secrets/<secret>; the content reaches
# cmp on its standard input.
same_content() {
  local status=0
  bounded_input 60 dc exec -T ${3:+-u "$3"} "$1" cmp -s "/run/secrets/$2" - < "$SECRETS/$2" || status=$?
  case "$status" in
    0) echo same ;;
    1) echo differs ;;
    *) echo "unknown ($status)" ;;
  esac
}

# set_passwords.sh as DEPLOYMENT.md, "Changing a password", has the operator
# run it: in the operator's directory, given the .env file's secrets
# directory, as create_secrets.sh was -- or, with "bare", without it.
set_passwords_job() {  # set_passwords_job <given|bare>: through bounded()
  cd "$OPDIR" || return 1
  if [ "$1" = bare ]; then
    isolated bash ./set_passwords.sh
  else
    isolated env PEOPLE_PUBS_SECRETS_DIR="$SECRETS_SETTING" bash ./set_passwords.sh
  fi
}
set_passwords() {  # set_passwords <log name>: the documented run, which has to succeed
  bounded 300 set_passwords_job given > "$WORK/$1" 2>&1 \
    && grep -qxF "020_role_passwords.sh: postgres, app_readonly and app_writer have the passwords from standard input, and each authenticated a new connection." "$WORK/$1" \
    || { mask < "$WORK/$1" >&2; fail "set_passwords.sh did not give the database the files' passwords"; }
}
# Overwrites, or with "replace" replaces, each secret file with the password
# given for it, mode 444.
write_secrets() {  # write_secrets <overwrite|replace> <superuser> <app_readonly> <app_writer>
  local how="$1" name new_file
  shift
  for name in "${SECRET_NAMES[@]}"; do
    if [ "$how" = replace ]; then
      new_file="$(mktemp "$SECRETS/.$name.XXXXXX")"
      printf '%s\n' "$1" > "$new_file"
      chmod 444 "$new_file"
      mv -f "$new_file" "$SECRETS/$name"
    else
      chmod u+w "$SECRETS/$name"
      printf '%s\n' "$1" > "$SECRETS/$name"
      chmod 444 "$SECRETS/$name"
    fi
    shift
  done
}
restart_api() {  # restart_api <log name>: docker compose restart api, then up --wait
  local before
  before="$(started_at api)"
  bounded 120 dc restart api > "$WORK/restart-$1" 2>&1 || fail "docker compose restart api failed"
  wait_healthy "up-$1"
  [ "$(started_at api)" != "$before" ] || fail "the read service was not restarted"
}

wait_healthy() {  # wait_healthy <log name>: docker compose up --wait, which waits for both services
  dc up --wait --wait-timeout "$WAIT_SECONDS" > "$WORK/$1" 2>&1 \
    || { mask < "$WORK/$1" | tail -n 20 >&2; fail "docker compose up --wait did not see a healthy stack"; }
}

# publication_check <page file>: the ingested publication with the roster's
# person as its internal author, or a failure; prints its fields as JSON.
publication_check() {
  isolated "$PY" -I - "$1" "$DEMO_DOI" "$INTERNAL_AUTHOR" <<'PY'
import json, sys
page = json.load(open(sys.argv[1], encoding="utf-8"))
items = [item for item in page["items"] if item["doi"] == sys.argv[2]]
assert len(page["items"]) == 1 and len(items) == 1, page
item = items[0]
expected = {
    "title": "Synthetic Collective Behaviour Paper",
    "year": 2024,
    "venue": "Journal of Synthetic Metadata",
    "internal_authors": sys.argv[3],
}
assert list(item) == ["pub_id", "doi", "title", "year", "venue", "source_of_truth", "internal_authors"], list(item)
assert {key: item[key] for key in expected} == expected, item
assert set(item["source_of_truth"].split("+")) == {"crossref", "orcid"}, item["source_of_truth"]
assert isinstance(item["pub_id"], int) and page["next_after"] is None, page
print(json.dumps(item, ensure_ascii=True))
PY
}

served_unchanged() {  # served_unchanged <file name>: the same publication, after a change
  wait_until 60 ready || fail "GET /readyz did not answer 200 again: $(cat "$WORK/readyz.json")"
  [ "$(http '/v0/publications?limit=100' "$WORK/$1")" = 200 ] || fail "GET /v0/publications"
  [ "$(publication_check "$WORK/$1" 2>/dev/null)" = "$PUBLICATION" ] || fail "the read service serves other records"
}

echo "Operator acceptance run $RUN_ID: project $PROJECT"
echo "      docker $engine_version, compose $compose_version"

# Never take over what exists already, however unlikely a collision.
if ! existing="$(owned ps && owned volume && owned network)"; then
  echo "ERROR: Docker cannot list the resources of project $PROJECT; nothing was created." >&2
  exit 1
fi
if [ -n "$existing" ]; then
  echo "ERROR: resources of project $PROJECT exist already; not touching them." >&2
  exit 1
fi
CLAIMED=1
WORK="$(mktemp -d)"
chmod 700 "$WORK"

# --- 1. the image -----------------------------------------------------------------------

echo "[1/9] Selecting the application image and the PostgreSQL image..."
if [ -z "$image_ref" ]; then
  tag="pubdex-operator:$RUN_ID"
  postgres_tag="pubdex-operator-postgres:$RUN_ID"
  for existing_tag in "$tag" "$postgres_tag"; do
    if docker image inspect "$existing_tag" >/dev/null 2>&1; then
      echo "ERROR: an image tagged $existing_tag exists already; not touching it." >&2
      exit 1
    fi
  done
  BUILT_TAG="$tag"
  BUILT_POSTGRES_TAG="$postgres_tag"
  if ! isolated bash "$ROOT/build_image.sh" --tag "$tag" --postgres-tag "$postgres_tag" > "$WORK/build.log" 2>&1; then
    tail -n 40 "$WORK/build.log" >&2
    fail "build_image.sh failed"
  fi
  image_ref="$tag"
  postgres_ref="$postgres_tag"
  ok "built $tag and $postgres_tag from this checkout with build_image.sh"
elif ! docker image inspect "$image_ref" >/dev/null 2>&1; then
  fail "Docker has no image $image_ref; pull it first. Nothing is built or pulled in its place."
fi
if [ -z "$postgres_ref" ]; then
  postgres_ref="$(compose_default_image "$ROOT/compose.yaml" PEOPLE_PUBS_POSTGRES_IMAGE)"
  [ -n "$postgres_ref" ] || fail "this checkout's compose.yaml names no default PostgreSQL image"
fi
docker image inspect "$postgres_ref" >/dev/null 2>&1 \
  || fail "Docker has no image $postgres_ref; pull it first, or name another with --postgres-image. Nothing is built or pulled in its place."
POSTGRES_ID="$(docker image inspect --format '{{.Id}}' "$postgres_ref")" || fail "cannot inspect $postgres_ref"
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$image_ref")" || fail "cannot inspect $image_ref"
labels="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}, revision {{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE_ID")" \
  || fail "cannot read the labels of $image_ref"
echo "      image $image_ref"
echo "      id $IMAGE_ID, version $labels"
postgres_labels="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}, revision {{index .Config.Labels "org.opencontainers.image.revision"}}, PostgreSQL {{index .Config.Labels "pubdex.postgresql-version"}}' "$POSTGRES_ID")" \
  || fail "cannot read the labels of $postgres_ref"
echo "      PostgreSQL image $postgres_ref"
echo "      id $POSTGRES_ID, version $postgres_labels"

# --- 2. the operator's directory, .env file and secrets ---------------------------------

echo "[2/9] Preparing the operator's directory, its .env file and the secret files..."
OPDIR="$WORK/$PROJECT"
mkdir "$OPDIR"
cp -p "$ROOT/compose.yaml" "$ROOT/create_secrets.sh" "$ROOT/set_passwords.sh" "$OPDIR/"
for file in compose.yaml create_secrets.sh set_passwords.sh; do
  cmp -s "$ROOT/$file" "$OPDIR/$file" || fail "the copy of $file differs from this checkout's"
done
PORT="$(isolated "$PY" -I -c '
import socket
with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    print(probe.getsockname()[1])')" || fail "cannot find a free port on 127.0.0.1"
cat > "$OPDIR/.env" <<EOF
COMPOSE_PROJECT_NAME=$PROJECT
PEOPLE_PUBS_IMAGE=$image_ref
PEOPLE_PUBS_POSTGRES_IMAGE=$postgres_ref
PEOPLE_PUBS_SECRETS_DIR=$SECRETS_SETTING
PEOPLE_PUBS_HTTP_PORT=$PORT
PEOPLE_PUBS_API_LOG_LEVEL=WARNING
EOF
[ "$(LC_ALL=C ls -A "$OPDIR" | tr '\n' ' ')" = ".env compose.yaml create_secrets.sh set_passwords.sh " ] || fail "the operator's directory holds other files"
ok "the operator's directory holds this checkout's compose.yaml, create_secrets.sh and set_passwords.sh and an .env file, nothing else"
# As DEPLOYMENT.md, "Settings", says: create_secrets.sh reads no .env file, so
# it is given the same directory.
( cd "$OPDIR"
  clear_exports
  export PATH="$SAVED_PATH" HOME="$SAVED_HOME" PEOPLE_PUBS_SECRETS_DIR="$SECRETS_SETTING"
  exec ./create_secrets.sh ) > "$WORK/create_secrets.log" 2>&1 \
  || { cat "$WORK/create_secrets.log" >&2; fail "create_secrets.sh failed"; }
SECRETS="$(cd "$WORK/operator-secrets" 2>/dev/null && pwd -P)" || fail "create_secrets.sh did not create $SECRETS_SETTING next to the operator's directory"
[ "$(grep -c '^  created ' "$WORK/create_secrets.log")" = 3 ] || fail "create_secrets.sh did not create the three files"
for name in postgres_password app_readonly_password app_writer_password; do
  IFS= read -r value < "$SECRETS/$name" || fail "cannot read the new $name"
  [ -n "$value" ] || fail "the new $name is empty"
  SECRET_VALUES+=("$value")
done
modes="$(ls -ld "$SECRETS" | cut -c1-10)"
for name in postgres_password app_readonly_password app_writer_password; do
  modes+=" $(ls -l "$SECRETS/$name" | cut -c1-10)"
done
[ "$modes" = "drwx------ -r--r--r-- -r--r--r-- -r--r--r--" ] \
  || fail "the secrets directory or its files do not have the documented modes ($modes)"
ok "create_secrets.sh, given the .env file's PEOPLE_PUBS_SECRETS_DIR, created the three files in $SECRETS_SETTING"
dc config --format json > "$WORK/config.json" 2> "$WORK/config.err" \
  || { mask < "$WORK/config.err" >&2; fail "docker compose config failed"; }
! leaks_in "$WORK/config.json" || fail "a password occurs in the rendered configuration"
isolated "$PY" -I - "$WORK/config.json" "$PROJECT" "$image_ref" "$PORT" "$SECRETS" "$postgres_ref" <<'PY' || fail "the rendered configuration is not the one the .env file configures"
import json, sys
config, project, image, port, secrets, postgres = json.load(open(sys.argv[1], encoding="utf-8")), *sys.argv[2:]
assert config["name"] == project, config["name"]
services = config["services"]
assert set(services) == {"bootstrap", "postgres", "api"}, sorted(services)
assert services["api"]["image"] == image, services["api"]["image"]
assert services["bootstrap"]["image"] == image, services["bootstrap"]["image"]
assert services["postgres"]["image"] == postgres, services["postgres"]["image"]
published = [(p.get("host_ip"), str(p.get("published")), p.get("target")) for p in services["api"]["ports"]]
assert published == [("127.0.0.1", port, 8000)], published
assert "ports" not in services["postgres"]
assert services["api"]["environment"]["PEOPLE_PUBS_API_LOG_LEVEL"] == "WARNING"
files = {name: secret["file"] for name, secret in config["secrets"].items()}
assert files == {name: f"{secrets}/{name}" for name in ("postgres_password", "app_readonly_password",
                                                        "app_writer_password")}, files
assert not config["networks"]["default"].get("internal"), config["networks"]["default"]
PY
ok "Compose renders the .env file's project, images, port 127.0.0.1:$PORT, secret files and log level, on the ordinary network"

# --- 3. the first start -----------------------------------------------------------------

echo "[3/9] Starting the stack as an operator does: docker compose up --wait..."
started=$SECONDS
wait_healthy up-first.log
ok "bootstrap completed, postgres and api healthy $((SECONDS - started))s after docker compose up --wait"
dc logs --no-color bootstrap > "$WORK/bootstrap-first.log" 2>&1 || true
dc logs --no-color postgres > "$WORK/postgres-first.log" 2>&1 || true
grep -qE "bootstrap: /var/lib/pubdex/initdb holds the [0-9]+ files of this image's schema bootstrap" "$WORK/bootstrap-first.log" \
  && grep -qF "running /docker-entrypoint-initdb.d/020_role_passwords.sh" "$WORK/postgres-first.log" \
  && grep -qF "app_readonly and app_writer have the passwords from their files; maintenance cannot log in." "$WORK/postgres-first.log" \
  || fail "the first start did not bootstrap the schema the image carries and provision the role passwords"
API_CONTAINER="$(container_of api)" && [ -n "$API_CONTAINER" ] || fail "cannot find the read service's container"
POSTGRES_CONTAINER="$(container_of postgres)" && [ -n "$POSTGRES_CONTAINER" ] || fail "cannot find PostgreSQL's container"
[ "$(docker inspect --format '{{.Image}}' "$API_CONTAINER")" = "$IMAGE_ID" ] || fail "the read service does not run the selected image"
ok "the empty volume was initialised from the schema bootstrap the image carries, and the read service runs the selected image"
mounts="$(foreign_mounts "$PROJECT" "$SECRETS")" || fail "cannot list the mounts of the project's containers"
[ -z "$mounts" ] || fail "a container mounts more than the project's volumes and the secret files: $mounts"
ok "the containers mount the project's two volumes and the secret files, and nothing of the operator's directory or the checkout"
[ "$(docker port "$API_CONTAINER" | sort -u)" = "8000/tcp -> 127.0.0.1:$PORT" ] \
  || fail "the read service is not published on 127.0.0.1:$PORT alone: $(docker port "$API_CONTAINER" | tr '\n' ' ')"
[ -z "$(docker port "$POSTGRES_CONTAINER")" ] || fail "PostgreSQL is published on a host port"
ok "Docker binds the read service to the configured 127.0.0.1:$PORT alone and PostgreSQL to no host port (the configured binding; no connection from another host is tried)"
postgres_networks="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' "$POSTGRES_CONTAINER")" \
  && [ -n "${postgres_networks// /}" ] || fail "cannot list the networks of PostgreSQL's container"
for network in $postgres_networks; do
  [ "$(docker network inspect --format '{{.Internal}}' "$network" 2>/dev/null)" = false ] \
    || fail "PostgreSQL is attached to $network, which is not the ordinary network of compose.yaml"
done
bounded 120 dc run --rm -T ingest python -I -c '
import pathlib
rows = pathlib.Path("/proc/net/route").read_text().splitlines()[1:]
print("default routes:", len([r for r in rows if r.split()[1] == "00000000" and int(r.split()[3], 16) & 0x201 == 0x1]))
' > "$WORK/routes.log" 2> "$WORK/routes.err" || { mask < "$WORK/routes.err" >&2; fail "the route check in an ingestion container failed"; }
routes_off_host='^default routes: [1-9]'
[[ "$(tail -n 1 "$WORK/routes.log")" =~ $routes_off_host ]] || fail "an ingestion container has no route off the host: $(tail -n 1 "$WORK/routes.log")"
ok "PostgreSQL and the ingestion containers are on the ordinary network, with a route off the host, as in a deployment"
[ "$(http /livez "$WORK/livez.json")" = 200 ] && [ "$(http /readyz "$WORK/readyz.json")" = 200 ] \
  && [ "$(http /v0/publications "$WORK/empty.json")" = 200 ] \
  && [ "$(cat "$WORK/empty.json")" = '{"items":[],"limit":25,"next_after":null}' ] \
  || fail "GET /livez, /readyz or /v0/publications on port $PORT"
ok "GET /livez, /readyz and /v0/publications answer 200 on port $PORT, with an empty list"

# --- 4. a private roster on standard input ---------------------------------------------

echo "[4/9] Importing a private roster on standard input..."
ROSTER="$WORK/roster.csv"
# Synthetic people with checksum-valid iDs. The first is the person the
# bundled payloads belong to, under a name of the roster's own, so that the
# publication of step 5 shows which record it was attributed to; the others
# have no payload.
INTERNAL_AUTHOR="Zoë Synthetic"
( umask 077
  printf '%s\n' \
    "orcid,display_name,first_name,last_name,person_kind,primary_email" \
    "0000-0000-0000-0001,$INTERNAL_AUTHOR,Zoë,Synthetic,internal,zoe.synthetic@example.org" \
    "0000-0000-0000-0079,Łukasz Fixture,Łukasz,Fixture,internal,lukasz.fixture@example.org" \
    "0000-0000-0000-0087,Chloé Placeholder,Chloé,Placeholder,external," > "$ROSTER" )
ROSTER_VALUES=("$INTERNAL_AUTHOR" "Łukasz Fixture" "Chloé Placeholder" zoe.synthetic@example.org lukasz.fixture@example.org)
EXPECTED_RECORDS="0000-0000-0000-0001|internal|$INTERNAL_AUTHOR|Zoë|Synthetic|zoe.synthetic@example.org
0000-0000-0000-0079|internal|Łukasz Fixture|Łukasz|Fixture|lukasz.fixture@example.org
0000-0000-0000-0087|external|Chloé Placeholder|Chloé|Placeholder|"
file_state() {  # mode, owner and SHA-256 of the roster
  isolated "$PY" -I -c '
import hashlib, os, stat, sys
info = os.stat(sys.argv[1])
print(oct(stat.S_IMODE(info.st_mode)), info.st_uid, hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$ROSTER"
}
records() {  # the people the imports wrote, one line each, from the database
  superuser_sql <<'SQL'
SELECT i.orcid, p.person_kind, p.display_name, coalesce(p.first_name::text, ''), coalesce(p.last_name::text, ''),
       coalesce(x.primary_email::text, '')
FROM app.identities_orcid AS i
JOIN app.people AS p USING (person_id)
LEFT JOIN pii.people_pii AS x USING (person_id)
ORDER BY i.orcid;
SQL
}
uid="$(id -u)"
state_before="$(file_state)" || fail "cannot read the roster's mode, owner and content"
[ "${state_before% *}" = "0o600 $uid" ] || fail "the roster is not a file of this user with mode 0600: ${state_before% *}"
container_uid="$(bounded 120 dc run --rm -T ingest id -u 2> "$WORK/uid.err")" || { mask < "$WORK/uid.err" >&2; fail "cannot ask an ingestion container for its user"; }
[ "$container_uid" = 10001 ] || fail "an ingestion container runs as user $container_uid, not 10001"
ok "the roster is a file of this user (uid $uid) with mode 0600; an ingestion container runs as uid 10001"
if [ "$uid" = 0 ] || [ "$uid" = 10001 ]; then
  echo "      note: this user is uid $uid, which can read the file in a container; the bind-mount check is left out"
else
  if bounded 120 dc run --rm -T -v "$ROSTER:/data/roster.csv:ro" ingest \
       python -m people_pubs.sync.add_people --csv /data/roster.csv --no-orcid-lookup > "$WORK/roster-mount.log" 2>&1; then
    fail "a container read the private roster through a bind mount"
  fi
  grep -qF "cannot read the roster /data/roster.csv: permission denied" "$WORK/roster-mount.log" \
    || { mask < "$WORK/roster-mount.log" | tail -n 5 >&2; fail "a bind mount of the private roster did not fail with the documented message"; }
  ok "a bind mount of the file fails with the documented 'permission denied', as the container's user may not read it"
fi
bounded_input 120 dc run --rm -T ingest python -m people_pubs.sync.add_people --csv - --no-orcid-lookup \
    < "$ROSTER" > "$WORK/roster-import.log" 2>&1 \
  || { mask < "$WORK/roster-import.log" | tail -n 20 >&2; fail "the roster import failed"; }
grep -qF "add_people: total=3 inserted=3 updated=0 skipped_existing=0 skipped_invalid=0 errors=0" "$WORK/roster-import.log" \
  || { mask < "$WORK/roster-import.log" | tail -n 5 >&2; fail "the roster import did not insert the three people"; }
records > "$WORK/records.txt" 2> "$WORK/records.err" || { mask < "$WORK/records.err" >&2; fail "cannot read the imported records"; }
[ "$(cat "$WORK/records.txt")" = "$EXPECTED_RECORDS" ] \
  || fail "the database holds other people than the roster's: $(tr '\n' ';' < "$WORK/records.txt")"
ok "docker compose run --rm -T ingest ... add_people --csv - --no-orcid-lookup < roster.csv wrote the three people: iDs, kinds, UTF-8 names, addresses"
bounded_input 120 dc run --rm -T ingest python -m people_pubs.sync.add_people --csv - --no-orcid-lookup \
    < "$ROSTER" > "$WORK/roster-again.log" 2>&1 \
  || { mask < "$WORK/roster-again.log" | tail -n 20 >&2; fail "the second roster import failed"; }
grep -qF "add_people: total=3 inserted=0 updated=0 skipped_existing=3 skipped_invalid=0 errors=0" "$WORK/roster-again.log" \
  && [ "$(records 2>/dev/null)" = "$EXPECTED_RECORDS" ] || fail "the second import of the same roster changed the records"
ok "a second import of the same roster skipped all three and changed nothing"
[ "$(file_state)" = "$state_before" ] || fail "the roster's mode, owner or content changed"
ok "the roster kept its owner, mode 0600 and content (SHA-256 ${state_before##* })"

# --- 5. ingestion, the refresh, the read service and the export -------------------------

echo "[5/9] Ingesting the bundled payloads, refreshing, reading and exporting..."
bounded 120 dc run --rm -T ingest python -m people_pubs.sync.attribution_rules set --affiliation "$DEMO_AFFILIATION" \
    > "$WORK/rules.log" 2>&1 || { mask < "$WORK/rules.log" | tail -n 20 >&2; fail "attribution_rules set failed"; }
grep -q "snapshots refreshed together: 0 canonical publications" "$WORK/rules.log" || fail "the attribution rule was not set before any ingestion"
bounded 300 dc run --rm -T ingest python -m people_pubs.sync.fixture_demo > "$WORK/fixtures.log" 2>&1 \
  || { mask < "$WORK/fixtures.log" | tail -n 30 >&2; fail "the ingestion of the bundled payloads failed"; }
grep -q "fixture stage orcid: processed=1 inserted=1" "$WORK/fixtures.log" \
  && grep -q "fixture stage crossref: processed=1 inserted=0 updated=1" "$WORK/fixtures.log" \
  || fail "the bundled ORCID and Crossref payloads were not ingested for the roster's person"
ok "the attribution rule, then the bundled ORCID and Crossref payloads ingested as app_writer for the roster's person"
[ "$(http '/v0/publications?limit=100' "$WORK/before-refresh.json")" = 200 ] \
  && [ "$(cat "$WORK/before-refresh.json")" = '{"items":[],"limit":100,"next_after":null}' ] \
  || fail "the read service served the new publication before the refresh, or failed: $(cat "$WORK/before-refresh.json")"
ok "before the refresh the read service still serves the empty snapshot"
bounded 120 dc run --rm -T ingest python -m people_pubs.sync.attribution_rules recompute --refresh-only \
    > "$WORK/refresh.log" 2>&1 || { mask < "$WORK/refresh.log" | tail -n 20 >&2; fail "the documented refresh failed"; }
grep -q "snapshots refreshed together: 1 canonical publications" "$WORK/refresh.log" || fail "the refresh did not publish one canonical publication"
[ "$(http '/v0/publications?limit=100' "$WORK/publications.json")" = 200 ] || fail "GET /v0/publications after the refresh"
PUBLICATION="$(publication_check "$WORK/publications.json" 2> "$WORK/publication-check.err")" \
  || { cat "$WORK/publication-check.err" >&2; fail "the read service does not serve the ingested publication as expected"; }
ok "after attribution_rules recompute --refresh-only, GET /v0/publications serves $DEMO_DOI with internal author $INTERNAL_AUTHOR"
bounded 120 dc run --rm -T ingest python -m people_pubs.sync.export_publications \
    > "$WORK/publications.csv" 2> "$WORK/export.err" \
  || { mask < "$WORK/export.err" | tail -n 20 >&2; fail "the documented export failed"; }
grep -q "Exported 1 publications to stdout" "$WORK/export.err" || fail "the export did not report one publication on its standard error"
isolated "$PY" -I - "$WORK/publications.csv" "$PUBLICATION" <<'PY' || fail "the export's standard output is not the CSV of the served publication"
import csv, json, sys
text = open(sys.argv[1], encoding="utf-8", newline="").read()
item = json.loads(sys.argv[2])
rows = list(csv.reader(text.splitlines()))
assert rows[0] == list(item), rows[0]
assert rows[1:] == [["" if value is None else str(value) for value in item.values()]], rows[1:]
PY
ok "docker compose run --rm -T ingest ... export_publications > publications.csv wrote that publication's row, UTF-8, and nothing else"

# --- 6. a password change in place ------------------------------------------------------

echo "[6/9] Changing the three passwords in place, with the documented commands..."
first_superuser="${SECRET_VALUES[0]}"
first_readonly="${SECRET_VALUES[1]}"
first_writer="${SECRET_VALUES[2]}"
second_superuser="$(new_password)"
second_readonly="$(new_password)"
second_writer="$(new_password)"
SECRET_VALUES+=("$second_superuser" "$second_readonly" "$second_writer")
inodes_before="$(inodes)"
write_secrets overwrite "$second_superuser" "$second_readonly" "$second_writer"
[ "$(inodes)" = "$inodes_before" ] \
  && [ "$(cd "$SECRETS" && ls -l "${SECRET_NAMES[@]}" | cut -c1-10 | sort -u)" = "-r--r--r--" ] \
  || fail "the files were not changed in place with their mode kept"
ok "the three files overwritten in place (same inodes, mode 444)"
set_passwords set-passwords-in-place.log
restart_api after-in-place.log
ok "set_passwords.sh, docker compose restart api, docker compose up --wait: healthy again"
[ "$(login_check postgres "$second_superuser" app_readonly "$second_readonly" app_writer "$second_writer" \
      postgres "$first_superuser" app_readonly "$first_readonly" app_writer "$first_writer" \
      2> "$WORK/logins-in-place.err")" \
    = $'accepted postgres\naccepted app_readonly\naccepted app_writer\nrefused\nrefused\nrefused' ] \
  || { mask < "$WORK/logins-in-place.err" | tail -n 5 >&2; fail "new connections do not accept exactly the new passwords"; }
ok "new connections: the three roles accepted with the new passwords, refused with the old ones"
served_unchanged publications-after-in-place.json
ok "the read service, restarted with its new password, serves the same publication; GET /readyz 200"

# --- 7. a password change by replacement ------------------------------------------------

echo "[7/9] Replacing the three password files by rename, then the documented commands..."
third_superuser="$(new_password)"
third_readonly="$(new_password)"
third_writer="$(new_password)"
SECRET_VALUES+=("$third_superuser" "$third_readonly" "$third_writer")
inodes_before=($(inodes))
write_secrets replace "$third_superuser" "$third_readonly" "$third_writer"
inodes_after=($(inodes))
for index in 0 1 2; do
  [ "${inodes_after[$index]}" != "${inodes_before[$index]}" ] || fail "${SECRET_NAMES[$index]} was not replaced"
done
seen=""
for name in "${SECRET_NAMES[@]}"; do
  seen+="$(same_content postgres "$name" postgres) "
done
[ "$seen" = "differs differs differs " ] \
  || fail "after the replacement the running database container reads: $seen-- not the old files, which this step is about"
status=0
bounded 60 dc exec -T -u postgres postgres /docker-entrypoint-initdb.d/020_role_passwords.sh --resync \
  > "$WORK/former-resync.log" 2>&1 || status=$?
[ "$status" = 1 ] && grep -qF "020_role_passwords.sh: unknown argument: --resync" "$WORK/former-resync.log" \
  || { mask < "$WORK/former-resync.log" >&2; fail "the former in-container resync, which read the old files, was not refused"; }
ok "the three files replaced by rename: the running database container still reads the old ones, and the former in-container resync from them is refused"
# The operator forgets the .env file's secrets directory, and an old
# secrets/ lies next to compose.yaml: a new container would read other files.
( cd "$OPDIR"
  clear_exports
  export PATH="$SAVED_PATH" HOME="$SAVED_HOME"
  exec ./create_secrets.sh ) > "$WORK/create_secrets-decoy.log" 2>&1 \
  || { cat "$WORK/create_secrets-decoy.log" >&2; fail "create_secrets.sh could not create the decoy secrets/"; }
for name in "${SECRET_NAMES[@]}"; do
  IFS= read -r value < "$OPDIR/secrets/$name" || fail "cannot read the decoy $name"
  SECRET_VALUES+=("$value")
done
status=0
bounded 300 set_passwords_job bare > "$WORK/set-passwords-decoy.log" 2>&1 || status=$?
[ "$status" = 1 ] \
  && grep -qF "a new api container reads another password at /run/secrets/app_readonly_password" "$WORK/set-passwords-decoy.log" \
  && grep -qF "nothing was changed" "$WORK/set-passwords-decoy.log" \
  || { mask < "$WORK/set-passwords-decoy.log" >&2; fail "set_passwords.sh without the .env file's secrets directory did not refuse the decoy files"; }
rm -rf -- "$OPDIR/secrets"
[ "$(login_check postgres "$second_superuser" app_readonly "$second_readonly" app_writer "$second_writer" \
      2> "$WORK/logins-decoy.err")" = $'accepted postgres\naccepted app_readonly\naccepted app_writer' ] \
  || { mask < "$WORK/logins-decoy.err" | tail -n 5 >&2; fail "the refused run changed a password"; }
ok "set_passwords.sh without the .env file's PEOPLE_PUBS_SECRETS_DIR, next to a decoy secrets/, refused: a new read-service container reads other files; the passwords of step 6 still work"
postgres_before="$(started_at postgres)"
set_passwords set-passwords-replaced.log
[ "$(started_at postgres)" = "$postgres_before" ] || fail "PostgreSQL was restarted"
restart_api after-replaced.log
[ "$(same_content api app_readonly_password)" = same ] || fail "the restarted read service does not read the new file"
ok "set_passwords.sh, docker compose restart api, docker compose up --wait: healthy again, PostgreSQL not restarted, the read service on the new file"
[ "$(login_check postgres "$third_superuser" app_readonly "$third_readonly" app_writer "$third_writer" \
      postgres "$second_superuser" app_readonly "$second_readonly" app_writer "$second_writer" \
      postgres "$first_superuser" app_readonly "$first_readonly" app_writer "$first_writer" \
      2> "$WORK/logins-replaced.err")" \
    = $'accepted postgres\naccepted app_readonly\naccepted app_writer\nrefused\nrefused\nrefused\nrefused\nrefused\nrefused' ] \
  || { mask < "$WORK/logins-replaced.err" | tail -n 5 >&2; fail "new connections do not accept exactly the newest passwords"; }
ok "new connections: the three roles accepted with the newest passwords, refused with both earlier ones"
served_unchanged publications-after-replaced.json
[ "$(records 2>/dev/null)" = "$EXPECTED_RECORDS" ] || fail "the roster's records changed"
ok "the same publication served and the roster's records kept, after two password changes"

# --- 8. backup and restore into a new stack ---------------------------------------------

echo "[8/9] Dumping the database, removing the stack with its volume, restoring the dump into a new one..."
# The verification query of DEPLOYMENT.md, "Backup and restore": people,
# stored publications, publications served.
COUNTS_QUERY='SELECT (SELECT count(*) FROM app.people), (SELECT count(*) FROM biblio.publications), (SELECT count(*) FROM biblio.publications_canon_web)'
counts() { bounded 60 dc exec -T postgres psql -U postgres -d people_db -XAtc "$COUNTS_QUERY"; }
counts_before="$(counts 2> "$WORK/counts-before.err")" || { mask < "$WORK/counts-before.err" >&2; fail "the verification query failed"; }
[ "$counts_before" = "3|1|1" ] || fail "the verification query counts $counts_before, not the roster's three people and one publication, stored and served"
DUMP="$WORK/people_db.dump"
( umask 077 && bounded 300 dc exec -T postgres pg_dump -U postgres -Fc people_db > "$DUMP" ) 2> "$WORK/dump.err" \
  || { mask < "$WORK/dump.err" >&2; fail "pg_dump failed"; }
[ -s "$DUMP" ] && [ "$(ls -l "$DUMP" | cut -c1-10)" = "-rw-------" ] || fail "the dump is empty, or readable by other users"
ok "pg_dump wrote the database into a file of mode 0600; the verification query counts $counts_before"
# Taking the stack down deletes its containers' logs: they are kept for the
# checks of step 9.
dc logs --no-color > "$WORK/all-before-restore.log" 2>&1 || fail "cannot save the containers' logs before removing the stack"
bounded 120 dc down --volumes > "$WORK/down-volumes.log" 2>&1 \
  || { mask < "$WORK/down-volumes.log" | tail -n 20 >&2; fail "docker compose down --volumes failed"; }
left="$(owned ps && owned volume && owned network)" || fail "cannot list the project's resources after docker compose down --volumes"
[ -z "$left" ] || fail "docker compose down --volumes left a container, volume or network"
dc up --wait --wait-timeout "$WAIT_SECONDS" postgres > "$WORK/up-restore.log" 2>&1 \
  || { mask < "$WORK/up-restore.log" | tail -n 20 >&2; fail "docker compose up --wait postgres did not see a healthy database"; }
[ -z "$(container_of api)" ] || fail "docker compose up --wait postgres started the read service"
dc logs --no-color postgres > "$WORK/postgres-restore-first.log" 2>&1 || true
grep -qF "app_readonly and app_writer have the passwords from their files; maintenance cannot log in." "$WORK/postgres-restore-first.log" \
  || fail "the new stack's PostgreSQL did not initialise a new volume"
ok "docker compose down --volumes removed the stack; docker compose up --wait postgres initialised a new volume and started no read service"
bounded 60 dc stop api > "$WORK/stop-api.log" 2>&1 || { mask < "$WORK/stop-api.log" >&2; fail "docker compose stop api failed"; }
bounded 60 dc exec -T postgres dropdb -U postgres --force people_db > "$WORK/dropdb.log" 2>&1 \
  || { mask < "$WORK/dropdb.log" >&2; fail "dropdb failed"; }
bounded 60 dc exec -T postgres createdb -U postgres people_db > "$WORK/createdb.log" 2>&1 \
  || { mask < "$WORK/createdb.log" >&2; fail "createdb failed"; }
bounded_input 300 dc exec -T postgres pg_restore -U postgres -d people_db --single-transaction --exit-on-error \
    < "$DUMP" > "$WORK/restore.log" 2>&1 \
  || { mask < "$WORK/restore.log" | tail -n 20 >&2; fail "pg_restore failed"; }
wait_healthy up-restored.log
counts_after="$(counts 2> "$WORK/counts-after.err")" || { mask < "$WORK/counts-after.err" >&2; fail "the verification query failed after the restore"; }
[ "$counts_after" = "$counts_before" ] || fail "after the restore the verification query counts $counts_after, before the dump $counts_before"
ok "dropdb, createdb and pg_restore in one transaction, then docker compose up --wait: healthy; the verification query counts $counts_after again"
served_unchanged publications-after-restore.json
[ "$(records 2>/dev/null)" = "$EXPECTED_RECORDS" ] || fail "the restored database holds other people than the roster's"
bounded 120 dc run --rm -T ingest python -m people_pubs.sync.attribution_rules recompute --refresh-only \
    > "$WORK/refresh-restored.log" 2>&1 || { mask < "$WORK/refresh-restored.log" | tail -n 20 >&2; fail "the refresh failed on the restored database"; }
grep -q "snapshots refreshed together: 1 canonical publications" "$WORK/refresh-restored.log" || fail "the refresh did not publish the restored publication"
ok "the new stack serves the same publication and holds the roster's records, and app_writer refreshes the restored snapshot"

# --- 9. environments, log level and leaks -----------------------------------------------

echo "[9/9] Checking the environments, the log level, the role model, and every output for passwords and roster data..."
bounded 30 dc exec -T -u postgres postgres sh -c 'tr "\0" "\n" < /proc/1/environ' \
    > "$WORK/postgres-environ.txt" 2> "$WORK/postgres-environ.err" \
  && grep -q '^PATH=' "$WORK/postgres-environ.txt" \
  || { mask < "$WORK/postgres-environ.err" >&2; fail "cannot read the database server's environment"; }
bounded 30 dc exec -T api sh -c 'tr "\0" "\n" < /proc/1/environ' \
    > "$WORK/application-environ.txt" 2> "$WORK/application-environ.err" \
  && grep -q '^PATH=' "$WORK/application-environ.txt" \
  || { mask < "$WORK/application-environ.err" >&2; fail "cannot read the read service's environment"; }
! grep -q '^POSTGRES_' "$WORK/postgres-environ.txt" || fail "the database server's environment holds POSTGRES_* settings"
! grep -qE '^(PGPASSWORD|POSTGRES_PASSWORD)=' "$WORK/application-environ.txt" || fail "the read service's environment holds a password"
grep -qx 'PEOPLE_PUBS_API_LOG_LEVEL=WARNING' "$WORK/application-environ.txt" || fail "the .env file's log level did not reach the read service"
ok "the environments of the database server and of the read service hold no password setting"
# The role model of DEPLOYMENT.md, "Roles and personal data", with its commands.
role_model="$(superuser_sql 2> "$WORK/role-model.err" <<'SQL'
SELECT 'maintenance can log in: ' || rolcanlogin::text || ', has a password: ' || (rolpassword IS NOT NULL)::text
FROM pg_catalog.pg_authid WHERE rolname = 'maintenance';
SELECT 'app_readonly may use the pii schema: ' || has_schema_privilege('app_readonly', 'pii', 'USAGE')::text;
SELECT 'pii tables app_writer or app_readonly may use: ' || count(*)
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'pii' AND c.relkind IN ('r', 'p')
  AND (has_table_privilege('app_writer', c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
       OR has_table_privilege('app_readonly', c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'));
SELECT 'pii functions app_writer may execute: '
       || string_agg(p.proname || CASE WHEN p.prosecdef THEN '' ELSE ' (not a definer function)' END, ' ' ORDER BY p.proname)
FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'pii' AND has_function_privilege('app_writer', p.oid, 'EXECUTE');
SELECT 'pii.people_pii rows: ' || count(*) FROM pii.people_pii;
SET ROLE maintenance;
SELECT 'as maintenance: ' || count(*) FROM pii.people_pii;
SQL
)" || { mask < "$WORK/role-model.err" >&2; fail "the superuser could not read pii.people_pii as maintenance"; }
pii_rows="$(sed -n 's/^pii.people_pii rows: //p' <<<"$role_model")"
[ "$role_model" = "maintenance can log in: false, has a password: false
app_readonly may use the pii schema: false
pii tables app_writer or app_readonly may use: 0
pii functions app_writer may execute: ensure_people_pii get_introduction_contacts log_introduction_email merge_people_verified_emails move_publication_author_emails upsert_publication_author_email
pii.people_pii rows: $pii_rows
as maintenance: $pii_rows" ] && [ "${pii_rows:-0}" -ge 2 ] || fail "the role model differs: $role_model"
refused_as() {  # refused_as <role> <query> <error>: the query fails for that role with that error
  ! printf 'SET ROLE %s;\n%s\n' "$1" "$2" | superuser_sql > "$WORK/refused.out" 2> "$WORK/refused.err" \
    && grep -qF "$3" "$WORK/refused.err"
}
refused_as maintenance "SELECT count(*) FROM app.people;" "permission denied for schema app" \
  || fail "maintenance may read app.people"
refused_as app_writer "SELECT count(*) FROM pii.people_pii;" "permission denied for table people_pii" \
  || fail "app_writer may read pii.people_pii directly"
ok "maintenance cannot log in; app_writer reaches pii through its six definer functions only, app_readonly not at all; the superuser on the socket, after SET ROLE maintenance, reads the $pii_rows pii.people_pii rows and is refused app.people; app_writer is refused pii.people_pii"
# The documented command gives maintenance a login and a password, here from
# standard input rather than a terminal, as 020_role_passwords.sh does.
maintenance_password="$(new_password)"
SECRET_VALUES+=("$maintenance_password")
printf '%s\n%s\n' "$maintenance_password" "$maintenance_password" \
  | bounded_input 60 dc exec -T -u postgres postgres setsid --wait psql -X -q -v ON_ERROR_STOP=1 -d people_db \
      -c 'ALTER ROLE maintenance LOGIN' -c '\password maintenance' > "$WORK/maintenance-password.log" 2>&1 \
  || { mask < "$WORK/maintenance-password.log" >&2; fail "the documented command did not give maintenance a password"; }
[ "$(login_check maintenance "$maintenance_password" 2> "$WORK/logins-maintenance.err")" = "accepted maintenance" ] \
  || { mask < "$WORK/logins-maintenance.err" | tail -n 5 >&2; fail "maintenance cannot log in with the password it was given"; }
superuser_sql <<<"ALTER ROLE maintenance NOLOGIN PASSWORD NULL;" > /dev/null 2> "$WORK/maintenance-nologin.err" \
  || fail "the documented command could not take the login away from maintenance again"
[ "$(login_check maintenance "$maintenance_password" 2>> "$WORK/logins-maintenance.err")" = refused ] \
  || fail "maintenance can still log in after its login was taken away"
ok "the documented commands give maintenance a password it logs in with over TCP, and take the login away again"
dc logs --no-color > "$WORK/all.log" 2>&1 || fail "cannot save the containers' logs"
dc logs --no-color api > "$WORK/api.log" 2>&1 || fail "cannot save the read service's log"
! grep -qF "uvicorn.access" "$WORK/api.log" || fail "the read service logged requests at the .env file's log level WARNING"
ok "the read service logs no request line at the .env file's log level WARNING"
ids="$(owned ps)" && [ -n "$ids" ] || fail "cannot list the project's containers"
for id in $ids; do
  docker inspect "$id" >> "$WORK/inspect.json" || fail "cannot inspect the container $id"
done
if leaks_in "$WORK"/*.log "$WORK"/*.json "$WORK"/*.txt "$WORK"/*.err "$WORK"/*.csv; then
  fail "a password occurs in a log, the configuration, docker inspect output or a container environment"
fi
ok "no password of any generation in the containers' logs, those of the first stack included, the commands' output, the rendered configuration, docker inspect output or a container environment"
if grep -qsF -f <(printf '%s\n' "${ROSTER_VALUES[@]}") "$WORK"/roster-*.log "$WORK/all-before-restore.log" "$WORK/all.log"; then
  fail "a name or address of the roster occurs in the output of an import or in a container's log"
fi
ok "no name or address of the roster in the imports' output or the containers' logs"

echo "      tested: the image $image_ref ($IMAGE_ID) and the PostgreSQL image $postgres_ref ($POSTGRES_ID) with this checkout's compose.yaml, in a directory that holds nothing else of the checkout but create_secrets.sh and set_passwords.sh"
echo "PASS: the operator's workflow -- an .env configuration on the ordinary network, a private roster on standard input, ingestion, refresh, read service and export, both ways of changing the passwords with set_passwords.sh, a backup restored into a new stack, and the role model -- worked as documented, and kept its secrets out of every output."
