# Task 2c — release automation, schema upgrade and upgrade tests

**PubDex 0.2.0 candidate, verified locally on 2026-11-24. The final
candidate's clean-clone run, public CI and publication remain pending.**

This tree implements the release automation and the documented upgrade
path, with upgrade and rollback tests on Compose and Kubernetes. All eleven
stages of the complete suite passed locally. The record documents were
written afterwards and passed stage 1 and the document checks. No PubDex
0.2.0 artifacts are published yet, and external acceptance is not asserted.

Project page: [PubDex at NLnet](https://nlnet.nl/project/PubDex/). Earlier
results are recorded in [`TASK_2A.md`](TASK_2A.md), the images and Compose
deployment, and [`TASK_2B.md`](TASK_2B.md), the Helm chart and cluster tests.

## The agreed task

> 2c. Release automation will produce tagged source and container
> artifacts, checksums, and an SBOM or equivalent provenance record. We'll
> document versioning, compatibility and what release notes must say about
> schema and ingestion changes, then show upgrade and rollback working
> between two test releases.

The implementation, the local suite, a clean-clone run of the final
candidate, public CI, the published PubDex release and external acceptance
are separate results. Only the first two are established here. A subsequent
record update will identify the published source and the runs that tested it.

## What task 2c adds

- **One PubDex version**, 0.2.0, for [`VERSION`](VERSION), both images,
  Compose's image defaults and the Helm chart's `version` and `appVersion`.
  [`VERSIONING.md`](VERSIONING.md) defines patch and minor releases,
  configuration compatibility, the supported upgrades and rollbacks, the
  release record and the platform rules. Until 1.0.0, a patch release
  changes neither the schema nor ingestion and preserves existing
  configuration; a minor release may change them and must describe it.
- **The schema upgrade**, [`DB/people_pubs/schema.py`](DB/people_pubs/schema.py),
  with numbered SQL files in [`DB/schema/`](DB/schema/), applied once, in
  order, each in its own transaction and recorded in the database. The
  baseline, [`DB/init/`](DB/init/), remains the schema of 0.1.0. Compose
  and both chart modes run the same upgrade command. The read service and
  ingestion refuse an unsupported schema and accept compatible newer ones.
- **Release automation**, [`release.sh`](release.sh): build and verify
  the complete artifact set, with a separate publication command for the
  maintainer. The source archive and chart package are reproducible with
  the same packaging tools; both images have SBOMs and a provenance record.
- **Two suite stages**: the release rehearsal,
  [`run_release_rehearsal.sh`](run_release_rehearsal.sh), stage 10, and the
  Compose upgrades, [`run_upgrade_acceptance.sh`](run_upgrade_acceptance.sh),
  stage 11. Stage 9, [`run_cluster_acceptance.sh`](run_cluster_acceptance.sh),
  also upgrades between test releases and from published 0.1.0, with the
  schema upgrade, on disposable clusters.
- **Operator and maintainer instructions.** [`DEPLOYMENT.md`](DEPLOYMENT.md)
  and [`KUBERNETES.md`](KUBERNETES.md) each give *Upgrading to a new
  release*. [`RELEASING.md`](RELEASING.md) gives the release procedure,
  schema-change rules and release-note duties. Their command tables name
  the suite steps that run each block, or state that it is unrun by the
  suite; stage 1 checks those mappings.

## Acceptance matrix

| Required | What was built and where documented | What tests it | Established result |
|---|---|---|---|
| Release automation will produce tagged source … artifacts | `release.sh build` requires a clean checkout at its annotated `v<version>` tag and writes the source archive; [`RELEASING.md`](RELEASING.md), *Build and verify at the tag* | stage 1, [`test_release_script.py`](DB/tests/test_release_script.py), tests tag and clean-checkout refusals with stubs; stage 10 builds and verifies the source twice under a test version | local test archives verified and byte-identical; the final candidate's build at an annotated tag is pending |
| … and container artifacts | the application and PostgreSQL images, one `linux/amd64` manifest each, built by [`build_image.sh`](build_image.sh); `release.sh publish` publishes them separately from building; [`RELEASING.md`](RELEASING.md) | stages 9–11 build and verify both test images and run them in deployments; stage 1 covers publication and its failure recovery with stubs | both local images verified and exercised; registry publication of 0.2.0 pending |
| checksums | `SHA256SUMS` covers the source archive, chart package, both SBOMs, provenance and local image inventory; [`releases/README.md`](releases/README.md) defines the committed record | stage 10 verifies each build; stage 1 tests changed, missing and unexpected files, including [`test_verify_release.py`](DB/tests/test_verify_release.py) | complete local artifact directories verified; five-file download completeness tested with synthetic inputs |
| an SBOM or equivalent provenance record | both: SPDX 2.3 JSON per image and `pubdex-provenance/2`; [`RELEASING.md`](RELEASING.md), *What a PubDex release is*, and [`DB/release_provenance.py`](DB/release_provenance.py) | stages 9–11 generate and verify them; stage 10 compares two builds; stage 1, [`test_release_provenance.py`](DB/tests/test_release_provenance.py) and the release-script and download-verifier tests | image contents, build inputs and config digests checked locally; published-image verification pending |
| document versioning, compatibility | [`VERSIONING.md`](VERSIONING.md): one version, patch/minor promises, configuration compatibility, upgrade floor, rollback rules and platforms | stage 1, [`test_release_version.py`](DB/tests/test_release_version.py), [`test_version_guard.py`](DB/tests/test_version_guard.py), [`test_release_record.py`](DB/tests/test_release_record.py); stages 9 and 11 exercise schema compatibility | documented; version identity and published-version guard tested; configuration compatibility is a documented rule, without an automated compatibility check |
| what release notes must say about schema and ingestion changes | [`CHANGELOG.md`](CHANGELOG.md), three fixed headings per PubDex version; [`RELEASING.md`](RELEASING.md), *The release notes* | stage 1 runs [`DB/check_release_notes.py`](DB/check_release_notes.py); [`test_release_notes.py`](DB/tests/test_release_notes.py) tests contradictory notes | notes agree with schema changes, transform version, preceding release and floor; configuration-change completeness is not checked |
| show upgrade and rollback working between two test releases | A is this checkout; B1 adds a compatible schema change and transform version; B2 adds an incompatible schema change; [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), the stage 9 and 11 sections | stage 9, steps 22–26, A→B1 and image rollback in external mode; stage 11, steps 1–5, A→B1 and image rollback, A→B2 and rollback by restore | passed locally on Kubernetes 1.37.0 and Compose; the restore test proves structural incompatibility and recovery of original data values |

## Release automation and artifact set

`release.sh setup` pulls the pinned Syft image. `release.sh build` writes
the following set and finishes by verifying it; `release.sh verify`
checks an existing directory without building anything.

| Artifact | Identity and verification |
|---|---|
| `pubdex-<version>-source.tar.gz` | the tagged source, or the working tree for a test release, with normalized file modes, ownership and timestamps |
| Application and PostgreSQL images | version and revision labels; local tags and image ids in `IMAGES`; config digests in provenance; registry references in `DIGESTS` after publication |
| `pubdex-<version>.tgz` | the packaged Helm chart, with its licence and examples, linted and rendered from the package file |
| `pubdex-<version>-image.spdx.json`, `pubdex-<version>-postgres-image.spdx.json` | SPDX 2.3 JSON from Syft 1.57.2, pinned by digest and run without network; checked against each image's packages and versions |
| `pubdex-<version>-provenance.json` | source revision, base-image digests, PostgreSQL source hash, dependency locks, build files and tools, compiler packages, client libraries, image labels, ids and config digests |
| `SHA256SUMS` | hashes of the file artifacts and `IMAGES`; excludes itself and the later `DIGESTS` |

A PubDex release builds both images without Docker's build cache, so its
Debian packages come from Debian's archive at build time. Test releases
use the cache. Stage 10 builds a test release twice on one machine: the
source archive and chart package must be byte-identical, with the same GNU
tar, gzip and pinned helm. The SBOMs and provenance must agree apart from
build times, image ids, config digests and the values derived from them.
The images themselves are not byte-identical on rebuild.

Publication is an explicit maintainer step. It pushes PostgreSQL first,
then the application, and writes `DIGESTS` only when both digests are
known. An interrupted publication can resume: an existing version counts
as already published only if its registry digest is one the verified local
image records for that repository. Another image under the version stops
publication before any push. These paths, including failure while writing
the completion record, are tested with stubs; no suite stage publishes.

The repository's [`releases/`](releases/README.md) holds the authoritative
release record. A forge's release page carries five files for download:
source archive, chart package, two SBOMs and provenance. `IMAGES` stays
local. [`DB/verify_release.py`](DB/verify_release.py) requires all five
downloads and checks them against the committed checksum file. Its image
check hashes the actual config bytes of a saved image and compares them
with provenance, independently of the image store's image id. Format 1
remains readable; only its classic-store records support that comparison.
The download-and-pull procedure in [`VERSIONING.md`](VERSIONING.md),
*Checking a PubDex release*, is not run by the suite against a release page.

Both CI adapters run the same four steps: `setup_venv.sh`,
`setup_cluster_tools.sh`, `release.sh setup` and `run_ci_suite.sh` with the
event's commit range, under a 60-minute limit. They take no registry
credential or repository secret and publish nothing. Stage 1 tests both
adapters; this does not establish a hosted eleven-stage run.

## Schema upgrade, upgrades and rollbacks

Schema change `0001_schema_change_record` creates `pubdex.schema_changes`.
It only adds and is backward compatible with 0.1.0. The record holds each
change's number, name, SHA-256, compatibility, applying PubDex version,
time and role. A transaction commits the SQL and its record together;
an advisory lock serializes runners. Changed files, gaps, a partial
baseline, an incompatible newer schema and an unsuitable owner are refused.

Every fresh installation starts with the frozen 0.1.0 baseline and then
runs `python -m people_pubs.schema upgrade`. Compose's one-shot
`schema-upgrade` service runs as `postgres` after the database is healthy;
the read service, ingestion and demonstration wait for it to succeed.
The chart uses a revision-specific Job in evaluation mode and a hook after
the setup Job in external mode, with the admin Secret. A routine external
upgrade changes neither passwords nor schema, runs neither hook and needs
no admin Secret.

The transform version is 1.5.4, from 1.5.3 in 0.1.0. Ingestion checks the
schema before any write; the source metadata becomes the same rows apart
from the transform-version stamp. Readiness and publication requests check
the schema on every request: `schema_upgrade_required` or
`schema_not_supported` returns HTTP 503 with an explanation and the upgrade
command, while liveness remains independent. Ingestion exits 1 before
writing. Compatible newer schema changes are accepted.

The test paths are:

| Deployment | Upgrade and rollback exercised |
|---|---|
| Compose, stage 11, steps 1–2 | A→B1 applies its additive schema change; re-ingestion stamps B1's transform version; image rollback to A keeps the schema and A serves and ingests on it |
| Compose, stage 11, steps 3–5 | B2 renames a column the serving query uses and adapts its own query. A's unguarded query fails with `UndefinedColumn`; its normal schema upgrade, read service and ingestion refuse B2's schema. Restoring the pre-upgrade backup with A's files and images restores the schema record, counts, transform stamps, title and year |
| Kubernetes 1.37.0, external mode, stage 9, steps 22–26 | A and B1 installed from their chart packages and packaged external examples. B1 without its schema upgrade remains unready while A serves; the setup and schema hook complete the upgrade. B1 re-ingests with its transform version; image rollback to A keeps the schema and A serves and ingests |
| Published 0.1.0→checkout on Compose, stage 11, step 6 | published images by digest with their own deployment files; upgrade applies `0001`; rollback to 0.1.0's files and images serves the publication on the upgraded schema |
| Published 0.1.0→checkout on Kubernetes, stage 9, steps 8–9, 17–20 and 33–34 | the preceding release's own chart and digest-pinned images; evaluation and external modes on 1.37.0, evaluation mode on 1.35.8 as a namespace-scoped operator; each upgrade records the checkout's schema changes and each image/chart rollback serves the publication on that schema |

For 0.2.0, the upgrade floor and preceding PubDex release are both 0.1.0,
so the published-source path runs once per deployment phase. Rollback is
supported only to the PubDex release the upgrade started from. All 0.2.0
schema changes are backward compatible, so rollback to 0.1.0 exchanges
images and deployment files and retains the upgraded schema.

Stages 3 and 5 also check repeated upgrades, transactional failure and
retry, concurrent runners, lock timeout, owner privileges and schema
refusals on disposable databases. Stage 5 runs the actual development
restore helper with an explicit synthetic dump and checks restored values,
the schema record, ownership and both application roles. Stages 7 and 11
test the canonical Compose restore, which retains owners and ACLs.

PubDex provides no rollback by restore on Kubernetes. After an incompatible
schema change, the operator restores the pre-upgrade backup before starting
the older images, using the external server's own means in external mode.

## Local verification record

**On 2026-11-24 the full suite passed on this tree, apart from
`TASK_2C.md`, `CHANGELOG.md`, `README.md` and `DB/TESTING_STRATEGY.md`,
which passed stage 1 afterwards.** The Kubernetes-window reading dates
were refreshed before the full run; the supported window remained
1.35–1.37. The suite also checked the implementation's transform-version
and secrets commit range.

| Stage | Seconds | Result |
|---|---|---|
| 1, offline checks | 536 | 2,376 passed, 138 expected skips; release notes, version guard and transform-version range check passed |
| 2, secrets and local configuration | 8 | publishable tree and commit range passed |
| 3, baseline and schema upgrade | 7 | passed |
| 4, fixture demonstration | 4 | passed without metadata-provider requests |
| 5, disposable integration | 36 | 135 passed, then 28 after the runtime patch and 28 after the development restore |
| 6, Compose acceptance | 49 | passed, cleanup confirmed |
| 7, operator workflow | 55 | passed, including restore; cleanup confirmed |
| 8, chart checks | 28 | 471 passed |
| 9, Kubernetes acceptance | 577 | all 35 steps passed on 1.37.0 and 1.35.8; cleanup confirmed |
| 10, release rehearsal | 32 | both builds verified and compared; cleanup confirmed |
| 11, Compose upgrades | 135 | compatible rollback, incompatible refusal and restore, published 0.1.0 path passed; cleanup confirmed |
| **Complete suite** | **1,467** | **all eleven stages passed** |

The host was Linux 6.8 on x86_64, Bash 5.2.21, Git 2.43.0, CPython
3.11.17, pip 26.2.2, psycopg 3.3.6 in its Python implementation with
libpq 16.15, Docker Engine 29.10.0 with the classic `overlay2` image
store, Compose 5.8.0 and buildx 0.39.1. Packaging used GNU tar 1.35 and
gzip 1.12. The cluster tools were helm 4.3.1, kubectl 1.36.6 and kind
0.33.0, with the two digest-pinned node images in
[`DB/lib/cluster_tools.sh`](DB/lib/cluster_tools.sh), Kubernetes 1.37.0
and 1.35.8; Syft was 1.57.2, pinned in
[`DB/lib/sbom_tool.sh`](DB/lib/sbom_tool.sh). OpenSSL was 3.0.10.
The existing virtual environment and cached tools and images were used.

Every Docker stage confirmed cleanup. Containers, networks, volumes,
image inventories and the suite's temporary directories matched their
pre-run inventory afterwards. No existing cluster was contacted. Stage 1,
Markdown links, secrets and scope checks passed on the record documents;
stages 2–11 were not rerun after those documents were written.

## Verification limits

- **One local host.** This result is a working-tree run on Linux x86_64,
  with psycopg's Python implementation and Docker's classic image store.
  The final candidate's clean-clone run and hosted run are pending.
  Containerd image-store behavior is covered by synthetic saved-image
  fixtures, without a runtime run on that store here.
- **Test releases.** The suite builds local test versions with the cache.
  The final candidate's clean tagged build without the cache, registry
  publication, published downloads, digest-pinned 0.2.0 pulls and published
  0.1.0→0.2.0 upgrades have not run. Publication and recovery paths use
  stubbed registries in stage 1. The two archive/package builds ran on one
  machine with the same tools; images are not claimed byte-identical.
- **Upgrade coverage.** The floor equals the preceding release, so no
  distinct floor was exercised. Intermediate published versions do not
  exist on this path. Kubernetes A→B1 ran only in external mode on 1.37.0;
  B2 and rollback by restore ran only on Compose. No rollback by restore
  from a published PubDex release was needed or run.
- **Clusters.** Single-node kind, kindnet and the default StorageClass.
  External PostgreSQL ran on a disposable server in the newest cluster;
  no managed service, other distribution, network plugin, storage
  provisioner or multi-node scheduling ran. Kubernetes 1.36 rests on API
  compatibility; 1.38 and later are not known. The ingress and scheduling
  examples reached API servers only in dry runs: no ingress controller or
  certificate manager served them. Kubernetes backup/restore, manual
  external installation without the setup hook, scheduled CronJob runs and
  external mode on the floor were not run by this suite.
- **Other CI.** No eleven-stage Forgejo job or GitLab pipeline ran. The
  adapter commands are tested offline; earlier public and private runs in
  the preceding task records do not establish this candidate's result.
- **Compatibility rules.** The release-note check verifies schema and
  transform facts and the upgrade floor; it does not verify that every
  incompatible configuration change has been documented. No API
  compatibility policy or contract tests are delivered here.

## Public CI and the published PubDex release

**Pending.** The record update after publication must identify the final
source, its clean-clone verification, the public pull-request and push
runs with their durations, and the published 0.2.0 artifact set. It must
record download checks against committed checksums, anonymous pulls by
digest, config-to-provenance comparisons, a rebuild of the source archive
and chart package, and upgrades and rollbacks between published 0.1.0 and
0.2.0 on Compose and a disposable cluster. No such result is claimed here.

## Not part of this result

- **Task 3b:** the API compatibility policy and contract tests, write paths
  and authentication.
- **Task 4a:** supported operator wrappers, scheduled backups, retention,
  recovery drills and a Kubernetes restore procedure. This result provides
  documented upgrade steps and the schema-upgrade command.
- **Task 6a:** the dependency-update and audit policy and public decision
  index.
- **Task 6b:** the final release policy, byte-identical images and signing.
  Nothing here signs tags, images or artifacts.
- A chart registry, automatic publication by CI, other architectures,
  highly available PostgreSQL, existing-cluster operation or external
  acceptance of the task.
