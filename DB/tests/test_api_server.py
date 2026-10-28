"""The read service's database layer, logging and process, offline.

No database is reached: the queries are inspected as text, the pool's
configuration as data, and failures are injected into its connection
method. The server-process tests start the real `python -m people_pubs.api`
on a Unix-domain socket in a temporary directory -- the network guard lets
local sockets through and still refuses any network or database connection
from the child -- and only ask it for liveness, which needs no database.
"""
from __future__ import annotations

import io
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import psycopg
import pytest
from psycopg import errors
from psycopg_pool import PoolClosed, PoolTimeout

from people_pubs.api import database, server
from people_pubs.api.app import create_app
from people_pubs.api.database import (
    APPLICATION_NAME,
    CHECK_READ_SURFACE,
    LIST_PUBLICATIONS,
    PUBLICATION_FIELDS,
    DatabaseUnavailable,
    ReadDatabase,
    ReadSurfaceUnavailable,
    connection_kwargs,
    unavailable_error,
)
from people_pubs.api.settings import Settings
from people_pubs.sync.export_publications import EXPORT_COLUMNS

DB_DIR = Path(__file__).resolve().parents[1]
DSN = "host=db.example.org dbname=people_db user=app_readonly"
MARKER = "marker-value-81c3"


# --- the queries -----------------------------------------------------------

def test_the_service_returns_exactly_the_export_columns() -> None:
    assert PUBLICATION_FIELDS == EXPORT_COLUMNS


@pytest.mark.parametrize("query", [LIST_PUBLICATIONS, CHECK_READ_SURFACE])
def test_each_query_is_one_select_of_the_named_columns_from_the_snapshot(query) -> None:
    text = query.as_string(None)
    columns = ", ".join(f'"{name}"' for name in PUBLICATION_FIELDS)
    assert text.startswith(f'SELECT {columns} FROM "biblio"."publications_canon_web"')
    assert "*" not in text and ";" not in text
    words = {word.upper() for word in text.replace(",", " ").split()}
    assert not words & {"INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "ALTER", "DROP", "TRUNCATE",
                        "GRANT", "REFRESH", "CALL", "DO", "COPY", "LOCK"}


def test_the_page_query_takes_its_bounds_as_parameters() -> None:
    text = LIST_PUBLICATIONS.as_string(None)
    assert text.endswith("WHERE pub_id > %(after)s ORDER BY pub_id LIMIT %(limit)s")
    assert CHECK_READ_SURFACE.as_string(None).endswith(" LIMIT 0")


# --- failures ---------------------------------------------------------------

@pytest.mark.parametrize("exc, expected", [
    (PoolTimeout("couldn't get a connection after 5.00 sec"), DatabaseUnavailable),
    (PoolClosed("the pool is closed"), DatabaseUnavailable),
    (psycopg.OperationalError("connection failed"), DatabaseUnavailable),
    (errors.QueryCanceled("canceling statement due to statement timeout"), DatabaseUnavailable),
    (errors.AdminShutdown("terminating connection due to administrator command"), DatabaseUnavailable),
    (psycopg.InterfaceError("the connection is closed"), DatabaseUnavailable),
    (errors.UndefinedTable("relation does not exist"), ReadSurfaceUnavailable),
    (errors.UndefinedColumn("column does not exist"), ReadSurfaceUnavailable),
    (errors.InvalidSchemaName("schema does not exist"), ReadSurfaceUnavailable),
    (errors.InsufficientPrivilege("permission denied"), ReadSurfaceUnavailable),
    (errors.ObjectNotInPrerequisiteState("materialized view has not been populated"), ReadSurfaceUnavailable),
    (errors.SyntaxError("syntax error"), None),
    (errors.ReadOnlySqlTransaction("cannot execute INSERT in a read-only transaction"), None),
    (ValueError("anything else"), None),
])
def test_failures_are_sorted_into_the_two_reasons(exc: BaseException, expected) -> None:
    assert unavailable_error(exc) is expected


