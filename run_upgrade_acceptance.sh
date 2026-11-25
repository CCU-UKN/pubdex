#!/usr/bin/env bash
# Upgrades and rollbacks of the Compose deployment between PubDex releases,
# stage 11 of run_ci_suite.sh, carried out with the commands an operator
# types, in Compose projects that this run creates, owns and removes again.
#
#   ./run_upgrade_acceptance.sh
#
# The releases. release.sh builds three test releases of this checkout, each
# under a version reserved for test releases with a random name of the run's
# own: A, the checkout as it is (--test a<run>); B1, the checkout with the
# test-only overlay b1 -- a backward-compatible schema change and a new
# transform version (--test b1<run> --overlay b1); B2, the checkout with the
# overlay b2, which adds a schema change that is not backward compatible
# (--test b2<run> --overlay b2). The published preceding PubDex release and
# the upgrade floor come from the release record (DB/release_record.py
# preceding and floor): their two images by digest, pulled anonymously when
# Docker lacks them. When the two are the same release, its part runs once.
#
# Each stack is an operator's directory: the deployment files the
# application image in use carries -- compose.yaml, create_secrets.sh and
# set_passwords.sh, taken out of it with the command of DEPLOYMENT.md,
# "Selecting a published image" -- an .env file naming the project, both
# images and a host port of the run's own for the read service, and the
# secret files create_secrets.sh writes. Switching a stack to other images
# is what an operator does: docker compose down, the other application
# image's deployment files, the two images in .env, docker compose up --wait,
# whose schema upgrade brings the database to the schema of the images in
# use. What one run does, stopping at the first failed check: it checks what
# it needs, before it creates anything -- a Git checkout whose VERSION has no
# pre-release part, the tools release.sh needs (the pinned helm and SBOM
# tool), Docker Engine and the Compose plugin of supported versions on
# linux/amd64, buildx, Python 3, and the release record's entries for the
# preceding release and the floor -- and builds A, B1 and B2 with release.sh
# build, each verified. Then come the steps that DB/TESTING_STRATEGY.md,
# "Upgrades between PubDex releases on Compose", numbers, each in the section
# of this script with the same number; DEPLOYMENT.md, "Upgrading to a new
# release", names them as the steps that ran its commands:
#
#   1. A to B1, the backward-compatible case: A on empty volumes, the
#      demonstration ingested and its publication read over HTTP, every row
#      stamped with A's transform version; a backup with the command of
#      DEPLOYMENT.md, "Backup and restore"; the switch to B1, whose schema
#      upgrade applies the one schema change it adds and records it, with
#      the read service ready and the publication still served; the
#      demonstration's payloads ingested again, every row then carrying B1's
#      transform version;
#   2. the rollback to A's images, an image rollback: the schema stays, A's
#      schema upgrade reports the later, backward-compatible change and has
#      nothing to do, A's read service is ready and serves the publication,
#      and A's ingestion writes on that schema;
#   3. A to B2, the other case, on the same stack: B2's schema upgrade applies
#      the change that is not backward compatible and B2 serves the
#      publication;
#   4. switched back to A's images, A refuses the database -- its schema
#      upgrade fails with the refusal in its log, so docker compose up --wait
#      fails and the read service does not start; started alone, the read
#      service answers /readyz and /v0/publications with 503 and the reason
#      schema_not_supported; an ingestion command exits 1 with the message and
#      writes nothing;
#   5. the rollback by restore: docker compose down --volumes, then the
#      commands of "Backup and restore" bring the backup of step 1 into a new
#      stack with A's images, whose schema upgrade finds the dump's record and
#      nothing to do; the verification query counts what it counted before
#      the dump, the rows carry A's transform version again, and the
#      publication is served;
#   6. for the preceding release, and for the floor once it is another
#      release: its published images by digest with the compose.yaml its
#      application image carries, the demonstration ingested and served, its
#      rows stamped with the transform version its record names; a backup;
#      the switch to A, whose schema upgrade applies every schema change of
#      A to that database; the rollback to the published images, an image
#      rollback: the published read service serves the publication on the
#      upgraded schema.
#
# Last, it checks that no password of the run occurs in any output it kept
# or printed, and cleans up as below, confirmed.
#
# Every switch, start and failure is a command of the operator's; the run
# adds only reads: HTTP requests to the read service, the verification
# query, the record of schema changes and the rows' transform versions read
# as the superuser on PostgreSQL's socket, the containers' state and logs.
# The read service and the ingestion command started without the schema
# upgrade (--no-deps) in step 4 show the application's own check, which an
# operator meets only when starting them by hand.
#
# Isolation. Each stack is a Compose project named pubdex-upgrade-<run>-...,
# and the operator's directory has that name too; a project of that name
# that exists already stops the run before anything is created. A
# compose.override.yaml of the run's own in each operator's directory, which
# Compose reads with compose.yaml, puts the run label
# pubdex.upgrade-acceptance.run=<run> on every service, volume and network of
# the compose.yaml in use, and nothing else. PostgreSQL is published on no
# host port, which the run checks after every start, and the read service on
# 127.0.0.1 alone. Every command of the operator's runs in the operator's
# directory with every exported variable removed except PATH, HOME and
# Docker's daemon selection, so the .env file configures it. Every Docker
# command of the run, release.sh's and the pulls included, uses a Docker
# client configuration of the run's own, as run_release_rehearsal.sh
# describes: no registry credential of the caller's is within reach, and the
# published images are pulled anonymously.
#
# Secrets. create_secrets.sh writes each stack's random passwords; they reach
# the containers as Compose file secrets. None is printed or put on a command
# line; output the run prints from Docker or the containers is masked, and
# its own records of that output are checked for every one of them. No
# set -x.
#
# Cleanup. Registered before anything is created, it runs on every exit and
# after SIGINT, SIGTERM or SIGHUP, ignoring further signals while it works:
# docker compose down --volumes in each operator's directory, then the
# removal of every container, network and volume carrying a project's name
# or the run label, every image of the three test releases -- by their
# version labels, which no other run uses -- and every container of them, and
# the run's temporary directory with the operator's directories, the secret
# files and the dumps. It then confirms that none of them is left; if one
# is, or Docker cannot say, it prints the commands that find it and fails --
# with status 1 after an otherwise successful run, and with the earlier
# failure's or the signal's status otherwise. It removes nothing else: the
# published images, base images, the SBOM tool and build cache stay, and
# nothing is pruned. SIGKILL skips it; the run label and the version labels
# find what is left.
#
# Network access: release.sh build pulls the base images by digest and
# fetches the locked files unless Docker has them; the published images are
# pulled by digest unless Docker has them. No metadata provider is
# contacted: the only ingestion is the demonstration's, of bundled payloads.
#
# Needs Bash, a Git checkout, the pinned helm (./setup_cluster_tools.sh), the
# pinned SBOM tool (./release.sh setup), Python 3 (.venv/bin/python, else
# python3), a Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2, for
# linux/amd64 with the buildx plugin and the Compose plugin 2.24.0 or later
# (DB/lib/docker_versions.sh says why), on this machine, and a free TCP port
# on 127.0.0.1 for each stack. Exit status: 0 when every check passed and the
# cleanup was confirmed, 1 for a failed check, build or cleanup, 2 for a
# usage error, 130, 143 or 129 after a signal.
set -euo pipefail

