# Testing Strategy

## How to run the checks

Two commands at the repository root cover everything, and both work from any
working directory:

| Command | What it runs | Needs |
|---|---|---|
| [`./run_checks.sh`](../run_checks.sh) | The **default offline check suite**: compilation of `people_pubs` and its tests, the Markdown link and path check, the release-note check of `VERSION`'s section in `CHANGELOG.md` against the tree and the preceding PubDex release in the release record ([`check_release_notes.py`](check_release_notes.py)), the version guard ([`check_version_guard.py`](check_version_guard.py)), which refuses a tree that carries the version of a published PubDex release but ships other files than that release's commit, the offline unit and source-fixture tests, and — when a commit range or the staged index is supplied — the transform-version guard. It is the pre-commit hook and the everyday command. Its tests run in a cleared environment (see *Isolation and cleanup*), so they stay offline and complete whatever the caller exports. | `.venv` with `requirements-dev.txt`, in a Git checkout; no database, no Docker, no network |
| [`./run_ci_suite.sh`](../run_ci_suite.sh) | The **complete verification suite**, the command public CI runs: the eleven stages below, in order, stopping at the first failure with that stage's exit status. `--base <sha> [--head <sha>]` adds the transform-version guard over that commit range and the check of the files its commits add or change; an empty or all-zero base (the first push of a branch) gives the guard no range, as in `run_checks.sh`, and makes the secrets check read every commit reachable from the head. | the same `.venv`, a Git checkout (with the complete history for a range, and with the commit of the PubDex release `VERSION` names once that release is published), the pinned helm, kubectl and kind that [`../setup_cluster_tools.sh`](../setup_cluster_tools.sh) installs, the pinned SBOM tool that `../release.sh setup` pulls, Linux on x86_64 for stages 9-11, and a Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2, with the buildx plugin and the Compose plugin 2.24.0 or later on the same machine — see *Running the suite elsewhere* |

The stages of `./run_ci_suite.sh`:

| Stage | Component command | What it proves |
|---|---|---|
| 1 | `./run_checks.sh`, with the range when one is given | everything the offline suite above proves |
| 2 | [`check_secrets_and_local_config.py`](../check_secrets_and_local_config.py), with the range when one is given | no committed secret or local configuration of a kind its rules recognise in the publishable tree and, given a range, in the files its commits add or change — a pattern check with documented limits, not proof that no secret exists; see *Committed secrets and local configuration* below |
| 3 | [`DB/run_migration_smoke.sh`](run_migration_smoke.sh) | the baseline bootstraps from an empty PostgreSQL database, the schema upgrade applies and records every schema change once, a second schema upgrade changes nothing, and the runtime patch re-applies cleanly — see *The schema upgrade's tests* |
| 4 | [`DB/run_task_1a_demo.sh`](run_task_1a_demo.sh) | the clean-clone acceptance path end to end — bootstrap and schema upgrade, synthetic roster, bundled ORCID and Crossref payloads, queries and a verified export — with no metadata-provider request |
| 5 | [`DB/run_disposable_integration.sh`](run_disposable_integration.sh) | the complete integration suite on its own disposable PostgreSQL, after the schema upgrade, then the publication-view and role tests again after the runtime patch has been re-applied to that database; an explicit synthetic backup through the actual development restore helper, checking values, the schema record, ownership, schema support and role boundaries |
| 6 | [`run_stack_acceptance.sh`](../run_stack_acceptance.sh) | the Compose deployment end to end: the application image built from the checkout, `compose.yaml` started from an empty volume in a project of its own, its schema upgrade and the record it leaves, the synthetic demonstration ingested inside it as `app_writer` and read back over HTTP as `app_readonly`, readiness across a database restart, persistence across recreation, and no password in any output — see *The Compose deployment's acceptance test* |
| 7 | [`run_operator_acceptance.sh`](../run_operator_acceptance.sh) | the operator's workflow on that deployment, as [`../DEPLOYMENT.md`](../DEPLOYMENT.md) gives it: the stack configured by an `.env` file — project name, image, secrets directory, port, log level — on the ordinary network of `compose.yaml`, a private roster imported on standard input, an ingestion of the bundled payloads, the refresh, the read service and the export, both documented ways of changing the roles' passwords, a dump restored into a new stack whose schema upgrade then finds nothing to do, and the documented role model, each checked by the records and connections it leaves — see *The operator's workflow test* |
| 8 | [`run_chart_checks.sh`](../run_chart_checks.sh) | the Helm chart in `charts/pubdex/`, offline with the pinned helm: it lints under `--strict` for the oldest and newest supported Kubernetes release, renders for each values file of the checks and each example, and passes the assertions on the manifests and the refusal of invalid values — see *The Helm chart's offline checks* |
| 9 | [`run_cluster_acceptance.sh`](../run_cluster_acceptance.sh) | the Helm chart on two disposable Kubernetes clusters, one after the other, each a kind cluster of its own on a Docker network of its own. On the newest supported release, Kubernetes 1.37: the images built from the checkout loaded into it and the published images pulled into it by digest, and the chart in both modes under Pod Security "restricted". Evaluation mode: a server-side dry run, the install of the published images with the synthetic demonstration, the publication read over HTTP field by field, `helm test`, an upgrade to the images under test with other values and a rollback, each with persistence, no new initialisation and both role logins checked, a password change, the database scaled away and back for readiness, the NetworkPolicy observed, and the uninstall. External mode against a PostgreSQL server of the run's own with TLS: the setup Job as a hook with a non-superuser admin, the demonstration by the notes' commands, an upgrade, a password change, a routine upgrade with the setup switched off and a rollback, both without the admin role's Secret and shown by the API server to have run no setup, and the uninstall. On the oldest, Kubernetes 1.35: evaluation mode's dry run, install, `helm test`, upgrade, rollback and uninstall as a user whose Role grants exactly the rights `KUBERNETES.md` lists, with the kubelet pulling the published images and an audit log showing no refused request — see *The Helm chart on a disposable cluster* |
| 10 | [`run_release_rehearsal.sh`](../run_release_rehearsal.sh) | the rehearsal of a PubDex release, with nothing published: [`../release.sh`](../release.sh) builds test release A — the checkout as it is, under a version reserved for test releases — with its source archive, chart package, both images, an SBOM of each and the provenance record, and verifies it; a second build gives the same source archive and chart package byte for byte, and SBOMs and a provenance record that differ only in the build's times, image ids and config digests; the chart lints for the oldest and newest supported Kubernetes release and renders the test release's images from its package file; the run holds no registry credential — see *The rehearsal of a PubDex release* |
| 11 | [`run_upgrade_acceptance.sh`](../run_upgrade_acceptance.sh) | the upgrades between PubDex releases on the Compose deployment, with an operator's commands: `release.sh` builds test release A of the checkout and test releases B1 and B2 with the test-only overlay; A is upgraded to B1 — a backward-compatible schema change and a new transform version — and rolled back by swapping the images, then upgraded to B2 — a schema change that is not backward compatible — which A refuses, and rolled back by restoring the backup taken before the upgrade; the published preceding release, and the upgrade floor once it is another, is upgraded to A and rolled back by swapping the images — see *Upgrades between PubDex releases on Compose* |

Public CI ([`.github/workflows/db-tests.yml`](../.github/workflows/db-tests.yml))
checks the repository out with its history, creates `.venv` with
[`../setup_venv.sh`](../setup_venv.sh) — which downloads the locked files
with [`fetch_verified.py`](fetch_verified.py), checking each against its
hash, and installs pip with [`install_pip.py`](install_pip.py) and the
development lock with [`install_locked.py`](install_locked.py) from those
files alone, psycopg's C implementation built against the runner's libpq —
installs the pinned helm, kubectl and kind under `.tools/` with
[`../setup_cluster_tools.sh`](../setup_cluster_tools.sh), pulls the pinned
SBOM tool with `../release.sh setup`, and calls
`./run_ci_suite.sh --base <event base> --head <event
commit>`. No test, rule, database step or assertion lives in the workflow, so
a green local run and a green CI run mean the same thing, and a runner on
another forge reproduces the result with the same four steps, provided it
meets the contract in *Running the suite elsewhere* below. The job publishes
nothing: it rehearses a PubDex release in stage 10 and builds test releases
in stages 9 and 11, none of which leaves the runner; it takes no registry
credential and no repository secret, its token can only read the
repository, and no step of it logs in to a registry, pushes anything or runs
the publish step of `release.sh`, which only the maintainer runs
([`../RELEASING.md`](../RELEASING.md)). The job's limit is 60 minutes. On
the machine of the local records the eleven stages took about 23 minutes;
the first nine, as they stood for task 2b, took about 14 minutes there and
15 to 18 minutes in their three runs on GitHub's hosted runner, where the
images are built without a cache, and the job's other steps about one more
([`../TASK_2B.md`](../TASK_2B.md) records the local runs and two of the
hosted ones). At those ratios the job is estimated at
26 to 32 minutes, and the limit is about twice that, because no run of the
eleven stages on a hosted runner has been measured. Every run starts by
printing the versions the repository does not pin (system, Bash, Python, pip,
psycopg's implementation and libpq, Git, Docker Engine, Docker Compose), each
of stages 3–5 the PostgreSQL image digest and server version it used, stage 6
the application image's id, version, revision and transform version and the
PostgreSQL image's id, version, revision and server, and stage 7 both images'
ids, versions and revisions.
[`.forgejo/workflows/db-tests.yml`](../.forgejo/workflows/db-tests.yml) is the
same adapter for Forgejo Actions, for a self-hosted runner that runs the job
on its host; *Forgejo Actions*, under *Running the suite elsewhere*, says what
it needs and where it differs.

### Isolation and cleanup

- Only the synthetic fixtures under `tests/fixtures/golden/` and disposable
  databases are used. No stage contacts a metadata provider, reads
  `DB/.env`, or touches an existing database. Only stages 6 and 7 use
  Docker Compose, each for a project of its own (the last items below).
- The offline tests run in a cleared environment. `./run_tests.sh` — step 3
  of `run_checks.sh`, and so part of stage 1 — starts pytest with only
  `PATH`, `HOME`, `TMPDIR` and `LANG` kept, `PYTHONPATH=.`, and
  `PEOPLE_PUBS_SKIP_DOTENV=1`, which stops `people_pubs.config` from filling
  settings in from `DB/.env`. No test DSN, `PEOPLE_DB_DSN`, libpq `PG*` or
  `POSTGRES_*` setting, `PYTEST_ADDOPTS` or `PYTEST_PLUGINS` value the caller
  exports can connect the tests to a database or narrow the run, so the
  integration tests always skip there. Arguments given to `./run_tests.sh`
  itself still reach pytest.
- Stages 3, 4 and 5 each create one PostgreSQL container and one data volume
  with a unique per-run name and an ownership label
  (`pubdex.migration-smoke.run`, `pubdex.task-1a-demo.run` and
  `pubdex.integration.run`, each set to that run's id). A name collision
  aborts the stage instead of taking the existing resource over. All three
  get this lifecycle — names, password, start, readiness wait, masking and
  cleanup — from one sourced file,
  [`lib/disposable_postgres.sh`](lib/disposable_postgres.sh), so a fix to it
  reaches all three at once.
- The readiness wait is bounded (120 seconds by default) and ends at once
  when the container exits, an init script fails, or the container finished
  initialising without running `DB/init/` at all — what happens when the
  Docker daemon cannot see the checkout's directory; the stage then names
  that requirement instead of timing out.
- Each stage registers its cleanup before it creates anything. The cleanup
  runs on every ordinary exit — success, a failure, the readiness timeout —
  and after SIGINT, SIGTERM or SIGHUP, and ignores further signals while it
  works, so a second Ctrl-C cannot cut it short. It removes only resources
  carrying that run's label (in stages 4 and 5 also the run's private
  temporary directory) and then confirms that nothing is left.
- Cleanup can still fail: Docker can refuse a removal, report success without
  removing anything, or stop answering, and a temporary directory can resist
  removal. The stage then prints the commands that find the leftovers —
  `docker ps -a --filter label=<label>=<run id>` and `docker volume ls
  --filter label=<label>=<run id>` — and fails: with status 1 when the run had
  otherwise succeeded, and with its own status when a failure or a signal had
  already ended it (130, 143 or 129). Only SIGKILL skips the cleanup outright
  — Forgejo Runner ends a job it cancels, or one that reaches its time limit,
  that way (*Forgejo Actions*); the labels `pubdex.migration-smoke.run`,
  `pubdex.task-1a-demo.run` and `pubdex.integration.run` find whatever it
  left.
- Network access is guarded during every pytest run, in the pytest process
  and in the Python processes it starts with its environment: offline tests
  may not use the network at all, and integration tests may reach only their
  disposable database. This is a guard inside Python, not operating-system
  network isolation, and its boundaries are listed under *Network access
  during tests*.
- A skipped test that `tests/expected_skips.py` does not name fails the run,
  and so does a test module skipped while it is collected (see *Expected
  skips*).
- Stages 4 and 5 publish PostgreSQL only on a loopback port that Docker picks;
  stage 3 publishes no port at all. In all three, the superuser password is
  random per run and independent of the printed run id, reaches Docker by
  variable name through the environment, is never printed or written to a
  file, and goes with the container. Every container log tail or error output
  they print is filtered through the same mask, so a log line that contained
  the password shows `<password>` instead; stage 5 filters the test output as
  well.
- The Python steps of stage 4 and the test command of stage 5 get the
  database only through their environment: they take no `--dsn` argument and
  are started by a shell subshell that removes every exported variable and
  function with builtins, exports only the isolated settings itself and then
  `exec`s the command. In that environment the connection string,
  `PEOPLE_DB_DSN` (and in stage 5 the test DSNs) and the libpq `PG*`
  settings all name the disposable database, and `PEOPLE_PUBS_SKIP_DOTENV=1`
  is set. So neither the connection string nor the password appears on any
  command line — not Docker's, not the command's, and not a launcher's, as
  it would in `env -i NAME=value ...`. An environment entry whose name is not
  a valid shell identifier cannot be removed by the shell and passes through;
  no setting any of these programs reads has such a name.
- Stage 6 applies the same lifecycle to a Compose project. Its project is
  `pubdex-acceptance-<run id>`, and every container, network and volume of it
  carries the label `pubdex.stack-acceptance.run=<run id>`; a project, label
  or image tag that exists already stops it before anything is created, and
  is left alone. It registers its cleanup first; afterwards, also after a
  failure or a signal, it runs `docker compose down --volumes`, removes
  whatever still carries its label or project name, the image tag it built
  and its private temporary directory with the secret files, then confirms
  that none of them is left, with the same statuses as above when it cannot.
  It keeps the images it pulled or was given, the PostgreSQL image and the
  build cache, and prunes nothing. Every Compose command it runs names the
  project, the project directory, both Compose files and its own
  environment file, and runs with every exported variable removed except
  `PATH`, `HOME` and Docker's daemon selection (`DOCKER_HOST`,
  `DOCKER_CONTEXT`, `DOCKER_CONFIG`, `DOCKER_CERT_PATH`,
  `DOCKER_TLS_VERIFY`), so no `COMPOSE_*` or `PEOPLE_PUBS_*` setting,
  `.env` file or `compose.override.yaml` of the caller can redirect it. Its
  passwords are random, created by `create_secrets.sh` for the run alone, and
  reach the containers as Compose file secrets only; whatever it prints from
  Docker or the containers is masked.
- Stage 7 applies that lifecycle to a project configured as an operator
  configures one. Its project is `pubdex-operator-<run id>`, named by the
  `.env` file of a directory of the same name in its private temporary
  directory, which holds a copy of the checkout's `compose.yaml`,
  `create_secrets.sh` and `init/`; its secret files and synthetic roster lie
  beside that directory. A project of that name that exists already stops it
  before anything is created. Its Compose commands run in that directory with
  no option naming the project, files or settings, and with every exported
  variable removed except `PATH`, `HOME` and Docker's daemon selection, so
  the synthetic `.env` file is all that configures them: no `COMPOSE_*` or
  `PEOPLE_PUBS_*` setting of the caller's, and neither the checkout's own
  `.env`, `compose.override.yaml` nor `secrets/`, reaches them. Its cleanup
  runs `docker compose down --volumes` there, removes whatever still carries
  the project's name, the image tag it built and its temporary directory,
  and confirms it as stage 6 does. Its later passwords are made by the run
  and written to the files with shell builtins; a connection check receives
  the passwords it tries on its standard input, never on a command line.
- Stage 9 applies that lifecycle to two kind clusters, one after the other.
  They are `pubdex-cluster-<run id>` and, for the floor,
  `pubdex-floor-<run id>`, each on the Docker network `<cluster>-net`, which
  it creates with the label `pubdex.cluster-acceptance.run=<run id>`; a cluster,
  network or image tag of those names that exists already stops it before
  anything is created. Its kubeconfig, `HOME`, kubectl's cache, helm's
  directories and the secret files lie in its private temporary directory,
  and every helm and kubectl call runs with a cleared environment, names
  that kubeconfig and the context `kind-<cluster>`, and starts only after
  the kubeconfig was found to name that context alone, at the loopback port
  Docker publishes for the run's control-plane container. Its cleanup
  deletes the cluster, then whatever still carries the cluster's label, the
  node's volumes, the network, the image tags it created and its temporary
  directory, and confirms it as stage 6 does. A network named `kind` and
  other clusters are never touched.
- Stage 10 builds test release A twice into a temporary directory of its
  own, under a version of its own, which labels its images, and runs every
  Docker command with a client configuration of its own that holds no
  registry credential. Its cleanup removes the containers and images with
  that label, both local tags, the SBOM tool's containers that mount its
  directory, and the directory, and confirms it as stage 6 does (*The
  rehearsal of a PubDex release*).
- Stage 11 builds its three test releases as stage 10 builds one, under
  versions of the run's own, with the same client configuration, and runs
  each stack in a Compose project of its own, `pubdex-upgrade-<run>-…`,
  whose services, volumes and network carry the run label
  `pubdex.upgrade-acceptance.run`. Its cleanup removes the projects, what
  carries their names or the run label, the test releases' images and
  containers, and its temporary directory, and confirms it as stage 6 does;
  the published images it pulled stay (*Upgrades between PubDex releases on
  Compose*).
- An interrupt from the terminal or an external timeout reaches the running
  stage directly, which cleans up at once. A signal sent to `run_ci_suite.sh`
  alone takes effect when the running stage returns, and no later stage
  starts.

### Component commands for focused debugging

Each stage is also a command of its own. From `DB/`:

| Command | Scope |
|---|---|
| `./run_tests.sh [pytest arguments]` | The offline unit and fixture tests alone, in the same cleared environment. |
| `../.venv/bin/python ../check_secrets_and_local_config.py [PATH ...]` | The secrets and local-configuration check, over the publishable tree or the files given; with `--base <rev> [--head <rev>]` also over the files the commits of that range add or change; `--list-rules` explains every rule. |
| `./run_migration_smoke.sh` | The baseline from an empty database, the schema upgrade twice, and re-application of the runtime patch, in a disposable container. |
| `./run_task_1a_demo.sh` | The clean-clone acceptance path end to end against a disposable database. |
| `./run_disposable_integration.sh [pytest arguments]` | The integration suite on a disposable PostgreSQL; arguments such as `-k attribution` narrow the main run. See *Running the integration suite in a disposable container* below. |
| `PEOPLE_PUBS_INTEGRATION_DSN=... ./run_integration_tests.sh` | The integration tests against a disposable database you started yourself. |
| `../run_stack_acceptance.sh [--release] [--image <reference> [--pull] [--expect-version <version>] [--expect-revision <commit>]]` | The acceptance test of the Compose deployment, with the image built from the checkout or a given one; `--release` verifies a release image with the `compose.yaml` it carries. See *The Compose deployment's acceptance test* below. |
| `../run_operator_acceptance.sh [--image <reference>]` | The operator's workflow on the Compose deployment, with the image built from the checkout or a given one. See *The operator's workflow test* below. |
| `../run_chart_checks.sh` | The offline checks of the Helm chart with the pinned helm; from the repository root, `.venv/bin/python -m pytest charts/tests [pytest arguments]` runs their tests alone. See *The Helm chart's offline checks* below. |
| `../run_cluster_acceptance.sh [--image <reference> --postgres-image <reference> [--expect-version <version>] [--expect-revision <commit>]]` | The Helm chart's lifecycle on a disposable kind cluster, with images built from the checkout or the given ones, which Docker has to have. See *The Helm chart on a disposable cluster* below. |
| `../run_release_rehearsal.sh` | The rehearsal of a PubDex release: test release A built, verified, built again and compared, and its chart package linted and rendered, with nothing published. See *The rehearsal of a PubDex release* below. |
| `../run_upgrade_acceptance.sh` | The upgrades and rollbacks between PubDex releases on the Compose deployment: test release A to B1 and to B2, and the published preceding release to A. See *Upgrades between PubDex releases on Compose* below. |
| `../release.sh build --test <name> [--out <dir>]` | One test release of the checkout as it is, built into `dist/pubdex-<VERSION>-test.<name>/` or the directory given and verified; `../release.sh verify <dir>` checks such a directory again. Needs `../release.sh setup` once. [`../RELEASING.md`](../RELEASING.md) is the procedure of a real PubDex release. |

### Network access during tests

The suite's offline guarantee is enforced inside the Python processes of a
test run. [`tests/conftest.py`](tests/conftest.py) installs the guard in
[`tests/network_guard.py`](tests/network_guard.py) before any test module is
imported, for every pytest run under `tests/`, and Python processes the tests
start inherit it:

- In an offline run — no `PEOPLE_PUBS_INTEGRATION_DSN` or
  `PEOPLE_DB_TEST_DSN` set, as in `./run_tests.sh` — the tests may use no
  network at all: no connection or datagram to any IPv4 or IPv6 address,
  loopback included (a database or service on the machine running the tests
  is out of reach as well), no host-name lookup except `localhost` and the
  machine's own name, and no PostgreSQL connection through psycopg.
- In an integration run the one exception is the disposable database the DSN
  names; any other server, port or provider is refused as before.
- A refusal raises `NetworkAccessRefused` before anything is sent, and is
  logged; a test during which anything was refused fails even when the code
  under test — or a child process it started — caught the exception, and a
  refusal outside a test body (during collection or in a fixture) fails the
  run.

