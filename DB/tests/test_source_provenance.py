"""
Offline tests for the per-source ingest envelope.

Lock down the biblio.publications.source_provenance contract: envelope entries
are created for exactly the source payloads written in a call, carry the
people_pubs transform version (D7), hash payloads canonically, survive merges
for untouched sources, and follow the payloads on duplicate merges. The
DB-write half stays covered by the DSN-gated integration tests.
"""

import re

import people_pubs
from people_pubs.db.publications import (
    TRANSFORM_VERSION,
    _prepare_publication_row,
    build_source_provenance,
    merge_publication_provenance,
    merge_publication_source_provenance,
    merge_source_provenance,
    payload_sha256,
)


def test_package_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", people_pubs.__version__)
    assert TRANSFORM_VERSION == f"people_pubs/{people_pubs.__version__}"


def test_payload_sha256_stable_across_key_order():
    a = {"title": "T", "year": 2024, "authors": [{"name": "A"}, {"name": "B"}]}
    b = {"authors": [{"name": "A"}, {"name": "B"}], "year": 2024, "title": "T"}
    assert payload_sha256(a) == payload_sha256(b)
    assert payload_sha256(a) != payload_sha256({**a, "year": 2025})


def test_build_source_provenance_entries():
    env = build_source_provenance(
        {"crossref": {"DOI": "10.5555/x"}, "orcid": None},
        fetched_at="2026-07-09T12:00:00+00:00",
    )
    assert set(env) == {"crossref"}  # None payloads never get envelope entries
    entry = env["crossref"]
    assert entry["fetched_at"] == "2026-07-09T12:00:00+00:00"
    assert entry["transform_version"] == TRANSFORM_VERSION
    assert entry["payload_sha256"] == payload_sha256({"DOI": "10.5555/x"})


def test_merge_source_provenance_updates_win_and_preserve_others():
    existing = {"orcid": {"fetched_at": "old", "payload_sha256": "aa"}}
    updates = {"crossref": {"fetched_at": "new", "payload_sha256": "bb"}}
    merged = merge_source_provenance(existing, updates)
    assert merged["orcid"]["fetched_at"] == "old"
    assert merged["crossref"] == {"fetched_at": "new", "payload_sha256": "bb"}
    # Non-dict existing values (legacy NULL) are tolerated.
    assert merge_source_provenance(None, updates) == updates


def test_prepare_row_creates_envelope_and_preserves_untouched_sources():
    existing = {
        "title": "Old title",
        "year": 2020,
        "venue": "V",
        "source_of_truth": "orcid",
        "raw_json": {"orcid": {"put-code": 1}},
        "source_provenance": {
            "orcid": {
                "fetched_at": "2026-01-01T00:00:00+00:00",
                "transform_version": "people_pubs/0.0.0",
                "payload_sha256": payload_sha256({"put-code": 1}),
            }
        },
        "is_preprint": False,
    }
    row = _prepare_publication_row(
        {"doi": "10.5555/x", "title": "New", "raw_crossref_json": {"DOI": "10.5555/x"}},
        existing,
    )
    env = row["source_provenance"]
    # Crossref payload written in this call gets a fresh stamped entry...
    assert env["crossref"]["transform_version"] == TRANSFORM_VERSION
    assert env["crossref"]["payload_sha256"] == payload_sha256({"DOI": "10.5555/x"})
    # ...while the untouched orcid entry survives unchanged.
    assert env["orcid"]["fetched_at"] == "2026-01-01T00:00:00+00:00"
    assert row["raw_json"]["orcid"] == {"put-code": 1}


def test_prepare_row_without_payloads_has_no_envelope():
    row = _prepare_publication_row({"doi": "10.5555/y", "title": "T"})
    assert row["source_provenance"] is None


def test_merge_publication_source_provenance_follows_payloads():
    winner_raw = {"crossref": {"a": 1}}
    winner_prov = {
        "crossref": {"fetched_at": "w", "payload_sha256": "w1"},
        "openalex": {"fetched_at": "stale", "payload_sha256": "gone"},
    }
    loser_raw = {"crossref": {"b": 2}, "orcid": {"c": 3}}
    loser_prov = {
        "crossref": {"fetched_at": "l", "payload_sha256": "l1"},
        "orcid": {"fetched_at": "l", "payload_sha256": "l2"},
    }
    merged = merge_publication_source_provenance(
        winner_raw, winner_prov, loser_raw, loser_prov, "crossref", "crossref+orcid"
    )
    # Winner's kept payload keeps the winner's entry; the loser's conflicting
    # crossref entry is discarded with its payload.
    assert merged["crossref"] == {"fetched_at": "w", "payload_sha256": "w1"}
    # Gap key contributed by the loser brings the loser's entry along.
    assert merged["orcid"] == {"fetched_at": "l", "payload_sha256": "l2"}
    # Envelope entries whose payload no longer exists are dropped.
    assert "openalex" not in merged


def test_duplicate_merge_keeps_unrecognised_payload_but_not_its_envelope():
    # Boundary for keys outside the recognised source set ("zeta" is fictional):
    # the loser's payload is normalized under "legacy" and survives the merge
    # there, but its envelope entry, keyed by the original key, is not carried
    # over. This pins down current behaviour; it is not an import guarantee.
    winner_raw = {"crossref": {"a": 1}}
    winner_prov = {"crossref": {"fetched_at": "w", "payload_sha256": "w1"}}
    loser_raw = {"zeta": {"id": "Z1"}}
    loser_prov = {"zeta": {"fetched_at": "l", "payload_sha256": "l1"}}

    merged_prov = merge_publication_source_provenance(
        winner_raw, winner_prov, loser_raw, loser_prov, "crossref", "zeta"
    )
    assert merged_prov == {"crossref": {"fetched_at": "w", "payload_sha256": "w1"}}

    merged_sot, merged_raw = merge_publication_provenance(
        "crossref", winner_raw, "zeta", loser_raw
    )
    assert merged_sot == "crossref+zeta"
    assert merged_raw == {"crossref": {"a": 1}, "legacy": {"zeta": {"id": "Z1"}}}