# A shell function the caller exported, or one a BASH_ENV file defined, would
# stand in for docker or any other command below. None of this script's own
# exists yet, so every function defined now goes.
while read -r _declare _flags _function; do
  if [ -n "$_function" ]; then unset -f "$_function"; fi
done <<<"$(declare -F)"

export LC_ALL=C
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PROG="run_upgrade_acceptance.sh"
# A variable of the caller's must not point git at another repository.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
  GIT_COMMON_DIR GIT_NAMESPACE GIT_CEILING_DIRECTORIES
# shellcheck source=DB/lib/disposable_postgres.sh
source "$ROOT/DB/lib/disposable_postgres.sh"  # for clear_exports
# shellcheck source=DB/lib/docker_versions.sh
source "$ROOT/DB/lib/docker_versions.sh"
# shellcheck source=DB/lib/cluster_tools.sh
source "$ROOT/DB/lib/cluster_tools.sh"
# shellcheck source=DB/lib/sbom_tool.sh
source "$ROOT/DB/lib/sbom_tool.sh"
# shellcheck source=DB/lib/compose_acceptance.sh
source "$ROOT/DB/lib/compose_acceptance.sh"  # isolated, bounded, mask, leaks_in, http, wait_until

PROJECT_LABEL="com.docker.compose.project"
RUN_LABEL="pubdex.upgrade-acceptance.run"
VERSION_LABEL="org.opencontainers.image.version"
PLATFORM="linux/amd64"
DEMO_DOI="10.5555/pubdex-fixture-001"
WAIT_SECONDS="${STACK_WAIT_SECONDS:-300}"
DEPLOYMENT_FILES=(compose.yaml create_secrets.sh set_passwords.sh)

usage() {
  cat <<'USAGE'
usage: run_upgrade_acceptance.sh

Upgrades and rolls back the Compose deployment between PubDex releases, with
the commands an operator types: from test release A of this checkout to test
release B1 (a backward-compatible schema change and a new transform version)
and back by an image rollback; from A to B2 (a schema change that is not
backward compatible), where A refuses the database and the rollback is the
restore of the backup taken before the upgrade; and from the published
preceding release, and the upgrade floor, to A and back by an image
rollback. release.sh builds the three test releases; nothing is published.
Every stack runs in a Compose project of the run's own, and everything the
run created is removed at the end, confirmed.
Needs the pinned helm (./setup_cluster_tools.sh), the pinned SBOM tool
(./release.sh setup), Python 3, and Docker Engine 28.0.1 or later, except
28.2.0 to 28.3.2, for linux/amd64, with the buildx plugin and the Compose
plugin 2.24.0 or later.

STACK_WAIT_SECONDS bounds each wait for a stack to become healthy (300).

  -h, --help    show this help
USAGE
}

if [ $# -gt 0 ]; then
  case "$1" in
    -h|--help)
      if [ $# -eq 1 ]; then usage; exit 0; fi ;;
  esac
  echo "$PROG: unexpected argument: $1" >&2
  usage >&2
  exit 2
fi
if ! [[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "$PROG: STACK_WAIT_SECONDS must be a positive number of seconds" >&2
  usage >&2
  exit 2
fi

started=$SECONDS
step() {
  echo
  echo "==> $* ($((SECONDS - started)) s)"
}

ok() { echo "    ok  $*"; }

prerequisite() {
  echo "$PROG: $1" >&2
  shift
  local hint
  for hint in "$@"; do
    echo "  $hint" >&2
  done
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || prerequisite "$1 was not found on PATH"
}

# --- the run's identity, cleanup and private directory ------------------------------

RUN_TAG=""
RUN_DIR=""
CLAIMED=0      # set once the run may own resources
PROJECTS=()    # each stack's project; its operator's directory is $RUN_DIR/<project>
VERSIONS=()    # the three test releases' versions, once drawn
SECRET_VALUES=()
PROJECT=""
OPDIR=""
PORT=""

# The ids of the run's containers, volumes or networks: those with the run
# label or one of the projects' names, one per line; fails when Docker cannot say.
owned() {  # owned <ps|volume|network>
  local filters=("label=$RUN_LABEL=$RUN_TAG") filter project
  for project in ${PROJECTS[@]+"${PROJECTS[@]}"}; do
    filters+=("label=$PROJECT_LABEL=$project")
  done
  for filter in "${filters[@]}"; do
    case "$1" in
      ps) docker ps -aq --no-trunc --filter "$filter" ;;
      volume) docker volume ls -q --filter "$filter" ;;
      network) docker network ls -q --no-trunc --filter "$filter" ;;
    esac || return 1
  done
}

# The ids of the test releases' images, dangling ones included, and of every
# container of them -- a probe of release.sh, a container that took out the
# deployment files -- or of the SBOM tool that mounts the run's directory.
owned_images() {
  local version
  for version in ${VERSIONS[@]+"${VERSIONS[@]}"}; do
    docker image ls -aq --no-trunc --filter "label=$VERSION_LABEL=$version" || return 1
  done
}
owned_image_containers() {
  local version ids id sources
  for version in ${VERSIONS[@]+"${VERSIONS[@]}"}; do
    docker ps -aq --no-trunc --filter "label=$VERSION_LABEL=$version" || return 1
  done
  ids="$(docker ps -aq --no-trunc --filter "ancestor=$PINNED_SYFT_IMAGE")" || return 1
  for id in $ids; do
    sources="$(docker inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "$id" 2>/dev/null)" || continue
    if grep -qF -- "$RUN_DIR/" <<<"$sources"; then
      echo "$id"
    fi
  done
}

remove_owned() {
  local project id version
  for project in ${PROJECTS[@]+"${PROJECTS[@]}"}; do
    if [ -f "$RUN_DIR/$project/.env" ] && [ -f "$RUN_DIR/$project/compose.yaml" ]; then
      ( cd "$RUN_DIR/$project" && isolated docker compose --profile ingest --profile demo down --volumes \
          --remove-orphans --timeout 5 ) >/dev/null 2>&1 || true
    fi
  done
  for id in $( { owned ps; owned_image_containers; } 2>/dev/null | sort -u); do
    docker rm -f -v "$id" >/dev/null 2>&1 || true
  done
  for id in $(owned volume 2>/dev/null | sort -u); do
    docker volume rm "$id" >/dev/null 2>&1 || true
  done
  for id in $(owned network 2>/dev/null | sort -u); do
    docker network rm "$id" >/dev/null 2>&1 || true
  done
  for version in ${VERSIONS[@]+"${VERSIONS[@]}"}; do
    docker image rm "pubdex-release:$version" "pubdex-postgres-release:$version" >/dev/null 2>&1 || true
  done
  for id in $(owned_images 2>/dev/null | sort -u); do
    docker image rm -f "$id" >/dev/null 2>&1 || true
  done
}

