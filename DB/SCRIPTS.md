# DB Scripts

This file lists the scripts that interact with the publications/people database,
their purpose, and a minimal example.

Notes:
- Run from `DB/` or set `PYTHONPATH=DB` when using `python -m ...`.
- DB connection uses `PEOPLE_DB_DSN` or `PG*` env vars unless `--dsn` is provided.

### PubDex canonical web snapshot

`DB/refresh_publications_canon_web.sh`

- Waits for the PostgreSQL container to become ready.
- Refreshes `biblio.publication_dedup_map_web` and `biblio.publications_canon_web`
  concurrently, both inside one transaction: readers keep the previous pair
  until the commit publishes the new pair together, and a failure rolls both
  back.
- Serializes overlapping refreshes with a transaction-level PostgreSQL advisory
  lock, shared with the attribution command's refresh.
- Is intended for a six-hour scheduled cadence, not for serving requests.

### PubDex canon snapshot freshness alert

`DB/check_canon_web_freshness.sh`

- Companion stale-snapshot alert for cron email (`MAILTO`).
- Exits non-zero with an `ALERT:` line on stderr when the freshest
  `biblio.publications_canon_web` row is older than `CANON_WEB_MAX_AGE_HOURS`
  (default 24) — catching a refresh that *never ran* (host down, cron disabled),
  which the refresh script itself cannot detect.
- Schedule on its own cron cadence (e.g. hourly), independent of the refresh:
```
MAILTO=ops@example.org
17 * * * * /path/to/DB/check_canon_web_freshness.sh
```

### DB restore helper
`DB/restore_db.sh`
- Restore `people_db` from backups in `DB/backups` (latest dump by default).
- Supports explicit files via `--dump`/`--globals`/`--curation`,
  auto-applies `init/010_ingestion_runtime_schema.sql` after restore by
  default, and imports the timestamp-matched `people_curation_*.json` when
  present.
- Use `--no-globals`, `--no-curation`, or `--no-runtime-schema-patch` only for
  intentional partial restores.
- Example:
```
./restore_db.sh -y
```

### DB role password sync helper
`DB/sync_db_role_passwords.sh`
- Check or apply PostgreSQL role passwords from `DB/.env`.
- Uses:
  - `APP_READONLY_PASSWORD`
  - `APP_WRITER_PASSWORD`
  - `MAINTENANCE_PASSWORD`
- `--check` verifies that the env values currently work.
- `--apply` runs `ALTER ROLE` for any of those variables that are set, then verifies login.
- Examples:
```
./sync_db_role_passwords.sh --check
./sync_db_role_passwords.sh --apply
```

### Stateful refresh runner (DB-backed staleness)
`DB/people_pubs/sync/refresh_state_runner.py`
- Uses `activity.source_refresh_state` to run only stale items and persist `last_attempt_at/last_success_at/last_error_at`.
- For `source=orcid_profile`, also propagates ORCID external last-updated into `external_updated_at`.
- If available, writes per-run history rows to `activity.refresh_run_log` (one row per executed job item).
- Automatically reads `DB/.env` (next to `refresh_jobs.json`) and maps `POSTGRES_*` vars to `PG*` for cron-safe DB auth.
- If `PGUSER` is `app_writer`, `app_readonly`, `maintenance`, or `postgres`, password resolution prefers the matching `APP_WRITER_PASSWORD`, `APP_READONLY_PASSWORD`, `MAINTENANCE_PASSWORD`, or `POSTGRES_PASSWORD`.
- Scheduled jobs should normally call `DB/run_refresh_job.sh`; it waits for `peopledb-postgres` readiness before invoking this runner.
- Supports:
  - `scope_type=person`: run one module invocation per due `person_id`
  - `scope_type=query`: run one module invocation per due `scope_key`
- Current default jobs in `refresh_jobs.json`:
  - `orcid_profiles_person` (3 months, limit 30)
  - `orcid_works_person` (14 days, limit 30)
  - `crossref_search_orcid_person` (14 days, limit 30)
  - `crossref_backfill_query` (14 days query-scope)
  - `semantic_backfill_query` (30 days query-scope)
  - `dblp_backfill_query` (30 days query-scope)
  - `low_source_canon_backfill_query` (6 days query-scope; canon rows with fewer than 4 non-`unknown` `source_of_truth` tokens)
- `stale_after_hours` exists for jobs a daily cron must not skip. With `stale_after_days: 1`, a
  10:00 cron whose previous run finished at 10:05 sees 23h55m elapsed, is judged not due, and
  skips — then drifts. A threshold below the cron period avoids that.
- Person-scope jobs process the `limit` stalest *due* people per run, so with a few hundred ORCID people at limit 30 the real refresh cycle is ~10–14 days. The two daily discovery jobs (`orcid_works`, `crossref_search_orcid`) therefore use `stale_after_days: 14` — a 6-day target is unreachable at that throughput and just makes the state table read perpetually overdue. The 90-day/3-month jobs keep up comfortably.
- Default config: `DB/refresh_jobs.json`
- Example:
```
# list jobs
python -m people_pubs.sync.refresh_state_runner --config refresh_jobs.json --list

# dry-run (does not update state table)
python -m people_pubs.sync.refresh_state_runner --config refresh_jobs.json --dry-run --debug

# run only ORCID person-scope refreshes
python -m people_pubs.sync.refresh_state_runner --config refresh_jobs.json --only orcid_profiles_person --stop-on-error

# run only Crossref-by-ORCID person refreshes
python -m people_pubs.sync.refresh_state_runner --config refresh_jobs.json --only crossref_search_orcid_person --stop-on-error

# run one job directly (per-job invocation still works for ad-hoc runs)
python -m people_pubs.sync.refresh_state_runner --config refresh_jobs.json --only crossref_search_orcid_person --stop-on-error
```

