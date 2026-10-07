from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from people_pubs.config import (
    TRUST_ORDER_PUBLICATIONS,
    has_more_trusted_source,
    source_rank,
)
from people_pubs.dedupe_policy import PublicationCandidate, choose_canonical_publication
from people_pubs.db.authorships import compute_order_tag
from people_pubs.db.publications import (
    _prepare_publication_row,
    merge_publication_provenance,
    normalize_raw_json,
)
from people_pubs.services.crossref_client import guess_is_preprint, pick_publication_date
from people_pubs.sync.backfill_all import (
    _pub_id_arg_for_string_parser,
    count_source_tokens,
    parse_args as parse_backfill_all_args,
)
from people_pubs.sync.refresh_state_runner import _normalize_db_env
from people_pubs.sync.crossref_backfill import (
    PubRow,
    merge_source_of_truth,
    merge_source_of_truth_replace_family,
)
from people_pubs.sync.openalex_backfill import _openalex_update_fields
from people_pubs.sync.rebuild_authorships import AuthorCandidate, _choose_best_source


def test_trust_order_locks_current_source_policy() -> None:
    assert [source.value for source in TRUST_ORDER_PUBLICATIONS] == [
        "manual",
        "crossref",
        "datacite",
        "semantic",
        "orcid",
        "scholar",
        "dblp",
        "openalex",
        "unknown",
    ]


def test_source_rank_prefers_manual_to_openalex_and_unknown_last() -> None:
    assert source_rank("manual") < source_rank("crossref") < source_rank("openalex")
    assert source_rank("crossref") < source_rank("datacite") < source_rank("semantic")
    assert source_rank("semantic") < source_rank("openalex")
    assert source_rank("openalex-funding") == source_rank("openalex")
    assert source_rank("openalex") < source_rank("unknown-source")


def test_source_policy_detects_more_trusted_existing_sources() -> None:
    assert has_more_trusted_source("crossref+datacite-affiliation", "openalex") is True
    assert has_more_trusted_source("datacite", "openalex-funding") is True
    assert has_more_trusted_source("openalex", "openalex") is False
    assert has_more_trusted_source("unknown", "openalex") is False


def test_shared_canonical_policy_prefers_non_preprint() -> None:
    choice = choose_canonical_publication(
        [
            PublicationCandidate(pub_id=1, doi="10.1000/x", source_of_truth="manual", is_preprint=True),
            PublicationCandidate(pub_id=2, doi="10.1000/x", source_of_truth="orcid", is_preprint=False),
        ]
    )
    assert choice.winner_pub_id == 2


def test_shared_canonical_policy_uses_source_trust_after_preprint_status() -> None:
    choice = choose_canonical_publication(
        [
            PublicationCandidate(pub_id=1, doi="10.1000/x", source_of_truth="openalex", is_preprint=False),
            PublicationCandidate(pub_id=2, doi="10.1000/x", source_of_truth="crossref", is_preprint=False),
        ]
    )
    assert choice.winner_pub_id == 2


def test_rebuild_authorships_prefers_trusted_source_before_author_count() -> None:
    def author(name: str, affiliations: list[str] | None = None) -> AuthorCandidate:
        return AuthorCandidate(
            name=name,
            orcid=None,
            affiliations=affiliations or [],
            is_corresponding=False,
            raw={},
        )

    source, authors = _choose_best_source(
        {
            "openalex": [author("OA 1"), author("OA 2"), author("OA 3")],
            "crossref": [author("Crossref 1")],
        },
        require_affiliations=False,
    )
    assert source == "crossref"
    assert [a.name for a in authors] == ["Crossref 1"]


def test_rebuild_authorships_falls_back_to_datacite_semantic_before_openalex() -> None:
    def author(name: str) -> AuthorCandidate:
        return AuthorCandidate(
            name=name,
            orcid=None,
            affiliations=["University"],
            is_corresponding=False,
            raw={},
        )

    source, _ = _choose_best_source(
        {
            "openalex": [author("OpenAlex")],
            "semantic": [author("Semantic Scholar")],
            "datacite": [author("DataCite")],
        },
        require_affiliations=True,
    )
    assert source == "datacite"

    source, _ = _choose_best_source(
        {
            "openalex": [author("OpenAlex")],
            "semantic": [author("Semantic Scholar")],
        },
        require_affiliations=True,
    )
    assert source == "semantic"


def test_shared_canonical_policy_honors_durable_winner_override() -> None:
    choice = choose_canonical_publication(
        [
            PublicationCandidate(pub_id=1, doi="10.1000/x", source_of_truth="manual", is_preprint=False),
            PublicationCandidate(pub_id=2, doi="10.1000/x", source_of_truth="orcid", is_preprint=True),
        ],
        durable_winner_pub_id=2,
    )
    assert choice.winner_pub_id == 2
    assert choice.reason == "durable_manual_decision"


