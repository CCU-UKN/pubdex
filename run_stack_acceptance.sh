#!/usr/bin/env bash
# The acceptance test of the Compose deployment: the real compose.yaml, the
# real application image and the real ingestion path, in a Compose project
# that this run creates, owns and removes again.
#
#   ./run_stack_acceptance.sh                         # builds both images from this checkout
#   ./run_stack_acceptance.sh --image <reference> [--postgres-image <reference>]
#                                                     # tests images Docker already has
#   ./run_stack_acceptance.sh --image <reference> --pull [--expect-version <version>] [--expect-revision <commit>]
#   ./run_stack_acceptance.sh --release --image <reference> [--postgres-image <reference>] [--pull] \
#       --expect-version <version> --expect-revision <commit>   # verifies a release
#
# What one run does, stopping at the first failed check:
#
#   1. selects the application image: without --image it builds this checkout
#      with build_image.sh under a tag of its own and checks that its labels
#      are this checkout's VERSION and revision; with --image it uses that
#      image, pulled first with --pull, never builds or substitutes one, and
#      checks the digest the reference names and the version and revision
#      that --expect-version and --expect-revision require. It then reads the
#      compose.yaml the image carries and selects the one to start (Identity,
#      below), and checks the image's files as its user 10001 finds them:
#      under /opt/pubdex root's, directories 755 and files 644, readable and
#      not writable for that user, who may write nothing else in the image
#      but the bootstrap volume's mount point and the temporary directories;
#      and lists the schema changes the image carries, which have to be
#      there, and for a build this checkout's DB/schema/, file for file.
#      The PostgreSQL image is selected the same way -- built with the
#      application image, given with --postgres-image, or else the one the
#      compose.yaml it starts names by default -- and its labels checked as
#      the application image's; once the stack runs, SQL/XML has to be
#      unavailable in its server (postgres.Dockerfile);
#   2. creates the three secret files with create_secrets.sh, in the run's
#      private temporary directory, and the project directory, a directory of
#      the run's own that holds that compose.yaml and nothing else;
#   3. starts it with `docker compose up --wait` on empty volumes and checks
#      the first start: the bootstrap service's copy of the schema bootstrap
#      the image carries, which PostgreSQL ran, and the role passwords; the
#      schema-upgrade service, which has to have exited with status 0 after
#      applying every schema change the image carries, and the record of
#      them in the database, read as app_readonly: the image's changes by
#      number, name and SHA-256, applied by the image's version; that
#      the containers mount the project's volumes and the secret files and
#      nothing else; the image the read service runs, its publication on
#      loopback alone as Docker reports the binding, no PostgreSQL port, and
#      the internal network below. The binding is configuration: no check
#      connects from another host, and whether other hosts are refused rests
#      on the Engine (DB/lib/docker_versions.sh) and the host's firewall;
#   4. queries the read service over HTTP: liveness, readiness, an empty list;
#   5. runs the synthetic demonstration inside the stack as app_writer
#      (`docker compose run --rm demo`: the bundled roster, ORCID and
#      Crossref payloads, an attribution rule and the snapshot refresh), then
#      reads its publication over HTTP as app_readonly, field by field, and
#      its cumulative provenance in the database;
#   6. stops PostgreSQL and starts it again: readiness fails, liveness holds,
#      and readiness recovers without a restart of the read service;
#   7. recreates every container on the same volumes, then removes the
#      containers and starts them again: no new initialisation, a schema
#      upgrade that finds nothing to do and leaves the record as it was, the
#      same records, app_writer and app_readonly still log in;
#   8. reruns create_secrets.sh, which keeps every file; reads the
#      environments of the database server and of the read service; stops
#      the read service; and checks that no password occurs in the logs of
#      the stack's containers -- each saved before a recreation or `down`
#      removed it -- the output of the one-shot commands, the rendered
#      configuration, `docker inspect` output or those environments.
#
# Identity. A deployment is the application image and its compose.yaml: the
# image carries the schema bootstrap (DB/init/) and the compose.yaml it was
# built with, and no file of a checkout takes part (DEPLOYMENT.md, "What
# identifies a deployment"). Every run prints the image's reference, id,
# registry digests and labels, and says whether the compose.yaml it starts is
# the one the image carries. Without --release it starts this checkout's
# compose.yaml, so that a change to it is tested: a development run. A build
# of this checkout has to carry that file unchanged; an image given with
# --image may carry another one, which the run reports. --release verifies a
# release: it requires the labels of both images to be exactly
# --expect-version and --expect-revision, none of them missing, "unknown",
# malformed or "-dirty", and starts the compose.yaml the application image
# carries, which has to name that version as its default application image
# and run the schema upgrade; nothing of this checkout but the test itself
# takes part, and Git is not needed. The version always comes from the label,
# never from a tag: a tag that names another version, and --pull of a
# reference without a digest, are refused as usage errors.
#
# Isolation. The project is named pubdex-acceptance-<run id>, and an extra
# Compose file, DB/tests/compose.acceptance.yaml, puts the label
# pubdex.stack-acceptance.run=<run id> on every container, network and
# volume and makes the network of PostgreSQL and the ingestion containers
# internal: no route off the host, so the demonstration cannot reach a
# metadata provider. A project, label or image tag that exists already stops
# the run before anything is created, and is left as it is. Every `docker
# compose` command names the project, the project directory, both Compose
# files and an environment file of the run's own, and runs with every
# exported variable removed except
# PATH, HOME and Docker's daemon selection (DOCKER_HOST, DOCKER_CONTEXT,
# DOCKER_CONFIG, DOCKER_CERT_PATH, DOCKER_TLS_VERIFY): no COMPOSE_* or
# PEOPLE_PUBS_* setting, .env file or compose.override.yaml of the caller can
# redirect it. The database and ingestion settings are those compose.yaml
# fixes, and nothing reads DB/.env. The read service is published on a
# loopback port Docker picks.
#
# Secrets. The passwords are random, created by create_secrets.sh for this
# run alone, and reach the containers as Compose file secrets only. They are
# never printed or put on a command line; output that the run prints from
# Docker or the containers is masked, and its own records of that output are
# checked for them. No set -x.
#
# Cleanup. Registered before anything is created, it runs on every exit and
# after SIGINT, SIGTERM or SIGHUP, ignoring further signals while it works:
# `docker compose down --volumes` for the project, then the removal of any
# container, network or volume still carrying the run's label or project
# name, of the image tags the run built, and of the run's temporary directory
# with the secret files. It then confirms that none of them is left; if one
# is, or Docker cannot say, it prints the commands that find it and fails --
# with status 1 after an otherwise successful run, and with the earlier
# failure's or the signal's status otherwise. It removes nothing else:
# images it pulled or was given, the PostgreSQL image, and build cache stay,
# and nothing is pruned. SIGKILL skips it; the label finds what is left.
#
# Network access. Docker needs the PostgreSQL image of compose.yaml and,
# when building, the base image and the Python packages from the package
# index, unless it has them; --pull fetches the image named. No metadata
# provider is contacted.
#
# Needs Bash, Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2, to build
# the image as to run it, with the Compose plugin 2.24.0 or later
# (DB/lib/docker_versions.sh says why), a daemon on this machine that can
# bind-mount files of the temporary directory, ports it publishes on
# 127.0.0.1 reachable from here, and Python 3 for the HTTP checks
# (.venv/bin/python, else python3); Git only to label a build, and only asked
# (DB/lib/release_identity.sh). Exit status: 0 when every check passed and
# the cleanup was confirmed, 1 for a failed check or cleanup, 2 for a usage
# error, 130, 143 or 129 after a signal.
set -euo pipefail

# A shell function the caller exported, or one a BASH_ENV file defined, would
# stand in for docker, git or any other command below. None of this script's
# own exists yet, so every function defined now goes.
while read -r _declare _flags _function; do
  if [ -n "$_function" ]; then unset -f "$_function"; fi
done <<<"$(declare -F)"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERRIDE="$ROOT/DB/tests/compose.acceptance.yaml"
LABEL_KEY="pubdex.stack-acceptance.run"
PROJECT_LABEL="com.docker.compose.project"
DEMO_DOI="10.5555/pubdex-fixture-001"
WAIT_SECONDS="${STACK_WAIT_SECONDS:-300}"
# shellcheck source=DB/lib/disposable_postgres.sh
source "$ROOT/DB/lib/disposable_postgres.sh"  # for clear_exports
# shellcheck source=DB/lib/docker_versions.sh
source "$ROOT/DB/lib/docker_versions.sh"
# shellcheck source=DB/lib/release_identity.sh
source "$ROOT/DB/lib/release_identity.sh"
# shellcheck source=DB/lib/compose_acceptance.sh
source "$ROOT/DB/lib/compose_acceptance.sh"  # isolated, bounded, mask, leaks_in, http, foreign_mounts

