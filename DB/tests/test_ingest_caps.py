"""Offline proof that the Crossref discovery searcher cannot ingest thousands of records.

Each test feeds a synthetic API that *offers* far more records than the cap and
asserts the real search loop (a) stops at --max-records and (b) drops records
that fail the affiliation post-filter. The HTTP boundary is monkeypatched; no
network, no DB. `max_records` is honored even with no_db=True, so these run in
the plain unit suite; the `max_new_records` write cap needs a DB and lives in
tests/integration/.

See DB/INGEST_SEARCH_SEMANTICS.md for why the post-filter is load-bearing
(Crossref query.affiliation is an "any word" match: a short institute name
alone can return over a million records).
"""
from __future__ import annotations

import httpx

from people_pubs.sync import crossref_search


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""
        self.headers: dict = {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)  # type: ignore[arg-type]


def _crossref_item(i: int, affiliation: str | None = None) -> dict:
    author = {"given": "Ada", "family": f"Lovelace{i}"}
    if affiliation is not None:
        author["affiliation"] = [{"name": affiliation}]
    return {"DOI": f"10.1234/x{i}", "title": [f"Title {i}"], "author": [author]}


def _crossref_fake_get(items: list[dict], per_page: int = 100):
    pages = [items[i : i + per_page] for i in range(0, len(items), per_page)] or [[]]

    def fake_get(self, url, params=None, **kwargs):  # noqa: ANN001
        cursor = (params or {}).get("cursor", "*")
        idx = 0 if cursor == "*" else int(cursor)
        page = pages[idx] if idx < len(pages) else []
        message = {"items": page, "total-results": len(items)}
        if idx + 1 < len(pages) and page:
            message["next-cursor"] = str(idx + 1)
        return _FakeResp({"message": message})

    return fake_get


def _run_crossref(monkeypatch, items, **over):
    monkeypatch.setattr(httpx.Client, "get", _crossref_fake_get(items))
    kwargs = dict(
        dsn=None, awards=[], affiliations=[], funders=[], author_orcids=[],
        only_person_ids=None, from_year=None, to_year=None, count=100,
        max_records=None, max_new_records=None, min_interval=0.0, no_db=True,
        dry_run=True, debug=False, debug_json=False, source_token="crossref",
        rewrite_sot=None, skip_existing=False, refresh_authorships=False,
        commit_every=0, award_mode="filter", require_affiliation_match=False,
    )
    kwargs.update(over)
    return crossref_search.run_crossref_search(**kwargs)


# --------------------------------------------------------------------------- #
# Cap tests: --max-records stops the loop well below what the API offers
# --------------------------------------------------------------------------- #
def test_crossref_max_records_caps_processing(monkeypatch) -> None:
    stats = _run_crossref(monkeypatch, [_crossref_item(i) for i in range(5000)],
                          affiliations=["Example University"], max_records=50)
    assert stats.processed == 50


# --------------------------------------------------------------------------- #
# Post-filter tests: --require-affiliation-match drops non-matching records
# --------------------------------------------------------------------------- #
def test_crossref_post_filter_drops_nonmatching(monkeypatch) -> None:
    items = [_crossref_item(i, "ICB, Example University") for i in range(5)]
    items += [_crossref_item(100 + i, "Unrelated Department, Elsewhere") for i in range(5)]
    stats = _run_crossref(monkeypatch, items, affiliations=["ICB"],
                          require_affiliation_match=True, max_records=100)
    assert stats.processed == 10
    assert stats.skipped_affiliation_mismatch == 5
