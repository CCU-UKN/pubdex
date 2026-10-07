# DB Setup, Restore, And User Access

This folder contains Docker Compose setup for `people_db`, initialization SQL, and restore scripts.

Operational rule: after any applied DB structure change, including tables,
views, functions, grants, or edits from `init/010_ingestion_runtime_schema.sql`,
run `./run_backup.sh` immediately to produce a fresh backup bundle.

## 1) Prerequisites

- Docker Engine + Docker Compose (`docker compose`)
- Access to this folder: `<repo>/DB`

Quick checks:

```bash
docker --version
docker compose version
```

Windows:
- Use Docker Desktop.
- Run commands from Git Bash or WSL for best compatibility with shell scripts here.

## 2) First-Time Setup (New Machine)

### 2.1 Create `.env`

Copy the canonical template from the repository root — the single
consolidated example — and edit the values; there is deliberately no second
inline template here:

```bash
cp ../.env.example .env
```

Set at least `POSTGRES_PASSWORD`/`PGPASSWORD`, the application role
passwords, the pgAdmin login, and `WG_BIND_IP`. The template documents every
key inline.

Two settings decide what counts as "ours". Both ship **empty**, and empty
means *nothing matches* rather than *everything matches*:

- `PEOPLE_PUBS_ORG_PATTERNS` (in `.env`) — the employers whose employments
  ORCID profile enrichment records for a person. See `SCRIPTS.md` →
  *ORCID profiles*.
- `app.institution_attribution_rules` (a database table, managed with
  `python -m people_pubs.sync.attribution_rules`) — the affiliation terms and
  funding identifiers that make a publication canonical. See `SCRIPTS.md` →
  *Institutional attribution*, and §8.1 below.

They are independent: configuring one does not configure the other.

Notes:
- `WG_BIND_IP` is the additional address the containers publish their ports
  on, besides `127.0.0.1`. On a host with a private interface (a VPN, for
  example), use that interface's address; otherwise use a second loopback
  address such as `127.0.0.2` (see `QUICKSTART.md` step 1).
- `WG_BIND_IP` is required (`compose.yaml` has no hardcoded fallback).
- On a different machine, this value will usually be different.

Find a private interface address (Linux):

```bash
ip -4 addr show <interface> | awk '/inet / {print $2}' | cut -d/ -f1
```

### 2.2 Prepare folders

```bash
mkdir -p backups
```

Notes:
- pgAdmin now stores its runtime state in the Docker named volume `peopledb-pgadmin-data`.
- This keeps container-owned SQLite/session files out of `DB/` and avoids the permission problems that bind-mounted pgAdmin state can cause.
- If you are migrating from the older bind-mounted setup, you can remove legacy `DB/pgadmin` or `DB/pgadmin.broken.*` folders after the fresh pgAdmin instance is working.

### 2.3 Start services

```bash
docker compose -f compose.yaml up -d postgres
docker compose -f compose.yaml up -d pgadmin
```

### 2.4 Verify health

```bash
docker compose -f compose.yaml ps
docker exec peopledb-postgres pg_isready -U postgres -d people_db
```

### 2.5 Sync DB role passwords from `.env`

Keep role passwords in `DB/.env`, then apply them with:

```bash
./sync_db_role_passwords.sh --apply
```

At minimum, set `APP_READONLY_PASSWORD` because the default pgAdmin server entry uses DB user `app_readonly`.
`APP_WRITER_PASSWORD` is recommended if you also use a write-capable pgAdmin/psql connection.

### 2.6 Ensure read-only grants are in place

```bash
docker exec peopledb-postgres psql -U postgres -d people_db -c "
GRANT USAGE ON SCHEMA app, biblio, activity, staging_raw TO app_readonly;
GRANT USAGE ON SCHEMA public TO app_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA app, biblio, activity, staging_raw TO app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA biblio GRANT SELECT ON TABLES TO app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA activity GRANT SELECT ON TABLES TO app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA staging_raw GRANT SELECT ON TABLES TO app_readonly;"
```

### 2.7 Verify endpoints and protocol

```bash
# pgAdmin web UI (HTTP)
curl -I "http://127.0.0.1:${PGADMIN_LOCAL_PORT:-8081}/"

# PostgreSQL readiness (PostgreSQL protocol, not HTTP)
docker exec peopledb-postgres pg_isready -U postgres -d people_db
```

