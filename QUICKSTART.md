# Quickstart: clean clone → running PubDex database

This walks a new technical user from a clean clone through the whole baseline,
without any private data:

1. bootstrapping the database;
2. importing a synthetic roster;
3. ingesting ORCID and Crossref metadata from the bundled fixtures;
4. querying the resulting baseline;
5. producing an export.

Commands assume Linux/macOS with Docker (with the compose plugin),
Python 3.11+, and git.

## The whole sequence in one command

Everything the walkthrough below does by hand also runs as a single
reproducible command:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r DB/requirements-dev.txt
./DB/run_task_1a_demo.sh
```

It creates its own disposable PostgreSQL container — unique name, loopback-only
port, removed again afterwards, never Docker Compose — bootstraps the schema
from `DB/init/`, imports the synthetic roster, ingests the bundled ORCID
payload, enriches the same work from the bundled Crossref payload, queries both
the stored record and the canonical list, produces the export and verifies its
contents. It makes no ORCID, Crossref or other metadata-provider request: its
input and its expected publication data come entirely from repository
fixtures, and it cannot reach a database you already run. Docker still needs
the PostgreSQL image and the interpreter needs the installed dependencies, so
it is reproducible on supported Docker environments once those are available.

The rest of this page is that same sequence, step by step, against an
installation of your own.

Every DB-writing `people_pubs.sync` command below supports `--dry-run` except
the fixture demonstration `fixture_demo` in step 5; prefer it on first contact
with any database you care about.

Every command block starts from the **repository root**; blocks that work
inside `DB/` use a `(cd DB && ...)` subshell, so no step depends on where an
earlier step left your shell.

## 1. Clone and configure the environment

Clone the repository using the clone URL your forge displays for it, then from
the clone:

```bash
cp .env.example DB/.env
```

Edit `DB/.env` and set at least:

- `POSTGRES_PASSWORD` / `PGPASSWORD` — pick one strong local password (the
  second is the libpq default so `psql`/ingestion find it).
- `WG_BIND_IP` — the private interface address the containers additionally
  publish on. Compose fails closed when unset. Without a private interface,
  use a second loopback address such as `127.0.0.2` (Postgres is always
  published on `127.0.0.1` too; picking `127.0.0.1` here would duplicate that
  mapping).
- `PEOPLE_PUBS_CROSSREF_MAILTO` — your contact email for Crossref's polite
  pool (any reachable address).

- `PEOPLE_PUBS_ORG_PATTERNS` — leave empty for now. It lists the employers
  that count as yours during ORCID profile enrichment; empty means no
  employment is matched. It does not affect which publications are canonical
  (step 7).

The remaining keys (API keys, pgAdmin, role passwords) can stay at their
defaults for this walkthrough. The root `.env.example` is the single
consolidated template; `DB/.env` is where your live copy lives.

## 2. Start PostgreSQL (schema bootstraps automatically)

```bash
(cd DB && docker compose -f compose.yaml up -d postgres)   # add pgadmin if wanted
docker exec peopledb-postgres pg_isready -U postgres -d people_db
```

On the **first** start with an empty data volume, Postgres runs everything in
`DB/init/`: `000_init.sql` (bootstrap-only; never re-run it against a
populated database) followed by `010_ingestion_runtime_schema.sql` (the
runtime patch — its **end state** is idempotent, but applying it is not
atomic and it drops/recreates the serving views along the way, so re-apply it
to a live, serving database only in a maintenance window). Details:
`DB/README.md` §3.3 *Migration Model And Re-Run Safety*.

To prove the migration story on your machine without touching any real
database (disposable container, auto-removed):

```bash
(cd DB && ./run_migration_smoke.sh)
```

## 3. Python environment

One virtualenv at the repo root serves ingestion and DB tests:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r DB/requirements-dev.txt
```

Run the default offline check suite once now — it needs no database and no
network:

```bash
./run_checks.sh
```

## 4. Import the demo member roster

A safe, synthetic roster ships at
`DB/tests/fixtures/golden/demo_roster_minimal.csv` (Josiah Carberry is
ORCID's official *fictional* researcher, so his iD resolves against the real
registry; the other rows are invented, with checksum-valid synthetic iDs used
only by these fixtures). The roster marks Ada Example **internal** and Bert
Example **external** — that distinction decides who is reported as one of your
own authors in step 7:

