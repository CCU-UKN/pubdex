# PubDex

PubDex keeps an auditable record of what the people of a research
organisation have published. It pulls publication metadata from ORCID,
Crossref, OpenAlex, DataCite, DBLP and Semantic Scholar into a local
PostgreSQL database, records the provenance of every field so you can always
tell which source a value came from, and resolves people and duplicate
versions of the same work into canonical records.

Curator corrections — merged people, name aliases, duplicate decisions,
detached authorships — are stored separately from the ingested data, so they
survive re-ingestion and even a full rebuild of the publication table.

## Origins and funding

PubDex grew out of a private prototype for publication reporting whose
original development predates the grant. Its ingestion, provenance,
deduplication, curation, export and maintenance code is the starting point of
this repository. Prototype functionality included in this baseline is not
claimed as funded work.

The first funded result is turning that prototype into this public
repository. The initial commit provides a curated, public-safe copy of the
code and documentation; the MIT and CC BY 4.0 licences; a secret-free example
configuration; synthetic fixtures and a demo roster; institutional attribution
through configuration instead of embedded organisation data, and the removal of
the institution-specific behaviour that was left in the application code; a
deterministic clean-clone demonstration that ingests bundled ORCID and Crossref
payloads and produces a verified export with no metadata-provider request;
fixes found while validating that walkthrough; and the project description, progress
record and funding acknowledgement. Before publication, the copy was checked
for secrets, private rosters, restricted-source data, database dumps and
machine-local state. The private prototype's history is deliberately not
published.

[`TASK_1A.md`](TASK_1A.md) is the acceptance record for that result: what was
required, which files carry it, and the command that verifies each part.
[`TASK_1B.md`](TASK_1B.md) does the same for the second one, the complete test
suite as one forge-portable command that public CI runs from a clean checkout.
[`TASK_2A.md`](TASK_2A.md) records the container image and the Compose
deployment, and [`TASK_2B.md`](TASK_2B.md) the Helm chart and its tests on a
disposable cluster. [`TASK_2C.md`](TASK_2C.md) records release automation,
the schema upgrade and upgrade and rollback tests between PubDex releases.

Later funded results will be introduced in their own commits and recorded in
[`CHANGELOG.md`](CHANGELOG.md) against the task they deliver and the way it
can be verified. Funded scope is determined from the agreed plan, not merely
from commit dates.

## Funding

