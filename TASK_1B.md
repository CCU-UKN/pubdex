# Task 1b — forge-portable test suite and public CI

**Local verification record. Public-CI evidence pending.**

Implemented, and verified locally on 2026-10-05 with the commands below. The
task also requires the suite to pass in public CI from a clean checkout. That
result exists only once this work has been pushed and the workflow has passed
for that commit; it is recorded under *Public-CI evidence* when it does, and
nothing here asserts it before then. Separately, on 2026-10-06 the commit
`ee9dee35214776cf020a7b1f2e33683d4246b182` passed the Forgejo workflow on a
private, self-hosted Forgejo instance, recorded under *Self-hosted Forgejo
evidence*; that instance is not publicly accessible, and those runs do not
stand in for public CI. Neither local nor public verification asserts
external acceptance of task 1b.

Project page: [PubDex at NLnet](https://nlnet.nl/project/PubDex/). The first
funded result is recorded in [`TASK_1A.md`](TASK_1A.md).

## The agreed task

> Extend the current CI setup into a forge-portable test suite: unit tests,
> migration from an empty database, the integration suite on disposable
> PostgreSQL, offline source fixtures, and checks for committed secrets and
> local configuration. The same suite has to run locally through one
> documented command and in public CI from a clean checkout. Helm tests belong
> to 2b.

Three states are kept apart, and each row below says which one it concerns:

- **repository implementation** — complete in the working tree verified on
  2026-10-05, and checkable with the commands below;
- **public CI** — pending: a green run of the `DB tests` workflow from a clean
  checkout of the pushed commit, to be recorded under *Public-CI evidence*;
- **external acceptance** — not asserted here.

## What task 1b adds to the task-1a baseline

The published task-1a commit (tag `public-baseline-1a`) already held most of
the tests and the Docker-backed checks; task 1b turns them into one suite
that runs the same way locally and in CI, and adds what that needs. Counts
are tests collected;
[`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *Coverage by purpose*,
breaks them down.

| Already present in the task-1a baseline | Added or strengthened by task 1b |
|---|---|
| 193 unit and policy tests and 53 offline source-fixture tests (ORCID, Crossref, OpenAlex, DataCite, DBLP, Semantic Scholar), run by `./run_checks.sh` | the same tests, now in a cleared environment that no exported setting or `DB/.env` reaches, under a network guard, and under a skip policy that fails a run on any skip it does not name |
| 92 integration tests, run against a database the user starts (`DB/run_integration_tests.sh` with a DSN) | [`DB/run_disposable_integration.sh`](DB/run_disposable_integration.sh), which starts, binds and removes a PostgreSQL for the run and fails on any skip; 10 more integration tests: reruns of the DataCite, DBLP and Semantic Scholar importers, and the database roles `app_writer`, `app_readonly` and `maintenance`, with the grant on the schema holding the extensions that those roles need, which `DB/init/` now issues |
| The migration smoke test and the task-1a demonstration, each on its own disposable container | both with labelled resources, a confirmed cleanup, random masked passwords and a readiness wait that stops at once on a broken bootstrap, in one shared lifecycle file |
| A GitHub workflow running `./run_checks.sh` alone | [`run_ci_suite.sh`](run_ci_suite.sh), all five stages in one command; the workflow only installs the requirements and calls it |
| A manual pre-publication review of the tree, not a public tool | [`check_secrets_and_local_config.py`](check_secrets_and_local_config.py), over the publishable tree and the commits a push or pull request adds |
| 7 offline tests of the migration smoke script | tests of all this tooling: 399 offline tests that need no Docker, and 5 integration tests of the network guard |

## The one command

From a clean clone of the repository:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r DB/requirements-dev.txt
./run_ci_suite.sh
```

It needs Bash, Python 3.11, a Git checkout and a Docker Engine on the same
machine that the user can reach and that can obtain `postgres:16`. Stages 3–5
bind-mount `DB/init/` from the checkout, and stages 4 and 5 connect to the
port the database publishes on 127.0.0.1. A runner whose job runs in a
container next to a separate Docker daemon does not provide this; where that
daemon cannot see the checkout, the suite stops in stage 3 and says so.
[`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md),
*Running the suite elsewhere*, gives the complete runner contract. The suite
runs five stages in this order and stops at the first one that fails, with
that stage's exit status:

| Stage | Component | What it proves |
|---|---|---|
| 1 | [`run_checks.sh`](run_checks.sh) | compilation, Markdown consistency, the offline unit and source-fixture tests and, given `--base`, the transform-version guard over that commit range |
| 2 | [`check_secrets_and_local_config.py`](check_secrets_and_local_config.py) | no committed secret or local configuration of a kind its rules recognise in the publishable tree and, given `--base`, in the files the range's commits add or change; a pattern check with documented limits, not proof that no secret exists |
| 3 | [`DB/run_migration_smoke.sh`](DB/run_migration_smoke.sh) | the schema bootstraps from an empty PostgreSQL database and the runtime patch re-applies cleanly, also to a cluster without the `maintenance` role |
| 4 | [`DB/run_task_1a_demo.sh`](DB/run_task_1a_demo.sh) | the task-1a clean-clone demonstration still passes end to end, offline |
| 5 | [`DB/run_disposable_integration.sh`](DB/run_disposable_integration.sh) | the complete integration suite on its own disposable PostgreSQL, and the publication-view and role tests again after the runtime patch has been re-applied to that database |

Public CI runs the same command. The workflow
[`.github/workflows/db-tests.yml`](.github/workflows/db-tests.yml) checks out
the full history on pull requests and on pushes to `main`, with read-only
permissions and no persisted credentials, installs `DB/requirements-dev.txt`
into `.venv`, and calls `./run_ci_suite.sh --base <event base> --head <event
commit>` under a 30-minute limit, so that stage 2 also reads the commits the
pull request or push adds. It defines no test, rule or database step of its
own and needs no repository secret; its actions are pinned by commit and its
runner image, Python and pip versions are fixed. A runner on another forge
reproduces the result with the same two steps — install the requirements, run
the command — if it meets the runner contract. For Forgejo the repository
carries that adapter:
[`.forgejo/workflows/db-tests.yml`](.forgejo/workflows/db-tests.yml) runs the
same two steps on a self-hosted runner with the host label `pubdex-ci-host`,
and starts a pull request's range at its merge base, because Forgejo tests a
pull request's head commit rather than a merge commit
([`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *Forgejo Actions*).
That adapter has run on a private, self-hosted Forgejo instance: the commit
`ee9dee3` passed it for a first push, a pull request and a push
(*Self-hosted Forgejo evidence*). No run on a publicly accessible second
forge has been made.

`./run_checks.sh` remains the fast offline command and the pre-commit hook;
nothing on the commit path needs Docker.

## Acceptance matrix

| Required | Where it lives | How to verify | Expected result |
|---|---|---|---|
| Unit tests | [`DB/tests/`](DB/tests/) | stage 1, or `./run_checks.sh` alone | `649 passed, 110 skipped` on Linux as an ordinary user. The skips are the 107 integration tests, which need a database and run in stage 5, and three guardrails parametrized over discovery jobs, of which the shipped job list has none — each named in [`DB/tests/expected_skips.py`](DB/tests/expected_skips.py), where any other skip, an expected failure or a module skipped while collected fails the stage. Elsewhere the counts may include its two platform skips (running as root, no `timeout(1)`) |
| Offline source fixtures | [`DB/tests/fixtures/golden/`](DB/tests/fixtures/golden/), `DB/tests/test_*_fixture_ingest.py`, [`DB/tests/integration/test_backfill_rerun_integration.py`](DB/tests/integration/test_backfill_rerun_integration.py) | stages 1 and 5 | the bundled payloads of all six providers parse offline into the expected records; on the disposable database the DataCite, DBLP and Semantic Scholar importers rerun from them with identical results |
| No provider access from the tests | [`DB/tests/network_guard.py`](DB/tests/network_guard.py), installed by [`DB/tests/conftest.py`](DB/tests/conftest.py) | stages 1 and 5 | offline tests reach no network and no database, integration tests only their disposable database, in the pytest process and the Python processes it starts; a guard inside Python, not operating-system isolation ([`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *Network access during tests*) |
| Migration from an empty database | [`DB/init/`](DB/init/), [`DB/run_migration_smoke.sh`](DB/run_migration_smoke.sh) | stage 3, or `(cd DB && ./run_migration_smoke.sh)` | `PASS: schema bootstraps from empty DB and the runtime patch is re-runnable.` |
| The integration suite on disposable PostgreSQL | [`DB/run_disposable_integration.sh`](DB/run_disposable_integration.sh), [`DB/tests/integration/`](DB/tests/integration/) | stage 5, or `./DB/run_disposable_integration.sh` | `107 collected: 107 passed, 0 failed, 0 errors, 0 skipped`, then `25 collected: 25 passed, 0 failed, 0 errors, 0 skipped` after the re-application, then `PASS: integration suite, runtime-patch re-application and view and role rerun on a disposable database.` |
| Checks for committed secrets and local configuration | [`check_secrets_and_local_config.py`](check_secrets_and_local_config.py) | stage 2, or `.venv/bin/python check_secrets_and_local_config.py [--base <rev> [--head <rev>]]` | `secrets and local-configuration check ok (171 files)` for this tree; with a range, also the number of commits read. A finding names rule, file or commit, and line, never the matched text; a shallow clone or an unreadable object fails the check (status 2) instead of passing it. Scope and limits: [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) |
| The same suite locally, through one documented command | [`run_ci_suite.sh`](run_ci_suite.sh), documented in [`README.md`](README.md) and [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) | `./run_ci_suite.sh` | `PASS: all 5 stages of the complete verification suite passed in <n>s.` |
| The same suite in public CI from a clean checkout | [`.github/workflows/db-tests.yml`](.github/workflows/db-tests.yml) | open the `DB tests` run of the pushed commit without signing in | **pending** — a green run whose log ends with the same `PASS` line; see *Public-CI evidence* |
| Forge portability | the tracked scripts above; the workflows hold no check; [`.forgejo/workflows/db-tests.yml`](.forgejo/workflows/db-tests.yml) for Forgejo | on a runner that meets the contract in [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *Running the suite elsewhere*: install the requirements, run `./run_ci_suite.sh` | the same five stages with the same results. Demonstrated on the machine of the local record below, and on Forgejo: on a private, self-hosted Forgejo 16.0.5 instance with Forgejo Runner 13.2.0 on the same workstation, the Forgejo workflow passed for a first push, a pull request and a push of `ee9dee3` — see *Self-hosted Forgejo evidence*; not publicly accessible. Not demonstrated: GitHub's runner with the workflow's Python 3.11.17, until public CI has run, and a GitLab pipeline: the GitLab CI example there is tested offline and its commands ran on the local machine |
| Tests of the suite's own tooling | [`DB/tests/test_ci_suite_script.py`](DB/tests/test_ci_suite_script.py), [`DB/tests/test_disposable_integration_script.py`](DB/tests/test_disposable_integration_script.py), [`DB/tests/test_migration_smoke_script.py`](DB/tests/test_migration_smoke_script.py), [`DB/tests/test_secrets_and_local_config.py`](DB/tests/test_secrets_and_local_config.py), [`DB/tests/test_offline_isolation.py`](DB/tests/test_offline_isolation.py), [`DB/tests/test_config_dotenv.py`](DB/tests/test_config_dotenv.py), [`DB/tests/test_network_guard.py`](DB/tests/test_network_guard.py), [`DB/tests/test_expected_skips.py`](DB/tests/test_expected_skips.py) | part of stage 1; they need no Docker | included in the stage-1 count |

The task-1a demonstration (stage 4) is not a task-1b requirement in itself; it
runs in the suite so that the first funded result stays verified by every
change.

## Isolation and cleanup

Only synthetic fixtures and disposable databases are used: no stage contacts
a metadata provider, uses Docker Compose, reads `DB/.env`, or touches an
existing database. The offline tests run in a cleared environment; every
pytest run is under the network guard and the skip policy. Each
Docker-backed stage creates one uniquely named, labelled container and volume,
publishes PostgreSQL on loopback at most, keeps its random password off every
command line and out of its output, removes only its own run's resources
after success, failure or a signal, and fails when it cannot confirm that
nothing is left. [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md),
*Isolation and cleanup*, states each guarantee and the tests that pin it.

## Decisions and evidence still open

- **Public CI** — the green `DB tests` run of the pushed commit, below.
- **A second forge** — the Forgejo workflow passed on a private,
  self-hosted Forgejo instance, with its runner on the same workstation
  (*Self-hosted Forgejo evidence*). That instance is not publicly
  accessible, so the runs supplement public CI and do not replace it. The
  GitLab CI example is tested offline and its commands ran on the local
  machine; whether GitLab accepts it and a GitLab runner meets the runner
  contract is unverified.
- **Field precedence of the enrichment backfills** — DataCite, DBLP and
  Semantic Scholar replace values that sources ranked above them supplied,
  where OpenAlex defers to the trust order. That is current behaviour, which
  characterization tests pin without approving; whether it should change is
  an open decision ([`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md),
  *Characterization of the enrichment precedence*).

## Local verification record

Verified on 2026-10-05 on Linux (Ubuntu 22.04, kernel 6.8), Bash 5.1,
Python 3.11.5, pip 26.2.1, Git 2.34.1 and Docker Engine 29.8.2 with the
`postgres:16` image at digest
`sha256:21f6013073bc6b92830a2129570e2f5ec42a6c734b5a985a41e83aa58f54c3c1`
(PostgreSQL 16.10), against the working tree that makes up this result:

| Command | Result |
|---|---|
| `.venv/bin/python check_markdown_links.py` | `markdown link check ok` |
| `.venv/bin/python check_secrets_and_local_config.py` | `secrets and local-configuration check ok (171 files)` |
| `.venv/bin/python check_secrets_and_local_config.py --base '' --head HEAD` | every commit reachable from `HEAD` read — the published task-1a commit, whose two accepted synthetic test values are its only findings — and no finding reported |
| `./run_ci_suite.sh` | all five stages passed: 649 passed and 110 skipped offline, every skip one the policy names; the secrets check clean; the migration smoke `PASS`, its last re-application without the `maintenance` role included; the demonstration `PASS`; 107 integration tests, then 25 view and role tests after re-applying the runtime patch, none skipped; no generated password and no connection string in the output |
| the GitLab CI example's three script lines, run by hand with the variables of a branch's first push | the same five stages passed, stage 2 reading every commit reachable from `HEAD` |
| `docker ps -a --filter label=<label>` and `docker volume ls --filter label=<label>`, for each of the three labels | nothing left after the runs |

These results come from one machine. The repository fixes the exact package
versions in `DB/requirements-dev.txt` and, in the workflow, the actions by
commit, the runner image release and the Python and pip versions. The
PostgreSQL image follows `postgres:16` unless an override names a digest,
and the kernel, Docker Engine and system tools come from the host; every run
prints the versions it used. This record ran on Python 3.11.5. The Forgejo
runs below used CPython 3.11.17 built from the python.org source; the GitHub
workflow's Python 3.11.17 on `ubuntu-24.04` has not run the suite yet, and
the public CI run will be the first.
[`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *Reproducing a verified
run*, gives commands that fix this record's pip, package and PostgreSQL image
versions, and lists the host versions a reproduction has to supply itself;
bit-for-bit reproduction is not claimed.

## Public-CI evidence

*To be filled in after the push; nothing below is asserted yet.*

- Commit: *pending*
- `DB tests` workflow run, viewable without signing in: *pending*
- Result: *pending*

## Self-hosted Forgejo evidence

Recorded on 2026-10-06. Runs of
[`.forgejo/workflows/db-tests.yml`](.forgejo/workflows/db-tests.yml) on a
private Forgejo instance on the maintainer's workstation, with a Forgejo
Runner on the same workstation that executes each job directly on it under
the host label `pubdex-ci-host`. The instance is not publicly accessible, so
this is the maintainer's record of those runs, not CI evidence anyone can
open; it complements *Public-CI evidence* and does not replace it. It was
written after the runs, in a later commit: the runs tested the commit named
here, not the commit that records them.

- Commit tested: `ee9dee35214776cf020a7b1f2e33683d4246b182`, whose parent is
  the published task-1a commit `95a13daf3e37`; it carries
  `.forgejo/workflows/db-tests.yml` as blob `37e0f9ac5bb1`.
- Forgejo 16.0.5, from its rootless container image, and Forgejo Runner
  13.2.0.
- Runner: on the same Linux workstation as the instance (Ubuntu 22.04,
  kernel 6.8), running each job on the host as an ordinary user with a
  cleared environment, one job at a time, with Docker Engine 29.8.2, Git
  2.34.1, CPython 3.11.17 built from the python.org source, pip 26.2.1, and
  Node.js 24.21.0 for the checkout action, which came from
  `data.forgejo.org` at the pinned commit. PostgreSQL 16.10 in all three
  database stages (`postgres:16`, digest `sha256:21f6013073bc`).

Each run was started by Forgejo for its event, tested that commit, and
succeeded at its first attempt:

| Trigger | Range the suite received | Range checks |
|---|---|---|
| first push of a new branch | all-zero base | no range for the transform-version guard; the secrets check read both commits reachable from the head |
| pull request into the branch holding the task-1a commit | from the pull request's merge base, the task-1a commit | version 1.5.0 → 1.5.1 accepted; the secrets check read the one commit the pull request adds |
| push moving that branch from the task-1a commit to the tested one | from the previous tip, the task-1a commit | as for the pull request |

In each run all five stages passed: 649 offline tests passed and 110
skipped, every skip one the policy names; the secrets check over 171 files;
the migration smoke and the demonstration; 107 integration tests, then 25
after re-applying the runtime patch, none skipped. The suite took 85 to
88 seconds. Afterwards no container or volume carrying the stages' labels
was left on the workstation.

## Not part of this result

- **Helm tests belong to task 2b.** Nothing here packages or tests a Helm
  chart.
- Equally outside task 1b: publishing container images, Compose packaging,
  release automation, the versioned HTTP API (task 3a), authentication and
  production deployment.
