# Running PubDex with Docker Compose

[`compose.yaml`](compose.yaml), in the repository root, is the canonical way
to run PubDex: PostgreSQL 16 and the PubDex read service as one Compose
project on one host, with the ingestion commands on request, all from one
application image that also carries the database schema. The image and
`compose.yaml` are the whole deployment. [`DB/compose.yaml`](DB/compose.yaml)
is the development and walkthrough setup of [`QUICKSTART.md`](QUICKSTART.md),
not a deployment ([The development setup](#the-development-setup-dbcomposeyaml)).
For a Kubernetes cluster, the Helm chart in [`charts/pubdex/`](charts/pubdex/)
runs the same images; [`KUBERNETES.md`](KUBERNETES.md) is its guide. This
page, and `compose.yaml`, stay the canonical deployment on one host.

## The short path

You need:

- Linux on x86_64 with Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2, and the Docker Compose plugin 2.24.0 or later ([Supported platforms](#supported-platforms));
- Bash;
- to build the image, a clone of this repository with Git, and Docker's buildx plugin.

Check the versions first. Nothing on this path does, and an unsupported
Engine starts the stack as well, but can let other hosts on your network
reach the read service ([What you need](#what-you-need)):

```bash
docker version --format '{{.Server.Version}}'    # 28.0.1 or later, except 28.2.0 to 28.3.2
docker compose version --short                   # 2.24.0 or later
```

Version 0.2.0 is published for `linux/amd64`. To use it without a clone,
follow [Selecting a published image](#selecting-a-published-image), which
names both verified image digests. To build the two images yourself,
prepare a clone once:

```bash
./create_secrets.sh                              # the three password files, in secrets/
./build_image.sh                                 # the images pubdex:local and pubdex-postgres:local
echo 'PEOPLE_PUBS_IMAGE=pubdex:local' >> .env    # those images, not the release compose.yaml names
echo 'PEOPLE_PUBS_POSTGRES_IMAGE=pubdex-postgres:local' >> .env
```

Start PostgreSQL and the read service; the command returns once both are
healthy:

```bash
docker compose up --wait                         # the read service on http://127.0.0.1:8000
```

Try it with the synthetic demonstration, on a stack you remove afterwards:

```bash
docker compose run --rm demo
curl -s 'http://127.0.0.1:8000/v0/publications?limit=5'
```

Stop and remove:

```bash
docker compose down                              # removes the containers; the database stays in its volume
docker compose down --volumes                    # also deletes the database
```

With the published images no clone is needed ([Selecting a published
image](#selecting-a-published-image)). The rest of this page is the detail.

## Contents

- [What you need](#what-you-need)
- [Supported platforms](#supported-platforms)
- [Portable defaults](#portable-defaults)
- [Setting up](#setting-up)
- [Starting, stopping and removing](#starting-stopping-and-removing)
- [The read service](#the-read-service)
- [Ingestion](#ingestion)
- [Settings](#settings)
- [Secrets](#secrets)
- [Health and readiness](#health-and-readiness)
- [Data and storage](#data-and-storage), with [Backup and restore](#backup-and-restore)
- [Logs](#logs)
- [Reaching the database](#reaching-the-database)
- [Roles and personal data](#roles-and-personal-data)
- [The development setup: DB/compose.yaml](#the-development-setup-dbcomposeyaml)
- [The application image](#the-application-image), with [The PostgreSQL image](#the-postgresql-image)
- [What identifies a deployment](#what-identifies-a-deployment)
- [Upgrading to a new release](#upgrading-to-a-new-release)
- [Checking a deployment](#checking-a-deployment)
- [Publishing an image](#publishing-an-image), in [`RELEASING.md`](RELEASING.md)
- [Troubleshooting](#troubleshooting)

## What you need

Linux on x86_64, Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2,
and the Docker Compose plugin 2.24.0 or later, to build as to run.
[Supported platforms](#supported-platforms) is the one statement of what is
supported, what has been tested and what is not known.

The Engine requirement is about the network. The read service has no
authentication and no TLS, and the stack publishes it on 127.0.0.1 so that
only this host reaches it. Docker documents that before Engine 28.0.0 hosts
on the same local network (layer 2) can reach ports published on 127.0.0.1;
28.0.0 drops such packets, with firewall rules that needed the kernel's
`ipset` modules until 28.0.1, and in 28.2.0 to 28.3.2 a reload of
firewalld removes that filter again (CVE-2025-54388, fixed in 28.3.3).
The filter is also off on a daemon started with
`DOCKER_INSECURE_NO_IPTABLES_RAW=1`. Older Engines can still build the
image, start the stack and pass its health checks, as 25.0.5 and Debian 13's
26.1.5 did in tests before this requirement: that the stack comes up shows
only that it runs, not that it runs on a supported Engine, so check `docker
version` before the first start. The Compose minimum is the first release
that applies the health checks' `start_interval` (Compose rejects it before
2.20.2 and drops it up to 2.23.x).

Only the tests of [Checking a deployment](#checking-a-deployment) check the
versions, and by the number alone: a distribution's package counts as the
release it reports, whatever it backported. Distributions update their
packages, so `docker version` decides. On 2026-10-28 Debian 13's own
`docker.io` was 26.1.5, below the requirement — use Docker's packages there
— and Ubuntu 24.04's, from its updates, 29.1.3, within it; the Compose
packages of both, 2.26.1 and 2.40.3, met the minimum. Building also needs
Git and buildx (`docker-buildx-plugin`, or `docker-buildx` on Debian and
Ubuntu, where Ubuntu's `docker.io` lacks it).
Setting up needs network access for the images and, to build, the Python
packages, PostgreSQL's source and Debian's packages; ingestion needs it for
metadata providers. The first build of the PostgreSQL image compiles
PostgreSQL, a few minutes. The images take about
160 MB (PubDex) and 450 MB (PostgreSQL).

## Supported platforms

This table is the one statement of where the deployment is supported; the
other documents of the repository refer to it. *Supported* means that a
configuration meets the requirement of [What you need](#what-you-need) and
that PubDex's tests passed on the releases the table names. Everything
listed under *Tested* ran on one machine, at the date given.

| Platform | Architecture | Docker Engine and Compose | Status | Tested | Date |
|---|---|---|---|---|---|
| Linux, with Docker Engine run as root and its default firewall backend, `iptables` | x86_64; the image is `linux/amd64` | Engine 28.0.1 or later, except 28.2.0 to 28.3.2; Compose plugin 2.24.0 or later | **supported** | Ubuntu 24.04.5, kernel 6.8, AppArmor, Docker's packages — Engine 29.9.2, Compose 5.6.1, buildx 0.38.1: the complete verification suite and both acceptance tests in a clean clone of release source 57a72dc; anonymous release acceptance on both published digests ([`TASK_2A.md`](TASK_2A.md), *Local verification record* and *Published-image evidence*) | 2026-10-30 and 2026-11-03 |
| the same | the same | the same | **supported** | Ubuntu 24.04's `docker.io` 29.1.3, `docker-compose-v2` 2.40.3 and `docker-buildx` 0.30.1, in a throwaway Docker daemon inside a privileged container on the machine above: `build_image.sh`; the image pushed to a registry, then pulled and started by `docker compose up --wait` beside only `compose.yaml` and the secret files; the steps of [Selecting a published image](#selecting-a-published-image) as they were then, before `set_passwords.sh` existed | 2026-10-23, with the inputs of that day |
| Linux | x86_64 | Engine before 28.0.1, or 28.2.0 to 28.3.2; Compose plugin before 2.24.0 | **refused** by the suite and the tests of [Checking a deployment](#checking-a-deployment) before they create anything; `docker compose` itself does not check, and starts a stack whose read service other hosts can reach | Engine 25.0.5 with Compose 2.24.0 and buildx 0.16.2, and Debian 13's `docker.io` 26.1.5 with `docker-compose` 2.26.1 and `docker-buildx` 0.13.1, each in a throwaway daemon as above: the same steps, and with 25.0.5 on 2026-10-27 also the password changes and a restore. These runs predate the requirement: they show that the stack runs there, not that other hosts are kept out. Compose 2.23.3 and 2.20.2 were refused before anything was created | 2026-10-23 and 2026-10-27; Compose 2026-10-19 |
| Linux | arm64 (aarch64) | — | **not supported**: no `linux/arm64` image is built, tested or scanned | nothing; the locked Python packages have aarch64 wheels and both base images have arm64 variants, which shows only that a build could be tried | — |
| A host running the `linux/amd64` image under emulation, such as a Mac with Apple silicon | arm64 | — | **not supported**, untested | nothing | — |
| Docker Desktop, on macOS, Windows or Linux | any | — | **not supported**, untested; on Windows the scripts need WSL 2, and Windows ignores their file modes | nothing | — |
| Rootless Docker, or a daemon with user-namespace remapping (`userns-remap`) | x86_64 | within the range of the first row | **not supported**, untested; rootless mode needs subordinate ids that include 999 and 10001, as the usual 65,536 do | nothing | — |
| A host enforcing SELinux, such as Fedora or Red Hat Enterprise Linux | x86_64 | within the range of the first row | **not supported**, untested; the containers probably cannot read the secret files until they carry a container label (`chcon -Rt container_file_t secrets`) | nothing | — |
| Podman, also through its Docker compatibility | any | not Docker Engine | **not supported**, untested; the version checks are those of Docker Engine and its Compose plugin, and how Podman handles the temporary mounts, the volume ownership `bootstrap` relies on and `start_interval` is unknown | nothing | — |

The release source also passed the complete suite on GitHub-hosted Ubuntu
24.04 with Engine 28.0.4 and Compose 2.38.2 on 2026-11-02. On 2026-11-03
the published images passed anonymous release acceptance on the first
configuration above, after every image layer was downloaded and
hash-verified without credentials ([`TASK_2A.md`](TASK_2A.md),
*Published-image evidence* and *Public-CI evidence*).

Within the supported range only Engines 28.0.4, 29.1.3 and 29.9.2 have run the
deployment; for every other release in it, support rests on the requirement
and on Docker's documentation. No test connects from another host:
the acceptance tests read the port bindings Docker reports, so that other
hosts cannot reach the read service rests on Docker's documentation of the
supported releases, for the default address, 127.0.0.1, and the default
`iptables` backend. Docker's experimental `nftables` backend, hosts running
firewalld and other addresses in `PEOPLE_PUBS_HTTP_ADDRESS` were not tested.

`build_image.sh` builds for the platform of the Docker daemon and prints it:
on another architecture it builds an image nobody has tested. A PubDex
release is built for `linux/amd64` alone ([`RELEASING.md`](RELEASING.md));
a two-platform image would be one reference for two images, one of them
untested.

## Portable defaults

The defaults of `compose.yaml` are portable in four ways, and only these:

- **Names of the project's own.** No container, network or volume has a
  fixed name: Compose prefixes each with the project's name, that of the
  directory unless `-p` or `COMPOSE_PROJECT_NAME` sets another, so two
  installations, or an acceptance test, run beside each other and beside
  other stacks ([Starting, stopping and
  removing](#starting-stopping-and-removing)).
- **Settings instead of edits.** The image, the read service's address and
  port, the directory of the secret files, the log level and the project
  name come from the environment or `.env` ([Settings](#settings)); the
  passwords come from secret files created on each host; PostgreSQL gets a
  host port only on request, one of your choosing ([Reaching the
  database](#reaching-the-database)).
- **Nothing of one site.** No institution, host name, absolute path,
  account or contact address is built in: the secret files lie in a
  directory relative to `compose.yaml`, the data in named volumes, and which
  publications count as yours, the contact for Crossref and the employers
  that count are settings or data you enter.
- **The image and its Compose file are the deployment.** The image carries
  the schema bootstrap, `compose.yaml` and the scripts an operator runs, so
  nothing is read from a checkout at run time ([What identifies a
  deployment](#what-identifies-a-deployment)).

These defaults make the deployment independent of one host and one site,
on the platforms that [Supported platforms](#supported-platforms) supports.
They do not extend it to other architectures, to Docker Desktop or to
another container runtime.

## Setting up

[The short path](#the-short-path) prepares a clone once. `compose.yaml`
names the images of its release, `ghcr.io/ccu-ukn/pubdex:<version>` and
`ghcr.io/ccu-ukn/pubdex-postgres:<version>`; `PEOPLE_PUBS_IMAGE=pubdex:local`
and `PEOPLE_PUBS_POSTGRES_IMAGE=pubdex-postgres:local` in `.env` put your
builds in their place. With published images the secret files are the only
preparation. In a checkout between two PubDex releases, `compose.yaml` names
the next version, which is not published yet: build the images and name
them as above ([`VERSIONING.md`](VERSIONING.md), *A checkout between PubDex
releases*).

`./create_secrets.sh` creates `secrets/` (mode 700) with a random password in
each of three files (mode 444; [Secrets](#secrets)). It never overwrites a
file: run again, it adds what is missing and checks what is there. Compose
creates no secret file, and without them the stack does not start. A script
does this, not the first start, because Compose can only hand a host file to
a container: passwords made inside the stack would live in a volume that
every container mounting it sees whole (per-file subpaths need Engine 26.0),
and would exist only inside Docker.

`./build_image.sh` builds `pubdex:local` and `pubdex-postgres:local`
([Building it](#building-it)). Compose never builds an image and pulls one
only when Docker lacks it: a local build is used as Docker has it, a missing one fails at the
pull, and a release, once pulled, stays in use even if its tag is later
moved. `docker compose pull` fetches what the names point to now.

`docker compose up --wait` runs the one-shot service `bootstrap`, which
copies the schema files the image carries — those of
[`DB/init/`](DB/init/) — into a volume of its own, then starts PostgreSQL,
waits until it is healthy, runs the one-shot service `schema-upgrade`, which
applies the schema changes the image carries that the database has not had
yet, starts the read service and returns once that is healthy, or fails
naming the service that is not. On the first start, with an
empty volume, PostgreSQL runs the schema files, gives the two application
roles the passwords from their files and takes the login away from
`maintenance`. Later starts copy the files again but never run them on a
volume with data. The schema upgrade runs at every start and before every
`docker compose run` of `ingest` or `demo`; with nothing outstanding it
changes nothing, and when it fails nothing that uses the database starts:
`docker compose ps --all` and `docker compose logs schema-upgrade` say why.
No SQL by hand and no roster is needed; the canonical
publication list stays empty until you configure which publications are
yours ([Ingestion](#ingestion)).

To try a build without touching your installation, run
`./run_stack_acceptance.sh --image pubdex:local --postgres-image pubdex-postgres:local`
and `./run_operator_acceptance.sh --image pubdex:local --postgres-image pubdex-postgres:local`,
which use projects of their own ([Checking a deployment](#checking-a-deployment)).

## Starting, stopping and removing

| Command | What it does | The database |
|---|---|---|
| `docker compose up --wait` | creates and starts what is missing, waits until healthy | kept |
| `docker compose ps` | lists the containers with their health | — |
| `docker compose stop` / `start` / `restart` | stops, starts or restarts the containers | kept |
| `docker compose down` | stops the stack and removes its containers and network | **kept** in the volume |
| `docker compose down --volumes` | also deletes the volumes | **deleted**, with every record in it |

The data, the role passwords and the attribution rules live in the volume,
so `down` and `up --wait` bring back the same database; only `--volumes`
(`-v`) deletes it, and there is no other copy unless you made one ([Backup and
restore](#backup-and-restore)). The containers restart with Docker unless you
stopped them (`restart: unless-stopped`). Compose names the project after the
directory of `compose.yaml` and prefixes every container, network and volume
with it, so give two installations in directories of the same name their own
names: `docker compose -p <name>` on every command, or
`COMPOSE_PROJECT_NAME=<name>` in the environment or `.env`.

## The read service

At `http://127.0.0.1:8000` by default, it serves three endpoints and no
more: `GET /livez` (the process runs), `GET /readyz` (a database connection
within 5 seconds and the snapshot readable, else 503 with the reason) and
`GET /v0/publications` (a page of the canonical list, with the CSV export's
columns). It is preliminary, the foundation of the versioned read API of task
3a and not that API: everything under `/v0/` may change.
[`DB/README.md`](DB/README.md), section 11, documents the parameters, format,
errors and settings. It runs as `app_readonly`, in read-only sessions, with
no TLS and no authentication, so the stack publishes it on 127.0.0.1 only,
which keeps other hosts out on a supported Engine ([What you
need](#what-you-need)) and does not replace a firewall: to serve other
machines, put a reverse proxy with TLS in front of it, or use
`PEOPLE_PUBS_HTTP_ADDRESS` only on a network you trust.

It reads the serving snapshot `biblio.publications_canon_web`, which changes
only when refreshed: the attribution command refreshes it after a rule
change; after an ingestion, refresh it — from cron for a regular refresh
([`DB/README.md`](DB/README.md), section 9) — with

```bash
docker compose run --rm ingest python -m people_pubs.sync.attribution_rules recompute --refresh-only
```

## Ingestion

```bash
docker compose run --rm ingest python -m people_pubs.sync.<command> [arguments]
```

runs one command of [`DB/SCRIPTS.md`](DB/SCRIPTS.md) and exits, as
`app_writer`, with the password from its secret file and no other
credential; `docker compose up` never starts the `ingest` service. The shell
scripts of `DB/` and `refresh_state_runner` belong to the development setup;
here, schedule the commands themselves. For example:

```bash
# Which publications are yours: the attribution rules (QUICKSTART.md, step 7)
docker compose run --rm ingest python -m people_pubs.sync.attribution_rules list
docker compose run --rm ingest python -m people_pubs.sync.attribution_rules set \
    --affiliation "Institute for Example Studies" --funding "EX-12345678"

# A roster from the host, on standard input (Importing a roster, below)
docker compose run --rm -T ingest python -m people_pubs.sync.add_people --csv - < roster.csv

# The canonical list as CSV, written to the host through standard output
docker compose run --rm -T ingest python -m people_pubs.sync.export_publications > publications.csv
```

Provider commands need network access and `PEOPLE_PUBS_CROSSREF_MAILTO` and
`PEOPLE_PUBS_ORG_PATTERNS` ([Settings](#settings)). The optional keys
`SEMANTIC_SCHOLAR_API_KEY` and `DATACITE_API_TOKEN` are passed to one run with
`-e SEMANTIC_SCHOLAR_API_KEY` from your shell, and stay in that container's
configuration until `--rm` removes it. The root file system is read-only: a
command that writes a file (`--output`, `--report`) needs a mounted directory
the user 10001 may write to, or `/tmp`; prefer standard output.

### Importing a roster

A roster holds personal data: keep its file private (mode `0600`) and give
it on standard input, as above, so the container (user 10001) receives only
its contents and none of it reaches a command line; `-T` passes it
unchanged, and `--csv -` refuses a terminal instead of waiting. A bind mount
(`-v "$PWD/roster.csv:/data/roster.csv:ro"`, `--csv /data/roster.csv`) fails
on a private file with `permission denied`; do not loosen the mode for it.
The roster is UTF-8 CSV, a byte-order mark allowed, whose header has an
`orcid` column ([`DB/SCRIPTS.md`](DB/SCRIPTS.md) lists the others). An empty
roster, one without that column or not in UTF-8 stops the import before it
connects, and an unreadable line stops it, by line number, with nothing
written. The output names people by ORCID iD and rows by number, never by
name or address; `--dry-run` shows what would be written, names and addresses
included, and `--no-orcid-lookup` makes no ORCID request.

### The demonstration

`docker compose run --rm demo` runs the synthetic demonstration of
[`QUICKSTART.md`](QUICKSTART.md) inside the stack, as `app_writer`: the
bundled roster, the bundled ORCID and Crossref payloads — no provider
request — and the fictional institute they name as an attribution rule,
which refreshes the snapshot; `GET /v0/publications` then serves the DOI
`10.5555/pubdex-fixture-001`. It writes synthetic records: run it only on a
stack you remove afterwards with `docker compose down --volumes`.

## Settings

All optional, from the environment of `docker compose` or a `.env` file next
to `compose.yaml`, one `NAME=value` per line. Passwords never go there: they
belong in the secret files.

| Variable | Default | Meaning |
|---|---|---|
| `PEOPLE_PUBS_IMAGE` | `ghcr.io/ccu-ukn/pubdex:<version>`, the release of `compose.yaml` | the application image, pulled when Docker lacks it and never built: a build (`pubdex:local`), or a release with its digest (`…:<version>@sha256:<digest>`) to pin that exact image ([What identifies a deployment](#what-identifies-a-deployment)) |
| `PEOPLE_PUBS_POSTGRES_IMAGE` | `ghcr.io/ccu-ukn/pubdex-postgres:<version>`, the release of `compose.yaml` | the PostgreSQL image, pulled and pinned the same way: a build (`pubdex-postgres:local`) or a release with its digest ([The PostgreSQL image](#the-postgresql-image)) |
| `PEOPLE_PUBS_HTTP_ADDRESS` | `127.0.0.1` | host address the read service is published on |
| `PEOPLE_PUBS_HTTP_PORT` | `8000` | its host port |
| `PEOPLE_PUBS_SECRETS_DIR` | `./secrets` | directory of the secret files, absolute or relative to `compose.yaml`. `create_secrets.sh` and `set_passwords.sh` read it from their environment only: if `.env` sets it, run them as `PEOPLE_PUBS_SECRETS_DIR=<directory> ./create_secrets.sh` |
| `PEOPLE_PUBS_API_LOG_LEVEL` | `INFO` | log level of the read service: `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `PEOPLE_PUBS_CROSSREF_MAILTO` | empty | contact address for Crossref's polite pool, for ingestion |
| `PEOPLE_PUBS_ORG_PATTERNS` | empty | employers that count as yours, for ORCID profile enrichment ([`.env.example`](.env.example) explains both) |
| `PEOPLE_PUBS_POSTGRES_ADDRESS`, `PEOPLE_PUBS_POSTGRES_PORT` | `127.0.0.1`, none | see [Reaching the database](#reaching-the-database) |
| `COMPOSE_PROJECT_NAME` | the directory's name | the project name |

`.env.example` is otherwise the template of `DB/.env`, the development
setup's; this deployment uses none of its passwords. `compose.yaml` fixes the
database `people_db`, the roles and the connection settings and password
files of the read service and of ingestion. To change another read-service
setting ([`DB/README.md`](DB/README.md), section 11.1), add it to the
service's `environment` in a `compose.override.yaml` next to `compose.yaml`;
when `-f` or `COMPOSE_FILE` name the files, name the override there too.

## Secrets

| File in `secrets/` | Password of | Read by |
|---|---|---|
| `postgres_password` | `postgres`, the bootstrap superuser | the PostgreSQL container, whose image reads the file at every start and uses it only when it initialises the database, and `schema-upgrade`, which logs in with it |
| `app_readonly_password` | `app_readonly` | the PostgreSQL container's initialisation, and the read service |
| `app_writer_password` | `app_writer` | the PostgreSQL container's initialisation, and `ingest` and `demo` |

Each container gets only its files, under `/run/secrets/`. The PostgreSQL
container mounts all three but reads the role files only on the first start,
when `020_role_passwords.sh` sets the passwords before the server accepts TCP
connections; whoever can read files there can read the database anyway. No
password is in a container's configured environment, a command line, a build
argument, the image or `docker compose config`; the read service masks its
own in its logs, an ingestion command holds its own in libpq's `PGPASSWORD`
within its process, visible inside its container and to root on the host
only, and the PostgreSQL image removes its `POSTGRES_*` settings before the
server starts. Not protected:

- **The files are plain text on the host**, bind-mounted, not encrypted:
  root, a backup, anyone who may use Docker here can read them.
- **They are readable for every user** (444): bind mounts keep owner and
  mode, Compose ignores `uid`, `gid` and `mode` for file secrets, and the
  containers run as 999 and 10001, which could not enter a mounted directory
  of mode 700. That mode on `secrets/` keeps other users out;
  `create_secrets.sh` and `set_passwords.sh` refuse a directory others can
  enter, and a role file its container's user cannot read.
- **Copies of the directory** carry the passwords: keep it out of
  synchronised folders, repositories and shared backups. `secrets/` is in
  `.gitignore` and outside the build context.

A file holds one line, the password as written less one trailing line
break; an empty file, a second line, a NUL byte, non-UTF-8 text or more than
4096 bytes is refused, naming the setting, never the content. Passwords of
your own go into the files before the first start, one per role: duplicates
are refused.

### Changing a password

The database keeps the passwords of its first start; a changed file alone
makes its readers fail to log in (`/readyz` 503 `database_unavailable`;
`password authentication failed for user "app_readonly"` in the PostgreSQL
log). To change the password of `postgres`, `app_readonly` or `app_writer`,
write it into its file, in place or by replacing it, mode 444, then:

```bash
./set_passwords.sh             # bash set_passwords.sh, for the copy from the image
docker compose restart api
docker compose up --wait
```

[`set_passwords.sh`](set_passwords.sh) gives all three roles their files'
passwords and confirms each with a new SCRAM-authenticated connection, or
fails saying why; PostgreSQL is not restarted. Nothing changes until its
checks pass — the files as `create_secrets.sh --check` checks them, a running
PostgreSQL, new read-service and ingestion containers reading exactly the
host's bytes — and no password reaches a command line. It reads the host's
files because a running container keeps a replaced file's old version. If the
container check refuses, give the script your `.env`'s
`PEOPLE_PUBS_SECRETS_DIR` and your `-p`, `-f` or `--env-file` options. Until
its restart the read service keeps its old connections, and `/readyz` 200;
ingestion reads its file on every run, so change `app_writer`'s while none
runs.

## Health and readiness

| Service | Health check | Healthy means |
|---|---|---|
| `postgres` | `pg_isready` on TCP, and a query of the roles | the server accepts connections, and both application roles have their passwords |
| `api` | `python -m people_pubs.api.probe` (`/readyz`), every 30 seconds | `/readyz` answers 200 within the probe's 4 seconds: a database connection in time, a schema the application supports and the snapshot readable |

The read service starts once PostgreSQL is healthy and the schema upgrade
has succeeded, and the database check includes the role passwords, so a
first start that failed half-way, which a
restart does not repeat, is never served. `python -m people_pubs.api.probe
--live` checks `/livez` instead, for an orchestrator that restarts an
unresponsive process. While PostgreSQL is down, `/readyz` answers 503
`database_unavailable` within 5 seconds and `/livez` 200; the read service
recovers by itself once PostgreSQL is back. On a schema it does not support,
which only a read service started without the schema upgrade meets,
`/readyz` and `/v0/publications` answer 503 with the reason
`schema_upgrade_required` or `schema_not_supported` and a message that names
the schema upgrade ([`VERSIONING.md`](VERSIONING.md), *Schema changes and
the schema upgrade*).

## Data and storage

| What | Where |
|---|---|
| the database | the named volume `<project>_postgres-data`, at `/var/lib/postgresql/data` in the PostgreSQL container |
| the schema bootstrap | the named volume `<project>_initdb`: a copy of the files the image carries, made by the `bootstrap` service on every start; it holds no data, and deleting it loses nothing |
| the secret files | `secrets/`, or `PEOPLE_PUBS_SECRETS_DIR`, on the host |
| everything else | nothing: the application containers write nothing except to `/tmp`, a private in-memory file system |

### Backup and restore

A backup is a dump in PostgreSQL's custom format, into a file only you can
read. It holds every record, personal data included, but no role or password,
which `pg_dump` leaves out:

```bash
(umask 077 && docker compose exec -T postgres pg_dump -U postgres -Fc people_db > people_db.dump)
```

To restore it, into this deployment or into a new one prepared as in
[Setting up](#setting-up), in the directory of its `compose.yaml`:

```bash
docker compose up --wait postgres          # PostgreSQL alone; a new volume is initialised
docker compose stop api                    # nothing else may use the database
docker compose exec -T postgres dropdb -U postgres --force people_db
docker compose exec -T postgres createdb -U postgres people_db
docker compose exec -T postgres pg_restore -U postgres -d people_db --single-transaction --exit-on-error < people_db.dump
docker compose up --wait                   # its schema upgrade finds the dump's record: nothing to do
```

- **The database has to be empty.** `dropdb` deletes it with every record,
  so dump one you keep first; `--force` ends the sessions still connected,
  such as the health check's; run no ingestion meanwhile. Into a database
  holding a schema, as every first start creates, `pg_restore` fails at the
  first object that exists, and the single transaction leaves it unchanged.
- **The same PostgreSQL major version, 16**, as `compose.yaml` pins, and **the
  PubDex release whose schema the dump holds**: restore it with that
  release's images, and upgrade afterwards ([Upgrading to a new
  release](#upgrading-to-a-new-release)). A dump of a
  deployment that ran the official `postgres:16` image restores into the
  PubDex PostgreSQL image, and the other way round (tested with synthetic
  data; [The PostgreSQL image](#the-postgresql-image)).
- **Role passwords stay as they are**: the roles and their passwords are
  those of the server restored into, from its secret files; the dump brings
  each object's owner and privileges, by role name.

One query verifies the restore: run it before the dump and after the
restore, and compare the counts of people, stored publications and
publications served. The snapshot is restored with its data, so the read
service serves the list at once.

```bash
docker compose exec -T postgres psql -U postgres -d people_db -XAtc 'SELECT (SELECT count(*) FROM app.people), (SELECT count(*) FROM biblio.publications), (SELECT count(*) FROM biblio.publications_canon_web)'
```

[`run_operator_acceptance.sh`](run_operator_acceptance.sh) runs exactly this
cycle, with `docker compose down --volumes` and a new stack between dump and
restore. Restoring a dump of the development setup, which has the same
format and roles, is untested. Scheduled backups, retention, off-host copies
and recovery drills belong to task 4a; `run_backup.sh` and `restore_db.sh`
in `DB/` serve the development setup only.

## Logs

```bash
docker compose logs -f api        # the read service
docker compose logs postgres      # the database, with its initialisation on the first start
docker compose logs bootstrap     # the schema files copied from the image, with their SHA-256
docker compose logs schema-upgrade  # the schema changes applied at each start, or why none could be
```

The read service logs one plain-text line per request to standard error,
health checks included, with the password masked;
`PEOPLE_PUBS_API_LOG_LEVEL=WARNING` drops the request lines. Docker's default
`json-file` driver does not rotate logs: configure rotation for the daemon on
a long-running host.

## Reaching the database

PostgreSQL is published on no host port. Inside its container, as its
operating-system user, no password is needed:

```bash
docker compose exec -u postgres postgres psql -d people_db
```

For a client on the host, publish it on a loopback port of your choice with
[`compose.postgres-port.yaml`](compose.postgres-port.yaml), on every command
of that project; the port is required, so that it never collides with
another PostgreSQL by default:

```bash
PEOPLE_PUBS_POSTGRES_PORT=5433 \
  docker compose -f compose.yaml -f compose.postgres-port.yaml up --wait
```

`COMPOSE_FILE=compose.yaml:compose.postgres-port.yaml` and the port in `.env`
make it permanent (list `compose.override.yaml` there too if you use one).
Read as `app_readonly`, with the password from its file. The connection is
not encrypted: publish on another address only behind a firewall and with
TLS of your own. pgAdmin is not part of this deployment.

## Roles and personal data

The schema of [`DB/init/`](DB/init/) creates the roles and privileges of the
development setup; only the logins differ:

| Role | Logs in here | May |
|---|---|---|
| `postgres` | `schema-upgrade`, with its password file; the health check and `set_passwords.sh` use PostgreSQL's socket inside its container, where no password is needed | everything: the bootstrap superuser |
| `app_readonly` | the read service | read the tables and views of `app`, `biblio`, `activity` and `staging_raw`; nothing in `pii` |
| `app_writer` | `ingest` and `demo` | read and write `app`, `biblio` and `activity`; `pii` only through its definer functions, below |
| `maintenance` | nobody: the first start takes its login away | read and change the tables of `pii`; nothing of the application schemas |

In the development setup `maintenance` is the login for correcting or
deleting personal data by hand ([`DB/README.md`](DB/README.md), section 2.5):
the addresses in `pii.people_pii`, `pii.orcid_emails` and
`pii.publication_author_emails`, `pii.email_log` and the raw ORCID records in
`pii.orcid_payloads`; cross-schema work such as `merge_people` or a restore
runs as `postgres`. Here no service needs that login, so none exists. Do its
work either

- **as the superuser on the socket**, after `SET ROLE maintenance`, which
  guards against mistakes, not against the superuser — whoever may run
  `docker compose exec` here can do anything in the database:

  ```bash
  docker compose exec -u postgres postgres psql -d people_db
  ```

  ```sql
  SET ROLE maintenance;
  SELECT person_id, primary_email FROM pii.people_pii WHERE primary_email = 'someone@example.org';
  ```

- **or with a temporary login**, for a TCP client with the port published,
  its password typed at the prompt (in no secret file; `set_passwords.sh`
  leaves `maintenance` alone):

  ```bash
  docker compose exec -u postgres postgres psql -d people_db -c 'ALTER ROLE maintenance LOGIN' -c '\password maintenance'
  docker compose exec -u postgres postgres psql -d people_db -c 'ALTER ROLE maintenance NOLOGIN PASSWORD NULL'
  ```

`app_writer` uses no `pii` table: it reaches personal data only through six
`SECURITY DEFINER` functions, which the other roles may not execute —
`merge_people_verified_emails`, `ensure_people_pii`,
`upsert_publication_author_email`, `move_publication_author_emails`,
`get_introduction_contacts` and `log_introduction_email`. The integration
test
[`test_application_roles_integration.py`](DB/tests/integration/test_application_roles_integration.py)
and `run_operator_acceptance.sh` check these privileges and commands.

## The development setup: DB/compose.yaml

[`DB/compose.yaml`](DB/compose.yaml) is the setup PubDex is developed with,
the one [`QUICKSTART.md`](QUICKSTART.md) walks through, documented in
[`DB/README.md`](DB/README.md). Both setups bootstrap from `DB/init/`, with
the same roles, created without passwords; they share nothing else and can
run on one host, each with its own file, never one on the other's volume.

| | `compose.yaml` (this page) | `DB/compose.yaml` |
|---|---|---|
| Purpose | the deployment | development and the walkthrough |
| PubDex itself | the application image, in containers | the `people_pubs` package, from a virtual environment on the host |
| Names | per Compose project, none fixed | fixed, such as `peopledb-postgres` |
| PostgreSQL | on no host port by default | on loopback and on a private-network address |
| Passwords | secret files: `create_secrets.sh`, `set_passwords.sh` | `DB/.env`: `./sync_db_role_passwords.sh --apply` ([`DB/README.md`](DB/README.md), section 2.5) |
| Schema | the copy of `DB/init/` the image carries | `DB/init/` of the checkout |
| Backup and restore | [Backup and restore](#backup-and-restore) | `run_backup.sh`, `restore_db.sh` and the cron jobs of `DB/` |
| pgAdmin | not included | included |

## The application image

One image holds the ingestion commands and a preliminary read service with
liveness, readiness and one paged list endpoint, the foundation of the
versioned read API of task 3a, not that API. The command decides:

| Command | What it runs |
|---|---|
| `python -m people_pubs.api` | the read service (the image's default command) |
| `python -m people_pubs.sync.<command> [arguments]` | an ingestion command |
| `python -m people_pubs.api.probe [--live]` | the read service's health check |
| `python -m people_pubs.bootstrap <directory>` | copies the schema bootstrap into the directory (the `bootstrap` service) |

It holds Python 3.11.18 on Debian 13 (`python:3.11.18-slim-trixie` by
digest, less setuptools and pip), the runtime lock
[`DB/requirements.txt`](DB/requirements.txt) with psycopg's C
implementation, built against Debian 13's libpq (`libpq5`, PostgreSQL 17's
client library, which serves PostgreSQL 16), and under `/opt/pubdex`:

| Path | What |
|---|---|
| `people_pubs/` | the `people_pubs` package |
| `init/` | the schema bootstrap, the four files of [`DB/init/`](DB/init/) |
| `compose.yaml`, `compose.postgres-port.yaml` | the Compose files the image is released with |
| `create_secrets.sh` | the script that creates their secret files ([Secrets](#secrets)) |
| `set_passwords.sh` | the script that gives the database the files' passwords ([Changing a password](#changing-a-password)) |
| `tests/fixtures/golden/` | the demonstration's synthetic roster and payloads |
| `requirements-pip.txt`, `requirements.txt`, `LICENSE`, `LICENSE-CC-BY-4.0` | the lock of pip, the runtime lock and the two licences |

All of it is root's, directories 755 and files 644, readable and not
writable for the unprivileged user 10001 that everything runs as, which owns
only the empty `/var/lib/pubdex/initdb`, so that the `bootstrap` volume
mounted there is its own. The image holds no credential, reads no `.env`,
needs no writable path and works from any working directory. Without
Compose, run it with `--init`, so that
signals reach the process, and the password file mounted:

```bash
docker run --rm --init -p 127.0.0.1:8000:8000 \
  -e PEOPLE_PUBS_API_DSN="host=db.example.org dbname=people_db user=app_readonly" \
  -e PEOPLE_PUBS_API_DB_PASSWORD_FILE=/run/secrets/app_readonly_password \
  --mount type=bind,source="$PWD/secrets/app_readonly_password",target=/run/secrets/app_readonly_password,readonly \
  pubdex:local
```

Such a server has to authenticate the service by SCRAM; for one that does
not, set `require_auth` in `PEOPLE_PUBS_API_DSN` ([`DB/README.md`](DB/README.md),
section 11.1). The ingestion commands require SCRAM in the same way
([`DB/SCRIPTS.md`](DB/SCRIPTS.md)).

### The PostgreSQL image

The `postgres` service runs PostgreSQL 16 from the PostgreSQL image of the
release, built by [`postgres.Dockerfile`](postgres.Dockerfile) from
PostgreSQL's source, by its SHA-256, without SQL/XML support. PubDex runs no
SQL/XML, and in this image every XML function fails with "unsupported XML
feature" before any parser exists to call.

It keeps what `compose.yaml`, its health check, the schema bootstrap and the
operations on this page rely on, taken from `postgres:16.16-trixie` by
digest: the entrypoint scripts, checked by SHA-256 — `POSTGRES_PASSWORD_FILE`,
`POSTGRES_DB`, `/docker-entrypoint-initdb.d`, initialisation on an empty
volume only, SCRAM for remote clients — the `postgres` user and group 999,
the data directory `/var/lib/postgresql/data`, the socket in
`/var/run/postgresql`, `en_US.utf8`, and `psql`, `pg_dump`, `pg_restore`,
`pg_isready` and the rest of PostgreSQL's programs on `PATH`. It leaves out
what the deployment never configures: XML and XSLT, JIT compilation (LLVM),
GSSAPI, LDAP and PAM authentication, PL/Perl, PL/Python and PL/Tcl, and
message translations. The contrib extensions are those of the official image
but `xml2`; the schema's `citext`, `pgcrypto`, `unaccent` and `pg_trgm` are
checked in the build.

Its data directory is compatible with the official image's of the same major
release on Debian 13 — the same block and segment sizes and checksum setting,
glibc 2.41's collations, ICU, LZ4 and Zstandard built in: in tests with
synthetic data it started on a volume the official image had initialised,
with the same records, orderings and index checks, and dumps went both ways.
Switching an existing installation to it is still a change of the database
server's binaries: take a backup first ([Backup and
restore](#backup-and-restore)), and restoring that backup into a new stack
is the way back.

For the initial versioned release, the custom PostgreSQL image is adopted
as a fixed release snapshot. The published release will be preserved
unchanged, with its dated verification and known limitations. No fixed
maintenance schedule or security-update deadline is promised; future
updates, if provided, will use new versions, with their own acceptance
tests and scans. A frozen image receives no later security fixes, and its
dated verification does not establish its suitability for continued
production use.

To prepare a future update, a PostgreSQL minor release changes
`PG_VERSION` and `PG_SHA256` in `postgres.Dockerfile` — the hash from
postgresql.org's `.sha256` file, compared with that of PGDG's signed source
index — and usually the entrypoint image's digest and its scripts' hashes;
a Debian update changes the Debian image's digest. Its libraries come from
Debian 13's archive when the build step that installs them runs, so such a
build takes Debian's newest security updates of them; Docker's build cache
otherwise serves the step as an earlier build ran it, unless the build is
made without the cache (`./build_image.sh --no-cache`, as a PubDex release's
build is). The image's package database records which.

### Building it

```bash
./build_image.sh                                  # pubdex:local and pubdex-postgres:local
./build_image.sh --tag registry.example.org/team/pubdex:candidate \
  --postgres-tag registry.example.org/team/pubdex-postgres:candidate
```

[`build_image.sh`](build_image.sh) builds [`postgres.Dockerfile`](postgres.Dockerfile)
and [`Dockerfile`](Dockerfile) and records in both images' labels (`docker
image inspect --format '{{json .Config.Labels}}' pubdex:local`):

| Label | Value |
|---|---|
| `org.opencontainers.image.version` | the release version, from [`VERSION`](VERSION); a test release's `<VERSION>-test.<name>`, given with `--version` |
| `org.opencontainers.image.revision` | the commit the checkout is at; `<commit>-dirty` when the working tree differs from it, `unknown` outside a Git checkout, where a test release's copy states `<commit>-dirty` with `--revision` |
| `org.opencontainers.image.created` | the time of the build |
| `org.opencontainers.image.source` | the repository URL given with `--source`, otherwise empty |
| `pubdex.transform-version` | `people_pubs.__version__` (application image) |
| `pubdex.postgresql-version` | the PostgreSQL release built (PostgreSQL image) |
| `org.opencontainers.image.base.name`, `.base.digest` | the base image |

`people_pubs.__version__`, the transform version stamped into source
provenance, changes independently of `VERSION`. A working tree with changes,
an ignored Python module of the package included, builds a `<commit>-dirty`
image, which `build_image.sh` refuses to tag, either of them, with the
release version. The application image's build context is the allowlist of
[`.dockerignore`](.dockerignore) — the files of the table above, `DB/init/`
by name, and the four scripts that fetch and install the locks, which the
build runs without copying them — so no `.git`, environment file, secret,
data or virtual environment reaches the image; a new schema file has to be
added there and to the `Dockerfile`, which a test checks. The PostgreSQL
image's context, [`postgres.Dockerfile.dockerignore`](postgres.Dockerfile.dockerignore),
holds only the downloader and the licence of the entrypoint scripts,
[`third_party/docker-library-postgres/LICENSE`](third_party/docker-library-postgres/LICENSE),
which the image carries in `/usr/share/doc/pubdex-postgres/` with
PostgreSQL's own; the build checks it by SHA-256 and downloads nothing but
PostgreSQL's source.

Downloading, compiling and installing are separate stages, and no pip runs
with network access. [`DB/fetch_verified.py`](DB/fetch_verified.py)
downloads every file of the pip, build and runtime locks — and, for the
PostgreSQL image, PostgreSQL's source — over HTTPS with Python's own client,
keeping each only when its SHA-256 is the one the repository fixes. A stage
with a compiler and Debian's libpq headers then builds psycopg's C
implementation from its verified source archive, without network; the
image's stage installs Debian's `libpq5` and then, without network
(`RUN --network=none`), pip with [`DB/install_pip.py`](DB/install_pip.py)
and the runtime lock with [`DB/install_locked.py`](DB/install_locked.py),
from those files alone, hash-checked, from wheels only — the built psycopg-c
by the hash recorded at its build — so no compiler, header or build tool
reaches it, and psycopg fails rather than falling back to another
implementation (`PSYCOPG_IMPL=c`) ([`DB/README.md`](DB/README.md), section
10). The base images are fixed by digest, the locked files and PostgreSQL's
source by hash; Debian's packages come from its signed archive when the
build step that installs them runs, and from Docker's build cache when the
cache serves that step, which a build with `--no-cache`, as a PubDex
release's is, never lets it do. Builds of one commit hold the same Python
packages and PostgreSQL build, not the same bytes, and a build without the
cache takes Debian's newest security updates of the libraries it installs.
Each image lists the Debian packages of the
stage that compiled its C code — psycopg-c, PostgreSQL — with their
versions, the compiler's among them, in
`/usr/share/doc/pubdex/build-packages.txt` and
`/usr/share/doc/pubdex-postgres/build-packages.txt`; its own package
database names those it runs with. The base image's own pip never runs:
`DB/install_pip.py` checks the locked pip's wheel against its hash before
running it. pip checks the installed packages' requirements and is then
removed in the same build step, with the wheels from which the standard
library's `ensurepip` could install it again: no command of the image needs
pip, and neither pip nor those wheels are in a container's file system
(`python -m pip` and `python -m ensurepip` fail there). That keeps pip's
own advisories out of the image; it does not stop a process in a container
from installing or running software by other means — Python, a shell, dpkg
and apt remain.
`build_image.sh` refuses Docker's legacy builder.

### Selecting a published image

Version 0.2.0 is public at `ghcr.io/ccu-ukn/pubdex` and
`ghcr.io/ccu-ukn/pubdex-postgres`, next to the [canonical
repository](https://github.com/CCU-UKN/pubdex). Both images are
`linux/amd64`, built from source commit
`1ce756048dbd7b51ecdd526967df270bb5d4e26a`; their publication and
anonymous verification are recorded in [`TASK_2C.md`](TASK_2C.md),
*Public CI and the published PubDex release*. Version 0.1.0, built from
`57a72dc9f27766007b51c3d16ac7aa8c6ff3798b`, stays public; the [release
record](releases/README.md) names both by digest, and
[`TASK_2A.md`](TASK_2A.md), *Published-image evidence*, records 0.1.0's
publication. The application image carries the deployment
files, so no clone is needed. With the Engine and Compose versions checked
as in [The short path](#the-short-path), in an empty directory, whose name becomes the
project's:

```bash
mkdir pubdex
cd pubdex
docker run --rm ghcr.io/ccu-ukn/pubdex:0.2.0 tar -cC /opt/pubdex compose.yaml create_secrets.sh set_passwords.sh | tar -x
bash create_secrets.sh
docker compose up --wait
```

The scripts come out with mode 644, hence `bash`. Pulling needs no account,
and no `latest` tag is published. To pin the release's exact images, with
its schema and Compose file, give the digests in the first command and in
`.env` before the first start: a pull by digest sets no tag, so Compose
would otherwise pull the movable tags again.

```bash
docker run --rm ghcr.io/ccu-ukn/pubdex:0.2.0@sha256:868371ce79d94bb77ccd3fe570485b102d270c41f9d4cb311b028a9155de3c61 tar -cC /opt/pubdex compose.yaml create_secrets.sh set_passwords.sh | tar -x
echo 'PEOPLE_PUBS_IMAGE=ghcr.io/ccu-ukn/pubdex:0.2.0@sha256:868371ce79d94bb77ccd3fe570485b102d270c41f9d4cb311b028a9155de3c61' > .env
echo 'PEOPLE_PUBS_POSTGRES_IMAGE=ghcr.io/ccu-ukn/pubdex-postgres:0.2.0@sha256:ba88f35fbc031610cf1fc3f0f4f712568a1085360a3ef3a6b585336e3d4eab40' >> .env
```

To check a release before relying on it, run in a clone, at any time:

```bash
./run_stack_acceptance.sh --release --pull \
  --image ghcr.io/ccu-ukn/pubdex:0.2.0@sha256:868371ce79d94bb77ccd3fe570485b102d270c41f9d4cb311b028a9155de3c61 \
  --postgres-image ghcr.io/ccu-ukn/pubdex-postgres:0.2.0@sha256:ba88f35fbc031610cf1fc3f0f4f712568a1085360a3ef3a6b585336e3d4eab40 \
  --expect-version 0.2.0 --expect-revision 1ce756048dbd7b51ecdd526967df270bb5d4e26a
```

## What identifies a deployment

A deployment is an application image, a PostgreSQL image and the
`compose.yaml` they run with. The application image holds the services, the
schema bootstrap and the Compose files of its release; the labels of both
images name the version and the commit; `compose.yaml` holds the settings,
the health checks and, as its defaults, the release's images by version tag.
It cannot name them by digest, since the images record the commit, which has
to exist first.

| Part | Pinned by |
|---|---|
| the application image | its digest, which the [release record](releases/README.md) names and which you give in `PEOPLE_PUBS_IMAGE`; the version tag in `compose.yaml` is only the default, and a tag can be moved |
| the schema and the role setup | the image: the baseline and the role setup in `/opt/pubdex/init`, the schema changes in `/opt/pubdex/schema`; the database's record, the table `pubdex.schema_changes`, names the schema changes it has had |
| `compose.yaml` | the image: `/opt/pubdex/compose.yaml` is the release's copy |
| the PostgreSQL image | its digest, which the release record names and which you give in `PEOPLE_PUBS_POSTGRES_IMAGE`; its labels name the same version and commit as the application image's |

A local build carries the files of its working tree and is labelled
`<commit>-dirty` when the tree had changes. An image of one release with
the `compose.yaml` of another is not a supported deployment, so an upgrade
replaces all three together ([Upgrading to a new
release](#upgrading-to-a-new-release)). The schema bootstrap runs only on an
empty volume, and the schema upgrade of every start adds the schema changes
of the image in use that the database has not had; a dump holds the schema
of its release with its record of schema changes.

## Upgrading to a new release

An upgrade replaces the two images and the deployment files with those of
the new PubDex release, whose application image carries them; the database
stays in its volume, and the schema upgrade of the next start brings it to
the new release's schema, applying the schema changes it has not had yet
(what other tools call a migration). [`VERSIONING.md`](VERSIONING.md) says
which upgrades are supported: from every PubDex release from the floor on,
and a rollback only to the PubDex release you upgraded from. Read the new
release's section in [`CHANGELOG.md`](CHANGELOG.md) first: *Upgrade and
rollback* names the floor and says whether the way back is an image
rollback or a restore. An upgrade to images built from a checkout between
PubDex releases is not covered.

For a deployment built from a clone as in [The short path](#the-short-path),
there is no upgrade path for the locally built images. Take a backup, start
a separate deployment of the published PubDex release the clone was at, as
in [Selecting a published image](#selecting-a-published-image), restore the
backup there with [Backup and restore](#backup-and-restore), and then
upgrade as below. Do not extract deployment files into the clone: they
would overwrite tracked files. Stage 7 runs the restore into the same
PubDex release, and stage 11 runs the upgrade. If the clone was at no
published PubDex release, the dump restores only into the PubDex release
whose schema it holds.

Run every command in the directory of the deployment's `compose.yaml`,
whose `.env` names the images in use by digest, as in [Selecting a
published image](#selecting-a-published-image).

**1. A backup**, before every upgrade, as in [Backup and
restore](#backup-and-restore). It is the only way back after a schema
change that is not backward compatible. Keep the two image lines of `.env`
as well: they are the way back after any other upgrade.
Run the verification query now and keep its counts for the comparison
after a restore.

```bash
docker compose exec -T postgres psql -U postgres -d people_db -XAtc 'SELECT (SELECT count(*) FROM app.people), (SELECT count(*) FROM biblio.publications), (SELECT count(*) FROM biblio.publications_canon_web)'
(umask 077 && docker compose exec -T postgres pg_dump -U postgres -Fc people_db > people_db.dump)
```

**2. The new images, by digest.** The [release record](releases/README.md)
names both: `images` in `releases/<version>/release.json`, and the
PubDex release's file `DIGESTS`, the application image first. A tag can be
moved; a digest cannot. These are the two lines of `.env` to change:

```text
PEOPLE_PUBS_IMAGE=ghcr.io/ccu-ukn/pubdex:<version>@sha256:<application image digest>
PEOPLE_PUBS_POSTGRES_IMAGE=ghcr.io/ccu-ukn/pubdex-postgres:<version>@sha256:<PostgreSQL image digest>
```

**3. The upgrade.** Stop the stack with the `compose.yaml` it runs with,
change the two lines, take the new release's deployment files out of its
application image, which replace the old ones, and start:

```bash
docker compose down                        # the database stays in its volume
#   now the two image lines of .env, as in 2
docker run --rm ghcr.io/ccu-ukn/pubdex:<version>@sha256:<application image digest> tar -cC /opt/pubdex compose.yaml create_secrets.sh set_passwords.sh | tar -x
docker compose up --wait                   # the schema upgrade applies the new schema changes, then the read service starts
```

Your settings stay in `.env` and the password files in `secrets/`, which
the new files leave alone. `docker compose up --wait` returns once the
read service is healthy, which it is only on a schema it supports.

**4. The checks.** The schema upgrade's log lists each schema change it
applied, `schema: applying <name> (backward compatible)` and then `schema:
done: applied …`, and `docker compose ps --all` shows `schema-upgrade`
exited with 0 and the other services healthy:

```bash
docker compose logs schema-upgrade
docker compose ps --all
```

If the schema upgrade refuses the database or fails, `docker compose up
--wait` fails with `service "schema-upgrade" didn't complete successfully:
exit 1`, nothing that uses the database starts, and the log says why. The
database is as the last schema change that succeeded left it, each change
having run in a transaction of its own.

**5. An image rollback**, after an upgrade whose schema changes are all
backward compatible, as *Schema changes* in the release notes says: the
same commands with the previous release's references, which you kept in 1:

```bash
docker compose down
#   the two image lines of .env back to the previous release's references
docker run --rm ghcr.io/ccu-ukn/pubdex:<previous version>@sha256:<its application image digest> tar -cC /opt/pubdex compose.yaml create_secrets.sh set_passwords.sh | tar -x
docker compose up --wait
```

The schema stays as the upgrade left it, with its record. The previous
release's schema upgrade logs `schema: the database also has 1 schema
change of a later PubDex release, all backward compatible: <name>` and
`schema: nothing to do`, and its read service and ingestion work on that
schema. A PubDex release without a schema upgrade of its own, such as
0.1.0, has no `schema-upgrade` service in its `compose.yaml`.

**6. A rollback by restore**, after an upgrade that applied a schema change
that is not backward compatible. The previous release refuses that
database: its schema upgrade logs `schema: refused: the database has schema
changes of a later PubDex release that are not backward compatible:
<name>.`, so `docker compose up --wait` fails as in 4. Delete the upgraded
database, return to the previous release's files and images, and restore
the backup of 1 with the commands of [Backup and
restore](#backup-and-restore):

```bash
docker compose down --volumes              # deletes the upgraded database, with everything written since the backup
#   the two image lines of .env back to the previous release's references
docker run --rm ghcr.io/ccu-ukn/pubdex:<previous version>@sha256:<its application image digest> tar -cC /opt/pubdex compose.yaml create_secrets.sh set_passwords.sh | tar -x
docker compose up --wait postgres
docker compose stop api
docker compose exec -T postgres dropdb -U postgres --force people_db
docker compose exec -T postgres createdb -U postgres people_db
docker compose exec -T postgres pg_restore -U postgres -d people_db --single-transaction --exit-on-error < people_db.dump
docker compose up --wait                   # its schema upgrade finds the dump's record: nothing to do
```

The verification query of [Backup and restore](#backup-and-restore) then
counts what it counted at the backup.

### What ran these commands

[`run_upgrade_acceptance.sh`](run_upgrade_acceptance.sh), stage 11 of
`./run_ci_suite.sh`, runs this section's commands as an operator types
them, in Compose projects of its own, on test releases of the checkout and
on the published preceding PubDex release; [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md),
*Upgrades between PubDex releases on Compose*, numbers its steps. One row
per block of commands, in order; [`DB/tests/test_deployment_guide.py`](DB/tests/test_deployment_guide.py),
in stage 1, checks that each `docker` command of a block is one the steps
named run: its words, in their order, on one command line of those steps,
which may add options of the run's own; the last column names them.

| Block | Ran by | Differences and limits |
|---|---|---|
| `pg_dump -U postgres -Fc people_db` | stage 11, steps 1 and 6 | before the upgrade from test release A (step 1), with the verification query run first and its counts kept; step 6 ran the dump before the upgrade from the published preceding release, 0.1.0, without that query |
| `pubdex:<version>@sha256` | stage 11, steps 1, 3 and 6 | `.env` was written whole, naming the project, both images by local tag for test releases and by digest for 0.1.0, and a port of the run's own; `up` also got `--wait-timeout`. To test release B1, with a backward-compatible schema change (step 1), to B2, with one that is not (step 3), and from 0.1.0 to A (step 6): each schema upgrade applied exactly the new schema changes and recorded them, and the read service served the publication |
| `docker compose logs schema-upgrade` | stage 11, steps 1 to 6 | with `--no-color`, after every start; `ps --all` ran after the refusal of step 4 only, with `--format` |
| `pubdex:<previous version>@sha256` | stage 11, steps 2 and 6 | from B1 back to A (step 2), after which A's ingestion wrote on B1's schema with A's transform version, and from A back to 0.1.0 (step 6), whose read service served on A's schema; `.env` and `--wait-timeout` as above |
| `docker compose down --volumes` | stage 11, steps 4 and 5 | A's files and images were already in place from the image rollback that A refused in step 4, so `down --volumes` ran after them, with A's `compose.yaml`; `--wait-timeout` as above. The verification query counted what it counted before the backup |

## Checking a deployment


```bash
./run_stack_acceptance.sh                                       # builds both images from this checkout
./run_stack_acceptance.sh --image <reference> [--postgres-image <reference>]   # images Docker has
./run_stack_acceptance.sh --image <reference> --pull --expect-version <version> --expect-revision <commit>
./run_stack_acceptance.sh --release --image <reference> [--postgres-image <reference>] [--pull] \
  --expect-version <version> --expect-revision <commit>
./run_operator_acceptance.sh [--image <reference> [--postgres-image <reference>]]
```

[`run_stack_acceptance.sh`](run_stack_acceptance.sh), stage 6 of
`./run_ci_suite.sh`, starts `compose.yaml` on empty volumes with the images
it built or the ones you name, never replaced; without `--postgres-image` the
PostgreSQL image is the one the started `compose.yaml` names. It checks both
images' labels (`--expect-version`, `--expect-revision`) and digests, the
application image's files and schema changes, the schema upgrade and its
record in the database, and that SQL/XML is unavailable in the running
server; runs the demonstration and reads it over HTTP; stops PostgreSQL,
recreates the containers and takes the stack down and up; and finds no
password in any output. `--release` verifies a release: it starts the
`compose.yaml` the image carries, which has to name the expected version for
both images and run the schema upgrade, and needs no checkout files or Git;
with `--pull` both images
are pulled by digest. [`run_operator_acceptance.sh`](run_operator_acceptance.sh),
stage 7, carries out this page's procedures as written, configured by an
`.env` file on the ordinary network: setting up, a roster import, an
ingestion, the refresh, the export, both ways of changing the passwords, a
backup restored into a new stack, and the role model. Each runs in a Compose
project of its own that it removes afterwards, also after a failure or an
interrupt, never touching your installation, and takes about a minute plus
the build. Both need Bash, Python 3, Docker Engine 28.0.1 or later, except
28.2.0 to 28.3.2, the Compose plugin 2.24.0 or later on the same machine, and
buildx to build, and refuse other versions before they create anything.
Their check of the loopback publication reads the bindings Docker reports:
neither connects from another host, so neither shows that other hosts are
refused.
[`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) lists every check.

## Publishing an image

The images are published as part of a PubDex release, by the publish step
of [`release.sh`](release.sh), which only the maintainer runs:
[`RELEASING.md`](RELEASING.md) is the procedure, from the version to the
release record, and says which of its commands the suite runs. Nothing in
the repository or its CI pushes an image, and no test release ever leaves
the machine that built it. A PubDex release is both images, published
together for `linux/amd64` alone, the platform built and tested ([Supported
platforms](#supported-platforms)); a version tag is never moved. A fork
publishes its own under its own registry and namespace, with a
`compose.yaml` that names its images. A copy by digest on another registry
with anonymous pulls, or an equivalent build from the source archive while
the base images and packages remain available, keeps the images from
depending on one host.

## Troubleshooting

- **`Docker Engine <version> is too old`**, or **`… one of the releases in
  which a firewalld reload opens ports published on 127.0.0.1`**, from an
  acceptance test or the suite — the stack would start on that Engine, but
  other hosts could reach its loopback port: upgrade it ([What you
  need](#what-you-need)).
- **`pull access denied for pubdex`**, or **`…/manifests/<version>": denied`**
  — Docker lacks the image `PEOPLE_PUBS_IMAGE` names and cannot pull it: build
  `pubdex:local` and name it, or name a published release. The same for
  **`pubdex-postgres`** and `PEOPLE_PUBS_POSTGRES_IMAGE`: `./build_image.sh`
  builds `pubdex-postgres:local`.
- **`the Docker buildx plugin was not found`** — install `docker-buildx`
  (Debian, Ubuntu) or `docker-buildx-plugin` (Docker's packages).
- **`service "bootstrap" didn't complete successfully`** — PostgreSQL was not
  started; `docker compose logs bootstrap` says why. `No module named
  people_pubs.bootstrap`: the image predates this `compose.yaml`. `cannot
  write ...: Permission denied`: something else created the volume
  `<project>_initdb`, which holds no data — `docker volume rm` it and start
  again.
- **`bind source path does not exist: .../secrets/...`** — run
  `./create_secrets.sh`, with Compose's `PEOPLE_PUBS_SECRETS_DIR` if you set one.
- **`dependency failed to start: container ...postgres... is unhealthy`** on
  the first start — the initialisation failed; `docker compose logs postgres`
  says why (for example `020_role_passwords.sh: the file
  PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE names is empty`). Fix the cause, delete
  the new database with `docker compose down --volumes`, and start again.
- **`/readyz` 503 `database_unavailable` with PostgreSQL healthy** — the read
  service's password file and the database disagree: [Changing a
  password](#changing-a-password). With a database outside this deployment,
  the server may also ask for another authentication than SCRAM, which the
  service refuses ([`DB/README.md`](DB/README.md), section 11.1).
- **`set_passwords.sh`: `a new api container reads another password`** — give
  the script your `.env`'s `PEOPLE_PUBS_SECRETS_DIR` or your `-p`, `-f`,
  `--env-file` options. Nothing was changed.
- **`/readyz` 503 `read_surface_unavailable`** — the snapshot is missing,
  unreadable for `app_readonly` or unpopulated; a database restored from
  elsewhere may lack the runtime patch ([`DB/README.md`](DB/README.md),
  section 3.2).
- **`service "schema-upgrade" didn't complete successfully: exit 1`** — the
  schema upgrade refused the database or a schema change failed; nothing
  that uses the database was started, and `docker compose logs
  schema-upgrade` says why. `schema: refused: the database has schema
  changes of a later PubDex release that are not backward compatible`
  after a return to older images: restore the backup taken before the
  upgrade ([Upgrading to a new release](#upgrading-to-a-new-release), 6).
- **`pg_restore: error: ... already exists`** — the database was not empty:
  drop and create it first ([Backup and restore](#backup-and-restore)).
- **`port is already allocated`** — set `PEOPLE_PUBS_HTTP_PORT`.
- **`cannot read the roster /data/roster.csv: permission denied`** — give the
  roster on standard input ([Importing a roster](#importing-a-roster)).
- **An empty publication list** — configure the attribution rules
  ([Ingestion](#ingestion)), and refresh after an ingestion ([The read
  service](#the-read-service)).
