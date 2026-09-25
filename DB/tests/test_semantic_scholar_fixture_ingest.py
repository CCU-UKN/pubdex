"""
Offline, fixture-backed Semantic Scholar ingestion tests.

These lock down Semantic Scholar paper parsing — DOI normalization,
title/year/venue extraction with their fallbacks, preprint detection, ordered
author iteration with ORCID extraction — and the provenance gates the backfill
applies (cumulative source_of_truth merge, source-keyed raw_json patch,
title-similarity threshold) with the synthetic golden payload so CI never
depends on live Semantic Scholar availability. The DB-writing half of
backfill_one needs a live connection and is out of scope here.
"""

import json
from pathlib import Path

from people_pubs.sync.semantic_scholar_backfill import (
    _search_best_by_title,
    guess_is_preprint,
    iter_authors_from_semantic,
    merge_raw_json,
    merge_source_of_truth,
    semantic_doi,
    semantic_is_preprint,
    semantic_title,
    semantic_venue,
    semantic_year,
)

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def _semantic_paper() -> dict:
    return json.loads((GOLDEN / "semantic_scholar_paper_minimal.json").read_text())


class _FakeSemanticClient:
    """Offline stand-in for SemanticScholarClient: canned search payload."""

    def __init__(self, papers):
        self._papers = papers

    def search(self, query, *, limit=5, offset=0):
        return {"data": self._papers}


def _search(client, title, year):
    return _search_best_by_title(
        client, title, year, count=5, debug_json=False, debug_max_chars=4000
    )


def test_field_extraction_reads_doi_title_year_venue():
    paper = _semantic_paper()
    # externalIds.DOI is normalized (lowercased) like every other source.
    assert semantic_doi(paper) == "10.5555/semantic-demo-0001"
    assert semantic_title(paper) == "Synthetic Collective Behaviour Paper"
    assert semantic_year(paper) == 2024
    assert semantic_venue(paper) == "J. Synthetic Metadata"


def test_venue_falls_back_to_publication_venue_name():
    paper = _semantic_paper()
    paper["venue"] = ""
    assert semantic_venue(paper) == "Journal of Synthetic Metadata"
    assert semantic_venue({}) is None


def test_year_falls_back_to_publication_date():
    paper = _semantic_paper()
    del paper["year"]
    assert semantic_year(paper) == 2024
    assert semantic_year({}) is None


def test_iter_authors_preserves_source_order_and_orcid():
    paper = _semantic_paper()
    authors = list(iter_authors_from_semantic(paper))
    assert [name for name, _, _, _ in authors] == ["Josiah Carberry", "Ada Example"]
    _, josiah_orcid, josiah_affs, josiah_corr = authors[0]
    assert josiah_orcid == "0000-0002-1825-0097"
    assert josiah_affs == ["Example University"]
    assert josiah_corr is False
    _, ada_orcid, ada_affs, _ = authors[1]
    assert ada_orcid is None
    assert ada_affs == []


def test_is_preprint_detection_and_generic_fallback():
    paper = _semantic_paper()
    # JournalArticle gives no verdict; backfill_one then falls back to the guess.
    assert semantic_is_preprint(paper) is None
    assert (
        guess_is_preprint(
            paper,
            doi=semantic_doi(paper),
            venue=semantic_venue(paper),
            title=semantic_title(paper),
        )
        is False
    )
    preprint = _semantic_paper()
    preprint["publicationTypes"] = ["Preprint"]
    assert semantic_is_preprint(preprint) is True


def test_search_best_by_title_applies_similarity_threshold():
    paper = _semantic_paper()
    client = _FakeSemanticClient([paper])
    assert _search(client, "Synthetic Collective Behaviour Paper", 2024) is paper
    # A dissimilar candidate stays below the 0.70 acceptance threshold.
    assert _search(client, "Completely Unrelated Quantum Chromatography Handbook", None) is None
    assert _search(client, "", None) is None


def test_update_gating_never_downgrades_trusted_provenance():
    # source_of_truth merge is cumulative and rerun-idempotent; the semantic
    # token joins — never displaces — more-trusted sources.
    assert merge_source_of_truth("", "semantic") == "semantic"
    assert merge_source_of_truth("crossref+orcid", "semantic") == "crossref+semantic+orcid"
    assert merge_source_of_truth("semantic", "crossref") == "crossref+semantic"
    assert (
        merge_source_of_truth("crossref+semantic+orcid", "semantic")
        == "crossref+semantic+orcid"
    )
    # The raw_json patch (as built in backfill_one) adds the semantic payload
    # without touching payloads owned by other sources.
    paper = _semantic_paper()
    existing_raw = {"crossref": {"DOI": "10.5555/semantic-demo-0001"}}
    merged = merge_raw_json(
        existing_raw,
        "crossref",
        {"semantic": paper, "semantic_backfill": {"at": "2026-01-01T00:00:00+00:00"}},
    )
    assert merged["crossref"] == {"DOI": "10.5555/semantic-demo-0001"}
    assert merged["semantic"] is paper
    assert "semantic_backfill" in merged
