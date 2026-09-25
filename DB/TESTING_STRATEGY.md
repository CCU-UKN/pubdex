# Testing Strategy

## How to run the checks

There are four commands, and only the first needs neither Docker nor a
database.

| Command | Scope |
|---|---|
| `./run_checks.sh` (repository root) | The **default offline check suite**: compilation of `people_pubs` and its tests, the Markdown link and path check, the offline unit and fixture tests, and — when a commit range or the staged index is supplied — the transform-version guard. |
| `./run_migration_smoke.sh` | Schema bootstrap from an empty PostgreSQL database plus idempotent re-apply of the runtime patch, in a disposable container. |
| `./run_task_1a_demo.sh` | The clean-clone acceptance path end to end against a disposable database: bootstrap, synthetic roster, bundled ORCID and Crossref payloads, queries and a verified export. Contacts no metadata provider. |
| `PEOPLE_PUBS_INTEGRATION_DSN=... ./run_integration_tests.sh` | The disposable-database integration suite; see *Running the integration suite in a disposable container* below. |

CI checks the repository out, installs the development requirements and invokes
`run_checks.sh`, so a green local run and a green CI run mean the same thing —
for the offline suite. The other three commands are **not** part of it yet, and
`run_checks.sh` passing does not mean they would; run them when you touch
`DB/init/`, the ingestion path or the export.

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
- Golden fixtures live under `tests/fixtures/golden/` and are intentionally tiny, synthetic examples for manual CSV, Crossref, and ORCID payload shapes, plus a safe demo roster and the publication-export golden file.
- Offline fixture-backed ingestion tests (`tests/test_orcid_fixture_ingest.py`, `tests/test_crossref_fixture_ingest.py`) lock down ORCID work-summary/contributor extraction and Crossref DOI/date/preprint/author parsing so plain CI never depends on live API availability; `tests/test_roster_and_export.py` does the same for `add_people` roster validation and the deterministic publication CSV export.
- `./run_migration_smoke.sh` (needs Docker) proves the schema bootstraps from an empty database and that `init/010_ingestion_runtime_schema.sql` re-applies cleanly. It uses a uniquely named, labelled container and volume with no host port and removes only those two, so it can run next to other instances and in parallel. Run it whenever `DB/init/` changes. Not wired into CI yet.
- `./run_task_1a_demo.sh` (needs Docker) runs the clean-clone acceptance path end to end against its own disposable database: bootstrap, synthetic roster, the bundled ORCID payload, Crossref enrichment of the same work, a stored-record query, a canonical-list query and an export whose contents it verifies. It contacts no metadata provider, never uses Docker Compose, and removes only the container and volume it created. `tests/test_fixture_demo.py` pins the offline half (the fixture sources open no socket; the payloads still parse into the expected publication, provenance and authorship shapes) and `tests/integration/test_fixture_demo_integration.py` the database half. Not wired into CI yet.
- `tests/test_migration_smoke_script.py` runs that script offline against a stub `docker` and pins its resource handling: unique names, cleanup registered before creation, no pre-delete, removal of exactly what it created after success, a failed start, a bounded bootstrap timeout, a container exit or an interrupt, and an abort on a name collision.
- `tests/test_attribution_rules_cli.py` pins the command semantics of `people_pubs.sync.attribution_rules` offline: incompatible command/option combinations are rejected before a connection is opened, `--dry-run` never commits or refreshes, `recompute` never changes rules, `list` is read-only, and `--refresh-only` belongs to `recompute` alone.
- `tests/test_operator_bundles.py` runs `RunAllDayly.sh` and `RunAllWeekly.sh` against stub runners and proves one failing job does not stop the later ones while the bundle still exits non-zero; it also pins the retained job lists.
- The migration smoke also seeds synthetic internal/external authorships and
  OpenAlex location variants for `biblio.member_open_access_pdfs`: it verifies
  member-only selection, exclusion of OA rows without PDFs, deterministic
  fallback from `best_oa_location` to `locations[]`, rights-tier
  classification, and `app_readonly` access.
- Optional integration tests live under `tests/integration/` and are skipped unless `PEOPLE_PUBS_INTEGRATION_DSN` or `PEOPLE_DB_TEST_DSN` points at a disposable PostgreSQL database.

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

Not yet covered: database rerun tests that drive `datacite_backfill`,
`dblp_backfill` and `semantic_scholar_backfill`; those three have the offline
fixture tests only.

## Running the integration suite in a disposable container

