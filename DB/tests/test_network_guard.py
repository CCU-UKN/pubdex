"""The test network guard (tests/network_guard.py), offline.

Every attempt below is refused before anything leaves the process -- the
addresses are from the documentation ranges and the names end in .invalid or
belong to real providers, so a guard that failed open would show up as a hang,
a resolver error or a connection error instead of the guard's refusal. The
policy under test is set explicitly, so these tests mean the same thing in an
offline run and in a run that has a database configured.

Database connections from child processes are tested against synthetic
loopback listeners that these tests open themselves and that stand in for a
database server: each counts the connections it receives, so a refusal is
shown to happen before the server is contacted, and an allowed connection is
shown to arrive. No existing database or external service is involved.
"""
from __future__ import annotations

import _socket
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg
import pytest

from people_pubs.db.connection import get_conn
from people_pubs.services.datacite_client import DataCiteClient
from tests import network_guard
from tests.network_guard import NetworkAccessRefused

DB_DIR = Path(__file__).resolve().parents[1]
EXTERNAL = "192.0.2.1"  # TEST-NET-1: documentation only, never routed
DATABASE = ("127.0.0.1", "54321")
OFFLINE: list = []


def refused(action, *args, **kwargs) -> list:
    """Runs action under the offline policy and returns what the guard refused."""
    with network_guard.using(OFFLINE), network_guard.expect_refusals() as seen:
        started = time.monotonic()
        with pytest.raises(NetworkAccessRefused, match="offline tests may not use the network"):
            action(*args, **kwargs)
        assert time.monotonic() - started < 1, "refused at once, not after a connection attempt"
    return seen


