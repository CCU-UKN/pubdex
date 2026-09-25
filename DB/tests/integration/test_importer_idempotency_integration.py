"""Disposable-DB importer idempotency tests (TESTING_STRATEGY priority
scenarios): run each importer twice with identical synthetic inputs and
assert publications, authorships, aliases, and refresh state are stable.

External APIs are replaced by in-process fakes fed from the golden fixtures;
no live network access, no real people data.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.db.publications import upsert_publication
from people_pubs.sync import crossref_backfill, manual_csv_dois, openalex_backfill, orcid_works


pytestmark = pytest.mark.integration

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _connect() -> psycopg.Connection:
    return psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True)


def _snapshot(conn: psycopg.Connection, dois: list[str]) -> dict:
    """Stable view of everything an importer may have written for our DOIs."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pub_id, doi, title, year, venue, source_of_truth,
                   ARRAY(SELECT jsonb_object_keys(raw_json) ORDER BY 1) AS raw_keys
            FROM biblio.publications WHERE doi = ANY(%s) ORDER BY doi
            """,
            (dois,),
        )
        pubs = cur.fetchall()
        cur.execute(
            """
            SELECT a.pub_id, a.person_id, a.author_name, a.author_position, a.author_orcid
            FROM biblio.authorships a
            JOIN biblio.publications p ON p.pub_id = a.pub_id
            WHERE p.doi = ANY(%s)
            ORDER BY a.pub_id, a.author_position NULLS LAST, a.author_name
            """,
            (dois,),
        )
        authorships = cur.fetchall()
    return {"pubs": pubs, "authorships": authorships}


def _cleanup(dois: list[str], person_ids: list[int]) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM biblio.authorships WHERE pub_id IN (SELECT pub_id FROM biblio.publications WHERE doi = ANY(%s))",
            (dois,),
        )
        cur.execute("DELETE FROM biblio.publications WHERE doi = ANY(%s)", (dois,))
        if person_ids:
            cur.execute("DELETE FROM app.people WHERE person_id = ANY(%s)", (person_ids,))


# --------------------------------------------------------------------------- #
# orcid_works: repeated works pull for one person
# --------------------------------------------------------------------------- #

def _orcid_work_fixture(doi: str) -> dict:
    work = json.loads((GOLDEN / "orcid_work_minimal.json").read_text())
    for ext in (work.get("external-ids") or {}).get("external-id", []):
        if ext.get("external-id-type") == "doi":
            ext["external-id-value"] = doi
    return work


class _FakeOrcidClient:
    """Stands in for orcidkit.OrcidClient inside orcid_works."""

    work: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def list_works(self, orcid: str) -> dict:
        return {"group": [{"work-summary": [self.work]}]}

    def get_work(self, orcid: str, put_code) -> dict:
        return self.work


class _FakeCrossrefClient:
    """Stands in for CrossrefClient; get_work returning None keeps the run
    ORCID-only (Crossref enrichment is covered by the backfill test)."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def get_work(self, doi: str):
        return None


def test_orcid_works_rerun_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/orcid-{slug}"
    dsn = _dsn()

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.people (display_name, person_kind) VALUES (%s, 'internal') RETURNING person_id",
            (f"orcid-idem-{slug}",),
        )
        person_id = int(cur.fetchone()["person_id"])
        cur.execute(
            "INSERT INTO app.identities_orcid (person_id, orcid) VALUES (%s, %s)",
            (person_id, "0000-0002-1825-0097"),
        )

    _FakeOrcidClient.work = _orcid_work_fixture(doi)
    monkeypatch.setattr(orcid_works, "OrcidClient", _FakeOrcidClient)
    monkeypatch.setattr(orcid_works, "CrossrefClient", _FakeCrossrefClient)

    def _run() -> None:
        orcid_works.run_orcid_works_sync(
            dsn=dsn,
            since=date(2000, 1, 1),
            limit_people=None,
            refreshed_before=None,
            max_age_days=None,
            only_person_id=person_id,
            only_orcid=None,
            crossref_mailto="pytest@example.org",
            dry_run=False,
        )

    try:
        _run()
        with _connect() as conn:
            first = _snapshot(conn, [doi])
        assert len(first["pubs"]) == 1, "importer must create exactly one publication"
        assert "orcid" in first["pubs"][0]["source_of_truth"]

        _run()
        with _connect() as conn:
            second = _snapshot(conn, [doi])
        assert second == first, "second identical run must not change anything"
    finally:
        _cleanup([doi], [person_id])


