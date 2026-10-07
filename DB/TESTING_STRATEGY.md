# Testing Strategy

## How to run the checks

Two commands at the repository root cover everything, and both work from any
working directory:

| Command | What it runs | Needs |
|---|---|---|
| [`./run_checks.sh`](../run_checks.sh) | The **default offline check suite**: compilation of `people_pubs` and its tests, the Markdown link and path check, the offline unit and source-fixture tests, and — when a commit range or the staged index is supplied — the transform-version guard. It is the pre-commit hook and the everyday command. Its tests run in a cleared environment (see *Isolation and cleanup*), so they stay offline and complete whatever the caller exports. | `.venv` with `requirements-dev.txt`, in a Git checkout; no database, no Docker, no network |
| [`./run_ci_suite.sh`](../run_ci_suite.sh) | The **complete verification suite**, the command public CI runs: the five stages below, in order, stopping at the first failure with that stage's exit status. `--base <sha> [--head <sha>]` adds the transform-version guard over that commit range and the check of the files its commits add or change; an empty or all-zero base (the first push of a branch) gives the guard no range, as in `run_checks.sh`, and makes the secrets check read every commit reachable from the head. | the same `.venv`, a Git checkout (with the complete history for a range), and a Docker Engine on the same machine — see *Running the suite elsewhere* |

The stages of `./run_ci_suite.sh`:

| Stage | Component command | What it proves |
|---|---|---|
| 1 | `./run_checks.sh`, with the range when one is given | everything the offline suite above proves |
| 2 | [`check_secrets_and_local_config.py`](../check_secrets_and_local_config.py), with the range when one is given | no committed secret or local configuration of a kind its rules recognise in the publishable tree and, given a range, in the files its commits add or change — a pattern check with documented limits, not proof that no secret exists; see *Committed secrets and local configuration* below |
| 3 | [`DB/run_migration_smoke.sh`](run_migration_smoke.sh) | the schema bootstraps from an empty PostgreSQL database and the runtime patch re-applies cleanly |
| 4 | [`DB/run_task_1a_demo.sh`](run_task_1a_demo.sh) | the clean-clone acceptance path end to end — bootstrap, synthetic roster, bundled ORCID and Crossref payloads, queries and a verified export — with no metadata-provider request |
| 5 | [`DB/run_disposable_integration.sh`](run_disposable_integration.sh) | the complete integration suite on its own disposable PostgreSQL, then the publication-view and role tests again after the runtime patch has been re-applied to that database |

Public CI ([`.github/workflows/db-tests.yml`](../.github/workflows/db-tests.yml))
checks the repository out with its history, installs `requirements-dev.txt`
into `.venv` and calls `./run_ci_suite.sh --base <event base> --head <event
commit>`. No test, rule, database step or assertion lives in the workflow, so
a green local run and a green CI run mean the same thing, and a runner on
another forge reproduces the result with the same two steps, provided it
meets the contract in *Running the suite elsewhere* below. Every run starts by
printing the versions the repository does not pin (system, Bash, Python, pip,
Git, Docker Engine), and each Docker-backed stage the PostgreSQL image digest
and server version it used.
[`.forgejo/workflows/db-tests.yml`](../.forgejo/workflows/db-tests.yml) is the
same adapter for Forgejo Actions, for a self-hosted runner that runs the job
on its host; *Forgejo Actions*, under *Running the suite elsewhere*, says what
it needs and where it differs.

### Isolation and cleanup

- Only the synthetic fixtures under `tests/fixtures/golden/` and disposable
  databases are used. No stage contacts a metadata provider, uses Docker
  Compose, reads `DB/.env`, or touches an existing database.
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
| `./run_migration_smoke.sh` | Schema bootstrap from an empty database plus re-application of the runtime patch, in a disposable container. |
| `./run_task_1a_demo.sh` | The clean-clone acceptance path end to end against a disposable database. |
| `./run_disposable_integration.sh [pytest arguments]` | The integration suite on a disposable PostgreSQL; arguments such as `-k attribution` narrow the main run. See *Running the integration suite in a disposable container* below. |
| `PEOPLE_PUBS_INTEGRATION_DSN=... ./run_integration_tests.sh` | The integration tests against a disposable database you started yourself. |

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

### The suite's own machinery, and why it stays

Several parts of the suite are written for this repository rather than taken
from a framework. Each covers something the standard tools leave open, and
each has the tests listed above:

