"""Disposable-DB importer reruns for datacite_backfill, dblp_backfill and
semantic_scholar_backfill (TESTING_STRATEGY priority scenarios).

Each importer runs through its real database-writing path -- selection,
in-place update, provenance merge, authorship refresh, alias writes and
commits -- against a disposable PostgreSQL. Only provider access is replaced:
httpx.Client.get answers from the golden fixtures, so the package's own
clients still build their requests and parse the responses, and the test
network guard refuses anything that would leave the machine.

Every test seeds two publications, one backed by Crossref (a source the trust
order ranks above all three providers) and one ORCID-only row with no year or
venue, plus an internal person carrying the fixture author's ORCID.

Two kinds of test, kept apart on purpose:

- Regression tests, one per importer, pin what holds whichever way the open
  precedence question below is decided: the first import fills the fields the
  row lacked, keeps a value the provider does not supply and never rewrites
  an existing DOI; source_of_truth stays cumulative, the other sources' raw
  payloads and envelope hashes are kept, and the authorships and name aliases
  are written; a forced rerun with identical input changes no identity, count
  or stored value -- only fetch timestamps may move -- and an unforced rerun
  selects nothing and sends no request.
- Characterization tests, marked `characterization`, pin the current field
  precedence without approving it: a title or year the provider supplies
  replaces a stored value even when a source that
  people_pubs.config.TRUST_ORDER_PUBLICATIONS ranks higher supplied it --
  Crossref on the first row, and for DBLP also ORCID on the second. Only
  openalex_backfill is documented to defer to more trusted sources; whether
  these importers should as well is an open decision (DB/TESTING_STRATEGY.md,
  "Characterization of the enrichment precedence"). If it is decided the
  other way, these tests change with the importers, deliberately.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.db.publications import payload_sha256, upsert_publication
from people_pubs.services.datacite_client import DATACITE_BASE
from people_pubs.services.semantic_scholar_client import SEMANTIC_SCHOLAR_BASE
from people_pubs.sync import datacite_backfill, dblp_backfill, semantic_scholar_backfill
from people_pubs.sync.dblp_backfill import DBLP_BASE

pytestmark = pytest.mark.integration

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
FIXTURE_ORCID = "0000-0002-1825-0097"  # ORCID's fictional researcher, used by the fixtures


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _connect() -> psycopg.Connection:
    return psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True)


def _golden(name: str) -> dict:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


class Providers:
    """Answers httpx.Client.get from fixture payloads keyed by DOI, and
    records every request."""

    def __init__(self) -> None:
        self.datacite: dict = {}
        self.semantic: dict = {}
        self.dblp: dict = {}
        self.requests: list = []

    def respond(self, url, params=None) -> httpx.Response:
        url = str(url)
        self.requests.append(url)
        request = httpx.Request("GET", url, params=params)
        if url.startswith(f"{DATACITE_BASE}/dois/"):
            entry = self.datacite.get(url.rsplit("/dois/", 1)[1].lower())
            body = {"data": entry} if entry else {"errors": [{"status": "404"}]}
            return httpx.Response(200 if entry else 404, json=body, request=request)
        if url.startswith(f"{SEMANTIC_SCHOLAR_BASE}/paper/DOI:"):
            paper = self.semantic.get(url.rsplit("/paper/DOI:", 1)[1].lower())
            return httpx.Response(200 if paper else 404, json=paper or {"error": "not found"}, request=request)
        if url == f"{DBLP_BASE}/search/publ/api":
            query = str((params or {}).get("q", ""))
            info = self.dblp.get(query.removeprefix("doi:").lower())
            hits = [{"@id": "1", "info": info}] if info else []
            return httpx.Response(200, json={"result": {"hits": {"hit": hits}}}, request=request)
        raise AssertionError(f"request to an unexpected provider URL: {url}")


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> Providers:
    fake = Providers()

    def get(client: httpx.Client, url, *, params=None, **kwargs) -> httpx.Response:
        return fake.respond(url, params)

    monkeypatch.setattr(httpx.Client, "get", get)
    return fake


@pytest.fixture
def seeded():
    """A Crossref-backed and an ORCID-only publication, plus an internal
    person with the fixture author's ORCID; removed again afterwards."""
    slug = uuid4().hex[:12]
    trusted_doi = f"10.5555/trusted-{slug}"
    gap_doi = f"10.5555/gap-{slug}"
    crossref_payload = {"DOI": trusted_doi, "title": [f"Crossref title {slug}"]}
    orcid_payload = {"put-code": 7, "title": {"title": {"value": f"ORCID title {slug}"}}}
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.people (display_name, person_kind) VALUES (%s, 'internal') RETURNING person_id",
                (f"Josiah Carberry {slug}",),
            )
            person_id = int(cur.fetchone()["person_id"])
            cur.execute("INSERT INTO app.identities_orcid (person_id, orcid) VALUES (%s, %s)",
                        (person_id, FIXTURE_ORCID))
        trusted = upsert_publication(conn, {
            "doi": trusted_doi, "title": f"Crossref title {slug}", "year": 2023,
            "venue": "Crossref Journal of Record", "raw_crossref_json": crossref_payload,
        })
        gap = upsert_publication(conn, {
            "doi": gap_doi, "title": f"ORCID title {slug}", "raw_orcid_json": orcid_payload,
        })
    rows = {
        "slug": slug, "person_id": person_id, "trusted": trusted, "gap": gap,
        "trusted_doi": trusted_doi, "gap_doi": gap_doi,
        "crossref_payload": crossref_payload, "orcid_payload": orcid_payload,
    }
    try:
        yield rows
    finally:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM biblio.authorships WHERE pub_id = ANY(%s)", ([trusted, gap],))
            cur.execute("DELETE FROM biblio.publications WHERE pub_id = ANY(%s)", ([trusted, gap],))
            cur.execute("DELETE FROM app.people WHERE person_id = %s", (person_id,))