def _settings(**fields) -> Settings:
    return Settings(**{"dsn": DSN, "user": "app_readonly", **fields})


@pytest.mark.parametrize("failure, expected", [
    (PoolTimeout(f"couldn't get a connection: {MARKER}"), DatabaseUnavailable),
    (errors.UndefinedTable(f'relation "{MARKER}" does not exist'), ReadSurfaceUnavailable),
])
def test_a_translated_failure_carries_no_database_text(failure, expected, monkeypatch, caplog) -> None:
    source = ReadDatabase(_settings())

    @contextmanager
    def failing_connection(timeout=None):
        raise failure
        yield  # pragma: no cover

    monkeypatch.setattr(source.pool, "connection", failing_connection)
    with caplog.at_level(logging.WARNING, logger="people_pubs.api.database"):
        with pytest.raises(expected) as raised:
            source.check()
    assert str(raised.value) == "" and raised.value.__suppress_context__
    assert MARKER not in caplog.text
    assert f"{expected.reason}: {type(failure).__name__}" in caplog.text


def test_an_unexpected_failure_passes_through_unchanged(monkeypatch) -> None:
    source = ReadDatabase(_settings())

    @contextmanager
    def failing_connection(timeout=None):
        raise errors.SyntaxError("syntax error")
        yield  # pragma: no cover

    monkeypatch.setattr(source.pool, "connection", failing_connection)
    with pytest.raises(errors.SyntaxError):
        source.list_publications(after=0, limit=1)


# --- the pool ---------------------------------------------------------------

def test_every_session_is_read_only_with_a_statement_timeout_that_wins() -> None:
    kwargs = connection_kwargs(_settings(
        dsn=DSN + " options='-c default_transaction_read_only=off -c statement_timeout=0'",
        statement_timeout_ms=1500,
    ))
    options = kwargs["options"].split(" -c ")
    # A later -c wins, so the service's settings come last.
    assert options[-2:] == ["default_transaction_read_only=on", "statement_timeout=1500"]
    assert kwargs["options"].startswith("-c default_transaction_read_only=off")
    assert kwargs["autocommit"] is True


def test_the_connection_settings_are_complete_and_keep_the_password_separate() -> None:
    kwargs = connection_kwargs(_settings(password=MARKER, db_wait_seconds=0.5))
    assert kwargs["password"] == MARKER
    assert kwargs["connect_timeout"] == 2, "libpq waits at least two seconds"
    assert kwargs["application_name"] == APPLICATION_NAME
    assert kwargs["options"] == "-c default_transaction_read_only=on -c statement_timeout=5000"
    assert "password" not in connection_kwargs(_settings())
    assert connection_kwargs(_settings(db_wait_seconds=7.2))["connect_timeout"] == 8
    named = connection_kwargs(_settings(dsn=DSN + " application_name=reader"))
    assert "application_name" not in named, "a name the connection string sets is kept"


def test_the_pool_starts_empty_bounded_and_without_the_password_in_its_connection_string() -> None:
    source = ReadDatabase(_settings(password=MARKER, pool_size=3, db_wait_seconds=1.5))
    pool = source.pool
    assert (pool.min_size, pool.max_size, pool.timeout) == (0, 3, 1.5)
    assert pool.name == APPLICATION_NAME
    assert pool.conninfo == DSN and MARKER not in pool.conninfo
    assert pool.kwargs == connection_kwargs(_settings(password=MARKER, pool_size=3, db_wait_seconds=1.5))
    assert pool.reconnect_timeout == database.RECONNECT_TIMEOUT_SECONDS
    assert pool.closed, "the pool opens with the service, not before"


# --- logging, environment and server configuration -------------------------

