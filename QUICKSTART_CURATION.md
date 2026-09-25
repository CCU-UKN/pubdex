# Quickstart: curation, aliases and reporting exports

This walks the curation feature set on top of the completed
[`QUICKSTART.md`](QUICKSTART.md) state: OpenAlex enrichment in safe demo mode,
alias/identity resolution, durable curation decisions, and the reporting
exports — still without any private data. Every DB-writing
`people_pubs.sync` command in this walkthrough supports `--dry-run`; prefer it
on first contact with any database you care about.

This walkthrough contacts live services (OpenAlex) and is a demonstration of
the curation feature set, not the reproducible acceptance path; that one is
`QUICKSTART.md` steps 1–8 and `DB/run_task_1a_demo.sh`, neither of which makes
a metadata-provider request.

Prerequisites: you finished `QUICKSTART.md` steps 1–5 (Postgres up, repo-root
`.venv` created, demo roster imported, the bundled ORCID and Crossref payloads
ingested).
Nothing needs to be exported or activated: like `QUICKSTART.md`, every command
block below starts from the **repository root**, uses the `.venv` interpreter
explicitly, wraps `DB/`-relative work in a `(cd DB && ...)` subshell, and lets
`people_pubs/config.py` fill the DB connection and the polite-pool contact
address from `DB/.env` (`PGHOST`/`PGDATABASE`/`PGPASSWORD`,
`PEOPLE_PUBS_CROSSREF_MAILTO`). Database spot-checks use
`docker exec peopledb-postgres psql`, which needs no password from the host.

## 1. OpenAlex enrichment (safe demo mode)

OpenAlex is **opt-in, last-fallback** enrichment: it
may fill missing fields and merge provenance, but it never overwrites
metadata backed by a more trusted source, and the combined backfill runner
only includes it with `--with-openalex`. The demo scope below only asks
OpenAlex about the handful of demo publications, throttled to one request per
second, with the polite-pool contact address from `DB/.env` attached
automatically (add `--mailto you@example.org` only to override it).

```bash
(cd DB && ../.venv/bin/python -m people_pubs.sync.openalex_backfill --max 25 --sleep 1 --dry-run --debug)
(cd DB && ../.venv/bin/python -m people_pubs.sync.openalex_backfill --max 25 --sleep 1)
```

Confirm the source-keyed payload and cumulative provenance landed (no
credentials needed — this runs inside the container):

```bash
docker exec peopledb-postgres psql -U postgres -d people_db -c "
  SELECT pub_id, source_of_truth, raw_json ? 'openalex' AS has_openalex_payload
  FROM biblio.publications
  WHERE raw_json ? 'openalex'
  ORDER BY pub_id LIMIT 10;"
```

Expected: enriched rows show `openalex` appended to `source_of_truth`
(e.g. `crossref+orcid+openalex`) and `has_openalex_payload = t`, while titles
and years that came from ORCID/Crossref are unchanged. The offline
equivalents of this behavior are locked by
`DB/tests/test_openalex_fixture_ingest.py` and the OpenAlex idempotency
integration test. OpenAlex-*only* records stay in the pool with
`source_of_truth = 'openalex'`.

## 2. Aliases and identity resolution

Derive name aliases from the authorship rows the demo ingest created (reads
through `biblio.authorships_curated`, so curator detach decisions are
honored):

```bash
(cd DB && ../.venv/bin/python -m people_pubs.sync.alias_backfill --dry-run --debug)
(cd DB && ../.venv/bin/python -m people_pubs.sync.alias_backfill)
```

Relink any authorship rows that are still missing a `person_id`, using ORCID
first and unique name/alias matches second:

```bash
(cd DB && ../.venv/bin/python -m people_pubs.sync.relink_authorships --max 500 --dry-run --debug)
(cd DB && ../.venv/bin/python -m people_pubs.sync.relink_authorships --max 500)
```

Expected: the summary line reports `matched`/`updated` for confident matches
and `skipped_no_match` for everything else. Low-confidence candidates are
**not** auto-linked — they keep `person_id = NULL` and remain reviewable and
correctable through the curation commands in `DB/SCRIPTS.md`.

## 3. Durable curation decisions (survive reingest)