# --------------------------------------------------------------------------- #
# crossref_backfill: enrichment rerun + DOI-collision merge
# --------------------------------------------------------------------------- #

def _crossref_message(doi: str) -> dict:
    msg = json.loads((GOLDEN / "crossref_work_minimal.json").read_text())
    msg["DOI"] = doi
    return msg


class _FakeCrossrefHTTP:
    """Stands in for crossref_backfill.CrossrefHTTP."""

    by_doi: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def get_work(self, doi: str):
        return self.by_doi.get((doi or "").lower())

    def search_best_by_title(self, title, year):
        return None

    def close(self) -> None:
        return None


def test_crossref_backfill_rerun_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/crbf-{slug}"
    dsn = _dsn()

    with _connect() as conn:
        pub_id = upsert_publication(
            conn,
            {"doi": doi, "title": f"sparse orcid row {slug}", "year": 2024,
             "raw_orcid_json": {"put-code": 1}},
        )

    _FakeCrossrefHTTP.by_doi = {doi: _crossref_message(doi)}
    monkeypatch.setattr(crossref_backfill, "CrossrefHTTP", _FakeCrossrefHTTP)

    def _run() -> None:
        crossref_backfill.run_crossref_backfill(
            dsn=dsn,
            mailto=None,
            max_n=10,
            only_pub_ids=[pub_id],
            merge_mode="doi",
            sleep_s=0.0,
            dry_run=False,
            force=True,
            refresh_authorships=True,
            source_token="crossref",
            include_department=False,
        )

    try:
        _run()
        with _connect() as conn:
            first = _snapshot(conn, [doi])
        assert len(first["pubs"]) == 1
        sot = first["pubs"][0]["source_of_truth"]
        assert "orcid" in sot and "crossref" in sot, "provenance must be cumulative"
        assert set(first["pubs"][0]["raw_keys"]) >= {"orcid", "crossref"}
        assert len(first["authorships"]) == 2, "fixture has two authors"

        _run()
        with _connect() as conn:
            second = _snapshot(conn, [doi])
        assert second == first, "second identical run must not change anything"
    finally:
        _cleanup([doi], [])


def test_crossref_backfill_doi_collision_merges_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    slug = uuid4().hex[:12]
    doi_a = f"10.5555/collide-a-{slug}"
    doi_b = f"10.5555/collide-b-{slug}"
    dsn = _dsn()

    with _connect() as conn:
        pub_a = upsert_publication(
            conn,
            {"doi": doi_a, "title": f"collision fixture {slug}", "year": 2024,
             "raw_orcid_json": {"put-code": 2}},
        )
        upsert_publication(
            conn,
            {"doi": doi_b, "title": f"collision fixture {slug}", "year": 2024,
             "raw_crossref_json": {"DOI": doi_b}},
        )

    # Crossref reports pub A's canonical DOI as doi_b -> DOI collision.
    _FakeCrossrefHTTP.by_doi = {doi_a: _crossref_message(doi_b), doi_b: _crossref_message(doi_b)}
    monkeypatch.setattr(crossref_backfill, "CrossrefHTTP", _FakeCrossrefHTTP)

    def _run(target: int) -> None:
        crossref_backfill.run_crossref_backfill(
            dsn=dsn,
            mailto=None,
            max_n=10,
            only_pub_ids=[target],
            merge_mode="doi",
            sleep_s=0.0,
            dry_run=False,
            force=True,
            refresh_authorships=False,
            source_token="crossref",
            include_department=False,
        )

    try:
        _run(pub_a)
        with _connect() as conn:
            after = _snapshot(conn, [doi_a, doi_b])
        assert len(after["pubs"]) == 1, "DOI collision must merge to one publication"
        merged = after["pubs"][0]
        assert merged["doi"] == doi_b
        # The loser's provenance must survive the merge: both its raw_json
        # payload and its source_of_truth tokens are folded into the winner
        # (merge_publication_provenance). Here the processed row is the loser
        # (orcid < crossref), so raw_json was already migrated by the enrichment
        # patch; the token union is what was added alongside it.
        assert set(merged["raw_keys"]) >= {"orcid", "crossref"}, "merge must not lose source payloads"
        assert {"crossref", "orcid"} <= set(merged["source_of_truth"].split("+")), (
            "cross-row merge must union the loser's source_of_truth tokens"
        )

        # rerun against the surviving row: nothing changes
        _run(int(merged["pub_id"]))
        with _connect() as conn:
            again = _snapshot(conn, [doi_a, doi_b])
        assert again == after
    finally:
        _cleanup([doi_a, doi_b], [])