Where it checks: a wrapper around `socket.socket.connect` checks an address
before a host name in it is resolved; an audit hook on every socket connect,
send and name lookup covers code that bypasses that class; and, because libpq
opens its connections in C where the socket module never sees them, psycopg's
public entry points — `psycopg.connect`, `Connection.connect` and
`AsyncConnection.connect` — check the server libpq would reach (the host,
`hostaddr` and port from the connection string, else from the `PG*`
environment, else libpq's defaults) before it is contacted. The environment
puts [`tests/subprocess_guard/`](tests/subprocess_guard/) first on
`PYTHONPATH` and describes the policy, so its `sitecustomize` installs the
same guard in every Python process started with that environment, logging
refusals to the same file. The psycopg wrapper is applied when a process
imports psycopg, so a child that never imports it, or runs on an interpreter
without it, does not load it for the guard.

The guard fails closed. A child process whose guard cannot be installed — an
unreadable policy, no refusal log, a psycopg whose connect methods are not
where the guard expects them — reports that, records it in the refusal log
where one exists, and ends (status 70) or fails the import instead of running
unguarded, and a refusal that cannot be recorded ends the process too.

Boundaries — it is a guard inside Python processes, not operating-system
network isolation: a process started with a cleared environment does not load
it (the Docker-backed scripts clear theirs on purpose, so the demonstration's
Python steps run without it, while stage 5's pytest run installs it again
through `tests/conftest.py`), and neither does one started with `python -I`,
`-E` or `-S`, or a program that is not Python, such as `psql` or `curl`.
libpq reached other than through those psycopg entry points (`psycopg.pq`,
`ctypes`) is not checked, reverse lookups (`gethostbyaddr`, `getnameinfo`)
are not refused, Unix-domain sockets stay allowed as local, and what Docker
itself fetches — the PostgreSQL image, for one — is outside the guard. The
scripts' own steps are offline by construction instead: the demonstration
ingests the bundled fixtures, the offline tests run every script against stub
`docker`, `git` and interpreter executables, and in stages 3–5 the only
server is the run's own container, published on loopback at most.

Stage 6 has an operating-system boundary in addition. Its Compose project
puts PostgreSQL and every ingestion container — the demonstration included —
on an internal Docker network, which has no route off the host, so an
ingestion step that tried to reach a metadata provider would fail the stage
instead of reaching it; the stage checks that the network is internal and
that an ingestion container has no usable default route. The read service is
attached to an ordinary network as well, which Docker needs to publish its
port; it makes no outgoing request. The boundary belongs to the test alone
([`tests/compose.acceptance.yaml`](tests/compose.acceptance.yaml)):
`compose.yaml` keeps Docker's ordinary default network, so that ingestion an
operator runs in a deployment can reach the providers. What Docker itself
fetches for this stage — the base image and the Python packages of the image
build, and the PostgreSQL image of `compose.yaml` — is outside every
boundary, as for stages 3–5.

Stage 7 has no such boundary, on purpose: it is the run on the ordinary
network an operator's deployment has, and checks that an ingestion container
there has a default route. It stays away from the metadata providers by its
commands alone. The roster import runs with `--no-orcid-lookup`, the only
ingestion is `people_pubs.sync.fixture_demo`, whose sources are the bundled
payloads, and the other commands — the attribution rule, the refresh, the
export and a connection check — make no request. Stage 6 is the one that
proves the demonstration needs no network.

Stages 9 to 11 build test releases with `release.sh`, whose image builds,
like those of stages 6 and 7, pull the base images by digest and fetch the
locked files unless Docker has them. The SBOM tool and `release.sh`'s
probes of the images run in containers without network, and helm contacts
no cluster but stage 9's own. Stage 11's stacks run on the ordinary network
of `compose.yaml`, as stage 7's does, and keep away from the metadata
providers by their commands alone: the only ingestion is the
demonstration's, of the bundled payloads. Stage 11 pulls the published
images of the preceding PubDex release, and of the floor, by digest when
Docker lacks them, without credentials; stage 9's nodes pull them
themselves (*The Helm chart on a disposable cluster*).

The tests of the guard itself
([`tests/test_network_guard.py`](tests/test_network_guard.py) and
[`tests/integration/test_network_guard_integration.py`](tests/integration/test_network_guard_integration.py))
prove that an accidental connection, lookup, datagram or psycopg connection is
refused, in the test process and in child processes, and that the disposable
database stays reachable from both. Child-process database connections are
tested against synthetic loopback listeners the tests open themselves, which
count what reaches them, and against the disposable database in stage 5;
never against an existing database or an external service.

### Expected skips

A skip can hide a test that silently stopped running, so every skip a run may
contain is named in [`tests/expected_skips.py`](tests/expected_skips.py): which
tests may skip, with which reason, under which condition. Any other skip, any
expected failure (xfail), and any test module skipped while it is collected
makes the run fail even when every test passed; the run lists the expected
skips by rule at the end. Only a passing run is turned into a failing one — a
status pytest sets itself for failed tests, an interrupted collection or a
run without tests stays as it is. The rules name tests and reasons, not
counts, so adding tests does not make the policy stale — a new reason to skip
needs a new rule:

| Rule | Tests | Reason | Condition, checked when the skip happens |
|---|---|---|---|
| integration test without a disposable database | tests marked `integration` under `tests/integration/` | "set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests" | neither `PEOPLE_PUBS_INTEGRATION_DSN` nor `PEOPLE_DB_TEST_DSN` is set; with one set, as in stage 5, the skip fails the run |
| no discovery job in refresh_jobs.json | `test_discovery_job_caps_new_records`, `test_discovery_job_skips_existing` and `test_affiliation_job_has_post_filter` in `tests/test_refresh_jobs_guardrails.py` | pytest's "got empty parameter set" | the shipped job list has no discovery job to parametrize over; synthetic probes in the same module run the same checks |
| discovery job without an affiliation search | `test_affiliation_job_has_post_filter` in the same module | "not an affiliation job" | the discovery job does not search affiliations |
| running as root | `test_unreadable_file_is_an_error_not_a_pass` in `tests/test_secrets_and_local_config.py` | "root can read any file" | the run's effective user is root |
| no timeout utility | `test_an_external_timeout_stops_the_running_stage_and_the_suite` in `tests/test_ci_suite_script.py` and `test_an_external_timeout_cleans_up` in `tests/test_disposable_integration_script.py` | "needs the timeout utility" | `timeout(1)` is not on `PATH` |

No collection-time skip is expected. A module that calls
`pytest.skip(..., allow_module_level=True)`, `pytest.importorskip` or raises
`unittest.SkipTest` while it is imported takes all its tests out of the run
and leaves no trace in the test count, so such a skip fails the run unless a
narrowly justified exception is added to `COLLECTION_RULES`, which is empty.

An offline run on Linux as an ordinary user therefore skips exactly the
integration tests and the three empty-parameter guardrails.

### Committed secrets and local configuration

`check_secrets_and_local_config.py` needs only the Python standard library. It
checks every file the repository would publish — the tracked files plus
untracked, nonignored ones, as `git ls-files --cached --others
--exclude-standard` lists them — so a new file is checked before it is staged,
while ignored material (virtual environments, caches, `DB/.env`, database state
and backups) is never opened. It reports:

- environment files other than `.example`, `.sample` or `.template`
  templates; private-key, key-store and credential files; database dumps and
  backups; machine-local state such as caches, logs, `DB/data/`, `DB/backups/`
  and `DB/cron`; symbolic links pointing outside the repository;
- private-key material and recognisable token formats (cloud, forge, chat,
  payment and package-registry tokens, `sk-` API keys, JSON Web Tokens);
- passwords in URLs and connection strings, and password, secret, token or
  API-key settings with a literal value in env files and their templates,
  configuration files, shell scripts, Dockerfiles, Markdown and Python
  source;
- in Python, which is read as a syntax tree wherever a statement stands
  (also in a one-line `if`, `class` or `def`, or after a semicolon): literal
  values for secret-like names in assignments, attribute and subscript
  targets (`self.password = ...`, `os.environ["PGPASSWORD"] = ...`), keyword
  arguments (`Client(api_key=...)`), dictionary entries (`{"api_key": ...}`)
  and parameter defaults, and the literal fallback of a lookup such as
  `os.environ.get("API_KEY", "...")` or `settings.get("password", "...")`. A
  value counts as literal when it is a string, a concatenation of strings, an
  f-string whose literal text is more than a short affix, either operand of
  `or` or a branch of a conditional expression. A value obtained elsewhere —
  `api_key=args.api_key`, `os.environ["API_KEY"]`, a lookup without a
  fallback or with an empty or placeholder fallback, `f"smoke-{run_id}"` —
  and comments and docstrings are not reported;
- absolute paths into a user's home directory and RFC 1918 private network
  addresses.

A credential that one commit adds and a later one removes is gone from the
tree but stays in the published history, so with `--base <rev> [--head
<rev>]` (`--head` defaults to `HEAD`) the check also reads the commits a push
or a pull request adds: every file each commit in `base..head` adds or
changes, with the same rules, earlier versions of a file included, and for a
merge commit what it changes relative to all of its parents. Commits the base
already contains are not read again, so the work grows with the range, not
with the history. `./run_ci_suite.sh` hands its range to the check, and the
GitHub workflow passes the event's: on a pull request the commits it adds,
with the merge commit GitHub tests, and on a push to `main` those after the
previous tip. Without a base to bound the range — an empty or all-zero base,
as the first push of a branch reports it, or a base the clone does not
contain, as after a rewritten history — it reads every commit reachable from
the head and says so. Reading commits needs the complete history and every
object in it: in a shallow clone, or when an object cannot be read (a
partial clone, a damaged repository), the check stops with status 2 instead of
passing, and a finding in the tree is still reported. A finding in a commit is
written as `<commit>:<path>:<line>: <rule>`, the commit abbreviated as Git
does, never with the matched text. A commit that is published already can no
longer change, so a finding in one is accepted only by an entry in the
checker's `ACCEPTED_IN_HISTORY` naming the commit, path, line and rule; the
only entries are two synthetic test values in `tests/test_publication_policy.py`
of the published task-1a commit, which later commits name as examples. The
same text added again by any other commit is reported. Without `--base` —
the plain local run — only the tree is checked.

A finding names the rule, the file and the line — never the matched text — so
a report can be shared safely. A value is not a finding when it is empty; when
it is, as a whole, a template field or a description of one (`{{ name }}`,
`{name}`, `%(name)s`, `<password>`, `[redacted]`) or a mask (`...`, `***`,
`xxxx`); when it is a conventional placeholder word, alone or followed by more
words — `change-me-postgres` as in `.env.example`, `your-password-here`,
`example`, `placeholder`, `dummy`, `redacted`; or when it refers to its value
instead of containing it: `$NAME`, `${NAME}`, `${NAME:-}`, `$(command)` or
`${{ expression }}`. A fallback or alternate value written into a reference —
`${NAME:-value}`, `${NAME-value}`, `${NAME:=value}`, `${NAME:+value}`, also
nested — is a value the expansion can produce, so it is judged like any other
value: `API_KEY="${API_KEY:-<literal key>}"` is a finding, while
`${PGPASSWORD:-}`, `${PGPASSWORD:-change-me}` and
`${PGPASSWORD:-$(cat /run/secrets/db)}` are not. A value composed at run
time from such references and at most twelve lower-case letters, digits and
separators, such as
`smoke-$RUN_ID`, is accepted only in shell code — scripts, Dockerfiles, shell
snippets in Markdown and Compose-style `KEY=value` list items — because only
there is it expanded. In env files (which `people_pubs` reads without
expansion), configuration files, JSON and Python strings, and as the password
of a URL or connection string, a value has to be a reference or template as a
whole: `PASSWORD = "smoke-$RUN_ID"` in Python is a finding. In shell code and
env files single-quoted text is literal, as the shell reads it, and a `$` or
an opening bracket somewhere in a value is never enough to make it a
placeholder. Exit status 0 means nothing was found, 1 means findings, 2 means
the check could not be completed.

What a clean result means: the publishable tree holds nothing these rules
recognise. It is a finite pattern check, not proof that no secret is present,
and its limits are accepted deliberately. Secrets are recognised by a known
token format or by a secret-like name, so a literal under an unremarkable
name (`x = "..."`) or passed positionally (`connect("...")`) passes, and so
does a token in a format the rules do not know. Token-, API-key- and
credential-like names count only for a value shaped like a credential — eight
or more characters mixing letters and digits — because such names also hold
identifiers (`source_token: crossref`); password- and secret-like names count
for any literal, so an API key written without a digit passes where the same
text as a password would be reported. A value that is syntactically a
template field (`{name}`) is accepted everywhere, a `${{ expression }}` is
accepted as a whole, whatever literal it contains, and in shell code a
double-quoted composition with a short lower-case affix (`"pa$word"`, which
the shell expands) is accepted too. Python that does not parse is read
statement by statement for simple string assignments only, and Python inside
Markdown is read as shell text. Only RFC 1918 IPv4 addresses count as private
addresses, and binary files are checked for dump signatures only. History is
read only for the commits of the range given — not before the base, and not
at all in a plain local run — and only what Git stores: content kept outside
Git objects, as by Git LFS, is not read.

## Coverage by purpose

A test count says little on its own: one parametrized rule table of the
secrets check accounts for a third of the tests, while one integration test
can drive a whole importer through the database. The 759 tests the working
tree verified on 2026-10-05 collects, by what they protect and by origin
(the task-1a baseline is the published commit tagged `public-baseline-1a`):

| Purpose | Test modules | Tests | In the task-1a baseline | Added or extended in task 1b |
|---|---|---|---|---|
| Ingestion, provenance, deduplication, curation and policy logic | 14 offline modules | 193 | all 193 | — |
| Offline source fixtures: the bundled ORCID, Crossref, OpenAlex, DataCite, DBLP and Semantic Scholar payloads parse into the expected records | 7 offline modules | 53 | all 53 | — |
| Database behaviour on a disposable PostgreSQL | 17 integration modules | 102 | 92 in 15 modules | reruns of the DataCite, DBLP and Semantic Scholar importers (3 regression and 3 characterization tests); the database roles `app_writer`, `app_readonly` and `maintenance` (4) |
| Lifecycle of the Docker-backed scripts: smoke test, demonstration, integration runner | `test_migration_smoke_script.py`, `test_disposable_integration_script.py` | 66 | 7 tests of the smoke script | 25 more for the smoke test and the demonstration; 34 for the new runner |
| The one command and its CI adapters | `test_ci_suite_script.py` | 35 | — | all |
| Offline isolation: cleared environment, no `DB/.env` | `test_offline_isolation.py`, `test_config_dotenv.py` | 16 | — | all |
| Network guard | `test_network_guard.py`, `integration/test_network_guard_integration.py` | 23 + 5 | — | all |
| Skip policy | `test_expected_skips.py` | 19 | — | all |
| Secrets and local-configuration check, tree and commits | `test_secrets_and_local_config.py` | 247 | — | all |

Stages 3 and 4 are checks of their own besides these tests: the migration
smoke and the demonstration assert on a real database what they prove, and
both were already part of the task-1a baseline.

Added after that record, for task 2a (working tree of 2026-10-08): the read
service, with 138 offline tests in `test_api_app.py`, `test_api_settings.py`
and `test_api_server.py` and 10 integration tests in
`integration/test_api_integration.py` (*What is covered now*, below) — 907
tests collected in all, 117 of them integration tests. Then (working tree of
2026-10-14) the application image and the Compose deployment: 138 offline
tests in `test_password_files.py`, `test_role_passwords_script.py`,
`test_create_secrets_script.py`, `test_build_image_script.py`,
`test_api_probe.py` and `test_container_files.py`, and 3 integration tests —
the snapshot-refresh function in `integration/test_application_roles_integration.py`
and the demonstration ingested as `app_writer` in
`integration/test_api_integration.py` — 1048 tests collected in all, 120 of
them integration tests. Starting the stack itself is checked against Docker,
not by these tests. Then (working tree of 2026-10-17) the stack as stage 6
of the suite: 39 offline tests of its runner in `test_stack_acceptance_script.py`,
one of the acceptance addition to the Compose file in `test_container_files.py`,
and a sixth stage in the order and failure tests of `test_ci_suite_script.py`
— 1089 tests collected in all, 120 of them integration tests. Then (working
tree of 2026-10-19) two cases in `test_build_image_script.py` and one in
`test_stack_acceptance_script.py` for a Python module that Git ignores but
the build context takes in — 1092 tests collected in all, 120 of them
integration tests. Then (working tree of 2026-10-22) the shared Docker
version rules, with 42 tests in the new `test_docker_versions.py`; 18 more
in `test_roster_and_export.py` for a roster on standard input and the errors
that stop an import before it writes; 59 more in
`test_stack_acceptance_script.py`, 99 in all, for the version prerequisites
and the deployment's identity; 3 more in `test_container_files.py` for the
removed setuptools and the fixed pins; one more in `test_ci_suite_script.py`
for the seventh stage; and the operator's workflow test, with 23 tests in
the new `test_operator_acceptance_script.py` — 1238 tests collected in all,
120 of them integration tests. Then (working tree of 2026-10-27) 10 tests in
the new `test_bootstrap.py` for the copy of the schema bootstrap; 37 in the
new `test_set_passwords_script.py` for the password change; 27 more in
`test_role_passwords_script.py` for its standard-input mode; 12 more in
`test_create_secrets_script.py` for `--check`; 10 more in
`test_container_files.py` for the image's contents and the hashed installs;
6 more in `test_docker_versions.py` for the one Engine minimum; one more
each in `test_build_image_script.py` and `test_operator_acceptance_script.py`;
and one fewer in `test_stack_acceptance_script.py`, now 98, whose release
check reads the image's own `compose.yaml` instead of a checkout — 1341
tests collected in all, 120 of them integration tests. Then (working tree
of 2026-10-28) the Engine requirement of 28.0.1 or later, except 28.2.0 to
28.3.2: 16 more tests in `test_docker_versions.py`, for the releases on
either side of each boundary and how the acceptance tests report their
binding check; 5 more in `test_stack_acceptance_script.py`, now 103, and 4
more in `test_operator_acceptance_script.py`, for refused and accepted
Engines — 1366 tests collected in all, 120 of them integration tests. Then
(the same day) the dependency updates: 2 more tests in
`test_container_files.py`, for pip's release and the workflow's Python —
1368 tests collected in all, 120 of them integration tests. Then (the same
day) pip leaving the image after the build, and pip's own lock in every
setup: 13 more tests in `test_ci_suite_script.py`, for the setup commands of
the scripts' messages, the documentation and the workflows; one more in
`test_container_files.py`; and 3 more in `test_stack_acceptance_script.py`,
now 106, for the check of the image's packages — 1385 tests collected in
all, 120 of them integration tests. Then (the same day) that check run on
synthetic images instead of against fixed answers: 2 more tests in
`test_stack_acceptance_script.py`, now 108, for a missing package, one at
another version and a lock that pins nothing — 1387 tests collected in all,
120 of them integration tests. Then (the same day) the narrow security
mitigations: 22 tests in the new `test_connection_security.py` and 2
integration tests in the new
`integration/test_connection_security_integration.py` for the
authentication every connection requires; 15 in the new
`test_install_pip.py` for the pip bootstrap; and 10 more in
`test_ci_suite_script.py`, now 60, for setups that create `.venv` without
pip and install from binary wheels only — 1436 tests collected in all, 122
of them integration tests. Then, on 2026-10-29, two corrections of the pip
bootstrap — pip run without the machine's pip configuration, success only
when the environment imports the locked pip, and redirects to `https:` only:
17 more tests in `test_install_pip.py`, now 32 — 1453 tests collected in
all, 122 of them integration tests.

### The suite's own machinery, and why it stays

Several parts of the suite are written for this repository rather than taken
from a framework. Each covers something the standard tools leave open, and
each has the tests listed above:

- **The disposable-database lifecycle** — unique labelled resources, a
  random password kept off every command line, masked output, a cleanup that
  removes only its own run's resources and confirms it, signal handling —
  exists because the scripts run next to other Docker work, on developer
  machines as well as runners, and must never touch or leak into anything
  else. Stages 3–5 do not use Docker Compose, because a Compose project
  named after the directory can join an existing installation's containers.
  The smoke test, the demonstration and the integration runner share one
  implementation of it, the sourced file
  [`lib/disposable_postgres.sh`](lib/disposable_postgres.sh), so a fix or
  a stricter check, such as the fail-fast readiness wait, applies to all
  three at once. Stage 6, which has to test the Compose deployment itself,
  gives its project an explicit name, files and settings, and keeps the same
  rules — a run label, refusal of anything that exists already, a confirmed
  cleanup — in [`../run_stack_acceptance.sh`](../run_stack_acceptance.sh).
  Stage 7 keeps them, by its project's name, in
  [`../run_operator_acceptance.sh`](../run_operator_acceptance.sh). The two
  share the clearing of the environment, the time limits, the masking of
  passwords and the HTTP requests in the sourced file
  [`lib/compose_acceptance.sh`](lib/compose_acceptance.sh).
- **The network guard** covers what socket-blocking pytest plugins do not:
  psycopg connections, which libpq opens in C, and the Python processes the
  tests start. Isolation by the operating system (network namespaces, a
  container without a network) would be stronger but is not available on
  every runner, and the guard documents what it leaves out.
- **The skip policy** exists because pytest has no setting that fails a run
  on an unexpected skip, and this suite skips by design — the integration
  tests offline — so an accidental skip elsewhere would otherwise pass
  unnoticed. The integration runner's own check of its test report (no skip,
  at least one test) overlaps with it on purpose: it does not depend on the
  plugin being loaded, it also catches an empty run, and it produces the
  counted summary the verification records quote.
- **The cleared environment** of the offline tests, and
  `PEOPLE_PUBS_SKIP_DOTENV`, exist because `people_pubs.config` fills
  settings in from `DB/.env` and the process environment, which on a
  developer machine can name a real database.
- **The secrets and local-configuration check** needs only the Python
  standard library, so it runs before the requirements are installed and on
  any forge without fetching a tool, and it applies one rule set to the
  tree and to the commits of a range. Its rules for local configuration —
  environment files, this repository's runtime locations, home-directory
  paths, private addresses, literal values in Python syntax — are ones
  general-purpose secret scanners do not cover.

## What is covered now

- Pure-unit regression tests cover DOI normalization (including HTML-escaped
  repository landing-page version selectors), ORCID normalization, source
  ranking, shared publication canonical policy, `source_of_truth` merge
  behavior, `upsert_publication()` provenance preparation, author-order tags,
  preprint heuristics, and publication-date parsing.
- Attribution catch-up unit tests distinguish retryable per-publication
  provider errors from completed no-payload lookups and pin the rule that
  error outcomes bypass the normal attempt cooldown.
- Internal-versus-external author classification is pinned by
  `tests/integration/test_internal_author_classification_integration.py`:
  `biblio.authorships.is_internal_at_ingest` follows the linked person's
  `app.people.person_kind`, an external co-author stays linked and keeps its
  source position while staying out of `internal_authors`, reruns are stable,
  and a `force_person` overlay follows the overridden person's classification
  in both directions. The migration smoke additionally seeds rows whose flag
  disagrees with `person_kind` and proves that re-applying the runtime patch
  repairs them.
- Primary-email selection is pinned as domain-neutral in both implementations:
  `tests/test_primary_email_policy.py` for the Python helper and
  `tests/integration/test_primary_email_policy_integration.py` for the SQL
  functions — an explicitly set primary is kept, an explicitly supplied one is
  honoured where the function accepts it, and otherwise the first address in
  stable input order wins, with no organisation, domain or top-level domain
  receiving special treatment and no silent fallback to lexical order.
