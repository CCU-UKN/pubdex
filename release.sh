#!/usr/bin/env bash
# Builds the artifact set of a PubDex release from this checkout and verifies
# it; a separate step, which only the maintainer runs, publishes its images.
#
#   ./release.sh setup
#   ./release.sh build [--test <name> [--overlay b1|b2]] [--out <dir>] [--source <URL>]
#   ./release.sh verify <dir> [--overlay b1|b2]
#   ./release.sh publish <dir> --repository-prefix <registry>/<namespace>
#
# setup, once per machine, pulls the pinned SBOM tool -- Syft's image, by
# the digest DB/lib/sbom_tool.sh pins -- and checks it as build does. The
# pinned helm comes from ./setup_cluster_tools.sh.
#
# build writes one directory, dist/pubdex-<version>/ unless --out names
# another, which must not exist or be empty (inside the checkout it has to lie
# under dist/, which Git ignores):
#
#   pubdex-<version>-source.tar.gz   the source archive
#   pubdex-<version>.tgz             the Helm chart's package, as helm names it
#   IMAGES                           one line per image: role (application or
#                                    postgres), local tag, image id
#   pubdex-<version>-image.spdx.json           the SBOM of the application
#   pubdex-<version>-postgres-image.spdx.json  image and of the PostgreSQL
#                                    image, SPDX 2.3 JSON, written by the
#                                    pinned Syft
#   pubdex-<version>-provenance.json the provenance record: what the release
#                                    was built from and with, and what the
#                                    build gave (DB/release_provenance.py)
#   SHA256SUMS                       the SHA-256 of every other file of the
#                                    directory, "<hash>  <name>" sorted by name,
#                                    as sha256sum writes and checks it
#
# and publish adds DIGESTS, one line per image, application first:
# <registry>/<namespace>/<repository>:<version>@sha256:<digest>.
#
# The version. A PubDex release takes VERSION's and is built only from a Git
# checkout without changes whose commit the annotated tag v<version> names.
# A test release (--test <name>, <name> lower-case letters and digits) takes
# <VERSION>-test.<name>, builds from the checkout as it is, uncommitted
# changes included, and is never published: every version whose pre-release
# part begins with "test" is reserved for test releases, and VERSION never
# holds one. Semantic Versioning orders 0.1.0 < 0.2.0-test.a < 0.2.0-test.b1
# < 0.2.0, as Helm does.
#
# The test-only overlay. With --overlay, a test release is built from a copy
# of the checkout's files in the temporary directory, with the overlay of
# DB/tests/release_overlay/ applied to the copy alone: b1 adds the schema
# change of compatible.sql, numbered after the copy's last one, and appends
# the suffix that transform-version-suffix holds to the transform version
# (people_pubs.__version__); b2 also adds the schema change of
# incompatible.sql after it and adapts its temporary application with
# application.py to the renamed serving column. These are test releases B1 and B2, which the
# upgrade tests (run_upgrade_acceptance.sh) upgrade test release A to. The
# copy keeps every file's and directory's mode and every file's extended
# attributes, which BuildKit's cache keys on as on the content, so the cache
# serves what the overlay leaves unchanged; its images record the revision
# <commit>-dirty, which the copy's build_image.sh states with --revision, as
# a copy is no Git checkout. Everything below that names the checkout's
# files means the copy's for such a build, and verify, given the same
# --overlay, makes the same copy again to check against.
#
# build, in order:
#   1. checks what it needs -- git, GNU tar, gzip, sha256sum, python3, Docker
#      with the buildx plugin, the pinned helm (./setup_cluster_tools.sh) and
#      the pinned SBOM tool (./release.sh setup): Syft's image has to be on
#      this machine with the pinned id and report the pinned version, and is
#      never pulled by build -- and the version as above; the chart's version
#      and appVersion have to equal VERSION;
#   2. the source archive: GNU tar over the sorted list of the checkout's
#      files -- git ls-files: the commit's files for a PubDex release, and for
#      a test release also the untracked files Git does not ignore, each that
#      exists -- under the directory pubdex-<version>/, in the pax format,
#      without directory entries, with every time the commit's, owner and
#      group 0, the modes 644 and 755, then gzip -n -9. A second build of the
#      same files with the same tar and gzip gives the same bytes;
#   3. the chart package: helm package of the chart's files from that list,
#      with SOURCE_DATE_EPOCH the commit's time, so a second build gives the
#      same bytes whatever the files' times; a test release's package
#      carries the test release's version and appVersion;
#   4. both images, through ./build_image.sh, tagged pubdex-release:<version>
#      and pubdex-postgres-release:<version> on this machine only, their
#      labels naming the version and the revision; then IMAGES. A PubDex
#      release builds both without Docker's build cache (--no-cache), so
#      their Debian packages come from Debian's archive at the time of the
#      build; a test release uses the cache;
#   5. an SBOM of each image: docker image save writes the image to a file,
#      which Syft reads in a container without network, as the calling user,
#      with a read-only root file system;
#   6. the provenance record, from the checkout's files, the images and the
#      tools (DB/release_provenance.py describes every field): the facts
#      that only the images hold -- the packages that compiled psycopg-c and
#      PostgreSQL, which each image carries in build-packages.txt, the client
#      libraries, the PostgreSQL release -- are read by short commands in
#      containers of the images, without network, as an unprivileged user,
#      with a read-only root file system;
#   7. SHA256SUMS, then verify, below, of the finished directory.
# Nothing is written into the checkout but that directory. The files are
# assembled in a temporary directory and moved there at the end, so a build
# that fails or is interrupted leaves no output directory; the images it
# built so far keep their local tags.
#
# verify <dir> builds nothing and checks:
#   - every file of the directory but SHA256SUMS and DIGESTS is in
#     SHA256SUMS, nothing else is, and every hash matches; no subdirectory;
#   - the version, from the source archive's name, against the checkout as
#     build requires it, a PubDex release's tag included;
#   - each image of IMAGES: present under its local tag with the recorded id,
#     platform linux/amd64, its labels naming the version and the checkout's
#     revision, the application image's the transform version;
#   - the source archive: every member under pubdex-<version>/, exactly the
#     checkout's file list in order, each file's content, symlink target and
#     executable bit equal to the checkout's;
#   - the chart package: its name, version and appVersion the version; every
#     file but Chart.yaml, which helm writes afresh, equal to the chart's in
#     the checkout, LICENSE among them; helm lint --strict and helm template
#     from the package file;
#   - the SBOMs and the provenance record, with the images' facts read again
#     as build reads them (DB/release_provenance.py check): each SBOM parses
#     and describes its image and version, the application image's names
#     every package of the runtime lock in its locked version and Debian's
#     libpq5 in the image's version, the PostgreSQL image's names the
#     PostgreSQL release postgres.Dockerfile builds; the record agrees with
#     the checkout's files and pins, the images and their labels, IMAGES and
#     the directory's files.
#
# publish <dir> --repository-prefix <registry>/<namespace> is the
# maintainer's step, after a docker login of their own; PubDex's prefix is
# ghcr.io/ccu-ukn, and a fork names its own. It refuses, before any push:
#   - a test release's version;
#   - a directory already published (it holds DIGESTS);
#   - a directory that verify does not pass, which includes a checkout that
#     is not the clean, tagged commit of the version;
#   - a version that either repository, <prefix>/pubdex-postgres or
#     <prefix>/pubdex, holds as another image, or as an image whose digest
#     the verified image does not record; the version is then spent
#     (RELEASING.md, step 7). docker manifest inspect asks the registry for
#     each: its explicit answer "manifest unknown" counts as absent, and the
#     image is to be pushed; any other answer but the manifest -- an access
#     denial, a network failure, an unknown repository -- stops it. For a
#     manifest the registry holds, docker buildx imagetools inspect reads its
#     digest. Only a digest that the verified image records for that
#     repository (docker image inspect, RepoDigests, which a push of it from
#     this machine records) makes it that very image, published by an
#     earlier run: it is not pushed again, and its digest is taken;
#   - a local tag of those names that already names another image.
# Both images are decided before the first push, so a refusal never follows
# a push. It then tags both images with their repository names, pushes the
# PostgreSQL image first, so that no published application image names a
# PostgreSQL image that is missing, then the application image -- each only
# if the registry does not hold it yet -- prints both digests and writes
# them to DIGESTS, from which the release record takes them
# (releases/README.md). So a publish that failed or was interrupted is
# completed by running it again, in the same clone, on the same directory;
# it pushes nothing over an image that this machine did not push. The
# absence check is a point-in-time check, not a reservation: coordinate
# with other publishers.
#
# What it never does: log in to a registry or read a credential; push, tag
# for a registry or contact one during build or verify (build_image.sh pulls
# the base images and fetches the locked files); move an existing tag;
# publish a test release; push the chart package anywhere; scan the images
# for vulnerabilities; change Git state -- git is only asked, as
# DB/lib/release_identity.sh describes; contact a cluster -- helm runs with
# a cleared environment and no kubeconfig.
#
# What is the same on a second build: the source archive and the chart
# package, byte for byte. The images get new ids, since their labels record
# the build's time, and so the SBOMs -- which also record the time Syft ran
# and a random document name -- and the provenance record, which records the
# build's times and the image ids, differ too.
#
# Exit status: 0 on success, 1 when a step fails or refuses, 2 for a usage
# error.
set -euo pipefail
export LC_ALL=C

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROG="release.sh"
# A variable of the caller's must not point git at another repository.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
  GIT_COMMON_DIR GIT_NAMESPACE GIT_CEILING_DIRECTORIES