usage() {
  cat <<'USAGE'
usage: run_stack_acceptance.sh [--image <reference> [--postgres-image <reference>] [--pull]
                                 [--expect-version <version>] [--expect-revision <commit>]]
       run_stack_acceptance.sh --release --image <reference> [--postgres-image <reference>] [--pull]
                               --expect-version <version> --expect-revision <commit>

Starts compose.yaml in a Compose project of its own, runs the synthetic
demonstration inside it, checks the read service, readiness, persistence and
secrets, and removes everything it created. The stack is the application
image, which carries the schema, and a compose.yaml alone in a directory of
the run's own: this checkout's, or with --release the one the image carries.

  (no option)                 build both images from this checkout (build_image.sh)
                              under tags of the run's own, removed afterwards
  --image <reference>         test this application image instead -- name:tag,
                              name@sha256:... or both; Docker has to have it
                              already unless --pull is given; nothing is built
  --postgres-image <reference>
                              with --image: test this PostgreSQL image, not the
                              one the started compose.yaml names by default
  --pull                      pull the images first
  --expect-version <version>  require this release version, such as 0.1.0, in
                              the image's org.opencontainers.image.version label
  --expect-revision <commit>  require this source revision in the image's
                              org.opencontainers.image.revision label: a full
                              commit id, with -dirty for a local build
  --release                   verify a release: require both images to carry
                              exactly that version and commit, and start the
                              compose.yaml the application image carries; needs
                              --image, --expect-version and --expect-revision,
                              and with --pull digests of both images
  -h, --help                  show this help

STACK_WAIT_SECONDS bounds each wait for the stack to become healthy (300).
USAGE
}

die_usage() {
  echo "run_stack_acceptance.sh: $1" >&2
  usage >&2
  exit 2
}

image_ref=""
postgres_ref=""
pull=0
expect_version=""
expect_revision=""
release=0
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
    --pull)
      pull=1
      shift
      ;;
    --expect-version)
      if [ $# -lt 2 ] || [ -z "$2" ]; then die_usage "--expect-version needs a value"; fi
      expect_version="$2"
      shift 2
      ;;
    --expect-revision)
      if [ $# -lt 2 ] || [ -z "$2" ]; then die_usage "--expect-revision needs a value"; fi
      expect_revision="$2"
      shift 2
      ;;
    --release)
      release=1
      shift
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
if [ "$release" = 1 ] && [ -z "$image_ref" ]; then
  die_usage "--release needs --image: it verifies a release image, and builds none"
fi
if [ "$pull" = 1 ] && [ -z "$image_ref" ]; then
  die_usage "--pull needs --image"
fi
if [ -n "$postgres_ref" ] && [ -z "$image_ref" ]; then
  die_usage "--postgres-image needs --image; without --image both images are built from this checkout"
fi
if [ -n "$expect_version" ] && [ -z "$image_ref" ]; then
  die_usage "--expect-version needs --image; a build is checked against this checkout's VERSION"
fi
if [ -n "$expect_revision" ] && [ -z "$image_ref" ]; then
  die_usage "--expect-revision needs --image; a build is checked against this checkout"
fi
if [ -n "$expect_version" ] && ! [[ "$expect_version" =~ $RELEASE_VERSION_RE ]]; then
  die_usage "--expect-version needs a release version such as 0.1.0 (MAJOR.MINOR.PATCH, optionally -prerelease), not '$expect_version'"
fi
if [ -n "$expect_revision" ] && ! [[ "${expect_revision%-dirty}" =~ $COMMIT_RE ]]; then
  die_usage "--expect-revision needs a full commit id -- 40 or 64 lower-case hexadecimal digits, as Git prints HEAD -- optionally with -dirty for a local build, not '$expect_revision'"
fi
if [ "$release" = 1 ]; then
  [ -n "$expect_version" ] || die_usage "--release needs --expect-version <version>: a release version is never taken from a tag"
  [ -n "$expect_revision" ] || die_usage "--release needs --expect-revision <commit>, the release commit"
  if [[ "$expect_revision" == *-dirty ]]; then
    die_usage "--release needs the release commit itself; a -dirty revision is that of a local build"
  fi
  if [ "$pull" = 1 ] && ! [[ "$image_ref" =~ @sha256:[0-9a-f]{64}$ ]]; then
    die_usage "--release with --pull needs a reference with its digest, <name>:<version>@sha256:<digest>: a tag alone can be moved to another image"
  fi
  if [ "$pull" = 1 ] && ! [[ "$postgres_ref" =~ @sha256:[0-9a-f]{64}$ ]]; then
    die_usage "--release with --pull needs --postgres-image with its digest, <name>:<version>@sha256:<digest>: a tag alone can be moved to another image"
  fi
  image_tag="$(reference_tag "$image_ref")"
  if [[ "$image_tag" =~ $RELEASE_VERSION_RE ]] && [ "$image_tag" != "$expect_version" ]; then
    die_usage "--image names the version tag $image_tag, but --expect-version is $expect_version"
  fi
  image_tag="$(reference_tag "$postgres_ref")"
  if [[ "$image_tag" =~ $RELEASE_VERSION_RE ]] && [ "$image_tag" != "$expect_version" ]; then
    die_usage "--postgres-image names the version tag $image_tag, but --expect-version is $expect_version"
  fi
fi
if ! [[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  die_usage "STACK_WAIT_SECONDS must be a positive number of seconds"
fi

prerequisite() {
  echo "run_stack_acceptance.sh: $1" >&2
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

# in_checkout <function> [arguments]: a function of DB/lib/release_identity.sh,
# whose git runs with isolated()'s PATH and HOME and nothing else, so that no
# GIT_DIR, GIT_WORK_TREE or GIT_INDEX_FILE of the caller's can make another
# repository answer for this checkout.
in_checkout() {
  ( clear_exports
    export PATH="$SAVED_PATH" HOME="$SAVED_HOME" LC_ALL=C
    "$@" )
}

# --- the run's identity, cleanup and private directory ------------------------------

RUN_ID="$(date +%Y%m%d%H%M%S)-$$-$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"
PROJECT="pubdex-acceptance-$RUN_ID"
BUILT_TAG=""
BUILT_POSTGRES_TAG=""
CLAIMED=0  # set once no resource of the project or the run existed before it
WORK=""
ENV_FILE=""
PROJECT_DIR=""  # the project directory: compose.yaml and nothing else
SECRETS=""
SECRET_VALUES=()

# Each label filter lists what carries the run's label or the project's name.
owned() {  # owned <ps|volume|network>: ids, one per line; fails when Docker cannot say
  local filter kind="$1"
  for filter in "label=$LABEL_KEY=$RUN_ID" "label=$PROJECT_LABEL=$PROJECT"; do
    case "$kind" in
      ps) docker ps -aq --filter "$filter" ;;
      volume) docker volume ls -q --filter "$filter" ;;
      network) docker network ls -q --filter "$filter" ;;
    esac || return 1
  done
}

remove_owned() {  # everything of the project and the run, and the image tag it built
  local id
  if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ] && [ -f "$PROJECT_DIR/compose.yaml" ]; then
    # The volumes go as well, so a container gets no long grace period to
    # shut down cleanly -- PostgreSQL in its first initialisation would use
    # all of it.
    compose --profile ingest --profile demo down --volumes --remove-orphans --timeout 5 >/dev/null 2>&1 || true
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
    echo "  docker ps -a --filter label=$LABEL_KEY=$RUN_ID" >&2
    echo "  docker volume ls --filter label=$LABEL_KEY=$RUN_ID" >&2
    echo "  docker network ls --filter label=$LABEL_KEY=$RUN_ID" >&2
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
      echo "WARNING: could not remove the run's temporary directory $WORK, which holds its secret files." >&2
      [ "$rc" -ne 0 ] || rc=1
    fi
  fi
  if [ "$rc" -eq 0 ]; then
    echo "      cleanup: the project's containers, networks and volumes, the run's image tags and secret files are gone"
  fi
  exit "$rc"
}

exec 3>&1 4>&2  # the run's own output, for the cleanup
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

compose() {
  isolated docker compose --project-name "$PROJECT" --project-directory "$PROJECT_DIR" \
    --file "$PROJECT_DIR/compose.yaml" --file "$OVERRIDE" --env-file "$ENV_FILE" "$@"
}

