"""
people_pubs.api.app

The HTTP side of the read service: routes, request validation and the
response format. create_app() takes the data source as an argument, so the
offline tests serve the same application from a stand-in.

A page of publications:

  {"items": [{"pub_id": 1, "doi": "10.5555/...", "title": "...",
              "year": 2024, "venue": "...", "source_of_truth": "crossref+orcid",
              "internal_authors": "..."}],
   "limit": 25, "next_after": null}

Items come in ascending pub_id order and carry exactly the fields of the CSV
export. limit (1 to 100, default 25) bounds the page, and after (a pub_id,
default 0) starts it after that id; next_after is the value of after for the
next page, or null when this page is the last. Any other query parameter, or
one given twice, is refused.

An error:

  {"error": {"code": "invalid_parameter", "message": "...", "parameter": "limit"}}

Each code has a fixed message: nothing in a response is taken from an
exception or from the database's error text.
"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Protocol, Tuple

import anyio.to_thread
from starlette.applications import Starlette
from starlette.datastructures import QueryParams
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from people_pubs.api.database import PUBLICATION_FIELDS, ReadUnavailable, SchemaUnavailable

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
#: The largest pub_id PostgreSQL's bigint can hold.
MAX_PUB_ID = 2**63 - 1
QUERY_PARAMETERS = ("limit", "after")

UNAVAILABLE_MESSAGES = {
    "database_unavailable": "the database is not available; try again later",
    "read_surface_unavailable": "the publication data is not available; try again later",
}

_DIGITS = re.compile(r"[0-9]{1,19}")
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


class ReadSource(Protocol):
    """What the application reads from; people_pubs.api.database.ReadDatabase."""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def check(self) -> None: ...

    def list_publications(self, *, after: int, limit: int) -> List[Dict[str, Any]]: ...


class InvalidQuery(ValueError):
    """A query string the publications endpoint does not accept."""

    def __init__(self, code: str, message: str, parameter: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.parameter = parameter


def respond(status: int, payload: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=_HEADERS)


def error_response(
    status: int,
    code: str,
    message: str,
    parameter: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> JSONResponse:
    error: Dict[str, Any] = {"code": code, "message": message}
    if parameter is not None:
        error["parameter"] = parameter
    response = respond(status, {"error": error})
    if headers:
        response.headers.update(headers)
    return response


def parse_page(query: QueryParams) -> Tuple[int, int]:
    """(after, limit) from the query string; InvalidQuery if it is unusable."""
    names = [name for name, _ in query.multi_items()]
    if any(name not in QUERY_PARAMETERS for name in names):
        raise InvalidQuery("unknown_parameter", "the only query parameters are limit and after")
    for name in QUERY_PARAMETERS:
        if names.count(name) > 1:
            raise InvalidQuery("invalid_parameter", f"{name} may be given only once", name)
    limit = _whole_number(query.get("limit"), "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    after = _whole_number(query.get("after"), "after", 0, 0, MAX_PUB_ID)
    return after, limit


def _whole_number(raw: Optional[str], name: str, default: int, low: int, high: int) -> int:
    if raw is None:
        return default
    if not _DIGITS.fullmatch(raw) or not low <= int(raw) <= high:
        raise InvalidQuery("invalid_parameter", f"{name} must be a whole number from {low} to {high}", name)
    return int(raw)


async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    if exc.status_code == 404:
        return error_response(404, "not_found", "no such resource")
    if exc.status_code == 405:
        return error_response(405, "method_not_allowed", "only GET and HEAD are served", headers=exc.headers)
    return error_response(exc.status_code, "http_error", "the request cannot be served")


async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    # Starlette re-raises the exception after this response, so the server
    # logs it; the client learns nothing from it.
    return error_response(500, "internal_error", "internal error")


def create_app(source: ReadSource) -> Starlette:
    """The read service's application, reading from source.

    The lifespan opens source when the server starts and closes it when the
    server stops. The endpoints are synchronous, so Starlette runs them in its
    thread pool, where they may wait for the database without holding up the
    event loop.
    """

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        source.open()
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(source.close)

    def livez(request: Request) -> JSONResponse:
        return respond(200, {"status": "alive"})

    def readyz(request: Request) -> JSONResponse:
        try:
            source.check()
        except ReadUnavailable as exc:
            payload = {"status": "not_ready", "reason": exc.reason}
            if isinstance(exc, SchemaUnavailable):
                payload["message"] = str(exc)
            return respond(503, payload)
        return respond(200, {"status": "ready"})

    def publications(request: Request) -> JSONResponse:
        try:
            after, limit = parse_page(request.query_params)
        except InvalidQuery as exc:
            return error_response(400, exc.code, exc.message, exc.parameter)
        try:
            # One row more than the page holds tells whether another page exists.
            rows = source.list_publications(after=after, limit=limit + 1)
        except ReadUnavailable as exc:
            message = str(exc) if isinstance(exc, SchemaUnavailable) else UNAVAILABLE_MESSAGES[exc.reason]
            return error_response(503, exc.reason, message)
        items = [{field: row[field] for field in PUBLICATION_FIELDS} for row in rows[:limit]]
        next_after = items[-1]["pub_id"] if len(rows) > limit else None
        return respond(200, {"items": items, "limit": limit, "next_after": next_after})

    app = Starlette(
        routes=[
            Route("/livez", livez, methods=["GET"]),
            Route("/readyz", readyz, methods=["GET"]),
            Route("/v0/publications", publications, methods=["GET"]),
        ],
        exception_handlers={HTTPException: _http_error, Exception: _internal_error},
        lifespan=lifespan,
    )
    # Exact paths only: "/livez/" is not found rather than redirected.
    app.router.redirect_slashes = False
    return app