# shellcheck source=DB/lib/release_identity.sh
source "$ROOT/DB/lib/release_identity.sh"
# shellcheck source=DB/lib/cluster_tools.sh
source "$ROOT/DB/lib/cluster_tools.sh"
# shellcheck source=DB/lib/sbom_tool.sh
source "$ROOT/DB/lib/sbom_tool.sh"

APP_LOCAL="pubdex-release"
POSTGRES_LOCAL="pubdex-postgres-release"
APP_REPOSITORY="pubdex"
POSTGRES_REPOSITORY="pubdex-postgres"
CHART_DIR="charts/pubdex"
PLATFORM="linux/amd64"
OVERLAY_DIR="DB/tests/release_overlay"
# The tree a build reads its files from: the checkout, or with --overlay the
# copy prepare_overlay makes.
SRC="$ROOT"
overlay=""
PREFIX_RE='^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+$'

usage() {
  cat <<'USAGE'
usage: release.sh setup
       release.sh build [--test <name> [--overlay b1|b2]] [--out <dir>] [--source <URL>]
       release.sh verify <dir> [--overlay b1|b2]
       release.sh publish <dir> --repository-prefix <registry>/<namespace>

setup    pulls the pinned SBOM tool's image by digest and checks it
build    builds the source archive, the Helm chart's package and both images
         of the version VERSION holds -- from a Git checkout without changes
         at the annotated tag v<VERSION> -- an SBOM of each image, the
         provenance record and the checksum file, into one directory, then
         verifies it
           --test <name>    a test release, <VERSION>-test.<name>, from the
                            checkout as it is; never published
           --overlay b1|b2  with --test: from a copy of the checkout with the
                            test-only overlay of DB/tests/release_overlay/ --
                            b1 its backward-compatible schema change and a new
                            transform version, b2 also its schema change that
                            is not backward compatible; the checkout is never
                            changed
           --out <dir>      the directory; default dist/pubdex-<version>
           --source <URL>   the repository URL for the images' source label
verify   checks a directory build wrote, against this checkout -- with the
         --overlay it was built with, against the same copy -- without
         building anything
publish  pushes the two images of a verified PubDex release -- the PostgreSQL
         image first -- to <registry>/<namespace>/pubdex-postgres and
         <registry>/<namespace>/pubdex, each that the registry explicitly
         reports unknown, and writes their digests to DIGESTS; an image the
         registry already holds under the version is taken as published
         only if it is the verified one, pushed from this machine, so a
         run that failed is completed by running it again.
         Log in first; it never does.
  -h, --help    show this help
USAGE
}

die_usage() {
  echo "$PROG: $1" >&2
  usage >&2
  exit 2
}

overlay_argument() {  # overlay_argument <value>: sets overlay, b1 or b2
  case "$1" in
    b1|b2) [ -z "$overlay" ] || die_usage "--overlay is given once"; overlay="$1" ;;
    "") die_usage "--overlay needs b1 or b2" ;;
    *) die_usage "--overlay takes b1 or b2, not $1" ;;
  esac
}

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

work=""
completion_temp=""
cleanup() {
  if [ -n "$completion_temp" ]; then
    rm -f -- "$completion_temp"
  fi
  if [ -n "$work" ]; then
    rm -rf -- "$work"
  fi
}
trap cleanup EXIT
trap 'echo "$PROG: interrupted" >&2; exit 130' INT
trap 'echo "$PROG: terminated" >&2; exit 143' TERM
trap 'exit 129' HUP

make_work() {
  work="$(mktemp -d "${TMPDIR:-/tmp}/pubdex-release.XXXXXX")"
  mkdir -p "$work/home" "$work/tmp" "$work/docker" "$work/helm/config" "$work/helm/cache" "$work/helm/data" \
    "$work/sbom"
}

need() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 was not found on PATH"
}

# --- tools -------------------------------------------------------------------

# helm with nothing of the caller's environment but PATH, and no kubeconfig:
# it renders and packages on this machine only. HELM_EPOCH holds the
# SOURCE_DATE_EPOCH of a helm package.
HELM=""
HELM_EPOCH=()
HELM_FACT=""
SYFT_FACT=""
epoch=""
run_helm() {
  env -i PATH="$PATH" LC_ALL=C HOME="$work/home" TMPDIR="$work/tmp" KUBECONFIG="$work/no-kubeconfig" \
    DOCKER_CONFIG="$work/docker" HELM_CONFIG_HOME="$work/helm/config" HELM_CACHE_HOME="$work/helm/cache" \
    HELM_DATA_HOME="$work/helm/data" ${HELM_EPOCH[@]+"${HELM_EPOCH[@]}"} "$HELM" "$@"
}

require_helm() {
  local shown=".tools/helm-$PINNED_HELM_VERSION/helm" have reported
  HELM="$(cluster_tool_path "$ROOT" helm)"
  if [ ! -f "$HELM" ] || [ ! -x "$HELM" ]; then
    fail "the pinned helm $PINNED_HELM_VERSION is not installed at $shown. Install it with:" "./setup_cluster_tools.sh"
  fi
  have="$(sha256sum < "$HELM")" || fail "could not read $shown"
  have="${have%% *}"
  if [ "$have" != "$PINNED_HELM_MEMBER_SHA256" ]; then
    fail "$shown has the SHA-256 $have, not the pinned $PINNED_HELM_MEMBER_SHA256." \
      "Remove .tools/helm-$PINNED_HELM_VERSION and install it again with: ./setup_cluster_tools.sh"
  fi
  reported="$(run_helm version --template '{{.Version}}' 2>/dev/null)" || reported=""
  if [ "$reported" != "v$PINNED_HELM_VERSION" ]; then
    fail "$shown reports the version '$reported', not the pinned v$PINNED_HELM_VERSION"
  fi
  HELM_FACT="$reported $have"
}

require_tools() {
  need git
  need tar
  need gzip
  need sha256sum
  need realpath
  need python3
  local tar_version
  tar_version="$(tar --version 2>/dev/null)" || tar_version=""
  [[ "${tar_version%%$'\n'*}" == *"GNU tar"* ]] || fail "tar is not GNU tar, which the reproducible source archive needs"
  need docker
  docker buildx version >/dev/null 2>&1 \
    || fail "the Docker buildx plugin was not found ('docker buildx version' fails); build_image.sh needs it"
  require_helm
}

