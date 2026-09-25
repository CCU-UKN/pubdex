# PubDex

PubDex keeps an auditable record of what the people of a research
organisation have published. It pulls publication metadata from ORCID,
Crossref, OpenAlex, DataCite, DBLP and Semantic Scholar into a local
PostgreSQL database, records the provenance of every field so you can always
tell which source a value came from, and resolves people and duplicate
versions of the same work into canonical records.

Curator corrections — merged people, name aliases, duplicate decisions,
detached authorships — are stored separately from the ingested data, so they
survive re-ingestion and even a full rebuild of the publication table.

## Origins and funding

PubDex grew out of a private prototype for publication reporting whose
original development predates the grant. Its ingestion, provenance,
deduplication, curation, export and maintenance code is the starting point of
this repository. Prototype functionality included in this baseline is not
claimed as funded work.

The first funded result is turning that prototype into this public
repository. The initial commit provides a curated, public-safe copy of the
code and documentation; the MIT and CC BY 4.0 licences; a secret-free example
configuration; synthetic fixtures and a demo roster; institutional attribution
through configuration instead of embedded organisation data, and the removal of
the institution-specific behaviour that was left in the application code; a
deterministic clean-clone demonstration that ingests bundled ORCID and Crossref
payloads and produces a verified export with no metadata-provider request;
fixes found while validating that walkthrough; and the project description, progress
record and funding acknowledgement. Before publication, the copy was checked
for secrets, private rosters, restricted-source data, database dumps and
machine-local state. The private prototype's history is deliberately not
published.

[`TASK_1A.md`](TASK_1A.md) is the acceptance record for that result: what was
required, which files carry it, and the command that verifies each part.

Later funded results will be introduced in their own commits and recorded in
[`CHANGELOG.md`](CHANGELOG.md) against the task they deliver and the way it
can be verified. Funded scope is determined from the agreed plan, not merely
from commit dates.

## Funding

This project was funded through the [NGI0 Commons
Fund](https://nlnet.nl/commonsfund/), a fund established by
[NLnet](https://nlnet.nl/) with financial support from the European
Commission's [Next Generation Internet](https://ngi.eu/) programme, under the
aegis of DG Communications Networks, Content and Technology under grant
agreement No 101135429. Additional funding is made available by the Swiss State
Secretariat for Education, Research and Innovation (SERI).

Project page: [PubDex at NLnet](https://nlnet.nl/project/PubDex/).

NLnet project number: 2026-02-585.

## Repository layout

| Path | What it is |
|---|---|
| [`DB/`](DB/) | Everything: the PostgreSQL schema ([`DB/init/`](DB/init/)), the Docker Compose service ([`DB/compose.yaml`](DB/compose.yaml)), the `people_pubs` Python package, its tests, and the operator scripts |
| [`run_checks.sh`](run_checks.sh) | The default offline check suite: compilation, Markdown consistency and the offline unit and fixture tests, plus the transform-version guard in pre-commit and CI modes. Run it locally; CI invokes the same script |
| [`DB/run_task_1a_demo.sh`](DB/run_task_1a_demo.sh) | The clean-clone acceptance demonstration: a disposable database, bundled ORCID and Crossref payloads, queries and a verified export, with no metadata-provider request |
| [`CHANGELOG.md`](CHANGELOG.md) | The public progress record: what each funded result delivered, and how to verify it |
| [`TASK_1A.md`](TASK_1A.md) | The acceptance record for the first funded result, requirement by requirement |
| [`DEVELOPMENT_METHODS.md`](DEVELOPMENT_METHODS.md) | How the initial public baseline was prepared, reviewed and verified |
| [`QUICKSTART.md`](QUICKSTART.md) | Clean clone to a running database with demo data, a query over what landed, and an export |
| [`QUICKSTART_CURATION.md`](QUICKSTART_CURATION.md) | Curation, aliases and reporting exports on top of that state |
| [`.github/workflows/db-tests.yml`](.github/workflows/db-tests.yml) | GitHub adapter: it checks out the repository, installs the dependencies and invokes `run_checks.sh`. The checks themselves live in that script, so any forge can run them |
| [`LICENSE`](LICENSE), [`LICENSE-CC-BY-4.0`](LICENSE-CC-BY-4.0) | The two licences described below |

## Getting started

```bash
cp .env.example DB/.env          # then edit: passwords, WG_BIND_IP, contact address
(cd DB && docker compose -f compose.yaml up -d postgres)

python3 -m venv .venv
.venv/bin/python -m pip install -r DB/requirements-dev.txt

./run_checks.sh                  # offline; needs no database and no network
```

`run_checks.sh` is the **default offline check suite** and the one CI invokes.
The default invocation runs compilation of the package and its tests, the
Markdown link and path check, and the offline unit and fixture tests; in
pre-commit and CI modes (`--staged`, or `--base`/`--head`) it additionally runs
the transform-version guard. It is not the whole verification story — these are
separate commands:

- `(cd DB && ./run_migration_smoke.sh)` — the schema bootstraps from an empty
  database and the runtime patch re-applies cleanly.
- `./DB/run_task_1a_demo.sh` — the clean-clone acceptance path end to end,
  against its own disposable database, with no metadata-provider request.
- `(cd DB && PEOPLE_PUBS_INTEGRATION_DSN=... ./run_integration_tests.sh)` — the
  disposable-database integration suite.

To run only the unit tests, `(cd DB && ./run_tests.sh)` still works.

The schema bootstraps itself on the first start with an empty data volume.
From there:

- [`QUICKSTART.md`](QUICKSTART.md) — the full walkthrough: demo roster, ORCID
  and Crossref fixture ingestion, querying what landed, CSV export, an optional
  live demonstration, troubleshooting.
- [`DB/README.md`](DB/README.md) — setup, roles and grants, backup and
  restore, the migration model.
- [`DB/SCRIPTS.md`](DB/SCRIPTS.md) — every ingestion and maintenance command,
  with examples.

Every `people_pubs.sync` command that writes to a database supports
`--dry-run`, with two exceptions: `merge_people` only previews unless given
`--execute`, and the fixture demonstration `fixture_demo` has no preview mode.
Prefer a dry run on first contact with any database you care about.

## Licensing

Two licences, split by what a file *is*:

| What | Licence | Covers |
|---|---|---|
| Code | MIT — [`LICENSE`](LICENSE) | The Python package, the SQL schemas and migrations under [`DB/init/`](DB/init/), and the executable shell scripts |
| Content | CC BY 4.0 — [`LICENSE-CC-BY-4.0`](LICENSE-CC-BY-4.0) | The Markdown documentation and the synthetic fixtures and examples under [`DB/tests/fixtures/`](DB/tests/fixtures/) |

Two things this repository does **not** relicense:

- **Publication metadata retrieved from external sources** keeps the terms of
  the source it came from. Ingesting a record into your database does not
  place it under either licence above.
- **Third-party dependencies** keep their own licences; see
  [`DB/requirements-dev.txt`](DB/requirements-dev.txt) for what a development
  install pulls in.

Suggested attribution when reusing the documentation or the synthetic
examples:

> PubDex, by the PubDex contributors, used under
> [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Source: this
> repository.

Replace "this repository" with the URL you obtained the material from. Check
the licence text itself for what your particular reuse requires.

[`LICENSE`](LICENSE) contains the stated copyright attribution.
