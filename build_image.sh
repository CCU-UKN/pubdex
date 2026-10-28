#!/usr/bin/env bash
# Builds the two images of the PubDex deployment from this checkout, and
# records in their labels what they were built from: the application image
# (Dockerfile), which holds the people_pubs package, the schema bootstrap of
# DB/init/ and the Compose files, and the PostgreSQL image (postgres.Dockerfile),
# PostgreSQL built from its verified source without XML support. They and
# compose.yaml are the whole deployment.
#
#   ./build_image.sh                          # tags pubdex:local and pubdex-postgres:local
#   ./build_image.sh --tag <name>:<tag> ...   # other application tags instead (repeatable)
#   ./build_image.sh --postgres-tag <name>:<tag> ...  # other PostgreSQL tags instead (repeatable)
#   ./build_image.sh --source <URL>           # records the repository they came from
#   ./build_image.sh --no-cache               # rebuilds every layer
#
# The labels of both images (DEPLOYMENT.md, "The application image"):
#
#   org.opencontainers.image.version    the release version, from VERSION
#   org.opencontainers.image.revision   the commit the checkout is at; with
#                                       "-dirty" appended when the working tree
#                                       differs from it in any file Git does
#                                       not ignore or holds a Python module of
#                                       the package that Git ignores, and
#                                       "unknown" outside a Git checkout, so a
#                                       build of local changes never claims to
#                                       be the commit
#   org.opencontainers.image.created    the time of the build (UTC)
#   org.opencontainers.image.source     the --source URL, empty without it
#   pubdex.transform-version            people_pubs.__version__, the stamp
#                                       ingestion writes into source provenance
#                                       (application image)
#   pubdex.postgresql-version           the PostgreSQL release it was built
#                                       from (PostgreSQL image)
#
# A tag whose tag part is the release version -- <name>:0.1.0 -- is refused,
# for either image, for a modified working tree, so a local build cannot
# carry the release tag.
# The build context is what .dockerignore lets in, and nothing is pushed:
# publishing an image is a separate step (DEPLOYMENT.md, "Publishing an
# image"). Git is used read-only, to find the commit and whether the working
# tree differs from it, by the rules of DB/lib/release_identity.sh, which
# run_stack_acceptance.sh checks the labels with.
#
# Needs Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2, the Engine the
# deployment supports (DB/lib/docker_versions.sh; this script does not check
# it), and its
# BuildKit builder, which the buildx plugin provides: docker-buildx-plugin in
# Docker's own packages, docker-buildx in Debian and Ubuntu, where an
# installation of Debian's docker.io brings it along as a recommended package
# and Ubuntu's docker.io only suggests it. Without the plugin Docker falls
# back to its deprecated legacy builder, which this script does not use. The
# builds pull their base images by digest unless the daemon has them, fetch
# the files of DB/requirements-pip.txt, DB/requirements-build.txt and
# DB/requirements.txt from the package index and the PostgreSQL source from
# postgresql.org, each checked against the SHA-256 the repository fixes,
# and Debian's packages from Debian's signed archive; nothing is installed
# with network access. Exit status: 0 on success, 1 when a build fails -- a
# file that does not match its hash included -- or the checkout cannot be
# built as asked, 2 for a usage error.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_TAG="pubdex:local"
DEFAULT_POSTGRES_TAG="pubdex-postgres:local"
# shellcheck source=DB/lib/release_identity.sh
source "$ROOT/DB/lib/release_identity.sh"

usage() {
  cat <<'USAGE'
usage: build_image.sh [--tag <reference>]... [--postgres-tag <reference>]...
                      [--source <URL>] [--no-cache]

Builds the application image and the PostgreSQL image from this checkout with
Docker and labels both with the release version (VERSION), the source
revision -- "<commit>-dirty" for a working tree with changes -- and the build
time, the application image also with the transform version, the PostgreSQL
image with its PostgreSQL release.

  --tag <reference>           tag the application image with this reference
                              instead of pubdex:local; repeatable
  --postgres-tag <reference>  tag the PostgreSQL image with this reference
                              instead of pubdex-postgres:local; repeatable
                              The release version as tag of either image
                              needs a working tree without changes.
  --source <URL>              record this repository URL in org.opencontainers.image.source
  --no-cache                  build every layer afresh
  -h, --help         show this help
USAGE
}

die_usage() {
  echo "build_image.sh: $1" >&2
  usage >&2
  exit 2
}

fail() {
  echo "build_image.sh: $*" >&2
  exit 1
}

tags=()
postgres_tags=()
source_url=""
no_cache=()
while [ $# -gt 0 ]; do
  case "$1" in
    --tag)
      if [ $# -lt 2 ] || [ -z "$2" ]; then die_usage "--tag needs a value"; fi
      tags+=("$2")
      shift 2
      ;;
    --postgres-tag)
      if [ $# -lt 2 ] || [ -z "$2" ]; then die_usage "--postgres-tag needs a value"; fi
      postgres_tags+=("$2")
      shift 2
      ;;
    --source)
      if [ $# -lt 2 ]; then die_usage "--source needs a value"; fi
      source_url="$2"
      shift 2
      ;;
    --no-cache)
      no_cache=(--no-cache)
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
if [ "${#tags[@]}" -eq 0 ]; then
  tags=("$DEFAULT_TAG")
