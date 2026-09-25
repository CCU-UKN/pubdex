"""
Offline, fixture-backed Crossref ingestion tests.

These lock down Crossref work parsing — DOI/date/preprint/authorship and the
cumulative source_of_truth merge — with the synthetic golden payload so CI
never depends on live Crossref availability. The DB-writing half is covered
by tests/integration/ against a disposable PostgreSQL.
"""

import datetime as dt
import json
from pathlib import Path

from people_pubs.db.publications import merge_source_of_truth
from people_pubs.sync.crossref_backfill import (
    _iter_authors_from_crossref,
    guess_is_preprint,
    norm_title,
    normalize_doi,
    parse_crossref_year,
    pick_publication_date,
    title_similarity,
)

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def _crossref_work() -> dict:
    return json.loads((GOLDEN / "crossref_work_minimal.json").read_text())


def test_normalize_doi_lowercases_and_strips_url_prefix():
    assert normalize_doi("10.5555/PubDex-Fixture-001") == "10.5555/pubdex-fixture-001"
    assert (
        normalize_doi("https://doi.org/10.5555/pubdex-fixture-001")
        == "10.5555/pubdex-fixture-001"
    )
    assert normalize_doi("") is None
    assert normalize_doi(None) is None


def test_parse_crossref_year_and_publication_date():
    msg = _crossref_work()
    assert parse_crossref_year(msg) == 2024
    assert pick_publication_date(msg, None) == dt.date(2024, 5, 6)
    # Fallback year is used when no date-parts exist at all.
    assert pick_publication_date({}, 2023) == dt.date(2023, 1, 1)
    assert pick_publication_date({}, None) is None


def test_guess_is_preprint():
    msg = _crossref_work()
    assert guess_is_preprint(msg, msg["DOI"], msg["container-title"][0], msg["title"][0]) is False
    assert guess_is_preprint({"type": "posted-content"}, None, None, None) is True
    assert guess_is_preprint({}, None, "bioRxiv", "Some title") is True


def test_iter_authors_preserves_source_order_and_orcid():
    msg = _crossref_work()
    authors = list(_iter_authors_from_crossref(msg))
    assert [name for name, _, _, _ in authors] == ["Ada Example", "Bert Example"]
    ada_name, ada_orcid, ada_affs, ada_raw = authors[0]
    assert ada_orcid == "0000-0000-0000-0001"
    assert ada_affs == ["Synthetic Institute for Collective Behaviour"]
    assert ada_raw["given"] == "Ada"
    _, bert_orcid, bert_affs, _ = authors[1]
    assert bert_orcid is None
    assert bert_affs == ["Synthetic Institute for Collective Behaviour"]


def test_merge_source_of_truth_is_cumulative_and_stable():
    assert merge_source_of_truth("", "crossref") == "crossref"
    # Trust order puts crossref before orcid regardless of merge order.
    assert merge_source_of_truth("orcid", "crossref") == "crossref+orcid"
    assert merge_source_of_truth("crossref", "orcid") == "crossref+orcid"
    # Re-running a merge must not change the result (idempotent reruns).
    assert merge_source_of_truth("crossref+orcid", "crossref") == "crossref+orcid"
    # unknown is dropped as soon as a real source exists.
    assert merge_source_of_truth("unknown", "datacite") == "datacite"


def test_merge_source_of_truth_never_downgrades_family_specific_tokens():
    # Reruns must not downgrade e.g. crossref-funding to plain crossref.
    merged = merge_source_of_truth("crossref-funding", "crossref")
    assert merged == "crossref-funding"
    merged = merge_source_of_truth("openalex-affiliation+crossref", "openalex")
    assert "openalex-affiliation" in merged.split("+")
    assert "openalex" not in merged.split("+")


def test_norm_title_and_similarity():
    assert norm_title("<b>Synthetic</b>  Collective, Behaviour!") == (
        "synthetic collective behaviour"
    )
    assert title_similarity(
        "Synthetic Collective Behaviour Paper",
        "<i>Synthetic Collective Behaviour Paper</i>",
    ) == 1.0
    assert title_similarity("", "anything") == 0.0
