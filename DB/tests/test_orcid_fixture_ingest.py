"""
Offline, fixture-backed ORCID ingestion tests.

These lock down the ORCID work-normalization path with the synthetic golden
payload so CI never depends on live ORCID availability. The DB-writing half
of the same flow is covered by tests/integration/ against a disposable
PostgreSQL.
"""

import json
from pathlib import Path

from people_pubs.orcidkit import extract_contributors, works_summary_records
from people_pubs.sync.orcid_works import (
    _build_publication_dict,
    _iter_authorship_rows,
    _merge_sot,
)

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def _orcid_work() -> dict:
    return json.loads((GOLDEN / "orcid_work_minimal.json").read_text())


def _crossref_work() -> dict:
    return json.loads((GOLDEN / "crossref_work_minimal.json").read_text())


def _contributors_standard_shape(work: dict) -> dict:
    """Golden fixture nests contributors under contributor-group; the ORCID
    /work/{put-code} API shape is contributors.contributor — build that."""
    std = dict(work)
    std["contributors"] = {
        "contributor": work["contributors"]["contributor-group"]["contributor"]
    }
    return std


def test_works_summary_records_extracts_doi_title_year():
    listing = {"group": [{"work-summary": [_orcid_work()]}]}
    records = works_summary_records(listing)
    assert len(records) == 1
    rec = records[0]
    assert rec["put_code"] == 123456789
    assert rec["title"] == "Synthetic Collective Behaviour Paper"
    assert rec["year"] == "2024"
    assert rec["doi"] == "10.5555/pubdex-fixture-001"
    assert rec["type"] == "journal-article"


def test_works_summary_records_is_defensive_about_odd_shapes():
    assert works_summary_records({}) == []
    assert works_summary_records({"group": None}) == []
    assert works_summary_records({"group": [{"work-summary": [None]}]}) == []
    # "external-ids": null happens in real ORCID responses
    work = _orcid_work()
    work["external-ids"] = None
    records = works_summary_records({"group": [{"work-summary": [work]}]})
    assert records[0]["doi"] is None


def test_extract_contributors_reads_name_and_orcid():
    std = _contributors_standard_shape(_orcid_work())
    assert extract_contributors(std) == [
        {"name": "Ada Example", "orcid": "0000-0000-0000-0001"}
    ]


def test_build_publication_dict_orcid_only():
    work = _orcid_work()
    summary = works_summary_records({"group": [{"work-summary": [work]}]})[0]
    pub = _build_publication_dict(summary, None, work)
    assert pub["doi"] == "10.5555/pubdex-fixture-001"
    assert pub["title"] == "Synthetic Collective Behaviour Paper"
    assert pub["year"] == 2024
    assert pub["journal_name"] is None
    assert pub["raw_orcid_json"] is work
    assert pub["raw_crossref_json"] is None
    assert pub["is_preprint"] is False
    assert _merge_sot("", pub) == "orcid"


def test_build_publication_dict_prefers_crossref_enrichment():
    work = _orcid_work()
    cr_msg = _crossref_work()
    summary = works_summary_records({"group": [{"work-summary": [work]}]})[0]
    pub = _build_publication_dict(summary, cr_msg, work)
    assert pub["journal_name"] == "Journal of Synthetic Metadata"
    assert pub["year"] == 2024
    assert pub["raw_crossref_json"] is cr_msg
    # Provenance is cumulative: both source families are recorded.
    assert _merge_sot("", pub) == "crossref+orcid"


def test_authorship_rows_prefer_crossref_order_then_orcid_fallback():
    cr_msg = _crossref_work()
    rows = _iter_authorship_rows(cr_msg, _orcid_work())
    # Source-provided author order must be preserved.
    assert [pos for pos, _ in rows] == [1, 2]
    assert rows[0][1]["family"] == "Example"
    assert rows[0][1]["given"] == "Ada"
    assert rows[1][1]["given"] == "Bert"

    std = _contributors_standard_shape(_orcid_work())
    fallback_rows = _iter_authorship_rows(None, std)
    assert fallback_rows == [(1, {"name": "Ada Example", "orcid": "0000-0000-0000-0001"})]
