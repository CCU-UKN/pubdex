"""The test network guard with a disposable database configured.

Integration tests keep their database: psycopg, the package's own connection
helper, a plain socket and a child process -- over a socket and through
psycopg, synchronous and asynchronous -- all reach it. Any other server, port
or provider is still refused before a connection is attempted.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from people_pubs.db.connection import db_conn
from tests import network_guard
from tests.network_guard import NetworkAccessRefused

pytestmark = pytest.mark.integration

EXTERNAL = "192.0.2.1"  # TEST-NET-1: documentation only, never routed


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _endpoint() -> tuple:
    (endpoint,) = network_guard.database_endpoints(conninfo_to_dict(_dsn()))
    return endpoint


def test_the_policy_names_the_disposable_database() -> None:
    policy = network_guard.policy()
    assert policy is not None and _endpoint() in policy.endpoints
    assert "only the disposable database" in policy.describe()


def test_the_disposable_database_stays_reachable() -> None:
    with psycopg.connect(_dsn(), connect_timeout=10) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    with db_conn(_dsn()) as conn:
        assert conn.execute("SELECT current_database() AS name").fetchone()["name"] == "people_db"
    host, port = _endpoint()
    if host and not host.startswith("/"):
        socket.create_connection((host, int(port)), timeout=10).close()


def test_any_other_server_is_refused() -> None:
    host, port = _endpoint()
    other_port = 1 if port != "1" else 2
    with network_guard.expect_refusals() as seen:
        with pytest.raises(NetworkAccessRefused, match="only the disposable database"):
            psycopg.connect(_dsn(), port=other_port, connect_timeout=2)
        with pytest.raises(NetworkAccessRefused, match="only the disposable database"):
            psycopg.connect(_dsn(), host=EXTERNAL, hostaddr=EXTERNAL, connect_timeout=2)
        with pytest.raises(NetworkAccessRefused, match="only the disposable database"):
            socket.create_connection((EXTERNAL, int(port)), timeout=2)
        with pytest.raises(NetworkAccessRefused, match="only the disposable database"):
            socket.getaddrinfo("api.crossref.org", 443)
    assert len(seen) == 4


def test_a_child_process_reaches_the_database_and_nothing_else() -> None:
    host, port = _endpoint()
    over_tcp = bool(host) and not host.startswith("/")
    code = "\n".join([
        "import socket",
        f"socket.create_connection(({host!r}, {int(port)}), timeout=10).close()" if over_tcp else "",
        "try:",
        f"    socket.create_connection(({EXTERNAL!r}, 443), timeout=2)",
        "except Exception as exc:",
        "    print(type(exc).__name__)",
    ])
    with network_guard.expect_refusals() as seen:
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "NetworkAccessRefused"
    assert len(seen) == 1 and seen[0].startswith(f"a connection to {EXTERNAL}:443")


# Reads the DSN from the environment it inherits, connects through psycopg
# synchronously and asynchronously, then tries the port in argv[1].
CHILD_THROUGH_PSYCOPG = '''
import asyncio
import os
import sys

import psycopg

dsn = os.environ.get("PEOPLE_PUBS_INTEGRATION_DSN") or os.environ["PEOPLE_DB_TEST_DSN"]
with psycopg.connect(dsn, connect_timeout=10) as conn:
    print(conn.execute("SELECT 1").fetchone()[0])


async def asynchronously():
    async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=10) as conn:
        cursor = await conn.execute("SELECT 2")
        print((await cursor.fetchone())[0])


asyncio.run(asynchronously())
try:
    psycopg.connect(dsn, port=int(sys.argv[1]), connect_timeout=2)
except Exception as exc:
    print(type(exc).__name__)
'''


def test_a_child_process_reaches_the_database_through_psycopg_and_nothing_else() -> None:
    host, port = _endpoint()
    other_port = "1" if port != "1" else "2"
    with network_guard.expect_refusals() as seen:
        proc = subprocess.run([sys.executable, "-c", CHILD_THROUGH_PSYCOPG, other_port],
                              capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["1", "2", "NetworkAccessRefused"]
    assert len(seen) == 1 and seen[0].startswith("a database connection to")
    assert f"{host}:{other_port}" in seen[0]