The installed crontab invokes the two bundle scripts rather than listing every
ingestion job, so the job list has a single source of truth
(`RunAllDayly.sh`/`RunAllWeekly.sh`, both git-tracked) and jobs run
sequentially (each waits for the previous instead of racing the shared
advisory lock). Copy the portable `DB/cron.example` to the machine-local,
git-ignored `DB/cron`, replace `REPO_ROOT`, and install it with
`crontab DB/cron`. Keep the independent canon refresh and freshness check in
that file as well:

```
# monthly log rotation
30 09 1 * * cd .../DB && ./rotate_logs.sh >> rotate_logs.log 2>&1
# daily: backup + per-person refresh (RunAllDayly.sh)
00 10 * * * cd .../DB && ./RunAllDayly.sh >> RunAllDayly.log 2>> RunAllDayly.err.log
# weekly Wed: metadata backfills, incl. low_source_canon (RunAllWeekly.sh)
15 12 * * 3 cd .../DB && ./RunAllWeekly.sh >> RunAllWeekly.log 2>> RunAllWeekly.err.log
# canon serving snapshot: six-hour refresh + hourly stale check
17 */6 * * * cd .../DB && ./refresh_publications_canon_web.sh >> refresh_publications_canon_web.log 2>&1
47 * * * * cd .../DB && ./check_canon_web_freshness.sh >> check_canon_web_freshness.log 2>&1
```

### Preflight discovery searches (avoid mass ingestion)