def child(code: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """A Python process started with this test process's environment, or env."""
    return subprocess.run([sys.executable, "-c", code, *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_every_test_runs_under_the_guard() -> None:
    policy = network_guard.policy()
    assert policy is not None
    assert os.environ[network_guard.POLICY_VARIABLE] == policy.to_environment()
    assert os.environ[network_guard.LOG_VARIABLE]
    first = os.environ["PYTHONPATH"].split(os.pathsep)[0]
    assert Path(first) == DB_DIR / "tests" / "subprocess_guard", "child processes load the guard first"
    assert psycopg.connect == psycopg.Connection.connect and psycopg.Connection.connect.network_guard


def test_a_connection_to_an_external_address_is_refused() -> None:
    seen = refused(socket.create_connection, (EXTERNAL, 443), timeout=5)
    assert seen and seen[0].startswith(f"a connection to {EXTERNAL}:443")


def test_a_provider_name_is_not_even_looked_up() -> None:
    seen = refused(socket.getaddrinfo, "api.crossref.org", 443)
    assert seen and seen[0].startswith("a host-name lookup of api.crossref.org")
    # A direct connect() would resolve the name first; the wrapper refuses it
    # before that, so the resolver error for this name never comes.
    sock = socket.socket()
    try:
        seen = refused(sock.connect, ("provider.invalid", 443))
    finally:
        sock.close()
    assert seen and seen[0].startswith("a connection to provider.invalid:443")


def test_a_provider_request_through_the_package_client_is_refused() -> None:
    client = DataCiteClient()
    try:
        seen = refused(client.get_doi, "10.5555/network-guard-probe")
    finally:
        client.close()
    assert seen and "api.datacite.org" in seen[0]


def test_loopback_is_refused_offline_and_local_sockets_stay_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Loopback could reach a database or service running on this machine.
    seen = refused(socket.create_connection, ("127.0.0.1", 5432), timeout=5)
    assert seen and seen[0].startswith("a connection to 127.0.0.1:5432")
    left, right = socket.socketpair()
    left.sendall(b"x")
    assert right.recv(1) == b"x"
    for sock in (left, right):
        sock.close()
    monkeypatch.chdir(tmp_path)  # a short relative name stays within the socket path limit
    server = socket.socket(socket.AF_UNIX)
    server.bind("local.sock")
    server.listen(1)
    client = socket.socket(socket.AF_UNIX)
    client.connect("local.sock")
    for sock in (client, server):
        sock.close()


def test_a_datagram_is_refused() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        seen = refused(sock.sendto, b"probe", (EXTERNAL, 53))
    finally:
        sock.close()
    assert seen and seen[0].startswith(f"a datagram to {EXTERNAL}:53")


def test_the_audit_hook_refuses_what_bypasses_the_socket_class() -> None:
    raw = _socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(5)
    try:
        seen = refused(raw.connect, (EXTERNAL, 443))
    finally:
        raw.close()
    assert seen and seen[0].startswith(f"a connection to {EXTERNAL}:443")


@pytest.mark.parametrize(
    "conninfo, shown",
    [
        ("host=127.0.0.1 port=5432 dbname=people_db", "127.0.0.1:5432"),
        ("postgresql://app@example.org/people_db", "example.org:5432"),
        ("dbname=people_db", "the default local socket, port 5432"),
    ],
    ids=["loopback", "remote", "default-socket"],
)
def test_a_database_connection_is_refused_offline(conninfo: str, shown: str) -> None:
    seen = refused(psycopg.connect, conninfo, connect_timeout=5)
    assert seen and seen[0].startswith(f"a database connection to {shown}")
    seen = refused(get_conn, conninfo)
    assert seen and seen[0].startswith(f"a database connection to {shown}")


def test_the_integration_policy_allows_the_disposable_database_alone() -> None:
    with network_guard.using([DATABASE]) as policy:
        network_guard.check_database("host=127.0.0.1 port=54321 dbname=people_db")
        network_guard.check_database("postgresql://postgres@127.0.0.1:54321/postgres")
        network_guard.check_database("dbname=people_db", host="127.0.0.1", port=54321)
        assert policy.allows_address("127.0.0.1", 54321)
        assert policy.allows_lookup("127.0.0.1") and policy.allows_lookup("localhost")
        assert not policy.allows_address("127.0.0.1", 5432)
        assert not policy.allows_address(EXTERNAL, 54321)
        assert not policy.allows_lookup("api.openalex.org")
        with network_guard.expect_refusals() as seen:
            for conninfo in ("host=127.0.0.1 port=5432 dbname=people_db",
                             "host=db.example.org port=54321 dbname=people_db",
                             "service=production"):
                with pytest.raises(NetworkAccessRefused, match="only the disposable database"):
                    network_guard.check_database(conninfo)
        assert len(seen) == 3


def test_libpq_resolves_the_target_as_libpq_would() -> None:
    endpoints = network_guard.database_endpoints
    assert endpoints({"host": "db.example.org", "port": "54321"}, {"PGHOSTADDR": "127.0.0.1"}) == [
        ("127.0.0.1", "54321")
    ], "hostaddr, here from the environment, is where libpq connects"
    assert endpoints({}, {"PGHOST": "127.0.0.1", "PGPORT": "54321"}) == [("127.0.0.1", "54321")]
    assert endpoints({"host": "a.example.org,b.example.org", "port": "1,2"}, {}) == [
        ("a.example.org", "1"), ("b.example.org", "2"),
    ]
    assert endpoints({}, {}) == [("", "5432")]
    assert network_guard.endpoints_from_environment(
        {"PEOPLE_PUBS_INTEGRATION_DSN": "postgresql://postgres@127.0.0.1:54321/people_db"}
    ) == [DATABASE]
    assert network_guard.endpoints_from_environment({}) == []


def test_a_python_child_process_is_guarded_too() -> None:
    with network_guard.expect_refusals() as seen:
        proc = child(f"import socket; socket.create_connection(({EXTERNAL!r}, 443), timeout=5)")
    assert proc.returncode != 0
    assert "NetworkAccessRefused" in proc.stderr and "test network guard refused" in proc.stderr
    assert any(entry.startswith(f"a connection to {EXTERNAL}:443") for entry in seen)


def test_a_refusal_a_child_process_swallows_is_still_recorded() -> None:
    code = "\n".join([
        "import socket",
        "try:",
        "    socket.getaddrinfo('api.orcid.org', 443)",
        "except Exception:",
        "    pass",
    ])
    with network_guard.expect_refusals() as seen:
        proc = child(code)
    assert proc.returncode == 0, "the child went on as if nothing happened"
    assert any(entry.startswith("a host-name lookup of api.orcid.org") for entry in seen)


INNER_TESTS = '''
import socket


def test_swallows_a_refusal():
    try:
        socket.create_connection(("192.0.2.1", 443), timeout=5)
    except Exception:
        pass


def test_stays_offline():
    assert True
'''


def run_inner(root: Path, files: dict) -> subprocess.CompletedProcess:
    """A whole test run in root, with tests/conftest.py loaded as a plugin."""
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(DB_DIR), os.environ["PYTHONPATH"]]))
    for name in network_guard.DATABASE_VARIABLES:
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider",
         "--rootdir", str(root), "-q", str(root)],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )


