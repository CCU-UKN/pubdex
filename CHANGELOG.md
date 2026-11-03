# Changelog

Every funded result of this project is recorded here as its own entry: what it
delivered, the files that carry it, and how it can be verified.

PubDex starts from a private publication-reporting prototype whose original
development predates the grant. That prototype is the starting point of this
repository, and prototype functionality included here is not claimed as funded
work; the entries below describe the funded results built on top of it.
Background: [Origins and funding](README.md).

## Container image and Compose deployment — version 0.1.0 published 2026-11-03

Task 2a: a versioned container image for the ingestion and API service, and a
one-command Compose deployment with PostgreSQL. This entry records what the
repository holds: the read service, the application image with its build, the
Compose deployment with its documentation, its acceptance test as the
sixth stage of the complete suite, and the test of the operator's workflow as
the seventh. The image holds the ingestion commands and a preliminary read
service with liveness, readiness and one paged list endpoint, which is the
foundation of the versioned read API of task 3a and not that API. Both
`ghcr.io/ccu-ukn/pubdex:0.1.0` and
`ghcr.io/ccu-ukn/pubdex-postgres:0.1.0` are published for `linux/amd64`,
from source commit `57a72dc9f27766007b51c3d16ac7aa8c6ff3798b`.
[Public CI](https://github.com/CCU-UKN/pubdex/actions/runs/41442310444)
passed on that commit on 2026-11-02; anonymous retrieval and release
acceptance by both digests passed on 2026-11-03. External acceptance of
the task is not asserted. Earlier private Forgejo results remain attached
to their original provisional commit.
[`TASK_2A.md`](TASK_2A.md) is the requirement-by-requirement record.

The maintainer's release-policy decision of 2026-10-30 adopts the custom
PostgreSQL image for the initial versioned release, preserved unchanged
with dated verification and known limitations. No fixed maintenance
schedule or security-update deadline is promised; future updates, if
provided, will use new versions ([`DEPLOYMENT.md`](DEPLOYMENT.md), *The
PostgreSQL image*).

### The read service

- [`people_pubs.api`](DB/people_pubs/api/), started with
  `python -m people_pubs.api`, serves the canonical publication list over
  HTTP from the serving snapshot `biblio.publications_canon_web`.
  `GET /v0/publications` returns bounded pages — `limit` from 1 to 100,
  default 25; `after` a `pub_id`; `next_after` for the next page, `null` on
  the last — whose items carry exactly the columns of the CSV export;
  `GET /livez` reports the process, and `GET /readyz` whether a database
  connection is available in time and the snapshot can be read with it,
  naming the reason when not. Everything under `/v0/` is preliminary, with
  no compatibility promise: it is the foundation of the versioned read API
  of task 3a, not that API.
- It cannot write: only `GET` and `HEAD` are served, it runs two fixed
  `SELECT` statements with the page bounds passed as parameters, and every
  session runs with `default_transaction_read_only` and a statement timeout
  that the connection string cannot override, normally as `app_readonly`.
  Invalid, unknown or repeated parameters are refused before the database is
  asked; errors are JSON with a fixed message per code, so no response
  carries an exception text, SQL or connection detail.
- Its configuration is its own: the `PEOPLE_PUBS_API_*` variables, a
  connection string that may not hold a password, and the password read from
  a file. It reads neither `DB/.env` nor `PEOPLE_DB_DSN`, removes libpq's
  `PG*` variables from its environment, imports no ingestion or provider
  code, and masks the password in every log line. It starts without the
  database, bounds every wait for it, replaces connections the server drops,
  and stops gracefully on SIGTERM or SIGINT; a configuration error ends it
  with status 2, naming the variable but not its value, and a failure to
  listen with status 3. [`DB/README.md`](DB/README.md), section 11, documents
  the contract and the settings.
- Its connections, like those of every ingestion command, require the
  server to authenticate them by SCRAM — or, for the service without a
  password file, to ask for no authentication — and do not negotiate
  GSSAPI encryption ([`people_pubs.connection_security`](DB/people_pubs/connection_security.py)).
  A server's request for a cleartext or MD5 password, GSSAPI or SSPI ends
  the connection before it is answered, so no password is sent that way
  and libpq does not call Kerberos (until 2026-10-30, the copy inside
  psycopg-binary; since then, Debian's). A connection
  string, or for ingestion `PGREQUIREAUTH` and `PGGSSENCMODE`, that sets
  `require_auth` or `gssencmode` keeps its own value, for an external
  server that authenticates otherwise; a server asking for MD5 or a
  cleartext password is refused unless so configured. The transform
  version moves to 1.5.3; importers write the same rows.
- The Python dependencies have separate runtime and development locks,
  [`DB/requirements.txt`](DB/requirements.txt) and
  [`DB/requirements-dev.txt`](DB/requirements-dev.txt), with exact versions
  and SHA-256 hashes. [`DB/requirements-pip.txt`](DB/requirements-pip.txt)
  pins the pip installer. The service adds Starlette 1.7.0, uvicorn 0.54.0,
  psycopg-pool 3.3.3 and click 8.5.0. The locks update anyio to 4.14.2,
  certifi to 2026.10.19, h2 to 4.4.1, hpack to 4.2.0, idna to 3.15 and
  psycopg to 3.3.6; sniffio leaves the locks. The shared setup and image
  build procedure is described below. The suite checks its prerequisites
  before stage 1.
- Tests: 138 offline tests drive the application in process from a stand-in
  data source, the configuration, the database layer without a database and
  the real server process on a Unix-domain socket
  ([`DB/tests/test_api_app.py`](DB/tests/test_api_app.py),
  [`DB/tests/test_api_settings.py`](DB/tests/test_api_settings.py),
  [`DB/tests/test_api_server.py`](DB/tests/test_api_server.py)); 10
  integration tests serve, as `app_readonly`, the publication that the
  demonstration's own roster import, fixture ingestion and attribution
  produce, and cover paging, the empty page, read-only sessions, an absent
  database, a role without access, an unpopulated snapshot, dropped
  connections and recovery after the database refused connections
  ([`DB/tests/integration/test_api_integration.py`](DB/tests/integration/test_api_integration.py)).

### The application image

- [`Dockerfile`](Dockerfile) builds one image for the preliminary read
  service — its default command — and every
  `python -m people_pubs.sync.<command>`: Python
  3.11.18 on Debian 13 (`python:3.11.18-slim-trixie`, fixed by digest) less
  the setuptools it ships with, the runtime lock installed by pip 26.2.2 —
  which `DB/install_pip.py`, mounted for that build step only, installs
  from its hash-checked wheel, so that the base image's pip 24.0 never runs
  — with `--require-hashes` from binary wheels only and checked with `pip
  check`, after which pip and the wheels `ensurepip` installs it from are
  removed, so that neither is in the image's file system, the
  `people_pubs` package, the demonstration's synthetic roster and payloads,
  the schema bootstrap of `DB/init/` (`/opt/pubdex/init`), the two Compose
  files, `create_secrets.sh`, `set_passwords.sh` and the two licences, all
  root's with directories 755 and files 644 whatever the checkout's modes, built with
  the Engine the deployment supports (below). It runs as the unprivileged
  user 10001 from any working directory, needs no writable path, reads no
  `.env` file and holds no credential. `python -m people_pubs.api.probe`, new, is its health check
  without curl, and `python -m people_pubs.bootstrap`, new, copies the
  schema bootstrap into a directory.
- [`.dockerignore`](.dockerignore) is an allowlist: the build context holds
  the runtime lock and pip's, `DB/install_pip.py`, which the build runs
  without copying it, the package's Python modules, the four
  fixture files, the four files of `DB/init/` by name, the two Compose files,
  `create_secrets.sh`, `set_passwords.sh` and the licences, and nothing else
  of a checkout.
- [`build_image.sh`](build_image.sh) builds it and labels it with the
  release version from the new [`VERSION`](VERSION) file (0.1.0), the source
  commit — `<commit>-dirty` for a working tree with changes, or with a
  Python module of the package that Git ignores but the build context takes
  in, and `unknown` outside a Git checkout — the build time, and the
  transform version
  `people_pubs.__version__`, which stays a separate number. It refuses the
  release version as the tag of a modified working tree, builds with
  BuildKit only, saying what to install when the buildx plugin is missing,
  and pushes nothing.

### The Compose deployment

- [`compose.yaml`](compose.yaml) in the repository root, the canonical way
  to run PubDex, runs PostgreSQL
  16.15 — fixed by digest until 2026-10-30, since then the PubDex PostgreSQL
  image (*Security fixes of 2026-10-30*) — and the read service from that
  image, with the
  ingestion commands (`docker compose run --rm ingest ...`) and the synthetic
  demonstration (`docker compose run --rm demo`) on request only. It needs
  no checkout: a one-shot `bootstrap` service, run from the application
  image without network or secret, copies the schema bootstrap the image
  carries into a volume that PostgreSQL mounts read-only at
  `/docker-entrypoint-initdb.d`, and PostgreSQL starts only once it has
  succeeded. Its default image is the release it belongs to,
  `ghcr.io/ccu-ukn/pubdex:0.1.0`; after a one-time `./create_secrets.sh`,
  `./build_image.sh` and `PEOPLE_PUBS_IMAGE=pubdex:local` in `.env`, `docker
  compose up --wait` starts it. Its defaults are portable: no fixed
  container, network or volume name, the read service published on
  127.0.0.1 only — which keeps other hosts out on the Engines the
  deployment supports (below) — PostgreSQL on no host port unless
  [`compose.postgres-port.yaml`](compose.postgres-port.yaml) is added, named
  volumes for the database and the schema bootstrap, and an application
  image Compose never builds and pulls only when Docker does not have it,
  so that for a published image the secret files are the only preparation;
  the image carries `compose.yaml`, `create_secrets.sh` and
  `set_passwords.sh` for a directory without a clone. The application containers run with an
  init process, a read-only root file system, no capabilities and no new
  privileges.
- Credentials come from three secret files that
  [`create_secrets.sh`](create_secrets.sh) creates once with random passwords
  and never overwrites. Each container receives only its own: the read
  service `app_readonly`'s, ingestion `app_writer`'s through the new
  `PEOPLE_DB_PASSWORD_FILE`, and the superuser's only the database container.
  No password is in a container's configured environment, a command line, a
  build argument, the image or the rendered Compose configuration; an
  ingestion command hands its password to libpq within its own process
  environment.
  [`DEPLOYMENT.md`](DEPLOYMENT.md) opens with the short path — prerequisites,
  the preparation, the one command, the demonstration, stopping and
  removing — and documents setup, every setting, the secrets and what their
  storage does not protect, health and readiness, storage, a backup with
  `pg_dump` and its restore with `pg_restore` into this deployment or a new
  one, ingestion, logs, the image and its build, and how a published image
  will be selected. [`DB/compose.yaml`](DB/compose.yaml) stays the
  development and walkthrough setup, which DEPLOYMENT.md compares with the
  deployment in one table. Scheduled backups, retention and recovery drills
  are task 4a.
- A new database no longer starts with known role passwords:
  [`DB/init/000_init.sql`](DB/init/000_init.sql) creates `app_readonly`,
  `app_writer` and `maintenance` without a password, also for the setup in
  `DB/compose.yaml`, whose documented `sync_db_role_passwords.sh --apply`
  step then sets them. In the Compose deployment the new
  [`DB/init/020_role_passwords.sh`](DB/init/020_role_passwords.sh) gives the
  two application roles their passwords from the secret files on the first
  start — through psql's `\password`, so the server receives only the SCRAM
  verifier — and takes the login away from `maintenance`. PostgreSQL's
  health check includes those passwords, so a first start that failed
  part-way never becomes healthy and nothing that needs the database starts.
- [`set_passwords.sh`](set_passwords.sh), next to `create_secrets.sh` in the
  repository and in the image, changes passwords: after an operator changed
  a secret file — overwritten in place or replaced, by any editor — it gives
  `postgres`, `app_readonly` and `app_writer` the passwords the host's files
  hold now. It checks the files (`create_secrets.sh --check`), that
  PostgreSQL runs and that new containers of the read service and of
  ingestion read the host's bytes, then sends the passwords on standard
  input to `020_role_passwords.sh --stdin` in the database container, which
  sets each through `\password` and has to open a new connection as each
  role that the server authenticated by SCRAM. It replaces the in-container
  `--resync`, which read the files the running container still saw in their
  old version after a replacement, and so could set the old passwords again
  and report success; a replacement no longer needs a restart of
  PostgreSQL. [`DEPLOYMENT.md`](DEPLOYMENT.md), *Roles and personal data*,
  documents the roles of this deployment: `maintenance` without a login, its
  work done by the superuser on the database's socket with `SET ROLE
  maintenance` or through a login given with a documented command, and
  `app_writer` reaching `pii` only through its definer functions.
- `app_writer` can refresh the serving snapshots: the runtime patch
  [`DB/init/010_ingestion_runtime_schema.sql`](DB/init/010_ingestion_runtime_schema.sql)
  adds `biblio.refresh_serving_snapshots()`, a `SECURITY DEFINER` function
  that runs as the snapshots' owner with a fixed `search_path`, does what
  `refresh_publications_canon_web.sql` does, and only `app_writer` may
  execute. `attribution_rules` refreshes through it, so the whole
  demonstration runs as the ingestion role; databases that predate it need
  the runtime patch re-applied. With `PEOPLE_DB_PASSWORD_FILE`, this changes
  `people_pubs.__version__` to 1.5.2; no importer transform changed.
- A roster reaches the ingestion container on standard input, `docker
  compose run --rm -T ingest python -m people_pubs.sync.add_people --csv - <
  roster.csv`, so its file can stay readable by its owner alone: through a
  bind mount the container's user 10001 cannot read a private file.
  `add_people --csv -` reads UTF-8, a byte-order mark allowed, and refuses a
  terminal. For a file and standard input alike, a roster that is empty, has
  no `orcid` column or is not UTF-8 now stops the import before it connects,
  and a line the CSV reader cannot read stops it with that line's number and
  nothing written; the messages number rejected rows instead of quoting
  them, and name people by ORCID iD only. No written row changes.
- Tests: 197 offline tests run the role-password script, the secret
  initialiser and the build script against stubs, pin the password-file
  format shared by all of them, the health probe, and the Dockerfile — the
  removed setuptools, `pip check` before pip and ensurepip's wheels are
  removed, the pins and hashes of the locks, pip's release and
  the GitHub workflow's Python, the image's, included —
  `.dockerignore` and Compose files; 37 run `set_passwords.sh` against a
  stub `docker`
  ([`DB/tests/test_set_passwords_script.py`](DB/tests/test_set_passwords_script.py))
  and 10 the copy of the schema bootstrap
  ([`DB/tests/test_bootstrap.py`](DB/tests/test_bootstrap.py)); 18 more in
  [`DB/tests/test_roster_and_export.py`](DB/tests/test_roster_and_export.py)
  read rosters from standard input and from files, and stop an unusable one
  before anything is written; 3 integration tests prove the refresh
  function's privileges and serve the demonstration ingested as `app_writer`
  ([`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *What is covered now*).

### The acceptance test, in the suite and in CI

- [`run_stack_acceptance.sh`](run_stack_acceptance.sh), new, is the
  acceptance test of the deployment and stage 6 of
  [`run_ci_suite.sh`](run_ci_suite.sh), so both workflows run it with the
  steps they had, pip now installed by `DB/install_pip.py` from
  `DB/requirements-pip.txt` and the packages from binary wheels:
  the GitHub workflow for pull requests and pushes to
  `main`, the Forgejo workflow for every push and pull request. It builds
  the image from the checkout
  with `build_image.sh`, or takes the image `--image` names — pulled with
  `--pull`, checked for its digest and, with `--expect-version` and
  `--expect-revision`, exactly those labels, never rebuilt — creates the secret files with
  `create_secrets.sh`, and starts `compose.yaml` with `docker compose up
  --wait` from an empty volume. It then runs the synthetic demonstration
  inside the stack as `app_writer`, reads its publication over HTTP as
  `app_readonly` and its cumulative provenance in the database, stops and
  starts PostgreSQL (readiness fails and recovers without a restart of the
  service), recreates the containers and runs `down` and `up` on the same
  volume (the same records, no new initialisation), and checks that the
  environments of the database server and of the read service — each read
  as the user its process runs as — hold no password setting, and that no
  password occurs in the logs of the stack's containers, saved before each
  recreation, the output of its one-shot commands, the rendered
  configuration or `docker inspect` output. Each one-shot command runs
  under a time limit that stops it.
- It owns what it creates: the project `pubdex-acceptance-<run id>`, a run
  label on every container, network and volume, an image tag and a private
  directory for the secret files; it refuses to start when any of them
  exists already, binds every Compose call to its own project, files and
  settings in a cleared environment, and afterwards removes all of it, also
  after a failure or a signal, confirming it. The test-only addition
  [`DB/tests/compose.acceptance.yaml`](DB/tests/compose.acceptance.yaml)
  labels the resources and puts PostgreSQL and ingestion on an internal
  network without a route off the host, so the small ingest provably needs
  no metadata provider; `compose.yaml` keeps its ordinary network.
- A deployment is the image and the `compose.yaml` it carries, which the
  image's digest covers together with the schema; `compose.yaml` names its
  release by version tag, since the image built from a commit records that
  commit and its digest exists only afterwards, and the digest goes into
  `PEOPLE_PUBS_IMAGE` ([`DEPLOYMENT.md`](DEPLOYMENT.md), *What identifies a
  deployment*). Selecting a published image needs no clone. Every
  acceptance run starts a `compose.yaml` alone in a project directory of its
  own, checks that the stack mounts nothing but its volumes and the secret
  files, prints the image's identity and says whether the image carries the
  `compose.yaml` it starts; a build has to carry the checkout's `compose.yaml`
  and `DB/init/`, and an image that carries no `compose.yaml` is refused.
  `--release` verifies a release: it requires the image's labels to be
  exactly the `--expect-version` and `--expect-revision`, and starts the
  `compose.yaml` the image carries, whose default image has to be that
  version, with no part of the checkout but the test; a label that is
  missing, `unknown`, malformed or `-dirty` fails, the version is never
  read from a tag, and `--pull` needs a digest. `--expect-version` and
  `--expect-revision`, which now has to be a full commit id, work without
  `--release` as well, for development runs of local `<commit>-dirty`
  builds. The rules are shared with `build_image.sh` in
  [`DB/lib/release_identity.sh`](DB/lib/release_identity.sh), and Git is
  only asked, for a build: `git status` runs with `--no-optional-locks`, so
  no index is written.
- `run_ci_suite.sh` checks before its first stage for Docker Engine 28.0.1 or
  later, except 28.2.0 to 28.3.2, to build the image as to run it, and the
  Compose plugin 2.24.0 or later, the first release that
  applies the health checks' `start_interval`, says what to install when one
  is missing, and reports the Compose version with the other versions. The
  suite and both acceptance tests compare full release numbers through
  [`DB/lib/docker_versions.sh`](DB/lib/docker_versions.sh): a `v` prefix and
  a distribution's build suffix are accepted, a pre-release ranks below its
  release, and a version that cannot be read stops the run. Neither `docker
  compose` nor the setup scripts check these versions, so
  [`DEPLOYMENT.md`](DEPLOYMENT.md) has the operator check them before the
  first start.
- The Engine requirement follows from the network, as of 2026-10-28: the
  read service has no authentication and no TLS and relies on its
  publication on 127.0.0.1, and Docker documents that before Engine 28.0.0
  hosts on the same local network can reach ports published there, while in
  28.2.0 to 28.3.2 a firewalld reload removes the filter again
  (CVE-2025-54388, fixed in 28.3.3). 28.0.1 replaced the `ipset`-based rules
  of 28.0.0. Earlier Engines built the image and started the stack in the
  tests of 2026-10-23 and 2026-10-27, 25.0.5 and Debian 13's 26.1.5 among
  them; they are refused because of the network, not because anything
  failed. The acceptance tests check the bindings Docker reports; none
  connects from another host.
- [`DEPLOYMENT.md`](DEPLOYMENT.md), *Supported platforms*, is the one
  statement of where the deployment is supported, as of 2026-10-28: Linux
  on x86_64 with the Engine and Compose releases above, running the
  `linux/amd64` image. It lists each configuration tested, with its date,
  and those neither tested nor supported: `linux/arm64`, amd64 emulation,
  Docker Desktop, rootless Docker, user-namespace remapping,
  SELinux-enforcing hosts and Podman. *Portable defaults* there says what
  the defaults of `compose.yaml` make portable, and that they do not extend
  the deployment to other architectures or container runtimes.
- [`run_operator_acceptance.sh`](run_operator_acceptance.sh), new, is stage
  7 of `run_ci_suite.sh` and tests the operator's workflow of
  [`DEPLOYMENT.md`](DEPLOYMENT.md) against Docker, the commands run as
  documented in a project of its own: a copy of `compose.yaml`,
  `create_secrets.sh` and `set_passwords.sh`, and nothing else of the
  checkout, configured by an `.env` file — project
  name, image, a relative secrets directory, port and log level — on the
  ordinary network of `compose.yaml`; a synthetic roster of mode `0600`
  imported on standard input, with the records, a second import, a refused
  bind mount and the unchanged file checked; an attribution rule, the bundled
  payloads, the refresh, the read service and the export; the three
  passwords changed with `set_passwords.sh`, the files overwritten in place
  and then replaced by rename while the running database container still
  sees the old ones, checked with new connections that the new passwords
  open and the old ones do not, a healthy read service and the records kept,
  and its refusal of a wrong secrets directory before any change; a dump of
  the database restored into a new stack after `docker compose down
  --volumes`, with the same counts, publication and records afterwards; and
  the role model of `DEPLOYMENT.md`, with its commands, on the restored
  database.
  No password of any generation and no roster entry may reach a log or
  output. It contacts no metadata provider, by its choice of commands. Its
  isolation, masking and cleanup are those of the acceptance test, whose
  helpers both now share in
  [`DB/lib/compose_acceptance.sh`](DB/lib/compose_acceptance.sh).
- [`DEPLOYMENT.md`](DEPLOYMENT.md), *Changing a password*, gives both
  sequences in full and ends each with `docker compose up --wait`, which
  returns once the restarted read service has logged in with its new
  password: until that restart its existing connections keep `/readyz` at
  200, so readiness alone does not show that a change worked.
- Tests: 108 offline tests of the runner's lifecycle, prerequisites,
  identity checks and check of the image's packages against a stub `docker`
  and `git`, the last running the check on synthetic images
  ([`DB/tests/test_stack_acceptance_script.py`](DB/tests/test_stack_acceptance_script.py)),
  one of the test-only Compose addition, the sixth and seventh stages in the
  tests of the suite and of its offline isolation, 64 of the version
  comparison, of the Engine requirement and the Compose minimum the
  documents state, and of how both acceptance tests report their check of
  the port bindings
  ([`DB/tests/test_docker_versions.py`](DB/tests/test_docker_versions.py)),
  and 28 offline tests of the operator test's lifecycle and isolation,
  one of which binds the documented backup and restore to the commands the
  test runs
  ([`DB/tests/test_operator_acceptance_script.py`](DB/tests/test_operator_acceptance_script.py)).
- [`DEPLOYMENT.md`](DEPLOYMENT.md) gains *Checking a deployment*, *Backup
  and restore* and a step-by-step *Publishing an image*: a clean release
  commit, the acceptance test on the built image, a check that the version
  is not published yet,
  the push of the version tag — for PubDex's own releases to
  `ghcr.io/ccu-ukn/pubdex`, once the release commit is published — the
  recorded digest, and the acceptance test of the pulled image without
  credentials. [`README.md`](README.md) names the two ways to run PubDex —
  the deployment, which is canonical, and the development setup — in one
  line each, with their guides.

### Build and dependency changes of 2026-10-30

Three changes update the dependency installation and the two deployment
images.

- **psycopg's C implementation against Debian's libpq.** psycopg-binary
  is replaced by psycopg-c 3.3.6
  ([`DB/requirements.in`](DB/requirements.in), both locks). Its only file is
  its source archive: the image builds it, by the archive's hash, in a stage
  with a compiler and Debian's libpq headers, with the build tools of the
  new [`DB/requirements-build.txt`](DB/requirements-build.txt), without
  network, and installs the wheel by the hash recorded at its build, with
  Debian 13's `libpq5` 17.11 and no compiler; `PSYCOPG_IMPL=c` keeps psycopg
  from falling back to another implementation. The SCRAM requirement is
  kept and was checked again against a real server. Development setups
  build psycopg-c against the host's libpq, or take the pure-Python
  implementation with `--psycopg python`, and refuse a libpq older than 16,
  which lacks `require_auth`.
- **Downloads apart from installations, and pip without network.** The new
  [`DB/fetch_verified.py`](DB/fetch_verified.py) is the one downloader:
  exactly one file per pin of the locks, over HTTPS verified with the
  system's store, redirects to `https:` only, timeouts, staggered IPv6 and
  IPv4 connection attempts, size limits and a deadline per file that covers
  the host name's lookup, the connection, TLS, redirects, the response's
  headers and its body — a connection is shut down when it passes, so a
  body that trickles in cannot hold a read open — each file kept only when
  its SHA-256 is one its lock lists. [`DB/install_pip.py`](DB/install_pip.py)
  now installs pip from that directory instead of downloading it, and the
  new [`DB/install_locked.py`](DB/install_locked.py) installs a lock from it:
  both check the directory against the locks, run pip with `--no-index`
  and without pip's configuration, and run it without network — in the
  image build under `RUN --network=none`, requiring that, and on hosts in a
  new network namespace where the system permits one, saying so where it
  does not. The new [`setup_venv.sh`](setup_venv.sh) runs the same steps for
  both CI workflows and every documented setup, which now give that one
  command; [`DB/locked_files.py`](DB/locked_files.py) holds what the three
  scripts share. The application image still holds no pip.
- **A PostgreSQL image without SQL/XML.** PubDex uses no SQL/XML. The new
  [`postgres.Dockerfile`](postgres.Dockerfile) builds PostgreSQL 16.15 from
  postgresql.org's source archive, by the SHA-256 that PGDG's signed source
  index confirms, without XML, LLVM, GSSAPI, LDAP and PAM, on Debian 13 by
  digest, with the official image's entrypoint scripts, user, data
  directory, socket and locale, and the notices of the licences involved —
  the entrypoint scripts' MIT licence kept in
  [`third_party/docker-library-postgres/LICENSE`](third_party/docker-library-postgres/LICENSE)
  as upstream has it at a fixed commit and checked by hash, so that the
  build downloads nothing but PostgreSQL's source and needs no particular
  forge; every XML function fails with "unsupported XML feature". `compose.yaml`
  names it, `ghcr.io/ccu-ukn/pubdex-postgres:0.1.0` by default or
  `PEOPLE_PUBS_POSTGRES_IMAGE`; [`build_image.sh`](build_image.sh) builds it
  with the application image (`--postgres-tag`); both acceptance tests take
  it with `--postgres-image`, check its labels like the application image's,
  and the stack test checks that SQL/XML is unavailable. Its data directory
  is compatible with the official image's, tested with synthetic data;
  [`DEPLOYMENT.md`](DEPLOYMENT.md), *The PostgreSQL image*, describes
  the initial release policy and how to prepare a future version.

Tests: [`DB/tests/test_fetch_verified.py`](DB/tests/test_fetch_verified.py)
and [`DB/tests/test_install_locked.py`](DB/tests/test_install_locked.py) are
new; the tests of the pip bootstrap, the container files, the build script,
the acceptance tests and the setups are rewritten for the new steps. They
cover corrupted, missing, extra and linked files, unsafe redirects, failed
downloads and oversized input, peers that stall a lookup, a TLS handshake,
response headers or a body past the deadline, against real local sockets,
installations without network or refused without it, the C build from
paths given relative to the caller's directory, and pip configuration aimed
at the inputs or the destination.

### Verification

The final source commit `57a72dc9f27766007b51c3d16ac7aa8c6ff3798b`
passed the clean-clone seven-stage suite locally on 2026-10-30 in 374
seconds and in public CI on 2026-11-02 in 552 seconds. Each run passed
1407 offline tests with 125 expected skips, 122 integration tests and 27
checks after schema patch reapplication, none skipped. Both deployment
and operator acceptance passed. GitHub's default C setup passed; its
installers reported no network namespace isolation, using `--no-index`
without a network sandbox on that runner.

The separately retained release image pair passed release-mode deployment
and operator acceptance before publication. On 2026-11-03 both public
version tags and manifest/config digests were checked anonymously, and all
23 compressed layers were downloaded and hash-verified. Anonymous
`--release --pull` acceptance then passed on the published digests, using
the application's packaged Compose file, and confirmed cleanup.

- Application manifest: `sha256:24d570015b20f222c767a8bb89dd18d792abbd25cc730137f7b8bc285df6a325`.
- PostgreSQL manifest: `sha256:33988fb179e2065e653ad7813cecf812c21f4381537b8b8db546ad37236b780a`.

Both images retain version 0.1.0 and the exact source revision above. These
runs precede this evidence update; they do not claim a pipeline on the
later documentation commit. [`TASK_2A.md`](TASK_2A.md) records the exact
commands, image identities, environments and limits.

Earlier full verification was on an uncommitted working tree of
`a0219be7bab86ce3fc53262f52089ac453589de8` on 2026-10-30. The seven-stage
suite passed in 375 seconds: 1407 offline tests passed, 125 expected skips,
122 integration tests and then 27 on the new PostgreSQL 16.15 image. The
secrets and transform-version range checks passed, and the suite left no
Docker resources behind. Stage 6 checked both images and SQL/XML absence;
stage 7 checked the documented operator workflow.

On a separate no-cache build, application image
`sha256:04d9bce2f3df304ec6104fe56dcc2ea4704ce52e3f0b48eaee7ff86ac282db3d`
and PostgreSQL image
`sha256:823472c0b26e1eb07587249691bb43828d0e25a844ac9246be5d7f11a42fd210`
passed deployment and operator acceptance. A real-server check verified
the connection settings. The images carry the source revision with
`-dirty`; release acceptance refused them as required. These runs precede
later documentation edits and are not final-commit or published-image
evidence. [`TASK_2A.md`](TASK_2A.md) records the exact tested identities.

The default C setup passed in a disposable Ubuntu 24.04 container with
CPython 3.11.17 and libpq 16.15. It reported that network isolation was
unavailable there. The pure-Python setup passed on the host with isolation.
These earlier setup checks predate the successful public CI run above;
the current implementation has not run on the private Forgejo host.

Earlier, a fresh clone of provisional local commit
`a0219be7bab86ce3fc53262f52089ac453589de8` passed the seven-stage suite
on 2026-10-29 in 293 seconds, with 1328 offline tests passed, 125 expected
skips, 122 integration tests and then 27. Its image passed release and
operator acceptance on two builds. The same commit passed three private
Forgejo workflow scenarios: first push, pull request and ordinary push.
Those results predate the two-image build and dependency changes above.

The earlier local and private Forgejo results do not establish public
publication or external acceptance; the dated public evidence is the
separate record above.

## Forge-portable test suite — verified locally 2026-10-05, public CI passed 2026-10-07

Task 1b: the complete test suite as one forge-portable command that runs
locally and in public CI from a clean checkout. Implemented and verified
locally on 2026-10-05 with the commands under *Verification*. The public-CI
result — the workflow passing from a clean checkout of the pushed commit —
followed on 2026-10-07 for the published commit `6fa0318`, also under
*Verification*. External acceptance is not asserted by this entry.
Requirement-by-requirement record: [`TASK_1B.md`](TASK_1B.md).

### One command, locally and in CI

- [`run_ci_suite.sh`](run_ci_suite.sh) runs the complete suite in five
  numbered stages and stops at the first failure with that stage's exit
  status: the offline check suite (compilation, Markdown consistency, the unit
  tests and the offline source fixtures, plus the transform-version guard over
  a commit range), the check for committed secrets and local configuration,
  schema bootstrap from an empty PostgreSQL database with re-application of the
  runtime patch, the task-1a clean-clone demonstration, and the complete
  integration suite on a disposable PostgreSQL. It runs from any working
  directory, passes `--base`/`--head` to the transform-version guard and to
  the secrets check, which then also reads the commits of that range, names
  the missing piece when the Python environment, Git or Docker is not
  available, and starts by printing the versions it runs with that the
  repository does not pin.
- [`.github/workflows/db-tests.yml`](.github/workflows/db-tests.yml) is only an
  adapter around it: on pull requests and pushes to `main` it checks out the
  full history with read-only permissions and no persisted credentials,
  installs `DB/requirements-dev.txt` into `.venv` and calls `./run_ci_suite.sh`
  with the event's commit range, under a 30-minute limit. No test, rule or
  database step lives in the workflow and it needs no repository secret. Its
  actions are pinned by commit, and its runner image (`ubuntu-24.04`),
  Python (3.11.17) and pip (26.2.1) versions are fixed.
  [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) states the contract a
  runner on another forge has to meet — Bash, Python 3.11, a Git checkout
  with its history, and a Docker Engine on the same machine that sees the
  checkout and whose published ports answer on 127.0.0.1 — and which
  arrangements meet it; gives a GitLab CI example, checked offline and run
  locally but not as a GitLab pipeline; and says which inputs are fixed and
  which come from the host, with commands that fix the pip, package and
  PostgreSQL image versions of the local record and the host versions a
  reproduction has to supply itself.
- [`.forgejo/workflows/db-tests.yml`](.forgejo/workflows/db-tests.yml) is the
  same adapter for Forgejo Actions: on every push and pull request it runs
  the same two steps on a self-hosted Forgejo Runner with the host label
  `pubdex-ci-host`, with the runner's `python3.11` and the same pinned pip,
  and fetches the checkout action, at the commit the GitHub workflow pins,
  from Forgejo's mirror. Because Forgejo tests a pull request's head commit
  rather than a merge commit, a pull request's range starts at its merge
  base. Its steps are tested offline against synthetic Forgejo event
  contexts, and on 2026-10-06 the commit `ee9dee3` passed it on a private,
  self-hosted Forgejo instance, with its runner on the same workstation, for
  a first push, a pull request and a push
  ([`TASK_1B.md`](TASK_1B.md), *Self-hosted Forgejo evidence*); that
  instance is not publicly accessible.
- [`run_checks.sh`](run_checks.sh) stays the fast offline command and the
  pre-commit hook; nothing on the commit path needs Docker. Its tests —
  [`DB/run_tests.sh`](DB/run_tests.sh), standalone and as stage 1 — run in a
  cleared environment: no test DSN, `PEOPLE_DB_DSN`, libpq or `POSTGRES_*`
  setting, `PYTEST_ADDOPTS` value or other variable the caller exports reaches
  them, and the new `PEOPLE_PUBS_SKIP_DOTENV=1` opt-out keeps
  [`people_pubs.config`](DB/people_pubs/config.py) from filling settings in
  from `DB/.env`. The configuration module is a transform path, so
  `people_pubs.__version__` moves to 1.5.1; no importer transform changed.

### Guards over every test run

- A network guard ([`DB/tests/network_guard.py`](DB/tests/network_guard.py),
  installed by [`DB/tests/conftest.py`](DB/tests/conftest.py)) enforces the
  offline guarantee inside the Python processes of a test run: offline tests
  may open no network connection, loopback included, look up no host name
  but the machine's own and reach no database, and integration tests may
  reach their disposable database and nothing else. It wraps socket connects
  before host names are resolved, adds an audit hook that code using the
  lower-level socket class cannot avoid, and checks connections through
  psycopg's public connect methods, which libpq opens out of sight of
  Python's socket module. Python processes started with the test environment
  load the same guard, applying the psycopg check when they import psycopg;
  a child whose guard cannot be set up stops instead of running unguarded. A
  test during which anything was refused fails even when the code under test,
  or a child process, caught the exception. It is not operating-system
  network isolation: processes started with a cleared environment or with
  Python's isolation flags, and programs that are not Python, are outside it.
- [`DB/tests/expected_skips.py`](DB/tests/expected_skips.py) names every skip
  a run may contain by test, reason and condition — the integration tests
  without a database, the three refresh-job guardrails that have no discovery
  job to check, the unreadable-file case when running as root, the two
  external-timeout cases without `timeout(1)` — and any other skip, an
  expected failure, or a test module skipped while it is collected fails the
  run. With a database configured, as in stage 5, a skipped integration test
  fails it too.

### Integration testing on a disposable PostgreSQL

- [`DB/run_disposable_integration.sh`](DB/run_disposable_integration.sh)
  replaces the pasteable recipe of the initial baseline with a tracked runner:
  a uniquely named container and volume carrying the run's ownership label; a
  cleanup registered before anything is created that removes only what carries
  that label, after success, failure, the readiness timeout, SIGINT, SIGTERM or
  SIGHUP; a loopback port that Docker picks; `DB/init/` mounted read-only; a
  bounded, configurable readiness wait that ends at once when the container
  exits or an init script fails; a per-run password kept off every command
  line and out of files, and masked in the test output and in every container
  diagnostic; and a test environment, cleared of every exported variable and
  function by the shell itself, in which the test DSNs, the application DSN
  and the libpq settings all name the disposable database and `DB/.env` stays
  unread. It runs `DB/run_integration_tests.sh`, re-applies the
  runtime patch and reruns the publication-view and role tests,
  and fails when a test was skipped or none ran.
- The migration smoke test and the task-1a demonstration ignore further
  signals while cleaning up, so a second interrupt can no longer leave their
  container or volume behind. Like the runner, they generate a random
  superuser password per run — the migration smoke's no longer derives from
  the printed run id — hand it to Docker by variable name instead of on the
  command line, and mask it in any container log tail they print. The
  demonstration's Python steps no longer take a `--dsn` argument: they read
  `PEOPLE_DB_DSN` from an environment the shell clears and exports itself,
  with the libpq settings bound to the same disposable database and
  `PEOPLE_PUBS_SKIP_DOTENV=1`, so no command line of the demonstration carries
  the connection string or the password
  ([`DB/run_migration_smoke.sh`](DB/run_migration_smoke.sh),
  [`DB/run_task_1a_demo.sh`](DB/run_task_1a_demo.sh)).
- All three confirm their cleanup instead of assuming it: they remove what
  carries their run's label, check that nothing with it is left and that
  their temporary directory is gone, and otherwise print the commands that
  find the leftovers and fail — with status 1 after an otherwise successful
  run, and with an earlier failure's or a signal's own status otherwise. A
  refused or ineffective removal and an unanswering daemon no longer pass
  unnoticed; only SIGKILL skips the cleanup.
- The three scripts take this lifecycle from one sourced file,
  [`DB/lib/disposable_postgres.sh`](DB/lib/disposable_postgres.sh), instead
  of three copies. Their readiness wait ends at once when the container
  exits, an init script fails, or the container initialised without running
  `DB/init/` — what a Docker daemon that cannot see the checkout produces,
  which the run now names instead of timing out. `DISPOSABLE_PG_IMAGE` sets
  the PostgreSQL image of all three, each script's own variable still taking
  precedence, and each reports the image digest and server version it used.
- New rerun tests drive `datacite_backfill`, `dblp_backfill` and
  `semantic_scholar_backfill` through their real database-writing paths on the
  disposable database, with provider access answered from the golden
  fixtures ([`DB/tests/integration/test_backfill_rerun_integration.py`](DB/tests/integration/test_backfill_rerun_integration.py)):
  regression tests pin the first import, the kept provenance of other
  sources and identical identities, counts and values after a forced rerun;
  separate characterization tests pin, without approving it, that these
  importers replace values that sources ranked above them in the trust order
  supplied, unlike OpenAlex. Whether they should is an open decision,
  recorded in [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) and
  [`DB/SCRIPTS.md`](DB/SCRIPTS.md); importer behaviour is unchanged.
- New role tests
  ([`DB/tests/integration/test_application_roles_integration.py`](DB/tests/integration/test_application_roles_integration.py))
  run the writes of routine ingestion as `app_writer`, the reads of
  reporting as `app_readonly` and the maintenance of personal data as
  `maintenance`, check that each role is refused what the permission model
  withholds, and sweep the grants of every table and view; the runner
  repeats them after re-applying the runtime patch.
- Grant correction: the login roles had no `USAGE` on schema `public`, which
  holds the `citext`, `pgcrypto`, `unaccent` and `pg_trgm` extensions. As
  `app_writer`, the application's calls of the `pii` email functions failed
  (`type "citext" does not exist`), and for all three roles `citext` columns
  such as `app.people.display_name` and `pii.people_pii.primary_email`
  compared case-sensitively, so a lookup or correction by an address in
  another case matched nothing. [`DB/init/000_init.sql`](DB/init/000_init.sql)
  now grants that `USAGE` — not `CREATE` — to `app_readonly`, `app_writer`
  and `maintenance`. The runtime patch grants it to the two application roles
  and to `maintenance` where that role exists, without creating it, so
  existing databases get the grant when the patch is re-applied; the
  migration smoke test re-applies the patch once more after removing the
  `maintenance` role from its own cluster.

### Committed secrets and local configuration

- [`check_secrets_and_local_config.py`](check_secrets_and_local_config.py)
  (standard library only) checks the publishable tree — the tracked files plus
  untracked, nonignored ones — for environment files other than example
  templates, private keys and credential files, recognisable token formats,
  passwords in URLs and connection strings, secret settings with literal
  values, database dumps and backups, machine-local state, home-directory
  paths and private network addresses. Findings name the rule, file and line
  but never the matched text. Python is read as a syntax tree, so literal
  credentials in keyword arguments, dictionary entries, parameter defaults,
  literal-only f-strings, one-line compound statements and the fallbacks of
  environment or mapping lookups are found as well as constants, while values
  obtained elsewhere (`api_key=args.api_key`, a lookup without a literal
  fallback) are not. A literal fallback written into a shell reference, as in
  `${API_KEY:-...}`, is judged like a direct value. Documented placeholders
  and values that refer to their content as a whole are accepted; a value
  that merely contains a `$` or starts with a bracket is not, and a value
  composed from references at run time, such as `smoke-$RUN_ID`, counts only
  in shell code, where it is actually expanded. It is a pattern check with
  documented limits, not proof that no secret is present.
- With `--base`/`--head` it also checks the commits a push or pull request
  adds, because a credential committed and removed again stays in the
  published history: every file each commit of the range adds or changes,
  and what a merge changes itself, with the same rules, reported as
  `commit:path:line` without the matched text. Commits the base contains are
  not read again. An empty or all-zero base, or one the clone does not
  contain, means every commit reachable from the head; a shallow clone or an
  unreadable object ends the check with status 2, never with a pass. Two
  synthetic test values in the published task-1a commit, which cannot change
  any more, are accepted by commit, path, line and rule.

### Tests for the tooling itself

Offline tests with stub `docker`, `git`, `rm` and stage commands pin the
suite's stage order, failure propagation, range forwarding, diagnostics and
signal handling, the GitHub and Forgejo workflows as pinned adapters whose
shell steps hand the suite the range of a push, a first push and a pull
request on each forge, and the GitLab CI example's commands under the
variables of a push, a first push and a merge request
([`DB/tests/test_ci_suite_script.py`](DB/tests/test_ci_suite_script.py));
the disposable runner's whole lifecycle, signals, timeouts and failing
cleanups included, and that no `docker`, test or `env` command line carries
its password ([`DB/tests/test_disposable_integration_script.py`](DB/tests/test_disposable_integration_script.py));
the checker's rules, placeholders, literal fallbacks and false-positive
boundaries, and its commit check against a synthetic history — a
credential committed and removed again, clean ranges, merges, and every way
the check can be incomplete
([`DB/tests/test_secrets_and_local_config.py`](DB/tests/test_secrets_and_local_config.py));
the password handling, log masking, second-interrupt cleanup and failing
cleanups of the migration smoke and the demonstration, and a whole
demonstration run whose recorded Python steps get the database only through
their environment ([`DB/tests/test_migration_smoke_script.py`](DB/tests/test_migration_smoke_script.py));
the isolation of the offline tests, run through the real `run_tests.sh`,
`run_checks.sh` and `run_ci_suite.sh` with a polluted environment, together
with the `DB/.env` opt-out itself
([`DB/tests/test_offline_isolation.py`](DB/tests/test_offline_isolation.py),
[`DB/tests/test_config_dotenv.py`](DB/tests/test_config_dotenv.py)); and the
network guard and the skip policy
([`DB/tests/test_network_guard.py`](DB/tests/test_network_guard.py),
[`DB/tests/test_expected_skips.py`](DB/tests/test_expected_skips.py)). None of
them needs Docker; the network guard is proven on the disposable database as
well ([`DB/tests/integration/test_network_guard_integration.py`](DB/tests/integration/test_network_guard_integration.py)).

### Verification

`./run_ci_suite.sh` from the repository root runs everything above. Locally,
on 2026-10-05 on Linux with real Docker and Python 3.11.5, all five stages
passed: 649 passed and 110 skipped in the offline suite — the 107
integration tests, which run in stage 5, and three refresh-job guardrails
parametrized over discovery jobs, of which the shipped job list has none,
all of them skips the policy names — a clean secrets and
local-configuration check over 171 files, the migration smoke, its
re-application without the `maintenance` role included, and the task-1a
demonstration, then 107 integration tests and 25 publication-view and role
tests after the runtime patch was re-applied, none skipped. The GitLab CI
example's commands, run on the same machine by hand with the variables of a
branch's first push, passed the same five stages, the secrets check reading
every commit reachable from the tested one; that is not a GitLab pipeline
run. Nothing carrying the suites' labels was left in Docker afterwards. In
public CI, the `DB tests` workflow passed on 2026-10-07 for the published
commit `6fa0318`, on GitHub's `ubuntu-24.04` runner with the workflow's
Python 3.11.17 — the suite's first run on GitHub's runner;
[`TASK_1B.md`](TASK_1B.md), *Public-CI evidence*, records the run. Helm tests
belong to task 2b and are not part of this result.

## Initial public baseline — published 2026-09-25

Task 1a: public-safe repository, licensing and clean-clone baseline.
Initial candidate prepared 2026-09-10. The amended candidate was verified on
2026-09-25 using the commands under *Verification*, then published at
<https://github.com/CCU-UKN/pubdex> with evidence tag `public-baseline-1a`.
Anonymous repository and tag access establish public availability; this entry
does not assert external acceptance. Requirement-by-requirement record:
[`TASK_1A.md`](TASK_1A.md).

### Repository and licensing

- A curated, public-safe copy of the PostgreSQL schema
  ([`DB/init/`](DB/init/)), the `people_pubs` ingestion package
  ([`DB/people_pubs/`](DB/people_pubs/)), its tests, the operator scripts and
  the documentation, prepared without the private repository's history. The
  ingestion package integrates ORCID, Crossref, OpenAlex, DataCite, DBLP and
  Semantic Scholar, plus manual CSV/DOI import.
- MIT for code, SQL schemas and migrations ([`LICENSE`](LICENSE)); CC BY 4.0
  for documentation and synthetic examples
  ([`LICENSE-CC-BY-4.0`](LICENSE-CC-BY-4.0)).
- A secret-free example configuration ([`.env.example`](.env.example)) as the
  single consolidated template for a local `DB/.env`.
- Synthetic fixtures and a demo roster of fictional people
  ([`DB/tests/fixtures/golden/`](DB/tests/fixtures/golden/)), so both the
  offline tests and the walkthrough run without any institutional data.
- The project description ([`README.md`](README.md)), this progress record, the
  acceptance record ([`TASK_1A.md`](TASK_1A.md)) and the funding
  acknowledgement.

### Institutional attribution as configuration

- Which publications count as an organisation's own output is installation
  configuration, held in `app.institution_attribution_rules`
  ([`DB/init/010_ingestion_runtime_schema.sql`](DB/init/010_ingestion_runtime_schema.sql))
  and managed by
  [`people_pubs.sync.attribution_rules`](DB/people_pubs/sync/attribution_rules.py)
  — no organisation data is embedded in the code. The rule set ships empty, so
  an unconfigured installation attributes nothing to anyone.
- Rule changes and concurrent ingestion coordinate through database-level
  locking, so an ingest overlapping a rule change cannot leave a cached
  attribution flag stale
  ([`DB/tests/integration/test_attribution_concurrency_integration.py`](DB/tests/integration/test_attribution_concurrency_integration.py),
  [`DB/tests/integration/test_institution_attribution_integration.py`](DB/tests/integration/test_institution_attribution_integration.py)).
- An author linked to a person row is no longer reported as one of the
  organisation's own authors just because the link exists.
  `biblio.authorships.is_internal_at_ingest` is documented as a snapshot of
  the linked person's `app.people.person_kind`, but ingestion set it from "a
  person was matched at all", so external co-authors appeared in
  `internal_authors`. Ingestion, relinking, curator attachment and the manual
  overlay now all follow `person_kind`; external co-authors stay linked and
  visible, and re-applying the runtime patch repairs rows written earlier
  ([`DB/people_pubs/db/authorships.py`](DB/people_pubs/db/authorships.py),
  [`DB/init/010_ingestion_runtime_schema.sql`](DB/init/010_ingestion_runtime_schema.sql),
  [`DB/tests/integration/test_internal_author_classification_integration.py`](DB/tests/integration/test_internal_author_classification_integration.py)).
- Primary-email selection no longer prefers one organisation's mail domain.
  The rule is domain-neutral in both the Python helper and the SQL functions:
  keep an explicitly set primary, honour an explicitly supplied one, otherwise
  take the first address in stable input order — which also fixes a fallback
  that had quietly been lexical rather than input order
  ([`DB/people_pubs/db/people.py`](DB/people_pubs/db/people.py),
  [`DB/init/010_ingestion_runtime_schema.sql`](DB/init/010_ingestion_runtime_schema.sql),
  [`DB/tests/test_primary_email_policy.py`](DB/tests/test_primary_email_policy.py),
  [`DB/tests/integration/test_primary_email_policy_integration.py`](DB/tests/integration/test_primary_email_policy_integration.py)).

### A reproducible clean-clone demonstration

- [`DB/run_task_1a_demo.sh`](DB/run_task_1a_demo.sh) performs the whole
  acceptance sequence in one command against its own disposable PostgreSQL
  container: bootstrap, synthetic roster, ORCID payload, Crossref enrichment of
  the same work, a stored-record query, a canonical-list query and a verified
  export. It makes no metadata-provider request and uses no existing database:
  its input and its expected publication data come entirely from repository
  fixtures, so it is reproducible on supported Docker environments once the
  PostgreSQL image and the Python dependencies are available.
- [`people_pubs.sync.fixture_demo`](DB/people_pubs/sync/fixture_demo.py) feeds
  the bundled payloads through the production ingestion path rather than
  inserting prepared rows, so the demonstration exercises the real parsing,
  provenance merge, upsert and authorship code
  ([`DB/tests/test_fixture_demo.py`](DB/tests/test_fixture_demo.py),
  [`DB/tests/integration/test_fixture_demo_integration.py`](DB/tests/integration/test_fixture_demo_integration.py)).
- The expected result is a nonempty export containing DOI
  `10.5555/pubdex-fixture-001` with `crossref+orcid` provenance and
  `Ada Example` as its only internal author — the roster's external co-author
  is linked to the work but is not one of the organisation's authors. The
  script fails if any part of that is missing.

### Fixes found while validating the walkthrough

- The attribution command rejects incompatible option combinations before it
  opens a connection, and `--dry-run` neither commits nor refreshes
  ([`DB/tests/test_attribution_rules_cli.py`](DB/tests/test_attribution_rules_cli.py)).
- The migration smoke test owns exactly the resources it creates: uniquely
  named and labelled container and volume, cleanup registered before creation,
  removal limited to that run's resources, and an abort instead of a takeover
  on a name collision
  ([`DB/run_migration_smoke.sh`](DB/run_migration_smoke.sh),
  [`DB/tests/test_migration_smoke_script.py`](DB/tests/test_migration_smoke_script.py)).
- Both serving snapshots refresh inside one transaction, so readers keep the
  previous consistent pair until the new pair is published, and a failed
  refresh leaves the old one readable
  ([`DB/refresh_publications_canon_web.sql`](DB/refresh_publications_canon_web.sql)).
- The weekly maintenance bundle runs each job as its own invocation, so one
  failing job no longer stops the later ones while the bundle still exits
  non-zero ([`DB/RunAllWeekly.sh`](DB/RunAllWeekly.sh),
  [`DB/tests/test_operator_bundles.py`](DB/tests/test_operator_bundles.py)).

### Usable from a clean clone

[`QUICKSTART.md`](QUICKSTART.md) takes a fresh clone through bootstrapping the
database, importing the synthetic roster, ingesting the bundled ORCID and
Crossref payloads, querying both the stored record and the canonical list, and
producing a CSV export — the same sequence as the one-command demonstration,
performed by hand. A live ORCID and Crossref demonstration against ORCID's
fictional test researcher is included as a clearly labelled optional extra,
since it depends on external services rather than on the repository.
[`QUICKSTART_CURATION.md`](QUICKSTART_CURATION.md) continues into curation,
aliases and the reporting exports.

### Checked before publication

The copy was checked for secrets, private rosters, restricted-source data,
database dumps and machine-local state. Private data, operational identifiers
and institution-specific application configuration were removed or replaced by
placeholders, example values or configuration the operator supplies. Copyright
attribution is a separate matter: [`LICENSE`](LICENSE) contains the stated
copyright attribution, which names an institution. The full list of review
categories is in [`TASK_1A.md`](TASK_1A.md).

### Verification

`./run_checks.sh` from the repository root runs the default offline check
suite. The default invocation runs compilation, the Markdown link and path
check and the offline unit/fixture suite; in pre-commit and CI modes
(`--staged`, or `--base`/`--head`) it additionally runs the transform-version
guard. Three further commands stand
on their own: `(cd DB && ./run_migration_smoke.sh)` proves the schema
bootstraps from an empty database and that the runtime patch re-applies
cleanly, `./DB/run_task_1a_demo.sh` runs the clean-clone acceptance path end to
end, and `(cd DB && PEOPLE_PUBS_INTEGRATION_DSN=... ./run_integration_tests.sh)`
runs the disposable-database integration suite. The same sequence by hand is
[`QUICKSTART.md`](QUICKSTART.md); the requirement-by-requirement record is
[`TASK_1A.md`](TASK_1A.md).

### Next

Task 1b, the complete forge-portable test suite that runs the whole
verification story — including integration testing against a disposable
PostgreSQL instance — from a clean checkout in public CI, is recorded in the
entry above; it passed in public CI on 2026-10-07. The versioned HTTP API is task
3a; this baseline serves reads through the documented PostgreSQL schema
instead, as [`TASK_1A.md`](TASK_1A.md) explains.
