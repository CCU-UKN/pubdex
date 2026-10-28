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
| [`run_checks.sh`](run_checks.sh) | The fast offline check suite: compilation, Markdown consistency and the offline unit and fixture tests, plus the transform-version guard in pre-commit and CI modes. The pre-commit hook, and the first stage of `run_ci_suite.sh` |
| [`run_ci_suite.sh`](run_ci_suite.sh) | The complete verification suite in one command, and exactly what public CI runs: the offline suite, the check for committed secrets and local configuration, migration from an empty database, the task-1a demonstration, the integration suite on a disposable PostgreSQL, the acceptance test of the Compose deployment, and the test of the operator's workflow on it |
| [`run_stack_acceptance.sh`](run_stack_acceptance.sh) | The acceptance test of the Compose deployment: the image built from the checkout, or a published one, started with `compose.yaml` in a project of its own, a small ingest inside it, the result read over HTTP, readiness, persistence and secrets checked, and everything it created removed |
| [`run_operator_acceptance.sh`](run_operator_acceptance.sh) | The operator's workflow on the Compose deployment, as `DEPLOYMENT.md` gives it: an `.env` configuration on the ordinary network, a private roster imported on standard input, ingestion of the bundled payloads, refresh, read service and export, password changes with `set_passwords.sh` for files overwritten and replaced, a backup restored into a new stack, and the documented role model, checked by the records and connections they leave |
| [`check_secrets_and_local_config.py`](check_secrets_and_local_config.py) | The check for committed secrets and local configuration over the publishable tree; findings name rule, file and line, never the matched text |
| [`DB/run_task_1a_demo.sh`](DB/run_task_1a_demo.sh) | The clean-clone acceptance demonstration: a disposable database, bundled ORCID and Crossref payloads, queries and a verified export, with no metadata-provider request |
| [`DB/run_disposable_integration.sh`](DB/run_disposable_integration.sh) | The complete integration suite against a PostgreSQL started, bound and removed for that run alone |
| [`CHANGELOG.md`](CHANGELOG.md) | The public progress record: what each funded result delivered, and how to verify it |
| [`TASK_1A.md`](TASK_1A.md) | The acceptance record for the first funded result, requirement by requirement |
| [`TASK_1B.md`](TASK_1B.md) | The acceptance record for the second funded result, the forge-portable test suite |
| [`TASK_2A.md`](TASK_2A.md) | The acceptance record for the container image and the Compose deployment |
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
| The development and walkthrough setup | PostgreSQL from [`DB/compose.yaml`](DB/compose.yaml), the `people_pubs` package from a virtual environment on the host | [`QUICKSTART.md`](QUICKSTART.md) |

[`DEPLOYMENT.md`](DEPLOYMENT.md), *The development setup*, compares the two.

### Running PubDex as a service

On Linux x86_64, with Docker Engine 28.0.1 or later, except 28.2.0 to
28.3.2, the Compose plugin 2.24.0 or later and Bash — and Git and Docker's
buildx plugin to build the image, as long as no PubDex image is published.
Other Engines start the stack too, but can let other hosts on the local
network reach the read service, which has no authentication
([`DEPLOYMENT.md`](DEPLOYMENT.md), *What you need*). Other architectures,
Docker Desktop, rootless Docker, SELinux-enforcing hosts and Podman are
neither tested nor supported: *Supported platforms* in `DEPLOYMENT.md` lists
what is supported and what was tested.

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
  package and its tests, the Markdown link and path check, and the offline unit
  and source-fixture tests; in pre-commit and CI modes (`--staged`, or
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
  and the test of the operator's workflow on that deployment.
  It stops at the first failing stage;
  `--base <sha> [--head <sha>]` adds the transform-version guard over that
  commit range and checks the files its commits add or change for secrets
  and local configuration, as the CI workflows do for the pushes and pull
  requests they run on. It
  needs Bash and a Docker Engine with the Compose plugin running on the same
  machine;
  [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) gives the contract a CI
  runner on another forge has to meet.

```bash
./run_ci_suite.sh                # everything CI runs; needs Docker
```

The complete suite needs a Git checkout, Python 3.11 with pip from
`DB/requirements-pip.txt` and then `DB/requirements-dev.txt` installed into
`.venv` (as above), and a Docker Engine
28.0.1 or later, except 28.2.0 to 28.3.2, with the buildx plugin and the Compose plugin 2.24.0 or later, which your user can reach
and which can pull `postgres:16` and the images the `Dockerfile` and
`compose.yaml` name. It uses only synthetic fixtures and disposable
databases: each Docker-backed stage starts its own uniquely named resources —
the last two a Compose project of their own — publishes them on loopback at
most, and removes them again when the stage ends, also after a failure or an
interrupt; their throwaway passwords never appear on a command line or in the
output. No stage connects to an existing database or a metadata provider, or
reads `DB/.env`, so the suite neither needs nor touches a database or stack
you run yourself.

Each stage is also a command of its own for focused debugging — for example
`(cd DB && ./run_migration_smoke.sh)` or `./DB/run_disposable_integration.sh`;
[`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) lists them all. To run only
the unit tests, `(cd DB && ./run_tests.sh)` still works.

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