diagnose() {  # the project's state and log tails, masked
  [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ] && [ -f "$PROJECT_DIR/compose.yaml" ] || return 0
  echo "--- diagnostics for run $RUN_ID" >&2
  compose ps -a 2>&1 | mask >&2 || true
  compose logs --no-color --tail 30 2>&1 | mask >&2 || true
}

fail() {
  echo "FAIL: $*" >&2
  diagnose
  exit 1
}

ok() { echo "  ok  $*"; }

PORT=""
read_port() {  # a port Docker picks changes whenever the container starts
  local binding
  binding="$(compose port api 8000 2>/dev/null)" || binding=""
  if ! [[ "$binding" =~ ^127\.0\.0\.1:([0-9]+)$ ]]; then
    fail "the read service is not published on a single loopback port (${binding:-none})"
  fi
  PORT="${BASH_REMATCH[1]}"
}

ready() { [ "$(http /readyz "$WORK/readyz.json")" = 200 ]; }
not_ready() { [ "$(http /readyz "$WORK/readyz.json")" = 503 ]; }
container_of() { compose ps -q "$1" 2>/dev/null; }
healthy() {
  [ "$(docker inspect --format '{{.State.Health.Status}}' "$(container_of "$1")" 2>/dev/null)" = healthy ]
}

as_readonly() {  # a query as app_readonly, through PostgreSQL's socket in its container
  bounded 60 compose exec -T postgres psql -XqtA -v ON_ERROR_STOP=1 -U app_readonly -d people_db -c "$1"
}

schema_record() {  # the record of the schema changes: number|name|SHA-256|release|applied at, one per line
  as_readonly "SELECT number || '|' || name || '|' || sha256 || '|' || release || '|' || applied_at
    FROM pubdex.schema_changes ORDER BY number"
}

writer_identity() {  # what an ingestion container connects as
  bounded 120 compose run --rm -T ingest python -c \
    'import people_pubs.config, psycopg; print(*psycopg.connect().execute("SELECT current_user, session_user").fetchone())'
}

publication_check() {  # publication_check <page file>: the demonstration's item, or a failure
  isolated "$PY" -I - "$1" "$DEMO_DOI" <<'PY'
import json, sys
page = json.load(open(sys.argv[1], encoding="utf-8"))
items = [item for item in page["items"] if item["doi"] == sys.argv[2]]
assert len(items) == 1, f"{len(items)} items with the demonstration DOI"
item = items[0]
expected = {
    "title": "Synthetic Collective Behaviour Paper",
    "year": 2024,
    "venue": "Journal of Synthetic Metadata",
    "internal_authors": "Ada Example",
}
assert list(item) == ["pub_id", "doi", "title", "year", "venue", "source_of_truth", "internal_authors"], list(item)
assert {key: item[key] for key in expected} == expected, item
assert set(item["source_of_truth"].split("+")) == {"crossref", "orcid"}, item["source_of_truth"]
assert isinstance(item["pub_id"], int) and page["next_after"] is None, page
print(item["pub_id"], item["source_of_truth"])
PY
}

echo "Stack acceptance run $RUN_ID: project $PROJECT"
echo "      docker $engine_version, compose $compose_version"

# Never take over what exists already, however unlikely a collision.
if ! existing="$(owned ps && owned volume && owned network)"; then
  echo "ERROR: Docker cannot list the resources of project $PROJECT; nothing was created." >&2
  exit 1
fi
if [ -n "$existing" ]; then
  echo "ERROR: resources of project $PROJECT or run $RUN_ID exist already; not touching them." >&2
  exit 1
fi
CLAIMED=1
WORK="$(mktemp -d)"
chmod 700 "$WORK"
SECRETS="$WORK/secrets"
ENV_FILE="$WORK/stack.env"
PROJECT_DIR="$WORK/project"

# --- 1. the image -----------------------------------------------------------------------

echo "[1/8] Selecting the application image, its compose.yaml and the PostgreSQL image..."
if [ -z "$image_ref" ]; then
  tag="pubdex-acceptance:$RUN_ID"
  postgres_tag="pubdex-acceptance-postgres:$RUN_ID"
  for existing_tag in "$tag" "$postgres_tag"; do
    if docker image inspect "$existing_tag" >/dev/null 2>&1; then
      echo "ERROR: an image tagged $existing_tag exists already; not touching it." >&2
      exit 1
    fi
  done
  BUILT_TAG="$tag"
  BUILT_POSTGRES_TAG="$postgres_tag"
  # The labels build_image.sh has to record, by its rules and in the cleared
  # environment it runs in: the release version of VERSION, and the commit,
  # with "-dirty" for a change Git reports or an ignored Python module of the
  # package or schema change, which the build takes in.
  expected_revision="$(in_checkout checkout_revision "$ROOT" "${IMAGE_CONTEXT_PATHSPECS[@]}")"
  expected_version="$(head -n 1 "$ROOT/VERSION" 2>/dev/null | tr -d '[:space:]')" || expected_version=""
  if ! isolated bash "$ROOT/build_image.sh" --tag "$tag" --postgres-tag "$postgres_tag" > "$WORK/build.log" 2>&1; then
    tail -n 40 "$WORK/build.log" >&2
    fail "build_image.sh failed"
  fi
  image_ref="$tag"
  postgres_ref="$postgres_tag"
  ok "built $tag and $postgres_tag from this checkout with build_image.sh"
else
  if [ "$pull" = 1 ]; then
    if ! bounded 900 isolated docker pull "$image_ref" > "$WORK/pull.log" 2>&1; then
      tail -n 20 "$WORK/pull.log" >&2
      fail "docker pull $image_ref failed"
    fi
    ok "pulled $image_ref"
  elif ! docker image inspect "$image_ref" >/dev/null 2>&1; then
    fail "Docker has no image $image_ref; pull it first, or pass --pull. Nothing is built in its place."
  fi
fi
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$image_ref")" || fail "cannot inspect $image_ref"
label() { docker image inspect --format "{{index .Config.Labels \"$1\"}}" "$IMAGE_ID"; }
image_version="$(label org.opencontainers.image.version)" \
  && image_revision="$(label org.opencontainers.image.revision)" \
  && image_transform="$(label pubdex.transform-version)" \
  && image_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE_ID")" \
  || fail "cannot read the labels and platform of $image_ref"
echo "      image $image_ref"
echo "      id $IMAGE_ID, $image_platform"
echo "      version $image_version, revision $image_revision, transform version $image_transform"
repo_digests="$(docker image inspect --format '{{join .RepoDigests " "}}' "$IMAGE_ID")"
echo "      registry digests: ${repo_digests:-none (not pulled from a registry)}"
if [[ "$image_ref" == *@sha256:* ]]; then
  digest="${image_ref##*@}"
  docker image inspect --format '{{join .RepoDigests "\n"}}' "$IMAGE_ID" | grep -q "@$digest\$" \
    || fail "the image Docker resolved for $image_ref does not carry the digest $digest"
  ok "the image carries the requested digest"
fi
if [ "$BUILT_TAG" = "$image_ref" ]; then
  # A build of this checkout: build_image.sh's labels, whatever they are.
  [ "$image_revision" = "$expected_revision" ] \
    || fail "the image's revision label is '$image_revision', expected '$expected_revision'"
  ok "the image's revision label is $expected_revision"
  [ "$image_version" = "$expected_version" ] \
    || fail "the image's release-version label is '$image_version', expected '$expected_version' from VERSION"
  ok "the image's release-version label is $expected_version, this checkout's VERSION"
fi
# What the caller requires of a given image: the label exactly, so one that
# is missing, "unknown" -- the default of a build without build_image.sh --
# or malformed fails, and so does a local build of the required commit.
if [ -n "$expect_version" ]; then
  case "$image_version" in
    "") fail "the image has no release-version label (org.opencontainers.image.version), so nothing shows which release it is; build_image.sh sets it" ;;
    unknown) fail "the image's release-version label is 'unknown': it was not built with build_image.sh, so nothing shows which release it is" ;;
  esac
  [[ "$image_version" =~ $RELEASE_VERSION_RE ]] || fail "the image's release-version label '$image_version' is not a release version"
  [ "$image_version" = "$expect_version" ] \
    || fail "the image's release-version label is '$image_version', expected '$expect_version'"
  ok "the image's release-version label is $expect_version"
