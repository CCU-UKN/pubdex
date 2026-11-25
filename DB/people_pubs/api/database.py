"""
people_pubs.api.database

The read service's database access: one pool of read-only sessions and two
fixed queries over the serving snapshot biblio.publications_canon_web.

Every session the pool opens runs with default_transaction_read_only=on and
the configured statement_timeout, after any options the connection string
sets, so neither can be configured away: even a role that could write cannot
write through the service. The pool starts empty and opens connections when a
request needs one, at most PEOPLE_PUBS_API_POOL_SIZE, and a request waits at
most PEOPLE_PUBS_API_DB_WAIT_SECONDS for one. A connection the server has
dropped, after a restart for example, is noticed when it is taken from the
pool and replaced.

The server has to authenticate every session by SCRAM, or, when the service
has no password, may not ask for any authentication, and GSSAPI encryption is
not negotiated, unless the connection string sets require_auth or gssencmode
(people_pubs.connection_security).

Failures reach the HTTP layer as one of two exceptions, whose reason is what
the service reports:

  DatabaseUnavailable     no usable connection in time, or the query ran
                          out of time: "database_unavailable"
  ReadSurfaceUnavailable  connected, but the snapshot, a column the service
                          reads or the privilege to read it is missing, or
                          the snapshot has not been populated:
                          "read_surface_unavailable"

Neither carries the database's message; the failure is logged by exception
class and SQLSTATE only.
"""
from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Type

import psycopg
from psycopg import errors, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from people_pubs import connection_security, schema
from people_pubs.api.settings import Settings, parse_dsn

LOGGER = logging.getLogger(__name__)

#: The fields the service reads and returns: the columns of the CSV export
#: (people_pubs.sync.export_publications), in the same order.
PUBLICATION_FIELDS = ("pub_id", "doi", "title", "year", "venue", "source_of_truth", "internal_authors")
#: The serving snapshot of the canonical list.
READ_SURFACE = ("biblio", "publications_canon_web")
#: Also the pool's name in log messages; shows in pg_stat_activity.
APPLICATION_NAME = "people-pubs-api"
#: How long the pool keeps retrying a connection it could not open before it
#: gives up; the next request that needs a connection then starts afresh.
RECONNECT_TIMEOUT_SECONDS = 10.0
#: How long closing the pool waits for its worker threads.
CLOSE_TIMEOUT_SECONDS = 5.0

_COLUMNS = sql.SQL(", ").join(sql.Identifier(name) for name in PUBLICATION_FIELDS)
_RELATION = sql.Identifier(*READ_SURFACE)
LIST_PUBLICATIONS = sql.SQL(
    "SELECT {columns} FROM {relation} WHERE pub_id > %(after)s ORDER BY pub_id LIMIT %(limit)s"
).format(columns=_COLUMNS, relation=_RELATION)
#: Readiness: the exact column list from the exact relation, as the service's
#: role, without reading a row. Fails on a missing schema, relation or column,
#: a missing privilege and an unpopulated snapshot alike.
CHECK_READ_SURFACE = sql.SQL("SELECT {columns} FROM {relation} LIMIT 0").format(
    columns=_COLUMNS, relation=_RELATION
)


class ReadUnavailable(Exception):
    """The service cannot read what it serves right now."""

    reason = "database_unavailable"


class DatabaseUnavailable(ReadUnavailable):
    reason = "database_unavailable"


class ReadSurfaceUnavailable(ReadUnavailable):
    reason = "read_surface_unavailable"


class SchemaUnavailable(ReadUnavailable):
    """A schema refusal safe to show to the operator."""

    def __init__(self, refusal: schema.Refused) -> None:
        super().__init__(str(refusal))
        self.reason = refusal.reason


# Connected, but what the service reads is missing, not readable by its role,
# or not populated (SQLSTATE 55000, for a snapshot created WITH NO DATA).
_READ_SURFACE_ERRORS = (
    errors.InvalidSchemaName,
    errors.UndefinedTable,
    errors.UndefinedColumn,
    errors.InsufficientPrivilege,
    errors.ObjectNotInPrerequisiteState,
)