# run_syft <arguments>: Syft from its pinned image, which is never pulled
# here, without network, as the calling user, with a read-only root file
# system and nothing of this machine but $work/sbom, at /sbom.
run_syft() {
  docker run --rm --pull never --network none --read-only --tmpfs /tmp:rw,mode=1777 \
    --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges \
    --env HOME=/tmp --env SYFT_CHECK_FOR_APP_UPDATE=false \
    --mount "type=bind,source=$work/sbom,target=/sbom" \
    "$PINNED_SYFT_IMAGE" "$@" < /dev/null
}

# The pinned SBOM tool is on this machine, has the pinned id and reports the
# pinned version; sets SYFT_FACT, its id and version output.
require_sbom_tool() {
  local id reported
  id="$(docker image inspect --format '{{.Id}}' "$PINNED_SYFT_IMAGE" < /dev/null 2>/dev/null)" || id=""
  if [ -z "$id" ]; then
    fail "the pinned SBOM tool, Syft $PINNED_SYFT_VERSION, is not on this machine. Pull it, by digest, with:" \
      "./release.sh setup"
  fi
  if [ "$id" != "$PINNED_SYFT_IMAGE_ID" ] && [ "$id" != "$PINNED_SYFT_INDEX_DIGEST" ]; then
    fail "the image of $PINNED_SYFT_IMAGE on this machine has the id $id, not the pinned $PINNED_SYFT_IMAGE_ID: it is not the pinned SBOM tool." \
      "Remove it with 'docker image rm $id' and pull it again with: ./release.sh setup"
  fi
  if ! run_syft version > "$work/syft-version" 2>&1; then
    sed 's/^/    /' "$work/syft-version" >&2
    fail "the pinned SBOM tool does not run"
  fi
  reported="$(sed -n 's/^Version:[[:space:]]*//p' "$work/syft-version" | sed -n 1p)"
  if [ "$reported" != "$PINNED_SYFT_VERSION" ]; then
    fail "the image of $PINNED_SYFT_IMAGE reports the version '$reported', not the pinned $PINNED_SYFT_VERSION"
  fi
  SYFT_FACT="$id"$'\n'"$(cat "$work/syft-version")"
}

# --- the checkout ------------------------------------------------------------

# Sets file_version, from VERSION.
read_file_version() {
  [ -r "$ROOT/VERSION" ] || fail "VERSION is missing from $ROOT"
  file_version="$(head -n 1 "$ROOT/VERSION" | tr -d '[:space:]')"
  [[ "$file_version" =~ $RELEASE_VERSION_RE ]] \
    || fail "VERSION must hold a version such as 0.2.0 (MAJOR.MINOR.PATCH, optionally -prerelease)"
  if is_test_version "$file_version"; then
    fail "VERSION holds $file_version, a version reserved for test releases, which VERSION never holds"
  fi
}

# Sets commit and revision: the checkout's commit, and the revision its
# images' labels record (DB/lib/release_identity.sh) -- with an overlay,
# which the commit never holds, <commit>-dirty.
read_checkout() {
  commit="$(checkout_commit "$ROOT")" || commit=""
  if ! [[ "$commit" =~ $COMMIT_RE ]]; then
    fail "$ROOT is not a Git checkout with a commit; the release script reads the checkout's files and commit with git"
  fi
  if [ -n "$overlay" ]; then
    revision="$commit-dirty"
  else
    revision="$(checkout_revision "$ROOT" "${IMAGE_CONTEXT_PATHSPECS[@]}")"
  fi
}

# A PubDex release <version>: the checkout has no changes and the annotated
# tag v<version> names its commit.
require_tagged_release() {
  local version="$1" tag="v$1" type tagged
  if [ "$revision" != "$commit" ]; then
    fail "PubDex $version is built only from a Git checkout without changes; this one is at revision $revision." \
      "Commit or remove the changes, or build a test release: ./release.sh build --test <name>"
  fi
  type="$(git -C "$ROOT" cat-file -t "refs/tags/$tag" 2>/dev/null)" || type=""
  if [ -z "$type" ]; then
    fail "PubDex $version is built only at the annotated tag $tag, which does not exist here"
  elif [ "$type" != tag ]; then
    fail "PubDex $version is built only at the annotated tag $tag, which is a lightweight tag here"
  fi
  tagged="$(git -C "$ROOT" rev-parse --verify --quiet "refs/tags/$tag^{commit}" 2>/dev/null)" || tagged=""
  if [ "$tagged" != "$commit" ]; then
    fail "the tag $tag names commit ${tagged:-(none)}, not this checkout's commit $commit"
  fi
}

# Sets version and test (yes or no) for a build or a check of <version>,
# or, with no version, for a build of a test release <name> or of VERSION.
resolve_version() {  # resolve_version build <name> | resolve_version check <version>
  if [ "$1" = build ]; then
    if [ -n "$2" ]; then
      if [[ "$file_version" == *-* ]]; then
        fail "a test release's version is <VERSION>-test.<name> for a VERSION without a pre-release part; VERSION holds $file_version"
      fi
      version="$file_version-test.$2"
      test=yes
    else
      version="$file_version"
      test=no
      require_tagged_release "$version"
    fi
  else
    version="$2"
    if is_test_version "$version"; then
      if ! [[ "$version" =~ $TEST_VERSION_RE ]] || [ "${version%-test.*}" != "$file_version" ]; then
        fail "$version is not a test release of this checkout's VERSION $file_version"
      fi
      test=yes
    else
      if [ "$version" != "$file_version" ]; then
        fail "the directory holds PubDex $version, but this checkout's VERSION is $file_version"
      fi
      test=no
      require_tagged_release "$version"
    fi
  fi
}

# list_files <list file>: the files the source archive holds, NUL separated,
# sorted: the checkout's (checkout_files), or with an overlay the copy's.
list_files() {
  if [ -n "$overlay" ]; then
    cp -- "$work/copy-files" "$1"
  else
    checkout_files "$1"
  fi
}

# checkout_files <list file>: the checkout's files, NUL separated, sorted:
# what git ls-files lists -- for a test release with the untracked files Git
# does not ignore -- that exists as a file or symlink.
checkout_files() {
  local list="$1" path
  local args=(ls-files -z)
  if [ "$test" = yes ]; then
    args+=(--cached --others --exclude-standard)
  fi
  git -C "$ROOT" "${args[@]}" > "$work/ls-files" 2>"$work/ls-files.err" \
    || fail "git ls-files failed in $ROOT: $(head -n 1 "$work/ls-files.err")"
  while IFS= read -r -d '' path; do
    if [ -L "$ROOT/$path" ] || [ -f "$ROOT/$path" ]; then
      printf '%s\0' "$path"
    fi
  done < "$work/ls-files" | sort -z -u > "$list"
  [ -s "$list" ] || fail "git ls-files lists no file in $ROOT"
}

source_date_epoch() {
  local epoch
  epoch="$(git -C "$ROOT" show --no-patch --format=%ct HEAD 2>/dev/null)" || epoch=""
  [[ "$epoch" =~ ^[0-9]+$ ]] || fail "cannot read the committer time of HEAD"
  printf '%s\n' "$epoch"
}

transform_version() {
  sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$SRC/DB/people_pubs/__init__.py"
}

