"""The authentication every database connection requires
(people_pubs.connection_security), offline.

The options are checked as the ingestion commands and the read service pass
them, and then against the real libpq of the installed psycopg: a stand-in
for a PostgreSQL server, on a Unix-domain socket in a temporary directory,
reads a client's startup message, answers with one authentication request and
records what the client sends back. A request for a method the connection
does not allow has to end the connection with libpq's own refusal, before
the client answers it: no password, no GSSAPI token. The network guard lets
these connections reach that socket and nothing else; no database server is
involved. The integration tests show that real connections succeed by SCRAM.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from unittest.mock import MagicMock

import psycopg
import pytest

from people_pubs import connection_security
from people_pubs.api.database import connection_kwargs
from people_pubs.api.settings import Settings
from people_pubs.db import connection
from tests import network_guard

PORT = 5432
#: The password the clients send, where they send one.
MARKER = "marker-value-5e7a"


def authentication_request(code: int, payload: bytes = b"") -> bytes:
    """An AuthenticationRequest message of the given kind."""
    return b"R" + struct.pack("!ii", 8 + len(payload), code) + payload


AUTHENTICATION_OK = authentication_request(0)
SCRAM_REQUEST = authentication_request(10, b"SCRAM-SHA-256\x00\x00")
MD5_REQUEST = authentication_request(5, b"salt")
# What a server sends after AuthenticationOk until the connection is ready.
READY = b"".join(
    b"S" + struct.pack("!i", 4 + len(body)) + body
    for body in (b"server_version\x0016.15\x00", b"client_encoding\x00UTF8\x00",
                 b"standard_conforming_strings\x00on\x00", b"integer_datetimes\x00on\x00",
                 b"DateStyle\x00ISO, MDY\x00", b"TimeZone\x00UTC\x00")
) + b"K" + struct.pack("!iii", 12, 4242, 4242) + b"Z" + struct.pack("!i", 5) + b"I"
# Requests for a method other than SCRAM, and libpq's description of each.
OTHER_METHODS = {
    "gssapi": (authentication_request(7), "server requested GSSAPI authentication"),
    "sspi": (authentication_request(9), "server requested SSPI authentication"),
    "cleartext": (authentication_request(3), "server requested a cleartext password"),
    "md5": (MD5_REQUEST, "server requested a hashed password"),
}


def _receive(conn: socket.socket, size: int) -> Optional[bytes]:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


class StandInServer:
    """Answers each connection's startup message with `answer` and records
    the startup parameters and the messages the client sends back: the first
    one only, unless `ready`, when the connection is completed and the client
    may send until it closes."""

    def __init__(self, directory: Path, answer: bytes, ready: bool = False) -> None:
        self.directory, self.answer, self.ready = directory, answer, ready
        self.sessions: List[Dict] = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(directory / f".s.PGSQL.{PORT}"))
        self._listener.listen(4)
        self._listener.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(5)
                session: Dict = {"startup": {}, "replies": []}
                try:
                    self._session(conn, session)
                finally:
                    self.sessions.append(session)

    def _session(self, conn: socket.socket, session: Dict) -> None:
        while True:
            header = _receive(conn, 8)
            if header is None:
                return
            length, code = struct.unpack("!ii", header)
            body = _receive(conn, length - 8) or b""
            if code in (80877103, 80877104):  # SSLRequest, GSSENCRequest: neither is offered
                conn.sendall(b"N")
                continue
            fields = body.split(b"\x00")
            session["startup"] = {k.decode(): v.decode() for k, v in zip(fields[::2], fields[1::2]) if k}
            break
        conn.sendall(self.answer + (READY if self.ready else b""))
        while True:
            kind = _receive(conn, 1)
            if kind is None:
                return
            length = struct.unpack("!i", _receive(conn, 4) or b"\x00\x00\x00\x04")[0]
            session["replies"].append((kind, _receive(conn, length - 4) or b""))
            if not self.ready:
                return

    def wait_for(self, count: int) -> List[Dict]:
        deadline = time.monotonic() + 10
        while len(self.sessions) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.sessions

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._listener.close()


@pytest.fixture
def stand_in(tmp_path: Path):
    """Starts a stand-in server and lets this process's psycopg reach it, and
    nothing else, for the length of the test."""
    started: List[StandInServer] = []

    def start(answer: bytes, ready: bool = False) -> StandInServer:
        assert not started, "one stand-in per test"
        started.append(StandInServer(tmp_path, answer, ready))
        return started[0]

    with network_guard.using([(str(tmp_path), str(PORT))]):
        yield start
    for server in started:
        server.close()


def _dsn(directory: Path, *extra: str) -> str:
    return " ".join((f"host={directory} port={PORT} dbname=people_db user=app_writer connect_timeout=5", *extra))


def _ingestion(directory: Path, monkeypatch, *extra: str) -> Callable[[], psycopg.Connection]:
    """A connection as an ingestion command opens it, with the password from
    the environment."""
    monkeypatch.setenv("PGPASSWORD", MARKER)
    return lambda: connection.get_conn(_dsn(directory, *extra))


def _service(directory: Path, password: Optional[str] = MARKER, *extra: str) -> Callable[[], psycopg.Connection]:
    """A connection as the read service's pool opens it."""
    settings = Settings(dsn=_dsn(directory, *extra).replace("app_writer", "app_readonly"), user="app_readonly",
                        password=password)
    return lambda: psycopg.connect(settings.dsn, **connection_kwargs(settings))