@pytest.fixture
def root_logging() -> Iterator[None]:
    """Restores the logging configuration a test changes."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    pool_level = logging.getLogger("psycopg.pool").level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("psycopg.pool").setLevel(pool_level)


def test_the_password_is_masked_in_messages_arguments_and_tracebacks(root_logging) -> None:
    stream = io.StringIO()
    server.configure_logging("INFO", [MARKER], stream=stream)
    logging.getLogger("psycopg.pool").warning("error connecting: password %s rejected", MARKER)
    logging.getLogger("people_pubs.api").info(f"literal {MARKER} in the message")
    try:
        raise RuntimeError(f"failure mentioning {MARKER}")
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    text = stream.getvalue()
    assert MARKER not in text
    assert text.count(server.MASK) == 3
    assert "Traceback" in text and "RuntimeError" in text


def test_routine_pool_messages_are_not_logged(root_logging) -> None:
    stream = io.StringIO()
    server.configure_logging("INFO", [], stream=stream)
    logging.getLogger("psycopg.pool").info("connection requested from 'people-pubs-api'")
    logging.getLogger("people_pubs.api").info("kept")
    assert stream.getvalue().count("\n") == 1 and "kept" in stream.getvalue()


def test_libpq_settings_are_removed_from_the_service_environment() -> None:
    environ = {"PGHOST": "db", "PGPASSWORD": MARKER, "PGSERVICEFILE": "/dev/null", "PGOPTIONS": "-c x=y",
               "PATH": "/usr/bin", "PEOPLE_PUBS_API_DSN": DSN, "APP_PGLIKE": "kept"}
    assert server.remove_libpq_settings(environ) == ["PGHOST", "PGOPTIONS", "PGPASSWORD", "PGSERVICEFILE"]
    assert environ == {"PATH": "/usr/bin", "PEOPLE_PUBS_API_DSN": DSN, "APP_PGLIKE": "kept"}


def test_the_server_configuration() -> None:
    app = create_app(ReadDatabase(_settings()))
    config = server.server_config(app, _settings(host="0.0.0.0", port=8123))
    assert (config.host, config.port, config.uds) == ("0.0.0.0", 8123, None)
    assert config.lifespan == "on" and config.log_config is None
    assert config.proxy_headers is False and config.server_header is False
    assert config.timeout_graceful_shutdown == server.GRACEFUL_SHUTDOWN_SECONDS < 10
    assert config.limit_concurrency == server.MAX_CONCURRENCY
    assert (config.http, config.ws, config.loop) == ("h11", "none", "asyncio")
    unix = server.server_config(app, _settings(unix_socket="/run/api/api.sock"))
    assert unix.uds == "/run/api/api.sock"


def test_the_service_imports_no_ingestion_configuration_or_provider_code() -> None:
    code = (
        "import sys, people_pubs.api.server; "
        "print(' '.join(sorted(name for name in sys.modules if name.split('.')[0] in ('people_pubs', 'httpx'))))"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=DB_DIR, env=_child_environment(),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    # people_pubs.password_file is the shared reader of password files, and
    # people_pubs.connection_security the connection options shared with
    # ingestion; neither imports anything of the package.
    assert proc.stdout.split() == [
        "people_pubs", "people_pubs.api", "people_pubs.api.app", "people_pubs.api.database",
        "people_pubs.api.server", "people_pubs.api.settings", "people_pubs.connection_security",
        "people_pubs.password_file",
    ]


# --- the server process ------------------------------------------------------

def _child_environment(**extra: str) -> dict:
    """The test environment, network guard included, with the package on the
    path and no libpq settings but those a test adds."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("PG")}
    env["PYTHONPATH"] = os.pathsep.join(part for part in (env.get("PYTHONPATH"), str(DB_DIR)) if part)
    env.update(extra)
    return env


