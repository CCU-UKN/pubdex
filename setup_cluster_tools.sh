#!/usr/bin/env bash
# Installs the helm, kubectl and kind that the Helm chart's checks and its
# acceptance test on a disposable kind cluster use -- the versions
# DB/lib/cluster_tools.sh pins, for Linux on x86_64 -- under .tools/ in this
# checkout, and writes nothing anywhere else:
#
#   ./setup_cluster_tools.sh
#
# Each tool goes to .tools/<tool>-<version>/<tool>. The scripts that use one
# call it by that path (cluster_tool_path in DB/lib/cluster_tools.sh), never a
# helm, kubectl or kind found on PATH or preinstalled on a CI runner. For each
# tool:
#
#   1. When the file is in place already, its SHA-256 is checked against the
#      pin, and nothing is downloaded. A file that does not match is reported
#      and left as it is, neither used nor replaced: remove its directory and
#      run the script again.
#   2. Otherwise DB/fetch_verified.py downloads the pinned URL over HTTPS,
#      within the pinned size, into a temporary directory under .tools/, and
#      keeps the file only when its SHA-256 is the pinned one. kubectl and
#      kind are that file. From helm's archive the script reads the one
#      pinned member -- a regular file under exactly that name, present once,
#      within its pinned size and with its pinned SHA-256 -- and writes no
#      other member, link or path.
#   3. The file is made executable and moved into place.
#
# Then each tool reports its version, which has to be the pinned one. They
# run with an environment of their own: KUBECONFIG names a file that does not
# exist, and HOME and helm's configuration, cache and data directories lie in
# the temporary directory, so no kubeconfig of the caller's is read and
# nothing is written outside .tools/. None of them contacts a cluster.
#
# Linux on x86_64 only: the kind node image and the deployment's images are
# linux/amd64. It needs python3 (for DB/fetch_verified.py) and sha256sum, and
# HTTPS access to get.helm.sh, dl.k8s.io and kind.sigs.k8s.io; the last
# redirects kind's download to the kind project's release storage on GitHub
# (DB/lib/cluster_tools.sh). Directories of earlier versions under .tools/ are
# left alone and can be removed. Exit status: 0 when all three tools are in
# place and report their pinned versions, 1 when anything fails, 2 for a
# usage error.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS="$ROOT/.tools"
PINS="DB/lib/cluster_tools.sh"
PROG="setup_cluster_tools.sh"

usage() {
  cat <<'USAGE'
usage: setup_cluster_tools.sh

Installs the helm, kubectl and kind that DB/lib/cluster_tools.sh pins under
.tools/<tool>-<version>/ in this checkout: each file downloaded over HTTPS by
DB/fetch_verified.py and kept only when its SHA-256 is the pinned one, or,
when it is there already, checked against that hash. Linux on x86_64 only.

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
  echo "$PROG: $*" >&2
  exit 1
}

os="$(uname -s)"
machine="$(uname -m)"
if [ "$os" != Linux ] || [ "$machine" != x86_64 ]; then
  fail "only Linux on x86_64 is supported, and this is $os on $machine"
fi
for program in python3 sha256sum; do
  command -v "$program" >/dev/null 2>&1 || fail "$program was not found"
done

[ -f "$ROOT/$PINS" ] || fail "$PINS was not found"
# Only the file sets the pins, never the caller's environment.
unset "${!PINNED_@}"
# shellcheck source=DB/lib/cluster_tools.sh
source "$ROOT/$PINS" || fail "$PINS could not be read"