def _clean_libpq_environment(monkeypatch) -> None:
    for name in ("PGREQUIREAUTH", "PGGSSENCMODE", "PGPASSWORD", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PGPASSFILE", "/dev/null")


# --- the options -------------------------------------------------------------

def test_scram_and_no_gssapi_encryption_unless_the_configuration_sets_them() -> None:
    assert connection_security.defaults({}, {}) == {"require_auth": "scram-sha-256", "gssencmode": "disable"}
    assert connection_security.defaults({}, {}, connection_security.NO_AUTHENTICATION) == {
        "require_auth": "none", "gssencmode": "disable"}
    # A connection string's choice is kept, and so is the environment's.
    assert connection_security.defaults({"require_auth": "md5"}, {}) == {"gssencmode": "disable"}
    assert connection_security.defaults({"gssencmode": "prefer"}, {}) == {"require_auth": "scram-sha-256"}
    assert connection_security.defaults({}, {"PGREQUIREAUTH": "none", "PGGSSENCMODE": "prefer"}) == {}
    # An empty value counts as unset.
    assert connection_security.defaults({"require_auth": ""}, {"PGGSSENCMODE": " "}) == {
        "require_auth": "scram-sha-256", "gssencmode": "disable"}


def test_ingestion_connections_get_the_options_unless_configured_otherwise(monkeypatch) -> None:
    calls: list = []
    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return MagicMock()

    monkeypatch.setattr(connection.psycopg, "connect", connect)
    monkeypatch.setattr(connection.schema, "check_supported", lambda conn: None)
    _clean_libpq_environment(monkeypatch)
    dsn = "host=db dbname=people_db user=app_writer"
    connection.get_conn(dsn)
    connection.get_conn(dsn + " require_auth=md5")
    connection.get_conn("postgresql://app_writer@db/people_db?gssencmode=prefer")
    monkeypatch.setenv("PGREQUIREAUTH", "none")
    connection.get_conn(dsn)
    monkeypatch.delenv("PGREQUIREAUTH")
    # Without a connection string, libpq's PG* variables configure the rest.
    monkeypatch.setattr(connection, "PEOPLE_DB_DSN", None)
    connection.get_conn()
    options = [{key: value for key, value in kwargs.items() if key != "row_factory"} for _, kwargs in calls]
    assert [args for args, _ in calls] == [(dsn,), (dsn + " require_auth=md5",),
                                           ("postgresql://app_writer@db/people_db?gssencmode=prefer",), (dsn,), ()]
    assert options == [
        {"require_auth": "scram-sha-256", "gssencmode": "disable"},
        {"gssencmode": "disable"},
        {"require_auth": "scram-sha-256"},
        {"gssencmode": "disable"},
        {"require_auth": "scram-sha-256", "gssencmode": "disable"},
    ]


def test_the_read_service_requires_scram_with_a_password_and_no_authentication_without(monkeypatch) -> None:
    dsn = "host=db dbname=people_db user=app_readonly"
    with_password = connection_kwargs(Settings(dsn=dsn, user="app_readonly", password=MARKER))
    without = connection_kwargs(Settings(dsn=dsn, user="app_readonly"))
    assert (with_password["require_auth"], with_password["gssencmode"]) == ("scram-sha-256", "disable")
    assert (without["require_auth"], without["gssencmode"]) == ("none", "disable")
    # Only the connection string can choose otherwise: the service removes
    # libpq's PG* variables before it connects, and they are not consulted.
    monkeypatch.setenv("PGREQUIREAUTH", "md5")
    assert connection_kwargs(Settings(dsn=dsn, user="app_readonly", password=MARKER))["require_auth"] == "scram-sha-256"
    chosen = connection_kwargs(Settings(dsn=dsn + " require_auth=md5 gssencmode=prefer", user="app_readonly",
                                        password=MARKER))
    assert "require_auth" not in chosen and "gssencmode" not in chosen


# --- libpq against a stand-in server ----------------------------------------

@pytest.mark.parametrize("method", list(OTHER_METHODS))
@pytest.mark.parametrize("client", ["ingestion", "read service"])
def test_a_request_for_another_method_is_refused_before_it_is_answered(
    stand_in, monkeypatch, tmp_path: Path, client: str, method: str,
) -> None:
    _clean_libpq_environment(monkeypatch)
    request, reason = OTHER_METHODS[method]
    server = stand_in(request)
    connect = _ingestion(tmp_path, monkeypatch) if client == "ingestion" else _service(tmp_path)
    with pytest.raises(psycopg.OperationalError) as refused:
        connect()
    assert f'authentication method requirement "scram-sha-256" failed: {reason}' in str(refused.value)
    sessions = server.wait_for(1)
    assert len(sessions) == 1 and sessions[0]["startup"]["user"] in ("app_writer", "app_readonly")
    assert sessions[0]["replies"] == [], "nothing was sent in answer: no password, no GSSAPI token"
    assert MARKER not in repr(sessions)


@pytest.mark.parametrize("client", ["ingestion", "read service"])
def test_a_server_that_skips_authentication_is_refused_when_a_password_is_configured(
    stand_in, monkeypatch, tmp_path: Path, client: str,
) -> None:
    _clean_libpq_environment(monkeypatch)
    server = stand_in(AUTHENTICATION_OK, ready=True)
    connect = _ingestion(tmp_path, monkeypatch) if client == "ingestion" else _service(tmp_path)
    with pytest.raises(psycopg.OperationalError) as refused:
        connect()
    assert 'authentication method requirement "scram-sha-256" failed: server did not complete authentication' \
        in str(refused.value)
    assert len(server.wait_for(1)) == 1


@pytest.mark.parametrize("client", ["ingestion", "read service"])
def test_a_scram_request_is_answered_with_scram(stand_in, monkeypatch, tmp_path: Path, client: str) -> None:
    _clean_libpq_environment(monkeypatch)
    server = stand_in(SCRAM_REQUEST)
    connect = _ingestion(tmp_path, monkeypatch) if client == "ingestion" else _service(tmp_path)
    # The stand-in cannot complete the exchange and closes the connection.
    with pytest.raises(psycopg.OperationalError) as failed:
        connect()
    assert "authentication method requirement" not in str(failed.value)
    (kind, body), = server.wait_for(1)[0]["replies"]
    assert kind == b"p" and body.startswith(b"SCRAM-SHA-256\x00"), "a SASLInitialResponse for SCRAM-SHA-256"
    assert MARKER.encode() not in body, "SCRAM never sends the password"


def test_the_read_service_without_a_password_accepts_only_a_server_that_asks_for_none(
    stand_in, monkeypatch, tmp_path: Path,
) -> None:
    _clean_libpq_environment(monkeypatch)
    server = stand_in(AUTHENTICATION_OK, ready=True)
    with _service(tmp_path, password=None)() as conn:
        assert conn.info.parameter_status("server_version") == "16.15"
    session, = server.wait_for(1)
    assert session["startup"]["user"] == "app_readonly"
    assert session["replies"] == [(b"X", b"")], "only the Terminate message of a normal close"


@pytest.mark.parametrize("method", ["scram", "gssapi", "md5"])
def test_the_read_service_without_a_password_refuses_any_authentication_request(
    stand_in, monkeypatch, tmp_path: Path, method: str,
) -> None:
    _clean_libpq_environment(monkeypatch)
    request, reason = (SCRAM_REQUEST, "server requested SASL authentication") if method == "scram" \
        else OTHER_METHODS[method]
    server = stand_in(request)
    with pytest.raises(psycopg.OperationalError) as refused:
        _service(tmp_path, password=None)()
    assert f'authentication method requirement "none" failed: {reason}' in str(refused.value)
    assert server.wait_for(1)[0]["replies"] == []


@pytest.mark.parametrize("client", ["ingestion connection string", "ingestion environment", "read service"])
def test_a_configured_method_is_used_as_configured(stand_in, monkeypatch, tmp_path: Path, client: str) -> None:
    # An external server that still asks for an MD5 password stays usable
    # when the configuration says so.
    _clean_libpq_environment(monkeypatch)
    server = stand_in(MD5_REQUEST)
    if client == "ingestion connection string":
        connect = _ingestion(tmp_path, monkeypatch, "require_auth=md5")
    elif client == "ingestion environment":
        monkeypatch.setenv("PGREQUIREAUTH", "md5")
        connect = _ingestion(tmp_path, monkeypatch)
    else:
        connect = _service(tmp_path, MARKER, "require_auth=md5")
    with pytest.raises(psycopg.OperationalError) as failed:
        connect()
    assert "authentication method requirement" not in str(failed.value)
    (kind, body), = server.wait_for(1)[0]["replies"]
    assert kind == b"p" and body.startswith(b"md5"), "the MD5 password response"