This project was funded through the [NGI0 Commons
Fund](https://nlnet.nl/commonsfund/), a fund established by
[NLnet](https://nlnet.nl/) with financial support from the European
Commission's [Next Generation Internet](https://ngi.eu/) programme, under the
aegis of DG Communications Networks, Content and Technology under grant
agreement No 101135429. Additional funding is made available by the Swiss State
Secretariat for Education, Research and Innovation (SERI).

Project page: [PubDex at NLnet](https://nlnet.nl/project/PubDex/).

NLnet project number: 2026-02-585.

## Repository layout

| Path | What it is |
|---|---|
| [`DB/`](DB/) | The database and the application: the PostgreSQL schema ([`DB/init/`](DB/init/)), the development and walkthrough setup ([`DB/compose.yaml`](DB/compose.yaml)), the `people_pubs` Python package, its tests, and the operator scripts |
| [`DB/people_pubs/api/`](DB/people_pubs/api/) | The read service, `python -m people_pubs.api`: a preliminary, read-only HTTP endpoint for the canonical publication list, with liveness and readiness checks ([`DB/README.md`](DB/README.md), section 11) |
| [`compose.yaml`](compose.yaml), [`DEPLOYMENT.md`](DEPLOYMENT.md) | The canonical way to run PubDex: PostgreSQL, the read service and the ingestion commands as one Compose project, started with one command, on portable defaults; [`compose.postgres-port.yaml`](compose.postgres-port.yaml) publishes the database on request |
| [`Dockerfile`](Dockerfile), [`build_image.sh`](build_image.sh), [`VERSION`](VERSION) | The application image — the ingestion commands and a preliminary read service with liveness, readiness and one paged list endpoint (the foundation of the versioned read API of task 3a, not that API), run by an unprivileged user, with the schema bootstrap and the Compose files — its build with version and source labels, and the release version; [`.dockerignore`](.dockerignore) limits the build context to an allowlist |
| [`create_secrets.sh`](create_secrets.sh) | Creates the deployment's password files once and never overwrites them |
| [`set_passwords.sh`](set_passwords.sh) | Gives the running deployment's database the passwords its files hold now, after one was changed, and checks each with a new connection |
| [`run_checks.sh`](run_checks.sh) | The fast offline check suite: compilation, Markdown consistency, the release-note check, the version guard and the offline unit and fixture tests, plus the transform-version guard in pre-commit and CI modes. The pre-commit hook, and the first stage of `run_ci_suite.sh` |
| [`charts/pubdex/`](charts/pubdex/), [`KUBERNETES.md`](KUBERNETES.md) | The Helm chart, for a Kubernetes cluster: an evaluation mode with a PostgreSQL of its own, or a PostgreSQL server you run, from the same release images; the operator guide covers install, upgrade, rollback and uninstall, Secrets and settings, ingestion, ingress and TLS, and NetworkPolicy. [`charts/tests/`](charts/tests/) holds the chart's offline tests |
| [`VERSIONING.md`](VERSIONING.md) | For operators: what a PubDex version number promises, the schema changes and the schema upgrade, which upgrades and rollbacks between PubDex releases are supported and which the suite runs, the release record, and the supported platforms with the Kubernetes window |
| [`RELEASING.md`](RELEASING.md) | For maintainers: how a PubDex release is made, from its version and release notes through the build and verification at the tag to publication and the record commit, with the rules for a schema change; each command marked as run by the suite or only at a real PubDex release |
| [`release.sh`](release.sh) | Builds the artifact set of a PubDex release — the source archive, the Helm chart's package, both images, an SBOM of each, the provenance record and the checksum file — and verifies it; `setup` pulls the pinned SBOM tool; a separate `publish` step, which only the maintainer runs, pushes the two images |
| [`releases/`](releases/README.md) | The release record: for each published PubDex release its commit, tag, image digests, schema changes and checksum file, and in `FLOOR` the oldest release an upgrade may start from |
| [`setup_cluster_tools.sh`](setup_cluster_tools.sh) | Installs the helm, kubectl and kind that [`DB/lib/cluster_tools.sh`](DB/lib/cluster_tools.sh) pins under `.tools/`, each checked against its pinned SHA-256, for the Kubernetes checks |
| [`run_ci_suite.sh`](run_ci_suite.sh) | The complete verification suite in one command, and exactly what public CI runs: the offline suite, the check for committed secrets and local configuration, migration from an empty database, the task-1a demonstration, the integration suite on a disposable PostgreSQL, the acceptance test of the Compose deployment, the test of the operator's workflow on it, the offline checks of the Helm chart, the chart on a disposable Kubernetes cluster, the rehearsal of a PubDex release, and the upgrades between PubDex releases on Compose |
| [`run_stack_acceptance.sh`](run_stack_acceptance.sh) | The acceptance test of the Compose deployment: the image built from the checkout, or a published one, started with `compose.yaml` in a project of its own, a small ingest inside it, the result read over HTTP, readiness, persistence and secrets checked, and everything it created removed |
| [`run_operator_acceptance.sh`](run_operator_acceptance.sh) | The operator's workflow on the Compose deployment, as `DEPLOYMENT.md` gives it: an `.env` configuration on the ordinary network, a private roster imported on standard input, ingestion of the bundled payloads, refresh, read service and export, password changes with `set_passwords.sh` for files overwritten and replaced, a backup restored into a new stack, and the documented role model, checked by the records and connections they leave |
| [`run_chart_checks.sh`](run_chart_checks.sh) | The offline checks of the Helm chart with the pinned helm: lint, rendering of every values file and example, and the assertions on the manifests; stage 8 of `run_ci_suite.sh` |
| [`run_cluster_acceptance.sh`](run_cluster_acceptance.sh) | The Helm chart on disposable kind clusters of its own: in both modes, the install of the preceding PubDex release, the demonstration, reads over HTTP, `helm test`, the upgrade to this checkout with the schema upgrade, rollback, password changes, readiness, NetworkPolicy and uninstall, and a fresh install of this checkout's chart; in external mode, the upgrade from test release A to test release B1, built by `release.sh`, with its schema upgrade and an image rollback; then the same upgrade in evaluation mode on the oldest supported Kubernetes release as a user with only the rights the guide lists; and everything it created removed; stage 9 of `run_ci_suite.sh` |
| [`run_release_rehearsal.sh`](run_release_rehearsal.sh) | The rehearsal of a PubDex release, publishing nothing: [`release.sh`](release.sh) builds a test release of the checkout as it is, verifies it, builds it again and compares the source archive and the chart package byte for byte, then lints and renders the chart from its package file; stage 10 of `run_ci_suite.sh` |
| [`run_upgrade_acceptance.sh`](run_upgrade_acceptance.sh) | The upgrades between PubDex releases on the Compose deployment, with an operator's commands: test release A of the checkout to test releases B1 and B2, with an image rollback and a rollback by restore, and the published preceding release to A with an image rollback; stage 11 of `run_ci_suite.sh` |
| [`check_secrets_and_local_config.py`](check_secrets_and_local_config.py) | The check for committed secrets and local configuration over the publishable tree; findings name rule, file and line, never the matched text |
| [`DB/run_task_1a_demo.sh`](DB/run_task_1a_demo.sh) | The clean-clone acceptance demonstration: a disposable database, bundled ORCID and Crossref payloads, queries and a verified export, with no metadata-provider request |
| [`DB/run_disposable_integration.sh`](DB/run_disposable_integration.sh) | The complete integration suite against a PostgreSQL started, bound and removed for that run alone |
| [`CHANGELOG.md`](CHANGELOG.md) | The public progress record: what each funded result delivered, and how to verify it |
| [`TASK_1A.md`](TASK_1A.md) | The acceptance record for the first funded result, requirement by requirement |
| [`TASK_1B.md`](TASK_1B.md) | The acceptance record for the second funded result, the forge-portable test suite |
| [`TASK_2A.md`](TASK_2A.md) | The acceptance record for the container image and the Compose deployment |
| [`TASK_2B.md`](TASK_2B.md) | The acceptance record for the Helm chart and its tests on a disposable cluster |
| [`TASK_2C.md`](TASK_2C.md) | The acceptance record for release automation, the schema upgrade and upgrade and rollback tests; public CI and publication pending |
| [`DEVELOPMENT_METHODS.md`](DEVELOPMENT_METHODS.md) | How the initial public baseline was prepared, reviewed and verified |
| [`QUICKSTART.md`](QUICKSTART.md) | Clean clone to a running database with demo data, a query over what landed, and an export |
| [`QUICKSTART_CURATION.md`](QUICKSTART_CURATION.md) | Curation, aliases and reporting exports on top of that state |
| [`.github/workflows/db-tests.yml`](.github/workflows/db-tests.yml) | GitHub adapter: it checks out the repository, installs the dependencies and invokes `run_ci_suite.sh`. The checks themselves live in tracked scripts, so any forge can run them |
| [`.forgejo/workflows/db-tests.yml`](.forgejo/workflows/db-tests.yml) | The same adapter for Forgejo Actions, for a self-hosted runner that runs the job on its host ([`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *Forgejo Actions*) |
| [`LICENSE`](LICENSE), [`LICENSE-CC-BY-4.0`](LICENSE-CC-BY-4.0) | The two licences described below |

## Getting started

| Way | What runs | Guide |
|---|---|---|
| **The deployment, canonical** | PostgreSQL, the read service and the ingestion commands from one image, with [`compose.yaml`](compose.yaml) | [`DEPLOYMENT.md`](DEPLOYMENT.md) |
| A Kubernetes cluster | the same images, installed with the Helm chart in [`charts/pubdex/`](charts/pubdex/), with a PostgreSQL of its own or yours | [`KUBERNETES.md`](KUBERNETES.md) |
| The development and walkthrough setup | PostgreSQL from [`DB/compose.yaml`](DB/compose.yaml), the `people_pubs` package from a virtual environment on the host | [`QUICKSTART.md`](QUICKSTART.md) |

[`DEPLOYMENT.md`](DEPLOYMENT.md), *The development setup*, compares the two.

### Running PubDex as a service

On Linux x86_64, with Docker Engine 28.0.1 or later, except 28.2.0 to
28.3.2, the Compose plugin 2.24.0 or later and Bash. Version 0.1.0 is public;
[Selecting a published image](DEPLOYMENT.md#selecting-a-published-image)
shows how to run it without a clone, using both verified image digests.
Building the images from this checkout also needs Git and Docker's buildx
plugin.
Other Engines start the stack too, but can let other hosts on the local
network reach the read service, which has no authentication
([`DEPLOYMENT.md`](DEPLOYMENT.md), *What you need*). Other architectures,
Docker Desktop, rootless Docker, SELinux-enforcing hosts and Podman are
neither tested nor supported: *Supported platforms* in `DEPLOYMENT.md` lists
what is supported and what was tested.

To build from source and start the deployment:

```bash
./create_secrets.sh                              # once: the password files, in secrets/
./build_image.sh                                 # once: the application and PostgreSQL images, from this checkout
echo 'PEOPLE_PUBS_IMAGE=pubdex:local' >> .env    # once: those images, not the release compose.yaml names
echo 'PEOPLE_PUBS_POSTGRES_IMAGE=pubdex-postgres:local' >> .env
docker compose up --wait                         # the one command: the read service on http://127.0.0.1:8000
docker compose run --rm demo                     # optional: the synthetic demonstration in that stack
```

[`DEPLOYMENT.md`](DEPLOYMENT.md) starts with this short path and covers the
rest: a published image without a clone, settings, secrets, health checks,
storage, backup and restore, ingestion, and checking a deployment.

### Running PubDex on Kubernetes

On Kubernetes 1.35 to 1.37 with Helm 4.3 or a later 4.x, the chart in
[`charts/pubdex/`](charts/pubdex/) installs the same images from a clone of
this repository: in evaluation mode with a PostgreSQL 16 of its own, or
against a PostgreSQL 16 server you run. [`KUBERNETES.md`](KUBERNETES.md)
starts with a short path from a clone to the synthetic demonstration read
over a port forward, says which platforms are supported and which were
tested, and ends with an appendix that says, for each block of its
commands, what ran it.
The read service has no authentication there either: the guide's
*Ingress and TLS* and *NetworkPolicy* say what that means in a cluster.

### Working on PubDex

```bash
./setup_venv.sh                  # .venv with the development lock; --psycopg python without a compiler
./run_checks.sh                  # offline; needs no database and no network
```

[`setup_venv.sh`](setup_venv.sh) keeps downloading and installing apart, as
CI and the image build do: [`DB/fetch_verified.py`](DB/fetch_verified.py)
downloads exactly the files the locks name, over HTTPS with Python's own
client, and keeps each only when its SHA-256 is one its lock lists; then
[`DB/install_pip.py`](DB/install_pip.py) installs the pinned pip from its
wheel, without running any other pip, and
[`DB/install_locked.py`](DB/install_locked.py) the development lock, both
with pip's `--no-index` from those files alone and without network access
where the system permits a network namespace. psycopg's C implementation is
built from its verified source archive against your system's libpq, which
needs a C compiler, Python's headers and libpq's headers with `pg_config`
(`libpq-dev` on Debian and Ubuntu); `./setup_venv.sh --psycopg python`
installs psycopg's pure-Python implementation instead, which loads the
system's libpq at run time. Either way that libpq has to be 16 or later.

For a database on the host as well, follow [`QUICKSTART.md`](QUICKSTART.md):
the full walkthrough from `DB/.env` and `DB/compose.yaml` through the demo
roster, ORCID and Crossref fixture ingestion, querying what landed, CSV
export, an optional live demonstration and troubleshooting. Then:

- [`DB/README.md`](DB/README.md) — setup, roles and grants, backup and
  restore, the migration model.
- [`DB/SCRIPTS.md`](DB/SCRIPTS.md) — every ingestion and maintenance command,
  with examples.

Every `people_pubs.sync` command that writes to a database supports
`--dry-run`, with two exceptions: `merge_people` only previews unless given
`--execute`, and the fixture demonstration `fixture_demo` has no preview mode.
Prefer a dry run on first contact with any database you care about.

### Running the checks

Two commands, both from the repository root:

- `./run_checks.sh` — the **fast offline check suite**: compilation of the
  package and its tests, the Markdown link and path check, the check of the
  release notes against the tree, the version guard, which refuses a tree
  that carries a published version but ships other files, and the offline
  unit and source-fixture tests; in pre-commit and CI modes (`--staged`, or
  `--base`/`--head`) it additionally runs the transform-version guard. It needs
  only the Python environment and a Git checkout — no database, no Docker, no
  network — and it is the pre-commit hook. Its tests run in a cleared
  environment, so no database setting or pytest option you export, and nothing
  in `DB/.env`, can connect them to a database or narrow the run; a network
  guard refuses connections from the tests and the Python processes they
  start, and a skip that `DB/tests/expected_skips.py` does not name, or a test
  module skipped while it is collected, fails the run.
- `./run_ci_suite.sh` — the **complete verification suite** in one command,
  and exactly what public CI runs: the offline suite, a check for committed
  secrets and local configuration, schema bootstrap from an empty database, the
  task-1a clean-clone demonstration, the complete integration suite against
  its own disposable PostgreSQL, the acceptance test of the Compose
  deployment, which builds the image from the checkout and starts the stack,
  the test of the operator's workflow on that deployment, the offline
  checks of the Helm chart, and the chart's acceptance test on disposable
  kind clusters of the newest and the oldest supported Kubernetes release,
  which installs, upgrades, rolls back and uninstalls it in both of its
  modes, the rehearsal of a PubDex release, which builds and verifies a
  test release of the checkout and publishes nothing, and the upgrades and
  rollbacks between PubDex releases on the Compose deployment.
  It stops at the first failing stage;
  `--base <sha> [--head <sha>]` adds the transform-version guard over that
  commit range and checks the files its commits add or change for secrets
  and local configuration, as the CI workflows do for the pushes and pull
  requests they run on. It
  needs Bash, the pinned helm, kubectl and kind (below) and a Docker Engine
  with the Compose plugin running on the same machine;
  [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) gives the contract a CI
  runner on another forge has to meet.

```bash
./run_ci_suite.sh                # everything CI runs; needs Docker
```

The complete suite needs a Git checkout, Python 3.11 with pip from
`DB/requirements-pip.txt` and then `DB/requirements-dev.txt` installed into
`.venv` (as above), the pinned helm, kubectl and kind that
`./setup_cluster_tools.sh` installs (below), the pinned SBOM tool that
`./release.sh setup` pulls, `openssl`, and a Docker Engine
28.0.1 or later, except 28.2.0 to 28.3.2, with the buildx plugin and the Compose plugin 2.24.0 or later, which your user can reach
and which can pull `postgres:16`, the images the `Dockerfile` and
`compose.yaml` name and kind's two node images; the kind clusters' nodes pull
the published images from `ghcr.io`, as stage 11 does when Docker lacks them. It uses only synthetic fixtures and disposable
databases: each Docker-backed stage starts its own uniquely named resources —
stages 6 and 7 a Compose project of their own, stage 9 two kind clusters,
one after the other, each on a Docker network of its own, stage 10 the
images of a test release, stage 11 those of three and Compose projects of
its own — publishes them on loopback at
most, and removes them again when the stage ends, also after a failure or an
interrupt; their throwaway passwords never appear on a command line or in the
output. No stage connects to an existing database, Kubernetes cluster or
metadata provider, or reads `DB/.env` or your kubeconfig, so the suite
neither needs nor touches a database, stack or cluster you run yourself.

Each stage is also a command of its own for focused debugging — for example
`(cd DB && ./run_migration_smoke.sh)` or `./DB/run_disposable_integration.sh`;
[`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) lists them all. To run only
the unit tests, `(cd DB && ./run_tests.sh)` still works.

The Kubernetes checks use their own pinned helm, kubectl and kind, never
the copies on your `PATH`: `./setup_cluster_tools.sh` (Linux on x86_64)
downloads the versions [`DB/lib/cluster_tools.sh`](DB/lib/cluster_tools.sh)
fixes, checks each against its pinned SHA-256 and installs it under `.tools/`.
`./run_chart_checks.sh`, stage 8 of the complete suite, checks the Helm chart
in `charts/pubdex/` offline with that helm. `./run_cluster_acceptance.sh`,
stage 9, runs the chart on two kind clusters that it creates and deletes
again, one after the other, each with its own kubeconfig, and runs the
commands that the appendix of [`KUBERNETES.md`](KUBERNETES.md) assigns to its
steps; about ten minutes on a workstation with Docker's build cache warm. `./run_checks.sh`
does not need the tools. Stages 9 to 11 build test releases with
[`release.sh`](release.sh), which writes an SBOM of each image with the
pinned Syft image that `./release.sh setup` pulls once per machine, by
digest and without credentials.

```bash
./setup_cluster_tools.sh         # once: the pinned helm, kubectl and kind under .tools/
./release.sh setup               # once per machine: the pinned SBOM tool, for stages 9-11
./run_chart_checks.sh            # stage 8: offline, no cluster
./run_cluster_acceptance.sh      # stage 9: a disposable kind cluster; needs Docker
./run_release_rehearsal.sh       # stage 10: a test release built and verified, nothing published
./run_upgrade_acceptance.sh      # stage 11: upgrades between PubDex releases on Compose
```

## Releases and versions

PubDex is published as numbered ***PubDex releases***. One version names
the tag `v<version>`, the application and PostgreSQL images and the Helm
chart's package, which come with a checksum file, an SBOM of each image and
a provenance record. Until 1.0.0, a patch release changes neither the
schema nor ingestion, and a minor release may change either. Each PubDex
release has a section in [`CHANGELOG.md`](CHANGELOG.md) whose first three
headings say what changed in the schema and in ingestion and how to upgrade
and roll back; the suite checks them against the tree. Nothing is signed.

- [`VERSIONING.md`](VERSIONING.md), for operators: what a version promises,
  schema changes and the schema upgrade, which upgrades and rollbacks
  between PubDex releases are supported and which the suite runs, and the
  supported platforms.
- Upgrading a deployment: [`DEPLOYMENT.md`](DEPLOYMENT.md), [Upgrading to a
  new release](DEPLOYMENT.md#upgrading-to-a-new-release), for Docker
  Compose, and [`KUBERNETES.md`](KUBERNETES.md), [Upgrading to a new
  release](KUBERNETES.md#upgrading-to-a-new-release), for the Helm chart.
  Take a backup first.
- The release record, [`releases/`](releases/README.md), committed with the
  repository, is authoritative: for every published PubDex release its
  commit, its tag and the digests of its images, by which to install it,
  and from 0.2.0 on the checksum file that checks the files of the forge's
  release page, a copy.
- [`RELEASING.md`](RELEASING.md), for maintainers: how a PubDex release is
  made and published. CI rehearses it on every run, stage 10 of the suite,
  and never publishes.

## Licensing

Two licences, split by what a file *is*:

| What | Licence | Covers |
|---|---|---|
| Code | MIT — [`LICENSE`](LICENSE) | The Python package, the SQL schemas and migrations under [`DB/init/`](DB/init/), and the executable shell scripts |
| Content | CC BY 4.0 — [`LICENSE-CC-BY-4.0`](LICENSE-CC-BY-4.0) | The Markdown documentation and the synthetic fixtures and examples under [`DB/tests/fixtures/`](DB/tests/fixtures/) |

Two things this repository does **not** relicense:

- **Publication metadata retrieved from external sources** keeps the terms of
  the source it came from. Ingesting a record into your database does not
  place it under either licence above.
- **Third-party dependencies** keep their own licences; see
  [`DB/requirements.txt`](DB/requirements.txt) for what running PubDex pulls
  in and [`DB/requirements-dev.txt`](DB/requirements-dev.txt) for a
  development install. [`third_party/`](third_party/) holds the one
  third-party licence text kept here: that of the docker-library/postgres
  project, whose entrypoint scripts the PostgreSQL image carries
  ([`postgres.Dockerfile`](postgres.Dockerfile)).

Suggested attribution when reusing the documentation or the synthetic
examples:

> PubDex, by the PubDex contributors, used under
> [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Source: this
> repository.

Replace "this repository" with the URL you obtained the material from. Check
the licence text itself for what your particular reuse requires.

[`LICENSE`](LICENSE) contains the stated copyright attribution.