Affiliation/funding discovery jobs can match huge result sets — a Crossref
`query.affiliation` for a research centre matches ~1.4M records (it is an "any
word" match, not a phrase).
Before enabling or widening one, preflight it. `--estimate` reports how many records
a query would match and exits without ingesting or touching the DB:

```
# one query
python -m people_pubs.sync.crossref_search \
  --affiliation "<your centre>" \
  --from-year 2019 --to-year 2026 --estimate

# every scheduled discovery job: table of raw match / cap / post-filter / verdict
python -m people_pubs.sync.estimate_search_jobs
```

Which portals are exact vs "any word", and which require `--require-affiliation-match`
(Crossref) — and why — are documented in `INGEST_SEARCH_SEMANTICS.md`. The guardrails
are enforced offline by `tests/test_refresh_jobs_guardrails.py` and
`tests/test_ingest_caps.py` (both in `./run_tests.sh`).

### Cron wrappers
`DB/wait_for_postgres.sh`
- Waits until `peopledb-postgres` accepts PostgreSQL connections.
- Default timeout: `300` seconds; override with the first argument or `DB_WAIT_TIMEOUT_SECONDS`.

`DB/run_refresh_job.sh`
- Waits for Postgres, then runs `people_pubs.sync.refresh_state_runner`.
- Uses the tracked `refresh_jobs.json`, which runs routine ingestion as `app_writer`.
- Pass runner args directly after the script name.

`DB/run_backup.sh`
- Waits for Postgres, writes a same-timestamp backup bundle under
  `DB/backups/`, and deletes backups older than 30 days.
- Bundle contents:
  - `people_db_*.dump`
  - `people_globals_*.sql`
  - `people_curation_*.json`

Examples:
```
./wait_for_postgres.sh 60
./run_refresh_job.sh --list
./run_refresh_job.sh --only orcid_profiles_person --dry-run
./run_backup.sh
```

Quick status query:
```sql
SELECT source, scope_type, max(last_success_at) AS last_success_at, max(last_error_at) AS last_error_at
FROM activity.source_refresh_state
GROUP BY source, scope_type
ORDER BY source, scope_type;
```

Run history query (if `activity.refresh_run_log` exists):
```sql
SELECT source, status, finished_at, job_name, person_id, scope_key, exit_code
FROM activity.refresh_run_log
ORDER BY finished_at DESC
LIMIT 200;
```

## Current pipeline (people_pubs)

### ORCID profiles
`DB/people_pubs/sync/orcid_profiles.py`
- Refresh ORCID profile data (names, emails, employments) into the DB.
- **Employment matching is configured, and ships empty.** An employment is
  recorded only when the employer name matches one of your organisation
  patterns, from `--org-match` or the `PEOPLE_PUBS_ORG_PATTERNS` environment
  variable (semicolon- or comma-separated; matching is accent-folded,
  case-insensitive and token-based, so `Example University` also matches
  `Universität Example`). **With no patterns set, no employment matches** —
  names and emails still refresh, the `matched_employments` list is simply
  empty. This is deliberate: an unconfigured install records no employer
  rather than every employer.
- This setting is **not** institutional publication attribution (below).
  Employment matching decides which employments are stored on a person;
  attribution decides which publications reach `biblio.publications_canon`.
  Setting one does not set the other.
- Example:
```
export PEOPLE_PUBS_ORG_PATTERNS="Institute for Example Studies;exinst"
python -m people_pubs.sync.orcid_profiles --max-age-days 180 --limit 20 --debug
```

### Institutional attribution (which publications are canonical)
`DB/people_pubs/sync/attribution_rules.py`

Ingestion stores every publication it fetches in `biblio.publications`.
Whether a publication is *yours* is a separate, installation-specific
decision, held as data in `app.institution_attribution_rules`. The table
**ships empty**, and while it is empty nothing is attributed automatically:
`biblio.publications_canon` then contains only rows a curator has explicitly
included. An unconfigured install cannot silently claim other people's papers.

Matching contract:
- Evidence is the publication's stored provider payloads — the text of
  `biblio.publications.raw_json`. **`source_of_truth` is not evidence**: a
  token such as `crossref-funding` records how a record was discovered, not
  whose it is.
- A rule matches when its pattern occurs as a case-insensitive **substring**
  of that payload text. `affiliation` and `funding` rules match identically;
  the kind exists so the two lists can be managed separately.
- Patterns are literal — `%` and regex metacharacters are not special.
- A publication is attributed when **at least one active rule matches**, so an
  empty rule set attributes nothing. Blank patterns cannot be stored (CHECK
  constraint), and patterns shorter than two characters are rejected, so an
  empty setting can never match everything.
- Short or common patterns over-match. Prefer a distinctive acronym, the full
  organisation name, or a grant number.
- Attribution is stored per publication row; canon membership is per duplicate
  **group**: evidence on any member attributes the whole group, so a finding on
  one version of a paper is not lost when another version wins de-duplication.

Changing the rules is one operation, because
`biblio.publications.institution_attributed` is a cache of
`biblio.institution_attribution_signal(raw_json)`: the command changes the
rules, recomputes the cached flags, and then refreshes the serving snapshots.

Transaction boundaries and coordination with ingestion:

- **Transaction 1 — rules and flags.** The rule change and the flag recompute
  commit together or not at all. Both run under an advisory lock the database
  takes itself: a statement trigger on `app.institution_attribution_rules` and
  `biblio.recompute_institution_attribution()` acquire it exclusively, and the
  trigger that computes the flag on every publication write (inserts, payload
  updates, merges, `COPY` during a restore) holds it shared until that writer
  commits. The command therefore waits for in-flight ingestion transactions,
  recomputes under a snapshot that includes everything they committed, and
  writers that arrive meanwhile wait for the commit and then evaluate the new
  rules. Two rule commands serialise the same way. The cached flags can thus
  never disagree with the committed rules, whichever side commits first. The
  guarantee relies on `READ COMMITTED`, which every shipped writer uses; the
  lock helper refuses `REPEATABLE READ`/`SERIALIZABLE` transactions instead of
  caching a value computed from a snapshot older than the rule change it
  waited for. On the rare deadlock with a writer that already held row locks,
  the command retries the transaction.
- **Transaction 2 — the snapshots.** `publication_dedup_map_web` and
  `publications_canon_web` are refreshed inside one transaction, under the
  same advisory lock as `./refresh_publications_canon_web.sh`, so readers
  switch from the old pair to the new pair atomically. It is a separate
  transaction so the attribution lock is not held for the seconds a snapshot
  rebuild takes on a large database. A failed refresh rolls both snapshots
  back to their previous state; the committed rules and flags stay consistent
  and only the served snapshots are stale — retry with
  `recompute --refresh-only` or `./refresh_publications_canon_web.sh`.
  Refreshing is idempotent.
- `--dry-run` runs transaction 1 and rolls it back: nothing is written and no
  snapshot is refreshed. `--no-refresh` commits transaction 1 and skips
  transaction 2 on purpose.

Options are validated before any connection is opened: `list` is read-only and
takes no other option; `set`/`add`/`remove` need at least one
`--affiliation`/`--funding` (`--note` is stored with `set`/`add` only);
`recompute` never touches the rules, takes no rule option, and is the only
command that accepts `--refresh-only`, which excludes `--dry-run` and
`--no-refresh`.

```
# What is configured now?
python -m people_pubs.sync.attribution_rules list

# Replace the whole rule set (recomputes flags, refreshes snapshots)
python -m people_pubs.sync.attribution_rules set \
  --affiliation "Institute for Example Studies" \
  --affiliation "Example Institute" \
  --funding "EX-12345678"

# Add or remove a single term
python -m people_pubs.sync.attribution_rules add --funding "EX-99999999"
python -m people_pubs.sync.attribution_rules remove --affiliation "Example Institute"

# Preview a change: nothing written, no snapshot refreshed
python -m people_pubs.sync.attribution_rules set --affiliation "Example Institute" --dry-run

# Re-sync flags and snapshots without changing rules (e.g. after a restore)
python -m people_pubs.sync.attribution_rules recompute

# Retry only the snapshot refresh
python -m people_pubs.sync.attribution_rules recompute --refresh-only
```

Nothing is lost by changing your mind: `raw_json` is never modified, so any
later rule can be re-derived from the untouched payloads.

### ORCID works -> publications/authorships
`DB/people_pubs/sync/orcid_works.py`
- Fetch ORCID works, enrich with Crossref, upsert publications/authorships.
- Example:
```
python -m people_pubs.sync.orcid_works --since 2019-01-01 --only-orcid 0000-0000-0000-0060 --debug
```

Publication upsert provenance:
- `upsert_publication()` merges `source_of_truth` and source-keyed `raw_json` by default when DOI already exists.
- Search commands that expose `--rewrite-sot` pass an explicit replacement flag; use that only for deliberate provenance repair.

### Crossref backfill (metadata enrichment)
`DB/people_pubs/sync/crossref_backfill.py`
- Backfill missing metadata for existing publications from Crossref and merge duplicates by DOI.
- Use `--refresh-authorships` (optionally with `--force`) to rebuild affiliations/authorships from Crossref.
- Use `--department` to include department fields in affiliation strings when available.
- `--only-pub-id` accepts a single pub_id or a CSV file with one pub_id column.
- `--source-token` sets the Crossref token in `source_of_truth`; when provided it replaces existing `crossref*` tokens (e.g., `crossref` -> `crossref-funding`).
- `--sleep` adds a fixed delay between Crossref requests.
- Example:
```
python -m people_pubs.sync.crossref_backfill --max 200 --merge-mode doi --debug
```

### OpenAlex backfill (metadata enrichment)
`DB/people_pubs/sync/openalex_backfill.py`
- Backfill missing metadata for existing publications from OpenAlex and merge duplicates by DOI.
- OpenAlex is the last known-source fallback: it stores raw provenance and can
  fill missing title/year/venue values, but it does not overwrite metadata or
  authorships already backed by more trusted sources such as Crossref or
  DataCite.
- Use `--refresh-authorships` (optionally with `--force`) to rebuild affiliations/authorships from OpenAlex.
- Use `--department` to include department fields in affiliation strings when available.
- `--only-pub-id` accepts a single pub_id or a CSV file with one pub_id column.
- `--source-token` sets the OpenAlex token in `source_of_truth`.
- `--sleep` adds a fixed delay between OpenAlex requests.
- Example:
```
python -m people_pubs.sync.openalex_backfill --max 200 --merge-mode doi --debug
```
- Safe demo run (small scope, throttled, polite mailto) is documented step by
  step in the repo-root `QUICKSTART_CURATION.md` §1.

### Semantic Scholar backfill (metadata enrichment)
`DB/people_pubs/sync/semantic_scholar_backfill.py`
- Backfill missing metadata for existing publications from Semantic Scholar and merge duplicates by DOI.
- Use `--refresh-authorships` (optionally with `--force`) to rebuild affiliations/authorships from Semantic Scholar.
- Use `--department` to include department fields in affiliation strings when available.
- `--only-pub-id` accepts a single pub_id or a CSV file with one pub_id column.
- `--source-token` sets the Semantic Scholar token in `source_of_truth`.
- `--count` controls the title-search rows; `--debug-json` prints Semantic Scholar JSON payloads.
- `--sleep` adds a fixed delay between Semantic Scholar requests.
- Example:
```
export SEMANTIC_SCHOLAR_API_KEY="..."
python -m people_pubs.sync.semantic_scholar_backfill --max 200 --merge-mode doi --debug
```

### DataCite backfill (metadata enrichment)
`DB/people_pubs/sync/datacite_backfill.py`
- Backfill missing metadata for existing publications from DataCite and merge duplicates by DOI.
- Use `--refresh-authorships` (optionally with `--force`) to rebuild affiliations/authorships from DataCite.
- Use `--department` to include department fields in affiliation strings when available.
- `--only-pub-id` accepts a single pub_id or a CSV file with one pub_id column.
- `--source-token` sets the DataCite token in `source_of_truth`.
- `--count` controls the title-search rows; `--debug-json` prints DataCite JSON payloads.
- `--sleep` adds a fixed delay between DataCite requests.
- Example:
```
python -m people_pubs.sync.datacite_backfill --max 200 --merge-mode doi --debug
```

### DBLP backfill (metadata enrichment)
`DB/people_pubs/sync/dblp_backfill.py`
- Backfill missing metadata for existing publications from DBLP and merge duplicates by DOI.
- Use `--refresh-authorships` (optionally with `--force`) to rebuild authorships when DBLP has affiliations.
- Use `--department` to include department fields in affiliation strings when available.
- `--only-pub-id` accepts a single pub_id or a CSV file with one pub_id column.
- `--source-token` sets the DBLP token in `source_of_truth`.
- `--sleep` adds a fixed delay between DBLP requests.
- Example:
```
python -m people_pubs.sync.dblp_backfill --max 200 --merge-mode doi --debug
```

### Field precedence of the enrichment backfills
Which publications a backfill selects ("missing metadata" above: no payload
from that source yet, or a missing DOI, year or venue; every selected row
with `--force`) is separate from which stored values it replaces once it has
a match:
- Crossref, DataCite, DBLP and Semantic Scholar replace the title, year and
  venue with any value the provider supplies, whatever source supplied the
  stored one; a value the provider does not supply stays, and an existing DOI
  is never rewritten.
- OpenAlex alone consults the trust order (`TRUST_ORDER_PUBLICATIONS` in
  `people_pubs/config.py`): it fills missing values but replaces none that a
  more trusted source backed.
- So a DataCite, DBLP or Semantic Scholar run, especially with `--force` as
  in the weekly low-source canon backfill, can replace values stored from
  Crossref, and a DBLP run values stored from ORCID, although the trust order
  ranks both higher. Whether these three should defer to the trust order as
  OpenAlex does is an open decision; until it is taken, the
  characterization tests described in `TESTING_STRATEGY.md` pin the current
  behaviour, so that it changes only deliberately.

### All backfills (default: Crossref + DataCite + Semantic Scholar)
`DB/people_pubs/sync/backfill_all.py`
- Runs the standard backfills in sequence with shared flags.
- Use `--refresh-authorships` (optionally with `--force`) to rebuild affiliations/authorships for each source.
- Use `--department` to include department fields in affiliation strings when available.
- Source tokens can be customized per source (e.g., `--crossref-source-token crossref-funding`).
- `--only-pub-id` accepts a single pub_id or a CSV file with one pub_id column.
- `--only-low-source-canon --source-threshold 4` selects rows from `biblio.publications_canon`
  whose current canonical `source_of_truth` has fewer than four non-`unknown`
  source tokens. Use it with `--force` for the weekly enrichment policy so rows
  that already have complete title/year/venue/raw JSON still get checked against
  other sources.
- Low-source canon backfills select canonical rows after the durable
  dedup/canon-scope views have been applied. Updates still go through the
  existing per-source backfill paths, so provenance tokens are merged rather
  than replaced and DOI collisions use the shared publication dedupe policy.
- The scheduled `low_source_canon_backfill_query` skips Semantic Scholar for now
  because the live API path returned 403 responses in testing. Keep using
  Semantic Scholar manually, or remove `--skip-semantic` from the job once API
  access is known-good.
- `--only-unattributed-missing-payload` selects publications that carry a linked internal author
  but no institutional attribution signal (see *Institutional attribution*) **and** are still
  missing a `crossref` payload. With no attribution rules configured nothing is
  attributed, so this selector offers every eligible publication.
  This is the attribution catch-up selector, and it differs from `--only-low-source-canon` in three
  ways that matter:
  - It reads `biblio.publications`, not `biblio.publications_canon` — an unattributed row is by
    definition *not* in canon, so the canon-scoped selector can never see it.
  - It selects on **which payload is missing**, not on a token count. `-funding`/`-affiliation`
    tokens record how a paper was *discovered*, so a row found via ORCID can never earn one by being
    re-fetched; the only lever is pulling a payload we do not hold and letting the
    attribution trigger re-read `raw_json`.
  - Every attempt is written to `activity.attribution_backfill_attempts`, and
    `--attempt-cooldown-days` (default 30) skips recently completed rows.
    Without this the selector never advances: a publication the provider simply
    does not hold stays "missing" forever and is re-served on every run.
- `last_outcome = 'no_new_payload'` means all relevant provider calls completed
  without adding a missing payload. A per-publication provider exception is
  recorded as `last_outcome = 'error'`; error rows bypass the cooldown and are
  eligible for the next scheduled run. `missing_before`/`missing_after` and
  `attempt_count` preserve the audit context.
- Expect a `merged_away` count in the summary: `--merge-mode doi` deletes a row when a DOI collision
  makes it the loser of a merge. Its content moves to the winner, which the selector picks up on its
  own merits, so no attempt row is written for the deleted pub_id.
- Skip standard sources with `--skip-crossref`, `--skip-datacite`, `--skip-semantic`.
- OpenAlex is not part of the default set because it currently introduces too many metadata mistakes to be treated as a standard source. Opt in with `--with-openalex`.
- DBLP is not part of the default set because its fallback/backfill path is comparatively slow. Opt in with `--with-dblp`.
- Example:
```
export SEMANTIC_SCHOLAR_API_KEY="..."
python -m people_pubs.sync.backfill_all --max 200 --merge-mode doi --debug

# Standard set plus OpenAlex + DBLP when explicitly desired
python -m people_pubs.sync.backfill_all --with-openalex --with-dblp --max 200 --merge-mode doi --debug

# Weekly low-provenance canon enrichment policy
python -m people_pubs.sync.backfill_all --only-low-source-canon --source-threshold 4 --max 150 --merge-mode doi --force --skip-semantic --debug

# Attribution catch-up (manual; no scheduled job ships for it)
python -m people_pubs.sync.backfill_all --only-unattributed-missing-payload \
    --attempt-cooldown-days 30 --max 50 --merge-mode doi --force \
    --skip-semantic --skip-datacite --debug

# How many candidates are left, and what the attempts say
psql -c "SELECT last_outcome, count(*), max(attempt_count) FROM activity.attribution_backfill_attempts GROUP BY 1"
```

### Authorship source inspector
`DB/people_pubs/sync/authorship_sources.py`
- Report whether Crossref/OpenAlex/DataCite/Semantic Scholar/DBLP raw JSON includes authors/affiliations for a given pub.
- Examples:
```
python -m people_pubs.sync.authorship_sources --pub-id 5184
python -m people_pubs.sync.authorship_sources --doi 10.1038/s41598-021-01441-w
```

### Rebuild authorships from raw_json (DB-only)
`DB/people_pubs/sync/rebuild_authorships.py`
- Reconstruct `biblio.authorships` using stored `publications.raw_json` (no API calls).
- `--exclude-source` lets you ignore a stored source such as OpenAlex when choosing author data.
- Examples:
```
python -m people_pubs.sync.rebuild_authorships --debug
python -m people_pubs.sync.rebuild_authorships --only-pub-id refresh.csv --debug
python -m people_pubs.sync.rebuild_authorships --replace-existing --debug
python -m people_pubs.sync.rebuild_authorships --only-pub-id 834 --replace-existing --exclude-source openalex --dry-run --debug
```

### Crossref search (grant/affiliation ingest)
`DB/people_pubs/sync/crossref_search.py`
- Search Crossref by award/funder/affiliation and/or ORCID and ingest publications/authorships.
- Useful throttling flags for broad queries:
  - `--skip-existing` to skip DOIs already present in `biblio.publications`
  - `--max-new-records N` to stop after N inserted/updated publications
- ORCID mode options:
  - `--author-orcid 0000-0000-0000-0000` (repeatable)
  - `--only-person-id <id|csv|id1,id2>` to resolve ORCID(s) from DB
- Example:
```
python -m people_pubs.sync.crossref_search \
  --award 12345678 \
  --affiliation "Example University" \
  --from-year 2019 --to-year 2025 \
  --count 100 --min-interval 0.2 \
  --source-token crossref-funding \
  --debug

# If Crossref rejects award filters, use query mode:
python -m people_pubs.sync.crossref_search --award 12345678 --award-mode query --debug

# Per-person ORCID search via person_id lookup:
python -m people_pubs.sync.crossref_search --only-person-id 213 --from-year 2019 --to-year 2026 --min-interval 1.0 --debug
```

### OpenAlex search (grant/affiliation ingest)
`DB/people_pubs/sync/openalex_search.py`
- Search OpenAlex by award/affiliation and ingest publications/authorships.
- Example:
```
python -m people_pubs.sync.openalex_search \
  --award 12345678 \
  --affiliation "Example University" \
  --from-year 2019 --to-year 2025 \
  --count 100 --min-interval 0.2 \
  --source-token openalex-funding \
  --debug

# If OpenAlex rejects award filters, use query mode:
python -m people_pubs.sync.openalex_search --award 12345678 --award-mode query --debug
```

### Semantic Scholar search (grant/affiliation ingest)
`DB/people_pubs/sync/semantic_scholar_search.py`
- Search Semantic Scholar by full-text query terms (awards/affiliations/addresses are treated as free-text terms).
- Example:
```
export SEMANTIC_SCHOLAR_API_KEY="..."
python -m people_pubs.sync.semantic_scholar_search \
  --award 12345678 \
  --affiliation "Example University" \
  --from-year 2019 --to-year 2025 \
  --count 100 --min-interval 1.0 \
  --source-token semantic-funding \
  --debug
```

### DataCite search (grant/affiliation ingest)
`DB/people_pubs/sync/datacite_search.py`
- Search DataCite with full-text query terms and ingest publications/authorships.
- Example:
```
python -m people_pubs.sync.datacite_search \
  --award 12345678 \
  --affiliation "Example University" \
  --from-year 2019 --to-year 2025 \
  --count 100 --min-interval 0.2 \
  --source-token datacite-funding \
  --debug
```

### DBLP search (series/venue/query ingest)
`DB/people_pubs/sync/dblp_search.py`
- Search DBLP by series/venue/author/title (award/affiliation/address are plain text terms).
- Example:
```
python -m people_pubs.sync.dblp_search \
  --series lni --series-mode stream \
  --from-year 2019 --to-year 2025 \
  --count 100 --min-interval 0.2 \
  --source-token dblp-lni \
  --debug
```

### DBLP search from DB people
`DB/people_pubs/sync/dblp_search_people.py`
- Search DBLP per person in `app.people` using author-name queries.
- `--no-db` is not supported for this command (it must read people from DB); use `--dry-run` for no-write tests.
- Example:
```
python -m people_pubs.sync.dblp_search_people \
  --limit-people 200 \
  --max-per-person 25 \
  --series lni --series-mode stream \
  --from-year 2019 --to-year 2025 \
  --debug
```

### Manual CSV DOI ingest
`DB/people_pubs/sync/manual_csv_dois.py`
- Ingest a curated CSV of DOIs; writes a report CSV; updates authorships/aliases.
- Example:
```
python -m people_pubs.sync.manual_csv_dois --csv my_pubs.csv --report manual_csv_report.csv --dry-run
```

### Merge duplicate publications by DOI
`DB/people_pubs/sync/merge_publication_duplicates.py`
- Merge multiple publication rows that share the same DOI.
- Uses the shared canonical publication policy in `people_pubs.dedupe_policy`.
- Example:
```
python -m people_pubs.sync.merge_publication_duplicates --doi 10.1234/abc123 --debug
```

### Enqueue provisional (DOI-less) publications for review
`DB/people_pubs/sync/enqueue_provisional.py`
- Inserts one *pending* `biblio.curation_review_queue` entry
  (`review_type='publication.provisional_no_doi'`) per publication with
  `doi IS NULL` that does not already have one.
- DOI-less records are provisional: no automatic identity key, no Crossref
  enrichment, and they stay out of `biblio.publications_canon` unless a
  curator recovers a DOI (see *Crossref search* / *Manual CSV DOI ingest*) or
  includes them via a canon scope decision. `publications_canon.is_provisional`
  flags any manually included DOI-less row.
- Idempotent: publications with an existing entry of this review_type are
  skipped regardless of that entry's status, so curator resolutions are never
  re-opened by reruns.
- Examples:
```
python -m people_pubs.sync.enqueue_provisional --dry-run
python -m people_pubs.sync.enqueue_provisional
```
- End of run logs `candidates / inserted / skipped_existing / errors`.

### Manual dedup overrides (publications_clean)
`DB/people_pubs/sync/publication_dedup_overrides.py`
- Manage manual dedup overrides for `biblio.publications_clean`:
  - `merge_to`: force loser `pub_id` to map to winner `pub_id`
  - `keep_separate`: keep a `pub_id` as its own canonical row (both entries can stay visible)
- Writes both the current `pub_id` override and a durable decision in `biblio.publication_dedup_decisions`.
- Durable decisions are keyed by DOI where possible, with title/year snapshots as a fallback, so curated preprint/final-version choices can be reapplied after publication reset or reingest.
- Current `pub_id` overrides still take precedence while those rows exist; durable decisions are the recovery layer when `pub_id`s change.
- If present, also writes audit entries to `biblio.manual_corrections_log`.
- `--applied-by` sets provenance user in the corrections log (defaults to `$USER`).
- Examples:
```
# Force conference/preprint loser -> canonical winner
python -m people_pubs.sync.publication_dedup_overrides \
  set-merge --loser-pub-id 12554 --winner-pub-id 5259 --note "Conference -> journal"

# Keep same-title rows separate (different venues)
python -m people_pubs.sync.publication_dedup_overrides \
  set-keep-separate --pub-id 6449 --pub-id 3763 --note "Same title, different conference"

# List / clear overrides
python -m people_pubs.sync.publication_dedup_overrides list
python -m people_pubs.sync.publication_dedup_overrides list --include-inactive
python -m people_pubs.sync.publication_dedup_overrides clear --pub-id 12554
python -m people_pubs.sync.publication_dedup_overrides clear --decision-id 17 --note "Wrong chosen version"

# Backfill durable decisions from existing current overrides
python -m people_pubs.sync.publication_dedup_overrides backfill-current --dry-run
python -m people_pubs.sync.publication_dedup_overrides backfill-current --applied-by curation_admin
```

### Manual curation backup
`DB/people_pubs/sync/curation_backup.py`
- Export/import the manual curation tables that should survive older-dump restores:
  - `biblio.publication_dedup_decisions`
  - `biblio.publication_dedup_overrides`
  - `biblio.authorship_manual_overrides`
  - `app.person_name_aliases_manual`
  - `biblio.manual_corrections_log` (always included since 2026-07-04;
    `--include-correction-log` is still accepted as a deprecated no-op)
- Import supports `--dry-run`; use it before writing to a restored DB.
- Examples:
```
python -m people_pubs.sync.curation_backup export --output backups/curation.json
python -m people_pubs.sync.curation_backup import --input backups/curation.json --dry-run
```

### Alias backfill (DB-only)
`DB/people_pubs/sync/alias_backfill.py`
- Backfill name aliases from existing authorship rows, read through
  `biblio.authorships_curated` so curator detach (`force_null`) decisions are
  honored.
- Example:
```
python -m people_pubs.sync.alias_backfill --dry-run --debug
```
- The operator review-and-correction workflow around aliases uses the
  attach/detach and dedup-override commands documented below.

### Relink authorships (DB-only)
`DB/people_pubs/sync/relink_authorships.py`
- Link authorship rows with missing `person_id` using ORCID/name aliases.
  Ambiguous or low-confidence names are skipped (`skipped_no_match`) and stay
  reviewable through the attach/detach commands below.
- Example:
```
python -m people_pubs.sync.relink_authorships --only-pub-id 275 --debug
```

### Attach authorship (DB-only)
`DB/people_pubs/sync/attach_authorship.py`
- Manually attach authorship rows and write name aliases.
- Single mode: attach one row to an explicit `person_id`.
- CSV mode: resolve `person_id` by existing ORCID (`paper_orcid`) and attach in batch.
  - Required CSV columns: `paper_orcid`, `pub_id`
  - Selector columns: `author_position` and/or `display_name_at_pub`
  - Only existing people are used; unknown ORCIDs are skipped.
- If present, writes/updates `biblio.authorship_manual_overrides` (`mode='force_person'`) for each successful attach.
- If present, writes audit entries to `biblio.manual_corrections_log`.
- `--applied-by` sets provenance user in both tables (defaults to `$USER`).
- Example:
```
python -m people_pubs.sync.attach_authorship --pub-id 9507 --author-position 2 --person-id 16 --debug

python -m people_pubs.sync.attach_authorship \
  --csv attach_authorship.csv \
  --dry-run --debug
```

### Add people (single or CSV; ORCID required)
`DB/people_pubs/sync/add_people.py`
- Insert people into `app.people` + `app.identities_orcid` (and `pii.people_pii`).
- Optional: insert one employment row into `app.employments`.
- Examples:
```
python -m people_pubs.sync.add_people \
  --orcid 0000-0002-1825-0097 \
  --display-name "Jane Doe" \
  --person-kind internal \
  --primary-email jane@example.org

python -m people_pubs.sync.add_people \
  --orcid 0000-0002-1825-0097 \
  --display-name "Jane Doe" \
  --employment-org-name "Example University" \
  --employment-start-date 2024-10-01 \
  --employment-role-title "Postdoc"

python -m people_pubs.sync.add_people --csv people.csv --person-kind internal --debug
```

#### Roster CSV columns
Only `orcid` is required. Recognized columns:

| Column | Required | Notes |
|---|---|---|
| `orcid` | yes | Bare iD (`0000-0002-1825-0097`) or `orcid.org/...` URL; format- **and** ISO 7064 checksum-validated (2026-07-10) |
| `display_name` | no* | *Derived from `first_name` + `last_name` when blank; a row with neither is rejected |
| `first_name`, `last_name` | no | |
| `person_kind` | no | `internal` or `external`; blank falls back to `--person-kind` (default `internal`); anything else rejects the row |
| `primary_email`, `emails`/`email` | no | Multiple emails split on `,`/`;`. Stored only in `pii.people_pii` — never exported or served to a reader |
| `employment_org_name`, `employment_department`, `employment_role_title`, `employment_start_date`, `employment_end_date`, `employment_is_current`, `employment_source` | no | Employment is written only when `employment_org_name` is non-empty; other employment values without it log a warning and are skipped |

A safe synthetic demo roster lives at `DB/tests/fixtures/golden/demo_roster_minimal.csv`.

#### Validation and error behavior
- **Missing ORCID** — row rejected: `skipped_invalid` + `WARNING Skipping invalid row: Missing or invalid ORCID`; the run continues.
- **Malformed ORCID** (wrong shape, e.g. `not-an-orcid` or too few digits) — same as missing. A *format-valid* iD with a wrong ISO 7064 check digit — i.e. a mistyped iD that would otherwise become a durable identity key — is likewise rejected (`ORCID checksum invalid`; added 2026-07-10, closing the caveat previously documented here).
- **Duplicate ORCID** (vs. the DB or an earlier row in the same file) — skipped with `INFO Skipping existing ORCID ... (person_id=...)` and counted as `skipped_existing`; with `--update-existing` the existing person is updated instead. `ON CONFLICT DO NOTHING` guards on `app.identities_orcid` and `app.person_keys` back this at the DB level, so re-imports never duplicate people (idempotent).
- **Duplicate local member ID** — not applicable by design: the roster carries no institution-supplied ID column. ORCID is the required stable roster key (its duplicate handling is above), and `app.people.person_id` — assigned by the database at import — is the local member ID afterwards. `person_id` is **not** stable across a rebuild from CSV; anything external that needs a durable member reference should key on ORCID. If an institutional ID (LDAP uid, HR number) is ever adopted, it goes into `app.person_keys` as a new `key_type` — never as an email, and never as a new column on `app.people`.
- **Database errors on a row** — `errors` + `logging.exception`, then a rollback to a per-record savepoint (2026-07-10; previously a full `conn.rollback()` that also discarded rows inserted earlier in the same run while their counters survived into the summary). Only the failing record's writes are discarded; earlier and later rows commit normally at the end of the run, and the stats summary counts exactly what was persisted. Re-imports stay idempotent, so re-running after fixing the cause adds just the failed rows (covered by `tests/integration/test_add_people_partial_failure.py`).
- End of run logs a stats summary: `total / inserted / updated / skipped_existing / skipped_invalid / errors`.

### Export canonical publications (CSV; read-only)
`DB/people_pubs/sync/export_publications.py`
- Deterministic, public-safe CSV of `biblio.publications_canon` for reporting.
- Columns: `pub_id, doi, title, year, venue, source_of_truth, internal_authors` (documented in the module docstring; no contact PII exists in the source view).
- Ordering: year descending (missing years last), then case-insensitive title, then `pub_id` — repeated exports of the same data are byte-identical.
- Golden-file locked by `tests/test_roster_and_export.py`.
- Examples:
```
python -m people_pubs.sync.export_publications --output publications.csv
python -m people_pubs.sync.export_publications > publications.csv   # stdout works too
```

### Source coverage report (CSV; read-only)
`DB/people_pubs/sync/source_coverage_report.py`
- Reports which metadata providers contributed to each canonical publication
  (`biblio.publications_canon`), by splitting the cumulative `source_of_truth`
  tokens and folding specialized tokens into their source family
  (`crossref-funding` counts as `crossref`).
- Per-publication columns: `pub_id, doi, title, year, n_source_families, source_families, source_tokens`; sorted by `pub_id`, byte-identical across repeated runs.
- Summary output: total canonical publications, publications per source family, single-source count, and a provenance-breadth histogram.
- Golden-file locked by `tests/test_source_coverage_report.py`. No contact PII exists in the source view.
- Examples:
```
python -m people_pubs.sync.source_coverage_report --summary
python -m people_pubs.sync.source_coverage_report --output coverage.csv --summary-output coverage_summary.csv
```

### Member open-access PDF manifest (SQL view; read-only)

`biblio.member_open_access_pdfs`

- One row per OpenAlex-confirmed OA publication with a non-empty PDF URL and at
  least one current internal member linked through
  `biblio.authorships_curated`.
- Includes canon and non-canon publications.
- Prefers `best_oa_location.pdf_url`; if absent, selects the first deterministic
  PDF-bearing entry in `locations[]`. License and source fields describe the
  selected location.
- `member_person_ids` and `members` provide local IDs plus names/ORCIDs;
  remember that local `person_id` values are not stable across a rebuild, while
  ORCID is the durable member key.
- `rights_tier` classifies explicit CC/public-domain labels, other source terms,
  and missing licenses. It is not legal permission and must not be used alone
  to authorize redistribution.
- Examples:

```sql
SELECT pub_id, doi, pdf_url, pdf_license, rights_tier, members
FROM biblio.member_open_access_pdfs
ORDER BY pub_id;

SELECT rights_tier, count(*)
FROM biblio.member_open_access_pdfs
GROUP BY rights_tier
ORDER BY rights_tier;
```

### Merge duplicate people (DB-only)
`DB/people_pubs/sync/merge_people.py`
- Merge one duplicate `app.people.person_id` into a canonical `person_id`.
- Rewires live `person_id` references across `app`, `biblio`, `activity`, and `pii`.
- Merges refresh state, selected ORCID identities, person keys, manual aliases, and `pii.people_pii`.
- ORCIDs are not silently merged: choose which ORCID(s) to keep with `--keep-orcid`, or run interactively and the script will ask.
- If `--keep-person-id` / `--drop-person-id` are omitted in an interactive terminal, the script will ask for them.
- Aborts if both people already appear on the same publication in `biblio.authorships`.
- Dry-run by default; pass `--execute` to commit.
- Intended to be run as `postgres` because it touches `pii.*`.
- Examples:
```
# Preview (omit --execute):
python -m people_pubs.sync.merge_people \
  --keep-person-id 62 \
  --drop-person-id 281 \
  --keep-orcid 0000-0002-1825-0097 \
  --debug

# Apply the merge (include --execute):
python -m people_pubs.sync.merge_people \
  --keep-person-id 62 \
  --drop-person-id 281 \
  --keep-orcid 0000-0002-1825-0097 \
  --execute --debug

# Preview, asking for the person ids interactively:
python -m people_pubs.sync.merge_people --debug
```

### Batch ORCID + publication refresh (one-off helper)
`DB/people_pubs/sync/batch_orcid_pub_refresh.py`
- Runs a manual refresh workflow from a CSV with columns:
  - `pub_id`, `paper_orcid`, `display_name_at_pub`
- Per unique ORCID, runs:
  1. `add_people`
  2. `orcid_profiles --only-orcid`
  3. `orcid_works --since ... --only-orcid`
  4. `crossref_search --author-orcid`
- Per unique `pub_id` (optional), runs:
  5. `crossref_backfill --only-pub-id ... --force --refresh-authorships`
- Supports `--dry-run`, `--debug`, `--stop-on-error`, `--print-only`, and step skipping flags.
- Examples:
```
# Safe preview (no DB writes)
python -m people_pubs.sync.batch_orcid_pub_refresh \
  --csv batch_refresh.csv \
  --dry-run --debug --stop-on-error

# Real run
python -m people_pubs.sync.batch_orcid_pub_refresh \
  --csv batch_refresh.csv \
  --debug --stop-on-error

# Only run Crossref backfill for pub_ids in CSV
python -m people_pubs.sync.batch_orcid_pub_refresh \
  --csv batch_refresh.csv \
  --skip-add-people --skip-orcid-profiles --skip-orcid-works --skip-crossref-search \
  --dry-run --debug
```

## CLI wrapper (optional)
`DB/people_pubs/cli/people_pubs_cli.py`
- Convenience wrapper for common tasks.
- Examples:
```
python -m people_pubs.cli.people_pubs_cli orcid:profiles --limit 5 --debug
python -m people_pubs.cli.people_pubs_cli orcid:works --since 2019-01-01 --debug
python -m people_pubs.cli.people_pubs_cli crossref:backfill --limit 100 --debug
```