fi
if [ -n "$expect_revision" ]; then
  case "$image_revision" in
    "") fail "the image has no revision label (org.opencontainers.image.revision), so nothing shows which commit it was built from; build_image.sh sets it" ;;
    unknown) fail "the image's revision label is 'unknown': it was built outside a Git checkout or without build_image.sh, so nothing shows which commit it was built from" ;;
  esac
  [[ "${image_revision%-dirty}" =~ $COMMIT_RE ]] || fail "the image's revision label '$image_revision' is not a commit id"
  if [ "$image_revision" = "$expect_revision-dirty" ]; then
    fail "the image was built from a working tree of $expect_revision with changes (revision label $image_revision), so it is not that commit's image"
  fi
  [ "$image_revision" = "$expect_revision" ] \
    || fail "the image's revision label is '$image_revision', expected '$expect_revision'"
  ok "the image's revision label is $expect_revision"
fi
[ -n "$image_transform" ] && [ "$image_transform" != unknown ] || fail "the image has no transform-version label"
# The Compose file the image carries, read in a container of the run's own:
# what --release starts, and what this checkout's is compared with.
bounded 120 isolated docker run --rm --network none --label "$LABEL_KEY=$RUN_ID" --entrypoint cat \
    "$IMAGE_ID" /opt/pubdex/compose.yaml > "$WORK/image-compose.yaml" 2> "$WORK/image-compose.err" \
  && [ -s "$WORK/image-compose.yaml" ] \
  || { head -n 5 "$WORK/image-compose.err" >&2
       fail "the image carries no /opt/pubdex/compose.yaml: it predates this deployment, which takes the schema and the Compose file from the image (DEPLOYMENT.md, \"What identifies a deployment\")"; }
# The image's files as its user finds them, whichever builder made it: under
# /opt/pubdex everything root's, directories 755 and files 644, so that user
# 10001 reads and enters all of it and writes none of it; and nothing else in
# the image that it may write but the bootstrap volume's mount point and the
# base image's directories for temporary files, which the read-only root file
# system of compose.yaml hides.
files_status=0
bounded_input 120 isolated docker run --rm -i --network none --label "$LABEL_KEY=$RUN_ID" --user 10001:10001 \
    --entrypoint python "$IMAGE_ID" -I - > "$WORK/image-files.log" 2>&1 <<'PY' || files_status=$?
import os, stat, sys
problems, counts = [], {0o755: 0, 0o644: 0}
for top, subdirs, names in os.walk("/opt/pubdex"):
    paths = [(top, 0o755)] + [(os.path.join(top, name), 0o644) for name in names]
    paths += [(os.path.join(top, name), 0o755) for name in subdirs if os.path.islink(os.path.join(top, name))]
    for path, mode in paths:
        info = os.lstat(path)
        kind = stat.S_ISDIR if mode == 0o755 else stat.S_ISREG
        access = os.R_OK | os.X_OK if mode == 0o755 else os.R_OK
        if (not kind(info.st_mode) or (info.st_uid, info.st_gid) != (0, 0) or stat.S_IMODE(info.st_mode) != mode
                or not os.access(path, access) or os.access(path, os.W_OK)):
            problems.append(f"{path}: {stat.filemode(info.st_mode)} {info.st_uid}:{info.st_gid}")
        counts[mode] += 1
writable = []
for top, subdirs, names in os.walk("/"):
    subdirs[:] = [name for name in subdirs if os.path.join(top, name) not in ("/proc", "/sys", "/dev")]
    writable += [path for path in (os.path.join(top, name) for name in subdirs + names)
                 if not os.path.islink(path) and os.access(path, os.W_OK)]
if sorted(writable) != ["/run/lock", "/tmp", "/var/lib/pubdex/initdb", "/var/tmp"]:
    problems.append("user 10001 may write " + " ".join(sorted(writable)))
print(*problems, sep="\n")
print(f"{counts[0o755]} directories and {counts[0o644]} files under /opt/pubdex")
sys.exit(1 if problems else 0)
PY
if [ "$files_status" -ne 0 ]; then
  tail -n 20 "$WORK/image-files.log" >&2
  fail "the image's files are not root's with modes 755 and 644, readable for user 10001 and closed to its writes"
fi
ok "the image's $(tail -n 1 "$WORK/image-files.log") are root's, 755 and 644: user 10001 reads and enters them and writes none; it may write nothing else in the image but /var/lib/pubdex/initdb and the temporary directories"
# The image's Python packages, from the metadata of the installed
# distributions, which the standard library reads, as the image has no pip:
# every package of the runtime lock it carries, at the locked version; no
# pip the image's Python can import; and no file or directory named as pip,
# its scripts, its distribution or a wheel, such as those ensurepip installs
# pip from (Dockerfile). The check goes by name: it shows that pip and those
# wheels are gone, not that nothing else in the image could install or run
# software. Whether the packages' own requirements are met, pip checks when
# it builds the image. The program checks the file system under its one
# argument, the container's root when it has none.
packages_status=0
bounded_input 120 isolated docker run --rm -i --network none --label "$LABEL_KEY=$RUN_ID" --user 10001:10001 \
    --entrypoint python "$IMAGE_ID" -I - > "$WORK/image-packages.log" 2>&1 <<'PY' || packages_status=$?
