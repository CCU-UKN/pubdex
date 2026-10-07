# Changelog

Every funded result of this project is recorded here as its own entry: what it
delivered, the files that carry it, and how it can be verified.

PubDex starts from a private publication-reporting prototype whose original
development predates the grant. That prototype is the starting point of this
repository, and prototype functionality included here is not claimed as funded
work; the entries below describe the funded results built on top of it.
Background: [Origins and funding](README.md).

## Forge-portable test suite — verified locally 2026-10-05, public CI pending

Task 1b: the complete test suite as one forge-portable command that runs
locally and in public CI from a clean checkout. Implemented and verified
locally on 2026-10-05 with the commands under *Verification*. The public-CI
result — the workflow passing from a clean checkout of the pushed commit — is
pending and is not asserted by this entry, and neither is external acceptance.
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
run, and the GitHub workflow's Python 3.11.17 has not run the suite yet. Nothing
carrying the suites' labels was left in Docker afterwards. The
public-CI run and its commit will be recorded in [`TASK_1B.md`](TASK_1B.md)
once the workflow has run for the pushed commit. Helm tests belong to task 2b
and are not part of this result.

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
entry above; its public-CI evidence is pending. The versioned HTTP API is task
3a; this baseline serves reads through the documented PostgreSQL schema
instead, as [`TASK_1A.md`](TASK_1A.md) explains.