# check_pin <name> <pattern>: refuses a pin that is unset, empty or not of
# the form <pattern> (an extended regular expression).
check_pin() {
  [[ "${!1:-}" =~ $2 ]] || fail "$1 in $PINS is missing or malformed: '${!1:-}'"
}
SHA256_PATTERN='^[0-9a-f]{64}$'
SIZE_PATTERN='^[1-9][0-9]{0,9}$'
for tool in helm kubectl kind; do
  pin="PINNED_${tool^^}"
  check_pin "${pin}_VERSION" '^[0-9]+\.[0-9]+\.[0-9]+$'
  check_pin "${pin}_URL" '^https://[A-Za-z0-9.-]+/[^[:space:]]+$'
  check_pin "${pin}_SHA256" "$SHA256_PATTERN"
  check_pin "${pin}_MAX_BYTES" "$SIZE_PATTERN"
  version_name="${pin}_VERSION"
  url_name="${pin}_URL"
  [[ "${!url_name}" == *"v${!version_name}"[/-]* ]] \
    || fail "${pin}_URL in $PINS does not name version ${!version_name}: ${!url_name}"
done
# A relative path whose every part starts with a letter, digit, - or _:
# no ".", "..", empty part or leading /.
check_pin PINNED_HELM_MEMBER '^[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$'
check_pin PINNED_HELM_MEMBER_SHA256 "$SHA256_PATTERN"
check_pin PINNED_HELM_MEMBER_MAX_BYTES "$SIZE_PATTERN"
check_pin PINNED_KIND_NODE_IMAGE '^kindest/node:v[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}$'
check_pin PINNED_KIND_FLOOR_NODE_IMAGE '^kindest/node:v[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}$'

# not_a_plain <path> <what>: refuses a path that is a symbolic link, or that
# exists and is not of the kind <what> ("directory" or "file").
not_a_plain() {
  local test=-d
  [ "$2" = file ] && test=-f
  if [ -L "$1" ] || { [ -e "$1" ] && ! [ "$test" "$1" ]; }; then
    fail "${1#"$ROOT/"} exists and is not a $2 (a symbolic link is not followed)"
  fi
}

not_a_plain "$TOOLS" directory
mkdir -p -- "$TOOLS"
work="$(mktemp -d "$TOOLS/.setup.XXXXXX")"
trap 'rm -rf -- "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

sha256_of() {
  local line
  line="$(sha256sum < "$1")" || fail "could not read ${1#"$ROOT/"}"
  printf '%s' "${line%% *}"
}

# take_member <archive> <output>: writes the pinned member of helm's archive,
# and nothing else, to <output>, which must not exist; Python's tarfile reads
# the archive, and no member is extracted by name or path.
take_member() {
  python3 -I -B - "$1" "$PINNED_HELM_MEMBER" "$PINNED_HELM_MEMBER_SHA256" \
    "$PINNED_HELM_MEMBER_MAX_BYTES" "$2" <<'PY'
import hashlib
import os
import posixpath
import sys
import tarfile

archive, member, want, limit, output = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]


def refuse(reason):
    print(f"setup_cluster_tools.sh: {os.path.basename(archive)}: {reason}; nothing was taken from it",
          file=sys.stderr)
    sys.exit(1)


def same_path(name):
    """Whether an archive entry's name would land on the member when unpacked."""
    return posixpath.normpath(name.lstrip("/")) == member


try:
    with tarfile.open(archive, "r:gz") as tar:
        found = [info for info in tar if same_path(info.name)]
        if not found:
            refuse(f"it has no member {member}")
        if len(found) > 1:
            refuse(f"it has {len(found)} entries for {member}")
        info = found[0]
        if info.name != member:
            refuse(f"its entry for {member} is named {info.name!r}")
        if not info.isreg():
            refuse(f"its member {member} is not a regular file")
        if info.size > limit:
            refuse(f"its member {member} is larger than {limit} bytes")
        source = tar.extractfile(info)
        digest, size = hashlib.sha256(), 0
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            while True:
                chunk = source.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    refuse(f"its member {member} is larger than {limit} bytes")
                digest.update(chunk)
                out.write(chunk)
except (tarfile.TarError, EOFError, OSError) as exc:
    refuse(f"it cannot be read: {exc}")
if digest.hexdigest() != want:
    os.unlink(output)
    refuse(f"its member {member} has the SHA-256 {digest.hexdigest()}, not the pinned {want}")
