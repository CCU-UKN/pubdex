"""
Offline, fixture-backed DBLP ingestion tests.

These lock down DBLP hit parsing — title cleanup, year parsing, DOI extraction
from the doi/ee fields, venue precedence, ordered author iteration (DBLP has
no structured affiliations, so authorship refresh must see empty lists) — and
the provenance gates the backfill applies (cumulative source_of_truth merge,
source-keyed raw_json patch, title-similarity threshold) with the synthetic
golden payload so CI never depends on live DBLP availability. The DB-writing
half of backfill_one needs a live connection and is out of scope here.
"""

import json
from pathlib import Path

from people_pubs.sync.dblp_backfill import (
    DblpHTTP,
    _clean_title,
    _extract_doi,
    _info_title,
    _info_venue,
    _iter_authors_from_dblp,
    _parse_year,
    merge_raw_json,
    merge_source_of_truth,
)

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def _dblp_hit() -> dict:
    return json.loads((GOLDEN / "dblp_hit_minimal.json").read_text())


def _dblp_info() -> dict:
    return _dblp_hit()["info"]


class _FakeDblpHTTP:
    """Offline stand-in for DblpHTTP: reuses the real search_best_by_title
    ranking on a canned _search result, no httpx client behind it."""

    search_best_by_title = DblpHTTP.search_best_by_title

    def __init__(self, infos):
        self._infos = infos

    def _search(self, query, *, rows=5, start=0):
        return self._infos


def test_info_title_strips_trailing_period_and_html():
    assert _info_title(_dblp_info()) == "Synthetic Collective Behaviour Paper"
    assert _clean_title("<i>Synthetic</i> Collective Title.") == "Synthetic Collective Title"
    assert _clean_title(None) is None
    assert _info_title({}) is None


def test_parse_year_handles_ints_strings_and_junk():
    assert _parse_year(_dblp_info()["year"]) == 2024
    assert _parse_year(2024) == 2024
    assert _parse_year("published in 2024") == 2024
    assert _parse_year("n/a") is None
    assert _parse_year(None) is None


def test_extract_doi_prefers_doi_field_then_ee_urls():
    info = _dblp_info()
    # The doi field wins and is normalized (lowercased).
    assert _extract_doi(info) == "10.5555/dblp-demo-0001"
    # Without it, the DOI is recovered from the ee URL (string or list).
    del info["doi"]
    assert _extract_doi(info) == "10.5555/dblp-demo-0001"
    info["ee"] = ["https://example.org/fulltext", "https://doi.org/10.5555/DBLP-DEMO-0001"]
    assert _extract_doi(info) == "10.5555/dblp-demo-0001"
    del info["ee"]
    assert _extract_doi(info) is None


def test_info_venue_precedence():
    assert _info_venue(_dblp_info()) == "Journal of Synthetic Metadata"
    # Without venue, booktitle beats journal beats series.
    assert (
        _info_venue({"booktitle": "Synthetic Conference", "journal": "Journal of Synthetic Metadata"})
        == "Synthetic Conference"
    )
    assert _info_venue({"journal": "Journal of Synthetic Metadata"}) == "Journal of Synthetic Metadata"
    assert _info_venue({}) is None


def test_iter_authors_preserves_order_without_affiliations():
    authors = list(_iter_authors_from_dblp(_dblp_info()))
    assert [name for name, _, _ in authors] == ["Josiah Carberry", "Ada Example"]
    # DBLP carries neither ORCIDs nor structured affiliations; the empty
    # affiliation list is what makes the authorship refresh skip these rows.
    assert all(orcid is None for _, orcid, _ in authors)
    assert all(affs == [] for _, _, affs in authors)
    # Single-author dict and plain-string shapes are handled too.
    assert list(_iter_authors_from_dblp({"authors": {"author": {"text": "Josiah Carberry"}}})) == [
        ("Josiah Carberry", None, [])
    ]
    assert list(_iter_authors_from_dblp({"authors": {"author": ["Ada Example"]}})) == [
        ("Ada Example", None, [])
    ]
    assert list(_iter_authors_from_dblp({})) == []


def test_search_best_by_title_applies_similarity_threshold():
    info = _dblp_info()
    dblp = _FakeDblpHTTP([info])
    assert dblp.search_best_by_title("Synthetic Collective Behaviour Paper", 2024) is info
    # A dissimilar candidate stays below the 0.70 acceptance threshold.
    assert (
        dblp.search_best_by_title("Completely Unrelated Quantum Chromatography Handbook", None)
        is None
    )
    assert dblp.search_best_by_title("", None) is None


def test_update_gating_never_downgrades_trusted_provenance():
    # source_of_truth merge is cumulative and rerun-idempotent; the dblp token
    # joins — never displaces — more-trusted sources.
    assert merge_source_of_truth("", "dblp") == "dblp"
    assert merge_source_of_truth("crossref+orcid", "dblp") == "crossref+orcid+dblp"
    assert merge_source_of_truth("dblp", "crossref") == "crossref+dblp"
    assert merge_source_of_truth("crossref+orcid+dblp", "dblp") == "crossref+orcid+dblp"
    # The raw_json patch (as built in backfill_one) adds the dblp payload
    # without touching payloads owned by other sources.
    info = _dblp_info()
    existing_raw = {"crossref": {"DOI": "10.5555/dblp-demo-0001"}}
    merged = merge_raw_json(
        existing_raw,
        "crossref",
        {"dblp": info, "dblp_backfill": {"at": "2026-01-01T00:00:00+00:00"}},
    )
    assert merged["crossref"] == {"DOI": "10.5555/dblp-demo-0001"}
    assert merged["dblp"] is info
    assert "dblp_backfill" in merged