def test_refresh_runner_uses_role_specific_writer_password() -> None:
    env = _normalize_db_env(
        {
            "PGUSER": "app_writer",
            "POSTGRES_PASSWORD": "example-superuser-secret",
            "APP_WRITER_PASSWORD": "example-writer-secret",
        }
    )
    assert env["PGPASSWORD"] == "example-writer-secret"


def test_merge_source_of_truth_preserves_specialized_funding_tokens() -> None:
    assert merge_source_of_truth("manual+crossref-funding", "crossref") == "manual+crossref-funding"


def test_merge_source_of_truth_preserves_specialized_affiliation_tokens() -> None:
    assert merge_source_of_truth("manual+openalex-affiliation", "openalex") == "manual+openalex-affiliation"


def test_merge_source_of_truth_replace_family_avoids_generic_downgrade() -> None:
    assert (
        merge_source_of_truth_replace_family(
            "manual+openalex-affiliation",
            "openalex",
            "openalex",
        )
        == "manual+openalex-affiliation"
    )


def test_merge_publication_provenance_unions_loser_specialized_tokens() -> None:
    # On a DOI merge the loser's family-specific affiliation token must survive
    # and downgrade the generic 'openalex' token, even though the winner won on trust.
    merged_sot, _ = merge_publication_provenance(
        "crossref+openalex", None, "openalex-affiliation", None
    )
    tokens = merged_sot.split("+")
    assert "openalex-affiliation" in tokens
    assert "crossref" in tokens
    assert "openalex" not in tokens


def test_merge_publication_provenance_migrates_loser_only_payload() -> None:
    # A source payload only the loser holds is carried onto the winner, not lost.
    _, merged_raw = merge_publication_provenance(
        "crossref",
        {"crossref": {"DOI": "10.1/x"}},
        "openalex",
        {"openalex": {"id": "W1"}},
    )
    assert merged_raw["crossref"] == {"DOI": "10.1/x"}
    assert merged_raw["openalex"] == {"id": "W1"}


def test_merge_publication_provenance_winner_wins_raw_json_conflicts() -> None:
    # When both rows carry the same source key, the winner's payload is kept.
    _, merged_raw = merge_publication_provenance(
        "crossref",
        {"crossref": {"title": "winner"}},
        "crossref",
        {"crossref": {"title": "loser"}},
    )
    assert merged_raw["crossref"] == {"title": "winner"}


# Tokens and payload keys outside the recognised source vocabulary, such as
# values written by another installation. "zeta" and "alpha" are fictional.
# These cases pin down the actual behaviour; they are not a promise that data
# from another installation imports without loss.


def test_merge_source_of_truth_keeps_unrecognised_tokens_after_known_ones() -> None:
    # Preserved rather than discarded, and appended in sorted order after the
    # ordered known families.
    assert merge_source_of_truth("zeta+crossref", "alpha") == "crossref+alpha+zeta"


def test_merge_source_of_truth_family_handling_applies_to_known_families_only() -> None:
    # A known family's generic token yields to its qualified token...
    assert merge_source_of_truth("openalex", "openalex-affiliation") == "openalex-affiliation"
    # ...while an unrecognised family gets no family-specific handling.
    assert merge_source_of_truth("zeta", "zeta-affiliation") == "zeta+zeta-affiliation"


def test_normalize_raw_json_wraps_unrecognised_only_payload_as_legacy() -> None:
    # A single recognised token wraps an unkeyed payload under that source...
    assert normalize_raw_json({"DOI": "10.1/x"}, "crossref") == {"crossref": {"DOI": "10.1/x"}}
    # ...but a dict keyed only by an unrecognised key is not source-keyed, so it
    # is kept whole under "legacy", even when source_of_truth names that key.
    assert normalize_raw_json({"zeta": {"id": "Z1"}}, "zeta") == {"legacy": {"zeta": {"id": "Z1"}}}


def test_normalize_raw_json_keeps_unrecognised_keys_next_to_recognised_ones() -> None:
    payload = {"crossref": {"DOI": "10.1/x"}, "zeta": {"id": "Z1"}}
    assert normalize_raw_json(payload, "crossref+zeta") == payload


def test_low_source_token_count_ignores_unknown_and_empty_tokens() -> None:
    assert count_source_tokens("") == 0
    assert count_source_tokens("unknown") == 0
    assert count_source_tokens("crossref+openalex-affiliation+manual") == 3


def test_backfill_all_parses_low_source_canon_policy_args() -> None:
    args = parse_backfill_all_args(
        [
            "--only-low-source-canon",
            "--source-threshold",
            "4",
            "--max",
            "150",
            "--force",
        ]
    )
    assert args.only_low_source_canon is True
    assert args.source_threshold == 4
    assert args.max == 150
    assert args.force is True


