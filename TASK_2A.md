# Task 2a — container image and Compose deployment

**Version 0.1.0 published on 2026-11-03. Public CI and anonymous release
acceptance passed.**

The release source is commit
[`57a72dc9f27766007b51c3d16ac7aa8c6ff3798b`](https://github.com/CCU-UKN/pubdex/commit/57a72dc9f27766007b51c3d16ac7aa8c6ff3798b).
Its clean-clone seven-stage suite passed locally on 2026-10-30 and in
[public CI](https://github.com/CCU-UKN/pubdex/actions/runs/41442310444) on
2026-11-02. Both versioned images are public; anonymous retrieval and
release acceptance by their exact digests passed on 2026-11-03
(*Published-image evidence*). This documentation was updated after those
runs; the results belong to the source commit and images named here.
External acceptance of task 2a is not asserted.

The release includes psycopg's C implementation built against Debian's
libpq, separate download and installation steps, and PostgreSQL built
from source without SQL/XML. Earlier working-tree and private Forgejo
results remain attached to the commits and images actually tested below.

On 2026-10-30 the maintainer adopted the custom PostgreSQL image for the
initial versioned release, to be preserved unchanged with dated verification
and known limitations. No fixed maintenance schedule or security-update
deadline is promised; future updates, if provided, will use new versions
([`DEPLOYMENT.md`](DEPLOYMENT.md), *The PostgreSQL image*).

Project page: [PubDex at NLnet](https://nlnet.nl/project/PubDex/). The
earlier funded results are recorded in [`TASK_1A.md`](TASK_1A.md) and
[`TASK_1B.md`](TASK_1B.md).

## The agreed task

> We'll publish a versioned container image for the ingestion and API
> service, plus a one-command Compose deployment with PostgreSQL and PubDex
> on portable defaults. Configuration, secret injection, health checks and
> persistent volumes will be documented. CI will start the stack from a
> clean clone and run a small ingest.

Five states are kept apart, and each row below says which one it concerns:

- **repository implementation** — verified in a clean clone of release
  commit `57a72dc9f27766007b51c3d16ac7aa8c6ff3798b` on 2026-10-30,
  including the two-image build, release acceptance and operator workflow;
- **self-hosted Forgejo** — the Forgejo workflow passed for earlier
  provisional commit `a0219be7` on 2026-10-29, for its first push, a pull
  request and an ordinary push, on a
  private instance on the maintainer's workstation (*Self-hosted Forgejo
  evidence*). It is not publicly accessible, so it complements public CI
  and does not replace it;
- **published images** — both 0.1.0 images public, their tags and digests
  checked without credentials, every layer downloaded and hash-verified,
  and release acceptance passed by digest on 2026-11-03;
- **public CI** — the `DB tests` workflow passed all seven stages for the
  release source commit on 2026-11-02; stages 6 and 7 start the stack;
- **external acceptance** — not asserted here.

## What task 2a adds

- **One application image** ([`Dockerfile`](Dockerfile)) holding every
  ingestion command and a preliminary read service with liveness, readiness
  and one paged list endpoint, built by
  [`build_image.sh`](build_image.sh) with its release version
  ([`VERSION`](VERSION), `0.1.0`), source revision and build time in its
  labels, from an allowlist build context ([`.dockerignore`](.dockerignore)).
  It carries the database's schema bootstrap ([`DB/init/`](DB/init/)) and
  the Compose files, so the image and `compose.yaml` are the whole
  deployment, with no checkout at run time — since 2026-10-30 with its
  second image, the **PostgreSQL image** ([`postgres.Dockerfile`](postgres.Dockerfile)):
  PostgreSQL 16 built from its verified source without SQL/XML, which
  `compose.yaml` runs and `build_image.sh` builds with the application
  image.
- **A preliminary read service**, [`DB/people_pubs/api/`](DB/people_pubs/api/):
  `GET /livez`, `GET /readyz` and `GET /v0/publications`, a bounded, paged,
  read-only list of the canonical publications with the CSV export's fields,
  as `app_readonly`. It is the foundation of the read API of task 3a, not
  that API: no people endpoints, filtering, stable schema, OpenAPI document,
  provenance or coverage fields, or compatibility promise — everything under
  `/v0/` may change ([`DB/README.md`](DB/README.md), section 11).
- **A Compose deployment** ([`compose.yaml`](compose.yaml)), the canonical
  way to run PubDex, of PostgreSQL and
  the read service, with ingestion and the synthetic demonstration on
  request, file-based secrets ([`create_secrets.sh`](create_secrets.sh)),
  the schema and the role passwords set up on the first start from the
  image's copy of `DB/init/`, put in place by a one-shot `bootstrap` service
  ([`DB/init/020_role_passwords.sh`](DB/init/020_role_passwords.sh)), health
  checks, a named volume, a documented backup and restore, and portable
  defaults, documented in [`DEPLOYMENT.md`](DEPLOYMENT.md);
  [`DB/compose.yaml`](DB/compose.yaml) stays the development and walkthrough
  setup.
- **Its acceptance test** ([`run_stack_acceptance.sh`](run_stack_acceptance.sh)),
  stage 6 of [`run_ci_suite.sh`](run_ci_suite.sh): it builds the image from
  the checkout — or takes a given image — starts the stack in a project of
  its own, runs a small ingest inside it and reads the result over HTTP.
- **The test of the operator's workflow**
  ([`run_operator_acceptance.sh`](run_operator_acceptance.sh)), stage 7: the
  procedures of [`DEPLOYMENT.md`](DEPLOYMENT.md) as written — the stack
  configured by an `.env` file on the ordinary network, a private roster
  imported on standard input, an ingestion of the bundled payloads, the
  refresh, the read service and the export, password changes with
  `set_passwords.sh` for files overwritten and replaced, a dump restored into
  a new stack, and the documented role model.

## The one command

Three steps prepare a clone, once, before the first start: `./create_secrets.sh`
creates the secret files, `./build_image.sh` builds the application and
PostgreSQL images from the clone, and `.env` names them in
`PEOPLE_PUBS_IMAGE` and `PEOPLE_PUBS_POSTGRES_IMAGE` in place of the
release `compose.yaml` names. With the published release, no clone is
needed: the Compose file is taken out of the image, which is pulled by
version and digest, and `.env` names it ([`DEPLOYMENT.md`](DEPLOYMENT.md),
*Selecting a published image*). Then one command starts PostgreSQL and the
read service and returns when both are healthy:

```bash
./create_secrets.sh                              # once
./build_image.sh                                 # once
echo 'PEOPLE_PUBS_IMAGE=pubdex:local' >> .env    # once
echo 'PEOPLE_PUBS_POSTGRES_IMAGE=pubdex-postgres:local' >> .env    # once
docker compose up --wait                         # the one command
```

It needs Linux on x86_64, Bash and Docker Engine 28.0.1 or later, except
28.2.0 to 28.3.2, with the Compose plugin 2.24.0 or later; building the
image also needs Git and the buildx plugin. Older Engines start the stack as
well, but there hosts on the same local network can reach the read service's
loopback port, so they are not supported ([`DEPLOYMENT.md`](DEPLOYMENT.md),
*What you need*; *Supported platforms* says what was tested and what is not
supported). No SQL
step or roster is needed: a one-shot service copies the schema files the
image carries into a volume, and on an empty database volume PostgreSQL runs
them and gives the two application roles their passwords from the secret
files. `docker compose up` creates no secret file and never builds the
application image; it pulls it when Docker does not have it, so with a
published image the secret files are the only preparation, and the image
carries `compose.yaml`, `create_secrets.sh` and `set_passwords.sh` for a
directory without a clone.
[`DEPLOYMENT.md`](DEPLOYMENT.md) opens with this short path; its *Setting
up* says exactly what each step does.

## Acceptance matrix

| Required | Where it lives | How to verify | Expected result | State |
|---|---|---|---|---|
| A container image … for the ingestion and API service | [`Dockerfile`](Dockerfile), [`.dockerignore`](.dockerignore), [`build_image.sh`](build_image.sh), the locks [`DB/requirements-pip.txt`](DB/requirements-pip.txt), [`DB/requirements-build.txt`](DB/requirements-build.txt) and [`DB/requirements.txt`](DB/requirements.txt), the downloader [`DB/fetch_verified.py`](DB/fetch_verified.py), the installers [`DB/install_pip.py`](DB/install_pip.py) and [`DB/install_locked.py`](DB/install_locked.py); the PostgreSQL image [`postgres.Dockerfile`](postgres.Dockerfile); `python -m people_pubs.api` and `python -m people_pubs.sync.<command>` | `./build_image.sh`; stage 6 runs the read service and the ingestion from the image and checks its files and Python packages as user 10001; [`DB/tests/test_container_files.py`](DB/tests/test_container_files.py) pins the build inputs | one image for the ingestion commands and the preliminary read service, carrying the schema bootstrap of `DB/init/`, `compose.yaml`, `create_secrets.sh` and `set_passwords.sh` under `/opt/pubdex`; non-root user 10001; the base image fixed by digest; every locked file downloaded in a stage of its own and kept only by its hash, so that a changed file fails the build; pip and every Python package installed without network from binary wheels, but psycopg-c, built from its verified source against Debian's libpq in a stage with a compiler that does not reach the image; pip removed again after `pip check`, with the wheels `ensurepip` would install it from, so that neither is in the image's file system; no credential, `.git`, environment file, data or local note in the build context or a layer | verified locally |
| … versioned | [`VERSION`](VERSION) (`0.1.0`), the OCI labels set by `build_image.sh`; a deployment is that image with the `compose.yaml` it carries, which names the release by its version tag ([`DEPLOYMENT.md`](DEPLOYMENT.md), *What identifies a deployment*) | `docker image inspect --format '{{json .Config.Labels}}' <image>`; stage 6 prints the labels, checks the version and revision of its build and that the build carries the checkout's `compose.yaml` and `DB/init/`; `--expect-version`, `--expect-revision` and `--release` check a given image and, with `--release`, start the `compose.yaml` it carries; [`DB/tests/test_build_image_script.py`](DB/tests/test_build_image_script.py) checks that the version comes from `VERSION`, [`DB/tests/test_stack_acceptance_script.py`](DB/tests/test_stack_acceptance_script.py) the identity checks | version `0.1.0`, revision the source commit (`<commit>-dirty` for a modified tree, and then no version tag), transform version separate; a missing, `unknown`, malformed or other label, a `-dirty` image, an image without its `compose.yaml` or a release whose `compose.yaml` names another version refused before the stack starts | verified locally |
| We'll publish … | [`DEPLOYMENT.md`](DEPLOYMENT.md), *Publishing an image* — a manual procedure for any OCI registry; PubDex's own images at `ghcr.io/ccu-ukn/pubdex` and, since 2026-10-30, `ghcr.io/ccu-ukn/pubdex-postgres` | in a clone: `./run_stack_acceptance.sh --release --pull --image ghcr.io/ccu-ukn/pubdex:0.1.0@sha256:<digest> --postgres-image ghcr.io/ccu-ukn/pubdex-postgres:0.1.0@sha256:<digest> --expect-version 0.1.0 --expect-revision <commit>`, without credentials | version tags and recorded digests of both images, `linux/amd64`, the revision of the release commit, no `latest` tag; the images passing as a release with the `compose.yaml` the application image carries | **passed 2026-11-03** — both published digests checked anonymously; release acceptance passed (*Published-image evidence*) |
| … the API service | [`DB/people_pubs/api/`](DB/people_pubs/api/), [`DB/README.md`](DB/README.md) section 11 | stage 1 (138 offline tests), stage 5 (integration), stage 6 over HTTP | the demonstration's publication with the export's fields, as `app_readonly`, read-only, from a preliminary service with liveness, readiness and one paged list endpoint under `/v0/`, which may change | verified locally, for that preliminary service; the versioned read API is task 3a and not claimed here |
| a one-command Compose deployment with PostgreSQL and PubDex | [`compose.yaml`](compose.yaml) | `docker compose up --wait` after the one-time steps above; stage 6, step 3; for an image in a registry, the files taken out of the image and the secret files, then the same command (*Local verification record*, 2026-10-27) | both services healthy from an empty volume, with no manual SQL; the application image pulled when Docker does not have it | verified locally |
| on portable defaults | [`compose.yaml`](compose.yaml): no fixed container, network or volume name, the read service on 127.0.0.1, PostgreSQL on no host port, settings from the environment; [`DEPLOYMENT.md`](DEPLOYMENT.md), *Portable defaults*, says what they cover and what not, and *Supported platforms* where the deployment is supported; the version requirement in [`DB/lib/docker_versions.sh`](DB/lib/docker_versions.sh) | stage 6, step 3, and stage 7, step 3, which read the port bindings Docker reports; [`DB/tests/test_container_files.py`](DB/tests/test_container_files.py), [`DB/tests/test_docker_versions.py`](DB/tests/test_docker_versions.py); the prerequisite checks of the suite and of both acceptance tests; the configurations tested, each with its date, in *Supported platforms* | runs as a project of its own next to other stacks; the read service bound to 127.0.0.1 alone; Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2 — the releases Docker documents as keeping other hosts away from ports published on loopback — and the Compose plugin 2.24.0 required, other versions refused before anything is created; Linux on x86_64 with the `linux/amd64` image the only platform built, tested and supported | verified locally, as configuration, with Docker Engine 29.9.2: no test connects from another host, and that other hosts are refused rests on Docker's documentation of those releases; arm64, amd64 emulation, Docker Desktop, rootless Docker, user-namespace remapping, SELinux-enforcing hosts and Podman neither tested nor supported |
| Configuration … documented | [`DEPLOYMENT.md`](DEPLOYMENT.md), *Settings*; [`DB/README.md`](DB/README.md) section 11 | Markdown check (stage 1) and review; stage 7, steps 2, 3 and 9: an `.env` file setting the project name, image, a relative secrets directory, the port and the log level | every setting with its default and effect | done |
| secret injection … documented | Compose file secrets, [`create_secrets.sh`](create_secrets.sh), [`set_passwords.sh`](set_passwords.sh), `PEOPLE_PUBS_API_DB_PASSWORD_FILE`, `PEOPLE_DB_PASSWORD_FILE`; [`DEPLOYMENT.md`](DEPLOYMENT.md), *Secrets*, *Changing a password* and *Roles and personal data* | stage 6, steps 2 and 8; stage 7, steps 6, 7 and 9: password changes with `set_passwords.sh` for files overwritten in place and replaced by rename, checked with new connections, and the role model; stage 1 (offline tests of the scripts and the password-file format) | each container only its own role's password, from a file; none in a container's configured environment, the database server's environment, a command line, build argument, image, rendered configuration, log or `docker inspect` output; a changed file applied by `set_passwords.sh`, which changes nothing until its checks pass and confirms each role by a SCRAM-authenticated connection, without a restart of PostgreSQL; what the file storage does not protect is stated, and that an ingestion command hands its password to libpq in its own environment | verified locally |
| health checks … documented | `/livez`, `/readyz`, the Compose health checks; [`DEPLOYMENT.md`](DEPLOYMENT.md), *Health and readiness* | stage 6, steps 3, 4 and 6; stage 5 | liveness without the database; readiness 503 while PostgreSQL is down and 200 again after it is back, without a restart | verified locally |
| persistent volumes … documented | the named volume `<project>_postgres-data`; [`DEPLOYMENT.md`](DEPLOYMENT.md), *Starting, stopping and removing* and *Data and storage*, with *Backup and restore* | stage 6, step 7; stage 7, step 8 | the same records and role passwords after `--force-recreate` and after `down` and `up`, with no new initialisation; only `down --volumes` deletes the database; a dump restored into a new stack serves the same records, with the roles' privileges | verified locally |
| CI will start the stack from a clean clone | [`run_ci_suite.sh`](run_ci_suite.sh) stage 6, [`run_stack_acceptance.sh`](run_stack_acceptance.sh); the workflows [`.github/workflows/db-tests.yml`](.github/workflows/db-tests.yml) and [`.forgejo/workflows/db-tests.yml`](.forgejo/workflows/db-tests.yml), adapters with the steps of task 1b, `.venv` now set up by [`setup_venv.sh`](setup_venv.sh) | `./run_ci_suite.sh`; public CI of the pushed commit | `PASS: all 7 stages of the complete verification suite passed in <n>s.` | **passed 2026-11-02** — all seven stages on the release source commit (*Public-CI evidence*) |
| … and run a small ingest | `docker compose run --rm demo` inside the stack: the synthetic roster and the bundled ORCID and Crossref payloads; the documented roster import on standard input, the refresh and the export | stage 6, step 5; stage 7, steps 4 and 5 | ingested as `app_writer` with no metadata-provider request; `GET /v0/publications` serves `10.5555/pubdex-fixture-001` with its expected fields; cumulative provenance `crossref+orcid` in the database | verified locally |

## The acceptance test

`./run_stack_acceptance.sh` is the end-to-end evidence for the rows above;
[`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *The Compose
deployment's acceptance test*, lists every check. In short, from a clean
clone, with Docker, Bash and Python 3:

1. it builds the image from the checkout with `build_image.sh`, under a tag
   of its own, and checks its version and revision labels — or takes the
   image `--image` names, pulled with `--pull`, checked for its digest and,
   with `--expect-version` and `--expect-revision`, exactly those labels,
   and never rebuilt or replaced; it prints the image's identity and reads
   the `compose.yaml` the image carries: without `--release` it starts the
   checkout's and says whether the image carries the same, with `--release`
   the image's own;
2. creates the secret files with `create_secrets.sh` in a private
   temporary directory, and a project directory holding that `compose.yaml`
   alone;
3. starts it with `docker compose up --wait` on empty volumes, in the
   Compose project `pubdex-acceptance-<run id>`, and checks the bootstrap
   from the schema the image carries, that nothing but the project's
   volumes and the secret files is mounted, the image the read service
   runs, its binding to a loopback port alone as Docker reports it — no
   connection from another host is tried — and the internal network;
4. queries `/livez`, `/readyz` and the empty list over HTTP;
5. runs `docker compose run --rm demo` — the synthetic roster, the bundled
   ORCID and Crossref payloads and the attribution rule, as `app_writer` —
   and reads the publication over HTTP, as `app_readonly`, and its
   provenance in the database;
6. stops and starts PostgreSQL: readiness fails and recovers;
7. recreates every container on the same volumes, then runs `down` and `up`:
   the same records, no new initialisation;
8. checks that `create_secrets.sh` keeps the files on a rerun, that the
   environments of the database server and of the read service hold no
   password setting, and that no password occurs in the logs of the stack's
   containers — saved before each recreation — or any other output.

The demonstration needs no metadata provider, and the test makes sure of
it: PostgreSQL and the ingestion containers run on an internal Docker
network without a route off the host, which a test-only addition to the
Compose file, [`DB/tests/compose.acceptance.yaml`](DB/tests/compose.acceptance.yaml),
sets up; the deployment itself keeps its ordinary network, so an operator's
ingestion can reach the providers. Docker does need network access to pull
the PostgreSQL image of `compose.yaml` and, for the build, the base image
and the Python packages, unless it has them.

The run owns everything it creates — every container, network and volume
carries its run label — refuses to start when its project, label or image
tag exists already, binds every Compose call to its own project, files and
settings in a cleared environment, and removes all of it afterwards, also
after a failure or an interrupt, confirming that nothing is left and failing
when it cannot. [`DB/tests/test_stack_acceptance_script.py`](DB/tests/test_stack_acceptance_script.py)
proves that lifecycle offline.

`./run_operator_acceptance.sh` runs the operator's side of the same
deployment, in a project of its own configured by an `.env` file, on the
ordinary network — a backup restored into a new stack among its steps — and
contacts no metadata provider by its choice of commands; [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *The
operator's workflow test*, lists its checks, and
[`DB/tests/test_operator_acceptance_script.py`](DB/tests/test_operator_acceptance_script.py)
proves its lifecycle offline.

## Image, platform and publication

- **Version.** The release version is the content of [`VERSION`](VERSION),
  `0.1.0`, recorded in the label `org.opencontainers.image.version`. It is
  independent of `people_pubs.__version__` (1.5.3), the transform version
  that ingestion stamps into source provenance, which the image carries as
  `pubdex.transform-version`.
- **Revision.** `org.opencontainers.image.revision` is the commit the image
  was built from; a build of a modified working tree is labelled
  `<commit>-dirty` and cannot carry the version as its tag. A release image
  is built from a clean checkout of the release commit, so an image of this
  result has to be built again once that commit exists.
- **Deployment identity.** The image holds the bootstrap files of
  `DB/init/` and the `compose.yaml` it was built with, under `/opt/pubdex`,
  so a deployment is identified by the image's version and digest alone,
  run with that `compose.yaml`. The file names its release by version tag,
  `ghcr.io/ccu-ukn/pubdex:0.1.0`, not by digest, which is known only after
  the commit it records exists; the digest comes from the release record and
  goes into `PEOPLE_PUBS_IMAGE`. `./run_stack_acceptance.sh --release`
  checks the labels and starts the `compose.yaml` the image carries;
  without `--release`, a run starts the checkout's `compose.yaml` with the
  image it is given, a local `<commit>-dirty` build included, and says
  whether the image carries the same ([`DEPLOYMENT.md`](DEPLOYMENT.md),
  *What identifies a deployment*).
- **Platform.** `linux/amd64`, the only platform built, tested and scanned.
  [`DEPLOYMENT.md`](DEPLOYMENT.md), *Supported platforms*, says which hosts
  are supported, what ran on which, and when.
- **Reference.** A published image is selected by version tag and digest,
  `ghcr.io/ccu-ukn/pubdex:0.1.0@sha256:<digest>`, in the GitHub container
  registry next to the canonical repository, and since 2026-10-30 the
  PostgreSQL image the same way, `ghcr.io/ccu-ukn/pubdex-postgres:0.1.0@sha256:<digest>`;
  no `latest` tag is published.
  Pulling needs no account, and the image is never the only source: a
  clone builds an equivalent one with `build_image.sh`, and another
  registry can carry a copy by digest.
- **Publication** is the maintainer's manual step, in
  [`DEPLOYMENT.md`](DEPLOYMENT.md), *Publishing an image*, taken once the
  commit holding this result is published and not before: build both
  images from a clean clone at that commit, verify them as a release, push
  the version tags, record the digests and the commit, then verify the
  pulled images again, by digest and without credentials.
  Release automation, checksums, provenance attestations, an SBOM and
  upgrades between releases belong to task 2c.

## Local verification record

On 2026-10-30 a clean clone of the final release source,
`57a72dc9f27766007b51c3d16ac7aa8c6ff3798b`, passed all seven stages in
374 seconds: 1407 offline tests passed with 125 expected skips, followed
by 122 integration tests and 27 checks after patch reapplication, none
skipped. The newly built PostgreSQL 16.15 image was selected explicitly
for stages 3–5. The source-range and secrets checks passed. The host used
CPython 3.11.17, the documented pure-Python setup with libpq 16.15 and
required network isolation, Docker Engine 29.9.2 and Compose 5.6.1.

The build from that clone produced both release images with version 0.1.0
and that exact revision, with no `-dirty` suffix. Their local
image IDs were application
`sha256:b3f67af382c60caf9b29f2104116d1665738e9e8d3d6ae64fc8229e74f24b625`
and PostgreSQL
`sha256:bdcd1cb38aa5a710d6796df9845f1f45156a2df5cfce992272d565272bdf518c`.
Release-mode deployment and the operator workflow passed separately on
this retained pair, including password changes and backup restoration.
These are the images subsequently published below. The clone stayed
clean and all pre-existing containers, volumes and networks were kept.

### Earlier working-tree verification

An earlier full run was on an uncommitted working tree of
`a0219be7bab86ce3fc53262f52089ac453589de8` on 2026-10-30. Every image
built there carries that commit's revision with `-dirty`; `--release`
refuses it. Later documentation edits do not constitute a new full-suite
run. These are historical working-tree results, separate from the final
commit and published-image results.

The host was Linux x86_64 (Ubuntu 24.04.5), with Docker Engine 29.9.2 and
BuildKit 0.34.0. The development environment used CPython 3.11.17 and
`./setup_venv.sh --psycopg python`, loading the host's libpq 16.15. pip
ran in a network namespace. The default C setup also passed in a
disposable Ubuntu 24.04 container with that Python and libpq-dev 16.15;
its connection tests passed, and it reported that network isolation was
unavailable there. That container result is not a run on either CI host.
The application image itself requires psycopg's C implementation and loads
Debian's libpq 17.11.

| Command or check | Result |
|---|---|
| `./run_checks.sh` | 1407 passed and 125 expected skips on the tested working tree |
| `DISPOSABLE_PG_IMAGE=<PostgreSQL image built from the tree> ./run_ci_suite.sh --base 6fa0318328547be3dbb9b234c3f392b452a363c3`, 06:46–06:53 UTC | All seven stages passed in 375 seconds; 1407 offline tests passed, 125 expected skips; 122 integration tests and then 27 passed on PostgreSQL 16.15. The transform-version guard accepted 1.5.1 → 1.5.3. The secrets check passed over the then-current 231-file tree and the range's one commit. Nothing was left in Docker |
| Stage 6 deployment acceptance | Passed on application image `sha256:8737467a3b1ac6d55cbee65e6a2738de0d60c6fde8eb9f9687c9f5329c07b0de` and PostgreSQL image `sha256:ca9f8f8869b0d9021e880ea9b69e7d54a0d8546928d935720613f91d4354c24e`; SQL/XML unavailable |
| Stage 7 operator workflow | Passed on application image `sha256:e8544f9ddd67af4f281bf862c5818c2424ff9b7775d4ef6639a1c63e7d736312` and PostgreSQL image `sha256:c1351cfbcb8f21d95036b6e686bce8311f885ab18e9dee513aa8be2514f40545` |
| Separate `./build_image.sh --no-cache`, built 06:39:14 UTC | Application image `sha256:04d9bce2f3df304ec6104fe56dcc2ea4704ce52e3f0b48eaee7ff86ac282db3d` and PostgreSQL image `sha256:823472c0b26e1eb07587249691bb43828d0e25a844ac9246be5d7f11a42fd210`, both linux/amd64 |
| Stack and operator acceptance with `--image` and `--postgres-image` on that separate pair | Both passed; password changes and dump restoration included |
| `--release` on that pair | Refused as dirty builds, before startup |
| Real-server connection check | SCRAM accepted; requests for MD5 or cleartext authentication, and a trust login offered a password, refused; without a password only a server requesting no authentication accepted |
| Image contents | C implementation required, no compiler, headers or pip; PostgreSQL 16.15 without SQL/XML; schema extensions and the entrypoint licence present |

On an earlier build of the same PostgreSQL server, all 220 PostgreSQL
regression tests passed. Synthetic-data compatibility tests confirmed
matching server settings, catalog properties, collation versions and
checksums, starting on an official-image volume, and dump restoration in
both directions. The final build's server files and Debian packages were
byte-identical to the build carrying the licence notices. No existing
installation's database was used.

Earlier on 2026-10-29, a fresh clone of provisional local commit
`a0219be7bab86ce3fc53262f52089ac453589de8` passed all seven suite stages
in 293 seconds: 1328 offline tests passed, 125 expected skips, and 122
integration tests followed by 27. Deployment release acceptance and the
operator workflow passed on image
`sha256:d67065d60ce8c5d55f2243a1311ae9618403168c39552d138fe3b909c57314ca`,
and again on a later build,
`sha256:ce9b67640ff7f8cb4524f70a7e0415ac197f36b35bda6d99bf8e9996b5178491`.
Those results predate the two-image build and the dependency changes of
2026-10-30. The private Forgejo workflow also passed for that provisional
commit (*Self-hosted Forgejo evidence*). Neither that commit's results nor
the dirty working-tree runs above are results of the final publication
commit. Those earlier images were not published.

## Published-image evidence

Published and verified without credentials on 2026-11-03:

| Image | Version tag | Registry manifest digest |
|---|---|---|
| Application | `ghcr.io/ccu-ukn/pubdex:0.1.0` | `sha256:24d570015b20f222c767a8bb89dd18d792abbd25cc730137f7b8bc285df6a325` |
| PostgreSQL | `ghcr.io/ccu-ukn/pubdex-postgres:0.1.0` | `sha256:33988fb179e2065e653ad7813cecf812c21f4381537b8b8db546ad37236b780a` |

Both are `linux/amd64`, version 0.1.0, revision
`57a72dc9f27766007b51c3d16ac7aa8c6ff3798b`. Their registry manifests'
config digests match the retained image IDs above. The version tags resolve
to these manifest digests; the release is preserved unchanged.

Without account credentials, both manifests, configuration blobs and all
23 compressed image layers were downloaded and checked against their
SHA-256 digests (57,274,990 application-layer bytes and 66,884,680
PostgreSQL-layer bytes). This check did not use Docker's local image cache.

In the clean clone at the release source commit, with an empty Docker
configuration, this command passed using the Compose file carried inside
the application image:

```bash
./run_stack_acceptance.sh --release --pull \
  --image ghcr.io/ccu-ukn/pubdex:0.1.0@sha256:24d570015b20f222c767a8bb89dd18d792abbd25cc730137f7b8bc285df6a325 \
  --postgres-image ghcr.io/ccu-ukn/pubdex-postgres:0.1.0@sha256:33988fb179e2065e653ad7813cecf812c21f4381537b8b8db546ad37236b780a \
  --expect-version 0.1.0 --expect-revision 57a72dc9f27766007b51c3d16ac7aa8c6ff3798b
```

Docker resolved and pulled both digests anonymously; its layer store
already held the verified image contents. The independent full-layer
download above checked anonymous access to all image bytes. On Ubuntu
24.04.5, kernel 6.8, Engine 29.9.2 and Compose 5.6.1, release acceptance
verified the labels, runtime packages, synthetic ingest and read service,
readiness through a database restart, persistence through recreation and
secret handling. Cleanup left no added containers, volumes or networks;
the clone remained clean. Other platform limits are unchanged
([`DEPLOYMENT.md`](DEPLOYMENT.md), *Supported platforms*).

## Public-CI evidence

- Source commit: `57a72dc9f27766007b51c3d16ac7aa8c6ff3798b`.
- [`DB tests` push run, attempt 1](https://github.com/CCU-UKN/pubdex/actions/runs/41442310444),
  completed successfully on 2026-11-02.
- All seven stages passed in 552 seconds: 1407 offline tests passed with
  125 expected skips, then 122 integration tests and 27 checks after patch
  reapplication, none skipped. Stages 6 and 7 built both images from the
  checkout and passed deployment and operator acceptance with cleanup.
  Those CI builds are separate from the retained images published above.
- GitHub-hosted Ubuntu 24.04, CPython 3.11.18, pip 26.2.2, psycopg 3.3.6
  C implementation with libpq 18.6, Docker Engine 28.0.4, Compose 2.38.2.
  The default C setup passed. Its installers reported that no network
  namespace was available: pip used `--no-index`, without a network
  sandbox on that runner. The image build's offline installation remains
  a separate check.

This evidence identifies the release source commit. A later documentation
commit does not change either published image's revision or make this
pipeline a run of that later commit.

## Self-hosted Forgejo evidence

Recorded on 2026-10-29. Runs of
[`.forgejo/workflows/db-tests.yml`](.forgejo/workflows/db-tests.yml) on a
private Forgejo instance on the maintainer's workstation, with a Forgejo
Runner on the same workstation that executes each job directly on it under
the host label `pubdex-ci-host`. The instance is not publicly accessible, so
this is the maintainer's record of those runs, not CI evidence anyone can
open; it complements *Public-CI evidence* and does not replace it. It was
written after the runs, in a later change: the runs tested the commit named
here, not the commit that records them.

- Commit tested: `a0219be7bab86ce3fc53262f52089ac453589de8`, the commit of
  the *Local verification record*, one commit on the published `6fa0318`;
  it carries `.forgejo/workflows/db-tests.yml` as blob `33658170d303`.
- Forgejo 16.0.5, from its rootless container image, and Forgejo Runner
  13.2.0.
- Runner: on the machine of the *Local verification record*, running each
  job on the host, with CPython 3.11.17 — not the 3.11.18 of the image and
  of the GitHub workflow — pip 26.2.2 installed by `DB/install_pip.py`, Git
  2.43.0, Docker Engine 29.9.2 and Docker Compose 5.6.1. Stages 3–5 ran on
  PostgreSQL 16.10 (`postgres:16`, digest `sha256:21f6013073bc`), the image
  the runner's Docker had, and stages 6 and 7 on the PostgreSQL 16.15 build
  `compose.yaml` pins.

Each run was started by Forgejo for its event, tested that commit, and
succeeded at its first attempt:

| Trigger | Range the suite received | Range checks | Suite | Images of stages 6 and 7 |
|---|---|---|---|---|
| first push of a new branch | all-zero base | no range for the transform-version guard; the secrets check read the three commits reachable from the head | 336 s | `sha256:11e5ea26592b98ba520040b7d5633b5795eef7e595ed09bdf1d6ff9b60747a76`, `sha256:fb98c1b15cdbc37c1cf9af33e45823c26e9e81516a264afd340b336e96679411` |
| pull request into the branch holding `6fa0318` | from the pull request's merge base, `6fa0318` | version 1.5.1 → 1.5.3 accepted; the secrets check read the one commit the pull request adds | 303 s | `sha256:80c11b724d297dab9edaed57f78bd451db38e389722686a28acb7559456543d0`, `sha256:82119fe768e388b84ba5a0f28786aa4886d85e6bff4d20c54fa52a56f6a1bd0f` |
| push moving that branch from `6fa0318` to the tested commit | from the previous tip, `6fa0318` | as for the pull request | 309 s | `sha256:04db875ac54cb342fd3dd89151139f980abda8ae6f40ad9dc61a956a4fe667da`, `sha256:6547e8396cd17f36f32e7305dcb04d4f465eb05bf09dce6d67e6b4ec0ecb0b20` |

In each run all seven stages passed: 1328 offline tests passed and 125
skipped, every skip one the policy names; the secrets check over 221 files;
the migration smoke and the demonstration; 122 integration tests, then 27,
none skipped; and the acceptance test of the deployment and the test of the
operator's workflow, each on an image built from the commit, labelled with
it and with version 0.1.0. Each log shows `DB/install_pip.py` installing
the pip whose wheel hash `DB/requirements-pip.txt` lists, no other pip
running, and every package installed from a binary wheel. Afterwards no
container, volume, network or image tag of the suite's stages was left on
the workstation. The images of these runs were not pushed or scanned.

The working tree of the 2026-10-30 entry of the *Local verification
record*, which changes how both workflows set up `.venv` and adds the
PostgreSQL image to stages 6 and 7, has not run on that private instance.
The final release source passed public CI as recorded above.

## Verification limits

- **Other platforms** — supported is Linux on x86_64 with the Docker Engine
  and Compose releases of the requirement, running the `linux/amd64` image.
  `linux/arm64`, amd64 emulation, Docker Desktop, rootless Docker,
  user-namespace remapping, SELinux-enforcing hosts and Podman are neither
  tested nor supported ([`DEPLOYMENT.md`](DEPLOYMENT.md), *Supported
  platforms*).
- **Forgejo on a private instance only** — the Forgejo workflow ran the
  five stages of task 1b on a private instance on 2026-10-06, and the seven
  stages of this result, for `a0219be7`, on 2026-10-29 (*Self-hosted Forgejo
  evidence*). No run on a Forgejo or GitLab instance anyone can inspect has
  happened.

## Not part of this result

- **The versioned read API is task 3a**: people endpoints, filtering, stable
  schemas, machine-readable API documentation, provenance and coverage in
  the API, example clients. Write paths and the compatibility policy are
  task 3b.
- **Helm and cluster testing are task 2b.**
- **Release automation, checksums or SBOM, and upgrade and rollback between
  releases are task 2c.**
- **Scheduled backups, retention, broader operator tooling and recovery
  drills are task 4a.** This deployment documents a manual backup and
  restore with `pg_dump` and `pg_restore` ([`DEPLOYMENT.md`](DEPLOYMENT.md),
  *Backup and restore*); the backup, restore and refresh scripts in `DB/`
  belong to the development setup, `DB/compose.yaml`.