confirm_removed() {  # nothing of the run left in Docker, or a warning with the commands that find it
  local containers volumes networks images image_containers project version
  if ! containers="$(owned ps 2>/dev/null)" || ! volumes="$(owned volume 2>/dev/null)" \
     || ! networks="$(owned network 2>/dev/null)" || ! images="$(owned_images 2>/dev/null)" \
     || ! image_containers="$(owned_image_containers 2>/dev/null)" \
     || [ -n "$containers$volumes$networks$images$image_containers" ]; then
    echo "WARNING: could not confirm that run $RUN_TAG left nothing behind. Check with" >&2
    echo "  docker ps -a --filter label=$RUN_LABEL=$RUN_TAG" >&2
    echo "  docker volume ls --filter label=$RUN_LABEL=$RUN_TAG" >&2
    echo "  docker network ls --filter label=$RUN_LABEL=$RUN_TAG" >&2
    for project in ${PROJECTS[@]+"${PROJECTS[@]}"}; do
      echo "  docker ps -a --filter label=$PROJECT_LABEL=$project    (and docker volume ls, docker network ls)" >&2
    done
    for version in ${VERSIONS[@]+"${VERSIONS[@]}"}; do
      echo "  docker image ls -a --filter label=$VERSION_LABEL=$version" >&2
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
    if confirm_removed; then
      echo "    ok  no container, volume, network or image of run $RUN_TAG is left"
    else
      [ "$rc" -ne 0 ] || rc=1  # an earlier failure keeps its own status
    fi
  fi
  if [ -n "$RUN_DIR" ]; then
    rm -rf -- "$RUN_DIR" 2>/dev/null || true
    if [ -e "$RUN_DIR" ]; then
      echo "WARNING: could not remove the run's temporary directory $RUN_DIR, which holds secret files and dumps." >&2
      [ "$rc" -ne 0 ] || rc=1
    elif [ "$CLAIMED" = 1 ]; then
      echo "    ok  the run's temporary directory, with the operator's directories, secret files and dumps, is gone"
    fi
  fi
  if [ "$rc" -eq 0 ]; then
    echo
    echo "PASS: upgrades and rollbacks between PubDex releases on Compose, as an operator runs them, in $((SECONDS - started)) s."
    echo "      $DURATIONS"
  fi
  exit "$rc"
}

DURATIONS=""
exec 3>&1 4>&2  # the run's own output, for the cleanup
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

diagnose() {  # the current stack's state and log tails, masked
  [ -n "$OPDIR" ] && [ -f "$OPDIR/.env" ] && [ -f "$OPDIR/compose.yaml" ] || return 0
  echo "--- diagnostics for project $PROJECT" >&2
  dc ps -a 2>&1 | mask >&2 || true
  dc logs --no-color --tail 30 2>&1 | mask >&2 || true
}

fail() {
  echo "FAIL: $*" >&2
  diagnose
  exit 1
}

# --- what the run needs ---------------------------------------------------------------

step "Checking what the run needs"
for tool in git tar gzip sha256sum cmp python3 od docker; do
  need "$tool"
done
if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  prerequisite "this directory is not a Git checkout; release.sh reads the checkout's files and commit with git"
fi
[ -r "$ROOT/VERSION" ] || prerequisite "VERSION is missing"
file_version="$(head -n 1 "$ROOT/VERSION" | tr -d '[:space:]')"
if ! [[ "$file_version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  prerequisite "VERSION holds '$file_version'; a test release's version is <VERSION>-test.<name> for a VERSION without a pre-release part"
fi
if [ ! -x "$(cluster_tool_path "$ROOT" helm)" ]; then
  prerequisite "the pinned helm $PINNED_HELM_VERSION is not installed under .tools/; release.sh packages the chart with it. Install it with:" \
    "./setup_cluster_tools.sh"
fi
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 2>/dev/null)" || prerequisite "neither .venv/bin/python nor python3 was found; the HTTP checks need Python 3."
fi

# The daemon the caller's Docker client uses, reached from now on through a
# client configuration of the run's own, which holds no credential.
if [ -z "${DOCKER_HOST:-}" ]; then
  context="$(docker context inspect --format '{{.Name}}{{"\t"}}{{.Endpoints.docker.Host}}{{"\t"}}{{len .TLSMaterial}}' 2>/dev/null)" \
    || context=""
  IFS=$'\t' read -r context_name endpoint tls_files <<<"$context" || true
  if [ -z "${endpoint:-}" ]; then
    prerequisite "cannot read the Docker daemon's address from the current Docker context ('docker context inspect' fails)"
  fi
  if [ "${tls_files:-0}" != 0 ]; then
    prerequisite "the Docker context $context_name uses TLS files, which the run's own client configuration cannot carry." \
      "Select its daemon with DOCKER_HOST, DOCKER_TLS_VERIFY and DOCKER_CERT_PATH instead."
  fi
  export DOCKER_HOST="$endpoint"
fi
caller_plugins="${DOCKER_CONFIG:-${HOME:-/nonexistent}/.docker}/cli-plugins"

RUN_TAG="$(od -An -N5 -tx1 /dev/urandom | tr -d ' \n')"
[[ "$RUN_TAG" =~ ^[0-9a-f]{10}$ ]] || prerequisite "could not draw the run's random name"
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pubdex-upgrade.XXXXXX")"
RUN_DIR="$(cd "$RUN_DIR" && pwd -P)"
chmod 700 "$RUN_DIR"
mkdir -p "$RUN_DIR/tmp" "$RUN_DIR/logs"
mkdir -m 700 "$RUN_DIR/docker"
# The plugins of the caller's configuration directory -- Compose or buildx
# installed for one user, under ~/.docker/cli-plugins -- stay within reach;
# nothing else of that directory is read.
if [ -d "$caller_plugins" ]; then
  python3 -I -c 'import json, sys; json.dump({"cliPluginsExtraDirs": [sys.argv[1]]}, open(sys.argv[2], "w"))' \
    "$caller_plugins" "$RUN_DIR/docker/config.json" || prerequisite "could not write the run's Docker client configuration"
fi
unset DOCKER_CONTEXT DOCKER_AUTH_CONFIG
export DOCKER_CONFIG="$RUN_DIR/docker"
LOGS="$RUN_DIR/logs"

if ! docker info >/dev/null 2>&1; then
  prerequisite "cannot reach the Docker daemon. Start it, or check that this user may use it ('docker info' shows the reason)."
fi
engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null)" || engine=""
problem="$(engine_problem "$engine")"
if [ -n "$problem" ]; then
  prerequisite "$problem: the run builds and starts the deployment, which needs Docker Engine $ENGINE_SUPPORTED" \
    "(DB/lib/docker_versions.sh says why). Upgrade the engine."
