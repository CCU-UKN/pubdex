# shellcheck shell=bash
# What identifies a release of PubDex (DEPLOYMENT.md, "What identifies a
# deployment"); shared by build_image.sh, which labels the image it builds
# with the release version and the revision of its checkout, and
# run_stack_acceptance.sh, which checks those labels and, with --release, the
# Compose file the image carries. Sourced, not run.
#
# A deployment is the application image and its compose.yaml. The image
# carries the schema bootstrap (DB/init/) and the compose.yaml it was built
# with; its labels name the release version and the commit it was built from,
# and that compose.yaml names the release's image by its version tag. No file
# of a checkout takes part when the stack runs.
#
# Git is only asked, never changed: no command below writes the index, as
# git status otherwise may to refresh it, or anything else under .git/. Each
# function runs git in the environment of its caller, which decides what
# that environment holds.

# A release version, as VERSION holds it: MAJOR.MINOR.PATCH, optionally with
# a -prerelease part.
RELEASE_VERSION_RE='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$'
# A commit id in full, as Git prints it: SHA-1 or SHA-256, lower case.
COMMIT_RE='^([0-9a-f]{40}|[0-9a-f]{64})$'

# checkout_commit <root>
#
# The commit that HEAD of the checkout at <root> is at, in full. Fails outside
# a Git checkout, and when git is missing.
checkout_commit() {
  git -C "$1" rev-parse --verify --quiet HEAD 2>/dev/null
}

# checkout_changes <root> <pathspec>...
#
# What makes the working tree at <root> differ from its commit, one line each:
# whatever git status reports -- a modified, staged, deleted or untracked
# file, in its short format -- then, as "!! <path>", each file that Git
# ignores and that matches a <pathspec>: a file a consumer of the checkout
# takes in although no commit holds it. Prints nothing when the working tree
# is the commit; fails when Git cannot say.
checkout_changes() {
  local root="$1" status ignored
  shift
  status="$(git -C "$root" --no-optional-locks status --porcelain --untracked-files=normal 2>/dev/null)" || return 1
  ignored="$(git -C "$root" ls-files --others --ignored --exclude-standard -- "$@" 2>/dev/null)" || return 1
  if [ -n "$status" ]; then printf '%s\n' "$status"; fi
  if [ -n "$ignored" ]; then printf '%s\n' "$ignored" | sed 's/^/!! /'; fi
}

# checkout_revision <root> <pathspec>...
#
# The revision of the checkout at <root>, as the image's revision label
# records it: the commit; "<commit>-dirty" when checkout_changes reports a
# difference, or cannot say; "unknown" outside a Git checkout. A local change
# therefore never passes for the commit.
checkout_revision() {
  local root="$1" commit changes
  shift
  if ! command -v git >/dev/null 2>&1 || ! commit="$(checkout_commit "$root")" || [ -z "$commit" ]; then
    echo unknown
  elif changes="$(checkout_changes "$root" "$@")" && [ -z "$changes" ]; then
    echo "$commit"
  else
    echo "$commit-dirty"
  fi
}

# reference_tag <image reference>
#
# The tag part of an image reference -- 0.1.0 in registry.example.org:5000/
# team/pubdex:0.1.0@sha256:<digest> -- or nothing when it has none. A
# registry's port is not a tag.
reference_tag() {
  local name="${1%%@*}"
  if [[ "${name##*/}" == *:* ]]; then
    printf '%s\n' "${name##*:}"
  fi
}

# compose_default_image <compose file> [<variable>]
#
# The image the Compose file names for an unset variable, PEOPLE_PUBS_IMAGE
# unless another is given -- ghcr.io/ccu-ukn/pubdex:0.1.0 for
# ${PEOPLE_PUBS_IMAGE:-ghcr.io/ccu-ukn/pubdex:0.1.0}, the application image,
# and with PEOPLE_PUBS_POSTGRES_IMAGE the PostgreSQL image -- or nothing when
# it names none, or more than one.
compose_default_image() {
  local found variable="${2:-PEOPLE_PUBS_IMAGE}"
  found="$(sed -n "s/^[[:space:]]*image: [\$]{${variable}:-\([^}]*\)}[[:space:]]*\$/\1/p" "$1")" || return 1
  if [ -n "$found" ] && [ "$(wc -l <<<"$found" | tr -d '[:space:]')" = 1 ]; then
    printf '%s\n' "$found"
  fi
}
