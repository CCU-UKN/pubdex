"""
Offline, fixture-backed DataCite ingestion tests.

These lock down DataCite work parsing — DOI/title/year/venue extraction,
preprint detection, ordered author iteration with ORCID normalization — and
the provenance gates the backfill applies (cumulative source_of_truth merge,
source-keyed raw_json patch, title-similarity threshold) with the synthetic
golden payload so CI never depends on live DataCite availability. The
DB-writing half of backfill_one needs a live connection and is out of scope
here.
"""

import json
from pathlib import Path

from people_pubs.sync.datacite_backfill import (
    _search_best_by_title,
    datacite_doi,
    datacite_is_preprint,
    datacite_title,
    datacite_venue,
    datacite_year,
    guess_is_preprint,
    iter_authors_from_datacite,
    merge_raw_json,
    merge_source_of_truth,
)

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def _datacite_work() -> dict:
    return json.loads((GOLDEN / "datacite_work_minimal.json").read_text())


class _FakeDataCiteClient:
    """Offline stand-in for DataCiteClient: canned search payload, no network."""

    def __init__(self, entries):
        self._entries = entries
        self.queries = []

    def search(self, query, *, size=5, page=1, filters=None):
        self.queries.append((query, filters))
        return {"data": self._entries}


def test_field_extraction_reads_doi_title_year_venue():
    entry = _datacite_work()
    assert datacite_doi(entry) == "10.5555/datacite-demo-0001"
    assert datacite_title(entry) == "Synthetic Collective Behaviour Dataset"
    assert datacite_year(entry) == 2024
    assert datacite_venue(entry) == "Journal of Synthetic Data Reports"


def test_field_extraction_fallbacks_are_defensive():
    entry = _datacite_work()
    # DOI falls back to the JSON:API id when attributes.doi is absent.
    del entry["attributes"]["doi"]
    assert datacite_doi(entry) == "10.5555/datacite-demo-0001"
    # publicationYear may arrive as a string.
    entry["attributes"]["publicationYear"] = "2024"
    assert datacite_year(entry) == 2024
    # Venue falls back to publisher when there is no container title.
    del entry["attributes"]["container"]
    assert datacite_venue(entry) == "Example Data Repository"
    assert datacite_year({}) is None
    assert datacite_venue({}) is None


def test_iter_authors_preserves_source_order_and_orcid():
    entry = _datacite_work()
    authors = list(iter_authors_from_datacite(entry))
    assert [name for name, _, _, _ in authors] == ["Carberry, Josiah", "Ada Example"]
    _, josiah_orcid, josiah_affs, josiah_corr = authors[0]
    # The ORCID URL in nameIdentifiers is normalized to the bare iD.
    assert josiah_orcid == "0000-0002-1825-0097"
    assert josiah_affs == ["Example University"]
    assert josiah_corr is False
    # Second creator has no "name": built from givenName/familyName, no ORCID.
    _, ada_orcid, ada_affs, _ = authors[1]
    assert ada_orcid is None
    assert ada_affs == ["Example University"]


def test_iter_authors_department_affiliation_is_opt_in():
    entry = _datacite_work()
    default_affs = list(iter_authors_from_datacite(entry))[0][2]
    assert "Department of Synthetic Studies" not in default_affs
    with_dept = list(iter_authors_from_datacite(entry, include_department=True))[0][2]
    assert with_dept == ["Example University", "Department of Synthetic Studies"]


def test_is_preprint_detection_and_generic_fallback():
    entry = _datacite_work()
    # A Dataset gives no verdict; backfill_one then falls back to the guess.
    assert datacite_is_preprint(entry) is None
    assert (
        guess_is_preprint(
            {},
            doi=datacite_doi(entry),
            venue=datacite_venue(entry),
            title=datacite_title(entry),
        )
        is False
    )
    preprint = _datacite_work()
    preprint["attributes"]["types"]["resourceTypeGeneral"] = "Preprint"
    assert datacite_is_preprint(preprint) is True


def test_search_best_by_title_applies_similarity_threshold():
    entry = _datacite_work()
    client = _FakeDataCiteClient([entry])
    best = _search_best_by_title(client, "Synthetic Collective Behaviour Dataset", 2024)
    assert best is entry
    # The year window is forwarded as a DataCite filter.
    assert client.queries[0][1] == "publicationYear:2024"
    # A dissimilar candidate stays below the 0.70 acceptance threshold.
    assert (
        _search_best_by_title(
            client, "Completely Unrelated Quantum Chromatography Handbook", None
        )
        is None
    )
    assert _search_best_by_title(client, "", None) is None


def test_update_gating_never_downgrades_trusted_provenance():
    # source_of_truth merge is cumulative and rerun-idempotent; the datacite
    # token joins — never displaces — a more-trusted source.
    assert merge_source_of_truth("", "datacite") == "datacite"
    assert merge_source_of_truth("crossref", "datacite") == "crossref+datacite"
    assert merge_source_of_truth("datacite", "crossref") == "crossref+datacite"
    assert merge_source_of_truth("crossref+datacite", "datacite") == "crossref+datacite"
    # The raw_json patch (as built in backfill_one) adds the datacite payload
    # without touching payloads owned by other sources.
    entry = _datacite_work()
    existing_raw = {"crossref": {"DOI": "10.5555/datacite-demo-0001"}}
    merged = merge_raw_json(
        existing_raw,
        "crossref",
        {"datacite": entry, "datacite_backfill": {"at": "2026-01-01T00:00:00+00:00"}},
    )
    assert merged["crossref"] == {"DOI": "10.5555/datacite-demo-0001"}
    assert merged["datacite"] is entry
    assert "datacite_backfill" in merged