fi
platform="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null)" || platform=""
[ "$platform" = "$PLATFORM" ] || prerequisite "the Docker daemon runs on '$platform'; the releases' images are for $PLATFORM"
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
docker buildx version >/dev/null 2>&1 \
  || prerequisite "the Docker buildx plugin was not found ('docker buildx version' fails); build_image.sh needs it"
sbom_id="$(docker image inspect --format '{{.Id}}' "$PINNED_SYFT_IMAGE" 2>/dev/null)" || sbom_id=""
if [ -z "$sbom_id" ]; then
  prerequisite "the pinned SBOM tool, Syft $PINNED_SYFT_VERSION, is not on this machine; release.sh builds the test releases with it. Pull it, by digest, with:" \
    "./release.sh setup"
fi
ok "Docker Engine $engine on $platform, Compose $compose_version, the buildx plugin, the pinned SBOM tool"

# The published releases an upgrade to this checkout starts from: the
# preceding release, and the floor once it is another release.
record() { python3 -I "$ROOT/DB/release_record.py" "$@"; }
preceding="$(record preceding 2> "$LOGS/record.err")" || prerequisite "the release record names no preceding release: $(head -n 1 "$LOGS/record.err")"
floor="$(record floor 2> "$LOGS/record.err")" || prerequisite "the release record names no floor: $(head -n 1 "$LOGS/record.err")"
PUBLISHED=("$preceding")
[ "$floor" = "$preceding" ] || PUBLISHED+=("$floor")
declare -A PUBLISHED_APP PUBLISHED_POSTGRES PUBLISHED_TRANSFORM PUBLISHED_SCHEMA
digest_ref='^[a-z0-9.:/_-]+:[0-9A-Za-z._-]+@sha256:[0-9a-f]{64}$'
for version in "${PUBLISHED[@]}"; do
  facts="$(record field "$version" images.application images.postgres transform_version schema_changes \
             2> "$LOGS/record.err")" \
    || prerequisite "the release record's entry of $version cannot be read: $(head -n 1 "$LOGS/record.err")"
  { read -r app_ref; read -r postgres_ref; read -r transform; read -r schema_changes; } <<<"$facts"
  [[ "$app_ref" =~ $digest_ref ]] && [[ "$postgres_ref" =~ $digest_ref ]] \
    || prerequisite "the release record names the images of $version without their digests: $app_ref, $postgres_ref"
  PUBLISHED_APP[$version]="$app_ref"
  PUBLISHED_POSTGRES[$version]="$postgres_ref"
  PUBLISHED_TRANSFORM[$version]="$transform"
  PUBLISHED_SCHEMA[$version]="$schema_changes"
done
ok "the preceding release $preceding and the floor $floor, from the release record; $( [ "$floor" = "$preceding" ] && echo "the same release, so its part runs once" || echo "two parts")"

CLAIMED=1
for name in a b1 b2; do
  VERSIONS+=("$file_version-test.$name$RUN_TAG")
done
A_VERSION="${VERSIONS[0]}"
B1_VERSION="${VERSIONS[1]}"
B2_VERSION="${VERSIONS[2]}"
ok "run $RUN_TAG: test releases A $A_VERSION, B1 $B1_VERSION, B2 $B2_VERSION, in $RUN_DIR"
save_caller_environment

# --- the operator's commands --------------------------------------------------------

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

# new_stack <project>: a new operator's directory of that name, for a
# project that does not exist yet, with a free port for its read service.
new_stack() {
  local existing
  PROJECT="$1"
  [ "$PROJECT" != db ] || fail "the project name db is never used"
  existing="$(docker ps -aq --filter "label=$PROJECT_LABEL=$PROJECT" && docker volume ls -q --filter "label=$PROJECT_LABEL=$PROJECT" \
    && docker network ls -q --filter "label=$PROJECT_LABEL=$PROJECT")" || fail "Docker cannot list the resources of project $PROJECT"
  [ -z "$existing" ] || fail "resources of project $PROJECT exist already; not touching them"
  PROJECTS+=("$PROJECT")
  OPDIR="$RUN_DIR/$PROJECT"
  mkdir "$OPDIR"
  PORT="$(isolated "$PY" -I -c '
import socket
with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    print(probe.getsockname()[1])')" || fail "cannot find a free port on 127.0.0.1"
}

# use_images <application image> <PostgreSQL image>: the images, as the
# operator names them in .env, and the deployment files the application image
# carries, taken out of it as DEPLOYMENT.md, "Selecting a published image",
# does; then the run label for its services, volumes and networks.
use_images() {
  local app="$1" postgres="$2" file services volumes name
  printf 'COMPOSE_PROJECT_NAME=%s\nPEOPLE_PUBS_IMAGE=%s\nPEOPLE_PUBS_POSTGRES_IMAGE=%s\nPEOPLE_PUBS_HTTP_PORT=%s\n' \
    "$PROJECT" "$app" "$postgres" "$PORT" > "$OPDIR/.env"
  rm -f -- "$OPDIR/compose.override.yaml" "${DEPLOYMENT_FILES[@]/#/$OPDIR/}"
  # shellcheck disable=SC2016
  ( cd "$OPDIR" && bounded 300 isolated bash -c \
      'set -o pipefail; docker run --rm "$1" tar -cC /opt/pubdex compose.yaml create_secrets.sh set_passwords.sh | tar -x' \
      bash "$app" ) > "$LOGS/files.log" 2>&1 \
    || { mask < "$LOGS/files.log" | tail -n 10 >&2; fail "the deployment files could not be taken out of $app"; }
  for file in "${DEPLOYMENT_FILES[@]}"; do
    [ -s "$OPDIR/$file" ] || fail "$app carries no /opt/pubdex/$file"
  done
  services="$(dc --profile ingest --profile demo config --services 2> "$LOGS/config.err")" \
    && volumes="$(dc config --volumes 2>> "$LOGS/config.err")" \
    || { mask < "$LOGS/config.err" >&2; fail "docker compose config cannot read the compose.yaml of $app"; }
  {
    echo "# The run label of run_upgrade_acceptance.sh, on every service, volume and network."
    echo "services:"
    for name in $services; do printf '  %s: {labels: {%s: "%s"}}\n' "$name" "$RUN_LABEL" "$RUN_TAG"; done
    echo "volumes:"
    for name in $volumes; do printf '  %s: {labels: {%s: "%s"}}\n' "$name" "$RUN_LABEL" "$RUN_TAG"; done
    echo "networks:"
    printf '  default: {labels: {%s: "%s"}}\n' "$RUN_LABEL" "$RUN_TAG"
  } > "$OPDIR/compose.override.yaml"
}

