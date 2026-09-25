"""Offline coverage for the acceptance-demonstration ingestion path.

`people_pubs.sync.fixture_demo` feeds the bundled golden payloads into the
production ingestion path so the clean-clone walkthrough is reproducible
without a metadata provider. These tests pin the two things that make that
claim true: the fixture sources answer only from the bundled payloads and
never touch the network, and the payloads still parse into the publication,
provenance and authorship shapes the acceptance run asserts on.

The database half of the same flow is covered by
tests/integration/test_fixture_demo_integration.py.
"""

import csv
import socket

import pytest

from people_pubs.sync.fixture_demo import (
    CROSSREF_FIXTURE,
    DEMO_AFFILIATION,
    DEMO_DOI,
    DEMO_ORCID,
    DEMO_TITLE,
    FixtureCrossrefWorks,
    FixtureOrcidWorks,
    ORCID_FIXTURE,
    _full_work_shape,
    load_fixture,
)
from people_pubs.orcidkit import extract_contributors, works_summary_records
from people_pubs.sync.orcid_works import (
    _build_publication_dict,
    _iter_authorship_rows,
    _merge_sot,
)


@pytest.fixture
def orcid_work() -> dict:
    return load_fixture(ORCID_FIXTURE)


@pytest.fixture
def crossref_work() -> dict:
    return load_fixture(CROSSREF_FIXTURE)


def test_bundled_fixtures_exist_and_describe_the_same_work(orcid_work, crossref_work) -> None:
    assert ORCID_FIXTURE.is_file()
    assert CROSSREF_FIXTURE.is_file()
    summary = works_summary_records({"group": [{"work-summary": [orcid_work]}]})[0]
    assert summary["doi"] == DEMO_DOI
    assert summary["title"] == DEMO_TITLE
    assert crossref_work["DOI"] == DEMO_DOI
    # The affiliation the acceptance run configures as an attribution rule has
    # to be present in the payload, or the canonical query proves nothing.
    affiliations = [
        aff.get("name")
        for author in crossref_work["author"]
        for aff in author.get("affiliation", [])
    ]
    assert DEMO_AFFILIATION in affiliations


def test_bundled_roster_marks_one_author_internal_and_one_external() -> None:
    """The acceptance export reports only Ada, which relies on this roster."""
    roster = ORCID_FIXTURE.parent / "demo_roster_minimal.csv"
    rows = list(csv.DictReader(roster.read_text(encoding="utf-8").splitlines()))
    kinds = {row["display_name"]: row["person_kind"] for row in rows}
    assert kinds["Ada Example"] == "internal"
    assert kinds["Bert Example"] == "external"


def test_full_work_shape_moves_contributors_out_of_the_group(orcid_work) -> None:
    assert "contributor-group" in orcid_work["contributors"]
    full = _full_work_shape(orcid_work)
    assert list(full["contributors"]) == ["contributor"]
    assert extract_contributors(full) == [{"name": "Ada Example", "orcid": DEMO_ORCID}]
    # The original document is not mutated.
    assert "contributor-group" in orcid_work["contributors"]


def test_full_work_shape_passes_through_an_already_flat_record() -> None:
    flat = {"contributors": {"contributor": []}}
    assert _full_work_shape(flat) is flat
    assert _full_work_shape({}) == {}


def test_orcid_source_answers_only_for_its_own_id_and_work(orcid_work) -> None:
    source = FixtureOrcidWorks(DEMO_ORCID, orcid_work)

    listing = source.list_works(DEMO_ORCID)
    assert works_summary_records(listing)[0]["doi"] == DEMO_DOI
    assert source.list_works("0000-0000-0000-0028") == {"group": []}

    put_code = orcid_work["put-code"]
    assert source.get_work(DEMO_ORCID, put_code)["contributors"] == {
        "contributor": orcid_work["contributors"]["contributor-group"]["contributor"]
    }
    assert source.get_work(DEMO_ORCID, 999) is None
    assert source.get_work("0000-0000-0000-0028", put_code) is None


def test_crossref_source_is_a_doi_lookup_and_nothing_more(crossref_work) -> None:
    source = FixtureCrossrefWorks({DEMO_DOI: crossref_work})
    assert source.get_work(DEMO_DOI) is crossref_work
    assert source.get_work(DEMO_DOI.upper()) is crossref_work
    assert source.get_work(f"  {DEMO_DOI}  ") is crossref_work
    assert source.get_work("10.5555/unknown") is None
    assert source.get_work(None) is None
    # An empty source stands for "Crossref knows nothing about this work".
    assert FixtureCrossrefWorks().get_work(DEMO_DOI) is None


def test_fixture_sources_open_no_socket(monkeypatch, orcid_work, crossref_work) -> None:
    """The acceptance run claims to contact no provider; prove the sources cannot."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("the fixture sources must not open a network connection")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    orcid_source = FixtureOrcidWorks(DEMO_ORCID, orcid_work)
    crossref_source = FixtureCrossrefWorks({DEMO_DOI: crossref_work})
    listing = orcid_source.list_works(DEMO_ORCID)
    summary = works_summary_records(listing)[0]
    assert orcid_source.get_work(DEMO_ORCID, summary["put_code"]) is not None
    assert crossref_source.get_work(DEMO_DOI) is crossref_work


def test_two_stage_ingest_produces_cumulative_provenance(orcid_work, crossref_work) -> None:
    """Stage 1 stores the ORCID record; stage 2 enriches the same work."""
    summary = works_summary_records({"group": [{"work-summary": [orcid_work]}]})[0]

    orcid_only = _build_publication_dict(summary, None, orcid_work)
    assert orcid_only["venue"] is None or orcid_only["journal_name"] is None
    assert _merge_sot("", orcid_only) == "orcid"

    enriched = _build_publication_dict(summary, crossref_work, orcid_work)
    assert enriched["journal_name"] == "Journal of Synthetic Metadata"
    assert enriched["year"] == 2024
    assert _merge_sot("orcid", enriched) == "crossref+orcid"


def test_authorship_rows_come_from_the_payloads(orcid_work, crossref_work) -> None:
    """Stage 1 has the ORCID contributor; stage 2 has the Crossref author list."""
    orcid_rows = _iter_authorship_rows(None, _full_work_shape(orcid_work))
    assert orcid_rows == [(1, {"name": "Ada Example", "orcid": DEMO_ORCID})]

    crossref_rows = _iter_authorship_rows(crossref_work, orcid_work)
    assert [pos for pos, _ in crossref_rows] == [1, 2]
    assert crossref_rows[0][1]["given"] == "Ada"
    assert crossref_rows[1][1]["given"] == "Bert"