- **The disposable-database lifecycle** — unique labelled resources, a
  random password kept off every command line, masked output, a cleanup that
  removes only its own run's resources and confirms it, signal handling —
  exists because the scripts run next to other Docker work, on developer
  machines as well as runners, and must never touch or leak into anything
  else; Docker Compose is not used, because a Compose project named after
  the directory can join an existing installation's containers. The smoke
  test, the demonstration and the integration runner share one
  implementation of it, the sourced file
  [`lib/disposable_postgres.sh`](lib/disposable_postgres.sh), so a fix or
  a stricter check, such as the fail-fast readiness wait, applies to all
  three at once.
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
- Offline fixture-backed ingestion tests (`tests/test_orcid_fixture_ingest.py`, `tests/test_crossref_fixture_ingest.py`) lock down ORCID work-summary/contributor extraction and Crossref DOI/date/preprint/author parsing so plain CI never depends on live API availability; `tests/test_roster_and_export.py` does the same for `add_people` roster validation and the deterministic publication CSV export.
- `./run_migration_smoke.sh` (needs Docker) proves the schema bootstraps from an empty database and that `init/010_ingestion_runtime_schema.sql` re-applies cleanly, with the login roles' `USAGE` — never `CREATE` — on the schema holding the extensions in place after each step. A last re-application runs after the `maintenance` role has been removed from the run's own cluster, as for a database restored into a cluster without it: the patch, which grants that role `USAGE` only where it exists, has to succeed there and must not create the role. It uses a uniquely named, labelled container and volume with no host port and removes only those two, so it can run next to other instances and in parallel. It is stage 3 of `../run_ci_suite.sh`; run it on its own whenever `DB/init/` changes.
- `./run_task_1a_demo.sh` (needs Docker) runs the clean-clone acceptance path end to end against its own disposable database: bootstrap, synthetic roster, the bundled ORCID payload, Crossref enrichment of the same work, a stored-record query, a canonical-list query and an export whose contents it verifies. It contacts no metadata provider, never uses Docker Compose, and removes only the container and volume it created. `tests/test_fixture_demo.py` pins the offline half (the fixture sources open no socket; the payloads still parse into the expected publication, provenance and authorship shapes) and `tests/integration/test_fixture_demo_integration.py` the database half. It is stage 4 of `../run_ci_suite.sh`.
- `tests/test_migration_smoke_script.py` runs that script offline against a stub `docker` and pins its resource handling: unique names, cleanup registered before creation, no pre-delete, removal of exactly what it created after success, a failed start, a bounded bootstrap timeout, a container exit or an interrupt, and an abort on a name collision. For the migration smoke and the acceptance demonstration alike — the demonstration runs from a scratch checkout whose interpreter records every Python step instead of connecting anywhere — it also proves that a second interrupt arriving while the container is being removed does not cut the cleanup short; that a cleanup which fails — a removal Docker refuses, one that reports success but leaves the resource, a daemon that no longer answers, or (for the demonstration) a temporary directory that cannot be removed — is reported with the commands that find the leftovers and fails an otherwise successful run, while a failed start or a signal keeps its own exit status; and that the superuser password is random per run, independent of the run id, handed to Docker by name so that no `docker` argument holds it, absent from the output, and replaced by `<password>` in a container log tail that contained it. From the recorded steps of a whole demonstration run it also proves: no step takes `--dsn`, no Python, `docker` or `env` command line holds the password, and each step's environment holds exactly the isolated settings — the database bound through `PEOPLE_DB_DSN` and `PG*`, and `PEOPLE_PUBS_SKIP_DOTENV=1` — with nothing inherited from a polluted caller. For both it proves that a container which finished initialising without running `init/` — an empty mount, as when the Docker daemon cannot see the checkout — stops the run at once with that diagnosis, and that `DISPOSABLE_PG_IMAGE` sets the image while each script's own variable takes precedence and the run reports the image it used.
- `tests/test_disposable_integration_script.py` does the same for `./run_disposable_integration.sh`, against a stub `docker` and a stub test command: unique owned resources, no pre-delete, a loopback-only port Docker picks, `init/` mounted read-only, the password kept off every command line — Docker's, the test command's and any `env` launcher's — and masked in the test output, in the container diagnostics and in the error output of a failed re-application, a test environment cleared of inherited variables and exported shell functions in which every DSN and `PG*` setting names the disposable database, `PEOPLE_PUBS_SKIP_DOTENV=1` is set and no `PYTEST_ADDOPTS` survives, the suite, the runtime-patch re-application and the rerun of the view and role tests in that order, the tests' own exit status preserved, a skipped or empty run treated as a failure, a bounded readiness wait, an immediate stop on an exited container, a failed init script or an init directory the daemon could not see, the image variables, a refused non-loopback port, an abort on a name collision, and removal of exactly what it created — including its private temporary directory — after success, failure, SIGINT, SIGTERM, SIGHUP, an external timeout or a second interrupt during the cleanup. A cleanup that fails in any of the ways above, or a temporary directory that cannot be removed, is reported and fails an otherwise successful run, while a test failure or a signal keeps its own exit status.
- `tests/test_network_guard.py` proves the guard offline: a connection to an external address, a host-name lookup of a provider (also through a direct `connect()`, which would otherwise resolve the name first), a request through the package's own DataCite client, a loopback connection, a datagram, a connection through the lower-level `_socket` class and psycopg connections to loopback, remote or default-socket servers are all refused at once; Unix-domain sockets keep working; the integration policy admits its one database and nothing else; a child Python process is refused too, and a refusal it swallows is still recorded; and a test that swallows a refusal fails. In child processes it also proves, against synthetic loopback listeners that count what reaches them, that psycopg connections through `psycopg.connect`, `Connection.connect` and `AsyncConnection.connect` are refused before the server is contacted under the offline policy, and reach the allowed server — and only that one — under a database policy; that a child loads psycopg only when it imports it and starts normally without it; that a database refusal a grandchild swallows fails the enclosing test run; that a test run started by a test, which holds a second copy of the guard, imports psycopg wrapped by both copies; and that a guard which cannot be set up, or a psycopg it cannot wrap, stops the child or fails its import and is recorded. `tests/integration/test_network_guard_integration.py` proves on the disposable database that psycopg, `people_pubs.db.connection`, a plain socket and a child process — over a socket and through psycopg, synchronous and asynchronous — reach it, while another port, another host or a provider is refused.
- `tests/test_expected_skips.py` pins each rule of the skip policy on the tests it names and the condition it states, with unrelated tests, other tests of the same module and false conditions (not root, `timeout(1)` present, a database configured) as counterparts, and that every test it names exists. Through whole test runs that load `tests/conftest.py` as a plugin it proves that expected skips keep a run green; that a module skipped while it is collected — by `pytest.skip(..., allow_module_level=True)`, `pytest.importorskip` or `unittest.SkipTest` — fails a run whose other test passes, which plain pytest would let pass; that unrelated tests and false conditions cannot use an exception; that an integration skip with a database configured, or outside `tests/integration/`, and an expected failure fail the run; and that pytest's own failure statuses stay as they are.
- `tests/test_ci_suite_script.py` runs `../run_ci_suite.sh` offline, with every stage replaced by a recording stub: the five stages in order from any working directory, the first failure stopping the run with its own status, `--base`/`--head` reaching the offline stage and the secrets check unchanged and no other stage (the all-zero and empty first-push bases included), the environment report, usage errors, the diagnostics for a missing interpreter, missing requirements, missing Git or Docker and an unreachable daemon, and what a terminal interrupt, an external timeout or a signal to the suite alone does. It also pins the public CI workflow as a thin adapter that only installs the requirements and calls the suite, with its actions pinned by commit and its runner image, Python and pip versions fixed; it runs the shell steps of the GitHub and the Forgejo workflow under Bash, with their expressions evaluated against synthetic event contexts of each forge for a push, the first push of a branch and a pull request, proving that they install the requirements and hand the suite the range each forge's pull request needs — the target branch's tip on GitHub, which tests a merge commit, and the merge base on Forgejo, which tests the head commit, also when the target branch has moved on; it pins the Forgejo workflow to the documented host label, to actions from Forgejo's mirror by absolute URL and commit, and to the same checkout commit and pip version as the GitHub workflow; and it runs the script lines of the GitLab CI example in *Running the suite elsewhere* under Bash, as a shell executor would, with the variables GitLab sets for a push, the first push of a branch and a merge-request pipeline, proving that they install the requirements and hand the suite the expected range.
- `tests/test_offline_isolation.py` runs the real `./run_tests.sh`, `../run_checks.sh` and `../run_ci_suite.sh` from a scratch checkout whose interpreter only records how it was called, with test DSNs, `PEOPLE_DB_DSN`, `PG*` and `POSTGRES_*` settings, `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, `PYTHONPATH` and `PEOPLE_PUBS_SKIP_DOTENV=0` exported: standalone and as stage 1 of the complete suite, pytest starts with none of them, with `PEOPLE_PUBS_SKIP_DOTENV=1` and no narrowing argument, while a commit range still reaches the transform-version guard and an all-zero base still counts as no range. `tests/test_config_dotenv.py` pins the opt-out itself on a `.env` file in a temporary directory: without it the file fills only missing values, with it nothing is read, also at import time.
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
3. It runs `./run_integration_tests.sh` in an environment from which every
   exported variable and function has been removed and in which
   `PEOPLE_PUBS_INTEGRATION_DSN`, `PEOPLE_DB_TEST_DSN`, `PEOPLE_DB_DSN` and the
   libpq settings (`PGHOST`, `PGHOSTADDR`, `PGPORT`, `PGUSER`, `PGPASSWORD`,
   `PGDATABASE`, with the service and password files pointing nowhere) all
   name the disposable database, and `PEOPLE_PUBS_SKIP_DOTENV=1` keeps
   `DB/.env` unread, so no inherited variable or local file can direct a test
   elsewhere. The shell exports these values itself before it `exec`s the
   command, so none of them is ever on a command line. Extra arguments go to
   pytest; `PYTEST_ADDOPTS` from the caller does not.
4. It re-applies `init/010_ingestion_runtime_schema.sql` to that database and
   reruns `tests/integration/test_publication_views_integration.py` and
   `tests/integration/test_application_roles_integration.py`, so the read
   views and the grants of the database roles are also proven on a
   database the runtime patch, which re-creates views and re-issues grants,
   has been applied to twice.

Every test has to run: a skipped test, or a run in which no test ran at all,
fails even when pytest itself passed, so a broken database binding cannot pass
as an empty green run. The exit status is the tests' own on a test failure, 1
on any other failure, and 130, 143 or 129 after SIGINT, SIGTERM or SIGHUP.
However the run ends, short of SIGKILL, the cleanup removes the container and
volume carrying this run's label, and the run's private temporary directory,
and then confirms that nothing is left; if something is, or Docker cannot
say, it prints the `docker` commands that find it, and a run that had passed
fails with status 1, while a failed one keeps its own status. The superuser password is generated
per run, reaches Docker and the test command only through their environment,
never through a command line, is never printed or written to a file, and is
masked in the test output, in the container diagnostics and in the error
output of a failed re-application.

Safety net (added 2026-07-13 after a real near-miss):
`tests/integration/conftest.py` exports `PEOPLE_DB_DSN` to the
integration DSN before any test imports `people_pubs`, so a code path that
accidentally falls back to the default connection resolution (env →
`DB/.env`) lands in the disposable database instead of a real one. Tests must
still pass explicit DSNs; this guard only changes the failure mode of a
missed patch from "writes to a real database" to "wrong rows in the
disposable one". The network guard (see *Network access during tests*) now
refuses a psycopg connection to any other server outright.

## Running the suite elsewhere

Everything the suite checks lives in the repository's scripts, so a CI
definition on any forge is an adapter that installs the requirements and
calls `./run_ci_suite.sh`, as the GitHub workflow does. The runner itself,
though, has to provide the following; this is the whole contract, and the
last column says what happens without each part — the suite names the
missing part where it can tell, not everywhere:

| The runner provides | Because | Without it |
|---|---|---|
| Bash and standard POSIX tools (`od`, `mktemp`, `awk`, `sed`, `grep`, `seq`) | the scripts are Bash | the scripts fail; `timeout(1)` is optional, and where it is missing two offline lifecycle tests skip under the skip policy |
| Python 3.11 with `DB/requirements-dev.txt` installed into `.venv` at the repository root | every Python step runs `.venv/bin/python` | the suite stops before stage 1 and says how to create it |
| A Git checkout, and for `--base`/`--head` its complete history | the publishable file list comes from Git, and the range checks read commits | without a checkout the suite stops before stage 1; in a shallow clone stage 2 cannot read the commits and fails with status 2 |
| A Docker Engine the job's user can use | stages 3–5 start PostgreSQL containers | the suite stops before stage 1 |
| A daemon that sees the checkout's directory under the same path | stages 3–5 bind-mount `DB/init/` into the container | the container initialises without the schema, and stage 3 stops at once and names this requirement |
| Ports that daemon publishes on 127.0.0.1 reachable from the job | stages 4 and 5 connect to the database on such a port | the first database connection of stage 4 is refused |
| The `postgres:16` image, from the daemon's cache or a registry it can reach | the disposable databases | `docker run` fails in stage 3; another image can be named (*Reproducing a verified run*) |

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

What has been demonstrated, and what has not:

- **Demonstrated:** the complete suite on Linux with Docker Engine on the
  machine of the local record in [`TASK_1B.md`](../TASK_1B.md), with
  Python 3.11.5; and there, the three script lines of the GitLab example
  below, run by hand with the variables of a branch's first push. On Forgejo,
  the Forgejo workflow for a first push, a pull request and a push of one
  commit, with a Forgejo Runner executing the jobs on the same workstation as
  a private Forgejo 16.0.5 instance and with CPython 3.11.17
  ([`TASK_1B.md`](../TASK_1B.md), *Self-hosted Forgejo evidence*).
- **Not demonstrated:** GitHub's hosted runner with the workflow's Python
  3.11.17, until the first public CI run; a GitLab pipeline — the GitLab
  example has not run as one; a run on a Forgejo instance anyone can
  inspect — the Forgejo runs were on a private one; macOS with Docker
  Desktop, and Windows through WSL 2.

An example for GitLab CI with the shell executor on such a machine:

```yaml
verify:
  variables:
    GIT_DEPTH: "0"   # the complete history, for the commit-range checks
  script:
    - python3.11 -m venv .venv
    - .venv/bin/python -m pip install -r DB/requirements-dev.txt
    - ./run_ci_suite.sh --base "${CI_MERGE_REQUEST_DIFF_BASE_SHA:-$CI_COMMIT_BEFORE_SHA}" --head "$CI_COMMIT_SHA"