create_secrets() {  # the guide's bash create_secrets.sh; the passwords are masked from then on
  local name value
  ( cd "$OPDIR" && isolated bash create_secrets.sh ) > "$LOGS/create_secrets-$PROJECT.log" 2>&1 \
    || { cat "$LOGS/create_secrets-$PROJECT.log" >&2; fail "create_secrets.sh failed"; }
  for name in postgres_password app_readonly_password app_writer_password; do
    IFS= read -r value < "$OPDIR/secrets/$name" || fail "create_secrets.sh wrote no secrets/$name"
    [ -n "$value" ] || fail "secrets/$name is empty"
    SECRET_VALUES+=("$value")
  done
}

up() {  # up <log name>: docker compose up --wait, which has to succeed; then the bindings
  local api postgres
  dc up --wait --wait-timeout "$WAIT_SECONDS" > "$LOGS/$1" 2>&1 \
    || { mask < "$LOGS/$1" | tail -n 20 >&2; fail "docker compose up --wait did not see a healthy stack"; }
  api="$(dc ps -q api)" && postgres="$(dc ps -q postgres)" && [ -n "$api" ] && [ -n "$postgres" ] \
    || fail "cannot find the read service's and PostgreSQL's containers"
  [ "$(docker port "$api" | sort -u)" = "8000/tcp -> 127.0.0.1:$PORT" ] \
    || fail "the read service is not published on 127.0.0.1:$PORT alone: $(docker port "$api" | tr '\n' ' ')"
  [ -z "$(docker port "$postgres")" ] || fail "PostgreSQL is published on a host port"
}

down() {  # down <log name> [--volumes]: docker compose down
  local log="$1"
  shift
  bounded 120 dc down "$@" > "$LOGS/$log" 2>&1 \
    || { mask < "$LOGS/$log" | tail -n 20 >&2; fail "docker compose down $* failed"; }
}

# switch_to <application image> <PostgreSQL image> <log name>: the operator's
# switch of a running stack to other images, which has to come up healthy.
switch_to() {
  down "down-$3"
  use_images "$1" "$2"
  up "up-$3"
}

# The backup of DEPLOYMENT.md, "Backup and restore", in the operator's
# directory: people_db.dump, mode 0600.
backup() {
  ( cd "$OPDIR" && umask 077 && bounded 300 isolated docker compose exec -T postgres pg_dump -U postgres -Fc people_db \
      > people_db.dump ) 2> "$LOGS/dump.err" \
    || { mask < "$LOGS/dump.err" >&2; fail "pg_dump failed"; }
  [ -s "$OPDIR/people_db.dump" ] && [ "$(ls -l "$OPDIR/people_db.dump" | cut -c1-10)" = "-rw-------" ] \
    || fail "the dump is empty, or readable by other users"
}

# The guide's restore of people_db.dump into the stack's new, empty volume.
restore() {
  dc up --wait --wait-timeout "$WAIT_SECONDS" postgres > "$LOGS/up-restore.log" 2>&1 \
    || { mask < "$LOGS/up-restore.log" | tail -n 20 >&2; fail "docker compose up --wait postgres did not see a healthy database"; }
  [ -z "$(dc ps -q api 2>/dev/null)" ] || fail "docker compose up --wait postgres started the read service"
  bounded 60 dc stop api > "$LOGS/stop-api.log" 2>&1 || { mask < "$LOGS/stop-api.log" >&2; fail "docker compose stop api failed"; }
  bounded 60 dc exec -T postgres dropdb -U postgres --force people_db > "$LOGS/dropdb.log" 2>&1 \
    || { mask < "$LOGS/dropdb.log" >&2; fail "dropdb failed"; }
  bounded 60 dc exec -T postgres createdb -U postgres people_db > "$LOGS/createdb.log" 2>&1 \
    || { mask < "$LOGS/createdb.log" >&2; fail "createdb failed"; }
  ( cd "$OPDIR" && bounded_input 300 isolated docker compose exec -T postgres pg_restore -U postgres -d people_db \
      --single-transaction --exit-on-error < people_db.dump ) > "$LOGS/restore.log" 2>&1 \
    || { mask < "$LOGS/restore.log" | tail -n 20 >&2; fail "pg_restore failed"; }
  up up-restored.log
}

# --- the run's reads ----------------------------------------------------------------

sql() {  # sql <query>: as the superuser on PostgreSQL's socket, unaligned
  bounded 60 dc exec -T postgres psql -U postgres -d people_db -XAtc "$1"
}
schema_record() {  # number|name|backward compatible|release, one change per line
  sql "SELECT number || '|' || name || '|' || backward_compatible || '|' || release FROM pubdex.schema_changes ORDER BY number"
}
has_record() { sql "SELECT to_regclass('pubdex.schema_changes') IS NOT NULL"; }
# The transform versions the rows' source provenance carries, one a line.
stamps() {
  sql "SELECT DISTINCT e.value->>'transform_version' FROM biblio.publications p CROSS JOIN LATERAL jsonb_each(p.source_provenance) e ORDER BY 1"
}
# One hash of every row's source provenance: what an ingestion rewrites.
provenance_hash() {
  sql "SELECT md5(coalesce(string_agg(source_provenance::text, ',' ORDER BY pub_id), '')) FROM biblio.publications"
}
counts() {
  bounded 60 dc exec -T postgres psql -U postgres -d people_db -XAtc 'SELECT (SELECT count(*) FROM app.people), (SELECT count(*) FROM biblio.publications), (SELECT count(*) FROM biblio.publications_canon_web)'
}

read_into() {  # read_into <variable> <what> <reader> [arguments]: the reader's output, or a failure
  local variable="$1" what="$2" value
  shift 2
  value="$("$@" 2> "$LOGS/read.err")" || { mask < "$LOGS/read.err" >&2; fail "cannot read $what"; }
  printf -v "$variable" '%s' "$value"
}

schema_log() {  # schema_log <log name>: the schema upgrade's log of the current stack
  dc logs --no-color schema-upgrade > "$LOGS/$1" 2>&1 || fail "cannot read the schema upgrade's log"
}
logged() { grep -qF -- "$2" "$LOGS/$1"; }  # logged <log name> <text>

ready() { [ "$(http /readyz "$LOGS/readyz.json")" = 200 ]; }