def unavailable_error(exc: BaseException) -> Optional[Type[ReadUnavailable]]:
    """Which of the two failures exc is, or None if it is neither."""
    if isinstance(exc, _READ_SURFACE_ERRORS):
        return ReadSurfaceUnavailable
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        # Covers the pool's timeout, a closed pool, a statement timeout
        # (QueryCanceled), a server shutting down and a dropped connection.
        return DatabaseUnavailable
    return None


def connection_kwargs(settings: Settings) -> Dict[str, Any]:
    """What the pool adds to the connection string for every connection."""
    params = parse_dsn(settings.dsn)
    session = (
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={settings.statement_timeout_ms}"
    )
    # A later -c wins, so these come after any options the string sets.
    options = " ".join(part for part in (str(params.get("options") or "").strip(), session) if part)
    kwargs: Dict[str, Any] = {
        "options": options,
        # libpq waits at least two seconds whatever is set.
        "connect_timeout": max(2, math.ceil(settings.db_wait_seconds)),
        "autocommit": True,
        "row_factory": dict_row,
    }
    if not params.get("application_name"):
        kwargs["application_name"] = APPLICATION_NAME
    if settings.password is not None:
        # A keyword argument, not part of the stored connection string.
        kwargs["password"] = settings.password
    # SCRAM with a password, no authentication at all without one, and no
    # GSSAPI encryption, unless the connection string chooses otherwise. The
    # service reads no PG* variable, so only the string can.
    required = connection_security.SCRAM if settings.password is not None else connection_security.NO_AUTHENTICATION
    kwargs.update(connection_security.defaults(params, {}, required))
    return kwargs


def _reconnect_failed(pool: ConnectionPool) -> None:
    LOGGER.warning(
        "no database connection could be opened for %gs; the next request that needs one tries again",
        RECONNECT_TIMEOUT_SECONDS,
    )


class ReadDatabase:
    """The service's database: a pool of read-only sessions and its queries."""

    def __init__(self, settings: Settings) -> None:
        self._pool = ConnectionPool(
            settings.dsn,
            kwargs=connection_kwargs(settings),
            min_size=0,
            max_size=settings.pool_size,
            open=False,
            timeout=settings.db_wait_seconds,
            reconnect_timeout=RECONNECT_TIMEOUT_SECONDS,
            reconnect_failed=_reconnect_failed,
            check=ConnectionPool.check_connection,
            name=APPLICATION_NAME,
        )

    @property
    def pool(self) -> ConnectionPool:
        return self._pool

    def open(self) -> None:
        """Start accepting requests; connections open when they are needed."""
        self._pool.open(wait=False)

    def close(self) -> None:
        self._pool.close(timeout=CLOSE_TIMEOUT_SECONDS)

    def check(self) -> None:
        """Raise ReadUnavailable unless a connection is available in time and
        the read surface can be read with it."""
        with self._session() as conn:
            conn.execute(CHECK_READ_SURFACE)

    def list_publications(self, *, after: int, limit: int) -> List[Dict[str, Any]]:
        """Up to limit snapshot rows with a pub_id greater than after, by pub_id."""
        with self._session() as conn:
            return conn.execute(LIST_PUBLICATIONS, {"after": after, "limit": limit}).fetchall()

    @contextmanager
    def _session(self) -> Iterator[psycopg.Connection]:
        try:
            with self._pool.connection() as conn:
                try:
                    schema.check_supported(conn)
                except schema.Refused as exc:
                    LOGGER.warning("%s: %s", exc.reason, exc)
                    raise SchemaUnavailable(exc) from None
                yield conn
        except Exception as exc:
            failure = unavailable_error(exc)
            if failure is None:
                raise
            sqlstate = getattr(exc, "sqlstate", None)
            LOGGER.warning(
                "%s: %s%s", failure.reason, type(exc).__name__, f" (SQLSTATE {sqlstate})" if sqlstate else ""
            )
            raise failure() from None