def test_backfill_all_uses_pub_id_csv_for_long_string_parsers() -> None:
    assert _pub_id_arg_for_string_parser([1, 2, 3]) == "1,2,3"

    path = Path(_pub_id_arg_for_string_parser(list(range(1000, 1075))) or "")
    try:
        assert path.exists()
        assert path.read_text(encoding="utf-8").splitlines()[:3] == [
            "pub_id",
            "1000",
            "1001",
        ]
    finally:
        path.unlink(missing_ok=True)


def test_prepare_publication_row_merges_existing_provenance_by_default() -> None:
    row = _prepare_publication_row(
        {
            "doi": "https://doi.org/10.1000/XYZ",
            "title": "Updated",
            "year": 2025,
            "raw_crossref_json": {"DOI": "10.1000/xyz"},
            "source_of_truth": "crossref",
        },
        {
            "source_of_truth": "manual+openalex-affiliation",
            "raw_json": {
                "manual_csv": {"title": "Original"},
                "openalex": {"id": "W1"},
            },
        },
    )
    assert row["source_of_truth"] == "manual+crossref+openalex-affiliation"
    assert row["raw_json"] == {
        "manual_csv": {"title": "Original"},
        "openalex": {"id": "W1"},
        "crossref": {"DOI": "10.1000/xyz"},
    }


def test_prepare_publication_row_explicit_repair_can_replace_source_of_truth() -> None:
    row = _prepare_publication_row(
        {
            "doi": "10.1000/xyz",
            "source_of_truth": "crossref",
            "replace_source_of_truth": True,
        },
        {
            "title": "Existing title",
            "year": 2024,
            "venue": "Existing venue",
            "source_of_truth": "manual+openalex-affiliation",
            "raw_json": {"openalex": {"id": "W1"}},
            "is_preprint": False,
        },
    )
    assert row["source_of_truth"] == "crossref"
    assert row["raw_json"] == {"openalex": {"id": "W1"}}
    assert row["title"] == "Existing title"
    assert row["year"] == 2024
    assert row["venue"] == "Existing venue"
    assert row["is_preprint"] is False


def test_prepare_publication_row_does_not_nest_source_keyed_raw_payloads() -> None:
    row = _prepare_publication_row(
        {
            "doi": "10.1000/xyz",
            "raw_crossref_json": {
                "crossref": {"DOI": "10.1000/xyz"},
                "orcid": {"put-code": 123},
            },
        },
    )
    assert row["source_of_truth"] == "crossref+orcid"
    assert row["raw_json"] == {
        "crossref": {"DOI": "10.1000/xyz"},
        "orcid": {"put-code": 123},
    }


def test_openalex_backfill_updates_only_missing_fields_when_trusted_source_exists() -> None:
    existing = PubRow(
        pub_id=1,
        doi="10.1000/xyz",
        title="Trusted title",
        year=2022,
        venue="Trusted venue",
        source_of_truth="crossref+datacite",
        raw_json={},
        is_preprint=False,
    )
    updates = _openalex_update_fields(
        existing,
        title="OpenAlex title",
        year=2024,
        venue="OpenAlex venue",
        is_preprint=True,
    )
    assert updates == {
        "title": None,
        "year": None,
        "venue": None,
        "is_preprint": None,
    }

    missing = PubRow(
        pub_id=2,
        doi="10.1000/missing",
        title="",
        year=None,
        venue=None,
        source_of_truth="crossref",
        raw_json={},
        is_preprint=None,
    )
    updates = _openalex_update_fields(
        missing,
        title="OpenAlex title",
        year=2024,
        venue="OpenAlex venue",
        is_preprint=True,
    )
    assert updates == {
        "title": "OpenAlex title",
        "year": 2024,
        "venue": "OpenAlex venue",
        "is_preprint": None,
    }


@pytest.mark.parametrize(
    ("position", "n_authors", "expected"),
    [
        (1, 1, "single"),
        (1, 3, "first"),
        (2, 3, "middle"),
        (3, 3, "last"),
    ],
)
def test_compute_order_tag(position: int, n_authors: int, expected: str) -> None:
    assert compute_order_tag(position, n_authors) == expected


def test_guess_is_preprint_from_crossref_type() -> None:
    assert guess_is_preprint({"type": "posted-content"}, doi=None, venue=None, title=None) is True


def test_guess_is_preprint_from_doi_prefix() -> None:
    assert guess_is_preprint({}, doi="10.48550/arXiv.2401.12345", venue=None, title=None) is True


def test_pick_publication_date_parses_partial_crossref_date_parts() -> None:
    work = {"issued": {"date-parts": [[2024, 5]]}}
    assert pick_publication_date(work, fallback_year=None) == date(2024, 5, 1)


def test_pick_publication_date_falls_back_to_year_only() -> None:
    assert pick_publication_date({}, fallback_year=2021) == date(2021, 1, 1)