# prepare_overlay: test release B1 or B2, as the header describes. Copies the
# checkout's files of a test release into $work/copy with their modes and
# extended attributes -- where the temporary directory's file system takes
# them -- sets every directory's mode to the checkout's, adds the overlay's schema changes
# and the new transform version there, writes the copy's file list to
# $work/copy-files and makes the copy the tree the build reads.
prepare_overlay() {
  local copy="$work/copy" templates=(compatible.sql) template path last=0 number reference="" name
  local suffix current
  [ "$overlay" = b2 ] && templates+=(incompatible.sql)
  for template in "${templates[@]}" transform-version-suffix; do
    [ -f "$ROOT/$OVERLAY_DIR/$template" ] || fail "the test-only overlay lacks $OVERLAY_DIR/$template"
  done
  suffix="$(head -n 1 "$ROOT/$OVERLAY_DIR/transform-version-suffix")"
  [[ "$suffix" =~ ^\+[0-9A-Za-z.-]+$ ]] \
    || fail "$OVERLAY_DIR/transform-version-suffix has to hold one suffix such as +test, not '$suffix'"
  test=yes
  checkout_files "$work/checkout-files"
  mkdir -p "$copy"
  if ! ( cd "$ROOT" && tar --create --file=- --xattrs --null --verbatim-files-from --no-recursion \
           --files-from="$work/checkout-files" ) \
       | ( cd "$copy" && tar --extract --file=- --xattrs --warning=no-xattr-write --preserve-permissions --no-same-owner ); then
    fail "the copy of the checkout for the overlay $overlay could not be made"
  fi
  chmod --reference="$ROOT" -- "$copy" || fail "cannot give the copy the checkout's directory mode"
  while IFS= read -r -d '' path; do
    chmod --reference="$ROOT/$path" -- "$copy/$path" || fail "cannot give $path in the copy the checkout's mode"
  done < <(cd "$copy" && find . -mindepth 1 -type d -printf '%P\0')
  shopt -s nullglob
  for path in "$copy"/DB/schema/[0-9][0-9][0-9][0-9]_*.sql; do
    number="${path##*/}"
    number=$((10#${number%%_*}))
    if [ "$number" -gt "$last" ]; then
      last="$number"
      reference="$path"
    fi
  done
  shopt -u nullglob
  [ -n "$reference" ] || fail "the checkout holds no schema change under DB/schema/ to number the overlay's after"
  : > "$work/copy-extra"
  for template in "${templates[@]}"; do
    last=$((last + 1))
    name="$(printf '%04d' "$last")_test_overlay_${template%.sql}.sql"
    cp -- "$ROOT/$OVERLAY_DIR/$template" "$copy/DB/schema/$name"
    chmod --reference="$reference" -- "$copy/DB/schema/$name"
    printf '%s\0' "DB/schema/$name" >> "$work/copy-extra"
    echo "    overlay: DB/schema/$name, from $OVERLAY_DIR/$template"
  done
  if [ "$overlay" = b2 ]; then
    python3 -I "$ROOT/$OVERLAY_DIR/application.py" "$copy" \
      || fail "the temporary B2 application overlay could not be applied"
  fi
  current="$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$copy/DB/people_pubs/__init__.py")"
  [[ "$current" =~ ^[0-9A-Za-z.+-]+$ ]] || fail "cannot read the transform version from DB/people_pubs/__init__.py"
  sed -i "s/^__version__ = \"[^\"]*\"\$/__version__ = \"$current$suffix\"/" "$copy/DB/people_pubs/__init__.py"
  SRC="$copy"
  [ "$(transform_version)" = "$current$suffix" ] || fail "the copy's transform version could not be set"
  echo "    overlay: the transform version $current$suffix in place of $current"
  cat "$work/checkout-files" "$work/copy-extra" | sort -z -u > "$work/copy-files"
}

# chart_field <helm show chart output> <field>: a top-level field, unquoted.
chart_field() {
  sed -n "/^$2: */{s///;s/^['\"]//;s/['\"]\$//;p;q;}" "$1"
}

# --- build -------------------------------------------------------------------

# make_source_archive <list> <version> <epoch> <output>
make_source_archive() {
  local list="$1" version="$2" epoch="$3" output="$4"
  if ! ( cd "$SRC" && env -i PATH="$PATH" LC_ALL=C tar --create --file=- --format=pax \
           --pax-option='exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime' \
           --sort=name --no-recursion --hard-dereference \
           --mtime="@$epoch" --owner=0 --group=0 --numeric-owner --mode='u+rw,go=rX,go-w' \
           --transform="s,^,pubdex-$version/,S" \
           --null --verbatim-files-from --files-from="$list" ) \
       | env -i PATH="$PATH" LC_ALL=C gzip -n -9 > "$output"; then
    fail "the source archive could not be written"
  fi
}

# make_chart_package <list> <version> <epoch> <destination directory>
make_chart_package() {
  local list="$1" version="$2" epoch="$3" destination="$4" copy="$work/chart/pubdex" path relative
  mkdir -p "$copy" "$work/package"
  while IFS= read -r -d '' path; do
    case "$path" in
      "$CHART_DIR"/*) relative="${path#"$CHART_DIR"/}" ;;
      *) continue ;;
    esac
    mkdir -p "$copy/$(dirname "$relative")"
    cp -P -- "$SRC/$path" "$copy/$relative"
  done < "$list"
  [ -f "$copy/Chart.yaml" ] || fail "the checkout's files hold no $CHART_DIR/Chart.yaml"
  local args=(package "$copy" --destination "$work/package")
  if [ "$test" = yes ]; then
    args+=(--version "$version" --app-version "$version")
  fi
  HELM_EPOCH=(SOURCE_DATE_EPOCH="$epoch")
  if ! run_helm "${args[@]}" > "$work/package.log" 2>&1; then
    HELM_EPOCH=()
    sed 's/^/    /' "$work/package.log" >&2
    fail "helm package failed"
  fi
  HELM_EPOCH=()
  [ -f "$work/package/pubdex-$version.tgz" ] || fail "helm package did not write pubdex-$version.tgz"
  mv "$work/package/pubdex-$version.tgz" "$destination/"
}

# The chart in the checkout has to carry VERSION as version and appVersion.
check_chart_source() {
  run_helm show chart "$SRC/$CHART_DIR" > "$work/chart-source.yaml" 2>"$work/chart-source.err" \
    || fail "helm show chart cannot read $CHART_DIR: $(head -n 1 "$work/chart-source.err")"
  local field value
  for field in version appVersion; do
    value="$(chart_field "$work/chart-source.yaml" "$field")"
    if [ "$value" != "$file_version" ]; then
      fail "$CHART_DIR/Chart.yaml has $field '$value', not VERSION's $file_version: one version names everything"
    fi
  done
}

image_id() {
  docker image inspect --format '{{.Id}}' "$1" < /dev/null 2>/dev/null
}

write_checksums() {
  local dir="$1" names=() name
  while IFS= read -r -d '' name; do
    names+=("$name")
  done < <(cd "$dir" && find . -mindepth 1 -maxdepth 1 -type f ! -name SHA256SUMS ! -name DIGESTS -printf '%f\0' | sort -z)
  ( cd "$dir" && sha256sum -- "${names[@]}" ) > "$dir/SHA256SUMS"
}

sbom_name() {  # sbom_name <role>
  if [ "$1" = application ]; then
    printf 'pubdex-%s-image.spdx.json\n' "$version"
  else
    printf 'pubdex-%s-postgres-image.spdx.json\n' "$version"
  fi
}

# make_sboms <directory with IMAGES>: an SBOM of each image, into it.
make_sboms() {
  local dir="$1" role reference name
  while read -r role reference _; do
    name="$(sbom_name "$role")"
    docker image save --output "$work/sbom/$role.tar" "$reference" < /dev/null \
      || fail "docker image save $reference failed"
    if ! run_syft "docker-archive:/sbom/$role.tar" --source-name "${reference%:*}" --source-version "$version" \
           --quiet --output "spdx-json@2.3=/sbom/$name" > "$work/syft.log" 2>&1; then
      sed 's/^/    /' "$work/syft.log" >&2
      fail "the SBOM tool failed on $reference"
    fi
    [ -s "$work/sbom/$name" ] || fail "the SBOM tool wrote no $name"
    mv -- "$work/sbom/$name" "$dir/$name"
    echo "    $name"
  done < "$dir/IMAGES"
}

# probe <image> <program> [arguments]: one command in a container of the
# image, which is removed again: no network, an unprivileged user, a
# read-only root file system, no capability.
probe() {
  local image="$1" program="$2"
  shift 2
  docker run --rm --pull never --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --user 65534:65534 --entrypoint "$program" "$image" "$@" < /dev/null
}

# What the application image reports of its client libraries, as JSON.
CLIENT_PROBE='import json, psycopg, psycopg_pool
from importlib.metadata import version
print(json.dumps({"psycopg": version("psycopg"), "psycopg-c": version("psycopg-c"),
                  "psycopg-pool": version("psycopg-pool"), "implementation": psycopg.pq.__impl__,
                  "libpq": psycopg.pq.version(), "libpq_build": psycopg.pq.__build_version__}))'

# fact <file> <command> [arguments]: the command's output into the facts
# file; a command that fails stops the step.
fact() {
  local file="$1"
  shift
  if ! "$@" > "$file" 2> "$work/fact.err"; then
    sed 's/^/    /' "$work/fact.err" >&2
    fail "could not read a fact for the provenance record: $*"
  fi
}

# image_facts <directory with IMAGES> <facts directory>: what only the images
# know, for DB/release_provenance.py.
image_facts() {
  local dir="$1" facts="$2" role reference doc
  mkdir -p "$facts"
  while read -r role reference _; do
    if [ ! -f "$work/sbom/$role.tar" ]; then
      docker image save --output "$work/sbom/$role.tar" "$reference" < /dev/null \
        || fail "docker image save $reference failed"
    fi
    fact "$facts/$role.config-digest" python3 -I "$ROOT/DB/verify_release.py" image-config "$work/sbom/$role.tar"
    doc=/usr/share/doc/pubdex
    [ "$role" = postgres ] && doc=/usr/share/doc/pubdex-postgres
    fact "$facts/$role.labels.json" docker image inspect --format '{{json .Config.Labels}}' "$reference"
    fact "$facts/$role.platform" docker image inspect --format '{{.Os}}/{{.Architecture}}' "$reference"
    fact "$facts/$role.build-packages.txt" probe "$reference" cat "$doc/build-packages.txt"
    if [ "$role" = application ]; then
      fact "$facts/application.client.json" probe "$reference" python -I -B -c "$CLIENT_PROBE"
      fact "$facts/application.libpq5.txt" probe "$reference" dpkg-query --show --showformat '${Version}\n' libpq5
    else
      fact "$facts/postgres.version.txt" probe "$reference" postgres --version
    fi
  done < "$dir/IMAGES"
}

# builder_facts <facts directory>: the build tools' versions; nothing that
# names this machine, its addresses or its paths reaches the record.
builder_facts() {
  local facts="$1"
  fact "$facts/builder.docker.txt" docker version --format '{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}'
  fact "$facts/builder.buildx.txt" docker buildx version
  fact "$facts/builder.buildkit.txt" docker buildx inspect
  fact "$facts/builder.store.txt" docker info --format \
    '{{.Driver}} {{range .DriverStatus}}{{if eq (index . 0) "driver-type"}}{{index . 1}}{{end}}{{end}}'
  fact "$facts/builder.git.txt" git --version
  fact "$work/tar-version" tar --version
  sed -n 1p "$work/tar-version" > "$facts/builder.tar.txt"
  fact "$work/gzip-version" gzip --version
  sed -n 1p "$work/gzip-version" > "$facts/builder.gzip.txt"
  printf '%s\n' "$HELM_FACT" > "$facts/builder.helm.txt"
  printf '%s\n' "$SYFT_FACT" > "$facts/builder.syft.txt"
}

# provenance write|check <dir> <facts directory> [arguments]
provenance() {
  local step="$1" dir="$2" facts="$3"
  shift 3
  python3 -I "$ROOT/DB/release_provenance.py" "$step" --root "$SRC" --dir "$dir" --facts "$facts" \
    --version "$version" --commit "$commit" --revision "$revision" --epoch "$epoch" "$@"
}

cmd_build() {
  local name="" out="" source_url=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --test)
        [ $# -ge 2 ] && [ -n "$2" ] || die_usage "--test needs a name"
        [[ "$2" =~ ^[a-z0-9]+$ ]] || die_usage "a test release's name is lower-case letters and digits: $2"
        name="$2"
        shift 2 ;;
      --overlay)
        overlay_argument "${2-}"
        shift 2 ;;
      --out)
        [ $# -ge 2 ] && [ -n "$2" ] || die_usage "--out needs a directory"
        out="$2"
        shift 2 ;;
      --source)
        [ $# -ge 2 ] && [ -n "$2" ] || die_usage "--source needs a URL"
        source_url="$2"
        shift 2 ;;
      *) die_usage "unknown argument to build: $1" ;;
    esac
  done
  if [ -n "$overlay" ] && [ -z "$name" ]; then
    die_usage "--overlay needs --test: only a test release carries the test-only overlay"
  fi

  local build_started
  build_started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  make_work
  step "Checking the tools and the checkout"
  require_tools
  require_sbom_tool
  read_file_version
  read_checkout
  resolve_version build "$name"
  if [ -n "$overlay" ]; then
    step "The copy of the checkout with the test-only overlay $overlay"
    prepare_overlay
  fi
  check_chart_source
  if [ -z "$out" ]; then
    out="$ROOT/dist/pubdex-$version"
  fi
  out="$(realpath -m -- "$out")"
  local root_real relative
  root_real="$(cd "$ROOT" && pwd -P)"
  relative="$(realpath -m --relative-base="$root_real" -- "$out")"
  if [ "$relative" = . ] || { [[ "$relative" != /* ]] && [[ "$relative" != dist/* ]]; }; then
    fail "the output directory $out lies in the checkout outside dist/: it would become part of the checkout's files"
  fi
  if [ -e "$out" ] && { [ ! -d "$out" ] || [ -n "$(ls -A -- "$out")" ]; }; then
    fail "$out exists and is not an empty directory; a build never writes into an earlier one"
  fi
  epoch="$(source_date_epoch)"
  echo "PubDex $version from $ROOT$([ -n "$overlay" ] && echo " with the test-only overlay $overlay, in a copy"), revision $revision$([ "$test" = yes ] && echo ', a test release')"
  echo "commit time $epoch (SOURCE_DATE_EPOCH), output $out"

  local staging="$work/out"
  mkdir -p "$staging"
  step "The source archive pubdex-$version-source.tar.gz"
  list_files "$work/files"
  make_source_archive "$work/files" "$version" "$epoch" "$staging/pubdex-$version-source.tar.gz"
  echo "    $(tr -cd '\0' < "$work/files" | wc -c) files"

  step "The chart package pubdex-$version.tgz"
  make_chart_package "$work/files" "$version" "$epoch" "$staging"

  step "The images $APP_LOCAL:$version and $POSTGRES_LOCAL:$version (./build_image.sh)"
  local build_args=(--tag "$APP_LOCAL:$version" --postgres-tag "$POSTGRES_LOCAL:$version")
  if [ -n "$source_url" ]; then
    build_args+=(--source "$source_url")
  fi
  if [ "$test" = yes ]; then
    build_args+=(--version "$version")
  else
    # Every layer afresh: Debian's packages from the archive as it is now,
    # never from an earlier build on this machine. A test release keeps the
    # cache.
    build_args+=(--no-cache)
  fi
  if [ -n "$overlay" ]; then
    # The copy's own build_image.sh, which finds no Git checkout above the
    # temporary directory, and so takes the revision it is given.
    build_args+=(--revision "$revision")
    GIT_CEILING_DIRECTORIES="$(cd "$work" && pwd -P)" "$SRC/build_image.sh" "${build_args[@]}" \
      || fail "build_image.sh failed"
  else
    "$ROOT/build_image.sh" "${build_args[@]}" || fail "build_image.sh failed"
  fi
  local app_id postgres_id
  app_id="$(image_id "$APP_LOCAL:$version")" || fail "the image $APP_LOCAL:$version was not found after the build"
  postgres_id="$(image_id "$POSTGRES_LOCAL:$version")" \
    || fail "the image $POSTGRES_LOCAL:$version was not found after the build"
  printf 'application %s:%s %s\npostgres %s:%s %s\n' "$APP_LOCAL" "$version" "$app_id" \
    "$POSTGRES_LOCAL" "$version" "$postgres_id" > "$staging/IMAGES"

  step "The SBOMs (Syft $PINNED_SYFT_VERSION, SPDX 2.3 JSON)"
  make_sboms "$staging"

  step "The provenance record pubdex-$version-provenance.json"
  image_facts "$staging" "$work/facts"
  builder_facts "$work/facts"
  if ! provenance write "$staging" "$work/facts" --started "$build_started" \
         --finished "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$work/provenance.log" 2>&1; then
    sed 's/^/    /' "$work/provenance.log" >&2
    fail "the provenance record could not be written"
  fi

  step "SHA256SUMS"
  write_checksums "$staging"
  sed 's/^/    /' "$staging/SHA256SUMS"

  mkdir -p "$(dirname "$out")"
  if [ -d "$out" ]; then
    rmdir -- "$out"
  fi
  mv -- "$staging" "$out"
  verify_directory "$out"
  echo
  echo "PASS: PubDex $version built and verified in $out ($((SECONDS - started)) s)."
  if [ "$test" = yes ]; then
    echo "A test release: never published. Remove its images with:"
    echo "  docker image rm $APP_LOCAL:$version $POSTGRES_LOCAL:$version"
  fi
}

# --- verify ------------------------------------------------------------------

# Sets dir_version from the source archive's name in <dir>.
directory_version() {
  local dir="$1" archives=()
  shopt -s nullglob
  archives=("$dir"/pubdex-*-source.tar.gz)
  shopt -u nullglob
  [ "${#archives[@]}" -eq 1 ] || fail "$dir holds ${#archives[@]} source archives (pubdex-<version>-source.tar.gz), not one"
  dir_version="${archives[0]##*/}"
  dir_version="${dir_version#pubdex-}"
  dir_version="${dir_version%-source.tar.gz}"
  [[ "$dir_version" =~ $RELEASE_VERSION_RE ]] || fail "the source archive's name holds no version: ${archives[0]##*/}"
}

