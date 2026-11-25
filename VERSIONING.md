# Versions, upgrades and platforms

This page is for operators. It says what a PubDex version number promises,
which upgrades and rollbacks between PubDex releases are supported and which
of them the verification suite runs, and on which platforms PubDex is
supported. The commands are in the two deployment guides:
[`DEPLOYMENT.md`](DEPLOYMENT.md), *Upgrading to a new release*, for Docker
Compose, and [`KUBERNETES.md`](KUBERNETES.md), *Upgrading to a new release*,
for the Helm chart.

A ***PubDex release*** is one version of PubDex published as one set: the
tagged source, the application image, the PostgreSQL image and the Helm
chart's package, with a checksum file, an SBOM for each image and a
provenance record. A *Helm release*, by contrast, is one installation of the
chart in a namespace.

## Contents

- [What a version promises](#what-a-version-promises), with [A checkout between PubDex releases](#a-checkout-between-pubdex-releases)
- [Schema changes and the schema upgrade](#schema-changes-and-the-schema-upgrade)
- [Supported upgrades](#supported-upgrades), with [What the suite runs](#what-the-suite-runs)
- [Rollback](#rollback)
- [The release record](#the-release-record), with [Checking a PubDex release](#checking-a-pubdex-release)
- [Platforms](#platforms), with [The Kubernetes window](#the-kubernetes-window)

## What a version promises

**One number.** A PubDex release has one version, `MAJOR.MINOR.PATCH`. It
is the content of [`VERSION`](VERSION), the version label of both images,
their tags `ghcr.io/ccu-ukn/pubdex:<version>` and
`ghcr.io/ccu-ukn/pubdex-postgres:<version>`, the Helm chart's `version` and
its `appVersion`, the Git tag `v<version>` and the name of every file of
the release. A test in the suite keeps `VERSION` and the chart's two fields
equal. A change to the chart alone is therefore a new PubDex release of
everything.

**Patch and minor releases.** Until 1.0.0:

- a ***patch release*** (0.2.**1**) changes neither the schema nor
  ingestion. Its images and those of the release before it can be exchanged
  in both directions;
- a ***minor release*** (0.**3**.0) may change the schema, ingestion, or
  both, and its notes say which.

A patch release also keeps chart values, Compose settings, environment
variables and secret files working as they are: it removes or renames none
and changes no default in a way that makes an existing values file or
`.env` fail; a minor release names each such change under *Upgrade and
rollback* in its notes.

What a version from 1.0.0 on promises is not decided yet; this page will
say it before 1.0.0 is published.

**The release notes.** Each PubDex release has one section in
[`CHANGELOG.md`](CHANGELOG.md), `## PubDex <version>`, whose first three
headings are always *Schema changes*, *Ingestion changes* and *Upgrade and
rollback*, with `None.` where nothing changed. The first stage of the suite
checks them against the tree: every schema change new since the preceding
PubDex release is named with whether it is backward compatible;
*Ingestion changes* says `None.` exactly when the transform version is
unchanged; *Upgrade and rollback* names the floor and the preceding PubDex
release, and says so when the floor was raised or when a rollback is a
restore; and a patch release carries neither a schema change nor a new
transform version.

**A tag can be moved; a digest cannot.** A registry lets whoever controls a
repository point a tag at another image. The PubDex project never moves a
version tag, but only the digest identifies an image for certain: install a
PubDex release by the two digests its [release record](#the-release-record)
names. The defaults of `compose.yaml` and of the chart name the images by
version tag only, because a commit cannot contain the digests of images
built from it.

**A published version keeps its content.** Once a version is in the
release record, the first stage of the suite refuses a tree that carries
that version in `VERSION` but ships other files than the release's commit:
the files the two images are built from and the Helm chart.

**Nothing is signed.** PubDex signs neither its tags nor its images. The
integrity of a PubDex release rests on the checksum file and the image
digests committed to the repository.

### A checkout between PubDex releases

After a PubDex release, `VERSION` moves to the next version in the first
commit that changes anything a PubDex release ships. From then on a checkout
names, in `VERSION`, in `compose.yaml` and in the chart's defaults, a
version that is not published, and its default images do not exist in the
registry. Such a checkout builds its own images with
[`build_image.sh`](build_image.sh) and names them: in `.env` for Compose
([`DEPLOYMENT.md`](DEPLOYMENT.md), *The short path*), in `image` and
`postgresImage` for the chart. Images built that way carry the version and
the commit in their labels, the commit marked `-dirty` when the working
tree had changes. They are no PubDex release, and the upgrade paths below do
not cover them.

## Schema changes and the schema upgrade

The ***baseline*** is the schema of PubDex 0.1.0, the files of
[`DB/init/`](DB/init/), which never change. Every installation starts from
it. Every later change of the database's structure is a ***schema change***
(what other tools call a migration): one numbered SQL file in
[`DB/schema/`](DB/schema/), `NNNN_<name>.sql`, numbered from 0001 without a
gap, shipped in the application image. A ***schema upgrade*** brings a
database to the schema of the images in use: it applies, in number order,
each schema change the database has not had yet, each in a transaction of
its own, and adds a row for it to the database's record, the table
`pubdex.schema_changes`: its number and name, the SHA-256 of its file,
whether it is backward compatible, the PubDex release that applied it, when
and as which role. A new installation is the baseline plus every schema
change, applied the same way.

The command is `python -m people_pubs.schema upgrade`, in the application
image; `python -m people_pubs.schema status` reports what is recorded and
what is outstanding and changes nothing. The deployments run it for you:

- **Compose**: the one-shot service `schema-upgrade`, at every
  `docker compose up` and before every `docker compose run` of `ingest` or
  `demo`; nothing that uses the database starts until it has succeeded.
- **The chart**: the schema upgrade Job, at the install and at every
  upgrade in evaluation mode. In external mode it runs exactly when the
  setup Job runs, as the admin role, so an upgrade that brings schema
  changes needs the admin Secret for that run. Without the setup Job you run
  the command yourself ([`KUBERNETES.md`](KUBERNETES.md), *Installing without
  the setup Job*).

Each schema change declares whether it is a ***backward-compatible schema
change***: whether the preceding PubDex release still works on the changed
schema. That decides how an upgrade across it is rolled back
([Rollback](#rollback)).

From 0.2.0 on, the application checks the schema before it uses the
database. Against a database that lacks schema changes of its images, the
read service is not ready, and `/readyz` and `/v0/publications` answer 503
with the reason `schema_upgrade_required`; against a database with a schema
change of a later PubDex release that is not backward compatible, or a
record it cannot accept, with `schema_not_supported`. The message names the
recorded version, the versions the application supports and the command
above. An ingestion command exits with status 1 and the same message before
it writes anything. A database whose later schema changes are all backward
compatible is accepted. PubDex 0.1.0 has no such check.

An ***ingestion change*** is a change to how source metadata becomes rows,
marked by a new transform version, `people_pubs.__version__`, which every
row ingestion writes carries. An upgrade rewrites no row: rows keep the
transform version that wrote them until ingestion writes them again.

## Supported upgrades

- **The floor.** An upgrade is supported from every PubDex release from the
  ***floor*** on, directly to the new one: the schema upgrade applies every
  schema change the database has not had, in order, whichever releases lay
  between. [`releases/FLOOR`](releases/FLOOR) names the floor for upgrading
  to the version of the checkout, and each PubDex release's entry in the
  release record the floor that was in force for it. A PubDex release may
  raise the floor; its notes then say so. For PubDex 0.2.0 the floor and
  the preceding release are both 0.1.0.
- **What is tested.** The suite runs the upgrade from the floor and from
  the preceding PubDex release, the newest one the release record holds
  below `VERSION`. An upgrade from a release between those two rests on the
  schema upgrade's order and record, and no test runs it.
- **Rollback.** A rollback is supported only to the PubDex release you
  upgraded from ([Rollback](#rollback)).
- **Take a backup before every upgrade.** It is the only way back after a
  schema change that is not backward compatible, and the way back from
  anything that goes wrong.

### What the suite runs

Stage 9 of `./run_ci_suite.sh` is the Helm chart on disposable kind
clusters, stage 11 the Compose deployment;
[`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) numbers the steps of
each. A ***test release*** is a PubDex release that the release script
builds inside the suite from the checkout, under a version reserved for
tests, and that never leaves the machine that built it: *A* is the checkout
as it is; *B1* adds a backward-compatible schema change and a new transform
version; *B2* adds to B1 a schema change that is not backward compatible.

| Upgrade | Compose, stage 11 | Kubernetes, stage 9 |
|---|---|---|
| The preceding PubDex release, published, to this checkout, with its schema upgrade, then the image rollback | step 6 | evaluation mode on the newest Kubernetes release (steps 6 to 9), external mode (steps 16 to 20), and evaluation mode on the floor as a namespace-scoped user (steps 32 to 34) |
| The floor, published, to this checkout, then the image rollback | step 6, once the floor is another release than the preceding one | — |
| Test release A to B1, then the image rollback, with ingestion on both | steps 1 and 2 | external mode on the newest Kubernetes release, each test release installed from its chart package file, including a routine upgrade that cannot complete (steps 22 to 26) |
| Test release A to B2, the refusal of A's images, then the rollback by restore | steps 3 to 5 | — |

## Rollback

There are two kinds, and the schema changes of the upgrade decide which one
applies.

- **An image rollback**, when every schema change the upgrade applied is
  backward compatible, and after every patch release: start the previous
  PubDex release's images again — on Compose with its `compose.yaml`, on
  Kubernetes with `helm rollback`. The schema and its record stay as they
  are; the older application accepts the later, backward-compatible schema
  changes, and its ingestion writes on that schema, stamping its own
  transform version. Data written since the upgrade stays.
- **A rollback by restore**, when the upgrade applied a schema change that
  is not backward compatible. The older images refuse that database: on
  Compose their schema upgrade fails and `docker compose up --wait` fails
  with it, and their read service and ingestion refuse it too. Restore the
  backup taken before the upgrade, then start the older images. Everything
  written after the backup is lost. Stage 11 runs this on Compose.

PubDex provides no rollback by restore on Kubernetes. After a schema
change that is not backward compatible, the way back is the backup taken
before the upgrade, restored by the operator before the older images
run; in external mode, with the server's own means. The new PubDex
release's notes say when an upgrade needs this.

A rollback never removes a schema change. PubDex 0.2.0's schema changes
are all backward compatible, so a rollback from 0.2.0 to 0.1.0 is an image
rollback: in stages 9 and 11, 0.1.0, which has no schema check, serves the
publication on the upgraded schema.

## The release record

[`releases/`](releases/README.md) in the repository is the authoritative
record of every published PubDex release. Its directory for a version
holds `release.json`: the commit and tag, the date it was published, the
platform, both images by registry, repository, tag and digest, the schema
changes it ships with their SHA-256, its transform version, its floor and
its Helm chart. From 0.2.0 on it also holds the checksum file `SHA256SUMS`,
over every file of the release, `DIGESTS`, the two image references with
their digests, the application image first, and the provenance record. A
forge's release page, which carries the source archive, the chart package,
the SBOMs and the provenance record, is a copy;
`DB/verify_release.py files` requires all five downloads and checks them
against the committed record. PubDex 0.1.0 predates the release automation: its
entry is `release.json` alone.

The source archive and the chart package are the same, byte for byte, when
built again from the tag with the same GNU tar, gzip and pinned helm,
whose versions the provenance record names; the release rehearsal, stage
10, builds both files twice on one machine and compares them. The images
are not: they can be built again from the pinned inputs the provenance
record lists, and they are identified by the digests the release record
holds.

### Checking a PubDex release

For a PubDex release from 0.2.0 on, use a checkout containing its committed
release record and download the source archive, chart package, both SBOMs
and provenance record. The suite tests the helpers on synthetic inputs;
it does not run this download-and-pull procedure. File verification needs
only Python 3.11 or later. Image verification also
needs the supported Docker Engine on Linux x86_64. Synthetic fixtures cover
the verifier in stage 1; the suite does not download a release page.

1. In the download directory, set the version and the absolute path to its
   committed record, then check the downloads against that record:

   ```bash
   version='<version>'
   record='<absolute path to checkout>/releases/'"$version"
   python3 "<absolute path to checkout>/DB/verify_release.py" files --version "$version" \
     --checksums "$record/SHA256SUMS"
   ```

   The check requires all five files and verifies their bytes: the source
   archive, chart package, both SPDX SBOMs and provenance record. It fails on
   any missing file, checksum entry or mismatched content. `IMAGES` is listed
   but not published and is optional. Use the committed checksum file, not
   a downloaded copy of it. This step needs no registry credentials, Docker,
   Helm or Syft.

2. Read both image references from `release.json` and pull them by digest
   with an empty Docker client configuration, without credentials:

   ```bash
   application=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["images"]["application"])' "$record/release.json")
   postgres=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["images"]["postgres"])' "$record/release.json")
   docker_config=$(mktemp -d)
   docker --config "$docker_config" pull --platform linux/amd64 "$application"
   docker --config "$docker_config" pull --platform linux/amd64 "$postgres"
   ```

   A pull by digest checks the content against that digest. Both images
   are single manifests for `linux/amd64`.

3. Compare each pulled image's saved config digest with its provenance.
   Format `pubdex-provenance/2` records `images.<role>.config_digest`
   independently of Docker's store-dependent image ID, preserving `id` as
   another builder identity. The checking machine and builder may each use
   either image store. `save --platform` requires Docker API 1.48 or later.

   ```bash
   (
   archive=$(mktemp)
   trap 'rm -f "$archive"' EXIT
   for role in application postgres; do
     image="$application"
     if [ "$role" = postgres ]; then image="$postgres"; fi
     docker --config "$docker_config" image save --platform linux/amd64 --output "$archive" "$image" || exit
     python3 "<absolute path to checkout>/DB/verify_release.py" image-config "$archive" \
       --provenance "pubdex-$version-provenance.json" --role "$role" || exit
   done
   )
   ```

   Both roles must report `OK`. The verifier reads the saved archive's
   `manifest.json` and hashes the config bytes it names. The
   [classic](https://github.com/moby/moby/blob/v28.5.1/image/tarexport/save.go)
   and [containerd](https://github.com/moby/moby/blob/v28.5.1/daemon/containerd/image_exporter.go)
   exporters provide this interface. Format 1 remains verifiable using `id`
   for classic-store builds, where it is the config digest. A format 1
   containerd build did not record this digest and cannot support this
   comparison; the checker reports that limitation rather than treating an
   index or manifest digest as a config digest. Existing records are unchanged.

4. These config digests connect the published images to provenance. An
   SBOM's local image tag and its `pkg:oci` identifier are not the registry
   manifest digest; do not compare them with `DIGESTS`.

## Platforms

- **Docker Compose**: Linux on x86_64 with Docker Engine 28.0.1 or later,
  except 28.2.0 to 28.3.2, and the Docker Compose plugin 2.24.0 or later.
  [`DEPLOYMENT.md`](DEPLOYMENT.md), *Supported platforms*, is the table of
  what is supported and what ran.
- **The images** are `linux/amd64` only.
- **PostgreSQL 16.** The PostgreSQL image of each PubDex release is
  PostgreSQL 16, and external mode needs a PostgreSQL 16 server. A backup
  restores into PostgreSQL 16 only. No PubDex release covers an upgrade to
  another PostgreSQL major version.
- **Kubernetes**: the window below, with Helm 4.3 or a later 4.x and a
  kubectl within one minor release of the cluster;
  [`KUBERNETES.md`](KUBERNETES.md), *Supported platforms*, is the table of
  what ran.

### The Kubernetes window

The supported Kubernetes releases are established again at each PubDex
release: the newest Kubernetes minor release on that date, and the oldest
one the Kubernetes project still maintains, the floor. The kind node images
of both are pinned in [`DB/lib/cluster_tools.sh`](DB/lib/cluster_tools.sh),
and stage 9 runs on both; a release between them is supported by API
compatibility. Between two PubDex releases the window stands as dated, even
when a new Kubernetes release appears or the floor reaches its end of life.
The chart's `kubeVersion` names the floor and has no upper bound, so Helm
installs the chart on a newer Kubernetes, whose status is *not known*. It
refuses, rather than warns about, a release older than the floor: stage 9
of this PubDex release did not run on it, and the Kubernetes project no
longer maintains it.

| Kubernetes | In the window | End of life |
|---|---|---|
| 1.37 | the newest release; stage 9 runs it | 2027-10-28 |
| 1.36 | supported by API compatibility | 2027-06-28 |
| 1.35 | the floor; stage 9 runs it | 2027-02-28, in maintenance mode from 2026-12-28 |
| 1.38 | not known; planned for 2026-12-16 | — |

The dates are those of the Kubernetes project's release information
(kubernetes.io, *Releases* and *Patch Releases*, and the 1.38 release
schedule), as read on 2026-11-24.