# served <file name>: the read service is ready and serves the
# demonstration's publication, once.
SERVED_CHECK='
import json, sys
page = json.load(open(sys.argv[1], encoding="utf-8"))
items = [item for item in page["items"] if item["doi"] == sys.argv[2]]
assert len(items) == 1, page
assert items[0]["title"] == "Synthetic Collective Behaviour Paper", items[0]
assert items[0]["year"] == 2024, items[0]
'
served() {
  wait_until 60 ready || fail "GET /readyz did not answer 200: $(cat "$LOGS/readyz.json")"
  [ "$(http '/v0/publications?limit=100' "$LOGS/$1")" = 200 ] || fail "GET /v0/publications did not answer 200"
  isolated "$PY" -I -c "$SERVED_CHECK" "$LOGS/$1" "$DEMO_DOI" 2> "$LOGS/served.err" \
    || { cat "$LOGS/served.err" >&2; fail "the read service does not serve the demonstration's publication $DEMO_DOI"; }
}

stamped() {  # stamped <transform version>: every row carries it
  local found
  read_into found "the rows' transform versions" stamps
  [ "$found" = "people_pubs/$1" ] || fail "the rows carry the transform versions '$(tr '\n' ' ' <<<"$found")', not people_pubs/$1 alone"
}

ingest_again() {  # ingest_again <log name>: the demonstration's payloads again, then the documented refresh
  bounded 300 dc run --rm -T ingest python -m people_pubs.sync.fixture_demo > "$LOGS/$1" 2>&1 \
    || { mask < "$LOGS/$1" | tail -n 20 >&2; fail "the ingestion of the demonstration's payloads failed"; }
  grep -q "fixture stage crossref: processed=1" "$LOGS/$1" || fail "the demonstration's Crossref payload was not processed again"
  bounded 120 dc run --rm -T ingest python -m people_pubs.sync.attribution_rules recompute --refresh-only \
      > "$LOGS/refresh-$1" 2>&1 || { mask < "$LOGS/refresh-$1" | tail -n 20 >&2; fail "the documented refresh failed"; }
}

transform_of() {  # transform_of <image>: its label pubdex.transform-version
  docker image inspect --format '{{index .Config.Labels "pubdex.transform-version"}}' "$1"
}

part_started=$SECONDS
part_done() {  # part_done <name>: records the part's duration
  DURATIONS+="${DURATIONS:+, }$1 $((SECONDS - part_started)) s"
  part_started=$SECONDS
}

# --- the test releases ----------------------------------------------------------------

release_build() {  # release_build <name> [--overlay b1|b2]: sets APP and POSTGRES from IMAGES
  local name="$1" out="$RUN_DIR/release-$1" began=$SECONDS
  shift
  if ! TMPDIR="$RUN_DIR/tmp" "$ROOT/release.sh" build --test "$name" "$@" --out "$out" > "$LOGS/release-$name.log" 2>&1; then
    tail -n 40 "$LOGS/release-$name.log" >&2
    fail "release.sh build --test $name${*:+ $*} failed"
  fi
  APP="$(sed -n 's/^application \([^ ]*\) .*$/\1/p' "$out/IMAGES")"
  POSTGRES="$(sed -n 's/^postgres \([^ ]*\) .*$/\1/p' "$out/IMAGES")"
  [ -n "$APP" ] && [ -n "$POSTGRES" ] || fail "release.sh wrote no IMAGES for $name"
  ok "release.sh build --test $name${*:+ $*}: $APP and $POSTGRES, built and verified ($((SECONDS - began)) s)"
}

part_started=$SECONDS
step "Building test releases A, B1 and B2 with release.sh"
release_build "a$RUN_TAG"
A_APP="$APP" A_POSTGRES="$POSTGRES"
release_build "b1$RUN_TAG" --overlay b1
B1_APP="$APP" B1_POSTGRES="$POSTGRES"
release_build "b2$RUN_TAG" --overlay b2
B2_APP="$APP" B2_POSTGRES="$POSTGRES"
A_TRANSFORM="$(transform_of "$A_APP")" && B1_TRANSFORM="$(transform_of "$B1_APP")" && B2_TRANSFORM="$(transform_of "$B2_APP")" \
  || fail "cannot read the test releases' transform versions"
[ -n "$A_TRANSFORM" ] && [ "$B1_TRANSFORM" != "$A_TRANSFORM" ] && [ "$B2_TRANSFORM" = "$B1_TRANSFORM" ] \
  || fail "B1 and B2 have to carry one new transform version, not A's: A $A_TRANSFORM, B1 $B1_TRANSFORM, B2 $B2_TRANSFORM"
ok "transform versions: A $A_TRANSFORM, B1 and B2 $B1_TRANSFORM"
part_done "builds"

# --- 1. A to B1, the backward-compatible case -----------------------------------------

step "A on empty volumes, with the demonstration"
new_stack "pubdex-upgrade-$RUN_TAG-test"
use_images "$A_APP" "$A_POSTGRES"
create_secrets
up up-a.log
schema_log schema-a.log
logged schema-a.log "schema: the database holds the baseline and has had no schema change" \
  && logged schema-a.log "schema: done: applied" || fail "A's schema upgrade did not apply its schema changes to the new database"
read_into RECORD_A "the record of schema changes" schema_record
bounded 300 dc run --rm demo > "$LOGS/demo-a.log" 2>&1 || { mask < "$LOGS/demo-a.log" | tail -n 20 >&2; fail "docker compose run --rm demo failed"; }
served served-a.json
stamped "$A_TRANSFORM"
ok "docker compose up --wait, docker compose run --rm demo: A serves $DEMO_DOI, its rows stamped people_pubs/$A_TRANSFORM; record: $(tr '\n' ' ' <<<"$RECORD_A")"
read_into COUNTS_A "the verification query" counts
backup
ok "the backup of DEPLOYMENT.md, people_db.dump, mode 0600; the verification query counts $COUNTS_A"

step "Upgrade from A to B1: docker compose down, B1's deployment files and images, docker compose up --wait"
switch_to "$B1_APP" "$B1_POSTGRES" b1
schema_log schema-b1.log
last_a="$(tail -n 1 <<<"$RECORD_A" | cut -d '|' -f 1)"
compatible="$(printf '%04d' $((last_a + 1)))_test_overlay_compatible"
logged schema-b1.log "schema: applying $compatible (backward compatible)" && logged schema-b1.log "schema: done: applied 1 schema change;" \
  || { mask < "$LOGS/schema-b1.log" | tail -n 10 >&2; fail "B1's schema upgrade did not apply $compatible alone"; }
read_into RECORD_B1 "the record of schema changes" schema_record
[ "$RECORD_B1" = "$RECORD_A"$'\n'"$((last_a + 1))|$compatible|true|$B1_VERSION" ] \
  || fail "after the upgrade to B1 the record is: $(tr '\n' ' ' <<<"$RECORD_B1")"
served served-b1.json
stamped "$A_TRANSFORM"
ok "B1's schema upgrade applied $compatible, recorded as backward compatible by $B1_VERSION; B1 is ready and serves $DEMO_DOI"
ingest_again ingest-b1.log
stamped "$B1_TRANSFORM"
served served-b1-ingested.json
ok "the demonstration's payloads ingested again: every row carries B1's transform version people_pubs/$B1_TRANSFORM"