- Golden fixtures live under `tests/fixtures/golden/` and are intentionally tiny, synthetic examples for manual CSV, Crossref, ORCID, OpenAlex, DataCite, DBLP and Semantic Scholar payload shapes, plus a safe demo roster and the export and source-coverage golden files.
- Offline fixture-backed ingestion tests (`tests/test_orcid_fixture_ingest.py`, `tests/test_crossref_fixture_ingest.py`) lock down ORCID work-summary/contributor extraction and Crossref DOI/date/preprint/author parsing so plain CI never depends on live API availability; `tests/test_roster_and_export.py` does the same for `add_people` roster validation, its roster input from a file or from standard input (`--csv -`: UTF-8 with a byte-order mark and CRLF line ends read like a file, the delimiter, an empty roster, one without an `orcid` column or header, Latin-1 and UTF-16 input refused before any database connection, an unreadable line stopping the run with its number and nothing committed, a terminal refused, a missing, unreadable or directory path, the conflict with `--orcid`, and messages that number rejected rows and never quote a name or address), and the deterministic publication CSV export.
- `./run_migration_smoke.sh` (needs Docker and `.venv`) proves the baseline bootstraps from an empty database without a record of schema changes, that the schema upgrade then applies and records every schema change of `schema/` and a second schema upgrade changes nothing (*The schema upgrade's tests*), and that `init/010_ingestion_runtime_schema.sql` re-applies cleanly and leaves the record as it was, with the login roles' `USAGE` — never `CREATE` — on the schema holding the extensions in place after each step. A last re-application runs after the `maintenance` role has been removed from the run's own cluster, as for a database restored into a cluster without it: the patch, which grants that role `USAGE` only where it exists, has to succeed there and must not create the role. It uses a uniquely named, labelled container and volume, published on a loopback port Docker picks for the schema upgrade, and removes only those two and its private temporary directory, so it can run next to other instances and in parallel. It is stage 3 of `../run_ci_suite.sh`; run it on its own whenever `schema/` or `people_pubs/schema.py` changes.
- `./run_task_1a_demo.sh` (needs Docker) runs the clean-clone acceptance path end to end against its own disposable database: bootstrap, synthetic roster, the bundled ORCID payload, Crossref enrichment of the same work, a stored-record query, a canonical-list query and an export whose contents it verifies. It contacts no metadata provider, never uses Docker Compose, and removes only the container and volume it created. `tests/test_fixture_demo.py` pins the offline half (the fixture sources open no socket; the payloads still parse into the expected publication, provenance and authorship shapes) and `tests/integration/test_fixture_demo_integration.py` the database half. It is stage 4 of `../run_ci_suite.sh`.
- `tests/test_migration_smoke_script.py` runs that script offline against a stub `docker` and pins its resource handling: unique names, cleanup registered before creation, no pre-delete, removal of exactly what it created after success, a failed start, a bounded bootstrap timeout, a container exit or an interrupt, and an abort on a name collision. For the migration smoke and the acceptance demonstration alike — the demonstration runs from a scratch checkout whose interpreter records every Python step instead of connecting anywhere — it also proves that a second interrupt arriving while the container is being removed does not cut the cleanup short; that a cleanup which fails — a removal Docker refuses, one that reports success but leaves the resource, a daemon that no longer answers, or (for the demonstration) a temporary directory that cannot be removed — is reported with the commands that find the leftovers and fails an otherwise successful run, while a failed start or a signal keeps its own exit status; and that the superuser password is random per run, independent of the run id, handed to Docker by name so that no `docker` argument holds it, absent from the output, and replaced by `<password>` in a container log tail that contained it. From the recorded steps of a whole demonstration run it also proves: no step takes `--dsn`, no Python, `docker` or `env` command line holds the password, and each step's environment holds exactly the isolated settings — the database bound through `PEOPLE_DB_DSN` and `PG*`, and `PEOPLE_PUBS_SKIP_DOTENV=1` — with nothing inherited from a polluted caller. For both it proves that a container which finished initialising without running `init/` — an empty mount, as when the Docker daemon cannot see the checkout — stops the run at once with that diagnosis, and that `DISPOSABLE_PG_IMAGE` sets the image while each script's own variable takes precedence and the run reports the image it used.
- `tests/test_disposable_integration_script.py` does the same for `./run_disposable_integration.sh`, against a stub `docker` and a stub test command: unique owned resources, no pre-delete, a loopback-only port Docker picks, `init/` mounted read-only, the password kept off every command line — Docker's, the test command's and any `env` launcher's — and masked in the test output, in the container diagnostics and in the error output of a failed re-application, a test environment cleared of inherited variables and exported shell functions in which every DSN and `PG*` setting names the disposable database, `PEOPLE_PUBS_SKIP_DOTENV=1` is set and no `PYTEST_ADDOPTS` survives, the suite, the runtime-patch re-application and the rerun of the view and role tests in that order, the tests' own exit status preserved, a skipped or empty run treated as a failure, a bounded readiness wait, an immediate stop on an exited container, a failed init script or an init directory the daemon could not see, the image variables, a refused non-loopback port, an abort on a name collision, and removal of exactly what it created — including its private temporary directory — after success, failure, SIGINT, SIGTERM, SIGHUP, an external timeout or a second interrupt during the cleanup. A cleanup that fails in any of the ways above, or a temporary directory that cannot be removed, is reported and fails an otherwise successful run, while a test failure or a signal keeps its own exit status.
- `tests/test_network_guard.py` proves the guard offline: a connection to an external address, a host-name lookup of a provider (also through a direct `connect()`, which would otherwise resolve the name first), a request through the package's own DataCite client, a loopback connection, a datagram, a connection through the lower-level `_socket` class and psycopg connections to loopback, remote or default-socket servers are all refused at once; Unix-domain sockets keep working; the integration policy admits its one database and nothing else; a child Python process is refused too, and a refusal it swallows is still recorded; and a test that swallows a refusal fails. In child processes it also proves, against synthetic loopback listeners that count what reaches them, that psycopg connections through `psycopg.connect`, `Connection.connect` and `AsyncConnection.connect` are refused before the server is contacted under the offline policy, and reach the allowed server — and only that one — under a database policy; that a child loads psycopg only when it imports it and starts normally without it; that a database refusal a grandchild swallows fails the enclosing test run; that a test run started by a test, which holds a second copy of the guard, imports psycopg wrapped by both copies; and that a guard which cannot be set up, or a psycopg it cannot wrap, stops the child or fails its import and is recorded. `tests/integration/test_network_guard_integration.py` proves on the disposable database that psycopg, `people_pubs.db.connection`, a plain socket and a child process — over a socket and through psycopg, synchronous and asynchronous — reach it, while another port, another host or a provider is refused.
- `tests/test_expected_skips.py` pins each rule of the skip policy on the tests it names and the condition it states, with unrelated tests, other tests of the same module and false conditions (not root, `timeout(1)` present, a database configured) as counterparts, and that every test it names exists. Through whole test runs that load `tests/conftest.py` as a plugin it proves that expected skips keep a run green; that a module skipped while it is collected — by `pytest.skip(..., allow_module_level=True)`, `pytest.importorskip` or `unittest.SkipTest` — fails a run whose other test passes, which plain pytest would let pass; that unrelated tests and false conditions cannot use an exception; that an integration skip with a database configured, or outside `tests/integration/`, and an expected failure fail the run; and that pytest's own failure statuses stay as they are.
- `tests/test_ci_suite_script.py` runs `../run_ci_suite.sh` offline, with every stage replaced by a recording stub: the eleven stages in order from any working directory, the first failure stopping the run with its own status, `--base`/`--head` reaching the offline stage and the secrets check unchanged and no other stage (the all-zero and empty first-push bases included), the environment report with the Engine and Compose versions, usage errors, the diagnostics for a missing interpreter, missing requirements, a missing pinned helm (with `./setup_cluster_tools.sh` as the command), a missing SBOM tool (with `./release.sh setup` as the command), missing Git or Docker, an unreachable daemon, a Docker Engine older than 28.0.1 (25.0.5, Debian 13's 26.1.5, 27.5.1 and 28.0.0 among them), one of 28.2.0 to 28.3.2 or of no recognisable version — with no stage run — while 28.0.1, 28.0.4, 28.3.3 and 29.9.2 are accepted, and a Compose plugin that is missing, older than 2.24.0 — 2.20.1, which rejects the `start_interval` of `compose.yaml`, 2.20.2 and 2.23.3, which leave it out of the containers, and a 2.24.0 pre-release included — or of no recognisable version, while 2.24.0, a `v` prefix, a distribution's build suffix, a later pre-release and Compose 5 are accepted, and what a terminal interrupt, an external timeout or a signal to the suite alone does. The message for a missing `.venv` — of this suite, `run_checks.sh`, `DB/run_task_1a_demo.sh` and `DB/run_disposable_integration.sh` — and for an existing one that lacks packages gives `./setup_venv.sh` and no other command; `setup_venv.sh` itself, run with recording interpreters, creates `.venv` without pip, fetches the files of the pip, build and development locks with `fetch_verified.py` into a temporary directory, installs pip with `install_pip.py` and the development lock with `install_locked.py` from that directory, runs `pip check` and removes the directory, passing `--python`, `--psycopg` and `--network-isolation` on, and refuses an existing `.venv` and unknown choices; and every setup in `README.md`, `QUICKSTART.md`, `DB/README.md`, this page, both workflows and those scripts gives `setup_venv.sh`, and none creates a `.venv`, runs pip to install anything or calls the fetch or install scripts itself. It also pins the public CI workflow as a thin adapter that only calls `setup_venv.sh`, `setup_cluster_tools.sh`, `release.sh setup` and the suite, never helm, kubectl, a stage or another step of `release.sh` itself, with its actions pinned by commit and its runner image, Python version fixed; it runs the shell steps of the GitHub and the Forgejo workflow under Bash, with their expressions evaluated against synthetic event contexts of each forge for a push, the first push of a branch and a pull request, proving that they call `./setup_venv.sh` with the workflow's interpreter, then `./setup_cluster_tools.sh` and `./release.sh setup`, and hand the suite the range each forge's pull request needs — the target branch's tip on GitHub, which tests a merge commit, and the merge base on Forgejo, which tests the head commit, also when the target branch has moved on; it pins that neither workflow takes a credential or has a step that could publish — the job's commands are those four lines and no other, its only actions the checkout and, on GitHub, the interpreter, the only values it takes from the forge the event's two commits, no secret, token, login, upload or write permission is named, the checkout does not keep the forge's token, and GitHub's permissions are `contents: read` alone; it pins the Forgejo workflow to the documented host label, to actions from Forgejo's mirror by absolute URL and commit, and to the same checkout commit, setup and time limit as the GitHub workflow, the limit this page states; and it runs the script lines of the GitLab CI example in *Running the suite elsewhere* under Bash, as a shell executor would, with the variables GitLab sets for a push, the first push of a branch and a merge-request pipeline, proving that they call `./setup_venv.sh --python python3.11`, then `./setup_cluster_tools.sh` and `./release.sh setup`, and hand the suite the expected range.
- `tests/test_install_pip.py` runs the real `install_pip.py`, the pip bootstrap of every new `.venv` and of the image, in a virtual environment without pip, against a wheelhouse holding a stand-in for pip's wheel, which records how it is run and the network interfaces it sees and places itself in the site-packages of the Python that runs it: a wheel whose SHA-256 the lock lists, and that runs as the locked pip, installs itself with `python -I` from a copy in a temporary directory of its own (`--no-index`), with `--isolated`, `--require-hashes`, `--only-binary=:all:` and `--no-deps`, in an environment where `PIP_CONFIG_FILE` names `os.devnull` and no other pip variable is set, whatever the caller's environment holds, and the copy is removed; it runs in a network namespace with nothing but loopback where the system permits one, and otherwise says so — with `--network-isolation required` it then runs nothing; success is reported only when the environment's Python then imports pip of the locked version, by module and by metadata, from its own site-packages — not from another directory, also not one a path file adds; a corrupted wheel, a missing one, a link in its place, a file the lock lists that is no wheel, and a wheel that reports another pip version are never run as an installer; a failed installation is reported; a lock that is not exactly one `pip==` requirement with hashes is refused before anything else; and the script needs nothing outside the standard library. The real pip runs too: a wheel of the pip installed with the tests, rebuilt from its files after each is checked against its `RECORD`, installs itself into the virtual environment under an `[install] target` from each place pip reads configuration — `PIP_CONFIG_FILE`, `PIP_*` variables, and the global, site and user files, all temporary, after pip itself has shown that it follows them.
- `tests/test_fetch_verified.py` tests `fetch_verified.py`, the one downloader of every setup and image build. Downloads over `https:` run in the test process through urllib's own handlers, with only `http.client`'s connections replaced by a transport that answers from a table and records each connection before anything is sent: a redirect from `https:` to `http:`, to `http:` and back to `https:`, or to `ftp:` is refused before that destination is contacted, and a redirect or a link from an `https:` page to a local file or to plain HTTP is refused before it is followed; redirects from `https:` to `https:` are followed, with certificate and host-name verification and the timeout; a failed request, a file over its size limit, a page over its limit and a total over its limit leave nothing under the file's name, and so does a file whose SHA-256 the lock does not list or that is not a wheel of its pin. The deadline is tested against real sockets: a server on a Unix-domain socket, which every host name resolves to, stalls while the downloader's own connection code, `http.client` and urllib run against it, with a timeout per operation far longer than the deadline. A body of which one byte arrives every 20 ms is cut off at the deadline while the first buffered read of 64 KiB is still waiting — no chunk reaches the file and nothing is kept — and so are a lookup that does not return, whose thread is shown to end once the resolver returns, its late answer dropped, a real TLS handshake with a peer that never answers, and response headers that trickle in after a redirect, the deadline counted from the first request; the server sees each connection shut down, a download within its deadline is kept, and no thread or descriptor of the downloader's is left. Whole runs against a local index of synthetic wheels take exactly one file per pin — a pure-Python wheel or one for the target CPython on a glibc no newer than the target's, a source archive only where `--allow-source` names the project — and refuse a pin with none, with two, or with a yanked one, locks that disagree or carry options, markers or URLs, and a wheelhouse directory that is not empty; the target's tag rules are tested one by one; and stand-in sockets show that the connection attempts interleave IPv6 and IPv4 and start every quarter of a second, so that an address that does not answer does not cost the timeout.
- `tests/test_install_locked.py` runs the real `install_locked.py` in a virtual environment without pip, into which `install_pip.py` installs a wheel of the pip installed with the tests, with synthetic locks and wheels: the real pip installs the lock from the wheelhouse alone, without network where a namespace is possible, and whatever pip's configuration files and variables say about indexes, extra file locations, hash checking or the destination; `--network-isolation required` without a namespace installs nothing; and a wheelhouse with a file missing, an extra, changed or linked one, a source archive where a wheel is locked, two files for one pin or another version stops the run before pip. The C build runs on a synthetic psycopg-c source archive whose build backend records the directory it runs in, the `PATH` it sees and the `pg_config` it finds: with the wheelhouse, the three locks, `--pg-config`, `--build-only` and `--built` given relative to the caller's directory, the build finds its inputs although pip and the backend run elsewhere, `PATH` holds no relative entry, the chosen `pg_config` wins over another one on `PATH`, and the build environment's pip is the one `--pip-lock` fixes; a `--pg-config` that is not the `pg_config` the build finds first is refused. Unit tests pin the requirements each psycopg implementation installs — psycopg-c by the hash of its build, or not at all — the record of a built psycopg-c wheel, which has to describe the verified archive and the wheel beside it, the build's environment without `PYTHON*` variables, the caller's `HOME` or pip's settings, the final check, which refuses another implementation than the one asked for, a libpq older than 16 and an installed psycopg-binary, and the detection of an `unshare` that does not isolate.
- `tests/test_offline_isolation.py` runs the real `./run_tests.sh`, `../run_checks.sh` and `../run_ci_suite.sh` from a scratch checkout whose interpreter only records how it was called, with test DSNs, `PEOPLE_DB_DSN`, `PG*` and `POSTGRES_*` settings, `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, `PYTHONPATH` and `PEOPLE_PUBS_SKIP_DOTENV=0` exported: standalone and as stage 1 of the complete suite, pytest starts with none of them, with `PEOPLE_PUBS_SKIP_DOTENV=1` and no narrowing argument, while a commit range still reaches the transform-version guard and an all-zero base still counts as no range; `run_checks.sh` runs the release-note check, with `PEOPLE_PUBS_SKIP_DOTENV=1`, and the version guard after the Markdown check and before the tests.
- `tests/test_release_notes.py` runs `check_release_notes.py` on the checkout, which passes, and on fixture trees, each a repository root of its own with `VERSION`, a release record, `DB/schema/`, a transform version and `CHANGELOG.md`, in which one rule is broken: a missing section, two sections of one version, each missing heading, headings out of order or after another subsection; a new schema change that *Schema changes* does not name as a code span in a list item of its own, or names without saying "backward compatible" or "not backward compatible" as its file declares; a named schema change that does not exist or has another name; text other than "None." when no schema change is new; a schema change of the preceding PubDex release missing from `DB/schema/` or edited; *Ingestion changes* "None." with a new transform version, text without the new transform version, or text with an unchanged one; *Upgrade and rollback* without the floor or the preceding PubDex release, without saying that a raised floor was raised, or without saying that the rollback is a restore when a new schema change is not backward compatible; a test release's version in `VERSION`; a patch release with a new schema change or a new transform version. Each fails with status 1 and a message naming what is wrong, and trees that follow every rule pass: a minor release with a backward-compatible schema change, a patch release that changes nothing, and a minor release with a schema change that is not backward compatible and a raised floor.
- `tests/test_version_guard.py` checks what `check_version_guard.py` counts as shipped on the checkout: every file the Dockerfiles copy from the build context and every file each allowlist rule matches, found independently with `pathlib`'s glob, is shipped, and so are both Dockerfiles, both allowlists, `build_image.sh`, `lib/release_identity.sh` and `charts/pubdex/`; the release record, documents, tests and the suite's scripts are not. Patterns are read as Docker reads them, and an allowlist rule the guard cannot read stops it. On fixture trees with a fake Git, a `VERSION` that names no published PubDex release passes without Git, the record commit of a published one passes, and a changed or untracked shipped file fails and is listed; a missing commit, a directory that is no checkout and a failing comparison stop the guard with a message. The command line runs with a stub `git` on `PATH` and without `git`. `tests/test_config_dotenv.py` pins the opt-out itself on a `.env` file in a temporary directory: without it the file fills only missing values, with it nothing is read, also at import time.
- `tests/test_secrets_and_local_config.py` pins `../check_secrets_and_local_config.py`: each rule on synthetic positives assembled at run time; literal values that merely contain a `$` or start with a bracket, single-quoted shell text and URL passwords that are only partly references; Python constants at module level, in a class, annotated, as attributes or as bytes; Python literals passed as keyword arguments (also over several lines), stored as dictionary entries, written as literal-only f-strings, concatenations, `or` operands, conditional branches, parameter defaults, subscript targets, tuple unpacking or `:=` targets, assigned inside one-line `if`, `class`, `def` and `for` statements or after a semicolon, and given as the fallback of an environment or mapping lookup — each against a safe counterpart that obtains its value elsewhere; the literal fallbacks and alternate values of shell references (`${NAME:-value}`, `${NAME:=value}`, `${NAME-value}`, `${NAME:+value}`, nested) in scripts, env files, Compose files and URL passwords, next to empty, placeholder, command, reference, error-message and pattern-removal forms that stay accepted; runtime compositions such as `smoke-$RUN_ID` reported in Python strings, env files, YAML and JSON, where nothing expands them, while Python's whole-value placeholders stay accepted; the documented placeholders and the runtime compositions of shell code; the false-positive boundaries (Python expressions, comments, composed f-strings and docstrings, names that merely contain a secret word, URLs without a password, relative paths, DOIs and version numbers); the report format that never shows a matched value, deterministic output, the exit statuses, and that the candidate set comes from the publishable file list, with ignored and deleted files never read. For a commit range it runs the checker against a stub `git` that answers from a synthetic commit graph in Git's own output formats: a credential committed and removed again, or replaced by a placeholder, is reported in the commit that added it although the tree is clean; clean commits pass and are counted; commits the base contains are not read again; a path or link a commit adds is checked like a file; a merge is checked for what it changes itself, and nothing is reported twice; without a base, or with one the clone lacks, every reachable commit is read; a shallow clone, a missing object, an unknown head, a failing `git` and unreadable output each end the check with status 2, never with a pass, while tree findings are still reported; and an accepted finding covers its published commit alone. A last test reads this checkout's own `HEAD` with the real `git`, read-only, to prove that the parsers read what Git prints. It also proves that the checker and its own test corpus hold no finding.
- `tests/test_attribution_rules_cli.py` pins the command semantics of `people_pubs.sync.attribution_rules` offline: incompatible command/option combinations are rejected before a connection is opened, `--dry-run` never commits or refreshes, `recompute` never changes rules, `list` is read-only, and `--refresh-only` belongs to `recompute` alone.
- `tests/test_operator_bundles.py` runs `RunAllDayly.sh` and `RunAllWeekly.sh` against stub runners and proves one failing job does not stop the later ones while the bundle still exits non-zero; it also pins the retained job lists.
- The migration smoke also seeds synthetic internal/external authorships and
  OpenAlex location variants for `biblio.member_open_access_pdfs`: it verifies
  member-only selection, exclusion of OA rows without PDFs, deterministic
  fallback from `best_oa_location` to `locations[]`, rights-tier
  classification, and `app_readonly` access.
- `tests/integration/test_application_roles_integration.py` covers the
  database roles, which the rest of the integration suite — connected as
  the superuser, whom no missing grant stops — cannot. With `SET ROLE` it
  runs, as `app_writer`, the writes routine ingestion makes through the
  package's own helpers (a person, a publication and its rerun, an
  authorship, a name alias, and author emails through the `pii` SECURITY
  DEFINER functions) and reads them back through the documented views; as
  `app_readonly` the reads reporting makes; and as `maintenance` the
  maintenance of personal data — finding a `pii.people_pii` row by its
  primary or listed address written in another case, correcting it in
  place and deleting an ORCID address the same way. Each role is refused
  what the model withholds from it: the `pii` tables and DDL for
  `app_writer`; any write, personal data and the `pii` functions for
  `app_readonly`; the application schemas, the `pii` functions and DDL for
  `maintenance`. The module also proves that all three roles find the
  extension types and operators the application uses — a value cast to
  `citext`, and `citext` columns comparing case-insensitively as they do for
  the superuser — and sweeps every table and view for the grants the model
  gives each role, including `USAGE` but never `CREATE` on schema `public`,
  which holds the extensions. Without that `USAGE`, which `init/000_init.sql`
  and the runtime patch grant, a role cannot name `citext` — the
  application's calls of the `pii` email functions fail with `type "citext"
  does not exist` — and its `citext` comparisons fall back silently to
  case-sensitive text. The disposable runner repeats the module after
  re-applying the runtime patch, which re-creates views and re-issues
  grants.
- The read service, `people_pubs.api`, is covered offline and on the
  disposable database. `tests/test_api_app.py` serves the application in
  process through httpx's ASGI transport, without a socket, from a stand-in
  data source, and pins its response contract: liveness without the
  database; readiness and its two reasons; pages with exactly the export's
  fields in `pub_id` order; `next_after` across pages and on the last one;
  the empty result; the accepted bounds of `limit` and `after`; every
  invalid, unknown or repeated parameter refused before the database is
  asked; 503 and 500 responses with fixed messages that repeat nothing of the
  failure; 404 for any other path; 405 with `Allow` for every write method;
  and the lifespan that opens and closes the data source.
  `tests/test_api_settings.py` pins the configuration: the defaults and every
  setting; ingestion and libpq settings not standing in for the service's
  own; connection strings that do not parse, carry a password or name no
  user; password files used as written, punctuation and spaces included, and
  every kind of unusable file refused, no refusal repeating the value.
  `tests/test_api_server.py` pins the database layer without a database —
  one `SELECT` of the export's columns from the snapshot, the page bounds as
  parameters, failures sorted into the two reasons, a translated failure that
  carries no database text, read-only sessions and the statement timeout
  placed after any options of the connection string, the password kept out
  of the pool's connection string, an empty, bounded pool that opens its
  connections with exactly those settings — and the process:
  the password masked in log messages, arguments and tracebacks; libpq
  settings removed from the environment; the server configuration; no
  ingestion, configuration or provider module imported; and the real
  `python -m people_pubs.api` on a Unix-domain socket in a temporary
  directory, which the network guard treats as local, while the child
  process stays under the guard: start without a database, liveness, a
  graceful stop on SIGTERM and on SIGINT (status 130, socket removed),
  status 2 for a configuration error without its value, and status 3 for an
  address in use and for a socket path taken by a regular file, which stays.
  `tests/test_connection_security.py` pins the authentication every
  connection requires (`people_pubs.connection_security`): SCRAM and no
  GSSAPI encryption for the ingestion commands, unless the connection string
  or `PGREQUIREAUTH` and `PGGSSENCMODE` say otherwise; for the read service
  SCRAM with a password file, no authentication without one, and only its
  connection string can choose otherwise. It then connects the installed
  libpq, as each of them, to a stand-in server on a Unix-domain socket in a
  temporary directory, the only place the network guard lets these tests
  reach: a request for GSSAPI, SSPI, a cleartext or an MD5 password, and a
  server that skips authentication although a password is configured, end
  the connection with libpq's refusal before anything is sent in answer, no
  password and no GSSAPI token; a SCRAM request is answered with SCRAM and
  never the password; the read service without a password connects only to
  a server that asks for nothing and refuses SCRAM, GSSAPI and MD5 requests;
  and a method the configuration names, through the connection string or
  the environment, is used. On the disposable database,
  `tests/integration/test_connection_security_integration.py` shows that an
  ingestion connection and a read-service session are authenticated by SCRAM
  (`SYSTEM_USER`), with those options in effect and without GSSAPI.
  On the disposable database, `tests/integration/test_api_integration.py`
  runs the demonstration's roster import, fixture ingestion and attribution
  — once as the superuser and once as `app_writer`, snapshot refresh
  included — and reads the resulting publication through the service as
  `app_readonly`: every field, and the external co-author, the affiliation,
  the payloads and the roster's email address withheld. It pages through the snapshot with
  several limits, gets an empty page after the last publication, proves the
  sessions read-only even for a superuser, reports a database that does not
  exist, a role without access to the snapshot and an unpopulated snapshot,
  ready again once refreshed, replaces connections the server terminated
  without a failed request, and goes not-ready while the database refuses
  connections and ready again afterwards. `tests/test_api_probe.py` pins
  the container health check `people_pubs.api.probe`: 200 with the expected
  status passes, every other answer fails with the service's reason and
  nothing more of its answer, an absent service fails without a traceback,
  the port and socket settings are read as the service reads them, and the
  real service process answers `--live`; readiness against a database is
  left to the integration tests, since the network guard refuses any
  database connection offline.
- The application image and the Compose deployment are covered offline by
  their files and scripts, run for real against stubs. `tests/test_password_files.py`
  pins the one password-file format of the read service, the ingestion
  commands, the role-password script and the secret initialiser, with shared
  samples of usable and unusable files, and `PEOPLE_DB_PASSWORD_FILE`: it
  supplies `PGPASSWORD`, wins over `DB/.env`, conflicts with a different
  `PGPASSWORD`, and stops a command with status 2 naming the variable, never
  the file or a value. `tests/test_role_passwords_script.py` runs
  `init/020_role_passwords.sh` against stub `psql` and `setsid`: nothing
  changes without its variables, half a configuration, an unusable file, two
  roles with one password or a role with the superuser's is refused before
  any change, every usable sample reaches psql exactly and only through its
  input, never on a command line or in the output, `maintenance` loses its
  login on a first start, a failing psql shows a masked error, and the
  script works sourced as well as executed. For `--stdin` (stub `hostname`
  as well): the three passwords come from standard input and from no file,
  each line is checked before any change — format, role, duplicates,
  missing roles, a NUL byte, the size, a terminal — each password is set and
  then tried on a new TCP connection to the container's own address, which
  a refused login or a trusted one (no `scram-sha-256` in `SYSTEM_USER`)
  fails, and `maintenance` is left alone; `--resync` is gone.
  `tests/test_set_passwords_script.py` runs `../set_passwords.sh` with the
  real `create_secrets.sh` against a stub `docker`: the files checked first,
  then that PostgreSQL runs, then that new `api` and `ingest` containers read
  the host's bytes, and only then the three passwords, as the containers
  read the files, on the standard input of `docker compose exec`; every
  refusal before that changes nothing, a failure or a missing confirmation
  fails the run with the output masked, `docker compose` runs in the
  directory of `compose.yaml` with the operator's `-p`, `-f` and
  `--env-file` and nothing else, and no password reaches a command line, an
  environment or the output.
  `tests/test_create_secrets_script.py` runs `create_secrets.sh` in a
  scratch root: an owner-only directory, three random files readable for the
  containers, every existing file kept byte for byte, only missing ones
  created, a directory open to others refused, unusable and shared files
  reported, links never followed, the directory found as Compose finds it,
  and no password printed. `tests/test_build_image_script.py` runs
  `build_image.sh` against stub `docker` and `git`: the PostgreSQL image's
  `postgres.Dockerfile` built first, then the application image's
  Dockerfile, both with the checkout as context and the same labels' values
  as build arguments — the transform version for the application image
  alone — no attestation or Git metadata of buildx's own, BuildKit and never
  the legacy builder — no buildx plugin stops it with what to install — the
  images reported through their tags, as every image store accepts them,
  `<commit>-dirty` — also for an ignored Python module of the package, which
  the build context takes in — and `unknown` revisions, the release tag
  refused for a modified working tree, for either image, a bad `VERSION` and
  a failed build stopping it before the next, and that Git is only asked:
  `rev-parse`, `ls-files` and `status` with `--no-optional-locks`, so that
  nothing under `.git/` is written; a test release's version
  (`--version <VERSION>-test.<name>`, no other form, and only for the
  checkout's `VERSION` without a pre-release part) labelling both images and
  keeping the release version off their tags, and a stated revision
  (`--revision <commit>-dirty`) accepted only with it and only outside a Git
  checkout. `tests/test_release_script.py` runs `../release.sh` from a
  scratch checkout against stub `git`, `docker`, helm and `build_image.sh`,
  with the real tar, gzip, sha256sum and `DB/release_provenance.py`: usage
  errors; a missing SBOM tool, one with another image id or one reporting
  another version refused before anything is built, and setup pulling
  exactly the pinned reference and checking it; the SBOMs written by the
  pinned Syft from the saved images, with no network, the calling user and
  a read-only root file system, and every other container a probe of a
  release image with no network, an unprivileged user and a read-only root
  file system; the provenance record written from the build, covered by
  the checksum file and naming nothing of the machine; a PubDex release
  refused from a checkout with changes, without its annotated tag
  `v<version>`, with a lightweight tag or one at another commit, a version
  reserved for test releases in `VERSION`, a chart of another version and an
  output directory in the way, each before anything is built; a failed image
  build leaving no output directory; the artifact names, the checksum file's
  format and `IMAGES`; a source archive that holds the checkout's files — for
  a test release also the untracked files Git does not ignore — under
  `pubdex-<version>/` with owner 0, the commit's time and modes 644 and 755,
  and is the same, byte for byte, when built again with other file times;
  a test release with the test-only overlay `b1` or `b2` built from a copy
  of the checkout, uncommitted and untracked files included, whose source
  archive holds the overlay's schema changes numbered after the last one
  and the transform version with its suffix, whose images the copy's
  `build_image.sh` builds with the revision `<commit>-dirty` and no Git
  checkout above it, where every path keeps the checkout's mode, while the
  working tree stays untouched; verify checking such a directory against
  the same copy and refusing it without the overlay or with the other one,
  and refusing the overlay for a PubDex release; an overlay that lacks a
  template, holds no suffix, or finds no schema change to number after
  stopping before anything is built; and both templates following the
  rules of a schema change, the backward-compatible one only adding;
  helm run with a cleared environment, with `SOURCE_DATE_EPOCH` for the
  package alone; verify refusing a changed file, an extra or unlisted file,
  an archive that is not the checkout's, a moved tag, another revision, a
  chart package of another version, a failing lint, a missing SBOM, an SBOM
  without a locked package, a provenance record that names another base
  image, and an image that reports another PostgreSQL release; publish refusing a
  test release, a published or unverified directory, a checkout that is not
  the tagged commit, a version either repository holds as another image or
  as one whose digest the verified image does not record, any other answer
  of the registry but its explicit "manifest unknown", a digest it does not
  give, and a local tag that names another image, each before any push, then
  pushing the PostgreSQL image before the application image, writing both
  digests and stopping after a failed push; a second publish completing an
  interrupted publication, pushing only what the registry does not hold
  yet, against a stubbed registry that keeps what a push left; and that
  build and verify never push, tag for a registry, ask one or log in, and
  that nothing logs in, pushes the chart or scans for vulnerabilities.
  `tests/test_release_provenance.py` runs `release_provenance.py` on a
  fixture checkout, release directory and facts, without Docker or Git:
  every field of the provenance record from the checkout's files, the
  directory or the facts — the base images and PostgreSQL's source from the
  Dockerfiles, the locks and scripts from the stage `build-files` — and
  nothing of the machine in it; the check passing on what was written and
  failing, by the field's name, for a record changed in any section, a field
  added or missing, a checkout, image or file changed since, labels that
  disagree with the Dockerfiles and client libraries other than the runtime
  lock's; SBOMs of another format, tool, image or version, or without a
  locked package, Debian's `libpq5` or PostgreSQL, failing too; and the real
  Dockerfiles giving the inputs a real build's record names.
  `tests/test_release_rehearsal_script.py` runs `../run_release_rehearsal.sh`,
  stage 10, from a scratch checkout against stub `release.sh`, `docker`,
  helm and `git`, with the real python3, tar, sha256sum and cmp: usage
  errors; every refusal before anything is built — an unreachable daemon,
  an Engine or a platform the deployment does not support, no buildx, a
  missing SBOM tool (with `./release.sh setup` as the command) or one with
  another image id, a missing pinned helm, one of another hash or one
  reporting another version, a Docker context with TLS files, a `VERSION`
  with a pre-release part and no Git checkout — each leaving nothing behind;
  the steps in order, with one test release version of the run's own for
  both builds and every output in the run's temporary directory; every
  Docker command after the first, `release.sh`'s included, with the run's
  own client configuration, empty or naming only the caller's plugin
  directory, and without the caller's context or `DOCKER_AUTH_CONFIG`; the comparison failing for a changed byte of the
  source archive or the chart package, for other files, and for SBOMs, a
  provenance record or `IMAGES` that differ beyond what a build has to
  change; a failed build or verify stopping the run; the chart linted for
  both supported Kubernetes releases and rendered with each example and the
  test release's images, a failing lint, another image or another chart
  label failing the run; the cleanup removing every container and image of
  the run and nothing else, after success, after a failure and after
  SIGINT, SIGTERM or SIGHUP to the process group, a signal to the script
  alone taking effect when the build returns, and a cleanup that cannot be
  confirmed failing with the commands that find what is left; a credential
  in the run's client configuration failing the run; and that the script
  never runs `release.sh publish`, `docker push`, `tag` or `login`.
  `tests/test_upgrade_acceptance_script.py` runs
  `../run_upgrade_acceptance.sh`, stage 11, from a scratch checkout with the
  real helpers, release record and deployment files, against a stub
  `release.sh` and a stub `docker` that keeps an inventory of images,
  containers, networks and volumes with their labels: usage errors; every
  missing prerequisite — a `VERSION` with a pre-release part, the pinned
  helm, an unreachable daemon, an Engine, platform or Compose plugin the
  deployment does not support, no buildx, a missing SBOM tool, a release
  record without a floor or naming images without digests — stopping the
  run before anything exists; the three test releases built under names of
  the run's own, B1 and B2 with their overlays, into the run's temporary
  directory; the first stack's deployment files taken out of A's image
  with the command of `DEPLOYMENT.md`, its `.env` file alone configuring
  every Compose call in a cleared environment, and the run label on every
  service, volume and network; every Docker command after the first,
  `release.sh`'s included, with the run's own client configuration; a
  project that exists already never touched; a password in Compose's
  output masked; and the cleanup after a failed build, a failed start, a
  Compose that leaves resources behind and SIGINT, SIGTERM or SIGHUP,
  removing exactly what the run created, or naming what is left when it
  cannot confirm it. It also reads the script for the documented backup,
  restore and deployment-file commands, for no `publish`, `push`, `tag`,
  `login` or port 5432, and for `--no-deps` in the two commands that show
  the application's own check alone.
  `tests/test_container_files.py`
  pins the Dockerfile, `.dockerignore` and the Compose files: one base image
  for every stage that runs anything, by digest; the three locks and the four
  scripts that fetch and install them in a stage of their own, the only bind
  mount of the steps that use them, so that no other file of the build
  context invalidates those steps; a stage of its own that downloads every file of
  the pip, build and runtime locks with `fetch_verified.py`, and no other
  download and no other installation by pip; a stage that compiles psycopg-c from
  its verified archive with a compiler and libpq's headers, without network;
  in both Dockerfiles a stage after the compilation that lists the compiling
  stage's packages, which the image copies to
  `/usr/share/doc/<pubdex or pubdex-postgres>/build-packages.txt`, where
  `release.sh` reads it;
  the image's stage with Debian's libpq alone, no compiler, header or build
  tool, `PSYCOPG_IMPL=c`, and its one install step without network — pip by
  `install_pip.py`, the base image's setuptools removed, the runtime lock by
  `install_locked.py` with the wheel built in the other stage, `pip check`,
  and then the removal of pip and of ensurepip's wheels — with every step
  that runs pip or those scripts under `RUN --network=none` and no later
  step or command running pip; an allowlist context that holds exactly what
  is copied and the four scripts, which the build mounts and never copies;
  an unprivileged user that owns nothing but the bootstrap volume's empty
  mount point, COPY without options but that list's and one step after it that makes every
  file 644 and every directory 755, every file of `init/`, both Compose files
  and `create_secrets.sh` in the image; every entry of the four locks at an
  exact version with its hashes, the runtime lock's entries the same in the
  development lock and the build lock's in it too, no psycopg-binary,
  psycopg-c by its source archive's hash alone and the build lock exactly
  psycopg-c's build requirements; the locks at or past the releases that
  fix the advisories their earlier pins had, pip at or past the release whose
  copy of certifi is fixed, the GitHub workflow on the Python release of the
  base image, no credential, each service with its own role's secret only, a
  `compose.yaml` that needs no checkout — no bind mount, the release of
  `VERSION` as its default application and PostgreSQL images, the
  `bootstrap` service without network or secret, PostgreSQL started after it
  and mounting its volume read-only and with `nocopy` — loopback
  publication, no PostgreSQL port, no fixed
  resource name, ingestion only on request. `tests/test_bootstrap.py` runs
  `people_pubs.bootstrap` on scratch directories: the target ends up holding
  exactly the files of `init/`, byte for byte, scripts 755 and the rest 644
  whatever the source's modes and the umask; files of an earlier image and
  remains of an interrupted copy are removed; a subdirectory, a source
  without SQL and a directory it may not write fail with the path and, for
  the last, the owner the volume needs. On the disposable database,
  `tests/integration/test_application_roles_integration.py` proves that
  `app_writer` refreshes the serving snapshots through
  `biblio.refresh_serving_snapshots()` — a temporary table of the same name
  taking no part — and cannot refresh, alter or drop them directly, that no
  other role may execute the function, and that it runs as the snapshots'
  owner with a fixed `search_path`. The same `test_container_files.py`
  pins `postgres.Dockerfile`: its Python, Debian and entrypoint images by
  digest, the Python image the application's, the PostgreSQL source
  downloaded by `fetch_verified.py` over HTTPS by the SHA-256 it names, the
  compilation under `RUN --network=none` with `--without-libxml` and
  `--without-libxslt` and no option taking XML in, the extensions `init/`
  creates checked in the build and `xml2` absent, the image's own checks
  that no XML library is installed or linked and that the entrypoint scripts
  are those of the digest, the entrypoint, signal, volume, user and socket of
  the official image, the notices of PostgreSQL's licence and of the
  entrypoint scripts' MIT licence — that text kept in the repository as
  upstream has it at a fixed commit, its SHA-256 checked by the test and in
  the build, with no download from any forge — and a build context of the
  downloader and that notice alone, the notice the only file of the checkout
  the image carries.
- `tests/test_stack_acceptance_script.py` runs `../run_stack_acceptance.sh` offline, from a scratch checkout with the real `create_secrets.sh` and disposable-database library, against a stub `docker` that keeps an inventory of the containers, networks, volumes and images it creates and logs every call with the environment of each Compose call, and stub `git` and `build_image.sh`. It proves that invalid arguments and missing prerequisites — no daemon, no Compose plugin, Compose older than 2.24.0 (2.20.1, 2.20.2, 2.23.3 and a 2.24.0 pre-release among them) or of no recognisable version, an Engine older than 28.0.1 (27.5.1, 25.0.5 and 28.0.0 among them), one of 28.2.0 to 28.3.2 or of no recognisable version, for a build or a given image alike — stop the run before anything exists, with what to install, while 28.0.1, 28.0.4, 28.3.3 and 29.9.2 go on to start the stack; that a project that exists already is never touched, not even by the cleanup; that a given image is never built, replaced or removed, is pulled by its exact reference with `--pull`, and is refused for a digest, version or revision other than the one asked for, and for a version or revision label that is missing, `unknown`, malformed or a `-dirty` build of the required commit, before anything starts; that identity options that cannot be checked — a version or abbreviated commit id that is not one, `--release` without `--image`, `--expect-version` or `--expect-revision`, with a `-dirty` revision, a tag naming another version or `--pull` without a digest — are usage errors that ask nothing of Docker or Git; that `--release` starts the `compose.yaml` the image carries, whatever the checkout holds and with no Git at all, by digest after a pull and before the push, and stops before the start when that file names another version, no default image or no `schema-upgrade` service; that an image carrying no `compose.yaml` is refused before the start; that the image's schema changes are listed as user 10001 in a container without network, that an image without any is refused before the start, and a build carrying other ones than the checkout's `DB/schema/`; that the image's files are checked as user 10001, in a container without network, from a program on standard input, and an image whose files are not root's with modes 755 and 644 is refused before the start; that the image's Python packages are then read the same way with `importlib.metadata` and no pip, by the program the script sends, which the stub runs on a synthetic image's file system with a Python of its own that holds no pip and none of the host's packages: the runtime lock's packages at their versions pass and are named with the base image's others, while a package that is missing or at another version, a lock that pins nothing, an importable pip, and a file named as pip, its scripts, its distribution or a wheel each stop the run before the start with the package or path named, and files whose names only resemble those, or that are under `/proc`, `/sys` and `/dev`, are not reported; that a development run starts this checkout's `compose.yaml` and says when the image carries another, a local `<commit>-dirty` build included; that a build carrying another `compose.yaml` than the checkout's is refused; that Git, asked only for a build, is only asked — `rev-parse`, `ls-files` and `status` with `--no-optional-locks` — and runs with none of the caller's `GIT_*` settings; that a build is this checkout's under a tag of the run, labelled with the commit (`-dirty` for a modified tree, or one with an ignored Python module of the package) and the checkout's `VERSION`, and that tag removed afterwards; that every Compose call names the run's project, a project directory of the run's own holding `compose.yaml` alone, both Compose files and the environment file, with an environment of `PATH`, `HOME` and the daemon selection only, whatever the caller exports — `COMPOSE_*`, `PEOPLE_PUBS_*`, `PG*` and an exported shell function standing in for `docker` included — and that the build runs in the same cleared environment; that the run's settings file holds exactly the documented keys, a loopback address and a port Docker picks; that no password is on a command line, in a Compose call's environment or in the output unmasked; that a rendered configuration holding a password, naming another project, giving the read service the superuser's password file or starting it before the schema upgrade has succeeded stops the run before the start; and that a failure after the start, a `docker compose down` that fails, a signal (SIGINT, SIGTERM, SIGHUP), a second interrupt during the cleanup, and a cleanup that cannot be confirmed — resources, the image tag, the temporary directory — each end the run with the right status, report what is left with the commands that find it, and otherwise leave nothing. It also runs the shared `foreign_mounts` check against a stub that lists mounts, a bind's empty name included, and expects exactly the binds and volumes that are not the project's volumes or its secret files. The PostgreSQL image is checked as the application image is: built with it under a tag of the run and removed with it, given with `--postgres-image` or else the one the started `compose.yaml` names, never built or pulled in place of a missing one, refused when its revision is another commit's or a `-dirty` build of the required one, when it carries no `pubdex.postgresql-version` label — the official image, for one — or when the rendered configuration would start another; `--release` with `--pull` needs its digest too, and its version tag has to be the release's. That SQL/XML is unavailable in the running server is checked against real Docker. The successful path runs against real Docker as stage 6; `tests/test_container_files.py` pins that the acceptance addition to `compose.yaml` changes nothing but labels and the internal network.
- `tests/test_operator_acceptance_script.py` runs `../run_operator_acceptance.sh` offline, from a scratch checkout with the real `create_secrets.sh`, `compose.yaml`, `init/` and helper libraries, against a stub `docker` that keeps an inventory of the containers, networks and volumes it creates, logs every call with the environment and working directory of each Compose call, and — as Compose does — takes the project's name, image, port and secrets directory from the `.env` file of that working directory. It proves that invalid arguments and missing prerequisites stop the run before anything exists; that a project of its name that exists already is never touched; that a given image Docker lacks is never built or pulled; that the operator's directory holds exactly the checkout's deployment files, byte for byte and mode for mode, and the `.env` file, which names the run's project, its image tag, a relative secrets directory outside the directory — where `create_secrets.sh` writes the files — a port of its own and the log level; that every Compose call runs in that directory with no option naming the project, files or settings and an environment of `PATH`, `HOME` and the daemon selection only, whatever the caller exports or the checkout's own `.env`, `compose.override.yaml` and `secrets/` hold; that no password is on a command line, in a Compose call's environment or in the output unmasked; that a rendered configuration holding a password, or naming another project, stops the run before the start; and that a failure after the start, a `docker compose down` that fails, a failed build, a signal (SIGINT, SIGTERM, SIGHUP), a second interrupt during the cleanup and a cleanup that cannot be confirmed each end the run with the right status, report what is left, and otherwise leave nothing — the operator's directory, the secret files and the roster included. The steps after the start run against real Docker as stage 7; offline, one more test holds the `docker compose` commands and the verification query of `../DEPLOYMENT.md`, *Backup and restore*, to those step 8 runs, so that the documented procedure and the tested one cannot drift apart, and two hold that step 8 checks the schema upgrade after the restore and that the login check starts no schema upgrade.
- `tests/test_docker_versions.py` sources `lib/docker_versions.sh`, which `../run_ci_suite.sh`, `../run_stack_acceptance.sh` and `../run_operator_acceptance.sh` use for their version checks, and pins how it compares the versions Docker and Compose report with the minimums: 2.24.0, the first Compose release that passes the health checks' `start_interval` to the container, a `v` prefix, a distribution's build metadata, later pre-releases and later major versions (Compose 5) accepted; 2.20.1 and older, which reject `start_interval`, 2.20.2 to 2.23.3, which accept it and leave it out, and a 2.24.0 pre-release refused; empty, `dev`, two- or four-part and other unrecognisable versions reported as such; Debian 13's `2.26.1-4` and Ubuntu 24.04's Compose releases accepted; one Engine requirement, to build and to run, 28.0.1 or later except 28.2.0 to 28.3.2: every release before 28.0.1 refused, the last of the 25.0 and 27.x lines, 28.0.0, Debian 13's `docker.io` 26.1.5 and Ubuntu 24.04's original 24.0.7 among them, as are 28.2.0 to 28.3.2 and a pre-release of 28.3.3, while 28.0.1, GitHub's runner's 28.0.4, 28.3.3, Debian's testing, Ubuntu 24.04's updated `docker.io` and later releases are accepted; that `compose.yaml` still sets `start_interval`; that the documents stating the Compose minimum all state the same one, and those stating the Engine requirement the same one with its exclusion; and that both acceptance tests report their check of the port bindings as configuration, not as a connection from another host.
- `tests/test_setup_cluster_tools_script.py` sources `lib/cluster_tools.sh`, the pinned helm, kubectl and kind for the Kubernetes checks, and pins its form — each tool's linux/amd64 download from the address its project documents, SHA-256 and size limit, the one member taken from helm's archive, and the kind node image by digest within kubectl's version skew — and runs `../setup_cluster_tools.sh`, which installs them under `.tools/`, offline from a scratch checkout with a stub `fetch_verified.py` serving synthetic tools and a stub `uname`: it installs exactly the three files and writes nothing else, runs each with a cleared environment to compare its reported version with the pin, checks files already in place by hash instead of downloading them and refuses one that changed, and refuses a missing or malformed pin, a pin from the environment, any platform but Linux on x86_64, a failed or mismatched download, a symbolic link under `.tools/`, a tool reporting another version, and a helm archive whose member is missing, a link, a directory, under another or a duplicate name, too large or of another hash, while no other member is ever written; it also checks that no script names a GitHub address and that `.tools/` is ignored and outside the image's build context.
- `tests/test_chart_checks_script.py` runs `../run_chart_checks.sh`, stage 8, offline from a scratch checkout in which helm is a recording stub whose SHA-256 the copied pin file carries and `.venv/bin/python` records the pytest call: helm's hash and version checked before it runs anything, and a missing helm, one of another hash or version, a missing `.venv` or missing requirements refused with the command that fixes them; `helm lint --strict` with the chart's own values, every values file of the checks and every example, for Kubernetes 1.35.0 and 1.37.0, then pytest on `charts/tests`; every helm and pytest call with a cleared environment whose home, temporary, helm and Docker configuration directories and kubeconfig path lie in one temporary directory under the caller's `TMPDIR`, no `KUBECONFIG`, `HELM_*` or `PYTEST_ADDOPTS` of the caller's reaching them; nothing written into the checkout, and the temporary directory removed after a pass, a failed lint, which stops the run before the tests, failed tests and a terminal interrupt; and usage errors. It pins the Kubernetes range to the floor of `kubeVersion` in `Chart.yaml` and the release of the pinned kind node image, and shows that stage 1 does not collect the chart tests, and that they fail, never skip, without the pinned helm or with one of another hash.
- `tests/test_cluster_acceptance_script.py` runs `../run_cluster_acceptance.sh`, stage 9, offline from a scratch checkout in which `docker`, `uname`, `build_image.sh`, `release.sh` and the pinned helm, kubectl and kind are recording stubs that keep an inventory of what they were asked to create, the stub `docker` answers `crictl` in the node and lists images by label, the stub `release.sh` writes a test release's directory and tags its two images, and the copied pin file carries the stubs' SHA-256: usage errors; every refused prerequisite — another system or architecture, an unsupported Engine or daemon platform, an old Compose plugin, a missing tool, one of another hash or version, a missing `.venv`, `openssl`, SBOM tool or buildx plugin, a `VERSION` with a pre-release part, a release record without the preceding PubDex release or with a malformed entry, a clone without the commit of that release's chart — refused before anything is created, and a chart of another version than the record names at that commit refused before any cluster exists; revision 1 installed from the preceding release's chart, which a stub of the repository's history gives for the commit the release record names, with the record's published images by digest, which the node pulls without credentials while only the images under test are loaded; the dry runs of revision 1, of this checkout's chart with the upgrade's values and of the three files that no step installs, before the install; a failed pull ending the run with a message that names the registry, before any helm call and with the cleanup confirmed; given images that are the published ones, and images under test with a published image's id, refused; a kubeconfig whose server is another address or another loopback port, that holds a second context, or a control plane published beyond loopback, refused before any kubectl or helm call that could reach a cluster, with the run's cluster, network and tags removed all the same; every kubectl and helm call naming the run's kubeconfig and context, with the run's `HOME`, cache, helm and Docker configuration directories and none of a polluted caller's `KUBECONFIG`, `HELM_*` or `KUBECACHEDIR`, kind running with the run's network and the caller's Docker client configuration, and the caller's `~/.kube/config` untouched; the cleanup deleting the cluster before the network and the network before the image tags, leaving a network named `kind` and another cluster alone, and failing with the commands that find what it could not remove; SIGTERM, SIGINT and SIGHUP during the install ending the run with 143, 130 and 129 after the cleanup; the fallback to an image archive when `kind load docker-image` fails; given images tagged, never built, their labels checked, and left in place; test releases A and B1 built by `release.sh` into the run's directory before the cluster exists, both images of each loaded after the images under test, a failed build, a test release of other schema changes than the checkout's (A) or than those and one more (B1), and two of one transform version stopping the run, and their images removed by tag and, a dangling one included, by version label. From the script's text it checks that the step numbers and the header agree; that the floor starts only after the first cluster is gone, that no published image reaches its node before the install, and that its operator's Role grants exactly the access rights `../KUBERNETES.md` lists; that in external mode, before the test releases, only the password change's upgrade has the admin Secret, the routine upgrade alone switches the setup off, the rollback returns to revision 1 and its published images, the routine upgrade, the rollback and the uninstall each show from the API server's replayed watch that they created nothing, while the same replay around the password change's upgrade has to find the setup's Job, ConfigMap and ServiceAccount and the schema upgrade's Job and ServiceAccount, and that this replay names what was created and fails, never passes, on an error; and that steps 22 to 26 empty the server before A installs from its chart package file with both images set explicitly, try B1 as a routine upgrade with the short timeout, the same upgrade as step 24's but with the setup switched off, tolerating only Helm's own failure line among warnings, show why it cannot complete, create the admin Secret only around step 24's upgrade, and roll back to A without it. It runs the check of B1's not-ready answer on sample answers.
- `tests/test_kubernetes_guide.py` keeps `../KUBERNETES.md` in agreement with the chart and with stage 9, offline: the guide's appendix has one row for every block of shell commands, in order, matching the block's section and text; each step of `../run_cluster_acceptance.sh` a row names exists and, read with the functions it calls, runs one of the block's kubectl and helm commands, and the steps together run all of them with every long option; each stage 8 test a row names exists; and the guide's table of values names every key of `../charts/pubdex/values.yaml`.
- `tests/test_deployment_guide.py` keeps `../DEPLOYMENT.md`, *Upgrading to a new release*, in agreement with stage 11, offline: the section's table has one row for every block of shell commands, in order, matching the block's text; each step of `../run_upgrade_acceptance.sh` a row names exists, is one the testing strategy numbers, and, read with the functions it calls, runs one of the block's commands; and every `docker` command of the block is one those steps run, its words in order on one command line. Every word of every command, altered once, is found.
- `tests/test_releasing_guide.py` keeps `../RELEASING.md` in agreement with the tree and with stage 10, offline: its table has one row for every block of shell commands, in order, matching the block's text, saying which stage ran it or that only a real PubDex release does, and the release-only rows that publish name the stubbed tests that cover them; each step of `../run_release_rehearsal.sh` a row names exists, is one the testing strategy numbers, and runs the block's `release.sh` command, every option that differs between the two named in the row; each script, file and option a block names exists; and the rows naming stage 1 name a program `run_checks.sh` runs or a test module that runs it.
- `tests/test_external_setup_script.py` runs `../charts/pubdex/files/external-setup.sh`, the preparation of a database on an operator's PostgreSQL server for the chart's external mode, against stub `psql` and `setsid` executables that answer its read-only query with the facts a test gives and record every call. It shows that the run stops before any change, naming every reason at once, for a server that is not PostgreSQL 16, an admin role without CREATEROLE or ownership, part of the schema, the roles of another installation, a missing ADMIN OPTION and extensions that cannot be created, and before the server is contacted for unusable or shared password files; that a database without the schema gets the init files in one transaction, in name order, with exactly three lines of `init/000_init.sql` left out, and an initialised one skips that step; that the two role passwords reach psql only on its input and the admin's only through a libpq password file of mode 0600 that is removed afterwards, none on a command line, in an environment or in the output; that each role has to log in again by SCRAM; and that every connection, the admin's three included, requires SCRAM (`PGREQUIREAUTH=scram-sha-256`): a server that asks any of them for a cleartext password, which the stub answers as libpq does, gets none and the run ends there, and an inherited `PGREQUIREAUTH` other than `scram-sha-256` stops the run before the server is contacted.
- Integration tests live under `tests/integration/` and are skipped unless `PEOPLE_PUBS_INTEGRATION_DSN` or `PEOPLE_DB_TEST_DSN` points at a disposable PostgreSQL database; `./run_disposable_integration.sh` provides one and runs them all, while `./run_tests.sh`, and with it `../run_checks.sh`, never passes an inherited value of either variable on, so there they always skip.

## What is intentionally not faked in this pass

- No live API tests.
- No tests against production backups or real people data.
- No pretend DB integration tests that mock transaction semantics.

## DB-container idempotency tests

Run these against a disposable PostgreSQL container or a temporary restored database, not the shared live DB.

1. Seed a tiny synthetic dataset that exercises DOI-based identity, authorship matching, alias writes, author-email writes, and refresh-state rows.
2. Run the importer once.
3. Snapshot relevant table counts and identity keys.
4. Run the exact same importer again with the same inputs.
5. Assert that row counts and keys are stable for `biblio.publications`, `biblio.authorships`, alias tables, author-email tables, refresh-state tables, and any correction-log table touched by that importer.
6. Assert that `source_of_truth`, source-specific `raw_json`, canonical DOI values, curated authorship reads, and durable dedup decisions remain stable after the second run.

Priority scenarios, all implemented (see the dated records below):

- `people_pubs.sync.orcid_works` on repeated ORCID work pulls for one person.
- `people_pubs.sync.crossref_backfill` on a DOI collision that triggers merge logic.
- `people_pubs.sync.manual_csv_dois` on a CSV rerun with the same DOI rows.
- `people_pubs.sync.refresh_state_runner` in non-dry-run mode, verifying state rows update once per item without duplicate inserts.
- `people_pubs.sync.publication_dedup_overrides` on a synthetic preprint/final-version choice, then delete/reinsert the publications with new `pub_id`s and verify `biblio.publications_clean` reapplies the durable decision.
- the curator-correction path on a disposable DB: apply an authorship attach/detach
  override, then rerun the relevant ingest and verify `biblio.authorships_curated`
  still exposes the corrected person linkage.
- `biblio.publications_canon_web`: continuously read the materialized snapshot
  while `REFRESH MATERIALIZED VIEW CONCURRENTLY` runs, then verify a failed
  refresh leaves the prior populated snapshot readable.

Built so far (2026-07-04): durable dedup reset, `upsert_publication` ON-CONFLICT
merge/idempotency/repair, importer rerun idempotency for `orcid_works`,
`crossref_backfill` (including the DOI-collision merge), `manual_csv_dois`,
`refresh_state_runner` orchestration (state rows once per item, failing job
does not stop later jobs, hung child killed by the per-job timeout), and
`publications_canon_web` concurrent refresh incl. failed-refresh behavior.

Added 2026-07-07: `openalex_backfill` rerun idempotency
incl. last-fallback field gating and skipped authorship refresh under a more
trusted source (`tests/integration/test_importer_idempotency_integration.py`);
alias backfill + authorship relinking with curated-view force_null handling
and rerun stability (`tests/integration/test_alias_relink_integration.py`);
and the formerly open curator-correction scenario — `people_pubs.curation_admin`
driven against a disposable DB, proving alias/email/attach/detach/dedup writes
persist with their audit trail and that a curator detach survives relinking via
the `biblio.authorships_curated` overlay
(`tests/integration/test_curation_admin_integration.py`). Offline fixture tests
cover every source module (orcid, crossref, openalex, datacite, semantic
scholar, dblp — `tests/test_*_fixture_ingest.py`) plus the
source-coverage report golden (`tests/test_source_coverage_report.py`).

Added 2026-09-08: configurable institutional attribution
(`tests/integration/test_institution_attribution_integration.py`: contract,
command end to end, `--dry-run` leaving rules, flags and snapshots untouched,
a failed refresh rolling both snapshots back together while the committed
rule change survives, and `recompute` re-syncing flags without touching
rules) and its coordination with concurrent ingestion
(`tests/integration/test_attribution_concurrency_integration.py`: two
connections drive inserts, payload updates and rule edits that overlap a rule
change in both directions — attribution added and removed — observing the
blocked side through `pg_stat_activity` rather than timing, then checking
every cached flag against the signal function; plus the real command against
an in-flight writer and the `READ COMMITTED` guard).

Added 2026-10-05: rerun tests for the remaining importers,
`datacite_backfill`, `dblp_backfill` and `semantic_scholar_backfill`
(`tests/integration/test_backfill_rerun_integration.py`). Each runs through
its real database-writing path on the disposable database — selection,
in-place update, provenance merge, authorship refresh, alias writes and
commits — with only `httpx.Client.get` answering from the golden fixtures, so
the package's own provider clients still build their requests and parse the
responses. Every test seeds a Crossref-backed and an ORCID-only publication
and an internal person carrying the fixture author's ORCID. The regression
test of each importer pins what holds however the precedence question below
is decided: the first import (the values the ORCID-only row lacked filled in,
a value the provider does not supply kept, DOIs left as they were,
cumulative `source_of_truth`, the other sources' raw payloads and envelope
hashes kept, the authorships and aliases written: two linked or unlinked
creators from DataCite, the affiliated author alone from Semantic Scholar,
none from DBLP, whose author records carry no affiliation), a forced rerun
that changes no identity, count or stored value except fetch timestamps, and
an unforced rerun that selects nothing and sends no request.

### Characterization of the enrichment precedence

A separate test per importer, marked `characterization`, pins the current
field precedence without approving it: a title or year the provider supplies
replaces the stored one even when a source ranked higher in
`people_pubs.config.TRUST_ORDER_PUBLICATIONS` supplied it — Crossref for all
three importers, and ORCID as well for DBLP, which ranks below it. That is
how `crossref_backfill` was written to behave, and the other three follow its
structure; the only documented constraint is the one on OpenAlex, which may
fill missing fields but never replaces metadata a more trusted source backed
([`SCRIPTS.md`](SCRIPTS.md), *OpenAlex backfill*). Whether DataCite, DBLP and
Semantic Scholar should defer to the trust order as well is an **open
decision**, not settled by these tests: they make sure the behaviour changes
only deliberately, together with them, and pytest's `-m characterization`
selects them. What they do not establish is that the current precedence is right, or
that the Crossref and ORCID values are; they establish only what the
importers do with the fixtures. *Field precedence of the enrichment
backfills* in [`SCRIPTS.md`](SCRIPTS.md) states the current behaviour for
operators.

## Running the integration suite in a disposable container

`./run_disposable_integration.sh` (stage 5 of `../run_ci_suite.sh`) runs the
complete integration suite against a PostgreSQL it starts for that run alone,
and needs no existing database:

1. It starts a uniquely named container and data volume, both labelled
   `pubdex.integration.run=<run id>`, with `init/` mounted read-only and the
   server published only on a loopback port that Docker picks. A name that is
   already taken stops the run instead of being taken over.
2. It waits until the entrypoint has logged `PostgreSQL init process complete`,
   every `init/*.sql` script has run and `pg_isready` answers for `people_db`.
   The wait gives up after `INTEGRATION_WAIT_SECONDS` (default 120), and at
   once if the container exits, an init script reports an error, or the
   entrypoint completed without running `init/` (the daemon could not see the
   checkout); either way it prints the container's log tail, with the
   password masked. It then prints the image digest and server version it
   ran against.
3. It applies the schema upgrade, `python -m people_pubs.schema upgrade`, in
   the environment of step 4, its output masked; a failed schema upgrade ends
   the run with its status before any test runs, so the tests always run on
   the baseline plus every schema change, as on a deployment.
4. It runs `./run_integration_tests.sh` in an environment from which every
   exported variable and function has been removed and in which
   `PEOPLE_PUBS_INTEGRATION_DSN`, `PEOPLE_DB_TEST_DSN`, `PEOPLE_DB_DSN` and the
   libpq settings (`PGHOST`, `PGHOSTADDR`, `PGPORT`, `PGUSER`, `PGPASSWORD`,
   `PGDATABASE`, with the service and password files pointing nowhere) all
   name the disposable database, and `PEOPLE_PUBS_SKIP_DOTENV=1` keeps
   `DB/.env` unread, so no inherited variable or local file can direct a test
   elsewhere. The shell exports these values itself before it `exec`s the
   command, so none of them is ever on a command line. Extra arguments go to
   pytest; `PYTEST_ADDOPTS` from the caller does not.
5. It re-applies `init/010_ingestion_runtime_schema.sql` to that database and
   reruns `tests/integration/test_publication_views_integration.py` and
   `tests/integration/test_application_roles_integration.py`, so the read
   views and the grants of the database roles are also proven on a
   database the runtime patch, which re-creates views and re-issues grants,
   has been applied to twice.
6. It runs `tests/restore_helper_check.py` in that isolated environment: an
   explicit synthetic dump, container, database and backup directory are passed
   to the actual development `restore_db.sh`, with globals, curation and
   Compose startup disabled. It checks the exact schema record, restored
   title/year, ownership and schema support as both application roles, then
   reruns the view and role tests to verify read/write boundaries. This covers
   the helper's `--no-owner` restore retaining ACLs; the canonical Compose
   restore in stages 7 and 11 retains owners as well and skips the baseline
   patch. No private configuration or backup is read.

Every test has to run: a skipped test, or a run in which no test ran at all,
fails even when pytest itself passed, so a broken database binding cannot pass
as an empty green run. The exit status is the tests' own on a test failure, 1
on any other failure, and 130, 143 or 129 after SIGINT, SIGTERM or SIGHUP.
However the run ends, short of SIGKILL, the cleanup removes the container and
volume carrying this run's label, and the run's private temporary directory,
and then confirms that nothing is left; if something is, or Docker cannot
say, it prints the `docker` commands that find it, and a run that had passed
fails with status 1, while a failed one keeps its own status. The superuser password is generated
per run, reaches Docker, the schema upgrade and the test command only through
their environment, never through a command line, is never printed or written
to a file, and is masked in the output of the schema upgrade and the tests, in
the container diagnostics and in the error output of a failed re-application.

Safety net (added 2026-07-13 after a real near-miss):
`tests/integration/conftest.py` exports `PEOPLE_DB_DSN` to the
integration DSN before any test imports `people_pubs`, so a code path that
accidentally falls back to the default connection resolution (env →
`DB/.env`) lands in the disposable database instead of a real one. Tests must
still pass explicit DSNs; this guard only changes the failure mode of a
missed patch from "writes to a real database" to "wrong rows in the
disposable one". The network guard (see *Network access during tests*) now
refuses a psycopg connection to any other server outright.

## The schema upgrade's tests

`DB/init/` is the baseline, the schema of PubDex 0.1.0, and never changes;
every later change of the schema is a schema change in `DB/schema/`, which
`python -m people_pubs.schema upgrade` applies and records in
`pubdex.schema_changes` (`people_pubs/schema.py` documents the rules). Every
database of the suite takes this path: stage 3 checks it step by step, and
stages 4 and 5 apply the schema upgrade after the baseline before they use
the database.

- **Offline, stage 1.** `tests/test_schema_changes.py` holds the SHA-256 of
  each file of `/opt/pubdex/init` in the published 0.1.0 application image
  and fails when `DB/init/` differs in any file, an added or a removed one
  included; it also checks that every file of `DB/schema/` follows the rules
  and that the changes of 0.2.0 only add, by the compatibility heuristic.
  `tests/test_schema_runner.py` covers the rest without a server: how a file
  is read and when it is refused, how statements are split, the heuristic,
  how a database is placed and every refusal, the order of the changes, the
  lock, a failed change, the command line's usage and password-file errors,
  and the exact messages.
- **Stage 3**, `./run_migration_smoke.sh`: the baseline without a record,
  the schema upgrade run from `.venv` with the password in a pipe that
  `PEOPLE_DB_PASSWORD_FILE` names, the record against `DB/schema/` and the
  release in `VERSION`, the application roles' privileges on every table and
  sequence of `app`, `biblio` and `activity` and on the record, then a second
  schema upgrade and `status` that leave the record unchanged, and two
  re-applications of the runtime patch that leave it unchanged too.
- **Stage 5**, `tests/integration/test_schema_runner_integration.py`, on
  databases of its own on the disposable server: a baseline with every
  change and a second run; a change that fails halfway, which leaves nothing
  of itself while the changes before it stay recorded, and the next run;
  two runners at once, the second waiting and then finding nothing to do,
  and a runner that gives up after its lock timeout; a role that is not a
  superuser, owning the baseline it applied as in external mode, upgrading
  it through the command line while a superuser that does not own it is
  refused; and the refusals of a database without the PubDex schema, with
  part of the baseline, with an edited change or with a later release's
  change that is not backward compatible. The role tests check the record's
  privileges on the suite's own upgraded database, again after the runtime
  patch has been re-applied.

The deployments run the same command. The Compose deployment runs it as
its one-shot service `schema-upgrade`, which stages 6 and 7 check; the
Helm chart as a Job in evaluation mode and as a hook after the setup in
external mode, which stage 8 renders and stage 9 runs (each below). The upgrade from one PubDex release to another, with
the schema changes between them, is stage 9's on Kubernetes and stage 11's
on Compose: from the published preceding release to the checkout, and from
test release A to B1, whose test-only overlay adds a backward-compatible
schema change, and, on Compose, to B2, which adds one that is not.

The application's schema contract is checked offline in
`tests/test_schema_support.py` and on disposable PostgreSQL databases in
`tests/integration/test_schema_support_integration.py`. Both the read service
and the roster ingestion command refuse a database without a recorded schema
version, an older schema and an incompatible newer schema; they accept the
matching schema and a newer schema whose later changes are backward compatible.
Readiness and publication requests check on every request, so an upgrade is
visible without restarting the read service. Liveness remains independent.
Stage 6 first starts PostgreSQL and the read service without the schema
service on fresh volumes: readiness reports `schema_upgrade_required`, the
unrecorded schema version and `python -m people_pubs.schema upgrade`; the
probe prints the message and liveness answers 200. It then runs the schema
service and continues the normal acceptance path.

`tests/test_release_version.py` checks that `VERSION`, both Compose image
defaults, the chart's `version` and `appVersion`, and both chart image tags
agree. Mutating each value independently proves that a mismatch fails.

## The Compose deployment's acceptance test

`../run_stack_acceptance.sh` (stage 6 of `../run_ci_suite.sh`) tests the
deployment of [`../DEPLOYMENT.md`](../DEPLOYMENT.md) as an operator runs it:
the real `compose.yaml`, the real image and the packaged ingestion path, with
the one addition [`tests/compose.acceptance.yaml`](tests/compose.acceptance.yaml),
which labels the run's resources and draws the network boundary described
under *Network access during tests*. In order, stopping at the first check
that fails:

1. **The image, and its `compose.yaml`.** Without options it builds the checkout
   with `../build_image.sh` under the tag `pubdex-acceptance:<run id>` and
   checks that the revision label is the checkout's commit, with `-dirty`
   for a modified working tree, and the version label the checkout's
   `VERSION`. With `--image <reference>` it uses that image — pulled first
   with `--pull`, otherwise one Docker already has — and never builds or
   substitutes one; a reference with a digest has to resolve to an image
   carrying it, and `--expect-version <version>` and `--expect-revision
   <commit>` require exactly those labels: one that is missing, `unknown`
   or malformed fails, and so does `<commit>-dirty` when `<commit>` is
   required. It prints the image's id, platform, version, revision,
   transform version and registry digests. It then reads
   `/opt/pubdex/compose.yaml` out of the image, in a container of the run's
   own without network; an image without it predates this deployment and
   is refused. In another container of the run's own, as the user 10001,
   everything under `/opt/pubdex` has to be root's, directories 755 and
   files 644, readable and not writable for that user, who may write
   nothing else in the image but `/var/lib/pubdex/initdb` and the temporary
   directories. In a third, as that user, the standard library's
   `importlib.metadata` has to find every package of the runtime lock the
   image carries at its locked version, and a lock that pins nothing fails;
   the image's Python may not be able to import pip, and no file in the
   image may be named as pip, its scripts, its distribution or a wheel; the
   other distributions, from the base image, are listed. The check goes by
   name: it shows that pip and the wheels `ensurepip` installs it from are
   gone, not that nothing in the image could install or run software. Without
   `--release` the run starts this checkout's `compose.yaml`: a build has
   to carry it unchanged, and for a given image
   the run says when the image carries another. With `--release` it starts
   the image's own, whose default image has to be the `--expect-version`;
   the checkout takes no part but the test itself, and Git is not needed
   ([`../DEPLOYMENT.md`](../DEPLOYMENT.md), *What identifies a deployment*).
   `--release` needs `--image`, `--expect-version` and a full commit id for
   `--expect-revision`, refuses a reference whose tag names another version
   and, with `--pull`, one without a digest; the version is always read
   from the label, never from a tag. With `--release` the image's
   `compose.yaml` also has to have the `schema-upgrade` service. Last, in a
   fourth container as user 10001, the run lists the schema changes the
   image carries in `/opt/pubdex/schema` by number, name and SHA-256: an
   image without any predates the schema upgrade and is refused, and a
   build has to carry exactly the checkout's `DB/schema/`.
2. **Secrets, settings and the project directory.** `../create_secrets.sh`
   creates the three password files in the run's private temporary
   directory (directory 700, files 444), and an environment file of the
   run's own sets the image, that directory, the loopback address and a
   port Docker picks. The project directory, also the run's own, holds the
   selected `compose.yaml` and nothing else. The rendered configuration has
   to name the run's project, image and label, list the six services, give
   each service only its own password files — the superuser's to
   PostgreSQL and `schema-upgrade` alone, none to `bootstrap` — start
   `schema-upgrade` once PostgreSQL is healthy, read-only, without
   capabilities and port, and the read service, `ingest` and `demo` only
   after it has succeeded, keep the `bootstrap` service off every network,
   keep PostgreSQL, the schema upgrade and ingestion on the internal
   network, and must not contain a password.
3. **The first start.** `docker compose up --wait` on empty volumes; the
   `bootstrap` service's log has to show that it copied the image's schema
   files — for a build, exactly the checkout's `init/`, by SHA-256 — and the
   PostgreSQL log that it ran `init/020_role_passwords.sh`, which gave both
   application roles their passwords. The `schema-upgrade` container has to
   have exited with status 0, its log has to show the baseline found and
   every schema change of the image applied, and `pubdex.schema_changes`,
   read as `app_readonly`, has to hold exactly the image's changes by
   number, name and SHA-256, applied by the image's version. The containers may mount nothing but
   the project's two volumes and the run's secret files: nothing of the
   checkout or of the project directory.
   The read service has to run exactly the selected image and be published
   on its loopback port only, PostgreSQL on no host port, PostgreSQL's
   networks have to be internal, and an ingestion container may have no
   usable default route.
4. **HTTP on an empty database.** `GET /livez` 200, `GET /readyz` 200, and an
   empty first page from `GET /v0/publications`.
5. **A small ingest, and what the service makes of it.** An ingestion
   container connects as `app_writer`; `docker compose run --rm demo` imports
   the bundled roster, ingests the bundled ORCID and Crossref payloads, adds
   the attribution rule and refreshes the snapshots, as `app_writer`. Then
   `GET /v0/publications` has to serve `10.5555/pubdex-fixture-001` with
   exactly the export's fields and their expected values, the database has
   to hold its cumulative provenance — `crossref+orcid`, and both sources'
   entries in `source_provenance` stamped with the image's transform version
   — read as `app_readonly` through PostgreSQL's socket in its container,
   and the read service's sessions have to run as `app_readonly`.
6. **A database interruption.** With PostgreSQL stopped, `/readyz` has to
   answer 503 `database_unavailable` within 30 seconds while `/livez` stays
   200; once PostgreSQL is started and healthy again, `/readyz` has to answer
   200 within 60 seconds, from the same read-service process.
7. **Persistence.** `docker compose up --wait --force-recreate` has to
   replace every container and keep the same database volume; `docker compose down`
   has to remove the containers and keep the volume, and `up --wait` start
   the stack again, the `bootstrap` service copying the schema files each
   time. After each, PostgreSQL has to skip initialisation — the
   log shows `Skipping initialization`, not `020_role_passwords.sh`, which is
   what tells a restart from a first start — the schema upgrade has to find
   nothing to do and leave the record as it was, and the stack has to serve the
   same publication, with the same `pub_id`, while `app_writer` still logs in.
8. **Secrets, and a clean stop.** A rerun of `create_secrets.sh` has to keep
   all three files byte for byte. The environment of the database server's
   process has to hold no `POSTGRES_*` setting, and that of the read
   service's process no password setting; each is read as the user its
   process runs as, the only one that may read it, and a read that fails or
   comes back empty fails the run. `docker compose stop` has to end the read service
   with `Application shutdown complete`. No password may occur in the logs
   of the stack's containers — each saved before a recreation or `down`
   removed it — the output of the one-shot commands, the rendered
   configuration, `docker inspect` of every container or those
   environments.

The waits are bounded: `STACK_WAIT_SECONDS` (default 300) for each `up
--wait` and for PostgreSQL to become healthy again, fixed limits for the
readiness checks, ten seconds for each HTTP request, and a limit for each
one-shot command, which stops the command when it is reached, and 900
seconds for the pull of `--pull`. The image build, and the pull of the
PostgreSQL image during the first `up` when Docker does not have it, have
no limit of the run's own; a CI job's time limit bounds them. The exit
status is 0 when every check passed and the cleanup was confirmed, 1 for a
failed check or cleanup, 2 for a usage error, and 130, 143 or 129 after
SIGINT, SIGTERM or SIGHUP. The cleanup is described
under *Isolation and cleanup*. Not checked in every run: a secret file that
is missing or unusable at the first start, which
`tests/test_role_passwords_script.py` and `tests/test_create_secrets_script.py`
cover on stubs, and which takes more than a minute to fail against Docker;
and how a one-shot container reacts to `docker stop`. Both were checked
against Docker when the deployment was built ([`../CHANGELOG.md`](../CHANGELOG.md),
the 2026-10-14 verification). A password file that no longer agrees with the
database, and `set_passwords.sh`, are checked against Docker in every run of
stage 7, *The operator's workflow test* below.

## The operator's workflow test

`../run_operator_acceptance.sh` (stage 7 of `../run_ci_suite.sh`) tests what
[`../DEPLOYMENT.md`](../DEPLOYMENT.md) tells an operator to do, done as it
says, on the deployment stage 6 tests: the real `compose.yaml`,
`create_secrets.sh` and `set_passwords.sh`, copied byte for byte into a directory of the run's own
that holds nothing else of the checkout, the real image, which carries the
schema, and
ingestion path, the ordinary network, and the documented commands typed as
an operator types them — `docker compose ...` in the directory of
`compose.yaml`, which an `.env` file configures. Only where a script has no
terminal does a command add `-T`. In order, stopping at the first check that
fails:

1. **The image.** Without options it builds the checkout with
   `../build_image.sh` under the tag `pubdex-operator:<run id>`; with
   `--image <reference>` it uses that image, which Docker has to have, and
   never builds or pulls one. It prints the image's id, version and revision.
2. **The operator's directory, `.env` file and secrets.** The copies of
   `compose.yaml`, `create_secrets.sh` and `set_passwords.sh` have to equal the checkout's, and
   the directory to hold them and the `.env` file alone. The `.env` file sets `COMPOSE_PROJECT_NAME`
   (`pubdex-operator-<run id>`), `PEOPLE_PUBS_IMAGE`,
   `PEOPLE_PUBS_SECRETS_DIR=../operator-secrets`, a free
   `PEOPLE_PUBS_HTTP_PORT` and `PEOPLE_PUBS_API_LOG_LEVEL=WARNING`.
   `create_secrets.sh`, given the same secrets directory as DEPLOYMENT.md
   says, has to create the three files there, outside the operator's
   directory (directory 700, files 444). `docker compose config` has to
   render that project, image, port on 127.0.0.1, those secret files and the
   log level, on an ordinary network, with no password in it.
3. **The first start.** `docker compose up --wait`; the `bootstrap`
   service's log has to show that it copied the image's schema files, and
   the PostgreSQL log that they ran and gave both application roles their
   passwords; the `schema-upgrade` log that it found the baseline and applied
   the image's schema changes, as many as `pubdex.schema_changes` then
   records; the containers may mount nothing but the project's two
   volumes and the secret files; the read service has to run the selected
   image and be published on
   127.0.0.1 at the configured port alone, PostgreSQL on no host port;
   PostgreSQL's networks have to be ordinary ones, and an ingestion container
   has to have a default route — the topology of a deployment, not of stage
   6; `GET /livez`, `/readyz` and `/v0/publications` on that port answer 200,
   the list empty.
4. **A private roster on standard input.** A synthetic roster of three
   people with checksum-valid iDs and UTF-8 names, one of them the person
   the bundled payloads belong to under a name of the roster's own, is a
   file of the invoking user's with mode `0600`. An ingestion container has
   to run as uid 10001; a bind mount of the file has to fail with the
   documented `cannot read the roster /data/roster.csv: permission denied`
   (left out when the invoking user is uid 0 or 10001, which could read
   it); `docker compose run --rm -T ingest python -m
   people_pubs.sync.add_people --csv - --no-orcid-lookup < roster.csv` has
   to report three insertions, and the database — read as the superuser
   through PostgreSQL's socket, the SQL on standard input — has to hold
   exactly the roster's iDs, kinds, names and addresses. A second import has
   to skip all three and change no record, and the file has to keep its
   owner, mode and SHA-256.
5. **Ingestion, refresh, read service, export.** `attribution_rules set`
   configures the payloads' institute before any ingestion, with an empty
   refresh; `people_pubs.sync.fixture_demo` has to ingest the ORCID and then
   the Crossref payload for the roster's person; `GET /v0/publications` has
   to serve the empty snapshot still; after the documented refresh,
   `attribution_rules recompute --refresh-only`, it has to serve
   `10.5555/pubdex-fixture-001` with its expected fields and the roster's
   name as its internal author; and the documented export, `docker compose
   run --rm -T ingest python -m people_pubs.sync.export_publications >
   publications.csv`, has to write exactly the header and that row, UTF-8,
   to standard output, its log line to standard error.
6. **A password change in place.** The three files are overwritten with new
   random passwords — `chmod u+w`, the password written, `chmod 444` — and
   have to keep their inodes and mode. Then the documented commands:
   `set_passwords.sh`, given the `.env` file's secrets directory, which has
   to confirm that each role authenticated a new connection,
   `docker compose restart api` and `docker compose up --wait`. The read
   service has to have restarted, the three roles have to be accepted with
   the new passwords and refused with the old ones on new TCP connections
   from a new ingestion container, which reads the passwords it tries from
   its standard input and starts with `--no-deps`, without the schema
   upgrade that `docker compose run` would otherwise start first with the
   superuser's file, and the read service has to answer `/readyz` 200 and
   serve the same publication. Its pooled connections are not taken as
   evidence: they stay logged in after the change, which is why the restart
   is part of the procedure.
7. **A password change by replacement.** New files are moved into place by
   rename and have to have new inodes, and the running database container
   has to still read the old content of all three (`cmp` inside it): the
   situation in which a resync from inside that container set the old
   passwords again. The former `020_role_passwords.sh --resync` has to be
   refused. `set_passwords.sh` run without the `.env` file's secrets
   directory, next to a decoy `secrets/` that `create_secrets.sh` made, has
   to refuse with "a new api container reads another password", and the
   passwords of step 6 have to work still. Run as documented, it has to
   succeed without a restart of PostgreSQL (its start time unchanged); after
   `restart api` and `up --wait` the read service reads the new file, the
   three roles are accepted with the newest passwords and refused with both
   earlier generations, and the read service serves the same publication
   while the database holds the roster's records unchanged.
8. **Backup and restore into a new stack.** The commands of
   DEPLOYMENT.md, *Backup and restore*: its verification query has to count
   the roster's three people and one publication, stored and served; `(umask
   077 && docker compose exec -T postgres pg_dump -U postgres -Fc people_db >
   people_db.dump)` has to write a file of mode `0600`. The containers' logs
   are saved, and `docker compose down --volumes` has to leave no container,
   volume or network of the project. `docker compose up --wait postgres` has
   to initialise a new volume — the PostgreSQL log shows the role passwords
   set from the files, now the third generation — and start no read
   service; then `docker compose stop api`, `dropdb --force`, `createdb`,
   `pg_restore --single-transaction --exit-on-error` from the dump on
   standard input, and `docker compose up --wait`, whose schema upgrade has
   to find the record of schema changes the dump brought and nothing to do,
   leaving that record as it was dumped. The verification query
   has to count the same again, the read service has to serve the same
   publication with the same `pub_id`, the database to hold the roster's
   records, and `app_writer` to refresh the restored snapshot, which takes
   its `EXECUTE` privilege on the definer function from the dump.
9. **Environments, log level, roles and leaks**, on the restored
   database. The database server's process holds no `POSTGRES_*` setting,
   the read service's no password setting but
   `PEOPLE_PUBS_API_LOG_LEVEL=WARNING`, and its log no request line. The
   role model of DEPLOYMENT.md, as the superuser on the socket:
   `maintenance` has no login and no password, `app_readonly` may not use
   `pii`, `app_writer` may use no `pii` table and execute exactly the six
   definer functions there; after `SET ROLE maintenance` the superuser reads
   the roster's `pii.people_pii` rows and is refused `app.people`, and after
   `SET ROLE app_writer` it is refused `pii.people_pii`. The documented
   commands give `maintenance` a login and a password — here from standard
   input, without a terminal — which a new TCP connection has to accept,
   and take the login away again, after which it is refused. No password of
   any generation, the decoys' included, may occur in the containers' logs,
   those of the first stack included, the output of the one-shot commands
   and of `set_passwords.sh`, the
   rendered configuration, `docker inspect` of every container or those
   environments, and no name or address of the roster in the imports'
   output or the containers' logs, the first stack's included.

The waits are bounded as in stage 6: `STACK_WAIT_SECONDS` (default 300) for
each `up --wait`, 60 seconds for readiness after a change, and a limit for
each one-shot command and `docker compose exec`, which stops it when it is
reached; the image build has no limit of its own. The exit statuses are
those of stage 6, and the cleanup is described under *Isolation and
cleanup*. Not checked: the steps an operator takes with an editor or a
scheduler, which the test replaces by shell builtins and direct commands;
`PEOPLE_PUBS_HTTP_ADDRESS` other than 127.0.0.1, which would publish the
read service beyond the host; the optional provider keys, whose commands
contact providers; and restoring a dump of the development setup,
`compose.yaml` in this directory, or of another release.

## The Helm chart's offline checks

`../run_chart_checks.sh` is stage 8 of `../run_ci_suite.sh`. It checks the
Helm chart in `../charts/pubdex/` with the helm that
[`lib/cluster_tools.sh`](lib/cluster_tools.sh) pins and
`../setup_cluster_tools.sh` installs under `.tools/`. It needs no cluster,
no Docker and no network. Before helm runs, the script compares its SHA-256
and the version it reports with the pin. Then:

1. `helm lint --strict` lints the chart for the oldest and the newest
   Kubernetes release it supports (1.35.0, the floor of `kubeVersion` in
   `Chart.yaml`, and 1.37.0, the release of the pinned kind node image). It
   does so once with the chart's own values, once with each values file of
   the checks in `../charts/tests/values/` and once with each example in
   `../charts/pubdex/examples/`.
2. pytest runs the tests in `../charts/tests/`. Each values file of the
   checks and each example is a case: helm template renders it and the
   tests parse the manifests.
   - **Pod Security "restricted".** Every pod meets it, and every container
     has a read-only root and writes only to the paths its entry in
     `charts/tests/chart.py` lists.
   - **Credentials.** No Secret is rendered, and no credential appears in an
     environment variable or a setting. Each container receives exactly its
     own keys of the operator's Secret, as files with mode 0440.
   - **Objects.** Labels, selectors and references resolve. Names follow
     the release and the namespace, without collisions between two
     releases.
   - **Values.** Images and their pinning by digest, the probes, the
     settings and their checksum annotation, which rolls the pods when a
     setting changes, and the rendered `NOTES.txt` all follow the values.
   - **External mode.** No PostgreSQL StatefulSet, Service or volume is
     rendered. The setup Job is a `pre-install,pre-upgrade` hook that runs
     `files/external-setup.sh` exactly as the chart holds it, and it is the
     only pod that mounts the admin Secret. A CA certificate is mounted
     only in the containers that connect to the server, at the path their
     settings name. The three lines the setup leaves out of
     `init/000_init.sql` each occur there once, and its filter removes
     exactly those.
   - **Ingestion and the demonstration.** Each ingestion task is a CronJob
     whose runs execute exactly its command as `app_writer`, with only that
     role's key and the ConfigMap's connection settings; a task without a
     schedule is suspended. The demonstration is off by default; enabled, it
     is a `post-install` hook Job that runs the commands of `compose.yaml`'s
     `demo` service, and external mode refuses it.
   - **The attached pod and the roster.** The tests build the pod
     `kubectl run` creates and apply the chart's JSON patch from the
     ConfigMap `<release>-run`. That pod has to keep kubectl's command and
     standard input and meet every check above. No value of the synthetic
     roster appears in any rendering or chart file, no value can hold a
     roster, and a task that runs `add_people` is refused.
   - **Network.** NetworkPolicy is on by default and restricts only
     ingress. Its selectors are checked against the rendered pods: PostgreSQL
     admits exactly the pods that connect to the database, the read service
     its client label and the configured peers, and neither admits another
     release's pods. The Ingress renders only when enabled, leads every host
     and path to the read service, and the values, the examples and
     `NOTES.txt` warn that it publishes a service without authentication.
   - **Refused values.** The tests give values that the schema or the
     templates have to refuse, and check each refusal for its message.
     Kubernetes releases below the floor are refused too.
   - **The package.** `helm package` packages the chart as `../release.sh`
     does, with `SOURCE_DATE_EPOCH` and a test release's version: the same
     bytes from a copy whose files have other times, every file of the chart
     and the repository's `LICENSE`, no Markdown link that leaves the
     package, the chart's own files named relative to the chart, every other
     file it names one its README locates, and `create_secrets.sh` never
     invoked as if it lay beside the chart. The package lints and renders
     from the file.

helm runs with a cleared environment. Its home, temporary, configuration,
cache, data and Docker configuration directories lie in a temporary
directory, and `KUBECONFIG` names a file that does not exist, so no
kubeconfig is read and no cluster is contacted. The tests run in a cleared
environment as in `./run_tests.sh`. `charts/tests/conftest.py` loads
`tests/conftest.py` as a plugin, so the network guard and the skip policy
of stage 1 apply to them as well. The tests live outside `DB/tests`
because they need helm. Stage 1 does not collect them, and without the
pinned helm they fail; they never skip. The script writes nothing outside
its temporary directory and removes it at the end. A new example or values
file becomes a case of every check without a change to the tests. A new
container has to be added to `chart.CONTAINERS` with its image, Secret keys
and writable paths, and with whether it receives the admin Secret or
connects to an external server. The checks render the Ingress only: no
ingress controller runs in them.

## The Helm chart on a disposable cluster

`../run_cluster_acceptance.sh` is stage 9 of `../run_ci_suite.sh`. It tests
the chart in `../charts/pubdex/` on two Kubernetes clusters that it creates,
one after the other, with the pinned kind from the pinned node images
([`lib/cluster_tools.sh`](lib/cluster_tools.sh)), and deletes again: in both
of its modes on the newest release the chart supports, Kubernetes 1.37, and
evaluation mode's lifecycle on the oldest, Kubernetes 1.35, the floor. It
needs Linux on x86_64, Docker with the buildx plugin, the pinned helm,
kubectl and kind under `.tools/`, whose hashes and versions it checks first,
the pinned SBOM tool of `../release.sh setup`, `.venv`, `openssl`, GNU tar,
access from the clusters' nodes to the registry of the published images
(`ghcr.io` for the release record's), and a clone with the history of the
preceding PubDex release's chart. Revision 1 of each Helm release is the
preceding PubDex release as an operator has it installed: the newest
release below `VERSION` in the release record (`../releases/`, read by
[`release_record.py`](release_record.py)), its chart, `charts/pubdex/` at the
commit its entry names, which `git archive` takes from the repository's
history into the run's temporary directory, and its two published images by
registry, tag and digest. A clone without that commit, a shallow one, fails
before anything is created. Revision 2 is this checkout's chart with the
images under test.

External mode also upgrades between two ***test releases*** of the checkout,
which `../release.sh build --test` builds and verifies under versions
reserved for test releases, with names of the run's own: A, the checkout as
it is, and B1, the checkout with the test-only overlay `b1` of
`tests/release_overlay/`, one backward-compatible schema change after the
checkout's and a new transform version. Each is installed from its chart
package file, with both images loaded into the node and set explicitly by
the local tags only this run gives them: in external mode the setup Job runs
the PostgreSQL image's `psql`. They are built with `--image` too:
they test the mechanism of the upgrade between PubDex releases, whatever
images the rest of the run tests.

The upgrades of steps 8, 17 and 33 run the schema upgrade: in evaluation
mode the revision's Job, which Helm waits for with `--wait-for-jobs`, and in
external mode the hook after the setup. After each, the database records
exactly the schema changes of `schema/`, by number, name and SHA-256, which
the run computes from the files, and the demonstration's publication is
still served. The rollbacks of steps 9, 20 and 34 return to revision 1's
chart and images and leave the schema and its record as they were; the
preceding release's images serve the publication on that schema, which
backward-compatible schema changes allow. That the checkout's `init/` is
still the baseline, the schema of 0.1.0, is shown by
`tests/test_schema_changes.py` and by the schema upgrade itself, which
refuses a database it does not recognise.

`tests/test_release_record.py`, in stage 1, checks the release record and
its reader offline: 0.1.0's entry names the published commit, both image
digests, no schema change, the transform version 1.5.3 and its chart's
commit; every committed entry passes the reader's checks and `FLOOR` names
one of them; versions order as Semantic Versioning orders them, a test
release's version included; the preceding release is the newest entry below
`VERSION` or below a given version; and a malformed entry, a test release in
the record, a `FLOOR` naming no recorded release and an unknown release are
refused with a message that says why.

Every step runs on every run; no option or setting skips one. One run:

1. builds both images from the checkout with `../build_image.sh` under tags
   of its own, as stage 6 does, and checks their version and revision
   labels; with `--image` and `--postgres-image` it tags the given images
   instead, and refuses them when they are the published images, which the
   upgrade has to replace. It then takes the preceding release's chart from
   the repository's history and checks that its version is the one the
   release record names. Last, it builds test releases A and B1 with
   `release.sh build`, each into its temporary directory with `TMPDIR`
   there, and checks that A's source archive holds exactly the checkout's
   schema changes and B1's those and one more, and that B1's application
   image carries another transform version than A's;
2. creates a Docker network of its own and the kind cluster on it, with a
   kubeconfig in its temporary directory, and checks that kubeconfig before
   any kubectl or helm call (*Isolation and cleanup*);
3. loads the two images under test into the node with
   `kind load docker-image`, or from an archive when that fails, and has the
   node's container runtime pull the two published images by digest,
   without credentials (`crictl pull`). Without access to their registry
   the run fails at this step with a message that says so; it never skips
   the upgrade from the published images. The images under test must have
   other image ids than the published ones. Then it loads both images of
   test releases A and B1; their application images have different ids in
   the node.

Evaluation mode, release `pubdex` in namespace `pubdex`:

4. creates the namespace with Pod Security `restricted` enforced, warned and
   audited, and the Secret from the files of `../create_secrets.sh`, with
   `kubectl create secret generic --from-file`, as the chart's values
   describe it;
5. validates every rendered object of revision 1, the preceding release's
   chart, and of this checkout's chart with the upgrade's values, the
   schema upgrade's Job included, with a server-side dry run
   (`kubectl create --dry-run=server`), which has to pass admission without
   a warning; `helm test`'s pod of each chart gets the same dry run once the
   release's ServiceAccount exists. The same dry run of this checkout's
   chart, without `helm test`'s pod, admits what no step installs, each file
   on its own: the two ingress examples,
   `../charts/pubdex/examples/values-ingress-tls.yaml` and
   `values-ingress-cert-manager.yaml`, and
   `../charts/tests/values/evaluation.yaml`, which sets the six scheduling
   keys at the top level and replaces some for the read service and
   PostgreSQL. The PriorityClasses it names exist in no cluster of the run:
   a dry run creates no pod, the only object whose class the API server
   resolves. Nothing is scheduled and no ingress controller runs;
6. installs revision 1, the preceding release's chart and published images,
   with the demonstration and `--wait`: the demonstration's `post-install`
   Job has to complete;
7. checks that every pod runs the published image by its digest and that
   the database's record of schema changes is the preceding release's
   (none for 0.1.0, which ships no schema change), reads
   `GET /v0/publications` through `kubectl port-forward` on 127.0.0.1, field
   by field as stage 6 does, and `/readyz`, finds the port forward in what
   `helm get notes` prints, then runs `helm test`;
8. upgrades to this checkout's chart and the images under test, with
   another log level in the ConfigMap and two read-service replicas, and
   `--wait-for-jobs`. The revision's schema upgrade Job has to complete,
   and the log of its pod to say that it applied what was outstanding; the
   database has to record exactly this checkout's schema changes. Every pod
   has to be replaced and run the new image, the settings' checksum has to
   change, the publication keeps its `pub_id`, PostgreSQL's log shows that
   it skipped initialisation on its volume, `app_readonly` and `app_writer`
   log in with their passwords over SCRAM, and `helm test` passes;
9. rolls back to revision 1, with the same checks; the record of schema
   changes is unchanged, revision 2's schema upgrade Job is gone with its
   revision, and the preceding release's images serve the publication on
   this checkout's schema. It finds the rollback as revision 3 in the
   release's history, and revision 1's values, both published images by
   digest among them, in what `helm get values --all` prints. Then it
   upgrades to this checkout's chart and the images under test again, with
   revision 1's log level and one replica: the new revision's schema
   upgrade Job finds nothing to do, the record is unchanged, and steps 10
   to 13 run this checkout's chart and images;
10. changes every password: new files from `../create_secrets.sh` replace
    the Secret's (`kubectl create secret generic … --dry-run=client
    --output yaml | kubectl replace --filename -`), the image's
    role-password step `020_role_passwords.sh --stdin`, run in the
    PostgreSQL pod with `kubectl exec -i`, sets them, and
    `kubectl rollout restart` restarts the read service. Each of
    `postgres`, `app_readonly` and `app_writer` logs in with its new
    password over SCRAM, and its old password is refused;
11. scales PostgreSQL to 0 with `kubectl scale`: `/readyz` answers 503 with
    the reason `database_unavailable` and `/livez` 200, and the read
    service's pod becomes not ready but is not restarted. Scaled back to 1,
    the same pod becomes ready again without a restart, and PostgreSQL
    starts on its volume without a new initialisation and logs in with the
    new passwords. This mirrors stage 6;
12. observes the NetworkPolicy on kind's network plugin, kindnet, which
    enforces it: three restricted pods of the namespace connect to the
    PostgreSQL and read-service Services with a 5-second timeout. One
    without the release's labels reaches neither, one with the client label
    `pubdex-client: "true"` reaches only the read service, and one with the
    labels of the release's ingestion pods reaches only PostgreSQL. A fourth
    pod without labels tries the read service until it connects; after its
    first failure, `kubectl label pod` gives it the client label, and its
    next attempt has to connect. This is observed on kindnet alone; other
    network plugins are not tested;
13. uninstalls the release: the database volume's claim and the operator's
    Secret remain, as the chart's notes say, and are then deleted with the
    notes' command and `kubectl delete secret`, after which the volume is
    gone and the namespace holds nothing of the release;
14. installs this checkout's chart afresh, as the guide's short path does,
    with the images under test, a new Secret, the demonstration, `--wait`
    and `--wait-for-jobs`: PostgreSQL initialises the baseline on its new
    volume, the schema upgrade Job of revision 1 applies every schema change
    of the checkout, and Helm starts the demonstration's `post-install` Job
    only after that Job has completed. The database records exactly this
    checkout's schema changes, every pod runs the images under test, the
    publication is served and `helm test` passes. The release is then
    uninstalled, and the claim and the Secret deleted, as in step 13.

External mode, release `pubdex` in namespace `pubdex-external`:

15. starts a PostgreSQL server of its own in namespace `pubdex-db`, which
    also enforces `restricted`: the published PostgreSQL image, prepared as
    a managed service would be. Its superuser is `clusteradmin`, no role is
    named `postgres`, and the database `people_db` belongs to
    `people_db_owner`, which has `CREATEROLE` and is no superuser. It serves
    TLS with a certificate for its Service's DNS name, issued by a CA the run
    creates with `openssl`;
16. creates the namespace, the Secret with the two application roles'
    passwords, the admin Secret and the CA's ConfigMap with the commands of
    [`../KUBERNETES.md`](../KUBERNETES.md), and installs revision 1, the
    preceding release's chart with its own `examples/values-external.yaml`,
    its published application image, the server's host and two on-demand
    ingestion tasks, after server-side dry runs of revision 1 and of this
    checkout's chart with the upgrade's values. The setup Job, a
    `pre-install` hook, prepares the database as `people_db_owner`; its Job,
    ConfigMap and ServiceAccount are gone afterwards, and the record of
    schema changes is the preceding release's. The run then
    deletes the admin Secret and checks that no pod or workload of the
    namespace refers to it, and that the read service's sessions use TLS
    (`sslmode` `verify-full` against the run's CA). It runs the synthetic
    demonstration with the commands the chart's notes print: the roster
    through `kubectl run … -i --rm` with the patch of ConfigMap
    `pubdex-run`, on standard input, and the other two steps as jobs created
    with `kubectl create job --from=cronjob/…`, whose logs
    `kubectl logs --follow` follows to the end once each job's pod has
    started; the log read afterwards has to be the same. The export then
    runs in the same attached pod: standard output has to be the CSV, with
    the demonstration's publication, and standard error the export's log.
    It reads the publication and runs `helm test`;
17. upgrades to this checkout's chart and the images under test with the
    changed values, the admin Secret created for the `pre-upgrade` hooks
    and deleted after them:
    the setup, then the schema upgrade, which applies this checkout's schema
    changes as `people_db_owner`. The database records exactly those
    changes, applied by that role; Helm deleted both hooks' objects; every
    read-service pod is replaced, the schema's objects keep their object ids
    on the server (no second initialisation), the publication keeps its
    `pub_id`, and `helm test` passes;
18. changes both role passwords: the Secret is replaced as in step 10,
    `helm upgrade` with step 17's values files runs the setup again with
    the admin Secret, and `kubectl rollout restart` restarts the read
    service. `--reuse-values` would keep a setup that a routine upgrade
    switched off, so the guide and this step give the values files. Both
    roles log in to the server with their new passwords, and their old ones
    are refused. The admin Secret is created just before that upgrade and
    deleted right after it, and is never created again. Around that
    upgrade the run notes the `resourceVersion` of the namespace's lists of
    Jobs, ConfigMaps and ServiceAccounts, the kinds of the setup's and the
    schema upgrade's hooks, and afterwards has the API server replay its
    watch of each from there: it has to show exactly the setup's Job,
    ConfigMap and ServiceAccount and the schema upgrade's Job and
    ServiceAccount, created, although Helm deleted them again once their
    Jobs succeeded. If the API server can no longer replay from that
    version, the check fails. The schema upgrade found nothing outstanding:
    the record of schema changes is unchanged;
19. runs a routine upgrade, one that changes no password and no schema: the
    API server first answers that the admin Secret does not exist. The same
    upgrade without the switch runs first, so with the setup, as an
    operator who forgot the switch would: it has to fail in its
    `pre-upgrade` hook, in less than half of the timeout, and
    `kubectl logs job/pubdex-setup` has to say that the file of
    `PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE` does not exist because its
    Secret is missing. `helm status` reports revision 4 as `failed`,
    `helm history` still shows revision 3 as `deployed`, of the hook
    objects of the setup and the schema upgrade only the setup's Job is
    left, and the read service runs on with the same pods and log level and
    serves the publication. Then `helm upgrade` with
    `--set externalDatabase.setup.enabled=false` changes one setting, the
    log level. The same replay, from before this upgrade, has to show no
    object created, so neither the setup nor the schema upgrade ran.
    `helm get hooks` shows that revision 5 carries only `helm test`'s hook.
    Every read-service pod is replaced and runs the same image, and the
    schema's objects keep their ids and the record its rows. The failed
    setup's Job is still there, and the guide's
    `kubectl delete job,configmap,serviceaccount` removes it and, with it,
    its pod; no pod or workload then refers to the admin Secret, the
    publication keeps its `pub_id`, and `helm test` passes;
20. rolls back to revision 1, the preceding release's chart and image, with
    `helm rollback`, still without the admin Secret. The rollback's revision
    carries revision 1's hooks, among them the setup's
    `pre-install,pre-upgrade` hooks (`helm get hooks`), and the replayed
    watch shows that nothing was created: Helm ran none of them. Every pod
    runs the published image by its digest, with the passwords of step 18;
    the schema's objects keep their ids and the record its rows, the
    publication keeps its `pub_id`, `helm history` shows revision 6 as the
    rollback to 1, `helm test` passes, and the admin Secret still does not
    exist;
21. uninstalls the release without the admin Secret, and the replayed watch
    shows nothing created: the operator's Secret and the CA's ConfigMap
    remain, and the server keeps the database, the publication and the
    roles `app_readonly`, `app_writer` and `maintenance`, as the chart's
    notes say. The run deletes the Secret and the ConfigMap. Last, no
    `FailedCreate` event in any namespace shows a pod that admission
    refused.

External mode, test release A to test release B1, release `pubdex` in
namespace `pubdex-external`, on the same server:

22. drops the database and the three roles step 21 left on the server, as
    the guide's *Uninstall* allows, and creates the database again for the
    admin role, so that the server is as step 15 checked it. With Secrets of
    new passwords and the CA's ConfigMap, it installs A from its chart
    package file with the setup: the setup applies the baseline and the
    schema upgrade's hook the checkout's schema changes, which the database
    records as A's, applied by the admin role. The admin Secret is deleted,
    and the demonstration runs with the commands of step 16, now with this
    checkout's chart and A's image: the roster through `kubectl run`, the
    tasks `fixture` and `rules`, and the export. The publication is served,
    and every row carries A's transform version;
23. tries B1 as a routine upgrade, without the admin Secret and with
    `--set externalDatabase.setup.enabled=false`, and a timeout of 30
    seconds in place of 300: neither the setup nor the schema upgrade runs
    (the replayed watch shows nothing created), so B1's schema change is
    outstanding. `helm upgrade` has to fail at that timeout; `helm status`
    reports revision 2 as `failed` and `helm history` revision 1 still
    `deployed`. B1's read service pod runs and is not ready: its events show
    the readiness probe failing with 503, its `/readyz` and
    `/v0/publications` answer 503 with the reason `schema_upgrade_required`
    and the message that names B1's schema change and the schema upgrade's
    command, and its log names them too. A's pod is the same, ready, and
    serves the publication, and the record of schema changes is unchanged;
24. creates the admin Secret and runs the same upgrade with the setup: the
    replayed watch shows the setup's and the schema upgrade's hooks
    created, and the database records B1's schema changes, the new one
    backward compatible and applied as B1's by the admin role. B1's pod of
    step 23 is not replaced: it turns ready once the schema upgrade has run,
    and A's pod goes. The admin Secret is deleted again; B1's ingestion task
    ingests the demonstration's payloads again, and every row then carries
    B1's transform version;
25. rolls back to revision 1, A, without the admin Secret: an image rollback
    after a backward-compatible schema change. The replayed watch shows
    nothing created, the schema and its record stay as B1 left them, A's
    read service is ready and serves the publication, A's ingestion task
    writes on that schema, every row carrying A's transform version again,
    and `helm history` shows revision 4 as the rollback to 1;
26. uninstalls the release without the admin Secret, deletes the Secret and
    the ConfigMap that remain, and checks again that admission refused no
    pod.

The floor, Kubernetes 1.35, evaluation mode, release `pubdex` in namespace
`pubdex`:

27. deletes the first cluster and its network, and confirms that they are
    gone, so the two clusters never run at the same time;
28. creates a Docker network of its own and on it a kind cluster from the
    pinned floor node image. Its API server writes an audit log of the
    operator's requests, their metadata only, from a policy the node mounts
    from the run's temporary directory. The kubeconfig is checked as in
    step 2;
29. loads the two images under test into the node, and checks that the
    node has neither published image, so that nothing pulls one before the
    install;
30. as the cluster's administrator, creates the namespace with Pod Security
    `restricted` enforced, a ServiceAccount for the operator, a Role
    granting exactly the rights that [`../KUBERNETES.md`](../KUBERNETES.md),
    *What you need*, lists under *Access rights* and a RoleBinding, and
    with `kubectl create token --duration 3600s` a token for that
    ServiceAccount. A second kubeconfig in the run's temporary directory
    holds only the operator, with that token, and passes the checks of
    step 2. The API server knows its user as
    `system:serviceaccount:pubdex:pubdex-operator`, which may not create a
    namespace;
31. as the operator, as every step to 35, with the guide's commands:
    creates the Secret from the files of `../create_secrets.sh` and
    validates revision 1, this checkout's chart with the upgrade's values,
    the two ingress examples and the scheduling values with server-side dry
    runs, as in step 5. The audit log then shows no refused request: the
    operator's rights cover every object they render, the Ingress and the
    schema upgrade's Job included;
32. installs revision 1 with the demonstration: the preceding release's
    chart and its published images by digest, with that chart's default
    pull policy `IfNotPresent`. The pods' events show the kubelet pulling
    each published image, and the node's images carry their digests. The
    checks of step 7 and `helm test` follow;
33. upgrades to this checkout's chart and the images under test with the
    changed values, with the checks of step 8, which include
    `kubectl exec`, `kubectl logs` of the schema upgrade's pod and
    `kubectl port-forward`, and `helm test`;
34. rolls back to revision 1, with the rollback's checks of step 9;
35. uninstalls the release and, with the notes' commands, deletes the
    database volume's claim and the Secret. As the administrator, the run
    then checks that the namespace holds nothing of the release, that the
    volume is gone and that admission refused no pod. It prints the
    operator's requests by resource and verb, from the audit log, and the
    rights of the Role that none used. Last, the administrator deletes the
    operator's RoleBinding, Role and ServiceAccount, and the token no
    longer authenticates.

After each of steps 31 to 35 the audit log must show no request of the
operator that the API server refused. A refusal fails the run and names
the verb and the resource, even where helm reports only a timeout.
`tests/test_cluster_acceptance_script.py`, in stage 1, compares the Role
with the guide's list, word by word, through a fixed meaning for each of
the guide's phrases: *create, change and delete* means the verbs create,
update, patch, delete, get, list and watch; *read* get, list and watch;
*change* update and patch; *create and get* those two.

Every helm and kubectl call is bounded in time, by `CLUSTER_WAIT_SECONDS`
(300) for the waits. The default kubeconfig is never read or written. A
failure prints the current namespace's pods, Jobs and events, and on the
floor the operator's refused requests. The clusters pull the two published
images and no other; the demonstration contacts no metadata provider.
Docker needs the two node images and the build's inputs unless it has
them. The run prints the versions of the tools, the node images, the API
servers and `openssl`, the published images' digests and the image ids in
the nodes, the Role as applied, and its duration. On the machine of its
local record a run took about ten minutes (573 to 598 seconds), with Docker's
build cache warm and the node images present. Not covered: an ingress
controller (the Ingress is only rendered, and admitted in steps 5 and 31),
a release installed with the scheduling keys set, the egress example of
`../KUBERNETES.md`, other network plugins, a managed PostgreSQL service,
an install without the setup Job
(`externalDatabase.setup.enabled=false`, the manual path) in a cluster,
which steps 19 and 23 use only for an upgrade of a release the setup prepared, a
scheduled CronJob run, the roster import in evaluation
mode, backup and restore on the cluster, external mode on the floor, the
guide's commands as the namespace-scoped user beyond the lifecycle of
steps 31 to 35, an upgrade from a PubDex release other than the preceding
one, the upgrade from test release A to B1 in evaluation mode and on the
floor, an upgrade across a schema change that is not backward compatible
(stage 11 runs it on Compose), and a Helm release in evaluation mode
installed from a chart package file.

[`../KUBERNETES.md`](../KUBERNETES.md), the operator guide, ends with an
appendix that gives every block of commands, in order, the steps of this
script that ran it and any difference, or says that only stage 8 rendered
it, that it ran once outside the suite, or that nothing ran it.
`tests/test_kubernetes_guide.py`, in stage 1, checks that each block has
its row, and reads this script step by step, each step with the functions
it calls. Each step a row names has to run at least one of the block's
kubectl and helm commands, and together they have to run all of them, with
every long option.

## The rehearsal of a PubDex release

`../run_release_rehearsal.sh` is stage 10 of `../run_ci_suite.sh`. It runs
[`../release.sh`](../release.sh) as the maintainer runs it for a PubDex
release, on test release A: the checkout as it is, uncommitted changes
included, under the version `<VERSION>-test.a<run>`, where `<run>` is ten
random hexadecimal digits of the run's own. Every version whose pre-release
part begins with `test` is reserved for test releases, and no test release
is published. Its steps, which the script's section comments number
alike:

1. **What the rehearsal needs.** Before it builds anything: a Git checkout
   whose `VERSION` has no pre-release part; GNU tar, gzip, `sha256sum`,
   `cmp` and python3; the pinned helm, its SHA-256 and the version it
   reports; a reachable Docker daemon for `linux/amd64` with a supported
   Engine and the buildx plugin; and the pinned SBOM tool that
   `../release.sh setup` pulls, with its pinned image id.
2. **Test release A.** `release.sh build --test a<run>` writes test
   release A into the run's temporary directory: the source archive, the
   chart package, both images under local tags, an SBOM of each image, the
   provenance record and the checksum file, and ends by verifying the
   finished directory.
3. **Its verification.** `release.sh verify` checks that directory again,
   on its own, building nothing.
4. **The second build and the comparison.** A second `release.sh build` of
   the same version, which ends by verifying its own directory, and the
   comparison of the two builds. Both wrote the
   same files. The source archive and the chart package are the same, byte
   for byte. `IMAGES` is the same but for the image ids. Each SBOM and the
   provenance record are the same once the values a build changes are left
   out: an SBOM's creation time, its document namespace and the image's own
   checksum and package URL; the record's build times, image ids, config digests, creation
   labels and the SHA-256 of `IMAGES` and of both SBOMs. The images
   themselves get new ids, since their labels record the build's time, and
   are not compared.
5. **The chart from its package file**, with the pinned helm in a cleared
   environment: `helm lint --strict` for the oldest and the newest
   Kubernetes release the chart supports — the floor of `kubeVersion` in
   the package's `Chart.yaml` and the release of the pinned kind node
   image — with the chart's own values and with each example the package
   holds, then `helm template` with each of them and test release A's
   images. The manifests have to name exactly those two images, and the
   chart's label the test release's version.
6. **Nothing published.** The run's images carry only their two local
   tags, and the Docker client configuration the run used holds no
   registry credential.

**No publication.** The script never runs `release.sh publish`,
`docker push`, `docker tag` or `docker login`; a test in stage 1 reads the
script for them. Every Docker command of the stage, those of `release.sh`
and `build_image.sh` included, runs with a Docker client configuration of
the run's own in its temporary directory, which names at most the caller's
plugin directory (`cliPluginsExtraDirs`, for a buildx installed under
`~/.docker/cli-plugins`), with `DOCKER_AUTH_CONFIG` and `DOCKER_CONTEXT`
unset and `DOCKER_HOST` naming the daemon the caller's client uses. No credential of the caller's, from
`docker login` or a credential helper, is within the run's reach. The stage
proves that `release.sh build` and `verify` produce and accept the complete
artifact set of the checkout, that the source archive and the chart package
are reproducible, and that the run published nothing. It does not prove that
`publish` works against a registry, nor the refusals that guard a PubDex
release — a checkout with changes, a missing or misplaced annotated tag, a
version a repository already holds as another image — which a test release
skips:
`tests/test_release_script.py` covers both with a stubbed `docker` in
stage 1. A build at the tag from a clean clone, without Docker's build
cache, stays the maintainer's step at a PubDex release.

[`../RELEASING.md`](../RELEASING.md), the maintainer's procedure, ends with
a table that names, for each block of its commands, the step of this stage
that ran it, or says that only a real PubDex release runs it and which test
covers it instead. `tests/test_releasing_guide.py`, in stage 1, checks that
each block has its row, in order, and that the steps a row names exist and
run the block's `release.sh` command, every option that differs being named
in the row.

On the machine of its local record the stage took about 27 seconds with
Docker's build cache warm, as it is in the suite, where stages 6, 7 and 9
have built the same images before; with an empty cache the first build adds
the images' complete build.

**Cleanup.** Every image of the run carries the label
`org.opencontainers.image.version=<its version>`, a version no other run
uses. On every exit, and after SIGINT, SIGTERM or SIGHUP, the stage removes
the containers of the run — the probes of `release.sh`, which carry that
label, and the SBOM tool's, which mount the run's directory — both local
tags, every image with the label, the first build's as well as the
second's, and the run's temporary directory, and confirms it as stage 6
does. The base images, the SBOM tool and the build cache stay. No test
release is kept for a later stage: a stage that needs one builds its own,
and BuildKit's cache makes a second build of the same content take seconds.

## Upgrades between PubDex releases on Compose

`../run_upgrade_acceptance.sh` is stage 11 of `../run_ci_suite.sh`. It
carries out, on the Compose deployment, the upgrades and rollbacks an
operator carries out between PubDex releases, with the operator's commands.
`release.sh` builds three test releases of the checkout, each under a
version reserved for test releases with a name of the run's own, and
verifies each:

- **A**, the checkout as it is (`--test a<run>`);
- **B1**, a copy of the checkout with the test-only overlay of
  `tests/release_overlay/` (`--test b1<run> --overlay b1`): the schema
  change `compatible.sql`, numbered after the checkout's last one — a new
  table with its sequence and a nullable column on `biblio.publications`,
  declared backward compatible — and a new transform version, the
  checkout's with the suffix `+test`;
- **B2**, the same with the overlay `b2`, which also adds `incompatible.sql`
  after it: a rename of `biblio.publications_canon_web.title` to
  `test_overlay_title`, declared not backward compatible. `application.py`
  adapts only B2's temporary readiness and listing queries to select the new
  name as `title`. A retains its real dependency on the original column.

The overlay is applied to a copy in `release.sh`'s temporary directory and
never to the working tree. The copy keeps every file's and directory's mode
and every file's extended attributes, so BuildKit's cache serves what the
overlay leaves unchanged and B1 and B2 build in seconds once A has.

Each stack lives in an operator's directory of the run's own: the
deployment files the application image in use carries, taken out of it with
the command of `DEPLOYMENT.md`, *Selecting a published image*, an `.env`
file naming the project, both images and the read service's port, and the
secret files `create_secrets.sh` writes. A switch to other images is
`docker compose down`, the other application image's deployment files, the
two images in `.env`, and `docker compose up --wait`, whose schema upgrade
brings the database to the schema of the images in use. Then:

1. **A to B1.** A on empty volumes; `docker compose run --rm demo`; the
   publication served over HTTP, every row stamped with A's transform
   version. A backup with the command of `DEPLOYMENT.md`, *Backup and
   restore*. The switch to B1: its schema upgrade applies the one schema
   change it adds and records it as backward compatible, and B1 is ready
   and serves the publication. The demonstration's payloads ingested again:
   every row carries B1's transform version.
2. **The image rollback to A.** The schema stays: A's schema upgrade
   reports B1's backward-compatible change and has nothing to do, A is
   ready, serves the publication, and its ingestion writes on B1's schema,
   stamping A's transform version again.
3. **A to B2.** On the same stack, B2's schema upgrade applies only the
   change that is not backward compatible, records it as such, and B2
   serves the publication through its changed SQL interface. Its ingestion
   and refresh also work. The title and year are then changed after the
   backup, and the changed snapshot values checked.
4. **A refuses B2's schema.** A's actual readiness query, executed directly
   as its application role without the schema guard, fails with
   `UndefinedColumn` (SQLSTATE 42703) on `title`, independently establishing
   the structural incompatibility. Switched back to A's images, A's schema
   upgrade exits 1 with its refusal in `docker compose logs schema-upgrade`,
   so `docker compose up --wait` fails and the read service does not start.
   Started alone (`--no-deps`), the read service answers `/readyz` and
   `/v0/publications` with 503 and the reason `schema_not_supported`, naming
   the change, and `/livez` with 200; an ingestion command exits 1 with the
   message and writes nothing.
5. **The rollback by restore.** `docker compose down --volumes`, then the
   restore of *Backup and restore* brings the backup of step 1 into a new
   volume with A's images: the schema upgrade finds the dump's record and
   nothing to do, the verification query counts what it counted before the
   dump, the rows carry A's transform version, and the publication is
   served with its original title and year restored.
6. **The published releases to A.** For the preceding release, and for the
   floor once it is another release (`DB/release_record.py preceding` and
   `floor`): its images by the digests of the release record, pulled
   without credentials when Docker lacks them, with the `compose.yaml` its
   application image carries; the demonstration, its rows stamped with the
   transform version its record names; a backup; the switch to A, whose
   schema upgrade applies A's schema changes to that database; and the
   image rollback to the published images, which serve the publication on
   the upgraded schema.

Every step but the reads — HTTP requests, the verification query, the
record of schema changes and the rows' transform versions read as the
superuser on PostgreSQL's socket, the containers' state and logs — is a
command of the operator's. Starting the read service and an ingestion
command without the schema upgrade, in step 4, shows the application's own
check, which an operator meets only when starting them by hand. No password
of the run occurs in any output it keeps or prints.

**Isolation and cleanup.** Every stack is a Compose project
`pubdex-upgrade-<run>-…`, never `db`; a project of that name that exists
already stops the run. A `compose.override.yaml` of the run's own, which
Compose reads with `compose.yaml`, puts the run label on every service,
volume and network. PostgreSQL is published on no host port, which the run
checks after every start, and the read service on 127.0.0.1 alone. Every
Docker command, `release.sh`'s and the pulls included, uses the run's own
client configuration, as in stage 10. On every exit, and after SIGINT,
SIGTERM or SIGHUP, the stage runs `docker compose down --volumes` in each
operator's directory, removes what carries a project's name or the run
label, the test releases' images and containers, and the temporary
directory with the secret files and the dumps, and confirms it as stage 6
does. The published images, base images, the SBOM tool and the build cache
stay.

[`../DEPLOYMENT.md`](../DEPLOYMENT.md), *Upgrading to a new release*, ends
with a table that names, for each block of its commands, the steps above
that ran it. `tests/test_deployment_guide.py`, in stage 1, checks that each
block has its row, in order; that the steps a row names exist, are the
steps this section numbers and the script's section comments number
alike, and run one of the block's commands; and that every `docker`
command of the block is one those steps run, word for word and in order,
the run's own options allowed between the words.

On the machine of its local record the stage took about 130 seconds with
Docker's build cache warm and the published images present: the three test
releases about 35 seconds, each switch between images 8 to 13 seconds.
While the floor is the preceding release, as for PubDex 0.2.0, whose floor
and preceding release are both 0.1.0, step 6 runs once. Not covered: an
upgrade from a PubDex release between the floor and the preceding one, a
rollback to a PubDex release other than the one upgraded from, which is not
supported, and a rollback by restore after an upgrade from a published
PubDex release, whose schema changes have all been backward compatible so
far.

## Running the suite elsewhere

Everything the suite checks lives in the repository's scripts, so a CI
definition on any forge is an adapter that installs the requirements and
calls `./run_ci_suite.sh`, as the GitHub workflow does. The runner itself,
though, has to provide the following; this is the whole contract, and the
last column says what happens without each part — the suite names the
missing part where it can tell, not everywhere:

| The runner provides | Because | Without it |
|---|---|---|
| Linux on x86_64 | stages 6 and 7 build the application image for the platform of the runner's Docker daemon, and only `linux/amd64` is built, tested and supported ([`DEPLOYMENT.md`](../DEPLOYMENT.md), *Supported platforms*) | only stages 9-11 check it, and refuse any other platform before they build anything; on another architecture the suite has never run, and stages 6 and 7 would test an image of another platform, which is not supported |
| Bash and standard POSIX tools (`od`, `mktemp`, `awk`, `sed`, `grep`, `seq`) | the scripts are Bash | the scripts fail; `timeout(1)` is optional, and where it is missing two offline lifecycle tests skip under the skip policy |
| GNU tar, gzip, `sha256sum` and `cmp` | `release.sh`, which stages 9-11 run, writes the source archive with GNU tar's reproducible options and gzip, and the checksum file with `sha256sum`; stage 10 compares its two builds with `cmp` | `release.sh`, and stage 10 before it, stops before it builds anything and names the tool |
| Python 3.11 with `.venv` at the repository root created by `./setup_venv.sh`: pip from `DB/requirements-pip.txt` and `DB/requirements-dev.txt`, fetched by `DB/fetch_verified.py` and installed from those files alone; for psycopg's C implementation, the default, a C compiler, Python's headers and libpq's headers with `pg_config` (`libpq-dev`), else `--psycopg python`; a system libpq 16 or later either way | every Python step runs `.venv/bin/python`; the setup is the one CI and the image build use | the suite stops before stage 1 and says how to create it; `setup_venv.sh` stops at a missing compiler or `pg_config`, and `DB/install_locked.py` at a libpq older than 16 |
| A Git checkout, and for `--base`/`--head` its complete history | the publishable file list comes from Git, and the range checks read commits | without a checkout the suite stops before stage 1; in a shallow clone stage 2 cannot read the commits and fails with status 2 |
| In that history, the commit whose `charts/pubdex/` is the chart of the preceding PubDex release, which the release record names | stage 9 installs that chart as revision 1 of each upgrade, read with `git archive` | stage 9 stops before it creates anything and says that a shallow clone lacks the commit |
| A Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2, with the buildx plugin, that the job's user can use | stages 3–7 start containers, stages 6 and 7 build the application image with BuildKit and start the deployment, stages 9-11 build both images and their test releases with BuildKit, and stage 11 starts the deployment too, which is supported on those Engines only: older ones start it too, but let other hosts reach the ports it publishes on 127.0.0.1 (`DB/lib/docker_versions.sh`) | the suite stops before stage 1 and says which; without buildx, `build_image.sh` stops stage 6 and says what to install |
| The Docker Compose plugin 2.24.0 or later (`docker compose`) | stages 6, 7 and 11 start the Compose deployment, whose health checks set `start_interval` | the suite stops before stage 1 and says what to install |
| A daemon that sees the checkout's directory and the temporary directory under the same paths | stages 3–5 bind-mount `DB/init/` into the container; stages 6, 7 and 11 take the schema from the image and bind-mount only their secret files from the temporary directory, and stage 7 its roster; stage 9's floor cluster mounts its audit policy from the temporary directory into its node; the SBOM tool of `release.sh`, in stages 9-11, reads the saved images from a bind mount of the run's temporary directory | the container initialises without the schema, and stage 3 stops at once and names this requirement; in stages 6 and 7 a secret file is missing and the stack does not start; in stage 9 the floor's API server does not start and step 28 fails; `release.sh` fails at its SBOM step |
| Ports that daemon publishes on 127.0.0.1 reachable from the job | stages 4 and 5 connect to the database on such a port, stages 6, 7 and 11 to the read service | the first database connection of stage 4 is refused |
| The `postgres:16` image, from the daemon's cache or a registry it can reach | the disposable databases | `docker run` fails in stage 3; another image can be named (*Reproducing a verified run*) |
| The base images the `Dockerfile` and `postgres.Dockerfile` name by digest, the Python packages of `DB/requirements-pip.txt` and `DB/requirements.txt`, and PostgreSQL's source, from the daemon's cache or the registry, package index and postgresql.org they come from | stages 6, 7 and 9-11 build the images | the build fails in stage 6 |
| The pinned helm, kubectl and kind under `.tools/`, which `./setup_cluster_tools.sh` installs over HTTPS from `get.helm.sh`, `dl.k8s.io` and `kind.sigs.k8s.io`, whose download of kind redirects to the kind project's release storage on GitHub | stages 8 and 9 use only these, never a helm, kubectl or kind on `PATH` or preinstalled on the runner | the suite stops before stage 1 and names the command that installs them; a tool of another hash or version stops its stage |
| The pinned SBOM tool, Syft's image by the digest `DB/lib/sbom_tool.sh` pins, which `./release.sh setup` pulls from Docker Hub without credentials | stages 9, 10 and 11 write an SBOM of each image of their test releases with it, never with a Syft on `PATH` | the suite stops before stage 1 and names the command; an image of another id stops `release.sh` before it builds anything |
| The published images of the preceding PubDex release and of the upgrade floor, by the digests the release record names, from the daemon's cache or, without credentials, their registry | stage 11 starts them on Compose and upgrades them to test release A | stage 11 fails at the pull and names the image; the upgrade from the published images is never skipped |
| `openssl` | stage 9 creates the CA and the certificate of its external PostgreSQL server | stage 9 stops before it creates anything |
| A Docker daemon that can run kind's node, a privileged container, and publishes the cluster's API server on 127.0.0.1, reachable from the job | stage 9 creates its clusters, one at a time, as containers of that daemon and talks to each through that port | kind fails in step 2 or 28, or the kubeconfig check refuses the cluster before any kubectl or helm call reaches it |
| The two kind node images by digest, from the daemon's cache or Docker Hub; from inside the clusters' nodes, without credentials, the registry of the published images, `ghcr.io` | stage 9's clusters, and revision 1 of every release, which runs the published images | step 2 or 28 fails to create a cluster, or step 3 fails and names the registry, or on the floor the install of step 32 does not become ready; the upgrade from the published images is never skipped |

These hold where the job runs directly on a machine with its own Docker
Engine: a workstation or VM, GitHub's hosted Ubuntu runners, a self-hosted
runner that runs jobs on its host — a Forgejo Runner with a host label
among them (*Forgejo Actions* below) — or a GitLab Runner with the shell
executor on such a machine whose user may use Docker. They do not hold, and
the suite does not support, a job that runs in a container talking to a
separate Docker daemon — the GitLab Docker executor with Docker-in-Docker or
with the host's socket mounted, an Actions runner of Forgejo or Gitea that
runs jobs in containers. How far the suite gets there depends on what the
job and the daemon share. Where the daemon cannot see the checkout, the
usual case, stage 3 stops at once and names that requirement. Where the
checkout's path is visible to the daemon but the ports it publishes are not
reachable from the job, stages 1 to 3 can pass and stage 4 fails at its
first database connection, with the connection error rather than a
diagnosis of its own.

**Other clusters.** Stages 8 and 9 need no access to an existing
Kubernetes cluster and take none, so the suite can run on a machine whose
user has a kubeconfig for other clusters. Stage 8 contacts no cluster: its
`KUBECONFIG` names a file that does not exist. Stage 9 talks only to the
kind cluster it creates, through the kubeconfig kind writes into the run's
temporary directory, which it checks first (*Isolation and cleanup*); it
never reads `~/.kube` or an inherited `KUBECONFIG`, and every kubectl and
helm call names that kubeconfig and the run's context. It changes no host
setting, such as the kernel's inotify limits that kind's known issues
mention; its single-node cluster ran with a limit of 128 inotify instances
per user. It deletes only its own cluster, network and image
tags, and never a network named `kind` or another kind cluster.

What has been demonstrated, and what has not:

- **Demonstrated:** the complete suite on Linux x86_64 with Docker Engine on the
  machine of the local records, with Python 3.11.5 — its five stages of task
  1b in [`TASK_1B.md`](../TASK_1B.md), all seven in [`TASK_2A.md`](../TASK_2A.md),
  and all nine, with Python 3.11.17 and the Helm chart's stages on kind
  clusters of Kubernetes 1.37.0 and 1.35.8, on 2026-11-16, on the tree of
  task-2b commit `a86d5e0` before it was committed and from a clean clone
  of that commit ([`TASK_2B.md`](../TASK_2B.md), *Local verification
  record*);
  all eleven stages on the working tree on 2026-11-24, with Python
  3.11.17, including the schema upgrade, test-release rehearsal and upgrades
  and rollbacks on Compose and Kubernetes ([`TASK_2C.md`](../TASK_2C.md),
  *Local verification record*; the final candidate's clean clone, public CI
  and publication remain pending);
  and there, the three script lines the GitLab example below had then —
  without its installation of pip — run by hand with the variables of a
  branch's first push, on the five-stage suite of task 1b. On Forgejo,
  the Forgejo workflow for a first push, a pull request and a push of one
  commit, with a Forgejo Runner executing the jobs on the same workstation as
  a private Forgejo 16.0.5 instance and with CPython 3.11.17: the five
  stages of task 1b on 2026-10-06 ([`TASK_1B.md`](../TASK_1B.md),
  *Self-hosted Forgejo evidence*), and all seven, the Compose stages
  included, for a local task-2a commit on 2026-10-29
  ([`TASK_2A.md`](../TASK_2A.md), *Self-hosted Forgejo evidence*). On
  GitHub's hosted `ubuntu-24.04` runner with the workflow's Python 3.11.17,
  the five stages of the task-1b commit in public CI on 2026-10-07
  ([`TASK_1B.md`](../TASK_1B.md), *Public-CI evidence*). All seven stages,
  including both Compose stages, passed on the same hosted runner platform
  with Python 3.11.18 for task-2a release source `57a72dc` on 2026-11-02
  ([`TASK_2A.md`](../TASK_2A.md), *Public-CI evidence*). All nine,
  including the Helm chart's stages on kind clusters, passed there with
  Python 3.11.18 and psycopg's C implementation for task-2b commit
  `a86d5e0` on 2026-11-17, in a pull-request run and in the push run on
  `main` ([`TASK_2B.md`](../TASK_2B.md), *Public-CI evidence*).
- **Not demonstrated:** the eleven stages on Forgejo — its workflow has run
  seven at most; a GitLab pipeline — the GitLab
  example has not run as one; a run on a Forgejo instance anyone can
  inspect — the Forgejo runs were on a private one; any architecture other
  than x86_64; macOS with Docker Desktop, and Windows through WSL 2.

An example for GitLab CI with the shell executor on such a machine:

```yaml
verify:
  variables:
    GIT_DEPTH: "0"   # the complete history, for the commit-range checks
  script:
    - ./setup_venv.sh --python python3.11
    - ./setup_cluster_tools.sh
    - ./release.sh setup
    - ./run_ci_suite.sh --base "${CI_MERGE_REQUEST_DIFF_BASE_SHA:-$CI_COMMIT_BEFORE_SHA}" --head "$CI_COMMIT_SHA"
```

On a merge request the range starts at the merge base; on a push, at the
previous tip of the branch; the first push of a branch reports an all-zero
SHA, for which the secrets check reads every commit reachable from the head.
What has been checked about it: `tests/test_ci_suite_script.py` runs its
script lines under Bash, as a shell executor does, with the variables GitLab
sets for each of those three pipelines, and proves that they install the
requirements in that order and hand the suite the expected range; and the
example's script lines of then, copied from this page, ran the complete
suite of task 1b, its five stages, on the machine of its local record with
the variables of a branch's first push set by hand. Those were three lines
that created `.venv` and installed into it; the installation of the pinned
pip came later, and since then all of it has become the one line of
`./setup_venv.sh`, which downloads the locked files itself and lets pip
install only from them; the lines that install the pinned cluster tools and
pull the pinned SBOM tool came with the stages that need them. The lines of
now have not run that way. That shows
the commands work on a machine that meets the contract; it is not a GitLab
pipeline run. Whether GitLab accepts the file, and whether a shell-executor
runner's checkout, Python installation and access to Docker meet the
contract, remain unverified. Without a commit range the job reduces to the
steps every runner shares: install the requirements and the pinned cluster
tools, pull the pinned SBOM tool, then run `./run_ci_suite.sh`. Like the
two workflows, the example has no step that publishes and names no
credential.

### Forgejo Actions

[`.forgejo/workflows/db-tests.yml`](../.forgejo/workflows/db-tests.yml) is
the adapter for Forgejo Actions. Forgejo reads `.forgejo/workflows/` and,
while that directory exists, ignores `.github/workflows/`; GitHub does not
read `.forgejo/`, so each forge runs its own adapter and neither runs the
other's. On every push and pull request the job checks the repository out
with its complete history and without stored credentials, creates `.venv`
with `./setup_venv.sh --python python3.11`, installs the pinned helm,
kubectl and kind with `./setup_cluster_tools.sh`, pulls the pinned SBOM tool
with `./release.sh setup`, and runs
`./run_ci_suite.sh --base <event base> --head <event commit>` under a
60-minute limit: the same four steps and the same limit as on GitHub, and
nothing else. Like that job it publishes nothing and reads no repository
secret.

It runs on a Forgejo Runner that has the label `pubdex-ci-host:host`, so the
job executes directly on the runner's machine. That machine has to meet the
contract above, and has to provide `python3.11` on its `PATH`, with a C
compiler and libpq's headers for psycopg's C implementation, and Node.js for
the checkout action. A runner that maps the label to a container image
(`pubdex-ci-host:docker://…`) runs jobs in containers and does not meet it.

Where it differs from the GitHub adapter, and why:

- **Pull requests.** Forgejo runs a pull request on its head commit and
  creates no merge commit; its event names the merge base. The range
  therefore starts at the merge base: the transform-version guard compares
  the version the pull request started from with the one it proposes, and
  the secrets check reads the commits the pull request adds. On GitHub the
  target branch's tip is the right base, because GitHub tests a merge commit
  on top of it. An event that names no merge base gives no range, so the
  secrets check reads every commit reachable from the head. Pushes work as on
  GitHub: the range starts at the previous tip, and the first push of a
  branch or tag reports an all-zero SHA.
- **Actions.** The checkout action is `actions/checkout` at the commit the
  GitHub workflow pins, fetched by absolute URL from Forgejo's mirror at
  `data.forgejo.org`, so the instance's default action source does not
  matter. No action installs Python, because `actions/setup-python`
  downloads its interpreters from GitHub; the runner provides `python3.11`,
  and each run's log shows the version it used.
- **The automatic token.** Forgejo documents `permissions:` as ignored, so
  the workflow sets none. Forgejo's automatic token can write to the
  repository, is limited to it and to the run, and is present in every
  step's environment; `persist-credentials: false` keeps it out of the
  checkout, and no step of the job or script of the suite reads it.
- **Ending a job.** Forgejo Runner ends a job that is cancelled or reaches
  its time limit with SIGKILL, which no cleanup can intercept: the
  Docker-backed stage running at that moment leaves its container and volume
  behind, and their labels find them (*Isolation and cleanup*); in stage 9,
  its kind cluster `pubdex-cluster-<run id>`, the network `<cluster>-net`
  and the image tags it created; in stages 9 to 11, the images of their
  test releases, which carry the test release's version as a label, and
  the temporary directory; and in stage 11 also its Compose projects
  `pubdex-upgrade-<run>-…`, with the run label
  `pubdex.upgrade-acceptance.run`.

At run time the job needs the Forgejo instance, `data.forgejo.org` for the
action, the Python package index, the downloads of the cluster tools,
Docker Hub for the SBOM tool's image, which `./release.sh setup` pulls, and
the registry of the `postgres:16` image, of the base images the two
Dockerfiles name by digest and of kind's two node images unless the Docker
daemon has them already; and `ghcr.io`, the registry of the published
images, from the runner's daemon for stage 11 and from inside kind's nodes
for stage 9. For stages 6, 7 and 9-11 the runner's machine needs Docker
Engine 28.0.1 or later, except 28.2.0 to 28.3.2, with the buildx plugin and
the Compose plugin 2.24.0 or later, and for stage 9 a daemon that runs
kind's node, as the contract above says. Two of these are GitHub's: kind's
download redirects to the kind project's release storage on GitHub, and the
published images are on GitHub's container registry. The checkout action's
code is GitHub's too, served by Forgejo's mirror; nothing else comes from
GitHub.

What has been checked, without a Forgejo instance:
`tests/test_ci_suite_script.py` runs the workflow's shell steps, with its
expressions evaluated against synthetic contexts carrying the fields of
Forgejo's events for a push, the first push of a branch and a pull request
whose target branch has moved past the merge base, and proves that they
install the requirements, pull the SBOM tool and hand the suite the expected
range; it also pins the label, the source and commit of the action, the same
checkout commit, setup and time limit as the GitHub workflow, and that the
job's commands are the four above and no other, with no secret, token or
login named. The schema check of Forgejo Runner
13.2.0 (`forgejo-runner validate`) accepts the workflow.

What has run on a Forgejo instance: on 2026-10-06, on a private Forgejo
16.0.5 instance with Forgejo Runner 13.2.0 executing the jobs on the same
workstation, one commit passed the workflow for the first push of a branch,
a pull request — whose run received the pull request's merge base, as
described above — and a push ([`TASK_1B.md`](../TASK_1B.md), *Self-hosted
Forgejo evidence*). That commit's suite had five stages. On 2026-10-29, on
the same instance and runner, the local task-2a commit `a0219be7` passed
the workflow for the same three events, with all seven stages, the
acceptance test of the deployment and the test of the operator's workflow
included, and with pip installed by `DB/install_pip.py`
([`TASK_2A.md`](../TASK_2A.md), *Self-hosted Forgejo evidence*). That
commit predates `setup_venv.sh`, `DB/fetch_verified.py`, psycopg's C
implementation and the PostgreSQL image built without XML: no Forgejo run
covers them yet. That instance is not publicly accessible.

### Reproducing a verified run

The suite is functionally repeatable: the same commands run the same checks
against the same fixtures, and every database is created fresh. That does
not make two runs identical. What the repository fixes, and what it leaves
to the host:

| Input | Fixed by | Left to the host |
|---|---|---|
| Python packages | `DB/requirements-dev.txt`: every package at an exact version and by the hashes of its files, dependencies included, installed from binary wheels only, but psycopg-c, built from its source archive by hash with the build tools of `DB/requirements-build.txt` | the compiler and the libpq psycopg-c is built against and loads: the host's, printed by the setup and by every suite run |
| pip | `./setup_venv.sh`, in both workflows and every documented setup: pip 26.2.2 by `DB/install_pip.py` from its hash-checked wheel, then the lock by `DB/install_locked.py`, pip with `--no-index` from the files `DB/fetch_verified.py` fetched, without the host's pip configuration and, where the host permits a network namespace, without network | whether the host permits that namespace; the setup's log says |
| Python | the GitHub workflow uses 3.11.18, the Python of the application image | elsewhere, the Forgejo workflow included, the `python3.11` the host has; nothing checks its patch version |
| PostgreSQL | stages 3–5: nothing by default, `postgres:16` follows the newest 16.x image; stages 6 and 7: the PubDex PostgreSQL image, built from the checkout by `postgres.Dockerfile` — PostgreSQL 16.16 from its source by hash, without XML | `DISPOSABLE_PG_IMAGE` names the image of stages 3–5 — the PubDex PostgreSQL image built with `./build_image.sh`, say — `SMOKE_PG_IMAGE`, `TASK_1A_PG_IMAGE` and `INTEGRATION_PG_IMAGE` that of one, a digest included |
| The images of stages 6 and 7 | the `Dockerfile` and `postgres.Dockerfile`: their bases by digest, every locked file and the PostgreSQL source by hash, downloaded in a stage of their own by `DB/fetch_verified.py` and installed and compiled without network; the build time is in their labels | Debian's packages, from its signed archive when the step that installs them runs, otherwise from the host's build cache, and the package index and postgresql.org serving the files; a file that matches no hash fails the build; `--image` and `--postgres-image` test given images instead |
| helm, kubectl, kind and the cluster of stage 9 | `DB/lib/cluster_tools.sh`: helm 4.3.1, kubectl 1.36.6 and kind 0.33.0 by SHA-256, installed by `./setup_cluster_tools.sh`; kind's two node images, Kubernetes 1.37.0 and 1.35.8 with their network plugin, by digest; the preceding PubDex release, revision 1 of every upgrade, by the digests of its images in the release record (`releases/`) and its chart at the commit the record names | the kernel and Docker Engine the nodes run on |
| The SBOM tool and the test releases of stages 9-11 | Syft's image by digest (`DB/lib/sbom_tool.sh`), pulled by `./release.sh setup` and run without network; each test release's version is new on every run, its source archive and chart package are the same, byte for byte, when built again from the same files with the same GNU tar, gzip and helm | the images' bytes, whose labels record the build's time; the host's tar and gzip, which the provenance record names |
| The published images of stage 11 | the preceding PubDex release's and the floor's, by the digests of the release record | — |
| GitHub actions and runner | actions by commit, runner image `ubuntu-24.04` | the runner image itself still receives updates |
| Forgejo action and runner | the checkout action by commit | the runner, its machine and its Node.js |
| Kernel, Docker Engine, Docker Compose, Bash, Git, system tools | — | always the host's |

So that a later run can be compared with an earlier one, every run of
`./run_ci_suite.sh` prints what it used before the first stage — system,
Bash, Python, pip, Git and Docker Engine versions — and each Docker-backed
stage the PostgreSQL image, its registry digest and the server version.

To rerun with the inputs of the local record in [`TASK_1B.md`](../TASK_1B.md),
from a clean clone:

```bash
./setup_venv.sh --python python3.11
DISPOSABLE_PG_IMAGE=postgres:16@sha256:21f6013073bc6b92830a2129570e2f5ec42a6c734b5a985a41e83aa58f54c3c1 \
  ./run_ci_suite.sh
```

These commands fix pip and every Python package, by version and hash, and
the PostgreSQL image of stages 3–5, by digest; the daemon has to find that image in its
cache or a registry it can reach. Everything else you supply, and to match
the record you supply its versions: Python 3.11.5 as `python3.11` — the
commands take whatever interpreter that name finds and do not check its
patch version — Docker Engine 29.8.2, Git 2.34.1, Bash 5.1, and Linux
(Ubuntu 22.04, kernel 6.8) with its system tools. The record is not a run
with the workflow's Python, 3.11.17 then and 3.11.18 now; only a CI run
exercises that interpreter. The record installed psycopg-binary, whose
wheel brought its own libpq; `setup_venv.sh` builds psycopg-c against the
host's libpq instead, and refuses one older than 16, such as a Python
installation's own older copy that the pure-Python implementation would
load.
A run with other host versions is still the same verification, and its own
report shows where it differs. Bit-for-bit reproduction is not claimed.

## Local smoke workflow

- Create the development environment with `../setup_venv.sh` (from `DB/`) when there is none, and again, after removing `../.venv`, when a lock has changed.
- Run the default offline check suite first: `./run_checks.sh` from the
  repository root (`../run_checks.sh` from `DB/`). To run only the unit tests,
  `./run_tests.sh` still works.
- Before pushing, run the complete suite, `./run_ci_suite.sh` from the
  repository root: it is exactly what public CI runs. With `--base <the
  commit your work starts from>` it also checks the commits you are about to
  push, as CI will.
- For a narrower loop after a schema change in `DB/schema/`, a change to
  the ingestion path or the export, run `./run_migration_smoke.sh`,
  `./run_task_1a_demo.sh` or `./run_disposable_integration.sh` on their
  own; after a change to the image, `compose.yaml` or the service,
  `../run_stack_acceptance.sh`; after a schema change, also
  `../run_upgrade_acceptance.sh`; after a change to `../release.sh` or to
  what a PubDex release holds, `../run_release_rehearsal.sh`.
- Run at least one importer in `--dry-run` mode using a synthetic fixture.
- If a change touches SQL assumptions, verify the target table/column exists before running a write command.

## Release verification corrections

Stage 1 runs `tests/test_verify_release.py` against synthetic downloads: all
five required files, a missing file, corrupt bytes and a missing checksum
entry, with `IMAGES` absent. This check needs only Python. It does not
download from a release page. Saved-image fixtures cover matching and
mismatched config digests for classic and containerd builder stores, plus
the format-1 classic fallback and the explicit limitation of a legacy
containerd record without a config digest. `tests/test_release_provenance.py`
checks generation, digest mismatches and validation of both record formats.

Provenance format 2 adds `images.<role>.config_digest` while retaining the
store-dependent `id`. Stage 10 reads real saved-image config bytes during
build and verification; its comparisons exclude config digests because
creation labels change those bytes. Containerd archive behaviour is covered
by fixtures and the [Engine export implementation](https://github.com/moby/moby/blob/v28.5.1/daemon/containerd/image_exporter.go),
which requests a Docker-compatible `manifest.json`. A run on a classic-store
daemon provides no runtime evidence for containerd.

`tests/test_release_script.py` runs publication only against stubbed registries.
It forces a write failure while creating the completion record, verifies
cleanup with no `DIGESTS` marker left, and retries using both already-published
matching images without another push. The final file must contain exactly
two complete lines. Existing completion records are never overwritten.
