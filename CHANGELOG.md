# Changelog

Every funded result of this project is recorded here as its own entry: what it
delivered, the files that carry it, and how it can be verified.

PubDex starts from a private publication-reporting prototype whose original
development predates the grant. That prototype is the starting point of this
repository, and prototype functionality included here is not claimed as funded
work; the entries below describe the funded results built on top of it.
Background: [Origins and funding](README.md).

## Initial public baseline — candidate prepared 2026-09-10

Task 1a: public-safe repository, licensing and clean-clone baseline.
Initial candidate prepared 2026-09-10. The amended candidate was verified on
2026-09-25 using the commands under *Verification*. Planned
evidence tag: `public-baseline-1a`. Public availability is established by
anonymous access to the canonical repository and that tag, plus successful
fresh-clone verification; this entry does not assert publication or external
acceptance. Requirement-by-requirement record: [`TASK_1A.md`](TASK_1A.md).

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

Task 1b will provide the complete forge-portable CI suite, running the whole
verification story — including integration testing against a disposable
PostgreSQL instance — from a clean checkout in public CI. The versioned HTTP
API is task 3a; this baseline serves reads through the documented PostgreSQL
schema instead, as [`TASK_1A.md`](TASK_1A.md) explains.