# --- 2. the image rollback to A -------------------------------------------------------

step "Image rollback from B1 to A: docker compose down, A's deployment files and images, docker compose up --wait"
switch_to "$A_APP" "$A_POSTGRES" a-rollback
schema_log schema-a-rollback.log
logged schema-a-rollback.log "schema: the database also has 1 schema change of a later PubDex release, all backward compatible: $compatible" \
  && logged schema-a-rollback.log "schema: nothing to do: the database has every schema change of PubDex $A_VERSION" \
  || { mask < "$LOGS/schema-a-rollback.log" | tail -n 10 >&2; fail "A's schema upgrade did not accept B1's schema as it stands"; }
[ "$(schema_record 2>/dev/null)" = "$RECORD_B1" ] || fail "the image rollback changed the record of schema changes"
served served-a-rollback.json
ok "the schema stays: A's schema upgrade reports B1's backward-compatible change and has nothing to do; A is ready and serves $DEMO_DOI"
ingest_again ingest-a-rollback.log
stamped "$A_TRANSFORM"
served served-a-rollback-ingested.json
ok "A's ingestion writes on B1's schema: every row carries A's transform version people_pubs/$A_TRANSFORM again"
part_done "A to B1 and the image rollback"

# --- 3. A to B2, the case that is not backward compatible -----------------------------

step "Upgrade from A to B2: docker compose down, B2's deployment files and images, docker compose up --wait"
switch_to "$B2_APP" "$B2_POSTGRES" b2
schema_log schema-b2.log
incompatible="$(printf '%04d' $((last_a + 2)))_test_overlay_incompatible"
logged schema-b2.log "schema: applying $incompatible (not backward compatible)" && logged schema-b2.log "schema: done: applied 1 schema change;" \
  && ! logged schema-b2.log "schema: applying $compatible" \
  || { mask < "$LOGS/schema-b2.log" | tail -n 10 >&2; fail "B2's schema upgrade did not apply $incompatible alone"; }
read_into RECORD_B2 "the record of schema changes" schema_record
[ "$RECORD_B2" = "$RECORD_B1"$'\n'"$((last_a + 2))|$incompatible|false|$B2_VERSION" ] \
  || fail "after the upgrade to B2 the record is: $(tr '\n' ' ' <<<"$RECORD_B2")"
served served-b2.json
ok "B2's schema upgrade applied $incompatible, recorded as not backward compatible by $B2_VERSION; B2 is ready and serves $DEMO_DOI"
ingest_again ingest-b2.log
stamped "$B2_TRANSFORM"
served served-b2-ingested.json
# Change a meaningful value after the backup; restoration must recover it.
sql "UPDATE biblio.publications SET title = 'Changed after backup', year = 2025 WHERE doi = '$DEMO_DOI'; SELECT biblio.refresh_serving_snapshots()" \
  > "$LOGS/changed-values.log" || fail "could not change the post-backup values"
[ "$(sql "SELECT test_overlay_title || '|' || year FROM biblio.publications_canon_web WHERE doi = '$DEMO_DOI'")" = 'Changed after backup|2025' ] \
  || fail "B2's snapshot did not retain the changed values"

# --- 4. A refuses B2's schema ---------------------------------------------------------

step "Back to A's images: A refuses the database"
down down-a-refused.log
use_images "$A_APP" "$A_POSTGRES"
if dc up --wait --wait-timeout "$WAIT_SECONDS" > "$LOGS/up-a-refused.log" 2>&1; then
  fail "docker compose up --wait succeeded with A's images on B2's schema"
fi
grep -qF 'service "schema-upgrade" didn'"'"'t complete successfully: exit 1' "$LOGS/up-a-refused.log" \
  || { mask < "$LOGS/up-a-refused.log" | tail -n 10 >&2; fail "docker compose up --wait did not fail at the schema upgrade"; }
states="$(dc ps --all --format '{{.Service}} {{.State}} {{.ExitCode}}' 2> "$LOGS/ps.err" | sort)" \
  || { mask < "$LOGS/ps.err" >&2; fail "docker compose ps --all failed"; }
grep -qx 'schema-upgrade exited 1' <<<"$states" && ! grep -q '^api running' <<<"$states" \
  || fail "after the refusal: $(tr '\n' ';' <<<"$states")"
schema_log schema-a-refused.log
refusal="schema: refused: the database has schema changes of a later PubDex release that are not backward compatible: $incompatible."
logged schema-a-refused.log "$refusal" || { mask < "$LOGS/schema-a-refused.log" | tail -n 10 >&2; fail "A's schema upgrade did not refuse B2's schema"; }
ok "docker compose up --wait fails: A's schema upgrade exits 1 with \"${refusal#schema: }\", and the read service is not started"
# Run A's actual query directly, bypassing only its schema guard, to establish
# the broken dependency independently of the compatibility declaration.
bounded 120 dc run --rm --no-deps -T api python -c '
import os, psycopg
from people_pubs.api.settings import load_settings
from people_pubs.api.database import CHECK_READ_SURFACE, connection_kwargs
settings = load_settings(os.environ)
with psycopg.connect(settings.dsn, **connection_kwargs(settings)) as conn:
    try:
        conn.execute(CHECK_READ_SURFACE)
    except psycopg.errors.UndefinedColumn as error:
        assert error.sqlstate == "42703" and "title" in str(error), error
        print("A query fails on the renamed title column: SQLSTATE 42703")
    else:
        raise AssertionError("A query unexpectedly accepts B2 schema")
' > "$LOGS/structural-refusal.log" 2>&1 \
  || { mask < "$LOGS/structural-refusal.log" >&2; fail "A's actual query did not demonstrate the broken column dependency"; }
ok "A's actual readiness query fails with UndefinedColumn (42703), independently of the schema guard"
bounded 120 dc up --detach --no-deps api > "$LOGS/up-api-alone.log" 2>&1 \
  || { mask < "$LOGS/up-api-alone.log" | tail -n 10 >&2; fail "docker compose up --detach --no-deps api failed"; }
answered() { [ "$(http /readyz "$LOGS/readyz-refused.json")" != 000 ]; }
wait_until 60 answered || fail "the read service started alone did not answer /readyz"
[ "$(http /readyz "$LOGS/readyz-refused.json")" = 503 ] && [ "$(http /livez "$LOGS/livez-refused.json")" = 200 ] \
  && [ "$(http /v0/publications "$LOGS/publications-refused.json")" = 503 ] \
  || fail "A's read service on B2's schema: /readyz, /livez or /v0/publications did not answer 503, 200 and 503"
isolated "$PY" -I - "$LOGS/readyz-refused.json" "$LOGS/publications-refused.json" "$incompatible" <<'PY' \
  || fail "A's read service does not explain its refusal as documented: $(cat "$LOGS/readyz-refused.json")"