fi
if [ "${#postgres_tags[@]}" -eq 0 ]; then
  postgres_tags=("$DEFAULT_POSTGRES_TAG")
fi

command -v docker >/dev/null 2>&1 || fail "docker was not found on PATH"
[ -r "$ROOT/VERSION" ] || fail "VERSION is missing from $ROOT"
version="$(head -n 1 "$ROOT/VERSION" | tr -d '[:space:]')"
if ! [[ "$version" =~ $RELEASE_VERSION_RE ]]; then
  fail "VERSION must hold a version such as 0.1.0 (MAJOR.MINOR.PATCH, optionally -prerelease)"
fi
transform_version="$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$ROOT/DB/people_pubs/__init__.py")"
[ -n "$transform_version" ] || fail "cannot read __version__ from DB/people_pubs/__init__.py"
docker buildx version >/dev/null 2>&1 \
  || fail "the Docker buildx plugin was not found ('docker buildx version' fails); install it: docker-buildx on Debian or Ubuntu, docker-buildx-plugin from Docker's own packages"

# The commit, and whether the working tree differs from it. A status that
# cannot be read counts as a difference, and so does a Python module of the
# package that Git ignores -- in a __pycache__ directory, say -- because the
# build context takes in every one (.dockerignore).
revision="$(checkout_revision "$ROOT" 'DB/people_pubs/*.py')"
modified=0
if [ "$revision" = unknown ] || [[ "$revision" == *-dirty ]]; then
  modified=1
fi

for tag in "${tags[@]}" "${postgres_tags[@]}"; do
  if [ "$(reference_tag "$tag")" = "$version" ] && [ "$modified" -eq 1 ]; then
    fail "refusing to tag $tag: the release version as tag needs a Git checkout whose working tree has no changes (this build would be labelled revision $revision)"
  fi
done

created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

tag_args=()
for tag in "${tags[@]}"; do
  tag_args+=(--tag "$tag")
done
postgres_tag_args=()
for tag in "${postgres_tags[@]}"; do
  postgres_tag_args+=(--tag "$tag")
done

# Attestations are left to a release process: each image stays one plain
# manifest, whatever builder and image store the daemon uses. Buildx's own
# Git metadata is switched off, so the labels are the only record of the
# source and cannot be overridden by a setting of the caller's, and so is a
# DOCKER_BUILDKIT=0 of the caller's, which would select the legacy builder.
build() {  # build <Dockerfile> [docker build arguments]
  local dockerfile="$1"
  shift
  BUILDX_GIT_INFO=0 BUILDX_GIT_LABELS=0 DOCKER_BUILDKIT=1 docker build \
    --file "$ROOT/$dockerfile" \
    --provenance=false \
    ${no_cache[@]+"${no_cache[@]}"} \
    --build-arg "PEOPLE_PUBS_VERSION=$version" \
    --build-arg "PEOPLE_PUBS_REVISION=$revision" \
    --build-arg "PEOPLE_PUBS_CREATED=$created" \
    --build-arg "PEOPLE_PUBS_SOURCE=$source_url" \
    "$@" \
    "$ROOT"
}

echo "Building the PubDex PostgreSQL image ${version} from $ROOT (revision $revision)..."
build postgres.Dockerfile "${postgres_tag_args[@]}" || fail "the PostgreSQL image build failed"
echo "Building the PubDex image ${version} from $ROOT (revision $revision)..."
build Dockerfile "${tag_args[@]}" --build-arg "PEOPLE_PUBS_TRANSFORM_VERSION=$transform_version" \
  || fail "the image build failed"

# Read through the first tag: with the containerd image store, the id that
# --iidfile would report is not one that docker image inspect accepts.
describe() {  # describe <title> <tag>...
  local title="$1" image_id platform tag
  shift
  image_id="$(docker image inspect --format '{{.Id}}' "$1" 2>/dev/null)" || image_id="unknown"
  platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$1" 2>/dev/null)" || platform="unknown"
  echo
  echo "Built $title $image_id"
  for tag in "$@"; do
    echo "  tag                $tag"
  done
  echo "  platform           $platform"
}
describe image "${tags[@]}"
describe "PostgreSQL image" "${postgres_tags[@]}"
postgresql_version="$(docker image inspect --format '{{index .Config.Labels "pubdex.postgresql-version"}}' "${postgres_tags[0]}" 2>/dev/null)" || postgresql_version="unknown"
echo "  PostgreSQL         ${postgresql_version:-unknown}"
echo
echo "Both images:"
echo "  release version    $version"
echo "  transform version  $transform_version (application image)"
echo "  revision           $revision"
echo "  created            $created"
if [ "$modified" -eq 1 ]; then
  echo "  The working tree differs from the commit, or is not a Git checkout: these are local builds, not release builds."
fi