def test_crossref_backfill_doi_collision_unions_when_processed_row_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ordering the test above does not cover: the *processed* row wins, so the
    # loser's raw_json is NOT carried by the enrichment patch. Before
    # merge_publication_provenance, the loser's payload and tokens were lost.
    slug = uuid4().hex[:12]
    doi_a = f"10.5555/win-a-{slug}"
    doi_b = f"10.5555/win-b-{slug}"
    dsn = _dsn()

    with _connect() as conn:
        # Processed row is 'datacite' (rank 2) -> wins over the 'openalex' row (rank 7).
        pub_a = upsert_publication(
            conn,
            {"doi": doi_a, "title": f"win fixture {slug}", "year": 2024,
             "raw_datacite_json": {"id": f"dc-{slug}"}},
        )
        upsert_publication(
            conn,
            {"doi": doi_b, "title": f"win fixture {slug}", "year": 2024,
             "raw_openalex_json": {"id": f"https://openalex.org/{slug}"}},
        )

    # Crossref reports pub A's canonical DOI as doi_b -> collision; A wins.
    _FakeCrossrefHTTP.by_doi = {doi_a: _crossref_message(doi_b), doi_b: _crossref_message(doi_b)}
    monkeypatch.setattr(crossref_backfill, "CrossrefHTTP", _FakeCrossrefHTTP)

    def _run(target: int) -> None:
        crossref_backfill.run_crossref_backfill(
            dsn=dsn,
            mailto=None,
            max_n=10,
            only_pub_ids=[target],
            merge_mode="doi",
            sleep_s=0.0,
            dry_run=False,
            force=True,
            refresh_authorships=False,
            source_token="crossref",
            include_department=False,
        )

    try:
        _run(pub_a)
        with _connect() as conn:
            after = _snapshot(conn, [doi_a, doi_b])
        assert len(after["pubs"]) == 1, "DOI collision must merge to one publication"
        merged = after["pubs"][0]
        assert merged["doi"] == doi_b
        # Loser (openalex) payload + token preserved even though it did not win.
        assert set(merged["raw_keys"]) >= {"datacite", "crossref", "openalex"}, (
            "loser raw_json payload must be migrated when the processed row wins"
        )
        assert {"crossref", "datacite", "openalex"} <= set(merged["source_of_truth"].split("+")), (
            "loser source_of_truth tokens must be unioned when the processed row wins"
        )
    finally:
        _cleanup([doi_a, doi_b], [])


# --------------------------------------------------------------------------- #
# openalex_backfill: enrichment rerun (last-fallback source)
# --------------------------------------------------------------------------- #

def _openalex_work_fixture(doi: str) -> dict:
    work = json.loads((GOLDEN / "openalex_work_minimal.json").read_text())
    work["doi"] = f"https://doi.org/{doi}"
    work["ids"]["doi"] = f"https://doi.org/{doi}"
    return work


class _FakeOpenAlexHTTP:
    """Stands in for openalex_backfill.OpenAlexHTTP."""

    by_doi: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def get_work_by_doi(self, doi: str):
        return self.by_doi.get((doi or "").lower())

    def search_best_by_title(self, title, year, rows: int = 5):
        return None

    def close(self) -> None:
        return None