def test_a_test_that_swallows_a_refusal_fails(tmp_path: Path) -> None:
    proc = run_inner(tmp_path, {"test_inner.py": INNER_TESTS})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "1 failed, 1 passed" in proc.stdout
    assert "test_swallows_a_refusal" in proc.stdout
    assert "refused network access during this test" in proc.stdout
    assert f"(a connection to {EXTERNAL}:443" in proc.stdout


# --------------------------------------------------------------------------
# psycopg in child processes
# --------------------------------------------------------------------------
class Listener:
    """A loopback TCP listener standing in for a database server: it accepts
    every connection, counts it and closes it at once."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _address = self.sock.accept()
            except OSError:
                continue
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.sock.close()


@pytest.fixture
def listeners():
    opened: list = []

    def open_listener() -> Listener:
        opened.append(Listener())
        return opened[-1]

    yield open_listener
    for listener in opened:
        listener.close()


# Connects through each public psycopg entry point to the port in argv[1] and
# prints, per attempt, the exception's class name or "connected".
CONNECT_THREE_WAYS = '''
import asyncio
import sys

import psycopg

target = dict(host="127.0.0.1", port=int(sys.argv[1]), dbname="people_db", user="probe",
              connect_timeout=5, sslmode="disable", gssencmode="disable")


def attempt(connect):
    try:
        connect()
    except Exception as exc:
        print(type(exc).__name__)
    else:
        print("connected")


attempt(lambda: psycopg.connect(**target))
attempt(lambda: psycopg.Connection.connect(**target))
attempt(lambda: asyncio.run(psycopg.AsyncConnection.connect(**target)))
'''


def test_a_child_cannot_reach_a_database_server(listeners) -> None:
    server = listeners()
    with network_guard.expect_refusals() as seen:
        proc = child(CONNECT_THREE_WAYS, str(server.port))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["NetworkAccessRefused"] * 3, "sync, class method and async alike"
    assert len(seen) == 3
    assert all(entry.startswith(f"a database connection to 127.0.0.1:{server.port}") for entry in seen)
    assert server.accepted == 0, "refused before the server was contacted"


def test_a_child_with_a_database_policy_reaches_that_server_alone(listeners) -> None:
    allowed, other = listeners(), listeners()
    env = dict(os.environ, **{network_guard.POLICY_VARIABLE: f"127.0.0.1\t{allowed.port}"})
    with network_guard.expect_refusals() as seen:
        reached = child(CONNECT_THREE_WAYS, str(allowed.port), env=env)
        refused_here = child(CONNECT_THREE_WAYS, str(other.port), env=env)
    # The stand-in server closes every connection, so libpq reports that,
    # after the guard has let the connection through.
    assert reached.stdout.split() == ["OperationalError"] * 3, reached.stderr
    assert allowed.accepted == 3
    assert refused_here.stdout.split() == ["NetworkAccessRefused"] * 3, refused_here.stderr
    assert other.accepted == 0
    assert len(seen) == 3 and all(f"127.0.0.1:{other.port}" in entry for entry in seen)


def test_children_load_psycopg_only_when_they_use_it() -> None:
    proc = child(
        "import sys; print('psycopg' in sys.modules); import psycopg; "
        "print(bool(psycopg.connect.network_guard), bool(psycopg.AsyncConnection.connect.network_guard))"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["False", "True", "True"]


def test_a_child_without_psycopg_starts_and_imports_as_usual() -> None:
    with network_guard.expect_refusals() as seen:
        proc = child(
            "import sys; sys.path[:] = [p for p in sys.path if 'site-packages' not in p]; import psycopg"
        )
    assert proc.returncode == 1 and "ModuleNotFoundError: No module named 'psycopg'" in proc.stderr
    assert seen == [], "an interpreter without psycopg is no guard failure"


GRANDCHILD = '''
import psycopg

try:
    psycopg.connect(host="127.0.0.1", port=1, dbname="people_db", connect_timeout=2)
except Exception:
    pass
'''

STARTS_A_GRANDCHILD = '''
import subprocess
import sys
from pathlib import Path


def test_starts_a_child_that_swallows_a_database_refusal():
    child = Path(__file__).with_name("grandchild.py")
    assert subprocess.run([sys.executable, str(child)]).returncode == 0


def test_stays_offline():
    assert True
'''


def test_a_database_refusal_a_child_swallows_fails_the_enclosing_run(tmp_path: Path) -> None:
    proc = run_inner(tmp_path, {"test_inner.py": STARTS_A_GRANDCHILD, "grandchild.py": GRANDCHILD})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "1 failed, 1 passed" in proc.stdout
    assert "test_starts_a_child_that_swallows_a_database_refusal" in proc.stdout
    assert "(a database connection to 127.0.0.1:1" in proc.stdout


IMPORTS_PSYCOPG_IN_A_RUN = '''
import psycopg
import pytest

from tests import network_guard


def test_psycopg_is_guarded_once_per_copy_of_the_guard():
    chain, func = [], psycopg.Connection.__dict__["connect"].__func__
    while func is not None:
        chain.append(getattr(func, "network_guard", None))
        func = getattr(func, "__wrapped__", None)
    assert chain == ["tests.network_guard", "_test_network_guard", None]
    with network_guard.expect_refusals() as seen:
        with pytest.raises(network_guard.NetworkAccessRefused):
            psycopg.connect(host="127.0.0.1", port=1, dbname="people_db", connect_timeout=2)
    assert len(seen) == 1
'''


def test_a_run_started_by_a_test_guards_psycopg_through_both_copies(tmp_path: Path) -> None:
    # The run's own conftest installs a second copy of the guard next to the
    # one its environment installed at start-up; importing psycopg there must
    # neither recurse between them nor leave either copy's check out.
    proc = run_inner(tmp_path, {"test_inner.py": IMPORTS_PSYCOPG_IN_A_RUN})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout


STUB_PSYCOPG = '''
class Connection:
    pass


class AsyncConnection:
    pass
'''

IMPORTS_PSYCOPG_QUIETLY = '''
try:
    import psycopg
except Exception as exc:
    print(type(exc).__name__)
'''


def test_a_guard_that_cannot_be_set_up_stops_the_child() -> None:
    with network_guard.expect_refusals() as seen:
        malformed = child("print('ran')", env=dict(os.environ, **{network_guard.POLICY_VARIABLE: "no-tab-here"}))
    assert malformed.returncode == network_guard.GUARD_FAILURE_STATUS and malformed.stdout == ""
    assert "could not be installed" in malformed.stderr
    assert len(seen) == 1 and "could not be installed" in seen[0], "recorded for the enclosing test"

    without_log = {k: v for k, v in os.environ.items() if k != network_guard.LOG_VARIABLE}
    proc = child("print('ran')", env=without_log)
    assert proc.returncode == network_guard.GUARD_FAILURE_STATUS and proc.stdout == ""
    assert f"{network_guard.LOG_VARIABLE} is not" in proc.stderr


def test_a_psycopg_the_guard_cannot_wrap_fails_to_import_and_is_recorded(tmp_path: Path) -> None:
    (tmp_path / "psycopg").mkdir()
    (tmp_path / "psycopg" / "__init__.py").write_text(STUB_PSYCOPG, encoding="utf-8")
    env = dict(os.environ)
    guard_dir, rest = env["PYTHONPATH"].split(os.pathsep, 1)
    env["PYTHONPATH"] = os.pathsep.join([guard_dir, str(tmp_path), rest])
    with network_guard.expect_refusals() as seen:
        proc = child(IMPORTS_PSYCOPG_QUIETLY, env=env)
    assert proc.returncode == 0 and proc.stdout.split() == ["RuntimeError"], proc.stderr
    assert len(seen) == 1 and "cannot find psycopg.Connection.connect" in seen[0], (
        "a swallowed failure still reaches the enclosing test"
    )
