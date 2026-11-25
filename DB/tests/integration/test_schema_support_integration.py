"""The read service and ingestion on older, matching and newer schemas."""
from __future__ import annotations

import os
import subprocess
import sys

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from people_pubs import schema
from people_pubs.api.app import create_app
from people_pubs.api.database import ReadDatabase
from people_pubs.api.settings import Settings
from people_pubs.db.connection import SCHEMA_REFUSED, get_conn
from tests.integration.test_api_integration import get
from tests.integration.test_schema_runner_integration import (
    DB_DIR, _changes, _dsn_of, _record, _run, databases, template,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("state, accepted", [("behind", False), ("level", True),
                                         ("ahead-compatible", True), ("ahead-incompatible", False)])
def test_read_service_and_ingestion_on_each_schema(databases, template, state, accepted, tmp_path) -> None:
    name = databases.create(template=template)
    dsn = _dsn_of(name)
    if state != "behind":
        assert _run("upgrade", dsn)[0] == 0
    if state.startswith("ahead"):
        compatible = state == "ahead-compatible"
        body = ("CREATE TABLE app.schema_support_probe (value text);" if compatible else
                "ALTER MATERIALIZED VIEW biblio.publications_canon_web RENAME COLUMN title TO support_probe_title;")
        changes = _changes(tmp_path, {"0002_support_probe.sql":
            f"-- Changes a support probe.\n-- backward-compatible: {'yes' if compatible else 'no'}\n{body}\n"})
        assert _run("upgrade", dsn, changes)[0] == 0
    params = conninfo_to_dict(dsn)
    password = params.pop("password")
    source = ReadDatabase(Settings(dsn=make_conninfo("", **params, options="-c role=app_readonly"),
                                   user="app_readonly", password=password))
    source.open()
    try:
        app = create_app(source)
        assert get(app, "/livez").status_code == 200
        ready = get(app, "/readyz")
        page = get(app, "/v0/publications")
        assert ready.status_code == page.status_code == (200 if accepted else 503)
        if not accepted:
            reason = schema.SCHEMA_UPGRADE_REQUIRED if state == "behind" else schema.SCHEMA_NOT_SUPPORTED
            assert ready.json()["reason"] == page.json()["error"]["code"] == reason
            assert ready.json()["message"] == page.json()["error"]["message"]
            assert "python -m people_pubs.schema upgrade" in ready.json()["message"]
            assert "schema version" in ready.json()["message"]
        # A schema upgrade and an incompatible later change are visible on
        # the very same pool, without a service restart.
        if state == "behind":
            assert _run("upgrade", dsn)[0] == 0
            assert get(app, "/readyz").status_code == 200
            with psycopg.connect(dsn) as conn:
                conn.execute("DROP SCHEMA pubdex CASCADE")
            assert get(app, "/readyz").json()["reason"] == schema.SCHEMA_UPGRADE_REQUIRED
    finally:
        source.close()

    env = {key: value for key, value in os.environ.items() if not key.startswith("PG") and key not in {
        "PEOPLE_DB_DSN", "PEOPLE_DB_PASSWORD_FILE"}}
    env.update(PEOPLE_DB_DSN=make_conninfo(dsn, options="-c role=app_writer"), PEOPLE_PUBS_SKIP_DOTENV="1")
    roster = DB_DIR / "tests/fixtures/golden/demo_roster_minimal.csv"
    command = [sys.executable, "-m", "people_pubs.sync.add_people", "--csv", str(roster), "--no-orcid-lookup"]
    with psycopg.connect(dsn) as conn:
        before = conn.execute("SELECT count(*) FROM app.people").fetchone()[0]
    result = subprocess.run(command, cwd=DB_DIR, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == (0 if accepted else SCHEMA_REFUSED), result.stderr
    with psycopg.connect(dsn) as conn:
        after = conn.execute("SELECT count(*) FROM app.people").fetchone()[0]
    if accepted:
        assert after > before
        with get_conn(dsn) as conn:
            assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            assert conn.row_factory is dict_row
    else:
        assert after == before
        assert ready.json()["message"] in result.stderr
        assert "Traceback" not in result.stderr


def test_a_recorded_database_one_change_behind_is_refused(databases, template, monkeypatch, tmp_path) -> None:
    dsn = _dsn_of(databases.create(template=template))
    assert _run("upgrade", dsn)[0] == 0
    changes = _changes(tmp_path, {"0002_support_probe.sql":
        "-- Adds a support probe.\n-- backward-compatible: yes\nSELECT 1;\n"})
    with psycopg.connect(dsn) as conn:
        with pytest.raises(schema.Refused, match="missing schema changes: 0002_support_probe") as exc:
            schema.check_supported(conn, changes)
    assert exc.value.reason == schema.SCHEMA_UPGRADE_REQUIRED
    monkeypatch.setattr(schema, "SCHEMA_DIR", tmp_path / "schema")
    params = conninfo_to_dict(dsn)
    password = params.pop("password")
    source = ReadDatabase(Settings(dsn=make_conninfo("", **params, options="-c role=app_readonly"),
                                   user="app_readonly", password=password))
    source.open()
    try:
        app = create_app(source)
        ready = get(app, "/readyz")
        assert ready.status_code == get(app, "/v0/publications").status_code == 503
        assert "schema version 0001" in ready.json()["message"]
        assert "supports 0002" in ready.json()["message"]
        assert get(app, "/livez").status_code == 200
    finally:
        source.close()
    # Give the ingestion entry point the same test-only schema directory.
    # The module and all its database access are the production ones.
    program = ("import runpy, sys; from pathlib import Path; from people_pubs import schema; "
               "schema.SCHEMA_DIR = Path(sys.argv.pop(1)); "
               "runpy.run_module('people_pubs.sync.add_people', run_name='__main__')")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG") and key not in {
        "PEOPLE_DB_DSN", "PEOPLE_DB_PASSWORD_FILE"}}
    env.update(PEOPLE_DB_DSN=make_conninfo(dsn, options="-c role=app_writer"), PEOPLE_PUBS_SKIP_DOTENV="1")
    command = [sys.executable, "-c", program, str(tmp_path / "schema"), "--csv",
               str(DB_DIR / "tests/fixtures/golden/demo_roster_minimal.csv"), "--no-orcid-lookup"]
    result = subprocess.run(command, cwd=DB_DIR, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == SCHEMA_REFUSED
    assert ready.json()["message"] in result.stderr
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM app.people").fetchone()[0] == 0
    assert len(_record(dsn)) == 1