check_checksums() {
  local dir="$1" line name previous="" listed=() present=()
  [ -f "$dir/SHA256SUMS" ] && [ ! -L "$dir/SHA256SUMS" ] || fail "$dir has no SHA256SUMS"
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[0-9a-f]{64}\ \ ([A-Za-z0-9._+-]+)$ ]] \
      || fail "SHA256SUMS: a line is not '<SHA-256>  <file name>': $line"
    name="${BASH_REMATCH[1]}"
    if [ -n "$previous" ] && ! [[ "$previous" < "$name" ]]; then
      fail "SHA256SUMS is not sorted by name, or names a file twice: $name"
    fi
    case "$name" in SHA256SUMS|DIGESTS) fail "SHA256SUMS lists $name, which it never covers" ;; esac
    listed+=("$name")
    previous="$name"
  done < "$dir/SHA256SUMS"
  while IFS= read -r -d '' name; do
    [ -f "$dir/$name" ] && [ ! -L "$dir/$name" ] || fail "$dir holds $name, which is not a regular file"
    case "$name" in SHA256SUMS|DIGESTS) continue ;; esac
    present+=("$name")
  done < <(cd "$dir" && find . -mindepth 1 -maxdepth 1 -printf '%f\0' | sort -z)
  if [ "${listed[*]-}" != "${present[*]-}" ]; then
    fail "SHA256SUMS lists '${listed[*]-}', but the directory holds '${present[*]-}'"
  fi
  ( cd "$dir" && sha256sum --check --strict --quiet SHA256SUMS ) \
    || fail "a file of $dir does not match its SHA-256 in SHA256SUMS"
  echo "    ok  SHA256SUMS covers ${#listed[@]} files, each matches"
}

