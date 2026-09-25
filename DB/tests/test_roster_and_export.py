"""
Roster-import validation and publication-export golden tests (
sections 3 and 9).

Covers, without any DB or network:
- the demo roster fixture stays valid against add_people's CSV contract,
- documented validation behavior for missing/malformed ORCID rows,
- the deterministic, PII-free CSV export of the canonical publication list.
"""

import csv
import io
from pathlib import Path

import pytest

from people_pubs.sync.add_people import _prepare_record
from people_pubs.sync.export_publications import (
    EXPORT_COLUMNS,
    sort_publications,
    write_publications_csv,
)

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


# --------------------------------------------------------------------------- #
# Demo roster fixture vs. the importer contract
# --------------------------------------------------------------------------- #

def _roster_rows() -> list[dict]:
    with (GOLDEN / "demo_roster_minimal.csv").open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_demo_roster_fixture_parses_cleanly():
    rows = _roster_rows()
    assert len(rows) == 3
    records = [_prepare_record(row, "internal") for row in rows]
    assert [r["orcid"] for r in records] == [
        "0000-0002-1825-0097",  # Josiah Carberry, ORCID's fictional researcher
        "0000-0000-0000-0001",
        "0000-0000-0000-001X",  # checksum-valid synthetic iD
    ]
    assert [r["display_name"] for r in records] == [
        "Josiah Carberry",
        "Ada Example",
        "Bert Example",
    ]
    assert [r["person_kind"] for r in records] == ["internal", "internal", "external"]


def test_demo_roster_fixture_is_public_safe():
    for row in _roster_rows():
        email = (row.get("primary_email") or "").strip()
        if email:
            assert email.endswith("@example.org")


def test_missing_orcid_is_rejected():
    with pytest.raises(ValueError, match="Missing or invalid ORCID"):
        _prepare_record({"orcid": "", "display_name": "No Orcid"}, "internal")


def test_malformed_orcid_is_rejected():
    with pytest.raises(ValueError, match="Missing or invalid ORCID"):
        _prepare_record({"orcid": "not-an-orcid", "display_name": "Bad"}, "internal")


def test_checksum_invalid_orcid_is_rejected():
    # Format-valid shape, wrong ISO 7064 MOD 11-2 check digit (a mistyped iD).
    with pytest.raises(ValueError, match="checksum"):
        _prepare_record({"orcid": "0000-0000-0000-0002", "display_name": "Bad Check"}, "internal")


def test_checksum_valid_orcid_normalizes_including_x_check_digit():
    record = _prepare_record(
        {"orcid": "https://orcid.org/0000-0000-0000-001x", "display_name": "Bert Example"},
        "internal",
    )
    assert record["orcid"] == "0000-0000-0000-001X"


def test_missing_display_name_is_rejected():
    with pytest.raises(ValueError, match="Missing display_name"):
        _prepare_record({"orcid": "0000-0000-0000-0001"}, "internal")


def test_display_name_derived_from_first_and_last():
    rec = _prepare_record(
        {"orcid": "0000-0000-0000-0001", "first_name": "Ada", "last_name": "Example"},
        "internal",
    )
    assert rec["display_name"] == "Ada Example"


def test_invalid_person_kind_is_rejected():
    with pytest.raises(ValueError, match="Invalid person_kind"):
        _prepare_record(
            {
                "orcid": "0000-0000-0000-0001",
                "display_name": "Ada Example",
                "person_kind": "robot",
            },
            "internal",
        )


# --------------------------------------------------------------------------- #
# Publication CSV export: golden file + determinism + PII stance
# --------------------------------------------------------------------------- #

_EXPORT_ROWS = [
    {
        "pub_id": 3,
        "doi": None,
        "title": "Older untitled venue-less record",
        "year": 2019,
        "venue": None,
        "source_of_truth": "orcid",
        "internal_authors": None,
    },
    {
        "pub_id": 1,
        "doi": "10.5555/pubdex-fixture-001",
        "title": "Synthetic Collective Behaviour Paper",
        "year": 2024,
        "venue": "Journal of Synthetic Metadata",
        "source_of_truth": "crossref+orcid",
        "internal_authors": "Ada Example",
    },
    {
        "pub_id": 2,
        "doi": "10.5555/pubdex-fixture-002",
        "title": "A second synthetic paper",
        "year": 2024,
        "venue": "Journal of Synthetic Metadata",
        "source_of_truth": "crossref",
        "internal_authors": "Ada Example, Cleo Example",
    },
    {
        "pub_id": 4,
        "doi": "10.5555/pubdex-fixture-004",
        "title": "Yearless provisional record",
        "year": None,
        "venue": None,
        "source_of_truth": "datacite",
        "internal_authors": None,
    },
]


def _export_text(rows) -> str:
    out = io.StringIO()
    write_publications_csv(sort_publications(rows), out)
    return out.getvalue()


def test_export_matches_golden_file():
    golden = (GOLDEN / "export_publications_minimal.csv").read_text()
    assert _export_text(_EXPORT_ROWS) == golden


def test_export_is_order_independent():
    assert _export_text(_EXPORT_ROWS) == _export_text(list(reversed(_EXPORT_ROWS)))


def test_export_columns_contain_no_contact_pii():
    forbidden = {"email", "primary_email", "emails", "phone", "address", "room"}
    assert not forbidden & set(EXPORT_COLUMNS)
