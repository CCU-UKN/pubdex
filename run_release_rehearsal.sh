#!/usr/bin/env bash
# The rehearsal of a PubDex release, stage 10 of run_ci_suite.sh: release.sh
# builds and verifies test release A -- the checkout as it is, under a
# version reserved for test releases -- as it builds a PubDex release, builds
# it a second time to compare the two, and nothing is published.
#
#   ./run_release_rehearsal.sh
#
# In order:
#   1. before it builds anything, it checks what the rehearsal needs: a Git
#      checkout whose VERSION has no pre-release part; git, GNU tar, gzip,
#      sha256sum, cmp and python3; the pinned helm (./setup_cluster_tools.sh),
#      its SHA-256 and the version it reports; a reachable Docker daemon for
#      linux/amd64 with an Engine DB/lib/docker_versions.sh supports and the
#      buildx plugin; and the pinned SBOM tool (./release.sh setup) with its
#      pinned image id;
#   2. ./release.sh build --test a<run> --out <dir>/first, where <run> is ten
#      random hexadecimal digits of this run's own and <dir> the run's
#      temporary directory: test release A, version <VERSION>-test.a<run>;
#   3. ./release.sh verify <dir>/first;
#   4. ./release.sh build --test a<run> --out <dir>/second, which ends with
#      verify of its own directory, and the comparison of the two builds:
#      the same files; the source archive and the chart package the same,
#      byte for byte; IMAGES the same but for the image ids; each SBOM and
#      the provenance record the same once the values a build changes are
#      left out -- in an SBOM its creation time, its document namespace and
#      the image's own checksums and package URL, in the record the build's
#      start and end, the image ids and config digests, the images' creation labels and the
#      SHA-256 of IMAGES and of both SBOMs. The second build gives the
#      images new ids under the same local tags, so the first directory is
#      verified before it starts;
#   5. the chart from the package file, with the pinned helm: helm lint
#      --strict for the oldest and the newest Kubernetes release the chart
#      supports -- the floor of kubeVersion in the package's Chart.yaml and
#      the release of the pinned kind node image (DB/lib/cluster_tools.sh) --
#      with the chart's own values and with each example the package holds,
#      then helm template with each of them and the images set to test
#      release A's local tags: every image the manifests name is one of the
#      two, both occur, and the chart's label names the test release's
#      version;
#   6. that nothing was published: no image of the run carries a tag but
#      its two local ones, and the Docker client configuration the run used
#      still holds no registry credential;
#   7. the cleanup below, confirmed.
#
# Publishing. This script never runs release.sh publish, docker push,
# docker login or docker tag. Every Docker command of the run -- its own and
# those of release.sh and build_image.sh -- uses a Docker client
# configuration of the run's own in its temporary directory, which names at
# most the caller's plugin directory (cliPluginsExtraDirs), with
# DOCKER_AUTH_CONFIG and DOCKER_CONTEXT unset and DOCKER_HOST naming the
# daemon the caller's client uses: no credential of the caller's, from
# docker login or a credential helper, is within the run's reach. What the
# rehearsal proves: that release.sh build and verify produce and accept the
# complete artifact set of the checkout as it is on this machine; that the
# source archive and the chart package come out the same, byte for byte,
# when built again, and the SBOMs and the provenance record differ only
# where a build has to; that the chart package lints and renders on its
# own; and that the run published nothing and held no credential with which
# it could have. What it does not prove: that publish works against a
# registry, which DB/tests/test_release_script.py tests with a stubbed
# docker in stage 1; the refusals that guard a PubDex release -- a checkout
# with changes, a missing or misplaced annotated tag, a version a repository
# already holds as another image -- which a test release skips and the same
# tests cover;
# that a build at the tag from a clean clone, without Docker's build cache,
# gives the same result, which is the maintainer's step at a PubDex release;
# and that the images are the
# same, byte for byte, when rebuilt, which they are not: they are
# rebuildable from pinned inputs and identified by their recorded digests.
#
# Cleanup. Registered before anything is created, it runs on every exit and
# after SIGINT, SIGTERM or SIGHUP, ignoring further signals while it works.
# Every image of the run carries the label
# org.opencontainers.image.version=<VERSION>-test.a<run>, a version no other
# run uses. The cleanup removes every container of the run -- a probe of
# release.sh, which carries the label of its image, or the SBOM tool, which
# mounts the run's directory -- both local tags, every image with the
# label, both builds' and any an interrupted build left, and the run's
# temporary directory. It then confirms that none of them is left; if one
# is, or Docker cannot say, it prints the commands that find it and fails --
# with status 1 after an otherwise successful run, and with the earlier
# failure's or the signal's status otherwise. It removes nothing else: the
# base images, the SBOM tool and the build cache stay, and nothing is
# pruned. An interrupt from the terminal or a signal to the process group
# reaches release.sh and the build at once; a signal to this script alone
# takes effect when the running command returns. SIGKILL skips the
# cleanup; the label finds what is left.
#
# No test release is kept for a later stage: each stage that needs one
# builds its own, and BuildKit's cache makes a second build of the same
# content take seconds.
#
# Network access: release.sh build pulls the base images by digest and
# fetches the locked files unless Docker has them (build_image.sh). The SBOM
# tool and the probes of the images run without network, and helm contacts
# no cluster.
#
# Needs Bash, a Git checkout, the pinned helm (./setup_cluster_tools.sh),
# the pinned SBOM tool (./release.sh setup), python3, and a Docker Engine
# 28.0.1 or later, except 28.2.0 to 28.3.2 (DB/lib/docker_versions.sh says
# why), for linux/amd64, with the buildx plugin, on this machine: the SBOM
# tool reads the saved images from a bind mount of the temporary directory.
# Exit status: 0 when every check passed and the cleanup was confirmed, 1
# for a failed check, build or cleanup, 2 for a usage error, 130, 143 or 129
# after a signal.
set -euo pipefail
export LC_ALL=C

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PROG="run_release_rehearsal.sh"
# A variable of the caller's must not point git at another repository.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
  GIT_COMMON_DIR GIT_NAMESPACE GIT_CEILING_DIRECTORIES
