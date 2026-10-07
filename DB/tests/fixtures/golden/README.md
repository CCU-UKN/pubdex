# Golden Fixtures

These fixtures are intentionally tiny and synthetic. They exist to lock down ingestion behavior without live APIs, secrets, real people, or production database rows.

- `manual_csv_minimal.csv`
  Minimal manual DOI import row. Proves the CSV reader can rely on the expected Zotero-style columns and a normalized DOI.
- `crossref_work_minimal.json`
  Minimal Crossref-like work payload with DOI, title, date parts, authors, ORCID URL, and affiliations. Use it to regression-test DOI/date/preprint/authorship parsing decisions.
- `orcid_work_minimal.json`
  Minimal ORCID work payload with DOI, publication date, and contributor ORCID. Use it to regression-test ORCID work normalization and provenance merge behavior.
- `demo_roster_minimal.csv`
  Safe demo member roster for `people_pubs.sync.add_people --csv`. Josiah Carberry (`0000-0002-1825-0097`) is ORCID's official fictional researcher, so his iD resolves against the live registry for demo ingestion runs; the other rows are invented, with `@example.org` emails only.
- `export_publications_minimal.csv`
  Golden output of `people_pubs.sync.export_publications` for the synthetic rows in `tests/test_roster_and_export.py`. Locks the export's column set, quoting, and deterministic ordering.
- `openalex_work_minimal.json`
  Minimal OpenAlex work payload (DOI, publication year/date, primary-location venue, two authorships with raw/display names, ORCID URL, raw-affiliation and institution fallbacks). Used by `tests/test_openalex_fixture_ingest.py` and the OpenAlex idempotency integration test.
- `datacite_work_minimal.json`
  Minimal DataCite JSON:API work (titles, creators with nameIdentifiers/affiliations, publicationYear, container, types). Used by `tests/test_datacite_fixture_ingest.py` and the DataCite rerun in `tests/integration/test_backfill_rerun_integration.py`.
- `semantic_scholar_paper_minimal.json`
  Minimal Semantic Scholar graph-API paper (externalIds with uppercase DOI, venue vs publicationVenue, authors with ORCID/affiliations). Used by `tests/test_semantic_scholar_fixture_ingest.py` and the Semantic Scholar rerun in `tests/integration/test_backfill_rerun_integration.py`.
- `dblp_hit_minimal.json`
  Minimal DBLP search hit (`info` with author list, trailing-period title, string year, doi + ee URL). Used by `tests/test_dblp_fixture_ingest.py` and the DBLP rerun in `tests/integration/test_backfill_rerun_integration.py`.
- `source_coverage_minimal.csv`
  Golden output of `people_pubs.sync.source_coverage_report` for the synthetic rows in `tests/test_source_coverage_report.py`. Locks the coverage report's column set, family folding, and deterministic ordering.

If a future fixture needs real-world structure, keep it public-safe and redact any PII before it is committed.