PY
}

# install <tool>: puts the pinned <tool> in place, or checks the one there.
declare -A how
install() {
  local tool="$1" pin="PINNED_${1^^}"
  local version_name="${pin}_VERSION" url_name="${pin}_URL" sha_name="${pin}_SHA256" max_name="${pin}_MAX_BYTES"
  local version="${!version_name}" url="${!url_name}" sha="${!sha_name}" max="${!max_name}"
  local dir="$TOOLS/$tool-$version" want="$sha" have file
  local target="$dir/$tool"
  [ "$tool" = helm ] && want="$PINNED_HELM_MEMBER_SHA256"
  not_a_plain "$dir" directory
  not_a_plain "$target" file
  if [ -f "$target" ]; then
    have="$(sha256_of "$target")"
    [ "$have" = "$want" ] || fail ".tools/$tool-$version/$tool has the SHA-256 $have, not the pinned $want;" \
      "it was left as it is: remove .tools/$tool-$version and run $PROG again"
    chmod 0755 -- "$target"
    how[$tool]="in place, SHA-256 checked"
    return
  fi
  echo "==> $tool $version: downloading $url"
  file="$work/$tool-download"
  python3 -I -B "$ROOT/DB/fetch_verified.py" url --sha256 "$sha" --max-bytes "$max" "$url" "$file" \
    || fail "$tool $version could not be downloaded"
  have="$(sha256_of "$file")"
  [ "$have" = "$sha" ] || fail "the download of $tool $version has the SHA-256 $have, not the pinned $sha"
  if [ "$tool" = helm ]; then
    take_member "$file" "$work/helm-member" || fail "helm $version could not be taken from its archive"
    file="$work/helm-member"
  fi
  chmod 0755 -- "$file"
  mkdir -p -- "$dir"
  mv -T -- "$file" "$target"
  how[$tool]="downloaded, SHA-256 checked"
}

# run_tool <path> <argument>...: runs a tool without the caller's environment.
run_tool() {
  env -i PATH="$PATH" LC_ALL=C HOME="$work/home" KUBECONFIG="$work/no-kubeconfig" \
    HELM_CONFIG_HOME="$work/helm/config" HELM_CACHE_HOME="$work/helm/cache" \
    HELM_DATA_HOME="$work/helm/data" "$@"
}

# reported <tool>: the version <tool> reports, as vX.Y.Z.
reported() {
  local path output name version extra
  path="$(cluster_tool_path "$ROOT" "$1")"
  case "$1" in
    helm) set -- helm version --template '{{.Version}}' ;;
    kubectl) set -- kubectl version --client ;;
    kind) set -- kind version ;;
  esac
  output="$(run_tool "$path" "${@:2}" 2>"$work/stderr")" \
    || fail "$1 does not run: $(head -c 2000 "$work/stderr")"
  case "$1" in
    helm) version="$output" ;;
    kubectl) version="$(sed -n 's/^Client Version: //p' <<<"$output")" ;;
    kind) read -r name version extra <<<"$output"
          [ "$name" = kind ] || version="" ;;
  esac
  printf '%s' "$version"
}

for tool in helm kubectl kind; do
  install "$tool"
done

for tool in helm kubectl kind; do
  version_name="PINNED_${tool^^}_VERSION"
  version="$(reported "$tool")"
  [ "$version" = "v${!version_name}" ] \
    || fail "$tool reports the version '$version', not the pinned v${!version_name}"
done

echo "$PROG: the pinned cluster tools are in place and report their pinned versions:"
for tool in helm kubectl kind; do
  version_name="PINNED_${tool^^}_VERSION"
  printf '  %-8s v%-8s %-31s (%s)\n' "$tool" "${!version_name}" ".tools/$tool-${!version_name}/$tool" \
    "${how[$tool]}"
done
echo "  kind node images: $PINNED_KIND_NODE_IMAGE"
echo "                    $PINNED_KIND_FLOOR_NODE_IMAGE (the oldest supported release)"
