#!/usr/bin/env bash
# The offline checks of the Helm chart in charts/pubdex/, stage 8 of
# run_ci_suite.sh. They need the pinned helm and .venv, and no cluster,
# Docker or network:
#
#   ./run_chart_checks.sh
#
#   1. helm has to be the one DB/lib/cluster_tools.sh pins, as
#      ./setup_cluster_tools.sh installs it under .tools/: its SHA-256 and
#      the version it reports are checked before it runs anything else;
#   2. helm lint --strict lints the chart with its own values, with every
#      values file of the checks (charts/tests/values/) and with every example
#      (charts/pubdex/examples/), each for the oldest and the newest
#      Kubernetes release the chart supports;
#   3. pytest runs charts/tests/ (charts/tests/conftest.py). The chart is
#      rendered with helm template for each of those files. The tests check
#      the parsed manifests: Pod Security "restricted", the read-only roots,
#      the credentials, which arrive only as files of the operator's Secret,
#      each container receiving its own keys, labels, selectors, names and
#      namespaces, images, probes, the settings and their checksum, the
#      ingestion CronJobs, the demonstration and the attached pod, that no
#      roster reaches the chart, the NetworkPolicies and the Ingress, and
#      NOTES.txt. They also give the chart values it has to refuse and check
#      each message.
#
# Every helm call runs with a cleared environment. HOME, TMPDIR, helm's
# configuration, cache and data directories and DOCKER_CONFIG lie in a new
# temporary directory, and KUBECONFIG names a file that does not exist there,
# so no kubeconfig of the caller's is read. Lint, template and a client-side
# dry run render on this machine and contact no cluster. pytest runs in a
# cleared environment as well, as in DB/run_tests.sh, with the network guard
# and the skip policy of DB/tests. Nothing is written outside that temporary
# directory, which is removed at the end, also after a failure or an
# interrupt.
#
# Needs .venv (./setup_venv.sh) and the pinned helm (./setup_cluster_tools.sh).
# Exit status: 0 when every check passes, 1 when a check fails or something
# it needs is missing, 2 for a usage error.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PROG="run_chart_checks.sh"
PYTHON="$ROOT/.venv/bin/python"
CHART="charts/pubdex"
# The oldest and the newest Kubernetes release the chart supports: the floor
# of kubeVersion in Chart.yaml, and the release of the pinned kind node image
# (DB/lib/cluster_tools.sh). charts/tests/chart.py names the same two.
KUBE_VERSIONS=(1.35.0 1.37.0)

usage() {
  cat <<'USAGE'
usage: run_chart_checks.sh

Checks the Helm chart in charts/pubdex/ offline, with the pinned helm:
helm lint --strict with its own values, the values files of the checks and
the examples, for the oldest and the newest supported Kubernetes release;
then the tests in charts/tests/, which render the chart and check the
manifests and the values it refuses. Needs .venv (./setup_venv.sh) and the
pinned helm (./setup_cluster_tools.sh); no cluster, Docker or network.

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

if [ ! -x "$PYTHON" ]; then
  fail "no interpreter at .venv/bin/python. Create it from the repository root with:" "./setup_venv.sh"
fi
if ! "$PYTHON" -c 'import pytest, yaml' >/dev/null 2>&1; then
  fail ".venv/bin/python lacks the development requirements (pytest, PyYAML). Remove .venv and create it again with:" \
    "./setup_venv.sh"
fi
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum was not found on PATH"

# shellcheck source=DB/lib/cluster_tools.sh
source "$ROOT/DB/lib/cluster_tools.sh"
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

work="$(mktemp -d "${TMPDIR:-/tmp}/pubdex-chart-checks.XXXXXX")"
cleanup() {
  rm -rf -- "$work"
}
trap cleanup EXIT
trap 'echo "$PROG: interrupted" >&2; exit 130' INT
trap 'echo "$PROG: terminated" >&2; exit 143' TERM
trap 'exit 129' HUP
mkdir -p "$work/home" "$work/tmp" "$work/docker" "$work/helm/config" "$work/helm/cache" "$work/helm/data"

# helm with nothing of the caller's environment but PATH.
run_helm() {
  env -i PATH="$PATH" LC_ALL=C HOME="$work/home" TMPDIR="$work/tmp" KUBECONFIG="$work/no-kubeconfig" \
    DOCKER_CONFIG="$work/docker" HELM_CONFIG_HOME="$work/helm/config" HELM_CACHE_HOME="$work/helm/cache" \
    HELM_DATA_HOME="$work/helm/data" "$HELM" "$@"
}

version="$(run_helm version --template '{{.Version}}' 2>/dev/null)" || version=""
if [ "$version" != "v$PINNED_HELM_VERSION" ]; then
  fail "$helm_shown reports the version '$version', not the pinned v$PINNED_HELM_VERSION"
fi
echo "helm $version ($helm_shown, SHA-256 matches the pin)"

shopt -s nullglob
values_files=(charts/tests/values/*.yaml "$CHART"/examples/*.yaml)
shopt -u nullglob

echo "==> helm lint --strict, Kubernetes ${KUBE_VERSIONS[*]}"
for kube in "${KUBE_VERSIONS[@]}"; do
  for file in "" "${values_files[@]}"; do
    args=()
    shown="the chart's own values"
    if [ -n "$file" ]; then
      args=(--values "$file")
      shown="$file"
    fi
    if ! run_helm lint --strict "$CHART" --kube-version "$kube" ${args[@]+"${args[@]}"} > "$work/lint.log" 2>&1; then
      sed 's/^/    /' "$work/lint.log" >&2
      fail "helm lint --strict failed for Kubernetes $kube with $shown"
    fi
    echo "    ok  Kubernetes $kube, $shown"
  done
done

echo "==> rendered manifests and refused values (charts/tests)"
status=0
env -i PATH="$PATH" HOME="$work/home" TMPDIR="$work/tmp" LANG="${LANG:-C.UTF-8}" PYTHONDONTWRITEBYTECODE=1 \
  PEOPLE_PUBS_SKIP_DOTENV=1 "$PYTHON" -m pytest -p no:cacheprovider charts/tests || status=$?
if [ "$status" -ne 0 ]; then
  fail "the chart tests failed (pytest exit status $status)"
fi

echo "PASS: the Helm chart's offline checks passed."