```

On a merge request the range starts at the merge base; on a push, at the
previous tip of the branch; the first push of a branch reports an all-zero
SHA, for which the secrets check reads every commit reachable from the head.
What has been checked about it: `tests/test_ci_suite_script.py` runs its
script lines under Bash, as a shell executor does, with the variables GitLab
sets for each of those three pipelines, and proves that they install the
requirements and hand the suite the expected range; and the same three lines,
copied from this page, ran the complete suite on the machine of the local
record with the variables of a branch's first push set by hand. That shows
the commands work on a machine that meets the contract; it is not a GitLab
pipeline run. Whether GitLab accepts the file, and whether a shell-executor
runner's checkout, Python installation and access to Docker meet the
contract, remain unverified. Without a commit range the job reduces to the
two steps every runner shares: install the requirements, then run
`./run_ci_suite.sh`.

### Forgejo Actions

[`.forgejo/workflows/db-tests.yml`](../.forgejo/workflows/db-tests.yml) is
the adapter for Forgejo Actions. Forgejo reads `.forgejo/workflows/` and,
while that directory exists, ignores `.github/workflows/`; GitHub does not
read `.forgejo/`, so each forge runs its own adapter and neither runs the
other's. On every push and pull request the job checks the repository out
with its complete history and without stored credentials, creates `.venv`
with `python3.11`, installs pip 26.2.1 and `DB/requirements-dev.txt`, and runs
`./run_ci_suite.sh --base <event base> --head <event commit>` under a
30-minute limit: the same two steps as on GitHub, and nothing else.

It runs on a Forgejo Runner that has the label `pubdex-ci-host:host`, so the
job executes directly on the runner's machine. That machine has to meet the
contract above, and has to provide `python3.11` on its `PATH` and Node.js for
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
  checkout.
- **Ending a job.** Forgejo Runner ends a job that is cancelled or reaches
  its time limit with SIGKILL, which no cleanup can intercept: the
  Docker-backed stage running at that moment leaves its container and volume
  behind, and their labels find them (*Isolation and cleanup*).

At run time the job needs the Forgejo instance, `data.forgejo.org` for the
action, the Python package index, and the registry of the `postgres:16` image
unless the Docker daemon has it already. Nothing is fetched from GitHub; the
checkout action's code is GitHub's, served by Forgejo's mirror.

What has been checked, without a Forgejo instance:
`tests/test_ci_suite_script.py` runs the workflow's shell steps, with its
expressions evaluated against synthetic contexts carrying the fields of
Forgejo's events for a push, the first push of a branch and a pull request
whose target branch has moved past the merge base, and proves that they
install the requirements and hand the suite the expected range; it also pins
the label, the source and commit of the action, and the same checkout commit
and pip version as the GitHub workflow. The schema check of Forgejo Runner
13.2.0 (`forgejo-runner validate`) accepts the workflow.

What has run on a Forgejo instance: on 2026-10-06, on a private Forgejo
16.0.5 instance with Forgejo Runner 13.2.0 executing the jobs on the same
workstation, one commit passed the workflow for the first push of a branch,
a pull request — whose run received the pull request's merge base, as
described above — and a push ([`TASK_1B.md`](../TASK_1B.md), *Self-hosted
Forgejo evidence*). That instance is not publicly accessible.

### Reproducing a verified run

The suite is functionally repeatable: the same commands run the same checks
against the same fixtures, and every database is created fresh. That does
not make two runs identical. What the repository fixes, and what it leaves
to the host:

| Input | Fixed by | Left to the host |
|---|---|---|
| Python packages | `DB/requirements-dev.txt`: every package at an exact version, dependencies included (versions, not hashes) | — |
| pip | the GitHub and Forgejo workflows install pip 26.2.1; the commands below do the same | any other run uses the pip its `.venv` has |
| Python | the GitHub workflow uses 3.11.17 | elsewhere, the Forgejo workflow included, the `python3.11` the host has; nothing checks its patch version |
| PostgreSQL | nothing by default: `postgres:16` follows the newest 16.x image | `DISPOSABLE_PG_IMAGE` names the image of all three Docker-backed stages, `SMOKE_PG_IMAGE`, `TASK_1A_PG_IMAGE` and `INTEGRATION_PG_IMAGE` that of one, a digest included |
| GitHub actions and runner | actions by commit, runner image `ubuntu-24.04` | the runner image itself still receives updates |
| Forgejo action and runner | the checkout action by commit | the runner, its machine and its Node.js |
| Kernel, Docker Engine, Bash, Git, system tools | — | always the host's |

So that a later run can be compared with an earlier one, every run of
`./run_ci_suite.sh` prints what it used before the first stage — system,
Bash, Python, pip, Git and Docker Engine versions — and each Docker-backed
stage the PostgreSQL image, its registry digest and the server version.

To rerun with the inputs of the local record in [`TASK_1B.md`](../TASK_1B.md),
from a clean clone:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install pip==26.2.1
.venv/bin/python -m pip install -r DB/requirements-dev.txt
DISPOSABLE_PG_IMAGE=postgres:16@sha256:21f6013073bc6b92830a2129570e2f5ec42a6c734b5a985a41e83aa58f54c3c1 \
  ./run_ci_suite.sh
```

