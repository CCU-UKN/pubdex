"""Offline fixture-backed OpenAlex ingestion tests.

Locks down the pure transform helpers and the HTTP response handling of
people_pubs.sync.openalex_backfill without any network or database access:
author iteration (raw/display name preference, ORCID normalization,
institution fallback), venue/year extraction, preprint detection, and the
DOI-lookup/title-search wrapper (404 -> None, 400 -> filter fallback, result
unwrapping, similarity threshold). Complements the update-gating test in
test_publication_policy.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from people_pubs.sync import openalex_backfill
from people_pubs.sync.crossref_backfill import PubRow
from people_pubs.sync.openalex_backfill import (
    OpenAlexConfig,
    OpenAlexHTTP,
    _is_preprint,
    _iter_authors_from_openalex,
    _openalex_update_fields,
    _openalex_venue,
    _openalex_year,
)

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def _work() -> dict:
    return json.loads((GOLDEN / "openalex_work_minimal.json").read_text())


def test_openalex_year_prefers_publication_year_then_date_then_fallback() -> None:
    assert _openalex_year(_work()) == 2024

    date_only = _work()
    date_only.pop("publication_year")
    date_only["publication_date"] = "2023-05-01"
    assert _openalex_year(date_only) == 2023

    assert _openalex_year({}, fallback_year=2019) == 2019
    assert _openalex_year({}) is None


def test_openalex_venue_prefers_host_venue_then_primary_location() -> None:
    assert _openalex_venue(_work()) == "Journal of Synthetic Metadata"

    with_host = _work()
    with_host["host_venue"] = {"display_name": "Host Venue Journal"}
    assert _openalex_venue(with_host) == "Host Venue Journal"

    assert _openalex_venue({}) is None


def test_is_preprint_detected_from_work_type() -> None:
    assert _is_preprint({"type": "preprint"}) is True
    assert _is_preprint({"type": "Preprint"}) is True
    assert _is_preprint(_work()) is False
    assert _is_preprint({}) is False


def test_iter_authors_preserves_order_orcid_and_affiliations() -> None:
    authors = list(_iter_authors_from_openalex(_work()))
    assert len(authors) == 2

    name, orcid, affs, is_corr = authors[0]
    assert name == "Carberry, Josiah", "raw_author_name must win over display_name"
    assert orcid == "0000-0002-1825-0097", "ORCID URL must be normalized to the bare form"
    assert affs == ["Example University, Psychoceramics Unit"]
    assert is_corr is True

    name, orcid, affs, is_corr = authors[1]
    assert name == "Ada Example", "display_name is the fallback when raw name is empty"
    assert orcid is None
    assert affs == ["Example Institute"], "institutions are the affiliation fallback"
    assert is_corr is False


def test_iter_authors_skips_nameless_entries() -> None:
    work = _work()
    work["authorships"].append({"author": {}, "raw_author_name": ""})
    work["authorships"].append("not-a-dict")
    assert len(list(_iter_authors_from_openalex(work))) == 2


def test_update_fields_fill_missing_but_never_downgrade_trusted_metadata() -> None:
    trusted = PubRow(
        pub_id=1,
        doi="10.5555/openalex-demo-0001",
        title="Trusted title",
        year=None,
        venue=None,
        source_of_truth="crossref+orcid",
        raw_json={},
        is_preprint=False,
    )
    updates = _openalex_update_fields(
        trusted, title="OpenAlex title", year=2024, venue="OpenAlex venue", is_preprint=True
    )
    assert updates["title"] is None, "trusted title must not be replaced"
    assert updates["year"] == 2024, "missing year may be filled"
    assert updates["venue"] == "OpenAlex venue", "missing venue may be filled"
    assert updates["is_preprint"] is None, "preprint flag stays with the trusted source"

    openalex_only = PubRow(
        pub_id=2,
        doi=None,
        title="Old openalex title",
        year=2020,
        venue="Old venue",
        source_of_truth="openalex",
        raw_json={},
        is_preprint=False,
    )
    updates = _openalex_update_fields(
        openalex_only, title="New title", year=2021, venue="New venue", is_preprint=True
    )
    assert updates == {
        "title": "New title",
        "year": 2021,
        "venue": "New venue",
        "is_preprint": True,
    }


class _Resp:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("GET", "https://api.openalex.org/works"),
                response=None,
            )


def _http() -> OpenAlexHTTP:
    return OpenAlexHTTP(OpenAlexConfig(mailto="pytest@example.org", sleep_s=0.0))


def test_get_work_by_doi_returns_work_and_none_on_404(monkeypatch) -> None:
    work = _work()
    calls: list[str] = []

    def fake_get(self, url, params=None):
        calls.append(url)
        if "10.5555/openalex-demo-0001" in url:
            return _Resp(200, work)
        return _Resp(404, {})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    oa = _http()
    assert oa.get_work_by_doi("10.5555/openalex-demo-0001") == work
    assert oa.get_work_by_doi("10.5555/does-not-exist") is None
    assert all(url.startswith("https://api.openalex.org/works/") for url in calls)


def test_get_work_by_doi_falls_back_to_filter_query_on_400(monkeypatch) -> None:
    work = _work()

    def fake_get(self, url, params=None):
        if "filter" in (params or {}):
            return _Resp(200, {"results": [work]})
        return _Resp(400, {"error": "bad request"})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    assert _http().get_work_by_doi("10.5555/openalex-demo-0001") == work


def test_search_best_by_title_applies_similarity_threshold(monkeypatch) -> None:
    target = "Synthetic Swarm Metadata Enrichment Paper"
    good = {"title": target, "publication_year": 2024}
    noise = {"title": "Completely Unrelated Gardening Almanac", "publication_year": 2024}

    def fake_get(self, url, params=None):
        return _Resp(200, {"results": [noise, good]})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    oa = _http()
    assert oa.search_best_by_title(target, 2024) == good

    def fake_get_noise(self, url, params=None):
        return _Resp(200, {"results": [noise]})

    monkeypatch.setattr(httpx.Client, "get", fake_get_noise)
    assert oa.search_best_by_title(target, 2024) is None, "weak matches must be rejected"


def test_search_best_by_title_returns_none_without_results(monkeypatch) -> None:
    def fake_get(self, url, params=None):
        return _Resp(200, {"results": []})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    assert _http().search_best_by_title("Synthetic Swarm Metadata Enrichment Paper", None) is None
    assert _http().search_best_by_title("", None) is None