Notes:
- Open pgAdmin in a browser at `http://127.0.0.1:8081` by default, or `http://127.0.0.1:$PGADMIN_LOCAL_PORT` if overridden.
- Over the private interface, use `http://$WG_BIND_IP:8080`.
- Do not use a browser for `http://127.0.0.1:5432` or `http://$WG_BIND_IP:5432`.
  Port `5432` speaks PostgreSQL protocol, not HTTP.

## 3) Restore From Backup

Put backup files in `DB/backups/`:
- `people_db_*.dump`
- optional `people_globals_*.sql`
- optional `people_curation_*.json`

Restore latest:

```bash
./restore_db.sh -y
```

Restore specific files:

```bash
./restore_db.sh \
  --dump backups/people_db_YYYYMMDDTHHMMSSZ.dump \
  --globals backups/people_globals_YYYYMMDDTHHMMSSZ.sql \
  --curation backups/people_curation_YYYYMMDDTHHMMSSZ.json \
  -y
```

By default, `restore_db.sh` now also:

- applies `init/010_ingestion_runtime_schema.sql` after the dump restore
- imports the timestamp-matched `people_curation_*.json` when present

Use `--no-runtime-schema-patch` or `--no-curation` only when you intentionally
want to skip those steps.

After restore, re-run:
1. Section `2.5` (`./sync_db_role_passwords.sh --apply`).
2. Section `2.6` (grants).
3. Section `4.2` (sync server to all pgAdmin users).
4. If the restored dump predates the current ingestion schema, apply
   `init/010_ingestion_runtime_schema.sql` (the single idempotent runtime
   patch; it also creates `biblio.publications_canon_web`).

```bash
docker exec -i peopledb-postgres psql -U postgres -d people_db -v ON_ERROR_STOP=1 -f - < init/010_ingestion_runtime_schema.sql
```

### 3.0.1 Curation backup cadence (operator notes)

Manual curation state — durable dedup decisions and `pub_id` overrides,
authorship manual overrides, manual name aliases, and the corrections log —
must survive dump restores and publication resets. The cadence that
guarantees this:

- **Every backup bundles curation state automatically.** `./run_backup.sh`
  writes a same-timestamp trio into `backups/`: the SQL dump, the globals
  SQL, and `people_curation_<ts>.json` (produced by
  `people_pubs.sync.curation_backup export`). A failed curation export does
  not abort the SQL dumps but exits non-zero so cron surfaces it.
- **Daily schedule.** The 10:00 cron sequence runs the backup *first*, before
  any refresh job (`RunAllDayly.sh`). Copy the tracked `DB/cron.example` to
  the machine-local `DB/cron`, set its `REPO_ROOT`, and install that file with
  `crontab DB/cron`. Bundles are retained for 30 days by the cleanup step in
  `run_backup.sh`.
- **Restores re-apply curation by default.** `./restore_db.sh` imports the
  timestamp-matched `people_curation_*.json` after the dump and the runtime
  schema patch (see above); use `--no-curation` only deliberately.
- **Manual export/import** (for migrations, or before risky curation work):

  ```bash
  python -m people_pubs.sync.curation_backup export --output backups/curation.json
  python -m people_pubs.sync.curation_backup import --input backups/curation.json --dry-run
  ```

- **After any schema change to a real DB, run `./run_backup.sh` immediately**,
  so the newest bundle restores cleanly against the current schema.

Day-to-day curation itself (alias review, attach/detach, version choices) runs
through the commands in [SCRIPTS.md](SCRIPTS.md).

## 3.1 One-time Activity State Patch (Existing DBs)

New installs now include:
- `activity.person_refresh_state`
- `activity.source_refresh_state`
- `activity.refresh_run_log`

If your DB was created before these tables were added, apply
`init/010_ingestion_runtime_schema.sql` once (it is idempotent and includes
these tables; the former standalone `activity_state_tables.sql` was removed
as a duplicate in 2026-07).

## 3.2 Runtime Schema Patch

Fresh Docker bootstrap runs `init/010_ingestion_runtime_schema.sql`
automatically after `init/000_init.sql`.
The patch is idempotent and aligns the tracked bootstrap with current ingestion code for:

- `biblio.publications.is_preprint`
- richer `biblio.authorships` fields and conflict key
- `app.person_name_aliases_manual` and alias view support
- manual correction, authorship override, review-queue, and durable dedup/canon-scope decision tables
- `biblio.authorships_curated` so manual authorship overrides survive reingest-facing reads
- narrow `pii.*` write functions for routine ingestion as `app_writer`
- `USAGE` (not `CREATE`) on schema `public`, which holds the `citext`,
  `pgcrypto`, `unaccent` and `pg_trgm` extensions, for `app_writer`,
  `app_readonly` and — where that role exists — `maintenance`: without it
  the application's `citext` arguments to those functions fail and `citext`
  columns, the `pii` email columns among them, compare case-sensitively for
  these roles. The patch never creates the `maintenance` role, and applies
  without it. Databases bootstrapped before this grant was added get it by
  re-applying the patch.