def test_openalex_backfill_rerun_is_idempotent_and_last_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = uuid4().hex[:12]
    doi_trusted = f"10.5555/oa-trusted-{slug}"  # orcid-backed row: fill-only
    doi_gap = f"10.5555/oa-gap-{slug}"          # openalex-only row: full enrich
    dsn = _dsn()

    with _connect() as conn:
        pub_trusted = upsert_publication(
            conn,
            {"doi": doi_trusted, "title": f"orcid backed row {slug}", "year": 2024,
             "raw_orcid_json": {"put-code": 4}},
        )
        pub_gap = upsert_publication(
            conn,
            {"doi": doi_gap, "title": f"openalex only row {slug}", "year": 2024,
             "raw_openalex_json": {"id": f"https://openalex.org/W{slug}"}},
        )

    _FakeOpenAlexHTTP.by_doi = {
        doi_trusted: _openalex_work_fixture(doi_trusted),
        doi_gap: _openalex_work_fixture(doi_gap),
    }
    monkeypatch.setattr(openalex_backfill, "OpenAlexHTTP", _FakeOpenAlexHTTP)

    def _run(force: bool) -> None:
        openalex_backfill.run_openalex_backfill(
            dsn=dsn,
            mailto=None,
            max_n=10,
            only_pub_ids=[pub_trusted, pub_gap],
            merge_mode="doi",
            sleep_s=0.0,
            dry_run=False,
            force=force,
            source_token="openalex",
            rewrite_sot=None,
            refresh_authorships=True,
            include_department=False,
        )

    try:
        _run(force=False)
        with _connect() as conn:
            first = _snapshot(conn, [doi_trusted, doi_gap])
        assert len(first["pubs"]) == 2, "DOI lookups return distinct works; nothing may merge"

        trusted = next(p for p in first["pubs"] if p["doi"] == doi_trusted)
        assert trusted["title"] == f"orcid backed row {slug}", (
            "OpenAlex must not replace metadata backed by a more trusted source"
        )
        assert trusted["venue"] == "Journal of Synthetic Metadata", (
            "OpenAlex may fill fields the trusted source left missing"
        )
        assert {"orcid", "openalex"} <= set(trusted["source_of_truth"].split("+"))
        assert {"orcid", "openalex", "openalex_backfill"} <= set(trusted["raw_keys"])
        trusted_authors = [a for a in first["authorships"] if a["pub_id"] == trusted["pub_id"]]
        assert trusted_authors == [], (
            "authorship refresh must be skipped while a more trusted source is present"
        )

        gap = next(p for p in first["pubs"] if p["doi"] == doi_gap)
        assert gap["title"] == "Synthetic Swarm Metadata Enrichment Paper", (
            "openalex-only rows may be fully enriched"
        )
        assert gap["venue"] == "Journal of Synthetic Metadata"
        gap_authors = [a for a in first["authorships"] if a["pub_id"] == gap["pub_id"]]
        assert [a["author_name"] for a in gap_authors] == ["Carberry, Josiah", "Ada Example"]
        assert gap_authors[0]["author_orcid"] == "0000-0002-1825-0097"

        # Rerun without force: both rows are now enriched (raw_json has the
        # openalex key and no fields are missing), so the selection filter must
        # skip them entirely.
        _run(force=False)
        with _connect() as conn:
            second = _snapshot(conn, [doi_trusted, doi_gap])
        assert second == first, "second identical run must not change anything"

        # Forced rerun re-processes both rows; provenance tokens, fields, and
        # authorships must still be stable (only the fetch timestamp inside
        # raw_json may move, which the snapshot deliberately ignores).
        _run(force=True)
        with _connect() as conn:
            forced = _snapshot(conn, [doi_trusted, doi_gap])
        assert forced == first, "forced rerun must not duplicate or downgrade anything"
    finally:
        _cleanup([doi_trusted, doi_gap], [])


# --------------------------------------------------------------------------- #
# manual_csv_dois: CSV rerun with the same DOI rows
# --------------------------------------------------------------------------- #

class _FakeManualCsvCrossref:
    by_doi: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def lookup_by_doi_with_status(self, doi: str):
        msg = self.by_doi.get((doi or "").lower())
        return (msg, 200 if msg else 404)

    def lookup_by_title(self, title: str, rows: int = 5):
        return []

    def lookup_registration_agency_with_status(self, doi: str):
        return ("crossref", 200)


def test_manual_csv_dois_rerun_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/csv-{slug}"
    dsn = _dsn()

    csv_path = tmp_path / "manual.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Key", "DOI", "Title", "Publication Title", "Publication Year", "Author"])
        writer.writerow(
            [f"fixture-{slug}", doi, "Synthetic Collective Behaviour Paper",
             "Journal of Synthetic Metadata", "2024", "Example, Ada; Example, Bert"]
        )

    _FakeManualCsvCrossref.by_doi = {doi: _crossref_message(doi)}
    monkeypatch.setattr(manual_csv_dois, "CrossrefClient", _FakeManualCsvCrossref)

    def _run() -> None:
        manual_csv_dois.run_manual_csv_doi_scan(
            csv_path=str(csv_path),
            dsn=dsn,
            doi_col="DOI",
            key_col="Key",
            title_col="Title",
            venue_col="Publication Title",
            year_col="Publication Year",
            report_path=str(tmp_path / "report.csv"),
            dry_run=False,
            debug=False,
            limit=None,
            source_of_truth="manual",
        )

    try:
        _run()
        with _connect() as conn:
            first = _snapshot(conn, [doi])
        assert len(first["pubs"]) == 1
        assert "manual" in first["pubs"][0]["source_of_truth"]

        _run()
        with _connect() as conn:
            second = _snapshot(conn, [doi])
        assert second == first, "CSV rerun with identical rows must be a no-op"
    finally:
        _cleanup([doi], [])
