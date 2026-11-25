# Making a PubDex release

This page is for maintainers: of PubDex, or of a fork on any forge. It is
the procedure that makes a ***PubDex release*** — one version of PubDex
published as one set — from the decision on its version to the record the
repository keeps of it, with the rules for the schema changes and the
release notes it ships. Operators read [`VERSIONING.md`](VERSIONING.md)
instead: what a version promises and how to upgrade.

Commands run from the root of a checkout unless a step says otherwise. In
them, `<version>` is the version being released, `<commit>` the full id of
the commit it is made from, `<previous version>` and `<previous commit>`
those of the preceding PubDex release, `<registry>/<namespace>` the prefix
of the image repositories (`ghcr.io/ccu-ukn` for PubDex), and `<public
repository URL>` the repository's public address
(`https://github.com/CCU-UKN/pubdex` for PubDex). Each block of commands
is marked in [What ran these commands](#what-ran-these-commands): run by a
stage of the suite, or only at a real PubDex release.

## Contents

- [What a PubDex release is](#what-a-pubdex-release-is)
- [What you need](#what-you-need)
- [Writing a schema change](#writing-a-schema-change)
- [The procedure](#the-procedure): [1. The version](#1-the-version),
  [2. The release notes](#2-the-release-notes),
  [3. The Kubernetes window](#3-the-kubernetes-window),
  [4. The suite](#4-the-suite), [5. The tag](#5-the-tag),
  [6. Build and verify at the tag](#6-build-and-verify-at-the-tag),
  [7. Publish](#7-publish), [8. The release page](#8-the-release-page),
  [9. The record commit](#9-the-record-commit)
- [What ran these commands](#what-ran-these-commands)

## What a PubDex release is

[`release.sh`](release.sh) builds the whole set into one directory,
`dist/pubdex-<version>/`, and verifies it; a separate step of the same
script, which only the maintainer runs, publishes the two images:

| File | What it is |
|---|---|
| `pubdex-<version>-source.tar.gz` | the source archive: every file of the tagged commit under `pubdex-<version>/` |
| `pubdex-<version>.tgz` | the Helm chart's package, `charts/pubdex/` with the repository's licence |
| `IMAGES` | the application image and the PostgreSQL image as this machine holds them: local tag and image id |
| `pubdex-<version>-image.spdx.json`, `pubdex-<version>-postgres-image.spdx.json` | an SBOM of each image, SPDX 2.3 JSON, written by the pinned Syft without network |
| `pubdex-<version>-provenance.json` | the provenance record: what the release was built from and with — the commit, the base images by digest, PostgreSQL's source hash, the dependency locks, the build files, the compiler packages each image records, the client libraries, the build tools — and both image ids and config digests (format 2; format 1 remains verifiable) ([`DB/release_provenance.py`](DB/release_provenance.py) describes every field) |
| `SHA256SUMS` | the SHA-256 of every other file, as `sha256sum` writes and checks it |
| `DIGESTS` | written by the publish step: both images by registry, repository, tag and digest, the application image first |

**The record and the copy.** The ***release record***,
[`releases/`](releases/README.md), committed to the repository, is
authoritative: for each published PubDex release its `release.json` — the
commit, the tag, both image digests, the schema changes, the transform
version, the floor, the chart — and the files `SHA256SUMS`, `DIGESTS` and
the provenance record as the build and the publish step wrote them. The
forge's release page carries a copy of the files for download; the
committed `SHA256SUMS` checks them wherever they come from.

**What is reproducible.** The source archive and the chart package are the
same, byte for byte, when built again from the tag with the same GNU tar,
gzip and pinned helm, whose versions the provenance record names: the
rehearsal (stage 10 of the suite) builds both files twice on one machine
and compares them. The images
are not: their labels record the time of the build, and a PubDex release
builds both without Docker's build cache, so their Debian packages come from
Debian's archive as it is at the time of the build; a test release uses the
cache. They can be built again from the pinned
inputs the provenance record lists, and are identified by the digests the
release record holds.

**Nothing is signed.** Neither the tags nor the images. The integrity of a
PubDex release rests on the committed checksum file and image digests.

**PubDex 0.1.0 predates this procedure.** Its images were published by
hand from commit `57a72dc9f27766007b51c3d16ac7aa8c6ff3798b`, which its tag
`v0.1.0` names ([`TASK_2A.md`](TASK_2A.md), *Published-image evidence*).
It has no source archive, checksum file, SBOM or provenance record, and no
chart package; its entry in the release record is `release.json` alone.

**CI rehearses and never publishes.** Every run of the suite, in public CI
as anywhere, includes the rehearsal: `release.sh` builds and verifies a
***test release*** of the checkout, under a version reserved for test
releases, with a Docker client configuration that holds no registry
credential. No
CI job logs in to a registry or pushes anything; publication is the
maintainer's step 7 alone.

**No vulnerability scan.** `release.sh` runs none, and the release record
holds no scan result.

## What you need

- A machine that meets the suite's contract
  ([`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *Running the suite
  elsewhere*): Linux on x86_64, a Docker Engine 28.0.1 or later, except
  28.2.0 to 28.3.2, with the buildx plugin and the Compose plugin 2.24.0
  or later, Python 3.11, Git, GNU tar, gzip and `sha256sum`.
- The pinned tools: helm, kubectl and kind under `.tools/`
  (`./setup_cluster_tools.sh`, per checkout), and Syft's image by its
  pinned digest (`./release.sh setup`, once per machine: an anonymous pull
  from Docker Hub, about 92 MB).
- Push access to the two image repositories
  `<registry>/<namespace>/pubdex` and `<registry>/<namespace>/pubdex-postgres`,
  and the right to tag the repository and to create its release page on
  the forge. On `ghcr.io` the login takes a classic personal access token
  with `write:packages`, typed at the password prompt.

A fork that publishes its own images also changes the image defaults that
name PubDex's — `ghcr.io/ccu-ukn` in `compose.yaml`, the registry and
repositories in the chart's `values.yaml` — and the tests that check them,
[`DB/tests/test_release_version.py`](DB/tests/test_release_version.py)
among them.

## Writing a schema change

A ***schema change*** is one file `DB/schema/NNNN_<name>.sql`: `NNNN` the
next number, four digits, without a gap across all PubDex releases;
`<name>` lower-case letters, digits and underscores. The image takes in
every `.sql` file of `DB/schema/`, and `python -m people_pubs.schema
upgrade` applies each one a database has not had, in number order, in a
transaction of its own ([`DB/people_pubs/schema.py`](DB/people_pubs/schema.py)
documents the command). `DB/init/`, the ***baseline***, never changes.

- **The header.** The file opens with a block of comment lines. The first
  says what the change does, and exactly one line reads
  `-- backward-compatible: yes` or `-- backward-compatible: no`.
- **One transaction.** No statement that ends or opens a transaction
  (`BEGIN`, `COMMIT`, `SAVEPOINT` and their kind), and none that PostgreSQL
  refuses in a transaction block (`VACUUM`, `CREATE INDEX CONCURRENTLY`,
  `ALTER SYSTEM`, and the others `transaction_problem()` in
  `people_pubs/schema.py` lists).
- **Names and privileges.** Schema-qualified names. A new sequence gets
  `GRANT USAGE, SELECT` to `app_writer` explicitly; a new schema gets its
  `USAGE` grants; a `SECURITY DEFINER` function revokes `EXECUTE` from
  `PUBLIC`. Stage 3 checks, after the schema upgrade, that `app_readonly`
  reads and `app_writer` writes every table of `app`, `biblio` and
  `activity` and uses every sequence there, and that both read the record
  and nothing else of its schema.
- **Backward compatible** means that the preceding PubDex release still
  works on the changed schema, so that a rollback only swaps the images:
  additions only — tables, views, functions, indexes, nullable or
  defaulted columns — and never a drop, a rename, a type change or a new
  `NOT NULL` without a default on anything the preceding release reads or
  writes. For a file marked `yes`, a test in stage 1 refuses `DROP`,
  `RENAME`, a column type change, `SET NOT NULL` and an added `NOT NULL`
  without `DEFAULT`; it is a heuristic over the statements, which does not
  look into function bodies. A change that is not backward compatible makes
  the rollback a restore of the backup taken before the upgrade, and the
  release notes say so.
- **Never edit a released change.** The schema upgrade refuses a database
  whose recorded change differs from the file of the same number, and the
  release-note check refuses a change of an earlier PubDex release that is
  missing or edited. A correction is a new schema change.
- **The runtime patch of the baseline.** `DB/init/010_ingestion_runtime_schema.sql`
  recreates the serving views and functions with `CREATE OR REPLACE`.
  Applied again — `DB/restore_db.sh` of the development setup does so after
  a restore — it puts back its own definitions of those objects, while the
  database's record still lists every schema change. A schema change that
  redefines one of them is therefore undone by it.

A new schema change raises the schema the application requires: from that
version on, the read service is not ready and ingestion stops on a database
without it, until the schema upgrade has run.

## The procedure

### 1. The version

**When `VERSION` moves.** After a PubDex release, `VERSION` moves to the
next version in the first commit that changes anything a PubDex release
ships: both Dockerfiles and their allowlists (`.dockerignore`,
`postgres.Dockerfile.dockerignore`) and every file those let in —
`DB/people_pubs/`, `DB/init/`, `DB/schema/`, `compose.yaml` among them —
`build_image.sh`, `DB/lib/release_identity.sh`, and all of
`charts/pubdex/`. The same commit moves the chart's `version` and
`appVersion`, both image tags in `charts/pubdex/values.yaml` and both image
defaults in `compose.yaml`, which a test in stage 1 keeps equal to
`VERSION`, and adds the new version's section to `CHANGELOG.md`, with the
three fixed headings, saying what that commit already changes and `None.`
where nothing has changed yet (step 2): stage 1 fails without it. The version guard,
[`DB/check_version_guard.py`](DB/check_version_guard.py), in stage 1,
refuses a tree whose `VERSION` names a published PubDex release but ships
other files than that release's commit; it names the files and what to
move. It needs the release's commit in the history: a shallow clone fails
it.

In that commit, also update the sentences that name the preceding PubDex
release: in [`VERSIONING.md`](VERSIONING.md), *Supported upgrades* and
*Rollback*; in [`DEPLOYMENT.md`](DEPLOYMENT.md), *Upgrading to a new
release*; and in [`KUBERNETES.md`](KUBERNETES.md), *Appendix: what ran
these commands*, including its table. Keep the table's evidence tied to
what actually ran: changing a version in prose does not test that upgrade.
Published-image instructions keep naming the latest published PubDex
release until the record commit (step 9).

**Which number.** Until 1.0.0:

- a ***patch release*** (`0.2.1`) changes neither the schema nor
  ingestion: no new schema change, and the same transform version,
  `people_pubs.__version__`;
- a ***minor release*** (`0.3.0`) may change either.

A patch release also keeps chart values, Compose settings, environment
variables and secret files working as they are: remove or rename none,
and change no default in a way that makes an existing values file or
`.env` fail. A minor release may make such changes; name each under
*Upgrade and rollback* in its notes.

So before the release, compare what `VERSION` names with what changed
since the preceding PubDex release. A new schema change or a new transform
version under a patch number means moving `VERSION` to the next minor
version, with the files above; the release-note check (step 2) refuses a
patch release that carries either. What a version from 1.0.0 on promises
is not decided yet.

**The floor.** [`releases/FLOOR`](releases/FLOOR) names the oldest PubDex
release from which an upgrade to this one is supported. A PubDex release
may raise it: change the file in the release commit, and say so in the
notes. While the floor is another release than the preceding one, stage 11
upgrades from both.

```bash
python3 DB/release_record.py preceding           # the PubDex release this one follows
python3 DB/check_version_guard.py                # VERSION and what the tree ships
```

### 2. The release notes

Each PubDex release has one section in [`CHANGELOG.md`](CHANGELOG.md),
headed `## PubDex <version>`; the date of publication follows on the same
line in the record commit (`## PubDex <version> — published <date>`). Its
first three subsections are always these, in this order, and where nothing
changed the whole text of one is `None.`:

- `### Schema changes`: one list item per schema change new since the
  preceding PubDex release, naming it as a code span — `` `0002` ``, or its
  file's name — and saying "backward compatible" or "not backward
  compatible", as its file declares.
- `### Ingestion changes`: `None.` exactly when the transform version is
  the preceding release's; otherwise text that names the new transform
  version and what it changes in the rows ingestion writes.
- `### Upgrade and rollback`: names the floor and the preceding PubDex
  release; says, in one sentence with "floor" and "raised", when the floor
  was raised; and when a new schema change is not backward compatible,
  says in one sentence with "rollback" and "is a restore" that the rollback
  is a restore of the backup taken before the upgrade. Name every removed
  or renamed chart value, Compose setting, environment variable or secret
  file, and every default change that needs an operator to edit an existing
  values file or `.env`, with what the operator must change.

Other subsections may follow the three. The check,
[`DB/check_release_notes.py`](DB/check_release_notes.py), compares the
section of `VERSION` with the tree and with the preceding release's entry
in the release record, offline and without Git history, in stage 1. It
prints what it compared — the new schema changes with their compatibility,
the transform versions, the floor — or one line per problem, and refuses
too a patch release with a schema change or a new transform version, and
a schema change of an earlier PubDex release that is missing or edited.
It does not check configuration compatibility or whether the notes name
configuration changes; the maintainer checks those against the preceding
PubDex release.

```bash
.venv/bin/python DB/check_release_notes.py
```

### 3. The Kubernetes window

Establish the supported Kubernetes releases again, on the day, by the rule
of [`VERSIONING.md`](VERSIONING.md), *The Kubernetes window*: the newest
Kubernetes minor release on that date, and the oldest one the Kubernetes
project still maintains. Read them from the project's release information
(kubernetes.io, *Releases* and *Patch Releases*, and the schedule of the
next release).

- **The window stands.** Update the date "as read on" and any changed end
  of life in `VERSIONING.md`, *The Kubernetes window*, and in
  [`KUBERNETES.md`](KUBERNETES.md), *What you need*.
- **The window moved.** In the release commit, together: a kind release
  with node images for both ends, the two node images by digest, a kubectl
  within one minor release of both and a helm that supports the newest, in
  [`DB/lib/cluster_tools.sh`](DB/lib/cluster_tools.sh), each checked
  against its primary source; `kubeVersion` in `charts/pubdex/Chart.yaml`
  (a shipped file, so before the tag); the Kubernetes releases that stage 8
  lints for and its tests; stage 9 and its tests; and every release number
  and date of the window in `KUBERNETES.md`, `VERSIONING.md`, `README.md`
  and `DB/TESTING_STRATEGY.md`. Then `./setup_cluster_tools.sh` and stages
  1, 8 and 9 (`./run_checks.sh`, `./run_chart_checks.sh`,
  `./run_cluster_acceptance.sh`).

### 4. The suite

The release commit is the commit on the main branch that the PubDex
release is made from: its tree has passed the complete suite locally, and
public CI on it, for the pull request and for the push to the main branch.
Locally, from a checkout of it:

```bash
./setup_venv.sh                  # .venv, when there is none or a lock changed
./setup_cluster_tools.sh         # the pinned helm, kubectl and kind
./release.sh setup               # the pinned SBOM tool, once per machine
```

```bash
./run_ci_suite.sh --base <previous commit> --head <commit>
```

With the preceding release's commit as base, the transform-version guard
and the secrets check cover every commit of the PubDex release. Stage 10
rehearses the build and verification of step 6 with a test release of the
checkout; nothing in the suite publishes.

### 5. The tag

Tag the release commit `v<version>`, annotated, in your repository:

```bash
git tag --annotate v<version> --message "PubDex <version>" <commit>
```

`release.sh` builds a PubDex release only at a commit that the annotated
tag `v<version>` names: it refuses a lightweight tag, a tag at another
commit and a checkout with changes. The tag is pushed in step 7, once the
build at the tag has been verified. A pushed tag is never moved.

### 6. Build and verify at the tag

In a new directory outside your checkout, a fresh clone at the tag, from
the repository that holds it — your own, since the tag is not pushed yet —
with the pinned tools:

```bash
git clone --branch v<version> <repository> <directory>
cd <directory>
./setup_cluster_tools.sh
./release.sh setup
```

Build the set; `build` ends by verifying the finished directory, and
`verify` checks it again on its own, building nothing:

```bash
./release.sh build --source <public repository URL>
```

```bash
./release.sh verify dist/pubdex-<version>
```

`--source` gives both images' `org.opencontainers.image.source` label. The
build takes longer than a test release's, because nothing comes from
Docker's build cache: building both images without it took about three
minutes on a workstation. The images are tagged `pubdex-release:<version>` and
`pubdex-postgres-release:<version>` on this machine only; their labels name
`<version>` and `<commit>`, never `-dirty`. Then verify them as a PubDex
release, with the `compose.yaml` the application image carries, and the
operator's workflow with them:

```bash
./run_stack_acceptance.sh --release --image pubdex-release:<version> \
  --postgres-image pubdex-postgres-release:<version> \
  --expect-version <version> --expect-revision <commit>
./run_operator_acceptance.sh --image pubdex-release:<version> \
  --postgres-image pubdex-postgres-release:<version>
```

Publish from this directory and these images, in this clone: a second
build gives the images new ids and moves the local tags, after which the
directory no longer verifies, and `publish` refuses a directory that does
not. Keep the directory until step 9.

### 7. Publish

From your checkout, push the tag to the forge:

```bash
git push origin v<version>
```

Then, in the clone, log in, publish and log out:

```bash
docker login <registry>          # asks for the credentials; never put them on a command line
./release.sh publish dist/pubdex-<version> --repository-prefix <registry>/<namespace>
docker logout <registry>
```

`publish` refuses, before any push: a test release; a directory that holds
`DIGESTS` already or that `verify` does not pass, which includes a checkout
that is not the clean, tagged commit; a version that either repository
holds as another image, or as one whose digest the verified image does not
record (below); and a local tag of those names that names another image.
Only the registry's explicit answer `manifest unknown` to `docker manifest
inspect` counts as absent: any other answer but the manifest stops it — an
access denial, a network failure, and also a repository the registry
reports as unknown (`name unknown`), so `publish` cannot make the first
publication to a registry that answers so for repositories that do not
exist yet. The check is made at one moment and reserves nothing:
coordinate with anyone else who publishes to the same repositories.
`publish` decides both images before the first push, then tags both with
their repository names, pushes the PostgreSQL image first, so that no
published application image names a PostgreSQL image that is missing, then
the application image — each only if the registry does not hold it yet —
prints both digests and writes them to a temporary file beside `DIGESTS`,
validates its complete contents, then installs it atomically without
overwriting an existing record. A failed write removes the temporary file
and leaves publication retryable. It never logs in, never
moves a tag and never pushes the chart package.

**If `publish` fails.** Fix the cause, such as an expired login or a lost
connection, and run the same `publish` command again, in the same clone,
with the same directory: it completes what the first run left. For an
image the registry already holds under the version, `docker buildx
imagetools inspect` reads the registry's digest, and `publish` compares it
with the digests the verified image records for that repository (`docker
image inspect`, `RepoDigests`), which a push of it from this machine
records. If the digest is among them, the registry holds that very image:
`publish` does not push it again, takes its digest, and pushes only what is
missing. It pushes nothing over an image that this machine did not push.
If it refuses because the registry holds another image under the version,
or one whose digest the verified image does not record, the version is
spent. Do not overwrite or delete anything in the registry. The pushed tag
`v<version>` stays and is never moved, and the release record gets no
entry for the version. Make the next version (step 1), and its release
notes (step 2) say that `<version>` was not published. Keep the build
directory and the images until the publication is complete: a second build
gives the images new ids, after which the directory no longer verifies.

On `ghcr.io` a package that the first push creates is private: make it
public, and check that it links to the repository (the `--source` label),
before the next check.

Then check the published images as a user receives them, without
credentials, on a machine that does not have them, or after `docker image
rm` of every tag of both images, with the two references of `DIGESTS`:

```bash
cat dist/pubdex-<version>/DIGESTS
DOCKER_CONFIG="$(mktemp -d)" ./run_stack_acceptance.sh --release --pull \
  --image <registry>/<namespace>/pubdex:<version>@sha256:<digest> \
  --postgres-image <registry>/<namespace>/pubdex-postgres:<version>@sha256:<postgres digest> \
  --expect-version <version> --expect-revision <commit>
```

The empty configuration also hides a Compose plugin installed under
`~/.docker/cli-plugins`; with one, run `docker logout <registry>` instead.

### 8. The release page

On the forge that hosts the repository, create the release page of the tag
`v<version>`, with the release notes as its text, and attach from
`dist/pubdex-<version>/`: the source archive, the chart package, both
SBOMs, the provenance record and `SHA256SUMS`. Use the forge's web
interface or its API; no step depends on one forge or on its command-line
tool. The page is a copy for download: the release record of step 9 is
authoritative, and a copy on another host serves as well. In a new
directory, download the attached files from the page, and check them
against the build's checksum file, which step 9 commits:

```bash
python3 <checkout>/DB/verify_release.py files --version '<version>' \
  --checksums <directory>/dist/pubdex-<version>/SHA256SUMS
```

This requires and hashes all five downloads against the checksum file that
step 9 commits. `IMAGES`, which names the build machine's images, is not a
required download. File verification needs only Python, without image tools
or registry credentials.

### 9. The record commit

The record commit follows the release commit on the main branch. It adds
the PubDex release to the release record and points the guides at the
published images, and touches no shipped file, so `VERSION` stays and the
version guard passes: it now compares what the tree ships with the release
commit.

```bash
mkdir releases/<version>
cp <directory>/dist/pubdex-<version>/SHA256SUMS <directory>/dist/pubdex-<version>/DIGESTS \
  <directory>/dist/pubdex-<version>/pubdex-<version>-provenance.json releases/<version>/
sha256sum DB/schema/*.sql
```

Then write `releases/<version>/release.json`, with the fields
[`releases/README.md`](releases/README.md) lists: `version`; `commit`, the
release commit; `tag`, `v<version>`; `published`, the date of step 7;
`platform`, `linux/amd64`; `images`, the two references of `DIGESTS`;
`schema_changes`, every file of `DB/schema/` with its number, its name and
the SHA-256 above; `transform_version`, `people_pubs.__version__`; `floor`,
the content of `releases/FLOOR`; and `chart`, with `version` `<version>`,
`commit` the release commit and `package` `pubdex-<version>.tgz`. Then, in
the same commit:

- the publication date on the section's heading in `CHANGELOG.md`;
- [`DEPLOYMENT.md`](DEPLOYMENT.md), *The short path*: the published
  version; and *Selecting a published image*: the version, source commit,
  publication and anonymous-verification evidence, both image digests and
  the commands' expected version and revision. Link evidence for this
  PubDex release rather than carrying forward the evidence of 0.1.0;
- [`KUBERNETES.md`](KUBERNETES.md), *Installing without the setup Job*,
  *Ingestion* and *Importing a roster*: every published image reference by
  digest;
- [`README.md`](README.md), *Running PubDex as a service*: the published
  version;
- this page, *What ran these commands*: the evidence for the published
  images checked in step 7 and the checks after the build in step 6.

Search these guides and [`VERSIONING.md`](VERSIONING.md) for the old
published version, its source commit and `@sha256:` to catch other
publication references. Keep historical facts and test evidence attached
to the PubDex release they describe. The preceding release in upgrade and
rollback descriptions is relative to `VERSION`; update it when `VERSION`
moves (step 1), not merely because a publication finished. Stage 1's
[`DB/tests/test_release_record.py`](DB/tests/test_release_record.py)
checks every concrete `ghcr.io/ccu-ukn/pubdex` and `pubdex-postgres`
version-and-digest reference in the public guides against that version's
release record; it does not check the surrounding prose or evidence.

Check the entry and the tree:

```bash
python3 DB/release_record.py entry <version>
./run_checks.sh
```

From the record commit on, the suite finds the PubDex release in the
release record: the version guard compares with its commit, and the next
`VERSION` (step 1) moves with the first commit that changes a shipped file.

## What ran these commands

Each row names a block of commands above and says what runs it: a stage of
`./run_ci_suite.sh`, with the step for stage 10; the suite itself; nothing
in the suite, for the suite's own prerequisites; or only a real PubDex
release, and then what covers it instead. Stage 10's steps are those of
[`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *The rehearsal of a
PubDex release*; stage 1 is `./run_checks.sh`. A test in stage 1 keeps this
table in agreement with the blocks above and with stage 10.

| Block | Run by | Notes |
|---|---|---|
| `python3 DB/release_record.py preceding` | stage 1 | `run_checks.sh` runs the version guard; [`DB/tests/test_release_record.py`](DB/tests/test_release_record.py) runs the reader on the committed record and its commands, `preceding` among them, on fixture records |
| `.venv/bin/python DB/check_release_notes.py` | stage 1 | `run_checks.sh` runs it; [`DB/tests/test_release_notes.py`](DB/tests/test_release_notes.py) runs it on fixture trees for every rule |
| `./setup_venv.sh` | not run by the suite | the suite's prerequisites: it stops before stage 1, naming the command, when one is missing |
| `./run_ci_suite.sh --base` | the suite | the complete suite, as public CI runs it |
| `git tag --annotate` | release only | [`DB/tests/test_release_script.py`](DB/tests/test_release_script.py), in stage 1, shows a PubDex release refused, with a stubbed `git`, without its annotated tag, with a lightweight tag or one at another commit, and with a tag moved after the build |
| `git clone --branch` | release only | a fresh clone at the tag; stage 10 builds from the checkout as it is |
| `./release.sh build --source` | stage 10, step 2 | as test release A, with `--test` and `--out`, without `--source`, from the checkout as it is; test releases use the cache; the refusals only a PubDex release meets, and its build without the cache, are tested with stubs in stage 1 ([`DB/tests/test_release_script.py`](DB/tests/test_release_script.py)) |
| `./release.sh verify dist/pubdex` | stage 10, step 3 | the directory of test release A's first build; step 4 builds again and compares |
| `./run_stack_acceptance.sh --release` | release only | stage 6 runs `run_stack_acceptance.sh`, and stage 7 `run_operator_acceptance.sh`, with the images they build from the checkout, without these options; `--release` ran for the published 0.1.0 images ([`TASK_2A.md`](TASK_2A.md), *Published-image evidence*) |
| `git push origin v<version>` | release only | the suite pushes nothing |
| `./release.sh publish` | release only | [`DB/tests/test_release_script.py`](DB/tests/test_release_script.py), in stage 1, runs `publish` against a stubbed `docker` and registry: every refusal, the PostgreSQL image pushed first, `DIGESTS`, a failed push and a completion-record write failure with cleanup and a retry without pushes; a second `publish` that completes an interrupted publication, and its refusals of a version the registry holds as another image or as one whose digest the verified image does not record; stage 10, step 6, shows that the rehearsal published nothing |
| `DOCKER_CONFIG="$(mktemp -d)" ./run_stack_acceptance.sh` | release only | ran in this form for the published 0.1.0 images ([`TASK_2A.md`](TASK_2A.md), *Published-image evidence*) |
| `DB/verify_release.py files` | stage 1 | [`DB/tests/test_verify_release.py`](DB/tests/test_verify_release.py) runs the checker on complete synthetic downloads, missing files and corrupt content, with `IMAGES` absent; nothing in the suite downloads from a release page |
| `mkdir releases/<version>` | release only | the record commit's files |
| `python3 DB/release_record.py entry` | stage 1 | [`DB/tests/test_release_record.py`](DB/tests/test_release_record.py) checks every committed entry with the reader; `run_checks.sh` is stage 1 |
