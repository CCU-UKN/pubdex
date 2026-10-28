"""
people_pubs.api.settings

Configuration of the read service, from its own environment variables and
nothing else:

  PEOPLE_PUBS_API_DSN                   required: a libpq keyword string or
                                        URI naming the server, the database
                                        and the user -- normally app_readonly
                                        -- but no password
  PEOPLE_PUBS_API_DB_PASSWORD_FILE      a file holding that user's password;
                                        one trailing newline is removed. Leave
                                        it unset for a server that asks for no
                                        password.
  PEOPLE_PUBS_API_HOST                  listen address (default 127.0.0.1)
  PEOPLE_PUBS_API_PORT                  listen port (default 8000)
  PEOPLE_PUBS_API_UNIX_SOCKET           listen on this Unix-domain socket
                                        instead of the address and port
  PEOPLE_PUBS_API_POOL_SIZE             database connections at most (default 4)
  PEOPLE_PUBS_API_DB_WAIT_SECONDS       longest wait for a database connection,
                                        per request and per readiness check
                                        (default 5)
  PEOPLE_PUBS_API_STATEMENT_TIMEOUT_MS  longest a query may run (default 5000)
  PEOPLE_PUBS_API_LOG_LEVEL             DEBUG, INFO, WARNING or ERROR
                                        (default INFO)

An empty value counts as unset. The password is accepted from a file only, so
that it reaches neither the environment nor a command line, and the
connection string may not carry one. Errors name the variable at fault, never
its value: a connection string that does not parse can still hold a password
the operator meant to remove.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional

from psycopg.conninfo import conninfo_to_dict

from people_pubs import password_file

DSN_VARIABLE = "PEOPLE_PUBS_API_DSN"
PASSWORD_FILE_VARIABLE = "PEOPLE_PUBS_API_DB_PASSWORD_FILE"
HOST_VARIABLE = "PEOPLE_PUBS_API_HOST"
PORT_VARIABLE = "PEOPLE_PUBS_API_PORT"
UNIX_SOCKET_VARIABLE = "PEOPLE_PUBS_API_UNIX_SOCKET"
POOL_SIZE_VARIABLE = "PEOPLE_PUBS_API_POOL_SIZE"
DB_WAIT_VARIABLE = "PEOPLE_PUBS_API_DB_WAIT_SECONDS"
STATEMENT_TIMEOUT_VARIABLE = "PEOPLE_PUBS_API_STATEMENT_TIMEOUT_MS"
LOG_LEVEL_VARIABLE = "PEOPLE_PUBS_API_LOG_LEVEL"

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
#: A password file larger than this is refused rather than read.
MAX_PASSWORD_BYTES = password_file.MAX_PASSWORD_BYTES

_WHOLE_NUMBER = re.compile(r"[0-9]{1,9}")
_DECIMAL = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6})?")


class SettingsError(ValueError):
    """The service cannot start with this configuration."""


@dataclass(frozen=True)
class Settings:
    """What the read service runs with. The password is left out of repr()."""

    dsn: str
    user: str
    password: Optional[str] = field(default=None, repr=False)
    host: str = "127.0.0.1"
    port: int = 8000
    unix_socket: Optional[str] = None
    pool_size: int = 4
    db_wait_seconds: float = 5.0
    statement_timeout_ms: int = 5000
    log_level: str = "INFO"


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Settings from the service's environment variables; SettingsError if
    any of them cannot be used."""
    dsn = _value(environ, DSN_VARIABLE)
    if dsn is None:
        raise SettingsError(
            f"{DSN_VARIABLE} is not set; it names the database server, the database "
            "and the read-only user, for example 'host=db dbname=people_db user=app_readonly'"
        )
    params = parse_dsn(dsn)
    password_path = _value(environ, PASSWORD_FILE_VARIABLE)
    host = _value(environ, HOST_VARIABLE) or "127.0.0.1"
    if any(c.isspace() for c in host):
        raise SettingsError(f"{HOST_VARIABLE} must be a host name or address without spaces")
    unix_socket = _value(environ, UNIX_SOCKET_VARIABLE)
    if unix_socket is not None and not os.path.isabs(unix_socket):
        raise SettingsError(f"{UNIX_SOCKET_VARIABLE} must be an absolute path")
    log_level = (_value(environ, LOG_LEVEL_VARIABLE) or "INFO").upper()
    if log_level not in LOG_LEVELS:
        raise SettingsError(f"{LOG_LEVEL_VARIABLE} must be one of {', '.join(LOG_LEVELS)}")
    return Settings(
        dsn=dsn,
        user=str(params["user"]),
        password=read_password_file(password_path) if password_path is not None else None,
        host=host,
        port=_whole_number(environ, PORT_VARIABLE, 8000, 1, 65535),
        unix_socket=unix_socket,
        pool_size=_whole_number(environ, POOL_SIZE_VARIABLE, 4, 1, 32),
        db_wait_seconds=_seconds(environ, DB_WAIT_VARIABLE, 5.0, 0.1, 300.0),
        statement_timeout_ms=_whole_number(environ, STATEMENT_TIMEOUT_VARIABLE, 5000, 100, 600000),
        log_level=log_level,
    )


def parse_dsn(dsn: str) -> dict:
    """The connection parameters of the service's connection string."""
    try:
        params = conninfo_to_dict(dsn)
    except Exception:
        # The parser's message quotes the text it could not read.
        raise SettingsError(f"{DSN_VARIABLE} is not a valid libpq connection string or URI") from None
    if "password" in params:
        raise SettingsError(
            f"{DSN_VARIABLE} must not contain a password; put the password in a file "
            f"and name that file in {PASSWORD_FILE_VARIABLE}"
        )
    if not params.get("user"):
        raise SettingsError(f"{DSN_VARIABLE} must name the database user (user=...), normally app_readonly")
    return params


def read_password_file(path: str) -> str:
    """The password the file holds, without one trailing line break.

    The file has to be UTF-8 text of one line (people_pubs.password_file);
    anything else is refused, so that a wrong file is reported instead of
    being sent as a password.
    """
    try:
        return password_file.read_password_file(path, PASSWORD_FILE_VARIABLE)
    except password_file.PasswordFileError as exc:
        raise SettingsError(str(exc)) from None


def _value(environ: Mapping[str, str], name: str) -> Optional[str]:
    value = environ.get(name, "").strip()
    return value or None


def _whole_number(environ: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    text = _value(environ, name)
    if text is None:
        return default
    if not _WHOLE_NUMBER.fullmatch(text) or not low <= int(text) <= high:
        raise SettingsError(f"{name} must be a whole number from {low} to {high}")
    return int(text)


def _seconds(environ: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    text = _value(environ, name)
    if text is None:
        return default
    if not _DECIMAL.fullmatch(text) or not low <= float(text) <= high:
        raise SettingsError(f"{name} must be a number of seconds from {low:g} to {high:g}")
    return float(text)