def _snapshot(seeded: dict, run_key: str) -> dict:
    """Everything the importers write for the seeded rows, without the fetch
    timestamps a rerun is allowed to move."""
    pub_ids = [seeded["trusted"], seeded["gap"]]
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT pub_id, doi, title, year, venue, is_preprint, source_of_truth, raw_json, source_provenance
            FROM biblio.publications WHERE pub_id = ANY(%s) ORDER BY pub_id
            """,
            (pub_ids,),
        )
        pubs = []
        for row in cur.fetchall():
            raw = row.pop("raw_json") or {}
            assert set(raw.get(run_key) or {}) <= {"at"}, "the run record holds a timestamp only"
            row["raw_json"] = {key: value for key, value in raw.items() if key != run_key}
            row["raw_keys"] = sorted(raw)
            row["envelopes"] = {
                key: (entry.get("transform_version"), entry.get("payload_sha256"))
                for key, entry in (row.pop("source_provenance") or {}).items()
                if key != run_key
            }
            pubs.append(row)
        cur.execute(
            """
            SELECT pub_id, author_position, author_name, person_id, author_orcid, paper_orcid,
                   is_internal_at_ingest, affiliations
            FROM biblio.authorships WHERE pub_id = ANY(%s) ORDER BY pub_id, author_position
            """,
            (pub_ids,),
        )
        authorships = cur.fetchall()
        cur.execute(
            """
            SELECT alias_name, alias_sources FROM app.person_name_aliases_manual
            WHERE person_id = %s ORDER BY lower(alias_name)
            """,
            (seeded["person_id"],),
        )
        aliases = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM biblio.publications WHERE doi = ANY(%s)",
                    ([seeded["trusted_doi"], seeded["gap_doi"]],))
        count = cur.fetchone()["n"]
    return {"pubs": pubs, "authorships": authorships, "aliases": aliases, "publication_count": count}


def _assert_preserved_and_cumulative(first: dict, seeded: dict, token: str, raw_key: str) -> tuple:
    trusted, gap = (next(p for p in first["pubs"] if p["pub_id"] == seeded[name]) for name in ("trusted", "gap"))
    assert first["publication_count"] == 2, "nothing merged, nothing duplicated"
    assert (trusted["doi"], gap["doi"]) == (seeded["trusted_doi"], seeded["gap_doi"]), "a stored DOI is kept"
    assert set(trusted["source_of_truth"].split("+")) == {"crossref", token}
    assert set(gap["source_of_truth"].split("+")) == {"orcid", token}
    # The other sources' payloads are kept as they were, next to the new one.
    assert trusted["raw_json"]["crossref"] == seeded["crossref_payload"]
    assert gap["raw_json"]["orcid"] == seeded["orcid_payload"]
    assert {"crossref", raw_key, f"{raw_key}_backfill"} <= set(trusted["raw_keys"])
    assert {"orcid", raw_key, f"{raw_key}_backfill"} <= set(gap["raw_keys"])
    # Each payload has an envelope whose hash still matches it.
    assert trusted["envelopes"]["crossref"][1] == payload_sha256(seeded["crossref_payload"])
    assert gap["envelopes"]["orcid"][1] == payload_sha256(seeded["orcid_payload"])
    assert trusted["envelopes"][raw_key][1] == payload_sha256(trusted["raw_json"][raw_key])
    return trusted, gap


def _rerun(run, seeded: dict, providers: Providers, first: dict, run_key: str) -> None:
    run(force=True)
    forced = _snapshot(seeded, run_key)
    assert forced == first, "a forced rerun with identical input changes nothing"
    before = len(providers.requests)
    run(force=False)
    assert len(providers.requests) == before, "an unforced rerun selects nothing to enrich"
    assert _snapshot(seeded, run_key) == first




def _row(snapshot: dict, pub_id: int) -> dict:
    return next(pub for pub in snapshot["pubs"] if pub["pub_id"] == pub_id)


# --------------------------------------------------------------------------- #
# datacite_backfill
# --------------------------------------------------------------------------- #
DATACITE_TITLE = "Synthetic Collective Behaviour Dataset"


def _datacite_entry(doi: str, *, with_venue: bool = True) -> dict:
    entry = _golden("datacite_work_minimal.json")
    entry["id"] = doi
    entry["attributes"]["doi"] = doi.upper()
    if not with_venue:
        entry["attributes"].pop("container")
        entry["attributes"].pop("publisher")
    return entry


def _datacite(seeded: dict, providers: Providers):
    """Fixture answers for both rows -- the Crossref-backed one without a
    venue -- and the importer as these tests run it."""
    providers.datacite = {
        seeded["trusted_doi"]: _datacite_entry(seeded["trusted_doi"], with_venue=False),
        seeded["gap_doi"]: _datacite_entry(seeded["gap_doi"]),
    }

    def run(force: bool) -> None:
        stats = datacite_backfill.run_datacite_backfill(
            _dsn(), token=None, max_n=10, only_pub_id=f"{seeded['trusted']},{seeded['gap']}",
            merge_mode="doi", sleep_s=0.0, dry_run=False, force=force, source_token="datacite",
            rewrite_sot=None, refresh_authorships=True, include_department=False, search_count=5,
            debug_json=False, debug_json_max_chars=4000,
        )
        assert stats.errors == 0

    return run


def test_datacite_backfill_first_import_and_rerun(seeded: dict, providers: Providers) -> None:
    run = _datacite(seeded, providers)
    run(force=False)
    first = _snapshot(seeded, "datacite_backfill")
    trusted, gap = _assert_preserved_and_cumulative(first, seeded, "datacite", "datacite")
    # The ORCID-only row gets what DataCite supplies (DataCite ranks above
    # ORCID); what DataCite does not supply stays as it was.
    assert (gap["title"], gap["year"], gap["venue"]) == (DATACITE_TITLE, 2024, "Journal of Synthetic Data Reports")
    assert trusted["venue"] == "Crossref Journal of Record"
    assert trusted["raw_json"]["datacite"] == providers.datacite[seeded["trusted_doi"]]

    # Both fixture creators carry an affiliation, so both become authorships;
    # the ORCID links the first to the internal person, the second stays unlinked.
    for pub_id in (seeded["trusted"], seeded["gap"]):
        rows = [a for a in first["authorships"] if a["pub_id"] == pub_id]
        assert [(a["author_position"], a["author_name"]) for a in rows] == [
            (1, "Carberry, Josiah"), (2, "Ada Example"),
        ]
        assert (rows[0]["person_id"], rows[0]["author_orcid"], rows[0]["paper_orcid"]) == (
            seeded["person_id"], FIXTURE_ORCID, FIXTURE_ORCID,
        )
        assert rows[0]["is_internal_at_ingest"] is True
        assert rows[1]["person_id"] is None and rows[1]["is_internal_at_ingest"] is False
    assert first["aliases"] and all(a["alias_sources"] == ["datacite.author_name"] for a in first["aliases"])
    assert {a["alias_name"] for a in first["aliases"]} >= {"Carberry, Josiah"}

    _rerun(run, seeded, providers, first, "datacite_backfill")


@pytest.mark.characterization
def test_datacite_backfill_replaces_values_crossref_supplied(seeded: dict, providers: Providers) -> None:
    """Current behaviour, not a policy decision: DataCite's title and year
    replace the ones stored from Crossref, which the trust order ranks above
    DataCite."""
    _datacite(seeded, providers)(force=False)
    trusted = _row(_snapshot(seeded, "datacite_backfill"), seeded["trusted"])
    assert "crossref" in trusted["source_of_truth"].split("+")
    assert (trusted["title"], trusted["year"]) == (DATACITE_TITLE, 2024)


# --------------------------------------------------------------------------- #
# semantic_scholar_backfill
# --------------------------------------------------------------------------- #
SEMANTIC_TITLE = "Synthetic Collective Behaviour Paper"


def _semantic_paper(doi: str, *, with_venue: bool = True) -> dict:
    paper = _golden("semantic_scholar_paper_minimal.json")
    paper["externalIds"]["DOI"] = doi.upper()
    if not with_venue:
        paper.pop("venue")
        paper.pop("publicationVenue")
    return paper


def _semantic(seeded: dict, providers: Providers):
    providers.semantic = {
        seeded["trusted_doi"]: _semantic_paper(seeded["trusted_doi"], with_venue=False),
        seeded["gap_doi"]: _semantic_paper(seeded["gap_doi"]),
    }

    def run(force: bool) -> None:
        stats = semantic_scholar_backfill.run_semantic_backfill(
            _dsn(), api_key=None, max_n=10, only_pub_id=f"{seeded['trusted']},{seeded['gap']}",
            merge_mode="doi", sleep_s=0.0, dry_run=False, force=force, source_token="semantic",
            rewrite_sot=None, refresh_authorships=True, include_department=False, search_count=5,
        )
        assert stats.errors == 0

    return run


def test_semantic_scholar_backfill_first_import_and_rerun(seeded: dict, providers: Providers) -> None:
    run = _semantic(seeded, providers)
    run(force=False)
    first = _snapshot(seeded, "semantic_backfill")
    trusted, gap = _assert_preserved_and_cumulative(first, seeded, "semantic", "semantic")
    # Semantic Scholar ranks above ORCID.
    assert (gap["title"], gap["year"], gap["venue"]) == (SEMANTIC_TITLE, 2024, "J. Synthetic Metadata")
    assert trusted["venue"] == "Crossref Journal of Record"

    # Only the first fixture author has an affiliation, and only authors with
    # one are written.
    for pub_id in (seeded["trusted"], seeded["gap"]):
        rows = [a for a in first["authorships"] if a["pub_id"] == pub_id]
        assert [(a["author_position"], a["author_name"], a["person_id"]) for a in rows] == [
            (1, "Josiah Carberry", seeded["person_id"]),
        ]
        assert rows[0]["is_internal_at_ingest"] is True and rows[0]["affiliations"]
    assert first["aliases"] and all(a["alias_sources"] == ["semantic.author_name"] for a in first["aliases"])

    _rerun(run, seeded, providers, first, "semantic_backfill")


@pytest.mark.characterization
def test_semantic_scholar_backfill_replaces_values_crossref_supplied(seeded: dict, providers: Providers) -> None:
    """Current behaviour, not a policy decision: Semantic Scholar's title and
    year replace the ones stored from Crossref, which the trust order ranks
    above Semantic Scholar."""
    _semantic(seeded, providers)(force=False)
    trusted = _row(_snapshot(seeded, "semantic_backfill"), seeded["trusted"])
    assert "crossref" in trusted["source_of_truth"].split("+")
    assert (trusted["title"], trusted["year"]) == (SEMANTIC_TITLE, 2024)


# --------------------------------------------------------------------------- #
# dblp_backfill
# --------------------------------------------------------------------------- #
DBLP_TITLE = "Synthetic Collective Behaviour Paper"  # the trailing period removed


def _dblp_info(doi: str, *, with_venue: bool = True) -> dict:
    info = copy.deepcopy(_golden("dblp_hit_minimal.json")["info"])
    info["doi"] = doi.upper()
    info["ee"] = f"https://doi.org/{doi.upper()}"
    if not with_venue:
        info.pop("venue")
    return info


def _dblp(seeded: dict, providers: Providers):
    providers.dblp = {
        seeded["trusted_doi"]: _dblp_info(seeded["trusted_doi"], with_venue=False),
        seeded["gap_doi"]: _dblp_info(seeded["gap_doi"]),
    }

    def run(force: bool) -> None:
        dblp_backfill.run_dblp_backfill(
            dsn=_dsn(), max_n=10, only_pub_ids=[seeded["trusted"], seeded["gap"]], merge_mode="doi",
            sleep_s=0.0, dry_run=False, force=force, source_token="dblp", rewrite_sot=None,
            refresh_authorships=True, include_department=False, title_fallback=False,
        )

    return run


def test_dblp_backfill_first_import_and_rerun(seeded: dict, providers: Providers) -> None:
    run = _dblp(seeded, providers)
    run(force=False)
    first = _snapshot(seeded, "dblp_backfill")
    trusted, gap = _assert_preserved_and_cumulative(first, seeded, "dblp", "dblp")
    # The year and venue the ORCID-only row lacked are filled; a venue DBLP
    # does not supply stays. Which title wins is characterised below.
    assert (gap["year"], gap["venue"]) == (2024, "Journal of Synthetic Metadata")
    assert trusted["venue"] == "Crossref Journal of Record"
    # DBLP author records carry no affiliation, so no authorship and no alias
    # is written even with the refresh requested.
    assert first["authorships"] == [] and first["aliases"] == []

    _rerun(run, seeded, providers, first, "dblp_backfill")


@pytest.mark.characterization
def test_dblp_backfill_replaces_values_crossref_and_orcid_supplied(seeded: dict, providers: Providers) -> None:
    """Current behaviour, not a policy decision: DBLP, ranked below both
    Crossref and ORCID, replaces the title and year stored from Crossref and
    the title stored from ORCID."""
    _dblp(seeded, providers)(force=False)
    snapshot = _snapshot(seeded, "dblp_backfill")
    trusted, gap = _row(snapshot, seeded["trusted"]), _row(snapshot, seeded["gap"])
    assert (trusted["title"], trusted["year"]) == (DBLP_TITLE, 2024)
    assert gap["title"] == DBLP_TITLE