label() {  # label <image> <name>
  docker image inspect --format "{{index .Config.Labels \"$2\"}}" "$1" < /dev/null 2>/dev/null
}

check_images() {
  local dir="$1" expected actual role reference id roles=() want platform value
  [ -f "$dir/IMAGES" ] || fail "$dir has no IMAGES"
  expected="$(printf 'application %s:%s\npostgres %s:%s' "$APP_LOCAL" "$version" "$POSTGRES_LOCAL" "$version")"
  actual="$(sed 's/ [^ ]*$//' "$dir/IMAGES")"
  if [ "$actual" != "$expected" ]; then
    fail "IMAGES has to name, in this order: $(tr '\n' ';' <<<"$expected")"
  fi
  while read -r role reference id; do
    [[ "$id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "IMAGES: not an image id for $role: $id"
    actual="$(image_id "$reference")" || fail "the image $reference is not on this machine"
    if [ "$actual" != "$id" ]; then
      fail "$reference is image $actual, not $id, which IMAGES records: the tag has moved since the build"
    fi
    platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$reference" < /dev/null 2>/dev/null)" \
      || platform=""
    [ "$platform" = "$PLATFORM" ] || fail "$reference is for '$platform', not $PLATFORM"
    value="$(label "$reference" org.opencontainers.image.version)" || value=""
    [ "$value" = "$version" ] || fail "$reference is labelled version '$value', not $version"
    value="$(label "$reference" org.opencontainers.image.revision)" || value=""
    [ "$value" = "$revision" ] || fail "$reference is labelled revision '$value', but the checkout is at $revision"
    if [ "$role" = application ]; then
      want="$(transform_version)"
      value="$(label "$reference" pubdex.transform-version)" || value=""
      [ -n "$want" ] && [ "$value" = "$want" ] \
        || fail "$reference is labelled transform version '$value', not the checkout's '$want'"
    fi
    roles+=("$role")
    echo "    ok  $role image $reference $id: version $version, revision $revision, $PLATFORM"
  done < "$dir/IMAGES"
  [ "${#roles[@]}" -eq 2 ] || fail "IMAGES has to hold exactly two lines"
}

check_source_archive() {
  local dir="$1" archive="$1/pubdex-$version-source.tar.gz" prefix="pubdex-$version/" path a t problems=0
  gzip -t "$archive" 2>/dev/null || fail "${archive##*/} is not a valid gzip file"
  list_files "$work/verify-files"
  tr '\0' '\n' < "$work/verify-files" | sed "s,^,$prefix," > "$work/expected-members"
  tar -tzf "$archive" > "$work/members" 2>"$work/members.err" || fail "${archive##*/} cannot be listed"
  if ! cmp -s "$work/expected-members" "$work/members"; then
    { diff "$work/expected-members" "$work/members" || true; } | head -n 20 | sed 's/^/    /' >&2
    fail "${archive##*/} does not hold exactly the checkout's files, in order (expected < > archive)"
  fi
  tar -tvzf "$archive" > "$work/members-long" 2>/dev/null || fail "${archive##*/} cannot be listed"
  if grep -qv '^[-l]' "$work/members-long"; then
    fail "${archive##*/} holds a member that is neither a file nor a symlink"
  fi
  mkdir -p "$work/archive"
  tar -xzf "$archive" -C "$work/archive" --no-same-owner || fail "${archive##*/} cannot be extracted"
  while IFS= read -r -d '' path; do
    a="$work/archive/$prefix$path"
    t="$SRC/$path"
    if [ -L "$t" ]; then
      if [ ! -L "$a" ] || [ "$(readlink -- "$a")" != "$(readlink -- "$t")" ]; then
        echo "    differs: $path (symlink)" >&2
        problems=$((problems + 1))
      fi
    elif [ -L "$a" ] || ! cmp -s -- "$t" "$a"; then
      echo "    differs: $path" >&2
      problems=$((problems + 1))
    elif { [ -x "$t" ] && [ ! -x "$a" ]; } || { [ ! -x "$t" ] && [ -x "$a" ]; }; then
      echo "    differs: $path (executable bit)" >&2
      problems=$((problems + 1))
    fi
  done < "$work/verify-files"
  [ "$problems" -eq 0 ] || fail "${archive##*/}: $problems files differ from the checkout's"
  echo "    ok  ${archive##*/}: $(wc -l < "$work/members") files under $prefix, each equal to the checkout's"
}

check_chart_package() {
  local dir="$1" package="$1/pubdex-$version.tgz" field value member relative problems=0
  [ -f "$package" ] || fail "$dir has no chart package pubdex-$version.tgz"
  run_helm show chart "$package" > "$work/chart.yaml" 2>"$work/chart.err" \
    || fail "helm show chart cannot read ${package##*/}: $(head -n 1 "$work/chart.err")"
  value="$(chart_field "$work/chart.yaml" name)"
  [ "$value" = pubdex ] || fail "${package##*/} holds the chart '$value', not pubdex"
  for field in version appVersion; do
    value="$(chart_field "$work/chart.yaml" "$field")"
    [ "$value" = "$version" ] || fail "${package##*/} has $field '$value', not $version"
  done
  mkdir -p "$work/package-files"
  tar -xzf "$package" -C "$work/package-files" --no-same-owner || fail "${package##*/} cannot be extracted"
  tar -tzf "$package" > "$work/package-members" || fail "${package##*/} cannot be listed"
  grep -qx 'pubdex/LICENSE' "$work/package-members" || fail "${package##*/} carries no LICENSE"
  while IFS= read -r member; do
    case "$member" in
      pubdex/*) relative="${member#pubdex/}" ;;
      *) fail "${package##*/} holds $member outside pubdex/" ;;
    esac
    [ "$relative" = Chart.yaml ] && continue
    if [ ! -f "$SRC/$CHART_DIR/$relative" ] || ! cmp -s -- "$SRC/$CHART_DIR/$relative" "$work/package-files/$member"; then
      echo "    differs: $CHART_DIR/$relative" >&2
      problems=$((problems + 1))
    fi
  done < "$work/package-members"
  [ "$problems" -eq 0 ] || fail "${package##*/}: $problems files differ from the chart in the checkout"
  if ! run_helm lint --strict "$package" > "$work/lint.log" 2>&1; then
    sed 's/^/    /' "$work/lint.log" >&2
    fail "helm lint --strict fails for ${package##*/}"
  fi
  if ! run_helm template pubdex "$package" --namespace pubdex --set secrets.name=pubdex-credentials \
         > "$work/rendered.yaml" 2>"$work/rendered.err"; then
    sed 's/^/    /' "$work/rendered.err" >&2
    fail "helm template fails for ${package##*/}"
  fi
  grep -qx 'kind: Deployment' "$work/rendered.yaml" || fail "helm template of ${package##*/} renders no Deployment"
  echo "    ok  ${package##*/}: version and appVersion $version, $(wc -l < "$work/package-members") files, LICENSE, lint and template pass"
}

check_provenance() {
  local dir="$1" role
  for role in application postgres; do
    [ -f "$dir/$(sbom_name "$role")" ] || fail "$dir has no SBOM $(sbom_name "$role")"
  done
  [ -f "$dir/pubdex-$version-provenance.json" ] || fail "$dir has no provenance record pubdex-$version-provenance.json"
  image_facts "$dir" "$work/verify-facts"
  if ! provenance check "$dir" "$work/verify-facts" > "$work/provenance-check.log" 2>&1; then
    sed 's/^/    /' "$work/provenance-check.log" >&2
    fail "the SBOMs or the provenance record disagree with the checkout, the images or the directory"
  fi
  echo "    ok  $(sbom_name application) and $(sbom_name postgres): SPDX 2.3, each its image's packages"
  echo "    ok  pubdex-$version-provenance.json agrees with the checkout, the images, IMAGES and the files"
}

# verify_directory <dir>: every check of verify; sets version.
verify_directory() {
  local dir="$1"
  step "Verifying $dir"
  directory_version "$dir"
  read_file_version
  read_checkout
  resolve_version check "$dir_version"
  epoch="$(source_date_epoch)"
  check_checksums "$dir"
  check_images "$dir"
  check_source_archive "$dir"
  check_chart_package "$dir"
  check_provenance "$dir"
  echo "    PubDex $version: the directory is verified against the checkout at revision $revision."
}

cmd_verify() {
  local dir="" given=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --overlay)
        overlay_argument "${2-}"
        shift 2 ;;
      -*) die_usage "unknown argument to verify: $1" ;;
      *)
        [ "$given" = 0 ] && [ -n "$1" ] || die_usage "verify takes one directory"
        dir="$1"
        given=1
        shift ;;
    esac
  done
  [ "$given" = 1 ] || die_usage "verify takes one directory"
  [ -d "$dir" ] || fail "no directory $dir"
  dir="$(cd "$dir" && pwd)"
  make_work
  require_tools
  if [ -n "$overlay" ]; then
    directory_version "$dir"
    is_test_version "$dir_version" \
      || fail "$dir holds PubDex $dir_version; only a test release is built with the test-only overlay"
    read_file_version
    read_checkout
    step "The copy of the checkout with the test-only overlay $overlay"
    prepare_overlay
  fi
  verify_directory "$dir"
  echo
  echo "PASS: $dir verified ($((SECONDS - started)) s)."
}

