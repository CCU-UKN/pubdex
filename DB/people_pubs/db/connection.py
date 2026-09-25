"""
people_pubs.db.connection

Psycopg connection helpers.
Uses PEOPLE_DB_DSN or PG* env vars and exposes db_conn context manager.
"""

# people_pubs/db/connection.py
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row

from people_pubs.config import PEOPLE_DB_DSN


def get_conn(dsn: Optional[str] = None) -> psycopg.Connection:
    """
    Open a psycopg connection with dict_row row_factory.

    - If `dsn` is given, use that.
    - Else, use PEOPLE_DB_DSN from config if set.
    - Else, fall back to PGHOST/PGUSER/etc. from the environment.
    """
    effective_dsn = dsn or PEOPLE_DB_DSN
    if effective_dsn:
        return psycopg.connect(effective_dsn, row_factory=dict_row)
    return psycopg.connect(row_factory=dict_row)


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