These commands fix the pip version, the version of every Python package and
the PostgreSQL image, by digest; the daemon has to find that image in its
cache or a registry it can reach. Everything else you supply, and to match
the record you supply its versions: Python 3.11.5 as `python3.11` — the
commands take whatever interpreter that name finds and do not check its
patch version — Docker Engine 29.8.2, Git 2.34.1, Bash 5.1, and Linux
(Ubuntu 22.04, kernel 6.8) with its system tools. The record is not a run
with the workflow's Python 3.11.17; only a CI run exercises that interpreter.
A run with other host versions is still the same verification, and its own
report shows where it differs. Bit-for-bit reproduction is not claimed.

## Local smoke workflow

- Install/update DB-local dev dependencies with `../.venv/bin/python -m pip install -r requirements-dev.txt` when the venv is missing test packages.
- Run the default offline check suite first: `./run_checks.sh` from the
  repository root (`../run_checks.sh` from `DB/`). To run only the unit tests,
  `./run_tests.sh` still works.
- Before pushing, run the complete suite, `./run_ci_suite.sh` from the
  repository root: it is exactly what public CI runs. With `--base <the
  commit your work starts from>` it also checks the commits you are about to
  push, as CI will.
- For a narrower loop after a change to `DB/init/`, the ingestion path or the
  export, run `./run_migration_smoke.sh`, `./run_task_1a_demo.sh` or
  `./run_disposable_integration.sh` on their own.
- Run at least one importer in `--dry-run` mode using a synthetic fixture.
- If a change touches SQL assumptions, verify the target table/column exists before running a write command.
