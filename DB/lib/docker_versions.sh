# shellcheck shell=bash
# The Docker versions the deployment needs, and how a reported version is
# compared with them; shared by run_ci_suite.sh, run_stack_acceptance.sh and
# run_operator_acceptance.sh.
# Sourced, not run.
#
# Docker Compose 2.24.0 is the first release that starts compose.yaml as it is
# written. Its health checks set start_interval: Compose before 2.20.2 rejects
# the file ("Additional property start_interval is not allowed"), and 2.20.2
# to 2.23.x accept it but leave it out of the container's health check, which
# then probes at the full interval while the service starts.
#
# Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2: one requirement,
# to build the image as to run it. The read service has no authentication and
# no TLS, and compose.yaml publishes it on 127.0.0.1 so that only this host
# reaches it. That holds only where the Engine drops packets that arrive from
# other hosts for a port published on loopback, as Docker documents for its
# releases since 28.0.0 (docs.docker.com, "Port publishing and mapping"):
# before, a host on the same layer-2 network could reach such a port
# (moby/moby#45610, fixed by moby/moby#49325 in 28.0.0 and not backported to
# an earlier branch). 28.0.1 rather than 28.0.0, whose firewall rules needed
# the kernel's ipset modules, which 28.0.1 no longer does. In 28.2.0 to
# 28.3.2 a reload of firewalld removes the filter again (CVE-2025-54388,
# fixed in 28.3.3). Engines from 25.0 (API 1.44, the first
# to run start_interval) start the stack and pass its health checks, and the
# Dockerfile uses nothing their builder lacks, but they are not supported:
# whether the stack starts says nothing about who else can reach it. The
# check reads the version only, so a distribution's package of an older
# release counts as that release, whatever it backported.
COMPOSE_MINIMUM=2.24.0
ENGINE_MINIMUM=28.0.1
ENGINE_EXCLUDED_FROM=28.2.0
ENGINE_EXCLUDED_UNTIL=28.3.3
ENGINE_SUPPORTED="28.0.1 or later, except 28.2.0 to 28.3.2"

# version_at_least <version> <minimum>
#
# Whether <version>, as Docker or Compose reports it, is a release at least
# <minimum> (major.minor.patch). Status 0 when it is, 1 when it is older, 2
# when it is not a version that can be compared.
#
#   - A leading "v" is ignored (v2.24.0), and so is build metadata after a
#     "+", as distribution packages append it (2.24.6+ds1-0ubuntu1).
#   - A pre-release -- anything after a "-": 2.24.0-rc.1, 2.24.6-desktop.1 --
#     ranks just below the release of the same number, as in Semantic
#     Versioning: 2.24.0-rc.1 is older than 2.24.0, 2.24.1-rc.1 is not.
#   - Every major version is compared by number, so Compose 5.6.0 is later
#     than 2.24.0.
#   - Anything else is not a version: empty, "dev", a missing patch number
#     (2.24), a fourth number or other trailing text (2.24.0.1, 2.24.0rc1).
version_at_least() {
  local version="${1#v}" prerelease=0 have want i
  version="${version%%+*}"
  if [[ "$version" == *-* ]]; then
    [ -n "${version#*-}" ] || return 2
    prerelease=1
    version="${version%%-*}"
  fi
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 2
  [[ "$2" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 2
  IFS=. read -r -a have <<<"$version"
  IFS=. read -r -a want <<<"$2"
  for i in 0 1 2; do
    if ((10#${have[i]} > 10#${want[i]})); then return 0; fi
    if ((10#${have[i]} < 10#${want[i]})); then return 1; fi
  done
  return "$prerelease"
}

# version_problem <program> <version> <minimum>
#
# Prints why <program> at <version> falls short of <minimum> -- too old, or
# a version that cannot be compared -- and prints nothing when it does not.
version_problem() {
  local status=0
  version_at_least "$2" "$3" || status=$?
  case "$status" in
    0) ;;
    1) echo "$1 $2 is too old" ;;
    *) if [ -z "$2" ]; then
         echo "$1 does not report its version"
       else
         echo "$1 reports the version '$2', which is not a release number that can be compared"
       fi ;;
  esac
}

# engine_problem <version>
#
# Prints why Docker Engine <version> is not one the deployment supports --
# too old, one of the excluded releases, or a version that cannot be
# compared -- and prints nothing when it is.
engine_problem() {
  local problem
  problem="$(version_problem "Docker Engine" "$1" "$ENGINE_MINIMUM")"
  if [ -z "$problem" ] && version_at_least "$1" "$ENGINE_EXCLUDED_FROM" \
      && ! version_at_least "$1" "$ENGINE_EXCLUDED_UNTIL"; then
    problem="Docker Engine $1 is one of the releases in which a firewalld reload opens ports published on 127.0.0.1 to other hosts (CVE-2025-54388)"
  fi
  [ -z "$problem" ] || echo "$problem"
}