# shellcheck source=DB/lib/docker_versions.sh
source "$ROOT/DB/lib/docker_versions.sh"
# shellcheck source=DB/lib/cluster_tools.sh
source "$ROOT/DB/lib/cluster_tools.sh"
# shellcheck source=DB/lib/sbom_tool.sh
source "$ROOT/DB/lib/sbom_tool.sh"

APP_LOCAL="pubdex-release"
POSTGRES_LOCAL="pubdex-postgres-release"
VERSION_LABEL="org.opencontainers.image.version"
PLATFORM="linux/amd64"

usage() {
  cat <<'USAGE'
usage: run_release_rehearsal.sh

Rehearses a PubDex release without publishing anything: release.sh builds
test release A -- the checkout as it is, under a version reserved for test
releases -- into a temporary directory and verifies it, builds it a second
time and compares the source archive and the chart package byte for byte
and the SBOMs and the provenance record but for the build's times and image
ids, then lints and renders the chart from its package file. Docker runs
with a client configuration of the run's own that holds no registry
credential. The run's images, tags and directory are removed at the end,
and the removal is confirmed.
Needs the pinned helm (./setup_cluster_tools.sh), the pinned SBOM tool
(./release.sh setup), python3, and Docker Engine 28.0.1 or later, except
28.2.0 to 28.3.2, for linux/amd64, with the buildx plugin.

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

fail() {
  echo "$PROG: $1" >&2
  shift
  local hint
  for hint in "$@"; do
    echo "  $hint" >&2
  done
  exit 1
}

started=$SECONDS
step() {
  echo
  echo "==> $* ($((SECONDS - started)) s)"
}

ok() { echo "    ok  $*"; }

need() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 was not found on PATH"
}

# --- the run's directory, identity and cleanup --------------------------------

RUN_DIR=""
version=""
BUILT=0  # set before the first build: from then on the run may own images