import importlib.metadata, importlib.util, os, re, sys
root = sys.argv[1] if len(sys.argv) > 1 else "/"
lock_path = os.path.join(root, "opt/pubdex/requirements.txt")
with open(lock_path, encoding="utf-8") as lock:
    locked = dict(re.findall(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==(\S+)", lock.read(), re.M))
problems = [] if locked else [f"{lock_path} pins no package"]
mismatched = 0
for name, version in sorted(locked.items()):
    try:
        installed = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        installed = None
    if installed != version:
        problems.append(f"{name}: {installed or 'not installed'}, locked {version}")
        mismatched += 1
if importlib.util.find_spec("pip") is not None:
    problems.append("pip can be imported: " + importlib.util.find_spec("pip").origin)
installers = re.compile(r"pip(3(\.\d+)?)?|pip-.*\.(dist-info|whl)|.*\.whl")
skipped = [os.path.join(root, name) for name in ("proc", "sys", "dev")]
for top, subdirs, names in os.walk(root):
    subdirs[:] = [name for name in subdirs if os.path.join(top, name) not in skipped]
    problems += [os.path.join(top, name) for name in subdirs + names if installers.fullmatch(name)]
others = sorted(f"{d.metadata['Name']} {d.version}" for d in importlib.metadata.distributions()
                if re.sub(r"[-_.]+", "-", d.metadata["Name"].lower()) not in locked)
print(*problems, sep="\n")
if mismatched:
    print(f"{mismatched} of the {len(locked)} packages of the runtime lock missing or at another version")
else:
    print(f"{len(locked)} packages of the runtime lock at their locked versions"
          + (", and from the base image " + ", ".join(others) if others else ""))
sys.exit(1 if problems else 0)
PY
if [ "$packages_status" -ne 0 ]; then
  tail -n 20 "$WORK/image-packages.log" >&2
  fail "the image's Python packages are not those of its runtime lock, or it holds pip or a wheel"
fi
ok "the image holds the $(tail -n 1 "$WORK/image-packages.log"); its Python cannot import pip, and no file in it is named as pip, its scripts, its distribution or a wheel"
# The schema changes the image carries, which the schema upgrade of step 3
# has to apply: one line per file of /opt/pubdex/schema, as the record in the
# database holds it -- number|name|SHA-256 of the file.
schema_status=0
bounded_input 120 isolated docker run --rm -i --network none --label "$LABEL_KEY=$RUN_ID" --user 10001:10001 \
    --entrypoint python "$IMAGE_ID" -I - > "$WORK/image-schema.txt" 2> "$WORK/image-schema.err" <<'PY' || schema_status=$?
import hashlib, pathlib
for path in sorted(pathlib.Path("/opt/pubdex/schema").glob("*.sql")):
    print(int(path.name[:4]), path.stem, hashlib.sha256(path.read_bytes()).hexdigest(), sep="|")
PY
if [ "$schema_status" -ne 0 ]; then
  head -n 5 "$WORK/image-schema.err" >&2
  fail "cannot list the schema changes in the image's /opt/pubdex/schema"
fi
[ -s "$WORK/image-schema.txt" ] \
  || fail "the image carries no schema change in /opt/pubdex/schema: it predates the schema upgrade that compose.yaml runs"
SCHEMA_CHANGES="$(wc -l < "$WORK/image-schema.txt" | tr -d '[:space:]') schema change"
[ "${SCHEMA_CHANGES%% *}" = 1 ] || SCHEMA_CHANGES+="s"
SCHEMA_LAST="$(tail -n 1 "$WORK/image-schema.txt" | cut -d '|' -f 2)"
schema_source=""
if [ "$BUILT_TAG" = "$image_ref" ]; then
  # A build of this checkout carries its DB/schema/, file for file.
  expected_schema="$(for file in "$ROOT"/DB/schema/*.sql; do
                       [ -e "$file" ] || continue
                       name="${file##*/}" && digest="$(sha256sum "$file")" \
                         && echo "$((10#${name:0:4}))|${name%.sql}|${digest%% *}"
                     done)"
  [ "$(cat "$WORK/image-schema.txt")" = "$expected_schema" ] \
    || fail "the image built from this checkout does not carry exactly the schema changes of its DB/schema/"
  schema_source=", exactly this checkout's DB/schema/"
fi
ok "the image carries $SCHEMA_CHANGES in /opt/pubdex/schema, the last $SCHEMA_LAST$schema_source"
mkdir "$PROJECT_DIR"
if [ "$release" = 1 ]; then
  default_image="$(compose_default_image "$WORK/image-compose.yaml")"
  [ -n "$default_image" ] || fail "the compose.yaml the image carries names no default application image"
  [ "$(reference_tag "$default_image")" = "$expect_version" ] \
    || fail "the compose.yaml the image carries names the image $default_image, not one of release $expect_version"
  grep -qE '^  schema-upgrade:' "$WORK/image-compose.yaml" \
    || fail "the compose.yaml the image carries has no schema-upgrade service, which brings the database to the schema of the image"
  cp "$WORK/image-compose.yaml" "$PROJECT_DIR/compose.yaml"
  compose_source="the compose.yaml the image carries"
  ok "the run starts the compose.yaml the image carries, which names the release's image $default_image"
else
  cp "$ROOT/compose.yaml" "$PROJECT_DIR/compose.yaml"
  if cmp -s "$ROOT/compose.yaml" "$WORK/image-compose.yaml"; then
    compose_source="this checkout's compose.yaml, which the image carries unchanged"
    ok "the run starts this checkout's compose.yaml, which the image carries unchanged"
  elif [ "$BUILT_TAG" = "$image_ref" ]; then
    fail "the image built from this checkout carries another compose.yaml than this checkout's"
  else
    compose_source="this checkout's compose.yaml, which differs from the one the image carries"
    echo "      note: the run starts this checkout's compose.yaml, which differs from the one the image carries; a development run (--release starts the image's)"
  fi
fi

# The PostgreSQL image: built above, given, or the one the compose.yaml the
# run starts names by default; its labels are checked as the application
# image's are, so that the two images of a release are of one commit.
if [ -z "$postgres_ref" ]; then
  postgres_ref="$(compose_default_image "$PROJECT_DIR/compose.yaml" PEOPLE_PUBS_POSTGRES_IMAGE)"
  [ -n "$postgres_ref" ] || fail "the compose.yaml the run starts names no default PostgreSQL image"
fi
if [ "$pull" = 1 ]; then
  if ! bounded 900 isolated docker pull "$postgres_ref" > "$WORK/pull-postgres.log" 2>&1; then
    tail -n 20 "$WORK/pull-postgres.log" >&2
    fail "docker pull $postgres_ref failed"
  fi
  ok "pulled $postgres_ref"
elif [ "$BUILT_POSTGRES_TAG" != "$postgres_ref" ] && ! docker image inspect "$postgres_ref" >/dev/null 2>&1; then
  fail "Docker has no image $postgres_ref; pull it first, pass --pull, or name another with --postgres-image. Nothing is built in its place."
fi
POSTGRES_ID="$(docker image inspect --format '{{.Id}}' "$postgres_ref")" || fail "cannot inspect $postgres_ref"
postgres_label() { docker image inspect --format "{{index .Config.Labels \"$1\"}}" "$POSTGRES_ID"; }
postgres_version_label="$(postgres_label org.opencontainers.image.version)" \
  && postgres_revision="$(postgres_label org.opencontainers.image.revision)" \
  && postgresql_release="$(postgres_label pubdex.postgresql-version)" \
  && postgres_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$POSTGRES_ID")" \
  || fail "cannot read the labels and platform of $postgres_ref"
echo "      PostgreSQL image $postgres_ref"
echo "      id $POSTGRES_ID, $postgres_platform, PostgreSQL ${postgresql_release:-unknown}"
echo "      version $postgres_version_label, revision $postgres_revision"
echo "      registry digests: $(docker image inspect --format '{{join .RepoDigests " "}}' "$POSTGRES_ID" | sed 's/^$/none (not pulled from a registry)/')"
if [[ "$postgres_ref" == *@sha256:* ]]; then
  docker image inspect --format '{{join .RepoDigests "\n"}}' "$POSTGRES_ID" | grep -q "@${postgres_ref##*@}\$" \
    || fail "the image Docker resolved for $postgres_ref does not carry the digest ${postgres_ref##*@}"
  ok "the PostgreSQL image carries the requested digest"
fi
[ -n "$postgresql_release" ] && [ "$postgresql_release" != unknown ] \
  || fail "the PostgreSQL image has no pubdex.postgresql-version label: it is not a PubDex PostgreSQL image (postgres.Dockerfile)"
if [ "$BUILT_POSTGRES_TAG" = "$postgres_ref" ]; then
  [ "$postgres_revision" = "$expected_revision" ] && [ "$postgres_version_label" = "$expected_version" ] \
    || fail "the PostgreSQL image's labels are version '$postgres_version_label', revision '$postgres_revision', expected '$expected_version', '$expected_revision'"
  ok "the PostgreSQL image's labels are this checkout's: version $expected_version, revision $expected_revision"
fi
if [ -n "$expect_version" ]; then
  [ "$postgres_version_label" = "$expect_version" ] \
    || fail "the PostgreSQL image's release-version label is '$postgres_version_label', expected '$expect_version'"
  ok "the PostgreSQL image's release-version label is $expect_version"
fi
if [ -n "$expect_revision" ]; then
  [ "$postgres_revision" != "$expect_revision-dirty" ] \
    || fail "the PostgreSQL image was built from a working tree of $expect_revision with changes (revision label $postgres_revision)"
  [ "$postgres_revision" = "$expect_revision" ] \
    || fail "the PostgreSQL image's revision label is '$postgres_revision', expected '$expect_revision'"
  ok "the PostgreSQL image's revision label is $expect_revision"
fi
if [ "$release" = 1 ]; then
  default_postgres="$(compose_default_image "$PROJECT_DIR/compose.yaml" PEOPLE_PUBS_POSTGRES_IMAGE)"
  [ "$(reference_tag "$default_postgres")" = "$expect_version" ] \
    || fail "the compose.yaml the image carries names the PostgreSQL image $default_postgres, not one of release $expect_version"
  ok "the compose.yaml the image carries names the release's PostgreSQL image $default_postgres"
fi

# --- 2. secrets and configuration ----------------------------------------------------------

echo "[2/8] Creating the secret files, the run's settings and its project directory..."
( clear_exports
  export PATH="$SAVED_PATH" HOME="$SAVED_HOME" PEOPLE_PUBS_SECRETS_DIR="$SECRETS"
  exec bash "$ROOT/create_secrets.sh" ) > "$WORK/create_secrets.log" 2>&1 \
  || { cat "$WORK/create_secrets.log" >&2; fail "create_secrets.sh failed"; }
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
mkdir "$WORK/secrets-as-created"
cp -p "$SECRETS"/postgres_password "$SECRETS"/app_readonly_password "$SECRETS"/app_writer_password "$WORK/secrets-as-created/"
ok "create_secrets.sh created three random password files (directory 700, files 444)"
cat > "$ENV_FILE" <<EOF
PEOPLE_PUBS_IMAGE=$image_ref
PEOPLE_PUBS_POSTGRES_IMAGE=$postgres_ref
PEOPLE_PUBS_SECRETS_DIR=$SECRETS
PEOPLE_PUBS_HTTP_ADDRESS=127.0.0.1
PEOPLE_PUBS_HTTP_PORT=0
PEOPLE_PUBS_API_LOG_LEVEL=INFO
PEOPLE_PUBS_CROSSREF_MAILTO=
PEOPLE_PUBS_ORG_PATTERNS=
PEOPLE_PUBS_ACCEPTANCE_RUN=$RUN_ID
EOF
[ "$(ls -A "$PROJECT_DIR")" = compose.yaml ] || fail "the project directory holds more than compose.yaml"
compose --profile ingest --profile demo config --format json > "$WORK/config.json" 2> "$WORK/config.err" \
  || { mask < "$WORK/config.err" >&2; fail "docker compose config failed"; }
! leaks_in "$WORK/config.json" || fail "a password occurs in the rendered configuration"
isolated "$PY" -I - "$WORK/config.json" "$PROJECT" "$image_ref" "$RUN_ID" "$LABEL_KEY" "$postgres_ref" <<'PY' || fail "the rendered configuration is not the run's own"
import json, sys
config, project, image, run, label, postgres = json.load(open(sys.argv[1], encoding="utf-8")), *sys.argv[2:]
assert config["name"] == project, config["name"]
services = config["services"]
assert set(services) == {"bootstrap", "postgres", "schema-upgrade", "api", "ingest", "demo"}, sorted(services)
for name in ("bootstrap", "schema-upgrade", "api", "ingest", "demo"):
    assert services[name]["image"] == image, (name, services[name]["image"])
    assert services[name]["labels"][label] == run, name
assert services["postgres"]["labels"][label] == run
assert services["postgres"]["image"] == postgres, services["postgres"]["image"]
assert services["bootstrap"].get("network_mode") == "none" and "networks" not in services["bootstrap"]
# Each service holds the password files it uses and no other: the
# superuser's only PostgreSQL and the schema upgrade, the bootstrap none.
held = {name: sorted(secret["source"] for secret in service.get("secrets", [])) for name, service in services.items()}
assert held == {"bootstrap": [], "postgres": ["app_readonly_password", "app_writer_password", "postgres_password"],
                "schema-upgrade": ["postgres_password"], "api": ["app_readonly_password"],
                "ingest": ["app_writer_password"], "demo": ["app_writer_password"]}, held
# The schema upgrade runs once PostgreSQL is healthy, hardened as the other
# one-shot services, and what uses the database waits for its success.
def condition(name, dependency):
    return services[name].get("depends_on", {}).get(dependency, {}).get("condition")
assert condition("schema-upgrade", "postgres") == "service_healthy"
for name in ("api", "ingest", "demo"):
    assert condition(name, "schema-upgrade") == "service_completed_successfully", name
upgrade = services["schema-upgrade"]
assert upgrade.get("read_only") is True and upgrade.get("cap_drop") == ["ALL"], upgrade
assert "no-new-privileges:true" in upgrade.get("security_opt", []) and "ports" not in upgrade, upgrade
assert config["networks"]["default"].get("internal") is True
assert sorted(services["api"]["networks"]) == ["default", "published"], services["api"]["networks"]
for name in ("postgres", "schema-upgrade", "ingest", "demo"):
    assert sorted(services[name].get("networks", {"default": None})) == ["default"], name
PY
ok "the project directory holds compose.yaml alone; the rendered configuration is the run's project, images and labels, names secret files only, gives each service only its own -- the superuser's to PostgreSQL and the schema upgrade alone -- starts the read service and ingestion after the schema upgrade, keeps the bootstrap service off every network and the schema upgrade and ingestion on the internal one"

# --- 3. the first start, on an empty volume ---------------------------------------------------

echo "[3/8] Starting the stack: docker compose up --wait, on empty volumes..."
started=$SECONDS
compose up -d --wait --wait-timeout "$WAIT_SECONDS" postgres > "$WORK/up-baseline.log" 2>&1 \
  || { mask < "$WORK/up-baseline.log" | tail -n 20 >&2; fail "docker compose up --wait failed"; }
compose logs --no-color bootstrap > "$WORK/bootstrap-first.log" 2>&1 || true
compose up -d --no-deps api > "$WORK/up-without-schema.log" 2>&1 \
  || { mask < "$WORK/up-without-schema.log" | tail -n 20 >&2; fail "cannot start the read service without the schema service"; }
read_port
wait_until 60 not_ready || fail "a fresh volume without the schema service did not report not ready"
grep -qF '"reason":"schema_upgrade_required"' "$WORK/readyz.json" \
  && grep -qF 'database schema version unrecorded' "$WORK/readyz.json" \
  && grep -qF 'python -m people_pubs.schema upgrade' "$WORK/readyz.json" \
  || fail "the fresh volume did not explain its missing schema upgrade"
[ "$(http /livez "$WORK/livez.json")" = 200 ] || fail "liveness failed without the schema service"
if compose exec -T api python -m people_pubs.api.probe > "$WORK/probe-without-schema.log" 2>&1; then
  fail "the readiness probe accepted an unrecorded schema"
fi
grep -qF 'schema_upgrade_required' "$WORK/probe-without-schema.log" \
  && grep -qF 'python -m people_pubs.schema upgrade' "$WORK/probe-without-schema.log" \
  || fail "the probe did not print the schema upgrade message"
ok "without the schema service, the fresh volume reports 503 schema_upgrade_required, schema version unrecorded and the upgrade command; the probe prints it and /livez stays 200"
compose up --wait --wait-timeout "$WAIT_SECONDS" > "$WORK/up-first.log" 2>&1 \
  || { mask < "$WORK/up-first.log" | tail -n 20 >&2; fail "docker compose up --wait failed"; }
ok "bootstrap and schema-upgrade completed, postgres and api healthy $((SECONDS - started))s after docker compose up --wait"
compose logs --no-color postgres > "$WORK/postgres-first.log" 2>&1 || true
grep -qE "bootstrap: /var/lib/pubdex/initdb holds the [0-9]+ files of this image's schema bootstrap" "$WORK/bootstrap-first.log" \
  && grep -qF "running /docker-entrypoint-initdb.d/020_role_passwords.sh" "$WORK/postgres-first.log" \
  && grep -qF "app_readonly and app_writer have the passwords from their files; maintenance cannot log in." "$WORK/postgres-first.log" \
  || fail "the first start did not bootstrap the schema the image carries and provision the role passwords"
if [ "$BUILT_TAG" = "$image_ref" ]; then
  # A build of this checkout carries its DB/init/, file for file.
  for file in "$ROOT"/DB/init/*; do
    digest="$(sha256sum "$file")" && grep -qF "bootstrap: ${file##*/} sha256:${digest%% *}" "$WORK/bootstrap-first.log" \
      || fail "the image built from this checkout does not carry its ${file#"$ROOT/"}"
  done
  [ "$(grep -c ' sha256:' "$WORK/bootstrap-first.log")" = "$(ls "$ROOT/DB/init" | wc -l | tr -d '[:space:]')" ] \
    || fail "the image built from this checkout carries other files than its DB/init/"
fi
ok "the empty volume was initialised from the schema bootstrap the image carries, and the application roles received their passwords"
upgrade_container="$(compose ps -aq schema-upgrade 2>/dev/null)" && [ -n "$upgrade_container" ] \
  || fail "cannot find the schema upgrade's container"
[ "$(docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' "$upgrade_container")" = "exited 0" ] \
  || fail "the schema upgrade did not end with status 0"
compose logs --no-color schema-upgrade > "$WORK/schema-upgrade-first.log" 2>&1 || true
grep -qF "schema: the database holds the baseline and has had no schema change" "$WORK/schema-upgrade-first.log" \
  && grep -qF "schema: done: applied $SCHEMA_CHANGES; the database has every schema change of PubDex $image_version, up to $SCHEMA_LAST" \
       "$WORK/schema-upgrade-first.log" \
  || { mask < "$WORK/schema-upgrade-first.log" | tail -n 10 >&2; fail "the schema upgrade did not apply the image's schema changes to the new database"; }
RECORD="$(schema_record 2> "$WORK/record.err")" \
  || { mask < "$WORK/record.err" >&2; fail "cannot read the record of schema changes as app_readonly"; }
[ "$(cut -d '|' -f 1-4 <<<"$RECORD")" = "$(sed "s/\$/|$image_version/" "$WORK/image-schema.txt")" ] \
  || fail "the database records other schema changes than the image carries, or another release: $(cut -d '|' -f 1-4 <<<"$RECORD" | tr '\n' ' ')"
ok "the schema upgrade exited with status 0 after applying the image's $SCHEMA_CHANGES, which the database records by number, name and SHA-256, up to $SCHEMA_LAST, applied by PubDex $image_version"
mounts="$(foreign_mounts "$PROJECT" "$SECRETS")" || fail "cannot list the mounts of the project's containers"
[ -z "$mounts" ] || fail "a container mounts more than the project's volumes and the secret files: $mounts"
ok "the stack runs from the image and compose.yaml alone: its containers mount the project's two volumes and the secret files, and nothing else"
API_CONTAINER="$(container_of api)" && [ -n "$API_CONTAINER" ] || fail "cannot find the read service's container"
POSTGRES_CONTAINER="$(container_of postgres)" && [ -n "$POSTGRES_CONTAINER" ] || fail "cannot find PostgreSQL's container"
[ "$(docker inspect --format '{{.Image}}' "$API_CONTAINER")" = "$IMAGE_ID" ] || fail "the read service does not run the selected image"
ok "the read service runs the selected image ($IMAGE_ID)"
[ "$(docker inspect --format '{{.Image}}' "$POSTGRES_CONTAINER")" = "$POSTGRES_ID" ] || fail "PostgreSQL does not run the selected PostgreSQL image"
echo "      postgres image $(docker inspect --format '{{.Config.Image}}' "$POSTGRES_CONTAINER"), $(bounded 30 compose exec -T postgres postgres --version 2>/dev/null)"
# SQL/XML, whose parser the PostgreSQL image leaves out, as any role meets it.
if as_readonly "SELECT xmlparse(document '<a/>')" > "$WORK/xml.log" 2>&1; then
  fail "the database server parses XML: its PostgreSQL was built with libxml2"
fi
grep -qF "unsupported XML feature" "$WORK/xml.log" || { cat "$WORK/xml.log" >&2; fail "an XML query failed for another reason than the missing XML support"; }
ok "PostgreSQL runs the selected PostgreSQL image ($POSTGRES_ID), and SQL/XML is unavailable in it: \"unsupported XML feature\""
read_port
postgres_ports="$(docker port "$POSTGRES_CONTAINER")" || fail "cannot list the ports of PostgreSQL's container"
[ -z "$postgres_ports" ] || fail "PostgreSQL is published on a host port"
[ "$(docker port "$API_CONTAINER" | sort -u)" = "8000/tcp -> 127.0.0.1:$PORT" ] || fail "the read service is published on more than its loopback port"
ok "Docker binds the read service to 127.0.0.1:$PORT alone and PostgreSQL to no host port (the configured binding; no connection from another host is tried)"
internal() { docker network inspect --format '{{.Internal}}' "$1" 2>/dev/null; }
postgres_networks="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' "$POSTGRES_CONTAINER")" \
  && [ -n "${postgres_networks// /}" ] || fail "cannot list the networks of PostgreSQL's container"
for network in $postgres_networks; do
  [ "$(internal "$network")" = true ] || fail "PostgreSQL is attached to $network, which is not internal"
done
# A usable default route is one that is up (flag 0x1) and not a reject route
# (0x200), as the kernel lists for IPv6 on the loopback interface.
bounded 120 compose run --rm -T ingest python -I -c '
import pathlib
def usable(flags): return int(flags, 16) & 0x201 == 0x1
v4 = pathlib.Path("/proc/net/route").read_text().splitlines()[1:]
default4 = [r for r in v4 if r.split()[1] == "00000000" and usable(r.split()[3])]
path6 = pathlib.Path("/proc/net/ipv6_route")
v6 = path6.read_text().splitlines() if path6.exists() else []
default6 = [r for r in v6 if r.split()[1] == "00" and usable(r.split()[8])]
print("default routes:", len(default4) + len(default6))
' > "$WORK/routes.log" 2>&1 || { mask < "$WORK/routes.log" >&2; fail "the route check in an ingestion container failed"; }
[ "$(tail -n 1 "$WORK/routes.log")" = "default routes: 0" ] || fail "an ingestion container has a route off the host: $(tail -n 1 "$WORK/routes.log")"
ok "PostgreSQL and the ingestion containers are on internal networks only, with no route off the host"

# --- 4. the read service on an empty database ------------------------------------------------

echo "[4/8] Querying the read service..."
[ "$(http /livez "$WORK/livez.json")" = 200 ] && [ "$(cat "$WORK/livez.json")" = '{"status":"alive"}' ] || fail "GET /livez"
[ "$(http /readyz "$WORK/readyz.json")" = 200 ] && [ "$(cat "$WORK/readyz.json")" = '{"status":"ready"}' ] || fail "GET /readyz"
[ "$(http /v0/publications "$WORK/empty.json")" = 200 ] \
  && [ "$(cat "$WORK/empty.json")" = '{"items":[],"limit":25,"next_after":null}' ] || fail "GET /v0/publications on a new installation"
ok "GET /livez 200, GET /readyz 200, GET /v0/publications 200 with an empty page"

# --- 5. ingestion inside the stack, then the publication over HTTP ----------------------------

echo "[5/8] Running the synthetic demonstration in the stack (docker compose run --rm demo)..."
[ "$(writer_identity 2> "$WORK/writer.err")" = "app_writer app_writer" ] || { mask < "$WORK/writer.err" >&2; fail "an ingestion container does not connect as app_writer"; }
ok "an ingestion container connects as app_writer, with its password file"
bounded 300 compose run --rm -T demo > "$WORK/demo.log" 2>&1 || { mask < "$WORK/demo.log" | tail -n 30 >&2; fail "the demonstration failed"; }
{ mask < "$WORK/demo.log" | grep -E "fixture stage|rules added|snapshots refreshed" || true; } | sort -u | sed 's/^/      /'
grep -q "fixture stage orcid: processed=1 inserted=1" "$WORK/demo.log" \
  && grep -q "fixture stage crossref: processed=1 inserted=0 updated=1" "$WORK/demo.log" \
  && grep -q "snapshots refreshed together: 1 canonical publications" "$WORK/demo.log" \
  || fail "the demonstration did not ingest both payloads and refresh the snapshots"
ok "the roster, the ORCID and Crossref payloads and the attribution rule were ingested as app_writer, and the snapshots refreshed"
[ "$(http '/v0/publications?limit=100' "$WORK/publications.json")" = 200 ] || fail "GET /v0/publications after the demonstration"
demo_item="$(publication_check "$WORK/publications.json" 2> "$WORK/publication-check.err")" \
  || { cat "$WORK/publication-check.err" >&2; fail "the read service does not serve the demonstration publication as expected"; }
DEMO_PUB_ID="${demo_item%% *}"
ok "GET /v0/publications serves $DEMO_DOI with the expected fields (pub_id $DEMO_PUB_ID, ${demo_item#* })"
provenance="$(as_readonly "SELECT source_of_truth || ' ' || (SELECT string_agg(k, ',' ORDER BY k) FROM jsonb_object_keys(source_provenance) AS k)
  || ' ' || (source_provenance->'orcid'->>'transform_version') || ' ' || (source_provenance->'crossref'->>'transform_version')
  FROM biblio.publications WHERE doi = '$DEMO_DOI'" 2> "$WORK/provenance.err")" \
  || { mask < "$WORK/provenance.err" >&2; fail "cannot read the stored provenance as app_readonly"; }
stamp="people_pubs/$image_transform"
[ "$provenance" = "crossref+orcid crossref,orcid $stamp $stamp" ] \
  || fail "the stored provenance is '$provenance', expected cumulative crossref+orcid, each source stamped $stamp"
ok "the database holds cumulative provenance: crossref+orcid, each source stamped $stamp, the image's transform version"
sessions="$(as_readonly "SELECT string_agg(DISTINCT usename, ',') FROM pg_stat_activity WHERE application_name = 'people-pubs-api'" 2>/dev/null)" || sessions=""
[ "$sessions" = app_readonly ] || fail "the read service's sessions run as '${sessions:-nothing}', not app_readonly"
ok "the read service's database sessions run as app_readonly"

# --- 6. database interruption ------------------------------------------------------------------

echo "[6/8] Stopping PostgreSQL and starting it again..."
api_started="$(docker inspect --format '{{.State.StartedAt}}' "$API_CONTAINER")" || fail "cannot inspect the read service's container"
compose stop --timeout 60 postgres > "$WORK/stop-postgres.log" 2>&1 || fail "docker compose stop postgres failed"
started=$SECONDS
wait_until 30 not_ready || fail "GET /readyz did not report the stopped database within 30s"
grep -qF '"reason":"database_unavailable"' "$WORK/readyz.json" || fail "GET /readyz gave another reason: $(cat "$WORK/readyz.json")"
[ "$(http /livez "$WORK/livez.json")" = 200 ] || fail "GET /livez failed while the database was down"
ok "with PostgreSQL stopped, /readyz 503 database_unavailable after $((SECONDS - started))s, /livez 200"
compose start postgres > "$WORK/start-postgres.log" 2>&1 || fail "docker compose start postgres failed"
started=$SECONDS
wait_until "$WAIT_SECONDS" healthy postgres || fail "PostgreSQL did not become healthy again"
wait_until 60 ready || fail "GET /readyz did not recover within 60s"
[ "$(docker inspect --format '{{.State.StartedAt}}' "$API_CONTAINER")" = "$api_started" ] || fail "the read service was restarted"
ok "PostgreSQL healthy again and /readyz 200 $((SECONDS - started))s after its start, without a restart of the read service"

# --- 7. persistence ---------------------------------------------------------------------------

echo "[7/8] Recreating the containers on the same volumes, then down and up..."
volume="${PROJECT}_postgres-data"
volume_identity="$(docker volume inspect --format '{{.Name}} {{.CreatedAt}}' "$volume")" || fail "the volume $volume is missing"
# Recreating a container deletes its log, so every log is saved first; the
# check of step 8 reads them all.
compose logs --no-color > "$WORK/all-before-recreate.log" 2>&1 || fail "cannot save the containers' logs before recreating them"
two_containers() { [ -n "$1" ] && [ "$(wc -l <<<"$1" | tr -d '[:space:]')" = 2 ]; }
containers_before="$(compose ps -q | sort)" && two_containers "$containers_before" \
  || fail "cannot list the two containers of the stack before recreating them"
compose up --wait --wait-timeout "$WAIT_SECONDS" --force-recreate > "$WORK/up-recreate.log" 2>&1 \
  || { mask < "$WORK/up-recreate.log" | tail -n 20 >&2; fail "docker compose up --wait --force-recreate failed"; }
containers_after="$(compose ps -q | sort)" && two_containers "$containers_after" \
  || fail "cannot list the two containers of the stack after recreating them"
[ -z "$(comm -12 <(echo "$containers_before") <(echo "$containers_after"))" ] || fail "a container was not recreated"
[ "$(docker volume inspect --format '{{.Name}} {{.CreatedAt}}' "$volume")" = "$volume_identity" ] || fail "the volume changed"
restart_checks() {  # restart_checks <log name>: a restart, not a bootstrap, with the same records
  compose logs --no-color postgres > "$WORK/$1" 2>&1 || true
  grep -qF "Skipping initialization" "$WORK/$1" && ! grep -qF "020_role_passwords.sh" "$WORK/$1" \
    || fail "PostgreSQL initialised the volume again"
  compose logs --no-color schema-upgrade > "$WORK/schema-upgrade-${1#postgres-}" 2>&1 || true
  grep -qF "schema: nothing to do: the database has every schema change of PubDex $image_version, up to $SCHEMA_LAST" \
       "$WORK/schema-upgrade-${1#postgres-}" && ! grep -qF "schema: applying" "$WORK/schema-upgrade-${1#postgres-}" \
    || { mask < "$WORK/schema-upgrade-${1#postgres-}" | tail -n 10 >&2; fail "the schema upgrade did not find the database at the image's schema"; }
  [ "$(schema_record 2>/dev/null)" = "$RECORD" ] || fail "the record of schema changes changed"
  read_port
  wait_until 60 ready || fail "GET /readyz after the restart"
  [ "$(http '/v0/publications?limit=100' "$WORK/publications-again.json")" = 200 ] || fail "GET /v0/publications after the restart"
  [ "$(publication_check "$WORK/publications-again.json" 2>/dev/null)" = "$demo_item" ] || fail "the restarted stack serves other records"
  [ "$(writer_identity 2> "$WORK/writer-${1%.log}.err")" = "app_writer app_writer" ] || fail "app_writer cannot log in after the restart"
}
restart_checks postgres-recreated.log
ok "every container recreated on the same volumes: no initialisation, a schema upgrade with nothing to do, the same publication (pub_id $DEMO_PUB_ID) served as app_readonly, app_writer logs in"
compose logs --no-color > "$WORK/all-before-down.log" 2>&1 || fail "cannot save the containers' logs before docker compose down"
compose down --timeout 60 > "$WORK/down.log" 2>&1 || fail "docker compose down failed"
left="$(docker ps -aq --filter "label=$PROJECT_LABEL=$PROJECT")" || fail "cannot list the project's containers after docker compose down"
[ -z "$left" ] || fail "docker compose down left containers"
docker volume inspect "$volume" >/dev/null 2>&1 || fail "docker compose down removed the volume"
compose up --wait --wait-timeout "$WAIT_SECONDS" > "$WORK/up-again.log" 2>&1 \
  || { mask < "$WORK/up-again.log" | tail -n 20 >&2; fail "docker compose up --wait after down failed"; }
API_CONTAINER="$(container_of api)" && [ -n "$API_CONTAINER" ] || fail "cannot find the read service's container after docker compose up"
restart_checks postgres-after-down.log
ok "docker compose down kept the volume; up again: no initialisation, a schema upgrade with nothing to do, the same publication, both roles log in"

# --- 8. secrets ---------------------------------------------------------------------------------

echo "[8/8] Checking the secrets and stopping the read service..."
( clear_exports
  export PATH="$SAVED_PATH" HOME="$SAVED_HOME" PEOPLE_PUBS_SECRETS_DIR="$SECRETS"
  exec bash "$ROOT/create_secrets.sh" ) > "$WORK/create_secrets-again.log" 2>&1 || fail "create_secrets.sh failed on its rerun"
[ "$(grep -c '^  kept ' "$WORK/create_secrets-again.log")" = 3 ] || fail "rerunning create_secrets.sh did not keep the three files"
for name in postgres_password app_readonly_password app_writer_password; do
  cmp -s "$SECRETS/$name" "$WORK/secrets-as-created/$name" || fail "rerunning create_secrets.sh changed $name"
done
ok "rerunning create_secrets.sh kept all three files unchanged"
# Process 1's environment, read as the user that process runs as: only that
# user may read it, root in a container included. The database server is
# process 1 of its container; the read service is started by process 1 of
# its own, the init process, with that environment.
bounded 30 compose exec -T -u postgres postgres sh -c 'tr "\0" "\n" < /proc/1/environ' \
    > "$WORK/postgres-environ.txt" 2> "$WORK/postgres-environ.err" \
  && grep -q '^PATH=' "$WORK/postgres-environ.txt" \
  || { mask < "$WORK/postgres-environ.err" >&2; fail "cannot read the database server's environment"; }
bounded 30 compose exec -T api sh -c 'tr "\0" "\n" < /proc/1/environ' \
    > "$WORK/application-environ.txt" 2> "$WORK/application-environ.err" \
  && grep -q '^PATH=' "$WORK/application-environ.txt" \
  || { mask < "$WORK/application-environ.err" >&2; fail "cannot read the read service's environment"; }
! grep -q '^POSTGRES_' "$WORK/postgres-environ.txt" || fail "the database server's environment holds POSTGRES_* settings"
! grep -qE '^(PGPASSWORD|POSTGRES_PASSWORD)=' "$WORK/application-environ.txt" || fail "the read service's environment holds a password"
ok "the environments of the database server and of the read service hold no password setting"
started=$SECONDS
compose stop --timeout 20 api > "$WORK/stop-api.log" 2>&1 || fail "docker compose stop api failed"
stopped_in=$((SECONDS - started))
compose logs --no-color > "$WORK/all.log" 2>&1 || fail "cannot save the containers' logs"
compose logs --no-color api > "$WORK/api-stopped.log" 2>&1 || fail "cannot save the read service's log"
grep -qF "Application shutdown complete" "$WORK/api-stopped.log" || fail "the read service did not shut down cleanly"
ok "docker compose stop ended the read service cleanly in ${stopped_in}s"
ids="$(docker ps -aq --filter "label=$PROJECT_LABEL=$PROJECT")" && [ -n "$ids" ] || fail "cannot list the project's containers"
for id in $ids; do
  docker inspect "$id" >> "$WORK/inspect.json" || fail "cannot inspect the container $id"
done
if leaks_in "$WORK"/*.log "$WORK"/*.json "$WORK"/*.txt "$WORK"/*.err; then
  fail "a password occurs in a log, the configuration, docker inspect output or a container environment"
fi
ok "no password in the logs of the stack's containers or the output of its one-shot commands, the rendered configuration, docker inspect output or a container environment"

if [ "$release" = 1 ]; then
  echo "      release $expect_version verified: the image $image_ref ($IMAGE_ID) and the PostgreSQL image $postgres_ref ($POSTGRES_ID), revision $expect_revision, with the compose.yaml the image carries"
else
  echo "      tested: the image $image_ref ($IMAGE_ID), revision $image_revision, and the PostgreSQL image $postgres_ref ($POSTGRES_ID), revision $postgres_revision, with $compose_source -- a development run, not the verification of a release"
fi
echo "PASS: the Compose deployment started from an empty volume, brought it to the schema of its image, served the demonstration ingested inside it, recovered from a database restart, kept its data across recreation, and kept its secrets out of every output."
