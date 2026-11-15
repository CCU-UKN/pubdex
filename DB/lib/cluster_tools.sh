# shellcheck shell=bash
# The pinned helm, kubectl and kind for the Helm chart's checks and its
# acceptance test on disposable kind clusters, and the kind node images those
# clusters run. setup_cluster_tools.sh installs the three tools under .tools/
# from exactly these files; the scripts that use them source this file and
# call each tool by the path cluster_tool_path prints, never one found on
# PATH. Sourced, not run.
#
# For each tool: its version, the linux/amd64 download, the file's SHA-256 and
# the largest size accepted for it (about one and a half times the pinned
# file's size). A new version needs all of them changed together, with the
# hash checked as described below for the current pins.
#
# Where the hashes come from. On the date given below each file was
# downloaded twice, once with curl and once by setup_cluster_tools.sh, and
# both copies matched the SHA-256 published next to the file on the same
# HTTPS server (the .sha256sum or .sha256 file named below):
#
#   - helm 4.3.1, released 2026-10-15, on 2026-11-10: get.helm.sh, no
#     redirect; the hash of helm-v4.3.1-linux-amd64.tar.gz.sha256sum, which
#     the release notes state as well. The archive's detached
#     signature, which the Helm project publishes with its release on GitHub
#     rather than on get.helm.sh, verified that day against key
#     208D D36E D5BB 3745 A167  43A4 C7C6 FBB5 B91C 1155 from the Helm
#     project's KEYS file; this setup does not repeat that check, the pinned
#     hash covers the same file. Only one member of the archive is used:
#     PINNED_HELM_MEMBER, a regular file whose SHA-256 was read from that
#     verified archive the same day.
#   - kubectl 1.36.6, on 2026-11-11: dl.k8s.io, no redirect; the hash of the
#     .sha256 file next to the binary. 1.36.6 is the release the dl.k8s.io
#     version marker stable-1.36.txt named that day. Its minor lies between
#     those of the two node images below, so it supports both clusters:
#     kubectl supports a server within one minor of its own.
#   - kind 0.33.0, released 2026-08-26, on 2026-11-10: kind.sigs.k8s.io/dl,
#     the download address the kind project documents. It redirects, that
#     day twice, to the release downloads the kind project keeps on GitHub,
#     so this one file still comes from GitHub's release storage, whichever
#     forge hosts this repository. The hash of kind-linux-amd64.sha256sum,
#     fetched the same way, which the release's own record of the file
#     states as well.
#
# The node images are the ones kind 0.33.0's release notes name, by digest,
# as kind requires. PINNED_KIND_NODE_IMAGE is Kubernetes 1.37.0, the newest
# release the chart supports: on 2026-11-10 the registry's tag v1.37.0
# resolved to this same image index, which holds a linux/amd64 image.
# PINNED_KIND_FLOOR_NODE_IMAGE is Kubernetes 1.35.8, of the oldest minor
# release the chart supports (Chart.yaml's kubeVersion): on 2026-11-11 the
# release notes named this digest for v1.35.8, and the registry's tag v1.35.8
# resolved to this same image index, which holds a linux/amd64 image.
readonly PINNED_HELM_VERSION=4.3.1
readonly PINNED_HELM_URL=https://get.helm.sh/helm-v4.3.1-linux-amd64.tar.gz
readonly PINNED_HELM_SHA256=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b
readonly PINNED_HELM_MAX_BYTES=31457280
readonly PINNED_HELM_MEMBER=linux-amd64/helm
readonly PINNED_HELM_MEMBER_SHA256=b66cb19f2b31f9d427632ea6c4b4a1ff89b74ecaeaee323cd166da41a7af53bd
readonly PINNED_HELM_MEMBER_MAX_BYTES=100663296

readonly PINNED_KUBECTL_VERSION=1.36.6
readonly PINNED_KUBECTL_URL=https://dl.k8s.io/release/v1.36.6/bin/linux/amd64/kubectl
readonly PINNED_KUBECTL_SHA256=29818b6729dc9262b692307470d0558d027725e738be5dd47c760261868a2634
readonly PINNED_KUBECTL_MAX_BYTES=94371840

readonly PINNED_KIND_VERSION=0.33.0
readonly PINNED_KIND_URL=https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64
readonly PINNED_KIND_SHA256=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d
readonly PINNED_KIND_MAX_BYTES=16777216

readonly PINNED_KIND_NODE_IMAGE=kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
readonly PINNED_KIND_FLOOR_NODE_IMAGE=kindest/node:v1.35.8@sha256:07b2536e30b803ed61d1677a79df6115f798ce64c80f9e22f6ed45afd09323c0

# cluster_tool_path <root> <tool>
#
# The path at which setup_cluster_tools.sh installs <tool> (helm, kubectl or
# kind) in the checkout at <root>: <root>/.tools/<tool>-<version>/<tool>.
# Status 2, and nothing printed, for any other name.
cluster_tool_path() {
  local version
  case "$2" in
    helm) version="$PINNED_HELM_VERSION" ;;
    kubectl) version="$PINNED_KUBECTL_VERSION" ;;
    kind) version="$PINNED_KIND_VERSION" ;;
    *) return 2 ;;
  esac
  printf '%s/.tools/%s-%s/%s\n' "$1" "$2" "$version" "$2"
}