# The ids of the run's containers, one per line; fails when Docker cannot say.
owned_containers() {
  local ids id sources
  ids="$(docker ps -aq --no-trunc --filter "label=$VERSION_LABEL=$version")" || return 1
  [ -z "$ids" ] || printf '%s\n' $ids
  ids="$(docker ps -aq --no-trunc --filter "ancestor=$PINNED_SYFT_IMAGE")" || return 1
  for id in $ids; do
    # A container that is gone by now is no longer the run's.
    sources="$(docker inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "$id" 2>/dev/null)" || continue
    if grep -qF -- "$RUN_DIR/" <<<"$sources"; then
      echo "$id"
    fi
  done
}

# The ids of the run's images, dangling ones included; fails when Docker cannot say.
owned_images() {
  docker image ls -aq --no-trunc --filter "label=$VERSION_LABEL=$version"
}

remove_owned() {
  local id tag
  for id in $(owned_containers 2>/dev/null | sort -u); do
    docker rm -f "$id" >/dev/null 2>&1 || true
  done
  for tag in "$APP_LOCAL:$version" "$POSTGRES_LOCAL:$version"; do
    docker image rm "$tag" >/dev/null 2>&1 || true
  done
  for id in $(owned_images 2>/dev/null | sort -u); do
    docker image rm "$id" >/dev/null 2>&1 || true
  done
}

confirm_removed() {  # nothing of the run left in Docker, or a warning with the commands that find it
  local containers images tag tags_left=""
  for tag in "$APP_LOCAL:$version" "$POSTGRES_LOCAL:$version"; do
    if docker image inspect "$tag" >/dev/null 2>&1; then
      tags_left+="$tag "
    fi
  done
  if ! containers="$(owned_containers 2>/dev/null)" || ! images="$(owned_images 2>/dev/null)" \
     || [ -n "$containers$images$tags_left" ]; then
    echo "WARNING: could not confirm that the rehearsal of $version left nothing behind. Check with" >&2
    echo "  docker image ls -a --filter label=$VERSION_LABEL=$version" >&2
    echo "  docker ps -a --filter label=$VERSION_LABEL=$version" >&2
    echo "  docker ps -a --filter ancestor=$PINNED_SYFT_IMAGE    (those that mount $RUN_DIR)" >&2
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
  if [ "$BUILT" = 1 ]; then
    remove_owned
    if confirm_removed; then
      echo "    ok  no image, tag or container of $version is left"
    else
      [ "$rc" -ne 0 ] || rc=1  # an earlier failure keeps its own status
    fi
  fi
  if [ -n "$RUN_DIR" ]; then
    rm -rf -- "$RUN_DIR" 2>/dev/null || true
    if [ -e "$RUN_DIR" ]; then
      echo "WARNING: could not remove the run's temporary directory $RUN_DIR." >&2
      [ "$rc" -ne 0 ] || rc=1
    elif [ "$BUILT" = 1 ]; then
      echo "    ok  the run's temporary directory is gone"
    fi
  fi
  if [ "$rc" -eq 0 ]; then
    echo
    echo "PASS: the rehearsal of test release A, $version, passed in $((SECONDS - started)) s; nothing was published."
  fi
  exit "$rc"
}

exec 3>&1 4>&2  # the run's own output, for the cleanup
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# --- 1. what the rehearsal needs -------------------------------------------------

step "Checking what the rehearsal needs"
for tool in git tar gzip sha256sum cmp python3 od docker; do
  need "$tool"
