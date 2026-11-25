"""
people_pubs.db.connection

Psycopg connection helpers.
Uses PEOPLE_DB_DSN or PG* env vars and exposes db_conn context manager.
"""

# people_pubs/db/connection.py
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from people_pubs import connection_security, schema
from people_pubs.config import PEOPLE_DB_DSN

SCHEMA_REFUSED = 1


def get_conn(dsn: Optional[str] = None) -> psycopg.Connection:
    """
    Open a psycopg connection with dict_row row_factory.

    - If `dsn` is given, use that.
    - Else, use PEOPLE_DB_DSN from config if set.
    - Else, fall back to PGHOST/PGUSER/etc. from the environment.

    The server has to authenticate the connection by SCRAM, and GSSAPI
    encryption is not negotiated, unless the connection string sets
    require_auth or gssencmode, or the environment PGREQUIREAUTH or
    PGGSSENCMODE (people_pubs.connection_security).
    """
    effective_dsn = dsn or PEOPLE_DB_DSN
    params = conninfo_to_dict(effective_dsn) if effective_dsn else {}
    security = connection_security.defaults(params, os.environ)
    conn = (psycopg.connect(effective_dsn, row_factory=dict_row, **security) if effective_dsn
            else psycopg.connect(row_factory=dict_row, **security))
    try:
        # Finish the read-only check before returning an idle connection to
        # callers, which may then choose autocommit or their own transaction.
        with conn.transaction():
            schema.check_supported(conn)
    except schema.SchemaError as exc:
        conn.close()
        print(f"people_pubs: {exc}", file=sys.stderr)
        raise SystemExit(SCHEMA_REFUSED) from None
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def db_conn(dsn: Optional[str] = None) -> Iterator[psycopg.Connection]:
    """
    Context manager wrapper around get_conn().

    Example:
        from people_pubs.db.connection import db_conn

        with db_conn() as conn:
            ...
    """
    conn = get_conn(dsn)
    try:
        yield conn
    finally:
        conn.close()
