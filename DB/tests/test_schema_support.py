"""The application's schema decisions and its connection boundary, offline."""
from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from people_pubs import schema
from people_pubs.api.database import ReadDatabase, SchemaUnavailable
from people_pubs.api.settings import Settings
from people_pubs.db import connection
from tests.test_api_app import StandInSource, get
from tests.test_schema_runner import _change, _recorded


@pytest.mark.parametrize("required", [1, 2, 3])
@pytest.mark.parametrize("applied", [None, 0, 1, 2, 3, 4])
@pytest.mark.parametrize("compatible", [False, True])
def test_schema_version_combinations(required, applied, compatible) -> None:
    changes = [_change(n) for n in range(1, required + 1)]
    recorded = None if applied is None else [_recorded(_change(n)) for n in range(1, applied + 1)]
    if applied and applied > required:
        recorded[-1] = replace(recorded[-1], backward_compatible=compatible)
    accepted = applied is not None and applied >= required and (applied == required or compatible)
    if accepted:
        assert not schema.require_supported(changes, recorded).outstanding
    else:
        with pytest.raises(schema.Refused) as refused:
            schema.require_supported(changes, recorded)
        expected = (schema.SCHEMA_UPGRADE_REQUIRED if applied is None or 0 < applied < required
                    else schema.SCHEMA_NOT_SUPPORTED)
        assert refused.value.reason == expected
        assert f"schema version {'unrecorded' if applied is None else f'{applied:04d}'}" in str(refused.value)
        assert f"supports {required:04d}" in str(refused.value)
        assert "python -m people_pubs.schema upgrade" in str(refused.value)


@pytest.mark.parametrize("rows, detail", [
    ([_recorded(_change(1)), _recorded(_change(3))], "gap"),
    ([replace(_recorded(_change(1)), name="0001_other")], "another change"),
    ([replace(_recorded(_change(1)), sha256="a" * 64)], "file was changed"),
    ([_recorded(_change(1)), _recorded(_change(2, False)), _recorded(_change(3))], "not backward compatible"),
])
def test_a_record_that_cannot_be_supported_is_refused(rows, detail) -> None:
    with pytest.raises(schema.Refused, match=detail) as refused:
        schema.require_supported([_change(1)], rows)
    assert refused.value.reason == schema.SCHEMA_NOT_SUPPORTED


@pytest.mark.parametrize("reason", [schema.SCHEMA_UPGRADE_REQUIRED, schema.SCHEMA_NOT_SUPPORTED])
def test_both_http_endpoints_explain_the_schema_and_liveness_is_independent(reason) -> None:
    message = "database schema version 0000; this application supports 0001. Run python -m people_pubs.schema upgrade."
    source = StandInSource(failure=SchemaUnavailable(schema.Refused(message, reason)))
    assert get(source, "/livez").json() == {"status": "alive"}
    assert source.calls == []
    assert get(source, "/readyz").json() == {"status": "not_ready", "reason": reason, "message": message}
    response = get(source, "/v0/publications")
    assert response.status_code == 503
    assert response.json() == {"error": {"code": reason, "message": message}}


def test_each_read_checks_the_schema_before_reading_in_the_same_session(monkeypatch) -> None:
    source = ReadDatabase(Settings(dsn="host=db.example.org dbname=people_db user=app_readonly", user="app_readonly"))
    events = []

    class Conn:
        def execute(self, *args):
            events.append("read")
            return self

        def fetchall(self):
            return []

    conn = Conn()

    @contextmanager
    def session():
        yield conn

    def check(actual):
        assert actual is conn
        events.append("check")
        if events.count("check") == 3:
            raise schema.Refused("schema changed")

    monkeypatch.setattr(source.pool, "connection", session)
    monkeypatch.setattr(schema, "check_supported", check)
    source.check()
    source.list_publications(after=0, limit=1)
    with pytest.raises(SchemaUnavailable, match="schema changed"):
        source.list_publications(after=0, limit=1)
    assert events == ["check", "read", "check", "read", "check"]


@pytest.mark.parametrize("failure", [None, schema.Refused("schema refused"), RuntimeError("query failed")])
def test_ingestion_checks_before_return_and_closes_a_refused_connection(monkeypatch, capsys, failure) -> None:
    events = []

    class Conn:
        @contextmanager
        def transaction(self):
            events.append("transaction")
            yield
            events.append("finished")

        def close(self):
            events.append("closed")

    conn = Conn()
    monkeypatch.setattr(connection.psycopg, "connect", lambda *a, **kw: conn)

    def check(actual):
        assert actual is conn
        events.append("check")
        if failure:
            raise failure

    monkeypatch.setattr(schema, "check_supported", check)
    if failure is None:
        assert connection.get_conn() is conn
        assert events == ["transaction", "check", "finished"]
    elif isinstance(failure, schema.Refused):
        with pytest.raises(SystemExit) as refused:
            connection.get_conn()
        assert refused.value.code == connection.SCHEMA_REFUSED
        assert capsys.readouterr().err == "people_pubs: schema refused\n"
        assert events == ["transaction", "check", "closed"]
    else:
        with pytest.raises(RuntimeError, match="query failed"):
            connection.get_conn()
        assert events == ["transaction", "check", "closed"]


def test_database_entry_points_stay_at_the_checked_boundaries() -> None:
    root = Path(__file__).resolve().parents[1] / "people_pubs"
    sites = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update((item.asname or item.name, item.name) for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                aliases.update((item.asname or item.name, f"{node.module}.{item.name}") for item in node.names)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            first, *rest = name.split(".")
            name = ".".join([aliases.get(first, first), *rest])
            if name.startswith(("psycopg.", "psycopg_pool.")) and name.split(".")[-1] in {
                "connect", "ConnectionPool", "AsyncConnectionPool", "NullConnectionPool", "AsyncNullConnectionPool"
            }:
                sites.append((path.relative_to(root).as_posix(), name))
    assert sorted(sites) == sorted([
        ("db/connection.py", "psycopg.connect"), ("db/connection.py", "psycopg.connect"),
        ("api/database.py", "psycopg_pool.ConnectionPool"), ("schema.py", "psycopg.connect"),
    ])