- refresh-state checkpoint support
- `biblio.publications_clean` and `biblio.publications_canon`
- `biblio.publications_canon_web`, the concurrently refreshed PubDex snapshot

Durable dedup decisions preserve curated publication-version choices, such as preprint vs final article, across resets that replace `pub_id`s.
Curation administration also relies on `biblio.authorships_curated` so manual
attach/detach decisions are reapplied by selector rather than only by the
current raw `pub_id` row state.

Apply it manually after restoring older dumps:

```bash
docker exec -i peopledb-postgres psql -U postgres -d people_db -v ON_ERROR_STOP=1 -f - < init/010_ingestion_runtime_schema.sql
```

The restore helper applies this patch automatically by default. The manual
command above is still useful when you intentionally skip the helper patch step
or need to repair an already-restored database in place.

## 3.3 Migration Model And Re-Run Safety

There is deliberately no migration-version table; the model is two files with
different contracts:

- `init/000_init.sql` — **bootstrap-only, not re-runnable.** It contains bare
  `CREATE TABLE`/`CREATE TYPE` statements and must only ever run against an
  empty database. Duplicate application is prevented by the runner itself:
  Postgres' `docker-entrypoint-initdb.d` mechanism executes `init/` exactly
  once, when the `peopledb_data` volume is first created. It never runs again
  on an existing volume.
- `init/010_ingestion_runtime_schema.sql` — **convergent, but not atomic and
  not transparent to a serving database** (contract corrected 2026-07-10;
  previously documented as "safe to re-apply at any time"). Its *end state*
  is idempotent (`IF NOT EXISTS` / `CREATE OR REPLACE` throughout), and this
  is the only file that evolves an existing database — `restore_db.sh`
  re-applies it automatically after every restore. However, the file runs
  statement-by-statement under `psql` with no enclosing transaction, and it
  drops and recreates the serving view chain (`publications_clean` →
  `publications_canon` → `publications_canon_web`) and both materialized
  snapshots mid-file. **Re-apply it to a live, serving database only in a
  maintenance window.** If an apply fails partway, the database can be left
  without serving views until the patch is re-run to completion (run with
  `-v ON_ERROR_STOP=1`, fix the cause, re-apply — it converges). A
  transactional/phased migration mechanism remains an open backlog choice.

To verify the bootstrap and convergence claims against the current files, run
the migration smoke test (disposable Docker container; never touches a real
database):

```bash
./run_migration_smoke.sh
```

It bootstraps an empty PostgreSQL from `init/`, asserts the expected tables,
views, and `pii.*` functions exist, re-applies the `010` patch, and asserts
everything is still in place. Note what it proves: final-state repeatability
on an empty database — it does not exercise mid-apply failure or concurrent
readers (hence the maintenance-window rule above).

## 4) pgAdmin Users (No Admin Privileges Needed)

### 4.1 Default behavior

- pgAdmin web login uses each person's pgAdmin account (email/password).
- DB queries use DB user `app_readonly` (configured in `pgadmin-servers.json`).

These are different credential sets.

### 4.2 Sync server entry to all existing pgAdmin users

Run:

```bash
./pgadmin_sync_servers.sh
```

This imports the same `PeopleDB` server entry for every active pgAdmin user.

### 4.3 When you create a new pgAdmin user

Run again:

```bash
./pgadmin_sync_servers.sh
```

Then the new user will see `Servers -> PeopleDB`.

### 4.4 What password users enter on "Connect to server"

Use `APP_READONLY_PASSWORD` from `DB/.env` for `app_readonly`.
It is a PostgreSQL role password, not the pgAdmin web password.

## 5) Network Notes

`compose.yaml` binds:
- `127.0.0.1:${PGADMIN_LOCAL_PORT:-8081}` for local pgAdmin browser use
- `${WG_BIND_IP}` for VPN users

`WG_BIND_IP` must be present in `.env` before running `docker compose`.
If it is unset/empty, port mappings that use `${WG_BIND_IP}` will fail.
`PGADMIN_LOCAL_PORT` is optional and defaults to `8081`.

If this machine's private-interface address changes, update `WG_BIND_IP` in `.env` and restart:

```bash
docker compose -f compose.yaml up -d
```

## 6) Quick Troubleshooting

