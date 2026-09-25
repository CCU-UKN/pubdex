"""Offline tests for the canonical source-coverage report.

Locks down people_pubs.sync.source_coverage_report: family folding of
specialized tokens (crossref-funding -> crossref), deterministic ordering,
summary metrics, the golden CSV layout, and the no-contact-PII column
contract. No network, no database.
"""
from __future__ import annotations

import io
from pathlib import Path

from people_pubs.sync.source_coverage_report import (
    COVERAGE_COLUMNS,
    build_coverage_rows,
    coverage_row,
    summarize_coverage,
    write_coverage_csv,
    write_summary_csv,
)

GOLDEN = Path(__file__).parent / "fixtures" / "golden"

_CANON_ROWS = [
    {"pub_id": 2, "doi": "10.5555/cov-0002", "title": "Beta Preprint", "year": 2023,
     "source_of_truth": "crossref-funding+crossref+openalex"},
    {"pub_id": 1, "doi": "10.5555/cov-0001", "title": "Alpha Paper", "year": 2024,
     "source_of_truth": "crossref+orcid"},
    {"pub_id": 3, "doi": None, "title": "Gamma Note", "year": None,
     "source_of_truth": "datacite-affiliation"},
    {"pub_id": 4, "doi": None, "title": "Delta Fragment", "year": None,
     "source_of_truth": ""},
]


def test_coverage_row_folds_specialized_tokens_into_families() -> None:
    row = coverage_row(_CANON_ROWS[0])
    assert row["source_tokens"] == "crossref+crossref-funding+openalex"
    assert row["source_families"] == "crossref+openalex", (
        "specialized tokens must count as their source family, not a new provider"
    )
    assert row["n_source_families"] == 2


def test_coverage_row_handles_empty_provenance() -> None:
    row = coverage_row(_CANON_ROWS[3])
    assert row["n_source_families"] == 0
    assert row["source_families"] == ""
    assert row["source_tokens"] == ""


def test_build_coverage_rows_sorts_by_pub_id() -> None:
    rows = build_coverage_rows(_CANON_ROWS)
    assert [r["pub_id"] for r in rows] == [1, 2, 3, 4]


def test_summary_counts_families_and_breadth() -> None:
    summary = dict(summarize_coverage(build_coverage_rows(_CANON_ROWS)))
    assert summary["total_canonical_publications"] == 4
    assert summary["publications_with_family=crossref"] == 2
    assert summary["publications_with_family=openalex"] == 1
    assert summary["publications_with_family=orcid"] == 1
    assert summary["publications_with_family=datacite"] == 1
    assert summary["single_source_publications"] == 1
    assert summary["publications_with_n_families=0"] == 1
    assert summary["publications_with_n_families=2"] == 2


def test_coverage_export_matches_golden_file() -> None:
    stream = io.StringIO()
    count = write_coverage_csv(build_coverage_rows(_CANON_ROWS), stream)
    assert count == 4
    assert stream.getvalue() == (GOLDEN / "source_coverage_minimal.csv").read_text()


def test_coverage_export_is_order_independent() -> None:
    stream = io.StringIO()
    write_coverage_csv(build_coverage_rows(list(reversed(_CANON_ROWS))), stream)
    assert stream.getvalue() == (GOLDEN / "source_coverage_minimal.csv").read_text()


def test_summary_csv_shape_is_stable() -> None:
    stream = io.StringIO()
    count = write_summary_csv(summarize_coverage(build_coverage_rows(_CANON_ROWS)), stream)
    lines = stream.getvalue().splitlines()
    assert lines[0] == "metric,value"
    assert count == len(lines) - 1


def test_coverage_columns_contain_no_contact_pii() -> None:
    assert COVERAGE_COLUMNS == (
        "pub_id", "doi", "title", "year",
        "n_source_families", "source_families", "source_tokens",
    )
    forbidden = {"email", "emails", "primary_email", "phone", "address"}
    assert not (set(COVERAGE_COLUMNS) & forbidden)