import json, sys
ready, publications, change = (json.load(open(sys.argv[1], encoding="utf-8")),
                                json.load(open(sys.argv[2], encoding="utf-8")), sys.argv[3])
assert ready["status"] == "not_ready" and ready["reason"] == "schema_not_supported", ready
assert publications["error"]["code"] == "schema_not_supported", publications
for message in (ready["message"], publications["error"]["message"]):
    assert f"not backward compatible: {change}" in message, message
    assert "python -m people_pubs.schema upgrade" in message, message
PY
ok "A's read service, started alone: /readyz and /v0/publications answer 503 schema_not_supported naming $incompatible, /livez 200"
read_into before_hash "the rows' source provenance" provenance_hash
status=0
bounded 120 dc run --rm --no-deps -T ingest python -m people_pubs.sync.fixture_demo > "$LOGS/ingest-refused.out" \
  2> "$LOGS/ingest-refused.err" || status=$?
[ "$status" = 1 ] && grep -q "^people_pubs: database schema version $(printf '%04d' $((last_a + 2)));" "$LOGS/ingest-refused.err" \
  && grep -qF "not backward compatible: $incompatible" "$LOGS/ingest-refused.err" && ! grep -q Traceback "$LOGS/ingest-refused.err" \
  || { mask < "$LOGS/ingest-refused.err" | tail -n 5 >&2; fail "A's ingestion did not stop with exit status 1 and the message (status $status)"; }
[ "$(provenance_hash 2>/dev/null)" = "$before_hash" ] || fail "A's refused ingestion wrote rows"
ok "A's ingestion command exits 1 with the message, before writing anything"

# --- 5. the rollback by restore -------------------------------------------------------

step "Rollback by restore: docker compose down --volumes, then the backup taken before the upgrade, with A's images"
down down-volumes.log --volumes
left="$(docker volume ls -q --filter "label=$PROJECT_LABEL=$PROJECT")" || fail "cannot list the project's volumes"
[ -z "$left" ] || fail "docker compose down --volumes left a volume"
restore
schema_log schema-restored.log
logged schema-restored.log "schema: nothing to do: the database has every schema change of PubDex $A_VERSION" \
  && ! logged schema-restored.log "schema: applying" \
  || { mask < "$LOGS/schema-restored.log" | tail -n 10 >&2; fail "A's schema upgrade after the restore did not find the dump's record"; }
[ "$(schema_record 2>/dev/null)" = "$RECORD_A" ] || fail "the restored database records other schema changes than A's"
[ "$(counts 2>/dev/null)" = "$COUNTS_A" ] || fail "after the restore the verification query counts other rows than before the dump"
stamped "$A_TRANSFORM"
served served-restored.json
ok "restored into a new volume with A's images: nothing to do, A's record, the verification query counts $COUNTS_A, rows stamped people_pubs/$A_TRANSFORM, $DEMO_DOI served"
down down-test-end.log --volumes
part_done "A to B2 and the rollback by restore"

# --- 6. the published releases to A ---------------------------------------------------

for version in "${PUBLISHED[@]}"; do
  app="${PUBLISHED_APP[$version]}"
  postgres="${PUBLISHED_POSTGRES[$version]}"
  role="the preceding release"
  [ "$version" = "$preceding" ] || role="the floor"
  step "$role $version, published: its images by digest and the compose.yaml its application image carries"
  for ref in "$app" "$postgres"; do
    if ! docker image inspect "$ref" >/dev/null 2>&1; then
      bounded 900 docker pull "$ref" > "$LOGS/pull.log" 2>&1 \
        || { tail -n 10 "$LOGS/pull.log" >&2; fail "docker pull $ref failed; the published images are pulled without credentials"; }
      ok "pulled $ref anonymously"
    fi
  done
  new_stack "pubdex-upgrade-$RUN_TAG-from-${version//./-}"
  use_images "$app" "$postgres"
  create_secrets
  up "up-$version.log"
  bounded 300 dc run --rm demo > "$LOGS/demo-$version.log" 2>&1 \
    || { mask < "$LOGS/demo-$version.log" | tail -n 20 >&2; fail "docker compose run --rm demo failed with $version"; }
  served "served-$version.json"
  stamped "${PUBLISHED_TRANSFORM[$version]}"
  # A release without schema changes in its record, such as 0.1.0, leaves the
  # database at the baseline, without the record.
  read_into recorded "whether the database has a record of schema changes" has_record
  if [ "${PUBLISHED_SCHEMA[$version]}" = "[]" ]; then
    [ "$recorded" = f ] || fail "PubDex $version records no schema change, but its database has the record"
  else
    [ "$recorded" = t ] || fail "PubDex $version records schema changes, but its database has no record"
  fi
  backup
  ok "PubDex $version serves $DEMO_DOI, rows stamped people_pubs/${PUBLISHED_TRANSFORM[$version]}; a backup taken"

  step "Upgrade from $version to A: docker compose down, A's deployment files and images, docker compose up --wait"
  switch_to "$A_APP" "$A_POSTGRES" "a-from-$version"
  schema_log "schema-a-from-$version.log"
  read_into upgraded "the record of schema changes" schema_record
  if [ "$recorded" = f ]; then
    logged "schema-a-from-$version.log" "schema: the database holds the baseline and has had no schema change" \
      && logged "schema-a-from-$version.log" "schema: done: applied" && [ "$upgraded" = "$RECORD_A" ] \
      || { mask < "$LOGS/schema-a-from-$version.log" | tail -n 10 >&2; fail "A's schema upgrade did not apply its schema changes to $version's baseline"; }
  else
    [ "$(cut -d '|' -f 1,2 <<<"$upgraded")" = "$(cut -d '|' -f 1,2 <<<"$RECORD_A")" ] \
      || fail "after the upgrade from $version the record holds other schema changes than A's: $(tr '\n' ' ' <<<"$upgraded")"
  fi
  served "served-a-from-$version.json"
  ok "A's schema upgrade brought $version's database to A's schema changes; A serves $DEMO_DOI"

  step "Image rollback from A to $version: docker compose down, $version's deployment files and images, docker compose up --wait"
  switch_to "$app" "$postgres" "rollback-$version"
  served "served-rollback-$version.json"
  [ "$(schema_record 2>/dev/null)" = "$upgraded" ] || fail "the image rollback to $version changed the record of schema changes"
  ok "PubDex $version's images, on A's schema, are ready and serve $DEMO_DOI"
  down "down-$version-end.log" --volumes
  part_done "$version to A and the image rollback"
done

# --- passwords ------------------------------------------------------------------------

step "Checking every output of the run for passwords"
if leaks_in "$LOGS"/*; then
  fail "a password occurs in an output the run kept"
fi
ok "no password of the run in any output it kept: Compose, the containers' logs, the commands' output and the HTTP answers"

step "Removing the run's stacks, images and directory"
exit 0