- pgAdmin web login says account locked:
  - Unlock/reset pgAdmin user, then retry web login.
- pgAdmin fails to start with `failed to bind host port 127.0.0.1:8080/tcp: address already in use`:
  - Set `PGADMIN_LOCAL_PORT=8081` or another free loopback port in `.env`, then run `docker compose -f compose.yaml up -d`.
- `localhost:$PGADMIN_LOCAL_PORT` does not open and pgAdmin logs show `/var/lib/pgadmin` permission errors, `The configuration database file is not valid`, `Database migration failed`, or `EOF when reading a line`:
  - Reset the named pgAdmin state volume and recreate the service:
    ```bash
    docker compose -f compose.yaml stop pgadmin
    docker rm -f peopledb-pgadmin
    docker volume rm peopledb-pgadmin-data
    docker compose -f compose.yaml up -d pgadmin
    ```
  - If this machine still has a legacy host-side pgAdmin state folder from the old bind-mounted setup, remove it after the new container is healthy:
    ```bash
    rm -rf pgadmin pgadmin.broken.*
    ```
- Server appears with red `x`:
  - Open server, enter `APP_READONLY_PASSWORD` from `DB/.env`.
- You are not sure whether env-backed DB role passwords still match the live DB:
  - Run `./sync_db_role_passwords.sh --check`.
  - If needed, run `./sync_db_role_passwords.sh --apply`.
- Query fails with `permission denied for schema biblio`:
  - Re-run section `2.6`.
- Opening `http://<host>:5432` in a browser fails:
  - Expected. Use pgAdmin, `psql`, or another PostgreSQL client for port `5432`.
- `docker compose` fails because `WG_BIND_IP` is missing/invalid:
  - Set `WG_BIND_IP` in `.env` to this host's current private-interface IPv4 (or a second loopback address), then run `docker compose -f compose.yaml up -d` again.
- New pgAdmin user sees no servers:
  - Run `./pgadmin_sync_servers.sh`.

## 7) Schema Map

```bash
docker cp schema_map.sql peopledb-postgres:/tmp/schema_map.sql
docker exec peopledb-postgres psql -U postgres -d people_db -f /tmp/schema_map.sql
```

## 8) Canon Query Performance Indexes

The trigram indexes that speed up `biblio.publications_canon` filters on
`source_of_truth` and `raw_json::text` ship with
`init/010_ingestion_runtime_schema.sql` (the former standalone
`performance_indexes.sql` was removed as a duplicate in 2026-07):
- `idx_pub_raw_json_trgm` on `lower(raw_json::text)` (GIN + trigram)
- `idx_pub_sot_trgm` on `lower(source_of_truth)` (GIN + trigram)

These indexes help isolated predicates, but the complete canonical view also
contains layered deduplication, curated-authorship aggregation, durable
decisions, and combined `OR` filters. PubDex therefore keeps the separate
materialized snapshot below as a serving snapshot, so that reads of the
canonical list need not execute the full view each time.

## 8.1) Institutional Attribution (Fresh Installs Start Empty)

`biblio.publications` stores everything ingestion fetches.
`biblio.publications_canon` is the canonical list — the subset that belongs to
this institution, de-duplicated. The rule that connects the two is data, not
code: `app.institution_attribution_rules` holds the affiliation terms and
funding identifiers to look for in each publication's stored provider payloads
(`raw_json`). A publication's result is cached in
`biblio.publications.institution_attributed`, maintained by trigger.

**The table ships empty, and an empty table attributes nothing.** On a fresh
install the canonical list therefore contains only rows a curator explicitly
included; ingestion is unaffected. Configure it with the one command that also
recomputes the cached flags and refreshes the serving snapshots:

```bash
../.venv/bin/python -m people_pubs.sync.attribution_rules set \
  --affiliation "Institute for Example Studies" --funding "EX-12345678"
```

The rule change and the flag recompute share one transaction, held under an
advisory lock that the database takes itself: the trigger that computes the
flag on every publication write holds it shared until that writer commits,
and rule changes and the recompute hold it exclusive. A rule change therefore
waits for in-flight ingestion, recomputes everything it committed, and
writers that arrive meanwhile evaluate the new rules once it commits, so the
cached flags can never disagree with the committed rules whichever side
commits first (this needs `READ COMMITTED`, which every shipped writer uses).
The two serving snapshots are then refreshed together in a second
transaction (§9). A failed refresh leaves rules and flags consistent and both
snapshots at their previous state — retry with
`attribution_rules recompute --refresh-only` or
`./refresh_publications_canon_web.sh`.

