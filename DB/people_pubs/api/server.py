"""
people_pubs.api.server

`python -m people_pubs.api`: run the read service under uvicorn.

The service is configured by the PEOPLE_PUBS_API_* variables alone
(people_pubs.api.settings). Before anything connects it removes libpq's PG*
variables from its own environment, so that settings meant for ingestion in
the same environment cannot redirect its connections. Logs go to stderr as
plain text, with the database password masked in every line, tracebacks
included. Start-up does not contact the database: the service starts while
the database is still down, /readyz reports when it can be used, and the pool
connects once a request needs it.

SIGTERM and SIGINT stop it gracefully: it accepts no new connections, lets
requests in flight finish for up to GRACEFUL_SHUTDOWN_SECONDS and closes the
pool. uvicorn then re-raises SIGTERM, so the process ends by that signal
(status 143 seen from a shell); as process 1 of a container without an init
process, where the kernel ignores the re-raised signal, it exits with 0. After
SIGINT it exits with 130.

Other exit statuses: 2 for a configuration error, whose message names the
variable at fault and never its value; 3 when the service could not start,
for example because the address is in use.
"""
from __future__ import annotations

import argparse
import logging
import os
import stat
import sys
from typing import IO, List, MutableMapping, Optional, Sequence

import uvicorn
from starlette.types import ASGIApp

from people_pubs.api.app import create_app
from people_pubs.api.database import ReadDatabase
from people_pubs.api.settings import Settings, SettingsError, load_settings

LOGGER = logging.getLogger("people_pubs.api")

CONFIGURATION_ERROR = 2
STARTUP_FAILURE = 3
INTERRUPTED = 130
#: Within the ten seconds Docker and Compose give a container to stop.
GRACEFUL_SHUTDOWN_SECONDS = 8
KEEP_ALIVE_SECONDS = 5
#: Connections and tasks at most; beyond that uvicorn answers 503 itself.
MAX_CONCURRENCY = 100
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MASK = "<password>"


class MaskingFormatter(logging.Formatter):
    """Formats a record, then replaces every secret in the result, so the
    message, its arguments and a traceback are all covered."""

    def __init__(self, fmt: str, secrets: Sequence[str]) -> None:
        super().__init__(fmt)
        self._secrets = [secret for secret in secrets if secret]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return text


def configure_logging(level: str, secrets: Sequence[str] = (), stream: Optional[IO[str]] = None) -> logging.Handler:
    """One stderr handler on the root logger, masking secrets; uvicorn's
    loggers propagate to it."""
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(MaskingFormatter(LOG_FORMAT, secrets))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # The pool logs every checkout and return at INFO.
    logging.getLogger("psycopg.pool").setLevel(logging.NOTSET if root.level <= logging.DEBUG else logging.WARNING)
    return handler


def remove_libpq_settings(environ: MutableMapping[str, str]) -> List[str]:
    """Removes libpq's PG* variables from environ; returns their names."""
    names = sorted(name for name in environ if name.startswith("PG"))
    for name in names:
        del environ[name]
    return names


def server_config(app: ASGIApp, settings: Settings) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        uds=settings.unix_socket,
        loop="asyncio",
        http="h11",
        ws="none",
        lifespan="on",
        log_config=None,
        access_log=True,
        # Client addresses are only logged, so forwarded headers are not trusted.
        proxy_headers=False,
        server_header=False,
        timeout_keep_alive=KEEP_ALIVE_SECONDS,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        limit_concurrency=MAX_CONCURRENCY,
    )


def _remove_socket(path: Optional[str]) -> None:
    """Removes the service's Unix-domain socket, and nothing that is not one."""
    if not path:
        return
    try:
        if stat.S_ISSOCK(os.lstat(path).st_mode):
            os.remove(path)
    except OSError:
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    argparse.ArgumentParser(
        prog="python -m people_pubs.api",
        description=(
            "Run the read service. It takes no arguments: the PEOPLE_PUBS_API_* "
            "environment variables configure it (DB/README.md, section 11)."
        ),
    ).parse_args(argv)
    try:
        settings = load_settings(os.environ)
    except SettingsError as exc:
        print(f"people_pubs.api: configuration error: {exc}", file=sys.stderr)
        return CONFIGURATION_ERROR
    configure_logging(settings.log_level, [settings.password] if settings.password else [])
    removed = remove_libpq_settings(os.environ)
    if removed:
        LOGGER.info("ignoring libpq settings in the environment: %s", ", ".join(removed))
    server = uvicorn.Server(server_config(create_app(ReadDatabase(settings)), settings))
    LOGGER.info(
        "read service starting as database user %s, with at most %d database connections",
        settings.user,
        settings.pool_size,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        return INTERRUPTED
    except OSError as exc:
        # uvicorn reports a failed TCP bind itself (status 3) but lets the
        # error of a failed Unix-socket bind escape.
        if server.started:
            raise
        LOGGER.error("could not listen: %s", exc)
        return STARTUP_FAILURE
    finally:
        _remove_socket(settings.unix_socket)
    return 0 if server.started else STARTUP_FAILURE