Record a manual dedup decision and prove it is durable. Pick two demo
`pub_id`s first:

```bash
docker exec peopledb-postgres psql -U postgres -d people_db \
  -c "SELECT pub_id, title FROM biblio.publications_canon ORDER BY pub_id LIMIT 10;"
```

Then (replace `<A>` and `<B>` with two of those ids):

```bash
(cd DB && ../.venv/bin/python -m people_pubs.sync.publication_dedup_overrides \
  set-keep-separate --pub-id <A> --pub-id <B> --note "demo: keep separate")
(cd DB && ../.venv/bin/python -m people_pubs.sync.publication_dedup_overrides list)
```

The command writes both the current `pub_id` override and a DOI/title-keyed
durable decision in `biblio.publication_dedup_decisions`, plus an audit entry
in `biblio.manual_corrections_log`. Because durable decisions are
selector-keyed, they replay after publication resets or reingests that
replace `pub_id`s (locked by
`DB/tests/integration/test_durable_dedup_integration.py`). The same decisions
can be made through `people_pubs.curation_admin` (confirm winner / keep
separate) with an audit trail; see `DB/SCRIPTS.md` → *Manual dedup overrides*.

Back up and restore the curation state explicitly (the daily
`run_backup.sh` bundle already includes this export automatically — see
`DB/README.md` → *Curation backup cadence*):

```bash
(cd DB && ../.venv/bin/python -m people_pubs.sync.curation_backup export --output /tmp/curation-demo.json)
(cd DB && ../.venv/bin/python -m people_pubs.sync.curation_backup import --input /tmp/curation-demo.json --dry-run)
```

Expected: the import dry-run reports each curation table with upsert counts
and rolls back.

## 4. Reporting exports (end to end)

The output files land in `DB/`:

```bash
# Canonical publication list (CSV; deterministic, golden-locked):
(cd DB && ../.venv/bin/python -m people_pubs.sync.export_publications --output publications.csv)

# Which providers contributed to each canonical publication:
(cd DB && ../.venv/bin/python -m people_pubs.sync.source_coverage_report --output coverage.csv --summary)

# Curation-state JSON bundle (also part of every backup):
(cd DB && ../.venv/bin/python -m people_pubs.sync.curation_backup export --output curation.json)
```

Expected: `publications.csv` has the documented seven columns
(`pub_id,doi,title,year,venue,source_of_truth,internal_authors`);
`coverage.csv` lists per-publication source families/tokens, and the summary
prints total counts per family plus a provenance-breadth histogram — after
step 1, demo rows report families like `crossref+openalex+orcid`. Repeated
exports of unchanged data are byte-identical.

## 5. Run all checks

```bash
./run_checks.sh                         # default offline suite (same as CI)
./DB/run_task_1a_demo.sh                # clean-clone acceptance path, offline
```

For the integration tests, which need a disposable database, run the recipe
in [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) → *Running the
integration suite in a disposable container* from `DB/`. It waits for the
schema bootstrap to finish before any test starts, and removes only the
container it created.

## Troubleshooting

- **OpenAlex 403/429 responses** — set a reachable
  `PEOPLE_PUBS_CROSSREF_MAILTO` in `DB/.env` (or pass `--mailto`; polite pool)
  and raise `--sleep`; the backfill has no retry loop by design, rerun it
  instead (reruns are idempotent).
- **`alias_backfill` writes nothing** — it only derives aliases from
  authorship rows whose `person_id` is set (through the curated view); run
  the relink step or attach authorships first.
- **`relink_authorships` links fewer rows than expected** — name matches
  require a *unique* case-insensitive candidate above the similarity
  threshold (`PEOPLE_PUBS_NAME_MATCH_MIN_SCORE`); ambiguous names stay
  reviewable by design.
- **Dedup override refuses a pub_id** — the row must exist in
  `biblio.publications`; `list --include-inactive` shows superseded
  decisions.
- **Commands can't find the database** — ingestion resolves its connection
  from `--dsn`, `PEOPLE_DB_DSN`, or `PG*` environment variables, with
  `DB/.env` filling every gap; see `QUICKSTART.md` step 4 and its
  troubleshooting section.