done
tar_version="$(tar --version 2>/dev/null)" || tar_version=""
[[ "${tar_version%%$'\n'*}" == *"GNU tar"* ]] || fail "tar is not GNU tar, which release.sh needs for the source archive"
if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  fail "this directory is not a Git checkout; release.sh reads the checkout's files and commit with git"
fi
[ -r "$ROOT/VERSION" ] || fail "VERSION is missing"
file_version="$(head -n 1 "$ROOT/VERSION" | tr -d '[:space:]')"
if ! [[ "$file_version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  fail "VERSION holds '$file_version'; a test release's version is <VERSION>-test.<name> for a VERSION without a pre-release part"
fi
run_tag="$(od -An -N5 -tx1 /dev/urandom | tr -d ' \n')"
[[ "$run_tag" =~ ^[0-9a-f]{10}$ ]] || fail "could not draw the run's random name"
name="a$run_tag"
version="$file_version-test.$name"

RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pubdex-rehearsal.XXXXXX")"
RUN_DIR="$(cd "$RUN_DIR" && pwd -P)"
mkdir -p "$RUN_DIR/home" "$RUN_DIR/tmp" "$RUN_DIR/helm/config" "$RUN_DIR/helm/cache" "$RUN_DIR/helm/data"
mkdir -m 700 "$RUN_DIR/docker"

HELM="$(cluster_tool_path "$ROOT" helm)"
helm_shown=".tools/helm-$PINNED_HELM_VERSION/helm"
if [ ! -f "$HELM" ] || [ ! -x "$HELM" ]; then
  fail "the pinned helm $PINNED_HELM_VERSION is not installed at $helm_shown. Install it with:" \
    "./setup_cluster_tools.sh"
fi
have="$(sha256sum < "$HELM")" || fail "could not read $helm_shown"
have="${have%% *}"
if [ "$have" != "$PINNED_HELM_MEMBER_SHA256" ]; then
  fail "$helm_shown has the SHA-256 $have, not the pinned $PINNED_HELM_MEMBER_SHA256." \
    "Remove .tools/helm-$PINNED_HELM_VERSION and install it again with: ./setup_cluster_tools.sh"
fi

# helm with nothing of the caller's environment but PATH, and no kubeconfig.
run_helm() {
  env -i PATH="$PATH" LC_ALL=C HOME="$RUN_DIR/home" TMPDIR="$RUN_DIR/tmp" KUBECONFIG="$RUN_DIR/no-kubeconfig" \
    DOCKER_CONFIG="$RUN_DIR/docker" HELM_CONFIG_HOME="$RUN_DIR/helm/config" HELM_CACHE_HOME="$RUN_DIR/helm/cache" \
    HELM_DATA_HOME="$RUN_DIR/helm/data" "$HELM" "$@"
}

reported="$(run_helm version --template '{{.Version}}' 2>/dev/null)" || reported=""
if [ "$reported" != "v$PINNED_HELM_VERSION" ]; then
  fail "$helm_shown reports the version '$reported', not the pinned v$PINNED_HELM_VERSION"
fi
ok "helm $reported ($helm_shown, SHA-256 matches the pin)"

# The daemon the caller's Docker client uses, reached from now on through a
# client configuration of the run's own, which holds no credential.
if [ -z "${DOCKER_HOST:-}" ]; then
  context="$(docker context inspect --format '{{.Name}}{{"\t"}}{{.Endpoints.docker.Host}}{{"\t"}}{{len .TLSMaterial}}' 2>/dev/null)" \
    || context=""
  IFS=$'\t' read -r context_name endpoint tls_files <<<"$context" || true
  if [ -z "${endpoint:-}" ]; then
    fail "cannot read the Docker daemon's address from the current Docker context ('docker context inspect' fails)"
  fi
  if [ "${tls_files:-0}" != 0 ]; then
    fail "the Docker context $context_name uses TLS files, which the run's own client configuration cannot carry." \
      "Select its daemon with DOCKER_HOST, DOCKER_TLS_VERIFY and DOCKER_CERT_PATH instead."
  fi
  export DOCKER_HOST="$endpoint"
fi
# The plugins of the caller's configuration directory -- buildx installed
# for one user, under ~/.docker/cli-plugins -- stay within reach; nothing
# else of that directory is read.
caller_plugins="${DOCKER_CONFIG:-${HOME:-/nonexistent}/.docker}/cli-plugins"
if [ -d "$caller_plugins" ]; then
  python3 -I -c 'import json, sys; json.dump({"cliPluginsExtraDirs": [sys.argv[1]]}, open(sys.argv[2], "w"))' \
    "$caller_plugins" "$RUN_DIR/docker/config.json" || fail "could not write the run's Docker client configuration"
fi
unset DOCKER_CONTEXT DOCKER_AUTH_CONFIG
export DOCKER_CONFIG="$RUN_DIR/docker"

if ! docker info >/dev/null 2>&1; then
  fail "cannot reach the Docker daemon. Start it, or check that this user may use it ('docker info' shows the reason)."
fi
engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null)" || engine=""
problem="$(engine_problem "$engine")"
if [ -n "$problem" ]; then
  fail "$problem: the rehearsal builds the images of the deployment, which need Docker Engine $ENGINE_SUPPORTED" \
    "(DB/lib/docker_versions.sh says why). Upgrade the engine."
fi
platform="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null)" || platform=""
[ "$platform" = "$PLATFORM" ] || fail "the Docker daemon runs on '$platform'; release.sh builds and verifies images for $PLATFORM"
docker buildx version >/dev/null 2>&1 \
  || fail "the Docker buildx plugin was not found ('docker buildx version' fails); build_image.sh needs it"
sbom_id="$(docker image inspect --format '{{.Id}}' "$PINNED_SYFT_IMAGE" 2>/dev/null)" || sbom_id=""
if [ -z "$sbom_id" ]; then
  fail "the pinned SBOM tool, Syft $PINNED_SYFT_VERSION, is not on this machine. Pull it, by digest, with:" \
    "./release.sh setup"
fi
if [ "$sbom_id" != "$PINNED_SYFT_IMAGE_ID" ] && [ "$sbom_id" != "$PINNED_SYFT_INDEX_DIGEST" ]; then
  fail "the image of $PINNED_SYFT_IMAGE on this machine has the id $sbom_id, not the pinned $PINNED_SYFT_IMAGE_ID." \
    "Remove it with 'docker image rm $sbom_id' and pull it again with: ./release.sh setup"
fi
ok "Docker Engine $engine on $platform, the buildx plugin, the pinned SBOM tool Syft $PINNED_SYFT_VERSION"
ok "test release A: $version, in $RUN_DIR"

# release.sh, with the run's temporary directory for its own.
release() {
  TMPDIR="$RUN_DIR/tmp" "$ROOT/release.sh" "$@"
}

# --- 2. test release A, built -----------------------------------------------------

BUILT=1
step "Test release A: ./release.sh build --test $name"
release build --test "$name" --out "$RUN_DIR/first" || fail "release.sh build failed for test release A"

# --- 3. test release A, verified ----------------------------------------------------

step "./release.sh verify of test release A"
release verify "$RUN_DIR/first" || fail "release.sh verify refused the directory its build wrote"

# --- 4. the second build and the comparison ---------------------------------------

step "The second build of test release A, for the comparison"
release build --test "$name" --out "$RUN_DIR/second" || fail "release.sh build failed for the second build of test release A"

step "Comparing the two builds"
first_files="$(cd "$RUN_DIR/first" && ls -A)"
second_files="$(cd "$RUN_DIR/second" && ls -A)"
if [ "$first_files" != "$second_files" ]; then
  fail "the two builds wrote different files: '$(tr '\n' ' ' <<<"$first_files")' and '$(tr '\n' ' ' <<<"$second_files")'"
fi
for file in "pubdex-$version-source.tar.gz" "pubdex-$version.tgz"; do
  first_hash="$(sha256sum < "$RUN_DIR/first/$file")"
  second_hash="$(sha256sum < "$RUN_DIR/second/$file")"
  if ! cmp -s -- "$RUN_DIR/first/$file" "$RUN_DIR/second/$file"; then
    fail "$file differs between the two builds (SHA-256 ${first_hash%% *} and ${second_hash%% *}); a second build has to give the same bytes"
  fi
  ok "$file: the same bytes in both builds, SHA-256 ${first_hash%% *}"
done

# IMAGES, both SBOMs and the provenance record of the two builds: equal once
# the values that a build has to change are left out.
COMPARE='
import json, sys
from pathlib import Path

first, second, version = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sboms = (f"pubdex-{version}-image.spdx.json", f"pubdex-{version}-postgres-image.spdx.json")
record = f"pubdex-{version}-provenance.json"


def images(directory):
    rows = [line.split(" ") for line in (directory / "IMAGES").read_text(encoding="utf-8").splitlines()]
    return [row[:2] for row in rows]


def sbom(document):
    document.pop("documentNamespace", None)
    document.get("creationInfo", {}).pop("created", None)
    for package in document.get("packages", []):
        if package.get("primaryPackagePurpose") == "CONTAINER":
            package.pop("checksums", None)
            package.pop("externalRefs", None)
    return document


def provenance(document):
    document["build"].pop("started", None)
    document["build"].pop("finished", None)
    for image in document["images"].values():
        image.pop("id", None)
        image.pop("config_digest", None)
        image["labels"].pop("org.opencontainers.image.created", None)
    for name in ("IMAGES", *sboms):
        document["files"].pop(name, None)
    return document


def differences(a, b, path):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            yield from differences(a.get(key), b.get(key), f"{path}.{key}")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for index, (x, y) in enumerate(zip(a, b)):
            yield from differences(x, y, f"{path}[{index}]")
    elif a != b:
        yield path


problems = 0
if images(first) != images(second):
    print("IMAGES names other roles or tags in the two builds")
    problems += 1
else:
    print("ok  IMAGES: the same roles and tags, other image ids")
for name, normal in ((sboms[0], sbom), (sboms[1], sbom), (record, provenance)):
    try:
        a = normal(json.loads((first / name).read_text(encoding="utf-8")))
        b = normal(json.loads((second / name).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as error:
        print(f"{name} cannot be compared: {error!r}")
        problems += 1
        continue
    found = list(differences(a, b, ""))
    if found:
        print(f"{name} differs beyond the build time and the image identities, at " + ", ".join(found[:10])
              + (f" and {len(found) - 10} more" if len(found) > 10 else ""))
        problems += 1
    else:
        print(f"ok  {name}: the same but for the build time and the image identities")
sys.exit(1 if problems else 0)
'
if ! python3 -I -c "$COMPARE" "$RUN_DIR/first" "$RUN_DIR/second" "$version" > "$RUN_DIR/compare.log" 2>&1; then
  sed 's/^/    /' "$RUN_DIR/compare.log" >&2
  fail "the SBOMs, the provenance record or IMAGES of the two builds differ where a build must not change them"
fi
sed 's/^/    /' "$RUN_DIR/compare.log"

# --- 5. the chart from its package file -------------------------------------------

package="$RUN_DIR/first/pubdex-$version.tgz"
step "The chart from its package file pubdex-$version.tgz"
run_helm show chart "$package" > "$RUN_DIR/chart.yaml" 2>"$RUN_DIR/chart.err" \
  || fail "helm show chart cannot read pubdex-$version.tgz: $(head -n 1 "$RUN_DIR/chart.err")"
oldest="$(sed -n "s/^kubeVersion: *['\"]\{0,1\}>=\([0-9]*\.[0-9]*\.[0-9]*\)-0['\"]\{0,1\} *\$/\1/p" "$RUN_DIR/chart.yaml")"
[ -n "$oldest" ] || fail "the package's Chart.yaml names no floor as kubeVersion: '>=<major>.<minor>.<patch>-0'"
newest="$(sed -n 's,^kindest/node:v\([0-9]*\.[0-9]*\.[0-9]*\)@sha256:[0-9a-f]*$,\1,p' <<<"$PINNED_KIND_NODE_IMAGE")"
[ -n "$newest" ] || fail "cannot read the Kubernetes release of the pinned kind node image $PINNED_KIND_NODE_IMAGE"
mkdir "$RUN_DIR/chart"
tar -xzf "$package" -C "$RUN_DIR/chart" --no-same-owner || fail "pubdex-$version.tgz cannot be extracted"
shopt -s nullglob
examples=("$RUN_DIR"/chart/pubdex/examples/*.yaml)
shopt -u nullglob
[ "${#examples[@]}" -gt 0 ] || fail "pubdex-$version.tgz holds no example under examples/"

for kube in "$oldest" "$newest"; do
  for file in "" "${examples[@]}"; do
    args=()
    shown="the chart's own values"
    if [ -n "$file" ]; then
      args=(--values "$file")
      shown="${file#"$RUN_DIR"/chart/pubdex/}"
    fi
    if ! run_helm lint --strict "$package" --kube-version "$kube" ${args[@]+"${args[@]}"} > "$RUN_DIR/lint.log" 2>&1; then
      sed 's/^/    /' "$RUN_DIR/lint.log" >&2
      fail "helm lint --strict fails for pubdex-$version.tgz, Kubernetes $kube, $shown"
    fi
    ok "helm lint --strict, Kubernetes $kube, $shown"
  done
done

app_image="$APP_LOCAL:$version"
postgres_image="$POSTGRES_LOCAL:$version"
for file in "" "${examples[@]}"; do
  args=()
  shown="the chart's own values"
  if [ -n "$file" ]; then
    args=(--values "$file")
    shown="${file#"$RUN_DIR"/chart/pubdex/}"
  fi
  if ! run_helm template rehearsal "$package" --namespace pubdex-rehearsal --kube-version "$newest" \
         ${args[@]+"${args[@]}"} --set secrets.name=pubdex-credentials \
         --set image.registry= --set image.repository="$APP_LOCAL" --set image.tag="$version" \
         --set postgresImage.registry= --set postgresImage.repository="$POSTGRES_LOCAL" \
         --set postgresImage.tag="$version" > "$RUN_DIR/rendered.yaml" 2>"$RUN_DIR/rendered.err"; then
    sed 's/^/    /' "$RUN_DIR/rendered.err" >&2
    fail "helm template fails for pubdex-$version.tgz with $shown"
  fi
  rendered_images="$(sed -n "s/^[[:space:]-]*image:[[:space:]]*['\"]\{0,1\}\([^'\"]*\)['\"]\{0,1\}[[:space:]]*\$/\1/p" \
    "$RUN_DIR/rendered.yaml" | sort -u)"
  if [ "$rendered_images" != "$(printf '%s\n%s' "$postgres_image" "$app_image" | sort -u)" ]; then
    fail "pubdex-$version.tgz with $shown names the images '$(tr '\n' ' ' <<<"$rendered_images")', not exactly $app_image and $postgres_image"
  fi
  if ! grep -qx "[[:space:]]*helm.sh/chart: ['\"]\{0,1\}pubdex-$version['\"]\{0,1\}" "$RUN_DIR/rendered.yaml"; then
    fail "pubdex-$version.tgz with $shown renders no label helm.sh/chart: pubdex-$version"
  fi
  ok "helm template, $shown: the images $app_image and $postgres_image, the chart pubdex-$version"
done

# --- 6. nothing published -----------------------------------------------------------

step "Nothing published"
ids="$(owned_images)" || fail "cannot list the run's images"
tags=""
for id in $(sort -u <<<"$ids"); do
  listed="$(docker image inspect --format '{{range .RepoTags}}{{println .}}{{end}}' "$id")" \
    || fail "cannot read the tags of the run's image $id"
  tags+="$listed"$'\n'
done
tags="$(sed '/^$/d' <<<"$tags" | sort -u)"
if [ "$tags" != "$(printf '%s\n%s' "$app_image" "$postgres_image" | sort -u)" ]; then
  fail "the run's images carry the tags '$(tr '\n' ' ' <<<"$tags")', not exactly the local $app_image and $postgres_image"
fi
ok "the run's images carry only the local tags $app_image and $postgres_image"
CREDENTIALS='
import json, sys
from pathlib import Path
config = Path(sys.argv[1]) / "config.json"
held = sorted(key for key in ("auths", "credsStore", "credHelpers")
              if config.exists() and json.loads(config.read_text(encoding="utf-8") or "{}").get(key))
print(" ".join(held))
'
held="$(python3 -I -c "$CREDENTIALS" "$DOCKER_CONFIG" 2>&1)" || fail "cannot read the run's Docker client configuration: $held"
[ -z "$held" ] || fail "the run's Docker client configuration holds registry credentials ($held)"
ok "the run's Docker client configuration holds no registry credential, and release.sh publish never ran"

step "Removing the run's images, tags and directory"
exit 0