class Service:
    """`python -m people_pubs.api` on a Unix-domain socket in tmp_path."""

    def __init__(self, tmp_path: Path, **extra: str) -> None:
        self.socket = tmp_path / "api.sock"
        self.log = tmp_path / "service.log"
        settings = {
            # Never contacted: the service does not connect until a request needs it.
            "PEOPLE_PUBS_API_DSN": "host=db.example.invalid dbname=people_db user=app_readonly",
            "PEOPLE_PUBS_API_UNIX_SOCKET": str(self.socket),
        }
        env = _child_environment(**{**settings, **extra})
        with open(self.log, "wb") as log:
            self.proc = subprocess.Popen([sys.executable, "-m", "people_pubs.api"], cwd=tmp_path, env=env,
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

    def wait_until_listening(self, seconds: float = 30) -> None:
        deadline = time.monotonic() + seconds
        while not (self.socket.exists() and "Uvicorn running on unix socket" in self.output()):
            assert self.proc.poll() is None, self.output()
            assert time.monotonic() < deadline, self.output()
            time.sleep(0.05)

    def get(self, path: str) -> httpx.Response:
        transport = httpx.HTTPTransport(uds=str(self.socket))
        with httpx.Client(transport=transport, base_url="http://service.test", trust_env=False, timeout=10) as client:
            return client.get(path)

    def stop(self, sig: int, seconds: float = 20) -> int:
        self.proc.send_signal(sig)
        return self.proc.wait(timeout=seconds)

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=20)


@pytest.fixture
def service(tmp_path: Path):
    started = []

    def start(**extra: str) -> Service:
        started.append(Service(tmp_path, **extra))
        return started[-1]

    yield start
    for each in started:
        each.kill()


def test_the_service_starts_without_a_database_and_answers_liveness(service, tmp_path: Path) -> None:
    password_file = tmp_path / "password"
    password_file.write_text(MARKER + "\n", encoding="utf-8")
    running = service(PEOPLE_PUBS_API_DB_PASSWORD_FILE=str(password_file), PGHOST="db.example.invalid",
                      PGPASSWORD=MARKER)
    running.wait_until_listening()
    response = running.get("/livez")
    assert response.status_code == 200 and response.json() == {"status": "alive"}
    assert running.stop(signal.SIGTERM) == -signal.SIGTERM
    log = running.output()
    assert "ignoring libpq settings in the environment: PGHOST, PGPASSWORD" in log
    assert "as database user app_readonly" in log
    assert "Application shutdown complete" in log and "Traceback" not in log
    assert MARKER not in log


def test_sigint_stops_it_gracefully_with_status_130_and_removes_its_socket(service) -> None:
    running = service()
    running.wait_until_listening()
    started = time.monotonic()
    assert running.stop(signal.SIGINT) == server.INTERRUPTED
    assert time.monotonic() - started < server.GRACEFUL_SHUTDOWN_SECONDS + 5
    assert "Application shutdown complete" in running.output() and "Traceback" not in running.output()
    assert not running.socket.exists()


def test_a_configuration_error_exits_with_2_and_does_not_repeat_the_value(tmp_path: Path) -> None:
    env = _child_environment(PEOPLE_PUBS_API_DSN=f"postgresql://app_readonly:{MARKER}@db/people_db")
    proc = subprocess.run([sys.executable, "-m", "people_pubs.api"], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == server.CONFIGURATION_ERROR
    assert "configuration error: PEOPLE_PUBS_API_DSN must not contain a password" in proc.stderr
    assert MARKER not in proc.stderr + proc.stdout and "Traceback" not in proc.stderr


def test_an_address_in_use_exits_with_3(service) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        running = service(PEOPLE_PUBS_API_UNIX_SOCKET="", PEOPLE_PUBS_API_PORT=str(taken.getsockname()[1]))
        assert running.proc.wait(timeout=30) == server.STARTUP_FAILURE, running.output()
    assert "Traceback" not in running.output()


def test_a_service_that_cannot_listen_exits_with_3_and_leaves_what_was_there(service, tmp_path: Path) -> None:
    # A regular file where the socket should go: the bind fails, and the
    # clean-up after the failed start must not delete what is not a socket.
    (tmp_path / "api.sock").write_text("an operator's file, not a socket\n", encoding="utf-8")
    running = service()
    assert running.proc.wait(timeout=30) == server.STARTUP_FAILURE, running.output()
    assert running.socket.read_text(encoding="utf-8") == "an operator's file, not a socket\n"