`biblio.institution_attribution_signal(jsonb)` reads that table, so it is
`STABLE`, not `IMMUTABLE`. Full matching contract: `SCRIPTS.md` →
*Institutional attribution*.

## 9) PubDex Canonical Web Snapshot

The snapshot (`biblio.publications_canon_web`) is created by
`init/010_ingestion_runtime_schema.sql`; apply that patch once on databases
that predate it.

Refresh manually:

```bash
./refresh_publications_canon_web.sh
```

The refresh:

- refreshes `biblio.publication_dedup_map_web` and
  `biblio.publications_canon_web` with `REFRESH MATERIALIZED VIEW CONCURRENTLY`
  inside one transaction, so readers keep the previous pair until the commit
  publishes the new pair together
- keeps both snapshots readable while the refresh runs; a failure rolls the
  whole transaction back and leaves the previous pair in place
- uses a transaction-level PostgreSQL advisory lock to serialize overlapping
  refreshes (the attribution command's refresh takes the same lock)
- records a common `snapshot_refreshed_at` value in every snapshot row

Recommended cadence: every six hours, plus an optional explicit refresh after a
successful ingestion batch or important curation session.

Linux cron example (also provided in `cron.example`):

```cron
17 */6 * * * cd /absolute/path/to/pubdex/DB && ./refresh_publications_canon_web.sh >> refresh_publications_canon_web.log 2>&1
47 * * * * cd /absolute/path/to/pubdex/DB && ./check_canon_web_freshness.sh >> check_canon_web_freshness.log 2>&1
```

## 9.1 Member Open-Access PDF View

`biblio.member_open_access_pdfs` is the read-only manifest source for
publication downloads involving current internal members. It contains one row
per publication when all of these are true:

- OpenAlex marks the publication open access.
- OpenAlex supplies a non-empty PDF URL, either at `best_oa_location` or in
  `locations[]`.
- `biblio.authorships_curated` links the publication to at least one
  `app.people` row whose current `person_kind` is `internal`.

The view includes DOI/title metadata, the selected PDF and landing-page URLs,
the selected location's license/source metadata, `rights_tier`, and aggregated
member IDs/names/ORCIDs. It deliberately reads curated authorships so manual
attach/detach decisions are honored. It includes both canon and non-canon
publications.

`rights_tier` is only a triage aid:

- `explicit_open_license`: selected location reports Creative Commons or
  public-domain terms.
- `source_terms_review`: another license label is present.
- `license_missing`: the selected PDF location has no license label.

Open-access status does not by itself grant redistribution rights. Inspect
`pdf_license`, the source terms, and the intended use before downloading or
redistributing a corpus.

```sql
SELECT pub_id, doi, pdf_url, pdf_license, rights_tier, member_person_ids
FROM biblio.member_open_access_pdfs
ORDER BY pub_id;

SELECT rights_tier, count(*)
FROM biblio.member_open_access_pdfs
GROUP BY rights_tier
ORDER BY rights_tier;
```

## 10) Local Tests

Install/update DB-local dev dependencies:

```bash
../.venv/bin/python -m pip install -r requirements-dev.txt
```

Run the unit tests:

```bash
./run_tests.sh
```

They run in a cleared environment with `PEOPLE_PUBS_SKIP_DOTENV=1`: no
database setting or pytest option you export, and nothing in `DB/.env`, reaches
them, so the integration tests always skip here, as the expected-skip policy
in `tests/expected_skips.py` allows; any other skip fails the run, and a
network guard refuses connections from the tests and the Python processes
they start (`TESTING_STRATEGY.md` lists its boundaries). Arguments you pass
still go to pytest.

For the default offline check suite — compilation, Markdown consistency and
this suite — use `../run_checks.sh` from here, or `./run_checks.sh` from the
repository root. `../run_ci_suite.sh` runs the complete suite that public CI
runs: that offline suite, the check for committed secrets and local
configuration, and the Docker-backed commands `./run_migration_smoke.sh`,
`./run_task_1a_demo.sh` and `./run_disposable_integration.sh`, each of which
also runs on its own; see `TESTING_STRATEGY.md`.

The current suite is code-only and uses synthetic fixtures; it must not depend on live APIs, production backups, or real PII.

Run the integration tests only against a throwaway database.
`./run_disposable_integration.sh` starts one, binds every connection setting to
it, runs the whole integration suite and removes it again; against a
disposable database of your own:

```bash
PEOPLE_PUBS_INTEGRATION_DSN="postgresql://..." ./run_integration_tests.sh
```
