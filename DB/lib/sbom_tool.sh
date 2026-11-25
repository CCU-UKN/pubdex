# shellcheck shell=bash
# The pinned SBOM tool of release.sh: Syft, which writes the software bill of
# materials of each image in the SPDX 2.3 JSON format. Sourced, not run.
#
# Syft runs from its container image, by digest, never from a binary or an
# image found by name: ./release.sh setup pulls exactly PINNED_SYFT_IMAGE,
# and release.sh build refuses to build anything unless the image of that
# reference is on this machine with the id below and reports the pinned
# version. It runs without network, as the calling user, on the images that
# docker save writes, and only reads them.
#
# Where the pins come from. On 2026-11-18 the tag list of the repository
# docker.io/anchore/syft on Docker Hub named v1.57.2, pushed 2026-11-16, as
# the newest release, the tag latest naming the same image index digest
# PINNED_SYFT_INDEX_DIGEST. Pulled by that digest the same day, without
# credentials, the index gave the linux/amd64 image whose configuration
# digest is PINNED_SYFT_IMAGE_ID -- the image id in Docker's classic image
# store; the containerd image store gives an image the id of the index it
# was pulled by, PINNED_SYFT_INDEX_DIGEST -- and that image reported version
# 1.57.2. The Syft project documents no signature of its images, so the pin
# rests on the digest alone, as the base images' pins do. A new version needs
# all four values changed together, checked the same way.
readonly PINNED_SYFT_VERSION=1.57.2
readonly PINNED_SYFT_IMAGE=docker.io/anchore/syft:v1.57.2@sha256:e97e20b6e6d1699c60296b66ab337941724f9bd06ac279d893eda89601fc4b2a
readonly PINNED_SYFT_INDEX_DIGEST=sha256:e97e20b6e6d1699c60296b66ab337941724f9bd06ac279d893eda89601fc4b2a
readonly PINNED_SYFT_IMAGE_ID=sha256:ace7e0c4149ee5e2054a622c4cad153cab792cf62ca1b8b1d375ff1db53c3820
