"""The read service's HTTP contract, offline.

The application is served in process through httpx's ASGI transport, so no
socket is opened, from a stand-in for the database whose rows and failures
each test chooses. tests/integration/test_api_integration.py serves the same
application from the real database layer on a disposable PostgreSQL.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
import pytest

from people_pubs.api.app import DEFAULT_LIMIT, MAX_LIMIT, MAX_PUB_ID, create_app
from people_pubs.api.database import (
    PUBLICATION_FIELDS,
    DatabaseUnavailable,
    ReadSurfaceUnavailable,
)

LEAK_MARKER = "marker-that-must-not-reach-a-response"


class StandInSource:
    """Rows and failures chosen by the test; records what it was asked."""

    def __init__(self, rows: List[Dict[str, Any]] = (), failure: Optional[BaseException] = None) -> None:
        self.rows = sorted((dict(row) for row in rows), key=lambda row: row["pub_id"])
        self.failure = failure
        self.calls: List[tuple] = []
        self.opened = 0
        self.closed = 0

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed += 1

    def check(self) -> None:
        self.calls.append(("check",))
        if self.failure is not None:
            raise self.failure

    def list_publications(self, *, after: int, limit: int) -> List[Dict[str, Any]]:
        self.calls.append(("list", after, limit))
        if self.failure is not None:
            raise self.failure
        return [row for row in self.rows if row["pub_id"] > after][:limit]


def publication(pub_id: int, **fields: Any) -> Dict[str, Any]:
    row = {
        "pub_id": pub_id,
        "doi": f"10.5555/stand-in-{pub_id}",
        "title": f"Stand-in title {pub_id}",
        "year": 2024,
        "venue": "Journal of Synthetic Tests",
        "source_of_truth": "crossref+orcid",
        "internal_authors": "Ada Example",
    }
    row.update(fields)
    return row


def call(app, method: str, target: str) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://service.test", trust_env=False) as client:
            return await client.request(method, target)

    return asyncio.run(send())


def get(source: StandInSource, target: str) -> httpx.Response:
    return call(create_app(source), "GET", target)


def assert_json(response: httpx.Response, status: int) -> Any:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    return response.json()


def test_liveness_answers_without_the_database() -> None:
    source = StandInSource(failure=DatabaseUnavailable())
    assert assert_json(get(source, "/livez"), 200) == {"status": "alive"}
    assert source.calls == []


def test_readiness_reports_a_usable_read_surface() -> None:
    source = StandInSource()
    assert assert_json(get(source, "/readyz"), 200) == {"status": "ready"}
    assert source.calls == [("check",)]


@pytest.mark.parametrize("failure, reason", [
    (DatabaseUnavailable(), "database_unavailable"),
    (ReadSurfaceUnavailable(), "read_surface_unavailable"),
])
def test_readiness_names_what_is_unavailable(failure: Exception, reason: str) -> None:
    body = assert_json(get(StandInSource(failure=failure), "/readyz"), 503)
    assert body == {"status": "not_ready", "reason": reason}


def test_a_page_holds_exactly_the_export_fields_in_pub_id_order() -> None:
    rows = [
        publication(7, raw_json={"crossref": {"secret": LEAK_MARKER}}, internal_affiliations="Somewhere"),
        publication(3, doi=None, year=None, venue=None, internal_authors=None),
    ]
    source = StandInSource(rows)
    body = assert_json(get(source, "/v0/publications"), 200)
    assert body["limit"] == DEFAULT_LIMIT and body["next_after"] is None
    assert [item["pub_id"] for item in body["items"]] == [3, 7]
    for item in body["items"]:
        assert tuple(item) == PUBLICATION_FIELDS
    assert body["items"][0] == {
        "pub_id": 3, "doi": None, "title": "Stand-in title 3", "year": None, "venue": None,
        "source_of_truth": "crossref+orcid", "internal_authors": None,
    }
    assert LEAK_MARKER not in get(source, "/v0/publications").text
    # One row more than the page, to learn whether another page follows.
    assert source.calls[0] == ("list", 0, DEFAULT_LIMIT + 1)


def test_an_empty_result_is_an_empty_last_page() -> None:
    body = assert_json(get(StandInSource(), "/v0/publications"), 200)
    assert body == {"items": [], "limit": DEFAULT_LIMIT, "next_after": None}


def test_next_after_continues_where_the_page_ended_and_the_last_page_says_so() -> None:
    source = StandInSource([publication(pub_id) for pub_id in (2, 4, 6, 8, 10)])
    first = assert_json(get(source, "/v0/publications?limit=2"), 200)
    assert [item["pub_id"] for item in first["items"]] == [2, 4] and first["next_after"] == 4
    second = assert_json(get(source, "/v0/publications?limit=2&after=4"), 200)
    assert [item["pub_id"] for item in second["items"]] == [6, 8] and second["next_after"] == 8
    last = assert_json(get(source, "/v0/publications?limit=2&after=8"), 200)
    assert [item["pub_id"] for item in last["items"]] == [10] and last["next_after"] is None
    exact = assert_json(get(source, "/v0/publications?limit=5"), 200)
    assert len(exact["items"]) == 5 and exact["next_after"] is None
    assert source.calls == [("list", 0, 3), ("list", 4, 3), ("list", 8, 3), ("list", 0, 6)]


@pytest.mark.parametrize("query, expected", [
    ("limit=1", (0, 1)),
    (f"limit={MAX_LIMIT}", (0, MAX_LIMIT)),
    ("after=0", (0, DEFAULT_LIMIT)),
    ("limit=007&after=0042", (42, 7)),
    (f"after={MAX_PUB_ID}", (MAX_PUB_ID, DEFAULT_LIMIT)),
])
def test_accepted_bounds(query: str, expected: tuple) -> None:
    source = StandInSource()
    assert_json(get(source, f"/v0/publications?{query}"), 200)
    after, limit = expected
    assert source.calls == [("list", after, limit + 1)]


@pytest.mark.parametrize("query, parameter", [
    ("limit=0", "limit"),
    (f"limit={MAX_LIMIT + 1}", "limit"),
    ("limit=-1", "limit"),
    ("limit=%2B5", "limit"),
    ("limit=%205", "limit"),
    ("limit=1.5", "limit"),
    ("limit=1e2", "limit"),
    ("limit=", "limit"),
    ("limit", "limit"),
    ("limit=ten", "limit"),
    ("limit=" + "9" * 40, "limit"),
    ("after=-1", "after"),
    ("after=x", "after"),
    (f"after={MAX_PUB_ID + 1}", "after"),
    ("after=" + "1" * 20, "after"),
    ("limit=5&limit=6", "limit"),
    ("after=1&after=2", "after"),
])
def test_invalid_values_are_refused_before_the_database_is_asked(query: str, parameter: str) -> None:
    source = StandInSource([publication(1)])
    body = assert_json(get(source, f"/v0/publications?{query}"), 400)
    assert body["error"]["code"] == "invalid_parameter"
    assert body["error"]["parameter"] == parameter
    assert source.calls == []


@pytest.mark.parametrize("query", ["page=2", "limit=5&sort=title", "LIMIT=5", "fields=raw_json", f"{LEAK_MARKER}=1"])
def test_unknown_parameters_are_refused_without_echoing_them(query: str) -> None:
    source = StandInSource([publication(1)])
    response = get(source, f"/v0/publications?{query}")
    body = assert_json(response, 400)
    assert body == {"error": {"code": "unknown_parameter", "message": "the only query parameters are limit and after"}}
    assert LEAK_MARKER not in response.text
    assert source.calls == []


@pytest.mark.parametrize("failure, code", [
    (DatabaseUnavailable(), "database_unavailable"),
    (ReadSurfaceUnavailable(), "read_surface_unavailable"),
])
def test_an_unavailable_database_is_a_503_with_a_fixed_message(failure: Exception, code: str) -> None:
    body = assert_json(get(StandInSource(failure=failure), "/v0/publications"), 503)
    assert body["error"]["code"] == code
    assert "try again later" in body["error"]["message"]
    assert set(body["error"]) == {"code", "message"}


@pytest.mark.parametrize("target", ["/v0/publications", "/readyz"])
def test_an_unexpected_failure_is_a_500_that_reveals_nothing(target: str) -> None:
    response = get(StandInSource(failure=RuntimeError(f"connection detail {LEAK_MARKER}")), target)
    body = assert_json(response, 500)
    assert body == {"error": {"code": "internal_error", "message": "internal error"}}
    assert LEAK_MARKER not in response.text


@pytest.mark.parametrize("target", ["/", "/v1/publications", "/v0/publications/1", "/v0/people", "/livez/"])
def test_other_paths_are_404(target: str) -> None:
    body = assert_json(get(StandInSource(), target), 404)
    assert body == {"error": {"code": "not_found", "message": "no such resource"}}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("target", ["/v0/publications", "/readyz", "/livez"])
def test_there_is_no_write_method(method: str, target: str) -> None:
    source = StandInSource()
    response = call(create_app(source), method, target)
    body = assert_json(response, 405)
    assert body["error"]["code"] == "method_not_allowed"
    assert set(response.headers["allow"].replace(" ", "").split(",")) == {"GET", "HEAD"}
    assert source.calls == []


def test_head_is_served_where_get_is() -> None:
    source = StandInSource([publication(1)])
    assert call(create_app(source), "HEAD", "/v0/publications").status_code == 200
    assert call(create_app(source), "HEAD", "/livez").status_code == 200


def test_the_lifespan_opens_the_source_and_closes_it_again() -> None:
    source = StandInSource()
    app = create_app(source)

    async def serve() -> None:
        async with app.router.lifespan_context(app):
            assert (source.opened, source.closed) == (1, 0)
        assert (source.opened, source.closed) == (1, 1)

    asyncio.run(serve())