From `DB/`, paste the block below into bash. It starts a uniquely named
PostgreSQL container on a loopback port that Docker chooses, and runs
`./run_integration_tests.sh` only once the entrypoint has logged
`PostgreSQL init process complete`, every `init/*.sql` script has run and
`pg_isready` answers for `people_db`. It gives up after 120 seconds, or as
soon as the container exits, and prints the container log. However the run
ends, it removes that container and nothing else. It exits with the tests'
status, with the bootstrap's failure status, or with 130 or 143 when
interrupted; if that work succeeded but the container could not be removed,
it warns and exits with 1. It needs no existing database; the generated
password is test-only, appears in no tracked configuration, and is discarded
with the container.

```bash
(
  set -euo pipefail
  [ -f init/000_init.sql ] && [ -x run_integration_tests.sh ] || { echo "Run this from DB/." >&2; exit 2; }
  name="pubdex-integration-$(date +%Y%m%d%H%M%S)-$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"
  pw="it-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
  cleanup() {  # removes only the container that carries this run's label
    local rc=$? id removed=1
    id="$(docker ps -aq --filter "label=pubdex.integration.run=$name")" || removed=0
    if [ "$removed" = 1 ] && [ -n "$id" ]; then
      docker rm -f -v "$id" >/dev/null || removed=0
    fi
    if [ "$removed" = 0 ]; then
      echo "WARNING: could not remove container $name; find it with" \
        "docker ps -a --filter label=pubdex.integration.run=$name" >&2
      [ "$rc" -ne 0 ] || rc=1  # an earlier failure keeps its own status
    fi
    exit "$rc"
  }
  trap cleanup EXIT; trap 'exit 130' INT; trap 'exit 143' TERM
  docker run -d --name "$name" --label "pubdex.integration.run=$name" -p 127.0.0.1::5432 \
    -e POSTGRES_PASSWORD="$pw" -e POSTGRES_DB=people_db \
    -v "$PWD/init:/docker-entrypoint-initdb.d:ro" postgres:16 >/dev/null
  bootstrapped() {  # init finished, every init/*.sql was run, server ready on TCP
    local logs f
    logs="$(docker logs "$name" 2>&1)" || return 1
    grep -qF 'PostgreSQL init process complete' <<<"$logs" || return 1
    for f in init/*.sql; do
      grep -qF "running /docker-entrypoint-initdb.d/${f#init/}" <<<"$logs" || return 1
    done
    docker exec "$name" pg_isready -h 127.0.0.1 -U postgres -d people_db >/dev/null 2>&1
  }
  echo "Waiting up to 120s for $name to bootstrap..."
  deadline=$((SECONDS + 120))
  until bootstrapped; do
    if [ "$(docker container inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" != true ]; then
      echo "ERROR: the container exited during bootstrap; log tail:" >&2
    elif [ "$SECONDS" -ge "$deadline" ]; then
      echo "ERROR: not bootstrapped within 120s; log tail:" >&2
    else
      sleep 1; continue
    fi
    docker logs --tail 40 "$name" >&2
    exit 1
  done
  port="$(docker port "$name" 5432/tcp | head -1 | sed 's/.*://')"
  [ -n "$port" ] || { echo "ERROR: no loopback port was mapped" >&2; exit 1; }
  env -i HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" \
    PEOPLE_PUBS_INTEGRATION_DSN="postgresql://postgres:${pw}@127.0.0.1:${port}/people_db" \
    ./run_integration_tests.sh
)
```

Safety net (added 2026-07-13 after a real near-miss):
`tests/integration/conftest.py` exports `PEOPLE_DB_DSN` to the
integration DSN before any test imports `people_pubs`, so a code path that
accidentally falls back to the default connection resolution (env →
`DB/.env`) lands in the disposable database instead of a real one. Tests must
still pass explicit DSNs; this guard only changes the failure mode of a
missed patch from "writes to a real database" to "wrong rows in the
disposable one".

## Local smoke workflow

- Install/update DB-local dev dependencies with `../.venv/bin/python -m pip install -r requirements-dev.txt` when the venv is missing test packages.
- Run the default offline check suite first: `./run_checks.sh` from the
  repository root (`../run_checks.sh` from `DB/`). To run only the unit tests,
  `./run_tests.sh` still works.
- After a change to `DB/init/`, the ingestion path or the export, also run
  `./run_migration_smoke.sh` and `./run_task_1a_demo.sh`.
- Run optional disposable-DB integration tests with `PEOPLE_PUBS_INTEGRATION_DSN="postgresql://..." ./run_integration_tests.sh`.
- Run at least one importer in `--dry-run` mode using a synthetic fixture.
- If a change touches SQL assumptions, verify the target table/column exists before running a write command.