```bash
(cd DB && ../.venv/bin/python -m people_pubs.sync.add_people --csv tests/fixtures/golden/demo_roster_minimal.csv --no-orcid-lookup --dry-run --debug)
(cd DB && ../.venv/bin/python -m people_pubs.sync.add_people --csv tests/fixtures/golden/demo_roster_minimal.csv --no-orcid-lookup)
```

(`--no-orcid-lookup` means the roster import makes no ORCID request; omit it
to let the importer fetch display names from the live ORCID registry.)

No connection setup is needed beyond step 1: ingestion resolves its
connection from `--dsn`, `PEOPLE_DB_DSN`, or `PG*` environment variables,
with `DB/.env` filling every gap (`people_pubs/config.py` reads it — the
template's `PGHOST`/`PGDATABASE`/`PGPASSWORD` keys cover this walkthrough).
`PEOPLE_PUBS_SKIP_DOTENV=1` switches that fill-in off; the check suites set
it, so that their tests never see your `DB/.env`.
On a fresh private instance running as `postgres` is fine; for shared
installs create the app roles first — `DB/README.md` §2.6.

Re-running the import is idempotent — existing ORCIDs are skipped. The CSV
column contract and every validation error (missing/malformed ORCID,
duplicates) are documented in `DB/SCRIPTS.md` → *Add people*.

## 5. Ingest the bundled ORCID and Crossref payloads

This is the reproducible ingestion path: two synthetic payloads that ship with
the repository, fed through exactly the code that runs against the live
providers — the same work-summary normalization, DOI handling, provenance merge
and authorship writing.

```bash
(cd DB && ../.venv/bin/python -m people_pubs.sync.fixture_demo --debug)
```

It ingests in two stages against the same work, so you can watch provenance
accumulate:

| Stage | Payload | Result |
|---|---|---|
| 1 | `DB/tests/fixtures/golden/orcid_work_minimal.json` | the work is stored with `source_of_truth = orcid` |
| 2 | `DB/tests/fixtures/golden/crossref_work_minimal.json` | the **same row** gains the venue and becomes `crossref+orcid` |

The payloads describe one invented paper — DOI `10.5555/pubdex-fixture-001`,
"Synthetic Collective Behaviour Paper" — by Ada Example
(`0000-0000-0000-0001`), who is one of the fictional people in the roster from
step 4. This step makes no metadata-provider request.

Every ingestion command that *does* contact a provider is catalogued in
`DB/SCRIPTS.md`; step 9 shows one of them against a live registry, as an
optional extra.

## 6. Query what landed

The baseline query interface is the documented PostgreSQL schema and its read
views: there is no HTTP service to start at this stage, so the queries below
are plain SQL. (A versioned network API is planned as a later funded result;
see [`TASK_1A.md`](TASK_1A.md).) `docker exec` runs `psql` inside the
container, which needs no password from the host.

The publications now linked to the fictional demo researcher, with the sources
each record came from:

```bash
docker exec peopledb-postgres psql -U postgres -d people_db -c "
  SELECT p.pub_id, p.title, p.source_of_truth
  FROM biblio.publications p
  JOIN biblio.authorships_curated a ON a.pub_id = p.pub_id
  WHERE COALESCE(a.paper_orcid, a.author_orcid) = '0000-0000-0000-0001'
  ORDER BY p.pub_id;"
```

Expected: one row, "Synthetic Collective Behaviour Paper", with
`source_of_truth = crossref+orcid` — one record, both payloads, provenance
kept cumulative rather than overwritten.

`paper_orcid` carries the iD exactly as the source payload had it, while
`author_orcid` may additionally hold one resolved from the matched person, so
the filter accepts either. Reading through `biblio.authorships_curated` rather
than `biblio.authorships` means curator decisions — attached and detached
authorships — are already applied.

How many records each source contributed:

```bash
docker exec peopledb-postgres psql -U postgres -d people_db -c "
  SELECT source_of_truth, count(*) AS publications
  FROM biblio.publications
  GROUP BY source_of_truth
  ORDER BY publications DESC, source_of_truth;"
```

`source_of_truth` accumulates every source that contributed to a record, so
after step 5 expect `crossref+orcid` rather than one row per provider.

`biblio.publications_canon` — the canonical institutional list — is still empty
here, and stays empty until attribution is configured. That is by design; step
7 explains it and then fills it.

## 7. Stored publications are not the canonical list

**Read this before you wonder where your publications went.** PubDex keeps two
different things, and a fresh install deliberately fills only the first:

| | What it is | After a fresh install |
|---|---|---|
| `biblio.publications` | every record ingestion has ever stored, from every source, with its provenance | **fills up normally** |
| `biblio.publications_canon` | the canonical list: your institution's output, one row per work after de-duplication | **stays empty** |

Ingestion never decides whose a publication is. That decision is
installation-specific configuration, and it ships empty: the table
`app.institution_attribution_rules` holds the affiliation terms and funding
identifiers that mark a publication as yours. While it is empty, **nothing is
attributed automatically**, so the canonical list contains only rows a curator
has explicitly included.

That is the safe default, not a bug: an unconfigured install cannot silently
claim other people's papers.

To configure it, name your organisation and any grant identifiers worth
matching. One command changes the rules, recomputes the cached flags and
refreshes the serving snapshots, in that order.

For this walkthrough, use the affiliation the bundled Crossref payload
actually carries, so the rule demonstrably matches something:

```bash
(cd DB && ../.venv/bin/python -m people_pubs.sync.attribution_rules set \
    --affiliation "Synthetic Institute for Collective Behaviour")

# See what is configured now
(cd DB && ../.venv/bin/python -m people_pubs.sync.attribution_rules list)
```

The canonical list is no longer empty:

```bash
docker exec peopledb-postgres psql -U postgres -d people_db -c "
  SELECT pub_id, doi, title, year, venue, source_of_truth, internal_authors
  FROM biblio.publications_canon
  ORDER BY pub_id;"
```

Expected: the fixture publication, with `Ada Example` among its internal
authors. On your own installation you would instead name your organisation and
its grant identifiers here; that fictional rule is chosen only because the
bundled payload contains it.

A rule matches when its text occurs, case-insensitively, anywhere in the
publication's stored provider payloads. Prefer a distinctive full name or
acronym and a grant number; a short or common word will over-match. The
provenance label on a record (`crossref-funding`, for example) is *not*
evidence — it records how a record was found, not whose it is.

Nothing is lost by getting this wrong at first: the raw payloads are never
modified, so you can change the rules at any time and re-derive attribution.
Full contract and the other subcommands: `DB/SCRIPTS.md` →
*Institutional attribution*.

Employment matching (`PEOPLE_PUBS_ORG_PATTERNS` in `DB/.env`) is a separate
setting and also ships empty; it decides which *employments* are recorded on a
person, not which publications are canonical.

## 8. Export the publication list

```bash
(cd DB && ../.venv/bin/python -m people_pubs.sync.export_publications --output publications.csv)
cat DB/publications.csv
```

Expected: a header plus at least one row — the fixture publication, with its
DOI, title, venue, `crossref+orcid` provenance and internal authors:

```csv
pub_id,doi,title,year,venue,source_of_truth,internal_authors
1,10.5555/pubdex-fixture-001,Synthetic Collective Behaviour Paper,2024,Journal of Synthetic Metadata,crossref+orcid,Ada Example
```

(`pub_id` is assigned by the database, so yours may differ.) Note that
`internal_authors` lists **Ada Example only**. Bert Example is an author of the
work and stays linked and visible in `biblio.authorships_curated`, but the
roster marks him external, so he is not reported as one of your organisation's
authors. Being linked and being yours are different things.

Deterministic ordering and the column set are documented in `DB/SCRIPTS.md`;
repeated exports of unchanged data are byte-identical.

## 9. Optional: a live ORCID and Crossref demonstration

Everything above works offline. If you also want to watch ingestion against the
real registries, PubDex ships a demo person for exactly that: Josiah Carberry
is ORCID's official *fictional* researcher, so his iD resolves against the live
registry without involving anybody's real data.

**This is a demonstration, not the acceptance test.** It depends on ORCID and
Crossref being reachable and on what they return today, so it is not
reproducible in the way steps 1–8 are, and it is deliberately not what CI or
`DB/run_task_1a_demo.sh` runs.

```bash
# ORCID works for Carberry (his test works date from 2008-2012, so widen the
# freshness filter, whose default is 2019-01-01):
(cd DB && ../.venv/bin/python -m people_pubs.sync.orcid_works --only-orcid 0000-0002-1825-0097 --since 2000-01-01 --dry-run --debug)
(cd DB && ../.venv/bin/python -m people_pubs.sync.orcid_works --only-orcid 0000-0002-1825-0097 --since 2000-01-01)

# Crossref enrichment/backfill over what landed:
(cd DB && ../.venv/bin/python -m people_pubs.sync.crossref_backfill --max 50 --dry-run --debug)
(cd DB && ../.venv/bin/python -m people_pubs.sync.crossref_backfill --max 50)
```

Both are idempotent and log `processed/inserted/updated/skipped/errors`
summaries at the end.

These records will **not** appear in `biblio.publications_canon`: the
attribution rule from step 7 describes the fixture's fictional institute, and
Carberry's works do not carry it. That is the safe default working as intended
— an unconfigured or differently configured install does not claim papers.

## 10. Run the checks

```bash
./run_checks.sh                         # fast offline suite (the pre-commit hook)
./run_ci_suite.sh                       # everything public CI runs; needs Docker
```

`run_checks.sh` is the default offline check suite. The default invocation
runs compilation, Markdown consistency and the offline unit/fixture suite; in
pre-commit and CI modes (`--staged`, or `--base`/`--head`) it additionally runs
the transform-version guard. `run_ci_suite.sh` runs the whole verification
story in one command, exactly as public CI does: that offline suite, the check
for committed secrets and local configuration, the migration smoke, the
acceptance demonstration and the integration suite, each Docker-backed stage on
a disposable PostgreSQL of its own. Every stage also runs on its own, for
example `(cd DB && ./run_migration_smoke.sh)`, `./DB/run_task_1a_demo.sh` or
`./DB/run_disposable_integration.sh`, and `(cd DB && ./run_tests.sh)` still
runs the unit tests alone.

## Troubleshooting

- **`docker compose` refuses to start: `WG_BIND_IP must be set`** — set it in
  `DB/.env` (see step 1); this guard prevents accidentally publishing
  Postgres on all interfaces.
- **`connection refused` on 127.0.0.1:5432** — the postgres container isn't
  up (`docker compose -f DB/compose.yaml ps`), or another local Postgres owns
  the port. `docker logs peopledb-postgres` shows bootstrap errors.
- **`password authentication failed`** — `PGPASSWORD`/`PEOPLE_DB_DSN` doesn't
  match `POSTGRES_PASSWORD` from the *first* boot: the superuser password is
  baked into the data volume when it is created. Either export the original
  password or wipe the volume (`docker compose down -v` — destroys data) and
  re-boot.
- **Ingestion says `Missing or invalid ORCID` / skips rows** — expected
  validation behavior; see `DB/SCRIPTS.md` → *Add people → Validation and
  error behavior*.
- **`relation ... does not exist` after restoring an old dump** — re-apply the
  idempotent runtime patch: `DB/README.md` §3.2 (the restore helper does this
  automatically by default). Never re-run `000_init.sql` on a populated DB.
- **`permission denied for schema biblio`** — role grants missing on a
  restored/shared DB; run the grants section in `DB/README.md` §2.6.
- **Publications ingest fine but `biblio.publications_canon` is empty** — by
  design until attribution is configured; see step 7. Check with
  `python -m people_pubs.sync.attribution_rules list`.
- **Rules are configured but canon still looks stale** — the serving snapshot
  `biblio.publications_canon_web` only changes when it is refreshed. The
  `attribution_rules` command refreshes it for you; if that step failed, retry
  with `attribution_rules recompute --refresh-only` or
  `DB/refresh_publications_canon_web.sh`.
- **No employments are recorded for anyone** — `PEOPLE_PUBS_ORG_PATTERNS` is
  empty, which matches no employer. Set it in `DB/.env`.