# --- publish -----------------------------------------------------------------

# registry_holds <reference>: true when the registry holds a manifest of this
# name and tag, false when it explicitly reports that it holds none; every
# other answer stops the step.
registry_holds() {
  local reference="$1"
  if docker manifest inspect "$reference" > "$work/manifest.out" 2> "$work/manifest.err"; then
    return 0
  fi
  if grep -Eiq 'unauthorized|denied|forbidden|authentication|toomanyrequests|timeout|timed out|tls|certificate|connection|no such host|name unknown|(^|[^0-9])(429|500|502|503|504)([^0-9]|$)' \
       "$work/manifest.err"; then
    sed 's/^/    /' "$work/manifest.err" >&2
    fail "the lookup of $reference failed, so its absence is not established. Resolve the lookup before publishing."
  fi
  if ! grep -Eiq 'manifest unknown|MANIFEST_UNKNOWN' "$work/manifest.err"; then
    sed 's/^/    /' "$work/manifest.err" >&2
    fail "the registry did not explicitly report $reference unknown ('manifest unknown'), so its absence is not established."
  fi
  return 1
}

# registry_digest <reference>: sets held to the digest of the manifest the
# registry holds under this name and tag, which docker buildx imagetools
# inspect prints on its Digest: line.
registry_digest() {
  local reference="$1"
  if ! docker buildx imagetools inspect "$reference" > "$work/imagetools.out" 2> "$work/imagetools.err"; then
    sed 's/^/    /' "$work/imagetools.err" >&2
    fail "the registry's digest of $reference could not be read, so whether it is the verified image is not established. Resolve the lookup before publishing."
  fi
  held="$(sed -n 's/^Digest:[[:space:]]*\(sha256:[0-9a-f]\{64\}\)[[:space:]]*$/\1/p' "$work/imagetools.out")"
  [[ "$held" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail "docker buildx imagetools inspect $reference did not name one digest, so whether it is the verified image is not established."
}

# recorded_digests <image> <repository>: the digests the local image records
# for the repository (docker image inspect, RepoDigests), which a push of it
# from this machine records; one per line.
recorded_digests() {
  local line
  while IFS= read -r line; do
    if [[ "$line" == "$2@sha256:"* ]] && [[ "${line#*@}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      echo "${line#*@}"
    fi
  done < <(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$1" 2>/dev/null || true)
}

# plan_push <reference> <repository> <verified image>: decides, before any
# push, what publish does with one image, and sets push and digest. The
# registry explicitly reports the reference unknown: push=yes. It holds the
# reference with a digest that the verified image records for the
# repository: that very image, published by an earlier run, so push=no and
# digest is the registry's. Any other digest, or none recorded, stops the
# step: the version is spent.
plan_push() {
  local reference="$1" repository="$2" image="$3" held="" recorded
  if ! registry_holds "$reference"; then
    echo "    ok  $reference: the registry reports manifest unknown; to be pushed"
    push=yes digest=""
    return 0
  fi
  registry_digest "$reference"
  recorded="$(recorded_digests "$image" "$repository")"
  local spent="Version $version is spent: a published version is never overwritten. Leave the registry and the pushed tag as they are, and make the next version (RELEASING.md, step 7, If publish fails)."
  if [ -z "$recorded" ]; then
    fail "$reference already exists, as $held, and the verified image $image records no digest for $repository, so nothing shows that it is this image." \
      "$spent"
  fi
  if ! grep -qxF -- "$held" <<< "$recorded"; then
    fail "$reference already exists, as $held, which is another image: the verified image $image records ${recorded//$'\n'/ and } for $repository." \
      "$spent"
  fi
  echo "    ok  $reference: the registry holds $held, which the verified image records for $repository:"
  echo "        published from this machine by an earlier run; it is not pushed again"
  push=no digest="$held"
}

# tag_for_registry <local tag> <reference> <image id>: a tag that already
# exists is never moved.
tag_for_registry() {
  local local_tag="$1" reference="$2" id="$3" existing
  if existing="$(image_id "$reference")"; then
    [ "$existing" = "$id" ] || fail "the local tag $reference already names image $existing, not $id; it is never moved"
  else
    docker tag "$local_tag" "$reference" || fail "docker tag $local_tag $reference failed"
  fi
  existing="$(image_id "$reference")" || existing=""
  [ "$existing" = "$id" ] || fail "$reference is image '$existing' after tagging, not $id"
}

# push_image <reference> <repository>: pushes and prints the registry's
# digest of the image.
push_image() {
  local reference="$1" repository="$2" pushed="" recorded="" line
  if ! docker push "$reference" 2>&1 | tee "$work/push.log"; then
    fail "docker push $reference failed"
  fi
  pushed="$(sed -n 's/^.*: digest: \(sha256:[0-9a-f]\{64\}\) size: [0-9]*$/\1/p' "$work/push.log" | tail -n 1)"
  while IFS= read -r line; do
    if [ -n "$recorded" ] && [ "$recorded" != "$line" ]; then
      fail "$reference has more than one digest in $repository: $recorded and $line"
    fi
    recorded="$line"
  done < <(recorded_digests "$reference" "$repository")
  if [ -n "$pushed" ] && [ -n "$recorded" ] && [ "$pushed" != "$recorded" ]; then
    fail "docker push reported $pushed for $reference, but the image records $recorded"
  fi
  digest="${recorded:-$pushed}"
  [ -n "$digest" ] || fail "no digest was reported for $reference; check the registry before publishing anything else"
}

cmd_publish() {
  local dir="" prefix=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --repository-prefix)
        [ $# -ge 2 ] && [ -n "$2" ] || die_usage "--repository-prefix needs <registry>/<namespace>"
        prefix="$2"
        shift 2 ;;
      -*) die_usage "unknown argument to publish: $1" ;;
      *)
        [ -z "$dir" ] || die_usage "publish takes one directory"
        dir="$1"
        shift ;;
    esac
  done
  [ -n "$dir" ] || die_usage "publish needs the directory build wrote"
  [ -n "$prefix" ] || die_usage "publish needs --repository-prefix <registry>/<namespace>; there is no default"
  [[ "$prefix" =~ $PREFIX_RE ]] \
    || die_usage "--repository-prefix takes <registry>/<namespace>, lower case, without a tag or a trailing slash: $prefix"
  [ -d "$dir" ] || fail "no directory $dir"
  dir="$(cd "$dir" && pwd)"

  directory_version "$dir"
  if is_test_version "$dir_version"; then
    fail "$dir_version is a test release's version: a test release is never published"
  fi
  if [ -e "$dir/DIGESTS" ] || [ -L "$dir/DIGESTS" ]; then
    fail "$dir holds DIGESTS: it has been published already"
  fi
  make_work
  require_tools
  verify_directory "$dir"

  local app_reference="$prefix/$APP_REPOSITORY:$version" postgres_reference="$prefix/$POSTGRES_REPOSITORY:$version"
  local app_id postgres_id
  app_id="$(sed -n 's/^application [^ ]* //p' "$dir/IMAGES")"
  postgres_id="$(sed -n 's/^postgres [^ ]* //p' "$dir/IMAGES")"

  # Both images are decided before the first push, so a refusal never
  # follows a push.
  local push digest postgres_push postgres_digest app_push app_digest
  step "Checking what each repository holds of $version"
  plan_push "$postgres_reference" "$prefix/$POSTGRES_REPOSITORY" "$POSTGRES_LOCAL:$version"
  postgres_push="$push" postgres_digest="$digest"
  plan_push "$app_reference" "$prefix/$APP_REPOSITORY" "$APP_LOCAL:$version"
  app_push="$push" app_digest="$digest"

  step "Tagging the verified images"
  tag_for_registry "$POSTGRES_LOCAL:$version" "$postgres_reference" "$postgres_id"
  tag_for_registry "$APP_LOCAL:$version" "$app_reference" "$app_id"
  echo "    $postgres_reference $postgres_id"
  echo "    $app_reference $app_id"

  if [ "$postgres_push" = yes ]; then
    step "Pushing $postgres_reference"
    push_image "$postgres_reference" "$prefix/$POSTGRES_REPOSITORY"
    postgres_digest="$digest"
    echo "    published $postgres_reference@$postgres_digest"
  fi
  if [ "$app_push" = yes ]; then
    step "Pushing $app_reference"
    push_image "$app_reference" "$prefix/$APP_REPOSITORY"
    app_digest="$digest"
    echo "    published $app_reference@$app_digest"
  fi

  completion_temp="$(mktemp "$dir/.DIGESTS.XXXXXX")" || fail "cannot create the completion record"
  printf '%s@%s\n%s@%s\n' "$app_reference" "$app_digest" "$postgres_reference" "$postgres_digest" > "$completion_temp" \
    || fail "cannot write the completion record; publication can be retried"
  cmp -s "$completion_temp" <(printf '%s@%s\n%s@%s\n' "$app_reference" "$app_digest" "$postgres_reference" "$postgres_digest") \
    || fail "incomplete completion record; publication can be retried"
  # A hard link installs the complete file atomically and refuses any existing
  # destination, including one another publisher completed in the meantime.
  ln -- "$completion_temp" "$dir/DIGESTS" || fail "cannot install DIGESTS; an existing record is never overwritten"
  rm -f -- "$completion_temp"
  completion_temp=""
  echo
  echo "PASS: PubDex $version published. $dir/DIGESTS:"
  sed 's/^/  /' "$dir/DIGESTS"
  echo "Log out of the registry, make both packages public where the registry made them private, and check"
  echo "the published images without credentials, by these digests."
}

# --- setup -------------------------------------------------------------------

cmd_setup() {
  [ $# -eq 0 ] || die_usage "setup takes no argument"
  make_work
  need docker
  step "Pulling the pinned SBOM tool, Syft $PINNED_SYFT_VERSION, by digest"
  docker pull "$PINNED_SYFT_IMAGE" < /dev/null || fail "docker pull $PINNED_SYFT_IMAGE failed"
  require_sbom_tool
  echo
  echo "PASS: the pinned SBOM tool is in place: $PINNED_SYFT_IMAGE, image ${SYFT_FACT%%$'\n'*}."
}

# --- main --------------------------------------------------------------------

if [ $# -eq 0 ]; then
  die_usage "a step is needed: setup, build, verify or publish"
fi
command="$1"
shift
case "$command" in
  setup) cmd_setup "$@" ;;
  build) cmd_build "$@" ;;
  verify) cmd_verify "$@" ;;
  publish) cmd_publish "$@" ;;
  -h|--help)
    [ $# -eq 0 ] || die_usage "unexpected argument: $1"
    usage ;;
  *) die_usage "unknown step: $command" ;;
esac
