# Ingest search-term semantics (which portals are safe to search broadly)

**TL;DR:** discovery searches by **affiliation** or **funding** must be either *exact*
or *client-post-filtered*, and must always be *capped*. Several portals treat a multi-word
affiliation as an **"any word" (OR) relevance search**, not an exact phrase — so an "exact"
search silently becomes an "any" search that matches enormous, mostly-irrelevant result sets.
Before enabling or widening any discovery job, run the preflight estimator (below) and keep the
guardrails the tests enforce.

## Why this matters

The same centre name, sent to different portals, can produce result sets that
differ by orders of magnitude — not because the portals disagree about the
centre, but because they disagree about what a multi-word query *means*.

- **Crossref `query.affiliation` is an any-word relevance search.** It scores
  every paper containing *any* of the words in your centre's name ("Centre",
  "Advanced", "Study", …) and returns them ranked; as you page, matches decay
  to single-word hits. A long, generic centre name can therefore match a
  seven-figure number of records. Two things and only two stop those from
  being ingested: the client-side post-filter `--require-affiliation-match`
  and the per-run cap `--max-new-records`. Remove either and the job ingests
  noise until it hits a limit.
- **Structured filters are exact.** A grant number
  (`filter=award.number:…`) matches only what it names.
- **Broad affiliation discovery must have an effective post-filter before it
  is scheduled.** Crossref implements the documented filter; the other
  portals' affiliation searches have none (see the matrix) and must stay
  unscheduled.

Measure your own institution's terms before enabling anything: run the
preflight estimator below and read the raw match count.

## Matrix

Legend — **Match semantics:** `exact` (structured/identifier filter) · `any-word` (OR relevance).
**Post-filter:** is client-side `--require-affiliation-match` *used and effective*? **Scheduled:**
wired as a broad discovery job in `refresh_jobs.json`? The shipped configuration schedules **no**
discovery job, so that column records what a portal would need if one were added.

| Portal | Search type | Field / param | Match semantics | Post-filter | Scheduled | Verdict |
|---|---|---|---|---|---|---|
| **Crossref** | affiliation | `query.affiliation` | **any-word** (no phrase support at all) | ✅ yes, effective | ❌ no | **Safe ONLY with `--require-affiliation-match`** (+ cap). Crossref keeps raw author-supplied affiliation strings, so the filter works. Never run affiliation without it. |
| **Crossref** | funding | `filter=award.number:` | exact | n/a | ❌ no | Safe (structured filter; award data is sparse but bounded). |
| **Crossref** | ORCID/person | `filter=orcid:` | exact | n/a | ✅ yes | Safe. |
| **DataCite** | aff/funding | full-text query | any-word / full-text | ❌ no | ❌ no (DOI backfill only) | Would need a post-filter before scheduling as discovery. |
| **OpenAlex** | affiliation | `…institutions.display_name.search:` | any-word | ❌ no | ❌ no | Opt-in last-fallback only; not for broad discovery. |
| **OpenAlex** | funding | `grants.award_number:` | exact | n/a | ❌ no | Safe if ever scheduled. |
| **Semantic Scholar** | aff/funding | free-text terms | any-word | ❌ no | ❌ no (DOI backfill only) | Would need a post-filter before scheduling as discovery. |
| **DBLP** | aff/funding | plain-text terms | any-word (no structured fields) | ❌ no | ❌ no | CS-only; not for aff/funding discovery. |

## Safety mechanisms (already in the code)

- **`--require-affiliation-match`** (Crossref): after fetching, keep a record only if a provided
  affiliation term appears (normalised substring) in the returned author affiliations. This is what
  turns a Crossref any-word result set into the records that actually name the centre. Implemented
  in `crossref_search._crossref_item_has_expected_affiliation`.
- **`--max-new-records N`**: stop after N inserted/updated publications (per run). The hard backstop.
- **`--skip-existing`**: don't re-touch DOIs already stored, so a run makes progress on *new* work
  instead of re-walking the same result set.
- **Staleness + `limit`** (person-scoped jobs): the ORCID sweep processes the N stalest people per run
  (`stale_after_days`/`limit` in `refresh_jobs.json`), so everyone is refreshed over time without
  saturating any single portal.

## Rules for any new or edited discovery job

1. **Never rely on an API's plain affiliation/keyword search being exact.** If the portal returns an
   **any-word** match set (Crossref always; OpenAlex/Semantic/DataCite/DBLP for affiliation), the job
   **must** carry an *effective* client-side post-filter (`--require-affiliation-match`) *or* not be
   scheduled.
2. **Always bound it:** `--max-new-records` (≤ 200) **and** `--skip-existing`. Enforced by
   `tests/test_refresh_jobs_guardrails.py` — CI fails if a discovery job drops either.
3. **Preflight before enabling:** run `--estimate` (below) and look at the raw match count. A large
   count is fine *only* when a post-filter + cap bound it; a large count with neither is the hazard.
4. **Funding by grant number** (Crossref `award.number`) is structured and safe. Prefer it over
   affiliation when the goal is grant-attributed output.

## Tooling

- **Preflight one query** — add `--estimate` to the Crossref searcher; it reports how many records
  the query would match and exits **without ingesting or touching the DB**:
  ```bash
  # from DB/
  PYTHONPATH=. ../.venv/bin/python -m people_pubs.sync.crossref_search \
    --affiliation "<your centre>" \
    --from-year 2019 --to-year 2026 --estimate
  # -> ESTIMATE crossref affiliation='…': <n> matching records (NOT ingested; …)
  ```
- **Preflight every scheduled discovery job** — `estimate_search_jobs.py` reads `refresh_jobs.json`,
  runs each funding/affiliation job with `--estimate`, and prints a table (raw match, cap, post-filter,
  verdict), exiting non-zero if any job is unbounded or an affiliation job lacks a post-filter:
  ```bash
  PYTHONPATH=. ../.venv/bin/python -m people_pubs.sync.estimate_search_jobs
  ```
- **Guardrail tests** (part of the default offline check suite, `run_checks.sh` at the repository root, which is also the first stage of what CI runs):
  - `tests/test_refresh_jobs_guardrails.py` — every discovery job is capped, skips existing, and
    affiliation jobs carry the post-filter; since only the Crossref searcher implements it, an
    affiliation job on any other portal fails the check.
  - `tests/test_ingest_caps.py` — feeds thousands of synthetic records through the real Crossref
    search loop and proves `--max-records` stops the loop and `--require-affiliation-match` drops
    non-matches.
  - `tests/integration/test_ingest_max_new_records_integration.py` — disposable DB: `--max-new-records=5`
    writes exactly five rows.

## Sources

- Crossref — [query.affiliation is word/relevance, not phrase](https://community.crossref.org/t/query-affiliation/2009);
  [no exact-phrase query](https://github.com/CrossRef/rest-api-doc/issues/377);
  [filters are exact](https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/).
