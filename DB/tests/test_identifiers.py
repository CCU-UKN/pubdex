from __future__ import annotations

from datetime import date

from people_pubs.services.crossref_client import normalize_doi
from people_pubs.sync.orcid_profiles import _parse_orcid_last_updated_date
from people_pubs.utils.identifiers import normalize_orcid


def test_normalize_doi_strips_prefix_url_case_and_trailing_punctuation() -> None:
    assert normalize_doi("DOI:https://doi.org/10.5555/AbC.123. ") == "10.5555/abc.123"


def test_normalize_doi_extracts_embedded_identifier() -> None:
    assert normalize_doi("See record 10.5555/PUBDEX-FIXTURE-001, then continue.") == "10.5555/pubdex-fixture-001"


def test_normalize_doi_strips_html_escaped_landing_page_version() -> None:
    assert (
        normalize_doi("10.18419/DARUS-3060&amp;VERSION=2.1")
        == "10.18419/darus-3060"
    )
    assert (
        normalize_doi(
            "https://darus.example/dataset.xhtml?"
            "persistentId=doi:10.18419/DARUS-3060&amp;version=2.1"
        )
        == "10.18419/darus-3060"
    )
    assert (
        normalize_doi("https://doi.org/10.18419/DARUS-3060?version=2.1")
        == "10.18419/darus-3060"
    )


def test_normalize_doi_preserves_non_version_ampersand_suffix() -> None:
    assert normalize_doi("10.5555/example&part") == "10.5555/example&part"


def test_normalize_orcid_returns_canonical_uppercase_form() -> None:
    assert normalize_orcid("https://orcid.org/0000-0000-0000-000x") == "0000-0000-0000-000X"


def test_normalize_orcid_rejects_malformed_values() -> None:
    assert normalize_orcid("not-an-orcid") is None


def test_normalize_orcid_strips_schemeless_and_www_prefixes() -> None:
    assert normalize_orcid("orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert normalize_orcid("https://www.orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert normalize_orcid("http://orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"


def test_parse_orcid_last_updated_date_accepts_iso_date_and_timestamp() -> None:
    assert _parse_orcid_last_updated_date("2024-05-01") == date(2024, 5, 1)
    assert _parse_orcid_last_updated_date("2024-05-01T12:34:56Z") == date(2024, 5, 1)
