# Task 1a — public-safe repository, licensing and clean-clone baseline

**Published verification record.**

Initial candidate prepared: 2026-09-10. The amended candidate was verified on
2026-09-25 using the commands below and published the same day at
<https://github.com/CCU-UKN/pubdex>, with evidence tag
`public-baseline-1a`.

Public availability is established by anonymous access to that canonical
repository and its evidence tag. The fresh-checkout verification recorded
below establishes that the published baseline passes its documented checks.
Publication and repository verification do not assert external acceptance of
task 1a.

This is the acceptance record for the first funded result of the PubDex
project: what was required, which files carry it, and the command anyone can
run to check it. Project page:
[PubDex at NLnet](https://nlnet.nl/project/PubDex/).

Three states are kept apart deliberately, and this document marks each row for
which of them applies:

- **repository implementation** — complete in the 2026-09-25 verification
  snapshot, and checkable by the commands below;
- **public availability** — complete on 2026-09-25, evidenced by anonymous
  access to the canonical repository and `public-baseline-1a` tag, plus
  successful fresh-checkout verification;
- **external acceptance** — not asserted here; confirmation is still required
  for how "query the baseline API" in the agreed wording maps onto the
  interface described under *What "query the baseline interface" means here*.

## What is funded work and what is not

PubDex grew out of a private prototype for publication reporting whose
original development predates the grant. Its metadata ingestion, provenance,
deduplication, curation, export and maintenance code is the **starting point**
of this repository. Prototype functionality included in this baseline is not
claimed as funded work, including routine prototype changes that happen to
fall inside the curated snapshot.

The funded result is turning that prototype into a repository that other people
can obtain, license, verify and run: curation and public-safety review,
licensing, a secret-free example configuration, synthetic fixtures, the removal
of institution-specific behaviour from the application code, a deterministic
clean-clone demonstration, the fixes that demonstration exposed, and the public
documentation you are reading.

The prototype's history is **not** published. The agreed plan explicitly did
not require publishing it, and it contains personal data and operational
material that has no place in a public repository.

## Acceptance matrix

| Required | Where it lives | How to verify | Expected result |
|---|---|---|---|
| A public repository under the PubDex name | [canonical repository](https://github.com/CCU-UKN/pubdex) | open it without signing in and select tag `public-baseline-1a` | one public root commit at the evidence tag |
| A curated copy of the code and documentation | [`DB/`](DB/), [`README.md`](README.md), [`DB/README.md`](DB/README.md), [`DB/SCRIPTS.md`](DB/SCRIPTS.md), [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) | `./run_checks.sh` | the default offline check suite passes; every Markdown link resolves inside the repository; the ingestion package integrates ORCID, Crossref, OpenAlex, DataCite, DBLP and Semantic Scholar, plus manual CSV/DOI import, and the offline fixture suite covers each of them |
| MIT and CC BY licences | [`LICENSE`](LICENSE), [`LICENSE-CC-BY-4.0`](LICENSE-CC-BY-4.0) | read them, and the Licensing section of [`README.md`](README.md) | code under MIT, documentation and synthetic examples under CC BY 4.0, with the boundary stated |
| A secret-free example configuration | [`.env.example`](.env.example) | read it against the variables the walkthrough sets | every value is a documented placeholder; the pre-publication review (below) reported no credential or private-host finding in the published baseline tree |
| A project description | [`README.md`](README.md) | read it | what PubDex does, how it is laid out, how to start |
| A public progress record | [`CHANGELOG.md`](CHANGELOG.md) | read it | one entry per funded result, with verification commands |
| The grant acknowledgement | Funding section of [`README.md`](README.md) | read it | the NGI0 Commons Fund acknowledgement, the EU grant agreement number and the project number |
| From a clean clone: bootstrap the database | [`DB/init/`](DB/init/), [`DB/run_migration_smoke.sh`](DB/run_migration_smoke.sh) | `(cd DB && ./run_migration_smoke.sh)` | `PASS: schema bootstraps from empty DB and the runtime patch is re-runnable.` |
| From a clean clone: load a synthetic roster | [`DB/tests/fixtures/golden/demo_roster_minimal.csv`](DB/tests/fixtures/golden/demo_roster_minimal.csv) | `./DB/run_task_1a_demo.sh` step 3 | three fictional people imported |
| From a clean clone: run the ORCID and Crossref fixtures | [`DB/people_pubs/sync/fixture_demo.py`](DB/people_pubs/sync/fixture_demo.py), [`DB/tests/fixtures/golden/`](DB/tests/fixtures/golden/) | `./DB/run_task_1a_demo.sh` step 4 | one publication stored from the ORCID payload, then enriched from the Crossref payload |
| From a clean clone: query the baseline interface | [`DB/init/010_ingestion_runtime_schema.sql`](DB/init/010_ingestion_runtime_schema.sql) | `./DB/run_task_1a_demo.sh` steps 6-7 | the stored record with cumulative provenance, and the same work in the canonical list once attribution is configured |
| From a clean clone: produce an export | [`DB/people_pubs/sync/export_publications.py`](DB/people_pubs/sync/export_publications.py) | `./DB/run_task_1a_demo.sh` step 8 | a nonempty CSV containing the expected synthetic publication |
| No secrets, private rosters, restricted-source data, database dumps or machine-local state | the whole tree | the categories below record a completed pre-publication review | reviewed before publication; the review combined automated scanning with inspection, and is a record rather than a public scanner you can re-run |

## The deterministic demonstration

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r DB/requirements-dev.txt
./DB/run_task_1a_demo.sh
```

One command performs the whole clean-clone sequence and checks its own result.
The numbers below are the stages it prints (`[1/8]` to `[8/8]`), which the
acceptance matrix above refers to:

1. starts a disposable PostgreSQL container — unique name, loopback-only port,
   removed again afterwards — with `DB/init/` mounted;
2. waits until the schema has bootstrapped from `DB/init/`;
3. imports the synthetic roster of fictional people;
4. ingests `DB/tests/fixtures/golden/orcid_work_minimal.json` through the
   production ingestion path, then enriches the same work from
   `DB/tests/fixtures/golden/crossref_work_minimal.json`;
5. configures a fictional institutional attribution rule;
6. queries the stored record and shows its cumulative provenance;
7. queries the canonical list;
8. runs the real export implementation and verifies its contents.

It **makes no ORCID, Crossref or other metadata-provider request**: its input
and its expected publication data come entirely from repository fixtures. It
uses neither Docker Compose nor any existing database. Docker still needs the
PostgreSQL image and the interpreter needs the installed dependencies, so it is
reproducible on supported Docker environments once those are available.

The expected result is one publication, DOI **`10.5555/pubdex-fixture-001`**
("Synthetic Collective Behaviour Paper", 2024, *Journal of Synthetic
Metadata*), provenance `crossref+orcid`, with a **nonempty** export:

```csv
pub_id,doi,title,year,venue,source_of_truth,internal_authors
1,10.5555/pubdex-fixture-001,Synthetic Collective Behaviour Paper,2024,Journal of Synthetic Metadata,crossref+orcid,Ada Example
```

`internal_authors` lists Ada Example alone. Both authors of the work are
linked to person records and both stay visible in `biblio.authorships_curated`,
but the bundled roster marks Ada internal and Bert external, and only an
internal person is reported as one of the organisation's own authors. The
demonstration fails if that distinction is lost in either direction.

Every person named is invented, and the DOI and ORCID iDs are synthetic
identifiers used only by the bundled fixtures; the demonstration never resolves
any of them over the network.

## What "query the baseline interface" means here

The interface this result delivers is the **documented PostgreSQL schema and
its read views** — `biblio.publications` for everything ingested with its
provenance, `biblio.publications_canon` for the canonical institutional list,
`biblio.authorships_curated` for authorship after curator decisions. The
demonstration above exercises exactly those, and
[`QUICKSTART.md`](QUICKSTART.md) shows the same queries against an
installation of your own.

To say it plainly: **task 1a demonstrates read access through the PostgreSQL
interface. The versioned network API is planned for task 3a** under the agreed
plan, and is deliberately not part of this result. Nothing in this repository
claims to serve one, and no command here starts a network endpoint.

## Verification commands

| Command | What it proves |
|---|---|
| `./run_checks.sh` | The default offline check suite. The default invocation runs compilation, Markdown consistency and the offline unit/fixture suite. In pre-commit and CI modes (`--staged`, or `--base`/`--head`) it additionally runs the transform-version guard. No database, and it makes no network request of its own. |
| `(cd DB && ./run_migration_smoke.sh)` | The schema bootstraps from an empty PostgreSQL database and the runtime patch re-applies cleanly, in a disposable container. |
| `./DB/run_task_1a_demo.sh` | The clean-clone acceptance path above, end to end and offline. |
| `(cd DB && PEOPLE_PUBS_INTEGRATION_DSN=... ./run_integration_tests.sh)` | The disposable-database integration suite, including the end-to-end fixture path and the primary-email policy. |

Running this complete verification story from a clean checkout in portable
public CI is the next funded result, task 1b, recorded in
[`TASK_1B.md`](TASK_1B.md): `./run_ci_suite.sh` runs the commands above in one
go, together with a check for committed secrets and local configuration, and
the workflow in `.github/workflows/` is an adapter that installs the
dependencies and calls it. Whether that workflow has passed in public CI is
recorded there, not here.

## Public-safety review before publication

This section records a completed review, not a reproducible public tool: the
scanner used is a maintainer-side heuristic that encodes patterns specific to
the private project, so it is deliberately not published. A generic public
check for committed secrets and local configuration is part of task 1b
([`check_secrets_and_local_config.py`](check_secrets_and_local_config.py), see
[`TASK_1B.md`](TASK_1B.md)); it guards the tree from then on and does not
replace the review recorded here.

The published baseline tree was scanned and reviewed against the following
categories:

- credentials, tokens, connection strings with passwords and private keys;
- personal data: contact addresses, rosters, member lists (example addresses
  use `example.org`);
- restricted-source data from metadata providers whose terms do not allow
  redistribution;
- database dumps, exports and any real institutional records;
- machine-local state: absolute paths, private network addresses, institutional
  hostnames and deployment configuration;
- institution-specific behaviour hard-coded into the application, which is now
  configuration instead;
- references to parts of the private project that are out of scope here.

Synthetic fixtures replaced every real record, and the demonstration and tests
run entirely on them.

[`LICENSE`](LICENSE) contains the stated copyright attribution, which names an
institution. That is attribution rather than application configuration, and it
is unaffected by the removal of institution-specific behaviour from the code.
