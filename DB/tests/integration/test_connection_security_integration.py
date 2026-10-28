"""Real connections of the ingestion commands and the read service on a
disposable PostgreSQL: the server authenticates each one by SCRAM, with the
options people_pubs.connection_security adds, and none negotiates GSSAPI.

The disposable database authenticates connections from outside its container
by scram-sha-256, as the official image's pg_hba.conf does on the Compose
deployment's network. The offline tests (tests/test_connection_security.py)
show that a request for any other method is refused before it is answered.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from people_pubs.api.database import ReadDatabase
from people_pubs.api.settings import load_settings
from people_pubs.db.connection import get_conn

pytestmark = pytest.mark.integration


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _authentication(conn: psycopg.Connection) -> dict:
    """How the server authenticated conn, and the options libpq used for it."""
    options = {option.keyword.decode(): (option.val or b"").decode() for option in conn.pgconn.info}
    return {
        "system_user": conn.execute("SELECT system_user AS who").fetchone()["who"],
        "require_auth": options["require_auth"],
        "gssencmode": options["gssencmode"],
        "used_gssapi": conn.pgconn.used_gssapi,
    }


def test_an_ingestion_connection_is_authenticated_by_scram_without_gssapi() -> None:
    user = conninfo_to_dict(_dsn())["user"]
    with get_conn(_dsn()) as conn:
        assert _authentication(conn) == {"system_user": f"scram-sha-256:{user}", "require_auth": "scram-sha-256",
                                         "gssencmode": "disable", "used_gssapi": False}


def test_a_read_service_session_is_authenticated_by_scram_without_gssapi(tmp_path: Path) -> None:
    target = conninfo_to_dict(_dsn())
    password_file = tmp_path / "password"
    password_file.write_text(target.pop("password") + "\n", encoding="utf-8")
    password_file.chmod(0o600)
    target["options"] = "-c role=app_readonly"
    source = ReadDatabase(load_settings({
        "PEOPLE_PUBS_API_DSN": make_conninfo("", **target),
        "PEOPLE_PUBS_API_DB_PASSWORD_FILE": str(password_file),
    }))
    source.open()
    try:
        with source.pool.connection() as conn:
            assert _authentication(conn) == {"system_user": f"scram-sha-256:{target['user']}",
                                             "require_auth": "scram-sha-256", "gssencmode": "disable",
                                             "used_gssapi": False}
            assert conn.execute("SELECT current_user AS who").fetchone()["who"] == "app_readonly"
    finally:
        source.close()
